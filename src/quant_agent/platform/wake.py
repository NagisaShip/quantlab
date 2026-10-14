"""worker 唤醒与短期状态（Redis）。

数据库是任务状态的唯一真相；Redis 只做两件事：
1. 有新任务 / 命令时立即叫醒 worker（LPUSH + BLPOP），否则 worker 每几秒轮询一次数据库；
2. worker 存活心跳（带过期时间的键），健康页据此显示有几个 worker 在线。

没有配置 `QAGENT_REDIS_URL` 时退化为进程内事件 + 数据库轮询，功能不变，只是唤醒有几秒延迟。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("quantlab.wake")
KEY = "qagent:wake"
WORKER_KEY = "qagent:worker:"


class LocalWaker:
    kind = "local"

    def __init__(self) -> None:
        self._event = threading.Event()
        self._workers: dict[str, tuple[float, dict]] = {}

    def notify(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> bool:
        hit = self._event.wait(timeout)
        self._event.clear()
        return hit

    def worker_alive(self, worker: str, info: dict[str, Any], ttl: int = 30) -> None:
        self._workers[worker] = (time.time() + ttl, info)

    def workers(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{"id": k, **v[1]} for k, v in self._workers.items() if v[0] > now]


class RedisWaker:
    kind = "redis"

    def __init__(self, url: str) -> None:
        import redis  # 仅在配置了 Redis 时导入

        self.r = redis.Redis.from_url(url, socket_timeout=10, socket_connect_timeout=3, decode_responses=True)
        self.r.ping()

    def notify(self) -> None:
        try:
            self.r.lpush(KEY, "1")
            self.r.ltrim(KEY, 0, 100)
        except Exception as exc:  # Redis 不可用不影响正确性（worker 会轮询数据库）
            log.warning("Redis 唤醒失败：%s", exc)

    def wait(self, timeout: float) -> bool:
        try:
            got = self.r.blpop([KEY], timeout=max(1, int(timeout)))
            if got:
                self.r.delete(KEY)  # 合并多次唤醒
            return bool(got)
        except Exception:
            time.sleep(min(timeout, 2))
            return False

    def worker_alive(self, worker: str, info: dict[str, Any], ttl: int = 30) -> None:
        try:
            self.r.set(WORKER_KEY + worker, json.dumps(info), ex=ttl)
        except Exception:
            pass

    def workers(self) -> list[dict[str, Any]]:
        try:
            out = []
            for k in self.r.scan_iter(WORKER_KEY + "*", count=100):
                v = self.r.get(k)
                if v:
                    out.append({"id": k[len(WORKER_KEY) :], **json.loads(v)})
            return out
        except Exception:
            return []


_LOCAL = LocalWaker()


def build() -> Any:
    url = os.environ.get("QAGENT_REDIS_URL", "").strip()
    if url:
        try:
            return RedisWaker(url)
        except Exception as exc:
            log.warning("无法连接 Redis（%s），退化为数据库轮询：%s", url, exc)
    return _LOCAL
