"""规则 DSL v2：逐项校验、解释执行的正确性、与回测 / 优化 / 实时模拟 / 版本的端到端集成。"""

from __future__ import annotations

import copy
import math
from decimal import Decimal

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_ledger import T0, H, mk

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.sdk import run_backtest
from quant_agent.sdk.runtimes import RuntimeCost
from quant_agent.strategies.dsl import EXAMPLES, TEMPLATE, DslError, evaluate, load_dsl, validate

ZERO = dict(slippage_bps=0, spread_bps=0, min_notional=0, rebalance_tolerance=0, max_volume_participation=1.0)


def spec(**patch):
    s = copy.deepcopy(TEMPLATE)
    s.update(patch)
    return s


def problems(s, **kw):
    with pytest.raises(DslError) as ei:
        validate(s, **kw)
    return {p["path"]: p["message"] for p in ei.value.problems}


# ---------------------------------------------------------------- 校验
def test_template_and_examples_validate():
    c = validate(TEMPLATE)
    assert c.warmup_bars >= 48 and c.summary and len(c.code_hash) == 64
    for ex in EXAMPLES.values():
        validate(ex)


def test_field_level_problems():
    s = spec()
    s["indicators"]["ma_fast"]["period"] = "$nope"
    s["indicators"]["bad name"] = {"type": "sma", "period": 5}
    s["entry"] = {"op": "lt", "left": {"ref": "rsi14"}, "right": {"const": 150}}
    s["position"] = {"mode": "weight", "value": 1.2, "max_weight": 1.0}
    s["extra"] = 1
    p = problems(s)
    assert "indicators.ma_fast.period" in p and "未声明" in p["indicators.ma_fast.period"]
    assert "indicators.bad name" in p
    assert "entry" in p and "0–100" in p["entry"]
    assert "position.value" in p
    assert "extra" in p


def test_fast_must_be_shorter_than_slow():
    s = spec(params={"fast": 50, "slow": 20})
    p = problems(s)
    assert any("快线" in m for m in p.values())
    same = spec(params={"fast": 20, "slow": 20})
    assert any("完全相同" in m for m in problems(same).values())


def test_entry_equal_exit_and_missing_exit_rejected():
    s = spec()
    s["exit"] = copy.deepcopy(s["entry"])
    assert "exit" in problems(s)
    s2 = spec(exit=None, risk={"stop_loss": 0, "take_profit": 0})
    assert "离场" in problems(s2)["exit"]


def test_quantity_precision_and_period_range():
    s = spec(position={"mode": "quantity", "value": 0.00015, "qty_step": 0.0001})
    assert "整数倍" in problems(s)["position.value"]
    s2 = spec(params={"fast": 0, "slow": 48})
    assert "indicators.ma_fast.period" in problems(s2)


def test_no_code_injection_possible():
    s = spec()
    s["entry"] = {"op": "gt", "left": {"ref": "__import__('os').system('id')"}, "right": {"const": 1}}
    assert "entry.left.ref" in problems(s)
    s["entry"] = {"op": "__class__", "left": {"const": 1}, "right": {"const": 2}}
    assert "entry.op" in problems(s)
    import ast
    import inspect

    import quant_agent.strategies.dsl as d

    tree = ast.parse(inspect.getsource(d))
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not called & {"eval", "exec", "compile", "__import__"}


def test_param_override_must_be_declared():
    assert "params.zzz" in problems(TEMPLATE, params={"zzz": 1})


# ---------------------------------------------------------------- 解释执行
def _rows(closes):
    return [{"open": c, "high": c, "low": c, "close": c} for c in closes]


def test_cross_consecutive_lag_semantics():
    r = validate(
        {
            "dsl_version": 2,
            "instrument": "BTC/USDT",
            "timeframe": "1h",
            "params": {},
            "indicators": {"f": {"type": "sma", "period": 2}, "s": {"type": "sma", "period": 4}},
            "entry": {"op": "cross_above", "left": {"ref": "f"}, "right": {"ref": "s"}},
            "exit": {
                "op": "consecutive",
                "bars": 2,
                "cond": {"op": "lt", "left": {"price": "close"}, "right": {"price": "close", "lag": 1}},
            },
            "position": {"mode": "weight", "value": 1.0},
        }
    ).resolved
    # f 上穿 s 恰好发生在最后一根
    assert evaluate(r, _rows([10, 9, 8, 7, 7, 9]))["entry"] is True
    assert evaluate(r, _rows([10, 9, 8, 7, 7, 9, 11]))["entry"] is False  # 已经在上方，不是“穿越”
    assert evaluate(r, _rows([5, 6, 7, 6, 5]))["exit"] is True  # 连续 2 根收盘下跌
    assert evaluate(r, _rows([5, 6, 7, 6, 7]))["exit"] is False


def test_indicators_match_reference_values():
    from quant_agent.strategies.dsl import _atr, _ema, _rsi, _sma

    xs = [float(x) for x in range(1, 21)]
    assert _sma(xs, 5)[-1] == pytest.approx(18.0)
    assert _ema(xs, 3)[-1] == pytest.approx(19.0)  # 线性序列的 EMA 滞后 (p-1)/2
    assert _rsi(xs, 14)[-1] == 100.0  # 只涨不跌
    flat = [10.0] * 20
    assert _atr([11.0] * 20, [9.0] * 20, flat, 14)[-1] == pytest.approx(2.0)


def test_dsl_backtest_fixed_quantity_and_exit_priority():
    s = {
        "dsl_version": 2,
        "instrument": "BTC/USDT",
        "timeframe": "1h",
        "params": {},
        "indicators": {"r": {"type": "return", "period": 1}},
        "entry": {"op": "gt", "left": {"ref": "r"}, "right": {"const": 0}},
        "exit": {"op": "lt", "left": {"ref": "r"}, "right": {"const": 0}},
        "position": {"mode": "quantity", "value": 2, "qty_step": 1, "max_weight": 1.0},
    }
    closes = [100, 100, 101, 101, 100, 100]
    bars = mk([(c, c, c, c) for c in closes])
    res = run_backtest(load_dsl(s), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO))
    sides = [(f["side"], f["amount"]) for f in res.fills]
    assert sides == [("buy", "2"), ("sell", "2")]
    assert res.fills[0]["ts"] == T0 + 3 * H  # 第 3 根收盘决策 → 第 4 根开盘成交
    assert res.ledger["balanced"]


def test_dsl_insufficient_funds_rejected():
    s = {
        "dsl_version": 2,
        "instrument": "BTC/USDT",
        "timeframe": "1h",
        "params": {},
        "indicators": {"r": {"type": "return", "period": 1}},
        "entry": {"op": "gt", "left": {"ref": "r"}, "right": {"const": 0}},
        "exit": {"op": "lt", "left": {"ref": "r"}, "right": {"const": 0}},
        "position": {"mode": "quantity", "value": 50, "qty_step": 1, "max_weight": 1.0},
    }
    bars = mk([(c, c, c, c) for c in [100, 100, 101, 101, 101]])
    res = run_backtest(load_dsl(s), {"BTC/USDT": bars}, initial_cash=1000, cost=RuntimeCost(**ZERO))
    assert res.fills == [] and res.rejected[0]["code"] == "INSUFFICIENT_FUNDS"


def test_dsl_max_weight_and_stop_loss_applied():
    s = copy.deepcopy(EXAMPLES["donchian_breakout"])
    s["timeframe"] = "1h"
    closes = [100 + 10 * math.sin(i / 9) + i * 0.05 for i in range(400)]
    bars = mk([(c, c * 1.004, c * 0.996, c) for c in closes])
    res = run_backtest(load_dsl(s), {"BTC/USDT": bars}, initial_cash=10_000, warmup_bars=30, cost=RuntimeCost(**ZERO))
    assert res.fills, "通道突破应当有成交"
    eq0 = 10_000
    first_buy = next(f for f in res.fills if f["side"] == "buy")
    assert float(first_buy["notional"]) <= eq0 * 0.5 + 1e-6  # max_weight 0.5
    assert res.risk["drawdown_pause"] == 0.25 and res.risk["max_trades_per_day"] == 2


# ---------------------------------------------------------------- API 端到端
@pytest.fixture()
def client(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_api_validate_returns_problems(client):
    bad = spec(params={"fast": 60, "slow": 20})
    r = client.post("/api/dsl/validate", json={"spec": bad})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert r.json()["problems"][0]["path"].startswith("entry")
    ok = client.post("/api/dsl/validate", json={"spec": TEMPLATE}).json()
    assert ok["ok"] and ok["warmup_bars"] > 0 and ok["summary"]
    schema = client.get("/api/dsl/schema").json()
    assert schema["schema"]["properties"]["position"] and "sma" in {i["type"] for i in schema["indicators"]}


def test_api_dsl_strategy_lifecycle(client):
    r = client.post("/api/dsl/strategies", json={"name": "DSL 均线", "spec": TEMPLATE})
    assert r.status_code == 200, r.text
    slug = r.json()["strategy"]["slug"]
    v1 = r.json()["version"]
    assert v1["status"] == "VALIDATED" and v1["version_no"] == 1 and v1["kind"] == "dsl"
    # 保存相同内容不产生新版本；修改后产生 v2，v1 内容不变
    again = client.post("/api/dsl/strategies", json={"name": "DSL 均线", "spec": TEMPLATE, "slug": slug}).json()
    assert again["version"]["id"] == v1["id"]
    changed = spec(params={"fast": 10, "slow": 48})
    v2 = client.post("/api/dsl/strategies", json={"name": "DSL 均线", "spec": changed, "slug": slug}).json()["version"]
    assert v2["version_no"] == 2
    old = client.get(f"/api/strategy-versions/{v1['id']}").json()["version"]
    assert '"$fast"' in old["code"] and old["params"] == {"fast": 12, "slow": 48}

    # 回测（DSL 在 worker 进程内解释执行）
    bt = client.post(
        "/api/research/backtests", json={"slug": slug, "source": "synthetic", "bars_limit": 600, "warmup_bars": 10}
    )
    assert bt.status_code == 200, bt.text
    body = bt.json()["result"]
    assert body["ledger"]["balanced"] and body["manifest"]["strategy_version_id"] == v2["id"]
    assert bt.json()["result"]["spec"]["warmup_bars"] >= validate(changed).warmup_bars  # 预热自动补足

    # 参数优化直接作用于 DSL params
    opt = client.post(
        "/api/research/optimize",
        json={"slug": slug, "source": "synthetic", "bars_limit": 800, "warmup_bars": 10, "grid": {"fast": [6, 10]}},
    ).json()
    job = client.get(f"/api/research/jobs/{opt['job_id']}").json()
    assert job["status"] == "done", job
    detail = client.get(f"/api/research/optimizations/{job['result']['id']}").json()
    assert len(detail["rows"]) == 2

    # 批准 = 冻结；冻结版本才能进实时模拟
    ap = client.post(f"/api/studio/strategies/{slug}/approve").json()
    assert ap["version"]["status"] == "FROZEN" and ap["version"]["id"] == v2["id"]
    pr = client.post("/api/studio/paper/start", json={"slug": slug, "source": "demo"})
    assert pr.status_code == 200, pr.text
    sid = pr.json()["session"]["id"]
    assert client.get(f"/api/runs/{sid}").json()["run"]["strategy_version_id"] == v2["id"]
    t = client.post(f"/api/studio/paper/{sid}/tick").json()
    assert t["session"]["status"] == "running"
    # 参数覆盖 = 新版本，未冻结不能启动
    r = client.post("/api/studio/paper/start", json={"slug": slug, "source": "demo", "params": {"fast": 8}})
    assert r.status_code == 403 and r.json()["code"] == "PERMISSION_DENIED"
    # 归档后不能再运行
    client.post(f"/api/strategy-versions/{v1['id']}/archive")
    assert client.get(f"/api/strategy-versions/{v1['id']}").json()["version"]["status"] == "ARCHIVED"
    versions = client.get(f"/api/strategies/{slug}/versions").json()["versions"]
    assert {v["status"] for v in versions} == {"ARCHIVED", "FROZEN", "VALIDATED"}  # v3 = 参数覆盖产生的新版本


def test_api_rejects_invalid_dsl_save(client):
    r = client.post("/api/dsl/strategies", json={"name": "坏", "spec": spec(position={"mode": "x", "value": 1})})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_PARAMETER"
    assert any(p["path"] == "position.mode" for p in r.json()["details"]["problems"])


def test_dsl_runs_even_when_python_disabled(client):
    client.put("/api/workspace/settings", json={"quotas": {"allow_python_strategies": False}})
    slug = client.post("/api/dsl/strategies", json={"name": "只用 DSL", "spec": TEMPLATE}).json()["strategy"]["slug"]
    r = client.post("/api/research/backtests", json={"slug": slug, "source": "synthetic", "bars_limit": 300})
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["result"]["ledger"]["final_equity"]) > 0
