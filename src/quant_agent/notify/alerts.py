"""T09 · 告警与通知。

## 目标（docs/VibeCoding任务包.md T09）

实时模拟触发风控暂停、run 失败、只读连接失效、网络判定变为 UNREACHABLE 时，
按工作区配置发送通知。验收：每种事件各一条测试；通知内容不含密钥；
同一事件 10 分钟内不重复发送。

## 为什么这样设计

**1. 事件入口只有一个：`raise_alert()`。**
四类事件发生在完全不同的地方（worker 的 run 状态机、实时模拟驱动器、只读账户同步、
网络探测周期钩子）。如果每处各写一段"拼消息 + 发通知"的代码，脱敏、冷却、重试
这三件事就会被复制四遍，且必然有一处漏掉。所以这里只暴露一个函数，
触发点只需要把"发生了什么"翻译成 `kind + subject + context` 交给它。

**2. 触发点不直接调 `raise_alert`，而是调 `raise_alert_safe`。**
告警是**旁路**：它失败绝不能把 run 弄失败、把同步弄失败。`raise_alert_safe`
吞掉所有异常并记日志，专供 worker / 驱动器 / 周期钩子这类"不能因为通知而中断"的位置。

**3. 冷却去重靠数据库，不靠内存。**
"同一事件 10 分钟内不重复发送"在部署上是跨进程的：API、worker（可能多个）、
定时任务都在写。内存里的 `last_sent` 字典在多进程下必然失效。
所以冷却状态就是 `alerts` 表里该 `dedupe_key` 最近一条的 `created_at`，
判定与写入在**同一个事务**里完成，并先取 `BEGIN IMMEDIATE`（SQLite）/ 行锁（PG）。

**4. 发通知是网络 I/O，绝不在 API 请求线程里做。**
`raise_alert()` 只做"校验 + 落库 + 入队 alert_deliveries"，一次本地事务，
毫秒级返回。真正的 HTTP 调用由 worker 的周期钩子 `drain_deliveries()` 执行。
`deliver_pending()` 就是给 worker 调的那个函数（见模块末尾"接线说明"）。

**5. 密钥只以密文存在。**
webhook URL、bot token、SMTP 密码都通过 `SecretBox` 加密后写到独立文件，
数据库里只有 `secret_ref`（一个随机文件名）。对外一律只给 `masked`。
连通知正文里的 URL 也会被 `scrub_text()` 抹掉 query 与 path 尾段——
企业微信 webhook 的 key 就在 URL 里，正文里出现一次就等于泄露一次。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..platform import context
from ..platform.db import Database, jdump, jload, now_ms
from ..platform.errors import AppError, ErrorCode
from ..platform.secretbox import SecretBox, SecretError
from .channels import ConsoleNotifier, Notification, Notifier

log = logging.getLogger("quantlab.alerts")

# ---------------------------------------------------------------------------
# 事件类型与默认冷却
# ---------------------------------------------------------------------------
#: 四类必须支持的事件（T09 原文）+ 三个平台已有落点的扩展事件。
EVENT_RUN_FAILED = "run_failed"
EVENT_PAPER_RISK_PAUSE = "paper_risk_pause"
EVENT_READONLY_CONNECTION_INVALID = "readonly_connection_invalid"
EVENT_NETWORK_UNREACHABLE = "network_unreachable"
EVENT_DRAWDOWN_EXCEEDED = "drawdown_exceeded"
EVENT_DAILY_LOSS_EXCEEDED = "daily_loss_exceeded"
EVENT_PAPER_STREAM_STALLED = "paper_stream_stalled"
EVENT_NODE_HEARTBEAT_LOST = "node_heartbeat_lost"
EVENT_CLOCK_DRIFT = "clock_drift"
EVENT_STRATEGY_DECAY = "strategy_decay"

EVENT_TYPES = (
    EVENT_RUN_FAILED,
    EVENT_PAPER_RISK_PAUSE,
    EVENT_READONLY_CONNECTION_INVALID,
    EVENT_NETWORK_UNREACHABLE,
    EVENT_DRAWDOWN_EXCEEDED,
    EVENT_DAILY_LOSS_EXCEEDED,
    EVENT_PAPER_STREAM_STALLED,
    EVENT_NODE_HEARTBEAT_LOST,
    EVENT_CLOCK_DRIFT,
    EVENT_STRATEGY_DECAY,
)

EVENT_LABELS: dict[str, str] = {
    EVENT_RUN_FAILED: "运行失败",
    EVENT_PAPER_RISK_PAUSE: "纸面模拟风控暂停",
    EVENT_READONLY_CONNECTION_INVALID: "只读连接失效",
    EVENT_NETWORK_UNREACHABLE: "行情接口不可达",
    EVENT_DRAWDOWN_EXCEEDED: "回撤超阈值",
    EVENT_DAILY_LOSS_EXCEEDED: "单日亏损超阈值",
    EVENT_PAPER_STREAM_STALLED: "纸面模拟行情断流",
    EVENT_NODE_HEARTBEAT_LOST: "运行节点失联",
    EVENT_CLOCK_DRIFT: "服务器时钟偏差",
    EVENT_STRATEGY_DECAY: "策略衰减",
}

#: 默认冷却窗口：T09 原文规定"同一事件 10 分钟内不重复发送"。
DEFAULT_COOLDOWN_SECONDS = 600

#: 各事件的默认严重级别（界面排序与颜色用）。
EVENT_SEVERITY: dict[str, str] = {
    EVENT_RUN_FAILED: "error",
    EVENT_PAPER_RISK_PAUSE: "warning",
    EVENT_READONLY_CONNECTION_INVALID: "error",
    EVENT_NETWORK_UNREACHABLE: "error",
    EVENT_DRAWDOWN_EXCEEDED: "warning",
    EVENT_DAILY_LOSS_EXCEEDED: "warning",
    EVENT_PAPER_STREAM_STALLED: "warning",
    EVENT_NODE_HEARTBEAT_LOST: "error",
    EVENT_CLOCK_DRIFT: "warning",
    EVENT_STRATEGY_DECAY: "warning",
}

#: 渠道类型 → 需要哪一类凭证。console 不需要凭证。
CHANNEL_KINDS = ("console", "webhook", "email", "wecom", "serverchan", "pushplus")
SECRET_KINDS = ("webhook", "email", "wecom", "serverchan", "pushplus")

#: 投递失败后的重试上限（次数）与退避基准（毫秒）。
MAX_ATTEMPTS = 3
BACKOFF_BASE_MS = 30_000

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
# 凭证可能出现在 URL 的 query（企业微信 key=…、Server 酱 /SCT…send）或 path 里。
# 只按"键名"过滤是不够的：企业微信 webhook 的 key 是 query 参数名 `key`，
# Server 酱的 token 直接嵌在 path 里，两者都不是 `token`/`secret` 这种显眼键名。
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"'<>）)]+")
_SENSITIVE_KEY_RE = re.compile(
    r"(secret|password|passwd|passphrase|token|api_?key|apikey|private|signature|send_?key|webhook|hook_url)",
    re.IGNORECASE,
)
#: 文本里出现的 `key=xxxx` / `token: xxxx` 一律替换。
_INLINE_CRED_RE = re.compile(
    r"(?i)\b(key|token|secret|password|passwd|passphrase|sendkey|send_key|access_token)\s*[=:]\s*([^\s&\"'<>]{4,})"
)
#: 长的高熵串（≥24 位、含大小写或数字）大概率是凭证。
_OPAQUE_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")

MASK = "***"


def mask_url(url: str) -> str:
    """把 URL 压成"只剩主机名"的形态，用于对外回显。

    `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc…` → `https://qyapi.weixin.qq.com/…`
    主机名不是凭证（它本来就在 SDK 源码里），path 与 query 才是。
    """
    text = (url or "").strip()
    if not text:
        return ""
    match = _URL_RE.match(text)
    if not match:
        return MASK
    raw = match.group(0)
    scheme, _, rest = raw.partition("://")
    host = rest.split("/", 1)[0].split("?", 1)[0]
    if not host:
        return MASK
    return f"{scheme}://{host}/…"


def mask_token(value: str) -> str:
    """短凭证的掩码：保留首尾各 2 位，中间固定 6 个星号。"""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:2]}{'*' * 6}{text[-2:]}"


def scrub_text(value: Any) -> Any:
    """把任意文本里的 URL 与凭证形态的串替换掉。

    通知正文、错误信息、渠道响应摘要都要过这一层：这些字段会进数据库、
    进接口响应、进日志，任何一处漏掉就等于把 webhook key 写进了可读文件。
    """
    if not isinstance(value, str):
        return value
    text = _URL_RE.sub(lambda m: mask_url(m.group(0)), value)
    text = _INLINE_CRED_RE.sub(lambda m: f"{m.group(1)}={MASK}", text)
    text = _OPAQUE_RE.sub(MASK, text)
    return text[:2000]


def scrub(value: Any) -> Any:
    """递归脱敏：键名命中敏感词 → 整值替换；字符串 → `scrub_text`。"""
    if isinstance(value, dict):
        return {str(k): (MASK if _SENSITIVE_KEY_RE.search(str(k)) else scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value[:50]]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def contains_secret(text: str, secrets: list[str]) -> bool:
    """自检用：正文里是否出现了任何一个明文凭证。

    这是验收项"通知内容不含密钥"的机器可判定形式——测试把真实凭证串喂进来，
    断言它一次都没出现。生产路径上 `scrub` 已经处理过，这里是最后一道保险。
    """
    for s in secrets:
        if s and s in (text or ""):
            return True
    return False


# ---------------------------------------------------------------------------
# 事件定义
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EventSpec:
    kind: str
    label: str
    severity: str
    #: 标题模板，`{subject}` 会被替换成触发对象
    title: str
    #: 建议动作：写进正文，告诉人"接下来该看什么"
    action: str
    #: 该事件是否按"对象"分别冷却（同一 run 一条、不同 run 各自一条）
    per_subject: bool = True


EVENT_SPECS: dict[str, EventSpec] = {
    EVENT_RUN_FAILED: EventSpec(
        kind=EVENT_RUN_FAILED,
        label=EVENT_LABELS[EVENT_RUN_FAILED],
        severity=EVENT_SEVERITY[EVENT_RUN_FAILED],
        title="运行失败：{subject}",
        action="到「运行」页查看失败原因与事件流；修好参数或数据后重新提交。",
    ),
    EVENT_PAPER_RISK_PAUSE: EventSpec(
        kind=EVENT_PAPER_RISK_PAUSE,
        label=EVENT_LABELS[EVENT_PAPER_RISK_PAUSE],
        severity=EVENT_SEVERITY[EVENT_PAPER_RISK_PAUSE],
        title="纸面模拟风控暂停：{subject}",
        action="风控层已停止新开仓（仍允许减仓）。检查回撤阈值与仓位上限，必要时人工恢复。",
    ),
    EVENT_READONLY_CONNECTION_INVALID: EventSpec(
        kind=EVENT_READONLY_CONNECTION_INVALID,
        label=EVENT_LABELS[EVENT_READONLY_CONNECTION_INVALID],
        severity=EVENT_SEVERITY[EVENT_READONLY_CONNECTION_INVALID],
        title="只读连接失效：{subject}",
        action="到「真实成交 → 账户连接」重新检测权限；失效的 Key 需要重新创建并只勾选读取权限。",
    ),
    EVENT_NETWORK_UNREACHABLE: EventSpec(
        kind=EVENT_NETWORK_UNREACHABLE,
        label=EVENT_LABELS[EVENT_NETWORK_UNREACHABLE],
        severity=EVENT_SEVERITY[EVENT_NETWORK_UNREACHABLE],
        title="行情接口不可达：{subject}",
        action="检查部署地网络与接口地址；网络可用性须连续实测 72 小时才可视为可用。",
    ),
    EVENT_DRAWDOWN_EXCEEDED: EventSpec(
        kind=EVENT_DRAWDOWN_EXCEEDED,
        label=EVENT_LABELS[EVENT_DRAWDOWN_EXCEEDED],
        severity=EVENT_SEVERITY[EVENT_DRAWDOWN_EXCEEDED],
        title="回撤超阈值：{subject}",
        action="复核策略与仓位；回撤是研究结论的一部分，不要用调参掩盖它。",
    ),
    EVENT_DAILY_LOSS_EXCEEDED: EventSpec(
        kind=EVENT_DAILY_LOSS_EXCEEDED,
        label=EVENT_LABELS[EVENT_DAILY_LOSS_EXCEEDED],
        severity=EVENT_SEVERITY[EVENT_DAILY_LOSS_EXCEEDED],
        title="单日亏损超阈值：{subject}",
        action="当日停止新增仓位，检查是否出现数据缺口或异常成交。",
    ),
    EVENT_PAPER_STREAM_STALLED: EventSpec(
        kind=EVENT_PAPER_STREAM_STALLED,
        label=EVENT_LABELS[EVENT_PAPER_STREAM_STALLED],
        severity=EVENT_SEVERITY[EVENT_PAPER_STREAM_STALLED],
        title="纸面模拟行情断流：{subject}",
        action="检查行情采集与 worker 心跳；断流期间的信号不会补成交。",
    ),
    EVENT_NODE_HEARTBEAT_LOST: EventSpec(
        kind=EVENT_NODE_HEARTBEAT_LOST,
        label=EVENT_LABELS[EVENT_NODE_HEARTBEAT_LOST],
        severity=EVENT_SEVERITY[EVENT_NODE_HEARTBEAT_LOST],
        title="运行节点失联：{subject}",
        action="运行中的实验超过 2 分钟没有心跳。检查服务器和 worker 容器（docker compose ps / logs worker）；"
        "节点恢复后会从检查点接着跑，停机前送出的订单先查单、不重下。",
    ),
    EVENT_CLOCK_DRIFT: EventSpec(
        kind=EVENT_CLOCK_DRIFT,
        label=EVENT_LABELS[EVENT_CLOCK_DRIFT],
        severity=EVENT_SEVERITY[EVENT_CLOCK_DRIFT],
        title="服务器时钟偏差：{subject}",
        action=(
            "平台已按交易所时间签名，但偏差过大说明服务器没开时间同步：执行 timedatectl set-ntp true 或安装 chrony。"
        ),
        per_subject=True,
    ),
    EVENT_STRATEGY_DECAY: EventSpec(
        kind=EVENT_STRATEGY_DECAY,
        label=EVENT_LABELS[EVENT_STRATEGY_DECAY],
        severity=EVENT_SEVERITY[EVENT_STRATEGY_DECAY],
        title="策略衰减：{subject}",
        action="运行表现持续跑输回测的正常区间。到「晋级关卡 → 自适应与衰减」看原因，确认后可退回上一阶段；"
        "平台不会自动停策略或降级。",
    ),
}


def event_spec(kind: str) -> EventSpec:
    spec = EVENT_SPECS.get(kind)
    if spec is None:
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            f"未知的告警事件类型：{kind}",
            details={"event_type": kind, "allowed": list(EVENT_TYPES)},
        )
    return spec


# ---------------------------------------------------------------------------
# 事件上报入口
# ---------------------------------------------------------------------------
@dataclass
class AlertOutcome:
    """`raise_alert` 的结果。调用方据此决定是否继续（一般不需要）。"""

    alert_id: int | None
    status: str  # pending | suppressed | skipped
    reason: str = ""
    deliveries: int = 0
    #: 命中的规则 id 列表（便于排查"为什么没发出去"）
    rules: list[str] = field(default_factory=list)

    @property
    def suppressed(self) -> bool:
        return self.status == "suppressed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "status": self.status,
            "reason": self.reason,
            "deliveries": self.deliveries,
            "rules": list(self.rules),
        }


def _dedupe_key(rule_id: str, scope: str, subject: str) -> str:
    if scope == "rule+subject" and subject:
        return f"{rule_id}#{subject}"
    return rule_id


def _title(spec: EventSpec, subject: str) -> str:
    return spec.title.replace("{subject}", subject or "（未指定对象）")[:200]


def _body(spec: EventSpec, *, subject: str, context_data: dict[str, Any], at: int) -> str:
    """拼中文正文。

    正文里**只放已经脱敏的上下文**，且显式标注"仅为研究结果"——
    告警是最容易被人截图转发的产物，免责声明必须自带。
    """
    from datetime import UTC, datetime

    when = datetime.fromtimestamp(at / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"事件：{spec.label}",
        f"对象：{subject or '（未指定对象）'}",
        f"时间：{when}",
    ]
    if context_data:
        lines.append("上下文：")
        for k, v in list(context_data.items())[:20]:
            lines.append(f"  - {k}: {v}")
    lines.append("")
    lines.append(f"建议：{spec.action}")
    lines.append("")
    lines.append("仅为研究结果，非投资建议。")
    return "\n".join(lines)


def raise_alert(
    db: Database,
    workspace_id: str,
    kind: str,
    *,
    subject: str = "",
    context_data: dict[str, Any] | None = None,
    severity: str | None = None,
    title: str | None = None,
    body: str | None = None,
    channels: list[str] | None = None,
    cooldown_seconds: int | None = None,
    now: int | None = None,
) -> AlertOutcome:
    """**唯一的事件上报入口**：把"发生了什么"变成一条告警 + 若干条待投递记录。

    参数
    ----
    db / workspace_id
        数据库与工作区。所有查询与写入都按 `workspace_id` 过滤。
    kind
        `EVENT_TYPES` 之一（run_failed / paper_risk_pause /
        readonly_connection_invalid / network_unreachable / …）。
    subject
        触发对象标识（run_id、session_id、connection_id、provider）。
        冷却默认按"规则 + 对象"维度，所以同一个 run 反复失败只发一条。
    context_data
        结构化上下文（进度、错误码、回撤比例…）。写入前统一 `scrub`。
    channels
        显式指定渠道 id；不给就用规则上配的渠道，规则也没配就落到工作区默认渠道。
    cooldown_seconds
        覆盖规则的冷却窗口；不给就用规则上的（默认 600 秒 = 10 分钟）。

    返回
    ----
    `AlertOutcome`。`status == "suppressed"` 表示命中冷却窗口，只留痕不投递。

    并发
    ----
    冷却判定与写入在同一个事务里；SQLite 走 `BEGIN IMMEDIATE`（`Database.tx` 已保证），
    PostgreSQL 走行级写锁。因此 N 个进程同时触发同一事件时，只有第一个会入队投递。
    """
    spec = event_spec(kind)
    at = now if now is not None else now_ms()
    ws = str(workspace_id or "")
    if not ws:
        raise AppError(ErrorCode.INVALID_PARAMETER, "告警必须绑定工作区")

    ctx = scrub(context_data or {})
    subj = scrub_text(subject or "")[:200]

    with db.tx() as t:
        rules = _match_rules(t, ws, kind)
        if not rules:
            # 没有匹配规则：事件仍然留痕（status=skipped），但不投递。
            # 为什么留痕：界面要能回答"这个事件到底发生过没有"，
            # 而"没配规则"和"没发生"是两件完全不同的事。
            alert_id = _insert_alert(
                t,
                workspace_id=ws,
                rule_id=None,
                kind=kind,
                severity=severity or spec.severity,
                title=(title or _title(spec, subj))[:200],
                body=body or _body(spec, subject=subj, context_data=ctx, at=at),
                subject=subj,
                context_data=ctx,
                status="skipped",
                dedupe_key="",
                suppressed_by=None,
                at=at,
            )
            return AlertOutcome(alert_id=alert_id, status="skipped", reason="没有匹配的告警规则")

        # 一条事件可能命中多条规则（例如"所有 run 失败" + "这个策略的 run 失败"）。
        # 逐条规则独立判定冷却：它们的渠道与窗口可以不同。
        #
        # 汇总状态的优先级：只要**有一条规则真的入队了**，整体就是 pending
        # （有东西要发出去）；否则如果全部被冷却压住，才是 suppressed。
        # 反过来的话，一条通配规则被压住会把另一条真正入队的规则也标成"没发"。
        first_id: int | None = None
        any_pending = False
        any_suppressed = False
        reason = ""
        deliveries = 0
        hit_rules: list[str] = []
        for rule in rules:
            hit_rules.append(rule["id"])
            scope = rule.get("dedupe_scope") or "rule"
            window = int(cooldown_seconds if cooldown_seconds is not None else rule.get("cooldown_seconds") or 0)
            key = _dedupe_key(rule["id"], scope, subj)
            prev = _last_sent(t, ws, key, at)
            if prev is not None and at - int(prev["created_at"]) < window * 1000:
                # 冷却窗口内：只留痕，不投递。
                alert_id = _insert_alert(
                    t,
                    workspace_id=ws,
                    rule_id=rule["id"],
                    kind=kind,
                    severity=severity or spec.severity,
                    title=(title or _title(spec, subj))[:200],
                    body=body or _body(spec, subject=subj, context_data=ctx, at=at),
                    subject=subj,
                    context_data=ctx,
                    status="suppressed",
                    dedupe_key=key,
                    suppressed_by=prev["id"],
                    at=at,
                )
                first_id = first_id or alert_id
                any_suppressed = True
                reason = f"命中冷却窗口（{window} 秒内已发送过）"
                continue

            alert_id = _insert_alert(
                t,
                workspace_id=ws,
                rule_id=rule["id"],
                kind=kind,
                severity=severity or spec.severity,
                title=(title or _title(spec, subj))[:200],
                body=body or _body(spec, subject=subj, context_data=ctx, at=at),
                subject=subj,
                context_data=ctx,
                status="pending",
                dedupe_key=key,
                suppressed_by=None,
                at=at,
            )
            first_id = first_id or alert_id
            any_pending = True
            targets = _resolve_targets(t, ws, rule, channels)
            for channel in targets:
                _insert_delivery(t, alert_id, ws, channel, at)
                deliveries += 1
            if not targets:
                reason = "规则未绑定任何可用渠道"

        if any_pending:
            status = "pending"
        elif any_suppressed:
            status = "suppressed"
        else:  # pragma: no cover - rules 非空时不可能走到
            status = "skipped"
        return AlertOutcome(
            alert_id=first_id,
            status=status,
            reason=reason,
            deliveries=deliveries,
            rules=hit_rules,
        )


def raise_alert_safe(
    db: Database,
    workspace_id: str,
    kind: str,
    *,
    subject: str = "",
    context_data: dict[str, Any] | None = None,
    **kw: Any,
) -> AlertOutcome | None:
    """**给触发点用的安全版本**：任何异常都吞掉并记日志，返回 None。

    为什么必须有这个包装：告警是旁路。worker 在把 run 标记为 FAILED 的路径上
    调它，如果这里抛异常，run 的状态就写不进去了——"因为发不出通知所以任务状态错了"
    是绝对不能接受的因果颠倒。
    """
    try:
        return raise_alert(db, workspace_id, kind, subject=subject, context_data=context_data, **kw)
    except Exception:
        log.exception("告警上报失败（已忽略，不影响主流程）：kind=%s subject=%s", kind, subject)
        return None


# ---------------------------------------------------------------------------
# 内部：规则匹配 / 冷却 / 入队
# ---------------------------------------------------------------------------
def _match_rules(t: Any, workspace_id: str, kind: str) -> list[dict[str, Any]]:
    """找出该工作区里订阅了这个事件类型、且已启用的规则。

    `event_type='*'` 是"订阅全部事件"的通配规则——没有它，每加一个事件类型
    都要人去把每条规则改一遍。
    """
    rows = t.fetchall(
        "SELECT * FROM alert_rules WHERE workspace_id=? AND enabled=1 AND (event_type=? OR event_type='*') "
        "ORDER BY created_at",
        (workspace_id, kind),
    )
    return rows


def _last_sent(t: Any, workspace_id: str, dedupe_key: str, at: int) -> dict[str, Any] | None:
    """该去重键最近一条**真正投递过**的告警。

    只看 `status IN ('pending','sent','failed')`：
    - `suppressed` 是冷却窗口内的留痕，它自己不能刷新冷却窗口，
      否则"每 9 分钟来一次事件"会让告警永远发不出去（窗口被无限续期）；
    - `skipped` 是没配规则的留痕，与投递无关。
    """
    if not dedupe_key:
        return None
    return t.fetchone(
        "SELECT id, created_at FROM alerts WHERE workspace_id=? AND dedupe_key=? "
        "AND status IN ('pending','sent','failed') AND created_at<=? ORDER BY created_at DESC LIMIT 1",
        (workspace_id, dedupe_key, at),
    )


def _insert_alert(
    t: Any,
    *,
    workspace_id: str,
    rule_id: str | None,
    kind: str,
    severity: str,
    title: str,
    body: str,
    subject: str,
    context_data: dict[str, Any],
    status: str,
    dedupe_key: str,
    suppressed_by: int | None,
    at: int,
) -> int:
    return t.insert_id(
        "INSERT INTO alerts (workspace_id, rule_id, event_type, severity, title, body, subject, context, status, "
        "suppressed_by, dedupe_key, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            workspace_id,
            rule_id,
            kind,
            severity,
            title,
            scrub_text(body),
            subject,
            jdump(context_data),
            status,
            suppressed_by,
            dedupe_key,
            at,
            at,
        ),
    )


def _resolve_targets(t: Any, workspace_id: str, rule: dict[str, Any], explicit: list[str] | None) -> list[dict]:
    """决定这条规则要投递到哪些渠道。

    优先级：调用方显式指定 > 规则绑定的渠道 > 工作区全部启用的渠道。
    最后一级兜底很重要：新建工作区时还没有规则，但"run 失败要告诉我"是本能预期。
    """
    if explicit:
        ids = [str(x) for x in explicit]
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        return t.fetchall(
            f"SELECT * FROM notify_channels WHERE workspace_id=? AND enabled=1 AND id IN ({marks})",
            [workspace_id, *ids],
        )
    bound = jload(rule.get("channel_ids"), []) or []
    if bound:
        marks = ",".join("?" for _ in bound)
        rows = t.fetchall(
            f"SELECT * FROM notify_channels WHERE workspace_id=? AND enabled=1 AND id IN ({marks})",
            [workspace_id, *bound],
        )
        if rows:
            return rows
    return t.fetchall(
        "SELECT * FROM notify_channels WHERE workspace_id=? AND enabled=1 ORDER BY created_at", (workspace_id,)
    )


def _insert_delivery(t: Any, alert_id: int, workspace_id: str, channel: dict[str, Any], at: int) -> int:
    return t.insert_id(
        "INSERT INTO alert_deliveries (alert_id, workspace_id, channel_id, channel_kind, status, attempts, "
        "max_attempts, next_attempt_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (alert_id, workspace_id, channel["id"], channel["kind"], "pending", 0, MAX_ATTEMPTS, at, at, at),
    )


# ---------------------------------------------------------------------------
# 渠道凭证（加密存放，只回显掩码）
# ---------------------------------------------------------------------------
class ChannelSecrets:
    """渠道凭证的加密存储。

    与 `live/accounts.py` 同一套做法：一个连接一个密文文件，文件名（`secret_ref`）
    是随机 hex，与数据库里的连接行解耦。撤销渠道时把文件内容覆写成 0 再删除，
    这样即使磁盘被恢复也拿不到明文（加密擦除）。
    """

    def __init__(self, secrets: SecretBox, home: Path) -> None:
        self.secrets = secrets
        self.dir = Path(home) / "notify-secrets"
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, str]) -> str:
        ref = uuid.uuid4().hex
        path = self.dir / f"{ref}.enc"
        path.write_text(self.secrets.encrypt(json.dumps(payload)), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return ref

    def read(self, ref: str | None) -> dict[str, str]:
        if not ref:
            return {}
        path = self.dir / f"{ref}.enc"
        if not path.exists():
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, "该渠道的凭证已删除，请重新配置")
        try:
            return json.loads(self.secrets.decrypt(path.read_text(encoding="utf-8")))
        except SecretError as exc:
            raise AppError(ErrorCode.CREDENTIAL_REVOKED, str(exc)) from exc

    def delete(self, ref: str | None) -> None:
        if not ref:
            return
        path = self.dir / f"{ref}.enc"
        if path.exists():
            path.write_bytes(b"\0" * path.stat().st_size)
            path.unlink()


def channel_mask(kind: str, creds: dict[str, str]) -> str:
    """渠道凭证的脱敏回显。**绝不返回明文**。"""
    if kind in ("webhook", "wecom"):
        return mask_url(creds.get("url") or creds.get("webhook_url") or "")
    if kind == "serverchan":
        return mask_token(creds.get("send_key") or "")
    if kind == "pushplus":
        return mask_token(creds.get("token") or "")
    if kind == "email":
        # SMTP 密码是凭证；用户名与主机不是，但为了不让人从掩码反推密码长度，
        # 这里只回显"已配置"形态。
        return "已配置" if creds.get("password") else ""
    return ""


# ---------------------------------------------------------------------------
# 投递（worker 侧）
# ---------------------------------------------------------------------------
def build_notifier(kind: str, config: dict[str, Any], creds: dict[str, str], *, transport: Any = None) -> Notifier:
    """按渠道类型构造 Notifier。

    复用 `notify/channels.py` 里已有的实现；`webhook` 是"通用 webhook"，
    用企业微信的 markdown 载荷发到任意 HTTP 端点，覆盖自建网关。
    """
    from .channels import PushPlusNotifier, ServerChanNotifier, WeComNotifier

    if kind == "console":
        return ConsoleNotifier()
    if kind in ("wecom", "webhook"):
        url = creds.get("url") or creds.get("webhook_url") or ""
        if not url:
            raise AppError(ErrorCode.DATA_INCOMPLETE, "该渠道缺少 webhook 地址")
        return WeComNotifier(url, transport=transport)
    if kind == "serverchan":
        key = creds.get("send_key") or ""
        if not key:
            raise AppError(ErrorCode.DATA_INCOMPLETE, "该渠道缺少 SendKey")
        return ServerChanNotifier(key, transport=transport)
    if kind == "pushplus":
        token = creds.get("token") or ""
        if not token:
            raise AppError(ErrorCode.DATA_INCOMPLETE, "该渠道缺少 token")
        return PushPlusNotifier(token, transport=transport)
    if kind == "email":
        raise AppError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "邮件渠道需要 SMTP 客户端，当前版本未内置；请改用 webhook / 企业微信 / Server 酱 / PushPlus",
        )
    raise AppError(ErrorCode.INVALID_PARAMETER, f"未知的渠道类型：{kind}")


class DeliveryService:
    """投递 worker：把 `alert_deliveries` 里的待投递记录真正发出去。

    只做三件事：认领一批待投递 → 逐条调用 Notifier → 写回结果（成功 / 退避重试 / 最终失败）。
    所有网络调用都在这里，**绝不在 API 请求线程里**。
    """

    def __init__(self, db: Database, secrets: SecretBox, home: Path, *, transport: Any = None) -> None:
        self.db = db
        self.creds = ChannelSecrets(secrets, home)
        self.transport = transport  # 测试注入 httpx.MockTransport

    # ---- 认领 ----
    def _claim(self, workspace_id: str | None, limit: int, at: int) -> list[dict[str, Any]]:
        """取出一批到期的待投递记录，并原子地标记为 sending。

        标记成 `sending` 而不是留在 `pending`：多 worker 并发时，
        两个进程同时读到同一条会重复发送（用户收到两条一样的告警）。
        SQLite 下 `Database.tx` 已用 `BEGIN IMMEDIATE` 串行化写事务，
        PostgreSQL 下这里用 `FOR UPDATE SKIP LOCKED` 避免互相等待。
        """
        sql = (
            "SELECT d.*, c.kind AS c_kind, c.config AS c_config, c.secret_ref AS c_secret_ref, "
            "a.title AS a_title, a.body AS a_body, a.event_type AS a_event_type, a.severity AS a_severity "
            "FROM alert_deliveries d "
            "JOIN notify_channels c ON c.id = d.channel_id "
            "JOIN alerts a ON a.id = d.alert_id "
            "WHERE d.status='pending' AND d.next_attempt_at<=?"
        )
        params: list[Any] = [at]
        if workspace_id:
            sql += " AND d.workspace_id=?"
            params.append(workspace_id)
        sql += " ORDER BY d.id LIMIT ?"
        params.append(min(limit, 200))
        if self.db.dialect == "postgres":
            sql += " FOR UPDATE SKIP LOCKED"
        with self.db.tx() as t:
            rows = t.fetchall(sql, params)
            for row in rows:
                t.execute(
                    "UPDATE alert_deliveries SET status='sending', updated_at=? WHERE id=? AND status='pending'",
                    (at, row["id"]),
                )
        return rows

    # ---- 主循环 ----
    def deliver_pending(
        self, *, workspace_id: str | None = None, limit: int = 50, now: int | None = None
    ) -> dict[str, Any]:
        """投递一批待发通知。worker 周期调用（建议 30~60 秒一次）。

        返回 `{"sent": n, "retry": n, "failed": n}`，便于 worker 日志与测试断言。
        """
        at = now if now is not None else now_ms()
        rows = self._claim(workspace_id, limit, at)
        sent = retry = failed = 0
        touched: set[int] = set()
        for row in rows:
            ok, detail = self._send_one(row)
            touched.add(int(row["alert_id"]))
            if ok:
                sent += 1
                self._finish(row, "sent", detail, at)
                continue
            attempts = int(row["attempts"]) + 1
            if attempts >= int(row["max_attempts"]):
                failed += 1
                self._finish(row, "failed", detail, at, attempts=attempts)
            else:
                retry += 1
                # 指数退避：30s、60s、120s… 避免上游抖动时把对方打挂。
                delay = BACKOFF_BASE_MS * (2 ** (attempts - 1))
                self.db.execute(
                    "UPDATE alert_deliveries SET status='pending', attempts=?, next_attempt_at=?, last_error=?, "
                    "last_response=?, updated_at=? WHERE id=?",
                    (attempts, at + delay, detail[:500], detail[:500], at, row["id"]),
                )
        for alert_id in touched:
            self._sync_alert_status(alert_id, at)
        return {"sent": sent, "retry": retry, "failed": failed, "claimed": len(rows)}

    def _send_one(self, row: dict[str, Any]) -> tuple[bool, str]:
        """真正发一条。返回 (是否成功, 已脱敏的响应摘要)。

        异常一律转成"失败 + 脱敏摘要"：投递失败要能重试，而不是把 worker 的
        周期钩子整个打断（周期钩子抛异常会被 worker 捕获记录，但这一批剩下的
        通知就都发不出去了）。
        """
        try:
            creds = self.creds.read(row.get("c_secret_ref"))
            notifier = build_notifier(
                row["c_kind"], jload(row.get("c_config"), {}) or {}, creds, transport=self.transport
            )
            note = Notification(
                title=str(row.get("a_title") or "告警"),
                body=str(row.get("a_body") or ""),
                channel=row["c_kind"],
            )
            ok = bool(notifier.send(note))
            return ok, "ok" if ok else "渠道返回失败"
        except AppError as exc:
            return False, scrub_text(exc.message)
        except Exception as exc:
            # 异常文本可能含 URL（httpx 的连接错误会带上目标地址），必须脱敏
            return False, scrub_text(f"{type(exc).__name__}: {exc}")[:500]

    def _finish(self, row: dict[str, Any], status: str, detail: str, at: int, *, attempts: int | None = None) -> None:
        self.db.execute(
            "UPDATE alert_deliveries SET status=?, attempts=?, last_error=?, last_response=?, updated_at=?, sent_at=? "
            "WHERE id=?",
            (
                status,
                attempts if attempts is not None else int(row["attempts"]) + 1,
                None if status == "sent" else detail[:500],
                detail[:500],
                at,
                at if status == "sent" else None,
                row["id"],
            ),
        )

    def _sync_alert_status(self, alert_id: int, at: int) -> None:
        """把告警的汇总状态对齐到它所有投递记录。

        为什么不让 `raise_alert` 直接写 `sent`：入队那一刻还没发出去，
        写成 sent 是撒谎。只有这里（投递结束后）才敢下结论。
        """
        row = self.db.fetchone(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS bad "
            "FROM alert_deliveries WHERE alert_id=?",
            (alert_id,),
        )
        if not row:
            return
        total = int(row["total"] or 0)
        ok = int(row["ok"] or 0)
        bad = int(row["bad"] or 0)
        if total and ok == total:
            status = "sent"
        elif bad and ok + bad >= total:
            status = "failed"
        else:
            status = "pending"
        self.db.execute("UPDATE alerts SET status=?, updated_at=? WHERE id=?", (status, at, alert_id))


# ---------------------------------------------------------------------------
# 规则与渠道的读写（供 API 层使用）
# ---------------------------------------------------------------------------
def list_rules(db: Database, workspace_id: str) -> list[dict[str, Any]]:
    rows = db.fetchall("SELECT * FROM alert_rules WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,))
    return [_rule_out(r) for r in rows]


def _rule_out(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["params"] = jload(row.get("params"), {}) or {}
    out["channel_ids"] = jload(row.get("channel_ids"), []) or []
    out["enabled"] = bool(row.get("enabled"))
    out["event_label"] = EVENT_LABELS.get(row.get("event_type") or "", row.get("event_type") or "")
    return out


def upsert_rule(
    db: Database,
    workspace_id: str,
    *,
    user_id: str,
    name: str,
    event_type: str,
    channel_ids: list[str] | None = None,
    params: dict[str, Any] | None = None,
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    dedupe_scope: str = "rule+subject",
    enabled: bool = True,
    rule_id: str | None = None,
) -> dict[str, Any]:
    """新建或更新一条告警规则。所有校验在这里，API 层只做转发。"""
    if event_type != "*":
        event_spec(event_type)  # 未知类型直接 400，而不是静默存进去永不触发
    if dedupe_scope not in ("rule", "rule+subject"):
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            "dedupe_scope 只能是 rule 或 rule+subject",
            details={"dedupe_scope": dedupe_scope},
        )
    if not 0 <= int(cooldown_seconds) <= 86_400:
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            "冷却窗口应在 0~86400 秒之间（0 表示不去重）",
            details={"cooldown_seconds": cooldown_seconds},
        )
    if not (name or "").strip():
        raise AppError(ErrorCode.INVALID_PARAMETER, "规则名称不能为空")
    ids = [str(x) for x in (channel_ids or [])]
    if ids:
        marks = ",".join("?" for _ in ids)
        found = {
            r["id"]
            for r in db.fetchall(
                f"SELECT id FROM notify_channels WHERE workspace_id=? AND id IN ({marks})", [workspace_id, *ids]
            )
        }
        missing = [x for x in ids if x not in found]
        if missing:
            # 不区分"不存在"和"别的 workspace 的"：避免探测其他工作区的渠道 id
            raise AppError(
                ErrorCode.INVALID_PARAMETER,
                "引用了不存在或不属于本工作区的通知渠道",
                details={"channel_ids": missing},
            )
    at = now_ms()
    rid = rule_id or f"rule-{uuid.uuid4().hex[:12]}"
    existing = db.fetchone("SELECT * FROM alert_rules WHERE id=? AND workspace_id=?", (rid, workspace_id))
    if existing:
        db.execute(
            "UPDATE alert_rules SET name=?, event_type=?, params=?, channel_ids=?, enabled=?, cooldown_seconds=?, "
            "dedupe_scope=?, updated_at=? WHERE id=? AND workspace_id=?",
            (
                name.strip()[:100],
                event_type,
                jdump(params or {}),
                jdump(ids),
                int(bool(enabled)),
                int(cooldown_seconds),
                dedupe_scope,
                at,
                rid,
                workspace_id,
            ),
        )
    else:
        db.execute(
            "INSERT INTO alert_rules (id, workspace_id, created_by, name, event_type, params, channel_ids, enabled, "
            "cooldown_seconds, dedupe_scope, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                workspace_id,
                user_id,
                name.strip()[:100],
                event_type,
                jdump(params or {}),
                jdump(ids),
                int(bool(enabled)),
                int(cooldown_seconds),
                dedupe_scope,
                at,
                at,
            ),
        )
    return _rule_out(db.fetchone("SELECT * FROM alert_rules WHERE id=?", (rid,)))


def delete_rule(db: Database, workspace_id: str, rule_id: str) -> bool:
    return bool(db.execute("DELETE FROM alert_rules WHERE id=? AND workspace_id=?", (rule_id, workspace_id)))


def list_channels(db: Database, workspace_id: str) -> list[dict[str, Any]]:
    rows = db.fetchall("SELECT * FROM notify_channels WHERE workspace_id=? ORDER BY created_at DESC", (workspace_id,))
    return [_channel_out(r) for r in rows]


def _channel_out(row: dict[str, Any]) -> dict[str, Any]:
    """对外形态：**只有掩码，没有 secret_ref，没有明文**。"""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row.get("name") or "",
        "enabled": bool(row.get("enabled")),
        "config": jload(row.get("config"), {}) or {},
        "masked": row.get("masked") or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_channel(
    db: Database,
    creds_store: ChannelSecrets,
    workspace_id: str,
    *,
    user_id: str,
    kind: str,
    name: str = "",
    config: dict[str, Any] | None = None,
    creds: dict[str, str] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    """新建通知渠道。凭证加密落盘，数据库只留 `secret_ref` 与掩码。"""
    if kind not in CHANNEL_KINDS:
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            f"未知的渠道类型：{kind}",
            details={"kind": kind, "allowed": list(CHANNEL_KINDS)},
        )
    creds = {k: v for k, v in (creds or {}).items() if v}
    if kind in SECRET_KINDS and not creds:
        raise AppError(ErrorCode.DATA_INCOMPLETE, f"{kind} 渠道必须提供凭证", details={"kind": kind})
    if kind == "email":
        raise AppError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "邮件渠道需要 SMTP 客户端，当前版本未内置；请改用 webhook / 企业微信 / Server 酱 / PushPlus",
        )
    cid = f"chan-{uuid.uuid4().hex[:12]}"
    at = now_ms()
    masked = channel_mask(kind, creds)
    # 先落库、后写密文：反过来的话，插入失败会留下一个没人引用的孤儿密文文件。
    db.execute(
        "INSERT INTO notify_channels (id, workspace_id, created_by, kind, name, enabled, config, secret_ref, masked, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            cid,
            workspace_id,
            user_id,
            kind,
            (name or kind)[:60],
            int(bool(enabled)),
            jdump(config or {}),
            None,
            masked,
            at,
            at,
        ),
    )
    try:
        ref = creds_store.write(creds) if creds else None
        db.execute("UPDATE notify_channels SET secret_ref=? WHERE id=?", (ref, cid))
    except Exception:
        db.execute("DELETE FROM notify_channels WHERE id=?", (cid,))
        raise
    return _channel_out(db.fetchone("SELECT * FROM notify_channels WHERE id=?", (cid,)))


def delete_channel(db: Database, creds_store: ChannelSecrets, workspace_id: str, channel_id: str) -> bool:
    """删除渠道：先删密文文件（加密擦除），再删数据库行。

    顺序不能反：先删行的话，密文文件就成了没人引用的孤儿，
    而它里面是明文可解的凭证——撤销了却还留在磁盘上，等于没撤销。
    """
    row = db.fetchone("SELECT * FROM notify_channels WHERE id=? AND workspace_id=?", (channel_id, workspace_id))
    if row is None:
        return False
    creds_store.delete(row.get("secret_ref"))
    db.execute("DELETE FROM alert_deliveries WHERE channel_id=? AND workspace_id=?", (channel_id, workspace_id))
    return bool(db.execute("DELETE FROM notify_channels WHERE id=? AND workspace_id=?", (channel_id, workspace_id)))


def list_alerts(
    db: Database, workspace_id: str, *, limit: int = DEFAULT_LIMIT, unread_only: bool = False
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM alerts WHERE workspace_id=?"
    if unread_only:
        sql += " AND read_at IS NULL"
    sql += " ORDER BY id DESC LIMIT ?"
    rows = db.fetchall(sql, (workspace_id, min(int(limit), MAX_LIMIT)))
    return [_alert_out(r) for r in rows]


def _alert_out(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["context"] = jload(row.get("context"), {}) or {}
    out["event_label"] = EVENT_LABELS.get(row.get("event_type") or "", row.get("event_type") or "")
    out["read"] = row.get("read_at") is not None
    return out


def mark_read(db: Database, workspace_id: str, alert_id: int) -> bool:
    return bool(
        db.execute(
            "UPDATE alerts SET read_at=?, updated_at=? WHERE id=? AND workspace_id=? AND read_at IS NULL",
            (now_ms(), now_ms(), int(alert_id), workspace_id),
        )
    )


def unread_count(db: Database, workspace_id: str) -> int:
    return int(db.scalar("SELECT COUNT(*) FROM alerts WHERE workspace_id=? AND read_at IS NULL", (workspace_id,)) or 0)


def pending_deliveries(db: Database, workspace_id: str) -> int:
    """待投递数量：界面显示"有 N 条通知排队中"，运维据此判断 worker 是否在跑。"""
    return int(
        db.scalar(
            "SELECT COUNT(*) FROM alert_deliveries WHERE workspace_id=? AND status IN ('pending','sending')",
            (workspace_id,),
        )
        or 0
    )


def alert_deliveries_summary(db: Database, workspace_id: str) -> dict[str, int]:
    """按状态统计投递记录。界面与运维排查共用（"卡在 pending"通常是 worker 没跑）。"""
    rows = db.fetchall(
        "SELECT status, COUNT(*) AS n FROM alert_deliveries WHERE workspace_id=? GROUP BY status", (workspace_id,)
    )
    out = {"pending": 0, "sending": 0, "sent": 0, "failed": 0}
    for r in rows:
        out[str(r["status"])] = int(r["n"] or 0)
    return out


def preview_notification(
    db: Database, workspace_id: str, kind: str, *, subject: str = "", context_data: dict[str, Any] | None = None
) -> dict[str, Any]:
    """预览一条告警会发出去的标题与正文（**不落库、不投递**）。

    存在的理由：告警正文里放什么、脱敏到什么程度，是配置时最需要确认的东西；
    让人先看一眼再决定要不要真发，比事后从手机里翻通知便宜得多。
    """
    spec = event_spec(kind)
    subj = scrub_text(subject or "")[:200]
    at = now_ms()
    ctx = scrub(context_data or {})
    return {
        "event_type": kind,
        "event_label": spec.label,
        "severity": spec.severity,
        "title": _title(spec, subj),
        "body": _body(spec, subject=subj, context_data=ctx, at=at),
        "subject": subj,
        "context": ctx,
    }


# ---------------------------------------------------------------------------
# 触发点适配（只翻译语义，不做投递）
# ---------------------------------------------------------------------------
#: 被拒绝的原因码 → 是否算"风控暂停"。与 docs/策略协议与数据口径.md 4.1 节一致。
RISK_PAUSE_CODES = ("RISK_DRAWDOWN_PAUSE",)


def raise_for_run_failure(
    db: Database,
    *,
    workspace_id: str,
    run_id: str,
    run_kind: str = "",
    mode: str = "",
    error: str = "",
    error_code: str = "",
) -> AlertOutcome | None:
    """run 失败。触发点：`platform/worker.py` 的 `_execute_batch` / `_start_resident` / `drive`。

    `error` 会进正文，所以先脱敏（异常文本里可能带 URL 或凭证）。
    """
    return raise_alert_safe(
        db,
        workspace_id,
        EVENT_RUN_FAILED,
        subject=run_id,
        context_data={
            "run_id": run_id,
            "kind": run_kind,
            "mode": mode,
            "error_code": error_code,
            "error": scrub_text(error or "")[:300],
        },
    )


def raise_for_paper_risk_pause(
    db: Database,
    *,
    workspace_id: str,
    session_id: str,
    reason_code: str,
    reason: str = "",
    drawdown: str = "",
    threshold: str = "",
) -> AlertOutcome | None:
    """实时模拟触发风控暂停。

    触发点有两处，二选一即可（都在 `api/tasks.py::PaperDriver`，不在本子代理的允许清单里）：

    a) **按原因码**（推荐，语义最准）：`tick()` 里 `step["rejected"]` 中
       `code == "RISK_DRAWDOWN_PAUSE"` 的条目——这是 `sdk/runtimes.py` 风控层
       真正拦下一笔新开仓的地方。但该字段目前**没有**从 `engine/live.py::step`
       暴露出来，需要先补一行（见交付报告"需要主代理补的接线"）。

    b) **按会话状态**（零改动，立即可用）：`PaperDriver.tick()` 在
       `session.status == "paused"` 且上一轮不是 paused 时上报一次。
       覆盖"行情过期暂停"与"风控暂停"两种暂停，正文里带上原因。
    """
    return raise_alert_safe(
        db,
        workspace_id,
        EVENT_PAPER_RISK_PAUSE,
        subject=session_id,
        context_data={
            "session_id": session_id,
            "reason_code": reason_code,
            "reason": scrub_text(reason or "")[:300],
            "drawdown": drawdown,
            "threshold": threshold,
        },
    )


def raise_for_readonly_connection_invalid(
    db: Database,
    *,
    workspace_id: str,
    connection_id: str,
    exchange: str = "",
    environment: str = "",
    reason: str = "",
) -> AlertOutcome | None:
    """只读连接失效。

    触发点：`api/tasks.py::handle_account_sync` 里 `ErrorCode.CREDENTIAL_REVOKED`
    的分支（那里已经把连接标成 `expired`），以及 `live/accounts.py::test()` 检测到
    权限不再只读时。两处都不在本子代理的允许清单里。

    `reason` 只放中文说明，**不放 Key**：`live/accounts.py` 的报错文本本身就不含凭证，
    这里再过一遍 `scrub_text` 作为保险。
    """
    return raise_alert_safe(
        db,
        workspace_id,
        EVENT_READONLY_CONNECTION_INVALID,
        subject=connection_id,
        context_data={
            "connection_id": connection_id,
            "exchange": exchange,
            "environment": environment,
            "reason": scrub_text(reason or "")[:300],
        },
    )


def unreachable_providers(db: Database) -> list[dict[str, Any]]:
    """读 `netprobe.status()` 的判定结果，返回判定为 `UNREACHABLE` 的行情接口。

    **只读**：一行都不改 `platform/netprobe.py`。worker 的周期钩子照常先调用
    `netprobe.probe_adapters(...)` 写探测记录，再调用本函数判断"判定是否变成 UNREACHABLE"，
    然后按工作区（`workspaces_with_rules`）逐个上报。

    这里复用 `netprobe.status` 而不是自己算成功率，是为了保证"界面上看到 UNREACHABLE"
    和"发出去了 UNREACHABLE 告警"用的是**同一个判定**——两处各算一遍迟早会漂移。
    """
    from ..platform import netprobe

    report = netprobe.status(db)
    return [p for p in (report.get("providers") or []) if p.get("verdict") == "UNREACHABLE"]


def raise_for_network_unreachable(
    db: Database,
    *,
    workspace_id: str,
    provider: str,
    endpoint: str = "",
    detail: str = "",
) -> AlertOutcome | None:
    """网络判定变为 UNREACHABLE。

    触发点：worker 周期钩子 `api/tasks.py::probe_network`（每 300 秒一次），
    在 `netprobe.probe_adapters(...)` 之后加三行（见交付报告"需要主代理补的接线"）。

    注意 `network_probes` 是**部署级**表（没有 workspace_id），而告警必须按工作区隔离，
    所以对每个订阅了这个事件的工作区各上报一次，用 `workspaces_with_rules()` 取订阅者；
    `endpoint` 只保留主机名（`mask_url`），避免把带凭证的完整 URL 写进告警。
    """
    return raise_alert_safe(
        db,
        workspace_id,
        EVENT_NETWORK_UNREACHABLE,
        subject=provider,
        context_data={
            "provider": provider,
            "endpoint": mask_url(endpoint) if endpoint else "",
            "detail": scrub_text(detail or "")[:300],
        },
    )


def report_unreachable_network(db: Database, *, detail: str = "") -> list[AlertOutcome]:
    """把当前所有 UNREACHABLE 的接口按订阅工作区上报一遍。**给 worker 周期钩子调用的那个函数。**

    返回每个工作区上报的结果，便于周期钩子的日志。

    为什么用 `provider` 作为 `subject`：冷却窗口按"规则 + 对象"算，
    所以"binance 不可达"和"okx 不可达"各发一条，而同一个接口 10 分钟内只发一条。
    """
    out: list[AlertOutcome] = []
    workspaces = workspaces_with_rules(db, EVENT_NETWORK_UNREACHABLE)
    if not workspaces:
        return out
    for item in unreachable_providers(db):
        provider = str(item.get("provider") or "")
        errors = item.get("recent_errors") or []
        text = detail or f"最近 1 小时全部探测失败；最近错误：{'；'.join(str(e) for e in errors[:3])}"
        for ws in workspaces:
            outcome = raise_for_network_unreachable(
                db, workspace_id=ws, provider=provider, endpoint=item.get("endpoint") or "", detail=text
            )
            if outcome is not None:
                out.append(outcome)
    return out


def delivery_service(services: Any, *, transport: Any = None) -> DeliveryService:
    """从 AppServices 构造投递服务（worker 周期钩子用）。"""
    return DeliveryService(services.db, services.secrets, services.home, transport=transport)


def deliver_pending(services: Any, *, limit: int = 50) -> dict[str, Any]:
    """worker 周期钩子：投递一批待发通知。

    接线方式（`api/tasks.py::build_worker` 的 `periodic` 列表加一项）::

        from ..notify.alerts import deliver_pending as drain_alerts
        periodic=[..., (30.0, drain_alerts)]

    放在 `notify.alerts` 而不是 `api.tasks` 里，是为了让"通知"这条链不依赖 API 层。
    """
    return delivery_service(services).deliver_pending(limit=limit)


def workspaces_with_rules(db: Database, kind: str) -> list[str]:
    """哪些工作区订阅了这个事件类型。

    给"部署级事件"（网络探测）用：探测结果是全局的，但要按订阅者逐个上报，
    否则会给完全没配通知的工作区也写一堆告警行。
    """
    rows = db.fetchall(
        "SELECT DISTINCT workspace_id FROM alert_rules WHERE enabled=1 AND (event_type=? OR event_type='*')",
        (kind,),
    )
    return [r["workspace_id"] for r in rows]


def bind_workspace(workspace_id: str):
    """给触发点用：在请求上下文之外以指定工作区身份执行告警上报。

    worker 的周期钩子与驱动器都在 `context.bind(...)` 之外跑，
    而 `raise_alert` 只按显式传入的 `workspace_id` 过滤，不读上下文——
    这里提供这个上下文管理器只是为了日志里的 trace_id 与审计一致。
    """
    return context.bind(workspace_id, user_id="system", role="admin")
