"""ModelHub is the atomic quota authority for Runtime model requests."""

import json
import threading
from types import SimpleNamespace

import pytest

from quant_agent.api.deps import AppServices
from quant_agent.llm.base import ChatResult, LLMError, Message
from quant_agent.llm.hub import ModelHub, ModelHubAdmission
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig
from quant_agent.platform import context
from quant_agent.platform.db import Database
from quant_agent.platform.errors import AppError
from quant_agent.platform.quota import QuotaService, day_start_ms


def _hub(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'quantlab.sqlite').as_posix()}")
    db.migrate()
    hub = ModelHub(store=None, secrets=None, db=db)
    provider = SimpleNamespace(
        name="test",
        scope="personal",
        owner_id="user",
        group_id="",
        currency="CNY",
        fx_rate="1",
        limits=[{"metric": "cost", "window": "total", "limit": "0.000005"}],
        setting=lambda model: {"price_in": "1", "price_out": "1"},
    )
    return db, hub, provider


def _workspace_quota(db, **quotas):
    db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "user@example.test", "User", "hash", 1),
    )
    db.execute(
        "INSERT INTO workspaces(id,name,owner_id,settings,created_at) VALUES(?,?,?,?,?)",
        ("ws", "Workspace", "user", json.dumps({"quotas": quotas}), 1),
    )


def test_concurrent_admission_and_idempotent_settlement(tmp_path):
    db, hub, provider = _hub(tmp_path)
    barrier = threading.Barrier(2)
    accepted = []
    rejected = []

    def reserve(request_id):
        with context.bind("ws", user_id="user"):
            barrier.wait()
            try:
                accepted.append(
                    hub.admit(provider, "model", request_id=request_id, reserved_tokens=3, reserved_micro=3)
                )
            except AppError as exc:
                rejected.append(exc.code)

    threads = [threading.Thread(target=reserve, args=(f"request-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(accepted) == 1 and rejected == ["QUOTA_EXHAUSTED"]
    request_id = accepted[0]["request_id"]
    with context.bind("ws", user_id="user"):
        assert (
            hub.admit(provider, "model", request_id=request_id, reserved_tokens=3, reserved_micro=3)["request_id"]
            == request_id
        )
        assert hub.mark_dispatched(request_id)
        assert not hub.mark_dispatched(request_id)
        assert not hub.settle_admission(provider, "model", request_id, None)
        assert hub.guard(provider, "model") is None  # one more micro-unit remains
        with pytest.raises(AppError):
            hub.admit(provider, "model", request_id="blocked", reserved_tokens=3, reserved_micro=3)
        assert hub.settle_admission(provider, "model", request_id, {"input_tokens": 2, "output_tokens": 0})
        assert not hub.settle_admission(provider, "model", request_id, {"input_tokens": 2, "output_tokens": 0})
        with pytest.raises(AppError) as conflict:
            hub.settle_admission(provider, "model", request_id, {"input_tokens": 1, "output_tokens": 1})
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
        assert db.scalar("SELECT COUNT(*) FROM llm_usage WHERE admission_id=?", (request_id,)) == 1
        assert hub.admit(provider, "model", request_id="next", reserved_tokens=3, reserved_micro=3)


def test_workspace_daily_llm_call_quota_is_atomic_across_active_reservations(tmp_path):
    db, hub, provider = _hub(tmp_path)
    provider.limits = []
    _workspace_quota(db, max_llm_calls_per_day=1, max_llm_tokens_per_day=100)
    hub.quota = QuotaService(db)
    barrier = threading.Barrier(2)
    accepted, rejected = [], []

    def reserve(request_id):
        with context.bind("ws", user_id="user"):
            barrier.wait()
            try:
                accepted.append(
                    hub.admit(provider, "model", request_id=request_id, reserved_tokens=3, reserved_micro=3)
                )
            except AppError as exc:
                rejected.append(exc.code)

    threads = [threading.Thread(target=reserve, args=(f"daily-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(accepted) == 1 and rejected == ["QUOTA_EXHAUSTED"]
    request_id = accepted[0]["request_id"]
    with context.bind("ws", user_id="user"):
        assert hub.mark_dispatched(request_id)
        assert hub.settle_admission(provider, "model", request_id, {"input_tokens": 2, "output_tokens": 1})
        assert not hub.settle_admission(provider, "model", request_id, {"input_tokens": 2, "output_tokens": 1})
    assert hub.quota.daily(workspace_id="ws")["llm_calls"] == "1"
    assert hub.quota.daily(workspace_id="ws")["llm_tokens"] == "3"


def test_workspace_token_reservation_blocks_until_released(tmp_path):
    db, hub, provider = _hub(tmp_path)
    provider.limits = []
    _workspace_quota(db, max_llm_calls_per_day=10, max_llm_tokens_per_day=10)
    hub.quota = QuotaService(db)
    with context.bind("ws", user_id="user"):
        hub.admit(provider, "model", request_id="held", reserved_tokens=6, reserved_micro=6)
        with pytest.raises(AppError, match="在途预留"):
            hub.admit(provider, "model", request_id="blocked", reserved_tokens=5, reserved_micro=5)
        assert hub.release_admission("held")
        assert hub.admit(provider, "model", request_id="after-release", reserved_tokens=5, reserved_micro=5)


def test_delayed_settlement_uses_admission_day_across_midnight(tmp_path, monkeypatch):
    db, hub, provider = _hub(tmp_path)
    provider.limits = []
    _workspace_quota(db, max_llm_calls_per_day=1, max_llm_tokens_per_day=10)
    hub.quota = QuotaService(db)
    clock = [day_start_ms("2026-10-24") - 1]
    monkeypatch.setattr("quant_agent.llm.hub.now_ms", lambda: clock[0])
    with context.bind("ws", user_id="user"):
        hub.admit(provider, "model", request_id="yesterday", reserved_tokens=10, reserved_micro=10)
        hub.mark_dispatched("yesterday")
        clock[0] += 2
        hub.admit(provider, "model", request_id="today", reserved_tokens=10, reserved_micro=10)
        assert hub.settle_admission(provider, "model", "yesterday", {"input_tokens": 2, "output_tokens": 1})
        assert hub.quota.daily(workspace_id="ws", day="2026-10-23")["llm_tokens"] == "3"
        assert hub.quota.daily(workspace_id="ws", day="2026-10-24")["llm_calls"] == "0"
        with pytest.raises(AppError):
            hub.admit(provider, "model", request_id="blocked-today", reserved_tokens=1, reserved_micro=1)
        assert hub.settle_admission(provider, "model", "today", {"input_tokens": 1, "output_tokens": 1})
    assert hub.quota.daily(workspace_id="ws", day="2026-10-24")["llm_tokens"] == "2"
    assert hub.quota.daily(workspace_id="ws", day="2026-10-24")["llm_calls"] == "1"


def test_quota_commit_failure_rolls_back_entire_model_settlement(tmp_path, monkeypatch):
    db, hub, provider = _hub(tmp_path)
    provider.limits = []
    _workspace_quota(db, max_llm_calls_per_day=1, max_llm_tokens_per_day=10)
    hub.quota = QuotaService(db)
    with context.bind("ws", user_id="user"):
        hub.admit(provider, "model", request_id="atomic", reserved_tokens=10, reserved_micro=10)
        hub.mark_dispatched("atomic")
        add = hub.quota.add_in_transaction

        def fail_after_counter_write(tx, metric, *args, **kwargs):
            result = add(tx, metric, *args, **kwargs)
            if metric == "llm_tokens":
                raise OSError("injected quota commit failure")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(hub.quota, "add_in_transaction", fail_after_counter_write)
            with pytest.raises(OSError, match="quota commit failure"):
                hub.settle_admission(provider, "model", "atomic", {"input_tokens": 2, "output_tokens": 1})
        assert hub.admission("atomic")["status"] == "IN_FLIGHT"
        assert db.scalar("SELECT COUNT(*) FROM llm_usage") == 0
        assert hub.quota.daily(workspace_id="ws")["llm_calls"] == "0"
        assert hub.settle_admission(provider, "model", "atomic", {"input_tokens": 2, "output_tokens": 1})
        assert hub.quota.daily(workspace_id="ws")["llm_calls"] == "1"


def test_confirmed_no_charge_releases_reservation(tmp_path):
    db, hub, provider = _hub(tmp_path)
    with context.bind("ws", user_id="user"):
        hub.admit(provider, "model", request_id="lost", reserved_tokens=5, reserved_micro=5)
        assert hub.release_admission("lost")
        assert not hub.release_admission("lost")
        assert hub.admit(provider, "model", request_id="new", reserved_tokens=5, reserved_micro=5)
    assert db.scalar("SELECT COUNT(*) FROM llm_usage") == 0


def test_workspace_concurrency_holds_unknown_calls_but_releases_finished_response(tmp_path):
    db, hub, provider = _hub(tmp_path)
    provider.limits = []
    hub.max_inflight = 1
    with context.bind("ws", user_id="user"):
        hub.admit(provider, "model", request_id="slow", reserved_tokens=5, reserved_micro=5)
        hub.mark_dispatched("slow")
        hub.settle_admission(provider, "model", "slow", None)
        with pytest.raises(AppError, match="并发请求"):
            hub.admit(provider, "model", request_id="blocked", reserved_tokens=1, reserved_micro=1)
        assert hub.admission("slow")["response_received_at"] is None
        hub.settle_admission(provider, "model", "slow", None, response_confirmed=True)
        assert hub.admission("slow")["status"] == "UNSETTLED"
        assert hub.admission("slow")["reserved_micro"] == 5
        assert hub.admission("slow")["response_received_at"] is not None
        hub.admit(provider, "model", request_id="next", reserved_tokens=1, reserved_micro=1)
    assert db.scalar("SELECT COUNT(*) FROM llm_usage") == 0


def test_workspace_model_concurrency_limit_is_atomic(tmp_path):
    _, hub, provider = _hub(tmp_path)
    provider.limits = []
    hub.max_inflight = 1
    barrier = threading.Barrier(2)
    accepted, rejected = [], []

    def reserve(index):
        with context.bind("ws", user_id="user"):
            barrier.wait()
            try:
                accepted.append(
                    hub.admit(provider, "model", request_id=f"slot-{index}", reserved_tokens=1, reserved_micro=1)
                )
            except AppError as exc:
                rejected.append(exc.code)

    workers = [threading.Thread(target=reserve, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert len(accepted) == 1 and rejected == ["QUOTA_EXHAUSTED"]


def test_pending_reservation_blocks_legacy_guard_and_overrun_is_visible(tmp_path):
    db, hub, provider = _hub(tmp_path)
    with context.bind("ws", user_id="user"):
        hub.admit(provider, "model", request_id="pending", reserved_tokens=5, reserved_micro=5)
        assert hub.guard(provider, "model")
        assert hub.settle_admission(provider, "model", "pending", {"input_tokens": 7, "output_tokens": 0})
    row = db.fetchone("SELECT over_tokens,over_micro,status FROM llm_admissions WHERE request_id='pending'")
    assert row == {"over_tokens": 2, "over_micro": 2, "status": "SETTLED"}


def test_admission_uses_original_price_after_provider_changes(tmp_path):
    db, hub, provider = _hub(tmp_path)
    with context.bind("ws", user_id="user"):
        hub.admit(provider, "model", request_id="priced", reserved_tokens=3, reserved_micro=3)
        provider.setting = lambda model: {"price_in": "100", "price_out": "100"}
        assert hub.mark_dispatched("priced")
        assert hub.settle_admission(provider, "model", "priced", {"input_tokens": 2, "output_tokens": 1})
    assert db.scalar("SELECT cost_micro FROM llm_usage WHERE admission_id='priced'") == 3
    assert hub.admission("priced")["actual_micro"] == 3


def test_legacy_router_uses_atomic_admission_before_provider_call(tmp_path):
    db, hub, _ = _hub(tmp_path)
    provider = ProviderConfig(
        name="test",
        type="openai_compatible",
        base_url="http://localhost",
        api_key="key",
        models=["model"],
        scope="personal",
        owner_id="user",
        model_settings={"model": {"price_in": "1", "price_out": "1"}},
        limits=[{"metric": "cost", "window": "total", "limit": "0.000005"}],
    )
    router = ModelRouter(
        AgentConfig(providers={"test": provider}, routing={"default": "test/model"}, limits={}, trace={})
    )
    router.admission = ModelHubAdmission(hub)
    entered = threading.Event()
    release = threading.Event()

    class Client:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(5)
            return ChatResult(
                content="ok", model="model", provider="test", usage={"input_tokens": 2, "output_tokens": 1}
            )

    client = Client()
    router._client = lambda name, pc: client
    first = []

    def run_first():
        with context.bind("ws", user_id="user"):
            first.append(router.chat([Message(role="user", content="hello")], max_tokens=3))

    worker = threading.Thread(target=run_first)
    worker.start()
    assert entered.wait(5)
    with context.bind("ws", user_id="user"):
        with pytest.raises(LLMError, match="模型额度不足"):
            router.chat([Message(role="user", content="hello")], max_tokens=3)
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert first[0].content == "ok"
    assert client.calls == 1
    assert db.scalar("SELECT COUNT(*) FROM llm_admissions WHERE status='SETTLED'") == 1
    assert db.scalar("SELECT COUNT(*) FROM llm_usage") == 1


def test_app_services_routes_legacy_chat_through_single_hub_ledger(tmp_path):
    services = AppServices.build(store_root=str(tmp_path))
    assert isinstance(services.router.admission, ModelHubAdmission)
    services.db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "user@example.test", "User", "hash", 1),
    )
    services.db.execute(
        "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("ws", "Workspace", "user", 1)
    )
    provider = ProviderConfig(
        name="test",
        type="openai_compatible",
        base_url="http://localhost",
        api_key="key",
        models=["model"],
        scope="personal",
        owner_id="user",
        model_settings={"model": {"price_in": "1", "price_out": "1"}},
    )
    services.router.resolver = lambda base: AgentConfig(
        providers={"test": provider}, routing={"default": "test/model"}, limits={}, trace={}
    )

    class Client:
        def chat(self, messages, **kwargs):
            return ChatResult(
                content="ok", model="model", provider="test", usage={"input_tokens": 2, "output_tokens": 1}
            )

    services.router._client = lambda name, pc: Client()
    with context.bind("ws", user_id="user"):
        assert services.router.chat([Message(role="user", content="hello")], max_tokens=3).content == "ok"
    assert services.db.scalar("SELECT COUNT(*) FROM llm_admissions WHERE status='SETTLED'") == 1
    assert services.db.scalar("SELECT COUNT(*) FROM llm_usage") == 1
    assert services.quota.daily(workspace_id="ws")["llm_calls"] == "1"
    assert services.quota.daily(workspace_id="ws")["llm_tokens"] == "3"
