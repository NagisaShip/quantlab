"""参数敏感度热力图：网格回测 → 矩阵 / 最佳点 / 稳健性 → 对话卡片。"""

from __future__ import annotations

import re

from quant_agent.agents.lab_tools import chat_tools
from quant_agent.agents.strategy_lab import LabTools, StrategyLab


class _Store:
    def __init__(self) -> None:
        self.project = {
            "id": "p1",
            "code": 'PARAMS = {"fast": 12, "slow": 48}\n\ndef initialize(ctx):\n    pass\n',
            "securities": ["BTC/USDT"],
            "timeframe": "1h",
            "versions": [],
            "current_v": 1,
        }
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


def test_parameter_heatmap_builds_matrix_and_posts_card():
    calls: list[tuple[int, int]] = []

    def backtest(project, code, *, cost_scale=1.0, label=""):
        fast = int(re.search(r'"fast": (\d+)', code).group(1))
        slow = int(re.search(r'"slow": (\d+)', code).group(1))
        calls.append((fast, slow))
        return {
            "ok": True,
            "bt_id": f"{fast}-{slow}",
            "metrics": {
                "sharpe": (fast - 12) ** 2 - (slow - 48) ** 2 / 100,
                "total_return": 0.1,
                "max_drawdown": 0.2,
                "trades": 5,
            },
        }

    store = _Store()
    lab = StrategyLab(None, store, LabTools(backtest=backtest, market=lambda *a, **k: {}, templates=lambda q: []))
    out = lab.parameter_heatmap("p1", x_values=[8, 10, 12, 14], y_values=[40, 44, 48, 52], metric="sharpe")

    assert out["ok"], out
    assert out["x"] == [8, 10, 12, 14]
    assert out["y"] == [40, 44, 48, 52]
    assert len(out["z"]) == 4 and all(len(row) == 4 for row in out["z"])
    assert out["best"] == {"x": 8, "y": 48, "value": 16.0}
    assert out["robustness"]["verdict"] in {"plateau", "mixed", "spike"}
    assert len(calls) == 16
    assert store.messages[-1]["kind"] == "parameter_heatmap"
    assert store.messages[-1]["data"]["x_param"] == "fast"
    assert store.messages[-1]["data"]["status"] == "done"


def test_parameter_heatmap_is_exposed_to_the_agent_tool_loop():
    def backtest(project, code, *, cost_scale=1.0, label=""):
        fast = int(re.search(r'"fast": (\d+)', code).group(1))
        slow = int(re.search(r'"slow": (\d+)', code).group(1))
        return {
            "ok": True,
            "bt_id": f"{fast}-{slow}",
            "metrics": {"sharpe": 1.0, "total_return": 0.1, "max_drawdown": 0.2, "trades": 4},
        }

    store = _Store()
    lab = StrategyLab(None, store, LabTools(backtest=backtest, market=lambda *a, **k: {}, templates=lambda q: []))
    tools = chat_tools(lab, "p1")
    assert "parameter_heatmap" in tools
    out = tools["parameter_heatmap"].handler(x_values=[10, 12], y_values=[40, 48], metric="sharpe")
    assert out["ok"] and out["status"] == "done"
    assert store.messages[-1]["kind"] == "parameter_heatmap"
