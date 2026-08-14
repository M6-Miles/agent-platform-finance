"""
FundamentalResult.field_status() 字段级数据状态测试（任务3）
============================================================
验证规则：
- PE/PB/总市值：值非 None 时跟随全局 data_status；为 None 时独立标 unavailable。
- ROE/资产负债率：可独立为 None（接口单独失败），此时标 unavailable，
  全局 data_status 仍可为 live。
- DCF：applicable=True 时状态跟随全局；applicable=False 时 not_applicable；
  dcf=None 时 unavailable。
- to_dict() 必须包含 "field_status" 键且结构正确。
"""
from __future__ import annotations


from agent_platform.finance.fundamental_agent import FundamentalResult


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：构造最小合法 FundamentalResult
# ─────────────────────────────────────────────────────────────────────────────

def _make_result(**overrides: object) -> FundamentalResult:
    defaults: dict = {
        "symbol": "600519",
        "name": "贵州茅台",
        "source": "MCP:get_valuation_metrics",
        "updated_at": "2026-08-10T00:00:00+00:00",
        "pe_ttm": 25.0,
        "pb": 8.5,
        "total_market_value_cny": 2.5e12,
        "roe_pct": 30.5,
        "valuation_signal": "fairly_valued",
        "valuation_note": "测试用",
        "disclaimer": "样本",
        "data_status": "live",
        "fallback_reason": None,
        "debt_to_asset_pct": 20.0,
        "dcf": {
            "applicable": True,
            "intrinsic_value": 1800.0,
            "confidence": "proxy, low confidence",
        },
    }
    defaults.update(overrides)
    return FundamentalResult(**defaults)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# 测试类
# ─────────────────────────────────────────────────────────────────────────────

class TestFieldStatus:

    def test_all_fields_present_and_have_status(self) -> None:
        """field_status() 必须覆盖 pe_ttm/pb/total_market_value_cny/roe_pct/debt_to_asset_pct/dcf。"""
        result = _make_result()
        fs = result.field_status()
        for key in ("pe_ttm", "pb", "total_market_value_cny", "roe_pct", "debt_to_asset_pct", "dcf"):
            assert key in fs, f"field_status 缺少字段 {key!r}"
            assert "status" in fs[key]
            assert "source" in fs[key]

    def test_pe_pb_market_value_follow_global_live_status(self) -> None:
        """PE/PB/总市值均有值时，status 与全局 data_status 一致。"""
        result = _make_result(data_status="live")
        fs = result.field_status()
        assert fs["pe_ttm"]["status"] == "live"
        assert fs["pb"]["status"] == "live"
        assert fs["total_market_value_cny"]["status"] == "live"

    def test_pe_pb_market_value_follow_global_offline_sample_status(self) -> None:
        """离线样例状态也被继承。"""
        result = _make_result(data_status="offline_sample")
        fs = result.field_status()
        assert fs["pe_ttm"]["status"] == "offline_sample"
        assert fs["pb"]["status"] == "offline_sample"

    def test_pe_none_marked_unavailable_independently(self) -> None:
        """PE 为 None 时，pe_ttm 单独标 unavailable，不影响 PB 状态。"""
        result = _make_result(pe_ttm=None, data_status="live")
        fs = result.field_status()
        assert fs["pe_ttm"]["status"] == "unavailable"
        assert fs["pb"]["status"] == "live"          # PB 不受影响

    def test_roe_unavailable_when_none_global_remains_live(self) -> None:
        """ROE 为 None 时 roe_pct 标 unavailable，但全局 data_status 仍为 live。"""
        result = _make_result(roe_pct=None, data_status="live")
        fs = result.field_status()
        assert fs["roe_pct"]["status"] == "unavailable"
        # PE/PB 仍为 live
        assert fs["pe_ttm"]["status"] == "live"

    def test_debt_to_asset_unavailable_when_none(self) -> None:
        """资产负债率为 None 时独立标 unavailable。"""
        result = _make_result(debt_to_asset_pct=None, data_status="live")
        fs = result.field_status()
        assert fs["debt_to_asset_pct"]["status"] == "unavailable"

    def test_dcf_applicable_status_follows_global(self) -> None:
        """DCF 适用时，status 跟随全局 data_status。"""
        result = _make_result(
            data_status="live",
            dcf={"applicable": True, "intrinsic_value": 1800.0},
        )
        fs = result.field_status()
        assert fs["dcf"]["status"] == "live"

    def test_dcf_not_applicable_when_flag_false(self) -> None:
        """DCF 不适用时（applicable=False），status='not_applicable'。"""
        result = _make_result(
            dcf={"applicable": False, "reason_not_applicable": "数据不足"},
        )
        fs = result.field_status()
        assert fs["dcf"]["status"] == "not_applicable"

    def test_dcf_none_marked_unavailable(self) -> None:
        """DCF 计算完全失败（dcf=None）时，status='unavailable'。"""
        result = _make_result(dcf=None)
        fs = result.field_status()
        assert fs["dcf"]["status"] == "unavailable"

    def test_to_dict_contains_field_status(self) -> None:
        """to_dict() 输出必须包含 'field_status' 键，前端可直接消费。"""
        result = _make_result()
        d = result.to_dict()
        assert "field_status" in d
        fs = d["field_status"]
        assert isinstance(fs, dict)
        assert "pe_ttm" in fs

    def test_field_status_source_matches_result_source(self) -> None:
        """field_status 中每个字段的 source 必须和 FundamentalResult.source 一致。"""
        src = "MCP:get_valuation_metrics/test"
        result = _make_result(source=src)
        fs = result.field_status()
        for key, info in fs.items():
            assert info["source"] == src, (
                f"字段 {key!r} 的 source={info['source']!r} 与预期 {src!r} 不符"
            )
