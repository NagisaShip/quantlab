"""回测 / 模拟运行时：把 SDK 的下单意图接到成本模型、风控层、撮合规则与 Decimal 账本上。

刻意不 import `engine` 包——SDK 要保持可独立安装、可单独测试。

统一策略协议（回测、模拟、只读实盘对照共用）
-------------------------------------------------
    行情事件 (Bar, event_id = "<标的>:<收盘时间>")
        → 策略回调 (handle_data / run_daily … / on_trade)
        → Signal(signal_id, strategy_version_id, instrument_id, event_id, decision_ts, reason, expires_at)
          + TargetPosition(数量 / 权重 / 金额, 单位, 有效期)
        → 风控层 RiskConfig（权重上限、总敞口、日内交易次数、价格偏离、回撤暂停）
        → 撮合（下一根开盘 / 限价保守模型 / 止损止盈）
        → Fill(fill_id 由 signal_id 派生) → Decimal 复式账本
`signal_id` 由 (run_id, 策略版本, 标的, 事件, 事件内序号) 确定性生成：同一事件重放会得到
同一个 id，已处理过的事件和信号不会再次成交——这是检查点恢复“不重复买卖”的依据。

撮合口径
--------
- 决策只能用**已收盘**的 bar；默认在**下一根 bar 开盘价** ±（半个价差 + 滑点）成交；
  `fill_at="close"` 时为当根收盘价（旧口径，仅用于对照）。
- 信号有效期默认 2 根 bar；下一根 bar 迟迟不来（停机、数据缺口）时委托过期，不补成交。
- 资金不足：按**数量 / 金额**下的单默认**拒绝**（`insufficient_funds="reject"`），可显式改为
  `"reduce"` 按可负担数量成交；按**权重**调仓（order_target_percent）本身就是“按权益比例折算”，
  会把手续费和滑点算进去、按可负担数量成交，不视为部分成交。
- 卖出超过持仓同样默认拒绝（现货不做空）。
- 永续合约（`BTC/USDT:PERP`）：可以做空，逐仓单向持仓，杠杆用 `set_leverage` 设置（默认 1 倍）。
  开仓占用 名义/杠杆 的保证金；每根 bar 用最高/最低价检查强平，触及强平价按**全部保证金损失**处理；
  资金费在结算时刻按持仓名义 × 费率结算（多头正费率付费），费率表由上层按时间点准备好。
- 限价单（保守 K 线模型）：买单只有 `low < 限价` 才成交（开盘已低于限价则按开盘价）；
  `low == 限价` 视为“触价歧义”不成交并计数。卖单对称。被动成交按 maker 费率、无滑点。
- 止损 / 止盈：同一根 bar 同时触及时**默认止损先触发**（保守），并计入歧义次数；
  跳空越过止损价时按开盘价成交。
- 手续费币种：`fee_currency="quote"`（默认，按计价币扣）或 `"received"`
  （买入按收到的标的币扣，卖出按收到的计价币扣，与多数交易所现货一致）。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..exchanges.base import Bar
from ..exchanges.contracts import contract_type_of, is_derivative, spec_for
from . import guards as guards_mod
from . import trade_mgmt
from .ledger import ZERO, InsufficientFunds, Ledger, LedgerError, dec, dstr, floor_to, is_perp, quantize
from .objects import Context, Portfolio
from .runtime import OrderRequest, OrderResult, ScheduleEntry

PRICE_STEP = Decimal("1e-10")
FEE_STEP = Decimal("1e-12")


@dataclass
class RuntimeCost:
    """运行时成本与撮合参数。默认值与平台基准成本模型一致。"""

    taker_fee: float = 0.001
    maker_fee: float = 0.0005
    slippage_bps: float = 5.0
    spread_bps: float = 2.0
    max_volume_participation: float = 0.1
    min_notional: float = 10.0
    # 调仓容差：目标仓位法每次被调用都会重新对齐目标，容差内的差额不成交（与实验三一致）。
    rebalance_tolerance: float = 0.005
    # next_open = 收盘决策、下一根开盘成交（默认）；close = 当根收盘成交（旧口径，仅对照）
    fill_at: str = "next_open"
    # 资金/持仓不足：reject（默认，拒绝并记录原因）| reduce（显式选择按可负担数量成交）
    insufficient_funds: str = "reject"
    # 手续费币种：quote（计价币）| received（收到的币种）
    fee_currency: str = "quote"
    # 数量精度（下单数量向下取整到该步长）；价格精度为 0 时不取整
    qty_step: float = 1e-8
    price_tick: float = 0.0
    # 信号有效期（以 bar 数计）。超过有效期仍未成交的委托作废，不补成交。
    signal_ttl_bars: int = 2
    # 撮合粒度：bar = 下一根 bar 开盘；tick = 用 OHLC 路径模拟盘中逐笔成交（若周期本身是 50ms/1s，
    # 就等价于逐笔 / 子秒级撮合）。真实逐笔数据可直接把回测周期设为 50ms~1s。
    execution_mode: str = "bar"
    # 动态滑点：基础滑点 + 振幅系数 × 本根振幅；冲击成本：a + b·√(下单量/成交量) bp
    dynamic_slippage: bool = False
    vol_slippage: float = 0.25
    impact_a_bps: float = 0.0
    impact_b_bps: float = 0.0
    # 涨跌停 / 流动性枯竭：0 = 关闭（加密现货默认无涨跌停）
    limit_move_pct: float = 0.0
    min_liquidity_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_at": self.fill_at,
            "taker_fee": self.taker_fee,
            "maker_fee": self.maker_fee,
            "slippage_bps": self.slippage_bps,
            "spread_bps": self.spread_bps,
            "max_volume_participation": self.max_volume_participation,
            "min_notional": self.min_notional,
            "rebalance_tolerance": self.rebalance_tolerance,
            "insufficient_funds": self.insufficient_funds,
            "fee_currency": self.fee_currency,
            "qty_step": self.qty_step,
            "price_tick": self.price_tick,
            "signal_ttl_bars": self.signal_ttl_bars,
            "execution_mode": self.execution_mode,
            "dynamic_slippage": self.dynamic_slippage,
            "vol_slippage": self.vol_slippage,
            "impact_a_bps": self.impact_a_bps,
            "impact_b_bps": self.impact_b_bps,
            "limit_move_pct": self.limit_move_pct,
            "min_liquidity_usd": self.min_liquidity_usd,
        }


@dataclass
class RiskConfig:
    """风控层。0 表示该项不限制。所有拦截都会记录原因码，出现在 rejected 与信号状态里。"""

    max_position_weight: float = 1.0  # 单标的市值 / 权益 上限
    max_gross_exposure: float = 1.0  # 全部持仓市值 / 权益 上限（现货不加杠杆）
    max_trades_per_day: int = 0  # 每个 UTC 日最多成交笔数
    max_price_deviation: float = 0.2  # 成交价相对决策价偏离上限（防止跳空后追单）
    drawdown_pause: float = 0.0  # 回撤超过该比例后暂停新开仓（仍允许减仓）
    stop_loss: float = 0.0  # 默认止损（相对持仓均价）
    take_profit: float = 0.0  # 默认止盈

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> RiskConfig:
        d = d or {}
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and d[k] is not None}
        return cls(**known)


@dataclass
class TargetPosition:
    """目标仓位：数量（base 单位）、权重（占权益比例）或金额（计价币）。"""

    instrument_id: str
    kind: str  # quantity | weight | value | delta_quantity | delta_value
    value: str  # Decimal 字符串
    unit: str  # base | fraction | quote
    valid_until: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    signal_id: str
    run_id: str
    strategy_version_id: str
    instrument_id: str
    event_id: str
    decision_ts: int
    reason: str
    expires_at: int
    target: TargetPosition
    order_type: str = "market"  # market | limit
    limit_price: str | None = None
    status: str = "accepted"  # accepted | filled | partial | rejected | expired | tolerance | canceled
    status_reason: str = ""
    reason_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["target"] = self.target.to_dict()
        return d


@dataclass
class Fill:
    """一笔成交。金额字段对外一律十进制字符串。"""

    ts: int
    security: str
    side: str
    amount: Decimal
    price: Decimal
    fee: Decimal
    reason: str = ""
    partial: bool = False
    note: str = ""
    realized_pnl: Decimal = ZERO
    fee_currency: str = "USDT"
    fee_value: Decimal = ZERO
    liquidity: str = "taker"
    fill_id: str = ""
    signal_id: str = ""
    source: str = "simulated"
    # 仿真真实度用：成交时的市场参考价（未加滑点）、策略想要的数量、决策时刻
    market_price: Decimal | None = None
    requested: Decimal | None = None
    decided_at: int = 0

    @property
    def notional(self) -> Decimal:
        return self.amount * self.price

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "signal_id": self.signal_id,
            "ts": self.ts,
            "security": self.security,
            "side": self.side,
            "amount": dstr(self.amount),
            "price": dstr(self.price),
            "fee": dstr(self.fee),
            "fee_currency": self.fee_currency,
            "fee_value": dstr(self.fee_value),
            "notional": dstr(self.notional),
            "liquidity": self.liquidity,
            "reason": self.reason,
            "partial": self.partial,
            "note": self.note,
            "realized_pnl": dstr(self.realized_pnl),
            "source": self.source,
            **({"market_price": dstr(self.market_price)} if self.market_price is not None else {}),
            **({"requested": dstr(self.requested)} if self.requested is not None else {}),
            **({"decided_at": self.decided_at} if self.decided_at else {}),
        }


@dataclass
class RoundTrip:
    """一次完整交易：从空仓建仓到卖回空仓。未平仓的持仓**不是**完成交易。"""

    security: str
    open_ts: int
    close_ts: int = 0
    cost_in: Decimal = ZERO
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    fills: int = 0
    entry_tag: str = ""  # 开仓那笔委托的 reason
    exit_reason: str = ""  # 平仓那笔委托的 reason（stop_loss / trailing_stop / roi / custom_exit / 策略自定义……）
    entries: int = 0  # 开仓 + 加仓成交次数

    @property
    def ret(self) -> float:
        return float(self.realized / self.cost_in) if self.cost_in else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "security": self.security,
            "open_ts": self.open_ts,
            "close_ts": self.close_ts,
            "holding_ms": max(0, self.close_ts - self.open_ts) if self.close_ts else None,
            "cost_in": dstr(self.cost_in),
            "realized": dstr(self.realized),
            "fees": dstr(self.fees),
            "return": round(self.ret, 8),
            "fills": self.fills,
            "entry_tag": self.entry_tag,
            "exit_reason": self.exit_reason,
            "entries": self.entries,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RoundTrip:
        return cls(
            security=d["security"],
            open_ts=d["open_ts"],
            close_ts=d.get("close_ts") or 0,
            cost_in=dec(d["cost_in"]),
            realized=dec(d["realized"]),
            fees=dec(d["fees"]),
            fills=d.get("fills", 0),
            entry_tag=d.get("entry_tag") or "",
            exit_reason=d.get("exit_reason") or "",
            entries=int(d.get("entries") or 0),
        )


EXIT_LABELS = {
    "stop_loss": "止损",
    "take_profit": "止盈",
    "trailing_stop": "移动止损",
    "custom_stop": "动态止损",
    "roi": "分段止盈",
    "exchange_stop": "交易所止损单",
}


def _day(ts: int) -> str:
    return datetime.fromtimestamp(max(ts - 1, 0) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class BacktestRuntime:
    """回测 / 模拟的 Runtime 实现（模拟与回测只差时钟）。"""

    mode = "backtest"

    def __init__(
        self,
        bars_by_security: dict[str, list[Bar]],
        *,
        initial_cash: float | str | Decimal = 10_000.0,
        initial_currency: str = "USDT",
        cost: RuntimeCost | None = None,
        ctx: Context | None = None,
        risk: RiskConfig | None = None,
        run_id: str = "local",
        strategy_version_id: str = "",
        quote_currency: str = "USDT",
        research: dict[str, Any] | None = None,
        cost_scale: float = 1.0,
        exchange: str = "binance",
        margin_mode: str = "isolated",
        position_mode: str = "one-way",
        contract_type: str = "linear",
        expiry_ts: int | None = None,
        ledger_v2: bool = False,
    ) -> None:
        self.bars_by_security = bars_by_security
        # 成本压力倍数：在成交时统一乘到手续费（吃单 / 挂单）、价差、滑点与冲击成本上。
        # 不在入参里预先放大，是因为策略可以在 initialize 里 set_order_cost 覆盖成本——那样压力测试会失效
        self.cost_scale = max(0.0, float(cost_scale or 1.0))
        # 回测前按时间点算好的研究数据：factor() / predict() 的取值表、optimize() 的方案配置
        self.research = research or {}
        self._research_missing: set[str] = set()
        self.cost = cost or RuntimeCost()
        self.risk = risk or RiskConfig()
        self.run_id = run_id
        self.strategy_version_id = strategy_version_id
        self.exchange = (exchange or "binance").lower()
        self.margin_mode = margin_mode
        self.position_mode = position_mode
        self.contract_type = contract_type
        self.expiry_ts = expiry_ts
        self.ledger_v2 = bool(ledger_v2)
        self.ledger = Ledger(base_currency=quote_currency, exchange=self.exchange)
        self.initial_cash = float(dec(initial_cash))
        self.initial_currency = (initial_currency or self.ledger.base_currency).upper()
        if dec(initial_cash) > 0:
            self.ledger.deposit(initial_cash, ref="initial", currency=self.initial_currency)
        self._setup_perp_book(bars_by_security)
        self.ctx = ctx or Context(run_id=run_id, mode="backtest")
        # v2.0：策略必须能从 Context 读到自己跑的是哪个版本
        self.ctx.strategy_version_id = strategy_version_id
        self.ctx._attach_portfolio(Portfolio(self.ledger))
        self._index: dict[str, int] = {s: -1 for s in bars_by_security}
        self._schedules: list[ScheduleEntry] = []
        self.fills: list[Fill] = []
        self.logs: list[dict[str, Any]] = []
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.benchmark: str | None = None
        self.slippage_override: float | None = None
        self.impact_model: dict[str, Any] | None = None  # set_execution()：随下单规模变化的滑点
        # 成本优先级：平台 UI 默认成本先注入；策略显式 set_order_cost / set_slippage 后覆盖，
        # cost_source 会写进回测报告，避免研究者误以为代码里的声明没有生效。
        self.cost_source: str = "platform"
        self.cost_overrides: dict[str, float] = {}
        # 指标缓存：key -> 整条序列（只在首次调用时算一次；每根 bar 只按当前索引取值）
        self._indicator_cache: dict[tuple[str, str, int, str], Any] = {}
        self.rejected: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []  # 市价委托：等下一根 bar 开盘
        self.open_orders: list[dict[str, Any]] = []  # 限价委托：直到成交 / 过期 / 撤销
        self.brackets: dict[str, dict[str, float]] = {}
        self.signals: list[Signal] = []
        self.signal_ids: set[str] = set()
        self.round_trips: list[RoundTrip] = []
        self._open_trips: dict[str, RoundTrip] = {}
        self.order_seq = 0
        self._event_seq: dict[str, int] = {}
        self.last_event_ts = 0
        self.trades_per_day: dict[str, int] = {}
        self.peak_nav = Decimal(1)
        self.units = self.ledger.equity()  # 份额：单位净值从 1 开始
        self.counters = {
            "limit_touch_ambiguous": 0,
            "bracket_same_bar": 0,
            "expired_signals": 0,
            "duplicate_events": 0,
            "insufficient_funds": 0,
            "risk_rejections": 0,
            "limit_locked": 0,
            "liquidity_drought": 0,
            "tick_path_fills": 0,
        }
        self.flows_applied: list[dict[str, Any]] = []
        self._new_fills_for_callback: list[Fill] = []
        # ---- 持仓管理（sdk/trade_mgmt.py）----
        self.trade_state: dict[str, dict[str, Any]] = {}  # 每个持仓：开仓时间、标签、最高/最低价、当前止损
        self.roi: dict[str, list[tuple[int, float]]] = {}  # 分段止盈表，"*" = 所有标的
        self._rs: dict[Any, dict[str, Any]] = {}  # 多周期合成缓存
        self.protects: dict[str, dict[str, Any]] = {}  # 交易所模拟盘上挂着的保护止损
        self.protect_seq = 0
        self._booked_protects: set[str] = set()
        self.confirm_hook: Any = None  # 策略的 confirm_entry（由 runner 挂上）
        self.guards = guards_mod.Guards(self)  # 组合保护、动态标的池、下架名单（sdk/guards.py）
        # ---- 成交来源 ----
        # internal：平台撮合（回测、纸面模拟）。
        # external：交易所模拟盘。风控、资金检查、数量计算照旧在这里做，但不在本地成交——
        # 算好的委托放进 outbox，由持有密钥的上层进程（OMS）送到交易所，成交回报再经
        # apply_external() 记进同一本账。策略代码和隔离进程都碰不到密钥。
        self.fill_mode = "internal"
        self.outbox: list[dict[str, Any]] = []
        self.awaiting: dict[str, dict[str, Any]] = {}  # client_order_id → 已送出、未回报的委托
        self._ext_cash = ZERO  # 在途委托对可用资金的预估影响
        self._ext_qty: dict[str, Decimal] = {}  # 在途委托对持仓的预估影响
        # ---- 永续合约资金费 ----
        # funding_rates[合约] = [(结算时刻, 费率)]（升序）；funding_ts[合约] = 已结算到的时刻
        self.funding_rates: dict[str, list[tuple[int, Decimal]]] = {}
        self.funding_marks: dict[str, dict[int, Decimal]] = {}
        self.funding_ts: dict[str, int] = {}
        self._funding_warned: set[str] = set()
        for key, tbl in list(self.research.items()):
            if key.startswith("funding:") and isinstance(tbl, dict):
                self.add_funding(
                    key.split(":", 1)[1],
                    list(
                        zip(
                            tbl.get("times") or [],
                            tbl.get("rates") or [],
                            tbl.get("mark_prices") or [""] * len(tbl.get("times") or []),
                            strict=False,
                        )
                    ),
                )

    def _setup_perp_book(self, bars_by_security: dict[str, list[Bar]]) -> None:
        """Enable the v2 ledger for advanced derivative modes.

        Linear isolated one-way retains the legacy net-position path for
        backward compatibility.  Cross, hedge, inverse and delivery always use
        the v2 book so a mode switch can never silently use the wrong ledger.
        """
        symbols = [s for s in bars_by_security if is_derivative(s)]
        if not symbols:
            return
        advanced = (
            self.ledger_v2
            or self.margin_mode == "cross"
            or self.position_mode == "hedge"
            or self.contract_type in ("inverse", "delivery")
            or any(contract_type_of(s) in ("inverse", "delivery") for s in symbols)
        )
        if not advanced:
            return
        specs = {}
        for symbol in symbols:
            kind = contract_type_of(symbol)
            specs[symbol] = spec_for(self.exchange, symbol, self.expiry_ts if kind == "delivery" else None)
        first = specs[symbols[0]]
        self.contract_type = first.contract_type
        self.ledger.enable_perp_book(
            margin_mode=self.margin_mode,
            position_mode=self.position_mode,
            contract_type=first.contract_type,
            quote_ccy=self.ledger.base_currency,
            settlement_ccy=first.settlement,
            expiry_ts=self.expiry_ts,
            exchange=self.exchange,
            specs=specs,
        )
        for symbol in symbols:
            self.ledger.perp_book.leverage[symbol.upper()] = self.ledger.leverage.get(symbol.upper(), Decimal(1))
        initial_marks = {
            symbol: bars_by_security[symbol][0].close for symbol in symbols if bars_by_security.get(symbol)
        }
        if initial_marks:
            self.ledger.perp_book.mark(initial_marks)

    def set_margin_mode(self, mode: str) -> None:
        mode = str(mode).lower()
        if mode not in ("isolated", "cross"):
            raise ValueError("margin_mode 只能是 isolated 或 cross")
        if self.ledger.perp_book is None:
            symbols = [s for s in self.bars_by_security if is_derivative(s)]
            if not symbols:
                raise ValueError("当前运行没有衍生品标的")
            self.margin_mode = mode
            self._setup_perp_book(self.bars_by_security)
            return
        if self.ledger.perp_book.all_legs():
            raise ValueError("已有持仓时不能切换保证金模式")
        self.margin_mode = mode
        self.ledger.perp_book.margin_mode = mode

    def set_position_mode(self, mode: str) -> None:
        mode = str(mode).lower()
        if mode not in ("one-way", "hedge"):
            raise ValueError("position_mode 只能是 one-way 或 hedge")
        if self.ledger.perp_book is None:
            symbols = [s for s in self.bars_by_security if is_derivative(s)]
            if not symbols:
                raise ValueError("当前运行没有衍生品标的")
            self.position_mode = mode
            self._setup_perp_book(self.bars_by_security)
            return
        if self.ledger.perp_book.all_legs():
            raise ValueError("已有持仓时不能切换持仓模式")
        self.position_mode = mode
        self.ledger.perp_book.position_mode = mode

    # ------------------------------------------------------------------ 时间推进
    def advance(self, ts: int) -> None:
        """把所有标的推进到时间戳 ts（含）为止的最后一根已收盘 bar。"""
        for security, bars in self.bars_by_security.items():
            idx = self._index[security]
            while idx + 1 < len(bars) and bars[idx + 1].close_time <= ts:
                idx += 1
            self._index[security] = idx

    def accept_event(self, ts: int) -> bool:
        """事件去重：时间不前进的事件（重复推送、乱序重放）直接丢弃，不会重复成交。"""
        if ts <= self.last_event_ts:
            self.counters["duplicate_events"] += 1
            return False
        self.last_event_ts = ts
        return True

    @property
    def now(self) -> int:
        return self.ctx.current_dt

    def bar_ms(self, security: str) -> int:
        bars = self.bars_by_security.get(security) or []
        if len(bars) >= 2:
            return max(1, bars[1].close_time - bars[0].close_time)
        if bars:
            return max(1, bars[0].close_time - bars[0].open_time)
        return 3_600_000

    # ------------------------------------------------------------------ 数据
    def price(self, security: str) -> float:
        idx = self._index.get(security, -1)
        if idx < 0:
            return 0.0
        return self.bars_by_security[security][idx].close

    def bars(self, security: str, count: int, unit: str | None = None) -> list[dict[str, Any]]:
        """截至当前时刻的最近 count 根已收盘 bar。绝不返回未来数据。

        unit（如 "4h"）：把本周期 K 线合成更大周期，只给已完整收盘的那些（见 trade_mgmt.resample）。
        """
        if unit:
            return trade_mgmt.resample(self, security, count, unit)
        idx = self._index.get(security, -1)
        if idx < 0:
            return []
        rows = self.bars_by_security[security][max(0, idx - count + 1) : idx + 1]
        return [
            {
                "t": b.open_time,
                "close_time": b.close_time,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in rows
        ]

    def current_bar(self, security: str) -> Bar | None:
        idx = self._index.get(security, -1)
        if idx < 0:
            return None
        return self.bars_by_security[security][idx]

    def securities(self) -> list[str]:
        return list(self.bars_by_security)

    def is_tradable(self, security: str) -> bool:
        bar = self.current_bar(security)
        return bool(bar and bar.volume > 0)

    # ------------------------------------------------------------------ 配置
    def set_benchmark(self, security: str) -> None:
        self.benchmark = security
        self.ctx.benchmark = security

    def set_order_cost(self, cost: dict[str, float]) -> None:
        """策略代码显式声明成本：覆盖平台 / UI 默认值，并记录覆盖项供报告标注。"""
        changed = False
        for key in ("taker_fee", "maker_fee", "slippage_bps", "spread_bps", "max_volume_participation", "min_notional"):
            if key in cost:
                value = float(cost[key])
                setattr(self.cost, key, value)
                self.cost_overrides[key] = value
                changed = True
        if changed:
            self.cost_source = "strategy"

    def set_slippage(self, bps: float) -> None:
        """set_slippage 也是代码显式覆盖，单独标注并同步到报告。"""
        self.slippage_override = float(bps)
        self.cost_overrides["slippage_bps"] = float(bps)
        self.cost_source = "strategy"

    # ------------------------------------------------------------------ 预计算指标
    def indicator(self, security: str, name: str, period: int, *, field: str = "close") -> float:
        """返回当前 bar 上的平台指标值（因果计算，预热期 NaN）。

        整条序列只算一次并缓存；策略在每根 bar 调用 ``ctx.indicators.ema(...)`` 时
        不会重复切片、重复递推。521k 根 1m 线也不会退化成 O(n²)。
        """
        if security not in self.bars_by_security:
            raise KeyError(f"未知标的：{security}")
        period = int(period)
        if period <= 0:
            raise ValueError("period 必须大于 0")
        name = name.lower()
        key = (security, name, period, field)
        arr = self._indicator_cache.get(key)
        if arr is None:
            import numpy as np

            from . import indicators as I

            bars = self.bars_by_security[security]
            if name in ("ema", "sma"):
                vals = [float(getattr(b, field)) for b in bars]
                values = I.ema(vals, period) if name == "ema" else I.sma(vals, period)
            elif name == "atr":
                values = I.atr(
                    [float(b.high) for b in bars],
                    [float(b.low) for b in bars],
                    [float(b.close) for b in bars],
                    period,
                )
            elif name == "rsi":
                values = I.rsi([float(b.close) for b in bars], period)
            else:
                raise ValueError(f"不支持的指标：{name}（可用 ema / sma / atr / rsi）")
            arr = np.asarray(values, dtype=float)
            self._indicator_cache[key] = arr
        idx = self._index.get(security, -1)
        if idx < 0:
            return float("nan")
        return float(arr[idx])

    def set_impact_model(self, tbl: dict[str, Any]) -> None:
        m = tbl.get("cost_model") or {}
        self.impact_model = {"a": float(m.get("a_bps") or 0.0), "b": float(m.get("b_bps") or 0.0)}
        self.log(
            "info",
            f"执行方案「{tbl.get('name')}」：滑点 ≈ {self.impact_model['a']:.2f} + "
            f"{self.impact_model['b']:.2f}·√(下单量/当根成交量) bp",
        )

    def set_option(self, name: str, value: Any) -> None:
        if name == "insufficient_funds":
            if value not in ("reject", "reduce"):
                raise ValueError("insufficient_funds 只能是 reject 或 reduce")
            self.cost.insufficient_funds = value
        elif name == "fee_currency":
            if value not in ("quote", "received"):
                raise ValueError("fee_currency 只能是 quote 或 received")
            self.cost.fee_currency = value
        elif name == "qty_step":
            if float(value) <= 0:
                raise ValueError("qty_step 必须为正")
            self.cost.qty_step = float(value)
        elif name in RiskConfig.__dataclass_fields__:
            setattr(self.risk, name, type(getattr(self.risk, name))(value))
        self.ctx.options[name] = value

    def set_trailing(self, security: str, pct: float, activate: float = 0.0) -> None:
        """移动止损：从持仓期间最高价（空头为最低价）回撤 pct 就平仓；activate > 0 时浮盈达到它才启动。"""
        b = self.brackets.setdefault(security or "*", {})
        b["trail"] = {"pct": float(pct), "activate": float(activate)} if pct else None

    def set_protections(self, items: list[dict[str, Any]] | None) -> None:
        self.guards.set_protections(items)

    def set_pairlist(self, cfg: dict[str, Any] | None) -> None:
        self.guards.set_pairlist(cfg)

    def set_delistings(self, rows: list[dict[str, Any]] | None, wall: int | None = None) -> list[str]:
        """平台传入下架公告。新生效的币立刻移出标的池；delist_exit 时顺手平掉持仓。"""
        self.guards.set_delistings(rows, wall)
        return self.poll_delistings()

    def poll_delistings(self) -> list[str]:
        new = self.guards.poll()
        if new and (self.guards.pairlist or {}).get("delist_exit"):
            from . import api
            from .runtime import bind, unbind

            token = bind(self)
            try:
                for sec, qty, _avg in trade_mgmt.positions(self):
                    if qty and guards_mod.base_coin(sec) in new:
                        api.order_target(sec, 0, reason="delisted")
            finally:
                unbind(token)
        return new

    def universe(self) -> list[str]:
        return self.guards.refresh()

    def set_roi(self, table: dict[Any, float], security: str | None = None) -> None:
        """分段止盈：{持有分钟: 目标收益率}。例 {0: 0.06, 60: 0.03, 240: 0}：刚开仓要赚 6% 才走，4 小时后不亏就走。"""
        rows = sorted((int(float(k)), float(v)) for k, v in (table or {}).items())
        if any(m < 0 for m, _ in rows):
            raise ValueError("分段止盈表的分钟数不能为负")
        if rows:
            self.roi[security or "*"] = rows
        else:
            self.roi.pop(security or "*", None)

    def set_bracket(self, security: str, *, stop_loss: float | None = None, take_profit: float | None = None) -> None:
        b = self.brackets.setdefault(security, {})
        if stop_loss is not None:
            b["sl"] = max(0.0, float(stop_loss))
        if take_profit is not None:
            b["tp"] = max(0.0, float(take_profit))

    # ------------------------------------------------------------------ 永续合约
    def set_leverage(self, security: str, leverage: float) -> None:
        if not is_perp(security):
            raise ValueError(f"{security} 不是永续合约（线性 :PERP / 反向 :INVERSE / 交割 :DELIVERY），现货不能加杠杆")
        try:
            lev = self.ledger.set_leverage(security, leverage)
        except LedgerError as exc:
            raise ValueError(str(exc)) from exc
        if self.ledger.perp_book is not None:
            legs = [x for x in self.ledger.perp_book.all_legs() if x.instrument.upper() == security.upper()]
            if legs and any(x.leverage != lev for x in legs):
                self.log("info", f"{security} 杠杆改为 {dstr(lev)} 倍：对之后的开仓生效，现有持仓保证金不变")
            return
        pos = self.ledger.perps.get(security)
        if pos is not None and pos.qty and pos.leverage != lev:
            self.log("info", f"{security} 杠杆改为 {dstr(lev)} 倍：对之后的开仓生效，现有持仓保证金不变")

    def add_funding(self, security: str, rows: list[Any]) -> int:
        """并入资金费率事件。

        兼容两种行：``(settlement_ts, rate)`` 与 ``(settlement_ts, rate, mark_price)``。
        标记价是资金费结算的名义价值基准；缺失时运行时会回退到 bar 开盘/收盘并显式警告。
        """
        cur = self.funding_rates.setdefault(security, [])
        marks = self.funding_marks.setdefault(security, {})
        known = {t for t, _ in cur}
        added = 0
        for row in rows:
            if not row:
                continue
            ts = int(row[0])
            rate = row[1] if len(row) > 1 else None
            mark = row[2] if len(row) > 2 else None
            if ts in known or rate is None:
                continue
            cur.append((ts, dec(rate)))
            known.add(ts)
            try:
                if mark not in (None, "", "0", 0):
                    marks[ts] = dec(mark)
            except (TypeError, ValueError):
                pass
            added += 1
        if added:
            cur.sort()
        return added

    def funding_rate(self, security: str) -> dict[str, Any] | None:
        import bisect

        rows = self.funding_rates.get(security) or []
        i = bisect.bisect_right(rows, (self.now, Decimal("Infinity"))) - 1
        if i < 0:
            return None
        return {"ts": rows[i][0], "rate": float(rows[i][1])}

    def _settle_delivery(self) -> list[OrderResult]:
        book = self.ledger.perp_book
        if book is None or not book.expiry_ts:
            return []
        out: list[OrderResult] = []
        for instrument in {x.instrument for x in book.all_legs()}:
            spec = book.spec(instrument)
            if spec.contract_type != "delivery":
                continue
            bar = self.current_bar(instrument)
            if bar is None or bar.close_time < (book.expiry_ts or 0):
                continue
            qty = sum((x.qty for x in book.all_legs() if x.instrument == instrument), ZERO)
            pnl = book.settle_expiry(
                self.ledger,
                bar.close,
                ts=bar.close_time,
                ref=f"delivery:{instrument}:{bar.close_time}",
                instrument=instrument,
            )
            self.ledger.liquidation_loss = book.liquidation_loss
            note = f"交割到期，按结算价 {dstr(dec(bar.close))} 平仓；已实现 {dstr(pnl)} {self.ledger.base_currency}"
            self.log("info", f"{instrument} {note}")
            out.append(
                OrderResult(
                    ok=True,
                    security=instrument,
                    side="sell",
                    filled_amount=float(qty),
                    filled_price=float(bar.close),
                    message=note,
                )
            )
        if not any(book.spec(x.instrument).contract_type == "delivery" for x in book.all_legs()):
            book.expired = True
        return out

    def _settle_funding(self, *, at_open: bool) -> None:
        """Settle all funding points in (last, upto], using the recorded mark price."""
        if self.ledger.perp_book is not None:
            book = self.ledger.perp_book
            instruments = {leg.instrument for leg in book.all_legs()}
            for sec in instruments:
                spec = book.spec(sec)
                if spec.funding_interval_hours <= 0:
                    continue
                rows = self.funding_rates.get(sec)
                if not rows:
                    if sec not in self._funding_warned:
                        self._funding_warned.add(sec)
                        self.log("warning", f"{sec} 没有资金费率数据，资金费按 0 计——资金费套利类策略的结果不可信")
                    continue
                last = self.funding_ts.get(sec, 0)
                bar = self.current_bar(sec)
                if bar is None:
                    continue
                upto = bar.open_time if at_open else bar.close_time
                mark = bar.open if at_open else bar.close
                for ts, rate in rows:
                    if ts <= last or ts > upto:
                        continue
                    actual_mark = self.funding_marks.get(sec, {}).get(ts, mark)
                    delta = self.ledger.settle_funding(
                        sec, rate=rate, mark_price=actual_mark, ts=ts, ref=f"funding:{sec}:{ts}"
                    )
                    trip = self._open_trips.get(sec)
                    if trip is not None:
                        trip.realized += delta
                    last = ts
                self.funding_ts[sec] = max(self.funding_ts.get(sec, 0), last)
            return
        for sec, pos in list(self.ledger.perps.items()):
            rows = self.funding_rates.get(sec)
            if not rows:
                if pos.qty and sec not in self._funding_warned:
                    self._funding_warned.add(sec)
                    self.log("warning", f"{sec} 没有资金费率数据，资金费按 0 计——资金费套利类策略的结果不可信")
                continue
            last = self.funding_ts.get(sec, 0)
            bar = self.current_bar(sec)
            if bar is None:
                continue
            upto = bar.open_time if at_open else bar.close_time
            mark = bar.open if at_open else bar.close
            for ts, rate in rows:
                if ts <= last or ts > upto:
                    continue
                if pos.qty:
                    actual_mark = self.funding_marks.get(sec, {}).get(ts, mark)
                    delta = self.ledger.settle_funding(
                        sec, rate=rate, mark_price=actual_mark, ts=ts, ref=f"funding:{sec}:{ts}"
                    )
                    trip = self._open_trips.get(sec)
                    if trip is not None:
                        trip.realized += delta
                last = ts
            self.funding_ts[sec] = max(self.funding_ts.get(sec, 0), last)

    def _v2_liquidation_fill(self, detail: dict[str, Any], result: Any, ts: int, ref: str) -> Fill:
        settlement = detail.get("settlement_ccy") or self.ledger.base_currency
        exit_price = dec(detail.get("exit_price") or 0)
        pnl = dec(detail.get("pnl") or 0)
        quote_pnl = pnl * (exit_price if settlement != self.ledger.base_currency else Decimal(1))
        fee_quote = result.fee if len(result.details) == 1 else ZERO
        return Fill(
            ts=ts,
            security=detail["instrument"],
            side="sell" if detail.get("side") == "long" else "buy",
            amount=dec(detail.get("qty") or 0),
            price=exit_price,
            fee=ZERO,
            reason="liquidation",
            note=f"强平：{detail.get('reason') or result.reason}；MMR {detail.get('mmr_tier') or '-'}",
            realized_pnl=quote_pnl,
            fee_currency=settlement,
            fee_value=fee_quote,
            fill_id=ref,
            source="liquidation",
        )

    def _check_liquidations(self) -> list[OrderResult]:
        """Check isolated leg stops or the account-wide cross liquidation trigger."""
        book = self.ledger.perp_book
        if book is not None:
            if not book.all_legs():
                return []
            out: list[OrderResult] = []
            prices: dict[str, Decimal] = {}
            for leg in book.all_legs():
                bar = self.current_bar(leg.instrument)
                if bar is not None:
                    prices[leg.instrument] = dec(bar.close)
                    leg.last_price = dec(bar.close)
            if book.cross:
                stress: dict[str, Decimal] = {}
                for leg in book.all_legs():
                    bar = self.current_bar(leg.instrument)
                    if bar is None:
                        continue
                    stress[leg.instrument] = dec(bar.low if leg.side == "long" else bar.high)
                if not stress or not book.liquidatable(self.ledger, stress):
                    return []
                ts = max(
                    (
                        self.current_bar(x.instrument).close_time
                        for x in book.all_legs()
                        if self.current_bar(x.instrument)
                    ),
                    default=self.now,
                )
                result = book.liquidate_cross_all(
                    self.ledger,
                    stress,
                    ts=ts,
                    ref=f"{self.run_id}:cross-liq:{ts}",
                    reason="全仓账户权益低于联合维持保证金；K 线不利极值触发",
                    fee_bps=Decimal(str(book.spec(book.all_legs()[0].instrument).liquidation_fee_bps)),
                )
                self.ledger.liquidation_loss = book.liquidation_loss
                self.counters["liquidations"] = self.counters.get("liquidations", 0) + 1
                for i, detail in enumerate(result.details):
                    fill = self._v2_liquidation_fill(detail, result, ts, f"{self.run_id}:cross-liq:{ts}:{i}")
                    self.fills.append(fill)
                    self._new_fills_for_callback.append(fill)
                    out.append(
                        OrderResult(
                            ok=True,
                            security=detail["instrument"],
                            side=fill.side,
                            filled_amount=float(fill.amount),
                            filled_price=float(fill.price),
                            message=fill.note,
                        )
                    )
                self.log("error", f"全仓联合强平：{result.to_dict()}")
                return out
            for leg in list(book.all_legs()):
                bar = self.current_bar(leg.instrument)
                if bar is None:
                    continue
                liq = leg.liquidation_price(book.spec(leg.instrument))
                hit = (leg.side == "long" and dec(bar.low) <= liq) or (leg.side == "short" and dec(bar.high) >= liq)
                if not hit or liq <= 0:
                    continue
                spec = book.spec(leg.instrument)
                slip = (leg.liquidation_slippage_bps or dec(spec.liquidation_slippage_bps)) / Decimal(10000)
                exit_px = liq * (Decimal(1) - slip if leg.side == "long" else Decimal(1) + slip)
                qty = leg.qty
                result = book.liquidate(
                    self.ledger,
                    leg,
                    price=max(exit_px, Decimal("0.00000001")),
                    ts=bar.close_time,
                    ref=f"{self.run_id}:liq:{leg.instrument}:{bar.close_time}",
                    mark_price=bar.close,
                    reason=f"K 线不利极值触及强平价 {dstr(liq)}；MMR {leg.mmr_tier or '-'}",
                )
                self.ledger.liquidation_loss = book.liquidation_loss
                self.counters["liquidations"] = self.counters.get("liquidations", 0) + 1
                for i, detail in enumerate(result.details):
                    fill = self._v2_liquidation_fill(
                        detail, result, bar.close_time, f"{self.run_id}:liq:{leg.instrument}:{bar.close_time}:{i}"
                    )
                    self.fills.append(fill)
                    self._new_fills_for_callback.append(fill)
                    out.append(
                        OrderResult(
                            ok=True,
                            security=leg.instrument,
                            side=fill.side,
                            filled_amount=float(qty),
                            filled_price=float(fill.price),
                            message=fill.note,
                        )
                    )
                self.log("error", f"{leg.instrument} 逐腿强平：{result.to_dict()}")
            return out
        out: list[OrderResult] = []
        for sec, pos in list(self.ledger.perps.items()):
            if not pos.qty:
                continue
            bar = self.current_bar(sec)
            if bar is None:
                continue
            liq = pos.liquidation_price
            hit = (pos.qty > 0 and dec(bar.low) <= liq) or (pos.qty < 0 and dec(bar.high) >= liq)
            if not hit or liq <= 0:
                continue
            side = "sell" if pos.qty > 0 else "buy"
            qty = abs(pos.qty)
            spec = spec_for(self.exchange, sec)
            slip_bps = float(pos.liquidation_slippage_bps or spec.liquidation_slippage_bps)
            fee_bps = float(pos.liquidation_fee_bps or spec.liquidation_fee_bps)
            q0 = abs(pos.qty)
            adverse = (
                liq * (Decimal(1) - Decimal(str(slip_bps)) / Decimal(10000))
                if pos.qty > 0
                else liq * (Decimal(1) + Decimal(str(slip_bps)) / Decimal(10000))
            )
            px = self._round_price(max(adverse, Decimal("0.00000001")))
            ts = bar.close_time
            self.order_seq += 1
            fill_id = f"{self.run_id}:liq:{sec}:{bar.close_time}"
            margin = pos.margin
            mark = pos.last_price or liq
            fee = q0 * liq * Decimal(str(fee_bps)) / Decimal(10000)
            booking = self.ledger.liquidate(
                sec,
                price=px,
                ts=ts,
                ref=fill_id,
                fee=fee,
                slippage_bps=slip_bps,
                mark_price=mark,
                reason=f"K 线 high/low 触及强平价；MMR 档位 {pos.mmr_tier or spec.tier_for_notional(float(q0 * liq))['label']}",
            )
            self.counters["liquidations"] = self.counters.get("liquidations", 0) + 1
            note = (
                f"触及强平价 {dstr(liq)}，按滑点后 {dstr(px)} 强平；损失保证金 "
                f"{dstr(quantize(margin, Decimal('0.01')))}，强平费 {dstr(quantize(booking.fee, Decimal('0.01')))}"
                f"；MMR 档位 {pos.mmr_tier or spec.tier_for_notional(float(q0 * liq))['label']}"
            )
            if self.fill_mode == "external":
                note += "（交易所模拟盘的强平由交易所执行，这里按强平价估算记账，以对账结果为准）"
            self._track_round_trip(sec, side, ts, qty * px, booking, reason="liquidation")
            fill = Fill(
                ts=ts,
                security=sec,
                side=side,
                amount=qty,
                price=px,
                fee=ZERO,
                reason="liquidation",
                note=note,
                realized_pnl=booking.realized,
                fee_currency=pos.quote,
                fill_id=fill_id,
                source="liquidation",
            )
            self.fills.append(fill)
            self._new_fills_for_callback.append(fill)
            self.log("error", f"{sec} 被强平：{note}")
            out.append(
                OrderResult(
                    ok=True, security=sec, side=side, filled_amount=float(qty), filled_price=float(px), message=note
                )
            )
        return out

    # ------------------------------------------------------------------ 调度与输出
    def schedule(self, entry: ScheduleEntry) -> None:
        if entry.func_name not in {s.func_name for s in self._schedules}:
            self._schedules.append(entry)

    def schedules(self) -> list[ScheduleEntry]:
        return list(self._schedules)

    def log(self, level: str, message: str) -> None:
        self.logs.append({"ts": self.now, "bar": self.ctx.bar_index, "level": level, "message": message})

    def record(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            self.records.setdefault(k, []).append({"t": self.now, "v": v})

    def research_table(self, kind: str, name: str) -> dict[str, Any] | None:
        tbl = self.research.get(f"{kind}:{name}")
        if tbl is None and f"{kind}:{name}" not in self._research_missing:
            self._research_missing.add(f"{kind}:{name}")
            self.log(
                "warning", f"{kind}('{name}') 没有预先计算的数据（只有平台回测会准备；编辑器试跑、实时模拟里返回空）"
            )
        return tbl

    def research_value(self, kind: str, name: str, security: str | None = None) -> Any:
        """按当前 bar 时间往回取最近一个已计算的值：只看得到已收盘的数据。"""
        import bisect

        tbl = self.research_table(kind, name)
        if not tbl or not tbl.get("times"):
            return None if security else {}
        i = bisect.bisect_right(tbl["times"], self.now) - 1
        if i < 0:
            return None if security else {}
        row = tbl["values"][i]
        if security is not None:
            try:
                return row[tbl["symbols"].index(security)]
            except ValueError:
                return None
        return {s: v for s, v in zip(tbl["symbols"], row, strict=True) if v is not None}

    def context(self) -> Context:
        return self.ctx

    # ------------------------------------------------------------------ 资金流（时间加权收益）
    @property
    def unit_nav(self) -> Decimal:
        return (self.ledger.equity() / self.units) if self.units else Decimal(1)

    def apply_flow(self, amount: Any, *, ts: int, ref: str = "") -> dict[str, Any]:
        """入金（正）/ 出金（负）。按流入前的单位净值申购 / 赎回份额，收益率不受资金流影响。"""
        amt = dec(amount)
        nav = self.unit_nav
        if amt > 0:
            self.ledger.deposit(amt, ts=ts, ref=ref or f"flow:{ts}")
        elif amt < 0:
            self.ledger.withdraw(-amt, ts=ts, ref=ref or f"flow:{ts}")
        else:
            return {}
        self.units += amt / nav if nav else amt
        row = {
            "ts": ts,
            "amount": dstr(amt),
            "unit_nav": dstr(quantize(nav, Decimal("1e-12"))),
            "units": dstr(self.units),
        }
        self.flows_applied.append(row)
        return row

    # ------------------------------------------------------------------ 信号
    def _make_signal(self, req: OrderRequest, *, bar: Bar, order_type: str = "market") -> Signal:
        security = req.security
        event_id = f"{security}:{bar.close_time}"
        n = self._event_seq.get(event_id, 0) + 1
        self._event_seq[event_id] = n
        raw = f"{self.run_id}|{self.strategy_version_id}|{security}|{event_id}|{n}"
        sid = hashlib.sha256(raw.encode()).hexdigest()[:24]
        ttl = max(1, int(self.cost.signal_ttl_bars or 1)) * self.bar_ms(security)
        expires = bar.close_time + ttl
        if req.expire_bars:
            expires = bar.close_time + int(req.expire_bars) * self.bar_ms(security)
        kind_map = {
            "percent_target": ("weight", "fraction"),
            "value_target": ("value", "quote"),
            "amount_target": ("quantity", "base"),
            "value": ("delta_value", "quote"),
        }
        kind, unit = kind_map.get(req.target_kind, ("delta_quantity", "base"))
        value = req.target_value if req.target_kind else (req.amount if req.side == "buy" else -req.amount)
        target = TargetPosition(
            instrument_id=security, kind=kind, value=dstr(dec(value)) or "0", unit=unit, valid_until=expires
        )
        sig = Signal(
            signal_id=sid,
            run_id=self.run_id,
            strategy_version_id=self.strategy_version_id,
            instrument_id=security,
            event_id=event_id,
            decision_ts=bar.close_time,
            reason=req.reason,
            expires_at=expires,
            target=target,
            order_type=order_type,
            limit_price=dstr(dec(req.limit_price)) if req.limit_price else None,
        )
        return sig

    def _register(self, sig: Signal) -> bool:
        """登记信号。同一 signal_id 已存在（检查点恢复后重放同一事件）→ 不再执行。"""
        if sig.signal_id in self.signal_ids:
            return False
        self.signal_ids.add(sig.signal_id)
        self.signals.append(sig)
        if len(self.signals) > 20_000:
            del self.signals[:5_000]
        return True

    def _set_status(self, sig: Signal | None, status: str, reason: str = "", code: str = "") -> None:
        if sig is None:
            return
        sig.status, sig.status_reason, sig.reason_code = status, reason, code

    # ------------------------------------------------------------------ 交易
    def effective_cost(self) -> dict[str, Any]:
        """报告里的成本模型：代码覆盖后的成本，再乘上压力倍数（倍数为 1 时与原值相同）。"""
        out = self.cost.to_dict()
        out["cost_source"] = self.cost_source
        if self.cost_overrides:
            out["strategy_cost_overrides"] = dict(self.cost_overrides)
        if self.slippage_override is not None:
            out["slippage_bps"] = self.slippage_override
            out["slippage_override"] = self.slippage_override
        if self.impact_model is not None:
            out["impact_model"] = dict(self.impact_model)
        if self.cost_scale != 1.0:
            out["cost_scale"] = self.cost_scale
            for k in ("taker_fee", "maker_fee", "slippage_bps", "spread_bps"):
                if isinstance(out.get(k), (int, float)):
                    out[f"base_{k}"] = out[k]
                    out[k] = out[k] * self.cost_scale
        return out

    def _fee_rate(self, rate: float) -> Decimal:
        """实际收取的手续费率（含成本压力倍数）。"""
        return dec(rate) * dec(self.cost_scale)

    def _exec_price(
        self,
        base: float | Decimal,
        side: str,
        participation: float | None = None,
        *,
        bar: Bar | None = None,
    ) -> Decimal:
        """成交价 = 基准价 ± 半价差 + 固定滑点 + 动态滑点 + 冲击成本。

        动态滑点按本根振幅放大；冲击成本优先用执行方案校准的 a/b，否则用回测规格里的
        a + b·√(参与率)。这样大单在低成交量 bar 上会自然得到更差的价格，而不是永远只扣固定 bp。
        """
        slippage = self.slippage_override if self.slippage_override is not None else self.cost.slippage_bps
        adj = dec(self.cost.spread_bps) / 2 + dec(slippage)
        p = max(0.0, float(participation or 0.0))
        if self.impact_model is not None and participation is not None:
            # 执行方案的成本曲线已经包含价差与冲击（不含手续费），替代固定的价差 + 滑点
            adj = dec(max(self.impact_model["a"] + self.impact_model["b"] * math.sqrt(p), 0.0))
        else:
            if self.cost.impact_a_bps or self.cost.impact_b_bps:
                adj += dec(self.cost.impact_a_bps + self.cost.impact_b_bps * math.sqrt(p))
        if self.cost.dynamic_slippage and bar is not None and bar.open > 0:
            rng_bps = max(0.0, (bar.high - bar.low) / bar.open * 10_000)
            adj += dec(rng_bps * max(0.0, self.cost.vol_slippage))
        adj = max(adj, 0) * dec(self.cost_scale)
        out = dec(base) * (1 + adj / 10_000) if side == "buy" else dec(base) * (1 - adj / 10_000)
        return self._round_price(out)

    def _tick_base_price(self, bar: Bar, side: str, participation: float = 0.0) -> Decimal:
        """用 OHLC 路径近似盘中逐笔成交价（不是未来数据：成交发生在下一根 bar 的路径里）。

        路径按阳线 / 阴线选择，再按参与率向最不利的一端偏移。把回测周期设为 50ms~1s 时，
        输入本身就是逐笔 / 子秒级 bar，撮合粒度也会随周期自动变细。
        """
        o, h, lo, c = float(bar.open), float(bar.high), float(bar.low), float(bar.close)
        path = (o, lo, h, c) if c >= o else (o, h, lo, c)
        weights = (0.35, 0.2, 0.3, 0.15)
        vwap = sum(x * w for x, w in zip(path, weights, strict=False))
        p = min(1.0, max(0.0, float(participation)))
        if side == "buy":
            vwap += (h - vwap) * p * 0.5
        else:
            vwap -= (vwap - lo) * p * 0.5
        return self._round_price(dec(max(vwap, 1e-12)))

    def _round_price(self, p: Decimal) -> Decimal:
        tick = dec(self.cost.price_tick) if self.cost.price_tick else PRICE_STEP
        return quantize(p, tick)

    def _reject(
        self, req: OrderRequest, reason: str, *, code: str = "REJECTED", sig: Signal | None = None
    ) -> OrderResult:
        self.rejected.append(
            {
                "ts": self.now,
                "security": req.security,
                "side": req.side,
                "reason": reason,
                "code": code,
                "signal_id": sig.signal_id if sig else "",
            }
        )
        self._set_status(sig, "rejected", reason, code)
        return OrderResult(ok=False, security=req.security, side=req.side, message=reason, status="rejected")

    def can_afford(self, security: str, amount: float, price: float) -> bool:
        need = dec(amount) * dec(price) * (1 + self._fee_rate(self.cost.taker_fee))
        return need <= self.ledger.cash()

    def _held(self, security: str, position_side: str = "") -> Decimal:
        book = self.ledger.perp_book
        if book is not None and is_derivative(security):
            legs = [x for x in book.all_legs() if x.instrument.upper() == security.upper()]
            if position_side:
                return sum((x.signed_qty for x in legs if x.side == position_side.lower()), ZERO)
            return sum((x.signed_qty for x in legs), ZERO) + self._ext_qty.get(security, ZERO)
        if is_perp(security):
            p = self.ledger.perps.get(security)
            return (p.qty if p else ZERO) + self._ext_qty.get(security, ZERO)
        h = self.ledger.holdings.get(security)
        # 交易所模拟盘：已送出未回报的委托按预期计入，避免同一目标重复下单
        return (h.qty if h else ZERO) + self._ext_qty.get(security, ZERO)

    def _projected_amount(self, security: str, position_side: str = "") -> Decimal:
        """当前持仓 + 已委托未成交的净数量。委托排队时用它判断"还要不要再调"。"""
        amount = self._held(security, position_side)
        for item in self.pending:
            if item["security"] == security and (not position_side or item.get("req").position_side == position_side):
                amount += item["delta"]
        return amount

    def _target_amount(self, req: OrderRequest, *, held: Decimal, price: Decimal, equity: Decimal) -> Decimal:
        """把下单意图换算成目标数量（Decimal），并计入合约面值。"""
        spec = None
        book = self.ledger.perp_book
        if is_derivative(req.security):
            spec = (
                book.spec(req.security) if book is not None else spec_for(self.exchange, req.security, self.expiry_ts)
            )
        mult = dec(spec.contract_multiplier) if spec is not None else Decimal(1)
        if req.target_kind == "percent_target":
            if not price:
                return held
            if spec is not None and spec.contract_type == "inverse":
                return (equity * dec(req.target_value)) / mult
            return (equity * dec(req.target_value)) / (price * mult)
        if req.target_kind == "value_target":
            if not price:
                return held
            if spec is not None and spec.contract_type == "inverse":
                return dec(req.target_value) / mult
            return dec(req.target_value) / (price * mult)
        if req.target_kind == "amount_target":
            return dec(req.target_value)
        if req.target_kind == "value":
            if not price:
                return held
            if spec is not None and spec.contract_type == "inverse":
                return held + dec(req.target_value) / mult
            return held + dec(req.target_value) / (price * mult)
        return held + (dec(req.amount) if req.side == "buy" else -dec(req.amount))

    def _threshold(self, equity: Decimal) -> Decimal:
        return max(dec(self.cost.min_notional), equity * dec(self.cost.rebalance_tolerance))

    def _tolerance_result(
        self, req: OrderRequest, delta: Decimal, price: Decimal, threshold: Decimal, sig: Signal | None = None
    ) -> OrderResult:
        msg = (
            f"调仓差额 {float(abs(delta) * price):.2f} 低于容差 {float(threshold):.2f}"
            f"（最小下单额 {self.cost.min_notional:g} 或净值 {self.cost.rebalance_tolerance:.1%}），不成交"
        )
        self._set_status(sig, "tolerance", msg, "BELOW_TOLERANCE")
        return OrderResult(
            ok=False, security=req.security, side="buy" if delta > 0 else "sell", message=msg, status="rejected"
        )

    def submit(self, req: OrderRequest) -> OrderResult:
        """接收下单意图，生成信号。市价单默认排队到下一根 bar 开盘成交；限价单进入挂单簿。"""
        security = req.security
        bar = self.current_bar(security)
        if bar is None:
            return self._reject(req, f"{security} 在当前时刻没有可用行情", code="DATA_INCOMPLETE")
        if bar.volume <= 0:
            return self._reject(req, f"{security} 当前 bar 成交量为 0，视为不可交易", code="NOT_TRADABLE")
        order_type = "limit" if req.limit_price else "market"
        tif = req.time_in_force or "GTC"
        if tif not in trade_mgmt.TIF:
            return self._reject(req, f"time_in_force 只能是 {'、'.join(trade_mgmt.TIF)}", code="INVALID_PARAMETER")
        if (req.on_expire or "cancel") not in trade_mgmt.ON_EXPIRE:
            return self._reject(req, f"on_expire 只能是 {'、'.join(trade_mgmt.ON_EXPIRE)}", code="INVALID_PARAMETER")
        if tif != "GTC" and order_type != "limit":
            return self._reject(req, f"{tif} 只适用于限价单，请同时给 limit_price", code="INVALID_PARAMETER")
        if self.confirm_hook is not None and trade_mgmt.is_entry(self, req, bar):
            ok = self.confirm_hook(
                req.security, req.side, float(req.amount or 0), float(req.limit_price or bar.close), req.reason
            )
            if ok is False:
                self.rejected.append(
                    {
                        "ts": self.now,
                        "security": req.security,
                        "side": req.side,
                        "reason": "confirm_entry 否决了这笔开仓",
                        "code": "ENTRY_VETOED",
                        "signal_id": "",
                    }
                )
                return OrderResult(
                    ok=False, security=req.security, side=req.side, message="confirm_entry 否决", status="rejected"
                )
        if self.guards.active and trade_mgmt.is_entry(self, req, bar):
            blocked = self.guards.entry_block(req.security)
            if blocked:
                code, why = blocked
                self.guards.note_block(code)
                self.rejected.append(
                    {
                        "ts": self.now,
                        "security": req.security,
                        "side": req.side,
                        "reason": why,
                        "code": code,
                        "signal_id": "",
                    }
                )
                return OrderResult(ok=False, security=req.security, side=req.side, message=why, status="rejected")
        sig = self._make_signal(req, bar=bar, order_type=order_type)
        if not self._register(sig):
            return OrderResult(
                ok=False, security=security, side=req.side, message="信号已处理过（事件重放），忽略", status="duplicate"
            )

        if order_type == "limit":
            if dec(req.limit_price) <= 0:
                return self._reject(req, "限价必须为正", code="INVALID_PARAMETER", sig=sig)
            if tif == "post_only":
                held = self._projected_amount(security)
                buy = self._target_amount(req, held=held, price=dec(bar.close), equity=self.ledger.equity()) > held
                if (dec(req.limit_price) >= dec(bar.close)) if buy else (dec(req.limit_price) <= dec(bar.close)):
                    return self._reject(
                        req,
                        "post_only 限价已越过当前价，挂出去会立刻吃单，交易所会拒绝",
                        code="POST_ONLY_WOULD_TAKE",
                        sig=sig,
                    )
            self.open_orders.append({"req": req, "security": security, "signal": sig, "placed_at": bar.close_time})
            self.order_seq += 1
            return OrderResult(
                ok=True,
                security=security,
                side=req.side,
                status="open",
                message=f"限价委托已挂出：{req.limit_price}（保守 K 线撮合）",
            )

        if self.cost.fill_at == "close":
            return self._fill(req, bar=bar, base_price=dec(bar.close), sig=sig, decision_price=dec(bar.close))

        equity = self.ledger.equity()
        held = self._projected_amount(security)
        close = dec(bar.close)
        target = self._target_amount(req, held=held, price=close, equity=equity)
        delta = target - held
        threshold = self._threshold(equity)
        if abs(delta) * close < threshold:
            return self._tolerance_result(req, delta, close, threshold, sig)
        self.pending.append(
            {
                "req": req,
                "security": security,
                "delta": delta,
                "decided_at": bar.close_time,
                "signal": sig,
                "decision_price": close,
            }
        )
        self.order_seq += 1
        return OrderResult(
            ok=True,
            security=security,
            side="buy" if delta > 0 else "sell",
            message="已委托：下一根 bar 开盘价成交",
            status="pending",
        )

    def cancel_all(self, security: str | None = None) -> int:
        n = 0
        for book in (self.pending, self.open_orders):
            keep = []
            for item in book:
                if security is None or item["security"] == security:
                    self._set_status(item.get("signal"), "canceled", "策略撤单", "CANCELED")
                    n += 1
                else:
                    keep.append(item)
            book[:] = keep
        return n

    # ------------------------------------------------------------------ 每根新 bar 的执行阶段
    def _has_derivative_positions(self) -> bool:
        if self.ledger.perp_book is not None:
            return bool(self.ledger.perp_book.all_legs())
        return bool(self.ledger.perps)

    def execute(self) -> list[OrderResult]:
        """新 bar 到来时的执行阶段：资金费 → 开盘撮合 → 到期/强平 → 限价/保护单。"""
        if self.funding_rates or self._has_derivative_positions():
            self._settle_funding(at_open=True)
        out = self.execute_pending()
        if self._has_derivative_positions():
            out += self._settle_delivery()
            out += self._check_liquidations()
            self._settle_funding(at_open=False)
        out += self._match_limits()
        out += self._check_brackets()
        return out

    def execute_pending(self) -> list[OrderResult]:
        """把上一根 bar 收盘时产生的委托，按当前 bar 开盘价撮合。"""
        if not self.pending:
            return []
        queue, self.pending = self.pending, []
        opens = {}
        for item in queue:
            bar = self.current_bar(item["security"])
            if bar is not None:
                opens[item["security"]] = bar.open
        self.ledger.mark(opens)
        results: list[OrderResult] = []
        for item in queue:
            bar = self.current_bar(item["security"])
            sig = item.get("signal")
            if bar is None or bar.open_time < item["decided_at"]:
                self.pending.append(item)  # 多标的时间轴不齐：这个标的还没有新 bar，继续等
                continue
            if sig is not None and bar.open_time > sig.expires_at:
                self.counters["expired_signals"] += 1
                self._set_status(sig, "expired", "信号已过有效期（停机或数据缺口），不补成交", "SIGNAL_EXPIRED")
                self.log("warning", f"{item['security']} 的委托已过期，不补成交")
                continue
            results.append(
                self._fill(
                    item["req"],
                    bar=bar,
                    base_price=dec(bar.open),
                    at_open=True,
                    sig=sig,
                    decision_price=item.get("decision_price"),
                )
            )
        return results

    def route_now(self) -> list[OrderResult]:
        """交易所模拟盘：收盘决策后立刻送单，参考价用这根 bar 的收盘价，不等下一根开盘。

        平台撮合（paper / 回测）仍按下一根开盘成交；两者的差别在仿真真实度报告里记为"延迟"。
        """
        if self.fill_mode != "external" or not self.pending:
            return []
        queue, self.pending = self.pending, []
        self.ledger.mark({it["security"]: b.close for it in queue if (b := self.current_bar(it["security"]))})
        results: list[OrderResult] = []
        for item in queue:
            bar = self.current_bar(item["security"])
            if bar is None or bar.close_time < item["decided_at"]:
                self.pending.append(item)
                continue
            results.append(
                self._fill(
                    item["req"],
                    bar=bar,
                    base_price=dec(bar.close),
                    sig=item.get("signal"),
                    decision_price=item.get("decision_price"),
                )
            )
        return results

    def _match_limits(self) -> list[OrderResult]:
        """限价挂单的 K 线撮合（保守）。

        开盘已越过限价 → 按开盘价吃单（不劣于限价）；盘中触及 → 按限价做挂单方。

        time_in_force：IOC 只看挂出后第一根的开盘，吃不到就撤；
        post_only 开盘已越过限价时撤单（交易所会拒绝吃单的挂单）。
        到期按 on_expire 处理：cancel 撤单；market 按这根开盘价改市价成交；chase 按开盘价重挂，最多 3 次。
        """
        if not self.open_orders:
            return []
        results: list[OrderResult] = []
        keep: list[dict[str, Any]] = []
        for item in self.open_orders:
            req: OrderRequest = item["req"]
            sig: Signal = item["signal"]
            bar = self.current_bar(item["security"])
            if bar is None or bar.open_time < item["placed_at"]:
                keep.append(item)
                continue
            tif = req.time_in_force or "GTC"
            o, lo, hi = dec(bar.open), dec(bar.low), dec(bar.high)
            if bar.open_time > sig.expires_at:
                action = req.on_expire or "cancel"
                if action == "market":
                    self._set_status(sig, "accepted", "限价单到期未成交，按 on_expire=market 改市价")
                    results.append(self._fill(req, bar=bar, base_price=o, at_open=True, sig=sig, decision_price=o))
                    continue
                if action == "chase" and int(item.get("chases") or 0) < trade_mgmt.MAX_CHASES:
                    ttl = sig.expires_at - item.get("first_placed", item["placed_at"])
                    item = {
                        **item,
                        "req": replace(req, limit_price=float(o)),
                        "placed_at": bar.close_time,
                        "chases": int(item.get("chases") or 0) + 1,
                        "first_placed": item.get("first_placed", item["placed_at"]),
                    }
                    sig.expires_at = bar.close_time + max(ttl, self.bar_ms(item["security"]))
                    sig.limit_price = dstr(o)
                    self.log(
                        "info",
                        f"{item['security']} 限价单到期未成交，按开盘价 {dstr(o)} 重挂（第 {item['chases']} 次）",
                    )
                    keep.append(item)
                    continue
                self.counters["expired_signals"] += 1
                self._set_status(sig, "expired", "限价单到期未成交", "SIGNAL_EXPIRED")
                continue
            limit = dec(req.limit_price)
            buy = req.side == "buy"
            crossed = o < limit if buy else o > limit
            touched = lo < limit if buy else hi > limit
            if crossed:
                if tif == "post_only":
                    self._set_status(
                        sig, "canceled", "post_only：开盘已越过限价，挂出去会吃单，交易所拒绝", "POST_ONLY_WOULD_TAKE"
                    )
                    continue
                results.append(
                    self._fill(
                        req,
                        bar=bar,
                        base_price=o,
                        at_open=True,
                        sig=sig,
                        decision_price=limit,
                        liquidity="taker",
                        price_cap=limit,
                    )
                )
                continue
            if tif == "IOC":
                self._set_status(sig, "canceled", "IOC：开盘没能立即成交，已撤单", "IOC_CANCELED")
                continue
            if touched:
                results.append(
                    self._fill(
                        req, bar=bar, base_price=limit, at_open=False, sig=sig, decision_price=limit, liquidity="maker"
                    )
                )
                continue
            if (lo == limit) if buy else (hi == limit):
                self.counters["limit_touch_ambiguous"] += 1
            keep.append(item)
        self.open_orders = keep
        return results

    def _check_brackets(self) -> list[OrderResult]:
        """止损止盈：固定比例、移动止损、custom_stoploss、分段止盈（ROI）里最先触发的那个。

        跳空越过 → 按开盘价；盘中触及 → 按止损/止盈价；同一根同时触及止损和止盈 → 保守假设止损先触发。
        """
        results: list[OrderResult] = []
        for position_key, held_qty, avg in trade_mgmt.positions(self):
            security, _, position_side = position_key.partition("|")
            short = held_qty < 0
            stops, takes = trade_mgmt.exit_levels(self, security, held_qty, avg)
            if not stops and not takes:
                continue
            bar = self.current_bar(security)
            if bar is None or bar.volume <= 0:
                continue
            o, lo, hi = dec(bar.open), dec(bar.low), dec(bar.high)
            sl_px, sl_kind = stops[0] if stops else (None, "")
            tp_px, tp_kind = takes[0] if takes else (None, "")
            if short:
                gap_sl = sl_px is not None and o >= sl_px
                gap_tp = tp_px is not None and o <= tp_px
                touch_sl = sl_px is not None and hi >= sl_px
                touch_tp = tp_px is not None and lo <= tp_px
            else:
                gap_sl = sl_px is not None and o <= sl_px
                gap_tp = tp_px is not None and o >= tp_px
                touch_sl = sl_px is not None and lo <= sl_px
                touch_tp = tp_px is not None and hi >= tp_px
            hit, base, liq = "", ZERO, "taker"
            if gap_sl:
                hit, base = sl_kind, o  # 跳空越过止损：按开盘价
            elif gap_tp:
                hit, base = tp_kind, o
            elif touch_sl:
                if touch_tp:
                    self.counters["bracket_same_bar"] += 1  # 同根同时触及：保守假设止损先触发
                hit, base = sl_kind, sl_px
            elif touch_tp:
                hit, base, liq = tp_kind, tp_px, "maker"
            if not hit:
                continue
            req = OrderRequest(
                security=security,
                side="buy" if short else "sell",
                amount=float(abs(held_qty)),
                reason=hit,
                target_kind="amount_target",
                target_value=0.0,
                position_side=position_side,
            )
            sig = self._make_signal(req, bar=bar)
            self._register(sig)
            r = self._fill(
                req,
                bar=bar,
                base_price=base,
                at_open=(base == o),
                sig=sig,
                decision_price=base,
                liquidity=liq,
                skip_tolerance=True,
                skip_deviation=True,
            )
            if r.ok:
                label = EXIT_LABELS.get(hit, hit)
                self.log("info", f"{security} 触发{label}，平掉全部持仓")
            results.append(r)
        return results

    # ------------------------------------------------------------------ 撮合核心
    def _fill(
        self,
        req: OrderRequest,
        *,
        bar: Bar,
        base_price: Decimal,
        at_open: bool = False,
        sig: Signal | None = None,
        decision_price: Decimal | None = None,
        liquidity: str = "taker",
        price_cap: Decimal | None = None,
        skip_tolerance: bool = False,
        skip_deviation: bool = False,
    ) -> OrderResult:
        security = req.security
        ledger = self.ledger
        held = self._held(security, req.position_side)
        if bar.volume <= 0:
            return self._reject(req, f"{security} 成交 bar 的成交量为 0，委托作废", code="NOT_TRADABLE", sig=sig)
        # 涨跌停 / 一字板：加密现货默认关闭（limit_move_pct=0），股票或模拟市场可显式开启。
        if self.cost.limit_move_pct > 0:
            bars = self.bars_by_security.get(security) or []
            idx = self._index.get(security, -1)
            prev = bars[idx - 1] if idx > 0 else None
            if prev is not None and prev.close > 0:
                move = (bar.open / prev.close) - 1.0
                locked = bar.high <= bar.low and abs(move) >= self.cost.limit_move_pct - 1e-9
                if locked and ((move > 0 and req.side == "buy") or (move < 0 and req.side == "sell")):
                    self.counters["limit_locked"] += 1
                    return self._reject(
                        req,
                        f"{security} 触及{'涨停' if move > 0 else '跌停'}且无流动性，{req.side} 委托无法成交",
                        code="LIMIT_LOCKED",
                        sig=sig,
                    )
        if self.cost.min_liquidity_usd > 0 and bar.volume * float(base_price) < self.cost.min_liquidity_usd:
            self.counters["liquidity_drought"] += 1
            return self._reject(
                req,
                f"{security} 本根流动性不足（成交额约 {bar.volume * float(base_price):,.0f}，"
                f"低于下限 {self.cost.min_liquidity_usd:,.0f}）",
                code="LIQUIDITY_DROUGHT",
                sig=sig,
            )
        if at_open and self.cost.execution_mode == "tick" and bar.high > bar.low:
            est_part = abs(float(req.amount or 0.0)) / bar.volume if bar.volume else 0.0
            base_price = self._tick_base_price(bar, req.side, est_part)
            self.counters["tick_path_fills"] += 1
        ledger.mark({security: base_price})
        equity = ledger.equity()

        target = self._target_amount(req, held=held, price=base_price, equity=equity)
        delta = target - held
        threshold = self._threshold(equity)
        tolerance_spec = (
            ledger.perp_book.spec(security) if ledger.perp_book is not None and is_derivative(security) else None
        )
        tolerance_notional = abs(delta) * base_price
        if tolerance_spec is not None:
            tolerance_notional = abs(delta) * dec(tolerance_spec.contract_multiplier)
            if tolerance_spec.contract_type != "inverse":
                tolerance_notional *= base_price
        if not skip_tolerance and tolerance_notional < threshold:
            if at_open:
                self.log("info", f"{security} 开盘时调仓差额已低于容差，委托不成交")
            return self._tolerance_result(req, delta, base_price, threshold, sig)

        side = "buy" if delta > 0 else "sell"
        if liquidity == "maker":
            exec_price = self._round_price(base_price)
        else:
            part = float(abs(delta)) / bar.volume if bar.volume else 0.0
            exec_price = self._exec_price(base_price, side, part, bar=bar)
            if price_cap is not None:
                exec_price = min(exec_price, price_cap) if side == "buy" else max(exec_price, price_cap)
        rate = self._fee_rate(self.cost.maker_fee if liquidity == "maker" else self.cost.taker_fee)
        step = dec(self.cost.qty_step) if self.cost.qty_step else Decimal("1e-12")
        qty = floor_to(abs(delta), step)
        explicit = req.target_kind in ("", "amount", "amount_target", "value", "value_target")
        notes: list[str] = []
        partial = False
        perp = is_derivative(security)
        # 永续：反向成交先平掉现有持仓（closing），超出部分才是新开仓；现货的买入就是开仓
        closing = min(qty, abs(held)) if perp and held and (held > 0) != (delta > 0) else ZERO
        opens = (qty > closing) if perp else side == "buy"

        # ---- 风控层 ----
        if not skip_deviation and decision_price and self.risk.max_price_deviation:
            dev = abs(exec_price / decision_price - 1)
            if dev > dec(self.risk.max_price_deviation):
                self.counters["risk_rejections"] += 1
                return self._reject(
                    req,
                    f"成交价相对决策价偏离 {float(dev):.2%}，超过风控上限 {self.risk.max_price_deviation:.0%}",
                    code="RISK_PRICE_DEVIATION",
                    sig=sig,
                )
        if self.risk.max_trades_per_day:
            day = _day(bar.close_time)
            if (
                self.trades_per_day.get(day, 0) >= self.risk.max_trades_per_day
                and req.reason not in trade_mgmt.STOP_REASONS
            ):
                self.counters["risk_rejections"] += 1
                return self._reject(
                    req, f"当日成交笔数已达上限 {self.risk.max_trades_per_day}", code="RISK_TRADE_LIMIT", sig=sig
                )
        if opens:
            if self.risk.drawdown_pause and self.peak_nav > 0:
                dd = 1 - self.unit_nav / self.peak_nav
                if dd > dec(self.risk.drawdown_pause):
                    self.counters["risk_rejections"] += 1
                    return self._reject(
                        req,
                        f"回撤 {float(dd):.1%} 超过 {self.risk.drawdown_pause:.0%}，风控暂停新开仓",
                        code="RISK_DRAWDOWN_PAUSE",
                        sig=sig,
                    )
            caps = []
            if self.risk.max_position_weight and self.risk.max_position_weight < 1:
                caps.append(
                    (equity * dec(self.risk.max_position_weight) - (abs(held) - closing) * base_price, "单标的权重上限")
                )
            if self.risk.max_gross_exposure and self.risk.max_gross_exposure < 1:
                gross = ledger.gross_exposure()
                caps.append((equity * dec(self.risk.max_gross_exposure) - gross, "总敞口上限"))
            for room, label in caps:
                max_q = closing + floor_to(max(ZERO, room) / exec_price, step)
                if qty > max_q * (1 + Decimal("1e-9")):
                    if explicit:
                        self.counters["risk_rejections"] += 1
                        return self._reject(req, f"超过{label}（{label}按权益计算）", code="RISK_LIMIT", sig=sig)
                    qty = max_q
                    notes.append(f"按{label}缩减")

        # ---- 成交量占比上限（真实的部分成交）----
        max_qty = floor_to(dec(bar.volume) * dec(self.cost.max_volume_participation), step)
        if max_qty <= 0:
            self.counters["liquidity_drought"] += 1
            return self._reject(
                req, f"{security} 本根成交量不足以成交一个最小数量步长，委托作废", code="LIQUIDITY_DROUGHT", sig=sig
            )
        if qty > max_qty:
            qty = max_qty
            partial = True
            notes.append(f"受单根 bar 成交量占比 {self.cost.max_volume_participation:.0%} 限制，未成交部分作废")

        # ---- 资金 / 持仓约束 ----
        fee_ccy_mode = self.cost.fee_currency
        if perp:
            if qty > closing:
                book = ledger.perp_book
                spec = book.spec(security) if book is not None else spec_for(self.exchange, security, self.expiry_ts)
                lev = ledger.leverage.get(security, Decimal(1))
                mult = dec(spec.contract_multiplier)
                fee_unit = (mult / exec_price if spec.contract_type == "inverse" else mult * exec_price) * rate
                if book is not None:
                    per_unit = book.margin_for(security, Decimal(1), exec_price) + fee_unit
                    avail = book.available(ledger) if book.cross else ledger.cash(spec.settlement)
                else:
                    per_unit = exec_price / lev + exec_price * rate
                    avail = ledger.cash() + self._ext_cash
                    pos = ledger.perps.get(security)
                    if closing and pos is not None:
                        avail += ledger._release_preview(pos, closing, exec_price)
                afford = floor_to(max(ZERO, avail) / per_unit, step) if per_unit > 0 else ZERO
                if qty - closing > afford:
                    if req.target_kind == "percent_target":
                        qty = closing + afford
                        notes.append("按可用保证金（含手续费与滑点）折算数量")
                    elif self.cost.insufficient_funds == "reduce":
                        qty = closing + afford
                        partial = True
                        notes.append("可用保证金不足，按 insufficient_funds=reduce 以可负担数量成交")
                    else:
                        self.counters["insufficient_funds"] += 1
                        need = (qty - closing) * per_unit
                        return self._reject(
                            req,
                            f"保证金不足：开仓需要 {dstr(quantize(need, Decimal('0.01')))}（{dstr(lev)} "
                            "倍杠杆，含手续费），"
                            f"可用 {dstr(quantize(avail, Decimal('0.01')))}；默认拒绝",
                            code="INSUFFICIENT_MARGIN",
                            sig=sig,
                        )
        elif side == "buy":
            fee_in_quote = fee_ccy_mode == "quote"
            unit_cost = exec_price * (1 + rate) if fee_in_quote else exec_price
            afford = floor_to(max(ZERO, ledger.cash() + self._ext_cash) / unit_cost, step) if unit_cost > 0 else ZERO
            if qty > afford:
                if req.target_kind == "percent_target":
                    qty = afford
                    notes.append("按可用资金（含手续费与滑点）折算数量")
                elif self.cost.insufficient_funds == "reduce":
                    qty = afford
                    partial = True
                    notes.append("可用资金不足，按显式设置 insufficient_funds=reduce 以可负担数量成交")
                else:
                    self.counters["insufficient_funds"] += 1
                    need = qty * unit_cost
                    return self._reject(
                        req,
                        f"资金不足：需要 {dstr(quantize(need, Decimal('0.01')))}，可用 "
                        f"{dstr(quantize(ledger.cash(), Decimal('0.01')))}；默认拒绝"
                        "（可用 set_option('insufficient_funds','reduce') 改为缩量）",
                        code="INSUFFICIENT_FUNDS",
                        sig=sig,
                    )
        else:
            if qty > held:
                if (
                    req.target_kind in ("amount_target", "percent_target", "value_target")
                    or self.cost.insufficient_funds == "reduce"
                    or req.reason in trade_mgmt.EXIT_REASONS
                ):
                    qty = held
                else:
                    self.counters["insufficient_funds"] += 1
                    return self._reject(
                        req,
                        f"卖出 {dstr(qty)} 超过持仓 {dstr(held)}（现货不做空），已拒绝",
                        code="INSUFFICIENT_POSITION",
                        sig=sig,
                    )

        if qty <= 0:
            return self._reject(req, "可成交数量为 0", code="ZERO_QUANTITY", sig=sig)

        if self.fill_mode == "external":
            return self._route_external(
                req,
                sig=sig,
                side=side,
                qty=qty,
                ref_price=exec_price,
                rate=rate,
                bar=bar,
                liquidity=liquidity,
                note="；".join(notes),
                perp=perp,
                open_qty=max(ZERO, qty - closing),
                market_price=base_price,
                position_side=req.position_side,
            )

        base_ccy, quote_ccy = ([*security.split("/"), "USDT"])[:2]
        quote_ccy = quote_ccy.partition(":")[0]
        if perp:
            spec = (
                ledger.perp_book.spec(security)
                if ledger.perp_book is not None
                else spec_for(self.exchange, security, self.expiry_ts)
            )
            mult = dec(spec.contract_multiplier)
            notional = qty * mult if spec.contract_type == "inverse" else qty * mult * exec_price
            if spec.contract_type == "inverse":
                fee, fee_ccy = quantize(notional * rate / exec_price, FEE_STEP), spec.settlement.upper()
            else:
                fee, fee_ccy = quantize(notional * rate, FEE_STEP), spec.settlement.upper()
        else:
            notional = qty * exec_price
            if side == "buy" and fee_ccy_mode == "received":
                fee, fee_ccy = quantize(qty * rate, FEE_STEP), base_ccy.upper()
            else:
                fee, fee_ccy = quantize(notional * rate, FEE_STEP), quote_ccy.upper()

        self.order_seq += 1
        fill_id = (
            f"{sig.signal_id}:{sum(1 for f in self.fills[-50:] if f.signal_id == sig.signal_id) + 1}"
            if sig
            else f"{self.run_id}:{self.order_seq}"
        )
        ts = (
            bar.open_time
            if at_open
            else (bar.close_time if liquidity == "maker" or self.cost.fill_at == "close" else self.now)
        )
        try:
            if perp:
                spec = (
                    ledger.perp_book.spec(security)
                    if ledger.perp_book is not None
                    else spec_for(self.exchange, security, self.expiry_ts)
                )
                mult = dec(spec.contract_multiplier)
                quote_notional = qty * mult if spec.contract_type == "inverse" else qty * mult * exec_price
                tier = spec.tier_for_notional(float(quote_notional))
                booking = ledger.perp_trade(
                    instrument=security,
                    side=side,
                    qty=qty,
                    price=exec_price,
                    fee=fee,
                    ts=ts,
                    ref=fill_id,
                    mmr=tier["mmr"],
                    mmr_tier=tier["label"],
                    liquidation_fee_bps=spec.liquidation_fee_bps,
                    liquidation_slippage_bps=spec.liquidation_slippage_bps,
                    position_side=req.position_side,
                    leverage=ledger.leverage.get(security, Decimal(1)),
                )
            else:
                booking = ledger.trade(
                    instrument=security,
                    side=side,
                    qty=qty,
                    price=exec_price,
                    fee=fee,
                    fee_currency=fee_ccy,
                    ts=ts,
                    ref=fill_id,
                )
        except InsufficientFunds as exc:
            self.counters["insufficient_funds"] += 1
            return self._reject(req, str(exc), code="INSUFFICIENT_FUNDS", sig=sig)

        self._track_round_trip(
            security,
            side,
            ts,
            notional,
            booking,
            opened=(
                (qty - closing)
                * (
                    dec(ledger.perp_book.spec(security).contract_multiplier)
                    if ledger.perp_book and ledger.perp_book.spec(security).contract_type == "inverse"
                    else exec_price
                )
                if perp
                else None
            ),
            flipped=perp and closing > 0 and closing == abs(held) and qty > closing,
            reason=req.reason,
            position_side=req.position_side,
        )
        day = _day(bar.close_time)
        self.trades_per_day[day] = self.trades_per_day.get(day, 0) + 1
        note = "；".join(notes)
        fill = Fill(
            ts=ts,
            security=security,
            side=side,
            amount=qty,
            price=exec_price,
            fee=fee,
            reason=req.reason,
            partial=partial,
            note=note,
            realized_pnl=booking.realized,
            fee_currency=fee_ccy,
            fee_value=booking.fee_value,
            liquidity=liquidity,
            fill_id=fill_id,
            signal_id=sig.signal_id if sig else "",
            market_price=base_price,
            requested=floor_to(abs(delta), step),
            decided_at=sig.decision_ts if sig else self.now,
        )
        self.fills.append(fill)
        self._new_fills_for_callback.append(fill)
        self._set_status(sig, "partial" if partial else "filled", note)
        return OrderResult(
            ok=True,
            security=security,
            side=side,
            filled_amount=float(qty),
            filled_price=float(exec_price),
            fee=float(booking.fee_value),
            message=note,
        )

    # ------------------------------------------------------------------ 交易所模拟盘
    @staticmethod
    def client_order_id(ref: str) -> str:
        """确定性的客户端订单号：同一信号重放得到同一个号，交易所据此去重，重启不会重复下单。

        两家交易所对格式的要求取交集：只用字母数字、32 位以内（OKX clOrdId 不允许符号）。
        """
        import hashlib

        return "q" + hashlib.sha256(ref.encode()).hexdigest()[:31]

    def _route_external(
        self,
        req: OrderRequest,
        *,
        sig: Signal | None,
        side: str,
        qty: Decimal,
        ref_price: Decimal,
        rate: Decimal,
        bar: Bar,
        liquidity: str,
        note: str,
        perp: bool = False,
        open_qty: Decimal = ZERO,
        market_price: Decimal | None = None,
        position_side: str = "",
    ) -> OrderResult:
        held = self._held(req.security, position_side)
        if req.security in self.protects and held and (held > 0) != (side == "buy"):
            trade_mgmt.cancel_protect(self, req.security)  # 平台自己减仓 / 平仓：先撤交易所上的保护止损
        self.order_seq += 1
        ref = f"{sig.signal_id}:x{self.order_seq}" if sig else f"{self.run_id}:x{self.order_seq}"
        coid = self.client_order_id(ref)
        intent = {
            "client_order_id": coid,
            "ref": ref,
            "security": req.security,
            "side": side,
            "qty": dstr(qty),
            "ref_price": dstr(ref_price),  # 平台撮合模型给出的价格：和实际成交价对照就是“仿真偏差”
            "market_price": dstr(market_price if market_price is not None else ref_price),  # 送单时的市场参考价
            "signal_id": sig.signal_id if sig else "",
            "reason": req.reason,
            "decided_at": self.now,
            "bar_open_time": bar.open_time,
            "liquidity": liquidity,
            "note": note,
        }
        if perp:
            # 永续：opens = 是否新开仓（熔断时只许减仓），leverage 交给 OMS 在下单前设置到交易所
            intent.update(
                {
                    "perp": True,
                    "open_qty": dstr(open_qty),
                    "opens": open_qty > 0,
                    "leverage": dstr(self.ledger.leverage.get(req.security, Decimal(1))),
                    "position_side": position_side,
                    "margin_mode": self.margin_mode,
                    "position_mode": self.position_mode,
                    "contract_type": (
                        self.ledger.perp_book.spec(req.security).contract_type
                        if self.ledger.perp_book is not None
                        else self.contract_type
                    ),
                    "expiry_ts": (
                        self.ledger.perp_book.spec(req.security).expiry_ts
                        if self.ledger.perp_book is not None
                        else self.expiry_ts
                    ),
                }
            )
        else:
            intent["opens"] = side == "buy"
        self.outbox.append(intent)
        self.awaiting[coid] = intent
        self._reserve(intent, rate, sign=1)
        self._set_status(sig, "submitted", "已送交易所模拟盘，等待成交回报")
        return OrderResult(
            ok=True,
            security=req.security,
            side=side,
            filled_amount=0.0,
            filled_price=float(ref_price),
            message="已送交易所模拟盘，等待成交回报",
            status="pending",
        )

    def _reserve(self, intent: dict[str, Any], rate: Decimal, *, sign: int) -> None:
        qty, px = dec(intent["qty"]), dec(intent["ref_price"])
        sec = intent["security"]
        if intent.get("perp"):
            # 永续：只有新开仓部分占用保证金；平仓释放的保证金等回报到了再用（保守）
            open_q = dec(intent.get("open_qty") or 0)
            lev = dec(intent.get("leverage") or 1)
            spec = (
                self.ledger.perp_book.spec(sec)
                if self.ledger.perp_book is not None
                else spec_for(self.exchange, sec, self.expiry_ts)
            )
            mult = dec(spec.contract_multiplier)
            margin = open_q * mult / (px * lev) if spec.contract_type == "inverse" else open_q * mult * px / lev
            fee_quote = qty * mult * rate / px if spec.contract_type == "inverse" else qty * mult * px * rate
            self._ext_cash -= sign * (margin + fee_quote)
            self._ext_qty[sec] = self._ext_qty.get(sec, ZERO) + sign * (qty if intent["side"] == "buy" else -qty)
        elif intent["side"] == "buy":
            self._ext_cash -= sign * qty * px * (1 + rate)
            self._ext_qty[sec] = self._ext_qty.get(sec, ZERO) + sign * qty
        else:
            # 卖出的预期回款打 98 折再借给同一根 bar 的买单用：实际成交价更差时不至于超支
            self._ext_cash += sign * qty * px * (1 - rate) * Decimal("0.98")
            self._ext_qty[sec] = self._ext_qty.get(sec, ZERO) - sign * qty
        if not self.awaiting:
            self._ext_cash, self._ext_qty = ZERO, {}

    def _signal_by_id(self, signal_id: str) -> Signal | None:
        if not signal_id:
            return None
        for sig in reversed(self.signals):
            if sig.signal_id == signal_id:
                return sig
        return None

    def apply_external(self, report: dict[str, Any]) -> Fill | None:
        """记一条交易所回报。同一个 client_order_id 只记一次（重复回报直接忽略）。

        report = {client_order_id, status: filled|partial|rejected|canceled|expired,
                  filled_qty, avg_price, fee, fee_currency, ts, exchange_order_id, message}
        按交易所**实际**成交数量、均价和手续费记账；只成交一部分时未成交部分作废（市价单不会挂着）。
        """
        coid = report.get("client_order_id") or ""
        intent = self.awaiting.pop(coid, None)
        if intent is None:
            # 交易所上的保护止损被触发（平台可能停过机）：按交易所实际成交记账
            prot = next((p for p in self.protects.values() if p["client_order_id"] == coid), None)
            if prot is None and isinstance(report.get("protect"), dict) and coid.startswith("qp"):
                prot = report["protect"]  # 平台刚撤、交易所却已成交的那张（OMS 撤单前查到的）
            if prot is None or coid in self._booked_protects or report.get("status") not in ("filled", "partial"):
                return None
            if self.protects.get(prot["security"], {}).get("client_order_id") == coid:
                self.protects.pop(prot["security"], None)
            self._booked_protects.add(coid)
            self.log("warning", f"{prot['security']} 交易所上的保护止损单已成交（触发价 {prot['trigger']}）")
            intent = {
                "client_order_id": coid,
                "ref": f"protect:{coid}",
                "security": prot["security"],
                "side": prot["side"],
                "qty": prot["qty"],
                "ref_price": prot["trigger"],
                "market_price": prot["trigger"],
                "signal_id": "",
                "reason": "exchange_stop",
                "decided_at": int(report.get("ts") or self.now),
                "perp": prot.get("perp", False),
                "note": "",
            }
            rate = self._fee_rate(self.cost.taker_fee)
        else:
            rate = self._fee_rate(self.cost.taker_fee)
            self._reserve(intent, rate, sign=-1)
        sig = self._signal_by_id(intent.get("signal_id") or "")
        sec, side = intent["security"], intent["side"]
        filled = dec(report.get("filled_qty") or 0)
        if report.get("status") in ("rejected", "canceled", "expired") or filled <= 0:
            reason = report.get("message") or "交易所未成交"
            self.rejected.append(
                {
                    "ts": int(report.get("ts") or self.now),
                    "security": sec,
                    "side": side,
                    "reason": f"交易所模拟盘：{reason}",
                    "code": "EXCHANGE_REJECTED",
                    "signal_id": intent.get("signal_id") or "",
                }
            )
            self._set_status(sig, "rejected", f"交易所模拟盘：{reason}", "EXCHANGE_REJECTED")
            return None
        price = dec(report.get("avg_price") or intent["ref_price"])
        fee = dec(report.get("fee") or 0)
        fee_ccy = (report.get("fee_currency") or "").upper()
        base_ccy, quote_ccy = ([*sec.split("/"), "USDT"])[:2]
        notes = []
        if fee_ccy and fee_ccy not in (base_ccy.upper(), quote_ccy.upper()):
            notes.append(f"手续费以 {fee_ccy} 扣除（{dstr(fee)}），不在本账户币种内，未计入账本")
            fee, fee_ccy = ZERO, quote_ccy.upper()
        fee_ccy = fee_ccy or quote_ccy.upper()
        ts = int(report.get("ts") or self.now)
        partial = report.get("status") == "partial" or filled < dec(intent["qty"])
        if partial:
            notes.append(f"交易所只成交 {dstr(filled)} / {intent['qty']}，其余作废")
        fill_id = f"{intent['ref']}:{report.get('exchange_order_id') or coid}"
        perp = bool(intent.get("perp"))
        if self.ledger.perp_book is not None:
            before = self._held(sec, intent.get("position_side") or "")
        else:
            _pos = self.ledger.perps.get(sec)
            before = _pos.qty if _pos else ZERO
        try:
            if perp:
                spec = (
                    self.ledger.perp_book.spec(sec)
                    if self.ledger.perp_book is not None
                    else spec_for(self.exchange, sec, self.expiry_ts)
                )
                if fee_ccy and fee_ccy != spec.settlement.upper() and fee_ccy != quote_ccy.partition(":")[0].upper():
                    fee = ZERO
                mult = dec(spec.contract_multiplier)
                quote_notional = filled * mult if spec.contract_type == "inverse" else filled * mult * price
                tier = spec.tier_for_notional(float(quote_notional))
                booking = self.ledger.perp_trade(
                    instrument=sec,
                    side=side,
                    qty=filled,
                    price=price,
                    fee=fee,
                    ts=ts,
                    ref=fill_id,
                    mmr=tier["mmr"],
                    mmr_tier=tier["label"],
                    liquidation_fee_bps=spec.liquidation_fee_bps,
                    liquidation_slippage_bps=spec.liquidation_slippage_bps,
                    position_side=intent.get("position_side") or "",
                    leverage=self.ledger.leverage.get(sec, Decimal(1)),
                )
            else:
                booking = self.ledger.trade(
                    instrument=sec,
                    side=side,
                    qty=filled,
                    price=price,
                    fee=fee,
                    fee_currency=fee_ccy,
                    ts=ts,
                    ref=fill_id,
                )
        except InsufficientFunds as exc:
            # 交易所成交了，本地账本却不够——说明两边已经对不上，交给对账去报警
            self.counters["ledger_mismatch"] = self.counters.get("ledger_mismatch", 0) + 1
            self.rejected.append(
                {
                    "ts": ts,
                    "security": sec,
                    "side": side,
                    "code": "LEDGER_MISMATCH",
                    "reason": f"交易所已成交但本地账本记不进去：{exc}",
                    "signal_id": intent.get("signal_id") or "",
                }
            )
            self._set_status(sig, "rejected", str(exc), "LEDGER_MISMATCH")
            return None
        notional = filled * price
        opened, flipped = None, False
        if perp:
            closing = min(filled, abs(before)) if before and (before > 0) != (side == "buy") else ZERO
            opened = (filled - closing) * price
            flipped = closing > 0 and closing == abs(before) and filled > closing
        self._track_round_trip(
            sec,
            side,
            ts,
            notional,
            booking,
            opened=opened,
            flipped=flipped,
            reason=intent.get("reason") or "",
            position_side=intent.get("position_side") or "",
        )
        day = _day(ts)
        self.trades_per_day[day] = self.trades_per_day.get(day, 0) + 1
        fill = Fill(
            ts=ts,
            security=sec,
            side=side,
            amount=filled,
            price=price,
            fee=fee,
            reason=intent.get("reason") or "",
            partial=partial,
            note="；".join(n for n in [intent.get("note") or "", *notes] if n),
            realized_pnl=booking.realized,
            fee_currency=fee_ccy,
            fee_value=booking.fee_value,
            liquidity="taker",
            fill_id=fill_id,
            signal_id=intent.get("signal_id") or "",
            source="exchange_demo",
            market_price=dec(intent.get("market_price") or intent["ref_price"]),
            requested=dec(intent["qty"]),
            decided_at=int(intent.get("decided_at") or 0),
        )
        self.fills.append(fill)
        self._new_fills_for_callback.append(fill)
        self._set_status(sig, "partial" if partial else "filled", fill.note)
        return fill

    def _track_round_trip(
        self,
        security: str,
        side: str,
        ts: int,
        notional: Decimal,
        booking,
        opened: Decimal | None = None,
        flipped: bool = False,
        reason: str = "",
        position_side: str = "",
    ) -> None:
        book = self.ledger.perp_book
        if book is not None and is_derivative(security):
            target_side = position_side or ("long" if side == "buy" else "short")
            trip_key = f"{security}|{target_side}" if book.hedge else security
            trip = self._open_trips.get(trip_key)
            if trip is None:
                trip = RoundTrip(security=security, open_ts=ts, entry_tag=reason)
                self._open_trips[trip_key] = trip
            trip.realized += booking.realized
            trip.fees += booking.fee_value
            trip.fills += 1
            legs = [x for x in book.all_legs() if x.instrument.upper() == security.upper()]
            flat = not legs if not book.hedge else not any(x.side == target_side for x in legs)
            if flat or flipped:
                if not trip.cost_in:
                    trip.cost_in = notional
                    trip.entries = max(trip.entries, 1)
                trip.close_ts = ts
                trip.exit_reason = reason
                self.round_trips.append(trip)
                self._open_trips.pop(trip_key, None)
                if flipped:
                    self._open_trips[trip_key] = RoundTrip(
                        security=security, open_ts=ts, cost_in=opened, entry_tag=reason, entries=1
                    )
            elif opened:
                trip.cost_in += opened
                trip.entries += 1
            return
        if is_perp(security):
            # 永续：从空仓开到回到空仓（或反手）算一次完整交易；投入按开仓名义，收益含手续费与资金费
            trip = self._open_trips.get(security)
            pos = self.ledger.perps.get(security)
            if trip is None:
                trip = RoundTrip(security=security, open_ts=ts, entry_tag=reason)
                self._open_trips[security] = trip
            trip.realized += booking.realized
            trip.fees += booking.fee_value
            trip.fills += 1
            flat = pos is None or not pos.qty
            if flat or flipped:
                if not trip.cost_in:
                    trip.cost_in = notional
                    trip.entries = max(trip.entries, 1)
                trip.close_ts = ts
                trip.exit_reason = reason
                self.round_trips.append(trip)
                self._open_trips.pop(security, None)
                if flipped:
                    self._open_trips[security] = RoundTrip(
                        security=security, open_ts=ts, cost_in=opened, entry_tag=reason, entries=1
                    )
            elif opened:
                trip.cost_in += opened
                trip.entries += 1
            return
        trip = self._open_trips.get(security)
        if side == "buy":
            if trip is None:
                trip = RoundTrip(security=security, open_ts=ts, entry_tag=reason)
                self._open_trips[security] = trip
            trip.cost_in += notional + (booking.fee_value)
            trip.entries += 1
        elif trip is None:
            return
        else:
            trip.realized += booking.realized
        trip.fees += booking.fee_value
        trip.fills += 1
        if booking.closed:
            trip.close_ts = ts
            trip.exit_reason = reason
            self.round_trips.append(trip)
            self._open_trips.pop(security, None)

    def drain_new_fills(self) -> list[Fill]:
        out, self._new_fills_for_callback = self._new_fills_for_callback, []
        return out

    def mark(self, extra: dict[str, float] | None = None) -> None:
        prices = {s: self.price(s) for s in self.securities()}
        if extra:
            prices.update({k: v for k, v in extra.items() if v})
        self.ledger.mark({k: v for k, v in prices.items() if v})
        nav = self.unit_nav
        if nav > self.peak_nav:
            self.peak_nav = nav

    # ------------------------------------------------------------------ 检查点
    def snapshot(self) -> dict[str, Any]:
        """可 JSON 序列化的运行时状态。worker 每处理完一个事件就落一次检查点。"""

        def item_out(item: dict[str, Any]) -> dict[str, Any]:
            out = {k: v for k, v in item.items() if k not in ("req", "signal", "delta", "decision_price")}
            out["req"] = asdict(item["req"])
            out["signal"] = item["signal"].to_dict() if item.get("signal") else None
            if "delta" in item:
                out["delta"] = dstr(item["delta"])
            if item.get("decision_price") is not None:
                out["decision_price"] = dstr(item["decision_price"])
            return out

        return {
            "version": 2,
            "ledger": self.ledger.to_dict(),
            "index_ts": {s: (self.bars_by_security[s][i].close_time if i >= 0 else 0) for s, i in self._index.items()},
            "pending": [item_out(i) for i in self.pending],
            "open_orders": [item_out(i) for i in self.open_orders],
            "brackets": self.brackets,
            "signal_ids": sorted(self.signal_ids)[-20_000:],
            "event_seq": {k: v for k, v in list(self._event_seq.items())[-2_000:]},
            "open_trips": {k: t.to_dict() for k, t in self._open_trips.items()},
            "round_trips": [t.to_dict() for t in self.round_trips],
            "order_seq": self.order_seq,
            "last_event_ts": self.last_event_ts,
            "trades_per_day": dict(list(self.trades_per_day.items())[-30:]),
            "peak_nav": dstr(self.peak_nav),
            "units": dstr(self.units),
            "counters": self.counters,
            "cost": self.cost.to_dict(),
            "risk": self.risk.to_dict(),
            "fills_total": len(self.fills),
            "fill_mode": self.fill_mode,
            "awaiting": list(self.awaiting.values()),
            "ext_cash": dstr(self._ext_cash),
            "ext_qty": {k: dstr(v) for k, v in self._ext_qty.items()},
            "funding_ts": dict(self.funding_ts),
            "funding_marks": {k: {str(t): dstr(v) for t, v in rows.items()} for k, rows in self.funding_marks.items()},
            "trade_state": self.trade_state,
            "roi": {k: [list(r) for r in v] for k, v in self.roi.items()},
            "protects": self.protects,
            "protect_seq": self.protect_seq,
            "booked_protects": sorted(self._booked_protects)[-200:],
            "guards": self.guards.snapshot(),
            "ctx": {"current_dt": self.ctx.current_dt, "bar_index": self.ctx.bar_index, "options": self.ctx.options},
        }

    def restore(self, snap: dict[str, Any]) -> None:
        def sig_in(d: dict[str, Any] | None) -> Signal | None:
            if not d:
                return None
            t = TargetPosition(**d["target"])
            return Signal(**{**d, "target": t})

        def item_in(d: dict[str, Any]) -> dict[str, Any]:
            out = dict(d)
            out["req"] = OrderRequest(**d["req"])
            out["signal"] = sig_in(d.get("signal"))
            if d.get("delta") is not None:
                out["delta"] = dec(d["delta"])
            if d.get("decision_price") is not None:
                out["decision_price"] = dec(d["decision_price"])
            return out

        self.ledger = Ledger.from_dict(snap["ledger"])
        self.ctx._attach_portfolio(Portfolio(self.ledger))
        # 数据索引按“时间”恢复（不同进程加载的行情列表下标不同）；只会向前推进，不会倒退
        for k, ts in (snap.get("index_ts") or {}).items():
            cur = self.current_bar(k)
            if k in self._index and ts and (cur is None or cur.close_time < ts):
                bars = self.bars_by_security[k]
                idx = self._index[k]
                while idx + 1 < len(bars) and bars[idx + 1].close_time <= ts:
                    idx += 1
                self._index[k] = idx
        self.pending = [item_in(i) for i in snap.get("pending", [])]
        self.open_orders = [item_in(i) for i in snap.get("open_orders", [])]
        self.brackets = snap.get("brackets") or {}
        self.signal_ids = set(snap.get("signal_ids") or [])
        self._event_seq = dict(snap.get("event_seq") or {})
        self._open_trips = {k: RoundTrip.from_dict(v) for k, v in (snap.get("open_trips") or {}).items()}
        self.round_trips = [RoundTrip.from_dict(v) for v in snap.get("round_trips") or []]
        self.order_seq = int(snap.get("order_seq") or 0)
        self.last_event_ts = max(self.last_event_ts, int(snap.get("last_event_ts") or 0))
        self.trades_per_day = dict(snap.get("trades_per_day") or {})
        self.peak_nav = dec(snap.get("peak_nav") or 1)
        self.units = dec(snap.get("units") or self.ledger.equity())
        self.counters.update(snap.get("counters") or {})
        if snap.get("risk"):
            self.risk = RiskConfig.from_dict(snap["risk"])
        self.awaiting = {i["client_order_id"]: i for i in snap.get("awaiting") or []}
        self._ext_cash = dec(snap.get("ext_cash") or 0)
        self._ext_qty = {k: dec(v) for k, v in (snap.get("ext_qty") or {}).items()}
        self.funding_ts.update({k: int(v) for k, v in (snap.get("funding_ts") or {}).items()})
        self.funding_marks = {
            k: {int(t): dec(v) for t, v in rows.items()} for k, rows in (snap.get("funding_marks") or {}).items()
        }
        self.trade_state = dict(snap.get("trade_state") or {})
        if snap.get("roi"):
            self.roi = {k: [(int(m), float(r)) for m, r in v] for k, v in snap["roi"].items()}
        self.protects = dict(snap.get("protects") or {})
        self.protect_seq = int(snap.get("protect_seq") or 0)
        self._booked_protects = set(snap.get("booked_protects") or [])
        self.guards.restore(snap.get("guards"))
        c = snap.get("ctx") or {}
        self.ctx.current_dt = int(c.get("current_dt") or 0)
        self.ctx.bar_index = int(c.get("bar_index") or 0)
        self.ctx.options.update(c.get("options") or {})


# ---------------------------------------------------------------------------
# 本地跑一遍：把 StrategyRunner 与 BacktestRuntime 接起来
# ---------------------------------------------------------------------------
def _simple_rule_curve(bars: list[Any], initial_cash: float, *, fast: int = 20, slow: int = 60) -> list[float]:
    """简单 SMA 规则基线：快线在上持有，快线在下空仓；不计成本。"""
    closes: list[float] = []
    cash, qty = float(initial_cash), 0.0
    out: list[float] = []
    for i, bar in enumerate(bars):
        c = float(bar.close)
        closes.append(c)
        if i + 1 >= slow:
            f = sum(closes[-fast:]) / fast
            s = sum(closes[-slow:]) / slow
            if f > s and qty == 0 and c > 0:
                qty, cash = cash / c, 0.0
            elif f <= s and qty > 0:
                cash, qty = qty * c, 0.0
        out.append(cash + qty * c)
    return out


def _asof_curve(bars: list[Any], values: list[float], timestamps: list[int], default: float) -> list[float]:
    """把基准 bar 上的曲线按“截至当前时刻”映射到策略时间轴。"""
    if not bars or not values:
        return [default] * len(timestamps)
    out: list[float] = []
    j = 0
    last = float(values[0])
    for ts in timestamps:
        while j < len(bars) and int(bars[j].close_time) <= int(ts):
            last = float(values[j])
            j += 1
        out.append(last)
    return out


@dataclass
class LocalRunResult:
    """本地运行结果。字段与平台回测报告保持同名，便于对照。"""

    strategy: str
    initial_cash: float
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list[int] = field(default_factory=list)
    drawdown: list[float] = field(default_factory=list)
    benchmark_curve: list[float] = field(default_factory=list)
    simple_benchmark_curve: list[float] = field(default_factory=list)
    simple_benchmark: str = "SMA 20/60 多头/空仓"
    simple_benchmark_metrics: dict[str, Any] = field(default_factory=dict)
    unit_nav: list[float] = field(default_factory=list)
    benchmark: str = ""
    contract_specs: dict[str, Any] = field(default_factory=dict)
    fills: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    records: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cost_model: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    globals: dict[str, Any] = field(default_factory=dict)
    daily: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    round_trips: list[dict[str, Any]] = field(default_factory=list)
    open_positions: list[dict[str, Any]] = field(default_factory=list)
    flows: list[dict[str, Any]] = field(default_factory=list)
    ledger: dict[str, Any] = field(default_factory=dict)
    assumptions: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    securities: list[str] = field(default_factory=list)
    timeframe: str = "1h"
    unfilled: int = 0
    timed_out: bool = False

    def to_dict(self, *, max_points: int | None = 800, max_rows: int = 2000) -> dict[str, Any]:
        eq, ts, dd, bm, nav = self.equity_curve, self.timestamps, self.drawdown, self.benchmark_curve, self.unit_nav
        if max_points and len(eq) > max_points:
            step = max(1, len(eq) // max_points)
            idx = list(range(0, len(eq), step))
            if idx[-1] != len(eq) - 1:
                idx.append(len(eq) - 1)  # 终点必须保留，否则图上的期末净值与指标对不上
            eq = [eq[i] for i in idx]
            ts = [ts[i] for i in idx]
            dd = [dd[i] for i in idx] if dd else dd
            bm = [bm[i] for i in idx] if bm else bm
            nav = [nav[i] for i in idx] if nav else nav
        records = {
            k: (v[:: max(1, len(v) // (max_points or len(v) or 1))] if max_points else v)
            for k, v in self.records.items()
        }
        return {
            "strategy": self.strategy,
            "initial_cash": self.initial_cash,
            "equity_curve": eq,
            "timestamps": ts,
            "drawdown": dd,
            "benchmark_curve": bm,
            "simple_benchmark_curve": self.simple_benchmark_curve,
            "simple_benchmark": self.simple_benchmark,
            "simple_benchmark_metrics": self.simple_benchmark_metrics,
            "unit_nav": nav,
            "benchmark": self.benchmark,
            "contract_specs": self.contract_specs,
            "fills": self.fills[:max_rows],
            "fills_total": len(self.fills),
            "logs": self.logs[:max_rows],
            "logs_total": len(self.logs),
            "records": records,
            "metrics": self.metrics,
            "errors": self.errors,
            "cost_model": self.cost_model,
            "risk": self.risk,
            "portfolio": self.portfolio,
            "globals": _jsonable(self.globals),
            "daily": self.daily[-max_rows:],
            "rejected": self.rejected[:200],
            "signals": self.signals[-max_rows:],
            "signals_total": len(self.signals),
            "round_trips": self.round_trips[-max_rows:],
            "open_positions": self.open_positions,
            "flows": self.flows,
            "ledger": self.ledger,
            "assumptions": self.assumptions,
            "counters": self.counters,
            "params": self.params,
            "securities": self.securities,
            "timeframe": self.timeframe,
            "unfilled": self.unfilled,
            "timed_out": self.timed_out,
        }


def _jsonable(value: Any) -> Any:
    """g 里可能放了任意对象；输出前转成可序列化的形状。"""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value][:200]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    if isinstance(value, Decimal):
        return dstr(value)
    return repr(value)[:200]


def _day_key(ts: int) -> str:
    """bar 收盘时间所属的 UTC 日期。00:00 收盘的 bar 属于前一天。"""
    return _day(ts)


def run_backtest(
    strategy,
    bars_by_security: dict[str, list[Bar]],
    *,
    initial_cash: float = 10_000.0,
    initial_currency: str = "USDT",
    cost: RuntimeCost | None = None,
    risk: RiskConfig | None = None,
    warmup_bars: int = 0,
    timeframe: str = "1h",
    benchmark: str | None = None,
    time_limit: float | None = None,
    cash_flows: list[dict[str, Any]] | None = None,
    run_id: str = "local",
    strategy_version_id: str = "",
    risk_free_rate: float = 0.0,
    research: dict[str, Any] | None = None,
    delistings: list[dict[str, Any]] | None = None,
    cost_scale: float = 1.0,
    exchange: str = "binance",
    margin_mode: str = "isolated",
    position_mode: str = "one-way",
    contract_type: str = "linear",
    expiry_ts: int | None = None,
    ledger_v2: bool = False,
) -> LocalRunResult:
    """在本地把一份策略跑一遍，返回净值曲线、基准、日度统计与指标。

    预热语义：前 `warmup_bars` 根 bar 只推进数据索引，**不调用策略**。
    基准：同等初始资金买入持有（不计成本）。
    资金流 `cash_flows=[{"ts":..., "amount": +入金/-出金}]`：在 ts 之后第一根 bar 开始前生效，
    收益率按单位净值（时间加权）计算，不受资金流规模影响。
    回测结束时**不会**自动平仓：未平仓持仓按最后价格估值，列在 open_positions，
    不计入已完成交易与胜率。
    """
    from .runner import StrategyRunner  # 局部导入，避免模块级循环
    from .sandbox import StrategyTimeout, TimeGuard

    runtime = BacktestRuntime(
        bars_by_security,
        initial_cash=initial_cash,
        initial_currency=initial_currency,
        cost=cost,
        risk=risk,
        run_id=run_id,
        strategy_version_id=strategy_version_id,
        research=research,
        cost_scale=cost_scale,
        exchange=exchange,
        margin_mode=margin_mode,
        position_mode=position_mode,
        contract_type=contract_type,
        expiry_ts=expiry_ts,
        ledger_v2=ledger_v2,
    )
    ctx = runtime.ctx
    ctx.securities = list(bars_by_security)
    if getattr(strategy, "params", None):
        ctx.params = dict(strategy.params)
    result = LocalRunResult(
        strategy=strategy.name,
        initial_cash=initial_cash,
        cost_model=runtime.effective_cost(),
        params=dict(getattr(strategy, "params", {}) or {}),
        securities=list(bars_by_security),
        timeframe=timeframe,
    )
    guard = TimeGuard(getattr(strategy, "filename", "") or f"<strategy:{strategy.name}>", time_limit)

    timeline = sorted({b.close_time for bars in bars_by_security.values() for b in bars})
    if not timeline:
        result.errors.append("行情为空，无法回测")
        return result
    flows = sorted(cash_flows or [], key=lambda f: int(f["ts"]))
    flow_i = 0

    with guard:
        runner = StrategyRunner(strategy, runtime, ctx)
        runner.start()
        # 下架公告：一次给全，到各自的可见时刻才生效（标的池按当时能看到的信息重建）
        dl = delistings if delistings is not None else (research or {}).get("delistings")
        if dl:
            runtime.guards.set_delistings(list(dl))
        bench = benchmark or runtime.benchmark or (ctx.securities[0] if ctx.securities else "")
        if bench not in bars_by_security:
            bench = next(iter(bars_by_security))
        result.benchmark = bench
        result.cost_model = runtime.effective_cost()
        derivative_symbols = [sec for sec in bars_by_security if is_derivative(sec)]
        if derivative_symbols:
            result.contract_specs = {
                sec: (
                    runtime.ledger.perp_book.spec(sec)
                    if runtime.ledger.perp_book is not None
                    else spec_for(exchange, sec, expiry_ts)
                ).to_dict()
                for sec in derivative_symbols
            }

        scoring_start = min(warmup_bars, max(0, len(timeline) - 2))
        bench_p0 = 0.0
        day_rows: list[dict[str, Any]] = []
        current_day = ""
        initial_dec = runtime.ledger.equity()
        initial_equity_quote = float(initial_dec)

        try:
            for i, ts in enumerate(timeline):
                runtime.advance(ts)
                if i < scoring_start:
                    continue
                if not runtime.accept_event(ts):
                    continue
                if i == scoring_start:
                    first = runtime.current_bar(bench)
                    bench_p0 = first.open if first else runtime.price(bench)
                    result.equity_curve.append(initial_equity_quote)
                    result.unit_nav.append(1.0)
                    result.timestamps.append(timeline[i - 1] if i > 0 else ts)
                    result.benchmark_curve.append(initial_equity_quote)
                while flow_i < len(flows) and int(flows[flow_i]["ts"]) <= ts:
                    try:
                        runtime.apply_flow(flows[flow_i]["amount"], ts=ts, ref=str(flows[flow_i].get("ref") or ""))
                    except Exception as exc:
                        result.errors.append(f"资金流 {flows[flow_i]} 未执行：{exc}")
                    flow_i += 1
                ctx.is_warmup = False
                runtime.execute()
                runner.dispatch_trades(runtime.drain_new_fills())
                runner.on_bar(ts, is_first=(i == scoring_start), is_last=(i == len(timeline) - 1))
                if runtime.cost.fill_at == "close":
                    runner.dispatch_trades(runtime.drain_new_fills())
                runtime.mark()
                equity = float(runtime.ledger.equity())
                bench_value = (
                    initial_equity_quote * runtime.price(bench) / bench_p0 if bench_p0 else initial_equity_quote
                )
                result.equity_curve.append(equity)
                result.unit_nav.append(float(runtime.unit_nav))
                result.timestamps.append(ts)
                result.benchmark_curve.append(bench_value)

                day = _day_key(ts)
                port = ctx.portfolio
                row = {
                    "date": day,
                    "equity": equity,
                    "unit_nav": float(runtime.unit_nav),
                    "benchmark": bench_value,
                    "cash": port.available_cash,
                    "positions_value": port.positions_value,
                    "fees": port.total_fees,
                    "net_contributions": port.net_contributions,
                    "positions": [p.to_dict() for p in port.positions.values()],
                }
                if day != current_day:
                    day_rows.append(row)
                    current_day = day
                else:
                    day_rows[-1] = row
        except StrategyTimeout as exc:
            result.errors.append(str(exc))
            result.timed_out = True

    # 双基准：买入持有 + 简单 SMA 20/60 多头/空仓（不计成本，只用于回答“策略是否跑赢一个朴素规则”）
    bench_bars = bars_by_security.get(result.benchmark or bench) or []
    simple_curve = _simple_rule_curve(bench_bars, float(initial_cash))
    result.simple_benchmark_curve = _asof_curve(bench_bars, simple_curve, result.timestamps, float(initial_cash))
    simple_last = result.simple_benchmark_curve[-1] if result.simple_benchmark_curve else float(initial_cash)
    result.simple_benchmark_metrics = {
        "total_return": simple_last / float(initial_cash) - 1 if initial_cash else None,
        "label": result.simple_benchmark,
        "fast": 20,
        "slow": 60,
        "costs": "未计成本（朴素基线）",
    }
    result.unfilled = len(runtime.pending) + len(runtime.open_orders)
    if runtime.pending:
        runtime.log(
            "warning", f"回测结束时仍有 {len(runtime.pending)} 笔委托未成交：最后一根 bar 的信号没有下一根 bar 可以成交"
        )
    for item in runtime.pending + runtime.open_orders:
        runtime._set_status(item.get("signal"), "expired", "回测结束，没有后续 bar 可以成交", "END_OF_DATA")
    result.daily = _finish_daily(day_rows, initial_cash)
    result.fills = [f.to_dict() for f in runtime.fills]
    result.logs = runtime.logs
    result.records = runtime.records
    result.errors = list(dict.fromkeys([*result.errors, *runner.errors]))
    result.portfolio = ctx.portfolio.to_dict()
    result.globals = ctx._g.to_dict()
    result.rejected = runtime.rejected
    result.signals = [s.to_dict() for s in runtime.signals]
    result.round_trips = [t.to_dict() for t in runtime.round_trips]
    result.open_positions = [
        {**p.to_dict(), "note": "未平仓：按最后价格估值，不计入已完成交易"} for p in ctx.portfolio.positions.values()
    ]
    result.flows = runtime.flows_applied
    result.risk = runtime.risk.to_dict()
    result.counters = dict(runtime.counters)
    tb = runtime.ledger.trial_balance()
    result.ledger = {
        "trial_balance": {k: dstr(v) for k, v in tb.items()},
        "balanced": all(v == 0 for v in tb.values()),
        "entries": runtime.ledger.seq,
        "final_equity": dstr(runtime.ledger.equity()),
        "cash": dstr(runtime.ledger.cash()),
        "net_contributions": dstr(runtime.ledger.net_contributions),
        "fees_by_currency": {k: dstr(v) for k, v in runtime.ledger.fees_by_currency.items()},
        # 未折算手续费必须出现在报告里：只给折算后的总额，使用者会以为"手续费就这么多"，
        # 而实际上还有一笔以 BNB 计的费用没被计入——那是静默少算，比报错更危险。
        "unconverted_fees": runtime.ledger.unconverted_fees_report(),
        # 资金费单列（T03）：它是持仓成本，不是交易成本，混进手续费会让毛利对不上交易所账单。
        "funding": runtime.ledger.funding_report(),
        "perps": {k: p.to_dict() for k, p in runtime.ledger.perps.items()},
        "perp_book": runtime.ledger.perp_book.to_dict() if runtime.ledger.perp_book is not None else None,
        "liquidation_loss": dstr(runtime.ledger.liquidation_loss),
        "realized_pnl": dstr(runtime.ledger.realized_total()),
        "initial_equity": dstr(initial_dec),
    }

    try:
        from ..engine.metrics import (
            assumptions_block,
            compute_metrics,
            compute_relative_metrics,
            drawdown_details,
            drawdown_series,
            holding_stats,
            worst_period,
        )

        # 收益口径：有资金流时用单位净值（时间加权）；无资金流时两者等价
        basis = [v * initial_equity_quote for v in result.unit_nav] if runtime.flows_applied else result.equity_curve
        trade_returns = [t.ret for t in runtime.round_trips]
        turnover = (
            float(sum((f.notional for f in runtime.fills), Decimal(0)) / dec(initial_equity_quote))
            if initial_equity_quote
            else 0.0
        )
        metrics = compute_metrics(
            basis, timeframe=timeframe, trade_returns=trade_returns, turnover=turnover, risk_free_rate=risk_free_rate
        )
        metrics.trades = len(trade_returns)
        body = metrics.to_dict()
        body.update(compute_relative_metrics(basis, result.benchmark_curve, timeframe=timeframe, daily=result.daily))
        simple_ret = (result.simple_benchmark_metrics or {}).get("total_return")
        body["simple_benchmark_return"] = simple_ret
        tr = body.get("total_return")
        if tr is not None and simple_ret is not None and (1 + simple_ret):
            body["excess_vs_simple"] = (1 + tr) / (1 + simple_ret) - 1
            body["beats_simple"] = tr > simple_ret
        body["simple_benchmark"] = result.simple_benchmark
        body["fills"] = len(runtime.fills)
        body["total_fees"] = float(runtime.ledger.fees_value)
        # 折算后的总手续费之外，另列出未折算的部分与资金费——
        # 报告里看不到它们，使用者就会把"少算的手续费"当成真实成本。
        unconverted = runtime.ledger.unconverted_fees_report()
        body["unconverted_fees"] = unconverted
        body["has_unconverted_fees"] = unconverted["has_unconverted"]
        body["funding"] = runtime.ledger.funding_report()
        body["liquidations"] = int(runtime.counters.get("liquidations", 0))
        body["liquidation_loss"] = float(runtime.ledger.liquidation_loss)
        body["open_positions"] = len(result.open_positions)
        body["unrealized_pnl"] = sum(float(p.unrealized_pnl) for p in ctx.portfolio.positions.values())
        body["drawdown_detail"] = drawdown_details(basis, result.timestamps)
        body["worst_day"] = worst_period(result.daily)
        body.update(holding_stats(runtime.round_trips))
        body["breakdown"] = trade_mgmt.breakdown(runtime.round_trips)
        if runtime.guards.active:
            body["guards"] = runtime.guards.report()
        body["return_basis"] = "time_weighted_unit_nav" if runtime.flows_applied else "equity"
        result.metrics = body
        result.drawdown = drawdown_series(basis)
        result.assumptions = assumptions_block(
            timeframe=timeframe,
            risk_free_rate=risk_free_rate,
            cost=runtime.effective_cost(),
            flows=bool(runtime.flows_applied),
            warmup_bars=warmup_bars,
            perps=[s for s in bars_by_security if is_derivative(s)],
            funding_sources={
                s: (runtime.research.get(f"funding:{s}") or {}).get("source", "")
                for s in bars_by_security
                if is_derivative(s)
            },
            margin_mode=runtime.margin_mode,
            position_mode=runtime.position_mode,
            contract_type=(
                runtime.ledger.perp_book.contract_type
                if runtime.ledger.perp_book is not None
                else runtime.contract_type
            ),
            settlement_ccy=(
                runtime.ledger.perp_book.settlement_ccy
                if runtime.ledger.perp_book is not None
                else runtime.ledger.base_currency
            ),
            expiry_ts=(
                runtime.ledger.perp_book.expiry_ts if runtime.ledger.perp_book is not None else runtime.expiry_ts
            ),
            liquidation_loss=float(runtime.ledger.liquidation_loss),
        )
    except ImportError:  # pragma: no cover - SDK 独立安装时走这条
        result.metrics = {"total_return": ctx.portfolio.total_return, "trades": len(runtime.round_trips)}
        result.drawdown = []
    return result


def _finish_daily(rows: list[dict[str, Any]], initial_cash: float) -> list[dict[str, Any]]:
    """补上日收益（按单位净值，资金流不影响）、基准日收益与累计收益。"""
    out: list[dict[str, Any]] = []
    prev_nav = 1.0
    prev_bm = initial_cash
    for row in rows:
        nav, bm = row.get("unit_nav", row["equity"] / initial_cash if initial_cash else 1.0), row["benchmark"]
        out.append(
            {
                **row,
                "equity": round(row["equity"], 4),
                "benchmark": round(bm, 4),
                "cash": round(row["cash"], 4),
                "positions_value": round(row["positions_value"], 4),
                "fees": round(row["fees"], 6),
                "return": (nav / prev_nav - 1) if prev_nav else 0.0,
                "benchmark_return": (bm / prev_bm - 1) if prev_bm else 0.0,
                "cum_return": nav - 1,
            }
        )
        prev_nav, prev_bm = nav, bm
    return out


def _trade_returns(fills: list[Fill]) -> list[float]:
    """兼容旧调用：从成交序列折算“完整交易”（卖回空仓）的收益率 = 已实现盈亏 / 投入成本。未平仓部分不计入。"""
    out: list[float] = []
    qty = basis = cost_in = realized = ZERO
    for f in fills:
        if f.side == "buy":
            qty += f.amount
            basis += f.amount * f.price + f.fee_value
            cost_in += f.amount * f.price + f.fee_value
        elif qty > 0:
            sell = min(f.amount, qty)
            released = basis if sell == qty else basis * sell / qty
            realized += sell * f.price - f.fee_value - released
            basis -= released
            qty -= sell
            if qty <= 0:
                out.append(float(realized / cost_in) if cost_in else 0.0)
                qty = basis = cost_in = realized = ZERO
    return out
