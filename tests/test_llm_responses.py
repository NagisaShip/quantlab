"""OpenAI Responses 协议的 provider：请求体转换、非流式解析、流式事件与工具回合。"""

from __future__ import annotations

import json

import httpx

from quant_agent.llm.base import Message, Tool, ToolCall
from quant_agent.llm.providers import OpenAIResponsesProvider
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def test_responses_payload_and_parse_with_tool_round():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "gpt-5",
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "先看数据"}]},
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "好的"}]},
                    {"type": "function_call", "call_id": "c9", "name": "run_backtest", "arguments": '{"days": 30}'},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    p = OpenAIResponsesProvider("gw", "https://gw.example/v1", "k", transport=httpx.MockTransport(handler))
    tool = Tool(name="run_backtest", description="d", parameters={"type": "object", "properties": {}})
    history = [
        Message(role="system", content="你是研究员"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="c0", name="x", arguments={"a": 1})]),
        Message(role="tool", content="{}", tool_call_id="c0"),
    ]
    r = p.chat(history, model="gpt-5", tools=[tool], effort="max", max_tokens=100)

    pl = seen["payload"]
    assert seen["url"] == "https://gw.example/v1/responses"
    assert pl["instructions"] == "你是研究员"
    assert pl["input"] == [
        {"role": "user", "content": "hi"},
        {"type": "function_call", "call_id": "c0", "name": "x", "arguments": '{"a": 1}'},
        {"type": "function_call_output", "call_id": "c0", "output": "{}"},
    ]
    assert pl["reasoning"]["effort"] == "high" and "temperature" not in pl
    assert pl["max_output_tokens"] == 100
    want = {"type": "function", "name": "run_backtest", "description": "d", "parameters": tool.parameters}
    assert pl["tools"][0] == want
    assert r.content == "好的" and r.thinking == "先看数据"
    assert r.tool_calls[0].id == "c9" and r.tool_calls[0].arguments == {"days": 30}
    assert r.stop_reason == "completed"


def test_responses_stream_deltas_and_final_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        final = {
            "model": "deepseek-v4.1-flash",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "你好呀"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }
        body = _sse(
            [
                {"type": "response.created", "response": {}},
                {"type": "response.output_text.delta", "delta": "你好"},
                {"type": "response.output_text.delta", "delta": "呀"},
                {"type": "response.completed", "response": final},
            ]
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    p = OpenAIResponsesProvider("gw", "https://gw.example/v1", "k", transport=httpx.MockTransport(handler))
    got: list[tuple[str, str]] = []
    r = p.chat(
        [Message(role="user", content="hi")], model="deepseek-v4.1-flash", on_delta=lambda k, t: got.append((k, t))
    )
    assert got == [("text", "你好"), ("text", "呀")]
    assert r.content == "你好呀" and r.usage == {"input_tokens": 3, "output_tokens": 2}
    assert r.model == "deepseek-v4.1-flash"


def test_router_builds_responses_client_for_that_type():
    gw = ProviderConfig(name="gw", type="openai_responses", base_url="https://gw.example/v1", api_key="k", models=["m"])
    cfg = AgentConfig(providers={"gw": gw}, routing={"default": "gw/m"}, limits={}, trace={})
    assert isinstance(ModelRouter(cfg)._client("gw"), OpenAIResponsesProvider)
