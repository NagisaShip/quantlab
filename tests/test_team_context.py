"""单 Agent 与团队评审共享同一份状态：同一数据窗口、完整版本演进；执行专家提示词；评审组并行 + 组长汇总。"""

from __future__ import annotations

from quant_agent.agents.roles import RoleStore, builtin_roles
from quant_agent.agents.strategy_lab import (
    LabStore,
    LabTools,
    StrategyLab,
    data_window_text,
    new_project,
    research_brief,
    version_history_text,
)
from quant_agent.agents.team import (
    LAB_REVIEW_TEAM,
    PARALLEL_BRIEF,
    Team,
    TeamRunner,
    TeamRunStep,
    builtin_teams,
    sync_builtin_teams,
)
from quant_agent.store import Collections, JsonStore

V1 = "PARAMS = {'fast': 20, 'slow': 60, 'atr_k': 2}\ndef initialize(ctx):\n    pass\n"
V3 = "PARAMS = {'fast': 20, 'slow': 60, 'atr_k': 3}\ndef initialize(ctx):\n    pass  # 斜率过滤\n"


def _lab(tmp_path, team=None):
    tools = LabTools(backtest=lambda *a, **k: {}, market=lambda *a: {}, templates=lambda q: [], team=team)
    return StrategyLab(router=None, store=LabStore(JsonStore(tmp_path)), tools=tools)


def _project_with_history(lab: StrategyLab) -> str:
    p = new_project({"idea": "BTC 1h 双均线金叉，ATR*2 止损", "securities": ["BTC/USDT"], "timeframe": "1h"})
    lab.store.save(p)
    pid = p["id"]
    lab._post(pid, role="user", kind="text", content="BTC 1h 双均线金叉，ATR*2 止损")
    for code, note, sharpe in [(V1, "首版", -0.4), (V1, "收紧入场", 0.2), (V3, "止损改成 ATR*3，加均线斜率过滤", 0.9)]:
        row = lab.add_version(pid, code, source="ai", note=note)
        lab._set_version_result(
            pid, row["v"], metrics={"sharpe": sharpe, "total_return": 0.01, "trades": 30}, evaluation={"passed": False}
        )
    lab._post(pid, role="user", kind="text", content="回撤太大，止损放宽一点再试")
    return pid


def test_brief_has_window_full_history_and_latest_asks(tmp_path):
    lab = _lab(tmp_path)
    pid = _project_with_history(lab)
    p = lab.store.get(pid)
    assert data_window_text(p) == "最近 1500 根 1h K 线（约 62.5 天）"
    hist = version_history_text(p)
    assert "v1" in hist and "v2" in hist and "v3（当前）" in hist
    assert "止损改成 ATR*3" in hist and "atr_k 2→3" in hist and "参数未变（改的是逻辑）" in hist
    assert "夏普 0.90" in hist and "未达标" in hist
    brief = research_brief(p, lab.store.messages(pid))
    assert "最近 1500 根 1h K 线" in brief and "止损放宽一点再试" in brief and "有意做的迭代" in brief
    assert "'atr_k': 3" in brief  # 当前代码是 v3，而不是只带初始想法


def test_convene_sends_the_same_state_to_the_team(tmp_path):
    seen = {}

    def team(topic, *, team_id=None, context=""):
        seen["context"] = context
        return {"id": "r1", "status": "done", "synthesis": "ok", "steps": []}

    lab = _lab(tmp_path, team=team)
    pid = _project_with_history(lab)
    lab._convene(pid, "评审")
    ctx = seen["context"]
    assert "【版本演进" in ctx and "v3（当前）" in ctx and "夏普 -0.40" in ctx and "最近 1500 根" in ctx


def test_market_summary_uses_backtest_window_and_team_tool_takes_bars(tmp_path, monkeypatch):
    from quant_agent.api.deps import MARKET_WINDOW, AppServices

    s = AppServices.build(store_root=str(tmp_path))
    asked = {}

    def fake_bars(symbol, timeframe, limit):
        asked["limit"] = limit
        return [], "test"

    monkeypatch.setattr(s.market, "get_bars", fake_bars)
    s.market_summary("BTC/USDT", "1h")
    assert asked["limit"] == MARKET_WINDOW == 1500
    s.market_summary("BTC/USDT", "1h", 800)
    assert asked["limit"] == 800
    spec = s.teams.tools.spec("market_summary")
    assert {"symbol", "timeframe", "bars"} <= set(spec["parameters"]["properties"])


def test_execution_specialist_prompt_is_focused():
    role = next(r for r in builtin_roles() if r.id == "execution_specialist")
    assert "三档假设下的结论区间" not in role.system_prompt + role.output_format
    for must in ("保本胜率", "Fee Drag", "滑点敏感度", "Beta", "≤150 字", "缺少 X，无法计算"):
        assert must in role.system_prompt


def test_legacy_execution_role_is_upgraded_but_custom_kept(tmp_path):
    store = JsonStore(tmp_path)
    old = next(r for r in builtin_roles() if r.id == "execution_specialist").to_dict()
    old["system_prompt"] = '你是交易执行专家。\n- 明确区分"保守 / 基准 / 乐观"三档假设下的结论区间。'
    old["output_format"] = "①撮合假设与局限 ②成本拆解 ③容量与延迟 ④三档假设下的结论区间"
    old["model"] = "a/m1"
    store.upsert(Collections.ROLES, old)
    RoleStore(store)
    got = store.get(Collections.ROLES, "execution_specialist")
    assert "保本胜率" in got["system_prompt"] and got["model"] == "a/m1"
    # 用户改过的提示词不动
    store.upsert(Collections.ROLES, {**got, "system_prompt": "我自己写的"})
    RoleStore(store)
    assert store.get(Collections.ROLES, "execution_specialist")["system_prompt"] == "我自己写的"


def test_review_team_is_parallel_and_legacy_execution_team_upgraded(tmp_path):
    teams = {t.id: t for t in builtin_teams()}
    review = teams[LAB_REVIEW_TEAM]
    assert review.mode == "parallel" and review.synthesizer_id == "research_lead"
    assert review.role_ids == ["market_analyst", "execution_specialist", "research_auditor"]
    store = JsonStore(tmp_path)
    old = teams["execution_team"].to_dict()
    store.upsert(Collections.TEAMS, {**old, "mode": "sequential"})
    store.upsert(Collections.TEAMS, {**teams["model_team"].to_dict(), "mode": "debate"})  # 用户改过的
    sync_builtin_teams(store)
    assert store.get(Collections.TEAMS, "execution_team")["mode"] == "parallel"
    assert store.get(Collections.TEAMS, LAB_REVIEW_TEAM) is not None
    assert store.get(Collections.TEAMS, "model_team")["mode"] == "debate"


def test_parallel_roles_get_same_material_and_brief_limit(tmp_path):
    roles = RoleStore(JsonStore(tmp_path))
    runner = TeamRunner(router=None, role_store=roles, store=JsonStore(tmp_path))
    tasks = {}

    def fake_invoke(role, task, max_steps=6):
        tasks[role.id] = task
        return TeamRunStep(role_id=role.id, role_name=role.name, model="m", status="ok", output=f"{role.name}观点")

    runner._invoke = fake_invoke
    team = Team(id="t", name="t", description="", mode="parallel", role_ids=["market_analyst", "execution_specialist"])
    runner._run_parallel([roles.get(r) for r in team.role_ids], "材料")
    assert set(tasks) == {"market_analyst", "execution_specialist"}
    assert all(t == "材料" + PARALLEL_BRIEF for t in tasks.values())  # 互相看不到对方的发言
    assert "≤150 字" in PARALLEL_BRIEF
