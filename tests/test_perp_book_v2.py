from decimal import Decimal

from quant_agent.exchanges.contracts import contract_type_of, spec_for
from quant_agent.sdk.ledger import Ledger
from quant_agent.sdk.perp_book import PerpBook

PERP = "BTC/USDT:PERP"
INVERSE = "BTC/USD:INVERSE"
DELIVERY = "BTC/USDT:DELIVERY"


def test_isolated_linear_fill_mark_and_close():
    led = Ledger()
    led.deposit(10_000)
    book = PerpBook(exchange="binance", contract_type="linear")
    led.perp_book = book
    book.leverage[PERP] = Decimal(10)
    fill = book.trade(led, instrument=PERP, side="buy", qty=1, price=100, fee="0.1")
    leg = book.all_legs()[0]
    assert fill.opened_qty == Decimal(1)
    assert leg.margin == Decimal(10)
    assert led.cash() == Decimal("9989.9")
    assert book.equity(led, {PERP: 100}) == Decimal("9999.9")

    book.mark({PERP: 110})
    assert leg.unrealized(book.spec(PERP)) == Decimal(10)
    assert book.equity(led, {PERP: 110}) == Decimal("10009.9")

    fill = book.trade(led, instrument=PERP, side="sell", qty=1, price=110, fee="0.11")
    assert fill.closed_qty == Decimal(1)
    assert fill.realized == Decimal("9.89")
    assert not book.all_legs()
    assert led.cash() == Decimal("10009.79")
    assert led.realized_total() == Decimal("9.89")
    assert all(v == 0 for v in led.trial_balance().values())


def test_hedge_has_independent_long_and_short_legs_and_attribution():
    led = Ledger()
    led.deposit(10_000)
    book = PerpBook(exchange="binance", position_mode="hedge")
    led.perp_book = book
    book.leverage[PERP] = Decimal(5)
    book.trade(led, instrument=PERP, side="buy", qty=1, price=100)
    book.trade(led, instrument=PERP, side="sell", qty=2, price=100)
    assert len(book.all_legs()) == 2
    long_leg = next(x for x in book.all_legs() if x.side == "long")
    short_leg = next(x for x in book.all_legs() if x.side == "short")
    assert long_leg.qty == Decimal(1)
    assert short_leg.qty == Decimal(2)

    book.trade(led, instrument=PERP, side="buy", qty=1, price=100, position_side="short")
    assert short_leg.qty == Decimal(1)
    assert long_leg.qty == Decimal(1)
    assert all(v == 0 for v in led.trial_balance().values())


def test_cross_margin_uses_account_equity_and_atomic_liquidation():
    led = Ledger()
    led.deposit(10_000)
    book = PerpBook(exchange="binance", margin_mode="cross")
    led.perp_book = book
    book.leverage[PERP] = Decimal(10)
    book.trade(led, instrument=PERP, side="buy", qty=900, price=100)
    assert book.equity(led, {PERP: 100}) == Decimal(10_000)
    assert not book.liquidatable(led, {PERP: 90})
    assert book.liquidatable(led, {PERP: 89})

    result = book.liquidate_cross_all(led, {PERP: 89}, ts=1, reason="测试联合强平")
    assert result.ok and result.legs == 1
    assert result.seized > 0
    assert not book.all_legs()
    assert led.equity() == 0
    assert book.liquidation_loss > 0
    assert all(v == 0 for v in led.trial_balance().values())


def test_inverse_uses_base_coin_margin_pnl_funding_and_equity():
    led = Ledger()
    led.deposit(1, currency="BTC")
    book = PerpBook(exchange="binance", contract_type="inverse")
    led.perp_book = book
    book.leverage[INVERSE] = Decimal(10)
    fill = book.trade(led, instrument=INVERSE, side="buy", qty=1, price=50_000)
    leg = book.all_legs()[0]
    assert fill.settlement_ccy == "BTC"
    assert leg.margin == Decimal("0.0002")
    assert led.cash("USDT") == 0
    assert led.cash("BTC") == Decimal("0.9998")
    assert book.equity(led, {INVERSE: 50_000}) == Decimal(50_000)

    assert book.equity(led, {INVERSE: 55_000}).quantize(Decimal("0.01")) == Decimal("55010.00")
    funding = book.settle_funding(led, INVERSE, rate="0.0001", mark_price=55_000, ts=1, ref="f1")
    assert funding < 0
    assert led.cash("BTC") < Decimal("0.9998")
    assert book.funding_details[-1]["settlement_ccy"] == "BTC"

    # Closing must move BTC, never the USDT account.
    book.trade(led, instrument=INVERSE, side="sell", qty=1, price=55_000)
    assert led.cash("USDT") == 0
    assert led.cash("BTC") > Decimal("1.0001")
    assert all(v == 0 for v in led.trial_balance().values())


def test_inverse_liquidation_price_and_fill_are_currency_consistent():
    led = Ledger()
    led.deposit(1, currency="BTC")
    book = PerpBook(exchange="binance", contract_type="inverse")
    led.perp_book = book
    book.leverage[INVERSE] = Decimal(10)
    book.trade(led, instrument=INVERSE, side="buy", qty=1, price=50_000)
    leg = book.all_legs()[0]
    liq = leg.liquidation_price(book.spec(INVERSE))
    # 10x inverse long: P = entry*(1+mmr)/(1+1/L), with BTC tier MMR 0.004
    assert liq.quantize(Decimal("0.01")) == Decimal("45636.36")
    assert book.liquidatable(led, {INVERSE: 45_000})
    result = book.liquidate(led, leg, price=45_000, ts=1, reason="inverse test")
    assert result.ok
    assert led.cash("USDT") == 0
    assert all(v == 0 for v in led.trial_balance().values())


def test_delivery_expiry_settles_once_and_serialization_keeps_spec():
    led = Ledger()
    led.deposit(10_000)
    book = PerpBook(exchange="binance", contract_type="delivery", expiry_ts=1_000)
    led.perp_book = book
    spec = book.spec(DELIVERY)
    assert spec.contract_type == "delivery"
    assert spec.expiry_ts == 1_000
    book.trade(led, instrument=DELIVERY, side="buy", qty=1, price=100)
    assert book.settle_expiry(led, 110, ts=999) == 0
    pnl = book.settle_expiry(led, 110, ts=1_000)
    assert pnl == Decimal(10)
    assert book.expired and not book.all_legs()

    restored = PerpBook.from_dict(book.to_dict())
    assert restored.exchange == "binance"
    assert restored.expiry_ts == 1_000
    assert restored.spec(DELIVERY).contract_type == "delivery"
    assert restored.spec(DELIVERY).contract_multiplier == spec.contract_multiplier


def test_contract_type_detection_and_exchange_face_values():
    assert contract_type_of(PERP) == "linear"
    assert contract_type_of(INVERSE) == "inverse"
    assert contract_type_of(DELIVERY) == "delivery"
    assert contract_type_of("BTC/USDT") == "spot"
    assert spec_for("binance", INVERSE).contract_multiplier == 100
    assert spec_for("bybit", INVERSE).contract_multiplier == 1


def test_runtime_inverse_uses_btc_cash_and_contract_multiplier():
    from test_perpetual import ZERO_COST, _bars  # type: ignore

    from quant_agent.sdk import load_source
    from quant_agent.sdk.runtimes import run_backtest

    INVERSE = "BTC/USD:INVERSE"

    mod = load_source(
        """
def initialize(ctx):
    set_leverage("BTC/USD:INVERSE", 10)
    g.sent = False

def handle_data(ctx):
    if not g.sent:
        g.sent = True
        order_target_percent("BTC/USD:INVERSE", 1.0, position_side="long")
""",
        name="inverse_runtime",
    )
    bars = _bars(INVERSE, [(50_000, 50_000, 50_000, 50_000), (55_000, 55_000, 55_000, 55_000)])
    r = run_backtest(
        mod,
        {INVERSE: bars},
        initial_cash=1,
        initial_currency="BTC",
        cost=ZERO_COST,
        contract_type="inverse",
        ledger_v2=True,
        exchange="binance",
    )
    assert not r.errors
    assert r.ledger["balanced"]
    assert r.fills
    assert r.fills[0]["fee_currency"] == "BTC"
    assert r.ledger["cash"] == "0"


def test_runtime_hedge_creates_two_attributed_legs():
    from test_perpetual import PERP, ZERO_COST, _bars  # type: ignore

    from quant_agent.sdk import load_source
    from quant_agent.sdk.runtimes import run_backtest

    mod = load_source(
        """
def initialize(ctx):
    set_leverage("BTC/USDT:PERP", 5)
    g.sent = False

def handle_data(ctx):
    if not g.sent:
        g.sent = True
        order_target_percent("BTC/USDT:PERP", 0.2, position_side="long")
        order_target_percent("BTC/USDT:PERP", 0.2, position_side="short")
""",
        name="hedge_runtime",
    )
    bars = _bars(PERP, [(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 100, 100)])
    r = run_backtest(
        mod,
        {PERP: bars},
        initial_cash=10_000,
        cost=ZERO_COST,
        position_mode="hedge",
        ledger_v2=True,
    )
    assert not r.errors
    assert r.ledger["balanced"]
    assert len(r.fills) == 2
    assert any("|LONG" in k for k in r.portfolio["positions"])
    assert any("|SHORT" in k for k in r.portfolio["positions"])


def test_runtime_cross_joint_liquidation_is_one_account_event():
    from test_perpetual import PERP, ZERO_COST, _bars  # type: ignore

    from quant_agent.sdk import load_source
    from quant_agent.sdk.runtimes import RiskConfig, run_backtest

    mod = load_source(
        """
def initialize(ctx):
    set_leverage("BTC/USDT:PERP", 10)
    g.sent = False

def handle_data(ctx):
    if not g.sent:
        g.sent = True
        order_target_percent("BTC/USDT:PERP", 9.0)
""",
        name="cross_runtime",
    )
    bars = _bars(PERP, [(100, 100, 100, 100), (100, 100, 100, 100), (89, 89, 89, 89), (89, 89, 89, 89)])
    r = run_backtest(
        mod,
        {PERP: bars},
        initial_cash=10_000,
        cost=ZERO_COST,
        margin_mode="cross",
        ledger_v2=True,
        risk=RiskConfig(max_position_weight=10, max_gross_exposure=10),
    )
    assert not r.errors
    assert r.counters.get("liquidations") == 1
    assert r.ledger["balanced"]
    assert r.ledger["final_equity"] == "0"
    assert r.portfolio["positions"] == {}
