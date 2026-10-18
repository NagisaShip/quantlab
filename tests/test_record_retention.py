"""统一研究记录配额：类型倍率、加权用量、PIN、删除与自动清理。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services


@pytest.fixture()
def client():
    root = Path(__file__).resolve().parent / ".tmp-record-retention" / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    set_services(AppServices.build(store_root=str(root)))
    try:
        with TestClient(create_app()) as c:
            me = login(c)
            yield c, me
    finally:
        reset_services()


def _store(me):
    return get_services().store.for_workspace(me["workspace"]["id"])


def _seed(store, collection, rid, *, uid, created_at=None, pinned=False, **extra):
    store.upsert(
        collection,
        {
            "id": rid,
            "created_by": uid,
            "created_at": created_at if created_at is not None else time.time() - 100,
            "pinned": pinned,
            **extra,
        },
    )


def _set_workspace_units(client, value):
    r = client.put("/api/research/record-retention/workspace", json={"max_records_per_user": value})
    assert r.status_code == 200, r.text
    return r.json()


def test_record_catalog_is_serializable_and_has_defaults(client):
    c, me = client
    store = _store(me)
    _seed(store, "factors", "factor-1", uid=me["user"]["id"], name="价值因子", expression="close")

    body = c.get("/api/research/record-retention").json()
    assert body["workspace"]["max_units_per_user"] == 100_000
    assert body["workspace"]["max_records_per_user"] == 100_000
    assert body["usage"]["total_records"] == 1
    assert body["usage"]["total_units"] == pytest.approx(0.18)
    assert {item["key"] for item in body["usage"]["types"]} == {
        "backtest",
        "optimization",
        "factor",
        "model",
        "model_run",
        "portfolio",
        "portfolio_run",
        "execution",
        "execution_run",
    }

    rows = c.get("/api/research/record-retention/records?type=factor")
    assert rows.status_code == 200, rows.text
    data = rows.json()
    assert data["items"][0]["record_title"] == "价值因子"
    assert data["items"][0]["record_type"] == "factor"
    assert data["types"][0] == {
        "key": "backtest",
        "label": "回测记录",
        "default_multiplier": 1.0,
        "approx_bytes": 350 * 1024,
    }
    assert c.get("/api/research/record-retention/records?type=unknown").status_code == 400


def test_admin_multipliers_are_weighted_and_member_cannot_edit(client):
    c, me = client
    store = _store(me)
    _seed(store, "factors", "factor-1", uid=me["user"]["id"], name="因子 A", expression="close")
    _seed(store, "pf_schemes", "pf-1", uid=me["user"]["id"], name="组合 A")

    r = c.put("/api/research/record-retention/multipliers", json={"multipliers": {"factor": 0.5, "portfolio": 0.2}})
    assert r.status_code == 200, r.text
    assert r.json()["multipliers"]["factor"] == 0.5

    usage = c.get("/api/research/record-retention").json()["usage"]
    assert usage["total_units"] == pytest.approx(0.7)
    by_key = {item["key"]: item for item in usage["types"]}
    assert by_key["factor"]["multiplier"] == 0.5
    assert by_key["portfolio"]["multiplier"] == 0.2

    invite = c.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(c.app) as member:
        login(member, email="records@example.com", password="Member12!", name="记录成员", invite_code=invite)
        assert (
            member.put("/api/research/record-retention/multipliers", json={"multipliers": {"factor": 1}}).status_code
            == 403
        )
        assert (
            member.put("/api/research/record-retention/workspace", json={"max_records_per_user": 10}).status_code == 403
        )


def test_quota_rejects_then_auto_deletes_oldest_unpinned(client):
    c, me = client
    store = _store(me)
    uid = me["user"]["id"]
    _seed(store, "factors", "old", uid=uid, created_at=10, name="旧因子", expression="close")
    _seed(store, "factors", "new", uid=uid, created_at=20, name="新因子", expression="open")

    _set_workspace_units(c, 2)
    assert (
        c.put(
            "/api/research/record-retention",
            json={
                "auto_delete_oldest": False,
                "per_strategy_limit": 5,
                "auto_delete_strategy_oldest": True,
            },
        ).status_code
        == 200
    )
    assert c.put("/api/research/record-retention/multipliers", json={"multipliers": {"factor": 1}}).status_code == 200

    blocked = c.post("/api/factors", json={"name": "第三个", "expression": "high", "evaluate": False})
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["details"]["reason"] == "record_units"

    assert (
        c.put(
            "/api/research/record-retention",
            json={
                "auto_delete_oldest": True,
                "per_strategy_limit": 5,
                "auto_delete_strategy_oldest": True,
            },
        ).status_code
        == 200
    )
    created = c.post("/api/factors", json={"name": "第三个", "expression": "high", "evaluate": False})
    assert created.status_code == 200, created.text
    ids = {row["id"] for row in store.list("factors")}
    assert "old" not in ids
    assert "new" in ids
    assert len(ids) == 2


def test_pin_protects_record_and_non_owner_cannot_manage(client):
    c, me = client
    store = _store(me)
    invite = c.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(c.app) as member:
        member_me = login(
            member, email="pincheck@example.com", password="Member12!", name="PIN 成员", invite_code=invite
        )
        member_id = member_me["user"]["id"]
        _seed(store, "factors", "pinned-factor", uid=member_id, name="受保护因子", expression="close")
        assert member.post("/api/research/record-retention/factor/pinned-factor/pin").json()["pinned"] is True
        protected = member.delete("/api/research/record-retention/factor/pinned-factor")
        assert protected.status_code == 409
        assert member.post("/api/research/record-retention/factor/pinned-factor/pin").json()["pinned"] is False
        assert member.delete("/api/research/record-retention/factor/pinned-factor").status_code == 200
        assert store.get("factors", "pinned-factor") is None

        _seed(store, "factors", "other-factor", uid="another-user", name="别人的因子", expression="low")
        assert member.post("/api/research/record-retention/factor/other-factor/pin").status_code == 403
        assert member.delete("/api/research/record-retention/factor/other-factor").status_code == 403


def test_workspace_limit_and_cleanup_endpoints(client):
    c, me = client
    store = _store(me)
    uid = me["user"]["id"]
    _seed(store, "factors", "a", uid=uid, created_at=10, name="A", expression="close")
    _seed(store, "factors", "b", uid=uid, created_at=20, name="B", expression="open")

    body = _set_workspace_units(c, 1)
    assert body["workspace"]["max_units_per_user"] == 1
    assert body["usage"]["max_units_per_user"] == 1
    assert body["usage"]["remaining_units"] == max(0, 1 - body["usage"]["total_units"])

    assert c.post("/api/research/record-retention/cleanup").status_code == 200
    deleted = c.post("/api/research/record-retention/cleanup").json()["deleted"]
    assert deleted >= 0
    assert c.post("/api/research/record-retention/factor/missing/pin").status_code == 404
    assert c.delete("/api/research/record-retention/factor/missing").status_code == 404


def test_member_default_record_units_limit_is_enforced(client):
    c, _me = client
    invite = c.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    defaults = c.put(
        "/api/config/member-defaults",
        json={"limits": [{"metric": "record_units", "window": "total", "limit": "0.2"}]},
    )
    assert defaults.status_code == 200, defaults.text
    assert defaults.json()["limits"][0]["metric"] == "record_units"

    with TestClient(c.app) as member:
        login(member, email="record-limit@example.com", password="Member12!", name="记录限额成员", invite_code=invite)
        first = member.post("/api/factors", json={"name": "限额因子一", "expression": "close", "evaluate": False})
        assert first.status_code == 200, first.text
        second = member.post("/api/factors", json={"name": "限额因子二", "expression": "open", "evaluate": False})
        assert second.status_code == 429, second.text
        assert second.json()["details"]["reason"] == "record_units"

    members = c.get("/api/config/members").json()["members"]
    member_row = next(row for row in members if row["name"] == "记录限额成员")
    record_status = next(x for x in member_row["limit_status"] if x["metric"] == "record_units")
    assert record_status["limit"] == "0.2"
    assert record_status["used"] == "0.18"
