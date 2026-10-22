"""Annual graphs, train-only preprocessing, explicit missing-label masks."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

import networkx as nx
import numpy as np
import pandas as pd
from sklearn.preprocessing import OneHotEncoder

from .io import INSTITUTION, NAMES, num, read_frame, read_json, save_frame, save_json, save_npz

NUMERIC = [
    "age",
    "registered_capital_log",
    "insured_employees_log",
    "roa",
    "roe",
    "net_margin",
    "gross_margin",
    "asset_turnover",
    "receivables_turnover",
    "inventory_turnover",
    "current_asset_turnover",
    "debt_ratio",
    "current_ratio",
    "quick_ratio",
    "cash_ratio",
    "ocf_liabilities",
    "revenue_growth",
    "profit_growth",
    "assets_growth",
    "equity_growth",
    "credit_total",
    "credit_used_ratio",
    "credit_unused",
]
CATEGORICAL = ["industry", "province"]
CHINESE = [
    "成立年限",
    "注册资本原口径对数",
    "参保人数对数",
    "总资产收益率",
    "净资产收益率",
    "净利率",
    "毛利率",
    "总资产周转率",
    "应收账款周转率",
    "存货周转率",
    "流动资产周转率",
    "资产负债率",
    "流动比率",
    "速动比率",
    "现金比率",
    "经营现金流负债比",
    "营业收入增长率",
    "净利润增长率",
    "总资产增长率",
    "净资产增长率",
    "授信总额（未取得）",
    "授信使用率（未取得）",
    "未用授信（未取得）",
]
RELATIONS = [
    "trade_sales",
    "trade_sales_reverse",
    "trade_purchase",
    "trade_purchase_reverse",
    "capital",
    "capital_reverse",
]


def ratio(a, b):
    a, b = num(a), num(b)
    return a / b if np.isfinite(a) and np.isfinite(b) and abs(b) > 1e-8 else np.nan


def employment_upper(text):
    text = str(text)
    if "以上" in text:
        return np.inf
    values = re.findall(r"\d+", text)
    return max(map(int, values)) if values else np.nan


def business_features(row, year):
    if row is None:
        return {}
    established = str(row.get("EstablishDate", ""))
    age = year - num(established[:4]) if re.match(r"^\d{4}", established) else np.nan
    cap = num(row.get("RegisteredCapital"))
    staff = num(row.get("InsuredEmployees"))
    return {
        "age": age if 0 <= age <= 150 else np.nan,
        "registered_capital_log": np.log1p(cap) if cap >= 0 else np.nan,
        "insured_employees_log": np.log1p(staff) if staff >= 0 else np.nan,
        "industry": str(row.get("IndustryCodeA", ""))[:3] or "未知",
        "province": str(row.get("ProvinceName", "")) or "未知",
    }


def financial_features(row, previous):
    if row is None:
        return {}

    def v(key):
        return row.get(key, np.nan)

    def avg(key):
        return (num(v(key)) + num(previous.get(key, np.nan))) / 2 if previous is not None else num(v(key))

    result = {
        "roa": ratio(v("profit"), avg("assets")),
        "roe": ratio(v("profit"), avg("equity")),
        "net_margin": ratio(v("profit"), v("revenue")),
        "gross_margin": ratio(num(v("revenue")) - num(v("cost")), v("revenue")),
        "asset_turnover": ratio(v("revenue"), avg("assets")),
        "receivables_turnover": ratio(v("revenue"), avg("receivables")),
        "inventory_turnover": ratio(v("cost"), avg("inventory")),
        "current_asset_turnover": ratio(v("revenue"), avg("current_assets")),
        "debt_ratio": ratio(v("liabilities"), v("assets")),
        "current_ratio": ratio(v("current_assets"), v("current_liabilities")),
        "quick_ratio": ratio(num(v("current_assets")) - num(v("inventory")), v("current_liabilities")),
        "cash_ratio": ratio(v("cash"), v("current_liabilities")),
        "ocf_liabilities": ratio(v("ocf"), v("liabilities")),
    }
    for feature, key in [
        ("revenue_growth", "revenue"),
        ("profit_growth", "profit"),
        ("assets_growth", "assets"),
        ("equity_growth", "equity"),
    ]:
        old = num(previous.get(key, np.nan)) if previous is not None else np.nan
        result[feature] = ratio(num(v(key)) - old, abs(old))
    return result


def build_graphs(ctx, inputs, params):
    prepared = inputs["prepared"]
    financial = read_frame(ctx, prepared["financial"], dtype={"stock": str, "entity": str})
    enterprise = read_frame(ctx, prepared["enterprise"], dtype=str, keep_default_na=False)
    enterprise["year"] = enterprise.year.astype(int)
    trades = read_frame(ctx, prepared["trades"], dtype={"anchor": str, "counterparty": str})
    capital = read_frame(ctx, prepared["capital"], dtype={"source": str, "target": str})
    names = read_json(ctx, prepared["names"])
    mapping = inputs.get("alignment", {}).get("mapping", {})
    if isinstance(mapping, dict) and "file" in mapping:
        mapping = read_json(ctx, mapping)
    mapping = dict(mapping)
    alias_targets = defaultdict(set)
    for record in read_json(ctx, prepared["aliases"]):
        for alias in [record["alias"], record["canonical"]]:
            alias_targets["N:" + alias].add(record["entity"])
    ambiguous_aliases = {name for name, targets in alias_targets.items() if len(targets) > 1}
    for name in ambiguous_aliases:
        mapping.pop(name, None)

    def translate(name):
        return mapping.get(name, name)

    before_graph = nx.Graph()
    before_graph.add_edges_from((r.anchor, "RAW:" + r.name) for r in trades.itertuples())
    before_graph.add_edges_from(("RAW:" + r.name, r.target) for r in capital.itertuples())
    before_nodes = set(before_graph)
    trades["counterparty"] = trades.counterparty.map(translate)
    capital["source"] = capital.source.map(translate)
    capital["target"] = capital.target.map(translate)
    for old, new in mapping.items():
        if new not in names and old in names:
            names[new] = names[old]
    frows = {(r["stock"], int(r["year"])): r for r in financial.to_dict("records")}
    brows = {(r["entity"], int(r["year"])): r for r in enterprise.to_dict("records")}
    for stock, group in financial.groupby("stock"):
        names.setdefault("L:" + stock, group.iloc[-1].short_name)
    annual = {}
    statistics = []
    panel_rows = []
    all_entities = set()
    for year in range(2012, 2025):
        tf = trades[trades.year.eq(year)]
        cf = capital[capital.year.eq(year)]
        listing = financial[financial.year.eq(year)]
        entities = set(listing.entity) | set(tf.anchor) | set(tf.counterparty) | set(cf.source) | set(cf.target)
        # Business data without a contemporaneously observed link are retained in
        # the audit but not silently added as supply-chain target observations.
        rows = []
        for entity in sorted(entities):
            business = brows.get((entity, year))
            industry = business.get("IndustryCodeA", "") if business else ""
            features = {k: np.nan for k in NUMERIC}
            features.update({k: "未知" for k in CATEGORICAL})
            label = -1
            composite = -1
            kind = 4
            if entity.startswith("L:"):
                stock = entity[2:]
                current = frows.get((stock, year))
                if current:
                    kind = int(current["node_type"])
                    label = int(current["st_label"])
                    composite = int(current["composite_label"])
                else:
                    kind = (
                        1 if stock.startswith(("002", "003", "300", "301", "688", "689", "43", "83", "87", "92")) else 0
                    )
                features.update(financial_features(frows.get((stock, year - 1)), frows.get((stock, year - 2))))
                features.update(business_features(brows.get((entity, year - 1)), year))
            else:
                name = names.get(entity, entity.split(":", 1)[-1])
                if INSTITUTION.search(name) and "公司" not in name:
                    kind = 3
                elif (
                    business
                    and not business.get("BusinessSymbol", "")
                    and str(industry).startswith("C")
                    and employment_upper(business.get("EmployScale", "")) < 1000
                ):
                    kind = 2
                features.update(business_features(business, year))
                state = business.get("BusinessState", "") if business else ""
                if state and any(t in state for t in ["注销", "吊销", "撤销", "歇业"]):
                    label = 1
                elif state and any(
                    t in state for t in ["存续", "在业", "开业", "在营", "正常", "仍注册", "核准设立", "登记成立"]
                ):
                    label = 0
                composite = label
            # SME proxy groups only are supervised; context companies carry no loss.
            target = int(kind in (1, 2) and label >= 0)
            row = {
                "entity": entity,
                "year": year,
                "name": names.get(entity, entity.split(":", 1)[-1]),
                "node_type": kind,
                "label": label,
                "composite_label": composite,
                "target": target,
                **features,
            }
            rows.append(row)
            if target:
                panel_rows.append(
                    {
                        k: row[k]
                        for k in [
                            "entity",
                            "year",
                            "name",
                            "node_type",
                            "label",
                            "composite_label",
                            *NUMERIC,
                            *CATEGORICAL,
                        ]
                    }
                )
        table = pd.DataFrame(rows)
        index = {entity: i for i, entity in enumerate(table.entity)}
        edges = []
        base_edges = []
        for row in tf.itertuples():
            source, target = (
                (row.anchor, row.counterparty) if row.direction == "sale" else (row.counterparty, row.anchor)
            )
            if source == target:
                continue
            rel = 0 if row.direction == "sale" else 2
            edges.extend(
                [(index[source], index[target], rel, row.weight), (index[target], index[source], rel + 1, row.weight)]
            )
            base_edges.append(
                {
                    "year": year,
                    "source": source,
                    "target": target,
                    "relation": "trade",
                    "basis": row.direction,
                    "weight": row.weight,
                }
            )
        for row in cf.itertuples():
            if row.source == row.target:
                continue
            edges.extend(
                [
                    (index[row.source], index[row.target], 4, row.weight),
                    (index[row.target], index[row.source], 5, row.weight),
                ]
            )
            base_edges.append(
                {
                    "year": year,
                    "source": row.source,
                    "target": row.target,
                    "relation": "capital",
                    "basis": "ownership",
                    "weight": row.weight,
                }
            )
        g = nx.Graph()
        g.add_nodes_from(table.entity)
        g.add_edges_from((e["source"], e["target"]) for e in base_edges)
        components = list(nx.connected_components(g))
        positive = int(table.loc[table.target.eq(1), "label"].sum())
        stat = {
            "year": year,
            "nodes": len(table),
            "economic_edges": len(base_edges),
            "message_edges": len(edges),
            "largest_component": max(map(len, components), default=0),
            "components": len(components),
            "isolates": nx.number_of_isolates(g),
            "mean_degree": sum(dict(g.degree()).values()) / max(1, len(g)),
            "target_rows": int(table.target.sum()),
            "target_positive": positive,
            "node_types": {NAMES[int(k)]: int(v) for k, v in table.node_type.value_counts().items()},
            "edge_types": dict(Counter(e["relation"] for e in base_edges)),
        }
        statistics.append(stat)
        annual[year] = {
            "table": table,
            "edges": np.array(edges, dtype=np.float64).reshape(-1, 4),
            "base_edges": base_edges,
        }
        all_entities.update(entities)
        ctx.log(
            f"{year}年：{len(table)}节点、{len(base_edges)}经济边、{stat['target_rows']}监督样本、{positive}风险样本"
        )

    # Fit all transformations only on 2012-2021 observed graph inputs.
    fitting = pd.concat([annual[y]["table"] for y in range(2012, 2022)], ignore_index=True)
    raw = fitting[NUMERIC].to_numpy(dtype=float)
    low = np.array([np.nanquantile(v, 0.01) if np.isfinite(v).any() else 0 for v in raw.T])
    high = np.array([np.nanquantile(v, 0.99) if np.isfinite(v).any() else 0 for v in raw.T])
    median = np.array([np.nanmedian(v) if np.isfinite(v).any() else 0 for v in raw.T])
    clipped = np.clip(np.where(np.isfinite(raw), raw, median), low, high)
    mean = clipped.mean(0)
    scale = clipped.std(0)
    scale[scale < 1e-8] = 1
    encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=False, dtype=np.float32)
    encoder.fit(fitting[CATEGORICAL].fillna("未知"))
    feature_names = [*NUMERIC, *[k + "_missing" for k in NUMERIC], *encoder.get_feature_names_out(CATEGORICAL).tolist()]
    global_ids = {entity: i for i, entity in enumerate(sorted(all_entities))}
    graphs = {}
    for year, data in annual.items():
        table = data["table"]
        v = table[NUMERIC].to_numpy(dtype=float)
        missing = ~np.isfinite(v)
        numeric = (np.clip(np.where(~missing, v, median), low, high) - mean) / scale
        x = np.concatenate(
            [numeric, missing.astype(float), encoder.transform(table[CATEGORICAL].fillna("未知"))], axis=1
        ).astype(np.float32)
        edges = data["edges"]
        graph_ref = save_npz(
            ctx,
            "graph_" + str(year),
            x=x,
            node_type=table.node_type.to_numpy(np.int64),
            y=table.label.to_numpy(np.int64),
            y_composite=table.composite_label.to_numpy(np.int64),
            target=table.target.to_numpy(bool),
            global_id=np.array([global_ids[e] for e in table.entity], dtype=np.int64),
            entity=table.entity.to_numpy(str),
            names=table.name.to_numpy(str),
            edge_index=edges[:, :2].T.astype(np.int64),
            edge_type=edges[:, 2].astype(np.int64),
            edge_weight=edges[:, 3].astype(np.float32),
        )
        graphs[str(year)] = graph_ref
        data["node_ref"] = save_frame(ctx, table, "nodes_" + str(year))
        data["edge_ref"] = save_frame(ctx, pd.DataFrame(data["base_edges"]), "edges_" + str(year))
        if year == 2023:
            from scipy.sparse import coo_matrix
            from scipy.sparse import save_npz as sparse_save

            path = __import__("pathlib").Path(ctx.work) / "adjacency_2023.npz"
            sparse_save(
                path,
                coo_matrix(
                    (edges[:, 3], (edges[:, 0].astype(int), edges[:, 1].astype(int))), shape=(len(table), len(table))
                ).tocsr(),
            )
            adjacency = ctx.save_file(path)
    after_graph = nx.Graph()
    for data in annual.values():
        after_graph.add_edges_from((e["source"], e["target"]) for e in data["base_edges"])
    alignment_stats = {
        "before_nodes": len(before_nodes),
        "after_nodes": len(after_graph),
        "before_largest_component": max(map(len, nx.connected_components(before_graph)), default=0),
        "after_largest_component": max(map(len, nx.connected_components(after_graph)), default=0),
        "ambiguous_aliases_not_merged": len(ambiguous_aliases),
        "scope": "union of observed named transaction/corporate-capital edges,2012-2024; before=raw counterparty/account names, after=stable IDs and verified aliases; anonymous records excluded before both counts; improvements not attributed solely to LoRA",
    }
    panel = save_frame(ctx, pd.DataFrame(panel_rows), "supervised_panel")
    preprocessing = save_json(
        ctx,
        {
            "fitted_years": [2012, 2021],
            "numeric": NUMERIC,
            "categorical": CATEGORICAL,
            "median": median.tolist(),
            "q01": low.tolist(),
            "q99": high.tolist(),
            "mean": mean.tolist(),
            "scale": scale.tolist(),
            "categories": [c.tolist() for c in encoder.categories_],
            "feature_names": feature_names,
            "bank_credit_observed": False,
            "registered_capital_unit": "source scale unverified; no CNY conversion asserted",
        },
        "preprocessing.json",
    )
    dataset = {
        "graphs": graphs,
        "feature_names": feature_names,
        "base_feature_names": NUMERIC + CATEGORICAL,
        "feature_chinese": dict(zip(NUMERIC, CHINESE, strict=True)),
        "relations": RELATIONS,
        "node_types": NAMES,
        "global_nodes": len(global_ids),
        "panel": panel,
        "preprocessing": preprocessing,
        "annual_statistics": statistics,
        "alignment_statistics": alignment_stats,
        "nodes": {str(y): d["node_ref"] for y, d in annual.items()},
        "edges": {str(y): d["edge_ref"] for y, d in annual.items()},
        "adjacency_2023": adjacency,
        "prepared": prepared,
        "protocol": {
            "train": [2012, 2021],
            "validation": [2022, 2023],
            "test": [2024, 2024],
            "main_label": "year-end ST (listed), observed registration-state proxy (unlisted)",
            "listed_sme": "board-based proxy following assignment example; not statutory SME certification",
            "unlisted_sme": "manufacturing, disclosed employment-scale upper bound below1000; not statutory revenue/headcount verification",
            "financial_lag_years": 1,
            "point_in_time_guaranteed": False,
            "unknown_context_type": 4,
        },
    }
    return {
        "dataset": dataset,
        "statistics": {
            "annual": statistics,
            "alignment": alignment_stats,
            "features": len(feature_names),
            "target_panel_rows": len(panel_rows),
        },
    }
