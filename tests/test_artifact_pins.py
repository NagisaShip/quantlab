"""P3：工作区级成果固定、版本历史与本地文件路径。

真实运行需要研究依赖（pandas 等），本文件遵循现有测试的做法，直接写入
“已完成运行留下的 run + report + artifact”契约证据，覆盖固定/取消/列表、
版本排序、跨工作区隔离、伪造证据拒绝、本机路径与缓存清理保护。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

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


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    project = tmp_path / "固定成果项目"
    project.mkdir()
    monkeypatch.setenv("QUANTLAB_MODE", "local")
    monkeypatch.setenv("QUANTLAB_PROJECT", str(project))
    monkeypatch.setenv("QUANTLAB_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("QUANTLAB_CREDENTIALS", str(tmp_path / "keys"))
    reset_services()
    services = AppServices.build(store_root=str(project / ".quantlab"))
    set_services(services)
    with TestClient(create_app()) as c:
        c.headers["X-Requested-With"] = "quantlab-test"
        me = c.get("/api/auth/me").json()
        with platform_context.bind(me["workspace"]["id"], user_id=me["user"]["id"], role="admin"):
            yield c, services, project, tmp_path, me
    reset_services()


def _saved_workflow(c, title="固定成果研究"):
    flow = csv_research()
    flow["title"] = title
    saved = c.post("/api/workflows", json={"workflow": flow})
    assert saved.status_code == 200, saved.text
    return saved.json()["id"]


def _run_with_output(
    services,
    workspace_id,
    user_id,
    workflow_id,
    *,
    value=None,
    node_id="source",
    port="data",
    created_at=None,
    revision=1,
    state="COMPLETED",
):
    """持久化一次已完成运行会留下的 run / report / artifact 三件证据。"""
    with platform_context.bind(workspace_id, user_id=user_id, role="admin"):
        service = WorkflowService(services)
        artifact = Runner(service.root, service.registry()).artifact(
            value if value is not None else [{"id": "1"}], {"type": "table"}
        )
        run = services.runs.create(
            workspace_id=workspace_id,
            user_id=user_id,
            kind="workflow",
            mode="analysis",
            title="固定成果运行",
            manifest={"workflow_id": workflow_id, "revision": revision, "mode": "research", "origin": "gui"},
        )
        services.db.execute(
            "UPDATE runs SET state=?, created_at=? WHERE id=?",
            (state, created_at or int(time.time() * 1000), run["id"]),
        )
        report = {
            "id": run["id"],
            "status": "completed" if state == "COMPLETED" else "failed",
            "mode": "research",
            "nodes": {node_id: {"status": "completed", "outputs": {port: artifact}}},
        }
        write_json(service.root / "runs" / f"{run['id']}.json", report)
    return run["id"], artifact


def _pin_body(workflow_id, run_id, artifact, *, node_id="source", port="data", **extra):
    return {
        "hash": artifact["hash"],
        "run_id": run_id,
        "workflow_id": workflow_id,
        "node_id": node_id,
        "port": port,
        "title": "源数据 · data",
        "type": artifact["type"],
        **extra,
    }


def test_pin_create_is_idempotent_and_list_filters_by_workflow_and_version_key(client):
    c, services, me = client
    workspace_id, user_id = me["workspace"]["id"], me["user"]["id"]
    wid = _saved_workflow(c)
    run_id, artifact = _run_with_output(services, workspace_id, user_id, wid)
    key = f"{wid}:source:data"
    body = _pin_body(wid, run_id, artifact, note="每周复盘")

    first = c.post("/api/workflows/artifacts/pins", json=body)
    assert first.status_code == 200, first.text
    pin = first.json()
    assert pin["id"] and pin["version_key"] == key
    assert pin["hash"] == artifact["hash"] and pin["run_id"] == run_id
    assert pin["pinned_at"] > 0 and pin["pinned_by"] == user_id
    assert pin["note"] == "每周复盘" and pin["type"] == "table"
    assert pin["title"] == "源数据 · data"

    again = c.post("/api/workflows/artifacts/pins", json=body)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == pin["id"]
    with platform_context.bind(workspace_id, user_id=user_id, role="admin"):
        assert len(services.store.list("artifact_pins")) == 1

    listed = c.get("/api/workflows/artifacts/pins", params={"workflow_id": wid})
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1 and listed.json()["pins"][0]["id"] == pin["id"]
    assert c.get("/api/workflows/artifacts/pins", params={"version_key": key}).json()["total"] == 1
    assert c.get("/api/workflows/artifacts/pins", params={"workflow_id": "workflow-other"}).json()["total"] == 0
    assert c.get("/api/workflows/artifacts/pins", params={"limit": 0}).status_code == 400

    # 同一 version_key 可以固定多个历史版本，按固定时间倒序。
    time.sleep(0.02)
    second_run, second_artifact = _run_with_output(
        services, workspace_id, user_id, wid, value=[{"id": "2"}], revision=2
    )
    second = c.post("/api/workflows/artifacts/pins", json=_pin_body(wid, second_run, second_artifact))
    assert second.status_code == 200, second.text
    rows = c.get("/api/workflows/artifacts/pins", params={"version_key": key}).json()
    assert rows["total"] == 2
    assert [r["hash"] for r in rows["pins"]] == [second_artifact["hash"], artifact["hash"]]


def test_versions_are_time_ordered_and_mark_pinned(client):
    c, services, me = client
    workspace_id, user_id = me["workspace"]["id"], me["user"]["id"]
    wid = _saved_workflow(c)
    base = int(time.time() * 1000)
    old_run, old_artifact = _run_with_output(
        services, workspace_id, user_id, wid, value=[{"v": 1}], created_at=base, revision=1
    )
    new_run, new_artifact = _run_with_output(
        services, workspace_id, user_id, wid, value=[{"v": 2}], created_at=base + 5000, revision=2
    )
    key = f"{wid}:source:data"

    listed = c.get("/api/workflows/artifacts/versions", params={"version_key": key})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["version_key"] == key and body["total"] == 2
    assert [v["hash"] for v in body["versions"]] == [new_artifact["hash"], old_artifact["hash"]]
    newest = body["versions"][0]
    assert newest["run_id"] == new_run and newest["revision"] == 2 and newest["status"] == "completed"
    assert newest["available"] is True and newest["pinned"] is False
    assert body["versions"][1]["run_id"] == old_run and body["versions"][1]["revision"] == 1

    pinned = c.post("/api/workflows/artifacts/pins", json=_pin_body(wid, old_run, old_artifact, note="保留首个版本"))
    assert pinned.status_code == 200, pinned.text
    after = c.get("/api/workflows/artifacts/versions", params={"version_key": key}).json()
    old_row = next(v for v in after["versions"] if v["hash"] == old_artifact["hash"])
    new_row = next(v for v in after["versions"] if v["hash"] == new_artifact["hash"])
    assert old_row["pinned"] is True and old_row["pinned_at"] == pinned.json()["pinned_at"]
    assert new_row["pinned"] is False

    # 通过 hash 反查同一个 version_key。
    by_hash = c.get(f"/api/workflows/artifacts/{old_artifact['hash']}/versions")
    assert by_hash.status_code == 200, by_hash.text
    assert by_hash.json()["version_key"] == key
    assert [v["hash"] for v in by_hash.json()["versions"]] == [new_artifact["hash"], old_artifact["hash"]]

    assert c.get("/api/workflows/artifacts/versions", params={"version_key": "bad"}).status_code == 400
    assert c.get("/api/workflows/artifacts/versions").status_code == 400
    unknown = "a" * 64
    missing = c.get(f"/api/workflows/artifacts/{unknown}/versions")
    assert missing.status_code == 200 and missing.json()["total"] == 0


def test_pins_are_workspace_scoped_and_forged_combinations_rejected(client):
    c, services, me = client
    workspace_id, user_id = me["workspace"]["id"], me["user"]["id"]
    wid = _saved_workflow(c)
    run_id, artifact = _run_with_output(services, workspace_id, user_id, wid)
    other_run, other_artifact = _run_with_output(services, workspace_id, user_id, wid, value=[{"id": "9"}])
    body = _pin_body(wid, run_id, artifact)

    assert c.post("/api/workflows/artifacts/pins", json={**body, "workflow_id": "workflow-other"}).status_code == 400
    assert c.post("/api/workflows/artifacts/pins", json={**body, "hash": other_artifact["hash"]}).status_code == 400
    assert c.post("/api/workflows/artifacts/pins", json={**body, "type": "image"}).status_code == 400
    assert c.post("/api/workflows/artifacts/pins", json={**body, "node_id": "missing"}).status_code == 400
    assert c.post("/api/workflows/artifacts/pins", json={**body, "run_id": "run-missing"}).status_code == 404
    forged = {**body, "hash": "f" * 64}
    assert c.post("/api/workflows/artifacts/pins", json=forged).status_code == 400

    pin = c.post("/api/workflows/artifacts/pins", json=body).json()
    key = f"{wid}:source:data"

    created = c.post("/api/workspaces", json={"name": "另一个固定空间"})
    assert created.status_code == 200, created.text
    c.headers.update({"X-Workspace-Id": created.json()["id"]})
    assert c.get("/api/workflows/artifacts/pins").json()["total"] == 0
    assert c.get("/api/workflows/artifacts/versions", params={"version_key": key}).json()["total"] == 0
    assert c.delete(f"/api/workflows/artifacts/pins/{pin['id']}").status_code == 400
    assert c.post("/api/workflows/artifacts/pins", json=body).status_code == 404

    c.headers.update({"X-Workspace-Id": workspace_id})
    assert c.get("/api/workflows/artifacts/pins", params={"workflow_id": wid}).json()["total"] == 1
    assert c.delete(f"/api/workflows/artifacts/pins/{pin['id']}").status_code == 200
    assert c.get("/api/workflows/artifacts/pins", params={"workflow_id": wid}).json()["total"] == 0
    remaining = c.get("/api/workflows/artifacts/versions", params={"version_key": key}).json()
    assert all(v["pinned"] is False for v in remaining["versions"])
    # other_run 只为制造不同 hash，未被使用但保证证据真实存在。
    assert other_run


def test_local_artifact_path_is_local_mode_only(client):
    c, _services, _me = client
    response = c.get("/api/local/artifacts/" + "a" * 64 + "/path")
    assert response.status_code == 404, response.text


def test_local_artifact_path_resolves_inside_project(local_project):
    c, services, project, _tmp, _me = local_project
    service = WorkflowService(services)
    ref = Runner(service.root, service.registry()).artifact([{"x": 1}], {"type": "table"})

    response = c.get(f"/api/local/artifacts/{ref['hash']}/path")
    assert response.status_code == 200, response.text
    body = response.json()
    path = Path(body["path"])
    assert path.is_file() and path.is_relative_to(project)
    assert body["hash"] == ref["hash"]
    assert body["directory"] == str(path.parent) and body["name"] == path.name
    assert body["bytes"] == path.stat().st_size and body["mode"] == "local"

    assert c.get(f"/api/local/artifacts/{'b' * 64}/path").status_code == 404
    assert c.get("/api/local/artifacts/not-a-digest/path").status_code == 400


def test_cache_clear_keeps_entries_referenced_by_pins(local_project):
    c, services, _project, _tmp, me = local_project
    workspace_id, user_id = me["workspace"]["id"], me["user"]["id"]
    wid = _saved_workflow(c)
    run_id, artifact = _run_with_output(services, workspace_id, user_id, wid)
    pinned = c.post("/api/workflows/artifacts/pins", json=_pin_body(wid, run_id, artifact))
    assert pinned.status_code == 200, pinned.text

    service = WorkflowService(services)
    cache = service.root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    keep = cache / "pinned-cache.json"
    keep.write_text(json.dumps({"data": {"hash": artifact["hash"], "type": "table"}}), encoding="utf-8")
    drop = cache / "cold-cache.json"
    drop.write_text(json.dumps({"data": {"hash": "c" * 64, "type": "table"}}), encoding="utf-8")
    evidence = service.root / "runs" / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")

    listed = c.get("/api/local/cache")
    assert listed.status_code == 200, listed.text
    assert listed.json()["entries"] == 2 and listed.json()["pinned_entries"] == 1

    cleared = c.post("/api/local/cache/clear")
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["cleared"] is True and body["skipped_pinned"] == 1
    assert body["protected_files"] == ["pinned-cache.json"]
    assert keep.is_file() and not drop.exists() and evidence.is_file()
    assert c.get("/api/workflows/artifacts/pins", params={"workflow_id": wid}).json()["total"] == 1
