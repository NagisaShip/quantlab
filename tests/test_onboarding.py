"""首次注册新手引导：状态机、API、隔离与审计。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.platform import onboarding


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QAGENT_OPEN_REGISTRATION", raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


def _client(app):
    return TestClient(app)


def test_new_user_starts_not_started_and_progresses(app):
    with _client(app) as c:
        login(c)
        state = c.get("/api/onboarding").json()["onboarding"]
        assert state["status"] == "not_started"
        assert state["current_step"] == "welcome"
        assert state["track"] == "owner"

        r = c.put("/api/onboarding", json={"status": "in_progress", "current_step": "flow"})
        assert r.status_code == 200, r.text
        state = r.json()["onboarding"]
        assert state["status"] == "in_progress" and state["started_at"]

        r = c.put("/api/onboarding", json={"completed_step": "flow", "current_step": "goal"})
        state = r.json()["onboarding"]
        assert state["current_step"] == "goal" and state["completed_steps"] == ["flow"]

        # 完整链路新增的群组与任务步骤必须被状态机接受。
        r = c.put("/api/onboarding", json={"completed_step": "square", "current_step": "groups"})
        assert r.status_code == 200, r.text
        assert r.json()["onboarding"]["current_step"] == "groups"

        r = c.put(
            "/api/onboarding",
            json={"completed_step": "finish", "current_step": "finish", "status": "completed"},
        )
        state = r.json()["onboarding"]
        assert state["status"] == "completed" and state["completed_at"]


def test_invalid_step_is_rejected(app):
    with _client(app) as c:
        login(c)
        r = c.put("/api/onboarding", json={"current_step": "not-a-step"})
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_PARAMETER"


def test_reset_and_skip_are_resumable(app):
    with _client(app) as c:
        login(c)
        c.put("/api/onboarding", json={"status": "in_progress", "current_step": "goal"})
        r = c.put("/api/onboarding", json={"status": "skipped"})
        assert r.json()["onboarding"]["status"] == "skipped"
        r = c.post("/api/onboarding/reset")
        state = r.json()["onboarding"]
        assert state["status"] == "in_progress" and state["current_step"] == "welcome"
        assert state["completed_steps"] == []


def test_onboarding_state_is_per_user(app, monkeypatch):
    monkeypatch.setenv("QAGENT_OPEN_REGISTRATION", "1")
    services = get_services()
    first = services.auth.register(username="first_onb", password="Abcdef12!")
    second = services.auth.register(username="second_onb", password="Abcdef12!")
    onboarding.ensure(services.auth.db, first["user_id"], is_admin=True)
    onboarding.ensure(services.auth.db, second["user_id"], is_admin=False)
    onboarding.update(
        services.auth.db, first["user_id"], status="completed", current_step="finish", completed_step="finish"
    )
    assert onboarding.get(services.auth.db, first["user_id"], is_admin=True)["status"] == "completed"
    assert onboarding.get(services.auth.db, second["user_id"], is_admin=False)["status"] == "not_started"
    assert onboarding.get(services.auth.db, second["user_id"], is_admin=False)["track"] == "member"
