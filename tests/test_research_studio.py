"""Research Studio contracts: real artifacts, Agent transport and shared policies."""

from __future__ import annotations

import hashlib

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.platform import context
from quant_agent.workflows import agent
from quant_agent.workflows.contracts import WorkflowError
from quant_agent.workflows.runtime import Runner
from quant_agent.workflows.service import WorkflowService
from quant_agent.workflows.templates import csv_research


@pytest.fixture
def studio(tmp_path):
    reset_services()
    s = AppServices.build(store_root=str(tmp_path))
    set_services(s)
    with TestClient(create_app()) as c:
        me = login(c)
        with context.bind(me["workspace"]["id"], user_id=me["user"]["id"]):
            yield c, s, WorkflowService(s), me
    reset_services()


def test_table_pagination_csv_and_workspace_boundary(studio):
    c, _, service, _ = studio
    ref = Runner(service.root, service.registry()).artifact(
        [{"id": i, "value": i * 2} for i in range(25)], {"type": "table"}
    )
    url = "/api/workflows/artifacts/" + ref["hash"]
    first = c.get(url, params={"limit": 20}).json()
    assert first["total"] == 25 and first["next_offset"] == 20
    second = c.get(url, params={"limit": 20, "offset": 20}).json()
    assert len(second["value"]) == 5 and not second["truncated"]
    csv = c.get(url + "/download?format=csv")
    assert csv.status_code == 200 and "24,48" in csv.text
    assert "attachment" in csv.headers["content-disposition"]
    assert c.get(url, params={"offset": -1}).status_code == 400
    new = c.post("/api/workspaces", json={"name": "isolated"}).json()
    assert c.get(url, headers={"X-Workspace-Id": new["id"]}).status_code == 400


def test_binary_bundle_verified_before_display(studio):
    c, _, service, _ = studio
    (service.files / "generated").mkdir(exist_ok=True)
    path = service.files / "generated" / "figure.png"
    path.write_bytes(b"verified-test-image")
    file = {"file": "generated/figure.png", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    ref = Runner(service.root, service.registry()).artifact({"files": [file]}, {"type": "report"})
    view = c.get("/api/workflows/artifacts/" + ref["hash"]).json()
    assert view["files"][0]["image"]
    assert c.get(view["files"][0]["url"]).content == b"verified-test-image"
    path.write_bytes(b"tampered")
    assert c.get("/api/workflows/artifacts/" + ref["hash"]).status_code == 400


def test_plugins_visibility_and_agent_are_independent(studio):
    c, s, _, _ = studio
    r = c.put("/api/auth/research-plugins/workflow", json={"visible": False, "agent": False})
    assert r.status_code == 200
    p = next(p for p in r.json()["plugins"] if p["id"] == "workflow")
    assert p["enabled"] and not p["visible"] and not p["agent"]
    # HTTP remains usable while the personal Agent switch is off.
    assert c.get("/api/workflows/catalog").status_code == 200
    with pytest.raises(WorkflowError, match="Agent"):
        s.teams.tools.get("workflow_catalog")()
    assert s.teams.tools.get("workflow_plugins")()["plugins"]
    assert c.put("/api/auth/research-plugins/workflow", json={"enabled": False}).status_code == 400


def test_disabled_experiments_block_both_api_and_agent(studio):
    c, s, _, _ = studio
    wid = c.post("/api/workflows", json={"workflow": csv_research()}).json()["id"]
    assert c.put("/api/auth/research-plugins/experiments", json={"enabled": False}).status_code == 200
    assert c.post(f"/api/workflows/{wid}/experiments/plan", json={"plan": {}}).status_code == 400
    with pytest.raises(WorkflowError, match="实验管理"):
        s.teams.tools.get("workflow_experiment_plan")(wid, {})
    assert c.get("/api/workflows/experiments").status_code == 200


def test_notes_conflict_and_foreign_evidence_rejected(studio):
    c, _, _, _ = studio
    wid = c.post("/api/workflows", json={"workflow": csv_research()}).json()["id"]
    path = f"/api/workflows/{wid}/notes"
    assert c.get(path).json()["version"] == 0
    assert c.put(path, json={"text": "研究发现", "expected_version": 0}).json()["version"] == 1
    assert c.put(path, json={"text": "旧版本覆盖", "expected_version": 0}).status_code == 400
    assert c.get(path).json()["text"] == "研究发现"
    assert (
        c.put(path, json={"text": "伪造引用", "run_ids": ["run-not-exist"], "expected_version": 1}).status_code != 200
    )


def test_agent_artifact_transport_preserves_rich_view(studio):
    _, s, service, _ = studio
    from quant_agent.agents import platform_tools as pt

    ref = Runner(service.root, service.registry()).artifact([{"x": 1}], {"type": "table"})
    result = pt.invoke("workflow_artifact", {"artifact_hash": ref["hash"]}, user="test")
    assert result["record"]["artifact_view"]["value"] == [{"x": 1}]
    assert result["record"]["artifact_view"]["workspace_id"] == context.current_workspace()
    assert s.teams.tools.get("workflow_cancel") and s.teams.tools.get("workflow_export")


def test_real_matplotlib_artifact_is_downloadable_and_searchable(studio):
    c, _, _, _ = studio
    result = agent.preview_chart({"chart_type": "line", "x": "x", "y": "y"}, data=[{"x": 1, "y": 2}, {"x": 2, "y": 4}])
    view = result["artifact_view"]
    png = next(file for file in view["files"] if file["image"])
    assert c.get(png["url"]).content.startswith(b"\x89PNG")
    found = c.get("/api/workflows/artifacts").json()["artifacts"]
    assert any(row["hash"] == result["artifact_hash"] and row["mode"] == "preview" for row in found)


def test_member_cannot_change_workspace_plugin_policy(studio):
    _, _, service, me = studio
    from quant_agent.workflows.plugins import update

    with context.bind(me["workspace"]["id"], user_id="member", role="member"):
        with pytest.raises(WorkflowError, match="管理员"):
            update(service, "charts", {"enabled": False})
        assert update(service, "charts", {"agent": False})["plugins"]


def test_generated_csv_content_has_bounded_preview(studio):
    c, _, service, _ = studio
    (service.files / "result.csv").write_text("year,value\n2022,1\n2023,2\n", encoding="utf-8")
    result = c.get("/api/workflows/files/preview?name=result.csv&limit=1").json()
    assert result["value"] == [{"year": "2022", "value": "1"}]
    assert result["next_offset"] == 1 and result["truncated"]
    assert c.get("/api/workflows/files/preview?name=../../secret.csv").status_code == 400


def test_activity_exposes_agent_runs_and_revision_inspection(studio):
    c, s, service, _ = studio
    source = service.import_data("activity.csv", data=[{"id": 1, "value": 2}])
    flow = csv_research()
    flow["nodes"][0]["params"]["file"] = source["name"]
    saved = service.save(flow)
    result = s.teams.tools.get("workflow_run")(saved["id"])
    activity = c.get(f"/api/workflows/{saved['id']}/activity").json()
    assert activity["runs"][0]["id"] == result["id"]
    assert activity["runs"][0]["origin"] == "agent"
    assert activity["runs"][0]["state"] == "COMPLETED"
    flow["title"] = "新研究名称"
    service.save(flow, wid=saved["id"], expected_revision=1)
    assert agent.inspect(saved["id"], revision=1)["workflow"]["title"] != flow["title"]


def test_plugin_impact_and_execution_include_nested_modules(studio):
    c, _, _, _ = studio
    flow = {
        "format": "quantlab.workflow/1",
        "title": "封装图表研究",
        "nodes": [
            {"id": "source", "kind": "source.csv", "version": "1", "params": {"file": "x.csv"}},
            {"id": "chart", "kind": "research.chart", "version": "1", "params": {"x": "x", "y": "y"}},
        ],
        "edges": [{"id": "e", "source": "source", "source_port": "data", "target": "chart", "target_port": "data"}],
    }
    grouped = c.post(
        "/api/workflows/subflows/encapsulate",
        json={"workflow": flow, "selected": ["source", "chart"], "kind": "user.chart_study", "title": "图表模块"},
    )
    assert grouped.status_code == 200, grouped.text
    wid = c.post("/api/workflows", json={"workflow": grouped.json()["workflow"]}).json()["id"]
    affected = c.get("/api/auth/research-plugins/charts/impact").json()["workflows"]
    assert any(w["id"] == wid for w in affected)
    assert c.put("/api/auth/research-plugins/charts", json={"enabled": False}).status_code == 200
    result = c.post(f"/api/workflows/{wid}/run", json={})
    assert result.status_code == 400 and "图表工作台" in result.text


def test_data_agent_preference_applies_to_reads_and_chart_dependencies(studio):
    c, s, _, _ = studio
    assert c.put("/api/auth/research-plugins/data", json={"agent": False}).status_code == 200
    with pytest.raises(WorkflowError, match="数据与产物"):
        s.teams.tools.get("workflow_artifacts")()
    with pytest.raises(WorkflowError, match="数据与产物"):
        s.teams.tools.get("workflow_chart_preview")({"chart_type": "line", "x": "x", "y": "y"}, data=[])
    assert c.get("/api/workflows/artifacts").status_code == 200
    from quant_agent.agents import platform_tools as pt

    schemas = {tool.name for tool in pt.tools_for(["workflow"])}
    assert "workflow_artifact" not in schemas and "workflow_chart_preview" not in schemas
    assert "workflow_plugins" in schemas


def test_export_tool_produces_live_task_and_download_contract(studio):
    c, s, service, _ = studio
    source = service.import_data("export.csv", data=[{"id": 1, "value": 2}])
    flow = csv_research()
    flow["nodes"][0]["params"]["file"] = source["name"]
    saved = service.save(flow)
    out = s.teams.tools.get("workflow_export")(workflow_id=saved["id"], include_wheels=False)
    assert out["task_view"]["state"] == "COMPLETED", out.get("error_message")
    assert c.get(out["task_view"]["summary"]["download"]).content.startswith(b"PK")
