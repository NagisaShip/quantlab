"""模型调用的错误信息：服务商响应正文、可行动的中文解释、OpenCode 会话头自动恢复。

背景：OpenCode 网关在缺少 x-opencode-session 时会回 400；httpx 的异常文本只有
「Client error '400 Bad Request' … For more information check: https://developer.mozilla.org/…」，
界面上会把这段 MDN 链接当成降级原因。这里覆盖三件事：
1. http_error 必须带上服务商响应正文；
2. explain_error 把常见失败整理成能行动的一句中文；
3. 网关偶发 MissingSessionID 时自动换新会话标识重试一次。
"""

from __future__ import annotations

import httpx
import pytest

from quant_agent.agents.strategy_lab import _short, _shrink_context
from quant_agent.llm.base import (
    LLMError,
    Message,
    Tool,
    ToolCall,
    explain_error,
    http_error,
    is_context_length_error,
    is_opencode_session_error,
)
from quant_agent.llm.providers import OpenAICompatibleProvider, OpenAIResponsesProvider
from quant_agent.llm.router import AgentConfig, ModelRouter

MDN_400 = (
    "Client error '400 Bad Request' for url 'https://opencode.ai/inference/go/v1/responses'\n"
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400"
)
SESSION_BODY = {
    "type": "error",
    "error": {
        "type": "MissingSessionID",
        "message": "Request is missing x-opencode-session and cannot be routed efficiently.",
    },
}
RESP_OK = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好的"}]}],
    "usage": {"input_tokens": 3, "output_tokens": 2},
}
CHAT_OK = {"choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}], "usage": {}}


def _status_error(status: int, body: dict, path: str = "/responses") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", f"https://opencode.ai/inference/go/v1{path}")
    response = httpx.Response(status, json=body, request=request)
    err = httpx.HTTPStatusError(
        f"Client error '{status} Bad Request' for url '{request.url}'\n"
        f"For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{status}",
        request=request,
        response=response,
    )
    return err


def test_http_error_keeps_provider_body() -> None:
    err = http_error("opencode", _status_error(400, SESSION_BODY))
    assert err.status == 400
    assert "MissingSessionID" in str(err)
    assert is_opencode_session_error(str(err))


def test_explain_error_hides_mdn_link_and_maps_statuses() -> None:
    missing = explain_error(str(http_error("opencode", _status_error(400, SESSION_BODY))))
    assert "x-opencode-session" in missing
    assert "developer.mozilla.org" not in missing

    unauthorized = explain_error(
        "Client error '401 Unauthorized' for url 'https://api.test/v1/chat/completions'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/401"
    )
    assert "密钥" in unauthorized and "developer.mozilla.org" not in unauthorized

    rate_limited = explain_error("[p] request failed: Client error '429 Too Many Requests' for url 'https://x'")
    assert "限流" in rate_limited

    server_error = explain_error("[p] request failed: Client error '503 Service Unavailable' for url 'https://x'")
    assert "稍后重试" in server_error

    network = explain_error("[p] request failed: ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]")
    assert "网络" in network or "连不上" in network


def test_explain_error_for_empty_router_failure() -> None:
    assert "没有可用的模型" in explain_error("所有模型均失败:\n")
    assert "未配置" in explain_error("[a] 未配置 API Key，已跳过")


def test_context_overflow_is_recognized_and_trimmed() -> None:
    assert is_context_length_error("This model's maximum context length is 128000 tokens")
    assert is_context_length_error("error: context_length_exceeded")
    assert is_context_length_error("413 Request Entity Too Large")
    assert "上下文" in explain_error("maximum context length exceeded")
    messages = [
        Message(role="tool", content="x" * 5000, tool_call_id="1"),
        Message(role="assistant", content="结论", reasoning="很长的思考"),
    ]
    _shrink_context(messages)
    assert len(messages[0].content) < 1000 and messages[0].content.endswith("（上下文超限，工具输出已压缩）")
    assert messages[1].reasoning == ""


def test_router_without_any_target_has_actionable_message() -> None:
    router = ModelRouter(AgentConfig(providers={}, routing={"fallback": []}, limits={}, trace={}))
    with pytest.raises(LLMError) as caught:
        router.chat([Message(role="user", content="hi")])
    assert "没有可用的模型" in str(caught.value)


def _responses_provider(handler) -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        "opencode", "https://opencode.ai/inference/go/v1", "k", transport=httpx.MockTransport(handler)
    )


def test_fallback_reason_never_shows_mdn_link() -> None:
    """研究室「规则降级」括号里的原因就是 _short(原始异常)：不能是 MDN 状态码链接。"""
    raw = (
        "所有模型均失败:\n"
        "[opencode] request failed: Client error '400 Bad Request' for url "
        "'https://opencode.ai/inference/go/v1/responses'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400 "
        '{"type":"error","error":{"type":"MissingSessionID","message":"Request is missing x-opencode-session."}}'
    )
    short = _short(raw)
    assert "developer.mozilla.org" not in short
    assert "x-opencode-session" in short


def test_responses_payload_replays_reasoning_for_thinking_models() -> None:
    """思考模型 + 工具回合必须把 reasoning 传回；否则 OpenCode 转发 DeepSeek 会 400。"""
    provider = OpenAIResponsesProvider(
        "opencode", "https://opencode.ai/inference/go/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    assistant = Message(
        role="assistant",
        content="我先查一下。",
        tool_calls=[ToolCall(id="c1", name="workflow_catalog", arguments={"section": "cards"})],
        reasoning="用户要看卡片列表，先调用工具。",
    )
    payload = provider.build_payload(
        [Message(role="user", content="看卡片"), assistant],
        model="deepseek-v4.1-flash",
        temperature=0.2,
        tools=[Tool(name="workflow_catalog", description="d", parameters={"type": "object"})],
        max_tokens=100,
        effort=None,
    )
    kinds = [item.get("type", item.get("role")) for item in payload["input"]]
    assert kinds == ["user", "assistant", "reasoning", "function_call"]
    reasoning_item = payload["input"][2]
    assert reasoning_item["summary"][0]["text"] == "用户要看卡片列表，先调用工具。"

    # 没有摘要也要占位：网关只检查 reasoning 项是否存在
    assistant.reasoning = ""
    payload = provider.build_payload(
        [Message(role="user", content="看卡片"), assistant],
        model="deepseek-v4.1-flash",
        temperature=0.2,
        tools=None,
        max_tokens=100,
        effort=None,
    )
    assert payload["input"][2]["summary"][0]["text"]

    # 非思考网关的普通模型不要凭空插入 reasoning item
    plain = OpenAIResponsesProvider("gw", "https://gw.example/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    plain_payload = plain.build_payload(
        [Message(role="user", content="hi"), assistant],
        model="gpt-5",
        temperature=0.2,
        tools=None,
        max_tokens=100,
        effort=None,
    )
    assert all(item.get("type") != "reasoning" for item in plain_payload["input"])


def test_responses_parse_keeps_raw_reasoning_items() -> None:
    data = {
        "model": "deepseek-v4.1-flash",
        "status": "completed",
        "output": [
            {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "想一下"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好"}]},
            {"type": "function_call", "call_id": "c2", "name": "x", "arguments": "{}"},
        ],
        "usage": {},
    }
    provider = OpenAIResponsesProvider(
        "opencode", "https://opencode.ai/inference/go/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    result = provider._parse_response(data, "deepseek-v4.1-flash", None)
    assert result.provider_blocks and result.provider_blocks[0]["id"] == "rs_1"
    payload = provider.build_payload(
        [
            Message(role="user", content="hi"),
            Message(
                role="assistant",
                content="好",
                tool_calls=[ToolCall(id="c2", name="x", arguments={})],
                reasoning="想一下",
                provider_blocks=result.provider_blocks,
            ),
        ],
        model="deepseek-v4.1-flash",
        temperature=0.2,
        tools=None,
        max_tokens=10,
        effort=None,
    )
    assert payload["input"][2]["id"] == "rs_1"  # 有原始 reasoning 项时原样传回


def test_chat_completions_passes_reasoning_content_for_deepseek_models() -> None:
    provider = OpenAICompatibleProvider(
        "opencode", "https://opencode.ai/inference/go/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="c3", name="x", arguments={})],
        reasoning="先想一下",
    )
    payload_messages = provider._messages([Message(role="user", content="hi"), assistant], "deepseek-v4.1-flash")
    assert payload_messages[1]["reasoning_content"] == "先想一下"
    # 普通 OpenAI 兼容网关不应看到这个 DeepSeek 专用字段
    plain_provider = OpenAICompatibleProvider(
        "gw", "https://gw.example/v1", "k", transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    plain = plain_provider._messages([Message(role="user", content="hi"), assistant], "gpt-4o")
    assert "reasoning_content" not in plain[1]


def test_opencode_missing_session_is_retried_with_a_fresh_session() -> None:
    sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers.get("x-opencode-session", ""))
        if len(sessions) == 1:
            return httpx.Response(400, json=SESSION_BODY)
        return httpx.Response(200, json=RESP_OK)

    out = _responses_provider(handler).chat([Message(role="user", content="你好")], model="deepseek-v4.1-flash")
    assert out.content == "好的"
    assert len(sessions) == 2
    assert all(sessions), "每次请求都必须带 x-opencode-session"
    assert sessions[0] != sessions[1], "恢复重试要换新的会话标识"


def test_opencode_missing_session_recovery_also_works_for_chat_completions() -> None:
    sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers.get("x-opencode-session", ""))
        if len(sessions) == 1:
            return httpx.Response(400, json=SESSION_BODY)
        return httpx.Response(200, json=CHAT_OK)

    provider = OpenAICompatibleProvider(
        "opencode", "https://opencode.ai/inference/go/v1", "k", transport=httpx.MockTransport(handler)
    )
    out = provider.chat([Message(role="user", content="你好")], model="deepseek-v4.1-flash")
    assert out.content == "好的"
    assert len(sessions) == 2 and sessions[0] != sessions[1]
