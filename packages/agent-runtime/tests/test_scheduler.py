"""Shared logical tool permits across workers, cancellation and unknown effects."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import AgentRuntime, AgentSpec, Journal, ModelResult, RuntimeScheduler, ToolCall, ToolSpec


class Model:
    def __init__(self, name="publish"):
        self.name = name

    def generate(self, messages, *, request_id):
        if any(message["role"] == "tool" for message in messages):
            return ModelResult(text="done", usage_micro=1)
        return ModelResult(tool_call=ToolCall(self.name, {"job": messages[0]["content"]}), usage_micro=1)


class Tools:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def execute(self, call, *, effect_id):
        self.calls.append((effect_id, call.arguments["job"]))
        if call.arguments["job"] == "hold":
            self.entered.set()
            assert self.release.wait(30)
        return {"ok": call.arguments["job"]}

    def reconcile(self, call, *, effect_id):
        return None


def prepare(journal, tools, job, *, tenant="a", name="publish", resource=None):
    runtime = AgentRuntime(journal, Model(name), tools, worker_id=job)
    metadata = {"tool_resource_classes": {name: resource}} if resource else {}
    rid = runtime.start(
        job,
        AgentSpec("test", "1", tools=(ToolSpec(name, effect="external_effect"),), metadata=metadata),
        tenant_id=tenant,
    )
    assert runtime.tick(rid)
    return runtime, rid


def wake(journal, step):
    with journal.tx() as db:
        db.execute("UPDATE admission_waits SET retry_at=0 WHERE step_id=?", (step["id"],))


def test_global_capacity_unknown_effect_and_cancel_hold_until_resolution(tmp_path):
    journal = Journal(tmp_path / "shared.sqlite")
    RuntimeScheduler(journal).set_limit("tools", 1)
    tools = Tools()
    first, a = prepare(journal, tools, "hold")
    other, b = prepare(Journal(journal.path), tools, "next", tenant="b")
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.resume, a)
        try:
            assert tools.entered.wait(10)
            waiting = other.resume(b)
            step = waiting["steps"][-1]
            assert waiting["state"] == "WAITING" and step["state"] == "WAITING_ADMISSION"
            assert step["attempt_count"] == 0 and step["lease_until"] is None
            assert other.resume(b)["seq"] == waiting["seq"]
            assert [job for _, job in tools.calls] == ["hold"]
            first.signal(a, "cancel", {}, signal_id="cancel", actor="owner")
            tools.release.set()
            assert running.result(timeout=10)["state"] == "CANCELING"
            with journal.tx() as db:
                db.execute("UPDATE steps SET lease_until=0 WHERE run_id=? AND kind='tool'", (a,))
            wake(journal, step)
            assert other.resume(b)["steps"][-1]["state"] == "WAITING_ADMISSION"
            unknown = first.get(a)["steps"][-1]
            first.signal(
                a,
                "effect_resolution",
                {"step_id": unknown["id"], "outcome": "succeeded", "result": {"ok": "hold"}},
                signal_id="verified",
                actor="operator",
            )
            assert first.get(a)["state"] == "CANCELED"
            with journal.read_tx() as db:
                interrupted = db.execute(
                    "SELECT status,ended_at FROM attempts WHERE step_id=?", (unknown["id"],)
                ).fetchone()
            assert interrupted["status"] == "UNKNOWN" and interrupted["ended_at"] is not None
            wake(journal, step)
            completed = other.resume(b)
            assert completed["state"] == "COMPLETED"
            assert completed["steps"][1]["id"] == step["id"]
            assert completed["steps"][1]["effect_id"] == step["effect_id"]
            assert [job for _, job in tools.calls] == ["hold", "next"]
            journal.audit(a)
            journal.audit(b)
        finally:
            tools.release.set()


def test_default_tenant_limit_and_persisted_override(tmp_path):
    journal = Journal(tmp_path / "tenant.sqlite")
    RuntimeScheduler(journal).set_limit("tools", 1, tenant_default=True)
    tools = Tools()
    first, a = prepare(journal, tools, "hold", tenant="a")
    second, b = prepare(Journal(journal.path), tools, "same", tenant="a")
    third, c = prepare(Journal(journal.path), tools, "other", tenant="b")
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.resume, a)
        try:
            assert tools.entered.wait(10)
            waiting = second.resume(b)
            assert waiting["steps"][-1]["state"] == "WAITING_ADMISSION"
            assert third.resume(c)["state"] == "COMPLETED"
            RuntimeScheduler(Journal(journal.path)).set_limit("tools", 2, tenant_id="a")
            wake(journal, waiting["steps"][-1])
            assert second.resume(b)["state"] == "COMPLETED"
        finally:
            tools.release.set()
        assert running.result(timeout=10)["state"] == "COMPLETED"


def test_frozen_capability_class_shares_capacity_across_tool_names(tmp_path):
    journal = Journal(tmp_path / "classes.sqlite")
    RuntimeScheduler(journal).set_limit("class:office", 1)
    tools = Tools()
    first, a = prepare(journal, tools, "hold", name="render_word", resource="class:office")
    second, b = prepare(Journal(journal.path), tools, "ppt", tenant="b", name="render_ppt", resource="class:office")
    third, c = prepare(Journal(journal.path), tools, "calculator", resource="class:math")
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.resume, a)
        try:
            assert tools.entered.wait(10)
            waiting = second.resume(b)
            assert waiting["steps"][-1]["state"] == "WAITING_ADMISSION"
            event = next(event for event in journal.events(b) if event["type"] == "scheduler.admission_wait")
            assert event["data"]["resource"] == "class:office"
            assert third.resume(c)["state"] == "COMPLETED"
            second.signal(b, "cancel", {}, signal_id="cancel-before-dispatch", actor="owner")
            assert second.get(b)["state"] == "CANCELED"
            assert "ppt" not in [job for _, job in tools.calls]
        finally:
            tools.release.set()
        assert running.result(timeout=10)["state"] == "COMPLETED"


_PROCESS_GATE = None
_PROCESS_ENTERED = None
_PROCESS_RELEASE = None


def _init_processes(gate, entered, release):
    global _PROCESS_GATE, _PROCESS_ENTERED, _PROCESS_RELEASE
    _PROCESS_GATE, _PROCESS_ENTERED, _PROCESS_RELEASE = gate, entered, release


def _claim_tool_process(args):
    import os

    path, schema, rid, receipts = args
    journal = Journal(path, schema=schema)

    class ProcessTools:
        def execute(self, call, *, effect_id):
            Path(receipts, str(os.getpid()) + ".txt").write_text(effect_id, encoding="utf-8")
            _PROCESS_ENTERED.set()
            assert _PROCESS_RELEASE.wait(30)
            return {"ok": True}

    _PROCESS_GATE.wait(timeout=30)
    return AgentRuntime(journal, Model(), ProcessTools(), worker_id="process-" + str(os.getpid())).resume(rid)["state"]


def test_real_processes_observe_shared_tool_permit(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor, wait

    journal = Journal(tmp_path / "process-tools.sqlite")
    RuntimeScheduler(journal).set_limit("tools", 1)
    runs = [prepare(journal, Tools(), f"process-{index}", tenant=str(index))[1] for index in range(4)]
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    spawn = multiprocessing.get_context("spawn")
    gate, entered, release = spawn.Barrier(4), spawn.Event(), spawn.Event()
    with ProcessPoolExecutor(
        max_workers=4, mp_context=spawn, initializer=_init_processes, initargs=(gate, entered, release)
    ) as pool:
        futures = [pool.submit(_claim_tool_process, (journal.path, journal.schema, rid, str(receipts))) for rid in runs]
        try:
            assert entered.wait(15)
            done, pending = wait(futures, timeout=5)
            assert len(done) == 3 and len(pending) == 1
            assert all(future.result() == "WAITING" for future in done)
            assert len(list(receipts.glob("*.txt"))) == 1
        finally:
            release.set()
        states = [future.result(timeout=15) for future in futures]
    assert states.count("COMPLETED") == 1 and states.count("WAITING") == 3
    assert len(list(receipts.glob("*.txt"))) == 1
    restored = AgentRuntime(Journal(journal.path), Model(), Tools())
    for rid in runs:
        current = restored.get(rid)
        if current["state"] == "WAITING":
            step = current["steps"][-1]
            assert step["state"] == "WAITING_ADMISSION" and step["attempt_count"] == 0
            wake(journal, step)
            assert restored.resume(rid)["state"] == "COMPLETED"
        journal.audit(rid)


def test_resource_wait_does_not_extend_tool_approval(tmp_path, monkeypatch):
    import hashlib

    from agent_runtime import engine
    from agent_runtime.journal import dump, now_ms

    journal = Journal(tmp_path / "resource-approval.sqlite")
    scheduler = RuntimeScheduler(journal)
    scheduler.set_limit("tools", 0)
    tools = Tools()
    runtime = AgentRuntime(journal, Model(), tools)
    rid = runtime.start(
        "next", AgentSpec("test", "1", tools=(ToolSpec("publish", effect="external_effect", requires_approval=True),))
    )
    waiting = runtime.resume(rid)
    step = waiting["steps"][-1]
    assert step["state"] == "WAITING_APPROVAL"
    runtime.signal(
        rid,
        "approval",
        {
            "step_id": step["id"],
            "approved": True,
            "arguments_sha256": hashlib.sha256(dump(step["payload"]).encode()).hexdigest(),
        },
        signal_id="grant",
        actor="owner",
    )
    assert runtime.resume(rid)["steps"][-1]["state"] == "WAITING_ADMISSION"
    scheduler.set_limit("tools", 1)
    future = now_ms() + 86401000
    monkeypatch.setattr(engine, "now_ms", lambda: future)
    assert runtime.resume(rid)["steps"][-1]["state"] == "WAITING_APPROVAL"
    assert tools.calls == []
    journal.audit(rid)


def test_missing_shared_policy_never_means_unlimited_dispatch(tmp_path):
    from agent_runtime.journal import JournalIntegrityError

    journal = Journal(tmp_path / "missing-policy.sqlite")
    tools = Tools()
    runtime, rid = prepare(journal, tools, "next")
    step = runtime.get(rid)["steps"][-1]
    with journal.tx() as db:
        db.execute("DELETE FROM resource_limits WHERE scope='global' AND resource='tools'")
    with pytest.raises(JournalIntegrityError, match="policy is missing"):
        runtime.resume(rid)
    restarted = AgentRuntime(Journal(journal.path), Model(), tools)
    with pytest.raises(JournalIntegrityError, match="policy is missing"):
        restarted.resume(rid)
    assert tools.calls == [] and runtime.get(rid)["steps"][-1]["attempt_count"] == 0
    RuntimeScheduler(journal).set_limit("tools", 32)
    assert runtime.resume(rid)["state"] == "COMPLETED"
    assert runtime.get(rid)["steps"][1]["id"] == step["id"]


def test_legacy_canceled_attempt_repair_is_durable_and_does_not_reexecute(tmp_path):
    from agent_runtime.telemetry import RuntimeTelemetry

    journal = Journal(tmp_path / "old-attempt.sqlite")
    tools = Tools()
    runtime, rid = prepare(journal, tools, "next")
    # Construct an old committed cancellation boundary without modern attempt closure.
    with journal.tx() as db:
        step = db.execute("SELECT id FROM steps WHERE run_id=? AND kind='tool'", (rid,)).fetchone()
        db.execute(
            "INSERT INTO attempts(id,step_id,epoch,status,started_at) VALUES(?,?,1,'IN_FLIGHT',1)",
            ("legacy-attempt", step["id"]),
        )
        db.execute("UPDATE steps SET attempt_count=1,lease_epoch=1 WHERE id=?", (step["id"],))
        journal.event(db, rid, "attempt.started", {"attempt_id": "legacy-attempt", "step_id": step["id"], "epoch": 1})
        journal.set_step(db, step["id"], "RECONCILING")
        journal.set_run(db, rid, "CANCELING")
    frozen = runtime.get(rid)["spec"]
    reopened = Journal(journal.path)
    state = AgentRuntime(reopened, Model(), tools).get(rid)
    assert state["state"] == "CANCELING" and state["spec"] == frozen
    assert state["steps"][-1]["state"] == "RECONCILING" and tools.calls == []
    with reopened.read_tx() as db:
        attempt = db.execute("SELECT status,ended_at FROM attempts WHERE id='legacy-attempt'").fetchone()
    assert attempt["status"] == "UNKNOWN" and attempt["ended_at"] is not None
    seq = state["seq"]
    assert AgentRuntime(Journal(journal.path), Model(), tools).get(rid)["seq"] == seq

    class Sink:
        spans = []

        def export(self, spans):
            self.spans.extend(spans)
            return True

    sink = Sink()
    RuntimeTelemetry(reopened).export_attempts(sink, consumer_id="repaired", tenant_id="a")
    assert any(span.attempt_id == "legacy-attempt" and span.status == "UNKNOWN" for span in sink.spans)
    reopened.audit(rid)


def test_legacy_repair_does_not_trust_orphan_attempt_without_committed_start(tmp_path):
    from agent_runtime.journal import JournalIntegrityError

    journal = Journal(tmp_path / "corrupt-attempt.sqlite")
    tools = Tools()
    runtime, rid = prepare(journal, tools, "next")
    step = runtime.get(rid)["steps"][-1]
    with journal.tx() as db:
        db.execute(
            "INSERT INTO attempts(id,step_id,epoch,status,started_at) VALUES('orphan',?,1,'IN_FLIGHT',1)", (step["id"],)
        )
    reopened = Journal(journal.path)
    with pytest.raises(JournalIntegrityError, match="attempt identities"):
        AgentRuntime(reopened, Model(), tools).resume(rid)
    assert tools.calls == []
    with reopened.read_tx() as db:
        row = db.execute("SELECT status,ended_at FROM attempts WHERE id='orphan'").fetchone()
    assert row["status"] == "IN_FLIGHT" and row["ended_at"] is None


def test_unsupported_policy_version_cannot_dispatch(tmp_path):
    from agent_runtime.journal import JournalIntegrityError

    journal = Journal(tmp_path / "policy-version.sqlite")
    tools = Tools()
    runtime, rid = prepare(journal, tools, "next")
    with journal.tx() as db:
        db.execute("UPDATE resource_policy_meta SET version=2 WHERE id=1")
    with pytest.raises(JournalIntegrityError, match="unsupported"):
        runtime.resume(rid)
    assert tools.calls == [] and runtime.get(rid)["steps"][-1]["attempt_count"] == 0
