"""组合研究室（研究类型 = 组合）：把预期收益和风险变成权重，并把风控规则写进方案。

产物是一份**配置方案**（JSON）：风险模型、优化器、预期收益来源、约束与风控规则。每一版都在同一组标的上做
滚动组合回测（只用过去的数据估计、扣成本、对照等权），附带风险贡献、VaR/CVaR、压力测试与有效前沿。
达标后问研究者是否启用（存入方案库，策略里用 optimize('方案名') 调用）。
没有模型密钥时走规则降级：风险平价 → HRP → 最小方差 → 加目标波动 → 最小 CVaR → 动量均值-方差。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..llm.base import LLMError, Tool
from ..research import portfolio as PF
from ..research.goals import PORTFOLIO
from ..research.panel import UNIVERSES, Panel, PanelError
from ..research.portfolio_lib import PortfolioLibrary, signal_for
from .research_lab import DomainLab, base_project, dumps_spec, loads_spec
from .strategy_lab import StopRequested, _short

PORTFOLIO_SYSTEM = """你是 QuantLab Bot —— QuantLab 平台内置的 AI 研究员，在这里担任组合经理兼风控官，和研究者一起设计加密资产的配置与风控方案。

## 配置方案（write_portfolio 的参数）
{"optimizer": "equal|inverse_vol|min_var|mean_var|risk_parity|hrp|max_div|min_cvar|kelly|black_litterman",
 "risk_model": "sample|ledoit_wolf|ewma|factor", "lookback": 180, "rebalance": 6,
 "expected": "none|momentum|mean|model:模型库里的名字|factor:因子库里的名字",
 "risk_aversion": 4, "kelly_fraction": 0.3, "ewma_halflife": 40, "factors": 3, "cvar_alpha": 0.95, "tau": 0.05,
 "views": [{"symbol": "BTC/USDT", "ret": 0.3, "confidence": 0.5}],
 "constraints": {"long_only": true, "max_weight": 0.35, "min_weight": 0, "max_gross": 1.0, "max_turnover": 1.0, "top_k": 0},
 "risk": {"vol_target": 0.5, "var_limit": 0.05, "dd_stop": 0.2, "cppi": {"floor": 0.8, "multiplier": 3}}}
- lookback / rebalance 以 K 线根数计；vol_target 是年化波动；var_limit 是单日 95% VaR；dd_stop 是回撤超过多少时减半仓位。
- views 是 Black-Litterman 的绝对观点（年化收益），confidence 0~1；expected 为模型时模型预测也会作为观点。
- 模型 / 因子给出的分数按 Grinold 公式 α = IC × σ × z 换算成预期收益，不要把预测值当收益。

## 平台怎么评估
滚动组合回测：每个调仓点只用之前 lookback 根的收益估计风险与预期，权重随价格漂移，扣换手成本，同时跑等权基准。
报告：夏普、回撤、年化波动、CVaR、换手、有效持仓数、夏普超出等权、前后两半表现；最新权重的风险贡献、VaR/CVaR、
压力测试（2020-03、LUNA、FTX 等情景与相关性趋同）和有效前沿。

## 工作方式
1. 先用 list_inputs 看标的、可用的模型与因子，从等权和风险平价这类不需要预期收益的方法起步。
2. 只有在预期收益来源经过验证（模型库里上线的模型、因子库里入库的因子）时才用均值-方差 / 凯利 / BL。
3. 风控规则要有理由：目标波动对应研究者能承受的波动；回撤止损会错过反弹，要看压力测试和前后两半。
4. 目标用 set_portfolio_goal / check_goals 管理；达标或需要研究者判断时用 ask_user。

## 铁律
- 等权是很难打败的基准，跑不赢就如实说。
- 不承诺收益；合成数据上的结论要明确说明不能作为结论。
- 回答用中文，简洁，先结论后细节。"""


@dataclass
class PortfolioDeps:
    panel: Callable[..., Panel]
    library: Callable[[], PortfolioLibrary]
    store: Any  # 用于读取模型库 / 因子库算预期收益


def new_portfolio_project(payload: dict[str, Any]) -> dict[str, Any]:
    universe = payload.get("universe") or "major10"
    symbols = [s.strip().upper() for s in payload.get("securities") or [] if s.strip()] or list(
        UNIVERSES.get(universe, UNIVERSES["major10"])["symbols"]
    )
    p = base_project(
        "portfolio", payload, PORTFOLIO, symbols=symbols, team_id="portfolio_team", title_default="未命名组合研究"
    )
    p["scheme_id"] = None
    return p


def summary_text(s: dict[str, Any] | None) -> str:
    if not s:
        return "尚未评估"

    def f(k: str, pct: bool = False, d: int = 2) -> str:
        v = s.get(k)
        if v is None:
            return "—"
        return f"{v * 100:.1f}%" if pct else f"{v:.{d}f}"

    head = (
        f"{PF.OPTIMIZERS.get(s.get('optimizer') or '', {}).get('label', s.get('optimizer'))} · "
        f"{PF.RISK_MODELS.get(s.get('risk_model') or '', {}).get('label', '')}"
    )
    return (
        f"{head}：夏普 {f('sharpe')}（等权 {f('ew_sharpe')}），年化 {f('ann_return', True)}，"
        f"波动 {f('ann_vol', True)}，"
        f"最大回撤 {f('max_drawdown', True)}（等权 {f('ew_max_drawdown', True)}），CVaR95 {f('cvar_95', True)}，"
        f"换手 {f('turnover', True)}，有效持仓 {f('effective_n', d=1)}"
    )


class PortfolioLab(DomainLab):
    kind = "portfolio"
    domain = PORTFOLIO
    team_default = "portfolio_team"
    noun = "配置方案"
    save_choices = ("启用", "存为候选")
    system_prompt = PORTFOLIO_SYSTEM
    system_title = "配置方案与评估口径"
    artifact_label = "当前配置方案"
    metrics_label = "最近一次评估"
    min_symbols = 2

    created_hint = "描述你想怎么配置（例如“主流币风险平价，年化波动控制在 50% 以内”），或点「自动迭代」从风险平价开始。"

    def __init__(self, router: Any, store: Any, tools: Any, deps: PortfolioDeps) -> None:
        super().__init__(router, store, tools)
        self.deps = deps

    def new_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        return new_portfolio_project(payload)

    def summary_text(self, s: dict[str, Any] | None) -> str:
        return summary_text(s)

    def normalize(self, project: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
        try:
            return PF.normalize_config(raw)
        except PF.PortfolioError as exc:
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
                    "name": "write_portfolio",
                    "label": "提交配置方案",
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
            kind="portfolio_spec",
            content=note or "提交配置方案",
            data={
                "v": row["v"],
                "before": before,
                "after": code,
                "spec": cfg,
                "optimizer_label": PF.OPTIMIZERS[cfg["optimizer"]]["label"],
                "risk_label": PF.RISK_MODELS[cfg["risk_model"]]["label"],
            },
        )
        return self.evaluate_current(pid)

    def _evaluate(self, project: dict[str, Any], cfg: dict[str, Any], pid: str | None = None) -> dict[str, Any]:
        fields: list[str] = []
        src = cfg.get("expected") or "none"
        if src.startswith("model:"):
            from ..research.model_lib import ModelLibrary
            from ..research.models import fields_for

            rec = ModelLibrary(self.deps.store).by_name(src[6:])
            if rec:
                fields = fields_for(rec["spec"])
        elif src.startswith("factor:"):
            from ..research.factor_expr import fields_used, parse
            from ..research.factor_lib import FactorLibrary

            rec = FactorLibrary(self.deps.store).by_name(src[7:])
            try:
                fields = sorted(fields_used(parse(rec["expression"] if rec else src[7:])))
            except Exception:
                fields = []
        panel = self.deps.panel(
            project["securities"],
            project["timeframe"],
            fields=fields,
            start=project.get("start"),
            end=project.get("end"),
            bars_limit=project.get("bars_limit") or 1500,
            source=project.get("data_source") or "auto",
        )
        stop = (lambda: self._check_stop(pid)) if pid else None
        signal, ic, warns = signal_for(cfg, panel, self.deps.store, stop=stop)
        rep = PF.evaluate_scheme(
            cfg, panel, cost_bps=project.get("cost_bps") or 10.0, signal=signal, signal_ic=ic, stop=stop
        )
        rep["panel"]["warnings"] = [*rep["panel"].get("warnings", []), *warns]
        return rep

    def evaluate_current(self, pid: str, *, cost_scale: float = 1.0, **_: Any) -> dict[str, Any]:
        project = self.store.get(pid) or {}
        if not project.get("code"):
            return {"ok": False, "error": "当前还没有配置方案"}
        v = project.get("current_v") or 0
        try:
            cfg = PF.normalize_config(loads_spec(project["code"]))
        except (ValueError, PF.PortfolioError) as exc:
            self._post(pid, role="event", kind="error", content=f"方案有误：{exc}")
            return {"ok": False, "error": str(exc)}
        stress = cost_scale != 1.0
        p2 = {**project, "cost_bps": (project.get("cost_bps") or 10.0) * cost_scale}
        try:
            report = self._tool(
                pid,
                "evaluate_portfolio",
                "滚动组合回测" + (f"（{cost_scale:g}× 成本）" if stress else ""),
                {
                    "版本": f"v{v}",
                    "优化器": PF.OPTIMIZERS[cfg["optimizer"]]["label"],
                    "风险模型": PF.RISK_MODELS[cfg["risk_model"]]["label"],
                    "标的": f"{len(project['securities'])} 个",
                },
                lambda: self._evaluate(p2, cfg, pid),
            )
        except StopRequested:
            raise
        except (PF.PortfolioError, PanelError, ValueError) as exc:
            self._post(pid, role="event", kind="error", content=f"评估没有完成：{exc}")
            return {"ok": False, "error": str(exc)}
        s = report["summary"]
        # 没设目标时不拿出厂默认阈值评判，只汇报实测结果
        evaluation = PORTFOLIO.evaluate(s, self.project_spec(project)["items"]) if self.has_goal(project) else None
        run_id = None
        if not stress:
            lib = self.deps.library()
            run = lib.record_run(
                cfg,
                report,
                symbols=project["securities"],
                timeframe=project["timeframe"],
                source="lab",
                project_id=pid,
                note=(self.current(project) or {}).get("note") or "",
            )
            run_id = run["id"]
            self._set_version_result(
                pid, v, metrics=s, evaluation=evaluation, admitted=report["admitted"], run_id=run_id
            )
        self._post(
            pid,
            role="tool",
            kind="portfolio_report",
            content=summary_text(s),
            data={
                "v": v,
                "run_id": run_id,
                "summary": s,
                "checks": report["checks"],
                "admitted": report["admitted"],
                "evaluation": None if stress else evaluation,
                "cost_scale": cost_scale,
                "nav": report["nav"][:: max(1, len(report["nav"]) // 150)],
                "latest": report.get("latest"),
                "risk": {k: v2 for k, v2 in (report.get("risk") or {}).items() if k != "corr"},
                "stress": report.get("stress"),
                "panel": report["panel"],
            },
        )
        return {
            "ok": True,
            "version": v,
            "run_id": run_id,
            "summary": {
                k: s.get(k)
                for k in (
                    "sharpe",
                    "ew_sharpe",
                    "beat_ew",
                    "ann_return",
                    "ann_vol",
                    "max_drawdown",
                    "ew_max_drawdown",
                    "cvar_95",
                    "turnover",
                    "effective_n",
                    "sharpe_h1",
                    "sharpe_h2",
                    "triggers",
                )
            },
            "latest_weights": (report.get("latest") or {}).get("weights"),
            "risk_contrib": (report.get("latest") or {}).get("risk_contrib"),
            "stress": [{"name": x["name"], "pnl": x["pnl"]} for x in report.get("stress") or []],
            "checks": [{"label": c["label"], "ok": c["ok"], "detail": c["detail"]} for c in report["checks"]],
            "goal": (
                {"passed": evaluation["passed"], "gaps": PORTFOLIO.gap_report(evaluation)}
                if evaluation
                else "未设定验收目标：只汇报实测结果，不要说达标或未达标"
            ),
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
            self._post(pid, role="event", kind="error", content="当前版本还没有评估，先评估再存入方案库")
            return None
        if status == "active" and not cur.get("admitted"):
            status = "candidate"
            self._post(
                pid,
                role="event",
                kind="text",
                content="风险调整后跑输等权，先存为候选。可以在「组合与风控」里手动改状态。",
            )
        lib = self.deps.library()
        report = self.report(pid, cur["v"])
        if report is not None:
            report = {**report, "project_goal_passed": (cur.get("evaluation") or {}).get("passed")}
        rec = self._tool(
            pid,
            "save_portfolio",
            "存入方案库",
            {"状态": "启用" if status == "active" else "候选"},
            lambda: lib.save(
                name=cur.get("name") or project.get("title") or "",
                config=loads_spec(cur["code"]),
                report=report,
                symbols=project["securities"],
                timeframe=project["timeframe"],
                status=status,
                note=project.get("idea") or "",
                scheme_id=project.get("scheme_id") if project.get("library_version") == cur.get("v") else None,
                project_id=pid,
                run_id=cur["run_id"],
                created_by="ai" if cur.get("source") != "user" else "user",
            ),
        )
        self.store.patch(pid, scheme_id=rec["id"], library_id=rec["id"], library_version=cur.get("v"))
        self._post(
            pid,
            role="event",
            kind="saved",
            content=f"已存入方案库：{rec['name']}",
            data={"scheme_id": rec["id"], "name": rec["name"], "status": rec["status"], "kind": "portfolio"},
        )
        return rec

    def finish_warnings(self, project: dict[str, Any], best: dict[str, Any]) -> list[str]:
        s = best.get("metrics") or {}
        warn = []
        if (s.get("beat_ew") or 0) < 0:
            warn.append("风险调整后没有跑赢等权")
        if (s.get("sharpe_h1") or 0) * (s.get("sharpe_h2") or 0) < 0:
            warn.append("前后两半表现方向相反")
        rep = self.report(project["id"], best["v"]) or {}
        if (rep.get("panel") or {}).get("synthetic"):
            warn.append("用了合成数据，结论不能作为研究结论")
        return warn

    # ---------------- 自动迭代 ----------------
    def proposal_prompt(self, project: dict[str, Any], r: int, history: list[str], notes: str) -> str:
        from ..research.factor_lib import FactorLibrary
        from ..research.model_lib import ModelLibrary

        cur = self.current(project)
        return (
            f"研究方向：{project.get('idea') or project.get('title')}\n目标：{self.goal_text(project)}\n"
            f"标的 {len(project['securities'])} 个：{','.join(project['securities'][:20])}，"
            f"周期 {project['timeframe']}。\n"
            + (f"小组建议：\n{notes}\n" if notes else "")
            + (
                f"当前方案：{project.get('code')}\n最近评估：{summary_text((cur or {}).get('metrics'))}\n"
                f"未达标：{json.dumps(PORTFOLIO.gap_report((cur or {}).get('evaluation') or {}), ensure_ascii=False)}\n"
                if project.get("code")
                else ""
            )
            + ("本项目试过：\n" + "\n".join(history[-6:]) + "\n" if history else "")
            + f"模型库：{json.dumps(ModelLibrary(self.deps.store).search()[:8], ensure_ascii=False)}\n"
            + f"因子库：{json.dumps(FactorLibrary(self.deps.store).search()[:8], ensure_ascii=False)}\n\n"
            '请给出这一轮的配置方案。只输出 JSON：{"spec": {配置方案}, "name": "简短中文名", '
            '"note": "这一轮改了什么、为什么"}'
        )

    def _rule_proposal(self, pid: str, project: dict[str, Any], r: int, tried: set[str]) -> dict[str, Any] | None:
        cur = self.current(project)
        base = loads_spec(project["code"]) if project.get("code") else {}
        s = (cur or {}).get("metrics") or {}
        items = {i["metric"]: i for i in self.project_spec(project)["items"]}
        plan: list[tuple[dict[str, Any], str]] = [
            ({"optimizer": "risk_parity", "risk_model": "ledoit_wolf"}, "风险平价 + Ledoit-Wolf：每个币贡献相同风险"),
            ({"optimizer": "hrp", "risk_model": "ledoit_wolf"}, "HRP：先按相关性聚类，再在簇之间分配风险"),
            ({"optimizer": "min_var", "risk_model": "ledoit_wolf"}, "最小方差：只看风险"),
        ]
        dd_target = (items.get("max_drawdown") or {}).get("target")
        vol_target = (items.get("ann_vol") or {}).get("target")
        if s and (
            (dd_target and (s.get("max_drawdown") or 0) > dd_target)
            or (vol_target and (s.get("ann_vol") or 0) > vol_target)
        ):
            vt = vol_target or max(0.15, min(0.8, (s.get("ann_vol") or 0.6) * 0.7))
            plan.insert(
                0,
                (
                    {**base, "risk": {**(base.get("risk") or {}), "vol_target": round(vt, 2)}},
                    f"在当前方案上加目标波动 {vt:.0%}：波动高时自动降仓",
                ),
            )
        plan += [
            ({"optimizer": "min_cvar", "risk_model": "ledoit_wolf"}, "最小 CVaR：直接压尾部损失"),
            ({"optimizer": "max_div", "risk_model": "ledoit_wolf"}, "最大分散化：偏好相关性低的币"),
            (
                {"optimizer": "risk_parity", "risk_model": "ewma", "ewma_halflife": 30},
                "风险平价 + EWMA：对波动变化反应更快",
            ),
            (
                {
                    "optimizer": "mean_var",
                    "expected": "momentum",
                    "risk_aversion": 8,
                    "constraints": {"max_weight": 0.3, "max_turnover": 0.5},
                },
                "动量均值-方差：加大风险厌恶并限制换手",
            ),
        ]
        from ..research.model_lib import ModelLibrary

        active = [m for m in ModelLibrary(self.deps.store).list("active") if m["spec"].get("label") != "volatility"]
        if active:
            plan.append(
                (
                    {"optimizer": "black_litterman", "expected": f"model:{active[0]['name']}"},
                    f"Black-Litterman：以等权为先验，融合模型「{active[0]['name']}」的预测作为观点",
                )
            )
        for art, why in plan:
            try:
                art = self.normalize(project, art)
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
            content="**规则降级**：预设方案都试过了。配置模型后 AI 可以设计组合约束与风控规则。",
        )
        return None

    # ---------------- 对话 ----------------
    def chat(self, pid: str, text: str) -> None:
        text = text.strip()
        head, _, rest = text.partition(" ")
        if head in ("/评估", "/eval", "/回测", "/backtest"):
            self.evaluate_current(pid)
            return
        if head in ("/团队", "/team"):
            self._convene(pid, rest or "评审当前组合方案：风险估计是否稳健、约束与风控规则是否合理、压力情景下能否承受")
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
            self.submit(pid, text, note="研究者直接输入的配置方案", source="user")
            return
        try:
            self._chat_llm(pid, text)
        except LLMError as exc:
            self._chat_fallback(pid, text, str(exc))

    def _chat_fallback(self, pid: str, text: str, reason: str) -> None:
        if self._preserve_on_model_failure(pid, reason):
            return
        project = self.store.get(pid) or {}
        if not project.get("code") or any(k in text for k in ("配置", "组合", "风控", "试试", "优化")):
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
                f"**规则降级**（{_short(reason)}）。这次没有拿到模型回答，可以点这条消息上的「重试」再试一次；现在也能用：\n\n"
                "- 粘贴一份 JSON 配置方案，平台会立即做滚动组合回测\n"
                "- 点「自动迭代」：风险平价 → HRP → 最小方差 → 加目标波动……每一版都和等权比\n"
                "- `/评估` 重新评估　`/团队` 召集组合小组　`/启用` 存进方案库"
            ),
        )

    def _explain(self, pid: str) -> None:
        project = self.store.get(pid) or {}
        if not project.get("code"):
            self._post(
                pid, role="assistant", kind="text", content="还没有配置方案。描述你的配置想法，或点「自动迭代」。"
            )
            return
        cfg = PF.normalize_config(loads_spec(project["code"]))
        c, rk = cfg["constraints"], cfg["risk"]
        rules = [
            f"目标年化波动 {rk['vol_target']:.0%}" if rk.get("vol_target") else "",
            f"单日 95% VaR ≤ {rk['var_limit']:.1%}" if rk.get("var_limit") else "",
            f"回撤超过 {rk['dd_stop']:.0%} 时减半" if rk.get("dd_stop") else "",
            f"CPPI 保底 {rk['cppi']['floor']:.0%}、乘数 {rk['cppi']['multiplier']:g}" if rk.get("cppi") else "",
        ]
        parts = [
            f"**{PF.OPTIMIZERS[cfg['optimizer']]['label']}** + **{PF.RISK_MODELS[cfg['risk_model']]['label']}**",
            "",
            f"- 做法：{PF.OPTIMIZERS[cfg['optimizer']]['hint']}",
            f"- 估计窗口 {cfg['lookback']} 根，每 {cfg['rebalance']} 根调仓",
            f"- 预期收益：{PF.EXPECTED_SOURCES.get(cfg['expected'], cfg['expected'])}",
            f"- 约束：{'只做多' if c['long_only'] else '可做空'}，单币 ≤ {c['max_weight']:.0%}，总仓位 "
            f"≤ {c['max_gross']:.0%}"
            + (f"，每次换手 ≤ {c['max_turnover']:.0%}" if c["max_turnover"] < 1 else "")
            + (f"，只持前 {c['top_k']} 个" if c["top_k"] else ""),
            f"- 风控：{'；'.join(x for x in rules if x) or '无额外规则'}",
        ]
        cur = self.current(project)
        if cur and cur.get("metrics"):
            parts.append(f"- 最近评估：{summary_text(cur['metrics'])}")
        self._post(pid, role="assistant", kind="text", content="\n".join(parts), mode="static")

    def _chat_tools(self, pid: str) -> dict[str, Tool]:
        from ..research.factor_lib import FactorLibrary
        from ..research.model_lib import ModelLibrary

        obj = {"type": "object"}

        def inputs() -> Any:
            p = self.store.get(pid) or {}
            return self._tool(
                pid,
                "list_inputs",
                "查看可用输入",
                {},
                lambda: {
                    "symbols": p.get("securities"),
                    "timeframe": p.get("timeframe"),
                    "optimizers": {k: v["label"] for k, v in PF.OPTIMIZERS.items()},
                    "risk_models": {k: v["label"] for k, v in PF.RISK_MODELS.items()},
                    "models": ModelLibrary(self.deps.store).search()[:10],
                    "factors": FactorLibrary(self.deps.store).search()[:10],
                    "scenarios": [s["name"] for s in PF.SCENARIOS],
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
                    "spec": r["code"][:600],
                    "note": r["note"],
                    "sharpe": (r.get("metrics") or {}).get("sharpe"),
                    "max_drawdown": (r.get("metrics") or {}).get("max_drawdown"),
                    "passed": (r.get("evaluation") or {}).get("passed"),
                }
                for r in p.get("versions") or []
            ]

        def library(query: str = "") -> Any:
            lib = self.deps.library()
            return self._tool(
                pid,
                "search_portfolio_library",
                "查询方案库",
                {"关键词": query or "全部"},
                lambda: {"schemes": lib.search(query), "runs_so_far": lib.trial_count()},
            )

        def set_goal(**kw: Any) -> Any:
            out = self.set_goal(pid, kw, source="model")
            if out.get("ok") is False:
                return out
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
                "goal": PORTFOLIO.items_text(spec["items"]),
                "latest": ev and {"passed": ev["passed"], "items": ev["items"], "gaps": PORTFOLIO.gap_report(ev)},
                "reasonableness": self.check_goal(p, spec),
            }

        def team(topic: str = "") -> Any:
            return self._convene(pid, topic or "评审当前组合方案")

        def ask(question: str = "", options: list[str] | None = None) -> Any:
            self._ask(pid, question or "这个方案要启用吗？", options or ["启用", "继续优化", "换个方向"])
            return {"ok": True, "note": "已向研究者提问，本轮结束"}

        spec_schema = {
            "type": "object",
            "properties": {
                "optimizer": {"type": "string", "enum": list(PF.OPTIMIZERS)},
                "risk_model": {"type": "string", "enum": list(PF.RISK_MODELS)},
                "lookback": {"type": "integer"},
                "rebalance": {"type": "integer"},
                "expected": {"type": "string"},
                "risk_aversion": {"type": "number"},
                "kelly_fraction": {"type": "number"},
                "views": {"type": "array", "items": {"type": "object"}},
                "constraints": {"type": "object"},
                "risk": {"type": "object"},
            },
            "required": ["optimizer"],
        }
        return {
            **self._shared_tools(pid),
            "list_inputs": Tool(
                "list_inputs",
                "查看标的、优化器与风险模型菜单、可用的模型与因子、压力情景",
                {**obj, "properties": {}},
                inputs,
            ),
            "write_portfolio": Tool(
                "write_portfolio",
                "提交配置方案：平台立即做滚动组合回测、风险分解与压力测试",
                {
                    **obj,
                    "properties": {"spec": spec_schema, "name": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["spec"],
                },
                write,
            ),
            "evaluate_portfolio": Tool("evaluate_portfolio", "按当前方案重新评估", {**obj, "properties": {}}, evaluate),
            "search_portfolio_library": Tool(
                "search_portfolio_library", "查询方案库", {**obj, "properties": {"query": {"type": "string"}}}, library
            ),
            "list_versions": Tool("list_versions", "列出本项目历次方案及成绩", {**obj, "properties": {}}, versions),
            "set_portfolio_goal": Tool(
                "set_portfolio_goal",
                "设定或修改组合研究目标（只传要改的字段）。指标："
                + "、".join(f"{k}={v['label']}" for k, v in PORTFOLIO.metrics.items()),
                {
                    **obj,
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "metric": {"type": "string", "enum": list(PORTFOLIO.metrics)},
                                    "op": {"type": "string", "enum": [">=", "<="]},
                                    "target": {"type": "number"},
                                    "priority": {"type": "string", "enum": ["must", "try"]},
                                },
                                "required": ["metric", "target"],
                            },
                        },
                        "symbols": {"type": "array", "items": {"type": "string"}},
                        "timeframe": {"type": "string"},
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
                "召集组合小组评审当前方案，返回小组结论",
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


__all__ = ["PORTFOLIO_SYSTEM", "PortfolioDeps", "PortfolioLab", "new_portfolio_project", "summary_text"]
