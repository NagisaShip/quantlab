"""研究产物 → Python 代码：因子、预测模型、组合方案、执行方案都能落到一段可读、可运行的 Python。

- 因子：表达式逐节点翻译成 numpy 代码（面板里每个字段是 [时间, 标的] 数组），算子实现原样拷自平台，
  结果与平台逐元素一致，只依赖 numpy，可以脱离平台独立运行；
- 预测模型 / 组合 / 执行：规格原样写成 Python 字典，再调用平台同一套引擎复现评估结果；
  模型另外给出每个特征的 pandas 实现，方便核对特征口径。
"""

from __future__ import annotations

import json
from typing import Any

from . import factor_expr as FX

# 平台求值用的底层函数：原样拷进生成的代码，保证口径完全一致（窗口 80% 有效值、并列平均名次、EMA 满窗后出值…）
_BASE = ("_quiet", "_rolling", "_nanwindow", "_delay", "_ts_rank", "_ts_corr", "_cs_rank", "_cs", "_cs_zscore",
         "_decay_linear", "_ema", "_call")
_GLUE = """

def _fin(x):
    x = np.asarray(x, dtype=float)
    x[~np.isfinite(x)] = np.nan
    return x


def OP(name, args, P):
    with np.errstate(all="ignore"), _quiet():
        return _fin(_call(name, args, P))


def BIN(op, a, b):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    with np.errstate(all="ignore"):
        if op == "+":
            out = a + b
        elif op == "-":
            out = a - b
        elif op == "*":
            out = a * b
        elif op == "/":
            out = a / np.where(b == 0, np.nan, b)
        elif op == "^":
            out = np.power(a, b)
        elif op in ("<", ">", "<=", ">=", "==", "!="):
            cmp = {"<": np.less, ">": np.greater, "<=": np.less_equal, ">=": np.greater_equal,
                   "==": np.equal, "!=": np.not_equal}[op](a, b).astype(float)
            out = np.where(np.isnan(a) | np.isnan(b), np.nan, cmp)
        elif op == "&&":
            out = np.where(np.isnan(a) | np.isnan(b), np.nan, ((a != 0) & (b != 0)).astype(float))
        else:  # ||
            out = np.where(np.isnan(a) | np.isnan(b), np.nan, ((a != 0) | (b != 0)).astype(float))
    return _fin(out)


def NOT(a):
    return np.where(np.isnan(a), np.nan, (a == 0).astype(float))
"""


def _base_source() -> str:
    import inspect
    import textwrap

    return "\n\n".join(textwrap.dedent(inspect.getsource(getattr(FX, n))).rstrip() for n in _BASE) + "\n" + _GLUE


def _py(n: FX.Node, shape: str = "P['close'].shape") -> str:
    """表达式节点 → 调用平台同名算子的 Python 表达式（numpy 面板：每个字段是 [时间, 标的] 数组）。"""
    if n.kind == "num":
        v = float(n.value)
        return f"np.full({shape}, {repr(v)})"
    if n.kind == "field":
        return f'P["{n.value}"]'
    if n.kind == "neg":
        return f"(-{_py(n.args[0], shape)})"
    if n.kind == "not":
        return f"NOT({_py(n.args[0], shape)})"
    if n.kind == "bin":
        return f'BIN("{n.value}", {_py(n.args[0], shape)}, {_py(n.args[1], shape)})'
    name = str(n.value)
    sig = FX.OPERATORS[name]["sig"].split(",")
    args = []
    for kind, a in zip(sig, n.args, strict=False):
        if kind == "d":
            args.append(str(int(a.value)))
        elif kind == "k":
            args.append(repr(float(a.value) if a.kind == "num" else -float(a.args[0].value)))
        else:
            args.append(_py(a, shape))
    return f'OP("{name}", [{", ".join(args)}], P)'


def _fn_name(name: str) -> str:
    fn = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_") or "factor"
    return f"f_{fn}" if fn[0].isdigit() else fn


_DEMO = """

if __name__ == "__main__":
    # 示例：随机游走价格演示（研究时换成真实面板：每个字段是 [时间, 标的] 的 numpy 数组）
    import warnings

    warnings.simplefilter("ignore", RuntimeWarning)
    rng = np.random.default_rng(0)
    T, N = 500, 3
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (T, N)), axis=0))
    P = {"close": close, "high": close * 1.002, "low": close * 0.998, "open": np.vstack([close[:1], close[:-1]]),
         "volume": rng.uniform(1, 2, (T, N))}
    P["ret"] = np.vstack([np.full((1, N), np.nan), close[1:] / close[:-1] - 1])
    P["vwap"] = (P["high"] + P["low"] + close) / 3
    P["amount"] = close * P["volume"]
    P["mkt_ret"] = np.repeat(np.nanmean(P["ret"], axis=1, keepdims=True), N, axis=1)
"""


def factor_python(expr: str, *, name: str = "factor", horizon: int | None = None) -> str:
    """因子表达式 → 独立可运行的 numpy 代码（算子实现原样取自平台，结果与平台逐元素一致）。"""
    node = FX.parse(expr)
    fields = FX.fields_used(node)
    fn = _fn_name(name)
    field_doc = "\n".join(f"    - {f}: {FX.FIELDS.get(f, {}).get('label', '自定义数据')}" for f in fields)
    return "\n".join([
        f'"""因子「{name}」的 Python 实现（QuantLab 从表达式自动生成，算子实现与平台完全一致）。',
        "",
        f"表达式：{expr}",
        f"输入面板 P：字段名 → [时间, 标的] 的 numpy 数组。用到的字段：\n{field_doc}",
        f"最长回看：{FX.max_lookback(node)} 根 K 线" + (f"；预测周期：{horizon} 根" if horizon else ""),
        '"""',
        "import math",
        "",
        "import numpy as np",
        "",
        "",
        _base_source().rstrip(),
        "",
        "",
        f"def {fn}(P: dict) -> np.ndarray:",
        f"    return {_py(node)}",
        _DEMO.rstrip(),
        f"    missing = [f for f in {fields!r} if f not in P]",
        "    if missing:",
        "        print('示例面板里没有这些字段，请用真实数据：', missing)",
        "    else:",
        f"        print({fn}(P)[-5:])",
        "",
    ])


def _spec_literal(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=4).replace(": true", ": True").replace(": false", ": False").replace(": null", ": None")


def model_python(spec: dict[str, Any], *, name: str = "model") -> str:
    feats = []
    for f in spec.get("features") or []:
        try:
            feats.append((f.get("name") or f["expression"], f["expression"], _py(FX.parse(f["expression"]))))
        except Exception:
            continue
    feat_lines = "\n".join(f'        "{n}": {body},  # {expr}' for n, expr, body in feats)
    return "\n".join([
        f'"""预测模型「{name}」：训练规格与特征的 Python 实现（QuantLab 自动生成）。',
        "",
        f"学习器：{spec.get('model')}；标签：{spec.get('label')}；预测周期：{spec.get('horizon')} 根 K 线；"
        f"周期 {spec.get('timeframe')}；标的 {', '.join(spec.get('symbols') or []) or '项目默认'}",
        "评估口径：滚动样本外（walk-forward），与线性基线对比，并做特征泄漏检查。",
        '"""',
        "import math",
        "",
        "import numpy as np",
        "",
        f"SPEC = {_spec_literal(spec)}",
        "",
        "",
        _base_source().rstrip(),
        "",
        "",
        "def features(P: dict) -> dict:",
        '    """每个特征一张 [时间, 标的] 数组，口径与平台一致。"""',
        "    return {",
        feat_lines,
        "    }",
        "",
        "",
        "def train_on_platform():",
        '    """在 QuantLab 环境里用同一套引擎复现训练与评估（滚动样本外、基线、泄漏检查、重要性）。"""',
        "    from quant_agent.api.deps import get_services",
        "    from quant_agent.api.routes_lab import _datahub",
        "    from quant_agent.research import models as M",
        "    from quant_agent.research.panel import build_panel",
        "",
        "    spec = M.normalize_spec(SPEC)",
        "    s = get_services()",
        "    panel = build_panel(s.market, _datahub(), spec['symbols'] or ['BTC/USDT', 'ETH/USDT'], spec['timeframe'],",
        "                        fields=M.fields_for(spec), bars_limit=spec['bars_limit'])",
        "    out = M.train_and_evaluate(spec, panel)",
        "    print((out.get('report') or {}).get('summary'))",
        "    return out",
        "",
    ])


def portfolio_python(cfg: dict[str, Any], *, name: str = "portfolio") -> str:
    return "\n".join([
        f'"""组合方案「{name}」（QuantLab 自动生成）：优化器 {cfg.get("optimizer")}、风险模型 {cfg.get("risk_model")}、'
        f'预期收益 {cfg.get("expected")}，每 {cfg.get("rebalance")} 根再平衡，回看 {cfg.get("lookback")} 根。',
        "",
        "约束与风控规则原样写在 CONFIG 里；evaluate() 用平台同一套引擎复现评估（含成本、换手与压力测试）。",
        '"""',
        f"CONFIG = {_spec_literal(cfg)}",
        "",
        "",
        "def evaluate(cost_bps: float = 10.0):",
        "    from quant_agent.api.deps import get_services",
        "    from quant_agent.api.routes_lab import _datahub",
        "    from quant_agent.research import portfolio as PF",
        "    from quant_agent.research.panel import build_panel",
        "",
        "    cfg = PF.normalize_config(CONFIG)",
        "    s = get_services()",
        "    symbols = cfg.get('symbols') or ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']",
        "    panel = build_panel(s.market, _datahub(), symbols, cfg.get('timeframe') or '4h', fields=['close', 'volume'])",
        "    report = PF.evaluate_scheme(cfg, panel, cost_bps=cost_bps)",
        "    print(report.get('summary'))",
        "    return report",
        "",
        "",
        "def target_weights(returns):",
        '    """给定最近 lookback 根的收益矩阵（时间 × 标的，numpy），按方案算目标权重。"""',
        "    import numpy as np",
        "    from quant_agent.research import portfolio as PF",
        "",
        "    cfg = PF.normalize_config(CONFIG)",
        "    R = np.asarray(returns, dtype=float)[-cfg['lookback']:]",
        "    S = PF.covariance(R, cfg['risk_model'])",
        "    mu = R.mean(axis=0)",
        "    return PF.solve(cfg, S, mu, R)",
        "",
    ])


def execution_python(cfg: dict[str, Any], *, name: str = "execution") -> str:
    return "\n".join([
        f'"""执行方案「{name}」（QuantLab 自动生成）：算法 {cfg.get("algo")}。全程仿真，没有真实下单。',
        "",
        "CONFIG 是方案规格；simulate() 用平台同一套撮合与成本模型（盘口、冲击、TCA）复现评估。",
        '"""',
        f"CONFIG = {_spec_literal(cfg)}",
        "",
        "",
        "def simulate(symbols=None, steps: int = 3000):",
        "    from quant_agent.api.deps import get_services",
        "    from quant_agent.research import execution as EX",
        "    from quant_agent.research.execution_lib import ExecutionLibrary",
        "    from quant_agent.research.execution_lib import load_tapes",
        "",
        "    cfg = EX.normalize_config(CONFIG)",
        "    s = get_services()",
        "    tapes = load_tapes(s.market, ExecutionLibrary(s.store), symbols or ['BTC/USDT'], source='kline', steps=steps)",
        "    report = EX.evaluate_scheme(cfg, tapes)",
        "    print(report.get('summary'))",
        "    return report",
        "",
    ])


def to_python(kind: str, artifact: Any, *, name: str = "") -> str:
    """kind: factor | model | portfolio | execution；artifact 是表达式或规格（dict / JSON 字符串）。"""
    if kind == "factor":
        return factor_python(str(artifact), name=name or "factor")
    spec = artifact if isinstance(artifact, dict) else json.loads(str(artifact or "{}"))
    if kind == "model":
        return model_python(spec, name=name or "model")
    if kind == "portfolio":
        return portfolio_python(spec, name=name or "portfolio")
    if kind == "execution":
        return execution_python(spec, name=name or "execution")
    raise ValueError(f"不支持的类型 {kind}")
