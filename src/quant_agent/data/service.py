"""数据服务：行情获取 + 本地缓存 + 合成数据兜底。

真实环境走交易所适配器；网络不可达时用可复现的合成序列，
保证前端与回测流程可以离线演示（明确标注为合成数据）。
"""

from __future__ import annotations

import functools
import hashlib
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..exchanges.base import Bar
from .cache import BarCache, FundingCache
from .quality import TIMEFRAME_MS, DataSet


@dataclass
class DataQuality:
    symbol: str
    timeframe: str
    bars: int
    gaps: int
    duplicates: int
    unclosed: int
    source: str
    coverage_start: int
    coverage_end: int

    def to_dict(self) -> dict:
        return self.__dict__


def check_quality(bars: list[Bar], *, symbol: str, timeframe: str, source: str, step_ms: int) -> DataQuality:
    """标记重复、乱序、缺失（对应 plan.md 第 4 章数据质量要求）。"""
    seen: set[int] = set()
    duplicates = 0
    gaps = 0
    unclosed = 0
    prev = None
    for b in bars:
        if b.open_time in seen:
            duplicates += 1
        seen.add(b.open_time)
        if not b.closed:
            unclosed += 1
        if prev is not None and b.open_time - prev > step_ms * 1.5:
            gaps += 1
        prev = b.open_time
    return DataQuality(
        symbol=symbol,
        timeframe=timeframe,
        bars=len(bars),
        gaps=gaps,
        duplicates=duplicates,
        unclosed=unclosed,
        source=source,
        coverage_start=bars[0].open_time if bars else 0,
        coverage_end=bars[-1].close_time if bars else 0,
    )


# 来源标签，界面上直接展示，不允许把两类数据混成一句"回测结果"
SOURCE_LABELS = {
    "synthetic": "教学合成数据",
    "exchange": "交易所真实历史行情",
    "csv": "用户 CSV · 来源自行确认",
}

# 少于这么多根就不算"取到了行情"，宁可走合成数据并明确标注
MIN_USABLE_BARS = 50


# 合成数据的起始价按币种给个量级相近的数，避免 ETH 也从 6 万起步让人误读
SYNTHETIC_START = {
    "BTC": 60_000.0,
    "ETH": 3_000.0,
    "SOL": 150.0,
    "BNB": 600.0,
    "XRP": 0.6,
    "DOGE": 0.15,
    "ADA": 0.45,
    "TRX": 0.12,
    "LTC": 80.0,
    "AVAX": 30.0,
    "LINK": 15.0,
    "DOT": 6.0,
}


def synthetic_bars(
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    n: int = 1500,
    *,
    start_price: float | None = None,
    seed: str | None = None,
    end: int | None = None,
    anchored: bool = False,
) -> list[Bar]:
    """可复现的合成行情（几何布朗运动 + 轻微趋势切换）。

    seed 默认由 symbol+timeframe 派生，保证同一请求得到同一序列。
    `end` 为最后一根 bar 的收盘时间（默认当前整点）。

    anchored=False（默认）：序列从起始价开始走，只和根数有关，和取数时刻无关，测试与示例结果可复现。
    anchored=True：按 bar 的绝对时间生成，同一时刻任何区间看到的价格都一样；平台页面取行情时用这个。
    """
    step = TIMEFRAME_MS.get(timeframe, 3_600_000)
    if symbol.upper().endswith(":PERP"):
        # 合成永续 = 同一条合成现货 × (1 + 小基差)，这样套利示例里两条腿走势一致
        spot = symbol.split(":")[0]
        base = synthetic_bars(spot, timeframe, n, start_price=start_price, seed=seed, end=end, anchored=anchored)
        brng = random.Random(int(hashlib.sha256(f"basis:{symbol}:{timeframe}".encode()).hexdigest()[:12], 16))
        out: list[Bar] = []
        basis = 0.0002
        for b in base:
            basis = max(-0.0008, min(0.0008, basis * 0.95 + brng.gauss(0, 0.00008)))
            k = 1 + basis
            out.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=b.open_time,
                    close_time=b.close_time,
                    open=round(b.open * k, 2),
                    high=round(b.high * k, 2),
                    low=round(b.low * k, 2),
                    close=round(b.close * k, 2),
                    volume=b.volume,
                    closed=True,
                )
            )
        return out
    if seed is None:
        seed = f"{symbol}:{timeframe}"
    if start_price is None:
        start_price = SYNTHETIC_START.get(symbol.split("/")[0].upper(), 100.0)

    if not anchored:
        return _synthetic_walk(symbol, timeframe, n, step, start_price, seed, end)

    now = int(time.time() * 1000) if end is None else int(end)
    now = now - (now % step)
    k0 = now // step - n  # 第一根 bar 的绝对序号（自 1970 起第几根）
    base = math.log(start_price)
    blocks: dict[int, tuple[float, list[float], list[float], list[float], list[float]]] = {}

    def block(b: int):
        if b not in blocks:
            blocks[b] = _synthetic_block(seed, b)
        return blocks[b]

    def log_price(k: int) -> float:
        """第 k 根 bar 开盘时刻的对数价格：只取决于 k，不取决于这次取多少根。"""
        b, j = divmod(k, _SYN_BLOCK)
        return base + _synthetic_trend(seed, k) + (block(b)[1][j - 1] if j else 0.0)

    bars: list[Bar] = []
    lp = log_price(k0)
    for k in range(k0, k0 + n):
        nxt = log_price(k + 1)
        _vol, _bridge, wick_h, wick_l, vols = block(k // _SYN_BLOCK)
        j = k % _SYN_BLOCK
        open_p, close_p = math.exp(lp), math.exp(nxt)
        ot = k * step
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=ot,
                close_time=ot + step,
                open=round(open_p, 2),
                high=round(max(open_p, close_p) * (1 + wick_h[j]), 2),
                low=round(min(open_p, close_p) * (1 - wick_l[j]), 2),
                close=round(close_p, 2),
                volume=round(vols[j], 2),
                closed=True,
            )
        )
        lp = nxt
    return bars


# 合成行情按 bar 的绝对序号生成：同一币种、同一周期，任何页面、任何区间在同一时刻看到的价格都一样。
# 价格 = 起始价 × exp(慢趋势 + 块内布朗桥)。慢趋势是几层平滑噪声（240 / 1200 / 6000 根一个节点），
# 有界，不会跑到离起始价太远；块内布朗桥在每 240 根的两端归零，提供逐根波动，每块随机选一档波动率。
_SYN_BLOCK = 240
_SYN_OCTAVES = ((240, 0.05), (1200, 0.12), (6000, 0.25))


def _syn_rng(*parts: object) -> random.Random:
    return random.Random(int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:16], 16))


@functools.lru_cache(maxsize=65536)
def _syn_knot(seed: str, period: int, amp: float, i: int) -> float:
    return _syn_rng(seed, "o", period, i).gauss(0, amp)


def _synthetic_trend(seed: str, k: int) -> float:
    total = 0.0
    for period, amp in _SYN_OCTAVES:
        i, r = divmod(k, period)
        a = _syn_knot(seed, period, amp, i)
        b = _syn_knot(seed, period, amp, i + 1)
        w = (1 - math.cos(math.pi * r / period)) / 2
        total += a + (b - a) * w
    return total


@functools.lru_cache(maxsize=4096)
def _synthetic_block(seed: str, b: int):
    rng = _syn_rng(seed, "b", b)
    vol = rng.choice([0.008, 0.012, 0.02])
    walk, x = [], 0.0
    for _ in range(_SYN_BLOCK):
        x += vol * rng.gauss(0, 1)
        walk.append(x)
    bridge = [w - (i + 1) / _SYN_BLOCK * x for i, w in enumerate(walk)]
    wick_h = [abs(rng.gauss(0, 0.003)) for _ in range(_SYN_BLOCK)]
    wick_l = [abs(rng.gauss(0, 0.003)) for _ in range(_SYN_BLOCK)]
    vols = [abs(rng.gauss(120, 40)) + 10 for _ in range(_SYN_BLOCK)]
    return vol, bridge, wick_h, wick_l, vols


def _synthetic_walk(
    symbol: str, timeframe: str, n: int, step: int, start_price: float, seed: str, end: int | None
) -> list[Bar]:
    """从起始价开始的几何布朗运动 + 每 240 根切换一次趋势。只和根数有关。"""
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16))

    now = int(time.time() * 1000) if end is None else int(end)
    now = now - (now % step)
    start = now - n * step

    price = start_price
    bars: list[Bar] = []
    drift = 0.00005
    vol = 0.012
    for i in range(n):
        if i % 240 == 0:  # 每 240 根切换一次趋势，制造震荡/趋势交替
            drift = rng.choice([0.00012, 0.00002, -0.00009, 0.00007])
            vol = rng.choice([0.008, 0.012, 0.02])
        shock = rng.gauss(0, 1)
        ret = drift + vol * shock
        open_p = price
        close_p = max(1.0, open_p * math.exp(ret))
        high = max(open_p, close_p) * (1 + abs(rng.gauss(0, 0.003)))
        low = min(open_p, close_p) * (1 - abs(rng.gauss(0, 0.003)))
        ot = start + i * step
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=ot,
                close_time=ot + step,
                open=round(open_p, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(close_p, 2),
                volume=round(abs(rng.gauss(120, 40)) + 10, 2),
                closed=True,
            )
        )
        price = close_p
    return bars


FUNDING_MS = 8 * 3_600_000


def synthetic_funding(symbol: str, start: int, end: int) -> list[tuple[int, str]]:
    """可复现的合成资金费率：每 8 小时一次，均值约万分之一、偶尔转负。只用于离线演示和测试。"""
    rows: list[tuple[int, str]] = []
    t = (int(start) // FUNDING_MS + 1) * FUNDING_MS
    while t <= int(end):
        rng = random.Random(int(hashlib.sha256(f"funding:{symbol}:{t}".encode()).hexdigest()[:12], 16))
        rate = 0.0001 + rng.gauss(0, 0.00007)
        if abs(rate) < 1e-6:
            rate = 1e-6
        rows.append((t, f"{rate:.8f}"))
        t += FUNDING_MS
    return rows


def stamp(bars: list[Bar], *, source: str, exchange: str, symbol: str, timeframe: str, raw_ref: str) -> list[Bar]:
    """事件审计字段：event_ts=收盘时间、receive_ts=平台收到时间、sequence=来源内单调序号（按 bar 序号）、
    source、instrument_id=交易所:标的、raw_ref=原始请求引用（附本批数据摘要，可复核）。"""
    import hashlib

    now = int(time.time() * 1000)
    step = TIMEFRAME_MS.get(timeframe, 3_600_000)
    digest = hashlib.sha256(
        "".join(f"{b.open_time},{b.open},{b.high},{b.low},{b.close},{b.volume};" for b in bars).encode()
    ).hexdigest()[:16]
    for b in bars:
        if not b.receive_ts:
            b.receive_ts = now
        b.sequence = b.sequence or b.open_time // step
        b.source = b.source or source
        b.instrument_id = b.instrument_id or f"{exchange}:{symbol}"
        b.raw_ref = b.raw_ref or f"{raw_ref}#sha256:{digest}"
    return bars


class DataUnavailable(RuntimeError):
    """真实行情不可用且调用方要求只用真实行情。"""


class MarketDataService:
    """行情服务：本地缓存 → 交易所补缺 → （可选）合成数据兜底。

    `source` 取值：
    - ``auto``：优先真实行情，失败时回退合成数据并明确标注（默认，保证离线也能演示）；
    - ``exchange``：只用真实行情，失败直接报错（实验三口径：不悄悄换成合成数据）；
    - ``synthetic``：只用教学合成数据。
    """

    def __init__(
        self,
        adapters: dict | None = None,
        *,
        allow_synthetic: bool | None = None,
        cache_root: str | Path | None = None,
    ) -> None:
        import os

        self.adapters = adapters or {}
        # 默认停用合成行情（QAGENT_ALLOW_SYNTHETIC=1 才允许，测试与离线演示用）
        if allow_synthetic is None:
            allow_synthetic = os.environ.get("QAGENT_ALLOW_SYNTHETIC", "0") == "1"
        self.allow_synthetic = allow_synthetic
        self.cache = BarCache(cache_root) if cache_root else None
        self.funding_cache = FundingCache(cache_root) if cache_root else None
        self.last_error: dict[str, str] = {}

    def get_bars(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        *,
        limit: int = 1500,
        exchange: str = "binance",
        start: int | None = None,
        end: int | None = None,
        source: str = "auto",
    ) -> tuple[list[Bar], str]:
        """返回 (bars, source)。只返回**已收盘**的 bar。source 为 synthetic / exchange。"""
        step = TIMEFRAME_MS.get(timeframe, 3_600_000)
        now = int(time.time() * 1000)
        end = min(int(end), now) if end else now
        start = int(start) if start else end - limit * step
        if start >= end:
            raise ValueError("开始时间必须早于结束时间")
        expected = max(1, (end - start) // step)

        if source not in {"auto", "exchange", "synthetic"}:
            raise ValueError(f"未知行情来源：{source}")
        if source == "synthetic" and not self.allow_synthetic:
            raise DataUnavailable(f"未接入数据：合成行情已停用（{symbol} {timeframe}），请同步真实行情")
        if source != "synthetic":
            adapter = self.adapters.get(exchange)
            error = f"未配置交易所适配器 {exchange}" if adapter is None else ""
            if adapter is not None:
                try:
                    bars = self._fetch(adapter, exchange, symbol, timeframe, start, end)
                    bars = [b for b in bars if b.closed and start <= b.open_time and b.close_time <= end]
                    # 最新一根还没收盘会被过滤掉：请求 N 根时通常只拿到 N-1 根，按 90% 判断够不够用，
                    # 否则短请求（例如 25 根）永远判为「取不到」
                    if len(bars) >= min(MIN_USABLE_BARS, max(1, int(expected * 0.9))):
                        self.last_error.pop(exchange, None)
                        return bars, "exchange"
                    error = f"{exchange} 只返回了 {len(bars)} 根 {timeframe} 行情"
                except Exception as exc:  # 网络、地域限制、交易对不存在……都在这里
                    error = f"{type(exc).__name__}: {exc}"[:300]
            self.last_error[exchange] = error
            if source == "exchange" or not self.allow_synthetic:
                raise DataUnavailable(f"未接入数据（{exchange} {symbol} {timeframe}）：{error}")
        n = int(min(expected, 20_000))
        bars = synthetic_bars(symbol, timeframe, n, end=end, anchored=True)
        return stamp(
            bars,
            source="synthetic",
            exchange="synthetic",
            symbol=symbol,
            timeframe=timeframe,
            raw_ref=f"synthetic:{symbol}:{timeframe}:end={end}",
        ), "synthetic"

    def _pull(self, adapter: Any, exchange: str, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        """向交易所拉一段 K 线，并给每根 bar 打上事件审计字段。"""
        bars = adapter.fetch_bars(symbol, timeframe, start, end)
        return stamp(
            bars,
            source=f"{exchange}:rest",
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            raw_ref=f"{exchange}:klines?symbol={symbol}&interval={timeframe}&start={start}&end={end}",
        )

    def _fetch(self, adapter: Any, exchange: str, symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
        """缓存优先，只向交易所补拉缺失的头尾。"""
        step = TIMEFRAME_MS.get(timeframe, 3_600_000)
        if self.cache is None:
            return self._pull(adapter, exchange, symbol, timeframe, start, end)
        cached = self.cache.load(exchange, symbol, timeframe)
        fresh: list[Bar] = []
        if not cached:
            fresh = self._pull(adapter, exchange, symbol, timeframe, start, end)
        else:
            first, last = cached[0].open_time, cached[-1].close_time
            if start < first - step // 2:
                fresh += self._pull(adapter, exchange, symbol, timeframe, start, first)
            if end > last + step // 2:
                fresh += self._pull(adapter, exchange, symbol, timeframe, last, end)
        if fresh:
            merged = self.cache.merge(exchange, symbol, timeframe, fresh)
            # 未收盘的最新一根不进缓存，但本次调用可以看到它（随后会被过滤）
            tail = [b for b in fresh if not b.closed]
            return merged + tail
        return cached

    def sync(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: int,
        end: int | None = None,
        exchange: str = "binance",
        chunk_bars: int = 1000,
    ) -> dict[str, Any]:
        """把 [start, end] 的真实 K 线同步进本地缓存：分段拉取，补齐头、尾以及**中间的缺口**，返回覆盖情况。

        与 get_bars 的「缓存优先、只补头尾」不同，这里会扫描缓存里的断档逐段补拉；
        每段最多 chunk_bars 根，单段失败不影响其他段（错误汇总在结果里）。
        """
        step = TIMEFRAME_MS.get(timeframe, 3_600_000)
        now = int(time.time() * 1000)
        end = min(int(end or now), now)
        start = int(start)
        adapter = self.adapters.get(exchange)
        if adapter is None:
            return {"ok": False, "symbol": symbol, "timeframe": timeframe, "error": f"未配置交易所适配器 {exchange}"}
        cached = self.cache.load(exchange, symbol, timeframe) if self.cache else []
        have = sorted(b.open_time for b in cached if start - step <= b.open_time <= end)
        missing: list[tuple[int, int]] = []
        if not have:
            missing.append((start, end))
        else:
            if have[0] - start > step:
                missing.append((start, have[0]))
            for a, b in zip(have, have[1:], strict=False):
                if b - a > step * 1.5:
                    missing.append((a + step, b))
            if end - have[-1] > 2 * step:
                missing.append((have[-1], end))
        fetched, errors = 0, []
        for a, b in missing:
            t = a
            while t < b:
                u = min(b, t + chunk_bars * step)
                try:
                    got = self._pull(adapter, exchange, symbol, timeframe, t, u)
                    if self.cache is not None and got:
                        self.cache.merge(exchange, symbol, timeframe, got)
                    fetched += len(got)
                except Exception as exc:
                    errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
                    if len(errors) >= 3:
                        break
                t = u
            if len(errors) >= 3:
                break
        rows = [
            b
            for b in (self.cache.load(exchange, symbol, timeframe) if self.cache else [])
            if start <= b.open_time <= end
        ]
        gaps = sum(1 for a, b in zip(rows, rows[1:], strict=False) if b.open_time - a.open_time > step * 1.5)
        expected = max(1, (end - start) // step)
        if not errors:
            self.last_error.pop(exchange, None)
        return {
            "ok": bool(rows),
            "symbol": symbol,
            "timeframe": timeframe,
            "exchange": exchange,
            "fetched": fetched,
            "bars": len(rows),
            "expected": expected,
            "coverage": round(min(1.0, len(rows) / expected), 3),
            "start_ts": rows[0].open_time if rows else None,
            "end_ts": rows[-1].close_time if rows else None,
            "gaps": gaps,
            "segments_filled": len(missing),
            "errors": errors,
        }

    def funding_rates(
        self,
        symbol: str,
        *,
        exchange: str = "binance",
        start: int,
        end: int,
        source: str = "auto",
    ) -> tuple[list[tuple[int, str]], str, str]:
        """永续合约在 (start, end] 内的已结算资金费率，返回 (rows, 来源, 错误)。rows = [(结算时刻, 费率字符串)]。

        来源同 get_bars：auto 取不到时用合成费率并标注；exchange 取不到就返回空和错误；synthetic 只用合成。
        """
        if not symbol.upper().endswith(":PERP"):
            return [], "", f"{symbol} 不是永续合约，没有资金费率"
        start, end = int(start), int(end)
        if source == "synthetic":
            if not self.allow_synthetic:
                return [], "exchange", "未接入数据：合成资金费率已停用"
            return synthetic_funding(symbol, start, end), "synthetic", ""
        error = ""
        adapter = self.adapters.get(exchange)
        if adapter is None or not hasattr(adapter, "fetch_funding_rates"):
            error = f"未配置支持资金费率的交易所适配器 {exchange}"
        else:
            try:
                if self.funding_cache is not None:
                    rows, lo, hi = self.funding_cache.load(exchange, symbol)
                    if rows and lo <= start and end <= hi:
                        return [(t, r) for t, r in rows if start < t <= end], "exchange", ""
                got = adapter.fetch_funding_rates(symbol, start, end)
                rows = sorted({int(f.ts): str(f.rate) for f in got if start < int(f.ts) <= end}.items())
                if self.funding_cache is not None:
                    # 未来的结算点还没发生，覆盖范围只记到当前
                    self.funding_cache.merge(exchange, symbol, rows, start, min(end, int(time.time() * 1000)))
                if rows:
                    return rows, "exchange", ""
                error = f"{exchange} 没有返回 {symbol} 的资金费率"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:300]
        if source == "exchange" or not self.allow_synthetic:
            return [], "exchange", error
        return synthetic_funding(symbol, start, end), "synthetic", error

    def ticker(self, symbol: str, exchange: str = "binance") -> dict[str, Any]:
        adapter = self.adapters.get(exchange)
        if adapter is None:
            raise DataUnavailable(f"未配置交易所适配器 {exchange}")
        return adapter.fetch_ticker(symbol)

    def dataset(
        self,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        *,
        limit: int = 1500,
        exchange: str = "binance",
        start: int | None = None,
        end: int | None = None,
        source: str = "auto",
    ) -> DataSet:
        """带来源标签与摘要的行情，供 run 冻结。

        若交易所实际返回的根数明显少于请求量（历史深度不够、或误连了测试环境），
        会在摘要里写明缺口——不能让一段只有几十小时的行情被当成"一个月的回测"。
        """
        bars, source = self.get_bars(
            symbol, timeframe, limit=limit, exchange=exchange, start=start, end=end, source=source
        )
        if start:
            limit = max(1, ((end or int(time.time() * 1000)) - int(start)) // TIMEFRAME_MS.get(timeframe, 3_600_000))
        ds = DataSet(
            symbol=symbol,
            timeframe=timeframe,
            bars=bars,
            source=source,
        ).freeze(SOURCE_LABELS.get(source, source))
        ds.exchange = exchange if source == "exchange" else ""
        step_hours = TIMEFRAME_MS.get(timeframe, 3_600_000) / 3_600_000
        ds.coverage_hours = round(len(bars) * step_hours, 2)
        ds.requested_bars = limit
        if source == "exchange" and len(bars) < limit * 0.8:
            ds.coverage_warning = (
                f"交易所只返回了 {len(bars)} 根 {timeframe} 行情（请求 {limit} 根），"
                f"实际覆盖约 {ds.coverage_hours:.0f} 小时；"
                "相关观察窗口会标注为「进行中」，请不要把它当作完整窗口的结果"
            )
        return ds

    def quality(self, symbol: str, timeframe: str, *, limit: int = 1500, exchange: str = "binance") -> DataQuality:
        bars, source = self.get_bars(symbol, timeframe, limit=limit, exchange=exchange)
        return check_quality(
            bars,
            symbol=symbol,
            timeframe=timeframe,
            source=source,
            step_ms=TIMEFRAME_MS.get(timeframe, 3_600_000),
        )
