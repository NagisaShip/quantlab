"""仿真真实度报告：同一策略的回测、纸面模拟、交易所模拟盘放在一起，把收益差拆成几项来源。

做法：
1. 取实时实验（纸面 / 模拟盘）实际跑过的时间段，用同一份冻结代码、同样参数和成本在同一段行情上重跑一次回测；
2. 按「标的 + 方向 + 决策时刻」把两边的成交配成对（决策时刻差一根 bar 以内也算）；
3. 收益差 = 实时期末权益 − 回测同一时刻权益，拆成：
   - 延迟：实际送单时的市价相对回测假定成交时刻的市价走了多少（× 数量）；
   - 滑点：实际成交价偏离送单市价的部分，减去回测模型本来就算进去的滑点；
   - 手续费：实际手续费 − 回测手续费；
   - 拒单：回测成交了、实时被交易所拒掉的委托，按「持有到报告期末」估算错过的盈亏；
   - 部分成交：实时只成交一部分，少成交的数量同样按持有到期末估算；
   - 其他（路径与复利）：上面几项之外的剩余，包括停机错过的信号、多出来的成交、仓位大小随权益变化的复利效应。
   拒单和部分成交按「持有到期末」估算，是近似值，界面会注明。
4. 校准：用实时成交量出实际滑点、手续费率、拒单率、部分成交率、送单延迟，给出回测成本模型的建议值。
"""

from __future__ import annotations

import math
import statistics
import time
import uuid
from typing import Any

REPORTS = "fidelity_reports"
CALIBRATIONS = "cost_calibrations"
COMPONENTS = [
    ("latency", "延迟"),
    ("slippage", "滑点"),
    ("fees", "手续费"),
    ("rejections", "拒单"),
    ("partials", "部分成交"),
    ("other", "其他（路径与复利）"),
]


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def _sign(side: str) -> int:
    return 1 if side == "buy" else -1


def _decided(f: dict[str, Any]) -> int:
    return int(f.get("decided_at") or f.get("ts") or 0)


def match_fills(
    bt: list[dict[str, Any]], live: list[dict[str, Any]], *, tol_ms: int
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """按 标的 + 方向 + 决策时刻 配对。同一决策时刻有多笔时按顺序一一对应。返回 (配对, 只在回测, 只在实时)。"""
    pool: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in sorted(live, key=_decided):
        if f.get("reason") == "liquidation":
            continue
        pool.setdefault((f["security"], f["side"]), []).append(f)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    only_bt: list[dict[str, Any]] = []
    for b in sorted(bt, key=_decided):
        cands = pool.get((b["security"], b["side"])) or []
        best, gap = None, tol_ms + 1
        for c in cands:
            d = abs(_decided(c) - _decided(b))
            if d < gap:
                best, gap = c, d
        if best is not None and gap <= tol_ms:
            cands.remove(best)
            pairs.append((b, best))
        else:
            only_bt.append(b)
    only_live = [f for fs in pool.values() for f in fs]
    return pairs, only_bt, only_live


def _equity_at(points: list[tuple[int, float]], ts: int) -> float | None:
    best = None
    for t, v in points:
        if t <= ts:
            best = v
        else:
            break
    return best


def _hold_pnl(side: str, qty: float, price: float, end: float | None) -> float:
    """按持有到期末估算一笔成交的盈亏（买入为正向敞口）。"""
    if end is None or not price:
        return 0.0
    return _sign(side) * (end - price) * qty


def decompose(
    *, backtest: dict[str, Any], live: dict[str, Any], end_prices: dict[str, float], bar_ms: int, model_slip_bps: float
) -> dict[str, Any]:
    """一路实时实验 vs 回测：配对、拆解、校准样本。纯函数，便于测试。

    backtest = {fills, equity: [(ts, value)]}；live = {fills, orders, equity: [(ts, value)], label, execution}
    """
    bt_fills = backtest.get("fills") or []
    lv_fills = live.get("fills") or []
    pairs, only_bt, only_live = match_fills(bt_fills, lv_fills, tol_ms=bar_ms)
    comp = {k: 0.0 for k, _ in COMPONENTS}
    rows: list[dict[str, Any]] = []
    slips: list[float] = []
    lat_ms: list[float] = []
    fee_n = fee_v = 0.0
    partial_n = 0
    for b, lf in pairs:
        s = _sign(b["side"])
        bq, lq = _f(b["amount"]), _f(lf["amount"])
        bp, lp = _f(b["price"]), _f(lf["price"])
        bm = _f(b.get("market_price"), bp)
        lm = _f(lf.get("market_price"), lp)
        q = min(bq, lq)
        latency = -s * (lm - bm) * q
        slippage = -s * ((lp - lm) - (bp - bm)) * q
        bfee = _f(b.get("fee_value") or b.get("fee"))
        lfee = _f(lf.get("fee_value") or lf.get("fee"))
        fees = -(lfee - bfee * (lq / bq if bq else 1))
        part = 0.0
        if lq < bq * 0.999:
            part = -_hold_pnl(b["side"], bq - lq, bp, end_prices.get(b["security"]))
            partial_n += 1
        elif lq > bq * 1.001:
            comp["other"] += _hold_pnl(lf["side"], lq - bq, lp, end_prices.get(lf["security"]))
        comp["latency"] += latency
        comp["slippage"] += slippage
        comp["fees"] += fees
        comp["partials"] += part
        if lm:
            slips.append(s * (lp / lm - 1) * 10_000)
        if lf.get("decided_at") and lf.get("ts"):
            lat_ms.append(max(0, int(lf["ts"]) - int(lf["decided_at"])))
        fee_n += lq * lp
        fee_v += lfee
        rows.append(
            {
                "security": b["security"],
                "side": b["side"],
                "decided_at": _decided(b),
                "bt": {"ts": b.get("ts"), "qty": bq, "price": bp, "market": bm, "fee": bfee},
                "live": {"ts": lf.get("ts"), "qty": lq, "price": lp, "market": lm, "fee": lfee},
                "latency": round(latency, 4),
                "slippage": round(slippage, 4),
                "fees": round(fees, 4),
                "partials": round(part, 4),
            }
        )
    rejects = [o for o in live.get("orders") or [] if o.get("status") == "rejected"]
    used: set[int] = set()
    rejected_n = 0
    missed: list[dict[str, Any]] = []
    for b in only_bt:
        hit = next(
            (
                i
                for i, o in enumerate(rejects)
                if i not in used
                and o.get("security") == b["security"]
                and o.get("side") == b["side"]
                and abs(int(o.get("decided_at") or o.get("submitted_at") or 0) - _decided(b)) <= 2 * bar_ms
            ),
            None,
        )
        pnl = _hold_pnl(b["side"], _f(b["amount"]), _f(b["price"]), end_prices.get(b["security"]))
        saved_fee = _f(b.get("fee_value") or b.get("fee"))
        if hit is not None:
            used.add(hit)
            rejected_n += 1
            comp["rejections"] += -(pnl - saved_fee)
            rows.append(
                {
                    "security": b["security"],
                    "side": b["side"],
                    "decided_at": _decided(b),
                    "bt": {"ts": b.get("ts"), "qty": _f(b["amount"]), "price": _f(b["price"])},
                    "live": None,
                    "rejected": rejects[hit].get("message") or "交易所拒单",
                    "rejections": round(-(pnl - saved_fee), 4),
                }
            )
        else:
            comp["other"] += -(pnl - saved_fee)
            missed.append(b)
    for lf in only_live:
        comp["other"] += _hold_pnl(lf["side"], _f(lf["amount"]), _f(lf["price"]), end_prices.get(lf["security"])) - _f(
            lf.get("fee_value") or lf.get("fee")
        )

    lv_eq = sorted((int(t), float(v)) for t, v in live.get("equity") or [])
    bt_eq = sorted((int(t), float(v)) for t, v in backtest.get("equity") or [])
    end_ts = min(lv_eq[-1][0], bt_eq[-1][0]) if lv_eq and bt_eq else 0
    lv_end = _equity_at(lv_eq, end_ts) if end_ts else None
    bt_end = _equity_at(bt_eq, end_ts) if end_ts else None
    total = (lv_end - bt_end) if lv_end is not None and bt_end is not None else sum(comp.values())
    explained = sum(v for k, v in comp.items() if k != "other")
    comp["other"] = total - explained
    start_eq = bt_eq[0][1] if bt_eq else _f(backtest.get("initial_cash"), 1.0)
    orders = len(live.get("orders") or [])
    n_total = len(pairs) + rejected_n
    return {
        "label": live.get("label") or "",
        "execution": live.get("execution") or "paper",
        "session_id": live.get("session_id") or "",
        "end_ts": end_ts,
        "live_end": lv_end,
        "backtest_end": bt_end,
        "total": round(total, 4),
        "total_bps": round(total / start_eq * 10_000, 2) if start_eq else None,
        "components": [
            {
                "key": k,
                "label": lbl,
                "value": round(comp[k], 4),
                "bps": round(comp[k] / start_eq * 10_000, 2) if start_eq else None,
            }
            for k, lbl in COMPONENTS
        ],
        "matched": len(pairs),
        "only_backtest": len(only_bt),
        "only_live": len(only_live),
        "missed": len(missed),
        "rows": sorted(rows, key=lambda r: r["decided_at"])[-300:],
        "sample": {
            "slippage_bps": slips,
            "latency_ms": lat_ms,
            "fee_notional": fee_n,
            "fee_value": fee_v,
            "fills": len(pairs),
            "rejected": rejected_n,
            "partials": partial_n,
            "orders": orders,
            "attempts": n_total,
            "model_slip_bps": model_slip_bps,
        },
    }


def calibrate(parts: list[dict[str, Any]], cost: dict[str, Any]) -> dict[str, Any]:
    """用交易所模拟盘的实际成交给回测成本模型出建议值。纸面模拟用的就是回测模型，不参与校准。"""
    demo = [p["sample"] for p in parts if p.get("execution") == "demo"]
    slips = [x for s in demo for x in s["slippage_bps"]]
    lat = [x for s in demo for x in s["latency_ms"]]
    notional = sum(s["fee_notional"] for s in demo)
    fees = sum(s["fee_value"] for s in demo)
    attempts = sum(s["attempts"] for s in demo)
    rejected = sum(s["rejected"] for s in demo)
    partials = sum(s["partials"] for s in demo)
    spread = _f(cost.get("spread_bps"), 2.0)
    cur = {
        "taker_fee": _f(cost.get("taker_fee"), 0.001),
        "slippage_bps": _f(cost.get("slippage_bps"), 5.0),
        "spread_bps": spread,
    }
    n = len(slips)
    if not n:
        return {
            "samples": 0,
            "current": cur,
            "suggested": None,
            "confidence": "none",
            "note": "还没有交易所模拟盘成交，无法校准。先用同一策略跑一段模拟盘。",
        }
    # 实际滑点（相对送单市价）已经包含一半价差；回测模型 = 价差/2 + 滑点
    obs = statistics.median(slips)
    p75 = sorted(slips)[min(n - 1, int(n * 0.75))]
    sugg_slip = round(max(0.0, obs - spread / 2), 2)
    sugg_fee = round(fees / notional, 6) if notional > 0 else cur["taker_fee"]
    return {
        "samples": n,
        "current": cur,
        "observed": {
            "slippage_bps_median": round(obs, 2),
            "slippage_bps_p75": round(p75, 2),
            "fee_rate": round(fees / notional, 6) if notional > 0 else None,
            "latency_ms_median": int(statistics.median(lat)) if lat else None,
            "reject_rate": round(rejected / attempts, 4) if attempts else 0.0,
            "partial_rate": round(partials / max(1, n), 4),
        },
        "suggested": {"taker_fee": sugg_fee, "slippage_bps": sugg_slip, "spread_bps": spread},
        "confidence": "high" if n >= 50 else "medium" if n >= 20 else "low",
        "note": ("样本还少，建议值只作参考" if n < 20 else "按中位数校准；想保守一点可以用 75 分位")
        + f"（{n} 笔模拟盘成交）",
    }


def summarize(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """真实度偏差：实时 vs 回测的收益差占初始资金的比例（绝对值），给毕业闸门用。"""
    out: dict[str, Any] = {}
    for p in parts:
        bps = p.get("total_bps")
        out[p["execution"]] = {
            "deviation_bps": abs(bps) if bps is not None else None,
            "total_bps": bps,
            "matched": p["matched"],
            "session_id": p.get("session_id"),
        }
    return out


# ---------------------------------------------------------------------------
# 和平台连起来：取实时实验、重跑回测、存报告
# ---------------------------------------------------------------------------
def _session_live(row: dict[str, Any]) -> dict[str, Any]:
    demo = row.get("demo") or {}
    return {
        "session_id": row["id"],
        "label": f"{'交易所模拟盘' if row.get('execution') == 'demo' else '纸面模拟'} · {row.get('strategy_name')}",
        "execution": row.get("execution") or "paper",
        "fills": row.get("fills") or [],
        "orders": demo.get("orders") or [],
        "equity": [(p["t"], p["v"]) for p in row.get("equity_points") or []],
    }


def window_bars(
    services: Any, row: dict[str, Any], secs: list[str], tf: str, warm: int, end: int
) -> tuple[dict[str, list], list[str]]:
    """实验跑过的那段行情（含预热）。演示行情用同一个时间函数重建，真实行情从交易所取（有缓存）。"""
    from ..data.quality import TIMEFRAME_MS
    from .live import clock_synthetic_bars

    step = TIMEFRAME_MS.get(tf, 3_600_000)
    warnings: list[str] = []
    bars: dict[str, list] = {}
    for sec in secs:
        if row.get("source") == "demo":
            bars[sec] = clock_synthetic_bars(sec, tf, warm, end)
            continue
        ds = services.market.dataset(
            sec,
            tf,
            limit=int((end - warm) / step) + 10,
            exchange=row.get("exchange") or "binance",
            start=warm,
            end=end,
            source="exchange",
        )
        bars[sec] = ds.bars
        if ds.coverage_warning:
            warnings.append(ds.coverage_warning)
    return bars, warnings


def counterpart_backtest(
    services: Any, row: dict[str, Any], manifest: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, float], list[str]]:
    """用实时实验冻结的代码、参数、成本，在它实际跑过的那段行情上重跑回测。"""
    from ..data.quality import TIMEFRAME_MS
    from ..platform import isolation
    from ..platform.sandbox_exec import bars_to_wire
    from .live import LIVE_WARMUP_BARS
    from .research import BacktestSpec

    tf = row.get("timeframe") or "1h"
    step = TIMEFRAME_MS.get(tf, 3_600_000)
    secs = row.get("securities") or [row.get("symbol")]
    # 实时实验启动时拿最近 LIVE_WARMUP_BARS 根已收盘 bar 预热，之后每根新收盘 bar 调一次策略；回测照此对齐
    start = int(row.get("warm_until") or row.get("started_at") or 0)
    end = int(row.get("last_bar_ts") or start)
    warm = start - (LIVE_WARMUP_BARS + 2) * step
    bars, warnings = window_bars(services, row, secs, tf, warm, end + step)
    bars = {k: [b for b in v if b.close_time <= end] for k, v in bars.items()}
    timeline = sorted({b.close_time for v in bars.values() for b in v})
    warmup_n = sum(1 for t in timeline if t <= start)
    if warmup_n >= len(timeline):
        raise ValueError("实验还没处理过新的 bar，没有可对比的区间")
    cost = manifest.get("cost") or {}
    spec = BacktestSpec(
        securities=list(secs),
        timeframe=tf,
        start=start,
        end=end,
        warmup_bars=warmup_n,
        initial_cash=float(row.get("initial_cash") or 10_000),
        exchange=row.get("exchange") or "binance",
        taker_fee=_f(cost.get("taker_fee"), 0.001),
        slippage_bps=_f(cost.get("slippage_bps"), 5.0),
        spread_bps=_f(cost.get("spread_bps"), 2.0),
        risk=manifest.get("risk") or {},
    )
    research: dict[str, Any] = {}
    if any(s.upper().endswith(":PERP") for s in secs):
        from .research import load_funding

        funding, _, fw = load_funding(services.market, spec, bars, [])
        research.update(funding)
        warnings.extend(fw)
    body = isolation.run_batch(
        {
            "op": "backtest",
            "research": research,
            "lang": manifest.get("lang") or "python",
            "source": manifest["source"],
            "name": manifest.get("module_name") or "strategy",
            "spec": spec.to_dict(),
            "bars": bars_to_wire(bars),
            "params": manifest.get("params"),
            "run_id": f"fid-{row['id']}",
            "strategy_version_id": "",
            "time_limit": 60.0,
            "max_points": 5000,
            "max_rows": 5000,
        },
        timeout=180,
    )
    end_prices = {s: float(b[-1].close) for s, b in bars.items() if b}
    return body, end_prices, warnings


def build_report(services: Any, session_ids: list[str], *, user: str = "") -> dict[str, Any]:
    from ..data.quality import TIMEFRAME_MS
    from ..store import Collections

    rows = []
    for sid in session_ids:
        r = services.store.get(Collections.PAPER_SESSIONS, sid)
        if r is None:
            raise ValueError(f"实验 {sid} 不存在")
        rows.append(r)
    if not rows:
        raise ValueError("至少选一个实时实验")
    slugs = {r.get("strategy_id") for r in rows}
    if len(slugs) > 1:
        raise ValueError("只能对比同一个策略的实验")
    base = rows[0]
    run = services.runs.get(base["id"])
    if not run or not (run.get("manifest") or {}).get("source"):
        raise ValueError("找不到这个实验冻结的策略代码")
    manifest = run["manifest"]
    if not base.get("fills"):
        raise ValueError("这个实验还没有成交，没法对比。等它跑出几笔成交再来")
    body, end_prices, warnings = counterpart_backtest(services, base, manifest)
    if body.get("errors") and not body.get("fills"):
        raise ValueError(f"重跑回测失败：{str(body['errors'][0])[:200]}")
    bt_fills = body.get("fills") or []
    lv_all = [f for r in rows for f in r.get("fills") or []]
    if any(len(r.get("fills") or []) >= 500 for r in rows) and lv_all:
        # 实验只保留最近 500 笔成交：回测这边也只比较同一段
        first = min(int(f.get("decided_at") or f.get("ts") or 0) for f in lv_all)
        bt_fills = [f for f in bt_fills if int(f.get("decided_at") or f.get("ts") or 0) >= first - 3_600_000]
        warnings.append("实验成交较多，只对比最近 500 笔成交覆盖的那一段")
    bt = {
        "fills": bt_fills,
        "initial_cash": base.get("initial_cash"),
        "equity": list(zip(body.get("timestamps") or [], body.get("equity_curve") or [], strict=False)),
    }
    bar_ms = TIMEFRAME_MS.get(base.get("timeframe") or "1h", 3_600_000)
    cost = manifest.get("cost") or {}
    model_slip = _f(cost.get("slippage_bps"), 5.0) + _f(cost.get("spread_bps"), 2.0) / 2
    parts = [
        decompose(backtest=bt, live=_session_live(r), end_prices=end_prices, bar_ms=bar_ms, model_slip_bps=model_slip)
        for r in rows
    ]
    if len({(r.get("securities") or [r.get("symbol")])[0] for r in rows}) > 1:
        warnings.append("几个实验的标的不完全一样，回测按第一个实验的标的重跑")
    bt_ret = (bt["equity"][-1][1] / bt["equity"][0][1] - 1) if bt["equity"] else None
    rep = {
        "id": f"fid_{uuid.uuid4().hex[:10]}",
        "strategy_id": base.get("strategy_id"),
        "strategy_name": base.get("strategy_name"),
        "sessions": [
            {
                "id": r["id"],
                "execution": r.get("execution") or "paper",
                "started_at": r.get("started_at"),
                "last_update": r.get("last_update"),
                "exchange": r.get("exchange"),
                "source": r.get("source"),
            }
            for r in rows
        ],
        "window": {
            "start": base.get("warm_until") or base.get("started_at"),
            "end": base.get("last_bar_ts") or base.get("last_update"),
            "timeframe": base.get("timeframe"),
        },
        "backtest": {
            "fills": len(bt["fills"]),
            "return": bt_ret,
            "equity": [{"t": t, "v": v} for t, v in bt["equity"][-600:]],
        },
        "parts": [{k: v for k, v in p.items() if k != "sample"} for p in parts],
        "calibration": calibrate(parts, cost),
        "deviation": summarize(parts),
        "warnings": warnings,
        "notes": [
            "拒单与部分成交按「持有到报告期末」估算错过的盈亏，是近似值",
            "纸面模拟和回测用同一套撮合模型，差异主要来自停机、行情推送时间和复利路径",
            "「其他」= 总差异 − 前五项，包括停机错过的信号、多出来的成交和仓位随权益变化的复利效应",
        ],
        "created_at": int(time.time() * 1000),
        "created_by": user,
    }
    services.store.upsert(REPORTS, rep)
    return rep


def apply_calibration(store: Any, *, exchange: str, suggested: dict[str, Any], report_id: str, user: str) -> dict:
    rec = {
        "id": exchange,
        "exchange": exchange,
        **{k: suggested[k] for k in ("taker_fee", "slippage_bps", "spread_bps") if k in suggested},
        "report_id": report_id,
        "updated_at": int(time.time() * 1000),
        "updated_by": user,
    }
    store.upsert(CALIBRATIONS, rec)
    return rec


__all__ = ["COMPONENTS", "apply_calibration", "build_report", "calibrate", "decompose", "match_fills", "summarize"]
