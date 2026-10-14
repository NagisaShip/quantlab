"""策略 SDK 契约（对应 plan.md 第 5 章 + 增补设计第 3.3 节）。

策略只能通过 ctx 访问时钟、数据与状态，输出目标仓位或信号，禁止直接下单。
框架负责把目标仓位转为满足最小订单要求的意图。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..exchanges.base import Bar, Fill


class StrategyContext:
    """策略运行期唯一可用的外部接口（时钟 / 数据 / 状态 / 仓位）。"""

    def __init__(self, run_id: str, initial_cash: float = 10_000.0) -> None:
        self.run_id = run_id
        self.now: int = 0  # 当前虚拟时钟(ms)
        self.cash = initial_cash
        self.position = 0.0  # 当前持仓(币)
        self._target_position = 0.0
        self._state: dict[str, Any] = {}
        self._bars: dict[str, list[Bar]] = {}
        self._fills: list[Fill] = []

    # --- 时钟 ---
    def set_time(self, ts: int) -> None:
        self.now = ts

    # --- 数据访问 ---
    def load_history(self, symbol: str, bars: list[Bar]) -> None:
        self._bars[symbol] = bars

    def history(self, symbol: str, n: int) -> list[Bar]:
        """返回截至当前时钟、已收盘的最近 n 根 bar（不含未来数据）。"""
        bars = [b for b in self._bars.get(symbol, []) if b.close_time <= self.now and b.closed]
        return bars[-n:]

    # --- 状态（策略自定义持久化）---
    def get_state(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self._state[key] = value

    # --- 输出：目标仓位 / 信号 ---
    def set_target_position(self, target: float) -> None:
        """target: 按名义金额或 -1..1 比例。框架后续转订单意图。"""
        self._target_position = target

    @property
    def target_position(self) -> float:
        return self._target_position

    def record_fill(self, fill: Fill) -> None:
        self._fills.append(fill)


@runtime_checkable
class Strategy(Protocol):
    """策略实现契约。Agent 生成的代码必须实现这些方法。"""

    def initialize(self, ctx: StrategyContext) -> None: ...
    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None: ...
    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None: ...
    def on_timer(self, ctx: StrategyContext) -> None: ...
    def snapshot(self, ctx: StrategyContext) -> dict: ...
    def restore(self, ctx: StrategyContext, state: dict) -> None: ...


class StrategyBase:
    """可选基类，提供默认空实现，方便用户/Agent 只覆写需要的方法。

    刻意写成普通类而不是 dataclass：子类用 `name = "ma_trend"` 这样的类属性
    声明模板名，dataclass 的字段默认值会把实例属性覆盖成默认值，导致模板名丢失。
    """

    name = "unnamed"
    label = "未命名"
    description = ""
    defaults: dict[str, Any] = {}
    param_schema: list[dict[str, Any]] = []

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})

    def initialize(self, ctx: StrategyContext) -> None:
        pass

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        pass

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        pass

    def on_timer(self, ctx: StrategyContext) -> None:
        pass

    def snapshot(self, ctx: StrategyContext) -> dict:
        return {}

    def restore(self, ctx: StrategyContext, state: dict) -> None:
        pass


# 沙箱红线：这些方法在策略进程中被禁用
FORBIDDEN_IN_STRATEGY = ("place_order", "cancel_order", "withdraw", "http", "socket", "requests")
