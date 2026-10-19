"""AI 研究室的状态存储与项目初始化。

从 ``strategy_lab.py`` 拆出的持久化层：每个对话的项目元数据、消息线程、图片 blob、
以及新建项目时的默认字段。这里不导入 LLM、工具或运行流程，因此可以被 API、worker
和测试独立复用。
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

COLLECTION = "lab_projects"
THREADS = "lab_threads"
REPORTS = "lab_reports"
IMAGES = "lab_images"

DEFAULT_GOALS: dict[str, float | None] = {
    "min_sharpe": 1.0,
    "max_drawdown": 0.25,
    "min_trades": 10,
    "min_excess": None,
}
GOAL_LABELS = {
    "min_sharpe": ("夏普", "≥"),
    "max_drawdown": ("最大回撤", "≤"),
    "min_trades": ("交易笔数", "≥"),
    "min_excess": ("超额收益", "≥"),
}
HISTORY_TOKEN_BUDGET = 6_000
AUTO_COMPACT_DEFAULT_RATIO = 0.8
AUTO_COMPACT_MIN_RATIO = 0.5
AUTO_COMPACT_MAX_RATIO = 0.95
DEFAULT_BYPASS_APPROVALS = ("delete",)
DEFAULT_MAX_TOOL_CALLS = 1000

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _now() -> float:
    return time.time()


class LabStore:
    def __init__(self, store: Any, *, on_create: Callable[[str], None] | None = None) -> None:
        self.on_create = on_create
        self.store = store  # JsonStore 或 WorkspaceStore（按请求上下文解析工作区）

    # ---- 项目 ----
    def list(self) -> list[dict[str, Any]]:
        rows = self.store.list(COLLECTION)
        return sorted(rows, key=lambda r: r.get("updated_at", 0), reverse=True)

    def get(self, pid: str) -> dict[str, Any] | None:
        return self.store.get(COLLECTION, pid)

    def save(self, project: dict[str, Any]) -> dict[str, Any]:
        if self.on_create is not None and self.get(project["id"]) is None:
            self.on_create(project["id"])
        project["updated_at"] = _now()
        self.store.upsert(COLLECTION, project)
        return project

    def patch(self, pid: str, **changes: Any) -> dict[str, Any] | None:
        with _lock(f"p:{pid}"):
            cur = self.get(pid)
            if cur is None:
                return None
            cur.update(changes)
            return self.save(cur)

    def delete(self, pid: str) -> bool:
        for m in self.messages(pid):
            self._drop_images(pid, m)
        self.store.delete_blob(THREADS, pid)
        return self.store.delete(COLLECTION, pid)

    # ---- 对话 ----
    def messages(self, pid: str, after: int = 0, since: float = 0.0) -> list[dict[str, Any]]:
        """after：只要序号更大的新消息；since：同时带上此时刻之后被更新过的旧消息（工具卡片状态变化）。"""
        data = self.store.get_blob(THREADS, pid) or {}
        return [m for m in data.get("messages", []) if m["seq"] > after or (since and m.get("updated_at", 0) > since)]

    # ---- 图片附件 ----
    def put_image(self, pid: str, data_url: str, *, name: str = "") -> dict[str, Any]:
        """存一张图片（data URL），返回消息里引用它的 {id, mime, name}。"""
        mime = data_url[5 : data_url.index(";")]
        iid = f"{pid}-{uuid.uuid4().hex[:12]}"
        self.store.put_blob(IMAGES, iid, {"pid": pid, "mime": mime, "url": data_url})
        return {"id": iid, "mime": mime, "name": name}

    def get_image(self, pid: str, iid: str) -> dict[str, Any] | None:
        blob = self.store.get_blob(IMAGES, iid)
        return blob if blob and blob.get("pid") == pid else None

    def _drop_images(self, pid: str, msg: dict[str, Any], keep: set[str] | frozenset[str] = frozenset()) -> None:
        """删掉这条消息引用的、属于本对话的图片（分叉出去的对话有自己的副本，不受影响）。"""
        for img in (msg.get("data") or {}).get("images") or []:
            if img["id"] not in keep and self.get_image(pid, img["id"]) is not None:
                self.store.delete_blob(IMAGES, img["id"])

    def append(self, pid: str, msg: dict[str, Any]) -> dict[str, Any]:
        with _lock(f"t:{pid}"):
            data = self.store.get_blob(THREADS, pid) or {"messages": []}
            # 序号只增不减：编辑 / 重试删掉的序号不再复用，前端按「序号更大」增量拉取才不会漏消息
            seq = max(int(data.get("next_seq") or 0), (data["messages"][-1]["seq"] if data["messages"] else 0) + 1)
            row = {"seq": seq, "created_at": _now(), "data": {}, "content": "", **msg}
            row["seq"] = seq
            data["messages"].append(row)
            data["next_seq"] = seq + 1
            self.store.put_blob(THREADS, pid, data)
            return row

    def truncate(self, pid: str, from_seq: int, *, keep_images: set[str] | frozenset[str] = frozenset()) -> int:
        """删掉 seq >= from_seq 的消息（编辑 / 重试用户消息时回到那一刻），返回删掉的条数。
        被删消息的附图一并删除，keep_images 里的除外（重试时要把原图再发一次）。"""
        with _lock(f"t:{pid}"):
            data = self.store.get_blob(THREADS, pid) or {"messages": []}
            keep = [m for m in data["messages"] if m["seq"] < from_seq]
            gone = [m for m in data["messages"] if m["seq"] >= from_seq]
            top = max((m["seq"] for m in data["messages"]), default=0)
            data["next_seq"] = max(int(data.get("next_seq") or 0), top + 1)
            data["messages"] = keep
            self.store.put_blob(THREADS, pid, data)
        for m in gone:
            self._drop_images(pid, m, keep_images)
        return len(gone)

    def copy_thread(self, src: str, dst: str, upto: int | None = None) -> int:
        """把 src 的对话（到 upto 为止，含）复制给 dst，序号保持不变。"""
        data = self.store.get_blob(THREADS, src) or {"messages": []}
        rows = [dict(m) for m in data["messages"] if upto is None or m["seq"] <= upto]
        for m in rows:
            m["data"] = dict(m.get("data") or {})
            m["data"].pop("streaming", None)
            # 附图复制一份给新对话：各自持有，删掉任何一边都不影响另一边
            refs = []
            for img in m["data"].get("images") or []:
                blob = self.get_image(src, img["id"])
                if blob is not None:
                    refs.append(self.put_image(dst, blob["url"], name=img.get("name", "")))
            if refs:
                m["data"]["images"] = refs
        self.store.put_blob(THREADS, dst, {"messages": rows, "next_seq": (rows[-1]["seq"] + 1) if rows else 1})
        return len(rows)

    def update(self, pid: str, seq: int, **changes: Any) -> None:
        """更新已有消息（工具卡片从“进行中”变为“完成”）。改动后 seq 不变，但给它一个新的 rev 让前端刷新。"""
        with _lock(f"t:{pid}"):
            data = self.store.get_blob(THREADS, pid) or {"messages": []}
            for m in data["messages"]:
                if m["seq"] == seq:
                    if "data" in changes:
                        m["data"] = {**m.get("data", {}), **changes.pop("data")}
                    m.update(changes)
                    m["rev"] = m.get("rev", 0) + 1
                    m["updated_at"] = _now()
            self.store.put_blob(THREADS, pid, data)


def new_project(payload: dict[str, Any]) -> dict[str, Any]:
    goals = {**DEFAULT_GOALS, **{k: v for k, v in (payload.get("goals") or {}).items() if k in DEFAULT_GOALS}}
    idea = (payload.get("idea") or "").strip()
    title = (payload.get("title") or "").strip() or (re.sub(r"\s+", " ", idea)[:24] if idea else "未命名策略项目")
    title_auto = not (payload.get("title") or "").strip()
    raw_symbols = [s.strip().upper() for s in payload.get("securities") or [] if s.strip()]
    # 标的默认交给 AI：只有调用方明确给了标的（或 market_auto=false）才固定下来。
    raw_auto = payload.get("market_auto")
    market_auto = bool(raw_auto) if raw_auto is not None else not raw_symbols
    symbols = [] if market_auto else raw_symbols
    return {
        "id": uuid.uuid4().hex[:12],
        "title": title[:60],
        "title_auto": title_auto,  # 标题由 AI 在首轮回答后总结；研究者改过标题后不再自动改
        "idea": idea,
        "securities": symbols,
        "timeframe": payload.get("timeframe") or "1h",
        "model": payload.get("model") or "",
        "effort": payload.get("effort") or "medium",  # 思考努力程度：low | medium | high | max
        "goals": goals,
        "goal": None,  # 结构化目标（/goal）；为空时按旧字段 goals 评估
        "goal_set": bool(payload.get("goals")),  # 研究者或 AI 设过目标后才能自动迭代
        # 对话管理：置顶、所属项目（文件夹）、分叉来源；上下文超出原文预算时自动压缩
        "pinned": False,
        "folder": (payload.get("folder") or "").strip()[:40],
        "parent_id": payload.get("parent_id"),
        "fork_seq": payload.get("fork_seq"),
        "auto_compact": True,
        "auto_compact_ratio": float(payload.get("auto_compact_ratio") or AUTO_COMPACT_DEFAULT_RATIO),
        # 标的交给 AI 根据对话决定（默认）；AI 用 set_market 设好后清掉
        "market_auto": market_auto,
        # 回答末尾附 2-3 条简短的后续建议（设置里可关）
        "suggest": payload.get("suggest", True) is not False,
        # 单轮最多调用多少次工具（设置里可改，默认 1000）
        "max_tool_calls": int(payload.get("max_tool_calls") or DEFAULT_MAX_TOOL_CALLS),
        # 研究流程：空 = 普通对话；pipeline = 完整流程（选标的→因子→策略→模型→组合→执行）；report = 调研报告
        "workflow": payload.get("workflow") or "",
        # 团队讨论 / 评审的结论自动钉进上下文（默认开）
        "auto_team_context": payload.get("auto_team_context", True) is not False,
        "reports": [],
        "start": payload.get("start"),
        "end": payload.get("end"),
        "max_rounds": int(payload.get("max_rounds") or 4),
        # 权限模式：auto 自动 | manual 手动（改动与运行都先问）| accept_edits 自动接受代码改动、运行先问
        # | plan 先出计划再动手 | bypass 跳过一切确认（含开始迭代前的目标检查）
        "permission": payload.get("permission") or "auto",
        # 完全权限（bypass）下仍要先审批的类别，研究者自选；见 platform_tools.APPROVAL_CATEGORIES
        "approvals": list(payload.get("approvals") or DEFAULT_BYPASS_APPROVALS),
        "bars_limit": int(payload.get("bars_limit") or 1500),
        "data_source": payload.get("data_source") or "auto",  # auto | exchange | synthetic
        "code": "",
        "current_v": 0,
        "versions": [],
        "pins": [],
        "status": "idle",  # idle | running | awaiting_user
        "job": None,
        "stop_requested": False,
        "slug": None,  # 保存为平台策略后的 slug
        "archived": False,
        "compacted_before": 0,  # 序号 ≤ 它的对话已压缩成摘要（钉在上下文里），不再原文发给模型
        "created_at": _now(),
        "updated_at": _now(),
    }
