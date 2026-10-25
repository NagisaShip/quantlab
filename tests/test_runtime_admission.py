"""Runtime queue admission is atomic across concurrent API processes."""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from quant_agent.api.deps import AppServices
from quant_agent.platform.errors import AppError, ErrorCode


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME", "1")
    services = AppServices.build(store_root=str(tmp_path))
    services.db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "queue@test.local", "Queue", "hash", 1),
    )
    services.db.execute(
        "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("ws", "Queue", "user", 1)
    )
    return services


def submit(services, key="one", prompt="hello"):
    return services.runtime_bridge.create(
        workspace_id="ws",
        user_id="user",
        title="Queued",
        prompt=prompt,
        spec={"name": "test", "version": "1"},
        idempotency_key=key,
    )


@pytest.mark.parametrize("limiting", ["max_concurrent_runs", "max_pending_runtime_runs", "max_runs_per_day"])
def test_concurrent_ingress_cannot_overrun_workspace_limit(ingress, limiting):
    services = ingress
    quotas = {"max_concurrent_runs": 128, "max_pending_runtime_runs": 128, "max_runs_per_day": 200, limiting: 1}
    services.db.execute("UPDATE workspaces SET settings=? WHERE id=?", (json.dumps({"quotas": quotas}), "ws"))
    barrier = threading.Barrier(8)

    def create(index):
        barrier.wait(timeout=10)
        try:
            return submit(services, key=f"run-{index}")["id"]
        except AppError as exc:
            assert exc.code == ErrorCode.QUOTA_EXHAUSTED
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(8)))
    assert sum(value is not None for value in ids) == 1
    assert services.db.scalar("SELECT COUNT(*) FROM runs WHERE workspace_id='ws'") == 1
    assert services.db.scalar("SELECT COUNT(*) FROM runtime_tickets") == 1
    assert services.db.scalar("SELECT COUNT(*) FROM run_events") == 1


def test_concurrent_idempotency_reuses_ticket_even_when_all_limits_are_full(ingress):
    services = ingress
    services.db.execute(
        "UPDATE workspaces SET settings=? WHERE id=?",
        (
            json.dumps(
                {
                    "quotas": {
                        "max_concurrent_runs": 1,
                        "max_pending_runtime_runs": 1,
                        "max_runs_per_day": 1,
                    }
                }
            ),
            "ws",
        ),
    )
    barrier = threading.Barrier(8)

    def create(_):
        barrier.wait(timeout=10)
        return submit(services)["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(create, range(8)))
    assert len(set(ids)) == 1
    assert services.db.scalar("SELECT COUNT(*) FROM runtime_tickets") == 1
    with pytest.raises(ValueError, match="different runtime input"):
        submit(services, prompt="changed")
    assert services.db.scalar("SELECT COUNT(*) FROM run_events") == 1


def test_terminal_projection_releases_pending_capacity_without_resetting_daily_count(ingress):
    services = ingress
    services.db.execute(
        "UPDATE workspaces SET settings=? WHERE id=?",
        (
            json.dumps(
                {
                    "quotas": {
                        "max_concurrent_runs": 1,
                        "max_pending_runtime_runs": 1,
                        "max_runs_per_day": 2,
                    }
                }
            ),
            "ws",
        ),
    )
    first = submit(services)
    bridge = services.runtime_bridge
    bridge.deliver(first["id"])
    assert bridge.cancel(first["id"], workspace_id="ws", actor="user", signal_id="cancel")
    second = submit(services, key="second")
    assert first["id"] != second["id"]
    bridge.deliver(second["id"])
    bridge.cancel(second["id"], workspace_id="ws", actor="user", signal_id="cancel-second")
    with pytest.raises(AppError, match="今日运行次数"):
        submit(services, key="third")
    assert services.db.scalar("SELECT COUNT(*) FROM runtime_tickets") == 2


def capturing_worker(services, slots):
    from concurrent.futures import Future
    from types import SimpleNamespace

    from quant_agent.platform.worker import Worker

    services.runtime_bridge.runtime = SimpleNamespace(get=lambda rid: None)
    worker = Worker(services, batch={}, resident={}, slots=slots, periodic=[])
    worker.pool.shutdown(wait=True)

    class Pool:
        submitted = []

        def submit(self, function, rid):
            self.submitted.append(rid)
            return Future()

        def shutdown(self, **kwargs):
            pass

    worker.pool = Pool()
    return worker


def test_runtime_dispatch_round_robins_workspaces_before_filling_same_tenant(ingress):
    services = ingress
    services.db.execute(
        "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("other", "Other", "user", 1)
    )
    first = [submit(services, key=f"older-{index}")["id"] for index in range(3)]
    second = services.runtime_bridge.create(
        workspace_id="other",
        user_id="user",
        title="Other",
        prompt="other",
        spec={"name": "test", "version": "1"},
        idempotency_key="other",
    )["id"]
    for stamp, rid in enumerate([*first, second], 1):
        services.db.execute("UPDATE runs SET created_at=? WHERE id=?", (stamp, rid))
    worker = capturing_worker(services, slots=2)
    try:
        worker._schedule_runtime()
        assert worker.pool.submitted == [first[0], second]
        assert len(worker.busy) == 2
    finally:
        worker.shutdown()


def test_single_slot_runtime_dispatch_rotates_after_last_workspace(ingress):
    services = ingress
    services.db.execute(
        "INSERT INTO workspaces(id,name,owner_id,created_at) VALUES(?,?,?,?)", ("other", "Other", "user", 1)
    )
    first = submit(services)["id"]
    second = services.runtime_bridge.create(
        workspace_id="other",
        user_id="user",
        title="Other",
        prompt="other",
        spec={"name": "test", "version": "1"},
        idempotency_key="other",
    )["id"]
    services.db.execute("UPDATE runs SET created_at=1 WHERE id=?", (first,))
    services.db.execute("UPDATE runs SET created_at=2 WHERE id=?", (second,))
    worker = capturing_worker(services, slots=1)
    try:
        worker._schedule_runtime()
        assert worker.pool.submitted == [first]
        worker.busy.clear()  # a new dispatch turn has one free local slot
        worker._schedule_runtime()
        assert worker.pool.submitted == [first, second]
    finally:
        worker.shutdown()


def test_runtime_priority_is_tenant_scoped_and_fifo_breaks_ties(ingress):
    services = ingress
    ids = []
    for index, priority in enumerate((0, 10, 10), 1):
        run = services.runtime_bridge.create(
            workspace_id="ws",
            user_id="user",
            title="Priority",
            prompt="go",
            spec={"name": "test", "version": "1", "metadata": {"dispatch_priority": priority}},
            idempotency_key=str(index),
        )
        ids.append(run["id"])
        services.db.execute("UPDATE runs SET created_at=? WHERE id=?", (index, run["id"]))
    worker = capturing_worker(services, slots=3)
    try:
        worker._schedule_runtime()
        assert worker.pool.submitted == [ids[1], ids[2], ids[0]]
    finally:
        worker.shutdown()


def test_daily_admission_uses_clock_after_waiting_for_workspace_lock(ingress, monkeypatch):
    from contextlib import contextmanager

    from quant_agent.platform import runtime_bridge

    services = ingress
    old_day, new_day = 86400000, 172800000
    previous = services.runs.create(workspace_id="ws", user_id="user", kind="noop", mode="analysis")
    services.db.execute("UPDATE runs SET state='CANCELED',created_at=? WHERE id=?", (old_day, previous["id"]))
    services.db.execute(
        "UPDATE workspaces SET settings=? WHERE id=?", (json.dumps({"quotas": {"max_runs_per_day": 1}}), "ws")
    )
    clock = [old_day + 86399000]
    monkeypatch.setattr(runtime_bridge, "now_ms", lambda: clock[0])
    original = services.db.tx

    class Locked:
        def __init__(self, tx):
            self.tx = tx

        def __getattr__(self, name):
            return getattr(self.tx, name)

        def fetchone(self, sql, params=()):
            result = self.tx.fetchone(sql, params)
            if sql.startswith("SELECT settings FROM workspaces"):
                clock[0] = new_day
            return result

    @contextmanager
    def lock_crosses_midnight():
        with original() as tx:
            yield Locked(tx)

    monkeypatch.setattr(services.db, "tx", lock_crosses_midnight)
    created = submit(services, key="new-day")
    assert created["created_at"] == new_day


_PROCESS_BARRIER = None


def _init_process_barrier(barrier):
    global _PROCESS_BARRIER
    _PROCESS_BARRIER = barrier


def _enqueue_in_process(args):
    home, url, index = args
    services = AppServices.build(store_root=home, database_url=url)
    _PROCESS_BARRIER.wait(timeout=60)
    try:
        return submit(services, key=f"process-{index}")["id"]
    except AppError as exc:
        if exc.code != ErrorCode.QUOTA_EXHAUSTED:
            raise
        return None


def test_real_processes_share_one_queue_limit(ingress):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    services = ingress
    services.auth.backfill_usernames()
    services.db.execute(
        "UPDATE workspaces SET settings=? WHERE id=?",
        (
            json.dumps(
                {
                    "quotas": {
                        "max_concurrent_runs": 128,
                        "max_pending_runtime_runs": 1,
                    }
                }
            ),
            "ws",
        ),
    )
    spawn = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=4, mp_context=spawn, initializer=_init_process_barrier, initargs=(spawn.Barrier(4),)
    ) as pool:
        results = list(
            pool.map(_enqueue_in_process, [(str(services.home), services.db.url, index) for index in range(4)])
        )
    assert sum(result is not None for result in results) == 1
    assert services.db.scalar("SELECT COUNT(*) FROM runtime_tickets") == 1
    assert services.db.scalar("SELECT COUNT(*) FROM run_events") == 1


@pytest.mark.parametrize(
    "spec",
    [
        {"name": "test", "version": "1", "max_steps": 0},
        {"name": "test", "version": "1", "metadata": None},
        {"name": "test", "version": "1", "metadata": {"dispatch_priority": 101}},
        {"name": "test", "version": "1", "metadata": {"dispatch_priority": True}},
    ],
)
def test_invalid_spec_never_consumes_queue_capacity(ingress, spec):
    services = ingress
    with pytest.raises(ValueError):
        services.runtime_bridge.create(
            workspace_id="ws", user_id="user", title="Invalid", prompt="go", spec=spec, idempotency_key="invalid"
        )
    assert services.db.scalar("SELECT COUNT(*) FROM runtime_tickets") == 0
    assert services.db.scalar("SELECT COUNT(*) FROM run_events") == 0
    assert submit(services)["state"] == "QUEUED"
