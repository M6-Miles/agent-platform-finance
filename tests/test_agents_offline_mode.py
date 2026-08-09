"""测试 Agent 在离线模式（force_offline=True）下使用确定性样例数据"""
from agent_platform.finance.fundamental_agent import analyze_fundamental
from agent_platform.finance.industry_agent import analyze_industry
from agent_platform.finance.market_regime_agent import analyze_market_regime


class TestAgentsOfflineMode:
    """验证 Agent 在离线模式下返回完整样例数据"""

    def test_fundamental_agent_offline_demo001(self):
        """基本面 Agent 离线模式返回 DEMO001 完整数据"""
        result = analyze_fundamental("DEMO001", force_offline=True)

        assert result.symbol == "DEMO001"
        assert result.name == "创新科技股份"
        assert result.pe_ttm == 25.6
        assert result.pb == 3.2
        assert result.total_market_value_cny == 1250000000.0
        assert result.roe_pct == 18.5
        assert result.valuation_signal == "fairly_valued"
        assert "PE=25.6" in result.valuation_note
        assert "内置样例数据" in result.source
        assert result.disclaimer != ""
        assert result.data_status == "offline_sample"
        assert result.fallback_reason is None

    def test_industry_agent_offline_demo001(self):
        """行业 Agent 离线模式返回 DEMO001 完整数据"""
        result = analyze_industry("DEMO001", force_offline=True)

        assert result.symbol == "DEMO001"
        assert result.industry_name == "软件服务"
        assert result.prosperity_signal == "booming"
        assert result.fund_flow_3d_cny == 850000000.0
        assert len(result.top_stocks) == 5
        assert result.top_stocks[0]["rank"] == 1
        assert "内置样例数据" in result.source
        assert result.disclaimer != ""
        assert result.data_status == "offline_sample"
        assert result.fallback_reason is None

    def test_market_regime_agent_offline(self):
        """市场状态 Agent 离线模式返回完整数据"""
        result = analyze_market_regime(force_offline=True)

        assert result.regime in ["bull", "bear", "consolidation"]
        assert result.risk_appetite in ["high", "medium", "low"]
        assert result.index_code == "sh000001"
        assert result.index_close is not None
        assert result.index_change_pct_5d is not None
        assert result.northbound_flow_cny is not None
        assert "内置样例数据" in result.source
        assert result.disclaimer != ""
        assert result.data_status == "offline_sample"
        assert result.fallback_reason is None

    def test_offline_mode_deterministic(self):
        """离线模式返回确定性结果"""
        result1 = analyze_fundamental("DEMO001", force_offline=True)
        result2 = analyze_fundamental("DEMO001", force_offline=True)

        assert result1.pe_ttm == result2.pe_ttm
        assert result1.pb == result2.pb
        assert result1.valuation_signal == result2.valuation_signal

    def test_offline_mode_no_none_fields(self):
        """离线模式不应返回大量 None 字段"""
        fund = analyze_fundamental("DEMO001", force_offline=True)
        assert fund.pe_ttm is not None
        assert fund.pb is not None
        assert fund.total_market_value_cny is not None
        assert fund.roe_pct is not None
        assert fund.valuation_signal != "unknown"

        ind = analyze_industry("DEMO001", force_offline=True)
        assert ind.industry_name != "未知行业"
        assert ind.fund_flow_3d_cny is not None
        assert len(ind.top_stocks) > 0

        market = analyze_market_regime(force_offline=True)
        assert market.index_close is not None
        assert market.index_change_pct_5d is not None

    def test_all_demo_codes_offline(self):
        """所有 DEMO 代码在离线模式下有完整数据"""
        for code in ["DEMO001", "DEMO002", "DEMO003", "DEMO004"]:
            fund = analyze_fundamental(code, force_offline=True)
            assert fund.pe_ttm is not None
            assert fund.pb is not None
            assert fund.valuation_signal != "unknown"

            ind = analyze_industry(code, force_offline=True)
            assert ind.industry_name != "未知行业"
            assert ind.prosperity_signal != "unknown"

    def test_000001_offline(self):
        """000001 在离线模式下返回样例数据"""
        fund = analyze_fundamental("000001", force_offline=True)
        assert fund.name == "平安银行"
        assert fund.pe_ttm == 6.8
        assert "内置样例数据" in fund.source
        assert fund.data_status == "offline_sample"
        assert fund.fallback_reason is None

        ind = analyze_industry("000001", force_offline=True)
        assert ind.industry_name == "银行"
        assert "内置样例数据" in ind.source
        assert ind.data_status == "offline_sample"
        assert ind.fallback_reason is None

    def test_offline_source_labels_clear(self):
        """离线模式的 source 字段明确标注"""
        fund = analyze_fundamental("DEMO001", force_offline=True)
        assert "内置样例数据" in fund.source
        assert "offline" in fund.source.lower() or "sample" in fund.source.lower()

        # 不应该伪装为 AkShare
        assert "akshare" not in fund.source.lower()
        assert "真实" not in fund.source

    def test_to_dict_includes_all_fields(self):
        """to_dict() 包含所有定义的字段"""
        fund = analyze_fundamental("DEMO001", force_offline=True)
        data = fund.to_dict()

        required_fields = [
            "symbol", "name", "source", "updated_at",
            "pe_ttm", "pb", "total_market_value_cny", "roe_pct",
            "valuation_signal", "valuation_note", "disclaimer",
            "data_status", "fallback_reason"
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

        ind = analyze_industry("DEMO001", force_offline=True)
        ind_data = ind.to_dict()

        ind_required = [
            "symbol", "industry_name", "source", "updated_at",
            "prosperity_signal", "prosperity_note", "top_stocks",
            "fund_flow_3d_cny", "disclaimer",
            "data_status", "fallback_reason"
        ]
        for field in ind_required:
            assert field in ind_data, f"Missing field: {field}"

        market = analyze_market_regime(force_offline=True)
        market_data = market.to_dict()

        market_required = [
            "regime", "risk_appetite", "index_code", "index_close",
            "index_change_pct_5d", "northbound_flow_cny",
            "regime_note", "source", "updated_at", "disclaimer",
            "data_status", "fallback_reason"
        ]
        for field in market_required:
            assert field in market_data, f"Missing field: {field}"
