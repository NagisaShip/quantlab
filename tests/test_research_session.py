"""通用 ResearchSession：LabProject 的 kind / context / origin、Ask→研究升级接口、Ask 的会话升级建议。

只覆盖后端增量：
- new_project 泛化（默认 kind=strategy 行为不变，research / workflow 是轻会话）；
- POST /api/lab/projects 支持 kind=research / workflow + context（不写策略代码、不设验收目标）；
- POST /api/lab/projects/from-ask 写入种子消息、默认私有、研究信封引用合并到 context；
- /api/ai/ask 与 /api/ai/ask/stream 的 suggest_session / reason（不自动建项目）。
"""

from __future__ import annotations

import json

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents import research_profile as RP
from quant_agent.agents.lab_state import MAX_CONTEXT_STR, new_project, normalize_context
from quant_agent.agents.strategy_lab import LAB_SYSTEM, research_brief
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.api.routes_lab import MAX_SEED_CONTENT, MAX_SEED_MESSAGES
from quant_agent.llm.base import ChatResult


def _clear_keys(monkeypatch) -> None:
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    _clear_keys(monkeypatch)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("QAGENT_OPEN_REGISTRATION", raising=False)
    _clear_keys(monkeypatch)
    set_services(AppServices.build(store_root=str(tmp_path)))
    yield create_app()
    reset_services()


def _team(app):
    """一个工作区管理员 + 两个成员，用来验证默认私有的跨用户不可见。"""
    admin = TestClient(app)
    login(admin)
    users = {"admin": (admin, admin.get("/api/auth/me").json()["user"]["id"])}
    for key, email, name in (
        ("a", "research-a@example.com", "研究员甲"),
        ("b", "research-b@example.com", "研究员乙"),
    ):
        code = admin.post("/api/workspace/invites", json={"role": "member"}).json()["invite"]["code"]
        c = TestClient(app)
        me = login(c, email=email, password="Member01!", name=name, invite_code=code)
        users[key] = (c, me["user"]["id"])
    return users


def _visibility(client, pid: str) -> str:
    return client.get(f"/api/collab/shares/project/{pid}").json()["share"]["visibility"]


def _events(text: str) -> tuple[list[dict], bool]:
    out, done = [], False
    for block in text.split("\n\n"):
        if block.startswith("event: done"):
            done = True
        for line in block.split("\n"):
            if line.startswith("data:") and not block.startswith("event:"):
                out.append(json.loads(line[5:]))
    return out, done


# ---------------------------------------------------------------------------
# new_project：默认策略行为不变，研究 / 工作流是轻会话
# ---------------------------------------------------------------------------
def test_new_project_generalizes_kind_context_origin():
    base = new_project({})
    assert base["kind"] == "strategy" and base["origin"] == "created"
    assert base["context"] == {} and base["goals"]["min_sharpe"] == 1.0
    assert base["title"] == "未命名策略项目"

    research = new_project(
        {
            "kind": "research",
            "origin": "ask_upgrade",
            "idea": "完整研究动量因子",
            "context": {"workflow": {"id": "wf-1"}, "note": "x" * (MAX_CONTEXT_STR + 500)},
        }
    )
    assert research["kind"] == "research" and research["origin"] == "ask_upgrade"
    assert research["goals"] == {} and research["goal_set"] is False  # 轻会话没有验收目标
    assert research["context"]["workflow"]["id"] == "wf-1"
    assert len(research["context"]["note"]) == MAX_CONTEXT_STR  # context 原样但限长

    # 未知 kind / origin 回落到安全默认值；非字典 context 丢弃
    fallback = new_project({"kind": "nope", "origin": "nope", "context": ["not-a-dict"]})
    assert fallback["kind"] == "strategy" and fallback["origin"] == "created" and fallback["context"] == {}

    # 结构过大时明确标注截断而不是写爆存储
    huge = normalize_context({f"k{i}": "y" * MAX_CONTEXT_STR for i in range(20)})
    assert huge.get("_truncated") is True and huge.get("_bytes", 0) > 20_000


# ---------------------------------------------------------------------------
# 创建 research / workflow 项目：轻项目，默认私有，现有策略行为不变
# ---------------------------------------------------------------------------
def test_create_research_and_workflow_projects_are_light(client):
    body = client.post(
        "/api/lab/projects",
        json={
            "kind": "research",
            "title": "研究：动量",
            "idea": "把动量和波动率放在一起看",
            "context": {"workflow": {"id": "wf-1"}, "run": {"id": "run-1"}},
            "instructions": "创建时就带上项目规则。",
        },
    )
    assert body.status_code == 200, body.text
    view = body.json()
    p = view["project"]
    assert p["kind"] == "research" and p["origin"] == "created"
    assert p["instructions"] == "创建时就带上项目规则。"
    assert p["context"]["workflow"]["id"] == "wf-1" and p["context"]["run"]["id"] == "run-1"
    assert p["goals"] == {} and p["goal_set"] is False  # 不写验收目标
    assert p["code"] == "" and p["versions"] == [] and p["current_v"] == 0  # 不写策略代码
    assert any(m["role"] == "event" and "研究会话已创建" in m["content"] for m in view["messages"])
    assert _visibility(client, p["id"]) == "private"

    wf = client.post("/api/lab/projects", json={"kind": "workflow", "title": "工作流升级"}).json()["project"]
    assert wf["kind"] == "workflow" and wf["goals"] == {} and wf["context"] == {}

    # 现有策略项目：kind=strategy，默认目标与版本行为完全不变
    legacy = client.post(
        "/api/lab/projects",
        json={
            "idea": "BTC 1 小时双均线金叉",
            "securities": ["BTC/USDT"],
            "market_auto": False,
            "data_source": "synthetic",
            "bars_limit": 600,
        },
    ).json()["project"]
    assert legacy["kind"] == "strategy" and legacy["origin"] == "created"
    assert legacy["goals"]["min_sharpe"] == 1.0 and legacy["current_v"] == 0
    assert legacy["context"] == {}


# ---------------------------------------------------------------------------
# POST /api/lab/projects/from-ask：种子消息、默认私有、研究引用合并
# ---------------------------------------------------------------------------
def test_from_ask_seeds_messages_and_is_private_by_default(client):
    body = client.post(
        "/api/lab/projects/from-ask",
        json={
            "title": "从问 AI 升级",
            "idea": "把这次讨论继续成完整研究",
            "context": {"page": {"title": "回测详情"}, "raw": "x" * (MAX_CONTEXT_STR + 100)},
            "messages": [
                {"role": "user", "content": "动量因子最近三个月表现如何？"},
                {"role": "assistant", "content": "可以按多步研究推进。", "data": {"note": "seed"}},
                {"role": "system", "content": "不应写入"},  # 未知角色过滤
                {"role": "user", "content": "   "},  # 空内容过滤
            ],
        },
    )
    assert body.status_code == 200, body.text
    view = body.json()
    p = view["project"]
    assert p["kind"] == "research" and p["origin"] == "ask_upgrade"
    assert p["context"]["page"]["title"] == "回测详情"
    assert len(p["context"]["raw"]) == MAX_CONTEXT_STR
    msgs = view["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "动量因子最近三个月表现如何？"),
        ("assistant", "可以按多步研究推进。"),
    ]
    assert [m["seq"] for m in msgs] == [1, 2]
    assert msgs[0]["created_at"] <= msgs[1]["created_at"]
    assert msgs[1]["data"]["note"] == "seed"
    assert _visibility(client, p["id"]) == "private"

    # 增量拉取与详情接口照常工作
    detail = client.get(f"/api/lab/projects/{p['id']}?after=1").json()
    assert [m["seq"] for m in detail["messages"]] == [2]
    assert detail["project"]["kind"] == "research"

    # 最多 40 条种子消息：超出时保留最近的 40 条，未知角色不占名额，也不让升级请求失败
    many = [{"role": "user", "content": f"m{i}"} for i in range(45)] + [{"role": "tool", "content": "不应写入"}]
    capped = client.post("/api/lab/projects/from-ask", json={"messages": many})
    assert capped.status_code == 200, capped.text
    rows = capped.json()["messages"]
    assert len(rows) == MAX_SEED_MESSAGES
    assert rows[0]["content"] == "m5" and rows[-1]["content"] == "m44"

    # 单条内容限长：超长种子写入时截断
    long_rows = client.post(
        "/api/lab/projects/from-ask",
        json={"messages": [{"role": "user", "content": "x" * (MAX_SEED_CONTENT + 500)}]},
    ).json()["messages"]
    assert len(long_rows) == 1 and len(long_rows[0]["content"]) == MAX_SEED_CONTENT


def test_from_ask_merges_research_references(client):
    research = {
        "version": 1,
        "workflow": {"id": "wf-9", "title": "动量研究", "revision": 3},
        "selected_node": {"id": "n1", "title": "特征", "kind": "factor", "status": "completed"},
        "run": {"id": "run-1", "mode": "research", "status": "COMPLETED"},
        "outputs": [{"port": "data", "type": "table", "hash": "abc", "run_id": "run-1", "node_id": "n1"}],
    }
    r = client.post(
        "/api/lab/projects/from-ask",
        json={"kind": "workflow", "title": "工作流升级", "research": research},
    )
    assert r.status_code == 200, r.text
    p = r.json()["project"]
    assert p["kind"] == "workflow"
    ctx = p["context"]
    assert ctx["workflow"]["id"] == "wf-9" and ctx["workflow"]["revision"] == 3
    assert ctx["run"]["id"] == "run-1" and ctx["node"]["id"] == "n1"
    assert ctx["outputs"][0]["hash"] == "abc"

    # 显式传入的 context 优先，研究信封只补缺失的引用
    explicit = client.post(
        "/api/lab/projects/from-ask",
        json={"context": {"workflow": {"id": "explicit-wf"}}, "research": research},
    ).json()["project"]
    assert explicit["context"]["workflow"]["id"] == "explicit-wf"
    assert explicit["context"]["run"]["id"] == "run-1"


def test_from_ask_private_visible_only_to_owner(app):
    users = _team(app)
    a, _a_id = users["a"]
    b, _b_id = users["b"]
    r = a.post(
        "/api/lab/projects/from-ask",
        json={"title": "私密研究", "messages": [{"role": "user", "content": "只给我看"}]},
    )
    assert r.status_code == 200, r.text
    pid = r.json()["project"]["id"]
    assert _visibility(a, pid) == "private"
    assert b.get(f"/api/lab/projects/{pid}").status_code == 403
    assert all(p["id"] != pid for p in b.get("/api/lab/projects").json()["projects"])
    # 作者改用现有分享体系公开后，其他成员可见（可见性没有另造一套）
    assert a.put(f"/api/collab/shares/project/{pid}", json={"visibility": "workspace"}).status_code == 200
    assert b.get(f"/api/lab/projects/{pid}").status_code == 200


# ---------------------------------------------------------------------------
# Ask 升级建议：仅「research + 多步研究意图」触发，不自动建项目
# ---------------------------------------------------------------------------
def test_ask_suggest_session_only_with_research_and_intent(client, monkeypatch):
    from quant_agent.api import routes_ai

    # 研究信封的解析依赖真实工作流；这里只验证建议逻辑，替换成已解析的最小上下文。
    monkeypatch.setattr(routes_ai, "_research", lambda payload: {"workflow": {"id": "wf-demo", "title": "演示研究"}})
    research = {"version": 1, "workflow": {}}

    before = client.get("/api/lab/projects").json()["projects"]

    triggered = client.post(
        "/api/ai/ask",
        json={"question": "帮我做一份完整的动量研究报告，包含回测实验", "research": research},
    )
    assert triggered.status_code == 200, triggered.text
    body = triggered.json()
    assert body["suggest_session"] is True and body["reason"]

    # 普通事实问答不触发
    plain = client.post("/api/ai/ask", json={"question": "夏普比率是什么意思？", "research": research}).json()
    assert plain["suggest_session"] is False and plain["reason"] == ""

    # 没有 research 上下文时，即使问题像多步任务也不触发（不打扰普通问 AI）
    no_ctx = client.post("/api/ai/ask", json={"question": "帮我做一份完整研究报告"}).json()
    assert no_ctx["suggest_session"] is False and no_ctx["reason"] == ""

    # 只给建议，不自动创建项目
    assert client.get("/api/lab/projects").json()["projects"] == before


def test_ask_stream_final_event_carries_suggestion(client, monkeypatch):
    from quant_agent.api import routes_ai

    monkeypatch.setattr(routes_ai, "_research", lambda payload: {"workflow": {"id": "wf-demo", "title": "演示研究"}})
    research = {"version": 1, "workflow": {}}
    text = client.post("/api/ai/ask/stream", json={"question": "把这个想法做成完整研究任务", "research": research}).text
    events, done = _events(text)
    assert done and events[-1]["type"] == "final"
    assert events[-1]["suggest_session"] is True and events[-1]["reason"]

    plain = client.post("/api/ai/ask/stream", json={"question": "夏普比率怎么算？", "research": research}).text
    plain_events, _ = _events(plain)
    assert plain_events[-1]["type"] == "final"
    assert plain_events[-1]["suggest_session"] is False and plain_events[-1]["reason"] == ""


def test_session_suggestion_helper():
    from quant_agent.api.routes_ai import session_suggestion

    assert session_suggestion("帮我做一份完整研究报告", False) == (False, "")
    assert session_suggestion("夏普比率是什么？", True) == (False, "")
    ok, reason = session_suggestion("分析整个策略从数据到结论的过程", True)
    assert ok is True and "多步研究" in reason


# ---------------------------------------------------------------------------
# Research / Workflow profile：不再被「策略 / 回测」提示词绑架
# ---------------------------------------------------------------------------
def _research_project(client, **kw):
    body = {"kind": "research", "title": "动量研究", "idea": "把动量和波动率放在一起看", **kw}
    r = client.post("/api/lab/projects", json=body)
    assert r.status_code == 200, r.text
    return r.json()["project"]


def _project_prompt(client, pid: str):
    """在请求工作区上下文里取项目 / 提示词快照：TestClient 调用之间没有工作区绑定。"""
    from quant_agent.api.routes_lab import _lab
    from quant_agent.platform import context

    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws):
        lab = _lab()
        project = dict(lab.store.get(pid) or {})
        msgs = lab.build_messages(project)
        fixed_tokens = lab._fixed_context_tokens(project)
    return lab, project, msgs, fixed_tokens


class _CaptureRouter:
    """只记录收到的消息、返回一句固定回答的模型替身。"""

    def __init__(self, reply: str = "好的，我先列计划。") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, messages, **kw):
        self.calls.append({"messages": list(messages), **kw})
        return ChatResult(content=self.reply, model="fake-1", provider="fake")

    def available_models(self):
        return [{"provider": "fake", "model": "fake-1", "target": "fake/fake-1", "ready": True}]

    def current_target(self, role="default"):
        return "fake/fake-1"


def test_research_profile_prompt_is_generic_research_not_strategy():
    assert RP.is_research_kind("research") and RP.is_research_kind("workflow")
    assert not RP.is_research_kind("strategy") and not RP.is_research_kind(None)

    system = RP.RESEARCH_SYSTEM
    # 通用研究 / 交付 profile 的关键约束都在
    for phrase in (
        "先列计划",
        "不是策略研发",
        "证据",
        "不得编造数据",
        "绝不猜测或虚构",
        "明确要求",
        "write_report",
        "local_*",
    ):
        assert phrase in system, phrase
    # 不再夹带策略室的硬约束
    for phrase in ("平台 SDK 与铁律", "提交成功后通常应 run_backtest", "不要在回答里重复贴整份代码"):
        assert phrase not in system, phrase
    assert system != LAB_SYSTEM

    guide = RP.RESEARCH_TOOL_GUIDE
    for phrase in ("workflow_inspect", "workflow_artifact", "local_*", "仅当研究者明确要求"):
        assert phrase in guide, phrase


def test_research_chat_prompt_uses_profile_and_injects_instructions(client):
    p = _research_project(client)
    rules = "只研究美元稳定币；不要动策略代码；报告里必须列出数据来源。"
    patched = client.patch(f"/api/lab/projects/{p['id']}", json={"instructions": rules})
    assert patched.status_code == 200, patched.text
    assert patched.json()["project"]["instructions"] == rules

    router = _CaptureRouter()
    get_services().router = router
    view = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "帮我做一份完整研究", "wait": True})
    assert view.status_code == 200, view.text
    systems = [m.content for m in router.calls[0]["messages"] if m.role == "system"]
    joined = "\n".join(systems)
    # 基础系统提示 = 通用研究 profile；instructions 以固定系统块注入并声明优先级
    assert RP.RESEARCH_SYSTEM in systems
    assert rules in joined and "以平台安全规则为准" in joined
    # 上下文块是研究会话形态：没有策略目标 / 最近回测，也没有策略室铁律
    assert "【研究会话】" in joined
    assert "【验收目标】" not in joined and "【最近回测】" not in joined
    assert "提交成功后通常应 run_backtest" not in joined
    # 工具说明换成通用研究版
    assert "workflow_inspect" in joined and "local_*" in joined

    lab, project, _, _ = _project_prompt(client, p["id"])
    assert lab.system_prompt_for(project) == RP.RESEARCH_SYSTEM
    assert lab.system_title_for(project) == "通用研究规则"


def test_plan_prompt_injects_instructions_too(client):
    p = _research_project(client)
    rules = "计划里要先列数据来源。"
    assert client.patch(f"/api/lab/projects/{p['id']}", json={"instructions": rules}).status_code == 200

    router = _CaptureRouter("计划：1. 明确问题 2. 收集证据")
    get_services().router = router
    r = client.post(f"/api/lab/projects/{p['id']}/plan", json={"text": "研究一下稳定币动量", "wait": True})
    assert r.status_code == 200, r.text
    joined = "\n".join(m.content for m in router.calls[0]["messages"] if m.role == "system")
    assert rules in joined and "以平台安全规则为准" in joined


def test_report_prompt_uses_research_rules_not_backtest(client):
    p = _research_project(client)
    router = _CaptureRouter("计划：1. 梳理来源 2. 写报告")
    get_services().router = router
    r = client.post(f"/api/lab/projects/{p['id']}/report", json={"text": "稳定币动量", "wait": True})
    assert r.status_code == 200, r.text
    user = next(m.content for m in router.calls[0]["messages"] if m.role == "user")
    assert "调研报告模式 · 通用研究" in user and "run_id" in user
    assert "run_backtest 补充平台数据" not in user  # 研究 / 工作流会话的报告流程不再强制回测


def test_team_brief_injects_instructions_and_uses_research_context():
    project = new_project(
        {
            "kind": "workflow",
            "title": "工作流会话",
            "idea": "从数据到结论的完整研究",
            "context": {"workflow": {"id": "wf-9", "title": "动量研究"}, "run": {"id": "run-1", "status": "COMPLETED"}},
            "instructions": "团队评审只看证据，不要提策略调参。",
        }
    )
    brief = research_brief(project, [{"role": "user", "kind": "text", "content": "把证据汇总一下"}])
    assert "团队评审只看证据" in brief and "以平台安全规则为准" in brief
    assert "【工作流会话】" in brief and "wf-9" in brief
    assert "【验收目标】" not in brief and "【当前代码" not in brief


# ---------------------------------------------------------------------------
# 项目级 instructions：默认空、20k 截断、PATCH 修改、权限
# ---------------------------------------------------------------------------
def test_instructions_default_and_truncation():
    assert new_project({})["instructions"] == ""
    assert new_project({"instructions": "  只做多  "})["instructions"] == "只做多"
    long = new_project({"instructions": "规" * 25_000})["instructions"]
    assert len(long) == RP.MAX_INSTRUCTIONS_CHARS == 20_000


def test_instructions_patch_clamps_and_clears(client):
    p = _research_project(client)
    assert p["instructions"] == ""

    too_long = client.patch(f"/api/lab/projects/{p['id']}", json={"instructions": "x" * 25_000})
    assert too_long.status_code == 200, too_long.text
    assert len(too_long.json()["project"]["instructions"]) == RP.MAX_INSTRUCTIONS_CHARS

    spaced = client.patch(f"/api/lab/projects/{p['id']}", json={"instructions": "  只看 BTC  "})
    assert spaced.json()["project"]["instructions"] == "只看 BTC"

    cleared = client.patch(f"/api/lab/projects/{p['id']}", json={"instructions": ""})
    assert cleared.json()["project"]["instructions"] == ""
    assert client.get(f"/api/lab/projects/{p['id']}").json()["project"]["instructions"] == ""


def test_instructions_patch_requires_visible_project(app):
    users = _team(app)
    a, _ = users["a"]
    b, _ = users["b"]
    pid = a.post("/api/lab/projects", json={"kind": "research", "title": "私密研究"}).json()["project"]["id"]
    assert b.patch(f"/api/lab/projects/{pid}", json={"instructions": "越权改规则"}).status_code == 403
    assert (
        a.patch(f"/api/lab/projects/{pid}", json={"instructions": "作者规则"}).json()["project"]["instructions"]
        == "作者规则"
    )
    # 作者公开后其他成员可以读取，但内容仍是作者写下的规则
    assert a.put(f"/api/collab/shares/project/{pid}", json={"visibility": "workspace"}).status_code == 200
    assert b.get(f"/api/lab/projects/{pid}").json()["project"]["instructions"] == "作者规则"


def test_research_context_view_shows_instructions_and_safe_sources(client):
    r = client.post(
        "/api/lab/projects/from-ask",
        json={
            "title": "带来源的研究",
            "context": {"api_key": "sk-secret-123", "note": "这里是机密正文"},
            "research": {
                "version": 1,
                "workflow": {"id": "wf-9", "title": "动量研究", "revision": 3},
                "run": {"id": "run-1", "status": "COMPLETED", "mode": "research"},
            },
            "instructions": "引用数据必须给出 run_id。",
        },
    )
    assert r.status_code == 200, r.text
    pid = r.json()["project"]["id"]
    view = client.get(f"/api/lab/projects/{pid}").json()
    items = {i["key"]: i for i in view["context"]["items"]}
    assert items["system"]["title"] == "通用研究规则"
    assert items["instructions"]["text"] == "引用数据必须给出 run_id。"
    assert "wf-9" in items["context"]["summary"] and "run-1" in items["context"]["summary"]
    # 来源摘要只给标题 / ID 与未知键名，不泄露值（密钥、正文）
    dumped = json.dumps(view["context"], ensure_ascii=False)
    assert "sk-secret-123" not in dumped and "这里是机密正文" not in dumped
    assert "api_key" in items["context"]["summary"].split("其他来源")[-1]


def test_strategy_prompts_unchanged_but_still_get_instructions(client):
    p = client.post(
        "/api/lab/projects",
        json={"idea": "BTC 双均线", "securities": ["BTC/USDT"], "market_auto": False, "data_source": "synthetic"},
    ).json()["project"]
    lab, project, msgs, fixed_tokens = _project_prompt(client, p["id"])
    assert lab.system_prompt_for(project) == LAB_SYSTEM
    assert msgs[0].content == LAB_SYSTEM
    assert "【目标】" in msgs[1].content  # 策略项目仍是原来的上下文块位置
    assert fixed_tokens > 0

    client.patch(f"/api/lab/projects/{p['id']}", json={"instructions": "只做多，不做空。"})
    lab, project, msgs, _ = _project_prompt(client, p["id"])
    assert msgs[0].content == LAB_SYSTEM
    assert "只做多，不做空。" in msgs[1].content and "以平台安全规则为准" in msgs[1].content
    assert "【目标】" in msgs[2].content


def test_research_kind_hides_strategy_only_tools(client):
    """research/workflow 会话的模型工具表不应再出现写策略/回测入口，避免退回策略研发。"""
    from quant_agent.api.routes_lab import _lab
    from quant_agent.agents.lab_state import new_project
    from quant_agent.platform import context

    ws = client.get("/api/auth/me").json()["workspace"]["id"]
    with context.bind(ws):
        lab = _lab()
        project = lab.store.save(new_project({"kind": "research", "idea": "通用研究工具表"}))
        names = set(lab._all_tools(project["id"]))
        assert "write_strategy" not in names
        assert "run_backtest" not in names
        assert "local_write_text" in names
        assert "local_export_docx_file" in names
        assert "local_append_code" in names

        strategy = lab.store.save(new_project({"kind": "strategy", "idea": "策略工具表"}))
        strategy_names = set(lab._all_tools(strategy["id"]))
        assert "write_strategy" in strategy_names


def test_context_pack_endpoint_projects_rules_and_sources(client):
    project = client.post(
        "/api/lab/projects",
        json={
            "kind": "research",
            "title": "上下文包",
            "idea": "统一 ContextPack",
            "instructions": "引用必须带 run_id。",
            "context": {"workflow": {"id": "wf-pack", "title": "打包流程"}},
        },
    ).json()["project"]
    body = client.get(f"/api/lab/projects/{project['id']}/context-pack").json()
    assert body["format"] == "quantlab.context-pack/1"
    assert body["project_id"] == project["id"]
    assert body["kind"] == "research"
    assert body["instructions"] == "引用必须带 run_id。"
    assert "wf-pack" in body["sources"]
    keys = {i["key"] for i in body["items"]}
    assert {"system", "brief", "instructions", "context"} <= keys
    assert body["budget"] > 0 and body["total"] > 0
