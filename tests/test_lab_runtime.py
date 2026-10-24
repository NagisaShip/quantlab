"""The research lab resumes its interactive loop from Runtime state."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "agent-runtime" / "src"))

from agent_runtime import Journal

from quant_agent.agents.lab_state import LabStore, new_project
from quant_agent.agents.strategy_lab import APPROVE, LabTools, StrategyLab
from quant_agent.llm.base import ChatResult, LLMError, Tool, ToolCall
from quant_agent.llm.hub import ModelHub, ModelHubAdmission
from quant_agent.llm.router import AgentConfig, ModelRouter, ProviderConfig
from quant_agent.platform import context
from quant_agent.platform.db import Database
from quant_agent.store import JsonStore


class Router:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return self.replies.pop(0)


def _lab(tmp_path, replies):
    store = LabStore(JsonStore(tmp_path / "store"))
    project = new_project({"kind": "research", "title": "Research", "idea": "question"})
    store.save(project)
    tools = LabTools(
        backtest=lambda *a, **kw: {},
        market=lambda *a, **kw: {},
        templates=lambda query: [],
        runtime_journal=Journal(tmp_path / "runtime.sqlite"),
    )
    router = Router(replies)
    lab = StrategyLab(router, store, tools)
    lab._maybe_autocompact = lambda pid: None
    return lab, router, project["id"]


def _reply(content="", calls=()):
    return ChatResult(content=content, model="fake", provider="test", tool_calls=list(calls))


def test_interactive_lab_executes_multiple_tool_steps_and_keeps_images(tmp_path):
    lab, router, pid = _lab(
        tmp_path,
        [_reply(calls=[ToolCall("a", "lookup", {"value": 1}), ToolCall("b", "lookup", {"value": 2})]), _reply("done")],
    )
    used = []
    lab._all_tools = lambda pid: {"lookup": Tool("lookup", "lookup", {}, lambda value: used.append(value) or value)}
    with context.bind("ws", user_id="user"):
        lab.store.append(pid, {"role": "user", "kind": "text", "content": "question"})
        lab._chat_llm(pid, "question", images=["data:image/png;base64,AA=="])
        run_id = lab.store.get(pid)["runtime_chat_id"]
    assert used == [1, 2]
    assert router.calls[0][-1].images == ["data:image/png;base64,AA=="]
    assert len([message for message in router.calls[1] if message.role == "tool"]) == 2
    assert lab.tools.runtime_journal.run(run_id)["state"] == "COMPLETED"
    assert [step["kind"] for step in lab.tools.runtime_journal.steps(run_id)] == ["model", "tool", "tool", "model"]


def test_approval_resumes_same_runtime_in_new_lab_instance(tmp_path):
    lab, router, pid = _lab(tmp_path, [_reply(calls=[ToolCall("a", "publish", {"value": 1})]), _reply("published")])
    used = []
    tool = Tool("publish", "publish", {}, lambda value: used.append(value) or value)
    lab._all_tools = lambda pid: {"publish": tool}
    lab._approval_category = lambda *args: "edit"
    with context.bind("ws", user_id="user"):
        lab.store.append(pid, {"role": "user", "kind": "text", "content": "publish"})
        lab._chat_llm(pid, "publish")
        run_id = lab.store.get(pid)["runtime_chat_id"]
        assert used == []
        restarted = StrategyLab(router, lab.store, lab.tools)
        restarted._all_tools = lab._all_tools
        restarted._approval_category = lab._approval_category
        assert restarted._decide_permission(pid, APPROVE, "")
        assert restarted.store.get(pid)["runtime_chat_id"] == run_id
    assert used == [1]
    assert lab.tools.runtime_journal.run(run_id)["state"] == "COMPLETED"


def test_ask_user_resumes_original_context_after_restart(tmp_path):
    lab, router, pid = _lab(
        tmp_path, [_reply(calls=[ToolCall("a", "ask_user", {"question": "Which?"})]), _reply("answer accepted")]
    )
    lab._all_tools = lambda pid: {
        "ask_user": Tool("ask_user", "ask", {}, lambda question: lab._ask(pid, question, ["A", "B"]))
    }
    with context.bind("ws", user_id="user"):
        lab.store.append(pid, {"role": "user", "kind": "text", "content": "question"})
        lab._chat_llm(pid, "question")
        run_id = lab.store.get(pid)["runtime_chat_id"]
        assert lab.tools.runtime_journal.steps(run_id)[-1]["state"] == "WAITING_INPUT"
        restarted = StrategyLab(router, lab.store, lab.tools)
        restarted._all_tools = lab._all_tools
        lab.store.append(pid, {"role": "user", "kind": "text", "content": "A"})
        restarted._chat_llm(pid, "A")
        assert restarted.store.get(pid)["runtime_chat_id"] == run_id
    assert router.calls[-1][-1].content == "A"
    assert lab.tools.runtime_journal.run(run_id)["state"] == "COMPLETED"


def test_real_lab_model_request_uses_step_identity_without_hidden_retries(tmp_path):
    lab, _, pid = _lab(tmp_path, [])
    db = Database(f"sqlite:///{(tmp_path / 'host.sqlite').as_posix()}")
    db.migrate()
    provider = ProviderConfig(
        "test", "openai_compatible", "http://localhost", "key", models=["fake"], scope="personal", owner_id="user"
    )
    router = ModelRouter(
        AgentConfig(providers={"test": provider}, routing={"default": "test/fake"}, limits={}, trace={})
    )
    hub = ModelHub(store=None, secrets=None, db=db)
    router.admission = ModelHubAdmission(hub)

    class Client:
        calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            raise LLMError("provider timeout", retryable=True)

    client = Client()
    router._client = lambda *args: client
    lab.router = router
    lab._all_tools = lambda pid: {}
    with context.bind("ws", user_id="user"):
        lab.store.append(pid, {"role": "user", "kind": "text", "content": "question"})
        lab._chat_llm(pid, "question")
        run_id = lab.store.get(pid)["runtime_chat_id"]
        lab._chat_llm(pid, "resume")
    step_id = lab.tools.runtime_journal.steps(run_id)[0]["id"]
    assert client.calls == 1
    assert hub.admission(step_id)["status"] == "UNSETTLED"
    assert hub.admission(step_id)["run_id"] == run_id
    assert lab.tools.runtime_journal.run(run_id)["state"] == "WAITING"


def test_tool_failure_is_not_replayed_by_new_lab_instance(tmp_path):
    lab, router, pid = _lab(tmp_path, [_reply(calls=[ToolCall("a", "publish", {})])])
    calls = []

    def publish():
        calls.append("external effect")
        raise ConnectionError("result lost after publish")

    lab._all_tools = lambda pid: {"publish": Tool("publish", "publish", {}, publish)}
    with context.bind("ws", user_id="user"):
        lab.store.append(pid, {"role": "user", "kind": "text", "content": "publish"})
        lab._chat_llm(pid, "publish")
        restarted = StrategyLab(router, lab.store, lab.tools)
        restarted._all_tools = lab._all_tools
        restarted._chat_llm(pid, "continue")
        run_id = lab.store.get(pid)["runtime_chat_id"]
    assert calls == ["external effect"]
    assert lab.tools.runtime_journal.steps(run_id)[-1]["state"] == "RECONCILING"


def test_fork_does_not_share_runtime_or_executable_approval(tmp_path):
    lab, _, pid = _lab(tmp_path, [_reply(calls=[ToolCall("a", "publish", {})])])
    lab._all_tools = lambda pid: {"publish": Tool("publish", "publish", {}, lambda: "published")}
    lab._approval_category = lambda *args: "edit"
    with context.bind("ws", user_id="user"):
        lab.store.append(pid, {"role": "user", "kind": "text", "content": "publish"})
        lab._chat_llm(pid, "publish")
        fork = lab.fork(pid)
    assert not fork.get("runtime_chat_id")
    cards = [message for message in lab.store.messages(fork["id"]) if message["kind"] == "ask"]
    assert cards and cards[-1]["data"]["answered"]
    assert "runtime_approval" not in cards[-1]["data"]


def test_recovery_does_not_consume_old_user_prompt_as_answer(tmp_path):
    from quant_agent.agents.lab_runtime import run_chat

    lab, router, pid = _lab(tmp_path, [_reply(calls=[ToolCall("a", "ask_user", {"question": "Which?"})])])
    lab._all_tools = lambda pid: {"ask_user": Tool("ask_user", "ask", {}, lambda question: lab._ask(pid, question))}
    with context.bind("ws", user_id="user"):
        lab.store.append(pid, {"role": "user", "kind": "text", "content": "question"})
        lab._chat_llm(pid, "question")
        run_chat(lab, pid)
        run_id = lab.store.get(pid)["runtime_chat_id"]
    assert len(router.calls) == 1
    assert lab.tools.runtime_journal.steps(run_id)[-1]["state"] == "WAITING_INPUT"


def test_rewind_cancels_old_approval_before_replacing_user_intent(tmp_path):
    lab, _, pid = _lab(tmp_path, [_reply(calls=[ToolCall("a", "publish", {})])])
    lab._all_tools = lambda pid: {"publish": Tool("publish", "publish", {}, lambda: "published")}
    lab._approval_category = lambda *args: "edit"
    with context.bind("ws", user_id="user"):
        user = lab.store.append(pid, {"role": "user", "kind": "text", "content": "publish"})
        lab._chat_llm(pid, "publish")
        old_run = lab.store.get(pid)["runtime_chat_id"]
        lab.rewind(pid, user["seq"])
    assert lab.tools.runtime_journal.run(old_run)["state"] == "CANCELED"
    assert lab.store.get(pid).get("runtime_chat_id") is None
    assert lab.store.messages(pid) == []
