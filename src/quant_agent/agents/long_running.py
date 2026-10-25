"""长期 Agent：通知型 + 高级策略型（对应增补设计第 5 章）。

通知型（DailyDigestAgent）：定期汇总净值/回撤/异常，推送微信。
策略型（StrategyMaintainerAgent）：定期生成候选版本 + 差异报告，仅供人审核。

红线：长期 Agent 永远不能直接启用实盘，只产出候选与建议。
"""

from __future__ import annotations

import json
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..context.manager import ContextManager, LongTermMemory, MemoryItem
from ..llm.router import ModelRouter
from ..notify.channels import DISCLAIMER, Notification, NotificationHub
from .base import Agent, tool

DIGEST_PROMPT = textwrap.dedent(
    """
    你负责生成每日策略简报，供微信阅读。

    约束：
    - 不超过 200 字。
    - 只陈述数据，不预测未来。
    - 若存在异常（订单 UNKNOWN、对账差异、任务失败），开头必须先提示。
    - 结尾固定附："仅为研究结果，非投资建议。"
    输出格式：标题 + 3~5 行要点 + 免责声明。
    """
).strip()

MAINTAINER_PROMPT = textwrap.dedent(
    """
    你是策略维护研究员。根据滚动前推验证窗口与参数稳定性数据，
    评估是否需要生成策略候选新版本。

    约束：
    - 只生成 candidate 版本 + 差异报告，绝对不得启用实盘。
    - 若证据不足，明确说明"维持现状"。
    - 变更必须说明原因、影响范围、需要重新进行的验证。
    """
).strip()


@dataclass
class DigestData:
    date: str
    nav: float
    period_change: float
    period: str = "7d"
    max_drawdown: float = 0.0
    trades: int = 0
    win_rate: float = 0.0
    alerts: list[str] = field(default_factory=list)


@dataclass
class AgentRunRecord:
    run_key: str
    agent_id: str
    ts: int
    input_digest: str
    output: str
    pushed_to: list[str] = field(default_factory=list)
    status: str = "ok"
    id: str = ""
    runtime_run_id: str = ""
    runtime_state: str = ""
    delivery_effect_id: str = ""
    delivery_state: str = ""
    generation_source: str = ""
    runtime_event_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "id" or v}


def fallback_digest(data: DigestData) -> str:
    """LLM 不可用时的确定性简报：宁可少几句分析，也不能没有数字。"""
    lines = [f"【策略简报】{data.date}"]
    lines.append(f"· 净值：{data.nav:,.2f}（{data.period}：{data.period_change:+.2%}）")
    lines.append(f"· 最大回撤：{data.max_drawdown:.2%}")
    lines.append(f"· 交易：{data.trades} 笔，胜率 {data.win_rate:.0%}")
    lines.append(f"· 异常：{'；'.join(data.alerts) if data.alerts else '无'}")
    lines.append("（本条由数据模板直接生成：当前无可用大模型）")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def fallback_maintainer(evidence: dict) -> str:
    """LLM 不可用时的确定性评估摘要。"""
    strategies = evidence.get("strategies") or {}
    lines = [f"【策略维护评估】{evidence.get('period', '')}"]
    if not strategies:
        lines.append("· 暂无可用于评估的实验记录。")
    for name, s in strategies.items():
        avg_sharpe = s.get("avg_sharpe")
        line = (
            f"· {name}：{s.get('runs', 0)} 次实验，"
            f"平均收益 {s.get('avg_return', 0):+.2%}，"
            f"收益标准差 {s.get('return_std', 0):.4f}，"
            f"平均回撤 {s.get('avg_max_drawdown', 0):.2%}"
        )
        if avg_sharpe is not None:
            line += f"，平均 Sharpe {avg_sharpe:.2f}"
        lines.append(line)
    lines.append("· 结论：数据模板生成，未做参数稳定性判断。")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


class LongRunningAgent:
    """长期 Agent 基类：带记忆、幂等 run_key、审计记录、通知。"""

    def __init__(
        self,
        *,
        agent_id: str,
        router: ModelRouter,
        hub: NotificationHub,
        memory: LongTermMemory | None = None,
        channel: str = "console",
    ) -> None:
        self.agent_id = agent_id
        self.router = router
        self.hub = hub
        self.memory = memory or LongTermMemory()
        self.channel = channel
        self.audit_log: list[AgentRunRecord] = []
        self._seen_keys: set[str] = set()
        self.record_store: Callable[[dict], Any] | None = None

    def _should_run(self, run_key: str) -> bool:
        """幂等：同一 run_key 不重复执行/推送。"""
        if run_key in self._seen_keys:
            return False
        self._seen_keys.add(run_key)
        return True

    def _record(self, record: AgentRunRecord) -> None:
        self.audit_log.append(record)
        self.memory.add(
            MemoryItem(
                key=f"{self.agent_id}:{record.run_key}",
                text=f"[{record.ts}] {record.output[:500]}",
                kind="conclusion",
                ts=record.ts,
            )
        )


class DailyDigestAgent(LongRunningAgent):
    """通知型长期 Agent：每日/每周简报 → 微信。"""

    def __init__(
        self,
        *,
        router: ModelRouter,
        hub: NotificationHub,
        collect_data: Callable[[], DigestData],
        memory: LongTermMemory | None = None,
        channel: str = "console",
    ) -> None:
        super().__init__(agent_id="daily_digest", router=router, hub=hub, memory=memory, channel=channel)
        self.collect_data = collect_data
        self.context = ContextManager(system_prompt=DIGEST_PROMPT)
        self.agent = Agent(
            name="daily_digest",
            role="report",
            router=router,
            context=self.context,
            tools=[],
            temperature=0.3,
            max_steps=1,
        )

    def run_once(self, *, date: str | None = None, dry_run: bool = False) -> AgentRunRecord:
        data = self.collect_data()
        date = date or data.date
        run_key = f"{self.agent_id}:{date}"
        if self.agent.runtime_journal is not None or getattr(self.router, "runtime_journal", None) is not None:
            from .long_runtime import run_once

            task = "请根据以下数据生成简报：\n" + json.dumps(data.__dict__, ensure_ascii=False, indent=2)
            record = run_once(self, task, run_key, f"策略简报 {date}", fallback_digest(data), dry_run=dry_run)
            self.audit_log.append(record)
            return record
        if not self._should_run(run_key):
            return AgentRunRecord(run_key, self.agent_id, int(time.time() * 1000), "", "已运行，跳过", status="skipped")

        task = json.dumps(data.__dict__, ensure_ascii=False, indent=2)
        result = self.agent.run(f"请根据以下数据生成简报：\n{task}")
        if result.status == "done" and result.final.strip():
            body = result.final
        else:
            body = fallback_digest(data)  # LLM 不可用也要出简报，不能只丢一句报错
        if DISCLAIMER not in body:
            body = body.rstrip() + f"\n\n{DISCLAIMER}"

        record = AgentRunRecord(
            run_key=run_key,
            agent_id=self.agent_id,
            ts=int(time.time() * 1000),
            input_digest=task,
            output=body,
        )
        if dry_run:
            print(f"[dry-run] {body}")
        else:
            ok = self.hub.send(Notification(title=f"策略简报 {date}", body=body, channel=self.channel))
            if ok:
                record.pushed_to.append(self.channel)
            else:
                record.status = "push_pending"
        self._record(record)
        return record


class StrategyMaintainerAgent(LongRunningAgent):
    """高级策略型长期 Agent：定期产出候选版本 + 差异报告（不自动上线）。"""

    def __init__(
        self,
        *,
        router: ModelRouter,
        hub: NotificationHub,
        collect_evidence: Callable[[], dict],
        propose_version: Callable[[dict], dict] | None = None,
        memory: LongTermMemory | None = None,
        channel: str = "console",
    ) -> None:
        super().__init__(agent_id="strategy_maintainer", router=router, hub=hub, memory=memory, channel=channel)
        self.collect_evidence = collect_evidence
        self.propose_version = propose_version
        tools = [
            tool(
                "propose_candidate",
                "创建一个候选策略版本（draft 状态，等待人工审核）",
                {"type": "object", "properties": {"payload": {"type": "object"}}, "required": ["payload"]},
                handler=lambda payload: propose_version(payload) if propose_version else {"note": "未接入版本服务"},
            )
        ]
        if propose_version is None:
            tools = []
        self.context = ContextManager(
            system_prompt=MAINTAINER_PROMPT
            + ("\n当前未连接候选版本服务，只能提供评估，不得声称创建版本。" if propose_version is None else "")
        )
        self.agent = Agent(
            name="strategy_maintainer",
            role="reasoning",
            router=router,
            context=self.context,
            tools=tools,
            temperature=0.3,
            max_steps=6,
        )

    def run_once(self, *, period: str | None = None, dry_run: bool = False) -> AgentRunRecord:
        evidence = self.collect_evidence()
        period = period or evidence.get("period", time.strftime("%Y-%m-%d"))
        run_key = f"{self.agent_id}:{period}"
        if self.agent.runtime_journal is not None or getattr(self.router, "runtime_journal", None) is not None:
            from .long_runtime import run_once

            task = "以下是滚动前推验证与参数稳定性数据，请评估是否生成候选新版本：\n" + json.dumps(
                evidence, ensure_ascii=False, indent=2
            )
            record = run_once(
                self, task, run_key, f"策略维护建议 {period}", fallback_maintainer(evidence), dry_run=dry_run
            )
            self.audit_log.append(record)
            return record
        if not self._should_run(run_key):
            return AgentRunRecord(run_key, self.agent_id, int(time.time() * 1000), "", "已运行，跳过", status="skipped")

        task = "以下是滚动前推验证与参数稳定性数据，请评估是否生成候选新版本：\n" + json.dumps(
            evidence, ensure_ascii=False, indent=2
        )
        result = self.agent.run(task)
        if result.status == "done" and result.final.strip():
            body = result.final
        else:
            body = fallback_maintainer(evidence)
        body = body.rstrip() + f"\n\n{DISCLAIMER}"

        record = AgentRunRecord(
            run_key=run_key,
            agent_id=self.agent_id,
            ts=int(time.time() * 1000),
            input_digest=task[:2000],
            output=body,
        )
        if dry_run:
            print(f"[dry-run] {body}")
        else:
            ok = self.hub.send(Notification(title=f"策略维护建议 {period}", body=body, channel=self.channel))
            if ok:
                record.pushed_to.append(self.channel)
            else:
                record.status = "push_pending"
        self._record(record)
        return record


class AgentScheduler:
    """极简调度器：把长期 Agent 挂到 cron 表达式上。

    生产可替换为 APScheduler / 平台任务队列；此处保持无外部依赖，便于骨架运行。
    """

    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def every(self, agent: LongRunningAgent, *, cron: str, handler: Callable[[], Any]) -> None:
        self.jobs.append({"agent_id": agent.agent_id, "cron": cron, "handler": handler})

    def run_due(self, now: time.struct_time | None = None) -> list[Any]:
        """占位：真实实现应解析 cron 并判断是否到期。此处返回空，避免误触发。"""
        return []
