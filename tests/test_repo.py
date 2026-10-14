"""策略文件仓库与"规则 → Python 编译"测试。"""

from __future__ import annotations

import json

import pytest

from quant_agent.data.service import synthetic_bars
from quant_agent.engine import BacktestEngine, CostModel
from quant_agent.sdk import RuntimeCost, check_source, load_source, run_backtest
from quant_agent.strategies.repo import StrategyRepository, rule_to_python, slugify
from quant_agent.strategies.rule_strategy import RuleStrategy
from quant_agent.strategies.rules import RULE_TEMPLATE, validate_rule_spec


@pytest.fixture
def repo(tmp_path):
    return StrategyRepository(tmp_path)


@pytest.fixture(scope="module")
def bars():
    return synthetic_bars("BTC/USDT", "1h", 900)


# ---------------- slug 与基本读写 ----------------
def test_slugify_handles_chinese_and_symbols():
    assert slugify("我的趋势策略").startswith("strategy-")
    assert slugify("My Trend v2!").startswith("my-trend-v2")
    # 同名必得同 slug（幂等）
    assert slugify("我的趋势策略") == slugify("我的趋势策略")


def test_save_read_list_delete(repo):
    rec = repo.save(
        name="我的趋势策略",
        source="def initialize(ctx):\n    run_daily(step)\n\ndef step(ctx):\n    pass\n",
        params={"fast": 12, "slow": 48},
        team="第四研究组",
        symbol="BTC/USDT",
    )
    assert rec.meta.version
    assert rec.path.endswith("strategy.py")
    assert rec.params["fast"] == 12

    got = repo.get(rec.meta.slug)
    assert got is not None and got.meta.name == "我的趋势策略"
    assert [r.meta.name for r in repo.list()] == ["我的趋势策略"]
    assert repo.delete(rec.meta.slug) is True
    assert repo.get(rec.meta.slug) is None


def test_strategy_is_a_real_directory_with_three_files(repo):
    rec = repo.save(name="目录结构", source="def initialize(ctx): pass\n")
    d = repo.dir_of(rec.meta.slug)
    assert (d / "strategy.py").exists()
    assert (d / "meta.json").exists()
    assert (d / "params.json").exists()
    assert (d / "versions").is_dir()
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "目录结构"


def test_saving_changes_snapshots_previous_version(repo):
    src_v1 = "def initialize(ctx):\n    g.x = 1\n"
    src_v2 = "def initialize(ctx):\n    g.x = 2\n"
    rec = repo.save(name="版本", source=src_v1)
    v1 = rec.meta.version
    rec2 = repo.save(name="版本", source=src_v2, slug=rec.meta.slug)
    assert rec2.meta.version != v1
    versions = repo.versions(rec.meta.slug)
    assert len(versions) == 1
    assert versions[0]["version"] == v1
    # 历史版本内容可读，且是旧的那一份
    old = repo.read_version(rec.meta.slug, versions[0]["file"])
    assert old == src_v1


def test_saving_identical_content_does_not_snapshot(repo):
    src = "def initialize(ctx): pass\n"
    rec = repo.save(name="重复", source=src)
    repo.save(name="重复", source=src, slug=rec.meta.slug)
    assert repo.versions(rec.meta.slug) == []


def test_read_version_rejects_path_traversal(repo):
    rec = repo.save(name="安全", source="def initialize(ctx): pass\n")
    assert repo.read_version(rec.meta.slug, "../../meta.json") is None


def test_update_meta_and_export(repo):
    rec = repo.save(name="导出", source="def initialize(ctx): pass\n", team="某组")
    updated = repo.update_meta(rec.meta.slug, status="tested", human_approved=True)
    assert updated is not None
    assert updated.meta.status == "tested" and updated.meta.human_approved is True

    exported = repo.export(rec.meta.slug)
    assert exported is not None
    assert exported.startswith('"""')
    assert "团队：某组" in exported
    assert "def initialize" in exported


def test_list_filters_by_kind(repo):
    repo.save(name="代码策略", source="def initialize(ctx): pass\n", kind="code")
    repo.save(name="规则策略", source="def initialize(ctx): pass\n", kind="rule")
    assert len(repo.list(kind="rule")) == 1
    assert len(repo.list(kind="code")) == 1


# ---------------- 规则 → Python ----------------
def test_rule_compiles_to_runnable_python(bars):
    spec = validate_rule_spec(RULE_TEMPLATE)
    source = rule_to_python(spec)
    assert check_source(source) == []
    assert "def initialize" in source and "def handle_bar" in source
    assert spec.buy in source and spec.sell in source

    result = run_backtest(
        load_source(source, name="compiled"),
        {"BTC/USDT": bars},
        initial_cash=10_000,
        warmup_bars=100,
    )
    assert not result.errors, result.errors
    assert result.fills, "编译出的策略必须能真正成交"


def test_compiled_rule_matches_interpreter(bars):
    """核心保证：规则编译出的 Python 与平台解释器跑出同一个结果。

    两个执行路径如果对不上，用户就没法相信"看源码"能理解实际行为。
    """
    spec = validate_rule_spec(RULE_TEMPLATE)
    source = rule_to_python(spec)

    sdk = run_backtest(
        load_source(source, name="compiled"),
        {"BTC/USDT": bars},
        initial_cash=10_000,
        warmup_bars=100,
        # 关掉调仓容差，让两边的逐笔成交可以严格对齐
        cost=RuntimeCost(taker_fee=0.001, slippage_bps=5, spread_bps=2, rebalance_tolerance=0.0),
    )
    engine = BacktestEngine(initial_cash=10_000, cost_model=CostModel())
    interp = engine.run(RuleStrategy(spec), bars, run_id="i", warmup_bars=100)

    assert abs(sdk.metrics["total_return"] - interp.metrics.total_return) < 0.002
    assert abs(sdk.metrics["max_drawdown"] - interp.metrics.max_drawdown) < 0.002
    # 一次完整交易 = 两次成交
    assert len(sdk.fills) == interp.metrics.trades * 2


def test_compiled_rule_only_trades_on_signal_change(bars):
    """无信号时保持持仓，不做无意义调仓 —— 成交次数应当与交易次数严格对应。"""
    spec = validate_rule_spec(RULE_TEMPLATE)
    sdk = run_backtest(
        load_source(rule_to_python(spec), name="compiled"),
        {"BTC/USDT": bars},
        initial_cash=10_000,
        warmup_bars=100,
        cost=RuntimeCost(rebalance_tolerance=0.0),
    )
    buys = sum(1 for f in sdk.fills if f["side"] == "buy")
    sells = sum(1 for f in sdk.fills if f["side"] == "sell")
    assert abs(buys - sells) <= 1, f"买卖应当交替，实际买 {buys} 卖 {sells}"


def test_rule_to_python_rejects_smuggled_names():
    class FakeSpec:
        name = "坏规则"
        team = "x"
        weight = 0.8
        fast = 12
        slow = 48
        period = 14
        buy = "close > 1 or __import__('os')"
        sell = "close < 1"

    with pytest.raises(ValueError, match="未授权"):
        rule_to_python(FakeSpec)


def test_rule_to_python_rejects_lambda_and_semicolon():
    class FakeSpec:
        name = "坏规则"
        weight = 0.8
        fast = 12
        slow = 48
        period = 14
        buy = "lambda: 1"
        sell = "close < 1"

    with pytest.raises(ValueError):
        rule_to_python(FakeSpec)


def test_fullwidth_symbols_survive_compilation():
    spec = validate_rule_spec({**RULE_TEMPLATE, "buy": "close ＞ 100", "sell": "close ＜ 50"})
    source = rule_to_python(spec)
    assert "close > 100" in source and "close < 50" in source
    assert check_source(source) == []


# ---------------- 仓库 + SDK 打通 ----------------
def test_repo_strategy_runs_from_its_file(repo, bars):
    from quant_agent.sdk import load_file

    spec = validate_rule_spec(RULE_TEMPLATE)
    rec = repo.save(name="仓库里的策略", source=rule_to_python(spec), kind="rule")
    module = load_file(rec.path)
    result = run_backtest(module, {"BTC/USDT": bars}, initial_cash=10_000, warmup_bars=100)
    assert not result.errors
    assert result.fills
