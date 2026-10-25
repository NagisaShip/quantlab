"""provider 层：思考努力程度映射、流式输出、推理内容回传。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from quant_agent.llm.base import Message, Tool, ToolCall
from quant_agent.llm.providers import AnthropicProvider, OpenAICompatibleProvider
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig, context_window


def _sse(events: list[dict], *, done: bool = False) -> bytes:
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    if done:
        body += "data: [DONE]\n\n"
    return body.encode()


def test_openai_stream_collects_text_reasoning_and_tool_calls():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        chunks = [
            {"model": "deepseek-reasoner", "choices": [{"delta": {"reasoning_content": "先看"}}]},
            {"choices": [{"delta": {"reasoning_content": "均线"}}]},
            {"choices": [{"delta": {"content": "好的，"}}]},
            {"choices": [{"delta": {"content": "我来回测。"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "c1", "function": {"name": "run_backtest", "arguments": '{"cost_'}}
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'scale": 2}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(chunks, done=True))

    p = OpenAICompatibleProvider("deepseek", "https://api.deepseek.com/v1", "k", transport=httpx.MockTransport(handler))
    got: list[tuple[str, str]] = []
    history = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            reasoning="上一轮的思考",
            tool_calls=[ToolCall(id="c0", name="x", arguments={})],
        ),
        Message(role="tool", content="{}", tool_call_id="c0"),
    ]
    r = p.chat(history, model="deepseek-reasoner", effort="high", on_delta=lambda k, t: got.append((k, t)))
    assert r.content == "好的，我来回测。"
    assert r.thinking == "先看均线"
    assert r.tool_calls[0].name == "run_backtest" and r.tool_calls[0].arguments == {"cost_scale": 2}
    assert ("thinking", "先看") in got and ("text", "好的，") in got
    assert seen["payload"]["stream"] is True
    # DeepSeek 的工具回合要把 reasoning_content 带回去
    assert seen["payload"]["messages"][1]["reasoning_content"] == "上一轮的思考"


def test_openai_reasoning_models_get_reasoning_effort_and_no_temperature():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "o4-mini", "choices": [{"message": {"content": "ok"}}]})

    p = OpenAICompatibleProvider("openai", "https://api.openai.com/v1", "k", transport=httpx.MockTransport(handler))
    p.chat([Message(role="user", content="hi")], model="o4-mini", effort="max")
    assert seen["payload"]["reasoning_effort"] == "high"
    assert "temperature" not in seen["payload"]
    p.chat([Message(role="user", content="hi")], model="gpt-4o", effort="high")
    assert "reasoning_effort" not in seen["payload"] and seen["payload"]["temperature"] == 0.2


def test_unknown_models_try_reasoning_effort_and_drop_it_when_rejected():
    from quant_agent.llm import providers as P

    P._NO_EFFORT.clear()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append("reasoning_effort" in body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {"message": "Unrecognized request argument: reasoning_effort"}})
        return httpx.Response(200, json={"model": "x", "choices": [{"message": {"content": "ok"}}]})

    p = OpenAICompatibleProvider("gw", "https://api.example.com/v1", "k", transport=httpx.MockTransport(handler))
    assert P.openai_effort_mode("some-new-model") == "auto"
    assert p.chat([Message(role="user", content="hi")], model="some-new-model", effort="high").content == "ok"
    assert calls == [True, False]
    # 记住了：之后不再带这个参数
    p.chat([Message(role="user", content="hi")], model="some-new-model", effort="high")
    assert calls == [True, False, False]


def test_anthropic_adaptive_thinking_effort_and_stream():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        events = [
            {"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 10}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "检查回撤"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "需要回测"}},
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "tool_use", "id": "t1", "name": "run_backtest", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"cost_scale":'},
            },
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": " 1}"}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}},
            {"type": "message_stop"},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(events))

    p = AnthropicProvider("anthropic", "https://api.anthropic.com", "k", transport=httpx.MockTransport(handler))
    got: list[tuple[str, str]] = []
    tool = Tool(name="run_backtest", description="d", parameters={"type": "object", "properties": {}})
    r = p.chat(
        [Message(role="system", content="sys"), Message(role="user", content="hi")],
        model="claude-opus-5-5",
        tools=[tool],
        effort="max",
        on_delta=lambda k, t: got.append((k, t)),
    )
    pl = seen["payload"]
    assert pl["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert pl["output_config"] == {"effort": "xhigh"}
    assert "temperature" not in pl and pl["max_tokens"] >= 16000
    assert pl["fallbacks"] == "default" and seen["headers"]["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert r.thinking == "检查回撤" and r.content == "需要回测"
    assert r.tool_calls[0].arguments == {"cost_scale": 1}
    assert r.provider_blocks == [{"type": "thinking", "thinking": "检查回撤", "signature": "sig"}]
    assert ("thinking", "检查回撤") in got and ("text", "需要回测") in got

    # 下一轮把 thinking 块原样放回 assistant 消息最前面；多条工具结果合并成一条 user 消息
    follow = [
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="需要回测",
            tool_calls=[*r.tool_calls, ToolCall("t2", "x", {})],
            provider_blocks=r.provider_blocks,
        ),
        Message(role="tool", content="{}", tool_call_id="t1"),
        Message(role="tool", content="{}", tool_call_id="t2"),
    ]
    payload, _ = p.build_payload(
        follow, model="claude-opus-5-5", temperature=0.2, tools=None, max_tokens=None, effort=None, show_thinking=False
    )
    assistant = payload["messages"][1]["content"]
    assert assistant[0]["type"] == "thinking" and assistant[0]["signature"] == "sig"
    assert len(payload["messages"]) == 3 and len(payload["messages"][2]["content"]) == 2
    assert payload["thinking"]["display"] == "omitted" and "output_config" not in payload


def test_anthropic_older_models_keep_temperature_or_use_budget():
    p = AnthropicProvider("anthropic", "https://api.anthropic.com", "k")
    msgs = [Message(role="user", content="hi")]
    pl, h = p.build_payload(
        msgs, model="claude-haiku-4-5", temperature=0.3, tools=None, max_tokens=None, effort="high", show_thinking=False
    )
    assert pl["thinking"] == {"type": "enabled", "budget_tokens": 8000} and "temperature" not in pl
    pl, h = p.build_payload(
        msgs, model="claude-haiku-4-5", temperature=0.3, tools=None, max_tokens=None, effort=None, show_thinking=False
    )
    assert pl["temperature"] == 0.3 and "thinking" not in pl and "anthropic-beta" not in h


def _router(handler, models=("deepseek-chat", "deepseek-reasoner")) -> ModelRouter:
    cfg = AgentConfig(
        providers={
            "deepseek": ProviderConfig(
                "deepseek", "openai_compatible", "https://api.deepseek.com/v1", "k", list(models)
            )
        },
        routing={"default": "deepseek/deepseek-chat"},
        limits={},
        trace={},
    )
    r = ModelRouter(cfg)
    r._clients["deepseek"] = OpenAICompatibleProvider(
        "deepseek", "https://api.deepseek.com/v1", "k", transport=httpx.MockTransport(handler)
    )
    return r


def test_router_switches_deepseek_to_reasoner_on_high_effort_and_falls_back():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "deepseek-reasoner":
            return httpx.Response(400, json={"error": "tools not supported"})
        return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": "ok"}}]})

    r = _router(handler)
    out = r.chat([Message(role="user", content="hi")], effort="high")
    assert calls == ["deepseek-reasoner", "deepseek-chat"] and out.content == "ok"
    calls.clear()
    r.chat([Message(role="user", content="hi")])
    assert calls == ["deepseek-chat"]
    assert r.effort_mode("deepseek/deepseek-chat") == "switch"
    models = {m["target"]: m for m in r.available_models()}
    assert models["deepseek/deepseek-chat"]["context_window"] == 128_000


def test_context_windows():
    assert context_window("claude-opus-5-5") == 1_000_000
    assert context_window("claude-haiku-4-5") == 200_000
    assert context_window("unknown-local") == 32_000


def test_runtime_first_token_observes_only_stream_deltas():
    from agent_runtime.observations import model_observation

    observed = []

    def mark():
        observed.append(True)
        return True

    provider = OpenAICompatibleProvider("test", "https://example.test/v1", "key")
    content = _sse(
        [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "first"}}]},
        ],
        done=True,
    )
    with model_observation(mark):
        provider._collect_stream(httpx.Response(200, content=content), "m", None)
    assert len(observed) == 1
    observed.clear()
    with model_observation(mark):
        provider._parse(
            httpx.Response(200, json={"choices": [{"message": {"content": "final"}}]}), "m", lambda kind, text: None
        )
    assert observed == []  # final-response UI callback is not streaming evidence


def test_runtime_observer_failure_cannot_break_model_stream():
    from agent_runtime.observations import model_observation

    def unavailable():
        raise OSError("telemetry storage unavailable")

    provider = OpenAICompatibleProvider("test", "https://example.test/v1", "key")
    with model_observation(unavailable):
        result = provider._collect_stream(
            httpx.Response(
                200,
                content=_sse(
                    [
                        {"choices": [{"delta": {"content": "saved"}}]},
                    ],
                    done=True,
                ),
            ),
            "m",
            None,
        )
    assert result.content == "saved"


def test_runtime_observes_responses_and_anthropic_tool_streams():
    from agent_runtime.observations import model_observation

    from quant_agent.llm.providers import OpenAIResponsesProvider

    observed = []

    def mark():
        observed.append(True)
        return True

    responses = OpenAIResponsesProvider("test", "https://example.test/v1", "key")
    with model_observation(mark):
        responses._collect_events(
            httpx.Response(
                200,
                content=_sse(
                    [
                        {"type": "response.function_call_arguments.delta", "delta": "{}"},
                        {"type": "response.completed", "response": {"output": [], "model": "m"}},
                    ]
                ),
            ),
            "m",
            None,
        )
    assert len(observed) == 1
    observed.clear()
    anthropic = AnthropicProvider("test", "https://example.test", "key")
    with model_observation(mark):
        anthropic._collect_stream(
            httpx.Response(
                200,
                content=_sse(
                    [
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "tool_use", "id": "t", "name": "calc"},
                        },
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "input_json_delta", "partial_json": "{}"},
                        },
                    ]
                ),
            ),
            "m",
            None,
        )
    assert len(observed) == 1
