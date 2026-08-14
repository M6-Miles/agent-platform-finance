from __future__ import annotations

import importlib
import sys
from typing import Callable

from agent_platform.config import Settings, get_settings
from agent_platform.core.agent_runtime import AgentRuntime
from agent_platform.core.mock_llm_provider import MockLLMProvider
from agent_platform.core.tools import RegisteredTool, ToolRegistry


AnalysisToolHandler = Callable[..., str]


def _reload_deepseek() -> type:
    """强制从磁盘重新加载 deepseek_llm_provider 模块。

    解决长生命周期 Python 进程中 sys.modules 缓存旧字节码的问题：
    即使文件已更新，若 Python 进程未重启，sys.modules 里仍是旧定义。
    importlib.reload() 强制重新执行 .py 文件，返回最新的 DeepSeekLLMProvider 类。
    """
    mod_name = "agent_platform.core.deepseek_llm_provider"
    if mod_name in sys.modules:
        importlib.reload(sys.modules[mod_name])
    from agent_platform.core.deepseek_llm_provider import DeepSeekLLMProvider  # noqa: PLC0415
    return DeepSeekLLMProvider


def build_runtime(
    analysis_handler: AnalysisToolHandler,
    settings: Settings | None = None,
) -> AgentRuntime:
    """创建 AgentRuntime，根据 Settings.llm_provider 选择 LLM。"""
    current_settings = settings or get_settings()
    provider_name = current_settings.llm_provider.strip().lower()

    if provider_name == "deepseek":
        api_key = current_settings.deepseek_api_key
        if not api_key:
            raise RuntimeError(
                "LLM_PROVIDER=deepseek 但未设置 DEEPSEEK_API_KEY。"
                "请在 .env 或环境变量中设置。"
            )
        DeepSeekLLMProvider = _reload_deepseek()
        provider = DeepSeekLLMProvider(
            api_key=api_key,
            model=current_settings.deepseek_model,
        )
    elif provider_name == "claude":
        api_key = current_settings.anthropic_api_key
        if not api_key:
            raise RuntimeError(
                "LLM_PROVIDER=claude 但未设置 ANTHROPIC_API_KEY。"
                "请在 .env 或环境变量中设置。"
            )
        from agent_platform.core.claude_llm_provider import ClaudeLLMProvider

        provider = ClaudeLLMProvider(
            api_key=api_key,
            model=current_settings.anthropic_model,
        )
    else:
        provider = MockLLMProvider()

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            name="analyze_security",
            description=(
                "当用户要求分析证券、行情、收益率、均线、MACD、RSI、KDJ、布林带或波动率时调用。"
                "参数 symbol 可填写 DEMO001/DEMO002 或真实 A 股 6 位代码（如 600519）。"
            ),
            handler=analysis_handler,
        )
    )
    return AgentRuntime(provider=provider, tools=registry)
