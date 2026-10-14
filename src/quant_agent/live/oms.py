"""订单管理（OMS）：把运行内核算好的委托送到交易所模拟盘，把回报交回内核记账。

流程（每根 bar 一次，同步完成）：

    内核 outbox ─▶ 下单前硬限制 ─▶ 按交易规则取整 ─▶ 下单 ─▶ 查单直到终态 ─▶ 回报 ─▶ 内核 apply_external

订单状态机：pending → submitted → filled / partial / rejected / canceled，网络异常时进入 unknown。
**状态未知时只查单、不重下**：客户端订单号由信号确定性生成，交易所按它去重；查到“没有这笔单”
才算没送达，下一轮再查也查不到就作废。这样重启、超时都不会重复下单。

下单前硬限制（任一触发即拒单，买单和卖单分别对待：熔断后仍允许卖出减仓）：
- 单笔金额上限；
- 每分钟下单数上限；
- 当日亏损上限：亏损超过初始资金的一定比例，熔断（只许减仓），直到人工解除；
- 人工熔断（一键停止）与对账不一致时的自动暂停。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from .demo_broker import BrokerError, BrokerUnknown, DemoBroker, OrderReport, floor_step, round_tick

TERMINAL = ("filled", "partial", "rejected", "canceled")


@dataclass
class OmsLimits:
    max_order_notional: float = 1_000.0
    max_orders_per_minute: int = 10
    max_daily_loss: float = 0.05  # 占初始资金的比例；0 表示不限
    reconcile_tolerance: float = 0.005  # 对账容差（相对）

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> OmsLimits:
        d = d or {}
        return cls(**{k: type(getattr(cls, k))(v) for k, v in d.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OmsState:
    """可检查点化的 OMS 状态。"""

    orders: list[dict[str, Any]] = field(default_factory=list)  # 最近的订单记录（界面展示）
    sent_ts: list[int] = field(default_factory=list)  # 最近一分钟的下单时间（限速）
    halted: bool = False
    halt_reason: str = ""
    day: str = ""
    day_start_equity: float = 0.0
    baseline: dict[str, str] = field(default_factory=dict)  # 启动时交易所余额（对账基准）
    reconcile: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> OmsState:
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _day(ts_ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000))


class DemoOMS:
    def __init__(
        self,
        broker: DemoBroker,
        limits: OmsLimits,
        state: OmsState | None = None,
        *,
        initial_cash: float = 0.0,
        poll_seconds: float = 0.5,
        poll_attempts: int = 8,
        clock: Any = None,
    ) -> None:
        self.broker = broker
        self.limits = limits
        self.state = state or OmsState()
        self.initial_cash = initial_cash
        self.poll_seconds = poll_seconds
        self.poll_attempts = poll_attempts
        self._now = clock or (lambda: int(time.time() * 1000))

    # ------------------------------------------------------------------ 风控
    def halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""

    def observe_equity(self, equity: float) -> str | None:
        """每次估值后调用。跨日重置日初权益；当日亏损超限 → 熔断。返回新触发的熔断原因。"""
        now = self._now()
        today = _day(now)
        if self.state.day != today:
            self.state.day = today
            self.state.day_start_equity = equity
        lim = self.limits.max_daily_loss
        base = self.initial_cash or self.state.day_start_equity
        if lim and base and not self.state.halted and self.state.day_start_equity - equity > lim * base:
            reason = (
                f"当日亏损 {self.state.day_start_equity - equity:.2f} 超过上限 "
                f"{lim:.0%} × {base:.2f}，已熔断：只允许减仓，需要你手动解除"
            )
            self.halt(reason)
            return reason
        return None

    def _pretrade(self, intent: dict[str, Any]) -> str | None:
        side = intent["side"]
        # 现货买入 = 开仓；永续由内核标出 opens（做空的卖单也是开仓，平空的买单是减仓）
        opens = intent.get("opens", side == "buy")
        notional = float(Decimal(intent["qty"]) * Decimal(intent["ref_price"]))
        if self.state.halted and opens:
            return f"风控熔断中（{self.state.halt_reason}），不开新仓"
        if self.limits.max_order_notional and notional > self.limits.max_order_notional and opens:
            return f"单笔金额 {notional:.2f} 超过上限 {self.limits.max_order_notional:.2f}"
        now = self._now()
        self.state.sent_ts = [t for t in self.state.sent_ts if now - t < 60_000]
        if self.limits.max_orders_per_minute and len(self.state.sent_ts) >= self.limits.max_orders_per_minute:
            return f"一分钟内已下 {len(self.state.sent_ts)} 笔，达到上限 {self.limits.max_orders_per_minute}"
        return None

    # ------------------------------------------------------------------ 下单
    def _record(
        self, intent: dict[str, Any], rep: OrderReport | None, status: str, message: str = "", latency_ms: int = 0
    ) -> dict[str, Any]:
        ref = Decimal(intent["ref_price"])
        rec = {
            "client_order_id": intent["client_order_id"],
            "security": intent["security"],
            "side": intent["side"],
            "qty": intent["qty"],
            "ref_price": intent["ref_price"],
            "market_price": intent.get("market_price") or intent["ref_price"],
            "decided_at": int(intent.get("decided_at") or 0),
            "signal_id": intent.get("signal_id") or "",
            "reason": intent.get("reason") or "",
            "status": status,
            "message": message,
            "submitted_at": self._now(),
            "latency_ms": latency_ms,
            "exchange_order_id": rep.exchange_order_id if rep else "",
            "filled_qty": str(rep.filled_qty) if rep else "0",
            "avg_price": str(rep.avg_price) if rep else "0",
            "fee": str(rep.fee) if rep else "0",
            "fee_currency": rep.fee_currency if rep else "",
            "slippage_bps": None,
        }
        if rep and rep.filled_qty > 0 and ref > 0:
            # 正数 = 比平台撮合模型更差（买得更贵 / 卖得更便宜）
            diff = (rep.avg_price - ref) / ref * 10_000
            rec["slippage_bps"] = round(float(diff if intent["side"] == "buy" else -diff), 2)
        for i, old in enumerate(self.state.orders):
            if old["client_order_id"] == rec["client_order_id"]:
                rec["submitted_at"] = old["submitted_at"]
                self.state.orders[i] = rec
                break
        else:
            self.state.orders.append(rec)
        del self.state.orders[:-300]
        return rec

    def _wire(self, rep: OrderReport, intent: dict[str, Any]) -> dict[str, Any]:
        return {
            "client_order_id": intent["client_order_id"],
            "status": rep.status,
            "filled_qty": str(rep.filled_qty),
            "avg_price": str(rep.avg_price),
            "fee": str(rep.fee),
            "fee_currency": rep.fee_currency,
            "exchange_order_id": rep.exchange_order_id,
            "ts": rep.ts or self._now(),
            "message": rep.message,
        }

    def _reject(self, intent: dict[str, Any], message: str) -> dict[str, Any]:
        self._record(intent, None, "rejected", message)
        self._bump("rejected")
        return {
            "client_order_id": intent["client_order_id"],
            "status": "rejected",
            "filled_qty": "0",
            "ts": self._now(),
            "message": message,
        }

    def _bump(self, key: str, n: int = 1) -> None:
        self.state.stats[key] = int(self.state.stats.get(key) or 0) + n

    def _settle(self, intent: dict[str, Any], rep: OrderReport, started: float) -> OrderReport:
        """市价单通常立刻成交；没到终态就短暂轮询。"""
        tries = 0
        while rep.status not in TERMINAL and tries < self.poll_attempts:
            time.sleep(self.poll_seconds * (1 if tries < 3 else 2))
            tries += 1
            try:
                rep = self.broker.query(intent["security"], intent["client_order_id"])
            except BrokerUnknown:
                continue
        return rep

    def submit(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        """送一笔委托。返回给内核的回报；状态未知时返回 None（委托留在在途，下一轮再查）。"""
        why = self._pretrade(intent)
        if why:
            return self._reject(intent, why)
        sym, side = intent["security"], intent["side"]
        qty = Decimal(intent["qty"])
        try:
            rules = self.broker.rules(sym)
        except Exception as exc:
            return self._reject(intent, f"取交易规则失败：{exc}")
        q = floor_step(qty, rules.step)
        if q <= 0 or (rules.min_qty and q < rules.min_qty):
            return self._reject(intent, f"数量 {qty} 低于交易所最小下单量 {rules.min_qty}")
        if rules.min_notional and q * Decimal(intent["ref_price"]) < rules.min_notional:
            return self._reject(intent, f"金额低于交易所最小下单额 {rules.min_notional}")
        intent = {**intent, "qty": format(q.normalize(), "f")}
        if intent.get("perp"):
            lev = str(intent.get("leverage") or "1")
            prepared = self.state.stats.setdefault("perp_prepared", {})
            if prepared.get(sym) != lev:
                try:
                    self.broker.prepare_perp(sym, Decimal(lev))
                except Exception as exc:
                    return self._reject(intent, f"设置逐仓与 {lev} 倍杠杆失败：{exc}")
                prepared[sym] = lev
        started = time.monotonic()
        self.state.sent_ts.append(self._now())
        self._bump("sent")
        self._record(intent, None, "submitted")
        try:
            rep = self.broker.place_market(sym, side, q, intent["client_order_id"])
        except BrokerError as exc:
            return self._reject(intent, f"交易所拒单：{exc}")
        except BrokerUnknown as exc:
            self._record(intent, None, "unknown", f"下单结果未知（{exc}），先查单，不重下")
            self._bump("unknown")
            return self.resolve(intent)
        rep = self._settle(intent, rep, started)
        latency = int((time.monotonic() - started) * 1000)
        if rep.status not in TERMINAL:
            self._record(intent, rep, "unknown", "交易所还没给出终态，下一轮继续查", latency)
            self._bump("unknown")
            return None
        self._record(intent, rep, rep.status, rep.message, latency)
        self._bump(rep.status)
        return self._wire(rep, intent)

    def resolve(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        """查一笔状态未知的订单。查不到 → 视为未送达，作废；查到终态 → 回报；其他 → 继续等。"""
        try:
            rep = self.broker.query(intent["security"], intent["client_order_id"])
        except (BrokerUnknown, BrokerError):
            return None
        if rep.status not in TERMINAL:
            return None
        self._record(intent, rep, rep.status, rep.message)
        return self._wire(rep, intent)

    # ------------------------------------------------------------------ 保护止损
    def _protect_record(
        self, p: dict[str, Any], status: str, message: str = "", rep: OrderReport | None = None
    ) -> None:
        rec = {
            "client_order_id": p["client_order_id"],
            "security": p["security"],
            "side": p.get("side", ""),
            "qty": p.get("qty", "0"),
            "ref_price": p.get("trigger", "0"),
            "market_price": p.get("trigger", "0"),
            "decided_at": int(p.get("placed_at") or 0),
            "signal_id": "",
            "reason": "exchange_stop",
            "kind": "protect",
            "trigger": p.get("trigger", ""),
            "status": status,
            "message": message,
            "submitted_at": self._now(),
            "latency_ms": 0,
            "exchange_order_id": rep.exchange_order_id if rep else "",
            "filled_qty": str(rep.filled_qty) if rep else "0",
            "avg_price": str(rep.avg_price) if rep else "0",
            "fee": str(rep.fee) if rep else "0",
            "fee_currency": rep.fee_currency if rep else "",
            "slippage_bps": None,
        }
        for i, old in enumerate(self.state.orders):
            if old["client_order_id"] == rec["client_order_id"]:
                rec["submitted_at"] = old["submitted_at"]
                self.state.orders[i] = rec
                break
        else:
            self.state.orders.append(rec)
        del self.state.orders[:-300]

    def _protect_fill(self, p: dict[str, Any], rep: OrderReport) -> dict[str, Any] | None:
        """查一张保护止损：成交了就变成给内核的回报（附上止损单本身，内核据此记账）。"""
        if rep.status not in ("filled", "partial") or rep.filled_qty <= 0:
            return None
        self._protect_record(p, rep.status, "交易所上的保护止损已触发成交", rep)
        self._bump("protect_filled")
        return {**self._wire(rep, p), "protect": {k: v for k, v in p.items() if k not in ("kind", "action")}}

    def _protect_place(self, p: dict[str, Any]) -> None:
        sym = p["security"]
        try:
            rules = self.broker.rules(sym)
            q = floor_step(Decimal(p["qty"]), rules.step)
            if q <= 0 or (rules.min_qty and q < rules.min_qty):
                self._protect_record(p, "rejected", f"数量 {p['qty']} 低于交易所最小下单量，没有挂保护止损")
                return
            trig = Decimal(p["trigger"])
            if rules.tick:
                trig = round_tick(trig, rules.tick, up=p.get("side") == "buy")
            self.broker.place_stop(sym, p["side"], q, trig, p["client_order_id"])
            self._protect_record(
                {**p, "qty": format(q.normalize(), "f"), "trigger": str(trig)},
                "active",
                f"已在交易所挂保护止损，触发价 {trig}",
            )
            self._bump("protect_placed")
        except BrokerUnknown as exc:
            self._protect_record(p, "unknown", f"挂保护止损结果未知（{exc}），下一轮查单")
        except Exception as exc:  # 挂不上不影响平台自己的止损，只记一笔、不熔断
            self._protect_record(p, "rejected", f"交易所没接受保护止损：{exc}")
            self._bump("protect_failed")

    def _protect_cancel(self, p: dict[str, Any]) -> dict[str, Any] | None:
        """撤一张保护止损。撤之前先查：如果已经成交，返回成交回报（平台不该再平一次）。"""
        sym = p["security"]
        try:
            rep = self.broker.query_stop(sym, p["client_order_id"])
            r = self._protect_fill(p, rep)
            if r is not None:
                return r
        except Exception:
            pass
        try:
            self.broker.cancel_stop(sym, p["client_order_id"])
            self._protect_record(p, "canceled", "平台撤掉了保护止损（换价、换量或自己平仓）")
        except Exception as exc:
            self._protect_record(p, "unknown", f"撤保护止损失败：{exc}")
        return None

    def check_protects(self, protects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """查交易所上挂着的保护止损有没有被触发（平台停机期间也可能触发）。"""
        reports = []
        for p in protects:
            try:
                rep = self.broker.query_stop(p["security"], p["client_order_id"])
            except Exception:
                continue
            r = self._protect_fill(p, rep)
            if r is not None:
                reports.append(r)
        return reports

    def process(
        self, outbox: list[dict[str, Any]], awaiting: list[dict[str, Any]], protects: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """一轮处理：先清理上一轮遗留的未知订单，再送新委托（卖单先送，回款给买单用）。

        保护止损：先撤（现货止损单会锁住币）→ 送普通委托 → 再挂新的 → 查其余挂着的有没有被触发。
        撤单时发现已经成交，平台同一标的的减仓单就不再送（否则会平两次，永续还可能反手开仓）。
        """
        reports: list[dict[str, Any]] = []
        prot = [i for i in outbox if i.get("kind") == "protect"]
        orders = [i for i in outbox if i.get("kind") != "protect"]
        touched = {p["client_order_id"] for p in prot}
        stopped: dict[str, str] = {}
        for p in prot:
            if p.get("action") == "cancel":
                r = self._protect_cancel(p)
                if r is not None:
                    reports.append(r)
                    stopped[p["security"]] = p.get("side", "")
        fresh = {i["client_order_id"] for i in orders}
        for intent in awaiting:
            if intent["client_order_id"] in fresh:
                continue
            r = self.resolve(intent)
            if r is not None:
                reports.append(r)
        for intent in sorted(orders, key=lambda i: 0 if i["side"] == "sell" else 1):
            if stopped.get(intent["security"]) == intent["side"]:
                reports.append(self._reject(intent, "交易所上的保护止损已先成交，这笔减仓单不再送"))
                continue
            r = self.submit(intent)
            if r is not None:
                reports.append(r)
        for p in prot:
            if p.get("action") == "place":
                self._protect_place(p)
        reports += self.check_protects([p for p in protects or [] if p["client_order_id"] not in touched])
        return reports

    # ------------------------------------------------------------------ 对账
    def snapshot_baseline(self) -> dict[str, str]:
        bal = self.broker.balances()
        self.state.baseline = {k: str(v) for k, v in bal.items()}
        return self.state.baseline

    def reconcile(
        self,
        ledger_cash: Decimal,
        holdings: dict[str, Decimal],
        quote: str = "USDT",
        perps: dict[str, Decimal] | None = None,
        margin: Decimal = Decimal(0),
    ) -> dict[str, Any]:
        """交易所余额 vs 平台账本。

        账本从 initial_cash、零持仓开始；交易所从启动时的余额（baseline）开始。
        所以“应有余额” = baseline + 账本变化量。差异超过容差 → 不一致（上层暂停开新仓并告警）。
        """
        bal = self.broker.balances()
        base = {k: Decimal(v) for k, v in self.state.baseline.items()}
        rows = []
        # 有永续时，保证金仍在交易所钱包里：应有余额 = 基准 + (现金 + 保证金 − 初始资金)
        expect_quote = base.get(quote, Decimal(0)) + (ledger_cash + margin - Decimal(str(self.initial_cash)))
        # 资金费按公开费率估算记账、各交易所对保证金是否计入余额的口径不同：有永续时余额差异只告警，不暂停
        rows.append((quote, expect_quote, bal.get(quote, Decimal(0)), perps is not None))
        for sec, qty in holdings.items():
            asset = sec.split("/")[0]
            rows.append((asset, base.get(asset, Decimal(0)) + qty, bal.get(asset, Decimal(0)), False))
        if perps is not None:
            have = self.broker.positions()
            for sec in sorted(set(perps) | set(have)):
                rows.append((sec, perps.get(sec, Decimal(0)), have.get(sec, Decimal(0)), False))
        tol = Decimal(str(self.limits.reconcile_tolerance))
        out = []
        ok = True
        for asset, exp, act, soft in rows:
            diff = act - exp
            scale = max(abs(exp), Decimal("1e-8"))
            bad = abs(diff) > tol * scale and abs(diff) > Decimal("1e-8")
            if not soft:
                ok = ok and not bad
            out.append(
                {
                    "asset": asset,
                    "expected": str(exp),
                    "actual": str(act),
                    "diff": str(diff),
                    "ok": not bad,
                    **({"soft": True} if soft else {}),
                }
            )
        self.state.reconcile = {"at": self._now(), "ok": ok, "rows": out}
        return self.state.reconcile
