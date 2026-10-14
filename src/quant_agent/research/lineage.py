"""血缘图：从任一策略结果倒查它用了哪些数据、因子、模型、组合方案和执行方案。

节点按层排：信息源 → 因子 → 模型 → 组合方案 / 执行方案 → 策略 → 回测。
边来自成果本身记录的引用：模型的特征表达式、组合方案的预期收益来源（model:名 / factor:名）、
策略源码里的 factor() / predict() / optimize() / set_execution() 调用、回测记录的策略 slug。

"需要重跑"：上游成果在下游最近一次评估之后改过（时间戳更晚）。只做提示，不自动重跑。
"""

from __future__ import annotations

from typing import Any

LAYERS = ["data", "factor", "model", "portfolio", "execution", "strategy", "backtest"]
LAYER_LABELS = {
    "data": "信息源",
    "factor": "因子",
    "model": "模型",
    "portfolio": "组合方案",
    "execution": "执行方案",
    "strategy": "策略",
    "backtest": "回测",
}


def _sec(t: Any) -> float:
    try:
        v = float(t or 0)
    except (TypeError, ValueError):
        return 0.0
    return v / 1000 if v > 1e11 else v


def _norm(expr: str) -> str:
    from .factor_expr import FactorExprError, normalize

    try:
        return normalize(expr)
    except (FactorExprError, Exception):
        return " ".join(str(expr).split())


def _fields(expr: str) -> list[str]:
    from .factor_expr import FactorExprError, fields_used, parse

    try:
        return sorted(fields_used(parse(expr)))
    except (FactorExprError, Exception):
        return []


def build(
    store: Any,
    strategies: list[dict[str, Any]],
    *,
    field_labels: dict[str, str] | None = None,
    backtests_per_strategy: int = 1,
) -> dict[str, Any]:
    """strategies: [{slug, name, source, updated_at, status}]（由 API 层从策略仓库读出来）。"""
    from .signals import scan

    labels = field_labels or {}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, str]] = []

    def node(nid: str, layer: str, label: str, **kw: Any) -> str:
        if nid not in nodes:
            nodes[nid] = {"id": nid, "layer": layer, "label": label, **kw}
        return nid

    def edge(a: str, b: str, how: str = "") -> None:
        if a in nodes and b in nodes and not any(e["from"] == a and e["to"] == b for e in edges):
            edges.append({"from": a, "to": b, "how": how})

    def data_nodes(expr: str) -> list[str]:
        out = []
        for f in _fields(expr):
            if f in ("open", "high", "low", "close", "volume", "vwap", "returns", "ret"):
                out.append(node("data:kline", "data", "K 线行情"))
            else:
                out.append(node(f"data:{f}", "data", labels.get(f, f), field=f))
        return out

    factors = store.list("factors")
    by_expr: dict[str, str] = {}
    by_fname: dict[str, str] = {}
    for f in factors:
        nid = node(
            f"factor:{f['id']}",
            "factor",
            f.get("name") or f["id"],
            ref=f["id"],
            status=f.get("status"),
            updated_at=_sec(f.get("updated_at") or f.get("created_at")),
            route=f"factors/{f['id']}",
            detail=f.get("expression", "")[:80],
        )
        by_expr[_norm(f.get("expression", ""))] = nid
        by_fname[f.get("name") or ""] = by_fname[f["id"]] = nid
        for d in data_nodes(f.get("expression", "")):
            edge(d, nid, "字段")

    models = store.list("ml_models")
    by_mname: dict[str, str] = {}
    for m in models:
        spec = m.get("spec") or {}
        nid = node(
            f"model:{m['id']}",
            "model",
            m.get("name") or m["id"],
            ref=m["id"],
            status=m.get("status"),
            updated_at=_sec(m.get("updated_at")),
            route=f"modellib/{m['id']}",
            detail=f"{spec.get('model')} → {spec.get('label')}",
        )
        by_mname[m.get("name") or ""] = by_mname[m["id"]] = nid
        for feat in spec.get("features") or []:
            expr = feat.get("expression", "")
            src = by_expr.get(_norm(expr)) or by_fname.get(feat.get("name") or "")
            if src:
                edge(src, nid, "特征")
            else:
                for d in data_nodes(expr):
                    edge(d, nid, "特征")

    by_pname: dict[str, str] = {}
    for p in store.list("pf_schemes"):
        cfg = p.get("config") or {}
        nid = node(
            f"portfolio:{p['id']}",
            "portfolio",
            p.get("name") or p["id"],
            ref=p["id"],
            status=p.get("status"),
            updated_at=_sec(p.get("updated_at")),
            route=f"portfolio/{p['id']}",
            detail=f"{cfg.get('optimizer')} · {cfg.get('risk_model')}",
        )
        by_pname[p.get("name") or ""] = by_pname[p["id"]] = nid
        node("data:kline", "data", "K 线行情")
        edge("data:kline", nid, "收益与协方差")
        exp = str(cfg.get("expected") or "")
        if exp.startswith("model:"):
            src = by_mname.get(exp[6:])
            if src:
                edge(src, nid, "预期收益")
        elif exp.startswith("factor:"):
            src = by_fname.get(exp[7:]) or by_expr.get(_norm(exp[7:]))
            if src:
                edge(src, nid, "预期收益")

    by_ename: dict[str, str] = {}
    for e in store.list("ex_schemes"):
        cfg = e.get("config") or {}
        nid = node(
            f"execution:{e['id']}",
            "execution",
            e.get("name") or e["id"],
            ref=e["id"],
            status=e.get("status"),
            updated_at=_sec(e.get("updated_at")),
            route=f"execution/{e['id']}",
            detail=str(cfg.get("algo") or ""),
        )
        by_ename[e.get("name") or ""] = by_ename[e["id"]] = nid

    bts = sorted(store.list("backtests"), key=lambda r: -(r.get("created_at") or 0))
    for s in strategies:
        sid = node(
            f"strategy:{s['slug']}",
            "strategy",
            s.get("name") or s["slug"],
            ref=s["slug"],
            status=s.get("status"),
            updated_at=_sec(s.get("updated_at")),
            route=f"editor/{s['slug']}",
        )
        for kind, name in scan(s.get("source") or ""):
            src = None
            if kind == "factor":
                src = by_fname.get(name) or by_expr.get(_norm(name))
                if src is None:
                    for d in data_nodes(name):
                        edge(d, sid, "表达式")
            elif kind == "predict":
                src = by_mname.get(name)
            elif kind == "optimize":
                src = by_pname.get(name)
            elif kind == "set_execution":
                src = by_ename.get(name)
            if src:
                edge(src, sid, f"{kind}()")
            else:
                nodes[sid].setdefault("missing", []).append(f"{kind}('{name}')")
        if not any(e["to"] == sid for e in edges):
            node("data:kline", "data", "K 线行情")
            edge("data:kline", sid, "行情")
        mine = [b for b in bts if b.get("slug") == s["slug"]][:backtests_per_strategy]
        for b in mine:
            m = b.get("metrics") or {}
            bid = node(
                f"backtest:{b['id']}",
                "backtest",
                b.get("version") or b["id"],
                ref=b["id"],
                updated_at=_sec(b.get("created_at")),
                route=f"backtest/{b['id']}",
                detail=f"夏普 {m.get('sharpe'):.2f}" if isinstance(m.get("sharpe"), (int, float)) else "",
            )
            edge(sid, bid, "回测")

    # 需要重跑：沿边向上找，任一祖先的更新时间晚于这个节点
    parents: dict[str, list[str]] = {}
    for e in edges:
        parents.setdefault(e["to"], []).append(e["from"])

    def ancestors(nid: str, seen: set[str]) -> set[str]:
        for p in parents.get(nid, []):
            if p not in seen:
                seen.add(p)
                ancestors(p, seen)
        return seen

    stale: list[dict[str, Any]] = []
    for nid, n in nodes.items():
        t = n.get("updated_at") or 0
        if n["layer"] == "data" or not t:
            continue
        newer = [nodes[a] for a in ancestors(nid, set()) if (nodes[a].get("updated_at") or 0) > t + 1]
        if newer:
            n["stale"] = True
            stale.append(
                {
                    "id": nid,
                    "label": n["label"],
                    "layer": n["layer"],
                    "because": [{"id": x["id"], "label": x["label"], "layer": x["layer"]} for x in newer[:4]],
                }
            )
    order = {k: i for i, k in enumerate(LAYERS)}
    return {
        "nodes": sorted(nodes.values(), key=lambda n: (order[n["layer"]], n["label"])),
        "edges": edges,
        "stale": stale,
        "layers": [{"id": k, "label": LAYER_LABELS[k]} for k in LAYERS],
    }


def trace(graph: dict[str, Any], nid: str) -> dict[str, list[str]]:
    """一个节点的上游与下游。"""
    up: dict[str, list[str]] = {}
    down: dict[str, list[str]] = {}
    for e in graph["edges"]:
        up.setdefault(e["to"], []).append(e["from"])
        down.setdefault(e["from"], []).append(e["to"])

    def walk(start: str, g: dict[str, list[str]]) -> list[str]:
        seen: list[str] = []
        stack = [start]
        while stack:
            x = stack.pop()
            for y in g.get(x, []):
                if y not in seen:
                    seen.append(y)
                    stack.append(y)
        return seen

    return {"upstream": walk(nid, up), "downstream": walk(nid, down)}


__all__ = ["LAYERS", "LAYER_LABELS", "build", "trace"]
