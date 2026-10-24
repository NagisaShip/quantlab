"""Team graph API preserves authority, owner identity and workspace visibility."""

import sys
import threading
from pathlib import Path

import pytest
from conftest import login
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from quant_agent.agents.roles import Role
from quant_agent.agents.team import Team
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.llm.base import ChatResult
from quant_agent.llm.router import AgentConfig, ProviderConfig
from quant_agent.platform import context


@pytest.fixture
def team_host(tmp_path, monkeypatch):
    monkeypatch.setenv("QAGENT_AGENT_RUNTIME", "1")
    services = AppServices.build(store_root=str(tmp_path))
    set_services(services)

    def config(base):
        provider = ProviderConfig(
            "fake",
            "openai_compatible",
            "http://localhost",
            "key",
            models=["m"],
            scope="personal",
            owner_id=context.current().user_id,
        )
        return AgentConfig(providers={"fake": provider}, routing={"default": "fake/m"}, limits={}, trace={})

    services.router.resolver = config

    class Client:
        calls = 0
        unknown_usage = False
        lock = threading.Lock()

        def chat(self, messages, **kwargs):
            with self.lock:
                self.calls += 1
            return ChatResult(
                content="saved answer",
                provider="fake",
                model="m",
                usage={} if self.unknown_usage else {"input_tokens": 2, "output_tokens": 1},
            )

    model = Client()
    services.router._client = lambda *args: model
    try:
        with TestClient(create_app()) as client:
            me = login(client)
            with context.bind(me["workspace"]["id"], user_id=me["user"]["id"], role="admin"):
                services.roles.save(Role("review", "Review", system_prompt="review", tools=[], restrict_tools=True))
                services.teams.save_team(
                    Team("durable", "Durable", mode="parallel", role_ids=["review"], synthesizer_id="review")
                )
            yield client, services, model
    finally:
        reset_services()


def test_team_http_resume_reuses_completed_work_and_reconstructs_index(team_host):
    client, _services, model = team_host
    response = client.post("/api/agents/teams/durable/run", json={"topic": "review", "include_market_data": False})
    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run["status"] == "done" and run["runtime_run_id"]
    assert model.calls == 2
    assert client.post(f"/api/agents/team-runs/{run['id']}/resume").json()["run"]["status"] == "done"
    assert client.get(f"/api/agents/team-runs/{run['id']}").json()["run"]["status"] == "done"
    assert model.calls == 2
    assert client.post("/api/agents/team-runs/missing/resume").status_code == 404


def test_team_http_waiting_is_not_retried_and_another_member_cannot_resume_or_cancel(team_host):
    client, _services, model = team_host
    model.unknown_usage = True
    response = client.post("/api/agents/teams/durable/run", json={"topic": "review", "include_market_data": False})
    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run["status"] == "waiting" and model.calls == 1
    assert client.post(f"/api/agents/team-runs/{run['id']}/resume").json()["run"]["status"] == "waiting"
    assert model.calls == 1
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        login(member, email="member@team.test", password="Member01!", name="Member", invite_code=invite)
        view = member.get(f"/api/agents/team-runs/{run['id']}")
        assert view.status_code == 200 and not view.json()["run"]["can_manage"]
        assert member.post(f"/api/agents/team-runs/{run['id']}/resume").status_code == 403
        assert (
            member.post(f"/api/agents/team-runs/{run['id']}/cancel", json={"signal_id": "not-owner"}).status_code == 403
        )
    canceled = client.post(f"/api/agents/team-runs/{run['id']}/cancel", json={"signal_id": "owner-cancel"})
    assert canceled.status_code == 200 and canceled.json()["run"]["status"] == "canceling"
    assert model.calls == 1
