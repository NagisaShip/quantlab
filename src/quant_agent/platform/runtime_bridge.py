"""Single-direction bridge from a QuantLab ingress ticket to a Runtime run.

Only the Runtime decides execution state. QuantLab's runs row is a compatible,
possibly delayed projection; runtime_event_seq records its source cursor.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from .db import IntegrityError, jdump, now_ms
from .errors import AppError, ErrorCode
from .runs import ACTIVE, RunService


class RuntimePort(Protocol):
    def start(self, prompt: str, spec: Any, *, run_id: str, tenant_id: str) -> str: ...

    def get(self, run_id: str) -> dict[str, Any] | None: ...

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]: ...

    def signal(self, run_id: str, type_: str, data: dict[str, Any], *, signal_id: str, actor: str) -> bool: ...


_PROJECTED_STATES = {
    "QUEUED": "QUEUED",
    "RUNNING": "RUNNING",
    "WAITING": "PAUSED",
    "RECOVERING": "RECOVERING",
    "CANCELING": "PAUSED",
    "COMPLETED": "COMPLETED",
    "FAILED": "FAILED",
    "CANCELED": "CANCELED",
}


class RuntimeBridge:
    def __init__(self, runs: RunService, runtime: RuntimePort, *, spec_loader: Any, version: str = "0.1.0") -> None:
        self.runs = runs
        self.runtime = runtime
        self.spec_loader = spec_loader
        self.version = version

    def create(
        self, *, workspace_id: str, user_id: str, title: str, prompt: str, spec: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise ValueError("runtime ingress requires an idempotency key")
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        try:
            with self.runs.db.tx() as tx:
                # Serialize admission and idempotency for one workspace. SQLite
                # already holds BEGIN IMMEDIATE; PG needs an explicit row lock.
                workspace = tx.fetchone(
                    "SELECT settings FROM workspaces WHERE id=?"
                    + (" FOR UPDATE" if self.runs.db.dialect == "postgres" else ""),
                    (workspace_id,),
                )
                if workspace is None:
                    raise AppError(ErrorCode.NOT_FOUND, "工作区不存在")
                existing = tx.fetchone(
                    "SELECT r.id,r.kind,r.runtime_version,t.prompt,t.spec FROM runs r "
                    "LEFT JOIN runtime_tickets t ON t.run_id=r.id WHERE r.workspace_id=? AND r.idempotency_key=?",
                    (workspace_id, idempotency_key),
                )
                if existing:
                    self._check_existing(existing, prompt, spec)
                    run_id = existing["id"]
                else:
                    self.spec_loader(spec)
                    metadata = spec.get("metadata", {})
                    if not isinstance(metadata, dict):
                        raise ValueError("Agent metadata must be an object")
                    priority = metadata.get("dispatch_priority", 0)
                    if type(priority) is not int or not -100 <= priority <= 100:
                        raise ValueError("dispatch_priority must be an integer between -100 and 100")
                    stamp = now_ms()
                    quotas = json.loads(workspace["settings"] or "{}").get("quotas") or {}
                    active = int(
                        tx.scalar(
                            "SELECT COUNT(*) FROM runs WHERE workspace_id=? AND state IN ("
                            + ",".join("?" for _ in ACTIVE)
                            + ")",
                            (workspace_id, *ACTIVE),
                        )
                        or 0
                    )
                    active_limit = int(quotas.get("max_concurrent_runs", 4))
                    if active >= active_limit:
                        raise AppError(
                            ErrorCode.QUOTA_EXHAUSTED,
                            "工作区同时运行的任务已达上限",
                            details={"active": active, "limit": active_limit},
                        )
                    pending = int(
                        tx.scalar(
                            "SELECT COUNT(*) FROM runtime_tickets t JOIN runs r ON r.id=t.run_id "
                            "WHERE r.workspace_id=? AND r.state IN (" + ",".join("?" for _ in ACTIVE) + ")",
                            (workspace_id, *ACTIVE),
                        )
                        or 0
                    )
                    pending_limit = int(quotas.get("max_pending_runtime_runs", 128))
                    if pending >= pending_limit:
                        raise AppError(
                            ErrorCode.QUOTA_EXHAUSTED,
                            "Agent 等待队列已满，请等待完成或取消后重试",
                            details={"pending": pending, "limit": pending_limit},
                        )
                    today = int(
                        tx.scalar(
                            "SELECT COUNT(*) FROM runs WHERE workspace_id=? AND created_at>=?",
                            (workspace_id, stamp // 86400000 * 86400000),
                        )
                        or 0
                    )
                    daily_limit = int(quotas.get("max_runs_per_day", 200))
                    if today >= daily_limit:
                        raise AppError(
                            ErrorCode.QUOTA_EXHAUSTED,
                            "今日运行次数已达上限",
                            details={"today": today, "limit": daily_limit},
                        )
                    tx.execute(
                        "INSERT INTO runs(id,workspace_id,created_by,mode,kind,state,title,manifest,"
                        "idempotency_key,created_at,updated_at,runtime_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            workspace_id,
                            user_id,
                            "agent",
                            "runtime_agent",
                            "QUEUED",
                            title[:200],
                            jdump({"runtime_version": self.version}),
                            idempotency_key,
                            stamp,
                            stamp,
                            self.version,
                        ),
                    )
                    tx.execute(
                        "INSERT INTO runtime_tickets(run_id,runtime_version,prompt,spec,status,created_at) VALUES(?,?,?,?,?,?)",
                        (run_id, self.version, prompt, jdump(spec), "PENDING", stamp),
                    )
                    tx.insert_id(
                        "INSERT INTO run_events(run_id,workspace_id,ts,type,data) VALUES(?,?,?,?,?)",
                        (run_id, workspace_id, stamp, "state", jdump({"state": "QUEUED"})),
                    )
        except IntegrityError:
            existing = self.runs.db.fetchone(
                "SELECT r.id,r.kind,r.runtime_version,t.prompt,t.spec FROM runs r "
                "LEFT JOIN runtime_tickets t ON t.run_id=r.id WHERE r.workspace_id=? AND r.idempotency_key=?",
                (workspace_id, idempotency_key),
            )
            if not existing:
                raise
            self._check_existing(existing, prompt, spec)
            run_id = existing["id"]
        self.runs._wake()
        return self.runs.get(run_id, workspace_id=workspace_id)

    def _check_existing(self, existing: dict, prompt: str, spec: dict) -> None:
        if existing["kind"] != "runtime_agent" or existing["runtime_version"] != self.version:
            raise ValueError("idempotency key belongs to a different run kind or version")
        if existing["prompt"] != prompt or existing["spec"] != jdump(spec):
            raise ValueError("idempotency key belongs to different runtime input")

    def deliver(self, run_id: str) -> bool:
        ticket = self.runs.db.fetchone(
            "SELECT t.*,r.workspace_id FROM runtime_tickets t JOIN runs r ON r.id=t.run_id WHERE t.run_id=?", (run_id,)
        )
        if ticket is None:
            raise KeyError(run_id)
        existing = self.runtime.get(run_id)
        if existing is None:
            try:
                self.runtime.start(
                    ticket["prompt"],
                    self.spec_loader(json.loads(ticket["spec"])),
                    run_id=run_id,
                    tenant_id=ticket["workspace_id"],
                )
            except Exception:
                # Another process may have delivered the same ticket meanwhile.
                existing = self.runtime.get(run_id)
                if existing is None:
                    raise
        current = self.runtime.get(run_id)
        if current is None or current["tenant_id"] != ticket["workspace_id"]:
            raise ValueError("runtime run tenant differs from ingress ticket")
        self.runs.db.execute(
            "UPDATE runtime_tickets SET status='DELIVERED',delivered_at=COALESCE(delivered_at,?) WHERE run_id=?",
            (now_ms(), run_id),
        )
        self.project(run_id)
        return existing is None

    def project(self, run_id: str) -> dict[str, Any]:
        current = self.runtime.get(run_id)
        if current is None:
            raise KeyError(run_id)
        runtime_seq = int(current["seq"])
        with self.runs.db.tx() as tx:
            row = tx.fetchone(
                "SELECT workspace_id,state,runtime_event_seq FROM runs WHERE id=? AND runtime_version=?",
                (run_id, self.version),
            )
            if row is None:
                raise KeyError(run_id)
            if runtime_seq <= row["runtime_event_seq"]:
                return self.runs.get(run_id)
            events = [
                event
                for event in self.runtime.events(run_id, after=row["runtime_event_seq"])
                if event["seq"] <= runtime_seq
            ]
            if [event["seq"] for event in events] != list(range(row["runtime_event_seq"] + 1, runtime_seq + 1)):
                raise ValueError("runtime event stream is incomplete")
            for event in events:
                tx.insert_id(
                    "INSERT INTO run_events(run_id,workspace_id,ts,type,data) VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        row["workspace_id"],
                        event["created_at"],
                        "runtime." + event["type"],
                        jdump({"runtime_seq": event["seq"], **event["data"]}),
                    ),
                )
            state = _PROJECTED_STATES[current["state"]]
            summary = {"runtime_state": current["state"], "runtime_event_seq": runtime_seq}
            if current["state"] == "COMPLETED":
                summary["output"] = current["output"]
            tx.execute(
                "UPDATE runs SET state=?,runtime_event_seq=?,summary=?,error=?,updated_at=?,"
                "ended_at=CASE WHEN ? IN ('COMPLETED','FAILED','CANCELED') THEN ? ELSE ended_at END "
                "WHERE id=? AND runtime_event_seq<?",
                (state, runtime_seq, jdump(summary), current["error"], now_ms(), state, now_ms(), run_id, runtime_seq),
            )
        return self.runs.get(run_id)

    def strong_get(self, run_id: str, *, workspace_id: str) -> dict[str, Any]:
        projected = self.runs.require(run_id, workspace_id)
        current = self.runtime.get(run_id)
        if current is None:
            return projected
        projected["state"] = _PROJECTED_STATES[current["state"]]
        projected["runtime_state"] = current["state"]
        projected["runtime_event_seq"] = current["seq"]
        return projected

    def project_active(self, *, workspace_id: str | None = None, limit: int = 200) -> int:
        """Refresh eventually consistent list views without granting execution rights."""
        sql = (
            "SELECT r.id FROM runs r JOIN runtime_tickets t ON t.run_id=r.id "
            "WHERE t.status='DELIVERED' AND r.state NOT IN ('COMPLETED','FAILED','CANCELED')"
        )
        params: list[Any] = []
        if workspace_id is not None:
            sql += " AND r.workspace_id=?"
            params.append(workspace_id)
        sql += " ORDER BY r.created_at,r.id LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        rows = self.runs.db.fetchall(sql, params)
        for row in rows:
            self.project(row["id"])
        return len(rows)

    def cancel(self, run_id: str, *, workspace_id: str, actor: str, signal_id: str) -> bool:
        self.runs.require(run_id, workspace_id)
        current = self.runtime.get(run_id)
        if current is None:
            raise AppError(ErrorCode.UPSTREAM_UNAVAILABLE, "Agent Runtime 尚未接收该运行")
        if current["state"] in {"COMPLETED", "FAILED", "CANCELED"}:
            raise AppError(ErrorCode.CONFLICT, "该 Agent 运行已结束")
        changed = self.runtime.signal(run_id, "cancel", {}, signal_id=signal_id, actor=actor)
        self.project(run_id)
        return changed
