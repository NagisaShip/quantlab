"""运行节点看门狗：节点失联告警、服务器时钟校准。

**失联**：worker 每 20 秒给运行中的实验续一次租约（60 秒）。看门狗跑在 API 进程里（worker 死了它还活着），
每 30 秒查一次：运行中 / 恢复中的常驻实验，最后一次心跳超过 `STALE_MS`（2 分钟）就发「运行节点失联」告警，
并且**自己把告警投递出去**——平时投递是 worker 做的，worker 挂了就没人发了。所以从断开到收到通知
最长 2 分钟 + 30 秒检查间隔 + 投递时间，在 3 分钟以内。

**恢复**：worker 容器由 docker 的 restart 策略拉起；租约过期后任务进入 RECOVERING，新 worker 从检查点
恢复。停机前送出、没拿到回报的订单按客户端订单号查单，不重下（见 api/tasks.py PaperDriver.start）。
worker 主循环自己也有一个看门狗：主循环卡住超过 `HANG_SECONDS` 就退出进程，让 docker 重启它。

**时钟**：worker 每 10 分钟用交易所公开时间接口测一次偏差（live/nodeguard.py），签名一律用交易所时间；
偏差超过 5 秒发「服务器时钟偏差」告警。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from .db import jload, now_ms
from .runs import LEASE_MS, RECOVERING, RUNNING

log = logging.getLogger(__name__)

STALE_MS = int(os.environ.get("QAGENT_STALE_MS", "120000"))
CHECK_SECONDS = 30.0
HANG_SECONDS = float(os.environ.get("QAGENT_HANG_SECONDS", "180"))
RESIDENT_KINDS = ("paper",)


def last_beat(run: dict[str, Any]) -> int:
    """租约到期时间 − 租约长度 = 最后一次续租的时刻。"""
    exp = int(run.get("lease_expires_at") or 0)
    return exp - LEASE_MS if exp else int(run.get("updated_at") or run.get("started_at") or 0)


def stale_runs(db: Any, *, now: int | None = None) -> list[dict[str, Any]]:
    at = now if now is not None else now_ms()
    rows = db.fetchall(
        f"SELECT * FROM runs WHERE state IN (?, ?) AND kind IN ({','.join('?' * len(RESIDENT_KINDS))})",
        (RUNNING, RECOVERING, *RESIDENT_KINDS),
    )
    out = []
    for r in rows:
        beat = last_beat(r)
        if at - beat >= STALE_MS:
            out.append({**r, "silent_ms": at - beat})
    return out


def check(services: Any, *, now: int | None = None, deliver: bool = True) -> list[dict[str, Any]]:
    """查一遍并发告警。返回失联的实验（便于测试和健康页）。"""
    from ..notify import alerts

    at = now if now is not None else now_ms()
    gone = stale_runs(services.db, now=at)
    for r in gone:
        manifest = jload(r.get("manifest")) or {}
        name = r.get("title") or manifest.get("strategy_name") or r["id"]
        alerts.raise_alert_safe(
            services.db,
            r["workspace_id"],
            alerts.EVENT_NODE_HEARTBEAT_LOST,
            subject=str(name),
            context_data={
                "run_id": r["id"],
                "silent_seconds": int(r["silent_ms"] / 1000),
                "execution": manifest.get("execution") or "paper",
                "state": r["state"],
            },
            now=at,
        )
    if gone and deliver:
        try:
            alerts.deliver_pending(services)
        except Exception:
            log.exception("看门狗投递告警失败")
    return gone


def check_clock(services: Any, *, exchanges: list[str] | None = None, transport: Any = None) -> list[dict[str, Any]]:
    """worker 周期钩子：测交易所时间偏差；有交易所模拟盘实验在跑时，偏差过大发告警。"""
    from ..live import nodeguard
    from ..notify import alerts

    rows = services.db.fetchall("SELECT * FROM runs WHERE state=? AND kind='paper'", (RUNNING,))
    by_ex: dict[str, set[str]] = {}
    for r in rows:
        m = jload(r.get("manifest")) or {}
        if m.get("execution") in ("demo", "live"):
            by_ex.setdefault(m.get("exchange") or "binance", set()).add(r["workspace_id"])
    targets = exchanges or sorted(by_ex) or []
    for ex in targets:
        try:
            nodeguard.CLOCK.measure(ex, transport=transport)
        except Exception as exc:
            log.info("测 %s 时间失败：%s", ex, exc)
    status = nodeguard.CLOCK.status()
    for s in status:
        if s["level"] == "bad":
            for ws in by_ex.get(s["exchange"], ()):
                alerts.raise_alert_safe(
                    services.db,
                    ws,
                    alerts.EVENT_CLOCK_DRIFT,
                    subject=s["exchange"],
                    context_data={"offset_ms": s["offset_ms"]},
                )
    return status


def health(services: Any) -> dict[str, Any]:
    """健康页 / 手机看板 / 机器人「状态」共用的节点摘要。"""
    from ..live import nodeguard

    at = now_ms()
    workers = services.db.fetchall("SELECT id, host, last_seen, info FROM workers ORDER BY last_seen DESC")
    alive = [w for w in workers if at - int(w["last_seen"] or 0) < STALE_MS]
    gone = stale_runs(services.db, now=at)
    return {
        "now": at,
        "workers_alive": len(alive),
        "workers": [
            {
                "id": w["id"],
                "host": w["host"],
                "last_seen": w["last_seen"],
                "alive": at - int(w["last_seen"] or 0) < STALE_MS,
            }
            for w in workers[:10]
        ],
        "stale_runs": [
            {"id": r["id"], "title": r.get("title"), "silent_seconds": int(r["silent_ms"] / 1000)} for r in gone
        ],
        "clock": nodeguard.CLOCK.status(),
        "rate_limits": nodeguard.limits_status(),
    }


# ---------------------------------------------------------------------- 后台线程
class Watchdog:
    def __init__(self, services_fn: Any, *, interval: float = CHECK_SECONDS) -> None:
        self.services_fn = services_fn
        self.interval = interval
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if os.environ.get("QAGENT_WATCHDOG", "1") == "0" or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="qagent-watchdog", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.wait(self.interval):
            try:
                check(self.services_fn())
            except Exception:
                log.exception("看门狗检查失败")


class HangGuard:
    """worker 主循环的心跳：超过 limit 秒没有 tick 就退出进程（docker 会重启）。"""

    def __init__(self, limit: float = HANG_SECONDS, *, on_hang: Any = None) -> None:
        self.limit = limit
        self.at = time.monotonic()
        self.on_hang = on_hang or (lambda: os._exit(70))
        self.stop = threading.Event()

    def tick(self) -> None:
        self.at = time.monotonic()

    def start(self) -> None:
        if self.limit <= 0:
            return
        threading.Thread(target=self._run, name="qagent-hangguard", daemon=True).start()

    def _run(self) -> None:
        while not self.stop.wait(min(10.0, self.limit / 3)):
            if time.monotonic() - self.at > self.limit:
                log.error("worker 主循环 %.0f 秒没有动静，退出进程让 docker 重启", time.monotonic() - self.at)
                self.on_hang()
                return
