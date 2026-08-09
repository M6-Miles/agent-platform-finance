"""
Tushare MCP 工具
================
补齐 AkShare 缺失的能力，主要是 **PS（市销率）** 和结构化的三大报表。

覆盖范围
--------
financials: get_income_statement / get_balance_sheet / get_cash_flow
valuation : get_daily_basic（PE / PE_TTM / PB / PS / PS_TTM / 总市值 / 换手率 / 股息率）
            get_fina_indicator（ROE / ROE 扣非 / 资产负债率 / 毛利率 / 净利率）
index     : get_index_daily_ts（指数日线，Tushare 口径）

凭证处理
--------
Token 从环境变量 `TUSHARE_TOKEN` 读取。**未配置时直接返回失败信封**
（error_type=MissingCredentialError），不会退化成假数据，也不会把 token
写进返回体或日志 —— 信封的 params 由 envelope 层统一脱敏。

明确的能力边界
--------------
* Tushare 免费额度对 `income` / `balancesheet` / `cashflow` / `fina_indicator`
  有积分门槛与频次限制。触发限流时上游抛异常，本层原样转成失败信封，
  由调用方决定降级，**不重试、不静默返回空值**。
* Tushare 无分钟线免费接口，分钟线请用 AkShare `get_minute_bars`。
* 本模块不做本地缓存。缓存会让"实时性"变得不可验证，与溯源要求冲突。
"""
from __future__ import annotations

import logging
import os
from typing import Any

from agent_platform.mcp.envelope import ok_envelope
from agent_platform.mcp.registry import MCPToolRegistry

logger = logging.getLogger(__name__)

_PROVIDER = "tushare"

_INCOME_FIELDS = (
    "ts_code,end_date,report_type,total_revenue,revenue,operate_profit,"
    "total_profit,n_income,n_income_attr_p,basic_eps"
)
_BALANCE_FIELDS = (
    "ts_code,end_date,report_type,total_assets,total_liab,total_hldr_eqy_exc_min_int,"
    "total_cur_assets,total_cur_liab,money_cap,lt_borr,st_borr"
)
_CASHFLOW_FIELDS = (
    "ts_code,end_date,report_type,n_cashflow_act,n_cashflow_inv_act,"
    "n_cash_flows_fnc_act,c_pay_acq_const_fiolta,free_cashflow"
)
_INDICATOR_FIELDS = (
    "ts_code,end_date,roe,roe_dt,roe_waa,debt_to_assets,"
    "grossprofit_margin,netprofit_margin,current_ratio,or_yoy,netprofit_yoy"
)


class MissingCredentialError(RuntimeError):
    """未配置 TUSHARE_TOKEN。属于环境问题，必须显式失败而非伪造数据。"""


# ─────────────────────────────────────────────────────────────────────────────
# 内部工具函数
# ─────────────────────────────────────────────────────────────────────────────

_PRO_CACHE: dict[str, Any] = {}


def _get_pro() -> Any:
    """
    获取 Tushare pro_api 客户端。

    客户端对象按 token 指纹缓存（只缓存客户端，不缓存数据）。
    token 本身不入日志、不入返回体。
    """
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        raise MissingCredentialError(
            "未配置环境变量 TUSHARE_TOKEN，Tushare 工具不可用。"
            "本工具不会用样例或随机值代替真实财报数据。"
        )
    fingerprint = f"len{len(token)}:{token[:2]}"   # 仅用于缓存键，不足以还原 token
    if fingerprint not in _PRO_CACHE:
        import tushare as ts  # noqa: PLC0415 — 延迟导入，未安装时转失败信封
        ts.set_token(token)
        _PRO_CACHE[fingerprint] = ts.pro_api()
    return _PRO_CACHE[fingerprint]


def _to_ts_code(symbol: Any) -> str:
    """
    代码归一化为 Tushare 格式：600519 → 600519.SH。
    已是 ts_code 形式则原样校验返回。
    """
    text = str(symbol or "").strip().upper()
    if not text:
        raise ValueError("证券代码不能为空")
    if "." in text:
        code, _, market = text.partition(".")
        if len(code) != 6 or not code.isdigit() or market not in ("SH", "SZ", "BJ"):
            raise ValueError(f"ts_code 非法: {symbol!r}，应形如 600519.SH")
        return text
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"证券代码非法: {symbol!r}，A 股应为 6 位数字")
    if text[0] in ("6", "9"):
        return f"{text}.SH"
    if text[0] in ("0", "2", "3"):
        return f"{text}.SZ"
    if text[0] in ("4", "8"):
        return f"{text}.BJ"
    raise ValueError(f"无法判断 {text} 所属交易所")


def _norm_period(value: Any) -> str:
    """报告期归一化为 YYYYMMDD；空字符串表示不限定期数。"""
    if value in (None, ""):
        return ""
    text = str(value).replace("-", "").strip()
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"报告期非法: {value!r}，应为 YYYYMMDD（如 20231231）")
    return text


def _clean(value: Any) -> Any:
    """与 akshare_tools 一致的标量清洗：NaN/NaT → None。"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        try:
            text = value.isoformat()
        except Exception:  # noqa: BLE001
            return str(value)
        return None if text in ("NaT", "nat") else text
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _df_to_records(df: Any, *, limit: int = 0) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    frame = df.head(limit) if limit and limit > 0 else df   # Tushare 默认按期数倒序
    return [
        {str(k): _clean(v) for k, v in record.items()}
        for record in frame.to_dict(orient="records")
    ]


def _empty_error(detail: str) -> None:
    """空结果统一抛 LookupError，由注册表转成失败信封。"""
    raise LookupError(f"Tushare 返回空数据集：{detail}")


def _statement(
    *, tool: str, api: str, fields: str, symbol: str, period: str, limit: int,
) -> dict[str, Any]:
    """三大报表共用的取数流程（三个接口签名一致，仅 api 名与字段不同）。"""
    source = f"{_PROVIDER}/{api}"
    params = {"symbol": symbol, "period": period, "limit": limit}

    ts_code = _to_ts_code(symbol)
    period_text = _norm_period(period)

    pro = _get_pro()
    kwargs: dict[str, Any] = {"ts_code": ts_code, "fields": fields}
    if period_text:
        kwargs["period"] = period_text

    df = getattr(pro, api)(**kwargs)
    records = _df_to_records(df, limit=limit)
    if not records:
        _empty_error(f"{ts_code} {api} 期数 {period_text or '不限'} 无数据")

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "ts_code": ts_code,
            "period": period_text or "latest",
            "periods": len(records),
            "records": records,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 工具实现：三大报表
# ─────────────────────────────────────────────────────────────────────────────

def get_income_statement(*, symbol: str, period: str = "", limit: int = 4) -> dict[str, Any]:
    """利润表。"""
    return _statement(
        tool="get_income_statement", api="income", fields=_INCOME_FIELDS,
        symbol=symbol, period=period, limit=limit,
    )


def get_balance_sheet(*, symbol: str, period: str = "", limit: int = 4) -> dict[str, Any]:
    """资产负债表。附带按报表口径直接计算的资产负债率（不是估算）。"""
    env = _statement(
        tool="get_balance_sheet", api="balancesheet", fields=_BALANCE_FIELDS,
        symbol=symbol, period=period, limit=limit,
    )
    latest = env["data"]["records"][0]
    assets = latest.get("total_assets")
    liab = latest.get("total_liab")
    if isinstance(assets, (int, float)) and isinstance(liab, (int, float)) and assets > 0:
        env["data"]["debt_to_asset_pct"] = round(liab / assets * 100.0, 4)
        env["data"]["debt_to_asset_basis"] = "total_liab / total_assets × 100（报表口径）"
    else:
        env["data"]["debt_to_asset_pct"] = None
        env["data"]["debt_to_asset_basis"] = "总资产或总负债缺失，无法计算"
    return env


def get_cash_flow(*, symbol: str, period: str = "", limit: int = 4) -> dict[str, Any]:
    """现金流量表。"""
    return _statement(
        tool="get_cash_flow", api="cashflow", fields=_CASHFLOW_FIELDS,
        symbol=symbol, period=period, limit=limit,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 工具实现：估值与财务指标
# ─────────────────────────────────────────────────────────────────────────────

def get_daily_basic(*, symbol: str, trade_date: str = "") -> dict[str, Any]:
    """
    每日指标：PE / PE_TTM / PB / **PS / PS_TTM** / 总市值 / 换手率 / 股息率。

    这是全项目 PS 的唯一真实来源（AkShare 现货快照不含 PS）。
    Tushare 的市值单位是万元，这里换算成元并注明，避免上层量纲错配。
    """
    tool, source = "get_daily_basic", f"{_PROVIDER}/daily_basic"
    params = {"symbol": symbol, "trade_date": trade_date}

    ts_code = _to_ts_code(symbol)
    date_text = _norm_period(trade_date)

    pro = _get_pro()
    kwargs: dict[str, Any] = {
        "ts_code": ts_code,
        "fields": (
            "ts_code,trade_date,close,pe,pe_ttm,pb,ps,ps_ttm,"
            "dv_ratio,dv_ttm,total_mv,circ_mv,turnover_rate,total_share"
        ),
    }
    if date_text:
        kwargs["trade_date"] = date_text

    df = pro.daily_basic(**kwargs)
    records = _df_to_records(df, limit=1)
    if not records:
        _empty_error(f"{ts_code} 在 {date_text or '最近交易日'} 无每日指标")

    latest = records[0]
    total_mv_wan = latest.get("total_mv")
    circ_mv_wan = latest.get("circ_mv")

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "ts_code": ts_code,
            "trade_date": latest.get("trade_date"),
            "close": latest.get("close"),
            "pe": latest.get("pe"),
            "pe_ttm": latest.get("pe_ttm"),
            "pb": latest.get("pb"),
            "ps": latest.get("ps"),
            "ps_ttm": latest.get("ps_ttm"),
            "dividend_yield_pct": latest.get("dv_ratio"),
            "turnover_rate": latest.get("turnover_rate"),
            "total_market_value_cny": (
                total_mv_wan * 1e4 if isinstance(total_mv_wan, (int, float)) else None
            ),
            "circulating_market_value_cny": (
                circ_mv_wan * 1e4 if isinstance(circ_mv_wan, (int, float)) else None
            ),
            "unit_note": "Tushare total_mv/circ_mv 单位为万元，此处已×1e4 换算为元",
        },
    )


def get_fina_indicator(*, symbol: str, period: str = "", limit: int = 4) -> dict[str, Any]:
    """财务指标：ROE、扣非 ROE、资产负债率、毛利率、净利率、同比增速。"""
    tool, source = "get_fina_indicator", f"{_PROVIDER}/fina_indicator"
    params = {"symbol": symbol, "period": period, "limit": limit}

    ts_code = _to_ts_code(symbol)
    period_text = _norm_period(period)

    pro = _get_pro()
    kwargs: dict[str, Any] = {"ts_code": ts_code, "fields": _INDICATOR_FIELDS}
    if period_text:
        kwargs["period"] = period_text

    df = pro.fina_indicator(**kwargs)
    records = _df_to_records(df, limit=limit)
    if not records:
        _empty_error(f"{ts_code} 期数 {period_text or '不限'} 无财务指标")

    latest = records[0]
    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "ts_code": ts_code,
            "end_date": latest.get("end_date"),
            "roe_pct": latest.get("roe"),
            "roe_deducted_pct": latest.get("roe_dt"),
            "debt_to_asset_pct": latest.get("debt_to_assets"),
            "gross_margin_pct": latest.get("grossprofit_margin"),
            "net_margin_pct": latest.get("netprofit_margin"),
            "revenue_yoy_pct": latest.get("or_yoy"),
            "net_profit_yoy_pct": latest.get("netprofit_yoy"),
            "periods": len(records),
            "records": records,
        },
    )


def get_index_daily_ts(
    *, index_code: str = "000001.SH", start_date: str = "", end_date: str = "", limit: int = 0,
) -> dict[str, Any]:
    """指数日线（Tushare 口径）。index_code 形如 000001.SH。"""
    tool, source = "get_index_daily_ts", f"{_PROVIDER}/index_daily"
    params = {"index_code": index_code, "start_date": start_date, "end_date": end_date}

    ts_code = _to_ts_code(index_code)
    start_text = _norm_period(start_date)
    end_text = _norm_period(end_date)
    if start_text and end_text and start_text > end_text:
        raise ValueError(f"起始日期 {start_text} 晚于结束日期 {end_text}")

    pro = _get_pro()
    kwargs: dict[str, Any] = {"ts_code": ts_code}
    if start_text:
        kwargs["start_date"] = start_text
    if end_text:
        kwargs["end_date"] = end_text

    df = pro.index_daily(**kwargs)
    records = _df_to_records(df, limit=limit)
    if not records:
        _empty_error(f"指数 {ts_code} 在 {start_text or '不限'}~{end_text or '不限'} 无日线")

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={"ts_code": ts_code, "rows": len(records), "records": records},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 注册
# ─────────────────────────────────────────────────────────────────────────────

def register_all(reg: MCPToolRegistry) -> None:
    """注册 Tushare 工具。工具名加 _ts 后缀避免与 AkShare 同名工具冲突。"""
    reg.register(
        "get_income_statement", get_income_statement,
        description="利润表（营收、营业利润、净利润、EPS）",
        requires_network=True, provider=_PROVIDER, category="financials",
    )
    reg.register(
        "get_balance_sheet", get_balance_sheet,
        description="资产负债表，附报表口径资产负债率",
        requires_network=True, provider=_PROVIDER, category="financials",
    )
    reg.register(
        "get_cash_flow", get_cash_flow,
        description="现金流量表（经营/投资/筹资净现金流）",
        requires_network=True, provider=_PROVIDER, category="financials",
    )
    reg.register(
        "get_daily_basic", get_daily_basic,
        description="每日指标 PE/PE_TTM/PB/PS/PS_TTM/市值/换手率/股息率",
        requires_network=True, provider=_PROVIDER, category="valuation",
    )
    reg.register(
        "get_fina_indicator", get_fina_indicator,
        description="ROE/扣非ROE/资产负债率/毛利率/净利率/同比增速",
        requires_network=True, provider=_PROVIDER, category="valuation",
    )
    reg.register(
        "get_index_daily_ts", get_index_daily_ts,
        description="指数日线（Tushare 口径）",
        requires_network=True, provider=_PROVIDER, category="index",
    )
