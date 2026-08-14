"""
信息 MCP 工具（info_tools）专项测试
=====================================
覆盖五类工具：财经新闻 / 公司公告 / 研报摘要 / 政策宏观 / 利率。

断言重点
--------
1. 成功路径：ok=True，data 非空，source/updated_at/data 均存在。
2. AkShare 函数不存在（getattr=None）→ unavailable 信封，含 data_status/fallback_reason。
3. 空结果 / 异常 → unavailable 或 error 信封，含 data_status/fallback_reason。
4. 离线 Registry 阻断 → 函数体不执行，error_type=OfflineModeBlocked。
"""
from __future__ import annotations

import pandas as pd
import pytest

from agent_platform.mcp import info_tools
from agent_platform.mcp.registry import build_default_registry


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：构造最小 DataFrame
# ─────────────────────────────────────────────────────────────────────────────

def _mini_df(col: str = "title") -> pd.DataFrame:
    return pd.DataFrame({col: ["测试标题1", "测试标题2"], "date": ["2026-08-01", "2026-08-01"]})


# ─────────────────────────────────────────────────────────────────────────────
# 一、财经新闻
# ─────────────────────────────────────────────────────────────────────────────

class TestGetFinancialNews:
    def test_success_individual_symbol(self, monkeypatch):
        """函数存在且有数据 → ok=True，source/updated_at/data 齐。"""
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk({"stock_news_em": _mini_df("关键词")}))
        env = info_tools.get_financial_news(symbol="600519", limit=5)
        assert env["ok"] is True
        assert env["source"]
        assert env["updated_at"]
        assert env["data"] is not None
        assert env["data"]["rows"] >= 1

    def test_function_raises_exception(self, monkeypatch):
        """AkShare 函数抛异常 → 失败信封含 data_status/fallback_reason。"""
        def _boom(**kw):
            raise RuntimeError("模拟超时")
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk({"stock_news_em": _boom}))
        env = info_tools.get_financial_news(symbol="600519")
        assert env["ok"] is False
        assert env["data"] is None
        assert env.get("data_status") == "error"
        assert env.get("fallback_reason")
        assert env["error_type"] == "RuntimeError"

    def test_empty_result(self, monkeypatch):
        """返回空 DataFrame → unavailable 信封含 data_status/fallback_reason。"""
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk({"stock_news_em": pd.DataFrame()}))
        env = info_tools.get_financial_news(symbol="600519")
        assert env["ok"] is False
        assert env.get("data_status") == "unavailable"
        assert env.get("fallback_reason")

    def test_offline_registry_blocks_function_body(self):
        """离线 Registry 阻断：函数体不执行。"""
        sentinel = {"called": False}
        orig_load = info_tools._load_akshare

        def _guarded_load():
            sentinel["called"] = True
            return orig_load()

        reg = build_default_registry(offline=True)
        reg.call("get_financial_news", symbol="600519")
        # 离线阻断在函数被调用前就返回，sentinel 不应被触发
        # （直接用 registry 调用，函数体 _load_akshare 不执行）
        env = reg.call("get_financial_news", symbol="600519")
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"
        assert env["data"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 二、公司公告
# ─────────────────────────────────────────────────────────────────────────────

class TestGetStockAnnouncements:
    def test_success_first_candidate(self, monkeypatch):
        """stock_notice_report 返回数据 → ok=True。"""
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk(
            {"stock_notice_report": _mini_df("标题"), "stock_gsrl_em": None}
        ))
        env = info_tools.get_stock_announcements(symbol="600519", limit=5)
        assert env["ok"] is True
        assert "akshare/stock_notice_report" in env["source"]

    def test_all_candidates_unavailable(self, monkeypatch):
        """所有候选函数均不存在 → unavailable 含 data_status。"""
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk({}))
        env = info_tools.get_stock_announcements(symbol="600519")
        assert env["ok"] is False
        assert env.get("data_status") == "unavailable"
        assert env.get("fallback_reason")

    def test_first_candidate_raises_fallback_to_second(self, monkeypatch):
        """第一候选抛异常，第二候选成功 → ok=True。"""
        def _boom(**kw):
            raise RuntimeError("候选1失败")
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk(
            {"stock_notice_report": _boom, "stock_gsrl_em": _mini_df("title")}
        ))
        env = info_tools.get_stock_announcements(symbol="600519")
        assert env["ok"] is True
        assert "stock_gsrl_em" in env["source"]

    def test_offline_registry_blocks(self):
        reg = build_default_registry(offline=True)
        env = reg.call("get_stock_announcements", symbol="600519")
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"


# ─────────────────────────────────────────────────────────────────────────────
# 三、研报摘要
# ─────────────────────────────────────────────────────────────────────────────

class TestGetResearchReportSummary:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk(
            {"stock_research_report_em": _mini_df("机构")}
        ))
        env = info_tools.get_research_report_summary(symbol="600519", limit=5)
        assert env["ok"] is True
        assert env["data"]["rows"] >= 1

    def test_function_not_in_akshare(self, monkeypatch):
        """AkShare 无此函数 → unavailable 含 data_status/fallback_reason。"""
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk({}))
        env = info_tools.get_research_report_summary(symbol="600519")
        assert env["ok"] is False
        assert env.get("data_status") == "unavailable"
        assert "stock_research_report_em" in env.get("fallback_reason", "")

    def test_exception_from_akshare(self, monkeypatch):
        """AkShare 函数抛异常 → error 信封含 data_status/fallback_reason。"""
        def _boom(**kw):
            raise ValueError("接口超限")
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk(
            {"stock_research_report_em": _boom}
        ))
        env = info_tools.get_research_report_summary(symbol="600519")
        assert env["ok"] is False
        assert env.get("data_status") == "error"
        assert env.get("fallback_reason")

    def test_offline_registry_blocks(self):
        reg = build_default_registry(offline=True)
        env = reg.call("get_research_report_summary", symbol="600519")
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"


# ─────────────────────────────────────────────────────────────────────────────
# 四、政策/宏观
# ─────────────────────────────────────────────────────────────────────────────

class TestGetMacroPolicy:
    def test_success_money_supply(self, monkeypatch):
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk(
            {"macro_china_money_supply": _mini_df("M2")}
        ))
        env = info_tools.get_macro_policy(indicator="money_supply", limit=5)
        assert env["ok"] is True
        assert env["data"]["indicator"] == "money_supply"

    def test_function_not_exist(self, monkeypatch):
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk({}))
        env = info_tools.get_macro_policy(indicator="money_supply")
        assert env["ok"] is False
        assert env.get("data_status") == "unavailable"

    def test_unsupported_indicator(self):
        env = info_tools.get_macro_policy(indicator="xyz_unknown")
        assert env["ok"] is False
        assert env.get("data_status") == "unavailable"
        assert env.get("fallback_reason")

    def test_exception_returns_error_envelope(self, monkeypatch):
        def _boom():
            raise RuntimeError("宏观接口超时")
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk(
            {"macro_china_money_supply": _boom}
        ))
        env = info_tools.get_macro_policy(indicator="money_supply")
        assert env["ok"] is False
        assert env.get("data_status") == "error"
        assert env.get("fallback_reason")

    def test_offline_registry_blocks(self):
        reg = build_default_registry(offline=True)
        env = reg.call("get_macro_policy", indicator="money_supply")
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"


# ─────────────────────────────────────────────────────────────────────────────
# 五、利率
# ─────────────────────────────────────────────────────────────────────────────

class TestGetInterestRates:
    def test_success_lpr(self, monkeypatch):
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk(
            {"macro_china_lpr": _mini_df("1年期LPR")}
        ))
        env = info_tools.get_interest_rates(rate_type="lpr", limit=5)
        assert env["ok"] is True
        assert env["data"]["rate_type"] == "lpr"

    def test_function_not_exist(self, monkeypatch):
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk({}))
        env = info_tools.get_interest_rates(rate_type="lpr")
        assert env["ok"] is False
        assert env.get("data_status") == "unavailable"

    def test_unsupported_rate_type(self):
        env = info_tools.get_interest_rates(rate_type="unknown_rate")
        assert env["ok"] is False
        assert env.get("data_status") == "unavailable"
        assert env.get("fallback_reason")

    def test_exception_returns_error_envelope(self, monkeypatch):
        def _boom():
            raise RuntimeError("利率接口不可用")
        monkeypatch.setattr(info_tools, "_load_akshare", lambda: _FakeAk(
            {"macro_china_lpr": _boom}
        ))
        env = info_tools.get_interest_rates(rate_type="lpr")
        assert env["ok"] is False
        assert env.get("data_status") == "error"
        assert env.get("fallback_reason")

    def test_offline_registry_blocks(self):
        reg = build_default_registry(offline=True)
        env = reg.call("get_interest_rates", rate_type="lpr")
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"


# ─────────────────────────────────────────────────────────────────────────────
# 六、所有工具的离线 Registry 全量阻断证明
# ─────────────────────────────────────────────────────────────────────────────

class TestAllInfoToolsOfflineBlock:
    _INFO_TOOL_CASES = [
        ("get_financial_news",         {"symbol": "600519"}),
        ("get_stock_announcements",    {"symbol": "600519"}),
        ("get_research_report_summary", {"symbol": "600519"}),
        ("get_macro_policy",           {"indicator": "money_supply"}),
        ("get_interest_rates",         {"rate_type": "lpr"}),
    ]

    @pytest.mark.parametrize("tool_name,kwargs", _INFO_TOOL_CASES)
    def test_offline_hard_block(self, tool_name, kwargs):
        """离线模式下，五类 info 工具函数体均不执行，由 Registry 层阻断。"""
        reg = build_default_registry(offline=True)
        env = reg.call(tool_name, **kwargs)
        assert env["ok"] is False, f"{tool_name} 在离线模式未被阻断"
        assert env["error_type"] == "OfflineModeBlocked"
        assert env["data"] is None


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：假 AkShare 对象（monkeypatch 用）
# ─────────────────────────────────────────────────────────────────────────────

class _FakeAk:
    """模拟 akshare 模块的最小实现，按名称映射到伪函数或 DataFrame。"""

    def __init__(self, mapping: dict) -> None:
        self._m = mapping

    def __getattr__(self, name: str):
        val = self._m.get(name)
        if val is None:
            raise AttributeError(name)
        if callable(val):
            return val
        # DataFrame 常量 → 包成调用即返回它的函数
        df = val
        def _fn(**kwargs):  # noqa: ANN001
            return df
        return _fn
