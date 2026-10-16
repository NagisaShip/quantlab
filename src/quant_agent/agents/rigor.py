"""研究纪律：回测前后的自动核验，让每条结论都能追溯到真实数据、明确假设和可复核的计算。

平台在把回测结果交给 Agent 之前先做三件事，结果原样附在工具输出里（Agent 只能解读，不能改写）：

1. **数据核验**（``data_check``）：请求区间 / K 线数 vs 实际取到的起止时间与根数、预热根数、数据新鲜度、
   数据来源。覆盖不足 90%、数据陈旧或来源不一致时标记为 **非正式回测**——可以看，但不能据此判断策略有效性。
2. **指标口径**（``definitions``）：初始资金、手续费 / 滑点 / 价差的单位与单边或往返口径、成交模型、
   基准区间与超额收益公式，便于复核。
3. **一致性检查**（``consistency``）：超额收益与总收益、基准收益能否互相核对；手续费占初始资金的比例；
   零交易时汇总策略用 ``record()`` 记下的各过滤条件通过次数，给出诊断而不是直接进入下一轮调参。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
MIN_COVERAGE = 0.9  # 实际覆盖不足请求区间的 90% → 非正式回测
STALE_BARS = 3  # 没有指定结束时间时，最后一根 K 线落后当前超过 3 根 → 数据不新鲜


def _day(ms: int | None) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def requested_range(project: dict[str, Any]) -> dict[str, Any]:
    """研究者要求的回测区间（毫秒）。没给起点时按 bars_limit 往回推。"""
    step = TF_MS.get(project.get("timeframe") or "1h", 3_600_000)
    now = int(time.time() * 1000)
    end = int(project.get("end") or now)
    start = int(project.get("start") or end - int(project.get("bars_limit") or 1500) * step)
    return {"start": start, "end": end, "bars": max(1, (end - start) // step), "explicit_end": bool(project.get("end"))}


def data_check(project: dict[str, Any], datasets: list[dict[str, Any]], *, warmup_bars: int = 100) -> dict[str, Any]:
    """对照请求区间核验实际数据。返回 {formal, issues, requested, actual}。"""
    tf = project.get("timeframe") or "1h"
    step = TF_MS.get(tf, 3_600_000)
    req = requested_range(project)
    issues: list[str] = []
    actual: list[dict[str, Any]] = []
    now = int(time.time() * 1000)
    if not datasets:
        issues.append("回测没有返回数据摘要，无法核验数据范围")
    sources = set()
    for d in datasets:
        if d.get("kind") == "funding":
            continue
        bars = int(d.get("bars") or 0)
        s, e = d.get("start_ts"), d.get("end_ts")
        sources.add(d.get("source") or "")
        # 预热：起点指定时平台会向前多取 warmup 根；计分区间从请求起点开始
        scored = max(0, bars - (warmup_bars if project.get("start") else 0))
        coverage = scored / req["bars"] if req["bars"] else 0.0
        row = {
            "symbol": d.get("symbol"),
            "source": d.get("source_label") or d.get("source"),
            "bars": bars,
            "scored_bars": scored,
            "start": _day(s),
            "end": _day(e),
            "coverage": round(min(coverage, 1.0), 3),
            "days": round(bars * step / 86_400_000, 1),
        }
        actual.append(row)
        if coverage < MIN_COVERAGE:
            issues.append(
                f"{d.get('symbol')}：请求约 {req['bars']} 根 {tf}（{req['bars'] * step / 86_400_000:.0f} 天），"
                f"实际只有 {scored} 根计分 K 线（约 {scored * step / 86_400_000:.1f} 天，覆盖 {coverage:.0%}）"
            )
        if e and not req["explicit_end"] and now - int(e) > STALE_BARS * step + step:
            lag_h = (now - int(e)) / 3_600_000
            issues.append(f"{d.get('symbol')}：最后一根 K 线是 {_day(e)} UTC，落后当前约 {lag_h:.0f} 小时，数据不新鲜")
        if d.get("source") == "synthetic":
            issues.append(f"{d.get('symbol')}：使用的是合成行情，只能验证流程，不能作为研究结论")
    if len(sources) > 1:
        issues.append("各标的的数据来源不一致")
    formal = not issues
    return {
        "formal": formal,
        "label": "正式回测" if formal else "非正式回测（数据核验未通过，结果不能用于判断策略是否有效）",
        "issues": issues[:8],
        "requested": {
            "start": _day(req["start"]),
            "end": _day(req["end"]),
            "bars": req["bars"],
            "timeframe": tf,
        },
        "actual": actual,
        "warmup_bars": warmup_bars,
    }


def definitions(
    *,
    initial_cash: float,
    taker_fee: float,
    slippage_bps: float,
    spread_bps: float,
    fill_model: str,
    cost_scale: float = 1.0,
) -> dict:
    """本次回测的指标口径（写给 Agent 和研究者复核用）。成本按实际生效值 × 压力倍数。"""
    k = max(0.0, float(cost_scale or 1.0))
    one_side = (taker_fee + (slippage_bps + spread_bps / 2) / 10_000) * k
    scale = f"（已乘成本压力倍数 ×{k:g}）" if k != 1 else ""
    return {
        "initial_cash": f"{initial_cash:,.0f} USDT（计价货币）",
        "fees": f"taker 手续费 {taker_fee * k:.4%} / 每笔成交（单边）{scale}，按成交额计，计价货币扣除",
        "slippage": f"滑点 {slippage_bps * k:g} bps / 每笔成交（单边），叠加半个价差 {spread_bps * k / 2:g} bps{scale}",
        "round_trip_cost": f"单边约 {one_side:.4%}，一次开平仓往返约 {2 * one_side:.4%}",
        "fill_model": fill_model or "下一根 bar 开盘价 ±（半价差 + 滑点）",
        "signal_timing": "策略只在 K 线收盘后看到这根 bar，订单在下一根 bar 成交（不会用到未收盘数据）",
        "stop_rule": "策略代码里的止损是 bar 级判断：按收盘价触发、下一根开盘成交；"
        "跳空越过止损价时按开盘价成交，不保证止损价",
        "total_return": "（期末权益 / 期初权益）− 1，计分区间从预热结束开始",
        "benchmark_return": "同一计分区间内买入持有（多标的等权）的收益",
        "excess_return": "几何超额 =（1 + 总收益）/（1 + 基准收益）− 1，不是简单相减",
        "max_drawdown": "权益曲线从历史最高点的最大回撤（逐 bar）",
        "sharpe": "逐 bar 收益均值 / 标准差 × √(每年 bar 数)，无风险利率按 0",
    }


def consistency(metrics: dict[str, Any] | None, *, initial_cash: float) -> dict[str, Any]:
    """指标之间互相核对；返回 {ok, checks, notes}。"""
    m = metrics or {}
    checks: list[dict[str, Any]] = []
    tr, br, ex = m.get("total_return"), m.get("benchmark_return"), m.get("excess_return")
    if tr is not None and br is not None and ex is not None and (1 + br):
        geo = (1 + tr) / (1 + br) - 1
        ok = abs(geo - ex) < 5e-4
        checks.append(
            {
                "check": "超额收益 = (1+总收益)/(1+基准)−1",
                "ok": ok,
                "detail": f"(1{tr:+.4f})/(1{br:+.4f})−1 = {geo:+.4f}，报告值 {ex:+.4f}；简单差 {tr - br:+.4f}",
            }
        )
    fees = m.get("total_fees")
    if fees is not None and initial_cash:
        checks.append(
            {
                "check": "手续费占初始资金",
                "ok": True,
                "detail": f"{fees:,.2f} USDT，占初始资金 {fees / initial_cash:.2%}",
            }
        )
    trades = m.get("trades")
    if trades is not None and trades < 10:
        checks.append(
            {
                "check": "交易笔数",
                "ok": False,
                "detail": f"只有 {trades} 笔完整交易，统计意义不足，夏普、胜率等不具说服力",
            }
        )
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def zero_trade_diagnosis(result: dict[str, Any]) -> dict[str, Any] | None:
    """零交易：汇总 record() 记下的条件计数，指出卡在哪一环。没有记录时给出补记录的写法。"""
    m = result.get("metrics") or {}
    if (m.get("trades") or 0) > 0 or (m.get("fills") or 0) > 0:
        return None
    records = result.get("records") or {}
    counts: dict[str, Any] = {}
    for key, points in list(records.items())[:20]:
        vals = [p.get("v") if isinstance(p, dict) else p for p in points or []]
        counts[key] = {
            "bars": len(vals),
            "true_or_positive": sum(1 for v in vals if isinstance(v, (int, float)) and v > 0),
        }
    if counts:
        hint = "各条件为真的 bar 数见 counts：先找出把信号全部过滤掉的那一环，只修改那一个条件再回测。"
    else:
        hint = (
            "策略没有记录诊断数据。请在 handle_bar 里用 record(raw_signal=1/0, filter_trend=1/0, ...) "
            "逐个记录原始信号与每个过滤条件是否通过，再回测一次定位原因；在定位之前不要同时修改多个条件。"
        )
    return {
        "zero_trades": True,
        "counts": counts,
        "hint": hint,
        "caution": "零交易只说明当前条件组合没有产生交易，不能单独证明市场处于震荡、也不能证明策略类型不适用",
    }


#: 写进系统提示的研究纪律（所有研究室共用）
RIGOR_RULES = """
【研究纪律（必须遵守）】
1. 回测前后核对数据：工具输出里的 data_check 写明请求区间、实际起止时间、K 线根数与覆盖率。
   data_check.formal=false 时，这次结果只是「非正式回测」：必须在回答里明确标注，并说明缺口；
   不得据此判断策略是否有效、市场处于什么状态。数据不足时先用 sync_market_data 同步数据再回测。
2. 指标要可复核：引用收益、超额、手续费时同时写出口径（见 definitions）——初始资金、费率是单边还是往返、
   超额是几何超额。consistency 里有不通过的检查要指出来。
3. 一次只改一类变量：每次 write_strategy 都要填 hypothesis（修改假设）、change_type（entry/exit/filter/
   risk/params/other 只选一个）与 expected（预期影响）；回测后对照预期说明实际结果。
   同时改多处逻辑会无法归因，禁止这样做。
4. 零交易或异常结果先诊断：看 zero_trade_diagnosis 定位是哪个条件过滤掉了全部信号，再只改那一处；
   零交易不能证明市场处于震荡或策略类型无效。
5. 区分事实、假设与无依据的结论：结论里用【事实】标注可由数据 / 代码 / 计算直接验证的内容，
   【假设】标注合理但未验证的解释，没有数据支持的收益或风险预测不要写；证据不足时直说「无法判断」。
6. 防止过拟合：研究目标（夏普、回撤等）是目标而不是承诺，允许不达标；不要为了达标在同一段样本上反复调参。
   结论前至少做一次样本外检查（walk_forward 分段或 holdout），并给出 2 倍成本下的结果。
7. 仓位、止损距离、单笔账户风险是三件事：说明风险时分别给出，不要用「仓位不超过 X%」代替账户风险。
""".strip()
