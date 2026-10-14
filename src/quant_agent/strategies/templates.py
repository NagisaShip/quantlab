"""预置策略模板（对应 plan.md 第 5 章的一期策略模板）。

五项：均线趋势、均值回归、有限仓位网格、定投再平衡、买入持有基线。
全部只输出目标仓位，不直接下单。

每个模板带 `label / description / defaults / param_schema`，供前端策略工作台
自动渲染参数表单，避免前后端字段漂移。
"""

from __future__ import annotations

from typing import Any

from ..exchanges.base import Bar
from .sdk import StrategyBase, StrategyContext


class MovingAverageTrend(StrategyBase):
    """均线趋势：快线上穿慢线做多，下穿平仓。收盘确认 + 仓位上限 + 止损。"""

    name = "ma_trend"
    label = "均线趋势"
    description = "快慢均线交叉的趋势跟随策略，收盘确认、含止损与仓位上限。"
    defaults = {"fast": 10, "slow": 30, "max_position": 1.0, "stop_loss": 0.08}
    param_schema = [
        {"key": "fast", "label": "快线周期", "type": "int", "default": 10, "min": 2, "max": 200},
        {"key": "slow", "label": "慢线周期", "type": "int", "default": 30, "min": 5, "max": 400},
        {"key": "max_position", "label": "仓位上限", "type": "float", "default": 1.0, "min": 0.05, "max": 1.0},
        {"key": "stop_loss", "label": "止损比例", "type": "float", "default": 0.08, "min": 0.01, "max": 0.5},
    ]

    def initialize(self, ctx: StrategyContext) -> None:
        p = {**self.defaults, **self.params}
        self.fast = int(p["fast"])
        self.slow = int(p["slow"])
        self.max_pos = float(p["max_position"])
        self.stop_loss = float(p["stop_loss"])
        self.entry_price: float | None = None

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        bars = ctx.history(bar.symbol, self.slow)
        if len(bars) < self.slow:
            return
        closes = [b.close for b in bars]
        fast_ma = sum(closes[-self.fast :]) / self.fast
        slow_ma = sum(closes) / self.slow

        if self.entry_price is not None and bar.close < self.entry_price * (1 - self.stop_loss):
            ctx.set_target_position(0.0)
            self.entry_price = None
            return

        if fast_ma > slow_ma and ctx.target_position == 0.0:
            ctx.set_target_position(self.max_pos)
            self.entry_price = bar.close
        elif fast_ma < slow_ma and ctx.target_position > 0.0:
            ctx.set_target_position(0.0)
            self.entry_price = None


class MeanReversion(StrategyBase):
    """均值回归：价格偏离均值超阈值入场，回归或超时/止损离场。"""

    name = "mean_reversion"
    label = "均值回归"
    description = "价格偏离滚动均值达到阈值时买入，回归均值、超时或触发止损时离场。"
    defaults = {"window": 20, "deviation": 0.03, "max_hold_bars": 48, "stop_loss": 0.06}
    param_schema = [
        {"key": "window", "label": "均值窗口", "type": "int", "default": 20, "min": 5, "max": 200},
        {"key": "deviation", "label": "入场偏离阈值", "type": "float", "default": 0.03, "min": 0.005, "max": 0.3},
        {"key": "max_hold_bars", "label": "最长持有 bar 数", "type": "int", "default": 48, "min": 1, "max": 500},
        {"key": "stop_loss", "label": "止损比例", "type": "float", "default": 0.06, "min": 0.01, "max": 0.5},
    ]

    def initialize(self, ctx: StrategyContext) -> None:
        p = {**self.defaults, **self.params}
        self.window = int(p["window"])
        self.deviation = float(p["deviation"])
        self.max_hold = int(p["max_hold_bars"])
        self.stop_loss = float(p["stop_loss"])
        self.entry_price: float | None = None
        self.hold_bars = 0

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        bars = ctx.history(bar.symbol, self.window)
        if len(bars) < self.window:
            return
        mean = sum(b.close for b in bars) / self.window

        if ctx.target_position > 0.0:
            self.hold_bars += 1
            if (
                bar.close >= mean
                or self.hold_bars >= self.max_hold
                or (self.entry_price and bar.close < self.entry_price * (1 - self.stop_loss))
            ):
                ctx.set_target_position(0.0)
                self.hold_bars = 0
        elif bar.close < mean * (1 - self.deviation):
            ctx.set_target_position(1.0)
            self.entry_price = bar.close
            self.hold_bars = 0


class GridBounded(StrategyBase):
    """有限仓位网格：价格边界 + 库存上限 + 越界暂停。"""

    name = "grid_bounded"
    label = "有限仓位网格"
    description = "在价格区间内网格化加减仓，设库存上限与越界暂停，验证手续费敏感性。"
    defaults = {"span": 0.3, "grids": 10, "max_inventory": 1.0}
    param_schema = [
        {"key": "span", "label": "区间半宽（±比例）", "type": "float", "default": 0.3, "min": 0.05, "max": 0.8},
        {"key": "grids", "label": "网格数量", "type": "int", "default": 10, "min": 2, "max": 100},
        {"key": "max_inventory", "label": "最大库存比例", "type": "float", "default": 1.0, "min": 0.05, "max": 1.0},
    ]

    def initialize(self, ctx: StrategyContext) -> None:
        p = {**self.defaults, **self.params}
        ref = float(p.get("reference_price", 60_000.0))
        span = float(p["span"])
        self.lower = float(p.get("lower", ref * (1 - span)))
        self.upper = float(p.get("upper", ref * (1 + span)))
        self.grids = int(p["grids"])
        self.max_inventory = float(p["max_inventory"])
        self.step = max((self.upper - self.lower) / self.grids, 1e-9)
        self._anchor: float | None = None

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        price = bar.close
        if price < self.lower or price > self.upper:
            ctx.set_target_position(0.0)  # 越界暂停
            return
        if self._anchor is None:
            self._anchor = price
            return
        delta = price - self._anchor
        target = min(self.max_inventory, max(0.0, -delta / self.step * 0.1))
        ctx.set_target_position(target)


class DCAandRebalance(StrategyBase):
    """定投与再平衡：固定周期投入，偏离阈值再平衡。投入流水独立处理。"""

    name = "dca_rebalance"
    label = "定投与再平衡"
    description = "按固定周期投入并按目标比例再平衡，作为组合与基线策略。"
    defaults = {"contribution": 100.0, "interval_bars": 24, "rebalance_threshold": 0.1, "target_ratio": 0.5}
    param_schema = [
        {"key": "contribution", "label": "每期投入金额", "type": "float", "default": 100.0, "min": 1, "max": 1e6},
        {"key": "interval_bars", "label": "投入间隔（bar）", "type": "int", "default": 24, "min": 1, "max": 2000},
        {"key": "rebalance_threshold", "label": "再平衡阈值", "type": "float", "default": 0.1, "min": 0.01, "max": 0.5},
        {"key": "target_ratio", "label": "目标现货占比", "type": "float", "default": 0.5, "min": 0.0, "max": 1.0},
    ]

    def initialize(self, ctx: StrategyContext) -> None:
        p = {**self.defaults, **self.params}
        self.contribution = float(p["contribution"])
        self.interval_bars = max(1, int(p["interval_bars"]))
        self.rebalance_threshold = float(p["rebalance_threshold"])
        self.target_ratio = float(p["target_ratio"])
        self._bar_count = 0

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        self._bar_count += 1
        if self._bar_count % self.interval_bars == 0:
            ctx.set_target_position(self.target_ratio)


class BuyAndHold(StrategyBase):
    """买入持有基线：同币种、同区间、同初始资金，作为统一对照。"""

    name = "buy_hold"
    label = "买入持有"
    description = "开仓后一直持有，作为统一比较基准（同币种、同区间、同初始资金）。"
    defaults = {"ratio": 1.0}
    param_schema = [
        {"key": "ratio", "label": "买入比例", "type": "float", "default": 1.0, "min": 0.05, "max": 1.0},
    ]

    def initialize(self, ctx: StrategyContext) -> None:
        p = {**self.defaults, **self.params}
        self.ratio = float(p["ratio"])

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if ctx.target_position == 0.0:
            ctx.set_target_position(self.ratio)


class CashBaseline(StrategyBase):
    """全现金基线：永远空仓，用于计算超额收益的分母。"""

    name = "cash"
    label = "全现金"
    description = "始终空仓，作为超额收益计算的分母。"
    defaults: dict[str, Any] = {}
    param_schema: list[dict[str, Any]] = []

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        ctx.set_target_position(0.0)


STRATEGY_REGISTRY: dict[str, type[StrategyBase]] = {
    MovingAverageTrend.name: MovingAverageTrend,
    MeanReversion.name: MeanReversion,
    GridBounded.name: GridBounded,
    DCAandRebalance.name: DCAandRebalance,
    BuyAndHold.name: BuyAndHold,
    CashBaseline.name: CashBaseline,
}


def describe_templates() -> list[dict[str, Any]]:
    """给前端的模板清单（含参数 schema，供自动渲染表单）。"""
    out = []
    for name, cls in STRATEGY_REGISTRY.items():
        out.append(
            {
                "name": name,
                "label": getattr(cls, "label", name),
                "description": getattr(cls, "description", ""),
                "defaults": dict(getattr(cls, "defaults", {})),
                "param_schema": list(getattr(cls, "param_schema", [])),
            }
        )
    return out


def build_strategy(name: str, params: dict[str, Any] | None = None) -> StrategyBase:
    cls = STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"未知策略模板: {name}")
    strategy = cls(params=params or {})
    strategy.name = name  # 用注册键作为实例名，保证回测记录与模板一致
    return strategy
