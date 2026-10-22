"""统一任务中心：把现有任务来源投影成一种只读结构，供侧栏 / 任务中心消费。

这是一个**读模型**，不是新的执行引擎：

- 不新增任务表、不引入状态机、不写任何字段；
- 不触发 worker / recover / 定时任务，只读现有存储；
- 三个来源各自一次读取，避免按条查询（N+1）：
  * ``run``       — ``RunService.list``，只取 QUEUED / RUNNING / PAUSED / RECOVERING 四种活动状态，按工作区隔离；
  * ``lab``       — ``LabStore.list``，私有项目复用协作可见性（``filter_visible``），只对 owner / 被共享者可见；
  * ``scheduler`` — ``research.nightshift.tasks``（读不到就跳过，不影响另外两个来源）。

状态映射只认真实字段（``runs.state`` / ``project.status`` / ``plan.status`` / 任务 ``status``），
未知状态不猜：``state`` 给 ``unknown``，原始值原样放在 ``raw_state``。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..platform import context
from ..platform.db import now_ms
from ..platform.runs import PAUSED, QUEUED, RECOVERING, RUNNING
from .deps import get_services

router = APIRouter(prefix="/api/tasks", tags=["统一任务中心"])

SOURCES = ("run", "lab", "scheduler")
RUN_STATES = {QUEUED: "queued", RUNNING: "running", PAUSED: "paused", RECOVERING: "recovering"}
LAB_STATUS_STATES = {"running": "running", "awaiting_user": "awaiting_user"}
SCHEDULER_STATES = {"queued": "queued", "running": "running", "done": "done", "failed": "failed"}
ACTIVE_STATES = frozenset({"queued", "running", "paused", "recovering", "awaiting_user"})
#: 计划已停止 / 已完成的项目只在这段时间内作为「失败 / 完成」出现在任务中心，避免历史项目长期占位。
RECENT_SECONDS = 6 * 60 * 60
#: 单个来源最多取这么多条，最终再按 limit 截断。
MAX_PER_SOURCE = 200


def _parse_sources(raw: str | None) -> list[str]:
    """解析 ``source=run,lab``；未知来源按 422 拒绝（这是客户端参数错误，不是服务端故障）。"""
    if raw is None or not raw.strip():
        return list(SOURCES)
    picked: list[str] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if part not in SOURCES:
            raise HTTPException(status_code=422, detail=f"未知任务来源 {part}；可选：{', '.join(SOURCES)}")
        if part not in picked:
            picked.append(part)
    if not picked:
        raise HTTPException(status_code=422, detail=f"任务来源不能为空；可选：{', '.join(SOURCES)}")
    return picked


def _progress(done: Any, total: Any, text: Any = "") -> dict[str, Any]:
    return {"done": int(done or 0), "total": int(total or 0), "text": str(text or "")}


def _run_items(services: Any, workspace_id: str, limit: int) -> list[dict[str, Any]]:
    """通用 runs：四种活动状态各查一次（固定 4 次，不随条数增长），再合并。"""
    # 每个状态至少取 20 条：limit 很小时也能看到「最近更新」而不只是「最近创建」的那一条。
    per_state = min(max(limit, 20), MAX_PER_SOURCE)
    rows: list[dict[str, Any]] = []
    for state in (QUEUED, RUNNING, PAUSED, RECOVERING):
        rows.extend(services.runs.list(workspace_id, state=state, limit=per_state))
    items: list[dict[str, Any]] = []
    for run in rows:
        raw_state = str(run.get("state") or "")
        progress = run.get("progress") or {}
        summary = run.get("summary") or {}
        items.append(
            {
                "id": run["id"],
                "source": "run",
                "kind": run.get("kind") or "run",
                "title": (run.get("title") or "").strip() or (run.get("kind") or "run"),
                "state": RUN_STATES.get(raw_state, "unknown"),
                "raw_state": raw_state,
                "progress": _progress(progress.get("done"), progress.get("total"), summary.get("text")),
                "updated_at": int(run.get("updated_at") or run.get("created_at") or 0),
                "href": "#/settings/runs",
                "workspace_id": run.get("workspace_id") or workspace_id,
                "error": str(run.get("error") or ""),
            }
        )
    return items


def _lab_items(services: Any, workspace_id: str) -> list[dict[str, Any]]:
    """AI 研究室：一次取项目列表，按真实字段判断「进行中 / 等回答 / 暂停 / 失败 / 完成」。"""
    from ..agents.lab_state import LabStore
    from .routes_collab import filter_visible

    rows = [p for p in LabStore(services.store).list() if not p.get("archived")]
    rows = filter_visible("project", rows, "id")
    now = time.time()
    items: list[dict[str, Any]] = []
    for project in rows:
        status = str(project.get("status") or "")
        job = project.get("job") if isinstance(project.get("job"), dict) else None
        plan = project.get("plan") if isinstance(project.get("plan"), dict) else {}
        plan_status = str(plan.get("status") or "")
        updated = float(project.get("updated_at") or 0)
        fresh = now - updated <= RECENT_SECONDS

        if status == "awaiting_user":
            state = "awaiting_user"
        elif status == "running" or job:
            state = "running"
        elif plan_status == "active":
            # 计划还在，但 autorun 关了（回合被停止 / 出错）：由真实字段判断为暂停。
            state = "running" if plan.get("autorun") else "paused"
        elif plan_status == "stopped" and fresh:
            state = "failed"
        elif plan_status == "done" and fresh:
            state = "done"
        else:
            continue

        # raw_state 用真正决定 state 的那个字段，排障时不用回头猜。
        if status in ("running", "awaiting_user"):
            raw_state = status
        elif job:
            raw_state = "job"
        elif plan_status:
            raw_state = plan_status
        else:
            raw_state = status or "idle"

        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        done_steps = sum(1 for step in steps if isinstance(step, dict) and step.get("status") in ("done", "skipped"))
        if state == "running" and job:
            text = job.get("phase") or job.get("detail") or ""
        elif state == "awaiting_user":
            text = job.get("phase") if job else "等待研究者回答"
        elif steps:
            current = next(
                (
                    step.get("text")
                    for step in steps
                    if isinstance(step, dict) and step.get("status") in ("doing", "pending")
                ),
                "",
            )
            text = current or plan.get("task") or ""
        else:
            text = plan.get("task") or ""

        items.append(
            {
                "id": project["id"],
                "source": "lab",
                "kind": project.get("kind") or "research",
                "title": (project.get("title") or project.get("idea") or "").strip()[:80] or "研究项目",
                "state": state,
                "raw_state": raw_state,
                "progress": _progress(done_steps, len(steps), text),
                "updated_at": int(updated * 1000),
                "href": f"#/lab/{project['id']}",
                "workspace_id": workspace_id,
                "error": str((job or {}).get("error") or project.get("last_error") or ""),
            }
        )
    return items


def _scheduler_items(services: Any, workspace_id: str) -> list[dict[str, Any]]:
    """定时任务队列（研究夜班）。读不到该集合时返回空列表，不让整个任务中心失败。"""
    from ..research import nightshift as NS

    try:
        rows = NS.tasks(services.store)
    except Exception:  # pragma: no cover - 存储异常时另外两个来源仍可用
        return []
    items: list[dict[str, Any]] = []
    for task in rows[:MAX_PER_SOURCE]:
        raw_state = str(task.get("status") or "")
        state = SCHEDULER_STATES.get(raw_state)
        if state is None:  # 未知状态不猜：没有映射就不作为任务展示
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        items.append(
            {
                "id": task.get("id") or "",
                "source": "scheduler",
                "kind": task.get("kind") or "research",
                "title": (task.get("idea") or "").strip()[:80] or "定时任务",
                "state": state,
                "raw_state": raw_state,
                "progress": _progress(0, 0, ""),
                "updated_at": int(float(task.get("created_at") or 0) * 1000),
                "href": "#/long-running",
                "workspace_id": workspace_id,
                "error": str(task.get("error") or result.get("error") or ""),
            }
        )
    return items


@router.get("/overview", summary="统一任务中心：运行、AI 研究室与定时任务的活动读模型")
def overview(
    limit: int = Query(50, ge=1, le=200, description="返回条目上限"),
    source: str | None = Query(None, description="逗号分隔的来源：run,lab,scheduler；省略表示全部"),
) -> dict:
    """聚合当前工作区可见的活动任务。只读，不触发任何执行或恢复。"""
    sources = _parse_sources(source)
    services = get_services()
    workspace_id = context.current_workspace()

    items: list[dict[str, Any]] = []
    if "run" in sources:
        items.extend(_run_items(services, workspace_id, limit))
    if "lab" in sources:
        items.extend(_lab_items(services, workspace_id))
    if "scheduler" in sources:
        items.extend(_scheduler_items(services, workspace_id))

    items.sort(key=lambda item: (item["updated_at"], item["id"]), reverse=True)
    truncated = len(items) > limit
    items = items[:limit]

    by_source = {name: 0 for name in SOURCES}
    by_state: dict[str, int] = {}
    for item in items:
        by_source[item["source"]] += 1
        by_state[item["state"]] = by_state.get(item["state"], 0) + 1
    counts = {
        "total": len(items),
        "active": sum(1 for item in items if item["state"] in ACTIVE_STATES),
        "by_source": by_source,
        "by_state": by_state,
        "truncated": truncated,
    }
    return {"items": items, "counts": counts, "generated_at": now_ms()}


__all__ = ["router"]
