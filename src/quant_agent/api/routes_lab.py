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
import time
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from ..agents import lab_goal as G
from ..agents.lab_state import MAX_INSTRUCTIONS_CHARS, normalize_context
from ..agents.research_profile import clamp_instructions
from ..agents.strategy_lab import DEFAULT_GOALS, LabStore, LabTools, StrategyLab, new_project
from ..agents.team import LAB_REVIEW_TEAM
from ..engine.research import summarize_metrics
from ..llm.base import split_data_url
from ..platform.errors import AppError, ErrorCode
from ..research import goals as RG
from ..sdk import declared_params
from ..store import Collections
from ..strategies.sdk_templates import TEMPLATES, get_template
from ..timeframes import TimeframeStr
from .deps import get_services
from .runtime_schemas import EffectResolutionIn, ModelUsageResolutionIn

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
    # 文件类钉住材料（上传的 txt、超长消息自动转成的文件）最多 40 万字；上下文里只放开头，Agent 用 read_pinned_file 分段读
    text: str = Field(..., min_length=1, max_length=400_000)


LabKind = Literal["strategy", "factor", "model", "portfolio", "execution", "research", "workflow"]
DomainKey = Literal["strategy", "factor", "model", "portfolio", "execution"]


class ProjectIn(BaseModel):
    kind: LabKind = "strategy"
    title: str = ""
    idea: str = Field("", max_length=20_000)
    securities: list[str] = Field(default_factory=list)
    timeframe: TimeframeStr = "1h"
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
    # 通用会话（研究 / 工作流升级）：只保存来源引用，不生成策略代码与验收目标
    context: dict[str, Any] = Field(default_factory=dict, description="来源引用：工作流 / 运行 / 节点 / 页面资料")
    # 项目级规则（研究规则 / 偏好 / 边界）：注入聊天、计划与团队提示词；存储时截断到 20k
    # 请求体上限放宽到 10 倍，落库时统一截断到 MAX_INSTRUCTIONS_CHARS（20k），避免一次超长就 422
    instructions: str = Field(
        "", max_length=MAX_INSTRUCTIONS_CHARS * 10, description="研究者设定的项目规则（≤20k 字符）"
    )
    permission: Literal["auto", "manual", "accept_edits", "plan", "bypass"] = "auto"
    folder: str = Field("", max_length=40)
    market_auto: bool | None = Field(None, description="标的与周期交给 AI 根据对话决定；省略时按是否提供了标的自动判断")
    suggest: bool = True
    max_tool_calls: int = Field(1000, ge=1, le=5000, description="单轮对话最多调用工具的次数")
    workflow: Literal["", "auto", "pipeline", "report"] = Field("", description="研究流程：自动 / 完整流程 / 调研报告")
    approvals: list[Literal["delete", "trading", "config", "save", "edit", "run"]] | None = None
    auto_team_context: bool = Field(True, description="团队讨论结论自动加入上下文")
    auto_compact_ratio: float = Field(0.8, ge=0.5, le=0.95, description="上下文达到模型窗口的该比例时自动压缩")


class ProjectPatch(BaseModel):
    title: str | None = Field(None, max_length=60)
    idea: str | None = Field(None, max_length=20_000)
    securities: list[str] | None = None
    market_auto: bool | None = Field(None, description="标的交给 AI 根据对话选择")
    timeframe: TimeframeStr | None = None
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
    auto_compact: bool | None = Field(None, description="上下文达到阈值时自动压缩较早的对话")
    auto_compact_ratio: float | None = Field(
        None, ge=0.5, le=0.95, description="自动压缩阈值：模型上下文窗口的比例，默认 0.8"
    )
    max_tool_calls: int | None = Field(None, ge=1, le=5000)
    workflow: Literal["", "auto", "pipeline", "report"] | None = None
    auto_team_context: bool | None = None
    instructions: str | None = Field(
        None, max_length=MAX_INSTRUCTIONS_CHARS * 10, description="研究者设定的项目规则（≤20k 字符，超出截断）"
    )
    approvals: list[Literal["delete", "trading", "config", "save", "edit", "run"]] | None = Field(
        None, description="完全权限下仍要先审批的操作类别"
    )


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
    text: str = Field("", max_length=20_000)
    images: list[ImageIn] = Field(default_factory=list, max_length=MAX_IMAGES)
    model: str | None = None  # 底栏切换的模型 / 努力程度：随消息一起提交，并记到项目上
    effort: Effort | None = None
    suggest: bool | None = Field(None, description="回答末尾是否附简短的后续建议（设置里的开关）")
    max_tool_calls: int | None = Field(None, ge=1, le=5000, description="单轮工具调用上限（设置里的值）")
    auto_team_context: bool | None = None
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
    cost_scale: float = Field(1.0, ge=0.5, le=20)


# ---------------------------------------------------------------------------
# 平台能力 → 策略室工具
# ---------------------------------------------------------------------------
def _platform_backtest(
    project: dict[str, Any],
    code: str,
    *,
    cost_scale: float = 1.0,
    label: str = "",
    execution_mode: str = "bar",
    dynamic_slippage: bool = False,
    vol_slippage: float = 0.25,
    impact_a_bps: float = 0.0,
    impact_b_bps: float = 0.0,
    limit_move_pct: float = 0.0,
    min_liquidity_usd: float = 0.0,
) -> dict:
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
        # 基准成本 + 压力倍数：倍数在撮合时统一生效，策略 initialize 里 set_order_cost 也会被放大
        taker_fee=0.001,
        slippage_bps=5.0,
        spread_bps=2.0,
        cost_scale=cost_scale,
        execution_mode="tick" if execution_mode == "tick" else "bar",
        dynamic_slippage=bool(dynamic_slippage),
        vol_slippage=float(vol_slippage or 0.0),
        impact_a_bps=float(impact_a_bps or 0.0),
        impact_b_bps=float(impact_b_bps or 0.0),
        limit_move_pct=float(limit_move_pct or 0.0),
        min_liquidity_usd=float(min_liquidity_usd or 0.0),
        source=_data_source(project),
        persist=True,
        label=label[:60],
        lab_project_id=project.get("id"),
        lab_title=(project.get("title") or "")[:60],
        wait_seconds=120,
    )
    try:
        out = run_backtest_endpoint(payload, SimpleNamespace(headers={}))
    except AppError as exc:
        return {"ok": False, "error": exc.message}
    except HTTPException as exc:
        detail = str(exc.detail)
        if exc.status_code == 502:  # 真实行情取不到：如实说未接入，提示 Agent 先同步数据
            detail = f"未接入数据：{detail}。可以先调用 sync_market_data 同步该标的行情，或换一个有数据的标的"
        return {"ok": False, "error": detail}
    if isinstance(out, JSONResponse):
        return {"ok": False, "error": "回测已排队但 worker 还没有执行（请确认 qagent worker 在运行）"}
    from ..agents import rigor as R

    result = out.get("result") or {}
    eq = result.get("equity_curve") or []
    step = max(1, len(eq) // 80)
    datasets = [d for d in result.get("datasets") or [] if d.get("kind") != "funding"]
    metrics = summarize_metrics(result.get("metrics") or {})
    # 口径按「这次回测实际用的成本」写：策略 initialize 里 set_order_cost 会覆盖平台默认值，
    # 只写平台默认值的话，研究者声明的成本看起来像没生效（两边差一倍也没人发现）
    used = (result.get("assumptions") or {}).get("cost") or {}
    eff = {
        k: float(used[k]) if used.get(k) is not None else float(getattr(payload, k))
        for k in ("taker_fee", "slippage_bps", "spread_bps")
    }
    # 压力倍数会让 effective_cost 里的费率翻倍；判断「代码是否覆盖」要用翻倍前的基准值，
    # 否则 2× 成本测试会被误标成「代码自定义费率」。
    base_used = {
        k: float(used.get(f"base_{k}", used.get(k, getattr(payload, k))))
        for k in ("taker_fee", "slippage_bps", "spread_bps")
    }
    overridden = {
        k: {"platform_default": float(getattr(payload, k)), "used": v}
        for k, v in base_used.items()
        if abs(v - float(getattr(payload, k))) > 1e-12
    }
    code_override = (result.get("cost_model") or {}).get("cost_source") == "strategy"
    definitions = R.definitions(
        initial_cash=payload.initial_cash,
        fill_model=(result.get("assumptions") or {}).get("fill_model") or "",
        cost_scale=cost_scale,
        **eff,
    )
    definitions["cost_source"] = (
        "策略代码 set_order_cost 覆盖了平台默认成本："
        + "、".join(f"{k} {v['platform_default']:g}→{v['used']:g}" for k, v in overridden.items())
        if overridden
        else (
            "策略代码显式调用了 set_order_cost（数值与平台默认相同）"
            if code_override
            else "平台默认成本（策略没有用 set_order_cost 覆盖）"
        )
    )
    return {
        "datasets": [
            {k: d.get(k) for k in ("symbol", "source", "source_label", "bars", "start_ts", "end_ts")} for d in datasets
        ],
        "data_check": R.data_check(project, datasets, warmup_bars=payload.warmup_bars),
        "definitions": definitions,
        "consistency": R.consistency(metrics, initial_cash=payload.initial_cash),
        "zero_trade_diagnosis": R.zero_trade_diagnosis({**result, "metrics": metrics}),
        "cost_scale": cost_scale,
        "cost_model": result.get("cost_model") or {},
        **_rest(out, result, eq, step, metrics),
    }


def _data_source(project: dict[str, Any]) -> str:
    """合成行情默认关闭：auto 在没有真实行情时直接报「未接入数据」（QAGENT_ALLOW_SYNTHETIC=1 时才允许合成）。"""
    src = project.get("data_source") or "auto"
    return src if src in ("auto", "exchange", "synthetic") else "auto"


def _rest(out: dict, result: dict, eq: list, step: int, metrics: dict) -> dict:
    return {
        "ok": True,
        "bt_id": out.get("id"),
        "run_id": out.get("run_id"),
        "metrics": metrics,
        "equity": [round(float(v), 2) for v in eq[::step]],
        "cost_model": result.get("cost_model") or {},
        "cost_source": (result.get("cost_model") or {}).get("cost_source", "platform"),
        "benchmark": [round(float(v), 2) for v in (result.get("benchmark_curve") or [])[::step]],
        "data_sources": sorted({d.get("source_label") or d.get("source") for d in result.get("datasets") or []}),
        "warnings": (result.get("warnings") or [])[:3],
        "errors": (result.get("errors") or [])[:3],
        "status": result.get("status"),
    }


def _team(topic: str, *, team_id: str | None = None, context: str = "", source_project_id: str = "") -> dict:
    s = get_services()
    team = s.teams.get_team(team_id or LAB_REVIEW_TEAM) or s.teams.get_team("review_board") or s.teams.default_team()
    return s.teams.run(team, topic, context=context, source_project_id=source_project_id).to_dict()


def _discuss(project: dict[str, Any], topic: str) -> dict:
    """AI 发起 /discuss：建一场团队讨论（后台进行），发言挂在对话里。"""
    from .routes_missions import MissionIn, _materials, _runner, lab_context

    s = get_services()
    teams = s.teams.list_teams()
    payload = MissionIn(
        directive=topic,
        mode="single",
        team_id=project.get("team_id")
        or (LAB_REVIEW_TEAM if any(t.id == LAB_REVIEW_TEAM for t in teams) else (teams[0].id if teams else None)),
        lab_project_id=project["id"],
        symbol="" if project.get("market_auto") or not project.get("securities") else project["securities"][0],
        timeframe=project.get("timeframe") or "1h",
    )
    r = _runner()
    m = r.create({**payload.model_dump(), "context": lab_context(payload)}, _materials(payload))
    r.start(m["id"], wait=False)
    return {"mission_id": m["id"]}


def _discussion_status(mission_id: str, wait_seconds: int = 0) -> dict:
    """AI 查询 /discuss 的进度；wait_seconds > 0 时轮询到讨论结束或超时（Agent 不用自己写轮询）。"""
    import time as _time

    from .routes_missions import MissionStore

    store = MissionStore(get_services().store)
    deadline = _time.time() + max(0, wait_seconds)
    while True:
        m = store.get(mission_id)
        if m is None:
            return {"ok": False, "error": f"找不到讨论 {mission_id}"}
        finished = m.get("status") in ("done", "failed", "stopped", "idle", "awaiting_approval")
        if finished or _time.time() >= deadline:
            break
        _time.sleep(2.0)
    feed = store.feed(mission_id)
    speeches = [
        {
            "role": u.get("role_name"),
            "kind": u.get("kind"),
            "status": u.get("status"),
            "summary": " ".join((u.get("content") or "").split())[:300],
            "fallback": u.get("mode") == "fallback",
        }
        for u in feed
        if u.get("kind") in ("speech", "challenge", "synthesis")
    ]
    return {
        "ok": True,
        "mission_id": mission_id,
        "status": m.get("status"),
        "finished": m.get("status") in ("done", "failed", "stopped"),
        "stages": [{"name": st.get("name"), "status": st.get("status")} for st in m.get("stages") or []],
        "speeches": speeches[-12:],
        "typing": [x["role"] for x in speeches if x["status"] == "typing"],
        "conclusion": (m.get("conclusion") or "")[:3000],
    }


def _visible_projects(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .routes_collab import filter_visible

    return filter_visible("project", rows, "id")


def _start_conversation(pid: str, text: str) -> None:
    """另一个对话的 Agent 发来消息：消息已写入，在对应研究室后台开一轮对话。"""
    lab = _lab_for(pid)
    lab.start(pid, "chat", lambda: lab.chat(pid, text), wait=False)


def _create_conversation(payload: dict[str, Any]) -> dict[str, Any]:
    """Agent 新建对话：和研究者在界面上新建走同一条路径（含因子 / 模型等研究室的初始化与归属）。"""
    out = create_project(ProjectIn(**payload))
    return out["project"]


def _night(project: dict[str, Any], idea: str) -> dict:
    """AI 发起 /schedule（旧别名 /night）：加入每日定时任务队列。"""
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
        market=lambda sym, tf, bars=1500: s.market_summary(sym, tf, bars),
        templates=s.search_templates,
        team=_team,
        team_for_project=lambda pid, topic, **kwargs: _team(topic, source_project_id=pid, **kwargs),
        save_strategy=_save_strategy,
        registry=s.teams.tools,
        discuss=_discuss,
        discussion_status=_discussion_status,
        conversations={"start": _start_conversation, "create": _create_conversation, "visible": _visible_projects},
        night=_night,
        collinearity=_collinearity if FACTOR_READY else None,
        stage=_stage_project,
        platform_tools=True,
        load_result=lambda bt_id: s.store.get_blob("backtests", bt_id),
        runtime_journal=s.teams.runtime_journal,
    )
    from .routes_collab import claim_new

    return StrategyLab(s.router, LabStore(s.store, on_create=lambda pid: claim_new("project", pid)), tools)


def _stage_project(kind: str, payload: dict) -> tuple[StrategyLab, dict]:
    """完整流程的一个阶段：建一个对应类型的研究项目，返回 (该研究室, 项目)。"""
    lab = _kind_lab(kind)
    if kind == "factor":
        project = lab.store.save(new_factor_project(payload))
    else:
        project = lab.store.save(lab.new_project(payload))  # type: ignore[attr-defined]
    lab.store.append(
        project["id"],
        {"role": "event", "kind": "text", "content": f"由完整流程分派：{(payload.get('idea') or '')[:300]}"},
    )
    return lab, project


def _collinearity(
    symbols: list[str], timeframe: str, *, factors: list[dict[str, str]], horizon: int = 1, bars_limit: int = 1500
) -> dict:
    """多因子共线性诊断：在同一面板上算两两相关、VIF、条件数与高相关簇。"""
    from ..research import collinearity as CL
    from ..research import factor_expr as FX

    s = get_services()
    fields: set[str] = {"close"}
    for f in factors:
        try:
            fields.update(FX.fields_used(FX.parse(str(f.get("expr") or ""))))
        except Exception:
            continue
    panel = build_panel(s.market, _datahub(), symbols, timeframe, fields=sorted(fields), bars_limit=bars_limit)
    return CL.diagnose(panel, factors, horizon=horizon)


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
    from .routes_collab import guard_visible

    p = lab.store.get(pid)
    if p is None:
        raise HTTPException(404, "策略项目不存在")
    guard_visible("project", pid)
    lab.recover_stale(pid)
    return lab.store.get(pid) or p


def _brief(p: dict[str, Any]) -> dict[str, Any]:
    versions = p.get("versions") or []
    cur = next((v for v in versions if v["v"] == p.get("current_v")), None)
    # 列表只需要摘要：context/instructions 可能很大，只在详情里返回，避免会话列表响应膨胀。
    context = p.get("context") or {}
    return {
        **{k: v for k, v in p.items() if k not in ("versions", "code", "pins", "context", "instructions")},
        "context_keys": sorted(str(k) for k in context)[:20] if isinstance(context, dict) else [],
        "versions_count": len(versions),
        "metrics": (cur or {}).get("metrics"),
        "passed": ((cur or {}).get("evaluation") or {}).get("passed"),
    }


def _feedback_view(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "rating": rec["rating"],
        "comment": rec.get("comment") or "",
        "score": rec.get("score"),
    }


def _view(lab: StrategyLab, p: dict[str, Any], after: int = 0, since: float = 0.0) -> dict[str, Any]:
    import time

    from ..platform import context

    snapshot_time = time.time()
    messages = lab.store.messages(p["id"], after=after, since=since)
    uid = context.current().user_id or ""
    if uid and messages:
        rows = [
            r
            for r in get_services().store.list(Collections.MESSAGE_FEEDBACK)
            if r.get("project_id") == p["id"] and r.get("user_id") == uid
        ]
        if rows:
            by_seq = {int(r["seq"]): _feedback_view(r) for r in rows}
            messages = [
                {**m, "feedback": by_seq[int(m["seq"])]}
                if m.get("role") == "assistant" and int(m["seq"]) in by_seq
                else m
                for m in messages
            ]
    turn_messages = lab.store.messages(p["id"])
    last_user = max((m.get("seq", 0) for m in turn_messages if m.get("role") == "user"), default=0)
    errors = [m["content"] for m in turn_messages if m.get("kind") == "error" and m.get("seq", 0) > last_user]
    outcome = ("partial" if p.get("code") else "failed") if errors else p.get("status", "idle")
    budget = p.get("tool_budget") or {}
    if budget.get("user_seq") == last_user and int(budget.get("used", 0)) >= int(p.get("max_tool_calls") or 1000):
        outcome = "budget_exhausted"
    return {
        "project": p,
        "execution": {"state": outcome, "errors": errors},
        "messages": messages,
        "context": lab.context_view(p),
        "server_time": snapshot_time,
    }


@router.get("/projects/{pid}/context-pack", summary="统一上下文包：系统规则、项目规则、来源引用与预算")
def context_pack(pid: str) -> dict:
    """把“本轮会进入模型的内容”收成一个稳定结构，供前端/导出/调试共用。

    只读投影，不改变提示词；items 仍是 ``context_view`` 的真实明细，sources 只含标题/ID。
    """
    lab = _lab_for(pid)
    project = _project_or_404(lab, pid)
    view = lab.context_view(project)
    return {
        "format": "quantlab.context-pack/1",
        "project_id": pid,
        "kind": project.get("kind") or "strategy",
        "title": project.get("title") or "",
        "instructions": clamp_instructions(project.get("instructions")),
        "context": project.get("context") or {},
        "sources": next((i.get("summary") or "" for i in view.get("items") or [] if i.get("key") == "context"), ""),
        "items": view.get("items") or [],
        "total": view.get("total") or 0,
        "budget": view.get("budget") or 0,
        "history_budget": view.get("history_budget") or 0,
        "auto_compact_tokens": view.get("auto_compact_tokens") or 0,
        "model": view.get("model") or "",
    }


def _start(lab: StrategyLab, pid: str, kind: str, fn, wait: bool) -> dict:
    # 要调用模型的回合：工作区当日 LLM 配额用完就直接拒绝，不再降级成模板回复「假装还能对话」
    if lab.tools.runtime_journal is None and kind not in ("backtest", "walkforward"):
        block = getattr(get_services(), "llm_quota_block", None)
        if callable(block) and (reason := block()):
            raise AppError(ErrorCode.QUOTA_EXHAUSTED, reason)
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
    default_target = getattr(s.router, "default_target", None)
    return {
        "models": s.router.available_models(),
        "routing": cfg.routing,
        # 默认 = 当前模型清单里的第一个（优先已配置密钥的）；没有模型时才回落到全局路由
        "default_model": default_target()
        if callable(default_target)
        else (current("code_gen") if callable(current) else ""),
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
    from .routes_collab import filter_visible

    lab = _lab()
    rows = [p for p in lab.store.list() if bool(p.get("archived")) == archived]
    return {"projects": [_brief(p) for p in filter_visible("project", rows, "id")]}


@router.post("/projects", summary="新建策略项目（可带初始代码、模板或钉住的上下文）")
def create_project(payload: ProjectIn) -> dict:
    out = _create_project(payload)
    from .routes_collab import claim_new

    pid = (out.get("project") or {}).get("id") if isinstance(out, dict) else None
    if pid:
        claim_new("project", pid)
    return out


def _create_project(payload: ProjectIn) -> dict:
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
    if payload.kind in ("research", "workflow"):
        # 轻项目：研究 / 工作流升级只保留想法与来源引用，不生成策略代码、版本与验收目标
        data.update(kind=payload.kind, origin="created")
        p = lab.store.save(new_project(data))
        for pin in payload.pins:
            lab.pin(p["id"], kind=pin.kind, title=pin.title, text=pin.text)
        label = "研究工作流" if payload.kind == "workflow" else "研究会话"
        lab.store.append(
            p["id"],
            {
                "role": "event",
                "kind": "text",
                "content": f"{label}已创建。"
                + (f"想法：{payload.idea[:200]}" if payload.idea else "可以在这里继续描述研究问题。"),
            },
        )
        return _view(lab, _project_or_404(lab, p["id"]))
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


# ---------------------------------------------------------------------------
# 从「问 AI」升级为研究会话
# ---------------------------------------------------------------------------
MAX_SEED_MESSAGES = 40
MAX_SEED_INPUT = 200  # 请求上限（防止超大 body），实际只保留最近 40 条可用的种子消息
MAX_SEED_CONTENT = 8_000
SEED_ROLES = ("user", "assistant", "event")


class SeedMessageIn(BaseModel):
    """升级时带回的对话种子消息；未知角色 / 空内容由服务端过滤，不写入。"""

    role: str = Field("user", max_length=40)
    content: str = Field("", max_length=20_000)
    data: dict[str, Any] | None = None


class FromAskIn(BaseModel):
    """把一次「问 AI」升级成研究会话（默认私有，归当前用户）。"""

    kind: Literal["research", "workflow"] = Field("research", description="研究会话 / 工作流会话")
    title: str = Field("", max_length=60)
    idea: str = Field("", max_length=20_000)
    context: dict[str, Any] = Field(default_factory=dict, description="来源引用：工作流 / 运行 / 节点 / 页面资料")
    messages: list[SeedMessageIn] = Field(default_factory=list, max_length=MAX_SEED_INPUT)
    research: dict[str, Any] | None = Field(None, description="Ask 的研究上下文信封：只取工作流 / 运行 / 节点引用")
    # 请求体上限放宽到 10 倍，落库时统一截断到 MAX_INSTRUCTIONS_CHARS（20k），避免一次超长就 422
    instructions: str = Field(
        "", max_length=MAX_INSTRUCTIONS_CHARS * 10, description="研究者设定的项目规则（≤20k 字符）"
    )


def _research_refs(research: Any) -> dict[str, Any]:
    """从 Ask 的研究上下文信封里提炼引用（工作流 / 运行 / 节点 / 输出），不搬运正文与图。"""
    if not isinstance(research, dict):
        return {}
    refs: dict[str, Any] = {}
    wf = research.get("workflow")
    if isinstance(wf, dict):
        picked = {k: wf.get(k) for k in ("id", "title", "revision", "saved", "unsaved") if wf.get(k) is not None}
        if picked.get("id") or picked.get("title"):
            refs["workflow"] = picked
    run = research.get("run")
    if isinstance(run, dict) and run.get("id"):
        refs["run"] = {
            k: run.get(k) for k in ("id", "mode", "status", "revision", "view_mode") if run.get(k) is not None
        }
    node = research.get("selected_node")
    if isinstance(node, dict) and node.get("id"):
        refs["node"] = {k: node.get(k) for k in ("id", "title", "kind", "status") if node.get(k) is not None}
    outputs = research.get("outputs")
    if isinstance(outputs, list):
        kept = [
            {k: item.get(k) for k in ("port", "type", "hash", "run_id", "node_id") if item.get(k)}
            for item in outputs[:6]
            if isinstance(item, dict)
        ]
        if kept:
            refs["outputs"] = kept
    for key in ("project_name", "draft_id"):
        if research.get(key):
            refs[key] = research[key]
    return refs


@router.post("/projects/from-ask", summary="把「问 AI」升级成研究会话：写入种子消息，默认私有归当前用户")
def create_project_from_ask(payload: FromAskIn) -> dict:
    from .routes_collab import claim_new

    lab = _lab()
    context = normalize_context(payload.context)
    for key, value in _research_refs(payload.research).items():
        context.setdefault(key, value)  # 显式传入的 context 优先，研究信封只补缺失的引用
    p = lab.store.save(
        new_project(
            {
                "kind": payload.kind,
                "origin": "ask_upgrade",
                "title": payload.title,
                "idea": payload.idea,
                "context": context,
                "instructions": payload.instructions,
            }
        )
    )
    pid = p["id"]
    claim_new("project", pid)  # 归属当前用户；可见性沿用现有默认设置（默认私有），不另造一套
    seeds = []
    for msg in payload.messages:
        role = (msg.role or "").strip().lower()
        if role not in SEED_ROLES:  # 未知角色直接过滤，绝不把 tool/system 消息塞进对话
            continue
        content = (msg.content or "").strip()
        if not content:
            continue
        data = normalize_context(msg.data) if isinstance(msg.data, dict) else {}
        seeds.append({"role": role, "kind": "text", "content": content[:MAX_SEED_CONTENT], "data": data})
    for msg in seeds[-MAX_SEED_MESSAGES:]:  # 超过 40 条时保留最近的对话，而不是让升级请求失败
        lab.store.append(pid, msg)
    return _view(lab, _project_or_404(lab, pid))


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
        changes["securities"] = [x.strip().upper() for x in changes["securities"] if x.strip()]
        if "market_auto" not in changes:
            changes["market_auto"] = not changes["securities"]
    if changes.get("market_auto") is True:
        changes["securities"] = []
    if "title" in changes:
        changes["title_auto"] = False  # 研究者自己改过标题，AI 不再改
    if "instructions" in changes:
        # 统一去空白并截断到 20k：PATCH 允许先提交长文本，落库口径与 new_project 一致
        changes["instructions"] = clamp_instructions(changes["instructions"])
    lab.store.patch(pid, **changes)
    return {"project": _project_or_404(lab, pid)}


@router.delete(
    "/projects/{pid}", summary="删除对话（已保存的平台策略不受影响）；cascade=true 时连同它的全部分叉一起删除"
)
def delete_project(pid: str, cascade: bool = False) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    ids = [pid]
    if cascade:
        # 分叉的分叉也算：按 parent_id 一层层往下找
        everything = lab.store.list()
        frontier = {pid}
        while frontier:
            kids = [p["id"] for p in everything if p.get("parent_id") in frontier and p["id"] not in ids]
            ids.extend(kids)
            frontier = set(kids)
    if lab.tools.runtime_journal is not None:
        from ..agents.lab_runtime import prepare_history_change

        try:
            for project_id in ids:
                lab.request_stop(project_id)
                prepare_history_change(lab, project_id, deleting=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    deleted = [i for i in reversed(ids) if lab.store.delete(i)]
    return {"ok": pid in deleted, "deleted": deleted}


@router.post("/projects/{pid}/messages", summary="发送一条消息：模型带工具协作（/回测 /解释 /团队 /迭代 为快捷指令）")
def send_message(pid: str, payload: MessageIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    if lab.store.get(pid).get("status") == "running":  # type: ignore[union-attr]
        raise HTTPException(409, "这个项目正在运行，请等它结束或先停止")
    if not payload.text.strip().startswith(("/plan", "继续计划", "继续执行", "按计划执行", "批准")):
        lab._pause_plan(pid)
    if payload.images:
        _check_image_input(lab, pid, payload.model)
    changes = {
        k: v
        for k, v in {
            "model": payload.model,
            "effort": payload.effort,
            "suggest": payload.suggest,
            "max_tool_calls": payload.max_tool_calls,
            "auto_team_context": payload.auto_team_context,
        }.items()
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


class MessageFeedbackIn(BaseModel):
    rating: Literal[1, -1]
    comment: str = Field("", max_length=2000)
    score: int | None = Field(None, ge=1, le=5)


@router.post("/projects/{pid}/messages/{seq}/feedback", summary="给一条 Agent 消息点赞 / 点踩（可附意见）")
def message_feedback(pid: str, seq: int, payload: MessageFeedbackIn) -> dict:
    from ..platform import context

    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    if not any(m["seq"] == seq and m.get("role") == "assistant" for m in lab.store.messages(pid)):
        raise HTTPException(404, "消息不存在")
    uid = context.current().user_id or ""
    if not uid:
        raise HTTPException(401, "请先登录")
    key = f"{pid}:{seq}:{uid}"
    old = get_services().store.get(Collections.MESSAGE_FEEDBACK, key) or {}
    # 点赞 / 点踩只带 rating：保留评分卡写入的意见与星级，避免覆盖已有评价。
    comment = payload.comment.strip() or (old.get("comment") or "").strip()
    score = payload.score if payload.score is not None else old.get("score")
    if (
        old
        and old.get("rating") == payload.rating
        and (old.get("comment") or "").strip() == comment
        and old.get("score") == score
    ):
        get_services().store.delete(Collections.MESSAGE_FEEDBACK, key)
        return {"ok": True, "feedback": None}
    settings = get_services().auth.workspace(context.current_workspace()).get("settings") or {}
    improvement_allowed = bool((settings.get("privacy") or {}).get("allow_improvement_feedback", True))
    rec = {
        "id": key,
        "project_id": pid,
        "seq": seq,
        "user_id": uid,
        "rating": payload.rating,
        "comment": comment,
        "score": score,
        "improvement_allowed": improvement_allowed,
        "created_at": old.get("created_at") or int(time.time() * 1000),
        "updated_at": int(time.time() * 1000),
    }
    get_services().store.upsert(Collections.MESSAGE_FEEDBACK, rec)
    return {"ok": True, "feedback": _feedback_view(rec)}


class RenderErrorItem(BaseModel):
    kind: str = Field(..., max_length=20)
    message: str = Field("", max_length=400)
    source: str = Field("", max_length=1500)


class RenderErrorsIn(BaseModel):
    seq: int
    errors: list[RenderErrorItem] = Field(..., min_length=1, max_length=8)


@router.post(
    "/projects/{pid}/render-errors", summary="回答里的内容块没能显示：把错误交回 AI 自动修正（每条消息只修一次）"
)
def report_render_errors(pid: str, payload: RenderErrorsIn) -> dict:
    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    msg = next((m for m in lab.store.messages(pid) if m["seq"] == payload.seq), None)
    if msg is None or msg.get("role") != "assistant":
        raise HTTPException(404, "消息不存在")
    if (msg.get("data") or {}).get("render_repair") or p.get("status") == "running" or p.get("auto_repair") is False:
        return {"ok": False, "skipped": True}
    # 先打标记再启动：前端重复上报、多个标签页同时上报都只会触发一次
    lab.store.update(pid, payload.seq, data={"render_repair": len(payload.errors)})
    label = {"mermaid": "Mermaid 图", "math": "LaTeX 公式", "block": "ql 内容块", "svg": "SVG", "html": "HTML"}
    lines = [
        f"{i}. {label.get(e.kind, e.kind)}：{e.message}\n```\n{e.source[:800]}\n```"
        for i, e in enumerate(payload.errors, 1)
    ]
    text = (
        f"【渲染失败】你上一条回答里有 {len(payload.errors)} 处内容没能在页面上显示：\n"
        + "\n".join(lines)
        + "\n请逐一修正（只重新输出修好的部分并简短说明改了什么），不要重复整段回答。"
    )
    lab.store.append(
        pid,
        {
            "role": "event",
            "kind": "text",
            "content": f"检测到 {len(payload.errors)} 处内容渲染失败，已交给 AI 自动修正",
        },
    )
    lab.store.append(pid, {"role": "user", "kind": "text", "content": text, "data": {"auto": "render_repair"}})
    return _start(lab, pid, "chat", lambda: lab.chat(pid, text), False)


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
    timeframe: TimeframeStr | None = None
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
    mode: Literal["sliced", "independent"] = Field(
        "sliced",
        description="sliced 一次全区间回测按时间切段（连续复利、可复原全区间）；independent 每段重置资金单独回测",
    )
    attribution: Literal["close", "open"] = Field("close", description="完整交易按平仓还是开仓时间归段")
    wait: bool = False


@router.post("/projects/{pid}/walkforward", summary="分段回测：把回测区间等分几段分别回测，检查策略是否只在某一段有效")
def walk_forward(pid: str, payload: WalkForwardIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    return _start(
        lab,
        pid,
        "walkforward",
        lambda: lab.walk_forward(pid, segments=payload.segments, mode=payload.mode, attribution=payload.attribution),
        payload.wait,
    )


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
    text: str | None = Field(None, max_length=20_000, description="编辑后的内容；不填则按原文重试")
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


class ReportIn(BaseModel):
    text: str = Field("", max_length=2000, description="调研主题；不填则按对话主题")
    wait: bool = False


@router.post(
    "/projects/{pid}/report", summary="/report：按调研报告流程列计划、收集资料、分析，最后生成可导出 PDF 的报告"
)
def run_report(pid: str, payload: ReportIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    return _start(lab, pid, "plan", lambda: lab.run_report(pid, payload.text.strip()), payload.wait)


@router.get("/projects/{pid}/reports/{rid}", summary="读取一份调研报告（Markdown 正文）")
def get_report(pid: str, rid: str) -> dict:
    lab = _lab_for(pid)
    p = _project_or_404(lab, pid)
    rep = lab.get_report(pid, rid)
    if rep is None:
        raise HTTPException(404, "报告不存在")
    return {"report": rep, "project": {k: p.get(k) for k in ("id", "title", "securities", "timeframe")}}


@router.post(
    "/projects/{pid}/stop", summary="停止正在运行的回合：模型输出与联网搜索立即中断；其他工具在跑完当前一步后停下"
)
def stop_project(pid: str) -> dict:
    import time

    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    lab.request_stop(pid)
    # 等回合真正停下（模型输出通常在 0.1 秒内中断），返回停下后的状态，前端不用再等下一次推送
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and (lab.store.get(pid) or {}).get("status") == "running":
        time.sleep(0.05)
    p = _project_or_404(lab, pid)
    return {"ok": True, "status": p.get("status")}


def _project_runtime(pid: str, run_id: str | None = None):
    from ..platform import context

    lab = _lab_for(pid)
    project = _project_or_404(lab, pid)
    run_id = run_id or project.get("runtime_chat_id")
    if lab.tools.runtime_journal is None or not run_id:
        raise HTTPException(404, "该对话没有 Runtime 运行记录")
    from agent_runtime import AgentRuntime

    runtime = AgentRuntime(lab.tools.runtime_journal, None, None)
    current = runtime.get(run_id)
    if (
        current is None
        or current["tenant_id"] != context.current_workspace()
        or current["spec"]["metadata"].get("project_id") != pid
    ):
        raise HTTPException(404, "Runtime 运行记录不存在")
    return lab, runtime, current


@router.get("/projects/{pid}/runtime", summary="读取对话的权威执行状态和数据库游标事件")
def project_runtime(
    pid: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000), run_id: str | None = None
) -> dict:
    _, runtime, current = _project_runtime(pid, run_id)
    events = [event for event in runtime.events(current["id"], after=after) if event["seq"] <= current["seq"]][:limit]
    return {
        "run": {key: current[key] for key in ("id", "state", "seq", "error", "steps")},
        "events": events,
        "cursor": events[-1]["seq"] if events else after,
    }


@router.post("/projects/{pid}/runtime/resume", summary="幂等请求推进已有对话运行")
def resume_project_runtime(pid: str, payload: WaitIn, run_id: str | None = None) -> dict:
    from ..agents.lab_runtime import run_chat

    lab, _, current = _project_runtime(pid, run_id)
    return _start(lab, pid, "runtime_recover", lambda: run_chat(lab, pid, run_id=current["id"]), payload.wait)


def _runtime_admin() -> str:
    from ..platform import context

    actor = context.current()
    if actor.role != "admin" and not actor.is_admin:
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有管理员可以确认不确定的外部效果与费用")
    return actor.user_id or "system"


@router.post("/projects/{pid}/runtime/effect-resolution", summary="管理员记录已核实的外部效果结果")
def resolve_project_effect(pid: str, payload: EffectResolutionIn, run_id: str | None = None) -> dict:
    actor = _runtime_admin()
    lab, runtime, current = _project_runtime(pid, run_id)
    try:
        accepted = runtime.signal(
            current["id"],
            "effect_resolution",
            payload.model_dump(exclude={"signal_id"}),
            signal_id=payload.signal_id,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    get_services().audit.record(
        "lab.runtime.effect_resolution",
        object_type="project",
        object_id=pid,
        details={"run_id": current["id"], "step_id": payload.step_id, "outcome": payload.outcome},
    )
    return _resolution_reply(lab, runtime, current, accepted)


def _resolution_reply(lab, runtime, current, accepted):
    state = runtime.get(current["id"])["state"]
    if state in {"COMPLETED", "CANCELED", "FAILED"}:
        pid = current["spec"]["metadata"]["project_id"]
        project = lab.store.get(pid) or {}
        lab.store.patch(
            pid,
            runtime_pending_ids=[
                run_id for run_id in project.get("runtime_pending_ids", []) if run_id != current["id"]
            ],
        )
        if project.get("runtime_chat_id") == current["id"]:
            from ..agents.lab_runtime import run_chat

            run_chat(lab, pid, run_id=current["id"], project_only=True)
            waiting = any(
                item["kind"] == "ask" and (item.get("data") or {}).get("answered") is None
                for item in lab.store.messages(pid)
            )
            lab.store.patch(pid, status="awaiting_user" if waiting else "idle", job=None)
    return {"accepted": accepted, "state": state}


@router.post("/projects/{pid}/runtime/model-usage", summary="管理员记录服务商已确认的模型用量")
def resolve_project_model_usage(pid: str, payload: ModelUsageResolutionIn, run_id: str | None = None) -> dict:
    from ..llm.router import ProviderConfig

    actor = _runtime_admin()
    lab, runtime, current = _project_runtime(pid, run_id)
    if (payload.usage is None) == (not payload.no_charge_confirmed):
        raise HTTPException(422, "必须提供已确认用量，或明确确认未计费，两者不可同时提供")
    step = next((step for step in current["steps"] if step["id"] == payload.step_id), None)
    if step is None or step["kind"] != "model":
        raise HTTPException(404, "模型步骤不存在")
    if step["state"] != "RECONCILING":
        return {"accepted": False, "state": current["state"]}
    hub = get_services().model_hub
    admission = hub.admission(payload.step_id)
    if not admission or admission["workspace_id"] != current["tenant_id"] or admission["run_id"] != current["id"]:
        raise HTTPException(409, "模型准入记录与该步骤不一致")
    try:
        if payload.no_charge_confirmed:
            if admission["status"] == "SETTLED":
                raise ValueError("已结算用量不可改为未计费")
            hub.release_admission(payload.step_id)
        else:
            if not admission["pricing_json"]:
                raise ValueError("缺少原始价格快照，不能推算费用")
            provider = ProviderConfig(admission["provider"], "openai_compatible", "", "")
            hub.settle_admission(provider, admission["model"], payload.step_id, payload.usage)
        confirmed = hub.admission(payload.step_id)
        accepted = runtime.settle_model_uncertainty(
            current["id"], payload.step_id, usage_micro=confirmed["actual_micro"], actor=actor
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    get_services().audit.record(
        "lab.runtime.model_usage",
        object_type="project",
        object_id=pid,
        details={"run_id": current["id"], "step_id": payload.step_id},
    )
    return _resolution_reply(lab, runtime, current, accepted)


@router.post("/projects/{pid}/backtest", summary="用平台回测跑当前版本（cost_scale>1 为成本压力测试）")
def backtest_project(pid: str, payload: WaitIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    return _start(lab, pid, "backtest", lambda: lab.backtest_current(pid, cost_scale=payload.cost_scale), payload.wait)


class SweepIn(BaseModel):
    scales: list[float] = Field(default_factory=list, max_length=8, description="成本倍数（0.5–20），不填用默认档位")
    wait: bool = False


@router.post("/projects/{pid}/cost-sweep", summary="成本压力扫描：并行回测多档成本倍数，找出不再满足目标的成本极限")
def cost_sweep_project(pid: str, payload: SweepIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    if type(lab) is not StrategyLab:
        raise HTTPException(400, "成本压力扫描目前只支持策略研究；因子 / 模型等请用 /stress 做单档成本评估")
    return _start(lab, pid, "backtest", lambda: lab.cost_sweep(pid, payload.scales or None), payload.wait)


class ParamHeatmapIn(BaseModel):
    x_param: str = ""
    y_param: str = ""
    x_values: list[float] = Field(default_factory=list, max_length=8)
    y_values: list[float] = Field(default_factory=list, max_length=8)
    metric: str = Field(
        "sharpe",
        pattern="^(sharpe|total_return|annualized_return|excess_return|max_drawdown|calmar|sortino|win_rate|profit_factor)$",
    )
    wait: bool = False


@router.post("/projects/{pid}/parameter-heatmap", summary="二维参数敏感度热力图：网格回测并在对话里渲染参数平原")
def parameter_heatmap_project(pid: str, payload: ParamHeatmapIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    if type(lab) is not StrategyLab:
        raise HTTPException(400, "参数热力图目前只支持策略研究")
    return _start(
        lab,
        pid,
        "backtest",
        lambda: lab.parameter_heatmap(
            pid,
            x_param=payload.x_param,
            y_param=payload.y_param,
            x_values=payload.x_values or None,
            y_values=payload.y_values or None,
            metric=payload.metric,
        ),
        payload.wait,
    )


class StressTestIn(BaseModel):
    segments: int = Field(3, ge=2, le=6)
    windows: int = Field(4, ge=3, le=12)
    cost_scales: list[float] = Field(default_factory=list, max_length=8)
    sensitivity: bool = True
    wait: bool = False


@router.post("/projects/{pid}/stress-test", summary="一键过拟合压力测试：滚动分段、参数敏感性、成本压力")
def stress_test_project(pid: str, payload: StressTestIn) -> dict:
    lab = _lab_for(pid)
    _project_or_404(lab, pid)
    if type(lab) is not StrategyLab:
        raise HTTPException(400, "一键压力测试目前只支持策略研究")
    return _start(
        lab,
        pid,
        "backtest",
        lambda: lab.stress_test(
            pid,
            segments=payload.segments,
            windows=payload.windows,
            cost_scales=payload.cost_scales or None,
            sensitivity=payload.sensitivity,
        ),
        payload.wait,
    )


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
