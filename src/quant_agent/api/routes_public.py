"""公开分享：生成、撤销与未登录受限预览。

分享 token 不暴露内部对象 ID；预览只返回公开展示所需的摘要，绝不返回群聊、任务、
邮箱、策略源码或完整回测明细。
"""

from __future__ import annotations

import secrets
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import context
from ..platform.db import now_ms
from ..platform.errors import AppError, ErrorCode
from ..store import Collections
from ..strategies.repo import StrategyRepository
from .deps import get_services

router = APIRouter(prefix="/api", tags=["公开分享"])

ShareType = Literal["group", "user", "post", "strategy"]
SHARE_TYPES = ("group", "user", "post", "strategy")


class PublicShareIn(BaseModel):
    object_type: ShareType
    object_id: str = Field(..., min_length=1, max_length=200)


def _db():
    return get_services().db


def _store(ws: str):
    return get_services().store.for_workspace(ws)


def _actor() -> tuple[str, str, bool]:
    ctx = context.current()
    return context.current_workspace(), ctx.user_id or "", ctx.role == "admin"


def _user_name(user_id: str | None) -> str:
    if not user_id:
        return "QuantLab 成员"
    row = _db().fetchone("SELECT name, username FROM users WHERE id=?", (user_id,))
    return (row.get("name") or row.get("username") or "QuantLab 成员") if row else "QuantLab 成员"


def _group_number(row: dict[str, Any]) -> str:
    return row.get("quantlab_no") or ""


def _check_object(ws: str, uid: str, object_type: str, object_id: str, *, ws_admin: bool) -> tuple[str, str]:
    """返回 (标题, 站内路由)，同时做创建分享的权限检查。"""
    if object_type == "group":
        row = _db().fetchone("SELECT * FROM collab_groups WHERE id=?", (object_id,))
        if not row or row.get("workspace_id") != ws or row.get("archived_at") or '"dm"' in (row.get("settings") or ""):
            raise HTTPException(404, "群组不存在")
        role = _db().fetchone("SELECT role FROM collab_group_members WHERE group_id=? AND user_id=?", (object_id, uid))
        if not role and not ws_admin and row.get("visibility") != "open":
            raise AppError(ErrorCode.PERMISSION_DENIED, "只有群成员可以分享这个群组")
        return row.get("name") or "QuantLab 群组", f"groups/{object_id}"
    if object_type == "user":
        target = get_services().social.user(ws, object_id)
        if not target:
            raise HTTPException(404, "用户不存在")
        if target.get("id") != uid and not ws_admin:
            profile = get_services().social.profile(ws, target.get("id") or "")
            if not profile.get("public_profile"):
                raise AppError(ErrorCode.PERMISSION_DENIED, "该用户尚未公开个人主页")
        return target.get("name") or target.get("username") or "QuantLab 用户", f"profile/{target.get('id')}"
    if object_type == "post":
        post = _store(ws).get(Collections.POSTS, object_id)
        if not post:
            raise HTTPException(404, "帖子不存在")
        if post.get("author_id") != uid and not ws_admin:
            # 广场帖子对工作区成员公开分享，但隐藏/草稿类内容不在这里出现。
            if post.get("visibility") in ("private", "draft") or post.get("deleted_at"):
                raise AppError(ErrorCode.PERMISSION_DENIED, "没有权限分享这篇帖子")
        return post.get("title") or "策略广场帖子", f"post/{object_id}"
    if object_type == "strategy":
        rec = StrategyRepository(_store(ws).root / "strategies").get(object_id)
        if not rec:
            raise HTTPException(404, "策略不存在")
        return rec.meta.name, f"editor/{object_id}"
    raise HTTPException(400, "不支持的分享类型")


def _preview(row: dict[str, Any]) -> dict[str, Any]:
    ws = row["workspace_id"]
    object_type = row["object_type"]
    object_id = row["object_id"]
    base = {
        "token": row["token"],
        "type": object_type,
        "title": row.get("title") or "",
        "route": row.get("route") or "",
        "created_at": row.get("created_at"),
    }
    if object_type == "group":
        g = _db().fetchone("SELECT * FROM collab_groups WHERE id=? AND archived_at IS NULL", (object_id,))
        if not g or g.get("workspace_id") != ws or '"dm"' in (g.get("settings") or ""):
            raise HTTPException(404, "分享内容已不可用")
        members = int(
            _db().scalar("SELECT COUNT(*) AS n FROM collab_group_members WHERE group_id=?", (object_id,)) or 0
        )
        tasks = int(
            _db().scalar(
                "SELECT COUNT(*) AS n FROM collab_tasks WHERE group_id=? AND status IN ('todo','doing','review')",
                (object_id,),
            )
            or 0
        )
        return {
            **base,
            "object": {
                "name": g.get("name") or "",
                "quantlab_no": _group_number(g),
                "description": g.get("description") or "",
                "member_count": members,
                "open_tasks": tasks,
                "visibility": g.get("visibility") or "open",
                "owner_name": _user_name(g.get("owner_id")),
                "created_at": g.get("created_at"),
            },
        }
    if object_type == "user":
        target = get_services().social.user(ws, object_id)
        if not target:
            raise HTTPException(404, "分享内容已不可用")
        prof = get_services().social.profile(ws, object_id)
        followers, following = get_services().social.follow_counts(ws, object_id)
        store = _store(ws)
        posts = [p for p in store.list(Collections.POSTS) if p.get("author_id") == object_id]
        return {
            **base,
            "object": {
                "name": target.get("name") or target.get("username") or "QuantLab 成员",
                "username": target.get("username") or "",
                "quantlab_uid": target.get("quantlab_uid") or "",
                "bio": prof.get("bio") or "",
                "avatar_color": prof.get("avatar_color") or "#2E5C9A",
                "followers": followers,
                "following": following,
                "posts": len(posts),
                "public_profile": bool(prof.get("public_profile")),
            },
        }
    if object_type == "post":
        post = _store(ws).get(Collections.POSTS, object_id)
        if not post:
            raise HTTPException(404, "分享内容已不可用")
        b = post.get("backtest") or {}
        metrics = b.get("metrics") or {}
        return {
            **base,
            "object": {
                "title": post.get("title") or "",
                "author": post.get("author") or "QuantLab 成员",
                "author_color": post.get("author_color") or "#2E5C9A",
                "type_label": post.get("type_label") or "文章",
                "body": (post.get("body") or "")[:1200],
                "excerpt": (post.get("excerpt") or "")[:520],
                "tags": (post.get("tags") or [])[:8],
                "likes": len(post.get("likes") or []),
                "comments": int(post.get("comment_count") or 0),
                "created_at": post.get("created_at"),
                "metrics": {
                    "total_return": metrics.get("total_return"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "sharpe": metrics.get("sharpe"),
                    "trades": metrics.get("trades"),
                }
                if metrics
                else {},
            },
        }
    if object_type == "strategy":
        rec = StrategyRepository(_store(ws).root / "strategies").get(object_id)
        if not rec:
            raise HTTPException(404, "分享内容已不可用")
        meta = rec.meta
        return {
            **base,
            "object": {
                "name": meta.name,
                "author": meta.author or "QuantLab 成员",
                "symbol": meta.symbol,
                "timeframe": meta.timeframe,
                "status": meta.status,
                "kind": meta.kind,
                "version": meta.version,
                "notes": meta.notes,
                "params": rec.params,
                "code_lines": rec.source.count("\n") + 1,
                "updated_at": meta.updated_at,
            },
        }
    raise HTTPException(404, "分享内容已不可用")


@router.post("/collab/public-shares", summary="为群组 / 用户 / 帖子 / 策略生成公开分享链接")
def create_public_share(payload: PublicShareIn) -> dict:
    ws, uid, ws_admin = _actor()
    if payload.object_type not in SHARE_TYPES:
        raise HTTPException(400, "不支持的分享类型")
    title, route = _check_object(ws, uid, payload.object_type, payload.object_id, ws_admin=ws_admin)
    existing = _db().fetchone(
        "SELECT * FROM public_shares WHERE workspace_id=? AND object_type=? AND object_id=? AND revoked_at IS NULL",
        (ws, payload.object_type, payload.object_id),
    )
    if existing:
        _db().execute("UPDATE public_shares SET title=?, route=? WHERE token=?", (title, route, existing["token"]))
        row = _db().fetchone("SELECT * FROM public_shares WHERE token=?", (existing["token"],))
    else:
        token = secrets.token_hex(12)
        _db().execute(
            "INSERT INTO public_shares (token, workspace_id, object_type, object_id, created_by, title, route, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (token, ws, payload.object_type, payload.object_id, uid, title, route, now_ms()),
        )
        row = _db().fetchone("SELECT * FROM public_shares WHERE token=?", (token,))
    get_services().audit.record(
        "public_share.create",
        object_type=payload.object_type,
        object_id=payload.object_id,
        details={"token": row["token"]},
    )
    return {"share": {"token": row["token"], "title": title, "route": route, "type": payload.object_type}}


@router.delete("/collab/public-shares/{token}", summary="撤销公开分享链接")
def revoke_public_share(token: str) -> dict:
    _ws, uid, ws_admin = _actor()
    row = _db().fetchone("SELECT * FROM public_shares WHERE token=?", (token,))
    if not row:
        raise HTTPException(404, "分享链接不存在")
    if row.get("created_by") != uid and not ws_admin:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有分享创建者或工作区管理员可以撤销")
    _db().execute("UPDATE public_shares SET revoked_at=? WHERE token=?", (now_ms(), token))
    get_services().audit.record(
        "public_share.revoke",
        object_type=row.get("object_type") or "",
        object_id=row.get("object_id") or "",
        details={"token": token},
    )
    return {"ok": True}


@router.get("/public/shares/{token}", summary="未登录可访问的公开分享预览")
def public_share(token: str) -> dict:
    row = _db().fetchone("SELECT * FROM public_shares WHERE token=? AND revoked_at IS NULL", (token,))
    if not row:
        raise HTTPException(404, "分享链接不存在或已撤销")
    return {"share": _preview(row), "brand": {"name": "QuantLab", "tagline": "让策略研究可复核、可协作"}}
