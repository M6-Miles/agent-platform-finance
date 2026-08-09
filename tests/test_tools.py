"""ToolRegistry 单元测试。"""
from __future__ import annotations

import pytest

from agent_platform.core.tools import RegisteredTool, ToolRegistry


def handler_ok(symbol: str) -> str:
    return f"分析完成：{symbol}"


def handler_error(symbol: str) -> str:
    raise RuntimeError("模拟执行失败")


class TestToolRegistry:
    def test_descriptions_returns_all(self) -> None:
        reg = ToolRegistry()
        reg.register(RegisteredTool("t1", "desc 1", handler_ok))
        reg.register(RegisteredTool("t2", "desc 2", handler_ok))
        descs = reg.descriptions()
        assert len(descs) == 2

    def test_execute_returns_output(self) -> None:
        reg = ToolRegistry()
        reg.register(RegisteredTool("test", "desc", handler_ok))
        result = reg.execute("test", {"symbol": "DEMO001"})
        assert not result.is_error
        assert "DEMO001" in result.output

    def test_execute_unknown_tool_returns_error(self) -> None:
        reg = ToolRegistry()
        result = reg.execute("nonexistent", {})
        assert result.is_error
        assert "未找到工具" in result.output

    def test_execute_handler_exception_returns_error(self) -> None:
        reg = ToolRegistry()
        reg.register(RegisteredTool("bad", "desc", handler_error))
        result = reg.execute("bad", {"symbol": "X"})
        assert result.is_error
        assert "工具执行失败" in result.output

    def test_register_duplicate_raises(self) -> None:
        reg = ToolRegistry()
        reg.register(RegisteredTool("dup", "desc", handler_ok))
        with pytest.raises(ValueError, match="已注册"):
            reg.register(RegisteredTool("dup", "desc", handler_ok))
