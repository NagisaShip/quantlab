"""AI 策略室 API：策略项目、对话（工具调用）、自动迭代闭环、版本与上下文。

回合（对话 / 自动迭代 / 用户决定）都在后台线程执行并逐条落库，前端用
`GET /api/lab/projects/{id}/events` 以 SSE 推送增量消息（模型输出逐段更新同一条消息），
也可以用 `GET /api/lab/projects/{id}?after=<seq>&since=<server_time>` 轮询，看到模型“边想边做”的过程。
`wait=true` 时同步执行（测试与脚本用）。回测仍走平台的 run → worker 通道，与「回测」页面同口径。
"""

from __future__ import annotations

import base64
import binascii
import inspect
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..agents import lab_goal as G
from ..agents.strategy_lab import DEFAULT_GOALS, LabStore, LabTools, StrategyLab, new_project
from ..engine.research import summarize_metrics
from ..llm.base import split_data_url
from ..platform.errors import AppError
from ..research import goals as RG
from ..sdk import declared_params
from ..strategies.sdk_templates import TEMPLATES, get_template
from .deps import get_services

try:  # 因子研究依赖 numpy；没装时平台其余功能照常可用，因子相关接口给出明确提示
    from ..agents import factor_lab as FL
    from ..agents.execution_lab import ExecutionDeps, ExecutionLab
    from ..agents.factor_lab import FactorDeps, FactorLab, new_factor_project
    from ..agents.model_lab import ModelDeps, ModelLab
    from ..agents.portfolio_lab import PortfolioDeps, PortfolioLab
    from ..agents.research_lab import DomainLab
    from ..research.datahub import DataHub
    from ..research.execution_lib import ExecutionLibrary, load_tapes
    from ..research.factor_lib import FactorLibrary
    from ..research.model_lib import ModelLibrary
    from ..research.panel import UNIVERSES, build_panel
    from ..research.portfolio_lib import PortfolioLibrary

    FACTOR_READY = True
except ImportError:  # pragma: no cover - 只在缺少 numpy 的环境里走到
    FACTOR_READY = False
    UNIVERSES = {}

    class FactorLab:  # type: ignore[no-redef]
        """占位：isinstance 判断永远为假。"""

    class DomainLab:  # type: ignore[no-redef]
        """占位：isinstance 判断永远为假。"""


NUMPY_HINT = "因子研究需要 numpy，请先执行 pip install numpy（或 pip install -e .）后重启服务"

router = APIRouter(prefix="/api/lab", tags=["AI 策略室"])


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
Effort = Literal["low", "medium", "high", "max"]
# 行情来源：auto / exchange / synthetic；执行研究另有 kline（只用真实 1 分钟 K 线）与 recording:<录制 id>
DATA_SOURCE = r"^(auto|exchange|synthetic|kline|recording:[A-Za-z0-9_]{1,40})$"


class Goals(BaseModel):
    min_sharpe: float | None = None
    max_drawdown: float | None = Field(None, ge=0, le=1)
    min_trades: float | None = None
    min_excess: float | None = None


class PinIn(BaseModel):
    kind: str = "note"
    title: str = Field(..., min_length=1, max_length=60)
    text: str = Field(..., min_length=1, max_length=8000)


LabKind = Literal["strategy", "factor", "model", "portfolio", "execution"]
DomainKey = Literal["strategy", "factor", "model", "portfolio", "execution"]


class ProjectIn(BaseModel):
    kind: LabKind = "strategy"
    title: str = ""
    idea: str = Field("", max_length=4000)
    securities: list[str] = Field(default_factory=list)
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"] = "1h"
    model: str = ""
    effort: Effort = "medium"
    goals: Goals | None = None
    max_rounds: int = Field(4, ge=1, le=8)
    bars_limit: int = Field(1500, ge=200, le=20_000)
    data_source: str = Field("auto", pattern=DATA_SOURCE)
    template_id: str | None = None
    code: str | None = None
    pins: list[PinIn] = Field(default_factory=list)
    # 因子研究
    universe: str = "major10"
    horizon: int = Field(1, ge=1, le=60)
    executor: Literal["solo", "team"] = "solo"
    team_id: str | None = None
    cost_bps: float = Field(10.0, ge=0, le=200)
    hypothesis: dict[str, Any] | None = None
    permission: Literal["auto", "manual", "accept_edits", "plan", "bypass"] = "auto"
    folder: str = Field("", max_length=40)
    market_auto: bool = Field(False, description="标的与周期交给 AI 根据对话决定")
    suggest: bool = True


class ProjectPatch(BaseModel):
    title: str | None = Field(None, max_length=60)
    idea: str | None = Field(None, max_length=4000)
    securities: list[str] | None = None
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"] | None = None
    model: str | None = None
    effort: Effort | None = None
    goals: Goals | None = None
    max_rounds: int | None = Field(None, ge=1, le=8)
    bars_limit: int | None = Field(None, ge=200, le=20_000)
    data_source: str | None = Field(None, pattern=DATA_SOURCE)
    archived: bool | None = None
    horizon: int | None = Field(None, ge=1, le=60)
    executor: Literal["solo", "team"] | None = None
    team_id: str | None = None
    cost_bps: float | None = Field(None, ge=0, le=200)
    permission: Literal["auto", "manual", "accept_edits", "plan", "bypass"] | None = Field(
        None, description="权限模式：自动 / 手动（改动与运行先问）/ 接受编辑 / 计划 / 跳过一切确认"
    )
    pinned: bool | None = Field(None, description="置顶")
    folder: str | None = Field(None, max_length=40, description="所属项目（文件夹）；空串表示不归入项目")
    auto_compact: bool | None = Field(None, description="上下文超出原文预算时自动压缩较早的对话")


IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 单张解码后上限，与 Anthropic 的单图限制一致


class ImageIn(BaseModel):
    data_url: str = Field(..., max_length=MAX_IMAGE_BYTES * 4 // 3 + 100)  # data:image/png;base64,...
    name: str = Field("", max_length=200)

    @field_validator("data_url")
    @classmethod
    def _check(cls, v: str) -> str:
        try:
            mime, data = split_data_url(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        if mime not in IMAGE_TYPES:
            raise ValueError("只支持 PNG / JPEG / GIF / WebP 图片")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("图片 base64 无法解码") from None
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError(f"单张图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")
        return v


class MessageIn(BaseModel):
    text: str = Field("", max_length=8000)
    images: list[ImageIn] = Field(default_factory=list, max_length=MAX_IMAGES)
    model: str | None = None  # 底栏切换的模型 / 努力程度：随消息一起提交，并记到项目上
    effort: Effort | None = None
    suggest: bool | None = Field(None, description="回答末尾是否附简短的后续建议（设置里的开关）")
    wait: bool = False

    @model_validator(mode="after")
    def _not_empty(self) -> MessageIn:
        if not self.text.strip() and not self.images:
            raise ValueError("消息不能为空")
        return self


class AutopilotIn(BaseModel):
    rounds: int | None = Field(None, ge=1, le=8)
    research: Literal["none", "market", "team"] = "none"
    team_id: str | None = None
    goals: Goals | None = None
    wait: bool = False


class DecisionIn(BaseModel):
    choice: str = Field(..., min_length=1, max_length=4000, description="点选的选项，或研究者自己写的回答")
    note: str = Field("", max_length=4000)
    wait: bool = False


class CodeIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=200_000)
    note: str = "手动编辑"
    wait: bool = False


class WaitIn(BaseModel):
    wait: bool = False
    cost_scale: float = Field(1.0, ge=0.5, le=5)


# ---------------------------------------------------------------------------
# 平台能力 → 策略室工具
# ---------------------------------------------------------------------------
def _platform_backtest(project: dict[str, Any], code: str, *, cost_scale: float = 1.0, label: str = "") -> dict:
    from .routes_research import run_backtest_endpoint
    from .schemas import ResearchBacktestIn

    payload = ResearchBacktestIn(
        code=code,
        name=project.get("title") or "策略室",
        lang="python",
        securities=project["securities"],
        timeframe=project["timeframe"],
        bars_limit=project.get("bars_limit") or 1500,
        start=project.get("start"),
        end=project.get("end"),
        taker_fee=0.001 * cost_scale,
        slippage_bps=5.0 * cost_scale,
        spread_bps=2.0 * cost_scale,
        source=project.get("data_source") or "auto",
        persist=True,
        label=label[:60],
        wait_seconds=120,
    )
    try:
        out = run_backtest_endpoint(payload, SimpleNamespace(headers={}))
    except AppError as exc:
        return {"ok": False, "error": exc.message}
    except HTTPException as exc:
        return {"ok": False, "error": str(exc.detail)}
    if isinstance(out, JSONResponse):
        return {"ok": False, "error": "回测已排队但 worker 还没有执行（请确认 qagent worker 在运行）"}
    result = out.get("result") or {}
    eq = result.get("equity_curve") or []
    step = max(1, len(eq) // 80)
    return {
        "ok": True,
        "bt_id": out.get("id"),
        "run_id": out.get("run_id"),
        "metrics": summarize_metrics(result.get("metrics") or {}),
        "equity": [round(float(v), 2) for v in eq[::step]],
        "benchmark": [round(float(v), 2) for v in (result.get("benchmark_curve") or [])[::step]],
        "data_sources": sorted({d.get("source_label") or d.get("source") for d in result.get("datasets") or []}),
        "warnings": (result.get("warnings") or [])[:3],
        "errors": (result.get("errors") or [])[:3],
        "status": result.get("status"),
    }


def _team(topic: str, *, team_id: str | None = None, context: str = "") -> dict:
    s = get_services()
    team = s.teams.get_team(team_id or "review_board") or s.teams.default_team()
    return s.teams.run(team, topic, context=context).to_dict()


def _discuss(project: dict[str, Any], topic: str) -> dict:
    """AI 发起 /discuss：建一场团队讨论（后台进行），发言挂在对话里。"""
    from .routes_missions import MissionIn, _materials, _runner

    s = get_services()
    teams = s.teams.list_teams()
    payload = MissionIn(
        directive=topic,
        mode="single",
        team_id=project.get("team_id") or (teams[0].id if teams else None),
        lab_project_id=project["id"],
        symbol=(project.get("securities") or ["BTC/USDT"])[0],
        timeframe=project.get("timeframe") or "1h",
    )
    r = _runner()
    m = r.create(payload.model_dump(), _materials(payload))
    r.start(m["id"], wait=False)
    return {"mission_id": m["id"]}


def _night(project: dict[str, Any], idea: str) -> dict:
    """AI 发起 /night：放进夜班队列。"""
    from .routes_rd import NS, _store, _user

    task = NS.add_task(
        _store(),
        user=_user(),
        kind=project.get("kind") or "strategy",
        idea=idea,
        executor=project.get("executor") or "solo",
        symbols=project.get("securities") or [],
        timeframe=project.get("timeframe"),
        rounds=min(8, int(project.get("max_rounds") or 4)),
        data_source=project.get("data_source") or "auto",
    )
    return {"task_id": task.get("id")}


def _save_strategy(project: dict[str, Any]) -> dict:
    from ..strategies.repo import StrategyRepository

    s = get_services()
    repo = StrategyRepository(s.store.root / "strategies")
    code = project["code"]
    slug = project.get("slug") if project.get("slug") and repo.exists(project["slug"]) else None
    name = project.get("title") or "AI 策略"
    rec = repo.save(
        name=name,
        source=code,
        params=declared_params(code),
        team="AI 策略室",
        kind="code",
        origin="agent",
        symbol=project["securities"][0],
        timeframe=project["timeframe"],
        securities=project["securities"],
        notes=f"AI 策略室项目「{name}」v{project.get('current_v')}：{(project.get('idea') or '')[:160]}",
        slug=slug or repo.unique_slug(name),
    )
    return {"slug": rec.meta.slug, "name": rec.meta.name, "version": rec.meta.version}


def _lab() -> StrategyLab:
    s = get_services()
    tools = LabTools(
        backtest=_platform_backtest,
        market=lambda sym, tf: s.market_summary(sym, tf),
        templates=s.search_templates,
        team=_team,
        save_strategy=_save_strategy,
        registry=s.teams.tools,
        discuss=_discuss,
        night=_night,
    )
    return StrategyLab(s.router, LabStore(s.store), tools)


def _datahub() -> DataHub:
    s = get_services()
    return DataHub(s.store, secrets_fn=s.read_secrets)


def _factor_deps() -> FactorDeps:  # type: ignore[valid-type]
    s = get_services()
    hub = _datahub()
    return FactorDeps(
        panel=lambda symbols, tf, **kw: build_panel(s.market, hub, symbols, tf, **kw),
        library=lambda: FactorLibrary(s.store),
    )


def _factor_lab(base: StrategyLab | None = None) -> FactorLab:
    if not FACTOR_READY:
        raise HTTPException(503, NUMPY_HINT)
    base = base or _lab()
    return FactorLab(base.router, base.store, base.tools, _factor_deps())


def _model_lab(base: StrategyLab | None = None) -> ModelLab:
    if not FACTOR_READY:
        raise HTTPException(503, NUMPY_HINT)
    base = base or _lab()
    s = get_services()
    hub = _datahub()
    deps = ModelDeps(
        panel=lambda symbols, tf, **kw: build_panel(s.market, hub, symbols, tf, **kw),
        library=lambda: ModelLibrary(s.store),
        factors=lambda: FactorLibrary(s.store),
    )
    return ModelLab(base.router, base.store, base.tools, deps)


def _portfolio_lab(base: StrategyLab | None = None) -> PortfolioLab:
    base = base or _lab()
    s = get_services()
    hub = _datahub()
    deps = PortfolioDeps(
        panel=lambda symbols, tf, **kw: build_panel(s.market, hub, symbols, tf, **kw),
        library=lambda: PortfolioLibrary(s.store),
        store=s.store,
    )
    return PortfolioLab(base.router, base.store, base.tools, deps)


def _kind_lab(kind: str, base: StrategyLab | None = None) -> StrategyLab:
    if kind == "factor":
        return _factor_lab(base)
    if kind == "model":
        return _model_lab(base)
    if kind in DOMAIN_LABS:
        if not FACTOR_READY:
            raise HTTPException(503, NUMPY_HINT)
        return DOMAIN_LABS[kind](base or _lab())
    return base or _lab()


def _execution_lab(base: StrategyLab | None = None) -> ExecutionLab:
    base = base or _lab()
    s = get_services()

    def tapes(symbols: list[str], *, source: str = "auto", steps: int = 3000, recording_id: str | None = None) -> Any:
        return load_tapes(
            s.market,
            ExecutionLibrary(s.store),
            symbols,
            source="kline" if source == "exchange" else source,
            steps=steps,
            recording_id=recording_id,
        )

    return ExecutionLab(
        base.router, base.store, base.tools, ExecutionDeps(tapes=tapes, library=lambda: ExecutionLibrary(s.store))
    )


DOMAIN_LABS: dict[str, Any] = {"portfolio": _portfolio_lab, "execution": _execution_lab}


def _lab_for(pid: str) -> StrategyLab:
    """按项目的研究类型选研究室：因子 / 模型 / 组合 / 执行项目走各自的研究室，其余走策略研究。"""
    lab = _lab()
    p = lab.store.get(pid)
    if p is not None and p.get("kind") not in (None, "strategy"):
        return _kind_lab(p["kind"], lab)
    return lab


def _project_or_404(lab: StrategyLab, pid: str) -> dict[str, Any]:
    p = lab.store.get(pid)
    if p is None:
        raise HTTPException(404, "策略项目不存在")
    return p


def _brief(p: dict[str, Any]) -> dict[str, Any]:
    versions = p.get("versions") or []
    cur = next((v for v in versions if v["v"] == p.get("current_v")), None)
    return {
        **{k: v for k, v in p.items() if k not in ("versions", "code", "pins")},
        "versions_count": len(versions),
        "metrics": (cur or {}).get("metrics"),
        "passed": ((cur or {}).get("evaluation") or {}).get("passed"),
    }


def _view(lab: StrategyLab, p: dict[str, Any], after: int = 0, since: float = 0.0) -> dict[str, Any]:
    import time

    now = time.time()
    return {
        "project": p,
        "messages": lab.store.messages(p["id"], after=after, since=since),
        "context": lab.context_view(p),
        "server_time": now,
    }


def _start(lab: StrategyLab, pid: str, kind: str, fn, wait: bool) -> dict:
    try:
        lab.start(pid, kind, fn, wait=wait)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(lab, _project_or_404(lab, pid))


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("/meta", summary="策略室元信息：可选模型、团队、默认验收目标、模型是否就绪")
def lab_meta() -> dict:
    s = get_services()
    cfg = s.router.config
    ready = any(s.router._has_credentials(p, cfg) for p in cfg.providers)
    current = getattr(s.router, "current_target", None)
    return {
        "models": s.router.available_models(),
        "routing": cfg.routing,
        "default_model": current("code_gen") if callable(current) else "",
        "efforts": [
            {"id": "low", "label": "低", "hint": "快速回答，少想"},
            {"id": "medium", "label": "中", "hint": "大多数任务"},
            {"id": "high", "label": "高", "hint": "复杂策略与排错"},
            {"id": "max", "label": "极高", "hint": "最深入，最慢"},
        ],
        "llm_ready": ready,
        "teams": [{"id": t.id, "name": t.name, "mode": t.mode, "roles": len(t.role_ids)} for t in s.teams.list_teams()],
        "default_goals": DEFAULT_GOALS,
        "goal_metrics": G.catalog(),
        "goal_templates": G.TEMPLATES,
        "search": s.websearch.status() if s.websearch else {"configured": False},
        "templates": [{"id": t.id, "name": t.name} for t in TEMPLATES],
        "domains": {k: v.meta() for k, v in RG.DOMAINS.items()},
        "kinds": [
            {"id": "strategy", "label": "策略", "hint": "写策略代码并回测"},
            {"id": "factor", "label": "因子", "hint": "挖掘有信息量的因子，按 IC 与样本外评估"},
            {"id": "model", "label": "模型", "hint": "用因子做特征训练预测模型，滚动样本外评估"},
            *(
                [{"id": "portfolio", "label": "组合", "hint": "把预测变成权重，控制风险"}]
                if "portfolio" in RG.DOMAINS
                else []
            ),
            *(
                [{"id": "execution", "label": "执行", "hint": "拆单算法与成本研究（仿真）"}]
                if "execution" in RG.DOMAINS
                else []
            ),
        ],
        "executors": [{"id": "solo", "label": "单个 AI"}, {"id": "team", "label": "研究小组"}],
        "universes": [{"id": k, "name": v["name"], "symbols": v["symbols"]} for k, v in UNIVERSES.items()],
    }


@router.get("/projects", summary="策略项目列表")
def list_projects(archived: bool = False) -> dict:
    lab = _lab()
    return {"projects": [_brief(p) for p in lab.store.list() if bool(p.get("archived")) == archived]}


@router.post("/projects", summary="新建策略项目（可带初始代码、模板或钉住的上下文）")
def create_project(payload: ProjectIn) -> dict:
    lab = _lab()
    data = payload.model_dump()
    if payload.kind == "factor":
        flab = _factor_lab(lab)
        p = lab.store.save(new_factor_project(data))
        for pin in payload.pins:
            flab.pin(p["id"], kind=pin.kind, title=pin.title, text=pin.text)
        lab.store.append(
            p["id"],
            {
                "role": "event",
                "kind": "text",
                "content": "因子研究已创建。"
                + (f"想法：{payload.idea[:200]}" if payload.idea else "可以描述一个市场现象，或直接输入表达式。"),
            },
        )
        if payload.code:
            from ..research import factor_expr as FX

            try:
                expr = str(FX.parse(payload.code))
            except FX.FactorExprError as exc:
                lab.store.delete(p["id"])
                raise HTTPException(400, f"表达式有误：{exc}") from exc
            flab.add_version(p["id"], expr, source="user", note="初始表达式", hypothesis=payload.hypothesis)
        return _view(flab, _project_or_404(flab, p["id"]))
    if payload.kind in ("model", "portfolio", "execution"):
        from ..agents.research_lab import dumps_spec, loads_spec

        dlab = _kind_lab(payload.kind, lab)
        p = lab.store.save(dlab.new_project(data))  # type: ignore[attr-defined]
        for pin in payload.pins:
            dlab.pin(p["id"], kind=pin.kind, title=pin.title, text=pin.text)
        lab.store.append(
            p["id"],
            {
                "role": "event",
                "kind": "text",
                "content": f"{dlab.domain.label}研究已创建。"
                + (  # type: ignore[attr-defined]
                    f"想法：{payload.idea[:200]}" if payload.idea else dlab.created_hint
                ),
            },
        )  # type: ignore[attr-defined]
        if payload.code:
            try:
                art = dlab.normalize(p, loads_spec(payload.code))  # type: ignore[attr-defined]
            except Exception as exc:
                lab.store.delete(p["id"])
                raise HTTPException(400, f"规格有误：{exc}") from exc
            dlab.add_version(p["id"], dumps_spec(art), source="user", note="初始规格")
        return _view(dlab, _project_or_404(dlab, p["id"]))
    data["goals"] = {k: v for k, v in (payload.goals.model_dump() if payload.goals else {}).items() if v is not None}
    p = lab.store.save(new_project(data))
    code = payload.code
    if not code and payload.template_id:
        tpl = get_template(payload.template_id)
        if tpl is None:
            raise HTTPException(404, "模板不存在")
        code = tpl.code
    if code:
        lab.add_version(
            p["id"],
            code,
            source="user" if payload.code else "template",
            note="初始代码" if payload.code else f"从模板「{payload.template_id}」开始",
        )
    for pin in payload.pins:
        lab.pin(p["id"], kind=pin.kind, title=pin.title, text=pin.text)
    if payload.idea:
        lab.store.append(
            p["id"], {"role": "event", "kind": "text", "content": f"项目已创建。想法：{payload.idea[:200]}"}
        )
    return _view(lab, _project_or_404(lab, p["id"]))


@router.get("/projects/{pid}", summary="项目详情与增量消息（after=已收到的最大序号，since=上次的 server_time）")
def get_project(pid: str, after: int = Query(0, ge=0), since: float = Query(0.0, ge=0)) -> dict:
    lab = _lab_for(pid)
    return _view(lab, _project_or_404(lab, pid), after, since)


@router.patch("/projects/{pid}", summary="修改项目：标题、模型、验收目标、标的、归档")
def patch_project(pid: str, payload: ProjectPatch) -> dict:
    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    changes = payload.model_dump(exclude_none=True)
    if "goals" in changes:
        changes["goals"] = {**p.get("goals", DEFAULT_GOALS), **payload.goals.model_dump()}  # type: ignore[union-attr]
        if (goal := G.merge_legacy(p, changes["goals"])) is not None:
            changes["goal"] = goal
    if "securities" in changes:
        changes["securities"] = [x.strip().upper() for x in changes["securities"] if x.strip()] or p["securities"]
    lab.store.patch(pid, **changes)
    return {"project": _project_or_404(lab, pid)}


@router.delete("/projects/{pid}", summary="删除策略项目（已保存的平台策略不受影响）")
def delete_project(pid: str) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    return {"ok": lab.store.delete(pid)}


@router.post("/projects/{pid}/messages", summary="发送一条消息：模型带工具协作（/回测 /解释 /团队 /迭代 为快捷指令）")
def send_message(pid: str, payload: MessageIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    if lab.store.get(pid).get("status") == "running":  # type: ignore[union-attr]
        raise HTTPException(409, "这个项目正在运行，请等它结束或先停止")
    if payload.images:
        _check_image_input(lab, pid, payload.model)
    changes = {
        k: v
        for k, v in {"model": payload.model, "effort": payload.effort, "suggest": payload.suggest}.items()
        if v is not None
    }
    if changes:
        lab.store.patch(pid, **changes)
    refs = [lab.store.put_image(pid, im.data_url, name=im.name) for im in payload.images]
    lab.store.append(
        pid, {"role": "user", "kind": "text", "content": payload.text, "data": {"images": refs} if refs else {}}
    )
    if refs:
        urls = [im.data_url for im in payload.images]
        return _start(lab, pid, "chat", lambda: lab.chat(pid, payload.text, images=urls), payload.wait)
    return _start(lab, pid, "chat", lambda: lab.chat(pid, payload.text), payload.wait)


def _check_image_input(lab: StrategyLab, pid: str, model: str | None) -> None:
    """图片只发给目录里勾选了「图片」输入的模型；其它研究室的对话暂不支持图片。"""
    if "images" not in inspect.signature(lab.chat).parameters:
        raise HTTPException(400, "这类研究项目的对话暂不支持图片，请在策略研究里使用")
    target = model or lab.model_target(lab.store.get(pid) or {})
    listing = lab.router.available_models() if hasattr(lab.router, "available_models") else []
    info = next((m for m in listing if m.get("target") == target), None)
    if info is None or "image" not in (info.get("inputs") or []):
        raise HTTPException(
            400,
            f"当前模型 {target or '（未选择）'} 未开启图片输入："
            "请在 设置 → 模型 里为它勾选「图片」，或换一个支持图片的模型",
        )


@router.get("/projects/{pid}/images/{iid}", summary="读取对话里附带的图片")
def get_image(pid: str, iid: str) -> Response:
    lab = _lab_for(pid)
    blob = lab.store.get_image(pid, iid)
    if blob is None:
        raise HTTPException(404, "图片不存在")
    _, data = split_data_url(blob["url"])
    return Response(
        base64.b64decode(data), media_type=blob["mime"], headers={"Cache-Control": "private, max-age=86400"}
    )


class GoalItemIn(BaseModel):
    metric: str
    op: Literal[">=", "<="] | None = None
    target: float
    priority: Literal["must", "try"] = "must"


class GoalSpecIn(BaseModel):
    items: list[GoalItemIn] | None = None
    symbols: list[str] | None = None
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"] | None = None
    start: int | str | None = None
    end: int | str | None = None
    bars_limit: int | None = Field(None, ge=200, le=20_000)
    max_rounds: int | None = Field(None, ge=1, le=8)
    note: str | None = Field(None, max_length=400)
    horizon: int | None = Field(None, ge=1, le=60)


class GoalTextIn(BaseModel):
    text: str = Field("", max_length=2000)
    project_id: str | None = None
    domain: DomainKey | None = None


class GoalCheckIn(BaseModel):
    spec: GoalSpecIn
    project_id: str | None = None
    domain: DomainKey | None = None
    review: bool = False  # true：规则检查之外再让模型复核


class GoalSetIn(BaseModel):
    spec: GoalSpecIn
    review: bool = True
    start: bool = False  # 设定后立即开始自动迭代
    research: Literal["none", "market", "team"] = "none"
    wait: bool = False


def _spec_payload(spec: GoalSpecIn) -> dict[str, Any]:
    """只带上调用方给出的字段；start / end 显式传 null 表示清空区间（改回按 K 线数回看）。"""
    return {
        k: v
        for k, v in spec.model_dump().items()
        if v is not None or (k in ("start", "end") and k in spec.model_fields_set)
    }


@router.post(
    "/goal/parse", summary="解析 /goal 文本：把“夏普>1.2 回撤<15% 4h 近 2 年”转成结构化目标（未出现的字段沿用项目现值）"
)
def parse_goal(payload: GoalTextIn) -> dict:
    lab = _lab()
    project = lab.store.get(payload.project_id) if payload.project_id else None
    dkind = (project or {}).get("kind") or payload.domain
    if dkind in ("model", "portfolio", "execution"):
        dlab = _kind_lab(dkind, lab)
        base = dlab.project_spec(project) if project else dlab.domain.normalize_spec({})  # type: ignore[attr-defined]
        parsed = dlab.domain.parse_text(payload.text)  # type: ignore[attr-defined]
        spec = dlab.domain.normalize_spec(parsed["spec"], base)  # type: ignore[attr-defined]
        return {
            "spec": spec,
            "parsed_fields": sorted(parsed["spec"]),
            "unparsed": parsed["unparsed"],
            "check": dlab.check_goal(project or {"securities": spec["symbols"]}, spec),
            "domain": dkind,
        }  # type: ignore[attr-defined]
    if payload.domain == "factor" or (project or {}).get("kind") == "factor":
        flab = _factor_lab(lab)
        base = FL.project_spec(project) if project else RG.FACTOR.normalize_spec({})
        parsed = RG.FACTOR.parse_text(payload.text)
        spec = RG.FACTOR.normalize_spec(parsed["spec"], base)
        return {
            "spec": spec,
            "parsed_fields": sorted(parsed["spec"]),
            "unparsed": parsed["unparsed"],
            "check": flab.check_goal(project or {}, spec),
            "domain": "factor",
        }
    base = G.project_spec(lab.store.get(payload.project_id) or {}) if payload.project_id else G.normalize_spec({})
    parsed = G.parse_goal_text(payload.text)
    spec = G.normalize_spec(parsed["spec"], base)
    return {
        "spec": spec,
        "parsed_fields": sorted(parsed["spec"]),
        "unparsed": parsed["unparsed"],
        "check": G.check_spec(spec),
    }


@router.post("/goal/check", summary="检查目标是否合理：返回结论与字段级建议值（可选让模型复核）")
def check_goal(payload: GoalCheckIn) -> dict:
    lab = _lab()
    project = lab.store.get(payload.project_id) if payload.project_id else None
    dkind = (project or {}).get("kind") or payload.domain
    if dkind in ("model", "portfolio", "execution"):
        dlab = _kind_lab(dkind, lab)
        base = dlab.project_spec(project) if project else dlab.domain.normalize_spec({})  # type: ignore[attr-defined]
        spec = dlab.domain.normalize_spec(_spec_payload(payload.spec), base)  # type: ignore[attr-defined]
        check = dlab.check_goal(project or {"securities": spec["symbols"]}, spec)  # type: ignore[attr-defined]
        return {
            "spec": spec,
            "check": check,
            "suggested": dlab.domain.apply_suggestions(spec, check),  # type: ignore[attr-defined]
            "domain": dkind,
        }
    if payload.domain == "factor" or (project or {}).get("kind") == "factor":
        flab = _factor_lab(lab)
        base = FL.project_spec(project) if project else RG.FACTOR.normalize_spec({})
        spec = RG.FACTOR.normalize_spec(_spec_payload(payload.spec), base)
        check = flab.check_goal(project or {}, spec)
        return {"spec": spec, "check": check, "suggested": RG.FACTOR.apply_suggestions(spec, check), "domain": "factor"}
    spec = G.normalize_spec(_spec_payload(payload.spec), G.project_spec(project) if project else G.normalize_spec({}))
    history = [v["metrics"] for v in (project or {}).get("versions") or [] if v.get("metrics")]
    check = G.check_spec(spec, history=history)
    if payload.review and project:
        check = lab.review_goal(project, spec, check)
    return {"spec": spec, "check": check, "suggested": G.apply_suggestions(spec, check)}


@router.post(
    "/projects/{pid}/goal", summary="设定项目目标（/goal）：写入条件与回测范围，做合理性检查；可选立即开始自动迭代"
)
def set_goal(pid: str, payload: GoalSetIn) -> dict:
    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    if p.get("status") == "running":
        raise HTTPException(409, "项目正在运行，请先停止")
    out = lab.set_goal(pid, _spec_payload(payload.spec), source="user", review=payload.review)
    if payload.start:
        view = _start(
            lab, pid, "autopilot", lambda: lab.autopilot(pid, research=payload.research, force=True), payload.wait
        )
        return {**view, "goal": out}
    return {**_view(lab, _project_or_404(lab, pid)), "goal": out}


class WalkForwardIn(BaseModel):
    segments: int = Field(3, ge=2, le=6)
    wait: bool = False


@router.post("/projects/{pid}/walkforward", summary="分段回测：把回测区间等分几段分别回测，检查策略是否只在某一段有效")
def walk_forward(pid: str, payload: WalkForwardIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    return _start(lab, pid, "walkforward", lambda: lab.walk_forward(pid, segments=payload.segments), payload.wait)


@router.post("/projects/{pid}/compact", summary="压缩对话历史：把之前的对话总结成摘要钉进上下文，之后不再发原文")
def compact_history(pid: str) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    out = lab.compact(pid)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "无法压缩")
    return _view(lab, _project_or_404(lab, pid))


@router.get("/projects/{pid}/events", summary="实时推送（SSE）：增量消息与项目状态，回合结束后自动关闭")
def project_events(
    pid: str, after: int = Query(0, ge=0), since: float = Query(0.0, ge=0), timeout: float = Query(120, ge=5, le=600)
) -> StreamingResponse:
    import asyncio
    import json as _json
    import time

    from ..platform import context

    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    ctx = context.current()  # 生成器在响应阶段运行，显式带上本次请求的工作区身份

    async def gen():
        context.set_context(ctx)
        last_seq, last_time, idle_ticks = after, since, 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            now = time.time()
            p = lab.store.get(pid)
            if p is None:
                yield "event: gone\ndata: {}\n\n"
                return
            msgs = lab.store.messages(pid, after=last_seq, since=last_time)
            payload = {"project": p, "messages": msgs, "server_time": now}
            if msgs:
                payload["context"] = lab.context_view(p)
            yield f"data: {_json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
            if msgs:
                last_seq = max(last_seq, *(m["seq"] for m in msgs))
            last_time = now
            idle_ticks = idle_ticks + 1 if (p.get("status") != "running" and not msgs) else 0
            if idle_ticks >= 2:
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(0.25 if p.get("status") == "running" else 0.5)

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.post("/projects/{pid}/autopilot", summary="自动迭代：调研 → 生成 → 回测 → 对照目标改进，结束后询问是否满意")
def start_autopilot(pid: str, payload: AutopilotIn) -> dict:
    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    if payload.goals:
        goals = {**p.get("goals", DEFAULT_GOALS), **payload.goals.model_dump()}
        goal = G.merge_legacy(p, goals)
        lab.store.patch(pid, goals=goals, goal_set=True, **({"goal": goal} if goal is not None else {}))
    elif not lab.has_goal(p):
        raise HTTPException(400, "还没有设定目标：先用 /goal 设定目标再自动迭代；没有目标时直接对话即可")
    return _start(
        lab,
        pid,
        "autopilot",
        lambda: lab.autopilot(pid, rounds=payload.rounds, research=payload.research, team_id=payload.team_id),
        payload.wait,
    )


@router.post("/projects/{pid}/decision", summary="回答 AI 的提问：满意（保存为策略）/ 继续优化 / 换个方向")
def decide(pid: str, payload: DecisionIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    return _start(lab, pid, "decision", lambda: lab.decide(pid, payload.choice, payload.note), payload.wait)


class DiscussionIn(BaseModel):
    mission_id: str = Field(..., max_length=40)
    topic: str = Field("", max_length=400)


@router.post("/projects/{pid}/discussion", summary="在对话里挂一张团队讨论卡片（/discuss 发起的讨论，发言以气泡展示）")
def attach_discussion(pid: str, payload: DiscussionIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    lab.store.append(
        pid,
        {
            "role": "event",
            "kind": "discussion",
            "content": payload.topic or "团队讨论",
            "data": {"mission_id": payload.mission_id},
        },
    )
    return _view(lab, _project_or_404(lab, pid))


class ForkIn(BaseModel):
    seq: int | None = Field(None, ge=1, description="从这条消息（含）分叉；不填则复制整段对话")
    title: str = Field("", max_length=60)


class RewindIn(BaseModel):
    seq: int = Field(..., ge=1, description="要编辑 / 重试的用户消息序号")
    text: str | None = Field(None, max_length=8000, description="编辑后的内容；不填则按原文重试")
    wait: bool = False


@router.post("/projects/{pid}/fork", summary="从某条消息分叉出一个新对话（挂在原对话下）")
def fork_project(pid: str, payload: ForkIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    p = lab.fork(pid, seq=payload.seq, title=payload.title)
    return {"project": _brief(p)}


@router.post("/projects/{pid}/rewind", summary="编辑或重试一条用户消息：删掉它之后的对话，用新内容重新发送")
def rewind_project(pid: str, payload: RewindIn) -> dict:
    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    if p.get("status") == "running":
        raise HTTPException(409, "这个对话正在运行，请等它结束或先停止")
    try:
        old = lab.rewind(pid, payload.seq)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    text = (payload.text if payload.text is not None else old["content"]).strip()
    refs = list((old.get("data") or {}).get("images") or [])
    if not text and not refs:
        raise HTTPException(400, "消息不能为空")
    edited = payload.text is not None
    data: dict[str, Any] = {"edited": edited, **({"images": refs} if refs else {})}
    lab.store.append(pid, {"role": "user", "kind": "text", "content": text, "data": data})
    if refs:  # 编辑 / 重试时附图一起重新发给模型
        urls = [b["url"] for b in (lab.store.get_image(pid, r["id"]) for r in refs) if b]
        return _start(lab, pid, "chat", lambda: lab.chat(pid, text, images=urls), payload.wait)
    return _start(lab, pid, "chat", lambda: lab.chat(pid, text), payload.wait)


class PlanIn(BaseModel):
    text: str = Field("", max_length=2000, description="要完成的任务；不填则继续执行当前计划")
    wait: bool = False


@router.post("/projects/{pid}/plan", summary="/plan：AI 列分步计划并一步步执行，直到全部完成且目标达标")
def run_plan(pid: str, payload: PlanIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    return _start(lab, pid, "plan", lambda: lab.run_plan(pid, payload.text.strip()), payload.wait)


@router.post("/projects/{pid}/stop", summary="请求停止正在运行的回合（在下一个检查点停下）")
def stop_project(pid: str) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    lab.store.patch(pid, stop_requested=True)
    return {"ok": True}


@router.post("/projects/{pid}/backtest", summary="用平台回测跑当前版本（cost_scale>1 为成本压力测试）")
def backtest_project(pid: str, payload: WaitIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    return _start(lab, pid, "backtest", lambda: lab.backtest_current(pid, cost_scale=payload.cost_scale), payload.wait)


@router.post("/projects/{pid}/code", summary="保存手动编辑的代码为新版本（先做沙箱试跑）")
def save_code(pid: str, payload: CodeIn) -> dict:
    from ..agents.copilot import smoke_test
    from ..research import factor_expr as FX

    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    if isinstance(lab, DomainLab):
        from ..agents.research_lab import loads_spec

        try:
            lab.normalize(p, loads_spec(payload.code))
        except Exception as exc:
            raise HTTPException(400, f"规格有误：{exc}") from exc
        return _start(
            lab, pid, "evaluate", lambda: lab.submit(pid, payload.code, note=payload.note, source="user"), payload.wait
        )
    if isinstance(lab, FactorLab):
        try:
            FX.parse(payload.code)
        except FX.FactorExprError as exc:
            raise HTTPException(400, f"表达式有误：{exc}") from exc
        return _start(
            lab,
            pid,
            "evaluate",
            lambda: lab.submit_factor(pid, payload.code, note=payload.note, source="user"),
            payload.wait,
        )
    checks = smoke_test(payload.code, p["securities"], p["timeframe"])
    if not checks.get("ok"):
        problems = [*checks.get("issues", []), *checks.get("errors", [])]
        raise HTTPException(400, "代码没有通过检查：" + "；".join(problems[:4]))
    before = p.get("code") or ""
    row = lab.add_version(pid, payload.code, source="user", note=payload.note, checks=checks)
    lab.store.append(
        pid,
        {
            "role": "user",
            "kind": "code",
            "content": payload.note,
            "data": {"v": row["v"], "before": before, "after": payload.code, "params": row["params"]},
        },
    )
    return _view(lab, _project_or_404(lab, pid))


@router.post("/projects/{pid}/versions/{v}/restore", summary="切换到历史版本（不删除任何版本）")
def restore_version(pid: str, v: int) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    if lab.restore(pid, v) is None:
        raise HTTPException(404, "版本不存在")
    return _view(lab, _project_or_404(lab, pid))


@router.post("/projects/{pid}/pins", summary="把一段材料钉进模型上下文（调研结论、笔记、回测解读）")
def add_pin(pid: str, payload: PinIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    item = lab.pin(pid, kind=payload.kind, title=payload.title, text=payload.text)
    return {"pin": item, "context": lab.context_view(_project_or_404(lab, pid))}


@router.delete("/projects/{pid}/pins/{pin_id}", summary="把材料移出模型上下文")
def remove_pin(pid: str, pin_id: str) -> dict:
    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    lab.store.patch(pid, pins=[x for x in p.get("pins") or [] if x["id"] != pin_id])
    return {"ok": True, "context": lab.context_view(_project_or_404(lab, pid))}


@router.post("/projects/{pid}/save", summary="把当前版本保存为平台策略草稿（可在编辑器、回测、模拟中继续使用）")
def save_as_strategy(pid: str) -> dict:
    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    if not p.get("code"):
        raise HTTPException(400, "项目还没有代码")
    if isinstance(lab, FactorLab):
        rec = lab.save_to_library(pid, status="active")
        if rec is None:
            raise HTTPException(400, "因子还没有评估，先评估再入库")
        return {"factor": rec}
    if isinstance(lab, DomainLab):
        rec = lab.save_to_library(pid, status="active")
        if rec is None:
            raise HTTPException(400, "还没有评估，先评估再保存")
        return {lab.kind: rec}
    rec = _save_strategy(p)
    lab.store.patch(pid, slug=rec["slug"])
    lab.store.append(pid, {"role": "event", "kind": "saved", "content": "已保存为策略草稿", "data": rec})
    return {"strategy": rec}
