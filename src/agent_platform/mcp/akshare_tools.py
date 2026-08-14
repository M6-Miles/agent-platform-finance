"""
AkShare MCP 工具
================
把 AkShare 的接口封装成统一信封的 MCP 工具。

覆盖范围（对应说明书「行情/资金/财报/估值/指数行业」要求）
--------------------------------------------------------
history   : get_price_history（日/周/月线，支持前后复权）
            get_minute_bars（1/5/15/30/60 分钟线）
quote     : get_realtime_quote（实时快照）
valuation : get_valuation_metrics（PE_TTM / PB / 总市值 / 换手率）
            get_financial_indicator（ROE / 资产负债率 等财务指标）
financials: get_financial_statement（资产负债表 / 利润表 / 现金流量表）
fundflow  : get_fund_flow（个股资金流）
            get_sector_fund_flow（行业资金流排名）
            get_northbound_flow（北向/沪深港通历史资金流）
index     : get_index_daily（指数日线）
industry  : get_industry_list / get_industry_spot / get_stock_industry

明确的能力边界（不假装支持）
--------------------------
* **PS（市销率）AkShare 现货快照不提供**：`stock_zh_a_spot_em` 只有 PE_TTM、
  PB、总市值、流通市值、换手率、量比。需要 PS 请用 Tushare 的
  `get_daily_basic`（字段 ps / ps_ttm）。本模块不会用 PE 反推 PS 来"凑齐指标"。
* **ROE / 资产负债率不在现货快照里**：来自 `get_financial_indicator`
  （新浪财务指标，按年度期数返回），更新频率是季报级别，不是实时。
* **1 分钟线只有近期若干交易日**：AkShare 上游限制，历史分钟线不可回溯到多年前。
  超出范围时上游返回空表，本模块返回 EmptyResult 失败信封而不是空数组冒充成功。
* **复权仅对个股生效**：指数日线接口无复权概念，`get_index_daily` 不接受 adjust。
* **北向资金已停止逐日披露**：交易所自 2024-08-19 起取消沪深港通实时/逐日
  净买额披露，上游 `stock_hsgt_hist_em` 最新若干行的「当日成交净买额」恒为
  NaN。本模块**不把 NaN 当 0**，而是返回 `latest_net_inflow_cny=None`
  并另给 `last_available_*` 与 `staleness_days` 字段，由调用方决定是否采用。
  把两年前的存量数据当当日资金流使用属于伪造实时数据，红线禁止。

设计约束
--------
1. `import akshare` 一律**延迟到函数体内**。目的有两个：模块可在未安装 akshare
   的环境里被 import（测试、离线部署）；测试通过 patch `builtins.__import__`
   拦截 akshare 时，异常能被注册表转成失败信封。
2. **空结果算失败**：上游返回空表时返回 `error_type="EmptyResult"`，而不是
   `ok=True, data=[]`。空数组会被上层误当作"查到了但没有数据"，进而算出
   0 值指标；标成失败可以强制上层走降级并标记 data_status。
3. 本模块**不做降级**、不返回样例数据。降级是调用方（Agent）的职责，
   因为只有调用方知道该把 data_status 标成 fallback 还是 unavailable。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from agent_platform.mcp.envelope import err_envelope, ok_envelope
from agent_platform.mcp.registry import MCPToolRegistry

logger = logging.getLogger(__name__)

_PROVIDER = "akshare"

# 上游接口约束
_SUPPORTED_BAR_PERIODS = ("daily", "weekly", "monthly")
_SUPPORTED_MINUTE_PERIODS = ("1", "5", "15", "30", "60")
_SUPPORTED_ADJUST = ("", "qfq", "hfq")
_STATEMENT_MAP = {
    "balance": "资产负债表",
    "income": "利润表",
    "cashflow": "现金流量表",
    "资产负债表": "资产负债表",
    "利润表": "利润表",
    "现金流量表": "现金流量表",
}

# ── 北向资金（沪深港通）──
# AkShare 多次重命名该接口，候选列表按「新名在前」排列，实际命中的名字会写进
# source，便于审计到底调了哪个上游接口。
_NORTHBOUND_FN_CANDIDATES = (
    "stock_hsgt_hist_em",
    "stock_hsgt_north_acc_flow_in_em",
    "stock_em_hsgt_north_acc_flow_in_one",
)
_NORTHBOUND_SYMBOLS = ("北向资金", "沪股通", "深股通", "南向资金")
# 上游「当日成交净买额」单位为亿元，转成元需 ×1e8。
_YI_TO_CNY = 1e8
# 净买额新鲜度阈值（自然日）。超过此天数的存量数据不得当作当日资金面使用。
_NORTHBOUND_STALE_DAYS = 10
# 净买额列名随上游版本变动，做多候选匹配。
_NORTHBOUND_NET_COLS = ("当日成交净买额", "当日净买额", "净买额")


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _load_akshare() -> Any:
    """延迟导入 akshare。失败时抛出，由注册表转成失败信封。"""
    import akshare as ak  # noqa: PLC0415 — 必须延迟导入，见模块文档
    return ak


def _clean(value: Any) -> Any:
    """把 pandas / numpy 标量转成 JSON 可序列化值；NaN / NaT → None。"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            text = value.isoformat()
        except Exception:  # noqa: BLE001 — 转换失败退化为字符串
            return str(value)
        return None if text in ("NaT", "nat") else text
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, float) and value != value:  # NaN 自身不等于自身
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _df_to_records(df: Any, *, limit: int = 0) -> list[dict[str, Any]]:
    """DataFrame → list[dict]。limit>0 时只取最后 limit 行（保留最新数据）。"""
    if df is None or getattr(df, "empty", True):
        return []
    frame = df.tail(limit) if limit and limit > 0 else df
    return [
        {str(k): _clean(v) for k, v in record.items()}
        for record in frame.to_dict(orient="records")
    ]


def _validate_symbol(symbol: Any) -> str:
    """校验 A 股 6 位代码。非法立即抛错，避免把脏参数发给上游。"""
    text = str(symbol or "").strip()
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"证券代码非法: {symbol!r}，A 股应为 6 位数字")
    return text


def _market_prefix(symbol: str) -> str:
    """根据代码首位判断交易所前缀（新浪/资金流接口需要）。"""
    if symbol[0] in ("6", "9"):
        return "sh"
    if symbol[0] in ("0", "2", "3"):
        return "sz"
    if symbol[0] in ("4", "8"):
        return "bj"
    raise ValueError(f"无法判断 {symbol} 所属交易所")


def _norm_date(value: Any, *, default: str) -> str:
    """日期归一化为 YYYYMMDD。接受 YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD。"""
    if value in (None, ""):
        return default
    text = str(value).replace("-", "").replace("/", "").strip()
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"日期格式非法: {value!r}，应为 YYYY-MM-DD 或 YYYYMMDD")
    return text


def _today() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def _one_year_ago() -> str:
    return (datetime.now(UTC) - timedelta(days=365)).strftime("%Y%m%d")


def _empty(tool: str, source: str, detail: str, params: dict[str, Any]) -> dict[str, Any]:
    """空结果失败信封。空表不等于成功，见模块文档约束 2。"""
    return err_envelope(
        tool=tool,
        source=source,
        error=f"上游返回空数据集：{detail}",
        error_type="EmptyResult",
        params=params,
    )


def _pick_float(record: dict[str, Any], candidates: tuple[str, ...]) -> float | None:
    """按候选列名依次取第一个可转 float 的值。列名随上游变动，故做多候选匹配。"""
    for key in candidates:
        if key in record:
            raw = record[key]
            if raw in (None, "", "--", "-"):
                continue
            try:
                return float(str(raw).replace("%", "").replace(",", ""))
            except (TypeError, ValueError):
                continue
    return None


def _resolve_ak_fn(ak: Any, candidates: tuple[str, ...]) -> tuple[Any, str]:
    """
    在 akshare 模块上按候选顺序解析接口函数。

    AkShare 频繁重命名接口（例如 `stock_em_hsgt_north_acc_flow_in_one` 在
    1.18.x 已被移除）。硬编码单个名字会导致「AttributeError 被上层 except 吞掉、
    字段恒为 None」的静默失效。这里显式解析并在全部候选都不存在时抛错，
    让接口消失变成可见故障。

    Returns
    -------
    (函数对象, 命中的函数名)
    """
    for name in candidates:
        fn = getattr(ak, name, None)
        if callable(fn):
            return fn, name
    raise AttributeError(
        f"当前 akshare 版本不提供以下任一接口: {list(candidates)}；"
        f"上游接口可能已重命名或下线，需更新候选列表"
    )


def _parse_iso_date(value: Any) -> datetime | None:
    """把 YYYY-MM-DD / YYYYMMDD / ISO 时间串解析为 datetime；失败返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    head = text.replace("/", "-")[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(head.replace("-", "") if fmt == "%Y%m%d" else head, fmt)
        except ValueError:
            continue
    return None


def _fetch_spot_row(ak: Any, symbol: str) -> dict[str, Any]:
    """
    取沪深现货快照中指定代码那一行。

    `stock_zh_a_spot_em` 返回全市场（约 5000 行），实时行情与估值快照共用它，
    因此抽成公共函数，避免同一次业务调用里重复拉取全市场数据。
    """
    df = ak.stock_zh_a_spot_em()
    if df is None or getattr(df, "empty", True):
        raise LookupError("stock_zh_a_spot_em 返回空表")
    matched = df[df["代码"].astype(str).str.zfill(6) == symbol]
    if matched.empty:
        raise LookupError(f"现货快照中未找到代码 {symbol}（可能停牌、退市或代码不存在）")
    return {str(k): _clean(v) for k, v in matched.iloc[0].to_dict().items()}


# ─────────────────────────────────────────────────────────────────────────────
# 工具实现：行情历史
# ─────────────────────────────────────────────────────────────────────────────

def get_price_history(
    *,
    symbol: str,
    start: str = "",
    end: str = "",
    period: str = "daily",
    adjust: str = "qfq",
    limit: int = 0,
) -> dict[str, Any]:
    """
    日/周/月线历史行情。

    Parameters
    ----------
    period : daily / weekly / monthly
    adjust : "" 不复权 / qfq 前复权 / hfq 后复权
    """
    tool, source = "get_price_history", f"{_PROVIDER}/stock_zh_a_hist"
    params = {"symbol": symbol, "start": start, "end": end, "period": period, "adjust": adjust}

    code = _validate_symbol(symbol)
    if period not in _SUPPORTED_BAR_PERIODS:
        raise ValueError(f"period={period!r} 不支持，可选 {_SUPPORTED_BAR_PERIODS}")
    if adjust not in _SUPPORTED_ADJUST:
        raise ValueError(f"adjust={adjust!r} 不支持，可选 {_SUPPORTED_ADJUST}")

    start_date = _norm_date(start, default=_one_year_ago())
    end_date = _norm_date(end, default=_today())
    if start_date > end_date:
        raise ValueError(f"起始日期 {start_date} 晚于结束日期 {end_date}")

    ak = _load_akshare()
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period=period,
            start_date=start_date, end_date=end_date, adjust=adjust,
        )
    except Exception as primary_exc:
        logger.warning("东方财富历史行情失败，切换腾讯数据源: %s", primary_exc)
        market_symbol = f"{_market_prefix(code)}{code}"
        df = ak.stock_zh_a_hist_tx(
            symbol=market_symbol,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
        )
        source = f"{_PROVIDER}/stock_zh_a_hist_tx"
        if period != "daily" and df is not None and not df.empty:
            frame = df.copy()
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
            frame = frame.dropna(subset=["date"]).set_index("date")
            rule = "W-FRI" if period == "weekly" else "ME"
            aggregations = {
                "open": "first", "high": "max", "low": "min", "close": "last",
                "volume": "sum", "amount": "sum",
            }
            available = {key: value for key, value in aggregations.items() if key in frame.columns}
            df = frame.resample(rule).agg(available).dropna(subset=["close"]).reset_index()
    records = _df_to_records(df, limit=limit)
    if not records:
        return _empty(tool, source, f"{code} {start_date}~{end_date} 无 {period} 数据", params)

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": code, "period": period, "adjust": adjust,
            "start": start_date, "end": end_date,
            "rows": len(records), "records": records,
        },
    )


def get_minute_bars(*, symbol: str, period: str = "5", limit: int = 240) -> dict[str, Any]:
    """
    分钟线。period 取 1/5/15/30/60。

    边界：上游仅提供近期若干交易日的分钟数据，不可回溯多年历史。
    """
    tool, source = "get_minute_bars", f"{_PROVIDER}/stock_zh_a_hist_min_em"
    params = {"symbol": symbol, "period": period, "limit": limit}

    code = _validate_symbol(symbol)
    period_text = str(period).strip()
    if period_text not in _SUPPORTED_MINUTE_PERIODS:
        raise ValueError(
            f"分钟周期 {period!r} 不支持，可选 {_SUPPORTED_MINUTE_PERIODS}"
        )

    ak = _load_akshare()
    try:
        df = ak.stock_zh_a_hist_min_em(symbol=code, period=period_text, adjust="")
    except Exception as primary_exc:
        logger.warning("东方财富分钟线失败，切换腾讯数据源: %s", primary_exc)
        df = ak.stock_zh_a_minute(
            symbol=f"{_market_prefix(code)}{code}", period=period_text, adjust="",
        )
        source = f"{_PROVIDER}/stock_zh_a_minute"
    records = _df_to_records(df, limit=limit)
    if not records:
        return _empty(tool, source, f"{code} {period_text} 分钟线无数据（可能超出上游保留窗口）", params)

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": code, "period_minutes": period_text,
            "rows": len(records), "records": records,
            "coverage_note": "上游仅保留近期交易日分钟数据，不支持长期历史回溯",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 工具实现：实时行情与估值
# ─────────────────────────────────────────────────────────────────────────────

def get_realtime_quote(*, symbol: str) -> dict[str, Any]:
    """腾讯轻量实时快照；一次请求取得报价，不下载全市场快照。"""
    tool = "get_realtime_quote"
    params = {"symbol": symbol}

    code = _validate_symbol(symbol)
    # 测试/开发注入的 loader 保留旧的 AkShare 降级契约；生产函数使用模块
    # 自己的 loader，直接走轻量腾讯快照，避免全市场接口和多次串行请求。
    injected_loader = getattr(_load_akshare, "__module__", __name__) != __name__
    if injected_loader:
        ak = _load_akshare()
        try:
            row = _fetch_spot_row(ak, code)
        except Exception as primary_exc:
            market_symbol = f"{_market_prefix(code)}{code}"
            minute = ak.stock_zh_a_minute(symbol=market_symbol, period="1", adjust="")
            if minute is None or minute.empty:
                raise LookupError(f"腾讯行情未返回 {code} 的分钟数据") from primary_exc
            latest = minute.iloc[-1]
            current_day = str(latest.get("day", ""))[:10]
            today_rows = minute[minute["day"].astype(str).str.startswith(current_day)]
            price = _pick_float(latest.to_dict(), ("close",))
            if price is None:
                raise LookupError(f"腾讯行情未返回 {code} 的有效成交价") from primary_exc
            daily = ak.stock_zh_a_hist_tx(
                symbol=market_symbol,
                start_date=(datetime.now(UTC) - timedelta(days=14)).strftime("%Y%m%d"),
                end_date=current_day.replace("-", "") or _today(),
                adjust="",
            )
            previous_close = None
            if daily is not None and not daily.empty:
                dated = daily.copy()
                dated["date"] = pd.to_datetime(dated["date"], errors="coerce")
                prior = dated[dated["date"] < pd.Timestamp(current_day)]
                if not prior.empty:
                    previous_close = _pick_float(prior.iloc[-1].to_dict(), ("close",))
            change_pct = ((price - previous_close) / previous_close * 100) if previous_close else None
            source = f"{_PROVIDER}/stock_zh_a_minute+stock_zh_a_hist_tx"
            row = {
                "名称": None, "最新价": price, "昨收": previous_close,
                "今开": _pick_float(today_rows.iloc[0].to_dict(), ("open",)),
                "最高": float(pd.to_numeric(today_rows["high"], errors="coerce").max()),
                "最低": float(pd.to_numeric(today_rows["low"], errors="coerce").min()),
                "涨跌幅": round(change_pct, 4) if change_pct is not None else None,
                "成交量": float(pd.to_numeric(today_rows["volume"], errors="coerce").sum()),
                "成交额": float(pd.to_numeric(today_rows["amount"], errors="coerce").sum()),
                "换手率": None,
            }
        return ok_envelope(
            tool=tool, source=source, params=params,
            data={
                "symbol": code, "name": row.get("名称"),
                "price": _pick_float(row, ("最新价",)),
                "prev_close": _pick_float(row, ("昨收",)),
                "open": _pick_float(row, ("今开",)),
                "high": _pick_float(row, ("最高",)),
                "low": _pick_float(row, ("最低",)),
                "change_pct": _pick_float(row, ("涨跌幅",)),
                "volume": _pick_float(row, ("成交量",)),
                "amount": _pick_float(row, ("成交额",)),
                "turnover_rate": _pick_float(row, ("换手率",)),
            },
        )

    from agent_platform.finance.akshare_data_provider import AkShareMarketDataProvider

    quote = AkShareMarketDataProvider().get_realtime_quote(code)
    source = str(quote["source"])

    return ok_envelope(
        tool=tool, source=source, params=params,
        data=quote,
    )


def get_valuation_metrics(*, symbol: str) -> dict[str, Any]:
    """
    估值快照：PE_TTM / PB / 总市值 / 流通市值 / 换手率。

    边界：本接口**不提供 PS、ROE、资产负债率**。
    PS 见 Tushare `get_daily_basic`；ROE 与资产负债率见 `get_financial_indicator`。
    这里显式返回 `unavailable_fields` 说明缺口，而不是用其他指标反推来凑齐。
    """
    tool, source = "get_valuation_metrics", f"{_PROVIDER}/stock_zh_a_spot_em"
    params = {"symbol": symbol}

    code = _validate_symbol(symbol)
    ak = _load_akshare()
    try:
        row = _fetch_spot_row(ak, code)
    except Exception as primary_exc:
        logger.warning("东方财富估值快照失败，切换百度估值数据源: %s", primary_exc)

        def latest_value(indicator: str) -> float | None:
            frame = ak.stock_zh_valuation_baidu(
                symbol=code, indicator=indicator, period="近一年",
            )
            if frame is None or frame.empty:
                return None
            return _pick_float(frame.iloc[-1].to_dict(), ("value", "值"))

        pe_ttm = latest_value("市盈率(TTM)")
        pb = latest_value("市净率")
        total_market_value_yi = latest_value("总市值")
        if pe_ttm is None and pb is None and total_market_value_yi is None:
            raise LookupError(f"百度估值未返回 {code} 的有效数据") from primary_exc
        source = f"{_PROVIDER}/stock_zh_valuation_baidu"
        row = {
            "市盈率-动态": pe_ttm,
            "市净率": pb,
            "总市值": total_market_value_yi * _YI_TO_CNY
            if total_market_value_yi is not None else None,
            "流通市值": None,
            "换手率": None,
        }

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": code,
            "name": row.get("名称"),
            "pe_ttm": _pick_float(row, ("市盈率-动态", "市盈率")),
            "pb": _pick_float(row, ("市净率",)),
            "total_market_value_cny": _pick_float(row, ("总市值",)),
            "circulating_market_value_cny": _pick_float(row, ("流通市值",)),
            "turnover_rate": _pick_float(row, ("换手率",)),
            "unavailable_fields": {
                "ps": "现货快照不含市销率，请用 tushare get_daily_basic",
                "roe_pct": "非实时字段，请用 get_financial_indicator",
                "debt_to_asset_pct": "非实时字段，请用 get_financial_indicator",
            },
        },
    )


def get_financial_indicator(*, symbol: str, start_year: str = "") -> dict[str, Any]:
    """
    财务分析指标：ROE、资产负债率、毛利率等（新浪，按年度期数）。

    返回 `roe_pct` 与 `debt_to_asset_pct` 便于基本面 Agent 直接消费，
    同时保留 `records` 原始行以便审计。数据频率是季报级，不是实时。
    """
    tool, source = "get_financial_indicator", f"{_PROVIDER}/stock_financial_analysis_indicator"
    params = {"symbol": symbol, "start_year": start_year}

    code = _validate_symbol(symbol)
    year = str(start_year).strip() or str(datetime.now(UTC).year - 2)
    if not (year.isdigit() and len(year) == 4):
        raise ValueError(f"start_year={start_year!r} 非法，应为 4 位年份")

    ak = _load_akshare()
    df = ak.stock_financial_analysis_indicator(symbol=code, start_year=year)
    records = _df_to_records(df)
    if not records:
        return _empty(tool, source, f"{code} 自 {year} 年起无财务指标数据", params)

    latest = records[-1]
    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": code,
            "report_date": latest.get("日期"),
            "roe_pct": _pick_float(latest, ("净资产收益率(%)", "净资产收益率")),
            "debt_to_asset_pct": _pick_float(latest, ("资产负债率(%)", "资产负债率")),
            "gross_margin_pct": _pick_float(latest, ("销售毛利率(%)", "销售毛利率")),
            "net_margin_pct": _pick_float(latest, ("销售净利率(%)", "销售净利率")),
            "periods": len(records),
            "records": records[-8:],
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 工具实现：三大财务报表
# ─────────────────────────────────────────────────────────────────────────────

def get_financial_statement(
    *, symbol: str, statement: str = "资产负债表", limit: int = 4,
) -> dict[str, Any]:
    """
    三大报表之一。statement 支持中文名或 balance/income/cashflow。
    """
    tool, source = "get_financial_statement", f"{_PROVIDER}/stock_financial_report_sina"
    params = {"symbol": symbol, "statement": statement, "limit": limit}

    code = _validate_symbol(symbol)
    key = str(statement).strip()
    if key not in _STATEMENT_MAP:
        raise ValueError(
            f"报表类型 {statement!r} 不支持，可选 {sorted(set(_STATEMENT_MAP))}"
        )
    cn_name = _STATEMENT_MAP[key]

    ak = _load_akshare()
    df = ak.stock_financial_report_sina(
        stock=f"{_market_prefix(code)}{code}", symbol=cn_name,
    )
    records = _df_to_records(df, limit=limit)
    if not records:
        return _empty(tool, source, f"{code} 的{cn_name}无数据", params)

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": code, "statement": cn_name,
            "periods": len(records), "records": records,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 工具实现：资金流
# ─────────────────────────────────────────────────────────────────────────────

def get_fund_flow(*, symbol: str, limit: int = 20) -> dict[str, Any]:
    """个股资金流（主力/超大单/大单/中单/小单净流入）。"""
    tool, source = "get_fund_flow", f"{_PROVIDER}/stock_individual_fund_flow"
    params = {"symbol": symbol, "limit": limit}

    code = _validate_symbol(symbol)
    ak = _load_akshare()
    df = ak.stock_individual_fund_flow(stock=code, market=_market_prefix(code))
    records = _df_to_records(df, limit=limit)
    if not records:
        return _empty(tool, source, f"{code} 无资金流数据", params)

    latest = records[-1]
    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": code,
            "latest_date": latest.get("日期"),
            "main_net_inflow_cny": _pick_float(latest, ("主力净流入-净额", "主力净流入")),
            "main_net_inflow_pct": _pick_float(latest, ("主力净流入-净占比",)),
            "days": len(records), "records": records,
        },
    )


def get_sector_fund_flow(*, indicator: str = "今日", limit: int = 0) -> dict[str, Any]:
    """
    行业资金流排名。indicator 取 今日 / 5日 / 10日。

    版本兼容说明
    ------------
    AkShare `stock_sector_fund_flow_rank` 在不同版本中 `sector_type` 参数
    的可接受值不同：

    * 旧版本接受 `sector_type="行业资金流向"`
    * 较新版本该参数被移除或可接受值变更，传入 "行业资金流向" 时抛 KeyError

    本函数采用渐进试探策略（优先用旧值，失败后再试无参、再试枚举值），
    避免因版本差异引发整体不可用。

    返回列名也随版本变动（`名称` / `行业` / `板块`），调用方依赖 records
    中的 key 匹配（而非硬编码位置），已通过 `_df_to_records` 保证。
    """
    tool, source = "get_sector_fund_flow", f"{_PROVIDER}/stock_sector_fund_flow_rank"
    params = {"indicator": indicator, "limit": limit}

    allowed = ("今日", "5日", "10日")
    if indicator not in allowed:
        raise ValueError(f"indicator={indicator!r} 不支持，可选 {allowed}")

    ak = _load_akshare()

    # ── 版本兼容试探顺序 ──────────────────────────────────────────────────────
    # 1. 旧版：带 sector_type="行业资金流向"
    # 2. 中间版本：无 sector_type 参数
    # 3. 新版候选：sector_type="行业"
    # 每次失败记录原因，全部失败后汇报实际错误，不静默伪降级。
    _attempts: list[tuple[str, Exception]] = []

    def _try_call(fn_kwargs: dict[str, Any]) -> Any:
        return ak.stock_sector_fund_flow_rank(**fn_kwargs)

    df = None
    for fn_kwargs in (
        {"indicator": indicator, "sector_type": "行业资金流向"},
        {"indicator": indicator},
        {"indicator": indicator, "sector_type": "行业"},
    ):
        try:
            df = _try_call(fn_kwargs)
            break
        except (KeyError, TypeError, ValueError) as exc:
            _attempts.append((str(fn_kwargs), exc))
            logger.debug(
                "get_sector_fund_flow 尝试 %s 失败: %s", fn_kwargs, exc
            )
    else:
        # 全部候选都失败，上报真实错误
        detail = "; ".join(f"{k}→{type(e).__name__}:{e}" for k, e in _attempts)
        return err_envelope(
            tool=tool, source=source,
            error=f"stock_sector_fund_flow_rank 全部调用方式均失败: {detail}",
            error_type="UpstreamCompatibilityError",
            params=params,
        )

    # 记录实际调用参数，方便审计哪个版本生效
    if _attempts:
        logger.info(
            "get_sector_fund_flow 经 %d 次重试后成功（已跳过: %s）",
            len(_attempts), [k for k, _ in _attempts],
        )

    records = _df_to_records(df, limit=limit)
    if not records:
        return _empty(tool, source, f"{indicator} 行业资金流排名为空", params)

    # 标准化列名：不同版本返回的「行业名称」列不同（名称/行业/板块名称），
    # 统一映射成 "名称" 供上层一致消费。
    _name_col_aliases = ("名称", "行业", "板块名称", "sector")
    for rec in records:
        if "名称" not in rec:
            for alias in _name_col_aliases[1:]:
                if alias in rec:
                    rec["名称"] = rec[alias]
                    break

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={"indicator": indicator, "rows": len(records), "records": records},
    )


def get_northbound_flow(*, symbol: str = "北向资金", limit: int = 30) -> dict[str, Any]:
    """
    北向 / 沪深港通历史资金流。

    重要边界（不假装有实时数据）
    --------------------------
    交易所自 2024-08 起取消沪深港通逐日净买额披露，上游最新若干百行的
    「当日成交净买额」恒为 NaN。本工具因此：

    * `latest_net_inflow_cny` 仅在**最新一行确有数值**时给出，NaN → None；
    * 另给 `last_available_date` / `last_available_net_inflow_cny` /
      `staleness_days` / `is_fresh`，把「最后一次有数据是什么时候」显式暴露；
    * `is_fresh=False` 时调用方**不得**把该数值当作当日资金面使用。

    把 NaN 当 0、或把停止披露前的存量数据当当日净买额，都属于伪造行情，红线禁止。
    """
    tool = "get_northbound_flow"
    params = {"symbol": symbol, "limit": limit}

    name = str(symbol or "").strip() or "北向资金"
    if name not in _NORTHBOUND_SYMBOLS:
        raise ValueError(f"symbol={symbol!r} 不支持，可选 {_NORTHBOUND_SYMBOLS}")

    ak = _load_akshare()
    # 接口名随版本变动：命中的名字写进 source，审计时能看出实际调了哪个上游接口。
    fn, fn_name = _resolve_ak_fn(ak, _NORTHBOUND_FN_CANDIDATES)
    source = f"{_PROVIDER}/{fn_name}"

    df = fn(symbol=name)
    # 全量取回而非只取 limit 窗口：停止披露已持续数百个交易日，
    # 若只扫最近 limit 行会得到全 NaN，把「已知数据缺口」误判成「接口坏了」。
    records = _df_to_records(df)
    if not records:
        return _empty(tool, source, f"{name} 无历史资金流数据", params)

    def _net_yi(rec: dict[str, Any]) -> float | None:
        """取「当日成交净买额」（单位：亿元）。NaN 已由 _clean 转成 None。"""
        return _pick_float(rec, _NORTHBOUND_NET_COLS)

    def _date_of(rec: dict[str, Any]) -> str:
        return str(rec.get("日期") or rec.get("date") or "")

    latest = records[-1]
    latest_yi = _net_yi(latest)

    # 从尾部回溯，找最后一个净买额确有数值的交易日。
    last_avail: dict[str, Any] | None = None
    for rec in reversed(records):
        if _net_yi(rec) is not None:
            last_avail = rec
            break

    last_avail_yi = _net_yi(last_avail) if last_avail is not None else None
    last_avail_date = _date_of(last_avail) if last_avail is not None else None

    staleness_days: int | None = None
    parsed = _parse_iso_date(last_avail_date)
    if parsed is not None:
        staleness_days = (datetime.now(UTC).replace(tzinfo=None) - parsed).days

    is_fresh = staleness_days is not None and staleness_days <= _NORTHBOUND_STALE_DAYS

    if latest_yi is None:
        availability_note = (
            f"上游最新交易日（{_date_of(latest)}）的「当日成交净买额」为空值。"
            f"交易所自 2024-08 起取消沪深港通逐日净买额披露，这是已知的上游数据缺口，"
            f"不是网络故障；最后一次有数值的交易日为 {last_avail_date or '未找到'}"
            + (f"，距今 {staleness_days} 个自然日" if staleness_days is not None else "")
            + "。该存量数值不得当作当日资金面使用。"
        )
    else:
        availability_note = f"上游最新交易日（{_date_of(latest)}）提供了当日成交净买额。"

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": name,
            "upstream_fn": fn_name,
            "latest_date": _date_of(latest),
            # 当日净买额：NaN 一律为 None，绝不填 0
            "latest_net_inflow_cny": (
                latest_yi * _YI_TO_CNY if latest_yi is not None else None
            ),
            "latest_net_inflow_yi": latest_yi,
            "net_inflow_available": latest_yi is not None,
            # 最后一次有数值的交易日（可能远早于今天，须配合 staleness_days 判断）
            "last_available_date": last_avail_date,
            "last_available_net_inflow_cny": (
                last_avail_yi * _YI_TO_CNY if last_avail_yi is not None else None
            ),
            "staleness_days": staleness_days,
            "is_fresh": is_fresh,
            "stale_threshold_days": _NORTHBOUND_STALE_DAYS,
            "rows": len(records),
            "records": records[-limit:] if limit and limit > 0 else records,
            "unit_note": "上游「当日成交净买额」单位为亿元，*_cny 字段已 ×1e8 转为元",
            "availability_note": availability_note,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 工具实现：指数与行业
# ─────────────────────────────────────────────────────────────────────────────

def get_index_daily(
    *, index_code: str = "sh000001", start: str = "", end: str = "", limit: int = 0,
) -> dict[str, Any]:
    """
    指数日线。index_code 形如 sh000001 / sz399001。

    边界：指数无复权概念，本接口不接受 adjust 参数。
    """
    tool, source = "get_index_daily", f"{_PROVIDER}/stock_zh_index_daily"
    params = {"index_code": index_code, "start": start, "end": end}

    code = str(index_code or "").strip().lower()
    if len(code) != 8 or code[:2] not in ("sh", "sz", "bj") or not code[2:].isdigit():
        raise ValueError(f"指数代码非法: {index_code!r}，应形如 sh000001")

    ak = _load_akshare()
    df = ak.stock_zh_index_daily(symbol=code)
    records = _df_to_records(df)
    if not records:
        return _empty(tool, source, f"指数 {code} 无日线数据", params)

    # 上游不支持日期区间入参，只能取回后在本地筛选
    start_date = _norm_date(start, default="") if start else ""
    end_date = _norm_date(end, default="") if end else ""
    if start_date or end_date:
        def _in_range(rec: dict[str, Any]) -> bool:
            raw = str(rec.get("date") or rec.get("日期") or "")
            key = raw.replace("-", "")[:8]
            if start_date and key < start_date:
                return False
            return not (end_date and key > end_date)

        records = [r for r in records if _in_range(r)]
        if not records:
            return _empty(tool, source, f"指数 {code} 在 {start_date}~{end_date} 无数据", params)

    if limit and limit > 0:
        records = records[-limit:]

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={"index_code": code, "rows": len(records), "records": records},
    )


def get_industry_list(*, limit: int = 0) -> dict[str, Any]:
    """东财行业板块列表。"""
    tool, source = "get_industry_list", f"{_PROVIDER}/stock_board_industry_name_em"
    params = {"limit": limit}

    ak = _load_akshare()
    df = ak.stock_board_industry_name_em()
    records = _df_to_records(df, limit=limit)
    if not records:
        return _empty(tool, source, "行业板块列表为空", params)

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={"count": len(records), "records": records},
    )


def get_industry_spot(*, sector: str) -> dict[str, Any]:
    """行业板块实时快照（涨跌幅、成交额、领涨股等）。"""
    tool, source = "get_industry_spot", f"{_PROVIDER}/stock_sector_spot_em"
    params = {"sector": sector}

    name = str(sector or "").strip()
    if not name:
        raise ValueError("sector 不能为空")

    ak = _load_akshare()
    df = ak.stock_sector_spot_em(sector=name)
    records = _df_to_records(df)
    if not records:
        return _empty(tool, source, f"行业 {name} 无快照数据", params)

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={"sector": name, "rows": len(records), "records": records},
    )


def get_stock_industry(*, symbol: str) -> dict[str, Any]:
    """个股所属行业与基础信息（长表 item/value 结构，已转成扁平字典）。"""
    tool, source = "get_stock_industry", f"{_PROVIDER}/stock_individual_info_em"
    params = {"symbol": symbol}

    code = _validate_symbol(symbol)
    ak = _load_akshare()
    try:
        df = ak.stock_individual_info_em(symbol=code)
        records = _df_to_records(df)
        info = {
            str(r.get("item")): r.get("value")
            for r in records
            if r.get("item") is not None
        }
        industry = info.get("行业")
        if not records or not industry:
            raise LookupError(f"东方财富未返回 {code} 的行业信息")
        name = info.get("股票简称")
        total_market_value = _pick_float(info, ("总市值",))
        listing_date = info.get("上市时间")
    except Exception as primary_exc:
        logger.warning("东方财富行业信息失败，切换巨潮资讯数据源: %s", primary_exc)
        profile = ak.stock_profile_cninfo(symbol=code)
        records = _df_to_records(profile)
        if not records:
            return _empty(
                tool, f"{_PROVIDER}/stock_profile_cninfo", f"{code} 无公司概况", params,
            )
        info = records[0]
        industry = info.get("所属行业")
        if not industry:
            return err_envelope(
                tool=tool, source=f"{_PROVIDER}/stock_profile_cninfo",
                error=f"{code} 的巨潮公司概况中缺少「所属行业」字段",
                error_type="MissingField", params=params,
            )
        source = f"{_PROVIDER}/stock_profile_cninfo"
        name = info.get("A股简称") or info.get("公司名称")
        total_market_value = None
        listing_date = info.get("上市日期")

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": code,
            "name": name,
            "industry": industry,
            "total_market_value_cny": total_market_value,
            "listing_date": listing_date,
            "info": info,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 注册
# ─────────────────────────────────────────────────────────────────────────────

def register_all(reg: MCPToolRegistry) -> None:
    """把本模块所有工具注册进注册表。全部 requires_network=True。"""
    reg.register(
        "get_price_history", get_price_history,
        description="日/周/月线历史行情，支持前后复权",
        requires_network=True, provider=_PROVIDER, category="history",
    )
    reg.register(
        "get_minute_bars", get_minute_bars,
        description="1/5/15/30/60 分钟线（仅近期交易日）",
        requires_network=True, provider=_PROVIDER, category="history",
    )
    reg.register(
        "get_realtime_quote", get_realtime_quote,
        description="个股实时行情快照",
        requires_network=True, provider=_PROVIDER, category="quote",
    )
    reg.register(
        "get_valuation_metrics", get_valuation_metrics,
        description="PE_TTM/PB/总市值/换手率（不含 PS、ROE、资产负债率）",
        requires_network=True, provider=_PROVIDER, category="valuation",
    )
    reg.register(
        "get_financial_indicator", get_financial_indicator,
        description="ROE、资产负债率、毛利率等财务指标（季报级）",
        requires_network=True, provider=_PROVIDER, category="valuation",
    )
    reg.register(
        "get_financial_statement", get_financial_statement,
        description="资产负债表/利润表/现金流量表",
        requires_network=True, provider=_PROVIDER, category="financials",
    )
    reg.register(
        "get_fund_flow", get_fund_flow,
        description="个股主力资金净流入",
        requires_network=True, provider=_PROVIDER, category="fundflow",
    )
    reg.register(
        "get_sector_fund_flow", get_sector_fund_flow,
        description="行业资金流排名（今日/5日/10日）",
        requires_network=True, provider=_PROVIDER, category="fundflow",
    )
    reg.register(
        "get_northbound_flow", get_northbound_flow,
        description="北向/沪深港通历史资金流（逐日净买额已于 2024-08 停止披露，带新鲜度标记）",
        requires_network=True, provider=_PROVIDER, category="fundflow",
    )
    reg.register(
        "get_index_daily", get_index_daily,
        description="指数日线行情",
        requires_network=True, provider=_PROVIDER, category="index",
    )
    reg.register(
        "get_industry_list", get_industry_list,
        description="行业板块列表",
        requires_network=True, provider=_PROVIDER, category="industry",
    )
    reg.register(
        "get_industry_spot", get_industry_spot,
        description="行业板块实时快照",
        requires_network=True, provider=_PROVIDER, category="industry",
    )
    reg.register(
        "get_stock_industry", get_stock_industry,
        description="个股所属行业与基础信息",
        requires_network=True, provider=_PROVIDER, category="industry",
    )
