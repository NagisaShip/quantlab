"""上线验证：仿真真实度拆解、成本校准、毕业闸门、AI 值守员。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_demo_trading import FLIP, FakeBroker

from quant_agent.api import tasks
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.data.quality import TIMEFRAME_MS
from quant_agent.engine import fidelity as FD
from quant_agent.engine.live import clock_synthetic_bars
from quant_agent.live import demo_accounts
from quant_agent.research import graduation as GR
from quant_agent.research import watcher as W
from quant_agent.store import Collections, JsonStore

H = 3_600_000
T0 = 1_700_000_000_000


def _f(ts, side, qty, price, market, fee, **kw):
    return {
        "ts": ts,
        "decided_at": ts - H,
        "security": "BTC/USDT",
        "side": side,
        "amount": str(qty),
        "price": str(price),
        "market_price": str(market),
        "fee_value": str(fee),
        **kw,
    }


# ---------------------------------------------------------------------------
# 拆解：手算的标准答案
# ---------------------------------------------------------------------------
def test_decompose_golden():
    bt = {
        "fills": [
            _f(T0, "buy", 1, 101, 100, 0.10),
            _f(T0 + 5 * H, "sell", 1, 109, 110, 0.11),
            _f(T0 + 8 * H, "buy", 2, 121, 120, 0.24),
        ],
        "equity": [(T0, 10_000.0), (T0 + 10 * H, 10_007.0)],
    }
    live = {
        "execution": "demo",
        "label": "x",
        # 第一笔：送单时市价 100.5（延迟 0.5），成交 101.8（多滑 0.3），手续费 0.12；
        # 第二笔：只成交 0.6（部分成交）；第三笔：被拒
        "fills": [
            _f(T0 + 60_000, "buy", 1, 101.8, 100.5, 0.12),
            _f(T0 + 5 * H + 60_000, "sell", 0.6, 109.0, 110.0, 0.07),
        ],
        "orders": [
            {
                "status": "rejected",
                "security": "BTC/USDT",
                "side": "buy",
                "decided_at": T0 + 7 * H,
                "message": "余额不足",
            }
        ],
        "equity": [(T0, 10_000.0), (T0 + 10 * H, 9_990.0)],
    }
    out = FD.decompose(backtest=bt, live=live, end_prices={"BTC/USDT": 125.0}, bar_ms=H, model_slip_bps=6)
    c = {x["key"]: x["value"] for x in out["components"]}
    assert out["matched"] == 2 and out["only_backtest"] == 1
    assert c["latency"] == pytest.approx(-0.5)  # 买贵了 0.5
    assert c["slippage"] == pytest.approx(-0.3)  # (101.8-100.5) - (101-100)
    # 手续费：-(0.12-0.10) - (0.07 - 0.11*0.6)
    assert c["fees"] == pytest.approx(-(0.02) - (0.07 - 0.066))
    # 部分成交：少卖 0.4，卖出价 109，期末 125 → 少卖反而少亏 0.4*16=6.4
    assert c["partials"] == pytest.approx(6.4)
    # 拒单：错过的买入 2 @121 → 期末 125，错过 8，省了 0.24 手续费 → -7.76
    assert c["rejections"] == pytest.approx(-7.76)
    assert out["total"] == pytest.approx(-17.0)
    assert sum(c.values()) == pytest.approx(out["total"])
    assert out["sample"]["rejected"] == 1 and out["sample"]["partials"] == 1


def test_calibration_suggests_cost_from_demo_fills():
    sample = {
        "slippage_bps": [8.0, 10.0, 12.0] * 10,
        "latency_ms": [800, 1200],
        "fee_notional": 100_000.0,
        "fee_value": 75.0,
        "fills": 30,
        "rejected": 3,
        "partials": 1,
        "orders": 33,
        "attempts": 33,
        "model_slip_bps": 6,
    }
    cal = FD.calibrate(
        [{"execution": "demo", "sample": sample}], {"taker_fee": 0.001, "slippage_bps": 5, "spread_bps": 2}
    )
    assert cal["samples"] == 30 and cal["confidence"] == "medium"
    assert cal["suggested"] == {"taker_fee": 0.00075, "slippage_bps": 9.0, "spread_bps": 2}
    assert cal["observed"]["reject_rate"] == pytest.approx(3 / 33, abs=1e-4)
    none = FD.calibrate([{"execution": "paper", "sample": sample}], {})
    assert none["suggested"] is None and none["samples"] == 0


# ---------------------------------------------------------------------------
# 端到端：纸面 + 模拟盘 → 重跑回测 → 报告 → 校准
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("validation-store")
    services = AppServices.build(store_root=str(root))
    set_services(services)
    mp = pytest.MonkeyPatch()
    broker = FakeBroker(usdt="20000")
    mp.setattr(demo_accounts, "broker_factory", lambda ex, env, creds: broker)
    with TestClient(create_app()) as c:
        login(c)
        yield c, broker, mp
    mp.undo()
    reset_services()


def test_fidelity_report_end_to_end(env):
    client, broker, mp = env
    tf, step = "1h", TIMEFRAME_MS["1h"]
    clock = {"end": T0 + 400 * step}

    def bars(services, session, count):
        out = {}
        for sec in session.securities or [session.symbol]:
            got = clock_synthetic_bars(sec, tf, clock["end"] - count * step, clock["end"])
            out[sec] = got
            if got:
                broker.price = Decimal(str(got[-1].close)) * Decimal("1.0008")  # 模拟盘成交比收盘价差一点
        return out

    mp.setattr(tasks, "latest_bars", bars)
    mp.setattr(get_services().market, "ticker", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("off")))
    mp.setattr(
        FD,
        "window_bars",
        lambda services, row, secs, tf_, warm, end: ({s: clock_synthetic_bars(s, tf_, warm, end) for s in secs}, []),
    )
    r = client.post(
        "/api/studio/strategies", json={"name": "真实度", "code": FLIP, "securities": ["BTC/USDT"], "timeframe": "1h"}
    )
    slug = r.json()["strategy"]["slug"]
    client.post(f"/api/studio/strategies/{slug}/approve")
    cid = client.post(
        "/api/demo/connections",
        json={"exchange": "binance", "env": "demo", "api_key": "demokey-fid001", "api_secret": "secret-123"},
    ).json()["connection"]["id"]
    base = {"slug": slug, "source": "exchange", "exchange": "binance", "initial_cash": 10_000}
    paper = client.post("/api/studio/paper/start", json=base).json()["session"]["id"]
    demo = client.post(
        "/api/studio/paper/start",
        json={**base, "execution": "demo", "demo_connection_id": cid, "max_order_notional": 100_000},
    ).json()["session"]["id"]
    start_bar = clock["end"]
    for _ in range(6):
        clock["end"] += step
        client.post(f"/api/studio/paper/{paper}/tick")
        client.post(f"/api/studio/paper/{demo}/tick")
    store = get_services().store.for_workspace(client.headers["X-Workspace-Id"])
    for sid in (paper, demo):  # 预热截止按行情时钟记录（测试里行情时钟和墙钟不同）
        row = store.get(Collections.PAPER_SESSIONS, sid)
        assert row["fills"], row.get("events")
        assert start_bar - step < row["warm_until"] <= start_bar

    r = client.post("/api/validation/fidelity", json={"session_ids": [paper, demo]})
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    parts = {p["execution"]: p for p in rep["parts"]}
    assert set(parts) == {"paper", "demo"}
    # 纸面模拟和回测是同一套撮合：成交一一对上，没有滑点和延迟差
    p = parts["paper"]
    assert p["matched"] >= 3 and p["only_backtest"] <= 1
    pc = {x["key"]: x["value"] for x in p["components"]}
    assert abs(pc["slippage"]) < 1e-6 and abs(pc["latency"]) < 1e-6
    # 模拟盘：配上对，并且拆出了延迟 / 滑点 / 手续费
    d = parts["demo"]
    assert d["matched"] >= 3
    dc = {x["key"]: x["value"] for x in d["components"]}
    assert dc["latency"] != 0 or dc["slippage"] != 0
    assert sum(dc.values()) == pytest.approx(d["total"], abs=1e-6)
    cal = rep["calibration"]
    assert cal["samples"] >= 3 and cal["suggested"]["slippage_bps"] >= 0
    assert set(rep["deviation"]) == {"paper", "demo"}

    r = client.post(f"/api/validation/fidelity/{rep['id']}/calibration", json={"confirm": False})
    assert r.status_code == 400
    r = client.post(f"/api/validation/fidelity/{rep['id']}/calibration", json={"confirm": True})
    assert r.status_code == 200 and r.json()["calibration"]["exchange"] == "binance"
    assert client.get("/api/validation/calibration").json()["calibrations"]

    # 值守员：没有说明书时用默认说明书，规则层照样跑
    r = client.post(f"/api/validation/watcher/{demo}?llm=false")
    assert r.status_code == 200
    rv = r.json()["review"]
    assert rv["trades"] and all(t["explanation"] for t in rv["trades"])
    # 毕业看板：刚建的策略在回测阶段，晋级检查不通过
    board = client.get("/api/validation/graduation").json()["board"]
    assert any(b["strategy"] == slug and b["stage"] == "backtest" and not b["passed"] for b in board)
    r = client.post(f"/api/validation/graduation/{slug}/promote", json={"confirm": True})
    assert r.status_code == 400 and "没通过" in r.json()["message"]


# ---------------------------------------------------------------------------
# 毕业闸门
# ---------------------------------------------------------------------------
def _session(sid, slug, execution, days, dd=0.05, source="exchange", halted=False):
    start = T0
    pts = [
        {"t": start, "v": 10_000.0},
        {"t": start + H, "v": 10_000 * (1 - dd)},
        {"t": start + days * 24 * H, "v": 10_100.0},
    ]
    return {
        "id": sid,
        "strategy_id": slug,
        "execution": execution,
        "source": source,
        "started_at": start,
        "last_update": start + int(days * 24 * H),
        "equity_points": pts,
        "demo": {"halted": halted},
    }


def test_graduation_gates(tmp_path):
    store = JsonStore(tmp_path)
    slug = "g1"
    ev = GR.evaluate(store, slug)
    assert ev["stage"] == "backtest" and not ev["passed"] and ev["live_trading"] == "closed"
    store.upsert(
        Collections.BACKTESTS,
        {
            "id": "bt1",
            "slug": slug,
            "status": "done",
            "created_at": 1,
            "start_ts": T0,
            "end_ts": T0 + 365 * 24 * H,
            "metrics": {"sharpe": 2.5, "max_drawdown": 0.12},
        },
    )
    ev = GR.evaluate(store, slug)
    assert not ev["passed"] and ev["checks"][-1]["value"] == "还没体检"  # 体检是硬条件
    store.upsert(
        "checkups",
        {
            "id": "ck1",
            "strategy_id": slug,
            "status": "done",
            "created_at": 1,
            "report": {"items": [{"id": "lookahead", "title": "未来函数", "verdict": "bad"}]},
        },
    )
    ev = GR.evaluate(store, slug)
    assert not ev["passed"] and "未来函数" in ev["checks"][-1]["value"]
    store.upsert(
        "checkups",
        {
            "id": "ck2",
            "strategy_id": slug,
            "status": "done",
            "created_at": 2,
            "report": {
                "items": [
                    {"id": "lookahead", "title": "未来函数", "verdict": "ok"},
                    {"id": "cost", "title": "成本翻倍", "verdict": "warn"},
                ]
            },
        },
    )
    ev = GR.evaluate(store, slug)
    assert ev["passed"], ev["checks"]
    with pytest.raises(ValueError, match="确认"):
        GR.promote(store, slug, confirm=False, user="u")
    ev = GR.promote(store, slug, confirm=True, user="u")
    assert ev["stage"] == "paper" and ev["history"][-1]["by"] == "u"
    # 纸面模拟：只跑了 3 天 → 不通过；演示行情不算
    store.upsert(Collections.PAPER_SESSIONS, _session("p1", slug, "paper", 3))
    store.upsert(Collections.PAPER_SESSIONS, _session("p2", slug, "paper", 30, source="demo"))
    ev = GR.evaluate(store, slug)
    assert not ev["passed"] and ev["checks"][0]["ok"] is False
    store.upsert(Collections.PAPER_SESSIONS, _session("p1", slug, "paper", 8))
    ev = GR.evaluate(store, slug)
    assert ev["checks"][0]["ok"] and ev["checks"][1]["ok"] and ev["checks"][2]["ok"] is None  # 还没有真实度报告
    store.upsert(
        FD.REPORTS, {"id": "f1", "strategy_id": slug, "created_at": 2, "deviation": {"paper": {"deviation_bps": 40.0}}}
    )
    assert GR.evaluate(store, slug)["passed"]
    GR.promote(store, slug, confirm=True, user="u")
    # 模拟盘：熔断中不能毕业
    store.upsert(Collections.PAPER_SESSIONS, _session("d1", slug, "demo", 15, halted=True))
    store.upsert(
        FD.REPORTS, {"id": "f2", "strategy_id": slug, "created_at": 3, "deviation": {"demo": {"deviation_bps": 120.0}}}
    )
    ev = GR.evaluate(store, slug)
    assert not ev["passed"] and ev["checks"][-1]["ok"] is False
    store.upsert(Collections.PAPER_SESSIONS, _session("d1", slug, "demo", 15, dd=0.25))
    assert not GR.evaluate(store, slug)["passed"]  # 回撤超过 15%
    store.upsert(Collections.PAPER_SESSIONS, _session("d1", slug, "demo", 15))
    ev = GR.promote(store, slug, confirm=True, user="u")
    assert ev["stage"] == "graduated" and ev["next"] is None
    ev = GR.demote(store, slug, to="paper", user="u", note="复查")
    assert ev["stage"] == "paper"


# ---------------------------------------------------------------------------
# 值守员
# ---------------------------------------------------------------------------
SPEC = """# 趋势
标的：BTC/USDT　周期：1h

## 策略逻辑
只做多，不加杠杆，单标的仓位不超过 50%。每天最多 2 笔。

## 什么时候应该停
回撤超过 10% 停止开仓；连续亏损 2 次停止。
"""


def test_watcher_rules_parse_and_flag(tmp_path):
    rules = W.parse_rules(SPEC)
    assert rules == {
        "long_only": True,
        "max_leverage": 1.0,
        "max_weight": 0.5,
        "max_trades_per_day": 2,
        "stop_drawdown": 0.10,
        "stop_losing_streak": 2,
        "securities": ["BTC/USDT"],
    }
    fills = [
        {
            "fill_id": "a",
            "ts": T0,
            "security": "BTC/USDT",
            "side": "buy",
            "amount": "1",
            "price": "100",
            "reason": "signal",
        },
        {
            "fill_id": "b",
            "ts": T0 + H,
            "security": "BTC/USDT",
            "side": "sell",
            "amount": "1",
            "price": "95",
            "realized_pnl": "-5",
        },
        {"fill_id": "c", "ts": T0 + 2 * H, "security": "BTC/USDT", "side": "buy", "amount": "1", "price": "96"},
        {
            "fill_id": "d",
            "ts": T0 + 3 * H,
            "security": "BTC/USDT",
            "side": "sell",
            "amount": "1",
            "price": "90",
            "realized_pnl": "-6",
        },
        {"fill_id": "e", "ts": T0 + 4 * H, "security": "ETH/USDT:PERP", "side": "sell", "amount": "2", "price": "50"},
    ]
    session = {
        "id": "s1",
        "strategy_name": "趋势",
        "fills": fills,
        "equity_points": [{"t": T0, "v": 10_000}, {"t": T0 + 3 * H, "v": 8_500}],
        "portfolio": {"equity": 8_500, "positions": [{"security": "ETH/USDT:PERP", "value": -5_000, "leverage": 3}]},
        "logs": [{"ts": T0, "message": "快线上穿慢线"}],
    }
    out = W.rule_review(session, SPEC)
    rules_hit = {f["rule"] for f in out["flags"]}
    assert {
        "long_only",
        "securities",
        "max_trades_per_day",
        "stop_drawdown",
        "stop_losing_streak",
        "max_leverage",
        "max_weight",
    } <= rules_hit
    assert "快线上穿慢线" in out["trades"][0]["explanation"]
    assert "亏 5.00" in out["trades"][1]["explanation"]

    store = JsonStore(tmp_path)
    calls = []

    def llm(system, user):
        calls.append(user)
        return (
            '{"explanations": [{"fill_id": "a", "text": "均线金叉入场"}], '
            '"contradictions": [{"fill_id": "e", "text": "横盘里开空"}]}'
        )

    rec = W.review(store, session, SPEC, llm=llm, model="m1")
    assert rec["tagger"]["kind"] == "llm" and rec["tagger"]["prompt_version"] == W.PROMPT_VERSION
    assert rec["trades"][0]["explanation_ai"] == "均线金叉入场"
    assert any(f["rule"] == "llm" for f in rec["flags"])
    sec = W.brief_section(store, {"s1"})
    assert sec["watched"] == 1 and sec["flagged"][0]["level"] == "error"
    # 模型挂了：规则层照常
    rec = W.review(store, session, SPEC, llm=lambda s, u: (_ for _ in ()).throw(RuntimeError("down")))
    assert rec["tagger"]["kind"] == "rules" and "down" in rec["tagger"]["llm_error"]


def test_stress_drill_api_runs_isolated(env, monkeypatch):
    """压力演练接口：后台取行情（这里没有网络 → 合成兜底）、隔离进程里跑策略、按场景出结果。"""
    import time

    from quant_agent.engine import stress as ST

    client, _broker, _mp = env
    monkeypatch.setattr(ST._Market, "_adapter", lambda self: None)
    r = client.post(
        "/api/studio/strategies", json={"name": "演练", "code": FLIP, "securities": ["BTC/USDT"], "timeframe": "1h"}
    )
    slug = r.json()["strategy"]["slug"]
    assert len(client.get("/api/validation/stress/scenarios").json()["scenarios"]) == 4
    r = client.post("/api/validation/stress", json={"slug": slug, "scenarios": ["may19-2021-05-19"]})
    assert r.status_code == 200, r.text
    rid = r.json()["run"]["id"]
    for _ in range(120):
        run = client.get(f"/api/validation/stress/{rid}").json()["run"]
        if run["status"] != "running":
            break
        time.sleep(0.25)
    assert run["status"] == "done", run.get("error")
    sc = run["report"]["scenarios"][0]
    assert sc["completed"] and sc["data_source"] == "synthetic" and sc["orders"] > 0
    assert run["report"]["summary"]["passed"]
    assert client.get("/api/validation/stress").json()["runs"][0]["id"] == rid


MA_TUNE = """
PARAMS = {"fast": 8, "slow": 24}
PARAM_SPACE = {"fast": (3, 15), "slow": (20, 60)}

def initialize(ctx):
    pass

def handle_data(ctx):
    s = ctx.securities[0]
    c = history(s, PARAMS["slow"], "close")
    if len(c) < PARAMS["slow"]:
        return
    f = sum(c[-PARAMS["fast"]:]) / PARAMS["fast"]
    order_target_percent(s, 0.8 if f > sum(c) / len(c) else 0.0)
"""


def test_checkup_api_and_graduation_gate(env):
    """体检接口：后台取数、隔离进程跑四项检查、红队（没配大模型 → 规则清单）；毕业闸门读最新一次。"""
    import time

    client, _broker, _mp = env
    r = client.post(
        "/api/studio/strategies", json={"name": "体检", "code": MA_TUNE, "securities": ["BTC/USDT"], "timeframe": "1h"}
    )
    slug = r.json()["strategy"]["slug"]
    r = client.post("/api/validation/checkup", json={"slug": slug, "bars_limit": 600, "use_llm": False})
    assert r.status_code == 200, r.text
    cid = r.json()["run"]["id"]
    for _ in range(400):
        got = client.get(f"/api/validation/checkup/{cid}").json()
        if got["run"]["status"] != "running":
            break
        time.sleep(0.25)
    run = got["run"]
    assert run["status"] == "done", run.get("error")
    ids = {i["id"]: i["verdict"] for i in run["report"]["items"]}
    assert ids["lookahead"] == "ok" and set(ids) == {"lookahead", "warmup", "cost", "plateau"}
    assert run["redteam"]["mode"] == "rules" and run["redteam"]["points"]
    assert got["stale"] is False
    assert client.get(f"/api/validation/checkup?slug={slug}").json()["runs"][0]["id"] == cid
    # 改了代码 → 这次体检过期（毕业闸门同样按代码哈希判断）
    client.post(
        "/api/studio/strategies",
        json={
            "name": "体检",
            "slug": slug,
            "code": MA_TUNE + "\n# v2\n",
            "securities": ["BTC/USDT"],
            "timeframe": "1h",
        },
    )
    assert client.get(f"/api/validation/checkup/{cid}").json()["stale"] is True


def test_bayes_optimize_api_records_trials_in_ledger(env):
    pytest.importorskip("optuna")
    client, _broker, _mp = env
    r = client.post(
        "/api/studio/strategies",
        json={"name": "智能调参", "code": MA_TUNE, "securities": ["BTC/USDT"], "timeframe": "1h"},
    )
    slug = r.json()["strategy"]["slug"]
    r = client.post(
        "/api/research/optimize",
        json={"slug": slug, "method": "bayes", "n_trials": 6, "bars_limit": 600, "securities": ["BTC/USDT"]},
    )
    assert r.status_code == 200, r.text
    opts = client.get(f"/api/research/optimizations?slug={slug}").json()["optimizations"]
    assert opts and opts[0]["method"] == "bayes" and 1 <= len(opts[0]["trial_sharpes"]) <= 6
    fam = next(f for f in client.get("/api/rd/ledger").json()["families"] if f["family"] == slug)
    assert fam["n"] >= len(opts[0]["trial_sharpes"])
    bad = client.post(
        "/api/research/optimize",
        json={"slug": slug, "method": "bayes", "space": {"fast": {"low": 5, "high": 1}}, "bars_limit": 600},
    )
    assert bad.status_code == 400 and "下限" in bad.text
