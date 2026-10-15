"""AI 助手：降级生成、模型自检修复循环、解释/解读/审稿；模型配置持久化。"""

from __future__ import annotations

import pytest
from conftest import login
from fastapi.testclient import TestClient

from quant_agent.agents.copilot import StrategyCopilot, parse_intent, smoke_test
from quant_agent.api.app import create_app
from quant_agent.api.deps import AppServices, reset_services, set_services
from quant_agent.llm.base import ChatResult, LLMError

GOOD = """```python
PARAMS = {"fast": 10, "slow": 30, "weight": 0.6}

def initialize(ctx):
    run_daily(step)

def step(ctx):
    s = ctx.securities[0]
    c = history(s, PARAMS["slow"], "close")
    if len(c) < PARAMS["slow"]:
        return
    order_target_percent(s, PARAMS["weight"] if sum(c[-PARAMS["fast"]:]) / PARAMS["fast"] > sum(c) / len(c) else 0)
```"""

BAD = """```python
import os
def initialize(ctx):
    pass
```"""


class FakeRouter:
    """按顺序返回预设回复；记录收到的提示词。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def chat(self, messages, **kwargs):
        self.prompts.append(messages[-1].content)
        if not self.replies:
            raise LLMError("no more replies")
        return ChatResult(content=self.replies.pop(0), model="fake-1", provider="fake")


class NoModelRouter:
    def chat(self, *a, **k):
        raise LLMError("所有模型均失败:\n[deepseek] 未配置 API Key，已跳过")


# ---------------- 降级 ----------------
@pytest.mark.parametrize(
    "text,family",
    [
        ("BTC 1小时线，20 和 60 均线金叉买入，止损 5%，仓位 70%", "ma_cross"),
        ("ETH 4小时 RSI 低于 25 买入 高于 75 卖出", "rsi_reversion"),
        ("在 BTC ETH SOL 之间做动量轮动", "momentum_rotation"),
        ("BTC 网格，每格 1.5%", "grid"),
        ("每周定投 BTC 200 USDT", "dca"),
        ("突破 55 日新高买入的海龟策略", "donchian"),
        ("布林带突破", "bollinger"),
        ("几个币做风险平价配置", "inverse_vol"),
    ],
)
def test_fallback_generate_produces_runnable_code(text, family):
    intent = parse_intent(text)
    assert intent["family"] == family
    result = StrategyCopilot(NoModelRouter()).generate(text)
    assert result.mode == "fallback"
    assert result.checks["ok"], result.checks
    assert "模板降级" in result.content


def test_fallback_applies_extracted_numbers():
    result = StrategyCopilot(NoModelRouter()).generate("BTC 1小时线，20 和 60 均线金叉买入，止损 5%，仓位 70%")
    assert result.params["fast"] == 20 and result.params["slow"] == 60
    assert result.params["stop_loss"] == pytest.approx(0.05)
    assert result.params["weight"] == pytest.approx(0.7)
    assert result.timeframe == "1h" and result.securities == ["BTC/USDT"]


def test_llm_generate_repairs_bad_code():
    router = FakeRouter([BAD, "修复如下\n" + GOOD])
    result = StrategyCopilot(router).generate("均线策略")
    assert result.mode == "llm"
    assert result.attempts == 2
    assert result.checks["ok"]
    assert "不允许导入" in router.prompts[1], "第二轮提示词应包含第一轮的检查问题"
    assert result.params == {"fast": 10, "slow": 30, "weight": 0.6}


def test_llm_failure_mid_way_falls_back():
    result = StrategyCopilot(FakeRouter([])).generate("均线")
    assert result.mode == "fallback" and result.code


def test_smoke_test_reports_runtime_errors():
    code = "def initialize(ctx):\n    run_daily(step)\n\ndef step(ctx):\n    x = 1 / 0\n"
    checks = smoke_test(code)
    assert not checks["ok"] and checks["errors"]


def test_explain_interpret_review_fallbacks():
    cp = StrategyCopilot(NoModelRouter())
    from quant_agent.strategies.sdk_templates import get_template

    code = get_template("ma_cross").code
    assert "静态分析" in cp.explain(code).content
    bt = {
        "metrics": {
            "total_return": 0.1,
            "benchmark_return": 0.05,
            "excess_return": 0.047,
            "max_drawdown": 0.08,
            "sharpe": 1.2,
            "trades": 4,
            "total_fees": 20,
            "beta": 0.4,
            "trading_days": 30,
        },
        "datasets": [{"source": "synthetic"}],
        "initial_cash": 10_000,
        "cost_model": {"taker_fee": 0.001, "slippage_bps": 5},
    }
    text = cp.interpret(bt).content
    assert "跑赢基准" in text and "合成数据" in text and "样本太少" in text
    review = cp.review(code, bt).content
    assert "可信度" in review


def test_fix_fallback_gives_rule_hints():
    errors = ["ZeroDivisionError: division by zero"]
    out = StrategyCopilot(NoModelRouter()).fix(
        "def initialize(ctx):\n    pass\n", errors, securities=["BTC/USDT"], timeframe="1h"
    )
    assert out.mode == "fallback" and "除以零" in out.content


# ---------------- API ----------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    set_services(AppServices.build(store_root=str(tmp_path)))
    with TestClient(create_app()) as c:
        login(c)
        yield c
    reset_services()


def test_generate_and_save_as_draft(client):
    r = client.post("/api/ai/generate", json={"description": "SOL 4小时 布林带突破", "save": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "fallback" and body["strategy"]["source"] == "agent"
    assert body["strategy"]["human_approved"] is False
    assert body["securities"] == ["SOL/USDT"] and body["timeframe"] == "4h"
    assert client.get("/api/ai/history").json()["items"][0]["action"] == "generate"


def test_no_builtin_providers(client):
    """平台不内置服务商：新工作区里一个都没有，AI 走模板降级。"""
    cfg = client.get("/api/config").json()
    assert cfg["providers"] == []
    assert client.get("/api/config/models").json()["models"] == []
    assert client.get("/api/config/status").json()["ready"] is False


def test_provider_key_from_ui_is_persisted_and_masked(client, tmp_path):
    r = client.post(
        "/api/config/providers",
        json={
            "name": "myllm",
            "type": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-test-1234567890",
            "models": ["m-1"],
        },
    )
    assert r.status_code == 200, r.text
    masked = r.json()["provider"]["key_masked"]
    assert masked.startswith("sk-t") and "1234567890" not in masked
    assert r.json()["provider"]["scope"] == "workspace"  # 管理员默认登记为工作区共享
    cfg = client.get("/api/config").json()
    mine = next(p for p in cfg["providers"] if p["name"] == "myllm")
    assert mine["has_key"] and "sk-test" not in str(cfg)
    # 路由改到新模型后，status 显示可用
    r = client.put("/api/config/routing?scope=workspace", json={"default": "myllm/m-1", "code_gen": "myllm/m-1"})
    assert r.status_code == 200, r.text
    assert client.get("/api/config/status").json()["ready"] is True
    # 重新构建服务（模拟重启）：密钥与路由仍在
    set_services(AppServices.build(store_root=str(tmp_path)))
    assert client.get("/api/config").json()["routing"]["default"] == "myllm/m-1"
    assert any(m["target"] == "myllm/m-1" and m["ready"] for m in client.get("/api/config/models").json()["models"])
    # 删除
    assert client.delete("/api/config/providers/myllm?scope=workspace").json()["ok"]
    assert client.get("/api/config").json()["providers"] == []


def test_routing_rejects_unknown_model(client):
    r = client.put("/api/config/routing", json={"default": "nope/x"})
    assert r.status_code == 400
