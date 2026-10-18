"""SDK 与执行引擎之间的契约。

策略代码只依赖 `Runtime` 这个协议；回测引擎、实时模拟引擎各提供一个实现。
这样同一份策略文件不改一个字，就能在回测 / 模拟 / 实盘对照三种模式下跑——
这正是"复用策略、组合和风控逻辑"落地的地方。

Runtime 由引擎实现，SDK 的模块级函数（order、history、log …）通过
`contextvars` 找到"当前正在跑哪个 runtime"，因此支持并发运行多个回测。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class OrderRequest:
    """策略发出的下单请求（已由 SDK 归一化，未做风控与撮合）。"""

    security: str
    side: str  # buy | sell
    amount: float  # 数量（正数）
    reason: str = ""
    target_kind: str = ""  # amount | value | percent
    target_value: float = 0.0
    limit_price: float | None = None  # 限价；None 为市价（下一根开盘）
    expire_bars: int | None = None  # 信号有效 bar 数；None 用运行时默认
    # 限价单：GTC 挂到过期 | IOC 只在下一根开盘能成交就成交、否则撤 | post_only 只做挂单方
    time_in_force: str = "GTC"
    on_expire: str = "cancel"  # 限价单到期：cancel 撤单 | market 改市价 | chase 按开盘价重挂（最多 3 次）
    position_side: str = ""  # hedge 模式：long | short；空表示按 action 推断


@dataclass
class OrderResult:
    """撮合结果。策略通常不关心细节，但 `record` 与日志会用到。"""

    ok: bool
    security: str
    side: str
    filled_amount: float = 0.0
    filled_price: float = 0.0
    fee: float = 0.0
    message: str = ""
    status: str = "filled"  # filled | pending | rejected


@dataclass
class ScheduleEntry:
    """run_daily / run_weekly / run_monthly / run_timer 注册的调度项。"""

    func_name: str
    period: str  # daily | weekly | monthly | timer
    time: str  # open | close | every_bar | HH:MM
    weekday: int | None = None
    monthday: int | None = None
    interval_ms: int = 0  # period == "timer" 时的间隔（毫秒）


@runtime_checkable
class Runtime(Protocol):
    """执行引擎必须实现的能力。"""

    mode: str

    # ---- 数据 ----
    def price(self, security: str) -> float: ...
    def bars(self, security: str, count: int) -> list[dict[str, Any]]: ...
    def securities(self) -> list[str]: ...
    def is_tradable(self, security: str) -> bool: ...

    # ---- 交易 ----
    def submit(self, req: OrderRequest) -> OrderResult: ...
    def can_afford(self, security: str, amount: float, price: float) -> bool: ...

    # ---- 配置 ----
    def set_benchmark(self, security: str) -> None: ...
    def set_order_cost(self, cost: dict[str, float]) -> None: ...
    def set_slippage(self, bps: float) -> None: ...
    def set_option(self, name: str, value: Any) -> None: ...

    # ---- 调度 ----
    def schedule(self, entry: ScheduleEntry) -> None: ...

    # ---- 输出 ----
    def log(self, level: str, message: str) -> None: ...
    def record(self, values: dict[str, Any]) -> None: ...

    # ---- 上下文 ----
    def context(self) -> Any: ...


_current: ContextVar[Runtime | None] = ContextVar("quant_agent_runtime", default=None)


def bind(runtime: Runtime):
    """绑定当前 runtime，返回可用于恢复的 token。"""
    return _current.set(runtime)


def unbind(token) -> None:
    _current.reset(token)


def current() -> Runtime:
    """取当前 runtime。策略在框架外被调用时给出可读的错误。"""
    rt = _current.get()
    if rt is None:
        raise RuntimeError(
            "策略函数必须在框架内运行。请通过平台回测 / 实时模拟触发，或用 quant_agent.sdk.runner 在本地跑一遍。"
        )
    return rt
