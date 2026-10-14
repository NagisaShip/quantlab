"""把研究成果接进策略：回测开始前，按时间点算好 ``factor()`` / ``predict()`` 要用到的数值。

策略里这样写：

    def rebalance(ctx):
        score = factor('资金费率拥挤反转')          # 因子库里的名字，或直接写表达式
        mu = predict('gbdt_fwd_ret_1')             # 模型库里的名字
        w = optimize('风险平价', expected=mu)       # 组合方案（见 portfolio.py）
        set_execution('自适应30分钟')               # 执行方案：回测滑点按它的成本曲线随下单规模变化

- 回测前扫描源码里的这几个调用（只认第一个参数是字符串常量的写法），用回测正在用的**同一份 K 线**组装面板，
  外部字段同样按时点对齐；模型按“滚动重训”生成预测：每隔一段用当时之前的数据重训，避免用未来数据训练出来的
  模型去回测过去。
- 结果以 {times, symbols, values} 的形式随回测请求进入沙箱进程；沙箱里只按当前 bar 的时间往回查，看不到未来。
- 沙箱进程仍然没有文件与网络权限：这里的计算都在 worker 里完成。
"""

from __future__ import annotations

import ast
from typing import Any

import numpy as np

CALLS = ("factor", "predict", "optimize", "set_execution")


def scan(source: str) -> list[tuple[str, str]]:
    """找出源码里 factor('x') / predict('x') / optimize('x') 的调用。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
        arg = node.args[0]
        if name in CALLS and isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            key = (name, arg.value)
            if key not in out:
                out.append(key)
    return out


def to_wire(times: np.ndarray, symbols: list[str], values: np.ndarray, **meta: Any) -> dict[str, Any]:
    vals = [[None if not np.isfinite(x) else round(float(x), 8) for x in row] for row in values]
    return {"times": [int(t) for t in times], "symbols": list(symbols), "values": vals, **meta}


def prepare(
    source: str,
    bars: dict[str, list[Any]],
    timeframe: str,
    *,
    store: Any,
    hub: Any = None,
    data_source: str = "auto",
    stop: Any = None,
) -> tuple[dict[str, Any], list[str]]:
    """返回 (research 字典, 警告)。research 的键是 "factor:名字" / "predict:名字" / "optimize:名字"。"""
    calls = scan(source)
    if not calls:
        return {}, []
    from .factor_expr import FactorExprError, fields_used, parse
    from .factor_lib import FactorLibrary
    from .panel import PanelError, panel_from_bars

    warnings: list[str] = []
    out: dict[str, Any] = {}
    flib = FactorLibrary(store)
    exprs: dict[str, str] = {}
    model_specs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    schemes: dict[str, dict[str, Any]] = {}
    fields: set[str] = set()
    for kind, name in calls:
        if kind == "factor":
            rec = flib.by_name(name)
            expr = rec["expression"] if rec else name
            try:
                fields.update(fields_used(parse(expr)))
                exprs[name] = expr
            except FactorExprError as exc:
                warnings.append(f"factor('{name}')：因子库里没有这个名字，也不是合法表达式（{exc}）")
        elif kind == "predict":
            from .model_lib import ModelLibrary
            from .models import fields_for

            mlib = ModelLibrary(store)
            rec = mlib.by_name(name)
            if rec is None:
                warnings.append(f"predict('{name}')：模型库里没有这个模型")
                continue
            model_specs[name] = (rec, rec["spec"])
            fields.update(fields_for(rec["spec"]))
        elif kind == "optimize":
            try:
                from .portfolio_lib import PortfolioLibrary
            except ImportError:  # pragma: no cover
                warnings.append("这个版本还不支持 optimize()")
                continue
            rec = PortfolioLibrary(store).by_name(name)
            if rec is None:
                warnings.append(f"optimize('{name}')：组合与风控里没有这个配置方案")
                continue
            schemes[name] = rec
        elif kind == "set_execution":
            try:
                from .execution_lib import ExecutionLibrary
            except ImportError:  # pragma: no cover
                continue
            rec = ExecutionLibrary(store).by_name(name)
            if rec is None or not rec.get("cost_model"):
                warnings.append(f"set_execution('{name}')：执行与监控里没有这个执行方案（或方案还没有成本曲线）")
                continue
            out[f"execution:{name}"] = {
                "name": name,
                "cost_model": rec["cost_model"],
                "fees": (rec.get("config") or {}).get("fees") or {},
                "horizon": (rec.get("config") or {}).get("horizon"),
            }
    if not (exprs or model_specs or schemes):
        return out, warnings
    try:
        panel = panel_from_bars(bars, hub, timeframe, fields=sorted(fields), source=data_source)
    except PanelError as exc:
        return {}, [*warnings, f"研究数据准备失败：{exc}"]
    warnings += panel.warnings
    from .factor_expr import evaluate

    for name, expr in exprs.items():
        F = evaluate(parse(expr), panel.data)
        out[f"factor:{name}"] = to_wire(panel.times, panel.symbols, F, expression=expr)
    for name, (_rec, spec) in model_specs.items():
        from .models import ModelError, rolling_predict

        try:
            P, info = rolling_predict({**spec, "symbols": panel.symbols}, panel, stop=stop)
        except ModelError as exc:
            warnings.append(f"predict('{name}')：{exc}")
            continue
        out[f"predict:{name}"] = to_wire(
            panel.times,
            panel.symbols,
            P,
            model=spec["model"],
            label=spec["label"],
            retrain_every=info["retrain_every"],
            first=info["first"],
        )
        warnings.append(
            f"predict('{name}') 按滚动重训生成：每 {info['retrain_every']} 根重训一次，最早一段训练窗口内没有预测"
        )
    for name, rec in schemes.items():
        out[f"optimize:{name}"] = {"config": rec.get("config") or {}, "name": name}
    return out, warnings


__all__ = ["CALLS", "prepare", "scan", "to_wire"]
