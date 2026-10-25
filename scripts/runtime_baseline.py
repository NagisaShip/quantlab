"""Reproducible local host baselines; synthetic model latency/fees are zero."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

from quant_agent.agents.base import Agent
from quant_agent.agents.lab_state import LabStore, new_project
from quant_agent.agents.roles import Role, RoleStore
from quant_agent.agents.strategy_lab import LabTools, StrategyLab
from quant_agent.agents.team import Team, TeamRunner
from quant_agent.context.manager import ContextManager
from quant_agent.llm.base import ChatResult
from quant_agent.platform import context
from quant_agent.store import JsonStore

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages/agent-runtime/src"))


class Router:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def chat(self, messages, **kwargs):
        with self.lock:
            self.calls += 1
        return ChatResult(
            content="Baseline answer",
            provider="synthetic",
            model="fixed",
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )


def case(kind, durable, root):
    from agent_runtime import Journal

    router = Router()
    journal = Journal(root / "runtime.sqlite") if durable else None
    if kind == "agent":
        router.runtime_journal = journal
        agent = Agent(
            name="baseline", role="report", router=router, context=ContextManager(system_prompt="Fixed baseline")
        )

        def run():
            result = agent.run("Answer once")
            assert result.status == "done"
            return result.final
    elif kind == "team":
        store = JsonStore(root / "store")
        roles = RoleStore(store)
        for rid in ("a", "b", "s"):
            roles.save(Role(rid, rid, system_prompt="Fixed baseline", tools=[], restrict_tools=True))
        runner = TeamRunner(router, roles, store=store)
        runner.runtime_journal = journal

        def run():
            result = runner.run(
                Team("t", "Baseline", mode="parallel", role_ids=["a", "b"], synthesizer_id="s"), "Review once"
            )
            assert result.status == "done"
            return result.synthesis
    else:
        store = LabStore(JsonStore(root / "store"))
        project = store.save(new_project({"kind": "research", "title": "Baseline", "idea": "Answer once"}))
        lab = StrategyLab(
            router,
            store,
            LabTools(
                backtest=lambda *a, **k: {}, market=lambda *a, **k: {}, templates=lambda q: [], runtime_journal=journal
            ),
        )
        lab._all_tools = lambda pid: {}
        lab._maybe_autocompact = lambda pid: None

        def run():
            pid = project["id"]
            store.append(pid, {"role": "user", "kind": "text", "content": "Answer once"})
            lab._chat_llm(pid, "Answer once")
            return next(
                m["content"] for m in reversed(store.messages(pid)) if m["role"] == "assistant" and m["kind"] == "text"
            )

    start = time.perf_counter()
    with context.bind("baseline", user_id="baseline", role="member"):
        output = run()
    elapsed = (time.perf_counter() - start) * 1000
    assert output == "Baseline answer", (kind, durable, output)
    assert router.calls == (3 if kind == "team" else 1)
    return elapsed, router.calls


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100:
        parser.error("iterations must be 1..100")
    results = []
    with tempfile.TemporaryDirectory(prefix="runtime-baseline-") as temporary:
        for kind in ("agent", "team", "lab"):
            for durable in (False, True):
                values = [
                    case(kind, durable, Path(temporary) / f"{kind}-{durable}-{i}") for i in range(args.iterations)
                ]
                times = sorted(v[0] for v in values)
                results.append(
                    {
                        "host": kind,
                        "runtime": durable,
                        "n": len(times),
                        "median_ms": round(statistics.median(times), 3),
                        "max_ms": round(max(times), 3),
                        "model_calls": sum(v[1] for v in values),
                        "synthetic_tokens": sum(v[1] for v in values) * 120,
                        "external_cost_micro": 0,
                        "failures": 0,
                    }
                )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.system(),
                "storage": "SQLite",
                "scope": "Fixed host inputs/outputs; setup excluded; synthetic provider latency and fees zero; not a production model benchmark",
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
