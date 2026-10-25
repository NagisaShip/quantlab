# ruff: noqa: F811
"""Human reconciliation is scoped, idempotent and never dispatches extra work."""

from conftest import login
from fastapi.testclient import TestClient
from test_team_runtime_api import team_host  # noqa: F401 - shared pytest fixture

from quant_agent.api.app import create_app
from quant_agent.llm.base import ChatResult, ToolCall
from quant_agent.platform import context


def start(client):
    response = client.post("/api/agents/teams/durable/run", json={"topic": "review", "include_market_data": False})
    assert response.status_code == 200, response.text
    return response.json()["run"]


def pending(client, run):
    response = client.get(f"/api/agents/team-runs/{run['id']}/runtime")
    assert response.status_code == 200, response.text
    return response.json()["pending"][0]


def test_confirmed_usage_closes_saved_output_without_regeneration_or_double_charge(team_host):
    client, services, model = team_host
    model.unknown_usage = True
    run = start(client)
    item = pending(client, run)
    path = f"/api/agents/team-runs/{run['id']}/runtime/model-usage"
    body = {
        "execution_id": item["execution_id"],
        "step_id": item["id"],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    resolved = client.post(path, json=body)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["accepted"] and model.calls == 1
    admission = services.model_hub.admission(item["id"])
    assert admission["status"] == "SETTLED" and admission["actual_tokens"] == 18
    assert not client.post(path, json=body).json()["accepted"]
    model.unknown_usage = False
    resumed = client.post(f"/api/agents/team-runs/{run['id']}/resume")
    assert resumed.status_code == 200 and resumed.json()["run"]["status"] == "done"
    assert model.calls == 2  # one child request and one synthesis request


def test_cancel_then_no_charge_confirmation_finishes_without_a_new_request(team_host):
    client, services, model = team_host
    model.unknown_usage = True
    run = start(client)
    item = pending(client, run)
    assert (
        client.post(f"/api/agents/team-runs/{run['id']}/cancel", json={"signal_id": "cancel"}).json()["run"]["status"]
        == "canceling"
    )
    path = f"/api/agents/team-runs/{run['id']}/runtime/model-usage"
    body = {"execution_id": item["execution_id"], "step_id": item["id"], "no_charge_confirmed": True}
    response = client.post(path, json=body)
    assert response.status_code == 200 and response.json()["run"]["status"] == "canceled"
    assert services.model_hub.admission(item["id"])["status"] == "RELEASED"
    assert not client.post(path, json=body).json()["accepted"]
    assert model.calls == 1


def test_cross_graph_steps_and_invalid_usage_never_change_billing(team_host):
    client, services, model = team_host
    model.unknown_usage = True
    first, second = start(client), start(client)
    item = pending(client, second)
    response = client.post(
        f"/api/agents/team-runs/{first['id']}/runtime/model-usage",
        json={"execution_id": item["execution_id"], "step_id": item["id"], "no_charge_confirmed": True},
    )
    assert response.status_code == 404
    path = f"/api/agents/team-runs/{second['id']}/runtime/model-usage"
    response = client.post(
        path, json={"execution_id": item["execution_id"], "step_id": item["id"], "usage": {"input_tokens": -1}}
    )
    assert response.status_code == 422
    response = client.post(
        path,
        json={
            "execution_id": item["execution_id"],
            "step_id": item["id"],
            "usage": {"input_tokens": 1},
            "no_charge_confirmed": True,
        },
    )
    assert response.status_code == 422
    assert services.model_hub.admission(item["id"])["status"] == "UNSETTLED"


def test_other_admin_can_confirm_facts_but_cannot_run_the_owners_personal_model(team_host):
    client, services, model = team_host
    model.unknown_usage = True
    run = start(client)
    item = pending(client, run)
    invite = client.post("/api/workspace/invites", json={"role": "admin"}).json()["invite"]["code"]
    with TestClient(create_app()) as admin:
        me = login(admin, email="admin@recovery.test", password="Member01!", name="Admin", invite_code=invite)
        view = admin.get(f"/api/agents/team-runs/{run['id']}/runtime").json()
        assert view["can_resolve"] and not view["can_manage"]
        response = admin.post(
            f"/api/agents/team-runs/{run['id']}/runtime/model-usage",
            json={"execution_id": item["execution_id"], "step_id": item["id"], "no_charge_confirmed": True},
        )
        assert response.status_code == 200, response.text
        assert not response.json()["run"]["can_manage"] and model.calls == 1
        assert admin.post(f"/api/agents/team-runs/{run['id']}/resume").status_code == 403
        event = next(
            event
            for event in services.teams.runtime_journal.events(item["execution_id"])
            if event["type"] == "model.usage_reconciled"
        )
        assert event["data"]["actor"] == me["user"]["id"]
    model.unknown_usage = False
    assert client.post(f"/api/agents/team-runs/{run['id']}/resume").json()["run"]["status"] == "done"
    assert model.calls == 2


def test_tool_resolution_is_idempotent_and_readonly_members_cannot_see_arguments(team_host):
    client, services, model = team_host
    effects = []
    me = client.get("/api/auth/me").json()
    with context.bind(me["workspace"]["id"], user_id=me["user"]["id"], role="admin"):
        role = services.roles.get("review")
        role.tools = ["publish"]
        services.roles.save(role)

    def publish(token):
        effects.append(token)
        raise OSError("published but reply lost")

    services.teams.tools.register(
        "publish", publish, parameters={"type": "object", "properties": {"token": {"type": "string"}}}
    )

    def chat(messages, **kwargs):
        model.calls += 1
        if kwargs.get("tools") and not any(message.role == "tool" for message in messages):
            return ChatResult(
                content="",
                provider="fake",
                model="m",
                usage={"input_tokens": 1, "output_tokens": 1},
                tool_calls=[ToolCall("call", "publish", {"token": "private-placeholder"})],
            )
        return ChatResult(
            content="saved result", provider="fake", model="m", usage={"input_tokens": 1, "output_tokens": 1}
        )

    model.chat = chat
    run = start(client)
    item = pending(client, run)
    assert item["kind"] == "tool" and effects == ["private-placeholder"]
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        login(member, email="member@recovery.test", password="Member01!", name="Member", invite_code=invite)
        view = member.get(f"/api/agents/team-runs/{run['id']}/runtime")
        assert view.status_code == 200 and "private-placeholder" not in view.text
        assert (
            member.post(
                f"/api/agents/team-runs/{run['id']}/runtime/effect-resolution",
                json={
                    "execution_id": item["execution_id"],
                    "step_id": item["id"],
                    "signal_id": "forbidden",
                    "outcome": "absent",
                },
            ).status_code
            == 403
        )
    path = f"/api/agents/team-runs/{run['id']}/runtime/effect-resolution"
    body = {
        "execution_id": item["execution_id"],
        "step_id": item["id"],
        "signal_id": "confirmed",
        "outcome": "succeeded",
        "result": {"ok": True},
    }
    assert client.post(path, json=body).json()["accepted"]
    assert not client.post(path, json=body).json()["accepted"]
    assert client.post(path, json={**body, "outcome": "absent"}).status_code == 409
    assert model.calls == 1 and effects == ["private-placeholder"]
    assert client.post(f"/api/agents/team-runs/{run['id']}/resume").json()["run"]["status"] == "done"
    assert effects == ["private-placeholder"]


def test_billing_commit_can_be_retried_after_runtime_commit_failure(team_host, monkeypatch):
    import pytest
    from agent_runtime import AgentRuntime

    client, services, model = team_host
    model.unknown_usage = True
    run = start(client)
    item = pending(client, run)
    body = {
        "execution_id": item["execution_id"],
        "step_id": item["id"],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    path = f"/api/agents/team-runs/{run['id']}/runtime/model-usage"
    original = AgentRuntime.settle_model_uncertainty

    def interrupted(*args, **kwargs):
        raise RuntimeError("injected failure after host ledger commit")

    monkeypatch.setattr(AgentRuntime, "settle_model_uncertainty", interrupted)
    with pytest.raises(RuntimeError, match="injected failure"):
        client.post(path, json=body)
    committed = services.model_hub.admission(item["id"])
    assert committed["status"] == "SETTLED"
    assert pending(client, run)["id"] == item["id"]
    monkeypatch.setattr(AgentRuntime, "settle_model_uncertainty", original)
    response = client.post(path, json=body)
    assert response.status_code == 200 and response.json()["accepted"]
    assert services.model_hub.admission(item["id"])["actual_micro"] == committed["actual_micro"]
    assert not client.post(path, json=body).json()["accepted"]
    assert model.calls == 1
