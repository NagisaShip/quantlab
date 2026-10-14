"""T03 · 资金费率与永续合约（只回测）。

验收标准（docs/VibeCoding任务包.md T03）：
> 持有 1 张多单跨过 3 个结算点、费率 0.01% → 账本出现 3 笔资金费分录、试算平衡为 0。

同时验证简化模型在口径声明里写明（不声明就等于假装完整）。
"""

from __future__ import annotations

from decimal import Decimal

from quant_agent.engine.metrics import assumptions_block
from quant_agent.exchanges.base import FundingRate, UnsupportedFeature
from quant_agent.sdk.ledger import FUNDING, Ledger, dec, dstr

DAY = 86_400_000
EIGHT_H = 8 * 3_600_000
PERP = "BTC/USDT:PERP"


def _ledger_with_long(qty="1", price="60000", cash="1000000"):
    """建仓：买入 qty 张永续多单。"""
    led = Ledger(base_currency="USDT")
    led.deposit(dec(cash), ref="init")
    led.trade(instrument=PERP, side="buy", qty=dec(qty), price=dec(price), ref="open")
    return led


# ---------------------------------------------------------------- 黄金样例
def test_three_settlements_produce_three_entries_and_balance():
    """黄金样例：1 张多单跨 3 个结算点、费率 0.01% → 3 笔资金费分录、试算平衡为 0。"""
    led = _ledger_with_long()
    start = 1_700_000_000_000
    for i in range(3):
        led.funding(
            instrument=PERP,
            qty=dec("1"),
            rate=dec("0.0001"),  # 0.01%
            mark_price=dec("60000"),
            ts=start + (i + 1) * EIGHT_H,
            ref=f"funding-{i}",
            side="long",
        )

    funding_entries = [e for e in led.entries if e.kind == "funding"]
    assert len(funding_entries) == 3, f"应有 3 笔资金费分录，实际 {len(funding_entries)}"

    # 试算平衡必须恒为 0
    tb = led.trial_balance()
    assert all(v == 0 for v in tb.values()), {k: dstr(v) for k, v in tb.items()}

    # 每笔 = 1 × 60000 × 0.0001 = 6 USDT；多头正费率是付出
    assert dstr(led.funding_paid) == "18", dstr(led.funding_paid)
    assert dstr(led.funding_received) == "0"
    assert led.funding_events == 3
    assert led.funding_report()["net"] == "-18"

    # 资金费记在 expenses:funding，而不是 expenses:fees
    assert led.balance(FUNDING, "USDT") == dec("18")
    assert led.balance("expenses:fees", "USDT") == dec("0"), "资金费不得混进交易手续费"

    # 现金：初始 1000000 − 建仓 60000 − 资金费 18
    assert dstr(led.cash("USDT")) == "939982", dstr(led.cash("USDT"))


def test_funding_does_not_change_cost_basis():
    """资金费是持仓成本而非建仓成本：不得改变持仓成本，否则已实现盈亏会偏离交易所口径。"""
    led = _ledger_with_long()
    before = led.holdings[PERP].cost
    led.funding(instrument=PERP, qty=dec("1"), rate=dec("0.0001"), mark_price=dec("60000"), side="long")
    assert led.holdings[PERP].cost == before, "资金费不得进持仓成本"


# ---------------------------------------------------------------- 方向与符号
def test_short_receives_when_rate_positive():
    """正费率下空头收钱，多头付钱——符号反了会让策略盈亏整体反向。"""
    led = Ledger(base_currency="USDT")
    led.deposit(dec("1000000"), ref="init")
    led.funding(instrument=PERP, qty=dec("2"), rate=dec("0.0001"), mark_price=dec("50000"), side="short")
    # 2 × 50000 × 0.0001 = 10，空头收取
    assert dstr(led.funding_received) == "10", dstr(led.funding_received)
    assert dstr(led.cash("USDT")) == "1000010"


def test_negative_rate_flips_direction():
    """负费率时空头付、多头收。"""
    led = _ledger_with_long()
    led.funding(instrument=PERP, qty=dec("1"), rate=dec("-0.0001"), mark_price=dec("60000"), side="long")
    assert dstr(led.funding_received) == "6", dstr(led.funding_received)
    assert dstr(led.cash("USDT")) == "940006"


def test_funding_uses_mark_price_not_entry_price():
    """名义价值随价格变：结算用结算点的标记价，不是开仓价。"""
    led = _ledger_with_long(price="60000")
    led.funding(instrument=PERP, qty=dec("1"), rate=dec("0.0001"), mark_price=dec("90000"), side="long")
    assert dstr(led.funding_paid) == "9", f"应按 90000 结算，实际 {dstr(led.funding_paid)}"


def test_zero_position_and_zero_rate_still_record_an_entry():
    """空仓或零费率仍记一笔零额分录：否则"跨过 N 个结算点"在账本里数不出来。"""
    led = Ledger(base_currency="USDT")
    led.deposit(dec("1000"), ref="init")
    led.funding(instrument=PERP, qty=dec("0"), rate=dec("0.0001"), mark_price=dec("60000"), side="long")
    led.funding(instrument=PERP, qty=dec("1"), rate=dec("0"), mark_price=dec("60000"), side="long")
    assert len([e for e in led.entries if e.kind == "funding"]) == 2
    assert all(v == 0 for v in led.trial_balance().values())
    assert dstr(led.cash("USDT")) == "1000", "零额分录不得改变现金"


def test_funding_rejects_bad_input():
    led = Ledger(base_currency="USDT")
    led.deposit(dec("1000"), ref="init")
    for kwargs in (
        {"qty": "-1", "rate": "0.0001", "mark_price": "60000"},
        {"qty": "1", "rate": "0.0001", "mark_price": "0"},  # 标记价必须为正
        {"qty": "1", "rate": "0.0001", "mark_price": "60000", "side": "sideways"},
    ):
        try:
            led.funding(instrument=PERP, **kwargs)
        except Exception:
            continue
        raise AssertionError(f"非法输入必须被拒绝：{kwargs}")


# ---------------------------------------------------------------- 精度
def test_funding_is_decimal_exact():
    """逐位精确：资金费不能出现二进制尾差。"""
    led = _ledger_with_long()
    led.funding(instrument=PERP, qty=dec("0.1"), rate=dec("0.0003"), mark_price=dec("33333.33"), side="long")
    # 0.1 × 33333.33 × 0.0003 = 0.9999999
    assert dstr(led.funding_paid) == "0.9999999", dstr(led.funding_paid)
    assert isinstance(led.funding_paid, Decimal)


def test_funding_report_shape_is_decimal_strings():
    led = _ledger_with_long()
    led.funding(instrument=PERP, qty=dec("1"), rate=dec("0.0001"), mark_price=dec("60000"), side="long")
    rep = led.funding_report()
    for k in ("paid", "received", "net"):
        assert isinstance(rep[k], str), (k, type(rep[k]))
    assert rep["by_instrument"] == {PERP: "-6"}


# ---------------------------------------------------------------- 快照往返
def test_funding_survives_snapshot_round_trip():
    """重启一致：资金费累计必须随账本快照一起恢复，否则重启后用量/盈亏会对不上。"""
    led = _ledger_with_long()
    led.funding(instrument=PERP, qty=dec("1"), rate=dec("0.0001"), mark_price=dec("60000"), side="long")
    restored = Ledger.from_dict(led.to_dict())
    assert restored.funding_events == led.funding_events
    assert restored.funding_paid == led.funding_paid
    assert restored.funding_by_instrument == led.funding_by_instrument
    assert all(v == 0 for v in restored.trial_balance().values())


# ---------------------------------------------------------------- 事件与方法
def test_funding_rate_event_has_audit_fields():
    fr = FundingRate(symbol=PERP, ts=1_700_000_000_000, rate="0.0001", mark_price="60000", source="binance")
    assert fr.event_ts == fr.ts
    a = fr.audit()
    for k in ("event_ts", "receive_ts", "sequence", "source", "instrument_id", "raw_ref"):
        assert k in a, k
    assert a["instrument_id"] == PERP
    assert fr.to_dict()["rate"] == "0.0001"


def test_spot_adapter_rejects_funding_rates():
    """现货没有资金费：必须显式拒绝，不能返回空列表被误读成"费率一直是 0"。"""

    class Spot:
        name = "spot"

    # 协议默认实现挂在 Protocol 上，用 BaseAdapter 的实例验证真实适配器的行为
    from quant_agent.exchanges.base import BaseAdapter

    adapter = BaseAdapter()
    try:
        adapter.fetch_funding_rates(PERP, 0, DAY)
    except (UnsupportedFeature, NotImplementedError):
        return
    raise AssertionError("现货适配器必须显式拒绝资金费率查询")


# ---------------------------------------------------------------- 口径声明
def test_assumptions_declare_perpetual_simplifications():
    """简化模型必须在口径声明里写明——不写就等于假装完整。"""
    block = assumptions_block(timeframe="1h")
    perp = block["perpetual"]
    assert perp["supported"] is True
    assert perp["funding_interval_hours"] == 8
    assert perp["funding_affects_cost_basis"] is False
    assert "逐仓" in perp["margin_model"]
    assert "保守" in perp["liquidation_model"]
    assert "标记价" in perp["mark_price"]
    assert perp["limitations"], "必须写明简化范围"
    assert "expenses:funding" in perp["funding_account"]
