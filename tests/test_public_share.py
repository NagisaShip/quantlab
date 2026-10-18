"""公开身份、目录搜索与未登录分享预览。"""

from __future__ import annotations

import re

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QAGENT_OPEN_REGISTRATION", raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


def test_identity_directory_public_share_and_revoke(app):
    admin = TestClient(app)
    me = login(admin)
    uid = me["user"]["id"]
    assert re.fullmatch(r"QLU-[0-9A-F]{8}", me["user"]["quantlab_uid"])

    profile = admin.put(
        "/api/community/profile",
        json={
            "name": "公开研究员",
            "bio": "只展示公开信息",
            "dm_policy": "none",
            "avatar_color": "#236B68",
            "public_profile": True,
        },
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["profile"]["public_profile"] is True
    assert profile.json()["profile"]["quantlab_uid"] == me["user"]["quantlab_uid"]

    group = admin.post(
        "/api/collab/groups",
        json={"name": "公开策略协作组", "description": "公开目录测试", "visibility": "open"},
    ).json()["group"]
    assert re.fullmatch(r"QLG-[0-9A-F]{8}", group["quantlab_no"])

    found_group = admin.get(f"/api/collab/discover?q={group['quantlab_no'].replace('-', '')}").json()
    assert any(g["id"] == group["id"] for g in found_group["groups"])
    found_user = admin.get(f"/api/collab/discover?q={me['user']['quantlab_uid']}").json()
    assert any(u["id"] == uid for u in found_user["users"])

    created = admin.post("/api/collab/public-shares", json={"object_type": "group", "object_id": group["id"]})
    assert created.status_code == 200, created.text
    token = created.json()["share"]["token"]
    anon = TestClient(app)
    preview = anon.get(f"/api/public/shares/{token}")
    assert preview.status_code == 200, preview.text
    body = preview.json()["share"]
    assert body["type"] == "group"
    assert body["object"]["quantlab_no"] == group["quantlab_no"]
    assert "messages" not in body["object"]
    assert "tasks" not in body["object"]

    assert admin.delete(f"/api/collab/public-shares/{token}").status_code == 200
    assert anon.get(f"/api/public/shares/{token}").status_code == 404

    rotated = admin.post(f"/api/collab/groups/{group['id']}/number/rotate", json={})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["quantlab_no"] != group["quantlab_no"]

    account = admin.post("/api/auth/quantlab-id/rotate", json={"password": "Passw0rd!"})
    assert account.status_code == 200, account.text
    assert account.json()["account"]["quantlab_uid"] != me["user"]["quantlab_uid"]


def test_public_post_and_strategy_preview_hide_private_fields(app):
    admin = TestClient(app)
    login(admin)
    post = admin.post(
        "/api/community/posts",
        json={"type": "article", "title": "公开研究记录", "body": "正文只给摘要", "tags": ["研究"]},
    ).json()["post"]
    share = admin.post("/api/collab/public-shares", json={"object_type": "post", "object_id": post["id"]}).json()[
        "share"
    ]
    anon = TestClient(app)
    body = anon.get(f"/api/public/shares/{share['token']}").json()["share"]
    assert body["object"]["title"] == "公开研究记录"
    assert "email" not in body["object"]

    strategy = admin.post("/api/research/strategies", json={"name": "不泄露源码", "template_id": "ma_cross"}).json()[
        "strategy"
    ]
    share = admin.post(
        "/api/collab/public-shares",
        json={"object_type": "strategy", "object_id": strategy["slug"]},
    ).json()["share"]
    body = anon.get(f"/api/public/shares/{share['token']}").json()["share"]
    assert body["object"]["name"] == "不泄露源码"
    assert "code" not in body["object"]
    assert "source" not in body["object"]
