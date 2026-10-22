from __future__ import annotations

import time

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.platform import context as platform_context
from quant_agent.workflows.environments import write_json
from quant_agent.workflows.runtime import Runner
from quant_agent.workflows.service import WorkflowService
from quant_agent.workflows.templates import csv_research


@pytest.fixture
def client(tmp_path):
    reset_services()
    services = AppServices.build(store_root=str(tmp_path))
    set_services(services)
    with TestClient(create_app()) as c:
        me = login(c)
        yield c, services, me
    reset_services()


def test_import_save_validate_execute_and_export_same_definitions(client):
    c, s, _ = client
    catalog = c.get("/api/workflows/catalog")
    assert catalog.status_code == 200, catalog.text
    assert any(d["kind"] == "source.csv" for d in catalog.json()["cards"])
    uploaded = c.post(
        "/api/workflows/files?name=input.csv",
        content=b"id,value\n000001,10\n",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 200, uploaded.text
    flow = csv_research()
    flow["nodes"][0]["params"]["file"] = uploaded.json()["name"]
    saved = c.post("/api/workflows", json={"workflow": flow})
    assert saved.status_code == 200, saved.text
    wid = saved.json()["id"]
    check = c.post(f"/api/workflows/{wid}/validate", json={})
    assert check.json()["ok"] is True, check.text
    run = c.post(f"/api/workflows/{wid}/run", json={})
    assert run.status_code == 200, run.text
    detail = c.get("/api/workflows/runs/" + run.json()["id"])
    assert detail.json()["run"]["state"] == "COMPLETED", detail.text
    report = detail.json()["report"]
    ref = report["nodes"]["source"]["outputs"]["data"]
    data = c.get("/api/workflows/artifacts/" + ref["hash"]).json()["value"]
    assert data[0]["id"] == "000001"
    exported = c.post(f"/api/workflows/{wid}/export", json={})
    assert exported.json()["state"] == "COMPLETED", exported.text
    download = exported.json()["summary"]["download"]
    assert c.get(download).content.startswith(b"PK")
    frozen = c.post("/api/workflows/runs/" + run.json()["id"] + "/export", json={})
    assert frozen.status_code == 200, frozen.text
    assert frozen.json()["state"] == "COMPLETED"
    assert (
        c.get("/api/workflows/files/download", params={"name": uploaded.json()["name"]}).content
        == b"id,value\n000001,10\n"
    )
    assert c.get("/api/workflows/files/download", params={"name": "../../secret.txt"}).status_code == 404
    assert s.teams.tools.get("workflow_validate") is not None


def test_seed_override_does_not_mutate_saved_revision(client):
    c, _, _ = client
    uploaded = c.post("/api/workflows/files?name=input.csv", content=b"id,value\n000001,10\n").json()
    flow = csv_research()
    flow["nodes"][0]["params"]["file"] = uploaded["name"]
    saved = c.post("/api/workflows", json={"workflow": flow}).json()
    run = c.post(f"/api/workflows/{saved['id']}/run", json={"seed": 17}).json()
    report = c.get("/api/workflows/runs/" + run["id"]).json()["report"]
    assert report["workflow"]["settings"]["seed"] == 17
    assert c.get(f"/api/workflows/{saved['id']}").json()["workflow"]["settings"]["seed"] == 7
    experiments = c.get(f"/api/workflows/experiments?workflow_id={saved['id']}").json()["experiments"]
    assert experiments[0]["seed"] == 17


def test_revision_conflicts_and_immutable_card_definitions(client):
    c, _, _ = client
    saved = c.post("/api/workflows", json={"workflow": csv_research()}).json()
    wid = saved["id"]
    assert (
        c.put(
            f"/api/workflows/{wid}",
            json={"workflow": csv_research(), "expected_revision": 1},
        ).status_code
        == 200
    )
    assert (
        c.put(
            f"/api/workflows/{wid}",
            json={"workflow": csv_research(), "expected_revision": 1},
        ).status_code
        == 400
    )
    assert c.get(f"/api/workflows/{wid}?revision=1").json()["revision"] == 1
    d = next(x for x in c.get("/api/workflows/catalog").json()["cards"] if x["kind"] == "config.value")
    code = d["code"].replace('"config.value"', '"user.config"')
    assert c.post("/api/workflows/definitions/preview", json={"code": code}).status_code == 200
    assert c.post("/api/workflows/definitions", json={"code": code}).status_code == 200
    assert (
        c.post(
            "/api/workflows/definitions",
            json={"code": code + "\n# incompatible update\n"},
        ).status_code
        == 400
    )


def test_workspace_isolation_of_workflows_files_artifacts(client):
    c, _services, me = client
    saved = c.post("/api/workflows", json={"workflow": csv_research()}).json()
    # Switch the same authenticated user into a different workspace.
    created = c.post("/api/workspaces", json={"name": "隔离研究空间"})
    assert created.status_code == 200, created.text
    assert created.json()["id"] != me["workspace"]["id"]
    c.headers.update({"X-Workspace-Id": created.json()["id"]})
    assert c.get("/api/workflows/" + saved["id"]).status_code == 400
    assert c.get("/api/workflows").json()["workflows"] == []
    assert c.get("/api/workflows/files").json()["files"] == []
    assert c.post("/api/workflows/files?name=../secret.csv", content=b"x").status_code == 400


def _saved_workflow(c, name: str):
    flow = csv_research()
    flow["title"] = name
    saved = c.post("/api/workflows", json={"workflow": flow})
    assert saved.status_code == 200, saved.text
    return saved.json()["id"]


def _completed_evidence(s, workspace_id, user_id, wid, *, origin="gui"):
    """Persist the same run/report/artifact contract a completed workflow run leaves."""
    with platform_context.bind(workspace_id, user_id=user_id, role="admin"):
        service = WorkflowService(s)
        artifact = Runner(service.root, service.registry()).artifact([{"id": "1"}], {"type": "table"})
        run = s.runs.create(
            workspace_id=workspace_id,
            user_id=user_id,
            kind="workflow",
            mode="analysis",
            title="最近结果研究",
            manifest={"workflow_id": wid, "revision": 1, "mode": "research", "origin": origin},
        )
        s.db.execute("UPDATE runs SET state='COMPLETED' WHERE id=?", (run["id"],))
        report = {
            "id": run["id"],
            "status": "completed",
            "mode": "research",
            "nodes": {"source": {"status": "completed", "outputs": {"data": artifact}}},
        }
        write_json(service.root / "runs" / f"{run['id']}.json", report)
    return run["id"], report


def _later_attempt(s, workspace_id, user_id, wid, state, offset):
    run = s.runs.create(
        workspace_id=workspace_id,
        user_id=user_id,
        kind="workflow",
        mode="analysis",
        title=f"{state} 尝试",
        manifest={"workflow_id": wid, "revision": 1, "mode": "research", "origin": "agent"},
    )
    s.db.execute(
        "UPDATE runs SET state=?, created_at=? WHERE id=?",
        (state, int(time.time() * 1000) + offset, run["id"]),
    )
    return run["id"]


def test_recent_result_is_empty_without_runs_and_does_not_cross_workspaces(client):
    c, _services, _me = client
    empty = c.get("/api/workflows/recent-result")
    assert empty.status_code == 200, empty.text
    assert empty.json()["state"] == "empty"
    assert empty.json()["result"] is None
    assert empty.json()["latest_attempt"] is None

    created = c.post("/api/workspaces", json={"name": "无运行工作区"})
    assert created.status_code == 200, created.text
    c.headers.update({"X-Workspace-Id": created.json()["id"]})
    isolated = c.get("/api/workflows/recent-result")
    assert isolated.status_code == 200, isolated.text
    assert isolated.json()["state"] == "empty"
    assert isolated.json()["result"] is None
    assert c.get("/api/local/preferences").status_code == 400
    assert c.post("/api/local/preferences", json={"last_workflow_id": "workflow-x"}).status_code == 400


def test_recent_result_keeps_completed_evidence_and_reports_newer_attempts(client):
    c, s, me = client
    wid = _saved_workflow(c, "最近结果研究")
    completed, _report = _completed_evidence(s, me["workspace"]["id"], me["user"]["id"], wid, origin="agent")

    ready = c.get("/api/workflows/recent-result")
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["state"] == "ready"
    assert body["result"]["run_id"] == completed
    assert body["result"]["workflow_id"] == wid
    assert body["result"]["origin"] == "agent"
    assert body["result"]["artifact_count"] >= 1
    assert body["latest_attempt"]["run_id"] == completed

    failed = _later_attempt(s, me["workspace"]["id"], me["user"]["id"], wid, "FAILED", 10_000)
    after_failure = c.get("/api/workflows/recent-result").json()
    assert after_failure["state"] == "ready"
    assert after_failure["result"]["run_id"] == completed
    assert after_failure["latest_attempt"]["run_id"] == failed
    assert after_failure["latest_attempt"]["state"] == "FAILED"
    assert after_failure["latest_attempt"]["workflow_id"] == wid

    running = _later_attempt(s, me["workspace"]["id"], me["user"]["id"], wid, "RUNNING", 20_000)
    after_running = c.get("/api/workflows/recent-result").json()
    assert after_running["state"] == "ready"
    assert after_running["result"]["run_id"] == completed
    assert after_running["latest_attempt"]["run_id"] == running
    assert after_running["latest_attempt"]["state"] == "RUNNING"


def test_recent_result_never_labels_failed_or_running_attempts_as_results(client):
    c, s, me = client
    wid = _saved_workflow(c, "尚未完成的研究")
    failed = _later_attempt(s, me["workspace"]["id"], me["user"]["id"], wid, "FAILED", 10_000)
    failed_body = c.get("/api/workflows/recent-result").json()
    assert failed_body["state"] == "failed"
    assert failed_body["result"] is None
    assert failed_body["latest_attempt"]["run_id"] == failed
    assert "尚无已完成结果" in failed_body["reason"]

    running = _later_attempt(s, me["workspace"]["id"], me["user"]["id"], wid, "RUNNING", 20_000)
    running_body = c.get("/api/workflows/recent-result").json()
    assert running_body["state"] == "running"
    assert running_body["result"] is None
    assert running_body["latest_attempt"]["run_id"] == running
    assert "正在进行" in running_body["reason"]


def test_recent_result_marks_broken_completed_evidence_unavailable(client):
    c, s, me = client
    wid = _saved_workflow(c, "工件缺失研究")
    _run_id, report = _completed_evidence(s, me["workspace"]["id"], me["user"]["id"], wid)
    ref = report["nodes"]["source"]["outputs"]["data"]
    with platform_context.bind(me["workspace"]["id"], user_id=me["user"]["id"], role="admin"):
        artifact = WorkflowService(s).root / "artifacts" / f"{ref['hash']}.json"
    assert artifact.is_file()
    artifact.unlink()

    response = c.get("/api/workflows/recent-result")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "unavailable"
    assert response.json()["result"] is None
