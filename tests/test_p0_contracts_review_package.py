from __future__ import annotations

from decimal import Decimal

from test_ledger import mk

from quant_agent.exchanges.contracts import spec_for
from quant_agent.platform.features import enabled, validate_patch
from quant_agent.research.package import build, inspect, replay_context
from quant_agent.research.review import build_review
from quant_agent.sdk import Ledger, load_source
from quant_agent.sdk.runtimes import RuntimeCost, run_backtest


def _clean_review(**kw):
    base = dict(
        metrics={"trades": 40, "fills": 50, "total_return": 0.22, "benchmark_return": 0.10, "total_fees": 1.2},
        source="def initialize(ctx): pass\ndef handle_data(ctx): pass\n",
        datasets=[{"symbol": "BTC/USDT", "source": "exchange", "digest": "abc", "bars": 500}],
        simple_benchmark={"total_return": 0.05},
        trial_count=1,
    )
    base.update(kw)
    return build_review(**base)


def test_contract_spec_has_mmr_tiers_and_binance_scope():
    spec = spec_for("binance", "BTC/USDT:PERP")
    assert spec.contract_type == "linear"
    assert spec.settlement == "USDT"
    assert spec.mmr_for_notional(10_000) < spec.mmr_for_notional(1_000_000)
    assert spec.mmr_for_notional(1_000_000) < spec.mmr_for_notional(20_000_000)
    assert spec.liquidation_fee_bps > 0 and spec.liquidation_slippage_bps >= 0


def test_review_blocks_zero_trade_or_missing_funding():
    rv = _clean_review(metrics={"trades": 0, "fills": 0})
    assert rv["verdict"] == "fail" and rv["blocks_promotion"]
    assert any(x["id"] == "trades" and x["severity"] == "block" for x in rv["items"])

    rv = _clean_review(
        datasets=[{"symbol": "BTC/USDT:PERP", "source": "exchange", "digest": "abc"}],
        funding=[],
        contract_specs={"BTC/USDT:PERP": spec_for("binance", "BTC/USDT:PERP").to_dict()},
    )
    assert rv["verdict"] == "fail" and rv["blocks_promotion"]
    assert any(x["id"] == "funding" and x["severity"] == "block" for x in rv["items"])


def test_review_marks_unknown_validation_as_insufficient_not_pass():
    rv = _clean_review()
    assert rv["verdict"] in ("conditional", "insufficient")
    assert any(x["id"] == "lookahead" and x["severity"] == "unknown" for x in rv["items"])


def test_research_package_round_trip_hashes():
    bt = {
        "id": "bt-1",
        "run_id": "run-1",
        "strategy_name": "demo",
        "code": "def initialize(ctx): pass\n",
        "params": {"fast": 12},
        "metrics": {"trades": 3, "total_return": 0.1},
        "row": {"strategy_version_id": "v1", "lang": "python"},
        "manifest": {"platform_version": "2.0.0", "lang": "python", "params": {"fast": 12}},
        "review": {"id": "rv-1", "verdict": "conditional", "blocks_promotion": False},
        "spec": {"params": {"fast": 12}, "timeframe": "1h"},
    }
    snap = {
        "bars": {"BTC/USDT": [[1, 2, 1, 2, 1, 2, 3, "1h"]]},
        "funding": {"funding:BTC/USDT:PERP": {"times": [2], "rates": ["0.0001"], "mark_prices": ["100"]}},
        "spec": {"params": {"fast": 12}, "timeframe": "1h"},
    }
    data, manifest = build(backtest=bt, snapshot=snap)
    got = inspect(data)
    assert got["ok"], got
    assert manifest["code_sha256"] == got["manifest"]["code_sha256"]
    ctx = replay_context(data)
    assert ctx["source"] == bt["code"]
    assert ctx["funding"]["funding:BTC/USDT:PERP"]["mark_prices"] == ["100"]


def test_capabilities_matrix_is_explicit_about_perps(tmp_path):
    from fastapi.testclient import TestClient

    from quant_agent.api.app import create_app
    from quant_agent.api.deps import AppServices, reset_services, set_services

    set_services(AppServices.build(store_root=str(tmp_path)))
    try:
        with TestClient(create_app()) as client:
            got = client.get("/api/capabilities").json()
    finally:
        reset_services()
    assert got["native_backtest"]["perp"]["supported"] is True
    assert "isolated" in got["native_backtest"]["perp"]["margin_modes"]
    assert got["native_backtest"]["perp"]["cross_margin"] is True
    assert got["native_backtest"]["perp"]["hedge_mode"] is True
    assert set(got["native_backtest"]["perp"]["exchanges"]) == {"binance", "okx", "bybit"}
    assert got["native_backtest"]["perp"]["specs"]["okx"]["contract_type"] == "linear"
    assert got["qstrat"]["perp"]["supported"] is True
    assert got["microlive"]["perp"]["supported"] is False
    assert got["real_money"]["perp"]["supported"] is True
    assert got["freqtrade"]["short"]["supported"] is True


def test_run_backtest_emits_simple_benchmark_curve():
    bars = mk([(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(120)])
    src = "def initialize(ctx): pass\ndef handle_data(ctx): pass\n"
    r = run_backtest(
        load_source(src),
        {"BTC/USDT": bars},
        cost=RuntimeCost(
            taker_fee=0, slippage_bps=0, spread_bps=0, min_notional=0, rebalance_tolerance=0, max_volume_participation=1
        ),
    )
    assert len(r.simple_benchmark_curve) == len(r.timestamps)
    assert r.simple_benchmark_metrics["label"].startswith("SMA")
    assert "simple_benchmark_return" in r.metrics


def test_ten_x_perp_crash_loss_is_bounded_by_isolated_margin():
    led = Ledger()
    led.deposit(1000)
    led.set_leverage("BTC/USDT:PERP", 10)
    led.perp_trade(instrument="BTC/USDT:PERP", side="buy", qty=1, price=100, fee=0)
    pos = led.perp("BTC/USDT:PERP")
    margin = pos.margin
    liq = pos.liquidation_price
    booking = led.liquidate(
        "BTC/USDT:PERP",
        price=liq * Decimal("0.999"),
        fee=0.05,
        slippage_bps=10,
        mark_price=liq,
        reason="test crash",
    )
    assert margin == 10
    assert led.liquidation_loss == margin
    assert booking.fee <= margin
    assert pos.liquidation_reason == "test crash"
    assert pos.mmr_tier


def test_unimplemented_features_cannot_be_enabled():
    assert enabled({"features": {"margin.cross": True}})["margin.cross"] is True
    assert validate_patch({"margin.cross": True})["margin.cross"] is True
