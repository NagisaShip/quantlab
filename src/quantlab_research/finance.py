"""Financial research primitives. Signals, execution delay and labels stay explicit."""

from __future__ import annotations

from .tables import columns, frame, numeric, records


def ordered(data, entity, time):
    data = frame(data)
    columns(data, [entity, time])
    if data[[entity, time]].isna().any().any() or data.duplicated([entity, time]).any():
        raise ValueError("实体/时间键必须非空且唯一")
    return data.sort_values([entity, time])


def returns(data, price, entity, time, periods=1):
    data = numeric(ordered(data, entity, time), [price])
    if (data[price] <= 0).any() or periods < 1:
        raise ValueError("价格必须为正，历史窗口必须为正")
    data["return"] = data.groupby(entity)[price].pct_change(periods, fill_method=None)
    return records(data)


def rolling(data, field, entity, time, window=20, method="std"):
    data = numeric(ordered(data, entity, time), [field])
    if method not in ("mean", "std", "sum", "min", "max", "median"):
        raise ValueError("未知滚动方法")
    data[f"{field}_{method}_{window}"] = data.groupby(entity)[field].transform(
        lambda s: s.rolling(window, min_periods=window).agg(method)
    )
    return records(data)


def forward_label(data, price, entity, time, horizon=5):
    data = numeric(ordered(data, entity, time), [price])
    if (data[price] <= 0).any() or horizon < 1:
        raise ValueError("价格与未来标签窗口无效")
    data["label"] = data.groupby(entity)[price].shift(-horizon) / data[price] - 1
    data["label_end"] = data.groupby(entity)[time].shift(-horizon)
    return records(data)


def purged_split(data, time, label_end, train_end, validation_end):
    data = frame(data)
    columns(data, [time, label_end])
    from .tables import time_bounds

    axis, train_end, validation_end = time_bounds(data[time], train_end, validation_end)
    import pandas as pd

    ends = (
        pd.to_numeric(data[label_end], errors="raise")
        if pd.api.types.is_numeric_dtype(axis)
        else pd.to_datetime(data[label_end], format="ISO8601", utc=True)
    )
    if not train_end < validation_end:
        raise ValueError("训练边界必须早于验证边界")
    if axis.isna().any():
        raise ValueError("观测时间不能缺失")
    known = ends.notna()
    if (ends[known] < axis[known]).any():
        raise ValueError("标签结束时间不能早于观测时间")
    train = known & (axis <= train_end) & (ends <= train_end)
    val = known & (axis > train_end) & (axis <= validation_end) & (ends <= validation_end)
    test = known & (axis > validation_end)
    return {
        "train": records(data[train]),
        "validation": records(data[val]),
        "test": records(data[test]),
        "audit": {"excluded_boundary_or_unknown": int((~(train | val | test)).sum())},
    }


def cross_section_rank(data, field, time):
    data = numeric(frame(data), [field])
    columns(data, [time])
    data[field + "_rank"] = data.groupby(time)[field].rank(pct=True)
    return records(data)


def neutralize(data, field, time, group):
    data = numeric(frame(data), [field])
    columns(data, [time, group])
    data[field + "_neutral"] = data[field] - data.groupby([time, group])[field].transform("mean")
    return records(data)


def ic(data, factor, label, time, method="spearman"):
    data = numeric(frame(data), [factor, label])
    columns(data, [time])
    if method not in ("spearman", "pearson"):
        raise ValueError("IC仅支持spearman或pearson")
    values = []
    for stamp, group in data.groupby(time):
        pair = group[[factor, label]].dropna()
        value = float(pair[factor].corr(pair[label], method=method)) if len(pair) >= 3 else None
        if value is not None and value != value:
            value = None
        values.append({time: stamp, "ic": value, "n": len(pair)})
    return records(frame(values))


def quantile_returns(data, factor, label, time, groups=5):
    import pandas as pd

    data = numeric(frame(data), [factor, label])
    columns(data, [time])
    values = []
    for stamp, part in data.groupby(time):
        part = part.dropna(subset=[factor, label]).copy()
        if len(part) < groups or part[factor].nunique() < groups:
            continue
        part["quantile"] = pd.qcut(part[factor], groups, labels=False, duplicates="drop") + 1
        for q, group in part.groupby("quantile"):
            values.append(
                {time: stamp, "quantile": int(q), "mean_forward_return": float(group[label].mean()), "n": len(group)}
            )
    if not values:
        raise ValueError("没有截面包含足够的有效样本和不同因子值")
    return records(frame(values))


def threshold_signal(data, field, threshold=0.0, short=False):
    data = numeric(frame(data), [field])
    data["signal"] = (data[field] > threshold).astype(float)
    if short:
        data.loc[data[field] < -abs(threshold), "signal"] = -1.0
    data.loc[data[field].isna(), "signal"] = 0.0
    return records(data)


def backtest(data, entity, time, returns_field="return", signal="signal", fee_bps=10.0, annual_periods=252):
    import numpy as np

    data = numeric(ordered(data, entity, time), [returns_field, signal])
    if data[[returns_field, signal]].isna().any().any():
        raise ValueError("回测收益或信号含缺失，请显式处理，避免暗中忽略样本")
    if not np.isfinite(data[[returns_field, signal]].to_numpy(dtype=float)).all():
        raise ValueError("回测输入必须为有限数")
    if (data[signal].abs() > 1).any():
        raise ValueError("此基础回测仅支持[-1,1]仓位，不隐式加入杠杆")
    data["position"] = data.groupby(entity)[signal].shift(1).fillna(0)
    data["turnover"] = data.groupby(entity)["position"].diff().fillna(data["position"]).abs()
    data["net_return"] = data["position"] * data[returns_field] - data["turnover"] * fee_bps / 10000
    daily = data.groupby(time)[["net_return", "turnover"]].mean().reset_index()
    daily["equity"] = (1 + daily.net_return).cumprod()
    daily["drawdown"] = daily.equity / daily.equity.cummax().clip(lower=1) - 1
    std = float(daily.net_return.std(ddof=1)) if len(daily) > 1 else 0
    return {
        "series": records(daily),
        "trades": records(data),
        "metrics": {
            "total_return": float(daily.equity.iloc[-1] - 1) if len(daily) else 0,
            "max_drawdown": float(daily.drawdown.min()) if len(daily) else 0,
            "sharpe": float(daily.net_return.mean() / std * annual_periods**0.5) if std > 0 else None,
            "assumptions": "One observation delay; daily equal-weight across observed entities; fixed bps turnover cost; no fills/liquidity simulation",
        },
    }


def inverse_volatility(data, asset, volatility):
    data = numeric(frame(data), [volatility])
    columns(data, [asset])
    if data.empty or data[asset].duplicated().any() or data[volatility].isna().any() or (data[volatility] <= 0).any():
        raise ValueError("需要唯一资产和正的非缺失波动率")
    values = 1 / data[volatility]
    data["weight"] = values / values.sum()
    return records(data)


def drawdown(data, value):
    data = numeric(frame(data), [value])
    if data[value].isna().any() or (data[value] <= 0).any():
        raise ValueError("净值必须为正且完整；输入须已按时间排序")
    data["drawdown"] = data[value] / data[value].cummax() - 1
    return records(data)


def ols(data, features, target, robust=True, entity=""):
    import numpy as np
    from scipy.stats import t

    data = frame(data)
    columns(data, [*features, target, *([entity] if entity else [])])
    selected = data[[*features, target]].astype(float)
    if not np.isfinite(selected.to_numpy()).all():
        raise ValueError("回归输入含缺失或无穷值，请先显式处理")
    if entity:
        selected = selected - selected.groupby(data[entity]).transform("mean")
    y = selected[target].to_numpy()
    X = selected[features].to_numpy()
    names = list(features)
    if not entity:
        X = np.column_stack([np.ones(len(X)), X])
        names = ["intercept", *names]
    rank = np.linalg.matrix_rank(X)
    df = len(X) - rank - (data[entity].nunique() if entity else 0)
    if rank != X.shape[1] or df <= 0:
        raise ValueError("回归设计矩阵奇异或自由度不足")
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    residual = y - X @ beta
    inverse = np.linalg.inv(X.T @ X)
    cov = (
        inverse @ (X.T @ (X * residual[:, None] ** 2)) @ inverse * len(X) / df
        if robust
        else inverse * (residual @ residual) / df
    )
    se = np.sqrt(np.maximum(np.diag(cov), 0))
    bound = t.ppf(0.975, df) * se
    return {
        "coefficients": [
            {"feature": f, "estimate": float(b), "std_error": float(s), "low": float(b - h), "high": float(b + h)}
            for f, b, s, h in zip(names, beta, se, bound, strict=True)
        ],
        "rows": len(X),
        "df": int(df),
        "covariance": "HC1" if robust else "homoskedastic",
        "fixed_effect": entity or None,
        "note": "HC1 is not clustered covariance; no causal identification is implied",
    }
