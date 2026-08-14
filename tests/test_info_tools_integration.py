"""
任务 B：信息 MCP 工具的完整测试套件
=====================================
覆盖：
1. 每类工具的成功与 unavailable 分支（monkeypatch）
2. Registry offline 模式硬阻断（函数体未执行证明）
3. 主业务链接入点（信息证据出现在 state 中）
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_platform.mcp.info_tools import (
    get_financial_news,
    get_interest_rates,
    get_macro_policy,
    get_research_report_summary,
    get_stock_announcements,
)
from agent_platform.mcp.registry import build_default_registry


# ═════════════════════════════════════════════════════════════════════════════
# 1. 单工具 monkeypatch 测试：成功 + unavailable
# ═════════════════════════════════════════════════════════════════════════════

def test_get_financial_news_success(monkeypatch):
    """财经新闻成功返回。"""
    mock_df = MagicMock()
    mock_df.empty = False
    mock_df.tail.return_value = mock_df
    mock_df.to_dict.return_value = [
        {"标题": "市场回暖", "发布时间": "2026-08-10", "内容": "..."},
        {"标题": "政策利好", "发布时间": "2026-08-09", "内容": "..."},
    ]

    def mock_load_akshare():
        ak = MagicMock()
        ak.stock_news_em.return_value = mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_financial_news(symbol="600519", limit=20)
    assert env["ok"] is True
    assert env["source"] == "akshare/stock_news_em"
    assert "updated_at" in env
    assert len(env["data"]["records"]) == 2


def test_get_financial_news_unavailable(monkeypatch):
    """财经新闻上游返回空 → unavailable。"""
    mock_df = MagicMock()
    mock_df.empty = True

    def mock_load_akshare():
        ak = MagicMock()
        ak.stock_news_em.return_value = mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_financial_news(symbol="600519", limit=20)
    assert env["ok"] is False
    assert env["data_status"] == "unavailable"
    assert "上游返回空新闻列表" in env["fallback_reason"]


def test_get_stock_announcements_success(monkeypatch):
    """公司公告成功返回。"""
    mock_df = MagicMock()
    mock_df.empty = False
    mock_df.tail.return_value = mock_df
    mock_df.to_dict.return_value = [
        {"公告标题": "2026年半年报", "公告日期": "2026-08-01"},
        {"公告标题": "董事会决议", "公告日期": "2026-07-30"},
    ]

    def mock_load_akshare():
        ak = MagicMock()
        ak.stock_notice_report = lambda **kw: mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_stock_announcements(symbol="600519", limit=10)
    assert env["ok"] is True
    assert "stock_notice_report" in env["source"]
    assert len(env["data"]["records"]) == 2


def test_get_stock_announcements_unavailable(monkeypatch):
    """公告接口全不可用。"""
    def mock_load_akshare():
        ak = MagicMock()
        # 所有候选函数都抛异常
        ak.stock_notice_report = MagicMock(side_effect=ValueError("not available"))
        ak.stock_gsrl_em = MagicMock(side_effect=ValueError("not available"))
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_stock_announcements(symbol="600519", limit=10)
    assert env["ok"] is False
    assert env["data_status"] == "unavailable"
    assert "公告接口全部不可用" in env["fallback_reason"]


def test_get_research_report_summary_success(monkeypatch):
    """研报列表成功返回。"""
    mock_df = MagicMock()
    mock_df.empty = False
    mock_df.tail.return_value = mock_df
    mock_df.to_dict.return_value = [
        {"标题": "买入评级", "机构": "某券商", "目标价": "2000"},
    ]

    def mock_load_akshare():
        ak = MagicMock()
        ak.stock_research_report_em = lambda **kw: mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_research_report_summary(symbol="600519", limit=5)
    assert env["ok"] is True
    assert env["source"] == "akshare/stock_research_report_em"
    assert "仅提供研报元数据" in env["data"]["content_note"]


def test_get_research_report_unavailable(monkeypatch):
    """研报列表为空。"""
    mock_df = MagicMock()
    mock_df.empty = True

    def mock_load_akshare():
        ak = MagicMock()
        ak.stock_research_report_em = lambda **kw: mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_research_report_summary(symbol="600519", limit=5)
    assert env["ok"] is False
    assert env["data_status"] == "unavailable"
    assert "无研报列表数据" in env["fallback_reason"]


def test_get_macro_policy_success(monkeypatch):
    """宏观政策成功返回。"""
    mock_df = MagicMock()
    mock_df.empty = False
    mock_df.tail.return_value = mock_df
    mock_df.to_dict.return_value = [
        {"月份": "2026-07", "M2同比增长": "8.5%"},
    ]

    def mock_load_akshare():
        ak = MagicMock()
        ak.macro_china_money_supply = lambda: mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_macro_policy(indicator="money_supply", limit=12)
    assert env["ok"] is True
    assert env["source"] == "akshare/macro_china_money_supply"
    assert len(env["data"]["records"]) == 1


def test_get_macro_policy_unavailable(monkeypatch):
    """宏观数据为空。"""
    mock_df = MagicMock()
    mock_df.empty = True

    def mock_load_akshare():
        ak = MagicMock()
        ak.macro_china_money_supply = lambda: mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_macro_policy(indicator="money_supply", limit=12)
    assert env["ok"] is False
    assert env["data_status"] == "unavailable"


def test_get_interest_rates_success(monkeypatch):
    """利率数据成功返回。"""
    mock_df = MagicMock()
    mock_df.empty = False
    mock_df.tail.return_value = mock_df
    mock_df.to_dict.return_value = [
        {"日期": "2026-08-01", "1年": "3.45", "5年以上": "4.20"},
    ]

    def mock_load_akshare():
        ak = MagicMock()
        ak.macro_china_lpr = lambda: mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_interest_rates(rate_type="lpr", limit=24)
    assert env["ok"] is True
    assert env["source"] == "akshare/macro_china_lpr"


def test_get_interest_rates_unavailable(monkeypatch):
    """利率数据为空。"""
    mock_df = MagicMock()
    mock_df.empty = True

    def mock_load_akshare():
        ak = MagicMock()
        ak.macro_china_lpr = lambda: mock_df
        return ak

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)
    env = get_interest_rates(rate_type="lpr", limit=24)
    assert env["ok"] is False
    assert env["data_status"] == "unavailable"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Registry offline 硬阻断（函数体未执行证明）
# ═════════════════════════════════════════════════════════════════════════════

def test_registry_offline_blocks_info_tools():
    """离线模式：信息工具被注册表硬阻断，函数体未执行。"""
    reg = build_default_registry(offline=True)

    # 所有信息工具在离线模式必须被阻断
    for tool_name in [
        "get_financial_news",
        "get_stock_announcements",
        "get_research_report_summary",
        "get_macro_policy",
        "get_interest_rates",
    ]:
        env = reg.call(tool_name, symbol="TEST001", limit=5)
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"
        assert "离线模式禁止网络调用" in env["error"]
        # 审计日志证明被阻断
        last_record = reg.call_log[-1]
        assert last_record.tool == tool_name
        assert last_record.blocked_offline is True
        assert last_record.duration_s >= 0  # 必须有耗时记录


def test_registry_offline_function_body_not_executed(monkeypatch):
    """离线模式：工具函数体完全未执行（通过 monkeypatch 断言）。"""
    execution_flag = {"executed": False}

    def mock_load_akshare():
        execution_flag["executed"] = True  # 如果函数体执行，此标志会被设为 True
        raise AssertionError("离线模式下函数体不应被执行")

    monkeypatch.setattr("agent_platform.mcp.info_tools._load_akshare", mock_load_akshare)

    reg = build_default_registry(offline=True)
    env = reg.call("get_financial_news", symbol="600519", limit=10)

    assert env["ok"] is False
    assert env["error_type"] == "OfflineModeBlocked"
    # 证明：函数体未执行，_load_akshare 从未被调用
    assert execution_flag["executed"] is False


# ═════════════════════════════════════════════════════════════════════════════
# 3. 主业务链接入测试（信息证据必须出现在 state 中）
# ═════════════════════════════════════════════════════════════════════════════

def test_info_evidence_in_real_offline_langgraph_state():
    """完整离线主链必须产出五类信息证据，且全部在 Registry 层阻断。"""
    from agent_platform.finance.securities_graph import run_securities_analysis

    state = run_securities_analysis("TEST001", data_mode="offline")

    evidence = state["information_evidence"]
    assert len(evidence) == 5
    assert {item["tool"] for item in evidence} == {
        "get_financial_news",
        "get_stock_announcements",
        "get_research_report_summary",
        "get_macro_policy",
        "get_interest_rates",
    }
    assert all(item["ok"] is False for item in evidence)
    assert all(item["error_type"] == "OfflineModeBlocked" for item in evidence)
    assert all(item["data_status"] == "offline_blocked" for item in evidence)
    assert state["synthesis"]["information_evidence"] == evidence
    assert state["information_trace"]["total"] == 5
    assert state["information_trace"]["offline"] is True


def test_online_information_summary_uses_real_envelope_metadata(monkeypatch):
    """受控在线信封必须被裁剪成摘要，正文不能进入 LangGraph 状态。"""
    from agent_platform.finance import securities_graph

    class FakeRegistry:
        def call(self, tool: str, **_params):
            return {
                "tool": tool,
                "ok": True,
                "data": {"rows": 2, "records": [{"title": "sensitive body"}]},
                "source": f"public/{tool}",
                "updated_at": "2026-08-10T00:00:00Z",
                "error": None,
                "error_type": None,
            }

    monkeypatch.setattr(
        "agent_platform.mcp.registry.build_default_registry",
        lambda **_kwargs: FakeRegistry(),
    )
    evidence, trace, limitations = securities_graph._collect_information_evidence({
        "symbol": "600519", "data_mode": "auto",
    })

    assert len(evidence) == 5
    assert limitations == []
    assert trace == {"total": 5, "ok": 5, "unavailable": 0, "offline": False}
    assert all(item["ok"] is True and item["data_status"] == "ok" for item in evidence)
    assert all(item["record_count"] == 2 for item in evidence)
    assert "records" not in str(evidence)
    assert "sensitive body" not in str(evidence)


def test_online_information_tools_start_concurrently(monkeypatch):
    """五类独立信息工具应并发启动，同时保持声明顺序输出。"""
    from agent_platform.finance import securities_graph

    barrier = threading.Barrier(5, timeout=2)

    class FakeRegistry:
        def call(self, tool: str, **_params):
            barrier.wait()
            return {
                "tool": tool,
                "ok": True,
                "data": {"rows": 1},
                "source": f"public/{tool}",
                "updated_at": "2026-08-10T00:00:00Z",
                "error": None,
                "error_type": None,
            }

    monkeypatch.setattr(
        "agent_platform.mcp.registry.build_default_registry",
        lambda **_kwargs: FakeRegistry(),
    )
    evidence, trace, limitations = securities_graph._collect_information_evidence({
        "symbol": "600519", "data_mode": "auto",
    })

    assert barrier.n_waiting == 0
    assert [item["tool"] for item in evidence] == [item[0] for item in securities_graph._INFO_TOOL_CALLS]
    assert trace["ok"] == 5
    assert limitations == []
