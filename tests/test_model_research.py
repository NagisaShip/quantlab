"""模型研究：学习器、滚动样本外训练、泄漏检查、模型库、策略里的 factor() / predict()、模型库 API 与研究室模型项目。"""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_factor_research import _panel

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.exchanges.base import Bar
from quant_agent.research import learners as L
from quant_agent.research import models as M
from quant_agent.research.goals import MODEL
from quant_agent.research.model_lib import ModelLibrary
from quant_agent.research.signals import prepare, scan
from quant_agent.sdk import load_source, run_backtest
from quant_agent.store import JsonStore

FEATS = ["-returns(5)", "ts_std(ret, 20)", "returns(30)"]


def _spec(**kw):
    return M.normalize_spec({"features": FEATS, **kw})


# ---------------------------------------------------------------------------
# 学习器与训练
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kind,label",
    [
        ("ridge", "fwd_ret"),
        ("ridge", "direction"),
        ("gbdt", "cs_rank"),
        ("mlp", "fwd_ret"),
        ("ridge", "triple_barrier"),
    ],
)
def test_learners_find_planted_signal_out_of_sample(kind, label):
    panel = _panel(T=1000, N=10, signal=0.15)
    out = M.train_and_evaluate(_spec(model=kind, label=label), panel)
    s = out["report"]["summary"]
    assert s["oos_rank_ic"] > 0.08, s
    assert s["leak_passed"] == 1.0
    assert abs(s["shuffle_rank_ic"]) < 0.03
    assert s["fold_positive"] == 1.0
    assert out["report"]["importance"][0]["name"] == "-returns(5)"
    # 模型状态可以序列化并原样恢复
    m = L.load_learner(out["state"])
    ds = M.build_dataset(_spec(model=kind, label=label), panel)
    idx = np.arange(len(ds.rows))[:50]
    assert np.all(np.isfinite(m.predict(ds, idx)))
    if M.LABELS[label]["task"] == "clf":
        assert 0 <= s["brier"] <= 0.3 and out["report"]["calibration"]


def test_noise_does_not_pass_and_baseline_is_enforced():
    panel = _panel(T=900, N=10, signal=0.0, seed=4)
    out = M.train_and_evaluate(_spec(model="gbdt"), panel)
    s = out["report"]["summary"]
    assert abs(s["oos_rank_ic"]) < 0.04
    ev = MODEL.evaluate(s, MODEL.templates[0]["items"])
    assert ev["passed"] is False


def test_builtin_gbdt_without_lightgbm(monkeypatch):
    real = L._has
    monkeypatch.setattr(L, "_has", lambda mod: False if mod == "lightgbm" else real(mod))
    panel = _panel(T=900, N=10, signal=0.15)
    out = M.train_and_evaluate(_spec(model="gbdt", params={"n_estimators": 40}), panel)
    assert out["state"]["backend"] == "builtin"
    assert out["report"]["summary"]["oos_rank_ic"] > 0.08
    m = L.load_learner(out["state"])
    assert m.trees


def test_volatility_models_beat_naive_on_garch_data():
    rng = np.random.default_rng(3)
    T, N = 1500, 6
    ret = np.zeros((T, N))
    s2 = np.full(N, 1e-4)
    for t in range(1, T):
        s2 = 1e-6 + 0.1 * ret[t - 1] ** 2 + 0.88 * s2
        ret[t] = rng.normal(0, np.sqrt(s2))
    p = _panel(T=T, N=N)
    close = 100 * np.exp(np.cumsum(ret, axis=0))
    p.data.update(close=close, open=close, high=close, low=close, vwap=close, amount=close * p.data["volume"])
    prev = np.vstack([np.full((1, N), np.nan), close[:-1]])
    p.data["ret"] = close / prev - 1
    for kind in ("garch", "har"):
        out = M.train_and_evaluate(M.normalize_spec({"model": kind, "label": "volatility", "horizon": 4}), p)
        s = out["report"]["summary"]
        assert s["mode"] == "volatility" and s["oos_rank_ic"] > 0.2, (kind, s)
    with pytest.raises(M.ModelError):
        M.normalize_spec({"model": "garch", "label": "fwd_ret"})


def test_lookahead_check_catches_future_feature(monkeypatch):
    panel = _panel(T=600, N=8, signal=0.1)
    spec = _spec()
    assert all(x["ok"] for x in M.lookahead_check(spec, panel))
    real = M.evaluate

    def leaky(node, data):
        out = real(node, data)
        return np.vstack([out[1:], np.full((1, out.shape[1]), np.nan)])  # 偷看下一根

    monkeypatch.setattr(M, "evaluate", leaky)
    assert not all(x["ok"] for x in M.lookahead_check(spec, panel))


def test_shuffle_test_catches_train_test_overlap(monkeypatch):
    panel = _panel(T=600, N=8, signal=0.0, seed=9)
    real = M.folds

    def overlapping(ds, n):  # 故意把测试样本放进训练集：模型能“记住”打乱后的标签
        return [(np.concatenate([tr, te]), va, te, lo, hi) for tr, va, te, lo, hi in real(ds, n)]

    monkeypatch.setattr(M, "folds", overlapping)
    out = M.train_and_evaluate(
        _spec(model="gbdt", params={"min_leaf": 5, "max_depth": 8, "n_estimators": 300, "patience": 300}), panel
    )
    shuffle = next(c for c in out["report"]["checks"] if c["key"] == "shuffle")
    assert shuffle["ok"] is False and out["report"]["summary"]["leak_passed"] == 0


def test_rolling_predict_has_no_lookahead():
    panel = _panel(T=900, N=8, signal=0.1)
    spec = _spec(model="ridge")
    P_full, info = M.rolling_predict(spec, panel, retrain_every=100, min_train=300)
    cut = 650
    P_cut, _ = M.rolling_predict(spec, panel.slice(0, cut), retrain_every=100, min_train=300)
    np.testing.assert_allclose(P_full[:cut], P_cut, equal_nan=True, atol=1e-9)
    assert np.all(np.isnan(P_full[:300])) and info["fits"] >= 5


@pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="没有安装 PyTorch")
@pytest.mark.parametrize("kind", ["gru", "tcn", "transformer"])
def test_sequence_models(kind):
    panel = _panel(T=700, N=8, signal=0.15)
    out = M.train_and_evaluate(_spec(model=kind, params={"epochs": 4, "seq_len": 8}, shuffle_test=False), panel)
    assert out["report"]["summary"]["oos_rank_ic"] > 0.03
    assert L.load_learner(out["state"]).predict(M.build_dataset(_spec(model=kind), panel), np.arange(5)).shape == (5,)


def test_torch_models_report_missing_dependency(monkeypatch):
    real = L._has
    monkeypatch.setattr(L, "_has", lambda mod: False if mod == "torch" else real(mod))
    with pytest.raises(L.LearnerError, match="PyTorch"):
        L.make_learner("lstm", {}, "reg")
    assert L.available()["lstm"] is False


# ---------------------------------------------------------------------------
# 模型库
# ---------------------------------------------------------------------------
def test_model_library_runs_save_freeze_and_health(tmp_path):
    store = JsonStore(tmp_path)
    lib = ModelLibrary(store)
    panel = _panel(T=800, N=8, signal=0.15)
    spec = _spec(model="ridge")
    out = M.train_and_evaluate(spec, panel.slice(0, 600))
    run = lib.record_run(spec, out["report"], out["state"], source="test")
    assert lib.trial_count() == 1 and lib.run_report(run["id"])["summary"]
    rec = lib.save_from_run(run["id"], name="反转模型", status="active")
    assert rec["card"]["weaknesses"] and rec["card"]["purpose"].startswith("预测")
    assert lib.by_name("反转模型")["id"] == rec["id"]
    lib.patch(rec["id"], frozen=True)
    again = lib.save_from_run(run["id"], model_id=rec["id"], name="反转模型")
    assert again["id"] != rec["id"]  # 冻结的不覆盖
    h = lib.health(rec["id"], panel)
    assert h["entry"]["live_bars"] > 100 and h["entry"]["live_rank_ic"] > 0
    assert set(h["latest"]["values"]) == set(panel.symbols)


# ---------------------------------------------------------------------------
# 策略里的 factor() / predict()
# ---------------------------------------------------------------------------
STRATEGY = """
def initialize(ctx):
    run_daily(rebalance, time="every_bar")

def rebalance(ctx):
    s = factor('-returns(5)')
    p = predict('反转模型', ctx.securities[0])
    record(n=len(s), p=p if p is not None else -9)
    if s:
        best = max(s, key=s.get)
        order_target_weights({best: 0.5})
"""


def _bars(panel):
    out = {}
    for j, sym in enumerate(panel.symbols):
        rows = []
        for i, t in enumerate(panel.times):
            c = float(panel.data["close"][i, j])
            rows.append(
                Bar(
                    symbol=sym,
                    timeframe="4h",
                    open_time=int(t) - 14_400_000,
                    close_time=int(t),
                    open=c,
                    high=c * 1.001,
                    low=c * 0.999,
                    close=c,
                    volume=100.0,
                    closed=True,
                )
            )
        out[sym] = rows
    return out


def test_signals_prepare_and_backtest(tmp_path):
    assert scan(STRATEGY) == [("factor", "-returns(5)"), ("predict", "反转模型")]
    store = JsonStore(tmp_path)
    panel = _panel(T=700, N=6, signal=0.15)
    spec = _spec(model="ridge")
    out = M.train_and_evaluate(spec, panel)
    lib = ModelLibrary(store)
    lib.save_from_run(lib.record_run(spec, out["report"], out["state"])["id"], name="反转模型")
    bars = _bars(panel)
    research, warnings = prepare(STRATEGY, bars, "4h", store=store, hub=None, data_source="synthetic")
    assert set(research) == {"factor:-returns(5)", "predict:反转模型"}
    assert any("滚动重训" in w for w in warnings)
    tbl = research["factor:-returns(5)"]
    assert len(tbl["times"]) == 700 and tbl["values"][0][0] is None

    result = run_backtest(load_source(STRATEGY, name="s"), bars, research=research, timeframe="4h")
    assert not result.errors, result.errors
    assert result.fills
    n_series = [r["v"] for r in result.records["n"]]
    assert max(n_series) == 6
    # 没准备研究数据时不报错，只返回空并记一条日志
    result2 = run_backtest(load_source(STRATEGY, name="s"), bars, timeframe="4h")
    assert not result2.errors and not result2.fills
    assert any("没有预先计算" in x["message"] for x in result2.logs)


def test_factor_value_is_as_of_current_bar(tmp_path):
    panel = _panel(T=300, N=4)
    bars = _bars(panel)
    src = "def initialize(ctx):\n    pass\n\ndef handle_data(ctx):\n    record(c=factor('close', ctx.securities[0]))\n"
    research, _ = prepare(src, bars, "4h", store=JsonStore(tmp_path))
    result = run_backtest(load_source(src, name="s"), bars, research=research, timeframe="4h")
    rec = result.records["c"]
    closes = {int(t): float(panel.data["close"][i, 0]) for i, t in enumerate(panel.times)}
    for r in rec[:50]:
        assert abs(r["v"] - closes[r["t"]]) < 1e-6  # 当前 bar 收盘值，不是下一根


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


TRAIN = {"universe": "major10", "timeframe": "4h", "bars_limit": 700, "source": "synthetic"}


def test_models_api_train_runs_compare_and_library(client):
    meta = client.get("/api/models/meta").json()
    assert {x["id"] for x in meta["learners"]} >= {"ridge", "gbdt", "mlp", "lstm", "har", "garch"}
    assert meta["goal"]["metrics"]
    r = client.post("/api/models/train", json={"model": "ridge", **TRAIN, "wait": True})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["status"] == "done", job
    rid = job["run_id"]
    r2 = client.post(
        "/api/models/train", json={"model": "gbdt", "params": {"n_estimators": 30}, **TRAIN, "wait": True}
    ).json()["job"]
    runs = client.get("/api/models/runs").json()
    assert len(runs["runs"]) == 2 and runs["trials"] == 2
    cmp = client.get(f"/api/models/compare?a={rid}&b={r2['run_id']}").json()
    assert cmp["spec"][0] == {"key": "model", "a": "ridge", "b": "gbdt"}
    assert client.post("/api/models/train", json={"model": "nope", **TRAIN}).status_code == 400
    assert client.post("/api/models/train", json={"model": "garch", "label": "fwd_ret", **TRAIN}).status_code == 400

    m = client.post("/api/models", json={"run_id": rid, "name": "基线模型"}).json()["model"]
    assert m["card"]["features"] and client.get("/api/models").json()["models"][0]["id"] == m["id"]
    assert client.get(f"/api/models/{m['id']}/report").json()["report"]["summary"]
    assert client.patch(f"/api/models/{m['id']}", json={"status": "active", "frozen": True}).json()["model"]["frozen"]
    r = client.post(f"/api/models/{m['id']}/refresh", json={"source": "synthetic", "bars_limit": 800})
    assert r.status_code == 200, r.text
    assert r.json()["latest"]["values"]
    assert client.delete(f"/api/models/{m['id']}").json()["ok"]


def test_model_project_fallback_autopilot_and_save(client):
    body = {
        "kind": "model",
        "idea": "用动量和波动率预测 4 小时截面收益",
        "data_source": "synthetic",
        "bars_limit": 700,
        "max_rounds": 2,
        "timeframe": "4h",
    }
    r = client.post("/api/lab/projects", json=body)
    assert r.status_code == 200, r.text
    p = r.json()["project"]
    assert p["kind"] == "model"
    meta = client.get("/api/lab/meta").json()
    assert "model" in meta["domains"] and "model" in {k["id"] for k in meta["kinds"]}

    r = client.post(f"/api/lab/projects/{p['id']}/autopilot", json={"wait": True})
    assert r.status_code == 200, r.text
    view = r.json()
    kinds = [m["kind"] for m in view["messages"]]
    assert "model_spec" in kinds and "model_report" in kinds and "ask" in kinds
    assert view["project"]["versions"][0]["metrics"]["oos_rank_ic"] is not None

    r = client.post(f"/api/lab/projects/{p['id']}/decision", json={"choice": "存为候选", "wait": True})
    assert r.status_code == 200, r.text
    mid = r.json()["project"]["model_id"]
    assert mid and client.get(f"/api/models/{mid}").json()["model"]["status"] == "candidate"

    # 直接输入 JSON 规格、/goal 解析
    pid = p["id"]
    r = client.post(
        f"/api/lab/projects/{pid}/code",
        json={"code": '{"model": "ridge", "label": "direction", "features": ["-returns(5)"]}', "wait": True},
    )
    assert r.status_code == 200, r.text
    assert '"direction"' in r.json()["project"]["code"]
    assert client.post(f"/api/lab/projects/{pid}/code", json={"code": '{"model": "zzz"}'}).status_code == 400
    parsed = client.post(
        "/api/lab/goal/parse", json={"text": "/model 样本外RankIC>=0.04 泄漏=1", "project_id": pid}
    ).json()
    assert parsed["domain"] == "model" and parsed["spec"]["items"][0]["metric"] == "oos_rank_ic"


def test_model_project_resolves_factor_library_features(client, tmp_path):
    f = client.post("/api/factors", json={"name": "反转", "expression": "-returns(5)", "evaluate": False}).json()
    f = f.get("factor", f)
    r = client.post(
        "/api/models/train", json={"model": "ridge", "features": [{"factor_id": f["id"]}], **TRAIN, "wait": True}
    )
    job = r.json()["job"]
    assert job["status"] == "done", job
    rep = client.get(f"/api/models/runs/{job['run_id']}").json()["report"]
    assert rep["spec"]["features"][0]["factor_id"] == f["id"]
