"""研究室通用骨架：模型、组合、执行三类研究共用的目标、版本、自动迭代、团队与决定逻辑。

和因子研究室（factor_lab.py）同一个套路，只是把“产物是什么、怎么评估、怎么改进”留给子类：

- ``submit(pid, artifact, ...)``：解析产物（JSON 规格），记一个新版本并立即评估，返回给模型的简要结果；
- ``evaluate_current(pid)``：评估当前版本，写回版本成绩与目标对照；
- ``_propose`` / ``_rule_proposal``：自动迭代时提出下一版（有模型时让模型提，没有时走规则降级）；
- ``save_to_library(pid, status)``：存进对应的成果库。

产物统一存成**格式化的 JSON 文本**放在项目的 ``code`` 字段里，这样版本对比、手动编辑、上下文展示都能复用。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from ..llm.base import LLMError, Message
from ..research.goals import GoalDomain
from .strategy_lab import (
    NO_GOAL_TEXT,
    StopRequested,
    StrategyLab,
    _json_object,
    _lock,
    _now,
    data_window_text,
    version_history_text,
)


def dumps_spec(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2)


def loads_spec(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    data = _json_object(text) if not text.startswith("{") else None
    if data is None:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("规格必须是一个 JSON 对象")
    return data


def range_text(spec: dict[str, Any]) -> str:
    from datetime import datetime, timezone

    if spec.get("start"):

        def day(ms: int | None) -> str:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ms else "现在"

        return f"{day(spec['start'])} 至 {day(spec.get('end'))}"
    return f"最近 {spec.get('bars_limit') or 1500} 根 K 线"


def base_project(
    kind: str, payload: dict[str, Any], domain: GoalDomain, *, symbols: list[str], team_id: str, title_default: str
) -> dict[str, Any]:
    idea = (payload.get("idea") or "").strip()
    title = (payload.get("title") or "").strip() or (re.sub(r"\s+", " ", idea)[:24] if idea else title_default)
    spec = domain.normalize_spec(
        {"items": domain.templates[0]["items"]},
        {
            "symbols": symbols,
            "timeframe": payload.get("timeframe") or "4h",
            "bars_limit": int(payload.get("bars_limit") or 1500),
            "max_rounds": int(payload.get("max_rounds") or 4),
            "horizon": int(payload.get("horizon") or 1),
        },
    )
    return {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "title": title[:60],
        "idea": idea,
        "securities": symbols,
        "universe": payload.get("universe") or "major10",
        "timeframe": spec["timeframe"],
        "horizon": spec["horizon"],
        "model": payload.get("model") or "",
        "effort": payload.get("effort") or "medium",
        "executor": payload.get("executor") or "solo",
        "team_id": payload.get("team_id") or team_id,
        "goals": {},
        "goal": {**spec, "updated_at": _now()},
        "start": payload.get("start"),
        "end": payload.get("end"),
        "max_rounds": spec["max_rounds"],
        "bars_limit": spec["bars_limit"],
        "data_source": payload.get("data_source") or "auto",
        "cost_bps": float(payload.get("cost_bps") or 10.0),
        "code": "",
        "current_v": 0,
        "versions": [],
        "pins": [],
        "status": "idle",
        "job": None,
        "stop_requested": False,
        "library_id": None,
        "archived": False,
        "compacted_before": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }


class DomainLab(StrategyLab):
    kind = "domain"
    domain: GoalDomain
    team_default = "review_board"
    noun = "方案"
    save_choices: tuple[str, str] = ("入库", "存为候选")
    min_symbols = 1

    # ---------------- 目标 ----------------
    def project_spec(self, project: dict[str, Any]) -> dict[str, Any]:
        goal = project.get("goal") or {}
        return self.domain.normalize_spec(
            {},
            {
                "items": goal.get("items") or self.domain.templates[0]["items"],
                "symbols": project.get("securities") or [],
                "timeframe": project.get("timeframe") or "4h",
                "start": project.get("start"),
                "end": project.get("end"),
                "bars_limit": project.get("bars_limit") or 1500,
                "max_rounds": project.get("max_rounds") or 4,
                "horizon": project.get("horizon") or 1,
                "note": goal.get("note", ""),
            },
        )

    def goal_text(self, project: dict[str, Any]) -> str:
        return self.domain.items_text(self.project_spec(project)["items"]) if self.has_goal(project) else NO_GOAL_TEXT

    def extra_checks(self, project: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
        n = len(spec.get("symbols") or [])
        if n < self.min_symbols:
            return [
                {
                    "field": "symbols",
                    "level": "error",
                    "suggest": None,
                    "problem": f"至少需要 {self.min_symbols} 个标的",
                }
            ]
        return []

    def check_goal(self, project: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        history = [v.get("metrics") for v in project.get("versions") or [] if v.get("metrics")]
        return self.domain.check(spec, history=history, extra=self.extra_checks(project, spec))

    def set_goal(self, pid: str, raw: dict[str, Any], *, source: str = "user", review: bool = False) -> dict[str, Any]:
        self.store.patch(pid, goal_set=True)
        with _lock(f"p:{pid}"):
            project = self.store.get(pid) or {}
            spec = self.domain.normalize_spec(raw, self.project_spec(project))
            project.update(
                goal={**spec, "updated_at": _now()},
                securities=spec["symbols"] or project["securities"],
                timeframe=spec["timeframe"],
                start=spec.get("start"),
                end=spec.get("end"),
                bars_limit=spec["bars_limit"],
                max_rounds=spec["max_rounds"],
                horizon=spec["horizon"],
            )
            self.store.save(project)
        check = self.check_goal(project, spec)
        self._post(
            pid,
            role="event",
            kind="goal",
            content=f"目标：{self.domain.items_text(spec['items'])}",
            data={
                "stage": "set",
                "source": source,
                "spec": spec,
                "range": range_text(spec),
                "check": check,
                "domain": self.kind,
            },
        )
        return {"spec": spec, "check": check}

    # ---------------- 版本 ----------------
    def current(self, project: dict[str, Any]) -> dict[str, Any] | None:
        return next((r for r in project.get("versions") or [] if r["v"] == project.get("current_v")), None)

    def add_version(
        self,
        pid: str,
        code: str,
        *,
        source: str,
        note: str = "",
        checks: dict | None = None,
        model: str = "",
        name: str = "",
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with _lock(f"p:{pid}"):
            project = self.store.get(pid) or {}
            versions = project.get("versions") or []
            v = (versions[-1]["v"] if versions else 0) + 1
            row = {
                "v": v,
                "code": code,
                "source": source,
                "note": note[:300],
                "model": model,
                "name": name[:40],
                "params": {},
                "checks_ok": True,
                "created_at": _now(),
                "metrics": None,
                "bt_id": None,
                "evaluation": None,
                "admitted": None,
                **(meta or {}),
            }
            versions.append(row)
            project.update(versions=versions, code=code, current_v=v)
            self.store.save(project)
            return row

    def restore(self, pid: str, v: int, *, note: str = "") -> dict[str, Any] | None:
        project = self.store.get(pid) or {}
        row = next((r for r in project.get("versions") or [] if r["v"] == v), None)
        if row is None:
            return None
        self.store.patch(pid, code=row["code"], current_v=v)
        self._post(pid, role="event", kind="text", content=note or f"已切换到 v{v}")
        return row

    def _tried(self, project: dict[str, Any]) -> set[str]:
        return {self.fingerprint(r["code"]) for r in project.get("versions") or []}

    def fingerprint(self, code: str) -> str:
        try:
            return json.dumps(loads_spec(code), sort_keys=True, ensure_ascii=False)
        except Exception:
            return code.strip()

    # ---------------- 子类实现 ----------------
    created_hint = "可以描述你的想法，或点「自动迭代」。"

    def new_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def normalize(self, project: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        return raw

    def submit(
        self, pid: str, artifact: Any, *, note: str = "", name: str = "", model: str = "", source: str = "ai"
    ) -> dict[str, Any]:
        raise NotImplementedError

    def evaluate_current(self, pid: str, **kw: Any) -> dict[str, Any]:
        raise NotImplementedError

    def save_to_library(self, pid: str, *, status: str = "active") -> dict[str, Any] | None:
        raise NotImplementedError

    def summary_text(self, s: dict[str, Any] | None) -> str:
        return json.dumps(s or {}, ensure_ascii=False)[:400]

    def proposal_prompt(self, project: dict[str, Any], r: int, history: list[str], notes: str) -> str:
        raise NotImplementedError

    def _rule_proposal(self, pid: str, project: dict[str, Any], r: int, tried: set[str]) -> dict[str, Any] | None:
        return None

    def team_context(self, project: dict[str, Any]) -> str:
        cur = self.current(project)
        return (
            f"{self.domain.label}研究项目：{project.get('title')}\n想法：{project.get('idea') or '—'}\n"
            f"标的：{','.join(project['securities'][:20])}，周期 {project['timeframe']}\n"
            f"目标：{self.goal_text(project)}\n当前{self.noun}：\n{(project.get('code') or '（尚无）')[:3000]}\n"
            f"最近评估：{self.summary_text((cur or {}).get('metrics'))}\n"
            f"数据窗口：{data_window_text(project)}\n"
            f"版本演进（每版的改动与评估，后续版本的改动是有意的迭代）：\n"
            f"{version_history_text(project, fmt=self.summary_text)}"
        )

    def finish_warnings(self, project: dict[str, Any], best: dict[str, Any]) -> list[str]:
        return []

    # ---------------- 通用：评估别名 ----------------
    def backtest_current(self, pid: str, *, cost_scale: float = 1.0, label: str = "") -> dict[str, Any]:
        return self.evaluate_current(pid, cost_scale=cost_scale)

    def walk_forward(self, pid: str, *, segments: int = 3) -> dict[str, Any]:
        return {"ok": False, "error": f"{self.domain.label}评估已经包含样本外检验"}

    # ---------------- 团队 ----------------
    def _convene(self, pid: str, topic: str, *, team_id: str | None = None) -> dict[str, Any]:
        if self.tools.team is None:
            return {"ok": False, "error": "团队功能不可用"}
        project = self.store.get(pid) or {}
        self._phase(pid, f"{self.domain.label}小组讨论中")
        run = self._tool(
            pid,
            "convene_team",
            f"召集{self.domain.label}小组",
            {"议题": topic[:40]},
            lambda: self.tools.team(
                topic,
                team_id=team_id or project.get("team_id") or self.team_default,
                context=self.team_context(project),
            ),
        )
        self._post(
            pid,
            role="tool",
            kind="team",
            content=run.get("synthesis", "")[:4000],
            data={
                "run_id": run.get("id"),
                "team_name": run.get("team_name"),
                "mode": run.get("mode"),
                "status": run.get("status"),
                "topic": topic,
                "steps": [
                    {
                        "role_id": s.get("role_id"),
                        "role_name": s.get("role_name"),
                        "status": s.get("status"),
                        "model": s.get("model"),
                        "output": (s.get("output") or s.get("error") or "")[:1500],
                    }
                    for s in run.get("steps", [])
                ],
            },
        )
        if run.get("synthesis"):
            self.pin(pid, kind="team", title=f"小组结论 · {topic[:20]}", text=run["synthesis"][:3000])
        return {"ok": run.get("status") == "done", "synthesis": (run.get("synthesis") or "")[:3000]}

    # ---------------- 自动迭代 ----------------
    def autopilot(
        self,
        pid: str,
        *,
        rounds: int | None = None,
        research: str = "none",
        team_id: str | None = None,
        force: bool = False,
    ) -> None:
        project = self.store.get(pid) or {}
        spec = self.project_spec(project)
        check = self.check_goal(project, spec)
        if check["verdict"] == "unreasonable" and not force and project.get("permission") != "bypass":
            self._post(
                pid,
                role="event",
                kind="goal",
                content="目标需要调整",
                data={
                    "stage": "check",
                    "spec": spec,
                    "range": range_text(spec),
                    "check": check,
                    "domain": self.kind,
                    "resume": {"rounds": rounds, "research": research},
                },
            )
            self._ask(
                pid,
                f"开始前检查了一下目标：{check['summary']}要怎么处理？",
                ["按建议修改后开始", "仍然开始", "先不开始"],
                decision=True,
            )
            return
        max_rounds = max(1, min(int(rounds or project.get("max_rounds") or 4), 8))
        self._post(
            pid,
            role="event",
            kind="round",
            content="开始自动迭代",
            data={
                "stage": "start",
                "goals_text": self.domain.items_text(spec["items"]),
                "rounds": max_rounds,
                "range": range_text(spec),
                "research": research,
                "domain": self.kind,
            },
        )
        notes = ""
        if project.get("executor") == "team" or research == "team":
            self._check_stop(pid)
            out = self._convene(
                pid, f"为这个研究方向给出具体方案与理由：{project.get('idea') or project.get('title')}", team_id=team_id
            )
            notes = out.get("synthesis", "")[:2500]
        history: list[str] = []
        scores: list[float] = []
        for r in range(1, max_rounds + 1):
            self._check_stop(pid)
            project = self.store.get(pid) or {}
            cur = self.current(project)
            if cur is not None and (cur.get("evaluation") or {}).get("passed") and r == 1:
                self._post(pid, role="event", kind="text", content=f"当前{self.noun}已经达标。")
                break
            self._phase(pid, f"第 {r} 轮 · {'提出方案' if not project.get('code') else '改进'}")
            proposal = self._propose(pid, project, r, history, notes)
            if proposal is None:
                break
            out = self.submit(
                pid,
                proposal["artifact"],
                note=proposal.get("note") or f"第 {r} 轮",
                name=proposal.get("name", ""),
                model=proposal.get("model", ""),
                source=proposal.get("source", "ai"),
            )
            if not out.get("ok"):
                history.append(f"- 第 {r} 轮：无法评估（{str(out.get('error', ''))[:80]}）")
                continue
            ev = ((self.store.get(pid) or {}).get("versions") or [{}])[-1].get("evaluation") or {}
            history.append(f"- 第 {r} 轮：{proposal.get('note') or ''} → {self.summary_text(out.get('summary'))[:200]}")
            self._post(
                pid,
                role="event",
                kind="round",
                content=f"第 {r} 轮结束",
                data={
                    "stage": "round",
                    "round": r,
                    "v": out.get("version"),
                    "passed": ev.get("passed"),
                    "items": ev.get("items"),
                    "metrics": out.get("summary"),
                    "domain": self.kind,
                },
            )
            if ev.get("passed"):
                break
            scores.append(ev.get("score") or 0)
            gaps = self.domain.gap_report(ev)
            if (
                r >= 3
                and len(scores) >= 3
                and max(scores[-2:]) <= scores[-3] + 0.05
                and gaps
                and all(x["gap"] > 0.7 for x in gaps if x["priority"] == "must")
            ):
                self._post(
                    pid,
                    role="event",
                    kind="goal",
                    content="提前结束迭代",
                    data={
                        "stage": "early_stop",
                        "domain": self.kind,
                        "gaps": gaps,
                        "reason": "连续几轮没有进步且离目标很远，继续试只会增加假阳性。",
                    },
                )
                break
        self._finish(pid)

    def _propose(
        self, pid: str, project: dict[str, Any], r: int, history: list[str], notes: str
    ) -> dict[str, Any] | None:
        tried = self._tried(project)
        try:
            prompt = self.proposal_prompt(project, r, history, notes)
            result = self._tool(
                pid,
                f"propose_{self.kind}",
                f"第 {r} 轮：{'提出方案' if not project.get('code') else '改进'}",
                {"已试": len(tried)},
                lambda: self._router_for(project).chat(
                    [Message(role="system", content=self.system_prompt), Message(role="user", content=prompt)],
                    role="reasoning",
                    temperature=0.4,
                    max_tokens=2500,
                ),
            )
            data = _json_object(result.content or "")
            art = (data or {}).get("spec") or (data or {}).get("artifact")
            if isinstance(art, dict) and self.fingerprint(json.dumps(art)) not in tried:
                if data.get("note"):
                    self._post(
                        pid,
                        role="assistant",
                        kind="text",
                        mode="llm",
                        model=f"{result.provider}/{result.model}",
                        content=str(data["note"])[:1500],
                    )
                return {
                    "artifact": art,
                    "note": data.get("note") or "",
                    "name": data.get("name") or "",
                    "model": f"{result.provider}/{result.model}",
                    "source": "ai",
                }
        except StopRequested:
            raise
        except (LLMError, Exception):
            pass
        return self._rule_proposal(pid, project, r, tried)

    def _finish(self, pid: str) -> None:
        project = self.store.get(pid) or {}
        scored = [r for r in project.get("versions") or [] if r.get("evaluation")]
        if not scored:
            return
        best = max(scored, key=lambda r: (r["evaluation"]["score"], bool(r.get("admitted"))))
        if best["v"] != project.get("current_v"):
            self.restore(pid, best["v"], note="自动迭代：回到评估最好的一版")
        self._check_stop(pid)
        warn = self.finish_warnings(self.store.get(pid) or {}, best)
        passed = best["evaluation"]["passed"]
        self._post(
            pid,
            role="event",
            kind="round",
            content="迭代完成",
            data={
                "stage": "done",
                "best_v": best["v"],
                "passed": passed,
                "items": best["evaluation"]["items"],
                "metrics": best.get("metrics"),
                "admitted": best.get("admitted"),
                "warnings": warn,
                "domain": self.kind,
            },
        )
        verdict = "已达成全部目标" if passed else "还没有全部达标"
        adm = "，检查全部通过" if best.get("admitted") else "，检查有未通过项"
        self._ask(
            pid,
            f"迭代结束：v{best['v']} {verdict}{adm}。{'；'.join(warn) + '。' if warn else ''}"
            f"要{self.save_choices[0]}吗？",
            [*self.save_choices, "继续优化", "换个方向"],
            decision=True,
        )

    # ---------------- 用户决定 ----------------
    def _answer_open(self, pid: str, choice: str, note: str) -> None:
        for m in self.store.messages(pid):
            if m["kind"] == "ask" and m["data"].get("answered") is None:
                self.store.update(pid, m["seq"], data={"answered": choice})
        self._answered(pid)
        self._post(pid, role="user", kind="text", content=choice + (f"：{note}" if note else ""))

    def decide(self, pid: str, choice: str, note: str = "") -> None:
        if self._decide_permission(pid, choice, note):
            return
        if choice in self.save_choices:
            self._answer_open(pid, choice, note)
            self.save_to_library(pid, status="active" if choice == self.save_choices[0] else "candidate")
            return
        if choice.startswith("按建议修改") or choice == "仍然开始":
            self._answer_open(pid, choice, note)
            if choice != "仍然开始":
                project = self.store.get(pid) or {}
                spec = self.project_spec(project)
                self.set_goal(
                    pid, self.domain.apply_suggestions(spec, self.check_goal(project, spec)), source="suggest"
                )
            self.autopilot(pid, force=True)
            return
        super().decide(pid, choice, note)

    # ---------------- 对话 ----------------
    def _context_block(self, project: dict[str, Any]) -> str:
        cur = self.current(project)
        parts = [
            f"【项目】{project.get('title')}　标的 {len(project['securities'])} "
            f"个（{','.join(project['securities'][:12])}）"
            f"　周期 {project['timeframe']}",
            f"【想法】{project.get('idea') or '—'}",
            f"【目标】{self.goal_text(project)}",
        ]
        for p in project.get("pins") or []:
            parts.append(f"【{p['title']}】\n{p['text'][:2000]}")
        if project.get("code"):
            parts.append(f"【当前{self.noun} v{project.get('current_v')}】\n{project['code'][:6000]}")
        parts.append(f"【最近评估】{self.summary_text((cur or {}).get('metrics'))}")
        return "\n\n".join(parts)


__all__ = ["DomainLab", "base_project", "dumps_spec", "loads_spec", "range_text"]
