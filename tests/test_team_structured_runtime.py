"""Actual Team parallel orchestration uses a durable graph and frozen roles."""

import json
import sys
import threading
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from agent_runtime import AgentRuntime, Journal
from agent_runtime.contracts import ModelUnavailable

from quant_agent.agents.roles import Role, RoleStore
from quant_agent.agents.team import Team, TeamRunner, ToolRegistry
from quant_agent.llm.base import ChatResult, ToolCall
from quant_agent.platform import context
from quant_agent.platform.errors import AppError
from quant_agent.store import Collections, JsonStore


class Router:
    def __init__(self, *, fail=False):
        self.calls = Counter()
        self.fail = fail
        self.lock = threading.Lock()

    def chat(self, messages, **kwargs):
        role = next(marker for marker in ("ROLE:A", "ROLE:B", "ROLE:S") if marker in messages[0].content)
        with self.lock:
            self.calls[role] += 1
        if role == "ROLE:B":
            if self.fail:
                raise ModelUnavailable("rejected before dispatch")
            if not any(message.role == "tool" for message in messages):
                return ChatResult(content="", provider="demo", model="m", tool_calls=[ToolCall("call", "publish", {})])
        if role == "ROLE:S":
            assert "child_results" in messages[-1].content
        return ChatResult(content=role + " result", provider="demo", model="m")


def make_runner(tmp_path, router=None, effects=None):
    store = JsonStore(tmp_path / "store")
    roles = RoleStore(store)
    for role in [
        Role("a", "A", system_prompt="ROLE:A", tools=[], restrict_tools=True),
        Role("b", "B", system_prompt="ROLE:B", tools=["publish"], restrict_tools=True),
        Role("s", "S", system_prompt="ROLE:S", tools=[], restrict_tools=True),
    ]:
        roles.save(role)
    registry = ToolRegistry()

    def publish():
        effects.append("published")
        raise OSError("published but response lost")

    registry.register("publish", publish, parameters={"type": "object", "properties": {}})
    runner = TeamRunner(router or Router(), roles, tools=registry, store=store)
    runner.runtime_journal = Journal(tmp_path / "runtime.sqlite")
    return runner


def test_parallel_team_restarts_without_replaying_completed_roles_or_unknown_tools(tmp_path):
    effects = []
    router = Router()
    runner = make_runner(tmp_path, router, effects)
    team = Team("team", "Team", mode="parallel", role_ids=["a", "b"], synthesizer_id="s")
    with context.bind("ws", user_id="owner", role="member"):
        first = runner.run(team, "review")
        assert first.status == "waiting"
        assert router.calls == {"ROLE:A": 1, "ROLE:B": 1}
        assert runner.get_run(first.id)["status"] == "waiting"
        # Change editable role definitions after the durable plan was committed.
        runner.roles.save(Role("s", "Changed", system_prompt="WRONG NEW ROLE"))
        rebuilt = TeamRunner(router, runner.roles, tools=runner.tools, store=runner.store)
        rebuilt.runtime_journal = Journal(runner.runtime_journal.path)
        assert rebuilt.resume_run(first.id).status == "waiting"
        assert effects == ["published"] and router.calls["ROLE:B"] == 1
        leaf = rebuilt.runtime_journal.run(first.steps[1].runtime_run_id)
        pending = rebuilt.runtime_journal.steps(leaf["id"])[1]
        AgentRuntime(rebuilt.runtime_journal, None, None).signal(
            leaf["id"],
            "effect_resolution",
            {"step_id": pending["id"], "outcome": "succeeded", "result": "confirmed"},
            signal_id="external-confirmation",
            actor="admin",
        )
        completed = rebuilt.resume_run(first.id)
        assert completed.status == "done" and completed.synthesis == "ROLE:S result"
        assert router.calls == {"ROLE:A": 1, "ROLE:B": 2, "ROLE:S": 1}
        assert rebuilt.resume_run(first.id).status == "done"
        assert router.calls["ROLE:S"] == 1
        rebuilt.store.delete(Collections.TEAM_RUNS, first.id)
        assert rebuilt.get_run(first.id)["status"] == "done"
        assert first.id in {item["id"] for item in rebuilt.list_runs()}


def test_parallel_team_cancel_preserves_unknown_effect_and_never_synthesizes(tmp_path):
    router = Router()
    effects = []
    runner = make_runner(tmp_path, router, effects)
    with context.bind("ws", user_id="owner", role="member"):
        run = runner.run(Team("t", "T", mode="parallel", role_ids=["a", "b"], synthesizer_id="s"), "review")
        assert runner.cancel_run(run.id, signal_id="cancel").status == "canceling"
        assert runner.cancel_run(run.id, signal_id="cancel").status == "canceling"
        leaf = run.steps[1].runtime_run_id
        engine = AgentRuntime(runner.runtime_journal, None, None)
        pending = engine.get(leaf)["steps"][1]
        engine.signal(
            leaf,
            "effect_resolution",
            {"step_id": pending["id"], "outcome": "succeeded", "result": "confirmed"},
            signal_id="resolved",
            actor="admin",
        )
        assert runner.resume_run(run.id).status == "canceled"
        assert effects == ["published"] and router.calls["ROLE:S"] == 0


def test_all_settled_team_synthesizes_known_failure_but_not_unknown_results(tmp_path):
    router = Router(fail=True)
    runner = make_runner(tmp_path, router, [])
    with context.bind("ws", user_id="owner", role="member"):
        run = runner.run(Team("t", "T", mode="parallel", role_ids=["a", "b"], synthesizer_id="s"), "review")
        assert run.status == "done"
        assert [step.status for step in run.steps] == ["ok", "error"]
        assert router.calls["ROLE:S"] == 1
    with context.bind("ws", user_id="different", role="member"):
        with pytest.raises(AppError):
            runner.resume_run(run.id)
    with context.bind("another-workspace", user_id="owner", role="member"):
        with pytest.raises(AppError):
            runner.get_run(run.id)


def test_synthesis_context_keeps_legacy_output_cap_without_mutating_committed_results(tmp_path):
    class LongRouter:
        def chat(self, messages, **kwargs):
            if "ROLE:S" in messages[0].content:
                result = json.loads(messages[-1].content)["child_results"][0]
                assert len(result["output"]) < 2000
                assert "已截断" in result["output"]
                return ChatResult(content="summary", provider="demo", model="m")
            return ChatResult(content="A" * 10000, provider="demo", model="m")

    runner = make_runner(tmp_path, LongRouter(), [])
    with context.bind("ws", user_id="owner", role="member"):
        run = runner.run(Team("t", "T", mode="parallel", role_ids=["a"], synthesizer_id="s"), "review")
        assert run.status == "done"
        root = runner.runtime_journal.run(run.runtime_run_id)
        committed = json.loads(
            next(
                message["content"] for message in root["messages"] if message["content"].startswith('{"child_results":')
            )
        )["child_results"][0]["output"]
        assert len(committed) == 10000
