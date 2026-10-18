"""行情与数据中心路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..data.service import DataUnavailable
from ..live import micro
from .deps import get_services

router = APIRouter(prefix="/api/market", tags=["行情与数据"])

SUPPORTED_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "LTC/USDT",
    "DOT/USDT",
    "TRX/USDT",
]


@router.get("/db-cache", summary="服务端数据库缓存：各类外部请求的缓存条目、命中与未命中")
def db_cache_stats() -> dict:
    s = get_services()
    return s.dbcache.stats() if s.dbcache else {"namespaces": []}


@router.delete("/db-cache", summary="清空服务端数据库缓存（可只清一个命名空间）")
def db_cache_clear(ns: str | None = None) -> dict:
    s = get_services()
    return {"deleted": s.dbcache.clear(ns) if s.dbcache else 0}


@router.get("/symbols")
def symbols() -> dict:
    from ..timeframes import SUB_MINUTE_HINT, TIMEFRAME_CHOICES

    return {
        "symbols": SUPPORTED_SYMBOLS,
        "timeframes": list(TIMEFRAME_CHOICES),
        "sub_minute_note": SUB_MINUTE_HINT,
        "exchanges": ["binance", "okx", "bybit"],
        "sources": ["auto", "exchange", *(["synthetic"] if get_services().market.allow_synthetic else [])],
    }


@router.get("/bars")
def bars(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    limit: int = Query(500, ge=10, le=5000),
    exchange: str = "binance",
    source: str = Query("auto", pattern="^(auto|exchange|synthetic)$"),
    start: int | None = None,
    end: int | None = None,
    audit: bool = False,
) -> dict:
    """已收盘 K 线。

    `audit=true` 时附带事件审计字段（event_ts / receive_ts / sequence / source / instrument_id / raw_ref）。
    """
    s = get_services()
    try:
        data, source = s.market.get_bars(
            symbol, timeframe, limit=limit, exchange=exchange, source=source, start=start, end=end
        )
    except DataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    stale = s.market.stale.get((exchange, symbol, timeframe), "") if source == "exchange" else ""
    return {
        "error": s.market.last_error.get(exchange, "") if source == "synthetic" else "",
        # 交易所这次没连上、用本地缓存顶上时给出原因：界面提示「可能缺最新几根」，而不是整页报错
        "stale": bool(stale),
        "warning": f"交易所这次没连上（{stale}），展示的是本地缓存" if stale else "",
        "symbol": symbol,
        "timeframe": timeframe,
        "source": source,
        "count": len(data),
        "bars": [
            {
                "t": b.open_time,
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "v": b.volume,
                "closed": b.closed,
                **({"audit": b.audit()} if audit else {}),
            }
            for b in data
        ],
        "last_event_ts": data[-1].close_time if data else None,
        "last_receive_ts": max((b.receive_ts for b in data), default=None),
    }


@router.get("/ticker")
def ticker(symbol: str = "BTC/USDT", exchange: str = "binance") -> dict:
    s = get_services()
    adapter = s.market.adapters.get(exchange)
    if adapter is not None:
        try:
            data = adapter.fetch_ticker(symbol)
            return {"symbol": symbol, "source": f"{exchange}:live", **data}
        except Exception:
            pass
    try:
        bars, source = s.market.get_bars(symbol, "1h", limit=50, exchange=exchange)
    except DataUnavailable as exc:
        return {"symbol": symbol, "source": "none", "price": None, "connected": False, "error": str(exc)[:200]}
    return {"symbol": symbol, "source": source, "price": bars[-1].close if bars else None}


@router.get("/quality")
def quality(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    limit: int = Query(1500, ge=100, le=5000),
    exchange: str = "binance",
) -> dict:
    s = get_services()
    q = s.market.quality(symbol, timeframe, limit=limit, exchange=exchange)
    return q.to_dict()


@router.get("/summary")
def summary(symbol: str = "BTC/USDT", timeframe: str = "1h") -> dict:
    return get_services().market_summary(symbol, timeframe)


@router.get("/cache")
def cache_entries() -> dict:
    """本地 K 线缓存清单（只含已收盘 bar）。"""
    s = get_services()
    entries = s.market.cache.entries() if s.market.cache else []
    return {
        "entries": entries,
        "root": str(s.market.cache.root) if s.market.cache else "",
        "last_errors": s.market.last_error,
        "pull_stats": s.market.pull_stats,
        "min_pull_interval_ms": round(s.market.min_pull_interval * 1000, 1),
    }


@router.delete("/cache")
def clear_cache(exchange: str, symbol: str, timeframe: str) -> dict:
    s = get_services()
    if not s.market.cache or not s.market.cache.clear(exchange, symbol, timeframe):
        raise HTTPException(404, "缓存不存在")
    return {"ok": True}


@router.post("/cache/fetch")
def prefetch(
    symbol: str = "BTC/USDT", timeframe: str = "1h", days: int = Query(30, ge=1, le=1500), exchange: str = "binance"
) -> dict:
    """预取一段真实历史行情进缓存（只用真实行情，失败直接报错）。"""
    import time as _time

    s = get_services()
    end = int(_time.time() * 1000)
    try:
        data, _ = s.market.get_bars(
            symbol, timeframe, exchange=exchange, source="exchange", start=end - days * 86_400_000, end=end
        )
    except DataUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return {
        "ok": True,
        "bars": len(data),
        "start_ts": data[0].open_time if data else 0,
        "end_ts": data[-1].close_time if data else 0,
    }


@router.post("/sync", summary="同步真实行情：分段拉取并补齐缺口")
def sync_data(
    symbols: str = Query("BTC/USDT", description="逗号分隔"),
    timeframe: str = "1h",
    days: int = Query(180, ge=1, le=1500),
    exchange: str = "binance",
) -> dict:
    """同步真实行情：分段拉取、补齐中间缺口，返回每个标的的覆盖率与缺口数。"""
    return get_services().sync_market_data(symbols.split(","), timeframe, days, exchange)


@router.get("/quotes", summary="关注列表行情：多个标的的收盘序列，未接入数据的标的如实标注")
def quotes(
    symbols: str = Query("BTC/USDT,ETH/USDT", description="逗号分隔"),
    timeframe: str = "1h",
    limit: int = Query(168, ge=10, le=1000),
) -> dict:
    """首页关注列表：每个标的的收盘序列；没有真实行情的标的返回 connected=false（不用合成数据顶替）。"""
    s = get_services()
    out = []
    for sym in [x.strip().upper() for x in symbols.split(",") if x.strip()][:12]:
        try:
            data, source = s.market.get_bars(sym, timeframe, limit=limit)
            out.append(
                {
                    "symbol": sym,
                    "connected": True,
                    "source": source,
                    "t": [b.open_time for b in data],
                    "c": [b.close for b in data],
                }
            )
        except (DataUnavailable, ValueError) as exc:
            out.append({"symbol": sym, "connected": False, "error": str(exc)[:200]})
    return {"timeframe": timeframe, "quotes": out}


@router.get("/network")
def network_status() -> dict:
    """部署地网络实测：72 小时内成功率 ≥ 99% 才判定可用；不足 72 小时显示“网络可用性待验证”。"""
    from ..platform import netprobe

    return netprobe.status(get_services().db)


@router.post("/network/probe")
def network_probe() -> dict:
    """立即探测一次（worker 每 5 分钟也会自动探测）。"""
    from ..platform import netprobe

    s = get_services()
    return {"results": netprobe.probe_adapters(s.db, s.market.adapters), "status": netprobe.status(s.db)}


_BANNER_CACHE: dict[str, Any] = {}


@router.get("/banner")
def status_banner(exchange: str = "binance", symbol: str = "BTC/USDT") -> dict:
    """全局横幅：运行模式 + 行情来源 + 最近有效事件时间 + 网络实测 + worker 与隔离状态。"""
    import time as _t

    from ..platform import isolation, netprobe
    from . import tasks

    s = get_services()
    key = f"{exchange}:{symbol}"
    cached = _BANNER_CACHE.get(key)
    if cached is None or _t.time() - cached["at"] > 60:
        try:
            bars, source = s.market.get_bars(symbol, "1h", limit=3, exchange=exchange, source="auto")
            last = bars[-1] if bars else None
            market = {
                "source": source,
                "exchange": exchange if source == "exchange" else "",
                "symbol": symbol,
                "last_event_ts": last.close_time if last else None,
                "last_receive_ts": last.receive_ts if last else None,
                "error": s.market.last_error.get(exchange, "") if source != "exchange" else "",
            }
        except Exception as exc:
            market = {
                "source": "unavailable",
                "exchange": exchange,
                "symbol": symbol,
                "last_event_ts": None,
                "last_receive_ts": None,
                "error": str(exc)[:200],
            }
        cached = {"at": _t.time(), "market": market}
        _BANNER_CACHE[key] = cached
    now = int(_t.time() * 1000)
    workers = s.db.fetchall("SELECT id FROM workers WHERE last_seen>?", (now - 60_000,))
    net = netprobe.status(s.db)
    iso = isolation.probe()
    mc = micro.config(s.store)
    live_enabled = bool(mc.get("enabled"))
    if live_enabled:
        mode_label = "研究与仿真 · 微实盘已开启（真实资金，小额）"
    elif micro.server_enabled():
        mode_label = "研究与仿真 · 虚拟资金（微实盘可用，需管理员开启）"
    else:
        mode_label = "研究与仿真 · 虚拟资金（微实盘默认关闭）"
    return {
        "mode": "micro_live" if live_enabled else "research",
        "mode_label": mode_label,
        "live_enabled": live_enabled,
        "market": cached["market"],
        "network": {"overall": net["overall"], "label": net["label"]},
        "worker": {"mode": tasks.worker_mode(), "online": len(workers)},
        "isolation": {"verified": iso.get("verified", False), "mode": iso.get("mode")},
        "server_time": now,
    }
