"""运行节点的两件基础设施：交易所时钟校准、请求限频。

**时钟**：签名请求带时间戳，本机时钟偏差超过交易所的接收窗口（Binance 10 秒、Bybit 5 秒、OKX 30 秒）
就会被拒。`ClockSync.measure()` 用交易所公开时间接口测偏差（取往返时间的中点），之后所有签名
都用 `now_ms()`（本机时间 + 偏差）。偏差超过 1 秒在健康页提示，超过 5 秒告警——说明服务器没开
NTP 同步（`timedatectl set-ntp true` 或装 chrony）。

**限频**：每家交易所一个令牌桶，所有签名请求先取令牌；收到 429 / 418 时按 Retry-After 整桶暂停，
期间的请求在本地等待（最多 `max_wait` 秒，超过就当限频拒单，不会把请求打到交易所被封 IP）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

TIME_ENDPOINTS: dict[str, tuple[str, str]] = {
    "binance": ("https://api.binance.com", "/api/v3/time"),
    "okx": ("https://www.okx.com", "/api/v5/public/time"),
    "bybit": ("https://api.bybit.com", "/v5/market/time"),
}
# 每秒请求数：取各家公开限额的一半左右，给同一 IP 上的行情请求留余量
RATES: dict[str, float] = {"binance": 10.0, "okx": 8.0, "bybit": 8.0}
WARN_MS = 1_000
ALERT_MS = 5_000


def _server_ms(exchange: str, body: Any) -> int:
    if exchange == "binance":
        return int(body["serverTime"])
    if exchange == "okx":
        return int(body["data"][0]["ts"])
    if exchange == "bybit":
        r = body.get("result") or {}
        if r.get("timeNano"):
            return int(r["timeNano"]) // 1_000_000
        return int(r["timeSecond"]) * 1000
    raise ValueError(exchange)


@dataclass
class ClockSync:
    offsets: dict[str, int] = field(default_factory=dict)  # 交易所时间 − 本机时间（毫秒）
    measured_at: dict[str, int] = field(default_factory=dict)
    rtt: dict[str, int] = field(default_factory=dict)

    def measure(self, exchange: str, *, transport: httpx.BaseTransport | None = None, samples: int = 3) -> int:
        base, path = TIME_ENDPOINTS[exchange]
        best: tuple[int, int] | None = None
        with httpx.Client(base_url=base, timeout=5.0, transport=transport) as c:
            for _ in range(samples):
                t0 = time.time() * 1000
                body = c.get(path).json()
                t1 = time.time() * 1000
                off = _server_ms(exchange, body) - int((t0 + t1) / 2)
                rtt = int(t1 - t0)
                if best is None or rtt < best[1]:
                    best = (off, rtt)
        assert best is not None
        self.offsets[exchange], self.rtt[exchange] = best
        self.measured_at[exchange] = int(time.time() * 1000)
        return best[0]

    def now_ms(self, exchange: str = "") -> int:
        return int(time.time() * 1000) + self.offsets.get(exchange, 0)

    def status(self) -> list[dict[str, Any]]:
        out = []
        for ex, off in sorted(self.offsets.items()):
            level = "bad" if abs(off) >= ALERT_MS else "warn" if abs(off) >= WARN_MS else "ok"
            out.append(
                {
                    "exchange": ex,
                    "offset_ms": off,
                    "rtt_ms": self.rtt.get(ex),
                    "level": level,
                    "measured_at": self.measured_at.get(ex),
                }
            )
        return out


CLOCK = ClockSync()


class RateLimited(RuntimeError):
    """本地等待超过上限：这笔请求没有发出去。"""


class Bucket:
    def __init__(self, rate: float, burst: float | None = None) -> None:
        self.rate = float(rate)
        self.cap = float(burst if burst is not None else max(1.0, rate))
        self.tokens = self.cap
        self.at = time.monotonic()
        self.paused_until = 0.0
        self.waited = 0.0
        self.hits_429 = 0
        self.lock = threading.Lock()

    def acquire(self, *, max_wait: float = 5.0, sleep: Any = time.sleep) -> float:
        """取一个令牌，返回等了多久。"""
        waited = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                if now < self.paused_until:
                    need = self.paused_until - now
                else:
                    self.tokens = min(self.cap, self.tokens + (now - self.at) * self.rate)
                    self.at = now
                    if self.tokens >= 1:
                        self.tokens -= 1
                        self.waited += waited
                        return waited
                    need = (1 - self.tokens) / self.rate
            if waited + need > max_wait:
                raise RateLimited(f"本地限频：需要再等 {need:.1f} 秒，超过上限 {max_wait:.0f} 秒，这笔请求没有发出")
            sleep(need)
            waited += need

    def penalize(self, seconds: float) -> None:
        with self.lock:
            self.hits_429 += 1
            self.paused_until = max(self.paused_until, time.monotonic() + max(1.0, float(seconds)))
            self.tokens = 0


_BUCKETS: dict[str, Bucket] = {}
_BL = threading.Lock()


def bucket(exchange: str) -> Bucket:
    with _BL:
        b = _BUCKETS.get(exchange)
        if b is None:
            b = _BUCKETS[exchange] = Bucket(RATES.get(exchange, 5.0))
        return b


def retry_after(resp: httpx.Response) -> float:
    try:
        return float(resp.headers.get("Retry-After") or 0) or (60.0 if resp.status_code == 418 else 5.0)
    except ValueError:
        return 5.0


def limits_status() -> list[dict[str, Any]]:
    with _BL:
        items = list(_BUCKETS.items())
    now = time.monotonic()
    return [
        {
            "exchange": ex,
            "rate_per_s": b.rate,
            "paused_s": round(max(0.0, b.paused_until - now), 1),
            "hits_429": b.hits_429,
            "waited_s": round(b.waited, 2),
        }
        for ex, b in items
    ]
