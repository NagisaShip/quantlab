"""成员与邀请：按成员叠加配额、存储占用统计与上传硬拦截。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QAGENT_OPEN_REGISTRATION", raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    try:
        yield create_app()
    finally:
        reset_services()


def test_member_storage_quota_self_usage_and_upload_rejection(app):
    with TestClient(app) as admin:
        login(admin)
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]

    with TestClient(app) as member:
        login(member, email="storage@example.com", password="Member12!", name="Storage", invite_code=code)
        uid = member.get("/api/auth/me").json()["user"]["id"]

    with TestClient(app) as admin:
        login(admin)
        r = admin.put(
            f"/api/config/members/{uid}",
            json={
                "limits": [
                    {"metric": "storage_bytes", "window": "total", "limit": "180"},
                    {"metric": "tokens", "window": "day", "limit": "1000"},
                    {"metric": "backtests", "window": "total", "limit": "2"},
                ]
            },
        )
        assert r.status_code == 200, r.text
        limits = r.json()["policy"]["limits"]
        assert len(limits) == 3
        assert next(x for x in limits if x["metric"] == "storage_bytes") == {
            "metric": "storage_bytes",
            "window": "total",
            "limit": "180",
        }

    with TestClient(app) as member:
        login(member, email="storage@example.com", password="Member12!")
        first = member.post(
            "/api/uploads?filename=first.txt",
            content=b"x" * 100,
            headers={"content-type": "text/plain"},
        )
        assert first.status_code == 200, first.text

        usage = member.get("/api/config/usage?view=me").json()
        assert usage["storage"]["used_bytes"] == 100
        assert usage["storage"]["file_count"] == 1
        storage_limit = next(x for x in usage["my_limits"] if x["metric"] == "storage_bytes")
        assert storage_limit["used"] == "100" and storage_limit["limit"] == "180"

        second = member.post(
            "/api/uploads?filename=second.txt",
            content=b"y" * 100,
            headers={"content-type": "text/plain"},
        )
        assert second.status_code == 429, second.text
        body = second.json()
        assert body["code"] == "QUOTA_EXHAUSTED"
        assert body["details"]["metric"] == "storage_bytes"
        assert body["details"]["used"] == "100"
        assert body["details"]["requested"] == "100"

        usage = member.get("/api/config/usage?view=me").json()
        assert usage["storage"]["used_bytes"] == 100
        assert usage["storage"]["file_count"] == 1
        assert member.get("/api/config/usage?view=workspace").status_code == 403

    with TestClient(app) as admin:
        login(admin)
        row = next(x for x in admin.get("/api/config/members").json()["members"] if x["user_id"] == uid)
        assert row["custom_limits"] is True
        assert row["storage"]["used_bytes"] == 100
        assert row["storage"]["file_count"] == 1


def test_default_storage_limit_and_individual_override(app):
    with TestClient(app) as admin:
        login(admin)
        r = admin.put(
            "/api/config/member-defaults",
            json={
                "limits": [
                    {"metric": "storage_bytes", "window": "day", "limit": "300"},
                    {"metric": "tokens", "window": "1d", "limit": "50"},
                ]
            },
        )
        assert r.status_code == 200, r.text
        stored = r.json()["limits"]
        assert next(x for x in stored if x["metric"] == "storage_bytes")["window"] == "total"
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]

    with TestClient(app) as member:
        login(member, email="default-storage@example.com", password="Member12!", name="Default", invite_code=code)
        uid = member.get("/api/auth/me").json()["user"]["id"]

    with TestClient(app) as admin:
        login(admin)
        row = next(x for x in admin.get("/api/config/members").json()["members"] if x["user_id"] == uid)
        assert row["custom_limits"] is False
        assert {x["metric"] for x in row["limits"]} == {"storage_bytes", "tokens"}
        admin.put(
            f"/api/config/members/{uid}",
            json={"limits": [{"metric": "storage_bytes", "window": "total", "limit": "50"}]},
        )
        row = next(x for x in admin.get("/api/config/members").json()["members"] if x["user_id"] == uid)
        assert row["custom_limits"] is True
        assert row["limits"] == [{"metric": "storage_bytes", "window": "total", "limit": "50"}]
