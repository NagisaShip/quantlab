"""规则 DSL v2 与策略版本 API。

- /api/dsl/schema       JSON Schema、指标列表、示例（前端逐项表单与 JSON 视图共用同一份 schema）
- /api/dsl/validate     逐项校验，返回字段路径 + 说明、自动计算的预热长度与中文描述
- /api/dsl/strategies   新建 / 保存 DSL 策略（保存即登记不可改写的版本）
- /api/strategies/{slug}/versions          版本列表（DRAFT / VALIDATED / FROZEN / ARCHIVED）
- /api/strategy-versions/{id}              取回某版本的确切内容（复现用）
- /api/strategy-versions/{id}/freeze|archive
- /api/dsl/to-code      规则 → 带 @rule / @param 中文注释的 Python 代码
- /api/dsl/from-code    代码 → 规则：AST 比较判断代码是否仍恰好是顶部 @rule 描述的规则
- /api/dsl/strategies/{slug}/to-code   把已保存的 DSL 策略转成代码策略（原地，旧 JSON 进版本快照）
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..platform import context
from ..platform.errors import AppError, ErrorCode
from ..sdk import declared_params
from ..strategies import lang as L
from ..strategies.dsl import (
    DSL_SCHEMA,
    EXAMPLES,
    INDICATOR_TYPES,
    DslError,
    canonical,
    evaluate_rotation,
    validate,
)
from ..strategies.repo import StrategyRecord, StrategyRepository
from ..strategies.rule_code import code_to_rule, rule_to_code
from .deps import get_services

router = APIRouter(prefix="/api", tags=["规则 DSL 与策略版本"])


def _repo() -> StrategyRepository:
    return StrategyRepository(get_services().store.root / "strategies")


def lang_of_record(rec: StrategyRecord) -> str:
    return L.lang_of(rec.meta.kind)


def register_version(rec: StrategyRecord, *, source: str | None = None, params: dict[str, Any] | None = None) -> dict:
    """登记（或取回）这份策略内容对应的不可改写版本。"""
    s = get_services()
    lang = lang_of_record(rec)
    code = rec.source if source is None else source
    issues = L.issues(lang, code)
    return s.versions.ensure(
        workspace_id=context.current_workspace(),
        slug=rec.meta.slug,
        lang=lang,
        name=rec.meta.name,
        code=code,
        params=params if params is not None else L.declared_params(lang, code),
        securities=rec.meta.securities,
        timeframe=rec.meta.timeframe,
        user_id=context.current().user_id,
        issues=issues,
    )


def _problems(exc: DslError) -> AppError:
    return AppError(ErrorCode.INVALID_PARAMETER, "DSL 校验未通过：" + str(exc), details={"problems": exc.problems})


# ---------------------------------------------------------------------------
# DSL
# ---------------------------------------------------------------------------
@router.get("/dsl/schema")
def dsl_schema() -> dict:
    return {
        "schema": DSL_SCHEMA,
        "indicators": [{"type": k, "label": v} for k, v in INDICATOR_TYPES.items()],
        "examples": EXAMPLES,
        "notes": [
            "规则以结构化 JSON 表达，平台解释执行；也可一键生成带 @rule 注释的 Python 代码，在规则与代码之间切换",
            "变量只读取当前及过去已完成的 bar；在 bar 收盘后判断，下一根开盘成交",
            "入场与离场同时满足时离场优先；同一根 bar 同时触及止损与止盈时止损优先",
            "参数用 $名字 引用，参数优化直接在 params 上做网格",
        ],
    }


class DslValidateIn(BaseModel):
    spec: dict[str, Any]
    params: dict[str, Any] | None = None


@router.post("/dsl/validate")
def dsl_validate(payload: DslValidateIn) -> dict:
    """逐项校验。始终返回 200：ok=false 时 problems 给出每个出错字段的路径与说明。"""
    try:
        c = validate(payload.spec, params=payload.params)
    except DslError as exc:
        return {"ok": False, "problems": exc.problems}
    return {"ok": True, "problems": [], **c.to_dict()}


class DslStrategyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    spec: dict[str, Any]
    team: str = "我的策略"
    notes: str = ""
    slug: str | None = None


@router.post("/dsl/strategies")
def save_dsl_strategy(payload: DslStrategyIn) -> dict:
    """新建或保存 DSL 策略：校验通过才保存；保存即登记版本（内容相同不重复登记）。"""
    try:
        compiled = validate(payload.spec)
    except DslError as exc:
        raise _problems(exc) from exc
    repo = _repo()
    if payload.slug and not repo.exists(payload.slug):
        raise AppError(ErrorCode.NOT_FOUND, "策略不存在")
    if payload.slug and repo.get(payload.slug).meta.kind != "dsl":
        raise AppError(ErrorCode.CONFLICT, "该策略不是 DSL 策略，不能用 DSL 覆盖")
    spec = {**compiled.spec, "name": payload.name}
    # 标的清单要带上**整个轮动池**：回测 / 实时模拟按 meta.securities 取行情，
    # 只登记 instrument 会让多标的策略运行时拿不到另外两个标的的 K 线，
    # 排名恒为 None、永远空仓——而且不会报错，属于最危险的那种静默失败。
    universe = list(compiled.resolved.get("instruments") or [spec["instrument"]])
    rec = repo.save(
        name=payload.name,
        source=canonical_pretty(spec),
        params=dict(spec.get("params") or {}),
        team=payload.team,
        kind="dsl",
        origin="user",
        symbol=universe[0],
        timeframe=spec["timeframe"],
        securities=universe,
        notes=payload.notes,
        slug=payload.slug or repo.unique_slug(payload.name),
    )
    version = register_version(rec)
    get_services().audit.record(
        "strategy.save",
        object_type="strategy",
        object_id=rec.meta.slug,
        details={"lang": "dsl", "version": version["version_no"]},
    )
    return {"strategy": rec.to_dict(), "version": version, "compiled": compiled.to_dict()}


def canonical_pretty(spec: dict[str, Any]) -> str:
    return json.dumps(json.loads(canonical(spec)), ensure_ascii=False, indent=2) + "\n"


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------
@router.get("/strategies/{slug}/versions")
def list_versions(slug: str) -> dict:
    rec = _repo().get(slug)
    if rec is None:
        raise AppError(ErrorCode.NOT_FOUND, "策略不存在")
    current = register_version(rec)
    return {"versions": get_services().versions.list(context.current_workspace(), slug), "current": current["id"]}


@router.get("/strategy-versions/{version_id}")
def get_version(version_id: str) -> dict:
    v = get_services().versions.get(context.current_workspace(), version_id, code=True)
    if v is None:
        raise AppError(ErrorCode.NOT_FOUND, "策略版本不存在")
    return {"version": v}


@router.post("/strategy-versions/{version_id}/freeze")
def freeze_version(version_id: str) -> dict:
    v = get_services().versions.freeze(context.current_workspace(), version_id)
    get_services().audit.record("strategy.freeze", object_type="strategy_version", object_id=version_id)
    return {"version": v}


@router.post("/strategy-versions/{version_id}/archive")
def archive_version(version_id: str) -> dict:
    v = get_services().versions.archive(context.current_workspace(), version_id)
    get_services().audit.record("strategy.archive", object_type="strategy_version", object_id=version_id)
    return {"version": v}


# ---------------------------------------------------------------------------
# 轮动预览（T04）
# ---------------------------------------------------------------------------
class DslRankIn(BaseModel):
    spec: dict[str, Any]
    params: dict[str, Any] | None = None
    ends: dict[str, list[float]] | None = Field(
        default=None,
        description="各标的的收盘价序列；缺省用最新行情（合成数据兜底）",
    )


@router.post("/dsl/rank")
def dsl_rank(payload: DslRankIn) -> dict:
    """按 rank 节点算一次轮动名次：每个标的的得分、完整名次、入选者与目标权重。

    只读**已收盘**的 bar，与回测 / 实时模拟走同一段 `evaluate_rotation`：
    前端看到的"会买谁、买多少"必须与真正下单时的口径一致，
    否则用户按预览结果调的参数到了回测里就是另一回事。
    """
    try:
        compiled = validate(payload.spec, params=payload.params)
    except DslError as exc:
        raise _problems(exc) from exc
    if not compiled.resolved.get("rank"):
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            "这份 DSL 没有 rank 节点，无法预览轮动名次",
            details={"problems": [{"path": "rank", "message": "缺少 rank 节点"}]},
        )
    universe = list(compiled.resolved["instruments"])
    closes = payload.ends
    if not closes:
        # 没有传行情就用平台自己的行情源（真实优先、失败回退合成），
        # 这样"保存前先看一眼"不需要用户自己准备数据。
        # `source="auto"` 与 K 线接口同一口径；单个标的取不到就跳过它（排名里会变成未就绪），
        # 而不是整条请求失败——缺一个标的时仍能让用户看到其余标的的名次。
        service = get_services().market
        closes = {}
        for name in universe:
            try:
                data, _src = service.get_bars(name, compiled.resolved["timeframe"], limit=300, source="auto")
                closes[name] = [float(b.close) for b in data]
            except Exception:
                closes[name] = []
    rows = {name: [{"open": c, "high": c, "low": c, "close": c} for c in closes.get(name, [])] for name in universe}
    out = evaluate_rotation(compiled.resolved, rows)
    return {
        "ok": True,
        "instruments": universe,
        "timeframe": compiled.resolved["timeframe"],
        "tie_break": out["tie_break"],
        "scores": {k: (None if v is None else round(float(v), 10)) for k, v in out["scores"].items()},
        "order": out["order"],
        "picks": out["picks"],
        # 权重对外用十进制字符串：与协议的金额 / 数量口径一致，避免浮点抖动。
        "weights": {k: f"{v:.10f}".rstrip("0").rstrip(".") or "0" for k, v in out["weights"].items()},
        "summary": compiled.summary,
    }


# ---------------------------------------------------------------------------
# 规则 ↔ 代码
# ---------------------------------------------------------------------------
class DslToCodeIn(BaseModel):
    spec: dict[str, Any]


@router.post("/dsl/to-code", summary="规则生成代码：输出带 @rule / @param 中文注释的 Python 策略")
def dsl_to_code(payload: DslToCodeIn) -> dict:
    """始终返回 200：规则不合法时 ok=false，problems 与 /dsl/validate 同口径。"""
    try:
        code = rule_to_code(payload.spec)
        compiled = validate(payload.spec)
    except DslError as exc:
        return {"ok": False, "problems": exc.problems, "code": None}
    return {
        "ok": True,
        "problems": [],
        "code": code,
        "params": dict(compiled.spec.get("params") or {}),
        "warmup_bars": compiled.warmup_bars,
        "summary": compiled.summary,
    }


class DslFromCodeIn(BaseModel):
    code: str = Field(..., max_length=200_000)


@router.post("/dsl/from-code", summary="代码识别规则：用 AST 比较判断代码是否仍是 @rule 描述的规则")
def dsl_from_code(payload: DslFromCodeIn) -> dict:
    """is_rule=true 时 spec 可直接在规则视图编辑；否则 reason 说明为何按代码策略处理。"""
    out = code_to_rule(payload.code)
    if out["is_rule"]:
        compiled = validate(out["spec"])
        out["summary"] = compiled.summary
        out["warmup_bars"] = compiled.warmup_bars
    return out


@router.post("/dsl/strategies/{slug}/to-code", summary="把 DSL 规则策略转成代码策略（生成带 @rule 注释的 Python）")
def convert_dsl_to_code(slug: str) -> dict:
    """原地转换：同一个策略改为代码策略，旧的 JSON 进版本快照；代码保留 @rule 注释，仍可切回规则视图。"""
    repo = _repo()
    rec = repo.get(slug)
    if rec is None:
        raise AppError(ErrorCode.NOT_FOUND, "策略不存在")
    if rec.meta.kind != "dsl":
        raise AppError(ErrorCode.CONFLICT, "该策略不是 DSL 策略")
    try:
        spec = json.loads(rec.source)
        code = rule_to_code({**spec, "name": rec.meta.name})
    except (ValueError, DslError) as exc:
        raise AppError(ErrorCode.INVALID_PARAMETER, f"规则无法转换为代码：{exc}") from exc
    new = repo.save(
        name=rec.meta.name,
        source=code,
        params=declared_params(code),
        team=rec.meta.team,
        kind="code",
        origin=rec.meta.source,
        symbol=rec.meta.symbol,
        timeframe=rec.meta.timeframe,
        securities=rec.meta.securities,
        notes=rec.meta.notes,
        slug=slug,
    )
    version = register_version(new)
    get_services().audit.record(
        "strategy.save",
        object_type="strategy",
        object_id=slug,
        details={"lang": "python", "version": version["version_no"], "from": "dsl"},
    )
    return {"strategy": new.to_dict(), "version": version, "code": code}
