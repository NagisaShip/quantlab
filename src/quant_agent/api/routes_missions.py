"""指挥台 API：作战任务（单周期 / 流水线作业）、逐条发言、关卡批准、插话与点名追问。"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..agents.mission import PIPELINE_PRESETS, MissionRunner, MissionStore
from ..store import Collections
from .deps import get_services

router = APIRouter(prefix="/api/missions", tags=["指挥台"])


class StageIn(BaseModel):
    key: str = ""
    name: str = ""
    mode: Literal["sequential", "parallel", "debate"] = "sequential"
    role_ids: list[str] = Field(default_factory=list)
    gate: bool = False


class MissionIn(BaseModel):
    directive: str = Field(..., min_length=2, max_length=2000)
    context: str = Field("", max_length=4000)
    mode: Literal["single", "pipeline"] = "single"
    team_id: str | None = None
    preset: str | None = None
    stages: list[StageIn] | None = None
    synthesizer_id: str | None = None
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    include_market_data: bool = True
    strategy: str | None = Field(None, description="附带一个模板策略的回测数据")
    lab_project_id: str | None = Field(None, description="附带 AI 策略室项目的当前代码与成绩")
    wait: bool = False


class PipelineIn(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=1, max_length=40)
    description: str = Field("", max_length=200)
    stages: list[StageIn] = Field(..., min_length=1)
    synthesizer_id: str = "research_lead"


class SayIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    to_role: str | None = None
    wait: bool = False


class WaitIn(BaseModel):
    wait: bool = False


def _runner() -> MissionRunner:
    s = get_services()
    return MissionRunner(s.teams, MissionStore(s.store), on_conclusion=_pin_conclusion)


def _pin_conclusion(mission: dict[str, Any], conclusion: str) -> None:
    """讨论结束：结论自动钉进发起它的 AI 研究室对话的上下文（对话的 auto_team_context 默认开启）。"""
    pid = mission.get("lab_project_id")
    if not pid:
        return
    from ..agents.strategy_lab import LabStore, _lock, _now

    store = LabStore(get_services().store)
    project = store.get(pid)
    if project is None or project.get("auto_team_context", True) is False:
        return
    title = f"团队讨论结论 · {(mission.get('directive') or '')[:16]}"
    with _lock(f"p:{pid}"):
        p = store.get(pid) or project
        pins = [x for x in p.get("pins") or [] if x.get("mission_id") != mission.get("id")]
        pins.append(
            {
                "id": uuid.uuid4().hex[:8],
                "kind": "team",
                "title": title[:60],
                "text": conclusion[:4000],
                "mission_id": mission.get("id"),
                "created_at": _now(),
            }
        )
        p["pins"] = pins[-12:]
        store.save(p)
    store.append(pid, {"role": "event", "kind": "text", "content": "团队讨论结论已自动加入上下文"})


def _or_404(r: MissionRunner, mid: str) -> dict[str, Any]:
    m = r.store.get(mid)
    if m is None:
        raise HTTPException(404, "任务不存在")
    return m


def _view(r: MissionRunner, mid: str, after: int = 0, since: float = 0.0) -> dict[str, Any]:
    now = time.time()
    return {"mission": _or_404(r, mid), "feed": r.store.feed(mid, after=after, since=since), "server_time": now}


def _materials(payload: MissionIn) -> dict[str, Any]:
    s = get_services()
    out: dict[str, Any] = {}
    if payload.include_market_data:
        try:
            out["market"] = s.market_summary(payload.symbol, payload.timeframe)
        except Exception as exc:  # 行情失败不阻塞讨论
            out["market_error"] = str(exc)[:200]
    if payload.strategy:
        try:
            res = s.run_backtest(
                strategy_name=payload.strategy, symbol=payload.symbol, timeframe=payload.timeframe, persist=False
            )
            out["backtest"] = {"strategy": payload.strategy, "metrics": res.metrics.to_dict()}
        except Exception as exc:
            out["backtest_error"] = str(exc)[:200]
    if payload.lab_project_id:
        from ..agents.strategy_lab import LabStore, _latest_metrics

        p = LabStore(s.store).get(payload.lab_project_id)
        if p:
            out["lab"] = {
                "title": p.get("title"),
                "idea": p.get("idea"),
                "version": p.get("current_v"),
                "metrics": _latest_metrics(p),
                "code": (p.get("code") or "")[:4000],
            }
    return out


def _start(r: MissionRunner, mid: str, wait: bool, job=None) -> dict:
    try:
        r.start(mid, wait=wait, job=job)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _view(r, mid)


def _saved_pipelines() -> list[dict[str, Any]]:
    s = get_services()
    rows = sorted(s.store.list(Collections.PIPELINES), key=lambda p: p.get("created_at", 0))
    return [{**p, "custom": True} for p in rows]


def _pipeline(pid: str | None) -> dict[str, Any] | None:
    if not pid or pid in PIPELINE_PRESETS:
        return None
    return get_services().store.get(Collections.PIPELINES, pid)


@router.get("/presets", summary="流水线作业预设（内置 + 已保存模板）与可用团队、角色")
def presets() -> dict:
    s = get_services()
    return {
        "pipelines": [{"id": k, **v, "custom": False} for k, v in PIPELINE_PRESETS.items()] + _saved_pipelines(),
        "teams": [t.to_dict() for t in s.teams.list_teams()],
        "roles": [
            {
                "id": r.id,
                "name": r.name,
                "title": r.title,
                "tags": r.tags,
                "model": r.model,
                "effort": r.effort,
                "avatar": r.avatar,
            }
            for r in s.roles.list()
        ],
    }


@router.post("/pipelines", summary="把当前流水线存为模板（带 id 则覆盖已保存的模板）")
def save_pipeline(payload: PipelineIn) -> dict:
    s = get_services()
    if payload.id and payload.id in PIPELINE_PRESETS:
        raise HTTPException(400, "内置流水线不能覆盖，请另存为新模板")
    old = s.store.get(Collections.PIPELINES, payload.id) if payload.id else None
    roles = {r.id for r in s.roles.list()}
    stages = [
        {
            **st.model_dump(),
            "key": st.key or f"s{i}",
            "name": st.name or f"阶段 {i + 1}",
            "role_ids": [r for r in st.role_ids if r in roles],
        }
        for i, st in enumerate(payload.stages)
    ]
    if not any(st["role_ids"] for st in stages):
        raise HTTPException(400, "模板里至少要有一个角色")
    doc = {
        "id": (old or {}).get("id") or f"pl_{uuid.uuid4().hex[:8]}",
        "name": payload.name,
        "description": payload.description,
        "stages": stages,
        "synthesizer_id": payload.synthesizer_id if payload.synthesizer_id in roles else "research_lead",
        "created_at": (old or {}).get("created_at") or time.time(),
    }
    s.store.upsert(Collections.PIPELINES, doc)
    return {"pipeline": {**doc, "custom": True}}


@router.delete("/pipelines/{pid}", summary="删除已保存的流水线模板")
def delete_pipeline(pid: str) -> dict:
    s = get_services()
    if not s.store.get(Collections.PIPELINES, pid):
        raise HTTPException(404, "模板不存在")
    return {"ok": s.store.delete(Collections.PIPELINES, pid)}


@router.get("", summary="作战任务列表")
def list_missions(limit: int = Query(30, ge=1, le=200)) -> dict:
    r = _runner()
    return {"missions": [{k: v for k, v in m.items() if k != "materials"} for m in r.store.list(limit)]}


@router.post("", summary="下达指令：创建作战任务并开始（单周期或流水线）")
def create_mission(payload: MissionIn) -> dict:
    r = _runner()
    data = payload.model_dump()
    if payload.stages is not None:
        data["stages"] = [st.model_dump() for st in payload.stages]
    saved = _pipeline(payload.preset) if payload.mode == "pipeline" else None
    if saved:  # 已保存的模板：展开成阶段，组长取模板里的
        data["stages"] = data.get("stages") or saved["stages"]
        data["synthesizer_id"] = data.get("synthesizer_id") or saved.get("synthesizer_id")
    m = r.create(data, _materials(payload))
    if not m["stages"]:
        raise HTTPException(400, "任务没有任何有效角色")
    return _start(r, m["id"], payload.wait)


@router.get("/{mid}", summary="任务详情与增量发言（after=已收到的最大序号，since=上次的 server_time）")
def get_mission(mid: str, after: int = Query(0, ge=0), since: float = Query(0.0, ge=0)) -> dict:
    return _view(_runner(), mid, after, since)


@router.delete("/{mid}", summary="删除作战任务")
def delete_mission(mid: str) -> dict:
    r = _runner()
    _or_404(r, mid)
    return {"ok": r.store.delete(mid)}


@router.post("/{mid}/approve", summary="关卡批准：进入下一阶段")
def approve(mid: str, payload: WaitIn) -> dict:
    r = _runner()
    m = _or_404(r, mid)
    if m["status"] not in ("awaiting_approval", "stopped", "idle", "failed"):
        raise HTTPException(409, "当前没有等待批准的关卡")
    return _start(r, mid, payload.wait, job=lambda: r.approve(mid))


@router.post("/{mid}/say", summary="指挥官插话；指定 to_role 时该角色会回应")
def say(mid: str, payload: SayIn) -> dict:
    r = _runner()
    m = _or_404(r, mid)
    r.say(mid, payload.text, to_role=payload.to_role)
    if payload.to_role:
        if m["status"] == "running":
            raise HTTPException(409, "讨论进行中，追问会在当前发言结束后再试；插话已记录")
        prev = m["status"]

        def job() -> None:
            r.answer(mid, payload.to_role or "")
            r.store.patch(mid, status=prev if prev != "queued" else "done")

        return _start(r, mid, payload.wait, job=job)
    return _view(r, mid)


@router.post("/{mid}/stop", summary="叫停任务（在当前发言结束后停下）")
def stop(mid: str) -> dict:
    r = _runner()
    _or_404(r, mid)
    r.store.patch(mid, stop_requested=True)
    return {"ok": True}
