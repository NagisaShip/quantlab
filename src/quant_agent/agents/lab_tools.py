"""StrategyLab 的模型工具构建器。

对话工具、跨对话工具、共享搜索工具和斜杠工具是 AI 研究室里最大的几块声明式代码。
把它们从 ``strategy_lab.py`` 拆出来后，主类只保留状态机与核心流程；工具构建函数通过
``lab`` 参数访问同一个研究室实例，因此权限、审计、停止事件和消息写入行为完全不变。
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..llm.base import Tool
from ..timeframes import is_timeframe
from . import lab_goal as G
from .strategy_lab import (
    PLAN_STATUSES,
    SHARED_TOOLS,
    _call_market,
    _history_metrics,
    _latest_eval,
    _latest_metrics,
    _range_text,
    _step_text,
    goal_line,
    plan_text,
    project_has_goal,
)


def chat_tools(lab, pid: str) -> dict[str, Tool]:
    def market(symbol: str = "", timeframe: str = "") -> Any:
        p = lab.store.get(pid) or {}
        if not symbol and not p.get("securities"):
            return {"ok": False, "error": "尚未设置标的，请先调用 set_market"}
        sym = symbol or p["securities"][0]
        tf = timeframe or p["timeframe"]
        n = int(p.get("bars_limit") or 1500)  # 与回测同一个窗口
        return lab._tool(
            pid,
            "get_market_summary",
            "查看行情摘要",
            {"标的": sym, "周期": tf, "K 线": n},
            lambda: _call_market(lab.tools.market, sym, tf, n),
        )

    def templates(query: str = "") -> Any:
        return lab._tool(
            pid,
            "search_templates",
            "搜索策略模板",
            {"关键词": query or "全部"},
            lambda: [{k: t.get(k) for k in ("name", "label", "description")} for t in lab.tools.templates(query)][:8],
        )

    def write(code: str = "", note: str = "", hypothesis: str = "", change_type: str = "", expected: str = "") -> Any:
        current = lab.store.get(pid) or {}
        if current.get("market_auto"):
            return {
                "ok": False,
                "error": "标的尚未设置（当前为自动）。请先调用 set_market 选择标的与周期，再提交代码。",
            }
        experiment = {
            k: v.strip()[:300]
            for k, v in {"hypothesis": hypothesis, "change_type": change_type, "expected": expected}.items()
            if isinstance(v, str) and v.strip()
        }
        out = lab._submit_code(pid, code, note=note or "模型提交", model="", experiment=experiment)
        p = lab.store.get(pid) or {}
        if out.get("ok") and int(p.get("current_v") or 0) > 1 and not experiment.get("hypothesis"):
            out["reminder"] = (
                "这一版没有写 hypothesis / change_type / expected：按研究纪律补充说明，并确认只改了一类变量"
            )
        return out

    def backtest(
        cost_scale: float = 1.0,
        execution_mode: str = "bar",
        dynamic_slippage: bool = False,
        vol_slippage: float = 0.25,
        impact_a_bps: float = 0.0,
        impact_b_bps: float = 0.0,
        limit_move_pct: float = 0.0,
        min_liquidity_usd: float = 0.0,
    ) -> Any:
        current = lab.store.get(pid) or {}
        if current.get("market_auto"):
            return {
                "ok": False,
                "error": "标的尚未设置（当前为自动）。请先调用 set_market 选择标的与周期，再运行回测。",
            }
        out = lab.backtest_current(
            pid,
            cost_scale=float(cost_scale or 1.0),
            execution_mode="tick" if execution_mode == "tick" else "bar",
            dynamic_slippage=bool(dynamic_slippage),
            vol_slippage=float(vol_slippage or 0.0),
            impact_a_bps=float(impact_a_bps or 0.0),
            impact_b_bps=float(impact_b_bps or 0.0),
            limit_move_pct=float(limit_move_pct or 0.0),
            min_liquidity_usd=float(min_liquidity_usd or 0.0),
        )
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error")}
        return {
            "ok": True,
            "metrics": out.get("metrics"),
            "evaluation": out.get("evaluation"),
            "data_check": out.get("data_check"),
            "definitions": out.get("definitions"),
            "consistency": out.get("consistency"),
            "zero_trade_diagnosis": out.get("zero_trade_diagnosis"),
            "experiment": out.get("experiment"),
            "warnings": out.get("warnings"),
            "errors": out.get("errors"),
        }

    def versions() -> Any:
        p = lab.store.get(pid) or {}
        return [
            {
                "v": r["v"],
                "note": r["note"],
                "source": r["source"],
                "metrics": {
                    k: (r.get("metrics") or {}).get(k)
                    for k in ("sharpe", "max_drawdown", "trades", "total_return", "excess_return")
                },
                "passed": (r.get("evaluation") or {}).get("passed"),
            }
            for r in p.get("versions") or []
        ]

    def set_goal(**kw: Any) -> Any:
        out = lab.set_goal(pid, kw, source="model")
        if out.get("ok") is False:
            return out
        return {
            "ok": True,
            "goal": out["spec"],
            "check": {k: out["check"][k] for k in ("verdict", "issues", "summary")},
        }

    def check_goals() -> Any:
        p = lab.store.get(pid) or {}
        items = G.project_items(p)
        ev = _latest_eval(p)
        spec = G.project_spec(p)
        return {
            "goal": G.items_text(items) if project_has_goal(p) else goal_line(p),
            "goal_set": project_has_goal(p),
            "range": _range_text(spec),
            "latest": ev and {"passed": ev["passed"], "items": ev["items"], "gaps": G.gap_report(ev)},
            "reasonableness": G.check_spec(spec, history=_history_metrics(p)),
        }

    def walk_forward(segments: int = 3, mode: str = "sliced", attribution: str = "close") -> Any:
        return lab.walk_forward(pid, segments=int(segments or 3), mode=mode, attribution=attribution)

    def stability(windows: int = 6, sensitivity: bool = True) -> Any:
        return lab.stability(pid, windows=int(windows or 6), sensitivity=bool(sensitivity))

    def stress_test(
        segments: int = 3, windows: int = 4, cost_scales: list[float] | None = None, sensitivity: bool = True
    ) -> Any:
        return lab.stress_test(
            pid,
            segments=int(segments or 3),
            windows=int(windows or 4),
            cost_scales=cost_scales,
            sensitivity=bool(sensitivity),
        )

    def parameter_heatmap(
        x_param: str = "",
        y_param: str = "",
        x_values: list[float] | None = None,
        y_values: list[float] | None = None,
        metric: str = "sharpe",
    ) -> Any:
        return lab.parameter_heatmap(
            pid,
            x_param=x_param,
            y_param=y_param,
            x_values=x_values,
            y_values=y_values,
            metric=metric,
        )

    def team(topic: str = "", team_id: str = "") -> Any:
        return lab._convene(pid, topic or "评审当前策略", team_id=team_id or None)

    def ask(question: str = "", options: list[str] | None = None) -> Any:
        lab._ask(pid, question or "你对当前结果满意吗？", options)
        return {"ok": True, "note": "已向研究者提问，本轮结束"}

    obj = {"type": "object"}
    shared = lab._shared_tools(pid)
    return {
        **shared,
        "get_market_summary": Tool(
            "get_market_summary",
            "查看标的行情摘要（价格、波动、区间、数据来源）",
            {**obj, "properties": {"symbol": {"type": "string"}, "timeframe": {"type": "string"}}},
            market,
        ),
        "search_templates": Tool(
            "search_templates",
            "按关键词搜索平台内置策略模板",
            {**obj, "properties": {"query": {"type": "string"}}},
            templates,
        ),
        "write_strategy": Tool(
            "write_strategy",
            "提交一版完整的平台 SDK 策略代码，平台会做沙箱检查与试跑",
            {
                **obj,
                "properties": {
                    "code": {"type": "string", "description": "完整 Python 代码"},
                    "note": {"type": "string", "description": "这一版改了什么（代码差异的一句话摘要）"},
                    "hypothesis": {"type": "string", "description": "修改假设：为什么这样改会更好"},
                    "change_type": {
                        "type": "string",
                        "enum": ["initial", "entry", "exit", "filter", "risk", "params", "other"],
                        "description": "这一版只改了哪一类变量（一次只改一类）",
                    },
                    "expected": {"type": "string", "description": "预期影响，例如“交易数减少约一半、回撤下降”"},
                },
                "required": ["code"],
            },
            write,
        ),
        "run_backtest": Tool(
            "run_backtest",
            "用平台真实回测跑当前代码；cost_scale=2 表示 2 倍成本压力测试。"
            "execution_mode=tick 用 OHLC 路径逐笔撮合；dynamic_slippage / impact_a_bps / impact_b_bps "
            "模拟动态滑点与冲击成本；min_liquidity_usd / limit_move_pct 模拟流动性枯竭与涨跌停。"
            "返回指标、data_check（数据核验，formal=false 为非正式回测）、definitions（指标口径）、"
            "consistency（指标一致性）、zero_trade_diagnosis（零交易诊断）",
            {
                **obj,
                "properties": {
                    "cost_scale": {"type": "number"},
                    "execution_mode": {"type": "string", "enum": ["bar", "tick"]},
                    "dynamic_slippage": {"type": "boolean"},
                    "vol_slippage": {"type": "number", "minimum": 0, "maximum": 10},
                    "impact_a_bps": {"type": "number", "minimum": 0, "maximum": 500},
                    "impact_b_bps": {"type": "number", "minimum": 0, "maximum": 2000},
                    "limit_move_pct": {"type": "number", "minimum": 0, "maximum": 1},
                    "min_liquidity_usd": {"type": "number", "minimum": 0},
                },
            },
            backtest,
        ),
        "cost_sweep": Tool(
            "cost_sweep",
            "成本压力扫描（等同 /sweep）：并行跑多档成本倍数（手续费、价差、滑点一起放大）的回测，"
            "找出策略不再满足目标（没设目标时为总收益转负）的成本极限，并核对各档手续费是否按倍数放大。"
            "scales 不填默认 1、1.5、2、3、4、6 倍；找到区间后会自动在区间内再细分。只展示，不改版本成绩",
            {
                **obj,
                "properties": {
                    "scales": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "成本倍数列表，0.5–20，最多 8 个",
                    }
                },
            },
            lambda scales=None: lab.cost_sweep(pid, scales),
        ),
        "list_versions": Tool("list_versions", "列出本项目历次版本及其回测成绩", {**obj, "properties": {}}, versions),
        "set_goal": Tool(
            "set_goal",
            "设定或修改本项目的目标：指标条件（must 必达 / try 尽量）与回测范围。"
            "比例用小数（回撤 0.2 = 20%）；只传要改的字段。设定后平台会做合理性检查并返回建议",
            {
                **obj,
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "metric": {"type": "string", "enum": list(G.METRICS)},
                                "op": {"type": "string", "enum": [">=", "<="]},
                                "target": {"type": "number"},
                                "priority": {"type": "string", "enum": ["must", "try"]},
                            },
                            "required": ["metric", "target"],
                        },
                    },
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "timeframe": {"type": "string", "enum": list(G.TIMEFRAMES)},
                    "start": {"type": "string", "description": "YYYY-MM-DD，可空"},
                    "end": {"type": "string", "description": "YYYY-MM-DD，可空"},
                    "max_rounds": {"type": "integer"},
                },
            },
            set_goal,
        ),
        "check_goals": Tool(
            "check_goals",
            "查看当前目标、最近一次回测与每个条件的差距，以及目标是否合理",
            {**obj, "properties": {}},
            check_goals,
        ),
        "walk_forward": Tool(
            "walk_forward",
            "分段回测：把回测区间切成几段，检查策略是否只在某一段有效。返回 semantics（是否连续复利、是否重置资金、"
            "跨段持仓、交易归段方式、能否复原全区间收益）、reconcile（复原核对）与每段的 data_role（是否已参与开发）",
            {
                **obj,
                "properties": {
                    "segments": {"type": "integer", "minimum": 2, "maximum": 6},
                    "mode": {
                        "type": "string",
                        "enum": ["sliced", "independent"],
                        "description": "sliced 连续切片（默认）；independent 每段重置资金单独回测",
                    },
                    "attribution": {
                        "type": "string",
                        "enum": ["close", "open"],
                        "description": "完整交易按平仓（默认）还是开仓时间归段，只对 sliced 生效",
                    },
                },
            },
            walk_forward,
        ),
        "stability_report": Tool(
            "stability_report",
            "稳定性报告：滚动窗口收益 / 夏普 / 超额、收益分布（偏度、峰度、分位数、极端收益占比）、基准对照，"
            "以及参数上下挪 20% 的敏感性（并行回测）。下结论或保存前做一次",
            {
                **obj,
                "properties": {
                    "windows": {"type": "integer", "minimum": 3, "maximum": 12},
                    "sensitivity": {"type": "boolean", "description": "是否做参数敏感性（多跑若干次回测）"},
                },
            },
            stability,
        ),
        "stress_test": Tool(
            "stress_test",
            "一键过拟合压力测试：自动串联滚动分段回测、滚动稳定性与参数敏感性、2×/3× 成本压力，"
            "返回逐项 checks 与 pass / warn / fail 结论；调用后对话流会先显示进行中卡片，完成后原地更新。"
            "研究者问「会不会过拟合」「能不能上线」「帮我做压力测试」时优先用它",
            {
                **obj,
                "properties": {
                    "segments": {"type": "integer", "minimum": 2, "maximum": 6},
                    "windows": {"type": "integer", "minimum": 3, "maximum": 12},
                    "cost_scales": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "成本倍数，默认 1 / 2 / 3；最多 8 个",
                    },
                    "sensitivity": {"type": "boolean", "description": "是否做参数上下挪 20% 的敏感性检查"},
                },
            },
            stress_test,
        ),
        "parameter_heatmap": Tool(
            "parameter_heatmap",
            "二维参数敏感度热力图：并行跑 X × Y 参数网格（默认围绕当前值 5×5），返回 Sharpe / 收益 / 回撤等指标的"
            "参数平原、最佳点与稳健性判断。调用后对话流会先显示进行中卡片，完成后原地更新为热力图。"
            "用于回答「参数怎么配」「是不是孤岛尖峰 / 过拟合」",
            {
                **obj,
                "properties": {
                    "x_param": {"type": "string", "description": "X 轴参数名（PARAMS 里的数值键，如 fast）"},
                    "y_param": {"type": "string", "description": "Y 轴参数名（如 slow）"},
                    "x_values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "X 候选值，2-7 个；省略则围绕当前值生成",
                    },
                    "y_values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "Y 候选值，2-7 个；省略则围绕当前值生成",
                    },
                    "metric": {
                        "type": "string",
                        "enum": [
                            "sharpe",
                            "total_return",
                            "annualized_return",
                            "excess_return",
                            "max_drawdown",
                            "calmar",
                            "sortino",
                            "win_rate",
                            "profit_factor",
                        ],
                    },
                },
            },
            parameter_heatmap,
        ),
        "convene_team": Tool(
            "convene_team",
            "召集 Agent 团队对当前策略开评审会，返回团队结论",
            {**obj, "properties": {"topic": {"type": "string"}, "team_id": {"type": "string"}}},
            team,
        ),
        "ask_user": Tool(
            "ask_user",
            "向研究者提问并结束本轮，options 为可点选的简短选项",
            {
                **obj,
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
            ask,
        ),
    }


def conversation_tools(lab, pid: str, obj: dict[str, Any]) -> dict[str, Tool]:
    conv = lab.tools.conversations

    def visible(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 只看研究者有权看到的对话（协作空间里别人的私有对话不可见）
        return conv["visible"](rows) if conv and conv.get("visible") else rows

    def find(cid: str) -> dict[str, Any] | None:
        p = lab.store.get(cid)
        return p if p is not None and visible([p]) else None

    def brief(p: dict[str, Any]) -> dict[str, Any]:
        last = next(
            (m for m in reversed(lab.store.messages(p["id"])) if m.get("role") == "assistant" and m.get("content")),
            None,
        )
        return {
            "id": p["id"],
            "title": p.get("title"),
            "kind": p.get("kind") or "strategy",
            "status": p.get("status"),
            "folder": p.get("folder") or "",
            "securities": p.get("securities"),
            "timeframe": p.get("timeframe"),
            "versions": len(p.get("versions") or []),
            "updated_at": p.get("updated_at"),
            "current": p["id"] == pid,
            "last_reply": " ".join((last or {}).get("content", "").split())[:200],
        }

    def list_conversations(query: str = "", kind: str = "", limit: int = 20, search_messages: bool = False) -> Any:
        q = (query or "").strip().lower()
        rows = [
            p
            for p in visible(lab.store.list())
            if not p.get("archived") and (not kind or (p.get("kind") or "strategy") == kind)
        ]
        hits = []
        for p in rows:
            text = f"{p.get('title') or ''} {p.get('idea') or ''} {p.get('folder') or ''}".lower()
            matched = not q or q in text
            snippet = ""
            if q and not matched and search_messages:
                for m in lab.store.messages(p["id"]):
                    c = str(m.get("content") or "")
                    i = c.lower().find(q)
                    if i >= 0:
                        matched, snippet = True, c[max(0, i - 60) : i + 120]
                        break
            if matched:
                hits.append({**brief(p), **({"match": snippet} if snippet else {})})
        hits.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
        return {"total": len(hits), "conversations": hits[: max(1, min(int(limit or 20), 50))]}

    def read_conversation(conversation_id: str, last_n: int = 20) -> Any:
        p = find(conversation_id)
        if p is None:
            return {"ok": False, "error": f"找不到对话 {conversation_id}，先用 list_conversations 查找"}
        msgs = [
            {
                "seq": m.get("seq"),
                "role": m.get("role"),
                "kind": m.get("kind"),
                "content": str(m.get("content") or "")[:1200],
            }
            for m in lab.store.messages(conversation_id)
            if m.get("kind") in ("text", "ask", "team", "backtest", "round", "error") and m.get("content")
        ][-max(1, min(int(last_n or 20), 60)) :]
        return {
            "ok": True,
            **brief(p),
            "idea": (p.get("idea") or "")[:600],
            "goal": goal_line(p),
            "latest_metrics": _latest_metrics(p),
            "messages": msgs,
            "link": f"#/lab/{conversation_id}",
        }

    def send_to_conversation(conversation_id: str, message: str, wait_seconds: int = 0) -> Any:
        if conv is None:
            return {"ok": False, "error": "当前环境不能调起其他对话"}
        if conversation_id == pid:
            return {"ok": False, "error": "不能给自己发消息"}
        target = find(conversation_id)
        if target is None:
            return {"ok": False, "error": f"找不到对话 {conversation_id}"}
        if target.get("status") == "running":
            return {"ok": False, "error": "那个对话正在运行，稍后再试，或先 read_conversation 看它在做什么"}
        hops = lab._relay_hops(pid) + 1
        if hops > lab.MAX_RELAY_HOPS:
            return {
                "ok": False,
                "error": f"对话之间已经转发了 {hops - 1} 次，为避免来回喊话不再继续转发；请在本对话里直接回答",
            }
        here = lab.store.get(pid) or {}
        text = (message or "").strip()[:4000]
        if not text:
            return {"ok": False, "error": "message 不能为空"}
        lab.store.append(
            conversation_id,
            {
                "role": "user",
                "kind": "text",
                "content": f"【来自对话「{here.get('title') or pid}」的 Agent】{text}",
                "data": {"from_conversation": pid, "from_title": here.get("title"), "relay_hops": hops},
            },
        )
        conv["start"](conversation_id, text)
        lab._post(
            pid,
            role="event",
            kind="text",
            content=f"已把消息发到对话「{target.get('title')}」，它在后台开始处理",
            data={"link": f"lab/{conversation_id}"},
        )
        out: dict[str, Any] = {"ok": True, "conversation_id": conversation_id, "link": f"#/lab/{conversation_id}"}
        deadline = time.time() + max(0, min(int(wait_seconds or 0), 180))
        if deadline > time.time():
            seq0 = max((m.get("seq") or 0) for m in lab.store.messages(conversation_id))
            time.sleep(1.0)
            while time.time() < deadline and (lab.store.get(conversation_id) or {}).get("status") == "running":
                lab._check_stop(pid)
                time.sleep(1.5)
            reply = [
                m
                for m in lab.store.messages(conversation_id)
                if (m.get("seq") or 0) > seq0 and m.get("role") == "assistant" and m.get("content")
            ]
            out["finished"] = (lab.store.get(conversation_id) or {}).get("status") != "running"
            out["reply"] = reply[-1]["content"][:3000] if reply else ""
        else:
            out["note"] = "对方在后台处理；需要结果时用 read_conversation 查看，或 wait_seconds>0 等它回复"
        return out

    def create_conversation(idea: str, kind: str = "strategy", message: str = "", workflow: str = "") -> Any:
        if conv is None:
            return {"ok": False, "error": "当前环境不能新建对话"}
        here = lab.store.get(pid) or {}
        p = conv["create"](
            {
                "kind": kind if kind in ("strategy", "factor", "model", "portfolio", "execution") else "strategy",
                "idea": (idea or "").strip()[:600],
                "workflow": workflow if workflow in ("", "auto", "pipeline", "report") else "",
                "folder": here.get("folder") or "",
                "market_auto": True,
                "model": here.get("model") or "",
            }
        )
        cid = p["id"]
        first = (message or "").strip()
        lab._post(
            pid, role="event", kind="text", content=f"已新建对话「{p.get('title')}」", data={"link": f"lab/{cid}"}
        )
        if first:
            return {**send_to_conversation(cid, first), "created": True}
        return {"ok": True, "conversation_id": cid, "title": p.get("title"), "link": f"#/lab/{cid}"}

    return {
        "list_conversations": Tool(
            "list_conversations",
            "查找研究者的其他对话（AI 研究室里的全部对话）：按关键词匹配标题 / 想法 / 文件夹，"
            "search_messages=true 时连消息正文一起搜；返回 id、类型、状态、版本数、最后一条回复摘要",
            {
                **obj,
                "properties": {
                    "query": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["", "strategy", "factor", "model", "portfolio", "execution"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "search_messages": {"type": "boolean"},
                },
            },
            list_conversations,
        ),
        "read_conversation": Tool(
            "read_conversation",
            "阅读另一个对话：项目信息、目标、最近成绩与最近 last_n 条消息。引用其中的结论时注明来自哪个对话",
            {
                **obj,
                "properties": {
                    "conversation_id": {"type": "string"},
                    "last_n": {"type": "integer", "minimum": 1, "maximum": 60},
                },
                "required": ["conversation_id"],
            },
            read_conversation,
        ),
        "send_to_conversation": Tool(
            "send_to_conversation",
            "调起另一个对话：把一条消息发过去，让那个对话的 Agent 在后台处理（例如请因子对话验证一个想法、"
            "让另一个策略对话用同一数据复现结果）。wait_seconds>0（≤180）时等它回复并带回结果",
            {
                **obj,
                "properties": {
                    "conversation_id": {"type": "string"},
                    "message": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 180},
                },
                "required": ["conversation_id", "message"],
            },
            send_to_conversation,
        ),
        "create_conversation": Tool(
            "create_conversation",
            "新建一个对话（可选研究类型），可附第一条消息让它立即开始；适合把一个子问题拆出去单独研究",
            {
                **obj,
                "properties": {
                    "idea": {"type": "string"},
                    "kind": {"type": "string", "enum": ["strategy", "factor", "model", "portfolio", "execution"]},
                    "message": {"type": "string"},
                    "workflow": {"type": "string", "enum": ["", "auto", "pipeline", "report"]},
                },
                "required": ["idea"],
            },
            create_conversation,
        ),
    }


def shared_tools(lab, pid: str) -> dict[str, Tool]:
    reg = lab.tools.registry
    if reg is None:
        return {}
    out: dict[str, Tool] = {}
    for name in SHARED_TOOLS:
        spec = reg.spec(name)
        if spec is None or not spec.get("parameters"):
            continue

        def handler(_name: str = name, _spec: dict = spec, **kw: Any) -> Any:
            shown = {k: str(v)[:60] for k, v in kw.items() if v not in (None, "")}
            return lab._tool(pid, _name, _spec.get("label") or _name, shown, lambda: _spec["handler"](**kw))

        out[name] = Tool(name, spec["description"], spec["parameters"], handler)
    return out


def slash_tools(lab, pid: str) -> dict[str, Tool]:
    """斜杠命令对应的工具：模型可以根据对话主动调用（受权限模式约束）。"""
    obj = {"type": "object"}

    def autopilot(rounds: int = 0) -> Any:
        project = lab.store.get(pid) or {}
        if not lab.has_goal(project):
            return {"ok": False, "error": "还没有设定目标，不能自动迭代；先用 set_goal 设目标，或继续正常对话"}
        lab.autopilot(pid, rounds=int(rounds) or None, force=True)
        p = lab.store.get(pid) or {}
        return {"ok": True, "current_v": p.get("current_v"), "status": p.get("status")}

    def pin_note(title: str = "", text: str = "") -> Any:
        if not text.strip():
            return {"ok": False, "error": "text 不能为空"}
        item = lab.pin(pid, kind="note", title=(title or text)[:40], text=text)
        return {"ok": True, "pin_id": item["id"]}

    def compact_history() -> Any:
        return lab.compact(pid)

    def set_market(symbols: list[str] | str = "", timeframe: str = "") -> Any:
        if isinstance(symbols, str):
            symbols = [x for x in re.split(r"[,，\s]+", symbols) if x]
        syms = [s.strip().upper() for s in symbols or [] if s.strip()][:20]
        tf = timeframe if is_timeframe(timeframe) else ""
        if not syms and not tf:
            return {"ok": False, "error": "至少给出标的或周期"}
        project = lab.store.get(pid) or {}
        changes: dict[str, Any] = {"market_auto": False}
        if not project.get("market_auto"):
            from ..research.contracts import guard_scope, latest_user_text

            requested = {}
            if syms:
                requested["symbols"] = syms
            if tf:
                requested["timeframe"] = tf
            if reason := guard_scope(project, requested, latest_user_text(lab, pid)):
                return {"ok": False, "error": reason}
        if syms:
            changes["securities"] = syms
        if tf:
            changes["timeframe"] = tf
        lab.store.patch(pid, **changes)
        p = lab.store.get(pid) or project
        lab._post(
            pid, role="event", kind="text", content=f"AI 把标的设为 {', '.join(p['securities'])} · {p['timeframe']}"
        )
        return {"ok": True, "securities": p["securities"], "timeframe": p["timeframe"]}

    def set_plan(steps: list[Any] | str = "", task: str = "", done_when: str = "", execute: bool = True) -> Any:
        if isinstance(steps, str):
            steps = [x for x in re.split(r"\n+", steps) if x.strip()]
        items = [re.sub(r"^\s*(?:\d+[.、)]|[-*•])\s*", "", _step_text(x)).strip()[:200] for x in steps or []]
        items = [x for x in items if x][:12]
        if not items:
            return {"ok": False, "error": "steps 不能为空：给出 3-8 个可验证的步骤"}
        plan = lab.set_plan(pid, items, task=task, done_when=done_when, execute=bool(execute))
        return {"ok": True, "steps": len(plan["steps"]), "plan": plan_text(plan)}

    def update_plan_step(
        index: int = 0, status: str = "done", note: str = "", add_steps: list[str] | None = None
    ) -> Any:
        return lab.update_plan_step(pid, int(index or 0), status=status, note=note, add_steps=add_steps or [])

    def start_discussion(topic: str = "") -> Any:
        if lab.tools.discuss is None:
            return {"ok": False, "error": "当前环境不能发起团队讨论"}
        project = lab.store.get(pid) or {}
        topic = (topic or f"评审「{project.get('title')}」：逻辑是否自洽、证据是否可信、最大风险在哪里、下一步怎么做")[
            :600
        ]
        out = lab.tools.discuss(project, topic)
        lab._post(pid, role="event", kind="discussion", content=topic, data={"mission_id": out["mission_id"]})
        return {
            "ok": True,
            "mission_id": out["mission_id"],
            "note": "讨论在后台进行，发言会显示在对话里；用 discussion_status(mission_id, wait_seconds) 查看进度或等结论",
        }

    def read_pinned_file(pin_id: str, offset: int = 0, length: int = 6000) -> Any:
        pin = next((p for p in (lab.store.get(pid) or {}).get("pins") or [] if p.get("id") == pin_id), None)
        if pin is None:
            return {"ok": False, "error": f"没有钉住材料 {pin_id}"}
        text = pin.get("text") or ""
        start = max(0, int(offset or 0))
        size = max(500, min(int(length or 6000), 8000))
        chunk = text[start : start + size]
        return {
            "ok": True,
            "title": pin.get("title"),
            "total_chars": len(text),
            "offset": start,
            "text": chunk,
            "next_offset": start + len(chunk) if start + len(chunk) < len(text) else None,
        }

    def discussion_status(mission_id: str = "", wait_seconds: int = 0) -> Any:
        if lab.tools.discussion_status is None:
            return {"ok": False, "error": "当前环境不能查询团队讨论"}
        if not mission_id:  # 没给 id 就查本对话最近一次发起的讨论
            ev = next(
                (
                    m
                    for m in reversed(lab.store.messages(pid))
                    if m.get("kind") == "discussion" and (m.get("data") or {}).get("mission_id")
                ),
                None,
            )
            if ev is None:
                return {"ok": False, "error": "本对话还没有发起过团队讨论"}
            mission_id = ev["data"]["mission_id"]
        lab._check_stop(pid)
        return lab.tools.discussion_status(mission_id, max(0, min(120, int(wait_seconds or 0))))

    def queue_night(idea: str = "") -> Any:
        if lab.tools.night is None:
            return {"ok": False, "error": "当前环境没有定时任务队列"}
        project = lab.store.get(pid) or {}
        text = (idea or project.get("idea") or project.get("title") or "").strip()
        if len(text) < 2:
            return {"ok": False, "error": "idea 不能为空"}
        out = lab.tools.night(project, text[:600])
        lab._post(
            pid, role="event", kind="text", content=f"已加入每日定时任务：{text[:60]}（按计划执行，结果写进晨报）"
        )
        return {"ok": True, **out}

    def context_usage() -> Any:
        v = lab.context_view(lab.store.get(pid) or {})
        return {
            "used": v.get("total"),
            "budget": v.get("budget"),
            "percent": round(100 * (v.get("total") or 0) / max(1, v.get("budget") or 1), 1),
            "pins": len(v.get("pins") or []),
        }

    plan_tools = {
        "set_plan": Tool(
            "set_plan",
            "列出 / 重新列出分步计划（等同 /plan）：多步任务先列 3-8 个可验证的步骤；"
            "execute=true 时列完就一步步执行，"
            "直到全部完成且目标达标才停",
            {
                **obj,
                "properties": {
                    "task": {"type": "string", "description": "这个计划要完成的任务，一句话"},
                    "steps": {"type": "array", "items": {"type": "string"}, "description": "按顺序的步骤"},
                    "done_when": {"type": "string", "description": "完成标准，例如“夏普 ≥ 1.5 且回撤 ≤ 15%”"},
                    "execute": {"type": "boolean", "description": "列完后是否立即连续执行（默认 true）"},
                },
                "required": ["steps"],
            },
            set_plan,
        ),
        "update_plan_step": Tool(
            "update_plan_step",
            "更新计划进度：index 从 1 开始；status 为 doing / done / skipped / failed；note 写结果要点；"
            "add_steps 在末尾追加新步骤（例如步骤做完了但目标没达标）",
            {
                **obj,
                "properties": {
                    "index": {"type": "integer"},
                    "status": {"type": "string", "enum": list(PLAN_STATUSES)},
                    "note": {"type": "string"},
                    "add_steps": {"type": "array", "items": {"type": "string"}},
                },
            },
            update_plan_step,
        ),
        "start_discussion": Tool(
            "start_discussion",
            "发起团队讨论（等同 /discuss）：多个角色依次发言、相互质疑、最后汇总；讨论在后台进行",
            {**obj, "properties": {"topic": {"type": "string", "description": "讨论议题"}}},
            start_discussion,
        ),
        **lab._conversation_tools(pid, obj),
        "read_pinned_file": Tool(
            "read_pinned_file",
            "分段读取钉住的长材料（上传的 txt、超长消息自动转成的文件）：上下文里只有开头 2000 字，"
            "需要全文时按 offset 逐段读，每段最多 8000 字",
            {
                **obj,
                "properties": {
                    "pin_id": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "length": {"type": "integer", "minimum": 500, "maximum": 8000},
                },
                "required": ["pin_id"],
            },
            read_pinned_file,
        ),
        "discussion_status": Tool(
            "discussion_status",
            "查询团队讨论（start_discussion / /discuss）的进度：状态、阶段、已完成的发言要点与最终结论。"
            "mission_id 省略时查本对话最近一次讨论；wait_seconds（≤120）> 0 时最多等这么久直到讨论结束",
            {
                **obj,
                "properties": {
                    "mission_id": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 120},
                },
            },
            discussion_status,
        ),
        "schedule_task": Tool(
            "schedule_task",
            "把一个研究想法加入每日定时任务（等同 /schedule）：按设定时间自动研究，结果写进晨报",
            {**obj, "properties": {"idea": {"type": "string"}}, "required": ["idea"]},
            queue_night,
        ),
        "queue_night": Tool(
            "queue_night",
            "旧命令别名，等同 schedule_task / schedule：加入每日定时任务队列",
            {**obj, "properties": {"idea": {"type": "string"}}, "required": ["idea"]},
            queue_night,
        ),
        "context_usage": Tool(
            "context_usage",
            "查看上下文用量（等同 /context）：已用 / 预算 token 与钉住的材料数",
            {**obj, "properties": {}},
            context_usage,
        ),
    }

    def write_report(title: str = "", markdown: str = "", summary: str = "", sources: list[str] | None = None) -> Any:
        if len((markdown or "").strip()) < 80:
            return {"ok": False, "error": "markdown 太短：请提交完整报告正文（Markdown）"}
        audit = lab.audit_report(pid, markdown)
        if audit["issues"]:
            markdown = markdown.rstrip() + "\n\n## 平台核验\n\n" + "\n".join(f"- ⚠ {x}" for x in audit["issues"])
        rep = lab.write_report(pid, title=title, markdown=markdown, summary=summary, sources=sources or [])
        return {
            "ok": True,
            "report_id": rep["id"],
            "audit": audit,
            "note": "报告已生成，研究者可以在对话里查看并导出 PDF"
            + ("；平台核验发现问题，已附在报告末尾，请在回答里说明并修正" if audit["issues"] else ""),
        }

    def spawn_subagents(topic: str = "", variants: list[str] | None = None, max_tool_calls: int = 40) -> Any:
        return lab.spawn_subagents(pid, topic=topic, variants=variants or [], max_tool_calls=max_tool_calls)

    def factor_correlation(
        factors: list[dict[str, str]] | None = None, symbols: list[str] | None = None, horizon: int = 1
    ) -> Any:
        if lab.tools.collinearity is None:
            return {"ok": False, "error": "当前环境不能做因子共线性诊断（需要 numpy）"}
        p = lab.store.get(pid) or {}
        syms = [s.upper() for s in symbols or []] or p.get("securities") or ["BTC/USDT"]
        facs = [f for f in factors or [] if isinstance(f, dict) and f.get("expr")]
        return lab._tool(
            pid,
            "factor_correlation",
            "因子共线性诊断",
            {"因子": len(facs), "标的": len(syms)},
            lambda: lab.tools.collinearity(
                syms,
                p.get("timeframe") or "1h",
                factors=facs,
                horizon=int(horizon or 1),
                bars_limit=int(p.get("bars_limit") or 1500),
            ),
        )

    research_tools = {
        "write_report": Tool(
            "write_report",
            "提交调研报告（等同 /report 的最后一步）："
            "Markdown 正文，含摘要、背景、数据与方法、发现、风险、结论与下一步、参考来源。"
            "报告会显示在对话里，可以导出为带平台标识的 PDF",
            {
                **obj,
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string", "description": "三句话以内的结论摘要"},
                    "markdown": {"type": "string", "description": "完整报告正文（Markdown）"},
                    "sources": {"type": "array", "items": {"type": "string"}, "description": "参考来源链接"},
                },
                "required": ["title", "markdown"],
            },
            write_report,
        ),
        "spawn_subagents": Tool(
            "spawn_subagents",
            "唤起多个子 Agent 并行探究同一研究主题的不同方向（2-5 个），每个子 Agent 在独立的子对话里写策略、回测、"
            "做 2 倍成本检查，最后返回并排对比。适合「比较几种思路哪个更好」；不要用于简单任务",
            {
                **obj,
                "properties": {
                    "topic": {"type": "string", "description": "研究主题"},
                    "variants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": '每个子 Agent 负责的方向，例如 ["均线趋势", "布林均值回归", "波动率突破"]',
                    },
                    "max_tool_calls": {"type": "integer", "description": "每个子 Agent 的工具调用上限，默认 40"},
                },
                "required": ["topic", "variants"],
            },
            spawn_subagents,
        ),
        "factor_correlation": Tool(
            "factor_correlation",
            "多因子共线性诊断：对一组因子表达式算两两相关、VIF、条件数、高相关簇，并给出建议保留 / 剔除的因子。"
            "多因子研究（尤其因子数量多时）在合成或建模前必须先做",
            {
                **obj,
                "properties": {
                    "factors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "expr": {"type": "string"}},
                            "required": ["expr"],
                        },
                        "description": "因子表达式，语法同因子研究，例如 ts_mean(close, 20) / close - 1",
                    },
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "horizon": {"type": "integer", "description": "计算 IC 用的持有期（K 线数）"},
                },
                "required": ["factors"],
            },
            factor_correlation,
        ),
    }
    if (lab.store.get(pid) or {}).get("subagent_of"):
        research_tools.pop("spawn_subagents")  # 子 Agent 不再嵌套唤起子 Agent
    return {
        **plan_tools,
        **research_tools,
        "start_autopilot": Tool(
            "start_autopilot",
            "按已设定的目标自动迭代若干轮（等同 /auto）。没有目标时不可用；研究者明确要求连续改进时再用",
            {**obj, "properties": {"rounds": {"type": "integer", "description": "轮数 1-8，不填用项目设置"}}},
            autopilot,
        ),
        "pin_note": Tool(
            "pin_note",
            "把一段重要材料钉进上下文（等同 /pin）：研究者给的约束、调研结论、需要长期记住的事实",
            {**obj, "properties": {"title": {"type": "string"}, "text": {"type": "string"}}, "required": ["text"]},
            pin_note,
        ),
        "compact_history": Tool(
            "compact_history",
            "把较早的对话压缩成摘要（等同 /compact），对话很长、上下文吃紧时用",
            {**obj, "properties": {}},
            compact_history,
        ),
        "set_market": Tool(
            "set_market",
            "设置研究的标的与 K 线周期（研究者没指定、或对话里改了主意时用）",
            {
                **obj,
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": '如 ["BTC/USDT"]'},
                    "timeframe": {
                        "type": "string",
                        "description": "周期：数字+单位，例如 100ms、250ms、1s、5s、1m、15m、1h、4h、1d（秒级 / 毫秒级只有 Binance 现货，由 1s K 线或逐笔成交聚合）",
                    },
                },
            },
            set_market,
        ),
        "restore_version": Tool(
            "restore_version",
            "恢复已有版本及其报告，不重算、不新增试验；比较归档历史版本时先恢复再save_work",
            {**obj, "properties": {"version": {"type": "integer", "minimum": 1}}, "required": ["version"]},
            lambda version: lab.restore_version_evidence(pid, int(version)),
        ),
        "save_work": Tool(
            "save_work",
            "保存当前成果（等同 /save）：策略存草稿，其余存候选；不同历史版本分别归档，不会自动启用",
            {**obj, "properties": {}},
            lambda: lab.save_work(pid),
        ),
    }
