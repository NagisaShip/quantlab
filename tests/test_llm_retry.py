"""模型调用失败自动重试：临时性错误（限流、5xx、超时、断流）同一个模型重试；不可重试的错误直接换下一个模型。"""

from __future__ import annotations

import httpx
import pytest

from quant_agent.llm.base import ChatResult, LLMError, Message, http_error
from quant_agent.llm.providers import OpenAICompatibleProvider
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig

OK = {
    "choices": [{"message": {"content": "好的"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
}


def _router(retries: int | None = None, fallback: list[str] | None = None) -> ModelRouter:
    routing: dict = {"default": "a/m1", "fallback": fallback or []}
    if retries is not None:
        routing["retries"] = retries
    cfg = AgentConfig(
        providers={
            "a": ProviderConfig(
                name="a", type="openai_compatible", base_url="https://a.test/v1", api_key="k", models=["m1"]
            ),
            "b": ProviderConfig(
                name="b", type="openai_compatible", base_url="https://b.test/v1", api_key="k", models=["m2"]
            ),
        },
        routing=routing,
        limits={},
        trace={},
    )
    r = ModelRouter(cfg)
    r.waits = []
    r.sleep = r.waits.append  # 不真的睡，记下等了多久
    return r


def _provider(router: ModelRouter, name: str, responses: list) -> list[int]:
    """给服务商装一个按顺序返回的假传输层；返回调用计数（列表长度即调用次数）。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body, headers = item
        return httpx.Response(status, json=body, headers=headers or {})

    base = router.config.providers[name].base_url
    router._clients[name] = OpenAICompatibleProvider(name, base, "k", transport=httpx.MockTransport(handler))
    return calls


def test_rate_limit_is_retried_on_the_same_model():
    r = _router()
    calls = _provider(r, "a", [(429, {"error": "slow down"}, None), (503, {"error": "busy"}, None), (200, OK, None)])
    seen = []
    out = r.chat([Message(role="user", content="hi")], on_retry=seen.append)
    assert out.content == "好的" and len(calls) == 3
    assert [s["attempt"] for s in seen] == [1, 2] and seen[0]["status"] == 429 and seen[0]["retries"] == 2
    # 指数退避：约 1 秒、约 2 秒（±20% 抖动）
    assert 0.8 <= r.waits[0] <= 1.2 and 1.6 <= r.waits[1] <= 2.4


def test_retry_after_header_is_honoured():
    r = _router()
    _provider(r, "a", [(429, {"error": "x"}, {"Retry-After": "3"}), (200, OK, None)])
    r.chat([Message(role="user", content="hi")])
    assert r.waits == [3.0]


def test_timeouts_and_network_errors_are_retried():
    r = _router()
    calls = _provider(r, "a", [httpx.ConnectTimeout("timeout"), httpx.ReadError("reset"), (200, OK, None)])
    assert r.chat([Message(role="user", content="hi")]).content == "好的" and len(calls) == 3


def test_bad_request_is_not_retried_and_falls_back():
    """密钥错、参数错重试也没用：不等待，直接换降级链里的下一个模型。"""
    r = _router(fallback=["b/m2"])
    a = _provider(r, "a", [(401, {"error": "bad key"}, None)])
    b = _provider(r, "b", [(200, OK, None)])
    assert r.chat([Message(role="user", content="hi")]).content == "好的"
    assert len(a) == 1 and len(b) == 1 and r.waits == []


def test_retries_exhausted_then_fallback_and_count_is_configurable():
    r = _router(retries=1, fallback=["b/m2"])
    a = _provider(r, "a", [(500, {}, None), (500, {}, None)])
    b = _provider(r, "b", [(200, OK, None)])
    assert r.chat([Message(role="user", content="hi")]).content == "好的"
    assert len(a) == 2 and len(b) == 1 and len(r.waits) == 1


def test_zero_retries_and_error_message_counts_attempts():
    r = _router(retries=0)
    _provider(r, "a", [(503, {}, None)])
    with pytest.raises(LLMError):
        r.chat([Message(role="user", content="hi")])
    assert r.waits == []
    r = _router(retries=2)
    _provider(r, "a", [(503, {}, None)] * 3)
    with pytest.raises(LLMError, match="已重试 2 次"):
        r.chat([Message(role="user", content="hi")])


def test_too_long_retry_after_skips_to_next_model():
    r = _router(fallback=["b/m2"])
    _provider(r, "a", [(429, {}, {"Retry-After": "600"})])
    _provider(r, "b", [(200, OK, None)])
    assert r.chat([Message(role="user", content="hi")]).content == "好的" and r.waits == []


def test_on_retry_can_abort():
    class Stop(Exception):
        pass

    def stop(_info):
        raise Stop()

    r = _router()
    _provider(r, "a", [(503, {}, None), (200, OK, None)])
    with pytest.raises(Stop):
        r.chat([Message(role="user", content="hi")], on_retry=stop)


def test_http_error_classification():
    req = httpx.Request("POST", "https://x")
    e = http_error("p", httpx.HTTPStatusError("x", request=req, response=httpx.Response(529, request=req)))
    assert e.retryable and e.status == 529
    e = http_error("p", httpx.HTTPStatusError("x", request=req, response=httpx.Response(400, request=req)))
    assert not e.retryable
    assert isinstance(ChatResult(content="", model="m", provider="p"), ChatResult)
