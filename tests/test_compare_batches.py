"""回测页「策略对比」：同一冻结条件下几个策略各跑一次，曲线与指标并排；压力回测加倍成本。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services

SPEC = {"source": "synthetic", "bars_limit": 400, "warmup_bars": 60, "securities": ["BTC/USDT"], "timeframe": "1h"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    set_services(AppServices.build(store_root=str(tmp_path_factory.mktemp("cmp"))))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _new(client, template_id, name):
    return client.post("/api/research/strategies", json={"name": name, "template_id": template_id}).json()["strategy"]


def test_compare_batch_flow(client):
    a, b = _new(client, "ma_cross", "均线A"), _new(client, "ma_cross", "均线B")
    assert (
        client.post("/api/research/compare-batches", json={"slugs": [a["slug"], a["slug"]], "spec": SPEC}).status_code
        == 400
    )
    r = client.post(
        "/api/research/compare-batches", json={"slugs": [a["slug"], b["slug"]], "spec": SPEC, "stress": True}
    )
    assert r.status_code == 200, r.text
    batch = r.json()["batch"]
    assert batch["spec"]["end"] and batch["stress_spec"]["taker_fee"] == pytest.approx(batch["spec"]["taker_fee"] * 2)

    for m in batch["members"]:
        for stress in (False, True):
            spec = batch["stress_spec"] if stress else batch["spec"]
            bt = client.post("/api/research/backtests", json={"slug": m["slug"], **spec, "label": "对比"}).json()
            assert bt["id"], bt
            ok = client.post(
                f"/api/research/compare-batches/{batch['id']}/results",
                json={"slug": m["slug"], "backtest_id": bt["id"], "stress": stress},
            )
            assert ok.status_code == 200
    detail = client.get(f"/api/research/compare-batches/{batch['id']}").json()
    assert len(detail["items"]) == 2 and len(detail["stress_items"]) == 2
    # 同一条件 → 两个同模板策略的曲线完全一样
    assert detail["items"][0]["returns"] == detail["items"][1]["returns"]
    assert client.get("/api/research/compare-batches").json()["batches"][0]["id"] == batch["id"]
    assert (
        client.post(
            f"/api/research/compare-batches/{batch['id']}/results",
            json={"slug": "nope", "backtest_id": detail["items"][0]["id"]},
        ).status_code
        == 400
    )
    assert client.delete(f"/api/research/compare-batches/{batch['id']}").json()["ok"]
