"""模型研究室（研究类型 = 模型）：用因子做特征，训练能在样本外稳定预测收益、方向或波动率的模型。

产物是一份**训练规格**（JSON）：特征、标签、持有期、模型与超参数。每一版都走同一条流程：
构建数据集 → 带 purge 的滚动训练 → 样本外评估（对照基线）→ 泄漏检查 → 记入实验台账。
达标后问研究者是否上线（存入模型库）。没有模型密钥时走规则降级：岭回归 → 梯度提升树 → MLP → 按重要性精简特征。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..llm.base import LLMError, Tool
from ..research import learners as L
from ..research import models as M
from ..research.factor_lib import FactorLibrary
from ..research.goals import MODEL
from ..research.model_lib import ModelLibrary
from ..research.panel import UNIVERSES, Panel, PanelError
from .research_lab import DomainLab, base_project, dumps_spec, loads_spec
from .strategy_lab import StopRequested, _short

MODEL_SYSTEM = """你是 QuantLab 平台的机器学习工程师，和研究者一起训练预测加密市场的模型。

## 训练规格（write_model_spec 的参数）
{"model": "ridge|gbdt|mlp|lstm|gru|tcn|transformer|har|garch",
 "label": "fwd_ret|cs_rank|direction|triple_barrier|volatility", "horizon": 1,
 "features": [{"factor_id": "因子库里的 id"} 或 {"name": "中文名", "expression": "因子表达式"}],
 "params": {模型超参数，只传要改的}, "folds": 4, "seed": 7}
- 特征就是因子表达式（语法同因子研究：ts_mean、cs_rank、returns(d)……），或引用因子库里的因子。
- 标签：fwd_ret 未来收益；cs_rank 截面收益排名；direction 涨跌（分类）；triple_barrier 三重障碍（分类）；volatility 未来波动率。
- har / garch 只能配 volatility 标签；lstm / gru / tcn / transformer 需要服务器装了 PyTorch。

## 平台怎么评估
扩张窗口滚动训练（默认 4 段，每段训练集在测试段前 purge 持有期），所有指标都在拼接起来的样本外预测上算：
样本外 RankIC、ICIR、方向准确率、Brier（分类）、多空夏普、换手、各段为正比例、PSI 漂移；
同时训练基线（线性模型），“超出基线”≤ 0 就说明复杂模型没有增量。
泄漏检查：截断数据重算特征必须一致；打乱标签重训后必须学不到东西。

## 工作方式
1. 先用 list_features 看因子库与可用字段，选 3~10 个有不同逻辑来源的特征。
2. 先跑线性基线，再尝试更复杂的模型；复杂模型跑不赢基线就不采用。
3. 读报告：看各段是否一致、重要性是否合理、学习曲线是否过拟合（训练损失下降但验证损失上升）。
4. 目标用 set_model_goal / check_goals 管理；达标或需要研究者判断时用 ask_user。

## 铁律
- 训练次数也计入多重检验；不要无目的地调超参数。
- 不承诺收益；合成数据上的结论要明确说明不能作为结论。
- 回答用中文，简洁，先结论后细节。"""  # noqa: E501 提示词原文，不折行


@dataclass
class ModelDeps:
    panel: Callable[..., Panel]  # (symbols, timeframe, *, fields, start, end, bars_limit, source)
    library: Callable[[], ModelLibrary]
    factors: Callable[[], FactorLibrary]


def new_model_project(payload: dict[str, Any]) -> dict[str, Any]:
    universe = payload.get("universe") or "major10"
    symbols = [s.strip().upper() for s in payload.get("securities") or [] if s.strip()] or list(
        UNIVERSES.get(universe, UNIVERSES["major10"])["symbols"]
    )
    p = base_project("model", payload, MODEL, symbols=symbols, team_id="model_team", title_default="未命名模型研究")
    p["model_id"] = None
    return p


def summary_text(s: dict[str, Any] | None) -> str:
    if not s:
        return "尚未训练"

    def f(k: str, pct: bool = False) -> str:
        v = s.get(k)
        if v is None:
            return "—"
        return f"{v * 100:.1f}%" if pct else f"{v:.3f}"

    head = (
        f"{L.LEARNERS.get(s.get('model') or '', {}).get('label', s.get('model'))} "
        f"· {M.LABELS.get(s.get('label') or '', {}).get('label', '')}"
    )
    if s.get("mode") == "volatility":
        return f"{head}：预测与实际波动相关 {f('oos_rank_ic')}，超出基线 {f('beat_baseline')}，QLIKE {f('qlike')}"
    return (
        f"{head}：样本外 RankIC {f('oos_rank_ic')}（基线 {f('baseline_rank_ic')}，超出 {f('beat_baseline')}），"
        f"方向准确率 {f('dir_acc', True)}，多空夏普 {f('ls_sharpe')}，换手 {f('turnover', True)}，"
        f"各段为正 {f('fold_positive', True)}，泄漏检查{'通过' if s.get('leak_passed') else '未通过'}"
    )


class ModelLab(DomainLab):
    kind = "model"
    domain = MODEL
    team_default = "model_team"
    noun = "训练规格"
    save_choices = ("上线", "存为候选")
    system_prompt = MODEL_SYSTEM
    system_title = "训练规格与评估口径"
    artifact_label = "当前训练规格"
    metrics_label = "最近一次训练"
    min_symbols = 1

    created_hint = "描述你想预测什么（例如“用资金费率和动量预测 4 小时后的截面收益”），或点「自动迭代」从线性基线开始。"

    def __init__(self, router: Any, store: Any, tools: Any, deps: ModelDeps) -> None:
        super().__init__(router, store, tools)
        self.deps = deps

    def new_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        return new_model_project(payload)

    def summary_text(self, s: dict[str, Any] | None) -> str:
        return summary_text(s)

    def extra_checks(self, project: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
        out = super().extra_checks(project, spec)
        n = len(spec.get("symbols") or [])
        if 0 < n < 4:
            out.append(
                {
                    "field": "symbols",
                    "level": "warn",
                    "suggest": None,
                    "problem": f"只有 {n} 个标的，样本主要来自时间维度，模型容易过拟合；建议至少 8 个",
                }
            )
        return out

    # ---------------- 产物 ----------------
    def _resolve(self, ref: str) -> dict[str, Any] | None:
        lib = self.deps.factors()
        return lib.get(ref) or lib.by_name(ref)

    def normalize(self, project: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        merged = {"horizon": project.get("horizon") or 1, **raw}
        spec = M.normalize_spec(merged, resolve=self._resolve)
        return {k: spec[k] for k in ("model", "label", "horizon", "features", "params", "folds", "seed", "barrier_k")}

    def full_spec(self, project: dict[str, Any], art: dict[str, Any]) -> dict[str, Any]:
        return M.normalize_spec(
            {
                **art,
                "symbols": project["securities"],
                "timeframe": project["timeframe"],
                "bars_limit": project.get("bars_limit") or 1500,
                "start": project.get("start"),
                "end": project.get("end"),
                "source": project.get("data_source") or "auto",
                "cost_bps": project.get("cost_bps") or 10.0,
            },
            resolve=self._resolve,
        )

    def submit(
        self, pid: str, artifact: Any, *, note: str = "", name: str = "", model: str = "", source: str = "ai"
    ) -> dict[str, Any]:
        project = self.store.get(pid) or {}
        try:
            raw = loads_spec(artifact) if isinstance(artifact, str) else dict(artifact or {})
            art = self.normalize(project, raw)
            L.make_learner(art["model"], art["params"], M.LABELS[art["label"]]["task"])
        except (ValueError, M.ModelError, L.LearnerError, json.JSONDecodeError) as exc:
            self._post(
                pid,
                role="tool",
                kind="tool",
                data={
                    "name": "write_model_spec",
                    "label": "提交训练规格",
                    "args": {},
                    "status": "error",
                    "error": str(exc),
                },
            )
            return {"ok": False, "error": str(exc), "hint": "修正规格后重新提交"}
        before = project.get("code") or ""
        code = dumps_spec(art)
        if art.get("horizon") != project.get("horizon"):
            self.store.patch(pid, horizon=art["horizon"])
        row = self.add_version(pid, code, source=source, note=note, model=model, name=name)
        self._post(
            pid,
            role="tool",
            kind="model_spec",
            content=note or "提交训练规格",
            data={
                "v": row["v"],
                "before": before,
                "after": code,
                "spec": art,
                "model_label": L.LEARNERS[art["model"]]["label"],
                "label_label": M.LABELS[art["label"]]["label"],
            },
        )
        return self.evaluate_current(pid)

    def evaluate_current(self, pid: str, **_: Any) -> dict[str, Any]:
        project = self.store.get(pid) or {}
        if not project.get("code"):
            return {"ok": False, "error": "当前还没有训练规格"}
        v = project.get("current_v") or 0
        lib = self.deps.library()
        try:
            art = loads_spec(project["code"])
            spec = self.full_spec(project, art)
        except (ValueError, M.ModelError) as exc:
            self._post(pid, role="event", kind="error", content=f"规格有误：{exc}")
            return {"ok": False, "error": str(exc)}

        def stop() -> None:
            self._check_stop(pid)

        def run() -> dict[str, Any]:
            panel = self.deps.panel(
                spec["symbols"],
                spec["timeframe"],
                fields=M.fields_for(spec),
                start=spec.get("start"),
                end=spec.get("end"),
                bars_limit=spec["bars_limit"],
                source=spec["source"],
            )
            return M.train_and_evaluate(
                spec, panel, stop=stop, progress=lambda t: self._phase(pid, t), trials=lib.trial_count() + 1
            )

        try:
            out = self._tool(
                pid,
                "train_model",
                "训练模型",
                {
                    "版本": f"v{v}",
                    "模型": L.LEARNERS[spec["model"]]["label"],
                    "标签": M.LABELS[spec["label"]]["label"],
                    "特征": f"{len(spec['features'])} 个",
                    "标的": f"{len(spec['symbols'])} 个",
                },
                run,
            )
        except StopRequested:
            raise
        except (M.ModelError, L.LearnerError, PanelError) as exc:
            self._post(pid, role="event", kind="error", content=f"训练没有完成：{exc}")
            return {"ok": False, "error": str(exc)}
        report = out["report"]
        s = report["summary"]
        run_rec = lib.record_run(
            spec,
            report,
            out["state"],
            source="lab",
            project_id=pid,
            note=((self.current(project) or {}).get("note") or ""),
        )
        # 没设目标时不拿出厂默认阈值评判，只汇报实测结果
        evaluation = MODEL.evaluate(s, self.project_spec(project)["items"]) if self.has_goal(project) else None
        self._set_version_result(
            pid, v, metrics=s, evaluation=evaluation, admitted=report["admitted"], run_id=run_rec["id"]
        )
        self._post(
            pid,
            role="tool",
            kind="model_report",
            content=summary_text(s),
            data={
                "v": v,
                "run_id": run_rec["id"],
                "summary": s,
                "checks": report["checks"],
                "admitted": report["admitted"],
                "evaluation": evaluation,
                "folds": report["folds"],
                "curve": report["curve"][:: max(1, len(report["curve"]) // 60)],
                "importance": report["importance"][:8],
                "calibration": report.get("calibration"),
                "regimes": report.get("regimes"),
                "baseline": report["baseline"],
                "panel": report["panel"],
                "ic_series": (report.get("ic_series") or [])[::3],
            },
        )
        return {
            "ok": True,
            "version": v,
            "run_id": run_rec["id"],
            "summary": {
                k: s.get(k)
                for k in (
                    "model",
                    "label",
                    "oos_rank_ic",
                    "baseline_rank_ic",
                    "beat_baseline",
                    "oos_icir",
                    "dir_acc",
                    "brier",
                    "ls_sharpe",
                    "turnover",
                    "fold_positive",
                    "psi_max",
                    "leak_passed",
                    "shuffle_rank_ic",
                    "samples",
                )
            },
            "folds": [{"fold": f["fold"], "rank_ic": f["rank_ic"]} for f in report["folds"]],
            "importance": report["importance"][:8],
            "checks": [{"label": c["label"], "ok": c["ok"], "detail": c["detail"]} for c in report["checks"]],
            "goal": (
                {"passed": evaluation["passed"], "gaps": MODEL.gap_report(evaluation)}
                if evaluation
                else "未设定验收目标：只汇报实测结果，不要说达标或未达标"
            ),
            "curve_tail": report["curve"][-3:],
            "warnings": report["panel"].get("warnings", [])[:3],
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
            self._post(pid, role="event", kind="error", content="当前版本还没有训练，先训练再存入模型库")
            return None
        if status == "active" and not cur.get("admitted"):
            failed = [
                c["label"]
                for c in (self.report(pid, cur["v"]) or {}).get("checks") or []
                if c["required"] and not c["ok"]
            ]
            status = "candidate"
            self._post(
                pid,
                role="event",
                kind="text",
                content=f"检查没有全部通过（{'、'.join(failed)}），先存为候选。可以在模型库里手动改状态。",
            )
        lib = self.deps.library()
        rec = self._tool(
            pid,
            "save_model",
            "存入模型库",
            {"状态": "上线" if status == "active" else "候选"},
            lambda: lib.save_from_run(
                cur["run_id"],
                name=cur.get("name") or project.get("title") or "",
                status=status,
                note=project.get("idea") or "",
                model_id=project.get("model_id"),
                project_id=pid,
                created_by="ai" if cur.get("source") != "user" else "user",
            ),
        )
        self.store.patch(pid, model_id=rec["id"], library_id=rec["id"])
        self._post(
            pid,
            role="event",
            kind="saved",
            content=f"已存入模型库：{rec['name']}",
            data={"model_id": rec["id"], "name": rec["name"], "status": rec["status"], "kind": "model"},
        )
        return rec

    def finish_warnings(self, project: dict[str, Any], best: dict[str, Any]) -> list[str]:
        s = best.get("metrics") or {}
        warn = []
        if (s.get("beat_baseline") or 0) <= 0:
            warn.append("没有跑赢基线")
        if not s.get("leak_passed"):
            warn.append("泄漏检查没有通过")
        rep = self.report(project["id"], best["v"]) or {}
        if (rep.get("panel") or {}).get("synthetic"):
            warn.append("用了合成数据，结论不能作为研究结论")
        return warn

    # ---------------- 自动迭代 ----------------
    def proposal_prompt(self, project: dict[str, Any], r: int, history: list[str], notes: str) -> str:
        cur = self.current(project)
        fl = self.deps.factors()
        return (
            f"研究方向：{project.get('idea') or project.get('title')}\n目标：{self.goal_text(project)}\n"
            f"标的 {len(project['securities'])} 个，周期 {project['timeframe']}，持有期 {project.get('horizon')} 期。\n"
            f"可用后端：{json.dumps(L.backends())}\n"
            + (f"小组建议：\n{notes}\n" if notes else "")
            + (
                f"当前规格：{project.get('code')}\n最近训练：{summary_text((cur or {}).get('metrics'))}\n"
                f"未达标：{json.dumps(MODEL.gap_report((cur or {}).get('evaluation') or {}), ensure_ascii=False)}\n"
                if project.get("code")
                else ""
            )
            + ("本项目试过：\n" + "\n".join(history[-6:]) + "\n" if history else "")
            + f"因子库：{json.dumps(fl.search()[:12], ensure_ascii=False)}\n\n"
            '请给出这一轮的训练规格。只输出 JSON：{"spec": {训练规格}, "name": "简短中文名", '
            '"note": "这一轮改了什么、为什么"}'
        )

    def _features_default(self) -> list[dict[str, Any]]:
        lib = self.deps.factors()
        active = [f for f in lib.list() if f.get("status") in ("active", "decaying")][:10]
        if len(active) >= 3:
            return [{"factor_id": f["id"]} for f in active]
        return [dict(f) for f in M.DEFAULT_FEATURES]

    def _rule_proposal(self, pid: str, project: dict[str, Any], r: int, tried: set[str]) -> dict[str, Any] | None:
        cur = self.current(project)
        horizon = project.get("horizon") or 1
        base = (
            loads_spec(project["code"])
            if project.get("code")
            else {"model": "ridge", "label": "fwd_ret", "horizon": horizon, "features": self._features_default()}
        )
        label = base.get("label") or "fwd_ret"
        plan: list[tuple[dict[str, Any], str]] = []
        if label == "volatility":
            plan += [
                ({**base, "model": "har", "params": {}}, "HAR-RV：用 1/6/24 根的已实现波动率做线性回归"),
                ({**base, "model": "garch", "params": {}}, "GARCH(1,1)：逐标的估计条件方差"),
                ({**base, "model": "gbdt", "params": {}}, "梯度提升树：加入其它特征的非线性"),
            ]
        else:
            plan.append(({**base, "model": "ridge", "params": {}}, "线性基线：先看特征本身的线性信息量"))
            plan.append(({**base, "model": "gbdt", "params": {}}, "梯度提升树：捕捉非线性与特征交互"))
            plan.append(({**base, "model": "mlp", "params": {}}, "MLP：两层神经网络，验证集早停"))
            if L.backends()["torch"]:
                plan.append(({**base, "model": "gru", "params": {}}, "GRU：看过去 16 根的特征序列"))
            rep = self.report(pid, cur["v"]) if cur else None
            imp = (rep or {}).get("importance") or []
            if imp and len(base.get("features") or []) > 3:
                keep = {i["name"] for i in imp if (i.get("drop") or 0) > 0}
                feats = [f for f in (rep or {}).get("spec", {}).get("features", []) if f["name"] in keep]
                if 2 <= len(feats) < len(base.get("features") or []):
                    plan.append(({**base, "features": feats}, f"按重要性精简特征：只保留 {len(feats)} 个有贡献的"))
            plan.append(
                ({**base, "model": "ridge", "label": "cs_rank", "params": {}}, "换成截面排名标签，降低极端收益的影响")
            )
        for art, why in plan:
            try:
                art = self.normalize(project, art)
            except M.ModelError:
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
            content="**规则降级**：预设的几种模型都试过了。配置模型后 AI 可以设计新的特征与结构。",
        )
        return None

    # ---------------- 对话 ----------------
    def chat(self, pid: str, text: str) -> None:
        text = text.strip()
        head, _, rest = text.partition(" ")
        if head in ("/训练", "/train", "/评估", "/eval", "/回测", "/backtest"):
            self.evaluate_current(pid)
            return
        if head in ("/团队", "/team"):
            self._convene(
                pid, rest or "评审当前模型：特征与标签是否合理、样本外是否可信、有没有泄漏与过拟合、下一步怎么改"
            )
            return
        if head in ("/迭代", "/auto"):
            self.autopilot(pid)
            return
        if head in ("/上线", "/save", "/入库"):
            self.save_to_library(pid, status="active")
            return
        if head in ("/解释", "/explain"):
            self._explain(pid)
            return
        if text.startswith("{"):
            self.submit(pid, text, note="研究者直接输入的训练规格", source="user")
            return
        try:
            self._chat_llm(pid, text)
        except LLMError as exc:
            self._chat_fallback(pid, text, str(exc))

    def _chat_fallback(self, pid: str, text: str, reason: str) -> None:
        project = self.store.get(pid) or {}
        if not project.get("code") or any(k in text for k in ("训练", "模型", "预测", "试试")):
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
                "- 粘贴一份 JSON 训练规格，平台会立即训练并评估\n"
                "- 点「自动迭代」：线性基线 → 梯度提升树 → MLP → 精简特征，每一版都和基线比\n"
                "- `/训练` 重新训练　`/团队` 召集模型小组　`/上线` 存进模型库"
            ),
        )

    def _explain(self, pid: str) -> None:
        project = self.store.get(pid) or {}
        if not project.get("code"):
            self._post(
                pid, role="assistant", kind="text", content="还没有训练规格。描述你想预测什么，或点「自动迭代」。"
            )
            return
        art = loads_spec(project["code"])
        parts = [
            f"**{L.LEARNERS[art['model']]['label']}** 预测未来 {art.get('horizon')} 根 K 线的"
            f"**{M.LABELS[art['label']]['label']}**",
            "",
            f"- 特征（{len(art['features'])} 个）：{'、'.join(f['name'] for f in art['features'])}",
            f"- 模型说明：{L.LEARNERS[art['model']]['hint']}",
            f"- 标签说明：{M.LABELS[art['label']]['hint']}",
            f"- 验证：{art.get('folds', 4)} 段扩张窗口滚动训练，purge {art.get('horizon')} 根",
        ]
        cur = self.current(project)
        if cur and cur.get("metrics"):
            parts.append(f"- 最近训练：{summary_text(cur['metrics'])}")
        self._post(pid, role="assistant", kind="text", content="\n".join(parts), mode="static")

    def _chat_tools(self, pid: str) -> dict[str, Tool]:
        obj = {"type": "object"}

        def features() -> Any:
            fl = self.deps.factors()
            return self._tool(
                pid,
                "list_features",
                "查看可用特征",
                {},
                lambda: {
                    "factor_library": fl.search()[:20],
                    "defaults": M.DEFAULT_FEATURES,
                    "labels": {k: v["label"] for k, v in M.LABELS.items()},
                    "models": {
                        k: {"label": v["label"], "params": v["params"], "available": L.available()[k]}
                        for k, v in L.LEARNERS.items()
                    },
                },
            )

        def write(spec: dict | None = None, note: str = "", name: str = "") -> Any:
            return self.submit(pid, spec or {}, note=note or "模型提交", name=name)

        def train() -> Any:
            return self.evaluate_current(pid)

        def versions() -> Any:
            p = self.store.get(pid) or {}
            return [
                {
                    "v": r["v"],
                    "spec": r["code"][:600],
                    "note": r["note"],
                    "oos_rank_ic": (r.get("metrics") or {}).get("oos_rank_ic"),
                    "beat_baseline": (r.get("metrics") or {}).get("beat_baseline"),
                    "passed": (r.get("evaluation") or {}).get("passed"),
                }
                for r in p.get("versions") or []
            ]

        def library(query: str = "") -> Any:
            ml = self.deps.library()
            return self._tool(
                pid,
                "search_model_library",
                "查询模型库",
                {"关键词": query or "全部"},
                lambda: {"models": ml.search(query), "runs_so_far": ml.trial_count()},
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
                "goal": MODEL.items_text(spec["items"]),
                "latest": ev and {"passed": ev["passed"], "items": ev["items"], "gaps": MODEL.gap_report(ev)},
                "reasonableness": self.check_goal(p, spec),
            }

        def team(topic: str = "") -> Any:
            return self._convene(pid, topic or "评审当前模型")

        def ask(question: str = "", options: list[str] | None = None) -> Any:
            self._ask(pid, question or "这个模型要上线吗？", options or ["上线", "继续优化", "换个方向"])
            return {"ok": True, "note": "已向研究者提问，本轮结束"}

        spec_schema = {
            "type": "object",
            "properties": {
                "model": {"type": "string", "enum": list(L.LEARNERS)},
                "label": {"type": "string", "enum": list(M.LABELS)},
                "horizon": {"type": "integer"},
                "features": {"type": "array", "items": {"type": "object"}},
                "params": {"type": "object"},
                "folds": {"type": "integer"},
                "seed": {"type": "integer"},
            },
            "required": ["model", "label", "features"],
        }
        return {
            **self._shared_tools(pid),
            "list_features": Tool(
                "list_features", "查看因子库（可作特征）、默认特征、标签与模型菜单", {**obj, "properties": {}}, features
            ),
            "write_model_spec": Tool(
                "write_model_spec",
                "提交训练规格：平台立即构建数据集、滚动训练、样本外评估并做泄漏检查",
                {
                    **obj,
                    "properties": {"spec": spec_schema, "name": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["spec"],
                },
                write,
            ),
            "train_model": Tool(
                "train_model", "按当前规格重新训练（数据或目标变了之后）", {**obj, "properties": {}}, train
            ),
            "search_model_library": Tool(
                "search_model_library",
                "查询模型库与已训练次数",
                {**obj, "properties": {"query": {"type": "string"}}},
                library,
            ),
            "list_versions": Tool("list_versions", "列出本项目历次训练规格及成绩", {**obj, "properties": {}}, versions),
            "set_model_goal": Tool(
                "set_model_goal",
                "设定或修改模型研究目标（只传要改的字段）。指标："
                + "、".join(f"{k}={v['label']}" for k, v in MODEL.metrics.items()),
                {
                    **obj,
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "metric": {"type": "string", "enum": list(MODEL.metrics)},
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
                "查看当前目标、最近一次训练与差距，以及目标是否合理",
                {**obj, "properties": {}},
                check_goals,
            ),
            "convene_team": Tool(
                "convene_team",
                "召集模型小组评审当前模型，返回小组结论",
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


__all__ = ["MODEL_SYSTEM", "ModelDeps", "ModelLab", "new_model_project", "summary_text"]
