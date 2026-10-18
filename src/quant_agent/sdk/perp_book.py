"""Contract ledger v2 for linear, inverse and delivery derivatives.

The ledger deliberately separates three concepts:

* position quantity is measured in contracts and stays positive on a leg;
* settlement currency is what margin, PnL and fees are actually denominated
  in (USDT for linear contracts, the base coin for coin-margined contracts);
* account equity is always translated back to the account quote currency
  using the mark price supplied by the caller.

This makes the bookkeeping auditable: a BTC/USD inverse leg never moves USDT
cash merely because its formula happens to be easier to read in USD.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from ..exchanges.contracts import ContractSpec, spec_for

ZERO = Decimal(0)
MARGIN_STEP = Decimal("1e-12")
PERP_PNL = "income:perp_pnl"
MARGIN = "margin"
LIQUIDATION = "expenses:liquidation"
FUNDING = "expenses:funding"
FEES = "expenses:fees"


def dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def q(value: Decimal) -> Decimal:
    return (value / MARGIN_STEP).to_integral_value(rounding=ROUND_HALF_UP) * MARGIN_STEP


def _dstr(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


@dataclass
class PerpLeg:
    instrument: str
    side: str
    qty: Decimal = ZERO
    entry: Decimal = ZERO
    margin: Decimal = ZERO
    leverage: Decimal = Decimal(1)
    mmr: Decimal = Decimal("0.005")
    realized: Decimal = ZERO
    funding: Decimal = ZERO
    opened_ts: int = 0
    last_price: Decimal = ZERO
    liquidations: int = 0
    mmr_tier: str = ""
    liquidation_fee_bps: Decimal = ZERO
    liquidation_slippage_bps: Decimal = ZERO
    liquidation_fee: Decimal = ZERO
    liquidation_mark_price: Decimal = ZERO
    liquidation_reason: str = ""
    settlement_ccy: str = "USDT"
    contract_type: str = "linear"

    @property
    def signed_qty(self) -> Decimal:
        return self.qty if self.side == "long" else -self.qty

    def notional(self, spec: ContractSpec, price: Decimal | None = None) -> Decimal:
        p = dec(price if price is not None else self.last_price)
        mult = dec(spec.contract_multiplier)
        if spec.contract_type == "inverse":
            return self.qty * mult
        return self.qty * mult * p

    def unrealized(self, spec: ContractSpec, price: Decimal | None = None) -> Decimal:
        if not self.qty:
            return ZERO
        p = dec(price if price is not None else self.last_price)
        mult = dec(spec.contract_multiplier)
        if p <= 0 or self.entry <= 0:
            return ZERO
        if spec.contract_type == "inverse":
            return q(self.signed_qty * mult * (Decimal(1) / self.entry - Decimal(1) / p))
        return q(self.signed_qty * mult * (p - self.entry))

    def maintenance(self, spec: ContractSpec, price: Decimal | None = None) -> Decimal:
        p = dec(price if price is not None else self.last_price)
        if not self.qty or p <= 0:
            return ZERO
        if spec.contract_type == "inverse":
            return q(abs(self.qty) * dec(spec.contract_multiplier) * self.mmr / p)
        return q(abs(self.qty) * dec(spec.contract_multiplier) * p * self.mmr)

    def liquidation_price(self, spec: ContractSpec) -> Decimal:
        if not self.qty or self.entry <= 0:
            return ZERO
        with localcontext() as c:
            c.prec = 40
            face = abs(self.qty) * dec(spec.contract_multiplier)
            if spec.contract_type == "inverse":
                ratio = self.margin * self.entry / face if face else ZERO
                if self.side == "long":
                    return max(self.entry * (Decimal(1) + self.mmr) / (Decimal(1) + ratio), ZERO)
                denom = Decimal(1) - ratio
                return max(self.entry * (Decimal(1) - self.mmr) / denom, ZERO) if denom > 0 else ZERO
            if self.side == "long":
                return max((self.entry - self.margin / face) / (Decimal(1) - self.mmr), ZERO)
            return max((self.entry + self.margin / face) / (Decimal(1) + self.mmr), ZERO)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: _dstr(v) if isinstance(v, Decimal) else v for k, v in d.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PerpLeg:
        return cls(
            instrument=d["instrument"],
            side=d.get("side") or "long",
            qty=dec(d.get("qty") or 0),
            entry=dec(d.get("entry") or 0),
            margin=dec(d.get("margin") or 0),
            leverage=dec(d.get("leverage") or 1),
            mmr=dec(d.get("mmr") or "0.005"),
            realized=dec(d.get("realized") or 0),
            funding=dec(d.get("funding") or 0),
            opened_ts=int(d.get("opened_ts") or 0),
            last_price=dec(d.get("last_price") or 0),
            liquidations=int(d.get("liquidations") or 0),
            mmr_tier=str(d.get("mmr_tier") or ""),
            liquidation_fee_bps=dec(d.get("liquidation_fee_bps") or 0),
            liquidation_slippage_bps=dec(d.get("liquidation_slippage_bps") or 0),
            liquidation_fee=dec(d.get("liquidation_fee") or 0),
            liquidation_mark_price=dec(d.get("liquidation_mark_price") or 0),
            liquidation_reason=str(d.get("liquidation_reason") or ""),
            settlement_ccy=str(d.get("settlement_ccy") or "USDT"),
            contract_type=str(d.get("contract_type") or "linear"),
        )


@dataclass
class PerpFill:
    instrument: str
    side: str
    qty: Decimal
    price: Decimal
    opened_qty: Decimal = ZERO
    closed_qty: Decimal = ZERO
    realized: Decimal = ZERO
    fee: Decimal = ZERO
    fee_value: Decimal = ZERO
    settlement_ccy: str = "USDT"
    leg: PerpLeg | None = None
    liquidation: bool = False


@dataclass
class LiquidationResult:
    ok: bool
    legs: int
    qty: Decimal
    realized: Decimal = ZERO
    fee: Decimal = ZERO
    seized: Decimal = ZERO
    reason: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "legs": self.legs,
            "qty": _dstr(self.qty),
            "realized": _dstr(self.realized),
            "fee": _dstr(self.fee),
            "seized": _dstr(self.seized),
            "reason": self.reason,
            "details": list(self.details),
        }


@dataclass
class PerpBook:
    margin_mode: str = "isolated"
    position_mode: str = "one-way"
    contract_type: str = "linear"
    quote_ccy: str = "USDT"
    settlement_ccy: str = "USDT"
    expiry_ts: int | None = None
    exchange: str = "binance"
    legs: dict[str, PerpLeg] = field(default_factory=dict)
    leverage: dict[str, Decimal] = field(default_factory=dict)
    specs: dict[str, ContractSpec] = field(default_factory=dict)
    liquidation_loss: Decimal = ZERO
    funding_events: int = 0
    funding_by_instrument: dict[str, Decimal] = field(default_factory=dict)
    funding_details: list[dict[str, Any]] = field(default_factory=list)
    expired: bool = False
    realized_quote: Decimal = ZERO
    last_marks: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.margin_mode not in ("isolated", "cross"):
            raise ValueError("margin_mode 只能是 isolated 或 cross")
        if self.position_mode not in ("one-way", "hedge"):
            raise ValueError("position_mode 只能是 one-way 或 hedge")
        if self.contract_type not in ("linear", "inverse", "delivery"):
            raise ValueError("contract_type 只能是 linear、inverse 或 delivery")

    @property
    def cross(self) -> bool:
        return self.margin_mode == "cross"

    @property
    def hedge(self) -> bool:
        return self.position_mode == "hedge"

    def add_spec(self, spec: ContractSpec) -> None:
        self.specs[spec.symbol.upper()] = spec
        if spec.contract_type == "inverse":
            self.settlement_ccy = spec.settlement
        if spec.expiry_ts:
            self.expiry_ts = spec.expiry_ts

    def spec(self, instrument: str) -> ContractSpec:
        key = instrument.upper()
        spec = self.specs.get(key)
        if spec is None:
            spec = spec_for(self.exchange, instrument, self.expiry_ts)
            self.add_spec(spec)
        return spec

    def settlement_for(self, instrument: str) -> str:
        return self.spec(instrument).settlement or self.quote_ccy

    def conversion_price(self, instrument: str, prices: dict[str, Any] | None = None) -> Decimal:
        spec = self.spec(instrument)
        if spec.settlement == self.quote_ccy:
            return Decimal(1)
        p = dec((prices or {}).get(instrument) or 0)
        if p <= 0:
            leg = next((x for x in self.all_legs() if x.instrument.upper() == instrument.upper()), None)
            p = leg.last_price if leg else ZERO
        if p <= 0:
            return ZERO
        return p

    def key(self, instrument: str, side: str = "") -> str:
        key = instrument.upper()
        return f"{key}|{side.upper()}" if self.hedge else key

    def leg(self, instrument: str, side: str) -> PerpLeg:
        side = side.lower()
        if side not in ("long", "short"):
            raise ValueError("leg side 只能是 long 或 short")
        k = self.key(instrument, side)
        leg = self.legs.get(k)
        if leg is None:
            spec = self.spec(instrument)
            leg = PerpLeg(
                instrument=instrument,
                side=side,
                leverage=self.leverage.get(instrument.upper(), Decimal(1)),
                mmr=dec(spec.mmr_for_notional(0)),
                last_price=ZERO,
                settlement_ccy=spec.settlement or self.quote_ccy,
                contract_type=spec.contract_type,
            )
            self.legs[k] = leg
        return leg

    def all_legs(self) -> list[PerpLeg]:
        return [x for x in self.legs.values() if x.qty]

    def net_qty(self, instrument: str) -> Decimal:
        return sum((x.signed_qty for x in self.all_legs() if x.instrument.upper() == instrument.upper()), ZERO)

    def mark(self, prices: dict[str, Any]) -> None:
        lookup = {k.upper(): v for k, v in prices.items()}
        for key, value in lookup.items():
            self.last_marks[key] = str(value)
        for leg in self.all_legs():
            value = lookup.get(leg.instrument.upper())
            if value is not None:
                leg.last_price = dec(value)

    def spot_value(self, ledger: Any) -> Decimal:
        return sum((h.qty * h.last_price for h in ledger.holdings.values() if h.qty), ZERO)

    def _cash_value(self, ledger: Any, prices: dict[str, Any] | None = None) -> Decimal:
        total = ledger.cash(self.quote_ccy)
        currencies = {leg.settlement_ccy for leg in self.all_legs()}
        currencies.update({self.settlement_ccy, self.quote_ccy})
        lookup = {k.upper(): v for k, v in (prices or {}).items()}
        for ccy in currencies:
            if ccy == self.quote_ccy:
                continue
            amount = ledger.cash(ccy)
            if not amount:
                continue
            px = ZERO
            for leg in self.all_legs():
                if leg.settlement_ccy == ccy:
                    px = dec(lookup.get(leg.instrument.upper()) or leg.last_price)
                    if px > 0:
                        break
            if px <= 0:
                for k, v in lookup.items():
                    if k.startswith(f"{ccy.upper()}/"):
                        px = dec(v)
                        break
            if px <= 0:
                px = dec(next((v for k, v in self.last_marks.items() if k.startswith(f"{ccy.upper()}/")), 0))
            total += amount * px
        return total

    def posted_margin(self, prices: dict[str, Any] | None = None) -> Decimal:
        if self.cross:
            return ZERO
        total = ZERO
        for leg in self.all_legs():
            total += leg.margin * self.conversion_price(leg.instrument, prices)
        return total

    def unrealized(self, prices: dict[str, Any] | None = None) -> Decimal:
        total = ZERO
        for leg in self.all_legs():
            p = dec(prices[leg.instrument]) if prices and leg.instrument in prices else leg.last_price
            total += leg.unrealized(self.spec(leg.instrument), p) * self.conversion_price(leg.instrument, prices)
        return total

    def maintenance(self, prices: dict[str, Any] | None = None) -> Decimal:
        total = ZERO
        for leg in self.all_legs():
            p = dec(prices[leg.instrument]) if prices and leg.instrument in prices else leg.last_price
            total += leg.maintenance(self.spec(leg.instrument), p) * self.conversion_price(leg.instrument, prices)
        return total

    def equity(self, ledger: Any, prices: dict[str, Any] | None = None) -> Decimal:
        return (
            self._cash_value(ledger, prices)
            + self.spot_value(ledger)
            + self.posted_margin(prices)
            + self.unrealized(prices)
        )

    def available(self, ledger: Any, prices: dict[str, Any] | None = None) -> Decimal:
        return self.equity(ledger, prices) - self.maintenance(prices)

    def margin_for(self, instrument: str, qty: Decimal, price: Decimal) -> Decimal:
        spec = self.spec(instrument)
        lev = self.leverage.get(instrument.upper(), Decimal(1))
        if lev <= 0:
            raise ValueError("杠杆必须为正")
        mult = dec(spec.contract_multiplier)
        if spec.contract_type == "inverse":
            return q(abs(qty) * mult / (price * lev)) if price > 0 else ZERO
        return q(abs(qty) * mult * price / lev)

    def _fee_to_quote(self, instrument: str, fee: Decimal, price: Decimal) -> Decimal:
        spec = self.spec(instrument)
        if spec.settlement == self.quote_ccy:
            return fee
        return fee * price

    def _post(
        self, ledger: Any, ts: int, kind: str, ref: str, postings: list[tuple[str, str, Decimal]], memo: str
    ) -> None:
        from .ledger import Posting

        ledger._post(ts, kind, ref, [Posting(a, c, v) for a, c, v in postings], memo)

    def _pnl(self, leg: PerpLeg, qty: Decimal, price: Decimal, spec: ContractSpec) -> Decimal:
        if not leg.qty or leg.entry <= 0 or price <= 0:
            return ZERO
        ratio = qty / leg.qty
        if spec.contract_type == "inverse":
            return q(
                leg.signed_qty * ratio * dec(spec.contract_multiplier) * (Decimal(1) / leg.entry - Decimal(1) / price)
            )
        return q(leg.signed_qty * ratio * dec(spec.contract_multiplier) * (price - leg.entry))

    def _record_fee(self, ledger: Any, fee: Decimal, fee_ccy: str, value: Decimal) -> None:
        if not fee:
            return
        ledger.fees_by_currency[fee_ccy] = ledger.fees_by_currency.get(fee_ccy, ZERO) + fee
        ledger.fees_value += value

    def trade(
        self,
        ledger: Any,
        *,
        instrument: str,
        side: str,
        qty: Any,
        price: Any,
        fee: Any = 0,
        position_side: str = "",
        leverage: Any | None = None,
        ts: int = 0,
        ref: str = "",
        mmr: Any | None = None,
        mmr_tier: str = "",
        liquidation_fee_bps: float = 50.0,
        liquidation_slippage_bps: float = 10.0,
    ) -> PerpFill:
        total_qty, px, total_fee = dec(qty), dec(price), dec(fee)
        if total_qty <= 0 or px <= 0 or total_fee < 0:
            raise ValueError("数量、价格必须为正，手续费不能为负")
        if side not in ("buy", "sell"):
            raise ValueError(f"未知方向：{side}")
        spec = self.spec(instrument)
        settlement = spec.settlement or self.quote_ccy
        if leverage is not None:
            lev = dec(leverage)
            if lev < 1:
                raise ValueError("杠杆必须至少为 1")
            self.leverage[instrument.upper()] = lev
        wanted = position_side.lower()
        if wanted not in ("", "long", "short"):
            raise ValueError("position_side 只能是 long 或 short")
        if not wanted:
            wanted = "long" if side == "buy" else "short"

        remaining = total_qty
        remaining_fee = total_fee
        opened = ZERO
        closed = ZERO
        close_realized = ZERO
        fee_used = ZERO
        last_leg: PerpLeg | None = None

        while remaining > 0:
            if self.hedge:
                leg = self.leg(instrument, wanted)
                can_close = bool(leg.qty) and (
                    (wanted == "long" and side == "sell") or (wanted == "short" and side == "buy")
                )
            else:
                net = self.net_qty(instrument)
                if net and ((net > 0) != (side == "buy")):
                    leg = next(x for x in self.all_legs() if x.instrument.upper() == instrument.upper())
                    can_close = True
                else:
                    leg = self.leg(instrument, wanted)
                    can_close = False
            if can_close:
                close_qty = min(remaining, leg.qty)
                close_fee = remaining_fee * close_qty / remaining if remaining else ZERO
                close_realized += self._close(ledger, leg, close_qty, px, close_fee, ts, ref, spec)
                remaining -= close_qty
                remaining_fee -= close_fee
                closed += close_qty
                fee_used += close_fee
                last_leg = leg
                if remaining <= 0:
                    break
                wanted = "long" if side == "buy" else "short"
                continue

            open_margin = self.margin_for(instrument, remaining, px)
            available = self.available(ledger) if self.cross else ledger.cash(settlement)
            if available < open_margin + remaining_fee:
                raise ValueError(
                    f"{settlement} 可用不足：需要保证金 {_dstr(open_margin)} + 手续费 {_dstr(remaining_fee)}"
                )
            if not leg.qty:
                leg.side = wanted
                leg.opened_ts = ts
                leg.entry = px
                leg.leverage = self.leverage.get(instrument.upper(), Decimal(1))
                leg.funding = ZERO
            else:
                with localcontext() as c:
                    c.prec = 40
                    leg.entry = (leg.qty * leg.entry + remaining * px) / (leg.qty + remaining)
            if mmr is not None:
                leg.mmr = dec(mmr)
            notional = abs((leg.qty + remaining) * dec(spec.contract_multiplier))
            if spec.contract_type != "inverse":
                notional *= px
            tier = spec.tier_for_notional(float(notional))
            leg.mmr_tier = mmr_tier or tier["label"]
            leg.leverage = self.leverage.get(instrument.upper(), leg.leverage)
            leg.liquidation_fee_bps = dec(liquidation_fee_bps)
            leg.liquidation_slippage_bps = dec(liquidation_slippage_bps)
            leg.qty += remaining
            leg.last_price = px
            leg.margin += open_margin
            postings: list[tuple[str, str, Decimal]] = []
            if not self.cross:
                postings += [("margin", settlement, open_margin), ("assets", settlement, -open_margin)]
            if remaining_fee:
                postings += [("expenses:fees", settlement, remaining_fee), ("assets", settlement, -remaining_fee)]
            if postings:
                self._post(ledger, ts, "perp_trade", ref, postings, f"开{leg.side} {remaining} {instrument} @ {px}")
            self._record_fee(ledger, remaining_fee, settlement, self._fee_to_quote(instrument, remaining_fee, px))
            opened += remaining
            fee_used += remaining_fee
            remaining = ZERO
            last_leg = leg

        if last_leg is None:
            raise ValueError("成交没有产生任何腿")
        quote_mult = px if settlement != self.quote_ccy else Decimal(1)
        return PerpFill(
            instrument=instrument,
            side=side,
            qty=total_qty,
            price=px,
            opened_qty=opened,
            closed_qty=closed,
            realized=close_realized * quote_mult,
            fee=fee_used,
            fee_value=fee_used * quote_mult,
            settlement_ccy=settlement,
            leg=last_leg,
        )

    def _close(
        self,
        ledger: Any,
        leg: PerpLeg,
        qty: Decimal,
        price: Decimal,
        fee: Decimal,
        ts: int,
        ref: str,
        spec: ContractSpec,
        pnl_override: Decimal | None = None,
    ) -> Decimal:
        qty = min(qty, leg.qty)
        if qty <= 0:
            return ZERO
        settlement = spec.settlement or self.quote_ccy
        pnl = self._pnl(leg, qty, price, spec) if pnl_override is None else pnl_override
        released = leg.margin * qty / leg.qty if leg.qty else ZERO
        postings: list[tuple[str, str, Decimal]] = []
        if not self.cross:
            postings += [("margin", settlement, -released), ("assets", settlement, released)]
        if pnl:
            postings += [("assets", settlement, pnl), (PERP_PNL, settlement, -pnl)]
        if fee:
            postings += [("expenses:fees", settlement, fee), ("assets", settlement, -fee)]
        if postings:
            self._post(ledger, ts, "perp_trade", ref, postings, f"平{leg.side} {qty} {leg.instrument} @ {price}")
        leg.qty -= qty
        leg.margin = ZERO if leg.qty == 0 else max(leg.margin - released, ZERO)
        if not leg.qty:
            leg.entry = ZERO
            leg.margin = ZERO
        leg.realized += pnl - fee
        self.realized_quote += (pnl - fee) * (price if settlement != self.quote_ccy else Decimal(1))
        leg.last_price = price
        self._record_fee(ledger, fee, settlement, self._fee_to_quote(leg.instrument, fee, price))
        return pnl - fee

    def settle_funding(
        self, ledger: Any, instrument: str, rate: Any, mark_price: Any, ts: int = 0, ref: str = ""
    ) -> Decimal:
        rate_dec, mark = dec(rate), dec(mark_price)
        total_quote = ZERO
        for leg in self.all_legs():
            if leg.instrument.upper() != instrument.upper():
                continue
            spec = self.spec(leg.instrument)
            settlement = spec.settlement or self.quote_ccy
            if spec.contract_type == "inverse":
                cash = -leg.signed_qty * dec(spec.contract_multiplier) * rate_dec / mark
            else:
                cash = -leg.signed_qty * dec(spec.contract_multiplier) * mark * rate_dec
            cash = q(cash)
            if not cash:
                continue
            self._post(
                ledger,
                ts,
                "funding",
                ref,
                [(FUNDING, settlement, -cash), ("assets", settlement, cash)],
                f"资金费 {leg.instrument} {leg.side} @ {mark}",
            )
            leg.funding += cash
            leg.realized += cash
            quote_cash = cash * (mark if settlement != self.quote_ccy else Decimal(1))
            total_quote += quote_cash
            self.realized_quote += quote_cash
            self.funding_by_instrument[leg.instrument] = (
                self.funding_by_instrument.get(leg.instrument, ZERO) + quote_cash
            )
            self.funding_details.append(
                {
                    "instrument": leg.instrument,
                    "side": leg.side,
                    "ts": ts,
                    "rate": str(rate_dec),
                    "mark_price": str(mark),
                    "settlement_ccy": settlement,
                    "cash_delta": _dstr(cash),
                    "quote_cash_delta": _dstr(quote_cash),
                    "ref": ref,
                }
            )
        self.funding_events += 1
        return total_quote

    def _liquidate_isolated(
        self,
        ledger: Any,
        leg: PerpLeg,
        *,
        price: Decimal,
        ts: int,
        ref: str,
        fee_bps: Decimal,
        mark_price: Decimal,
        reason: str,
    ) -> LiquidationResult:
        spec = self.spec(leg.instrument)
        settlement = spec.settlement or self.quote_ccy
        margin = leg.margin
        pnl = max(self._pnl(leg, leg.qty, price, spec), -margin)
        remaining = max(margin + pnl, ZERO)
        fee_settle = min(max(margin * fee_bps / Decimal(10000), ZERO), remaining)
        seized = remaining
        postings: list[tuple[str, str, Decimal]] = [
            ("margin", settlement, -margin),
            ("assets", settlement, margin),
        ]
        if pnl:
            postings += [("assets", settlement, pnl), (PERP_PNL, settlement, -pnl)]
        if seized:
            postings += [("assets", settlement, -seized), (LIQUIDATION, settlement, seized)]
        self._post(ledger, ts, "liquidation", ref, postings, f"强平 {leg.instrument} {leg.side} @ {price}；{reason}")
        quote_mult = price if settlement != self.quote_ccy else Decimal(1)
        lost_quote = margin * quote_mult
        leg.realized -= margin
        leg.liquidation_fee = fee_settle
        leg.liquidation_mark_price = mark_price
        leg.liquidation_reason = reason
        leg.liquidations += 1
        self.liquidation_loss += lost_quote
        self.realized_quote -= lost_quote
        details = {
            "instrument": leg.instrument,
            "side": leg.side,
            "qty": _dstr(leg.qty),
            "entry": _dstr(leg.entry),
            "mark_price": _dstr(mark_price),
            "exit_price": _dstr(price),
            "settlement_ccy": settlement,
            "margin": _dstr(margin),
            "pnl": _dstr(pnl),
            "liquidation_fee": _dstr(fee_settle),
            "seized": _dstr(seized),
            "mmr_tier": leg.mmr_tier,
            "reason": reason,
        }
        leg.qty = ZERO
        leg.margin = ZERO
        leg.entry = ZERO
        leg.last_price = price
        return LiquidationResult(
            True, 1, Decimal(0), -lost_quote, fee_settle * quote_mult, lost_quote, reason, [details]
        )

    def liquidate(
        self,
        ledger: Any,
        leg: PerpLeg,
        *,
        price: Any,
        ts: int = 0,
        ref: str = "",
        fee: Any = 0,
        mark_price: Any = None,
        reason: str = "",
    ) -> LiquidationResult:
        px = dec(price)
        mark = dec(mark_price or px)
        spec = self.spec(leg.instrument)
        if self.cross:
            return self.liquidate_cross_all(ledger, {leg.instrument: px}, ts=ts, ref=ref, reason=reason)
        fee_bps = leg.liquidation_fee_bps or dec(spec.liquidation_fee_bps)
        return self._liquidate_isolated(
            ledger,
            leg,
            price=px,
            ts=ts,
            ref=ref,
            fee_bps=fee_bps,
            mark_price=mark,
            reason=reason,
        )

    def liquidate_cross_all(
        self,
        ledger: Any,
        prices: dict[str, Any],
        *,
        ts: int = 0,
        ref: str = "",
        reason: str = "",
        fee_bps: Decimal | None = None,
    ) -> LiquidationResult:
        legs = list(self.all_legs())
        if not legs:
            return LiquidationResult(False, 0, ZERO, reason=reason)
        total_qty = ZERO
        details: list[dict[str, Any]] = []
        equity_before = self.equity(ledger, prices)
        exits: list[tuple[PerpLeg, Decimal, Decimal, Decimal]] = []
        total_pnl = ZERO
        for leg in legs:
            spec = self.spec(leg.instrument)
            mark = dec(prices.get(leg.instrument) or leg.last_price)
            slip = (leg.liquidation_slippage_bps or dec(spec.liquidation_slippage_bps)) / Decimal(10000)
            exit_px = mark * (Decimal(1) - slip if leg.side == "long" else Decimal(1) + slip)
            if exit_px <= 0:
                exit_px = mark
            pnl = self._pnl(leg, leg.qty, exit_px, spec)
            exits.append((leg, exit_px, pnl, mark))
            total_pnl += pnl
        # Cross margin is limited by account equity.  Allocate a proportional
        # cap so a gap through bankruptcy cannot create a negative customer
        # balance in the simplified model.
        cap = max(equity_before, ZERO)
        scale = cap / -total_pnl if total_pnl < -cap and total_pnl < 0 else Decimal(1)
        for leg, exit_px, pnl, mark in exits:
            if scale < 1 and pnl < 0:
                pnl = pnl * scale
            qty = leg.qty
            self._close(ledger, leg, qty, exit_px, ZERO, ts, ref, spec, pnl_override=pnl)
            total_qty += qty
            details.append(
                {
                    "instrument": leg.instrument,
                    "side": leg.side,
                    "qty": _dstr(qty),
                    "entry": _dstr(leg.entry),
                    "mark_price": _dstr(mark),
                    "exit_price": _dstr(exit_px),
                    "pnl": _dstr(pnl),
                    "settlement_ccy": spec.settlement,
                    "reason": reason,
                }
            )
        residual = max(self.equity(ledger, prices), ZERO)
        fee_quote = residual * (fee_bps / Decimal(10000)) if fee_bps is not None else ZERO
        fee_quote = min(fee_quote, residual)
        seized = residual
        ccy = self.settlement_ccy or self.quote_ccy
        amount = ledger.cash(ccy)
        if ccy != self.quote_ccy:
            px = next(
                (dec(prices.get(leg.instrument) or leg.last_price) for leg in legs if leg.settlement_ccy == ccy),
                ZERO,
            )
            if px > 0:
                amount = min(amount, seized / px)
        else:
            amount = min(amount, seized)
        amount = max(amount, ZERO)
        if amount:
            self._post(
                ledger,
                ts,
                "liquidation",
                ref,
                [("assets", ccy, -amount), (LIQUIDATION, ccy, amount)],
                f"全仓联合强平；{reason}",
            )
        self.liquidation_loss += seized
        self.realized_quote -= seized
        for leg in legs:
            leg.liquidations += 1
            leg.liquidation_mark_price = dec(prices.get(leg.instrument) or leg.last_price)
            leg.liquidation_reason = reason
            leg.qty = ZERO
            leg.margin = ZERO
            leg.entry = ZERO
        return LiquidationResult(True, len(legs), total_qty, -seized, fee_quote, seized, reason, details)

    def liquidatable(self, ledger: Any, prices: dict[str, Any]) -> bool:
        if not self.all_legs():
            return False
        if self.cross:
            return self.equity(ledger, prices) <= self.maintenance(prices)
        for leg in self.all_legs():
            p = dec(prices.get(leg.instrument) or leg.last_price)
            liq = leg.liquidation_price(self.spec(leg.instrument))
            if (leg.side == "long" and p <= liq) or (leg.side == "short" and p >= liq):
                return True
        return False

    def settle_expiry(
        self,
        ledger: Any,
        settlement_price: Any,
        ts: int = 0,
        ref: str = "",
        instrument: str | None = None,
    ) -> Decimal:
        if not self.expiry_ts or ts < self.expiry_ts:
            return ZERO
        px = dec(settlement_price)
        total = ZERO
        legs = [x for x in self.all_legs() if instrument is None or x.instrument.upper() == instrument.upper()]
        for leg in legs:
            spec = self.spec(leg.instrument)
            pnl = self._pnl(leg, leg.qty, px, spec)
            self._close(ledger, leg, leg.qty, px, ZERO, ts, ref or "delivery", spec)
            total += pnl * (px if (spec.settlement or self.quote_ccy) != self.quote_ccy else Decimal(1))
        if instrument is None:
            self.expired = True
        return total

    def realized_value(self, prices: dict[str, Any] | None = None) -> Decimal:
        # Closed legs disappear from all_legs(), so lifetime realized PnL is
        # maintained as a quote-currency aggregate instead of being inferred
        # from currently open positions.
        return self.realized_quote

    def to_dict(self) -> dict[str, Any]:
        return {
            "margin_mode": self.margin_mode,
            "position_mode": self.position_mode,
            "contract_type": self.contract_type,
            "quote_ccy": self.quote_ccy,
            "settlement_ccy": self.settlement_ccy,
            "expiry_ts": self.expiry_ts,
            "exchange": self.exchange,
            "expired": self.expired,
            "realized_quote": _dstr(self.realized_quote),
            "last_marks": dict(self.last_marks),
            "legs": {k: v.to_dict() for k, v in self.legs.items()},
            "leverage": {k: str(v) for k, v in self.leverage.items()},
            "specs": {k: v.to_dict() for k, v in self.specs.items()},
            "liquidation_loss": _dstr(self.liquidation_loss),
            "funding_events": self.funding_events,
            "funding_by_instrument": {k: _dstr(v) for k, v in self.funding_by_instrument.items()},
            "funding_details": list(self.funding_details),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PerpBook:
        book = cls(
            margin_mode=d.get("margin_mode") or "isolated",
            position_mode=d.get("position_mode") or "one-way",
            contract_type=d.get("contract_type") or "linear",
            quote_ccy=d.get("quote_ccy") or "USDT",
            settlement_ccy=d.get("settlement_ccy") or "USDT",
            expiry_ts=d.get("expiry_ts"),
            exchange=d.get("exchange") or "binance",
            expired=bool(d.get("expired")),
        )
        book.legs = {k: PerpLeg.from_dict(v) for k, v in (d.get("legs") or {}).items()}
        book.leverage = {k: dec(v) for k, v in (d.get("leverage") or {}).items()}
        for k, v in (d.get("specs") or {}).items():
            fallback = spec_for(book.exchange, k, d.get("expiry_ts"))
            try:
                tiers = tuple((float(x["upper"]), float(x["mmr"])) for x in v.get("mmr_tiers") or [])
                book.specs[k.upper()] = ContractSpec(
                    exchange=v.get("exchange") or fallback.exchange,
                    symbol=v.get("symbol") or fallback.symbol,
                    contract_type=v.get("contract_type") or fallback.contract_type,
                    settlement=v.get("settlement") or fallback.settlement,
                    max_leverage=float(v.get("max_leverage") or fallback.max_leverage),
                    mmr_tiers=tiers or fallback.mmr_tiers,
                    liquidation_fee_bps=float(v.get("liquidation_fee_bps") or fallback.liquidation_fee_bps),
                    liquidation_slippage_bps=float(
                        v.get("liquidation_slippage_bps") or fallback.liquidation_slippage_bps
                    ),
                    funding_interval_hours=int(v.get("funding_interval_hours") or fallback.funding_interval_hours),
                    source=v.get("source") or fallback.source,
                    updated_at=v.get("updated_at") or fallback.updated_at,
                    margin_mode_scope=v.get("margin_mode_scope") or fallback.margin_mode_scope,
                    position_mode_scope=v.get("position_mode_scope") or fallback.position_mode_scope,
                    contract_multiplier=float(v.get("contract_multiplier") or fallback.contract_multiplier),
                    expiry_ts=v.get("expiry_ts"),
                    base_asset=v.get("base_asset") or fallback.base_asset,
                    quote_asset=v.get("quote_asset") or fallback.quote_asset,
                )
            except (KeyError, TypeError, ValueError):
                book.specs[k.upper()] = fallback
        book.liquidation_loss = dec(d.get("liquidation_loss") or 0)
        book.realized_quote = dec(d.get("realized_quote") or 0)
        book.last_marks = {str(k): str(v) for k, v in (d.get("last_marks") or {}).items()}
        book.funding_events = int(d.get("funding_events") or 0)
        book.funding_by_instrument = {k: dec(v) for k, v in (d.get("funding_by_instrument") or {}).items()}
        book.funding_details = [dict(x) for x in (d.get("funding_details") or [])]
        return book


__all__ = ["LiquidationResult", "PerpBook", "PerpFill", "PerpLeg"]
