"""研究中心：模板、新建/克隆、回测记录、对比、参数优化；实时模拟执行器。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.api.routes_research import rewrite_params
from quant_agent.engine.live import LiveRunner, clock_synthetic_bars
from quant_agent.engine.research import spearman, split_bars
from quant_agent.sdk import RuntimeCost, load_source
from quant_agent.strategies.sdk_templates import TEMPLATES

H = 3_600_000
SPEC = {"source": "synthetic", "bars_limit": 600, "warmup_bars": 60}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("research-store")
    set_services(AppServices.build(store_root=str(root)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _new(client, template_id="ma_cross", name="测试均线"):
    r = client.post("/api/research/strategies", json={"name": name, "template_id": template_id})
    assert r.status_code == 200, r.text
    return r.json()["strategy"]


def test_templates_listed_and_all_valid(client):
    rows = client.get("/api/research/templates").json()["templates"]
    assert {t["id"] for t in rows} == {t.id for t in TEMPLATES}
    detail = client.get("/api/research/templates/momentum_rotation").json()
    assert detail["params"]["lookback"] == 72
    assert len(detail["template"]["securities"]) >= 3


def test_new_strategy_never_overwrites_same_name(client):
    a = _new(client, name="同名策略")
    b = _new(client, name="同名策略")
    assert a["slug"] != b["slug"]
    assert a["params"]["fast"] == 12


def test_clone_and_apply_params(client):
    src = _new(client, name="待克隆")
    clone = client.post(f"/api/research/strategies/{src['slug']}/clone", json={}).json()["strategy"]
    assert clone["name"].endswith("（副本）") and clone["slug"] != src["slug"]
    r = client.post(f"/api/research/strategies/{clone['slug']}/apply-params", json={"params": {"fast": 8}})
    assert r.status_code == 200, r.text
    body = r.json()["strategy"]
    assert body["params"]["fast"] == 8
    assert "# 快线周期" in body["code"], "改参数不应抹掉注释"
    assert body["version"] != clone["version"]


def test_rewrite_params_keeps_everything_else():
    src = 'PARAMS = {\n    "a": 1,   # 注释\n    "b": 0.5,\n}\n\ndef initialize(ctx):\n    pass\n'
    out = rewrite_params(src, {"a": 3, "b": 0.25})
    assert '"a": 3,   # 注释' in out and '"b": 0.25,' in out and "def initialize" in out


def test_quick_run_does_not_persist(client):
    s = _new(client, name="编译运行")
    r = client.post("/api/research/backtests", json={"slug": s["slug"], "persist": False, **SPEC})
    assert r.status_code == 200, r.text
    assert r.json()["id"] is None
    assert client.get(f"/api/research/backtests?slug={s['slug']}").json()["total"] == 0


def test_full_backtest_persists_detail_and_manifest(client):
    s = _new(client, name="完整回测")
    r = client.post("/api/research/backtests", json={"slug": s["slug"], **SPEC})
    assert r.status_code == 200, r.text
    bt_id = r.json()["id"]
    detail = client.get(f"/api/research/backtests/{bt_id}").json()
    for key in ("equity_curve", "benchmark_curve", "daily", "fills", "logs", "metrics", "manifest", "code"):
        assert key in detail
    assert detail["manifest"]["code_sha256"] and detail["manifest"]["data"][0]["digest"]
    assert detail["metrics"]["alpha"] is not None or detail["metrics"]["beta"] is not None
    assert any("合成数据" in w for w in detail["warnings"])
    rows = client.get(f"/api/research/backtests?slug={s['slug']}").json()["backtests"]
    assert rows[0]["id"] == bt_id and "sharpe" in rows[0]["metrics"]


def test_backtest_with_param_override_and_date_range(client):
    s = _new(client, name="区间回测")
    import time

    end = int(time.time() * 1000) // H * H
    r = client.post(
        "/api/research/backtests",
        json={
            "slug": s["slug"],
            "source": "synthetic",
            "start": end - 300 * H,
            "end": end,
            "warmup_bars": 50,
            "params": {"fast": 6},
            "persist": False,
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["params"]["fast"] == 6
    # 计分从起始日开始（第一点是预热结束时刻）
    assert abs(result["timestamps"][0] - (end - 300 * H)) <= H


def test_compare_and_delete(client):
    s = _new(client, name="对比")
    a = client.post("/api/research/backtests", json={"slug": s["slug"], **SPEC}).json()["id"]
    b = client.post("/api/research/backtests", json={"slug": s["slug"], **SPEC, "params": {"fast": 20}}).json()["id"]
    items = client.post("/api/research/backtests/compare", json={"ids": [a, b]}).json()["items"]
    assert len(items) == 2 and items[0]["returns"][0] == 0
    assert client.delete(f"/api/research/backtests/{a}").json()["ok"]
    assert client.get(f"/api/research/backtests/{a}").status_code == 404


def test_bad_code_is_rejected_before_running(client):
    r = client.post("/api/research/backtests", json={"code": "import os\ndef initialize(ctx): pass", **SPEC})
    assert r.status_code == 400 and "os" in r.json()["message"]


def test_optimize_job_reports_in_and_out_of_sample(client):
    s = _new(client, name="优化")
    r = client.post(
        "/api/research/optimize",
        json={
            "slug": s["slug"],
            **SPEC,
            "bars_limit": 800,
            "grid": {"fast": [6, 12], "slow": [30, 48]},
            "objective": "sharpe",
        },
    )
    assert r.status_code == 200, r.text
    job = client.get(f"/api/research/jobs/{r.json()['job_id']}").json()
    assert job["status"] == "done", job
    opt = client.get(f"/api/research/optimizations/{job['result']['id']}").json()
    assert len(opt["rows"]) == 4
    assert all("in_sample" in row and "out_sample" in row for row in opt["rows"])
    assert opt["diagnostics"]["warnings"]
    assert opt["best"]["in_rank"] == 1


def test_optimize_validates_grid(client):
    s = _new(client, name="优化校验")
    bad = client.post("/api/research/optimize", json={"slug": s["slug"], **SPEC, "grid": {"nope": [1]}})
    assert bad.status_code == 400
    grid = {"fast": list(range(2, 30)), "slow": list(range(30, 40))}
    big = client.post("/api/research/optimize", json={"slug": s["slug"], **SPEC, "grid": grid})
    assert big.status_code == 400 and "上限" in big.json()["message"]


def test_split_and_spearman_helpers():
    from quant_agent.data.service import synthetic_bars

    bars = {"BTC/USDT": synthetic_bars("BTC/USDT", "1h", 500)}
    ins, oos, cut = split_bars(bars, 0.7, 50)
    assert ins["BTC/USDT"][-1].close_time == cut
    assert oos["BTC/USDT"][0].close_time < cut, "样本外要带预热"
    assert spearman([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1)


# ---------------- 实时模拟执行器 ----------------
LIVE_SRC = """
def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    if ctx.position(s).amount == 0:
        order_target_percent(s, 0.5)
"""


def test_live_runner_trades_on_new_bars_and_fills_at_next_open():
    t0 = 1_700_000_000_000 // H * H
    history = {"BTC/USDT": clock_synthetic_bars("BTC/USDT", "1h", t0 - 50 * H, t0)}
    runner = LiveRunner()
    runner.start(
        "s1",
        load_source(LIVE_SRC, name="live"),
        history,
        timeframe="1h",
        initial_cash=10_000,
        cost=RuntimeCost(),
        started_at=t0,
    )
    # 第一根新 bar：策略下单，排队
    nb1 = clock_synthetic_bars("BTC/USDT", "1h", t0, t0 + H)
    out1 = runner.step("s1", {"BTC/USDT": nb1})
    assert out1["triggered"] and out1["pending"] == 1 and not out1["fills"]
    # 第二根新 bar：按它的开盘价成交
    nb2 = clock_synthetic_bars("BTC/USDT", "1h", t0 + H, t0 + 2 * H)
    out2 = runner.step("s1", {"BTC/USDT": nb2})
    assert len(out2["fills"]) == 1
    assert out2["fills"][0]["ts"] == nb2[0].open_time
    assert out2["portfolio"]["position_ratio"] == pytest.approx(0.5, abs=0.05)
    # 同一根 bar 再喂一次：不会重复触发
    out3 = runner.step("s1", {"BTC/USDT": nb2})
    assert out3["new_bars"] == 0 and not out3["triggered"]


def test_live_runner_skips_stale_bars_after_gap():
    t0 = 1_700_000_000_000 // H * H
    history = {"BTC/USDT": clock_synthetic_bars("BTC/USDT", "1h", t0 - 50 * H, t0)}
    runner = LiveRunner()
    runner.start(
        "s2",
        load_source(LIVE_SRC, name="live2"),
        history,
        timeframe="1h",
        initial_cash=10_000,
        cost=RuntimeCost(),
        started_at=t0,
    )
    burst = clock_synthetic_bars("BTC/USDT", "1h", t0, t0 + 5 * H)
    out = runner.step("s2", {"BTC/USDT": burst})
    assert out["new_bars"] == 5 and out["skipped_bars"] == 4
    assert out["triggered"]


def test_clock_synthetic_is_window_independent():
    t0 = 1_700_000_000_000 // H * H
    a = clock_synthetic_bars("ETH/USDT", "1h", t0, t0 + 10 * H)
    b = clock_synthetic_bars("ETH/USDT", "1h", t0 - 20 * H, t0 + 10 * H)
    assert [x.close for x in a] == [x.close for x in b[-10:]]
