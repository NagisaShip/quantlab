"""Generic Agent entry points share Runtime authority without hidden retries."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from agent_runtime import Journal

from quant_agent.agents.base import Agent, tool
from quant_agent.context.manager import ContextManager
from quant_agent.llm.base import ChatResult, ToolCall
from quant_agent.platform import context


def test_generic_agent_persists_trace_and_reopens_without_repeating_tools(tmp_path):
    effects = []

    class Router:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            assert kwargs["role"] == "code_gen"
            if self.calls == 1:
                return ChatResult(content="", provider="demo", model="m", tool_calls=[ToolCall("c", "save", {})])
            return ChatResult(content="saved", provider="demo", model="m")

    router = Router()
    router.runtime_journal = Journal(tmp_path / "runtime.sqlite")

    def make_agent():
        return Agent(
            name="writer",
            role="code_gen",
            router=router,
            context=ContextManager(system_prompt="Produce a strategy"),
            tools=[tool("save", "save", {}, lambda: effects.append("saved") or {"ok": True})],
        )

    with context.bind("ws", user_id="u", role="member"):
        result = make_agent().run("write")
        assert result.status == "done" and result.final == "saved"
        assert [step.kind for step in result.steps] == ["llm", "tool", "llm"]
        reopened = make_agent().resume_runtime(result.run_id)
        assert reopened.context_hash == result.context_hash
        assert reopened.final == "saved"
    assert effects == ["saved"] and router.calls == 2
    with context.bind("other", user_id="u", role="member"):
        with pytest.raises(ValueError, match="workspace"):
            make_agent().resume_runtime(result.run_id)
    with context.bind("ws", user_id="another", role="member"):
        with pytest.raises(ValueError, match="user"):
            make_agent().resume_runtime(result.run_id)


def test_generic_agent_never_replays_a_tool_that_raised_after_effect(tmp_path):
    effects = []

    class Router:
        def chat(self, messages, **kwargs):
            return ChatResult(content="", provider="demo", model="m", tool_calls=[ToolCall("c", "publish", {})])

    def publish():
        effects.append("published")
        raise OSError("response lost after publishing")

    router = Router()
    router.runtime_journal = Journal(tmp_path / "runtime.sqlite")
    agent = Agent(
        name="writer",
        role="report",
        router=router,
        context=ContextManager(system_prompt="report"),
        tools=[tool("publish", "publish", {}, publish)],
    )
    with context.bind("ws", user_id="u", role="member"):
        result = agent.run("publish")
        assert result.status == "blocked"
        assert agent.resume_runtime(result.run_id).status == "blocked"
    assert effects == ["published"]


def test_generic_model_rebuilds_document_inspection_from_saved_tool_result(monkeypatch):
    from quant_agent.local import documents
    from quant_agent.platform.runtime_adapters import QuantLabAgentModelPort

    monkeypatch.setattr(
        documents,
        "preview_images",
        lambda name, result: (
            ["data:image/png;base64,saved"] if name == "local_preview_document" and result.get("rendered") else []
        ),
    )

    class Router:
        def chat(self, messages, **kwargs):
            assert messages[-1].images == ["data:image/png;base64,saved"]
            return ChatResult(content="layout checked", provider="demo", model="m")

    messages = [
        {"role": "tool", "name": "local_preview_document", "content": '{"rendered": true}', "tool_call_id": "c"}
    ]
    port = QuantLabAgentModelPort(Router(), workspace_id="ws", user_id="u")
    assert port.generate(messages, request_id="step").text == "layout checked"
    assert len(messages) == 1
