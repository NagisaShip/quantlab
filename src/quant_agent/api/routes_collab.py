"""工作区协作：群组（讨论 / 任务摊派 / 群组模型与额度）、可见性、编辑人员标记与策略血缘、
消息通知中心与屏蔽、问题报告、文件上传。

权限都在服务端判定：
- 群组：公开群任何工作区成员可加入；私密群只有群主 / 群管理员能拉人；工作区管理员可管理全部群组；
- 可见性：作者或工作区管理员修改；列表与详情按可见性过滤；
- 问题报告：提交者本人、部署管理员、提交时所在工作区的管理员可见；后两者可以回复与改状态。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..platform import context
from ..platform.collab import ISSUE_CATEGORIES, ISSUE_STATUS, NOTIFY_KINDS, TASK_STATUS_LABEL
from ..platform.errors import AppError, ErrorCode
from ..store import Collections
from .deps import get_services

router = APIRouter(prefix="/api", tags=["协作与通知"])


def _c():
    return get_services().collab


def _me() -> tuple[str, str, str]:
    ctx = context.current()
    return context.current_workspace(), ctx.user_id or "", ctx.user_name or ctx.user_email or "成员"


def _ws_admin() -> bool:
    return context.current().role == "admin"


def _writer() -> None:
    if not context.current().can_write:
        raise AppError(ErrorCode.PERMISSION_DENIED, "审阅者只能查看，不能修改或发起")


# ---------------------------------------------------------------------------
# 工作区成员目录（选择器用）
# ---------------------------------------------------------------------------
@router.get("/collab/members", summary="工作区成员目录（选择群成员、指派任务、@ 提及、私聊用；q 按名字 / 用户名 / 邮箱搜索）")
def members(q: str = "") -> dict:
    ws, uid, _ = _me()
    rows = get_services().auth.members(ws)
    muted = _c().muted_users(uid)
    ql = q.strip().lower()
    online = presence_online(ws)
    return {
        "members": [
            {"user_id": r["user_id"], "name": r["name"] or r["username"] or "成员", "username": r["username"],
             "role": r["role"], "muted": r["user_id"] in muted, "online": r["user_id"] in online}
            for r in rows
            if r["status"] == "active"
            and (not ql or any(ql in str(r.get(k) or "").lower() for k in ("name", "username", "email")))
        ]
    }


# ---------------------------------------------------------------------------
# 私聊
# ---------------------------------------------------------------------------
class DmIn(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)


@router.post("/collab/dm", summary="打开与某位成员的私聊（没有就新建），返回私聊会话")
def open_dm(payload: DmIn) -> dict:
    _writer()
    ws, uid, _ = _me()
    gid = _c().open_dm(ws, uid, payload.user_id)
    return {"group": _c().get_group(ws, gid, uid)}


# ---------------------------------------------------------------------------
# 分享到群组 / 私聊 / 策略广场
# ---------------------------------------------------------------------------
class ShareToIn(BaseModel):
    target: Literal["group", "dm", "square"]
    target_id: str = Field("", max_length=64, description="群组 id 或私聊对象的 user_id；分享到广场时为空")
    ref: dict[str, Any] = Field(..., description="被分享的内容：{type, id, title}；type=link 时 id 是站内路由")
    note: str = Field("", max_length=4000, description="附言（Markdown）")
    title: str = Field("", max_length=120, description="分享到广场时的帖子标题，默认用内容标题")


@router.post("/collab/share-to", summary="把平台里的任意内容（回测、策略、报告、帖子、页面）分享到群组、私聊或策略广场")
def share_to(payload: ShareToIn) -> dict:
    _writer()
    ws, uid, name = _me()
    refs = _resolve_refs([payload.ref])
    if not refs:
        raise AppError(ErrorCode.NOT_FOUND, "要分享的内容不存在，或你没有权限查看")
    ref = refs[0]
    if payload.target == "square":
        from ..api.schemas import PostIn
        from .routes_community import publish

        title = (payload.title or ref.get("title") or "分享").strip()[:120]
        if len(title) < 2:
            title = f"分享：{title}"
        body = (payload.note.strip() + "\n\n" if payload.note.strip() else "") + f"{{{{ref:{ref['type']}:{ref['id']}}}}}"
        post = publish(PostIn(title=title, body=body, refs=[ref]), {"kind": "human", "label": "成员"})
        return {"ok": True, "target": "square", "link": f"post/{post['id']}"}
    gid = payload.target_id
    if payload.target == "dm":
        gid = _c().open_dm(ws, uid, payload.target_id)
    _c().require_group_member(gid, uid, ws=ws)
    msg = _c().post_message(gid, author_id=uid, author_name=name, body=payload.note.strip(), refs=[ref])
    return {"ok": True, "target": payload.target, "link": f"groups/{gid}", "message": msg}


# ---------------------------------------------------------------------------
# 在线状态：谁正在看 / 编辑某个对象（进程内，心跳 20 秒，45 秒没心跳算离开）
# ---------------------------------------------------------------------------
_PRESENCE: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
_PRESENCE_TTL = 45.0


def presence_online(ws: str) -> set[str]:
    """工作区里最近 45 秒有心跳的成员（成员目录里的绿点）。"""
    now = time.time()
    return {u for (w, _, _), who in _PRESENCE.items() if w == ws for u, v in who.items() if now - v["at"] < _PRESENCE_TTL}


class PresenceIn(BaseModel):
    object_type: str = Field(..., max_length=24)
    object_id: str = Field(..., max_length=160)
    editing: bool = False
    leave: bool = False


@router.post("/collab/presence", summary="在线心跳：我正在看 / 编辑这个对象；返回同时在场的其他成员")
def presence(payload: PresenceIn) -> dict:
    ws, uid, name = _me()
    key = (ws, payload.object_type, payload.object_id)
    now = time.time()
    who = _PRESENCE.setdefault(key, {})
    if payload.leave:
        who.pop(uid, None)
    else:
        who[uid] = {"name": name, "editing": payload.editing, "at": now}
    for k in [k for k, v in who.items() if now - v["at"] >= _PRESENCE_TTL]:
        who.pop(k, None)
    if not who:
        _PRESENCE.pop(key, None)
    return {
        "others": [
            {"user_id": k, "name": v["name"], "editing": v["editing"]} for k, v in who.items() if k != uid
        ]
    }


# ---------------------------------------------------------------------------
# 群组
# ---------------------------------------------------------------------------
class GroupIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    description: str = Field("", max_length=500)
    visibility: Literal["open", "private"] = "open"
    members: list[str] = Field(default_factory=list, max_length=200)


class GroupPatch(BaseModel):
    name: str | None = Field(None, max_length=40)
    description: str | None = Field(None, max_length=500)
    visibility: Literal["open", "private"] | None = None
    member_limits: list[dict[str, Any]] | None = Field(None, description="群主给每位成员设定的群组模型限额")


class GroupQuotaIn(BaseModel):
    ws_limits: list[dict[str, Any]] = Field(default_factory=list, description="工作区共享模型给本群的额度（全群合计）")


class MembersIn(BaseModel):
    user_ids: list[str] = Field(..., min_length=1, max_length=200)


class MemberRoleIn(BaseModel):
    role: Literal["owner", "admin", "member"]


@router.get("/collab/groups", summary="群组列表：我加入的与工作区内公开的群组")
def list_groups() -> dict:
    ws, uid, _ = _me()
    return {"groups": _c().list_groups(ws, uid, ws_admin=_ws_admin())}


@router.post("/collab/groups", summary="新建群组（任何成员都可以组建）")
def create_group(payload: GroupIn) -> dict:
    _writer()
    ws, uid, name = _me()
    gid = _c().create_group(
        ws, uid, name=payload.name, description=payload.description, visibility=payload.visibility,
    )
    if payload.members:
        _c().add_members(gid, payload.members, by=uid, by_name=name)
    get_services().audit.record("group.create", object_type="group", object_id=gid, details={"name": payload.name})
    return {"group": _c().get_group(ws, gid, uid, ws_admin=_ws_admin())}


@router.get("/collab/groups/{gid}", summary="群组详情与成员")
def get_group(gid: str) -> dict:
    ws, uid, _ = _me()
    return {"group": _c().get_group(ws, gid, uid, ws_admin=_ws_admin())}


@router.put("/collab/groups/{gid}", summary="修改群组名称、简介、可见性与群成员的群组模型限额（群主 / 群管理员）")
def update_group(gid: str, payload: GroupPatch) -> dict:
    ws, uid, _ = _me()
    _c().require_group_admin(gid, uid, ws_admin=_ws_admin())
    _c().update_group(gid, payload.model_dump(exclude_none=True))
    return {"group": _c().get_group(ws, gid, uid, ws_admin=_ws_admin())}


@router.put("/collab/groups/{gid}/quota", summary="给群组分配工作区共享模型额度（工作区管理员）")
def set_group_quota(gid: str, payload: GroupQuotaIn) -> dict:
    ws, uid, _ = _me()
    if not _ws_admin():
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以给群组分配共享模型额度")
    _c().get_group(ws, gid, uid, ws_admin=True)
    _c().update_group(gid, {"ws_limits": payload.ws_limits})
    get_services().audit.record("group.quota", object_type="group", object_id=gid, details=payload.model_dump())
    return {"group": _c().get_group(ws, gid, uid, ws_admin=True)}


@router.delete("/collab/groups/{gid}", summary="解散（归档）群组：群主或工作区管理员")
def archive_group(gid: str) -> dict:
    ws, uid, _ = _me()
    row = _c().require_group_admin(gid, uid, ws_admin=_ws_admin())
    if row["owner_id"] != uid and not _ws_admin():
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有群主或工作区管理员可以解散群组")
    _c().archive_group(gid)
    get_services().audit.record("group.archive", object_type="group", object_id=gid)
    return {"ok": True}


@router.post("/collab/groups/{gid}/join", summary="加入公开群组")
def join_group(gid: str) -> dict:
    _writer()
    ws, uid, name = _me()
    _c().get_group(ws, gid, uid, ws_admin=_ws_admin())
    _c().join(gid, uid, name)
    return {"group": _c().get_group(ws, gid, uid, ws_admin=_ws_admin())}


@router.post("/collab/groups/{gid}/members", summary="拉成员进群（群主 / 群管理员；公开群的成员也可以邀请）")
def add_members(gid: str, payload: MembersIn) -> dict:
    ws, uid, name = _me()
    row = _c().require_group_member(gid, uid, ws=ws) if not _ws_admin() else _c()._group_row(gid)
    if row["visibility"] == "private" and not _ws_admin():
        _c().require_group_admin(gid, uid)
    added = _c().add_members(gid, payload.user_ids, by=uid, by_name=name)
    return {"added": added, "group": _c().get_group(ws, gid, uid, ws_admin=_ws_admin())}


@router.delete("/collab/groups/{gid}/members/{user_id}", summary="移出群成员（群主 / 群管理员）或退出群组（本人）")
def remove_member(gid: str, user_id: str) -> dict:
    ws, uid, name = _me()
    if user_id != uid:
        _c().require_group_admin(gid, uid, ws_admin=_ws_admin())
    _c().remove_member(gid, user_id, by_name=name, left=user_id == uid)
    return {"ok": True}


@router.put("/collab/groups/{gid}/members/{user_id}", summary="设置群角色或转让群主")
def set_member_role(gid: str, user_id: str, payload: MemberRoleIn) -> dict:
    ws, uid, _ = _me()
    _c().require_group_admin(gid, uid, ws_admin=_ws_admin())
    _c().set_member_role(gid, user_id, payload.role, actor_id=uid)
    return {"group": _c().get_group(ws, gid, uid, ws_admin=_ws_admin())}


# ---------------------------------------------------------------------------
# 群聊
# ---------------------------------------------------------------------------
class MessageIn(BaseModel):
    body: str = Field("", max_length=20_000)
    refs: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    reply_to: int | None = None


class MessagePatch(BaseModel):
    body: str = Field(..., min_length=1, max_length=20_000)


class ReadIn(BaseModel):
    last_id: int


def _readable_group(gid: str) -> dict[str, Any]:
    ws, uid, _ = _me()
    row = _c()._group_row(gid)
    if row["workspace_id"] != ws:
        raise AppError(ErrorCode.NOT_FOUND, "群组不存在")
    if _c().group_role(gid, uid) is None and (
        '"dm"' in (row.get("settings") or "") or not (row["visibility"] == "open" or _ws_admin())
    ):
        raise AppError(ErrorCode.PERMISSION_DENIED, "你不在这个群组里")
    return row


@router.get("/collab/groups/{gid}/messages", summary="群聊消息（after 增量拉取，before 向上翻页）")
def list_messages(gid: str, after: int = 0, before: int | None = None, limit: int = Query(60, ge=1, le=200)) -> dict:
    _readable_group(gid)
    _, uid, _ = _me()
    msgs = _c().messages(gid, after_id=after, before_id=before, limit=limit)
    muted = _c().muted_users(uid)
    for m in msgs:
        m["muted"] = m["author_id"] in muted
    return {"messages": msgs, "pinned": _c().pinned_messages(gid) if not after else None, "server_time": time.time()}


@router.post("/collab/groups/{gid}/messages", summary="发群消息（Markdown + 引用回测 / 策略 / 代码 / 文件；@ 提及进通知中心）")
def post_message(gid: str, payload: MessageIn) -> dict:
    _writer()
    ws, uid, name = _me()
    _c().require_group_member(gid, uid, ws=ws)
    refs = _resolve_refs(payload.refs)
    return {"message": _c().post_message(gid, author_id=uid, author_name=name, body=payload.body, refs=refs,
                                         reply_to=payload.reply_to)}


@router.put("/collab/messages/{mid}", summary="编辑自己的消息")
def edit_message(mid: int, payload: MessagePatch) -> dict:
    _, uid, _ = _me()
    return {"message": _c().edit_message(mid, uid, payload.body)}


@router.delete("/collab/messages/{mid}", summary="撤回消息（本人；群主 / 群管理员可删除任何消息）")
def delete_message(mid: int) -> dict:
    _, uid, _ = _me()
    msg = _c().message(mid)
    mod = _ws_admin() or _c().group_role(msg["group_id"], uid) in ("owner", "admin")
    _c().delete_message(mid, uid, moderator=mod)
    return {"ok": True}


@router.post("/collab/messages/{mid}/pin", summary="置顶 / 取消置顶群消息（群主 / 群管理员）")
def pin_message(mid: int) -> dict:
    _, uid, _ = _me()
    msg = _c().message(mid)
    _c().require_group_admin(msg["group_id"], uid, ws_admin=_ws_admin())
    return {"pinned": _c().pin_message(mid)}


@router.post("/collab/groups/{gid}/read", summary="标记群消息已读到某条")
def mark_read(gid: str, payload: ReadIn) -> dict:
    _, uid, _ = _me()
    _c().mark_group_read(gid, uid, payload.last_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 任务摊派
# ---------------------------------------------------------------------------
class TaskIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=4000)
    assignee_id: str | None = None
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    due_at: int | None = Field(None, description="截止时间（毫秒）")
    refs: list[dict[str, Any]] = Field(default_factory=list, max_length=12)


class TaskPatch(BaseModel):
    title: str | None = Field(None, max_length=120)
    description: str | None = Field(None, max_length=4000)
    assignee_id: str | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    status: Literal["todo", "doing", "review", "done", "cancelled"] | None = None
    due_at: int | None = None
    refs: list[dict[str, Any]] | None = None


@router.get("/collab/groups/{gid}/tasks", summary="群组任务看板")
def group_tasks(gid: str) -> dict:
    _readable_group(gid)
    return {"tasks": _c().tasks(group_id=gid), "status_labels": TASK_STATUS_LABEL}


@router.post("/collab/groups/{gid}/tasks", summary="新建并指派任务（群成员）")
def create_task(gid: str, payload: TaskIn) -> dict:
    _writer()
    ws, uid, name = _me()
    _c().require_group_member(gid, uid, ws=ws)
    data = payload.model_dump()
    data["refs"] = _resolve_refs(payload.refs)
    return {"task": _c().create_task(gid, by=uid, by_name=name, **data)}


@router.put("/collab/tasks/{tid}", summary="修改任务：改状态、重新指派、调整优先级与截止时间")
def update_task(tid: str, payload: TaskPatch) -> dict:
    _writer()
    ws, uid, name = _me()
    task = _c().task(tid)
    if not _ws_admin():
        _c().require_group_member(task["group_id"], uid, ws=ws)
    mod = _ws_admin() or _c().group_role(task["group_id"], uid) in ("owner", "admin")
    patch = payload.model_dump(exclude_unset=True)
    if "refs" in patch and patch["refs"] is not None:
        patch["refs"] = _resolve_refs(patch["refs"])
    return {"task": _c().update_task(tid, patch, by=uid, by_name=name, moderator=mod)}


@router.delete("/collab/tasks/{tid}", summary="删除任务（创建者或群主 / 群管理员）")
def delete_task(tid: str) -> dict:
    _, uid, _ = _me()
    task = _c().task(tid)
    mod = _ws_admin() or _c().group_role(task["group_id"], uid) in ("owner", "admin")
    _c().delete_task(tid, by=uid, moderator=mod)
    return {"ok": True}


@router.get("/collab/tasks/mine", summary="指派给我的任务（跨群组）")
def my_tasks(include_closed: bool = False) -> dict:
    ws, uid, _ = _me()
    tasks = _c().tasks(assignee_id=uid, ws=ws, include_closed=include_closed)
    groups = {g["id"]: g["name"] for g in _c().list_groups(ws, uid, ws_admin=True)}
    for t in tasks:
        t["group_name"] = groups.get(t["group_id"], "")
    return {"tasks": tasks}


# ---------------------------------------------------------------------------
# 群组模型用量与共享内容
# ---------------------------------------------------------------------------
@router.get("/collab/groups/{gid}/usage", summary="群组模型用量与限额状态（群组服务商、全群共享模型额度、成员用量）")
def group_usage(gid: str, days: int = Query(30, ge=1, le=365)) -> dict:
    _readable_group(gid)
    ws, uid, _ = _me()
    s = get_services()
    hub = s.model_hub
    settings = _c().group_settings(gid)
    members = _c().group_member_ids(gid)
    is_mod = _ws_admin() or _c().group_role(gid, uid) in ("owner", "admin")
    since = int((time.time() - days * 86400) * 1000)
    names = _c().names(members)
    per_user = []
    for m in members if is_mod else [uid]:
        g_tokens, g_cost = hub._sum(ws, since, scope="group", group_id=gid, user_id=m)
        w_tokens, w_cost = hub._sum(ws, since, scope="workspace", user_id=m)
        per_user.append({
            "user_id": m, "name": names.get(m, "?"),
            "group_tokens": g_tokens, "group_cost": f"{g_cost / 1e6:.4f}",
            "ws_tokens": w_tokens, "ws_cost": f"{w_cost / 1e6:.4f}",
            "member_limit_status": hub.rule_status(ws, settings.get("member_limits") or [], scope="group",
                                                   group_id=gid, user_id=m),
        })
    providers = [
        {**hub.view(r), "limit_status": hub.rule_status(ws, r.get("limits") or [], provider=r["name"], scope="group",
                                                        group_id=gid)}
        for r in hub._records(ws)
        if r.get("scope") == "group" and r.get("group_id") == gid
    ]
    return {
        "providers": providers,
        "ws_limits": settings.get("ws_limits") or [],
        "ws_limit_status": hub.rule_status(ws, settings.get("ws_limits") or [], scope="workspace", user_ids=members),
        "member_limits": settings.get("member_limits") or [],
        "members": per_user,
        "can_manage": is_mod,
        "is_ws_admin": _ws_admin(),
    }


@router.get("/collab/groups/{gid}/shared", summary="在本群组内公开的策略与研究项目")
def group_shared(gid: str) -> dict:
    _readable_group(gid)
    ws, _, _ = _me()
    out: dict[str, list[dict[str, Any]]] = {"strategy": [], "project": []}
    for t in ("strategy", "project"):
        for oid, sh in _c().shares(ws, t).items():
            if sh["visibility"] == "group" and gid in sh["group_ids"]:
                out[t].append({"id": oid, "owner_id": sh["owner_id"], "updated_at": sh["updated_at"]})
    repo = _repo()
    names = _c().names({x["owner_id"] for v in out.values() for x in v})
    for x in out["strategy"]:
        rec = repo.get(x["id"])
        x["title"] = rec.meta.name if rec else x["id"]
        x["owner_name"] = names.get(x["owner_id"], "")
    for x in out["project"]:
        x["title"] = _project_title(x["id"])
        x["owner_name"] = names.get(x["owner_id"], "")
    return {"strategies": out["strategy"], "projects": out["project"]}


# ---------------------------------------------------------------------------
# 可见性
# ---------------------------------------------------------------------------
class ShareIn(BaseModel):
    visibility: Literal["private", "workspace", "group"]
    group_ids: list[str] = Field(default_factory=list, max_length=50)


@router.get("/collab/shares/{object_type}/{object_id}", summary="读取策略 / 研究项目的可见性与作者")
def get_share(object_type: Literal["strategy", "project"], object_id: str) -> dict:
    ws, uid, _ = _me()
    sh = _c().share(ws, object_type, object_id)
    owner = _c().owner_of(ws, object_type, object_id)
    return {
        "share": sh or {"visibility": "workspace", "group_ids": [], "owner_id": owner},
        "owner_name": _c().names([owner]).get(owner, "") if owner else "",
        "can_edit": _ws_admin() or (owner == uid) or (owner is None and _ws_admin()),
        "groups": [{"id": g["id"], "name": g["name"]} for g in _c().list_groups(ws, uid, ws_admin=_ws_admin())],
    }


@router.put("/collab/shares/{object_type}/{object_id}", summary="设置可见性：仅自己 / 工作区内公开 / 指定群组内公开")
def put_share(object_type: Literal["strategy", "project"], object_id: str, payload: ShareIn) -> dict:
    ws, uid, _ = _me()
    sh = _c().set_share(ws, object_type, object_id, user_id=uid, ws_admin=_ws_admin(),
                        visibility=payload.visibility, group_ids=payload.group_ids)
    get_services().audit.record("share.set", object_type=object_type, object_id=object_id, details=payload.model_dump())
    return {"share": sh}


def guard_visible(object_type: str, object_id: str) -> None:
    """详情接口用：作者没有向当前成员公开时拒绝访问。worker / 系统上下文不受限。"""
    ctx = context.current()
    if not ctx.workspace_id or not ctx.user_id or ctx.user_id == "system":
        return
    _c().assert_visible(ctx.workspace_id, object_type, object_id, ctx.user_id, ws_admin=ctx.role == "admin")


def filter_visible(object_type: str, items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """列表接口用：去掉没有向当前成员公开的条目。"""
    ctx = context.current()
    if not ctx.workspace_id or not ctx.user_id:
        return items
    hidden = _c().hidden_ids(ctx.workspace_id, object_type, ctx.user_id, ws_admin=ctx.role == "admin")
    return [i for i in items if i.get(key) not in hidden] if hidden else items


def claim_new(object_type: str, object_id: str) -> None:
    ctx = context.current()
    if ctx.workspace_id and ctx.user_id:
        _c().claim(ctx.workspace_id, object_type, object_id, ctx.user_id)


# ---------------------------------------------------------------------------
# 编辑人员标记与血缘
# ---------------------------------------------------------------------------
def _repo():
    from ..strategies.repo import StrategyRepository

    return StrategyRepository(get_services().store.root / "strategies")


def _project_title(pid: str) -> str:
    try:
        from .routes_lab import _lab

        p = _lab().store.get(pid)
        return (p or {}).get("title") or (p or {}).get("name") or pid
    except Exception:
        return pid


@router.get("/collab/edits/{object_type}/{object_id}", summary="编辑记录：谁（人 / Agent）在什么时候改出了哪个版本")
def edit_log(object_type: str, object_id: str) -> dict:
    ws, uid, _ = _me()
    if object_type in ("strategy", "project"):
        _c().assert_visible(ws, object_type, object_id, uid, ws_admin=_ws_admin())
    return {"edits": _c().edits(ws, object_type, object_id)}


@router.get("/collab/editors/{object_type}", summary="每个对象的编辑者（列表上的头像标记）")
def editors(object_type: str) -> dict:
    ws, uid, _ = _me()
    shares = _c().shares(ws, object_type) if object_type in ("strategy", "project") else {}
    names = _c().names({s["owner_id"] for s in shares.values()})
    return {
        "editors": _c().editors(ws, object_type),
        "visibility": {
            k: {"visibility": v["visibility"], "owner_id": v["owner_id"], "owner_name": names.get(v["owner_id"], ""),
                "groups": v["group_ids"]}
            for k, v in shares.items()
        },
    }


def _bt_brief(b: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    m = b.get("metrics") or {}
    return {
        "id": b["id"],
        "created_at": b.get("created_at"),
        "by": names.get(b.get("created_by") or "", ""),
        "status": b.get("status"),
        "data_sources": b.get("data_sources") or [],
        "metrics": {k: m.get(k) for k in ("total_return", "sharpe", "max_drawdown", "trades", "excess_return")},
    }


@router.get("/collab/lineage", summary="策略血缘树：版本链、克隆派生、各版本的编辑者与回测")
def lineage(slug: str | None = None) -> dict:
    ws, uid, _ = _me()
    store = get_services().store
    repo = _repo()
    hidden = _c().hidden_ids(ws, "strategy", uid, ws_admin=_ws_admin())
    strategies = {r.meta.slug: r for r in repo.list() if r.meta.slug not in hidden}
    edits = [e for e in _c().edits(ws, "strategy") if e["object_id"] not in hidden]
    backtests = [b for b in store.list(Collections.BACKTESTS) if b.get("slug") and b["slug"] not in hidden]
    names = _c().names({b.get("created_by") or "" for b in backtests})
    posts = {p["id"]: p for p in store.list(Collections.POSTS)}

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []

    def node(s: str, version: str, **kw: Any) -> dict[str, Any]:
        key = f"{s}@{version or '?'}"
        if key not in nodes:
            rec = strategies.get(s)
            nodes[key] = {
                "id": key, "slug": s, "version": version or "?", "name": rec.meta.name if rec else s,
                "exists": rec is not None, "current": bool(rec and rec.meta.version == version),
                "author": None, "editors": [], "at": None, "action": "", "note": "", "backtests": [],
                "status": rec.meta.status if rec and rec.meta.version == version else "",
            }
        nodes[key].update({k: v for k, v in kw.items() if v is not None})
        return nodes[key]

    def link(a: str, b: str, kind: str) -> None:
        if a != b and not any(e["from"] == a and e["to"] == b for e in edges):
            edges.append({"from": a, "to": b, "kind": kind})

    # 1. 没有编辑记录的老策略：用版本快照文件补一条链
    marked = {e["object_id"] for e in edits}
    for s, rec in strategies.items():
        if s in marked:
            continue
        chain = [v["version"] for v in reversed(repo.versions(s))] + [rec.meta.version]
        chain = [v for i, v in enumerate(chain) if v and (i == 0 or chain[i - 1] != v)]
        prev = None
        for v in chain:
            n = node(s, v, at=rec.meta.created_at if prev is None else None)
            if v == rec.meta.version:
                n["at"] = rec.meta.updated_at
                if rec.meta.author:
                    n["author"] = {"user_id": "", "name": rec.meta.author, "actor_kind": "agent" if rec.meta.source == "agent" else "human"}
            if prev:
                link(prev, n["id"], "edit")
            prev = n["id"]

    # 2. 编辑记录：每条记录对应一个版本节点
    for e in edits:
        s = e["object_id"]
        if e["action"] not in ("create", "edit", "clone"):
            continue
        n = node(s, e["version"], at=e["created_at"], action=e["action"], note=e["note"])
        who = {"user_id": e["user_id"], "name": e["user_name"], "actor_kind": e["actor_kind"]}
        if n["author"] is None:
            n["author"] = who
        if not any(x["user_id"] == who["user_id"] and x["actor_kind"] == who["actor_kind"] for x in n["editors"]):
            n["editors"].append(who)
        if e["parent_version"]:
            link(node(s, e["parent_version"])["id"], n["id"], "edit")
        src = e.get("derived_from") or ""
        if src.startswith("strategy:"):
            ps, _, pv = src[len("strategy:"):].partition("@")
            if ps not in hidden:
                link(node(ps, pv)["id"], n["id"], "clone")
        elif src.startswith("post:"):
            pid = src[len("post:"):]
            snap = (posts.get(pid) or {}).get("strategy") or {}
            if snap.get("slug") and snap["slug"] not in hidden:
                link(node(snap["slug"], snap.get("version") or "")["id"], n["id"], "clone")
            else:
                key = f"post:{pid}"
                nodes.setdefault(key, {
                    "id": key, "slug": "", "version": "", "name": (posts.get(pid) or {}).get("title") or "策略广场帖子",
                    "kind": "post", "exists": pid in posts, "current": False, "author": None, "editors": [],
                    "at": (posts.get(pid) or {}).get("created_at"), "action": "post", "note": "", "backtests": [],
                    "status": "",
                })
                link(key, n["id"], "clone")

    # 3. 回测挂到对应版本；版本对不上的挂到该策略最新的节点
    latest: dict[str, str] = {}
    for k, n in nodes.items():
        if n["slug"] and (n["slug"] not in latest or (n.get("at") or 0) >= (nodes[latest[n["slug"]]].get("at") or 0)):
            latest[n["slug"]] = k
    for b in sorted(backtests, key=lambda x: x.get("created_at") or 0, reverse=True):
        key = f"{b['slug']}@{b.get('version')}"
        target = nodes.get(key) or (nodes.get(latest[b["slug"]]) if b["slug"] in latest else None)
        if target is not None and len(target["backtests"]) < 6:
            target["backtests"].append(_bt_brief(b, names))

    # 4. 只看某个策略：取它所在的连通分量
    if slug:
        keep = {k for k, n in nodes.items() if n["slug"] == slug}
        changed = True
        while changed:
            changed = False
            for e in edges:
                if (e["from"] in keep) != (e["to"] in keep):
                    keep.update((e["from"], e["to"]))
                    changed = True
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [e for e in edges if e["from"] in keep and e["to"] in keep]
    for n in nodes.values():
        n.setdefault("kind", "version")
    return {"nodes": list(nodes.values()), "edges": edges}


# ---------------------------------------------------------------------------
# 引用（帖子、群聊、任务共用）
# ---------------------------------------------------------------------------
def _resolve_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """服务端补全引用的标题与摘要：回测取冻结指标，策略取当时的版本与代码哈希，文件核对归属。"""
    import hashlib

    from ..platform.collab import clean_refs

    store = get_services().store
    ws, uid, _ = _me()
    out = []
    for r in clean_refs(refs):
        t, rid = r["type"], r["id"]
        if t == "backtest":
            row = next((b for b in store.list(Collections.BACKTESTS) if b["id"] == rid), None)
            if row is None:
                continue
            m = row.get("metrics") or {}
            r["title"] = r["title"] or f"{row.get('strategy_name') or row.get('slug')} 回测"
            r["summary"] = {
                "strategy": row.get("strategy_name"), "slug": row.get("slug"), "version": row.get("version"),
                "total_return": m.get("total_return"), "sharpe": m.get("sharpe"), "max_drawdown": m.get("max_drawdown"),
                "trades": m.get("trades"), "excess_return": m.get("excess_return"),
                "timeframe": row.get("timeframe"), "sources": ",".join(row.get("data_sources") or []),
                "start_ts": row.get("start_ts"), "end_ts": row.get("end_ts"),
            }
        elif t == "strategy":
            rec = _repo().get(rid)
            if rec is None or rid in _c().hidden_ids(ws, "strategy", uid, ws_admin=_ws_admin()):
                continue
            r["title"] = r["title"] or rec.meta.name
            r["summary"] = {
                "version": rec.meta.version, "timeframe": rec.meta.timeframe,
                "securities": ",".join(rec.meta.securities or []), "status": rec.meta.status,
                "code_sha256": hashlib.sha256(rec.source.encode("utf-8")).hexdigest()[:12],
            }
        elif t == "file":
            up = _c().upload(rid)
            if up is None or not _can_read_upload(up):
                continue
            r.update({"title": up["filename"], "mime": up["mime"], "size": up["size"]})
        elif t == "post":
            p = store.get(Collections.POSTS, rid)
            if p is None:
                continue
            r["title"] = p["title"]
            r["summary"] = {"author": p.get("author"), "type": p.get("type")}
        elif t == "task":
            try:
                task = _c().task(rid)
            except AppError:
                continue
            r["title"] = task["title"]
            r["summary"] = {"status": task["status_label"], "assignee": task["assignee_name"]}
        elif t == "project":
            r["title"] = r["title"] or _project_title(rid)
        elif t == "report":
            # AI 研究室的调研报告：id = <对话 id>/<报告 id>
            pid, _, rep = rid.partition("/")
            if not pid or not rep:
                continue
            r["title"] = r["title"] or "调研报告"
            r["summary"] = {"project": _project_title(pid)}
        elif t == "link":
            # 站内任意页面：只接受站内路由，不接受外部网址
            import re as _re

            if not _re.fullmatch(r"[a-z][\w\-./:]{0,150}", rid) or "//" in rid:
                continue
            r["title"] = r["title"] or rid
        out.append(r)
    return out


@router.get("/collab/refs/search", summary="引用选择器：按类型搜索可引用的回测、策略、帖子、文件与任务")
def search_refs(type: Literal["backtest", "strategy", "post", "file", "task"], q: str = "") -> dict:
    ws, uid, _ = _me()
    store = get_services().store
    ql = q.strip().lower()
    items: list[dict[str, Any]] = []
    if type == "backtest":
        hidden = _c().hidden_ids(ws, "strategy", uid, ws_admin=_ws_admin())
        for b in sorted(store.list(Collections.BACKTESTS), key=lambda x: x.get("created_at") or 0, reverse=True):
            if b.get("slug") in hidden:
                continue
            title = f"{b.get('strategy_name') or b.get('slug')} · {b.get('version')}"
            if ql and ql not in title.lower() and ql not in b["id"].lower():
                continue
            m = b.get("metrics") or {}
            items.append({"type": "backtest", "id": b["id"], "title": title, "at": b.get("created_at"),
                          "hint": f"收益 {_pct(m.get('total_return'))} · 夏普 {_num(m.get('sharpe'))}"})
    elif type == "strategy":
        hidden = _c().hidden_ids(ws, "strategy", uid, ws_admin=_ws_admin())
        for r in _repo().list():
            if r.meta.slug in hidden or (ql and ql not in r.meta.name.lower() and ql not in r.meta.slug):
                continue
            items.append({"type": "strategy", "id": r.meta.slug, "title": r.meta.name, "at": r.meta.updated_at,
                          "hint": f"{r.meta.version} · {r.meta.timeframe}"})
    elif type == "post":
        for p in sorted(store.list(Collections.POSTS), key=lambda x: x.get("created_at") or 0, reverse=True):
            if ql and ql not in p["title"].lower():
                continue
            items.append({"type": "post", "id": p["id"], "title": p["title"], "at": p.get("created_at"),
                          "hint": p.get("author")})
    elif type == "file":
        rows = get_services().db.fetchall(
            "SELECT * FROM uploads WHERE user_id=? OR workspace_id=? ORDER BY created_at DESC LIMIT 100", (uid, ws)
        )
        for u in rows:
            if ql and ql not in u["filename"].lower():
                continue
            items.append({"type": "file", "id": u["id"], "title": u["filename"], "at": u["created_at"],
                          "hint": f"{u['mime']} · {u['size'] // 1024} KB", "mime": u["mime"], "size": u["size"]})
    elif type == "task":
        for t in _c().tasks(ws=ws):
            if ql and ql not in t["title"].lower():
                continue
            if _c().group_role(t["group_id"], uid) is None and not _ws_admin():
                continue
            items.append({"type": "task", "id": t["id"], "title": t["title"], "at": t["created_at"],
                          "hint": f"{t['status_label']} · {t['assignee_name'] or '未指派'}"})
    return {"items": items[:40]}


def _pct(v: Any) -> str:
    return f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "—"


def _num(v: Any) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "—"


# ---------------------------------------------------------------------------
# 通知中心
# ---------------------------------------------------------------------------
class InboxReadIn(BaseModel):
    ids: list[int] | None = Field(None, max_length=500)
    kind: str | None = None


class InboxClearIn(BaseModel):
    ids: list[int] | None = Field(None, max_length=500)
    read_only: bool = False


class MuteIn(BaseModel):
    target_type: Literal["user", "group", "kind", "post"]
    target_id: str = Field(..., min_length=1, max_length=120)
    label: str = Field("", max_length=80)


@router.get("/inbox", summary="消息通知中心：我的通知（可只看未读、按类型筛选、向前翻页）")
def inbox(unread: bool = False, kind: str | None = None, before: int | None = None,
          limit: int = Query(50, ge=1, le=200)) -> dict:
    uid = context.current().user_id or ""
    return {"items": _c().inbox(uid, unread=unread, kind=kind, before_id=before, limit=limit),
            "kinds": NOTIFY_KINDS, "unread": _c().unread(uid)}


@router.get("/inbox/summary", summary="顶栏角标：未读通知数、我的待办任务数、待处理问题报告（管理员）与全站公告")
def inbox_summary() -> dict:
    ctx = context.current()
    uid = ctx.user_id or ""
    c = _c()
    out: dict[str, Any] = {"unread": c.unread(uid), "banner": _banner()}
    if ctx.workspace_id:
        out["tasks"] = len(c.tasks(assignee_id=uid, ws=ctx.workspace_id, include_closed=False))
        out["group_unread"] = sum(g["unread"] for g in c.list_groups(ctx.workspace_id, uid) if g["my_role"])
    if ctx.is_admin:
        out["open_issues"] = c.issue_counts().get("open", 0)
    return out


def _banner() -> dict[str, Any] | None:
    from .routes_admin import platform_settings

    b = platform_settings().get("banner") or {}
    if not b.get("text") or (b.get("until") and b["until"] < time.time() * 1000):
        return None
    return b


@router.post("/inbox/read", summary="标记已读：指定 id、某一类型，或全部")
def inbox_read(payload: InboxReadIn) -> dict:
    uid = context.current().user_id or ""
    return {"updated": _c().mark_read(uid, payload.ids, payload.kind)}


@router.post("/inbox/clear", summary="删除通知：指定 id，或清空全部已读")
def inbox_clear(payload: InboxClearIn) -> dict:
    uid = context.current().user_id or ""
    return {"deleted": _c().delete_notifications(uid, payload.ids, payload.read_only)}


@router.get("/inbox/mutes", summary="我的屏蔽列表（人、群组、通知类型、帖子）")
def list_mutes() -> dict:
    return {"mutes": _c().mutes(context.current().user_id or ""), "kinds": NOTIFY_KINDS}


@router.post("/inbox/mutes", summary="屏蔽：不再收到某人 / 某群组 / 某类 / 某帖子的通知；屏蔽的人的群消息会折叠")
def add_mute(payload: MuteIn) -> dict:
    _c().mute(context.current().user_id or "", payload.target_type, payload.target_id, payload.label)
    return {"ok": True}


@router.delete("/inbox/mutes/{target_type}/{target_id}", summary="取消屏蔽")
def remove_mute(target_type: str, target_id: str) -> dict:
    return {"ok": _c().unmute(context.current().user_id or "", target_type, target_id)}


# ---------------------------------------------------------------------------
# 问题报告
# ---------------------------------------------------------------------------
class IssueIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=120)
    body: str = Field("", max_length=20_000)
    category: Literal["bug", "feature", "question", "data", "other"] = "bug"
    severity: Literal["low", "normal", "high", "critical"] = "normal"
    attachments: list[str] = Field(default_factory=list, max_length=10)
    context: dict[str, Any] = Field(default_factory=dict, description="页面、浏览器、界面语言等环境信息（自动附带）")


class IssueReplyIn(BaseModel):
    body: str = Field("", max_length=20_000)
    attachments: list[str] = Field(default_factory=list, max_length=10)


class IssueStatusIn(BaseModel):
    status: Literal["open", "replied", "resolved", "closed"]


def _admin_workspaces() -> list[str]:
    uid = context.current().user_id or ""
    return [m["id"] for m in get_services().auth.memberships(uid) if m["role"] == "admin"]


def _is_staff(issue: dict[str, Any]) -> bool:
    ctx = context.current()
    return bool(ctx.is_admin or (issue.get("workspace_id") and issue["workspace_id"] in _admin_workspaces()))


def _issue_or_403(iid: str) -> dict[str, Any]:
    row = _c().issue_row(iid)
    if row["user_id"] != context.current().user_id and not _is_staff(row):
        raise AppError(ErrorCode.NOT_FOUND, "问题报告不存在")
    return row


@router.get("/issues", summary="问题报告：成员看自己的；管理员看自己管理的工作区（部署管理员看全部）")
def list_issues(scope: Literal["mine", "all"] = "mine", status: str | None = None) -> dict:
    ctx = context.current()
    c = _c()
    if scope == "all" and (ctx.is_admin or _admin_workspaces()):
        items = c.issues(all_issues=ctx.is_admin, workspace_ids=_admin_workspaces(), user_id=ctx.user_id,
                         status=status)
    else:
        items = c.issues(user_id=ctx.user_id, status=status)
    return {
        "items": items,
        "categories": ISSUE_CATEGORIES,
        "statuses": ISSUE_STATUS,
        "is_staff": bool(ctx.is_admin or _admin_workspaces()),
        "counts": c.issue_counts() if ctx.is_admin else None,
    }


@router.post("/issues", summary="提交问题报告（可附截图与文件）")
def create_issue(payload: IssueIn) -> dict:
    ctx = context.current()
    c = _c()
    iid = c.create_issue(
        ws=ctx.workspace_id, user_id=ctx.user_id or "", title=payload.title, body=payload.body,
        category=payload.category, severity=payload.severity, attachments=payload.attachments,
        context_info=payload.context,
    )
    # 通知部署管理员与该工作区的管理员
    db = get_services().db
    staff = {r["id"] for r in db.fetchall("SELECT id FROM users WHERE is_admin=1 AND is_active=1")}
    if ctx.workspace_id:
        staff |= {
            r["user_id"]
            for r in db.fetchall(
                "SELECT user_id FROM memberships WHERE workspace_id=? AND role='admin' AND status='active'",
                (ctx.workspace_id,),
            )
        }
    c.notify(
        staff, kind="issue", title=f"新的问题报告：{payload.title}", body=payload.body[:200],
        link=f"issues/{iid}", actor_id=ctx.user_id, actor_name=ctx.user_name, source_type="issue", source_id=iid,
        workspace_id=ctx.workspace_id, force=True,
    )
    get_services().audit.record("issue.create", object_type="issue", object_id=iid,
                                details={"category": payload.category, "severity": payload.severity})
    return {"issue": c.issue(iid)}


@router.get("/issues/{iid}", summary="问题报告详情与回复")
def get_issue(iid: str) -> dict:
    row = _issue_or_403(iid)
    return {"issue": _c().issue(iid), "is_staff": _is_staff(row), "statuses": ISSUE_STATUS}


@router.post("/issues/{iid}/replies", summary="回复问题报告（提交者补充，或管理员答复）")
def reply_issue(iid: str, payload: IssueReplyIn) -> dict:
    row = _issue_or_403(iid)
    ctx = context.current()
    staff = _is_staff(row) and row["user_id"] != ctx.user_id
    _c().reply_issue(iid, user_id=ctx.user_id or "", user_name=ctx.user_name, body=payload.body, staff=staff,
                     attachments=payload.attachments)
    if not staff:
        db = get_services().db
        admins = {r["id"] for r in db.fetchall("SELECT id FROM users WHERE is_admin=1 AND is_active=1")}
        _c().notify(admins, kind="issue", title=f"问题报告有新补充：{row['title']}", body=payload.body[:200],
                    link=f"issues/{iid}", actor_id=ctx.user_id, actor_name=ctx.user_name, source_type="issue",
                    source_id=iid, workspace_id=row["workspace_id"], force=True)
    return {"issue": _c().issue(iid)}


@router.put("/issues/{iid}/status", summary="修改问题报告状态（管理员；提交者可以关闭自己的报告）")
def issue_status(iid: str, payload: IssueStatusIn) -> dict:
    row = _issue_or_403(iid)
    ctx = context.current()
    if not _is_staff(row) and not (row["user_id"] == ctx.user_id and payload.status in ("closed", "open")):
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有管理员可以修改状态（你可以关闭或重新打开自己的报告）")
    _c().set_issue_status(iid, payload.status, by=ctx.user_id or "", by_name=ctx.user_name)
    get_services().audit.record("issue.status", object_type="issue", object_id=iid, details={"status": payload.status})
    return {"issue": _c().issue(iid)}


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------
def _can_read_upload(up: dict[str, Any]) -> bool:
    ctx = context.current()
    if ctx.is_admin or up["user_id"] == ctx.user_id:
        return True
    if up.get("workspace_id"):
        ids = {m["id"] for m in get_services().auth.memberships(ctx.user_id or "")}
        if up["workspace_id"] in ids:
            return True
    # 问题报告的附件：该工作区管理员可读
    return False


@router.post("/uploads", summary="上传文件（原始字节，Content-Type 为文件类型，?filename= 文件名；单个 10 MB）")
async def upload(request: Request, filename: str = Query(..., max_length=200)) -> dict:
    data = await request.body()
    ctx = context.current()
    info = _c().save_upload(ws=ctx.workspace_id, user_id=ctx.user_id or "", filename=filename,
                            mime=request.headers.get("content-type", ""), data=data)
    return {"file": {**info, "url": f"/api/uploads/{info['id']}"}}


@router.get("/uploads/{fid}", summary="读取上传的文件（图片直接显示，其他类型下载）")
def read_upload(fid: str):
    up = _c().upload(fid)
    if up is None or not _can_read_upload(up):
        raise AppError(ErrorCode.NOT_FOUND, "文件不存在")
    path = _c().upload_file(fid)
    if not path.exists():
        raise AppError(ErrorCode.NOT_FOUND, "文件已被清理")
    inline = up["mime"].startswith("image/") or up["mime"] == "application/pdf"
    return FileResponse(
        path, media_type=up["mime"], filename=up["filename"],
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400",
                 "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"},
    )
