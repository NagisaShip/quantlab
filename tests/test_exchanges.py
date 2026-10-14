"""交易所适配器测试：能力矩阵、精度取整、签名、模拟盘隔离。"""

from __future__ import annotations

import httpx
import pytest

from quant_agent.exchanges.base import OrderIntent, UnsupportedFeature
from quant_agent.exchanges.binance import BinanceAdapter
from quant_agent.exchanges.okx import ALLOWED_HOSTS, OKXAdapter
from quant_agent.exchanges.signing import (
    decimal_str,
    hmac_sha256_b64,
    hmac_sha256_hex,
    iso_timestamp_ms,
    signed_query,
)


# ---------------- 签名与格式化 ----------------
def test_signatures_are_deterministic():
    assert hmac_sha256_hex("s", "p") == hmac_sha256_hex("s", "p")
    assert hmac_sha256_b64("s", "p") == hmac_sha256_b64("s", "p")
    assert hmac_sha256_hex("s", "p") != hmac_sha256_hex("s", "q")


def test_iso_timestamp_format():
    assert iso_timestamp_ms(1_788_220_800_000) == "2026-09-01T00:00:00.000Z"
    assert iso_timestamp_ms(1_788_220_800_123).endswith(".123Z")


def test_decimal_str_never_uses_scientific_notation():
    """1e-05 这种写法会被交易所直接拒单，必须转成普通小数。"""
    assert "e" not in decimal_str(0.00001)
    assert decimal_str(0.00001) == "0.00001"
    assert decimal_str(1.0) == "1"
    assert decimal_str(0.001000) == "0.001"


def test_signed_query_sorts_and_appends_signature():
    q = signed_query({"b": 2, "a": 1}, "secret")
    assert q.startswith("a=1&b=2&signature=")
    assert hmac_sha256_hex("secret", "a=1&b=2") in q


# ---------------- 能力矩阵 ----------------
def test_capability_matrix_is_explicit():
    """对不支持的功能必须显式标注 False，而不是含糊过去。"""
    binance = {c.key: c for c in BinanceAdapter().capabilities()}
    okx = {c.key: c for c in OKXAdapter().capabilities()}
    for caps in (binance, okx):
        assert caps["public_market_data"].supported is True
        assert caps["simulated_trading"].supported is True
        assert caps["withdraw"].supported is False
        assert "提现" in caps["withdraw"].note


def test_okx_requires_official_domain():
    with pytest.raises(ValueError, match="只允许官方域名"):
        OKXAdapter(base_url="https://evil.example.com")
    for host in ALLOWED_HOSTS:
        OKXAdapter(base_url=f"https://{host}")


# ---------------- 模拟盘隔离 ----------------
def test_okx_simulated_header_only_when_simulated():
    signed = OKXAdapter(simulated=True, api_key="k", api_secret="s", passphrase="p")
    live = OKXAdapter(simulated=False, api_key="k", api_secret="s", passphrase="p")
    assert signed._headers()["x-simulated-trading"] == "1"
    assert signed._signed_headers("GET", "/x", "")["x-simulated-trading"] == "1"
    assert "x-simulated-trading" not in live._signed_headers("GET", "/x", "")


def test_okx_signing_requires_full_credentials():
    a = OKXAdapter(simulated=True, api_key="k")
    with pytest.raises(UnsupportedFeature, match="Passphrase"):
        a._signed_headers("GET", "/x", "")


def test_binance_market_data_domain_is_separate_from_trading():
    """行情域名与交易域名必须分开：测试环境的历史 K 线非常浅。"""
    live = BinanceAdapter(simulated=False)
    paper = BinanceAdapter(simulated=True)
    assert live.base == "https://data-api.binance.vision"
    assert paper.base == "https://data-api.binance.vision"  # 行情始终走公共域名
    assert paper.trade_base == "https://testnet.binance.vision"
    assert live.trade_base == "https://data-api.binance.vision"


# ---------------- 精度 ----------------
def _with_instruments(adapter, **overrides):
    from quant_agent.exchanges.base import Instrument

    inst = Instrument(
        exchange=adapter.name,
        symbol="BTC/USDT",
        market_type="spot",
        base="BTC",
        quote="USDT",
        tick_size=overrides.get("tick_size", "0.01"),
        lot_size=overrides.get("lot_size", "0.00001"),
        min_notional=overrides.get("min_notional", "10"),
    )
    adapter._instruments = {"BTC/USDT": inst}
    return adapter


@pytest.mark.parametrize(
    "raw,expected",
    [(0.000019, "0.00001"), (0.00001, "0.00001"), (1.234567891, "1.23456"), (0.0, "0")],
)
def test_round_qty_floors_to_step(raw, expected):
    from decimal import Decimal

    a = _with_instruments(BinanceAdapter())
    assert a.round_qty("BTC/USDT", raw) == Decimal(expected)  # Decimal 精确，不是近似


def test_round_price_snaps_to_tick():
    a = _with_instruments(OKXAdapter(), tick_size="0.1")
    from decimal import Decimal

    assert a.round_price("BTC/USDT", 62_000.14) == Decimal("62000.1")


def test_min_notional_check():
    a = _with_instruments(BinanceAdapter(), min_notional="50")
    assert a.meets_min_notional("BTC/USDT", 0.001, 60_000) is True
    assert a.meets_min_notional("BTC/USDT", 0.0001, 60_000) is False


def test_round_qty_is_safe_without_instrument_cache():
    a = BinanceAdapter()
    assert str(a.round_qty("UNKNOWN/USDT", 1.234)) == "1.234"


# ---------------- 下单前置校验 ----------------
def test_place_order_requires_client_order_id():
    """没有 client_order_id 就无法与实盘成交逐笔对账，必须拒绝。"""
    for adapter in (BinanceAdapter(simulated=True), OKXAdapter(simulated=True)):
        with pytest.raises(ValueError, match="client_order_id"):
            adapter.place_order(OrderIntent(run_id="r", signal_id="s", symbol="BTC/USDT", side="buy", qty="0.01"))


def test_place_order_requires_credentials():
    intent = OrderIntent(
        run_id="r",
        signal_id="s",
        symbol="BTC/USDT",
        side="buy",
        qty="0.01",
        client_order_id="c-1",
    )
    with pytest.raises(UnsupportedFeature):
        BinanceAdapter().place_order(intent)
    with pytest.raises(UnsupportedFeature):
        OKXAdapter().place_order(intent)


def test_unsupported_order_type_is_rejected_explicitly():
    intent = OrderIntent(
        run_id="r",
        signal_id="s",
        symbol="BTC/USDT",
        side="buy",
        qty="0.01",
        order_type="stop_loss",
        client_order_id="c-1",
    )
    with pytest.raises(UnsupportedFeature, match="暂不支持"):
        BinanceAdapter(simulated=True, api_key="k", api_secret="s").place_order(intent)


def test_limit_order_requires_price():
    intent = OrderIntent(
        run_id="r",
        signal_id="s",
        symbol="BTC/USDT",
        side="buy",
        qty="0.01",
        order_type="limit",
        client_order_id="c-1",
    )
    adapter = BinanceAdapter(simulated=True, api_key="k", api_secret="s")
    _with_instruments(adapter)
    with pytest.raises(ValueError, match="限价单必须提供 price"):
        adapter.place_order(intent)


# ---------------- 用 mock transport 走通下单路径 ----------------
def test_binance_place_order_sends_signed_request():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={"orderId": 123, "clientOrderId": "c-1", "status": "NEW", "symbol": "BTCUSDT"},
        )

    adapter = BinanceAdapter(
        simulated=True,
        api_key="key",
        api_secret="secret",
        transport=httpx.MockTransport(handler),
    )
    _with_instruments(adapter)
    out = adapter.place_order(
        OrderIntent(
            run_id="r",
            signal_id="s",
            symbol="BTC/USDT",
            side="buy",
            qty="0.001",
            client_order_id="c-1",
        )
    )
    assert out["ok"] is True
    assert out["order_id"] == "123"
    assert out["mode"] == "simulated"
    body = str(captured["body"])
    assert "signature=" in body and "newClientOrderId=c-1" in body
    assert "quantity=0.001" in body


def test_binance_place_order_surfaces_exchange_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -2010, "msg": "insufficient balance"})

    adapter = BinanceAdapter(simulated=True, api_key="k", api_secret="s", transport=httpx.MockTransport(handler))
    _with_instruments(adapter)
    with pytest.raises(UnsupportedFeature, match="insufficient balance"):
        adapter.place_order(
            OrderIntent(
                run_id="r",
                signal_id="s",
                symbol="BTC/USDT",
                side="buy",
                qty="0.001",
                client_order_id="c-1",
            )
        )


def test_okx_place_order_handles_business_error_in_http_200():
    """OKX 用 code 字段表示业务结果，HTTP 200 也可能是失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "51008", "msg": "Order failed", "data": []})

    adapter = OKXAdapter(
        simulated=True,
        api_key="k",
        api_secret="s",
        passphrase="p",
        transport=httpx.MockTransport(handler),
    )
    _with_instruments(adapter)
    with pytest.raises(UnsupportedFeature, match="Order failed"):
        adapter.place_order(
            OrderIntent(
                run_id="r",
                signal_id="s",
                symbol="BTC/USDT",
                side="buy",
                qty="0.001",
                client_order_id="c-1",
            )
        )


def test_okx_place_order_accepts_success_response():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert '"instId":"BTC-USDT"' in body
        assert '"tdMode":"cash"' in body  # 现货现金模式，不加杠杆
        assert '"clOrdId":"c-1"' in body
        assert '"tgtCcy":"base_ccy"' in body  # 数量口径明确为"多少个币"
        assert request.headers.get("x-simulated-trading") == "1"
        return httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [{"ordId": "ord-1", "clOrdId": "c-1", "sCode": "0"}]},
        )

    adapter = OKXAdapter(
        simulated=True,
        api_key="k",
        api_secret="s",
        passphrase="p",
        transport=httpx.MockTransport(handler),
    )
    _with_instruments(adapter)
    out = adapter.place_order(
        OrderIntent(
            run_id="r",
            signal_id="s",
            symbol="BTC/USDT",
            side="sell",
            qty="0.001",
            client_order_id="c-1",
        )
    )
    assert out["ok"] and out["order_id"] == "ord-1"
    assert out["mode"] == "simulated"


def test_query_order_returns_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"orderId": 7, "status": "FILLED", "executedQty": "0.001", "cummulativeQuoteQty": "62"}
        )

    adapter = BinanceAdapter(simulated=True, api_key="k", api_secret="s", transport=httpx.MockTransport(handler))
    out = adapter.query_order("BTC/USDT", "7")
    assert out["status"] == "FILLED"
    assert out["executed_qty"] == "0.001"
