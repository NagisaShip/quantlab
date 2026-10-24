"""QuantLab model adapter uses one ModelHub admission per Runtime model step."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from agent_runtime import AgentRuntime, AgentSpec, Journal, ToolSpec

from quant_agent.llm.base import ChatResult, ToolCall
from quant_agent.llm.hub import ModelHub
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig
from quant_agent.platform.db import Database
from quant_agent.platform.quota import QuotaService
from quant_agent.platform.runtime_adapters import (
    QuantLabModelPort,
    QuantLabRuntimeFacade,
    QuantLabToolPort,
    RuntimeToolBinding,
)


def test_model_tool_loop_uses_single_hub_ledger(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'quantlab.sqlite').as_posix()}")
    db.migrate()
    provider = ProviderConfig(
        name="demo",
        type="openai_compatible",
        base_url="http://localhost",
        api_key="key",
        models=["model"],
        scope="personal",
        owner_id="user",
        model_settings={"model": {"price_in": "1", "price_out": "1"}},
        limits=[{"metric": "cost", "window": "total", "limit": "0.001"}],
    )
    router = ModelRouter(
        AgentConfig(providers={"demo": provider}, routing={"default": "demo/model"}, limits={}, trace={})
    )

    class Client:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResult(
                    content="",
                    model="model",
                    provider="demo",
                    tool_calls=[ToolCall("provider-call", "calculator", {"a": 2, "b": 3})],
                    usage={"input_tokens": 2, "output_tokens": 1},
                )
            assert messages[-2].tool_calls[0].id == messages[-1].tool_call_id
            return ChatResult(
                content="5", model="model", provider="demo", usage={"input_tokens": 2, "output_tokens": 1}
            )

    client = Client()
    router._client = lambda name, pc: client
    db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "user@example.test", "User", "hash", 1),
    )
    db.execute(
        "INSERT INTO workspaces(id,name,owner_id,settings,created_at) VALUES(?,?,?,?,?)",
        (
            "ws",
            "Workspace",
            "user",
            json.dumps({"quotas": {"max_llm_calls_per_day": 10, "max_llm_tokens_per_day": 100}}),
            1,
        ),
    )
    quota = QuotaService(db)
    hub = ModelHub(store=None, secrets=None, db=db, quota=quota)
    model = QuantLabModelPort(hub, router, workspace_id="ws", user_id="user", max_tokens=10)
    tools = QuantLabToolPort({"calculator": RuntimeToolBinding(execute=lambda args, effect_id: args["a"] + args["b"])})
    runtime = AgentRuntime(Journal(tmp_path / "runtime.sqlite"), model, tools)
    run_id = runtime.start(
        "add",
        AgentSpec("demo", "1", tools=(ToolSpec("calculator"),), budget_limit_micro=100, model_reservation_micro=20),
        tenant_id="ws",
    )
    assert runtime.resume(run_id)["output"] == "5"
    assert client.calls == 2
    assert db.scalar("SELECT COUNT(*) FROM llm_admissions WHERE run_id=? AND status='SETTLED'", (run_id,)) == 2
    assert db.scalar("SELECT COUNT(*) FROM llm_usage WHERE admission_id IS NOT NULL") == 2
    assert quota.daily(workspace_id="ws")["llm_calls"] == "2"
    assert quota.daily(workspace_id="ws")["llm_tokens"] == "6"


def test_provider_timeout_preserves_both_unsettled_reservations(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'quantlab.sqlite').as_posix()}")
    db.migrate()
    provider = ProviderConfig(
        name="demo",
        type="openai_compatible",
        base_url="http://localhost",
        api_key="key",
        models=["model"],
        scope="personal",
        owner_id="user",
        model_settings={"model": {"price_in": "1", "price_out": "1"}},
        limits=[{"metric": "cost", "window": "total", "limit": "0.001"}],
    )
    router = ModelRouter(
        AgentConfig(providers={"demo": provider}, routing={"default": "demo/model"}, limits={}, trace={})
    )

    class LostClient:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            raise TimeoutError("provider result unknown")

    client = LostClient()
    router._client = lambda name, pc: client
    hub = ModelHub(store=None, secrets=None, db=db)
    runtime = AgentRuntime(
        Journal(tmp_path / "runtime.sqlite"),
        QuantLabModelPort(hub, router, workspace_id="ws", user_id="user", max_tokens=10),
        QuantLabToolPort({}),
    )
    run_id = runtime.start(
        "hello", AgentSpec("demo", "1", budget_limit_micro=100, model_reservation_micro=20), tenant_id="ws"
    )
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert runtime.resume(run_id)["state"] == "WAITING"
    assert client.calls == 1
    assert db.scalar("SELECT COUNT(*) FROM llm_admissions WHERE status='UNSETTLED'") == 1
    assert runtime.journal.budget("run:" + run_id)["reserved_micro"] == 20


def test_missing_usage_reconciles_host_and_runtime_without_second_model_call(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'quantlab.sqlite').as_posix()}")
    db.migrate()
    provider = ProviderConfig(
        name="demo",
        type="openai_compatible",
        base_url="http://localhost",
        api_key="key",
        models=["model"],
        scope="personal",
        owner_id="user",
        model_settings={"model": {"price_in": "1", "price_out": "1"}},
    )
    router = ModelRouter(
        AgentConfig(providers={"demo": provider}, routing={"default": "demo/model"}, limits={}, trace={})
    )

    class Client:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return ChatResult(content="answer", model="model", provider="demo", usage=None)

    client = Client()
    router._client = lambda name, pc: client
    hub = ModelHub(store=None, secrets=None, db=db)
    journal = Journal(tmp_path / "runtime.sqlite")
    run_id = "test-run"
    runs = SimpleNamespace(
        get=lambda requested: (
            {"runtime_version": "0.1.0", "workspace_id": "ws", "created_by": "user"} if requested == run_id else None
        )
    )
    facade = QuantLabRuntimeFacade(journal=journal, runs=runs, hub=hub, router=router)
    facade.start(
        "question",
        AgentSpec("demo", "1", budget_limit_micro=100, model_reservation_micro=20),
        run_id=run_id,
        tenant_id="ws",
    )
    assert facade.resume(run_id)["state"] == "WAITING"
    step_id = facade.get(run_id)["steps"][0]["id"]
    assert hub.admission(step_id)["status"] == "UNSETTLED"
    assert facade.reconcile_model_usage(
        run_id, step_id, actor="provider", usage={"input_tokens": 2, "output_tokens": 1}
    )
    assert facade.get(run_id)["state"] == "COMPLETED"
    assert facade.get(run_id)["output"] == "answer"
    assert hub.admission(step_id)["actual_micro"] == 3
    assert journal.budget("run:" + run_id)["spent_micro"] == 3
    assert client.calls == 1


def test_workspace_quota_denial_fails_runtime_before_provider_dispatch(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'quantlab.sqlite').as_posix()}")
    db.migrate()
    provider = ProviderConfig(
        name="demo",
        type="openai_compatible",
        base_url="http://localhost",
        api_key="key",
        models=["model"],
        scope="personal",
        owner_id="user",
        model_settings={"model": {"price_in": "1", "price_out": "1"}},
        limits=[],
    )
    router = ModelRouter(
        AgentConfig(providers={"demo": provider}, routing={"default": "demo/model"}, limits={}, trace={})
    )

    class Client:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return ChatResult(content="should not run", model="model", provider="demo", usage={"input_tokens": 1})

    client = Client()
    router._client = lambda name, pc: client
    db.execute(
        "INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)",
        ("user", "user@example.test", "User", "hash", 1),
    )
    db.execute(
        "INSERT INTO workspaces(id,name,owner_id,settings,created_at) VALUES(?,?,?,?,?)",
        ("ws", "Workspace", "user", json.dumps({"quotas": {"max_llm_calls_per_day": 0}}), 1),
    )
    quota = QuotaService(db)
    journal = Journal(tmp_path / "runtime.sqlite")
    runtime = AgentRuntime(
        journal,
        QuantLabModelPort(
            ModelHub(store=None, secrets=None, db=db, quota=quota),
            router,
            workspace_id="ws",
            user_id="user",
            max_tokens=10,
        ),
        QuantLabToolPort({}),
    )
    run_id = runtime.start(
        "hello", AgentSpec("demo", "1", budget_limit_micro=100, model_reservation_micro=20), tenant_id="ws"
    )
    result = runtime.resume(run_id)
    assert result["state"] == "FAILED"
    assert "模型请求在发出前被拒绝" in result["error"]
    assert client.calls == 0
    assert db.scalar("SELECT COUNT(*) FROM llm_admissions") == 0
    assert journal.budget("run:" + run_id)["reserved_micro"] == 0
