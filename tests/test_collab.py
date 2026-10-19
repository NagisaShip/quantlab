"""工作区协作：群组讨论与任务摊派、可见性、编辑人员标记与血缘、通知中心与屏蔽、问题报告与上传、
群组模型额度与自然周期限额、部署管理员运维（封禁、重置密码、公告、开放注册）。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform import context

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SIMPLE = """
def initialize(ctx):
    set_benchmark(ctx.securities[0])
    g.fast, g.slow = 12, 48
    run_daily(rebalance, time="close")

def rebalance(ctx):
    s = ctx.securities[0]
    closes = history(s, g.slow, "close")
    if len(closes) < g.slow:
        return
    fast = sum(closes[-g.fast:]) / g.fast
    slow = sum(closes) / g.slow
    order_target_percent(s, 0.8 if fast > slow else 0.0)
"""


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QAGENT_OPEN_REGISTRATION", raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


def _team(app):
    """管理员 + 两名成员（A、B），返回三个已登录的客户端与各自的 user_id。"""
    admin = TestClient(app)
    login(admin)
    users = {"admin": (admin, admin.get("/api/auth/me").json()["user"]["id"])}
    for key, email, name in (("a", "a@example.com", "研究员甲"), ("b", "b@example.com", "研究员乙")):
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        c = TestClient(app)
        me = login(c, email=email, password="Member01!", name=name, invite_code=code)
        users[key] = (c, me["user"]["id"])
    return users


def _strategy(client, name="协作策略"):
    return client.post("/api/research/strategies", json={"name": name, "template_id": "ma_cross"}).json()["strategy"]


# ---------------------------------------------------------------- 群组与任务
def test_groups_messages_mentions_tasks_and_mutes(app):
    u = _team(app)
    a, a_id = u["a"]
    b, b_id = u["b"]
    admin, _ = u["admin"]

    g = a.post("/api/collab/groups", json={"name": "趋势小组", "visibility": "private"}).json()["group"]
    assert g["my_role"] == "owner" and g["member_count"] == 1
    # 私密群：其他人看不到、不能自己加入
    assert all(x["id"] != g["id"] for x in b.get("/api/collab/groups").json()["groups"])
    assert b.post(f"/api/collab/groups/{g['id']}/join").status_code == 403
    # 工作区管理员能看到全部群组
    assert any(x["id"] == g["id"] for x in admin.get("/api/collab/groups").json()["groups"])

    r = a.post(f"/api/collab/groups/{g['id']}/members", json={"user_ids": [b_id, "not-a-member"]})
    assert r.json()["added"] == [b_id]
    assert b.get("/api/inbox").json()["items"][0]["kind"] == "group"

    m = a.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "@研究员乙 看看这条回测"}).json()["message"]
    inbox = b.get("/api/inbox?unread=true").json()
    assert inbox["items"][0]["kind"] == "mention" and inbox["unread"]["mention"] == 1
    assert b.get("/api/collab/groups").json()["groups"][0]["unread"] >= 1
    b.post(f"/api/collab/groups/{g['id']}/read", json={"last_id": m["id"]})
    assert b.get("/api/collab/groups").json()["groups"][0]["unread"] == 0
    # 普通群消息也进入通知中心，并在打开群组已读时同步清除
    ordinary = a.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "普通群消息：复测进度"}).json()["message"]
    inbox = b.get("/api/inbox?unread=true").json()
    assert inbox["items"][0]["kind"] == "group"
    assert inbox["items"][0]["body"] == "普通群消息：复测进度"
    assert inbox["items"][0]["source_type"] == "message"
    b.post(f"/api/collab/groups/{g['id']}/read", json={"last_id": ordinary["id"]})
    assert b.get("/api/inbox?unread=true").json()["unread"].get("group", 0) == 0
    # 回复别人的消息也会通知对方；空消息被拒绝
    b.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "收到", "reply_to": m["id"]})
    assert a.get("/api/inbox").json()["items"][0]["kind"] == "mention"
    assert a.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "  "}).status_code == 400

    # 任务摊派：指派 → 通知负责人；改状态 → 通知创建者
    t = a.post(
        f"/api/collab/groups/{g['id']}/tasks", json={"title": "复测 2 倍成本", "assignee_id": b_id, "priority": "high"}
    ).json()["task"]
    assert t["assignee_name"] == "研究员乙" and t["status"] == "todo"
    assert b.get("/api/inbox").json()["items"][0]["kind"] == "task"
    assert [x["id"] for x in b.get("/api/collab/tasks/mine").json()["tasks"]] == [t["id"]]
    r = b.put(f"/api/collab/tasks/{t['id']}", json={"status": "done"})
    assert r.json()["task"]["status"] == "done" and r.json()["task"]["done_at"]
    assert a.get("/api/inbox").json()["items"][0]["title"].endswith("已完成")
    # 不能指派给群外的人
    assert (
        a.post(f"/api/collab/groups/{g['id']}/tasks", json={"title": "x", "assignee_id": "nobody"}).status_code == 400
    )

    # 屏蔽：屏蔽甲后，甲 @ 乙不再进乙的通知中心，群消息标记为已屏蔽
    assert (
        b.post("/api/inbox/mutes", json={"target_type": "user", "target_id": a_id, "label": "研究员甲"}).status_code
        == 200
    )
    before = b.get("/api/inbox").json()["unread"].get("total", 0)
    a.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "@研究员乙 再看一下"})
    assert b.get("/api/inbox").json()["unread"].get("total", 0) == before
    msgs = b.get(f"/api/collab/groups/{g['id']}/messages").json()["messages"]
    assert msgs[-1]["muted"] is True
    # 屏蔽整类通知
    b.delete(f"/api/inbox/mutes/user/{a_id}")
    b.post("/api/inbox/mutes", json={"target_type": "kind", "target_id": "mention"})
    before = b.get("/api/inbox").json()["unread"].get("mention", 0)
    a.post(f"/api/collab/groups/{g['id']}/messages", json={"body": "@研究员乙 第三次"})
    assert b.get("/api/inbox").json()["unread"].get("mention", 0) == before
    assert {m["target_type"] for m in b.get("/api/inbox/mutes").json()["mutes"]} == {"kind"}
    assert b.post("/api/inbox/mutes", json={"target_type": "kind", "target_id": "announce"}).status_code == 400

    # 已读与清理
    assert b.post("/api/inbox/read", json={}).json()["updated"] >= 1
    assert b.get("/api/inbox/summary").json()["unread"]["total"] == 0
    assert b.post("/api/inbox/clear", json={"read_only": True}).json()["deleted"] >= 1

    # 群主不能退群；成员可以；转让群主
    assert a.delete(f"/api/collab/groups/{g['id']}/members/{a_id}").status_code == 400
    a.put(f"/api/collab/groups/{g['id']}/members/{b_id}", json={"role": "owner"})
    grp = b.get(f"/api/collab/groups/{g['id']}").json()["group"]
    assert grp["owner_id"] == b_id and {m["user_id"]: m["role"] for m in grp["members"]}[a_id] == "admin"


# ---------------------------------------------------------------- 可见性
def test_strategy_and_project_visibility(app):
    u = _team(app)
    a, a_id = u["a"]
    b, b_id = u["b"]
    admin, _ = u["admin"]
    s = _strategy(a)
    slug = s["slug"]
    # 默认工作区内公开（与旧版本一致）
    assert any(x["slug"] == slug for x in b.get("/api/studio/strategies").json()["strategies"])
    share = a.get(f"/api/collab/shares/strategy/{slug}").json()
    assert share["share"]["owner_id"] == a_id and share["can_edit"]
    # 别人不能改作者的可见性
    assert b.put(f"/api/collab/shares/strategy/{slug}", json={"visibility": "private"}).status_code == 403

    a.put(f"/api/collab/shares/strategy/{slug}", json={"visibility": "private"})
    assert all(x["slug"] != slug for x in b.get("/api/studio/strategies").json()["strategies"])
    assert b.get(f"/api/studio/strategies/{slug}").status_code == 403
    assert any(x["slug"] == slug for x in admin.get("/api/studio/strategies").json()["strategies"])

    # 群组内公开：只有群成员看得到，并收到共享通知
    g = a.post("/api/collab/groups", json={"name": "共享组", "members": [b_id]}).json()["group"]
    assert (
        a.put(f"/api/collab/shares/strategy/{slug}", json={"visibility": "group", "group_ids": []}).status_code == 400
    )
    a.put(f"/api/collab/shares/strategy/{slug}", json={"visibility": "group", "group_ids": [g["id"]]})
    assert b.get(f"/api/studio/strategies/{slug}").status_code == 200
    assert any(n["kind"] == "share" for n in b.get("/api/inbox").json()["items"])
    shared = b.get(f"/api/collab/groups/{g['id']}/shared").json()
    assert shared["strategies"][0]["id"] == slug and shared["strategies"][0]["owner_name"] == "研究员甲"

    # 研究项目同样可以设为仅自己
    pid = a.post("/api/lab/projects", json={"title": "私人研究"}).json()["project"]["id"]
    assert all(p["id"] != pid for p in b.get("/api/lab/projects").json()["projects"])
    a.put(f"/api/collab/shares/project/{pid}", json={"visibility": "private"})
    assert all(p["id"] != pid for p in b.get("/api/lab/projects").json()["projects"])
    assert b.get(f"/api/lab/projects/{pid}").status_code == 403


# ---------------------------------------------------------------- 编辑标记与血缘
def test_edit_marks_and_lineage_with_clone(app):
    u = _team(app)
    a, a_id = u["a"]
    b, b_id = u["b"]
    s = _strategy(a, "血缘策略")
    slug = s["slug"]
    detail = b.get(f"/api/studio/strategies/{slug}").json()["strategy"]
    # 乙改了一版
    r = b.post(
        "/api/studio/strategies",
        json={
            "name": detail["name"],
            "slug": slug,
            "code": SIMPLE,
            "symbol": "BTC/USDT",
            "timeframe": "1h",
            "allow_issues": True,
        },
    )
    assert r.status_code == 200, r.text
    edits = a.get(f"/api/collab/edits/strategy/{slug}").json()["edits"]
    assert [(e["user_id"], e["action"]) for e in edits] == [(a_id, "create"), (b_id, "edit")]
    assert edits[1]["parent_version"] == edits[0]["version"]
    eds = a.get("/api/collab/editors/strategy").json()["editors"][slug]
    assert {e["name"] for e in eds} == {"研究员甲", "研究员乙"}

    # 跑一次回测，挂到当前版本上
    bt = a.post(
        "/api/research/backtests", json={"slug": slug, "source": "synthetic", "bars_limit": 400, "warmup_bars": 60}
    ).json()
    # 分享到广场后被乙克隆：血缘里出现克隆边
    post = a.post(
        "/api/community/posts",
        json={"type": "strategy", "title": "血缘测试", "body": "x", "strategy_slug": slug, "backtest_id": bt["id"]},
    ).json()["post"]
    clone = b.post(f"/api/community/posts/{post['id']}/clone").json()["strategy"]
    lin = a.get(f"/api/collab/lineage?slug={slug}").json()
    by_slug = {}
    for n in lin["nodes"]:
        by_slug.setdefault(n["slug"], []).append(n)
    assert len(by_slug[slug]) == 2 and clone["slug"] in by_slug
    kinds = {e["kind"] for e in lin["edges"]}
    assert kinds == {"edit", "clone"}
    cur = next(n for n in by_slug[slug] if n["current"])
    assert cur["author"]["name"] == "研究员乙" and cur["backtests"][0]["id"] == bt["id"]
    assert by_slug[clone["slug"]][0]["action"] == "clone"
    # 被克隆的作者收到通知
    assert any(n["kind"] == "like" and "克隆" in n["title"] for n in a.get("/api/inbox").json()["items"])


# ---------------------------------------------------------------- 广场：来源、引用、Agent 发帖
def test_post_source_refs_and_agent_publish(app):
    u = _team(app)
    a, a_id = u["a"]
    b, _ = u["b"]
    s = _strategy(a, "引用策略")
    bt = a.post(
        "/api/research/backtests", json={"slug": s["slug"], "source": "synthetic", "bars_limit": 400, "warmup_bars": 60}
    ).json()
    up = a.post("/api/uploads?filename=chart.png", content=PNG, headers={"Content-Type": "image/png"}).json()["file"]
    body = f"见回测：\n{{{{ref:backtest:{bt['id']}}}}}\n截图：\n{{{{ref:file:{up['id']}}}}}"
    post = a.post(
        "/api/community/posts",
        json={
            "type": "article",
            "title": "带引用的文章",
            "body": body,
            "refs": [
                {"type": "backtest", "id": bt["id"]},
                {"type": "file", "id": up["id"]},
                {"type": "backtest", "id": "missing"},
                {"type": "evil", "id": "x"},
            ],
        },
    ).json()["post"]
    assert post["source"]["kind"] == "human"
    assert [r["type"] for r in post["refs"]] == ["backtest", "file"]
    assert post["refs"][0]["summary"]["sharpe"] is not None and post["refs"][1]["mime"] == "image/png"
    # 评论 → 作者收到通知
    b.post(f"/api/community/posts/{post['id']}/comments", json={"body": "不错"})
    assert a.get("/api/inbox").json()["items"][0]["kind"] == "comment"

    # Agent 代发：来源标记为 agent，可以按来源筛选
    from quant_agent.agents.platform_tools import t_community_post

    ws = a.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws, user_id=a_id, role="member") as ctx:
        ctx.user_name = "研究员甲"
        out = t_community_post("Agent 周报", "本周结论……", tags=["研究"])
    assert "post_id" in out, out
    agent_posts = b.get("/api/community/posts?source=agent").json()["items"]
    assert [p["id"] for p in agent_posts] == [out["post_id"]]
    assert agent_posts[0]["source"]["on_behalf_of"] == "研究员甲"


# ---------------------------------------------------------------- 问题报告与上传
def test_issue_reports_with_attachments(app):
    u = _team(app)
    a, _a_id = u["a"]
    b, _ = u["b"]
    admin, _ = u["admin"]
    # 类型与内容必须一致；不支持的类型拒绝
    assert (
        a.post("/api/uploads?filename=x.png", content=b"<html>", headers={"Content-Type": "image/png"}).status_code
        == 400
    )
    assert (
        a.post(
            "/api/uploads?filename=x.exe", content=b"MZ", headers={"Content-Type": "application/x-msdownload"}
        ).status_code
        == 400
    )
    up = a.post("/api/uploads?filename=err.png", content=PNG, headers={"Content-Type": "image/png"}).json()["file"]
    r = a.get(f"/api/uploads/{up['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"

    iss = a.post(
        "/api/issues",
        json={
            "title": "回测页报错",
            "body": "点开就白屏",
            "category": "bug",
            "severity": "high",
            "attachments": [up["id"]],
            "context": {"page": "backtest"},
        },
    ).json()["issue"]
    assert iss["attachments"][0]["filename"] == "err.png" and iss["status"] == "open"
    assert any(n["kind"] == "issue" for n in admin.get("/api/inbox").json()["items"])
    # 其他成员看不到别人的报告
    assert b.get(f"/api/issues/{iss['id']}").status_code == 404
    assert b.get("/api/issues?scope=all").json()["items"] == []
    # 管理员回复 → 状态变为已回复，提交者收到通知（不受屏蔽影响）
    a.post("/api/inbox/mutes", json={"target_type": "kind", "target_id": "comment"})
    r = admin.post(f"/api/issues/{iss['id']}/replies", json={"body": "已复现，正在修"})
    assert r.json()["issue"]["status"] == "replied"
    assert a.get("/api/inbox").json()["items"][0]["kind"] == "issue"
    # 提交者补充后重新变为待处理；成员不能把状态改成已解决，但可以关闭
    assert a.post(f"/api/issues/{iss['id']}/replies", json={"body": "补充日志"}).json()["issue"]["status"] == "open"
    assert a.put(f"/api/issues/{iss['id']}/status", json={"status": "resolved"}).status_code == 403
    assert (
        admin.put(f"/api/issues/{iss['id']}/status", json={"status": "resolved"}).json()["issue"]["status"]
        == "resolved"
    )
    items = admin.get("/api/issues?scope=all").json()
    assert items["is_staff"] and items["items"][0]["reply_count"] == 2


# ---------------------------------------------------------------- 限额：自然周期与群组
def test_calendar_windows_and_group_quotas(app):
    from quant_agent.llm.hub import window_start

    # 自然周期起点：周一 00:00 / 每月 1 日 00:00（上海时区）
    t = 1_760_000_000  # 2025-10-09 周四
    day, week, month = (window_start(w, "Asia/Shanghai", t) for w in ("day", "week", "month"))
    assert week <= day and month <= day and window_start("total") == 0
    import datetime as dt
    from zoneinfo import ZoneInfo

    z = ZoneInfo("Asia/Shanghai")
    assert dt.datetime.fromtimestamp(week / 1000, z).weekday() == 0
    assert dt.datetime.fromtimestamp(month / 1000, z).day == 1

    u = _team(app)
    admin, _ = u["admin"]
    a, a_id = u["a"]
    b, b_id = u["b"]
    shared = {
        "name": "shared",
        "type": "openai_compatible",
        "base_url": "https://a.example/v1",
        "api_key": "sk-admin-0001",
        "models": ["m"],
        "model_settings": {"m": {"price_in": "1", "price_out": "1"}},
    }
    assert admin.post("/api/config/providers", json=shared).status_code == 200
    # 新成员默认限额：没单独设过的成员沿用
    r = admin.put(
        "/api/config/member-defaults", json={"limits": [{"metric": "tokens", "window": "total", "limit": "1000000"}]}
    )
    assert r.json()["limits"][0]["window"] == "total"
    row = next(m for m in admin.get("/api/config/members").json()["members"] if m["user_id"] == a_id)
    assert row["limits"][0]["window"] == "total"

    g = a.post("/api/collab/groups", json={"name": "额度组", "members": [b_id]}).json()["group"]
    # 只有工作区管理员能给群组分配共享模型额度
    assert a.put(f"/api/collab/groups/{g['id']}/quota", json={"ws_limits": []}).status_code == 403
    r = admin.put(
        f"/api/collab/groups/{g['id']}/quota",
        json={"ws_limits": [{"metric": "tokens", "window": "day", "limit": "100"}]},
    )
    assert r.json()["group"]["ws_limits"][0]["window"] == "day"

    s = get_services()
    ws = a.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws, user_id=b_id, role="member"):
        pc = s.router.config.providers["shared"]
        assert s.model_hub.guard(pc, "m") is None
        s.model_hub.record(pc, "m", {"input_tokens": 80, "output_tokens": 30})
    # 乙用掉了全群额度：甲也被拦下
    with context.bind(ws, user_id=a_id, role="member"):
        msg = s.model_hub.guard(s.router.config.providers["shared"], "m")
        assert msg and "群组的共享模型额度" in msg

    # 群组模型：群主登记，只有群成员可见；群主给成员设限额
    gp = {**shared, "name": "grp-ds", "scope": "group", "group_id": g["id"], "api_key": "sk-group-0001"}
    assert b.post("/api/config/providers", json=gp).status_code == 403
    r = a.post("/api/config/providers", json=gp)
    assert r.status_code == 200 and r.json()["provider"]["group_id"] == g["id"]
    assert any(p["name"] == "grp-ds" for p in b.get("/api/config").json()["providers"])
    assert all(p["name"] != "grp-ds" for p in admin.get("/api/config").json()["providers"])
    a.put(
        f"/api/collab/groups/{g['id']}", json={"member_limits": [{"metric": "tokens", "window": "week", "limit": "50"}]}
    )
    with context.bind(ws, user_id=b_id, role="member"):
        pc = s.router.config.providers["grp-ds"]
        assert pc.api_key == "sk-group-0001" and pc.group_id == g["id"]
        s.model_hub.record(pc, "m", {"input_tokens": 40, "output_tokens": 20})
        assert "群主给你设定" in s.model_hub.guard(pc, "m")
    usage = a.get(f"/api/collab/groups/{g['id']}/usage").json()
    row = next(m for m in usage["members"] if m["user_id"] == b_id)
    assert row["group_tokens"] == 60 and row["member_limit_status"][0]["exceeded"]
    assert usage["ws_limit_status"][0]["exceeded"]


# ---------------------------------------------------------------- 部署管理员
def test_admin_console_ban_reset_announce_and_registration(app):
    u = _team(app)
    admin, admin_id = u["admin"]
    a, a_id = u["a"]
    # 普通成员（即使是工作区管理员）不能进运维控制台
    assert a.get("/api/admin/overview").status_code == 403
    ov = admin.get("/api/admin/overview").json()
    assert ov["users"]["total"] == 3
    assert ov["server"]["db"] in {"sqlite", "postgres"}
    assert "disk" in ov["server"]
    users = admin.get("/api/admin/users?q=example").json()["users"]
    assert {x["email"] for x in users} >= {"a@example.com", "b@example.com"}
    assert admin.get(f"/api/admin/users/{a_id}").json()["workspaces"][0]["role"] == "member"

    # 不能封禁自己 / 撤销最后一名部署管理员
    assert admin.post(f"/api/admin/users/{admin_id}/ban", json={"reason": "x"}).status_code == 400
    assert admin.post(f"/api/admin/users/{admin_id}/admin", json={"is_admin": False}).status_code == 400

    # 封禁：立即掉线，登录时看到原因
    assert admin.post(f"/api/admin/users/{a_id}/ban", json={"reason": "滥用模型额度"}).json()["user"]["banned_at"]
    assert a.get("/api/auth/me").status_code == 401
    r = TestClient(app).post("/api/auth/login", json={"email": "a@example.com", "password": "Member01!"})
    assert r.status_code == 403 and "滥用模型额度" in r.json()["message"]
    # 密码错时不透露封禁状态
    r = TestClient(app).post("/api/auth/login", json={"email": "a@example.com", "password": "Wrong0001!"})
    assert r.status_code == 401
    admin.post(f"/api/admin/users/{a_id}/unban")
    # 重置密码：临时密码可登录，旧密码失效
    pw = admin.post(f"/api/admin/users/{a_id}/reset-password").json()["temp_password"]
    c = TestClient(app)
    c.headers.update({"X-Requested-With": "pytest"})
    assert c.post("/api/auth/login", json={"email": "a@example.com", "password": "Member01!"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "a@example.com", "password": pw}).status_code == 200

    # 直接创建账号并加入工作区
    ws = admin.get("/api/auth/me").json()["workspace"]["id"]
    r = admin.post("/api/admin/users", json={"email": "new@example.com", "workspace_id": ws, "role": "reviewer"})
    assert r.status_code == 200 and r.json()["temp_password"]

    # 公告：发给全部账号，挂横幅
    n = admin.post("/api/admin/announce", json={"title": "周六凌晨维护", "body": "停机 30 分钟", "banner": True}).json()
    assert n["recipients"] >= 3
    assert c.get("/api/inbox").json()["items"][0]["kind"] == "announce"
    assert c.get("/api/inbox/summary").json()["banner"]["text"] == "周六凌晨维护"

    # 开放注册（覆盖环境变量）
    assert (
        TestClient(app).post("/api/auth/register", json={"email": "x@y.com", "password": "Abcdef12!"}).status_code
        == 403
    )
    admin.put("/api/admin/settings", json={"open_registration": True})
    assert (
        TestClient(app).post("/api/auth/register", json={"email": "x@y.com", "password": "Abcdef12!"}).status_code
        == 200
    )

    # 停用工作区 / 全站用量 / 审计
    other = admin.get("/api/admin/workspaces").json()["workspaces"]
    assert any(w["id"] == ws for w in other)
    assert admin.put(f"/api/admin/workspaces/{ws}/status", json={"status": "suspended"}).status_code == 400
    assert admin.get("/api/admin/usage?days=7").json()["days"] == 7
    actions = {x["action"] for x in admin.get("/api/admin/audit").json()["items"]}
    assert {"admin.user_ban", "admin.user_reset_password", "admin.announce"} <= actions
