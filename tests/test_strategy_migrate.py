"""旧版统一赛道策略（模板 / 规则 / Agent 代码）转换成策略文件：幂等、原记录保留。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    set_services(AppServices.build(store_root=str(tmp_path_factory.mktemp("mig"))))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_migrate_legacy_strategies(client):
    assert (
        client.post(
            "/api/strategies", json={"name": "旧均线", "template": "ma_trend", "params": {"fast": 7, "slow": 40}}
        ).status_code
        == 200
    )
    r = client.post(
        "/api/arena/rules/save", json={"name": "旧规则", "buy": "sma_fast > sma_slow", "sell": "sma_fast < sma_slow"}
    )
    assert r.status_code == 200, r.text
    assert len(client.get("/api/strategies-migration").json()["pending"]) == 2

    results = client.post("/api/strategies-migration").json()["results"]
    assert all(x["ok"] for x in results), results
    kinds = {x["name"]: x["kind"] for x in results}
    assert kinds == {"旧均线": "code", "旧规则": "rule"}
    files = {s["name"]: s for s in client.get("/api/studio/strategies").json()["strategies"]}
    assert "旧均线" in files and "旧规则" in files
    params = client.get(f"/api/research/strategies/{files['旧均线']['slug']}/params").json()["params"]
    assert params.get("fast") == 7 and params.get("slow") == 40
    # 幂等：再执行一次不会重复转换；原记录仍在
    assert client.get("/api/strategies-migration").json()["pending"] == []
    assert client.post("/api/strategies-migration").json()["results"] == []
    assert len(client.get("/api/strategies").json()["strategies"]) == 2
