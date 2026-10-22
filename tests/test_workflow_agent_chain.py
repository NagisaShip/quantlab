"""Exercise the actual chat bridge, not just the underlying HTTP save endpoint."""

from __future__ import annotations

import json

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents import platform_tools as PT
from quant_agent.agents.lab_state import new_project
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.platform import context
from quant_agent.workflows.contracts import WorkflowError
from quant_agent.workflows.service import WorkflowService
from quant_agent.workflows.transport import tool_text

STATS = """NODE={"kind":"table.test_return_stats","version":"1","title":"收益统计验收","inputs":{"data":{"type":"table"}},"outputs":{"report":{"type":"report"}},"parameters":{"type":"object","properties":{}},"requirements":[]}
def execute(ctx,inputs,params):
    values=[float(r['return']) for r in inputs['data']]
    return {'report':{'count':len(values),'mean':sum(values)/len(values)}}
"""


@pytest.fixture
def agent_workspace(tmp_path):
    reset_services()
    s = AppServices.build(store_root=str(tmp_path))
    set_services(s)
    with TestClient(create_app()) as c:
        me = login(c)
        file = c.post(
            "/api/workflows/files?name=agent.csv", content=b"id,return\n001,0.1\n002,-0.05\n003,0.04\n"
        ).json()
        with context.bind(me["workspace"]["id"], user_id=me["user"]["id"], role="admin"):
            yield c, s, file, me
    reset_services()


def flow_for(file):
    return {
        "format": "quantlab.workflow/1",
        "title": "Agent完整链路验收（构造数据）",
        "nodes": [
            {"id": "source", "kind": "source.csv", "version": "1", "params": {"file": file["name"]}},
            {
                "id": "check",
                "kind": "table.check",
                "version": "1",
                "params": {"required_columns": ["id", "return"], "unique_key": ["id"]},
            },
            {"id": "stats", "kind": "table.test_return_stats", "version": "1", "params": {}},
            {"id": "evidence", "kind": "report.collect", "version": "1", "params": {}},
        ],
        "edges": [
            {"from": {"node": "source", "port": "data"}, "to": {"node": "check", "port": "data"}},
            {"source": "check", "source_port": "data", "target": "stats", "target_port": "data"},
            {"source": "stats", "source_port": "report", "target": "evidence", "target_port": "report"},
        ],
    }


def test_chat_bridge_creates_revision_zero_then_runs_and_reads_real_evidence(agent_workspace):
    c, s, file, _me = agent_workspace
    from quant_agent.api.routes_lab import _lab

    lab = _lab()
    project = lab.store.save(new_project({"idea": "验证工作流编排，无策略调参"}))
    bridge = lab._platform_bridge(project["id"], {"workflow_revise"})

    def call(name, **args):
        result = bridge[name].handler(**args)
        assert not result.get("error"), result
        return result

    cat = call("workflow_catalog")
    assert cat["total"] > 10 and len(cat["cards"]) == 6 and cat["next_cursor"]
    source = call("workflow_sources", kind="table.check", version="1")
    assert source["files"][0]["path"] == "NODE.py"
    code = call("workflow_sources", kind="table.check", version="1", file_path="NODE.py", offset=0, limit=3000)
    assert "block_on_failure" in code["code"]
    published = call("workflow_definition_publish", code=STATS)
    with pytest.raises(WorkflowError, match="不可覆盖"):
        WorkflowService(s).publish_card(STATS.replace("sum(values)/len(values)", "0"))
    assert published["code_hash"]
    saved = call("platform_workflow_revise", workflow_id="", expected_revision=0, workflow=flow_for(file))
    assert saved["created"] and saved["revision"] == 1
    wid = saved["id"]
    assert saved["after"]["edges"][0]["source"] == "source" and "from" not in saved["after"]["edges"][0]
    assert saved["workflow_view"]["evidence"] == "saved"
    assert all(n["status"] == "not_run" for n in saved["workflow_view"]["nodes"])
    assert call("workflow_validate", workflow_id=wid)["ok"]
    run = call("workflow_run", workflow_id=wid)
    actual = call("workflow_inspect", workflow_id=wid, run_id=run["id"])
    assert actual["run"]["status"] == "completed", actual
    assert all(n["status"] in ("completed", "cached") for n in actual["workflow_view"]["nodes"])
    node = call("workflow_inspect", workflow_id=wid, run_id=run["id"], node_id="stats")["node"]
    artifact = call("workflow_artifact", artifact_hash=node["outputs"]["report"]["hash"])
    assert artifact["value"]["count"] == 3 and artifact["value"]["mean"] == pytest.approx(0.03)
    hash_value = actual["run"]["nodes"]["check"]["outputs"]["data"]["hash"]
    chart = call(
        "workflow_chart_preview", artifact_hash=hash_value, config={"chart_type": "line", "x": "id", "y": "return"}
    )
    assert chart["total"] == 3
    plan = {"mode": "grid", "axes": [{"node": "source", "parameter": "max_rows", "values": [10, 20]}]}
    assert len(call("workflow_experiment_plan", workflow_id=wid, plan=plan)["variants"]) == 2
    experiments = call("workflow_experiment_submit", workflow_id=wid, plan=plan, expected_revision=1)
    compared = call("workflow_compare", run_ids=[r["run"]["id"] for r in experiments["runs"]])
    assert all(r["state"] == "COMPLETED" for r in compared["experiments"])
    messages = lab.store.messages(project["id"])
    views = [m["data"]["workflow_view"] for m in messages if m.get("data", {}).get("workflow_view")]
    assert views and views[-1]["run_id"] == run["id"]
    assert all(m["data"]["status"] == "ok" for m in messages if m["kind"] == "tool")
    trace = c.get(f"/api/workflows/{wid}/view", params={"run_id": run["id"]})
    assert trace.status_code == 200 and trace.json()["revision"] == 1


def test_creation_conflicts_and_evidence_ownership(agent_workspace):
    c, s, file, me = agent_workspace
    from quant_agent.workflows import agent

    agent.publish_definition(STATS)
    a = agent.create(flow_for(file))
    b = agent.create(flow_for(file))
    with pytest.raises(WorkflowError, match="新版本"):
        agent.revise(workflow_id=a["id"], expected_revision=0, workflow=flow_for(file))
    with pytest.raises(WorkflowError, match="创建首版"):
        agent.revise(workflow_id="missing", expected_revision=1, workflow=flow_for(file))
    with pytest.raises(WorkflowError, match="非负整数"):
        agent.revise(workflow_id="", expected_revision=-1, workflow=flow_for(file))
    run = agent.run(a["id"])
    with pytest.raises(WorkflowError, match="不属于"):
        agent.inspect(b["id"], run["id"])
    assert c.get(f"/api/workflows/{b['id']}/view?run_id={run['id']}").status_code == 400
    agent.revise(workflow_id=a["id"], expected_revision=1, workflow={**flow_for(file), "title": "第二版"})
    old = c.get(f"/api/workflows/{a['id']}/view?revision=1").json()
    assert old["revision"] == 1 and old["title"] != "第二版"
    assert old["workflow_id"] == a["id"]
    assert agent.inspect(a["id"], run["id"])["workflow_view"]["revision"] == 1
    other = s.auth.create_workspace(owner_id=me["user"]["id"], name="隔离验证工作区")
    with context.bind(other, user_id=me["user"]["id"], role="admin"):
        with pytest.raises(WorkflowError):
            agent.inspect(a["id"], run["id"])


def test_network_errors_and_graph_examples_are_actionable(agent_workspace):
    c, _, _, _ = agent_workspace
    from quant_agent.workflows.agent import help_workflow, inspect_network
    from quantlab_research.network import build

    for wrong in ({"layers": []}, {"components": []}):
        with pytest.raises(ValueError, match="workflow_help"):
            inspect_network(wrong)
    with pytest.raises(ValueError, match="shape"):
        inspect_network({"nodes": [{"id": "in", "type": "input", "dim": 8}], "output": "in"})
    spec = inspect_network(
        {
            "nodes": [{"id": "in", "type": "input", "shape": [8]}, {"id": "out", "type": "dense", "out_features": 1}],
            "edges": [{"source": "in", "target": "out"}],
            "output": "out",
        }
    )
    assert spec["parameter_count"] == 9 and spec["graph"]["nodes"][1]["inputs"] == ["in"]
    assert help_workflow("network")["example"]
    if pytest.importorskip("torch"):
        assert sum(p.numel() for p in build(spec["graph"]).parameters()) == 9
    response = c.post(
        "/api/workflows/network/inspect", json={"graph": {"nodes": [{"id": "in", "type": "Input"}], "output": "in"}}
    )
    assert response.status_code == 400 and "shape" in response.text


def test_chart_without_run_and_wrong_namespace(agent_workspace):
    c, _, _, _ = agent_workspace
    from quant_agent.workflows.agent import preview_chart

    data = [{"x": 1, "y": 2}, {"x": 2, "y": 3}]
    assert preview_chart(config={"x": "x", "y": "y"}, data=data)["total"] == 2
    with pytest.raises(WorkflowError, match="资产sha256"):
        preview_chart(config={}, artifact_hash="a" * 64)
    response = c.post("/api/workflows/charts/preview", json={"config": {}, "artifact_hash": "a" * 64})
    assert response.status_code == 400 and "outputs" in response.text


def test_workflow_transport_is_parseable_and_explicit_about_truncation():
    value = {"id": "workflow-test", "revision": 1, "after": {"text": "界面" * 15000}}
    text = tool_text("platform_workflow_revise", value)
    assert len(text) <= 6000 and json.loads(text)["truncated"]
    assert json.loads(text)["id"] == "workflow-test"
    text = tool_text(
        "workflow_experiment_submit",
        {
            "runs": [{"name": "a", "run": {"id": "run-proof", "state": "COMPLETED", "manifest": "x" * 12000}}],
            "submitted": 1,
        },
    )
    assert json.loads(text)["runs"][0]["run"]["id"] == "run-proof"
    text = tool_text("workflow_sources", {"files": [{"code": '"\\\n' * 10000}]})
    assert len(text) <= 6000 and json.loads(text)["truncated"]


def test_zero_false_empty_are_values_not_missing(agent_workspace, monkeypatch):
    _, _, _, me = agent_workspace

    def handler(**args):
        return args

    tool = PT.PlatformTool(
        name="false_values",
        title="测试",
        scene="workflow",
        description="测试",
        when="测试",
        risk="confirm",
        params={k: {} for k in ("zero", "flag", "text")},
        required=["zero", "flag", "text"],
        handler=handler,
    )
    monkeypatch.setitem(PT.registry(), "false_values", tool)
    result = PT.invoke("false_values", {"zero": 0, "flag": False, "text": ""}, user=me["user"]["id"])
    assert result["record"]["status"] == "pending"
    assert (
        PT.invoke("false_values", {"zero": None, "flag": False, "text": ""}, user=me["user"]["id"])["record"]["status"]
        == "error"
    )


def test_catalog_paging_sources_and_tool_specs(agent_workspace):
    from quant_agent.workflows import agent

    page = agent.catalog(limit=3)
    second = agent.catalog(cursor=page["next_cursor"], limit=3)
    assert not ({x["kind"] for x in page["cards"]} & {x["kind"] for x in second["cards"]})
    assert len(json.dumps(page, ensure_ascii=False)) < 6000
    source = agent.inspect_sources("table.check", "1", "NODE.py", offset=0, limit=100)
    tail = agent.inspect_sources("table.check", "1", "NODE.py", offset=source["next_offset"], limit=100)
    assert source["code"] != tail["code"] and source["truncated"]
    specs = {n: (h, p) for n, h, _, p in agent.tool_specs()}
    assert agent.required_args(specs["workflow_revise"][0]) == ["workflow"]
    assert agent.required_args(specs["workflow_chart_preview"][0]) == ["config"]


def test_ask_tool_record_contains_view_and_missing_bridge_args_are_failed(agent_workspace):
    _, _, file, me = agent_workspace
    from quant_agent.api.routes_lab import _lab

    lab = _lab()
    project = lab.store.save(new_project({"idea": "缺参和轻量编排记录验收"}))
    bridge = lab._platform_bridge(project["id"], set())
    missing = bridge["workflow_create"].handler()
    assert missing.get("error")
    record = lab.store.messages(project["id"])[-1]
    assert record["data"]["status"] == "error"
    flow = {
        "format": "quantlab.workflow/1",
        "nodes": [{"id": "source", "kind": "source.csv", "version": "1", "params": {"file": file["name"]}}],
        "edges": [],
    }
    result = PT.invoke(
        "workflow_revise", {"workflow_id": "", "expected_revision": 0, "workflow": flow}, user=me["user"]["id"]
    )
    assert result["result"]["revision"] == 1
    assert result["record"]["workflow_view"]["format"] == "quantlab.workflow-view/1"
