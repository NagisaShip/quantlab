"""组合保护与动态标的池：锁定时刻、只拦开仓、三种模式一致、标的池按当时可见数据重建、下架公告即时生效。"""

from __future__ import annotations

import time
from decimal import Decimal

from test_trade_mgmt import StopBroker, series

from quant_agent.data.quality import TIMEFRAME_MS
from quant_agent.engine.live import LiveRunner
from quant_agent.live.oms import DemoOMS, OmsLimits
from quant_agent.sdk import load_source, run_backtest
from quant_agent.sdk.runtimes import RuntimeCost

H = TIMEFRAME_MS["1h"]


class BarBroker(StopBroker):
    """成交回报带上当前 bar 的时间（真实交易所回报的是成交时刻）。"""

    ts = 0

    def place_market(self, symbol, side, qty, client_order_id):
        rep = super().place_market(symbol, side, qty, client_order_id)
        rep.ts = self.ts
        return rep


COST = RuntimeCost(taker_fee=0, slippage_bps=0)

# 每根 bar 想开仓、开仓后下一根就止损出场：用来稳定地制造"连续止损"
CHURN = """
PROTECTIONS = {prot}

def initialize(ctx):
    set_stop_loss(ctx.securities[0], 0.01)
    g.tries = 0

def handle_data(ctx):
    s = ctx.securities[0]
    if not ctx.portfolio.positions.get(s) or ctx.portfolio.positions[s].amount == 0:
        g.tries += 1
        order_target_percent(s, 0.5, reason="dip")
"""


def falling(n=60, step=0.02):
    """一路下跌：每次开仓都会在下一根被 1% 止损打掉。"""
    return [100 * (1 - step) ** i for i in range(n)]


def run(prot, closes, **kw):
    src = CHURN.replace("{prot}", repr(prot))
    return run_backtest(load_source(src, name="g"), {"BTC/USDT": series(closes)}, cost=COST, **kw)


def entries(res):
    return [t["open_ts"] for t in res.round_trips]


def test_stoploss_guard_pauses_entries_and_records_lock():
    free = run([], falling())
    guarded = run([{"method": "StoplossGuard", "lookback_bars": 10, "trade_limit": 3, "stop_bars": 8}], falling())
    assert len(guarded.round_trips) < len(free.round_trips)
    g = guarded.metrics["guards"]
    assert g["locks"] and g["locks"][0]["method"] == "StoplossGuard" and g["locks"][0]["security"] == "*"
    assert g["blocked"].get("PROTECTION_LOCK", 0) > 0
    lk = g["locks"][0]
    # 锁定期间没有做出任何开仓决定（开仓在下一根开盘成交，决定时刻是前一根收盘）；锁到期后又能开
    assert not [t for t in entries(guarded) if lk["from"] < t - 1 < lk["until"]]
    assert any(t >= lk["until"] for t in entries(guarded))
    assert any(r.get("code") == "PROTECTION_LOCK" for r in guarded.rejected)


def test_cooldown_blocks_reentry_on_same_pair_only_for_n_bars():
    res = run([{"method": "CooldownPeriod", "stop_bars": 3}], falling())
    trips = res.round_trips
    for a, b in zip(trips, trips[1:], strict=False):
        assert b["open_ts"] - a["close_ts"] >= 3 * H - 1


def test_max_drawdown_locks_globally():
    res = run(
        [
            {
                "method": "MaxDrawdown",
                "lookback_bars": 30,
                "trade_limit": 2,
                "max_allowed_drawdown": 0.01,
                "stop_bars": 20,
            }
        ],
        falling(),
    )
    locks = res.metrics["guards"]["locks"]
    assert locks and all(lk["security"] == "*" for lk in locks)


def test_low_profit_pairs_locks_that_pair():
    res = run(
        [{"method": "LowProfitPairs", "lookback_bars": 20, "trade_limit": 2, "required_profit": 0.0, "stop_bars": 10}],
        falling(),
    )
    locks = res.metrics["guards"]["locks"]
    assert locks and locks[0]["security"] == "BTC/USDT"


def test_locks_never_block_exits():
    # 锁住开仓时已有的持仓照样止损：用 MaxDrawdown 锁住后不应出现"止损单被拒"
    res = run([{"method": "StoplossGuard", "lookback_bars": 10, "trade_limit": 1, "stop_bars": 50}], falling())
    assert all(r.get("code") == "PROTECTION_LOCK" for r in res.rejected if r.get("code"))
    assert all(t["exit_reason"] == "stop_loss" for t in res.round_trips)


def test_bad_protection_config_is_rejected():
    import pytest

    from quant_agent.sdk.runner import StrategyLoadError

    with pytest.raises(StrategyLoadError, match="保护规则只能是"):
        run([{"method": "Nope"}], falling(10))


# ---------------------------------------------------------------- 三种模式一致
def _live(prot, closes, fill_mode):
    src = CHURN.replace("{prot}", repr(prot))
    bars = series(closes)
    live = LiveRunner()
    live.start(
        "p",
        load_source(src, name="g"),
        {"BTC/USDT": bars[:2]},
        timeframe="1h",
        initial_cash=10_000,
        cost=COST,
        started_at=0,
        fill_mode=fill_mode,
    )
    broker = BarBroker(closes[0])
    oms = DemoOMS(broker, OmsLimits(max_order_notional=1e9, max_orders_per_minute=10_000), initial_cash=10_000)
    for b in bars[2:]:
        broker.price, broker.ts = Decimal(str(b.open)), b.close_time  # 这个测试里订单在收盘决策后立即成交
        out = live.step("p", {"BTC/USDT": [b]})
        if fill_mode == "external":
            outbox = out.get("outbox") or []
            for _ in range(3):
                if not outbox:
                    break
                reps = oms.process(outbox, [], [])
                res = live.apply_reports("p", reps)
                outbox = res.get("outbox") or []
    return live._states["p"].runtime


def test_protection_locks_identical_in_backtest_paper_and_demo():
    prot = [{"method": "StoplossGuard", "lookback_bars": 10, "trade_limit": 3, "stop_bars": 8}]
    closes = falling(40)
    bt = run(prot, closes)
    paper = _live(prot, closes, "internal")
    demo = _live(prot, closes, "external")
    # 纸面模拟比回测晚 2 根起步（前 2 根用来预热），除此之外锁定时刻逐条一致
    a = [(lk["from"] - 2 * H, lk["until"] - 2 * H) for lk in paper.guards.locks()]
    b = [(lk["from"], lk["until"]) for lk in bt.metrics["guards"]["locks"]]
    assert a and a == b[: len(a)]
    # 模拟盘的止损由交易所挂单在下一根触发，成交时刻和内部撮合不同；规则本身一样：锁定期间不开新仓
    dl = demo.guards.locks()
    assert dl and all(lk["method"] == "StoplossGuard" for lk in dl)
    for lk in dl:
        assert not [t for t in demo.round_trips if lk["from"] < t.open_ts < lk["until"]]
    assert demo.guards.blocked.get("PROTECTION_LOCK", 0) > 0


# ---------------------------------------------------------------- 动态标的池
def _bars(sym, closes, vol, start):
    out = series(closes, sym, start=start)
    return [
        b.__class__(b.symbol, b.timeframe, b.open_time, b.close_time, b.open, b.high, b.low, b.close, vol) for b in out
    ]


ROTATE = """
PAIRLIST = {cfg}

def initialize(ctx):
    g.seen = []

def handle_data(ctx):
    u = get_universe()
    g.seen.append([ctx.current_dt, u])
    for s in ctx.securities:
        order_target_percent(s, 0.3 if s in u else 0.0)
"""

T0 = series([1])[0].open_time


def test_universe_is_point_in_time_no_survivorship():
    flat = [100.0] * 80
    data = {
        "AAA/USDT": _bars("AAA/USDT", flat, 1e6, T0),
        "BBB/USDT": _bars("BBB/USDT", flat, 1e3, T0),
        # CCC 在第 40 根才上市（之前没有数据），成交额最大
        "CCC/USDT": _bars("CCC/USDT", flat[:40], 1e9, T0 + 40 * H),
    }
    cfg = {
        "refresh_bars": 5,
        "filters": [{"method": "AgeFilter", "min_bars": 10}, {"method": "VolumeFilter", "lookback_bars": 5, "top": 1}],
    }
    res = run_backtest(load_source(ROTATE.replace("{cfg}", repr(cfg)), name="u"), data, cost=COST)
    seen = res.globals["seen"]
    before = [u for ts, u in seen if T0 + 11 * H <= ts < T0 + 45 * H]
    after = [u for ts, u in seen if ts >= T0 + 56 * H]
    assert before and all(u == ["AAA/USDT"] for u in before), "上市前和上市不满 10 根时不能选到 CCC"
    assert after and all(u == ["CCC/USDT"] for u in after)
    hist = res.metrics["guards"]["universe_history"]
    assert any("CCC/USDT" in h["dropped"] for h in hist)
    # BBB 从没进池子，也就从没开过仓
    assert not [f for f in res.fills if f["security"] == "BBB/USDT"]


def test_delisting_announcement_blocks_from_its_publish_time_in_backtest():
    flat = [100.0] * 30
    data = {"AAA/USDT": _bars("AAA/USDT", flat, 1e6, T0), "BBB/USDT": _bars("BBB/USDT", flat, 1e6, T0)}
    cfg = {"refresh_bars": 1000, "filters": [], "delist_exit": True}
    at = T0 + 10 * H + 30 * 60_000  # 第 10 根中间发布
    res = run_backtest(
        load_source(ROTATE.replace("{cfg}", repr(cfg)), name="u"),
        data,
        cost=COST,
        delistings=[{"coin": "BBB", "available_at": at}],
    )
    seen = res.globals["seen"]
    assert all("BBB/USDT" in u for ts, u in seen if ts < at)
    assert all("BBB/USDT" not in u for ts, u in seen if ts >= at)
    sells = [f for f in res.fills if f["security"] == "BBB/USDT" and f["side"] == "sell"]
    assert sells and sells[0]["reason"] == "delisted"


def test_delisting_in_live_takes_effect_on_next_tick_without_new_bar():
    flat = [100.0] * 12
    data = {"AAA/USDT": _bars("AAA/USDT", flat, 1e6, T0), "BBB/USDT": _bars("BBB/USDT", flat, 1e6, T0)}
    cfg = {"refresh_bars": 1000, "filters": []}
    live = LiveRunner()
    live.start(
        "p",
        load_source(ROTATE.replace("{cfg}", repr(cfg)), name="u"),
        {k: v[:6] for k, v in data.items()},
        timeframe="1h",
        initial_cash=10_000,
        cost=COST,
        started_at=0,
    )
    live.step("p", {k: [v[6]] for k, v in data.items()})
    rt = live._states["p"].runtime
    assert "BBB/USDT" in rt.universe()
    published = int(time.time() * 1000)
    t0 = time.monotonic()
    # 没有新 bar，只带公告：同一次调用里就生效（运行节点每 30 秒一拍，满足"1 分钟内移出"）
    live.step("p", {k: [v[6]] for k, v in data.items()}, delistings=[{"coin": "BBB", "available_at": published}])
    assert time.monotonic() - t0 < 60
    assert "BBB/USDT" not in rt.universe()
    assert rt.guards.entry_block("BBB/USDT")[0] == "DELISTED"
    # 快照恢复后仍然有效
    snap = live.snapshot("p")
    live2 = LiveRunner()
    live2.start(
        "q",
        load_source(ROTATE.replace("{cfg}", repr(cfg)), name="u"),
        {k: v[:7] for k, v in data.items()},
        timeframe="1h",
        initial_cash=10_000,
        cost=COST,
        started_at=0,
    )
    live2.restore("q", snap)
    assert live2._states["q"].runtime.guards.entry_block("BBB/USDT")[0] == "DELISTED"


def test_old_strategy_without_guards_unaffected():
    res = run_backtest(
        load_source(
            "def initialize(ctx):\n    pass\n\ndef handle_data(ctx):\n"
            "    order_target_percent(ctx.securities[0], 0.5)\n",
            name="o",
        ),
        {"BTC/USDT": series([100 + i for i in range(20)])},
        cost=COST,
    )
    assert "guards" not in res.metrics


def test_platform_hands_stored_delistings_to_kernel():
    from quant_agent.research.events import delisting_rows, uses_pairlist

    class Store:
        def list(self, _):
            return [
                {"type": "delisting", "coins": ["BBB", "CCC"], "available_at": 5, "ts": 1},
                {"type": "listing", "coins": ["DDD"], "available_at": 9},
            ]

    assert delisting_rows(Store()) == [{"coin": "BBB", "available_at": 5}, {"coin": "CCC", "available_at": 5}]
    assert uses_pairlist("PAIRLIST = {}") and uses_pairlist("set_pairlist({})") and not uses_pairlist("x = 1")
