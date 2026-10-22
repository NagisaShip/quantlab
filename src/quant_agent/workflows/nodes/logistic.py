NODE = {
    "kind": "model.logistic",
    "version": "1",
    "title": "逻辑回归",
    "category": "建模",
    "description": "可审查的二分类基线；标准化只拟合训练集，阈值只在验证集选择。",
    "inputs": {
        "train": {"type": "table", "role": "train"},
        "validation": {"type": "table", "role": "validation"},
        "config": {"type": "json", "required": False},
    },
    "outputs": {"model": {"type": "model"}, "validation_report": {"type": "report"}},
    "parameters": {
        "type": "object",
        "properties": {
            "features": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
            "label": {"type": "string", "default": "label"},
            "epochs": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5000,
                "default": 300,
            },
            "learning_rate": {
                "type": "number",
                "minimum": 0.00001,
                "maximum": 1,
                "default": 0.05,
            },
            "l2": {"type": "number", "minimum": 0, "maximum": 100, "default": 0.001},
        },
        "required": ["features"],
        "additionalProperties": False,
    },
    "requirements": ["numpy>=1.26"],
}


def execute(ctx, inputs, params):
    import numpy as np

    features = params["features"]
    label = params["label"]
    if not features or label in features:
        raise ValueError("特征不能为空或包含标签本身")
    train = inputs["train"]
    validation = inputs["validation"]
    x = np.asarray([[float(r[k]) for k in features] for r in train])
    y = np.asarray([int(r[label]) for r in train])
    vx = np.asarray([[float(r[k]) for k in features] for r in validation])
    vy = np.asarray([int(r[label]) for r in validation])
    if not np.isfinite(x).all() or not np.isfinite(vx).all():
        raise ValueError("特征含缺失或非有限值，请先处理")
    if set(y.tolist()) != {0, 1} or set(vy.tolist()) != {0, 1}:
        raise ValueError("训练与验证均需要0/1两类标签")
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale < 1e-12] = 1
    x = (x - mean) / scale
    vx = (vx - mean) / scale
    weights = np.zeros(len(features))
    bias = 0.0
    positive = max(1, int(y.sum()))
    negative = max(1, len(y) - positive)
    sw = np.where(y == 1, len(y) / (2 * positive), len(y) / (2 * negative))
    for _ in range(params["epochs"]):
        prob = 1 / (1 + np.exp(-np.clip(x @ weights + bias, -40, 40)))
        err = (prob - y) * sw
        weights -= params["learning_rate"] * (x.T @ err / len(x) + params["l2"] * weights)
        bias -= params["learning_rate"] * float(err.mean())
    pv = 1 / (1 + np.exp(-np.clip(vx @ weights + bias, -40, 40)))
    best = (-1, 0.5)
    for threshold in [i / 100 for i in range(5, 96, 5)]:
        predicted = pv >= threshold
        tp = int(((vy == 1) & predicted).sum())
        fp = int(((vy == 0) & predicted).sum())
        fn = int(((vy == 1) & ~predicted).sum())
        f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0
        if f1 > best[0]:
            best = (f1, threshold)
    ctx.log(f"训练{len(train)}行，验证{len(validation)}行；未访问测试集")
    return {
        "model": {
            "kind": "logistic",
            "features": features,
            "label": label,
            "mean": mean.tolist(),
            "scale": scale.tolist(),
            "weights": weights.tolist(),
            "bias": bias,
            "threshold": best[1],
        },
        "validation_report": {
            "f1": best[0],
            "threshold": best[1],
            "samples": len(validation),
            "threshold_selected_on": "validation",
        },
    }
