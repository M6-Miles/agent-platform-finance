"""
任务 A 回归测试：五路状态分类
===============================
测试 validate_deliverables.py 的 classify_preflight_results() 纯函数。

验证：
- 五类互斥（每条记录只进一类）
- 总和正确（sum == len(results)）
- no_trade 不计入 error
- manual_review 不计入 execute
- 真正异常才进 error
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "Scripts"))

from validate_deliverables import classify_graph_state, classify_preflight_results


def test_all_execute():
    """全部通过风控（execute）。"""
    results = [{"preflight": "execute"} for _ in range(5)]
    counts = classify_preflight_results(results)
    assert counts == {
        "execute": 5,
        "manual_review": 0,
        "block": 0,
        "no_trade": 0,
        "error": 0,
    }
    assert sum(counts.values()) == 5


def test_mixed_batch():
    """混合批次：五类各有记录。"""
    results = [
        {"preflight": "execute", "status": "✅ execute"},
        {"preflight": "execute", "status": "✅ execute"},
        {"preflight": "manual_review", "status": "⚠️ manual_review"},
        {"preflight": "block", "status": "⛔ block"},
        {"preflight": "no_trade", "status": "⚪ low_confidence"},
        {"preflight": "error", "status": "❌ ValueError"},
    ]
    counts = classify_preflight_results(results)
    assert counts["execute"] == 2
    assert counts["manual_review"] == 1
    assert counts["block"] == 1
    assert counts["no_trade"] == 1
    assert counts["error"] == 1
    assert sum(counts.values()) == 6


def test_no_trade_not_counted_as_error():
    """no_trade 是正常状态，不计入 error。"""
    results = [
        {"preflight": "no_trade", "status": "⚪ low_confidence"},
        {"preflight": "no_trade", "status": "⚪ hold"},
        {"preflight": "error", "status": "❌ Exception"},
    ]
    counts = classify_preflight_results(results)
    assert counts["no_trade"] == 2
    assert counts["error"] == 1  # 只有真正异常才是 error
    assert sum(counts.values()) == 3


def test_manual_review_not_execute():
    """manual_review 不计入 execute。"""
    results = [
        {"preflight": "execute", "status": "✅"},
        {"preflight": "manual_review", "status": "⚠️ 需人工审批"},
        {"status": "需人工审批"},  # status 包含"审批"但无 preflight
    ]
    counts = classify_preflight_results(results)
    assert counts["execute"] == 1
    assert counts["manual_review"] == 2  # 两种形式都计入 manual_review
    assert sum(counts.values()) == 3


def test_mutually_exclusive():
    """五类互斥：每条记录只进一类。"""
    results = [
        {"preflight": "execute"},
        {"preflight": "manual_review"},
        {"preflight": "block"},
        {"preflight": "no_trade"},
        {"preflight": "error"},
    ]
    counts = classify_preflight_results(results)
    assert counts["execute"] == 1
    assert counts["manual_review"] == 1
    assert counts["block"] == 1
    assert counts["no_trade"] == 1
    assert counts["error"] == 1
    # 每条记录恰好进一类，无重复
    assert sum(counts.values()) == 5


def test_unknown_preflight_defaults_to_error():
    """未知 preflight 防御性归入 error。"""
    results = [
        {"preflight": "unknown_state", "status": "some_status"},
        {"status": "no preflight key"},
    ]
    counts = classify_preflight_results(results)
    assert counts["error"] == 2  # 未知状态都归入 error
    assert sum(counts.values()) == 2


def test_empty_results():
    """空批次返回全零。"""
    counts = classify_preflight_results([])
    assert counts == {
        "execute": 0,
        "manual_review": 0,
        "block": 0,
        "no_trade": 0,
        "error": 0,
    }
    assert sum(counts.values()) == 0


def test_real_error_only():
    """真正异常才是 error，no_trade/block 不是。"""
    results = [
        {"preflight": "error", "status": "❌ ValueError: invalid symbol"},
        {"preflight": "error", "status": "❌ ConnectionError"},
        {"preflight": "no_trade", "status": "⚪ 低置信度"},
        {"preflight": "block", "status": "⛔ 风控阻断"},
    ]
    counts = classify_preflight_results(results)
    assert counts["error"] == 2  # 只有 preflight="error" 才是真正异常
    assert counts["no_trade"] == 1
    assert counts["block"] == 1
    assert sum(counts.values()) == 4


def test_graph_interrupt_is_manual_review_not_no_trade():
    state = {
        "status": "trading",
        "final_action": None,
        "__interrupt__": ({"type": "manual_review"},),
    }
    assert classify_graph_state(state) == "manual_review"


def test_graph_explicit_no_trade_requires_no_trade_terminal_state():
    assert classify_graph_state({"status": "no_trade"}) == "no_trade"


def test_graph_unknown_incomplete_state_is_error():
    assert classify_graph_state({"status": "trading"}) == "error"
