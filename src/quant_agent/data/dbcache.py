"""服务端数据库缓存（SQLite）：把对外部接口的请求结果存一段时间，重复请求不再打到交易所 / 搜索服务。

为什么不只靠 K 线缓存：K 线已经有按周期落盘的 BarCache（只存已收盘 bar、只补缺口），但行情摘要、
搜索结果、网页正文这类「同样的问题短时间内会被反复问」的请求，每次都打外部接口既慢又容易让服务器 IP 被限流甚至封禁。

- 键是 (命名空间, 键)，值是 JSON；每条带过期时间，过期后视为未命中（后台按需清理）；
- 线程安全：每次操作一个短连接 + WAL，多个后台回合同时读写也没问题；
- 统计命中 / 未命中，设置页可以看到缓存省了多少次外部请求。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    ns TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ns, key)
);
CREATE INDEX IF NOT EXISTS cache_expiry ON cache (expires_at);
"""

#: 单条值的上限：超过就不缓存（例如很长的网页正文），避免数据库膨胀
MAX_VALUE_BYTES = 2_000_000


class DbCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stats: dict[str, dict[str, int]] = {}
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _count(self, ns: str, what: str) -> None:
        with self._lock:
            row = self._stats.setdefault(ns, {"hits": 0, "misses": 0})
            row[what] += 1

    def get(self, ns: str, key: str) -> Any | None:
        now = time.time()
        try:
            with self._conn() as c:
                row = c.execute("SELECT value, expires_at FROM cache WHERE ns=? AND key=?", (ns, key)).fetchone()
                if row is None or row[1] < now:
                    self._count(ns, "misses")
                    return None
                c.execute("UPDATE cache SET hits = hits + 1 WHERE ns=? AND key=?", (ns, key))
        except sqlite3.Error:
            return None
        self._count(ns, "hits")
        return json.loads(row[0])

    def set(self, ns: str, key: str, value: Any, ttl: float) -> None:
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text.encode("utf-8")) > MAX_VALUE_BYTES or ttl <= 0:
            return
        now = time.time()
        try:
            with self._conn() as c:
                c.execute(
                    "INSERT INTO cache (ns, key, value, expires_at, created_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(ns, key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at, "
                    "created_at=excluded.created_at, hits=0",
                    (ns, key, text, now + ttl, now),
                )
                if int(now) % 50 == 0:  # 偶尔顺手清理过期条目
                    c.execute("DELETE FROM cache WHERE expires_at < ?", (now,))
        except sqlite3.Error:
            return

    def remember(self, ns: str, key: str, ttl: float, fn: Callable[[], Any], *, keep: Callable[[Any], bool] | None = None) -> Any:
        """命中就直接返回；未命中才调用 fn，结果满足 keep（默认：非空）时写入缓存。失败的结果不缓存。"""
        hit = self.get(ns, key)
        if hit is not None:
            return hit
        value = fn()
        if (keep or bool)(value):
            self.set(ns, key, value, ttl)
        return value

    def stats(self) -> dict[str, Any]:
        now = time.time()
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT ns, COUNT(*), SUM(LENGTH(value)), SUM(hits) FROM cache WHERE expires_at >= ? GROUP BY ns", (now,)
                ).fetchall()
        except sqlite3.Error:
            rows = []
        with self._lock:
            session = {k: dict(v) for k, v in self._stats.items()}
        return {
            "path": str(self.path),
            "namespaces": [
                {"ns": ns, "entries": n, "bytes": int(b or 0), "hits_stored": int(h or 0), **session.get(ns, {"hits": 0, "misses": 0})}
                for ns, n, b, h in rows
            ],
        }

    def clear(self, ns: str | None = None) -> int:
        with self._conn() as c:
            cur = c.execute("DELETE FROM cache WHERE ns=?", (ns,)) if ns else c.execute("DELETE FROM cache")
            return cur.rowcount


__all__ = ["DbCache"]
