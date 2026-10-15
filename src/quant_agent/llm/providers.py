"""OpenAI 兼容（Chat Completions / Responses）与 Anthropic 三种 Provider 实现。

OpenAI 兼容层可对接 DeepSeek、通义、Kimi、vLLM、Ollama 等；
依赖 http 客户端 httpx（在 config 中注入 transport，便于测试 mock）。

两个 provider 都支持：
- effort：平台统一的思考努力程度（low / medium / high / max），各自映射到原生参数；
- on_delta：流式输出，正文与思考摘要逐段回调，供对话界面逐字显示；
- 推理内容回传：工具回合里把 thinking 块 / reasoning_content 原样带回，推理模型才能接着想。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any

import httpx

from .base import ChatResult, LLMError, Message, Tool, ToolCall, split_data_url


def _resolve_env(value: str) -> str:
    """支持 ${ENV_VAR} 形式，避免密钥落盘明文。"""
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value


def _iter_sse(resp: httpx.Response):
    """把 text/event-stream 拆成 data 载荷（字符串）。"""
    for line in resp.iter_lines():
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            yield line[5:].strip()


def _is_event_stream(resp: httpx.Response) -> bool:
    return "text/event-stream" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# OpenAI 兼容
# ---------------------------------------------------------------------------
_OPENAI_REASONING = re.compile(r"^(o\d|gpt-5)", re.I)


def openai_effort_mode(model: str, base_url: str = "") -> str:
    """native：发 reasoning_effort；switch：DeepSeek 在 chat / reasoner 之间切换；none：不支持。"""
    if _OPENAI_REASONING.match(model):
        return "native"
    if "deepseek" in model.lower() or "deepseek" in base_url.lower():
        return "switch"
    return "none"


class OpenAICompatibleProvider:
    type = "openai_compatible"

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = _resolve_env(api_key)
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._deepseek = "deepseek" in (name + base_url).lower()

    def _messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            d = m.to_dict()
            if self._deepseek and m.reasoning and m.role == "assistant":
                d["reasoning_content"] = m.reasoning  # DeepSeek 思考模式的工具回合需要原样带回
            out.append(d)
        return out

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 0.2,
        tools: list[Tool] | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        effort: str | None = None,
        on_delta: Callable[[str, str], None] | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {"model": model, "messages": self._messages(messages)}
        if effort and openai_effort_mode(model) == "native":
            payload["reasoning_effort"] = {"low": "low", "medium": "medium", "high": "high", "max": "high"}.get(
                effort, "medium"
            )
        elif not _OPENAI_REASONING.match(model):
            payload["temperature"] = temperature  # o 系列 / gpt-5 只接受默认温度
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [t.schema() for t in tools]
            payload["tool_choice"] = "auto"
        streaming = bool(on_delta) or stream
        if streaming:
            payload["stream"] = True

        try:
            if streaming:
                with self._client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                ) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        resp.raise_for_status()
                    if _is_event_stream(resp):
                        return self._collect_stream(resp, model, on_delta)
                    resp.read()
                    return self._parse(resp, model, on_delta)
            resp = self._client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:  # 网络/限流/超时统一包装
            raise LLMError(f"[{self.name}] request failed: {e}") from e
        return self._parse(resp, model, None)

    def _parse(self, resp: httpx.Response, model: str, on_delta) -> ChatResult:
        try:
            data = resp.json()
            choice = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(f"[{self.name}] 返回格式无法解析：{str(resp.text)[:200]}") from e
        tool_calls = [
            ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=_safe_json(tc["function"].get("arguments", "{}")),
            )
            for tc in choice.get("tool_calls") or []
        ]
        content = choice.get("content") or ""
        reasoning = choice.get("reasoning_content") or ""
        if on_delta:  # 服务端没给流式时，一次性回调，界面逻辑不用分两套
            if reasoning:
                on_delta("thinking", reasoning)
            if content:
                on_delta("text", content)
        return ChatResult(
            content=content,
            model=data.get("model", model),
            provider=self.name,
            tool_calls=tool_calls,
            usage=data.get("usage") or {},
            raw=data,
            thinking=reasoning,
            stop_reason=(data["choices"][0].get("finish_reason") or ""),
        )

    def _collect_stream(self, resp: httpx.Response, model: str, on_delta) -> ChatResult:
        text: list[str] = []
        thinking: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        finish = ""
        real_model = model
        for data in _iter_sse(resp):
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            real_model = chunk.get("model") or real_model
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    thinking.append(delta["reasoning_content"])
                    if on_delta:
                        on_delta("thinking", delta["reasoning_content"])
                if delta.get("content"):
                    text.append(delta["content"])
                    if on_delta:
                        on_delta("text", delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] = fn.get("name") or slot["name"]
                    slot["args"] += fn.get("arguments") or ""
                finish = choice.get("finish_reason") or finish
        tool_calls = [
            ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=_safe_json(c["args"] or "{}"))
            for i, c in sorted(calls.items())
            if c["name"]
        ]
        return ChatResult(
            content="".join(text),
            model=real_model,
            provider=self.name,
            tool_calls=tool_calls,
            usage=usage,
            thinking="".join(thinking),
            stop_reason=finish,
        )

    def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        resp = self._client.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
        return [row["embedding"] for row in resp.json()["data"]]


# ---------------------------------------------------------------------------
# OpenAI Responses API（/v1/responses）
# ---------------------------------------------------------------------------
class OpenAIResponsesProvider(OpenAICompatibleProvider):
    """走 Responses 协议：system 进 instructions，工具回合用 function_call / function_call_output 条目。"""

    type = "openai_responses"

    def build_payload(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float,
        tools: list[Tool] | None,
        max_tokens: int | None,
        effort: str | None,
    ) -> dict[str, Any]:
        instructions = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
        items: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                items.append({"type": "function_call_output", "call_id": m.tool_call_id, "output": m.content})
                continue
            if m.images and m.role == "user":
                parts: list[dict[str, Any]] = [{"type": "input_text", "text": m.content}] if m.content else []
                parts += [{"type": "input_image", "image_url": u} for u in m.images]
                items.append({"role": "user", "content": parts})
            elif m.content:
                items.append({"role": m.role, "content": m.content})
            for tc in m.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.id,
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    }
                )
        payload: dict[str, Any] = {"model": model, "input": items, "store": False}
        if instructions:
            payload["instructions"] = instructions
        if effort and openai_effort_mode(model) == "native":
            level = {"low": "low", "medium": "medium", "high": "high", "max": "high"}.get(effort, "medium")
            payload["reasoning"] = {"effort": level, "summary": "auto"}
        elif not _OPENAI_REASONING.match(model):
            payload["temperature"] = temperature
        if max_tokens:
            payload["max_output_tokens"] = max_tokens
        if tools:
            payload["tools"] = [
                {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 0.2,
        tools: list[Tool] | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        effort: str | None = None,
        on_delta: Callable[[str, str], None] | None = None,
    ) -> ChatResult:
        payload = self.build_payload(
            messages, model=model, temperature=temperature, tools=tools, max_tokens=max_tokens, effort=effort
        )
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/responses"
        try:
            if on_delta or stream:
                payload["stream"] = True
                with self._client.stream("POST", url, headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        resp.raise_for_status()
                    if _is_event_stream(resp):
                        return self._collect_events(resp, model, on_delta)
                    resp.read()
                    return self._parse_response(resp.json(), model, on_delta)
            resp = self._client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"[{self.name}] request failed: {e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"[{self.name}] 返回格式无法解析：{resp.text[:200]}") from e
        return self._parse_response(data, model, None)

    def _parse_response(self, data: dict[str, Any], model: str, on_delta) -> ChatResult:
        if not isinstance(data, dict) or not isinstance(data.get("output"), list):
            raise LLMError(f"[{self.name}] 返回格式无法解析：{str(data)[:200]}")
        if data.get("error"):
            raise LLMError(f"[{self.name}] {data['error']}")
        text: list[str] = []
        thinking: list[str] = []
        calls: list[ToolCall] = []
        for item in data["output"]:
            kind = item.get("type")
            if kind == "message":
                text.extend(c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text")
            elif kind == "reasoning":
                thinking.extend(s.get("text", "") for s in item.get("summary") or [])
            elif kind == "function_call":
                calls.append(
                    ToolCall(
                        id=item.get("call_id") or item.get("id") or f"call_{len(calls)}",
                        name=item.get("name", ""),
                        arguments=_safe_json(item.get("arguments") or "{}"),
                    )
                )
        content, reasoning = "".join(text), "\n".join(t for t in thinking if t)
        if on_delta:
            if reasoning:
                on_delta("thinking", reasoning)
            if content:
                on_delta("text", content)
        stop = data.get("status") or ""
        if stop == "incomplete":
            stop = (data.get("incomplete_details") or {}).get("reason") or stop
        return ChatResult(
            content=content,
            model=data.get("model", model),
            provider=self.name,
            tool_calls=calls,
            usage=data.get("usage") or {},
            raw=data,
            thinking=reasoning,
            stop_reason=stop,
        )

    def _collect_events(self, resp: httpx.Response, model: str, on_delta) -> ChatResult:
        """逐段回调正文与思考摘要；最终结果以 response.completed 里的完整 response 为准。"""
        text: list[str] = []
        final: dict[str, Any] | None = None
        for data in _iter_sse(resp):
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type", "")
            if kind == "response.output_text.delta":
                text.append(ev.get("delta", ""))
                if on_delta:
                    on_delta("text", ev.get("delta", ""))
            elif kind == "response.reasoning_summary_text.delta" and on_delta:
                on_delta("thinking", ev.get("delta", ""))
            elif kind in ("response.completed", "response.incomplete"):
                final = ev.get("response")
            elif kind in ("response.failed", "error"):
                err = (ev.get("response") or {}).get("error") or ev.get("error") or ev.get("message") or ev
                raise LLMError(f"[{self.name}] {err}")
        if final is not None:
            return self._parse_response(final, model, None)
        return ChatResult(content="".join(text), model=model, provider=self.name)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
# 自适应思考 + effort 的新一代模型；其中一部分已不接受 temperature
_CLAUDE_ADAPTIVE = re.compile(r"claude-(opus-(4-[6-9]|5)|sonnet-(4-6|5)|haiku-5|fable|mythos)", re.I)
_CLAUDE_NO_TEMPERATURE = re.compile(r"claude-(opus-(4-[7-9]|5)|sonnet-5|haiku-5|fable|mythos)", re.I)
_CLAUDE_XHIGH = re.compile(r"claude-(opus-(4-[7-9]|5)|sonnet-5|haiku-5|fable|mythos)", re.I)
_CLAUDE_BUDGET = re.compile(r"claude-(haiku-4-5|sonnet-4-5|opus-4-[015]|sonnet-4-0|3-7)", re.I)
# 服务端拒答时自动换模型兜底（只对第一方 API 的这几款开启）
_CLAUDE_FALLBACKS = re.compile(r"claude-(opus-5-5|fable-5-1|sonnet-5-5)", re.I)


def anthropic_effort_mode(model: str) -> str:
    if _CLAUDE_ADAPTIVE.search(model):
        return "native"
    if _CLAUDE_BUDGET.search(model):
        return "budget"
    return "none"


class AnthropicProvider:
    type = "anthropic"

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = _resolve_env(api_key)
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def build_payload(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float,
        tools: list[Tool] | None,
        max_tokens: int | None,
        effort: str | None,
        show_thinking: bool,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        system = "\n".join(m.content for m in messages if m.role == "system")
        convo = _merge_tool_results([_to_anthropic(m) for m in messages if m.role != "system"])
        mode = anthropic_effort_mode(model)
        payload: dict[str, Any] = {"model": model, "messages": convo, "max_tokens": max_tokens or 4096}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        if mode == "native":
            payload["max_tokens"] = max(payload["max_tokens"], 16000)
            payload["thinking"] = {"type": "adaptive", "display": "summarized" if show_thinking else "omitted"}
            if effort:
                level = effort
                if effort == "max":
                    level = "xhigh" if _CLAUDE_XHIGH.search(model) else "max"
                payload["output_config"] = {"effort": level}
        elif mode == "budget" and effort in ("medium", "high", "max"):
            budget = {"medium": 2048, "high": 8000, "max": 16000}[effort]
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            payload["max_tokens"] = max(payload["max_tokens"], budget + 4096)
        if not _CLAUDE_NO_TEMPERATURE.search(model) and "thinking" not in payload:
            payload["temperature"] = temperature
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        if _CLAUDE_FALLBACKS.search(model) and "api.anthropic.com" in self.base_url:
            payload["fallbacks"] = "default"
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        return payload, headers

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 0.2,
        tools: list[Tool] | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        effort: str | None = None,
        on_delta: Callable[[str, str], None] | None = None,
    ) -> ChatResult:
        payload, headers = self.build_payload(
            messages,
            model=model,
            temperature=temperature,
            tools=tools,
            max_tokens=max_tokens,
            effort=effort,
            show_thinking=bool(on_delta),
        )
        try:
            if on_delta or stream:
                payload["stream"] = True
                with self._client.stream("POST", f"{self.base_url}/v1/messages", headers=headers, json=payload) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        resp.raise_for_status()
                    if _is_event_stream(resp):
                        return self._collect_stream(resp, model, on_delta)
                    resp.read()
                    return self._parse(resp.json(), model, on_delta)
            resp = self._client.post(f"{self.base_url}/v1/messages", headers=headers, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.text[:300]
            except Exception:  # 流式响应未读完时拿不到正文
                pass
            raise LLMError(f"[{self.name}] request failed: {e} {detail}".strip()) from e
        except httpx.HTTPError as e:
            raise LLMError(f"[{self.name}] request failed: {e}") from e
        return self._parse(resp.json(), model, None)

    def _parse(self, data: dict[str, Any], model: str, on_delta) -> ChatResult:
        blocks = data.get("content", []) or []
        text_parts, tool_calls, thinking, keep = [], [], [], []
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block["text"])
            elif kind == "tool_use":
                tool_calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block.get("input", {})))
            elif kind in ("thinking", "redacted_thinking"):
                keep.append(block)
                if block.get("thinking"):
                    thinking.append(block["thinking"])
        if on_delta:
            if thinking:
                on_delta("thinking", "\n".join(thinking))
            if text_parts:
                on_delta("text", "\n".join(text_parts))
        return ChatResult(
            content="\n".join(text_parts),
            model=data.get("model", model),
            provider=self.name,
            tool_calls=tool_calls,
            usage=data.get("usage", {}),
            raw=data,
            thinking="\n".join(thinking),
            provider_blocks=keep,
            stop_reason=data.get("stop_reason") or "",
        )

    def _collect_stream(self, resp: httpx.Response, model: str, on_delta) -> ChatResult:
        blocks: dict[int, dict[str, Any]] = {}
        partial_json: dict[int, str] = {}
        usage: dict[str, Any] = {}
        stop_reason = ""
        real_model = model
        for data in _iter_sse(resp):
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "message_start":
                msg = ev.get("message") or {}
                real_model = msg.get("model") or real_model
                usage.update(msg.get("usage") or {})
            elif kind == "content_block_start":
                block = dict(ev.get("content_block") or {})
                blocks[ev.get("index", 0)] = block
            elif kind == "content_block_delta":
                idx = ev.get("index", 0)
                block = blocks.setdefault(idx, {"type": "text", "text": ""})
                delta = ev.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    block["text"] = block.get("text", "") + delta.get("text", "")
                    if on_delta:
                        on_delta("text", delta.get("text", ""))
                elif dt == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
                    if on_delta:
                        on_delta("thinking", delta.get("thinking", ""))
                elif dt == "signature_delta":
                    block["signature"] = block.get("signature", "") + delta.get("signature", "")
                elif dt == "input_json_delta":
                    partial_json[idx] = partial_json.get(idx, "") + delta.get("partial_json", "")
            elif kind == "message_delta":
                stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
                usage.update(ev.get("usage") or {})
            elif kind == "error":
                raise LLMError(f"[{self.name}] 流式输出中断：{(ev.get('error') or {}).get('message', '')}")
        for idx, raw in partial_json.items():
            if blocks.get(idx, {}).get("type") == "tool_use":
                blocks[idx]["input"] = _safe_json(raw or "{}")
        ordered = [blocks[i] for i in sorted(blocks)]
        result = self._parse(
            {"content": ordered, "model": real_model, "usage": usage, "stop_reason": stop_reason}, model, None
        )
        return result

    def embed(self, texts: list[str], *, model: str) -> list[list[float]]:
        raise LLMError("Anthropic 暂无公开 embeddings 接口，请配置独立嵌入 provider")


def _to_anthropic(m: Message) -> dict[str, Any]:
    if m.role == "tool":
        return {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}],
        }
    blocks: list[dict[str, Any]] = []
    if m.role == "assistant":
        blocks.extend(m.provider_blocks)  # thinking 块必须原样排在最前面
    if m.role == "user":
        for url in m.images:  # 图片放在文字前面，Anthropic 推荐的顺序
            media_type, data = split_data_url(url)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
    if m.content:
        blocks.append({"type": "text", "text": m.content})
    for tc in m.tool_calls:
        blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
    return {"role": m.role, "content": blocks or m.content}


def _merge_tool_results(convo: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一轮的多个工具结果合并到一条 user 消息里（并行工具调用的要求）。"""
    out: list[dict[str, Any]] = []
    for msg in convo:
        is_result = (
            msg["role"] == "user"
            and isinstance(msg["content"], list)
            and msg["content"]
            and all(b.get("type") == "tool_result" for b in msg["content"])
        )
        prev = out[-1] if out else None
        if (
            is_result
            and prev
            and prev["role"] == "user"
            and isinstance(prev["content"], list)
            and prev["content"]
            and all(b.get("type") == "tool_result" for b in prev["content"])
        ):
            prev["content"] = prev["content"] + msg["content"]
        else:
            out.append(msg)
    return out


def _safe_json(s: str | dict) -> dict[str, Any]:
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {}
