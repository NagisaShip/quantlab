"""实时模拟（paper trading）与实盘对照。

两部分：

**PaperSession** —— 真实行情驱动虚拟账户。与回测共用同一份策略文件、同一套
下单语义与成本模型，差别只在时钟：这里的时间来自真实世界，不能加速。
7 天 / 30 天检查点必须**实际经过** 168 / 720 小时，历史重放替代不了。

**对账（reconcile）** —— 把实盘成交与同一策略版本的模拟成交逐笔匹配，
输出信号 / 订单 / 成交 / 费用 / 权益五层差异，并明确列出未匹配项。
原则：不把实际成交价格塞回模拟收益曲线；两类结果分别核算。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ..exchanges.base import Bar
from ..sdk.objects import Portfolio
from ..sdk.runtimes import RuntimeCost
from .windows import HOUR_MS, WARMUP_BARS, compute_windows, window_status_text

# 一次采集间隔超过这个时长就记一次运行缺口
GAP_THRESHOLD_MS = 90_000
# 行情年龄超过这个值视为过期，暂停新增成交
STALE_QUOTE_MS = 5 * 60_000


@dataclass
class LiveEvent:
    """运行期事件：缺口、异常、跳过信号等。全部留档，不静默删除。"""

    ts: int
    level: str  # info | warning | error
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PaperSession:
    """一次实时模拟实验。"""

    id: str
    strategy_id: str
    strategy_name: str
    strategy_version: str
    symbol: str
    timeframe: str
    initial_cash: float
    started_at: int
    status: str = "running"  # running | paused | stopped | interrupted | completed
    last_update: int = 0
    last_bar_ts: int = 0
    last_sample_ts: int = 0
    last_equity_ts: int = 0
    source: str = "exchange"
    cost: dict[str, float] = field(default_factory=dict)
    observations: int = 0
    errors: int = 0
    events: list[LiveEvent] = field(default_factory=list)
    equity_points: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    securities: list[str] = field(default_factory=list)
    exchange: str = "binance"
    params: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    pending: int = 0
    # paper：平台撮合；demo：交易所模拟盘（真实行情 + 交易所模拟账户）
    execution: str = "paper"
    # 行情来源：ws（WebSocket 推送）| rest（轮询）；feed_status 为网关的健康状况
    feed: str = "rest"
    feed_status: dict[str, Any] = field(default_factory=dict)
    # 交易所模拟盘：连接、OMS 订单、熔断、对账（execution=demo 时才有）
    demo: dict[str, Any] = field(default_factory=dict)
    # 启动预热用到的最后一根 bar 的收盘时刻：之后的 bar 才调用策略（仿真真实度报告据此对齐回测）
    warm_until: int = 0

    @property
    def elapsed_hours(self) -> float:
        end = self.last_update or self.started_at
        return max(0.0, (end - self.started_at) / HOUR_MS)

    def checkpoint_progress(self, days: int) -> float:
        target = days * 24
        return min(100.0, self.elapsed_hours / target * 100) if target else 0.0

    def note(self, level: str, message: str, ts: int | None = None) -> None:
        self.events.append(LiveEvent(ts=ts or int(time.time() * 1000), level=level, message=message))
        if level == "error":
            self.errors += 1

    def to_dict(self, *, max_points: int = 600) -> dict[str, Any]:
        pts = self.equity_points
        if len(pts) > max_points:
            step = max(1, len(pts) // max_points)
            pts = pts[::step]
            if pts[-1] != self.equity_points[-1]:
                pts = [*pts, self.equity_points[-1]]
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "initial_cash": self.initial_cash,
            "started_at": self.started_at,
            "status": self.status,
            "last_update": self.last_update,
            "last_bar_ts": self.last_bar_ts,
            "elapsed_hours": round(self.elapsed_hours, 2),
            "source": self.source,
            "cost": self.cost,
            "observations": self.observations,
            "errors": self.errors,
            "events": [e.to_dict() for e in self.events[-200:]],
            "equity_points": pts,
            "fills": self.fills[-500:],
            "message": self.message,
            "securities": self.securities or [self.symbol],
            "exchange": self.exchange,
            "params": self.params,
            "portfolio": self.portfolio,
            "logs": self.logs[-300:],
            "pending": self.pending,
            "execution": self.execution,
            "feed": self.feed,
            "feed_status": self.feed_status,
            "demo": self.demo,
            "warm_until": self.warm_until,
            "checkpoints": [
                {
                    "days": d,
                    "hours": d * 24,
                    "progress": round(self.checkpoint_progress(d), 2),
                    "complete": self.elapsed_hours >= d * 24,
                }
                for d in (7, 30)
            ],
            "note": "实时模拟使用虚拟资金。7 天与 30 天检查点需要实际经过时间，不能加速。",
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperSession:
        known = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in data.items() if k in known}
        payload["events"] = [LiveEvent(**e) for e in data.get("events", [])]
        return cls(**payload)


class PaperTradingEngine:
    """实时模拟引擎。

    设计要点：
    - 与回测共用 SDK 的 `order_*` 语义，但**不共用时钟**：这里的每一根 bar 都
      来自真实行情，引擎只做"采到新 bar → 调一次策略 → 记一次净值"。
    - 服务重启时把 running 状态的实验标记为 `interrupted`，不伪造停机时段。
    - 行情过期（校准后年龄超阈值）时暂停新增成交并记事件，恢复后跳过过期信号。
    """

    def __init__(self, cost: RuntimeCost | None = None) -> None:
        self.cost = cost or RuntimeCost()

    # ---------------- 生命周期 ----------------
    def start(
        self,
        *,
        strategy_id: str,
        strategy_name: str,
        strategy_version: str,
        symbol: str,
        timeframe: str = "1h",
        initial_cash: float = 10_000.0,
        source: str = "exchange",
        now: int | None = None,
    ) -> PaperSession:
        ts = now or int(time.time() * 1000)
        return PaperSession(
            id=uuid.uuid4().hex[:12],
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            symbol=symbol,
            timeframe=timeframe,
            initial_cash=initial_cash,
            started_at=ts,
            last_update=ts,
            last_sample_ts=ts,
            last_equity_ts=ts,
            source=source,
            cost=self.cost.to_dict(),
            message="实验已启动。首个完整 bar 的信号将在下一次采集时执行。",
        )

    def recover(self, session: PaperSession, *, now: int | None = None) -> PaperSession:
        """服务重启后的恢复：标记中断、不补成交、不伪造停机时段。"""
        if session.status in ("running", "paused"):
            session.status = "interrupted"
            session.note(
                "warning",
                "服务重启，原实验已中断。已保留全部观测记录；如需继续观察请开始新批次。",
                ts=now,
            )
            session.last_update = now or int(time.time() * 1000)
        return session

    # ---------------- 采集与推进 ----------------
    def on_sample(
        self,
        session: PaperSession,
        *,
        bars: list[Bar],
        quote: dict[str, Any] | None = None,
        equity: float | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """处理一次采样。返回本次采样的结果摘要。

        `bars` 是**已收盘**的行情序列；只有出现新的完整 bar 才会触发策略。
        """
        ts = now or int(time.time() * 1000)
        result: dict[str, Any] = {"triggered": False, "skipped": None}

        # 只有 running 才推进。paused / stopped / interrupted 一律不处理新采样，
        # 否则"暂停新增成交"就成了一句空话。
        if session.status != "running":
            result["skipped"] = f"实验状态为 {session.status}，不处理采样"
            return result

        # 1) 运行缺口：两次采样间隔过长要记录，不能假装连续
        if session.last_sample_ts and ts - session.last_sample_ts > GAP_THRESHOLD_MS:
            gap_min = (ts - session.last_sample_ts) / 60_000
            session.note("warning", f"采样间隔 {gap_min:.1f} 分钟，记录运行缺口", ts=ts)

        # 2) 行情新鲜度：过期就暂停新增成交
        if quote is not None:
            age = quote.get("age_ms")
            if isinstance(age, int) and age > STALE_QUOTE_MS:
                session.note("error", f"行情年龄 {age} ms 超过阈值，暂停新增成交", ts=ts)
                session.status = "paused"
                session.last_sample_ts = ts
                session.last_update = ts
                result["skipped"] = "行情过期"
                return result

        # 3) 新 bar 才触发策略
        closed = [b for b in bars if b.closed]
        latest = closed[-1].close_time if closed else 0
        if latest and latest > session.last_bar_ts:
            if session.last_bar_ts and latest - session.last_bar_ts > HOUR_MS * 2:
                session.note("info", "恢复后跳过过期信号，不补成交", ts=ts)
            session.last_bar_ts = latest
            session.last_update = ts
            result["triggered"] = True

        # 4) 记录净值：按分钟节流。用独立的 last_equity_ts，
        #    否则每次采样都会重置节流窗口，净值点会被高频采样灌满。
        if equity is not None and ts - session.last_equity_ts >= 60_000:
            session.equity_points.append({"t": ts, "v": round(equity, 4)})
            session.observations += 1
            session.last_equity_ts = ts
        session.last_sample_ts = ts
        session.last_update = ts

        # 5) 到 30 天自动完成
        if session.elapsed_hours >= 30 * 24:
            session.status = "completed"
            session.message = "已到达 30 天检查点。"
        return result

    def stop(self, session: PaperSession, *, now: int | None = None) -> PaperSession:
        """手动停止。持仓按最后行情估值，不自动平仓。"""
        session.status = "stopped"
        session.note("info", "实验已手动停止；持仓按最后行情估值，未自动平仓", ts=now)
        session.last_update = now or int(time.time() * 1000)
        return session

    # ---------------- 报告 ----------------
    def report(self, session: PaperSession, *, timeframe: str = "1h") -> dict[str, Any]:
        """把观测点折成与回测同口径的窗口报告。"""
        equity = [p["v"] for p in session.equity_points]
        ts = [p["t"] for p in session.equity_points]
        windows = compute_windows(
            equity, ts, initial=session.initial_cash, timeframe=timeframe, warmup_bars=WARMUP_BARS
        )
        return {
            "session": session.to_dict(),
            "windows": windows.to_dict(),
            "window_status": {k: window_status_text(v) for k, v in windows.windows.items()},
            "conventions": [
                "实时模拟使用真实行情 + 本地虚拟账户，不代表真实成交能力",
                "7 天 / 30 天检查点需要实际经过 168 / 720 小时，回放与加速不能替代",
                "停机时段不补成交；恢复后跳过过期信号",
                "未平仓持仓按最后可用价格估值，并在界面标注数据新鲜度",
            ],
        }


# ---------------------------------------------------------------------------
# 实盘对照
# ---------------------------------------------------------------------------
@dataclass
class LiveFill:
    """一条实盘成交（从只读账户或 CSV 导入）。"""

    ts: int
    security: str
    side: str
    amount: float
    price: float
    fee: float = 0.0
    fee_asset: str = "USDT"
    order_id: str = ""
    client_order_id: str = ""
    fill_id: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LiveFill:
        return cls(
            ts=int(raw.get("ts") or raw.get("timestamp") or 0),
            security=str(raw.get("security") or raw.get("symbol") or ""),
            side=str(raw.get("side") or "").lower(),
            amount=abs(float(raw.get("amount") or raw.get("qty") or 0)),
            price=float(raw.get("price") or 0),
            fee=abs(float(raw.get("fee") or 0)),
            fee_asset=str(raw.get("fee_asset") or "USDT"),
            order_id=str(raw.get("order_id") or ""),
            client_order_id=str(raw.get("client_order_id") or ""),
            fill_id=str(raw.get("fill_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatchedPair:
    """一对匹配上的（模拟，实盘）成交。"""

    simulated: dict[str, Any]
    live: dict[str, Any]
    price_diff_bps: float
    amount_diff_ratio: float
    fee_diff: float
    time_diff_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReconciliationReport:
    """逐笔对照报告。五层差异分别列出，最后给未解释残差。

    五层（v2.0）：
    1. 信号层——信号/决策是否一致，未匹配项各自多少；
    2. 订单层——订单意图与订单结果是否一致（撤单、拒单、未成交、部分成交）；
    3. 成交层——成交价格、数量、时机的偏差；
    4. 费用层——手续费差异；
    5. 权益层——两侧权益曲线的差额，并区分"可解释"（成交+费用）与"未解释"残差。

    第 2 层需要调用方提供订单列表，第 5 层需要两侧权益曲线；没有提供时该层会明确标注
    "数据不足"并说明原因，**不会**用成交层数据顶替，也不会给一个看起来完整的假数字。
    """

    simulated_fills: int
    live_fills: int
    matched: list[MatchedPair] = field(default_factory=list)
    unmatched_simulated: list[dict[str, Any]] = field(default_factory=list)
    unmatched_live: list[dict[str, Any]] = field(default_factory=list)
    layers: dict[str, Any] = field(default_factory=dict)
    residual: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    layer_status: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "simulated_fills": self.simulated_fills,
            "live_fills": self.live_fills,
            "matched": [m.to_dict() for m in self.matched],
            "unmatched_simulated": self.unmatched_simulated,
            "unmatched_live": self.unmatched_live,
            "match_rate": round(len(self.matched) / max(1, min(self.simulated_fills, self.live_fills)), 4),
            "layers": self.layers,
            "layer_status": self.layer_status,
            "residual": self.residual,
            "notes": self.notes,
        }


#: 五层归因的固定顺序与中文名，供前端稳定渲染
LAYER_ORDER = ("信号层", "订单层", "成交层", "费用层", "权益层")


def reconcile(
    simulated: list[dict[str, Any]],
    live: list[LiveFill] | list[dict[str, Any]],
    *,
    time_tolerance_ms: int = 10 * 60_000,
    amount_tolerance: float = 0.02,
    simulated_orders: list[dict[str, Any]] | None = None,
    live_orders: list[dict[str, Any]] | None = None,
    simulated_equity: list[dict[str, Any]] | None = None,
    live_equity: list[dict[str, Any]] | None = None,
) -> ReconciliationReport:
    """把模拟成交与实盘成交逐笔匹配，并输出五层差异归因。

    匹配键优先级：`client_order_id` → `order_id` →（品种, 方向, 时间窗, 数量接近）。
    返回的每一层差异都单独列出，避免把价格、数量、时机的差异重复相加。

    订单层（`simulated_orders` / `live_orders`）与权益层（`simulated_equity` / `live_equity`）
    需要调用方提供相应数据；缺数据时这两层会标注为"数据不足"，其余三层照常输出。
    """
    live_fills = [f if isinstance(f, LiveFill) else LiveFill.from_dict(f) for f in live]
    report = ReconciliationReport(simulated_fills=len(simulated), live_fills=len(live_fills))
    if not simulated and not live_fills:
        report.notes.append("模拟与实盘都没有成交记录，无法对照")
        return report

    used_live: set[int] = set()
    used_sim: set[int] = set()

    # ---- 第 1 轮：按订单号精确匹配 ----
    by_client = {f.client_order_id: i for i, f in enumerate(live_fills) if f.client_order_id}
    by_order = {f.order_id: i for i, f in enumerate(live_fills) if f.order_id}
    for si, sim in enumerate(simulated):
        cid = str(sim.get("client_order_id") or "")
        oid = str(sim.get("order_id") or "")
        idx = by_client.get(cid) if cid else None
        if idx is None and oid:
            idx = by_order.get(oid)
        if idx is not None and idx not in used_live:
            used_live.add(idx)
            used_sim.add(si)
            report.matched.append(_pair(sim, live_fills[idx]))

    # ---- 第 2 轮：按（品种, 方向, 时间窗, 数量）近似匹配 ----
    for si, sim in enumerate(simulated):
        if si in used_sim:
            continue
        sim_ts = int(sim.get("ts") or 0)
        sim_sec = str(sim.get("security") or "")
        sim_side = str(sim.get("side") or "").lower()
        sim_amt = abs(float(sim.get("amount") or 0))
        best: tuple[float, int] | None = None
        for li, lf in enumerate(live_fills):
            if li in used_live:
                continue
            if lf.security and sim_sec and lf.security != sim_sec:
                continue
            if lf.side and sim_side and lf.side != sim_side:
                continue
            if abs(lf.ts - sim_ts) > time_tolerance_ms:
                continue
            if sim_amt and lf.amount and abs(lf.amount - sim_amt) / sim_amt > amount_tolerance:
                continue
            score = abs(lf.ts - sim_ts) / max(1, time_tolerance_ms)
            if best is None or score < best[0]:
                best = (score, li)
        if best is not None:
            used_live.add(best[1])
            used_sim.add(si)
            report.matched.append(_pair(sim, live_fills[best[1]]))

    report.unmatched_simulated = [simulated[i] for i in range(len(simulated)) if i not in used_sim][:50]
    report.unmatched_live = [live_fills[i].to_dict() for i in range(len(live_fills)) if i not in used_live][:50]

    # ---- 分层差异 ----
    if report.matched:
        n = len(report.matched)
        price_bps = sum(m.price_diff_bps for m in report.matched) / n
        amount_ratio = sum(m.amount_diff_ratio for m in report.matched) / n
        fee_diff = sum(m.fee_diff for m in report.matched)
        time_diff = sum(m.time_diff_ms for m in report.matched) / n
        report.layers = {
            "信号层": {
                "matched": n,
                "unmatched_simulated": len(report.unmatched_simulated),
                "unmatched_live": len(report.unmatched_live),
                "说明": "未匹配项通常来自信号触发时刻差异、外部手工交易或缺失记录",
            },
            "成交层": {
                "平均价格偏差_bps": round(price_bps, 3),
                "平均数量偏差比例": round(amount_ratio, 5),
                "平均时间偏差_ms": round(time_diff, 1),
                "说明": "价格偏差来自滑点与价差；数量偏差来自部分成交与最小下单量",
            },
            "费用层": {
                "模拟费用合计": round(sum(float(m.simulated.get("fee") or 0) for m in report.matched), 6),
                "实盘费用合计": round(sum(float(m.live.get("fee") or 0) for m in report.matched), 6),
                "差额": round(fee_diff, 6),
                "说明": "费率档位、费用币种与折扣都会造成差异",
            },
        }
    else:
        report.layers = {
            "信号层": {
                "matched": 0,
                "unmatched_simulated": len(report.unmatched_simulated),
                "unmatched_live": len(report.unmatched_live),
                "说明": (
                    "没有任何一笔匹配上。先检查时间窗与标的；"
                    "实盘侧多出的成交通常来自外部手工交易、"
                    "共享账户里的其他策略，或缺失的订单标签"
                ),
            }
        }

    # ---- 第 2 层：订单层（订单意图 vs 订单结果）----
    report.layers["订单层"], order_status = _order_layer(simulated, live_fills, simulated_orders, live_orders)
    # ---- 第 5 层：权益层（两侧权益曲线）----
    report.layers["权益层"], nav_status = _equity_layer(simulated_equity, live_equity, report)
    report.layer_status = {
        "信号层": "已计算",
        "订单层": order_status,
        "成交层": "已计算" if report.matched else "数据不足：没有任何成交匹配上",
        "费用层": "已计算" if report.matched else "数据不足：没有任何成交匹配上",
        "权益层": nav_status,
    }
    # 保证五层顺序稳定，前端可以按固定顺序渲染
    report.layers = {k: report.layers[k] for k in LAYER_ORDER if k in report.layers}

    sim_notional = sum(abs(float(s.get("amount") or 0) * float(s.get("price") or 0)) for s in simulated)
    live_notional = sum(f.amount * f.price for f in live_fills)
    sim_fee = sum(abs(float(s.get("fee") or 0)) for s in simulated)
    live_fee = sum(f.fee for f in live_fills)
    report.residual = {
        "模拟成交名义金额": round(sim_notional, 4),
        "实盘成交名义金额": round(live_notional, 4),
        "名义金额差": round(live_notional - sim_notional, 4),
        "模拟费用": round(sim_fee, 6),
        "实盘费用": round(live_fee, 6),
        "费用差": round(live_fee - sim_fee, 6),
        "说明": "以上为未做归因的原始差额；请先用分层差异解释，剩下的才计入残差",
    }
    report.notes = [
        "实盘与模拟分别核算，不把实际成交价格塞回模拟收益曲线",
        "历史导入缺少订单标签时按品种/方向/时间/数量近似匹配，匹配置信度低于精确匹配",
        "共享账户中无法归因的外部交易必须单独列出，不能混入策略绩效",
    ]
    return report


def _order_layer(
    simulated: list[dict[str, Any]],
    live_fills: list[LiveFill],
    simulated_orders: list[dict[str, Any]] | None,
    live_orders: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], str]:
    """订单层：比较"下了什么单"与"单子最后怎么了"。

    这一层关心的不是成交价，而是订单本身的命运：撤单、拒单、未成交、部分成交。
    两侧订单列表由调用方提供；只给成交时，可以退化为"按订单号聚合的成交单"，
    但**看不到**没成交的单子，因此会明确标注数据不足，不假装完整。
    """
    sim_orders = simulated_orders
    live_orders_ = live_orders
    degraded = sim_orders is None or live_orders_ is None
    if sim_orders is None:
        sim_orders = _orders_from_fills(simulated)
    if live_orders_ is None:
        live_orders_ = _orders_from_fills([f.to_dict() for f in live_fills])

    sim_keys = {_order_key(o) for o in sim_orders if _order_key(o)}
    live_keys = {_order_key(o) for o in live_orders_ if _order_key(o)}
    both = sim_keys & live_keys

    def tally(orders: list[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in orders:
            st = str(o.get("status") or "filled").lower()
            out[st] = out.get(st, 0) + 1
        return out

    sim_tally, live_tally = tally(sim_orders), tally(live_orders_)
    layer: dict[str, Any] = {
        "模拟订单数": len(sim_orders),
        "实盘订单数": len(live_orders_),
        "两侧都有": len(both),
        "仅模拟侧": len(sim_keys - live_keys),
        "仅实盘侧": len(live_keys - sim_keys),
        "模拟订单状态分布": sim_tally,
        "实盘订单状态分布": live_tally,
        "未成交单_模拟": sum(v for k, v in sim_tally.items() if k in ("canceled", "rejected", "expired", "pending")),
        "未成交单_实盘": sum(v for k, v in live_tally.items() if k in ("canceled", "rejected", "expired", "pending")),
        "说明": (
            "订单层只关心订单的命运（撤单/拒单/未成交/部分成交），不重复计入价格与费用偏差；"
            "仅实盘侧多出的订单常见于手工下单或共享账户里的其他策略"
        ),
    }
    if degraded:
        layer["数据不足"] = (
            "调用方未提供订单列表，本层由成交按订单号聚合退化而来，"
            "看不到未成交、被拒或被撤的订单；要完整对账请提供 simulated_orders / live_orders"
        )
        return layer, "部分：未提供订单列表，已由成交聚合退化"
    return layer, "已计算"


def _orders_from_fills(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把成交按订单号聚合回订单（退化视图）。同一订单的多笔部分成交合并成一条。"""
    out: dict[str, dict[str, Any]] = {}
    for i, f in enumerate(fills):
        key = str(f.get("client_order_id") or f.get("order_id") or f"anon-{i}")
        row = out.setdefault(
            key,
            {
                "order_key": key,
                "ts": int(f.get("ts") or 0),
                "security": str(f.get("security") or ""),
                "side": str(f.get("side") or "").lower(),
                "amount": 0.0,
                "status": "filled",
            },
        )
        row["amount"] += abs(float(f.get("amount") or 0))
        row["ts"] = min(row["ts"], int(f.get("ts") or 0)) or int(f.get("ts") or 0)
    return list(out.values())


def _order_key(order: dict[str, Any]) -> str:
    return str(order.get("order_key") or order.get("client_order_id") or order.get("order_id") or "")


def _equity_layer(
    simulated_equity: list[dict[str, Any]] | None,
    live_equity: list[dict[str, Any]] | None,
    report: ReconciliationReport,
) -> tuple[dict[str, Any], str]:
    """权益层：比较两侧权益曲线，并把差额拆成"可解释"与"未解释"。

    可解释部分 = 已匹配成交的盈亏差 + 手续费差；剩下的才是未解释残差。
    两条曲线都必须由调用方提供（模拟侧来自 run 的净值序列，实盘侧来自余额快照）；
    缺任一侧就标注数据不足——权益曲线不能由成交推算，推出来的数字会误导人。
    """
    sim_pts = _equity_points(simulated_equity)
    live_pts = _equity_points(live_equity)
    if not sim_pts or not live_pts:
        missing = []
        if not sim_pts:
            missing.append("模拟侧权益曲线")
        if not live_pts:
            missing.append("实盘侧权益曲线")
        return (
            {
                "数据不足": f"缺少{'、'.join(missing)}；权益层需要两侧的权益时间序列才能比较",
                "说明": (
                    "权益差不能由成交记录反推（未实现盈亏、入出金、未平仓都会影响），因此本层在缺数据时不给估算值"
                ),
            },
            f"数据不足：缺少{'、'.join(missing)}",
        )

    sim_last = sim_pts[-1][1]
    live_last = live_pts[-1][1]
    sim_first = sim_pts[0][1]
    live_first = live_pts[0][1]
    # 两侧起点不同（例如实盘账户本来就有底仓）时，比较增量而不是绝对值
    sim_change = sim_last - sim_first
    live_change = live_last - live_first

    explained = sum(
        (float(m.live.get("realized_pnl") or 0) - float(m.simulated.get("realized_pnl") or 0)) + m.fee_diff
        for m in report.matched
    )
    gap = live_change - sim_change
    layer = {
        "模拟期初权益": round(sim_first, 6),
        "模拟期末权益": round(sim_last, 6),
        "实盘期初权益": round(live_first, 6),
        "实盘期末权益": round(live_last, 6),
        "模拟权益变动": round(sim_change, 6),
        "实盘权益变动": round(live_change, 6),
        "权益变动差": round(gap, 6),
        "已解释部分": round(explained, 6),
        "未解释残差": round(gap - explained, 6),
        "模拟采样点": len(sim_pts),
        "实盘采样点": len(live_pts),
        "说明": (
            "比较的是权益**变动**而非绝对值，避免把实盘账户的初始底仓算成策略收益；"
            "已解释部分来自已匹配成交的盈亏差与费用差，未解释残差需要人工排查"
            "（未归因成交、未实现盈亏、入出金、两侧计价时点差异）"
        ),
    }
    return layer, "已计算"


def _equity_points(series: list[dict[str, Any]] | None) -> list[tuple[int, float]]:
    """把权益序列规范成按时间升序的 (ts, value)；同时接受 {"ts","value"} 与 [ts, value]。"""
    if not series:
        return []
    out: list[tuple[int, float]] = []
    for item in series:
        try:
            if isinstance(item, dict):
                ts = int(item.get("ts") or item.get("t") or 0)
                val = item.get("value", item.get("equity", item.get("v")))
            else:
                ts, val = int(item[0]), item[1]
            out.append((ts, float(val)))
        except (TypeError, ValueError, IndexError, KeyError):
            continue
    out.sort(key=lambda p: p[0])
    return out


def _pair(sim: dict[str, Any], live: LiveFill) -> MatchedPair:
    sim_price = float(sim.get("price") or 0)
    sim_amt = abs(float(sim.get("amount") or 0))
    sim_fee = abs(float(sim.get("fee") or 0))
    price_diff_bps = ((live.price - sim_price) / sim_price * 10_000) if sim_price else 0.0
    amount_diff = ((live.amount - sim_amt) / sim_amt) if sim_amt else 0.0
    return MatchedPair(
        simulated=sim,
        live=live.to_dict(),
        price_diff_bps=round(price_diff_bps, 3),
        amount_diff_ratio=round(amount_diff, 6),
        fee_diff=round(live.fee - sim_fee, 8),
        time_diff_ms=int(live.ts - int(sim.get("ts") or 0)),
    )


def parse_live_fills_csv(text: str) -> list[LiveFill]:
    """从 CSV 导入实盘成交。

    必需列：timestamp, security, side, amount, price；fee / order_id / fill_id 可选。
    """
    import csv
    import io

    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    columns = reader.fieldnames or []
    required = ("timestamp", "security", "side", "amount", "price")
    missing = [c for c in required if c not in columns]
    if missing:
        raise ValueError(
            f"实盘成交 CSV 缺少必需列：{'、'.join(missing)}；"
            f"必需列为 {', '.join(required)}（fee、order_id、fill_id 可选）"
        )
    out: list[LiveFill] = []
    for i, row in enumerate(reader):
        if i >= 5000:
            raise ValueError("实盘成交 CSV 最多 5000 行")
        try:
            out.append(LiveFill.from_dict(row))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {i + 2} 行格式错误：{exc}") from exc
    return out


def demo_simulated_fills(portfolio: Portfolio | None = None) -> list[dict[str, Any]]:
    """给对照页做演示用的一小段模拟成交（不是真实数据，仅用于界面预览）。"""
    _ = portfolio
    base = int(time.time() * 1000) - 6 * 3_600_000
    rows = []
    for i in range(6):
        side = "buy" if i % 2 == 0 else "sell"
        rows.append(
            {
                "ts": base + i * 3_600_000,
                "security": "BTC/USDT",
                "side": side,
                "amount": 0.05 + i * 0.002,
                "price": 62_000 + i * 180,
                "fee": (0.05 + i * 0.002) * (62_000 + i * 180) * 0.001,
                "order_id": f"sim-{i}",
                "client_order_id": f"c-{i}",
                "reason": "signal_change",
            }
        )
    return rows


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
