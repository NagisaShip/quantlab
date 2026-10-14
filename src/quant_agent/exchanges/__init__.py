"""交易所层导出。"""

from .base import (
    Balance,
    Bar,
    BaseAdapter,
    ExchangeAdapter,
    Fill,
    Instrument,
    OrderIntent,
    UnsupportedFeature,
)
from .binance import BinanceAdapter
from .bybit import BybitAdapter
from .okx import OKXAdapter


def public_adapter(exchange: str) -> BaseAdapter:
    """按交易所名取一个公共行情适配器（无密钥）。未知名称退回 Binance，与原有行为一致。"""
    if exchange.startswith("ccxt:"):
        from .ccxt_adapter import CcxtAdapter

        return CcxtAdapter(exchange.split(":", 1)[1])
    if exchange == "okx":
        return OKXAdapter()
    if exchange == "bybit":
        return BybitAdapter()
    return BinanceAdapter()


__all__ = [
    "Balance",
    "Bar",
    "BaseAdapter",
    "BinanceAdapter",
    "BybitAdapter",
    "ExchangeAdapter",
    "Fill",
    "Instrument",
    "OKXAdapter",
    "OrderIntent",
    "UnsupportedFeature",
    "public_adapter",
]
