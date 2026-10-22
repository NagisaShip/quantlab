"""Tabular adapters around the same engines used by the professional pages."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from .tables import columns, frame, records


def panel(data, entity="symbol", time="date", timeframe="1d"):
    data = frame(data)
    columns(data, [entity, time, "close"])
    if data[[entity, time]].isna().any().any() or data.duplicated([entity, time]).any():
        raise ValueError("原生研究输入的资产和时间必须非空且唯一")
    # Numeric timestamps use the professional engine's explicit millisecond convention.
    times = (
        data[time]
        if pd.api.types.is_numeric_dtype(data[time])
        else pd.to_datetime(data[time], utc=True).astype("int64") // 1_000_000
    )
    data = data.assign(**{time: times})
    for name in data.columns:
        if name not in (entity, time):
            try:
                data[name] = pd.to_numeric(data[name], errors="raise")
            except (ValueError, TypeError):
                if name == "close":
                    raise ValueError("close价格必须为数值") from None
    symbols = sorted(data[entity].unique().tolist())
    index = np.sort(data[time].unique())
    values = {
        key: data.pivot(index=time, columns=entity, values=key).reindex(index=index, columns=symbols).to_numpy(float)
        for key in data.select_dtypes(include="number").columns
        if key != time
    }
    result = SimpleNamespace(
        data=values,
        times=index,
        symbols=symbols,
        timeframe=timeframe,
        synthetic=False,
        warnings=["用户导入数据，平台未核验其真实性或可得时点"],
    )
    result.describe = lambda: {
        "symbols": symbols,
        "bars": len(index),
        "start": int(index[0]),
        "end": int(index[-1]),
        "timeframe": timeframe,
    }
    return data, result


def factor(data, expression="returns(20)", entity="symbol", time="date", output="factor"):
    from .engines.factor_expr import evaluate

    table, p = panel(data, entity, time)
    values = evaluate(expression, p.data)
    features = (
        pd.DataFrame(values, index=p.times, columns=p.symbols)
        .rename_axis(time)
        .reset_index()
        .melt(id_vars=time, var_name=entity, value_name=output)
    )
    if output in table.columns:
        raise ValueError("因子输出字段已存在，请设置新名称")
    result = table.merge(features, on=[time, entity], validate="one_to_one", how="left", sort=False)
    result[time] = frame(data)[time].to_numpy()
    return records(result)


def portfolio(data, config=None, entity="symbol", time="date", timeframe="1d", cost_bps=10):
    from .engines.portfolio import evaluate_scheme

    _, p = panel(data, entity, time, timeframe)
    return evaluate_scheme(config or {}, p, cost_bps=cost_bps)


def execution(data, config=None, entity="symbol", time="date", step_ms=60000):
    from .engines.execution import evaluate_scheme, tape_from_bars

    table = frame(data)
    columns(table, [entity, time, "open", "high", "low", "close", "volume"])
    if table[[entity, time]].isna().any().any() or table.duplicated([entity, time]).any():
        raise ValueError("执行研究的资产与时间必须唯一且非空")
    if not pd.api.types.is_numeric_dtype(table[time]):
        table[time] = pd.to_datetime(table[time], utc=True).astype("int64") // 1_000_000
    tapes = []
    for symbol, part in table.sort_values(time).groupby(entity):
        if (np.diff(part[time]) != step_ms).any():
            raise ValueError("盘口推算需要等间隔K线，时间单位为毫秒")
        bars = [SimpleNamespace(**{**row, "open_time": row[time]}) for row in part.to_dict("records")]
        tapes.append(tape_from_bars(str(symbol), bars, step_ms=step_ms))
    return evaluate_scheme(config or {}, tapes)


def select_table(report, path):
    value = report
    for key in path.split("."):
        value = value[int(key)] if isinstance(value, list) else value[key]
    if isinstance(value, dict) and all(isinstance(v, list) for v in value.values()):
        return records(pd.DataFrame(value))
    return records(frame(value))
