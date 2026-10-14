"""策略 SDK 的运行时对象（对标聚宽的 context / portfolio / position）。

这些对象由框架构造并注入策略，策略**只读**它们。策略想表达仓位，
必须走 `order_*` 系列函数——不允许直接改 portfolio。

分层与聚宽一致：
    Context      当前时刻、账户、标的池、参数、运行模式
    Portfolio    总权益、可用现金、持仓字典、已实现盈亏
    Position     单个标的的持仓数量、成本、市值、浮动盈亏
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .ledger import ZERO, Holding, Ledger, PerpPosition, dstr, is_perp


class ReadOnlyError(AttributeError):
    pass


_RO_MSG = "持仓与账户是只读视图：请用 order / order_target / order_target_percent 等函数表达仓位意图"


class Position:
    """单个标的持仓的**只读视图**。数值以 float 提供给策略计算，精确值见 `*_exact`。

    内部账本是 Decimal 复式记账（见 sdk/ledger.py）；策略拿到的只是快照，
    任何赋值都会报错，不能绕过撮合直接改仓位。
    """

    __slots__ = ("_cost", "_opened", "_perp", "_price", "_qty", "_realized", "security")

    def __init__(
        self,
        security: str,
        qty: Decimal = ZERO,
        cost: Decimal = ZERO,
        price: Decimal = ZERO,
        realized: Decimal = ZERO,
        opened_ts: int = 0,
        perp: PerpPosition | None = None,
    ) -> None:
        object.__setattr__(self, "security", security)
        object.__setattr__(self, "_perp", perp)
        object.__setattr__(self, "_qty", qty)
        object.__setattr__(self, "_cost", cost)
        object.__setattr__(self, "_price", price)
        object.__setattr__(self, "_realized", realized)
        object.__setattr__(self, "_opened", opened_ts)

    @classmethod
    def of(cls, h: Holding | None, security: str) -> Position:
        if h is None:
            return cls(security)
        return cls(security, h.qty, h.cost, h.last_price, h.realized, h.opened_ts)

    @classmethod
    def of_perp(cls, p: PerpPosition | None, security: str) -> Position:
        """永续持仓：数量带符号（空头为负），成本 = 数量 × 开仓均价，市值为带符号的名义价值。"""
        if p is None:
            return cls(security)
        return cls(security, p.qty, p.qty * p.entry, p.last_price, p.realized, p.opened_ts, perp=p)

    # 永续合约专有
    @property
    def is_perp(self) -> bool:
        return self._perp is not None or is_perp(self.security)

    @property
    def side(self) -> str:
        return "long" if self._qty > 0 else "short" if self._qty < 0 else "flat"

    @property
    def leverage(self) -> float:
        return float(self._perp.leverage) if self._perp is not None else 1.0

    @property
    def margin(self) -> float:
        return float(self._perp.margin) if self._perp is not None else 0.0

    @property
    def liquidation_price(self) -> float:
        return float(self._perp.liquidation_price) if self._perp is not None and self._qty else 0.0

    @property
    def funding(self) -> float:
        """本轮持仓累计资金费（正 = 收到）。"""
        return float(self._perp.funding) if self._perp is not None else 0.0

    def __setattr__(self, name: str, value: Any) -> None:
        raise ReadOnlyError(_RO_MSG)

    def __delattr__(self, name: str) -> None:
        raise ReadOnlyError(_RO_MSG)

    # 精确值
    @property
    def amount_exact(self) -> Decimal:
        return self._qty

    @property
    def value_exact(self) -> Decimal:
        return self._qty * self._price

    # 便于策略计算的 float 视图
    @property
    def amount(self) -> float:
        return float(self._qty)

    @property
    def avg_cost(self) -> float:
        return float(self._cost / self._qty) if self._qty else 0.0

    @property
    def last_price(self) -> float:
        return float(self._price)

    @property
    def realized_pnl(self) -> float:
        return float(self._realized)

    @property
    def opened_ts(self) -> int:
        return self._opened

    @property
    def value(self) -> float:
        return float(self._qty * self._price)

    @property
    def cost_value(self) -> float:
        return float(self._cost)

    @property
    def unrealized_pnl(self) -> float:
        return float(self._qty * self._price - self._cost)

    @property
    def pnl_ratio(self) -> float:
        return float((self._qty * self._price - self._cost) / abs(self._cost)) if self._cost else 0.0

    def to_dict(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self._perp is not None:
            p = self._perp
            extra = {
                "perp": True,
                "side": self.side,
                "leverage": dstr(p.leverage),
                "margin": dstr(p.margin),
                "liquidation_price": dstr(p.liquidation_price.quantize(Decimal("1e-8"))) if self._qty else "0",
                "funding": dstr(p.funding),
                "notional": dstr(abs(self._qty) * self._price),
            }
        return {
            **extra,
            "security": self.security,
            "amount": dstr(self._qty),
            "avg_cost": dstr(self._cost / self._qty) if self._qty else "0",
            "last_price": dstr(self._price),
            "value": dstr(self._qty * self._price),
            "unrealized_pnl": dstr(self._qty * self._price - self._cost),
            "pnl_ratio": round(self.pnl_ratio, 6),
            "realized_pnl": dstr(self._realized),
            "opened_ts": self._opened,
        }


class Portfolio:
    """账户的**只读视图**，背后是 Decimal 复式账本。策略只读，改仓位要走 order_*。"""

    __slots__ = ("_ledger", "_starting")

    def __init__(self, ledger: Ledger | None = None, *, starting_cash: Any = 10_000) -> None:
        if ledger is None:
            ledger = Ledger()
            if starting_cash:
                ledger.deposit(starting_cash, ref="initial")
        object.__setattr__(self, "_ledger", ledger)
        object.__setattr__(self, "_starting", ledger.net_contributions if ledger.net_contributions else Decimal(0))

    def __setattr__(self, name: str, value: Any) -> None:
        raise ReadOnlyError(_RO_MSG)

    def __delattr__(self, name: str) -> None:
        raise ReadOnlyError(_RO_MSG)

    @property
    def starting_cash(self) -> float:
        return float(self._starting)

    @property
    def net_contributions(self) -> float:
        return float(self._ledger.net_contributions)

    @property
    def available_cash(self) -> float:
        return float(self._ledger.cash())

    @property
    def cash_exact(self) -> Decimal:
        return self._ledger.cash()

    @property
    def frozen_cash(self) -> float:
        return 0.0

    @property
    def positions(self) -> dict[str, Position]:
        out = {k: Position.of(h, k) for k, h in self._ledger.holdings.items() if h.qty > 0}
        out.update({k: Position.of_perp(p, k) for k, p in self._ledger.perps.items() if p.qty})
        return out

    @property
    def gross_exposure(self) -> float:
        """总敞口：现货市值 + 永续名义价值（绝对值）。"""
        return float(self._ledger.gross_exposure())

    @property
    def total_fees(self) -> float:
        return float(self._ledger.fees_value)

    @property
    def positions_value(self) -> float:
        return float(self._ledger.positions_value())

    @property
    def total_value(self) -> float:
        """总权益 = 现金 + 持仓市值（含未实现盈亏）。"""
        return float(self._ledger.equity())

    @property
    def total_value_exact(self) -> Decimal:
        return self._ledger.equity()

    @property
    def total_return(self) -> float:
        """相对净投入的收益。有出入金时请看单位净值收益（时间加权）。"""
        nc = self._ledger.net_contributions
        return float(self._ledger.equity() / nc - 1) if nc else 0.0

    @property
    def position_ratio(self) -> float:
        total = self._ledger.equity()
        return float(self._ledger.positions_value() / total) if total else 0.0

    def position(self, security: str) -> Position:
        """取持仓；没有就返回一个数量为 0 的空持仓，避免策略里到处判空。"""
        if is_perp(security):
            return Position.of_perp(self._ledger.perps.get(security), security)
        return Position.of(self._ledger.holdings.get(security), security)

    def to_dict(self) -> dict[str, Any]:
        led = self._ledger
        return {
            "currency": led.base_currency,
            "starting_cash": dstr(self._starting),
            "net_contributions": dstr(led.net_contributions),
            "available_cash": dstr(led.cash()),
            "frozen_cash": "0",
            "positions_value": dstr(led.positions_value()),
            "total_value": dstr(led.equity()),
            "total_return": round(self.total_return, 6),
            "position_ratio": round(self.position_ratio, 6),
            "total_fees": dstr(led.fees_value),
            "fees_by_currency": {k: dstr(v) for k, v in led.fees_by_currency.items()},
            "realized_pnl": dstr(led.realized_total()),
            "gross_exposure": dstr(led.gross_exposure()),
            "funding": led.funding_report() if led.funding_events else None,
            "liquidation_loss": dstr(led.liquidation_loss),
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
        }


@dataclass
class Context:
    """策略运行上下文。`ctx` 就是它。"""

    run_id: str
    mode: str  # backtest | paper | live
    portfolio: Portfolio = field(default_factory=Portfolio)  # 只读；运行时通过 _attach_portfolio 绑定
    current_dt: int = 0  # 当前时刻（毫秒，UTC）
    previous_dt: int = 0
    securities: list[str] = field(default_factory=list)
    benchmark: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    bar_index: int = 0
    is_warmup: bool = False
    # 当前运行的策略版本 ID（v2.0 要求 Context 携带策略版本）。
    # 策略可以用它写进自己发出的信号/日志，便于把一次成交追溯到确切的策略版本。
    strategy_version_id: str = ""
    strategy_name: str = ""

    # 运行期由框架填充，策略一般不直接读
    _runtime: Any = None
    _g: Any = None  # 策略全局变量容器；由 StrategyRunner 在构造时注入
    _sealed: bool = False  # 是否已封口（封口后 _PROTECTED 字段只读）

    # 策略不能替换账户、改运行编号、模式或策略版本（沙箱也禁止访问下划线属性）
    _PROTECTED = frozenset({"portfolio", "run_id", "mode", "strategy_version_id", "strategy_name"})

    def __setattr__(self, name: str, value: Any) -> None:
        if name in Context._PROTECTED and getattr(self, "_sealed", False):
            raise ReadOnlyError(f"ctx.{name} 是只读的：{_RO_MSG}")
        object.__setattr__(self, name, value)

    def _attach_portfolio(self, portfolio: Portfolio) -> None:
        object.__setattr__(self, "portfolio", portfolio)

    def _seal(self) -> None:
        object.__setattr__(self, "_sealed", True)

    # ---------- 便捷只读视图 ----------
    def price(self, security: str) -> float:
        """该标的的最新标记价。"""
        return self._runtime.price(security) if self._runtime else 0.0

    def position(self, security: str) -> Position:
        return self.portfolio.position(security)

    @property
    def cash(self) -> float:
        return self.portfolio.available_cash

    @property
    def total_value(self) -> float:
        return self.portfolio.total_value

    def version(self) -> str:
        """当前策略版本 ID（等价于 `ctx.strategy_version_id`）。"""
        return self.strategy_version_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "strategy_version_id": self.strategy_version_id,
            "strategy_name": self.strategy_name,
            "current_dt": self.current_dt,
            "securities": self.securities,
            "benchmark": self.benchmark,
            "bar_index": self.bar_index,
            "is_warmup": self.is_warmup,
            "portfolio": self.portfolio.to_dict(),
        }


class GlobalVars:
    """`g` 的容器。每次运行新建，不同运行之间不共享可变状态。"""

    def __init__(self) -> None:
        object.__setattr__(self, "_store", {})

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, "_store")[name]
        except KeyError as exc:
            raise AttributeError(f"g.{name} 还没有赋值。策略里要先在 initialize 中初始化它。") from exc

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(self, "_store")[name] = value

    def __delattr__(self, name: str) -> None:
        object.__getattribute__(self, "_store").pop(name, None)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in object.__getattribute__(self, "_store").items():
            out[k] = v.to_dict() if hasattr(v, "to_dict") else v
        return out
