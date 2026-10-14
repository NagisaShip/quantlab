"""策略生成 Agent（对应增补设计第 3 章）。

把自然语言交易想法，经"澄清 → 检索 → 生成 → 校验 → 回测 → 解释"
多步流程，产出符合平台 SDK 契约的候选策略。
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..context.manager import ContextManager
from ..llm.router import ModelRouter
from .base import Agent, tool

SYSTEM_PROMPT = textwrap.dedent(
    """
    你是一名专业且谨慎的加密货币量化研究员，工作在"仿真验证平台"内。

    铁律：
    1. 只通过 order_* 函数表达目标仓位，禁止调用交易所、网络或文件系统。
    2. 只能使用已收盘的 bar，禁止使用未来数据。
    3. 必须包含止损与仓位上限。
    4. 用户描述缺少关键参数时，先列出澄清问题，不要臆测。
    5. 输出结构化：策略逻辑 → 代码 → 参数表 → 风险与局限。
    6. 不得承诺收益，不得使用"稳赚""保证"等表述。

    平台策略 SDK（模块级函数，不需要 import，直接写）：
    - 入口：`def initialize(ctx)` 启动一次；`def handle_data(ctx)` 或 `def on_bar(ctx, bar)`
      每根 bar 调用一次。
    - 成交回报：`def on_trade(ctx, trade)` / `def on_fill(ctx, trade)` /
      `def on_order_filled(ctx, trade)` 三个名字等价，都在成交后收到同一份成交。
    - 定时：先用 `run_timer(interval_ms)` 注册，才会调用 `def on_timer(ctx)`。
    - 检查点：`def snapshot(ctx)` 返回可 JSON 序列化的状态，`def restore(ctx, state)`
      在恢复时收回（仅实时模拟下调用）。
    - 下单：`order_target_percent(symbol, 0.5)`、`order_target(symbol, 数量)`、
      `order_target_value`、`order_value`、`order`。
    - 数据：`history(symbol, n)` / `attribute_history(...)` 返回截至当前时钟、**已收盘**的
      最近 n 根 bar。
    - 参数：顶部写 `PARAMS = {"fast": 12}`，用 `get_param("fast")` 读取。
    - 版本：`get_strategy_version()` 或 `ctx.strategy_version_id` 取当前策略版本 ID。
    - 仓位只读：`ctx.portfolio` 不可赋值，改仓位必须走上面的 order_* 函数。
    """
).strip()


@dataclass
class GeneratedStrategy:
    name: str
    code: str
    params: dict[str, Any]
    logic: str = ""
    risks: str = ""
    clarifications: list[str] = field(default_factory=list)
    prompt_hash: str = ""
    model: str = ""
    trace_id: str = ""


class StrategyGenAgent:
    def __init__(
        self,
        router: ModelRouter,
        *,
        template_search: Callable[[str], list[dict]] | None = None,
        sandbox_check: Callable[[str], dict] | None = None,
        backtest: Callable[[str, dict], dict] | None = None,
    ) -> None:
        self.router = router
        self.template_search = template_search
        self.sandbox_check = sandbox_check
        self.backtest = backtest
        self.context = ContextManager(system_prompt=SYSTEM_PROMPT)
        tools = [
            tool(
                "search_templates",
                "在平台策略模板库中检索相似策略，返回模板名与说明",
                {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                handler=self._search_templates,
            ),
            tool(
                "check_strategy_code",
                "对策略代码做静态校验与契约测试（是否只返回目标仓位、是否含未来数据）",
                {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
                handler=self._check_code,
            ),
        ]
        self.agent = Agent(
            name="strategy_gen",
            role="code_gen",
            router=router,
            context=self.context,
            tools=tools,
            temperature=0.1,
        )

    def _search_templates(self, query: str) -> list[dict]:
        if self.template_search is None:
            return []
        return self.template_search(query)

    def _check_code(self, code: str) -> dict:
        if self.sandbox_check is None:
            return {"ok": True, "note": "sandbox_check 未注入，跳过"}
        return self.sandbox_check(code)

    def generate(self, user_input: str, *, symbol: str = "BTC/USDT", timeframe: str = "1h") -> GeneratedStrategy:
        task = (
            f"用户想法：{user_input}\n"
            f"标的：{symbol}　周期：{timeframe}\n\n"
            "请按模板输出：A.策略逻辑说明 B.Python 代码 C.参数表 D.风险与局限。"
            "若缺少关键参数，先列出需要澄清的问题。"
        )
        run = self.agent.run(task)
        result = self._parse(run.final)
        result.prompt_hash = hashlib.sha256(task.encode("utf-8")).hexdigest()[:16]
        result.trace_id = run.run_id
        return result

    @staticmethod
    def _parse(text: str) -> GeneratedStrategy:
        clarifications: list[str] = []
        for line in text.splitlines():
            if re.match(r"\s*[-*\d]+[.)、]?\s*.*(澄清|确认|请问|需要.*参数)", line):
                clarifications.append(line.strip())

        code = ""
        for match in re.finditer(r"```(?:python)?\n(.*?)```", text, re.DOTALL):
            block = match.group(1)
            if "class" in block or "def initialize" in block:
                code = block.strip()
                break
        if not code:
            code = (
                "# 未解析到完整代码，请检查 Agent 输出\n"
                "class Strategy:\n"
                "    def initialize(self, ctx): pass\n"
                "    def on_bar(self, ctx, bar): ctx.set_target_position(0.0)\n"
            )

        params = _extract_params(text)
        risks = ""
        if m := re.search(r"(风险与局限[:：]?)(.*?)(?=\n#|\Z)", text, re.DOTALL):
            risks = m.group(2).strip()[:800]

        return GeneratedStrategy(
            name=_guess_name(text),
            code=code,
            params=params,
            logic=text[:1200],
            risks=risks,
            clarifications=clarifications,
        )


def _extract_params(text: str) -> dict[str, Any]:
    """从文本里抓简单的 参数名=默认值 表格。"""
    params: dict[str, Any] = {}
    for m in re.finditer(r"\|\s*`?(\w+)`?\s*\|\s*([\d.]+)\s*\|", text):
        try:
            params[m.group(1)] = float(m.group(2)) if "." in m.group(2) else int(m.group(2))
        except ValueError:
            continue
    return params


def _guess_name(text: str) -> str:
    for m in re.finditer(r"(策略名|name)[:：]\s*(\w+)", text):
        return m.group(2)
    return "generated_strategy"
