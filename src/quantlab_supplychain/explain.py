from __future__ import annotations

from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

from .evaluate import plotting
from .io import checked_path, load_npz, read_frame, save_frame, save_json
from .train import to_torch


def explain(ctx, inputs, params):
    import torch

    from .models import make

    dataset = inputs["dataset"]
    trained = inputs["model"]
    first = next(c for c in trained["checkpoints"] if c["seed"] == 7)
    checkpoint = torch.load(checked_path(ctx, first["checkpoint"]), map_location="cpu", weights_only=False)
    if checkpoint["kind"] != "HGNNs":
        raise ValueError("解释器需要HGNNs检查点")
    raw = load_npz(ctx, dataset["graphs"]["2024"])
    device = "cuda" if torch.cuda.is_available() else "cpu"
    g = to_torch({2024: raw}, device)[2024]
    model = make("HGNNs", checkpoint["inputs"], checkpoint["hidden"]).to(device)
    model.load_state_dict(checkpoint["state"])
    model.eval()
    g["x"] = g["x"].clone().detach().requires_grad_(True)
    logits, h, messages, relation_attention, dimension = model(g, explain=True, return_messages=True)
    probability = torch.sigmoid(logits).detach().cpu().numpy()
    target = raw["target"]
    cutoff = float(checkpoint["threshold"])
    selected = np.flatnonzero(target & (probability >= cutoff))
    if not len(selected):
        selected = np.flatnonzero(target)[np.argsort(probability[target])[-10:]]
    logits[selected].mean().backward()
    alpha = h.grad[selected].mean(0)
    cam = torch.relu((h * alpha).sum(1)).detach().cpu().numpy()
    attribution = (g["x"].grad * g["x"]).detach().abs().cpu().numpy()[selected].mean(0)
    features = pd.DataFrame({"feature": dataset["feature_names"], "gradient_x_input": attribution}).sort_values(
        "gradient_x_input", ascending=False
    )
    features["description"] = features.feature.map(lambda name: dataset.get("feature_chinese", {}).get(name, name))
    latent = pd.DataFrame({"channel": np.arange(len(alpha)), "gradcam_weight": alpha.detach().cpu().numpy()})
    candidates = []
    for label, condition in [
        ("真阳性", target & (raw["y"] == 1) & (probability >= cutoff)),
        ("漏判", target & (raw["y"] == 1) & (probability < cutoff)),
        ("误报", target & (raw["y"] == 0) & (probability >= cutoff)),
    ]:
        ids = np.flatnonzero(condition)
        if len(ids):
            chosen = int(ids[np.argmax(probability[ids])])
            candidates.append((label, chosen))
    training_weights = {rel: [] for rel in range(6)}
    for year in range(2012, 2022):
        training = load_npz(ctx, dataset["graphs"][str(year)])
        for rel in range(6):
            training_weights[rel].extend(training["edge_weight"][training["edge_type"] == rel].tolist())
    cuts = {
        rel: np.quantile(values, [1 / 3, 2 / 3]).tolist() if values else [0, 1]
        for rel, values in training_weights.items()
    }
    by_target = {}
    for source, destination, message, rel in messages:
        effective = message * relation_attention[destination, rel, None] * dimension[destination]
        norms = effective.detach().norm(dim=1).cpu().numpy()
        weights = raw["edge_weight"][raw["edge_type"] == rel]
        for a, b, value, weight in zip(source.cpu().numpy(), destination.cpu().numpy(), norms, weights, strict=True):
            tier = "低" if weight <= cuts[rel][0] else "中" if weight <= cuts[rel][1] else "高"
            by_target.setdefault(int(b), []).append(
                {
                    "source_index": int(a),
                    "target_index": int(b),
                    "relation": dataset["relations"][rel],
                    "message_norm": float(value),
                    "weight": float(weight),
                    "weight_tier": tier,
                }
            )
    cases = []
    paths = []
    for category, index in candidates:
        neighbors = sorted(by_target.get(index, []), key=lambda e: e["message_norm"], reverse=True)[:8]
        case = {
            "category": category,
            "entity": str(raw["entity"][index]),
            "name": str(raw["names"][index]),
            "label": int(raw["y"][index]),
            "probability": float(probability[index]),
            "threshold": cutoff,
            "gradcam": float(cam[index]),
            "neighbors": [],
        }
        for edge in neighbors:
            a = edge["source_index"]
            entry = {
                **edge,
                "source": str(raw["entity"][a]),
                "source_name": str(raw["names"][a]),
                "source_label": int(raw["y"][a]) if raw["y"][a] >= 0 else None,
            }
            if len(case["neighbors"]) < 3:
                keep = ~((g["edge_index"][0] == a) & (g["edge_index"][1] == index))
                altered = {
                    **g,
                    "edge_index": g["edge_index"][:, keep],
                    "edge_type": g["edge_type"][keep],
                    "edge_weight": g["edge_weight"][keep],
                }
                with torch.inference_mode():
                    score, _ = model(altered)
                    without = float(torch.sigmoid(score[index]))
                entry["score_without_direct_messages"] = without
                entry["score_change_when_removed"] = without - float(probability[index])
            case["neighbors"].append(entry)
            preceding = sorted(
                [e for e in by_target.get(a, []) if e["source_index"] != index],
                key=lambda e: e["message_norm"],
                reverse=True,
            )[:1]
            if preceding:
                start = preceding[0]["source_index"]
                paths.append(
                    {
                        "target": case["entity"],
                        "path": [str(raw["entity"][start]), str(raw["entity"][a]), case["entity"]],
                        "names": [str(raw["names"][start]), str(raw["names"][a]), case["name"]],
                        "message_norms": [preceding[0]["message_norm"], edge["message_norm"]],
                        "relations": [preceding[0]["relation"], edge["relation"]],
                        "weight_tiers": [preceding[0]["weight_tier"], edge["weight_tier"]],
                        "interpretation": "two-edge candidate network chain assembled from local message strengths; this HGNN encoder has one message-passing layer, so only the direct neighbor is part of the target computational receptive field; reverse edges are auxiliary directions; not causal contagion evidence",
                    }
                )
        cases.append(case)
    plt = plotting()
    figures = {}
    fig, ax = plt.subplots(figsize=(10, 5.7))
    top = features.head(12).iloc[::-1]
    ax.barh(top.description, top.gradient_x_input, color="#176c68")
    ax.set(xlabel="平均 |梯度 × 标准化输入|", title="HGNNs · 2024高分监督节点的特征敏感性")
    fig.tight_layout()
    path = Path(ctx.work) / "feature_sensitivity.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    figures["feature_sensitivity"] = ctx.save_file(path)
    nodes = read_frame(ctx, dataset["nodes"]["2023"], dtype={"entity": str})
    edges = read_frame(ctx, dataset["edges"]["2023"], dtype={"source": str, "target": str})
    graph = nx.Graph()
    graph.add_nodes_from(nodes.entity)
    graph.add_edges_from(zip(edges.source, edges.target, strict=True))
    central = sorted(graph, key=lambda n: graph.degree(n), reverse=True)[:30]
    selected_nodes = set(central)
    for n in central:
        selected_nodes.update(sorted(graph[n], key=lambda v: graph.degree(v), reverse=True)[:5])
    selected_nodes = sorted(selected_nodes, key=lambda n: graph.degree(n), reverse=True)[:160]
    local = graph.subgraph(selected_nodes)
    position = nx.spring_layout(local, seed=7, k=0.45, iterations=100)
    types = nodes.set_index("entity").node_type.to_dict()
    names = nodes.set_index("entity").name.to_dict()
    palette = ["#394f68", "#176c68", "#d0894d", "#994a64", "#bbc3ca"]
    fig, ax = plt.subplots(figsize=(11, 7.0))
    nx.draw_networkx_edges(local, position, ax=ax, width=0.6, alpha=0.32, edge_color="#79838b")
    for kind in range(5):
        group = [n for n in local if types.get(n, 4) == kind]
        nx.draw_networkx_nodes(
            local,
            position,
            nodelist=group,
            ax=ax,
            node_size=[20 + min(300, graph.degree(n) * 3) for n in group],
            node_color=palette[kind],
            label=dataset["node_types"][kind],
            alpha=0.9,
        )
    nx.draw_networkx_labels(
        local,
        position,
        labels={n: str(names.get(n, n))[:12] for n in central[:5] if n in local},
        font_size=8,
        font_family=plt.rcParams["font.family"][0],
        ax=ax,
    )
    ax.set_title("2023年度真实网络局部 · 高度节点与相邻主体（最多160节点）")
    ax.axis("off")
    ax.legend(fontsize=9, loc="upper left")
    fig.tight_layout()
    path = Path(ctx.work) / "network_2023_local.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    figures["network_2023_local"] = ctx.save_file(path)
    degree = np.array([d for _, d in graph.degree()])
    fig, ax = plt.subplots(figsize=(9, 4.5))
    values, counts = np.unique(degree, return_counts=True)
    ax.scatter(values[values > 0], counts[values > 0], s=17, color="#176c68")
    ax.set(
        xscale="log",
        yscale="log",
        xlabel="度（对数）",
        ylabel="节点数（对数）",
        title="2023年度全网络度分布；孤立节点单独计数",
    )
    fig.tight_layout()
    path = Path(ctx.work) / "degree_distribution.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    figures["degree_distribution"] = ctx.save_file(path)
    result = {
        "model": "HGNNs",
        "seed": 7,
        "feature_method": "absolute gradient x input on standardized financial/business/missingness inputs; sensitivity not causal marginal effect",
        "gradcam_method": "mean target-score gradient at fused latent layer, weighted activation and ReLU; channel/node salience, not direct raw-financial-feature causal contribution",
        "message_method": "L2 norm of actual transformed, edge-normalized message multiplied by learned relation/channel attention; top3 direct neighbors additionally tested by deleting their incoming messages and recomputing score",
        "edge_weight_tiers": {
            "fitted_years": [2012, 2021],
            "quantiles": [1 / 3, 2 / 3],
            "cuts": {dataset["relations"][k]: v for k, v in cuts.items()},
        },
        "feature_importance": features.head(25).to_dict("records"),
        "features_file": save_frame(ctx, features, "feature_sensitivity"),
        "latent_gradcam_file": save_frame(ctx, latent, "gradcam_latent_channels"),
        "cases": cases,
        "paths": paths[:12],
        "figures": figures,
        "network_visual_scope": {
            "year": 2023,
            "full_nodes": len(graph),
            "full_edges_simple_undirected": graph.number_of_edges(),
            "shown_nodes": len(local),
            "isolates": int((degree == 0).sum()),
            "selection": "degree-top30 plus up to5 neighbors each; capped160;not a full-network drawing",
        },
    }
    result["evidence_file"] = save_json(ctx, result, "explanation_evidence.json")
    return {"explanation": result}
