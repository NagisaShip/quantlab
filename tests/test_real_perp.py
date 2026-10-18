from decimal import Decimal
from pathlib import Path

import pytest

from quant_agent.live import real_perp as RP
from quant_agent.live.oms import OmsLimits, OmsState


class FakeStore:
    def __init__(self):
        self.d = {}

    def get(self, coll, key):
        return self.d.get((coll, key))

    def upsert(self, coll, doc):
        self.d[(coll, doc.get("id", "config"))] = doc
        return doc

    def list(self, coll):
        return [v for (c, _), v in self.d.items() if c == coll]

    def delete(self, coll, key):
        self.d.pop((coll, key), None)


class Broker:
    exchange = "binance"

    def __init__(self):
        self.orders = []

    def positions(self):
        return {}

    def balances(self):
        return {"USDT": Decimal(1000)}

    def place_market(self, *a, **k):
        self.orders.append((a, k))
        return type("R", (), {"status": "filled"})()

    def rules(self, symbol):
        return type("R", (), {"step": Decimal("1"), "min_qty": Decimal("0"), "min_notional": Decimal("0")})()

    def prepare_perp(self, *a, **k):
        pass


def test_real_perp_is_independent_and_requires_all_flags(monkeypatch):
    store = FakeStore()
    home = Path.cwd() / ".realperp-test-home"
    home.mkdir(exist_ok=True)
    monkeypatch.delenv(RP.SERVER_FLAG, raising=False)
    st = RP.status(store, home)
    assert st["ready"] is False
    assert any(RP.SERVER_FLAG in x for x in st["blockers"])
    monkeypatch.setenv(RP.SERVER_FLAG, "1")
    RP.enable(store, user="u", phrase=RP.ACK_PHRASE, notice_version=RP.NOTICE_VERSION)
    st = RP.status(store, home)
    assert st["ready"] is True
    RP.set_killed(store, True, user="u")
    assert RP.status(store, home)["killed"] is True
    from quant_agent.platform.errors import AppError

    with pytest.raises(AppError):
        RP.set_killed(store, False, user="u", phrase="wrong")


def test_real_perp_oms_rejects_spot_and_caps_notional(monkeypatch):
    b = Broker()
    oms = RP.RealPerpOMS(b, OmsLimits(max_order_notional=100), OmsState(), cap=1000, quote="USDT")
    assert oms._pretrade({"security": "BTC/USDT", "side": "buy", "qty": "1", "ref_price": "100", "opens": True})
    assert oms._pretrade(
        {"security": "BTC/USDT:PERP", "side": "buy", "qty": "3", "ref_price": "100", "opens": True, "leverage": 2}
    )
    assert (
        oms._pretrade(
            {"security": "BTC/USDT:PERP", "side": "buy", "qty": "1", "ref_price": "100", "opens": True, "leverage": 20}
        )
        is not None
    )


def test_real_perp_uses_separate_connection_collection():
    assert RP.CONNS != __import__("quant_agent.live.micro", fromlist=["CONNS"]).CONNS
    assert RP.SERVER_FLAG != "QAGENT_MICROLIVE"
