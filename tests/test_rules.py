"""规则策略引擎测试。

重点覆盖：白名单拒绝任意代码、比较必须用原始数值（不是布尔）、
版本摘要随参数/规则变化、以及规则策略与模板策略走同一套账。
"""

from __future__ import annotations

import pytest

from quant_agent.data.service import synthetic_bars
from quant_agent.engine import BacktestEngine, CostModel
from quant_agent.strategies.rule_strategy import RuleStrategy
from quant_agent.strategies.rules import (
    RULE_TEMPLATE,
    RuleError,
    evaluate,
    indicator_context,
    parse_rule,
    rsi,
    rule_target,
    validate_rule_spec,
)
from quant_agent.strategies.templates import build_strategy

CTX = {"close": 100.0, "sma_fast": 1.0, "sma_slow": 2.0, "rsi": 50.0, "position": 0.0}


# ---------------- 白名单：拒绝任意代码 ----------------
@pytest.mark.parametrize(
    "rule",
    [
        "__import__('os').system('whoami')",
        "close.__class__",
        "[x for x in close]",
        "close ** 100",
        "close[0]",
        "100",
        "close > float(1)",
        "close > 'a'",
        "close > True",
        "open > close",
        "close > 1; import os",
        "lambda: close",
        "(close > 1) if True else False",
    ],
)
def test_malicious_or_invalid_rules_are_rejected(rule):
    with pytest.raises(RuleError):
        parse_rule(rule)


def test_rule_length_and_complexity_limits():
    with pytest.raises(RuleError, match="最多"):
        parse_rule("close > 1 and " * 40 + "close > 1")
    with pytest.raises(RuleError, match="不能为空"):
        parse_rule("   ")


# ---------------- 求值：必须使用原始数值 ----------------
def test_comparison_uses_numeric_values_not_booleans():
    """回归测试：曾经把指标包成 bool，导致 `1 > 2` 变成 `True > True` → False。"""
    node = parse_rule("sma_fast > sma_slow")
    assert evaluate(node, {"sma_fast": 3.0, "sma_slow": 2.0}) is True
    assert evaluate(node, {"sma_fast": 1.0, "sma_slow": 2.0}) is False


def test_rule_with_default_template_actually_fires_on_data():
    """端到端回归：默认模板必须能在真实形状的数据上产生成交。"""
    spec = validate_rule_spec(RULE_TEMPLATE)
    bars = synthetic_bars("BTC/USDT", "1h", 1500)
    engine = BacktestEngine(initial_cash=10_000, cost_model=CostModel())
    result = engine.run(RuleStrategy(spec), bars, run_id="r", symbol="BTC/USDT", timeframe="1h")
    assert result.metrics.trades > 0, "规则策略一次都没成交，说明信号链路有问题"


def test_boolean_logic_and_not():
    assert evaluate(parse_rule("close > 100 and not rsi > 75"), {"close": 110.0, "rsi": 50.0}) is True
    assert evaluate(parse_rule("close > 100 and not rsi > 75"), {"close": 110.0, "rsi": 80.0}) is False
    assert evaluate(parse_rule("close < 100 or rsi < 30"), {"close": 110.0, "rsi": 20.0}) is True
    combined = parse_rule("(close > 100 or rsi < 30) and position < 0.5")
    assert evaluate(combined, {"close": 110.0, "rsi": 20.0, "position": 0.2}) is True
    assert evaluate(combined, {"close": 110.0, "rsi": 20.0, "position": 0.9}) is False


def test_chained_comparison():
    assert evaluate(parse_rule("30 < rsi < 70"), {"rsi": 50.0}) is True
    assert evaluate(parse_rule("30 < rsi < 70"), {"rsi": 80.0}) is False


def test_fullwidth_symbols_are_normalized():
    spec = validate_rule_spec({**RULE_TEMPLATE, "buy": "close ＞ 100", "sell": "close ＜ 50"})
    assert ">" in spec.buy and "<" in spec.sell


# ---------------- 版本摘要 ----------------
def test_version_changes_with_params_and_rules():
    base = validate_rule_spec(RULE_TEMPLATE)
    assert validate_rule_spec({**RULE_TEMPLATE, "fast": 13}).version != base.version
    assert validate_rule_spec({**RULE_TEMPLATE, "buy": "close > 1"}).version != base.version
    assert validate_rule_spec({**RULE_TEMPLATE, "team": "另一组"}).version != base.version
    # 完全相同则稳定
    assert validate_rule_spec(dict(RULE_TEMPLATE)).version == base.version
    assert base.id.startswith(base.version)


def test_fast_must_be_less_than_slow():
    with pytest.raises(RuleError, match="必须小于"):
        validate_rule_spec({**RULE_TEMPLATE, "fast": 48, "slow": 12})


def test_field_validation():
    with pytest.raises(RuleError, match="策略名称"):
        validate_rule_spec({**RULE_TEMPLATE, "name": ""})
    with pytest.raises(RuleError, match="目标仓位"):
        validate_rule_spec({**RULE_TEMPLATE, "weight": 1.5})
    with pytest.raises(RuleError, match="团队名称"):
        validate_rule_spec({**RULE_TEMPLATE, "team": "x" * 50})


# ---------------- RSI 与指标上下文 ----------------
def test_rsi_bounds():
    rising = [float(i) for i in range(1, 40)]
    falling = [float(i) for i in range(40, 1, -1)]
    assert rsi(rising, 14) == 100.0
    assert rsi(falling, 14) == 0.0
    assert rsi([5.0] * 30, 14) == 50.0


def test_indicator_context_requires_warmup():
    assert indicator_context([1.0] * 10, fast=12, slow=48, period=14, position=0.0) is None
    ctx = indicator_context([float(i) for i in range(1, 60)], fast=12, slow=48, period=14, position=0.3)
    assert ctx is not None
    assert set(ctx) == {"close", "sma_fast", "sma_slow", "rsi", "position"}


def test_sell_wins_when_both_conditions_true():
    """买卖同时满足时应当离场（保守假设）。"""
    spec = validate_rule_spec({**RULE_TEMPLATE, "buy": "close > 1", "sell": "close > 1"})
    closes = [100.0] * 60
    assert rule_target(spec, closes, 0.0) == 0.0


def test_no_signal_returns_none_meaning_hold():
    spec = validate_rule_spec({**RULE_TEMPLATE, "buy": "close > 999999", "sell": "close < 0.001"})
    assert rule_target(spec, [100.0] * 60, 0.0) is None


# ---------------- 与模板策略同账 ----------------
def test_rule_and_template_share_the_same_accounting():
    bars = synthetic_bars("BTC/USDT", "1h", 800)
    engine = BacktestEngine(initial_cash=10_000, cost_model=CostModel())
    rule = engine.run(RuleStrategy(validate_rule_spec(RULE_TEMPLATE)), bars, run_id="a")
    ma = engine.run(build_strategy("ma_trend"), bars, run_id="b")
    for result in (rule, ma):
        assert result.metrics.initial_equity == 10_000
        assert len(result.equity_curve) == len(result.timestamps)
        assert result.metrics.final_equity > 0
    # 同一份数据、同一套成本，两个策略的观测 bar 数必须一致
    assert rule.metrics.observations == ma.metrics.observations
