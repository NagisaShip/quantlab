"""信息源 API：连接器状态、同步、CSV 导入、数据集与序列预览、连接器密钥。

每条数据都按 (ts, available_at, value) 存放，因子研究按 available_at 做 as-of 对齐，避免用到当时还没发布的数据。
连接器密钥是部署级配置（与模型密钥同一处加密存放），只有部署管理员可以修改；接口只返回是否已填写。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..platform import context
from ..research.datahub import CONNECTORS, FIELD_SCOPE, DataHub, connectors_view
from .deps import get_services

router = APIRouter(prefix="/api/datahub", tags=["信息源"])

KEYED = {c["id"] for c in CONNECTORS if c["needs_key"] or c.get("key_optional")}
_RECORDERS: dict[str, object] = {}  # 每个工作区一个强平流记录器（进程内常驻）


class SyncIn(BaseModel):
    connector: str
    symbols: list[str] = Field(default_factory=list)
    start: int | None = None
    end: int | None = None
    period: Literal["5m", "15m", "30m", "1h", "4h", "1d"] = "1h"
    series: str = Field("", max_length=120)
    name: str = Field("", max_length=30)
    wait: bool = True


class CsvIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=30)
    text: str = Field(..., min_length=1, max_length=5_000_000)
    symbol: str | None = None
    lag_hours: float = Field(0, ge=0, le=24 * 30)


class KeyIn(BaseModel):
    connector: str
    key: str = Field("", max_length=400)


def _llm():
    """新闻标注用的大模型：没配置密钥时返回 None，走规则词典。"""
    from ..llm.base import Message

    router = getattr(get_services(), "router", None)
    if router is None:
        return None

    def call(system: str, user: str):
        return router.chat(
            [Message("system", system), Message("user", user)], role="default", temperature=0.0, max_tokens=2500
        )

    return call


def _hub() -> DataHub:
    s = get_services()
    return DataHub(s.store, secrets_fn=s.read_secrets, llm=_llm())


@router.get("/connectors", summary="连接器列表：类别、字段、是否需要密钥、已有数据集数与最近同步")
def list_connectors() -> dict:
    s = get_services()
    hub = _hub()
    return {
        "connectors": connectors_view(hub, s.read_secrets()),
        "field_scope": FIELD_SCOPE,
        "can_edit_keys": bool(context.current().is_admin),
    }


@router.get("/datasets", summary="已有数据集（字段 × 标的 × 来源）")
def list_datasets() -> dict:
    return {"datasets": _hub().datasets()}


@router.get("/datasets/{key:path}/series", summary="数据集序列预览（最多 limit 个点，等间隔抽样）")
def dataset_series(key: str, limit: int = 400) -> dict:
    hub = _hub()
    rec = hub.dataset(key)
    if rec is None:
        raise HTTPException(404, "数据集不存在")
    rows = hub.series(key)
    step = max(1, len(rows) // max(10, min(limit, 5000)))
    return {"dataset": rec, "rows": rows[::step], "total": len(rows)}


@router.delete("/datasets/{key:path}", summary="删除数据集")
def delete_dataset(key: str) -> dict:
    hub = _hub()
    if hub.dataset(key) is None:
        raise HTTPException(404, "数据集不存在")
    return {"ok": hub.delete(key)}


@router.post("/sync", summary="同步连接器数据（wait=false 时后台执行，数据集状态显示进度）")
def sync(payload: SyncIn) -> dict:
    hub = _hub()
    kw = {
        "symbols": payload.symbols or None,
        "start": payload.start,
        "end": payload.end,
        "period": payload.period,
        "series": payload.series,
        "name": payload.name,
    }
    try:
        if not payload.wait:
            if payload.connector not in {c["id"] for c in CONNECTORS} or payload.connector == "csv":
                raise ValueError("这个连接器不能同步")
            needs = next((c["needs_key"] for c in CONNECTORS if c["id"] == payload.connector), False)
            if needs and not get_services().read_secrets().get(f"datahub:{payload.connector}"):
                raise ValueError("这个连接器需要先填写密钥")
            hub.sync_async(payload.connector, **kw)
            return {"ok": True, "queued": True}
        return hub.sync(payload.connector, **kw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class AutoSyncIn(BaseModel):
    enabled: bool
    interval_hours: float = Field(6, ge=0.5, le=168)


@router.get("/auto", summary="研究数据自动同步：是否开启、间隔、上次运行结果")
def auto_status() -> dict:
    hub = _hub()
    return {**hub.auto_settings(), "due": hub.auto_due()}


@router.post("/auto", summary="开启 / 关闭研究数据自动同步（worker 按间隔把已有数据集补到最新）")
def auto_set(payload: AutoSyncIn) -> dict:
    return _hub().set_auto(enabled=payload.enabled, interval_hours=payload.interval_hours)


@router.post("/auto/run", summary="立即把已有数据集补到最新（后台执行）")
def auto_run() -> dict:
    import contextvars
    import threading

    hub = _hub()
    ctx = contextvars.copy_context()
    threading.Thread(
        target=ctx.run, args=(lambda: hub.auto_sync(force=True),), daemon=True, name="datahub-auto"
    ).start()
    return {"ok": True, "queued": True}


@router.get("/events", summary="事件流：交易所公告、新闻（含标注器与回看偏差标记）、宏观日程，按发布时间倒序")
def list_events(kind: str | None = None, coin: str | None = None, limit: int = 200) -> dict:
    from ..research.events import EventHub

    ev = EventHub(get_services().store, _hub())
    rows = ev.events(kind=kind, coin=coin, limit=max(1, min(limit, 1000)))
    return {"events": rows, "total": len(get_services().store.list("datahub_events"))}


def _recorder():
    from ..research.events import LiquidationRecorder

    s = get_services()
    key = str(getattr(s.store, "root", id(s.store)))
    rec = _RECORDERS.get(key)
    if rec is None:
        rec = _RECORDERS[key] = LiquidationRecorder(DataHub(s.store))
    return rec


class StreamIn(BaseModel):
    action: Literal["start", "stop", "flush"]


@router.get("/liquidations", summary="强平流记录器状态：是否在记录、收到的推送数、最近一条的时间")
def liquidations_status() -> dict:
    rec = _recorder()
    return {"stats": rec.stats, "pending_buckets": len(rec.buckets)}


@router.post("/liquidations", summary="开始 / 停止记录币安全市场强平流，或把已结束的小时写进数据集")
def liquidations_control(payload: StreamIn) -> dict:
    rec = _recorder()
    if payload.action == "start":
        if not rec.start():
            raise HTTPException(400, rec.stats.get("error") or "无法启动")
    elif payload.action == "stop":
        rec.stop()
        rec.flush()
    else:
        rec.flush()
    return {"stats": rec.stats, "pending_buckets": len(rec.buckets)}


@router.post("/import", summary="导入自定义 CSV：ts,value[,available_at]，在因子里用 x_<名字> 引用")
def import_csv(payload: CsvIn) -> dict:
    try:
        rec = _hub().import_csv(
            payload.text,
            name=payload.name,
            symbol=(payload.symbol or "").upper() or None,
            lag_ms=int(payload.lag_hours * 3_600_000),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"dataset": rec}


@router.post("/keys", summary="填写或清除连接器密钥（部署管理员）")
def set_key(payload: KeyIn) -> dict:
    if not context.current().is_admin:
        raise HTTPException(403, "只有部署管理员可以修改连接器密钥")
    if payload.connector not in KEYED:
        raise HTTPException(400, "这个连接器不需要密钥")
    s = get_services()
    secrets = s.read_secrets()
    name = f"datahub:{payload.connector}"
    if payload.key.strip():
        secrets[name] = payload.key.strip()
    else:
        secrets.pop(name, None)
    s.write_secrets(secrets)
    return {"ok": True, "has_key": bool(payload.key.strip())}
