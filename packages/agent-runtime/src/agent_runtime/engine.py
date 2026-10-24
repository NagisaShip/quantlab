"""Small durable agent loop. The journal is the sole execution-state authority."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import asdict
from typing import Any

from .contracts import AgentSpec, ModelPort, ModelUnavailable, ToolCall, ToolPort
from .journal import Journal, dump, load, now_ms


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
        stamp = now_ms()
        with self.journal.tx() as db:
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
                db.execute(
                    "INSERT INTO budget_accounts(id,limit_micro) VALUES(?,?)", (account_id, spec.budget_limit_micro)
                )
                db.execute("INSERT INTO run_accounts(run_id,account_id) VALUES(?,?)", (run_id, account_id))
            self.journal.event(db, run_id, "run.created", {"run_id": run_id})
            self.journal.new_step(db, run_id, "model", "READY", {})
            self.journal.set_run(db, run_id, "RUNNING")
        return run_id

    def get(self, run_id: str) -> dict[str, Any] | None:
        run = self.journal.run(run_id)
        if run:
            run["steps"] = self.journal.steps(run_id)
        return run

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.journal.events(run_id, after)

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
                uncertain = False
                for step in db.execute("SELECT id,state FROM steps WHERE run_id=?", (run_id,)).fetchall():
                    if step["state"] not in {"SUCCEEDED", "FAILED", "CANCELED"}:
                        if step["state"] in {"IN_FLIGHT", "RECONCILING"}:
                            uncertain = True
                            self.journal.set_step(db, step["id"], "RECONCILING")
                        else:
                            self.journal.set_step(db, step["id"], "CANCELED")
                self.journal.set_run(db, run_id, "CANCELING" if uncertain else "CANCELED")
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
                        self.journal.set_run(db, run_id, "CANCELED")
                    else:
                        full_run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                        self._finish_tool(db, full_run, step, result)
                elif run["state"] == "CANCELING":
                    db.execute(
                        "UPDATE effects SET state='ABSENT',updated_at=? WHERE id=?", (now_ms(), step["effect_id"])
                    )
                    self.journal.set_step(db, step["id"], "CANCELED")
                    self.journal.set_run(db, run_id, "CANCELED")
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
                    self.journal.set_run(db, run_id, "CANCELED")
                else:
                    self._finish_tool(db, run, step, result)
            else:
                if run["state"] == "CANCELING":
                    self.journal.set_step(db, step_id, "CANCELED")
                    self.journal.set_run(db, run_id, "CANCELED")
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
                self.journal.set_run(db, run_id, "CANCELED")
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
            if run["state"] in {"COMPLETED", "FAILED", "CANCELED", "CANCELING"}:
                return False
            step = db.execute(
                "SELECT * FROM steps WHERE run_id=? AND state IN ('READY','WAITING_ADMISSION','IN_FLIGHT','RECONCILING') ORDER BY ordinal LIMIT 1",
                (run_id,),
            ).fetchone()
            if step is None:
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
            if step["kind"] == "model" and step["attempt_count"] == 0:
                if not self.journal.reserve(db, run_id, step["id"], spec["model_reservation_micro"]):
                    self.journal.set_step(db, step["id"], "WAITING_ADMISSION")
                    self.journal.set_run(db, run_id, "WAITING")
                    return False
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
            result = self._execute_with_lease(run_id, step, epoch, messages, payload)
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

    def _execute_with_lease(self, run_id: str, step: Any, epoch: int, messages: Any, payload: Any) -> Any:
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
