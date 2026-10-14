"""同一账户上跑多个策略：先内部对冲，再只把净额送交易所。

几个策略同时对同一个币下单（A 买 0.3、B 卖 0.2），各自送单会白付两边手续费、还可能被交易所
当成自成交拒掉。这里把一轮里所有成员的委托按标的合并：

- 买卖相抵的部分在内部按参考价成交，不付手续费；
- 剩下的净额合成**一笔**委托交给 OMS 送出；成交价和手续费按数量比例分回给较大一侧的各个成员；
- 总敞口上限（ACCOUNT_RISK_CAP）：这一轮加仓后整个账户的持仓市值超过 权益×上限 时，
  从金额最大的加仓委托开始拒，减仓委托永远放行；
- 共享账户下各成员不在交易所挂各自的保护止损（同一个币上几张止损单会互相打架），
  止损由各自内核在 bar 上触发，作为普通减仓委托参与合并。

分回去的回报和交易所回报格式一样（client_order_id / status / filled_qty / avg_price / fee），
各成员内核照常 apply_reports，所以回测、纸面、模拟盘的记账口径不变。
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

ZERO = Decimal(0)


def _d(x: Any) -> Decimal:
    return Decimal(str(x or 0))


@dataclass
class NetStats:
    rounds: int = 0
    crossed_notional: float = 0.0  # 内部对冲掉的成交额（省下的手续费 = 这个 × 费率）
    sent_orders: int = 0
    member_orders: int = 0
    capped: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "crossed_notional": round(self.crossed_notional, 2),
            "sent_orders": self.sent_orders,
            "member_orders": self.member_orders,
            "capped": self.capped,
            "log": self.log[-50:],
        }


class AccountNetter:
    """oms：有 submit(intent) -> report|None 的对象（DemoOMS）。

    positions_fn() -> {security: 当前账户持仓数量}；equity_fn() -> 账户权益；risk_cap 为 None 时不限。
    """

    def __init__(
        self,
        oms: Any,
        *,
        risk_cap: float | None = None,
        positions_fn: Callable[[], dict[str, Any]] | None = None,
        equity_fn: Callable[[], float] | None = None,
    ) -> None:
        self.oms = oms
        self.risk_cap = risk_cap
        self.positions_fn = positions_fn or (lambda: {})
        self.equity_fn = equity_fn
        self.stats = NetStats()

    # ------------------------------------------------------------------ 总敞口
    def _cap(self, batches: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
        """超过总敞口上限的加仓委托：返回 {成员: [拒单回报]}，并从 batches 里拿掉。"""
        out: dict[str, list[dict[str, Any]]] = {}
        if not self.risk_cap or self.equity_fn is None:
            return out
        equity = float(self.equity_fn() or 0)
        if equity <= 0:
            return out
        limit = equity * float(self.risk_cap)
        pos = {k: _d(v) for k, v in (self.positions_fn() or {}).items()}
        price: dict[str, Decimal] = {}
        for items in batches.values():
            for i in items:
                price[i["security"]] = _d(i.get("market_price") or i["ref_price"])

        def gross(p: dict[str, Decimal]) -> float:
            return float(sum(abs(q) * price.get(s, ZERO) for s, q in p.items()))

        after = dict(pos)
        for items in batches.values():
            for i in items:
                sgn = 1 if i["side"] == "buy" else -1
                after[i["security"]] = after.get(i["security"], ZERO) + sgn * _d(i["qty"])
        if gross(after) <= limit + 1e-9:
            return out

        def adds(i: dict[str, Any]) -> bool:
            # 让这个标的的绝对持仓变大的委托才算加仓
            q = pos.get(i["security"], ZERO)
            return (i["side"] == "buy" and q >= 0) or (i["side"] == "sell" and q <= 0)

        cands = sorted(
            ((m, i) for m, items in batches.items() for i in items if adds(i)),
            key=lambda mi: -float(_d(mi[1]["qty"]) * price[mi[1]["security"]]),
        )
        for m, i in cands:
            if gross(after) <= limit + 1e-9:
                break
            sgn = 1 if i["side"] == "buy" else -1
            after[i["security"]] -= sgn * _d(i["qty"])
            batches[m] = [x for x in batches[m] if x is not i]
            out.setdefault(m, []).append(
                {
                    "client_order_id": i["client_order_id"],
                    "status": "rejected",
                    "filled_qty": "0",
                    "code": "ACCOUNT_RISK_CAP",
                    "message": f"账户总敞口上限：持仓市值不超过权益的 {float(self.risk_cap):.0%}（{limit:,.0f}）",
                }
            )
            self.stats.capped += 1
        return out

    # ------------------------------------------------------------------ 一轮
    def process(self, batches: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
        """batches：{成员 id: 该成员内核本轮 outbox}。返回 {成员 id: 回报列表}。"""
        self.stats.rounds += 1
        out: dict[str, list[dict[str, Any]]] = {m: [] for m in batches}
        work: dict[str, list[dict[str, Any]]] = {}
        for m, items in batches.items():
            keep = []
            for i in items:
                if i.get("kind") == "protect":
                    continue  # 共享账户不挂各自的交易所止损，见模块说明
                keep.append(i)
            work[m] = keep
            self.stats.member_orders += len(keep)
        for m, reps in self._cap(work).items():
            out[m].extend(reps)

        by_sec: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for m, items in work.items():
            for i in items:
                by_sec.setdefault(i["security"], []).append((m, i))

        # 先处理净卖出的标的（回款给净买入用），和 OMS 的顺序一致
        def net_of(rows: list[tuple[str, dict[str, Any]]]) -> Decimal:
            return sum((_d(i["qty"]) if i["side"] == "buy" else -_d(i["qty"]) for _, i in rows), ZERO)

        for sec, rows in sorted(by_sec.items(), key=lambda kv: 0 if net_of(kv[1]) < 0 else 1):
            for m, rep in self._one(sec, rows).items():
                out[m].extend(rep)
        return out

    def _one(self, sec: str, rows: list[tuple[str, dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
        buys = [(m, i) for m, i in rows if i["side"] == "buy"]
        sells = [(m, i) for m, i in rows if i["side"] == "sell"]
        B = sum((_d(i["qty"]) for _, i in buys), ZERO)
        S = sum((_d(i["qty"]) for _, i in sells), ZERO)
        crossed = min(B, S)
        ref = _d(rows[-1][1].get("market_price") or rows[-1][1]["ref_price"])
        net = B - S
        big, small, T = (buys, sells, B) if net > 0 else (sells, buys, S)
        res: dict[str, list[dict[str, Any]]] = {}
        sent: dict[str, Any] | None = None
        if net != 0:
            first = big[0][1]
            key = "|".join(i["client_order_id"] for _, i in rows)
            intent = {
                **first,
                "client_order_id": "net-" + hashlib.sha1(key.encode()).hexdigest()[:20],
                "qty": format(abs(net).normalize(), "f"),
                "reason": "net",
                "signal_id": "",
                "members": [i["client_order_id"] for _, i in big],
            }
            sent = self.oms.submit(intent)
            self.stats.sent_orders += 1
        ext_qty = _d(sent.get("filled_qty")) if sent and sent.get("status") in ("filled", "partial") else ZERO
        ext_px = _d(sent.get("avg_price")) if ext_qty > 0 else ZERO
        ext_fee = _d(sent.get("fee")) if ext_qty > 0 else ZERO
        ts = (sent or {}).get("ts")
        self.stats.crossed_notional += float(crossed * ref) * 2
        if crossed > 0 or net != 0:
            self.stats.log.append(
                {
                    "security": sec,
                    "buy": str(B),
                    "sell": str(S),
                    "crossed": str(crossed),
                    "net": str(net),
                    "sent": (sent or {}).get("status", "") if net != 0 else "",
                    "ts": ts,
                }
            )
            del self.stats.log[:-200]

        def rep(i: dict[str, Any], internal: Decimal, ext: Decimal, fee: Decimal, msg: str) -> dict[str, Any]:
            q = internal + ext
            want = _d(i["qty"])
            if q <= 0:
                return {
                    "client_order_id": i["client_order_id"],
                    "status": "rejected",
                    "filled_qty": "0",
                    "message": (sent or {}).get("message") or "净额委托没有成交",
                }
            px = (internal * ref + ext * ext_px) / q
            return {
                "client_order_id": i["client_order_id"],
                "status": "filled" if q >= want else "partial",
                "filled_qty": format(q.normalize(), "f"),
                "avg_price": format(px, "f"),
                "fee": format(fee, "f"),
                "fee_currency": (sent or {}).get("fee_currency", "") if ext > 0 else "",
                "exchange_order_id": (sent or {}).get("exchange_order_id", "") if ext > 0 else "internal",
                "ts": ts,
                "message": msg,
            }

        for m, i in small:  # 较小一侧全部在内部对冲成交
            res.setdefault(m, []).append(rep(i, _d(i["qty"]), ZERO, ZERO, "同账户内部对冲成交，未送交易所"))
        if T > 0:
            internal_total = crossed
            for m, i in big:
                f = _d(i["qty"]) / T
                res.setdefault(m, []).append(
                    rep(i, internal_total * f, ext_qty * f, ext_fee * f, "与同账户其他策略合并为一笔净额委托")
                )
        return res


# ---------------------------------------------------------------------- 资金分配
def allocate(
    members: dict[str, dict[str, Any]], *, regime: str | None = None, tilt: float = 0.5, floor: float = 0.0
) -> dict[str, float]:
    """按行情状态和近期表现分配资金权重，和为 1。

    members：{id: {"base": 基础权重, "returns": 近期每期收益列表, "regimes": {状态: 倍数}}}。
    近期收益的 t 值 / 3 截断到 [-1, 1] 后按 tilt 倾斜：表现好的多分一点、差的少分一点，不会一下清零。
    """
    raw: dict[str, float] = {}
    for k, m in members.items():
        w = float(m.get("base", 1.0))
        if regime and m.get("regimes"):
            w *= float(m["regimes"].get(regime, 1.0))
        r = [float(x) for x in m.get("returns") or [] if x is not None and math.isfinite(float(x))]
        if len(r) >= 5:
            mu = sum(r) / len(r)
            sd = math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) or 1e-12
            s = max(-1.0, min(1.0, mu / sd * math.sqrt(len(r)) / 3))  # t 值 / 3
            w *= max(0.0, 1 + tilt * s)
        raw[k] = max(float(floor), w)
    tot = sum(raw.values())
    if tot <= 0:
        return {k: 0.0 for k in raw}
    return {k: v / tot for k, v in raw.items()}
