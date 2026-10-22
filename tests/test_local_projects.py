"""Local mode, real interpreter selection, bounded files and immutable evidence."""

from __future__ import annotations

import json
import os
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.platform import context
from quant_agent.workflows.contracts import WorkflowError
from quant_agent.workflows.environments import EnvironmentManager
from quant_agent.workflows.service import WorkflowService


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    project = tmp_path / "研究项目"
    project.mkdir()
    monkeypatch.setenv("QUANTLAB_MODE", "local")
    monkeypatch.setenv("QUANTLAB_PROJECT", str(project))
    monkeypatch.setenv("QUANTLAB_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("QUANTLAB_CREDENTIALS", str(tmp_path / "keys"))
    reset_services()
    s = AppServices.build(store_root=str(project / ".quantlab"))
    set_services(s)
    with TestClient(create_app()) as c:
        c.headers["X-Requested-With"] = "quantlab-test"
        me = c.get("/api/auth/me").json()
        with context.bind(me["workspace"]["id"], user_id=me["user"]["id"], role="admin"):
            yield c, s, project, tmp_path
    reset_services()


def test_local_identity_and_services_are_explicit_and_scoped(local_project):
    c, _s, root, _ = local_project
    me = c.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["mode"] == "local"
    for route in ("/api/auth/login", "/api/auth/register", "/api/community/posts", "/api/groups", "/api/issues"):
        assert c.post(route, json={}).status_code == 404
    assert c.get("/api/local/info").json()["upload_byte_limit"] is None
    assert c.get("/api/local/info", headers={"Host": "evil.example"}).status_code == 403
    assert c.post("/api/local/cache/clear", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    c.headers.pop("X-Requested-With")
    assert c.post("/api/local/cache/clear", json={}).status_code == 403
    c.headers["X-Requested-With"] = "quantlab-test"
    assert c.get("/api/auth/me", headers={"X-Workspace-Id": "stale-project"}).status_code == 200
    assert c.get("/api/local/info", headers={"X-Workspace-Id": "other-project"}).status_code == 403
    assert (root / ".quantlab/project.json").is_file()


def test_local_plugin_settings_are_project_scoped(local_project):
    c, _, _, _ = local_project
    response = c.get("/api/auth/research-plugins")
    assert response.status_code == 200, response.text
    assert any(p["id"] == "charts" for p in response.json()["plugins"])
    changed = c.put("/api/auth/research-plugins/charts", json={"agent": False, "visible": False})
    assert changed.status_code == 200, changed.text
    current = c.get("/api/auth/research-plugins").json()["plugins"]
    assert next(p for p in current if p["id"] == "charts")["agent"] is False
    assert c.get("/api/auth/research-plugins", headers={"X-Workspace-Id": "other-project"}).status_code == 403
    assert (
        c.put(
            "/api/auth/research-plugins/charts", json={"agent": True}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert c.post("/api/auth/register", json={}).status_code == 404


def test_authorized_reference_preview_changes_and_relocation(local_project):
    c, s, _root, tmp = local_project
    data = tmp / "外部数据"
    data.mkdir()
    file = data / "input.csv"
    file.write_text("id,value\n001,10\n002,20\n", encoding="utf-8")
    assert c.post("/api/local/files/import", json={"path": str(file), "mode": "reference"}).status_code == 400
    assert c.post("/api/local/permissions", json={"path": str(data), "mode": "read"}).status_code == 200
    result = c.post("/api/local/files/import", json={"path": str(file), "mode": "reference"}).json()
    name = result["name"]
    service = WorkflowService(s)
    assert not (service.files / name).exists()
    preview = c.get("/api/local/files/preview", params={"path": str(file), "limit": 1}).json()
    assert preview["data"][0]["id"] == "001" and preview["has_more"] and preview["total"] is None
    from quant_agent.workflows.files import resolve_input

    assert resolve_input(service.files, name) == file.resolve()
    file.write_text("id,value\n001,11\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="变化"):
        resolve_input(service.files, name)
    changed = c.post("/api/local/files/relocate", json={"name": name, "path": str(file)}).json()
    assert changed["changed"] and changed["history"][0]["sha256"] == result["sha256"]
    assert c.post("/api/local/permissions/revoke", json={"path": str(data)}).status_code == 200
    with pytest.raises(WorkflowError, match="授权"):
        resolve_input(service.files, name)


def test_large_streaming_upload_and_agent_data_bootstrap(local_project):
    c, s, _, _ = local_project
    from quant_agent.workflows.agent import import_data, read_artifact

    file = import_data("agent.csv", data=[{"id": "001", "value": 1}, {"id": "002", "value": 2}])
    assert file["rows"] == 2
    assert (
        c.post(
            "/api/workflows/files?name=large.csv",
            content=iter([b"id,value\n", *[b"x,1\n" * (256 * 1024)] * 27]),
            headers={"Content-Type": "application/octet-stream"},
        ).status_code
        == 200
    )
    from quant_agent.workflows.runtime import Runner

    runner = Runner(WorkflowService(s).root, WorkflowService(s).registry())
    ref = runner.artifact([{"x": i} for i in range(5)], {"type": "table"})
    assert read_artifact(ref["hash"], offset=5)["value"] == []
    with pytest.raises(WorkflowError, match="total=5"):
        read_artifact(ref["hash"], offset=999)
    assert c.get("/api/workflows/artifacts/" + ref["hash"] + "?offset=999").status_code == 400


def test_selected_environment_runs_preview_and_cards_even_if_server_missing_dependencies(local_project):
    c, s, _, _ = local_project
    selected = os.environ.get("QUANTLAB_TEST_RESEARCH_PYTHON", sys.executable)
    response = c.get("/api/workflows/environment")
    assert response.status_code == 200 and "python" in response.json(), response.text
    manager = EnvironmentManager(WorkflowService(s).root)
    info = manager.select(selected)
    if not any(x["ok"] for x in info["checks"] if x["requirement"].startswith("pandas")):
        pytest.skip("研究环境未提供pandas")
    response = c.post(
        "/api/workflows/charts/preview",
        json={"data": [{"x": 1, "y": 2}, {"x": 2, "y": 3}], "config": {"x": "x", "y": "y"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["png"].startswith("data:image/png;base64,")
    service = WorkflowService(s)
    file = service.import_data("env.csv", data=[{"value": 1}, {"value": 2}])
    flow = {
        "format": "quantlab.workflow/1",
        "nodes": [
            {"id": "src", "kind": "source.csv", "version": "1", "params": {"file": file["name"]}},
            {"id": "clean", "kind": "research.cast", "version": "1", "params": {"mapping": {"value": "number"}}},
        ],
        "edges": [{"source": "src", "source_port": "data", "target": "clean", "target_port": "data"}],
    }
    from quant_agent.workflows.runtime import Runner

    runner = Runner(service.root, service.registry(), files=service.files)
    report = runner.run(flow)
    assert report["status"] == "completed", report
    assert runner.read(report["nodes"]["clean"]["outputs"]["data"]) == [{"value": 1.0}, {"value": 2.0}]
    assert report["environment"]["python"] == str(Path(selected).resolve())
    assert manager.info()["python"] == str(Path(selected).resolve())
    assert service.chart_preview([{"x": 1, "y": 2}], {"x": "x", "y": "y"})["data"]
    assert c.post("/api/workflows/environment/select", json={"python": "missing-python"}).status_code == 400
    assert manager.selected() == str(Path(selected).resolve())


def test_project_export_excludes_credentials_and_cache_cleanup_keeps_evidence(local_project):
    _c, s, root, _ = local_project
    (root / "input.csv").write_text("a\n1\n", encoding="utf-8")
    (root / ".env").write_text("API_KEY=do-not-export", encoding="utf-8")
    (root / ".quantlab-keys").mkdir()
    (root / ".quantlab-keys/secret.key").write_text("do-not-export", encoding="utf-8")
    from quant_agent.local.management import export_project

    service = WorkflowService(s)
    (service.root / "cache").mkdir()
    (service.root / "cache/cache.json").write_text("{}", encoding="utf-8")
    (service.root / "runs").mkdir()
    (service.root / "runs/evidence.json").write_text("{}", encoding="utf-8")
    assert s.local.cache(service, True)["cleared"]
    assert (service.root / "runs/evidence.json").exists()
    archive = export_project(s.local)
    with zipfile.ZipFile(archive["file"]) as z:
        assert "input.csv" in z.namelist() and ".env" not in z.namelist()
        assert not any("secret.key" in n for n in z.namelist())
        assert "QUANTLAB-TRANSFER.json" in z.namelist()


def test_module_library_is_immutable_and_can_be_imported(local_project):
    _c, s, _, _ = local_project
    service = WorkflowService(s)
    code = """NODE={"kind":"user.local_test","version":"1","title":"可复用函数","inputs":{},"outputs":{"value":{"type":"json"}},"parameters":{"type":"object","properties":{}},"requirements":[]}
def execute(ctx,inputs,params):return {"value":42}
"""
    service.publish_card(code)
    from quant_agent.local.management import import_library, publish_library

    first = publish_library(service, "user.local_test", "1")
    second = publish_library(service, "user.local_test", "1")
    assert first["id"] == second["id"]
    assert import_library(service, first["id"])["imported"]
    assert service.registry().get("user.local_test", "1")["code"] == code


def test_experiment_help_and_snapshot_reference_are_complete(local_project):
    _, s, _, _ = local_project
    from quant_agent.workflows.agent import catalog, help_workflow, snapshot_asset
    from quant_agent.workflows.experiments import variants

    assert help_workflow("experiments")["grid"]["axes"][0]["parameter"] == "max_rows"
    with pytest.raises(WorkflowError, match="axes"):
        variants({}, WorkflowService(s).registry(), {"grid": []})
    s.store.upsert("factors", {"id": "f-test", "name": "可读名称", "expression": "returns(2)"})
    a = snapshot_asset("factor", "f-test")
    b = snapshot_asset("factor", "f-test")
    assert a["kind"] == b["kind"] and a["truncated"] is False and a["definition_ref"]["file_path"] == "NODE.py"
    assert len(json.dumps(a, ensure_ascii=False)) < 6000
    assert "cards" not in catalog(section="workflows") and catalog(section="workflows")["card_total"] > 10


def test_native_key_protection_and_instance_lock(local_project):
    _, s, _, _ = local_project
    from quant_agent.local.credentials import crypt
    from quant_agent.local.lifecycle import instance_lock, recover_ui_operations

    if os.name == "nt":
        secret = b"local-master-key-regression"
        encrypted = crypt(secret)
        assert encrypted != secret and crypt(encrypted, False) == secret
    with instance_lock(s.home):
        with pytest.raises(RuntimeError, match="已有运行实例"):
            with instance_lock(s.home):
                pass
    s.store.upsert("lab_projects", {"id": "interrupted", "status": "running", "job": "old", "title": "保留的会话"})
    jobs = WorkflowService(s).root / "management-jobs"
    jobs.mkdir()
    (jobs / "old.json").write_text(json.dumps({"state": "running"}), encoding="utf-8")
    recover_ui_operations(s)
    assert s.store.get("lab_projects", "interrupted")["status"] == "stopped"
    assert json.loads((jobs / "old.json").read_text(encoding="utf-8"))["state"] == "failed"


def test_module_drilldown_and_selected_export(local_project):
    c, s, _, _ = local_project
    service = WorkflowService(s)
    source = service.import_data("portable.csv", data=[{"x": 1}])
    flow = {
        "format": "quantlab.workflow/1",
        "title": "独立复现",
        "nodes": [
            {"id": "input", "kind": "source.csv", "version": "1", "params": {"file": source["name"]}},
            {"id": "clean", "kind": "module.clean", "version": "1", "params": {}},
        ],
        "edges": [{"from": {"node": "input", "port": "data"}, "to": {"node": "clean", "port": "data"}}],
    }
    saved = c.post("/api/workflows", json={"workflow": flow}).json()
    view = c.get(f"/api/workflows/{saved['id']}/module-view", params={"node_path": "clean"})
    assert view.status_code == 200, view.text
    assert view.json()["nodes"] and view.json()["module_path"] == "clean"
    selected = os.environ.get("QUANTLAB_TEST_RESEARCH_PYTHON", sys.executable)
    EnvironmentManager(service.root).select(selected)
    pure = service.save({**flow, "nodes": flow["nodes"][:1], "edges": []})
    name, manifest = service.export(pure["id"])
    assert manifest["python"] == EnvironmentManager(service.root).info()["version"]
    with zipfile.ZipFile(service.root / "exports" / name) as z:
        assert any(n.endswith(source["name"]) for n in z.namelist())


def test_desktop_bridge_keeps_window_reference_private():
    """桌面 js_api 不能把 pywebview 的 window 暴露成公开属性。

    pywebview 6 在注入 js_api 时会递归展开对象的公开属性（webview.util.get_functions）。
    NativeBridge.window 一旦是公开属性，递归就会顺着 Window → 原生 WinForms/WebView2
    控件图一直展开，把 GUI 线程饿死，桌面窗口启动几秒后变成「未响应」。
    """
    from quant_agent.local.launcher import NativeBridge

    bridge = NativeBridge("http://127.0.0.1:1234")
    public = sorted(name for name in dir(bridge) if not name.startswith("_"))
    assert public == ["choose_files", "choose_folder", "open_project", "url"]
    assert bridge._window is None


def test_desktop_landing_url_uses_research_for_existing_projects():
    """首次打开空项目先进「本地项目」设置页；已有项目的启动应该直接进研究工作区。"""
    from quant_agent.local.launcher import _landing_url

    assert _landing_url("http://127.0.0.1:1234", True) == "http://127.0.0.1:1234/#/local"
    assert _landing_url("http://127.0.0.1:1234", False) == "http://127.0.0.1:1234/#/workflows"


def test_desktop_preferences_do_not_leak_between_local_projects(tmp_path, monkeypatch):
    from quant_agent.workflows.templates import csv_research

    def open_project(name):
        project = tmp_path / name
        project.mkdir()
        monkeypatch.setenv("QUANTLAB_MODE", "local")
        monkeypatch.setenv("QUANTLAB_PROJECT", str(project))
        monkeypatch.setenv("QUANTLAB_LIBRARY", str(tmp_path / "library"))
        monkeypatch.setenv("QUANTLAB_CREDENTIALS", str(tmp_path / "keys"))
        reset_services()
        services = AppServices.build(store_root=str(project / ".quantlab"))
        set_services(services)
        client = TestClient(create_app())
        client.__enter__()
        client.headers["X-Requested-With"] = "quantlab-test"
        return client, project

    first, first_root = open_project("项目甲")
    try:
        saved = first.post("/api/workflows", json={"workflow": csv_research()})
        assert saved.status_code == 200, saved.text
        assert first.post("/api/local/preferences", json={"last_workflow_id": saved.json()["id"]}).status_code == 200
        assert (first_root / ".quantlab/desktop-preferences.json").is_file()
    finally:
        first.__exit__(None, None, None)
        reset_services()

    second, second_root = open_project("项目乙")
    try:
        assert second.get("/api/local/preferences").json() == {"close_action": "ask"}
        assert not (second_root / ".quantlab/desktop-preferences.json").exists()
    finally:
        second.__exit__(None, None, None)
        reset_services()


def test_desktop_preferences_merge_and_validate_last_workflow(local_project):
    c, s, root, _ = local_project
    from quant_agent.workflows.templates import csv_research

    path = root / ".quantlab/desktop-preferences.json"
    path.write_text(json.dumps({"close_action": "background", "future_field": "keep"}), encoding="utf-8")
    saved = c.post("/api/workflows", json={"workflow": csv_research()})
    assert saved.status_code == 200, saved.text
    wid = saved.json()["id"]

    assert c.get("/api/local/preferences").json() == {
        "close_action": "background",
        "future_field": "keep",
    }
    merged = c.post("/api/local/preferences", json={"last_workflow_id": wid})
    assert merged.status_code == 200, merged.text
    assert merged.json() == {
        "close_action": "background",
        "future_field": "keep",
        "last_workflow_id": wid,
    }
    close_only = c.post("/api/local/preferences", json={"close_action": "exit"})
    assert close_only.status_code == 200, close_only.text
    assert close_only.json()["close_action"] == "exit"
    assert close_only.json()["last_workflow_id"] == wid
    assert json.loads(path.read_text(encoding="utf-8"))["future_field"] == "keep"

    invalid = c.post("/api/local/preferences", json={"last_workflow_id": "../other-project"})
    assert invalid.status_code == 400
    assert json.loads(path.read_text(encoding="utf-8"))["last_workflow_id"] == wid
    s.store.delete("workflows", wid)
    deleted = c.post("/api/local/preferences", json={"last_workflow_id": wid})
    assert deleted.status_code == 400
    assert json.loads(path.read_text(encoding="utf-8"))["last_workflow_id"] == wid
    assert c.post("/api/local/preferences", json={"last_workflow_id": ""}).json().get("last_workflow_id") is None


def test_shutdown_reports_structured_running_tasks(local_project):
    """有任务时退出必须返回结构化 409，桌面端才能给出三种选择；/tasks 同步可见。"""
    c, s, _root, _ = local_project
    workspace = s.local.manifest["workspace_id"]
    now = int(time.time())
    s.db.execute(
        "INSERT INTO runs (id, workspace_id, created_by, mode, kind, state, title, manifest, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("run-busy", workspace, "tester", "local", "research", "RUNNING", "进行中的研究", "{}", now, now),
    )

    busy = c.post("/api/local/shutdown")
    assert busy.status_code == 409, busy.text
    body = busy.json()
    assert body["code"] == "tasks_running"
    assert body["active_count"] == 1
    assert body["detail"]
    assert body["tasks"][0]["kind"] == "run"
    assert body["tasks"][0]["id"] == "run-busy"

    tasks = c.get("/api/local/tasks").json()
    assert tasks["active_count"] == 1
    assert any(item["id"] == "run-busy" for item in tasks["active"])

    s.db.execute("UPDATE runs SET state='CANCELLED' WHERE id=?", ("run-busy",))
    assert c.get("/api/local/tasks").json()["active_count"] == 0


def test_relative_paths_resolve_against_the_local_project_root(local_project):
    """Agent / 界面传相对路径时按本地项目根解析，而不是后端 cwd（QuantLab 仓库）。"""
    c, s, root, tmp = local_project
    (root / "验收输入").mkdir()
    evidence = root / "验收输入/关键证据摘要.md"
    evidence.write_text("# 关键证据\n", encoding="utf-8")

    # 1) 项目根下的相对路径可读
    assert s.local.authorize("验收输入/关键证据摘要.md") == evidence.resolve()
    # 2) 绝对路径照旧可读
    assert s.local.authorize(str(evidence)) == evidence.resolve()
    # 3) 越界仍然拒绝：相对 ../ 与绝对路径都不能绕过授权
    outside = tmp / "外部" / "secret.md"
    outside.parent.mkdir()
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(WorkflowError, match="授权"):
        s.local.authorize("../外部/secret.md")
    with pytest.raises(WorkflowError, match="授权"):
        s.local.authorize(str(outside))

    # 浏览与引用导入复用同一套解析（相对路径）
    browse = c.get("/api/local/files", params={"path": "验收输入"})
    assert browse.status_code == 200, browse.text
    assert [entry["name"] for entry in browse.json()["entries"]] == ["关键证据摘要.md"]
    imported = c.post("/api/local/files/import", json={"path": "验收输入/关键证据摘要.md", "mode": "reference"})
    assert imported.status_code == 200, imported.text
    assert imported.json()["source"] == str(evidence.resolve())
