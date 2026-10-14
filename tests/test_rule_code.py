"""规则 ↔ 代码 统一：规则生成带 @rule / @param 注释的代码、AST 判断代码是否仍是规则、生成代码的真实回测。"""

from __future__ import annotations

import copy
import math
import random

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_ledger import mk

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.sdk import check_source, load_source, run_backtest
from quant_agent.sdk.runtimes import RuntimeCost
from quant_agent.strategies.dsl import EXAMPLES, TEMPLATE, DslError, canonical, load_dsl, validate
from quant_agent.strategies.rule_code import CODE_ONLY_REASON, code_to_rule, extract_rule_comments, rule_to_code

ZERO = dict(slippage_bps=0, spread_bps=0, min_notional=0, rebalance_tolerance=0, max_volume_participation=1.0)


def _same_rule(a: dict, b: dict) -> bool:
    return canonical(validate(a).spec) == canonical(validate(b).spec)


# ---------------------------------------------------------------- 生成
@pytest.mark.parametrize("key", list(EXAMPLES))
def test_examples_roundtrip_and_pass_sandbox(key):
    spec = EXAMPLES[key]
    code = rule_to_code(spec)
    assert code == rule_to_code(copy.deepcopy(spec)), "同一份规则必须生成完全相同的代码"
    assert check_source(code) == []
    load_source(code)  # 沙箱加载通过
    assert "# @rule" in code and "# @endrule" in code
    out = code_to_rule(code)
    assert out["is_rule"], out
    assert _same_rule(out["spec"], spec)


def test_code_carries_chinese_comments_and_params():
    code = rule_to_code(TEMPLATE)
    rule, params, err = extract_rule_comments(code)
    assert err == "" and rule["instrument"] == "BTC/USDT" and "params" not in rule
    assert params == {"fast": 12, "slow": 48}
    assert "# @param fast = 12  # 指标 ma_fast（SMA）的周期" in code
    assert "PARAMS = {" in code and '"fast": 12,' in code
    for text in ("# 入场条件：", "# 离场条件：", "# 全部满足 1：", "预热未完成", "离场优先"):
        assert text in code
    assert 'PARAMS["fast"]' in code  # $参数 引用落到 PARAMS 上，参数优化可直接作用


def test_invalid_rule_raises_problems():
    bad = copy.deepcopy(TEMPLATE)
    bad["params"] = {"fast": 60, "slow": 20}
    with pytest.raises(DslError):
        rule_to_code(bad)


# ---------------------------------------------------------------- AST 判断
def test_comments_whitespace_docstring_do_not_matter():
    code = rule_to_code(TEMPLATE)
    edited = code.replace('"""均线交叉', '"""我改过的说明 均线交叉', 1)
    edited = edited.replace("def handle_data(ctx):\n", "def handle_data(ctx):\n    # 我加的注释\n\n")
    edited = edited.replace(
        "    held = ctx.position(s).amount > 0", "    held  =  ctx.position(s).amount>0  # 是否持仓"
    )
    out = code_to_rule(edited)
    assert out["is_rule"], out


def test_params_dict_change_keeps_rule_and_updates_spec():
    code = rule_to_code(TEMPLATE).replace('    "fast": 12,', '    "fast": 9,')
    out = code_to_rule(code)
    assert out["is_rule"], out
    assert out["spec"]["params"] == {"fast": 9, "slow": 48}
    assert out["warnings"] and "PARAMS" in out["warnings"][0]


def test_params_that_break_rule_are_reported():
    code = rule_to_code(TEMPLATE).replace('    "fast": 12,', '    "fast": 60,')
    out = code_to_rule(code)
    assert not out["is_rule"] and "不再合法" in out["reason"]


def test_logic_edit_becomes_code_strategy():
    code = rule_to_code(TEMPLATE)
    edited = code.replace('ind["rsi14"][-1] < 70', 'ind["rsi14"][-1] < 65')
    assert edited != code
    out = code_to_rule(edited)
    assert out["is_rule"] is False and out["reason"] == CODE_ONLY_REASON
    assert "entry_signal" in out["diff"]
    # 多一行逻辑同样超出规则
    extra = code.replace("    ind = indicators(bars)\n", "    ind = indicators(bars)\n    record(x=1)\n")
    out = code_to_rule(extra)
    assert not out["is_rule"] and "handle_data" in out["diff"]
    # 新增一个函数
    out = code_to_rule(code + "\n\ndef on_bar(ctx, bar):\n    pass\n")
    assert not out["is_rule"] and "on_bar" in out["diff"]


def test_rule_block_edit_must_match_code():
    """只改 @rule 注释、不改代码：代码与规则不再一致，按代码策略处理。"""
    code = rule_to_code(TEMPLATE).replace('"right": {"const": 70}', '"right": {"const": 65}')
    out = code_to_rule(code)
    assert not out["is_rule"] and out["reason"] == CODE_ONLY_REASON


def test_missing_or_broken_rule_block():
    assert "没有 @rule" in code_to_rule("def initialize(ctx):\n    pass\n")["reason"]
    code = rule_to_code(TEMPLATE)
    broken = code.replace('#   "dsl_version": 2,', '#   "dsl_version": 2,,')
    assert "不是合法的 JSON" in code_to_rule(broken)["reason"]
    unterminated = code.replace("# @endrule\n", "")
    assert not code_to_rule(unterminated)["is_rule"]
    assert "语法错误" in code_to_rule("def x(:\n")["reason"]


# ---------------------------------------------------------------- 真实回测：生成代码与 DSL 解释器同口径
def _fills(res):
    return [(f["ts"], f["side"], f["amount"], f["price"]) for f in res.fills]


@pytest.mark.parametrize("key", ["ma_cross_rsi", "donchian_breakout", "rsi_reversion_fixed_qty"])
def test_generated_code_backtests_same_as_dsl(key):
    spec = copy.deepcopy(EXAMPLES[key])
    spec["timeframe"] = "1h"
    symbol = spec["instrument"]
    rnd = random.Random(7)
    noise = 3.0 if key == "ma_cross_rsi" else 0.0  # 均线交叉 + RSI 过滤需要更"毛糙"的行情才会触发
    closes = [
        100 + 12 * math.sin(i / 11) + 4 * math.sin(i / 3.7) + i * 0.03 + rnd.uniform(-noise, noise) for i in range(700)
    ]
    bars = mk([(c * 0.999, c * 1.006, c * 0.994, c) for c in closes], symbol=symbol)
    warm = validate(spec).warmup_bars + 2
    kw = dict(initial_cash=10_000, warmup_bars=warm, cost=RuntimeCost(**ZERO))
    by_dsl = run_backtest(load_dsl(spec), {symbol: bars}, **kw)
    by_code = run_backtest(load_source(rule_to_code(spec)), {symbol: bars}, **kw)
    assert not getattr(by_code, "errors", None)
    assert by_dsl.fills, "样例行情下应当有成交"
    assert _fills(by_code) == _fills(by_dsl)


def test_generated_rotation_backtests_same_as_dsl():
    spec = copy.deepcopy(EXAMPLES["momentum_rotation_weighted"])
    spec["timeframe"] = "1h"
    data = {}
    for k, sym in enumerate(spec["instruments"]):
        closes = [100 + 15 * math.sin(i / (9 + 4 * k) + k) + i * 0.02 * (k + 1) for i in range(500)]
        data[sym] = mk([(c, c * 1.003, c * 0.997, c) for c in closes], symbol=sym)
    warm = validate(spec).warmup_bars + 2
    kw = dict(initial_cash=10_000, warmup_bars=warm, cost=RuntimeCost(**ZERO))
    by_dsl = run_backtest(load_dsl(spec), data, **kw)
    by_code = run_backtest(load_source(rule_to_code(spec)), data, **kw)
    assert by_dsl.fills
    key = lambda f: (f["ts"], f["security"] if "security" in f else f.get("symbol"), f["side"], f["amount"])  # noqa: E731
    assert [key(f) for f in by_code.fills] == [key(f) for f in by_dsl.fills]


# ---------------------------------------------------------------- API
@pytest.fixture()
def client(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_api_to_code_and_from_code(client):
    r = client.post("/api/dsl/to-code", json={"spec": TEMPLATE})
    assert r.status_code == 200 and r.json()["ok"], r.text
    code = r.json()["code"]
    assert "# @rule" in code and r.json()["params"] == {"fast": 12, "slow": 48}

    bad = copy.deepcopy(TEMPLATE)
    bad["params"] = {"fast": 60, "slow": 20}
    r = client.post("/api/dsl/to-code", json={"spec": bad}).json()
    assert r["ok"] is False and r["problems"]

    back = client.post("/api/dsl/from-code", json={"code": code}).json()
    assert back["is_rule"] and back["summary"] and back["warmup_bars"] > 0
    edited = client.post("/api/dsl/from-code", json={"code": code + "\nX = 1\n"}).json()
    assert edited["is_rule"] is False and edited["reason"] == CODE_ONLY_REASON


def test_api_convert_dsl_strategy_to_code_and_backtest(client):
    r = client.post("/api/dsl/strategies", json={"name": "转代码测试", "spec": TEMPLATE})
    assert r.status_code == 200, r.text
    slug = r.json()["strategy"]["slug"]
    conv = client.post(f"/api/dsl/strategies/{slug}/to-code")
    assert conv.status_code == 200, conv.text
    assert conv.json()["strategy"]["kind"] == "code"
    assert conv.json()["version"]["kind"] != "dsl"
    got = client.get(f"/api/studio/strategies/{slug}").json()
    assert got["strategy"]["kind"] == "code" and got["issues"] == []
    assert code_to_rule(got["strategy"]["code"])["is_rule"]
    # 已经是代码策略：不能再转，也不能被 DSL 覆盖
    assert client.post(f"/api/dsl/strategies/{slug}/to-code").status_code == 409

    # 生成的代码在平台回测引擎上真实运行（worker 隔离进程执行 Python）
    bt = client.post(
        "/api/research/backtests", json={"slug": slug, "source": "synthetic", "bars_limit": 600, "warmup_bars": 60}
    )
    assert bt.status_code == 200, bt.text
    body = bt.json()["result"]
    assert body["ledger"]["balanced"]
    assert not body.get("errors"), body.get("errors")


def test_complex_rule_ops_backtest_same_as_dsl():
    """not / any / consecutive / lag / mul / $参数常量 / $参数仓位 / EMA / ATR 一并覆盖。"""
    spec = {
        "dsl_version": 2,
        "name": "复杂条件",
        "instrument": "BTC/USDT",
        "timeframe": "1h",
        "params": {"fast": 8, "level": 45, "w": 0.6},
        "indicators": {
            "e": {"type": "ema", "period": "$fast"},
            "r": {"type": "rsi", "period": 10},
            "a": {"type": "atr", "period": 14},
            "lo": {"type": "lowest", "period": 20},
        },
        "entry": {
            "op": "any",
            "conds": [
                {
                    "op": "all",
                    "conds": [
                        {"op": "gt", "left": {"price": "close"}, "right": {"ref": "e", "lag": 1, "mul": 1.002}},
                        {"op": "not", "cond": {"op": "gt", "left": {"ref": "r"}, "right": {"const": 70}}},
                    ],
                },
                {
                    "op": "consecutive",
                    "bars": 2,
                    "cond": {"op": "lt", "left": {"ref": "r"}, "right": {"const": "$level"}},
                },
            ],
        },
        "exit": {"op": "lt", "left": {"price": "close"}, "right": {"ref": "lo", "lag": 1, "mul": 1.01}},
        "position": {"mode": "weight", "value": "$w", "max_weight": 0.8},
        "risk": {"stop_loss": 0.04, "take_profit": 0.08},
    }
    code = rule_to_code(spec)
    assert code_to_rule(code)["is_rule"]
    rnd = random.Random(3)
    closes = [100 + 10 * math.sin(i / 13) + rnd.uniform(-2, 2) for i in range(600)]
    bars = mk([(c, c * 1.005, c * 0.995, c) for c in closes])
    kw = dict(initial_cash=10_000, warmup_bars=validate(spec).warmup_bars + 2, cost=RuntimeCost(**ZERO))
    by_dsl = run_backtest(load_dsl(spec), {"BTC/USDT": bars}, **kw)
    by_code = run_backtest(load_source(code), {"BTC/USDT": bars}, **kw)
    assert by_dsl.fills
    assert _fills(by_code) == _fills(by_dsl)
    # 参数优化覆盖 PARAMS 时同样一致
    by_dsl2 = run_backtest(load_dsl(spec, params={"fast": 12}), {"BTC/USDT": bars}, **kw)
    by_code2 = run_backtest(load_source(code, params={"fast": 12}), {"BTC/USDT": bars}, **kw)
    assert _fills(by_code2) == _fills(by_dsl2)


def test_generated_rotation_reenters_after_stop_loss_same_as_dsl():
    """止损打掉持仓、但目标权重没变：生成代码必须和 DSL 解释器一样重新建仓。

    下单三条规则（目标变化 / 目标为正但空仓 / 目标为零但持仓）少了第二条时，
    止损后目标仍是同一个值，代码版会一直空仓，与 DSL 的成交序列分叉。
    """
    spec = copy.deepcopy(EXAMPLES["momentum_rotation_weighted"])
    spec["timeframe"] = "1h"
    spec["rank"] = {"by": "ret", "top": 1, "order": "desc", "weighting": "equal"}
    spec["exit"] = None
    spec["risk"] = {"stop_loss": 0.03, "take_profit": 0, "max_trades_per_day": 0, "drawdown_pause": 0}
    data = {}
    for k, sym in enumerate(spec["instruments"]):
        # 第一个标的稳步上涨、其余下跌：名次始终不变。第一个标的每 40 根有一次下影线
        # 探到收盘价下方 10%（触发止损），收盘价不受影响，所以收益得分与目标权重都不变。
        rows = []
        for i in range(400):
            c = 100 * (1.001 if k == 0 else 0.999) ** i
            low = c * (0.9 if k == 0 and i % 40 == 39 else 0.999)
            rows.append((c, c * 1.001, low, c))
        data[sym] = mk(rows, symbol=sym)
    warm = validate(spec).warmup_bars + 2
    kw = dict(initial_cash=10_000, warmup_bars=warm, cost=RuntimeCost(**ZERO))
    by_dsl = run_backtest(load_dsl(spec), data, **kw)
    by_code = run_backtest(load_source(rule_to_code(spec)), data, **kw)
    assert not getattr(by_code, "errors", None)
    sym = lambda f: f["security"] if "security" in f else f.get("symbol")  # noqa: E731
    buys = [f for f in by_dsl.fills if f["side"] == "buy" and sym(f) == spec["instruments"][0]]
    assert len(buys) >= 2, "样例行情应当出现止损后再次建仓"
    key = lambda f: (f["ts"], sym(f), f["side"], f["amount"])  # noqa: E731
    assert [key(f) for f in by_code.fills] == [key(f) for f in by_dsl.fills]
