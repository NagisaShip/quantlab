"""plan v2.0 第 245 行“必须验证”清单的逐条验收测试（与其它文件中的细测互为补充）。

1 未来数据不可见            2 收盘信号下一事件执行       3 重复事件不重复成交
4 手续费与资金流处理        5 策略版本不可变             6 用户跨工作区不可读取资源
7 断流不产生新单            8 重启一致                   9 未接真实账户时不展示实盘收益
10 只读模式不能触达任何真实下单路径
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_ledger import T0, H, mk

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.sdk import load_source, run_backtest
from quant_agent.sdk.runner import StrategyRunner
from quant_agent.sdk.runtimes import BacktestRuntime, RuntimeCost

ZERO = dict(slippage_bps=0, spread_bps=0, min_notional=0, rebalance_tolerance=0, max_volume_participation=1.0)

PEEK = """
def initialize(ctx):
    pass

def handle_data(ctx):
    rows = history(ctx.securities[0], 1000, "close_time")
    record(latest=rows[-1], now=ctx.current_dt)
"""

TOGGLE = """
def initialize(ctx):
    pass

def handle_data(ctx):
    s = ctx.securities[0]
    if ctx.bar_index % 4 == 1:
        order_target_percent(s, 0.5)
    elif ctx.bar_index % 4 == 3:
        order_target_percent(s, 0)
"""


@pytest.fixture()
def client(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _bars(n=40):
    return mk([(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(n)])


def test_1_future_data_invisible():
    r = run_backtest(load_source(PEEK), {"BTC/USDT": _bars()})
    for latest, now in zip(r.records["latest"], r.records["now"], strict=True):
        assert latest["v"] <= now["v"]


def test_2_close_signal_executes_on_next_event():
    r = run_backtest(load_source(TOGGLE), {"BTC/USDT": _bars()}, cost=RuntimeCost(**ZERO))
    first = r.fills[0]
    sig = next(s for s in r.signals if s["signal_id"] == first["signal_id"])
    assert first["ts"] == sig["decision_ts"]  # 下一根 bar 的开盘时刻 = 决策 bar 的收盘时刻
    assert Decimal(first["price"]) == Decimal(str(100 + (sig["decision_ts"] - T0) // H))  # 成交价 = 下一根开盘价


def test_3_duplicate_events_do_not_fill_twice():
    bars = _bars()
    rt = BacktestRuntime({"BTC/USDT": bars}, cost=RuntimeCost(**ZERO))
    rt.ctx.securities = ["BTC/USDT"]
    runner = StrategyRunner(load_source(TOGGLE), rt, rt.ctx)
    runner.start()
    for b in bars[:6] + bars[2:4] + bars[6:10]:  # 第 3、4 根被重复推送
        if not rt.accept_event(b.close_time):
            continue
        rt.advance(b.close_time)
        rt.execute()
        runner.on_bar(b.close_time)
    once = run_backtest(load_source(TOGGLE), {"BTC/USDT": bars[:10]}, cost=RuntimeCost(**ZERO))
    assert [f.fill_id for f in rt.fills] == [f["fill_id"] for f in once.fills]
    assert rt.counters["duplicate_events"] == 2


def test_4_fees_and_cash_flows():
    r = run_backtest(
        load_source(TOGGLE),
        {"BTC/USDT": _bars()},
        initial_cash=1000,
        cash_flows=[{"ts": T0 + 10 * H, "amount": 500}, {"ts": T0 + 20 * H, "amount": -200}],
    )
    assert r.ledger["balanced"] and r.ledger["net_contributions"] == "1300"
    fees = sum(Decimal(f["fee"]) for f in r.fills)
    assert Decimal(r.ledger["fees_by_currency"]["USDT"]) == fees
    assert r.metrics["return_basis"] == "time_weighted_unit_nav"
    # 单位净值收益与权益比值不同：入金没有被算作收益
    assert r.metrics["total_return"] < r.equity_curve[-1] / 1000 - 1


def test_5_strategy_versions_are_immutable(client):
    code = TOGGLE
    s = client.post("/api/research/strategies", json={"name": "版本", "code": code}).json()["strategy"]
    v1 = client.get(f"/api/strategies/{s['slug']}/versions").json()["versions"][0]
    client.post("/api/studio/strategies", json={"name": "版本", "code": code.replace("0.5", "0.6"), "slug": s["slug"]})
    vs = client.get(f"/api/strategies/{s['slug']}/versions").json()["versions"]
    assert len(vs) == 2
    again = client.get(f"/api/strategy-versions/{v1['id']}").json()["version"]
    assert again["code"] == code and again["code_hash"] == v1["code_hash"]


def test_6_cross_workspace_unreadable(tmp_path, monkeypatch):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    set_services(AppServices.build(store_root=str(tmp_path)))
    try:
        app = create_app()
        with TestClient(app) as a, TestClient(app) as b:
            login(a, email="a@example.com", password="Alice123!")
            login(b, email="b@example.com", password="Bobby123!")
            s = a.post("/api/research/strategies", json={"name": "A", "code": TOGGLE}).json()["strategy"]
            rid = a.post(
                "/api/research/backtests",
                json={"slug": s["slug"], "source": "synthetic", "bars_limit": 200, "warmup_bars": 5},
            ).json()["run_id"]
            vid = a.get(f"/api/runs/{rid}").json()["run"]["strategy_version_id"]
            for path in (
                f"/api/runs/{rid}",
                f"/api/runs/{rid}/fills",
                f"/api/runs/{rid}/events",
                f"/api/strategy-versions/{vid}",
                f"/api/studio/strategies/{s['slug']}",
            ):
                assert b.get(path).status_code == 404, path
            b.headers["X-Workspace-Id"] = a.get("/api/auth/me").json()["workspace"]["id"]
            assert b.get(f"/api/runs/{rid}").status_code == 403
    finally:
        reset_services()


def test_7_data_outage_creates_no_new_orders():
    from quant_agent.engine.paper import PaperTradingEngine

    eng = PaperTradingEngine()
    s = eng.start(strategy_id="x", strategy_name="x", strategy_version="v", symbol="BTC/USDT")
    res = eng.on_sample(s, bars=[], quote={"age_ms": 10 * 60_000}, equity=10_000)
    assert res["skipped"] == "行情过期" and s.status == "paused"
    assert eng.on_sample(s, bars=_bars(3), equity=10_000)["skipped"]  # 暂停期间不再处理新 bar
    # 运行时层面：停机后到来的旧信号过期作废，不补成交
    bars = _bars(12)
    rt = BacktestRuntime({"BTC/USDT": bars}, cost=RuntimeCost(**ZERO, signal_ttl_bars=1))
    rt.ctx.securities = ["BTC/USDT"]
    runner = StrategyRunner(load_source(TOGGLE), rt, rt.ctx)
    runner.start()
    rt.advance(bars[0].close_time)
    rt.accept_event(bars[0].close_time)
    runner.on_bar(bars[0].close_time)  # 发出委托
    rt.advance(bars[5].close_time)  # 断流 5 根后才恢复
    rt.execute()
    assert rt.fills == [] and rt.counters["expired_signals"] == 1


def test_8_restart_is_consistent():
    bars = _bars(60)
    full = run_backtest(load_source(TOGGLE), {"BTC/USDT": bars}, cost=RuntimeCost(**ZERO), run_id="r")

    def drive(rt, runner, seq):
        for b in seq:
            if rt.accept_event(b.close_time):
                rt.advance(b.close_time)
                rt.execute()
                runner.on_bar(b.close_time)
                rt.mark()

    rt1 = BacktestRuntime({"BTC/USDT": bars}, cost=RuntimeCost(**ZERO), run_id="r")
    rt1.ctx.securities = ["BTC/USDT"]
    r1 = StrategyRunner(load_source(TOGGLE), rt1, rt1.ctx)
    r1.start()
    drive(rt1, r1, bars[:30])
    snap = rt1.snapshot()
    fills_before = [f.to_dict() for f in rt1.fills]
    # “进程崩溃”：新运行时从检查点恢复，并收到一段重叠的重放
    rt2 = BacktestRuntime({"BTC/USDT": bars}, cost=RuntimeCost(**ZERO), run_id="r")
    rt2.ctx.securities = ["BTC/USDT"]
    r2 = StrategyRunner(load_source(TOGGLE), rt2, rt2.ctx)
    r2.start()
    rt2.restore(snap)
    r2.ctx.bar_index = snap["ctx"]["bar_index"]
    drive(rt2, r2, bars[25:])
    combined = fills_before + [f.to_dict() for f in rt2.fills]
    assert [f["fill_id"] for f in combined] == [f["fill_id"] for f in full.fills]
    assert rt2.ledger.equity() == Decimal(full.ledger["final_equity"])


def test_9_no_live_returns_without_live_data(client):
    st = client.get("/api/live/status").json()
    assert st["has_live_data"] is False and st["live_fills"] == 0 and st["live_enabled"] is False
    rid = client.post(
        "/api/research/backtests",
        json={"code": TOGGLE, "source": "synthetic", "bars_limit": 200, "warmup_bars": 5, "persist": False},
    ).json()["run_id"]
    r = client.post("/api/comparisons", json={"sim_run_id": rid, "live_run_ids": [rid]})
    assert r.status_code in (404, 422) and r.json()["code"] in ("NOT_FOUND", "DATA_INCOMPLETE")


def test_10_readonly_mode_has_no_order_path(client):
    from quant_agent.live import accounts

    for cls in (accounts.BinanceReadOnly, accounts.OKXReadOnly):
        assert not [n for n in dir(cls) if any(w in n.lower() for w in ("order", "withdraw", "transfer"))]
    r = client.post(
        "/api/studio/order",
        json={"symbol": "BTC/USDT", "side": "buy", "qty": 0.001, "client_order_id": "c-1", "simulated": False},
    )
    assert r.status_code == 400
    assert client.get("/api/auth/me").json()["live_enabled"] is False
    routes = {(m, r.path) for r in client.app.routes for m in getattr(r, "methods", [])}
    assert not any("live" in p and "order" in p for _, p in routes)


def test_healthz_is_public_and_does_not_touch_exchanges(client):
    """容器探活只检查数据库：外网抖动不应导致 API 容器被反复重启。"""
    client.cookies.clear()
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["backend"] in ("sqlite", "postgres")
    assert "adapters" not in r.json()
