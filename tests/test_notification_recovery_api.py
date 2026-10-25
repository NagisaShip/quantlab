# ruff: noqa: F811
"""Scheduled delivery exposes the same scoped, durable reconciliation contract."""

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_team_runtime_api import team_host  # noqa: F401

from quant_agent.api.app import create_app


def start(client):
    response = client.post("/api/long-running/digest/run", json={"date": "2026-10-25", "dry_run": True})
    assert response.status_code == 200, response.text
    return response.json()["record"]


def test_unknown_usage_has_no_false_delivery_and_confirmation_does_not_regenerate(team_host):
    client, services, model = team_host
    model.unknown_usage = True
    record = start(client)
    assert record["status"] == "waiting" and not record["pushed_to"]
    base = f"/api/long-running/records/{record['id']}/runtime"
    view = client.get(base).json()
    assert view["can_resolve"] and view["can_resume"]
    step = view["run"]["steps"][0]
    body = {"step_id": step["id"], "usage": {"input_tokens": 5, "output_tokens": 3}}
    response = client.post(base + "/model-usage", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] and model.calls == 1
    assert services.model_hub.admission(step["id"])["status"] == "SETTLED"
    assert not client.post(base + "/model-usage", json=body).json()["accepted"]
    done = client.post(base + "/resume").json()["record"]
    assert done["status"] == "ok" and done["delivery_state"] == "preview"
    assert model.calls == 1
    assert start(client)["id"] == record["id"] and model.calls == 1


def test_other_admin_can_confirm_facts_but_cannot_borrow_owner_model(team_host):
    client, _services, model = team_host
    model.unknown_usage = True
    record = start(client)
    base = f"/api/long-running/records/{record['id']}/runtime"
    step = client.get(base).json()["run"]["steps"][0]
    invite = client.post("/api/workspace/invites", json={"role": "admin"}).json()["invite"]["code"]
    with TestClient(create_app()) as admin:
        login(admin, email="notification-admin@test.local", password="Member01!", name="Admin", invite_code=invite)
        view = admin.get(base).json()
        assert view["can_resolve"] and not view["can_resume"]
        assert admin.post(base + "/resume").status_code == 403
        response = admin.post(base + "/model-usage", json={"step_id": step["id"], "no_charge_confirmed": True})
        assert response.status_code == 200, response.text
        assert model.calls == 1


def test_other_member_cannot_view_or_cancel_and_wrong_step_is_rejected(team_host):
    client, _services, model = team_host
    model.unknown_usage = True
    record = start(client)
    base = f"/api/long-running/records/{record['id']}/runtime"
    response = client.post(base + "/model-usage", json={"step_id": "foreign-step", "no_charge_confirmed": True})
    assert response.status_code == 404
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        login(member, email="notification-member@test.local", password="Member01!", name="Member", invite_code=invite)
        assert member.get(base).status_code == 403
        assert member.post(base + "/cancel").status_code == 403
    assert model.calls == 1


def test_deferred_wrapper_releases_slot_and_only_wakes_after_kernel_progress(team_host):
    from quant_agent.platform.worker import Worker

    client, services, model = team_host
    model.unknown_usage = True
    record = start(client)
    base = f"/api/long-running/records/{record['id']}/runtime"
    run = client.get(base).json()["run"]
    identity = run["spec"]["metadata"]["business_identity"]
    job = services.runs.create(
        workspace_id=run["tenant_id"], user_id=identity["user"], kind="agent_job", mode="agent", title="scheduled"
    )
    claimed = services.runs.claim("test-worker", kinds=["agent_job"])
    assert claimed["id"] == job["id"]
    assert services.runs.defer(job["id"], "test-worker", {"runtime_run_id": run["id"]})
    worker = Worker(services)
    try:
        for _ in range(3):
            worker._wake_deferred_agents()
        parent = services.runs.get(job["id"])
        assert parent["state"] == "PAUSED" and parent["lease_owner"] is None
        assert model.calls == 1 and not worker.busy
        response = client.post(
            base + "/model-usage", json={"step_id": run["steps"][0]["id"], "usage": {"input_tokens": 2}}
        )
        assert response.status_code == 200, response.text
        worker._wake_deferred_agents()
        assert services.runs.get(job["id"])["state"] == "QUEUED"
    finally:
        worker.pool.shutdown(wait=True)


def test_cancel_wrapper_waits_for_model_fee_confirmation_before_waking(team_host):
    from quant_agent.platform.worker import Worker

    client, services, model = team_host
    model.unknown_usage = True
    record = start(client)
    base = f"/api/long-running/records/{record['id']}/runtime"
    run = client.get(base).json()["run"]
    user = run["spec"]["metadata"]["business_identity"]["user"]
    job = services.runs.create(
        workspace_id=run["tenant_id"], user_id=user, kind="agent_job", mode="agent", title="cancel"
    )
    services.runs.claim("test-worker", kinds=["agent_job"])
    services.runs.defer(job["id"], "test-worker", {"runtime_run_id": run["id"]})
    command = services.runs.command(job["id"], run["tenant_id"], user, "cancel")
    worker = Worker(services)
    try:
        worker._wake_deferred_agents()
        assert services.runs.get(job["id"])["state"] == "PAUSED"
        assert client.get(base).json()["run"]["state"] == "CANCELING"
        assert services.runs.commands(job["id"])[0]["id"] == command["id"]
        response = client.post(
            base + "/model-usage", json={"step_id": run["steps"][0]["id"], "no_charge_confirmed": True}
        )
        assert response.status_code == 200 and response.json()["run"]["state"] == "CANCELED"
        worker._wake_deferred_agents()
        assert services.runs.get(job["id"])["state"] == "QUEUED"
        assert model.calls == 1
    finally:
        worker.pool.shutdown(wait=True)


@pytest.mark.parametrize("kind,field,value", [("digest", "date", "2001-01-01"), ("maintainer", "period", "2001-W01")])
def test_wrapper_recovery_uses_original_business_period_not_current_day(team_host, kind, field, value):
    from quant_agent.api.tasks import handle_agent_job
    from quant_agent.platform import context
    from quant_agent.platform.worker import TaskCtl

    client, services, model = team_host
    response = client.post(f"/api/long-running/{kind}/run", json={field: value, "dry_run": True})
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    run = client.get(f"/api/long-running/records/{record['id']}/runtime").json()["run"]
    user = run["spec"]["metadata"]["business_identity"]["user"]
    job = services.runs.create(
        workspace_id=run["tenant_id"],
        user_id=user,
        kind="agent_job",
        mode="agent",
        title="recovered",
        manifest={"job": {"id": "old-schedule", "kind": kind, "channel": "changed-channel"}},
    )
    claimed = services.runs.claim("test-worker", kinds=["agent_job"])
    assert claimed["id"] == job["id"]
    ctl = TaskCtl(claimed, services.runs, "test-worker", services)
    ctl.checkpoint({"runtime_dependency": run["id"]})
    with context.bind(run["tenant_id"], user_id=user, role="admin"):
        summary = handle_agent_job(ctl)
    assert summary["runtime_run_id"] == run["id"] and summary["status"] == "ok"
    assert model.calls == 1
