"""对话删除：删掉后列表里不再出现，后台回合、分叉、刷新都不能让它「复活」。"""

from __future__ import annotations

import threading
import time

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _ids(client):
    return {p["id"] for p in client.get("/api/lab/projects").json()["projects"]}


def _new(client, **kw):
    body = {"idea": "BTC 均线", "data_source": "synthetic", "bars_limit": 600, **kw}
    return client.post("/api/lab/projects", json=body).json()["project"]


def test_delete_removes_conversation(client):
    p = _new(client)
    assert p["id"] in _ids(client)
    assert client.delete(f"/api/lab/projects/{p['id']}").json()["ok"]
    assert p["id"] not in _ids(client)
    assert client.get(f"/api/lab/projects/{p['id']}").status_code == 404


def test_delete_factor_conversation(client):
    p = _new(client, kind="factor", idea="资金费率")
    assert client.delete(f"/api/lab/projects/{p['id']}").json()["ok"]
    assert p["id"] not in _ids(client)


class SlowRouter:
    """第一次调用卡住一会儿，模拟删除发生在回合进行中。"""

    def __init__(self):
        self.gate = threading.Event()

    def chat(self, messages, **kw):
        self.gate.wait(5)
        return ChatResult(content="回答", model="m", provider="fake")

    def available_models(self):
        return [{"provider": "fake", "model": "m", "target": "fake/m", "type": "openai_compatible", "ready": True}]

    def _has_credentials(self, _):
        return True


def test_delete_while_turn_running_does_not_resurrect(client):
    router = SlowRouter()
    get_services().router = router
    p = _new(client, model="fake/m")
    client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "你好"})  # 后台回合
    time.sleep(0.3)
    assert client.delete(f"/api/lab/projects/{p['id']}").json()["ok"]
    router.gate.set()  # 回合继续跑完，写回结果
    time.sleep(1.0)
    assert p["id"] not in _ids(client), "回合结束时把已删除的对话又写回来了"
