NODE = {
    "kind": "model.predict",
    "version": "1",
    "title": "冻结模型预测",
    "category": "建模",
    "description": "使用已训练模型和固定阈值生成预测，不重新拟合。",
    "inputs": {"model": {"type": "model"}, "data": {"type": "table", "role": "test"}},
    "outputs": {"predictions": {"type": "predictions", "role": "test"}},
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    "requirements": [],
}


def execute(ctx, inputs, params):
    import math

    m = inputs["model"]
    if m["kind"] != "logistic":
        raise ValueError("此预测函数仅支持logistic模型；其他模型请注册对应卡片")
    result = []
    for r in inputs["data"]:
        x = [(float(r[k]) - mean) / scale for k, mean, scale in zip(m["features"], m["mean"], m["scale"], strict=True)]
        score = sum(v * w for v, w in zip(x, m["weights"], strict=True)) + m["bias"]
        p = 1 / (1 + math.exp(-max(-40, min(40, score))))
        result.append(
            {
                "probability": p,
                "prediction": int(p >= m["threshold"]),
                "label": int(r[m["label"]]),
            }
        )
    return {"predictions": result}
