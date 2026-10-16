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
    # 用户回合附带的图片：data URL（data:image/png;base64,...），由各 provider 序列化成自己的图片块
    images: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        content: Any = self.content
        if self.images and self.role == "user":
            content = [{"type": "text", "text": self.content}] if self.content else []
            content += [{"type": "image_url", "image_url": {"url": u}} for u in self.images]
        d: dict[str, Any] = {"role": self.role, "content": content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return d


def split_data_url(url: str) -> tuple[str, str]:
    """data:image/png;base64,xxxx -> ("image/png", "xxxx")；不是 base64 data URL 时抛 ValueError。"""
    head, sep, data = url.partition(",")
    if not sep or not head.startswith("data:") or not head.endswith(";base64"):
        raise ValueError("图片需为 base64 data URL")
    return head[5:-7], data


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
    """模型调用失败。retryable：临时性错误（限流、服务端 5xx、超时、网络中断、流式中断），
    同一个模型稍后重试大概率能成功；否则（密钥错、参数错、格式无法解析）重试没有意义，直接换下一个模型。"""

    def __init__(
        self, message: str = "", *, retryable: bool = False, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


#: 值得重试的 HTTP 状态码：超时、冲突、限流、服务端错误、过载（Anthropic 529）
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})


def http_error(name: str, exc: Exception, detail: str = "") -> LLMError:
    """把 httpx 异常包装成 LLMError，并标出能否重试、服务端要求的等待时间（Retry-After）。"""
    import httpx

    status = None
    retry_after = None
    retryable = isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retryable = status in RETRYABLE_STATUS
        raw = exc.response.headers.get("retry-after")
        if raw:
            try:
                retry_after = max(0.0, float(raw))
            except ValueError:
                retry_after = None
    msg = f"[{name}] request failed: {exc}" + (f" {detail}" if detail else "")
    return LLMError(msg.strip(), retryable=retryable, status=status, retry_after=retry_after)


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
