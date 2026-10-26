"""研究任务的通用目标（/goal 的泛化）。

策略研究沿用 `agents/lab_goal.py`；因子、模型、组合、执行这几类研究各自声明一个 `GoalDomain`
（指标目录 + 别名 + 模板 + 合理区间），共用同一套：规范化、文本解析、评估、差距报告、合理性检查。

目标结构与策略的 /goal 一致，前端表单可以复用：

    {"items": [{"metric": "rank_ic", "op": ">=", "target": 0.03, "priority": "must"}, ...],
     "symbols": [...], "timeframe": "4h", "start": 毫秒|None, "end": 毫秒|None,
     "bars_limit": 1500, "max_rounds": 4, "horizon": 1, "note": ""}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..agents.lab_goal import TF_MS, TIMEFRAMES, _ms

_OP = r"(>=|<=|≥|≤|>|<|=|大于等于|小于等于|不低于|不高于|不超过|至少|至多|大于|小于|高于|低于|超过|控制在)"
_NUM = r"(-?\d+(?:\.\d+)?)\s*(%|％|倍|笔|次|bps)?"
_LE_WORDS = ("<=", "≤", "<", "小于等于", "不高于", "不超过", "至多", "小于", "低于", "控制在")
_GE_WORDS = (">=", "≥", ">", "大于等于", "不低于", "至少", "大于", "高于", "超过")


@dataclass
class GoalDomain:
    """一类研究的验收指标目录。

    metrics: 键 → {label, unit(pct|num|int|bps), op, soft_max/hard_max/soft_min/hard_min, hint}
    aliases: [(正则, 键)]，用于解析 `/factor RankIC>0.03 换手<30%` 这类文本
    """

    key: str
    label: str
    metrics: dict[str, dict[str, Any]]
    aliases: list[tuple[str, str]]
    templates: list[dict[str, Any]]
    defaults: dict[str, Any] = field(default_factory=dict)
    score_key: str = ""  # 选“最佳版本”时的连续分数指标（越大越好）

    # ---------------- 规范化 ----------------
    def normalize_item(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        metric = str(raw.get("metric") or raw.get("key") or "").strip()
        meta = self.metrics.get(metric)
        if meta is None:
            return None
        try:
            target = float(raw.get("target"))
        except (TypeError, ValueError):
            return None
        op = raw.get("op") if raw.get("op") in (">=", "<=") else meta["op"]
        if meta["unit"] == "pct" and abs(target) > 1.5 and not meta.get("big"):
            target = target / 100  # 用户写了 30 表示 30%
        if meta["unit"] == "int":
            target = float(round(target))
        priority = "try" if raw.get("priority") == "try" else "must"
        return {"metric": metric, "op": op, "target": target, "priority": priority}

    def normalize_spec(self, raw: dict[str, Any] | None, base: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = raw or {}
        base = {**self.defaults, **(base or {})}
        items_raw = raw.get("items")
        if items_raw is None:
            items = list(base.get("items") or [])
        else:
            seen: dict[tuple[str, str], dict[str, Any]] = {}
            for i in (self.normalize_item(x) for x in items_raw or []):
                if i:
                    seen[(i["metric"], i["op"])] = i
            items = list(seen.values())
        symbols = raw.get("symbols", base.get("symbols"))
        symbols = [s.strip().upper() for s in (symbols or []) if str(s).strip()] or list(base.get("symbols") or [])
        timeframe = raw.get("timeframe") or base.get("timeframe") or "4h"
        if timeframe not in TIMEFRAMES:
            timeframe = base.get("timeframe") or "4h"
        horizon = raw.get("horizon", base.get("horizon"))
        return {
            "items": items,
            "symbols": symbols,
            "timeframe": timeframe,
            "start": _ms(raw["start"]) if "start" in raw else base.get("start"),
            "end": _ms(raw["end"]) if "end" in raw else base.get("end"),
            "bars_limit": int(raw.get("bars_limit") or base.get("bars_limit") or 1500),
            "max_rounds": max(1, min(int(raw.get("max_rounds") or base.get("max_rounds") or 4), 8)),
            "horizon": max(1, min(int(horizon or 1), 60)),
            "note": str(raw.get("note", base.get("note", "")) or "")[:400],
        }

    # ---------------- 展示 ----------------
    def fmt_value(self, metric: str, v: Any) -> str:
        if v is None:
            return "—"
        unit = self.metrics.get(metric, {}).get("unit", "num")
        if unit == "pct":
            return f"{float(v) * 100:.1f}%".replace(".0%", "%")
        if unit == "int":
            return str(int(v))
        if unit == "bps":
            return f"{float(v):.1f} bps"
        return f"{float(v):.4g}"

    def item_text(self, i: dict[str, Any]) -> str:
        label = self.metrics[i["metric"]]["label"]
        op = "≥" if i["op"] == ">=" else "≤"
        tail = "（尽量）" if i.get("priority") == "try" else ""
        return f"{label} {op} {self.fmt_value(i['metric'], i['target'])}{tail}"

    def items_text(self, items: list[dict[str, Any]]) -> str:
        return "、".join(self.item_text(i) for i in items) or "未设置"

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {"metric": k, "label": v["label"], "unit": v["unit"], "op": v["op"], "hint": v.get("hint", "")}
            for k, v in self.metrics.items()
        ]

    # ---------------- 评估 ----------------
    def evaluate(self, metrics: dict[str, Any] | None, items: list[dict[str, Any]]) -> dict[str, Any]:
        m = metrics or {}
        out = []
        for i in items:
            meta = self.metrics.get(i["metric"])
            if meta is None:
                continue
            actual = m.get(i["metric"])
            if actual is None:
                ok = False
            elif i["op"] == "<=":
                ok = actual <= i["target"]
            else:
                ok = actual >= i["target"]
            out.append(
                {
                    "key": f"{i['priority']}:{i['metric']}",
                    "metric": i["metric"],
                    "label": meta["label"],
                    "op": "≥" if i["op"] == ">=" else "≤",
                    "target": i["target"],
                    "actual": actual,
                    "ok": ok,
                    "priority": i["priority"],
                    "unit": meta["unit"],
                }
            )
        musts = [x for x in out if x["priority"] == "must"]
        passed = bool(out) and all(x["ok"] for x in (musts or out))
        must_ok = sum(1 for x in out if x["ok"] and x["priority"] == "must")
        soft_ok = sum(1 for x in out if x["ok"] and x["priority"] == "try")
        cont = m.get(self.score_key) or 0.0
        try:
            cont = max(min(float(cont), 5.0), -5.0)
        except (TypeError, ValueError):
            cont = 0.0
        return {"passed": passed, "items": out, "score": round(must_ok * 10 + soft_ok * 3 + cont, 4)}

    def gap_report(self, evaluation: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for i in evaluation.get("items") or []:
            if i["ok"] or i["actual"] is None:
                continue
            t, a = float(i["target"]), float(i["actual"])
            out.append(
                {
                    "metric": i["metric"],
                    "label": i["label"],
                    "target": t,
                    "actual": a,
                    "gap": round(abs(t - a) / max(abs(t), 1e-9), 3),
                    "priority": i["priority"],
                }
            )
        return out

    # ---------------- 文本解析 ----------------
    def parse_text(self, text: str) -> dict[str, Any]:
        s = re.sub(r"^/\S+", "", (text or "").strip()).strip()
        rest = s
        spec: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        names = sorted((a for a, _ in self.aliases), key=len, reverse=True)
        pattern = re.compile(
            r"(尽量|最好|加分|try|希望|必须|must)?\s*(" + "|".join(names) + r")\s*" + _OP + r"?\s*" + _NUM, re.I
        )
        mode = "must"
        for m in pattern.finditer(s):
            prefix, name, op_word, num, unit = m.groups()
            if prefix:
                mode = "try" if prefix.lower() in ("尽量", "最好", "加分", "try", "希望") else "must"
            metric = next(k for a, k in self.aliases if re.fullmatch(a, name, re.I))
            val = float(num)
            if unit in ("%", "％"):
                val /= 100
            op = self.metrics[metric]["op"]
            if op_word in _LE_WORDS:
                op = "<="
            elif op_word in _GE_WORDS:
                op = ">="
            items.append({"metric": metric, "op": op, "target": val, "priority": mode})
            rest = rest.replace(m.group(0).strip(), " ", 1)
        if items:
            spec["items"] = [i for i in (self.normalize_item(x) for x in items) if i]
        syms = re.findall(r"\b([A-Za-z]{2,10})\s*/\s*(USDT|USDC|USD)\b", s, re.I)
        if syms:
            spec["symbols"] = [f"{a.upper()}/{b.upper()}" for a, b in syms]
            rest = re.sub(r"\b[A-Za-z]{2,10}\s*/\s*(USDT|USDC|USD)\b", " ", rest, flags=re.I)
        tf = re.search(r"\b(1m|5m|15m|30m|1h|4h|1d)\b", s, re.I)
        if tf:
            spec["timeframe"] = tf.group(1).lower()
            rest = rest.replace(tf.group(0), " ", 1)
        hz = re.search(r"(?:持有期|预测期|horizon)\s*[=:：]?\s*(\d+)", s, re.I)
        if hz:
            spec["horizon"] = int(hz.group(1))
            rest = rest.replace(hz.group(0), " ", 1)
        recent = re.search(r"(?:近|最近|过去)\s*(\d+)\s*(年|个月|月|天)", s)
        if recent:
            n, u = int(recent.group(1)), recent.group(2)
            days = n * {"年": 365, "个月": 30, "月": 30, "天": 1}[u]
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            spec["start"], spec["end"] = now - days * 86_400_000, None
            rest = rest.replace(recent.group(0), " ", 1)
        rounds = re.search(r"(\d+)\s*轮", s)
        if rounds:
            spec["max_rounds"] = int(rounds.group(1))
            rest = rest.replace(rounds.group(0), " ", 1)
        rest = re.sub(r"(尽量|最好|加分|必须|希望|并且|而且|同时|以及|和|且|，|,|；|;|。)", " ", rest)
        return {"spec": spec, "unparsed": re.sub(r"\s+", " ", rest).strip()}

    # ---------------- 合理性检查 ----------------
    def check(
        self,
        spec: dict[str, Any],
        *,
        history: list[dict[str, Any]] | None = None,
        extra: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        issues: list[dict[str, Any]] = list(extra or [])
        items = spec.get("items") or []
        if not items:
            issues.append(
                {
                    "field": "items",
                    "level": "error",
                    "problem": "还没有任何指标条件",
                    "suggest": {"items": self.templates[0]["items"]} if self.templates else None,
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
        for idx, i in enumerate(items):
            meta = self.metrics[i["metric"]]
            t, f = i["target"], f"items.{idx}.target"
            if i["op"] == ">=" and "hard_max" in meta and t > meta["hard_max"]:
                issues.append(
                    {
                        "field": f,
                        "level": "error",
                        "problem": f"{meta['label']} ≥ {self.fmt_value(i['metric'], t)} 在加密市场几乎不可能稳定做到，"
                        "强行追求只会逼出过拟合",
                        "suggest": meta["soft_max"],
                    }
                )
            elif i["op"] == ">=" and "soft_max" in meta and t > meta["soft_max"]:
                issues.append(
                    {
                        "field": f,
                        "level": "warn",
                        "problem": f"{meta['label']} ≥ {self.fmt_value(i['metric'], t)} 偏高，容易逼出过拟合",
                        "suggest": meta["soft_max"],
                    }
                )
            if i["op"] == "<=" and "hard_min" in meta and t < meta["hard_min"]:
                issues.append(
                    {
                        "field": f,
                        "level": "error",
                        "problem": f"{meta['label']} ≤ {self.fmt_value(i['metric'], t)} 过于严格",
                        "suggest": meta["soft_min"],
                    }
                )
            elif i["op"] == "<=" and "soft_min" in meta and t < meta["soft_min"]:
                issues.append(
                    {
                        "field": f,
                        "level": "warn",
                        "problem": f"{meta['label']} ≤ {self.fmt_value(i['metric'], t)} 很严格",
                        "suggest": meta["soft_min"],
                    }
                )
        bars = self._bars(spec)
        min_bars = int(self.defaults.get("min_bars") or 300)
        if bars is not None and bars < min_bars:
            issues.append(
                {
                    "field": "range",
                    "level": "error",
                    "problem": f"区间只有约 {bars} 根 K 线，样本太少（至少 {min_bars} 根）",
                    "suggest": None,
                }
            )
        if spec.get("start") and spec.get("end") and spec["start"] >= spec["end"]:
            issues.append({"field": "range", "level": "error", "problem": "开始时间晚于结束时间", "suggest": None})
        if (spec.get("max_rounds") or 4) > 6:
            issues.append(
                {
                    "field": "max_rounds",
                    "level": "warn",
                    "problem": "轮数越多，越容易在同一段历史上碰运气找到假信号",
                    "suggest": 4,
                }
            )
        if history and items:
            for idx, i in enumerate(items):
                vals = [h.get(i["metric"]) for h in history if h and h.get(i["metric"]) is not None]
                if not vals or i["priority"] != "must":
                    continue
                best = max(vals) if i["op"] == ">=" else min(vals)
                far = (i["op"] == ">=" and i["target"] > 0 and best < i["target"] * 0.35) or (
                    i["op"] == "<=" and best > i["target"] * 3
                )
                if far:
                    issues.append(
                        {
                            "field": f"items.{idx}.target",
                            "level": "warn",
                            "problem": f"已有版本里{self.metrics[i['metric']]['label']}最好只有 "
                            f"{self.fmt_value(i['metric'], best)}，离目标很远",
                            "suggest": round((i["target"] + best) / 2, 4),
                        }
                    )
        level = "unreasonable" if any(x["level"] == "error" for x in issues) else ("warn" if issues else "ok")
        summary = {
            "ok": "目标合理，可以开始。",
            "warn": "目标可以执行，但有几处值得调整。",
            "unreasonable": "目标存在明显问题，建议先修改再开始。",
        }[level]
        return {"verdict": level, "issues": issues, "summary": summary, "bars": bars}

    def apply_suggestions(self, spec: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
        out = {**spec, "items": [dict(i) for i in spec.get("items") or []]}
        for issue in check.get("issues") or []:
            fld, val = issue.get("field") or "", issue.get("suggest")
            if val is None:
                continue
            m = re.fullmatch(r"items\.(\d+)\.target", fld)
            if m and int(m.group(1)) < len(out["items"]):
                try:
                    out["items"][int(m.group(1))]["target"] = float(val)
                except (TypeError, ValueError):
                    pass
            elif fld == "items" and isinstance(val, dict) and val.get("items"):
                out["items"] = [dict(i) for i in val["items"]]
            elif fld == "max_rounds":
                out["max_rounds"] = int(val)
        return out

    @staticmethod
    def _bars(spec: dict[str, Any]) -> int | None:
        tf_ms = TF_MS.get(spec.get("timeframe") or "4h", 14_400_000)
        if spec.get("start"):
            end = spec.get("end") or int(datetime.now(timezone.utc).timestamp() * 1000)
            return max(0, int((end - spec["start"]) / tf_ms))
        return int(spec.get("bars_limit") or 1500)

    def meta(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "metrics": self.catalog(),
            "templates": self.templates,
            "defaults": {k: v for k, v in self.defaults.items() if k != "items"},
        }


# ---------------------------------------------------------------------------
# 因子研究的指标目录
# ---------------------------------------------------------------------------
FACTOR = GoalDomain(
    key="factor",
    label="因子",
    metrics={
        "rank_ic": {
            "label": "RankIC 均值",
            "unit": "num",
            "op": ">=",
            "soft_max": 0.08,
            "hard_max": 0.15,
            "hint": "因子值与下一期收益的秩相关；加密截面 0.02~0.05 已算不错",
        },
        "ic": {"label": "IC 均值", "unit": "num", "op": ">=", "soft_max": 0.08, "hard_max": 0.15},
        "icir": {
            "label": "ICIR",
            "unit": "num",
            "op": ">=",
            "soft_max": 0.6,
            "hard_max": 1.2,
            "hint": "IC 均值 / IC 标准差（按期），衡量稳定性",
        },
        "ic_t": {
            "label": "IC t 值",
            "unit": "num",
            "op": ">=",
            "soft_max": 8.0,
            "hard_max": 20.0,
            "hint": "按试验次数修正前；建议 ≥ 3",
        },
        "ic_win": {"label": "IC 为正比例", "unit": "pct", "op": ">=", "soft_max": 0.7, "hard_max": 0.85},
        "ls_sharpe": {
            "label": "多空夏普",
            "unit": "num",
            "op": ">=",
            "soft_max": 3.0,
            "hard_max": 6.0,
            "hint": "按分组做多最高组、做空最低组，扣除成本后的年化夏普",
        },
        "ls_return": {
            "label": "多空年化收益",
            "unit": "pct",
            "op": ">=",
            "soft_max": 1.0,
            "hard_max": 3.0,
            "big": True,
        },
        "turnover": {
            "label": "换手率",
            "unit": "pct",
            "op": "<=",
            "soft_min": 0.05,
            "hard_min": 0.01,
            "hint": "每期多空组合权重变化的平均值",
        },
        "max_corr": {"label": "与库内最大相关", "unit": "num", "op": "<=", "soft_min": 0.3, "hard_min": 0.1},
        "oos_retention": {
            "label": "样本外保留率",
            "unit": "pct",
            "op": ">=",
            "soft_max": 1.0,
            "hard_max": 1.5,
            "big": True,
            "hint": "样本外 RankIC / 样本内 RankIC",
        },
        "coverage": {"label": "覆盖率", "unit": "pct", "op": ">=", "hard_max": 1.0},
    },
    aliases=[
        (r"rank\s*_?ic|rankic|秩相关", "rank_ic"),
        (r"icir|ic\s*ir|信息比", "icir"),
        (r"ic\s*_?t|t\s*值", "ic_t"),
        (r"ic\s*胜率|ic\s*为正", "ic_win"),
        (r"多空夏普|ls\s*_?sharpe", "ls_sharpe"),
        (r"多空(?:年化)?收益|ls\s*_?return", "ls_return"),
        (r"换手(?:率)?|turnover", "turnover"),
        (r"(?:库内)?相关(?:性)?|max\s*_?corr", "max_corr"),
        (r"样本外(?:保留率?)?|oos", "oos_retention"),
        (r"覆盖率?|coverage", "coverage"),
        (r"\bic\b|IC", "ic"),
    ],
    templates=[
        {
            "id": "solid",
            "name": "稳健入库",
            "items": [
                {"metric": "rank_ic", "op": ">=", "target": 0.03, "priority": "must"},
                {"metric": "icir", "op": ">=", "target": 0.15, "priority": "must"},
                {"metric": "oos_retention", "op": ">=", "target": 0.5, "priority": "must"},
                {"metric": "max_corr", "op": "<=", "target": 0.7, "priority": "must"},
                {"metric": "turnover", "op": "<=", "target": 0.5, "priority": "try"},
            ],
        },
        {
            "id": "tradable",
            "name": "可交易",
            "items": [
                {"metric": "ls_sharpe", "op": ">=", "target": 1.0, "priority": "must"},
                {"metric": "turnover", "op": "<=", "target": 0.3, "priority": "must"},
                {"metric": "rank_ic", "op": ">=", "target": 0.02, "priority": "try"},
            ],
        },
        {
            "id": "novel",
            "name": "求新",
            "items": [
                {"metric": "rank_ic", "op": ">=", "target": 0.02, "priority": "must"},
                {"metric": "max_corr", "op": "<=", "target": 0.4, "priority": "must"},
                {"metric": "oos_retention", "op": ">=", "target": 0.5, "priority": "try"},
            ],
        },
    ],
    defaults={"symbols": [], "timeframe": "4h", "bars_limit": 1500, "max_rounds": 4, "horizon": 1, "min_bars": 300},
    score_key="rank_ic_x100",
)


# ---------------------------------------------------------------------------
# 模型研究的指标目录（全部是滚动样本外口径）
# ---------------------------------------------------------------------------
MODEL = GoalDomain(
    key="model",
    label="模型",
    metrics={
        "oos_rank_ic": {
            "label": "样本外 RankIC",
            "unit": "num",
            "op": ">=",
            "soft_max": 0.1,
            "hard_max": 0.2,
            "hint": "滚动训练拼接出的样本外预测与真实收益的秩相关；波动率模型为预测与实际波动的相关",
        },
        "beat_baseline": {
            "label": "超出基线",
            "unit": "num",
            "op": ">=",
            "soft_max": 0.05,
            "hard_max": 0.12,
            "hint": "样本外 RankIC − 基线（线性模型或特征等权）的 RankIC；≤ 0 说明复杂模型没有带来增量",
        },
        "oos_icir": {"label": "样本外 ICIR", "unit": "num", "op": ">=", "soft_max": 0.6, "hard_max": 1.2},
        "dir_acc": {
            "label": "方向准确率",
            "unit": "pct",
            "op": ">=",
            "soft_max": 0.6,
            "hard_max": 0.7,
            "hint": "截面口径是“跑赢中位数”的判断准确率；52%~55% 已经有用",
        },
        "brier": {
            "label": "Brier 分数",
            "unit": "num",
            "op": "<=",
            "soft_min": 0.2,
            "hard_min": 0.15,
            "hint": "分类概率的均方误差，0.25 相当于瞎猜",
        },
        "ls_sharpe": {"label": "多空夏普", "unit": "num", "op": ">=", "soft_max": 3.0, "hard_max": 6.0},
        "turnover": {"label": "换手率", "unit": "pct", "op": "<=", "soft_min": 0.05, "hard_min": 0.01},
        "fold_positive": {
            "label": "各段为正比例",
            "unit": "pct",
            "op": ">=",
            "hard_max": 1.0,
            "hint": "滚动各段样本外 RankIC 为正的比例",
        },
        "psi_max": {"label": "最大特征漂移 PSI", "unit": "num", "op": "<=", "soft_min": 0.05, "hard_min": 0.01},
        "leak_passed": {
            "label": "泄漏检查通过",
            "unit": "int",
            "op": ">=",
            "hard_max": 1,
            "hint": "1 = 截断重算一致、打乱标签学不到东西、purge 足够",
        },
    },
    aliases=[
        (r"样本外\s*rank\s*_?ic|oos\s*_?rank\s*_?ic|rank\s*_?ic|rankic", "oos_rank_ic"),
        (r"超出基线|跑赢基线|beat\s*_?baseline", "beat_baseline"),
        (r"icir|ic\s*ir", "oos_icir"),
        (r"方向准确率|准确率|accuracy|dir\s*_?acc", "dir_acc"),
        (r"brier", "brier"),
        (r"多空夏普|ls\s*_?sharpe|夏普", "ls_sharpe"),
        (r"换手(?:率)?|turnover", "turnover"),
        (r"各段为正|fold\s*_?positive", "fold_positive"),
        (r"psi|漂移", "psi_max"),
        (r"泄漏(?:检查)?|leak", "leak_passed"),
    ],
    templates=[
        {
            "id": "solid",
            "name": "稳健预测",
            "items": [
                {"metric": "oos_rank_ic", "op": ">=", "target": 0.03, "priority": "must"},
                {"metric": "beat_baseline", "op": ">=", "target": 0.0, "priority": "must"},
                {"metric": "leak_passed", "op": ">=", "target": 1, "priority": "must"},
                {"metric": "fold_positive", "op": ">=", "target": 0.75, "priority": "try"},
            ],
        },
        {
            "id": "direction",
            "name": "方向预测",
            "items": [
                {"metric": "dir_acc", "op": ">=", "target": 0.53, "priority": "must"},
                {"metric": "brier", "op": "<=", "target": 0.25, "priority": "must"},
                {"metric": "leak_passed", "op": ">=", "target": 1, "priority": "must"},
            ],
        },
        {
            "id": "tradable",
            "name": "可交易",
            "items": [
                {"metric": "ls_sharpe", "op": ">=", "target": 1.0, "priority": "must"},
                {"metric": "turnover", "op": "<=", "target": 0.3, "priority": "must"},
                {"metric": "leak_passed", "op": ">=", "target": 1, "priority": "must"},
            ],
        },
    ],
    defaults={"symbols": [], "timeframe": "4h", "bars_limit": 1500, "max_rounds": 4, "horizon": 1, "min_bars": 500},
    score_key="oos_rank_ic_x100",
)


PORTFOLIO = GoalDomain(
    key="portfolio",
    label="组合",
    metrics={
        "sharpe": {
            "label": "夏普",
            "unit": "num",
            "op": ">=",
            "soft_max": 2.5,
            "hard_max": 4.0,
            "hint": "滚动组合回测（扣成本）的年化夏普",
        },
        "max_drawdown": {"label": "最大回撤", "unit": "pct", "op": "<=", "soft_min": 0.08, "hard_min": 0.03},
        "ann_vol": {"label": "年化波动", "unit": "pct", "op": "<=", "soft_min": 0.08, "hard_min": 0.03},
        "cvar_95": {
            "label": "CVaR 95%（单根 K 线）",
            "unit": "pct",
            "op": "<=",
            "soft_min": 0.003,
            "hard_min": 0.001,
            "hint": "最差 5% 的 K 线平均亏损",
        },
        "beat_ew": {
            "label": "夏普超出等权",
            "unit": "num",
            "op": ">=",
            "soft_max": 1.0,
            "hard_max": 2.0,
            "hint": "组合夏普 − 同期等权组合夏普；等权是很难打败的基准",
        },
        "dd_vs_ew": {"label": "回撤比等权少", "unit": "pct", "op": ">=", "soft_max": 0.3, "hard_max": 0.5},
        "turnover": {"label": "调仓换手", "unit": "pct", "op": "<=", "soft_min": 0.02, "hard_min": 0.005},
        "effective_n": {
            "label": "有效持仓数",
            "unit": "num",
            "op": ">=",
            "hint": "风险资产按总敞口归一后计算 1 / Σw²；现金不计入",
        },
        "calmar": {"label": "Calmar", "unit": "num", "op": ">=", "soft_max": 5.0, "hard_max": 10.0},
    },
    aliases=[
        (r"夏普超出等权|跑赢等权|beat\s*_?ew", "beat_ew"),
        (r"夏普|sharpe", "sharpe"),
        (r"回撤比等权少|dd\s*_?vs\s*_?ew", "dd_vs_ew"),
        (r"(?:最大)?回撤|drawdown|mdd", "max_drawdown"),
        (r"(?:年化)?波动(?:率)?|vol", "ann_vol"),
        (r"cvar", "cvar_95"),
        (r"换手(?:率)?|turnover", "turnover"),
        (r"有效持仓(?:数)?|effective\s*_?n", "effective_n"),
        (r"calmar|卡玛", "calmar"),
    ],
    templates=[
        {
            "id": "steady",
            "name": "稳健",
            "items": [
                {"metric": "sharpe", "op": ">=", "target": 1.0, "priority": "must"},
                {"metric": "max_drawdown", "op": "<=", "target": 0.25, "priority": "must"},
                {"metric": "beat_ew", "op": ">=", "target": 0.05, "priority": "try"},
            ],
        },
        {
            "id": "low_vol",
            "name": "低波动",
            "items": [
                {"metric": "ann_vol", "op": "<=", "target": 0.4, "priority": "must"},
                {"metric": "max_drawdown", "op": "<=", "target": 0.2, "priority": "must"},
                {"metric": "dd_vs_ew", "op": ">=", "target": 0.0, "priority": "try"},
            ],
        },
        {
            "id": "diversified",
            "name": "分散",
            "items": [
                {"metric": "effective_n", "op": ">=", "target": 4, "priority": "must"},
                {"metric": "beat_ew", "op": ">=", "target": 0.05, "priority": "must"},
                {"metric": "turnover", "op": "<=", "target": 0.3, "priority": "try"},
            ],
        },
    ],
    defaults={"symbols": [], "timeframe": "4h", "bars_limit": 1500, "max_rounds": 4, "horizon": 1, "min_bars": 400},
    score_key="sharpe",
)


EXECUTION = GoalDomain(
    key="execution",
    label="执行",
    metrics={
        "is_bps": {
            "label": "执行落差",
            "unit": "bps",
            "op": "<=",
            "hint": "相对到达中间价的平均成本（含手续费与没做完的机会成本）",
        },
        "is_std_bps": {"label": "成本波动", "unit": "bps", "op": "<=", "hint": "各执行片段成本的标准差：越小越可预期"},
        "is_p95_bps": {"label": "最差 5% 成本", "unit": "bps", "op": "<="},
        "beat_twap_bps": {
            "label": "比 TWAP 省",
            "unit": "bps",
            "op": ">=",
            "soft_max": 10.0,
            "hard_max": 30.0,
            "hint": "同一批片段上 TWAP 的成本 − 本方案成本",
        },
        "vs_vwap_bps": {"label": "相对区间 VWAP", "unit": "bps", "op": "<=", "hint": "成交均价比区间 VWAP 贵多少"},
        "fill_rate": {"label": "完成率", "unit": "pct", "op": ">=", "hint": "母单平均完成的比例"},
        "maker_ratio": {"label": "被动成交占比", "unit": "pct", "op": ">="},
        "impact_bps": {"label": "冲击成本", "unit": "bps", "op": "<="},
        "markout_5": {
            "label": "成交后 5 步 markout",
            "unit": "bps",
            "op": ">=",
            "hint": "成交后价格朝有利方向走了多少；负数说明被逆向选择",
        },
    },
    aliases=[
        (r"执行落差|实施落差|成本|implementation\s*shortfall|is", "is_bps"),
        (r"成本波动|is\s*_?std", "is_std_bps"),
        (r"最差\s*5%\s*成本|p95", "is_p95_bps"),
        (r"比\s*TWAP\s*省|跑赢\s*TWAP|beat\s*_?twap", "beat_twap_bps"),
        (r"相对\s*VWAP|vs\s*_?vwap|VWAP\s*偏离", "vs_vwap_bps"),
        (r"完成率|fill\s*_?rate", "fill_rate"),
        (r"被动成交占比|maker", "maker_ratio"),
        (r"冲击(?:成本)?|impact", "impact_bps"),
        (r"markout", "markout_5"),
    ],
    templates=[
        {
            "id": "cheap",
            "name": "低成本",
            "items": [
                {"metric": "beat_twap_bps", "op": ">=", "target": 0.5, "priority": "must"},
                {"metric": "fill_rate", "op": ">=", "target": 0.98, "priority": "must"},
                {"metric": "is_std_bps", "op": "<=", "target": 25.0, "priority": "try"},
            ],
        },
        {
            "id": "vwap",
            "name": "贴近 VWAP",
            "items": [
                {"metric": "vs_vwap_bps", "op": "<=", "target": 2.0, "priority": "must"},
                {"metric": "fill_rate", "op": ">=", "target": 0.98, "priority": "must"},
            ],
        },
        {
            "id": "stable",
            "name": "成本可预期",
            "items": [
                {"metric": "is_std_bps", "op": "<=", "target": 15.0, "priority": "must"},
                {"metric": "fill_rate", "op": ">=", "target": 0.99, "priority": "must"},
                {"metric": "beat_twap_bps", "op": ">=", "target": 0.0, "priority": "try"},
            ],
        },
    ],
    defaults={"symbols": [], "timeframe": "1m", "bars_limit": 3000, "max_rounds": 4, "horizon": 30, "min_bars": 600},
    score_key="score",
)


DOMAINS: dict[str, GoalDomain] = {"factor": FACTOR, "model": MODEL, "portfolio": PORTFOLIO, "execution": EXECUTION}

__all__ = ["DOMAINS", "EXECUTION", "FACTOR", "MODEL", "PORTFOLIO", "GoalDomain"]
