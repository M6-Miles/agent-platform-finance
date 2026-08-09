from __future__ import annotations


class MarketDataError(RuntimeError):
    """行情数据错误基类，可安全展示给本地演示用户。"""


class MarketDataDependencyError(MarketDataError):
    """缺少可选数据源依赖。"""


class MarketDataUnavailableError(MarketDataError):
    """外部行情服务暂时不可用。"""


class InvalidSecuritySymbolError(MarketDataError):
    """证券代码格式不正确或数据源未返回该证券。"""
