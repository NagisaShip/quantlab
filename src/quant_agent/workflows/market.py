"""Native data sources for workflow cards.

Cards run in a child process with a sanitized environment and only see files, so
platform services (exchange adapters, local bar cache, synthetic fallback) are not
reachable from card code. ``source.market`` therefore declares
``capabilities.native = "market_bars"`` and is executed in-process by the worker;
the fetched rows are still frozen into the normal run artifact so the evidence is
reproducible. Standalone/offline packages keep the card but refuse to fake data.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .contracts import WorkflowError

#: 与平台其它页面一致的标的写法：BTC/USDT、BTCUSDT、btc-usdt 都接受
_QUOTES = ("USDT", "USDC", "USD", "BTC", "ETH", "EUR")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$")


def normalize_symbol(raw: str) -> str:
    text = (raw or "").strip().upper().replace("-", "/").replace("_", "/")
    if not text:
        raise WorkflowError("请填写标的，例如 BTC/USDT")
    if "/" in text:
        base, _, quote = text.partition("/")
        symbol = f"{base}/{quote}"
    else:
        for quote in _QUOTES:
            if text.endswith(quote) and len(text) > len(quote):
                symbol = f"{text[: -len(quote)]}/{quote}"
                break
        else:
            raise WorkflowError(f"看不懂标的「{raw}」：请写成 BTC/USDT 或 BTCUSDT")
    if not _SYMBOL_RE.fullmatch(symbol):
        raise WorkflowError(f"标的「{raw}」格式不合法：请写成 BTC/USDT")
    return symbol


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _row(bar) -> dict[str, Any]:
    return {
        "time": _iso(bar.open_time),
        "open_time": bar.open_time,
        "close_time": bar.close_time,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "closed": bool(bar.closed),
        "symbol": bar.symbol,
        "timeframe": bar.timeframe,
        "source": bar.source,
    }


def _quality(bars: list, symbol: str, timeframe: str, actual: str) -> dict[str, Any]:
    from ..data.quality import TIMEFRAME_MS
    from ..data.service import check_quality

    quality = check_quality(
        bars,
        symbol=symbol,
        timeframe=timeframe,
        source=actual,
        step_ms=TIMEFRAME_MS.get(timeframe, 3_600_000),
    )
    return quality.to_dict()


def market_bars(
    services,
    params: dict[str, Any],
    *,
    tick: Callable[[], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Fetch closed bars from the platform market service and freeze them as a table.

    Returns exactly the declared output ports: ``data`` (rows) and ``source``
    (provenance/quality). Never invents data: synthetic bars are only used when
    the caller asked for them or when the deployment explicitly allows fallback,
    and either way the report says so.
    """
    tick = tick or (lambda: None)
    log = log or (lambda _message: None)
    market = getattr(services, "market", None)
    if market is None:
        raise WorkflowError("当前部署没有行情服务，无法直连行情；请改用 CSV 数据源")

    symbol = normalize_symbol(str(params.get("symbol") or "BTC/USDT"))
    timeframe = str(params.get("timeframe") or "1h")
    limit = int(params.get("bars") or 1500)
    requested = str(params.get("source") or "auto")
    exchange = str(params.get("exchange") or "auto")
    if limit < 50 or limit > 5000:
        raise WorkflowError("K线根数须在 50—5000 之间")
    if timeframe not in ("1m", "5m", "15m", "30m", "1h", "4h", "1d"):
        raise WorkflowError(f"不支持的周期：{timeframe}")

    candidates = ["binance", "okx", "bybit"] if exchange == "auto" else [exchange]
    if requested == "synthetic":
        candidates = [candidates[0]]
    errors: list[str] = []
    fallback: tuple[list, str, str] | None = None
    chosen: tuple[list, str, str] | None = None
    for name in candidates:
        tick()
        try:
            bars, actual = market.get_bars(symbol, timeframe, limit=limit, exchange=name, source=requested)
        except Exception as exc:  # 网络、地域、交易对不存在等：换下一个交易所，但要保留原因
            errors.append(f"{name}: {exc}"[:220])
            log(f"{name} 取数失败：{exc}")
            continue
        if actual == "exchange":
            chosen = (bars, actual, name)
            break
        fallback = (bars, actual, name)
        log(f"{name} 未取得真实行情，{actual} 作为兜底候选")
    if chosen is None:
        if fallback is not None and requested in ("auto", "synthetic"):
            chosen = fallback
        else:
            reason = "；".join(errors[-3:]) if errors else "没有可用交易所"
            raise WorkflowError(f"直连行情失败（{symbol} {timeframe}）：{reason}")
    bars, actual, exchange_used = chosen
    bars = [bar for bar in bars if bar.closed]
    if not bars:
        raise WorkflowError(f"直连行情没有返回已收盘K线（{symbol} {timeframe}）")

    stale = getattr(market, "stale", {}).get((exchange_used, symbol, timeframe), "")
    quality = _quality(bars, symbol, timeframe, actual)
    report = {
        "symbol": symbol,
        "timeframe": timeframe,
        "requested": {"exchange": exchange, "source": requested, "bars": limit},
        "exchange": exchange_used,
        "source": actual,
        "source_label": (
            "交易所真实历史行情" if actual == "exchange" else "合成数据（不是交易所行情）" if actual == "synthetic" else actual
        ),
        "bars": len(bars),
        "coverage_start": bars[0].open_time,
        "coverage_end": bars[-1].close_time,
        "coverage_start_iso": _iso(bars[0].open_time),
        "coverage_end_iso": _iso(bars[-1].close_time),
        "fetched_at": time.time(),
        "quality": quality,
        "warning": stale if actual == "exchange" else "这一列不是交易所真实行情，不能当作实盘证据" if actual == "synthetic" else "",
    }
    log(
        f"直连行情：{symbol} {timeframe} {len(bars)}根，来源 {report['source_label']}"
        + (f"（缓存可能滞后：{stale}）" if stale else "")
    )
    return {"data": [_row(bar) for bar in bars], "source": report}


def native_handlers(services) -> dict[str, Callable[..., dict[str, Any]]]:
    """Worker-side handlers for workflow cards that declare capabilities.native."""
    return {"market_bars": lambda params, *, tick=None, log=None: market_bars(services, params, tick=tick, log=log)}
