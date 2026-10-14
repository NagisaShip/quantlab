"""LLM 抽象层：统一消息、工具、结果与 Provider 协议。

任何 provider（OpenAI 兼容、Anthropic、本地）都实现 LLMProvider，
上层 Agent 只依赖这里的类型，不关心底层是哪家。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class Message:
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 推理模型在工具回合里需要原样回传的内容：Anthropic 的 thinking 块、DeepSeek 的 reasoning_content
    provider_blocks: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return d


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        import json

        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)},
        }


@dataclass
class Tool:
    """一个可被 LLM 调用的工具。handler 由业务层注入。"""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Any = None

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ChatResult:
    content: str
    model: str
    provider: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None
    thinking: str = ""  # 可展示的思考摘要（模型提供时）
    provider_blocks: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""


class LLMError(RuntimeError):
    pass


# 思考努力程度：平台统一的四档，由各 provider 映射到自己的参数
EFFORTS = ("low", "medium", "high", "max")

# on_delta(kind, text)：kind 为 "text" 或 "thinking"，流式输出时逐段回调
DeltaFn = Any


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 0.2,
        tools: list[Tool] | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
    ) -> ChatResult: ...

    def embed(self, texts: list[str], *, model: str) -> list[list[float]]: ...
