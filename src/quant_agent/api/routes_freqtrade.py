"""Freqtrade 策略导入 / 导出：检查、导入（自动体检）、AI 改写跑不了的写法、导回 Freqtrade 文件。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..strategies import freqtrade_import as FI
from ..strategies.repo import StrategyRepository
from .deps import get_services

router = APIRouter(prefix="/api/freqtrade", tags=["Freqtrade 导入"])

MAX_SOURCE = 200_000


class SourceIn(BaseModel):
    source: str = Field(..., max_length=MAX_SOURCE)


class ImportIn(SourceIn):
    name: str = ""
    securities: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    checkup: bool = True


class RewriteIn(SourceIn):
    errors: list[str] = Field(default_factory=list)
    securities: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    timeframe: str = "1h"


def _repo() -> StrategyRepository:
    return StrategyRepository(get_services().store.root / "strategies")


@router.get("/support", summary="兼容范围：支持的指标、会被忽略和不支持的写法")
def support() -> dict:
    from ..sdk.ft import SUPPORTED_TA

    return {
        "ta": SUPPORTED_TA,
        "ignored": FI.IGNORED_METHODS,
        "blocking": FI.BLOCKING_METHODS,
        "dataprovider": sorted(FI.DP_OK),
    }


@router.post("/check", summary="检查一个 Freqtrade 策略能否导入（试跑一小段，不保存）")
def check(payload: SourceIn) -> dict:
    if not FI.is_freqtrade(payload.source):
        raise HTTPException(400, "没找到继承 IStrategy 的策略类，这不像 Freqtrade 策略")
    return FI.convert(payload.source).to_dict()


@router.post("/import", summary="导入 Freqtrade 策略为平台策略，并自动开始一次体检")
def import_strategy(payload: ImportIn) -> dict:
    from .routes_research import _python_allowed

    _python_allowed()
    if not FI.is_freqtrade(payload.source):
        raise HTTPException(400, "没找到继承 IStrategy 的策略类，这不像 Freqtrade 策略")
    rep = FI.convert(payload.source)
    if not rep.ok:
        return {"report": rep.to_dict(), "strategy": None}
    secs = [s.strip().upper() for s in payload.securities if s.strip()] or ["BTC/USDT"]
    repo = _repo()
    name = (payload.name or rep.class_name or "Freqtrade 策略")[:60]
    from ..sdk.runner import declared_params

    rec = repo.save(
        name=name,
        source=rep.source,
        params=declared_params(rep.source),
        team="Freqtrade 导入",
        kind="code",
        origin="freqtrade",
        symbol=secs[0],
        timeframe=rep.timeframe or "1h",
        securities=secs,
        notes=f"从 Freqtrade 导入（类 {rep.class_name}）"
        + (f"；提示：{'；'.join(rep.warnings[:3])}" if rep.warnings else ""),
        slug=repo.unique_slug(name),
    )
    get_services().audit.record(
        "freqtrade.import",
        object_type="strategy",
        object_id=rec.meta.slug,
        details={"class": rep.class_name, "warnings": len(rep.warnings)},
    )
    body: dict[str, Any] = {"report": rep.to_dict(), "strategy": rec.to_dict(include_source=False)}
    if payload.checkup:
        from .routes_validation import CheckupIn, checkup_start

        try:
            body["checkup"] = checkup_start(CheckupIn(slug=rec.meta.slug))["run"]
        except HTTPException as exc:
            body["checkup_error"] = exc.detail
    return body


@router.post("/rewrite", summary="AI 把导入不了的 Freqtrade 策略改写成平台原生策略")
def rewrite(payload: RewriteIn) -> dict:
    from ..agents.copilot import StrategyCopilot

    problems = "；".join(payload.errors) or "无"
    instruction = (
        "这是一个 Freqtrade（IStrategy）策略，平台的兼容层跑不了它，问题是：" + problems + "。"
        "请改写成平台原生策略（initialize / handle_data，用平台 SDK 的指标和下单函数），"
        "保持原策略的入场、出场、止损和止盈逻辑；做不到的部分（如做空、杠杆）去掉并在改动要点里说明。"
    )
    res = StrategyCopilot(get_services().router).edit(
        payload.source, instruction, securities=payload.securities, timeframe=payload.timeframe
    )
    return res.to_dict()


@router.get(
    "/export/{slug}", summary="把导入过的策略导回 Freqtrade 文件（参数写回 default）", response_class=PlainTextResponse
)
def export_strategy(slug: str) -> PlainTextResponse:
    rec = _repo().get(slug)
    if rec is None:
        raise HTTPException(404, "策略不存在")
    try:
        text = FI.export(
            rec.source, {k: v for k, v in (rec.params or {}).items() if k not in ("max_open_trades", "ft_record")}
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    fname = f"{slug}.py"
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{fname}"'})
