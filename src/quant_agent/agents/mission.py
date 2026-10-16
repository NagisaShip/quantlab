"""指挥台：研究者是指挥官，AI 角色是参谋团队。

一次「作战任务」（Mission）= 一条指令 + 若干阶段（Stage），每个阶段由若干角色按编排模式发言：
- 单周期作业：选一支已有团队跑一轮（流水线 / 并行 / 对抗），最后由汇总角色给出结论。
- 流水线作业：多个阶段串起来（侦察 → 设计 → 红队质询 → 决策），阶段之间可设「关卡」，
  指挥官批准后才进入下一段。

发言逐条落库（先放一个“正在发言”的占位气泡，完成后替换），前端轮询即可像看小组讨论一样看到过程。
指挥官可以随时插话（之后发言的角色都能看到）、点名追问某个角色、在关卡处批准或叫停。

没有模型密钥时，每个角色按自己的职责用平台数据做规则化发言，并标注「规则降级」。
所有角色共享平台红线：禁止下单、禁止未来数据、禁止收益承诺。
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .roles import Role
from .team import PARALLEL_BRIEF, TeamRunner

COLLECTION = "missions"
FEEDS = "mission_feeds"

PIPELINE_PRESETS: dict[str, dict[str, Any]] = {
    "full": {
        "name": "完整研究流水线",
        "description": "侦察市场 → 设计方案 → 红队质询 → 组长决策。设计完成后停下等你批准。",
        "stages": [
            {
                "key": "scout",
                "name": "侦察 · 市场与执行环境",
                "mode": "parallel",
                "role_ids": ["market_analyst", "execution_specialist"],
                "gate": False,
            },
            {
                "key": "design",
                "name": "设计 · 策略方案与实现要点",
                "mode": "sequential",
                "role_ids": ["strategy_designer", "quant_engineer"],
                "gate": True,
            },
            {
                "key": "redteam",
                "name": "红队 · 质询与风控",
                "mode": "debate",
                "role_ids": ["backtest_analyst", "devil_advocate", "risk_officer"],
                "gate": False,
            },
        ],
        "synthesizer_id": "research_lead",
    },
    "fast": {
        "name": "快速评估",
        "description": "市场分析与策略设计并行出观点，风控官把关，组长拍板。",
        "stages": [
            {
                "key": "views",
                "name": "观点 · 市场与策略",
                "mode": "parallel",
                "role_ids": ["market_analyst", "strategy_designer"],
                "gate": False,
            },
            {
                "key": "risk",
                "name": "把关 · 风险评估",
                "mode": "sequential",
                "role_ids": ["risk_officer"],
                "gate": False,
            },
        ],
        "synthesizer_id": "research_lead",
    },
    "redteam": {
        "name": "红队审查",
        "description": "专门挑刺：回测解读、反方质疑、合规审查，适合上线前的最后一道关。",
        "stages": [
            {
                "key": "evidence",
                "name": "证据 · 回测解读",
                "mode": "sequential",
                "role_ids": ["backtest_analyst"],
                "gate": False,
            },
            {
                "key": "attack",
                "name": "攻击 · 反方与合规",
                "mode": "debate",
                "role_ids": ["devil_advocate", "compliance_officer", "risk_officer"],
                "gate": False,
            },
        ],
        "synthesizer_id": "research_lead",
    },
    "factor": {
        "name": "因子小组",
        "description": "数据准备 → 两名研究员并行提假设 → 审计员质询 → 组长决定入库（关卡）。",
        "stages": [
            {
                "key": "data",
                "name": "数据 · 字段与时点",
                "mode": "sequential",
                "role_ids": ["data_engineer"],
                "gate": False,
            },
            {
                "key": "ideas",
                "name": "假设 · 因子与表达式",
                "mode": "parallel",
                "role_ids": ["factor_researcher", "market_analyst"],
                "gate": True,
            },
            {
                "key": "audit",
                "name": "审计 · 泄漏与过拟合",
                "mode": "debate",
                "role_ids": ["research_auditor", "devil_advocate"],
                "gate": False,
            },
        ],
        "synthesizer_id": "research_lead",
    },
    "model": {
        "name": "模型小组",
        "description": "标签与切分 → 模型设计 → 泄漏审计 → 组长决定上线（关卡）。",
        "stages": [
            {
                "key": "data",
                "name": "数据 · 标签与切分",
                "mode": "sequential",
                "role_ids": ["data_engineer"],
                "gate": False,
            },
            {
                "key": "model",
                "name": "建模 · 基线与候选",
                "mode": "sequential",
                "role_ids": ["ml_engineer"],
                "gate": True,
            },
            {
                "key": "audit",
                "name": "审计 · 泄漏与过拟合",
                "mode": "debate",
                "role_ids": ["research_auditor", "devil_advocate"],
                "gate": False,
            },
        ],
        "synthesizer_id": "research_lead",
    },
    "portfolio": {
        "name": "组合小组",
        "description": "风险估计 → 优化器比较 → 压力测试 → 组长拍板（关卡）。",
        "stages": [
            {
                "key": "risk",
                "name": "风险 · 模型与尾部",
                "mode": "sequential",
                "role_ids": ["risk_modeler"],
                "gate": False,
            },
            {
                "key": "alloc",
                "name": "配置 · 优化器与约束",
                "mode": "sequential",
                "role_ids": ["portfolio_manager"],
                "gate": True,
            },
            {
                "key": "stress",
                "name": "压力 · 情景与风控",
                "mode": "debate",
                "role_ids": ["risk_officer", "devil_advocate"],
                "gate": False,
            },
        ],
        "synthesizer_id": "research_lead",
    },
    "execution": {
        "name": "执行小组",
        "description": "盘口状态 → 拆单算法比较 → 成本模型复核 → 组长拍板。",
        "stages": [
            {
                "key": "state",
                "name": "状态 · 盘口与流动性",
                "mode": "sequential",
                "role_ids": ["market_analyst"],
                "gate": False,
            },
            {
                "key": "algo",
                "name": "算法 · 拆单与参数",
                "mode": "sequential",
                "role_ids": ["execution_specialist"],
                "gate": True,
            },
            {
                "key": "audit",
                "name": "复核 · 成本模型",
                "mode": "sequential",
                "role_ids": ["research_auditor"],
                "gate": False,
            },
        ],
        "synthesizer_id": "research_lead",
    },
}

_LOCKS: dict[str, threading.Lock] = {}
_GUARD = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


class MissionStore:
    def __init__(self, store: Any) -> None:
        self.store = store

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = sorted(self.store.list(COLLECTION), key=lambda r: r.get("created_at", 0), reverse=True)
        return rows[:limit]

    def get(self, mid: str) -> dict[str, Any] | None:
        return self.store.get(COLLECTION, mid)

    def save(self, m: dict[str, Any]) -> dict[str, Any]:
        m["updated_at"] = time.time()
        self.store.upsert(COLLECTION, m)
        return m

    def patch(self, mid: str, **changes: Any) -> dict[str, Any] | None:
        with _lock(f"m:{mid}"):
            cur = self.get(mid)
            if cur is None:
                return None
            cur.update(changes)
            return self.save(cur)

    def delete(self, mid: str) -> bool:
        self.store.delete_blob(FEEDS, mid)
        return self.store.delete(COLLECTION, mid)

    def feed(self, mid: str, after: int = 0, since: float = 0.0) -> list[dict[str, Any]]:
        data = self.store.get_blob(FEEDS, mid) or {}
        return [u for u in data.get("items", []) if u["seq"] > after or (since and u.get("updated_at", 0) > since)]

    def post(self, mid: str, item: dict[str, Any]) -> dict[str, Any]:
        with _lock(f"f:{mid}"):
            data = self.store.get_blob(FEEDS, mid) or {"items": []}
            seq = (data["items"][-1]["seq"] if data["items"] else 0) + 1
            now = time.time()
            row = {"content": "", "status": "done", "created_at": now, "updated_at": now, **item, "seq": seq}
            data["items"].append(row)
            self.store.put_blob(FEEDS, mid, data)
            return row

    def update(self, mid: str, seq: int, **changes: Any) -> None:
        with _lock(f"f:{mid}"):
            data = self.store.get_blob(FEEDS, mid) or {"items": []}
            for u in data["items"]:
                if u["seq"] == seq:
                    u.update(changes)
                    u["updated_at"] = time.time()
            self.store.put_blob(FEEDS, mid, data)


class MissionStopped(Exception):
    pass


class MissionRunner:
    def __init__(
        self,
        team_runner: TeamRunner,
        store: MissionStore,
        on_conclusion: Callable[[dict[str, Any], str], None] | None = None,
    ) -> None:
        self.teams = team_runner
        self.store = store
        self.on_conclusion = on_conclusion  # (mission, conclusion)：讨论汇总完成后回调

    # ---------------- 创建 ----------------
    def create(self, payload: dict[str, Any], materials: dict[str, Any]) -> dict[str, Any]:
        mode = payload.get("mode") or "single"
        if mode == "pipeline":
            preset = PIPELINE_PRESETS.get(payload.get("preset") or "full", PIPELINE_PRESETS["full"])
            stages = payload.get("stages") or preset["stages"]
            synthesizer = payload.get("synthesizer_id") or preset["synthesizer_id"]
        else:
            team = self.teams.get_team(payload.get("team_id") or "") or self.teams.default_team()
            stages = [
                {
                    "key": "cycle",
                    "name": f"单周期 · {team.name}",
                    "mode": team.mode,
                    "role_ids": team.role_ids,
                    "gate": False,
                    "challenge_rounds": team.challenge_rounds,
                }
            ]
            synthesizer = team.synthesizer_id or (team.role_ids[-1] if team.role_ids else "")
        stages = [
            {
                "key": s.get("key") or f"s{i}",
                "name": s.get("name") or f"阶段 {i + 1}",
                "mode": s.get("mode") or "sequential",
                "role_ids": [r for r in s.get("role_ids") or [] if self.teams.roles.get(r)],
                "gate": bool(s.get("gate")),
                "status": "pending",
            }
            for i, s in enumerate(stages)
        ]
        stages = [s for s in stages if s["role_ids"]]
        flat = [r for s in stages for r in s["role_ids"]]
        role_ids = list(dict.fromkeys(flat + ([synthesizer] if synthesizer else [])))
        roster = []
        for rid in role_ids:
            role = self.teams.roles.get(rid)
            if role:
                roster.append(
                    {
                        "id": role.id,
                        "name": role.name,
                        "title": role.title,
                        "model": role.model,
                        "effort": role.effort,
                        "avatar": role.avatar,
                        "tags": role.tags,
                    }
                )
        mission = {
            "id": uuid.uuid4().hex[:12],
            "directive": payload["directive"],
            "context": payload.get("context") or "",
            "mode": mode,
            "preset": payload.get("preset"),
            "team_id": payload.get("team_id"),
            "stages": stages,
            "synthesizer_id": synthesizer,
            "roster": roster,
            "symbol": payload.get("symbol") or "BTC/USDT",
            "timeframe": payload.get("timeframe") or "1h",
            "lab_project_id": payload.get("lab_project_id"),
            "materials": materials,
            "cursor": 0,
            "status": "queued",
            "stop_requested": False,
            "conclusion": "",
            "created_at": time.time(),
        }
        self.store.save(mission)
        self.store.post(mission["id"], {"kind": "system", "content": f"指挥官下达指令：{payload['directive']}"})
        return mission

    # ---------------- 执行 ----------------
    def start(self, mid: str, *, wait: bool = False, job: Any = None) -> None:
        m = self.store.get(mid)
        if m is None:
            raise KeyError(mid)
        if m["status"] == "running":
            raise RuntimeError("任务正在进行中")
        self.store.patch(mid, status="running", stop_requested=False)

        def body() -> None:
            try:
                (job or (lambda: self._run_from_cursor(mid)))()
            except MissionStopped:
                self.store.patch(mid, status="stopped")
                self.store.post(mid, {"kind": "system", "content": "指挥官叫停了任务。随时可以继续。"})
            except Exception as exc:
                self.store.patch(mid, status="failed")
                self.store.post(mid, {"kind": "system", "content": f"任务出错：{type(exc).__name__}: {exc}"})
            else:
                cur = self.store.get(mid) or {}
                if cur.get("status") == "running":
                    done = cur.get("cursor", 0) >= len(cur.get("stages") or []) and bool(cur.get("conclusion"))
                    self.store.patch(mid, status="done" if done else "idle")

        if wait:
            body()
        else:
            ctx = contextvars.copy_context()
            threading.Thread(target=ctx.run, args=(body,), daemon=True, name=f"mission-{mid}").start()

    def _check(self, mid: str) -> dict[str, Any]:
        m = self.store.get(mid) or {}
        if m.get("stop_requested"):
            raise MissionStopped()
        return m

    def _run_from_cursor(self, mid: str) -> None:
        m = self._check(mid)
        stages = m["stages"]
        while m["cursor"] < len(stages):
            i = m["cursor"]
            stage = stages[i]
            self._set_stage(mid, i, "running")
            self.store.post(
                mid,
                {
                    "kind": "stage",
                    "stage": i,
                    "content": stage["name"],
                    "data": {"mode": stage["mode"], "roles": stage["role_ids"]},
                },
            )
            self._run_stage(mid, i, stage)
            self._set_stage(mid, i, "done")
            m = self.store.patch(mid, cursor=i + 1) or m
            m = self._check(mid)
            if stage.get("gate") and i + 1 < len(stages):
                self.store.patch(mid, status="awaiting_approval")
                self.store.post(
                    mid,
                    {
                        "kind": "gate",
                        "stage": i,
                        "content": (
                            f"「{stage['name']}」已完成。下一阶段是「{stages[i + 1]['name']}」，等待指挥官批准。"
                        ),
                    },
                )
                return
        self._synthesize(mid)

    def _set_stage(self, mid: str, i: int, status: str) -> None:
        with _lock(f"m:{mid}"):
            m = self.store.get(mid) or {}
            m["stages"][i]["status"] = status
            self.store.save(m)

    def _run_stage(self, mid: str, i: int, stage: dict[str, Any]) -> None:
        roles = [r for r in (self.teams.roles.get(rid) for rid in stage["role_ids"]) if r]
        if stage["mode"] == "parallel":
            self._speak_parallel(mid, i, roles)
        elif stage["mode"] == "debate":
            critics = [r for r in roles if {"质疑", "风控"} & set(r.tags)] or roles[-1:]
            speakers = [r for r in roles if r not in critics] or roles
            self._speak_parallel(mid, i, speakers)
            for _ in range(max(1, int(stage.get("challenge_rounds") or 1))):
                for critic in critics:
                    self._check(mid)
                    targets = [r.name for r in speakers if r is not critic]
                    self._speak(
                        mid,
                        i,
                        critic,
                        kind="challenge",
                        reply_to=targets,
                        instruction=(
                            "请针对以上发言进行质疑：指出最脆弱的假设、构造具体反例、"
                            "指出哪些结论看起来像证据其实不是。直接点名你在回应谁。"
                        ),
                    )
        else:
            for role in roles:
                self._check(mid)
                self._speak(mid, i, role)

    # ---------------- 发言 ----------------
    def _transcript(self, mid: str, *, limit_chars: int = 9000) -> str:
        m = self.store.get(mid) or {}
        parts = [f"【指挥官指令】{m.get('directive')}"]
        if m.get("context"):
            parts.append(f"【背景】{m['context']}")
        if m.get("materials"):
            parts.append("【平台数据】\n" + json.dumps(m["materials"], ensure_ascii=False, default=str)[:4000])
        lines = []
        for u in self.store.feed(mid):
            if u.get("status") != "done" or not u.get("content"):
                continue
            if u["kind"] in ("speech", "challenge", "synthesis"):
                lines.append(f"### {u.get('role_name')}\n{u['content']}")
            elif u["kind"] == "commander":
                target = f"（对 {u['data'].get('to_name')}）" if (u.get("data") or {}).get("to_name") else ""
                lines.append(f"### 指挥官{target}\n{u['content']}")
        convo = "\n\n".join(lines)
        if len(convo) > limit_chars:
            convo = "……（更早的发言已省略）\n" + convo[-limit_chars:]
        if convo:
            parts.append("【讨论记录】\n" + convo)
        return "\n\n".join(parts)

    def _speak(
        self,
        mid: str,
        stage: int,
        role: Role,
        *,
        kind: str = "speech",
        reply_to: list[str] | None = None,
        instruction: str = "",
    ) -> dict[str, Any]:
        placeholder = self.store.post(mid, self._placeholder(stage, role, kind, reply_to))
        task = self._transcript(mid) + (
            f"\n\n【你的任务】{instruction}"
            if instruction
            else "\n\n【你的任务】按你的角色职责发言，回应前面的观点（如有），简洁、有依据。"
        )
        self._finish_speech(mid, placeholder["seq"], role, self.teams._invoke(role, task), kind)
        return placeholder

    def _speak_parallel(self, mid: str, stage: int, roles: list[Role]) -> None:
        if not roles:
            return
        task = (
            self._transcript(mid)
            + "\n\n【你的任务】按你的角色职责独立给出观点（不要等别人），简洁、有依据。"
            + PARALLEL_BRIEF
        )
        holders = {r.id: self.store.post(mid, self._placeholder(stage, r, "speech", None)) for r in roles}
        with ThreadPoolExecutor(max_workers=min(4, len(roles))) as pool:
            futures = {pool.submit(self.teams._invoke, r, task): r for r in roles}
            for fut in as_completed(futures):
                role = futures[fut]
                self._finish_speech(mid, holders[role.id]["seq"], role, fut.result(), "speech")

    @staticmethod
    def _placeholder(stage: int, role: Role, kind: str, reply_to: list[str] | None) -> dict[str, Any]:
        return {
            "kind": kind,
            "stage": stage,
            "role_id": role.id,
            "role_name": role.name,
            "status": "typing",
            "model": role.model or "",
            "data": {"reply_to": reply_to or []},
        }

    def _finish_speech(self, mid: str, seq: int, role: Role, step: Any, kind: str) -> None:
        if step.status == "ok":
            self.store.update(
                mid, seq, status="done", content=step.output, model=step.model, mode="llm", elapsed_ms=step.elapsed_ms
            )
            return
        m = self.store.get(mid) or {}
        text = fallback_speech(role, m, kind, self.store.feed(mid))
        self.store.update(
            mid,
            seq,
            status="done",
            content=text,
            model="规则降级",
            mode="fallback",
            elapsed_ms=step.elapsed_ms,
            error=(step.error or "")[:300],
        )

    def _synthesize(self, mid: str) -> None:
        m = self._check(mid)
        role = self.teams.roles.get(m.get("synthesizer_id") or "")
        if role is None:
            self.store.patch(mid, conclusion="（未配置汇总角色）")
            return
        holder = self.store.post(mid, self._placeholder(len(m["stages"]), role, "synthesis", None))
        task = self._transcript(mid) + (
            "\n\n【你的任务】作为汇总人，整合以上讨论，按证据质量而不是发言人数取舍：\n"
            "1. 已验证的事实（注明出处）；2. 未验证的假设（写清楚怎么验证）；"
            "3. 被剔除的无依据结论（例如没有数据支持的收益 / 回撤预测）；4. 分歧与你的判断；5. 下一步行动项。"
        )
        step = self.teams._invoke(role, task, max_steps=2, use_tools=False)
        self._finish_speech(mid, holder["seq"], role, step, "synthesis")
        final = next((u for u in self.store.feed(mid) if u["seq"] == holder["seq"]), {})
        self.store.patch(mid, conclusion=final.get("content", ""))
        if self.on_conclusion is not None and final.get("content"):
            try:  # 结论自动钉进发起讨论的 AI 研究室对话（对话设置里可关）
                self.on_conclusion(self.store.get(mid) or m, final["content"])
            except Exception:
                pass

    # ---------------- 指挥官操作 ----------------
    def say(self, mid: str, text: str, *, to_role: str | None = None) -> dict[str, Any]:
        role = self.teams.roles.get(to_role) if to_role else None
        return self.store.post(
            mid,
            {
                "kind": "commander",
                "content": text,
                "role_name": "指挥官",
                "data": {"to": to_role, "to_name": role.name if role else None},
            },
        )

    def answer(self, mid: str, role_id: str) -> None:
        """被点名的角色回应指挥官的最新追问。"""
        role = self.teams.roles.get(role_id)
        if role is None:
            return
        m = self.store.get(mid) or {}
        stage = min(m.get("cursor", 0), max(0, len(m.get("stages") or []) - 1))
        self._speak(mid, stage, role, instruction="指挥官刚刚点名问你，请直接、具体地回答指挥官的问题。")

    def approve(self, mid: str) -> None:
        self.store.post(mid, {"kind": "system", "content": "指挥官批准，进入下一阶段。"})
        self._run_from_cursor(mid)


# ---------------------------------------------------------------------------
# 规则化发言（无模型时）
# ---------------------------------------------------------------------------
def fallback_speech(role: Role, mission: dict[str, Any], kind: str, feed: list[dict[str, Any]]) -> str:
    mat = mission.get("materials") or {}
    mk = mat.get("market") or {}
    bt = (mat.get("backtest") or {}).get("metrics") or (mat.get("lab") or {}).get("metrics") or {}
    ret = mk.get("period_return")
    vol = mk.get("volatility_per_bar")
    trend = ret is not None and abs(ret) > 0.08
    phase = ("上行趋势" if (ret or 0) > 0 else "下行趋势") if trend else "区间震荡"
    src = mk.get("source", "未知来源")
    tag = "【规则降级】"

    def pct(v: Any) -> str:
        return "—" if v is None else f"{v * 100:.1f}%"

    rid = role.id
    if rid == "market_analyst":
        if not mk:
            return f"{tag}没有附带行情数据，无法判断市场阶段。建议在指令里勾选「附带市场概览」。"
        return (
            f"{tag}{mk.get('symbol')} {mk.get('timeframe')}（{src}，{mk.get('bars')} 根）区间涨跌 {pct(ret)}，"
            f"单根波动 {pct(vol)}，高点 {mk.get('high'):.2f} / 低点 {mk.get('low'):.2f}。"
            f"判断更接近 **{phase}**。{'趋势跟随类策略更适配' if trend else '均值回归、网格类策略更适配'}；"
            "[推测] 阶段可能随时切换，需要结合更长周期确认。"
        )
    if rid == "strategy_designer":
        idea = "双均线或唐奇安通道突破，配合 ATR 止损" if trend else "RSI 均值回归或有限仓位网格，配合仓位上限"
        return (
            f"{tag}基于「{phase}」的判断，建议方案：{idea}。参数先取常用值（快线 10~20、慢线 40~60 或 RSI 14），"
            "上线前必须做样本外检验。"
        )
    if rid == "quant_engineer":
        return (
            f"{tag}实现要点：可调参数集中在 PARAMS；history 取数前判断长度防止预热期越界；"
            "止损按持仓成本计算；仓位用 order_target_percent 表达目标，避免重复下单。"
        )
    if rid == "backtest_analyst":
        if not bt:
            return f"{tag}本次没有附带回测数据，任何关于收益的结论都没有证据支撑。建议附带一个策略的回测再评审。"
        dd = pct(bt.get("max_drawdown"))
        n_trades = bt.get("trades") or 0
        return (
            f"{tag}回测：总收益 {pct(bt.get('total_return'))}，超额 {pct(bt.get('excess_return'))}，"
            f"夏普 {bt.get('sharpe') or 0:.2f}，最大回撤 {dd}，交易 {n_trades} 笔。"
            f"{'交易笔数偏少，统计意义有限。' if n_trades < 20 else ''}"
        )
    if rid == "risk_officer":
        if kind == "challenge":
            return (
                f"{tag}回应各位：方案里还没有看到单笔止损与总仓位上限的具体数值；"
                "建议最大回撤红线 20%、单币仓位 ≤ 70%，并用 2 倍成本做压力测试后再讨论收益。"
            )
        return f"{tag}风控底线：最大回撤 ≤ 20%，仓位 ≤ 70%，必须有止损；通过 2 倍成本压力测试才放行。"
    if rid == "devil_advocate":
        speakers = [u.get("role_name") for u in feed if u["kind"] == "speech" and u.get("status") == "done"]
        who = "、".join(dict.fromkeys(s for s in speakers if s)) or "前面的发言"
        return (
            f"{tag}我来唱反调，回应 {who}：①样本区间有多长？只覆盖一种行情的结论不可外推；"
            f"②{'数据来自合成行情，' if 'synthetic' in str(src) else ''}没有样本外结果就谈参数是在拟合噪声；"
            "③如果把手续费翻倍，结论还成立吗？"
        )
    if rid == "execution_specialist":
        return (
            f"{tag}执行视角：按 taker 0.1% + 5bp 滑点估算，单次往返成本约 0.3%；"
            f"{'波动较低时高频交易会被成本吃掉。' if (vol or 0) < 0.005 else '当前波动下成本占比可接受，'}"
            "保守/基准/乐观三档成本都应复测。"
        )
    if rid == "compliance_officer":
        return f"{tag}表述审查：避免“稳赚”“保证收益”等措辞；所有结论需注明“仅为研究结果，非投资建议”。"
    if rid == "research_lead" or kind == "synthesis":
        heads = [
            f"- **{u.get('role_name')}**：{(u.get('content') or '').replace(tag, '')[:60]}…"
            for u in feed
            if u["kind"] in ("speech", "challenge") and u.get("status") == "done"
        ]
        return (
            f"{tag}**汇总**\n\n"
            + "\n".join(heads[-8:])
            + f"\n\n**结论**：市场更接近{phase}，可以按设计方案写出策略并回测验证。"
            "\n\n**下一步**：①交给策略室生成代码并回测 ②做样本外与 2 倍成本压力测试 ③通过后进入实时模拟观察。"
            "\n\n仅为研究结果，非投资建议。"
        )
    return f"{tag}{role.name}：已阅读讨论记录。没有模型时无法给出深入分析，请在「AI 设置 → AI 模型」中配置模型后重试。"
