"""Agent 主动跟进：AI 在对话里发起的后台任务（智能调参、压力演练、策略体检）完成后，
自动回到原对话汇报结果——研究者不用守着页面反复问「好了没有」。

做法：平台工具启动这些任务时登记一条「待跟进」（带上当时的请求上下文：工作区、成员）；
后台线程每 8 秒查一次结果，完成或失败后：
1. 在原对话里追加一条「后台任务完成」的系统消息，并以它为输入启动一个回合，让 AI 汇报结论与下一步；
2. 给发起人发一条站内通知（头像红点），点开直接回到这个对话。
对话正在运行时先不打断，等它结束再汇报；任务超过 3 小时还没结束就放弃跟进。
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("quantlab.followup")

POLL_SECONDS = 8.0
GIVE_UP_SECONDS = 3 * 3600


def _fetch(tool: str, run_id: str) -> tuple[bool, Any]:
    """查任务结果，返回 (是否已结束, 结果摘要)。"""
    from . import platform_tools as PT

    if tool == "start_tuning":
        res = PT.t_tuning_result(opt_id=run_id)
        state = res.get("state") if isinstance(res, dict) else None
        finished = isinstance(res, dict) and (
            bool(res.get("best_params")) or state in ("FAILED", "CANCELED") or "error" in res
        )
        return finished, res
    if tool == "start_stress_drill":
        res = PT.t_stress_result(run_id=run_id)
        return isinstance(res, dict) and res.get("status") in ("done", "failed", "error"), res
    if tool == "run_checkup":
        res = PT.t_checkup_result(run_id=run_id)
        return isinstance(res, dict) and res.get("status") in ("done", "failed", "error"), res
    return True, {"error": f"未知的跟进类型 {tool}"}


LABEL = {"start_tuning": "智能调参", "start_stress_drill": "压力演练", "run_checkup": "策略体检"}


@dataclass
class _Watch:
    lab: Any
    pid: str
    tool: str
    run_id: str
    ctx: contextvars.Context
    user_id: str
    created: float = field(default_factory=time.time)


class FollowUps:
    def __init__(self) -> None:
        self._items: list[_Watch] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def watch(self, lab: Any, pid: str, tool: str, result: Any) -> None:
        """平台工具返回后调用：是可跟进的任务且启动成功就登记。"""
        if tool not in LABEL or not isinstance(result, dict) or not result.get("run_id"):
            return
        from ..platform import context

        item = _Watch(
            lab, pid, tool, str(result["run_id"]), contextvars.copy_context(), context.current().user_id or ""
        )
        with self._lock:
            self._items.append(item)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="lab-followup")
                self._thread.start()

    #: 查结果的工具 → 它对应的发起工具
    RESULT_OF = {
        "tuning_result": "start_tuning",
        "stress_result": "start_stress_drill",
        "checkup_result": "run_checkup",
    }

    def seen(self, pid: str, tool: str, out: Any) -> None:
        """AI 自己查到了已结束的结果：对应的跟进取消，不再重复汇报。"""
        start = self.RESULT_OF.get(tool)
        if not start or not isinstance(out, (dict, list)):
            return
        text = json.dumps(out, ensure_ascii=False, default=str)
        finished = isinstance(out, list) or bool(
            out.get("best_params")
            or out.get("status") in ("done", "failed", "error")
            or out.get("state") in ("FAILED", "CANCELED")
        )
        if not finished:
            return
        with self._lock:
            for w in [w for w in self._items if w.pid == pid and w.tool == start]:
                # 列表形式的结果里要能看到这个任务，单个结果默认就是最近发起的那个
                if isinstance(out, list) and w.run_id not in text:
                    continue
                self._items.remove(w)

    def pending(self, pid: str) -> list[dict[str, str]]:
        with self._lock:
            return [{"tool": w.tool, "run_id": w.run_id} for w in self._items if w.pid == pid]

    def _loop(self) -> None:
        while True:
            time.sleep(POLL_SECONDS)
            with self._lock:
                items = list(self._items)
            if not items:
                with self._lock:
                    if not self._items:
                        self._thread = None
                        return
                continue
            for w in items:
                try:
                    done = w.ctx.run(self._check, w)
                except Exception:  # 单个跟进出错不影响其他
                    log.exception("跟进 %s/%s 失败", w.pid, w.run_id)
                    done = True
                if done:
                    with self._lock:
                        if w in self._items:
                            self._items.remove(w)

    def _check(self, w: _Watch) -> bool:
        project = w.lab.store.get(w.pid)
        if project is None:
            return True  # 对话已删除
        if time.time() - w.created > GIVE_UP_SECONDS:
            w.lab.store.append(
                w.pid,
                {
                    "role": "event",
                    "kind": "text",
                    "content": f"{LABEL[w.tool]} {w.run_id} 超过 3 小时还没结束，不再自动跟进",
                },
            )
            return True
        finished, res = _fetch(w.tool, w.run_id)
        if not finished:
            return False
        if project.get("status") == "running":
            return False  # 对话正忙：等这一轮结束再汇报
        # AI 正在等研究者回答（审批卡、提问）：不另起回合把问题冲掉，只把结果写进对话、发通知
        self._deliver(w, project, res, start_turn=project.get("status") != "awaiting_user")
        return True

    def _deliver(self, w: _Watch, project: dict[str, Any], res: Any, *, start_turn: bool = True) -> None:
        label = LABEL[w.tool]
        summary = json.dumps(res, ensure_ascii=False, default=str)
        if len(summary) > 6000:
            summary = summary[:6000] + "…（已截断）"
        text = (
            f"【后台任务完成】你之前发起的{label}（{w.run_id}）已经结束，结果如下：\n{summary}\n"
            "请主动向研究者汇报：结论是什么、可信度如何（样本区间、成交数、是否失败及原因）、建议的下一步。"
        )
        if not start_turn:
            w.lab.store.append(
                w.pid,
                {
                    "role": "event",
                    "kind": "text",
                    "content": f"{label} {w.run_id} 已完成；回答上面的问题后 AI 会一起汇报",
                },
            )
            w.lab.store.append(w.pid, {"role": "user", "kind": "text", "content": text, "data": {"auto": "followup"}})
        else:
            w.lab.store.append(
                w.pid, {"role": "event", "kind": "text", "content": f"{label} {w.run_id} 已完成，AI 正在汇报结果"}
            )
            w.lab.store.append(w.pid, {"role": "user", "kind": "text", "content": text, "data": {"auto": "followup"}})
            try:
                w.lab.start(w.pid, "chat", lambda: w.lab.chat(w.pid, text), wait=False)
            except RuntimeError:
                pass  # 刚好有人发了消息：结果已经在对话里，下一轮 AI 会看到
        try:
            from ..api.deps import get_services

            get_services().collab.notify(
                [w.user_id],
                kind="system",
                title=f"{label}完成：{project.get('title') or '研究对话'}",
                body="AI 已回到对话里汇报结果",
                link=f"lab/{w.pid}",
                source_type="project",
                source_id=w.pid,
                workspace_id=None,
            )
        except Exception:
            log.debug("跟进通知发送失败", exc_info=True)


FOLLOWUPS = FollowUps()
