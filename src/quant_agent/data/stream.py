"""实时行情网关：交易所 WebSocket K 线推送 → 平台的 Bar 事件。

为什么要有它：实时模拟原来每 30 秒用 REST 拉一次已收盘 K 线，新 bar 最多晚 30 秒才进策略，
盘中价格也看不到。网关用一条长连接收推送（Binance 约每 2 秒、OKX 每 0.5 秒左右推一次正在形成的 bar），
新 bar 收盘后 1–2 秒内就能进策略。

设计：
- 一个 (交易所, 标的, 周期) 一条连接，多个实验共享（引用计数），后台线程里跑 asyncio。
- 断线自动重连（指数退避，上限 60 秒）；重连后**用 REST 补齐断线期间的已收盘 bar**，
  补数和断线都记进事件列表，界面和运行事件里如实展示——沿用平台“不静默丢数据”的原则。
- 只认交易所明确标记“已收盘”的 bar；正在形成的 bar 只用于盘中估值和画图，不触发策略。
- 网关只是更快的数据源：上层发现它不健康（未连上、超过 90 秒没消息）时退回 REST 轮询。
- 行情域名只用公共数据域名（Binance：data-stream.binance.vision；OKX：ws.okx.com），不涉及任何密钥。
  代理沿用环境变量（HTTPS_PROXY / ALL_PROXY），websockets 15 会自动读取。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..exchanges.base import Bar
from .quality import TIMEFRAME_MS

log = logging.getLogger("quantlab.stream")

BINANCE_WS = "wss://data-stream.binance.vision/stream"
OKX_WS = "wss://ws.okx.com:8443/ws/v5/business"
BINANCE_FUTURES_WS = "wss://fstream.binance.com/stream"
BYBIT_WS = "wss://stream.bybit.com/v5/public"
_BYBIT_INTERVAL = {"1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240", "1d": "D"}
EXCHANGES = ("binance", "okx", "bybit")
_OKX_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}

#: 超过这么久没有任何消息，视为连接不健康（上层退回 REST）
STALE_MS = 90_000
MAX_BACKOFF = 60.0


def enabled() -> bool:
    """QAGENT_STREAM=0 关闭网关（测试默认关闭；没有外网的环境也可以关掉，免得后台反复重连）。"""
    return os.environ.get("QAGENT_STREAM", "1").strip().lower() not in ("0", "false", "off", "no")


def now_ms() -> int:
    return int(time.time() * 1000)


def bar_dict(b: Bar) -> dict[str, Any]:
    return {
        "t": b.open_time,
        "T": b.close_time,
        "o": b.open,
        "h": b.high,
        "l": b.low,
        "c": b.close,
        "v": b.volume,
        "closed": b.closed,
    }


# ---------------------------------------------------------------------------
# 消息解析（纯函数，便于用录下来的消息做测试）
# ---------------------------------------------------------------------------
def _is_perp(symbol: str) -> bool:
    return symbol.upper().endswith(":PERP")


def binance_url(symbol: str, timeframe: str) -> str:
    base = BINANCE_FUTURES_WS if _is_perp(symbol) else BINANCE_WS
    return f"{base}?streams={symbol.split(':')[0].replace('/', '').lower()}@kline_{timeframe}"


def parse_binance(msg: dict[str, Any], symbol: str, timeframe: str) -> Bar | None:
    data = msg.get("data", msg)
    if data.get("e") != "kline":
        return None
    k = data["k"]
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        open_time=int(k["t"]),
        close_time=int(k["T"]),  # 与 REST klines 的 close_time 口径一致（开盘 + 周期 - 1ms）
        open=float(k["o"]),
        high=float(k["h"]),
        low=float(k["l"]),
        close=float(k["c"]),
        volume=float(k["v"]),
        closed=bool(k.get("x")),
        receive_ts=now_ms(),
        source="binance-ws",
    )


def okx_subscribe(symbol: str, timeframe: str) -> dict[str, Any]:
    from ..exchanges.okx import inst_id_of

    return {"op": "subscribe", "args": [{"channel": f"candle{_OKX_BAR[timeframe]}", "instId": inst_id_of(symbol)}]}


def parse_okx(msg: dict[str, Any], symbol: str, timeframe: str) -> list[Bar]:
    if not str((msg.get("arg") or {}).get("channel", "")).startswith("candle") or "data" not in msg:
        return []
    step = TIMEFRAME_MS[timeframe]
    out = []
    for r in msg["data"]:
        ts = int(r[0])
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=ts,
                close_time=ts + step,  # 与 OKX REST 适配器口径一致
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
                closed=(r[8] == "1") if len(r) > 8 else False,
                receive_ts=now_ms(),
                source="okx-ws",
            )
        )
    return out


def bybit_url(symbol: str) -> str:
    return f"{BYBIT_WS}/{'linear' if _is_perp(symbol) else 'spot'}"


def bybit_subscribe(symbol: str, timeframe: str) -> dict[str, Any]:
    sym = symbol.split(":")[0].replace("/", "").upper()
    return {"op": "subscribe", "args": [f"kline.{_BYBIT_INTERVAL[timeframe]}.{sym}"]}


def parse_bybit(msg: dict[str, Any], symbol: str, timeframe: str) -> list[Bar]:
    if not str(msg.get("topic", "")).startswith("kline.") or not isinstance(msg.get("data"), list):
        return []
    out = []
    for r in msg["data"]:
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=int(r["start"]),
                close_time=int(r["end"]) + 1,  # Bybit 的 end 是周期最后一毫秒
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
                closed=bool(r.get("confirm")),
                receive_ts=now_ms(),
                source="bybit-ws",
            )
        )
    return out


# ---------------------------------------------------------------------------
# 单条连接
# ---------------------------------------------------------------------------
@dataclass
class StreamStats:
    connected: bool = False
    connects: int = 0
    reconnects: int = 0
    messages: int = 0
    last_message_ts: int = 0
    last_closed_ts: int = 0
    lag_ms: int = 0  # 最近一根已收盘 bar：收到时间 - 收盘时间
    backfilled: int = 0
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connects": self.connects,
            "reconnects": self.reconnects,
            "messages": self.messages,
            "last_message_ts": self.last_message_ts,
            "last_closed_ts": self.last_closed_ts,
            "lag_ms": self.lag_ms,
            "backfilled": self.backfilled,
            "error": self.error,
            "healthy": self.healthy(),
            "events": self.events[-20:],
        }

    def healthy(self) -> bool:
        return self.connected and now_ms() - self.last_message_ts < STALE_MS

    def note(self, level: str, message: str) -> None:
        self.events.append({"ts": now_ms(), "level": level, "message": message})
        del self.events[:-100]


Backfill = Callable[[str, str, int, int], list[Bar]]


class BarStream:
    """一条 (交易所, 标的, 周期) 的推送连接。线程安全地暴露最新 bar 与已收盘 bar 缓冲。"""

    def __init__(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        *,
        backfill: Backfill | None = None,
        url: str | None = None,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        if timeframe not in TIMEFRAME_MS:
            raise ValueError(f"不支持的周期：{timeframe}")
        if exchange not in EXCHANGES:
            raise ValueError(f"不支持的交易所：{exchange}")
        self.exchange, self.symbol, self.timeframe = exchange, symbol, timeframe
        self.url = (
            url
            or {
                "binance": lambda: binance_url(symbol, timeframe),
                "okx": lambda: OKX_WS,
                "bybit": lambda: bybit_url(symbol),
            }[exchange]()
        )
        self.backfill = backfill
        self._connect = connect
        self.stats = StreamStats()
        self.forming: Bar | None = None
        self.closed: deque[Bar] = deque(maxlen=2000)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- 生命周期 ----
    def start(self) -> BarStream:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"stream-{self.exchange}-{self.symbol}", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        import websockets

        connect = self._connect or websockets.connect
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with connect(
                    self.url, open_timeout=15, ping_interval=20, ping_timeout=20, close_timeout=3, max_size=2**20
                ) as ws:
                    if self.exchange == "okx":
                        await ws.send(json.dumps(okx_subscribe(self.symbol, self.timeframe)))
                    elif self.exchange == "bybit":
                        await ws.send(json.dumps(bybit_subscribe(self.symbol, self.timeframe)))
                    with self._lock:
                        self.stats.connected = True
                        self.stats.connects += 1
                        self.stats.error = ""
                        if self.stats.connects > 1:
                            self.stats.reconnects += 1
                        self.stats.note("info", "已连接" if self.stats.connects == 1 else "已重连")
                    backoff = 1.0
                    await self._after_connect()
                    await self._pump(ws)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # 网络错误：记一次，退避后重连
                with self._lock:
                    was = self.stats.connected
                    self.stats.connected = False
                    self.stats.error = f"{type(exc).__name__}: {exc}"[:200]
                    if was or self.stats.connects == 0:
                        self.stats.note("warning", f"连接断开：{self.stats.error}")
            with self._lock:
                self.stats.connected = False
            if self._stop.is_set():
                break
            await self._sleep(backoff)
            backoff = min(MAX_BACKOFF, backoff * 2)

    async def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < end:
            await asyncio.sleep(0.2)

    async def _pump(self, ws: Any) -> None:
        last_ping = time.monotonic()
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except TimeoutError:
                # OKX 要求 30 秒内没有数据时客户端发文本 "ping"
                if self.exchange == "okx" and time.monotonic() - last_ping > 25:
                    await ws.send("ping")
                    last_ping = time.monotonic()
                elif self.exchange == "bybit" and time.monotonic() - last_ping > 20:
                    await ws.send(json.dumps({"op": "ping"}))  # Bybit 要求 20 秒一次心跳
                    last_ping = time.monotonic()
                continue
            if raw == "pong":
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if self.exchange == "binance":
                bars = [b] if (b := parse_binance(msg, self.symbol, self.timeframe)) else []
            elif self.exchange == "bybit":
                if msg.get("op") == "subscribe" and msg.get("success") is False:
                    raise RuntimeError(f"Bybit 订阅失败：{msg.get('ret_msg')}")
                bars = parse_bybit(msg, self.symbol, self.timeframe)
            else:
                bars = parse_okx(msg, self.symbol, self.timeframe)
            if msg.get("event") == "error":
                raise RuntimeError(f"OKX 订阅失败：{msg.get('msg')}")
            for bar in bars:
                self._ingest(bar)

    async def _after_connect(self) -> None:
        """重连后补齐断线期间已收盘的 bar（REST），并记录补了几根。"""
        if self.backfill is None:
            return
        with self._lock:
            if not self.closed:
                return  # 首次连接：预热历史由调用方用 REST 加载，这里不补
            last = self.closed[-1].open_time
        step = TIMEFRAME_MS[self.timeframe]
        loop = asyncio.get_running_loop()
        try:
            bars = await loop.run_in_executor(None, self.backfill, self.symbol, self.timeframe, last + step, now_ms())
        except Exception as exc:
            with self._lock:
                self.stats.note("warning", f"重连后补数失败：{exc}")
            return
        n = 0
        for b in bars:
            if b.closed and b.open_time > last:
                self._ingest(b, backfilled=True)
                n += 1
        if n:
            with self._lock:
                self.stats.backfilled += n
                self.stats.note("warning", f"断线期间缺 {n} 根已收盘 bar，已用 REST 补齐")

    def _ingest(self, bar: Bar, *, backfilled: bool = False) -> None:
        with self._cond:
            self.stats.messages += 1
            self.stats.last_message_ts = now_ms()
            if bar.closed:
                last_open = self.closed[-1].open_time if self.closed else -1
                if bar.open_time > last_open:
                    self.closed.append(bar)
                    self.stats.last_closed_ts = bar.close_time
                    if not backfilled:
                        self.stats.lag_ms = max(0, now_ms() - bar.close_time)
                    self._cond.notify_all()
                if self.forming is not None and self.forming.open_time <= bar.open_time:
                    self.forming = None
            elif self.forming is None or bar.open_time >= self.forming.open_time:
                self.forming = bar

    # ---- 读取 ----
    def closed_since(self, open_time: int) -> list[Bar]:
        with self._lock:
            return [b for b in self.closed if b.open_time > open_time]

    def wait_closed(self, after_open_time: int, timeout: float) -> list[Bar]:
        with self._cond:
            self._cond.wait_for(
                lambda: bool(self.closed) and self.closed[-1].open_time > after_open_time, timeout=timeout
            )
            return [b for b in self.closed if b.open_time > after_open_time]

    def last_price(self) -> float | None:
        with self._lock:
            if self.forming is not None:
                return self.forming.close
            return self.closed[-1].close if self.closed else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            f = self.forming
            return {
                "exchange": self.exchange,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "forming": bar_dict(f) if f is not None else None,
                **self.stats.to_dict(),
            }


# ---------------------------------------------------------------------------
# 网关：按 (交易所, 标的, 周期) 共享连接
# ---------------------------------------------------------------------------
class MarketGateway:
    def __init__(self, backfill_factory: Callable[[str], Backfill | None] | None = None) -> None:
        self._streams: dict[tuple[str, str, str], BarStream] = {}
        self._refs: dict[tuple[str, str, str], int] = {}
        self._lock = threading.Lock()
        self._backfill_factory = backfill_factory

    def subscribe(self, exchange: str, symbol: str, timeframe: str) -> BarStream:
        key = (exchange, symbol, timeframe)
        with self._lock:
            s = self._streams.get(key)
            if s is None:
                bf = self._backfill_factory(exchange) if self._backfill_factory else None
                s = BarStream(exchange, symbol, timeframe, backfill=bf).start()
                self._streams[key] = s
            self._refs[key] = self._refs.get(key, 0) + 1
            return s

    def release(self, exchange: str, symbol: str, timeframe: str) -> None:
        key = (exchange, symbol, timeframe)
        with self._lock:
            n = self._refs.get(key, 0) - 1
            if n > 0:
                self._refs[key] = n
                return
            self._refs.pop(key, None)
            s = self._streams.pop(key, None)
        if s is not None:
            s.stop()

    def get(self, exchange: str, symbol: str, timeframe: str) -> BarStream | None:
        return self._streams.get((exchange, symbol, timeframe))

    def status(self) -> list[dict[str, Any]]:
        return [s.snapshot() for s in list(self._streams.values())]

    def close(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
            self._refs.clear()
        for s in streams:
            s.stop()


_GATEWAY: MarketGateway | None = None
_GW_LOCK = threading.Lock()


def gateway(backfill_factory: Callable[[str], Backfill | None] | None = None) -> MarketGateway:
    """进程内单例。worker 进程里所有实时模拟共享。"""
    global _GATEWAY
    with _GW_LOCK:
        if _GATEWAY is None:
            _GATEWAY = MarketGateway(backfill_factory)
        return _GATEWAY


# ---------------------------------------------------------------------------
# qrun 用的多标的行情源
# ---------------------------------------------------------------------------
class StreamFeed:
    """把几条 BarStream 拼成 qrun 需要的接口：closed_bars(wait) / marks()。"""

    def __init__(self, exchange: str, securities: list[str], timeframe: str) -> None:
        from ..exchanges import public_adapter

        adapter = public_adapter(exchange)
        self.streams = {
            s: BarStream(exchange, s, timeframe, backfill=lambda sym, tf, a, b: adapter.fetch_bars(sym, tf, a, b))
            for s in securities
        }
        self.seen = {s: 0 for s in securities}

    def describe(self) -> str:
        return "WebSocket 推送，断线自动重连并用 REST 补数"

    def start(self) -> None:
        for s in self.streams.values():
            s.start()
        deadline = time.time() + 15
        while time.time() < deadline and not all(s.stats.connected for s in self.streams.values()):
            time.sleep(0.2)
        if not all(s.stats.connected for s in self.streams.values()):
            err = next((s.stats.error for s in self.streams.values() if s.stats.error), "连接超时")
            self.close()
            raise RuntimeError(err)

    def closed_bars(self, wait: float) -> dict[str, list[Bar]]:
        first = next(iter(self.streams))
        self.streams[first].wait_closed(self.seen[first], wait)
        out = {}
        for sym, st in self.streams.items():
            bars = st.closed_since(self.seen[sym])
            if bars:
                self.seen[sym] = bars[-1].open_time
                out[sym] = bars
        return out

    def marks(self) -> dict[str, float]:
        return {s: p for s, st in self.streams.items() if (p := st.last_price()) is not None}

    def close(self) -> None:
        for s in self.streams.values():
            s.stop()
