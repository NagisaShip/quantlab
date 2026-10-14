"""事件驱动回测引擎（对应 plan.md 第 6 章）。

设计要点：
- 策略只输出目标仓位；引擎负责把目标仓位转成"订单意图"并按撮合规则成交。
- K 线级撮合：以"下一根可交易价格"为基准，叠加估计价差与滑点，限制成交量占比。
- 手续费按 maker/taker 费率与费用币种计入；每笔买卖均扣成本。
- 逐 bar 推进虚拟时钟，策略永远只能看到已收盘 bar。
- 记录每笔交易收益，供指标引擎计算胜率/盈亏比。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..exchanges.base import Bar
from ..strategies.sdk import StrategyBase, StrategyContext
from .metrics import Metrics, compute_metrics, drawdown_series
from .windows import compute_windows


@dataclass
class CostModel:
    """成本模型。默认保守（基准）。压力场景把各项放大。"""

    taker_fee: float = 0.001  # 0.1% 单边
    maker_fee: float = 0.0005
    slippage_bps: float = 5.0  # 5 bps 滑点
    spread_bps: float = 2.0  # 半个价差估计
    max_volume_participation: float = 0.1  # 单根 bar 最多吃掉 10% 成交量

    def scale(self, factor: float) -> CostModel:
        return CostModel(
            taker_fee=self.taker_fee * factor,
            maker_fee=self.maker_fee * factor,
            slippage_bps=self.slippage_bps * factor,
            spread_bps=self.spread_bps * factor,
            max_volume_participation=self.max_volume_participation,
        )


@dataclass
class Trade:
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    gross_return: float
    net_return: float
    fees: float
    reason: str = ""


@dataclass
class BacktestResult:
    run_id: str
    symbol: str
    timeframe: str
    strategy: str
    params: dict[str, Any]
    initial_cash: float
    equity_curve: list[float]
    timestamps: list[int]
    drawdown: list[float]
    trades: list[Trade]
    metrics: Metrics
    cost_model: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    mode: str = "backtest"
    warmup_bars: int = 0
    windows: dict[str, Any] = field(default_factory=dict)
    open_position: dict[str, Any] | None = None

    def to_dict(self, *, max_points: int | None = None) -> dict[str, Any]:
        eq, ts, dd = self.equity_curve, self.timestamps, self.drawdown
        if max_points and len(eq) > max_points:
            step = max(1, len(eq) // max_points)
            eq, ts, dd = eq[::step], ts[::step], dd[::step]
        return {
            "id": self.run_id,
            "run_id": self.run_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy": self.strategy,
            "params": self.params,
            "initial_cash": self.initial_cash,
            "mode": self.mode,
            "warmup_bars": self.warmup_bars,
            "windows": self.windows,
            "metrics": self.metrics.to_dict(),
            "equity_curve": eq,
            "timestamps": ts,
            "drawdown": dd,
            "trades": [t.__dict__ for t in self.trades[:200]],
            "cost_model": self.cost_model,
            "notes": self.notes,
            "open_position": self.open_position,
        }


class BacktestEngine:
    """单品种、全仓目标仓位的事件驱动回测。

    目标仓位 target ∈ [0, 1] 表示"资金投入比例"（现货多头，一期不做空）。
    仓位变化 → 生成买卖订单 → 按成本模型成交。
    """

    def __init__(
        self,
        *,
        initial_cash: float = 10_000.0,
        cost_model: CostModel | None = None,
        min_trade_notional: float = 10.0,
        max_position: float = 1.0,
    ) -> None:
        self.initial_cash = initial_cash
        self.cost = cost_model or CostModel()
        self.min_trade_notional = min_trade_notional
        self.max_position = max_position

    def run(
        self,
        strategy: StrategyBase,
        bars: list[Bar],
        *,
        run_id: str = "run",
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        warmup_bars: int = 0,
    ) -> BacktestResult:
        """跑一次回测。

        `warmup_bars > 0` 时，前 N 根 bar 只用于指标预热，不计入净值与成绩
        （对应原方案第 5 章"指标预热"与实验三的 100 小时预热口径）。
        """
        if not bars:
            raise ValueError("回测需要至少一根 bar")
        warmup = max(0, min(warmup_bars, len(bars) - 2)) if warmup_bars else 0

        ctx = StrategyContext(run_id=run_id, initial_cash=self.initial_cash)
        ctx.load_history(symbol, bars)
        strategy.initialize(ctx)

        cash = self.initial_cash
        position = 0.0  # 持仓数量（币）
        equity_curve: list[float] = []
        timestamps: list[int] = []
        trades: list[Trade] = []
        turnover = 0.0
        notes: list[str] = []
        if warmup:
            notes.append(f"前 {warmup} 根 bar 仅用于指标预热，不计入净值与成绩")

        open_trade: dict[str, Any] | None = None
        last_target = 0.0
        scoring = False  # 预热结束前不记录净值、不成交

        for i, bar in enumerate(bars):
            ctx.set_time(bar.close_time)
            # 策略只能看到截至上一根已收盘 bar 的信息；执行在下一根
            strategy.on_bar(ctx, bar)

            if i < warmup:
                continue  # 预热期：只喂数据，不记账、不成交
            if not scoring:
                # 预热结束的第一根 = 成绩起点：净值记为初始资金，时间戳取预热最后一根的收盘。
                # 这与实验三"points[0] = 预热结束时的初始资金"口径一致。
                scoring = True
                equity_curve.append(self.initial_cash)
                timestamps.append(bars[i - 1].close_time if i > 0 else bar.open_time)

            # 账户快照必须回灌给策略：策略有权看到自己当前的现金与持仓
            ctx.cash = cash
            ctx.position = position

            target = max(0.0, min(self.max_position, ctx.target_position))

            # 用当前 bar 收盘价执行（K 线级近似：以收盘价为可成交价，加成本）
            exec_price = self._exec_price(bar, side="buy" if target > last_target else "sell")
            equity_before = cash + position * bar.close

            if target != last_target and i < len(bars):
                desired_notional = target * (cash + position * exec_price)
                current_notional = position * exec_price
                delta_notional = desired_notional - current_notional
                if abs(delta_notional) >= self.min_trade_notional:
                    qty = delta_notional / exec_price
                    # 成交量占比限制（近似：不超过该 bar 成交量的 10%）
                    max_qty = bar.volume * self.cost.max_volume_participation
                    if max_qty > 0 and abs(qty) > max_qty:
                        qty = math.copysign(max_qty, qty)
                        notes.append(f"bar {i}: 受成交量占比限制，成交被截断")
                    fee_rate = self.cost.taker_fee
                    if qty > 0:
                        # 目标权重法：按“可用现金 ÷ (成交价 × (1+费率))”折算可负担数量（与 SDK 的
                        # order_target_percent 同一规则）；这是按比例定仓，不是透支后缩量。
                        afford = cash / (exec_price * (1 + fee_rate)) if exec_price > 0 else 0.0
                        if qty > afford:
                            qty = afford
                    fee = abs(qty * exec_price) * fee_rate
                    if qty > 0:  # 买入
                        cost = qty * exec_price + fee
                        cash -= cost
                        position += qty
                        if open_trade is None:
                            open_trade = {"ts": bar.close_time, "price": exec_price, "qty": qty, "fees": fee}
                        else:
                            open_trade["fees"] += fee
                            open_trade["qty"] += qty
                            open_trade["price"] = (
                                open_trade["price"] * (open_trade["qty"] - qty) + exec_price * qty
                            ) / open_trade["qty"]
                    else:  # 卖出
                        sell_qty = min(position, abs(qty))
                        proceeds = sell_qty * exec_price - fee
                        cash += proceeds
                        position -= sell_qty
                        if open_trade is not None:
                            gross = (exec_price - open_trade["price"]) / open_trade["price"]
                            total_fees = open_trade["fees"] + fee
                            net = gross - total_fees / (open_trade["price"] * open_trade["qty"])
                            trades.append(
                                Trade(
                                    entry_ts=open_trade["ts"],
                                    exit_ts=bar.close_time,
                                    entry_price=open_trade["price"],
                                    exit_price=exec_price,
                                    qty=open_trade["qty"],
                                    gross_return=gross,
                                    net_return=net,
                                    fees=total_fees,
                                    reason="target_change",
                                )
                            )
                            open_trade = None
                    turnover += abs(qty * exec_price)
                    last_target = target

            equity_after = cash + position * bar.close
            equity_curve.append(equity_after)
            timestamps.append(bar.close_time)
            if equity_before <= 0:
                notes.append("权益归零，回测提前结束")
                break

        # 收尾：未平仓持仓**不**视为完成交易——只按最后价格估值并单独列出，
        # 不进入胜率、盈亏比、交易次数（v0.3 曾把它当作 final_liquidation 计入，已纠正）。
        open_position = None
        if open_trade is not None and bars and position > 0:
            last = bars[min(len(timestamps) - 1, len(bars) - 1)]
            open_position = {
                "entry_ts": open_trade["ts"],
                "entry_price": open_trade["price"],
                "qty": position,
                "mark_price": last.close,
                "unrealized_return": (last.close - open_trade["price"]) / open_trade["price"],
                "fees": open_trade["fees"],
                "note": "未平仓：按最后价格估值，不计入已完成交易",
            }
            notes.append("期末仍有未平仓持仓：已按最后价格估值，未计入交易统计")

        metrics = compute_metrics(
            equity_curve,
            timeframe=timeframe,
            trade_returns=[t.net_return for t in trades],
            turnover=turnover / self.initial_cash if self.initial_cash else 0.0,
        )
        metrics.notes.extend(notes)

        windows = compute_windows(
            equity_curve,
            timestamps,
            initial=self.initial_cash,
            timeframe=timeframe,
            warmup_bars=warmup,
            trades=[t.__dict__ for t in trades],
        )

        return BacktestResult(
            run_id=run_id,
            symbol=symbol,
            timeframe=timeframe,
            strategy=strategy.name,
            params=strategy.params,
            initial_cash=self.initial_cash,
            equity_curve=equity_curve,
            timestamps=timestamps,
            drawdown=drawdown_series(equity_curve),
            trades=trades,
            metrics=metrics,
            cost_model=self.cost.__dict__,
            notes=list(dict.fromkeys(metrics.notes)),
            warmup_bars=warmup,
            windows=windows.to_dict(),
            open_position=open_position,
        )

    def _exec_price(self, bar: Bar, *, side: str) -> float:
        """K 线级撮合近似：收盘价 + 半个价差 + 滑点。买入向上、卖出向下。"""
        cost_bps = self.cost.spread_bps / 2 + self.cost.slippage_bps
        adj = cost_bps / 10_000
        return bar.close * (1 + adj) if side == "buy" else bar.close * (1 - adj)


def stress_scenarios(base: CostModel) -> dict[str, CostModel]:
    """压力场景（对应 plan.md 第 6 章）：成本 1.5x / 2x、流动性下降。"""
    return {
        "baseline": base,
        "cost_1.5x": base.scale(1.5),
        "cost_2x": base.scale(2.0),
        "low_liquidity": CostModel(
            taker_fee=base.taker_fee,
            maker_fee=base.maker_fee,
            slippage_bps=base.slippage_bps * 2,
            spread_bps=base.spread_bps * 3,
            max_volume_participation=base.max_volume_participation / 5,
        ),
    }
