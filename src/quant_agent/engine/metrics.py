"""统计指标（对应 plan.md 第 7 章）。

所有比率在零波动/零回撤时返回 None（界面显示"不适用"），
而不是伪造一个数字——这是原方案的明确要求。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# 加密市场常用：日频 Sharpe 用 sqrt(365) 年化（原方案第 7 章）。
# 本模块按"每年 bar 数"参数化，由调用方按周期给出。
BARS_PER_YEAR = {
    "1m": 525_600,
    "5m": 105_120,
    "15m": 35_040,
    "1h": 8_760,
    "4h": 2_190,
    "1d": 365,
}


@dataclass
class Metrics:
    """回测/模拟指标。收益类为小数（0.08 = 8%）。"""

    observations: int = 0
    total_return: float = 0.0
    annualized_return: float | None = None
    max_drawdown: float = 0.0
    max_drawdown_duration: int = 0
    recovery_bars: int | None = None
    volatility: float | None = None
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None
    win_rate: float | None = None
    profit_factor: float | None = None
    payoff_ratio: float | None = None
    turnover: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    final_equity: float = 0.0
    initial_equity: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__


def _safe_div(a: float, b: float) -> float | None:
    if b == 0 or not math.isfinite(b):
        return None
    return a / b


def compute_metrics(
    equity_curve: list[float],
    *,
    timeframe: str = "1h",
    risk_free_rate: float = 0.0,
    trade_returns: list[float] | None = None,
    turnover: float = 0.0,
) -> Metrics:
    """从权益曲线计算指标。

    equity_curve: 每个 bar 末的组合权益（含未实现盈亏）。
    trade_returns: 每笔平仓交易的收益率，用于胜率/盈亏比/利润因子。
    """
    m = Metrics()
    n = len(equity_curve)
    m.observations = n
    if n == 0:
        m.notes.append("无数据")
        return m

    m.initial_equity = equity_curve[0]
    m.final_equity = equity_curve[-1]
    m.total_return = _safe_div(m.final_equity - m.initial_equity, m.initial_equity) or 0.0
    m.turnover = turnover

    # --- 逐 bar 收益 ---
    rets: list[float] = []
    for prev, cur in zip(equity_curve, equity_curve[1:], strict=False):
        r = _safe_div(cur - prev, prev)
        if r is not None:
            rets.append(r)

    if rets:
        mean_r = sum(rets) / len(rets)
        var = sum((r - mean_r) ** 2 for r in rets) / len(rets)
        std = math.sqrt(var)
        bars_per_year = BARS_PER_YEAR.get(timeframe, 8_760)
        m.volatility = std * math.sqrt(bars_per_year)

        # 年化收益：用几何年化，避免短区间算术年化夸大
        if m.initial_equity > 0 and m.final_equity > 0 and n > 1:
            years = n / bars_per_year
            if years > 0:
                m.annualized_return = (m.final_equity / m.initial_equity) ** (1 / years) - 1
                if n < bars_per_year:
                    m.notes.append(
                        f"观测 {n} 根 {timeframe} bar（不足一年），年化收益可能夸大，"
                        f"请同时参考累计收益 {m.total_return:.2%}"
                    )

        rf_per_bar = risk_free_rate / bars_per_year
        if std > 0:
            m.sharpe = (mean_r - rf_per_bar) / std * math.sqrt(bars_per_year)
        else:
            m.notes.append("波动为零，Sharpe 不适用")

        downside = [r for r in rets if r < rf_per_bar]
        if downside:
            dstd = math.sqrt(sum((r - rf_per_bar) ** 2 for r in downside) / len(downside))
            if dstd > 0:
                m.sortino = (mean_r - rf_per_bar) / dstd * math.sqrt(bars_per_year)
        else:
            m.notes.append("无下行波动，Sortino 不适用")

    # --- 回撤 ---
    peak = equity_curve[0]
    peak_idx = 0
    max_dd = 0.0
    dd_start = 0
    max_dd_len = 0
    recovery: int | None = None
    for i, e in enumerate(equity_curve):
        if e > peak:
            peak = e
            peak_idx = i
        dd = _safe_div(peak - e, peak)
        if dd is not None and dd > max_dd:
            max_dd = dd
            dd_start = peak_idx
            max_dd_len = i - peak_idx
            recovery = None
        elif recovery is None and max_dd > 0 and e >= equity_curve[dd_start]:
            recovery = i - dd_start
    m.max_drawdown = max_dd
    m.max_drawdown_duration = max_dd_len
    m.recovery_bars = recovery
    if max_dd > 0:
        m.calmar = _safe_div(m.annualized_return or 0.0, max_dd)
    else:
        m.notes.append("无回撤，Calmar 不适用")

    # --- 交易统计 ---
    if trade_returns:
        wins = [r for r in trade_returns if r > 0]
        losses = [r for r in trade_returns if r < 0]
        m.trades = len(trade_returns)
        m.win_rate = _safe_div(len(wins), len(trade_returns))
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0:
            m.profit_factor = gross_win / gross_loss
        avg_win = (sum(wins) / len(wins)) if wins else 0.0
        avg_loss = (abs(sum(losses)) / len(losses)) if losses else 0.0
        m.payoff_ratio = _safe_div(avg_win, avg_loss)
        # 连续亏损
        streak = best = 0
        for r in trade_returns:
            streak = streak + 1 if r < 0 else 0
            best = max(best, streak)
        m.consecutive_losses = best

    return m


def drawdown_series(equity_curve: list[float]) -> list[float]:
    """水下曲线（正数表示回撤幅度）。"""
    out: list[float] = []
    peak = equity_curve[0] if equity_curve else 0.0
    for e in equity_curve:
        peak = max(peak, e)
        out.append((peak - e) / peak if peak else 0.0)
    return out


def drawdown_details(values: list[float], timestamps: list[int] | None = None) -> dict:
    """最大回撤的完整描述：峰值 / 谷值 / 恢复点（下标与时间）、是否已恢复、持续与恢复长度。

    黄金样例：[100, 110, 88, 99, 121] → 最大回撤 20%，峰值下标 1，谷值下标 2，恢复下标 4。
    恢复定义为净值重新**达到或超过**回撤前峰值的第一个点；期末仍未达到则 recovered=False。
    """
    out: dict = {
        "max_drawdown": 0.0,
        "peak_index": None,
        "trough_index": None,
        "recovery_index": None,
        "peak_ts": None,
        "trough_ts": None,
        "recovery_ts": None,
        "recovered": None,
        "drawdown_bars": 0,
        "recovery_bars": None,
        "underwater_bars": 0,
    }
    if not values:
        return out
    ts = timestamps if timestamps and len(timestamps) == len(values) else None
    peak_i = 0
    best = (0.0, None, None)
    for i, v in enumerate(values):
        if v > values[peak_i]:
            peak_i = i
        pv = values[peak_i]
        dd = (pv - v) / pv if pv else 0.0
        if dd > best[0]:
            best = (dd, peak_i, i)
    mdd, p_i, t_i = best
    out["max_drawdown"] = mdd
    if p_i is None:
        out["recovered"] = True
        return out
    rec = next((j for j in range(t_i + 1, len(values)) if values[j] >= values[p_i]), None)
    out.update(
        {
            "peak_index": p_i,
            "trough_index": t_i,
            "recovery_index": rec,
            "recovered": rec is not None,
            "drawdown_bars": t_i - p_i,
            "recovery_bars": (rec - t_i) if rec is not None else None,
            "underwater_bars": ((rec if rec is not None else len(values) - 1) - p_i),
        }
    )
    if ts:
        out["peak_ts"], out["trough_ts"] = ts[p_i], ts[t_i]
        out["recovery_ts"] = ts[rec] if rec is not None else None
    return out


def worst_period(daily: list[dict] | None) -> dict | None:
    """最差的一天（按日收益）。"""
    if not daily:
        return None
    row = min(daily, key=lambda d: d.get("return", 0.0))
    return {"date": row.get("date"), "return": row.get("return", 0.0)}


def holding_stats(round_trips: list) -> dict:
    """完整交易（从空仓建仓到卖回空仓）的持仓时长统计；未平仓持仓不计入。"""
    hours = [max(0, t.close_ts - t.open_ts) / 3_600_000 for t in round_trips if getattr(t, "close_ts", 0)]
    if not hours:
        return {"avg_holding_hours": None, "max_holding_hours": None, "completed_trades": 0}
    return {
        "avg_holding_hours": sum(hours) / len(hours),
        "max_holding_hours": max(hours),
        "completed_trades": len(hours),
    }


def fee_summary(
    *,
    by_currency: dict | None = None,
    converted: object = 0,
    unconverted: dict | None = None,
    unconverted_details: list | None = None,
    base_currency: str = "USDT",
) -> dict:
    """总手续费的统一口径（回测报告与实盘对照共用同一形状）。

    口径（文档第 8 章"总手续费：按币种汇总，并折算计价币"）：
    - `by_currency` 是**实际扣费币种**的原始数量，永远给全，不做任何折算；
    - `converted_value` 只含**能拿到成交时刻汇率**的部分，折算到计价币；
    - `unconverted` 是拿不到汇率的部分，按币种单列，**绝不并进 `converted_value`**。

    为什么必须分成两个数：把 BNB 的数量直接加到 USDT 的金额上，等于按 1:1 估算，
    手续费会被系统性低估或高估，而且报告里看不出问题。宁可让使用者看到一个
    "还有 0.01 BNB 没折算"的提示，也不要给一个看起来完整、实际错误的总额。
    """
    unconv = dict(unconverted or {})
    return {
        "base_currency": base_currency,
        "by_currency": {k: str(v) for k, v in sorted((by_currency or {}).items())},
        "converted_value": str(converted),
        "unconverted": {k: str(v) for k, v in sorted(unconv.items())},
        "unconverted_count": len(unconverted_details or []),
        "unconverted_details": list(unconverted_details or []),
        "has_unconverted": bool(unconv),
        "note": (
            "部分手续费以非计价币种支付且缺少成交时刻汇率，未折算进总手续费（不估算、不按 1:1）："
            + "、".join(f"{k} {v}" for k, v in sorted(unconv.items()))
            if unconv
            else ""
        ),
    }


def assumptions_block(
    *,
    timeframe: str,
    risk_free_rate: float = 0.0,
    cost: dict | None = None,
    flows: bool = False,
    warmup_bars: int = 0,
    perps: list[str] | None = None,
    funding_sources: dict[str, str] | None = None,
) -> dict:
    """指标口径声明：采样频率、年化因子、无风险利率、收益基准、撮合与成本假设。报告必须带上它。"""
    bpy = BARS_PER_YEAR.get(timeframe, 8_760)
    return {
        "sampling": timeframe,
        "periods_per_year": bpy,
        "annualization": f"波动率 × √{bpy}；年化收益为几何年化（观测不足一年时可能夸大）",
        "risk_free_rate": risk_free_rate,
        "return_basis": "单位净值（时间加权，剔除出入金影响）" if flows else "权益曲线（无出入金，与单位净值等价）",
        "drawdown_basis": "按同一收益基准逐 bar 计算",
        "fill_model": (cost or {}).get("fill_at", "next_open"),
        "insufficient_funds": (cost or {}).get("insufficient_funds", "reject"),
        "limit_model": "保守 K 线：严格穿越才成交，触价视为歧义不成交",
        "bracket_priority": "同一根 bar 同时触及止损与止盈时，止损优先",
        "open_positions": "期末未平仓按最后价格估值，不计入完成交易、胜率与持仓时长",
        "warmup_bars": warmup_bars,
        "cost": cost or {},
        # ---- 永续合约模型 ----
        # 明确声明哪里是简化的；不声明的话，使用者会把简化结果当成真实交易所行为。
        "perpetual": {
            "supported": True,
            "instruments": perps or [],
            "funding_sources": funding_sources or {},
            "funding_interval_hours": 8,
            "funding_basis": "名义价值 × 费率 = 持仓数量 × 结算点标记价 × 费率",
            "funding_sign": "正费率多头付空头；负费率空头付多头",
            "funding_account": "expenses:funding（与交易手续费 expenses:fees 分开列示）",
            "funding_affects_cost_basis": False,
            "margin_model": "逐仓、单向持仓：开仓占用 名义/杠杆 的保证金（杠杆用 set_leverage 设置，默认 1 倍）",
            "mark_price": (
                "标记价用永续合约自身的 K 线价格（收盘价估值、最高/最低价判断强平），不是交易所的指数加权标记价"
            ),
            "liquidation_model": (
                "每根 bar 用最高/最低价检查强平价（维持保证金率 0.5%）；触及即按强平价平掉全部持仓，"
                "损失全部逐仓保证金（剩余维持保证金视为强平费用没收，比交易所实际结果略保守）"
            ),
            "funding_from_cash": "资金费从现金结算，不从逐仓保证金扣，所以不推动强平价",
            "limitations": (
                "不含阶梯维持保证金率、交易所费率上下限、结算精度取整与资金费的时间加权（首期不足 8 小时按整段计）"
            ),
        },
    }


def compute_relative_metrics(
    equity_curve: list[float],
    benchmark_curve: list[float],
    *,
    timeframe: str = "1h",
    daily: list[dict] | None = None,
) -> dict:
    """相对基准的指标（对标聚宽回测详情的"收益概述"）。

    - 基准收益 / 基准年化 / 基准波动率
    - 超额收益：(1 + 策略收益) / (1 + 基准收益) − 1，与聚宽口径一致
    - Beta = Cov(策略, 基准) / Var(基准)，按逐 bar 收益计算
    - Alpha（年化）= (策略平均收益 − Beta × 基准平均收益) × 每年 bar 数，无风险利率取 0
    - 信息比率 = 平均超额收益 / 超额收益标准差 × √每年 bar 数
    - 日胜率 = 策略日收益高于基准日收益的天数占比；盈利天数占比单独给出

    样本太短或基准零波动时返回 None（界面显示"不适用"），不编造数字。
    """
    out: dict = {
        "benchmark_return": None,
        "benchmark_annualized": None,
        "benchmark_volatility": None,
        "excess_return": None,
        "alpha": None,
        "beta": None,
        "information_ratio": None,
        "tracking_error": None,
        "daily_win_rate": None,
        "profit_day_ratio": None,
        "trading_days": len(daily or []),
    }
    n = min(len(equity_curve), len(benchmark_curve))
    if n < 3 or not benchmark_curve[0]:
        return out
    bpy = BARS_PER_YEAR.get(timeframe, 8_760)
    b0, b1 = benchmark_curve[0], benchmark_curve[n - 1]
    e0, e1 = equity_curve[0], equity_curve[n - 1]
    bench_ret = b1 / b0 - 1
    strat_ret = e1 / e0 - 1 if e0 else 0.0
    out["benchmark_return"] = bench_ret
    out["excess_return"] = (1 + strat_ret) / (1 + bench_ret) - 1 if (1 + bench_ret) else None
    years = n / bpy
    if years > 0 and b1 > 0:
        out["benchmark_annualized"] = (b1 / b0) ** (1 / years) - 1

    rs: list[float] = []
    rb: list[float] = []
    for i in range(1, n):
        if equity_curve[i - 1] and benchmark_curve[i - 1]:
            rs.append(equity_curve[i] / equity_curve[i - 1] - 1)
            rb.append(benchmark_curve[i] / benchmark_curve[i - 1] - 1)
    if len(rb) >= 2:
        mb = sum(rb) / len(rb)
        ms = sum(rs) / len(rs)
        var_b = sum((x - mb) ** 2 for x in rb) / len(rb)
        out["benchmark_volatility"] = math.sqrt(var_b) * math.sqrt(bpy)
        if var_b > 0:
            cov = sum((a - ms) * (b - mb) for a, b in zip(rs, rb, strict=False)) / len(rb)
            beta = cov / var_b
            out["beta"] = beta
            out["alpha"] = (ms - beta * mb) * bpy
        diff = [a - b for a, b in zip(rs, rb, strict=False)]
        md = sum(diff) / len(diff)
        te = math.sqrt(sum((d - md) ** 2 for d in diff) / len(diff))
        if te > 0:
            out["tracking_error"] = te * math.sqrt(bpy)
            out["information_ratio"] = md / te * math.sqrt(bpy)

    if daily:
        beat = sum(1 for d in daily if d.get("return", 0) > d.get("benchmark_return", 0))
        up = sum(1 for d in daily if d.get("return", 0) > 0)
        out["daily_win_rate"] = beat / len(daily)
        out["profit_day_ratio"] = up / len(daily)
    return out
