"""回测口径与结论核验：分段口径、成本一致性、开发数据标记、代码与报告核对、稳定性报告。"""

from __future__ import annotations

import math

from quant_agent.agents import backtest_audit as BA

H = 3_600_000


def _result(n=240, drift=0.001, trips=None, fee_rate=0.001, slip=0.0005, fills=None, scale=1.0):
    eq = [10_000 * (1 + drift) ** i * (1 + 0.01 * math.sin(i / 7)) for i in range(n)]
    ts = [1_700_000_000_000 + i * H for i in range(n)]
    if fills is None:
        fills = []
        for k in range(10):
            notional = 5000.0
            fills.append(
                {
                    "ts": ts[k * 20],
                    "notional": str(notional),
                    "fee_value": str(notional * fee_rate * scale),
                    "price": str(100 * (1 + slip * scale)),
                    "market_price": "100",
                }
            )
    fees = sum(float(f["fee_value"]) for f in fills)
    return {
        "timestamps": ts,
        "equity_curve": eq,
        "benchmark_curve": [10_000 * (1 + 0.0005) ** i for i in range(n)],
        "initial_cash": 10_000,
        "timeframe": "1h",
        "fills": fills,
        "fills_total": len(fills),
        "round_trips": trips if trips is not None else [
            {"open_ts": ts[10], "close_ts": ts[50], "return": 0.02},
            {"open_ts": ts[70], "close_ts": ts[130], "return": -0.01},  # 跨段
            {"open_ts": ts[150], "close_ts": ts[200], "return": 0.03},
        ],
        "metrics": {"total_return": eq[-1] / 10_000 - 1, "total_fees": fees, "final_equity": eq[-1] - fees * 0, "trades": 3},
    }


def test_semantics_declare_every_field():
    for mode in ("independent", "sliced", "full"):
        s = BA.segment_semantics(mode)
        for key in ("continuous_compounding", "reset_capital", "cross_segment_positions", "trade_attribution", "reconstructs_full"):
            assert key in s
    assert BA.segment_semantics("independent")["reset_capital"] is True
    assert BA.segment_semantics("independent")["reconstructs_full"] is False
    assert BA.segment_semantics("sliced")["continuous_compounding"] is True
    assert BA.segment_semantics("sliced", "open")["trade_attribution"] == "open"


def test_sliced_segments_reconstruct_full_return_and_attribute_trades():
    r = _result()
    out = BA.slice_full(r, 3, attribution="close")
    assert out["ok"]
    rec = out["reconcile"]
    assert rec["reconstructs"] is True, rec
    assert abs(rec["chained_return"] - r["metrics"]["total_return"]) < 1e-9
    # 3 笔交易按平仓时间各归一段，合计等于全区间
    assert [s["metrics"]["trades"] for s in out["segments"]] == [1, 1, 1]
    assert rec["segment_trades"] == 3 and not rec["issues"]
    # 第二笔开仓在第 1 段、平仓在第 2 段：第 2 段带着跨段持仓开始
    assert out["segments"][1]["carried_position_in"] is True
    by_open = BA.slice_full(r, 3, attribution="open")
    assert [s["metrics"]["trades"] for s in by_open["segments"]] == [2, 1, 0]


def test_independent_segments_never_claim_to_reconstruct():
    rows = [{"ok": True, "metrics": {"total_return": 0.05, "trades": 2}}, {"ok": True, "metrics": {"total_return": 0.05, "trades": 1}}]
    rec = BA.reconcile(rows, {"total_return": 0.2, "trades": 4}, "independent")
    assert rec["reconstructs"] is False and rec["issues"]


def test_cost_audit_passes_when_fees_scale_and_blocks_conclusion_when_not():
    base = _result()
    good = _result(scale=2.0)
    good["metrics"]["final_equity"] = base["metrics"]["final_equity"] - (good["metrics"]["total_fees"] - base["metrics"]["total_fees"]) - 25
    good["metrics"]["total_return"] = base["metrics"]["total_return"] - 0.004
    ok = BA.cost_audit(base, good, 2.0)
    assert ok["checks"] and all(c["ok"] for c in ok["checks"] if c["name"].startswith(("有效费率", "滑点"))), ok
    # 压力回测的手续费没有翻倍（例如倍数没生效）：标记异常，禁止「成本不是约束」
    bad = _result(scale=1.0)
    out = BA.cost_audit(base, bad, 2.0)
    assert out["consistent"] is False
    assert any("有效费率" in a for a in out["anomalies"])
    assert out["conclusion_allowed"]["cost_not_binding"] is False
    # 明细合计和汇总对不上也算异常
    broken = _result(scale=2.0)
    broken["metrics"]["total_fees"] = 1.0
    assert any("明细合计" in a for a in BA.cost_audit(base, broken, 2.0)["anomalies"])


def test_ranges_seen_before_a_code_change_are_development_data():
    p = {"current_v": 1, "seen_ranges": []}
    p["seen_ranges"] = BA.record_seen(p, 0, 100, "全区间回测")
    rows = [{"segment": 1, "start": 0, "end": 50}, {"segment": 2, "start": 150, "end": 200}]
    # 还没改代码：同一版本看过不算参与开发
    assert [r["data_role"] for r in BA.tag_development(p, rows)] == ["unseen", "unseen"]
    p["current_v"] = 2  # 看过 0–100 的结果后改了代码
    tagged = BA.tag_development(p, rows)
    assert [r["data_role"] for r in tagged] == ["development", "unseen"]


def test_trailing_stop_must_move_one_way():
    bad = "def step(ctx):\n    ctx.trail_stop = ctx.close - 2 * ctx.atr + 0 * ctx.trail_stop\n"
    good = "def step(ctx):\n    ctx.trail_stop = max(ctx.trail_stop, ctx.close - 2 * ctx.atr)\n"
    assert BA.trailing_stop_check(bad)
    assert not BA.trailing_stop_check(good)


def test_factor_formula_must_match_entry_and_report_numbers_must_match():
    issues = BA.factor_entry_check("rank(ts_mean(funding, 24))", "if close > ma(close, 20): buy()")
    assert any("funding" in x for x in issues)
    out = BA.code_report_check(
        code="x = 1",
        report="共 12 笔交易，使用 1500 根 K 线。第 2 段收益 5.0%。",
        facts={"trades": 9, "bars": 1500, "segments": [{"segment": 2, "total_return": 0.031}]},
    )
    assert not out["ok"]
    assert any("12 笔交易" in x for x in out["issues"]) and any("第 2 段" in x for x in out["issues"])
    assert not any("1500" in x for x in out["issues"])


def test_stability_report_and_sensitivity():
    rep = BA.stability_report(_result(), windows=4)
    assert rep["ok"] and len(rep["windows"]) == 4
    d = rep["distribution"]
    assert d["p05"] <= d["p50"] <= d["p95"] and d["worst"] <= d["best"]
    grid = BA.sensitivity_grid({"fast": 10, "slow": 30, "k": 2.0, "flag": True})
    assert ("fast", 8) in grid and ("fast", 12) in grid and all(k != "flag" for k, _ in grid)
    s = BA.sensitivity_summary(1.5, [{"sharpe": 1.3}, {"sharpe": 0.2}])
    assert s["fragile"] is True
