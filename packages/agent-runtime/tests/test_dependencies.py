"""Persisted sibling dependencies gate execution and survive host replacement."""

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime import AgentRuntime, AgentSpec, ChildTask, Journal, ModelResult
from agent_runtime.journal import JournalIntegrityError, dump


class Model:
    def __init__(self):
        self.calls = []

    def generate(self, messages, *, request_id):
        self.calls.append((request_id, messages))
        return ModelResult(text=messages[0]["content"], usage_micro=0)


def graph(tmp_path, tasks):
    journal = Journal(tmp_path / "runtime.sqlite")
    model = Model()
    runtime = AgentRuntime(journal, model, None)
    root = runtime.start_with_children(
        "summary", AgentSpec("root", "1"), tasks, run_id="graph", tenant_id="tenant", join_policy="all_settled"
    )
    return runtime, model, root, runtime.children(root)


def test_dependencies_preserve_order_and_input_after_host_replacement(tmp_path):
    spec = AgentSpec("role", "1")
    runtime, model, root, children = graph(
        tmp_path, [ChildTask("A", spec), ChildTask("B", spec, depends_on=(0,)), ChildTask("C", spec, depends_on=(0, 1))]
    )
    assert runtime.resume(children[1])["state"] == "WAITING"
    assert model.calls == []
    runtime.resume(children[0])
    rebuilt = AgentRuntime(Journal(runtime.journal.path), model, None)
    assert rebuilt.resume(children[1])["state"] == "COMPLETED"
    assert rebuilt.resume(children[2])["state"] == "COMPLETED"
    dependencies = json.loads(model.calls[-1][1][-1]["content"])["dependency_results"]
    assert [row["output"] for row in dependencies] == ["A", "B"]
    assert runtime.resume(root)["state"] == "COMPLETED"
    assert len([event for event in runtime.events(children[1]) if event["type"] == "dependency.resolved"]) == 1


@pytest.mark.parametrize("dependencies", [(0,), (1,), (-1,), (0, 0)])
def test_invalid_forward_self_or_duplicate_edges_roll_back_the_whole_graph(tmp_path, dependencies):
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(journal, None, None)
    with pytest.raises(ValueError, match="earlier child"):
        runtime.start_with_children(
            "root",
            AgentSpec("root", "1"),
            [ChildTask("bad", AgentSpec("role", "1"), depends_on=dependencies)],
            run_id="bad",
            tenant_id="tenant",
        )
    assert runtime.get("bad") is None


@pytest.mark.parametrize("policy", ["all_success", "all_settled"])
def test_known_failure_policy_is_distinct_from_unknown_results(tmp_path, policy):
    spec = AgentSpec("role", "1")
    runtime, model, root, children = graph(
        tmp_path, [ChildTask("A", spec), ChildTask("B", spec, depends_on=(0,), dependency_policy=policy)]
    )
    runtime.signal(children[0], "cancel", {}, signal_id="cancel", actor="user")
    result = runtime.resume(children[1])
    assert result["state"] == ("COMPLETED" if policy == "all_settled" else "FAILED")
    assert len(model.calls) == (1 if policy == "all_settled" else 0)
    assert runtime.resume(root)["state"] == "COMPLETED"


def test_missing_edge_stops_execution_and_does_not_dispatch_a_model(tmp_path):
    spec = AgentSpec("role", "1")
    runtime, model, _root, children = graph(tmp_path, [ChildTask("A", spec), ChildTask("B", spec, depends_on=(0,))])
    runtime.resume(children[0])
    with runtime.journal.tx() as db:
        db.execute("DELETE FROM run_dependencies WHERE child_id=?", (children[1],))
    with pytest.raises(JournalIntegrityError, match="committed intent"):
        runtime.resume(children[1])
    assert len(model.calls) == 1


def test_refresh_never_dispatches_work_and_propagates_dependency_failure(tmp_path):
    spec = AgentSpec("role", "1")
    runtime, model, root, children = graph(
        tmp_path, [ChildTask("A", spec), ChildTask("B", spec, depends_on=(0,)), ChildTask("C", spec, depends_on=(1,))]
    )
    runtime.signal(children[0], "cancel", {}, signal_id="cancel", actor="user")
    before = runtime.refresh(root)
    assert runtime.get(children[1])["state"] == "FAILED"
    assert runtime.get(children[2])["state"] == "FAILED"
    assert before["state"] == "RUNNING" and model.calls == []
    assert runtime.refresh(root)["seq"] == before["seq"]


def test_two_workers_resolve_the_dependency_context_once(tmp_path):
    spec = AgentSpec("role", "1")
    runtime, _model, _root, children = graph(tmp_path, [ChildTask("A", spec), ChildTask("B", spec, depends_on=(0,))])
    runtime.resume(children[0])

    def refresh():
        return AgentRuntime(runtime.journal, None, None).refresh(children[1])

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: refresh(), range(2)))
    assert all(state["state"] == "RUNNING" for state in states)
    assert len([event for event in runtime.events(children[1]) if event["type"] == "dependency.resolved"]) == 1


def test_cancel_does_not_unlock_a_blocked_successor(tmp_path):
    class Unknown:
        def generate(self, messages, *, request_id):
            return ModelResult(text="saved", usage_micro=None)

    spec = AgentSpec("role", "1")
    runtime, model, root, children = graph(
        tmp_path, [ChildTask("A", spec), ChildTask("B", spec, depends_on=(0,), dependency_policy="all_settled")]
    )
    leaf = AgentRuntime(runtime.journal, Unknown(), None)
    assert leaf.resume(children[0])["state"] == "WAITING"
    assert runtime.resume(children[1])["state"] == "WAITING"
    runtime.signal(root, "cancel", {}, signal_id="cancel", actor="owner")
    step = leaf.get(children[0])["steps"][0]
    leaf.settle_model_uncertainty(children[0], step["id"], usage_micro=0, actor="provider")
    assert runtime.refresh(root)["state"] == "CANCELED"
    assert runtime.get(children[1])["state"] == "CANCELED" and model.calls == []


def test_graph_creation_retry_preserves_legacy_contract_identity(tmp_path):
    spec = AgentSpec("role", "1")
    tasks = [ChildTask("A", spec)]
    runtime, model, root, children = graph(tmp_path, tasks)
    from dataclasses import asdict

    old_child = asdict(tasks[0])
    old_child.pop("depends_on")
    old_child.pop("dependency_policy")
    request = {
        "prompt": "summary",
        "spec": asdict(AgentSpec("root", "1")),
        "children": [old_child],
        "messages": None,
        "tenant_id": "tenant",
        "join_policy": "all_settled",
    }
    with runtime.journal.tx() as db:
        db.execute(
            "UPDATE events SET data=? WHERE run_id=? AND type='graph.created'",
            (dump({"request_sha256": hashlib.sha256(dump(request).encode()).hexdigest()}), root),
        )
    assert (
        runtime.start_with_children(
            "summary", AgentSpec("root", "1"), tasks, run_id=root, tenant_id="tenant", join_policy="all_settled"
        )
        == root
    )
    assert runtime.children(root) == children and model.calls == []
    with pytest.raises(ValueError, match="another request"):
        runtime.start_with_children(
            "changed", AgentSpec("root", "1"), tasks, run_id=root, tenant_id="tenant", join_policy="all_settled"
        )
