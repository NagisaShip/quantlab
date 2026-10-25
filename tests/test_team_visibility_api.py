# ruff: noqa: F811
"""A Team review must never widen its source conversation's visibility."""

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_team_runtime_api import team_host  # noqa: F401

from quant_agent.api.app import create_app
from quant_agent.platform import context
from quant_agent.store import Collections


@pytest.mark.parametrize("durable", [False, True])
def test_private_project_team_tracks_share_changes_and_deleted_index(team_host, durable):
    owner, services, model = team_host
    if not durable:
        services.teams.runtime_journal = None
    else:
        model.unknown_usage = True
    services.teams.default_team = lambda: services.teams.get_team("durable")
    project = owner.post("/api/lab/projects", json={"kind": "research", "title": "private-source-secret"}).json()[
        "project"
    ]
    pid = project["id"]
    assert owner.put(f"/api/collab/shares/project/{pid}", json={"visibility": "private"}).status_code == 200
    response = owner.post(
        f"/api/lab/projects/{pid}/messages", json={"text": "/team private-topic-secret", "wait": True}
    )
    assert response.status_code == 200, response.text
    records = owner.get("/api/agents/team-runs").json()["runs"]
    assert len(records) == 1, records
    run = records[0]
    assert run["source_project_id"] == pid
    run_id = run["id"]
    calls_before_reads = model.calls
    if durable:
        assert run["status"] == "waiting"
    me = owner.get("/api/auth/me").json()
    invite = owner.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        joined = login(
            member, email="private-team@visibility.test", password="Member01!", name="Member", invite_code=invite
        )
        assert member.get(f"/api/lab/projects/{pid}").status_code == 403
        assert member.get(f"/api/agents/team-runs/{run_id}").status_code == 403
        listing = member.get("/api/agents/team-runs")
        assert listing.status_code == 200 and listing.json()["runs"] == []
        assert "private-topic-secret" not in listing.text
        if durable:
            assert member.get(f"/api/agents/team-runs/{run_id}/runtime").status_code == 403
            assert member.post(f"/api/agents/team-runs/{run_id}/resume").status_code == 403
            # A missing JSON projection is rebuilt from the protected authoritative plan.
            with context.bind(me["workspace"]["id"], user_id=me["user"]["id"]):
                services.store.delete(Collections.TEAM_RUNS, run_id)
            assert member.get("/api/agents/team-runs").json()["runs"] == []
        assert owner.put(f"/api/collab/shares/project/{pid}", json={"visibility": "workspace"}).status_code == 200
        assert member.get(f"/api/agents/team-runs/{run_id}").status_code == 200
        assert member.get("/api/agents/team-runs").json()["runs"][0]["id"] == run_id
        assert owner.put(f"/api/collab/shares/project/{pid}", json={"visibility": "private"}).status_code == 200
        assert member.get(f"/api/agents/team-runs/{run_id}").status_code == 403
        assert member.get("/api/agents/team-runs").json()["runs"] == []
        group = owner.post("/api/collab/groups", json={"name": "reviewers", "members": [joined["user"]["id"]]}).json()[
            "group"
        ]
        assert (
            owner.put(
                f"/api/collab/shares/project/{pid}", json={"visibility": "group", "group_ids": [group["id"]]}
            ).status_code
            == 200
        )
        assert member.get(f"/api/agents/team-runs/{run_id}").status_code == 200
        assert owner.delete(f"/api/collab/groups/{group['id']}/members/{joined['user']['id']}").status_code == 200
        assert member.get(f"/api/agents/team-runs/{run_id}").status_code == 403
    assert model.calls == calls_before_reads


def test_missing_source_project_is_not_treated_as_workspace_public(team_host):
    client, services, model = team_host
    services.teams.default_team = lambda: services.teams.get_team("durable")
    model.unknown_usage = True
    pid = client.post("/api/lab/projects", json={"kind": "research", "title": "temporary"}).json()["project"]["id"]
    client.post(f"/api/lab/projects/{pid}/messages", json={"text": "/team review", "wait": True})
    run = client.get("/api/agents/team-runs").json()["runs"][0]
    me = client.get("/api/auth/me").json()
    with context.bind(me["workspace"]["id"], user_id=me["user"]["id"]):
        services.store.delete("lab_projects", pid)
    assert client.get(f"/api/agents/team-runs/{run['id']}").status_code == 403
    assert client.get("/api/agents/team-runs").json()["runs"] == []
