"""HTTP verification for durable lab input, recovery cursors and manual resolution."""

import sys
from pathlib import Path

import pytest
from conftest import login
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from quant_agent.agents.strategy_lab import StrategyLab
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.llm.base import ChatResult, Tool, ToolCall
from quant_agent.llm.router import AgentConfig, ProviderConfig
from quant_agent.platform import context


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME", "1")
    services = AppServices.build(store_root=str(tmp_path))
    set_services(services)

    def config(base):
        provider = ProviderConfig(
            "fake",
            "openai_compatible",
            "http://localhost",
            "key",
            models=["model"],
            scope="personal",
            owner_id=context.current().user_id,
            model_settings={"model": {"price_in": "1", "price_out": "1"}},
        )
        return AgentConfig(providers={"fake": provider}, routing={"default": "fake/model"}, limits={}, trace={})

    services.router.resolver = config

    class Client:
        calls = 0
        replies = []

        def chat(self, messages, **kwargs):
            self.calls += 1
            return self.replies.pop(0)

    model = Client()
    services.router._client = lambda *args: model
    try:
        with TestClient(create_app()) as client:
            login(client)
            response = client.post(
                "/api/lab/projects", json={"kind": "research", "title": "Research", "idea": "question"}
            )
            assert response.status_code == 200, response.text
            yield client, model, response.json()["project"]["id"]
    finally:
        reset_services()


def _reply(content="", calls=(), usage=None):
    return ChatResult(
        content=content,
        provider="fake",
        model="model",
        tool_calls=list(calls),
        usage={"input_tokens": 2, "output_tokens": 1} if usage is None else usage,
    )


def _send(client, pid, text):
    response = client.post(f"/api/lab/projects/{pid}/messages", json={"text": text, "wait": True})
    assert response.status_code == 200, response.text
    return response.json()


def test_http_input_resume_and_cursor_reconnection(host):
    client, model, pid = host
    model.replies = [
        _reply(calls=[ToolCall("ask", "ask_user", {"question": "Which?", "options": ["A", "B"]})]),
        _reply("A accepted"),
    ]
    first = _send(client, pid, "question")
    run_id = first["project"]["runtime_chat_id"]
    cursor, sequences = 0, []
    while True:
        response = client.get(f"/api/lab/projects/{pid}/runtime", params={"after": cursor, "limit": 2})
        assert response.status_code == 200, response.text
        page = response.json()
        sequences.extend(event["seq"] for event in page["events"])
        cursor = page["cursor"]
        if cursor == page["run"]["seq"]:
            break
    assert sequences == list(range(1, cursor + 1))
    assert client.get(f"/api/lab/projects/{pid}/runtime", params={"after": cursor}).json()["events"] == []
    _send(client, pid, "A")
    state = client.get(f"/api/lab/projects/{pid}/runtime").json()["run"]
    assert state["id"] == run_id and state["state"] == "COMPLETED"
    assert model.calls == 2


def test_http_external_effect_resolution_is_idempotent_and_does_not_reexecute(host, monkeypatch):
    client, model, pid = host
    effects = []

    def publish():
        effects.append("published")
        raise TimeoutError("external result was lost")

    monkeypatch.setattr(
        StrategyLab, "_all_tools", lambda self, pid: {"publish": Tool("publish", "publish", {}, publish)}
    )
    model.replies = [_reply(calls=[ToolCall("publish", "publish", {})]), _reply("confirmed")]
    _send(client, pid, "publish")
    current = client.get(f"/api/lab/projects/{pid}/runtime").json()["run"]
    step_id = current["steps"][-1]["id"]
    blocked = client.delete(f"/api/lab/projects/{pid}")
    assert blocked.status_code == 409, blocked.text
    payload = {"step_id": step_id, "signal_id": "confirmed-publication", "outcome": "succeeded", "result": "published"}
    for accepted in (True, False):
        response = client.post(f"/api/lab/projects/{pid}/runtime/effect-resolution", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is accepted
    response = client.post(f"/api/lab/projects/{pid}/runtime/resume", json={"wait": True})
    assert response.status_code == 200, response.text
    assert client.get(f"/api/lab/projects/{pid}/runtime").json()["run"]["state"] == "CANCELED"
    assert effects == ["published"]


def test_http_confirmed_billing_finishes_saved_output_without_regeneration(host):
    client, model, pid = host
    model.replies = [_reply("answer", usage={})]
    _send(client, pid, "question")
    current = client.get(f"/api/lab/projects/{pid}/runtime").json()["run"]
    assert current["state"] == "WAITING"
    step_id = current["steps"][0]["id"]
    response = client.post(
        f"/api/lab/projects/{pid}/runtime/model-usage",
        json={"step_id": step_id, "usage": {"input_tokens": 2, "output_tokens": 1}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "COMPLETED"
    response = client.post(f"/api/lab/projects/{pid}/runtime/resume", json={"wait": True})
    assert response.status_code == 200, response.text
    assert model.calls == 1
    assert any(message["content"] == "answer" for message in response.json()["messages"])


def test_member_cannot_adjudicate_effects_or_billing(host):
    client, model, pid = host
    model.replies = [_reply("answer", usage={})]
    _send(client, pid, "question")
    current = client.get(f"/api/lab/projects/{pid}/runtime").json()["run"]
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        login(member, email="member@example.test", password="Member01!", name="Member", invite_code=invite)
        response = member.post(
            f"/api/lab/projects/{pid}/runtime/model-usage",
            json={"step_id": current["steps"][0]["id"], "no_charge_confirmed": True},
        )
        assert response.status_code == 403, response.text
        response = member.post(
            f"/api/lab/projects/{pid}/runtime/effect-resolution",
            json={"step_id": "unknown", "signal_id": "forbidden", "outcome": "absent"},
        )
        assert response.status_code == 403, response.text
    assert client.get(f"/api/lab/projects/{pid}/runtime").json()["run"]["state"] == "WAITING"


def test_runtime_run_id_cannot_escape_its_project(host):
    client, model, pid = host
    model.replies = [_reply("answer", usage={})]
    _send(client, pid, "question")
    current = client.get(f"/api/lab/projects/{pid}/runtime").json()["run"]
    other = client.post("/api/lab/projects", json={"kind": "research", "title": "Other"}).json()["project"]["id"]
    response = client.get(f"/api/lab/projects/{other}/runtime", params={"run_id": current["id"]})
    assert response.status_code == 404, response.text
    response = client.post(
        f"/api/lab/projects/{other}/runtime/model-usage",
        params={"run_id": current["id"]},
        json={"step_id": current["steps"][0]["id"], "no_charge_confirmed": True},
    )
    assert response.status_code == 404, response.text
    invalid = client.post(
        f"/api/lab/projects/{pid}/runtime/model-usage",
        json={"step_id": current["steps"][0]["id"], "usage": {"input_tokens": -1}},
    )
    assert invalid.status_code == 422, invalid.text
    assert client.get(f"/api/lab/projects/{pid}/runtime").json()["run"]["state"] == "WAITING"


def test_event_cursor_does_not_advance_beyond_its_state_snapshot(host, monkeypatch):
    from agent_runtime import AgentRuntime

    client, model, pid = host
    model.replies = [_reply("answer", usage={})]
    _send(client, pid, "question")
    original = AgentRuntime.events

    def commit_between_snapshot_and_event_read(runtime, run_id, after=0):
        with runtime.journal.tx() as db:
            runtime.journal.event(db, run_id, "test.concurrent_commit", {})
        return original(runtime, run_id, after)

    monkeypatch.setattr(AgentRuntime, "events", commit_between_snapshot_and_event_read)
    first = client.get(f"/api/lab/projects/{pid}/runtime").json()
    assert first["cursor"] == first["run"]["seq"]
    second = client.get(f"/api/lab/projects/{pid}/runtime", params={"after": first["cursor"]}).json()
    assert second["events"][0]["seq"] == first["cursor"] + 1
    assert second["cursor"] == second["run"]["seq"]
