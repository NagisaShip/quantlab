"""策略室的目标（/goal）：结构化验收目标、文本解析与合理性检查。

一个目标由若干「指标条件」加上回测范围组成：

    {"items": [{"metric": "sharpe", "op": ">=", "target": 1.2, "priority": "must"}, ...],
     "symbols": ["BTC/USDT"], "timeframe": "4h", "start": 毫秒|None, "end": 毫秒|None,
     "bars_limit": 1500, "max_rounds": 4, "note": ""}

- priority=must 的条件全部满足才算达标；try 是加分项，只影响“最佳版本”排序。
- 与旧版 goals 字典（min_sharpe / max_drawdown / min_trades / min_excess）双向兼容。
- 合理性检查先用规则（不依赖模型），再可选地让模型复核；不合理时给出字段级建议值。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# 指标目录：键 → (中文名, 单位, 默认方向, 合理区间提示)
#   单位 pct 表示小数（0.2 = 20%），num 为普通数值，int 为整数
METRICS: dict[str, dict[str, Any]] = {
    "sharpe": {"label": "夏普", "unit": "num", "op": ">=", "soft_max": 2.5, "hard_max": 4.0},
    "sortino": {"label": "索提诺", "unit": "num", "op": ">=", "soft_max": 3.5, "hard_max": 6.0},
    "calmar": {"label": "卡玛", "unit": "num", "op": ">=", "soft_max": 3.0, "hard_max": 6.0},
    "max_drawdown": {"label": "最大回撤", "unit": "pct", "op": "<=", "soft_min": 0.08, "hard_min": 0.03},
    "total_return": {"label": "总收益", "unit": "pct", "op": ">=", "soft_max": 1.5, "hard_max": 5.0},
    "annualized_return": {"label": "年化收益", "unit": "pct", "op": ">=", "soft_max": 0.8, "hard_max": 2.0},
    "excess_return": {"label": "超额收益", "unit": "pct", "op": ">=", "soft_max": 0.6, "hard_max": 2.0},
    "win_rate": {"label": "胜率", "unit": "pct", "op": ">=", "soft_max": 0.65, "hard_max": 0.8},
    "profit_factor": {"label": "盈亏比", "unit": "num", "op": ">=", "soft_max": 2.5, "hard_max": 4.0},
    "volatility": {"label": "波动率", "unit": "pct", "op": "<=", "soft_min": 0.1, "hard_min": 0.03},
    "trades": {"label": "交易笔数", "unit": "int", "op": ">="},
}

# 旧版 goals 键 ↔ 指标
LEGACY = {
    "min_sharpe": ("sharpe", ">="),
    "max_drawdown": ("max_drawdown", "<="),
    "min_trades": ("trades", ">="),
    "min_excess": ("excess_return", ">="),
}
LEGACY_BY_METRIC = {m: k for k, (m, _) in LEGACY.items()}

TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# 解析用的别名（中英文都认）
ALIASES: list[tuple[str, str]] = [
    (r"夏普(?:比率)?|sharpe", "sharpe"),
    (r"索提诺|sortino", "sortino"),
    (r"卡玛|calmar", "calmar"),
    (r"最大回撤|回撤|max[_ ]?dd|drawdown|mdd", "max_drawdown"),
    (r"年化(?:收益)?|annual(?:ized)?(?:[_ ]?return)?|cagr", "annualized_return"),
    (r"超额(?:收益)?|excess(?:[_ ]?return)?|alpha", "excess_return"),
    (r"总收益|收益率?|total[_ ]?return|return", "total_return"),
    (r"胜率|win[_ ]?rate", "win_rate"),
    (r"盈亏比|profit[_ ]?factor|pf", "profit_factor"),
    (r"波动率?|volatility|vol", "volatility"),
    (r"交易(?:笔数|次数)?|成交笔数|trades?", "trades"),
]

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "steady",
        "name": "稳健",
        "items": [
            {"metric": "sharpe", "op": ">=", "target": 1.0, "priority": "must"},
            {"metric": "max_drawdown", "op": "<=", "target": 0.2, "priority": "must"},
            {"metric": "trades", "op": ">=", "target": 10, "priority": "must"},
        ],
    },
    {
        "id": "aggressive",
        "name": "进取",
        "items": [
            {"metric": "annualized_return", "op": ">=", "target": 0.3, "priority": "must"},
            {"metric": "max_drawdown", "op": "<=", "target": 0.35, "priority": "must"},
            {"metric": "sharpe", "op": ">=", "target": 1.0, "priority": "try"},
        ],
    },
    {
        "id": "beat_hold",
        "name": "跑赢持有",
        "items": [
            {"metric": "excess_return", "op": ">=", "target": 0.05, "priority": "must"},
            {"metric": "max_drawdown", "op": "<=", "target": 0.25, "priority": "must"},
            {"metric": "trades", "op": ">=", "target": 8, "priority": "try"},
        ],
    },
    {
        "id": "low_dd",
        "name": "低回撤",
        "items": [
            {"metric": "max_drawdown", "op": "<=", "target": 0.1, "priority": "must"},
            {"metric": "sharpe", "op": ">=", "target": 0.8, "priority": "must"},
            {"metric": "win_rate", "op": ">=", "target": 0.45, "priority": "try"},
        ],
    },
]


# ---------------------------------------------------------------------------
# 规范化 / 兼容
# ---------------------------------------------------------------------------
def normalize_item(raw: dict[str, Any]) -> dict[str, Any] | None:
    metric = str(raw.get("metric") or raw.get("key") or "").strip()
    if metric in LEGACY:
        metric = LEGACY[metric][0]
    if metric not in METRICS:
        return None
    try:
        target = float(raw.get("target"))
    except (TypeError, ValueError):
        return None
    op = raw.get("op") if raw.get("op") in (">=", "<=") else METRICS[metric]["op"]
    if (
        METRICS[metric]["unit"] == "pct"
        and abs(target) > 1.5
        and metric not in ("total_return", "annualized_return", "excess_return")
    ):
        target = target / 100  # 用户写了 15 表示 15%
    if metric == "max_drawdown":
        target = abs(target)
    if METRICS[metric]["unit"] == "int":
        target = float(round(target))
    priority = "try" if raw.get("priority") == "try" else "must"
    return {"metric": metric, "op": op, "target": target, "priority": priority}


def normalize_spec(raw: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """把前端 / 模型给的目标规整成标准结构；缺的字段沿用 base（通常是项目现有值）。"""
    raw = raw or {}
    base = base or {}
    items_raw = raw.get("items")
    if items_raw is None:
        items = list(base.get("items") or [])
    else:
        items = [i for i in (normalize_item(x) for x in items_raw or []) if i]
        # 同一指标同一方向只留最后一条
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for i in items:
            seen[(i["metric"], i["op"])] = i
        items = list(seen.values())
    symbols = raw.get("symbols", base.get("symbols"))
    symbols = [s.strip().upper() for s in (symbols or []) if str(s).strip()] or list(base.get("symbols") or [])
    timeframe = raw.get("timeframe") or base.get("timeframe") or "1h"
    if timeframe not in TIMEFRAMES:
        timeframe = base.get("timeframe") or "1h"
    return {
        "items": items,
        "symbols": symbols,
        "timeframe": timeframe,
        "start": _ms(raw["start"]) if "start" in raw else base.get("start"),
        "end": _ms(raw["end"]) if "end" in raw else base.get("end"),
        "bars_limit": int(raw.get("bars_limit") or base.get("bars_limit") or 1500),
        "max_rounds": max(1, min(int(raw.get("max_rounds") or base.get("max_rounds") or 4), 8)),
        "note": str(raw.get("note", base.get("note", "")) or "")[:400],
    }


def items_from_legacy(goals: dict[str, Any] | None) -> list[dict[str, Any]]:
    out = []
    for key, target in (goals or {}).items():
        if key in LEGACY and target is not None:
            metric, op = LEGACY[key]
            out.append({"metric": metric, "op": op, "target": float(target), "priority": "must"})
    return out


def legacy_from_items(items: list[dict[str, Any]]) -> dict[str, float | None]:
    """旧字段：只有 must 且方向一致的条件能映射回去，其余为 None。"""
    out: dict[str, float | None] = {k: None for k in LEGACY}
    for i in items:
        key = LEGACY_BY_METRIC.get(i["metric"])
        if key and i["priority"] == "must" and LEGACY[key][1] == i["op"]:
            out[key] = i["target"]
    return out


def project_items(project: dict[str, Any]) -> list[dict[str, Any]]:
    """项目当前生效的条件：优先新结构，没有就从旧字段转换。"""
    goal = project.get("goal") or {}
    if goal.get("items"):
        return list(goal["items"])
    return items_from_legacy(project.get("goals"))


def project_spec(project: dict[str, Any]) -> dict[str, Any]:
    """项目当前的完整目标。回测范围以项目字段为准（apply_spec 会同步写回），goal 里只取条件与备注。"""
    goal = project.get("goal") or {}
    return normalize_spec(
        {},
        {
            "items": project_items(project),
            "symbols": project.get("securities") or ["BTC/USDT"],
            "timeframe": project.get("timeframe") or "1h",
            "start": project.get("start"),
            "end": project.get("end"),
            "bars_limit": project.get("bars_limit") or 1500,
            "max_rounds": project.get("max_rounds") or 4,
            "note": goal.get("note", ""),
        },
    )


def merge_legacy(project: dict[str, Any], goals: dict[str, Any]) -> dict[str, Any] | None:
    """旧接口改了 goals 字典时，同步更新结构化目标里对应的必达条件（其它条件保留）。"""
    goal = project.get("goal") or {}
    if not goal.get("items"):
        return None
    kept = [
        i
        for i in goal["items"]
        if not (
            i["priority"] == "must"
            and i["metric"] in LEGACY_BY_METRIC
            and LEGACY[LEGACY_BY_METRIC[i["metric"]]][1] == i["op"]
        )
    ]
    return {**goal, "items": items_from_legacy(goals) + kept}


def apply_spec(project: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """返回要写回项目的字段（新结构 + 旧字段 + 回测范围），调用方负责保存。"""
    return {
        "goal": {**spec, "updated_at": datetime.now(timezone.utc).timestamp()},
        "goals": legacy_from_items(spec["items"]),
        "securities": spec["symbols"] or project.get("securities") or ["BTC/USDT"],
        "timeframe": spec["timeframe"],
        "start": spec.get("start"),
        "end": spec.get("end"),
        "bars_limit": spec["bars_limit"],
        "max_rounds": spec["max_rounds"],
    }


# ---------------------------------------------------------------------------
# 评估与展示
# ---------------------------------------------------------------------------
def fmt_value(metric: str, v: Any) -> str:
    if v is None:
        return "—"
    unit = METRICS.get(metric, {}).get("unit", "num")
    if unit == "pct":
        return f"{float(v) * 100:.1f}%".replace(".0%", "%")
    if unit == "int":
        return str(int(v))
    return f"{float(v):g}"


def item_text(i: dict[str, Any]) -> str:
    label = METRICS[i["metric"]]["label"]
    op = "≥" if i["op"] == ">=" else "≤"
    tail = "（尽量）" if i.get("priority") == "try" else ""
    return f"{label} {op} {fmt_value(i['metric'], i['target'])}{tail}"


def items_text(items: list[dict[str, Any]]) -> str:
    return "、".join(item_text(i) for i in items) or "未设置"


def _actual(metrics: dict[str, Any], metric: str) -> Any:
    v = metrics.get(metric)
    if metric == "max_drawdown" and v is not None:
        return abs(v)
    return v


def evaluate_items(metrics: dict[str, Any] | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    m = metrics or {}
    out = []
    for i in items:
        actual = _actual(m, i["metric"])
        if actual is None:
            ok = False
        elif i["op"] == "<=":
            ok = actual <= i["target"]
        else:
            ok = actual >= i["target"]
        legacy = LEGACY_BY_METRIC.get(i["metric"]) if i["priority"] == "must" else None
        out.append(
            {
                "key": legacy or f"{i['priority']}:{i['metric']}",
                "metric": i["metric"],
                "label": METRICS[i["metric"]]["label"],
                "op": "≥" if i["op"] == ">=" else "≤",
                "target": i["target"],
                "actual": actual,
                "ok": ok,
                "priority": i["priority"],
                "unit": METRICS[i["metric"]]["unit"],
            }
        )
    musts = [x for x in out if x["priority"] == "must"]
    passed = bool(out) and all(x["ok"] for x in (musts or out))
    return {"passed": passed, "items": out, "score": _score(m, out)}


def _score(m: dict[str, Any], items: list[dict[str, Any]]) -> float:
    """选“最佳版本”用：必达项权重最高，尽量项其次，再看夏普，回撤做惩罚。"""
    must = sum(1 for i in items if i["ok"] and i.get("priority", "must") == "must")
    soft = sum(1 for i in items if i["ok"] and i.get("priority") == "try")
    sharpe = m.get("sharpe") or 0.0
    dd = abs(m.get("max_drawdown") or 0.0)
    return round(must * 10 + soft * 3 + max(min(sharpe, 5), -5) - dd * 2, 4)


def gap_report(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    """每个未达标条件离目标还差多少（相对差距），供模型决定怎么改、或判断是否该提前结束。"""
    out = []
    for i in evaluation.get("items") or []:
        if i["ok"] or i["actual"] is None:
            continue
        t, a = float(i["target"]), float(i["actual"])
        rel = abs(t - a) / max(abs(t), 1e-9)
        out.append(
            {
                "metric": i["metric"],
                "label": i["label"],
                "target": t,
                "actual": a,
                "gap": round(rel, 3),
                "priority": i["priority"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# 解析 /goal 文本
# ---------------------------------------------------------------------------
_OP = r"(>=|<=|≥|≤|>|<|=|大于等于|小于等于|不低于|不高于|不超过|至少|至多|大于|小于|高于|低于|超过|控制在)"
_NUM = r"(-?\d+(?:\.\d+)?)\s*(%|％|倍|笔|次)?"
_DATE = r"(\d{4}[-/.]\d{1,2}(?:[-/.]\d{1,2})?)"


def parse_goal_text(text: str) -> dict[str, Any]:
    """把「夏普>1.2 回撤<15% 交易>=20 尽量 胜率>50% BTC/USDT 4h 2023-01~2024-06 6轮」解析成目标。

    返回 {"spec": {...部分字段...}, "unparsed": 剩余文字}；没出现的字段不放进 spec，便于和项目现值合并。
    """
    s = (text or "").strip()
    s = re.sub(r"^/goal\b|^/目标", "", s).strip()
    rest = s
    items: list[dict[str, Any]] = []
    spec: dict[str, Any] = {}

    # 1. 指标条件（"尽量/加分/try" 之后的条件记为 try，直到遇到 "必须/must"）
    tokens = re.split(r"([，,；;。\s]+)", s)
    joined = "".join(tokens)
    pattern = re.compile(
        r"(尽量|最好|加分|try|希望|必须|must)?\s*(" + "|".join(a for a, _ in ALIASES) + r")\s*" + _OP + r"?\s*" + _NUM,
        re.I,
    )
    mode = "must"
    for m in pattern.finditer(joined):
        prefix, name, op_word, num, unit = m.groups()
        if prefix:
            mode = "try" if prefix.lower() in ("尽量", "最好", "加分", "try", "希望") else "must"
        metric = next(k for a, k in ALIASES if re.fullmatch(a, name, re.I))
        val = float(num)
        if unit in ("%", "％"):
            val /= 100
        elif METRICS[metric]["unit"] == "pct" and abs(val) > 1.5:
            val /= 100
        op = METRICS[metric]["op"]
        if op_word:
            if op_word in ("<=", "≤", "<", "小于等于", "不高于", "不超过", "至多", "小于", "低于", "控制在"):
                op = "<="
            elif op_word in (">=", "≥", ">", "大于等于", "不低于", "至少", "大于", "高于", "超过"):
                op = ">="
        if metric == "max_drawdown":
            op, val = "<=", abs(val)
        items.append({"metric": metric, "op": op, "target": val, "priority": mode})
        rest = rest.replace(m.group(0).strip(), " ", 1)
    if items:
        spec["items"] = [i for i in (normalize_item(x) for x in items) if i]

    # 2. 标的
    syms = re.findall(r"\b([A-Za-z]{2,10})\s*/\s*(USDT|USDC|USD|BTC|ETH)\b", s, re.I)
    if syms:
        spec["symbols"] = [f"{a.upper()}/{b.upper()}" for a, b in syms]
        rest = re.sub(r"\b[A-Za-z]{2,10}\s*/\s*(USDT|USDC|USD|BTC|ETH)\b", " ", rest, flags=re.I)
    else:
        bare = re.findall(r"\b(BTC|ETH|SOL|BNB|XRP|DOGE|ADA|AVAX|LINK|DOT)\b", s, re.I)
        if bare:
            spec["symbols"] = [f"{b.upper()}/USDT" for b in dict.fromkeys(x.upper() for x in bare)]
            rest = re.sub(r"\b(BTC|ETH|SOL|BNB|XRP|DOGE|ADA|AVAX|LINK|DOT)\b", " ", rest, flags=re.I)

    # 3. 周期
    tf = re.search(r"\b(1m|5m|15m|30m|1h|4h|1d)\b|(\d+)\s*(分钟|小时|日线|天)", s, re.I)
    if tf:
        if tf.group(1):
            spec["timeframe"] = tf.group(1).lower()
        else:
            n, u = int(tf.group(2)), tf.group(3)
            cand = {"分钟": f"{n}m", "小时": f"{n}h", "日线": "1d", "天": "1d"}[u]
            if cand in TIMEFRAMES:
                spec["timeframe"] = cand
        rest = rest.replace(tf.group(0), " ", 1)

    # 4. 区间：2023-01-01~2024-06-30 / 2023-01 至 2024-06 / 近 2 年
    rng = re.search(_DATE + r"\s*(?:~|～|至|到|—|-{1,2}|→)\s*" + _DATE, s)
    if rng:
        spec["start"], spec["end"] = _ms(rng.group(1)), _ms(rng.group(2), end=True)
        rest = rest.replace(rng.group(0), " ", 1)
    else:
        recent = re.search(r"(?:近|最近|过去)\s*(\d+)\s*(年|个月|月|天)", s)
        if recent:
            n, u = int(recent.group(1)), recent.group(2)
            days = n * {"年": 365, "个月": 30, "月": 30, "天": 1}[u]
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            spec["start"], spec["end"] = now - days * 86_400_000, None
            rest = rest.replace(recent.group(0), " ", 1)

    # 5. 轮数
    rounds = re.search(r"(\d+)\s*轮|rounds?\s*[=:]?\s*(\d+)", s, re.I)
    if rounds:
        spec["max_rounds"] = int(rounds.group(1) or rounds.group(2))
        rest = rest.replace(rounds.group(0), " ", 1)

    rest = re.sub(r"(尽量|最好|加分|必须|希望|并且|而且|同时|以及|和|且|，|,|；|;|。)", " ", rest)
    return {"spec": spec, "unparsed": re.sub(r"\s+", " ", rest).strip()}


def _ms(v: Any, *, end: bool = False) -> int | None:
    if v in (None, "", 0):
        return None
    if isinstance(v, (int, float)):
        return int(v if v > 10**11 else v * 1000)
    txt = str(v).strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            d = datetime.strptime(txt, fmt).replace(tzinfo=timezone.utc)
            if end and fmt == "%Y-%m":
                d = d.replace(month=d.month % 12 + 1, year=d.year + (d.month == 12))
            return int(d.timestamp() * 1000)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 合理性检查（规则）
# ---------------------------------------------------------------------------
def check_spec(spec: dict[str, Any], *, history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """返回 {verdict: ok|warn|unreasonable, issues: [{field, level, problem, suggest}], summary}。

    field 指向表单字段：items.<i>.target、items、timeframe、range、bars_limit、max_rounds、symbols。
    history 为已有版本的回测指标（可选），用来判断目标离现实有多远。
    """
    issues: list[dict[str, Any]] = []
    items = spec.get("items") or []
    tf = spec.get("timeframe") or "1h"

    if not items:
        issues.append(
            {
                "field": "items",
                "level": "error",
                "problem": "还没有任何指标条件",
                "suggest": {"items": TEMPLATES[0]["items"]},
            }
        )
    if items and not any(i["priority"] == "must" for i in items):
        issues.append(
            {
                "field": "items",
                "level": "warn",
                "problem": "所有条件都是“尽量”，迭代没有明确的停止标准",
                "suggest": None,
            }
        )

    bars = _bars_in_range(spec)
    for idx, i in enumerate(items):
        meta = METRICS[i["metric"]]
        t = i["target"]
        f = f"items.{idx}.target"
        if i["op"] == ">=" and "hard_max" in meta and t > meta["hard_max"]:
            issues.append(
                {
                    "field": f,
                    "level": "error",
                    "problem": (
                        f"{meta['label']} ≥ {fmt_value(i['metric'], t)} 几乎不可能在样本外持续，通常意味着过拟合"
                    ),
                    "suggest": meta["soft_max"],
                }
            )
        elif i["op"] == ">=" and "soft_max" in meta and t > meta["soft_max"]:
            issues.append(
                {
                    "field": f,
                    "level": "warn",
                    "problem": f"{meta['label']} ≥ {fmt_value(i['metric'], t)} 偏高，容易逼出过拟合的参数",
                    "suggest": meta["soft_max"],
                }
            )
        if i["op"] == "<=" and "hard_min" in meta and t < meta["hard_min"]:
            issues.append(
                {
                    "field": f,
                    "level": "error",
                    "problem": (
                        f"{meta['label']} ≤ {fmt_value(i['metric'], t)} 对加密资产过于严格，几乎任何策略都做不到"
                    ),
                    "suggest": meta["soft_min"],
                }
            )
        elif i["op"] == "<=" and "soft_min" in meta and t < meta["soft_min"]:
            issues.append(
                {
                    "field": f,
                    "level": "warn",
                    "problem": f"{meta['label']} ≤ {fmt_value(i['metric'], t)} 很严格，可能只能靠极低仓位达到",
                    "suggest": meta["soft_min"],
                }
            )
        if i["metric"] == "trades":
            if t < 5:
                issues.append(
                    {"field": f, "level": "warn", "problem": "交易笔数太少，回测结果没有统计意义", "suggest": 10}
                )
            elif bars and t > bars / 6:
                issues.append(
                    {
                        "field": f,
                        "level": "error",
                        "problem": f"区间约 {bars} 根 K 线，要求 {int(t)} 笔交易意味着几根 K 线就要一次进出，"
                        f"成本会吞掉收益",
                        "suggest": max(10, int(bars / 30)),
                    }
                )
        if i["metric"] == "win_rate" and i["op"] == ">=" and t > 0.6:
            pf = next((x for x in items if x["metric"] == "profit_factor"), None)
            if pf and pf["target"] >= 2:
                issues.append(
                    {"field": f, "level": "warn", "problem": "高胜率和高盈亏比同时要求很少能兼得", "suggest": 0.5}
                )

    # 收益和回撤的组合：年化很高但回撤要求很低
    ann = next((i for i in items if i["metric"] in ("annualized_return", "total_return") and i["op"] == ">="), None)
    dd = next((i for i in items if i["metric"] == "max_drawdown"), None)
    if ann and dd and dd["target"] > 0 and ann["target"] / dd["target"] > 6:
        issues.append(
            {
                "field": f"items.{items.index(dd)}.target",
                "level": "warn",
                "problem": f"{METRICS[ann['metric']]['label']}目标是回撤上限的 {ann['target'] / dd['target']:.0f} 倍，"
                f"收益与回撤比例过于理想",
                "suggest": round(min(0.5, ann["target"] / 3), 2),
            }
        )

    if bars is not None:
        if bars < 300:
            issues.append(
                {
                    "field": "range",
                    "level": "error",
                    "problem": f"回测区间只有约 {bars} 根 K 线，样本太少",
                    "suggest": None,
                }
            )
        elif bars > 20_000:
            issues.append(
                {
                    "field": "range",
                    "level": "warn",
                    "problem": f"区间约 {bars} 根 K 线，超过单次回测上限，会被截断",
                    "suggest": None,
                }
            )
    if spec.get("start") and spec.get("end") and spec["start"] >= spec["end"]:
        issues.append({"field": "range", "level": "error", "problem": "开始时间晚于结束时间", "suggest": None})
    if tf in ("1m", "5m") and (spec.get("bars_limit") or 0) < 2000 and not spec.get("start"):
        issues.append(
            {
                "field": "timeframe",
                "level": "warn",
                "problem": f"{tf} 周期下 {spec.get('bars_limit')} 根 K 线只覆盖几天，结论不可靠",
                "suggest": "1h",
            }
        )
    if (spec.get("max_rounds") or 4) > 6:
        issues.append(
            {"field": "max_rounds", "level": "warn", "problem": "迭代轮数越多越容易对同一段历史过拟合", "suggest": 4}
        )

    # 和已有成绩比：最好的一版都远远够不着，提醒先放宽
    if history and items:
        best = {}
        for m in history:
            for i in items:
                a = _actual(m or {}, i["metric"])
                if a is None:
                    continue
                cur = best.get(i["metric"])
                if cur is None or ((a > cur) if i["op"] == ">=" else (a < cur)):
                    best[i["metric"]] = a
        for idx, i in enumerate(items):
            a = best.get(i["metric"])
            if a is None or i["priority"] != "must":
                continue
            far = (i["op"] == ">=" and i["target"] > 0 and a < i["target"] * 0.35) or (
                i["op"] == "<=" and a > i["target"] * 3
            )
            if far:
                issues.append(
                    {
                        "field": f"items.{idx}.target",
                        "level": "warn",
                        "problem": f"已有版本里{METRICS[i['metric']]['label']}最好只有 "
                        f"{fmt_value(i['metric'], a)}，离目标很远",
                        "suggest": _halfway(i, a),
                    }
                )

    level = "unreasonable" if any(x["level"] == "error" for x in issues) else ("warn" if issues else "ok")
    summary = {
        "ok": "目标合理，可以开始。",
        "warn": "目标可以执行，但有几处值得调整。",
        "unreasonable": "目标存在明显问题，建议先修改再开始。",
    }[level]
    return {"verdict": level, "issues": issues, "summary": summary, "bars": bars}


def _halfway(item: dict[str, Any], actual: float) -> float:
    v = (item["target"] + actual) / 2
    return float(round(v)) if METRICS[item["metric"]]["unit"] == "int" else round(v, 3)


def _bars_in_range(spec: dict[str, Any]) -> int | None:
    tf_ms = TF_MS.get(spec.get("timeframe") or "1h", 3_600_000)
    if spec.get("start"):
        end = spec.get("end") or int(datetime.now(timezone.utc).timestamp() * 1000)
        return max(0, int((end - spec["start"]) / tf_ms))
    return int(spec.get("bars_limit") or 1500)


def apply_suggestions(spec: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
    """把检查给出的字段级建议值写回目标（只处理能直接落地的：条件阈值、周期、轮数、补默认条件）。"""
    out = {**spec, "items": [dict(i) for i in spec.get("items") or []]}
    for issue in check.get("issues") or []:
        field, val = issue.get("field") or "", issue.get("suggest")
        if val is None:
            continue
        m = re.fullmatch(r"items\.(\d+)\.target", field)
        if m and int(m.group(1)) < len(out["items"]):
            try:
                out["items"][int(m.group(1))]["target"] = float(val)
            except (TypeError, ValueError):
                pass
        elif field == "items" and isinstance(val, dict) and val.get("items"):
            out["items"] = [dict(i) for i in val["items"]]
        elif field == "timeframe" and val in TIMEFRAMES:
            out["timeframe"] = val
        elif field == "max_rounds":
            try:
                out["max_rounds"] = int(val)
            except (TypeError, ValueError):
                pass
    return out


def catalog() -> list[dict[str, Any]]:
    """给前端表单用的指标目录。"""
    return [{"metric": k, "label": v["label"], "unit": v["unit"], "op": v["op"]} for k, v in METRICS.items()]


__all__ = [
    "METRICS",
    "TEMPLATES",
    "apply_spec",
    "apply_suggestions",
    "catalog",
    "check_spec",
    "evaluate_items",
    "gap_report",
    "items_text",
    "normalize_spec",
    "parse_goal_text",
    "project_items",
    "project_spec",
]
