"""执行与监控：盘口带、成交模拟、成本分解恒等式、无前视、市场状态、录制回放、成本校准、容量、API 与研究室。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.research import execution as EX
from quant_agent.research.execution_lib import ExecutionLibrary, start_recording
from quant_agent.store import JsonStore


@pytest.fixture(scope="module")
def tape():
    return EX.synthetic_tape(steps=1500, seed=3)


def test_market_order_walks_the_book_and_decomposition_holds(tape):
    cfg = EX.normalize_config({"algo": "market"})
    states, _ = EX.detect_states(tape)
    k = 500
    Q = tape.bid_qty[k, :3].sum()  # 吃掉三档
    r = EX.run_episode(tape, cfg, k, 1, Q, states, record=True)
    assert r["avg_price"] > tape.ask_px[k, 0] and r["fill_rate"] == pytest.approx(1)
    parts = sum(r[x] for x in ("spread_bps", "impact_bps", "timing_bps", "fee_bps", "opportunity_bps"))
    assert parts == pytest.approx(r["is_bps"], abs=1e-9)
    for algo in EX.ALGOS:
        c = EX.normalize_config({"algo": algo, "episodes": 6})
        for row in EX.simulate(tape, c):
            parts = sum(row[x] for x in ("spread_bps", "impact_bps", "timing_bps", "fee_bps", "opportunity_bps"))
            assert parts == pytest.approx(row["is_bps"], abs=1e-8)
            assert 0 <= row["fill_rate"] <= 1 + 1e-9


def test_passive_fills_never_beat_the_limit_and_no_lookahead(tape):
    cfg = EX.normalize_config({"algo": "twap", "style": "passive", "finish": False})
    states, _ = EX.detect_states(tape)
    s = 700
    Q = EX.order_qty(tape, cfg, s)
    full = EX.run_episode(tape, cfg, s, 1, Q, states, record=True)
    assert full["maker_ratio"] == pytest.approx(1) or full["children"] == 0
    for f in full["fills"]:
        assert f["maker"]
    # 截掉执行期之后的数据，结果不变（状态与成交都只用到当时及以前）
    cut = tape.slice(0, s + cfg["horizon"] + 20)
    st2, _ = EX.detect_states(cut)
    assert (st2 == states[: len(cut)]).all()
    short = EX.run_episode(cut, cfg, s, 1, Q, st2)
    assert short["fill_rate"] == pytest.approx(full["fill_rate"]) and short["spread_bps"] == pytest.approx(
        full["spread_bps"]
    )


def test_states_and_hmm():
    rng = np.random.default_rng(0)
    x = np.concatenate([rng.normal(0, 0.001, 600), rng.normal(0, 0.006, 300), rng.normal(0, 0.001, 600)])
    m = EX.hmm_fit(x, n=2)
    assert math.sqrt(m["var"][1]) > 3 * math.sqrt(m["var"][0])
    p = EX.hmm_filter(x, m)
    assert p[650:850, 1].mean() > 0.8 and p[100:500, 0].mean() > 0.8
    assert (EX.corwin_schultz(np.array([10.2, 10.3, 10.1]), np.array([9.9, 10.0, 9.8])) >= 0).all()


def test_evaluate_scheme_report(tape):
    rep = EX.evaluate_scheme({"algo": "adaptive", "episodes": 12}, [tape])
    s = rep["summary"]
    assert {"is_bps", "twap_is_bps", "market_is_bps", "beat_twap_bps", "vs_vwap_bps", "markout_5"} <= set(s)
    assert s["fill_rate"] > 0.99 and s["maker_ratio"] > 0.3
    assert rep["frontier"] and rep["example"]["timeline"] and rep["cost_curve"]["points"]
    assert rep["tape"]["synthetic"] and any(c["key"] == "real" and not c["ok"] for c in rep["checks"])
    with pytest.raises(EX.ExecutionError):
        EX.normalize_config({"algo": "nope"})


class FakeBook:
    def __init__(self):
        self.i = 0

    def fetch_order_book(self, symbol, depth):
        self.i += 1
        mid = 100 + 0.01 * math.sin(self.i / 3)
        return {
            "ts": 1_700_000_000_000 + self.i * 1000,
            "bids": [[mid - 0.01 - 0.01 * k, 5 + k] for k in range(depth)],
            "asks": [[mid + 0.01 + 0.01 * k, 5 + k] for k in range(depth)],
        }

    def fetch_trades(self, symbol):
        t = 1_700_000_000_000 + self.i * 1000
        return [
            {
                "id": f"{self.i}-{j}",
                "t": t + 100 * j,
                "p": 100 + 0.01 * (-1) ** j,
                "q": 0.5,
                "side": "buy" if j % 2 else "sell",
            }
            for j in range(5)
        ]


def test_recording_replay(tmp_path):
    lib = ExecutionLibrary(JsonStore(tmp_path))
    rec = start_recording(
        lib, FakeBook(), exchange="fake", symbol="X/USDT", seconds=10, interval=0.5, depth=5, sync=True
    )
    rec = lib.recording(rec["id"])
    assert rec["status"] == "done" and rec["snapshots"] >= 19
    data = lib.recording_data(rec["id"])
    t = EX.tape_from_recording({**data, "snapshots": data["snapshots"] * 8})  # 重复拼长一点只为跑通
    assert t.source == "recording" and t.bid_px.shape[1] == EX.LEVELS and t.volume.sum() > 0


def test_calibrate_and_capacity():
    rng = np.random.default_rng(1)
    adv, sig = 1e8, 0.03
    fills = []
    for i in range(200):
        notional = 10 ** rng.uniform(3, 6.5)
        slip = 2.0 + 0.8 * sig * 1e4 * math.sqrt(notional / adv) + rng.normal(0, 0.5)
        side = 1 if i % 2 else -1
        price = 100 * (1 + side * slip / 1e4)
        fills.append(
            {"ts": i, "symbol": "A", "side": "buy" if side > 0 else "sell", "qty": notional / price, "price": price}
        )
    m = EX.calibrate(fills, {"A": {"ref": lambda t: 100.0, "adv_usd": adv, "sigma_d": sig}})
    assert m["a_bps"] == pytest.approx(2.0, abs=0.5) and m["Y"] == pytest.approx(0.8, abs=0.15)
    cap = EX.capacity(
        m, [{"symbol": "A", "weight": 1.0, "adv_usd": adv, "sigma_d": sig}], turnover=20, alpha=0.3, rebalances=365
    )
    costs = [c["annual_cost"] for c in cap["curve"]]
    assert all(b >= a for a, b in zip(costs, costs[1:], strict=False))
    assert cap["half_alpha_aum"] and cap["zero_alpha_aum"] and cap["half_alpha_aum"] < cap["zero_alpha_aum"]


def test_set_execution_in_backtest(tmp_path):
    from test_factor_research import _panel
    from test_model_research import _bars

    from quant_agent.research.signals import prepare
    from quant_agent.sdk import load_source, run_backtest

    store = JsonStore(tmp_path)
    ExecutionLibrary(store).save(
        name="贵",
        config=EX.normalize_config({}),
        report={"summary": {}, "admitted": True, "cost_curve": {"a_bps": 40.0, "b_bps": 200.0, "r2": 1.0}},
        symbols=["S0"],
    )
    panel = _panel(T=300, N=2, seed=4)
    bars = _bars(panel)
    body = (
        "def initialize(ctx):\n    {x}\n    g.n = 0\n    run_daily(go, time='every_bar')\n\n"
        "def go(ctx):\n    g.n += 1\n    "
        "order_target_weights({{s: 0.5 if g.n % 2 == 0 else 0.0 for s in ['S0/USDT', 'S1/USDT']}})\n"
    )
    src_a = body.format(x="set_execution('贵')")
    src_b = body.format(x="pass")
    research, warnings = prepare(src_a, bars, "4h", store=store)
    assert "execution:贵" in research, warnings
    ra = run_backtest(load_source(src_a, name="a"), bars, research=research, timeframe="4h")
    rb = run_backtest(load_source(src_b, name="b"), bars, timeframe="4h")
    assert not ra.errors and not rb.errors
    assert ra.metrics["total_return"] < rb.metrics["total_return"]


# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


SIM = {"symbols": ["BTC/USDT"], "source": "synthetic", "steps": 1500}


def test_execution_api(client):
    meta = client.get("/api/execution/meta").json()
    assert {a["id"] for a in meta["algos"]} >= {"twap", "vwap", "pov", "iceberg", "almgren_chriss", "adaptive"}
    r = client.post("/api/execution/evaluate", json={"config": {"algo": "twap", "episodes": 8}, **SIM})
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    assert r.json()["report"]["summary"]["is_bps"] is not None
    assert client.post("/api/execution/evaluate", json={"config": {"algo": "zzz"}, **SIM}).status_code == 400
    rp = client.post("/api/execution/replay", json={"config": {"algo": "adaptive"}, **SIM}).json()
    assert len(rp["timeline"]) == 30 and rp["context"] and "twap_is_bps" in rp
    sc = client.post("/api/execution", json={"run_id": run["id"], "name": "TWAP 半小时", "status": "active"}).json()
    sid = sc["scheme"]["id"]
    assert sc["scheme"]["cost_model"]["b_bps"] is not None
    assert client.get("/api/execution").json()["schemes"][0]["name"] == "TWAP 半小时"
    assert client.get(f"/api/execution/{sid}/report").json()["report"]["summary"]
    assert client.patch(f"/api/execution/{sid}", json={"status": "retired"}).json()["scheme"]["status"] == "retired"
    rd = client.get("/api/execution/radar?universe=major10&timeframe=5m&source=synthetic").json()
    assert len(rd["rows"]) == 10 and all("state" in x for x in rd["rows"] if "error" not in x)
    cap = client.post("/api/execution/capacity", json={"symbols": ["BTC/USDT"], "turnover": 30, "alpha": 0.4}).json()
    assert cap["curve"] and cap["model"]["Y"] == 0.7
    assert client.post("/api/execution/costs/calibrate", json={}).status_code == 400
    get_services().market.adapters["binance"] = FakeBook()
    rec = client.post("/api/execution/recordings", json={"symbol": "BTC/USDT", "seconds": 10, "interval": 0.5}).json()
    assert rec["recording"]["status"] == "recording"
    client.post(f"/api/execution/recordings/{rec['recording']['id']}/stop")
    assert client.get("/api/execution/recordings").json()["recordings"]


def test_execution_project_autopilot_and_save(client):
    r = client.post(
        "/api/lab/projects",
        json={
            "kind": "execution",
            "idea": "半小时内买入 BTC，比 TWAP 便宜",
            "securities": ["BTC/USDT"],
            "data_source": "synthetic",
            "bars_limit": 1500,
            "max_rounds": 2,
        },
    )
    assert r.status_code == 200, r.text
    pid = r.json()["project"]["id"]
    view = client.post(f"/api/lab/projects/{pid}/autopilot", json={"wait": True}).json()
    kinds = [m["kind"] for m in view["messages"]]
    assert "execution_spec" in kinds and "execution_report" in kinds and "ask" in kinds
    r = client.post(f"/api/lab/projects/{pid}/decision", json={"choice": "存为候选", "wait": True})
    sid = r.json()["project"]["scheme_id"]
    assert sid and client.get(f"/api/execution/{sid}").json()["scheme"]["status"] == "candidate"
    r = client.post(
        f"/api/lab/projects/{pid}/code", json={"code": '{"algo": "pov", "participation": 0.05}', "wait": True}
    )
    assert r.status_code == 200 and '"pov"' in r.json()["project"]["code"]
    g = client.post("/api/lab/goal/parse", json={"text": "完成率>98% 比TWAP省>1bps", "domain": "execution"}).json()
    assert {i["metric"] for i in g["spec"]["items"]} == {"fill_rate", "beat_twap_bps"}
    assert (
        client.post("/api/lab/projects", json={"kind": "execution", "data_source": "recording:nope"}).status_code == 200
    )
