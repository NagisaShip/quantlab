"""默认模型选择：当前模型清单里的第一个（优先已配置密钥的），不再默认跟随全局路由。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig


def _router(providers: list[ProviderConfig], routing: dict | None = None) -> ModelRouter:
    return ModelRouter(
        AgentConfig(
            providers={p.name: p for p in providers},
            routing=dict(routing or {}),
            limits={},
            trace={},
        )
    )


def test_default_target_is_first_ready_model_in_config_order() -> None:
    router = _router(
        [
            ProviderConfig("nokey", "openai", "https://a.example", "", models=["m0"]),
            ProviderConfig(
                "gw", "openai_responses", "https://b.example", "sk-test", models=["deepseek-v4.1-flash", "other"]
            ),
        ]
    )
    assert router.default_target() == "gw/deepseek-v4.1-flash"
    # 全局路由不再影响默认选择
    assert router.current_target("code_gen") == ""


def test_default_target_uses_first_model_when_no_key_is_configured() -> None:
    router = _router(
        [
            ProviderConfig("a", "openai", "https://a.example", "", models=["m1"]),
            ProviderConfig("b", "openai", "https://b.example", "", models=["m2"]),
        ]
    )
    assert router.default_target() == "a/m1"


def test_default_target_empty_without_models() -> None:
    router = _router([ProviderConfig("a", "openai", "https://a.example", "sk-x", models=[])])
    assert router.default_target() == ""

    router = _router([])
    assert router.default_target() == ""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    reset_services()
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_lab_meta_default_model_is_first_available_model(client) -> None:
    """设置页的模型顺序决定默认：第一个服务商没密钥时，默认取下一个有密钥的第一个模型。"""
    assert client.get("/api/lab/meta").json()["default_model"] == ""
    for name, key in (("first", ""), ("second", "sk-test-123")):
        saved = client.post(
            "/api/config/providers",
            json={
                "name": name,
                "type": "openai_responses",
                "base_url": f"https://{name}.example/v1",
                "api_key": key,
                "models": ["deepseek-v4.1-flash", "other-model"],
            },
        )
        assert saved.status_code == 200, saved.text
    meta = client.get("/api/lab/meta").json()
    assert meta["default_model"] == "second/deepseek-v4.1-flash"
    assert meta["default_model"] in [m["target"] for m in meta["models"]]
