"""策略广场：对标聚宽社区（文章 / 问答 / 策略分享 / 精华 / 标签 / 克隆策略）。

与聚宽不同的两点，都是为了"可信"：
1. 策略分享附带的是**冻结快照**：代码哈希、参数、回测清单（数据来源、成本、成交模型）。
   读者看到的曲线与克隆到手的代码是同一份，不会被作者事后改动。
2. 可以请 AI 以"质疑者"身份审稿：检查交易次数、区间长度、成本假设、参数数量等过拟合信号，
   审稿意见作为一条带标记的评论公开挂在帖子下。
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..agents.copilot import StrategyCopilot
from ..platform import context
from ..platform.collab_bot import BOT_AUTHOR_ID, BOT_NAME, mentions_bot
from ..sdk import declared_params
from ..store import Collections
from ..strategies.repo import StrategyRepository
from .deps import get_services
from .schemas import CommentIn, FavoriteIn, PostIn, PostReportDecisionIn, PostReportIn, PostUpdateIn, ProfileIn, QmojiIn

router = APIRouter(prefix="/api/community", tags=["策略广场"])
log = logging.getLogger(__name__)

TYPE_LABELS = {"article": "文章", "question": "问答", "strategy": "策略分享", "notice": "公告"}
REPORT_REASONS = {
    "spam": "垃圾广告 / 引流",
    "abuse": "攻击谩骂 / 骚扰",
    "misinformation": "虚假或误导信息",
    "duplicate": "重复发布 / 抄袭",
    "other": "其他问题",
}
REPORT_STATUS_LABELS = {"open": "待处理", "resolved": "已处理", "dismissed": "已忽略"}
SUGGESTED_TAGS = [
    "趋势",
    "均值回归",
    "轮动",
    "网格",
    "定投",
    "多因子",
    "择时",
    "风控",
    "参数优化",
    "过拟合",
    "回测口径",
    "AI 生成",
    "入门",
    "研究",
    "心得",
    "问答",
]
SYSTEM_AUTHOR = "平台小助手"
AI_AUTHOR = "AI 审稿人"
BOT_AUTHOR = BOT_NAME


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _store():
    return get_services().store


def me() -> dict[str, Any]:
    """当前作者 = 服务端会话里的用户；资料与私信策略来自 social 服务。"""
    ctx = context.current()
    social = get_services().social
    prof = social.profile(ctx.workspace_id or "", ctx.user_id or "")
    account = (
        get_services().db.fetchone("SELECT username, email, quantlab_uid FROM users WHERE id=?", (ctx.user_id,)) or {}
    )
    return {
        "id": ctx.user_id,
        "name": ctx.user_name or "研究员",
        "username": account.get("username") or (account.get("email") or "").split("@")[0],
        "quantlab_uid": account.get("quantlab_uid") or "",
        **prof,
    }


def _is_moderator() -> bool:
    ctx = context.current()
    return ctx.role == "admin" or bool(ctx.is_admin)


def _owns(obj: dict[str, Any]) -> bool:
    return obj.get("author_id") == context.current().user_id


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = re.sub(r"\s+", " ", str(t)).strip()[:12]
        if t and t not in out:
            out.append(t)
    return out[:8]


def _engagement(post: dict[str, Any]) -> dict[str, float]:
    likes = float(len(post.get("likes", [])))
    dislikes = float(len(post.get("dislikes", [])))
    favorites = float(len(post.get("favorites", [])))
    comments = float(post.get("comment_count", 0) or 0)
    clones = float(post.get("clones", 0) or 0)
    views = float(post.get("views", 0) or 0)
    positive = likes + favorites * 1.4 + comments * 0.8 + clones * 2.0
    if post.get("accepted_comment"):
        positive += 1.0
    # 贝叶斯平滑：只有 1 个赞的新帖不会压过长期稳定高质量内容。
    quality = (positive + 2.0) / (positive + dislikes * 1.5 + 5.0)
    weighted = likes * 4 + favorites * 3 + comments * 2 + clones * 5 - dislikes * 4 + views * 0.05
    reach = math.log1p(max(0.0, weighted))
    return {"quality": quality, "reach": reach, "dislikes": dislikes}


def _tag_affinity(posts: list[dict[str, Any]], viewer: str) -> dict[str, float]:
    """用当前成员赞过 / 收藏过的帖子推断兴趣标签，作为推荐排序的轻量个性化信号。"""
    affinity: dict[str, float] = {}
    if not viewer:
        return affinity
    for p in posts:
        liked = viewer in p.get("likes", [])
        favorited = viewer in p.get("favorites", [])
        if not (liked or favorited):
            continue
        weight = 2.0 if liked else 1.2
        for tag in p.get("tags", []):
            affinity[str(tag)] = affinity.get(str(tag), 0.0) + weight
    return affinity


def _recommend_score(
    post: dict[str, Any],
    now: float,
    viewer: str,
    tag_affinity: dict[str, float],
    followed: set[str],
) -> float:
    """推荐分：质量置信度 + 互动广度 + 时效衰减 + 关注/兴趣偏置。"""
    eng = _engagement(post)
    hours = max(0.0, (now - float(post.get("created_at", now))) / 3600)
    score = (eng["quality"] * 2.4 + eng["reach"] + (1.3 if post.get("featured") else 0.0)) / math.pow(hours + 6, 1.15)
    if post.get("author_id") in followed:
        score *= 1.45
    tags = [str(t) for t in post.get("tags", [])]
    if tags and tag_affinity:
        overlap = sum(tag_affinity.get(t, 0.0) for t in tags) / max(1, len(tags))
        score *= 1.0 + min(0.55, overlap * 0.16)
    reply_at = float(post.get("last_reply_at") or 0)
    if reply_at > 0:
        reply_hours = max(0.0, (now - reply_at) / 3600)
        score += max(0.0, 1.2 - reply_hours / 24) / math.pow(hours + 6, 1.15)
    return score


def _hot_score(post: dict[str, Any]) -> float:
    eng = _engagement(post)
    return eng["reach"] * 2 + eng["quality"] * 3 - eng["dislikes"] * 0.8


def _diversify_by_author(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """推荐页限制同一作者在靠前位置过度集中，同时保留全部帖子。"""
    primary: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for post in rows:
        author = str(post.get("author_id") or post.get("author") or "")
        seen[author] = seen.get(author, 0) + 1
        (primary if post.get("pinned") or seen[author] <= 2 else deferred).append(post)
    return primary + deferred


def source_of(post: dict[str, Any]) -> dict[str, Any]:
    """帖子来源：人写的、Agent 发的、平台发的。旧帖子没有记录时按作者推断。"""
    src = post.get("source")
    if isinstance(src, dict) and src.get("kind"):
        return src
    if post.get("author_id") == "system":
        return {"kind": "system", "label": "平台"}
    return {"kind": "human", "label": "成员"}


def _qmoji_reactions(post: dict[str, Any], viewer: str) -> list[dict[str, Any]]:
    raw = post.get("reactions")
    if not isinstance(raw, dict):
        return []
    out: list[dict[str, Any]] = []
    for emoji, people in raw.items():
        if not isinstance(people, list):
            continue
        users = list(dict.fromkeys(str(u) for u in people if u))
        if users:
            out.append({"emoji": str(emoji), "count": len(users), "mine": viewer in users})
    return out


def _public(post: dict[str, Any], viewer: str) -> dict[str, Any]:
    out = {k: v for k, v in post.items() if k not in ("likes", "dislikes", "favorites", "reactions")}
    out["source"] = source_of(post)
    out.setdefault("refs", [])
    out["like_count"] = len(post.get("likes", []))
    out["dislike_count"] = len(post.get("dislikes", []))
    out["favorite_count"] = len(post.get("favorites", []))
    out["liked"] = viewer in post.get("likes", [])
    out["disliked"] = viewer in post.get("dislikes", [])
    out["favorited"] = viewer in post.get("favorites", [])
    out["reactions"] = _qmoji_reactions(post, viewer)
    out["type_label"] = TYPE_LABELS.get(post.get("type", ""), "")
    out["excerpt"] = _excerpt(post.get("body", ""))
    return out


def _excerpt(body: str, n: int = 140) -> str:
    text = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    text = re.sub(r"[#>*`_\-\[\]()!|]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


def _get_post(post_id: str) -> dict[str, Any]:
    post = _store().get(Collections.POSTS, post_id)
    if post is None:
        raise HTTPException(404, "帖子不存在")
    return post


def _ensure_not_blocked(post: dict[str, Any]) -> None:
    author_id = str(post.get("author_id") or "")
    viewer = context.current().user_id or ""
    if not author_id or not viewer or author_id == viewer:
        return
    ws = context.current_workspace()
    social = get_services().social
    if social.blocked(ws, viewer, author_id):
        raise HTTPException(403, "你已拉黑该作者")
    if social.blocked(ws, author_id, viewer):
        raise HTTPException(403, "对方已将你拉黑")


def _snapshot_backtest(bt: dict[str, Any]) -> dict[str, Any]:
    """回测快照：只保留展示与复核所需的字段，曲线降采样到 160 点。"""
    eq = bt.get("equity_curve") or []
    bm = bt.get("benchmark_curve") or []
    ts = bt.get("timestamps") or []
    step = max(1, math.ceil(len(eq) / 160))
    idx = list(range(0, len(eq), step))
    if eq and idx[-1] != len(eq) - 1:
        idx.append(len(eq) - 1)
    m = bt.get("metrics") or {}
    keys = (
        "total_return",
        "annualized_return",
        "max_drawdown",
        "sharpe",
        "sortino",
        "benchmark_return",
        "excess_return",
        "alpha",
        "beta",
        "win_rate",
        "trades",
        "information_ratio",
        "volatility",
        "trading_days",
        "daily_win_rate",
        "total_fees",
    )
    return {
        "id": bt.get("id"),
        "metrics": {k: m.get(k) for k in keys},
        "equity": [eq[i] for i in idx],
        "benchmark": [bm[i] for i in idx] if bm else [],
        "timestamps": [ts[i] for i in idx] if ts else [],
        "initial_cash": bt.get("initial_cash"),
        "securities": bt.get("securities"),
        "timeframe": bt.get("timeframe"),
        "params": bt.get("params"),
        "manifest": bt.get("manifest"),
        "data_sources": sorted({d.get("source_label") or d.get("source") for d in bt.get("datasets") or []}),
        "warnings": bt.get("warnings") or [],
    }


# ---------------------------------------------------------------------------
# 资料
# ---------------------------------------------------------------------------
@router.get("/profile")
def get_profile() -> dict:
    return {"profile": me()}


@router.put("/profile")
def put_profile(payload: ProfileIn) -> dict:
    name = payload.name.strip()
    if name in (SYSTEM_AUTHOR, AI_AUTHOR, BOT_AUTHOR):
        raise HTTPException(400, "这个名字保留给平台使用")
    ctx = context.current()
    get_services().db.execute("UPDATE users SET name=? WHERE id=?", (name, ctx.user_id))
    ctx.user_name = name
    get_services().social.save_profile(
        ctx.workspace_id or "",
        ctx.user_id or "",
        bio=payload.bio,
        dm_policy=payload.dm_policy,
        avatar_color=payload.avatar_color,
        public_profile=payload.public_profile,
    )
    # 兼容旧版本：同步写一份到原 JSON 资料，避免回滚后简介丢失。
    _store().upsert(Collections.PROFILE, {"id": ctx.user_id, "bio": payload.bio.strip()})
    return {"profile": me()}


def _profile_view(identifier: str) -> dict[str, Any]:
    ctx = context.current()
    ws = ctx.workspace_id or ""
    social = get_services().social
    target = social.user(ws, identifier)
    if target is None:
        raise HTTPException(404, "用户不存在")
    uid = str(target["id"])
    viewer = ctx.user_id or ""
    prof = social.profile(ws, uid)
    followers, following = social.follow_counts(ws, uid)
    store = _store()
    posts = [p for p in store.list(Collections.POSTS) if p.get("author_id") == uid]
    posts.sort(key=lambda p: p.get("created_at", 0), reverse=True)
    comments = [c for c in store.list(Collections.COMMENTS) if c.get("author_id") == uid]
    is_self = uid == viewer
    blocked = social.blocked(ws, viewer, uid)
    blocked_me = social.blocked(ws, uid, viewer)
    if blocked or blocked_me:
        posts = []
    cards = []
    for p in posts[:30]:
        item = _public(p, viewer)
        item.pop("body", None)
        item.pop("strategy", None)
        item.pop("backtest", None)
        cards.append(item)
    can_dm = not is_self and not blocked and not blocked_me and prof["dm_policy"] != "none"
    return {
        "user": {
            "id": uid,
            "username": target.get("username") or "",
            "name": target.get("name") or target.get("username") or "研究员",
            "quantlab_uid": target.get("quantlab_uid") or "",
            "bio": prof["bio"],
            "public_profile": prof.get("public_profile", False),
            "avatar_color": prof["avatar_color"],
            "role": target.get("role") or "member",
            "dm_policy": prof["dm_policy"],
            "dm_policy_label": prof["dm_policy_label"],
            "followers": followers,
            "following": following,
            "posts": len(posts),
            "likes": sum(len(p.get("likes", [])) for p in posts),
            "clones": sum(int(p.get("clones", 0)) for p in posts),
            "comments": len(comments),
        },
        "is_me": is_self,
        "is_following": social.is_following(ws, viewer, uid) if viewer else False,
        "is_blocked": blocked,
        "blocked_me": blocked_me,
        "can_dm": can_dm,
        "dm_reason": "对方已关闭私信"
        if prof["dm_policy"] == "none"
        else ("你们之间存在拉黑关系" if blocked or blocked_me else ""),
        "posts": cards,
    }


@router.get("/users/{identifier}", summary="用户主页：资料、关注/拉黑状态、私信策略与帖子")
def user_profile(identifier: str) -> dict:
    return _profile_view(identifier)


@router.post("/users/{identifier}/follow", summary="关注用户")
def follow_user(identifier: str) -> dict:
    ws, viewer, name = (
        context.current_workspace(),
        context.current().user_id or "",
        context.current().user_name or "成员",
    )
    target = get_services().social.user(ws, identifier)
    if target is None:
        raise HTTPException(404, "用户不存在")
    uid = str(target["id"])
    social = get_services().social
    if social.blocked(ws, viewer, uid) or social.blocked(ws, uid, viewer):
        raise HTTPException(403, "存在拉黑关系，无法关注")
    added = social.follow(ws, viewer, uid)
    if added:
        get_services().collab.notify(
            [uid],
            kind="follow",
            title=f"{name} 关注了你",
            body="",
            link=f"profile/{viewer}",
            actor_id=viewer,
            actor_name=name,
            source_type="user",
            source_id=viewer,
            workspace_id=ws,
        )
    followers, following = social.follow_counts(ws, uid)
    return {"following": True, "added": added, "followers": followers, "following_count": following}


@router.delete("/users/{identifier}/follow", summary="取消关注")
def unfollow_user(identifier: str) -> dict:
    ws, viewer = context.current_workspace(), context.current().user_id or ""
    target = get_services().social.user(ws, identifier)
    if target is None:
        raise HTTPException(404, "用户不存在")
    uid = str(target["id"])
    removed = get_services().social.unfollow(ws, viewer, uid)
    followers, following = get_services().social.follow_counts(ws, uid)
    return {"following": False, "removed": removed, "followers": followers, "following_count": following}


@router.post("/users/{identifier}/block", summary="拉黑用户：自动解除关注并隐藏其帖子")
def block_user(identifier: str) -> dict:
    ctx = context.current()
    ws, viewer = ctx.workspace_id or "", ctx.user_id or ""
    target = get_services().social.user(ws, identifier)
    if target is None:
        raise HTTPException(404, "用户不存在")
    uid = str(target["id"])
    if uid == viewer:
        raise HTTPException(400, "不能拉黑自己")
    if target.get("role") == "admin":
        raise HTTPException(400, "不能拉黑工作区管理员")
    added = get_services().social.block(ws, viewer, uid)
    get_services().collab.mute(viewer, "user", uid, target.get("name") or target.get("username") or "")
    return {"blocked": True, "added": added}


@router.delete("/users/{identifier}/block", summary="取消拉黑")
def unblock_user(identifier: str) -> dict:
    ws, viewer = context.current_workspace(), context.current().user_id or ""
    target = get_services().social.user(ws, identifier)
    if target is None:
        raise HTTPException(404, "用户不存在")
    uid = str(target["id"])
    removed = get_services().social.unblock(ws, viewer, uid)
    get_services().collab.unmute(viewer, "user", uid)
    return {"blocked": False, "removed": removed}


FAVORITE_TYPES = {
    "post": "帖子",
    "strategy": "策略",
    "backtest": "回测",
    "factor": "因子",
    "model": "模型",
    "project": "研究项目",
    "link": "链接",
}


def _favorite_target(payload: FavoriteIn) -> tuple[str, str, dict[str, Any]]:
    title, link, meta = payload.title.strip(), payload.link.strip(), payload.meta or {}
    if payload.object_type == "post":
        post = _get_post(payload.object_id)
        _ensure_not_blocked(post)
        title = title or post.get("title") or "帖子"
        link = link or f"post/{post['id']}"
        meta = {**meta, "author": post.get("author"), "type": post.get("type")}
    elif payload.object_type == "strategy":
        rec = StrategyRepository(_store().root / "strategies").get(payload.object_id)
        if rec is None:
            raise HTTPException(404, "策略不存在")
        title = title or rec.meta.name
        link = link or f"editor/{rec.meta.slug}"
        meta = {**meta, "version": rec.meta.version, "status": rec.meta.status}
    elif payload.object_type == "backtest":
        bt = _store().get_blob("backtests", payload.object_id)
        if bt is None:
            raise HTTPException(404, "回测不存在")
        title = title or f"{bt.get('strategy_name') or '策略'} 回测"
        link = link or f"backtest/{payload.object_id}"
        meta = {**meta, "metrics": bt.get("metrics") or {}}
    if not title:
        title = payload.object_id[:80]
    return title[:160], link[:300], meta


def _set_favorite_state(payload: FavoriteIn, on: bool) -> bool:
    ctx = context.current()
    ws, uid = ctx.workspace_id or "", ctx.user_id or ""
    social = get_services().social
    if on:
        title, link, meta = _favorite_target(payload)
        return social.set_favorite(
            ws,
            uid,
            object_type=payload.object_type,
            object_id=payload.object_id,
            title=title,
            link=link,
            meta=meta,
        )
    return social.remove_favorite(ws, uid, payload.object_type, payload.object_id)


@router.get("/favorites", summary="我的收藏：帖子、策略、回测等统一列表")
def list_favorites(type: str | None = None) -> dict:
    ctx = context.current()
    items = get_services().social.favorites(ctx.workspace_id or "", ctx.user_id or "", type or None)
    return {"items": items, "types": FAVORITE_TYPES}


@router.get("/favorites/{object_type}/{object_id}", summary="检查某个对象是否已收藏")
def favorite_status(object_type: str, object_id: str) -> dict:
    ctx = context.current()
    return {
        "favorite": get_services().social.is_favorite(ctx.workspace_id or "", ctx.user_id or "", object_type, object_id)
    }


@router.post("/favorites", summary="收藏内容；已收藏时再次调用会取消")
def toggle_favorite(payload: FavoriteIn) -> dict:
    ctx = context.current()
    social = get_services().social
    current = social.is_favorite(ctx.workspace_id or "", ctx.user_id or "", payload.object_type, payload.object_id)
    added = _set_favorite_state(payload, not current)
    return {"favorite": not current, "added": added}


@router.delete("/favorites/{object_type}/{object_id}", summary="取消收藏")
def delete_favorite(object_type: str, object_id: str) -> dict:
    ctx = context.current()
    removed = get_services().social.remove_favorite(ctx.workspace_id or "", ctx.user_id or "", object_type, object_id)
    return {"favorite": False, "removed": removed}


# ---------------------------------------------------------------------------
# 帖子
# ---------------------------------------------------------------------------
@router.get("/posts")
def list_posts(
    type: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    sort: str = Query("recommend", pattern="^(recommend|latest|reply|hot)$"),
    featured: bool = False,
    mine: bool = False,
    favorited: bool = False,
    following: bool = False,
    source: str | None = Query(None, description="human / agent / system：只看人写的、Agent 发的或平台发的"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
) -> dict:
    seed_if_empty()
    viewer = me()["id"]
    now = time.time()
    all_rows = _store().list(Collections.POSTS)
    followed = get_services().social.following_ids(context.current_workspace(), viewer)
    affinity = _tag_affinity(all_rows, viewer)
    rows = list(all_rows)
    if type:
        rows = [r for r in rows if r.get("type") == type]
    if tag:
        rows = [r for r in rows if tag in r.get("tags", [])]
    if featured:
        rows = [r for r in rows if r.get("featured")]
    if mine:
        rows = [r for r in rows if r.get("author_id") == viewer]
    if favorited:
        rows = [r for r in rows if viewer in r.get("favorites", [])]
    if following:
        rows = [r for r in rows if r.get("author_id") in followed]
    blocked = get_services().social.blocked_ids(context.current_workspace(), viewer) if viewer else set()
    if blocked and not mine:
        rows = [r for r in rows if r.get("author_id") not in blocked]
    if source in ("human", "agent", "system"):
        rows = [r for r in rows if source_of(r)["kind"] == source]
    muted = get_services().collab.muted_users(viewer) if viewer else set()
    if muted and not mine:
        rows = [r for r in rows if r.get("author_id") not in muted]
    if q:
        ql = q.lower()
        rows = [
            r
            for r in rows
            if ql in r.get("title", "").lower()
            or ql in r.get("body", "").lower()
            or any(ql in t.lower() for t in r.get("tags", []))
        ]
    if sort == "latest":
        rows.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    elif sort == "reply":
        rows.sort(key=lambda r: r.get("last_reply_at") or r.get("created_at", 0), reverse=True)
    elif sort == "hot":
        rows.sort(key=_hot_score, reverse=True)
    else:
        rows.sort(
            key=lambda r: _recommend_score(r, now, viewer, affinity, followed),
            reverse=True,
        )
        rows = _diversify_by_author(rows)
    pinned = [r for r in rows if r.get("pinned")]
    rows = pinned + [r for r in rows if not r.get("pinned")]
    total = len(rows)
    page_rows = rows[(page - 1) * size : page * size]
    items = []
    social = get_services().social
    ws = context.current_workspace()
    for r in page_rows:
        p = _public(r, viewer)
        author_id = str(p.get("author_id") or "")
        if author_id and author_id not in ("system",):
            author_prof = social.profile(ws, author_id)
            author_user = social.user(ws, author_id) or {}
            p["author_color"] = author_prof.get("avatar_color", "#2E5C9A")
            p["author_role"] = author_user.get("role") or "member"
        else:
            p["author_color"] = "#2E5C9A"
            p["author_role"] = "system"
        p.pop("body", None)
        if p.get("strategy"):
            p["strategy"] = {k: p["strategy"].get(k) for k in ("name", "securities", "timeframe", "version")}
        if p.get("backtest"):
            bt = p["backtest"]
            p["backtest"] = {
                "metrics": bt.get("metrics"),
                "equity": bt.get("equity", [])[::4],
                "data_sources": bt.get("data_sources"),
            }
        items.append(p)
    return {"items": items, "total": total, "page": page, "size": size, "viewer": me()["name"], "viewer_id": viewer}


@router.post("/posts")
def create_post(payload: PostIn) -> dict:
    return {"post": publish(payload, {"kind": "human", "label": "成员"})}


def publish(payload: PostIn, source: dict[str, Any]) -> dict[str, Any]:
    """发帖。source 标记来源：human（成员手写）/ agent（Agent 代发，附代谁发、来自哪个对话）。"""
    seed_if_empty()
    author = me()["name"]
    author_id = me()["id"]
    store = _store()
    from .routes_collab import _resolve_refs

    post: dict[str, Any] = {
        "type": payload.type,
        "title": payload.title.strip(),
        "body": payload.body,
        "tags": _clean_tags(payload.tags),
        "refs": _resolve_refs(payload.refs),
        "source": source,
        "author": author,
        "author_id": author_id,
        "likes": [],
        "dislikes": [],
        "favorites": [],
        "views": 0,
        "clones": 0,
        "comment_count": 0,
        "pinned": False,
        "featured": False,
        "last_reply_at": None,
        "accepted_comment": None,
    }
    if payload.strategy_slug:
        rec = StrategyRepository(store.root / "strategies").get(payload.strategy_slug)
        if rec is None:
            raise HTTPException(404, "要附加的策略不存在")
        post["strategy"] = {
            "slug": rec.meta.slug,
            "name": rec.meta.name,
            "code": rec.source,
            "code_sha256": hashlib.sha256(rec.source.encode("utf-8")).hexdigest(),
            "params": declared_params(rec.source),
            "securities": rec.meta.securities,
            "timeframe": rec.meta.timeframe,
            "version": rec.meta.version,
            "origin": rec.meta.source,
        }
        if payload.type == "article":
            post["type"] = "strategy"
    if payload.backtest_id:
        bt = store.get_blob("backtests", payload.backtest_id)
        if bt is None:
            raise HTTPException(404, "要附加的回测记录不存在")
        post["backtest"] = _snapshot_backtest(bt)
        if "strategy" not in post and bt.get("code"):
            post["strategy"] = {
                "slug": (bt.get("row") or {}).get("slug"),
                "name": bt.get("strategy_name"),
                "code": bt["code"],
                "code_sha256": hashlib.sha256(bt["code"].encode("utf-8")).hexdigest(),
                "params": bt.get("params") or {},
                "securities": bt.get("securities"),
                "timeframe": bt.get("timeframe"),
                "version": (bt.get("row") or {}).get("version"),
                "origin": "backtest",
            }
            post["type"] = "strategy" if payload.type == "article" else payload.type
    if post["type"] == "strategy" and "strategy" not in post:
        raise HTTPException(400, "策略分享需要附加一个策略或一次回测")
    saved = store.upsert(Collections.POSTS, post)
    if payload.ai_review and saved.get("strategy"):
        _ai_review(saved["id"])
        saved = _get_post(saved["id"])
    return _public(saved, author_id)


@router.get("/posts/{post_id}")
def read_post(post_id: str) -> dict:
    seed_if_empty()
    post = _get_post(post_id)
    store = _store()
    viewer = me()["id"]
    ws = context.current_workspace()
    social = get_services().social
    author_id = str(post.get("author_id") or "")
    blocked = bool(author_id and viewer and social.blocked(ws, viewer, author_id))
    blocked_me = bool(author_id and viewer and social.blocked(ws, author_id, viewer))
    if blocked:
        raise HTTPException(403, "你已拉黑该作者，先取消拉黑才能查看")
    if blocked_me:
        raise HTTPException(403, "对方已将你拉黑")
    post["views"] = post.get("views", 0) + 1
    store.upsert(Collections.POSTS, post)
    collab = get_services().collab
    muted = collab.muted_users(viewer)
    blocked_ids = social.blocked_ids(ws, viewer)
    comments = [
        c
        for c in store.list(Collections.COMMENTS)
        if c.get("post_id") == post_id and c.get("author_id") not in blocked_ids
    ]
    comments.sort(key=lambda c: c.get("created_at", 0))
    for c in comments:
        c["like_count"] = len(c.get("likes", []))
        c["liked"] = viewer in c.get("likes", [])
        c["muted"] = c.get("author_id") in muted
        cid = str(c.get("author_id") or "")
        if cid and cid != "system":
            cp = social.profile(ws, cid)
            cu = social.user(ws, cid) or {}
            c["author_color"] = cp.get("avatar_color", "#2E5C9A")
            c["author_role"] = cu.get("role") or "member"
        else:
            c["author_color"] = "#2E5C9A"
            c["author_role"] = "system"
        c.pop("likes", None)
    related = [
        _public(r, viewer)
        for r in store.list(Collections.POSTS)
        if r["id"] != post_id and set(r.get("tags", [])) & set(post.get("tags", []))
    ][:5]
    for r in related:
        r.pop("body", None)
        r.pop("strategy", None)
        r.pop("backtest", None)
    author_prof = social.profile(ws, author_id) if author_id else {"avatar_color": "#2E5C9A"}
    author_user = (social.user(ws, author_id) if author_id else None) or {}
    post_view = _public(post, viewer)
    post_view["author_color"] = author_prof.get("avatar_color", "#2E5C9A")
    post_view["author_role"] = author_user.get("role") or "system"
    relation = {
        "author_id": author_id,
        "author_role": author_user.get("role") or "system",
        "is_following": bool(author_id and social.is_following(ws, viewer, author_id)),
        "is_blocked": False,
        "can_dm": False,
        "dm_policy": "none",
        "dm_reason": "私聊不可用",
    }
    if author_id and author_id not in ("system", viewer):
        prof = social.profile(ws, author_id)
        relation["dm_policy"] = prof["dm_policy"]
        relation["can_dm"] = prof["dm_policy"] != "none"
        relation["dm_reason"] = "对方已关闭私信" if prof["dm_policy"] == "none" else ""
    return {
        "post": post_view,
        "comments": comments,
        "author_relation": relation,
        "related": related,
        "viewer": me()["name"],
        "viewer_id": viewer,
        "can_moderate": _is_moderator(),
        "post_muted": ("post", post_id) in collab.muted(viewer),
        "author_muted": post.get("author_id") in muted,
    }


@router.put("/posts/{post_id}")
def update_post(post_id: str, payload: PostUpdateIn) -> dict:
    post = _get_post(post_id)
    if not _owns(post):
        raise HTTPException(403, "只能编辑自己的帖子")
    if payload.title is not None:
        post["title"] = payload.title.strip()
    if payload.body is not None:
        post["body"] = payload.body
    if payload.tags is not None:
        post["tags"] = _clean_tags(payload.tags)
    if payload.refs is not None:
        from .routes_collab import _resolve_refs

        # 已有的引用保留发布时冻结的摘要，只给新加的补全
        old = {(r["type"], r["id"]): r for r in post.get("refs") or []}
        fresh = _resolve_refs([r for r in payload.refs if (r.get("type"), r.get("id")) not in old])
        keep = [old[(r.get("type"), r.get("id"))] for r in payload.refs if (r.get("type"), r.get("id")) in old]
        post["refs"] = keep + fresh
    post["edited_at"] = time.time()
    return {"post": _public(_store().upsert(Collections.POSTS, post), me()["id"])}


@router.delete("/posts/{post_id}")
def delete_post(post_id: str) -> dict:
    post = _get_post(post_id)
    ctx = context.current()
    moderator = _is_moderator()
    if not (_owns(post) or moderator):
        raise HTTPException(403, "只能删除自己的帖子（工作区管理员可删除任何帖子）")
    store = _store()
    now = int(time.time() * 1000)
    for c in store.list(Collections.COMMENTS):
        if c.get("post_id") == post_id:
            store.delete(Collections.COMMENTS, c["id"])
    store.delete(Collections.POSTS, post_id)
    get_services().db.execute(
        "UPDATE post_reports SET status='resolved', action_taken='post_deleted', resolution=?, "
        "reviewed_by=?, reviewed_at=?, updated_at=? "
        "WHERE workspace_id=? AND post_id=? AND status IN ('open')",
        ("帖子已删除", ctx.user_id, now, now, context.current_workspace(), post_id),
    )
    get_services().audit.record(
        "community.post_delete",
        object_type="post",
        object_id=post_id,
        details={"author_id": post.get("author_id"), "moderator": moderator and not _owns(post)},
    )
    return {"ok": True}


def _toggle(post_id: str, field: str) -> dict:
    post = _get_post(post_id)
    who = me()["id"]
    bucket = post.setdefault(field, [])
    on = who not in bucket
    if on:
        bucket.append(who)
    else:
        bucket.remove(who)
    _store().upsert(Collections.POSTS, post)
    return {"on": on, "count": len(bucket)}


def _toggle_reaction(post_id: str, target: str) -> dict:
    """点赞 / 点踩互斥；点踩只影响排序，不向作者发通知。"""
    post = _get_post(post_id)
    who = me()["id"]
    likes = list(dict.fromkeys(post.get("likes", [])))
    dislikes = list(dict.fromkeys(post.get("dislikes", [])))
    liked = who in likes
    disliked = who in dislikes
    if target == "like":
        if liked:
            likes.remove(who)
            liked = False
        else:
            likes.append(who)
            liked = True
            if who in dislikes:
                dislikes.remove(who)
                disliked = False
    else:
        if disliked:
            dislikes.remove(who)
            disliked = False
        else:
            dislikes.append(who)
            disliked = True
            if who in likes:
                likes.remove(who)
                liked = False
    post["likes"] = likes
    post["dislikes"] = dislikes
    _store().upsert(Collections.POSTS, post)
    if liked:
        _notify_author(post, kind="like", title=f"{me()['name']} 赞了你的帖子「{post['title']}」")
    return {
        "liked": liked,
        "disliked": disliked,
        "like_count": len(likes),
        "dislike_count": len(dislikes),
    }


def _notify_author(post: dict[str, Any], *, kind: str, title: str, body: str = "", to: str | None = None) -> None:
    ctx = context.current()
    target = to or post.get("author_id")
    if not target or target == "system":
        return
    get_services().collab.notify(
        [target],
        kind=kind,
        title=title,
        body=body[:200],
        link=f"post/{post['id']}",
        actor_id=ctx.user_id,
        actor_name=ctx.user_name,
        source_type="post",
        source_id=post["id"],
        workspace_id=ctx.workspace_id,
        post_id=post["id"],
    )


@router.post("/posts/{post_id}/like")
def like_post(post_id: str) -> dict:
    post = _get_post(post_id)
    _ensure_not_blocked(post)
    result = _toggle_reaction(post_id, "like")
    return {**result, "on": result["liked"], "count": result["like_count"]}


@router.post("/posts/{post_id}/dislike", summary="踩帖子；再次调用取消")
def dislike_post(post_id: str) -> dict:
    post = _get_post(post_id)
    _ensure_not_blocked(post)
    result = _toggle_reaction(post_id, "dislike")
    return {**result, "on": result["disliked"], "count": result["dislike_count"]}


@router.post("/posts/{post_id}/qmoji", summary="给帖子贴一个 Qmoji 反应；再次调用取消")
def qmoji_post(post_id: str, payload: QmojiIn) -> dict:
    post = _get_post(post_id)
    _ensure_not_blocked(post)
    who = me()["id"]
    raw = post.get("reactions") if isinstance(post.get("reactions"), dict) else {}
    existing = raw.get(payload.emoji)
    bucket = list(dict.fromkeys(str(u) for u in existing if u)) if isinstance(existing, list) else []
    on = who not in bucket
    if on:
        bucket.append(who)
    else:
        bucket.remove(who)
    if bucket:
        raw[payload.emoji] = bucket
    else:
        raw.pop(payload.emoji, None)
    post["reactions"] = raw
    _store().upsert(Collections.POSTS, post)
    if on and who != post.get("author_id"):
        _notify_author(post, kind="like", title=f"{me()['name']} 用 Qmoji 回应了你的帖子「{post['title']}」")
    reactions = _qmoji_reactions(post, who)
    count = next((r["count"] for r in reactions if r["emoji"] == payload.emoji), 0)
    return {"emoji": payload.emoji, "on": on, "count": count, "reactions": reactions}


@router.post("/posts/{post_id}/favorite")
def favorite_post(post_id: str) -> dict:
    post = _get_post(post_id)
    _ensure_not_blocked(post)
    result = _toggle(post_id, "favorites")
    _set_favorite_state(
        FavoriteIn(object_type="post", object_id=post_id, title=post.get("title") or "帖子", link=f"post/{post_id}"),
        result["on"],
    )
    return result


def _report_item(
    row: dict[str, Any], *, post: dict[str, Any] | None, reporter_name: str, same_post: int
) -> dict[str, Any]:
    return {
        **row,
        "reason_label": REPORT_REASONS.get(str(row.get("reason")), "其他问题"),
        "status_label": REPORT_STATUS_LABELS.get(str(row.get("status")), str(row.get("status"))),
        "reporter_name": reporter_name,
        "post_exists": post is not None,
        "post_title": post.get("title") if post else "帖子已删除",
        "post_author": post.get("author") if post else "—",
        "post_author_id": post.get("author_id") if post else None,
        "post_excerpt": _excerpt(post.get("body", ""), 120) if post else "帖子已删除，举报记录保留用于审计。",
        "same_post_reports": same_post,
    }


@router.post("/posts/{post_id}/report", summary="举报帖子")
def report_post(post_id: str, payload: PostReportIn) -> dict:
    post = _get_post(post_id)
    if _owns(post):
        raise HTTPException(400, "不能举报自己的帖子")
    _ensure_not_blocked(post)
    ctx = context.current()
    ws = context.current_workspace()
    db = get_services().db
    existing = db.fetchone(
        "SELECT * FROM post_reports WHERE workspace_id=? AND post_id=? AND reporter_id=?",
        (ws, post_id, ctx.user_id),
    )
    if existing:
        return {
            "report": _report_item(existing, post=post, reporter_name=ctx.user_name or "", same_post=1),
            "already_reported": True,
        }
    rid = uuid.uuid4().hex
    now = int(time.time() * 1000)
    db.execute(
        "INSERT INTO post_reports (id, workspace_id, post_id, reporter_id, reason, detail, status, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, ws, post_id, ctx.user_id, payload.reason, payload.detail.strip(), "open", now, now),
    )
    admins = [
        m["user_id"]
        for m in get_services().auth.members(ws)
        if m.get("role") == "admin" and m.get("user_id") != ctx.user_id
    ]
    if admins:
        get_services().collab.notify(
            admins,
            kind="system",
            title=f"新帖子举报：{post.get('title', '')[:40]}",
            body=f"{REPORT_REASONS.get(payload.reason, '其他问题')}：{payload.detail.strip()[:120]}",
            link="square",
            actor_id=ctx.user_id,
            actor_name=ctx.user_name,
            source_type="post",
            source_id=post_id,
            workspace_id=ws,
            post_id=post_id,
        )
    get_services().audit.record(
        "community.post_report",
        object_type="post",
        object_id=post_id,
        details={"report_id": rid, "reason": payload.reason},
    )
    row = db.fetchone("SELECT * FROM post_reports WHERE id=?", (rid,)) or {}
    return {
        "report": _report_item(row, post=post, reporter_name=ctx.user_name or "", same_post=1),
        "already_reported": False,
    }


def _report_counts(ws: str) -> dict[str, int]:
    rows = get_services().db.fetchall(
        "SELECT status, COUNT(*) AS n FROM post_reports WHERE workspace_id=? GROUP BY status", (ws,)
    )
    counts = {"open": 0, "resolved": 0, "dismissed": 0, "all": 0}
    for row in rows:
        n = int(row.get("n") or 0)
        counts[str(row.get("status"))] = n
        counts["all"] += n
    return counts


@router.get("/reports", summary="帖子举报列表（工作区管理员）")
def list_reports(status: str = Query("open", pattern="^(open|resolved|dismissed|all)$")) -> dict:
    if not _is_moderator():
        raise HTTPException(403, "只有工作区管理员可以审核举报")
    ws = context.current_workspace()
    db = get_services().db
    where = "workspace_id=?"
    args: list[Any] = [ws]
    if status != "all":
        where += " AND status=?"
        args.append(status)
    rows = db.fetchall(f"SELECT * FROM post_reports WHERE {where} ORDER BY created_at DESC LIMIT 300", tuple(args))
    reporter_ids = sorted({str(r.get("reporter_id")) for r in rows if r.get("reporter_id")})
    names: dict[str, str] = {}
    if reporter_ids:
        marks = ",".join("?" * len(reporter_ids))
        for user in db.fetchall(f"SELECT id, name FROM users WHERE id IN ({marks})", tuple(reporter_ids)):
            names[str(user["id"])] = user.get("name") or str(user["id"])
    same_counts: dict[str, int] = {}
    for agg in db.fetchall(
        "SELECT post_id, COUNT(*) AS n FROM post_reports WHERE workspace_id=? AND status='open' GROUP BY post_id",
        (ws,),
    ):
        same_counts[str(agg["post_id"])] = int(agg.get("n") or 0)
    items = []
    for row in rows:
        post = _store().get(Collections.POSTS, str(row.get("post_id")))
        items.append(
            _report_item(
                row,
                post=post,
                reporter_name=names.get(str(row.get("reporter_id")), str(row.get("reporter_id"))),
                same_post=same_counts.get(str(row.get("post_id")), 0),
            )
        )
    return {"items": items, "counts": _report_counts(ws), "status": status}


@router.post("/reports/{report_id}/decide", summary="处理或忽略帖子举报（工作区管理员）")
def decide_report(report_id: str, payload: PostReportDecisionIn) -> dict:
    if not _is_moderator():
        raise HTTPException(403, "只有工作区管理员可以审核举报")
    ws = context.current_workspace()
    db = get_services().db
    row = db.fetchone("SELECT * FROM post_reports WHERE id=? AND workspace_id=?", (report_id, ws))
    if row is None:
        raise HTTPException(404, "举报记录不存在")
    ctx = context.current()
    now = int(time.time() * 1000)
    db.execute(
        "UPDATE post_reports SET status=?, resolution=?, reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?",
        (payload.status, payload.note.strip(), ctx.user_id, now, now, report_id),
    )
    get_services().audit.record(
        "community.post_report_decide",
        object_type="post_report",
        object_id=report_id,
        details={"post_id": row.get("post_id"), "status": payload.status, "note": payload.note.strip()},
    )
    updated = db.fetchone("SELECT * FROM post_reports WHERE id=?", (report_id,)) or {}
    post = _store().get(Collections.POSTS, str(row.get("post_id")))
    return {"report": _report_item(updated, post=post, reporter_name="", same_post=0)}


@router.post("/posts/{post_id}/feature")
def feature_post(post_id: str) -> dict:
    """设为 / 取消精华（工作区管理员）。"""
    if not _is_moderator():
        raise HTTPException(403, "只有工作区管理员可以设置精华")
    post = _get_post(post_id)
    post["featured"] = not post.get("featured")
    _store().upsert(Collections.POSTS, post)
    if post["featured"]:
        _notify_author(post, kind="like", title=f"你的帖子「{post['title']}」被设为精华")
    return {"featured": post["featured"]}


@router.post("/posts/{post_id}/pin")
def pin_post(post_id: str) -> dict:
    if not _is_moderator():
        raise HTTPException(403, "只有工作区管理员可以置顶")
    post = _get_post(post_id)
    post["pinned"] = not post.get("pinned")
    _store().upsert(Collections.POSTS, post)
    return {"pinned": post["pinned"]}


@router.post("/posts/{post_id}/clone")
def clone_from_post(post_id: str) -> dict:
    """一键克隆帖子里的策略到"我的策略"。克隆的是帖子冻结的那份代码。"""
    post = _get_post(post_id)
    _ensure_not_blocked(post)
    snap = post.get("strategy")
    if not snap or not snap.get("code"):
        raise HTTPException(400, "这篇帖子没有附带策略代码")
    repo = StrategyRepository(_store().root / "strategies")
    name = f"{snap.get('name') or post['title']}（克隆）"[:60]
    sha = snap.get("code_sha256", "")[:12]
    rec = repo.save(
        name=name,
        source=snap["code"],
        params=declared_params(snap["code"]),
        team="社区克隆",
        kind="code",
        origin="community",
        symbol=(snap.get("securities") or ["BTC/USDT"])[0],
        timeframe=snap.get("timeframe") or "1h",
        securities=snap.get("securities") or ["BTC/USDT"],
        notes=f"克隆自策略广场「{post['title']}」（作者 {post.get('author')}，代码 {sha}）",
        slug=repo.unique_slug(name),
    )
    post["clones"] = post.get("clones", 0) + 1
    _store().upsert(Collections.POSTS, post)
    # 血缘：把保存钩子刚记下的那一笔改成「克隆」，并记下派生自哪里
    ctx = context.current()
    src = f"strategy:{snap['slug']}@{snap.get('version') or ''}" if snap.get("slug") else f"post:{post_id}"
    get_services().db.execute(
        "UPDATE collab_edits SET action='clone', derived_from=?, note=? WHERE id=(SELECT MAX(id) FROM collab_edits "
        "WHERE workspace_id=? AND object_type='strategy' AND object_id=?)",
        (src, f"克隆自策略广场「{post['title']}」"[:300], ctx.workspace_id, rec.meta.slug),
    )
    _notify_author(post, kind="like", title=f"{me()['name']} 克隆了你分享的策略「{post['title']}」")
    return {"strategy": rec.to_dict(include_source=False)}


# ---------------------------------------------------------------------------
# 评论
# ---------------------------------------------------------------------------
def _add_comment(
    post: dict[str, Any],
    body: str,
    *,
    author: str,
    author_id: str = "system",
    parent_id: str | None = None,
    is_ai: bool = False,
    meta: dict | None = None,
) -> dict[str, Any]:
    store = _store()
    if parent_id:
        parent = store.get(Collections.COMMENTS, parent_id)
        if parent is None or parent.get("post_id") != post["id"]:
            raise HTTPException(404, "要回复的评论不存在")
    comment = store.upsert(
        Collections.COMMENTS,
        {
            "post_id": post["id"],
            "parent_id": parent_id,
            "author": author,
            "author_id": author_id,
            "body": body,
            "likes": [],
            "is_ai": is_ai,
            "meta": meta or {},
        },
    )
    post["comment_count"] = post.get("comment_count", 0) + 1
    post["last_reply_at"] = time.time()
    store.upsert(Collections.POSTS, post)
    return comment


@router.post("/posts/{post_id}/comments")
def add_comment(post_id: str, payload: CommentIn) -> dict:
    post = _get_post(post_id)
    _ensure_not_blocked(post)
    body = payload.body.strip()
    parent = _store().get(Collections.COMMENTS, payload.parent_id) if payload.parent_id else None
    if payload.parent_id:
        if parent is None or parent.get("post_id") != post["id"]:
            raise HTTPException(404, "要回复的评论不存在")
        mention = f"@{parent.get('author') or '成员'}"
        if not body.startswith(mention):
            body = f"{mention} {body}"
    name = me()["name"]
    comment = _add_comment(post, body, author=name, author_id=me()["id"], parent_id=payload.parent_id)
    _notify_author(post, kind="comment", title=f"{name} 评论了你的帖子「{post['title']}」", body=body)
    if payload.parent_id and parent:
        if parent.get("author_id") and parent["author_id"] != post.get("author_id"):
            _notify_author(
                post,
                kind="comment",
                title=f"{name} 回复了你在「{post['title']}」下的评论",
                body=body,
                to=parent["author_id"],
            )
    bot_comment = None
    if mentions_bot(body):
        try:
            bot_comment = _square_bot_reply(
                post,
                trigger=comment,
                trigger_body=body,
                parent=parent,
                trigger_id=me()["id"],
            )
        except Exception:
            log.exception("community QuantLab Bot reply failed: post=%s", post["id"])
    return {"comment": comment, "bot_comment": bot_comment}


def _square_bot_reply(
    post: dict[str, Any],
    *,
    trigger: dict[str, Any],
    trigger_body: str,
    parent: dict[str, Any] | None,
    trigger_id: str,
) -> dict[str, Any]:
    """广场 Bot 回复：模型调用按触发 @ 的读者计费，回复挂在触发评论下。"""
    store = _store()
    recent = [
        c
        for c in store.list(Collections.COMMENTS)
        if c.get("post_id") == post["id"] and c.get("id") != trigger.get("id") and c.get("author") != BOT_AUTHOR
    ]
    recent.sort(key=lambda c: c.get("created_at") or 0)
    role = context.current().role or "member"
    reply = get_services().collab_bot.reply_square(
        get_services().router,
        workspace_id=context.current_workspace(),
        trigger_user_id=trigger_id,
        role=role,
        post=post,
        trigger_body=trigger_body,
        parent_author=str((parent or {}).get("author") or ""),
        recent=recent[-8:],
    )
    return _add_comment(
        post,
        reply["body"],
        author=BOT_AUTHOR,
        author_id=BOT_AUTHOR_ID,
        parent_id=trigger.get("id"),
        is_ai=True,
        meta={"mode": reply.get("mode") or "fallback", "model": reply.get("model") or "", "bot": True},
    )


@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: str) -> dict:
    store = _store()
    c = store.get(Collections.COMMENTS, comment_id)
    if c is None:
        raise HTTPException(404, "评论不存在")
    if not (
        _owns(c) or (c.get("author") in (AI_AUTHOR, BOT_AUTHOR) and _owns(_get_post(c["post_id"]))) or _is_moderator()
    ):
        raise HTTPException(403, "只能删除自己的评论")
    store.delete(Collections.COMMENTS, comment_id)
    post = store.get(Collections.POSTS, c["post_id"])
    if post:
        post["comment_count"] = max(0, post.get("comment_count", 1) - 1)
        store.upsert(Collections.POSTS, post)
    return {"ok": True}


@router.post("/comments/{comment_id}/like")
def like_comment(comment_id: str) -> dict:
    store = _store()
    c = store.get(Collections.COMMENTS, comment_id)
    if c is None:
        raise HTTPException(404, "评论不存在")
    who = me()["id"]
    likes = c.setdefault("likes", [])
    on = who not in likes
    likes.append(who) if on else likes.remove(who)
    store.upsert(Collections.COMMENTS, c)
    return {"on": on, "count": len(likes)}


@router.post("/posts/{post_id}/accept/{comment_id}")
def accept_answer(post_id: str, comment_id: str) -> dict:
    post = _get_post(post_id)
    if post.get("type") != "question":
        raise HTTPException(400, "只有问答帖可以采纳回答")
    if not _owns(post):
        raise HTTPException(403, "只有提问者可以采纳回答")
    post["accepted_comment"] = None if post.get("accepted_comment") == comment_id else comment_id
    _store().upsert(Collections.POSTS, post)
    return {"accepted_comment": post["accepted_comment"]}


def _ai_review(post_id: str) -> dict[str, Any]:
    post = _get_post(post_id)
    snap = post.get("strategy")
    if not snap:
        raise HTTPException(400, "没有附带策略的帖子无需代码审稿")
    bt_view = None
    if post.get("backtest"):
        b = post["backtest"]
        bt_view = {
            "metrics": b.get("metrics"),
            "datasets": [
                {"source": "synthetic" if "合成" in (s or "") else "exchange", "source_label": s}
                for s in b.get("data_sources", [])
            ],
            "cost_model": (b.get("manifest") or {}).get("cost_model"),
            "initial_cash": b.get("initial_cash"),
        }
    result = StrategyCopilot(get_services().router).review(snap["code"], bt_view, title=post["title"])
    return _add_comment(
        post, result.content, author=AI_AUTHOR, is_ai=True, meta={"mode": result.mode, "model": result.model}
    )


@router.post("/posts/{post_id}/ai-review")
def ai_review(post_id: str) -> dict:
    return {"comment": _ai_review(post_id)}


# ---------------------------------------------------------------------------
# 标签、榜单
# ---------------------------------------------------------------------------
@router.get("/tags")
def tags() -> dict:
    seed_if_empty()
    counts: dict[str, int] = {}
    for p in _store().list(Collections.POSTS):
        for t in p.get("tags", []):
            counts[t] = counts.get(t, 0) + 1
    hot = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return {"hot": [{"tag": t, "count": c} for t, c in hot[:24]], "suggested": SUGGESTED_TAGS}


@router.get("/stats")
def stats() -> dict:
    """贡献榜：发帖 ×5 + 获赞 ×2 + 被克隆 ×3 + 评论 ×1（AI 与平台账号不计入）。"""
    seed_if_empty()
    store = _store()
    board: dict[str, dict[str, int]] = {}

    def row(name: str) -> dict[str, int]:
        return board.setdefault(name, {"posts": 0, "likes": 0, "clones": 0, "comments": 0})

    posts = store.list(Collections.POSTS)
    for p in posts:
        r = row(p.get("author", "?"))
        r["posts"] += 1
        r["likes"] += len(p.get("likes", []))
        r["clones"] += p.get("clones", 0)
    comments = store.list(Collections.COMMENTS)
    for c in comments:
        row(c.get("author", "?"))["comments"] += 1
    ranking = [
        {"author": a, **v, "score": v["posts"] * 5 + v["likes"] * 2 + v["clones"] * 3 + v["comments"]}
        for a, v in board.items()
        if a not in (SYSTEM_AUTHOR, AI_AUTHOR, BOT_AUTHOR)
    ]
    ranking.sort(key=lambda r: r["score"], reverse=True)
    return {
        "ranking": ranking[:10],
        "counts": {
            "posts": len(posts),
            "strategies": sum(1 for p in posts if p.get("strategy")),
            "questions": sum(1 for p in posts if p.get("type") == "question"),
            "comments": len(comments),
            "clones": sum(p.get("clones", 0) for p in posts),
        },
    }


# ---------------------------------------------------------------------------
# 初始内容
# ---------------------------------------------------------------------------
GUIDE = """欢迎来到策略广场。这里的每一篇策略分享都附带**冻结的代码与回测清单**，你看到的曲线和克隆到手的代码是同一份。

## 怎么用
1. 在「策略研究」里写策略或让 AI 生成，跑一次完整回测；
2. 回测详情页点「分享到广场」，选择文章类型、写下思路与局限；
3. 读者可以一键**克隆**到自己的策略库，换参数、换区间复测；
4. 可以请「AI 审稿人」挑刺：交易次数够不够、成本假设是否现实、有没有过拟合迹象。

## 发帖建议
- 写清楚：标的、周期、区间、数据来源（真实 / 合成）、手续费与滑点假设；
- 同时给出**样本外**或实时模拟结果，比只给最好看的那条曲线更有说服力；
- 讨论收益之前先讨论回撤和交易次数。

> 所有内容均为研究讨论，不构成投资建议。
"""

CONVENTIONS = """平台所有回测与实时模拟共用一套口径，读别人的帖子前先确认这些前提：

| 项目 | 口径 |
| --- | --- |
| 决策时点 | 只用**已收盘** bar，绝不读取未来数据 |
| 成交价格 | 下一根 bar **开盘价** ±（半个价差 + 滑点） |
| 手续费 | 默认 taker 0.1%，买卖双向收取 |
| 成交量 | 单根 bar 成交不超过该 bar 成交量的 10% |
| 调仓容差 | 差额小于净值 0.5% 或 10 USDT 不成交 |
| 预热 | 前 N 根 bar 只用于指标预热，不计成绩 |
| 基准 | 同等资金买入持有标的池第一个币（不计成本） |
| 超额收益 | (1 + 策略收益) / (1 + 基准收益) − 1 |

**合成数据**只能用于检验代码与流程，不能作为策略结论；实时模拟 7 天 / 30 天检查点必须真实经过时间。
"""

OVERFIT = """参数优化最容易骗到的人是自己。平台的「参数优化」默认把区间切成**样本内 / 样本外**两段：

- 只用样本内挑参数；样本外只用来"验"；
- 看**秩相关**：样本内排名靠前的参数，样本外是否也靠前？相关接近 0 说明"好参数"可能只是噪声；
- 看**最优组合在样本外的排名**：样本内第一、样本外垫底，是最典型的过拟合；
- 尝试的组合越多，"最好那组"越可能是运气（多重检验）。

建议：参数取值用粗网格；优先选"一片区域都不错"的参数，而不是孤立的最高点；通过后再进实时模拟。
"""


_SEED_LOCK = threading.Lock()


_STABLE_SEED_IDS = {
    "策略广场新手指南": "seed-guide",
    "回测与实时模拟的统一口径（必读）": "seed-conventions",
    "参数优化如何不骗自己：样本内 / 样本外与秩相关": "seed-overfit",
    "模板分享：多币种动量轮动（趋势质量打分）": "seed-momentum-rotation",
}


def seed_if_empty() -> None:
    """首次访问时写入几篇置顶 / 精华帖。帖子用固定 id：并发的首个请求同时进来也只会写同一份，不会出现重复帖。"""
    store = get_services().store
    if store.get(Collections.SETTINGS, "community_seeded"):
        with _SEED_LOCK:
            # 旧版本的种子帖使用随机 id。新手引导会直接打开稳定 id
            # （例如 seed-momentum-rotation），所以这里做一次原地迁移。
            _migrate_seed_ids(store)
            if not store.get(Collections.SETTINGS, "community_dedup_v1"):
                _dedup_seed_posts(store)
        return
    with _SEED_LOCK:
        if store.get(Collections.SETTINGS, "community_seeded"):
            return
        _seed(store)
        _migrate_seed_ids(store)


def _migrate_seed_ids(store: Any) -> None:
    """把旧版随机 id 的系统帖改写成稳定 id，并同步评论引用。"""
    if store.get(Collections.SETTINGS, "community_stable_ids_v1"):
        return
    posts = store.list(Collections.POSTS)
    existing = {str(p.get("id") or "") for p in posts}
    for p in posts:
        if p.get("author_id") != "system":
            continue
        old_id = str(p.get("id") or "")
        stable_id = _STABLE_SEED_IDS.get(str(p.get("title") or ""))
        if not old_id or not stable_id or old_id == stable_id:
            continue
        if stable_id not in existing:
            # 先写稳定 id，再改评论引用，最后删旧帖，避免读者在迁移窗口内 404。
            store.upsert(Collections.POSTS, {**p, "id": stable_id})
            existing.add(stable_id)
            for comment in store.list(Collections.COMMENTS):
                if comment.get("post_id") == old_id:
                    store.upsert(Collections.COMMENTS, {**comment, "post_id": stable_id})
            store.delete(Collections.POSTS, old_id)
            existing.discard(old_id)
            continue
        # 稳定 id 已存在：旧帖如果没人互动就删掉；有互动则保留，交给人工处理。
        untouched = not (p.get("likes") or p.get("favorites") or p.get("comment_count") or p.get("clones"))
        if untouched:
            store.delete(Collections.POSTS, old_id)
            existing.discard(old_id)
    store.upsert(Collections.SETTINGS, {"id": "community_stable_ids_v1", "at": time.time()})


def _dedup_seed_posts(store: Any) -> None:
    """旧版本并发首访会把系统帖写两遍：同标题、平台作者、没人互动过的重复帖只留最早的一篇。"""
    seen: set[str] = set()
    for p in sorted(store.list(Collections.POSTS), key=lambda x: x.get("created_at", 0)):
        if p.get("author_id") != "system":
            continue
        untouched = not (p.get("likes") or p.get("favorites") or p.get("comment_count") or p.get("clones"))
        if p.get("title") in seen and untouched:
            store.delete(Collections.POSTS, p["id"])
        seen.add(p.get("title"))
    store.upsert(Collections.SETTINGS, {"id": "community_dedup_v1", "at": time.time()})


def _seed(store: Any) -> None:
    from ..strategies.sdk_templates import get_template

    base = {
        "likes": [],
        "favorites": [],
        "views": 0,
        "clones": 0,
        "comment_count": 0,
        "last_reply_at": None,
        "accepted_comment": None,
        "author": SYSTEM_AUTHOR,
        "author_id": "system",
    }
    store.upsert(
        Collections.POSTS,
        {
            **base,
            "id": "seed-guide",
            "type": "notice",
            "title": "策略广场新手指南",
            "body": GUIDE,
            "tags": ["入门"],
            "pinned": True,
            "featured": True,
        },
    )
    store.upsert(
        Collections.POSTS,
        {
            **base,
            "id": "seed-conventions",
            "type": "article",
            "title": "回测与实时模拟的统一口径（必读）",
            "body": CONVENTIONS,
            "tags": ["回测口径", "入门"],
            "pinned": False,
            "featured": True,
        },
    )
    title = "参数优化如何不骗自己：样本内 / 样本外与秩相关"
    store.upsert(
        Collections.POSTS,
        {
            **base,
            "id": "seed-overfit",
            "type": "article",
            "title": title,
            "body": OVERFIT,
            "tags": ["参数优化", "过拟合", "研究"],
            "pinned": False,
            "featured": True,
        },
    )
    tpl = get_template("momentum_rotation")
    if tpl:
        store.upsert(
            Collections.POSTS,
            {
                **base,
                "id": "seed-momentum-rotation",
                "type": "strategy",
                "title": "模板分享：多币种动量轮动（趋势质量打分）",
                "body": (
                    "用「区间涨幅 × 线性回归 R²」给每个币打分，只持有得分最高的一个；新候选得分需超过当前持仓 "
                    "1.2 倍才换仓，减少来回切换的成本；全部得分为负时空仓。\n\n"
                    "这只是一个起点：**没有附回测**，请克隆后在真实行情上自己验证，并试试不同的打分窗口。"
                ),
                "tags": ["轮动", "多标的", "入门"],
                "pinned": False,
                "featured": False,
                "strategy": {
                    "name": tpl.name,
                    "code": tpl.code,
                    "code_sha256": hashlib.sha256(tpl.code.encode("utf-8")).hexdigest(),
                    "params": declared_params(tpl.code),
                    "securities": tpl.securities,
                    "timeframe": tpl.timeframe,
                    "version": "template",
                    "origin": "template",
                },
            },
        )
    store.upsert(Collections.SETTINGS, {"id": "community_seeded", "at": time.time()})
