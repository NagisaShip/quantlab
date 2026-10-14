"""统一交易所适配器接口（对应 plan.md 第 3 章）。

一期只覆盖"公共行情 + 只读账户导入 + 模拟盘"，交易适配器为可选扩展。
所有实现必须返回标准事件，不允许用近似语义偷偷替代不支持的功能。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable


@dataclass
class Instrument:
    exchange: str
    symbol: str  # 标准化的 base/quote，如 BTC/USDT
    market_type: str  # spot | swap
    base: str
    quote: str
    tick_size: str  # Decimal 字符串，禁止浮点记账
    lot_size: str
    min_notional: str = "0"


@dataclass
class Bar:
    symbol: str
    timeframe: str
    open_time: int  # ms, UTC
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool = True  # 区分未完成与已收盘（关键！）
    # ---- 事件审计字段（v2.0）：每根 bar 都是一个可追溯的行情事件 ----
    # event_ts = close_time（事件时间）；receive_ts 为平台收到它的时间；
    # sequence 为同一来源内单调递增的序号；raw_ref 指向原始响应（文件/批次/哈希）。
    # 审计元数据不参与相等比较：同一根行情无论何时收到都是同一个事件
    receive_ts: int = field(default=0, compare=False)
    sequence: int = field(default=0, compare=False)
    source: str = field(default="", compare=False)
    instrument_id: str = field(default="", compare=False)
    raw_ref: str = field(default="", compare=False)

    @property
    def event_ts(self) -> int:
        return self.close_time

    def audit(self) -> dict:
        return {
            "event_ts": self.close_time,
            "receive_ts": self.receive_ts,
            "sequence": self.sequence,
            "source": self.source,
            "instrument_id": self.instrument_id or self.symbol,
            "raw_ref": self.raw_ref,
        }


@dataclass
class Balance:
    asset: str
    free: str
    locked: str


@dataclass
class FundingRate:
    """永续合约资金费率事件（T03）。

    资金费按固定周期（交易所常见 8 小时）在结算点收取/支付，与逐笔成交是**两类不同的事件**：
    成交改变持仓，资金费只改变现金。把它建模成独立的 `FundingRate` 而不是往 `Bar` 里塞字段，
    是因为它有自己的事件时间（结算点）与自己的来源，混进 K 线会让事件审计字段说不清"这笔钱
    到底是哪来的"。
    """

    symbol: str
    ts: int  # 结算时刻（ms, UTC），即 event_ts
    rate: str  # Decimal 字符串；正数 = 多头付空头
    mark_price: str = "0"  # 结算用的标记价（名义价值按它算）
    interval_hours: int = 8
    # ---- 事件审计字段（与 Bar 同口径，便于统一追溯）----
    receive_ts: int = field(default=0, compare=False)
    sequence: int = field(default=0, compare=False)
    source: str = field(default="", compare=False)
    instrument_id: str = field(default="", compare=False)
    raw_ref: str = field(default="", compare=False)

    @property
    def event_ts(self) -> int:
        return self.ts

    def audit(self) -> dict:
        return {
            "event_ts": self.ts,
            "receive_ts": self.receive_ts,
            "sequence": self.sequence,
            "source": self.source,
            "instrument_id": self.instrument_id or self.symbol,
            "raw_ref": self.raw_ref,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ts": self.ts,
            "rate": self.rate,
            "mark_price": self.mark_price,
            "interval_hours": self.interval_hours,
            **self.audit(),
        }


@dataclass
class Fill:
    symbol: str
    side: str
    price: str
    qty: str
    fee: str
    fee_asset: str
    fill_id: str
    ts: int


@dataclass
class OrderIntent:
    """组合层产出的意图；执行层才决定走仿真/测试/实盘。"""

    run_id: str
    signal_id: str
    symbol: str
    side: str  # buy | sell
    qty: str  # Decimal 字符串
    order_type: str = "market"
    client_order_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Capability:
    """交易所能力矩阵的一行。

    对不支持的功能**显式拒绝**，禁止用近似语义悄悄替代——
    这是原方案的硬要求：悄悄降级会让人把模拟结果当成真实成交。
    """

    key: str
    supported: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "supported": self.supported, "note": self.note}


class UnsupportedFeature(NotImplementedError):
    """显式拒绝，禁止悄悄降级。"""


@runtime_checkable
class ExchangeAdapter(Protocol):
    name: str

    # --- 行情 ---
    def get_instruments(self) -> list[Instrument]: ...
    def fetch_bars(self, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]: ...
    def fetch_ticker(self, symbol: str) -> dict[str, Any]: ...
    def fetch_funding_rates(self, symbol: str, start: int, end: int) -> list[FundingRate]:
        """永续合约资金费率历史。现货标的没有资金费，默认显式拒绝而不是返回空列表——
        返回空列表会被上层误读成"这段时间费率为 0"，而实际是"这个标的不支持"。"""
        raise UnsupportedFeature(f"{getattr(self, 'name', 'adapter')} 未提供资金费率")

    # --- 账户（只读）---
    def fetch_balances(self) -> list[Balance]: ...
    def fetch_fills(self, symbol: str | None = None, start: int | None = None) -> list[Fill]: ...

    # --- 模拟/交易（可选，默认抛 UnsupportedFeature）---
    def place_order(self, intent: OrderIntent) -> dict[str, Any]:
        raise UnsupportedFeature(f"{self.name} 未实现交易")

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        raise UnsupportedFeature(f"{self.name} 未实现撤单")

    def query_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        raise UnsupportedFeature(f"{self.name} 未实现查单")

    def capabilities(self) -> list[Capability]:
        raise UnsupportedFeature(f"{self.name} 未提供能力矩阵")


class BaseAdapter:
    """共享工具：精度取整、能力矩阵、限流占位。"""

    name = "base"
    supports_trading = False

    def __init__(self, *, simulated: bool = False) -> None:
        self.simulated = simulated
        self._instruments: dict[str, Instrument] = {}

    # ---------- 精度 ----------
    def round_qty(self, symbol: str, qty: Any) -> Decimal:
        """按交易所步长向下取整（Decimal 精确运算，不经过浮点）。"""
        q = _d(qty)
        inst = self._instruments.get(symbol)
        step = _d(inst.lot_size) if inst is not None and inst.lot_size else Decimal(0)
        if step <= 0:
            return q
        return (q / step).to_integral_value(rounding=ROUND_DOWN) * step

    def round_price(self, symbol: str, price: Any) -> Decimal:
        p = _d(price)
        inst = self._instruments.get(symbol)
        tick = _d(inst.tick_size) if inst is not None and inst.tick_size else Decimal(0)
        if tick <= 0:
            return p
        return (p / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick

    def meets_min_notional(self, symbol: str, qty: Any, price: Any) -> bool:
        inst = self._instruments.get(symbol)
        if inst is None or not inst.min_notional:
            return True
        try:
            return _d(qty) * _d(price) >= _d(inst.min_notional)
        except (InvalidOperation, ValueError):
            return True

    def cache_instruments(self) -> list[Instrument]:
        """拉一次交易规则并缓存，供精度换算使用。

        已经有缓存就直接复用——否则每次下单都会多打一次 exchangeInfo，
        既浪费限流额度，也让"下单失败"的排查变复杂。
        """
        if self._instruments:
            return list(self._instruments.values())
        items = self.get_instruments()
        self._instruments = {i.symbol: i for i in items}
        return items

    # ---------- 永续合约 ----------
    def fetch_funding_rates(self, symbol: str, start: int, end: int) -> list[FundingRate]:
        """永续合约资金费率历史。现货默认显式拒绝（不是返回空列表）。

        返回空列表会被上层误读成"这段时间费率为 0"，而实际是"这个标的不支持"。
        """
        raise UnsupportedFeature(f"{self.name} 未提供资金费率")

    def capabilities(self) -> list[Capability]:
        """默认能力矩阵；具体交易所覆写。"""
        return [
            Capability("public_market_data", True, "公共行情，无需鉴权"),
            Capability("readonly_account", True, "只读账户，需要只读密钥"),
            Capability("simulated_trading", False, "未接入模拟盘"),
            Capability("live_trading", False, "未接入实盘下单"),
            Capability("withdraw", False, "平台不提供提现能力"),
        ]


def _d(v: Any) -> Decimal:
    """数值 → Decimal（float 先转字符串，避免把二进制尾差带进订单数量）。"""
    if isinstance(v, Decimal):
        return v
    return Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v))
