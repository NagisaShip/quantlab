"""统一任务中心读模型（GET /api/tasks/overview）：聚合、状态映射、可见性、过滤与空列表。

只读投影：测试直接造 run / 研究室项目 / 定时任务，再断言接口把真实字段映射成统一结构。
"""

from __future__ import annotations

import time

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents.lab_state import LabStore
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform import context


@pytest.fixture()
def app(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


def _admin(app):
    client = TestClient(app)
    me = login(client)
    return client, me


def _member(app, admin, *, email="member@example.com", name="研究员"):
    code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    client = TestClient(app)
    me = login(client, email=email, password="Member01!", name=name, invite_code=code)
    return client, me


def _overview(client, **params):
    response = client.get("/api/tasks/overview", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _items(body):
    return {item["id"]: item for item in body["items"]}


def _run(ws, user, *, title, state="RUNNING", done=0, total=0, kind="backtest", mode="backtest", workspace=None):
    services = get_services()
    workspace_id = workspace or ws
    with context.bind(workspace_id, user_id=user):
        run = services.runs.create(workspace_id=workspace_id, user_id=user, kind=kind, mode=mode, title=title)
        services.db.execute(
            "UPDATE runs SET state=?, progress_done=?, progress_total=?, updated_at=? WHERE id=?",
            (state, done, total, int(time.time() * 1000), run["id"]),
        )
    return run["id"]


def _lab(client, *, title="研究室项目", status="running", plan=None, job=None):
    response = client.post("/api/lab/projects", json={"kind": "strategy", "title": title})
    assert response.status_code == 200, response.text
    pid = response.json()["project"]["id"]
    me = client.get("/api/auth/me").json()
    with context.bind(me["workspace"]["id"], user_id=me["user"]["id"]):
        LabStore(get_services().store).patch(pid, status=status, job=job, plan=plan)
    return pid


def test_overview_aggregates_active_runs_lab_and_scheduler(app):
    admin, me = _admin(app)
    ws, uid = me["workspace"]["id"], me["user"]["id"]
    running = _run(ws, uid, title="均线回测", state="RUNNING", done=3, total=10)
    queued = _run(ws, uid, title="参数优化", state="QUEUED")
    paused = _run(ws, uid, title="暂停的回测", state="PAUSED")
    finished = _run(ws, uid, title="已完成回测", state="COMPLETED")  # 终态不进入任务中心
    pid = _lab(
        admin,
        title="趋势研究",
        status="running",
        job={"kind": "chat", "started_at": time.time(), "phase": "分析数据"},
    )
    task = admin.post("/api/rd/nightshift/tasks", json={"kind": "factor", "idea": "挖掘动量因子"}).json()["task"]

    body = _overview(admin)
    items = _items(body)
    assert {running, queued, paused}.issubset(items)
    assert finished not in items

    run_item = items[running]
    assert run_item["source"] == "run"
    assert run_item["title"] == "均线回测"
    assert run_item["state"] == "running" and run_item["raw_state"] == "RUNNING"
    assert run_item["progress"] == {"done": 3, "total": 10, "text": ""}
    assert run_item["href"] == "#/settings/runs" and run_item["workspace_id"] == ws
    assert items[queued]["state"] == "queued" and items[paused]["state"] == "paused"

    lab_item = items[pid]
    assert lab_item["source"] == "lab" and lab_item["kind"] == "strategy"
    assert lab_item["state"] == "running" and lab_item["raw_state"] == "running"
    assert lab_item["progress"]["text"] == "分析数据"
    assert lab_item["href"] == f"#/lab/{pid}"

    scheduler_item = items[task["id"]]
    assert scheduler_item["source"] == "scheduler" and scheduler_item["state"] == "queued"
    assert scheduler_item["href"] == "#/long-running"

    assert body["counts"]["total"] == len(body["items"])
    assert body["counts"]["by_source"] == {"run": 3, "lab": 1, "scheduler": 1}
    assert body["counts"]["active"] == len(body["items"])
    assert isinstance(body["generated_at"], int) and body["generated_at"] > 0
    stamps = [item["updated_at"] for item in body["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_overview_state_mapping_uses_real_fields(app):
    admin, me = _admin(app)
    ws, uid = me["workspace"]["id"], me["user"]["id"]
    awaiting = _lab(admin, title="等回答", status="awaiting_user")
    paused_plan = _lab(
        admin,
        title="暂停计划",
        status="idle",
        plan={
            "status": "active",
            "autorun": False,
            "steps": [{"text": "第一步", "status": "doing"}, {"text": "第二步", "status": "pending"}],
        },
    )
    failed_plan = _lab(
        admin,
        title="失败计划",
        status="idle",
        plan={"status": "stopped", "steps": [{"text": "失败步骤", "status": "failed"}]},
    )
    done_plan = _lab(
        admin,
        title="完成计划",
        status="idle",
        plan={"status": "done", "steps": [{"text": "完成", "status": "done"}]},
    )
    queued_run = _run(ws, uid, title="排队回测", state="QUEUED")
    recovering_run = _run(ws, uid, title="恢复回测", state="RECOVERING")

    items = _items(_overview(admin, source="lab,run"))
    assert items[awaiting]["state"] == "awaiting_user" and items[awaiting]["raw_state"] == "awaiting_user"
    assert items[paused_plan]["state"] == "paused" and items[paused_plan]["raw_state"] == "active"
    assert items[paused_plan]["progress"] == {"done": 0, "total": 2, "text": "第一步"}
    assert items[failed_plan]["state"] == "failed" and items[failed_plan]["raw_state"] == "stopped"
    assert items[done_plan]["state"] == "done" and items[done_plan]["raw_state"] == "done"
    assert items[queued_run]["state"] == "queued" and items[queued_run]["raw_state"] == "QUEUED"
    assert items[recovering_run]["state"] == "recovering" and items[recovering_run]["raw_state"] == "RECOVERING"
    # 纯 idle、没有计划的项目不是任务
    idle = _lab(admin, title="空闲项目", status="idle")
    assert idle not in _items(_overview(admin))


def test_overview_hides_private_lab_projects_from_other_members(app):
    admin, _ = _admin(app)
    a, _a_me = _member(app, admin, email="a@example.com", name="研究员甲")
    b, _b_me = _member(app, admin, email="b@example.com", name="研究员乙")
    pid = _lab(
        a,
        title="甲的私有研究",
        status="running",
        job={"kind": "chat", "started_at": time.time(), "phase": "跑因子"},
    )

    assert pid in _items(_overview(a))
    assert pid not in _items(_overview(b))
    # 工作区管理员可以看全部（与 /api/lab/projects 的口径一致）
    assert pid in _items(_overview(admin))
    # 共享到工作区后，其他成员也能看到
    shared = a.put(f"/api/collab/shares/project/{pid}", json={"visibility": "workspace"})
    assert shared.status_code == 200, shared.text
    assert pid in _items(_overview(b))


def test_overview_scopes_runs_to_the_current_workspace(app):
    admin, me = _admin(app)
    ws = me["workspace"]["id"]
    mine = _run(ws, me["user"]["id"], title="本工作区回测", state="RUNNING")
    other_ws = admin.post("/api/workspaces", json={"name": "另一个工作区"}).json()["id"]
    other = _run(ws, me["user"]["id"], title="别的工作区回测", state="RUNNING", workspace=other_ws)

    body = _overview(admin)
    ids = _items(body)
    assert mine in ids and other not in ids
    assert all(item["workspace_id"] == ws for item in body["items"])


def test_overview_empty_list_limit_source_and_invalid_params(app):
    admin, me = _admin(app)
    ws, uid = me["workspace"]["id"], me["user"]["id"]

    empty = _overview(admin)
    assert empty["items"] == []
    assert empty["counts"] == {
        "total": 0,
        "active": 0,
        "by_source": {"run": 0, "lab": 0, "scheduler": 0},
        "by_state": {},
        "truncated": False,
    }

    for index in range(3):
        _run(ws, uid, title=f"回测 {index}", state="RUNNING")
    pid = _lab(admin, title="研究项目", status="running", job={"kind": "chat", "phase": "分析"})

    limited = _overview(admin, limit=2)
    assert len(limited["items"]) == 2 and limited["counts"]["truncated"] is True

    lab_only = _overview(admin, source="lab")
    assert {item["source"] for item in lab_only["items"]} == {"lab"}
    assert pid in _items(lab_only)
    assert lab_only["counts"]["by_source"] == {"run": 0, "lab": 1, "scheduler": 0}

    run_only = _overview(admin, source="run")
    assert {item["source"] for item in run_only["items"]} == {"run"}
    assert len(run_only["items"]) == 3

    # 非法参数：422（客户端参数错误）
    assert admin.get("/api/tasks/overview", params={"source": "bogus"}).status_code == 422
    assert admin.get("/api/tasks/overview", params={"source": "run,"}).status_code == 200
    assert admin.get("/api/tasks/overview", params={"limit": 0}).status_code == 422
    assert admin.get("/api/tasks/overview", params={"limit": 201}).status_code == 422
    assert admin.get("/api/tasks/overview", params={"limit": "many"}).status_code == 422


def test_overview_returns_items_in_local_mode(tmp_path, monkeypatch):
    """本地项目模式（无账号协作）同样能返回统一任务视图。"""
    project = tmp_path / "本地项目"
    project.mkdir()
    monkeypatch.setenv("QUANTLAB_MODE", "local")
    monkeypatch.setenv("QUANTLAB_PROJECT", str(project))
    monkeypatch.setenv("QUANTLAB_LIBRARY", str(tmp_path / "library"))
    monkeypatch.setenv("QUANTLAB_CREDENTIALS", str(tmp_path / "keys"))
    reset_services()
    services = AppServices.build(store_root=str(project / ".quantlab"))
    set_services(services)
    try:
        with TestClient(create_app()) as client:
            client.headers["X-Requested-With"] = "quantlab-test"
            me = client.get("/api/auth/me").json()
            assert me["mode"] == "local"
            ws, uid = me["workspace"]["id"], me["user"]["id"]
            client.headers["X-Workspace-Id"] = ws
            with context.bind(ws, user_id=uid, role="admin"):
                run = services.runs.create(
                    workspace_id=ws, user_id=uid, kind="backtest", mode="backtest", title="本地回测"
                )
            created = client.post("/api/lab/projects", json={"title": "本地研究"})
            assert created.status_code == 200, created.text
            pid = created.json()["project"]["id"]
            with context.bind(ws, user_id=uid, role="admin"):
                LabStore(services.store).patch(pid, status="running", job={"kind": "chat", "phase": "本地分析"})
            body = client.get("/api/tasks/overview").json()
            items = _items(body)
            assert items[run["id"]]["source"] == "run" and items[run["id"]]["state"] == "queued"
            assert items[pid]["source"] == "lab" and items[pid]["state"] == "running"
            assert body["counts"]["by_source"] == {"run": 1, "lab": 1, "scheduler": 0}
    finally:
        reset_services()
