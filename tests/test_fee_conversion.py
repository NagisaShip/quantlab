"""多币种手续费折算的黄金样例（T02）。

口径（docs/策略协议与数据口径.md 第 1、7、8 节）：

- 手续费可以用**第三币种**支付（币安常见的 BNB 抵扣）。记账仍按实际扣费币种过账，
  每个币种借贷恒等；
- 折算到计价币需要一笔**成交时刻的汇率**，由调用方显式给出并说明来源
  （来源写进成交的 `raw_ref` / 分录 memo，事后可复核）；
- **拿不到汇率时不估算**：不按 1:1、不忽略、不猜。该笔费用记为"未折算"，
  在报告的 `unconverted_fees` 里单列，且**不**计入折算后的总手续费。

黄金样例：买 1 BTC @60000，手续费 0.01 BNB @600 USDT/BNB → 总手续费 "6" USDT。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quant_agent.engine.metrics import fee_summary
from quant_agent.live.attribution import attribute, live_result
from quant_agent.sdk.ledger import Ledger, LedgerError, dstr

BNB_SOURCE = "binance:BNBUSDT@1791850800000"


def _balanced(led: Ledger) -> bool:
    """每个币种所有账户余额之和为 0（复式记账的硬约束）。"""
    return all(v == 0 for v in led.trial_balance().values())


# ---------------------------------------------------------------- 黄金样例：0.01 BNB @ 600
def test_golden_third_currency_fee_converts_at_given_rate():
    """买 1 BTC @60000，手续费 0.01 BNB @600 USDT/BNB → 总手续费 "6" USDT。"""
    led = Ledger()
    led.deposit(100_000)
    b = led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
        ts=1_791_850_800_000,
        ref="fill-1",
    )
    # 0.01 × 600 = 6，逐位精确
    assert b.fee_value == Decimal("6")
    assert led.fees_value == Decimal("6")
    # 对外一律十进制字符串，且去掉无意义尾零
    assert dstr(led.fees_value) == "6"
    assert isinstance(dstr(led.fees_value), str)
    assert led.fees_by_currency == {"BNB": Decimal("0.01")}
    assert b.fee_converted is True
    assert b.fee_rate == Decimal("600")
    assert b.fee_rate_source == BNB_SOURCE
    # 现金只被成交额影响：手续费是 BNB 扣的，USDT 现金不该被多扣
    assert led.cash() == Decimal("40000")
    assert _balanced(led)


def test_golden_output_is_string_not_number():
    """输出必须是字符串 "6"，不能是数字 6、也不能是 6.0。"""
    led = Ledger()
    led.deposit(100_000)
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
    )
    out = dstr(led.fees_value)
    assert out == "6"
    assert out != "6.0"
    assert not isinstance(out, (int, float))
    assert type(out) is str


def test_rate_source_is_recorded_on_the_entry_for_audit():
    """汇率来源必须落到分录上，事后能复核 "6 是怎么来的"。"""
    led = Ledger()
    led.deposit(100_000)
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
        ref="fill-1",
    )
    entry = led.entries[-1]
    assert entry.ref == "fill-1"
    assert BNB_SOURCE in entry.memo
    assert "0.01 BNB" in entry.memo
    assert "6 USDT" in entry.memo


# ---------------------------------------------------------------- 三币种分录平衡
def test_three_currency_postings_stay_balanced():
    """BTC/USDT 成交 + BNB 手续费：USDT、BTC、BNB 三个币种各自借贷恒等。"""
    led = Ledger()
    led.deposit(100_000)
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
        ts=1,
        ref="b1",
    )
    tb = led.trial_balance()
    assert set(tb) == {"USDT", "BTC", "BNB"}
    assert all(v == 0 for v in tb.values()), {k: dstr(v) for k, v in tb.items()}
    # 手续费记在实际扣费币种的费用账户上
    assert led.balance("expenses:fees", "BNB") == Decimal("0.01")
    assert led.balance("assets", "BNB") == Decimal("-0.01")
    assert led.balance("assets", "BTC") == Decimal("1")
    assert led.balance("assets", "USDT") == Decimal("40000")


def test_third_currency_fee_enters_holding_cost():
    """买入成本含折算后的手续费：1×60000 + 6 = 60006。"""
    led = Ledger()
    led.deposit(100_000)
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
    )
    h = led.holdings["BTC/USDT"]
    assert h.cost == Decimal("60006")
    assert h.entry_notional == Decimal("60000")  # 入场均价不含手续费
    assert h.avg_entry == Decimal("60000")


def test_third_currency_fee_on_sell_does_not_touch_quote_cash():
    """卖出的 BNB 手续费只扣 BNB，不能从 USDT 现金里再扣一次。"""
    led = Ledger()
    led.deposit(100_000)
    led.trade(instrument="BTC/USDT", side="buy", qty=1, price=60_000, ts=1, ref="b")
    b = led.trade(
        instrument="BTC/USDT",
        side="sell",
        qty=1,
        price=61_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
        ts=2,
        ref="s",
    )
    # 现金 = 100000 − 60000 + 61000，BNB 手续费不从这里扣
    assert led.cash() == Decimal("101000")
    # 已实现 = 61000 − 6(折算手续费) − 60000(成本)
    assert b.realized == Decimal("994")
    assert led.fees_value == Decimal("6")
    assert _balanced(led)


# ---------------------------------------------------------------- 无汇率：不估算
def test_missing_rate_is_marked_unconverted_and_excluded_from_total():
    """拿不到汇率时明确标注"未折算"，且**不**计入总手续费。"""
    led = Ledger()
    led.deposit(100_000)
    b = led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",  # 没有 fee_rate
        ts=1,
        ref="fill-1",
    )
    # 不估算：折算值必须是 0，而不是 0.01（1:1）或任何猜出来的数
    assert b.fee_value == Decimal("0")
    assert b.fee_converted is False
    assert b.fee_rate is None
    assert led.fees_value == Decimal("0")
    # 但费用本身没被丢掉：按币种单列
    assert led.unconverted_fees == {"BNB": Decimal("0.01")}
    rep = led.unconverted_fees_report()
    assert rep["has_unconverted"] is True
    assert rep["by_currency"] == {"BNB": "0.01"}
    assert rep["converted"] == "0"
    assert rep["count"] == 1
    assert "未折算" in rep["note"]
    # 分录仍然平衡，费用仍记在 BNB 上
    assert led.balance("expenses:fees", "BNB") == Decimal("0.01")
    assert _balanced(led)
    # 未折算部分不进持仓成本
    assert led.holdings["BTC/USDT"].cost == Decimal("60000")
    assert "未折算" in led.entries[-1].memo


def test_unconverted_fees_are_not_added_to_converted_total():
    """一笔能折算、一笔不能：总额只含能折算的那笔。"""
    led = Ledger()
    led.deposit(200_000)
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
        ts=1,
        ref="a",
    )
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.02",
        fee_currency="BNB",
        ts=2,
        ref="b",
    )
    assert dstr(led.fees_value) == "6"  # 只有第一笔
    assert led.fees_by_currency == {"BNB": Decimal("0.03")}  # 按币种汇总仍是全部
    assert led.unconverted_fees == {"BNB": Decimal("0.02")}
    assert led.unconverted_fees_report()["count"] == 1
    assert _balanced(led)


def test_rate_without_source_is_rejected():
    """无法复核的折算比不折算更危险：给了汇率就必须给来源。"""
    led = Ledger()
    led.deposit(100_000)
    with pytest.raises(LedgerError, match="汇率来源"):
        led.trade(
            instrument="BTC/USDT",
            side="buy",
            qty=1,
            price=60_000,
            fee="0.01",
            fee_currency="BNB",
            fee_rate=600,
        )


def test_non_positive_rate_is_rejected():
    led = Ledger()
    led.deposit(100_000)
    for bad in (0, "-600"):
        with pytest.raises(LedgerError, match="汇率必须为正"):
            led.trade(
                instrument="BTC/USDT",
                side="buy",
                qty=1,
                price=60_000,
                fee="0.01",
                fee_currency="BNB",
                fee_rate=bad,
                fee_rate_source=BNB_SOURCE,
            )


def test_rate_on_base_or_quote_fee_currency_is_rejected():
    """base/quote 币种能用成交价直接折算，多给汇率说明口径混乱，直接拒绝。"""
    led = Ledger()
    led.deposit(100_000)
    with pytest.raises(LedgerError, match="不需要额外汇率"):
        led.trade(instrument="BTC/USDT", side="buy", qty=1, price=60_000, fee="0.1", fee_currency="USDT", fee_rate=600)


# ---------------------------------------------------------------- 向后兼容
def test_base_quote_fee_paths_unchanged():
    """只涉及 base/quote 的既有口径完全不变。"""
    led = Ledger()
    led.deposit(1000)
    led.trade(instrument="BTC/USDT", side="buy", qty=1, price=100, fee="0.1")
    assert led.cash() == Decimal("899.9")
    b = led.trade(instrument="BTC/USDT", side="sell", qty=1, price=110, fee="0.11")
    assert led.cash() == Decimal("1009.79")
    assert b.realized == Decimal("9.79")
    assert led.fees_value == Decimal("0.21")
    assert led.unconverted_fees == {}
    assert _balanced(led)

    led2 = Ledger()
    led2.deposit(1000)
    b2 = led2.trade(instrument="BTC/USDT", side="buy", qty=1, price=100, fee="0.001", fee_currency="BTC")
    assert b2.fee_value == Decimal("0.1")
    assert led2.holdings["BTC/USDT"].qty == Decimal("0.999")
    assert _balanced(led2)


# ---------------------------------------------------------------- 浮点输入不漏二进制误差
def test_float_inputs_do_not_leak_binary_error():
    """float 汇率/费率走 str() 转换，折算结果逐位精确。"""
    led = Ledger()
    led.deposit(100_000.0)
    b = led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1.0,
        price=60_000.0,
        fee=0.01,
        fee_currency="BNB",
        fee_rate=600.0,
        fee_rate_source=BNB_SOURCE,
    )
    assert b.fee_value == Decimal("6")
    assert dstr(b.fee_value) == "6"
    # float 版 0.01 * 600.0 会得到 6.000000000000001 之类
    assert str(b.fee_value) in ("6", "6.00", "6.000")
    assert led.cash() == Decimal("40000")
    assert _balanced(led)


def test_awkward_rate_stays_exact():
    """非整除汇率也不引入误差：0.003 BNB @ 312.3456789 → 逐位精确。"""
    led = Ledger()
    led.deposit(100_000)
    b = led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.003",
        fee_currency="BNB",
        fee_rate="312.3456789",
        fee_rate_source=BNB_SOURCE,
    )
    assert b.fee_value == Decimal("0.003") * Decimal("312.3456789")
    assert dstr(b.fee_value) == "0.9370370367"
    assert _balanced(led)


# ---------------------------------------------------------------- 快照往返
def test_snapshot_roundtrip_keeps_unconverted_fees():
    led = Ledger()
    led.deposit(200_000)
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        ts=1,
        ref="a",
    )
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
        ts=2,
        ref="b",
    )
    again = Ledger.from_dict(led.to_dict())
    assert again.fees_value == led.fees_value == Decimal("6")
    assert again.unconverted_fees == {"BNB": Decimal("0.01")}
    assert again.unconverted_fee_details == led.unconverted_fee_details
    assert _balanced(again)


# ---------------------------------------------------------------- 报告口径（metrics.fee_summary）
def test_fee_summary_separates_converted_and_unconverted():
    led = Ledger()
    led.deposit(200_000)
    led.trade(
        instrument="BTC/USDT",
        side="buy",
        qty=1,
        price=60_000,
        fee="0.01",
        fee_currency="BNB",
        fee_rate=600,
        fee_rate_source=BNB_SOURCE,
        ts=1,
        ref="a",
    )
    led.trade(instrument="BTC/USDT", side="buy", qty=1, price=60_000, fee="0.02", fee_currency="BNB", ts=2, ref="b")
    rep = led.unconverted_fees_report()
    out = fee_summary(
        by_currency=led.fees_by_currency,
        converted=dstr(led.fees_value),
        unconverted=rep["by_currency"],
        unconverted_details=rep["details"],
    )
    assert out["converted_value"] == "6"
    assert out["unconverted"] == {"BNB": "0.02"}
    assert out["has_unconverted"] is True
    assert out["by_currency"] == {"BNB": "0.03"}
    assert "未折算" in out["note"]
    # 两个数不能相加：converted_value 里没有未折算的部分
    assert out["converted_value"] != "6.02"


def test_fee_summary_without_unconverted_has_no_note():
    out = fee_summary(by_currency={"USDT": Decimal("0.21")}, converted="0.21")
    assert out["converted_value"] == "0.21"
    assert out["unconverted"] == {}
    assert out["has_unconverted"] is False
    assert out["note"] == ""


# ---------------------------------------------------------------- 实盘归因（live/attribution.py）
def _live_fill(**kw) -> dict:
    base = {
        "fill_id": "x:1",
        "instrument": "BTC/USDT",
        "side": "buy",
        "qty": "1",
        "price": "60000",
        "fee": "0.01",
        "fee_currency": "BNB",
        "ts": 1_791_850_800_000,
    }
    base.update(kw)
    return base


def test_live_result_converts_with_explicit_rates():
    out = live_result([_live_fill()], fee_rates={"BNB": 600})
    assert out["fees"] == "6"
    assert out["has_unconverted_fees"] is False
    assert out["unconverted_fees"] == {}
    assert out["fees_by_currency"] == {"BNB": "0.01"}


def test_live_result_without_rate_reports_unconverted():
    """归因账本同样不估算：没给汇率就单列，不并进 fees。"""
    out = live_result([_live_fill()])
    assert out["fees"] == "0"
    assert out["has_unconverted_fees"] is True
    assert out["unconverted_fees"] == {"BNB": "0.01"}
    assert len(out["unconverted_fee_details"]) == 1
    assert "未折算" in out["note"]


def test_attribute_report_carries_fee_rates_and_notes():
    sim = [
        {
            "fill_id": "f1",
            "security": "BTC/USDT",
            "instrument": "BTC/USDT",
            "side": "buy",
            "amount": "1",
            "qty": "1",
            "price": "60000",
            "fee": "6",
            "fee_value": "6",
            "ts": 1_791_850_800_000,
        }
    ]
    live = [_live_fill(client_order_id="f1")]
    rep = attribute(sim, live, fee_rates={"BNB": 600})
    assert rep.summary()["attributed_result"]["fees"] == "6"
    assert not any("未折算" in n for n in rep.notes)

    rep2 = attribute(sim, live)
    assert rep2.summary()["attributed_result"]["fees"] == "0"
    assert any("未折算" in n for n in rep2.notes)
    # 报告里能按币种看到未折算的数量
    assert rep2.to_dict()["summary"]["attributed_result"]["unconverted_fees"] == {"BNB": "0.01"}
