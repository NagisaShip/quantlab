"""实时模拟与实盘对照测试。"""

from __future__ import annotations

import pytest

from quant_agent.data.service import synthetic_bars
from quant_agent.engine import (
    HOUR_MS,
    LiveFill,
    PaperSession,
    PaperTradingEngine,
    parse_live_fills_csv,
    reconcile,
)
from quant_agent.sdk import RuntimeCost


@pytest.fixture
def engine():
    return PaperTradingEngine(RuntimeCost())


def make_session(engine: PaperTradingEngine, *, now: int = 1_700_000_000_000) -> PaperSession:
    return engine.start(
        strategy_id="ma_trend",
        strategy_name="双均线趋势",
        strategy_version="abc123",
        symbol="BTC/USDT",
        initial_cash=10_000,
        now=now,
    )


# ---------------- 生命周期 ----------------
def test_start_creates_running_session(engine):
    s = make_session(engine)
    assert s.status == "running"
    assert s.id and s.started_at
    assert s.cost["taker_fee"] == 0.001
    assert s.elapsed_hours == 0


def test_restart_marks_session_interrupted_without_faking_downtime(engine):
    s = make_session(engine)
    engine.on_sample(s, bars=[], equity=10_000, now=s.started_at + 60_000)
    recovered = engine.recover(s, now=s.started_at + 5 * HOUR_MS)
    assert recovered.status == "interrupted"
    assert any("中断" in e.message for e in recovered.events)
    # 停机时段不补净值点：观测数保持不变
    assert recovered.observations == 1


def test_stop_does_not_auto_close_position(engine):
    s = make_session(engine)
    engine.stop(s)
    assert s.status == "stopped"
    assert any("未自动平仓" in e.message for e in s.events)


# ---------------- 采样与推进 ----------------
def test_only_new_closed_bar_triggers_strategy(engine):
    s = make_session(engine)
    bars = synthetic_bars("BTC/USDT", "1h", 10)
    first = engine.on_sample(s, bars=bars[:5], equity=10_000, now=s.started_at + 60_000)
    assert first["triggered"] is True
    # 同样的 bar 再来一次：不应重复触发
    again = engine.on_sample(s, bars=bars[:5], equity=10_000, now=s.started_at + 120_000)
    assert again["triggered"] is False
    newer = engine.on_sample(s, bars=bars[:6], equity=10_000, now=s.started_at + 180_000)
    assert newer["triggered"] is True


def test_open_bars_are_ignored(engine):
    s = make_session(engine)
    bars = synthetic_bars("BTC/USDT", "1h", 5)
    bars[-1] = type(bars[-1])(**{**bars[-1].__dict__, "closed": False})
    engine.on_sample(s, bars=bars, equity=10_000, now=s.started_at + 60_000)
    assert s.last_bar_ts == bars[-2].close_time


def test_sampling_gap_is_recorded(engine):
    s = make_session(engine)
    engine.on_sample(s, bars=[], equity=10_000, now=s.started_at + 60_000)
    engine.on_sample(s, bars=[], equity=10_000, now=s.started_at + 60_000 + 5 * 60_000)
    assert any("运行缺口" in e.message for e in s.events)


def test_stale_quote_pauses_and_records(engine):
    s = make_session(engine)
    result = engine.on_sample(s, bars=[], quote={"age_ms": 30 * 60_000}, equity=10_000, now=s.started_at + 60_000)
    assert result["skipped"] == "行情过期"
    assert s.status == "paused"
    assert s.errors == 1


def test_fresh_quote_does_not_pause(engine):
    s = make_session(engine)
    engine.on_sample(s, bars=[], quote={"age_ms": 1200}, equity=10_000, now=s.started_at + 60_000)
    assert s.status == "running"


def test_equity_points_are_throttled_to_one_per_minute(engine):
    s = make_session(engine)
    for i in range(5):
        engine.on_sample(s, bars=[], equity=10_000 + i, now=s.started_at + i * 10_000)
    # 只有间隔 >= 60 秒的采样才记录净值
    assert s.observations == 0
    engine.on_sample(s, bars=[], equity=10_050, now=s.started_at + 70_000)
    assert s.observations == 1


def test_skipped_bar_signal_is_logged_after_recovery(engine):
    s = make_session(engine)
    bars = synthetic_bars("BTC/USDT", "1h", 40)
    engine.on_sample(s, bars=bars[:2], equity=10_000, now=s.started_at + 60_000)
    # 隔了 5 小时才采到新 bar：应记录"跳过过期信号"
    engine.on_sample(s, bars=bars[:30], equity=10_000, now=s.started_at + 5 * HOUR_MS)
    assert any("跳过过期信号" in e.message for e in s.events)


def test_session_completes_at_30_days(engine):
    s = make_session(engine)
    engine.on_sample(s, bars=[], equity=10_000, now=s.started_at + 30 * 24 * HOUR_MS + 1)
    assert s.status == "completed"


def test_checkpoint_progress(engine):
    s = make_session(engine)
    s.last_update = s.started_at + 84 * HOUR_MS  # 3.5 天
    assert s.checkpoint_progress(7) == pytest.approx(50.0, abs=0.5)
    assert s.checkpoint_progress(30) == pytest.approx(11.67, abs=0.5)


def test_paused_session_does_not_process_samples(engine):
    s = make_session(engine)
    s.status = "paused"
    result = engine.on_sample(s, bars=[], equity=10_000, now=s.started_at + 60_000)
    assert result["skipped"]


# ---------------- 报告 ----------------
def test_report_has_windows_and_conventions(engine):
    s = make_session(engine)
    # 造 20 天的净值点
    for i in range(20 * 24):
        engine.on_sample(s, bars=[], equity=10_000 * (1 + i * 0.0002), now=s.started_at + (i + 1) * HOUR_MS)
    report = engine.report(s)
    assert report["session"]["observations"] == 20 * 24
    assert "7" in report["windows"]["windows"]
    assert "30" in report["windows"]["windows"]
    assert report["windows"]["windows"]["7"]["complete"] is True
    assert report["windows"]["windows"]["30"]["complete"] is False
    assert "未满" in report["window_status"]["30"]
    joined = " ".join(report["conventions"])
    assert "虚拟账户" in joined and "不能替代" in joined


def test_session_roundtrip_through_dict(engine):
    s = make_session(engine)
    engine.on_sample(s, bars=[], equity=10_000, now=s.started_at + 70_000)
    data = s.to_dict()
    restored = PaperSession.from_dict(data)
    assert restored.id == s.id
    assert restored.observations == s.observations
    assert len(restored.events) == len(s.events)


# ---------------- 实盘对照 ----------------
def _sim_fills():
    base = 1_700_000_000_000
    return [
        {
            "ts": base,
            "security": "BTC/USDT",
            "side": "buy",
            "amount": 0.05,
            "price": 62_000,
            "fee": 3.1,
            "order_id": "sim-0",
            "client_order_id": "c-0",
        },
        {
            "ts": base + HOUR_MS,
            "security": "BTC/USDT",
            "side": "sell",
            "amount": 0.05,
            "price": 62_400,
            "fee": 3.12,
            "order_id": "sim-1",
            "client_order_id": "c-1",
        },
    ]


def test_reconcile_exact_match_by_client_order_id():
    sim = _sim_fills()
    live = [
        LiveFill(
            ts=sim[0]["ts"] + 1500,
            security="BTC/USDT",
            side="buy",
            amount=0.05,
            price=62_003,
            fee=3.2,
            client_order_id="c-0",
        ),
        LiveFill(
            ts=sim[1]["ts"] + 1800,
            security="BTC/USDT",
            side="sell",
            amount=0.05,
            price=62_398,
            fee=3.22,
            client_order_id="c-1",
        ),
    ]
    report = reconcile(sim, live)
    assert len(report.matched) == 2
    assert not report.unmatched_simulated and not report.unmatched_live
    assert report.to_dict()["match_rate"] == 1.0
    # 价格偏差应该在个位数 bps
    assert abs(report.matched[0].price_diff_bps) < 5
    assert report.layers["成交层"]["平均价格偏差_bps"] is not None
    assert "费用层" in report.layers


def test_reconcile_falls_back_to_fuzzy_match():
    sim = _sim_fills()
    live = [
        LiveFill(ts=sim[0]["ts"] + 60_000, security="BTC/USDT", side="buy", amount=0.0505, price=62_010, fee=3.2),
        LiveFill(ts=sim[1]["ts"] + 90_000, security="BTC/USDT", side="sell", amount=0.05, price=62_390, fee=3.22),
    ]
    report = reconcile(sim, live)
    assert len(report.matched) == 2
    assert "信号层" in report.layers


def test_reconcile_reports_unmatched_on_both_sides():
    sim = _sim_fills()
    live = [
        # 多出来的一笔：可能是外部手工交易
        LiveFill(ts=sim[0]["ts"] + 5 * HOUR_MS, security="BTC/USDT", side="buy", amount=0.3, price=61_000, fee=18.0),
    ]
    report = reconcile(sim, live)
    assert report.unmatched_simulated
    assert report.unmatched_live
    assert report.to_dict()["match_rate"] == 0.0
    assert "外部手工交易" in report.layers["信号层"]["说明"]


def test_reconcile_empty_inputs():
    report = reconcile([], [])
    assert "无法对照" in report.notes[0]


def test_reconcile_residual_separates_sim_and_live_accounting():
    report = reconcile(_sim_fills(), [])
    assert report.residual["模拟成交名义金额"] > 0
    assert report.residual["实盘成交名义金额"] == 0
    assert any("分别核算" in n for n in report.notes)


def test_parse_live_fills_csv_roundtrip():
    text = (
        "timestamp,security,side,amount,price,fee,order_id,fill_id\n"
        "1700000000000,BTC/USDT,buy,0.05,62000,3.1,o-1,f-1\n"
        "1700003600000,BTC/USDT,sell,0.05,62400,3.12,o-2,f-2\n"
    )
    fills = parse_live_fills_csv(text)
    assert len(fills) == 2
    assert fills[0].side == "buy" and fills[0].amount == 0.05
    assert fills[1].fill_id == "f-2"


def test_parse_live_fills_csv_rejects_missing_columns():
    with pytest.raises(ValueError, match="缺少必需列"):
        parse_live_fills_csv("timestamp,security,side\n1,BTC/USDT,buy\n")


def test_parse_live_fills_csv_row_limit():
    header = "timestamp,security,side,amount,price\n"
    rows = "".join(f"{1700000000000 + i},BTC/USDT,buy,0.01,62000\n" for i in range(5001))
    with pytest.raises(ValueError, match="最多 5000 行"):
        parse_live_fills_csv(header + rows)
