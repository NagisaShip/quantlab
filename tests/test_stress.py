"""压力演练：四个极端行情日回放 + 故障注入，走实时执行路径（LiveRunner external 模式）。

测试全部离线：交易所适配器要么直接抛错（走合成兜底），要么是返回确定性行情的假适配器；
并且把 socket 连接打桩成报错，保证没有任何真实网络请求。
"""

from __future__ import annotations

import socket

import pytest

from quant_agent.data.service import MarketDataService
from quant_agent.engine.live import clock_synthetic_bars
from quant_agent.engine.stress import (
    SCENARIOS,
    FaultPlan,
    run_stress,
    scenario_window,
    synthetic_crash_minutes,
)

TREND = """
PARAMS = {"n": 20, "weight": 0.45}

def initialize(ctx):
    g.k = 0

def handle_data(ctx):
    g.k += 1
    for s in ctx.securities:
        closes = history(s, PARAMS["n"], "close")
        if len(closes) < PARAMS["n"]:
            continue
        ma = sum(closes) / len(closes)
        order_target_percent(s, PARAMS["weight"] if closes[-1] > ma else 0.0)
"""

LEVERED_LONG = """
def initialize(ctx):
    set_leverage("BTC/USDT:PERP", 10)
    g.sent = False

def handle_data(ctx):
    if not g.sent:
        g.sent = True
        order_target_percent("BTC/USDT:PERP", 0.9)
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("压力演练测试不允许真实网络请求")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class DownAdapter:
    """模拟网络不可用：每次取数都抛连接错误。"""

    def __init__(self):
        self.calls = 0

    def fetch_bars(self, *a, **k):
        self.calls += 1
        raise ConnectionError("网络不可达")

    def fetch_funding_rates(self, *a, **k):
        self.calls += 1
        raise ConnectionError("网络不可达")


class FakeAdapter:
    """返回确定性行情的假交易所：现货有数据，永续 K 线没有（测试由现货推导）。"""

    def __init__(self):
        self.calls = 0

    def fetch_bars(self, symbol, timeframe, start, end):
        self.calls += 1
        if symbol.endswith(":PERP"):
            raise ValueError("该时段没有永续 K 线")
        bars = clock_synthetic_bars(symbol, timeframe, start, end)
        for b in bars:  # 交易所口径：close_time = 下一根开盘 - 1
            b.close_time -= 1
        return bars

    def fetch_funding_rates(self, symbol, start, end):
        return []


def _strip(report):
    for sc in report["scenarios"]:
        sc.pop("elapsed_s", None)
    report["summary"].pop("elapsed_s", None)
    report.pop("generated_at", None)
    return report


@pytest.fixture(scope="module")
def trend_report():
    return run_stress(MarketDataService({"binance": DownAdapter()}), source=TREND, initial_cash=10_000)


def test_all_four_scenarios_complete_on_synthetic_fallback(trend_report):
    r = trend_report
    assert [s["id"] for s in r["scenarios"]] == [s["id"] for s in SCENARIOS]
    assert r["summary"]["passed"] is True and r["summary"]["completed"] == 4
    assert r["config"]["securities"] == ["BTC/USDT", "ETH/USDT"]
    for sc in r["scenarios"]:
        assert sc["status"] == "completed" and sc["completed"]
        assert sc["data_source"] == "synthetic"
        assert any("合成" in w for w in sc["warnings"])  # 不能悄悄冒充真实行情
        start, end = scenario_window(next(s for s in SCENARIOS if s["id"] == sc["id"]))
        assert sc["bars"] == (end - start) // 60_000
        assert sc["intraday_low"] < -0.1  # 合成路径确实是暴跌形状
        assert 0 < len(sc["equity_curve"]) <= 600 and 0 < len(sc["price_curve"]) <= 600
        assert sc["verdict"] and sc["strategy_errors"] == 0
        assert sc["ledger_mismatch"] == 0  # 交易所侧按真实持仓拒单，账本不会记坏
    assert r["scenarios"][2]["bars"] == 2880  # FTX 两天
    assert any(f["kind"] == "synthetic_data" for f in r["summary"]["flags"])


def test_fault_counters_are_nonzero(trend_report):
    tot = {
        k: sum(sc[k] for sc in trend_report["scenarios"])
        for k in ("orders", "fills", "rejections", "partials", "delayed", "backfill_bursts")
    }
    assert all(v > 0 for v in tot.values()), tot
    for sc in trend_report["scenarios"]:
        assert len(sc["disconnects"]) == 2
        for d in sc["disconnects"]:
            assert 3 <= d["minutes"] <= 10 and d["backfill_bars"] == d["minutes"] + 1
            assert d["skipped_bars"] == d["minutes"]  # 补发期间只在最新一根上调用策略
    msgs = [x["message"] for sc in trend_report["scenarios"] for x in sc["exchange_log"] if x["status"] == "rejected"]
    assert any("过载" in m or "-1003" in m for m in msgs)


def test_deterministic_with_same_seed():
    kw = dict(source=TREND, scenarios=["may19-2021-05-19"], faults={"seed": 42})
    a = _strip(run_stress(MarketDataService(), **kw))
    b = _strip(run_stress(MarketDataService(), **kw))
    assert a == b
    c = _strip(run_stress(MarketDataService(), **{**kw, "faults": {"seed": 43}}))
    assert c["scenarios"][0]["exchange_log"] != a["scenarios"][0]["exchange_log"]


def test_levered_perp_long_is_liquidated_on_crash():
    r = run_stress(MarketDataService(), source=LEVERED_LONG, securities=["BTC/USDT:PERP"])
    assert r["summary"]["passed"]
    for sc in r["scenarios"]:
        assert sc["liquidations"] >= 1, sc["id"]
        assert sc["liquidation_loss"] > 800  # 10 倍杠杆，保证金约 900 全部损失
        assert "被强平" in sc["verdict"]
        assert sc["funding"] is not None
    assert any(f["kind"] == "liquidation" for f in r["summary"]["flags"])


def test_network_failure_is_tried_once_then_synthetic():
    ad = DownAdapter()
    r = run_stress(
        MarketDataService({"binance": ad}), source=TREND, scenarios=["covid-2020-03-12", "yen-carry-2024-08-05"]
    )
    assert r["summary"]["passed"] and ad.calls == 1  # 第一次失败后不再重试，不会每个场景都等超时
    assert all(sc["data_source"] == "synthetic" for sc in r["scenarios"])


def test_exchange_data_path_with_cache_and_derived_perp(tmp_path):
    ad = FakeAdapter()
    market = MarketDataService({"binance": ad}, cache_root=tmp_path)
    kw = dict(
        source=TREND, securities=["BTC/USDT", "BTC/USDT:PERP"], scenarios=["yen-carry-2024-08-05"], timeframe="15m"
    )
    r = run_stress(market, **kw)
    sc = r["scenarios"][0]
    assert sc["completed"] and sc["data_source"] == "exchange"
    assert any("永续 K 线取不到" in w for w in sc["warnings"])
    assert any("资金费率取不到" in w for w in sc["warnings"])
    assert sc["bars"] == 1440 and sc["strategy_bars"] <= 96
    assert sc["disconnects"] and all(d["minutes"] and d["end_ts"] for d in sc["disconnects"])
    calls = ad.calls
    r2 = run_stress(market, **kw)
    assert ad.calls - calls == 1  # 现货走缓存，只剩永续再试一次
    assert _strip(r2)["scenarios"][0]["equity_curve"] == _strip(r)["scenarios"][0]["equity_curve"]


def test_fault_plan_overrides_and_strategy_errors_do_not_crash():
    bad = TREND.replace("g.k += 1", "g.k += 1\n    if g.k % 50 == 0:\n        raise RuntimeError('boom')")
    r = run_stress(
        MarketDataService(),
        source=bad,
        scenarios=["may19-2021-05-19"],
        faults={"disconnects": [], "reject_rate": 1.0, "base_reject_rate": 1.0},
    )
    sc = r["scenarios"][0]
    assert sc["completed"] and r["summary"]["passed"]
    assert sc["disconnects"] == [] and sc["orders"] > 0 and sc["rejections"] == sc["orders"] and sc["fills"] == 0
    assert sc["strategy_errors"] > 0 and "策略报错" in sc["verdict"]
    assert sc["end_equity"] == pytest.approx(10_000)
    plan = FaultPlan.from_dict({"delay_minutes": [5, 2], "partial_rate": 3})
    assert plan.delay_minutes == (2, 5) and plan.partial_rate == 1.0


def test_synthetic_path_is_deterministic_and_rejects_bad_input():
    a = synthetic_crash_minutes("covid-2020-03-12", "BTC/USDT", 0, 600 * 60_000)
    b = synthetic_crash_minutes("covid-2020-03-12", "BTC/USDT", 0, 600 * 60_000)
    assert a == b and all(x.low <= min(x.open, x.close) and x.high >= max(x.open, x.close) for x in a)
    with pytest.raises(ValueError):
        run_stress(MarketDataService(), source=TREND, scenarios=["nope"])
    with pytest.raises(ValueError):
        run_stress(MarketDataService(), source=TREND, timeframe="1w")
