"""AI 研究室的计划模式。

计划是一个可持久化的状态机：列步骤、逐步执行、每轮检查是否停止或需要用户决定。
独立成模块后，``StrategyLab`` 只保留兼容方法；计划状态仍写回同一个项目集合和消息卡片。
"""

from __future__ import annotations

from typing import Any

from ..llm.base import LLMError
from .strategy_lab import PLAN_MAX_TURNS, PLAN_STATUSES, _now, plan_text


def set_plan(
    lab, pid: str, steps: list[str], *, task: str = "", done_when: str = "", execute: bool = True
) -> dict[str, Any]:
    """列出 / 重列计划：卡片发进对话，之后进度更新都改这张卡片。"""
    project = lab.store.get(pid) or {}
    old = project.get("plan") or {}
    plan = {
        "task": (task or old.get("task") or project.get("idea") or "")[:300],
        "done_when": (done_when or old.get("done_when") or "")[:300],
        "steps": [{"text": t, "status": "pending", "note": ""} for t in steps],
        "status": "active",
        "autorun": bool(execute),
        "turns": 0,
        "created_at": _now(),
    }
    card = lab._post(pid, role="assistant", kind="plan", content=plan["task"], data={"plan": plan})
    plan["seq"] = card["seq"]
    lab.store.patch(pid, plan=plan)
    return plan


def _save_plan(lab, pid: str, plan: dict[str, Any]) -> None:
    lab.store.patch(pid, plan=plan)
    if plan.get("seq"):
        lab.store.update(pid, plan["seq"], data={"plan": plan})


def update_plan_step(
    lab, pid: str, index: int, *, status: str = "done", note: str = "", add_steps: list[str] | None = None
) -> dict[str, Any]:
    project = lab.store.get(pid) or {}
    plan = project.get("plan")
    if not plan:
        return {"ok": False, "error": "还没有计划，先用 set_plan 列计划"}
    steps = plan["steps"]
    if index:
        if not 1 <= index <= len(steps):
            return {"ok": False, "error": f"index 超出范围（1-{len(steps)}）"}
        if status not in PLAN_STATUSES:
            return {"ok": False, "error": f"status 只能是 {'/'.join(PLAN_STATUSES)}"}
        steps[index - 1]["status"] = status
        if note:
            steps[index - 1]["note"] = note[:200]
    for t in add_steps or []:
        if str(t).strip() and len(steps) < 16:
            steps.append({"text": str(t).strip()[:200], "status": "pending", "note": ""})
    lab._save_plan(pid, plan)
    left = [i + 1 for i, s in enumerate(steps) if s["status"] not in ("done", "skipped")]
    return {"ok": True, "remaining": left, "plan": plan_text(plan)}


def _plan_goal_ok(lab, project: dict[str, Any]) -> bool | None:
    """有目标时看当前版本是否达标；没有目标返回 None（按计划步骤是否完成判断）。"""
    if not lab.has_goal(project):
        return None
    cur = next((r for r in project.get("versions") or [] if r.get("v") == project.get("current_v")), None)
    return bool(((cur or {}).get("evaluation") or {}).get("passed"))


def run_plan(lab, pid: str, task: str = "") -> None:
    """/plan：让 AI 列计划（或接着已有计划），然后一步步执行，直到全部完成且目标达标。"""
    project = lab.store.get(pid) or {}
    plan = project.get("plan") or {}
    if task or plan.get("status") != "active":
        what = task or project.get("idea") or project.get("title") or "当前研究"
        lab._phase(pid, "列计划", what[:80])
        lab._plan_turn(
            pid,
            f"/plan {what}\n请先用 set_plan 列出完成这个任务的分步计划"
            "（3-8 步，每步可验证，写清完成标准 done_when），"
            "然后立即开始执行第 1 步。",
        )
    else:
        plan["autorun"] = True
        lab.store.patch(pid, plan=plan)
    lab._plan_loop(pid)


def _continue_plan(lab, pid: str) -> None:
    """普通对话里 AI 用 set_plan(execute=true) 列了计划：这一回合结束后接着推进。"""
    plan = (lab.store.get(pid) or {}).get("plan") or {}
    if plan.get("status") == "active" and plan.get("autorun"):
        lab._plan_loop(pid)


def _plan_turn(lab, pid: str, text: str) -> None:
    try:
        lab._chat_llm(pid, text)
    except LLMError as exc:
        lab._post(pid, role="event", kind="error", content=f"计划执行中模型调用失败：{exc}")
        plan = (lab.store.get(pid) or {}).get("plan")
        if plan:
            plan["status"] = "stopped"
            lab._save_plan(pid, plan)


def _plan_loop(lab, pid: str) -> None:
    stalled = 0
    for _ in range(PLAN_MAX_TURNS):
        lab._check_stop(pid)
        project = lab.store.get(pid) or {}
        plan = project.get("plan")
        if not plan or plan.get("status") != "active" or not plan.get("autorun"):
            return
        if project.get("status") == "awaiting_user":  # AI 提问或等审批：等研究者回答
            return
        steps = plan["steps"]
        left = [i for i, s in enumerate(steps) if s["status"] not in ("done", "skipped")]
        goal_ok = lab._plan_goal_ok(project)
        if not left:
            plan["status"] = "done"
            lab._save_plan(pid, plan)
            lab._post(
                pid,
                role="event",
                kind="text",
                content=f"计划完成：{len(steps)} 步全部做完"
                + ("，目标已达标" if goal_ok else "，未达标项如实保留，不自动追加调参"),
            )
            return
        i = left[0]
        if steps[i]["status"] != "doing":
            steps[i]["status"] = "doing"
            lab._save_plan(pid, plan)
        prompt = (
            f"继续执行计划第 {i + 1} 步：{steps[i]['text']}。完成后用 update_plan_step 标记（写上结果要点），"
            "然后接着做下一步。完成已声明的步骤后如实报告，不为追逐默认指标自动增加试验。"
        )
        lab._phase(pid, f"执行计划 · 第 {i + 1}/{len(steps)} 步", steps[i]["text"][:160])
        before = (
            [s["status"] for s in steps],
            project.get("current_v"),
            len(steps),
        )
        plan["turns"] = int(plan.get("turns") or 0) + 1
        lab.store.patch(pid, plan=plan)
        lab._plan_turn(pid, prompt)
        after_p = lab.store.get(pid) or {}
        after_plan = after_p.get("plan") or {}
        after = (
            [s["status"] for s in after_plan.get("steps") or []],
            after_p.get("current_v"),
            len(after_plan.get("steps") or []),
        )
        stalled = stalled + 1 if after == before else 0
        if stalled >= 2:
            lab._stop_plan(pid, "连续两轮没有进展，先停在这里。可以补充要求后输入 /plan 继续。")
            return
    lab._stop_plan(pid, f"已连续推进 {PLAN_MAX_TURNS} 轮还没完成，先停在这里。输入 /plan 继续。")


def _pause_plan(lab, pid: str) -> None:
    """回合被停止或出错：计划暂停，之后的普通对话不会自动接着推进（/plan 或「继续执行」恢复）。"""
    plan = (lab.store.get(pid) or {}).get("plan")
    if plan and plan.get("status") == "active" and plan.get("autorun"):
        plan["autorun"] = False
        lab._save_plan(pid, plan)


def _stop_plan(lab, pid: str, why: str) -> None:
    plan = (lab.store.get(pid) or {}).get("plan")
    if plan:
        plan["autorun"] = False
        lab._save_plan(pid, plan)
    lab._post(pid, role="event", kind="text", content=why)
