"""Charts retain their source, statistical choices and presentation separately."""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from threading import RLock

from .tables import columns, frame, numeric, records

TYPES = ("line", "bar", "area", "scatter", "histogram", "box", "heatmap", "network", "candlestick")
_RENDER_LOCK = RLock()


def serialized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _RENDER_LOCK:
            return function(*args, **kwargs)

    return wrapped


def prepare(data, config):
    data = frame(data)
    kind = config.get("chart_type", "line")
    if kind not in TYPES or data.empty:
        raise ValueError("图表类型未知或数据为空")
    if len(data) > 5000:
        raise ValueError("交互图表最多5000行，请先显式筛选或聚合，不会自动丢弃样本")
    x, y, group = config.get("x", ""), config.get("y", ""), config.get("group", "")
    columns(data, [c for c in (x, y, group) if c])
    method = config.get("aggregate", "none")
    if y and kind != "network" and method != "count":
        data = numeric(data, [y])
    if kind == "candlestick":
        data = numeric(data, ["open", "high", "low", "close"])
    if group and data[group].nunique(dropna=False) > 40:
        raise ValueError("分组图最多40组，请显式筛选")
    if method != "none":
        if not x or not y or method not in ("mean", "sum", "median", "min", "max", "count"):
            raise ValueError("聚合需要x、y及已支持的方法")
        data = data.groupby([x, *([group] if group else [])], dropna=False)[y].agg(method).reset_index()
    if kind in ("line", "bar", "area", "scatter", "network") and (not x or not y):
        raise ValueError("该图表需要明确的x与y字段")
    return data, {
        "format": "quantlab.chart/1",
        "chart_type": kind,
        "statistics": {"x": x, "y": y, "group": group, "aggregate": method, "rows": len(data)},
        "style": {
            k: config[k]
            for k in ("title", "color", "x_label", "y_label", "log_y", "grid", "width", "height", "bins")
            if k in config
        },
        "config": config,
        "data": records(data),
    }


@serialized
def render(data, config, destination):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    data, spec = prepare(data, config)
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    kind, x, y, group = spec["chart_type"], config.get("x", ""), config.get("y", ""), config.get("group", "")
    width, height = float(config.get("width", 9)), float(config.get("height", 5))
    if not 3 <= width <= 16 or not 2 <= height <= 12:
        raise ValueError("图表宽度须为3—16英寸，高度2—12英寸")
    color = config.get("color", "#176c68")
    fig, ax = plt.subplots(figsize=(width, height), layout="constrained")
    try:
        if kind in ("line", "area", "bar", "scatter"):
            groups = data.groupby(group, dropna=False) if group else [("", data)]
            for label, part in groups:
                part = part.sort_values(x, kind="stable") if kind in ("line", "area") else part
                opts = {"label": str(label)}
                if not group:
                    opts["color"] = color
                if kind == "line":
                    ax.plot(part[x], part[y], **opts)
                elif kind == "area":
                    ax.fill_between(part[x], part[y], alpha=0.4, **opts)
                elif kind == "bar":
                    labels = part[x].astype(str) + (" · " + str(label) if group else "")
                    ax.bar(labels, part[y], **opts)
                else:
                    ax.scatter(part[x], part[y], alpha=0.7, **opts)
            if group:
                ax.legend(title=group)
        elif kind == "histogram":
            columns(data, [y])
            ax.hist(data[y].dropna(), bins=int(config.get("bins", 20)), color=color)
        elif kind == "box":
            columns(data, [y])
            parts = list(data.groupby(group)) if group else [(y, data)]
            ax.boxplot([p[y].dropna().to_numpy() for _, p in parts], tick_labels=[str(k) for k, _ in parts])
        elif kind == "heatmap":
            labels = data[x].astype(str).tolist() if x else [str(i) for i in data.index]
            matrix = data.drop(columns=[x] if x else []).select_dtypes(include="number")
            if matrix.empty:
                raise ValueError("热力图需要数值列")
            image = ax.imshow(matrix, aspect="auto", cmap="RdBu_r")
            ax.set_xticks(range(len(matrix.columns)), matrix.columns, rotation=30, ha="right")
            if len(labels) <= 40:
                ax.set_yticks(range(len(labels)), labels)
            fig.colorbar(image, ax=ax)
        elif kind == "network":
            import networkx as nx

            graph = nx.from_pandas_edgelist(data, x, y, create_using=nx.DiGraph)
            if len(graph) > 200:
                raise ValueError("网络预览最多200节点，请显式选择局部网络")
            nx.draw_networkx(
                graph,
                pos=nx.spring_layout(graph, seed=7),
                ax=ax,
                node_color=color,
                font_family="Microsoft YaHei",
                font_size=8,
                node_size=350,
                arrows=True,
            )
            ax.set_axis_off()
        else:
            from matplotlib.patches import Rectangle

            columns(data, [x, "open", "high", "low", "close"])
            if len(data) > 300:
                raise ValueError("蜡烛图最多300根，请先选择时间范围")
            for i, row in enumerate(data.sort_values(x).to_dict("records")):
                if row["low"] > min(row["open"], row["close"]) or row["high"] < max(row["open"], row["close"]):
                    raise ValueError("OHLC高低价关系无效")
                tone = "#bc4a43" if row["close"] >= row["open"] else color
                ax.vlines(i, row["low"], row["high"], color=tone)
                ax.add_patch(
                    Rectangle(
                        (i - 0.3, min(row["open"], row["close"])),
                        0.6,
                        max(abs(row["close"] - row["open"]), 1e-12),
                        color=tone,
                    )
                )
            ax.autoscale_view()
            ticks = np.linspace(0, len(data) - 1, min(8, len(data)), dtype=int)
            ax.set_xticks(ticks, data.sort_values(x).iloc[ticks][x].astype(str), rotation=25)
        ax.set(title=config.get("title", ""), xlabel=config.get("x_label") or x, ylabel=config.get("y_label") or y)
        if config.get("log_y"):
            ax.set_yscale("log")
        if config.get("grid", True) and kind != "network":
            ax.grid(alpha=0.15)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(destination, dpi=140)
        fig.savefig(destination.with_suffix(".svg"))
    finally:
        plt.close(fig)
    return spec


def execute(ctx, inputs, params):
    target = Path(ctx.work) / "chart.png"
    spec = render(inputs["data"], params, target)
    spec["files"] = {"png": ctx.save_file(target), "svg": ctx.save_file(target.with_suffix(".svg"))}
    return {"chart": spec}
