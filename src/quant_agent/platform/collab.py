"""工作区协作：群组讨论与任务摊派、对象可见性、编辑人员标记、通知中心与屏蔽、问题报告与附件。

参照飞书 / Slack / Linear 的做法，但只保留量化研究里真正用得上的部分：
- 群组：工作区内自由组建；公开群可被发现、直接加入，私密群只能由群主 / 群管理员拉人；
  群聊支持 Markdown 与引用（回测、策略、代码、文件、帖子、任务），@某人 / @所有人 会进通知中心；
- 任务摊派：群组内的任务看板（待办 → 进行中 → 待验收 → 完成），指派、改状态都会通知相关人；
- 可见性：策略 / 研究项目默认在工作区内公开（与旧版本行为一致），作者可以改为仅自己或指定群组；
- 编辑人员标记：每次保存记一笔「谁（人还是 Agent）在什么时候改出了哪个版本、派生自哪里」，
  策略血缘图与文件上的编辑者头像都来自这里；
- 通知中心：站内信箱，支持按人 / 群组 / 类型 / 帖子屏蔽；
- 问题报告：成员提交（可附截图 / 文件），部署管理员与工作区管理员回复、改状态。

所有写入都走数据库（见 migrations/0011_collab.sql），按人查询、并发安全。
"""

from __future__ import annotations

import hashlib
import io
import re
import secrets
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .db import Database, jdump, jload, now_ms
from .errors import AppError, ErrorCode
from .storage_usage import upload_storage

GROUP_ROLES = ("owner", "admin", "member")
TASK_STATUS = ("todo", "doing", "review", "done", "cancelled")
TASK_STATUS_LABEL = {"todo": "待办", "doing": "进行中", "review": "待验收", "done": "已完成", "cancelled": "已取消"}
TASK_PRIORITY = ("low", "normal", "high", "urgent")
VISIBILITY = ("private", "workspace", "group")
SHARE_TYPES = ("strategy", "project")
NOTIFY_KINDS = {
    "mention": "@我的",
    "dm": "私聊",
    "task": "任务",
    "group": "群组",
    "comment": "评论与回复",
    "like": "点赞与克隆",
    "follow": "关注",
    "share": "共享",
    "issue": "问题报告",
    "announce": "公告",
    "system": "系统",
}
MUTE_TYPES = ("user", "group", "kind", "post")
ISSUE_CATEGORIES = {"bug": "缺陷", "feature": "功能建议", "question": "使用问题", "data": "数据问题", "other": "其他"}
ISSUE_SEVERITY = ("low", "normal", "high", "critical")
ISSUE_STATUS = {"open": "待处理", "replied": "已回复", "resolved": "已解决", "closed": "已关闭"}

# 上传：只收图片、PDF、文本类与常见数据文件；单个 10 MB
UPLOAD_MAX = 10 * 1024 * 1024
UPLOAD_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/markdown": ".md",
    "application/json": ".json",
    "text/x-python": ".py",
    "application/zip": ".zip",
}
EXT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".json": "application/json",
    ".py": "text/x-python",
    ".zip": "application/zip",
}
# 文件头魔数：声明的类型与实际内容不符就拒绝（防止把 HTML 伪装成图片）
_MAGIC = {
    "image/png": (b"\x89PNG",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
    "application/pdf": (b"%PDF",),
    "application/zip": (b"PK\x03\x04", b"PK\x05\x06"),
}

#: link：平台内任意页面（报告、因子、模型……）的分享卡片，id 是站内路由（不带 #/）
REF_TYPES = ("backtest", "strategy", "code", "file", "post", "task", "project", "message", "report", "link")
MENTION_ALL = ("所有人", "all", "everyone")


def _err(msg: str, code: ErrorCode = ErrorCode.INVALID_PARAMETER) -> AppError:
    return AppError(code, msg)


def _deny(msg: str) -> AppError:
    return AppError(ErrorCode.PERMISSION_DENIED, msg)


def _code_key(raw: str) -> str:
    value = re.sub(r"[^0-9a-z]", "", (raw or "").lower())
    for prefix in ("qlg", "qlu", "quantlab"):
        if value.startswith(prefix):
            return value[len(prefix) :]
    return value


def _new_group_no(db: Database) -> str:
    for _ in range(32):
        candidate = f"QLG-{secrets.token_hex(4).upper()}"
        if not db.fetchone("SELECT 1 AS x FROM collab_groups WHERE quantlab_no=?", (candidate,)):
            return candidate
    raise _err("无法分配 QuantLab 群号，请稍后重试")


def _missing(msg: str) -> AppError:
    return AppError(ErrorCode.NOT_FOUND, msg)


def clean_refs(refs: Any) -> list[dict[str, Any]]:
    """引用只保留白名单字段：类型、id、标题、少量展示用摘要（不信任客户端传来的大对象）。"""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in refs or []:
        if not isinstance(r, dict):
            continue
        t = str(r.get("type") or "")
        rid = str(r.get("id") or "")[:120]
        if t not in REF_TYPES or not rid or (t, rid) in seen:
            continue
        seen.add((t, rid))
        item: dict[str, Any] = {"type": t, "id": rid, "title": str(r.get("title") or "")[:120]}
        if isinstance(r.get("summary"), dict):
            item["summary"] = {
                k: v for k, v in list(r["summary"].items())[:12] if isinstance(v, (int, float, str, type(None)))
            }
        if t == "code" and isinstance(r.get("code"), str):
            item["code"] = r["code"][:20000]
            item["lang"] = str(r.get("lang") or "python")[:20]
        if t == "file":
            item["mime"] = str(r.get("mime") or "")[:60]
            item["size"] = int(r.get("size") or 0)
        out.append(item)
    return out[:12]


class CollabService:
    def __init__(self, db: Database, home: Path) -> None:
        self.db = db
        self.home = Path(home)

    # ================================================================== 用户
    def names(self, ids: list[str] | set[str]) -> dict[str, str]:
        ids = [i for i in dict.fromkeys(ids) if i]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.db.fetchall(f"SELECT id, name, email FROM users WHERE id IN ({marks})", tuple(ids))
        out = {r["id"]: r["name"] or r["email"].split("@")[0] for r in rows}
        out.setdefault("system", "平台")
        return out

    def ws_member_ids(self, ws: str) -> list[str]:
        return [
            r["user_id"]
            for r in self.db.fetchall(
                "SELECT m.user_id FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.workspace_id=? AND m.status='active' AND u.is_active=1",
                (ws,),
            )
        ]

    def ws_role(self, ws: str, user_id: str) -> str | None:
        row = self.db.fetchone(
            "SELECT role FROM memberships WHERE workspace_id=? AND user_id=? AND status='active'", (ws, user_id)
        )
        return row["role"] if row else None

    # ================================================================== 通知中心
    def muted(self, user_id: str) -> set[tuple[str, str]]:
        return {
            (r["target_type"], r["target_id"])
            for r in self.db.fetchall(
                "SELECT target_type, target_id FROM notification_mutes WHERE user_id=?", (user_id,)
            )
        }

    def notify(
        self,
        user_ids: list[str] | set[str],
        *,
        kind: str,
        title: str,
        body: str = "",
        link: str = "",
        actor_id: str | None = None,
        actor_name: str = "",
        source_type: str = "",
        source_id: str = "",
        workspace_id: str | None = None,
        group_id: str | None = None,
        post_id: str | None = None,
        force: bool = False,
    ) -> int:
        """给一组人发站内通知。跳过触发者本人与屏蔽了该来源的人；公告（force）不受屏蔽影响。"""
        targets = [u for u in dict.fromkeys(user_ids) if u and u != actor_id and u != "system"]
        n = 0
        now = now_ms()
        for uid in targets:
            if not force:
                mutes = self.muted(uid)
                if ("kind", kind) in mutes:
                    continue
                if actor_id and ("user", actor_id) in mutes:
                    continue
                if group_id and ("group", group_id) in mutes and kind != "task":
                    continue
                if post_id and ("post", post_id) in mutes:
                    continue
            self.db.execute(
                "INSERT INTO notifications_inbox (user_id, workspace_id, kind, title, body, link, actor_id, actor_name, "
                "source_type, source_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uid,
                    workspace_id,
                    kind,
                    title[:200],
                    body[:1000],
                    link[:300],
                    actor_id,
                    actor_name[:60],
                    source_type,
                    str(source_id)[:120],
                    now,
                ),
            )
            n += 1
        return n

    def inbox(
        self,
        user_id: str,
        *,
        unread: bool = False,
        kind: str | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM notifications_inbox WHERE user_id=?"
        args: list[Any] = [user_id]
        if unread:
            sql += " AND read_at IS NULL"
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if before_id:
            sql += " AND id<?"
            args.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return self.db.fetchall(sql, tuple(args))

    def unread(self, user_id: str) -> dict[str, int]:
        rows = self.db.fetchall(
            "SELECT kind, COUNT(*) AS n FROM notifications_inbox WHERE user_id=? AND read_at IS NULL GROUP BY kind",
            (user_id,),
        )
        out = {r["kind"]: int(r["n"]) for r in rows}
        out["total"] = sum(out.values())
        return out

    def mark_read(self, user_id: str, ids: list[int] | None = None, kind: str | None = None) -> int:
        now = now_ms()
        if ids:
            marks = ",".join("?" * len(ids))
            return self.db.execute(
                f"UPDATE notifications_inbox SET read_at=? WHERE user_id=? AND read_at IS NULL AND id IN ({marks})",
                (now, user_id, *ids),
            )
        if kind:
            return self.db.execute(
                "UPDATE notifications_inbox SET read_at=? WHERE user_id=? AND read_at IS NULL AND kind=?",
                (now, user_id, kind),
            )
        return self.db.execute(
            "UPDATE notifications_inbox SET read_at=? WHERE user_id=? AND read_at IS NULL", (now, user_id)
        )

    def delete_notifications(self, user_id: str, ids: list[int] | None = None, read_only: bool = False) -> int:
        if ids:
            marks = ",".join("?" * len(ids))
            return self.db.execute(
                f"DELETE FROM notifications_inbox WHERE user_id=? AND id IN ({marks})", (user_id, *ids)
            )
        if read_only:
            return self.db.execute(
                "DELETE FROM notifications_inbox WHERE user_id=? AND read_at IS NOT NULL", (user_id,)
            )
        return 0

    def mutes(self, user_id: str) -> list[dict[str, Any]]:
        return self.db.fetchall(
            "SELECT target_type, target_id, label, created_at FROM notification_mutes WHERE user_id=? "
            "ORDER BY created_at DESC",
            (user_id,),
        )

    def mute(self, user_id: str, target_type: str, target_id: str, label: str = "") -> None:
        if target_type not in MUTE_TYPES:
            raise _err(f"屏蔽对象只能是 {'、'.join(MUTE_TYPES)}")
        if target_type == "kind" and target_id not in NOTIFY_KINDS:
            raise _err("未知的通知类型")
        if target_type == "kind" and target_id in ("announce", "issue"):
            raise _err("公告与问题报告的回复不能屏蔽")
        if target_type == "user" and target_id == user_id:
            raise _err("不能屏蔽自己")
        self.db.execute(
            "DELETE FROM notification_mutes WHERE user_id=? AND target_type=? AND target_id=?",
            (user_id, target_type, target_id),
        )
        self.db.execute(
            "INSERT INTO notification_mutes (user_id, target_type, target_id, label, created_at) VALUES (?,?,?,?,?)",
            (user_id, target_type, target_id, label[:80], now_ms()),
        )

    def unmute(self, user_id: str, target_type: str, target_id: str) -> bool:
        return bool(
            self.db.execute(
                "DELETE FROM notification_mutes WHERE user_id=? AND target_type=? AND target_id=?",
                (user_id, target_type, target_id),
            )
        )

    def is_user_muted(self, viewer: str, other: str) -> bool:
        return bool(
            self.db.fetchone(
                "SELECT 1 AS x FROM notification_mutes WHERE user_id=? AND target_type='user' AND target_id=?",
                (viewer, other),
            )
        )

    def muted_users(self, viewer: str) -> set[str]:
        return {
            r["target_id"]
            for r in self.db.fetchall(
                "SELECT target_id FROM notification_mutes WHERE user_id=? AND target_type='user'", (viewer,)
            )
        }

    # ================================================================== 群组
    def _group_row(self, gid: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM collab_groups WHERE id=?", (gid,))
        if row is None:
            raise _missing("群组不存在")
        return row

    def group_role(self, gid: str, user_id: str) -> str | None:
        row = self.db.fetchone("SELECT role FROM collab_group_members WHERE group_id=? AND user_id=?", (gid, user_id))
        return row["role"] if row else None

    def user_groups(self, ws: str, user_id: str) -> list[str]:
        return [
            r["id"]
            for r in self.db.fetchall(
                "SELECT g.id FROM collab_groups g JOIN collab_group_members m ON m.group_id=g.id "
                "WHERE g.workspace_id=? AND m.user_id=? AND g.archived_at IS NULL",
                (ws, user_id),
            )
        ]

    def group_member_ids(self, gid: str) -> list[str]:
        return [
            r["user_id"] for r in self.db.fetchall("SELECT user_id FROM collab_group_members WHERE group_id=?", (gid,))
        ]

    def _view_group(self, row: dict[str, Any], user_id: str) -> dict[str, Any]:
        settings = jload(row.get("settings"), {}) or {}
        role = self.group_role(row["id"], user_id)
        n = int(self.db.scalar("SELECT COUNT(*) AS n FROM collab_group_members WHERE group_id=?", (row["id"],)) or 0)
        from .collab_bot import bot_member

        if bot_member(row["id"], settings) and not settings.get("dm"):
            n += 1  # Bot 是虚拟成员：不复制用户关系，但显示与成员计数都算一个人
        last = self.db.fetchone(
            "SELECT id, body, author_name, created_at FROM collab_messages WHERE group_id=? AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (row["id"],),
        )
        unread = 0
        if role:
            read = self.db.scalar(
                "SELECT last_read_id FROM collab_group_members WHERE group_id=? AND user_id=?", (row["id"], user_id)
            )
            unread = int(
                self.db.scalar(
                    "SELECT COUNT(*) AS n FROM collab_messages WHERE group_id=? AND id>? AND deleted_at IS NULL "
                    "AND author_id<>?",
                    (row["id"], int(read or 0), user_id),
                )
                or 0
            )
        open_tasks = int(
            self.db.scalar(
                "SELECT COUNT(*) AS n FROM collab_tasks WHERE group_id=? AND status IN ('todo','doing','review')",
                (row["id"],),
            )
            or 0
        )
        dm = settings.get("dm") or []
        peer = next((u for u in dm if u != user_id), None) if dm else None
        return {
            "id": row["id"],
            "name": row["name"],
            # 私聊：两人的私密群组，界面显示对方名字
            "kind": "dm" if dm else "group",
            "peer": {"user_id": peer, "name": self.names([peer]).get(peer, "?")} if peer else None,
            "description": row["description"],
            "visibility": row["visibility"],
            "quantlab_no": row.get("quantlab_no") or "",
            "owner_id": row["owner_id"],
            "created_at": row["created_at"],
            "archived": bool(row.get("archived_at")),
            "member_count": n,
            "my_role": role,
            "unread": unread,
            "open_tasks": open_tasks,
            "last_message": (
                {"body": (last["body"] or "")[:80], "author": last["author_name"], "at": last["created_at"]}
                if last
                else None
            ),
            "ws_limits": settings.get("ws_limits") or [],
            "member_limits": settings.get("member_limits") or [],
        }

    def list_groups(self, ws: str, user_id: str, *, ws_admin: bool = False) -> list[dict[str, Any]]:
        """我加入的群 + 工作区内的公开群（管理员能看到全部）。"""
        rows = self.db.fetchall(
            "SELECT * FROM collab_groups WHERE workspace_id=? AND archived_at IS NULL ORDER BY created_at", (ws,)
        )
        mine = set(self.user_groups(ws, user_id))
        out = [
            self._view_group(r, user_id)
            for r in rows
            # 私聊只给两位当事人看，工作区管理员也不列出
            if r["id"] in mine or ((r["visibility"] == "open" or ws_admin) and '"dm"' not in (r.get("settings") or ""))
        ]
        out.sort(key=lambda g: (g["my_role"] is None, -((g["last_message"] or {}).get("at") or g["created_at"])))
        return out

    def get_group(self, ws: str, gid: str, user_id: str, *, ws_admin: bool = False) -> dict[str, Any]:
        row = self._group_row(gid)
        if row["workspace_id"] != ws:
            raise _missing("群组不存在")
        role = self.group_role(gid, user_id)
        if role is None and '"dm"' in (row.get("settings") or ""):
            raise _missing("群组不存在")  # 私聊只有两位当事人能看，管理员也不行
        if role is None and row["visibility"] != "open" and not ws_admin:
            raise _deny("这是私密群组，需要群主或群管理员邀请")
        view = self._view_group(row, user_id)
        names = self.names(self.group_member_ids(gid))
        members = self.db.fetchall(
            "SELECT user_id, role, joined_at FROM collab_group_members WHERE group_id=? ORDER BY joined_at", (gid,)
        )
        view["members"] = [{**m, "name": names.get(m["user_id"], "?")} for m in members]
        from .collab_bot import bot_member

        bot = bot_member(gid, self.group_settings(gid))
        if bot:
            view["members"].append(bot)
        return view

    def create_group(
        self,
        ws: str,
        owner_id: str,
        *,
        name: str,
        description: str = "",
        visibility: str = "open",
        members: list[str] | None = None,
    ) -> str:
        name = re.sub(r"\s+", " ", name).strip()[:40]
        if not name:
            raise _err("群组名称不能为空")
        if visibility not in ("open", "private"):
            raise _err("可见性只能是 open（公开）或 private（私密）")
        if self.db.fetchone(
            "SELECT id FROM collab_groups WHERE workspace_id=? AND name=? AND archived_at IS NULL", (ws, name)
        ):
            raise AppError(ErrorCode.CONFLICT, f"已有同名群组「{name}」")
        gid = uuid.uuid4().hex[:12]
        quantlab_no = _new_group_no(self.db)
        now = now_ms()
        ws_members = set(self.ws_member_ids(ws))
        with self.db.tx() as t:
            t.execute(
                "INSERT INTO collab_groups (id, workspace_id, name, description, visibility, owner_id, settings, "
                "created_at, quantlab_no) VALUES (?,?,?,?,?,?,?,?,?)",
                (gid, ws, name, description[:500], visibility, owner_id, "{}", now, quantlab_no),
            )
            t.execute(
                "INSERT INTO collab_group_members (group_id, user_id, role, joined_at) VALUES (?,?,?,?)",
                (gid, owner_id, "owner", now),
            )
            for uid in dict.fromkeys(members or []):
                if uid != owner_id and uid in ws_members:
                    t.execute(
                        "INSERT INTO collab_group_members (group_id, user_id, role, joined_at) VALUES (?,?,?,?)",
                        (gid, uid, "member", now),
                    )
        self._system_message(gid, ws, f"群组「{name}」已创建")
        return gid

    def open_dm(self, ws: str, user_id: str, peer_id: str) -> str:
        """和某位工作区成员的私聊：两人的私密群组，同一对人只有一个（id 由两人 id 决定）。"""
        if peer_id == user_id:
            raise _err("不能和自己私聊")
        if peer_id not in set(self.ws_member_ids(ws)):
            raise _missing("对方不在这个工作区")
        a, b = sorted((user_id, peer_id))
        gid = "dm" + hashlib.sha256(f"{ws}|{a}|{b}".encode()).hexdigest()[:14]
        if self.db.fetchone("SELECT id FROM collab_groups WHERE id=?", (gid,)):
            self.db.execute("UPDATE collab_groups SET archived_at=NULL WHERE id=?", (gid,))
            return gid
        now = now_ms()
        with self.db.tx() as t:
            t.execute(
                "INSERT INTO collab_groups (id, workspace_id, name, description, visibility, owner_id, settings, "
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (gid, ws, f"私聊 {gid[-6:]}", "", "private", a, jdump({"dm": [a, b]}), now),
            )
            for uid in (a, b):
                t.execute(
                    "INSERT INTO collab_group_members (group_id, user_id, role, joined_at) VALUES (?,?,?,?)",
                    (gid, uid, "member", now),
                )
        return gid

    def is_dm(self, gid: str) -> bool:
        return bool((self.group_settings(gid) or {}).get("dm"))

    def require_group_admin(self, gid: str, user_id: str, *, ws_admin: bool = False) -> dict[str, Any]:
        row = self._group_row(gid)
        if ws_admin:
            return row
        if self.group_role(gid, user_id) not in ("owner", "admin"):
            raise _deny("只有群主或群管理员可以执行该操作")
        return row

    def require_group_member(self, gid: str, user_id: str, *, ws: str | None = None) -> dict[str, Any]:
        row = self._group_row(gid)
        if ws and row["workspace_id"] != ws:
            raise _missing("群组不存在")
        if row.get("archived_at"):
            raise _err("群组已归档")
        if self.group_role(gid, user_id) is None:
            raise _deny("你不在这个群组里")
        return row

    def update_group(self, gid: str, patch: dict[str, Any]) -> None:
        row = self._group_row(gid)
        settings = jload(row.get("settings"), {}) or {}
        fields: dict[str, Any] = {}
        if patch.get("name"):
            fields["name"] = re.sub(r"\s+", " ", str(patch["name"])).strip()[:40]
        if patch.get("description") is not None:
            fields["description"] = str(patch["description"])[:500]
        if patch.get("visibility") in ("open", "private"):
            fields["visibility"] = patch["visibility"]
        for k in ("ws_limits", "member_limits"):
            if patch.get(k) is not None:
                from ..llm.hub import clean_limits

                settings[k] = clean_limits(patch[k])
        fields["settings"] = jdump(settings)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE collab_groups SET {sets} WHERE id=?", (*fields.values(), gid))

    def group_settings(self, gid: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT settings FROM collab_groups WHERE id=?", (gid,))
        return jload(row["settings"], {}) if row else {}

    def rotate_group_number(self, gid: str) -> str:
        """销毁旧群号并生成新群号；群组本身、消息和任务不变。"""
        row = self._group_row(gid)
        if '"dm"' in (row.get("settings") or ""):
            raise _err("私聊没有公开群号")
        number = _new_group_no(self.db)
        self.db.execute("UPDATE collab_groups SET quantlab_no=? WHERE id=?", (number, gid))
        return number

    def discover(self, ws: str, viewer: str, query: str = "") -> dict[str, Any]:
        """公开目录：只返回显式公开的用户资料与公开群组。按名称或号码搜索。"""
        q = (query or "").strip()
        like = f"%{q}%"
        key = _code_key(q)
        groups = self.db.fetchall(
            "SELECT * FROM collab_groups WHERE workspace_id=? AND archived_at IS NULL AND visibility='open' "
            "AND settings NOT LIKE '%\"dm\"%' AND (?='' OR name LIKE ? OR replace(lower(quantlab_no),'-','') LIKE ?) "
            "ORDER BY created_at DESC LIMIT 30",
            (ws, q, like, f"%{key.lower()}%"),
        )
        users = self.db.fetchall(
            "SELECT u.id, u.username, u.name, u.quantlab_uid, p.bio, p.avatar_color, m.role "
            "FROM users u JOIN memberships m ON m.user_id=u.id "
            "JOIN community_profiles p ON p.user_id=u.id "
            "WHERE m.workspace_id=? AND m.status='active' AND u.is_active=1 AND p.public_profile=1 "
            "AND (?='' OR u.name LIKE ? OR u.username LIKE ? OR replace(lower(u.quantlab_uid),'-','') LIKE ?) "
            "ORDER BY u.name LIMIT 30",
            (ws, q, like, like, f"%{key.lower()}%"),
        )
        return {
            "query": q,
            "groups": [self._view_group(r, viewer) for r in groups],
            "users": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "username": r.get("username") or "",
                    "quantlab_uid": r.get("quantlab_uid") or "",
                    "bio": r.get("bio") or "",
                    "avatar_color": r.get("avatar_color") or "#2E5C9A",
                    "role": r.get("role") or "member",
                }
                for r in users
            ],
        }

    def archive_group(self, gid: str) -> None:
        self.db.execute("UPDATE collab_groups SET archived_at=? WHERE id=?", (now_ms(), gid))

    def add_members(self, gid: str, user_ids: list[str], *, by: str, by_name: str) -> list[str]:
        row = self._group_row(gid)
        if '"dm"' in (row.get("settings") or ""):
            raise _err("私聊不能拉人，可以新建一个群组")
        ws_members = set(self.ws_member_ids(row["workspace_id"]))
        existing = set(self.group_member_ids(gid))
        added = [u for u in dict.fromkeys(user_ids) if u in ws_members and u not in existing]
        now = now_ms()
        for uid in added:
            self.db.execute(
                "INSERT INTO collab_group_members (group_id, user_id, role, joined_at) VALUES (?,?,?,?)",
                (gid, uid, "member", now),
            )
        if added:
            names = self.names(added)
            self._system_message(gid, row["workspace_id"], f"{by_name} 邀请 {'、'.join(names.values())} 加入了群组")
            self.notify(
                added,
                kind="group",
                title=f"{by_name} 邀请你加入群组「{row['name']}」",
                link=f"groups/{gid}",
                actor_id=by,
                actor_name=by_name,
                source_type="group",
                source_id=gid,
                workspace_id=row["workspace_id"],
            )
        return added

    def join(self, gid: str, user_id: str, user_name: str) -> None:
        row = self._group_row(gid)
        if row["visibility"] != "open":
            raise _deny("这是私密群组，需要群主或群管理员邀请")
        if self.group_role(gid, user_id):
            return
        if user_id not in self.ws_member_ids(row["workspace_id"]):
            raise _deny("你不在该工作区")
        self.db.execute(
            "INSERT INTO collab_group_members (group_id, user_id, role, joined_at) VALUES (?,?,?,?)",
            (gid, user_id, "member", now_ms()),
        )
        self._system_message(gid, row["workspace_id"], f"{user_name} 加入了群组")

    def remove_member(self, gid: str, user_id: str, *, by_name: str, left: bool = False) -> None:
        row = self._group_row(gid)
        role = self.group_role(gid, user_id)
        if role is None:
            raise _missing("该成员不在群组里")
        if role == "owner":
            raise _err("群主不能退出或被移出，请先转让群主")
        self.db.execute("DELETE FROM collab_group_members WHERE group_id=? AND user_id=?", (gid, user_id))
        name = self.names([user_id]).get(user_id, "?")
        self._system_message(
            gid, row["workspace_id"], f"{name} 退出了群组" if left else f"{by_name} 将 {name} 移出了群组"
        )

    def set_member_role(self, gid: str, user_id: str, role: str, *, actor_id: str) -> None:
        if role not in GROUP_ROLES:
            raise _err("群角色只能是 owner / admin / member")
        cur = self.group_role(gid, user_id)
        if cur is None:
            raise _missing("该成员不在群组里")
        if role == "owner":
            # 转让群主：原群主降为群管理员
            row = self._group_row(gid)
            if row["owner_id"] != actor_id:
                raise _deny("只有群主可以转让群主")
            with self.db.tx() as t:
                t.execute(
                    "UPDATE collab_group_members SET role='admin' WHERE group_id=? AND user_id=?", (gid, actor_id)
                )
                t.execute("UPDATE collab_group_members SET role='owner' WHERE group_id=? AND user_id=?", (gid, user_id))
                t.execute("UPDATE collab_groups SET owner_id=? WHERE id=?", (user_id, gid))
            return
        if cur == "owner":
            raise _err("请先把群主转让给其他成员")
        self.db.execute("UPDATE collab_group_members SET role=? WHERE group_id=? AND user_id=?", (role, gid, user_id))

    # ------------------------------------------------------------------ 群聊
    def _system_message(self, gid: str, ws: str, text: str) -> None:
        self.db.execute(
            "INSERT INTO collab_messages (group_id, workspace_id, author_id, author_name, author_kind, body, refs, "
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (gid, ws, "system", "系统", "system", text, "[]", now_ms()),
        )

    def post_message(
        self,
        gid: str,
        *,
        author_id: str,
        author_name: str,
        body: str,
        refs: Any = None,
        reply_to: int | None = None,
        author_kind: str = "user",
        auto_mention: bool = True,
    ) -> dict[str, Any]:
        row = self._group_row(gid)
        body = (body or "").strip()
        refs_clean = clean_refs(refs)
        if reply_to and not self.db.fetchone(
            "SELECT id FROM collab_messages WHERE id=? AND group_id=?", (reply_to, gid)
        ):
            reply_to = None
        # 回复默认带上 @对方：读者一眼看清在回谁，也让 @ 高亮与通知有一致的依据。
        # 私聊（两人对话）不加前缀；对方是 Bot / 系统时不加，否则会反过来触发 Bot 回复。
        if auto_mention and reply_to and '"dm"' not in (row.get("settings") or ""):
            parent = self.db.fetchone(
                "SELECT author_id, author_name, author_kind FROM collab_messages WHERE id=?", (reply_to,)
            )
            if parent and parent.get("author_id") != author_id and parent.get("author_kind") == "user":
                who = str(parent.get("author_name") or "").strip()
                mention = f"@{who}"
                if who and not body.startswith(mention):
                    body = f"{mention} {body}".strip()
        if not body and not refs_clean:
            raise _err("消息不能为空")
        if len(body) > 20000:
            raise _err("消息太长（上限 2 万字）")
        with self.db.tx() as t:
            mid = t.insert_id(
                "INSERT INTO collab_messages (group_id, workspace_id, author_id, author_name, author_kind, body, refs, "
                "reply_to, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    gid,
                    row["workspace_id"],
                    author_id,
                    author_name,
                    author_kind,
                    body,
                    jdump(refs_clean),
                    reply_to,
                    now_ms(),
                ),
            )
            t.execute(
                "UPDATE collab_group_members SET last_read_id=? WHERE group_id=? AND user_id=?", (mid, gid, author_id)
            )
        members = self.group_member_ids(gid)
        if '"dm"' in (row.get("settings") or ""):
            # 私聊：每条消息都提醒对方（头像红点与通知中心）
            self.notify(
                [u for u in members if u != author_id],
                kind="dm",
                title=f"{author_name} 给你发了私信",
                body=body[:200] or "[引用]",
                link=f"groups/{gid}",
                actor_id=author_id,
                actor_name=author_name,
                source_type="message",
                source_id=str(mid),
                workspace_id=row["workspace_id"],
                group_id=gid,
            )
            return self.message(mid, viewer=author_id)
        # @提及：@所有人 / @成员名
        names = self.names(members)
        mentioned: set[str] = set()
        if any(f"@{w}" in body for w in MENTION_ALL):
            mentioned.update(members)
        for uid, nm in sorted(names.items(), key=lambda kv: -len(kv[1])):
            if nm and f"@{nm}" in body:
                mentioned.add(uid)
        if reply_to:
            parent = self.db.fetchone("SELECT author_id FROM collab_messages WHERE id=?", (reply_to,))
            if parent and parent["author_id"] in members:
                mentioned.add(parent["author_id"])
        if mentioned:
            self.notify(
                mentioned,
                kind="mention",
                title=f"{author_name} 在「{row['name']}」中提到了你",
                body=body[:200] or "[引用或附件]",
                link=f"groups/{gid}",
                actor_id=author_id,
                actor_name=author_name,
                source_type="message",
                source_id=str(mid),
                workspace_id=row["workspace_id"],
                group_id=gid,
            )
        remaining = [u for u in members if u != author_id and u not in mentioned]
        if remaining:
            self.notify(
                remaining,
                kind="group",
                title=f"{author_name} 在「{row['name']}」发来消息",
                body=body[:200] or "[引用或附件]",
                link=f"groups/{gid}",
                actor_id=author_id,
                actor_name=author_name,
                source_type="message",
                source_id=str(mid),
                workspace_id=row["workspace_id"],
                group_id=gid,
            )
        return self.message(mid, viewer=author_id)

    def message(self, mid: int, *, viewer: str = "") -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM collab_messages WHERE id=?", (mid,))
        if row is None:
            raise _missing("消息不存在")
        return self._view_message(row, self._reactions_for([mid], viewer).get(mid, []))

    def _reactions_for(self, ids: list[int], viewer: str = "") -> dict[int, list[dict[str, Any]]]:
        clean = list(dict.fromkeys(int(i) for i in ids if i))
        if not clean:
            return {}
        marks = ",".join("?" for _ in clean)
        rows = self.db.fetchall(
            f"SELECT message_id, emoji, COUNT(*) AS count, "
            f"MAX(CASE WHEN user_id=? THEN 1 ELSE 0 END) AS mine "
            f"FROM collab_message_reactions WHERE message_id IN ({marks}) "
            "GROUP BY message_id, emoji ORDER BY MIN(created_at)",
            (viewer, *clean),
        )
        out: dict[int, list[dict[str, Any]]] = {i: [] for i in clean}
        for row in rows:
            mid = int(row["message_id"])
            out.setdefault(mid, []).append(
                {"emoji": str(row["emoji"]), "count": int(row["count"]), "mine": bool(row["mine"])}
            )
        return out

    @staticmethod
    def _view_message(row: dict[str, Any], reactions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        deleted = bool(row.get("deleted_at"))
        return {
            "id": row["id"],
            "group_id": row["group_id"],
            "author_id": row["author_id"],
            "author_name": row["author_name"],
            "author_kind": row["author_kind"],
            "body": "" if deleted else row["body"],
            "refs": [] if deleted else jload(row["refs"], []),
            "reply_to": row.get("reply_to"),
            "pinned": bool(row.get("pinned")),
            "created_at": row["created_at"],
            "edited_at": row.get("edited_at"),
            "deleted": deleted,
            "reactions": [] if deleted else (reactions or []),
        }

    def react_message(self, mid: int, user_id: str, emoji: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT id, deleted_at FROM collab_messages WHERE id=?", (mid,))
        if row is None:
            raise _missing("消息不存在")
        if row.get("deleted_at"):
            raise _err("已撤回的消息不能回应")
        existing = self.db.fetchone(
            "SELECT 1 FROM collab_message_reactions WHERE message_id=? AND user_id=? AND emoji=?",
            (mid, user_id, emoji),
        )
        with self.db.tx() as t:
            if existing:
                t.execute(
                    "DELETE FROM collab_message_reactions WHERE message_id=? AND user_id=? AND emoji=?",
                    (mid, user_id, emoji),
                )
            else:
                t.execute(
                    "INSERT INTO collab_message_reactions (message_id, user_id, emoji, created_at) VALUES (?,?,?,?)",
                    (mid, user_id, emoji, now_ms()),
                )
        reactions = self._reactions_for([mid], user_id).get(mid, [])
        count = next((int(r["count"]) for r in reactions if r["emoji"] == emoji), 0)
        return {"emoji": emoji, "on": not existing, "count": count, "reactions": reactions}

    def messages(
        self, gid: str, *, after_id: int = 0, before_id: int | None = None, limit: int = 60, viewer: str = ""
    ) -> list[dict[str, Any]]:
        if after_id:
            rows = self.db.fetchall(
                "SELECT * FROM collab_messages WHERE group_id=? AND id>? ORDER BY id LIMIT ?", (gid, after_id, limit)
            )
        else:
            sql = "SELECT * FROM collab_messages WHERE group_id=?"
            args: list[Any] = [gid]
            if before_id:
                sql += " AND id<?"
                args.append(before_id)
            rows = self.db.fetchall(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))
            rows.reverse()
        reactions = self._reactions_for([int(r["id"]) for r in rows], viewer)
        return [self._view_message(r, reactions.get(int(r["id"]), [])) for r in rows]

    def pinned_messages(self, gid: str, *, viewer: str = "") -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM collab_messages WHERE group_id=? AND pinned=1 AND deleted_at IS NULL ORDER BY id DESC",
            (gid,),
        )
        reactions = self._reactions_for([int(r["id"]) for r in rows], viewer)
        return [self._view_message(r, reactions.get(int(r["id"]), [])) for r in rows]

    def mark_group_read(self, gid: str, user_id: str, last_id: int) -> None:
        self.db.execute(
            "UPDATE collab_group_members SET last_read_id=? WHERE group_id=? AND user_id=? AND last_read_id<?",
            (last_id, gid, user_id, last_id),
        )
        group = self.db.fetchone("SELECT workspace_id FROM collab_groups WHERE id=?", (gid,))
        if group:
            self.db.execute(
                "UPDATE notifications_inbox SET read_at=? WHERE user_id=? AND workspace_id=? AND link=? AND read_at IS NULL",
                (now_ms(), user_id, group["workspace_id"], f"groups/{gid}"),
            )

    def edit_message(self, mid: int, user_id: str, body: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM collab_messages WHERE id=?", (mid,))
        if row is None or row.get("deleted_at"):
            raise _missing("消息不存在")
        if row["author_id"] != user_id:
            raise _deny("只能编辑自己的消息")
        self.db.execute("UPDATE collab_messages SET body=?, edited_at=? WHERE id=?", (body[:20000], now_ms(), mid))
        return self.message(mid, viewer=user_id)

    def delete_message(self, mid: int, user_id: str, *, moderator: bool) -> None:
        row = self.db.fetchone("SELECT * FROM collab_messages WHERE id=?", (mid,))
        if row is None:
            raise _missing("消息不存在")
        if row["author_id"] != user_id and not moderator:
            raise _deny("只能撤回自己的消息（群主 / 群管理员可删除任何消息）")
        self.db.execute("UPDATE collab_messages SET deleted_at=?, pinned=0 WHERE id=?", (now_ms(), mid))

    def pin_message(self, mid: int) -> bool:
        row = self.db.fetchone("SELECT pinned FROM collab_messages WHERE id=? AND deleted_at IS NULL", (mid,))
        if row is None:
            raise _missing("消息不存在")
        on = not bool(row["pinned"])
        self.db.execute("UPDATE collab_messages SET pinned=? WHERE id=?", (1 if on else 0, mid))
        return on

    # ------------------------------------------------------------------ 任务
    def _view_task(self, row: dict[str, Any], names: dict[str, str] | None = None) -> dict[str, Any]:
        names = names if names is not None else self.names([row.get("assignee_id") or "", row["created_by"]])
        return {
            **{
                k: row[k]
                for k in (
                    "id",
                    "group_id",
                    "title",
                    "description",
                    "assignee_id",
                    "created_by",
                    "status",
                    "priority",
                    "due_at",
                    "created_at",
                    "updated_at",
                    "done_at",
                )
            },
            "refs": jload(row["refs"], []),
            "assignee_name": names.get(row.get("assignee_id") or "", ""),
            "creator_name": names.get(row["created_by"], ""),
            "status_label": TASK_STATUS_LABEL.get(row["status"], row["status"]),
            "overdue": bool(
                row.get("due_at") and row["due_at"] < now_ms() and row["status"] not in ("done", "cancelled")
            ),
        }

    def tasks(
        self,
        *,
        group_id: str | None = None,
        assignee_id: str | None = None,
        ws: str | None = None,
        include_closed: bool = True,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM collab_tasks WHERE 1=1"
        args: list[Any] = []
        if group_id:
            sql += " AND group_id=?"
            args.append(group_id)
        if assignee_id:
            sql += " AND assignee_id=?"
            args.append(assignee_id)
        if ws:
            sql += " AND workspace_id=?"
            args.append(ws)
        if not include_closed:
            sql += " AND status NOT IN ('done','cancelled')"
        rows = self.db.fetchall(sql + " ORDER BY created_at DESC LIMIT 500", tuple(args))
        names = self.names({r.get("assignee_id") or "" for r in rows} | {r["created_by"] for r in rows})
        prio = {p: i for i, p in enumerate(reversed(TASK_PRIORITY))}
        out = [self._view_task(r, names) for r in rows]
        out.sort(key=lambda t: (prio.get(t["priority"], 9), t["due_at"] or 9e15))
        return out

    def task(self, tid: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM collab_tasks WHERE id=?", (tid,))
        if row is None:
            raise _missing("任务不存在")
        return self._view_task(row)

    def create_task(
        self,
        gid: str,
        *,
        by: str,
        by_name: str,
        title: str,
        description: str = "",
        assignee_id: str | None = None,
        priority: str = "normal",
        due_at: int | None = None,
        refs: Any = None,
    ) -> dict[str, Any]:
        row = self._group_row(gid)
        title = title.strip()[:120]
        if not title:
            raise _err("任务标题不能为空")
        if priority not in TASK_PRIORITY:
            raise _err("优先级只能是 low / normal / high / urgent")
        if assignee_id and self.group_role(gid, assignee_id) is None:
            raise _err("只能把任务指派给群组成员")
        tid = uuid.uuid4().hex[:12]
        now = now_ms()
        self.db.execute(
            "INSERT INTO collab_tasks (id, group_id, workspace_id, title, description, assignee_id, created_by, status, "
            "priority, due_at, refs, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid,
                gid,
                row["workspace_id"],
                title,
                description[:4000],
                assignee_id or None,
                by,
                "todo",
                priority,
                due_at,
                jdump(clean_refs(refs)),
                now,
                now,
            ),
        )
        who = self.names([assignee_id]).get(assignee_id or "", "") if assignee_id else ""
        self._system_message(
            gid, row["workspace_id"], f"{by_name} 创建了任务「{title}」" + (f"，指派给 {who}" if who else "")
        )
        if assignee_id:
            self.notify(
                [assignee_id],
                kind="task",
                title=f"{by_name} 给你指派了任务「{title}」",
                body=description[:200],
                link=f"groups/{gid}/tasks",
                actor_id=by,
                actor_name=by_name,
                source_type="task",
                source_id=tid,
                workspace_id=row["workspace_id"],
                group_id=gid,
            )
        return self.task(tid)

    def update_task(self, tid: str, patch: dict[str, Any], *, by: str, by_name: str, moderator: bool) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM collab_tasks WHERE id=?", (tid,))
        if row is None:
            raise _missing("任务不存在")
        involved = by in (row["created_by"], row.get("assignee_id"))
        if not (involved or moderator):
            raise _deny("只有任务创建者、负责人或群主 / 群管理员可以修改任务")
        fields: dict[str, Any] = {}
        gid = row["group_id"]
        if patch.get("title"):
            fields["title"] = str(patch["title"]).strip()[:120]
        if patch.get("description") is not None:
            fields["description"] = str(patch["description"])[:4000]
        if patch.get("priority"):
            if patch["priority"] not in TASK_PRIORITY:
                raise _err("优先级只能是 low / normal / high / urgent")
            fields["priority"] = patch["priority"]
        if "due_at" in patch:
            fields["due_at"] = patch["due_at"] or None
        if patch.get("refs") is not None:
            fields["refs"] = jdump(clean_refs(patch["refs"]))
        new_assignee = None
        if "assignee_id" in patch:
            aid = patch["assignee_id"] or None
            if aid and self.group_role(gid, aid) is None:
                raise _err("只能把任务指派给群组成员")
            if aid != row.get("assignee_id"):
                fields["assignee_id"] = aid
                new_assignee = aid
        if patch.get("status"):
            st = patch["status"]
            if st not in TASK_STATUS:
                raise _err("状态只能是 " + " / ".join(TASK_STATUS))
            if st != row["status"]:
                fields["status"] = st
                fields["done_at"] = now_ms() if st == "done" else None
        if not fields:
            return self._view_task(row)
        fields["updated_at"] = now_ms()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE collab_tasks SET {sets} WHERE id=?", (*fields.values(), tid))
        title = fields.get("title") or row["title"]
        if new_assignee:
            self.notify(
                [new_assignee],
                kind="task",
                title=f"{by_name} 把任务「{title}」指派给了你",
                link=f"groups/{gid}/tasks",
                actor_id=by,
                actor_name=by_name,
                source_type="task",
                source_id=tid,
                workspace_id=row["workspace_id"],
                group_id=gid,
            )
        if "status" in fields:
            label = TASK_STATUS_LABEL[fields["status"]]
            self._system_message(gid, row["workspace_id"], f"{by_name} 把任务「{title}」标为{label}")
            self.notify(
                [row["created_by"], row.get("assignee_id") or ""],
                kind="task",
                title=f"任务「{title}」→ {label}",
                link=f"groups/{gid}/tasks",
                actor_id=by,
                actor_name=by_name,
                source_type="task",
                source_id=tid,
                workspace_id=row["workspace_id"],
                group_id=gid,
            )
        return self.task(tid)

    def delete_task(self, tid: str, *, by: str, moderator: bool) -> None:
        row = self.db.fetchone("SELECT created_by FROM collab_tasks WHERE id=?", (tid,))
        if row is None:
            raise _missing("任务不存在")
        if row["created_by"] != by and not moderator:
            raise _deny("只有任务创建者或群主 / 群管理员可以删除任务")
        self.db.execute("DELETE FROM collab_tasks WHERE id=?", (tid,))

    # ================================================================== 可见性
    def share(self, ws: str, object_type: str, object_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM collab_shares WHERE workspace_id=? AND object_type=? AND object_id=?",
            (ws, object_type, object_id),
        )
        if row:
            row["group_ids"] = jload(row["group_ids"], [])
        return row

    def shares(self, ws: str, object_type: str) -> dict[str, dict[str, Any]]:
        rows = self.db.fetchall("SELECT * FROM collab_shares WHERE workspace_id=? AND object_type=?", (ws, object_type))
        return {r["object_id"]: {**r, "group_ids": jload(r["group_ids"], [])} for r in rows}

    def owner_of(self, ws: str, object_type: str, object_id: str) -> str | None:
        sh = self.share(ws, object_type, object_id)
        if sh:
            return sh["owner_id"]
        row = self.db.fetchone(
            "SELECT user_id FROM collab_edits WHERE workspace_id=? AND object_type=? AND object_id=? "
            "AND actor_kind='human' ORDER BY id LIMIT 1",
            (ws, object_type, object_id),
        )
        return row["user_id"] if row else None

    def claim(self, ws: str, object_type: str, object_id: str, owner_id: str) -> None:
        """新建对象时登记所有者（默认在工作区内公开，与旧版本行为一致）。已有记录不覆盖。"""
        if object_type not in SHARE_TYPES or not owner_id or owner_id == "system":
            return
        if self.share(ws, object_type, object_id):
            return
        try:
            self.db.execute(
                "INSERT INTO collab_shares (workspace_id, object_type, object_id, owner_id, visibility, group_ids, "
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (ws, object_type, object_id, owner_id, "workspace", "[]", now_ms()),
            )
        except Exception:  # 并发首存：另一请求已登记
            pass

    def set_share(
        self,
        ws: str,
        object_type: str,
        object_id: str,
        *,
        user_id: str,
        ws_admin: bool,
        visibility: str,
        group_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if object_type not in SHARE_TYPES:
            raise _err("只能设置策略或研究项目的可见性")
        if visibility not in VISIBILITY:
            raise _err("可见性只能是 private / workspace / group")
        owner = self.owner_of(ws, object_type, object_id)
        if owner and owner != user_id and not ws_admin:
            raise _deny("只有作者或工作区管理员可以修改可见性")
        if not owner and not ws_admin:
            raise _deny("这份内容没有登记作者，请工作区管理员设置可见性")
        gids = [g for g in dict.fromkeys(group_ids or []) if g]
        if visibility == "group":
            if not gids:
                raise _err("选择至少一个群组")
            valid = {
                r["id"]
                for r in self.db.fetchall(
                    "SELECT id FROM collab_groups WHERE workspace_id=? AND archived_at IS NULL", (ws,)
                )
            }
            gids = [g for g in gids if g in valid]
            if not gids:
                raise _err("群组不存在")
        else:
            gids = []
        owner = owner or user_id
        self.db.execute(
            "DELETE FROM collab_shares WHERE workspace_id=? AND object_type=? AND object_id=?",
            (ws, object_type, object_id),
        )
        self.db.execute(
            "INSERT INTO collab_shares (workspace_id, object_type, object_id, owner_id, visibility, group_ids, "
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (ws, object_type, object_id, owner, visibility, jdump(gids), now_ms()),
        )
        if visibility == "group":
            members: set[str] = set()
            for g in gids:
                members.update(self.group_member_ids(g))
            self.notify(
                members,
                kind="share",
                title=f"有一个{'策略' if object_type == 'strategy' else '研究项目'}在群组内共享给你",
                body=object_id,
                link=("editor/" if object_type == "strategy" else "lab/") + object_id,
                actor_id=user_id,
                actor_name=self.names([user_id]).get(user_id, ""),
                source_type=object_type,
                source_id=object_id,
                workspace_id=ws,
            )
        return self.share(ws, object_type, object_id) or {}

    def visible_to(
        self, share: dict[str, Any] | None, user_id: str, *, ws_admin: bool, my_groups: set[str] | None = None
    ) -> bool:
        if share is None or share["visibility"] == "workspace" or ws_admin or share["owner_id"] == user_id:
            return True
        if share["visibility"] == "private":
            return False
        return bool(set(share.get("group_ids") or []) & (my_groups or set()))

    def hidden_ids(self, ws: str, object_type: str, user_id: str, *, ws_admin: bool) -> set[str]:
        if ws_admin:
            return set()
        my_groups = set(self.user_groups(ws, user_id))
        return {
            oid
            for oid, sh in self.shares(ws, object_type).items()
            if not self.visible_to(sh, user_id, ws_admin=False, my_groups=my_groups)
        }

    def assert_visible(self, ws: str, object_type: str, object_id: str, user_id: str, *, ws_admin: bool) -> None:
        sh = self.share(ws, object_type, object_id)
        if not self.visible_to(sh, user_id, ws_admin=ws_admin, my_groups=set(self.user_groups(ws, user_id))):
            raise _deny("作者没有向你公开这份内容")

    # ================================================================== 编辑人员标记
    def mark_edit(
        self,
        ws: str,
        object_type: str,
        object_id: str,
        *,
        user_id: str,
        user_name: str,
        action: str,
        version: str = "",
        parent_version: str = "",
        derived_from: str = "",
        note: str = "",
        actor_kind: str = "human",
    ) -> None:
        self.db.execute(
            "INSERT INTO collab_edits (workspace_id, object_type, object_id, user_id, user_name, actor_kind, action, "
            "version, parent_version, derived_from, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ws,
                object_type,
                object_id,
                user_id or "system",
                user_name[:60],
                actor_kind,
                action,
                version[:40],
                parent_version[:40],
                derived_from[:200],
                note[:300],
                now_ms(),
            ),
        )

    def edits(self, ws: str, object_type: str, object_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        if object_id:
            return self.db.fetchall(
                "SELECT * FROM collab_edits WHERE workspace_id=? AND object_type=? AND object_id=? ORDER BY id LIMIT ?",
                (ws, object_type, object_id, limit),
            )
        return self.db.fetchall(
            "SELECT * FROM collab_edits WHERE workspace_id=? AND object_type=? ORDER BY id LIMIT ?",
            (ws, object_type, limit * 10),
        )

    def editors(self, ws: str, object_type: str) -> dict[str, list[dict[str, Any]]]:
        """每个对象的编辑者：按最近编辑时间排序，含编辑次数，人与 Agent 分开标记。"""
        rows = self.db.fetchall(
            "SELECT object_id, user_id, user_name, actor_kind, COUNT(*) AS n, MAX(created_at) AS last_at "
            "FROM collab_edits WHERE workspace_id=? AND object_type=? AND action<>'backtest' "
            "GROUP BY object_id, user_id, user_name, actor_kind",
            (ws, object_type),
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            out.setdefault(r["object_id"], []).append(
                {
                    "user_id": r["user_id"],
                    "name": r["user_name"],
                    "actor_kind": r["actor_kind"],
                    "count": int(r["n"]),
                    "last_at": int(r["last_at"]),
                }
            )
        for v in out.values():
            v.sort(key=lambda e: -e["last_at"])
        return out

    # ================================================================== 问题报告
    def create_issue(
        self,
        *,
        ws: str | None,
        user_id: str,
        title: str,
        body: str,
        category: str,
        severity: str,
        attachments: list[str],
        context_info: dict[str, Any],
    ) -> str:
        title = title.strip()[:120]
        if len(title) < 2:
            raise _err("标题至少 2 个字")
        if category not in ISSUE_CATEGORIES:
            raise _err("未知的问题类型")
        if severity not in ISSUE_SEVERITY:
            raise _err("未知的严重程度")
        files = self._own_uploads(attachments, user_id)
        iid = "iss-" + uuid.uuid4().hex[:10]
        now = now_ms()
        self.db.execute(
            "INSERT INTO issue_reports (id, workspace_id, user_id, title, body, category, severity, status, "
            "attachments, context, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                iid,
                ws,
                user_id,
                title,
                body[:20000],
                category,
                severity,
                "open",
                jdump(files),
                jdump({k: str(v)[:300] for k, v in list((context_info or {}).items())[:12]}),
                now,
                now,
            ),
        )
        return iid

    def _own_uploads(self, ids: list[str], user_id: str) -> list[dict[str, Any]]:
        out = []
        for fid in list(dict.fromkeys(ids or []))[:10]:
            row = self.upload(fid)
            if row and row["user_id"] == user_id:
                out.append({"id": row["id"], "filename": row["filename"], "mime": row["mime"], "size": row["size"]})
        return out

    def issues(
        self,
        *,
        user_id: str | None = None,
        workspace_ids: list[str] | None = None,
        status: str | None = None,
        all_issues: bool = False,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM issue_reports WHERE 1=1"
        args: list[Any] = []
        if not all_issues:
            conds = []
            if user_id:
                conds.append("user_id=?")
                args.append(user_id)
            if workspace_ids:
                conds.append(f"workspace_id IN ({','.join('?' * len(workspace_ids))})")
                args.extend(workspace_ids)
            if not conds:
                return []
            sql += " AND (" + " OR ".join(conds) + ")"
        if status:
            sql += " AND status=?"
            args.append(status)
        rows = self.db.fetchall(sql + " ORDER BY updated_at DESC LIMIT ?", (*args, limit))
        names = self.names({r["user_id"] for r in rows})
        out = []
        for r in rows:
            n = int(self.db.scalar("SELECT COUNT(*) AS n FROM issue_replies WHERE issue_id=?", (r["id"],)) or 0)
            out.append(self._view_issue(r, names, replies=n))
        return out

    def _view_issue(self, r: dict[str, Any], names: dict[str, str], replies: int = 0) -> dict[str, Any]:
        return {
            **{
                k: r[k]
                for k in (
                    "id",
                    "workspace_id",
                    "user_id",
                    "title",
                    "body",
                    "category",
                    "severity",
                    "status",
                    "created_at",
                    "updated_at",
                )
            },
            "attachments": jload(r["attachments"], []),
            "context": jload(r["context"], {}),
            "user_name": names.get(r["user_id"], "?"),
            "category_label": ISSUE_CATEGORIES.get(r["category"], r["category"]),
            "status_label": ISSUE_STATUS.get(r["status"], r["status"]),
            "reply_count": replies,
        }

    def issue_row(self, iid: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM issue_reports WHERE id=?", (iid,))
        if row is None:
            raise _missing("问题报告不存在")
        return row

    def issue(self, iid: str) -> dict[str, Any]:
        row = self.issue_row(iid)
        replies = self.db.fetchall("SELECT * FROM issue_replies WHERE issue_id=? ORDER BY id", (iid,))
        view = self._view_issue(row, self.names([row["user_id"]]), replies=len(replies))
        view["replies"] = [
            {**r, "is_staff": bool(r["is_staff"]), "attachments": jload(r["attachments"], [])} for r in replies
        ]
        return view

    def reply_issue(
        self, iid: str, *, user_id: str, user_name: str, body: str, staff: bool, attachments: list[str]
    ) -> None:
        row = self.issue_row(iid)
        files = self._own_uploads(attachments, user_id)
        if not body.strip() and not files:
            raise _err("回复不能为空")
        now = now_ms()
        self.db.execute(
            "INSERT INTO issue_replies (issue_id, user_id, user_name, is_staff, body, attachments, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (iid, user_id, user_name[:60], 1 if staff else 0, body[:20000], jdump(files), now),
        )
        status = row["status"]
        if staff and status == "open":
            status = "replied"
        elif not staff and status in ("replied", "resolved"):
            status = "open"
        self.db.execute("UPDATE issue_reports SET status=?, updated_at=? WHERE id=?", (status, now, iid))
        if staff and row["user_id"] != user_id:
            self.notify(
                [row["user_id"]],
                kind="issue",
                title=f"管理员回复了你的问题「{row['title']}」",
                body=body[:200],
                link=f"issues/{iid}",
                actor_id=user_id,
                actor_name=user_name,
                source_type="issue",
                source_id=iid,
                workspace_id=row["workspace_id"],
                force=True,
            )

    def set_issue_status(self, iid: str, status: str, *, by: str, by_name: str) -> None:
        if status not in ISSUE_STATUS:
            raise _err("未知的状态")
        row = self.issue_row(iid)
        self.db.execute("UPDATE issue_reports SET status=?, updated_at=? WHERE id=?", (status, now_ms(), iid))
        if row["user_id"] != by:
            self.notify(
                [row["user_id"]],
                kind="issue",
                title=f"你的问题「{row['title']}」已标为{ISSUE_STATUS[status]}",
                link=f"issues/{iid}",
                actor_id=by,
                actor_name=by_name,
                source_type="issue",
                source_id=iid,
                workspace_id=row["workspace_id"],
                force=True,
            )

    def issue_counts(self, workspace_id: str | None = None) -> dict[str, int]:
        if workspace_id:
            rows = self.db.fetchall(
                "SELECT status, COUNT(*) AS n FROM issue_reports WHERE workspace_id=? GROUP BY status",
                (workspace_id,),
            )
        else:
            rows = self.db.fetchall("SELECT status, COUNT(*) AS n FROM issue_reports GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    # ================================================================== 上传
    @property
    def upload_dir(self) -> Path:
        return self.home / "uploads"

    def save_upload(
        self,
        *,
        ws: str | None,
        user_id: str,
        filename: str,
        mime: str,
        data: bytes,
        quota_guard: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        name = re.sub(r"[\\/\x00-\x1f]", "_", filename or "file").strip()[:120] or "file"
        ext = Path(name).suffix.lower()
        mime = (mime or "").split(";")[0].strip().lower()
        if mime not in UPLOAD_TYPES:
            mime = EXT_TYPES.get(ext, "")
        if mime not in UPLOAD_TYPES:
            raise _err("只支持图片（PNG / JPG / GIF / WebP）、PDF、文本、CSV、JSON、Python 与 ZIP 文件")
        if not data:
            raise _err("文件是空的")
        if len(data) > UPLOAD_MAX:
            raise _err("单个文件不能超过 10 MB")
        magic = _MAGIC.get(mime)
        if magic and not data.startswith(magic):
            raise _err("文件内容与类型不符")
        if mime.startswith("text/") or mime == "application/json":
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise _err("文本文件需要 UTF-8 编码") from exc
        fid = uuid.uuid4().hex
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        (self.upload_dir / fid).write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        thumb = self._make_thumbnail(fid, data, mime)
        thumb_bytes = int(thumb.get("thumb_size") or 0)
        pending_bytes = len(data) + thumb_bytes
        try:
            with self.db.tx() as t:
                used_bytes = 0
                if ws and user_id:
                    # PostgreSQL 下锁住成员行，串行化同一成员的上传前检查与写入；
                    # SQLite 的 BEGIN IMMEDIATE 已经提供同等的写串行化。
                    if self.db.dialect == "postgres":
                        t.execute(
                            "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=? FOR UPDATE",
                            (ws, user_id),
                        )
                    used_bytes = int(
                        t.scalar(
                            "SELECT COALESCE(SUM(size + COALESCE(thumb_size, 0)), 0) AS n "
                            "FROM uploads WHERE workspace_id=? AND user_id=?",
                            (ws, user_id),
                        )
                        or 0
                    )
                if quota_guard is not None:
                    quota_guard(used_bytes, pending_bytes)
                t.execute(
                    "INSERT INTO uploads (id, workspace_id, user_id, filename, mime, size, sha256, created_at, "
                    "thumb_mime, thumb_size, width, height) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fid,
                        ws,
                        user_id,
                        name,
                        mime,
                        len(data),
                        sha,
                        now_ms(),
                        thumb.get("thumb_mime"),
                        thumb.get("thumb_size"),
                        thumb.get("width"),
                        thumb.get("height"),
                    ),
                )
        except BaseException:
            # 配额拒绝或数据库写入失败时，不留下原文件 / 缩略图。
            for target in (self.upload_dir / fid, self.upload_dir / f"thumb-{fid}.webp"):
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        return {
            "id": fid,
            "filename": name,
            "mime": mime,
            "size": len(data),
            "thumb_url": f"/api/uploads/{fid}/thumb" if thumb else None,
            **thumb,
        }

    def _make_thumbnail(self, fid: str, data: bytes, mime: str) -> dict[str, Any]:
        """上传时生成最长边 640px 的 WebP 缩略图；原图完整保留。"""
        if not mime.startswith("image/") or mime in ("image/svg+xml", "image/gif"):
            return {}
        try:
            from PIL import Image, ImageOps
        except ImportError:
            return {}
        try:
            with Image.open(io.BytesIO(data)) as src:
                src.seek(0)
                im = ImageOps.exif_transpose(src)
                width, height = im.size
                im.thumbnail((640, 640), Image.Resampling.LANCZOS)
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
                target = self.upload_dir / f"thumb-{fid}.webp"
                im.save(target, "WEBP", quality=82, method=4)
            return {
                "thumb_mime": "image/webp",
                "thumb_size": target.stat().st_size,
                "width": width,
                "height": height,
            }
        except Exception:
            return {}

    def upload(self, fid: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", fid or ""):
            return None
        return self.db.fetchone("SELECT * FROM uploads WHERE id=?", (fid,))

    def upload_file(self, fid: str) -> Path:
        return self.upload_dir / fid

    def upload_thumb_file(self, fid: str) -> Path:
        return self.upload_dir / f"thumb-{fid}.webp"

    def storage_stats(self, ws: str, user_id: str | None = None) -> dict[str, Any]:
        """当前工作区或成员的上传存储统计（含缩略图）。"""
        return upload_storage(self.db, ws, user_id)

    def storage_bytes(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(SUM(size + COALESCE(thumb_size, 0)), 0) AS n FROM uploads") or 0)
