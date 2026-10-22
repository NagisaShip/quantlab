"""Small table operations; fitting and application have explicit boundaries."""

from __future__ import annotations

import math


def frame(rows):
    import pandas as pd

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("需要记录组成的表格")
    return pd.DataFrame(rows)


def identity(data):
    return records(frame(data))


def numeric(data, names):
    """Convert only explicit measures, keeping identifiers and leading zeros."""
    import pandas as pd

    data = data.copy()
    columns(data, names)
    for name in names:
        data[name] = pd.to_numeric(data[name].replace("", None), errors="raise")
    return data


def records(data):
    import json

    # JSON serialization handles timestamps, missing values and NumPy scalars.
    return json.loads(data.to_json(orient="records", date_format="iso", force_ascii=False))


def columns(data, names):
    missing = set(names) - set(data.columns)
    if missing:
        raise ValueError("缺少字段：" + ", ".join(sorted(missing)))


def rename(data, mapping):
    data = frame(data)
    columns(data, mapping)
    renamed = data.rename(columns=mapping)
    if renamed.columns.duplicated().any():
        raise ValueError("字段映射产生重名列")
    return records(renamed)


def deduplicate(data, keys, keep="first"):
    data = frame(data)
    columns(data, keys)
    return records(data.drop_duplicates(subset=keys or None, keep=keep))


def fill_constant(data, fields, value):
    data = frame(data)
    columns(data, fields)
    data[fields] = data[fields].fillna(value)
    return records(data)


def cast(data, mapping):
    import pandas as pd

    data = frame(data)
    columns(data, mapping)
    for name, kind in mapping.items():
        if kind == "number":
            data[name] = pd.to_numeric(data[name], errors="raise")
        elif kind == "date":
            data[name] = pd.to_datetime(data[name], errors="raise", utc=True)
        elif kind == "string":
            data[name] = data[name].astype("string")
        else:
            raise ValueError("转换类型仅支持number/date/string")
    return records(data)


def sort(data, keys, ascending=True):
    data = frame(data)
    columns(data, keys)
    return records(data.sort_values(keys, ascending=ascending, kind="stable"))


def filter_rows(data, field, operator, value):
    data = frame(data)
    columns(data, [field])
    operations = {
        "eq": lambda s: s == value,
        "ne": lambda s: s != value,
        "gt": lambda s: s > value,
        "ge": lambda s: s >= value,
        "lt": lambda s: s < value,
        "le": lambda s: s <= value,
        "not_null": lambda s: s.notna(),
        "in": lambda s: s.isin(value),
    }
    if operator not in operations:
        raise ValueError("未知筛选操作")
    return records(data.loc[operations[operator](data[field])])


def join(left, right, keys, how="left", cardinality="many_to_one"):
    left, right = frame(left), frame(right)
    columns(left, keys)
    columns(right, keys)
    if left[keys].isna().any().any() or right[keys].isna().any().any():
        raise ValueError("连接键含缺失，先处理后再连接，避免空键互相匹配")
    return records(left.merge(right, on=keys, how=how, validate=cardinality, suffixes=("", "_right")))


def aggregate(data, keys, values, method="mean"):
    data = frame(data)
    columns(data, keys + values)
    if method not in ("mean", "sum", "median", "min", "max", "count", "std"):
        raise ValueError("未知聚合方法")
    return records(data.groupby(keys, dropna=False)[values].agg(method).reset_index())


def reshape(data, keys, variable="variable", value="value"):
    data = frame(data)
    columns(data, keys)
    return records(data.melt(id_vars=keys, var_name=variable, value_name=value))


def quality(data, keys=None):
    data = frame(data)
    keys = keys or []
    columns(data, keys)
    return {
        "rows": len(data),
        "columns": [
            {
                "name": str(c),
                "dtype": str(data[c].dtype),
                "missing": int(data[c].isna().sum()),
                "unique": int(data[c].nunique()),
            }
            for c in data
        ],
        "duplicate_keys": int(data.duplicated(keys or None).sum()),
    }


def fit_transformer(data, fields, method="standardize", lower=0.01, upper=0.99):
    import numpy as np
    import pandas as pd

    data = frame(data)
    columns(data, fields)
    if not fields or method not in ("standardize", "impute", "winsorize") or not 0 <= lower < upper <= 1:
        raise ValueError("变换参数无效")
    learned = {}
    for name in fields:
        s = pd.to_numeric(data[name], errors="raise").replace([np.inf, -np.inf], np.nan).dropna()
        if s.empty:
            raise ValueError(f"{name}在拟合样本全缺失，无法估计变换参数")
        learned[name] = {
            "median": float(s.median()),
            "mean": float(s.mean()),
            "scale": float(s.std(ddof=0)) or 1.0,
            "lower": float(s.quantile(lower)),
            "upper": float(s.quantile(upper)),
        }
    return {
        "format": "quantlab.transformer/1",
        "method": method,
        "fields": learned,
        "fitted_rows": len(data),
        "fit_scope": "explicit_train_input",
    }


def apply_transformer(data, transformer):
    import pandas as pd

    data = frame(data)
    if transformer.get("format") != "quantlab.transformer/1":
        raise ValueError("未知变换器")
    columns(data, transformer["fields"])
    for name, p in transformer["fields"].items():
        s = pd.to_numeric(data[name], errors="raise")
        data[name + "_missing"] = s.isna()
        s = s.fillna(p["median"])
        if transformer["method"] == "standardize":
            s = (s - p["mean"]) / p["scale"]
        elif transformer["method"] == "winsorize":
            s = s.clip(p["lower"], p["upper"])
        data[name] = s
    return records(data)


def lag(data, fields, time, entity, periods=1):
    if periods < 1:
        raise ValueError("滞后步数必须为正，不允许用负移位冒充历史特征")
    data = frame(data)
    columns(data, [*fields, time, entity])
    if data.duplicated([entity, time]).any():
        raise ValueError("实体与时间键必须唯一")
    data = data.sort_values([entity, time])
    for name in fields:
        data[name + f"_lag{periods}"] = data.groupby(entity)[name].shift(periods)
    return records(data)


def temporal_split(data, time, train_end, validation_end):
    data = frame(data)
    columns(data, [time])
    axis, train_end, validation_end = time_bounds(data[time], train_end, validation_end)
    if not train_end < validation_end or axis.isna().any():
        raise ValueError("边界顺序无效或时间缺失")
    return {
        "train": records(data[axis <= train_end]),
        "validation": records(data[(axis > train_end) & (axis <= validation_end)]),
        "test": records(data[axis > validation_end]),
    }


def time_bounds(series, train_end, validation_end):
    import pandas as pd

    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() == series.notna().sum():
        return numeric, float(train_end), float(validation_end)
    return (
        pd.to_datetime(series, format="ISO8601", utc=True),
        pd.to_datetime(train_end, format="ISO8601", utc=True),
        pd.to_datetime(validation_end, format="ISO8601", utc=True),
    )


def describe(data, fields):
    data = frame(data)
    columns(data, fields)
    return records(data[fields].describe().rename_axis("statistic").reset_index())


def correlation(data, fields, method="spearman"):
    data = frame(data)
    columns(data, fields)
    if method not in ("spearman", "pearson", "kendall"):
        raise ValueError("未知相关方法")
    return records(data[fields].corr(method=method).rename_axis("field").reset_index())


def bootstrap_mean(data, field, draws=500, seed=7):
    import numpy as np

    data = frame(data)
    columns(data, [field])
    s = data[field].dropna().to_numpy(dtype=float)
    if len(s) < 2 or not all(math.isfinite(float(v)) for v in s):
        raise ValueError("均值区间需要至少两个有限观测")
    rng = np.random.default_rng(seed)
    means = [float(rng.choice(s, len(s), replace=True).mean()) for _ in range(draws)]
    return {
        "mean": float(s.mean()),
        "low": float(np.quantile(means, 0.025)),
        "high": float(np.quantile(means, 0.975)),
        "draws": draws,
        "assumption": "IID resampling; clustered/time dependent data need grouped bootstrap",
    }
