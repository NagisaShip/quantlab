"""策略广场：对标聚宽社区（文章 / 问答 / 策略分享 / 精华 / 标签 / 克隆策略）。

与聚宽不同的两点，都是为了"可信"：
1. 策略分享附带的是**冻结快照**：代码哈希、参数、回测清单（数据来源、成本、成交模型）。
   读者看到的曲线与克隆到手的代码是同一份，不会被作者事后改动。
2. 可以请 AI 以"质疑者"身份审稿：检查交易次数、区间长度、成本假设、参数数量等过拟合信号，
   审稿意见作为一条带标记的评论公开挂在帖子下。
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..agents.copilot import StrategyCopilot
from ..platform import context
from ..sdk import declared_params
from ..store import Collections
from ..strategies.repo import StrategyRepository
from .deps import get_services
from .schemas import CommentIn, PostIn, PostUpdateIn, ProfileIn

router = APIRouter(prefix="/api/community", tags=["策略广场"])

TYPE_LABELS = {"article": "文章", "question": "问答", "strategy": "策略分享", "notice": "公告"}
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


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _store():
    return get_services().store


def me() -> dict[str, Any]:
    """当前作者 = 服务端会话里的用户。显示名来自账号，简介按用户保存在工作区内。"""
    ctx = context.current()
    prof = _store().get(Collections.PROFILE, ctx.user_id or "anon") or {}
    return {"id": ctx.user_id, "name": ctx.user_name or "研究员", "bio": prof.get("bio", "")}


def _is_moderator() -> bool:
    return context.current().role == "admin"


def _owns(obj: dict[str, Any]) -> bool:
    return obj.get("author_id") == context.current().user_id


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        t = re.sub(r"\s+", " ", str(t)).strip()[:12]
        if t and t not in out:
            out.append(t)
    return out[:8]


def _score(post: dict[str, Any], now: float) -> float:
    """推荐分：互动加权 + 时间衰减（与常见社区排序一致）。置顶另行处理。"""
    interact = (
        len(post.get("likes", [])) * 3
        + post.get("comment_count", 0) * 2
        + post.get("clones", 0) * 4
        + len(post.get("favorites", [])) * 2
        + post.get("views", 0) * 0.05
    )
    if post.get("featured"):
        interact += 20
    hours = max(0.0, (now - post.get("created_at", now)) / 3600)
    return (interact + 1) / math.pow(hours + 2, 1.2)


def source_of(post: dict[str, Any]) -> dict[str, Any]:
    """帖子来源：人写的、Agent 发的、平台发的。旧帖子没有记录时按作者推断。"""
    src = post.get("source")
    if isinstance(src, dict) and src.get("kind"):
        return src
    if post.get("author_id") == "system":
        return {"kind": "system", "label": "平台"}
    return {"kind": "human", "label": "成员"}


def _public(post: dict[str, Any], viewer: str) -> dict[str, Any]:
    out = {k: v for k, v in post.items() if k not in ("likes", "favorites")}
    out["source"] = source_of(post)
    out.setdefault("refs", [])
    out["like_count"] = len(post.get("likes", []))
    out["favorite_count"] = len(post.get("favorites", []))
    out["liked"] = viewer in post.get("likes", [])
    out["favorited"] = viewer in post.get("favorites", [])
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
    if name in (SYSTEM_AUTHOR, AI_AUTHOR):
        raise HTTPException(400, "这个名字保留给平台使用")
    ctx = context.current()
    get_services().db.execute("UPDATE users SET name=? WHERE id=?", (name, ctx.user_id))
    ctx.user_name = name
    _store().upsert(Collections.PROFILE, {"id": ctx.user_id, "bio": payload.bio.strip()})
    return {"profile": me()}


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
    source: str | None = Query(None, description="human / agent / system：只看人写的、Agent 发的或平台发的"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=50),
) -> dict:
    seed_if_empty()
    viewer = me()["id"]
    now = time.time()
    rows = _store().list(Collections.POSTS)
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
        rows.sort(
            key=lambda r: (
                len(r.get("likes", [])) * 3
                + r.get("comment_count", 0) * 2
                + r.get("clones", 0) * 4
                + r.get("views", 0) * 0.05
            ),
            reverse=True,
        )
    else:
        rows.sort(key=lambda r: _score(r, now), reverse=True)
    pinned = [r for r in rows if r.get("pinned")]
    rows = pinned + [r for r in rows if not r.get("pinned")]
    total = len(rows)
    page_rows = rows[(page - 1) * size : page * size]
    items = []
    for r in page_rows:
        p = _public(r, viewer)
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
    post = _get_post(post_id)
    post["views"] = post.get("views", 0) + 1
    store = _store()
    store.upsert(Collections.POSTS, post)
    viewer = me()["id"]
    collab = get_services().collab
    muted = collab.muted_users(viewer)
    comments = [c for c in store.list(Collections.COMMENTS) if c.get("post_id") == post_id]
    comments.sort(key=lambda c: c.get("created_at", 0))
    for c in comments:
        c["like_count"] = len(c.get("likes", []))
        c["liked"] = viewer in c.get("likes", [])
        c["muted"] = c.get("author_id") in muted
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
    return {
        "post": _public(post, viewer),
        "comments": comments,
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
    if not (_owns(post) or _is_moderator()):
        raise HTTPException(403, "只能删除自己的帖子（工作区管理员可删除任何帖子）")
    store = _store()
    for c in store.list(Collections.COMMENTS):
        if c.get("post_id") == post_id:
            store.delete(Collections.COMMENTS, c["id"])
    store.delete(Collections.POSTS, post_id)
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
    if on and field == "likes":
        _notify_author(post, kind="like", title=f"{me()['name']} 赞了你的帖子「{post['title']}」")
    return {"on": on, "count": len(bucket)}


def _notify_author(post: dict[str, Any], *, kind: str, title: str, body: str = "", to: str | None = None) -> None:
    ctx = context.current()
    target = to or post.get("author_id")
    if not target or target == "system":
        return
    get_services().collab.notify(
        [target], kind=kind, title=title, body=body[:200], link=f"post/{post['id']}", actor_id=ctx.user_id,
        actor_name=ctx.user_name, source_type="post", source_id=post["id"], workspace_id=ctx.workspace_id,
        post_id=post["id"],
    )


@router.post("/posts/{post_id}/like")
def like_post(post_id: str) -> dict:
    return _toggle(post_id, "likes")


@router.post("/posts/{post_id}/favorite")
def favorite_post(post_id: str) -> dict:
    return _toggle(post_id, "favorites")


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
    comment = _add_comment(
        post, payload.body.strip(), author=me()["name"], author_id=me()["id"], parent_id=payload.parent_id
    )
    name = me()["name"]
    _notify_author(post, kind="comment", title=f"{name} 评论了你的帖子「{post['title']}」", body=payload.body)
    if payload.parent_id:
        parent = _store().get(Collections.COMMENTS, payload.parent_id) or {}
        if parent.get("author_id") and parent["author_id"] != post.get("author_id"):
            _notify_author(post, kind="comment", title=f"{name} 回复了你在「{post['title']}」下的评论",
                           body=payload.body, to=parent["author_id"])
    return {"comment": comment}


@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: str) -> dict:
    store = _store()
    c = store.get(Collections.COMMENTS, comment_id)
    if c is None:
        raise HTTPException(404, "评论不存在")
    if not (_owns(c) or (c.get("author") == AI_AUTHOR and _owns(_get_post(c["post_id"]))) or _is_moderator()):
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
        if a not in (SYSTEM_AUTHOR, AI_AUTHOR)
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


def seed_if_empty() -> None:
    """首次访问时写入几篇置顶 / 精华帖。帖子用固定 id：并发的首个请求同时进来也只会写同一份，不会出现重复帖。"""
    store = get_services().store
    if store.get(Collections.SETTINGS, "community_seeded"):
        if not store.get(Collections.SETTINGS, "community_dedup_v1"):
            with _SEED_LOCK:
                _dedup_seed_posts(store)
        return
    with _SEED_LOCK:
        if store.get(Collections.SETTINGS, "community_seeded"):
            return
        _seed(store)


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
