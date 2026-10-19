"""策略广场社交关系：用户资料、关注、拉黑与私信策略。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from ..store import Collections, WorkspaceStore
from .db import Database, jdump, jload, now_ms

DM_POLICIES = {
    "reply_only": "回复后可继续",
    "open": "允许自由私信",
    "none": "不允许私信",
}

# 个人主页可以自由展示 / 隐藏的板块。顺序即展示顺序，默认全部展示。
SHOWCASE_KEYS = ("factors", "strategies", "backtests", "models", "paper")
SHOWCASE_LABELS = {
    "factors": "因子",
    "strategies": "策略",
    "backtests": "回测结果",
    "models": "模型",
    "paper": "模拟收益",
}
SHOWCASE_LIMIT_MAX = 12


def default_showcase() -> dict[str, Any]:
    return {"items": [{"key": k, "show": True} for k in SHOWCASE_KEYS], "limit": 5}


def normalize_showcase(raw: Any) -> dict[str, Any]:
    """把用户提交的展示配置收敛成固定形状：已知板块、去重、未知板块自动补到末尾。"""
    data = raw if isinstance(raw, dict) else {}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = str((item or {}).get("key") or "")
        if key in SHOWCASE_KEYS and key not in seen:
            seen.add(key)
            ids = item.get("ids") if isinstance(item.get("ids"), list) else []
            out.append(
                {
                    "key": key,
                    "show": bool(item.get("show", True)),
                    "selection": "selected" if item.get("selection") == "selected" else "latest",
                    "ids": list(dict.fromkeys(str(i)[:200] for i in ids if isinstance(i, str) and i))[:100],
                }
            )
    for key in SHOWCASE_KEYS:  # 以后新增的板块：自动出现在末尾并默认展示
        if key not in seen:
            out.append({"key": key, "show": True})
    try:
        limit = int(data.get("limit"))
    except (TypeError, ValueError):
        limit = 5
    return {"items": out, "limit": max(1, min(SHOWCASE_LIMIT_MAX, limit))}


class SocialService:
    def __init__(self, db: Database, store: WorkspaceStore) -> None:
        self.db = db
        self.store = store

    def _ensure_profile(self, ws: str, user_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM community_profiles WHERE user_id=?", (user_id,))
        if row:
            return row
        now = now_ms()
        bio = ""
        try:
            legacy = self.store.for_workspace(ws).get(Collections.PROFILE, user_id) or {}
            bio = str(legacy.get("bio") or "")[:500]
        except Exception:
            bio = ""
        self.db.execute(
            "INSERT INTO community_profiles (user_id, workspace_id, bio, dm_policy, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, ws, bio, "reply_only", now, now),
        )
        return self.db.fetchone("SELECT * FROM community_profiles WHERE user_id=?", (user_id,)) or {
            "user_id": user_id,
            "workspace_id": ws,
            "bio": bio,
            "dm_policy": "reply_only",
        }

    def profile(self, ws: str, user_id: str) -> dict[str, Any]:
        row = self._ensure_profile(ws, user_id)
        policy = row.get("dm_policy") if row.get("dm_policy") in DM_POLICIES else "reply_only"
        color = row.get("avatar_color") if str(row.get("avatar_color") or "").startswith("#") else "#2E5C9A"
        return {
            "bio": row.get("bio") or "",
            "dm_policy": policy,
            "dm_policy_label": DM_POLICIES[policy],
            "avatar_color": color,
            "avatar_url": (self.store.for_workspace(ws).get(Collections.PROFILE, user_id) or {}).get("avatar_url", ""),
            "public_profile": bool(row.get("public_profile")),
            "showcase": normalize_showcase(jload(row.get("showcase"), None)),
        }

    def save_profile(
        self,
        ws: str,
        user_id: str,
        *,
        bio: str,
        dm_policy: str,
        avatar_color: str = "#2E5C9A",
        public_profile: bool = False,
        showcase: Any = None,
    ) -> dict[str, Any]:
        self._ensure_profile(ws, user_id)
        policy = dm_policy if dm_policy in DM_POLICIES else "reply_only"
        color = avatar_color if re.fullmatch(r"#[0-9a-fA-F]{6}", avatar_color or "") else "#2E5C9A"
        current = normalize_showcase(jload(self._ensure_profile(ws, user_id).get("showcase"), None))
        next_showcase = normalize_showcase(showcase) if showcase is not None else current
        self.db.execute(
            "UPDATE community_profiles SET bio=?, dm_policy=?, avatar_color=?, public_profile=?, showcase=?, "
            "updated_at=? WHERE user_id=?",
            (
                bio.strip()[:500],
                policy,
                color,
                1 if public_profile else 0,
                jdump(next_showcase),
                now_ms(),
                user_id,
            ),
        )
        return self.profile(ws, user_id)

    def user(self, ws: str, identifier: str) -> dict[str, Any] | None:
        ident = (identifier or "").strip()
        if not ident:
            return None
        row = self.db.fetchone(
            "SELECT u.id, u.username, u.name, u.email, u.quantlab_uid, u.is_active, u.banned_at, m.role, m.status "
            "FROM users u JOIN memberships m ON m.user_id=u.id "
            "WHERE m.workspace_id=? AND (u.id=? OR lower(u.username)=lower(?) OR lower(u.email)=lower(?))",
            (ws, ident, ident, ident),
        )
        if row and row.get("status") == "active" and row.get("is_active") and not row.get("banned_at"):
            return row
        return None

    # ------------------------------------------------------------------ 关注
    def is_following(self, ws: str, follower: str, following: str) -> bool:
        return bool(
            self.db.fetchone(
                "SELECT 1 AS x FROM user_follows WHERE workspace_id=? AND follower_id=? AND following_id=?",
                (ws, follower, following),
            )
        )

    def following_ids(self, ws: str, user_id: str) -> set[str]:
        return {
            str(r["following_id"])
            for r in self.db.fetchall(
                "SELECT following_id FROM user_follows WHERE workspace_id=? AND follower_id=?", (ws, user_id)
            )
        }

    def follow(self, ws: str, follower: str, following: str) -> bool:
        if follower == following or self.blocked(ws, follower, following) or self.blocked(ws, following, follower):
            return False
        return (
            self.db.execute(
                "INSERT INTO user_follows (id, workspace_id, follower_id, following_id, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT (workspace_id, follower_id, following_id) DO NOTHING",
                (uuid.uuid4().hex, ws, follower, following, now_ms()),
            )
            > 0
        )

    def unfollow(self, ws: str, follower: str, following: str) -> bool:
        return (
            self.db.execute(
                "DELETE FROM user_follows WHERE workspace_id=? AND follower_id=? AND following_id=?",
                (ws, follower, following),
            )
            > 0
        )

    def follow_counts(self, ws: str, user_id: str) -> tuple[int, int]:
        followers = int(
            self.db.scalar("SELECT COUNT(*) FROM user_follows WHERE workspace_id=? AND following_id=?", (ws, user_id))
            or 0
        )
        following = int(
            self.db.scalar("SELECT COUNT(*) FROM user_follows WHERE workspace_id=? AND follower_id=?", (ws, user_id))
            or 0
        )
        return followers, following

    # -------------------------------------------------------------- 互相关注
    def is_mutual(self, ws: str, a: str, b: str) -> bool:
        """互相关注：双向都在 follow 表里。"""
        if not a or not b or a == b:
            return False
        return self.is_following(ws, a, b) and self.is_following(ws, b, a)

    def mutual_ids(self, ws: str, user_id: str) -> list[str]:
        """和我互相关注的人（按名字排序）。"""
        return [str(r["id"]) for r in self.mutual_people(ws, user_id)]

    def mutual_people(self, ws: str, user_id: str) -> list[dict[str, Any]]:
        """互相关注的成员：@ 提及与快捷私聊的候选名单。"""
        if not user_id:
            return []
        rows = self.db.fetchall(
            "SELECT u.id, u.name, u.username, u.quantlab_uid, "
            "COALESCE(p.avatar_color, '#2E5C9A') AS avatar_color, COALESCE(p.public_profile, 0) AS public_profile "
            "FROM user_follows mine "
            "JOIN user_follows back ON back.workspace_id=mine.workspace_id "
            "  AND back.follower_id=mine.following_id AND back.following_id=mine.follower_id "
            "JOIN users u ON u.id=mine.following_id "
            "LEFT JOIN community_profiles p ON p.user_id=u.id "
            "WHERE mine.workspace_id=? AND mine.follower_id=? AND u.is_active=1 AND u.banned_at IS NULL "
            "ORDER BY u.name",
            (ws, user_id),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            if self.blocked(ws, user_id, str(r["id"])) or self.blocked(ws, str(r["id"]), user_id):
                continue
            out.append(
                {
                    "id": str(r["id"]),
                    "name": r.get("name") or r.get("username") or "研究员",
                    "username": r.get("username") or "",
                    "quantlab_uid": r.get("quantlab_uid") or "",
                    "avatar_color": r.get("avatar_color") or "#2E5C9A",
                    "public_profile": bool(r.get("public_profile")),
                }
            )
        return out

    # ------------------------------------------------------------------ 拉黑
    def blocked(self, ws: str, blocker: str, blocked: str) -> bool:
        return bool(
            self.db.fetchone(
                "SELECT 1 AS x FROM user_blocks WHERE workspace_id=? AND blocker_id=? AND blocked_id=?",
                (ws, blocker, blocked),
            )
        )

    def blocked_ids(self, ws: str, blocker: str) -> set[str]:
        return {
            str(r["blocked_id"])
            for r in self.db.fetchall(
                "SELECT blocked_id FROM user_blocks WHERE workspace_id=? AND blocker_id=?", (ws, blocker)
            )
        }

    def block(self, ws: str, blocker: str, blocked: str) -> bool:
        if blocker == blocked or self.blocked(ws, blocker, blocked):
            return False
        with self.db.tx() as t:
            t.execute(
                "INSERT INTO user_blocks (id, workspace_id, blocker_id, blocked_id, created_at) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, ws, blocker, blocked, now_ms()),
            )
            t.execute(
                "DELETE FROM user_follows WHERE workspace_id=? AND "
                "((follower_id=? AND following_id=?) OR (follower_id=? AND following_id=?))",
                (ws, blocker, blocked, blocked, blocker),
            )
        return True

    def unblock(self, ws: str, blocker: str, blocked: str) -> bool:
        return (
            self.db.execute(
                "DELETE FROM user_blocks WHERE workspace_id=? AND blocker_id=? AND blocked_id=?",
                (ws, blocker, blocked),
            )
            > 0
        )

    # ------------------------------------------------------------------ 收藏
    def set_favorite(
        self,
        ws: str,
        user_id: str,
        *,
        object_type: str,
        object_id: str,
        title: str,
        link: str,
        meta: dict[str, Any] | None = None,
    ) -> bool:
        row = self.db.fetchone(
            "SELECT id FROM favorites WHERE workspace_id=? AND user_id=? AND object_type=? AND object_id=?",
            (ws, user_id, object_type, object_id),
        )
        if row:
            return False
        self.db.execute(
            "INSERT INTO favorites (id, workspace_id, user_id, object_type, object_id, title, link, meta, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex,
                ws,
                user_id,
                object_type,
                object_id,
                title[:160],
                link[:300],
                jdump(meta or {}),
                now_ms(),
            ),
        )
        return True

    def remove_favorite(self, ws: str, user_id: str, object_type: str, object_id: str) -> bool:
        return (
            self.db.execute(
                "DELETE FROM favorites WHERE workspace_id=? AND user_id=? AND object_type=? AND object_id=?",
                (ws, user_id, object_type, object_id),
            )
            > 0
        )

    def is_favorite(self, ws: str, user_id: str, object_type: str, object_id: str) -> bool:
        return bool(
            self.db.fetchone(
                "SELECT 1 AS x FROM favorites WHERE workspace_id=? AND user_id=? AND object_type=? AND object_id=?",
                (ws, user_id, object_type, object_id),
            )
        )

    def favorites(self, ws: str, user_id: str, object_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM favorites WHERE workspace_id=? AND user_id=?"
        args: list[Any] = [ws, user_id]
        if object_type:
            sql += " AND object_type=?"
            args.append(object_type)
        rows = self.db.fetchall(sql + " ORDER BY created_at DESC", tuple(args))
        return [{**r, "meta": jload(r.get("meta"), {}) or {}} for r in rows]

    # ------------------------------------------------------------------ 私信策略
    def can_dm(self, gid: str, ws: str, sender: str, recipient: str) -> dict[str, Any]:
        if self.blocked(ws, sender, recipient) or self.blocked(ws, recipient, sender):
            return {"ok": False, "reason": "你们之间存在拉黑关系，无法发送私信", "policy": "blocked"}
        prof = self.profile(ws, recipient)
        policy = prof["dm_policy"]
        receiver_sent = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM collab_messages WHERE group_id=? AND author_id=? AND deleted_at IS NULL",
                (gid, recipient),
            )
            or 0
        )
        sender_sent = int(
            self.db.scalar(
                "SELECT COUNT(*) FROM collab_messages WHERE group_id=? AND author_id=? AND deleted_at IS NULL",
                (gid, sender),
            )
            or 0
        )
        if receiver_sent:
            return {"ok": True, "reason": "", "policy": policy, "established": True}
        if policy == "none":
            return {"ok": False, "reason": "对方已关闭私信", "policy": policy, "established": False}
        if policy == "reply_only" and sender_sent >= 1:
            return {
                "ok": False,
                "reason": "对方回复前，你只能发送一条私信",
                "policy": policy,
                "established": False,
                "awaiting_reply": True,
            }
        return {"ok": True, "reason": "", "policy": policy, "established": False}
