"""Boundary faults that span signals, batches, cancellation and lease ownership."""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import AgentRuntime, AgentSpec, Journal, ModelResult, ToolCall, ToolSpec


class FinalModel:
    def generate(self, messages, *, request_id):
        return ModelResult(text="done", usage_micro=0)


class NoTool:
    def execute(self, call, *, effect_id):
        raise AssertionError("unexpected tool")

    def reconcile(self, call, *, effect_id):
        return None


def test_signal_identity_cannot_be_reused_for_another_actor_payload_or_run(tmp_path):
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), FinalModel(), NoTool())
    first = runtime.start("one", AgentSpec("demo", "1"))
    second = runtime.start("two", AgentSpec("demo", "1"))
    assert runtime.signal(first, "cancel", {}, signal_id="signal", actor="alice")
    assert not runtime.signal(first, "cancel", {}, signal_id="signal", actor="alice")
    for run_id, actor, data in [(first, "bob", {}), (first, "alice", {"reason": "changed"}), (second, "alice", {})]:
        with pytest.raises(ValueError, match="different request"):
            runtime.signal(run_id, "cancel", data, signal_id="signal", actor=actor)
    assert runtime.get(second)["state"] == "RUNNING"


def test_long_call_renews_lease_and_competitor_cannot_claim(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class SlowModel:
        def generate(self, messages, *, request_id):
            entered.set()
            assert release.wait(5)
            return ModelResult(text="done", usage_micro=0)

    journal = Journal(tmp_path / "runtime.sqlite")
    first = AgentRuntime(journal, SlowModel(), NoTool(), worker_id="first", lease_ms=300)
    second = AgentRuntime(Journal(journal.path), FinalModel(), NoTool(), worker_id="second")
    run_id = first.start("slow", AgentSpec("demo", "1"))
    result = []
    worker = threading.Thread(target=lambda: result.append(first.resume(run_id)))
    worker.start()
    try:
        assert entered.wait(5)
        time.sleep(0.7)  # More than two leases: only heartbeats keep this attempt valid.
        assert second.tick(run_id) is False
        assert second.get(run_id)["steps"][0]["attempt_count"] == 1
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert result[0]["state"] == "COMPLETED"


def test_expired_lease_rejects_result_even_without_a_successor(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")

    class ExpiringModel:
        def generate(self, messages, *, request_id):
            with journal.tx() as db:
                db.execute("UPDATE steps SET lease_until=0 WHERE id=?", (request_id,))
            return ModelResult(text="must not commit", usage_micro=0)

    runtime = AgentRuntime(journal, ExpiringModel(), NoTool())
    run_id = runtime.start("expire", AgentSpec("demo", "1"))
    assert not runtime.tick(run_id)
    assert runtime.get(run_id)["output"] is None
    assert runtime.resume(run_id)["state"] == "WAITING"
    with journal.connection() as db:
        assert db.execute("SELECT status FROM attempts").fetchone()["status"] == "UNKNOWN"


def test_batch_step_limit_rejects_all_tools_before_any_effect(tmp_path):
    class BatchModel:
        def generate(self, messages, *, request_id):
            return ModelResult(tool_calls=[ToolCall("write", {}), ToolCall("write", {})], usage_micro=0)

    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), BatchModel(), NoTool())
    run_id = runtime.start("batch", AgentSpec("demo", "1", tools=(ToolSpec("write"),), max_steps=2))
    assert runtime.resume(run_id)["state"] == "FAILED"
    assert len(runtime.get(run_id)["steps"]) == 1


def test_tool_budget_cannot_be_exceeded_by_a_single_multi_call_response(tmp_path):
    class BatchModel:
        def generate(self, messages, *, request_id):
            return ModelResult(tool_calls=[ToolCall("write", {}), ToolCall("write", {})], usage_micro=0)

    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), BatchModel(), NoTool())
    run_id = runtime.start("batch", AgentSpec("demo", "1", tools=(ToolSpec("write"),), max_steps=100, max_tool_calls=1))
    assert runtime.resume(run_id)["error"] == "max_tool_calls exceeded"
    assert len(runtime.get(run_id)["steps"]) == 1


def test_cancel_batch_prevents_remaining_tools_and_reconciles_only_dispatched_effect(tmp_path):
    class BatchModel:
        def generate(self, messages, *, request_id):
            return ModelResult(tool_calls=[ToolCall("write", {"n": 1}), ToolCall("write", {"n": 2})], usage_micro=0)

    calls = []

    class CancelingTool:
        def execute(self, call, *, effect_id):
            calls.append(effect_id)
            runtime.signal(run_id, "cancel", {}, signal_id="cancel", actor="user")
            return "external success"

        def reconcile(self, call, *, effect_id):
            return True, "external success"

    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), BatchModel(), CancelingTool())
    run_id = runtime.start("batch", AgentSpec("demo", "1", tools=(ToolSpec("write", effect="external_effect"),)))
    assert runtime.resume(run_id)["state"] == "CANCELING"
    assert len(calls) == 1
    steps = runtime.get(run_id)["steps"]
    assert steps[1]["state"] == "RECONCILING" and steps[2]["state"] == "CANCELED"
    assert runtime.reconcile_effect(run_id, steps[1]["id"], actor="external-status")
    assert runtime.get(run_id)["state"] == "CANCELED"


@pytest.mark.parametrize("outcome", ["succeeded", "absent"])
def test_manual_effect_resolution_is_a_durable_idempotent_signal(tmp_path, outcome):
    class CallingModel:
        def generate(self, messages, *, request_id):
            if any(message["role"] == "tool" for message in messages):
                return ModelResult(text="done", usage_micro=0)
            return ModelResult(tool_call=ToolCall("write", {}), usage_micro=0)

    effects = []

    class UncertainTool:
        def execute(self, call, *, effect_id):
            effects.append(effect_id)
            if len(effects) == 1:
                raise TimeoutError("result unknown")
            return "confirmed result"

        def reconcile(self, call, *, effect_id):
            return None

    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, CallingModel(), UncertainTool())
    run_id = runtime.start("write", AgentSpec("demo", "1", tools=(ToolSpec("write", effect="external_effect"),)))
    assert runtime.resume(run_id)["state"] == "WAITING"
    step_id = runtime.get(run_id)["steps"][-1]["id"]
    resolution = {"step_id": step_id, "outcome": outcome, "result": "confirmed result"}
    assert runtime.signal(run_id, "effect_resolution", resolution, signal_id="resolution", actor="admin")
    assert not runtime.signal(run_id, "effect_resolution", resolution, signal_id="resolution", actor="admin")
    assert runtime.resume(run_id)["state"] == "COMPLETED"
    assert len(effects) == (1 if outcome == "succeeded" else 2)
    assert len(set(effects)) == 1


def test_checkpoint_compaction_retains_fallback_and_all_execution_facts(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, FinalModel(), NoTool())
    run_id = runtime.start("run", AgentSpec("demo", "1"))
    journal.checkpoint(run_id)
    with journal.tx() as db:
        journal.event(db, run_id, "test.marker", {"marker": 1})
    older = journal.checkpoint(run_id)
    assert runtime.resume(run_id)["state"] == "COMPLETED"
    latest = journal.checkpoint(run_id)
    with journal.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM checkpoints WHERE run_id=?", (run_id,)).fetchone()["n"] == 2
        db.execute("UPDATE checkpoints SET state='{}' WHERE run_id=? AND seq=?", (run_id, latest))
    audit = journal.audit(run_id)
    assert audit["checkpoint_seq"] == older
    assert audit["state"]["run"] == "COMPLETED"
    events = runtime.events(run_id)
    assert [event["seq"] for event in events] == list(range(1, latest + 1))
