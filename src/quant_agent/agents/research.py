"""调研 Agent：生成行情/策略背景调研摘要（对应增补设计第 7 章）。"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from dataclasses import dataclass

from ..context.manager import ContextManager
from ..llm.router import ModelRouter
from .base import Agent, tool

SYSTEM_PROMPT = textwrap.dedent(
    """
    你是一名量化研究助理。职责：调研指定策略/标的市场表现的背景信息。

    约束：
    - 区分"事实"与"推测"，推测必须标注。
    - 不得给出投资建议或收益承诺。
    - 引用平台历史实验数据时注明 run_id。
    - 输出要点式摘要（≤300 字）+ 数据来源 + 未解决问题。
    """
).strip()


@dataclass
class ResearchResult:
    summary: str
    sources: list[str]
    open_questions: list[str]
    trace_id: str = ""


class ResearchAgent:
    def __init__(
        self,
        router: ModelRouter,
        *,
        market_summary: Callable[[str], dict] | None = None,
        query_runs: Callable[[str], list[dict]] | None = None,
    ) -> None:
        self.router = router
        self.context = ContextManager(system_prompt=SYSTEM_PROMPT)
        tools = [
            tool(
                "market_summary",
                "获取标的市场概览（价格、波动、阶段）",
                {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
                handler=lambda symbol: market_summary(symbol) if market_summary else {"note": "未接入"},
            ),
            tool(
                "query_runs",
                "查询平台历史实验记录",
                {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                handler=lambda query: query_runs(query) if query_runs else [],
            ),
        ]
        self.agent = Agent(
            name="research",
            role="reasoning",
            router=router,
            context=self.context,
            tools=tools,
            temperature=0.4,
        )

    def research(self, topic: str) -> ResearchResult:
        run = self.agent.run(topic)
        return ResearchResult(
            summary=run.final,
            sources=[],
            open_questions=[],
            trace_id=run.run_id,
        )
