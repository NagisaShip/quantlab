"""Structured fork/join survives restart and shares budget ownership."""

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import AgentRuntime, AgentSpec, ChildTask, Journal, ModelResult, ToolCall, ToolSpec
from agent_runtime.contracts import ModelUnavailable
from agent_runtime.journal import JournalIntegrityError


class Model:
    def generate(self, messages, *, request_id):
        return ModelResult(text="answer:" + messages[0]["content"], usage_micro=1)


def test_join_reopens_and_does_not_run_children_through_parent_ports(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    parent = AgentRuntime(journal, Model(), None)
    run_id = parent.start("parent", AgentSpec("parent", "1"), tenant_id="tenant")
    requests = [("first", AgentSpec("one", "1")), ("second", AgentSpec("two", "1"))]
    children = parent.fork_children(run_id, requests, idempotency_key="group")
    assert parent.fork_children(run_id, requests, idempotency_key="group") == children
    assert parent.resume(run_id)["state"] == "WAITING"
    assert all(journal.run(child)["tenant_id"] == "tenant" for child in children)
    assert all(journal.run(child)["parent_id"] == run_id for child in children)
    with pytest.raises(ValueError, match="another request"):
        parent.fork_children(run_id, [("changed", AgentSpec("one", "1"))], idempotency_key="group")
    AgentRuntime(Journal(journal.path), Model(), None).resume(children[1])
    assert parent.resume(run_id)["state"] == "WAITING"
    AgentRuntime(Journal(journal.path), Model(), None).resume(children[0])
    result = AgentRuntime(Journal(journal.path), Model(), None).resume(run_id)
    assert result["state"] == "COMPLETED"
    messages = result["messages"]
    assert "answer:first" in messages[1]["content"]
    assert messages[1]["content"].index(children[0]) < messages[1]["content"].index(children[1])
    seq = result["seq"]
    assert parent.resume(run_id)["seq"] == seq
    for item in [run_id, *children]:
        journal.audit(item)


def test_children_atomically_share_parent_budget_under_concurrency(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    entered, release = threading.Event(), threading.Event()

    class Blocking:
        def generate(self, messages, *, request_id):
            entered.set()
            assert release.wait(5)
            return ModelResult(text="child", usage_micro=1)

    parent = AgentRuntime(journal, Model(), None)
    run_id = parent.start("parent", AgentSpec("parent", "1", budget_limit_micro=3, model_reservation_micro=1))
    spec = AgentSpec("child", "1", model_reservation_micro=2)
    children = parent.fork_children(run_id, [("one", spec), ("two", spec)], idempotency_key="budget")
    with ThreadPoolExecutor() as pool:
        first = pool.submit(AgentRuntime(journal, Blocking(), None).resume, children[0])
        assert entered.wait(5)
        blocked = AgentRuntime(journal, Model(), None).resume(children[1])
        assert blocked["steps"][0]["state"] == "WAITING_ADMISSION"
        assert journal.budget("run:" + run_id)["reserved_micro"] == 2
        release.set()
        assert first.result()["state"] == "COMPLETED"
    assert AgentRuntime(journal, Model(), None).resume(children[1])["state"] == "COMPLETED"
    assert parent.resume(run_id)["state"] == "COMPLETED"
    assert journal.budget("run:" + run_id)["spent_micro"] == 3
    assert journal.budget("run:" + run_id)["reserved_micro"] == 0


def test_failed_child_does_not_let_parent_claim_success(tmp_path):
    class Failing:
        def generate(self, messages, *, request_id):
            raise ModelUnavailable("rejected before dispatch")

    journal = Journal(tmp_path / "runtime.sqlite")
    parent = AgentRuntime(journal, Model(), None)
    run_id = parent.start("parent", AgentSpec("parent", "1"))
    children = parent.fork_children(run_id, [("child", AgentSpec("child", "1"))], idempotency_key="failure")
    AgentRuntime(journal, Failing(), None).resume(children[0])
    assert parent.resume(run_id)["state"] == "FAILED"


def test_cancel_tree_keeps_unknown_nested_effects_until_reconciled(tmp_path):
    class Calling:
        def generate(self, messages, *, request_id):
            return ModelResult(tool_call=ToolCall("publish", {}), usage_micro=0)

    effects = []

    class Tool:
        def execute(self, call, *, effect_id):
            effects.append(effect_id)
            raise OSError("effect happened, reply lost")

        def reconcile(self, call, *, effect_id):
            return True, "published"

    journal = Journal(tmp_path / "runtime.sqlite")
    parent = AgentRuntime(journal, Model(), None)
    run_id = parent.start("parent", AgentSpec("parent", "1"))
    child = parent.fork_children(run_id, [("child", AgentSpec("child", "1"))], idempotency_key="outer")[0]
    grandchild = parent.fork_children(
        child,
        [("grandchild", AgentSpec("grandchild", "1", tools=(ToolSpec("publish", effect="external_effect"),)))],
        idempotency_key="inner",
    )[0]
    leaf = AgentRuntime(journal, Calling(), Tool())
    assert leaf.resume(grandchild)["state"] == "WAITING"
    parent.signal(run_id, "cancel", {}, signal_id="cancel", actor="owner")
    assert parent.resume(run_id)["state"] == "CANCELING"
    assert journal.run(child)["state"] == "CANCELING"
    step = leaf.get(grandchild)["steps"][1]
    assert leaf.reconcile_effect(grandchild, step["id"], actor="external-status")
    assert parent.resume(run_id)["state"] == "CANCELED"
    assert journal.run(child)["state"] == "CANCELED"
    assert len(effects) == 1
    for item in [run_id, child, grandchild]:
        journal.audit(item)


def test_fork_is_atomic_and_rejects_zero_estimate_with_inherited_budget(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start("parent", AgentSpec("parent", "1", budget_limit_micro=10, model_reservation_micro=1))
    before = runtime.get(run_id)
    with pytest.raises(ValueError, match="reservation"):
        runtime.fork_children(run_id, [("child", AgentSpec("child", "1"))], idempotency_key="unsafe")
    original = runtime._start

    def fail(db, prompt, spec, **kwargs):
        original(db, prompt, spec, **kwargs)
        raise OSError("transaction lost before fork commit")

    monkeypatch.setattr(runtime, "_start", fail)
    with pytest.raises(OSError, match="transaction lost"):
        runtime.fork_children(
            run_id, [("child", AgentSpec("child", "1", model_reservation_micro=1))], idempotency_key="atomic"
        )
    assert runtime.get(run_id) == before
    with journal.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1
        assert db.execute("SELECT COUNT(*) AS n FROM run_joins").fetchone()["n"] == 0


def test_missing_join_member_stops_recovery_instead_of_claiming_success(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start("parent", AgentSpec("parent", "1"))
    children = runtime.fork_children(run_id, [("one", AgentSpec("one", "1"))], idempotency_key="group")
    with journal.tx() as db:
        db.execute("DELETE FROM run_join_children WHERE child_id=?", (children[0],))
    with pytest.raises(JournalIntegrityError, match="committed fork"):
        runtime.resume(run_id)
    assert journal.run(run_id)["state"] == "WAITING"


def test_two_workers_fork_once_at_the_same_boundary(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start("parent", AgentSpec("parent", "1"))

    def fork():
        return AgentRuntime(journal, None, None).fork_children(
            run_id, [("one", AgentSpec("one", "1")), ("two", AgentSpec("two", "1"))], idempotency_key="same"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fork) for _ in range(2)]
        assert futures[0].result() == futures[1].result()
    with journal.connection() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 3


def test_parent_join_survives_real_process_exit(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    parent = AgentRuntime(journal, Model(), None)
    run_id = parent.start("parent", AgentSpec("parent", "1"))
    requests = [("one", AgentSpec("one", "1")), ("two", AgentSpec("two", "1"))]
    children = parent.fork_children(run_id, requests, idempotency_key="process")
    script = """
import os,sys
from agent_runtime import AgentRuntime,Journal,ModelResult
class Model:
    def generate(self,messages,*,request_id):
        return ModelResult(text="saved in child process",usage_micro=1)
journal=Journal(os.environ.get("QAGENT_TEST_RUNTIME_DB") or sys.argv[1],schema=os.environ.get("QAGENT_TEST_RUNTIME_SCHEMA","agent_runtime"))
AgentRuntime(journal,Model(),None).resume(sys.argv[2])
os._exit(29)
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.run(
        [sys.executable, "-c", script, journal.path, children[0]], env=env, capture_output=True, timeout=30
    )
    assert process.returncode == 29, process.stderr.decode(errors="replace")
    assert parent.resume(run_id)["state"] == "WAITING"
    assert parent.fork_children(run_id, requests, idempotency_key="process") == children
    assert journal.run(children[0])["output"] == "saved in child process"
    AgentRuntime(journal, Model(), None).resume(children[1])
    assert AgentRuntime(Journal(journal.path), Model(), None).resume(run_id)["state"] == "COMPLETED"


def test_child_system_and_image_context_is_durable(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start("parent", AgentSpec("parent", "1"))
    messages = [
        {"role": "system", "content": "reviewer rules"},
        {"role": "user", "content": "inspect", "images": ["data:image/png;base64,saved"]},
    ]
    child = runtime.fork_children(
        run_id, [ChildTask("inspect", AgentSpec("reviewer", "1"), messages)], idempotency_key="context"
    )[0]

    class Reviewer:
        def generate(self, saved, *, request_id):
            assert saved == messages
            return ModelResult(text="checked", usage_micro=0)

    assert AgentRuntime(Journal(journal.path), Reviewer(), None).resume(child)["state"] == "COMPLETED"
    assert runtime.children(run_id) == [child]


@pytest.mark.parametrize("limit", ["max_children", "max_descendants", "max_child_depth"])
def test_zero_parent_bound_disables_fork_atomically(tmp_path, limit):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start("parent", AgentSpec("parent", "1", **{limit: 0}))
    before = runtime.get(run_id)
    with pytest.raises(ValueError, match="maximum"):
        runtime.fork_children(run_id, [("child", AgentSpec("child", "1"))], idempotency_key="limited")
    assert runtime.get(run_id) == before


@pytest.mark.parametrize("limit", ["max_descendants", "max_child_depth"])
def test_nested_fork_cannot_bypass_an_ancestor_bound(tmp_path, limit):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start("parent", AgentSpec("parent", "1", **{limit: 1}))
    child = runtime.fork_children(run_id, [("child", AgentSpec("child", "1"))], idempotency_key="outer")[0]
    with pytest.raises(ValueError, match="maximum"):
        runtime.fork_children(child, [("grandchild", AgentSpec("grandchild", "1"))], idempotency_key="inner")
    assert runtime.children(child) == []


@pytest.mark.parametrize("corruption", ["cycle", "tenant"])
def test_invalid_child_tree_cannot_propagate_cancel(tmp_path, corruption):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start("parent", AgentSpec("parent", "1"))
    child = runtime.fork_children(run_id, [("child", AgentSpec("child", "1"))], idempotency_key="outer")[0]
    with journal.tx() as db:
        if corruption == "cycle":
            db.execute("UPDATE runs SET parent_id=? WHERE id=?", (child, run_id))
        else:
            db.execute("UPDATE runs SET tenant_id='another' WHERE id=?", (child,))
    with pytest.raises(JournalIntegrityError):
        runtime.signal(run_id, "cancel", {}, signal_id="cancel", actor="owner")
    assert journal.run(child)["state"] == "RUNNING"


def test_fresh_journal_can_be_initialized_by_multiple_hosts(tmp_path):
    path = tmp_path / "fresh.sqlite"

    def host(index):
        journal = Journal(path)
        runtime = AgentRuntime(journal, Model(), None)
        run_id = runtime.start(str(index), AgentSpec("test", "1"))
        return runtime.resume(run_id)["state"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(host, range(4))) == ["COMPLETED"] * 4


def test_parent_cancel_retains_child_unsettled_model_reservation(tmp_path):
    class Unsettled:
        def generate(self, messages, *, request_id):
            return ModelResult(text="saved response", usage_micro=None)

    journal = Journal(tmp_path / "runtime.sqlite")
    parent = AgentRuntime(journal, Model(), None)
    run_id = parent.start("parent", AgentSpec("parent", "1", budget_limit_micro=10, model_reservation_micro=1))
    child = parent.fork_children(
        run_id, [("child", AgentSpec("child", "1", model_reservation_micro=3))], idempotency_key="billing"
    )[0]
    leaf = AgentRuntime(journal, Unsettled(), None)
    assert leaf.resume(child)["state"] == "WAITING"
    step_id = leaf.get(child)["steps"][0]["id"]
    parent.signal(run_id, "cancel", {}, signal_id="cancel-billing", actor="owner")
    assert parent.resume(run_id)["state"] == "CANCELING"
    assert journal.budget("run:" + run_id)["reserved_micro"] == 3
    assert leaf.settle_model_uncertainty(child, step_id, usage_micro=2, actor="provider")
    assert parent.resume(run_id)["state"] == "CANCELED"
    assert journal.budget("run:" + run_id)["reserved_micro"] == 0
    assert journal.budget("run:" + run_id)["spent_micro"] == 2


def test_corrupt_join_cannot_resurrect_a_completed_parent(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    run_id = runtime.start("parent", AgentSpec("parent", "1"))
    child = runtime.fork_children(run_id, [("child", AgentSpec("child", "1"))], idempotency_key="group")[0]
    runtime.resume(child)
    assert runtime.resume(run_id)["state"] == "COMPLETED"
    with journal.tx() as db:
        db.execute("UPDATE run_joins SET state='WAITING' WHERE parent_id=?", (run_id,))
    with pytest.raises(JournalIntegrityError, match="disagrees"):
        runtime.resume(run_id)
    assert runtime.get(run_id)["state"] == "COMPLETED"


def test_initial_graph_is_atomic_and_repeated_creation_keeps_original_children(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    parent_spec = AgentSpec("parent", "1", budget_limit_micro=10, model_reservation_micro=1)
    with pytest.raises(ValueError, match="reservation"):
        runtime.start_with_children(
            "parent", parent_spec, [ChildTask("child", AgentSpec("child", "1"))], run_id="graph", tenant_id="tenant"
        )
    assert runtime.get("graph") is None
    children = [ChildTask("child", AgentSpec("child", "1", model_reservation_micro=2))]
    assert runtime.start_with_children("parent", parent_spec, children, run_id="graph", tenant_id="tenant") == "graph"
    ids = runtime.children("graph")
    assert runtime.start_with_children("parent", parent_spec, children, run_id="graph", tenant_id="tenant") == "graph"
    assert runtime.children("graph") == ids
    with pytest.raises(ValueError, match="another request"):
        runtime.start_with_children("changed", parent_spec, children, run_id="graph", tenant_id="tenant")


def test_all_settled_join_waits_for_unknown_child_and_then_includes_known_failure(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    root = runtime.start_with_children(
        "parent",
        AgentSpec("parent", "1"),
        [ChildTask("one", AgentSpec("one", "1")), ChildTask("two", AgentSpec("two", "1"))],
        run_id="settled",
        tenant_id="tenant",
        join_policy="all_settled",
    )
    children = runtime.children(root)
    runtime.signal(children[0], "cancel", {}, signal_id="cancel-child", actor="user")
    assert runtime.resume(root)["state"] == "WAITING"
    runtime.resume(children[1])
    result = runtime.resume(root)
    assert result["state"] == "COMPLETED" and '"state":"CANCELED"' in result["messages"][1]["content"]


def test_legacy_join_request_retries_keep_the_same_identity(tmp_path):
    from agent_runtime.journal import dump, load

    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, Model(), None)
    root = runtime.start("parent", AgentSpec("parent", "1"))
    children = [("child", AgentSpec("child", "1"))]
    ids = runtime.fork_children(root, children, idempotency_key="legacy")
    with journal.tx() as db:
        request = load(db.execute("SELECT request FROM run_joins WHERE parent_id=?", (root,)).fetchone()["request"])
        db.execute("UPDATE run_joins SET request=? WHERE parent_id=?", (dump(request["children"]), root))
    assert runtime.fork_children(root, children, idempotency_key="legacy") == ids
    with pytest.raises(ValueError, match="another request"):
        runtime.fork_children(root, children, idempotency_key="legacy", join_policy="all_settled")
