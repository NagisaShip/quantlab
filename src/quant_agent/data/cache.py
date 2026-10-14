"""K 线本地缓存（历史行情快照）。

为什么要缓存：回测、参数优化、实时模拟会反复请求同一段历史。每次都打交易所接口
既慢、又容易触发限流，还会让"同一段区间两次回测结果不一样"（交易所偶尔回补数据）。
缓存后：
- 只有**已收盘**的 bar 进缓存；未收盘 bar 永远现取现用、用完即弃。
- 只补缺口：请求区间超出缓存覆盖时，只去交易所拉缺的那一头。
- 每根 bar 连同审计字段一起保存：receive_ts（平台收到的时间）、sequence（来源内序号）、
  raw_ref（原始响应引用：接口、参数与响应摘要）。

存储格式：安装了 pyarrow 时用 **Parquet**（列式、压缩，适合长历史；部署镜像默认安装），
否则退化为一个 (交易所, 标的, 周期) 一个 JSON 文件。两种格式可以互相读取迁移。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ..exchanges.base import Bar

MAX_CACHED_BARS = 200_000  # 1 小时线约 22 年；超出时丢掉最早的部分
COLUMNS = ["open_time", "close_time", "open", "high", "low", "close", "volume", "receive_ts", "sequence", "raw_ref"]

try:  # pragma: no cover - 取决于部署环境
    import pyarrow as pa
    import pyarrow.parquet as pq

    HAS_PARQUET = os.environ.get("QAGENT_BAR_FORMAT", "parquet") == "parquet"
except ImportError:  # pragma: no cover
    pa = pq = None
    HAS_PARQUET = False


def _key(exchange: str, symbol: str, timeframe: str) -> str:
    # Windows 文件名不能含冒号：永续 BTC/USDT:PERP 存成 BTC-USDT+PERP
    return f"{exchange}_{symbol.replace('/', '-').replace(':', '+')}_{timeframe}"


def _symbol_of(part: str) -> str:
    return part.replace("+", ":").replace("-", "/")


def _row(b: Bar) -> list[Any]:
    return [b.open_time, b.close_time, b.open, b.high, b.low, b.close, b.volume, b.receive_ts, b.sequence, b.raw_ref]


class BarCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.format = "parquet" if HAS_PARQUET else "json"

    def _path(self, exchange: str, symbol: str, timeframe: str, fmt: str | None = None) -> Path:
        return self.root / f"{_key(exchange, symbol, timeframe)}.{fmt or self.format}"

    def _read_rows(self, exchange: str, symbol: str, timeframe: str) -> list[list[Any]]:
        pqp, js = self._path(exchange, symbol, timeframe, "parquet"), self._path(exchange, symbol, timeframe, "json")
        if pq is not None and pqp.exists():
            try:
                d = pq.read_table(pqp).to_pydict()
                return [[d[c][i] if c in d else None for c in COLUMNS] for i in range(len(d["open_time"]))]
            except Exception:
                return []
        if js.exists():
            try:
                return json.loads(js.read_text(encoding="utf-8")).get("bars", [])
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def load(self, exchange: str, symbol: str, timeframe: str) -> list[Bar]:
        with self._lock:
            rows = self._read_rows(exchange, symbol, timeframe)
        out = []
        for r in rows:
            r = list(r) + [None] * (len(COLUMNS) - len(r))
            out.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    open_time=int(r[0]),
                    close_time=int(r[1]),
                    open=r[2],
                    high=r[3],
                    low=r[4],
                    close=r[5],
                    volume=r[6],
                    closed=True,
                    receive_ts=int(r[7] or 0),
                    sequence=int(r[8] or 0),
                    source=f"{exchange}:cache",
                    instrument_id=f"{exchange}:{symbol}",
                    raw_ref=r[9] or "",
                )
            )
        return out

    def merge(self, exchange: str, symbol: str, timeframe: str, bars: list[Bar]) -> list[Bar]:
        """把新 bar 并入缓存（只收已收盘的），返回合并后的完整序列。已有 bar 不被覆盖（首次收到的为准）。"""
        closed = [b for b in bars if b.closed]
        with self._lock:
            existing = {b.open_time: b for b in self.load(exchange, symbol, timeframe)}
            for b in closed:
                existing.setdefault(b.open_time, b)
            merged = [existing[k] for k in sorted(existing)][-MAX_CACHED_BARS:]
            rows = [_row(b) for b in merged]
            if self.format == "parquet":
                path = self._path(exchange, symbol, timeframe, "parquet")
                tmp = path.with_suffix(".tmp")
                cols = {c: [r[i] for r in rows] for i, c in enumerate(COLUMNS)}
                pq.write_table(pa.table(cols), tmp, compression="zstd")
                tmp.replace(path)
                old = self._path(exchange, symbol, timeframe, "json")
                if old.exists():
                    old.unlink()
            else:
                path = self._path(exchange, symbol, timeframe, "json")
                tmp = path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(
                        {
                            "exchange": exchange,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "columns": COLUMNS,
                            "bars": rows,
                        },
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                tmp.replace(path)
        return self.load(exchange, symbol, timeframe)

    def entries(self) -> list[dict[str, Any]]:
        """缓存清单，供数据中心展示。"""
        out: list[dict[str, Any]] = []
        for path in sorted([*self.root.glob("*.json"), *self.root.glob("*.parquet")]):
            parts = path.stem.split("_")
            if len(parts) < 3:
                continue
            exchange, symbol, timeframe = parts[0], _symbol_of(parts[1]), parts[2]
            rows = self._read_rows(exchange, symbol, timeframe)
            out.append(
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bars": len(rows),
                    "start_ts": rows[0][0] if rows else 0,
                    "end_ts": rows[-1][1] if rows else 0,
                    "bytes": path.stat().st_size,
                    "format": path.suffix.lstrip("."),
                }
            )
        return out

    def clear(self, exchange: str, symbol: str, timeframe: str) -> bool:
        hit = False
        with self._lock:
            for fmt in ("json", "parquet"):
                path = self._path(exchange, symbol, timeframe, fmt)
                if path.exists():
                    path.unlink()
                    hit = True
        return hit


class FundingCache:
    """永续合约资金费率的本地缓存：一个 (交易所, 合约) 一个 JSON 文件，放在 funding/ 子目录。

    记录已覆盖的区间 [start, end]：请求落在覆盖范围内就不再打交易所接口。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "funding"
        self._lock = threading.RLock()

    def _path(self, exchange: str, symbol: str) -> Path:
        return self.root / f"{_key(exchange, symbol, 'funding')}.json"

    def load(self, exchange: str, symbol: str) -> tuple[list[tuple[int, str]], int, int]:
        path = self._path(exchange, symbol)
        with self._lock:
            if not path.exists():
                return [], 0, 0
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return [], 0, 0
        return [(int(t), str(r)) for t, r in d.get("rows", [])], int(d.get("start", 0)), int(d.get("end", 0))

    def merge(self, exchange: str, symbol: str, rows: list[tuple[int, str]], start: int, end: int) -> None:
        old, lo, hi = self.load(exchange, symbol)
        merged = {t: r for t, r in old}
        for t, r in rows:
            merged.setdefault(int(t), str(r))
        if old and lo <= end and start <= hi:  # 区间相连才合并覆盖范围，否则以新区间为准
            start, end = min(start, lo), max(end, hi)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(exchange, symbol)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "exchange": exchange,
                        "symbol": symbol,
                        "start": start,
                        "end": end,
                        "rows": [[t, merged[t]] for t in sorted(merged)],
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            tmp.replace(path)
