from __future__ import annotations

from quant_agent.exchanges.contract_verification import normalize_bill, verify_bills
from quant_agent.exchanges.contracts import spec_for


def test_okx_bill_normalization_and_funding_interval():
    rows = [
        {
            "instId": "BTC-USDT-SWAP",
            "ts": 0,
            "fundingRate": "0.0001",
            "markPx": "60000",
            "mgnMode": "isolated",
            "type": "funding",
        },
        {
            "instId": "BTC-USDT-SWAP",
            "ts": 8 * 3_600_000,
            "fundingRate": "0.0002",
            "markPx": "60100",
            "mgnMode": "isolated",
            "type": "funding",
        },
        {
            "instId": "BTC-USDT-SWAP",
            "ts": 16 * 3_600_000,
            "fundingRate": "-0.0001",
            "markPx": "59900",
            "mgnMode": "isolated",
            "type": "funding",
        },
    ]
    row = normalize_bill("okx", rows[0])
    assert row.symbol == "BTC/USDT:PERP"
    assert row.mark_price is not None and str(row.mark_price) == "60000"
    report = verify_bills(exchange="okx", spec=spec_for("okx", "BTC/USDT:PERP"), rows=rows)
    assert report["ok"] and report["summary"]["pass"] >= 2
    assert any(c["name"] == "资金费结算间隔" and c["status"] == "pass" for c in report["checks"])


def test_contract_bill_check_marks_unseen_liquidation_unknown():
    rows = [
        {
            "symbol": "BTCUSDT",
            "transactionTime": 1,
            "side": "Buy",
            "qty": "0.01",
            "tradePrice": "60000",
            "fee": "0.6",
            "mgnMode": "isolated",
        }
    ]
    report = verify_bills(exchange="bybit", spec=spec_for("bybit", "BTC/USDT:PERP"), rows=rows)
    assert any(c["name"] == "强平样本" and c["status"] == "unknown" for c in report["checks"])
    assert any(c["name"] == "结算币种" and c["status"] == "pass" for c in report["checks"])
