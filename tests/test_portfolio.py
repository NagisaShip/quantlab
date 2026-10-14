"""组合与风控：风险模型、优化器、约束、风控叠加、压力测试、滚动回测无前视、方案库、optimize()、API 与研究室。"""

from __future__ import annotations

import numpy as np
import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_factor_research import _panel
from test_model_research import _bars

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.research import portfolio as PF
from quant_agent.research.panel import Panel
from quant_agent.research.portfolio_lib import PortfolioLibrary
from quant_agent.research.signals import prepare
from quant_agent.sdk import load_source, run_backtest
from quant_agent.store import JsonStore


def _cov(n=5, seed=0):
    rng = np.random.default_rng(seed)
    A = rng.normal(size=(300, n)) * np.linspace(0.01, 0.03, n)
    A[:, 1] += A[:, 0] * 0.8
    return A, np.cov(A, rowvar=False)


def test_ledoit_wolf_is_valid_shrinkage():
    R, _S = _cov(6)
    LW, delta = PF.ledoit_wolf(R[:40])
    assert 0 <= delta <= 1
    assert np.linalg.eigvalsh(LW).min() > 0
    assert np.allclose(LW, LW.T)
    for m in PF.RISK_MODELS:
        C = PF.covariance(R, m)
        assert C.shape == (6, 6) and np.linalg.eigvalsh(C).min() > 0


def test_projection_and_constraints():
    rng = np.random.default_rng(3)
    for _ in range(100):
        n = int(rng.integers(2, 12))
        v = rng.normal(size=n)
        hi = float(rng.uniform(1 / n, 1))
        w = PF.project_simplex_box(v, 1.0, 0.0, hi)
        assert abs(w.sum() - min(1.0, n * hi)) < 1e-8 and w.max() <= hi + 1e-12 and w.min() >= -1e-12
    w = PF.apply_constraints(np.array([0.5, 0.3, 0.1, 0.1]), {"long_only": True, "max_weight": 0.3, "top_k": 2})
    assert (w > 0).sum() == 2 and abs(w.sum() - 0.6) < 1e-9  # 两个各 30%，其余是现金


def test_optimizers_sanity():
    R, S = _cov(5)
    w = PF.risk_parity(S)
    rc = PF.risk_contribution(w, S)
    assert np.allclose(rc, 0.2, atol=1e-4)
    h = PF.hrp(S)
    assert abs(h.sum() - 1) < 1e-9 and (h > 0).all()
    cons = {"long_only": True, "max_weight": 1.0, "max_gross": 1.0, "top_k": 0}
    mv = PF._pgd(S, np.zeros(5), 1.0, cons, iters=3000)
    ew = np.full(5, 0.2)
    assert mv @ S @ mv <= ew @ S @ ew
    assert mv[0] > mv[-1]  # 低波动的权重更高
    # BL 没有观点时后验 = 先验
    post, pi = PF.black_litterman(S, [f"S{i}" for i in range(5)], [], delta=3, tau=0.05, w_prior=ew, per_bar=2190)
    assert np.allclose(post, pi)
    post, _ = PF.black_litterman(
        S,
        [f"S{i}" for i in range(5)],
        [{"symbol": "S4", "ret": 2.0, "confidence": 0.9}],
        delta=3,
        tau=0.05,
        w_prior=ew,
        per_bar=2190,
    )
    assert post[4] > pi[4]
    cv = PF.min_cvar(R, 0.95, cons)
    assert abs(cv.sum() - 1) < 1e-9


def test_scheme_backtest_has_no_lookahead_and_respects_rules():
    panel = _panel(T=900, N=6, seed=5)
    cfg = {"optimizer": "min_var", "lookback": 120, "rebalance": 10, "constraints": {"max_weight": 0.25}}
    full = PF.evaluate_scheme(cfg, panel)
    cut = PF.evaluate_scheme(cfg, panel.slice(0, 600))
    a = {r["t"]: r for r in full["weights"]}
    shared = [r for r in cut["weights"] if r["t"] in a]
    assert shared and all(a[r["t"]] == r for r in shared)  # 截断未来数据不改变过去的权重
    assert all(v <= 0.25 + 1e-6 for r in full["weights"] for k, v in r.items() if k != "t")
    s = full["summary"]
    assert s["ew_sharpe"] is not None and s["rebalances"] > 10
    assert {"latest", "risk", "stress", "frontier", "checks"} <= set(full)
    vt = PF.evaluate_scheme({**cfg, "risk": {"vol_target": 0.05}}, panel)
    assert vt["summary"]["ann_vol"] < s["ann_vol"] and vt["summary"]["triggers"].get("vol_target")
    assert vt["latest"]["cash"] > 0.3


def test_stress_test_replays_history_when_covered():
    T = 400
    times = np.arange(T, dtype=np.int64) * 14_400_000 + 1_583_000_000_000  # 覆盖 2020-03
    rng = np.random.default_rng(1)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (T, 2)), axis=0))
    panel = Panel(times, ["BTC/USDT", "ETH/USDT"], {"close": close}, "4h", {"BTC/USDT": "x", "ETH/USDT": "x"})
    out = PF.risk_check({"BTC/USDT": 0.5, "ETH/USDT": 0.5}, panel, lookback=200)
    by = {x["id"]: x for x in out["stress"]}
    assert by["covid_2020_03"]["method"] == "历史重放"
    assert by["luna_2022_05"]["method"] == "按贝塔推算" and by["luna_2022_05"]["pnl"] < 0
    assert "corr_09" in by and out["ann_vol"] > 0


def test_optimize_in_strategy_backtest(tmp_path):
    store = JsonStore(tmp_path)
    panel = _panel(T=500, N=5, seed=2)
    PortfolioLibrary(store).save(
        name="风险平价",
        config=PF.normalize_config({"optimizer": "risk_parity", "lookback": 60}),
        report=None,
        symbols=panel.symbols,
        timeframe="4h",
    )
    src = (
        "def initialize(ctx):\n    run_daily(rebalance, time='every_bar')\n\n"
        "def rebalance(ctx):\n    w = optimize('风险平价')\n    record(n=len(w))\n    if w:\n        "
        "order_target_weights(w)\n"
    )
    bars = _bars(panel)
    research, _warnings = prepare(src, bars, "4h", store=store)
    assert "optimize:风险平价" in research
    res = run_backtest(load_source(src, name="s"), bars, research=research, timeframe="4h")
    assert not res.errors, res.errors[:3]
    assert res.fills and max(r["v"] for r in res.records["n"]) == 5
    w = PF.optimize_live(
        {"optimizer": "hrp", "constraints": {"max_weight": 0.3}},
        {s: list(panel.data["close"][-120:, j]) for j, s in enumerate(panel.symbols)},
        {},
    )
    assert abs(sum(w.values()) - 1) < 1e-4 and max(w.values()) <= 0.3 + 1e-9


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


EV = {"universe": "major10", "timeframe": "4h", "bars_limit": 800, "source": "synthetic"}


def test_portfolio_api(client):
    meta = client.get("/api/portfolio/meta").json()
    assert {o["id"] for o in meta["optimizers"]} >= {"risk_parity", "hrp", "black_litterman", "min_cvar", "kelly"}
    r = client.post("/api/portfolio/evaluate", json={"config": {"optimizer": "hrp"}, **EV})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report"]["summary"]["sharpe"] is not None and body["report"]["stress"]
    assert client.post("/api/portfolio/evaluate", json={"config": {"optimizer": "zzz"}, **EV}).status_code == 400
    s = client.post("/api/portfolio", json={"run_id": body["run"]["id"], "name": "HRP 主流", "status": "active"}).json()
    sid = s["scheme"]["id"]
    assert client.get("/api/portfolio").json()["schemes"][0]["name"] == "HRP 主流"
    assert client.get(f"/api/portfolio/{sid}/report").json()["report"]["summary"]
    assert client.patch(f"/api/portfolio/{sid}", json={"status": "retired"}).json()["scheme"]["status"] == "retired"
    rk = client.post("/api/portfolio/risk", json={"weights": {"BTC/USDT": 0.6, "ETH/USDT": 0.4}, "source": "synthetic"})
    assert rk.status_code == 200 and rk.json()["stress"]
    assert len(client.get("/api/portfolio/runs").json()["runs"]) == 1


def test_portfolio_project_autopilot_and_save(client):
    r = client.post(
        "/api/lab/projects",
        json={
            "kind": "portfolio",
            "idea": "主流币风险平价，控制回撤",
            "data_source": "synthetic",
            "bars_limit": 800,
            "max_rounds": 2,
        },
    )
    assert r.status_code == 200, r.text
    pid = r.json()["project"]["id"]
    assert "portfolio" in {k["id"] for k in client.get("/api/lab/meta").json()["kinds"]}
    view = client.post(f"/api/lab/projects/{pid}/autopilot", json={"wait": True}).json()
    kinds = [m["kind"] for m in view["messages"]]
    assert "portfolio_spec" in kinds and "portfolio_report" in kinds and "ask" in kinds
    r = client.post(f"/api/lab/projects/{pid}/decision", json={"choice": "存为候选", "wait": True})
    sid = r.json()["project"]["scheme_id"]
    assert sid and client.get(f"/api/portfolio/{sid}").json()["scheme"]["status"] == "candidate"
    r = client.post(
        f"/api/lab/projects/{pid}/code",
        json={"code": '{"optimizer": "min_var", "risk": {"vol_target": 0.3}}', "wait": True},
    )
    assert r.status_code == 200 and '"min_var"' in r.json()["project"]["code"]
    assert client.post(f"/api/lab/projects/{pid}/code", json={"code": '{"optimizer": "nope"}'}).status_code == 400
    g = client.post("/api/lab/goal/parse", json={"text": "夏普>1 回撤<20%", "domain": "portfolio"}).json()
    assert {i["metric"] for i in g["spec"]["items"]} == {"sharpe", "max_drawdown"}
