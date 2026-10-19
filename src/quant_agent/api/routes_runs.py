"""运行（Run）API：列表、详情、事件流（游标）、命令、成交、信号、净值。

所有长任务（回测、参数优化、实时模拟）都是一个 run；控制面只登记与查询，执行在 worker。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from typing import Any, Literal

import anyio
from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..platform import context
from ..platform.db import jload
from ..platform.errors import AppError, ErrorCode
from ..platform.runs import QUEUED, TERMINAL
from . import tasks
from .deps import get_services

router = APIRouter(prefix="/api/runs", tags=["运行与任务"])

# 事件推送：一次最多推这么多条，空闲多久发一次心跳。
# 心跳让浏览器能判断"连接已经半开"——断网时浏览器不一定立刻触发 close，
# 只靠 onclose 的话前端会以为连接还在，断线期间的事件就补不上了。
WS_BATCH = 200
WS_HEARTBEAT_SECONDS = float(os.environ.get("QAGENT_WS_HEARTBEAT_SECONDS", "15"))
WS_POLL_SECONDS = float(os.environ.get("QAGENT_WS_POLL_SECONDS", "1"))


def enqueue(
    *,
    kind: str,
    mode: str,
    title: str,
    manifest: dict[str, Any],
    strategy_version_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """登记一个 run（检查配额）。"""
    s = get_services()
    ctx = context.current()
    ws_id = context.current_workspace()
    quotas = (s.auth.workspace(ws_id).get("settings") or {}).get("quotas") or {}
    if idempotency_key:
        existing = s.db.fetchone(
            "SELECT id FROM runs WHERE workspace_id=? AND idempotency_key=?", (ws_id, idempotency_key)
        )
        if existing:
            return s.runs.get(existing["id"])
    active = s.runs.count_active(ws_id)
    if active >= int(quotas.get("max_concurrent_runs", 4)) and kind != "paper":
        raise AppError(
            ErrorCode.QUOTA_EXHAUSTED,
            f"工作区同时进行中的任务已达上限 {quotas.get('max_concurrent_runs', 4)}，请等待完成或取消后再试",
            details={"active": active},
        )
    today = s.runs.count_since(ws_id, int(time.time() // 86400 * 86400 * 1000))
    if today >= int(quotas.get("max_runs_per_day", 200)):
        raise AppError(
            ErrorCode.QUOTA_EXHAUSTED,
            f"今日运行次数已达上限 {quotas.get('max_runs_per_day', 200)}",
            details={"today": today},
        )
    # 用量计量侧的配额（T07）：与上面基于 runs 表实时计数的限制是**两套不同的口径**——
    # 上面数的是"登记了多少个 run"，这里数的是"实际消耗了多少资源"（CPU 秒、优化组合数、存储）。
    # 两者都要判：只判前者的话，一个 5000 组的参数网格会被当成 1 次运行放行。
    s.quota.enforce_enqueue(kind, manifest, workspace_id=ws_id)
    if s.model_hub is not None:
        blocked = s.model_hub.task_guard(kind, manifest)
        if blocked:
            raise AppError(ErrorCode.QUOTA_EXHAUSTED, blocked, details={"kind": kind})
    if kind == "paper":
        paper = s.runs.count_active(ws_id, kind="paper")
        if paper >= int(quotas.get("max_paper_runs", 3)):
            raise AppError(
                ErrorCode.QUOTA_EXHAUSTED,
                f"同时运行的实时模拟已达上限 {quotas.get('max_paper_runs', 3)}",
                details={"active_paper": paper},
            )
    run = s.runs.create(
        workspace_id=ws_id,
        user_id=ctx.user_id,
        kind=kind,
        mode=mode,
        title=title,
        manifest=manifest,
        strategy_version_id=strategy_version_id,
        idempotency_key=idempotency_key,
    )
    s.audit.record(f"run.create.{kind}", object_type="run", object_id=run["id"], details={"mode": mode, "title": title})
    return run


def _public(run: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in run.items() if k not in ("manifest", "checkpoint")}
    m = run.get("manifest") or {}
    out["manifest"] = {k: v for k, v in m.items() if k != "source"}
    out["terminal"] = run["state"] in TERMINAL
    if run.get("state") == QUEUED:
        s = get_services()
        ahead = s.db.scalar(
            "SELECT COUNT(*) AS n FROM runs WHERE state=? AND (created_at<? OR (created_at=? AND id<?))",
            (QUEUED, run["created_at"], run["created_at"], run["id"]),
        )
        out["queue_position"] = int(ahead or 0) + 1
    return out


@router.get("")
def list_runs(
    kind: str | None = None,
    mode: str | None = None,
    state: str | None = None,
    q: str | None = Query(None, max_length=160),
    before_created_at: int | None = None,
    before_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    s = get_services()
    rows = s.runs.list(
        context.current_workspace(),
        kind=kind,
        mode=mode,
        state=state,
        q=q,
        before_created_at=before_created_at,
        before_id=before_id,
        limit=min(limit + 1, 501),
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = {"created_at": items[-1]["created_at"], "id": items[-1]["id"]} if has_more and items else None
    return {
        "items": [_public(r) for r in items],
        "worker_mode": tasks.worker_mode(),
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


@router.get("/workers")
def workers() -> dict:
    s = get_services()
    now = int(time.time() * 1000)
    rows = s.db.fetchall(
        "SELECT id, host, pid, started_at, last_seen, info FROM workers WHERE last_seen>? ORDER BY last_seen DESC",
        (now - 60_000,),
    )
    items = []
    for row in rows:
        info = jload(row.get("info"), {}) or {}
        items.append(
            {
                "id": row["id"],
                "host": row["host"],
                "pid": row.get("pid"),
                "started_at": row.get("started_at"),
                "last_seen": row["last_seen"],
                "slots": int(info.get("slots") or 0),
                "busy": int(info.get("busy_count") or len(info.get("busy") or [])),
                "resident": int(info.get("resident_count") or len(info.get("resident") or [])),
                "rss_bytes": int(info.get("rss_bytes") or 0),
                "private_bytes": int(info.get("private_bytes") or 0),
                "cpu_seconds": float(info.get("cpu_seconds") or 0),
                "threads": int(info.get("threads") or 0),
                "kinds": info.get("kinds") or [],
            }
        )
    queued = int(s.db.scalar("SELECT COUNT(*) AS n FROM runs WHERE state=?", (QUEUED,)) or 0)
    return {
        "workers": items,
        "mode": tasks.worker_mode(),
        "wake": getattr(s.runs.wake, "kind", "none"),
        "queued": queued,
        "slots_total": sum(int(w["slots"]) for w in items),
        "busy_total": sum(int(w["busy"]) for w in items),
    }


@router.get("/{run_id}")
def get_run(run_id: str) -> dict:
    s = get_services()
    run = s.runs.require(run_id, context.current_workspace())
    return {"run": _public(run), "commands": s.runs.commands(run_id)}


@router.get("/{run_id}/result")
def run_result(run_id: str) -> dict:
    """运行结果（向后兼容：仍然一次性返回完整结果）。

    T06 之后大数组实际存在 `<数据目录>/results/<工作区>/<run>.<格式>`，blob 里只有摘要；
    `store.get_blob` 会透明地把大数组合并回来，所以这里的 `result` 与改动前**逐元素相同**。
    额外给出 `result_ref`：里面写清**实际**用的格式（本机没装 pyarrow 时是降级格式 `jsonl`，
    不是 Parquet）、路径、行数与校验和——调用方不该以为拿到的一定是 Parquet。
    """
    s = get_services()
    run = s.runs.require(run_id, context.current_workspace())
    tasks.raise_if_failed(run)
    if run["state"] not in TERMINAL:
        raise AppError(ErrorCode.CONFLICT, "任务尚未完成", details={"state": run["state"]})
    body = s.store.get_blob("run_results", run_id)
    if body is None:
        raise AppError(ErrorCode.NOT_FOUND, "该运行没有可下载的结果")
    out: dict[str, Any] = {"run": _public(run), "result": body}
    ref = s.store.result_ref("run_results", run_id)
    if ref is not None:
        out["result_ref"] = ref
    return out


@router.get("/{run_id}/series")
def run_series(
    run_id: str,
    name: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(2000, ge=1, le=20000),
) -> dict:
    """分页读取落盘的大数组（逐 bar 净值、信号、成交等）。

    不传 `name` 时只返回可读的数组清单，供调用方发现有哪些数据。
    逐页取出的数据拼起来，与 `/result` 里对应字段**逐元素相等**——这正是"没坏"的证明。

    归属校验走 `s.runs.require(run_id, 当前工作区)`：跨工作区一律 404，
    且落盘文件本身也按工作区目录隔离，两层都挡住越权读取。
    """
    s = get_services()
    s.runs.require(run_id, context.current_workspace())
    ref = s.store.result_ref("run_results", run_id)
    if ref is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "该运行没有落盘的大数据；可能是尚未完成，或是升级前产生的旧运行",
            details={"run_id": run_id},
        )
    if not name:
        # 只报清单：不传 name 时不返回数据，避免"顺手拉一个默认大数组"造成意外流量
        return {
            "run_id": run_id,
            "format": ref.get("format"),
            "series": sorted((ref.get("series") or {}).keys()),
            "result_ref": ref,
        }
    page = s.store.result_page("run_results", run_id, name, offset=offset, limit=limit)
    return {"run_id": run_id, "result_ref": ref, **page}


@router.get("/{run_id}/events")
def run_events(run_id: str, after: int = 0, limit: int = Query(500, ge=1, le=5000), types: str | None = None) -> dict:
    """事件流（游标分页）：after = 上次拿到的最后一个事件 id。"""
    s = get_services()
    s.runs.require(run_id, context.current_workspace())
    items = s.runs.events(run_id, after=after, limit=limit, types=types.split(",") if types else None)
    return {"items": items, "cursor": items[-1]["id"] if items else after}


@router.get("/{run_id}/fills")
def run_fills(run_id: str, limit: int = Query(2000, ge=1, le=20000)) -> dict:
    s = get_services()
    s.runs.require(run_id, context.current_workspace())
    return {"items": s.runs.fills(run_id, limit=limit)}


@router.get("/{run_id}/signals")
def run_signals(run_id: str, limit: int = Query(2000, ge=1, le=20000)) -> dict:
    s = get_services()
    s.runs.require(run_id, context.current_workspace())
    return {"items": s.runs.signals(run_id, limit=limit)}


@router.get("/{run_id}/nav")
def run_nav(run_id: str) -> dict:
    s = get_services()
    s.runs.require(run_id, context.current_workspace())
    return {"items": s.runs.nav(run_id)}


class CommandIn(BaseModel):
    command: Literal["pause", "resume", "cancel", "liquidate_and_stop", "tick"]
    idempotency_key: str | None = None


@router.post("/{run_id}/commands")
def send_command(run_id: str, payload: CommandIn, request: Request) -> dict:
    s = get_services()
    ctx = context.current()
    key = payload.idempotency_key or request.headers.get("idempotency-key")
    cmd = s.runs.command(run_id, context.current_workspace(), ctx.user_id, payload.command, idempotency_key=key)
    s.audit.record(f"run.command.{payload.command}", object_type="run", object_id=run_id)
    if tasks.worker_mode() == "inline":
        apply_inline(run_id)
    run = s.runs.get(run_id)
    return {"command": cmd, "run": _public(run)}


def apply_inline(run_id: str, *, force_tick: bool = False) -> None:
    """inline 模式：在当前请求里应用命令 / 推进一次（与独立 worker 相同代码）。"""
    s = get_services()
    w = tasks.inline_worker(s)
    run = s.runs.get(run_id)
    if run is None or run["state"] in TERMINAL:
        return
    if run["kind"] in tasks.RESIDENT:
        if run_id not in w.drivers:
            w.execute_now(run_id)
        w.drive(run_id, force_tick=force_tick)
    elif run["state"] in ("QUEUED", "RECOVERING"):
        w.execute_now(run_id)


@router.websocket("/{run_id}/ws")
async def run_ws(websocket: WebSocket, run_id: str, after: int = 0) -> None:
    """推送事件流（按游标轮询数据库）。认证沿用会话 Cookie。

    `after` 是**排他**游标：只推 `id > after` 的事件，与 `Runs.events(after=...)`
    的 `WHERE id>?` 完全同义。所以客户端把上次拿到的最后一个事件 id 原样带回来即可，
    既不会重复也不会漏——断线补齐靠的就是这一点。

    消息协议（都是 JSON 对象）：
    - `{"items": [...], "cursor": N}`：新增事件，按 id 升序，`cursor` 是本批最后一个 id；
    - `{"heartbeat": true, "ts": ..., "interval_ms": N}`：空闲心跳。浏览器在断网时
      不一定会立刻触发 close（连接可能停在半开状态），前端靠"多久没收到任何消息"
      判定链路已死并主动重连；
    - `{"done": true, "state": "..."}`：run 进入终态且事件已全部推完，随后服务端关闭连接。

    关闭码：4401 未认证、4403 无权访问该 run（前端见到这两个码不再重连）。
    """
    from .middleware import SESSION_COOKIE, _cookie

    s = get_services()
    token = _cookie({"cookie": websocket.headers.get("cookie", "")}, SESSION_COOKIE)
    # 数据库与认证都是同步实现，放进线程里跑，别把事件循环按住。
    user = await anyio.to_thread.run_sync(s.auth.resolve, token) if token else None
    if user is None:
        await websocket.close(code=4401)
        return
    run = await anyio.to_thread.run_sync(s.runs.get, run_id)
    memberships = await anyio.to_thread.run_sync(s.auth.memberships, user.user_id)
    if run is None or run["workspace_id"] not in {m["id"] for m in memberships}:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    cursor = after
    last_sent = time.monotonic()
    try:
        while True:
            # 一次把积压的事件推完再睡：断线十秒后可能攒了几十条，
            # 每批之间都睡一秒的话补齐会慢到肉眼可见。
            while True:
                # 把 cursor 按值绑定进闭包：它是会在循环里被重新赋值的变量，
                # 而 run_sync 是**在线程里**执行的，晚绑定会让它读到已经前进过的游标
                # ——那正好会跳过一整批事件，表现为"事件序号不连续"。
                items = await anyio.to_thread.run_sync(
                    lambda cur=cursor: s.runs.events(run_id, after=cur, limit=WS_BATCH)
                )
                if not items:
                    break
                cursor = items[-1]["id"]
                await websocket.send_json({"items": items, "cursor": cursor})
                last_sent = time.monotonic()
                if len(items) < WS_BATCH:
                    break
            cur = await anyio.to_thread.run_sync(s.runs.get, run_id)
            if cur and cur["state"] in TERMINAL:
                await websocket.send_json({"done": True, "state": cur["state"]})
                break
            now = time.monotonic()
            if now - last_sent >= WS_HEARTBEAT_SECONDS:
                await websocket.send_json(
                    {
                        "heartbeat": True,
                        "ts": int(time.time() * 1000),
                        "interval_ms": int(WS_HEARTBEAT_SECONDS * 1000),
                    }
                )
                last_sent = now
            await asyncio.sleep(WS_POLL_SECONDS)
    except (WebSocketDisconnect, RuntimeError, ConnectionError):
        # 客户端已经走了。Starlette 在收到 close 之后再 send 会抛 RuntimeError，
        # 这里一并吞掉：连接已经断了，没什么可补救的。
        return
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()
