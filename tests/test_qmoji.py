from quant_agent.agents.copilot import StrategyCopilot
from quant_agent.agents.qmoji import decorate_qmoji, metrics_from_materials, qmoji_for_metrics
from quant_agent.llm.base import ChatResult


def test_strong_sharpe_gets_alpha_or_moon():
    metrics = {"sharpe": 2.6, "excess_return": 0.05, "trades": 40}
    assert qmoji_for_metrics("结论", role="backtest_analyst", tags=["回测"], metrics=metrics) == "alpha"
    metrics["excess_return"] = 0.12
    assert qmoji_for_metrics("结论", role="backtest_analyst", tags=["回测"], metrics=metrics) == "moon"


def test_strong_sharpe_with_few_trades_does_not_celebrate():
    metrics = {"sharpe": 3.2, "excess_return": 0.20, "trades": 6}
    assert qmoji_for_metrics("结论", role="backtest_analyst", tags=["回测"], metrics=metrics) == ""


def test_risk_block_and_unclear_fee():
    assert qmoji_for_metrics("结论：不通过", role="risk_officer", tags=["风控"], metrics={}) == "no"
    assert (
        qmoji_for_metrics("最大回撤超过上限", role="risk_officer", tags=["风控"], metrics={"max_drawdown": 0.21})
        == "no"
    )
    assert (
        qmoji_for_metrics("手续费口径没有说明，无法判断", role="risk_officer", tags=["风控"], metrics={}) == "question"
    )


def test_loss_and_drawdown():
    assert (
        qmoji_for_metrics("总收益很差", role="backtest_analyst", tags=["回测"], metrics={"total_return": -0.12})
        == "cry"
    )
    assert (
        qmoji_for_metrics("回撤偏大", role="backtest_analyst", tags=["回测"], metrics={"max_drawdown": 0.28})
        == "drawdown"
    )


def test_hardcore_quant_keywords():
    assert qmoji_for_metrics("参数尖峰，典型过拟合", role="backtest_analyst", tags=["回测"], metrics={}) == "overfit"
    assert qmoji_for_metrics("这段代码有未来函数", role="research_auditor", tags=["研究"], metrics={}) == "future"
    assert qmoji_for_metrics("出现了插针", role="backtest_analyst", tags=["回测"], metrics={}) == "wick"
    assert qmoji_for_metrics("没有过拟合，也未发现未来函数", role="backtest_analyst", tags=["回测"], metrics={}) == ""
    assert qmoji_for_metrics("没有插针", role="backtest_analyst", tags=["回测"], metrics={}) == ""


def test_decorate_is_idempotent_and_uses_metrics_from_materials():
    text = "回测结论：夏普 2.8，超额 6%"
    out = decorate_qmoji(
        text,
        role="backtest_analyst",
        tags=["回测"],
        metrics=metrics_from_materials({"backtest": {"metrics": {"sharpe": 2.8, "excess_return": 0.06, "trades": 30}}}),
    )
    assert out.endswith(":qbot-alpha:")
    assert decorate_qmoji(out, role="backtest_analyst", metrics={}) == out


class _StrongRouter:
    def chat(self, messages, **kwargs):
        return ChatResult(content="回测结论：夏普 2.8，超额 6%，交易 30 笔。", model="fake", provider="test")


def test_copilot_interpret_appends_qmoji_from_metrics():
    bt = {
        "metrics": {"sharpe": 2.8, "excess_return": 0.06, "trades": 30, "total_return": 0.2},
        "datasets": [{"source": "synthetic"}],
        "initial_cash": 10000,
        "cost_model": {"taker_fee": 0.001, "slippage_bps": 5},
    }
    out = StrategyCopilot(_StrongRouter()).interpret(bt).content
    assert out.endswith(":qbot-alpha:")
