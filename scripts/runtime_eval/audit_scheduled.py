"""Fault probes of current scheduled delivery wrappers; no external notifications."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages/agent-runtime/src"))


def child(root):
    from agent_runtime import Journal

    from quant_agent.agents.long_running import DailyDigestAgent, DigestData
    from quant_agent.llm.base import ChatResult
    from quant_agent.notify.channels import NotificationHub
    from quant_agent.platform import context

    class Router:
        runtime_journal = Journal(root / "shared.sqlite")

        def chat(self, *args, **kwargs):
            with (root / "provider-receipts.jsonl").open("a") as stream:
                stream.write(json.dumps({"pid": os.getpid()}) + "\n")
            return ChatResult(content="Fixed digest", provider="mock", model="fixed")

    class Capture:
        def send(self, note):
            with (root / "notification-receipts.jsonl").open("a") as stream:
                stream.write(json.dumps({"pid": os.getpid()}) + "\n")
            return True

    agent = DailyDigestAgent(
        router=Router(),
        hub=NotificationHub({"capture": Capture()}),
        channel="capture",
        collect_data=lambda: DigestData("2026-10-25", 10000, 0),
    )
    with context.bind("ws", user_id="user", role="member"):
        record = agent.run_once(date="2026-10-25")
    return record.to_dict()


def audit_worker(root, kind):
    from agent_runtime import Journal

    from quant_agent.api.deps import AppServices, reset_services, set_services
    from quant_agent.api.tasks import handle_agent_job
    from quant_agent.llm.base import ChatResult, ToolCall
    from quant_agent.notify.channels import NotificationHub
    from quant_agent.platform.worker import Worker

    reset_services()
    services = AppServices.build(store_root=str(root / "host"))
    set_services(services)
    services.db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "audit@example.test", "User", "hash", 1),
    )
    services.db.execute(
        "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("ws", "Audit", "user", 1)
    )
    journal = Journal(root / "generation.sqlite")
    sent = []
    effects = []

    class Router:
        runtime_journal = journal
        calls = 0

        def chat(self, *args, **kwargs):
            self.calls += 1
            if kind == "digest":
                raise OSError("injected provider result uncertainty")
            if self.calls == 1:
                return ChatResult(
                    content="",
                    provider="mock",
                    model="fixed",
                    tool_calls=[ToolCall("candidate", "propose_candidate", {"payload": {"id": "candidate"}})],
                )
            return ChatResult(content="candidate ready", provider="mock", model="fixed")

    class Capture:
        def send(self, note):
            sent.append(note.body)
            return True

    services.router = Router()
    services.hub = NotificationHub({"capture": Capture()})
    # Route's maintainer has no external version service; inspect that its stub
    # effect is durably recorded instead of inventing a publication failure.
    run = services.runs.create(
        workspace_id="ws",
        user_id="user",
        kind="agent_job",
        mode="agent",
        title="Audit",
        manifest={"job": {"id": "audit", "kind": kind, "channel": "capture"}},
        idempotency_key="audit",
    )
    worker = Worker(services, batch={"agent_job": handle_agent_job}, resident={}, slots=1, periodic=[])
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            finished = worker.execute_now(run["id"])
        with journal.connection() as db:
            rows = [dict(row) for row in db.execute("SELECT id,state FROM runs").fetchall()]
        from quant_agent.platform import context

        with context.bind("ws", user_id="user", role="member"):
            records = services.store.list("notifications")
        return {
            "kind": kind,
            "delivery_state": finished["state"],
            "delivery_summary": finished["summary"],
            "generation_runs": rows,
            "notification_count": len(sent),
            "provider_calls": services.router.calls,
            "records": records,
            "effects": effects,
        }
    finally:
        worker.shutdown()
        reset_services()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.child:
        print(json.dumps(child(args.output), ensure_ascii=False))
        return
    rows = []
    for kind in ("digest", "maintainer"):
        folder = args.output / kind
        folder.mkdir(exist_ok=True)
        rows.append(audit_worker(folder, kind))
    repeat = args.output / "cross-process"
    repeat.mkdir(exist_ok=True)
    records = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--output", str(repeat), "--child"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        records.append(json.loads(result.stdout))
    counts = {
        kind: len((repeat / f"{kind}-receipts.jsonl").read_text().splitlines()) for kind in ("provider", "notification")
    }
    payload = {
        "worker_probes": rows,
        "same_run_key_new_processes": {"records": records, **counts},
        "external_notifications_sent": 0,
    }
    (args.output / "audit.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "worker_states": [(r["kind"], r["delivery_state"], r["generation_runs"]) for r in rows],
                "cross_process_counts": counts,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
