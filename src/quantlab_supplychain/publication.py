"""Print-readable figures derived from frozen predictions, never model refitting."""

from pathlib import Path

import networkx as nx
import pandas as pd

from .evaluate import plotting
from .io import read_frame


def publication_figures(ctx, inputs, params):
    from sklearn.metrics import precision_recall_curve, roc_curve

    dataset = inputs["dataset"]
    evaluation = inputs["evaluation"]
    explanation = inputs["explanation"]
    plt = plotting()
    figures = {}

    def save(fig, name):
        fig.tight_layout()
        path = Path(ctx.work) / (name + ".png")
        fig.savefig(path, bbox_inches="tight", dpi=240)
        plt.close(fig)
        figures[name] = ctx.save_file(path)

    main = evaluation["main_models"]
    summary = pd.DataFrame(evaluation["summary"])
    overall = summary[summary.subgroup.eq("全部")].set_index("model").loc[main]
    colors = ["#34495e", "#cd783f", "#8494a7", "#438f9b", "#176c68", "#bc4a43"]
    for metric, label in [("auc", "ROC-AUC"), ("f1", "F1")]:
        fig, ax = plt.subplots(figsize=(6.5, 4.2))
        ax.bar(range(6), overall[metric + "_mean"], yerr=overall[metric + "_std"].fillna(0), color=colors, capsize=3)
        ax.set_xticks(range(6), main, rotation=20)
        ax.set(ylabel=label, ylim=(0, 1.04), title="2024 测试集：三个种子的均值和标准差")
        for i, v in enumerate(overall[metric + "_mean"]):
            ax.text(i, v + 0.035, f"{v:.3f}", ha="center", fontsize=10)
        save(fig, "print_" + metric)
    predictions = read_frame(ctx, evaluation["predictions"], dtype={"entity": str})
    for mode in ["roc", "pr"]:
        fig, ax = plt.subplots(figsize=(6.5, 4.7))
        for name, color in zip(main, colors, strict=True):
            test = (
                predictions[(predictions.year.eq(2024)) & predictions.model.eq(name)]
                .groupby(["entity", "label"], as_index=False)
                .probability.mean()
            )
            if mode == "roc":
                x, y, _ = roc_curve(test.label, test.probability)
                metric = "auc"
            else:
                y, x, _ = precision_recall_curve(test.label, test.probability)
                metric = "pr_auc"
            value = next(
                r[metric] for r in evaluation["ensemble_metrics"] if r["model"] == name and r["subgroup"] == "全部"
            )
            ax.plot(x, y, label=f"{name} {value:.3f}", color=color, linewidth=1.6)
        if mode == "roc":
            ax.plot([0, 1], [0, 1], "--", color="#aaaaaa", linewidth=1)
        ax.set(
            xlabel="假阳性率" if mode == "roc" else "召回率",
            ylabel="真阳性率" if mode == "roc" else "精确率",
            title="2024 测试集 " + mode.upper() + " 曲线（三种子均分）",
        )
        ax.legend(fontsize=9, loc="lower right" if mode == "roc" else "upper right")
        save(fig, "print_" + mode)
    names = ["HGNNs", "HGNNs / no_trade", "HGNNs / no_capital", "T-HGNNs", "T-HGNNs / no_temporal"]
    ablation = summary[summary.subgroup.eq("全部")].set_index("model").reindex(names)
    fig, ax = plt.subplots(figsize=(6.5, 4.7))
    ax.barh(
        range(5),
        ablation.auc_mean,
        xerr=ablation.auc_std.fillna(0),
        color=["#176c68", "#95a3aa", "#cf9b70", "#bc4a43", "#bfa3a0"],
        capsize=3,
    )
    ax.set_yticks(range(5), ["HGNNs", "去交易", "去资本", "T-HGNNs三年", "T-HGNNs单步"])
    ax.invert_yaxis()
    ax.set(xlabel="ROC-AUC", xlim=(0, 1), title="关系与历史信息消融（三个种子）")
    save(fig, "print_ablation")
    annual = pd.DataFrame(dataset["annual_statistics"])
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    ax.plot(annual.year, annual.nodes / 1000, "o-", color="#176c68", label="节点")
    ax.plot(annual.year, annual.economic_edges / 1000, "s-", color="#cd783f", label="经济边")
    ax.set(xlabel="统计年度", ylabel="千", title="年度网络规模")
    ax.legend()
    save(fig, "print_coverage")
    features = pd.DataFrame(explanation["feature_importance"]).head(10).iloc[::-1]
    fig, ax = plt.subplots(figsize=(6.5, 5.2))
    ax.barh(features.description, features.gradient_x_input, color="#176c68")
    ax.set(xlabel="平均 |梯度 × 标准化输入|", title="HGNNs高分节点的输入敏感性")
    save(fig, "print_features")
    nodes = read_frame(ctx, dataset["nodes"]["2023"], dtype={"entity": str})
    edges = read_frame(ctx, dataset["edges"]["2023"], dtype={"source": str, "target": str})
    graph = nx.Graph()
    graph.add_nodes_from(nodes.entity)
    graph.add_edges_from(zip(edges.source, edges.target, strict=True))
    central = sorted(graph, key=lambda n: graph.degree(n), reverse=True)[:30]
    chosen = set(central)
    for n in central:
        chosen.update(sorted(graph[n], key=lambda x: graph.degree(x), reverse=True)[:5])
    chosen = sorted(chosen, key=lambda n: graph.degree(n), reverse=True)[:160]
    local = graph.subgraph(chosen)
    position = nx.spring_layout(local, seed=7, k=0.45, iterations=100)
    types = nodes.set_index("entity").node_type.to_dict()
    palette = ["#394f68", "#176c68", "#d0894d", "#994a64", "#bbc3ca"]
    fig, ax = plt.subplots(figsize=(6.5, 4.9))
    nx.draw_networkx_edges(local, position, ax=ax, width=0.5, alpha=0.3, edge_color="#79838b")
    for kind, label in enumerate(["大型锚点", "上市代理", "未上市代理", "事业单位", "待核验"]):
        group = [n for n in local if types.get(n, 4) == kind]
        nx.draw_networkx_nodes(
            local,
            position,
            nodelist=group,
            ax=ax,
            node_size=[12 + min(180, graph.degree(n) * 2) for n in group],
            node_color=palette[kind],
            label=label,
            alpha=0.9,
        )
    ax.legend(fontsize=9, ncol=3, loc="lower left")
    ax.set_title("2023真实网络局部（最多160节点）")
    ax.axis("off")
    save(fig, "print_network_2023")
    return {
        "figures": {
            "files": figures,
            "source_evaluation_run": params.get("evaluation_run", ""),
            "purpose": "print-size figures from frozen model outputs; no fitting or parameter selection",
        }
    }
