"""Small durable agent loop. The journal is the sole execution-state authority."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import asdict
from typing import Any

from .contracts import AgentSpec, ChildTask, ModelAdmissionPending, ModelPort, ModelUnavailable, ToolCall, ToolPort
from .journal import Journal, JournalIntegrityError, dump, load, now_ms


class AgentRuntime:
    def __init__(
        self, journal: Journal, model: ModelPort, tools: ToolPort, *, worker_id: str = "local", lease_ms: int = 30000
    ) -> None:
        if lease_ms < 100:
            raise ValueError("lease_ms must be at least 100")
        self.journal = journal
        self.model = model
        self.tools = tools
        self.worker_id = worker_id
        self.lease_ms = lease_ms

    def start(
        self,
        prompt: str,
        spec: AgentSpec,
        *,
        run_id: str | None = None,
        tenant_id: str = "default",
        account_ids: tuple[str, ...] = (),
        initial_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        run_id = run_id or uuid.uuid4().hex
        with self.journal.tx() as db:
            self._start(
                db,
                prompt,
                spec,
                run_id=run_id,
                tenant_id=tenant_id,
                account_ids=account_ids,
                initial_messages=initial_messages,
            )
        return run_id

    def _start(
        self,
        db: Any,
        prompt: str,
        spec: AgentSpec,
        *,
        run_id: str,
        tenant_id: str,
        account_ids: tuple[str, ...] = (),
        initial_messages: list[dict[str, Any]] | None = None,
        parent_id: str | None = None,
    ) -> None:
        stamp = now_ms()
        db.execute(
            "INSERT INTO runs(id,tenant_id,state,spec,messages,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                tenant_id,
                "QUEUED",
                dump(asdict(spec)),
                dump(initial_messages if initial_messages is not None else [{"role": "user", "content": prompt}]),
                stamp,
                stamp,
            ),
        )
        for account_id in account_ids:
            db.execute("INSERT INTO run_accounts(run_id,account_id) VALUES(?,?)", (run_id, account_id))
        if spec.budget_limit_micro:
            account_id = f"run:{run_id}"
            db.execute("INSERT INTO budget_accounts(id,limit_micro) VALUES(?,?)", (account_id, spec.budget_limit_micro))
            db.execute("INSERT INTO run_accounts(run_id,account_id) VALUES(?,?)", (run_id, account_id))
        self.journal.event(db, run_id, "run.created", {"run_id": run_id})
        self.journal.new_step(db, run_id, "model", "READY", {})
        self.journal.set_run(db, run_id, "RUNNING")
        if parent_id is not None:
            db.execute("UPDATE runs SET parent_id=? WHERE id=?", (parent_id, run_id))
            self.journal.event(db, run_id, "run.parent", {"parent_id": parent_id})

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self.journal.run(run_id)
        if run:
            run["steps"] = self.journal.steps(run_id)
        return run

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.journal.events(run_id, after)

    def children(self, run_id: str) -> list[str]:
        """Discover committed direct children for host-side port rebinding."""
        with self.journal.read_tx() as db:
            if db.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone() is None:
                raise KeyError(run_id)
            joins = db.execute(
                "SELECT j.* FROM run_joins j JOIN steps s ON s.id=j.step_id WHERE j.parent_id=? ORDER BY s.ordinal",
                (run_id,),
            ).fetchall()
            return [child["id"] for join in joins for child in self._join_children(db, join)]

    def fork_children(
        self,
        run_id: str,
        children: list[ChildTask | tuple[str, AgentSpec]],
        *,
        idempotency_key: str,
        join_policy: str = "all_success",
    ) -> list[str]:
        """Suspend a READY boundary and atomically create durable child runs."""
        self.journal.audit(run_id)
        with self.journal.tx() as db:
            return self._fork_children(db, run_id, children, idempotency_key=idempotency_key, join_policy=join_policy)

    def start_with_children(
        self,
        prompt: str,
        spec: AgentSpec,
        children: list[ChildTask],
        *,
        run_id: str,
        tenant_id: str,
        initial_messages: list[dict[str, Any]] | None = None,
        join_policy: str = "all_success",
    ) -> str:
        """Create a whole graph in one transaction, before any worker may run it."""
        request = dump(
            {
                "prompt": prompt,
                "spec": asdict(spec),
                "children": [asdict(child) for child in children],
                "messages": initial_messages,
                "tenant_id": tenant_id,
                "join_policy": join_policy,
            }
        )
        digest = hashlib.sha256(request.encode()).hexdigest()
        legacy_digest = None
        if all(not child.depends_on and child.dependency_policy == "all_success" for child in children):
            legacy = load(request)
            for child in legacy["children"]:
                child.pop("depends_on")
                child.pop("dependency_policy")
            legacy_digest = hashlib.sha256(dump(legacy).encode()).hexdigest()
        with self.journal.tx() as db:
            existing = db.execute("SELECT tenant_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if existing:
                event = db.execute(
                    "SELECT data FROM events WHERE run_id=? AND type='graph.created'", (run_id,)
                ).fetchone()
                fact = load(event["data"]) if event else {}
                matches = fact.get("request_sha256") == digest or (
                    fact.get("request_contract", 1) == 1
                    and legacy_digest is not None
                    and fact.get("request_sha256") == legacy_digest
                )
                if existing["tenant_id"] != tenant_id or not matches:
                    raise ValueError("graph identity belongs to another request")
                return run_id
            self._start(db, prompt, spec, run_id=run_id, tenant_id=tenant_id, initial_messages=initial_messages)
            self._fork_children(db, run_id, children, idempotency_key="initial", join_policy=join_policy)
            self.journal.event(db, run_id, "graph.created", {"request_sha256": digest, "request_contract": 2})
        return run_id

    def _fork_children(
        self,
        db: Any,
        run_id: str,
        children: list[ChildTask | tuple[str, AgentSpec]],
        *,
        idempotency_key: str,
        join_policy: str,
    ) -> list[str]:
        if not idempotency_key or len(idempotency_key) > 160 or not 1 <= len(children) <= 32:
            raise ValueError("a stable key and between 1 and 32 children are required")
        if join_policy not in {"all_success", "all_settled"}:
            raise ValueError("unsupported join policy")
        tasks = [child if isinstance(child, ChildTask) else ChildTask(*child) for child in children]
        for index, task in enumerate(tasks):
            if task.dependency_policy not in {"all_success", "all_settled"}:
                raise ValueError("unsupported dependency policy")
            if len(set(task.depends_on)) != len(task.depends_on) or any(
                type(value) is not int or not 0 <= value < index for value in task.depends_on
            ):
                raise ValueError("dependencies must be distinct earlier child indices")
        request = dump({"children": [asdict(child) for child in tasks], "policy": join_policy})
        join_id = "join-" + hashlib.sha256(dump([run_id, idempotency_key]).encode()).hexdigest()
        prior = db.execute("SELECT * FROM run_joins WHERE id=?", (join_id,)).fetchone()
        if prior:
            previous = load(prior["request"])
            if isinstance(previous, list):
                previous = {"children": previous, "policy": "all_success"}
            for child in previous["children"]:
                child.setdefault("depends_on", [])
                child.setdefault("dependency_policy", "all_success")
            if dump(previous) != request:
                raise ValueError("join idempotency key belongs to another request")
            return [row["id"] for row in self._join_children(db, prior)]
        run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise KeyError(run_id)
        pending = db.execute(
            "SELECT * FROM steps WHERE run_id=? AND state NOT IN ('SUCCEEDED','FAILED','CANCELED')",
            (run_id,),
        ).fetchall()
        if (
            run["state"] != "RUNNING"
            or len(pending) != 1
            or pending[0]["kind"] != "model"
            or pending[0]["state"] != "READY"
        ):
            raise ValueError("children may only fork at an unclaimed READY model boundary")
        step = pending[0]
        if db.execute("SELECT id FROM run_joins WHERE step_id=?", (step["id"],)).fetchone():
            raise ValueError("this model boundary already has a join")
        accounts = tuple(
            row["account_id"]
            for row in db.execute(
                "SELECT account_id FROM run_accounts WHERE run_id=? ORDER BY account_id", (run_id,)
            ).fetchall()
        )
        # A zero estimate must not bypass an inherited hard budget.
        self._check_child_bounds(db, run, len(tasks))
        if accounts and any(child.spec.model_reservation_micro <= 0 for child in tasks):
            raise ValueError("children sharing a budget require a positive reservation estimate")
        db.execute(
            "INSERT INTO run_joins(id,parent_id,step_id,request,state) VALUES(?,?,?,?,'WAITING')",
            (join_id, run_id, step["id"], request),
        )
        child_ids = []
        for ordinal, child in enumerate(tasks):
            child_id = f"{join_id}:{ordinal}"
            self._start(
                db,
                child.prompt,
                child.spec,
                run_id=child_id,
                initial_messages=child.initial_messages,
                tenant_id=run["tenant_id"],
                account_ids=accounts,
                parent_id=run_id,
            )
            db.execute(
                "INSERT INTO run_join_children(join_id,child_id,ordinal) VALUES(?,?,?)",
                (join_id, child_id, ordinal),
            )
            child_ids.append(child_id)
            if child.depends_on:
                child_step = db.execute("SELECT id FROM steps WHERE run_id=? AND ordinal=1", (child_id,)).fetchone()[
                    "id"
                ]
                for position, predecessor in enumerate(child.depends_on):
                    db.execute(
                        "INSERT INTO run_dependencies(child_id,predecessor_id,ordinal) VALUES(?,?,?)",
                        (child_id, child_ids[predecessor], position),
                    )
                self.journal.event(
                    db,
                    child_id,
                    "dependency.created",
                    {"step_id": child_step, "predecessors": [child_ids[index] for index in child.depends_on]},
                )
                self.journal.set_step(db, child_step, "WAITING_DEPENDENCIES")
                self.journal.set_run(db, child_id, "WAITING")
        self.journal.event(
            db, run_id, "join.created", {"join_id": join_id, "child_ids": child_ids, "step_id": step["id"]}
        )
        self.journal.set_step(db, step["id"], "WAITING_CHILDREN")
        self.journal.set_run(db, run_id, "WAITING")
        return child_ids

    def _check_child_bounds(self, db: Any, parent: Any, count: int) -> None:
        direct = db.execute("SELECT COUNT(*) AS n FROM runs WHERE parent_id=?", (parent["id"],)).fetchone()["n"]
        if direct + count > load(parent["spec"]).get("max_children", 32):
            raise ValueError("maximum direct children exceeded")
        ancestor, seen, depth = parent, set(), 1
        while ancestor is not None:
            if ancestor["id"] in seen or ancestor["tenant_id"] != parent["tenant_id"]:
                raise JournalIntegrityError("invalid ancestor chain")
            seen.add(ancestor["id"])
            spec = load(ancestor["spec"])
            if depth > spec.get("max_child_depth", 16):
                raise ValueError("maximum child depth exceeded")
            if len(self._tree_ids(db, ancestor["id"])) - 1 + count > spec.get("max_descendants", 256):
                raise ValueError("maximum descendants exceeded")
            ancestor = (
                db.execute("SELECT * FROM runs WHERE id=?", (ancestor["parent_id"],)).fetchone()
                if ancestor["parent_id"]
                else None
            )
            depth += 1

    def _finish_cancellation(self, db: Any, run_id: str) -> None:
        pending = db.execute(
            "SELECT id FROM steps WHERE run_id=? AND state IN ('IN_FLIGHT','RECONCILING') LIMIT 1", (run_id,)
        ).fetchone()
        child = db.execute(
            "SELECT id FROM runs WHERE parent_id=? AND state NOT IN ('COMPLETED','FAILED','CANCELED') LIMIT 1",
            (run_id,),
        ).fetchone()
        self.journal.set_run(db, run_id, "CANCELING" if pending or child else "CANCELED")

    @staticmethod
    def _tree_ids(db: Any, run_id: str) -> list[str]:
        tenant = db.execute("SELECT tenant_id FROM runs WHERE id=?", (run_id,)).fetchone()["tenant_id"]
        ids, seen = [run_id], {run_id}
        for current in ids:
            for child in db.execute("SELECT id,tenant_id FROM runs WHERE parent_id=?", (current,)).fetchall():
                if child["id"] in seen or child["tenant_id"] != tenant:
                    raise JournalIntegrityError("child tree contains a cycle or crosses tenant boundaries")
                seen.add(child["id"])
                ids.append(child["id"])
        return ids

    def _cancel_tree(self, db: Any, run_id: str, actor: str) -> None:
        ids = self._tree_ids(db, run_id)
        for current in ids[1:]:
            self.journal.audit(current)
        for current in reversed(ids):
            state = db.execute("SELECT state FROM runs WHERE id=?", (current,)).fetchone()["state"]
            if state in {"COMPLETED", "FAILED", "CANCELED"}:
                continue
            if current != run_id:
                self.journal.event(db, current, "run.cancel_propagated", {"root_id": run_id, "actor": actor})
            for step in db.execute("SELECT id,state FROM steps WHERE run_id=?", (current,)).fetchall():
                if step["state"] not in {"SUCCEEDED", "FAILED", "CANCELED"}:
                    interrupted = db.execute(
                        "SELECT id FROM attempts WHERE step_id=? AND status='IN_FLIGHT' AND ended_at IS NULL",
                        (step["id"],),
                    ).fetchall()
                    for attempt in interrupted:
                        db.execute(
                            "UPDATE attempts SET status='UNKNOWN',ended_at=?,error=? WHERE id=?",
                            (
                                now_ms(),
                                "cancel fenced the executing attempt; external result unconfirmed",
                                attempt["id"],
                            ),
                        )
                        self.journal.event(
                            db,
                            current,
                            "attempt.interrupted",
                            {
                                "attempt_id": attempt["id"],
                                "step_id": step["id"],
                                "reason": "cancel_requested",
                            },
                        )
                    if step["state"] not in {"IN_FLIGHT", "RECONCILING"}:
                        if db.execute(
                            "SELECT 1 FROM reservations WHERE step_id=? AND status='reserved'", (step["id"],)
                        ).fetchone():
                            self.journal.settle(db, current, step["id"], 0)
                        db.execute("DELETE FROM admission_waits WHERE step_id=?", (step["id"],))
                    self.journal.set_step(
                        db, step["id"], "RECONCILING" if step["state"] in {"IN_FLIGHT", "RECONCILING"} else "CANCELED"
                    )
            self._finish_cancellation(db, current)

    def _refresh_joins(self, db: Any, run: Any) -> None:
        for join in db.execute(
            "SELECT * FROM run_joins WHERE parent_id=? AND state='WAITING'", (run["id"],)
        ).fetchall():
            step = db.execute("SELECT state FROM steps WHERE id=?", (join["step_id"],)).fetchone()
            expected_step = "WAITING_CHILDREN" if run["state"] == "WAITING" else "CANCELED"
            if run["state"] not in {"WAITING", "CANCELING", "CANCELED"} or step["state"] != expected_step:
                raise JournalIntegrityError("pending join disagrees with parent execution state")
            children = self._join_children(db, join)
            if any(child["state"] not in {"COMPLETED", "FAILED", "CANCELED"} for child in children):
                continue
            canceled = run["state"] in {"CANCELING", "CANCELED"}
            request = load(join["request"])
            policy = request.get("policy", "all_success") if isinstance(request, dict) else "all_success"
            succeeded = policy == "all_settled" or all(child["state"] == "COMPLETED" for child in children)
            state = "CANCELED" if canceled else ("COMPLETED" if succeeded else "FAILED")
            db.execute("UPDATE run_joins SET state=? WHERE id=?", (state, join["id"]))
            self.journal.event(db, run["id"], "join.resolved", {"join_id": join["id"], "state": state})
            if canceled:
                self._finish_cancellation(db, run["id"])
            elif not succeeded:
                self.journal.set_step(db, join["step_id"], "FAILED")
                self.journal.set_run(db, run["id"], "FAILED", error="child task failed or was canceled")
            else:
                messages = load(run["messages"])
                messages.append(
                    {
                        "role": "user",
                        "content": dump(
                            {
                                "child_results": [
                                    {"id": child["id"], "state": child["state"], "output": child["output"]}
                                    for child in children
                                ]
                            }
                        ),
                    }
                )
                self.journal.set_step(db, join["step_id"], "READY")
                self.journal.set_run(db, run["id"], "RUNNING", messages=messages)

    def _join_children(self, db: Any, join: Any) -> list[Any]:
        children = db.execute(
            "SELECT r.*,c.ordinal FROM run_join_children c JOIN runs r ON r.id=c.child_id "
            "WHERE c.join_id=? ORDER BY c.ordinal",
            (join["id"],),
        ).fetchall()
        expected = load(join["request"])
        if isinstance(expected, dict):
            expected = expected["children"]
        tenant = db.execute("SELECT tenant_id FROM runs WHERE id=?", (join["parent_id"],)).fetchone()["tenant_id"]
        if len(children) != len(expected) or not children:
            raise JournalIntegrityError("join children differ from the committed fork request")
        for ordinal, child in enumerate(children):
            if (
                child["id"] != f"{join['id']}:{ordinal}"
                or child["ordinal"] != ordinal
                or child["parent_id"] != join["parent_id"]
                or child["tenant_id"] != tenant
                or load(child["spec"]) != expected[ordinal]["spec"]
            ):
                raise JournalIntegrityError("join child identity or specification differs from committed intent")
        return children

    def _refresh_dependencies(self, db: Any, run: Any) -> None:
        if run["state"] in {"COMPLETED", "FAILED", "CANCELING", "CANCELED"}:
            return
        step = db.execute(
            "SELECT * FROM steps WHERE run_id=? AND state='WAITING_DEPENDENCIES'", (run["id"],)
        ).fetchone()
        if step is None:
            return
        membership = db.execute(
            "SELECT c.ordinal,j.* FROM run_join_children c JOIN run_joins j ON j.id=c.join_id WHERE c.child_id=?",
            (run["id"],),
        ).fetchone()
        if membership is None or step["ordinal"] != 1 or step["attempt_count"] != 0:
            raise JournalIntegrityError("invalid dependency wait boundary")
        request = load(membership["request"])
        tasks = request["children"] if isinstance(request, dict) else request
        intent = tasks[membership["ordinal"]]
        expected = [f"{membership['id']}:{index}" for index in intent.get("depends_on", [])]
        predecessors = db.execute(
            "SELECT r.id,r.state,r.output FROM run_dependencies d JOIN runs r ON r.id=d.predecessor_id WHERE d.child_id=? ORDER BY d.ordinal",
            (run["id"],),
        ).fetchall()
        if not expected or [row["id"] for row in predecessors] != expected:
            raise JournalIntegrityError("dependency edges differ from committed intent")
        for predecessor in predecessors:
            self.journal.audit(predecessor["id"])
        if any(row["state"] not in {"COMPLETED", "FAILED", "CANCELED"} for row in predecessors):
            return
        if intent.get("dependency_policy", "all_success") == "all_success" and any(
            row["state"] != "COMPLETED" for row in predecessors
        ):
            self.journal.set_step(db, step["id"], "FAILED")
            self.journal.set_run(db, run["id"], "FAILED", error="predecessor task failed or was canceled")
            return
        messages = load(run["messages"])
        messages.append({"role": "user", "content": dump({"dependency_results": [dict(row) for row in predecessors]})})
        self.journal.event(db, run["id"], "dependency.resolved", {"step_id": step["id"], "predecessors": expected})
        self.journal.set_step(db, step["id"], "READY")
        self.journal.set_run(db, run["id"], "RUNNING", messages=messages)

    def refresh(self, run_id: str) -> dict[str, Any]:
        """Fold ready dependencies and joins without dispatching any external work."""
        self.journal.audit(run_id)
        with self.journal.tx() as db:
            ids = self._tree_ids(db, run_id)
            for _ in range(len(ids) + 1):
                before = tuple(db.execute("SELECT seq FROM runs WHERE id=?", (item,)).fetchone()["seq"] for item in ids)
                self._refresh_tree_joins(db, run_id)
                after = tuple(db.execute("SELECT seq FROM runs WHERE id=?", (item,)).fetchone()["seq"] for item in ids)
                if before == after:
                    break
            else:
                raise JournalIntegrityError("dependency projection failed to converge")
        return self.get(run_id)

    def _refresh_tree_joins(self, db: Any, run_id: str) -> None:
        ids = self._tree_ids(db, run_id)
        for current in ids[1:]:
            self.journal.audit(current)
        for current in ids:
            run = db.execute("SELECT * FROM runs WHERE id=?", (current,)).fetchone()
            self._refresh_dependencies(db, run)
        for current in reversed(ids):
            run = db.execute("SELECT * FROM runs WHERE id=?", (current,)).fetchone()
            self._refresh_joins(db, run)

    def signal(self, run_id: str, type_: str, data: dict[str, Any], *, signal_id: str, actor: str) -> bool:
        if not actor or not signal_id:
            raise ValueError("signals require actor and stable signal_id")
        self.journal.audit(run_id)
        with self.journal.tx() as db:
            run = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            existing = db.execute("SELECT run_id,type,data FROM signals WHERE id=?", (signal_id,)).fetchone()
            encoded = dump({**data, "actor": actor})
            if existing:
                if (existing["run_id"], existing["type"], existing["data"]) != (run_id, type_, encoded):
                    raise ValueError("signal id belongs to a different request")
                return False
            if type_ == "cancel" and run["state"] in {"COMPLETED", "FAILED", "CANCELED"}:
                raise ValueError("run is already terminal")
            db.execute(
                "INSERT INTO signals(id,run_id,type,data,created_at) VALUES(?,?,?,?,?)",
                (signal_id, run_id, type_, encoded, now_ms()),
            )
            self.journal.event(db, run_id, "signal.received", {"id": signal_id, "type": type_, "actor": actor})
            if type_ == "cancel" and run["state"] not in {"COMPLETED", "FAILED", "CANCELED"}:
                self._cancel_tree(db, run_id, actor)
            elif type_ == "effect_resolution":
                step = db.execute(
                    "SELECT * FROM steps WHERE id=? AND run_id=?", (data.get("step_id"), run_id)
                ).fetchone()
                if step is None or step["kind"] != "tool" or step["state"] != "RECONCILING":
                    raise ValueError("effect resolution target is not reconciling")
                if run["state"] not in {"WAITING", "CANCELING"}:
                    raise ValueError("run is not waiting for effect resolution")
                outcome = data.get("outcome")
                if outcome not in {"succeeded", "absent"}:
                    raise ValueError("effect outcome must be succeeded or absent")
                self.journal.event(
                    db,
                    run_id,
                    "effect.resolved",
                    {
                        "step_id": step["id"],
                        "effect_id": step["effect_id"],
                        "outcome": outcome,
                        "actor": actor,
                    },
                )
                if outcome == "succeeded":
                    result = data.get("result")
                    db.execute(
                        "UPDATE effects SET state='SUCCEEDED',result=?,updated_at=? WHERE id=?",
                        (dump(result), now_ms(), step["effect_id"]),
                    )
                    if run["state"] == "CANCELING":
                        self.journal.set_step(db, step["id"], "SUCCEEDED", result=result)
                        self._finish_cancellation(db, run_id)
                    else:
                        full_run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                        self._finish_tool(db, full_run, step, result)
                elif run["state"] == "CANCELING":
                    db.execute(
                        "UPDATE effects SET state='ABSENT',updated_at=? WHERE id=?", (now_ms(), step["effect_id"])
                    )
                    self.journal.set_step(db, step["id"], "CANCELED")
                    self._finish_cancellation(db, run_id)
                else:
                    db.execute(
                        "UPDATE effects SET state='PENDING',updated_at=? WHERE id=?", (now_ms(), step["effect_id"])
                    )
                    self.journal.set_step(db, step["id"], "READY")
                    self.journal.set_run(db, run_id, "RUNNING", error=None)
            elif type_ == "input":
                step = db.execute(
                    "SELECT id FROM steps WHERE run_id=? AND state='WAITING_INPUT' ORDER BY ordinal LIMIT 1",
                    (run_id,),
                ).fetchone()
                if step is None or data.get("step_id") != step["id"]:
                    raise ValueError("input target is not waiting")
                messages = load(db.execute("SELECT messages FROM runs WHERE id=?", (run_id,)).fetchone()["messages"])
                messages.append(
                    {
                        "role": "user",
                        "content": str(data.get("content") or ""),
                        "images": list(data.get("images") or []),
                    }
                )
                self.journal.set_step(db, step["id"], "READY")
                self.journal.set_run(db, run_id, "RUNNING", messages=messages)
            elif type_ == "approval":
                step_id = data.get("step_id")
                step = db.execute(
                    "SELECT state,payload FROM steps WHERE id=? AND run_id=?", (step_id, run_id)
                ).fetchone()
                if step is None or step["state"] != "WAITING_APPROVAL":
                    raise ValueError("approval target is not waiting")
                digest = hashlib.sha256(step["payload"].encode("utf-8")).hexdigest()
                if data.get("arguments_sha256") != digest:
                    raise ValueError("approval arguments do not match the pending tool call")
                if data.get("approved"):
                    self.journal.set_step(db, step_id, "READY")
                    self.journal.set_run(db, run_id, "RUNNING")
                else:
                    self.journal.set_step(db, step_id, "FAILED")
                    self.journal.set_run(db, run_id, "FAILED", error="approval denied")
            else:
                if type_ not in {"approval", "cancel"}:
                    raise ValueError(type_)
        return True

    def reconcile_effect(self, run_id: str, step_id: str, *, actor: str) -> bool:
        """Confirm an uncertain effect through the host's external status lookup."""
        if not actor:
            raise ValueError("reconciliation actor is required")
        self.journal.audit(run_id)
        with self.journal.connection() as db:
            step = db.execute("SELECT * FROM steps WHERE id=? AND run_id=?", (step_id, run_id)).fetchone()
            if step is None or step["state"] != "RECONCILING":
                return False
            call = ToolCall(**load(step["payload"]))
            effect_id = step["effect_id"]
        finding = self.tools.reconcile(call, effect_id=effect_id)
        if finding is None:
            return False
        confirmed, result = finding
        with self.journal.tx() as db:
            step = db.execute("SELECT * FROM steps WHERE id=? AND run_id=?", (step_id, run_id)).fetchone()
            if step is None or step["state"] != "RECONCILING":
                return False
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if confirmed:
                db.execute(
                    "INSERT INTO effects(id,step_id,state,result,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,result=excluded.result,updated_at=excluded.updated_at",
                    (effect_id, step_id, "SUCCEEDED", dump(result), now_ms()),
                )
                self.journal.event(db, run_id, "effect.reconciled", {"step_id": step_id, "actor": actor})
                if run["state"] == "CANCELING":
                    self.journal.set_step(db, step_id, "SUCCEEDED", result=result)
                    self._finish_cancellation(db, run_id)
                else:
                    self._finish_tool(db, run, step, result)
            else:
                if run["state"] == "CANCELING":
                    self.journal.set_step(db, step_id, "CANCELED")
                    self._finish_cancellation(db, run_id)
                else:
                    self.journal.set_step(db, step_id, "READY")
                    self.journal.set_run(db, run_id, "RUNNING")
        return True

    def settle_model_uncertainty(self, run_id: str, step_id: str, *, usage_micro: int, actor: str) -> bool:
        """Record a provider-confirmed charge before closing or retrying an unknown call."""
        if not actor or usage_micro < 0:
            raise ValueError("confirmed usage and actor are required")
        self.journal.audit(run_id)
        with self.journal.tx() as db:
            run = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            step = db.execute(
                "SELECT kind,state,result FROM steps WHERE id=? AND run_id=?", (step_id, run_id)
            ).fetchone()
            if (
                not run
                or not step
                or run["state"] not in {"CANCELING", "WAITING"}
                or step["kind"] != "model"
                or step["state"] != "RECONCILING"
            ):
                return False
            self.journal.settle(db, run_id, step_id, usage_micro)
            self.journal.event(db, run_id, "model.usage_reconciled", {"step_id": step_id, "actor": actor})
            if run["state"] == "CANCELING":
                self.journal.set_step(db, step_id, "CANCELED")
                self._finish_cancellation(db, run_id)
            elif step["result"] is not None:
                full_run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                result = load(step["result"])
                result["usage_micro"] = usage_micro
                self._finish_model(db, full_run, step_id, result)
            else:
                self.journal.set_step(db, step_id, "FAILED")
                full_run = db.execute("SELECT spec FROM runs WHERE id=?", (run_id,)).fetchone()
                count = db.execute("SELECT COUNT(*) AS n FROM steps WHERE run_id=?", (run_id,)).fetchone()["n"]
                if count >= load(full_run["spec"])["max_steps"]:
                    self.journal.set_run(db, run_id, "FAILED", error="max_steps exceeded")
                else:
                    self.journal.new_step(db, run_id, "model", "READY", {})
                    self.journal.set_run(db, run_id, "RUNNING", error=None)
        return True

    def resume(self, run_id: str) -> dict[str, Any]:
        """Idempotently request progress; never resurrect an already claimed step."""
        while self.tick(run_id):
            pass
        result = self.get(run_id)
        if result is None:
            raise KeyError(run_id)
        return result

    def tick(self, run_id: str) -> bool:
        self.journal.audit(run_id)
        with self.journal.tx() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            self._refresh_tree_joins(db, run_id)
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run["state"] in {"COMPLETED", "FAILED", "CANCELED", "CANCELING"}:
                return False
            step = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND state IN ('READY','WAITING_ADMISSION','IN_FLIGHT','RECONCILING') ORDER BY ordinal LIMIT 1",
                (run_id,),
            ).fetchone()
            if step is None:
                return False
            if step["state"] in {"WAITING_ADMISSION", "RECONCILING"}:
                wait = db.execute("SELECT retry_at FROM admission_waits WHERE step_id=?", (step["id"],)).fetchone()
                if wait and wait["retry_at"] > now_ms():
                    return False
            if step["state"] == "IN_FLIGHT" and step["lease_until"] and step["lease_until"] > now_ms():
                return False
            if step["state"] == "IN_FLIGHT":
                db.execute(
                    "UPDATE attempts SET status='UNKNOWN',ended_at=?,error=? "
                    "WHERE step_id=? AND epoch=? AND status='IN_FLIGHT'",
                    (now_ms(), "lease expired before result commit", step["id"], step["lease_epoch"]),
                )
                self.journal.event(
                    db, run_id, "attempt.lease_expired", {"step_id": step["id"], "epoch": step["lease_epoch"]}
                )
                self.journal.set_step(db, step["id"], "RECONCILING")
                step = db.execute("SELECT * FROM steps WHERE id=?", (step["id"],)).fetchone()
            spec = load(run["spec"])
            payload = load(step["payload"])
            if step["kind"] == "tool" and step["state"] == "RECONCILING":
                effect = db.execute("SELECT * FROM effects WHERE id=?", (step["effect_id"],)).fetchone()
                if effect and effect["state"] == "SUCCEEDED":
                    self._finish_tool(db, run, step, load(effect["result"]))
                    return True
                tool_spec = next(x for x in spec["tools"] if x["name"] == payload["name"])
                if tool_spec["effect"] == "external_effect" or (
                    tool_spec["effect"] == "read_only" and not tool_spec["retry_safe"]
                ):
                    self.journal.set_run(db, run_id, "WAITING")
                    return False
            if step["kind"] == "model" and step["state"] == "RECONCILING":
                self.journal.set_run(db, run_id, "WAITING")
                return False
            if step["kind"] == "tool":
                tool_spec = next(x for x in spec["tools"] if x["name"] == payload["name"])
                approved = False
                if tool_spec["requires_approval"]:
                    for signal in db.execute(
                        "SELECT data,created_at FROM signals WHERE run_id=? AND type='approval'", (run_id,)
                    ).fetchall():
                        signal_data = load(signal["data"])
                        if (
                            signal_data.get("step_id") == step["id"]
                            and signal_data.get("approved")
                            and signal_data.get("arguments_sha256")
                            == hashlib.sha256(step["payload"].encode("utf-8")).hexdigest()
                            and now_ms() < signal["created_at"] + tool_spec.get("approval_ttl_ms", 86400000)
                        ):
                            approved = True
                            break
                if tool_spec["requires_approval"] and not approved:
                    self.journal.set_step(db, step["id"], "WAITING_APPROVAL")
                    self.journal.event(
                        db,
                        run_id,
                        "approval.required",
                        {
                            "step_id": step["id"],
                            "tool": payload["name"],
                            "arguments_sha256": hashlib.sha256(step["payload"].encode("utf-8")).hexdigest(),
                        },
                    )
                    self.journal.set_run(db, run_id, "WAITING")
                    return False
            if step["kind"] == "tool":
                from .scheduler import RuntimeScheduler

                blocked = RuntimeScheduler.tool_admission(db, run, step, spec, payload)
                if blocked is not None:
                    retry_at = now_ms() + 1000
                    db.execute(
                        "INSERT INTO admission_waits(step_id,retry_at) VALUES(?,?) "
                        "ON CONFLICT(step_id) DO UPDATE SET retry_at=excluded.retry_at",
                        (step["id"], retry_at),
                    )
                    self.journal.event(
                        db, run_id, "scheduler.admission_wait", {"step_id": step["id"], "retry_at": retry_at, **blocked}
                    )
                    if step["state"] != "RECONCILING":
                        self.journal.set_step(db, step["id"], "WAITING_ADMISSION", lease_owner=None, lease_until=None)
                    self.journal.set_run(db, run_id, "WAITING", error=None)
                    return False
            if step["kind"] == "model" and step["attempt_count"] == 0:
                if not self.journal.reserve(db, run_id, step["id"], spec["model_reservation_micro"]):
                    self.journal.set_step(db, step["id"], "WAITING_ADMISSION")
                    self.journal.set_run(db, run_id, "WAITING")
                    return False
            db.execute("DELETE FROM admission_waits WHERE step_id=?", (step["id"],))
            epoch = step["lease_epoch"] + 1
            attempt_id = uuid.uuid4().hex
            db.execute(
                "UPDATE steps SET state='IN_FLIGHT',lease_owner=?,lease_epoch=?,lease_until=?,attempt_count=attempt_count+1 WHERE id=?",
                (self.worker_id, epoch, now_ms() + self.lease_ms, step["id"]),
            )
            db.execute(
                "INSERT INTO attempts(id,step_id,epoch,status,started_at) VALUES(?,?,?,?,?)",
                (attempt_id, step["id"], epoch, "IN_FLIGHT", now_ms()),
            )
            self.journal.event(
                db, run_id, "attempt.started", {"attempt_id": attempt_id, "step_id": step["id"], "epoch": epoch}
            )
            if step["kind"] == "tool":
                db.execute(
                    "UPDATE effects SET state='DISPATCHED',updated_at=? WHERE id=?", (now_ms(), step["effect_id"])
                )
                self.journal.event(
                    db, run_id, "effect.dispatched", {"step_id": step["id"], "effect_id": step["effect_id"]}
                )
            self.journal.set_run(db, run_id, "RUNNING")
            messages = load(run["messages"])
        try:
            result = self._execute_with_lease(run_id, step, epoch, messages, payload, attempt_id)
        except ModelAdmissionPending as exc:
            with self.journal.tx() as db:
                if not self._owns(db, step["id"], epoch, run_id):
                    late = db.execute(
                        "SELECT s.state,s.lease_epoch,s.lease_until,r.state AS run_state "
                        "FROM steps s JOIN runs r ON r.id=s.run_id WHERE s.id=? AND r.id=?",
                        (step["id"], run_id),
                    ).fetchone()
                    if (
                        step["kind"] == "model"
                        and late
                        and late["run_state"] == "CANCELING"
                        and late["state"] == "RECONCILING"
                        and late["lease_epoch"] == epoch
                        and (late["lease_until"] or 0) > now_ms()
                    ):
                        db.execute(
                            "UPDATE attempts SET status='NOT_ADMITTED',ended_at=?,error=? WHERE id=?",
                            (now_ms(), str(exc), attempt_id),
                        )
                        self.journal.settle(db, run_id, step["id"], 0)
                        self.journal.set_step(db, step["id"], "CANCELED", lease_owner=None, lease_until=None)
                        self._finish_cancellation(db, run_id)
                    return False
                if step["kind"] != "model":
                    # A tool must never turn an unknown external effect into a retry.
                    db.execute(
                        "UPDATE attempts SET status='UNKNOWN',ended_at=?,error=? WHERE id=?",
                        (now_ms(), "invalid tool admission exception", attempt_id),
                    )
                    self.journal.set_step(db, step["id"], "RECONCILING")
                    self.journal.set_run(db, run_id, "WAITING", error="tool result requires reconciliation")
                    return False
                retry_at = now_ms() + exc.retry_after_ms
                db.execute(
                    "UPDATE attempts SET status='NOT_ADMITTED',ended_at=?,error=? WHERE id=?",
                    (now_ms(), str(exc), attempt_id),
                )
                db.execute(
                    "INSERT INTO admission_waits(step_id,retry_at) VALUES(?,?) "
                    "ON CONFLICT(step_id) DO UPDATE SET retry_at=excluded.retry_at",
                    (step["id"], retry_at),
                )
                self.journal.event(
                    db,
                    run_id,
                    "model.admission_wait",
                    {
                        "step_id": step["id"],
                        "attempt_id": attempt_id,
                        "retry_at": retry_at,
                    },
                )
                self.journal.set_step(db, step["id"], "WAITING_ADMISSION", lease_owner=None, lease_until=None)
                self.journal.set_run(db, run_id, "WAITING", error=None)
            return False
        except ModelUnavailable as exc:
            # These failures are definitive: admission rejected the call before
            # dispatch, or the provider result is already settled but invalid.
            # Marking them RECONCILING would strand the run waiting forever.
            with self.journal.tx() as db:
                if not self._owns(db, step["id"], epoch, run_id):
                    return False
                db.execute(
                    "UPDATE attempts SET status='FAILED',ended_at=?,error=? WHERE id=?",
                    (now_ms(), repr(exc), attempt_id),
                )
                if step["kind"] == "model":
                    self.journal.settle(db, run_id, step["id"], 0)
                self.journal.set_step(db, step["id"], "FAILED")
                self.journal.set_run(db, run_id, "FAILED", error=str(exc))
            return False
        except Exception as exc:
            with self.journal.tx() as db:
                if not self._owns(db, step["id"], epoch, run_id):
                    return False
                db.execute(
                    "UPDATE attempts SET status='UNKNOWN',ended_at=?,error=? WHERE id=?",
                    (now_ms(), repr(exc), attempt_id),
                )
                self.journal.set_step(db, step["id"], "RECONCILING")
                self.journal.set_run(db, run_id, "WAITING", error=repr(exc))
            return False
        with self.journal.tx() as db:
            if not self._owns(db, step["id"], epoch, run_id):
                return False
            db.execute("UPDATE attempts SET status='SUCCEEDED',ended_at=? WHERE id=?", (now_ms(), attempt_id))
            if step["kind"] == "model":
                self.journal.settle(db, run_id, step["id"], result.usage_micro)
                if result.usage_micro is None:
                    self.journal.set_step(db, step["id"], "RECONCILING", result=asdict(result))
                    self.journal.set_run(db, run_id, "WAITING", error="model usage is unconfirmed")
                else:
                    self._finish_model(db, run, step["id"], asdict(result))
            else:
                db.execute(
                    "INSERT INTO effects(id,step_id,state,result,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,result=excluded.result,updated_at=excluded.updated_at",
                    (step["effect_id"], step["id"], "SUCCEEDED", dump(result), now_ms()),
                )
                self._finish_tool(db, run, step, result)
        return True

    def _finish_model(self, db: Any, run: Any, step_id: str, result: dict[str, Any]) -> None:
        run_id = run["id"]
        spec = load(run["spec"])
        self.journal.set_step(db, step_id, "SUCCEEDED", result=result)
        calls = result.get("tool_calls") or ([result["tool_call"]] if result.get("tool_call") else [])
        if calls:
            tool_count = db.execute(
                "SELECT COUNT(*) AS n FROM steps WHERE run_id=? AND kind='tool'", (run_id,)
            ).fetchone()["n"]
            if any(tool["name"] not in {item["name"] for item in spec["tools"]} for tool in calls):
                self.journal.set_run(db, run_id, "FAILED", error="unknown tool")
            elif spec.get("max_tool_calls") is not None and tool_count + len(calls) > spec["max_tool_calls"]:
                self.journal.set_run(db, run_id, "FAILED", error="max_tool_calls exceeded")
            elif (
                db.execute("SELECT COUNT(*) AS n FROM steps WHERE run_id=?", (run_id,)).fetchone()["n"] + len(calls)
                > spec["max_steps"]
            ):
                self.journal.set_run(db, run_id, "FAILED", error="max_steps exceeded")
            else:
                identified = [{"id": uuid.uuid4().hex, **tool} for tool in calls]
                messages = load(run["messages"])
                messages.append(
                    {
                        "role": "assistant",
                        "content": result["text"],
                        "tool_calls": identified,
                        "provider_blocks": result.get("provider_blocks", []),
                        "reasoning": result.get("thinking", ""),
                    }
                )
                db.execute("UPDATE runs SET messages=? WHERE id=?", (dump(messages), run_id))
                for tool in identified:
                    self.journal.new_step(
                        db,
                        run_id,
                        "tool",
                        "READY",
                        {"name": tool["name"], "arguments": tool["arguments"]},
                        effect_id=tool["id"],
                    )
                self.journal.set_run(db, run_id, "RUNNING", error=None)
        else:
            self.journal.set_run(db, run_id, "COMPLETED", output=result["text"], error=None)

    @staticmethod
    def _owns(db: Any, step_id: str, epoch: int, run_id: str) -> bool:
        row = db.execute(
            "SELECT s.lease_epoch,s.lease_until,s.state,r.state AS run_state FROM steps s JOIN runs r ON r.id=s.run_id WHERE s.id=? AND r.id=?",
            (step_id, run_id),
        ).fetchone()
        return bool(
            row
            and row["lease_epoch"] == epoch
            and row["state"] == "IN_FLIGHT"
            and row["run_state"] == "RUNNING"
            and (row["lease_until"] or 0) > now_ms()
        )

    def _execute_with_lease(
        self, run_id: str, step: Any, epoch: int, messages: Any, payload: Any, attempt_id: str
    ) -> Any:
        stopped = threading.Event()

        def renew():
            while not stopped.wait(self.lease_ms / 3000):
                try:
                    with self.journal.tx() as db:
                        if not self._owns(db, step["id"], epoch, run_id):
                            return
                        db.execute(
                            "UPDATE steps SET lease_until=? WHERE id=? AND lease_epoch=?",
                            (now_ms() + self.lease_ms, step["id"], epoch),
                        )
                except Exception:
                    logging.getLogger(__name__).exception("lease renewal failed for %s", step["id"])
                    return

        heartbeat = threading.Thread(target=renew, daemon=True, name=f"runtime-lease-{step['id']}")
        heartbeat.start()
        try:
            if step["kind"] == "model":
                from .observations import model_observation, model_trace_context

                recorded = False

                def first_token() -> bool:
                    nonlocal recorded
                    if recorded:
                        return True
                    with self.journal.tx() as db:
                        if not self._owns(db, step["id"], epoch, run_id):
                            return False
                        db.execute(
                            "INSERT INTO attempt_timings(attempt_id,first_token_at) VALUES(?,?) "
                            "ON CONFLICT(attempt_id) DO NOTHING",
                            (attempt_id, now_ms()),
                        )
                    recorded = True
                    return True

                trace_context = None
                try:
                    with self.journal.read_tx() as db:
                        origin = db.execute("SELECT id,parent_id,tenant_id FROM runs WHERE id=?", (run_id,)).fetchone()
                        root = origin
                        seen = set()
                        while root and root["parent_id"]:
                            if root["id"] in seen or len(seen) >= 4096:
                                raise ValueError("invalid trace ancestry")
                            seen.add(root["id"])
                            root = db.execute(
                                "SELECT id,parent_id,tenant_id FROM runs WHERE id=?", (root["parent_id"],)
                            ).fetchone()
                            if root is None or root["tenant_id"] != origin["tenant_id"]:
                                raise ValueError("invalid trace tenant ancestry")
                        if root:
                            trace_context = model_trace_context(root["id"], attempt_id)
                except Exception:
                    logging.getLogger(__name__).exception("model trace context unavailable")
                with model_observation(first_token, trace_context=trace_context):
                    return self.model.generate(messages, request_id=step["id"])
            return self.tools.execute(ToolCall(**payload), effect_id=step["effect_id"])
        finally:
            stopped.set()
            heartbeat.join(timeout=1)

    def _finish_tool(self, db: Any, run: Any, step: Any, result: Any) -> None:
        messages = load(run["messages"])
        messages.append(
            {
                "role": "tool",
                "name": load(step["payload"])["name"],
                "tool_call_id": step["effect_id"],
                "content": json.dumps(result, ensure_ascii=False),
            }
        )
        self.journal.set_step(db, step["id"], "SUCCEEDED", result=result)
        db.execute("UPDATE runs SET messages=? WHERE id=?", (dump(messages), run["id"]))
        spec = load(run["spec"])
        pending = db.execute(
            "SELECT id FROM steps WHERE run_id=? AND kind='tool' AND state NOT IN ('SUCCEEDED','FAILED','CANCELED') LIMIT 1",
            (run["id"],),
        ).fetchone()
        if pending is not None:
            self.journal.set_run(db, run["id"], "RUNNING")
            return
        count = db.execute("SELECT COUNT(*) AS n FROM steps WHERE run_id=?", (run["id"],)).fetchone()["n"]
        if count >= spec["max_steps"]:
            self.journal.set_run(db, run["id"], "FAILED", error="max_steps exceeded")
        else:
            last_model = db.execute(
                "SELECT MAX(ordinal) AS n FROM steps WHERE run_id=? AND kind='model'", (run["id"],)
            ).fetchone()["n"]
            batch = db.execute(
                "SELECT payload FROM steps WHERE run_id=? AND kind='tool' AND ordinal>?", (run["id"], last_model)
            ).fetchall()
            pause_names = {tool["name"] for tool in spec["tools"] if tool.get("pause_after")}
            pause = any(load(tool["payload"])["name"] in pause_names for tool in batch)
            self.journal.new_step(db, run["id"], "model", "WAITING_INPUT" if pause else "READY", {})
            self.journal.set_run(db, run["id"], "WAITING" if pause else "RUNNING")
