"""Opt-in QuantLab wiring delivers a run through the Runtime worker path."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from quant_agent.api import routes_runs
from quant_agent.api.deps import AppServices
from quant_agent.llm.base import ChatResult
from quant_agent.llm.router import AgentConfig, ProviderConfig
from quant_agent.platform import context
from quant_agent.platform.worker import Worker


def test_flagged_runtime_run_reaches_terminal_via_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME", "1")
    monkeypatch.setenv("QAGENT_WORKER", "external")
    services = AppServices.build(store_root=str(tmp_path))
    assert services.runtime_bridge is not None
    assert services.teams.runtime_journal is services.runtime_bridge.runtime.journal
    services.db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "user@example.test", "User", "hash", 1),
    )
    services.db.execute(
        "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("ws", "Workspace", "user", 1)
    )
    provider = ProviderConfig(
        name="demo",
        type="openai_compatible",
        base_url="http://localhost",
        api_key="key",
        models=["model"],
        scope="personal",
        owner_id="user",
        model_settings={"model": {"price_in": "1", "price_out": "1"}},
    )
    services.router.resolver = lambda base: AgentConfig(
        providers={"demo": provider}, routing={"default": "demo/model"}, limits={}, trace={}
    )

    class Client:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return ChatResult(
                content="done", model="model", provider="demo", usage={"input_tokens": 2, "output_tokens": 1}
            )

    client = Client()
    services.router._client = lambda name, pc: client
    monkeypatch.setattr(routes_runs, "get_services", lambda: services)
    with context.bind("ws", user_id="user", role="admin"):
        created = routes_runs.create_runtime_agent(
            routes_runs.RuntimeAgentIn(
                prompt="hello",
                idempotency_key="preview-one",
                budget_limit_micro=100,
                model_reservation_micro=20,
            )
        )["run"]
        run_id = created["id"]
        assert created["runtime_version"] == "0.1.0"
        worker = Worker(services, batch={}, resident={}, slots=1, periodic=[])
        try:
            worker.loop_once()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and services.runs.get(run_id)["state"] != "COMPLETED":
                time.sleep(0.02)
            assert services.runtime_bridge.runtime.get(run_id)["state"] == "COMPLETED"
            assert services.runs.get(run_id)["state"] == "COMPLETED"
            assert routes_runs.get_run(run_id)["run"]["state"] == "COMPLETED"
            assert client.calls == 1
        finally:
            worker.shutdown()


@pytest.mark.parametrize("wait_state", ["WAITING_APPROVAL", "WAITING_ADMISSION", "RECONCILING"])
def test_scheduler_skips_approval_wait_without_claiming_slot(tmp_path, monkeypatch, wait_state):
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME", "1")
    services = AppServices.build(store_root=str(tmp_path))
    services.db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "user@example.test", "User", "hash", 1),
    )
    services.db.execute(
        "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("ws", "Workspace", "user", 1)
    )
    services.db.execute("UPDATE workspaces SET settings=? WHERE id=?", ('{"quotas":{"max_concurrent_runs":128}}', "ws"))
    waiting = [
        services.runtime_bridge.create(
            workspace_id="ws",
            user_id="user",
            title="Approval",
            prompt="wait",
            spec={"name": "demo", "version": "1"},
            idempotency_key=f"approval-slot-{index}",
        )["id"]
        for index in range(33)
    ]
    ready = services.runtime_bridge.create(
        workspace_id="ws",
        user_id="user",
        title="Ready",
        prompt="go",
        spec={"name": "demo", "version": "1"},
        idempotency_key="ready-after-approvals",
    )["id"]
    services.db.execute("UPDATE runs SET created_at=1 WHERE id<>?", (ready,))
    services.db.execute("UPDATE runs SET created_at=2 WHERE id=?", (ready,))

    class WaitingRuntime:
        journal = services.runtime_bridge.runtime.journal

        def get(self, run_id):
            if run_id == ready:
                return None
            assert run_id in waiting
            return {
                "state": "WAITING",
                "steps": [
                    {"state": wait_state, "kind": "model", "admission_retry_at": int(time.time() * 1000) + 60000}
                ],
            }

    services.runtime_bridge.runtime = WaitingRuntime()
    worker = Worker(services, batch={}, resident={}, slots=1, periodic=[])
    dispatched = []
    worker._execute_runtime = dispatched.append
    try:
        worker.loop_once()
        assert dispatched == [ready] or worker.busy.get(ready) is not None
        assert all(run_id not in worker.busy for run_id in waiting)
    finally:
        worker.shutdown()
