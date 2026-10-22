"""Fitting and prediction cards share portable model artifacts, never hidden services."""

from __future__ import annotations

from pathlib import Path

from .tables import columns, frame, records


def arrays(rows, features, target=None):
    import numpy as np

    data = frame(rows)
    columns(data, features + ([target] if target else []))
    x = data[features].to_numpy(dtype=float)
    if not len(data) or not features or not np.isfinite(x).all():
        raise ValueError("学习输入必须非空且特征为有限数，请先处理缺失")
    y = data[target].to_numpy(dtype=float) if target else None
    if y is not None and not np.isfinite(y).all():
        raise ValueError("训练标签包含未知值，不能填作负例")
    return data, x, y


def metrics(y, p, task="classification"):
    import numpy as np
    from sklearn.metrics import (
        f1_score,
        mean_absolute_error,
        mean_squared_error,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    if not len(y) or not np.isfinite(y).all() or not np.isfinite(p).all():
        raise ValueError("评价样本须非空且标签和预测为有限数")
    if task == "regression":
        return {"mae": float(mean_absolute_error(y, p)), "mse": float(mean_squared_error(y, p))}
    return {
        "precision": float(precision_score(y, p >= 0.5, zero_division=0)),
        "recall": float(recall_score(y, p >= 0.5, zero_division=0)),
        "f1": float(f1_score(y, p >= 0.5, zero_division=0)),
        "auc": float(roc_auc_score(y, p)) if len(set(y)) == 2 else None,
        "threshold": 0.5,
        "rows": len(y),
    }


def fit(ctx, inputs, params):
    import joblib
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.utils.class_weight import compute_sample_weight

    features, target = params["features"], params["target"]
    _, x, y = arrays(inputs["train"], features, target)
    _, vx, vy = arrays(inputs["validation"], features, target)
    name = params["algorithm"]
    task = "regression" if name == "ridge" else "classification"
    if task == "classification" and (set(y) != {0.0, 1.0} or not set(vy) <= {0.0, 1.0}):
        raise ValueError("二分类训练必须有0、1两类，验证标签也须为0或1")
    models = {
        "logistic": lambda: LogisticRegression(
            C=params.get("C", 1.0),
            max_iter=params.get("max_iter", 1000),
            class_weight="balanced",
            random_state=ctx.seed,
        ),
        "gbdt": lambda: HistGradientBoostingClassifier(
            max_iter=params.get("iterations", 100), max_leaf_nodes=params.get("leaves", 15), random_state=ctx.seed
        ),
        "random_forest": lambda: RandomForestClassifier(
            n_estimators=params.get("iterations", 100),
            max_depth=params.get("depth", 8),
            class_weight="balanced",
            random_state=ctx.seed,
            n_jobs=1,
        ),
        "ridge": lambda: Ridge(alpha=params.get("alpha", 1.0)),
    }
    if name not in models:
        raise ValueError("未知学习算法")
    estimator = models[name]()
    kwargs = {"sample_weight": compute_sample_weight("balanced", y)} if name == "gbdt" else {}
    estimator.fit(x, y, **kwargs)
    prediction = estimator.predict(vx) if task == "regression" else estimator.predict_proba(vx)[:, 1]
    path = Path(ctx.work) / "estimator.joblib"
    joblib.dump(estimator, path)
    model = {
        "format": "quantlab.model/1",
        "backend": "sklearn",
        "algorithm": name,
        "features": features,
        "target": target,
        "task": task,
        "checkpoint": ctx.save_file(path),
        "seed": ctx.seed,
    }
    return {"model": model, "validation": metrics(vy, prediction, task)}


def predict(ctx, inputs, params):
    import hashlib

    import joblib
    import numpy as np

    model = inputs["model"]
    reference = model["checkpoint"]
    path = ctx.file(reference["file"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference["sha256"]:
        raise ValueError("模型文件哈希校验失败")
    data, x, _ = arrays(inputs["data"], model["features"])
    if model["backend"] == "sklearn":
        estimator = joblib.load(path)
        p = estimator.predict(x) if model["task"] == "regression" else estimator.predict_proba(x)[:, 1]
    elif model["backend"] == "torch":
        import torch

        from .network import build

        estimator = build(model["graph"])
        estimator.load_state_dict(torch.load(path, map_location="cpu", weights_only=True))
        estimator.eval()
        with torch.inference_mode():
            values = estimator(torch.tensor(x, dtype=torch.float32)).reshape(-1)
            p = (torch.sigmoid(values) if model["task"] == "classification" else values).numpy()
    else:
        raise ValueError("未知模型类型")
    if not np.isfinite(p).all():
        raise ValueError("模型输出包含非有限数")
    data["score"] = p
    if model["task"] == "classification":
        data["prediction"] = (p >= params.get("threshold", 0.5)).astype(int)
    return {"data": records(data)}


def train_network(ctx, inputs, params):
    import copy

    import numpy as np
    import torch

    from .network import build, inspect

    graph = inputs["network"]["graph"]
    spec = inspect(graph)
    features = params["features"]
    target = params["target"]
    if (
        len(spec["input_ids"]) != 1
        or spec["output_shape"] != [1]
        or next(c["shape"] for c in spec["components"] if c["type"] == "Input") != [len(features)]
    ):
        raise ValueError("此表格训练器要求单个[特征数]输入和[1]输出；序列/多输入请使用自定义训练函数")
    if next(n["type"] for n in graph["nodes"] if n["id"] == graph["output"]) == "Sigmoid":
        raise ValueError("BCEWithLogitsLoss要求原始logit输出，请移除最后的Sigmoid")
    torch.manual_seed(ctx.seed)
    np.random.seed(ctx.seed)
    _, x, y = arrays(inputs["train"], features, target)
    _, vx, vy = arrays(inputs["validation"], features, target)
    if set(y) != {0.0, 1.0} or not set(vy) <= {0.0, 1.0}:
        raise ValueError("需要0/1二分类标签且训练含两类")
    if spec["parameter_count"] > 10_000_000:
        raise ValueError("基础训练器参数超过一千万，请使用专用训练模块")
    model = build(graph)
    tx = torch.tensor(x, dtype=torch.float32)
    ty = torch.tensor(y, dtype=torch.float32)
    tvx = torch.tensor(vx, dtype=torch.float32)
    tvy = torch.tensor(vy, dtype=torch.float32)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=params.get("learning_rate", 0.001), weight_decay=params.get("weight_decay", 0.0001)
    )
    criterion = torch.nn.BCEWithLogitsLoss()
    best = float("inf")
    saved = None
    history = []
    stale = 0
    for epoch in range(params.get("epochs", 50)):
        model.train()
        optimizer.zero_grad()
        loss = criterion(model(tx).reshape(-1), ty)
        if not torch.isfinite(loss):
            raise ValueError("训练损失非有限值，请检查输入尺度")
        loss.backward()
        optimizer.step()
        model.eval()
        with torch.inference_mode():
            vl = float(criterion(model(tvx).reshape(-1), tvy))
        history.append({"epoch": epoch + 1, "train_loss": float(loss.detach()), "validation_loss": vl})
        if vl < best:
            best = vl
            saved = copy.deepcopy(model.state_dict())
            stale = 0
        else:
            stale += 1
        if stale >= params.get("patience", 8):
            break
    if saved is None:
        raise ValueError("没有有效的验证损失，无法选择模型")
    model.load_state_dict(saved)
    model.eval()
    with torch.inference_mode():
        p = torch.sigmoid(model(tvx).reshape(-1)).numpy()
    path = Path(ctx.work) / "network.pt"
    torch.save(saved, path)
    return {
        "model": {
            "format": "quantlab.model/1",
            "backend": "torch",
            "graph": graph,
            "features": features,
            "target": target,
            "task": "classification",
            "checkpoint": ctx.save_file(path),
            "seed": ctx.seed,
            "parameter_count": spec["parameter_count"],
            "weights": [
                {
                    "name": n,
                    "shape": list(v.shape),
                    "mean": float(v.float().mean()),
                    "std": float(v.float().std(unbiased=False)),
                }
                for n, v in saved.items()
            ],
        },
        "validation": metrics(vy, p),
        "history": history,
    }


def evaluate(ctx, inputs, params):
    data = frame(inputs["data"])
    columns(data, [params["target"], params["score"]])
    return {
        "report": metrics(
            data[params["target"]].to_numpy(), data[params["score"]].to_numpy(), params.get("task", "classification")
        )
    }
