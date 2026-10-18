"""回测记录保留策略：总量、单策略、自动清理与 PIN 保护。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.platform.backtest_retention import plan_for_new, record_rows_for_user
from quant_agent.store import JsonStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _strategy(client: TestClient) -> str:
    rec = client.post("/api/research/strategies", json={"name": "保留策略", "template_id": "ma_cross"}).json()[
        "strategy"
    ]
    return rec["slug"]


def _backtest(client: TestClient, slug: str) -> dict:
    return client.post(
        "/api/research/backtests",
        json={"slug": slug, "source": "synthetic", "bars_limit": 320, "warmup_bars": 50, "persist": True},
    )


def test_retention_defaults_and_admin_setting(client):
    body = client.get("/api/research/backtest-retention").json()
    assert body["workspace"]["max_records_per_user"] == 100_000
    assert body["user"] == {
        "auto_delete_oldest": False,
        "per_strategy_limit": 5,
        "auto_delete_strategy_oldest": True,
    }
    assert body["is_workspace_admin"] is True
    assert (
        client.put("/api/research/backtest-retention/workspace", json={"max_records_per_user": 100_000}).status_code
        == 200
    )
    assert client.get("/api/research/backtest-retention").json()["workspace"]["max_records_per_user"] == 100_000

    code = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(client.app) as member:
        login(member, email="retention@example.com", password="Member123!", name="保留成员", invite_code=code)
        assert member.get("/api/research/backtest-retention").status_code == 200
        assert (
            member.put("/api/research/backtest-retention/workspace", json={"max_records_per_user": 10}).status_code
            == 403
        )


def test_strategy_limit_pin_and_auto_delete(client):
    slug = _strategy(client)
    saved = client.put(
        "/api/research/backtest-retention",
        json={"auto_delete_oldest": False, "per_strategy_limit": 1, "auto_delete_strategy_oldest": False},
    )
    assert saved.status_code == 200, saved.text

    first = _backtest(client, slug)
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]
    assert first_id

    blocked = _backtest(client, slug)
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["code"] == "QUOTA_EXHAUSTED"
    assert blocked.json()["details"]["reason"] == "strategy_limit"

    pinned = client.post(f"/api/research/backtests/{first_id}/pin")
    assert pinned.status_code == 200 and pinned.json()["pinned"] is True

    client.put(
        "/api/research/backtest-retention",
        json={"auto_delete_oldest": False, "per_strategy_limit": 1, "auto_delete_strategy_oldest": True},
    )
    blocked_by_pin = _backtest(client, slug)
    assert blocked_by_pin.status_code == 429
    assert blocked_by_pin.json()["details"]["reason"] == "strategy_pinned"

    assert client.post(f"/api/research/backtests/{first_id}/pin").json()["pinned"] is False
    second = _backtest(client, slug)
    assert second.status_code == 200, second.text
    second_id = second.json()["id"]
    assert second_id and second_id != first_id
    rows = client.get(f"/api/research/backtests?slug={slug}").json()["backtests"]
    assert [r["id"] for r in rows] == [second_id]
    assert client.get(f"/api/research/backtests/{first_id}").status_code == 404


def test_total_retention_plan_skips_pinned_oldest():
    now = 1000.0
    rows = [
        {"id": "old", "slug": "a", "strategy_name": "A", "created_at": now - 30, "created_by": "u", "pinned": False},
        {"id": "pinned", "slug": "b", "strategy_name": "B", "created_at": now - 20, "created_by": "u", "pinned": True},
        {"id": "newer", "slug": "c", "strategy_name": "C", "created_at": now - 10, "created_by": "u", "pinned": False},
    ]
    pending = {
        "id": "__pending__",
        "slug": "d",
        "strategy_name": "D",
        "created_at": now,
        "created_by": "u",
        "pinned": False,
    }
    prefs = {"auto_delete_oldest": True, "per_strategy_limit": 5, "auto_delete_strategy_oldest": True}
    deletions = plan_for_new(rows, pending, max_records=2, prefs=prefs)
    assert [r["id"] for r in deletions] == ["old", "newer"]


def test_record_search_and_cursor_pagination(tmp_path):
    store = JsonStore(tmp_path / "store")
    for i in range(5):
        store.upsert(
            "factors",
            {
                "id": f"f-{i}",
                "name": f"Needle factor {i}",
                "created_by": "u1",
                "created_at": 1000 + i,
            },
        )
    first = record_rows_for_user(store, "u1", q="needle factor", limit=2)
    assert [row["id"] for row in first] == ["f-4", "f-3"]
    cursor = first[-1]
    second = record_rows_for_user(
        store,
        "u1",
        q="needle factor",
        before_time=cursor["created_at"],
        before_id=cursor["id"],
        limit=2,
    )
    assert [row["id"] for row in second] == ["f-2", "f-1"]
    assert record_rows_for_user(store, "u1", q="missing") == []
