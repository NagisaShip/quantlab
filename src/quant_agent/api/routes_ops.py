"""长期 Agent、通知与总览路由（对应增补设计第 5 章）。"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..agents.long_running import DailyDigestAgent, DigestData, StrategyMaintainerAgent
from ..notify.channels import Notification
from ..platform import context as _ctx
from ..platform.db import dstr
from ..platform.errors import AppError, ErrorCode
from ..store import Collections
from .deps import get_services
from .schemas import DigestRunIn, MaintainerRunIn

router = APIRouter(prefix="/api", tags=["长期 Agent 与总览"])


# ---------------------------------------------------------------------------
# 数据汇总：从存储里算出简报需要的数字
# ---------------------------------------------------------------------------
def collect_digest_data() -> DigestData:
    s = get_services()
    runs = s.store.list(Collections.RUNS, limit=200)
    today = datetime.now().strftime("%Y-%m-%d")
    if not runs:
        return DigestData(date=today, nav=10_000.0, period_change=0.0, period="7d", alerts=["尚无实验记录"])

    latest = runs[0]
    metrics = latest.get("metrics", {}) or {}
    initial = latest.get("initial_cash", 10_000.0) or 10_000.0
    final = metrics.get("final_equity", initial)

    alerts: list[str] = []
    # 数据来源标注（模拟 vs 真实）
    source = latest.get("data_source", "unknown")
    if source == "synthetic":
        alerts.append("行情为合成数据，仅用于流程演示")
    # 跨实验的一致性检查
    for r in runs[:20]:
        if r.get("scenario") and r["scenario"] != "baseline":
            continue
        if not (r.get("metrics") or {}).get("observations"):
            alerts.append(f"实验 {r.get('id')} 缺少观测数据")

    recent = [r for r in runs[:5] if (r.get("metrics") or {}).get("observations")]
    if recent:
        changes = [
            (r["metrics"].get("final_equity", initial) - (r.get("initial_cash") or initial))
            / (r.get("initial_cash") or initial)
            for r in recent
        ]
        period_change = sum(changes) / len(changes)
    else:
        period_change = 0.0

    return DigestData(
        date=today,
        nav=round(final, 2),
        period_change=round(period_change, 4),
        period="近 5 次实验均值",
        max_drawdown=round(metrics.get("max_drawdown", 0.0), 4),
        trades=int(metrics.get("trades", 0)),
        win_rate=round(metrics.get("win_rate") or 0.0, 4),
        alerts=alerts[:5],
    )


def collect_maintainer_evidence() -> dict:
    s = get_services()
    runs = s.store.list(Collections.RUNS, limit=200)
    baseline = [r for r in runs if (r.get("scenario") or "baseline") == "baseline" and r.get("metrics")]
    by_strategy: dict[str, list[dict]] = {}
    for r in baseline:
        by_strategy.setdefault(r.get("strategy", "unknown"), []).append(r)

    summary = {}
    for name, items in by_strategy.items():
        rets = [i["metrics"].get("total_return", 0.0) for i in items]
        mdds = [i["metrics"].get("max_drawdown", 0.0) for i in items]
        sharpes = [i["metrics"].get("sharpe") for i in items if i["metrics"].get("sharpe") is not None]
        summary[name] = {
            "runs": len(items),
            "avg_return": sum(rets) / len(rets) if rets else 0.0,
            "return_std": _std(rets),
            "avg_max_drawdown": sum(mdds) / len(mdds) if mdds else 0.0,
            "avg_sharpe": sum(sharpes) / len(sharpes) if sharpes else None,
            "latest_params": items[0].get("params", {}),
        }

    return {
        "period": datetime.now().strftime("%Y-W%W"),
        "strategies": summary,
        "note": "滚动前推验证窗口与样本外数据需在真实数据接入后补充；此处为已存实验的聚合。",
    }


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


# ---------------------------------------------------------------------------
# 长期 Agent 运行
# ---------------------------------------------------------------------------
@router.post("/long-running/digest/run")
def run_digest(payload: DigestRunIn) -> dict:
    s = get_services()
    agent = DailyDigestAgent(
        router=s.router,
        hub=s.hub,
        collect_data=collect_digest_data,
        channel=payload.channel,
    )
    record = agent.run_once(date=payload.date, dry_run=payload.dry_run)
    saved = s.store.upsert(Collections.NOTIFICATIONS, record.to_dict())
    return {"record": saved, "dry_run": payload.dry_run}


@router.post("/long-running/maintainer/run")
def run_maintainer(payload: MaintainerRunIn) -> dict:
    s = get_services()
    agent = StrategyMaintainerAgent(
        router=s.router,
        hub=s.hub,
        collect_evidence=collect_maintainer_evidence,
        channel=payload.channel,
    )
    record = agent.run_once(period=payload.period, dry_run=payload.dry_run)
    saved = s.store.upsert(Collections.NOTIFICATIONS, record.to_dict())
    return {"record": saved, "dry_run": payload.dry_run}


@router.get("/long-running/preview")
def preview_digest() -> dict:
    """不调用 LLM，直接给出简报所需的结构化数据，便于前端预览。"""
    return {"digest_data": collect_digest_data().__dict__, "evidence": collect_maintainer_evidence()}


@router.get("/notifications")
def list_notifications(limit: int = Query(50, ge=1, le=500)) -> dict:
    s = get_services()
    return {
        "notifications": s.store.list(Collections.NOTIFICATIONS, limit=limit),
        "pending": [n.__dict__ for n in s.hub.pending],
    }


@router.post("/notifications/test")
def test_notification(channel: str = "console") -> dict:
    s = get_services()
    note = Notification(
        title="通知通道测试",
        body="这是一条测试消息。\n\n仅为研究结果，非投资建议。",
        channel=channel,
    )
    ok = s.hub.send(note)
    return {"ok": ok, "channel": channel, "pending": len(s.hub.pending)}


@router.post("/notifications/flush")
def flush_notifications() -> dict:
    s = get_services()
    return {"sent": s.hub.flush(), "pending": len(s.hub.pending)}


# ---------------------------------------------------------------------------
# 总览
# ---------------------------------------------------------------------------
@router.get("/overview")
def overview() -> dict:
    s = get_services()
    runs = s.store.list(Collections.RUNS, limit=20)
    strategies = s.store.list(Collections.STRATEGIES, limit=50)
    team_runs = s.teams.list_runs(limit=5)
    latest_metrics = (runs[0].get("metrics") if runs else {}) or {}

    return {
        "counts": {
            "runs": s.store.count(Collections.RUNS),
            "strategies": s.store.count(Collections.STRATEGIES),
            "roles": s.store.count(Collections.ROLES),
            "teams": s.store.count(Collections.TEAMS),
            "team_runs": s.store.count(Collections.TEAM_RUNS),
            "notifications": s.store.count(Collections.NOTIFICATIONS),
            "batches": s.store.count(Collections.BATCHES),
            "datasets": s.store.count(Collections.DATASETS),
        },
        "latest_run": {
            "id": runs[0].get("id") if runs else None,
            "strategy": runs[0].get("strategy") if runs else None,
            "symbol": runs[0].get("symbol") if runs else None,
            "timeframe": runs[0].get("timeframe") if runs else None,
            "data_source": runs[0].get("data_source") if runs else None,
            "metrics": latest_metrics,
        },
        "recent_runs": [
            {
                "id": r.get("id"),
                "strategy": r.get("strategy"),
                "symbol": r.get("symbol"),
                "timeframe": r.get("timeframe"),
                "data_source": r.get("data_source"),
                "created_at": r.get("created_at"),
                "total_return": (r.get("metrics") or {}).get("total_return"),
                "max_drawdown": (r.get("metrics") or {}).get("max_drawdown"),
            }
            for r in runs[:8]
        ],
        "pending_strategies": [x for x in strategies if not x.get("human_approved")][:8],
        "recent_team_runs": [
            {
                "id": t.get("id"),
                "team_name": t.get("team_name"),
                "topic": t.get("topic"),
                "mode": t.get("mode"),
                "status": t.get("status"),
                "created_at": t.get("created_at"),
            }
            for t in team_runs
        ],
        "models": s.router.available_models(),
        "routing": s.router.config.routing,
        "disclaimer": "仅为研究结果，非投资建议。",
    }


@router.get("/healthz")
def healthz() -> dict:
    """容器探活：只检查数据库可用，不访问交易所（避免外网抖动导致容器被反复重启）。"""
    s = get_services()
    s.db.scalar("SELECT 1 AS ok")
    return {"status": "ok", "backend": s.db.dialect, "time": int(time.time() * 1000)}


@router.get("/health")
def health() -> dict:
    s = get_services()
    adapters = {}
    for name, adapter in s.market.adapters.items():
        try:
            adapters[name] = {"reachable": bool(adapter.ping())}
        except Exception as e:
            adapters[name] = {"reachable": False, "error": str(e)[:120]}
    return {
        "status": "ok",
        "time": int(time.time() * 1000),
        "adapters": adapters,
        "synthetic_fallback": s.market.allow_synthetic,
    }


# ---------------------------------------------------------------------------
# 简易调度器：把长期 Agent 挂到 cron 上（进程内，随服务运行）
# ---------------------------------------------------------------------------
class CronScheduler:
    """极简 5 字段 cron 匹配（分 时 日 月 周）。够用即可，复杂调度交给外部系统。"""

    def __init__(self) -> None:
        pass

    @staticmethod
    def _match(field: str, value: int) -> bool:
        if field == "*":
            return True
        for part in field.split(","):
            if part.startswith("*/"):
                step = int(part[2:])
                if step and value % step == 0:
                    return True
            elif "-" in part:
                a, b = part.split("-", 1)
                if int(a) <= value <= int(b):
                    return True
            elif part.isdigit() and int(part) == value:
                return True
        return False

    def matches(self, cron: str, now: datetime) -> bool:
        parts = cron.split()
        if len(parts) != 5:
            return False
        minute, hour, dom, month, dow = parts
        return (
            self._match(minute, now.minute)
            and self._match(hour, now.hour)
            and self._match(dom, now.day)
            and self._match(month, now.month)
            and self._match(dow, now.weekday())
        )


def due_schedules(services: Any, now: datetime | None = None, *, workspace_ids: list[str] | None = None) -> list[dict]:
    """（worker 周期调用）扫描所有工作区的定时任务，到期的登记为 run。

    幂等键 = sched:<任务>:<分钟>：多个 worker 同时扫描也只会登记一次。
    """
    from ..platform import context as _ctx

    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%dT%H:%M")
    created: list[dict] = []
    rows = [{"id": w} for w in workspace_ids] if workspace_ids else services.db.fetchall("SELECT id FROM workspaces")
    for row in rows:
        with _ctx.bind(row["id"]):
            for job in services.store.list(Collections.SCHEDULES):
                if not job.get("enabled", True) or not scheduler.matches(job.get("cron", ""), now):
                    continue
                run = services.runs.create(
                    workspace_id=row["id"],
                    user_id=job.get("created_by") or "scheduler",
                    kind="agent_job",
                    mode="agent",
                    title=f"定时任务 · {job['kind']}",
                    manifest={"job": job, "fired_at": stamp},
                    idempotency_key=f"sched:{job['id']}:{stamp}",
                )
                created.append({"job_id": job["id"], "run_id": run["id"], "fired_at": stamp})
    return created


scheduler = CronScheduler()


@router.get("/schedules")
def list_schedules() -> dict:
    return {"jobs": get_services().store.list(Collections.SCHEDULES)}


@router.post("/schedules")
def create_schedule(payload: dict) -> dict:
    from ..platform import context as _ctx

    job_id = str(payload.get("id") or f"job_{int(time.time())}")[:60]
    kind = payload.get("kind", "digest")
    if kind not in ("digest", "maintainer", "brief", "nightshift"):
        raise HTTPException(400, "kind 必须是 digest、maintainer、brief 或 nightshift")
    cron = payload.get("cron", "0 9 * * *")
    if len(cron.split()) != 5:
        raise HTTPException(400, "cron 需要 5 个字段：分 时 日 月 周")
    job = {
        "id": job_id,
        "cron": cron,
        "kind": kind,
        "channel": payload.get("channel", "console"),
        "enabled": True,
        "created_by": _ctx.current().user_id,
    }
    get_services().store.upsert(Collections.SCHEDULES, job)
    return job


@router.delete("/schedules/{job_id}")
def delete_schedule(job_id: str) -> dict:
    if not get_services().store.delete(Collections.SCHEDULES, job_id):
        raise HTTPException(404, "调度任务不存在")
    return {"ok": True}


@router.post("/schedules/tick")
def tick_schedules() -> dict:
    """手动触发一次调度检查（只看当前工作区），到期的任务登记为 run 交给 worker。"""
    from ..platform import context as _ctx
    from . import tasks

    s = get_services()
    ws = _ctx.current_workspace()
    fired = due_schedules(s, workspace_ids=[ws])
    for f in fired:
        tasks.dispatch(s, s.runs.get(f["run_id"]))
    return {"fired": fired}


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 配额计量与用量面板（T07）
#
# 口径：按「工作区 × UTC 自然日 × 计量项」累计。计量写入在 platform/quota.py，
# 全部是数据库侧的原子自增（并发不丢更新）；这里只做读取、展示与管理员调整上限。
# 所有数值对外都是十进制字符串，前端不做浮点运算。
# ---------------------------------------------------------------------------
def _quota():
    """取配额服务。挂在 services 上而不是每次新建：服务是无状态的，但少一层构造。"""
    s = get_services()
    svc = getattr(s, "quota", None)
    if svc is None:
        from ..platform.quota import QuotaService

        svc = QuotaService(s.db)
    return svc


def _require_ws_admin() -> str:
    """只有工作区管理员能改配额上限；普通成员只能看自己的用量。"""
    ctx = _ctx.current()
    if not ctx.workspace_id or ctx.role != "admin":
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以调整配额上限")
    return ctx.workspace_id


@router.get("/workspace/usage")
def workspace_usage(days: int = Query(30, ge=1, le=366)) -> dict:
    """当前工作区的用量面板数据：每个计量项的已用 / 上限 / 剩余 / 占比，外加最近 N 天走势。

    工作区只来自服务端已验证的请求上下文，不接受客户端指定工作区——
    否则就是一个"看别人用量"的越权接口。
    """
    ws = _ctx.current_workspace()
    return _quota().panel(workspace_id=ws, days=days)


@router.get("/workspace/usage/history")
def workspace_usage_history(
    days: int = Query(30, ge=1, le=366),
    metric: str | None = Query(None, description="只看某个计量项；不传则返回全部每日计量项"),
) -> dict:
    """用量走势明细（按日期升序），供面板画折线图。"""
    ws = _ctx.current_workspace()
    svc = _quota()
    return {
        "workspace_id": ws,
        "days": days,
        "metric": metric,
        "history": svc.history(workspace_id=ws, days=days, metric=metric),
    }


@router.put("/workspace/usage/limits")
def update_usage_limits(payload: dict) -> dict:
    """调整配额上限（管理员）。传 `null` 表示取消该项限制。

    为什么单独一个端点而不是复用 `/api/workspace/settings`：
    上限是"用量面板"的输入，面板要能就地改；而且这里要校验计量项键名与单位，
    和 settings 的通用 patch 语义不同（settings 里未知键会被静默忽略，这里必须报错）。
    """
    ws = _require_ws_admin()
    svc = _quota()
    limits = svc.set_limits(ws, payload or {})
    get_services().audit.record(
        "workspace.quota_limits", object_type="workspace", object_id=ws, details={"limits": limits}
    )
    return {"workspace_id": ws, "limits": limits}


@router.post("/workspace/usage/recount")
def recount_usage() -> dict:
    """重新统计当前工作区的存储占用（管理员）。

    存储占用是存量指标，只能靠重新统计得到，不能靠事件累加；
    这是**同步的目录遍历**，因此只统计当前工作区自己的目录，且带文件数上限
    （见 platform/quota.py::measure_dir），不会把请求线程拖死。
    """
    ws = _require_ws_admin()
    s = get_services()
    svc = _quota()
    root = s.store.home / "workspaces" / ws
    size = svc.measure_storage(root, workspace_id=ws)
    return {"workspace_id": ws, "storage_bytes": dstr(size)}


@router.post("/workspace/usage/check")
def check_usage(payload: dict) -> dict:
    """配额预检：给定运行类型，判断现在还能不能跑（不产生任何用量）。

    前端在"用量面板"里用它决定按钮是否可点、并提前显示原因；超限时返回
    `QUOTA_EXHAUSTED` 且 `details` 含 `used` / `limit` / `requested`。

    为什么要有这个只读接口：真正的强制在 run 入队时做（见
    `platform/quota.py::QuotaService.enforce_enqueue`），但让用户提交一次再被拒绝
    体验很差，而且会浪费一次请求体校验。预检与强制共用同一套判定逻辑，口径不会漂移。

    注意：预检是**只读**的，不会写脏计数（见 QuotaService.check 的说明）。
    """
    ws = _ctx.current_workspace()
    svc = _quota()
    body = payload or {}
    kind = str(body.get("kind") or "backtest")
    if kind not in ("backtest", "template_backtest", "optimize", "paper", "agent_job"):
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            f"未知的运行类型 {kind}",
            details={"kind": kind, "allowed": ["backtest", "template_backtest", "optimize", "paper", "agent_job"]},
        )
    # combinations 用于优化：要按本次网格的组合数判定，而不是按"一次优化"判定
    manifest = {"combinations": body["combinations"]} if body.get("combinations") is not None else None
    # 超限时这里直接抛 AppError(QUOTA_EXHAUSTED)，由统一错误处理器转成 429 + details
    svc.enforce_enqueue(kind, manifest, workspace_id=ws)
    return {
        "workspace_id": ws,
        "kind": kind,
        "allowed": True,
        "limits": svc.limits(ws),
        "note": "预检只读，不产生用量；真正的强制在运行入队时执行。",
    }


# ---------------------------------------------------------------------------
# 告警与通知（T09）
#
# 分工：API 只做「校验 + 落库 + 入队」，**一个网络请求都不发**。
# 发通知（企业微信 / Server 酱 / SMTP 都可能超时数秒）由 worker 的周期钩子
# `notify.alerts.deliver_pending` 从 alert_deliveries 里捞出来执行。
# 因此这里的每个写端点都是毫秒级返回，不会把请求线程拖住。
#
# 凭证：webhook URL / bot token / SMTP 密码全部经 SecretBox 加密后写独立文件，
# 数据库只留 secret_ref，接口只回显 masked。任何响应与审计 details 里都没有明文。
# ---------------------------------------------------------------------------
def _alerts_service():
    """取渠道凭证存储（加密目录在数据目录下的 notify-secrets）。"""
    from ..notify.alerts import ChannelSecrets

    s = get_services()
    return ChannelSecrets(s.secrets, s.home)


def _require_alert_admin() -> str:
    """改通知渠道与规则是"能把工作区数据推到外部"的操作，限管理员。"""
    ctx = _ctx.current()
    if not ctx.workspace_id or ctx.role != "admin":
        raise AppError(ErrorCode.PERMISSION_DENIED, "只有工作区管理员可以配置告警渠道与规则")
    return ctx.workspace_id


@router.get("/alerts")
def list_alerts(
    limit: int = Query(50, ge=1, le=500),
    unread_only: bool = Query(False, description="只看未读"),
) -> dict:
    """告警历史（按时间倒序）。只返回当前工作区的记录。"""
    from ..notify import alerts as alert_svc

    ws = _ctx.current_workspace()
    s = get_services()
    return {
        "alerts": alert_svc.list_alerts(s.db, ws, limit=limit, unread_only=unread_only),
        "unread": alert_svc.unread_count(s.db, ws),
        "deliveries": alert_svc.alert_deliveries_summary(s.db, ws),
        "event_types": [{"event_type": k, "label": v} for k, v in alert_svc.EVENT_LABELS.items()],
    }


@router.get("/alerts/status")
def alerts_status() -> dict:
    """告警通道健康状况：未读数、待投递数、投递成败计数。

    存在的理由：告警本身静默失败是最糟的故障——界面上必须能看出
    "有 N 条排队中但没人发"（通常是 worker 没跑）。
    """
    from ..notify import alerts as alert_svc

    ws = _ctx.current_workspace()
    s = get_services()
    return {
        "workspace_id": ws,
        "unread": alert_svc.unread_count(s.db, ws),
        "pending": alert_svc.pending_deliveries(s.db, ws),
        "deliveries": alert_svc.alert_deliveries_summary(s.db, ws),
        "channels": len(alert_svc.list_channels(s.db, ws)),
        "rules": len(alert_svc.list_rules(s.db, ws)),
        "note": "待投递数长期不下降说明 worker 未运行；通知只在 worker 里发送。",
    }


@router.post("/alerts/read")
def mark_alerts_read(payload: dict) -> dict:
    """把若干条告警标记为已读。只能标记本工作区的记录。"""
    from ..notify import alerts as alert_svc

    ws = _ctx.current_workspace()
    s = get_services()
    raw = (payload or {}).get("alert_ids") or []
    if not isinstance(raw, list) or not raw:
        raise AppError(ErrorCode.INVALID_PARAMETER, "alert_ids 必须是非空数组")
    if len(raw) > 500:
        raise AppError(ErrorCode.INVALID_PARAMETER, "一次最多标记 500 条", details={"count": len(raw)})
    marked = 0
    for item in raw:
        try:
            alert_id = int(item)
        except (TypeError, ValueError) as exc:
            raise AppError(ErrorCode.INVALID_PARAMETER, "alert_ids 必须是整数", details={"value": str(item)}) from exc
        if alert_svc.mark_read(s.db, ws, alert_id):
            marked += 1
    return {"marked": marked, "unread": alert_svc.unread_count(s.db, ws)}


@router.post("/alerts/preview")
def preview_alert(payload: dict) -> dict:
    """预览一条告警的标题与正文（不落库、不投递）。

    告警正文里放什么、脱敏到什么程度，是配置时最需要确认的东西；
    让人先看一眼再决定要不要真发，比事后从手机里翻通知便宜得多。
    """
    from ..notify import alerts as alert_svc

    ws = _ctx.current_workspace()
    s = get_services()
    kind = str((payload or {}).get("event_type") or alert_svc.EVENT_RUN_FAILED)
    return alert_svc.preview_notification(
        s.db,
        ws,
        kind,
        subject=str((payload or {}).get("subject") or ""),
        context_data=(payload or {}).get("context") or {},
    )


@router.post("/alerts/test")
def test_alert(payload: dict) -> dict:
    """按当前工作区的规则真实触发一条告警（**只入队，不在此处发送**）。

    与 `/api/notifications/test` 的区别：那个走进程内的 NotificationHub 直接发送，
    只在单机调试有意义；这个走完整的生产链路（规则匹配 → 冷却 → 入队 → worker 投递），
    因此能验证"渠道配置到底通不通"。
    """
    from ..notify import alerts as alert_svc

    ws = _require_alert_admin()
    s = get_services()
    body = payload or {}
    kind = str(body.get("event_type") or alert_svc.EVENT_RUN_FAILED)
    channel_ids = body.get("channel_ids")
    if channel_ids is not None and not isinstance(channel_ids, list):
        raise AppError(ErrorCode.INVALID_PARAMETER, "channel_ids 必须是数组")
    outcome = alert_svc.raise_alert(
        s.db,
        ws,
        kind,
        subject=str(body.get("subject") or "test"),
        context_data={"source": "手动测试", **(body.get("context") or {})},
        channels=[str(x) for x in channel_ids] if channel_ids else None,
        # 手动测试要能立刻看到效果，所以显式绕过冷却窗口
        cooldown_seconds=0,
    )
    return {"outcome": outcome.to_dict(), "note": "已入队；通知由 worker 投递，接口不等待发送结果。"}


@router.get("/notify/channels")
def list_notify_channels() -> dict:
    """通知渠道列表。**只含掩码，不含凭证**。"""
    from ..notify import alerts as alert_svc

    ws = _ctx.current_workspace()
    s = get_services()
    return {
        "channels": alert_svc.list_channels(s.db, ws),
        "kinds": [
            {"kind": "console", "label": "控制台（调试用）"},
            {"kind": "wecom", "label": "企业微信群机器人"},
            {"kind": "webhook", "label": "通用 Webhook"},
            {"kind": "serverchan", "label": "Server 酱"},
            {"kind": "pushplus", "label": "PushPlus"},
        ],
        "note": "凭证加密保存，接口只回显掩码；邮件渠道需要 SMTP 客户端，当前版本未内置。",
    }


@router.post("/notify/channels")
def create_notify_channel(payload: dict) -> dict:
    """新建通知渠道（管理员）。凭证加密落盘，接口只回显掩码。"""
    from ..notify import alerts as alert_svc

    ws = _require_alert_admin()
    s = get_services()
    body = payload or {}
    kind = str(body.get("kind") or "")
    creds = body.get("credentials") or {}
    if not isinstance(creds, dict):
        raise AppError(ErrorCode.INVALID_PARAMETER, "credentials 必须是对象")
    channel = alert_svc.create_channel(
        s.db,
        _alerts_service(),
        ws,
        user_id=_ctx.current().user_id or "system",
        kind=kind,
        name=str(body.get("name") or ""),
        config=body.get("config") or {},
        creds={str(k): str(v) for k, v in creds.items()},
        enabled=bool(body.get("enabled", True)),
    )
    # 审计只记渠道类型与掩码；凭证永不进审计
    s.audit.record(
        "notify.channel_create",
        object_type="notify_channel",
        object_id=channel["id"],
        details={"kind": channel["kind"], "masked": channel["masked"]},
    )
    return {"channel": channel}


@router.delete("/notify/channels/{channel_id}")
def delete_notify_channel(channel_id: str) -> dict:
    """删除通知渠道（管理员）：密文文件被覆写后删除，等于加密擦除。"""
    from ..notify import alerts as alert_svc

    ws = _require_alert_admin()
    s = get_services()
    if not alert_svc.delete_channel(s.db, _alerts_service(), ws, channel_id):
        raise AppError(ErrorCode.NOT_FOUND, "通知渠道不存在")
    s.audit.record("notify.channel_delete", object_type="notify_channel", object_id=channel_id)
    return {"ok": True}


@router.get("/alert-rules")
def list_alert_rules() -> dict:
    """告警规则列表（本工作区）。"""
    from ..notify import alerts as alert_svc

    ws = _ctx.current_workspace()
    s = get_services()
    return {
        "rules": alert_svc.list_rules(s.db, ws),
        "default_cooldown_seconds": alert_svc.DEFAULT_COOLDOWN_SECONDS,
    }


@router.post("/alert-rules")
def save_alert_rule(payload: dict) -> dict:
    """新建或更新一条告警规则（管理员）。

    冷却窗口默认 600 秒（T09 原文：同一事件 10 分钟内不重复发送）。
    `dedupe_scope=rule+subject` 表示同一规则下每个对象各有一条冷却窗口，
    所以"run A 失败"和"run B 失败"都会发，但 run A 连续失败 10 分钟内只发一条。
    """
    from ..notify import alerts as alert_svc

    ws = _require_alert_admin()
    s = get_services()
    body = payload or {}
    rule = alert_svc.upsert_rule(
        s.db,
        ws,
        user_id=_ctx.current().user_id or "system",
        name=str(body.get("name") or ""),
        event_type=str(body.get("event_type") or ""),
        channel_ids=body.get("channel_ids") or [],
        params=body.get("params") or {},
        cooldown_seconds=int(body.get("cooldown_seconds", alert_svc.DEFAULT_COOLDOWN_SECONDS)),
        dedupe_scope=str(body.get("dedupe_scope") or "rule+subject"),
        enabled=bool(body.get("enabled", True)),
        rule_id=str(body["id"]) if body.get("id") else None,
    )
    s.audit.record(
        "notify.rule_save",
        object_type="alert_rule",
        object_id=rule["id"],
        details={"event_type": rule["event_type"], "enabled": rule["enabled"]},
    )
    return {"rule": rule}


@router.delete("/alert-rules/{rule_id}")
def delete_alert_rule(rule_id: str) -> dict:
    """删除告警规则（管理员）。已产生的告警历史不受影响。"""
    from ..notify import alerts as alert_svc

    ws = _require_alert_admin()
    s = get_services()
    if not alert_svc.delete_rule(s.db, ws, rule_id):
        raise AppError(ErrorCode.NOT_FOUND, "告警规则不存在")
    s.audit.record("notify.rule_delete", object_type="alert_rule", object_id=rule_id)
    return {"ok": True}
