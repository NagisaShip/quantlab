"""策略引导（对应增补设计第 7.2 节）。

预置若干"结构化问答引导"：用户答完即生成可回测的候选策略。
每套引导 = 问题列表 + 默认值 + 对应提示词模板 id。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Question:
    key: str
    text: str
    default: str | None = None
    choices: list[str] = field(default_factory=list)


@dataclass
class Guidance:
    id: str
    title: str
    description: str
    template_name: str  # 对应 strategies/templates.py 的模板
    prompt_id: str  # 对应 prompts/*.md 的提示词
    questions: list[Question]

    def filled_defaults(self) -> dict[str, str]:
        return {q.key: (q.default or "") for q in self.questions if q.default is not None}


GUIDANCES: dict[str, Guidance] = {
    "trend": Guidance(
        id="trend",
        title="趋势策略引导",
        description="从标的到止损，一步步搭出一个均线/通道趋势策略。",
        template_name="ma_trend",
        prompt_id="strategy_gen.trend",
        questions=[
            Question("symbol", "交易标的？", default="BTC/USDT"),
            Question("timeframe", "周期？", default="1h", choices=["1m", "5m", "15m", "1h", "4h", "1d"]),
            Question("fast", "快线周期？", default="10"),
            Question("slow", "慢线周期？", default="30"),
            Question("max_position", "单策略仓位上限（占资金比）？", default="1.0"),
            Question("stop_loss", "止损比例？", default="0.05"),
        ],
    ),
    "mean_reversion": Guidance(
        id="mean_reversion",
        title="均值回归引导",
        description="设定偏离阈值、持有时限与止损，验证震荡与趋势失效场景。",
        template_name="mean_reversion",
        prompt_id="strategy_gen.mean_reversion",
        questions=[
            Question("symbol", "交易标的？", default="ETH/USDT"),
            Question("timeframe", "周期？", default="15m"),
            Question("window", "均值窗口？", default="20"),
            Question("deviation", "入场偏离阈值？", default="0.03"),
            Question("max_hold_bars", "最长持有 bar 数？", default="48"),
            Question("stop_loss", "止损比例？", default="0.05"),
        ],
    ),
    "grid": Guidance(
        id="grid",
        title="网格策略引导",
        description="设定价格边界、库存上限与暂停条件，测试手续费敏感性。",
        template_name="grid_bounded",
        prompt_id="strategy_gen.grid",
        questions=[
            Question("symbol", "交易标的？", default="BTC/USDT"),
            Question("lower", "网格下边界价格？", default="40000"),
            Question("upper", "网格上边界价格？", default="80000"),
            Question("grids", "网格数量？", default="10"),
            Question("max_inventory", "最大库存（币）？", default="1.0"),
        ],
    ),
    "dca": Guidance(
        id="dca",
        title="定投再平衡引导",
        description="作为组合与基线策略：投入流水独立处理，再平衡计入成本。",
        template_name="dca_rebalance",
        prompt_id="strategy_gen.dca",
        questions=[
            Question("symbol", "交易标的？", default="BTC/USDT"),
            Question("contribution", "每期投入金额（计价币）？", default="100"),
            Question("interval_bars", "投入间隔（bar 数）？", default="24"),
            Question("rebalance_threshold", "再平衡偏离阈值？", default="0.1"),
            Question("target_ratio", "目标现货占比？", default="0.5"),
        ],
    ),
    "baseline": Guidance(
        id="baseline",
        title="基线引导",
        description="买入持有 / 全现金，作为统一比较基准（同币种、同区间、同初始资金）。",
        template_name="buy_hold",
        prompt_id="strategy_gen.baseline",
        questions=[
            Question("symbol", "交易标的？", default="BTC/USDT"),
            Question("ratio", "买入比例？", default="1.0"),
        ],
    ),
}


def render_guidance_prompt(guidance: Guidance, answers: dict[str, str]) -> str:
    """把引导答案渲染成给策略生成 Agent 的自然语言输入。"""
    lines = [f"请生成一个「{guidance.title}」策略，参数如下："]
    for q in guidance.questions:
        val = answers.get(q.key, q.default or "")
        lines.append(f"- {q.text} {val}")
    lines.append("请严格按平台 SDK 契约输出代码、参数表与风险说明。")
    return "\n".join(lines)
