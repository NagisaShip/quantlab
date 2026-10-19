"""运行（Run）队列：控制面只登记任务与命令，长任务由独立 worker 认领执行。

状态机（持久化在 runs.state）::

    QUEUED ──claim──▶ RUNNING ──完成──▶ COMPLETED
       │                │  ├──异常──▶ FAILED
       │                │  ├──cancel / liquidate_and_stop──▶ CANCELED / COMPLETED
       │                │  └──pause──▶ PAUSED ──resume──▶ RUNNING
       │                └──租约过期（worker 崩溃）──▶ RECOVERING ──claim──▶ RUNNING（从检查点继续）
       └──cancel──▶ CANCELED

- **单租约**：同一时刻只有一个 worker 持有某个 run 的租约（lease_owner + lease_expires_at）。
  认领用条件更新实现，PostgreSQL 下加 `FOR UPDATE SKIP LOCKED`，多 worker 并发也不会重复认领。
- **租约围栏**：心跳、写检查点、写事件、结束都带上 lease_owner 条件；租约已被别人接手的旧 worker
  写不进去（返回 False），从而不会出现两个 worker 同时推进一个 run。
- **检查点**：worker 每处理完一个事件就把运行时快照与 last_seq 一起写入；崩溃后新 worker 从检查点恢复，
  已处理的事件（按序号/时间）与信号（按确定性 signal_id）不会再成交一次。
- **命令**：pause / resume / cancel / liquidate_and_stop / tick 以带幂等键的命令行写入 run_commands，
  worker 在事件之间按顺序应用并回写结果。
"""

from __future__ import annotations

import os
import socket
import uuid
from typing import Any

from .db import Database, IntegrityError, dec, jdump, jload, now_ms
from .errors import AppError, ErrorCode

QUEUED, RUNNING, PAUSED, COMPLETED, FAILED, CANCELED, RECOVERING = (
    "QUEUED",
    "RUNNING",
    "PAUSED",
    "COMPLETED",
    "FAILED",
    "CANCELED",
    "RECOVERING",
)
ACTIVE = (QUEUED, RUNNING, PAUSED, RECOVERING)
TERMINAL = (COMPLETED, FAILED, CANCELED)
COMMANDS = ("pause", "resume", "cancel", "liquidate_and_stop", "tick", "unhalt")
LEASE_MS = int(os.environ.get("QAGENT_LEASE_MS", "60000"))

#: 允许的运行模式（v2.0）。模式决定这条 run 的数据属于"仿真"还是"真实"，
#: 靠约定字符串判断会在拼写错误时静默把真实数据当成模拟数据，所以在这里显式校验。
MODES = (
    "backtest",  # 历史回测
    "paper",  # 实时模拟（虚拟资金）
    "testnet",  # 测试网真实成交（标为"测试"，不算实盘）
    "imported_live",  # 导入的真实成交
    "readonly_live",  # 只读同步的真实成交
    "agent",  # Agent 协作任务（不产生成交）
    "analysis",  # 只读分析（不产生成交）
    "micro_live",  # 微实盘：真实资金、小额上限（见 live/micro.py）
)

#: 属于"真实结果"的模式（testnet 单独一档，不计入实盘）
REAL_MODES = ("imported_live", "readonly_live", "micro_live")


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class RunService:
    def __init__(self, db: Database, *, wake: Any = None) -> None:
        self.db = db
        self.wake = wake  # 可选：Redis 唤醒器（有新任务/命令时立即叫醒 worker）

    # ------------------------------------------------------------------ 登记
    def create(
        self,
        *,
        workspace_id: str,
        user_id: str,
        kind: str,
        mode: str,
        title: str = "",
        manifest: dict[str, Any] | None = None,
        strategy_version_id: str | None = None,
        idempotency_key: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        rid = run_id or f"run-{uuid.uuid4().hex[:16]}"
        now = now_ms()
        if mode not in MODES:
            raise ValueError(f"未知的运行模式 {mode!r}；允许的模式：{'、'.join(MODES)}")
        try:
            self.db.execute(
                "INSERT INTO runs (id, workspace_id, created_by, mode, kind, state, title, strategy_version_id, manife"
                "st, "
                "idempotency_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    workspace_id,
                    user_id,
                    mode,
                    kind,
                    QUEUED,
                    title[:200],
                    strategy_version_id,
                    jdump(manifest or {}),
                    idempotency_key,
                    now,
                    now,
                ),
            )
        except IntegrityError:
            if idempotency_key:
                row = self.db.fetchone(
                    "SELECT * FROM runs WHERE workspace_id=? AND idempotency_key=?", (workspace_id, idempotency_key)
                )
                if row:
                    return self._out(row)
            raise
        self.event(rid, "state", {"state": QUEUED}, workspace_id=workspace_id)
        self._wake()
        return self.get(rid, workspace_id=workspace_id)

    def _wake(self) -> None:
        if self.wake is not None:
            try:
                self.wake.notify()
            except Exception:
                pass

    # ------------------------------------------------------------------ 查询
    @staticmethod
    def _out(row: dict[str, Any], *, checkpoint: bool = False) -> dict[str, Any]:
        out = dict(row)
        out["manifest"] = jload(row.get("manifest"), {})
        out["summary"] = jload(row.get("summary"), {})
        if checkpoint:
            out["checkpoint"] = jload(row.get("checkpoint"), None)
        else:
            out.pop("checkpoint", None)
        out["progress"] = {"done": row.get("progress_done") or 0, "total": row.get("progress_total") or 0}
        return out

    def get(self, run_id: str, *, workspace_id: str | None = None, checkpoint: bool = False) -> dict[str, Any] | None:
        if workspace_id:
            row = self.db.fetchone("SELECT * FROM runs WHERE id=? AND workspace_id=?", (run_id, workspace_id))
        else:
            row = self.db.fetchone("SELECT * FROM runs WHERE id=?", (run_id,))
        return self._out(row, checkpoint=checkpoint) if row else None

    def require(self, run_id: str, workspace_id: str) -> dict[str, Any]:
        r = self.get(run_id, workspace_id=workspace_id)
        if r is None:
            raise AppError(ErrorCode.NOT_FOUND, "运行不存在")
        return r

    def list(
        self,
        workspace_id: str,
        *,
        kind: str | None = None,
        mode: str | None = None,
        state: str | None = None,
        q: str | None = None,
        before_created_at: int | None = None,
        before_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs WHERE workspace_id=?"
        params: list[Any] = [workspace_id]
        for col, val in (("kind", kind), ("mode", mode), ("state", state)):
            if val:
                sql += f" AND {col}=?"
                params.append(val)
        term = (q or "").strip().lower()
        if term:
            like = f"%{term}%"
            sql += (
                " AND (LOWER(id) LIKE ? OR LOWER(title) LIKE ? OR LOWER(kind) LIKE ? OR LOWER(mode) LIKE ?"
                " OR LOWER(COALESCE(error, '')) LIKE ?)"
            )
            params.extend([like] * 5)
        if before_created_at is not None and before_id:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            params.extend([int(before_created_at), int(before_created_at), before_id])
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(min(limit, 501))
        return [self._out(r) for r in self.db.fetchall(sql, params)]

    def count_active(self, workspace_id: str, *, kind: str | None = None) -> int:
        sql = f"SELECT COUNT(*) FROM runs WHERE workspace_id=? AND state IN ({','.join('?' * len(ACTIVE))})"
        params: list[Any] = [workspace_id, *ACTIVE]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        return int(self.db.scalar(sql, params) or 0)

    def count_since(self, workspace_id: str, since_ms: int) -> int:
        return int(
            self.db.scalar("SELECT COUNT(*) FROM runs WHERE workspace_id=? AND created_at>=?", (workspace_id, since_ms))
            or 0
        )

    # ------------------------------------------------------------------ 事件流
    def event(
        self,
        run_id: str,
        type_: str,
        data: dict[str, Any] | None = None,
        *,
        workspace_id: str | None = None,
        owner: str | None = None,
        ts: int | None = None,
    ) -> int | None:
        """追加一条运行事件；带 owner 时校验租约（围栏）。返回事件游标 id。"""
        with self.db.tx() as t:
            if owner is not None:
                ok = t.fetchone("SELECT workspace_id FROM runs WHERE id=? AND lease_owner=?", (run_id, owner))
                if not ok:
                    return None
                workspace_id = ok["workspace_id"]
            if workspace_id is None:
                row = t.fetchone("SELECT workspace_id FROM runs WHERE id=?", (run_id,))
                workspace_id = row["workspace_id"] if row else ""
            return t.insert_id(
                "INSERT INTO run_events (run_id, workspace_id, ts, type, data) VALUES (?,?,?,?,?)",
                (run_id, workspace_id, ts or now_ms(), type_, jdump(data or {})),
            )

    def events(self, run_id: str, *, after: int = 0, limit: int = 500, types: list[str] | None = None) -> list[dict]:
        sql = "SELECT id, ts, type, data FROM run_events WHERE run_id=? AND id>?"
        params: list[Any] = [run_id, after]
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params += types
        sql += " ORDER BY id LIMIT ?"
        params.append(min(limit, 5000))
        rows = self.db.fetchall(sql, params)
        for r in rows:
            r["data"] = jload(r["data"], {})
        return rows

    # ------------------------------------------------------------------ 命令
    def command(
        self, run_id: str, workspace_id: str, user_id: str, command: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        if command not in COMMANDS:
            raise AppError(ErrorCode.INVALID_PARAMETER, f"未知命令：{command}；可选 {', '.join(COMMANDS)}")
        run = self.require(run_id, workspace_id)
        if run["state"] in TERMINAL:
            raise AppError(
                ErrorCode.CONFLICT, f"运行已结束（{run['state']}），不能再发送命令", details={"state": run["state"]}
            )
        if command == "pause" and run["state"] == PAUSED:
            raise AppError(ErrorCode.CONFLICT, "运行已经处于暂停状态")
        if command == "resume" and run["state"] != PAUSED:
            raise AppError(ErrorCode.CONFLICT, "只有暂停中的运行才能恢复")
        cid = f"cmd-{uuid.uuid4().hex[:12]}"
        try:
            self.db.execute(
                "INSERT INTO run_commands (id, run_id, workspace_id, command, status, idempotency_key, created_by, cre"
                "ated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, run_id, workspace_id, command, "pending", idempotency_key, user_id, now_ms()),
            )
        except IntegrityError:
            row = self.db.fetchone(
                "SELECT * FROM run_commands WHERE run_id=? AND idempotency_key=?", (run_id, idempotency_key)
            )
            if row and row["command"] != command:
                raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "该幂等键已用于另一个命令") from None
            return dict(row or {})
        # 还在排队的任务可以直接取消，不必等 worker
        if command == "cancel" and run["state"] == QUEUED:
            self.db.execute(
                "UPDATE runs SET state=?, ended_at=?, updated_at=? WHERE id=? AND state=?",
                (CANCELED, now_ms(), now_ms(), run_id, QUEUED),
            )
            self.db.execute(
                "UPDATE run_commands SET status='applied', applied_at=?, message=? WHERE id=?",
                (now_ms(), "排队中直接取消", cid),
            )
            self.event(run_id, "state", {"state": CANCELED, "by": "command"}, workspace_id=workspace_id)
        self.event(run_id, "command", {"command": command, "id": cid}, workspace_id=workspace_id)
        self._wake()
        return dict(self.db.fetchone("SELECT * FROM run_commands WHERE id=?", (cid,)) or {})

    def pending_commands(self, run_id: str) -> list[dict[str, Any]]:
        return self.db.fetchall(
            "SELECT * FROM run_commands WHERE run_id=? AND status='pending' ORDER BY created_at, id", (run_id,)
        )

    def ack_command(self, command_id: str, *, status: str = "applied", message: str = "") -> None:
        self.db.execute(
            "UPDATE run_commands SET status=?, message=?, applied_at=? WHERE id=?",
            (status, message[:500], now_ms(), command_id),
        )

    def commands(self, run_id: str) -> list[dict[str, Any]]:
        return self.db.fetchall("SELECT * FROM run_commands WHERE run_id=? ORDER BY created_at, id", (run_id,))

    # ------------------------------------------------------------------ worker 侧
    def recover_expired(self) -> int:
        """把租约过期的 RUNNING/PAUSED 标为 RECOVERING（worker 崩溃或失联）。"""
        now = now_ms()
        rows = self.db.fetchall(
            "SELECT id, workspace_id, state, lease_owner FROM runs WHERE state IN (?,?) AND lease_expires_at IS NOT NU"
            "LL "
            "AND lease_expires_at < ?",
            (RUNNING, PAUSED, now),
        )
        n = 0
        for r in rows:
            changed = self.db.execute(
                "UPDATE runs SET state=?, updated_at=? WHERE id=? AND state=? AND lease_expires_at < ?",
                (RECOVERING, now, r["id"], r["state"], now),
            )
            if changed:
                n += 1
                self.event(
                    r["id"],
                    "state",
                    {"state": RECOVERING, "previous": r["state"], "lost_owner": r["lease_owner"]},
                    workspace_id=r["workspace_id"],
                )
        return n

    def claim(self, owner: str, *, kinds: list[str] | None = None, lease_ms: int = LEASE_MS) -> dict[str, Any] | None:
        """原子认领一个可执行的 run（QUEUED 或 RECOVERING）。"""
        self.recover_expired()
        now = now_ms()
        kind_sql = f" AND kind IN ({','.join('?' * len(kinds))})" if kinds else ""
        lock = " FOR UPDATE SKIP LOCKED" if self.db.dialect == "postgres" else ""
        with self.db.tx() as t:
            row = t.fetchone(
                f"SELECT id, state, workspace_id FROM runs WHERE state IN (?,?){kind_sql} ORDER BY created_at, id LIMIT "
                f"1{lock}",
                [QUEUED, RECOVERING, *(kinds or [])],
            )
            if not row:
                return None
            # RECOVERING 恢复成它中断前的语义：暂停中的继续暂停
            prev_paused = False
            if row["state"] == RECOVERING:
                last = t.fetchone(
                    "SELECT data FROM run_events WHERE run_id=? AND type='state' ORDER BY id DESC LIMIT 1", (row["id"],)
                )
                prev_paused = bool(last and jload(last["data"], {}).get("previous") == PAUSED)
            new_state = PAUSED if prev_paused else RUNNING
            changed = t.execute(
                "UPDATE runs SET state=?, lease_owner=?, lease_expires_at=?, attempts=attempts+1, "
                "started_at=COALESCE(started_at, ?), updated_at=? WHERE id=? AND state=?",
                (new_state, owner, now + lease_ms, now, now, row["id"], row["state"]),
            )
            if not changed:
                return None
        self.event(
            row["id"],
            "state",
            {"state": new_state, "owner": owner, "recovered": row["state"] == RECOVERING},
            workspace_id=row["workspace_id"],
        )
        return self.get(row["id"], checkpoint=True)

    def heartbeat(self, run_id: str, owner: str, *, lease_ms: int = LEASE_MS) -> bool:
        now = now_ms()
        return bool(
            self.db.execute(
                "UPDATE runs SET lease_expires_at=?, updated_at=? WHERE id=? AND lease_owner=? AND state IN (?,?)",
                (now + lease_ms, now, run_id, owner, RUNNING, PAUSED),
            )
        )

    def checkpoint(
        self,
        run_id: str,
        owner: str,
        checkpoint: dict[str, Any],
        *,
        last_seq: int | None = None,
        progress: tuple[int, int] | None = None,
        summary: dict[str, Any] | None = None,
        lease_ms: int = LEASE_MS,
    ) -> bool:
        now = now_ms()
        sets = ["checkpoint=?", "lease_expires_at=?", "updated_at=?"]
        params: list[Any] = [jdump(checkpoint), now + lease_ms, now]
        if last_seq is not None:
            sets.append("last_seq=?")
            params.append(last_seq)
        if progress is not None:
            sets += ["progress_done=?", "progress_total=?"]
            params += [progress[0], progress[1]]
        if summary is not None:
            sets.append("summary=?")
            params.append(jdump(summary))
        params += [run_id, owner]
        return bool(self.db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=? AND lease_owner=?", params))

    def progress(self, run_id: str, owner: str, done: int, total: int) -> bool:
        now = now_ms()
        return bool(
            self.db.execute(
                "UPDATE runs SET progress_done=?, progress_total=?, lease_expires_at=?, updated_at=? WHERE id=? AND le"
                "ase_owner=?",
                (done, total, now + LEASE_MS, now, run_id, owner),
            )
        )

    def set_state(
        self,
        run_id: str,
        owner: str,
        state: str,
        *,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        now = now_ms()
        sets = ["state=?", "updated_at=?"]
        params: list[Any] = [state, now]
        if state in TERMINAL:
            sets += ["ended_at=?", "lease_owner=NULL", "lease_expires_at=NULL"]
            params.append(now)
        if summary is not None:
            sets.append("summary=?")
            params.append(jdump(summary))
        if error is not None:
            sets.append("error=?")
            params.append(error[:2000])
        if error_code is not None:
            sets.append("error_code=?")
            params.append(error_code)
        params += [run_id, owner]
        ok = bool(self.db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=? AND lease_owner=?", params))
        if ok:
            self.event(
                run_id, "state", {"state": state, **({"error": error[:300], "code": error_code} if error else {})}
            )
        return ok

    def release(self, run_id: str, owner: str) -> None:
        """worker 正常退出时交还租约：任务回到 RECOVERING，由下一个 worker 立即接手。"""
        now = now_ms()
        self.db.execute(
            "UPDATE runs SET state=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE id=? AND lease_owner"
            "=? "
            "AND state IN (?,?)",
            (RECOVERING, now, run_id, owner, RUNNING, PAUSED),
        )

    # ------------------------------------------------------------------ 结果数据（信号、成交、净值）
    def save_fills(
        self, run_id: str, workspace_id: str, fills: list[dict[str, Any]], *, source: str = "simulated"
    ) -> int:
        """成交按 fill_key 去重写入：同一笔成交重放多次也只有一行。"""
        n = 0
        for f in fills:
            try:
                self.db.execute(
                    "INSERT INTO fills (id, run_id, workspace_id, fill_key, signal_id, order_id, instrument_id, side, "
                    "qty, price, "
                    "fee, fee_currency, ts, source, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{run_id}:{f['fill_id']}",
                        run_id,
                        workspace_id,
                        f["fill_id"],
                        f.get("signal_id"),
                        f.get("order_id"),
                        f["security"],
                        f["side"],
                        dec(f["amount"]),
                        dec(f["price"]),
                        dec(f["fee"]),
                        f.get("fee_currency", "USDT"),
                        f["ts"],
                        source,
                        jdump(f),
                    ),
                )
                n += 1
            except IntegrityError:
                continue
        return n

    def fills(self, run_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT meta FROM fills WHERE run_id=? ORDER BY ts, id LIMIT ?", (run_id, limit))
        return [jload(r["meta"], {}) for r in rows]

    def save_signals(self, run_id: str, workspace_id: str, signals: list[dict[str, Any]]) -> None:
        for s in signals:
            params = (
                s["signal_id"],
                run_id,
                workspace_id,
                s.get("strategy_version_id"),
                s["instrument_id"],
                s["event_id"],
                s["decision_ts"],
                s["expires_at"],
                s.get("reason", ""),
                jdump(s),
                s.get("status", "accepted"),
            )
            try:
                self.db.execute(
                    "INSERT INTO signals (signal_id, run_id, workspace_id, strategy_version_id, instrument_id, event_i"
                    "d, "
                    "decision_ts, expires_at, reason, target, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    params,
                )
            except IntegrityError:
                self.db.execute(
                    "UPDATE signals SET status=?, target=? WHERE signal_id=? AND run_id=?",
                    (s.get("status", "accepted"), jdump(s), s["signal_id"], run_id),
                )

    def signals(self, run_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT target FROM signals WHERE run_id=? ORDER BY decision_ts LIMIT ?", (run_id, limit)
        )
        return [jload(r["target"], {}) for r in rows]

    def signal_rows(self, run_id: str, *, limit: int = 2000) -> list[dict[str, Any]]:
        """信号全字段（含 signal_id / decision_ts / status），供订单层对照使用。

        与 `signals()` 的区别：那个只返回 target 供信号列表展示，这个要能判断
        "这笔信号最后是成交、被拒还是过期"，所以必须带状态与时间。
        """
        rows = self.db.fetchall(
            "SELECT signal_id, strategy_version_id, instrument_id, event_id, decision_ts, expires_at, reason, "
            "status, target FROM signals WHERE run_id=? ORDER BY decision_ts LIMIT ?",
            (run_id, limit),
        )
        out = []
        for r in rows:
            row = dict(r)
            row["target"] = jload(row.get("target"), {})
            out.append(row)
        return out

    def save_nav(self, run_id: str, points: list[dict[str, Any]]) -> None:
        for p in points:
            try:
                self.db.execute(
                    "INSERT INTO nav_points (run_id, ts, nav, units, unit_nav, cash, positions_value, stale) VALUES (?"
                    ",?,?,?,?,?,?,?)",
                    (
                        run_id,
                        p["ts"],
                        dec(p["nav"]),
                        dec(p["units"]),
                        dec(p["unit_nav"]),
                        dec(p["cash"]),
                        dec(p["positions_value"]),
                        int(bool(p.get("stale"))),
                    ),
                )
            except IntegrityError:
                continue

    def nav(self, run_id: str, *, limit: int = 20_000) -> list[dict[str, Any]]:
        rows = self.db.fetchall("SELECT * FROM nav_points WHERE run_id=? ORDER BY ts LIMIT ?", (run_id, limit))
        return [
            {k: (str(v) if k in ("nav", "units", "unit_nav", "cash", "positions_value") else v) for k, v in r.items()}
            for r in rows
        ]
