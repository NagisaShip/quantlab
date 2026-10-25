"""Business identities and delivery effects survive ambiguity and host restart."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "packages/agent-runtime/src"))
from agent_runtime import AgentRuntime, Journal

from quant_agent.agents.long_running import DailyDigestAgent, DigestData, StrategyMaintainerAgent
from quant_agent.llm.base import ChatResult
from quant_agent.notify.channels import NotificationHub
from quant_agent.platform import context


class Router:
    def __init__(self, journal):
        self.runtime_journal = journal
        self.calls = 0

    def chat(self, *args, **kwargs):
        self.calls += 1
        return ChatResult(content="Verified digest", provider="fake", model="test")


class Notifier:
    def __init__(self):
        self.receipts = []
        self.fail = False

    def send(self, note):
        self.receipts.append(note.body)
        if self.fail:
            raise OSError("external success, acknowledgement lost")
        return True


def make(router, notifier, *, nav=10000):
    return DailyDigestAgent(
        router=router,
        hub=NotificationHub({"capture": notifier}),
        channel="capture",
        collect_data=lambda: DigestData("2026-10-25", nav, 0),
    )


def test_restart_reuses_generation_and_confirmed_delivery_not_new_inputs(tmp_path):
    journal = Journal(tmp_path / "runtime.sqlite")
    router = Router(journal)
    notifier = Notifier()
    with context.bind("ws", user_id="owner", role="member"):
        first = make(router, notifier).run_once()
        router.runtime_journal = Journal(journal.path)
        again = make(router, notifier, nav=99999).run_once()
        assert first.id == again.id and first.runtime_run_id == again.runtime_run_id
        assert first.delivery_effect_id == again.delivery_effect_id and first.input_digest == again.input_digest
        assert first.status == again.status == "ok" and first.delivery_state == "sent"
    assert router.calls == 1 and len(notifier.receipts) == 1
    journal.audit(first.runtime_run_id)


def test_unknown_model_does_not_send_template_and_confirmed_usage_can_resume(tmp_path):
    journal = Journal(tmp_path / "unknown.sqlite")
    notifier = Notifier()

    class Unknown(Router):
        def chat(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                raise OSError("provider outcome unknown")
            return ChatResult(content="Confirmed", provider="fake", model="test")

    router = Unknown(journal)
    with context.bind("ws", user_id="owner", role="member"):
        waiting = make(router, notifier).run_once()
        assert waiting.status == "waiting" and waiting.runtime_state == "WAITING" and waiting.output == ""
        assert not notifier.receipts
        assert make(router, notifier).run_once().runtime_run_id == waiting.runtime_run_id
        assert router.calls == 1
        engine = AgentRuntime(journal, None, None)
        step = engine.get(waiting.runtime_run_id)["steps"][0]
        assert engine.settle_model_uncertainty(waiting.runtime_run_id, step["id"], usage_micro=0, actor="reviewer")
        assert make(router, notifier).run_once().status == "ok"
    assert router.calls == 2 and len(notifier.receipts) == 1


def test_unknown_notification_is_not_put_into_legacy_retry_queue(tmp_path):
    journal = Journal(tmp_path / "notify.sqlite")
    router = Router(journal)
    notifier = Notifier()
    notifier.fail = True
    with context.bind("ws", user_id="owner", role="member"):
        agent = make(router, notifier)
        waiting = agent.run_once()
        assert waiting.status == "waiting" and waiting.delivery_state == "reconciling"
        assert not agent.hub.pending
        assert make(router, notifier).run_once().status == "waiting"
        assert len(notifier.receipts) == 1 and router.calls == 1
        engine = AgentRuntime(Journal(journal.path), None, None)
        step = next(s for s in engine.get(waiting.runtime_run_id)["steps"] if s["kind"] == "tool")
        arguments = step["payload"]["arguments"]
        result = {
            "sent": True,
            "body": arguments["body"],
            "source": arguments["source"],
            "effect_id": step["effect_id"],
        }
        engine.signal(
            waiting.runtime_run_id,
            "effect_resolution",
            {"step_id": step["id"], "outcome": "succeeded", "result": result},
            signal_id="receipt",
            actor="reviewer",
        )
        done = make(router, notifier).run_once()
        assert done.status == "ok" and done.delivery_effect_id == waiting.delivery_effect_id
    assert len(notifier.receipts) == 1 and router.calls == 1


def test_same_business_key_concurrent_hosts_only_send_once(tmp_path):
    journal = Journal(tmp_path / "concurrent.sqlite")
    notifier = Notifier()
    entered, release = threading.Event(), threading.Event()

    class Slow(Router):
        def chat(self, *a, **k):
            self.calls += 1
            entered.set()
            assert release.wait(10)
            return ChatResult(content="One result", provider="fake", model="test")

    router = Slow(journal)

    def run():
        with context.bind("ws", user_id="owner", role="member"):
            return make(router, notifier).run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(run)
        assert entered.wait(5)
        pending = run()
        assert pending.status == "waiting"
        release.set()
        done = future.result(timeout=10)
    assert pending.runtime_run_id == done.runtime_run_id
    assert router.calls == 1 and len(notifier.receipts) == 1


def test_preview_is_distinct_from_real_delivery_and_absent_candidate_is_not_advertised(tmp_path):
    router = Router(Journal(tmp_path / "preview.sqlite"))
    notifier = Notifier()
    with context.bind("ws", user_id="owner", role="member"):
        preview = make(router, notifier).run_once(dry_run=True)
        assert preview.delivery_state == "preview" and not notifier.receipts
        sent = make(router, notifier).run_once()
        assert sent.runtime_run_id != preview.runtime_run_id
    assert len(notifier.receipts) == 1
    maintainer = StrategyMaintainerAgent(router=router, hub=NotificationHub({}), collect_evidence=lambda: {})
    assert "propose_candidate" not in maintainer.agent.tools


def test_stale_record_projection_cannot_replace_confirmed_delivery(tmp_path):
    from quant_agent.store import JsonStore

    store = JsonStore(tmp_path / "store")
    store.upsert(
        "notifications", {"id": "same", "runtime_event_seq": 20, "status": "ok"}, version_key="runtime_event_seq"
    )
    result = store.upsert(
        "notifications", {"id": "same", "runtime_event_seq": 5, "status": "waiting"}, version_key="runtime_event_seq"
    )
    assert result["status"] == "ok" and store.get("notifications", "same")["runtime_event_seq"] == 20


def test_external_success_followed_by_transaction_failure_never_resends(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "commit-failure.sqlite")
    router, notifier = Router(journal), Notifier()
    finish = AgentRuntime._finish_tool

    def fail_commit(self, db, run, step, result):
        finish(self, db, run, step, result)
        raise OSError("injected database commit failure after external success")

    with context.bind("ws", user_id="owner", role="member"):
        with monkeypatch.context() as patch:
            patch.setattr(AgentRuntime, "_finish_tool", fail_commit)
            waiting = make(router, notifier).run_once()
        assert waiting.status == "waiting" and len(notifier.receipts) == 1
        state = AgentRuntime(journal, None, None).get(waiting.runtime_run_id)
        step = next(s for s in state["steps"] if s["kind"] == "tool")
        assert step["state"] == "IN_FLIGHT" and step["result"] is None
        assert make(router, notifier).run_once().status == "waiting"
        assert len(notifier.receipts) == 1
        # Expire the lease through the journal so the crash recovery protocol runs.
        with journal.tx() as db:
            db.execute("UPDATE steps SET lease_until=0 WHERE id=?", (step["id"],))
        pending = make(router, notifier).run_once()
        assert pending.delivery_state == "reconciling" and len(notifier.receipts) == 1
        journal.audit(waiting.runtime_run_id)
