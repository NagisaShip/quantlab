"""审计日志。

记录谁、在哪个工作区、什么时候、对什么对象做了什么。details 在写入前统一过滤：
任何看起来像凭证的键（key、secret、token、password、passphrase）都被替换为掩码，
保证审计、导出与错误信息里不出现明文凭证。
"""

from __future__ import annotations

import re
from typing import Any

from . import context
from .db import Database, jdump, jload, now_ms

_SENSITIVE = re.compile(r"(secret|password|passphrase|token|api_?key|private|signature)", re.IGNORECASE)


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if _SENSITIVE.search(str(k)) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "…"
    return value


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        action: str,
        *,
        object_type: str = "",
        object_id: str = "",
        details: dict[str, Any] | None = None,
        workspace_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        ctx = context.current()
        try:
            self.db.execute(
                "INSERT INTO audit_log (ts, workspace_id, user_id, action, object_type, object_id, trace_id, ip, detai"
                "ls) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    now_ms(),
                    workspace_id if workspace_id is not None else ctx.workspace_id,
                    user_id if user_id is not None else ctx.user_id,
                    action,
                    object_type,
                    str(object_id)[:120],
                    ctx.trace_id,
                    ctx.ip,
                    jdump(scrub(details or {})),
                ),
            )
        except Exception:
            # 审计写入失败不应让业务请求失败；但要能在日志里看到
            import logging

            logging.getLogger("quantlab.audit").exception("审计写入失败：%s", action)

    def list(
        self,
        workspace_id: str,
        *,
        limit: int = 200,
        action: str | None = None,
        before_id: int | None = None,
        since: int | None = None,
        until: int | None = None,
        user_id: str | None = None,
        q: str | None = None,
    ) -> list[dict]:
        sql = (
            "SELECT a.id, a.ts, a.action, a.object_type, a.object_id, a.trace_id, a.ip, a.details, a.user_id, u.name A"
            "S user_name "
            "FROM audit_log a LEFT JOIN users u ON u.id = a.user_id WHERE a.workspace_id=?"
        )
        params: list[Any] = [workspace_id]
        if action:
            sql += " AND a.action LIKE ?"
            params.append(f"{action}%")
        if before_id:
            sql += " AND a.id < ?"
            params.append(before_id)
        # 时间范围用闭开区间 [since, until)，避免边界那一毫秒被算进两天
        if since is not None:
            sql += " AND a.ts >= ?"
            params.append(int(since))
        if until is not None:
            sql += " AND a.ts < ?"
            params.append(int(until))
        if user_id:
            sql += " AND a.user_id = ?"
            params.append(user_id)
        term = (q or "").strip().lower()
        if term:
            like = f"%{term}%"
            sql += (
                " AND (LOWER(a.action) LIKE ? OR LOWER(a.object_type) LIKE ? OR LOWER(a.object_id) LIKE ?"
                " OR LOWER(a.trace_id) LIKE ? OR LOWER(a.ip) LIKE ? OR LOWER(COALESCE(a.details, '')) LIKE ?"
                " OR LOWER(COALESCE(u.name, '')) LIKE ? OR LOWER(COALESCE(a.user_id, '')) LIKE ?)"
            )
            params.extend([like] * 8)
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(min(limit, 1000))
        rows = self.db.fetchall(sql, params)
        for r in rows:
            r["details"] = jload(r["details"], {})
        return rows

    # ------------------------------------------------------------------ 导出
    EXPORT_COLUMNS = [
        "id",
        "ts",
        "time",
        "action",
        "object_type",
        "object_id",
        "user_id",
        "user_name",
        "ip",
        "trace_id",
        "details",
    ]

    def export_rows(self, workspace_id: str, **filters: Any) -> list[dict]:
        """导出用：把 details 压成单行 JSON 文本，方便进 CSV 单元格。

        导出走的是同一套筛选条件，但**不分页**——导出的意义就是拿到完整区间，
        只给前 200 条会让人误以为审计日志只有这么点。
        """
        rows = self.list(workspace_id, limit=1000, **filters)
        out: list[dict] = []
        for r in rows:
            out.append(
                {
                    **r,
                    "time": _iso(r["ts"]),
                    "details": jdump(r.get("details") or {}),
                }
            )
        return out

    def export_csv(self, workspace_id: str, **filters: Any) -> str:
        """导出 CSV。经 csvsafe 中和，防止 `=` 开头的字段在表格软件里被当公式执行。"""
        from .csvsafe import to_csv

        return to_csv(self.export_rows(workspace_id, **filters), self.EXPORT_COLUMNS)

    # ------------------------------------------------------------------ 保留策略
    def archive_expired(self, workspace_id: str, *, retention_days: int, now: int | None = None) -> dict[str, Any]:
        """超过保留期的记录移到归档表，而不是直接删掉。

        审计日志是"谁做了什么"的证据，直接 DELETE 会让历史无法追溯；
        归档后主表变小、日常查询变快，需要时仍能从归档里查回来。
        """
        if retention_days <= 0:
            raise ValueError("保留天数必须为正")
        cutoff = (now if now is not None else now_ms()) - retention_days * 86_400_000
        rows = self.db.fetchall(
            "SELECT * FROM audit_log WHERE workspace_id=? AND ts < ? ORDER BY id LIMIT 5000",
            (workspace_id, cutoff),
        )
        if not rows:
            return {"archived": 0, "cutoff": cutoff}
        for r in rows:
            self.db.execute(
                "INSERT INTO audit_archive (id, ts, workspace_id, user_id, action, object_type, object_id, trace_id, "
                "ip, details, archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["id"],
                    r["ts"],
                    r["workspace_id"],
                    r["user_id"],
                    r["action"],
                    r["object_type"],
                    r["object_id"],
                    r["trace_id"],
                    r["ip"],
                    r["details"],
                    now_ms(),
                ),
            )
        ids = [r["id"] for r in rows]
        marks = ",".join("?" for _ in ids)
        self.db.execute(f"DELETE FROM audit_log WHERE workspace_id=? AND id IN ({marks})", [workspace_id, *ids])
        return {"archived": len(rows), "cutoff": cutoff}

    def list_archived(self, workspace_id: str, *, limit: int = 200) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT id, ts, action, object_type, object_id, user_id, trace_id, ip, details, archived_at "
            "FROM audit_archive WHERE workspace_id=? ORDER BY id DESC LIMIT ?",
            (workspace_id, min(limit, 1000)),
        )
        for r in rows:
            r["details"] = jload(r["details"], {})
        return rows


def _iso(ts: Any) -> str:
    """毫秒时间戳 → UTC ISO 字符串。导出给人看，可读性比紧凑重要。"""
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(int(ts) / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""
