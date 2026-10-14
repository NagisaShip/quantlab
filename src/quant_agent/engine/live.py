"""实时模拟执行器：让策略真的在最新行情上跑起来。

`PaperTradingEngine` 负责实验的账面规则（采样缺口、行情新鲜度、净值节流、检查点）；
这里负责"执行"：维护一个常驻内存的策略运行时，每采到一根新的已收盘 bar，
就把上一根的委托按开盘价撮合、调用一次策略、按最新价估值。

与回测**同一套**撮合与成本代码（`BacktestRuntime`），区别只有时钟：
- 回测一次性喂入全部历史；这里 bar 一根一根到来，不能加速、不能回放。
- 启动时加载一段历史只用于指标预热，预热期不调用策略、不成交。
- 运行时状态（账本、挂单、`g`、已处理事件）每推进一根 bar 写一次检查点；worker 崩溃后 run 进入
  RECOVERING，由任意 worker 从检查点恢复（`snapshot` / `restore`），已处理事件去重、信号 ID 确定，不会重复成交。
- 恢复后**不补算停机期间的成交**：停机期间到来的多根 bar 只推进数据，只在最新一根上调用策略
  ——这是实验三确立的口径。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..data.quality import TIMEFRAME_MS
from ..exchanges.base import Bar
from ..sdk import trade_mgmt
from ..sdk.objects import Context
from ..sdk.runner import StrategyModule, StrategyRunner
from ..sdk.runtimes import BacktestRuntime, RiskConfig, RuntimeCost
from ..sdk.sandbox import StrategyTimeout, TimeGuard

LIVE_WARMUP_BARS = 300
CALL_TIME_LIMIT = 10.0  # 单次策略回调的时长上限（秒）


@dataclass
class LiveState:
    """一个运行中实验的内存状态。"""

    session_id: str
    module: StrategyModule
    runtime: BacktestRuntime
    runner: StrategyRunner
    securities: list[str]
    timeframe: str
    started_at: int
    last_bar_ts: int = 0
    fills_seen: int = 0
    logs_seen: int = 0
    rejected_seen: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class LiveRunner:
    """进程内的实时运行时注册表。"""

    def __init__(self) -> None:
        self._states: dict[str, LiveState] = {}
        self._lock = threading.Lock()

    def has(self, session_id: str) -> bool:
        return session_id in self._states

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._states.pop(session_id, None)

    def active_ids(self) -> list[str]:
        return list(self._states)

    def start(
        self,
        session_id: str,
        module: StrategyModule,
        history: dict[str, list[Bar]],
        *,
        timeframe: str,
        initial_cash: float,
        cost: RuntimeCost,
        started_at: int,
        risk: RiskConfig | None = None,
        strategy_version_id: str = "",
        fill_mode: str = "internal",
    ) -> LiveState:
        """加载预热历史、调用 initialize。预热期不调用策略。

        fill_mode="external"：交易所模拟盘。委托不在本地成交，放进 outbox 交给上层 OMS，
        成交回报经 `apply_reports` 记账。
        """
        ctx = Context(run_id=session_id, mode="demo" if fill_mode == "external" else "paper")
        runtime = BacktestRuntime(
            {s: list(b) for s, b in history.items()},
            initial_cash=initial_cash,
            cost=cost,
            ctx=ctx,
            risk=risk,
            run_id=session_id,
            strategy_version_id=strategy_version_id,
        )
        runtime.fill_mode = fill_mode
        ctx.securities = list(history)
        if module.params:
            ctx.params = dict(module.params)
        runner = StrategyRunner(module, runtime, ctx)
        with TimeGuard(module.filename, CALL_TIME_LIMIT):
            runner.start()
        last = max((b[-1].close_time for b in history.values() if b), default=0)
        if last:
            runtime.advance(last)
            runtime.accept_event(last)  # 预热最后一根视为已处理，之后的重复推送不会再触发
            ctx.current_dt = last
        state = LiveState(
            session_id=session_id,
            module=module,
            runtime=runtime,
            runner=runner,
            securities=list(history),
            timeframe=timeframe,
            started_at=started_at,
            last_bar_ts=last,
        )
        with self._lock:
            self._states[session_id] = state
        return state

    # ---------------- 检查点 ----------------
    def snapshot(self, session_id: str) -> dict[str, Any]:
        """运行时快照 + 策略全局变量 g（可 JSON 化的部分）+ 策略 snapshot 钩子 + 已处理到的 bar。"""
        state = self._states[session_id]
        with state.lock:
            snap = state.runtime.snapshot()
            snap["g"] = _jsonable_g(state.runner.ctx._g.to_dict())
            snap["strategy_state"] = _jsonable(state.runner.snapshot_state())
            snap["last_bar_ts"] = state.last_bar_ts
            snap["fired_periods"] = sorted(state.runner._fired_periods)[-500:]
            snap["timer_last"] = dict(state.runner._timer_last)
            return snap

    def restore(self, session_id: str, snap: dict[str, Any]) -> None:
        """从检查点恢复：先由 start() 重新 initialize（重建调度），再覆盖账户、委托、信号与 g。

        已处理过的事件（last_event_ts）与信号（signal_id）都在快照里，恢复后重复推送同一根 bar
        不会再次下单或成交。停机期间错过的 bar 只推进数据、不补成交（信号按有效期过期）。
        最后把检查点里的 `strategy_state` 交回策略的 `restore(ctx, state)` 钩子。
        """
        state = self._states[session_id]
        with state.lock:
            state.runtime.restore(snap)
            for k, v in (snap.get("g") or {}).items():
                setattr(state.runner.ctx._g, k, v)
            state.runner._fired_periods.update(snap.get("fired_periods") or [])
            state.runner._timer_last.update(snap.get("timer_last") or {})
            state.last_bar_ts = max(state.last_bar_ts, int(snap.get("last_bar_ts") or 0))
            state.fills_seen = len(state.runtime.fills)
            state.logs_seen = len(state.runtime.logs)
        # 钩子在锁外调用：策略代码可能耗时，不该阻塞 tick
        state.runner.restore_state(snap.get("strategy_state"))

    def liquidate(self, session_id: str, *, reason: str = "liquidate") -> dict[str, Any]:
        """模拟平仓：撤销全部委托，按最新收盘价 ± 成本卖出全部持仓（记为模拟成交）。"""
        state = self._states[session_id]
        with state.lock:
            rt = state.runtime
            canceled = rt.cancel_all()
            results = []
            books = [(k, h.qty) for k, h in rt.ledger.holdings.items() if h.qty > 0]
            books += [(k, p.qty) for k, p in rt.ledger.perps.items() if p.qty]
            for sec, qty in books:
                bar = rt.current_bar(sec)
                if bar is None:
                    continue
                from ..sdk.runtime import OrderRequest

                req = OrderRequest(
                    security=sec,
                    side="sell" if qty > 0 else "buy",
                    amount=float(abs(qty)),
                    reason=reason,
                    target_kind="amount_target",
                    target_value=0.0,
                )
                sig = rt._make_signal(req, bar=bar)
                rt._register(sig)
                r = rt._fill(
                    req,
                    bar=bar,
                    base_price=__import__("decimal").Decimal(str(bar.close)),
                    sig=sig,
                    skip_tolerance=True,
                    skip_deviation=True,
                )
                results.append({"security": sec, "ok": r.ok, "message": r.message})
            rt.mark()
            new_fills = [f.to_dict() for f in rt.fills[state.fills_seen :]]
            state.fills_seen = len(rt.fills)
            outbox, rt.outbox = rt.outbox, []
            return {
                "outbox": outbox,
                "canceled": canceled,
                "results": results,
                "fills": new_fills,
                "portfolio": rt.ctx.portfolio.to_dict(),
                "equity": rt.ctx.portfolio.total_value,
            }

    def step(
        self,
        session_id: str,
        latest: dict[str, list[Bar]],
        *,
        marks: dict[str, float] | None = None,
        call_strategy: bool = True,
        funding: dict[str, list[Any]] | None = None,
        delistings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """喂入最新已收盘 bar，返回本次新增的成交、日志与账户快照。

        delistings：已发布的下架公告，到了就立刻移出标的池（不等新 bar）。

        若一次到来多根新 bar（采集中断后恢复），只在最新一根上调用策略，
        更早的 bar 只推进数据——过期信号不补成交。
        """
        state = self._states.get(session_id)
        if state is None:
            raise KeyError(session_id)
        with state.lock:
            rt = state.runtime
            # 0) 永续合约的资金费率（上层按时间点取来的已结算记录）
            for sec, rows in (funding or {}).items():
                if sec in rt.bars_by_security:
                    rt.add_funding(sec, [(int(t), r) for t, r in rows])
            # 0b) 下架公告：一到就生效，不等新 bar
            if delistings:
                new = rt.set_delistings(delistings, wall=int(time.time() * 1000))
                if new:
                    rt.log("warning", f"下架公告：{'、'.join(new)} 已移出标的池，不再开新仓")
            # 1) 并入新 bar（按开盘时间去重，只收已收盘）
            new_close_times: set[int] = set()
            for sec, bars in latest.items():
                if sec not in rt.bars_by_security:
                    continue
                known = rt.bars_by_security[sec]
                last_open = known[-1].open_time if known else -1
                for b in bars:
                    if b.closed and b.open_time > last_open:
                        known.append(b)
                        last_open = b.open_time
                        if b.close_time > state.last_bar_ts:
                            new_close_times.add(b.close_time)

            timeline = sorted(new_close_times)
            skipped = max(0, len(timeline) - 1)
            errors_before = len(state.runner.errors)
            triggered = False
            timed_out = ""
            for i, ts in enumerate(timeline):
                if not rt.accept_event(ts):
                    continue  # 重复 / 乱序事件：丢弃，不会重复成交
                rt.advance(ts)
                if i == 0:
                    rt.execute()  # 上一根收盘的委托在这根开盘成交；限价、止损止盈
                    state.runner.dispatch_trades(rt.drain_new_fills())
                elif rt.ledger.perps:
                    # 补进来的 bar：交易所那边强平和资金费不会因为我们断线而暂停，逐根补算
                    rt._settle_funding(at_open=True)
                    rt._check_liquidations()
                    rt._settle_funding(at_open=False)
                    state.runner.dispatch_trades(rt.drain_new_fills())
                if i == len(timeline) - 1 and call_strategy:
                    try:
                        with TimeGuard(state.module.filename, CALL_TIME_LIMIT):
                            state.runner.on_bar(ts)
                        triggered = True
                    except StrategyTimeout as exc:
                        timed_out = str(exc)
                    rt.route_now()  # 交易所模拟盘：决策后立刻送单
                    trade_mgmt.sync_protects(rt)  # 交易所上的保护止损跟上最新止损价与持仓
                state.last_bar_ts = ts

            # 2) 估值：有实时报价用报价，否则用最新收盘
            rt.mark({k: float(v) for k, v in (marks or {}).items() if v and math.isfinite(float(v))})

            new_fills = [f.to_dict() for f in rt.fills[state.fills_seen :]]
            new_logs = rt.logs[state.logs_seen :]
            new_rejected = rt.rejected[state.rejected_seen :]
            state.fills_seen = len(rt.fills)
            state.logs_seen = len(rt.logs)
            state.rejected_seen = len(rt.rejected)
            outbox, rt.outbox = rt.outbox, []
            sigs = [
                s.to_dict() for s in rt.signals[-50:] if s.decision_ts >= (timeline[0] if timeline else 1 << 62) - 1
            ]
            return {
                "signals": sigs,
                "nav": {
                    "ts": state.last_bar_ts,
                    "nav": str(rt.ledger.equity()),
                    "units": str(rt.units),
                    "unit_nav": str(rt.unit_nav),
                    "cash": str(rt.ledger.cash()),
                    "positions_value": str(rt.ledger.positions_value()),
                },
                "counters": dict(rt.counters),
                "new_bars": len(timeline),
                "skipped_bars": skipped,
                "triggered": triggered,
                "fills": new_fills,
                "logs": new_logs,
                # 风控拒单也要暴露给上层（T09）：被风控暂停新开仓时必须能发告警。
                # 用 seen 游标只给"本次新增的"，否则每 tick 都会把历史拒单重报一遍，
                # 告警侧的去重窗口再宽也扛不住。
                "rejected": new_rejected,
                "outbox": outbox,
                "awaiting": len(rt.awaiting),
                "protects": list(rt.protects.values()),
                "errors": state.runner.errors[errors_before:] + ([timed_out] if timed_out else []),
                "equity": rt.ctx.portfolio.total_value,
                "portfolio": rt.ctx.portfolio.to_dict(),
                "pending": len(rt.pending),
                "last_bar_ts": state.last_bar_ts,
                "globals": state.runner.ctx._g.to_dict(),
            }

    def apply_reports(
        self, session_id: str, reports: list[dict[str, Any]], *, marks: dict[str, float] | None = None
    ) -> dict[str, Any]:
        """记交易所回报（成交 / 拒单），触发策略的 on_trade 回调，重新估值。"""
        state = self._states[session_id]
        with state.lock:
            rt = state.runtime
            for r in reports:
                rt.apply_external(r)
            errors_before = len(state.runner.errors)
            fills = rt.drain_new_fills()
            if fills:
                try:
                    with TimeGuard(state.module.filename, CALL_TIME_LIMIT):
                        state.runner.dispatch_trades(fills)
                except StrategyTimeout as exc:
                    state.runner.errors.append(str(exc))
            rt.route_now()  # on_trade 里新下的单也立刻送
            trade_mgmt.sync_protects(rt)  # 开仓成交回来了：把保护止损挂上
            rt.mark({k: float(v) for k, v in (marks or {}).items() if v and math.isfinite(float(v))})
            new_fills = [f.to_dict() for f in rt.fills[state.fills_seen :]]
            new_rejected = rt.rejected[state.rejected_seen :]
            state.fills_seen = len(rt.fills)
            state.rejected_seen = len(rt.rejected)
            state.logs_seen = len(rt.logs)
            outbox, rt.outbox = rt.outbox, []  # on_trade 里可能又下了单
            return {
                "fills": new_fills,
                "rejected": new_rejected,
                "outbox": outbox,
                "awaiting": len(rt.awaiting),
                "protects": list(rt.protects.values()),
                "errors": state.runner.errors[errors_before:],
                "equity": rt.ctx.portfolio.total_value,
                "portfolio": rt.ctx.portfolio.to_dict(),
                "ledger": {
                    "cash": str(rt.ledger.cash()),
                    "holdings": {k: str(h.qty) for k, h in rt.ledger.holdings.items()},
                    "perps": {k: str(p.qty) for k, p in rt.ledger.perps.items() if p.qty},
                    "margin": str(sum((p.margin for p in rt.ledger.perps.values()), rt.ledger.cash() * 0)),
                },
                "nav": {
                    "ts": state.last_bar_ts,
                    "nav": str(rt.ledger.equity()),
                    "units": str(rt.units),
                    "unit_nav": str(rt.unit_nav),
                    "cash": str(rt.ledger.cash()),
                    "positions_value": str(rt.ledger.positions_value()),
                },
            }

    def awaiting(self, session_id: str) -> list[dict[str, Any]]:
        state = self._states[session_id]
        with state.lock:
            return list(state.runtime.awaiting.values())


def _jsonable_g(values: dict[str, Any]) -> dict[str, Any]:
    import json

    out = {}
    for k, v in values.items():
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            continue  # 不可序列化的对象不进检查点；恢复后由 initialize 重新构造
    return out


def _jsonable(value: Any) -> Any:
    """策略 `snapshot()` 的返回值必须是可 JSON 化的；不可序列化时宁可不存，也不写坏检查点。"""
    import json

    if value is None:
        return None
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return None
    return value


def clock_synthetic_bars(symbol: str, timeframe: str, start: int, end: int) -> list[Bar]:
    """时间确定性的演示行情：同一时刻永远得到同一根 bar，不随请求窗口变化。

    用于"演示模式"的实时模拟（无法访问交易所时也能看到实验跑起来）。
    价格 = 基准价 × exp(几条不同周期的正弦叠加 + 逐 bar 的确定性小扰动)。
    只能用于演示流程，**不能**用于得出任何策略结论；界面会明确标注。
    """
    import hashlib

    from ..data.service import SYNTHETIC_START

    step = TIMEFRAME_MS.get(timeframe, 3_600_000)
    perp = symbol.upper().endswith(":PERP")
    spot_symbol = symbol.split(":")[0]
    base = SYNTHETIC_START.get(symbol.split("/")[0].upper(), 100.0)
    if perp:  # 永续跟随同名现货，叠加很小的确定性基差
        base *= 1.0003
    phase = int(hashlib.sha256(spot_symbol.encode()).hexdigest()[:6], 16) / 0xFFFFFF * 2 * math.pi

    def log_price(t: int) -> float:
        hours = t / 3_600_000
        noise = int(hashlib.sha256(f"{spot_symbol}:{t}".encode()).hexdigest()[:6], 16) / 0xFFFFFF - 0.5
        return (
            0.08 * math.sin(2 * math.pi * hours / (24 * 21) + phase)
            + 0.03 * math.sin(2 * math.pi * hours / (24 * 4) + 2 * phase)
            + 0.012 * math.sin(2 * math.pi * hours / 9 + 3 * phase)
            + 0.004 * noise
        )

    out: list[Bar] = []
    t = start - start % step
    while t + step <= end:
        o = base * math.exp(log_price(t))
        c = base * math.exp(log_price(t + step))
        wiggle = abs(log_price(t + step // 2) - log_price(t)) + 0.001
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=t,
                close_time=t + step,
                open=round(o, 4),
                high=round(max(o, c) * (1 + wiggle / 3), 4),
                low=round(min(o, c) * (1 - wiggle / 3), 4),
                close=round(c, 4),
                volume=100.0 + (int(t / step) % 17) * 5,
                closed=True,
            )
        )
        t += step
    return out


def now_ms() -> int:
    return int(time.time() * 1000)
