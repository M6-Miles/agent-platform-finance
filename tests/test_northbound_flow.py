"""
北向资金 MCP 工具 + 大盘 Agent MCP 改接线 专项测试
==================================================
本文件覆盖两件事：

一、`get_northbound_flow` 工具本身
    交易所自 2024-08 起取消沪深港通逐日净买额披露，上游最新数百行的
    「当日成交净买额」恒为 NaN。工具必须：
      * NaN → None，**绝不当 0**（红线：不得把缺失值当真实行情）；
      * 显式暴露 last_available_date / staleness_days / is_fresh；
      * 接口被上游重命名时抛出可见错误，而不是静默返回 None。

二、`analyze_market_regime` 经 MCP 取数
    原实现硬编码 `ak.stock_em_hsgt_north_acc_flow_in_one`（akshare 1.18.x 已移除），
    AttributeError 被 `logger.debug("非关键")` 吞掉，导致在线模式 northbound
    恒为 None 且无人可见。本文件用回归测试锁死修复后的行为。

全部测试零网络：akshare 通过 monkeypatch 替换 `_load_akshare`，
Agent 通过 monkeypatch 替换 `get_registry` 注入桩注册表。
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from agent_platform.mcp import akshare_tools as at
from agent_platform.mcp.envelope import ok_envelope, validate_envelope
from agent_platform.mcp.registry import build_default_registry


# ═══════════════════════════════════════════════════════════════
#   测试脚手架：假 akshare 模块（零网络）
# ═══════════════════════════════════════════════════════════════

def _dstr(days_ago: int) -> str:
    """返回 days_ago 个自然日前的 YYYY-MM-DD。"""
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _make_df(rows: list[tuple[str, float | None]]) -> pd.DataFrame:
    """构造与上游同构的 DataFrame：日期 + 当日成交净买额（单位亿元）。"""
    return pd.DataFrame(
        {
            "日期": [d for d, _ in rows],
            "当日成交净买额": [math.nan if v is None else v for _, v in rows],
            "历史累计净买额": [1.76 for _ in rows],
            "持股市值": [0.0 for _ in rows],
        }
    )


class FakeAk:
    """只暴露指定接口名的假 akshare 模块。"""

    def __init__(self, fn_name: str, df: pd.DataFrame) -> None:
        self._df = df
        self.received: list[dict[str, Any]] = []
        setattr(self, fn_name, self._impl)

    def _impl(self, **kwargs: Any) -> pd.DataFrame:
        self.received.append(kwargs)
        return self._df


def _install(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr(at, "_load_akshare", lambda: fake)


# ═══════════════════════════════════════════════════════════════
#   一、停止披露场景：NaN 绝不当 0
# ═══════════════════════════════════════════════════════════════

class TestStoppedDisclosure:
    """上游最新若干行净买额为 NaN —— 这是真实的线上现状。"""

    @pytest.fixture
    def env(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        df = _make_df([
            (_dstr(700), -67.7499),   # 最后一次有数值
            (_dstr(699), None),
            (_dstr(3), None),
            (_dstr(2), None),
            (_dstr(1), None),
        ])
        _install(monkeypatch, FakeAk("stock_hsgt_hist_em", df))
        return at.get_northbound_flow()

    def test_envelope_structurally_valid(self, env):
        assert validate_envelope(env) == []

    def test_call_succeeds_because_interface_works(self, env):
        """接口本身可用（取回了数据），失败的只是某个字段 —— 不能报成接口坏了。"""
        assert env["ok"] is True

    def test_latest_net_inflow_is_none_not_zero(self, env):
        """红线：NaN 必须是 None，绝不能变成 0。"""
        d = env["data"]
        assert d["latest_net_inflow_cny"] is None
        assert d["latest_net_inflow_cny"] != 0
        assert d["latest_net_inflow_yi"] is None

    def test_availability_flag_false(self, env):
        assert env["data"]["net_inflow_available"] is False

    def test_last_available_date_exposed(self, env):
        assert env["data"]["last_available_date"] == _dstr(700)

    def test_staleness_days_computed(self, env):
        assert env["data"]["staleness_days"] == pytest.approx(700, abs=1)

    def test_is_fresh_false(self, env):
        assert env["data"]["is_fresh"] is False

    def test_availability_note_explains_gap(self, env):
        note = env["data"]["availability_note"]
        # 断言实质内容而非具体措辞：必须说明这是上游披露缺口而非网络故障，
        # 且必须给出最后一次有数据的日期，否则调用方无法判断陈旧程度。
        assert "披露" in note
        assert "不是网络故障" in note
        assert str(env["data"]["last_available_date"]) in note

    def test_last_available_value_converted_to_cny(self, env):
        """-67.7499 亿元 → -6.77499e9 元。"""
        assert env["data"]["last_available_net_inflow_cny"] == pytest.approx(-6774990000.0)


# ═══════════════════════════════════════════════════════════════
#   二、正常披露场景（历史数据 / 上游若恢复披露）
# ═══════════════════════════════════════════════════════════════

class TestFreshDisclosure:
    @pytest.fixture
    def env(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        df = _make_df([(_dstr(2), 30.0), (_dstr(1), 12.5)])
        _install(monkeypatch, FakeAk("stock_hsgt_hist_em", df))
        return at.get_northbound_flow()

    def test_ok(self, env):
        assert env["ok"] is True

    def test_unit_conversion_yi_to_cny(self, env):
        """12.5 亿元 → 1.25e9 元。单位换算错误会让风险偏好判断整体失真。"""
        assert env["data"]["latest_net_inflow_cny"] == pytest.approx(1.25e9)
        assert env["data"]["latest_net_inflow_yi"] == pytest.approx(12.5)

    def test_available_and_fresh(self, env):
        d = env["data"]
        assert d["net_inflow_available"] is True
        assert d["is_fresh"] is True
        assert d["staleness_days"] <= at._NORTHBOUND_STALE_DAYS

    def test_note_says_provided(self, env):
        assert "提供了当日成交净买额" in env["data"]["availability_note"]


class TestFreshnessBoundary:
    """新鲜度阈值边界：阈值内算新鲜，阈值外不算。"""

    def _fresh_flag(self, monkeypatch: pytest.MonkeyPatch, days_ago: int) -> bool:
        df = _make_df([(_dstr(days_ago), 5.0)])
        _install(monkeypatch, FakeAk("stock_hsgt_hist_em", df))
        return at.get_northbound_flow()["data"]["is_fresh"]

    def test_inside_threshold_is_fresh(self, monkeypatch):
        assert self._fresh_flag(monkeypatch, at._NORTHBOUND_STALE_DAYS - 1) is True

    def test_outside_threshold_not_fresh(self, monkeypatch):
        assert self._fresh_flag(monkeypatch, at._NORTHBOUND_STALE_DAYS + 5) is False


# ═══════════════════════════════════════════════════════════════
#   三、接口重命名 / 消失必须可见（本次修复的缺陷类）
# ═══════════════════════════════════════════════════════════════

class TestInterfaceResolution:
    def test_resolves_first_available_candidate(self, monkeypatch):
        df = _make_df([(_dstr(1), 1.0)])
        fake = FakeAk("stock_hsgt_hist_em", df)
        _install(monkeypatch, fake)
        env = at.get_northbound_flow()
        assert env["data"]["upstream_fn"] == "stock_hsgt_hist_em"
        assert "stock_hsgt_hist_em" in env["source"]

    def test_falls_back_to_legacy_candidate_name(self, monkeypatch):
        """旧版 akshare 只有老名字时也应可用。"""
        df = _make_df([(_dstr(1), 2.0)])
        fake = FakeAk("stock_em_hsgt_north_acc_flow_in_one", df)
        _install(monkeypatch, fake)
        env = at.get_northbound_flow()
        assert env["data"]["upstream_fn"] == "stock_em_hsgt_north_acc_flow_in_one"

    def test_missing_all_candidates_raises_visibly(self, monkeypatch):
        """
        回归测试：原缺陷正是「接口名消失 → AttributeError 被吞 → 字段恒 None」。
        现在必须抛出 AttributeError（由注册表转成失败信封），不得静默。
        """
        class Empty:
            pass

        _install(monkeypatch, Empty())
        with pytest.raises(AttributeError) as exc:
            at.get_northbound_flow()
        assert "akshare" in str(exc.value)

    def test_registry_converts_missing_interface_to_error_envelope(self, monkeypatch):
        class Empty:
            pass

        _install(monkeypatch, Empty())
        reg = build_default_registry(offline=False)
        env = reg.call("get_northbound_flow")
        assert env["ok"] is False
        assert env["error_type"] == "AttributeError"
        assert env["data"] is None


# ═══════════════════════════════════════════════════════════════
#   四、入参校验与空结果
# ═══════════════════════════════════════════════════════════════

class TestValidation:
    def test_rejects_unknown_symbol(self, monkeypatch):
        _install(monkeypatch, FakeAk("stock_hsgt_hist_em", _make_df([(_dstr(1), 1.0)])))
        with pytest.raises(ValueError):
            at.get_northbound_flow(symbol="乱七八糟")

    def test_default_symbol_is_northbound(self, monkeypatch):
        fake = FakeAk("stock_hsgt_hist_em", _make_df([(_dstr(1), 1.0)]))
        _install(monkeypatch, fake)
        at.get_northbound_flow()
        assert fake.received[0]["symbol"] == "北向资金"

    def test_empty_upstream_is_failure_not_empty_success(self, monkeypatch):
        """空表算失败，避免上层把空数组当成「查到了但是 0」。"""
        _install(monkeypatch, FakeAk("stock_hsgt_hist_em", pd.DataFrame()))
        env = at.get_northbound_flow()
        assert env["ok"] is False
        assert env["error_type"] == "EmptyResult"
        assert env["data"] is None

    def test_limit_trims_records(self, monkeypatch):
        rows = [(_dstr(10 - i), float(i)) for i in range(10)]
        _install(monkeypatch, FakeAk("stock_hsgt_hist_em", _make_df(rows)))
        env = at.get_northbound_flow(limit=3)
        assert env["data"]["rows"] == 10          # 总行数不受 limit 影响
        assert len(env["data"]["records"]) == 3   # 仅返回的明细被裁剪

    def test_scans_full_history_not_just_limit_window(self, monkeypatch):
        """
        关键：停止披露已持续数百个交易日。若只扫 limit 窗口会全是 NaN，
        把「已知数据缺口」误判成「找不到任何历史数据」。
        """
        rows = [(_dstr(800), -50.0)] + [(_dstr(30 - i), None) for i in range(30)]
        _install(monkeypatch, FakeAk("stock_hsgt_hist_em", _make_df(rows)))
        env = at.get_northbound_flow(limit=5)
        assert env["data"]["last_available_date"] == _dstr(800)


# ═══════════════════════════════════════════════════════════════
#   五、注册与离线硬阻断
# ═══════════════════════════════════════════════════════════════

class TestRegistration:
    def test_registered_in_default_registry(self):
        assert build_default_registry(offline=True).has("get_northbound_flow")

    def test_declared_as_network_tool(self):
        spec = build_default_registry(offline=True).spec("get_northbound_flow")
        assert spec.requires_network is True
        assert spec.provider == "akshare"
        assert spec.category == "fundflow"

    def test_blocked_in_offline_registry(self, monkeypatch):
        """离线模式下函数体一次也不能执行。"""
        sentinel = {"hit": False}

        def _boom() -> Any:
            sentinel["hit"] = True
            raise AssertionError("离线模式下不应加载 akshare")

        monkeypatch.setattr(at, "_load_akshare", _boom)
        env = build_default_registry(offline=True).call("get_northbound_flow")
        assert env["ok"] is False
        assert env["error_type"] == "OfflineModeBlocked"
        assert sentinel["hit"] is False


# ═══════════════════════════════════════════════════════════════
#   六、大盘 Agent 经 MCP 取数
# ═══════════════════════════════════════════════════════════════

class StubRegistry:
    """桩注册表：按工具名返回预置信封，并记录调用序列。"""

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(name)
        if name not in self.responses:
            raise AssertionError(f"Agent 调用了未预期的工具: {name}")
        return self.responses[name]


def _index_env(closes: list[float]) -> dict[str, Any]:
    return ok_envelope(
        tool="get_index_daily",
        source="akshare/stock_zh_index_daily",
        data={
            "index_code": "sh000001",
            "rows": len(closes),
            "records": [{"date": _dstr(len(closes) - i), "close": c}
                        for i, c in enumerate(closes)],
        },
    )


def _nb_env(*, fresh: bool, latest_cny: float | None) -> dict[str, Any]:
    return ok_envelope(
        tool="get_northbound_flow",
        source="akshare/stock_hsgt_hist_em",
        data={
            "latest_net_inflow_cny": latest_cny,
            "net_inflow_available": latest_cny is not None,
            "is_fresh": fresh,
            "last_available_date": _dstr(1 if fresh else 700),
            "staleness_days": 1 if fresh else 700,
        },
    )


_CLOSES = [3000.0, 3010.0, 3020.0, 3030.0, 3040.0, 3050.0, 3060.0, 3070.0, 3080.0, 3100.0]


def _patch_registry(monkeypatch: pytest.MonkeyPatch, stub: StubRegistry) -> None:
    import agent_platform.mcp as mcp_pkg

    monkeypatch.setattr(mcp_pkg, "get_registry", lambda **_kw: stub)


class TestAgentOfflineGoesThroughMCP:
    def test_source_marks_mcp_tool(self):
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        r = analyze_market_regime(force_offline=True)
        assert "MCP:" in r.source, f"未经过 MCP 层: {r.source}"
        assert "内置样例数据" in r.source
        assert r.data_status == "offline_sample"
        assert r.fallback_reason is None

    def test_offline_uses_offline_tool_only(self, monkeypatch):
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        stub = StubRegistry({
            "get_offline_market_regime": ok_envelope(
                tool="get_offline_market_regime",
                source="offline_sample/offline_sample_data.py",
                data={
                    "regime": "bull", "risk_appetite": "high",
                    "index_close": 3200.0, "index_change_pct_5d": 4.2,
                    "northbound_flow_cny": 9e8, "regime_note": "样例",
                },
            ),
        })
        _patch_registry(monkeypatch, stub)
        analyze_market_regime(force_offline=True)
        assert stub.calls == ["get_offline_market_regime"]


class TestAgentOnlineNorthbound:
    def test_stale_northbound_is_not_used(self, monkeypatch):
        """
        红线回归：距今 700 天的存量净买额**不得**当作当日资金面。
        """
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        stub = StubRegistry({
            "get_index_daily": _index_env(_CLOSES),
            "get_northbound_flow": _nb_env(fresh=False, latest_cny=None),
        })
        _patch_registry(monkeypatch, stub)
        r = analyze_market_regime()

        assert r.northbound_flow_cny is None
        assert r.data_status == "live"          # 指数是实时的
        assert "北向资金当日净买额不可用" in r.regime_note
        assert "停止逐日披露" in r.regime_note

    def test_stale_value_present_but_still_rejected(self, monkeypatch):
        """即便上游给了数值，只要 is_fresh=False 就不得采用。"""
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        stub = StubRegistry({
            "get_index_daily": _index_env(_CLOSES),
            "get_northbound_flow": _nb_env(fresh=False, latest_cny=-6.77e9),
        })
        _patch_registry(monkeypatch, stub)
        r = analyze_market_regime()
        assert r.northbound_flow_cny is None

    def test_fresh_northbound_is_used(self, monkeypatch):
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        stub = StubRegistry({
            "get_index_daily": _index_env(_CLOSES),
            "get_northbound_flow": _nb_env(fresh=True, latest_cny=8.5e8),
        })
        _patch_registry(monkeypatch, stub)
        r = analyze_market_regime()

        assert r.northbound_flow_cny == pytest.approx(8.5e8)
        assert r.risk_appetite == "high"        # >5e8 → high
        assert "北向净流入" in r.regime_note

    def test_northbound_tool_failure_recorded_in_note(self, monkeypatch):
        from agent_platform.finance.market_regime_agent import analyze_market_regime
        from agent_platform.mcp.envelope import err_envelope

        stub = StubRegistry({
            "get_index_daily": _index_env(_CLOSES),
            "get_northbound_flow": err_envelope(
                tool="get_northbound_flow", source="akshare",
                error="接口已下线", error_type="AttributeError",
            ),
        })
        _patch_registry(monkeypatch, stub)
        r = analyze_market_regime()

        assert r.northbound_flow_cny is None
        assert "北向资金取数失败" in r.regime_note
        assert "AttributeError" in r.regime_note

    def test_index_computed_from_mcp_records(self, monkeypatch):
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        stub = StubRegistry({
            "get_index_daily": _index_env(_CLOSES),
            "get_northbound_flow": _nb_env(fresh=False, latest_cny=None),
        })
        _patch_registry(monkeypatch, stub)
        r = analyze_market_regime()

        assert r.index_close == pytest.approx(_CLOSES[-1])
        # 基期 = closes[max(-6, -len(closes))]，即倒数第 6 根。
        # 期望值由 fixture 现算而非写死数字：写死数字时改动 fixture
        # 仍可能"通过"，却验证了错误的基期。
        base = _CLOSES[max(-6, -len(_CLOSES))]
        expected = (_CLOSES[-1] - base) / base * 100
        assert r.index_change_pct_5d == pytest.approx(expected, abs=1e-9)
        assert r.regime == "consolidation"


class TestAgentOnlineIndexFailure:
    """指数取数失败必须标 fallback，不能标 offline_sample。"""

    @pytest.fixture
    def result(self, monkeypatch):
        from agent_platform.finance.market_regime_agent import analyze_market_regime
        from agent_platform.mcp.envelope import err_envelope

        stub = StubRegistry({
            "get_index_daily": err_envelope(
                tool="get_index_daily", source="akshare",
                error="连接超时", error_type="ConnectionError",
            ),
            "get_offline_market_regime": ok_envelope(
                tool="get_offline_market_regime",
                source="offline_sample/offline_sample_data.py",
                data={
                    "regime": "bull", "risk_appetite": "high",
                    "index_close": 3200.0, "index_change_pct_5d": 4.2,
                    "northbound_flow_cny": 9e8, "regime_note": "样例",
                },
            ),
        })
        _patch_registry(monkeypatch, stub)
        return analyze_market_regime()

    def test_status_is_fallback_not_offline_sample(self, result):
        """
        回归：原实现在联网失败时仍标 offline_sample，
        等于把「联网失败」说成「主动离线」，掩盖了真实故障。
        """
        assert result.data_status == "fallback"

    def test_fallback_reason_names_real_cause(self, result):
        assert result.fallback_reason is not None
        assert "ConnectionError" in result.fallback_reason

    def test_source_marks_degradation(self, result):
        assert "降级样例数据" in result.source

    def test_no_northbound_call_after_index_failure(self, monkeypatch, result):
        """指数都拿不到时不必再问北向，避免无意义的外网请求。"""
        assert result.index_close is not None

    def test_insufficient_closes_also_degrades(self, monkeypatch):
        from agent_platform.finance.market_regime_agent import analyze_market_regime

        stub = StubRegistry({
            "get_index_daily": _index_env([3000.0]),   # 只有 1 根，无法算涨跌幅
            "get_offline_market_regime": ok_envelope(
                tool="get_offline_market_regime",
                source="offline_sample/offline_sample_data.py",
                data={
                    "regime": "bull", "risk_appetite": "high",
                    "index_close": 3200.0, "index_change_pct_5d": 4.2,
                    "northbound_flow_cny": 9e8, "regime_note": "样例",
                },
            ),
        })
        _patch_registry(monkeypatch, stub)
        r = analyze_market_regime()
        assert r.data_status == "fallback"
        assert "有效收盘价不足" in (r.fallback_reason or "")
