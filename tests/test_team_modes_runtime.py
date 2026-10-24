"""Sequential and debate graph semantics match the original Team transcripts."""

import json
import sys
import threading
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from agent_runtime import AgentRuntime, Journal
from test_team_structured_runtime import make_runner

from quant_agent.agents.roles import Role
from quant_agent.agents.team import Team, TeamRunner
from quant_agent.llm.base import ChatResult, ToolCall
from quant_agent.platform import context


class Router:
    def __init__(self):
        self.calls = Counter()
        self.inputs = []
        self.lock = threading.Lock()

    def chat(self, messages, **kwargs):
        role = next(marker for marker in ("ROLE:A", "ROLE:B", "ROLE:S") if marker in messages[0].content)
        prompt = messages[1].content
        turn = next((index for index in (1, 2) if f"请进行第 {index} 轮质疑" in prompt), 0)
        dependencies = (
            json.loads(messages[-1].content).get("dependency_results")
            if messages[-1].content.startswith('{"dependency_results":')
            else None
        )
        with self.lock:
            self.calls[(role, turn)] += 1
            self.inputs.append((role, turn, dependencies, prompt))
        if role == "ROLE:S":
            assert "child_results" in messages[-1].content
        return ChatResult(content=f"{role}-turn-{turn}", provider="demo", model="m")


def test_sequential_roles_receive_every_prior_successful_result_in_order(tmp_path):
    router = Router()
    runner = make_runner(tmp_path, router, [])
    with context.bind("ws", user_id="owner", role="member"):
        run = runner.run(Team("t", "T", mode="sequential", role_ids=["a", "b"], synthesizer_id="s"), "review")
        assert run.status == "done"
        assert [(role, turn) for role, turn, *_ in router.inputs] == [("ROLE:A", 0), ("ROLE:B", 0), ("ROLE:S", 0)]
        assert [item["output"] for item in router.inputs[1][2]] == ["ROLE:A-turn-0"]
        assert "按顺序对应角色：A" in router.inputs[1][3]
        assert runner.resume_run(run.id).status == "done"
        assert sum(router.calls.values()) == 3


def test_debate_rounds_see_prior_rounds_but_never_future_opinions(tmp_path):
    router = Router()
    runner = make_runner(tmp_path, router, [])
    runner.roles.save(Role("b", "B", system_prompt="ROLE:B", tools=[], tags=["风控"], restrict_tools=True))
    with context.bind("ws", user_id="owner", role="member"):
        run = runner.run(
            Team("t", "T", mode="debate", role_ids=["a", "b"], synthesizer_id="s", challenge_rounds=2), "review"
        )
        assert run.status == "done" and len(run.steps) == 4
        rounds = {turn: deps for role, turn, deps, _ in router.inputs if role == "ROLE:B" and turn > 0}
        assert [item["output"] for item in rounds[1]] == ["ROLE:A-turn-0", "ROLE:B-turn-0"]
        assert [item["output"] for item in rounds[2]] == ["ROLE:A-turn-0", "ROLE:B-turn-0", "ROLE:B-turn-1"]
        assert run.steps[-1].role_name == "B · 第 2 轮质疑"
        assert sum(router.calls.values()) == 5


def test_same_round_critics_keep_the_same_independent_prior_snapshot(tmp_path):
    router = Router()
    runner = make_runner(tmp_path, router, [])
    for rid in ("a", "b"):
        runner.roles.save(
            Role(rid, rid.upper(), system_prompt=f"ROLE:{rid.upper()}", tools=[], tags=["质疑"], restrict_tools=True)
        )
    with context.bind("ws", user_id="owner", role="member"):
        run = runner.run(Team("t", "T", mode="debate", role_ids=["a", "b"], synthesizer_id="s"), "review")
        assert run.status == "done"
        critics = [deps for _, turn, deps, _ in router.inputs if turn == 1]
        assert len(critics) == 2
        assert critics[0] == critics[1]
        assert all(len(deps) == 2 for deps in critics)


def test_sequential_restart_never_skips_unknown_effect_or_repeats_finished_predecessor(tmp_path):
    class UnknownRouter(Router):
        def chat(self, messages, **kwargs):
            if "ROLE:A" in messages[0].content and not any(message.role == "tool" for message in messages):
                with self.lock:
                    self.calls[("ROLE:A", "tool")] += 1
                return ChatResult(content="", provider="demo", model="m", tool_calls=[ToolCall("call", "publish", {})])
            return super().chat(messages, **kwargs)

    router, effects = UnknownRouter(), []
    runner = make_runner(tmp_path, router, effects)
    runner.roles.save(Role("a", "A", system_prompt="ROLE:A", tools=["publish"], restrict_tools=True))
    with context.bind("ws", user_id="owner", role="member"):
        run = runner.run(Team("t", "T", mode="sequential", role_ids=["a", "b"], synthesizer_id="s"), "review")
        assert run.status == "waiting" and len(router.calls) == 1
        assert [step.status for step in run.steps] == ["waiting", "blocked"]
        assert run.steps[1].model == ""
        assert effects == ["published"]
        rebuilt = TeamRunner(router, runner.roles, tools=runner.tools, store=runner.store)
        rebuilt.runtime_journal = Journal(runner.runtime_journal.path)
        assert rebuilt.resume_run(run.id).status == "waiting"
        assert len(router.calls) == 1
        leaf = run.steps[0].runtime_run_id
        engine = AgentRuntime(rebuilt.runtime_journal, None, None)
        pending = engine.get(leaf)["steps"][1]
        engine.signal(
            leaf,
            "effect_resolution",
            {"step_id": pending["id"], "outcome": "succeeded", "result": "confirmed"},
            signal_id="confirmed",
            actor="admin",
        )
        assert rebuilt.resume_run(run.id).status == "done"
        assert effects == ["published"]
        assert router.calls[("ROLE:A", "tool")] == 1
        assert router.calls[("ROLE:B", 0)] == 1
