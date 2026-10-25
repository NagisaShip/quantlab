"""Agent 团队调研（对应新增需求：Agent 团队调研策略）。

一支团队 = 若干角色 + 编排模式。三种模式：

1. sequential（流水线）：角色按顺序接力，后一个角色能看到前面所有角色的产出。
   适合"分析 → 设计 → 实现 → 回测解读 → 风控 → 汇总"这类有依赖的流程。
2. parallel（并行评审）：所有角色独立看同一份材料，然后由汇总角色整合。
   适合"多视角评审"——避免后发言者被前发言者锚定。
3. debate（质疑回合）：先并行出一轮观点，再让质疑角色针对性反驳，最后汇总。
   适合高风险决策，专门暴露隐藏假设。

每个角色可以指定自己的模型（Role.model），所以"同一团队里用不同大模型"是配置能力。
所有角色共享平台红线：禁止下单、禁止未来数据、禁止收益承诺。
"""

from __future__ import annotations

import contextvars
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..context.manager import ContextManager
from ..llm.router import ModelRouter
from ..platform import context
from ..store import Collections, JsonStore
from .base import Agent, AgentRun
from .qmoji import decorate_qmoji, metrics_from_materials
from .roles import Role, RoleStore

TEAM_SAFETY_PREFIX = (
    "【团队共同红线】禁止调用任何交易 / 下单接口（模拟盘下单也必须由研究者确认）；禁止使用未来数据；"
    "联网查询只允许只读公开资料，不能把外部资料说成平台实测；"
    "不得给出投资建议或收益承诺；区分事实与推测；不确定时明确说明。\n"
    "【证据规则】\n"
    "1. 每条观点前标注：【事实】可由代码 / 数据 / 回测结果直接验证（注明出处）；【假设】合理但未验证；"
    "没有数据、计算过程或明确假设支持的定量预测（年化收益、回撤区间、触发概率等）一律不要给，"
    "直接写「无法判断，需要 X 数据」；\n"
    "2. 只说你这个角色职责内、别人还没说过的新问题或新证据；同意前面的观点时一句话带过，不要重复论述——"
    "多个角色重复同一意见不等于多份独立证据；\n"
    "3. 数据覆盖不足、只有一段样本、没有交易明细时，先指出缺口，不要据此推断市场状态或策略适用性；\n"
    "4. 成本要写清单边还是往返、费率与滑点的单位，并基于实际交易次数估算；仓位上限、止损距离、单笔账户风险分开说；\n"
    "5. 汇总时以证据质量而不是共识数量或措辞流畅度为准。\n\n"
    "【Qmoji 表情】\n"
    "- 回测夏普 ≥ 2.5、超额收益 ≥ 3% 且交易数足够时，在结论末尾单独一行附 :qbot-alpha:；超额 ≥ 10% 用 :qbot-moon:。\n"
    "- 风控因回撤过大 / 手续费口径不对而拦截时，附 :qbot-no: 或 :qbot-question:。\n"
    "- 严重亏损（总收益 ≤ -10%）附 :qbot-cry:；大回撤附 :qbot-drawdown:。\n"
    "- 提到过拟合 / 未来函数 / 插针时分别附 :qbot-overfit: / :qbot-future: / :qbot-wick:。\n"
    "- 每条发言最多一个 Qmoji，放在结论末尾；不插入代码块、表格或数字中间；没有数据不要强行发。\n\n"
)

TEAM_TOOL_HINT = (
    "\n【工具使用】涉及回测评审时，先用 list_backtests 找 backtest_id，再用 list_trades 核对逐笔成交、"
    "用 trade_breakdown 看出场原因与分组；不要在没有明细时断言“读不到逐笔”。"
    "算成本、收益、额度、仓位或复利时优先用 calculator，不要心算。"
    "检查后台任务是否有人执行时用 node_health。\n"
)

#: 并行评审时附在任务后：各角色同时、独立发言，只写本领域的核心洞察，交给组长消解冲突
PARALLEL_BRIEF = (
    "\n\n【并行评审】各角色同时独立发言，互相看不到。只写你职责范围内最重要的洞察，≤150 字（要点 + 数字），"
    "其他领域留给对应角色，不要铺垫、不要复述材料；研究组长会汇总并消解分歧。"
)


@dataclass
class TeamRunStep:
    role_id: str
    role_name: str
    model: str
    status: str
    output: str
    elapsed_ms: int = 0
    error: str = ""
    runtime_run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


@dataclass
class TeamRun:
    id: str
    team_id: str
    team_name: str
    topic: str
    mode: str
    context: str = ""
    steps: list[TeamRunStep] = field(default_factory=list)
    synthesis: str = ""
    status: str = "running"
    created_at: float = field(default_factory=time.time)
    runtime_run_id: str = ""
    runtime_event_seq: int = 0
    can_manage: bool = False
    source_project_id: str | None = None
    source_policy_version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "topic": self.topic,
            "mode": self.mode,
            "context": self.context,
            **({"source_project_id": self.source_project_id} if self.source_project_id is not None else {}),
            "source_policy_version": self.source_policy_version,
            "steps": [s.to_dict() for s in self.steps],
            "synthesis": self.synthesis,
            "status": self.status,
            "created_at": self.created_at,
            "runtime_run_id": self.runtime_run_id,
            "runtime_event_seq": self.runtime_event_seq,
            "can_manage": self.can_manage,
        }


@dataclass
class Team:
    id: str
    name: str
    description: str = ""
    mode: str = "sequential"  # sequential | parallel | debate
    role_ids: list[str] = field(default_factory=list)
    synthesizer_id: str = ""  # 汇总角色；空则用最后一个角色
    challenge_rounds: int = 1  # debate 模式下的质疑回合数
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__

    @classmethod
    def from_dict(cls, data: dict) -> Team:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# 工具注册表：角色声明的 tools 名 → 实际 handler（由平台注入）
# ---------------------------------------------------------------------------
class ToolRegistry:
    """平台工具的统一登记处：角色、团队、策略室共用。

    旧式工具只登记一个接收 query 字符串的 handler；新式工具同时登记中文名、说明与参数 JSON Schema，
    模型按 schema 传关键字参数。前端的角色编辑器用 catalog() 列出可选工具。
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._specs: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        label: str = "",
        description: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self._handlers[name] = handler
        self._specs[name] = {
            "name": name,
            "label": label or name,
            "description": description or f"平台工具 {name}",
            "parameters": parameters,
            "handler": handler,
        }

    def get(self, name: str) -> Callable[..., Any] | None:
        return self._handlers.get(name)

    def spec(self, name: str) -> dict[str, Any] | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._handlers)

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"name": s["name"], "label": s["label"], "description": s["description"]}
            for s in sorted(self._specs.values(), key=lambda x: x["name"])
        ]


class TeamRunner:
    """执行团队调研。任何角色失败都不中断整队，结果里标注该角色失败。"""

    def __init__(
        self,
        router: ModelRouter,
        role_store: RoleStore,
        *,
        store: JsonStore | None = None,
        tools: ToolRegistry | None = None,
        max_workers: int = 4,
        platform_tools: bool = False,
    ) -> None:
        self.router = router
        self.roles = role_store
        self.store = store or JsonStore()
        self.tools = tools or ToolRegistry()
        self.max_workers = max_workers
        # API 注入时开启：团队角色与对话 Agent 共用平台只读 / 运行工具（confirm 类不开放）。
        self.platform_tools = platform_tools
        # Set by AppServices when QAGENT_AGENT_RUNTIME=1 is enabled. A shared
        # Journal owns role invocations and all Team execution graphs, including
        # sequential dependencies and round-specific debate snapshots.
        self.runtime_journal: Any = None
        self.source_visible: Callable[[str], bool] | None = None
        self.run_visible: Callable[[dict, str | None], bool] | None = None

    # ---------------- 团队 CRUD ----------------
    def list_teams(self) -> list[Team]:
        return [Team.from_dict(d) for d in self.store.list(Collections.TEAMS)]

    def get_team(self, team_id: str) -> Team | None:
        data = self.store.get(Collections.TEAMS, team_id)
        return Team.from_dict(data) if data else None

    def save_team(self, team: Team) -> Team:
        self.store.upsert(Collections.TEAMS, team.to_dict())
        return team

    def delete_team(self, team_id: str) -> bool:
        return self.store.delete(Collections.TEAMS, team_id)

    def default_team(self) -> Team:
        """默认团队：五角色流水线（分析→设计→回测→风控→汇总）。"""
        for t in self.list_teams():
            if t.id == "default_research_team":
                return t
        team = Team(
            id="default_research_team",
            name="默认调研团队",
            description="市场分析 → 策略设计 → 回测解读 → 风控 → 汇总，五角色流水线。",
            mode="sequential",
            role_ids=["market_analyst", "strategy_designer", "backtest_analyst", "risk_officer"],
            synthesizer_id="research_lead",
        )
        return self.save_team(team)

    # ---------------- 执行 ----------------
    def run(
        self,
        team: Team,
        topic: str,
        *,
        context: str = "",
        materials: dict[str, Any] | None = None,
        source_project_id: str = "",
    ) -> TeamRun:
        self._assert_source_visible(source_project_id)
        run = TeamRun(
            id=uuid.uuid4().hex[:12],
            team_id=team.id,
            team_name=team.name,
            topic=topic,
            mode=team.mode,
            context=context,
            source_project_id=source_project_id,
            source_policy_version=1,
        )
        roles = [r for r in (self.roles.get(rid) for rid in team.role_ids) if r is not None]
        if not roles:
            run.status = "failed"
            run.synthesis = "团队未配置任何有效角色"
            self._persist(run)
            return run

        shared = self._build_material(topic, context, materials)
        qmetrics = metrics_from_materials(materials)

        if self.runtime_journal is not None:
            from .team_runtime import start_team

            synthesizer = self.roles.get(team.synthesizer_id) if team.synthesizer_id else None
            return start_team(
                self, run, roles, synthesizer or roles[-1], shared, qmetrics, rounds=team.challenge_rounds
            )
        if team.mode == "parallel":
            run.steps = self._run_parallel(roles, shared, qmetrics)
        elif team.mode == "debate":
            run.steps = self._run_debate(roles, shared, team.challenge_rounds, qmetrics)
        else:
            run.steps = self._run_sequential(roles, shared, qmetrics)

        # 汇总
        synthesizer = self.roles.get(team.synthesizer_id) if team.synthesizer_id else None
        if synthesizer is None:
            synthesizer = roles[-1]
        run.synthesis = self._synthesize(synthesizer, topic, run.steps, qmetrics)

        ok = any(s.status == "ok" for s in run.steps)
        run.status = "done" if ok else "failed"
        self._persist(run)
        return run

    def _persist(self, run: TeamRun) -> None:
        self.store.upsert(Collections.TEAM_RUNS, run.to_dict())

    def _source_is_visible(self, source_project_id: str) -> bool:
        return not source_project_id or bool(self.source_visible and self.source_visible(source_project_id))

    def _assert_source_visible(self, source_project_id: str) -> None:
        if not self._source_is_visible(source_project_id):
            from ..platform.errors import AppError, ErrorCode

            raise AppError(ErrorCode.PERMISSION_DENIED, "团队评审的来源会话未向你公开")

    def _record_is_visible(self, record: dict, owner_id: str | None = None) -> bool:
        if self.run_visible is not None:
            return self.run_visible(record, owner_id)
        return self._source_is_visible(record.get("source_project_id", ""))

    def _assert_record_visible(self, record: dict, owner_id: str | None = None) -> None:
        if not self._record_is_visible(record, owner_id):
            from ..platform.errors import AppError, ErrorCode

            raise AppError(ErrorCode.PERMISSION_DENIED, "团队评审来源未公开或历史来源尚未确认")

    def list_runs(self, *, limit: int = 30) -> list[dict]:
        records = [
            item for item in self.store.list(Collections.TEAM_RUNS, limit=limit) if self._record_is_visible(item)
        ]
        if self.runtime_journal is not None:
            from .team_runtime import recover_indexes

            records = recover_indexes(self, records, limit)
        return records

    def get_run(self, run_id: str) -> dict | None:
        if self.runtime_journal is not None:
            from .team_runtime import get_run

            result = get_run(self, run_id)
            if result is not None:
                return result.to_dict()
        record = self.store.get(Collections.TEAM_RUNS, run_id)
        if record:
            self._assert_record_visible(record)
        return record

    def resume_run(self, run_id: str) -> TeamRun:
        from .team_runtime import resume

        return resume(self, run_id)

    def cancel_run(self, run_id: str, *, signal_id: str) -> TeamRun:
        from .team_runtime import cancel

        return cancel(self, run_id, signal_id=signal_id)

    # ---------------- 编排模式 ----------------
    def _run_sequential(
        self, roles: list[Role], material: str, qmetrics: dict[str, Any] | None = None
    ) -> list[TeamRunStep]:
        steps: list[TeamRunStep] = []
        transcript: list[str] = []
        for role in roles:
            task = material
            if transcript:
                task += "\n\n【前序角色产出】\n" + "\n\n".join(transcript)
            step = self._invoke(role, task, qmetrics=qmetrics)
            steps.append(step)
            if step.status == "ok":
                transcript.append(f"### {role.name}\n{step.output}")
        return steps

    def _run_parallel(
        self, roles: list[Role], material: str, qmetrics: dict[str, Any] | None = None
    ) -> list[TeamRunStep]:
        task = material + PARALLEL_BRIEF

        def one(role: Role) -> TeamRunStep:
            return self._invoke(role, task, qmetrics=qmetrics)

        # 每个线程带上当前请求上下文（工作区 + 成员）：模型服务商按上下文解析，
        # 不带的话并行角色看不到任何服务商，全部「所有模型均失败」
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(roles))) as pool:
            futures = [pool.submit(contextvars.copy_context().run, one, r) for r in roles]
            return [f.result() for f in futures]

    def _run_debate(
        self, roles: list[Role], material: str, rounds: int, qmetrics: dict[str, Any] | None = None
    ) -> list[TeamRunStep]:
        steps = self._run_parallel(roles, material, qmetrics)
        for r in range(max(1, rounds)):
            critics = [role for role in roles if "质疑" in role.tags or "风控" in role.tags]
            if not critics:
                critics = roles[-1:]
            prior = "\n\n".join(f"### {s.role_name}\n{s.output}" for s in steps if s.status == "ok")
            for role in critics:
                task = (
                    f"{material}\n\n【其他角色的观点】\n{prior}\n\n"
                    f"请针对以上观点进行第 {r + 1} 轮质疑：指出最脆弱的假设、构造具体反例、"
                    "指出哪些结论看起来像证据其实不是。"
                )
                steps.append(self._invoke(role, task, qmetrics=qmetrics))
        return steps

    #: 汇总时每个角色的发言最多带多少字：防止多角色长发言把汇总请求撑爆上下文 / 输出上限
    SYNTH_CHARS_PER_STEP = 1800

    def _synthesize(
        self, role: Role, topic: str, steps: list[TeamRunStep], qmetrics: dict[str, Any] | None = None
    ) -> str:
        if not steps:
            return ""
        cap = self.SYNTH_CHARS_PER_STEP

        def body(s: TeamRunStep) -> str:
            if s.status != "ok":
                return "[失败] " + (s.error or "")[:200]
            out = s.output.strip()
            return out if len(out) <= cap else out[:cap] + "……（已截断）"

        transcript = "\n\n".join(f"### {s.role_name}（{s.model or '默认模型'}）\n{body(s)}" for s in steps)
        task = (
            f"调研主题：{topic}\n\n"
            f"以下是团队各角色的独立意见：\n\n{transcript}\n\n"
            "请按你的角色要求汇总：共识、分歧与你的判断、结论、下一步行动项。不要再调用工具，直接写汇总。"
        )
        # 汇总只需整合已有发言：不给工具，避免汇总人把步数花在查数据上、最后没写结论（曾返回「未产出内容」）
        step = self._invoke(role, task, max_steps=2, use_tools=False, qmetrics=qmetrics)
        if step.status == "ok":
            return step.output
        return decorate_qmoji(
            fallback_synthesis(steps, reason=step.error), role=role.id, tags=role.tags, metrics=qmetrics
        )

    # ---------------- 单角色调用 ----------------
    def _invoke(
        self,
        role: Role,
        task: str,
        *,
        max_steps: int = 6,
        use_tools: bool = True,
        qmetrics: dict[str, Any] | None = None,
    ) -> TeamRunStep:
        started = time.time()
        agent = self._make_agent(role, max_steps=max_steps, use_tools=use_tools)
        try:
            run: AgentRun = agent.run(task)
        except Exception as e:  # 单角色失败不影响整队
            return TeamRunStep(
                role_id=role.id,
                role_name=role.name,
                model=role.model or "默认",
                status="error",
                output="",
                elapsed_ms=int((time.time() - started) * 1000),
                error=str(e),
            )
        status = "ok" if run.status == "done" and run.final.strip() else "error"
        output = (
            decorate_qmoji(run.final, role=role.id, tags=role.tags, metrics=qmetrics) if status == "ok" else run.final
        )
        return TeamRunStep(
            role_id=role.id,
            role_name=role.name,
            model=run.model or (role.model or "默认"),
            status=status,
            output=output,
            elapsed_ms=int((time.time() - started) * 1000),
            error="" if status == "ok" else (run.final or "未产出内容"),
            runtime_run_id=run.run_id if self.runtime_journal is not None else "",
        )

    def _make_agent(
        self,
        role: Role,
        *,
        max_steps: int = 6,
        use_tools: bool = True,
        tool_names: list[str] | None = None,
    ) -> Agent:
        # 默认把平台工具都交给角色、由 AI 自己决定何时调用；角色勾了「只用指定工具」才收窄
        if getattr(role, "restrict_tools", False):
            names = list(role.tools)
        else:
            # 团队与对话 Agent 使用同一套平台只读 / 运行工具；confirm 类操作没有人工审批入口，不交给角色。
            names = sorted({*self.tools.names(), *self._platform_tool_names(), *role.tools})
        if not use_tools:
            names = []
        if tool_names is not None:
            names = tool_names
        tools = [t for t in (self._make_tool(name) for name in names) if t is not None]
        agent_context = ContextManager(
            system_prompt=self._system_prompt(role) + (TEAM_TOOL_HINT if self.platform_tools else "")
        )
        return Agent(
            name=role.id,
            role="reasoning",
            router=self.router,
            context=agent_context,
            tools=tools,
            temperature=role.temperature,
            max_steps=max_steps,
            model=role.model or None,
            effort=role.effort or None,
            runtime_journal=self.runtime_journal,
        )

    @staticmethod
    def _system_prompt(role: Role) -> str:
        parts = [TEAM_SAFETY_PREFIX, role.system_prompt]
        if role.output_format:
            parts.append(f"\n【输出格式要求】{role.output_format}")
        return "".join(parts)

    def _platform_tool_names(self) -> list[str]:
        if not self.platform_tools:
            return []
        from . import platform_tools as PT

        return [name for name, tool in PT.available_registry().items() if tool.risk != "confirm"]

    def _make_tool(self, name: str):
        from .base import Tool

        handler = self.tools.get(name)
        if handler is not None:
            spec = self.tools.spec(name) or {}
            if spec.get("parameters"):
                return Tool(
                    name=name,
                    description=spec["description"],
                    parameters=spec["parameters"],
                    handler=lambda **kw: handler(**kw),
                )
            return Tool(
                name=name,
                description=spec.get("description") or f"平台工具 {name}",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": []},
                handler=lambda **kw: handler(kw.get("query", "")),
            )
        if not self.platform_tools:
            return None
        from . import platform_tools as PT

        tool = PT.registry().get(name)
        if tool is None or tool.risk == "confirm":
            return None
        try:
            user = context.current().user_id or "system"
        except Exception:
            user = "system"
        return Tool(
            name=name,
            description=f"{tool.title}：{tool.description} 可用时机：{tool.when}",
            parameters=tool.schema(),
            handler=lambda **kw: PT.invoke(name, kw, user=user)["result"],
        )

    @staticmethod
    def _build_material(topic: str, context: str, materials: dict[str, Any] | None) -> str:
        import json

        parts = [f"【调研主题】{topic}"]
        if context:
            parts.append(f"【背景】{context}")
        if materials:
            parts.append("【平台数据】\n" + json.dumps(materials, ensure_ascii=False, indent=2))
        return "\n\n".join(parts)


def fallback_synthesis(steps: list[TeamRunStep], *, reason: str = "") -> str:
    """模型汇总失败（超时、额度、上下文过长）时的规则汇总：把成功角色的要点按原样列出，标明未经模型整合。"""
    ok = [s for s in steps if s.status == "ok" and s.output.strip()]
    if not ok:
        return ""
    lines = [
        f"【规则汇总】模型汇总未完成（{(reason or '未产出内容')[:120]}），以下为各角色要点原文摘录，未经整合与取舍："
    ]
    for s in ok:
        first = " ".join(s.output.strip().split())[:400]
        lines.append(f"- **{s.role_name}**：{first}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 预设团队
# ---------------------------------------------------------------------------
def builtin_teams() -> list[Team]:
    return [
        Team(
            id="default_research_team",
            name="默认调研团队",
            description="市场分析 → 策略设计 → 回测解读 → 风控 → 汇总，五角色流水线。",
            mode="sequential",
            role_ids=["market_analyst", "strategy_designer", "backtest_analyst", "risk_officer"],
            synthesizer_id="research_lead",
        ),
        Team(
            id="strategy_build_team",
            name="策略落地团队",
            description="策略设计 → 工程实现 → 执行成本评估 → 风控 → 汇总，面向把想法变成可回测策略。",
            mode="sequential",
            role_ids=["strategy_designer", "quant_engineer", "execution_specialist", "risk_officer"],
            synthesizer_id="research_lead",
        ),
        Team(
            id="review_board",
            name="多视角评审团",
            description="并行评审：各角色独立看同一份材料后整合，避免相互锚定。",
            mode="parallel",
            role_ids=[
                "market_analyst",
                "backtest_analyst",
                "risk_officer",
                "execution_specialist",
                "compliance_officer",
            ],
            synthesizer_id="research_lead",
        ),
        Team(
            id="adversarial_team",
            name="对抗审查组",
            description="先并行出观点，再由反方质疑者与风控官针对性反驳，最后汇总。",
            mode="debate",
            role_ids=["strategy_designer", "backtest_analyst", "devil_advocate", "risk_officer"],
            synthesizer_id="research_lead",
            challenge_rounds=1,
        ),
        Team(
            id="factor_team",
            name="因子小组",
            description="数据工程师准备数据 → 因子研究员提出假设与表达式 → 研究审计员质询 → 组长决定是否入库。",
            mode="sequential",
            role_ids=["data_engineer", "factor_researcher", "research_auditor"],
            synthesizer_id="research_lead",
        ),
        Team(
            id="model_team",
            name="模型小组",
            description="数据工程师定标签与切分 → 机器学习工程师设计模型 → 研究审计员查泄漏 → 组长决定是否上线。",
            mode="sequential",
            role_ids=["data_engineer", "ml_engineer", "research_auditor"],
            synthesizer_id="research_lead",
        ),
        Team(
            id="portfolio_team",
            name="组合小组",
            description="风险建模师估计风险 → 组合经理比较优化器 → 风控官做压力测试 → 组长拍板。",
            mode="sequential",
            role_ids=["risk_modeler", "portfolio_manager", "risk_officer"],
            synthesizer_id="research_lead",
        ),
        Team(
            id="execution_team",
            name="执行小组",
            description="市场分析师、执行与成本专家、研究审计员并行给出各自领域的结论，研究组长消解冲突后拍板。",
            mode="parallel",
            role_ids=["market_analyst", "execution_specialist", "research_auditor"],
            synthesizer_id="research_lead",
        ),
        Team(
            id=LAB_REVIEW_TEAM,
            name="研究评审组",
            description="AI 研究室默认的评审组：市场、成本执行、代码审计三个角色互不依赖，并行各写 ≤150 字的核心洞察，"
            "研究组长消解冲突并汇总。比串行接力快，也不会后发言的人复述前面的观点。",
            mode="parallel",
            role_ids=["market_analyst", "execution_specialist", "research_auditor"],
            synthesizer_id="research_lead",
        ),
    ]


#: AI 研究室 /team、/discuss 默认使用的团队
LAB_REVIEW_TEAM = "lab_review"

#: 出厂团队改过编排时旧出厂版本的样子：库里的团队还是这个样子（用户没改过）就升级到新版
_LEGACY_TEAMS: dict[str, list[dict[str, Any]]] = {
    "execution_team": [
        {"mode": "sequential", "role_ids": ["market_analyst", "execution_specialist", "research_auditor"]},
    ],
}


def sync_builtin_teams(store: Any) -> None:
    """把预设团队写进工作区：缺的补上；还是旧出厂编排的升级到新版（用户改过的不动）。"""
    existing = {t["id"]: t for t in store.list(Collections.TEAMS)}
    for team in builtin_teams():
        new = team.to_dict()
        cur = existing.get(team.id)
        if cur is None:
            store.upsert(Collections.TEAMS, new)
            continue
        for old in _LEGACY_TEAMS.get(team.id, []):
            if all(cur.get(k) == v for k, v in old.items()):
                store.upsert(Collections.TEAMS, {**cur, **{k: new[k] for k in ("mode", "description", "role_ids")}})
                break
