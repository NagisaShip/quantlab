"""网络可用性实测（plan v2.0：部署地网络须连续实测 72 小时后才能宣称可用）。

worker 每 5 分钟对各行情接口做一次探测（REST ping），结果写入 network_probes。
`status()` 给出每个接口的判定：
- PENDING（网络可用性待验证）：观测不足 72 小时或样本太少——界面横幅持续提示；
- AVAILABLE：72 小时内成功率 ≥ 99%；
- DEGRADED：成功率 < 99%（列出最近失败原因）；
- UNREACHABLE：最近 1 小时全部失败。
"""

from __future__ import annotations

import time
from typing import Any

from .db import Database, now_ms

WINDOW_MS = 72 * 3_600_000
MIN_SAMPLES = 72 * 6  # 至少约每 10 分钟一个样本
PROBE_INTERVAL_S = 300.0


def record(
    db: Database,
    provider: str,
    endpoint: str,
    ok: bool,
    latency_ms: int | None,
    stage: str = "rest",
    error: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO network_probes (ts, provider, endpoint, ok, latency_ms, stage, error) VALUES (?,?,?,?,?,?,?)",
        (now_ms(), provider, endpoint, int(ok), latency_ms, stage, (error or "")[:300] or None),
    )


def probe_adapters(db: Database, adapters: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, ad in adapters.items():
        t0 = time.perf_counter()
        try:
            ok, err = bool(ad.ping()), None
        except Exception as exc:
            ok, err = False, f"{type(exc).__name__}: {exc}"
        ms = int((time.perf_counter() - t0) * 1000)
        record(db, name, getattr(ad, "base", "") or name, ok, ms, "rest", err)
        out.append({"provider": name, "ok": ok, "latency_ms": ms, "error": err})
    # 只保留 30 天
    db.execute("DELETE FROM network_probes WHERE ts < ?", (now_ms() - 30 * 86_400_000,))
    return out


def status(db: Database) -> dict[str, Any]:
    now = now_ms()
    providers = [r["provider"] for r in db.fetchall("SELECT DISTINCT provider FROM network_probes")]
    items = []
    for p in providers:
        first = db.scalar("SELECT MIN(ts) FROM network_probes WHERE provider=?", (p,)) or now
        rows = db.fetchall(
            "SELECT ts, ok, latency_ms, error FROM network_probes WHERE provider=? AND ts>=? ORDER BY ts",
            (p, now - WINDOW_MS),
        )
        n = len(rows)
        okn = sum(1 for r in rows if r["ok"])
        last_hour = [r for r in rows if r["ts"] >= now - 3_600_000]
        observed_h = round((now - int(first)) / 3_600_000, 1)
        rate = okn / n if n else None
        lat = sorted(r["latency_ms"] for r in rows if r["ok"] and r["latency_ms"] is not None)
        if last_hour and not any(r["ok"] for r in last_hour):
            verdict = "UNREACHABLE"
        elif observed_h < 72 or n < MIN_SAMPLES:
            verdict = "PENDING"
        elif rate is not None and rate >= 0.99:
            verdict = "AVAILABLE"
        else:
            verdict = "DEGRADED"
        items.append(
            {
                "provider": p,
                "verdict": verdict,
                "observed_hours": observed_h,
                "samples_72h": n,
                "success_rate_72h": round(rate, 4) if rate is not None else None,
                "p50_latency_ms": lat[len(lat) // 2] if lat else None,
                "p95_latency_ms": lat[int(len(lat) * 0.95)] if lat else None,
                "last_ok": max((r["ts"] for r in rows if r["ok"]), default=None),
                "recent_errors": [r["error"] for r in rows[-20:] if not r["ok"] and r["error"]][-3:],
            }
        )
    overall = (
        "PENDING"
        if not items
        else (
            "AVAILABLE"
            if all(i["verdict"] == "AVAILABLE" for i in items)
            else "UNREACHABLE"
            if all(i["verdict"] == "UNREACHABLE" for i in items)
            else "PENDING"
            if any(i["verdict"] == "PENDING" for i in items)
            else "DEGRADED"
        )
    )
    labels = {
        "PENDING": "网络可用性待验证",
        "AVAILABLE": "网络已连续 72 小时可用",
        "DEGRADED": "网络不稳定",
        "UNREACHABLE": "行情接口不可达",
    }
    return {
        "overall": overall,
        "label": labels[overall],
        "providers": items,
        "window_hours": 72,
        "note": "部署地（尤其中国大陆）访问交易所接口可能受限；须连续实测 72 小时、成功率 ≥ 99% 才视为可用",
    }
