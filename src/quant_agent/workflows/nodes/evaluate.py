NODE = {
    "kind": "classification.evaluate",
    "version": "1",
    "title": "分类评价",
    "category": "评价",
    "description": "生成分类指标，单一类别时AUC明确为空，不以0代替。",
    "inputs": {"predictions": {"type": "predictions", "role": "test"}},
    "outputs": {"report": {"type": "report"}},
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    "requirements": [],
}


def execute(ctx, inputs, params):
    rows = inputs["predictions"]
    if not rows:
        raise ValueError("没有可评价样本")
    if any(r["label"] not in (0, 1) or r["prediction"] not in (0, 1) or not 0 <= r["probability"] <= 1 for r in rows):
        raise ValueError("预测与标签必须符合二分类约定")
    tp = sum(r["label"] == 1 and r["prediction"] == 1 for r in rows)
    fp = sum(r["label"] == 0 and r["prediction"] == 1 for r in rows)
    fn = sum(r["label"] == 1 and r["prediction"] == 0 for r in rows)
    tn = len(rows) - tp - fp - fn
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    positive = sum(x["label"] for x in rows)
    negative = len(rows) - positive
    ordered = sorted(rows, key=lambda x: x["probability"])
    rank_sum = 0
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j]["probability"] == ordered[i]["probability"]:
            j += 1
        rank_sum += sum(x["label"] for x in ordered[i:j]) * (i + 1 + j) / 2
        i = j
    auc = (rank_sum - positive * (positive + 1) / 2) / (positive * negative) if positive and negative else None
    # Average precision groups ties at a common threshold.
    ap = 0
    seen = 0
    found = 0
    descending = list(reversed(ordered))
    i = 0
    while i < len(descending):
        j = i + 1
        while j < len(descending) and descending[j]["probability"] == descending[i]["probability"]:
            j += 1
        hits = sum(x["label"] for x in descending[i:j])
        found += hits
        seen += j - i
        if positive:
            ap += hits / positive * found / seen
        i = j
    return {
        "report": {
            "samples": len(rows),
            "positive": positive,
            "negative": negative,
            "precision": p,
            "recall": r,
            "f1": 2 * p * r / (p + r) if p + r else 0,
            "roc_auc": auc,
            "average_precision": ap if positive else None,
            "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
            "evaluation_role": "test",
            "checks": [{"name": "包含正负两类测试样本", "passed": bool(positive and negative)}],
        }
    }
