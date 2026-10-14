"""复式记账账本（Decimal）。

为什么要有它：v0.3 的账户用 float 直接加减现金和持仓，`899.9 + 110 - 0.11`
会得到 `1009.7900000000001`，长时间运行后误差累积，对账时无法逐笔核对。

这里的规则：
- 所有金额、数量、价格、费用一律 `Decimal`；外部传入的 float 先经 `str()` 再转，
  避免把二进制误差带进账本；
- 每笔业务生成一条**借贷平衡**的分录：同一币种内所有过账之和必须为 0，
  否则拒绝入账（`LedgerError`）。`trial_balance()` 随时可以验证；
- 账户：
    assets:<CCY>      持有的资产（USDT 现金、BTC 持仓都在这里，按币种分开）
    trading:<CCY>     兑换中转（买 BTC 时 BTC 从这里转出、USDT 转入），
                      让跨币种的一笔成交在每个币种内各自平衡
    expenses:fees     手续费（按实际扣费币种记）
    equity:capital    出入金（初始资金、追加、提取）
- 持仓成本按**移动平均**法：买入成本含以计价币种折算的手续费，卖出按比例结转；
  已实现盈亏 = 卖出所得 − 卖出手续费 − 结转成本。
- 资金不足、卖出超过持仓都直接拒绝（`InsufficientFunds`），是否“缩量成交”
  由上层撮合按策略配置决定，账本自身永远不透支。
- 手续费可以用**第三币种**支付（例如币安用 BNB 抵扣手续费）。这种费用在记账上仍按
  实际扣费币种过账（借贷必须平衡），但折算成计价币需要一笔**外部汇率**：
  调用方必须显式传入 `fee_rate`（1 单位手续费币种 = 多少计价币）并说明来源
  （`fee_rate_source`）。账本**不猜汇率、不按 1:1、不联网取价**——拿不到汇率时
  该笔费用记为“未折算”（`unconverted_fees`），既不进 `fees_value`，也不进持仓成本，
  更不会被悄悄丢掉：报告必须把它单列出来。

黄金样例（tests/test_ledger.py）：
    入金 1000 → 买 1 @100 费 0.1 → 现金 899.9；按 110 估值 1009.9；
    卖 1 @110 费 0.11 → 现金 1009.79，已实现 9.79。

多币种手续费黄金样例（tests/test_fee_conversion.py）：
    买 1 BTC @60000，手续费 0.01 BNB @600 USDT/BNB → 折算手续费 6 USDT。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any

ZERO = Decimal(0)
CAPITAL = "equity:capital"
FEES = "expenses:fees"
FUNDING = "expenses:funding"  # 永续合约资金费（T03）
MARGIN = "margin"  # 永续合约逐仓保证金（从现金划入，平仓 / 强平时划回或没收）
PERP_PNL = "income:perp_pnl"  # 永续合约已实现盈亏（负余额 = 盈利）
LIQUIDATION = "expenses:liquidation"  # 强平时被没收的剩余维持保证金
PERP_SUFFIX = ":PERP"
DEFAULT_MMR = Decimal("0.005")
MARGIN_STEP = Decimal(
    "1e-12"
)  # 保证金与永续盈亏的记账精度：避免 28 位上下文的尾差让分录不平衡  # 维持保证金率（BTC 永续最低档的常见值）


def is_perp(instrument: str) -> bool:
    """`BTC/USDT:PERP` 这样带市场后缀的标的是 U 本位永续合约。"""
    return instrument.upper().endswith(PERP_SUFFIX)


class LedgerError(ValueError):
    """分录不平衡、数量非法等账本层面的错误。"""


class InsufficientFunds(LedgerError):
    """现金或持仓不足。"""


def dec(value: Any) -> Decimal:
    """任意数值 → Decimal。float 走 str，杜绝二进制尾差进入账本。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise LedgerError("布尔值不能作为金额")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise LedgerError("金额不能是 NaN 或无穷大")
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise LedgerError(f"无法解析为数值：{value!r}") from exc


def dstr(value: Decimal | None) -> str | None:
    """Decimal → 去掉无意义尾零的十进制字符串（JSON 里金额一律用字符串）。"""
    if value is None:
        return None
    if value == 0:
        return "0"
    s = format(value.normalize(), "f")
    return s


def quantize(value: Decimal, step: Decimal, *, rounding=ROUND_HALF_UP) -> Decimal:
    if not step or step <= 0:
        return value
    return (value / step).to_integral_value(rounding=rounding) * step


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    return quantize(value, step, rounding=ROUND_DOWN)


def split_instrument(instrument: str, default_quote: str = "USDT") -> tuple[str, str]:
    """`BTC/USDT` → (`BTC`, `USDT`)；`BTC/USDT:PERP` → (`BTC`, `USDT`)。

    必须剥掉 `:PERP` 这类**市场后缀**：永续合约的结算币种是计价币（USDT），
    不是 `USDT:PERP`。不剥离的话账本会凭空多出一个叫 "USDT:PERP" 的币种，
    现金永远为 0，建仓直接报"资金不足"——而且因为币种名看起来很像，排查时极易误判。
    """
    base, _, quote = instrument.partition("/")
    quote = (quote or default_quote).upper()
    quote = quote.partition(":")[0] or default_quote.upper()
    return (base or instrument).upper(), quote


@dataclass(frozen=True)
class Posting:
    account: str
    currency: str
    amount: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {"account": self.account, "currency": self.currency, "amount": dstr(self.amount)}


@dataclass(frozen=True)
class JournalEntry:
    seq: int
    ts: int
    kind: str  # deposit | withdraw | trade
    ref: str  # 关联对象：fill_id / flow_id
    postings: tuple[Posting, ...]
    memo: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "ref": self.ref,
            "memo": self.memo,
            "postings": [p.to_dict() for p in self.postings],
        }


@dataclass
class Holding:
    """单个标的的持仓（账本内部状态，外部只读它的视图）。"""

    instrument: str
    base: str
    quote: str
    qty: Decimal = ZERO
    cost: Decimal = ZERO  # 以计价币种计的持仓成本（含买入手续费）
    realized: Decimal = ZERO
    opened_ts: int = 0  # 本轮持仓（从 0 建仓起）的开始时间
    last_price: Decimal = ZERO
    entry_notional: Decimal = ZERO  # 不含手续费的买入金额（用于“入场均价”，止损止盈以它为基准）

    @property
    def avg_entry(self) -> Decimal:
        return (self.entry_notional / self.qty) if self.qty else ZERO

    @property
    def avg_cost(self) -> Decimal:
        return (self.cost / self.qty) if self.qty else ZERO

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "base": self.base,
            "quote": self.quote,
            "qty": dstr(self.qty),
            "cost": dstr(self.cost),
            "realized": dstr(self.realized),
            "opened_ts": self.opened_ts,
            "last_price": dstr(self.last_price),
            "entry_notional": dstr(self.entry_notional),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Holding:
        return cls(
            instrument=d["instrument"],
            base=d["base"],
            quote=d["quote"],
            qty=dec(d["qty"]),
            cost=dec(d["cost"]),
            realized=dec(d["realized"]),
            opened_ts=int(d.get("opened_ts") or 0),
            last_price=dec(d.get("last_price") or 0),
            entry_notional=dec(d.get("entry_notional") or 0),
        )


@dataclass
class PerpPosition:
    """U 本位永续合约的逐仓、单向持仓。qty 带符号：正 = 多，负 = 空。

    - 开仓 / 加仓：按 名义 / 杠杆 从现金划入保证金；
    - 减仓 / 平仓：按比例划回保证金，盈亏 = 数量 × (成交价 − 开仓均价) × 方向，记 income:perp_pnl；
    - 强平价（逐仓）：保证金 + 浮动盈亏 = 维持保证金率 × 名义 时触发：
        多：P = (E − m/q) / (1 − mmr)；空：P = (E + m/q) / (1 + mmr)
      按 m = qE/L 代入即 多 E(1−1/L)/(1−mmr)、空 E(1+1/L)/(1+mmr)。
    - 强平按**全部保证金损失**处理（剩余维持保证金作为强平费用没收），比交易所的实际结果略保守。
    """

    instrument: str
    quote: str
    qty: Decimal = ZERO
    entry: Decimal = ZERO
    margin: Decimal = ZERO
    leverage: Decimal = Decimal(1)
    mmr: Decimal = DEFAULT_MMR
    realized: Decimal = ZERO
    funding: Decimal = ZERO  # 本轮持仓累计资金费（正 = 收到）
    opened_ts: int = 0
    last_price: Decimal = ZERO
    liquidations: int = 0

    @property
    def side(self) -> str:
        return "long" if self.qty > 0 else "short" if self.qty < 0 else "flat"

    @property
    def notional(self) -> Decimal:
        return abs(self.qty) * self.last_price

    @property
    def unrealized(self) -> Decimal:
        return self.qty * (self.last_price - self.entry) if self.qty else ZERO

    @property
    def liquidation_price(self) -> Decimal:
        if not self.qty:
            return ZERO
        q = abs(self.qty)
        with localcontext() as c:
            c.prec = 40
            if self.qty > 0:
                p = (self.entry - self.margin / q) / (1 - self.mmr)
            else:
                p = (self.entry + self.margin / q) / (1 + self.mmr)
        return max(p, ZERO)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "quote": self.quote,
            "qty": dstr(self.qty),
            "entry": dstr(self.entry),
            "margin": dstr(self.margin),
            "leverage": dstr(self.leverage),
            "mmr": dstr(self.mmr),
            "realized": dstr(self.realized),
            "funding": dstr(self.funding),
            "opened_ts": self.opened_ts,
            "last_price": dstr(self.last_price),
            "liquidations": self.liquidations,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PerpPosition:
        return cls(
            instrument=d["instrument"],
            quote=d.get("quote") or "USDT",
            qty=dec(d.get("qty") or 0),
            entry=dec(d.get("entry") or 0),
            margin=dec(d.get("margin") or 0),
            leverage=dec(d.get("leverage") or 1),
            mmr=dec(d.get("mmr") or DEFAULT_MMR),
            realized=dec(d.get("realized") or 0),
            funding=dec(d.get("funding") or 0),
            opened_ts=int(d.get("opened_ts") or 0),
            last_price=dec(d.get("last_price") or 0),
            liquidations=int(d.get("liquidations") or 0),
        )


@dataclass(frozen=True)
class TradeBooking:
    """一笔成交入账后的结果。"""

    qty_delta: Decimal  # 持仓数量变化（买入为实收数量，扣除以标的币计的手续费后）
    cash_delta: Decimal  # 计价币种现金变化
    fee: Decimal
    fee_currency: str
    fee_value: Decimal  # 手续费折算成计价币种的金额；未折算时为 0
    realized: Decimal  # 本笔实现的盈亏（买入为 0）
    closed: bool  # 本笔之后持仓是否归零
    holding_ms: int  # 平仓时：本轮持仓时长
    fee_rate: Decimal | None = None  # 1 单位手续费币种 = 多少计价币；未折算时为 None
    fee_rate_source: str = ""  # 汇率来源（谁提供的、来自哪条原始数据）
    fee_converted: bool = True  # 该笔手续费是否已折算到计价币


@dataclass
class Ledger:
    base_currency: str = "USDT"
    balances: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    entries: list[JournalEntry] = field(default_factory=list)
    holdings: dict[str, Holding] = field(default_factory=dict)
    fees_by_currency: dict[str, Decimal] = field(default_factory=dict)
    fees_value: Decimal = ZERO  # 全部手续费按成交时汇率折成计价币种的合计（只含已折算部分）
    # 未折算手续费：按币种汇总。拿不到汇率时费用记在这里，**不**混进 fees_value——
    # 混进去就等于按 1:1 估算，会把 BNB 的数量当成 USDT 的金额。
    unconverted_fees: dict[str, Decimal] = field(default_factory=dict)
    # 未折算手续费的明细（币种、数量、发生时间、关联成交），用于报告里逐笔说明为什么没折算
    unconverted_fee_details: list[dict[str, Any]] = field(default_factory=list)
    net_contributions: Decimal = ZERO
    # ---- 永续合约资金费（T03）----
    # 资金费是持仓的"持有成本/收益"，按结算点的标记价与费率逐次结算。
    # 单列而不是并进 fees_value：手续费是交易成本、资金费是持仓成本，
    # 混在一起会让"策略毛利"无法与交易所账单对齐。
    funding_by_instrument: dict[str, Decimal] = field(default_factory=dict)
    funding_paid: Decimal = ZERO  # 累计付出（多头正费率时）
    funding_received: Decimal = ZERO  # 累计收取（空头正费率时）
    funding_events: int = 0
    # ---- 永续合约（逐仓、单向）----
    perps: dict[str, PerpPosition] = field(default_factory=dict)
    leverage: dict[str, Decimal] = field(default_factory=dict)  # 每个合约的杠杆设置（开新仓时使用）
    liquidation_loss: Decimal = ZERO
    seq: int = 0
    keep_entries: int = 50_000  # 内存里保留的分录条数上限（完整分录另行落库）

    # ------------------------------------------------------------------ 基础
    def balance(self, account: str, currency: str) -> Decimal:
        return self.balances.get((account, currency.upper()), ZERO)

    def cash(self, currency: str | None = None) -> Decimal:
        return self.balance("assets", currency or self.base_currency)

    def _post(self, ts: int, kind: str, ref: str, postings: list[Posting], memo: str = "") -> JournalEntry:
        sums: dict[str, Decimal] = {}
        for p in postings:
            sums[p.currency] = sums.get(p.currency, ZERO) + p.amount
        bad = {c: s for c, s in sums.items() if s != 0}
        if bad:
            raise LedgerError(f"分录不平衡：{ {c: dstr(s) for c, s in bad.items()} }")
        for p in postings:
            key = (p.account, p.currency)
            self.balances[key] = self.balances.get(key, ZERO) + p.amount
        self.seq += 1
        entry = JournalEntry(seq=self.seq, ts=ts, kind=kind, ref=ref, postings=tuple(postings), memo=memo)
        self.entries.append(entry)
        if len(self.entries) > self.keep_entries:
            del self.entries[: len(self.entries) - self.keep_entries]
        return entry

    def trial_balance(self) -> dict[str, Decimal]:
        """每个币种所有账户余额之和。复式记账下恒为 0。"""
        out: dict[str, Decimal] = {}
        for (_, ccy), amount in self.balances.items():
            out[ccy] = out.get(ccy, ZERO) + amount
        return out

    # ------------------------------------------------------------------ 资金流
    def deposit(self, amount: Any, *, ts: int = 0, ref: str = "", currency: str | None = None) -> JournalEntry:
        amt = dec(amount)
        if amt <= 0:
            raise LedgerError("入金金额必须为正")
        ccy = (currency or self.base_currency).upper()
        e = self._post(ts, "deposit", ref, [Posting("assets", ccy, amt), Posting(CAPITAL, ccy, -amt)], "入金")
        if ccy == self.base_currency:
            self.net_contributions += amt
        return e

    def withdraw(self, amount: Any, *, ts: int = 0, ref: str = "", currency: str | None = None) -> JournalEntry:
        amt = dec(amount)
        if amt <= 0:
            raise LedgerError("出金金额必须为正")
        ccy = (currency or self.base_currency).upper()
        if self.cash(ccy) < amt:
            raise InsufficientFunds(f"可用 {ccy} {dstr(self.cash(ccy))} 不足以出金 {dstr(amt)}")
        e = self._post(ts, "withdraw", ref, [Posting("assets", ccy, -amt), Posting(CAPITAL, ccy, amt)], "出金")
        if ccy == self.base_currency:
            self.net_contributions -= amt
        return e

    # ------------------------------------------------------------------ 成交
    def trade(
        self,
        *,
        instrument: str,
        side: str,
        qty: Any,
        price: Any,
        fee: Any = 0,
        fee_currency: str | None = None,
        fee_rate: Any = None,
        fee_rate_source: str = "",
        ts: int = 0,
        ref: str = "",
    ) -> TradeBooking:
        """记一笔现货成交。fee_currency 缺省为计价币种；买入时也可以是标的币种（实收数量扣费）。

        `fee_rate`：1 单位手续费币种 = 多少计价币，**只在手续费币种是第三币种时才有意义**，
        且必须由调用方显式给出（来自成交时刻的行情或交易所账单），账本不自己取价。
        `fee_rate_source`：这笔汇率的来源说明，写进成交的 `raw_ref`，让事后能复核
        "6 USDT 是怎么来的"。传了 `fee_rate` 却不给来源时拒绝入账——无法复核的折算
        比不折算更危险，因为它在报告里看起来和真实数据一模一样。

        拿不到汇率（第三币种且未传 `fee_rate`）时**不估算**：该笔费用仍按实际币种过账
        （借贷必须平衡），但记为“未折算”，落在 `unconverted_fees` 里单列。
        """
        q, p, f = dec(qty), dec(price), dec(fee)
        if q <= 0 or p <= 0:
            raise LedgerError("成交数量与价格必须为正")
        if f < 0:
            raise LedgerError("手续费不能为负")
        base, quote = split_instrument(instrument, self.base_currency)
        fccy = (fee_currency or quote).upper()
        # 第三币种手续费（如 BNB 抵扣）需要外部汇率；base/quote 币种能直接用成交价折算，
        # 传了 fee_rate 反而说明调用方口径混乱，直接拒绝而不是二选一。
        third_ccy = fccy not in (base, quote)
        rate: Decimal | None = None
        if third_ccy:
            if fee_rate is None:
                rate = None  # 未折算，走下面的 unconverted 分支
            else:
                rate = dec(fee_rate)
                if rate <= 0:
                    raise LedgerError("手续费汇率必须为正")
                if not fee_rate_source:
                    raise LedgerError(
                        f"以 {fccy} 支付的手续费需要说明汇率来源（fee_rate_source），否则无法复核折算结果"
                    )
        elif fee_rate is not None:
            raise LedgerError(f"手续费币种 {fccy} 属于 {base}/{quote}，不需要额外汇率")
        h = self.holdings.get(instrument) or Holding(instrument=instrument, base=base, quote=quote)
        notional = q * p
        # 折算规则：计价币直接取面值；标的币按成交价（原口径不变）；第三币种按显式汇率。
        if fccy == quote:
            fee_value = f
        elif fccy == base:
            fee_value = f * p
        elif rate is not None:
            fee_value = f * rate
        else:
            fee_value = ZERO  # 未折算：不估算，不计入 fees_value 与持仓成本
        postings: list[Posting] = []
        realized = ZERO
        holding_ms = 0
        if side == "buy":
            need = notional + (f if fccy == quote else ZERO)
            if self.cash(quote) < need:
                raise InsufficientFunds(f"可用 {quote} {dstr(self.cash(quote))} 不足，需要 {dstr(need)}")
            if fccy == base and f >= q:
                raise LedgerError("以标的币扣除的手续费不能大于等于成交数量")
            postings += [
                Posting("assets", base, q),
                Posting("trading", base, -q),
                Posting("trading", quote, notional),
                Posting("assets", quote, -notional),
            ]
            if f:
                postings += [Posting(FEES, fccy, f), Posting("assets", fccy, -f)]
            received = q - (f if fccy == base else ZERO)
            if h.qty == 0:
                h.opened_ts = ts
            h.qty += received
            h.cost += notional + fee_value
            h.entry_notional += notional
            qty_delta, cash_delta = received, -need
        elif side == "sell":
            if h.qty < q:
                raise InsufficientFunds(f"持有 {base} {dstr(h.qty)} 不足以卖出 {dstr(q)}")
            if fccy == base:
                raise LedgerError("卖出的手续费按计价币种扣除")
            postings += [
                Posting("assets", base, -q),
                Posting("trading", base, q),
                Posting("trading", quote, -notional),
                Posting("assets", quote, notional),
            ]
            if f:
                postings += [Posting(FEES, fccy, f), Posting("assets", fccy, -f)]
            with localcontext() as c:
                c.prec = 40
                released = h.cost if q == h.qty else (h.cost * q / h.qty)
                released_entry = h.entry_notional if q == h.qty else (h.entry_notional * q / h.qty)
            # 已实现盈亏只扣已折算的手续费；未折算的部分无法用计价币表达，
            # 硬按 0 扣会高估盈亏，所以它单独出现在 unconverted_fees 里由使用者判断。
            realized = notional - fee_value - released
            h.cost -= released
            h.entry_notional -= released_entry
            h.qty -= q
            h.realized += realized
            qty_delta, cash_delta = -q, notional - (f if fccy == quote else ZERO)
            if h.qty == 0:
                h.cost = ZERO
                h.entry_notional = ZERO
                holding_ms = max(0, ts - h.opened_ts)
        else:
            raise LedgerError(f"未知方向：{side}")
        memo = f"{side} {dstr(q)} {instrument} @ {dstr(p)}"
        if third_ccy and f:
            memo += f"；手续费 {dstr(f)} {fccy}"
            if rate is not None:
                memo += (
                    f"（按 1 {fccy} = {dstr(rate)} {quote} 折算为 {dstr(fee_value)} {quote}，来源：{fee_rate_source}）"
                )
            else:
                memo += "（无汇率，未折算）"
        self._post(ts, "trade", ref, postings, memo)
        h.last_price = p
        self.holdings[instrument] = h
        if f:
            self.fees_by_currency[fccy] = self.fees_by_currency.get(fccy, ZERO) + f
            if third_ccy and rate is None:
                self.unconverted_fees[fccy] = self.unconverted_fees.get(fccy, ZERO) + f
                self.unconverted_fee_details.append(
                    {
                        "currency": fccy,
                        "amount": dstr(f),
                        "ts": ts,
                        "ref": ref,
                        "instrument": instrument,
                        "side": side,
                        "reason": f"缺少 {fccy}/{quote} 成交时刻汇率，未折算（不估算）",
                    }
                )
        self.fees_value += fee_value
        return TradeBooking(
            qty_delta=qty_delta,
            cash_delta=cash_delta,
            fee=f,
            fee_currency=fccy,
            fee_value=fee_value,
            realized=realized,
            closed=(side == "sell" and h.qty == 0),
            holding_ms=holding_ms,
            fee_rate=rate,
            fee_rate_source=fee_rate_source if rate is not None else "",
            fee_converted=not (third_ccy and rate is None),
        )

    # ------------------------------------------------------------------ 永续合约
    def set_leverage(self, instrument: str, leverage: Any) -> Decimal:
        lev = dec(leverage)
        if lev < 1 or lev > 125:
            raise LedgerError("杠杆必须在 1 到 125 倍之间")
        self.leverage[instrument] = lev
        return lev

    def perp(self, instrument: str) -> PerpPosition:
        p = self.perps.get(instrument)
        if p is None:
            _, quote = split_instrument(instrument, self.base_currency)
            p = PerpPosition(instrument=instrument, quote=quote, leverage=self.leverage.get(instrument, Decimal(1)))
            self.perps[instrument] = p
        return p

    def perp_margin_needed(self, instrument: str, qty: Any, price: Any) -> Decimal:
        lev = self.leverage.get(instrument, Decimal(1))
        return abs(dec(qty)) * dec(price) / lev

    def perp_trade(
        self,
        *,
        instrument: str,
        side: str,
        qty: Any,
        price: Any,
        fee: Any = 0,
        ts: int = 0,
        ref: str = "",
        mmr: Any = None,
    ) -> TradeBooking:
        """记一笔永续合约成交（逐仓、单向持仓）。手续费一律按计价币扣。

        方向相反且数量超过现有持仓时**反手**：先全部平掉，再按剩余数量反向开仓。
        开仓部分需要的保证金 + 手续费超过现金时拒绝入账（InsufficientFunds）。
        """
        q, p, f = dec(qty), dec(price), dec(fee)
        if q <= 0 or p <= 0:
            raise LedgerError("成交数量与价格必须为正")
        if f < 0:
            raise LedgerError("手续费不能为负")
        if side not in ("buy", "sell"):
            raise LedgerError(f"未知方向：{side}")
        pos = self.perp(instrument)
        quote = pos.quote
        signed = q if side == "buy" else -q
        closing = ZERO
        if pos.qty and (pos.qty > 0) != (signed > 0):
            closing = min(q, abs(pos.qty))
        opening = q - closing
        lev = self.leverage.get(instrument, pos.leverage if pos.qty else Decimal(1))
        open_margin = quantize(opening * p / lev, MARGIN_STEP) if opening else ZERO
        if open_margin + f > self.cash(quote) + (self._release_preview(pos, closing, p) if closing else ZERO):
            raise InsufficientFunds(
                f"可用 {quote} {dstr(self.cash(quote))} 不足：开仓保证金 {dstr(open_margin)} + 手续费 {dstr(f)}"
            )
        postings: list[Posting] = []
        pnl = ZERO
        holding_ms = 0
        closed = False
        if closing:
            q0 = abs(pos.qty)
            with localcontext() as c:
                c.prec = 40
                released = pos.margin if closing == q0 else quantize(pos.margin * closing / q0, MARGIN_STEP)
                direction = Decimal(1) if pos.qty > 0 else Decimal(-1)
                pnl = quantize(closing * (p - pos.entry) * direction, MARGIN_STEP)
            postings += [Posting(MARGIN, quote, -released), Posting("assets", quote, released)]
            if pnl:
                postings += [Posting("assets", quote, pnl), Posting(PERP_PNL, quote, -pnl)]
            pos.margin -= released
            pos.qty -= closing * direction
            if pos.qty == 0:
                pos.margin = ZERO
                pos.entry = ZERO
                closed = True
                holding_ms = max(0, ts - pos.opened_ts)
        if opening:
            if pos.qty == 0:
                pos.opened_ts = ts
                pos.funding = ZERO
                pos.leverage = lev
                pos.entry = p
            else:
                with localcontext() as c:
                    c.prec = 40
                    pos.entry = (abs(pos.qty) * pos.entry + opening * p) / (abs(pos.qty) + opening)
            if mmr is not None:
                pos.mmr = dec(mmr)
            pos.qty += opening if signed > 0 else -opening
            pos.margin += open_margin
            postings += [Posting(MARGIN, quote, open_margin), Posting("assets", quote, -open_margin)]
        if f:
            postings += [Posting(FEES, quote, f), Posting("assets", quote, -f)]
        memo = f"永续 {side} {dstr(q)} {instrument} @ {dstr(p)}"
        if closing and opening:
            memo += "（反手）"
        self._post(ts, "perp_trade", ref, postings, memo)
        pos.last_price = p
        pos.realized += pnl - f
        if f:
            self.fees_by_currency[quote] = self.fees_by_currency.get(quote, ZERO) + f
        self.fees_value += f
        return TradeBooking(
            qty_delta=signed,
            cash_delta=ZERO,
            fee=f,
            fee_currency=quote,
            fee_value=f,
            realized=pnl - f,
            closed=closed and not opening,
            holding_ms=holding_ms,
        )

    @staticmethod
    def _release_preview(pos: PerpPosition, closing: Decimal, price: Decimal) -> Decimal:
        q0 = abs(pos.qty)
        if not q0:
            return ZERO
        released = pos.margin * closing / q0
        direction = Decimal(1) if pos.qty > 0 else Decimal(-1)
        return released + closing * (price - pos.entry) * direction

    def liquidate(self, instrument: str, *, price: Any, ts: int = 0, ref: str = "") -> TradeBooking:
        """强平：在强平价平掉全部持仓，损失全部逐仓保证金（剩余维持保证金作为强平费用没收）。"""
        pos = self.perps.get(instrument)
        if pos is None or not pos.qty:
            raise LedgerError(f"{instrument} 没有永续持仓，无法强平")
        p = dec(price)
        quote = pos.quote
        m = pos.margin
        direction = Decimal(1) if pos.qty > 0 else Decimal(-1)
        q0 = abs(pos.qty)
        pnl = max(quantize(q0 * (p - pos.entry) * direction, MARGIN_STEP), -m)
        remaining = m + pnl  # ≥ 0：被没收的剩余保证金
        postings = [Posting(MARGIN, quote, -m), Posting("assets", quote, m)]
        if pnl:
            postings += [Posting("assets", quote, pnl), Posting(PERP_PNL, quote, -pnl)]
        if remaining:
            postings += [Posting("assets", quote, -remaining), Posting(LIQUIDATION, quote, remaining)]
        self._post(
            ts,
            "liquidation",
            ref,
            postings,
            f"强平 {instrument} {pos.side} {dstr(q0)} @ {dstr(p)}，损失保证金 {dstr(m)}",
        )
        holding_ms = max(0, ts - pos.opened_ts)
        pos.realized += -m
        pos.qty = ZERO
        pos.margin = ZERO
        pos.entry = ZERO
        pos.last_price = p
        pos.liquidations += 1
        self.liquidation_loss += m
        return TradeBooking(
            qty_delta=-direction * q0,
            cash_delta=ZERO,
            fee=ZERO,
            fee_currency=quote,
            fee_value=ZERO,
            realized=-m,
            closed=True,
            holding_ms=holding_ms,
        )

    def settle_funding(self, instrument: str, *, rate: Any, mark_price: Any, ts: int = 0, ref: str = "") -> Decimal:
        """按当前永续持仓结算一次资金费，返回现金变化（正 = 收到）。空仓记一笔零额分录。"""
        pos = self.perps.get(instrument)
        qty = abs(pos.qty) if pos else ZERO
        side = pos.side if pos and pos.qty else "long"
        before = self.funding_received - self.funding_paid
        self.funding(instrument=instrument, qty=qty, rate=rate, mark_price=mark_price, ts=ts, ref=ref, side=side)
        delta = (self.funding_received - self.funding_paid) - before
        if pos is not None and delta:
            pos.funding += delta
            pos.realized += delta
        return delta

    def perp_value(self) -> Decimal:
        """永续部分的权益贡献 = 保证金 + 浮动盈亏。"""
        return sum((p.margin + p.unrealized for p in self.perps.values() if p.qty), ZERO)

    def gross_exposure(self) -> Decimal:
        """总敞口：现货市值 + 永续名义价值（绝对值）。"""
        spot = sum((h.qty * h.last_price for h in self.holdings.values() if h.qty), ZERO)
        return spot + sum((p.notional for p in self.perps.values() if p.qty), ZERO)

    # ------------------------------------------------------------------ 资金费率
    def funding(
        self,
        *,
        instrument: str,
        qty: Any,
        rate: Any,
        mark_price: Any,
        ts: int = 0,
        ref: str = "",
        side: str = "",
    ) -> JournalEntry:
        """永续合约资金费结算：`expenses:funding`（T03）。

        口径（写进 `assumptions_block`，报告里必须能读到）：
        - 资金费 = 持仓名义价值 × 费率 = `qty × mark_price × rate`；
        - **正费率**：多头付给空头 → 多头记支出；**负费率**：空头付给多头 → 多头记收入；
        - 结算周期 8 小时（交易所常见档位），由调用方按结算点触发，账本不自己计时；
        - 结算用**结算时刻的标记价**，不是开仓价——资金费按名义价值收，名义价值随价格变。

        简化处（写进回测假设）：
        - 资金费从现金结算，不从逐仓保证金里扣，所以不会推动强平价（交易所逐仓模式下会）；
        - 不模拟交易所的费率上下限与结算精度取整；
        - 资金费直接增减计价币现金，不改变持仓成本（资金费不是建仓成本，
          把它算进成本会让"已实现盈亏"偏离交易所口径）。

        `qty` 为持仓数量（正数），`side` 为 `long`/`short`（缺省按 `long` 处理）。
        分录恒平衡：支出记 `expenses:funding` 借、`assets:<quote>` 贷；收入反向。
        """
        q, r, mp = dec(qty), dec(rate), dec(mark_price)
        if q < 0:
            raise LedgerError("资金费结算的持仓数量不能为负（方向用 side 表达）")
        if mp <= 0:
            raise LedgerError("资金费结算的标记价必须为正")
        if q == 0 or r == 0:
            # 空仓或零费率没有资金费，但仍记一笔零额分录，
            # 这样"跨过 3 个结算点就有 3 笔分录"在账本里是可数的，不会静默丢事件。
            return self._post(ts, "funding", ref, [], f"资金费 {instrument}：无持仓或零费率，金额 0")
        _, quote = split_instrument(instrument, self.base_currency)
        direction = (side or "long").lower()
        if direction not in ("long", "short"):
            raise LedgerError(f"未知持仓方向：{side}")
        notional = q * mp
        # 多头视角的现金变动：正费率时多头付出（负号），空头收取（正号）。
        # 负费率自动反向——不写两支 if，符号只在最前面定一次，避免两处逻辑不一致。
        sign = Decimal(-1) if direction == "long" else Decimal(1)
        cash_delta = sign * notional * r
        if cash_delta == 0:
            return self._post(ts, "funding", ref, [], f"资金费 {instrument}：金额 0")
        if cash_delta < 0:
            paid = -cash_delta
            postings = [Posting(FUNDING, quote, paid), Posting("assets", quote, -paid)]
            memo = (
                f"资金费 {instrument} {direction}：名义 {dstr(notional)} × 费率 {dstr(r)} = 支付 {dstr(paid)} {quote}"
            )
        else:
            postings = [Posting(FUNDING, quote, -cash_delta), Posting("assets", quote, cash_delta)]
            memo = (
                f"资金费 {instrument} {direction}：名义 {dstr(notional)} × 费率 {dstr(r)} "
                f"= 收取 {dstr(cash_delta)} {quote}"
            )
        entry = self._post(ts, "funding", ref, postings, memo)
        self.funding_by_instrument[instrument] = self.funding_by_instrument.get(instrument, ZERO) + cash_delta
        if cash_delta < 0:
            self.funding_paid += -cash_delta
        else:
            self.funding_received += cash_delta
        self.funding_events += 1
        return entry

    def funding_report(self) -> dict[str, Any]:
        """资金费汇总（Decimal → 十进制字符串）。"""
        return {
            "events": self.funding_events,
            "paid": dstr(self.funding_paid),
            "received": dstr(self.funding_received),
            "net": dstr(self.funding_received - self.funding_paid),
            "by_instrument": {k: dstr(v) for k, v in self.funding_by_instrument.items()},
        }

    # ------------------------------------------------------------------ 估值
    def mark(self, prices: dict[str, Any]) -> None:
        for inst, price in prices.items():
            if not price:
                continue
            h = self.holdings.get(inst)
            if h is not None:
                h.last_price = dec(price)
            p = self.perps.get(inst)
            if p is not None:
                p.last_price = dec(price)

    def positions_value(self) -> Decimal:
        """持仓价值：现货按市值；永续按 保证金 + 浮动盈亏（这样 权益 = 现金 + 持仓价值 恒成立）。"""
        spot = sum((h.qty * h.last_price for h in self.holdings.values() if h.qty), ZERO)
        return spot + self.perp_value()

    def equity(self) -> Decimal:
        """总权益（计价币种）= 现金 + 持仓按最新标记价估值（永续含保证金与浮动盈亏）。"""
        return self.cash() + self.positions_value()

    def realized_total(self) -> Decimal:
        return sum((h.realized for h in self.holdings.values()), ZERO) + sum(
            (p.realized for p in self.perps.values()), ZERO
        )

    def open_holdings(self) -> list[Holding]:
        return [h for h in self.holdings.values() if h.qty > 0]

    def unconverted_fees_report(self) -> dict[str, Any]:
        """未折算手续费的统一对外形状（Decimal → 十进制字符串）。

        报告里必须能一眼看出"哪些手续费没进总额、为什么"——只给一个总数的话，
        使用者会把 `fees_value` 当成全部成本，从而低估手续费。
        `converted` 是已折算合计，`by_currency` 是未折算的按币种汇总，两者**不能相加**：
        相加就等于把 BNB 的数量当成 USDT 的金额。
        """
        return {
            "has_unconverted": bool(self.unconverted_fees),
            "by_currency": {k: dstr(v) for k, v in sorted(self.unconverted_fees.items())},
            "count": len(self.unconverted_fee_details),
            "details": list(self.unconverted_fee_details),
            "converted": dstr(self.fees_value),
            "note": (
                "以下手续费以非计价币种支付且缺少成交时刻汇率，未折算进总手续费（不估算、不按 1:1）："
                + "、".join(f"{k} {dstr(v)}" for k, v in sorted(self.unconverted_fees.items()))
                if self.unconverted_fees
                else ""
            ),
        }

    # ------------------------------------------------------------------ 序列化（检查点）
    def to_dict(self, *, entries: bool = False) -> dict[str, Any]:
        out = {
            "base_currency": self.base_currency,
            "seq": self.seq,
            "balances": [[a, c, dstr(v)] for (a, c), v in sorted(self.balances.items())],
            "holdings": {k: h.to_dict() for k, h in self.holdings.items()},
            "fees_by_currency": {k: dstr(v) for k, v in self.fees_by_currency.items()},
            "fees_value": dstr(self.fees_value),
            "unconverted_fees": {k: dstr(v) for k, v in self.unconverted_fees.items()},
            "unconverted_fee_details": list(self.unconverted_fee_details),
            "net_contributions": dstr(self.net_contributions),
            "funding_by_instrument": {k: dstr(v) for k, v in self.funding_by_instrument.items()},
            "funding_paid": dstr(self.funding_paid),
            "funding_received": dstr(self.funding_received),
            "funding_events": self.funding_events,
            "perps": {k: p.to_dict() for k, p in self.perps.items()},
            "leverage": {k: dstr(v) for k, v in self.leverage.items()},
            "liquidation_loss": dstr(self.liquidation_loss),
        }
        if entries:
            out["entries"] = [e.to_dict() for e in self.entries]
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Ledger:
        led = cls(base_currency=d.get("base_currency", "USDT"))
        led.seq = int(d.get("seq") or 0)
        led.balances = {(a, c): dec(v) for a, c, v in d.get("balances", [])}
        led.holdings = {k: Holding.from_dict(v) for k, v in (d.get("holdings") or {}).items()}
        led.fees_by_currency = {k: dec(v) for k, v in (d.get("fees_by_currency") or {}).items()}
        led.fees_value = dec(d.get("fees_value") or 0)
        led.unconverted_fees = {k: dec(v) for k, v in (d.get("unconverted_fees") or {}).items()}
        led.unconverted_fee_details = [dict(x) for x in (d.get("unconverted_fee_details") or [])]
        led.net_contributions = dec(d.get("net_contributions") or 0)
        led.funding_by_instrument = {k: dec(v) for k, v in (d.get("funding_by_instrument") or {}).items()}
        led.funding_paid = dec(d.get("funding_paid") or 0)
        led.funding_received = dec(d.get("funding_received") or 0)
        led.funding_events = int(d.get("funding_events") or 0)
        led.perps = {k: PerpPosition.from_dict(v) for k, v in (d.get("perps") or {}).items()}
        led.leverage = {k: dec(v) for k, v in (d.get("leverage") or {}).items()}
        led.liquidation_loss = dec(d.get("liquidation_loss") or 0)
        return led
