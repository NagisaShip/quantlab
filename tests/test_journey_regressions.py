"""Regressions from real-provider user journeys; no external network required."""

import json

import pytest
from conftest import login
from fastapi.testclient import TestClient
from test_factor_research import _panel
from test_lab_goal import ToolRouter

from quant_agent.agents.redteam import rule_points
from quant_agent.agents.strategy_lab import StrategyLab
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, get_services, reset_services, set_services
from quant_agent.llm.base import ChatResult, LLMError, Tool, ToolCall
from quant_agent.research.contracts import activation_error, annotate_report, guard_scope
from quant_agent.research.factor_eval import evaluate_factor
from quant_agent.research.factor_lib import FactorLibrary
from quant_agent.research.portfolio import evaluate_scheme
from quant_agent.store import JsonStore
from quant_agent.strategies.repo import VALID_SLUG, StrategyRepository, slugify


@pytest.fixture
def client(tmp_path):
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def project(client, kind="strategy", **kwargs):
    return client.post(
        "/api/lab/projects",
        json={
            "kind": kind,
            "securities": ["BTC/USDT", "ETH/USDT"],
            "timeframe": "4h",
            "bars_limit": 400,
            "data_source": "synthetic",
            **kwargs,
        },
    ).json()["project"]


def reply(*calls):
    return ChatResult(content="", model="fake-1", provider="fake", tool_calls=list(calls))


def test_fee_and_annualization_contract_matches_nav():
    report = evaluate_factor("-returns(5)", _panel(T=600, signal=0.15), cost_bps=10)
    s = report["summary"]
    assert s["ls_total_return"] == pytest.approx(report["ls_nav"][-1]["nav"] - 1)
    assert report["metric_contract"]["ls_return"] == {"period": "annualized", "costs": "included", "unit": "fraction"}
    assert s["ls_return"] != pytest.approx(s["ls_total_return"])
    assert not report["activation_eligible"]  # synthetic evidence cannot enable an asset


@pytest.mark.parametrize(
    "change",
    [
        {"admitted": False},
        {"panel": {"synthetic": True}},
        {"project_goal_passed": False},
        {"summary": {"ls_return": -0.1}},
        {"checks": [{"key": "drift", "ok": False, "label": "PSI"}]},
    ],
)
def test_activation_checks_more_than_research_admission(change):
    report = {"admitted": True, "panel": {"synthetic": False}, "summary": {"ls_return": 0.2}, "checks": []}
    assert not activation_error(report)
    assert activation_error({**report, **change})


def test_status_api_cannot_enable_unassessed_factor(client):
    f = client.post("/api/factors", json={"name": "unassessed", "expression": "returns(5)", "evaluate": False}).json()[
        "factor"
    ]
    assert client.post(f"/api/factors/{f['id']}/status", json={"status": "active"}).status_code == 409
    assert client.get(f"/api/factors/{f['id']}").json()["factor"]["status"] == "candidate"


def test_factor_rewrite_invalidates_previous_activation_proof(tmp_path):
    lib = FactorLibrary(JsonStore(tmp_path))
    proof = {"admitted": True, "panel": {"synthetic": False}, "summary": {"ls_return": 0.2}, "checks": []}
    f = lib.save({"name": "tested", "expression": "returns(5)", "status": "active"}, report=proof)
    assert f["status"] == "active"
    edited = lib.save({"id": f["id"], "expression": "returns(60)"})
    assert edited["status"] == "candidate" and not edited["activation_eligible"]
    with pytest.raises(ValueError):
        lib.set_status(f["id"], "active")


def test_deleveraging_does_not_inflate_diversification():
    panel = _panel(T=600, N=6)
    full = evaluate_scheme({"optimizer": "equal"}, panel)
    low = evaluate_scheme({"optimizer": "equal", "risk": {"vol_target": 0.01}}, panel)
    assert low["summary"]["avg_exposure"] < 0.5
    assert low["summary"]["effective_n"] == pytest.approx(6, abs=0.02)
    assert low["summary"]["effective_n"] <= len(panel.symbols) + 1e-8
    assert full["summary"]["effective_n"] == pytest.approx(low["summary"]["effective_n"], abs=0.02)
    annotated = annotate_report(low)
    assert annotated["metric_contract"]["turnover"]["definition"] == "full_L1_weight_change"


def test_handwritten_stop_does_not_trigger_definitive_no_stop_claim():
    source = """def handle_data(ctx):
    if ctx.price < ctx.avg_cost * 0.92:
        order_target_percent("BTC/USDT", 0)
"""
    points = rule_points(source, {}, {"trades": 50, "max_drawdown": 0.1})
    assert not any("没有任何止损" in p.get("title", "") or "只能等信号反转" in p.get("detail", "") for p in points)
    assert any("保护止损" in p.get("title", "") for p in points)


def test_scope_changes_require_user_values():
    p = {"securities": ["BTC/USDT"], "timeframe": "4h", "horizon": 1}
    assert guard_scope(p, {"symbols": ["BTC/USDT", "ETH/USDT"]})
    assert not guard_scope(p, {"timeframe": "1h"}, "改成1小时")
    assert guard_scope(p, {"symbols": ["BTC/USDT", "ETH/USDT"]}, "不要改变币池")


def test_model_tools_cannot_expand_pool_or_change_label(client):
    p = project(client, "model")
    get_services().router = ToolRouter(
        [
            reply(ToolCall("scope", "set_model_goal", {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]})),
            "保留原始范围。",
        ]
    )
    r = client.post(
        f"/api/lab/projects/{p['id']}/messages", json={"text": "比较两个模型并存档，不换标的", "wait": True}
    )
    assert r.status_code == 200
    assert r.json()["project"]["securities"] == p["securities"]


def test_batch_tool_calls_cannot_overshoot_budget(client, monkeypatch):
    seen = []
    monkeypatch.setattr(
        StrategyLab,
        "_all_tools",
        lambda self, pid: {
            "probe": Tool("probe", "probe", {"type": "object", "properties": {}}, lambda: seen.append(1))
        },
    )
    p = project(client)
    router = ToolRouter([reply(ToolCall("a", "probe", {}), ToolCall("b", "probe", {}))])
    get_services().router = router
    r = client.post(
        f"/api/lab/projects/{p['id']}/messages", json={"text": "run probes", "wait": True, "max_tool_calls": 1}
    )
    assert r.status_code == 200 and seen == [1]
    assert r.json()["project"]["tool_budget"]["used"] == 1


def test_plan_continuations_share_user_budget(client):
    p = project(client)
    router = ToolRouter(
        [
            reply(ToolCall("p", "set_plan", {"steps": ["检查版本"], "execute": True})),
            "现在执行。",
            reply(ToolCall("v", "list_versions", {})),
            reply(ToolCall("v2", "list_versions", {})),
        ]
    )
    get_services().router = router
    r = client.post(
        f"/api/lab/projects/{p['id']}/messages", json={"text": "检查版本", "wait": True, "max_tool_calls": 2}
    )
    d = r.json()
    assert d["project"]["tool_budget"]["used"] == 2
    assert d["project"]["plan"]["autorun"] is False
    assert len(router.calls) == 3


def test_existing_portfolio_survives_model_failure(client):
    p = project(client, "portfolio")
    spec = {
        "optimizer": "risk_parity",
        "constraints": {"max_weight": 0.3},
        "risk": {"vol_target": 0.35, "dd_stop": 0.15},
    }
    saved = client.post(f"/api/lab/projects/{p['id']}/code", json={"code": json.dumps(spec), "wait": True}).json()[
        "project"
    ]

    class Broken:
        def chat(self, *a, **k):
            raise LLMError("provider temporarily unavailable")

    get_services().router = Broken()
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "继续组合研究", "wait": True})
    d = r.json()
    assert d["project"]["code"] == saved["code"]
    assert d["project"]["current_v"] == saved["current_v"]
    assert d["execution"]["state"] == "partial"


def test_archiving_versions_does_not_recompute_or_overwrite(client):
    p = project(client, "portfolio")
    for optimizer in ["equal", "risk_parity"]:
        r = client.post(
            f"/api/lab/projects/{p['id']}/code",
            json={"code": json.dumps({"optimizer": optimizer, "constraints": {"max_weight": 0.3}}), "wait": True},
        )
        assert r.status_code == 200
    before = client.get("/api/portfolio/runs").json()
    get_services().router = ToolRouter(
        [
            reply(ToolCall("r1", "restore_version", {"version": 1})),
            reply(ToolCall("s1", "save_work", {})),
            reply(ToolCall("r2", "restore_version", {"version": 2})),
            reply(ToolCall("s2", "save_work", {})),
            "两份候选分别保存。",
        ]
    )
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "只归档两个历史版本，不重算", "wait": True})
    assert r.status_code == 200
    rows = client.get("/api/portfolio").json()["schemes"]
    assert len(rows) == 2 and len({row["id"] for row in rows}) == 2
    assert all(row["status"] == "candidate" for row in rows)
    assert len(r.json()["project"]["versions"]) == 2
    after = client.get("/api/portfolio/runs").json()
    assert len(before["runs"]) == len(after["runs"])


def test_model_cannot_change_target_label_during_comparison(client):
    p = project(client, "model")
    spec = {"model": "ridge", "label": "fwd_ret", "features": ["returns(5)", "-ts_std(ret,20)"]}
    saved = client.post(f"/api/lab/projects/{p['id']}/code", json={"code": json.dumps(spec), "wait": True}).json()[
        "project"
    ]
    get_services().router = ToolRouter(
        [reply(ToolCall("bad", "write_model_spec", {"spec": {**spec, "label": "direction"}})), "沿用原标签。"]
    )
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "比较并归档，不改变研究协议", "wait": True})
    assert r.json()["project"]["code"] == saved["code"]
    assert r.json()["project"]["current_v"] == saved["current_v"]


def test_completed_steps_do_not_trigger_goal_chasing(client):
    p = project(client, goals={"min_sharpe": 99.0})
    router = ToolRouter(
        [
            reply(
                ToolCall("p", "set_plan", {"steps": ["归档"], "execute": True}),
                ToolCall("done", "update_plan_step", {"index": 1, "status": "done"}),
            ),
            "如实归档，未达标。",
        ]
    )
    get_services().router = router
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "只归档", "wait": True})
    assert len(router.calls) == 2
    assert r.json()["project"]["plan"]["status"] == "done"
    assert len(r.json()["project"]["plan"]["steps"]) == 1


def test_new_user_request_pauses_old_autorun_plan(client):
    p = project(client)
    router = ToolRouter(
        [
            reply(
                ToolCall("p", "set_plan", {"steps": ["执行旧实验"], "execute": True}),
                ToolCall("ask", "ask_user", {"question": "继续吗？", "options": ["继续", "停止"]}),
            ),
            "改为只读说明，不继续旧实验。",
        ]
    )
    get_services().router = router
    client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "列出旧计划", "wait": True})
    r = client.post(f"/api/lab/projects/{p['id']}/messages", json={"text": "只解释现状，不继续旧计划", "wait": True})
    assert len(router.calls) == 2
    assert r.json()["project"]["plan"]["autorun"] is False


@pytest.mark.parametrize("title", ["真实体验_strategy", "_strategy", "___", "中文名___MA", "---", "中文"])
def test_generated_slugs_always_pass_repository_validation(tmp_path, title):
    slug = slugify(title)
    assert VALID_SLUG.fullmatch(slug)
    repo = StrategyRepository(tmp_path)
    assert repo.dir_of(slug).parent == tmp_path
