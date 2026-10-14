"""统一赛道与团队比较测试（对应 plan.md 第 4 章）。"""

from __future__ import annotations

import pytest

from quant_agent.data import DataSet
from quant_agent.data.service import synthetic_bars
from quant_agent.engine.arena import DEFAULT_TRACK, Arena, Entrant, track_digest
from quant_agent.strategies.rule_strategy import RuleStrategy
from quant_agent.strategies.rules import RULE_TEMPLATE, validate_rule_spec
from quant_agent.strategies.templates import build_strategy


@pytest.fixture(scope="module")
def dataset():
    bars = synthetic_bars("BTC/USDT", "1h", 900)
    return DataSet("BTC/USDT", "1h", bars, "synthetic").freeze("教学合成数据")


@pytest.fixture
def entrants():
    spec = validate_rule_spec(RULE_TEMPLATE)
    return [
        Entrant("t:ma", "双均线趋势", "趋势研究组", build_strategy("ma_trend"), version="v1"),
        Entrant("t:mr", "RSI 均值回归", "均值回归组", build_strategy("mean_reversion"), version="v2"),
        Entrant(
            "r:trend_rsi",
            "趋势过滤 RSI",
            "第四研究组",
            RuleStrategy(spec),
            origin="user",
            kind="rule",
            version=spec.version,
        ),
    ]


# ---------------- 赛道配置 ----------------
def test_track_defaults_match_the_spec():
    track = Arena.build_track()
    assert track["market"] == "spot"
    assert track["leverage"] == 1.0
    assert track["direction"] == "long_only"
    assert track["initial_cash"] == 10_000
    assert track["timeframe"] == "1h"
    assert track["warmup_bars"] == 100
    assert track["timezone"] == "UTC"
    assert track["cost"]["taker_fee"] == 0.001


@pytest.mark.parametrize(
    "override,match",
    [
        ({"market": "swap"}, "仅支持现货"),
        ({"leverage": 3.0}, "不支持杠杆"),
        ({"direction": "long_short"}, "仅做多"),
        ({"symbols": []}, "至少需要一个交易对"),
        ({"initial_cash": 10}, "初始资金"),
        ({"unknown_key": 1}, "不支持的配置项"),
    ],
)
def test_invalid_track_is_rejected(override, match):
    with pytest.raises(ValueError, match=match):
        Arena.build_track(override)


def test_track_digest_changes_with_costs():
    a = Arena.build_track()
    b = Arena.build_track({"cost": {"taker_fee": 0.002}})
    assert track_digest(a) != track_digest(b)
    assert track_digest(Arena.build_track()) == track_digest(a)


# ---------------- 批次运行 ----------------
def test_all_entrants_share_the_same_track(dataset, entrants):
    batch = Arena().run(entrants, dataset)
    assert len(batch.standings) == len(entrants) + 2  # 两个平台基准恒存在
    for s in batch.standings:
        assert s.metrics["initial_equity"] == 10_000
        assert s.completeness["warmup_bars"] == 100
        # 同一份数据、同一预热，所有策略的观测 bar 数必须一致
        assert s.completeness["scored_bars"] == batch.standings[0].completeness["scored_bars"]
    assert any("统一赛道" in n for n in batch.notes)
    assert any("预热" in n for n in batch.notes)


def test_platform_benchmarks_exist_even_without_hold_strategy(dataset):
    only_rule = Entrant("r", "仅规则", "某组", RuleStrategy(validate_rule_spec(RULE_TEMPLATE)), kind="rule")
    batch = Arena().run([only_rule], dataset)
    ids = {s.strategy_id for s in batch.standings}
    assert {"bench:buy_hold", "bench:cash"} <= ids


def test_short_data_is_rejected(dataset):
    short = DataSet("BTC/USDT", "1h", dataset.bars[:80], "synthetic").freeze("教学合成数据")
    with pytest.raises(ValueError, match="预热"):
        Arena().run([Entrant("t", "x", "y", build_strategy("buy_hold"))], short)


def test_empty_batch_is_rejected(dataset):
    with pytest.raises(ValueError, match="至少需要一个参赛策略"):
        Arena().run([], dataset)


# ---------------- 排行榜 ----------------
def test_leaderboard_presents_metrics_side_by_side(dataset, entrants):
    arena = Arena()
    batch = arena.run(entrants, dataset)
    lb = arena.leaderboard(batch, window="30")
    rows = lb["rows"]

    assert lb["window"] == "30"
    assert lb["benchmark_return"] is not None
    # 每一行都必须同时带收益、回撤、超额、完整性
    for r in rows:
        assert set(r) >= {"return", "max_drawdown", "excess_return", "completeness", "complete", "hours"}
    # 按净收益降序
    returns = [r["return"] for r in rows if r["return"] is not None]
    assert returns == sorted(returns, reverse=True)
    # 现金基准的超额收益 = -买入持有收益
    cash = next(r for r in rows if r["strategy_id"] == "bench:cash")
    assert cash["return"] == pytest.approx(0.0)
    # 口径说明必须写清"不合并为单一分数"与固定窗口而非滚动
    joined = " ".join(lb["conventions"])
    assert "不合并为单一分数" in joined
    assert "前 720 小时" in joined
    assert "不适用" in joined


def test_leaderboard_window_switch(dataset, entrants):
    arena = Arena()
    batch = arena.run(entrants, dataset)
    lb7 = arena.leaderboard(batch, window="7")
    lb30 = arena.leaderboard(batch, window="30")
    assert all(r["hours"] == 168.0 for r in lb7["rows"])
    assert all(r["hours"] == 720.0 for r in lb30["rows"])
    # 两个窗口应给出不同的收益，说明确实按窗口重算了
    a = {r["strategy_id"]: r["return"] for r in lb7["rows"]}
    b = {r["strategy_id"]: r["return"] for r in lb30["rows"]}
    assert any(a[k] != b[k] for k in a if k in b)


def test_leaderboard_can_hide_benchmarks(dataset, entrants):
    arena = Arena()
    batch = arena.run(entrants, dataset)
    lb = arena.leaderboard(batch, include_benchmarks=False)
    assert not any(r["is_benchmark"] for r in lb["rows"])
    assert len(lb["rows"]) == len(entrants)


def test_incomplete_window_is_flagged(dataset):
    short_bars = synthetic_bars("BTC/USDT", "1h", 260)  # 计分约 6.7 天
    short = DataSet("BTC/USDT", "1h", short_bars, "synthetic").freeze("教学合成数据")
    arena = Arena()
    batch = arena.run([Entrant("t", "双均线", "趋势组", build_strategy("ma_trend"))], short)
    lb = arena.leaderboard(batch, window="7")
    for r in lb["rows"]:
        assert r["complete"] is False
        assert r["hours"] < 168
    assert arena.leaderboard(batch, window="7", only_complete=True)["rows"] == []


def test_scenarios_use_same_entrants(dataset, entrants):
    arena = Arena()
    batch = arena.run(entrants, dataset, with_scenarios=True)
    assert set(batch.scenarios) == {"baseline", "cost_1.5x", "cost_2x", "low_liquidity"}
    for rows in batch.scenarios.values():
        assert set(rows) == {e.name for e in entrants}
    # 成本上升不应让收益变好
    for name in {e.name for e in entrants}:
        assert (
            batch.scenarios["cost_2x"][name]["total_return"] <= batch.scenarios["baseline"][name]["total_return"] + 1e-9
        )


def test_batch_persists_to_store(tmp_path, dataset, entrants):
    from quant_agent.store import JsonStore

    store = JsonStore(tmp_path)
    arena = Arena(store=store)
    batch = arena.run(entrants, dataset)
    saved = store.get("batches", batch.id)
    assert saved is not None
    assert saved["track_digest"] == batch.track_digest
    assert saved["count"] == len(batch.standings)
    assert len(saved["standings"]) == len(batch.standings)


def test_default_track_is_not_mutated_by_overrides():
    before = dict(DEFAULT_TRACK["cost"])
    Arena.build_track({"cost": {"taker_fee": 0.009}})
    assert DEFAULT_TRACK["cost"] == before, "赛道默认值被覆盖污染了，后续批次会串参数"
