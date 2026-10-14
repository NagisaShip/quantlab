"""积木式组装：从库里选标的池、信号（因子 / 模型 / 动量）、组合构建、风控和执行方案，拼成一个策略并生成代码。

结构对应 QuantConnect 的算法框架：选股池 → Alpha → 组合构建 → 风控 → 执行。
生成的是普通的 Python 策略（PARAMS + initialize + rebalance），可以继续手改、回测、交给 AI 改进。
"""

from __future__ import annotations

import json
from typing import Any

SIGNALS = {
    "factor": "因子库里的因子",
    "model": "模型库里的模型（预测值）",
    "momentum": "价格动量（不依赖库）",
    "none": "不用信号（只做配置）",
}
BUILDERS = {
    "scheme": "组合与风控里的配置方案（optimize）",
    "topk": "信号最强的前 K 个等权",
    "rank": "按信号强弱加权（只做多）",
    "equal": "全部标的等权",
}


class AssembleError(ValueError):
    pass


def normalize(blocks: dict[str, Any]) -> dict[str, Any]:
    b = dict(blocks or {})
    syms = [str(s).strip().upper() for s in b.get("symbols") or [] if str(s).strip()]
    if not syms:
        raise AssembleError("至少选一个标的")
    sig = dict(b.get("signal") or {"type": "momentum"})
    if sig.get("type") not in SIGNALS:
        raise AssembleError(f"未知的信号类型：{sig.get('type')}")
    if sig["type"] in ("factor", "model") and not str(sig.get("name") or "").strip():
        raise AssembleError("选了因子或模型就要选具体是哪一个")
    build = dict(b.get("build") or {"type": "topk"})
    if build.get("type") not in BUILDERS:
        raise AssembleError(f"未知的组合构建方式：{build.get('type')}")
    if build["type"] == "scheme" and not str(build.get("name") or "").strip():
        raise AssembleError("选了配置方案就要选具体是哪一个")
    if build["type"] in ("topk", "rank") and sig["type"] == "none":
        raise AssembleError("按信号选币需要一个信号")
    risk = dict(b.get("risk") or {})
    return {
        "name": str(b.get("name") or "积木策略").strip()[:40] or "积木策略",
        "symbols": syms[:30],
        "timeframe": str(b.get("timeframe") or "4h"),
        "signal": {
            "type": sig["type"],
            "name": str(sig.get("name") or "").strip(),
            "lookback": int(sig.get("lookback") or 20),
        },
        "build": {
            "type": build["type"],
            "name": str(build.get("name") or "").strip(),
            "k": max(1, min(int(build.get("k") or 3), len(syms))),
        },
        "risk": {
            "max_weight": float(risk.get("max_weight") or 1.0),
            "cash": float(risk.get("cash") or 0.0),
            "stop_dd": float(risk.get("stop_dd") or 0.0),
        },
        "execution": str(b.get("execution") or "").strip(),
        "every": max(1, int(b.get("every") or 1)),
    }


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def generate(blocks: dict[str, Any]) -> dict[str, Any]:
    """返回 {code, config, outline}。outline 是给界面看的一句话流水线。"""
    c = normalize(blocks)
    sig, build, risk = c["signal"], c["build"], c["risk"]
    params = {"every": c["every"], "max_weight": round(risk["max_weight"], 4), "cash": round(risk["cash"], 4)}
    if build["type"] == "topk":
        params["top_k"] = build["k"]
    if sig["type"] == "momentum":
        params["lookback"] = sig["lookback"]
    if risk["stop_dd"] > 0:
        params["stop_dd"] = round(risk["stop_dd"], 4)

    pl = [f"    {json.dumps(k)}: {json.dumps(v)}," for k, v in params.items()]
    comments = {
        "every": "每隔几根 bar 调一次仓",
        "max_weight": "单个标的权重上限",
        "cash": "保留现金比例",
        "top_k": "持有信号最强的几个",
        "lookback": "动量回看长度（bar）",
        "stop_dd": "账户回撤超过这个比例就清仓观望",
    }
    pl = [f"{line:<34}# {comments[k]}" for line, k in zip(pl, params, strict=True)]
    lines = [
        f'"""{c["name"]}：由积木组装生成。',
        "",
        f"标的池：{', '.join(c['symbols'])} · {c['timeframe']}",
        f"信号：{SIGNALS[sig['type']]}{('「' + sig['name'] + '」') if sig['name'] else ''}",
        f"组合构建：{BUILDERS[build['type']]}{('「' + build['name'] + '」') if build['type'] == 'scheme' else ''}",
        f"执行：{('执行方案「' + c['execution'] + '」') if c['execution'] else '平台默认成本模型'}",
        '"""',
        "",
        "PARAMS = {",
        *pl,
        "}",
        "",
        "",
        "def initialize(ctx):",
        f"    set_universe({json.dumps(c['symbols'])})",
    ]
    if c["execution"]:
        lines.append(f"    set_execution('{_q(c['execution'])}')   # 回测成交价按执行方案的成本曲线")
    lines += [
        "    g.n = 0",
        "    g.peak = 0.0",
        '    run_daily(rebalance, time="every_bar")',
        "",
        "",
        "def signal(ctx):",
    ]
    if sig["type"] == "factor":
        lines.append(f"    return factor('{_q(sig['name'])}')          # {{标的: 因子值}}，只用当前 bar 及以前的数据")
    elif sig["type"] == "model":
        lines.append(f"    return predict('{_q(sig['name'])}')         # {{标的: 预测值}}，回测时滚动重训")
    elif sig["type"] == "momentum":
        lines += [
            "    out = {}",
            "    for s in ctx.securities:",
            '        closes = history(s, PARAMS["lookback"] + 1, "close")',
            '        if len(closes) > PARAMS["lookback"] and closes[0] > 0:',
            "            out[s] = closes[-1] / closes[0] - 1",
            "    return out",
        ]
    else:
        lines.append("    return {}")
    lines += ["", "", "def rebalance(ctx):", "    g.n += 1", '    if (g.n - 1) % PARAMS["every"]:', "        return"]
    if risk["stop_dd"] > 0:
        lines += [
            "    equity = ctx.portfolio.total_value",
            "    g.peak = max(g.peak, equity)",
            '    if g.peak and equity < g.peak * (1 - PARAMS["stop_dd"]):',
            '        log.info("回撤超过上限，清仓观望")',
            "        order_target_weights({})",
            "        g.peak = equity",
            "        return",
        ]
    lines.append("    score = signal(ctx)")
    if build["type"] == "scheme":
        exp = "score or None" if sig["type"] != "none" else "None"
        lines.append(f"    w = optimize('{_q(build['name'])}', expected={exp})   # 方案自带约束与风控")
    elif build["type"] == "topk":
        lines += [
            "    if not score:",
            "        return",
            '    top = sorted(score, key=score.get, reverse=True)[:PARAMS["top_k"]]',
            "    w = {s: 1 / len(top) for s in top}",
        ]
    elif build["type"] == "rank":
        lines += [
            "    if not score:",
            "        return",
            "    ranked = sorted(score, key=score.get)",
            "    w = {s: i + 1 for i, s in enumerate(ranked)}",
            "    total = sum(w.values())",
            "    w = {s: v / total for s, v in w.items()}",
        ]
    else:
        lines.append("    w = {s: 1 / len(ctx.securities) for s in ctx.securities}")
    lines += [
        "    w = cap_weights(w or {})",
        "    order_target_weights(w)",
        "",
        "",
        "def cap_weights(w):",
        '    """单币上限 + 现金比例：超出上限的部分不再分给别人，留作现金。"""',
        '    budget = max(0.0, 1 - PARAMS["cash"])',
        "    total = sum(v for v in w.values() if v > 0)",
        "    if total <= 0:",
        "        return {}",
        '    return {s: min(v / total * budget, PARAMS["max_weight"]) for s, v in w.items() if v > 0}',
        "",
    ]
    outline = [
        {"step": "标的池", "text": f"{len(c['symbols'])} 个 · {c['timeframe']}"},
        {"step": "信号", "text": (sig["name"] or SIGNALS[sig["type"]])},
        {"step": "组合构建", "text": build["name"] if build["type"] == "scheme" else BUILDERS[build["type"]]},
        {
            "step": "风控",
            "text": f"单币 ≤ {risk['max_weight']:.0%}"
            + (f" · 回撤 {risk['stop_dd']:.0%} 清仓" if risk["stop_dd"] else "")
            + (f" · 现金 {risk['cash']:.0%}" if risk["cash"] else ""),
        },
        {"step": "执行", "text": c["execution"] or "默认成本"},
    ]
    return {"code": "\n".join(lines), "config": c, "outline": outline}


def check_refs(c: dict[str, Any], store: Any) -> list[str]:
    """引用的库成果是否存在。"""
    warn: list[str] = []
    sig, build = c["signal"], c["build"]
    if sig["type"] == "factor":
        from .factor_expr import FactorExprError, parse
        from .factor_lib import FactorLibrary

        if FactorLibrary(store).by_name(sig["name"]) is None:
            try:
                parse(sig["name"])
            except FactorExprError:
                warn.append(f"因子库里没有「{sig['name']}」，也不是合法的因子表达式")
    if sig["type"] == "model":
        from .model_lib import ModelLibrary

        if ModelLibrary(store).by_name(sig["name"]) is None:
            warn.append(f"模型库里没有「{sig['name']}」")
    if build["type"] == "scheme":
        from .portfolio_lib import PortfolioLibrary

        if PortfolioLibrary(store).by_name(build["name"]) is None:
            warn.append(f"组合与风控里没有配置方案「{build['name']}」")
    if c["execution"]:
        from .execution_lib import ExecutionLibrary

        rec = ExecutionLibrary(store).by_name(c["execution"])
        if rec is None or not rec.get("cost_model"):
            warn.append(f"执行与监控里没有执行方案「{c['execution']}」（或还没有成本曲线）")
    return warn


__all__ = ["BUILDERS", "SIGNALS", "AssembleError", "check_refs", "generate", "normalize"]
