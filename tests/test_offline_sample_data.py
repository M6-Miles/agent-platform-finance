"""测试离线样例数据的完整性和确定性"""
from agent_platform.finance.offline_sample_data import (
    get_sample_fundamental,
    get_sample_industry,
    get_sample_market_regime,
)


class TestOfflineSampleData:
    """验证离线样例数据提供完整且有意义的数据"""

    def test_demo001_fundamental_complete(self):
        """DEMO001 基本面数据完整性"""
        data = get_sample_fundamental("DEMO001")

        assert data["name"] == "创新科技股份"
        assert data["pe_ttm"] == 25.6
        assert data["pb"] == 3.2
        assert data["total_market_value_cny"] == 1250000000.0
        assert data["roe_pct"] == 18.5
        assert data["valuation_signal"] == "fairly_valued"
        assert "PE=25.6" in data["valuation_note"]

    def test_demo001_industry_complete(self):
        """DEMO001 行业数据完整性"""
        data = get_sample_industry("DEMO001")

        assert data["industry_name"] == "软件服务"
        assert data["prosperity_signal"] == "booming"
        assert data["fund_flow_3d_cny"] == 850000000.0
        assert len(data["top_stocks"]) == 5
        assert data["top_stocks"][0]["rank"] == 1
        assert "code" in data["top_stocks"][0]
        assert "name" in data["top_stocks"][0]
        assert "change_pct" in data["top_stocks"][0]

    def test_market_regime_default_complete(self):
        """市场状态数据完整性"""
        data = get_sample_market_regime()

        assert data["regime"] in ["bull", "bear", "consolidation"]
        assert data["risk_appetite"] in ["high", "medium", "low"]
        assert data["index_code"] == "sh000001"
        assert data["index_close"] is not None
        assert data["index_change_pct_5d"] is not None
        assert data["northbound_flow_cny"] is not None
        assert len(data["regime_note"]) > 0

    def test_000001_has_sample_data(self):
        """000001 有完整样例数据（用于离线模式）"""
        fund = get_sample_fundamental("000001")
        assert fund["name"] == "平安银行"
        assert fund["pe_ttm"] == 6.8

        ind = get_sample_industry("000001")
        assert ind["industry_name"] == "银行"
        assert ind["prosperity_signal"] in ["booming", "normal", "sluggish"]

    def test_600519_has_sample_data(self):
        """600519 有完整样例数据"""
        fund = get_sample_fundamental("600519")
        assert fund["name"] == "贵州茅台"
        assert fund["pe_ttm"] > 0

        ind = get_sample_industry("600519")
        assert ind["industry_name"] == "白酒"

    def test_unknown_symbol_returns_default(self):
        """未知代码返回默认数据"""
        fund = get_sample_fundamental("TEST999")
        assert fund["name"] == "样例公司TEST999"
        assert fund["pe_ttm"] == 20.0

        ind = get_sample_industry("TEST999")
        assert ind["industry_name"] == "综合行业"

    def test_sample_data_deterministic(self):
        """样例数据是确定性的（多次调用返回相同结果）"""
        data1 = get_sample_fundamental("DEMO001")
        data2 = get_sample_fundamental("DEMO001")

        assert data1 == data2

    def test_market_regime_scenarios(self):
        """市场状态支持多个场景"""
        bull = get_sample_market_regime(scenario="bull_market")
        assert bull["regime"] == "bull"
        assert bull["risk_appetite"] == "high"
        assert bull["index_change_pct_5d"] > 3.0

        bear = get_sample_market_regime(scenario="bear_market")
        assert bear["regime"] == "bear"
        assert bear["risk_appetite"] == "low"
        assert bear["index_change_pct_5d"] < -3.0

    def test_all_demo_codes_covered(self):
        """所有 DEMO 代码都有完整数据"""
        for code in ["DEMO001", "DEMO002", "DEMO003", "DEMO004"]:
            fund = get_sample_fundamental(code)
            assert fund["pe_ttm"] is not None
            assert fund["pb"] is not None
            assert fund["roe_pct"] is not None
            assert fund["valuation_signal"] != "unknown"

            ind = get_sample_industry(code)
            assert ind["industry_name"] != "未知行业"
            assert ind["prosperity_signal"] != "unknown"
            assert len(ind["top_stocks"]) > 0
