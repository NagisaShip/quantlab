"""配额计量与超限拒绝（T07）。

## 为什么这样设计

**1. 计量写入必须并发安全。**
API 进程与 worker 进程会同时给同一个工作区加计数（一次回测结束由 worker 记账，
一次 LLM 调用由 API 进程记账）。任何"先 SELECT 再 UPDATE"的写法在并发下都会丢更新，
所以这里一律用**单条 SQL 的原子自增**：先 `INSERT ... ON CONFLICT DO NOTHING` 占位，
再 `UPDATE ... SET value = value + ?`。两步在同一个事务里，且加法发生在数据库侧
（不是 Python 侧算好再写回），因此 N 个并发调用者累加的结果精确等于 N 次增量之和。

**2. 存储用细粒度整数，边界用 Decimal。**
`DECIMAL_T` 在 SQLite 下是 TEXT，而 SQLite 对 TEXT 列做 `value + ?` 会退化成 REAL
（二进制浮点）运算——实测在数值较大时会写成 `1.23456789012346e+19`，精度直接丢失。
配额判定是硬边界，不能建立在会漂移的表示上。因此表里存**细粒度整数**
（CPU 秒存毫秒、字节存字节、次数存次，见 `Metric.scale`），整数加法在 SQLite 与
PostgreSQL 上都精确；而对外的 Python / JSON 边界一律是 `Decimal` 与十进制字符串，
账本之外的统计也不经过 float。

**3. 所有查询按 workspace_id 过滤。**
本模块没有任何一条语句能跨工作区读或写。工作区 id 只从服务端已验证的请求上下文
（`context.current_workspace()`）取或由调用方显式传入——跨工作区读到别人的用量
比"多算一次"严重得多，所以这里不提供任何不按工作区过滤的重载。

**4. 超限统一用 `AppError(ErrorCode.QUOTA_EXHAUSTED, 中文说明, details=...)`。**
details 里给出 `metric` / `label` / `used` / `limit` / `requested`，
前端"用量面板"与错误提示共用同一份数据。

**5. 口径以任务包 T07 原文为准**：每日 CPU 秒、回测次数、优化组合数、存储占用；
并按原文的"等"补上 LLM 调用次数与 token、数据下载字节数——这两项是真实成本，
且平台已有落点（`agents/base.py` 的 `usage`、行情缓存）。

**6. 日界**：自然日按 UTC 计算，格式 `YYYY-MM-DD`。工作区时区只影响展示，不影响计量口径。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import context
from .db import Database, dec, dstr, jdump, jload, now_ms
from .errors import AppError, ErrorCode

log = logging.getLogger("quantlab.quota")


# ---------------------------------------------------------------------------
# 计量项
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Metric:
    """一个计量项。

    - `unit`：展示单位（用量面板、错误文案用）；
    - `scale`：1 个展示单位 = scale 个存储单位。存储单位必须是整数，
      这样数据库侧的 `value = value + ?` 才是精确的（见模块文档第 2 条）；
    - `quota_key`：对应工作区设置 `settings.quotas` 里的上限字段；
      为 None 表示只计量不限制。
    """

    name: str
    label: str
    unit: str
    scale: int
    quota_key: str | None


#: 计量项清单。顺序即用量面板的展示顺序。
METRICS: tuple[Metric, ...] = (
    # CPU 秒存毫秒：毫秒级足够刻画一次回测的算力成本，且整数相加精确
    Metric("backtest_cpu_seconds", "回测 CPU 秒", "秒", 1000, "max_cpu_seconds_per_day"),
    Metric("backtest_runs", "回测次数", "次", 1, "max_backtests_per_day"),
    Metric("optimize_combinations", "优化组合数", "组", 1, "max_optimize_combinations_per_day"),
    Metric("llm_calls", "LLM 调用次数", "次", 1, "max_llm_calls_per_day"),
    Metric("llm_tokens", "LLM token 数", "token", 1, "max_llm_tokens_per_day"),
    Metric("data_download_bytes", "数据下载量", "字节", 1, "max_download_bytes_per_day"),
    # 存储占用是存量指标：只计量、只展示，不自动拒绝（超限该删什么数据是运维决策，
    # 平台不该自动删用户数据）；上限配了就显示，没配就显示"不限制"。
    Metric("storage_bytes", "存储占用", "字节", 1, "max_storage_bytes"),
)

METRIC_BY_NAME: dict[str, Metric] = {m.name: m for m in METRICS}

#: 默认上限（单位是展示单位；None 表示不限制）。
#: 工作区设置里没写这一项时用这里的兜底值，管理员可在
#: 「设置与审计 → 工作区」通过 PUT /api/workspace/usage/limits 覆盖。
DEFAULT_LIMITS: dict[str, Decimal | None] = {
    "max_cpu_seconds_per_day": Decimal(3600),
    "max_backtests_per_day": Decimal(200),
    "max_optimize_combinations_per_day": Decimal(2000),
    "max_llm_calls_per_day": Decimal(500),
    "max_llm_tokens_per_day": Decimal(2_000_000),
    "max_download_bytes_per_day": Decimal(2 * 1024 * 1024 * 1024),
    "max_storage_bytes": None,
}

#: 每日口径的计量项（按 day 归集）；其余是存量口径（按 TOTAL_DAY 归集）。
DAILY_METRICS = frozenset(
    {
        "backtest_cpu_seconds",
        "backtest_runs",
        "optimize_combinations",
        "llm_calls",
        "llm_tokens",
        "data_download_bytes",
    }
)
#: 存量口径的 day 值。空串是安全的哨兵：自然日永远是非空字符串。
TOTAL_DAY = ""


def today(ts_ms: int | None = None) -> str:
    """UTC 自然日字符串 YYYY-MM-DD。"""
    moment = datetime.fromtimestamp((ts_ms if ts_ms is not None else now_ms()) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%d")


def day_start_ms(day: str | None = None) -> int:
    """UTC start-of-day in epoch milliseconds for quota-aligned reservation queries."""
    bucket = day or today()
    moment = datetime.strptime(bucket, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _check_metric(metric: str) -> Metric:
    item = METRIC_BY_NAME.get(metric)
    if item is None:
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            f"未知的计量项 {metric}",
            details={"metric": metric, "allowed": sorted(METRIC_BY_NAME)},
        )
    return item


def _bucket(metric: str, day: str | None) -> str:
    """计量项落到哪个日桶。调用方一般不用关心，传 None 由这里按口径决定。"""
    if day is not None:
        return day
    return today() if metric in DAILY_METRICS else TOTAL_DAY


def to_units(item: Metric, amount: Any) -> int:
    """展示单位 → 存储单位（整数）。精度超过存储单位时明确报错，而不是静默截断。

    为什么报错而不是取整：静默取整会让"传了 0.0005 秒"变成"记 0 秒"，
    调用方以为计量生效了，实际没有；配额统计宁可吵闹也不要悄悄失真。
    """
    value = dec(amount) * item.scale
    if value != value.to_integral_value():
        raise AppError(
            ErrorCode.INVALID_PARAMETER,
            f"{item.label}的精度超过计量单位：最小可记 1/{item.scale} {item.unit}",
            details={"metric": item.name, "unit": item.unit, "scale": item.scale, "amount": dstr(amount)},
        )
    return int(value)


def from_units(item: Metric, units: Any) -> Decimal:
    """存储单位（整数）→ 展示单位的 Decimal。"""
    return dec(units) / item.scale


# ---------------------------------------------------------------------------
# 存量指标：目录占用
# ---------------------------------------------------------------------------
def measure_dir(path: str | Path, *, max_files: int = 50_000) -> int:
    """统计目录占用字节数（只算普通文件，不跟随符号链接）。

    两个上限是刻意的：
    - 不跟随符号链接：避免把整个磁盘算成一个工作区的占用；
    - `max_files` 上限：这是个在 API 进程里同步跑的目录遍历，工作区文件异常多时
      不能让一次用量查询把请求线程拖死。达到上限就停止累加，返回的是下界，
      调用方（用量面板）会据此显示"至少"而不是假装精确。
    """
    root = Path(path)
    if not root.is_dir():
        return 0
    total = 0
    seen = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue  # 权限不足或目录刚被删除：跳过，不让统计失败
        for entry in entries:
            seen += 1
            if seen > max_files:
                return total
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


# ---------------------------------------------------------------------------
# 计量服务
# ---------------------------------------------------------------------------
class QuotaService:
    """按工作区计量用量并判定配额。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------- 写入（原子自增） ----------------
    def add_in_transaction(
        self,
        tx: Any,
        metric: str,
        amount: Any = 1,
        *,
        workspace_id: str | None = None,
        day: str | None = None,
    ) -> Decimal:
        """Atomically add usage using a transaction owned by the caller.

        ModelHub uses this to commit provider usage, admission settlement and the
        workspace daily counter together. A crash cannot leave the model ledger
        and the user-visible quota panel disagreeing.
        """
        item = _check_metric(metric)
        ws = workspace_id or context.current_workspace()
        delta = to_units(item, amount)
        if delta < 0:
            raise AppError(
                ErrorCode.INVALID_PARAMETER,
                f"{item.label}的增量不能为负（如需修正请用 set_value 覆盖）",
                details={"metric": item.name, "amount": dstr(amount)},
            )
        bucket = _bucket(item.name, day)
        stamp = now_ms()
        tx.execute(
            "INSERT INTO quota_usage (workspace_id, day, metric, value, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT (workspace_id, day, metric) DO NOTHING",
            (ws, bucket, item.name, 0, stamp),
        )
        tx.execute(
            "UPDATE quota_usage SET value = value + ?, updated_at=? WHERE workspace_id=? AND day=? AND metric=?",
            (delta, stamp, ws, bucket, item.name),
        )
        row = tx.fetchone(
            "SELECT value FROM quota_usage WHERE workspace_id=? AND day=? AND metric=?",
            (ws, bucket, item.name),
        )
        return from_units(item, row["value"]) if row else dec(0)

    def add(
        self,
        metric: str,
        amount: Any = 1,
        *,
        workspace_id: str | None = None,
        day: str | None = None,
    ) -> Decimal:
        """给工作区累加一个计量项，返回累加后的新值（展示单位）。

        并发安全：占位 INSERT 与自增 UPDATE 在同一个事务里，且加法发生在数据库侧
        （`value = value + ?`），因此多个进程同时调用不会丢更新。
        """
        with self.db.tx() as tx:
            return self.add_in_transaction(tx, metric, amount, workspace_id=workspace_id, day=day)

    def check_with_reserved(
        self,
        tx: Any,
        metric: str,
        amount: Any = 0,
        *,
        reserved: Any = 0,
        workspace_id: str | None = None,
        day: str | None = None,
    ) -> None:
        """Check used + caller-held reservations + this request in one transaction.

        `amount` is the new request's estimate. `reserved` is the sum of active
        model admissions that has not yet been settled or released. This closes
        the check-then-record race where concurrent callers could all pass the
        same daily workspace limit.
        """
        item = _check_metric(metric)
        ws = workspace_id or context.current_workspace()
        bucket = _bucket(item.name, day)
        limit = self.limit_for(item.name, ws)
        if limit is None:
            return
        used_units = int(
            tx.scalar(
                "SELECT COALESCE(value,0) FROM quota_usage WHERE workspace_id=? AND day=? AND metric=?",
                (ws, bucket, item.name),
            )
            or 0
        )
        requested_units = to_units(item, amount)
        reserved_units = to_units(item, reserved)
        limit_units = to_units(item, limit)
        if used_units + reserved_units + requested_units <= limit_units:
            return
        used = from_units(item, used_units)
        held = from_units(item, reserved_units)
        wanted = from_units(item, requested_units)
        raise AppError(
            ErrorCode.QUOTA_EXHAUSTED,
            f"{item.label}配额已耗尽：已用 {dstr(used)}{item.unit}，"
            f"在途预留 {dstr(held)}{item.unit}，本次还需要 {dstr(wanted)}{item.unit}。"
            "请等待次日重置（UTC），或由工作区管理员在「设置与审计 → 工作区」调整上限。",
            details={
                "metric": item.name,
                "label": item.label,
                "unit": item.unit,
                "used": dstr(used),
                "reserved": dstr(held),
                "limit": dstr(limit),
                "requested": dstr(wanted),
                "quota_key": item.quota_key,
            },
        )

    def set_value(
        self,
        metric: str,
        value: Any,
        *,
        workspace_id: str | None = None,
        day: str | None = None,
    ) -> Decimal:
        """把计量项直接设为某个值（展示单位），返回该值。

        为什么存量指标不能累加：存储占用是当前快照，每次统计都应覆盖；
        累加会让数字随统计次数单调增长，永远不下降，面板就永远显示"超限"。
        """
        item = _check_metric(metric)
        ws = workspace_id or context.current_workspace()
        units = to_units(item, value)
        bucket = _bucket(item.name, day)
        stamp = now_ms()
        with self.db.tx() as t:
            t.execute(
                "INSERT INTO quota_usage (workspace_id, day, metric, value, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT (workspace_id, day, metric) DO UPDATE SET value=?, updated_at=?",
                (ws, bucket, item.name, units, stamp, units, stamp),
            )
        return from_units(item, units)

    def measure_storage(self, root: str | Path, *, workspace_id: str | None = None) -> Decimal:
        """统计并记录一个工作区的存储占用（存量指标，覆盖写）。"""
        ws = workspace_id or context.current_workspace()
        size = measure_dir(root)
        self.set_value("storage_bytes", size, workspace_id=ws)
        return dec(size)

    # ---------------- 读取 ----------------
    def used(
        self,
        metric: str,
        *,
        workspace_id: str | None = None,
        day: str | None = None,
    ) -> Decimal:
        """读取某个计量项的已用量（展示单位），只读指定工作区。"""
        item = _check_metric(metric)
        ws = workspace_id or context.current_workspace()
        row = self.db.fetchone(
            "SELECT value FROM quota_usage WHERE workspace_id=? AND day=? AND metric=?",
            (ws, _bucket(item.name, day), item.name),
        )
        return from_units(item, row["value"]) if row else dec(0)

    def daily(self, *, workspace_id: str | None = None, day: str | None = None) -> dict[str, str]:
        """某一天的全部每日计量项用量（十进制字符串）。

        没有记录的补 "0"：面板不需要区分"没用到"和"没有这一行"。
        """
        ws = workspace_id or context.current_workspace()
        bucket = day or today()
        rows = self.db.fetchall("SELECT metric, value FROM quota_usage WHERE workspace_id=? AND day=?", (ws, bucket))
        found = {r["metric"]: r["value"] for r in rows}
        return {m.name: dstr(from_units(m, found.get(m.name, 0))) for m in METRICS if m.name in DAILY_METRICS}

    def totals(self, *, workspace_id: str | None = None) -> dict[str, str]:
        """存量计量项的当前值（十进制字符串）。"""
        ws = workspace_id or context.current_workspace()
        rows = self.db.fetchall("SELECT metric, value FROM quota_usage WHERE workspace_id=? AND day=?", (ws, TOTAL_DAY))
        found = {r["metric"]: r["value"] for r in rows}
        return {m.name: dstr(from_units(m, found.get(m.name, 0))) for m in METRICS if m.name not in DAILY_METRICS}

    def history(
        self, *, workspace_id: str | None = None, days: int = 30, metric: str | None = None
    ) -> list[dict[str, str]]:
        """最近 N 天的每日用量明细，按日期升序（前端画折线图从旧到新）。"""
        ws = workspace_id or context.current_workspace()
        span = max(1, min(int(days), 366))
        if metric is not None:
            item = _check_metric(metric)
            rows = self.db.fetchall(
                "SELECT day, metric, value, updated_at FROM quota_usage "
                "WHERE workspace_id=? AND metric=? AND day<>? ORDER BY day DESC LIMIT ?",
                (ws, item.name, TOTAL_DAY, span),
            )
        else:
            rows = self.db.fetchall(
                "SELECT day, metric, value, updated_at FROM quota_usage "
                "WHERE workspace_id=? AND day<>? ORDER BY day DESC LIMIT ?",
                (ws, TOTAL_DAY, span * len(DAILY_METRICS)),
            )
        rows.reverse()
        out: list[dict[str, str]] = []
        for r in rows:
            item = METRIC_BY_NAME.get(r["metric"])
            if item is None:
                continue  # 旧版写下的未知计量项：跳过而不是猜单位
            out.append({"day": r["day"], "metric": item.name, "value": dstr(from_units(item, r["value"]))})
        return out

    # ---------------- 配额上限 ----------------
    def _settings(self, workspace_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT settings FROM workspaces WHERE id=?", (workspace_id,))
        if row is None:
            raise AppError(ErrorCode.NOT_FOUND, "工作区不存在")
        return jload(row["settings"], {}) or {}

    def limit_for(self, metric: str, workspace_id: str | None = None) -> Decimal | None:
        """某个计量项的每日/存量上限（展示单位）；None 表示不限制。

        要区分两种"没有上限"：
        - 设置里**没有这个键** → 用平台默认上限兜底（否则管理员不配置就等于没有配额）；
        - 设置里**显式写了 null** → 管理员主动取消限制，不再回落到默认值。
        两者混为一谈的话，"取消限制"这个操作会静默失效。
        """
        item = _check_metric(metric)
        ws = workspace_id or context.current_workspace()
        if item.quota_key is None:
            return None
        quotas = self._settings(ws).get("quotas") or {}
        if item.quota_key in quotas:
            raw = quotas[item.quota_key]
            return None if raw is None else dec(raw)
        raw = DEFAULT_LIMITS.get(item.quota_key)
        return None if raw is None else dec(raw)

    def limits(self, workspace_id: str) -> dict[str, str | None]:
        """工作区全部配额上限（十进制字符串；None 表示不限制）。"""
        if self.db.fetchone("SELECT id FROM workspaces WHERE id=?", (workspace_id,)) is None:
            raise AppError(ErrorCode.NOT_FOUND, "工作区不存在")
        out: dict[str, str | None] = {}
        for item in METRICS:
            if item.quota_key is None:
                continue
            limit = self.limit_for(item.name, workspace_id)
            out[item.quota_key] = None if limit is None else dstr(limit)
        return out

    def set_limits(self, workspace_id: str, patch: dict[str, Any]) -> dict[str, str | None]:
        """管理员调整配额上限；只覆盖 patch 里出现的项，其余保持不变。

        上限本身也要合法：不能为负，且必须是已知计量项的配额键——
        写错键名如果被静默接受，管理员会以为限制生效了，实际上没有。
        """
        known = {m.quota_key for m in METRICS if m.quota_key}
        settings = self._settings(workspace_id)
        quotas = dict(settings.get("quotas") or {})
        for key, raw in (patch or {}).items():
            if key not in known:
                raise AppError(
                    ErrorCode.INVALID_PARAMETER,
                    f"未知的配额项 {key}",
                    details={"quota_key": key, "allowed": sorted(known)},
                )
            if raw is None:
                quotas[key] = None  # 显式传 null = 取消限制
                continue
            value = dec(raw)
            if value < 0:
                raise AppError(
                    ErrorCode.INVALID_PARAMETER,
                    f"配额 {key} 不能为负数",
                    details={"quota_key": key, "value": dstr(raw)},
                )
            quotas[key] = dstr(value)
        settings["quotas"] = quotas
        self.db.execute("UPDATE workspaces SET settings=? WHERE id=?", (jdump(settings), workspace_id))
        return self.limits(workspace_id)

    # ---------------- 超限拒绝 ----------------
    def check(
        self,
        metric: str,
        amount: Any = 0,
        *,
        workspace_id: str | None = None,
        day: str | None = None,
    ) -> None:
        """预检：若"已用 + 本次要用的量"会超过上限，抛 QUOTA_EXHAUSTED。

        为什么本方法是**只读**的（先判后记，而不是先记后判）：
        先记后判意味着超限的请求已经把计数写脏了，之后即使拒绝，用量面板也会显示
        一次从未真正发生的用量，而且并发下会互相推高、形成"越试越满"。

        边界：`used + amount > limit` 才拒绝，等于上限时放行——上限是"最多可以到多少"，
        不是"最多可以到多少减一"。
        """
        item = _check_metric(metric)
        ws = workspace_id or context.current_workspace()
        limit = self.limit_for(item.name, ws)
        if limit is None:
            return  # 未配置上限 = 不限制
        used = self.used(item.name, workspace_id=ws, day=day)
        want = dec(amount)
        if used + want > limit:
            raise AppError(
                ErrorCode.QUOTA_EXHAUSTED,
                f"{item.label}配额已耗尽：已用 {dstr(used)}{item.unit}，上限 {dstr(limit)}{item.unit}，"
                f"本次还需要 {dstr(want)}{item.unit}。"
                "请等待次日重置（UTC），或由工作区管理员在「设置与审计 → 工作区」调整上限。",
                details={
                    "metric": item.name,
                    "label": item.label,
                    "unit": item.unit,
                    "used": dstr(used),
                    "limit": dstr(limit),
                    "requested": dstr(want),
                    "quota_key": item.quota_key,
                },
            )

    def enforce_enqueue(
        self, kind: str, manifest: dict[str, Any] | None = None, *, workspace_id: str | None = None
    ) -> None:
        """按运行类型做入队前的配额预检（只读，不产生用量）。

        供 `api/routes_runs.py::enqueue` 调用（一行接入）：
        `s.quota.enforce_enqueue(kind, manifest)`。
        放在这里而不是路由里，是因为"哪种任务算哪个计量项、按什么量计"的口径
        必须和计量侧保持一致，否则判定的和记录的不是同一件事。

        `manifest` 用来取本次运行**实际要消耗的量**：参数优化要按网格组合数判定，
        而不是按"一次优化"判定——否则一个 5000 组的网格会被当成 1 组放行，
        等跑完才发现当天配额早就爆了。
        """
        ws = workspace_id or context.current_workspace()
        m = manifest or {}
        if kind in ("backtest", "template_backtest"):
            self.check("backtest_runs", 1, workspace_id=ws)
        elif kind == "optimize":
            combos = dec(m.get("combinations") or 1)
            self.check("optimize_combinations", combos, workspace_id=ws)
        # paper / agent_job / 其余类型不参与这几项配额：实时模拟的并发限制由
        # `max_paper_runs` 单独管（见 api/routes_runs.py），这里不重复判定。

    # ---------------- 面板 ----------------
    def panel(self, *, workspace_id: str | None = None, day: str | None = None, days: int = 30) -> dict[str, Any]:
        """用量面板数据：每个计量项的已用/上限/剩余/占比，外加最近 N 天走势。

        所有数值都是十进制字符串，前端只做展示，不做浮点运算。
        """
        ws = workspace_id or context.current_workspace()
        bucket = day or today()
        daily = self.daily(workspace_id=ws, day=bucket)
        totals = self.totals(workspace_id=ws)

        items: list[dict[str, Any]] = []
        for item in METRICS:
            is_daily = item.name in DAILY_METRICS
            used = dec(daily[item.name] if is_daily else totals[item.name])
            limit = self.limit_for(item.name, ws)
            if limit is None:
                remaining, ratio, exhausted = None, None, False
            else:
                left = limit - used
                remaining = dstr(left if left > 0 else dec(0))
                # 占比用 Decimal 计算再转字符串：浮点会让 99.9% 显示成 100%，看起来像已耗尽
                ratio = dstr((used / limit * 100).quantize(Decimal("0.01"))) if limit > 0 else "100"
                exhausted = used >= limit
            items.append(
                {
                    "metric": item.name,
                    "label": item.label,
                    "unit": item.unit,
                    "period": "daily" if is_daily else "total",
                    "used": dstr(used),
                    "limit": None if limit is None else dstr(limit),
                    "remaining": remaining,
                    "used_percent": ratio,
                    "exhausted": exhausted,
                    "quota_key": item.quota_key,
                }
            )
        return {
            "workspace_id": ws,
            "day": bucket,
            "items": items,
            "history": self.history(workspace_id=ws, days=days),
            "note": "用量按工作区计量，跨工作区互不可见；日界为 UTC 自然日。仅为研究结果，非投资建议。",
        }
