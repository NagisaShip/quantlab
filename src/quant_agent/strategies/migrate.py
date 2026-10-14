"""把旧版「统一赛道」的策略版本记录转换成策略文件。

旧记录在 store 的 strategies 集合里（模板参数 / JSON 规则 / Agent 生成代码三种），新平台统一用
策略文件仓库（repo.py）。转换规则（与用户确认过的映射表一致）：

- JSON 规则 → 用 `rule_to_python` 编译成等价 Python，类型 rule（规则页仍能读回）；
- 模板参数 → 选最接近的新版 SDK 模板，同名参数写回 PARAMS，其余在备注里列明；
- Agent 生成代码 → 代码原样保存为代码策略，自检问题写进备注，保持未批准。

原记录不删除，只打上 `migrated_slug`，因此重复执行是幂等的。
"""

from __future__ import annotations

from typing import Any

from ..sdk import check_source, declared_params
from ..store import Collections
from .params_util import ParamsError, rewrite_params
from .repo import StrategyRepository, rule_to_python
from .rules import RULE_TEMPLATE, RuleError, validate_rule_spec
from .sdk_templates import get_template

# 旧模板 → 新 SDK 模板（没有完全等价的就选最接近的，并在备注里说明）
TEMPLATE_MAP = {
    "ma_trend": "ma_cross",
    "mean_reversion": "bollinger",
    "grid_bounded": "grid",
    "dca_rebalance": "dca",
    "buy_hold": "buy_hold",
}


def pending(store: Any) -> list[dict[str, Any]]:
    return [r for r in store.list(Collections.STRATEGIES, limit=1000) if not r.get("migrated_slug")]


def migrate_one(store: Any, repo: StrategyRepository, rec: dict[str, Any]) -> dict[str, Any]:
    name = rec.get("name") or "旧版策略"
    symbol = rec.get("symbol") or "BTC/USDT"
    timeframe = rec.get("timeframe") or "1h"
    kind, notes = "code", [f"由旧版统一赛道策略「{name}」转换"]
    template = rec.get("template")
    if rec.get("kind") == "rule" or template == "rule":
        try:
            spec = validate_rule_spec({**RULE_TEMPLATE, **(rec.get("params") or {})})
        except RuleError as exc:
            return {"id": rec.get("id"), "name": name, "ok": False, "reason": f"规则不合法：{exc}"}
        source, kind = rule_to_python(spec), "rule"
        params: dict[str, Any] = spec.to_dict()
    elif template in TEMPLATE_MAP:
        tpl = get_template(TEMPLATE_MAP[template])
        if tpl is None:
            return {"id": rec.get("id"), "name": name, "ok": False, "reason": f"找不到对应模板 {template}"}
        source = tpl.code
        declared = declared_params(source)
        old = rec.get("params") or {}
        same = {k: v for k, v in old.items() if k in declared}
        if same:
            try:
                source = rewrite_params(source, same)
            except ParamsError:
                same = {}
        rest = sorted(set(old) - set(same))
        notes.append(f"按模板「{tpl.name}」重建" + (f"，未能对应的旧参数：{', '.join(rest)}" if rest else ""))
        params = declared_params(source)
    elif rec.get("code"):
        source = rec["code"]
        issues = check_source(source)
        if issues:
            notes.append("旧引擎代码，自检问题：" + "；".join(issues[:5]))
        params = rec.get("params") or {}
    else:
        return {"id": rec.get("id"), "name": name, "ok": False, "reason": "没有可转换的规则、模板或代码"}

    saved = repo.save(
        name=name,
        source=source,
        params=params,
        team=rec.get("team") or "我的策略",
        kind=kind,
        origin="agent" if rec.get("source") == "agent" else "user",
        symbol=symbol,
        timeframe=timeframe,
        securities=[symbol],
        notes="；".join(notes),
        slug=repo.unique_slug(name),
    )
    store.upsert(Collections.STRATEGIES, {**rec, "migrated_slug": saved.meta.slug})
    return {"id": rec.get("id"), "name": name, "ok": True, "slug": saved.meta.slug, "kind": kind}


def migrate_all(store: Any, repo: StrategyRepository) -> list[dict[str, Any]]:
    return [migrate_one(store, repo, r) for r in pending(store)]
