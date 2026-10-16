"""部署管理员（平台服务实际部署方）的运维控制台。

部署管理员 = 第一个注册的账号（users.is_admin），可以再授予其他账号。和「工作区管理员」不同：
工作区管理员只管自己的工作区；部署管理员管整台服务器——全部账号、全部工作区、全站用量与运行状态。

能做的事：
- 服务器总览：版本、运行时长、数据库与磁盘、worker 心跳、运行队列、近 24 小时 / 7 天 / 30 天模型用量；
- 账号：搜索、查看所在工作区与用量、封禁 / 解封（立即踢下线）、授予 / 撤销部署管理员、
  强制下线、重置密码（临时密码只显示一次）、直接创建账号并加入工作区；
- 工作区：列表、成员数与用量、停用 / 恢复；
- 全站模型用量：按天、按工作区、按账号；
- 公告：给全部账号（或某个工作区）发站内通知，顶栏横幅；
- 平台设置：是否开放注册；
- 全站审计日志。

所有操作写审计日志。
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..platform import context
from ..platform.auth import hash_password, validate_password
from ..platform.db import jload, now_ms
from ..platform.errors import AppError, ErrorCode
from ..store import Collections
from .deps import get_services

router = APIRouter(prefix="/api/admin", tags=["部署运维"])
STARTED_AT = time.time()
SETTINGS_ID = "platform_settings"


def _require_admin() -> None:
    if not context.current().is_admin:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有部署管理员可以使用运维控制台")


def platform_settings() -> dict[str, Any]:
    s = get_services()
    if s.global_store is None:
        return {}
    return s.global_store.get(Collections.SETTINGS, SETTINGS_ID) or {}


def _dir_size(path: Path, cap: int = 20000) -> int:
    """目录大小（字节）。文件很多时只数前 cap 个，避免拖慢接口。"""
    total, n = 0, 0
    stack = [path]
    while stack and n < cap:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    n += 1
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _usage_since(days: float, **where: Any) -> dict[str, Any]:
    db = get_services().db
    sql = (
        "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens, "
        "COALESCE(SUM(cost_micro), 0) AS cost FROM llm_usage WHERE created_at>=?"
    )
    args: list[Any] = [now_ms() - int(days * 86400_000)]
    for k, v in where.items():
        sql += f" AND {k}=?"
        args.append(v)
    row = db.fetchone(sql, tuple(args)) or {}
    return {"calls": int(row.get("calls") or 0), "tokens": int(row.get("tokens") or 0),
            "cost": f"{int(row.get('cost') or 0) / 1e6:.4f}"}


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------
@router.get("/overview", summary="服务器总览：版本、运行时长、数据库与磁盘、worker、运行队列、账号与模型用量")
def overview() -> dict:
    _require_admin()
    s = get_services()
    db = s.db
    from ..platform import watchdog

    home = Path(s.home)
    disk = shutil.disk_usage(home)
    db_size = None
    if db.dialect == "sqlite":
        try:
            db_size = sum(Path(db.path + suf).stat().st_size for suf in ("", "-wal", "-shm") if Path(db.path + suf).exists())
        except OSError:
            db_size = None
    users = db.fetchone(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active, "
        "SUM(CASE WHEN banned_at IS NOT NULL THEN 1 ELSE 0 END) AS banned, "
        "SUM(CASE WHEN is_admin=1 THEN 1 ELSE 0 END) AS admins FROM users"
    ) or {}
    active_7d = int(db.scalar("SELECT COUNT(*) AS n FROM users WHERE last_login_at>=?", (now_ms() - 7 * 86400_000,)) or 0)
    ws = db.fetchone(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active FROM workspaces"
    ) or {}
    runs = {r["state"]: int(r["n"]) for r in db.fetchall("SELECT state, COUNT(*) AS n FROM runs GROUP BY state")}
    try:
        h = watchdog.health(s)
        workers = {"alive": h["workers_alive"], "list": h["workers"], "stale_runs": len(h.get("stale_runs") or [])}
    except Exception as exc:  # 运维页不能因为某个子系统挂了打不开
        workers = {"alive": 0, "list": [], "error": str(exc)[:200]}
    sessions = int(db.scalar("SELECT COUNT(*) AS n FROM sessions WHERE revoked_at IS NULL AND expires_at>?", (now_ms(),)) or 0)
    folders = {}
    for name in ("workspaces", "results", "market", "uploads", "global"):
        p = home / name
        if p.exists():
            folders[name] = _dir_size(p)
    return {
        "server": {
            "python": sys.version.split()[0],
            "platform": platform.platform(terse=True),
            "host": platform.node(),
            "pid": os.getpid(),
            "uptime_s": int(time.time() - STARTED_AT),
            "data_dir": str(home),
            "db": db.dialect,
            "db_bytes": db_size,
            "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
            "folders": folders,
            "worker_mode": os.environ.get("QAGENT_WORKER", "process"),
            "open_registration": s.auth.open_registration(),
        },
        "users": {
            "total": int(users.get("total") or 0), "active": int(users.get("active") or 0),
            "banned": int(users.get("banned") or 0), "admins": int(users.get("admins") or 0),
            "active_7d": active_7d, "sessions": sessions,
        },
        "workspaces": {"total": int(ws.get("total") or 0), "active": int(ws.get("active") or 0)},
        "groups": int(db.scalar("SELECT COUNT(*) AS n FROM collab_groups WHERE archived_at IS NULL") or 0),
        "runs": runs,
        "workers": workers,
        "llm": {"24h": _usage_since(1), "7d": _usage_since(7), "30d": _usage_since(30)},
        "issues": s.collab.issue_counts(),
        "uploads_bytes": s.collab.storage_bytes(),
    }


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------
class BanIn(BaseModel):
    reason: str = Field("", max_length=200)


class AdminFlagIn(BaseModel):
    is_admin: bool


class CreateUserIn(BaseModel):
    email: str = Field(..., max_length=200)
    name: str = Field("", max_length=40)
    workspace_id: str | None = None
    role: Literal["admin", "member", "reviewer"] = "member"


def _user_row(uid: str) -> dict[str, Any]:
    row = get_services().db.fetchone("SELECT * FROM users WHERE id=?", (uid,))
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, "账号不存在")
    return row


def _user_view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"], "email": r["email"], "name": r["name"], "is_admin": bool(r["is_admin"]),
        "is_active": bool(r["is_active"]), "created_at": r["created_at"], "last_login_at": r.get("last_login_at"),
        "banned_at": r.get("banned_at"), "banned_reason": r.get("banned_reason") or "",
    }


@router.get("/users", summary="全部账号：搜索、按状态筛选，附所在工作区数与近 30 天模型费用")
def list_users(q: str = "", status: Literal["all", "active", "banned", "admin"] = "all",
               limit: int = Query(200, ge=1, le=1000)) -> dict:
    _require_admin()
    db = get_services().db
    sql = "SELECT * FROM users WHERE 1=1"
    args: list[Any] = []
    if q.strip():
        sql += " AND (LOWER(email) LIKE ? OR LOWER(name) LIKE ?)"
        args += [f"%{q.strip().lower()}%"] * 2
    if status == "active":
        sql += " AND is_active=1"
    elif status == "banned":
        sql += " AND banned_at IS NOT NULL"
    elif status == "admin":
        sql += " AND is_admin=1"
    rows = db.fetchall(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit))
    since = now_ms() - 30 * 86400_000
    cost = {r["user_id"]: r for r in db.fetchall(
        "SELECT user_id, COUNT(*) AS calls, SUM(input_tokens + output_tokens) AS tokens, SUM(cost_micro) AS cost "
        "FROM llm_usage WHERE created_at>=? GROUP BY user_id", (since,))}
    ws_n = {r["user_id"]: int(r["n"]) for r in db.fetchall(
        "SELECT user_id, COUNT(*) AS n FROM memberships WHERE status='active' GROUP BY user_id")}
    totp = {r["user_id"] for r in db.fetchall("SELECT user_id FROM user_totp WHERE enabled_at IS NOT NULL")}
    out = []
    for r in rows:
        u = _user_view(r)
        c = cost.get(r["id"]) or {}
        u.update({
            "workspaces": ws_n.get(r["id"], 0), "totp": r["id"] in totp,
            "usage_30d": {"calls": int(c.get("calls") or 0), "tokens": int(c.get("tokens") or 0),
                          "cost": f"{int(c.get('cost') or 0) / 1e6:.4f}"},
        })
        out.append(u)
    return {"users": out}


@router.get("/users/{uid}", summary="账号详情：所在工作区与角色、活跃会话、各工作区模型用量、最近操作")
def user_detail(uid: str) -> dict:
    _require_admin()
    s = get_services()
    db = s.db
    row = _user_row(uid)
    ws = db.fetchall(
        "SELECT w.id, w.name, w.status, m.role, m.status AS member_status, m.created_at FROM memberships m "
        "JOIN workspaces w ON w.id=m.workspace_id WHERE m.user_id=? ORDER BY m.created_at", (uid,))
    sessions = db.fetchall(
        "SELECT id, created_at, expires_at, ip, user_agent FROM sessions WHERE user_id=? AND revoked_at IS NULL "
        "AND expires_at>? ORDER BY created_at DESC LIMIT 20", (uid, now_ms()))
    usage = db.fetchall(
        "SELECT workspace_id, scope, COUNT(*) AS calls, SUM(input_tokens + output_tokens) AS tokens, "
        "SUM(cost_micro) AS cost FROM llm_usage WHERE user_id=? AND created_at>=? GROUP BY workspace_id, scope",
        (uid, now_ms() - 30 * 86400_000))
    audit = db.fetchall(
        "SELECT ts, workspace_id, action, object_type, object_id, ip FROM audit_log WHERE user_id=? "
        "ORDER BY id DESC LIMIT 30", (uid,))
    return {
        "user": _user_view(row),
        "workspaces": ws,
        "sessions": sessions,
        "usage_30d": [{**u, "cost": f"{int(u['cost'] or 0) / 1e6:.4f}", "tokens": int(u["tokens"] or 0)} for u in usage],
        "audit": audit,
        "groups": db.fetchall(
            "SELECT g.id, g.name, g.workspace_id, m.role FROM collab_group_members m JOIN collab_groups g "
            "ON g.id=m.group_id WHERE m.user_id=? AND g.archived_at IS NULL", (uid,)),
    }


def _guard_self(uid: str, what: str) -> None:
    if uid == context.current().user_id:
        raise AppError(ErrorCode.INVALID_PARAMETER, f"不能{what}自己")


def _last_admin_guard(uid: str) -> None:
    db = get_services().db
    row = _user_row(uid)
    if row["is_admin"]:
        n = int(db.scalar("SELECT COUNT(*) AS n FROM users WHERE is_admin=1 AND is_active=1") or 0)
        if n <= 1:
            raise AppError(ErrorCode.CONFLICT, "至少要保留一名可用的部署管理员")


@router.post("/users/{uid}/ban", summary="封禁账号：立即踢下线，之后无法登录（附原因，登录时提示）")
def ban_user(uid: str, payload: BanIn) -> dict:
    _require_admin()
    _guard_self(uid, "封禁")
    _last_admin_guard(uid)
    s = get_services()
    now = now_ms()
    with s.db.tx() as t:
        t.execute("UPDATE users SET is_active=0, banned_at=?, banned_reason=?, banned_by=? WHERE id=?",
                  (now, payload.reason.strip(), context.current().user_id, uid))
        t.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, uid))
    s.audit.record("admin.user_ban", object_type="user", object_id=uid, details={"reason": payload.reason})
    return {"user": _user_view(_user_row(uid))}


@router.post("/users/{uid}/unban", summary="解封账号")
def unban_user(uid: str) -> dict:
    _require_admin()
    s = get_services()
    s.db.execute("UPDATE users SET is_active=1, banned_at=NULL, banned_reason=NULL, banned_by=NULL WHERE id=?", (uid,))
    s.audit.record("admin.user_unban", object_type="user", object_id=uid)
    return {"user": _user_view(_user_row(uid))}


@router.post("/users/{uid}/admin", summary="授予 / 撤销部署管理员")
def set_admin(uid: str, payload: AdminFlagIn) -> dict:
    _require_admin()
    if not payload.is_admin:
        _guard_self(uid, "撤销")
        _last_admin_guard(uid)
    s = get_services()
    s.db.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if payload.is_admin else 0, uid))
    s.audit.record("admin.user_admin", object_type="user", object_id=uid, details={"is_admin": payload.is_admin})
    return {"user": _user_view(_user_row(uid))}


@router.post("/users/{uid}/sessions/revoke", summary="强制下线：作废该账号的全部会话")
def revoke_sessions(uid: str) -> dict:
    _require_admin()
    _guard_self(uid, "强制下线")
    s = get_services()
    n = s.db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_ms(), uid))
    s.audit.record("admin.user_logout", object_type="user", object_id=uid, details={"sessions": n})
    return {"revoked": n}


def _temp_password() -> str:
    # 保证满足口令规则：大小写字母 + 数字 + 符号
    return "Q" + secrets.token_urlsafe(9) + "a7!"


@router.post("/users/{uid}/reset-password", summary="重置密码：生成临时密码（只显示一次），并作废全部会话")
def reset_password(uid: str) -> dict:
    _require_admin()
    _user_row(uid)
    pw = _temp_password()
    validate_password(pw)
    s = get_services()
    with s.db.tx() as t:
        t.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(pw), uid))
        t.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now_ms(), uid))
    s.audit.record("admin.user_reset_password", object_type="user", object_id=uid)
    return {"temp_password": pw, "note": "临时密码只显示这一次，请通过安全渠道告知对方，并提醒登录后立即修改"}


@router.post("/users", summary="直接创建账号（临时密码只显示一次），可同时加入某个工作区")
def create_user(payload: CreateUserIn) -> dict:
    _require_admin()
    import uuid

    from ..platform.db import IntegrityError

    s = get_services()
    email = payload.email.strip().lower()
    if "@" not in email:
        raise AppError(ErrorCode.INVALID_PARAMETER, "邮箱格式不正确")
    pw = _temp_password()
    uid = uuid.uuid4().hex
    now = now_ms()
    name = payload.name.strip() or email.split("@")[0]
    with s.db.tx() as t:
        try:
            t.execute("INSERT INTO users (id, email, name, password_hash, is_admin, created_at) VALUES (?,?,?,?,?,?)",
                      (uid, email, name, hash_password(pw), 0, now))
        except IntegrityError as exc:
            raise AppError(ErrorCode.CONFLICT, "该邮箱已注册") from exc
        if payload.workspace_id:
            if not t.fetchone("SELECT id FROM workspaces WHERE id=?", (payload.workspace_id,)):
                raise AppError(ErrorCode.NOT_FOUND, "工作区不存在")
            t.execute("INSERT INTO memberships (workspace_id, user_id, role, created_at) VALUES (?,?,?,?)",
                      (payload.workspace_id, uid, payload.role, now))
    if not payload.workspace_id:
        s.auth.create_workspace(owner_id=uid, name=f"{name} 的工作区")
    s.audit.record("admin.user_create", object_type="user", object_id=uid,
                   details={"email": email, "workspace_id": payload.workspace_id, "role": payload.role})
    return {"user": _user_view(_user_row(uid)), "temp_password": pw}


# ---------------------------------------------------------------------------
# 工作区
# ---------------------------------------------------------------------------
@router.get("/workspaces", summary="全部工作区：所有者、成员数、群组数、近 30 天模型费用与运行数、状态")
def list_workspaces() -> dict:
    _require_admin()
    db = get_services().db
    rows = db.fetchall(
        "SELECT w.*, u.email AS owner_email, u.name AS owner_name FROM workspaces w "
        "LEFT JOIN users u ON u.id=w.owner_id ORDER BY w.created_at DESC")
    since = now_ms() - 30 * 86400_000
    members = {r["workspace_id"]: int(r["n"]) for r in db.fetchall(
        "SELECT workspace_id, COUNT(*) AS n FROM memberships WHERE status='active' GROUP BY workspace_id")}
    cost = {r["workspace_id"]: r for r in db.fetchall(
        "SELECT workspace_id, COUNT(*) AS calls, SUM(cost_micro) AS cost, SUM(input_tokens + output_tokens) AS tokens "
        "FROM llm_usage WHERE created_at>=? GROUP BY workspace_id", (since,))}
    runs = {r["workspace_id"]: int(r["n"]) for r in db.fetchall(
        "SELECT workspace_id, COUNT(*) AS n FROM runs WHERE created_at>=? GROUP BY workspace_id", (since,))}
    groups = {r["workspace_id"]: int(r["n"]) for r in db.fetchall(
        "SELECT workspace_id, COUNT(*) AS n FROM collab_groups WHERE archived_at IS NULL GROUP BY workspace_id")}
    out = []
    for r in rows:
        c = cost.get(r["id"]) or {}
        out.append({
            "id": r["id"], "name": r["name"], "status": r["status"], "is_demo": bool(r["is_demo"]),
            "created_at": r["created_at"], "owner_id": r["owner_id"], "owner_email": r.get("owner_email"),
            "owner_name": r.get("owner_name"), "members": members.get(r["id"], 0), "groups": groups.get(r["id"], 0),
            "runs_30d": runs.get(r["id"], 0),
            "usage_30d": {"calls": int(c.get("calls") or 0), "tokens": int(c.get("tokens") or 0),
                          "cost": f"{int(c.get('cost') or 0) / 1e6:.4f}"},
        })
    return {"workspaces": out}


class WsStatusIn(BaseModel):
    status: Literal["active", "suspended"]


@router.put("/workspaces/{wid}/status", summary="停用 / 恢复工作区（停用后成员无法进入，数据保留）")
def set_ws_status(wid: str, payload: WsStatusIn) -> dict:
    _require_admin()
    s = get_services()
    if payload.status == "suspended" and wid == context.current().workspace_id:
        raise AppError(ErrorCode.INVALID_PARAMETER, "不能停用你当前所在的工作区，先切换到其他工作区")
    n = s.db.execute("UPDATE workspaces SET status=? WHERE id=?", (payload.status, wid))
    if not n:
        raise AppError(ErrorCode.NOT_FOUND, "工作区不存在")
    s.audit.record("admin.workspace_status", object_type="workspace", object_id=wid, details={"status": payload.status})
    return {"ok": True}


# ---------------------------------------------------------------------------
# 全站模型用量
# ---------------------------------------------------------------------------
@router.get("/usage", summary="全站模型用量：按天、按工作区、按账号、按服务商")
def usage(days: int = Query(30, ge=1, le=365)) -> dict:
    _require_admin()
    db = get_services().db
    since = now_ms() - days * 86400_000
    rows = db.fetchall("SELECT created_at, input_tokens, output_tokens, cost_micro FROM llm_usage WHERE created_at>=?",
                       (since,))
    daily: dict[str, dict[str, int]] = {}
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.gmtime(int(r["created_at"]) / 1000))
        d = daily.setdefault(day, {"tokens": 0, "cost": 0, "calls": 0})
        d["tokens"] += int(r["input_tokens"] or 0) + int(r["output_tokens"] or 0)
        d["cost"] += int(r["cost_micro"] or 0)
        d["calls"] += 1
    series = []
    for i in range(days - 1, -1, -1):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400))
        d = daily.get(day, {"tokens": 0, "cost": 0, "calls": 0})
        series.append({"day": day, "tokens": d["tokens"], "calls": d["calls"], "cost": f"{d['cost'] / 1e6:.4f}"})

    def group(col: str) -> list[dict[str, Any]]:
        return [
            {**r, "tokens": int(r["tokens"] or 0), "cost": f"{int(r['cost'] or 0) / 1e6:.4f}"}
            for r in db.fetchall(
                f"SELECT {col}, COUNT(*) AS calls, SUM(input_tokens + output_tokens) AS tokens, SUM(cost_micro) AS cost "
                f"FROM llm_usage WHERE created_at>=? GROUP BY {col} ORDER BY cost DESC, tokens DESC LIMIT 50", (since,))
        ]

    by_user = group("user_id")
    names = get_services().collab.names({r["user_id"] for r in by_user})
    for r in by_user:
        r["name"] = names.get(r["user_id"], r["user_id"])
    by_ws = group("workspace_id")
    wn = {r["id"]: r["name"] for r in db.fetchall("SELECT id, name FROM workspaces")}
    for r in by_ws:
        r["name"] = wn.get(r["workspace_id"], r["workspace_id"])
    return {"days": days, "daily": series, "by_user": by_user, "by_workspace": by_ws,
            "by_provider": group("provider"), "by_scope": group("scope")}


# ---------------------------------------------------------------------------
# 公告与平台设置
# ---------------------------------------------------------------------------
class AnnounceIn(BaseModel):
    title: str = Field(..., min_length=2, max_length=120)
    body: str = Field("", max_length=2000)
    workspace_id: str | None = Field(None, description="只发给某个工作区的成员；不填发给全部账号")
    banner: bool = Field(False, description="同时显示为顶栏横幅")
    banner_hours: int = Field(72, ge=1, le=24 * 90)
    tone: Literal["info", "warn"] = "info"


class SettingsIn(BaseModel):
    open_registration: bool | None = Field(None, description="开放注册（不需要邀请码）；null 表示沿用环境变量")
    banner_text: str | None = Field(None, max_length=200, description="顶栏横幅；空字符串表示撤下")


@router.post("/announce", summary="发布公告：给全部账号或某个工作区发站内通知（不受屏蔽影响），可同时挂顶栏横幅")
def announce(payload: AnnounceIn) -> dict:
    _require_admin()
    s = get_services()
    ctx = context.current()
    if payload.workspace_id:
        ids = s.collab.ws_member_ids(payload.workspace_id)
    else:
        ids = [r["id"] for r in s.db.fetchall("SELECT id FROM users WHERE is_active=1")]
    n = s.collab.notify(ids + [ctx.user_id or ""], kind="announce", title=payload.title, body=payload.body,
                        actor_id=None, actor_name=ctx.user_name or "管理员", source_type="announce",
                        workspace_id=payload.workspace_id, force=True)
    if payload.banner:
        cur = platform_settings()
        cur.update({"id": SETTINGS_ID, "banner": {"text": payload.title, "body": payload.body, "tone": payload.tone,
                                                  "until": now_ms() + payload.banner_hours * 3600_000,
                                                  "at": now_ms()}})
        s.global_store.upsert(Collections.SETTINGS, cur)
    s.audit.record("admin.announce", object_type="announce", object_id="",
                   details={"title": payload.title, "recipients": n, "workspace_id": payload.workspace_id})
    return {"recipients": n}


@router.get("/settings", summary="平台设置：注册方式、顶栏横幅")
def get_settings() -> dict:
    _require_admin()
    st = platform_settings()
    return {
        "open_registration": get_services().auth.open_registration(),
        "open_registration_override": st.get("open_registration"),
        "env_open_registration": os.environ.get("QAGENT_OPEN_REGISTRATION", "") == "1",
        "banner": st.get("banner"),
    }


@router.put("/settings", summary="修改平台设置")
def put_settings(payload: SettingsIn) -> dict:
    _require_admin()
    s = get_services()
    cur = platform_settings()
    cur["id"] = SETTINGS_ID
    if "open_registration" in payload.model_fields_set:
        cur["open_registration"] = payload.open_registration
    if payload.banner_text is not None:
        cur["banner"] = (
            {"text": payload.banner_text, "tone": "info", "until": None, "at": now_ms()} if payload.banner_text else None
        )
    s.global_store.upsert(Collections.SETTINGS, cur)
    s.audit.record("admin.settings", object_type="platform", object_id="settings",
                   details=payload.model_dump(exclude_unset=True))
    return get_settings()


@router.get("/audit", summary="全站审计日志（可按操作、账号、工作区筛选）")
def audit(action: str | None = None, user_id: str | None = None, workspace_id: str | None = None,
          before_id: int | None = None, limit: int = Query(200, ge=1, le=1000)) -> dict:
    _require_admin()
    db = get_services().db
    sql = "SELECT * FROM audit_log WHERE 1=1"
    args: list[Any] = []
    if action:
        sql += " AND action LIKE ?"
        args.append(f"{action}%")
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    if workspace_id:
        sql += " AND workspace_id=?"
        args.append(workspace_id)
    if before_id:
        sql += " AND id<?"
        args.append(before_id)
    rows = db.fetchall(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
    names = get_services().collab.names({r["user_id"] or "" for r in rows})
    return {"items": [{**r, "details": jload(r["details"], {}), "user_name": names.get(r["user_id"] or "", "")}
                      for r in rows]}
