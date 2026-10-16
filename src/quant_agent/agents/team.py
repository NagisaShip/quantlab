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

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..context.manager import ContextManager
from ..llm.router import ModelRouter
from ..store import Collections, JsonStore
from .base import Agent, AgentRun
from .roles import Role, RoleStore

TEAM_SAFETY_PREFIX = (
    "【团队共同红线】禁止调用交易所、网络或任何下单接口；禁止使用未来数据；"
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "topic": self.topic,
            "mode": self.mode,
            "context": self.context,
            "steps": [s.to_dict() for s in self.steps],
            "synthesis": self.synthesis,
            "status": self.status,
            "created_at": self.created_at,
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
    ) -> None:
        self.router = router
        self.roles = role_store
        self.store = store or JsonStore()
        self.tools = tools or ToolRegistry()
        self.max_workers = max_workers

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
    ) -> TeamRun:
        run = TeamRun(
            id=uuid.uuid4().hex[:12],
            team_id=team.id,
            team_name=team.name,
            topic=topic,
            mode=team.mode,
            context=context,
        )
        roles = [r for r in (self.roles.get(rid) for rid in team.role_ids) if r is not None]
        if not roles:
            run.status = "failed"
            run.synthesis = "团队未配置任何有效角色"
            self._persist(run)
            return run

        shared = self._build_material(topic, context, materials)

        if team.mode == "parallel":
            run.steps = self._run_parallel(roles, shared)
        elif team.mode == "debate":
            run.steps = self._run_debate(roles, shared, team.challenge_rounds)
        else:
            run.steps = self._run_sequential(roles, shared)

        # 汇总
        synthesizer = self.roles.get(team.synthesizer_id) if team.synthesizer_id else None
        if synthesizer is None:
            synthesizer = roles[-1]
        run.synthesis = self._synthesize(synthesizer, topic, run.steps)

        ok = any(s.status == "ok" for s in run.steps)
        run.status = "done" if ok else "failed"
        self._persist(run)
        return run

    def _persist(self, run: TeamRun) -> None:
        self.store.upsert(Collections.TEAM_RUNS, run.to_dict())

    def list_runs(self, *, limit: int = 30) -> list[dict]:
        return self.store.list(Collections.TEAM_RUNS, limit=limit)

    def get_run(self, run_id: str) -> dict | None:
        return self.store.get(Collections.TEAM_RUNS, run_id)

    # ---------------- 编排模式 ----------------
    def _run_sequential(self, roles: list[Role], material: str) -> list[TeamRunStep]:
        steps: list[TeamRunStep] = []
        transcript: list[str] = []
        for role in roles:
            task = material
            if transcript:
                task += "\n\n【前序角色产出】\n" + "\n\n".join(transcript)
            step = self._invoke(role, task)
            steps.append(step)
            if step.status == "ok":
                transcript.append(f"### {role.name}\n{step.output}")
        return steps

    def _run_parallel(self, roles: list[Role], material: str) -> list[TeamRunStep]:
        def one(role: Role) -> TeamRunStep:
            return self._invoke(role, material)

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(roles))) as pool:
            return list(pool.map(one, roles))

    def _run_debate(self, roles: list[Role], material: str, rounds: int) -> list[TeamRunStep]:
        steps = self._run_parallel(roles, material)
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
                steps.append(self._invoke(role, task))
        return steps

    def _synthesize(self, role: Role, topic: str, steps: list[TeamRunStep]) -> str:
        if not steps:
            return ""
        transcript = "\n\n".join(
            f"### {s.role_name}（{s.model or '默认模型'}）\n{s.output if s.status == 'ok' else '[失败] ' + s.error}"
            for s in steps
        )
        task = (
            f"调研主题：{topic}\n\n"
            f"以下是团队各角色的独立意见：\n\n{transcript}\n\n"
            "请按你的角色要求汇总：共识、分歧与你的判断、结论、下一步行动项。"
        )
        step = self._invoke(role, task, max_steps=3)
        return step.output if step.status == "ok" else f"汇总失败：{step.error}"

    # ---------------- 单角色调用 ----------------
    def _invoke(self, role: Role, task: str, *, max_steps: int = 6) -> TeamRunStep:
        started = time.time()
        # 默认把平台工具都交给角色、由 AI 自己决定何时调用；角色勾了「只用指定工具」才收窄
        names = role.tools if getattr(role, "restrict_tools", False) else sorted({*self.tools.names(), *role.tools})
        tools = [t for t in (self._make_tool(name) for name in names) if t is not None]
        agent = Agent(
            name=role.id,
            role="reasoning",
            router=self.router,
            context=ContextManager(system_prompt=self._system_prompt(role)),
            tools=tools,
            temperature=role.temperature,
            max_steps=max_steps,
            model=role.model or None,
            effort=role.effort or None,
        )
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
        return TeamRunStep(
            role_id=role.id,
            role_name=role.name,
            model=run.model or (role.model or "默认"),
            status=status,
            output=run.final,
            elapsed_ms=int((time.time() - started) * 1000),
            error="" if status == "ok" else (run.final or "未产出内容"),
        )

    @staticmethod
    def _system_prompt(role: Role) -> str:
        parts = [TEAM_SAFETY_PREFIX, role.system_prompt]
        if role.output_format:
            parts.append(f"\n【输出格式要求】{role.output_format}")
        return "".join(parts)

    def _make_tool(self, name: str):
        from .base import Tool

        handler = self.tools.get(name)
        if handler is None:
            return None
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

    @staticmethod
    def _build_material(topic: str, context: str, materials: dict[str, Any] | None) -> str:
        import json

        parts = [f"【调研主题】{topic}"]
        if context:
            parts.append(f"【背景】{context}")
        if materials:
            parts.append("【平台数据】\n" + json.dumps(materials, ensure_ascii=False, indent=2))
        return "\n\n".join(parts)


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
            description="市场分析师判断盘口状态 → 执行与成本专家比较拆单算法 → 研究审计员复核成本模型 → 组长拍板。",
            mode="sequential",
            role_ids=["market_analyst", "execution_specialist", "research_auditor"],
            synthesizer_id="research_lead",
        ),
    ]
