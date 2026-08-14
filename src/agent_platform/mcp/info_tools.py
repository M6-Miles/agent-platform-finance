"""
MCP 信息工具：财经新闻 / 公司公告 / 研报摘要 / 政策宏观 / 利率
==============================================================
覆盖说明书「新闻、公告、研报摘要、政策和利率」要求。

能力边界（诚实声明）
--------------------
* **财经新闻**：AkShare `stock_news_em` 可获取东财 A 股新闻；
  不存在时返回 unavailable 信封，绝不生成假新闻。
* **公司公告**：AkShare `stock_notice_report` / `stock_gsrl_em`
  可获取部分公告列表；字段随版本变动，做多候选匹配。
* **研报摘要**：AkShare `stock_research_report_em` 提供研报列表；
  无真实摘要正文时明确标 unavailable，不编造内容。
* **政策/宏观**：AkShare 提供央行公告 (`stock_notice_cninfo`)、
  货币供应量 (`macro_china_money_supply`)，不存在时 unavailable。
* **利率**：AkShare `macro_china_lpr` 提供 LPR 历史；
  `macro_china_shibor` 提供 SHIBOR；不存在时 unavailable。

禁止行为
--------
* 不得生成假新闻、假公告、假政策、假研报内容。
* 失败时返回规范 unavailable 信封（含 source/updated_at/error_type）。
* 离线模式在 Registry 层由 requires_network=True 硬阻断，函数体不执行。
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from agent_platform.mcp.envelope import err_envelope, ok_envelope
from agent_platform.mcp.registry import MCPToolRegistry

logger = logging.getLogger(__name__)

_PROVIDER = "akshare"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_akshare() -> Any:
    import akshare as ak  # noqa: PLC0415
    return ak


def _clean_val(v: Any) -> Any:
    """简单的 NaN/NaT → None，类型安全。"""
    if v is None:
        return None
    if hasattr(v, "item"):
        try:
            v = v.item()
        except Exception:  # noqa: BLE001
            return str(v)
    if isinstance(v, float) and v != v:
        return None
    if hasattr(v, "isoformat"):
        try:
            t = v.isoformat()
        except Exception:  # noqa: BLE001
            return str(v)
        return None if t in ("NaT", "nat") else t
    return v


def _df_rows(df: Any, limit: int = 0) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    frame = df.tail(limit) if limit and limit > 0 else df
    return [{str(k): _clean_val(v) for k, v in rec.items()}
            for rec in frame.to_dict(orient="records")]


def _unavailable(tool: str, source: str, reason: str, params: dict[str, Any]) -> dict[str, Any]:
    env = err_envelope(
        tool=tool, source=source,
        error=reason, error_type="Unavailable", params=params,
    )
    env["data_status"] = "unavailable"
    env["fallback_reason"] = reason
    return env


# ─────────────────────────────────────────────────────────────────────────────
# 1. 财经新闻
# ─────────────────────────────────────────────────────────────────────────────

def get_financial_news(*, symbol: str = "", limit: int = 20) -> dict[str, Any]:
    """
    A 股财经新闻（东方财富）。

    symbol 非空时获取个股新闻（stock_news_em）；为空时获取市场综合新闻
    （stock_telegraph_cls / 财联社电报，fallback 到个股空 symbol）。
    绝不生成假新闻 —— 上游无数据时返回 unavailable。
    """
    tool = "get_financial_news"
    params = {"symbol": symbol, "limit": limit}
    source = f"{_PROVIDER}/stock_news_em"

    ak = _load_akshare()
    try:
        if symbol:
            from agent_platform.mcp.akshare_tools import _validate_symbol
            code = _validate_symbol(symbol)
            df = ak.stock_news_em(symbol=code)
        else:
            # 综合市场快讯：尝试财联社电报，失败则跳过
            try:
                df = ak.stock_telegraph_cls(symbol="全部")
                source = f"{_PROVIDER}/stock_telegraph_cls"
            except Exception:  # noqa: BLE001
                return _unavailable(
                    tool, source,
                    "综合市场新闻接口不可用（stock_telegraph_cls 不支持当前版本）",
                    params,
                )
        records = _df_rows(df, limit=limit)
        if not records:
            return _unavailable(tool, source, "上游返回空新闻列表", params)
    except Exception as exc:  # noqa: BLE001
        env = err_envelope(
            tool=tool, source=source,
            error=f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__, params=params,
        )
        env["data_status"] = "error"
        env["fallback_reason"] = f"{type(exc).__name__}: {exc}"
        return env

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={"symbol": symbol or "market", "rows": len(records), "records": records},
    )



# ─────────────────────────────────────────────────────────────────────────────
# 2. 公司公告
# ─────────────────────────────────────────────────────────────────────────────

# 公告接口候选（按「更稳定/数据更全」优先排列）
_NOTICE_FN_CANDIDATES = (
    "stock_notice_report",      # 巨潮，稳定
    "stock_gsrl_em",            # 东财公告，字段略不同
)


def get_stock_announcements(*, symbol: str, limit: int = 10) -> dict[str, Any]:
    """
    个股公司公告列表（巨潮/东财）。

    返回公告标题、日期、类型；**不返回正文**（正文需访问 PDF，非免费接口）。
    上游不可用时返回 unavailable，不生成假公告。
    """
    tool = "get_stock_announcements"
    params = {"symbol": symbol, "limit": limit}

    from agent_platform.mcp.akshare_tools import _validate_symbol
    code = _validate_symbol(symbol)
    ak = _load_akshare()

    for fn_name in _NOTICE_FN_CANDIDATES:
        fn = getattr(ak, fn_name, None)
        if not callable(fn):
            continue
        try:
            df = fn(symbol=code)
            records = _df_rows(df, limit=limit)
            if records:
                source = f"{_PROVIDER}/{fn_name}"
                return ok_envelope(
                    tool=tool, source=source, params=params,
                    data={"symbol": code, "rows": len(records), "records": records},
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_stock_announcements %s 失败: %s", fn_name, exc)
            continue

    return _unavailable(
        tool, f"{_PROVIDER}/stock_notice_report",
        f"{code} 公告接口全部不可用（{_NOTICE_FN_CANDIDATES}）", params,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. 研报摘要
# ─────────────────────────────────────────────────────────────────────────────

def get_research_report_summary(*, symbol: str, limit: int = 5) -> dict[str, Any]:
    """
    研报列表（机构/评级/目标价）。

    只返回研报元数据（标题、机构、评级、目标价），**不返回正文**。
    正文属于付费内容；本工具仅使用 AkShare 免费列表接口。
    若列表本身也不可用，返回 unavailable，不编造研报内容。
    """
    tool = "get_research_report_summary"
    params = {"symbol": symbol, "limit": limit}
    source = f"{_PROVIDER}/stock_research_report_em"

    from agent_platform.mcp.akshare_tools import _validate_symbol
    code = _validate_symbol(symbol)
    ak = _load_akshare()

    fn = getattr(ak, "stock_research_report_em", None)
    if not callable(fn):
        return _unavailable(
            tool, source,
            "stock_research_report_em 在当前 AkShare 版本不存在", params,
        )

    try:
        df = fn(symbol=code)
        records = _df_rows(df, limit=limit)
        if not records:
            return _unavailable(tool, source, f"{code} 无研报列表数据", params)
    except Exception as exc:  # noqa: BLE001
        env = err_envelope(
            tool=tool, source=source,
            error=f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__, params=params,
        )
        env["data_status"] = "error"
        env["fallback_reason"] = f"{type(exc).__name__}: {exc}"
        return env

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={
            "symbol": code, "rows": len(records), "records": records,
            "content_note": "仅提供研报元数据（标题/机构/评级/目标价），正文不可通过免费接口获取",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# 4. 政策 / 宏观
# ─────────────────────────────────────────────────────────────────────────────

def get_macro_policy(*, indicator: str = "money_supply", limit: int = 12) -> dict[str, Any]:
    """
    宏观政策 / 经济指标。

    indicator 可选：
      money_supply   → 货币供应量（M0/M1/M2）
      cpi            → CPI 同比
      ppi            → PPI 同比
      gdp            → GDP 季度数据
    上游不可用时返回 unavailable，绝不生成假政策数据。
    """
    tool = "get_macro_policy"
    params = {"indicator": indicator, "limit": limit}

    _fn_map = {
        "money_supply": ("macro_china_money_supply", f"{_PROVIDER}/macro_china_money_supply"),
        "cpi":          ("macro_china_cpi_yearly",   f"{_PROVIDER}/macro_china_cpi_yearly"),
        "ppi":          ("macro_china_ppi_yearly",   f"{_PROVIDER}/macro_china_ppi_yearly"),
        "gdp":          ("macro_china_gdp_yearly",   f"{_PROVIDER}/macro_china_gdp_yearly"),
    }

    if indicator not in _fn_map:
        env = err_envelope(
            tool=tool, source=f"{_PROVIDER}/macro",
            error=f"indicator={indicator!r} 不支持，可选 {sorted(_fn_map)}",
            error_type="ValueError", params=params,
        )
        env["data_status"] = "unavailable"
        env["fallback_reason"] = f"indicator={indicator!r} 不支持"
        return env

    fn_name, source = _fn_map[indicator]
    ak = _load_akshare()
    fn = getattr(ak, fn_name, None)
    if not callable(fn):
        return _unavailable(
            tool, source,
            f"{fn_name} 在当前 AkShare 版本不存在", params,
        )

    try:
        df = fn()
        records = _df_rows(df, limit=limit)
        if not records:
            return _unavailable(tool, source, f"{indicator} 宏观数据为空", params)
    except Exception as exc:  # noqa: BLE001
        env = err_envelope(
            tool=tool, source=source,
            error=f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__, params=params,
        )
        env["data_status"] = "error"
        env["fallback_reason"] = f"{type(exc).__name__}: {exc}"
        return env

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={"indicator": indicator, "rows": len(records), "records": records},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. 利率
# ─────────────────────────────────────────────────────────────────────────────

def get_interest_rates(*, rate_type: str = "lpr", limit: int = 24) -> dict[str, Any]:
    """
    利率数据。

    rate_type 可选：
      lpr     → 贷款市场报价利率（LPR），1年期 / 5年期
      shibor  → 上海同业拆放利率（SHIBOR）
    上游不可用时返回 unavailable，绝不伪造利率数据。
    """
    tool = "get_interest_rates"
    params = {"rate_type": rate_type, "limit": limit}

    _fn_map = {
        "lpr":    ("macro_china_lpr",    f"{_PROVIDER}/macro_china_lpr"),
        "shibor": ("macro_china_shibor", f"{_PROVIDER}/macro_china_shibor"),
    }

    if rate_type not in _fn_map:
        env = err_envelope(
            tool=tool, source=f"{_PROVIDER}/macro",
            error=f"rate_type={rate_type!r} 不支持，可选 {sorted(_fn_map)}",
            error_type="ValueError", params=params,
        )
        env["data_status"] = "unavailable"
        env["fallback_reason"] = f"rate_type={rate_type!r} 不支持"
        return env

    fn_name, source = _fn_map[rate_type]
    ak = _load_akshare()
    fn = getattr(ak, fn_name, None)
    if not callable(fn):
        return _unavailable(
            tool, source,
            f"{fn_name} 在当前 AkShare 版本不存在", params,
        )

    try:
        df = fn()
        records = _df_rows(df, limit=limit)
        if not records:
            return _unavailable(tool, source, f"{rate_type} 利率数据为空", params)
    except Exception as exc:  # noqa: BLE001
        env = err_envelope(
            tool=tool, source=source,
            error=f"{type(exc).__name__}: {exc}",
            error_type=type(exc).__name__, params=params,
        )
        env["data_status"] = "error"
        env["fallback_reason"] = f"{type(exc).__name__}: {exc}"
        return env

    return ok_envelope(
        tool=tool, source=source, params=params,
        data={"rate_type": rate_type, "rows": len(records), "records": records},
    )


# ─────────────────────────────────────────────────────────────────────────────
# 注册
# ─────────────────────────────────────────────────────────────────────────────

def register_all(reg: MCPToolRegistry) -> None:
    """把信息工具注册进注册表。全部 requires_network=True，离线模式被硬阻断。"""
    reg.register(
        "get_financial_news", get_financial_news,
        description="财经新闻（东财/财联社），个股或市场综合，绝不生成假新闻",
        requires_network=True, provider=_PROVIDER, category="news",
    )
    reg.register(
        "get_stock_announcements", get_stock_announcements,
        description="公司公告列表（巨潮/东财），仅元数据，不含正文",
        requires_network=True, provider=_PROVIDER, category="announcements",
    )
    reg.register(
        "get_research_report_summary", get_research_report_summary,
        description="研报列表（机构/评级/目标价），仅元数据，不含正文",
        requires_network=True, provider=_PROVIDER, category="research",
    )
    reg.register(
        "get_macro_policy", get_macro_policy,
        description="宏观政策/经济指标（货币供应量/CPI/PPI/GDP）",
        requires_network=True, provider=_PROVIDER, category="macro",
    )
    reg.register(
        "get_interest_rates", get_interest_rates,
        description="利率数据（LPR / SHIBOR），不伪造利率",
        requires_network=True, provider=_PROVIDER, category="macro",
    )
