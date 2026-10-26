"""Machine-readable research semantics and eligibility for enabling saved assets."""

from __future__ import annotations

import math
import re
from typing import Any

METRIC_CONTRACT = {
    "ls_return": {"period": "annualized", "costs": "included", "unit": "fraction"},
    "ls_total_return": {"period": "cumulative", "costs": "included", "unit": "fraction"},
    "ls_sharpe": {"period": "annualized", "costs": "included"},
    "turnover": {"definition": "half_L1_weight_change", "cost_basis": "full_L1_weight_change"},
    "admitted": {"meaning": "research_checks_passed", "live_approval": False},
}

RESEARCH_INTERPRETATION = """研究解释约束：
- admitted 只表示研究检查通过，不代表可交易或实盘批准；启用还要通过 activation_eligible。
- 因子/模型 ls_return 是已经扣除 cost_bps 的年化收益，绝不是累计毛收益，禁止再次扣费。
  ls_total_return 才是累计费后收益；turnover=权重变化L1的一半，计费按完整L1。
- 基线必须逐次读取 baseline 字段：ridge 的基线是特征等权合成，其他收益预测学习器的基线是 ridge。
  不同特征/标的/标签会改变基线，禁止混作同一实验或据少量失败断言“结构性不可能”。
- 1/sqrt(n-1) 仅是零假设下秩相关标准差的近似，不是备择假设下的噪声下界，
  不能据此宣称某ICIR不可达，不能据此分解不可消除方差；未验证的因果解释必须标为假设。
- 用户要求比较和归档时，完成指定实验并如实报告未达标即可；不要追逐默认目标追加调参。
- 不得自行扩大币池、改变数据区间/周期/标签或启用候选。save_work 默认仅保存候选。
"""


def activation_error(report: dict[str, Any] | None) -> str:
    if not report or report.get("admitted") is not True:
        return "研究检查未全部通过或缺少评估报告，请先评估并保存为候选"
    if (report.get("panel") or {}).get("synthetic") or report.get("synthetic"):
        return "合成数据只能用于研究演示，不能启用资产"
    if report.get("project_goal_passed") is False:
        return "项目必达目标尚未通过，请保存为候选"
    s = report.get("summary") or {}
    if s.get("mode") != "volatility":
        value = s.get("ls_return") if "ls_return" in s else s.get("ann_return")
        try:
            positive = value is not None and math.isfinite(float(value)) and float(value) > 0
        except (TypeError, ValueError):
            positive = False
        if not positive:
            return "费后收益未为正或缺少收益证据，请保存为候选"
    failed = [
        c.get("label", c.get("key", ""))
        for c in report.get("checks") or []
        if c.get("key") in {"significance", "drift"} and not c.get("ok")
    ]
    return f"启用检查未通过：{'、'.join(failed)}" if failed else ""


def annotate_report(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") or {}
    if "ls_return" in summary and "ls_total_return" not in summary and report.get("ls_nav"):
        summary["ls_total_return"] = report["ls_nav"][-1]["nav"] - 1
    if report.get("baseline"):
        summary["baseline_name"] = report["baseline"]
    report["metric_contract"] = (
        {
            "ann_return": {"period": "annualized", "costs": "included", "unit": "fraction"},
            "turnover": {"definition": "full_L1_weight_change", "cost_basis": "full_L1_weight_change"},
            "effective_n": {"definition": "gross_exposure_squared_over_sum_squared_weights", "cash": "excluded"},
            "admitted": METRIC_CONTRACT["admitted"],
        }
        if summary.get("optimizer")
        else METRIC_CONTRACT
    )
    report["activation_block_reason"] = activation_error(report)
    report["activation_eligible"] = not report["activation_block_reason"]
    return report


def latest_user_text(lab: Any, pid: str) -> str:
    return next((m.get("content", "") for m in reversed(lab.store.messages(pid)) if m.get("role") == "user"), "")


def guard_scope(project: dict[str, Any], spec: dict[str, Any], user_text: str = "") -> str:
    """Model tools cannot silently replace an established research protocol."""
    keys = {
        "symbols": "securities",
        "timeframe": "timeframe",
        "start": "start",
        "end": "end",
        "bars_limit": "bars_limit",
        "horizon": "horizon",
    }
    for field, stored in keys.items():
        if field in spec and spec[field] != project.get(stored):
            text = re.sub(r"\s+", "", user_text).lower().replace("小时", "h").replace("分钟", "m")
            values = spec[field] if field == "symbols" else [spec[field]]
            # Explicit user-supplied values authorize the model tool; unmentioned expansions do not.
            if values and all(value is not None and str(value).lower() in text for value in values):
                continue
            return "研究范围已固定；请通过项目设置修改范围或另建实验，不能为追逐目标自行更换样本"
    return ""
