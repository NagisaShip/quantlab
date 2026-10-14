"""研究纪律与自动化：DSR / PBO、紧封测试集、血缘图、积木组装、夜班、晨报、影子运行。"""

from __future__ import annotations

import time

import numpy as np
import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.research import discipline as D
from quant_agent.research import lineage as LN
from quant_agent.research import nightshift as NS
from quant_agent.research import shadow as SH
from quant_agent.research.assemble import AssembleError, generate


# ---------------------------------------------------------------------------
# 纯计算
# ---------------------------------------------------------------------------
def test_deflated_sharpe_penalizes_many_trials():
    one = D.deflated_sharpe(1.5, n_trials=1, sr_ann_var=0.25, periods=2000, periods_per_year=2190)
    many = D.deflated_sharpe(1.5, n_trials=200, sr_ann_var=0.25, periods=2000, periods_per_year=2190)
    assert one["dsr"] > 0.9 and one["sr0_ann"] == 0
    assert many["dsr"] < one["dsr"] and many["sr0_ann"] > 0.5
    assert D.t_threshold(1) == pytest.approx(1.96, abs=0.01)
    assert D.t_threshold(100) > 3.4


def test_pbo_noise_vs_real_edge():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 0.01, size=(800, 12))
    r = D.pbo_cscv(noise, splits=10)
    assert 0.25 < r["pbo"] < 0.85 and r["combos"] == 252
    edge = noise.copy()
    edge[:, 3] += 0.004  # 真有优势的一列
    r2 = D.pbo_cscv(edge, splits=10)
    assert r2["pbo"] < 0.05 and r2["prob_oos_loss"] < 0.05
    with pytest.raises(ValueError):
        D.pbo_cscv(noise[:20], splits=10)
    keys, mat, ts = D.align_series(
        {"a": [(t, 1 + t * 0.01) for t in range(50)], "b": [(t, 1.0) for t in range(10, 60)]}
    )
    assert keys == ["a", "b"] and mat.shape == (39, 2) and ts[0] == 11


def test_shadow_compare():
    h = SH.HOUR
    t0 = 1_700_000_000_000
    rng = np.random.default_rng(3)
    base = [{"t": t0 + i * h, "v": 10000 * (1 + 0.0001 * i + 0.001 * rng.normal())} for i in range(24 * 10)]
    cand = [{"t": t0 + i * h, "v": 10000 * (1 + 0.0004 * i + 0.001 * rng.normal())} for i in range(24 * 10)]
    c = SH.compare({"equity_points": base}, {"equity_points": cand}, min_days=7)
    assert c["hours"] > 7 * 24 and c["excess"] > 0 and c["curve"]
    assert SH.compare({"equity_points": base[:30]}, {"equity_points": cand[:30]}, min_days=7)["verdict"].startswith(
        "观察中"
    )


def test_assemble_generates_valid_strategy():
    out = generate(
        {
            "name": "动量轮动",
            "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            "timeframe": "4h",
            "signal": {"type": "momentum", "lookback": 30},
            "build": {"type": "topk", "k": 2},
            "risk": {"max_weight": 0.6, "stop_dd": 0.2},
            "every": 6,
        }
    )
    code = out["code"]
    compile(code, "<assembled>", "exec")
    assert "PARAMS" in code and '"top_k": 2' in code and "set_universe" in code and "stop_dd" in code
    assert [x["step"] for x in out["outline"]] == ["标的池", "信号", "组合构建", "风控", "执行"]
    sch = generate(
        {
            "symbols": ["BTC/USDT"],
            "signal": {"type": "model", "name": "m1"},
            "build": {"type": "scheme", "name": "风险平价"},
            "execution": "TWAP",
        }
    )["code"]
    assert (
        "predict('m1')" in sch
        and "optimize('风险平价', expected=score or None)" in sch
        and "set_execution('TWAP')" in sch
    )
    with pytest.raises(AssembleError):
        generate({"symbols": ["BTC/USDT"], "signal": {"type": "none"}, "build": {"type": "topk"}})
    with pytest.raises(AssembleError):
        generate({"symbols": []})


def test_nightshift_due():
    cfg = {**NS.DEFAULT_CONFIG, "enabled": True, "hour": 1, "minute": 30}
    t = time.strptime("2026-10-13 02:00", "%Y-%m-%d %H:%M")
    assert NS.due(cfg, t, None)
    assert not NS.due(cfg, time.strptime("2026-10-13 01:00", "%Y-%m-%d %H:%M"), None)
    assert not NS.due(cfg, t, time.mktime(time.strptime("2026-10-13 01:40", "%Y-%m-%d %H:%M")))
    assert not NS.due({**cfg, "enabled": False}, t, None)


# ---------------------------------------------------------------------------
# API
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


FX = {"universe": "major10", "timeframe": "4h", "bars_limit": 800, "source": "synthetic"}


def test_holdout_seal_clamp_and_unseal(client):
    assert client.get("/api/rd/holdout").json()["sealed"] is False
    r = client.post("/api/rd/holdout", json={"days": 60, "note": "最近两个月"})
    assert r.status_code == 200 and r.json()["sealed"] and r.json()["days"] == pytest.approx(60, abs=0.1)
    cutoff = r.json()["cutoff"]
    rep = client.post("/api/factors/evaluate", json={"expression": "-returns(6)", **FX}).json()
    assert rep["panel"]["end"] <= cutoff
    assert any("紧封测试集" in w for w in rep["panel"]["warnings"])
    f = client.post(
        "/api/factors", json={"expression": "-returns(6)", "name": "短期反转", "status": "active", "evaluate": False}
    ).json()["factor"]
    u = client.post("/api/rd/holdout/unseal", json={"kind": "factor", "ref": f["id"]})
    assert u.status_code == 200, u.text
    assert u.json()["again"] is False and u.json()["record"]["result"]["bars"] >= 30
    again = client.post("/api/rd/holdout/unseal", json={"kind": "factor", "ref": f["id"]}).json()
    assert again["again"] is True and again["record"]["at"] == u.json()["record"]["at"]
    # 有开封记录后紧封线只能往前移
    assert client.post("/api/rd/holdout", json={"days": 30}).status_code == 409
    assert client.post("/api/rd/holdout", json={"days": 90}).status_code == 200
    h = client.get("/api/rd/holdout").json()
    assert len(h["records"]) == 1 and [x["action"] for x in h["log"]][:2] == ["change", "seal"]
    # 策略回测同样被夹在紧封线之前
    s = client.post("/api/research/strategies", json={"name": "均线", "template_id": "ma_cross"}).json()["strategy"]
    bt = client.post(
        "/api/research/backtests",
        json={"slug": s["slug"], "source": "synthetic", "bars_limit": 300, "timeframe": "1h", "wait_seconds": 60},
    ).json()
    assert bt["result"]["timestamps"][-1] <= h["cutoff"]
    assert any("紧封测试集" in w for w in bt["result"]["warnings"])
    client.post("/api/rd/holdout/remove", json={"reason": "测试结束"})
    assert client.get("/api/rd/holdout").json()["sealed"] is False


def test_ledger_pbo_lineage_assemble(client):
    for expr in ("-returns(3)", "returns(12)", "-ts_zscore(close, 20)"):
        assert client.post("/api/factors/evaluate", json={"expression": expr, **FX}).status_code == 200
    f = client.post(
        "/api/factors", json={"expression": "-returns(3)", "name": "反转3", "status": "active", "evaluate": False}
    ).json()["factor"]
    for opt in ("hrp", "risk_parity", "min_var", "inverse_vol"):
        client.post(
            "/api/portfolio/evaluate",
            json={
                "config": {"optimizer": opt},
                "universe": "major10",
                "timeframe": "4h",
                "bars_limit": 800,
                "source": "synthetic",
            },
        )
    lg = client.get("/api/rd/ledger").json()
    assert lg["by_domain"]["factor"] >= 3 and lg["by_domain"]["portfolio"] == 4
    fam = next(x for x in lg["families"] if x["domain"] == "portfolio")
    assert fam["n"] == 4 and 0 <= fam["dsr"] <= 1 and fam["verdict"] in ("ok", "warn", "bad")
    assert any(x.get("t_needed") for x in lg["families"] if x["domain"] == "factor")

    # 组装一个用因子库因子的策略并存下来
    opts = client.get("/api/rd/assemble/options").json()
    assert opts["factors"][0]["name"] == "反转3" and opts["universes"]
    blocks = {
        "name": "反转轮动",
        "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"],
        "timeframe": "4h",
        "signal": {"type": "factor", "name": "反转3"},
        "build": {"type": "topk", "k": 2},
        "risk": {"max_weight": 0.5},
    }
    pv = client.post("/api/rd/assemble", json={"blocks": blocks}).json()
    assert pv["issues"] == [] and pv["warnings"] == [] and "factor('反转3')" in pv["code"]
    bad = client.post(
        "/api/rd/assemble", json={"blocks": {**blocks, "signal": {"type": "model", "name": "不存在"}}}
    ).json()
    assert bad["warnings"]
    made = client.post("/api/rd/assemble", json={"blocks": blocks, "create": True}).json()
    slug = made["strategy"]["slug"]
    for n in (60, 120, 200):
        r = client.post(
            "/api/research/backtests",
            json={
                "slug": slug,
                "source": "synthetic",
                "bars_limit": 400,
                "timeframe": "4h",
                "wait_seconds": 60,
                "params": {},
                "label": f"b{n}",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["result"]["metrics"]["trades"] > 0
    p = client.post("/api/rd/ledger/pbo", json={"slug": slug})
    # 同一份代码三次回测收益完全一样：PBO 能算出来（排名并列）
    assert p.status_code == 200, p.text
    assert 0 <= p.json()["pbo"] <= 1 and p.json()["dsr"]["n_trials"] == 3

    g = client.get("/api/rd/lineage").json()
    ids = {n["id"] for n in g["nodes"]}
    assert f"factor:{f['id']}" in ids and f"strategy:{slug}" in ids
    assert {"from": f"factor:{f['id']}", "to": f"strategy:{slug}", "how": "factor()"} in g["edges"]
    assert any(e["to"] == f"factor:{f['id']}" for e in g["edges"])  # 数据 → 因子
    assert any(e["from"] == f"strategy:{slug}" and e["to"].startswith("backtest:") for e in g["edges"])
    # 上游因子改动之后，下游策略提示需要重跑
    time.sleep(1.1)
    assert client.patch(f"/api/factors/{f['id']}", json={"note": "改了假设"}).status_code == 200
    g2 = client.get(f"/api/rd/lineage?node=strategy:{slug}").json()
    assert any(s["id"] == f"strategy:{slug}" for s in g2["stale"])
    assert f"factor:{f['id']}" in g2["trace"]["upstream"]


def test_lineage_pure():
    class S:
        def __init__(self):
            self.d = {
                "factors": [{"id": "f1", "name": "资金费反转", "expression": "-ts_mean(funding, 3)", "updated_at": 10}],
                "ml_models": [
                    {
                        "id": "m1",
                        "name": "lgb",
                        "updated_at": 20,
                        "spec": {
                            "model": "ridge",
                            "label": "ret",
                            "features": [{"name": "x", "expression": "-ts_mean(funding, 3)"}],
                        },
                    }
                ],
                "pf_schemes": [{"id": "p1", "name": "BL", "updated_at": 30, "config": {"expected": "model:lgb"}}],
                "ex_schemes": [],
                "backtests": [],
            }

        def list(self, k):
            return self.d.get(k, [])

    g = LN.build(
        S(),
        [
            {
                "slug": "s1",
                "name": "策略",
                "updated_at": 40_000,
                "source": "def r(ctx):\n    w = optimize('BL', expected=predict('lgb'))\n",
            }
        ],
    )
    e = {(x["from"], x["to"]) for x in g["edges"]}
    assert ("data:funding", "factor:f1") in e and ("factor:f1", "model:m1") in e
    assert (
        ("model:m1", "portfolio:p1") in e and ("portfolio:p1", "strategy:s1") in e and ("model:m1", "strategy:s1") in e
    )
    assert not g["stale"]


def test_nightshift_brief_pipeline(client):
    assert client.post("/api/rd/nightshift/run", json={}).status_code == 400  # 队列空
    t1 = client.post(
        "/api/rd/nightshift/tasks",
        json={"kind": "factor", "idea": "资金费率高的币短期回调", "data_source": "synthetic", "rounds": 2},
    ).json()["task"]
    client.post(
        "/api/rd/nightshift/tasks", json={"kind": "portfolio", "idea": "主流币风险平价", "data_source": "synthetic"}
    )
    t3 = client.post(
        "/api/rd/nightshift/tasks", json={"kind": "execution", "idea": "大单比 TWAP 便宜", "data_source": "synthetic"}
    ).json()["task"]
    assert client.delete(f"/api/rd/nightshift/tasks/{t3['id']}").status_code == 200
    client.put("/api/rd/nightshift/config", json={"max_trials": 3, "rounds": 2, "brief": True, "channel": "console"})
    sh = client.post("/api/rd/nightshift/run", json={"wait": True}).json()["shift"]
    assert sh["status"] == "done" and sh["items"], sh
    first = sh["items"][0]
    assert first["kind"] == "factor" and first["project_id"] and first["headline"].startswith("因子")
    assert sh["used"]["trials"] >= 1
    # 预算 3 个版本：第一个任务两轮后，第二个任务被预算截停或不再开始
    assert sh["stop_reason"]
    view = client.get("/api/rd/nightshift").json()
    assert view["shifts"][0]["id"] == sh["id"] and view["running"] is None
    assert any(t["id"] == t1["id"] and t["status"] in ("done", "stopped") for t in view["tasks"])
    b = client.get("/api/rd/brief").json()["brief"]
    assert b and b["source"] == "nightshift" and any(s["key"] == "shift" for s in b["sections"])
    assert "研究晨报" in b["text"] and b["headline"]
    b2 = client.post("/api/rd/brief", json={"push": True, "channel": "console"}).json()["brief"]
    assert b2["pushed"] == ["console"]
    pl = client.get("/api/rd/pipeline").json()
    assert pl["nightshift"]["last"]["id"] == sh["id"] and pl["discipline"]["trials"] >= 1 and pl["brief"]["id"]
    assert client.get("/api/rd/shadows").json()["shadows"] == []
    assert (
        client.post("/api/rd/shadows", json={"baseline_session_id": "nope", "candidate_slug": "x"}).status_code == 404
    )


def test_project_pbo(client):
    r = client.post(
        "/api/lab/projects",
        json={
            "kind": "portfolio",
            "idea": "主流币配置",
            "data_source": "synthetic",
            "bars_limit": 1000,
            "max_rounds": 3,
            "timeframe": "4h",
        },
    )
    pid = r.json()["project"]["id"]
    client.post(f"/api/lab/projects/{pid}/autopilot", json={"wait": True})
    for opt in ("hrp", "min_var"):
        r = client.post(f"/api/lab/projects/{pid}/code", json={"code": f'{{"optimizer": "{opt}"}}', "wait": True})
        assert r.status_code == 200, r.text
    res = client.post("/api/rd/ledger/pbo", json={"project_id": pid})
    assert res.status_code == 200, res.text
    j = res.json()
    assert len(j["versions"]) >= 2 and 0 <= j["pbo"] <= 1 and j["hist"]
