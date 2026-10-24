"""Existing Team role execution can use the durable Runtime kernel."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from agent_runtime import Journal

from quant_agent.agents.roles import Role
from quant_agent.agents.team import TeamRunner, ToolRegistry
from quant_agent.llm.base import ChatResult, LLMError
from quant_agent.llm.base import ToolCall as HostToolCall
from quant_agent.llm.hub import ModelHub, ModelHubAdmission
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig
from quant_agent.platform import context
from quant_agent.platform.db import Database


class Router:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            assert messages[0].role == "system"
            assert "calculator" in {tool.name for tool in kwargs["tools"]}
            return ChatResult(
                content="",
                model="model",
                provider="demo",
                tool_calls=[HostToolCall("call-1", "calculator", {"a": 2, "b": 3})],
                usage={"input_tokens": 2, "output_tokens": 1},
            )
        assert messages[-2].tool_calls[0].id == messages[-1].tool_call_id
        return ChatResult(content="5", model="model", provider="demo", usage={"input_tokens": 2, "output_tokens": 1})


def test_team_role_tool_loop_is_persisted_by_runtime(tmp_path):
    registry = ToolRegistry()
    registry.register(
        "calculator",
        lambda a, b: a + b,
        parameters={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    )
    router = Router()
    journal = Journal(tmp_path / "runtime.sqlite")
    runner = TeamRunner(router, None, tools=registry)
    runner.runtime_journal = journal
    role = Role(id="analyst", name="Analyst", tools=["calculator"], restrict_tools=True)
    with context.bind("ws", user_id="user", role="member"):
        step = runner._invoke(role, "calculate 2+3")
    assert step.status == "ok"
    assert step.output == "5"
    assert step.runtime_run_id.startswith("team-")
    assert router.calls == 2
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0


def test_real_team_request_is_single_dispatch_and_context_is_durable(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'host.sqlite').as_posix()}")
    db.migrate()
    provider = ProviderConfig(
        "test", "openai_compatible", "http://localhost", "key", models=["model"], scope="personal", owner_id="user"
    )
    router = ModelRouter(
        AgentConfig(providers={"test": provider}, routing={"default": "test/model"}, limits={}, trace={})
    )
    hub = ModelHub(store=None, secrets=None, db=db)
    router.admission = ModelHubAdmission(hub)

    class Client:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            raise LLMError("unknown response", retryable=True)

    client = Client()
    router._client = lambda *args: client
    runner = TeamRunner(router, None)
    runner.runtime_journal = Journal(tmp_path / "runtime.sqlite")
    role = Role(id="analyst", name="Analyst", system_prompt="Durable system rules", tools=[], restrict_tools=True)
    with context.bind("ws", user_id="user", role="member"):
        step = runner._invoke(role, "question")
    run = runner.runtime_journal.run(step.runtime_run_id)
    assert run["state"] == "WAITING"
    assert "Durable system rules" in run["messages"][0]["content"]
    assert client.calls == 1
    admission = hub.admission(runner.runtime_journal.steps(run["id"])[0]["id"])
    assert admission["run_id"] == run["id"] and admission["status"] == "UNSETTLED"
