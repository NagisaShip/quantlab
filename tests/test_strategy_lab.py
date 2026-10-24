"""AI 策略室：项目、对话工具调用、自动迭代闭环（降级与模型两条路径）、版本、上下文、保存为策略。"""

from __future__ import annotations

import json

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents.strategy_lab import DEFAULT_GOALS, evaluate_goals, rule_revise
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult, LLMError, ToolCall
from quant_agent.strategies.sdk_templates import get_template

GOOD = """PARAMS = {"fast": 10, "slow": 30, "weight": 0.6}

def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    c = history(s, PARAMS["slow"], "close")
    if len(c) < PARAMS["slow"]:
        return
    order_target_percent(s, PARAMS["weight"] if sum(c[-PARAMS["fast"]:]) / PARAMS["fast"] > sum(c) / len(c) else 0)
"""


class ScriptedRouter:
    """按顺序返回预设的 ChatResult（可含工具调用）；记录每次收到的消息与工具。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        if not self.replies:
            raise LLMError("no more replies")
        r = self.replies.pop(0)
        return r if isinstance(r, ChatResult) else ChatResult(content=r, model="fake-1", provider="fake")

    def available_models(self):
        return [{"provider": "fake", "model": "fake-1", "target": "fake/fake-1", "type": "openai_compatible"}]

    def _has_credentials(self, _):
        return True


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def _project(client, **kw):
    body = {
        "idea": "BTC 1小时 20 和 60 均线金叉，止损 5%，仓位 70%",
        "securities": ["BTC/USDT"],
        "timeframe": "1h",
        "market_auto": False,
        "data_source": "synthetic",
        "bars_limit": 600,
        "max_rounds": 3,
        **kw,
    }
    r = client.post("/api/lab/projects", json=body)
    assert r.status_code == 200, r.text
    return r.json()["project"]


# ---------------- 纯函数 ----------------
def test_agent_protocol_residue_and_empty_delete_guards():
    from quant_agent.agents.tool_guards import approval_input_error, clean_protocol_residue

    assert clean_protocol_residue("结论\n<target_placeholder>\n继续") == "结论\n\n继续"
    assert clean_protocol_residue("代码示例 <tool> 保留") == "代码示例 <tool> 保留"
    assert "未创建审批" in approval_input_error("delete_backtests", {"ids": []})
    assert "未创建审批" in approval_input_error("delete_backtests", {"ids": ["  "]})
    assert approval_input_error("delete_backtests", {"ids": ["bt-1"]}) == ""
    assert approval_input_error("delete_strategy", {"slug": ""})
    assert approval_input_error("delete_factor", {"fid": "factor-1"}) == ""


def test_evaluate_goals_and_rule_revise():
    ev = evaluate_goals({"sharpe": 0.5, "max_drawdown": 0.4, "trades": 3}, DEFAULT_GOALS)
    assert not ev["passed"] and {i["key"] for i in ev["items"] if not i["ok"]} == {
        "min_sharpe",
        "max_drawdown",
        "min_trades",
    }
    ok = evaluate_goals({"sharpe": 1.5, "max_drawdown": 0.1, "trades": 30}, DEFAULT_GOALS)
    assert ok["passed"] and ok["score"] > ev["score"]
    code = get_template("ma_cross").code
    new_code, notes = rule_revise(code, ev, 1)
    assert new_code != code and notes
    assert any("仓位" in n for n in notes) and any("周期" in n for n in notes)


# ---------------- 项目 ----------------
def test_project_crud_and_meta(client):
    meta = client.get("/api/lab/meta").json()
    assert meta["default_goals"]["min_sharpe"] == 1.0 and meta["teams"] and meta["llm_ready"] is False
    p = _project(client, template_id="ma_cross", pins=[{"title": "我的笔记", "text": "只做多，避免高频"}])
    assert p["current_v"] == 1 and p["versions"][0]["source"] == "template"
    view = client.get(f"/api/lab/projects/{p['id']}").json()
    keys = [i["key"] for i in view["context"]["items"]]
    assert "code" in keys and any(k.startswith("pin:") for k in keys)
    pin_id = view["project"]["pins"][0]["id"]
    assert client.delete(f"/api/lab/projects/{p['id']}/pins/{pin_id}").json()["ok"]
    assert (
        client.patch(f"/api/lab/projects/{p['id']}", json={"title": "改名", "goals": {"max_drawdown": 0.15}}).json()[
            "project"
        ]["goals"]["max_drawdown"]
        == 0.15
    )
    rows = client.get("/api/lab/projects").json()["projects"]
    assert rows[0]["title"] == "改名" and "code" not in rows[0]
    assert client.delete(f"/api/lab/projects/{p['id']}").json()["ok"]
    assert client.get(f"/api/lab/projects/{p['id']}").status_code == 404


def test_manual_code_is_checked_and_versioned(client):
    p = _project(client)
    bad = client.post(f"/api/lab/projects/{p['id']}/code", json={"code": "import os\ndef initialize(ctx):\n pass\n"})
    assert bad.status_code == 400
    ok = client.post(f"/api/lab/projects/{p['id']}/code", json={"code": GOOD}).json()
    assert ok["project"]["current_v"] == 1 and ok["messages"][-1]["kind"] == "code"
    bt = client.post(f"/api/lab/projects/{p['id']}/backtest", json={"wait": True}).json()
    card = [m for m in bt["messages"] if m["kind"] == "backtest"][-1]
    assert card["data"]["ok"] and card["data"]["bt_id"] and "sharpe" in card["data"]["metrics"]
    assert bt["project"]["versions"][0]["metrics"] is not None
    # 回测记录进入平台「回测」列表，可打开详情
    assert client.get(f"/api/research/backtests/{card['data']['bt_id']}").status_code == 200


# ---------------- 闭环（无模型：模板生成 + 规则调参）----------------
def test_autopilot_fallback_closes_the_loop(client):
    p = _project(client, goals={"min_sharpe": 99})  # 不可能达标 → 先被目标检查拦下
    r = client.post(f"/api/lab/projects/{p['id']}/autopilot", json={"research": "market", "wait": True})
    assert r.status_code == 200, r.text
    first = r.json()
    check = [m for m in first["messages"] if m["kind"] == "goal"][-1]
    assert check["data"]["check"]["verdict"] == "unreasonable" and first["project"]["status"] == "awaiting_user"
    assert first["messages"][-1]["kind"] == "ask" and "仍然开始" in first["messages"][-1]["data"]["options"]
    # 研究者坚持 → 按原参数（含先调研）开始
    r = client.post(f"/api/lab/projects/{p['id']}/decision", json={"choice": "仍然开始", "wait": True})
    assert r.status_code == 200, r.text
    view = r.json()
    msgs = view["messages"]
    kinds = [m["kind"] for m in msgs]
    assert "round" in kinds and "backtest" in kinds and "code" in kinds
    tools = [m["data"]["name"] for m in msgs if m["kind"] == "tool"]
    assert tools[0] == "get_market_summary" and "generate" in tools and "revise" in tools
    assert any("规则降级" in m["content"] for m in msgs if m["role"] == "assistant")
    done = [m for m in msgs if m["kind"] == "round" and m["data"].get("stage") == "done"][-1]
    assert done["data"]["stress"] is not None, "收尾应做 2 倍成本压力测试"
    ask = msgs[-1]
    assert ask["kind"] == "ask" and ask["data"]["decision"] and "满意，保存为策略" in ask["data"]["options"]
    assert view["project"]["status"] == "awaiting_user" and len(view["project"]["versions"]) >= 2
    # 用户满意 → 保存为平台策略草稿
    d = client.post(f"/api/lab/projects/{p['id']}/decision", json={"choice": "满意，保存为策略", "wait": True}).json()
    assert d["project"]["slug"] and d["project"]["status"] == "idle"
    strat = client.get(f"/api/studio/strategies/{d['project']['slug']}").json()
    assert "def initialize" in json.dumps(strat, ensure_ascii=False)


def test_restore_version(client):
    p = _project(client, template_id="ma_cross")
    client.post(f"/api/lab/projects/{p['id']}/code", json={"code": GOOD})
    r = client.post(f"/api/lab/projects/{p['id']}/versions/1/restore").json()
    assert r["project"]["current_v"] == 1 and len(r["project"]["versions"]) == 2


def test_fallback_chat_and_slash_commands(client):
    p = _project(client)
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "帮我写一个 BTC 均线策略", "wait": True})
    view = r.json()
    assert view["project"]["code"] and any(m["kind"] == "backtest" for m in view["messages"])
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "/解释", "wait": True}).json()
    assert "静态分析" in r["messages"][-1]["content"]


# ---------------- 模型路径：工具调用 ----------------
def test_llm_chat_uses_tools_and_asks_user(client):
    router = ScriptedRouter(
        [
            ChatResult(
                content="我先写一版。",
                model="m",
                provider="fake",
                tool_calls=[ToolCall(id="1", name="write_strategy", arguments={"code": "import os\n", "note": "坏"})],
            ),
            ChatResult(
                content="",
                model="m",
                provider="fake",
                tool_calls=[ToolCall(id="2", name="write_strategy", arguments={"code": GOOD, "note": "均线"})],
            ),
            ChatResult(
                content="", model="m", provider="fake", tool_calls=[ToolCall(id="3", name="run_backtest", arguments={})]
            ),
            ChatResult(
                content="夏普偏低，原因是……",
                model="m",
                provider="fake",
                tool_calls=[
                    ToolCall(
                        id="4",
                        name="ask_user",
                        arguments={"question": "要不要加波动率过滤？", "options": ["加", "不加"]},
                    )
                ],
            ),
        ]
    )
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    view = client.post(
        f"/api/lab/projects/{p['id']}/messages", json={"text": "写个均线策略并回测", "wait": True}
    ).json()
    msgs = view["messages"]
    cards = [m for m in msgs if m["kind"] == "tool"]
    assert [c["data"]["name"] for c in cards] == ["write_strategy", "write_strategy", "run_backtest"]
    assert cards[0]["data"]["status"] == "ok" and '"ok": false' in cards[0]["data"]["preview"]
    assert view["project"]["current_v"] == 1, "坏代码不应成为版本"
    assert msgs[-1]["kind"] == "ask" and msgs[-1]["data"]["options"] == ["加", "不加"]
    assert view["project"]["status"] == "awaiting_user"
    # 模型选择被带到每次调用；第二次调用看到了第一次工具的报错
    assert all(c.get("model") == "fake/fake-1" for c in router.calls)
    tool_msgs = [m for m in router.calls[1]["messages"] if m.role == "tool"]
    assert "不允许" in tool_msgs[0].content or "import" in tool_msgs[0].content
    # 系统上下文里有项目简报
    assert "【目标】" in router.calls[0]["messages"][1].content


def test_manual_permission_waits_for_approval_before_edits_and_runs(client):
    """手动模式：模型想写代码，先发审批卡片、不改项目；批准后才落地，再接着对话。"""
    router = ScriptedRouter(
        [
            ChatResult(
                content="我打算这样改。",
                model="m",
                provider="fake",
                tool_calls=[ToolCall(id="1", name="write_strategy", arguments={"code": GOOD, "note": "均线"})],
            ),
            "代码已经提交，接下来可以回测。",
        ]
    )
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    patched = client.patch(f"/api/lab/projects/{p['id']}", json={"permission": "manual"}).json()
    assert patched["project"]["permission"] == "manual"
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "写个均线策略", "wait": True}).json()
    ask = view["messages"][-1]
    assert ask["kind"] == "ask" and ask["data"]["options"] == ["批准", "拒绝"]
    assert ask["data"]["approval"][0]["tool"] == "write_strategy"
    assert view["project"]["current_v"] == 0 and view["project"]["status"] == "awaiting_user"
    view = client.post(f"/api/lab/projects/{p['id']}/decision", json={"choice": "批准", "wait": True}).json()
    assert view["project"]["current_v"] == 1
    assert view["messages"][-1]["content"].startswith("代码已经提交")


def test_plan_permission_hides_edit_tools_and_asks_to_execute(client):
    router = ScriptedRouter(["计划：1. 写均线策略 2. 回测 3. 看回撤"])
    get_services().router = router
    p = _project(client, model="fake/fake-1", permission="plan")
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "做个均线策略", "wait": True}).json()
    names = {t.name for t in router.calls[0]["tools"]}
    assert "write_strategy" not in names and "run_backtest" not in names and "ask_user" in names
    assert "【计划模式】" in router.calls[0]["messages"][-1].content
    assert view["messages"][-1]["data"]["options"] == ["按计划执行", "修改计划"]


def test_autopilot_needs_goal_and_model_gets_slash_tools(client):
    router = ScriptedRouter(["好的，我们先聊聊思路。"])
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    r = client.post(f"/api/lab/projects/{p['id']}/autopilot", json={"wait": True})
    assert r.status_code == 400 and "目标" in r.text
    client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "聊聊", "wait": True})
    names = {t.name for t in router.calls[0]["tools"]}
    assert {"start_autopilot", "pin_note", "compact_history", "save_work"} <= names


def test_fork_rewind_and_conversation_fields(client):
    router = ScriptedRouter(["第一版回答", "重试后的回答", "编辑后的回答"])
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    pid = p["id"]
    view = client.post(f"/api/lab/projects/{pid}/messages", json={"text": "你好", "wait": True}).json()
    user = next(m for m in view["messages"] if m["role"] == "user" and m["content"] == "你好")
    # 重试：删掉之后的回答，按原文重新发送
    view = client.post(f"/api/lab/projects/{pid}/rewind", json={"seq": user["seq"], "wait": True}).json()
    assert view["messages"][-1]["content"] == "重试后的回答"
    assert sum(1 for m in view["messages"] if m["role"] == "user") == 1
    # 编辑：换成新内容
    user = next(m for m in view["messages"] if m["role"] == "user")
    body = {"seq": user["seq"], "text": "改一下", "wait": True}
    view = client.post(f"/api/lab/projects/{pid}/rewind", json=body).json()
    assert [m["content"] for m in view["messages"] if m["role"] == "user"] == ["改一下"]
    # 从用户消息分叉：子对话挂在原对话下，只带到那条消息为止
    user = next(m for m in view["messages"] if m["role"] == "user")
    child = client.post(f"/api/lab/projects/{pid}/fork", json={"seq": user["seq"]}).json()["project"]
    assert child["parent_id"] == pid and child["fork_seq"] == user["seq"]
    cv = client.get(f"/api/lab/projects/{child['id']}").json()
    assert cv["messages"][-2]["content"] == "改一下" and "分叉" in cv["messages"][-1]["content"]
    # 置顶、归入项目
    patched = client.patch(f"/api/lab/projects/{child['id']}", json={"pinned": True, "folder": "BTC 研究"}).json()
    assert patched["project"]["pinned"] and patched["project"]["folder"] == "BTC 研究"


def test_patch_project_can_switch_market_to_auto(client):
    p = _project(client)
    r = client.patch(f"/api/lab/projects/{p['id']}", json={"securities": [], "market_auto": True})
    assert r.status_code == 200, r.text
    assert r.json()["project"]["securities"] == [] and r.json()["project"]["market_auto"] is True
    r = client.patch(f"/api/lab/projects/{p['id']}", json={"securities": ["eth/usdt"], "market_auto": False})
    assert r.json()["project"]["securities"] == ["ETH/USDT"] and r.json()["project"]["market_auto"] is False


def test_new_project_defaults_to_auto_symbol():
    from quant_agent.agents.strategy_lab import new_project

    p = new_project({"idea": "趋势策略"})
    assert p["securities"] == [] and p["market_auto"] is True
    p = new_project({"idea": "趋势策略", "securities": ["eth/usdt"]})
    assert p["securities"] == ["ETH/USDT"] and p["market_auto"] is False
    p = new_project({"idea": "趋势策略", "securities": ["btc/usdt"], "market_auto": True})
    assert p["securities"] == [] and p["market_auto"] is True


def test_suggestions_and_ai_sets_market(client):
    from quant_agent.agents.strategy_lab import split_suggestions

    assert split_suggestions("好的。\n<suggestions>跑一次回测|把止损收紧</suggestions>") == (
        "好的。",
        ["跑一次回测", "把止损收紧"],
    )
    router = ScriptedRouter(
        [
            ChatResult(
                content="先定标的。",
                model="m",
                provider="fake",
                tool_calls=[
                    ToolCall(id="1", name="set_market", arguments={"symbols": ["eth/usdt"], "timeframe": "4h"})
                ],
            ),
            "就用 ETH 4 小时。\n<suggestions>写一版均线策略|/backtest</suggestions>",
        ]
    )
    get_services().router = router
    p = _project(client, model="fake/fake-1", market_auto=True)
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "做个趋势策略", "wait": True}).json()
    assert view["project"]["securities"] == ["ETH/USDT"] and view["project"]["timeframe"] == "4h"
    assert not view["project"]["market_auto"]
    assert any(
        "set_market" in m.content or "标的" in m.content for m in router.calls[0]["messages"] if m.role == "system"
    )
    last = view["messages"][-1]
    assert last["content"] == "就用 ETH 4 小时。" and last["data"]["suggestions"] == ["写一版均线策略", "/backtest"]
    # 关掉建议：不再提示模型
    router.replies = ["好的"]
    client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "嗯", "suggest": False, "wait": True})
    assert not any("<suggestions>" in m.content for m in router.calls[-1]["messages"] if m.role == "system")


def test_roles_get_all_tools_unless_restricted():
    from quant_agent.agents.roles import Role
    from quant_agent.agents.team import TeamRunner, ToolRegistry

    reg = ToolRegistry()
    reg.register("web_search", lambda query="": "ok")
    reg.register("market_summary", lambda query="": "ok")
    runner = TeamRunner(None, None, tools=reg)
    seen = {}

    def fake_make(name):
        seen.setdefault("names", []).append(name)
        return None

    runner._make_tool = fake_make
    for restrict, expect in ((False, {"web_search", "market_summary"}), (True, {"market_summary"})):
        seen.clear()
        role = Role(id="r", name="R", tools=["market_summary"], restrict_tools=restrict)
        try:
            runner._invoke(role, "hi", max_steps=1)
        except Exception:
            pass
        assert set(seen["names"]) == expect


def test_auto_compact_threshold_defaults_and_patch(client):
    p = _project(client, model="fake/fake-1")
    assert p["auto_compact_ratio"] == 0.8
    view = client.get(f"/api/lab/projects/{p['id']}").json()
    ctx = view["context"]
    assert ctx["auto_compact_ratio"] == 0.8
    assert ctx["auto_compact_tokens"] == int(ctx["budget"] * 0.8)

    patched = client.patch(f"/api/lab/projects/{p['id']}", json={"auto_compact_ratio": 0.9}).json()["project"]
    assert patched["auto_compact_ratio"] == 0.9
    ctx = client.get(f"/api/lab/projects/{p['id']}").json()["context"]
    assert ctx["auto_compact_tokens"] == int(ctx["budget"] * 0.9)
    assert client.patch(f"/api/lab/projects/{p['id']}", json={"auto_compact_ratio": 0.99}).status_code == 422


def test_auto_compact_waits_for_configured_threshold(client):
    from quant_agent.api.routes_lab import _lab
    from quant_agent.platform import context

    router = ScriptedRouter(["第一次回答", "摘要：继续做 BTC 研究。", "第二次回答"])
    get_services().router = router
    p = _project(client, model="fake/fake-1", auto_compact_ratio=0.9)
    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws):
        lab = _lab()
        project = lab.store.get(p["id"])
        threshold = lab.auto_compact_tokens(project)
        fixed = lab._fixed_context_tokens(project)
        chunk_tokens = max(500, (threshold - fixed) // 8)
        chunk = "a" * (chunk_tokens * 4)
        for i in range(6):
            lab.store.append(p["id"], {"role": "user" if i % 2 == 0 else "assistant", "kind": "text", "content": chunk})

    first = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "第一次继续", "wait": True}).json()
    assert first["project"]["compacted_before"] == 0

    with context.bind(ws):
        lab = _lab()
        chunk = "a" * (chunk_tokens * 4)
        for i in range(6):
            lab.store.append(p["id"], {"role": "user" if i % 2 == 0 else "assistant", "kind": "text", "content": chunk})

    second = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "第二次继续", "wait": True}).json()
    assert second["project"]["compacted_before"] > 0


def test_stale_running_project_is_recovered(client):
    from quant_agent.agents.strategy_lab import stop_event
    from quant_agent.api.routes_lab import _lab
    from quant_agent.platform import context

    p = _project(client)
    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws):
        lab = _lab()
        lab.store.patch(p["id"], status="running", stop_requested=True, job={"kind": "chat", "phase": "联网搜索"})
        card = lab.store.append(
            p["id"],
            {"role": "tool", "kind": "tool", "content": "", "data": {"name": "web_search", "status": "running"}},
        )
        assert lab.recover_stale(p["id"])
        project = lab.store.get(p["id"])
        assert project["status"] == "idle" and project["job"] is None and project["stop_requested"] is False
        stopped = next(m for m in lab.store.messages(p["id"]) if m["seq"] == card["seq"])
        assert stopped["data"]["status"] == "stopped"
        assert not lab.recover_stale(p["id"])

        lab.store.patch(p["id"], status="running", job={"kind": "chat", "phase": "联网搜索"})
        lab.request_stop(p["id"])
        assert lab.store.get(p["id"])["status"] == "idle"
        stop_event(p["id"]).clear()


def test_cancellable_tool_stops_immediately(client):
    import contextvars
    import threading
    import time

    from quant_agent.agents.strategy_lab import StopRequested, stop_event
    from quant_agent.api.routes_lab import _lab
    from quant_agent.platform import context

    p = _project(client)
    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws):
        lab = _lab()
        stop_event(p["id"]).clear()

        def slow_tool():
            time.sleep(2)
            return {"ok": True}

        ctx = contextvars.copy_context()
        threading.Thread(target=lambda: (time.sleep(0.1), ctx.run(lab.request_stop, p["id"])), daemon=True).start()
        t0 = time.monotonic()
        with pytest.raises(StopRequested):
            lab._tool(p["id"], "web_search", "联网搜索", {"query": "btc"}, slow_tool)
        assert time.monotonic() - t0 < 0.8
        card = [m for m in lab.store.messages(p["id"]) if m.get("kind") == "tool"][-1]
        assert card["data"]["status"] == "stopped"
        stop_event(p["id"]).clear()


def test_auto_compact_before_long_turn(client):
    from quant_agent.api.routes_lab import _lab
    from quant_agent.platform import context

    router = ScriptedRouter(["摘要：研究者要做 BTC 均线。", "继续回答"])
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws):
        store = _lab().store
        for i in range(8):
            role = "user" if i % 2 == 0 else "assistant"
            store.append(p["id"], {"role": role, "kind": "text", "content": "很长的讨论 " * 4000})
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "继续", "wait": True}).json()
    assert any(m["content"].startswith("已把前面") for m in view["messages"])
    assert view["project"]["compacted_before"] > 0


def test_llm_autopilot_revises_with_backtest_feedback(client):
    revised = GOOD.replace('"weight": 0.6', '"weight": 0.4')
    router = ScriptedRouter([f"一句话\n```python\n{GOOD}```", f"降低仓位\n```python\n{revised}```"])
    get_services().router = router
    p = _project(client, goals={"min_sharpe": 3.9}, max_rounds=2)
    view = client.post(f"/api/lab/projects/{p['id']}/autopilot", json={"wait": True}).json()
    assert [v["source"] for v in view["project"]["versions"]] == ["ai", "ai"]
    revise_prompt = router.calls[1]["messages"][-1].content
    assert "最近一次平台回测" in revise_prompt and "夏普" in revise_prompt


def test_running_project_rejects_second_turn(client):
    p = _project(client)
    from quant_agent.agents import strategy_lab as SL
    from quant_agent.agents.strategy_lab import LabStore
    from quant_agent.platform import context

    with context.bind(client.headers["X-Workspace-Id"]):
        LabStore(get_services().store).patch(p["id"], status="running")
        # 模拟真实的执行中回合：只有 _ACTIVE_RUNS 里存在的项目才不会被 recover_stale 回收。
        with SL._RUNS_GUARD:
            SL._ACTIVE_RUNS.add(p["id"])
    try:
        r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "hi"})
    finally:
        with SL._RUNS_GUARD:
            SL._ACTIVE_RUNS.discard(p["id"])
    assert r.status_code == 409


def test_background_turn_streams_messages(client):
    """不带 wait：回合在后台线程执行（继承工作区上下文），前端用 after 增量轮询。"""
    import time

    p = _project(client, max_rounds=1, goals={"min_sharpe": 0.5})
    r = client.post(f"/api/lab/projects/{p['id']}/autopilot", json={})
    assert r.status_code == 200 and r.json()["project"]["status"] == "running"
    seen, after = [], 0
    deadline = time.time() + 90
    while time.time() < deadline:
        v = client.get(f"/api/lab/projects/{p['id']}?after={after}").json()
        seen += v["messages"]
        if v["messages"]:
            after = max(m["seq"] for m in v["messages"])
        if v["project"]["status"] != "running":
            break
        time.sleep(0.3)
    assert v["project"]["status"] == "awaiting_user"
    assert any(m["kind"] == "backtest" for m in seen)


class StreamingRouter(ScriptedRouter):
    """支持 on_delta / effort 的路由：把回复拆成几段流式回调，并附带思考摘要。"""

    def chat(self, messages, *, on_delta=None, effort=None, **kw):
        result = super().chat(messages, on_delta=on_delta, effort=effort, **kw)
        if on_delta:
            on_delta("thinking", "先确认均线参数。")
            for part in (result.content[: len(result.content) // 2], result.content[len(result.content) // 2 :]):
                if part:
                    on_delta("text", part)
        result.thinking = "先确认均线参数。"
        result.provider_blocks = [{"type": "thinking", "thinking": "先确认均线参数。", "signature": "s"}]
        return result

    def current_target(self, role="default"):
        return "fake/fake-1"


def test_chat_streams_into_one_message_with_thinking_and_effort(client):
    router = StreamingRouter(
        [
            ChatResult(
                content="我来回测。",
                model="m",
                provider="fake",
                tool_calls=[ToolCall(id="1", name="write_strategy", arguments={"code": GOOD, "note": "均线"})],
            ),
            ChatResult(content="已写好第一版，夏普见上方卡片。", model="m", provider="fake"),
        ]
    )
    get_services().router = router
    p = _project(client)
    view = client.post(
        f"/api/lab/projects/{p['id']}/messages",
        json={"text": "写个均线策略", "model": "fake/fake-1", "effort": "high", "wait": True},
    ).json()
    assert view["project"]["effort"] == "high" and view["project"]["model"] == "fake/fake-1"
    assert all(c["effort"] == "high" and c["on_delta"] for c in router.calls)
    texts = [m for m in view["messages"] if m["role"] == "assistant" and m["kind"] == "text"]
    assert [m["content"] for m in texts] == ["我来回测。", "已写好第一版，夏普见上方卡片。"]
    assert all(m["data"]["streaming"] is False and m["data"]["thinking"] for m in texts)
    assert texts[0]["model"] == "fake/m"
    # 第二步把第一步的 thinking 块原样带回
    assistant_turn = [m for m in router.calls[1]["messages"] if m.role == "assistant" and m.tool_calls][-1]
    assert assistant_turn.provider_blocks[0]["signature"] == "s"
    assert view["context"]["model"] == "fake/fake-1"


def test_routers_without_streaming_still_work(client):
    """旧式路由（只认识固定参数）不会被传入 effort / on_delta。"""

    class Plain:
        def __init__(self):
            self.kw = None

        def chat(self, messages, role="default", model=None, temperature=0.2, tools=None, max_tokens=None):
            self.kw = {"role": role, "model": model}
            return ChatResult(content="好的", model="m", provider="plain")

        def available_models(self):
            return []

        def _has_credentials(self, _):
            return True

    router = Plain()
    get_services().router = router
    p = _project(client)
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "你好", "wait": True}).json()
    assert view["messages"][-1]["content"] == "好的" and router.kw["role"] == "code_gen"


def test_compact_history_pins_summary_and_drops_old_turns(client):
    router = ScriptedRouter(["第一条回答", "第二条回答", "研究者想要稳健的 BTC 均线策略，已排除网格。"])
    get_services().router = router
    p = _project(client)
    for text in ("第一问", "第二问"):
        client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": text, "wait": True})
    view = client.post(f"/api/lab/projects/{p['id']}/compact").json()
    pins = view["project"]["pins"]
    assert pins[-1]["kind"] == "summary" and "稳健" in pins[-1]["text"]
    assert view["project"]["compacted_before"] > 0
    assert any("摘要" in i["title"] for i in view["context"]["items"])
    assert "（最近 0 条原文）" in next(i["title"] for i in view["context"]["items"] if i["key"] == "history")
    assert client.post(f"/api/lab/projects/{p['id']}/compact").status_code == 400


def test_events_stream_pushes_updates_and_closes(client):
    p = _project(client)
    with client.stream("GET", f"/api/lab/projects/{p['id']}/events?timeout=10") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        lines = [ln for ln in r.iter_lines() if ln]
    data = [json.loads(ln[5:]) for ln in lines if ln.startswith("data:") and ln != "data: {}"]
    assert data[0]["project"]["id"] == p["id"] and data[0]["messages"], "首包带上已有消息"
    assert lines[-2:] == ["event: done", "data: {}"]


# ---------------- 图片附件 ----------------
PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class VisionRouter(ScriptedRouter):
    def __init__(self, replies, inputs):
        super().__init__(replies)
        self.inputs = inputs

    def available_models(self):
        return [{**super().available_models()[0], "inputs": self.inputs}]


def test_message_images_sent_on_user_turn_and_stored(client):
    router = VisionRouter(["看到了一张 K 线截图", "第二条回答"], ["text", "image"])
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    r = client.post(
        f"/api/lab/projects/{p['id']}/messages",
        json={"text": "", "images": [{"data_url": PNG, "name": "k.png"}], "wait": True},
    )
    assert r.status_code == 200, r.text
    user = [m for m in r.json()["messages"] if m["role"] == "user"][-1]
    ref = user["data"]["images"][0]
    assert ref["mime"] == "image/png" and ref["name"] == "k.png" and "url" not in ref  # 线程里只留引用
    last = router.calls[0]["messages"][-1]
    assert last.role == "user" and last.images == [PNG]
    img = client.get(f"/api/lab/projects/{p['id']}/images/{ref['id']}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content.startswith(b"\x89PNG")
    assert client.get(f"/api/lab/projects/other/images/{ref['id']}").status_code == 404
    # 下一轮：历史里的图片不重发，只留一句说明
    client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "继续", "wait": True})
    hist = router.calls[1]["messages"]
    assert not any(m.images for m in hist) and any("附图 1 张" in m.content for m in hist if m.role == "user")


def test_message_images_rejected_when_model_lacks_image_input(client):
    get_services().router = VisionRouter(["不该被调用"], ["text"])
    p = _project(client, model="fake/fake-1")
    url = f"/api/lab/projects/{p['id']}/messages"
    r = client.post(url, json={"text": "看图", "images": [{"data_url": PNG}], "wait": True})
    assert r.status_code == 400 and "图片输入" in r.text
    bad = client.post(url, json={"text": "看图", "images": [{"data_url": "data:text/plain;base64,aGk="}]})
    assert bad.status_code == 422
    assert client.post(url, json={"text": "  "}).status_code == 422
    too_many = client.post(url, json={"text": "x", "images": [{"data_url": PNG}] * 5})
    assert too_many.status_code == 422


def test_images_survive_retry_and_fork(client):
    router = VisionRouter(["第一次", "重试后"], ["text", "image"])
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    pid = p["id"]
    view = client.post(
        f"/api/lab/projects/{pid}/messages", json={"text": "看图", "images": [{"data_url": PNG}], "wait": True}
    ).json()
    user = next(m for m in view["messages"] if m["role"] == "user" and m["data"].get("images"))
    iid = user["data"]["images"][0]["id"]
    # 重试：附图一起重新发给模型，原图保留
    view = client.post(f"/api/lab/projects/{pid}/rewind", json={"seq": user["seq"], "wait": True}).json()
    assert router.calls[-1]["messages"][-1].images == [PNG]
    assert client.get(f"/api/lab/projects/{pid}/images/{iid}").status_code == 200
    # 分叉：子对话有自己的图片副本；删掉子对话不影响原对话的图片
    user = next(m for m in view["messages"] if m["role"] == "user" and m["data"].get("images"))
    child = client.post(f"/api/lab/projects/{pid}/fork", json={"seq": user["seq"]}).json()["project"]
    cmsg = next(m for m in client.get(f"/api/lab/projects/{child['id']}").json()["messages"] if m["data"].get("images"))
    cid = cmsg["data"]["images"][0]["id"]
    assert cid != iid and client.get(f"/api/lab/projects/{child['id']}/images/{cid}").status_code == 200
    client.delete(f"/api/lab/projects/{child['id']}")
    assert client.get(f"/api/lab/projects/{pid}/images/{iid}").status_code == 200


def _ask_direction(client, router_tail):
    router = ScriptedRouter(
        [
            ChatResult(
                content="有两个方向可以试。",
                model="m",
                provider="fake",
                tool_calls=[
                    ToolCall(
                        id="1", name="ask_user", arguments={"question": "选哪个方向？", "options": ["均值回归", "突破"]}
                    )
                ],
            ),
            *router_tail,
        ]
    )
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "帮我想想方向", "wait": True})
    return router, p["id"]


def test_picking_an_ask_option_hands_the_turn_back_to_the_model(client, monkeypatch):
    """点选 AI 提的方向后，AI 要接着做，而不是只记一条用户消息就停下。
    回合进行中状态必须保持 running：前端看到 idle 就停止跟随，AI 的回答不会出现在页面上。"""
    from quant_agent.agents.strategy_lab import StrategyLab

    router, pid = _ask_direction(client, ["好的，按均值回归来写。"])
    statuses = []
    orig = StrategyLab._phase

    def spy(self, p, phase, detail=None):
        statuses.append((self.store.get(p) or {}).get("status"))
        return orig(self, p, phase, detail)

    monkeypatch.setattr(StrategyLab, "_phase", spy)
    view = client.post(f"/api/lab/projects/{pid}/decision", json={"choice": "均值回归", "wait": True}).json()
    assert view["messages"][-1]["role"] == "assistant" and "均值回归" in view["messages"][-1]["content"]
    assert router.calls[-1]["messages"][-1].content.startswith("均值回归")
    assert statuses and set(statuses) == {"running"}, statuses
    assert view["project"]["status"] == "idle"


def test_custom_long_answer_to_ask_is_accepted(client):
    """不选选项、自己写一段较长的方向，也能发出去并由 AI 接着做。"""
    _, pid = _ask_direction(client, ["收到，按你的方向来。"])
    mine = "换成 4 小时级别的资金费率反转：费率连续三期为正且价格创新高时做空，止损 3%，只在波动率不高时开仓"
    assert len(mine) > 40
    view = client.post(f"/api/lab/projects/{pid}/decision", json={"choice": mine, "wait": True}).json()
    assert any(m["role"] == "user" and m["content"] == mine for m in view["messages"])
    assert view["messages"][-1]["content"] == "收到，按你的方向来。"
    ask = next(m for m in view["messages"] if m["kind"] == "ask")
    assert ask["data"]["answered"] == mine


def test_rewind_never_reuses_message_numbers(client):
    """编辑后新消息的序号必须比删掉的都大：前端按「序号更大」增量拉取，复用序号会漏掉 AI 的回答。"""
    get_services().router = ScriptedRouter(["第一版回答", "编辑后的回答"])
    p = _project(client, model="fake/fake-1")
    pid = p["id"]
    view = client.post(f"/api/lab/projects/{pid}/messages", json={"text": "你好", "wait": True}).json()
    top = max(m["seq"] for m in view["messages"])
    user = next(m for m in view["messages"] if m["role"] == "user" and m["content"] == "你好")
    client.post(f"/api/lab/projects/{pid}/rewind", json={"seq": user["seq"], "text": "改一下", "wait": True})
    fresh = client.get(f"/api/lab/projects/{pid}", params={"after": top}).json()["messages"]
    assert [m["content"] for m in fresh] == ["改一下", "编辑后的回答"]


def test_turn_progress_says_what_the_agent_is_doing(client, monkeypatch):
    """回合进度要说清在做什么：第一步写模型与上下文，调用工具后写「根据 X 的结果继续推理」。"""
    from quant_agent.agents.strategy_lab import StrategyLab

    seen = []
    orig = StrategyLab._phase

    def spy(self, pid, phase, detail=None):
        seen.append((phase, detail or ""))
        return orig(self, pid, phase, detail)

    monkeypatch.setattr(StrategyLab, "_phase", spy)
    get_services().router = ScriptedRouter(
        [
            ChatResult(
                content="",
                model="m",
                provider="fake",
                tool_calls=[ToolCall(id="1", name="write_strategy", arguments={"code": GOOD, "note": "均线"})],
            ),
            "写好了。",
        ]
    )
    p = _project(client, model="fake/fake-1")
    client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "写个均线策略", "wait": True})
    assert "规划" in seen[0][0] and "fake-1" in seen[0][1]
    assert any("写代码" in ph and "已调用 1 个工具" in d for ph, d in seen), seen


def _tc(i, name, **args):
    return ChatResult(
        content="", model="m", provider="fake", tool_calls=[ToolCall(id=str(i), name=name, arguments=args)]
    )


def test_plan_runs_step_by_step_until_done(client):
    """/plan：AI 列计划，然后不停地一步步执行，全部完成才停；计划卡片随进度更新。"""
    router = ScriptedRouter(
        [
            _tc(1, "set_plan", task="比较两种出场", steps=["写固定止损版", "写移动止损版"], done_when="两版都回测过"),
            "计划列好了，开始第 1 步。",
            _tc(2, "update_plan_step", index=1, status="done", note="固定止损夏普 0.8"),
            "第 1 步完成。",
            _tc(3, "update_plan_step", index=2, status="done", note="移动止损夏普 1.1"),
            "第 2 步完成，移动止损更好。",
        ]
    )
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    view = client.post(f"/api/lab/projects/{p['id']}/plan", json={"text": "比较两种出场", "wait": True}).json()
    plan = view["project"]["plan"]
    assert plan["status"] == "done" and [s["status"] for s in plan["steps"]] == ["done", "done"]
    card = next(m for m in view["messages"] if m["kind"] == "plan")
    assert [s["status"] for s in card["data"]["plan"]["steps"]] == ["done", "done"]
    assert any(m["kind"] == "text" and m["content"].startswith("计划完成") for m in view["messages"])
    # 计划推进时的提示里带着当前步骤，并且模型看得到计划与功能说明
    prompts = [m.content for c in router.calls for m in c["messages"] if m.role == "user"]
    assert any("第 2 步" in t and "写移动止损版" in t for t in prompts)
    systems = "\n".join(m.content for m in router.calls[-1]["messages"] if m.role == "system")
    assert (
        "【当前计划" in systems
        and "start_discussion" in systems
        and "schedule_task" in systems
        and "queue_night" in systems
    )
    names = {t.name for t in router.calls[0]["tools"]}
    assert {
        "set_plan",
        "update_plan_step",
        "start_discussion",
        "schedule_task",
        "queue_night",
        "context_usage",
    } <= names
    assert view["project"]["status"] == "idle"


def test_plan_stops_when_no_progress(client):
    router = ScriptedRouter(
        [_tc(1, "set_plan", steps=["第一步", "第二步"]), "列好了。", "还在想。", "还是没做。", "不应该再被调用"]
    )
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    view = client.post(f"/api/lab/projects/{p['id']}/plan", json={"text": "做点事", "wait": True}).json()
    assert view["project"]["plan"]["autorun"] is False
    assert "没有进展" in view["messages"][-1]["content"]
    assert router.replies == ["不应该再被调用"]


def test_ai_can_start_a_plan_in_normal_chat(client):
    """普通对话里 AI 自己判断任务需要多步，用 set_plan 列计划后会接着执行，不等研究者输入 /plan。"""
    router = ScriptedRouter(
        [
            # 模型有时把步骤写成对象：要取出文字，不能把 dict 原样显示
            _tc(1, "set_plan", steps=[{"step": "只有一步", "done_when": "跑完"}], execute=True),
            "我列了计划，马上开始。",
            _tc(2, "update_plan_step", index=1, status="done"),
            "做完了。",
        ]
    )
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "帮我系统地做完", "wait": True}).json()
    assert view["project"]["plan"]["status"] == "done"
    assert view["project"]["plan"]["steps"][0]["text"] == "只有一步（完成标准：跑完）"
    assert not router.replies


def test_stopping_a_plan_pauses_it(client):
    """停止后计划暂停：之后随便聊一句，不会又自动接着跑计划。"""
    from quant_agent.agents.strategy_lab import StopRequested

    class StopAfterPlan(ScriptedRouter):
        def chat(self, messages, **kw):
            if len(self.calls) == 2 and not getattr(self, "stopped", False):
                self.stopped = True
                raise StopRequested()
            return super().chat(messages, **kw)

    router = StopAfterPlan([_tc(1, "set_plan", steps=["一", "二"]), "列好了。", "随便聊聊的回答"])
    get_services().router = router
    p = _project(client, model="fake/fake-1")
    view = client.post(f"/api/lab/projects/{p['id']}/plan", json={"text": "做事", "wait": True}).json()
    assert view["project"]["plan"]["autorun"] is False and view["project"]["plan"]["status"] == "active"
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "随便聊聊", "wait": True}).json()
    assert view["messages"][-1]["content"] == "随便聊聊的回答"
    assert view["project"]["plan"]["steps"][0]["status"] in ("pending", "doing")


def test_message_feedback_is_saved_and_can_be_toggled(client):
    """消息点赞 / 评分落库，刷新后仍能读回；再次提交相同内容会取消。"""
    p = _project(client)
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "你好", "wait": True}).json()
    msg = next(m for m in reversed(view["messages"]) if m["role"] == "assistant" and m["kind"] == "text")
    url = f"/api/lab/projects/{p['id']}/messages/{msg['seq']}/feedback"

    first = client.post(url, json={"rating": 1, "comment": "有帮助", "score": 5})
    assert first.status_code == 200, first.text
    assert first.json()["feedback"] == {"rating": 1, "comment": "有帮助", "score": 5}

    stored = client.get(f"/api/lab/projects/{p['id']}").json()
    saved = next(m for m in stored["messages"] if m["seq"] == msg["seq"])
    assert saved["feedback"] == {"rating": 1, "comment": "有帮助", "score": 5}

    # 同一评分、意见和星级再次提交 = 取消评价
    assert client.post(url, json={"rating": 1, "comment": "有帮助", "score": 5}).json()["feedback"] is None
    assert "feedback" not in next(
        m for m in client.get(f"/api/lab/projects/{p['id']}").json()["messages"] if m["seq"] == msg["seq"]
    )

    updated = client.post(url, json={"rating": -1, "comment": "不准确", "score": 2}).json()["feedback"]
    assert updated == {"rating": -1, "comment": "不准确", "score": 2}
    # 点踩按钮只带 rating：保留评分卡写下的意见与星级
    kept = client.post(url, json={"rating": 1}).json()["feedback"]
    assert kept == {"rating": 1, "comment": "不准确", "score": 2}
    # 再点一次相同评价才取消
    assert client.post(url, json={"rating": 1}).json()["feedback"] is None
    assert client.post(url, json={"rating": -1, "comment": "不准确", "score": 2}).status_code == 200

    # 管理员可以在存储分类里预览并清理评价数据
    import time

    time.sleep(0.01)
    scan = client.get("/api/workspace/storage").json()
    assert any(c["key"] == "feedback" for c in scan["categories"])
    dry = client.post(
        "/api/workspace/storage/cleanup", json={"category": "feedback", "keep_days": 0, "dry_run": True}
    ).json()
    assert dry["deleted"]["records"] >= 1
    real = client.post(
        "/api/workspace/storage/cleanup", json={"category": "feedback", "keep_days": 0, "dry_run": False}
    ).json()
    assert real["deleted"]["records"] >= 1
    after = client.get(f"/api/lab/projects/{p['id']}").json()
    assert "feedback" not in next(m for m in after["messages"] if m["seq"] == msg["seq"])
