"""因子研究室：在 AI 研究室里做因子研究（研究类型 = 因子）。

和策略研究共用同一套对话、工具卡片、上下文、后台回合与提问机制（继承 StrategyLab），区别在于：
- 产物是**因子表达式 + 假设卡**，每一版都在平台面板数据上评估（IC、衰减、分组、多空、样本外、入库检查）；
- 目标用因子指标目录（RankIC、ICIR、样本外保留率、库内相关……），见 research/goals.py；
- 每次评估都记入试验台账，显著性门槛随试验次数提高；
- 自动迭代：提出假设 → 实现表达式 → 评估 → 对照目标 → 带着报告改进；结束后问研究者是否入库。

没有模型密钥时走规则降级：从种子假设库按关键词挑起点，改进只做窗口、排名、平滑这类变换，界面标明「规则降级」。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..llm.base import LLMError, Message, Tool
from ..research import factor_expr as FX
from ..research.factor_eval import evaluate_factor
from ..research.factor_lib import CATEGORIES, FactorLibrary, expr_hash, mutate, pick_seeds
from ..research.goals import FACTOR
from ..research.panel import UNIVERSES, Panel, PanelError
from .strategy_lab import LabStore, LabTools, StopRequested, StrategyLab, _json_object, _lock, _now, _short

FACTOR_SYSTEM = """你是 QuantLab 平台的资深因子研究员，和研究者一起在加密货币市场里挖掘有信息量的因子。

## 因子表达式语法
在面板数据（K 线 × 标的）上逐元素计算，所有窗口参数必须是正整数常量，表达式不可能看到未来。
- 字段：open high low close volume amount vwap ret mkt_ret（行情）；funding oi ls_ratio taker_ratio basis（衍生品）；
  fng（恐惧贪婪，全市场）stablecoin tvl（链上，全市场）；x_<名字>（研究者导入的自定义数据）
- 时序：delay delta pct_change returns(d) ts_mean ts_sum ts_std ts_min ts_max ts_rank ts_zscore ts_skew
  ts_corr(x,y,d) ts_cov ts_beta ts_argmax ts_argmin decay_linear ema
- 截面：rank/cs_rank cs_zscore cs_demean scale winsorize(x,k)
- 数学：abs log sqrt sign signed_power(x,k) max min clip(x,lo,hi) where(c,a,b) fillna(x,k)，以及 + - * / ^ 比较与 && ||
示例：cs_rank(-ts_mean(funding, 9))、-ts_corr(close, volume, 20)、returns(30) - returns(3)

## 工作方式
1. 先提假设，再写公式：每个因子都要有假设卡（类别、逻辑、预期方向、什么结果会推翻它）。
2. 用 write_factor 提交（表达式 + 假设卡），平台会解析并在当前标的池上评估，返回 RankIC、ICIR、t 值、衰减、分组、
   多空夏普、换手、样本外保留率、与库内因子相关、入库检查。
3. 读报告找问题：方向反了就取负；衰减快就拉长窗口或做平滑；换手高就做 decay_linear；与库内高度相关就换思路。
4. 目标用 set_factor_goal / check_goals 管理；达标或需要研究者判断时用 ask_user。
5. 用 search_factor_library 查看已有因子与失败记录，避免重复。

## 铁律
- 简单优先：表达式节点越多越容易过拟合。显著性门槛会随试验次数提高，不要乱试。
- 不承诺收益；合成数据上的结论要明确说明不能作为结论。
- 回答用中文，简洁，先结论后细节。"""


@dataclass
class FactorDeps:
    """因子研究需要的平台能力（注入，便于测试）。"""

    panel: Callable[..., Panel]  # (symbols, timeframe, *, fields, start, end, bars_limit, source)
    library: Callable[[], FactorLibrary]


def new_factor_project(payload: dict[str, Any]) -> dict[str, Any]:
    idea = (payload.get("idea") or "").strip()
    title = (payload.get("title") or "").strip() or (re.sub(r"\s+", " ", idea)[:24] if idea else "未命名因子研究")
    universe = payload.get("universe") or "major10"
    symbols = [s.strip().upper() for s in payload.get("securities") or [] if s.strip()] or list(
        UNIVERSES.get(universe, UNIVERSES["major10"])["symbols"]
    )
    spec = FACTOR.normalize_spec(
        {"items": FACTOR.templates[0]["items"]},
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
        "kind": "factor",
        "title": title[:60],
        "idea": idea,
        "securities": symbols,
        "universe": universe,
        "timeframe": spec["timeframe"],
        "horizon": spec["horizon"],
        "model": payload.get("model") or "",
        "effort": payload.get("effort") or "medium",
        "executor": payload.get("executor") or "solo",  # solo 单个 AI | team 因子小组
        "team_id": payload.get("team_id") or "factor_team",
        "goals": {},
        "goal": {**spec, "updated_at": _now()},
        "start": payload.get("start"),
        "end": payload.get("end"),
        "max_rounds": spec["max_rounds"],
        "bars_limit": spec["bars_limit"],
        "data_source": payload.get("data_source") or "auto",
        "cost_bps": float(payload.get("cost_bps") or 10.0),
        "code": "",  # 当前表达式
        "hypothesis": {},
        "current_v": 0,
        "versions": [],
        "pins": [],
        "status": "idle",
        "job": None,
        "stop_requested": False,
        "factor_id": None,  # 入库后的因子 id
        "archived": False,
        "compacted_before": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }


def project_spec(project: dict[str, Any]) -> dict[str, Any]:
    goal = project.get("goal") or {}
    return FACTOR.normalize_spec(
        {},
        {
            "items": goal.get("items") or FACTOR.templates[0]["items"],
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


def summary_text(s: dict[str, Any] | None) -> str:
    if not s:
        return "尚未评估"

    def f(k: str, pct: bool = False) -> str:
        v = s.get(k)
        if v is None:
            return "—"
        return f"{v * 100:.1f}%" if pct else f"{v:.3f}"

    return (
        f"RankIC {f('rank_ic')}，ICIR {f('icir')}，t {f('ic_t')}（门槛 {s.get('t_required', '—')}），"
        f"多空夏普 {f('ls_sharpe')}，换手 {f('turnover', True)}，样本外保留 {f('oos_retention', True)}，"
        f"库内最大相关 {f('max_corr')}，"
        f"{'截面' if s.get('mode') == 'cross' else '时序'}口径，持有 {s.get('horizon')} 期"
    )


class FactorLab(StrategyLab):
    kind = "factor"
    system_prompt = FACTOR_SYSTEM
    system_title = "因子语法与研究铁律"
    artifact_label = "当前因子"
    metrics_label = "最近一次评估"

    def __init__(self, router: Any, store: LabStore, tools: LabTools, deps: FactorDeps) -> None:
        super().__init__(router, store, tools)
        self.deps = deps

    # ---------------- 目标 ----------------
    def goal_text(self, project: dict[str, Any]) -> str:
        return FACTOR.items_text(project_spec(project)["items"])

    def set_goal(self, pid: str, raw: dict[str, Any], *, source: str = "user", review: bool = False) -> dict[str, Any]:
        self.store.patch(pid, goal_set=True)
        with _lock(f"p:{pid}"):
            project = self.store.get(pid) or {}
            spec = FACTOR.normalize_spec(raw, project_spec(project))
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
            content=f"目标：{FACTOR.items_text(spec['items'])}",
            data={
                "stage": "set",
                "source": source,
                "spec": spec,
                "range": _range(spec),
                "check": check,
                "domain": "factor",
            },
        )
        return {"spec": spec, "check": check}

    def check_goal(self, project: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
        extra = []
        n = len(spec.get("symbols") or [])
        if n == 0:
            extra.append({"field": "symbols", "level": "error", "problem": "标的池为空", "suggest": None})
        elif n < 4:
            extra.append(
                {
                    "field": "symbols",
                    "level": "warn",
                    "suggest": None,
                    "problem": f"只有 {n} 个标的，只能做时序评估；截面因子建议至少 8 个",
                }
            )
        if spec.get("horizon", 1) > 20:
            extra.append(
                {"field": "horizon", "level": "warn", "suggest": 4, "problem": "持有期过长，有效样本会变得很少"}
            )
        history = [v.get("metrics") for v in project.get("versions") or [] if v.get("metrics")]
        return FACTOR.check(spec, history=history, extra=extra)

    # ---------------- 版本 ----------------
    def add_version(
        self,
        pid: str,
        code: str,
        *,
        source: str,
        note: str = "",
        checks: dict | None = None,
        model: str = "",
        hypothesis: dict[str, Any] | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        with _lock(f"p:{pid}"):
            project = self.store.get(pid) or {}
            versions = project.get("versions") or []
            v = (versions[-1]["v"] if versions else 0) + 1
            hyp = hypothesis or project.get("hypothesis") or {}
            row = {
                "v": v,
                "code": code,
                "source": source,
                "note": note[:300],
                "model": model,
                "name": name[:40],
                "hypothesis": hyp,
                "params": {},
                "checks_ok": True,
                "created_at": _now(),
                "metrics": None,
                "bt_id": None,
                "evaluation": None,
                "admitted": None,
            }
            versions.append(row)
            project.update(versions=versions, code=code, current_v=v, hypothesis=hyp)
            self.store.save(project)
            return row

    def submit_factor(
        self,
        pid: str,
        expression: str,
        *,
        hypothesis: dict[str, Any] | None = None,
        note: str = "",
        name: str = "",
        model: str = "",
        source: str = "ai",
    ) -> dict[str, Any]:
        """解析表达式 → 新版本 → 立即评估。返回给模型的简要结果。"""
        try:
            node = FX.parse(expression)
        except FX.FactorExprError as exc:
            self._post(
                pid,
                role="tool",
                kind="tool",
                data={
                    "name": "write_factor",
                    "label": "提交因子",
                    "args": {"表达式": expression[:60]},
                    "status": "error",
                    "error": str(exc),
                },
            )
            return {"ok": False, "error": str(exc), "hint": "修正表达式后重新提交"}
        expr = str(node)
        project = self.store.get(pid) or {}
        before = project.get("code") or ""
        hyp = _clean_hyp(hypothesis) or project.get("hypothesis") or {}
        row = self.add_version(pid, expr, source=source, note=note, model=model, hypothesis=hyp, name=name)
        self._post(
            pid,
            role="tool",
            kind="factor_code",
            content=note or "提交因子",
            data={
                "v": row["v"],
                "before": before,
                "after": expr,
                "hypothesis": hyp,
                "name": name,
                "complexity": FX.complexity(node),
                "fields": FX.fields_used(node),
            },
        )
        out = self.evaluate_current(pid)
        return out

    # ---------------- 评估 ----------------
    def evaluate_current(self, pid: str, *, cost_scale: float = 1.0, **_: Any) -> dict[str, Any]:
        """评估当前版本。cost_scale ≠ 1 是成本压力测试：只展示，不改版本成绩、不计入试验台账。"""
        stress = abs(cost_scale - 1.0) > 1e-9
        project = self.store.get(pid) or {}
        expr = project.get("code") or ""
        if not expr:
            return {"ok": False, "error": "当前还没有因子表达式"}
        v = project.get("current_v") or 0
        lib = self.deps.library()

        def run() -> dict[str, Any]:
            node = FX.parse(expr)
            panel = self.deps.panel(
                project["securities"],
                project["timeframe"],
                fields=FX.fields_used(node),
                start=project.get("start"),
                end=project.get("end"),
                bars_limit=project.get("bars_limit") or 1500,
                source=project.get("data_source") or "auto",
            )
            library = lib.library_values(panel, exclude=project.get("factor_id"))
            trials = lib.trial_count() + (0 if lib.store.get("research_trials", expr_hash(expr)) or stress else 1)
            return evaluate_factor(
                node,
                panel,
                horizon=int(project.get("horizon") or 1),
                cost_bps=float(project.get("cost_bps") or 10.0) * cost_scale,
                library=library,
                trials=max(1, trials),
            )

        try:
            report = self._tool(
                pid,
                "evaluate_factor",
                f"{cost_scale:g}× 成本评估" if stress else "评估因子",
                {
                    "版本": f"v{v}",
                    "标的": f"{len(project['securities'])} 个",
                    "周期": project["timeframe"],
                    "持有期": project.get("horizon") or 1,
                },
                run,
            )
        except (FX.FactorExprError, PanelError) as exc:
            self._post(pid, role="event", kind="error", content=f"评估没有完成：{exc}")
            return {"ok": False, "error": str(exc)}
        s = report["summary"]
        if stress:
            self._post(
                pid,
                role="tool",
                kind="factor_report",
                content=summary_text(s),
                data={
                    "v": v,
                    "expression": expr,
                    "summary": s,
                    "cost_scale": cost_scale,
                    "checks": report["checks"],
                    "admitted": report["admitted"],
                    "panel": report["panel"],
                },
            )
            return {
                "ok": True,
                "version": v,
                "stress": True,
                "summary": {k: s.get(k) for k in ("rank_ic", "ls_sharpe", "ls_return", "turnover")},
            }
        lib.record_trial(expr, s, source="lab", project_id=pid)
        evaluation = FACTOR.evaluate(s, project_spec(project)["items"])
        self._set_version_result(pid, v, metrics=s, evaluation=evaluation, admitted=report["admitted"])
        self.store.store.put_blob("factor_lab_reports", f"{pid}_{v}", report)
        self._post(
            pid,
            role="tool",
            kind="factor_report",
            content=summary_text(s),
            data={
                "v": v,
                "expression": expr,
                "summary": s,
                "cost_scale": 1,
                "checks": report["checks"],
                "admitted": report["admitted"],
                "evaluation": evaluation,
                "decay": report["decay"],
                "regimes": report["regimes"],
                "groups": {"n": report["groups"]["n"], "mean": report["groups"]["mean"]},
                "ls_nav": report["ls_nav"][::3],
                "ic_series": report["ic_series"][::3],
                "correlations": report["correlations"][:5],
                "panel": report["panel"],
            },
        )
        return {
            "ok": True,
            "version": v,
            "summary": {
                k: s.get(k)
                for k in (
                    "mode",
                    "horizon",
                    "rank_ic",
                    "icir",
                    "ic_t",
                    "t_required",
                    "ic_win",
                    "ls_sharpe",
                    "ls_return",
                    "turnover",
                    "coverage",
                    "max_corr",
                    "is_rank_ic",
                    "oos_rank_ic",
                    "oos_retention",
                    "sub_rank_ic",
                    "direction",
                )
            },
            "decay": report["decay"],
            "regimes": report["regimes"],
            "groups_mean_bps": report["groups"]["mean"],
            "checks": [{"label": c["label"], "ok": c["ok"], "detail": c["detail"]} for c in report["checks"]],
            "goal": {"passed": evaluation["passed"], "gaps": FACTOR.gap_report(evaluation)},
            "warnings": report["panel"].get("warnings", [])[:3],
        }

    def backtest_current(self, pid: str, *, cost_scale: float = 1.0, label: str = "") -> dict[str, Any]:
        """因子项目里的“回测”就是评估（cost_scale 放大成本，用作压力测试）。"""
        return self.evaluate_current(pid, cost_scale=cost_scale)

    def walk_forward(self, pid: str, *, segments: int = 3) -> dict[str, Any]:
        return {"ok": False, "error": "因子评估已经包含样本外与分状态检验，不需要分段回测"}

    def report(self, pid: str, v: int) -> dict[str, Any] | None:
        return self.store.store.get_blob("factor_lab_reports", f"{pid}_{v}")

    # ---------------- 对话 ----------------
    def chat(self, pid: str, text: str) -> None:
        text = text.strip()
        head, _, rest = text.partition(" ")
        if head in ("/评估", "/eval", "/回测", "/backtest"):
            self.evaluate_current(pid)
            return
        if head in ("/团队", "/team"):
            self._convene(pid, rest or "评审当前因子：假设是否成立、评估是否可信、有没有泄漏与过拟合、下一步怎么改")
            return
        if head in ("/迭代", "/auto"):
            self.autopilot(pid)
            return
        if head in ("/入库", "/save"):
            self.save_to_library(pid, status="active")
            return
        if head in ("/解释", "/explain"):
            self._explain(pid)
            return
        if _looks_like_expression(text):
            self.submit_factor(pid, text, note="研究者直接输入的表达式", source="user")
            return
        try:
            self._chat_llm(pid, text)
        except LLMError as exc:
            self._chat_fallback(pid, text, str(exc))

    def _chat_tools(self, pid: str) -> dict[str, Tool]:
        obj = {"type": "object"}
        hyp_schema = {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "rationale": {"type": "string"},
                "direction": {"type": "string"},
                "falsify": {"type": "string"},
            },
            "required": ["rationale"],
        }

        def write(expression: str = "", hypothesis: dict | None = None, note: str = "", name: str = "") -> Any:
            return self.submit_factor(pid, expression, hypothesis=hypothesis, note=note or "模型提交", name=name)

        def evaluate() -> Any:
            return self.evaluate_current(pid)

        def library(query: str = "") -> Any:
            lib = self.deps.library()
            return self._tool(
                pid,
                "search_factor_library",
                "查询因子库",
                {"关键词": query or "全部"},
                lambda: {
                    "factors": lib.search(query),
                    "failed_ideas": lib.failed_ideas(),
                    "trials_so_far": lib.trial_count(),
                },
            )

        def catalog() -> Any:
            return self._tool(
                pid,
                "list_fields",
                "查看字段与算子",
                {},
                lambda: {
                    "fields": {k: v["label"] for k, v in FX.FIELDS.items()},
                    "operators": {k: f"{v['sig']}：{v['doc']}" for k, v in FX.OPERATORS.items()},
                    "custom": sorted(
                        {
                            d.get("field")
                            for d in self.deps.library().store.list("datahub_datasets")
                            if str(d.get("field", "")).startswith("x_")
                        }
                    ),
                },
            )

        def versions() -> Any:
            p = self.store.get(pid) or {}
            return [
                {
                    "v": r["v"],
                    "expression": r["code"],
                    "note": r["note"],
                    "rank_ic": (r.get("metrics") or {}).get("rank_ic"),
                    "icir": (r.get("metrics") or {}).get("icir"),
                    "oos_retention": (r.get("metrics") or {}).get("oos_retention"),
                    "passed": (r.get("evaluation") or {}).get("passed"),
                }
                for r in p.get("versions") or []
            ]

        def set_goal(**kw: Any) -> Any:
            out = self.set_goal(pid, kw, source="model")
            return {
                "ok": True,
                "goal": out["spec"],
                "check": {k: out["check"][k] for k in ("verdict", "issues", "summary")},
            }

        def check_goals() -> Any:
            p = self.store.get(pid) or {}
            spec = project_spec(p)
            cur = next((r for r in p.get("versions") or [] if r["v"] == p.get("current_v")), None)
            ev = (cur or {}).get("evaluation")
            return {
                "goal": FACTOR.items_text(spec["items"]),
                "latest": ev and {"passed": ev["passed"], "items": ev["items"], "gaps": FACTOR.gap_report(ev)},
                "reasonableness": self.check_goal(p, spec),
            }

        def team(topic: str = "") -> Any:
            return self._convene(pid, topic or "评审当前因子")

        def ask(question: str = "", options: list[str] | None = None) -> Any:
            self._ask(pid, question or "这个因子要入库吗？", options or ["入库", "继续优化", "换个方向"])
            return {"ok": True, "note": "已向研究者提问，本轮结束"}

        return {
            **self._shared_tools(pid),
            "list_fields": Tool(
                "list_fields", "查看可用字段（含已导入的自定义数据）与全部算子", {**obj, "properties": {}}, catalog
            ),
            "write_factor": Tool(
                "write_factor",
                "提交一个因子：表达式 + 假设卡。平台解析后立即在标的池上评估并返回报告",
                {
                    **obj,
                    "properties": {
                        "expression": {"type": "string"},
                        "hypothesis": hyp_schema,
                        "name": {"type": "string", "description": "简短中文名"},
                        "note": {"type": "string", "description": "这一版改了什么"},
                    },
                    "required": ["expression", "hypothesis"],
                },
                write,
            ),
            "evaluate_factor": Tool(
                "evaluate_factor", "重新评估当前因子（数据或目标变了之后）", {**obj, "properties": {}}, evaluate
            ),
            "search_factor_library": Tool(
                "search_factor_library",
                "查询因子库里已有的因子、近期失败的想法与已试验次数",
                {**obj, "properties": {"query": {"type": "string"}}},
                library,
            ),
            "list_versions": Tool(
                "list_versions", "列出本项目历次因子版本及其评估", {**obj, "properties": {}}, versions
            ),
            "set_factor_goal": Tool(
                "set_factor_goal",
                "设定或修改因子研究目标（只传要改的字段）。指标："
                + "、".join(f"{k}={v['label']}" for k, v in FACTOR.metrics.items()),
                {
                    **obj,
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "metric": {"type": "string", "enum": list(FACTOR.metrics)},
                                    "op": {"type": "string", "enum": [">=", "<="]},
                                    "target": {"type": "number"},
                                    "priority": {"type": "string", "enum": ["must", "try"]},
                                },
                                "required": ["metric", "target"],
                            },
                        },
                        "symbols": {"type": "array", "items": {"type": "string"}},
                        "timeframe": {"type": "string"},
                        "horizon": {"type": "integer"},
                        "max_rounds": {"type": "integer"},
                    },
                },
                set_goal,
            ),
            "check_goals": Tool(
                "check_goals",
                "查看当前目标、最近一次评估与差距，以及目标是否合理",
                {**obj, "properties": {}},
                check_goals,
            ),
            "convene_team": Tool(
                "convene_team",
                "召集因子小组评审当前因子，返回小组结论",
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

    def _chat_fallback(self, pid: str, text: str, reason: str) -> None:
        project = self.store.get(pid) or {}
        if not project.get("code") or any(k in text for k in ("挖", "找", "因子", "假设", "试试")):
            seeds = pick_seeds(text or project.get("idea") or "", exclude=self._tried(project), k=1)
            if seeds:
                s = seeds[0]
                self._post(
                    pid,
                    role="assistant",
                    kind="text",
                    mode="fallback",
                    content=(
                        f"**规则降级**（{_short(reason)}）：从种子假设库里挑了「{s['name']}」作为起点。\n\n"
                        f"- 逻辑：{s['hypothesis']['rationale']}\n- 预期：{s['hypothesis']['direction']}\n"
                        f"- 证伪：{s['hypothesis']['falsify']}"
                    ),
                )
                self.submit_factor(
                    pid, s["expression"], hypothesis=s["hypothesis"], name=s["name"], note="种子假设", source="fallback"
                )
                return
        self._post(
            pid,
            role="assistant",
            kind="text",
            mode="fallback",
            content=(
                f"**规则降级**（{_short(reason)}）：自由对话需要模型。现在可以直接用：\n\n"
                "- 直接输入一个因子表达式，例如 `cs_rank(-ts_mean(funding, 9))`，平台会立即评估\n"
                "- 点「自动迭代」：按目标从种子假设出发，自动 评估 → 调窗口 / 排名 / 平滑\n"
                "- `/评估` 重新评估　`/团队` 召集因子小组　`/入库` 把当前因子存进因子库"
            ),
        )

    def _explain(self, pid: str) -> None:
        project = self.store.get(pid) or {}
        expr = project.get("code") or ""
        if not expr:
            self._post(pid, role="assistant", kind="text", content="还没有因子。描述一个想法，或直接输入表达式。")
            return
        node = FX.parse(expr)
        parts = [
            f"**{expr}**",
            "",
            f"- 用到的字段：{'、'.join(FX.FIELDS.get(f, {}).get('label', f) for f in FX.fields_used(node))}",
            f"- 复杂度：{FX.complexity(node)} 个节点，最长需要约 {FX.max_lookback(node)} 根 K 线的历史",
        ]
        hyp = project.get("hypothesis") or {}
        if hyp.get("rationale"):
            parts += [
                f"- 假设（{CATEGORIES.get(hyp.get('category'), '—')}）：{hyp['rationale']}",
                f"- 预期方向：{hyp.get('direction') or '—'}",
                f"- 证伪条件：{hyp.get('falsify') or '—'}",
            ]
        self._post(pid, role="assistant", kind="text", content="\n".join(parts), mode="static")

    def _convene(self, pid: str, topic: str, *, team_id: str | None = None) -> dict[str, Any]:
        if self.tools.team is None:
            return {"ok": False, "error": "团队功能不可用"}
        project = self.store.get(pid) or {}
        cur = next((r for r in project.get("versions") or [] if r["v"] == project.get("current_v")), None)
        lib = self.deps.library()
        context = (
            f"因子研究项目：{project.get('title')}\n想法：{project.get('idea') or '—'}\n"
            f"标的池：{','.join(project['securities'][:20])}，周期 {project['timeframe']}，"
            f"持有期 {project.get('horizon')}\n"
            f"目标：{self.goal_text(project)}\n当前因子：{project.get('code') or '（尚无）'}\n"
            f"假设：{json.dumps(project.get('hypothesis') or {}, ensure_ascii=False)}\n"
            f"最近评估：{summary_text((cur or {}).get('metrics'))}\n"
            f"已试验次数：{lib.trial_count()}；库内因子："
            f"{', '.join(f['name'] for f in lib.list('active')[:12]) or '无'}\n\n"
            f"因子表达式语法：字段 {', '.join(FX.FIELDS)}；算子 {', '.join(FX.OPERATORS)}。"
        )
        self._phase(pid, "因子小组讨论中")
        run = self._tool(
            pid,
            "convene_team",
            "召集因子小组",
            {"议题": topic[:40]},
            lambda: self.tools.team(topic, team_id=team_id or project.get("team_id") or "factor_team", context=context),
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

    def _context_block(self, project: dict[str, Any]) -> str:
        cur = next((r for r in project.get("versions") or [] if r["v"] == project.get("current_v")), None)
        lib = self.deps.library()
        parts = [
            f"【项目】{project.get('title')}　标的池 {len(project['securities'])} 个"
            f"（{','.join(project['securities'][:12])}）"
            f"　周期 {project['timeframe']}　持有期 {project.get('horizon')} 期　成本 {project.get('cost_bps')} bps",
            f"【想法】{project.get('idea') or '—'}",
            f"【目标】{self.goal_text(project)}",
            f"【因子库】已试验 {lib.trial_count()} 次；入库因子："
            + ("、".join(f"{f['name']}={f['expression']}" for f in lib.list("active")[:10]) or "无"),
        ]
        for p in project.get("pins") or []:
            parts.append(f"【{p['title']}】\n{p['text'][:2000]}")
        if project.get("code"):
            parts.append(
                f"【当前因子 v{project.get('current_v')}】{project['code']}\n"
                f"假设：{json.dumps(project.get('hypothesis') or {}, ensure_ascii=False)}"
            )
        parts.append(f"【最近评估】{summary_text((cur or {}).get('metrics'))}")
        return "\n\n".join(parts)

    def _tried(self, project: dict[str, Any]) -> set[str]:
        return {expr_hash(r["code"]) for r in project.get("versions") or []}

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
        spec = project_spec(project)
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
                    "range": _range(spec),
                    "check": check,
                    "domain": "factor",
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
                "goals_text": FACTOR.items_text(spec["items"]),
                "rounds": max_rounds,
                "range": _range(spec),
                "research": research,
                "domain": "factor",
            },
        )
        notes = ""
        if project.get("executor") == "team" or research == "team":
            self._check_stop(pid)
            out = self._convene(
                pid,
                f"为这个研究方向提出 2~3 个有经济学逻辑的因子假设并给出表达式："
                f"{project.get('idea') or project.get('title')}",
            )
            notes = out.get("synthesis", "")[:2500]
        history: list[str] = []
        scores: list[float] = []
        for r in range(1, max_rounds + 1):
            self._check_stop(pid)
            project = self.store.get(pid) or {}
            cur = next((x for x in project.get("versions") or [] if x["v"] == project.get("current_v")), None)
            if cur is not None and (cur.get("evaluation") or {}).get("passed") and r == 1:
                self._post(pid, role="event", kind="text", content="当前因子已经达标。")
                break
            self._phase(pid, f"第 {r} 轮 · {'提出假设' if not project.get('code') else '改进因子'}")
            proposal = self._propose(pid, project, r, history, notes)
            if proposal is None:
                break
            out = self.submit_factor(
                pid,
                proposal["expression"],
                hypothesis=proposal.get("hypothesis"),
                note=proposal.get("note") or f"第 {r} 轮",
                name=proposal.get("name", ""),
                model=proposal.get("model", ""),
                source=proposal.get("source", "ai"),
            )
            if not out.get("ok"):
                history.append(f"- 第 {r} 轮：{proposal['expression']} 无法评估（{out.get('error', '')[:60]}）")
                continue
            ev = (self.store.get(pid) or {}).get("versions", [{}])[-1].get("evaluation") or {}
            s = out["summary"]
            history.append(
                f"- 第 {r} 轮：{proposal['expression']} → RankIC {s.get('rank_ic') or 0:.3f}，"
                f"ICIR {s.get('icir') or 0:.2f}，样本外保留 {s.get('oos_retention') or 0:.0%}"
            )
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
                    "metrics": s,
                    "domain": "factor",
                },
            )
            if ev.get("passed"):
                break
            scores.append(ev.get("score") or 0)
            if (
                r >= 3
                and len(scores) >= 3
                and max(scores[-2:]) <= scores[-3] + 0.05
                and all(
                    abs(x["actual"]) < abs(x["target"]) * 0.3 for x in FACTOR.gap_report(ev) if x["priority"] == "must"
                )
                and FACTOR.gap_report(ev)
            ):
                self._post(
                    pid,
                    role="event",
                    kind="goal",
                    content="提前结束迭代",
                    data={
                        "stage": "early_stop",
                        "domain": "factor",
                        "reason": "连续几轮没有进步且离目标很远，这个方向可能没有信息量，继续试只会增加假阳性。",
                        "gaps": FACTOR.gap_report(ev),
                    },
                )
                break
        self._finish(pid)

    def _propose(
        self, pid: str, project: dict[str, Any], r: int, history: list[str], notes: str
    ) -> dict[str, Any] | None:
        tried = self._tried(project)
        cur = next((x for x in project.get("versions") or [] if x["v"] == project.get("current_v")), None)
        lib = self.deps.library()
        prompt = (
            f"研究方向：{project.get('idea') or project.get('title')}\n目标：{self.goal_text(project)}\n"
            f"标的池 {len(project['securities'])} 个，周期 {project['timeframe']}，"
            f"持有期 {project.get('horizon')} 期。\n"
            + (f"小组建议：\n{notes}\n" if notes else "")
            + (
                f"当前因子：{project.get('code')}\n最近评估：{summary_text((cur or {}).get('metrics'))}\n"
                f"未达标：{json.dumps(FACTOR.gap_report((cur or {}).get('evaluation') or {}), ensure_ascii=False)}\n"
                if project.get("code")
                else ""
            )
            + ("本项目试过：\n" + "\n".join(history[-6:]) + "\n" if history else "")
            + f"近期失败的想法（不要再试）：{json.dumps(lib.failed_ideas(8), ensure_ascii=False)}\n"
            + f"库内因子：{json.dumps(lib.search()[:8], ensure_ascii=False)}\n\n"
            '请给出这一轮要试的一个因子。只输出 JSON：{"name": "简短中文名", "expression": "...", '
            '"hypothesis": {"category": "risk_premium|behavioral|microstructure|structural|other", '
            '"rationale": "...", "direction": "...", "falsify": "..."}, "note": "这一轮改了什么、'
            '为什么"}'
        )
        try:
            result = self._tool(
                pid,
                "propose_factor",
                f"第 {r} 轮：{'提出假设' if not project.get('code') else '改进因子'}",
                {"已试": len(tried)},
                lambda: self._router_for(project).chat(
                    [Message(role="system", content=FACTOR_SYSTEM), Message(role="user", content=prompt)],
                    role="reasoning",
                    temperature=0.5,
                    max_tokens=2000,
                ),
            )
            data = _json_object(result.content or "")
            if data and data.get("expression"):
                FX.parse(str(data["expression"]))
                if expr_hash(str(data["expression"])) not in tried:
                    if data.get("note"):
                        self._post(
                            pid,
                            role="assistant",
                            kind="text",
                            mode="llm",
                            model=f"{result.provider}/{result.model}",
                            content=str(data["note"])[:1500],
                        )
                    return {**data, "model": f"{result.provider}/{result.model}", "source": "ai"}
        except StopRequested:
            raise
        except (LLMError, FX.FactorExprError, Exception):
            pass
        return self._rule_proposal(pid, project, r, tried)

    def _rule_proposal(self, pid: str, project: dict[str, Any], r: int, tried: set[str]) -> dict[str, Any] | None:
        cur = next((x for x in project.get("versions") or [] if x["v"] == project.get("current_v")), None)
        s = (cur or {}).get("metrics") or {}
        if project.get("code") and s:
            expr = project["code"]
            if (s.get("rank_ic") or 0) < 0 and (s.get("ic_t") or 0) < -1:
                cand = expr[1:] if expr.startswith("-") and "(" in expr[:2] else f"-({expr})"
                if expr_hash(cand) not in tried:
                    self._post(
                        pid,
                        role="assistant",
                        kind="text",
                        mode="fallback",
                        content="**规则降级改进**：RankIC 显著为负，把因子方向取反。",
                    )
                    return {"expression": cand, "note": "方向取反", "source": "fallback"}
            for cand, why in mutate(expr, r):
                if expr_hash(cand) not in tried:
                    try:
                        FX.parse(cand)
                    except FX.FactorExprError:
                        continue
                    self._post(
                        pid,
                        role="assistant",
                        kind="text",
                        mode="fallback",
                        content=f"**规则降级改进**（未调用大模型）：{why}。",
                    )
                    return {"expression": cand, "note": why, "source": "fallback"}
        seeds = pick_seeds(project.get("idea") or project.get("title") or "", exclude=tried, k=1)
        if not seeds:
            self._post(
                pid,
                role="assistant",
                kind="text",
                mode="fallback",
                content="**规则降级**：种子假设与规则变换都试过了。配置模型后 AI 可以提出新的假设。",
            )
            return None
        seed = seeds[0]
        self._post(
            pid,
            role="assistant",
            kind="text",
            mode="fallback",
            content=(
                f"**规则降级**：从种子假设库挑了「{seed['name']}」。\n\n- 逻辑：{seed['hypothesis']['rationale']}\n"
                f"- 证伪：{seed['hypothesis']['falsify']}"
            ),
        )
        return {
            "expression": seed["expression"],
            "hypothesis": seed["hypothesis"],
            "name": seed["name"],
            "note": "种子假设",
            "source": "fallback",
        }

    def _finish(self, pid: str) -> None:
        project = self.store.get(pid) or {}
        scored = [r for r in project.get("versions") or [] if r.get("evaluation")]
        if not scored:
            return
        best = max(scored, key=lambda r: (r["evaluation"]["score"], bool(r.get("admitted"))))
        if best["v"] != project.get("current_v"):
            self.restore(pid, best["v"], note="自动迭代：回到评估最好的一版")
        self._check_stop(pid)
        self._phase(pid, "成本压力测试")
        stress = self.backtest_current(pid, cost_scale=2.0)
        warn = []
        s = best.get("metrics") or {}
        if stress.get("ok"):
            ss = stress.get("summary") or {}
            if (s.get("ls_sharpe") or 0) > 0 and (ss.get("ls_sharpe") or 0) < 0:
                warn.append("2 倍成本下多空收益转负：换手太高")
        if (s.get("oos_retention") or 0) < 0.5:
            warn.append("样本外明显变弱")
        rep = self.report(pid, best["v"]) or {}
        if (rep.get("panel") or {}).get("synthetic"):
            warn.append("用了合成数据，结论不能作为研究结论")
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
                "metrics": s,
                "admitted": best.get("admitted"),
                "warnings": warn,
                "domain": "factor",
                "stress": (stress.get("summary") if stress.get("ok") else None),
            },
        )
        verdict = "已达成全部目标" if passed else "还没有全部达标"
        adm = "，入库检查全部通过" if best.get("admitted") else "，入库检查有未通过项"
        self._ask(
            pid,
            f"迭代结束：v{best['v']} {verdict}{adm}。{'；'.join(warn) + '。' if warn else ''}要把它存进因子库吗？",
            ["入库", "存为候选", "继续优化", "换个方向"],
            decision=True,
        )

    # ---------------- 用户决定 ----------------
    def decide(self, pid: str, choice: str, note: str = "") -> None:
        if self._decide_permission(pid, choice, note):
            return
        if choice in ("入库", "存为候选"):
            for m in self.store.messages(pid):
                if m["kind"] == "ask" and m["data"].get("answered") is None:
                    self.store.update(pid, m["seq"], data={"answered": choice})
            self._answered(pid)
            self._post(pid, role="user", kind="text", content=choice + (f"：{note}" if note else ""))
            self.save_to_library(pid, status="active" if choice == "入库" else "candidate")
            return
        if choice.startswith("按建议修改") or choice == "仍然开始":
            for m in self.store.messages(pid):
                if m["kind"] == "ask" and m["data"].get("answered") is None:
                    self.store.update(pid, m["seq"], data={"answered": choice})
            self._answered(pid)
            self._post(pid, role="user", kind="text", content=choice)
            if choice != "仍然开始":
                project = self.store.get(pid) or {}
                spec = project_spec(project)
                self.set_goal(pid, FACTOR.apply_suggestions(spec, self.check_goal(project, spec)), source="suggest")
            self.autopilot(pid, force=True)
            return
        super().decide(pid, choice, note)

    def save_to_library(self, pid: str, *, status: str = "active") -> dict[str, Any] | None:
        project = self.store.get(pid) or {}
        if not project.get("code"):
            self._post(pid, role="event", kind="error", content="还没有因子可以入库")
            return None
        cur = next((r for r in project.get("versions") or [] if r["v"] == project.get("current_v")), {})
        report = self.report(pid, project.get("current_v") or 0)
        if status == "active" and report and not report.get("admitted"):
            failed = [c["label"] for c in report.get("checks") or [] if c["required"] and not c["ok"]]
            status = "candidate"
            self._post(
                pid,
                role="event",
                kind="text",
                content=f"入库检查没有全部通过（{'、'.join(failed)}），先存为候选。可以在因子库里手动改状态。",
            )
        lib = self.deps.library()
        rec = self._tool(
            pid,
            "save_factor",
            "存入因子库",
            {"状态": "入库" if status == "active" else "候选"},
            lambda: lib.save(
                {
                    "id": project.get("factor_id"),
                    "name": cur.get("name") or project.get("title"),
                    "expression": project["code"],
                    "hypothesis": project.get("hypothesis") or {},
                    "status": status,
                    "universe": project["securities"],
                    "timeframe": project["timeframe"],
                    "horizon": project.get("horizon") or 1,
                    "created_by": "ai" if cur.get("source") != "user" else "user",
                    "project_id": pid,
                    "note": project.get("idea") or "",
                },
                report=report,
            ),
        )
        self.store.patch(pid, factor_id=rec["id"])
        self._post(
            pid,
            role="event",
            kind="saved",
            content=f"已存入因子库：{rec['name']}",
            data={"factor_id": rec["id"], "name": rec["name"], "status": rec["status"], "kind": "factor"},
        )
        return rec

    def restore(self, pid: str, v: int, *, note: str = "") -> dict[str, Any] | None:
        project = self.store.get(pid) or {}
        row = next((r for r in project.get("versions") or [] if r["v"] == v), None)
        if row is None:
            return None
        self.store.patch(pid, code=row["code"], current_v=v, hypothesis=row.get("hypothesis") or {})
        self._post(pid, role="event", kind="text", content=note or f"已切换到 v{v}")
        return row


def _clean_hyp(h: Any) -> dict[str, Any]:
    if not isinstance(h, dict):
        return {}
    out = {k: str(h.get(k) or "")[:400] for k in ("category", "rationale", "direction", "falsify") if h.get(k)}
    if out.get("category") not in CATEGORIES:
        out["category"] = "other" if out else ""
    return {k: v for k, v in out.items() if v}


def _looks_like_expression(text: str) -> bool:
    t = text.strip()
    if not t or len(t) > 600 or re.search(r"[一-鿿]", t) or t.startswith("/"):
        return False
    if not re.search(r"[(*/+-]", t):
        return False
    try:
        FX.parse(t)
        return True
    except FX.FactorExprError:
        return False


def _range(spec: dict[str, Any]) -> str:
    from datetime import datetime, timezone

    if spec.get("start"):

        def day(ms: int | None) -> str:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ms else "现在"

        return f"{day(spec['start'])} 至 {day(spec.get('end'))}"
    return f"最近 {spec.get('bars_limit') or 1500} 根 K 线"


__all__ = ["FACTOR_SYSTEM", "FactorDeps", "FactorLab", "new_factor_project", "project_spec", "summary_text"]
_ = time
