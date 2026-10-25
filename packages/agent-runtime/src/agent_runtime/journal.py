"""Durable authority for runs, steps, attempts, effects, budgets and events.

SQLite is the local default; PostgreSQL enables multi-worker deployments.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load(value: str | None) -> Any:
    return json.loads(value) if value else None


def now_ms() -> int:
    return int(time.time() * 1000)


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    parent_id TEXT,
    state TEXT NOT NULL,
    spec TEXT NOT NULL,
    messages TEXT NOT NULL,
    seq BIGINT NOT NULL DEFAULT 0,
    next_ordinal BIGINT NOT NULL DEFAULT 1,
    output TEXT,
    error TEXT,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    ordinal BIGINT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    effect_id TEXT,
    attempt_count BIGINT NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_epoch BIGINT NOT NULL DEFAULT 0,
    lease_until BIGINT,
    UNIQUE(run_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_id);
CREATE INDEX IF NOT EXISTS idx_steps_ready ON steps(state, run_id, ordinal);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES steps(id),
    epoch BIGINT NOT NULL,
    status TEXT NOT NULL,
    started_at BIGINT NOT NULL,
    ended_at BIGINT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS attempt_timings (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
    first_token_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
    id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL UNIQUE REFERENCES steps(id),
    state TEXT NOT NULL,
    result TEXT,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq BIGINT NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    PRIMARY KEY(run_id, seq)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL REFERENCES runs(id),
    seq BIGINT NOT NULL,
    state TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at BIGINT NOT NULL,
    PRIMARY KEY(run_id, seq)
);
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_accounts (
    id TEXT PRIMARY KEY,
    limit_micro BIGINT NOT NULL,
    spent_micro BIGINT NOT NULL DEFAULT 0,
    reserved_micro BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_accounts (
    run_id TEXT NOT NULL REFERENCES runs(id),
    account_id TEXT NOT NULL REFERENCES budget_accounts(id),
    PRIMARY KEY(run_id, account_id)
);
CREATE TABLE IF NOT EXISTS reservations (
    step_id TEXT NOT NULL REFERENCES steps(id),
    account_id TEXT NOT NULL REFERENCES budget_accounts(id),
    amount_micro BIGINT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(step_id, account_id)
);
CREATE TABLE IF NOT EXISTS telemetry_span_exports (
    consumer_id TEXT NOT NULL,
    span_key TEXT NOT NULL,
    exported_at BIGINT NOT NULL,
    PRIMARY KEY(consumer_id, span_key)
);
CREATE TABLE IF NOT EXISTS telemetry_exports (
    consumer_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    exported_at BIGINT NOT NULL,
    PRIMARY KEY(consumer_id, attempt_id)
);
CREATE TABLE IF NOT EXISTS run_joins (
    id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL REFERENCES runs(id),
    step_id TEXT NOT NULL UNIQUE REFERENCES steps(id),
    request TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_joins_parent ON run_joins(parent_id,state);
CREATE TABLE IF NOT EXISTS run_join_children (
    join_id TEXT NOT NULL REFERENCES run_joins(id),
    child_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
    ordinal BIGINT NOT NULL,
    PRIMARY KEY(join_id, ordinal)
);
CREATE TABLE IF NOT EXISTS run_dependencies (
    child_id TEXT NOT NULL REFERENCES runs(id),
    predecessor_id TEXT NOT NULL REFERENCES runs(id),
    ordinal BIGINT NOT NULL,
    PRIMARY KEY(child_id, ordinal),
    UNIQUE(child_id, predecessor_id)
);
"""


class JournalIntegrityError(RuntimeError):
    """Committed state and event history disagree; execution must stop."""


def _fold(state: dict[str, Any], event: Any) -> None:
    data = load(event["data"])
    if event["type"] == "run.created":
        state["run"] = "QUEUED"
    elif event["type"] == "run.state":
        state["run"] = data["state"]
    elif event["type"] in {"step.created", "step.state"}:
        state["steps"][data["step_id"]] = data["state"]
    elif event["type"] == "attempt.started":
        state["steps"][data["step_id"]] = "IN_FLIGHT"


class _DB:
    """Thin DB-API adapter: qmark SQL for callers, PostgreSQL params underneath."""

    def __init__(self, raw: Any, dialect: str) -> None:
        self.raw = raw
        self.dialect = dialect

    def execute(self, sql: str, params: tuple | list = ()) -> Any:
        if self.dialect == "postgres":
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, tuple(params))

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


class Journal:
    def __init__(self, path: str | Path, *, schema: str = "agent_runtime") -> None:
        self.path = str(path)
        self.dialect = "postgres" if urlparse(self.path).scheme in {"postgres", "postgresql"} else "sqlite"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
            raise ValueError("invalid journal schema")
        self.schema = schema
        if self.dialect == "postgres":
            import psycopg

            # IF NOT EXISTS alone does not serialize concurrent catalog writes.
            # Bootstrap schema and tables atomically with the execution lock.
            with psycopg.connect(self.path) as bootstrap:
                bootstrap.execute("SELECT pg_advisory_xact_lock(7243003)")
                bootstrap.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
                bootstrap.execute(f'SET LOCAL search_path TO "{self.schema}", public')
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        bootstrap.execute(statement)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with self.connection() as db:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        db.execute("PRAGMA journal_mode=WAL")
                        break
                    except sqlite3.OperationalError as exc:
                        # Journal-mode transitions may bypass busy_timeout when
                        # fresh API/worker processes initialize the same file.
                        code = getattr(exc, "sqlite_errorcode", 0) & 255
                        if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)

    @contextmanager
    def connection(self) -> Iterator[_DB]:
        if self.dialect == "postgres":
            import psycopg
            from psycopg.rows import dict_row

            raw = psycopg.connect(self.path, autocommit=True, row_factory=dict_row)
        else:
            raw = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            raw.row_factory = sqlite3.Row
            raw.execute("PRAGMA foreign_keys=ON")
            raw.execute("PRAGMA busy_timeout=30000")
        db = _DB(raw, self.dialect)
        if self.dialect == "postgres":
            db.execute(f'SET search_path TO "{self.schema}", public')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def read_tx(self) -> Iterator[_DB]:
        """Consistent read snapshot without taking the execution writer lock."""
        with self.connection() as db:
            if self.dialect == "sqlite":
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
            else:
                db.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                yield db
            finally:
                db.rollback()

    @contextmanager
    def tx(self) -> Iterator[_DB]:
        with self.connection() as db:
            if self.dialect == "postgres":
                db.execute("BEGIN")
                # The Runtime journal is shared by many runs. A single global
                # transaction lock keeps event sequence and budget transitions
                # serialized across worker processes; per-run locks can come later.
                db.execute("SELECT pg_advisory_xact_lock(7243003)")
            else:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def event(db: Any, run_id: str, type_: str, data: dict[str, Any]) -> int:
        row = db.execute("SELECT seq FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        seq = int(row["seq"]) + 1
        stamp = now_ms()
        db.execute("UPDATE runs SET seq=?, updated_at=? WHERE id=?", (seq, stamp, run_id))
        db.execute(
            "INSERT INTO events(run_id,seq,type,data,created_at) VALUES(?,?,?,?,?)",
            (run_id, seq, type_, dump(data), stamp),
        )
        return seq

    @staticmethod
    def new_step(
        db: Any,
        run_id: str,
        kind: str,
        state: str,
        payload: dict[str, Any],
        *,
        effect_id: str | None = None,
    ) -> str:
        row = db.execute("SELECT next_ordinal FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        ordinal = int(row["next_ordinal"])
        step_id = f"{run_id}:{ordinal:06d}"
        db.execute("UPDATE runs SET next_ordinal=? WHERE id=?", (ordinal + 1, run_id))
        db.execute(
            "INSERT INTO steps(id,run_id,ordinal,kind,state,payload,effect_id) VALUES(?,?,?,?,?,?,?)",
            (step_id, run_id, ordinal, kind, state, dump(payload), effect_id),
        )
        if effect_id is not None:
            db.execute(
                "INSERT INTO effects(id,step_id,state,result,updated_at) VALUES(?,?,?,?,?)",
                (effect_id, step_id, "PENDING", None, now_ms()),
            )
            Journal.event(
                db,
                run_id,
                "effect.intent",
                {
                    "step_id": step_id,
                    "effect_id": effect_id,
                    "arguments_sha256": hashlib.sha256(dump(payload).encode("utf-8")).hexdigest(),
                },
            )
        Journal.event(db, run_id, "step.created", {"step_id": step_id, "kind": kind, "state": state})
        return step_id

    @staticmethod
    def set_run(db: Any, run_id: str, state: str, **fields: Any) -> None:
        parts = ["state=?", "updated_at=?"]
        values: list[Any] = [state, now_ms()]
        for name, value in fields.items():
            if name not in {"output", "error", "messages"}:
                raise ValueError(name)
            parts.append(f"{name}=?")
            values.append(dump(value) if name == "messages" else value)
        values.append(run_id)
        db.execute(f"UPDATE runs SET {', '.join(parts)} WHERE id=?", values)
        Journal.event(db, run_id, "run.state", {"state": state})

    @staticmethod
    def set_step(db: Any, step_id: str, state: str, **fields: Any) -> None:
        parts = ["state=?"]
        values: list[Any] = [state]
        for name, value in fields.items():
            if name not in {"result", "lease_owner", "lease_until"}:
                raise ValueError(name)
            parts.append(f"{name}=?")
            values.append(dump(value) if name == "result" else value)
        values.append(step_id)
        db.execute(f"UPDATE steps SET {', '.join(parts)} WHERE id=?", values)
        row = db.execute("SELECT run_id FROM steps WHERE id=?", (step_id,)).fetchone()
        if row is None:
            raise KeyError(step_id)
        Journal.event(db, row["run_id"], "step.state", {"step_id": step_id, "state": state})

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["spec"] = load(result["spec"])
            result["messages"] = load(result["messages"])
            return result

    def steps(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM steps WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
            return [{**dict(row), "payload": load(row["payload"]), "result": load(row["result"])} for row in rows]

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT seq,type,data,created_at FROM events WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after),
            ).fetchall()
            return [{**dict(row), "data": load(row["data"])} for row in rows]

    def checkpoint(self, run_id: str, *, retain: int = 2) -> int:
        """Save an optional, checksummed summary at an already committed event cursor."""
        if retain < 1:
            raise ValueError("retain must be positive")
        with self.tx() as db:
            run = db.execute("SELECT seq,state FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            state = {
                "run": run["state"],
                "steps": {
                    row["id"]: row["state"]
                    for row in db.execute("SELECT id,state FROM steps WHERE run_id=?", (run_id,))
                },
            }
            encoded = dump(state)
            db.execute(
                "INSERT INTO checkpoints(run_id,seq,state,sha256,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(run_id,seq) DO NOTHING",
                (run_id, run["seq"], encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), now_ms()),
            )
            db.execute(
                "DELETE FROM checkpoints WHERE run_id=? AND seq NOT IN "
                "(SELECT seq FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT ?)",
                (run_id, run_id, retain),
            )
            return run["seq"]

    def audit(self, run_id: str) -> dict[str, Any]:
        """Fold committed events and compare with authoritative rows before recovery."""
        with self.read_tx() as db:
            run = db.execute("SELECT seq,state FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            events = db.execute("SELECT seq,type,data FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
            checkpoints = db.execute(
                "SELECT seq,state,sha256 FROM checkpoints WHERE run_id=? AND seq<=? ORDER BY seq DESC",
                (run_id, run["seq"]),
            ).fetchall()
            state: dict[str, Any] = {"run": None, "steps": {}}
            usable_checkpoint = 0
            candidates = {row["seq"]: row for row in checkpoints}
            for expected, event in enumerate(events, 1):
                if event["seq"] != expected:
                    raise JournalIntegrityError(f"event gap for {run_id} at {expected}")
                _fold(state, event)
                candidate = candidates.get(expected)
                if candidate:
                    digest = hashlib.sha256(candidate["state"].encode("utf-8")).hexdigest()
                    if digest == candidate["sha256"] and load(candidate["state"]) == state:
                        usable_checkpoint = expected
            actual = {
                "run": run["state"],
                "steps": {
                    row["id"]: row["state"]
                    for row in db.execute("SELECT id,state FROM steps WHERE run_id=?", (run_id,))
                },
            }
            if len(events) != run["seq"] or state != actual:
                raise JournalIntegrityError(
                    f"committed state differs from events for {run_id}: "
                    f"events={len(events)} seq={run['seq']} folded={state!r} actual={actual!r}"
                )
            return {"seq": run["seq"], "checkpoint_seq": usable_checkpoint, "state": state}

    def budget(self, account_id: str) -> dict[str, int] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM budget_accounts WHERE id=?", (account_id,)).fetchone()
            return dict(row) if row else None

    def ensure_budget(self, account_id: str, limit_micro: int) -> None:
        if limit_micro < 0:
            raise ValueError("negative budget")
        with self.tx() as db:
            existing = db.execute(
                "SELECT spent_micro,reserved_micro FROM budget_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if existing and existing["spent_micro"] + existing["reserved_micro"] > limit_micro:
                raise ValueError("new limit is below committed spending and reservations")
            db.execute(
                "INSERT INTO budget_accounts(id,limit_micro) VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET limit_micro=excluded.limit_micro",
                (account_id, limit_micro),
            )

    @staticmethod
    def reserve(db: Any, run_id: str, step_id: str, amount_micro: int) -> bool:
        if amount_micro <= 0:
            return True
        accounts = db.execute(
            "SELECT b.* FROM budget_accounts b JOIN run_accounts ra ON ra.account_id=b.id "
            "WHERE ra.run_id=? ORDER BY b.id",
            (run_id,),
        ).fetchall()
        if not accounts:
            raise ValueError("budgeted run has no budget account")
        if any(a["limit_micro"] - a["spent_micro"] - a["reserved_micro"] < amount_micro for a in accounts):
            return False
        for account in accounts:
            db.execute(
                "UPDATE budget_accounts SET reserved_micro=reserved_micro+? WHERE id=?",
                (amount_micro, account["id"]),
            )
            db.execute(
                "INSERT INTO reservations(step_id,account_id,amount_micro,status) VALUES(?,?,?,'reserved')",
                (step_id, account["id"], amount_micro),
            )
        Journal.event(db, run_id, "budget.reserved", {"step_id": step_id, "amount_micro": amount_micro})
        return True

    @staticmethod
    def settle(db: Any, run_id: str, step_id: str, actual_micro: int | None) -> None:
        rows = db.execute(
            "SELECT account_id,amount_micro FROM reservations WHERE step_id=? AND status='reserved'",
            (step_id,),
        ).fetchall()
        if actual_micro is None:
            Journal.event(db, run_id, "budget.unsettled", {"step_id": step_id})
            return
        if actual_micro < 0:
            raise ValueError("negative usage")
        for row in rows:
            db.execute(
                "UPDATE budget_accounts SET reserved_micro=reserved_micro-?, spent_micro=spent_micro+? WHERE id=?",
                (row["amount_micro"], actual_micro, row["account_id"]),
            )
            account = db.execute(
                "SELECT limit_micro,spent_micro,reserved_micro FROM budget_accounts WHERE id=?", (row["account_id"],)
            ).fetchone()
            if account["spent_micro"] + account["reserved_micro"] > account["limit_micro"]:
                Journal.event(
                    db,
                    run_id,
                    "budget.overrun",
                    {
                        "step_id": step_id,
                        "account_id": row["account_id"],
                        "over_micro": account["spent_micro"] + account["reserved_micro"] - account["limit_micro"],
                    },
                )
            db.execute(
                "UPDATE reservations SET status='settled' WHERE step_id=? AND account_id=?",
                (step_id, row["account_id"]),
            )
        Journal.event(db, run_id, "budget.settled", {"step_id": step_id, "actual_micro": actual_micro})
