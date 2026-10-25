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


def _old_shape(monkeypatch):
    from quant_agent.agents.team import TeamRun

    original = TeamRun.to_dict

    def legacy(self):
        record = original(self)
        record.pop("source_project_id", None)
        record.pop("source_policy_version", None)
        return record

    monkeypatch.setattr(TeamRun, "to_dict", legacy)


@pytest.mark.parametrize("durable", [False, True])
def test_legacy_origin_migration_survives_rewind_index_loss_and_policy_restart(team_host, monkeypatch, durable):
    from quant_agent.platform.team_visibility import TeamRunVisibility

    client, services, model = team_host
    if not durable:
        services.teams.runtime_journal = None
    else:
        model.unknown_usage = True
    services.teams.default_team = lambda: services.teams.get_team("durable")
    pid = client.post("/api/lab/projects", json={"kind": "research", "title": "legacy private"}).json()["project"]["id"]
    with monkeypatch.context() as old:
        _old_shape(old)
        response = client.post(
            f"/api/lab/projects/{pid}/messages", json={"text": "/team confidential legacy review", "wait": True}
        )
        assert response.status_code == 200, response.text
    card = next(message for message in response.json()["messages"] if message["kind"] == "team")
    rid = card["data"]["run_id"]
    before = services.teams.runtime_journal.run("team-root-" + rid) if durable else None
    calls = model.calls
    me = client.get("/api/auth/me").json()
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        login(member, email="legacy-reader@visibility.test", password="Member01!", name="Reader", invite_code=invite)
        assert member.get(f"/api/agents/team-runs/{rid}").status_code == 403
        assert member.get("/api/agents/team-runs").json()["runs"] == []
        evidence = services.db.fetchall(
            "SELECT * FROM team_run_sources WHERE workspace_id=? AND run_id=?", (me["workspace"]["id"], rid)
        )
        assert len(evidence) == 1 and evidence[0]["project_id"] == pid and evidence[0]["evidence"] == "legacy_thread"
        with context.bind(me["workspace"]["id"], user_id=me["user"]["id"]):
            services.store.put_blob("lab_threads", pid, {"messages": []})
            if durable:
                services.store.delete(Collections.TEAM_RUNS, rid)
        old_policy = services.teams.run_visible
        services.teams.run_visible = TeamRunVisibility(services.db, services.store, old_policy.source_visible)
        assert member.get(f"/api/agents/team-runs/{rid}").status_code == 403
        if durable:
            assert member.get(f"/api/agents/team-runs/{rid}/runtime").status_code == 403
            assert member.post(f"/api/agents/team-runs/{rid}/resume").status_code == 403
        assert client.put(f"/api/collab/shares/project/{pid}", json={"visibility": "workspace"}).status_code == 200
        assert member.get(f"/api/agents/team-runs/{rid}").status_code == 200
        assert member.get("/api/agents/team-runs").json()["runs"][0]["id"] == rid
        assert client.put(f"/api/collab/shares/project/{pid}", json={"visibility": "private"}).status_code == 200
        assert member.get(f"/api/agents/team-runs/{rid}").status_code == 403
    assert model.calls == calls
    if durable:
        assert services.teams.runtime_journal.run("team-root-" + rid) == before
        services.teams.runtime_journal.audit("team-root-" + rid)


def test_unknown_legacy_runtime_keeps_original_owner_access_but_not_workspace_public(team_host, monkeypatch):
    client, _services, model = team_host
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as author:
        login(author, email="legacy-author@visibility.test", password="Member01!", name="Author", invite_code=invite)
        with monkeypatch.context() as old:
            _old_shape(old)
            response = author.post(
                "/api/agents/teams/durable/run", json={"topic": "old standalone", "include_market_data": False}
            )
            assert response.status_code == 200, response.text
        rid = response.json()["run"]["id"]
        assert author.get(f"/api/agents/team-runs/{rid}").status_code == 200
        assert author.get("/api/agents/team-runs").json()["runs"][0]["id"] == rid
        # Administrator still has access to inspect historical data.
        assert client.get(f"/api/agents/team-runs/{rid}").status_code == 200
        other_invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        with TestClient(create_app()) as reader:
            login(
                reader,
                email="legacy-other@visibility.test",
                password="Member01!",
                name="Other",
                invite_code=other_invite,
            )
            assert reader.get(f"/api/agents/team-runs/{rid}").status_code == 403
            assert reader.get("/api/agents/team-runs").json()["runs"] == []
        assert model.calls == 2


def test_orphan_thread_reference_cannot_become_workspace_public(team_host, monkeypatch):
    client, services, _model = team_host
    me = client.get("/api/auth/me").json()
    with monkeypatch.context() as old:
        _old_shape(old)
        response = client.post("/api/agents/teams/durable/run", json={"topic": "orphan", "include_market_data": False})
    rid = response.json()["run"]["id"]
    with context.bind(me["workspace"]["id"], user_id=me["user"]["id"]):
        services.store.put_blob(
            "lab_threads",
            "deleted-source",
            {
                "messages": [
                    {"role": "tool", "kind": "team", "data": {"run_id": rid}},
                ]
            },
        )
    assert client.get(f"/api/agents/team-runs/{rid}").status_code == 403
    assert services.db.scalar("SELECT COUNT(*) FROM team_run_sources WHERE run_id=?", (rid,)) == 1


def test_shared_fork_is_not_mistaken_for_original_legacy_source(team_host, monkeypatch):
    client, services, model = team_host
    services.teams.default_team = lambda: services.teams.get_team("durable")
    model.unknown_usage = True
    pid = client.post("/api/lab/projects", json={"kind": "research", "title": "original private"}).json()["project"][
        "id"
    ]
    with monkeypatch.context() as old:
        _old_shape(old)
        response = client.post(
            f"/api/lab/projects/{pid}/messages", json={"text": "/team sensitive source", "wait": True}
        )
    rid = next(message for message in response.json()["messages"] if message["kind"] == "team")["data"]["run_id"]
    fork = client.post(f"/api/lab/projects/{pid}/fork", json={"title": "shared fork"})
    assert fork.status_code == 200, fork.text
    fork_id = fork.json()["project"]["id"]
    assert client.put(f"/api/collab/shares/project/{fork_id}", json={"visibility": "workspace"}).status_code == 200
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        login(member, email="fork-reader@visibility.test", password="Member01!", name="Reader", invite_code=invite)
        assert member.get(f"/api/lab/projects/{fork_id}").status_code == 200
        assert member.get(f"/api/agents/team-runs/{rid}").status_code == 403
        sources = services.db.fetchall("SELECT project_id FROM team_run_sources WHERE run_id=?", (rid,))
        assert [row["project_id"] for row in sources] == [pid]


def test_legacy_json_without_origin_is_admin_only(team_host, monkeypatch):
    client, services, _model = team_host
    services.teams.runtime_journal = None
    with monkeypatch.context() as old:
        _old_shape(old)
        response = client.post(
            "/api/agents/teams/durable/run", json={"topic": "unattributed", "include_market_data": False}
        )
    rid = response.json()["run"]["id"]
    assert client.get(f"/api/agents/team-runs/{rid}").status_code == 200
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        login(member, email="json-reader@visibility.test", password="Member01!", name="Reader", invite_code=invite)
        assert member.get(f"/api/agents/team-runs/{rid}").status_code == 403
        assert member.get("/api/agents/team-runs").json()["runs"] == []


def test_ambiguous_legacy_sources_require_all_original_scopes(team_host, monkeypatch):
    client, services, model = team_host
    pids = [
        client.post("/api/lab/projects", json={"kind": "research", "title": title}).json()["project"]["id"]
        for title in ("private before review", "shared before review")
    ]
    assert client.put(f"/api/collab/shares/project/{pids[1]}", json={"visibility": "workspace"}).status_code == 200
    with monkeypatch.context() as old:
        _old_shape(old)
        response = client.post(
            "/api/agents/teams/durable/run", json={"topic": "ambiguous", "include_market_data": False}
        )
    rid = response.json()["run"]["id"]
    me = client.get("/api/auth/me").json()
    with context.bind(me["workspace"]["id"], user_id=me["user"]["id"]):
        for pid in pids:
            services.store.put_blob(
                "lab_threads",
                pid,
                {
                    "messages": [
                        {"role": "tool", "kind": "team", "data": {"run_id": rid}},
                    ]
                },
            )
    calls = model.calls
    invite = client.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
    with TestClient(create_app()) as member:
        login(member, email="ambiguous-reader@visibility.test", password="Member01!", name="Reader", invite_code=invite)
        assert member.get(f"/api/agents/team-runs/{rid}").status_code == 403
        assert {
            row["project_id"]
            for row in services.db.fetchall("SELECT project_id FROM team_run_sources WHERE run_id=?", (rid,))
        } == set(pids)
        assert client.put(f"/api/collab/shares/project/{pids[0]}", json={"visibility": "workspace"}).status_code == 200
        assert member.get(f"/api/agents/team-runs/{rid}").status_code == 200
    assert model.calls == calls


def test_source_migration_commit_failure_cannot_keep_only_public_subset(team_host, monkeypatch):
    from contextlib import contextmanager

    client, services, _model = team_host
    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    policy = services.teams.run_visible
    original_tx = services.db.tx

    @contextmanager
    def interrupted():
        with original_tx() as tx:
            yield tx
            raise OSError("injected source commit failure")

    with monkeypatch.context() as failing:
        failing.setattr(services.db, "tx", interrupted)
        with pytest.raises(OSError, match="source commit failure"):
            policy._remember(ws, "atomic-source", ["public-origin", "private-origin"], "legacy_thread")
    assert policy._sources(ws, "atomic-source") == []
    policy._remember(ws, "atomic-source", ["public-origin", "private-origin"], "legacy_thread")
    policy._remember(ws, "atomic-source", ["public-origin", "private-origin"], "legacy_thread")
    assert policy._sources(ws, "atomic-source") == ["private-origin", "public-origin"]
