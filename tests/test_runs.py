"""运行队列与 worker：单租约、围栏、检查点恢复、命令、配额、跨工作区隔离、独立 worker 模式。"""

from __future__ import annotations

import sys
import threading

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform.runs import RECOVERING, RUNNING, RunService

SIMPLE = """
PARAMS = {"fast": 6, "slow": 24}

def initialize(ctx):
    run_daily(step, time="close")

def step(ctx):
    s = ctx.securities[0]
    closes = history(s, PARAMS["slow"], "close")
    if len(closes) < PARAMS["slow"]:
        return
    fast = sum(closes[-PARAMS["fast"]:]) / PARAMS["fast"]
    slow = sum(closes) / PARAMS["slow"]
    order_target_percent(s, 0.8 if fast > slow else 0.0)
"""
SPEC = {"source": "synthetic", "bars_limit": 400, "warmup_bars": 30}


@pytest.fixture()
def app(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        login(c)
        yield c


def _ws(svc) -> str:
    return svc.db.fetchone("SELECT id FROM workspaces LIMIT 1")["id"]


def test_single_lease_under_concurrent_claims(client):
    svc = get_services()
    runs: RunService = svc.runs
    ws = _ws(svc)
    run = runs.create(workspace_id=ws, user_id="u", kind="noop", mode="backtest")
    got: list = []
    barrier = threading.Barrier(6)

    def claim(i):
        barrier.wait()
        r = runs.claim(f"w{i}", kinds=["noop"])
        if r:
            got.append(r["lease_owner"])

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 1
    assert runs.get(run["id"])["state"] == RUNNING


def test_lease_fencing_and_recovery(client):
    svc = get_services()
    runs = svc.runs
    ws = _ws(svc)
    run = runs.create(workspace_id=ws, user_id="u", kind="noop", mode="backtest")
    a = runs.claim("worker-a", kinds=["noop"])
    assert runs.checkpoint(a["id"], "worker-a", {"step": 1}, last_seq=1)
    # worker-a 失联：租约过期 → RECOVERING → worker-b 接手并拿到检查点
    svc.db.execute("UPDATE runs SET lease_expires_at=1 WHERE id=?", (run["id"],))
    assert runs.recover_expired() == 1 and runs.get(run["id"])["state"] == RECOVERING
    b = runs.claim("worker-b", kinds=["noop"])
    assert b["checkpoint"] == {"step": 1} and b["attempts"] == 2
    # 旧 worker 的写入被围栏挡住
    assert not runs.checkpoint(run["id"], "worker-a", {"step": 99})
    assert not runs.heartbeat(run["id"], "worker-a")
    assert runs.event(run["id"], "x", {}, owner="worker-a") is None
    assert runs.get(run["id"], checkpoint=True)["checkpoint"] == {"step": 1}


def test_backtest_runs_as_run_with_db_fills(client):
    s = client.post("/api/research/strategies", json={"name": "队列回测", "code": SIMPLE}).json()["strategy"]
    r = client.post("/api/research/backtests", json={"slug": s["slug"], **SPEC})
    assert r.status_code == 200, r.text
    body = r.json()
    run = client.get(f"/api/runs/{body['run_id']}").json()["run"]
    assert run["state"] == "COMPLETED" and run["kind"] == "backtest"
    ver = client.get(f"/api/strategy-versions/{run['strategy_version_id']}").json()["version"]
    assert ver["strategy_slug"] == s["slug"] and ver["status"] == "VALIDATED" and ver["code"] == SIMPLE
    assert "source" not in run["manifest"]  # 源码不随运行详情外泄
    fills = client.get(f"/api/runs/{body['run_id']}/fills").json()["items"]
    assert len(fills) == len(body["result"]["fills"])
    assert all(isinstance(f["price"], str) for f in fills)
    assert body["result"]["ledger"]["balanced"] is True
    assert body["result"]["manifest"]["isolation"]["process"] is True
    events = client.get(f"/api/runs/{body['run_id']}/events").json()
    assert [e["data"]["state"] for e in events["items"] if e["type"] == "state"] == ["QUEUED", "RUNNING", "COMPLETED"]
    # 游标：after 之后没有新事件
    assert client.get(f"/api/runs/{body['run_id']}/events?after={events['cursor']}").json()["items"] == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="该用例依赖 POSIX 专有的 preexec_fn 与 resource 模块，Windows 上无法执行；CI 在 Linux 上会运行它",
)
def test_sandbox_child_has_no_secrets_and_limits(monkeypatch):
    """隔离子进程：拿不到平台进程的环境变量（数据库地址、加密密钥），有资源上限，（root 下）无网络。"""
    import json
    import os
    import subprocess
    import sys
    import tempfile

    from quant_agent.platform import isolation

    monkeypatch.setenv("QAGENT_SECRET_PROBE", "should-not-leak")
    code = (
        "import os,json,resource;print(json.dumps({'env':sorted(os.environ),"
        "'as':resource.getrlimit(resource.RLIMIT_AS)[0]}))"
    )
    p = subprocess.run(
        [sys.executable, "-c", code],
        preexec_fn=isolation._preexec(isolation.limits()),
        env=isolation._env(),
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
        timeout=30,
    )
    out = json.loads(p.stdout)
    assert not any(k.startswith("QAGENT") for k in out["env"])
    assert out["as"] == isolation.limits()["memory_mb"] * 1024 * 1024
    rep = isolation.probe()
    assert rep["process"] and rep["env_cleared"] and rep["rlimits"]
    if os.getuid() == 0:
        assert rep["netns"]


def test_runaway_strategy_is_killed():
    from test_ledger import mk

    from quant_agent.platform import isolation
    from quant_agent.platform.sandbox_exec import bars_to_wire

    hog = "def initialize(ctx):\n    pass\n\ndef handle_data(ctx):\n    x = [0] * (10 ** 10)\n"
    out = isolation.run_batch(
        {
            "op": "backtest",
            "source": hog,
            "name": "hog",
            "spec": {"warmup_bars": 1},
            "bars": bars_to_wire({"BTC/USDT": mk([(1, 1, 1, 1)] * 5)}),
        },
        timeout=30,
    )
    assert out["errors"], out["errors"]  # 内存上限生效：策略拿不到 80GB，报错而不是拖垮 worker


def test_optimize_resumes_from_checkpoint(client):
    from quant_agent.api import tasks
    from quant_agent.platform.worker import TaskCtl

    s = client.post("/api/research/strategies", json={"name": "断点优化", "code": SIMPLE}).json()["strategy"]
    svc = get_services()
    ws = _ws(svc)
    run = svc.runs.create(
        workspace_id=ws,
        user_id="u",
        kind="optimize",
        mode="backtest",
        manifest={
            "source": SIMPLE,
            "module_name": s["slug"],
            "name": "x",
            "slug": s["slug"],
            "spec": {**SPEC, "securities": ["BTC/USDT"]},
            "grid": {"fast": [4, 6, 8]},
            "split": 0.7,
            "objective": "sharpe",
            "combinations": 3,
        },
    )
    claimed = svc.runs.claim("w", kinds=["optimize"])
    # 模拟上一个 worker 已算完 2 组并写了检查点
    done = [
        {"params": {"fast": 4}, "in_sample": {"sharpe": 1.0}, "out_sample": {"sharpe": 0.5}},
        {"params": {"fast": 6}, "in_sample": {"sharpe": 0.8}, "out_sample": {"sharpe": 0.2}},
    ]
    svc.runs.checkpoint(run["id"], "w", {"rows": done})
    claimed["checkpoint"] = {"rows": done}
    from quant_agent.platform import context

    with context.bind(ws):
        out = tasks.handle_optimize(TaskCtl(run=claimed, runs=svc.runs, owner="w", services=svc))
    cp = svc.runs.get(run["id"], checkpoint=True)["checkpoint"]
    assert len(cp["rows"]) == 3  # 只新算了 1 组
    assert cp["rows"][:2] == done
    assert out["id"].startswith("opt-")


def test_quota_exhausted(client):
    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    client.put("/api/workspace/settings", json={"quotas": {"max_runs_per_day": 1}})
    client.post("/api/research/backtests", json={"code": SIMPLE, **SPEC, "persist": False})
    r = client.post("/api/research/backtests", json={"code": SIMPLE, **SPEC, "persist": False})
    assert r.status_code == 429 and r.json()["code"] == "QUOTA_EXHAUSTED"
    assert ws


def test_python_strategies_can_be_disabled(client):
    client.put("/api/workspace/settings", json={"quotas": {"allow_python_strategies": False}})
    r = client.post("/api/research/backtests", json={"code": SIMPLE, **SPEC})
    assert r.status_code == 400 and r.json()["code"] == "UNSUPPORTED_CAPABILITY"


def test_runs_are_workspace_isolated(app, monkeypatch):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    with TestClient(app) as a, TestClient(app) as b:
        login(a, email="a@example.com", password="Alice123!")
        login(b, email="b@example.com", password="Bobby123!")
        rid = a.post("/api/research/backtests", json={"code": SIMPLE, **SPEC, "persist": False}).json()["run_id"]
        assert b.get(f"/api/runs/{rid}").status_code == 404
        assert b.get(f"/api/runs/{rid}/fills").status_code == 404
        assert b.post(f"/api/runs/{rid}/commands", json={"command": "cancel"}).status_code == 404
        assert all(x["id"] != rid for x in b.get("/api/runs").json()["items"])


def test_external_worker_mode(app, monkeypatch):
    """独立 worker 模式：API 只登记，返回 202；worker 认领执行后结果可取。"""
    from quant_agent.api.tasks import build_worker

    monkeypatch.setenv("QAGENT_WORKER", "external")
    with TestClient(app) as c:
        login(c)
        r = c.post("/api/research/backtests", json={"code": SIMPLE, **SPEC, "wait_seconds": 0})
        assert r.status_code == 202, r.text
        rid = r.json()["run_id"]
        assert c.get(f"/api/runs/{rid}").json()["run"]["state"] == "QUEUED"
        worker = build_worker(get_services(), slots=1)
        worker.register()
        worker.loop_once()
        worker.pool.shutdown(wait=True)
        run = c.get(f"/api/runs/{rid}").json()["run"]
        assert run["state"] == "COMPLETED", run
        assert c.get(f"/api/runs/{rid}/result").json()["result"]["ledger"]["balanced"]
        # 排队中的任务可以直接取消
        r2 = c.post("/api/research/backtests", json={"code": SIMPLE, **SPEC, "wait_seconds": 0}).json()["run_id"]
        c.post(f"/api/runs/{r2}/commands", json={"command": "cancel"})
        assert c.get(f"/api/runs/{r2}").json()["run"]["state"] == "CANCELED"
        assert c.get("/api/runs/workers").json()["workers"]


def test_schedules_are_per_workspace_and_fire_once_per_minute(app, monkeypatch):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    # 固定时钟。这条用例验证的是"同一分钟内两次扫描只登记一次"，幂等键是
    # `sched:<任务>:<分钟>`（见 routes_ops.due_schedules）。它原本直接读墙钟，
    # 于是两次 tick 之间一旦**真实跨过分钟边界**，第二次就会**合法地**新建一个 run，
    # 断言随之随机失败——这是测试依赖墙钟的缺陷，不是产品缺陷。
    # due_schedules 本来就接受 now 参数，所以从源头把 now 钉死，而不是放宽断言：
    # 放宽断言就等于不再验证"同一分钟只登记一次"这件事。
    from datetime import datetime as _dt

    from quant_agent.api import routes_ops

    class _FrozenDatetime(_dt):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, 12, 0, 30)

    monkeypatch.setattr(routes_ops, "datetime", _FrozenDatetime)
    with TestClient(app) as a, TestClient(app) as b:
        login(a, email="a@example.com", password="Alice123!")
        login(b, email="b@example.com", password="Bobby123!")
        a.post("/api/schedules", json={"id": "every", "cron": "* * * * *", "kind": "digest"})
        assert b.get("/api/schedules").json()["jobs"] == []
        f1 = a.post("/api/schedules/tick").json()["fired"]
        f2 = a.post("/api/schedules/tick").json()["fired"]
        assert len(f1) == 1 and f1[0]["run_id"] == f2[0]["run_id"]  # 同一分钟只登记一次
        run = a.get(f"/api/runs/{f1[0]['run_id']}").json()["run"]
        assert run["kind"] == "agent_job" and run["state"] in ("COMPLETED", "FAILED")
        assert b.get(f"/api/runs/{f1[0]['run_id']}").status_code == 404


def test_json_store_is_multiprocess_safe(tmp_path):
    """API 与 worker 是两个进程：一个写入，另一个必须立刻读到；并发写不能互相覆盖。"""
    import multiprocessing as mp

    from quant_agent.store import JsonStore

    a, b = JsonStore(tmp_path), JsonStore(tmp_path)
    assert a.list("x") == []
    b.upsert("x", {"id": "1", "v": 1})
    assert [i["id"] for i in a.list("x")] == ["1"]  # 缓存按文件签名失效

    # fork 在 Windows 上不存在；改用 spawn 依然验证"两个进程共享同一份 JSON 存储"这一语义
    ctx = mp.get_context("spawn" if sys.platform == "win32" else "fork")
    procs = [ctx.Process(target=_writer, args=(str(tmp_path), k)) for k in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert len(a.list("x")) == 1 + 4 * 25


def _writer(root, k):
    from quant_agent.store import JsonStore

    s = JsonStore(root)
    for i in range(25):
        s.upsert("x", {"id": f"{k}-{i}"})
