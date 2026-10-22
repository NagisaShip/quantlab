from __future__ import annotations

import copy
import gc
import random
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .io import load_npz, save_frame, save_json


def auc(y, p):
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else 0.5


def threshold(y, p):
    from sklearn.metrics import precision_recall_curve

    precision, recall, candidates = precision_recall_curve(y, p)
    if not len(candidates):
        return 0.5
    f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(precision[:-1] + recall[:-1], 1e-12)
    return float(candidates[int(np.argmax(f1))])


def masks(graphs, variant):
    output = {}
    for year, g in graphs.items():
        y = g["y_composite"] if variant == "composite" else g["y"]
        mask = g["target"] & (y >= 0)
        if variant == "composite" and year < 2018:
            mask &= False
        output[year] = (mask, y)
    return output


def concatenate(graphs, selected, mask_map):
    x = []
    y = []
    rows = []
    for year in selected:
        g = graphs[year]
        mask, labels = mask_map[year]
        x.append(g["x"][mask])
        y.append(labels[mask])
        rows.extend(
            {"entity": str(entity), "year": year, "node_type": int(t)}
            for entity, t in zip(g["entity"][mask], g["node_type"][mask], strict=True)
        )
    return np.concatenate(x), np.concatenate(y), pd.DataFrame(rows)


def to_torch(graphs, device, ablation="none"):
    import torch

    result = {}
    for year, g in graphs.items():
        result[year] = {
            k: torch.as_tensor(g[k], device=device)
            for k in [
                "x",
                "node_type",
                "y",
                "y_composite",
                "target",
                "global_id",
                "edge_index",
                "edge_type",
                "edge_weight",
            ]
        }
        if ablation in ("no_trade", "no_capital"):
            selected = g["edge_type"] >= 4 if ablation == "no_trade" else g["edge_type"] < 4
            result[year]["edge_index"] = result[year]["edge_index"][:, selected]
            result[year]["edge_type"] = result[year]["edge_type"][selected]
            result[year]["edge_weight"] = result[year]["edge_weight"][selected]
    return result


def fit_graph(ctx, kind, graphs, mask_map, hidden, epochs, device, ablation):
    import torch

    from .models import make

    tensors = to_torch(graphs, device, ablation)
    model = make(kind, graphs[2012]["x"].shape[1], hidden).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.01, weight_decay=1e-4)
    train_y = np.concatenate([mask_map[y][1][mask_map[y][0]] for y in range(2012, 2022)])
    weight = torch.tensor(float((train_y == 0).sum() / max(1, (train_y == 1).sum())), device=device)
    criterion = torch.nn.BCEWithLogitsLoss(pos_weight=weight, reduction="sum")
    best = -1
    best_state = None
    patience = 0
    history = []
    for epoch in range(epochs):
        model.train()
        optimizer.zero_grad(set_to_none=True)
        loss_value = 0
        order = list(range(2012, 2022))
        random.shuffle(order)
        for year in order:
            mask, labels = mask_map[year]
            if not mask.any():
                continue
            g = tensors[year]
            logits, _ = model(g)
            loss = criterion(logits[mask], torch.as_tensor(labels[mask], dtype=torch.float32, device=device)) / len(
                train_y
            )
            loss.backward()
            loss_value += float(loss.detach())
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
        optimizer.step()
        if epoch % 2 == 0 or epoch == epochs - 1:
            model.eval()
            values = []
            truth = []
            with torch.inference_mode():
                for year in (2022, 2023):
                    m, y = mask_map[year]
                    p, _ = model(tensors[year])
                    values.extend(torch.sigmoid(p[m]).cpu().numpy())
                    truth.extend(y[m])
            score = auc(truth, values)
            history.append({"epoch": epoch + 1, "loss": loss_value, "validation_auc": score})
            if score > best + 1e-5:
                best = score
                best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
                patience = 0
            else:
                patience += 1
            if patience >= 6:
                break
    model.load_state_dict(best_state)
    model.eval()
    predictions = {}
    with torch.inference_mode():
        for year in (2022, 2023, 2024):
            logits, _ = model(tensors[year])
            predictions[year] = torch.sigmoid(logits).cpu().numpy()
    del tensors, optimizer
    gc.collect()
    return model, best_state, predictions, best, history


def fit_temporal(ctx, graphs, mask_map, hidden, epochs, device, history_length=3):
    import torch

    from .models import Heterogeneous, Temporal

    tensors = to_torch(graphs, device)
    encoder = Heterogeneous(graphs[2012]["x"].shape[1], hidden).to(device)
    optimizer = torch.optim.AdamW(encoder.parameters(), lr=0.01, weight_decay=1e-4)
    embeddings = {}
    stage_log = []
    previous = None
    for year in range(2012, 2025):
        mask, labels = mask_map[year]
        if year <= 2021 and mask.any():
            y = labels[mask]
            pos_weight = torch.tensor((y == 0).sum() / max(1, (y == 1).sum()), device=device, dtype=torch.float32)
            criterion = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
            encoder.train()
            for _ in range(4):
                optimizer.zero_grad(set_to_none=True)
                logits, _ = encoder(tensors[year])
                loss = criterion(logits[mask], torch.as_tensor(y, dtype=torch.float32, device=device))
                loss.backward()
                torch.nn.utils.clip_grad_norm_(encoder.parameters(), 5)
                optimizer.step()
            stage_log.append({"year": year, "supervised_updates": 4, "training_labels": len(y)})
        else:
            stage_log.append({"year": year, "supervised_updates": 0, "training_labels": 0})
        encoder.eval()
        with torch.inference_mode():
            _, h = encoder(tensors[year])
            current = h.cpu().numpy()
        # Orthogonal alignment makes yearly warm-start embeddings comparable.
        if previous is not None:
            old_year, old_h = previous
            common, old_index, new_index = np.intersect1d(
                graphs[old_year]["global_id"], graphs[year]["global_id"], return_indices=True
            )
            if len(common) >= hidden:
                u, _, vt = np.linalg.svd(current[new_index].T @ old_h[old_index], full_matrices=False)
                current = current @ (u @ vt)
        embeddings[year] = current.astype(np.float32)
        previous = (year, current)
    lookup = {year: {int(i): row for row, i in enumerate(graphs[year]["global_id"])} for year in graphs}
    sequences = {}
    labels_by = {}
    masks_by = {}
    for year, g in graphs.items():
        mask, labels = mask_map[year]
        indices = np.flatnonzero(mask)
        seq = np.zeros((len(indices), 3, hidden + 1), dtype=np.float32)
        for step, history_year in enumerate(range(year - 2, year + 1)):
            if history_year not in embeddings:
                continue
            for row, index in enumerate(indices):
                prior = lookup[history_year].get(int(g["global_id"][index]))
                if prior is not None:
                    seq[row, step, :hidden] = embeddings[history_year][prior]
                    seq[row, step, hidden] = 1
        sequences[year] = seq[:, -history_length:, :]
        labels_by[year] = labels[mask]
        masks_by[year] = mask
    model = Temporal(hidden, hidden).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.005, weight_decay=1e-4)
    xtrain = np.concatenate([sequences[y] for y in range(2012, 2022)])
    ytrain = np.concatenate([labels_by[y] for y in range(2012, 2022)])
    xval = np.concatenate([sequences[y] for y in (2022, 2023)])
    yval = np.concatenate([labels_by[y] for y in (2022, 2023)])
    weight = torch.tensor((ytrain == 0).sum() / max(1, (ytrain == 1).sum()), device=device, dtype=torch.float32)
    criterion = torch.nn.BCEWithLogitsLoss(pos_weight=weight)
    best = -1
    best_state = None
    history = []
    patience = 0
    for epoch in range(epochs):
        model.train()
        order = np.random.permutation(len(ytrain))
        losses = []
        for start in range(0, len(order), 2048):
            batch = order[start : start + 2048]
            x = torch.as_tensor(xtrain[batch], device=device)
            y = torch.as_tensor(ytrain[batch], device=device, dtype=torch.float32)
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(x), y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)
            optimizer.step()
            losses.append(float(loss.detach()))
        model.eval()
        with torch.inference_mode():
            p = torch.sigmoid(model(torch.as_tensor(xval, device=device))).cpu().numpy()
        score = auc(yval, p)
        history.append({"epoch": epoch + 1, "loss": float(np.mean(losses)), "validation_auc": score})
        if score > best + 1e-5:
            best = score
            best_state = copy.deepcopy({k: v.detach().cpu() for k, v in model.state_dict().items()})
            patience = 0
        else:
            patience += 1
        if patience >= 8:
            break
    model.load_state_dict(best_state)
    model.eval()
    predictions = {}
    with torch.inference_mode():
        for year in (2022, 2023, 2024):
            predictions[year] = np.full(len(graphs[year]["x"]), np.nan, dtype=np.float32)
            predictions[year][masks_by[year]] = (
                torch.sigmoid(model(torch.as_tensor(sequences[year], device=device))).cpu().numpy()
            )
    artifact = {
        "temporal_state": best_state,
        "encoder_state": {k: v.detach().cpu() for k, v in encoder.state_dict().items()},
        "yearly_protocol": stage_log,
        "embedding_width": hidden,
        "history_length": history_length,
        "alignment": "orthogonal Procrustes on common IDs;warm-start yearly encoder;2022-2024 no supervised updates",
    }
    del tensors, encoder, optimizer
    gc.collect()
    return model, artifact, predictions, best, history


def train_model(ctx, inputs, params):
    import torch
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.utils.class_weight import compute_sample_weight

    torch.set_num_threads(8)
    dataset = inputs["dataset"]
    graphs = {int(y): load_npz(ctx, r) for y, r in dataset["graphs"].items()}
    variant = params.get("label_variant", "main")
    mask_map = masks(graphs, variant)
    xtrain, ytrain, _ = concatenate(graphs, range(2012, 2022), mask_map)
    xval, yval, _val_rows = concatenate(graphs, (2022, 2023), mask_map)
    if len(np.unique(ytrain)) < 2 or len(np.unique(yval)) < 2:
        raise ValueError("训练或验证样本只有单一类别，不能调参")
    kind = params["model"]
    ablation = params.get("ablation", "none")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    seeds = params.get("seeds", [7, 17, 27])
    records = []
    prediction_rows = []
    checkpoints = []
    for seed in seeds:
        start = time.time()
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if device == "cuda":
            torch.cuda.manual_seed_all(seed)
        candidates = []
        histories = []
        grid = [16, 32] if kind not in ("LR", "GBDT") else ([0.1, 1.0] if kind == "LR" else [15, 31])
        for value in grid:
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            if kind == "LR":
                model = LogisticRegression(
                    C=value, max_iter=1000, random_state=seed, class_weight="balanced", solver="lbfgs"
                )
                model.fit(xtrain, ytrain)
                p = model.predict_proba(xval)[:, 1]
                score = auc(yval, p)
                state = None
                all_prob = None
                history = []
            elif kind == "GBDT":
                model = HistGradientBoostingClassifier(
                    max_leaf_nodes=value,
                    max_iter=160,
                    max_bins=64,
                    learning_rate=0.08,
                    l2_regularization=1,
                    early_stopping=False,
                    random_state=seed,
                )
                model.fit(xtrain, ytrain, sample_weight=compute_sample_weight("balanced", ytrain))
                p = model.predict_proba(xval)[:, 1]
                score = auc(yval, p)
                state = None
                all_prob = None
                history = []
            else:
                fitter = fit_temporal if kind == "T-HGNNs" else fit_graph
                if kind == "T-HGNNs":
                    model, state, all_prob, score, history = fitter(
                        ctx,
                        graphs,
                        mask_map,
                        value,
                        params.get("epochs", 35),
                        device,
                        history_length=1 if ablation == "no_temporal" else 3,
                    )
                else:
                    model, state, all_prob, score, history = fitter(
                        ctx, kind, graphs, mask_map, value, params.get("epochs", 35), device, ablation
                    )
                p = np.concatenate([all_prob[y][mask_map[y][0]] for y in (2022, 2023)])
            histories.append({"configuration": value, "validation_auc": score, "history": history})
            candidates.append(
                {
                    "model": model,
                    "state": state,
                    "all_prob": all_prob,
                    "score": score,
                    "value": value,
                    "validation": p.copy(),
                }
            )
            ctx.log(f"{kind} 种子{seed} 配置{value}：验证AUC={score:.5f}")
        best = max(candidates, key=lambda r: r["score"])
        model = best["model"]
        cutoff = threshold(yval, best["validation"])
        model_name = (
            kind + (" / " + ablation if ablation != "none" else "") + (" / composite" if variant == "composite" else "")
        )
        for year in (2022, 2023, 2024):
            g = graphs[year]
            mask, _ = mask_map[year]
            probability = (
                model.predict_proba(g["x"][mask])[:, 1] if kind in ("LR", "GBDT") else best["all_prob"][year][mask]
            )
            prediction_rows.extend(
                {
                    "entity": str(entity),
                    "year": year,
                    "node_type": int(t),
                    "probability": float(p),
                    "threshold": cutoff,
                    "prediction": int(p >= cutoff),
                    "seed": seed,
                    "model": model_name,
                    "label_variant": variant,
                }
                for entity, t, p in zip(g["entity"][mask], g["node_type"][mask], probability, strict=True)
            )
        path = Path(ctx.work) / (kind.replace("-", "_") + "_" + str(seed) + ".pt")
        if kind in ("LR", "GBDT"):
            import joblib

            path = path.with_suffix(".joblib")
            joblib.dump(model, path)
        else:
            torch.save(
                {
                    "kind": kind,
                    "hidden": best["value"],
                    "inputs": len(dataset["feature_names"]),
                    "state": best["state"],
                    "threshold": cutoff,
                    "seed": seed,
                    "ablation": ablation,
                    "label_variant": variant,
                },
                path,
            )
        checkpoint = ctx.save_file(path)
        checkpoints.append(
            {"seed": seed, "model": model_name, "checkpoint": checkpoint, "threshold": cutoff, "hidden": best["value"]}
        )
        records.append(
            {
                "seed": seed,
                "model": model_name,
                "selected": best["value"],
                "validation_auc": best["score"],
                "threshold": cutoff,
                "train_rows": len(ytrain),
                "train_positive": int(ytrain.sum()),
                "validation_rows": len(yval),
                "validation_positive": int(yval.sum()),
                "seconds": time.time() - start,
                "search": histories,
            }
        )
        del candidates, model
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()
    predictions = save_frame(ctx, pd.DataFrame(prediction_rows), "predictions_" + kind.replace("-", "_"))
    training = {
        "model": kind,
        "label_variant": variant,
        "ablation": ablation,
        "seeds": records,
        "checkpoints": checkpoints,
        "predictions": predictions,
        "selection": "validation ROC-AUC; threshold=max validation F1; no test labels read during fitting/selection",
        "device": device,
    }
    training["audit_file"] = save_json(ctx, training, "training_" + kind.replace("-", "_") + ".json")
    return {"model": training}


def train_temporal_ablation(ctx, inputs, params):
    return train_model(ctx, inputs, {**params, "model": "T-HGNNs", "label_variant": "main", "ablation": "no_temporal"})
