"""统一审批收件箱：把 AI 行动的待确认项与 AI 研究室里的审批卡片投影成同一种结构。

只做**读模型 + 决策转发**，不新增审批表、不改现有权限模式：

- ``ai``：``platform_tools.ACTIONS`` 中 status=pending 的行动，沿用原 ``confirm`` 执行；
- ``lab``：AI 研究室消息里 ``kind=ask``、``data.approval`` 且未回答的卡片，转发到原 ``lab.decide``。

两种来源仍各自校验自己的权限；这个路由只负责聚合和统一 id/href。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..agents.platform_tools import ACTIONS, confirm as confirm_action
from ..platform import context
from .deps import get_services
from .routes_collab import filter_visible
from .routes_lab import _lab_for, _project_or_404, _start

router = APIRouter(prefix="/api/approvals", tags=["统一审批"])

SOURCES = ("ai", "lab")
MAX_ITEMS = 200


class ApprovalDecisionIn(BaseModel):
    approve: bool
    note: str = Field("", max_length=400)


def _iso(ts: Any) -> int:
    """审批项的时间统一成毫秒；存储里有的用秒、有的用毫秒。"""
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        return 0
    return int(value * 1000) if value < 10_000_000_000 else int(value)


def _lab_items(limit: int) -> list[dict[str, Any]]:
    from .routes_lab import _lab

    lab = _lab()
    rows = filter_visible("project", lab.store.list(), "id")
    items: list[dict[str, Any]] = []
    for project in rows:
        if project.get("archived"):
            continue
        try:
            messages = lab.store.messages(project["id"])
        except Exception:
            continue
        pending = next(
            (
                m
                for m in reversed(messages)
                if m.get("kind") == "ask"
                and isinstance(m.get("data"), dict)
                and m["data"].get("approval")
                and m["data"].get("answered") is None
            ),
            None,
        )
        if pending is None:
            continue
        actions = [
            {
                "tool": str(a.get("tool") or ""),
                "label": str(a.get("label") or a.get("tool") or ""),
                "category": str(a.get("category") or ""),
            }
            for a in pending["data"]["approval"]
            if isinstance(a, dict)
        ]
        items.append(
            {
                "id": f"lab:{project['id']}:{pending['seq']}",
                "source": "lab",
                "title": (project.get("title") or project.get("idea") or "").strip()[:80] or "研究项目",
                "summary": "、".join(a["label"] for a in actions)[:200],
                "risk": sorted({a["category"] for a in actions if a["category"]}),
                "actions": actions,
                "created_at": _iso(pending.get("created_at")),
                "href": f"#/lab/{project['id']}?seq={pending['seq']}",
                "project_id": project["id"],
                "seq": pending["seq"],
            }
        )
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:limit]


def _ai_items(limit: int) -> list[dict[str, Any]]:
    services = get_services()
    user = context.current().user_id or ""
    items: list[dict[str, Any]] = []
    for action in services.store.list(ACTIONS):
        if action.get("status") != "pending":
            continue
        if action.get("created_by") and action["created_by"] != user:
            continue
        items.append(
            {
                "id": f"ai:{action['id']}",
                "source": "ai",
                "title": str(action.get("summary") or action.get("tool") or "AI 操作")[:120],
                "summary": str(action.get("tool") or ""),
                "risk": [str(action.get("tool") or "")],
                "actions": [
                    {
                        "tool": str(action.get("tool") or ""),
                        "label": str(action.get("summary") or action.get("tool") or ""),
                        "category": "",
                    }
                ],
                "created_at": _iso(action.get("created_at")),
                "href": "#/ask",
                "action_id": action["id"],
            }
        )
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:limit]


@router.get("", summary="统一审批收件箱：AI 行动 + AI 研究室审批卡片")
def list_approvals(
    source: str | None = Query(None, description="ai / lab，逗号分隔；默认全部"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    picked = [s.strip() for s in (source or "").replace(" ", "").split(",") if s.strip()] or list(SOURCES)
    unknown = [s for s in picked if s not in SOURCES]
    if unknown:
        raise HTTPException(422, f"未知审批来源 {unknown[0]}；可选：{', '.join(SOURCES)}")
    items: list[dict[str, Any]] = []
    if "ai" in picked:
        items.extend(_ai_items(limit))
    if "lab" in picked:
        items.extend(_lab_items(limit))
    items.sort(key=lambda x: x["created_at"], reverse=True)
    counts = {s: sum(1 for i in items if i["source"] == s) for s in SOURCES}
    return {"items": items[:limit], "counts": counts, "total": sum(counts.values()), "server_time": int(time.time() * 1000)}


@router.post("/{approval_id}", summary="批准/拒绝一条统一审批项")
def decide(approval_id: str, payload: ApprovalDecisionIn) -> dict[str, Any]:
    if approval_id.startswith("ai:"):
        action_id = approval_id[3:]
        try:
            result = confirm_action(action_id, user=context.current().user_id or "", approve=payload.approve)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "source": "ai", "approval": result}
    if approval_id.startswith("lab:"):
        try:
            _tag, pid, seq = approval_id.split(":", 2)
            seq_no = int(seq)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "审批项 id 无效") from exc
        lab = _lab_for(pid)
        _project_or_404(lab, pid)
        pending = next(
            (
                m
                for m in lab.store.messages(pid)
                if m.get("seq") == seq_no
                and m.get("kind") == "ask"
                and isinstance(m.get("data"), dict)
                and m["data"].get("approval")
                and m["data"].get("answered") is None
            ),
            None,
        )
        if pending is None:
            raise HTTPException(404, "这条审批已经处理或不存在")
        choice = "批准" if payload.approve else "拒绝"
        return _start(lab, pid, "decision", lambda: lab.decide(pid, choice, payload.note), False)
    raise HTTPException(404, "审批项不存在")
