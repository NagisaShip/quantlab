"""AI 红队：专门找"这个策略上线后会怎么亏钱"。

输入是策略源码、体检结果和回测成绩，输出一张按严重度排好的问题清单。
有大模型时用内置的「红队」角色（角色管理里可以改它的提示词）；没有或调用失败时用规则清单，
规则只基于看得见的事实（体检结论、交易数、回撤、源码里有没有止损……），不编造。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from typing import Any

SEVERITY = {"high": "严重", "medium": "注意", "low": "提示"}
ROLE_ID = "red_team"


def _p(severity: str, title: str, detail: str) -> dict[str, str]:
    return {"severity": severity, "title": title, "detail": detail}


def _calls(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else "")
        elif isinstance(n, ast.FunctionDef):
            out.add(f"def:{n.name}")
        elif isinstance(n, ast.Import | ast.ImportFrom):
            for a in n.names:
                out.add(f"import:{a.name}")
    return out


def rule_points(source: str, report: dict[str, Any], metrics: dict[str, Any]) -> list[dict[str, str]]:
    pts: list[dict[str, str]] = []
    items = {i["id"]: i for i in report.get("items") or []}
    for key in ("lookahead", "warmup", "cost", "plateau"):
        it = items.get(key)
        if it and it["verdict"] in ("bad", "warn"):
            pts.append(_p("high" if it["verdict"] == "bad" else "medium", f"体检：{it['title']}", it["summary"]))
    calls = _calls(source)
    trades = int(metrics.get("trades") or 0)
    if 0 < trades < 30:
        pts.append(
            _p(
                "high",
                "交易太少，成绩不可信",
                f"只有 {trades} 笔完成交易，胜率和夏普的误差都很大；至少拉长到 30 笔以上再下结论。",
            )
        )
    elif trades == 0:
        pts.append(_p("high", "没有完成交易", "回测期间一笔都没平仓，现在的成绩只是持仓浮动。"))
    dd = metrics.get("max_drawdown")
    if dd is not None and float(dd) > 0.3:
        pts.append(_p("high", "回撤太深", f"最大回撤 {float(dd):.0%}。实盘里这种回撤大多数人会在谷底停掉策略。"))
    if not ({"set_stop_loss", "set_trailing_stop", "def:custom_stoploss", "def:custom_exit"} & calls):
        pts.append(
            _p(
                "medium",
                "没有任何止损",
                "策略里没有 set_stop_loss / set_trailing_stop / custom_stoploss / "
                "custom_exit，遇到单边暴跌只能等信号反转。交易所模拟盘上也挂不了保护止损单。",
            )
        )
    if "import:random" in calls:
        pts.append(_p("medium", "用了随机数", "结果每次不同，回测成绩无法复现。"))
    stamps = re.findall(r"\b1[5-9]\d{11}\b", source)
    if stamps:
        pts.append(
            _p(
                "high",
                "源码里写死了时间戳",
                f"发现 {len(stamps)} 个 13 位时间戳（如 {stamps[0]}）。按日期开关交易等于把答案写进代码。",
            )
        )
    params = report.get("params") or {}
    if len(params) > 6:
        pts.append(_p("medium", "参数太多", f"声明了 {len(params)} 个参数，自由度越高越容易拟合噪声。"))
    bd = report.get("breakdown") or metrics.get("breakdown") or {}
    for dim, label in (("security", "单一标的"), ("entry_tag", "单一入场标签"), ("hour", "单一时段")):
        rows = bd.get(dim) or []
        pos = [r for r in rows if float(r.get("pnl") or 0) > 0]
        total = sum(float(r["pnl"]) for r in pos)
        if len(rows) >= 2 and total > 0:
            top = max(pos, key=lambda r: float(r["pnl"]))
            if float(top["pnl"]) / total > 0.8 and top["trades"] < max(5, sum(r["trades"] for r in rows) * 0.3):
                pts.append(
                    _p(
                        "medium",
                        f"利润集中在{label}",
                        f"{top['key']} 贡献了 {float(top['pnl']) / total:.0%} 的盈利，"
                        f"但只占 {top['trades']} 笔交易。去掉它策略可能就不赚钱。",
                    )
                )
    exits = {r["key"]: r for r in bd.get("exit_reason") or []}
    stop = sum(exits[k]["trades"] for k in ("stop_loss", "trailing_stop", "custom_stop") if k in exits)
    if trades and stop / trades > 0.6:
        pts.append(_p("low", "大多数交易是止损出场", f"{stop}/{trades} 笔以止损结束，入场信号可能太早或止损太紧。"))
    order = {"high": 0, "medium": 1, "low": 2}
    pts.sort(key=lambda p: order[p["severity"]])
    if not pts:
        pts.append(_p("low", "没有找到明显漏洞", "规则清单没有发现问题；仍建议跑满纸面模拟再晋级。"))
    return pts


def _brief(source: str, report: dict[str, Any], metrics: dict[str, Any]) -> str:
    import json

    items = [{k: i.get(k) for k in ("title", "verdict", "summary")} for i in report.get("items") or []]
    bd = {
        k: (v or [])[:6]
        for k, v in (report.get("breakdown") or {}).items()
        if k in ("entry_tag", "exit_reason", "security")
    }
    return (
        f"源码：\n```python\n{source[:6000]}\n```\n\n体检结论：{json.dumps(items, ensure_ascii=False)}\n\n"
        f"回测成绩：{json.dumps(metrics, ensure_ascii=False, default=str)}\n\n交易分解"
        f"：{json.dumps(bd, ensure_ascii=False)}"
    )


def critique(
    source: str,
    report: dict[str, Any],
    metrics: dict[str, Any],
    *,
    chat: Callable[[str, str], Any] | None = None,
    system: str = "",
) -> dict[str, Any]:
    """返回 {mode, model, points, content, note}。chat(system, user) -> 带 content / model 的结果。"""
    rules = rule_points(source, report, metrics)
    if chat is None:
        return {"mode": "rules", "model": "", "points": rules, "content": "", "note": "没有配置大模型，用规则清单"}
    try:
        from .roles import builtin_roles

        prompt = system or next((r.system_prompt for r in builtin_roles() if r.id == ROLE_ID), "")
        res = chat(prompt, "请对下面这个策略做红队审查。\n\n" + _brief(source, report, metrics))
        text = getattr(res, "content", None) or str(res)
        model = f"{getattr(res, 'provider', '')}/{getattr(res, 'model', '')}".strip("/")
        return {"mode": "llm", "model": model, "points": rules, "content": text, "note": ""}
    except Exception as exc:
        return {
            "mode": "rules",
            "model": "",
            "points": rules,
            "content": "",
            "note": f"大模型调用失败，改用规则清单（{type(exc).__name__}）",
        }


ROLE_SPEC: dict[str, Any] = {
    "key": ROLE_ID,
    "name": "红队",
    "title": "专门找策略会怎么亏钱",
    "description": "站在对手方，假设这个策略已经上线并且在亏钱，倒推最可能的原因。策略体检单里会调用它。",
    "temperature": 0.3,
    "tools": [],
    "system_prompt": (
        "你是量化策略的红队审查员。默认立场：这个策略上线三个月后亏了钱，你要找出最可能的原因。\n\n"
        "逐项检查，只根据给你的源码、体检结论和回测数字说话，不要编造数据：\n"
        "- 未来函数与数据泄漏：是否用了收盘后才知道的信息、写死日期、全样本统计量。\n"
        "- 过拟合：参数个数、参数平台是否孤峰、交易笔数够不够、样本区间是否只覆盖一种行情。\n"
        "- 成本与执行：换手率、成本翻倍后的表现、限价单能否成交、滑点假设。\n"
        "- 风险：有没有止损、最大回撤、单一标的或单一信号贡献了大部分利润、杠杆。\n"
        "- 行情切换：趋势 / 震荡 / 暴跌时分别会怎样。\n\n"
        "输出：按严重度排序的问题清单（每条：问题、证据、怎么验证或修复），最后一行给结论："
        "「可以进纸面模拟」/「修完再说」/「不建议继续」。不超过 400 字，用中文。"
    ),
    "output_format": "①问题清单（严重 → 提示，每条带证据）②结论",
    "tags": ["风控", "体检"],
}
