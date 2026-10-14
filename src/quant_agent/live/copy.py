"""跟单扩展：跟随模式、源策略关闭处置、跟随账户业绩口径（plan v2.0 第 183 行）。

平台本期仍然**不执行真实订单**（live_enabled=false）。这里定义的是"个人执行器"侧
必须实现的策略与口径：执行器拿到 `TargetPositionEnvelope` 后，按跟随者自己的资金、
持仓与风险上限，算出**自己**该下多少，而不是照搬源策略的数量。

三条硬规则：

1. 信封只给"目标权重"，不给数量。跟随者按自己的权益折算——源策略有 100 万，跟随者
   只有 1 万，照搬数量会直接爆仓。
2. 三种跟随模式（固定金额 / 权益比例 / 风险预算）都要有单标的与总敞口上限，
   并且尊重交易所最小下单量。
3. **源策略的历史收益不得显示为跟随账户的收益**。跟随账户的业绩只能由它自己的
   成交与资金流算出（`follower_report`），源策略的曲线是参考信息，不是收益。

源策略关闭后的处置（`on_source_closed`）三选一，必须显式声明，不能默认"什么都不做"
却对外宣称还在跟随。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..sdk.ledger import Ledger, dec, dstr

#: 三种跟随模式
COPY_MODES = ("fixed_amount", "equity_ratio", "risk_budget")

COPY_MODE_LABELS = {
    "fixed_amount": "固定金额：每个信号投入固定的计价币金额，与账户规模无关",
    "equity_ratio": "权益比例：按跟随账户当前权益的比例投入，账户越大仓位越大",
    "risk_budget": "风险预算：按单笔最大可承受亏损占权益的比例反推仓位，需要止损距离",
}

#: 源策略关闭后的处置
SOURCE_CLOSED_ACTIONS = ("keep", "stop_new", "exit")

SOURCE_CLOSED_LABELS = {
    "keep": "保留仓位并继续接受信号直到源策略真正停止（宽松）",
    "stop_new": "立即停止接受新信号，保留已有跟随仓位（默认，最稳妥）",
    "exit": "立即停止接受新信号，并按市价平掉全部跟随仓位",
}


class CopyError(ValueError):
    """跟单参数或状态不合法。消息面向使用者。"""


@dataclass
class CopyPolicy:
    """跟随账户的跟单策略。所有金额字段都是十进制字符串，避免浮点误差累积。"""

    mode: str = "equity_ratio"
    fixed_amount: str = "0"  # mode=fixed_amount 时每个信号投入的计价币金额
    equity_ratio: float = 0.0  # mode=equity_ratio 时投入的权益比例
    risk_budget: float = 0.0  # mode=risk_budget 时单笔可承受亏损占权益比例
    stop_loss_pct: float = 0.0  # mode=risk_budget 时用于反推仓位的止损距离（比例）
    max_weight: float = 1.0  # 单标的目标权重上限
    max_gross_exposure: float = 1.0  # 总敞口（各标的目标权重之和）上限
    min_order_value: str = "0"  # 交易所最小下单金额，低于它就不下单
    on_source_closed: str = "stop_new"

    def __post_init__(self) -> None:
        if self.mode not in COPY_MODES:
            raise CopyError(f"跟随模式只能是 {'、'.join(COPY_MODES)} 之一，收到 {self.mode!r}")
        if self.on_source_closed not in SOURCE_CLOSED_ACTIONS:
            raise CopyError(
                f"源策略关闭后的处置只能是 {'、'.join(SOURCE_CLOSED_ACTIONS)} 之一，收到 {self.on_source_closed!r}"
            )
        if not 0 < self.max_weight <= 1:
            raise CopyError("单标的上限 max_weight 必须在 (0, 1] 内")
        if not 0 < self.max_gross_exposure <= 1:
            raise CopyError("总敞口上限 max_gross_exposure 必须在 (0, 1] 内")
        if self.mode == "fixed_amount" and dec(self.fixed_amount) <= 0:
            raise CopyError("固定金额模式需要 fixed_amount > 0")
        if self.mode == "equity_ratio" and not 0 < self.equity_ratio <= 1:
            raise CopyError("权益比例模式的 equity_ratio 必须在 (0, 1] 内")
        if self.mode == "risk_budget":
            if not 0 < self.risk_budget <= 1:
                raise CopyError("风险预算模式的 risk_budget 必须在 (0, 1] 内")
            if not 0 < self.stop_loss_pct < 1:
                raise CopyError("风险预算模式需要 0 < stop_loss_pct < 1（用它把风险预算换算成仓位）")
        if dec(self.min_order_value) < 0:
            raise CopyError("最小下单金额不能为负")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "mode_label": COPY_MODE_LABELS[self.mode],
            "fixed_amount": self.fixed_amount,
            "equity_ratio": self.equity_ratio,
            "risk_budget": self.risk_budget,
            "stop_loss_pct": self.stop_loss_pct,
            "max_weight": self.max_weight,
            "max_gross_exposure": self.max_gross_exposure,
            "min_order_value": self.min_order_value,
            "on_source_closed": self.on_source_closed,
            "on_source_closed_label": SOURCE_CLOSED_LABELS[self.on_source_closed],
            "live_enabled": False,
            "note": "平台本期不执行真实订单；这里只计算跟随者自己的目标仓位",
        }

    # ------------------------------------------------------------------ 核心换算
    def target_notional(self, *, equity: Any, price: Any, source_weight: float) -> Decimal:
        """按跟随账户的权益算出这一笔该投多少计价币。

        三种模式都先算出"名义金额"，再统一受单标的权重上限约束——上限必须在换算之后
        生效，否则固定金额模式在账户很小时会给出超过 100% 权益的仓位。
        """
        eq = dec(equity)
        px = dec(price)
        if eq <= 0:
            raise CopyError("跟随账户权益必须为正，无法折算仓位")
        if px <= 0:
            raise CopyError("价格必须为正，无法折算仓位")
        w = max(0.0, min(1.0, float(source_weight)))

        if self.mode == "fixed_amount":
            notional = dec(self.fixed_amount)
        elif self.mode == "equity_ratio":
            notional = eq * dec(str(self.equity_ratio))
        else:  # risk_budget
            # 风险预算 = 权益 × risk_budget；仓位 = 风险预算 / 止损距离
            budget = eq * dec(str(self.risk_budget))
            notional = budget / dec(str(self.stop_loss_pct))

        # 源策略只想要 w 的仓位时，跟随者不该超过这个比例对应的金额
        notional = min(notional, eq * dec(str(w)))
        # 单标的上限
        notional = min(notional, eq * dec(str(self.max_weight)))
        return notional

    def plan(
        self,
        *,
        envelope: dict[str, Any],
        equity: Any,
        price: Any,
        current_notional: Any = 0,
        gross_exposure: float = 0.0,
        source_closed: bool = False,
    ) -> dict[str, Any]:
        """给出一笔跟随订单的计划（或明确的"不下单"理由）。

        返回的 `side` / `qty` 是**跟随者自己的**下单量，由自己的持仓与权益决定。
        """
        if source_closed and self.on_source_closed == "stop_new":
            return _skip("源策略已关闭，按 stop_new 处置：不再接受新信号")
        if source_closed and self.on_source_closed == "exit":
            return _skip("源策略已关闭，按 exit 处置：应平掉跟随仓位，不再开新仓")

        w = envelope.get("target_weight")
        if not isinstance(w, (int, float)) or isinstance(w, bool):
            raise CopyError("信封缺少合法的 target_weight")

        target = self.target_notional(equity=equity, price=price, source_weight=float(w))
        # 总敞口约束：把这一笔换成权重后，加上已有敞口不能超过上限
        eq = dec(equity)
        room = eq * dec(str(self.max_gross_exposure)) - eq * dec(str(gross_exposure))
        if room <= 0:
            return _skip(f"总敞口已达上限 {self.max_gross_exposure}，不再加仓")
        target = min(target, room)

        cur = dec(current_notional)
        delta = target - cur
        if delta <= 0:
            return _skip(
                "目标金额不高于当前持仓，按跟单口径不减仓（减仓由跟随者自己的风控决定）",
                target_notional=dstr(target),
                current_notional=dstr(cur),
            )
        if target < dec(self.min_order_value):
            return _skip(
                f"目标金额 {dstr(target)} 低于最小下单金额 {self.min_order_value}，跳过",
                target_notional=dstr(target),
            )
        qty = delta / dec(price)
        return {
            "action": "order",
            "side": "buy",
            "qty": dstr(qty),
            "notional": dstr(delta),
            "target_notional": dstr(target),
            "current_notional": dstr(cur),
            "price": dstr(dec(price)),
            "mode": self.mode,
            "reason": f"跟随 {envelope.get('signal_id')}：{COPY_MODE_LABELS[self.mode]}",
            "live_enabled": False,
        }


def _skip(reason: str, **extra: Any) -> dict[str, Any]:
    return {"action": "skip", "reason": reason, "live_enabled": False, **extra}


def plan_exit(policy: CopyPolicy, *, positions: dict[str, Any], prices: dict[str, Any]) -> list[dict[str, Any]]:
    """源策略关闭且处置为 exit 时，生成跟随账户的平仓计划（仍不真实下单）。"""
    out: list[dict[str, Any]] = []
    for inst, qty in positions.items():
        q = dec(qty)
        if q <= 0:
            continue
        px = prices.get(inst)
        if not px:
            out.append(
                {
                    "action": "hold",
                    "instrument_id": inst,
                    "reason": "缺少价格，无法生成平仓计划（不猜价格）",
                    "live_enabled": False,
                }
            )
            continue
        out.append(
            {
                "action": "order",
                "side": "sell",
                "instrument_id": inst,
                "qty": dstr(q),
                "notional": dstr(q * dec(px)),
                "price": dstr(dec(px)),
                "reason": "源策略已关闭，按 exit 处置平掉跟随仓位",
                "live_enabled": False,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 跟随账户业绩口径
# ---------------------------------------------------------------------------
def follower_report(
    *,
    fills: list[dict[str, Any]],
    flows: list[dict[str, Any]] | None = None,
    base_currency: str = "USDT",
    source_returns: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """只用**跟随账户自己的**成交与资金流算业绩。

    `source_returns` 是源策略的曲线，仅作为参考放进 `reference` 字段，并在返回里显式标注
    "参考信息，不是跟随账户收益"。调用方如果试图把它当成跟随收益展示，会在这里被拦住。
    """
    led = Ledger(base_currency=base_currency)
    flows = flows or []
    realized = Decimal(0)
    skipped = 0
    for f in sorted(fills, key=lambda x: int(x.get("ts") or 0)):
        try:
            b = led.trade(
                instrument=f.get("instrument") or f.get("security") or "",
                side=str(f.get("side") or "").lower(),
                qty=dec(f.get("qty") or f.get("amount") or 0),
                price=dec(f.get("price") or 0),
                fee=dec(f.get("fee") or 0),
                fee_currency=str(f.get("fee_currency") or base_currency).upper(),
                ts=int(f.get("ts") or 0),
            )
            realized += b.realized
        except Exception:
            skipped += 1
    for fl in flows:
        amt = dec(fl.get("amount") or 0)
        ts = int(fl.get("ts") or 0)
        if amt > 0:
            led.deposit(amt, ts=ts, ref=str(fl.get("ref") or f"flow:{ts}"))
        elif amt < 0:
            led.withdraw(-amt, ts=ts, ref=str(fl.get("ref") or f"flow:{ts}"))

    return {
        "basis": "跟随账户自己的成交与资金流",
        "realized_pnl": dstr(realized),
        "fees": dstr(led.fees_value),
        "net_contributions": dstr(led.net_contributions),
        "open_positions": {k: dstr(h.qty) for k, h in led.holdings.items() if h.qty > 0},
        "fills": len(fills),
        "skipped": skipped,
        "reference": {
            "source_returns": source_returns or None,
            "label": "源策略历史收益——参考信息，**不是**跟随账户的收益，不得直接展示为跟随账户业绩",
        },
        "note": (
            "跟随账户的业绩只由它自己的成交与资金流计算；"
            "源策略的收益曲线受其资金规模、成交价与费率影响，直接搬过来会严重高估跟随收益"
        ),
    }


def assert_not_source_returns(payload: dict[str, Any]) -> None:
    """防御性检查：拒绝把源策略收益字段当成跟随账户收益返回。

    给执行器/前端留一个显式的断言，避免"顺手把源曲线填进跟随收益"这类错误。
    """
    forbidden = ("source_return", "source_returns", "source_equity", "source_nav")
    for key in forbidden:
        if payload.get(key) is not None:
            raise CopyError(
                f"不能把 `{key}` 作为跟随账户收益：源策略收益只能作为参考信息展示，"
                "跟随账户业绩必须由自己的成交与资金流计算"
            )
