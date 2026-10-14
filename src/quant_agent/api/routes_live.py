"""真实记录与个人账户 API：CSV 导入、只读账户连接、逐笔对照、目标仓位信封。

本期 live_enabled=false：这里没有任何下单接口；只读账户客户端结构上不包含交易方法。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ..live import accounts as acc
from ..live.copy import (
    COPY_MODE_LABELS,
    COPY_MODES,
    SOURCE_CLOSED_ACTIONS,
    SOURCE_CLOSED_LABELS,
    CopyError,
    CopyPolicy,
    follower_report,
    plan_exit,
)
from ..live.envelope import ENVELOPE_SCHEMA, EnvelopeValidator, from_signal
from ..live.imports import FIELDS, MAX_BYTES, ImportError_
from ..platform import context
from ..platform.csvsafe import to_csv
from ..platform.errors import AppError, ErrorCode
from . import tasks
from .deps import get_services
from .routes_runs import enqueue

router = APIRouter(prefix="/api", tags=["真实记录与个人账户"])


def _ws() -> str:
    return context.current_workspace()


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
@router.get("/live/status")
def live_status() -> dict:
    """是否接入了真实成交。没有时前端必须显示“未接入真实成交”，不得用模拟曲线补位。"""
    s = get_services()
    out = s.live.status(_ws())
    out["connections"] = [c for c in s.accounts.list(_ws()) if c["status"] != "revoked"]
    return out


# ---------------------------------------------------------------------------
# CSV 导入
# ---------------------------------------------------------------------------
class ImportIn(BaseModel):
    kind: Literal["fills", "flows", "balances"]
    filename: str = Field("import.csv", max_length=200)
    content: str = Field(..., max_length=MAX_BYTES + 1024)
    mapping: dict[str, str] | None = None
    environment: Literal["live", "testnet"] = "live"
    exchange: str = Field("", max_length=20)


@router.get("/imports/spec")
def import_spec() -> dict:
    return {
        "kinds": FIELDS,
        "max_bytes": MAX_BYTES,
        "notes": [
            "导入是全有或全无：任何一行不合法，整份文件不入库并逐行报告原因",
            "以 = + - @ 开头且不是数字的单元格视为公式注入，整行拒绝",
            "时间支持毫秒时间戳或 UTC 时间文本；金额按十进制精确保存",
            "测试网成交请选择 environment=testnet，它们永远标为测试，不进入实盘结果",
        ],
    }


@router.post("/imports")
def create_import(payload: ImportIn) -> dict:
    s = get_services()
    try:
        imp = s.live.create_import(
            workspace_id=_ws(),
            user_id=context.current().user_id,
            kind=payload.kind,
            filename=payload.filename,
            content=payload.content,
            mapping=payload.mapping,
            environment=payload.environment,
            exchange=payload.exchange,
        )
    except ImportError_ as exc:
        raise AppError(ErrorCode.INVALID_PARAMETER, str(exc), details={"errors": exc.errors}) from exc
    s.audit.record(
        "live.import",
        object_type="import",
        object_id=imp["id"],
        details={"kind": payload.kind, "rows": imp["rows"], "environment": payload.environment},
    )
    return {"import": imp}


@router.get("/imports")
def list_imports() -> dict:
    return {"items": get_services().live.list_imports(_ws())}


@router.get("/imports/{import_id}")
def get_import(import_id: str, limit: int = Query(200, ge=1, le=5000)) -> dict:
    s = get_services()
    imp = s.live.get_import(_ws(), import_id)
    rows: list[dict[str, Any]] = []
    if imp["kind"] == "fills":
        rows = s.live.live_fills(_ws(), [imp["run_id"]])[:limit]
    elif imp["kind"] == "flows":
        rows = s.live.flows(_ws(), [imp["run_id"]])[:limit]
    return {"import": imp, "rows": rows}


@router.delete("/imports/{import_id}")
def delete_import(import_id: str) -> dict:
    s = get_services()
    s.live.delete_import(_ws(), import_id)
    s.audit.record("live.import_delete", object_type="import", object_id=import_id)
    return {"ok": True}


@router.get("/imports/{import_id}/export.csv", response_class=PlainTextResponse)
def export_import(import_id: str) -> PlainTextResponse:
    """导出已导入的成交（单元格做公式中和）。"""
    s = get_services()
    imp = s.live.get_import(_ws(), import_id)
    rows = s.live.live_fills(_ws(), [imp["run_id"]])
    cols = [
        "ts",
        "instrument",
        "side",
        "qty",
        "price",
        "fee",
        "fee_currency",
        "order_id",
        "client_order_id",
        "exchange_fill_id",
    ]
    return PlainTextResponse(
        to_csv(rows, cols),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{import_id}.csv"'},
    )


# ---------------------------------------------------------------------------
# 个人账户（只读）
# ---------------------------------------------------------------------------
@router.get("/accounts/guide")
def account_guide() -> dict:
    return {
        "exchanges": acc.GUIDES,
        "notes": acc.COMMON_NOTES,
        "unbind_notice": acc.UNBIND_NOTICE,
        "live_enabled": False,
    }


@router.get("/accounts/connections")
def list_connections() -> dict:
    return {"items": get_services().accounts.list(_ws())}


class ConnectIn(BaseModel):
    exchange: Literal["binance", "okx"]
    environment: Literal["live", "testnet"] = "live"
    label: str = Field("", max_length=60)
    api_key: str = Field(..., min_length=8, max_length=256)
    api_secret: str = Field(..., min_length=8, max_length=256)
    passphrase: str | None = Field(None, max_length=128)


@router.post("/accounts/connections")
def connect(payload: ConnectIn) -> dict:
    """测试时间、权限与账户身份；只有只读 Key 才会被（加密）保存。响应只含掩码与 connection_id。"""
    s = get_services()
    if payload.exchange == "okx" and not payload.passphrase:
        raise AppError(ErrorCode.INVALID_PARAMETER, "OKX 需要 Passphrase")
    creds = {"api_key": payload.api_key, "api_secret": payload.api_secret}
    if payload.passphrase:
        creds["passphrase"] = payload.passphrase
    try:
        conn = s.accounts.connect(
            workspace_id=_ws(),
            user_id=context.current().user_id,
            exchange=payload.exchange,
            environment=payload.environment,
            label=payload.label,
            creds=creds,
        )
    except AppError as exc:
        s.audit.record(
            "account.connect_rejected",
            object_type="account",
            object_id=payload.exchange,
            details={"reason": exc.message[:200]},
        )
        raise
    s.audit.record(
        "account.connect",
        object_type="account",
        object_id=conn["id"],
        details={"exchange": payload.exchange, "environment": payload.environment},
    )
    return {"connection": conn}


@router.post("/accounts/connections/{cid}/test")
def test_connection(cid: str) -> dict:
    s = get_services()
    conn = s.accounts.test(_ws(), cid)
    s.audit.record("account.test", object_type="account", object_id=cid, details={"status": conn["status"]})
    return {"connection": conn}


class SyncIn(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT"], max_length=20)
    since: int | None = None


@router.post("/accounts/connections/{cid}/sync")
def sync_connection(cid: str, payload: SyncIn) -> dict:
    """同步余额与成交（worker 执行）。"""
    s = get_services()
    row = s.accounts.get(_ws(), cid)
    if row["status"] != "active":
        raise AppError(ErrorCode.CREDENTIAL_REVOKED, f"连接状态为 {row['status']}，不能同步")
    mode = "testnet" if row["environment"] == "testnet" else "readonly_live"
    run = enqueue(
        kind="account_sync",
        mode=mode,
        title=f"同步账户 · {row['label']}",
        manifest={
            "connection_id": cid,
            "symbols": [x.upper() for x in payload.symbols],
            "since": payload.since,
            "exchange": row["exchange"],
        },
    )
    run = tasks.dispatch(s, run, wait_seconds=20)
    tasks.raise_if_failed(run)
    return {"run": {k: run[k] for k in ("id", "state", "mode", "summary") if k in run}}


@router.get("/accounts/connections/{cid}/watermarks")
def connection_watermarks(cid: str) -> dict:
    """该连接每个交易对的成交同步水位（分页游标 + 时间 + 页数）。

    T01 之后增量同步靠水位，用户必须能在界面上看到"同步到哪了"：
    水位长期不动、页数长期顶在上限、last_error 有值，都是需要人介入的信号，
    藏在日志里等于没有。
    """
    s = get_services()
    ws = _ws()
    # 先按 workspace 取连接：不存在（含"存在但属于别的工作区"）一律 404，
    # 绝不按 connection_id 直接查水位表——那样跨工作区就能读到别人的同步进度。
    s.accounts.get(ws, cid)
    rows = s.accounts.watermarks(ws, cid)
    items = [
        {
            "symbol": r["symbol"],
            "exchange": r["exchange"],
            "environment": r["environment"],
            "last_fill_id": r.get("last_fill_id"),
            "last_fill_ts": r.get("last_fill_ts"),
            "last_batch_size": r.get("last_batch_size"),
            "total_fills": r.get("total_fills"),
            "total_pages": r.get("total_pages"),
            "last_sync_at": r.get("last_sync_at"),
            "last_error": r.get("last_error"),
        }
        for r in sorted(rows.values(), key=lambda x: x["symbol"])
    ]
    return {"connection_id": cid, "items": items, "count": len(items)}


@router.delete("/accounts/connections/{cid}")
def revoke_connection(cid: str) -> dict:
    s = get_services()
    conn = s.accounts.revoke(_ws(), cid)
    s.audit.record("account.revoke", object_type="account", object_id=cid)
    return {
        "connection": conn,
        "note": "已删除平台保存的加密凭证与引用。交易所侧的 API Key 仍然有效，请到交易所后台删除或禁用该 Key。",
    }


# ---------------------------------------------------------------------------
# 对照
# ---------------------------------------------------------------------------
class CompareIn(BaseModel):
    sim_run_id: str
    live_run_ids: list[str] = Field(..., min_length=1, max_length=20)
    mapping: dict[str, str] | None = Field(
        None, description="显式映射：真实订单号 / 成交号 → 模拟 signal_id 或 fill_id"
    )
    window_minutes: int = Field(120, ge=1, le=7 * 24 * 60)


@router.post("/comparisons")
def create_comparison(payload: CompareIn) -> dict:
    s = get_services()
    run = enqueue(
        kind="compare",
        mode="analysis",
        title="实盘对照",
        manifest={
            "sim_run_id": payload.sim_run_id,
            "live_run_ids": payload.live_run_ids,
            "mapping": payload.mapping,
            "window_ms": payload.window_minutes * 60_000,
        },
    )
    run = tasks.dispatch(s, run, wait_seconds=30)
    tasks.raise_if_failed(run)
    if run["state"] != "COMPLETED":
        return {"run_id": run["id"], "state": run["state"], "comparison": None}
    return {"run_id": run["id"], "comparison": s.live.get_comparison(_ws(), run["summary"]["comparison_id"])}


@router.get("/comparisons")
def list_comparisons() -> dict:
    return {"items": get_services().live.list_comparisons(_ws())}


@router.get("/comparisons/{cid}")
def get_comparison(cid: str) -> dict:
    return {"comparison": get_services().live.get_comparison(_ws(), cid)}


# ---------------------------------------------------------------------------
# 目标仓位信封（为后续个人执行器定义；本期不执行）
# ---------------------------------------------------------------------------
@router.get("/envelopes/schema")
def envelope_schema() -> dict:
    return {
        "schema": ENVELOPE_SCHEMA,
        "live_enabled": False,
        "note": "执行器必须拒绝过期、重复或乱序的信封，并按个人资金与持仓自行计算差额",
    }


@router.get("/runs/{run_id}/envelopes")
def run_envelopes(run_id: str) -> dict:
    s = get_services()
    s.runs.require(run_id, _ws())
    sigs = s.runs.signals(run_id)
    envs = [e for e in (from_signal(x) for x in sigs) if e]
    return {"items": envs, "skipped": len(sigs) - len(envs)}


class EnvelopeCheckIn(BaseModel):
    envelopes: list[dict[str, Any]] = Field(..., max_length=1000)
    #: 可选。省略时用**服务端当前时间**判定过期——过期判定不该由客户端自证。
    now: int | None = None
    max_weight: float = Field(1.0, ge=0, le=1)


@router.post("/envelopes/validate")
def validate_envelopes(payload: EnvelopeCheckIn) -> dict:
    """按执行器规则逐个校验（演示与联调用）：过期、重复、乱序、越权一律拒绝。

    `now` 缺省取服务端时间。真实执行器应使用自己的本地时钟，而不是信封里或请求里的时间。
    """
    from ..platform.db import now_ms

    now = payload.now if payload.now is not None else now_ms()
    v = EnvelopeValidator(max_weight=payload.max_weight)
    out = []
    for e in payload.envelopes:
        ok, msg = v.check(e, now=now)
        out.append({"signal_id": e.get("signal_id"), "accepted": ok, "message": msg})
    return {"results": out, "now": now, "now_source": "client" if payload.now is not None else "server"}


# ---------------------------------------------------------------------------
# 跟单扩展（三种模式 + 源策略关闭处置 + 跟随账户业绩口径）
# ---------------------------------------------------------------------------
class CopyPolicyIn(BaseModel):
    """跟随账户的跟单策略。所有金额字段用字符串传，避免浮点误差。"""

    mode: Literal["fixed_amount", "equity_ratio", "risk_budget"] = "equity_ratio"
    fixed_amount: str = "0"
    equity_ratio: float = Field(0.0, ge=0, le=1)
    risk_budget: float = Field(0.0, ge=0, le=1)
    stop_loss_pct: float = Field(0.0, ge=0, lt=1)
    max_weight: float = Field(1.0, gt=0, le=1)
    max_gross_exposure: float = Field(1.0, gt=0, le=1)
    min_order_value: str = "0"
    on_source_closed: Literal["keep", "stop_new", "exit"] = "stop_new"


class CopyPlanIn(BaseModel):
    policy: CopyPolicyIn = Field(default_factory=CopyPolicyIn)
    envelope: dict[str, Any]
    equity: str
    price: str
    current_notional: str = "0"
    gross_exposure: float = Field(0.0, ge=0, le=1)
    source_closed: bool = False


@router.get("/copy/policy")
def copy_policy_options() -> dict:
    """可选的跟单模式与源策略关闭处置，供前端渲染表单。"""
    return {
        "modes": [{"value": m, "label": COPY_MODE_LABELS[m]} for m in COPY_MODES],
        "on_source_closed": [{"value": a, "label": SOURCE_CLOSED_LABELS[a]} for a in SOURCE_CLOSED_ACTIONS],
        "live_enabled": False,
        "notes": [
            "信封只给目标权重，不下发数量：跟随者按自己的权益折算仓位",
            "源策略的历史收益是参考信息，不得直接展示为跟随账户的收益",
            "平台本期不执行真实订单，这里只计算跟随者自己的目标仓位",
        ],
    }


@router.post("/copy/plan")
def copy_plan(payload: CopyPlanIn) -> dict:
    """按跟随账户自己的资金与持仓，算出一笔跟单的目标仓位（不真实下单）。"""
    try:
        policy = CopyPolicy(
            mode=payload.policy.mode,
            fixed_amount=payload.policy.fixed_amount,
            equity_ratio=payload.policy.equity_ratio,
            risk_budget=payload.policy.risk_budget,
            stop_loss_pct=payload.policy.stop_loss_pct,
            max_weight=payload.policy.max_weight,
            max_gross_exposure=payload.policy.max_gross_exposure,
            min_order_value=payload.policy.min_order_value,
            on_source_closed=payload.policy.on_source_closed,
        )
        plan = policy.plan(
            envelope=payload.envelope,
            equity=payload.equity,
            price=payload.price,
            current_notional=payload.current_notional,
            gross_exposure=payload.gross_exposure,
            source_closed=payload.source_closed,
        )
    except CopyError as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc
    return {"policy": policy.to_dict(), "plan": plan}


class CopyExitIn(BaseModel):
    policy: CopyPolicyIn = Field(default_factory=CopyPolicyIn)
    positions: dict[str, str] = Field(default_factory=dict)
    prices: dict[str, str] = Field(default_factory=dict)


@router.post("/copy/exit-plan")
def copy_exit_plan(payload: CopyExitIn) -> dict:
    """源策略关闭且处置为 exit 时的平仓计划（不真实下单）。"""
    try:
        policy = CopyPolicy(
            mode=payload.policy.mode,
            fixed_amount=payload.policy.fixed_amount,
            equity_ratio=payload.policy.equity_ratio,
            risk_budget=payload.policy.risk_budget,
            stop_loss_pct=payload.policy.stop_loss_pct,
            max_weight=payload.policy.max_weight,
            max_gross_exposure=payload.policy.max_gross_exposure,
            min_order_value=payload.policy.min_order_value,
            on_source_closed=payload.policy.on_source_closed,
        )
    except CopyError as exc:
        raise AppError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc
    return {"plan": plan_exit(policy, positions=payload.positions, prices=payload.prices), "live_enabled": False}


@router.get("/copy/report")
def copy_report(workspace_id: str | None = None) -> dict:
    """跟随账户业绩：只由**跟随账户自己的**成交与资金流计算。

    源策略的曲线作为参考信息返回，并明确标注不得当作跟随账户收益。
    """
    ws = workspace_id or _ws()
    s = get_services()
    runs = [r for r in s.live.live_runs(ws) if r["mode"] in ("imported_live", "readonly_live")]
    run_ids = [r["id"] for r in runs]
    fills = s.live.live_fills(ws, run_ids)
    flows = s.live.flows(ws, run_ids)
    report = follower_report(fills=fills, flows=flows)
    report["runs"] = [{"id": r["id"], "mode": r["mode"], "title": r["title"]} for r in runs]
    report["live_enabled"] = False
    return report
