"""Explicit snapshots bridge existing libraries without hidden platform state."""

from __future__ import annotations

import copy
import pprint

from .contracts import WorkflowError, fingerprint

COLLECTIONS = {"factor": "factors", "model": "ml_models", "portfolio": "pf_schemes", "execution": "ex_schemes"}
PAGES = {
    "factor": "factors",
    "model": "modellib",
    "portfolio": "portfolio",
    "execution": "execution",
    "strategy": "strategies",
}


def listing(service):
    rows = []
    for domain, collection in COLLECTIONS.items():
        for item in service.store.list(collection):
            rows.append(
                {
                    "domain": domain,
                    "id": item["id"],
                    "name": item.get("name", item["id"]),
                    "status": item.get("status", ""),
                    "page": PAGES[domain],
                    "mode": "snapshot" if domain == "model" else "executable",
                }
            )
    from ..strategies.repo import StrategyRepository

    for item in StrategyRepository(service.store.root / "strategies").list():
        rows.append(
            {
                "domain": "strategy",
                "id": item.meta.slug,
                "name": item.meta.name,
                "status": item.meta.status,
                "page": "editor/" + item.meta.slug,
                "mode": "snapshot",
            }
        )
    return rows


def snapshot(service, domain, asset_id, *, origin="gui"):
    if domain not in PAGES:
        raise WorkflowError("不支持的研究资产类别")
    if domain in ("factor", "model", "portfolio", "execution"):
        from .plugins import require

        require(service, domain, agent=origin == "agent")
    if domain == "strategy":
        from ..strategies.repo import StrategyRepository

        item = StrategyRepository(service.store.root / "strategies").get(asset_id)
        if item is None:
            raise WorkflowError("当前工作区没有该策略")
        record = {"meta": item.meta.to_dict(), "source": item.source, "params": item.params}
        name = item.meta.name
    else:
        record = service.store.get(COLLECTIONS[domain], asset_id)
        if record is None:
            raise WorkflowError("当前工作区没有该研究资产")
        name = record.get("name", asset_id)
    origin = {"domain": domain, "id": asset_id, "sha256": fingerprint(record), "snapshot": record}
    if domain in ("factor", "portfolio", "execution"):
        kind = "module.native_" + domain
        spec = copy.deepcopy(service.registry().get(kind, "1"))
        spec = {k: v for k, v in spec.items() if k not in ("code", "code_hash", "builtin")}
        defaults = (
            {"expression": record.get("expression") or record.get("expr")}
            if domain == "factor"
            else {"config": record.get("config", {})}
        )
        if any(v is None for v in defaults.values()):
            raise WorkflowError("资产缺少可执行定义")
        for key, value in defaults.items():
            spec["parameters"]["properties"][key]["default"] = value
        if "timeframe" in spec["parameters"]["properties"] and record.get("timeframe"):
            spec["parameters"]["properties"]["timeframe"]["default"] = record["timeframe"]
        body = "    return ctx.run_subflow(inputs, params)\n"
    else:
        # A native strategy callback or native model state has a different execution
        # contract. Preserve it faithfully; do not silently translate it into sklearn.
        spec = {
            "version": "1",
            "inputs": {},
            "outputs": {"asset": {"type": domain}},
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            "requirements": [],
            "description": "原生资产定义快照；保留原执行协议，接自定义适配器或在专业页执行，不会自动转换算法。",
        }
        body = '    return {"asset": NODE["asset"]["snapshot"]}\n'
    spec.update(
        kind="user.asset_" + fingerprint({"domain": domain, "id": asset_id, "sha256": origin["sha256"]})[:12],
        version="1",
        title=name + " · " + ("资产快照" if domain in ("strategy", "model") else "研究模块"),
        category="已有研究资产",
        level="module",
        asset=origin,
    )
    code = (
        "NODE = " + pprint.pformat(spec, sort_dicts=False, width=110) + "\n\ndef execute(ctx, inputs, params):\n" + body
    )
    return service.publish_card(code, origin=origin)
