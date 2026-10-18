"""执行研究的存储与数据：试验记录、执行方案库、成本模型、盘口录制，以及给仿真准备盘口带。

- 试验（``ex_runs``）：每评估一次记一笔，完整报告存 blob。
- 执行方案库（``ex_schemes``）：存好的拆单方案；策略里用 ``set_execution('方案名')`` 让回测按方案的成本曲线计滑点。
- 成本模型（``ex_costs``）：用真实成交校准出来的平方根冲击模型。
- 盘口录制（``ex_recordings``）：从交易所公开接口按固定间隔抓盘口快照与逐笔成交，录完可以回放。

全程是仿真：这里没有任何下单路径。
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import threading
import time
import uuid
from typing import Any

import numpy as np

from . import execution as EX

RUNS = "ex_runs"
SCHEMES = "ex_schemes"
RUN_REPORTS = "ex_run_reports"
REPORTS = "ex_reports"
COSTS = "ex_costs"
RECORDINGS = "ex_recordings"
REC_DATA = "ex_recording_data"

STATUS_LABELS = {"candidate": "候选", "active": "启用", "retired": "退役"}
MAX_RECORD_SECONDS = 4 * 3600


def _now() -> float:
    return time.time()


def config_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _slim(report: dict[str, Any]) -> dict[str, Any]:
    return {k: report.get(k) for k in ("summary", "admitted")}


class ExecutionLibrary:
    def __init__(self, store: Any) -> None:
        self.store = store

    # ---------------- 试验 ----------------
    def record_run(
        self,
        cfg: dict[str, Any],
        report: dict[str, Any],
        *,
        symbols: list[str],
        source: str = "",
        project_id: str = "",
        note: str = "",
        created_by: str = "user",
    ) -> dict[str, Any]:
        rid = f"x_{uuid.uuid4().hex[:10]}"
        rec = {
            "id": rid,
            "hash": config_hash(cfg),
            "algo": cfg.get("algo"),
            "style": cfg.get("style"),
            "horizon": cfg.get("horizon"),
            "symbols": symbols,
            "summary": report.get("summary"),
            "admitted": report.get("admitted"),
            "tape_sources": (report.get("tape") or {}).get("sources"),
            "source": source,
            "project_id": project_id,
            "note": note[:200],
            "synthetic": bool((report.get("tape") or {}).get("synthetic")),
            "created_by": created_by,
            "created_at": _now(),
        }
        self.store.upsert(RUNS, rec)
        self.store.put_blob(RUN_REPORTS, rid, report)
        return rec

    def runs(self, limit: int = 200) -> list[dict[str, Any]]:
        return sorted(self.store.list(RUNS), key=lambda r: -(r.get("created_at") or 0))[:limit]

    def run(self, rid: str) -> dict[str, Any] | None:
        return self.store.get(RUNS, rid)

    def run_report(self, rid: str) -> dict[str, Any] | None:
        return self.store.get_blob(RUN_REPORTS, rid)

    def delete_run(self, rid: str) -> bool:
        self.store.delete_blob(RUN_REPORTS, rid)
        return self.store.delete(RUNS, rid)

    def trial_count(self) -> int:
        return len({r.get("hash") for r in self.store.list(RUNS)})

    # ---------------- 方案库 ----------------
    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = self.store.list(SCHEMES)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return sorted(rows, key=lambda r: -(r.get("updated_at") or 0))

    def get(self, sid: str) -> dict[str, Any] | None:
        return self.store.get(SCHEMES, sid)

    def by_name(self, name: str) -> dict[str, Any] | None:
        return next((m for m in self.store.list(SCHEMES) if m.get("name") == name or m.get("id") == name), None)

    def save(
        self,
        *,
        name: str,
        config: dict[str, Any],
        report: dict[str, Any] | None,
        symbols: list[str],
        status: str = "candidate",
        note: str = "",
        scheme_id: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        created_by: str = "user",
    ) -> dict[str, Any]:
        prev = self.get(scheme_id) if scheme_id else None
        sid = scheme_id or f"ex_{uuid.uuid4().hex[:8]}"
        name = (name or (prev or {}).get("name") or f"{config.get('algo')}_{config.get('horizon')}").strip()[:40]
        if next((m for m in self.store.list(SCHEMES) if m.get("name") == name and m.get("id") != sid), None):
            name = f"{name}_{sid[-4:]}"
        curve = (report or {}).get("cost_curve") or {}
        rec = {
            "id": sid,
            "name": name,
            "config": config,
            "symbols": symbols,
            "summary": (report or {}).get("summary"),
            "admitted": (report or {}).get("admitted"),
            "cost_model": {"a_bps": curve.get("a_bps"), "b_bps": curve.get("b_bps"), "r2": curve.get("r2")}
            if curve
            else None,
            "status": status if status in STATUS_LABELS else "candidate",
            "note": note[:400] or (prev or {}).get("note", ""),
            "project_id": project_id or (prev or {}).get("project_id"),
            "run_id": run_id,
            "created_by": created_by,
            "created_at": (prev or {}).get("created_at") or _now(),
            "updated_at": _now(),
        }
        self.store.upsert(SCHEMES, rec)
        if report is not None:
            self.store.put_blob(REPORTS, sid, report)
        return rec

    def report(self, sid: str) -> dict[str, Any] | None:
        return self.store.get_blob(REPORTS, sid)

    def patch(self, sid: str, **changes: Any) -> dict[str, Any] | None:
        rec = self.get(sid)
        if rec is None:
            return None
        if "status" in changes and changes["status"] not in STATUS_LABELS:
            changes.pop("status")
        if "name" in changes:
            changes["name"] = str(changes["name"]).strip()[:40] or rec["name"]
        rec.update(changes, updated_at=_now())
        self.store.upsert(SCHEMES, rec)
        return rec

    def delete(self, sid: str) -> bool:
        self.store.delete_blob(REPORTS, sid)
        return self.store.delete(SCHEMES, sid)

    def search(self, query: str = "") -> list[dict[str, Any]]:
        q = (query or "").lower()
        out = []
        for m in self.list():
            if q and q not in (m["name"] + (m.get("note") or "") + str(m["config"].get("algo"))).lower():
                continue
            s = m.get("summary") or {}
            out.append(
                {
                    "name": m["name"],
                    "status": m["status"],
                    "algo": m["config"].get("algo"),
                    "horizon": m["config"].get("horizon"),
                    "is_bps": s.get("is_bps"),
                    "beat_twap_bps": s.get("beat_twap_bps"),
                }
            )
        return out[:30]

    # ---------------- 成本模型 ----------------
    def save_cost_model(self, *, name: str, model: dict[str, Any], symbols: list[str], source: str) -> dict[str, Any]:
        cid = f"cm_{uuid.uuid4().hex[:8]}"
        rec = {
            "id": cid,
            "name": (name or "成本模型").strip()[:40],
            "symbols": symbols,
            "source": source,
            **{k: model.get(k) for k in ("a_bps", "Y", "Y_se", "r2", "n", "used", "mean_slip_bps", "note")},
            "active": False,
            "created_at": _now(),
        }
        self.store.upsert(COSTS, rec)
        self.store.put_blob(COSTS + "_points", cid, {"points": model.get("points") or []})
        return rec

    def cost_models(self) -> list[dict[str, Any]]:
        return sorted(self.store.list(COSTS), key=lambda r: -(r.get("created_at") or 0))

    def cost_model(self, cid: str) -> dict[str, Any] | None:
        rec = self.store.get(COSTS, cid)
        if rec:
            rec = {**rec, **(self.store.get_blob(COSTS + "_points", cid) or {})}
        return rec

    def activate_cost_model(self, cid: str) -> dict[str, Any] | None:
        target = None
        for r in self.store.list(COSTS):
            on = r["id"] == cid
            if on or r.get("active"):
                r["active"] = on
                self.store.upsert(COSTS, r)
            if on:
                target = r
        return target

    def active_cost_model(self) -> dict[str, Any]:
        rec = next((r for r in self.store.list(COSTS) if r.get("active")), None)
        return rec or {"a_bps": 2.0, "Y": 0.7, "name": "默认（文献经验值）", "id": None}

    def delete_cost_model(self, cid: str) -> bool:
        self.store.delete_blob(COSTS + "_points", cid)
        return self.store.delete(COSTS, cid)

    # ---------------- 盘口录制 ----------------
    def recordings(self) -> list[dict[str, Any]]:
        return sorted(self.store.list(RECORDINGS), key=lambda r: -(r.get("created_at") or 0))

    def recording(self, rid: str) -> dict[str, Any] | None:
        return self.store.get(RECORDINGS, rid)

    def recording_data(self, rid: str) -> dict[str, Any] | None:
        return self.store.get_blob(REC_DATA, rid)

    def delete_recording(self, rid: str) -> bool:
        stop_recording(rid)
        self.store.delete_blob(REC_DATA, rid)
        return self.store.delete(RECORDINGS, rid)


# ---------------------------------------------------------------------------
# 盘口录制：后台线程按间隔抓公开盘口与逐笔，不需要密钥
# ---------------------------------------------------------------------------
_ACTIVE: dict[str, threading.Event] = {}


def stop_recording(rid: str) -> bool:
    ev = _ACTIVE.get(rid)
    if ev:
        ev.set()
        return True
    return False


def start_recording(
    lib: ExecutionLibrary,
    adapter: Any,
    *,
    exchange: str,
    symbol: str,
    seconds: int,
    interval: float = 2.0,
    depth: int = 20,
    sync: bool = False,
) -> dict[str, Any]:
    """开始录制。adapter 需要 fetch_order_book(symbol, depth) 与 fetch_trades(symbol)。

    sync=True 时在当前线程跑完（测试用）。
    """
    if not hasattr(adapter, "fetch_order_book"):
        raise EX.ExecutionError(f"{exchange} 暂不支持读取盘口")
    seconds = int(min(max(seconds, 10), MAX_RECORD_SECONDS))
    interval = float(min(max(interval, 0.5), 60.0))
    rid = f"rec_{uuid.uuid4().hex[:8]}"
    rec = {
        "id": rid,
        "exchange": exchange,
        "symbol": symbol,
        "seconds": seconds,
        "interval": interval,
        "depth": depth,
        "status": "recording",
        "snapshots": 0,
        "trades": 0,
        "error": "",
        "created_at": _now(),
        "finished_at": None,
    }
    lib.store.upsert(RECORDINGS, rec)
    ev = threading.Event()
    _ACTIVE[rid] = ev

    def run() -> None:
        snaps: list[dict[str, Any]] = []
        trades: dict[str, dict[str, Any]] = {}
        end = time.time() + seconds
        errors = 0
        last_save = time.time()
        try:
            while time.time() < end and not ev.is_set():
                t0 = time.time()
                try:
                    ob = adapter.fetch_order_book(symbol, depth)
                    snaps.append(
                        {"t": int(ob.get("ts") or t0 * 1000), "bids": ob["bids"][:depth], "asks": ob["asks"][:depth]}
                    )
                    for tr in adapter.fetch_trades(symbol) or []:
                        trades[str(tr.get("id") or f"{tr['t']}-{tr['p']}-{tr['q']}")] = tr
                    errors = 0
                except Exception as exc:
                    errors += 1
                    rec["error"] = str(exc)[:200]
                    if errors >= 5:
                        raise
                if time.time() - last_save > 15 or sync:
                    rec.update(snapshots=len(snaps), trades=len(trades))
                    lib.store.upsert(RECORDINGS, rec)
                    last_save = time.time()
                ev.wait(max(0.0, interval - (time.time() - t0)))
            rec["status"] = "done" if not ev.is_set() else "stopped"
        except Exception as exc:
            rec["status"] = "error"
            rec["error"] = str(exc)[:200]
        finally:
            _ACTIVE.pop(rid, None)
            rows = sorted(trades.values(), key=lambda x: x["t"])
            lib.store.put_blob(
                REC_DATA, rid, {"exchange": exchange, "symbol": symbol, "snapshots": snaps, "trades": rows}
            )
            rec.update(snapshots=len(snaps), trades=len(rows), finished_at=_now())
            if rec["status"] in ("done", "stopped") and len(snaps) < 20:
                rec["error"] = rec["error"] or "快照少于 20 个，不够回放"
            lib.store.upsert(RECORDINGS, rec)

    if sync:
        run()
    else:
        ctx = contextvars.copy_context()  # 带上当前工作区，后台线程写的是同一个工作区的存储
        threading.Thread(target=ctx.run, args=(run,), name=f"record-{rid}", daemon=True).start()
    return rec


# ---------------------------------------------------------------------------
# 数据：给仿真准备盘口带
# ---------------------------------------------------------------------------
def load_tapes(
    market: Any,
    lib: ExecutionLibrary,
    symbols: list[str],
    *,
    source: str = "auto",
    steps: int = 3000,
    recording_id: str | None = None,
) -> list[EX.Tape]:
    """source: auto（真实 1 分钟 K 线，拿不到退合成）| kline | synthetic | recording（需要 recording_id）。"""
    if source == "recording" or recording_id:
        data = lib.recording_data(recording_id or "")
        if not data:
            raise EX.ExecutionError("找不到这段盘口录制")
        return [EX.tape_from_recording(data)]
    tapes = []
    for i, sym in enumerate(symbols[:4]):
        if source == "synthetic":
            tapes.append(EX.synthetic_tape(sym, steps, seed=11 + i, price=_seed_price(sym)))
            continue
        try:
            bars, src = market.get_bars(sym, "1m", limit=steps)
            if not bars or len(bars) < 300 or (src == "synthetic" and source == "kline"):
                raise EX.ExecutionError(f"{sym} 的 1 分钟 K 线不足")
            tape = EX.tape_from_bars(sym, bars)
            if src == "synthetic":
                tape.source = "synthetic"
                tape.warnings.append("交易所不可用，K 线为合成数据")
            tapes.append(tape)
        except Exception as exc:
            if source == "kline":
                raise EX.ExecutionError(f"{sym}：拿不到真实 1 分钟 K 线（{exc}）") from exc
            t = EX.synthetic_tape(sym, steps, seed=11 + i, price=_seed_price(sym))
            t.warnings.append(f"{sym} 拿不到真实行情，改用合成盘口")
            tapes.append(t)
    if not tapes:
        raise EX.ExecutionError("没有可用的标的")
    return tapes


def _seed_price(sym: str) -> float:
    return {"BTC": 60_000.0, "ETH": 3_000.0, "SOL": 150.0, "BNB": 580.0}.get(sym.split("/")[0], 10.0)


def asset_stats(market: Any, symbols: list[str], *, days: int = 60) -> dict[str, dict[str, float]]:
    """日均成交额与日波动，用于成本模型与容量。拿不到时给经验值并标注。"""
    out = {}
    for sym in symbols:
        try:
            bars, src = market.get_bars(sym, "1d", limit=days)
            c = np.array([float(b.close) for b in bars])
            v = np.array([float(b.volume) * float(b.close) for b in bars])
            r = np.diff(np.log(c))
            out[sym] = {
                "adv_usd": float(np.median(v[-30:])),
                "sigma_d": float(r[-30:].std()) or 0.03,
                "synthetic": src == "synthetic",
            }
        except Exception:
            out[sym] = {"adv_usd": 5e7, "sigma_d": 0.04, "synthetic": True}
    return out


def fill_refs(market: Any, fills: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """每个标的：参考价函数（成交所在分钟的开盘价，拿不到 1 分钟就用 1 小时）、日均成交额、日波动。"""
    import bisect

    refs: dict[str, dict[str, Any]] = {}
    by_sym: dict[str, list[int]] = {}
    for f in fills:
        by_sym.setdefault(f["symbol"], []).append(int(f["ts"]))
    stats = asset_stats(market, list(by_sym))
    for sym, ts in by_sym.items():
        lo, hi = min(ts), max(ts)
        table: list[tuple[int, float]] = []
        for tf, step in (("1m", 60_000), ("1h", 3_600_000)):
            try:
                bars, src = market.get_bars(sym, tf, start=lo - step, end=hi + step)
                if bars and src != "synthetic":
                    table = [(int(b.open_time), float(b.open)) for b in bars]
                    break
            except Exception:
                continue
        if not table:
            continue
        keys = [k for k, _ in table]

        def ref(t: int, keys: list[int] = keys, table: list[tuple[int, float]] = table) -> float | None:
            i = bisect.bisect_right(keys, t) - 1
            return table[i][1] if i >= 0 else None

        refs[sym] = {"ref": ref, **stats[sym]}
    return refs


def expected_cost(scheme: dict[str, Any], participation: float) -> float:
    """方案的成本曲线在某个参与率（母单 / 期间成交量）下的预期成本（bp）。"""
    m = scheme.get("cost_model") or {}
    return float(m.get("a_bps") or 0) + float(m.get("b_bps") or 0) * math.sqrt(max(participation, 0))


__all__ = [
    "STATUS_LABELS",
    "ExecutionLibrary",
    "asset_stats",
    "expected_cost",
    "fill_refs",
    "load_tapes",
    "start_recording",
    "stop_recording",
]
