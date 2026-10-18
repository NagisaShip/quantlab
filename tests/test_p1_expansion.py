from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path):
    from conftest import login
    from fastapi.testclient import TestClient

    from quant_agent.api.app import create_app
    from quant_agent.api.deps import AppServices, reset_services, set_services

    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_contract_specs_cover_three_linear_perp_exchanges():
    from quant_agent.exchanges.contracts import spec_for

    for exchange in ("binance", "okx", "bybit"):
        spec = spec_for(exchange, "BTC/USDT:PERP")
        assert spec.exchange == exchange
        assert spec.contract_type == "linear" and spec.settlement == "USDT"
        assert "isolated" in spec.margin_mode_scope and "cross" in spec.margin_mode_scope
        assert "one-way" in spec.position_mode_scope and "hedge" in spec.position_mode_scope
        assert spec.liquidation_fee_bps == 50
        assert spec.mmr_for_notional(1_000_000) > spec.mmr_for_notional(10_000)


def test_runtime_records_exchange_specific_perp_spec():
    from quant_agent.data.service import synthetic_bars
    from quant_agent.sdk import load_source, run_backtest

    bars = synthetic_bars("BTC/USDT:PERP", "1h", 160)
    mod = load_source(
        "def initialize(ctx):\n    set_leverage(ctx.securities[0], 2)\n\ndef handle_data(ctx):\n    pass\n",
        name="okx_spec",
    )
    res = run_backtest(mod, {"BTC/USDT:PERP": bars}, timeframe="1h", exchange="okx")
    spec = res.contract_specs["BTC/USDT:PERP"]
    assert spec["exchange"] == "okx" and spec["contract_type"] == "linear"
    assert spec["source"].startswith("OKX")


def test_qstrat_perp_export_inspect_import_round_trip(client):
    code = (
        "def initialize(ctx):\n"
        "    set_leverage(ctx.securities[0], 2)\n\n"
        "def handle_data(ctx):\n"
        "    order_target_percent(ctx.securities[0], 0.0 if ctx.now % 2 else 0.2)\n"
    )
    r = client.post(
        "/api/studio/strategies",
        json={
            "name": "永续包回归",
            "code": code,
            "symbol": "BTC/USDT:PERP",
            "securities": ["BTC/USDT:PERP"],
            "timeframe": "1h",
            "kind": "code",
        },
    )
    assert r.status_code == 200, r.text
    slug = r.json()["strategy"]["slug"]
    pkg = client.get(f"/api/studio/strategies/{slug}/package")
    assert pkg.status_code == 200 and pkg.content[:2] == b"PK", pkg.text
    rep = client.post("/api/studio/packages/inspect", content=pkg.content, headers={"Content-Type": "application/zip"})
    assert rep.status_code == 200, rep.text
    body = rep.json()["report"]
    assert body["importable"] and body["golden_match"] and body["demo_ready"], body
    assert body["manifest"]["market"] == "perp"
    assert body["manifest"]["contract_type"] == "linear"
    assert body["manifest"]["margin_mode"] == "isolated"
    imported = client.post(
        "/api/studio/packages/import?slug=perp-imported",
        content=pkg.content,
        headers={"Content-Type": "application/zip"},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["strategy"]["symbol"] == "BTC/USDT:PERP"


def test_mentor_course_progress_is_persisted(client):
    got = client.get("/api/mentor/course")
    assert got.status_code == 200, got.text
    course = got.json()
    assert course["total"] >= 6 and course["modules"][0]["id"] == "hypothesis"
    saved = client.put("/api/mentor/progress", json={"completed": ["hypothesis", "data"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["progress"] == round(2 / course["total"], 3)
    assert "hypothesis" in client.get("/api/mentor/course").json()["completed"]
    bad = client.put("/api/mentor/progress", json={"completed": ["not-a-lesson"]})
    assert bad.status_code == 400


def test_implemented_feature_can_be_enabled(client):
    r = client.put("/api/workspace/features", json={"features": {"margin.cross": True}})
    assert r.status_code == 200, r.text
    got = client.get("/api/workspace/features").json()
    assert got["enabled"]["margin.cross"] is True
    assert got["registry"]["margin.cross"]["implementation"] == "ready"


def test_contract_bill_verification_endpoint(client):
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
    ]
    r = client.post(
        "/api/capabilities/contracts/verify-bills",
        json={"exchange": "okx", "symbol": "BTC/USDT:PERP", "rows": rows, "assumed_taker_fee_bps": 10},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == 2 and body["summary"]["pass"] >= 1
    assert any(c["name"] == "资金费结算间隔" for c in body["checks"])
