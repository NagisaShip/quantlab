"""执行研究室（研究类型 = 执行）：设计拆单算法与参数，在盘口回放里仿真，按交易成本分析（TCA）验收。

产物是一份**执行方案**（JSON）：算法、执行期、切片、挂单风格、参与率上限、状态感知规则。每一版都在同一批执行片段上
和 TWAP、市价两个基准比较，给出成本分解（价差 / 冲击 / 择时 / 手续费 / 机会成本）、按市场状态的成本、成本-风险前沿、
母单规模的成本曲线，以及一个片段的逐步回放。全程仿真，不下任何真实委托。
没有模型密钥时走规则降级：TWAP → 被动 TWAP → VWAP → 自适应 → AC → POV → 冰山。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..llm.base import LLMError, Tool
from ..research import execution as EX
from ..research.execution_lib import ExecutionLibrary
from ..research.goals import EXECUTION
from .research_lab import DomainLab, base_project, dumps_spec, loads_spec
from .strategy_lab import StopRequested, _short

EXECUTION_SYSTEM = """你是 QuantLab 平台的执行交易员兼交易成本分析师，和研究者一起设计拆单执行方案。全程是仿真，没有真实下单。

## 执行方案（write_execution 的参数）
{"algo": "market|twap|vwap|pov|iceberg|almgren_chriss|adaptive", "horizon": 30, "slices": 6,
 "style": "passive|neutral|aggressive", "participation": 0.1, "max_participation": 0.25, "urgency": 0.5,
 "display": 0.2, "reprice_bps": 5, "finish": true, "state_model": "rules|hmm",
 "adapt": {"behind": 0.08, "ahead": 0.08, "pause_illiquid": true},
 "order": {"side": "both|buy|sell", "size_pct": 0.05}, "fees": {"maker_bps": 1, "taker_bps": 5},
 "impact": {"resilience": 1.0, "permanent": 0.3}, "episodes": 30}
- horizon / slices 以盘口带的步数计（1 分钟 K 线推算时 1 步 = 1 分钟）；size_pct 是母单占执行期预计成交量的比例。
- adaptive：默认被动挂单，落后计划超过 behind 或行情对自己不利时转主动，流动性枯竭时只挂单。
- almgren_chriss：urgency 越大越前置（κT 从 0.1 到 6）。

## 平台怎么评估
在同一批执行片段（买卖交替）上跑方案、TWAP、市价；成本相对到达中间价：价差 + 冲击 + 择时 + 手续费 + 机会成本 = 执行落差（IS）。
还给出相对区间 VWAP、完成率、被动成交占比、成交后 markout（逆向选择）、按市场状态（平静 / 趋势 / 剧烈波动 / 流动性枯竭）的成本、
成本-风险前沿和母单规模的成本曲线（平方根冲击拟合）。

## 工作方式
1. 先用 list_inputs 看标的、盘口来源、可用录制与成本模型。
2. 从 TWAP 起步，再根据成本分解改：价差成本高 → 多挂单；机会成本 / 落后多 → 提高急迫度；markout 很负 → 被动成交被逆向选择，要少挂。
3. 目标用 set_execution_goal / check_goals 管理；达标或需要研究者判断时用 ask_user。

## 铁律
- 片段之间成本波动很大，差异小于成本波动 / √片段数 时不要说“更好”。
- 盘口由 K 线推算或合成时，被动成交的结论只能作参考，要说明。
- 回答用中文，简洁，先结论后细节。"""  # noqa: E501 提示词原文，不折行


@dataclass
class ExecutionDeps:
    tapes: Callable[..., list[EX.Tape]]
    library: Callable[[], ExecutionLibrary]


def new_execution_project(payload: dict[str, Any]) -> dict[str, Any]:
    symbols = [s.strip().upper() for s in payload.get("securities") or [] if s.strip()] or ["BTC/USDT", "ETH/USDT"]
    p = base_project(
        "execution",
        {**payload, "timeframe": "1m", "bars_limit": payload.get("bars_limit") or 3000},
        EXECUTION,
        symbols=symbols[:4],
        team_id="execution_team",
        title_default="未命名执行研究",
    )
    p["scheme_id"] = None
    return p


def summary_text(s: dict[str, Any] | None) -> str:
    if not s:
        return "尚未评估"

    def f(k: str, pct: bool = False) -> str:
        v = s.get(k)
        if v is None:
            return "—"
        return f"{v * 100:.1f}%" if pct else f"{v:.2f}"

    head = f"{EX.ALGOS.get(s.get('algo') or '', {}).get('label', s.get('algo'))} · {s.get('horizon')} 步"
    return (
        f"{head}：执行落差 {f('is_bps')} bp（TWAP {f('twap_is_bps')}，市价 {f('market_is_bps')}），"
        f"成本波动 {f('is_std_bps')} bp，相对 VWAP {f('vs_vwap_bps')} bp，完成 {f('fill_rate', True)}，"
        f"被动成交 {f('maker_ratio', True)}，markout(5) {f('markout_5')} bp"
    )


class ExecutionLab(DomainLab):
    kind = "execution"
    domain = EXECUTION
    team_default = "execution_team"
    noun = "执行方案"
    save_choices = ("启用", "存为候选")
    system_prompt = EXECUTION_SYSTEM
    system_title = "执行方案与 TCA 口径"
    artifact_label = "当前执行方案"
    metrics_label = "最近一次 TCA"
    min_symbols = 1

    created_hint = (
        "描述你的执行问题（例如“在 30 分钟内买入相当于 5% 成交量的 BTC，尽量比 TWAP 便宜”），"
        "或点「自动迭代」从 TWAP 开始。"
    )

    def __init__(self, router: Any, store: Any, tools: Any, deps: ExecutionDeps) -> None:
        super().__init__(router, store, tools)
        self.deps = deps

    def new_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        return new_execution_project(payload)

    def summary_text(self, s: dict[str, Any] | None) -> str:
        return summary_text(s)

    def normalize(self, project: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        try:
            return EX.normalize_config(raw)
        except EX.ExecutionError as exc:
            raise ValueError(str(exc)) from exc

    # ---------------- 产物 ----------------
    def submit(
        self, pid: str, artifact: Any, *, note: str = "", name: str = "", model: str = "", source: str = "ai"
    ) -> dict[str, Any]:
        project = self.store.get(pid) or {}
        try:
            raw = loads_spec(artifact) if isinstance(artifact, str) else dict(artifact or {})
            cfg = self.normalize(project, raw)
        except (ValueError, json.JSONDecodeError) as exc:
            self._post(
                pid,
                role="tool",
                kind="tool",
                data={
                    "name": "write_execution",
                    "label": "提交执行方案",
                    "args": {},
                    "status": "error",
                    "error": str(exc),
                },
            )
            return {"ok": False, "error": str(exc), "hint": "修正方案后重新提交"}
        before = project.get("code") or ""
        code = dumps_spec(cfg)
        row = self.add_version(pid, code, source=source, note=note, model=model, name=name)
        self._post(
            pid,
            role="tool",
            kind="execution_spec",
            content=note or "提交执行方案",
            data={
                "v": row["v"],
                "before": before,
                "after": code,
                "spec": cfg,
                "algo_label": EX.ALGOS[cfg["algo"]]["label"],
                "style_label": EX.STYLES[cfg["style"]],
            },
        )
        return self.evaluate_current(pid)

    def _tapes(self, project: dict[str, Any]) -> list[EX.Tape]:
        src = project.get("data_source") or "auto"
        rec = src.split(":", 1)[1] if src.startswith("recording:") else None
        return self.deps.tapes(
            project["securities"],
            source="recording" if rec else src,
            steps=int(project.get("bars_limit") or 3000),
            recording_id=rec,
        )

    def evaluate_current(self, pid: str, *, cost_scale: float = 1.0, **_: Any) -> dict[str, Any]:
        project = self.store.get(pid) or {}
        if not project.get("code"):
            return {"ok": False, "error": "当前还没有执行方案"}
        v = project.get("current_v") or 0
        try:
            cfg = EX.normalize_config(loads_spec(project["code"]))
        except (ValueError, EX.ExecutionError) as exc:
            self._post(pid, role="event", kind="error", content=f"方案有误：{exc}")
            return {"ok": False, "error": str(exc)}
        stress = cost_scale != 1.0
        if stress:  # 成本压力：手续费与冲击都放大
            cfg = {
                **cfg,
                "fees": {k: x * cost_scale for k, x in cfg["fees"].items()},
                "impact": {**cfg["impact"], "resilience": cfg["impact"]["resilience"] * cost_scale},
            }
        try:
            report = self._tool(
                pid,
                "simulate_execution",
                "盘口回放仿真" + (f"（{cost_scale:g}× 成本）" if stress else ""),
                {
                    "版本": f"v{v}",
                    "算法": EX.ALGOS[cfg["algo"]]["label"],
                    "执行期": f"{cfg['horizon']} 步",
                    "标的": f"{len(project['securities'])} 个",
                },
                lambda: EX.evaluate_scheme(cfg, self._tapes(project), stop=lambda: self._check_stop(pid)),
            )
        except StopRequested:
            raise
        except (EX.ExecutionError, ValueError) as exc:
            self._post(pid, role="event", kind="error", content=f"仿真没有完成：{exc}")
            return {"ok": False, "error": str(exc)}
        s = report["summary"]
        # 没设目标时不拿出厂默认阈值评判，只汇报实测结果
        evaluation = EXECUTION.evaluate(s, self.project_spec(project)["items"]) if self.has_goal(project) else None
        run_id = None
        if not stress:
            run = self.deps.library().record_run(
                cfg,
                report,
                symbols=project["securities"],
                source="lab",
                project_id=pid,
                note=(self.current(project) or {}).get("note") or "",
            )
            run_id = run["id"]
            self._set_version_result(
                pid, v, metrics=s, evaluation=evaluation, admitted=report["admitted"], run_id=run_id
            )
        ex = report.get("example") or {}
        self._post(
            pid,
            role="tool",
            kind="execution_report",
            content=summary_text(s),
            data={
                "v": v,
                "run_id": run_id,
                "summary": s,
                "checks": report["checks"],
                "admitted": report["admitted"],
                "evaluation": None if stress else evaluation,
                "cost_scale": cost_scale,
                "by_state": report["by_state"],
                "frontier": report["frontier"],
                "tape": report["tape"],
                "example": {k: ex.get(k) for k in ("timeline", "alerts", "fills", "side", "symbol", "is_bps")}
                if ex
                else None,
            },
        )
        return {
            "ok": True,
            "version": v,
            "run_id": run_id,
            "summary": {
                k: s.get(k)
                for k in (
                    "is_bps",
                    "is_std_bps",
                    "twap_is_bps",
                    "market_is_bps",
                    "beat_twap_bps",
                    "spread_bps",
                    "impact_bps",
                    "timing_bps",
                    "fee_bps",
                    "opportunity_bps",
                    "vs_vwap_bps",
                    "fill_rate",
                    "maker_ratio",
                    "markout_5",
                    "episodes",
                )
            },
            "by_state": report["by_state"],
            "frontier": report["frontier"],
            "cost_curve": {k: report["cost_curve"][k] for k in ("a_bps", "b_bps", "r2")},
            "checks": [{"label": c["label"], "ok": c["ok"], "detail": c["detail"]} for c in report["checks"]],
            "goal": (
                {"passed": evaluation["passed"], "gaps": EXECUTION.gap_report(evaluation)}
                if evaluation
                else "未设定验收目标：只汇报实测结果，不要说达标或未达标"
            ),
            "warnings": report["tape"]["warnings"][:3],
        }

    def report(self, pid: str, v: int) -> dict[str, Any] | None:
        project = self.store.get(pid) or {}
        row = next((r for r in project.get("versions") or [] if r["v"] == v), None)
        if not row or not row.get("run_id"):
            return None
        return self.deps.library().run_report(row["run_id"])

    def save_to_library(self, pid: str, *, status: str = "active") -> dict[str, Any] | None:
        project = self.store.get(pid) or {}
        cur = self.current(project) or {}
        if not cur.get("run_id"):
            self._post(pid, role="event", kind="error", content="当前版本还没有仿真，先评估再存入执行方案库")
            return None
        if status == "active" and not cur.get("admitted"):
            status = "candidate"
            self._post(
                pid,
                role="event",
                kind="text",
                content="还没有通过准入检查（做完率、不比 TWAP 差、片段数），先存为候选。",
            )
        lib = self.deps.library()
        report = self.report(pid, cur["v"])
        rec = self._tool(
            pid,
            "save_execution",
            "存入执行方案库",
            {"状态": "启用" if status == "active" else "候选"},
            lambda: lib.save(
                name=cur.get("name") or project.get("title") or "",
                config=loads_spec(cur["code"]),
                report=report,
                symbols=project["securities"],
                status=status,
                note=project.get("idea") or "",
                scheme_id=project.get("scheme_id"),
                project_id=pid,
                run_id=cur["run_id"],
                created_by="ai" if cur.get("source") != "user" else "user",
            ),
        )
        self.store.patch(pid, scheme_id=rec["id"], library_id=rec["id"])
        self._post(
            pid,
            role="event",
            kind="saved",
            content=f"已存入执行方案库：{rec['name']}",
            data={"exec_id": rec["id"], "name": rec["name"], "status": rec["status"], "kind": "execution"},
        )
        return rec

    def finish_warnings(self, project: dict[str, Any], best: dict[str, Any]) -> list[str]:
        s = best.get("metrics") or {}
        warn = []
        if (s.get("beat_twap_bps") or 0) < 0:
            warn.append("没有比 TWAP 便宜")
        if (
            s.get("is_std_bps")
            and s.get("episodes")
            and abs(s.get("beat_twap_bps") or 0) < s["is_std_bps"] / max(s["episodes"], 1) ** 0.5
        ):
            warn.append("和 TWAP 的差距小于统计误差，不能说明更好")
        rep = self.report(project["id"], best["v"]) or {}
        t = rep.get("tape") or {}
        if t.get("synthetic"):
            warn.append("用了合成盘口，结论不能作为研究结论")
        elif t.get("kline"):
            warn.append("盘口由 K 线推算，被动成交的结论只能参考；可以先录制一段真实盘口再验证")
        return warn

    # ---------------- 自动迭代 ----------------
    def proposal_prompt(self, project: dict[str, Any], r: int, history: list[str], notes: str) -> str:
        cur = self.current(project)
        return (
            f"研究方向：{project.get('idea') or project.get('title')}\n目标：{self.goal_text(project)}\n"
            f"标的：{','.join(project['securities'])}，盘口来源：{project.get('data_source') or 'auto'}。\n"
            + (f"小组建议：\n{notes}\n" if notes else "")
            + (
                f"当前方案：{project.get('code')}\n最近评估：{summary_text((cur or {}).get('metrics'))}\n"
                f"未达标：{json.dumps(EXECUTION.gap_report((cur or {}).get('evaluation') or {}), ensure_ascii=False)}\n"
                if project.get("code")
                else ""
            )
            + ("本项目试过：\n" + "\n".join(history[-6:]) + "\n" if history else "")
            + '\n请给出这一轮的执行方案。只输出 JSON：{"spec": {执行方案}, "name": "简短中文名", '
            '"note": "这一轮改了什么、为什么"}'
        )

    def _rule_proposal(self, pid: str, project: dict[str, Any], r: int, tried: set[str]) -> dict[str, Any] | None:
        cur = self.current(project)
        base = loads_spec(project["code"]) if project.get("code") else {}
        s = (cur or {}).get("metrics") or {}
        keep = {k: base[k] for k in ("horizon", "order", "fees") if k in base}
        plan: list[tuple[dict[str, Any], str]] = [
            ({"algo": "twap", "style": "neutral"}, "TWAP（挂单 + 片末补单）：先建立基准"),
            ({"algo": "adaptive"}, "自适应限价：默认被动挂单，落后或行情不利时转主动"),
            ({"algo": "vwap", "style": "neutral"}, "VWAP：按历史成交量分布切片"),
        ]
        if s and ((s.get("opportunity_bps") or 0) > 1 or (s.get("fill_rate") or 1) < 0.97):
            plan.insert(
                0,
                (
                    {**base, "finish": True, "adapt": {"behind": 0.05, "ahead": 0.08, "pause_illiquid": False}},
                    "完成率不够：到期补齐并更早转主动",
                ),
            )
        if s and (s.get("markout_5") or 0) < -3 and (s.get("maker_ratio") or 0) > 0.5:
            plan.insert(
                0,
                (
                    {**base, "algo": "adaptive", "adapt": {"behind": 0.04, "ahead": 0.04, "pause_illiquid": True}},
                    "被动成交被逆向选择：缩小允许落后的幅度，少挂单",
                ),
            )
        plan += [
            ({"algo": "twap", "style": "passive", "slices": 10}, "被动 TWAP：更多切片、只挂单，看价差能省多少"),
            ({"algo": "almgren_chriss", "urgency": 0.3}, "Almgren-Chriss（缓）：在冲击与风险之间折中"),
            ({"algo": "pov", "participation": 0.08}, "POV 8%：跟随市场成交量"),
            ({"algo": "iceberg", "display": 0.1, "reprice_bps": 4}, "冰山单：小显示量，价格跑开 4bp 追价"),
            ({"algo": "adaptive", "state_model": "hmm"}, "自适应 + HMM 状态：用隐马尔可夫模型识别高波动段"),
        ]
        for art, why in plan:
            try:
                art = self.normalize(project, {**keep, **art})
            except ValueError:
                continue
            if self.fingerprint(dumps_spec(art)) not in tried:
                self._post(
                    pid,
                    role="assistant",
                    kind="text",
                    mode="fallback",
                    content=f"**规则降级**（未调用大模型）：{why}。",
                )
                return {"artifact": art, "note": why, "source": "fallback"}
        self._post(
            pid,
            role="assistant",
            kind="text",
            mode="fallback",
            content="**规则降级**：预设方案都试过了。配置模型后 AI 可以针对成本分解调参数。",
        )
        return None

    # ---------------- 对话 ----------------
    def chat(self, pid: str, text: str) -> None:
        text = text.strip()
        head, _, rest = text.partition(" ")
        if head in ("/评估", "/仿真", "/eval", "/backtest"):
            self.evaluate_current(pid)
            return
        if head in ("/团队", "/team"):
            self._convene(
                pid, rest or "评审当前执行方案：成本分解是否合理、被动成交有没有逆向选择、不同市场状态下是否稳健"
            )
            return
        if head in ("/迭代", "/auto"):
            self.autopilot(pid)
            return
        if head in ("/启用", "/save", "/入库", "/上线"):
            self.save_to_library(pid, status="active")
            return
        if head in ("/解释", "/explain"):
            self._explain(pid)
            return
        if text.startswith("{"):
            self.submit(pid, text, note="研究者直接输入的执行方案", source="user")
            return
        try:
            self._chat_llm(pid, text)
        except LLMError as exc:
            self._chat_fallback(pid, text, str(exc))

    def _chat_fallback(self, pid: str, text: str, reason: str) -> None:
        project = self.store.get(pid) or {}
        if not project.get("code") or any(k in text for k in ("执行", "拆单", "试试", "优化", "算法")):
            prop = self._rule_proposal(pid, project, 1, self._tried(project))
            if prop:
                self.submit(pid, prop["artifact"], note=prop["note"], source="fallback")
                return
        self._post(
            pid,
            role="assistant",
            kind="text",
            mode="fallback",
            content=(
                f"**规则降级**（{_short(reason)}）：自由对话需要模型。现在可以直接用：\n\n"
                "- 粘贴一份 JSON 执行方案，平台会立即在盘口回放里仿真\n"
                "- 点「自动迭代」：TWAP → 自适应 → VWAP → 被动 TWAP → AC → POV → 冰山，每一版都和 TWAP、市价比\n"
                "- `/评估` 重新仿真　`/团队` 召集执行小组　`/启用` 存进执行方案库"
            ),
        )

    def _explain(self, pid: str) -> None:
        project = self.store.get(pid) or {}
        if not project.get("code"):
            self._post(
                pid, role="assistant", kind="text", content="还没有执行方案。描述你的执行问题，或点「自动迭代」。"
            )
            return
        cfg = EX.normalize_config(loads_spec(project["code"]))
        o = cfg["order"]
        parts = [
            f"**{EX.ALGOS[cfg['algo']]['label']}** · {EX.STYLES[cfg['style']]}",
            "",
            f"- 做法：{EX.ALGOS[cfg['algo']]['hint']}",
            f"- 执行期 {cfg['horizon']} 步，{cfg['slices']} 片；"
            f"单步主动成交不超过上一步成交量的 {cfg['max_participation']:.0%}",
            f"- 母单：{'买卖交替' if o['side'] == 'both' else ('买入' if o['side'] == 'buy' else '卖出')}，"
            + (f"名义 {o['notional']:,.0f} USDT" if o.get("notional") else f"约为执行期成交量的 {o['size_pct']:.0%}"),
            f"- 手续费：挂单 {cfg['fees']['maker_bps']:g} bp，吃单 {cfg['fees']['taker_bps']:g} bp；"
            f"冲击恢复 {cfg['impact']['resilience']:g} 步，永久部分 {cfg['impact']['permanent']:.0%}",
            f"- 市场状态：{'隐马尔可夫模型' if cfg['state_model'] == 'hmm' else '阈值规则'}",
        ]
        cur = self.current(project)
        if cur and cur.get("metrics"):
            parts.append(f"- 最近评估：{summary_text(cur['metrics'])}")
        self._post(pid, role="assistant", kind="text", content="\n".join(parts), mode="static")

    def _chat_tools(self, pid: str) -> dict[str, Tool]:
        obj = {"type": "object"}

        def inputs() -> Any:
            p = self.store.get(pid) or {}
            lib = self.deps.library()
            return self._tool(
                pid,
                "list_inputs",
                "查看可用输入",
                {},
                lambda: {
                    "symbols": p.get("securities"),
                    "data_source": p.get("data_source"),
                    "algos": {k: v["label"] for k, v in EX.ALGOS.items()},
                    "styles": EX.STYLES,
                    "recordings": [
                        {"id": r["id"], "symbol": r["symbol"], "snapshots": r["snapshots"], "status": r["status"]}
                        for r in lib.recordings()[:10]
                    ],
                    "cost_model": {k: lib.active_cost_model().get(k) for k in ("name", "a_bps", "Y")},
                },
            )

        def write(spec: dict | None = None, note: str = "", name: str = "") -> Any:
            return self.submit(pid, spec or {}, note=note or "模型提交", name=name)

        def evaluate() -> Any:
            return self.evaluate_current(pid)

        def versions() -> Any:
            p = self.store.get(pid) or {}
            return [
                {
                    "v": r["v"],
                    "spec": r["code"][:500],
                    "note": r["note"],
                    "is_bps": (r.get("metrics") or {}).get("is_bps"),
                    "beat_twap_bps": (r.get("metrics") or {}).get("beat_twap_bps"),
                    "passed": (r.get("evaluation") or {}).get("passed"),
                }
                for r in p.get("versions") or []
            ]

        def library(query: str = "") -> Any:
            lib = self.deps.library()
            return self._tool(
                pid,
                "search_execution_library",
                "查询执行方案库",
                {"关键词": query or "全部"},
                lambda: {"schemes": lib.search(query), "runs_so_far": lib.trial_count()},
            )

        def set_goal(**kw: Any) -> Any:
            out = self.set_goal(pid, kw, source="model")
            return {
                "ok": True,
                "goal": out["spec"],
                "check": {k: out["check"][k] for k in ("verdict", "issues", "summary")},
            }

        def check_goals() -> Any:
            p = self.store.get(pid) or {}
            spec = self.project_spec(p)
            ev = (self.current(p) or {}).get("evaluation")
            return {
                "goal": EXECUTION.items_text(spec["items"]),
                "latest": ev and {"passed": ev["passed"], "items": ev["items"], "gaps": EXECUTION.gap_report(ev)},
                "reasonableness": self.check_goal(p, spec),
            }

        def team(topic: str = "") -> Any:
            return self._convene(pid, topic or "评审当前执行方案")

        def ask(question: str = "", options: list[str] | None = None) -> Any:
            self._ask(pid, question or "这个执行方案要启用吗？", options or ["启用", "继续优化", "换个方向"])
            return {"ok": True, "note": "已向研究者提问，本轮结束"}

        spec_schema = {
            "type": "object",
            "properties": {
                "algo": {"type": "string", "enum": list(EX.ALGOS)},
                "style": {"type": "string", "enum": list(EX.STYLES)},
                "horizon": {"type": "integer"},
                "slices": {"type": "integer"},
                "participation": {"type": "number"},
                "max_participation": {"type": "number"},
                "urgency": {"type": "number"},
                "display": {"type": "number"},
                "reprice_bps": {"type": "number"},
                "finish": {"type": "boolean"},
                "state_model": {"type": "string", "enum": ["rules", "hmm"]},
                "adapt": {"type": "object"},
                "order": {"type": "object"},
                "fees": {"type": "object"},
                "impact": {"type": "object"},
            },
            "required": ["algo"],
        }
        return {
            **self._shared_tools(pid),
            "list_inputs": Tool(
                "list_inputs", "查看标的、算法菜单、盘口录制与当前成本模型", {**obj, "properties": {}}, inputs
            ),
            "write_execution": Tool(
                "write_execution",
                "提交执行方案：平台立即在盘口回放里仿真并做 TCA",
                {
                    **obj,
                    "properties": {"spec": spec_schema, "name": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["spec"],
                },
                write,
            ),
            "evaluate_execution": Tool("evaluate_execution", "按当前方案重新仿真", {**obj, "properties": {}}, evaluate),
            "search_execution_library": Tool(
                "search_execution_library",
                "查询执行方案库",
                {**obj, "properties": {"query": {"type": "string"}}},
                library,
            ),
            "list_versions": Tool("list_versions", "列出本项目历次方案及成绩", {**obj, "properties": {}}, versions),
            "set_execution_goal": Tool(
                "set_execution_goal",
                "设定或修改执行研究目标（只传要改的字段）。指标："
                + "、".join(f"{k}={v['label']}" for k, v in EXECUTION.metrics.items()),
                {
                    **obj,
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "metric": {"type": "string", "enum": list(EXECUTION.metrics)},
                                    "op": {"type": "string", "enum": [">=", "<="]},
                                    "target": {"type": "number"},
                                    "priority": {"type": "string", "enum": ["must", "try"]},
                                },
                                "required": ["metric", "target"],
                            },
                        },
                        "symbols": {"type": "array", "items": {"type": "string"}},
                        "max_rounds": {"type": "integer"},
                    },
                },
                set_goal,
            ),
            "check_goals": Tool(
                "check_goals", "查看当前目标、最近一次评估与差距", {**obj, "properties": {}}, check_goals
            ),
            "convene_team": Tool(
                "convene_team",
                "召集执行小组评审当前方案，返回小组结论",
                {**obj, "properties": {"topic": {"type": "string"}}},
                team,
            ),
            "ask_user": Tool(
                "ask_user",
                "向研究者提问并结束本轮",
                {
                    **obj,
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["question"],
                },
                ask,
            ),
        }


__all__ = ["EXECUTION_SYSTEM", "ExecutionDeps", "ExecutionLab", "new_execution_project", "summary_text"]
