"""Opt-in ingress/projection keeps QuantLab's old worker out of Runtime runs."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from agent_runtime import AgentRuntime, AgentSpec, Journal, ModelResult

from quant_agent.api import routes_runs
from quant_agent.platform.db import Database, DatabaseError
from quant_agent.platform.errors import AppError
from quant_agent.platform.runs import RunService
from quant_agent.platform.runtime_bridge import RuntimeBridge


class Model:
    def generate(self, messages, *, request_id):
        return ModelResult(text="runtime answer", usage_micro=0)


class Tools:
    def execute(self, call, *, effect_id):
        raise AssertionError("no tools configured")

    def reconcile(self, call, *, effect_id):
        return None


def test_opt_in_run_has_one_execution_authority_and_monotonic_projection(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{(tmp_path / 'quantlab.sqlite').as_posix()}")
    db.migrate()
    db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "user@example.test", "User", "hash", 1),
    )
    db.execute("INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("ws", "Workspace", "user", 1))
    runs = RunService(db)
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), Model(), Tools())
    bridge = RuntimeBridge(runs, runtime, spec_loader=lambda data: AgentSpec(**data))
    spec = {"name": "demo", "version": "1"}
    ticket = bridge.create(
        workspace_id="ws", user_id="user", title="Agent", prompt="hello", spec=spec, idempotency_key="one"
    )
    again = bridge.create(
        workspace_id="ws", user_id="user", title="Agent", prompt="hello", spec=spec, idempotency_key="one"
    )
    assert ticket["id"] == again["id"]
    with pytest.raises(ValueError, match="different runtime input"):
        bridge.create(
            workspace_id="ws", user_id="user", title="Agent", prompt="changed", spec=spec, idempotency_key="one"
        )
    assert ticket["runtime_version"] == "0.1.0"
    assert runs.claim("old-worker") is None
    legacy = runs.create(workspace_id="ws", user_id="user", kind="noop", mode="analysis")
    assert runs.claim("old-worker", kinds=["noop"])["id"] == legacy["id"]
    assert bridge.deliver(ticket["id"])
    assert not bridge.deliver(ticket["id"])
    assert runtime.resume(ticket["id"])["state"] == "COMPLETED"
    assert bridge.strong_get(ticket["id"], workspace_id="ws")["state"] == "COMPLETED"
    assert bridge.project_active(workspace_id="ws") == 1
    projected = bridge.project(ticket["id"])
    assert projected["state"] == "COMPLETED"
    seq = projected["runtime_event_seq"]
    assert seq == runtime.get(ticket["id"])["seq"]
    assert bridge.project(ticket["id"])["runtime_event_seq"] == seq
    assert runs.set_state(ticket["id"], "old-worker", "FAILED") is False
    with pytest.raises(AppError):
        runs.command(ticket["id"], "ws", "user", "cancel")
    with pytest.raises(AppError):
        bridge.strong_get(ticket["id"], workspace_id="other")
    monkeypatch.setattr(routes_runs, "get_services", lambda: SimpleNamespace(runs=runs, runtime_bridge=bridge))
    monkeypatch.setattr(routes_runs.context, "current_workspace", lambda: "ws")
    monkeypatch.setattr(routes_runs.context, "current", lambda: SimpleNamespace(user_id="user"))
    assert routes_runs.get_run(ticket["id"])["run"]["runtime_event_seq"] == seq
    assert routes_runs.run_events(ticket["id"], limit=500)["items"][-1]["data"]["runtime_seq"] == seq
    pending = bridge.create(
        workspace_id="ws", user_id="user", title="Cancel", prompt="stop", spec=spec, idempotency_key="two"
    )
    bridge.deliver(pending["id"])
    command = routes_runs.send_command(
        pending["id"],
        routes_runs.CommandIn(command="cancel", idempotency_key="cancel-two"),
        SimpleNamespace(headers={}),
    )
    assert command["run"]["state"] == "CANCELED"
    assert runtime.get(pending["id"])["state"] == "CANCELED"


def test_ticket_and_legacy_run_commit_atomically(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'quantlab.sqlite').as_posix()}")
    db.migrate()
    db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "user@example.test", "User", "hash", 1),
    )
    db.execute("INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("ws", "Workspace", "user", 1))
    db.execute("CREATE TRIGGER reject_ticket BEFORE INSERT ON runtime_tickets BEGIN SELECT RAISE(ABORT, 'fault'); END")
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), Model(), Tools())
    bridge = RuntimeBridge(RunService(db), runtime, spec_loader=lambda data: AgentSpec(**data))
    with pytest.raises(DatabaseError):
        bridge.create(
            workspace_id="ws",
            user_id="user",
            title="Agent",
            prompt="hello",
            spec={"name": "demo", "version": "1"},
            idempotency_key="fault",
        )
    assert db.scalar("SELECT COUNT(*) FROM runs WHERE idempotency_key='fault'") == 0
