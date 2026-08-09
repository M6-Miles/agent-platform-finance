"""测试 Provider 路由逻辑：auto 模式应使用配置的 Provider，offline 模式应使用 Sample。"""

import pytest
from unittest.mock import patch, MagicMock

from agent_platform.finance.analysis import analyze_security
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider


class TestProviderRouting:
    """验证 analyze_security() 的 Provider 选择逻辑。"""

    def test_offline_mode_uses_sample_provider(self):
        """offline 模式：显式传入 SampleMarketDataProvider 应返回样例数据。"""
        provider = SampleMarketDataProvider()
        result = analyze_security("DEMO001", provider=provider)

        assert result.symbol == "DEMO001"
        assert "样例数据" in result.source or "generate_sample_data" in result.source
        # 样例数据中 DEMO001 的名称是"创新科技股份"
        assert result.name is not None and len(result.name) > 0

    def test_auto_mode_uses_factory_default(self):
        """auto 模式：provider=None 应调用 create_market_data_provider()。"""
        with patch('agent_platform.finance.provider_factory.create_market_data_provider') as mock_factory:
            mock_provider = MagicMock()
            mock_provider.get_price_history.return_value = SampleMarketDataProvider().get_price_history("DEMO001")
            mock_factory.return_value = mock_provider

            analyze_security("DEMO001", provider=None)

            # 验证 factory 被调用了一次
            mock_factory.assert_called_once()
            # 验证返回的 provider 被使用
            mock_provider.get_price_history.assert_called_once()

    def test_explicit_provider_overrides_factory(self):
        """显式传入 provider 参数应优先使用，不调用 factory。"""
        with patch('agent_platform.finance.provider_factory.create_market_data_provider') as mock_factory:
            explicit_provider = SampleMarketDataProvider()
            result = analyze_security("DEMO001", provider=explicit_provider)

            # factory 不应被调用
            mock_factory.assert_not_called()
            assert result.symbol == "DEMO001"

    @pytest.mark.skipif(
        True,  # 默认跳过，需要真实网络环境
        reason="需要 AkShare 网络访问，CI 环境中跳过"
    )
    def test_real_akshare_with_000001(self):
        """真实 AkShare 测试：000001（平安银行）应返回真实数据。

        此测试需要：
        1. 环境变量 MARKET_DATA_PROVIDER=akshare
        2. 网络连通
        3. AkShare API 可用

        运行方式：pytest -k test_real_akshare_with_000001 -v
        """
        import os
        os.environ["MARKET_DATA_PROVIDER"] = "akshare"

        # 重新加载 factory 以应用环境变量
        from importlib import reload
        import agent_platform.finance.provider_factory as pf
        reload(pf)

        result = analyze_security("000001", provider=None)

        assert result.symbol == "000001"
        assert "AkShare" in result.source or "真实" in result.source
        assert result.latest_close > 0
        # 真实股票名称应包含"平安"
        assert "平安" in result.name or "000001" in result.name
