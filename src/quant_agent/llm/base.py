"""LLM 抽象层：统一消息、工具、结果与 Provider 协议。

任何 provider（OpenAI 兼容、Anthropic、本地）都实现 LLMProvider，
上层 Agent 只依赖这里的类型，不关心底层是哪家。
"""

from __future__ import annotations

import json
import re
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
    """把 httpx 异常包装成 LLMError，并标出能否重试、服务端要求的等待时间（Retry-After）。

    服务商的错误正文（通常是 JSON 里的 message）一并带上：只显示 httpx 的
    ``Client error '400 Bad Request' … For more information check: https://developer.mozilla.org/…``
    对用户没有任何帮助，真正的原因往往在响应正文里。
    """
    import httpx

    status = None
    retry_after = None
    retryable = isinstance(exc, (httpx.TimeoutException, httpx.TransportError))
    body = ""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retryable = status in RETRYABLE_STATUS
        raw = exc.response.headers.get("retry-after")
        if raw:
            try:
                retry_after = max(0.0, float(raw))
            except ValueError:
                retry_after = None
        try:
            body = (exc.response.text or "").strip()
        except Exception:  # 流式响应未读完 / 已被关闭
            body = ""
    if not detail and body:
        detail = body[:800]
    msg = f"[{name}] request failed: {exc}" + (f" {detail}" if detail else "")
    return LLMError(msg.strip(), retryable=retryable, status=status, retry_after=retry_after)


#: httpx 的 HTTPStatusError 文本里带这段“去 MDN 看状态码”的尾巴，对用户没有信息量
_MDN_LINK = re.compile(r"https?://developer\.mozilla\.org/\S+")
_JSON_MESSAGE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)+)"')
_JSON_TYPE = re.compile(r'"type"\s*:\s*"((?:[^"\\]|\\.)+)"')
_HTTP_STATUS = re.compile(r"(?:Client error|Server error) '(\d{3})|HTTP (\d{3})|status(?: code)?\D{0,4}(\d{3})", re.I)
_NET_ERRORS = ("timeout", "timed out", "connect", "connection", "unexpected_eof", "ssl", "eof occurred", "network")


def _clean_reason(reason: str) -> str:
    """把多行错误压成一行，去掉 MDN 链接这类噪声；保留服务商原文。"""
    text = (reason or "").replace("\r", "\n")
    text = _MDN_LINK.sub("", text)
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def _json_field(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    raw = match.group(1)
    try:
        return str(json.loads('{"message": "' + raw + '"}')["message"]).strip()
    except (ValueError, KeyError):
        return raw.strip()


def _status_of(text: str) -> int | None:
    match = _HTTP_STATUS.search(text)
    if not match:
        return None
    for group in match.groups():
        if group:
            try:
                return int(group)
            except ValueError:
                return None
    return None


def is_opencode_session_error(reason: str) -> bool:
    """OpenCode 网关要求请求头带 x-opencode-session；缺了它会直接回 400。"""
    low = (reason or "").lower()
    return "missingsessionid" in low or ("opencode" in low and "session" in low and "400" in low)


def is_context_length_error(reason: str) -> bool:
    low = (reason or "").lower()
    return any(
        k in low
        for k in (
            "context length",
            "context_length",
            "maximum context",
            "context window",
            "too many tokens",
            "too large",
            "request entity too large",
            "payload too large",
            "body too large",
            "上下文长度",
            "超出上下文",
            "上下文超限",
        )
    )


def explain_error(reason: str) -> str:
    """把路由器的原始异常整理成一句用户能行动的中文原因（去掉 MDN/堆栈噪声）。

    例：
    - ``Client error '400 Bad Request' … For more information check: …`` + ``{"message":"MissingSessionID"}``
      → ``OpenCode 网关缺少会话标识（x-opencode-session）``
    - 401 → ``模型密钥无效或没有权限（设置 → 模型 里重新保存密钥）``
    - 429 → ``模型服务限流，稍后重试``
    - 网络超时 → ``连不上模型服务（网络中断或超时）``
    """
    raw = (reason or "").strip()
    if not raw:
        return "模型不可用"
    if "未配置 API Key" in raw:
        return "未配置任何模型密钥（设置 → 模型）"
    text = _clean_reason(raw)
    if not text:
        return "模型不可用"
    lowered = text.lower()
    service = _json_field(_JSON_TYPE, text)
    message = _json_field(_JSON_MESSAGE, text) or _json_field(_JSON_TYPE, text)
    status = _status_of(text)

    if is_opencode_session_error(text):
        return "模型网关缺少会话标识（x-opencode-session），已自动重试；仍失败请稍后再试"
    if is_context_length_error(text):
        return "这次对话太长，超出模型上下文（可先「压缩上下文」再重试）"
    if "未配置" in text or "no api key" in lowered or "missing api key" in lowered:
        return "未配置任何模型密钥（设置 → 模型）"
    if "所有模型均失败" in text:
        if len(text) <= len("所有模型均失败:"):
            return "没有可用的模型：请在 设置 → 模型 添加服务商并选择路由"
        rest = text.split("所有模型均失败", 1)[-1].lstrip(":：").strip()
        return explain_error(rest) if rest else "没有可用的模型：请在 设置 → 模型 添加服务商并选择路由"
    if status in (401, 403):
        tail = f"（{message}）" if message and message not in text[:40] else ""
        return f"模型密钥无效或没有权限{tail}（设置 → 模型 里重新保存密钥）"
    if status == 404:
        return "模型接口或模型名不存在（检查 API 地址与模型名）"
    if status == 413:
        return "请求内容过大被模型服务拒绝（先压缩上下文再重试）"
    if status == 429:
        return "模型服务限流，稍后重试"
    if status and 500 <= status < 600:
        return f"模型服务暂时异常（HTTP {status}），稍后重试"
    if status == 400:
        detail = message or ""
        if service and message and service.lower() not in ("error", "invalid_request_error"):
            detail = f"{service}：{message}"
        return f"模型服务拒绝了这次请求（HTTP 400）{('：' + detail) if detail else ''}"
    if any(k in lowered for k in _NET_ERRORS):
        return "连不上模型服务（网络中断或超时），可稍后重试"
    # 兜底：给出去掉 URL 后的一行原文，限制长度
    return text[:160]


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
