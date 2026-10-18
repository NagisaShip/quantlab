"""过拟合压力测试编排：滚动分段 + 稳定性 + 成本压力 → 对话卡片。"""

from __future__ import annotations

from quant_agent.agents.lab_tools import chat_tools
from quant_agent.agents.strategy_lab import LabTools, StrategyLab


class _Store:
    def __init__(self) -> None:
        self.project = {"id": "p1", "code": "def initialize(ctx):\n    pass\n", "current_v": 3}
        self.messages: list[dict] = []

    def get(self, pid: str) -> dict:
        return self.project

    def append(self, pid: str, msg: dict) -> dict:
        msg = {**msg, "seq": len(self.messages) + 1}
        self.messages.append(msg)
        return msg

    def patch(self, pid: str, **kw) -> None:
        self.project.update(kw)

    def update(self, pid: str, seq: int, **changes) -> None:
        for msg in self.messages:
            if msg.get("seq") == seq:
                if "data" in changes:
                    msg["data"] = {**msg.get("data", {}), **changes.pop("data")}
                msg.update(changes)

    def save(self, project: dict) -> None:
        self.project = project


def test_stress_test_orchestrates_checks_and_updates_card():
    store = _Store()
    lab = StrategyLab(
        None,
        store,
        LabTools(
            backtest=lambda *a, **k: {"ok": True, "metrics": {"total_return": 0.1, "sharpe": 1.0}},
            market=lambda *a, **k: {},
            templates=lambda q: [],
        ),
    )
    lab.walk_forward = lambda pid, segments=3, mode="sliced": {
        "ok": True,
        "verdict": "稳定",
        "segments": [
            {
                "segment": 1,
                "start": 1,
                "end": 2,
                "metrics": {"total_return": 0.08, "sharpe": 1.2, "max_drawdown": 0.05, "trades": 4},
            },
            {
                "segment": 2,
                "start": 2,
                "end": 3,
                "metrics": {"total_return": 0.03, "sharpe": 0.7, "max_drawdown": 0.06, "trades": 3},
            },
            {
                "segment": 3,
                "start": 3,
                "end": 4,
                "metrics": {"total_return": 0.02, "sharpe": 0.5, "max_drawdown": 0.04, "trades": 2},
            },
        ],
        "reconcile": {"reconstructs": True},
    }
    lab.stability = lambda pid, windows=4, sensitivity=True: {
        "flags": [],
        "sensitivity": {"fragile": False, "note": "参数邻域稳定", "runs": []},
    }
    lab.cost_sweep = lambda pid, scales=None, refine=True: {
        "rows": [
            {"scale": 1.0, "passed": True, "metrics": {"total_return": 0.1, "sharpe": 1.0}},
            {"scale": 2.0, "passed": True, "metrics": {"total_return": 0.04, "sharpe": 0.5}},
            {"scale": 3.0, "passed": False, "metrics": {"total_return": -0.02, "sharpe": -0.3}},
        ],
        "limit": {"pass_max": 2.0, "fail_min": 3.0},
        "summary": "2× 成本仍满足，3× 不满足",
    }
    out = lab.stress_test("p1", segments=3, windows=4, cost_scales=[1, 2, 3])
    assert out["ok"] and out["verdict"] == "pass", out
    assert len(out["checks"]) == 6 and all(c["ok"] for c in out["checks"])
    assert store.messages[-1]["kind"] == "stress_test"
    assert store.messages[-1]["data"]["status"] == "done"


def test_stress_test_is_exposed_to_the_agent_tool_loop():
    store = _Store()
    lab = StrategyLab(
        None, store, LabTools(backtest=lambda *a, **k: {"ok": True}, market=lambda *a, **k: {}, templates=lambda q: [])
    )
    tools = chat_tools(lab, "p1")
    assert "stress_test" in tools
    assert "过拟合" in tools["stress_test"].description
