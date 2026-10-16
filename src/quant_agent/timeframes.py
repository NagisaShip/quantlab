"""K 线周期：从毫秒级到日线的统一解析。

周期写成「数字 + 单位」：ms（毫秒）、s（秒）、m（分）、h（时）、d（天）、w（周），例如 100ms、250ms、1s、5s、
1m、30m、4h、1d。全平台查周期毫秒数、每年 bar 数都走这里，所以新增周期不需要改各处的字典。

数据来源：
- 交易所原生支持的周期直接拉 K 线（Binance 原生支持 1s）；
- 秒级的非原生周期（5s、15s…）由 1s K 线聚合；
- 亚秒级（100ms、250ms、500ms…）由逐笔成交（aggTrades）聚合，没有成交的区间用上一根收盘价补一根 0 成交量的 bar。
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator

_UNIT_MS = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
_RE = re.compile(r"^(\d{1,6})(ms|s|m|h|d|w)$")

#: 最细到 50 毫秒：再细就只是在逐笔成交上重复采样，没有意义，还会把请求量放大
MIN_TIMEFRAME_MS = 50
YEAR_MS = 365 * 86_400_000

#: 界面与模型工具里给出的常用周期（不限于这些：任何合法写法都能用）
TIMEFRAME_CHOICES = (
    "100ms", "250ms", "500ms", "1s", "5s", "15s", "30s",
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d",
)
#: 秒级以下（含 1 分钟以下）——回测按逐笔或秒线聚合，提示词与界面要提醒数据量与成本
SUB_MINUTE_HINT = "秒级 / 毫秒级周期由 1 秒 K 线或逐笔成交聚合而成，单次最多 5000 根；只有 Binance 提供这类数据"


def parse_timeframe_ms(tf: str) -> int | None:
    """「250ms」→ 250；「4h」→ 14400000；写法不合法返回 None。"""
    m = _RE.match(str(tf or "").strip())
    if not m:
        return None
    ms = int(m.group(1)) * _UNIT_MS[m.group(2)]
    return ms if MIN_TIMEFRAME_MS <= ms <= 30 * 86_400_000 else None


def is_timeframe(tf: str) -> bool:
    return parse_timeframe_ms(tf) is not None


def check_timeframe(tf: str) -> str:
    """pydantic 校验器：不合法时给出可读的报错。"""
    if not is_timeframe(tf):
        raise ValueError(f"不支持的周期 {tf!r}：写成 数字+单位（ms / s / m / h / d），例如 100ms、1s、5m、4h；最细 {MIN_TIMEFRAME_MS}ms")
    return tf


#: API 入参用的周期类型：任意合法写法都接受，不合法时报出可读的错误
TimeframeStr = Annotated[str, AfterValidator(check_timeframe)]


def is_sub_minute(tf: str) -> bool:
    ms = parse_timeframe_ms(tf)
    return ms is not None and ms < 60_000


class TimeframeTable(dict):
    """像普通字典一样用（TIMEFRAME_MS["1h"]、.get(tf, 默认)、tf in TIMEFRAME_MS），但能识别任意合法周期。

    fn 把周期毫秒数换成表里的值（周期毫秒数本身，或每年 bar 数）。
    """

    def __init__(self, base: dict[str, Any], fn: Any) -> None:
        super().__init__(base)
        self._fn = fn

    def _derive(self, key: Any) -> Any:
        ms = parse_timeframe_ms(key) if isinstance(key, str) else None
        return None if ms is None else self._fn(ms)

    def __missing__(self, key: Any) -> Any:
        v = self._derive(key)
        if v is None:
            raise KeyError(key)
        return v

    def get(self, key: Any, default: Any = None) -> Any:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        v = self._derive(key)
        return default if v is None else v

    def __contains__(self, key: object) -> bool:
        return dict.__contains__(self, key) or (isinstance(key, str) and is_timeframe(key))


__all__ = [
    "MIN_TIMEFRAME_MS",
    "SUB_MINUTE_HINT",
    "TIMEFRAME_CHOICES",
    "TimeframeStr",
    "TimeframeTable",
    "check_timeframe",
    "is_sub_minute",
    "is_timeframe",
    "parse_timeframe_ms",
]
