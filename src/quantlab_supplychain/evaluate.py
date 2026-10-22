from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .io import load_npz, read_frame, save_frame, save_json
from .train import threshold


def load_experiments(ctx, inputs, params):
    import json

    value = json.loads(ctx.file(params["file"]).read_text(encoding="utf-8"))
    if not value.get("provenance", {}).get("main_run"):
        raise ValueError("冻结实验集合缺少来源运行记录")
    return value["outputs"]


def measures(y, p, cutoff):
    from sklearn.metrics import (
        average_precision_score,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    prediction = p >= cutoff
    tn, fp, fn, tp = confusion_matrix(y, prediction, labels=[0, 1]).ravel()
    both = len(np.unique(y)) == 2
    return {
        "n": len(y),
        "positive": int(np.sum(y)),
        "precision": float(precision_score(y, prediction, zero_division=0)),
        "recall": float(recall_score(y, prediction, zero_division=0)),
        "f1": float(f1_score(y, prediction, zero_division=0)),
        "auc": float(roc_auc_score(y, p)) if both else None,
        "pr_auc": float(average_precision_score(y, p)) if both else None,
        "tn": int(tn),
        "fp": int(fp),
        "fn": int(fn),
        "tp": int(tp),
        "threshold": float(cutoff),
    }


def bootstrap(y, p, cutoff, seed=7):
    from sklearn.metrics import roc_auc_score

    if len(np.unique(y)) < 2:
        return {"auc_low": None, "auc_high": None}
    rng = np.random.default_rng(seed)
    positive = np.flatnonzero(y == 1)
    negative = np.flatnonzero(y == 0)
    scores = []
    for _ in range(300):
        index = np.concatenate(
            [rng.choice(positive, len(positive), replace=True), rng.choice(negative, len(negative), replace=True)]
        )
        scores.append(roc_auc_score(y[index], p[index]))
    low, high = np.quantile(scores, [0.025, 0.975])
    return {
        "auc_low": float(low),
        "auc_high": float(high),
        "bootstrap": "300 stratified resamples;fixed fitted ensemble and validation threshold;conditional on observed graph/sample",
    }


def plotting():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Arial Unicode MS"]:
        if name in installed:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams.update(
        {
            "axes.unicode_minus": False,
            "figure.dpi": 160,
            "savefig.dpi": 220,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.labelsize": 12,
            "xtick.labelsize": 10,
            "ytick.labelsize": 10,
            "font.size": 12,
        }
    )
    return plt


def evaluate(ctx, inputs, params):
    from sklearn.metrics import precision_recall_curve, roc_curve

    dataset = inputs["dataset"]
    graphs = {int(y): load_npz(ctx, r) for y, r in dataset["graphs"].items() if int(y) >= 2022}
    ground = []
    for year, g in graphs.items():
        for i in np.flatnonzero(g["target"]):
            ground.append(
                {
                    "entity": str(g["entity"][i]),
                    "year": year,
                    "main_label": int(g["y"][i]),
                    "composite_label": int(g["y_composite"][i]),
                }
            )
    ground = pd.DataFrame(ground)
    runs = []
    for key, model in inputs.items():
        if key == "dataset" or not isinstance(model, dict) or "predictions" not in model:
            continue
        prediction = read_frame(ctx, model["predictions"], dtype={"entity": str})
        prediction = prediction.merge(ground, on=["entity", "year"], how="left", validate="many_to_one")
        prediction["label"] = np.where(
            prediction.label_variant.eq("composite"), prediction.composite_label, prediction.main_label
        ).astype(int)
        if prediction.label.lt(0).any():
            raise ValueError("评价收到未观测标签")
        runs.append(prediction)
    data = pd.concat(runs, ignore_index=True)
    seed_metrics = []
    ensemble_metrics = []
    ensembles = {}
    for (name, seed), group in data[data.year.eq(2024)].groupby(["model", "seed"]):
        cutoff = float(group.threshold.iloc[0])
        for subtype, selected in [
            ("全部", group),
            ("上市中小企业代理", group[group.node_type.eq(1)]),
            ("未上市中小企业代理", group[group.node_type.eq(2)]),
        ]:
            if selected.empty:
                continue
            seed_metrics.append(
                {
                    "model": name,
                    "seed": int(seed),
                    "subgroup": subtype,
                    **measures(selected.label.to_numpy(), selected.probability.to_numpy(), cutoff),
                }
            )
    seed_frame = pd.DataFrame(seed_metrics)
    metrics = ["precision", "recall", "f1", "auc", "pr_auc"]
    summary = seed_frame.groupby(["model", "subgroup"])[metrics].agg(["mean", "std"]).reset_index()
    summary.columns = ["_".join(c).strip("_") if isinstance(c, tuple) else c for c in summary.columns]
    for name, group in data.groupby("model"):
        average = group.groupby(["entity", "year", "node_type", "label"], as_index=False).probability.mean()
        val = average[average.year.isin([2022, 2023])]
        test = average[average.year.eq(2024)].copy()
        cutoff = threshold(val.label.to_numpy(), val.probability.to_numpy())
        test["prediction"] = (test.probability >= cutoff).astype(int)
        ensembles[name] = {"test": test, "cutoff": cutoff}
        for subtype, selected in [
            ("全部", test),
            ("上市中小企业代理", test[test.node_type.eq(1)]),
            ("未上市中小企业代理", test[test.node_type.eq(2)]),
        ]:
            if selected.empty:
                continue
            y = selected.label.to_numpy()
            p = selected.probability.to_numpy()
            ensemble_metrics.append(
                {"model": name, "subgroup": subtype, **measures(y, p, cutoff), **bootstrap(y, p, cutoff)}
            )
    main = ["LR", "GBDT", "GCN", "GAT", "HGNNs", "T-HGNNs"]
    plt = plotting()
    figures = {}
    colors = ["#34495e", "#cd783f", "#8494a7", "#438f9b", "#176c68", "#bc4a43"]

    def save(fig, name):
        fig.tight_layout()
        path = Path(ctx.work) / (name + ".png")
        fig.savefig(path, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        figures[name] = ctx.save_file(path)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    for name, color in zip(main, colors, strict=True):
        item = ensembles[name]
        test = item["test"]
        y = test.label.to_numpy()
        p = test.probability.to_numpy()
        false, true, _ = roc_curve(y, p)
        precision, recall, _ = precision_recall_curve(y, p)
        result = next(r for r in ensemble_metrics if r["model"] == name and r["subgroup"] == "全部")
        axes[0].plot(false, true, color=color, label=f"{name}  {result['auc']:.3f}", linewidth=1.8)
        axes[1].plot(recall, precision, color=color, label=f"{name}  {result['pr_auc']:.3f}", linewidth=1.8)
    axes[0].plot([0, 1], [0, 1], "--", color="#aaaaaa", linewidth=1)
    axes[0].set(xlabel="假阳性率", ylabel="真阳性率", title="2024 测试集 ROC")
    axes[1].set(xlabel="召回率", ylabel="精确率", title="2024 测试集 PR")
    axes[0].legend(fontsize=9, loc="lower right")
    axes[1].legend(fontsize=9, loc="upper right")
    save(fig, "roc_pr")
    overall = summary[summary.subgroup.eq("全部")].set_index("model").loc[main]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    for ax, metric, title in zip(axes, ["auc", "f1"], ["ROC-AUC", "F1"], strict=True):
        ax.bar(
            np.arange(6), overall[metric + "_mean"], yerr=overall[metric + "_std"].fillna(0), color=colors, capsize=4
        )
        ax.set_xticks(np.arange(6), main, rotation=20)
        ax.set_ylim(0, 1.03)
        ax.set_ylabel(title)
        ax.set_title("三个种子的均值与标准差")
        for i, v in enumerate(overall[metric + "_mean"]):
            ax.text(i, v + 0.035, f"{v:.3f}", ha="center", fontsize=10)
    save(fig, "model_comparison")
    fig, axes = plt.subplots(2, 3, figsize=(11, 7.3))
    for ax, name in zip(axes.ravel(), main, strict=True):
        r = next(r for r in ensemble_metrics if r["model"] == name and r["subgroup"] == "全部")
        matrix = np.array([[r["tn"], r["fp"]], [r["fn"], r["tp"]]])
        ax.imshow(matrix, cmap="Blues")
        for (i, j), v in np.ndenumerate(matrix):
            ax.text(
                j,
                i,
                str(v),
                ha="center",
                va="center",
                color="white" if v > matrix.max() / 2 else "#1d2939",
                fontsize=13,
            )
        ax.set(
            xticks=[0, 1],
            xticklabels=["正常", "风险"],
            yticks=[0, 1],
            yticklabels=["正常", "风险"],
            xlabel="预测",
            ylabel="真实",
            title=name,
        )
    save(fig, "confusion_matrices")
    ablation_names = ["HGNNs", "HGNNs / no_trade", "HGNNs / no_capital", "T-HGNNs"]
    if "T-HGNNs / no_temporal" in set(summary.model):
        ablation_names.append("T-HGNNs / no_temporal")
    selected = (
        summary[(summary.subgroup == "全部") & summary.model.isin(ablation_names)]
        .set_index("model")
        .reindex(ablation_names)
    )
    if selected.auc_mean.notna().all():
        fig, ax = plt.subplots(figsize=(10, 4.5))
        ax.bar(
            range(len(ablation_names)),
            selected.auc_mean,
            yerr=selected.auc_std.fillna(0),
            color=["#176c68", "#95a3aa", "#cf9b70", "#bc4a43", "#bfa3a0"][: len(ablation_names)],
            capsize=4,
        )
        ax.set_xticks(
            range(len(ablation_names)),
            ["HGNNs", "去交易关系", "去资本关系", "T-HGNNs", "仅当前一步"][: len(ablation_names)],
        )
        ax.set(ylabel="ROC-AUC", ylim=(0, 1.03), title="关系消融与严格历史信息消融：相同测试样本")
        save(fig, "ablation")
    annual = pd.DataFrame(dataset["annual_statistics"])
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].plot(annual.year, annual.nodes / 1000, "o-", label="节点", color="#176c68")
    axes[0].plot(annual.year, annual.economic_edges / 1000, "s-", label="经济关系边", color="#cd783f")
    axes[0].set(ylabel="千", xlabel="统计年度", title="年度网络规模")
    axes[0].legend()
    axes[1].bar(annual.year, annual.target_rows, color="#95b9b6", label="监督样本")
    second = axes[1].twinx()
    second.plot(annual.year, annual.target_positive / annual.target_rows * 100, "o-", color="#bc4a43", label="风险占比")
    second.set_ylabel("风险占比（%）")
    axes[1].set(xlabel="标签年度", ylabel="监督样本数", title="样本覆盖与类别不平衡")
    save(fig, "coverage")
    counts = seed_frame[(seed_frame.seed.eq(7)) & seed_frame.model.eq("LR")][["subgroup", "n", "positive"]].to_dict(
        "records"
    )
    result = {
        "test_year": 2024,
        "main_models": main,
        "seed_metrics": seed_metrics,
        "summary": summary.replace({np.nan: None}).to_dict("records"),
        "ensemble_metrics": ensemble_metrics,
        "test_counts": counts,
        "figures": figures,
        "predictions": save_frame(ctx, data, "all_predictions_with_observed_labels"),
        "seed_metrics_file": save_frame(ctx, seed_frame, "seed_metrics"),
        "summary_file": save_frame(ctx, summary, "metrics_mean_std"),
        "ensemble_metrics_file": save_frame(ctx, pd.DataFrame(ensemble_metrics), "ensemble_metrics"),
        "protocol": dataset["protocol"],
        "selection": "all model configurations and seed-specific thresholds selected before test scoring; ensemble threshold selected from mean validation probability; no winner-based refitting",
    }
    result["evidence_file"] = save_json(ctx, result, "evaluation_evidence.json")
    ctx.log("完成冻结模型的2024测试评价；已保留分组指标、三种子波动与条件置信区间")
    return {"evaluation": result}
