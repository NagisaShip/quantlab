"""自适应与衰减：衰减监测（误报与检出）、降级建议、模型定期重训（无未来数据、版本与血缘）、市场状态与调度权重。"""

from __future__ import annotations

import random

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_factor_research import _panel

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.research import decay as D
from quant_agent.research import graduation as GR
from quant_agent.research import models as M
from quant_agent.research import regime as RG
from quant_agent.research import retrain as RT
from quant_agent.research.model_lib import ModelLibrary
from quant_agent.store import JsonStore

DAY = D.DAY
T0 = 1_700_000_000_000


def _backtest(seed: int = 1) -> dict:
    r = random.Random(seed)
    return {
        "id": "bt1",
        "daily": [{"return": r.gauss(0.002, 0.012)} for _ in range(365)],
        "round_trips": [{"return": r.gauss(0.004, 0.02)} for _ in range(730)],
    }


def _live(seed: int, mu: float, sd: float, tmu: float, days: int = 14):
    """按小时的净值点，每 12 小时平一笔仓（卖出带 realized_pnl）。"""
    r = random.Random(seed)
    pts, fills, v = [], [], 10_000.0
    for h in range(days * 24 + 1):
        pts.append({"t": T0 + h * 3_600_000, "v": v})
        v *= 1 + r.gauss(mu / 24, sd / 24**0.5)
        if h % 12 == 6:
            x = r.gauss(tmu, 0.02)
            fills.append(
                {"side": "sell", "realized_pnl": 1000 * x, "notional": 1000 * (1 + x), "ts": T0 + h * 3_600_000}
            )
    return pts, fills


def _first_decay(prof, pts, fills):
    for d in range(3, 15):
        cut = T0 + d * DAY
        a = D.assess(
            prof, D.live_stats([p for p in pts if p["t"] <= cut], [f for f in fills if f["ts"] <= cut]), samples=400
        )
        if a["status"] == "decay":
            return d
    return None


def test_broken_strategy_flagged_within_two_weeks_and_healthy_rarely():
    prof = D.profile(_backtest())
    healthy = [_first_decay(prof, *_live(s, 0.002, 0.012, 0.004)) for s in range(25)]
    broken = [_first_decay(prof, *_live(100 + s, -0.005, 0.015, -0.008)) for s in range(25)]
    assert sum(x is not None for x in healthy) <= 2, healthy
    assert sum(x is not None for x in broken) >= 22, broken


def test_warming_and_reasons():
    prof = D.profile(_backtest())
    pts, fills = _live(5, -0.01, 0.015, -0.01, days=2)
    assert D.assess(prof, D.live_stats(pts, fills))["status"] == "warming"
    pts, fills = _live(5, -0.01, 0.015, -0.01, days=10)
    a = D.assess(prof, D.live_stats(pts, fills), samples=400)
    assert a["status"] == "decay" and a["reasons"] and a["score"]["value"] >= a["score"]["decay"]
    assert {m["key"] for m in a["metrics"]} == {"return", "max_drawdown", "win_rate", "trade_mean"}


def test_check_session_suggests_one_stage_back(tmp_path):
    store = JsonStore(tmp_path)
    store.upsert(GR.GRAD, {"id": "s1", "slug": "s1", "stage": "demo", "history": []})
    pts, fills = _live(7, -0.008, 0.015, -0.01, days=12)
    sess = {"id": "p1", "strategy_id": "s1", "strategy_name": "均线", "equity_points": pts, "fills": fills}
    rep = D.check_session(store, sess, backtest=_backtest())
    assert rep["status"] == "decay" and rep["suggest_demote_to"] == "paper" and rep["changed"]
    again = D.check_session(store, sess, backtest=_backtest())
    assert not again["changed"] and again["first_decay_at"] == rep["first_decay_at"]


# ---------------------------------------------------------------- 重训
FEATS = ["-returns(5)", "ts_std(ret, 20)", "returns(30)"]


def _model(store):
    lib = ModelLibrary(store)
    panel = _panel(T=1200, N=8, signal=0.15)
    spec = M.normalize_spec({"features": FEATS, "model": "ridge", "label": "fwd_ret"})
    first = M.train_and_evaluate(spec, panel.slice(0, 800))
    run = lib.record_run(spec, first["report"], first["state"])
    rec = lib.save_from_run(run["id"], name="反转", status="active")
    return lib, rec, panel


def test_retrain_uses_only_data_before_asof_and_versions(tmp_path):
    lib, rec, panel = _model(JsonStore(tmp_path))
    asof = int(panel.times[1000])
    out = RT.retrain(lib, rec["id"], panel, asof_ms=asof)
    e = out["retrain"]
    assert e["asof"] == asof and e["parent_run_id"] == rec["run_id"] and e["accepted"], e
    m = lib.get(rec["id"])
    assert len(m["versions"]) == 2 and m["versions"][-1]["parent_run_id"] == rec["run_id"]
    assert m["run_id"] == e["run_id"] and lib.run(e["run_id"])["source"] == "retrain"

    # 把 asof 之后的数据整个换掉，重训结果必须一模一样：说明没用到未来数据
    poisoned = panel.slice(0, len(panel.times))
    poisoned.data = {k: v.copy() for k, v in panel.data.items()}
    after = panel.times > asof
    for k in poisoned.data:
        poisoned.data[k][after] = poisoned.data[k][after] * 3 + 7
    lib2, rec2, _ = _model(JsonStore(tmp_path / "b"))
    out2 = RT.retrain(lib2, rec2["id"], poisoned, asof_ms=asof)
    assert out2["retrain"]["oos_rank_ic"] == pytest.approx(e["oos_rank_ic"])


def test_retrain_rejects_worse_and_respects_frozen(tmp_path):
    lib, rec, panel = _model(JsonStore(tmp_path))
    RT.set_plan(lib, rec["id"], min_ratio=50.0, enabled=True, every_days=7)
    out = RT.retrain(lib, rec["id"], panel, asof_ms=int(panel.times[1000]))
    assert not out["retrain"]["accepted"] and lib.get(rec["id"])["run_id"] == rec["run_id"]
    assert not RT.due(lib.get(rec["id"]))
    RT.set_plan(lib, rec["id"], min_ratio=0.0)
    lib.patch(rec["id"], frozen=True)
    out = RT.retrain(lib, rec["id"], panel, asof_ms=int(panel.times[1100]))
    assert out["retrain"]["saved_as"] != rec["id"] and lib.get(rec["id"])["run_id"] == rec["run_id"]


def test_retrain_due_by_plan(tmp_path):
    lib, rec, _ = _model(JsonStore(tmp_path))
    assert not RT.due(rec)
    RT.set_plan(lib, rec["id"], enabled=True, every_days=7)
    m = lib.get(rec["id"])
    assert not RT.due(m) and RT.due(m, now=RT.last_retrain(m) + 7 * 86_400 + 1)


# ---------------------------------------------------------------- 市场状态与权重
def test_regime_classify_and_weights():
    trend = [100 * (1.01**i) for i in range(60)]
    assert RG.classify(trend)[-1] == "trend"
    chop = [100 + (1 if i % 2 else -1) for i in range(60)]
    assert RG.classify(chop)[-1] == "range"
    assert RG.classify(trend)[: RG.WIN] == [None] * RG.WIN
    w = RG.weights({"a": {"regimes": {"trend": 2.0}}, "b": {"regimes": {"trend": 0.5}}}, "trend")
    assert w["a"] == pytest.approx(0.8) and w["b"] == pytest.approx(0.2)
    rets = [0.01] * 30 + [-0.01] * 30
    labels = ["trend"] * 30 + ["range"] * 30
    mult = RG.multipliers(rets, labels)
    assert mult["trend"] > 1 > mult["range"]


# ---------------------------------------------------------------- 接口
@pytest.fixture()
def client(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_api_overview_plan_and_demote(client):
    from quant_agent.api.deps import get_services
    from quant_agent.platform import context

    ws = client.headers["X-Workspace-Id"]
    s = get_services()
    with context.bind(ws, user_id="u"):
        store = s.store
        store.upsert(GR.GRAD, {"id": "s1", "slug": "s1", "stage": "demo", "history": []})
        pts, fills = _live(7, -0.008, 0.015, -0.01, days=12)
        D.check_session(
            store,
            {"id": "p1", "strategy_id": "s1", "strategy_name": "均线", "equity_points": pts, "fills": fills},
            backtest=_backtest(),
        )
        _lib, rec, _ = _model(store)
    body = client.get("/api/lifecycle").json()
    assert body["decay"][0]["status"] == "decay" and body["models"][0]["id"] == rec["id"]
    r = client.post(f"/api/models/{rec['id']}/retrain-plan", json={"enabled": True, "every_days": 3})
    assert r.json()["plan"]["every_days"] == 3
    r = client.post("/api/lifecycle/p1/demote")
    assert r.status_code == 200 and r.json()["graduation"]["stage"] == "paper"
    assert client.post("/api/lifecycle/p1/demote").status_code == 400
    assert client.post("/api/lifecycle/check").status_code == 200


def test_scan_queues_due_retrain_once_per_day(client):
    from quant_agent.api.deps import get_services
    from quant_agent.api.tasks import scan_lifecycle
    from quant_agent.platform import context

    ws = client.headers["X-Workspace-Id"]
    s = get_services()
    with context.bind(ws, user_id="u"):
        lib, rec, _ = _model(s.store)
        RT.set_plan(lib, rec["id"], enabled=True, every_days=1)
        lib.patch(rec["id"], created_at=1.0, versions=[{**v, "at": 1.0} for v in lib.get(rec["id"])["versions"]])
    a = scan_lifecycle(s)
    b = scan_lifecycle(s)
    assert len(a["queued"]) == 1 and b["queued"] == a["queued"]
