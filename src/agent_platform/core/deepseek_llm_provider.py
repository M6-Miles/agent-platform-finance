"""DeepSeek API 实现 LLMProvider 协议（httpx 直联，强制 UTF-8 编码）。

改用 httpx 直接构造请求，对请求体显式执行
  json.dumps(payload, ensure_ascii=False).encode("utf-8")
彻底消除 Windows 环境下系统默认 ascii codec 导致的
  'ascii' codec can't encode characters ... ordinal not in range(128)
问题，无需依赖 openai 库的内部编码行为。
"""
from __future__ import annotations

import json
import logging
from typing import Sequence

import httpx

from agent_platform.core.llm_provider import (
    ChatMessage,
    ModelReply,
    ToolCall,
    ToolDescription,
    LLMAuthenticationError,
    LLMInvalidRequestError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
)

log = logging.getLogger(__name__)


class DeepSeekLLMProvider:
    """通过 OpenAI 兼容接口调用 DeepSeek 模型（httpx 直联，确保 UTF-8 编码）。"""

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        max_tokens: int = 4096,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._endpoint = f"{self._base_url}/v1/chat/completions"
        # Content-Type 显式声明 charset=utf-8，避免服务端或中间件误判编码
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }

    @property
    def name(self) -> str:
        return f"deepseek/{self._model}"

    @property
    def supports_json_mode(self) -> bool:
        """标记该适配器支持 OpenAI 兼容的 JSON 输出约束。"""
        # DeepSeek 的历史 ``deepseek-chat`` 别名在部分账户/网关上会对
        # response_format 返回 400；只有明确的 V4 模型才启用该参数。
        return self._model.startswith("deepseek-v4")

    def generate(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDescription],
        response_format: dict[str, str] | None = None,
    ) -> ModelReply:
        if not messages:
            return ModelReply(text="请先输入一个问题。")

        _BASE_SYSTEM = (
            "你是一个专业的证券金融分析助手。"
            "你可以调用 analyze_security 工具来获取行情数据和技术指标。"
            "所有分析仅供参考，不构成投资建议。"
            "请用中文回复，分析要数据支撑，语言简洁专业。"
            "⚠️ 重要：若用户提问涉及具体股票代码，必须先调用工具获取实时数据，"
            "不得凭训练记忆直接给出公司名称（训练数据可能已过期）。"
        )

        # 从 user message 中提取 AkShare 注入的实时股票信息，迁移到 system prompt
        # 以获得更高的注意力权重，防止 DeepSeek 用过期训练数据覆盖
        _INJECT_MARKER = "【AkShare 实时行情数据"
        _INJECT_SEP = "用户提问：\n"
        stock_context_block = ""
        for msg in reversed(messages):
            if msg.role == "user" and _INJECT_MARKER in msg.content:
                sep_idx = msg.content.find(_INJECT_SEP)
                if sep_idx > 0:
                    stock_context_block = msg.content[:sep_idx].strip()
                break

        if stock_context_block:
            system_prompt = (
                _BASE_SYSTEM + "\n\n"
                + stock_context_block + "\n"
                "以上股票名称已由实时接口确认，回答时必须使用这些名称，"
                "禁止使用训练数据中的任何其他名称。"
            )
        else:
            system_prompt = _BASE_SYSTEM

        openai_msgs: list[dict] = [
            {"role": "system", "content": system_prompt}
        ]
        for msg in messages:
            role = "assistant" if msg.role == "assistant" else "user"
            content = msg.content
            # 对包含注入前缀的 user 消息，只向 LLM 发送纯净问题部分（前缀已迁移到 system）
            if msg.role == "user" and _INJECT_MARKER in content:
                parts = content.split(_INJECT_SEP, 1)
                content = parts[1] if len(parts) > 1 else content
            openai_msgs.append({"role": role, "content": content})

        payload: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": openai_msgs,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "symbol": {
                                    "type": "string",
                                    "description": "证券代码（如 DEMO001, 600519）",
                                },
                            },
                            "required": ["symbol"],
                        },
                    },
                }
                for t in tools
            ]

        try:
            # ── 核心修复 ──────────────────────────────────────────────────────
            # ensure_ascii=False：中文直接输出 UTF-8，而非 \uXXXX 转义序列
            # .encode("utf-8")  ：显式转字节，彻底绕过 Windows ascii codec
            body: bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            log.debug(
                "DeepSeek request to %s, body_size=%d bytes",
                self._endpoint,
                len(body),
            )

            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    self._endpoint,
                    content=body,
                    headers=self._headers,
                )
                resp.raise_for_status()

            data: dict = resp.json()

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.warning("DeepSeek HTTP failure status=%d", status)
            if status in (401, 403):
                raise LLMAuthenticationError("LLM 凭证无效或无权限") from exc
            if status == 429:
                raise LLMRateLimitError("LLM 请求频率受限") from exc
            if 500 <= status <= 599:
                raise LLMServerError("LLM 服务暂时不可用") from exc
            raise LLMInvalidRequestError("LLM 请求无效") from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            log.warning("DeepSeek request timed out")
            raise LLMNetworkError("LLM 网络请求超时") from exc
        except (httpx.NetworkError, ConnectionError) as exc:
            log.warning("DeepSeek network failure type=%s", type(exc).__name__)
            raise LLMNetworkError("LLM 网络连接失败") from exc
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("DeepSeek response parsing failed")
            raise LLMInvalidRequestError("LLM 响应格式无效") from exc
        except Exception as exc:
            log.warning("DeepSeek unexpected failure type=%s", type(exc).__name__)
            raise LLMInvalidRequestError("LLM 调用失败") from exc

        # ── 解析响应 ──────────────────────────────────────────────────────────
        choices = data.get("choices") or []
        if not choices:
            raise LLMInvalidRequestError("LLM 响应缺少有效结果")

        msg_data: dict = choices[0].get("message", {})

        tool_calls: list[ToolCall] = []
        for tc in msg_data.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            tool_calls.append(
                ToolCall(
                    name=tc["function"]["name"],
                    arguments=args,
                )
            )

        text: str = msg_data.get("content") or ""
        if not text and tool_calls:
            text = "正在调用工具分析…"

        usage = data.get("usage") or {}
        return ModelReply(
            text=text,
            tool_calls=tuple(tool_calls),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )
