from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture
def research_client(tmp_path):
    reset_services()
    services = AppServices.build(store_root=str(tmp_path))
    set_services(services)
    with TestClient(create_app()) as client:
        login(client)
        yield client, services
    reset_services()


def test_component_inspection_errors_are_reviewable(research_client):
    c, _ = research_client
    invalid = c.post("/api/workflows/network/inspect", json={"graph": {"nodes": []}})
    assert invalid.status_code == 400, invalid.text
    from quantlab_research.network import DEFAULT

    result = c.post("/api/workflows/network/inspect", json={"graph": DEFAULT})
    assert result.status_code == 200 and result.json()["parameter_count"] == 321


def test_existing_asset_is_frozen_and_uses_shared_engine(research_client):
    pytest.importorskip("pandas")
    c, s = research_client
    # Store writes here use the current request's workspace store.
    from quant_agent.platform import context

    me = c.get("/api/auth/me").json()
    with context.bind(me["workspace"]["id"], user_id=me["user"]["id"], role="admin"):
        s.store.upsert("factors", {"id": "f-test", "name": "两期收益", "expression": "returns(2)"})
    assets = c.get("/api/workflows/assets")
    assert assets.status_code == 200 and any(a["id"] == "f-test" for a in assets.json()["assets"])
    result = c.post("/api/workflows/assets/snapshot", json={"domain": "factor", "asset_id": "f-test"})
    assert result.status_code == 200, result.text
    definition = result.json()
    assert definition["parameters"]["properties"]["expression"]["default"] == "returns(2)"
    assert definition["asset"]["snapshot"]["expression"] == "returns(2)"
    assert definition["subflow"]["workflow"]["nodes"][0]["kind"] == "research.native_factor"
    assert c.post("/api/workflows/assets/snapshot", json={"domain": "factor", "asset_id": "missing"}).status_code == 400

    file = c.post(
        "/api/workflows/files?name=prices.csv",
        content=b"symbol,date,close\n001,2024-01-01,100\n001,2024-01-02,101\n001,2024-01-03,102\n",
    ).json()
    flow = {
        "format": "quantlab.workflow/1",
        "nodes": [
            {"id": "source", "kind": "source.csv", "version": "1", "params": {"file": file["name"]}},
            {"id": "research", "kind": definition["kind"], "version": "1", "params": {}},
        ],
        "edges": [{"source": "source", "source_port": "data", "target": "research", "target_port": "data"}],
    }
    saved = c.post("/api/workflows", json={"workflow": flow}).json()
    run = c.post(f"/api/workflows/{saved['id']}/run", json={}).json()
    report = c.get("/api/workflows/runs/" + run["id"]).json()["report"]
    assert report["status"] == "completed", report
    child = report["nodes"]["research"]["child_run_id"]
    assert c.get(f"/api/workflows/runs/{run['id']}/children/{child}").status_code == 200
    assert c.get(f"/api/workflows/runs/{run['id']}/children/not-a-child").status_code == 400


def test_chart_preview_renders_without_creating_a_training_run(research_client):
    pytest.importorskip("matplotlib")
    c, _ = research_client
    response = c.post(
        "/api/workflows/charts/preview",
        json={"data": [{"x": 1, "y": 2}, {"x": 2, "y": 4}], "config": {"x": "x", "y": "y", "chart_type": "line"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["png"].startswith("data:image/png;base64,")
    assert "<svg" in response.json()["svg"]
    invalid = c.post(
        "/api/workflows/charts/preview", json={"data": [{"x": 1, "y": 2}], "config": {"chart_type": "not-real"}}
    )
    assert invalid.status_code == 400


def test_grid_submissions_freeze_variants_and_leave_original_saved_revision(research_client):
    c, s = research_client
    from quant_agent.workflows.templates import csv_research

    f = c.post("/api/workflows/files?name=input.csv", content=b"id,value\n001,10\n").json()
    flow = csv_research()
    flow["nodes"][0]["params"]["file"] = f["name"]
    saved = c.post("/api/workflows", json={"workflow": flow}).json()
    wid = saved["id"]
    baseline = c.get(f"/api/workflows/{wid}").json()
    plan = {"mode": "grid", "axes": [{"node": "source", "parameter": "max_rows", "values": [10, 20]}]}
    preview = c.post(f"/api/workflows/{wid}/experiments/plan", json={"plan": plan})
    assert preview.status_code == 200, preview.text
    assert len(preview.json()["variants"]) == 2
    runs = c.post(f"/api/workflows/{wid}/experiments/submit", json={"revision": saved["revision"], "plan": plan})
    assert runs.status_code == 200, runs.text
    assert runs.json()["submitted"] == 2 and runs.json()["errors"] == []
    assert c.get(f"/api/workflows/{wid}").json() == baseline
    ids = [r["run"]["id"] for r in runs.json()["runs"]]
    comparison = c.post("/api/workflows/experiments/compare", json={"run_ids": ids})
    assert comparison.status_code == 200, comparison.text
    assert all(r["state"] == "COMPLETED" for r in comparison.json()["experiments"])
    assert s.teams.tools.get("workflow_network_inspect") is not None


def test_composite_code_browser_includes_child_functions_and_real_sources(research_client):
    c, _ = research_client
    result = c.get("/api/workflows/definitions/module.network/1/sources")
    assert result.status_code == 200, result.text
    files = result.json()["files"]
    assert any(f["path"] == "cards/research.train_network@1.py" for f in files)
    assert any(f["path"] == "quantlab_research/learning.py" for f in files)
