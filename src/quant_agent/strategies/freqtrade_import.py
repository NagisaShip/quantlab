"""Freqtrade 策略导入 / 导出。

导入：把 IStrategy 写法的策略改成能在平台内核里跑的文件——只改 import 行（指向 ``quant_agent.sdk.ft`` 兼容层），
策略类原样保留，末尾接上 initialize / handle_data。改之前先做检查：用到兼容层没有的指标、做空、杠杆、
仓位调整等写法会列出来；严重的（会让策略跑不起来的）算错误，建议用 AI 改写成平台原生写法。

导出：导入过的策略可以导回 Freqtrade：恢复原来的 import 行，去掉平台接上的部分，
并把调参后的参数写回各个 Parameter 的 default。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Any

MARK = "# ---- 以下由平台导入 Freqtrade 策略时生成，导出时会去掉 ----"
HEADER = "# FREQTRADE_IMPORTS: "

FT_NAMES = {
    "IStrategy",
    "IntParameter",
    "DecimalParameter",
    "RealParameter",
    "CategoricalParameter",
    "BooleanParameter",
    "informative",
    "merge_informative_pair",
    "stoploss_from_open",
    "stoploss_from_absolute",
    "timeframe_to_minutes",
    "timeframe_to_prev_date",
    "DataFrame",
    "Series",
    "Trade",
    "BaseParameter",
}
IGNORED_METHODS = {
    "custom_entry_price": "自定义入场价（平台按下一根开盘价成交）",
    "custom_exit_price": "自定义出场价",
    "custom_stake_amount": "自定义下单金额（平台按 max_open_trades 均分资金）",
    "order_filled": "成交回调",
    "check_entry_timeout": "挂单超时",
    "check_exit_timeout": "挂单超时",
    "confirm_trade_exit": "出场确认",
}
BLOCKING_METHODS = {
    "adjust_trade_position": "仓位调整（DCA）",
}
DP_OK = {"get_pair_dataframe", "current_whitelist", "get_analyzed_dataframe", "historic_ohlcv", "runmode"}


@dataclass
class ImportReport:
    ok: bool
    class_name: str = ""
    timeframe: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    param_space: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    smoke: dict[str, Any] | None = None
    can_short: bool = False
    uses_leverage: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            k: getattr(self, k)
            for k in (
                "ok",
                "class_name",
                "timeframe",
                "errors",
                "warnings",
                "features",
                "params",
                "param_space",
                "source",
                "smoke",
                "can_short",
                "uses_leverage",
            )
        }


def is_freqtrade(source: str) -> bool:
    return bool(re.search(r"\bIStrategy\b", source or "")) and ("populate_" in (source or ""))


def _rewrite_import(node: ast.stmt, errors: list[str], warnings: list[str]) -> str | None:
    """一条 import → 新的 import 文本；None 表示原样保留。"""

    def ft(names: list[tuple[str, str | None]]) -> str:
        return "from quant_agent.sdk.ft import " + ", ".join(f"{n} as {a}" if a and a != n else n for n, a in names)

    if isinstance(node, ast.Import):
        out: list[str] = []
        keep: list[str] = []
        for al in node.names:
            mod, alias = al.name, al.asname
            if mod in ("talib.abstract", "talib"):
                out.append(ft([("ta", alias or "talib")]))
            elif mod in ("freqtrade.vendor.qtpylib.indicators", "technical.qtpylib"):
                out.append(ft([("qtpylib", alias or mod.split(".")[0])]))
            elif mod == "pandas":
                out.append(ft([("pd", alias or "pandas")]))
            elif mod == "logging":
                out.append(ft([("logging", alias or "logging")]))
            elif mod in ("numpy", "math", "datetime", "functools", "typing", "decimal", "statistics"):
                keep.append(f"import {mod}" + (f" as {alias}" if alias else ""))
            elif mod in ("pandas_ta", "technical", "technical.indicators", "finta", "ta", "tapy", "scipy", "sklearn"):
                errors.append(f"用到了 `{mod}`：兼容层只提供 TA-Lib 与 qtpylib 的常用指标")
            else:
                errors.append(f"不支持导入 `{mod}`")
        return "\n".join(keep + out)
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        names = [(a.name, a.asname) for a in node.names]
        if mod.startswith("freqtrade.strategy") or mod in (
            "freqtrade.persistence",
            "freqtrade.exchange",
            "freqtrade.optimize.space",
        ):
            bad = [n for n, _ in names if n not in FT_NAMES]
            ok = [(n, a) for n, a in names if n in FT_NAMES]
            for n in bad:
                warnings.append(f"`{mod}.{n}` 兼容层没有，已去掉；用到它的地方会报错")
            return ft(ok) if ok else ""
        if mod in ("freqtrade.vendor.qtpylib", "technical") and any(
            n == "qtpylib" or n == "indicators" for n, _ in names
        ):
            return ft([("qtpylib", a or n) for n, a in names if n in ("qtpylib", "indicators")])
        if mod == "pandas":
            ok = [(n, a) for n, a in names if n in ("DataFrame", "Series")]
            return ft(ok) if ok else ""
        if mod in ("talib", "talib.abstract"):
            errors.append("`from talib import X` 写法不支持，请改成 `import talib.abstract as ta`")
            return ""
        if mod in (
            "numpy",
            "math",
            "datetime",
            "functools",
            "typing",
            "decimal",
            "statistics",
            "collections",
            "itertools",
            "enum",
            "dataclasses",
        ):
            return None
        if mod.startswith("freqtrade"):
            warnings.append(f"`{mod}` 兼容层没有，已去掉")
            return ""
        errors.append(f"不支持从 `{mod}` 导入")
        return ""
    return None


def _strategy_class(tree: ast.Module) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for b in node.bases:
                name = b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else ""
                if name == "IStrategy":
                    return node
    return None


def _class_attr(cls: ast.ClassDef, name: str) -> Any:
    for st in cls.body:
        targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name and getattr(st, "value", None) is not None:
                try:
                    return ast.literal_eval(st.value)
                except ValueError:
                    return None
    return None


def convert(source: str, *, smoke: bool = True) -> ImportReport:
    rep = ImportReport(ok=False)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        rep.errors.append(f"语法错误：第 {exc.lineno} 行 {exc.msg}")
        return rep
    cls = _strategy_class(tree)
    if cls is None:
        rep.errors.append("没找到继承 IStrategy 的策略类")
        return rep
    rep.class_name = cls.name
    rep.timeframe = str(_class_attr(cls, "timeframe") or "5m")

    # ---- 检查写法 ----
    methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    for m, why in BLOCKING_METHODS.items():
        if m in methods:
            rep.errors.append(f"用到了{why}（{m}）")
    for m, why in IGNORED_METHODS.items():
        if m in methods:
            rep.warnings.append(f"{m}：{why}，平台不调用")
    rep.can_short = bool(_class_attr(cls, "can_short"))
    rep.uses_leverage = "leverage" in methods
    if rep.can_short:
        rep.features.append("做空信号（仅线性永续）")
    if rep.uses_leverage:
        rep.features.append("Freqtrade leverage() 杠杆设置")
    if _class_attr(cls, "position_adjustment_enable"):
        rep.errors.append("position_adjustment_enable = True：仓位调整（DCA）不支持")
    from ..sdk.ft import SUPPORTED_TA, _Qt

    qt_ok = {n for n in dir(_Qt) if not n.startswith("_")}
    ta_alias = {"ta", "talib"}
    qt_alias = {"qtpylib"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for al in node.names:
                if al.name.startswith("talib"):
                    ta_alias.add(al.asname or al.name)
                if "qtpylib" in al.name:
                    qt_alias.add(al.asname or al.name.split(".")[0])
    used_ta: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base = node.value.id
            if base in ta_alias:
                used_ta.add(node.attr)
                if node.attr not in SUPPORTED_TA:
                    rep.errors.append(f"第 {node.lineno} 行：指标 ta.{node.attr} 兼容层还没有")
            elif base in qt_alias and node.attr not in qt_ok:
                rep.errors.append(f"第 {node.lineno} 行：qtpylib.{node.attr} 兼容层还没有")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        ):
            if node.value.attr == "dp" and node.attr not in DP_OK:
                rep.errors.append(f"第 {node.lineno} 行：self.dp.{node.attr}（实时盘口 / 行情接口）不支持")
            if node.value.attr == "wallets":
                rep.errors.append(f"第 {node.lineno} 行：self.wallets（钱包余额）不支持")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super":
            rep.errors.append(f"第 {node.lineno} 行：super() 在沙箱里不可用（多数情况下删掉这行即可）")
    rep.errors = list(dict.fromkeys(rep.errors))
    if used_ta:
        rep.features.append("TA-Lib 指标：" + "、".join(sorted(used_ta)))
    for f, label in (
        ("minimal_roi", "分段止盈 minimal_roi"),
        ("stoploss", "止损 stoploss"),
        ("trailing_stop", "移动止损 trailing_stop"),
    ):
        if _class_attr(cls, f) not in (None, False):
            rep.features.append(label)
    if "custom_stoploss" in methods:
        rep.features.append("custom_stoploss")
    if "custom_exit" in methods or "custom_sell" in methods:
        rep.features.append("custom_exit")
    if "populate_buy_trend" in methods:
        rep.features.append("旧版 buy / sell 列（populate_buy_trend）")
    if any(
        getattr(getattr(d, "func", d), "id", "") == "informative"
        for n in cls.body
        if isinstance(n, ast.FunctionDef)
        for d in n.decorator_list
    ):
        rep.features.append("@informative 大周期数据")

    # ---- 改 import 行 ----
    lines = source.splitlines()
    original: list[str] = []
    for node in sorted((n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))), key=lambda n: -n.lineno):
        new = _rewrite_import(node, rep.errors, rep.warnings)
        if new is None:
            continue
        seg = lines[node.lineno - 1 : node.end_lineno]
        original.insert(0, "\n".join(seg))
        lines[node.lineno - 1 : node.end_lineno] = new.splitlines() or [""]
    body = "\n".join(lines).rstrip() + "\n"
    # 运行时不需要 __init__ 里的 super().__init__(config)：直接去掉这一行，免得被沙箱拦下
    body = re.sub(r"^[ \t]*super\(\)\.__init__\([^)]*\)[ \t]*\n", "", body, flags=re.M)
    if not re.search(r"super\(", body):
        rep.errors = [e for e in rep.errors if "super()" not in e]

    # ---- 参数 ----
    try:
        rep.params, rep.param_space = _params(body, cls.name)
    except Exception as exc:  # 参数类定义本身有错
        rep.errors.append(f"读取参数失败：{type(exc).__name__}: {exc}")
    params = {**rep.params, "max_open_trades": 3, "ft_record": False}
    use_cs = bool(_class_attr(cls, "use_custom_stoploss")) and "custom_stoploss" in methods
    has_ce = "custom_exit" in methods or "custom_sell" in methods
    footer = [
        "",
        MARK,
        HEADER + json.dumps(original, ensure_ascii=False),
        "from quant_agent.sdk.ft import FtAdapter",
        "",
        f"PARAMS = {params!r}",
        f"PARAM_SPACE = {rep.param_space!r}",
        f"FT = FtAdapter({cls.name})",
        "",
        "",
        "def initialize(ctx):",
        "    FT.configure({k: get_param(k, v) for k, v in PARAMS.items()})",
        "    FT.record = bool(get_param('ft_record', False))",
        "    FT.initialize(ctx)",
        "",
        "",
        "def handle_data(ctx):",
        "    FT.handle_data(ctx)",
        "    if get_param('ft_record', False):",
        "        g.ft_signals = FT.signals",
    ]
    if use_cs:
        footer += ["", "", "def custom_stoploss(ctx, trade):", "    return FT.custom_stoploss(ctx, trade)"]
    if has_ce:
        footer += ["", "", "def custom_exit(ctx, trade):", "    return FT.custom_exit(ctx, trade)"]
    rep.source = body + "\n".join(footer) + "\n"

    from ..sdk.sandbox import audit

    for issue in audit(rep.source):
        rep.errors.append(issue)
    rep.errors = list(dict.fromkeys(rep.errors))
    if not rep.errors and smoke:
        rep.smoke = smoke_test(rep.source, rep.timeframe, pair="BTC/USDT:PERP" if rep.can_short else "BTC/USDT")
        if rep.smoke.get("error"):
            rep.errors.append("试运行出错：" + rep.smoke["error"])
    rep.ok = not rep.errors
    return rep


def _params(body: str, cls_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """在沙箱里执行一遍类定义，读出 hyperopt 参数。"""
    from ..sdk import load_source
    from ..sdk.ft import param_space

    mod = load_source(body + "\n\ndef initialize(ctx):\n    pass\n", name="ft_probe")
    cls = mod.fn(cls_name) if hasattr(mod, "fn") else getattr(mod, cls_name)
    return param_space(cls)


def smoke_test(source: str, timeframe: str, bars: int = 400, pair: str = "BTC/USDT") -> dict[str, Any]:
    """在合成行情上跑一小段，确认能跑通（不评价好坏）。"""
    import math as _m

    from ..data.quality import TIMEFRAME_MS
    from ..exchanges.base import Bar
    from ..sdk import load_source, run_backtest

    tf = timeframe if timeframe in TIMEFRAME_MS else "1h"
    step = TIMEFRAME_MS[tf]
    t0 = 1_700_000_000_000 - 1_700_000_000_000 % step
    out = []
    p = 100.0
    for i in range(bars):
        o = p
        p = 100 * (1 + 0.08 * _m.sin(i / 17) + 0.03 * _m.sin(i / 5.3)) + 0.02 * i
        hi, lo = max(o, p) * 1.003, min(o, p) * 0.997
        out.append(Bar(pair, tf, t0 + i * step, t0 + (i + 1) * step - 1, o, hi, lo, p, 1000 + 50 * _m.cos(i / 7)))
    try:
        res = run_backtest(load_source(source, name="ft_smoke"), {pair: out}, timeframe=tf)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    errs = list(getattr(res, "errors", []) or [])
    if errs:
        return {"error": errs[0][:300], "trades": len(res.round_trips)}
    return {"bars": bars, "timeframe": tf, "trades": len(res.round_trips), "fills": len(res.fills)}


# ---------------------------------------------------------------------- 导出
def export(source: str, params: dict[str, Any] | None = None) -> str:
    """导入过的策略 → Freqtrade 文件。params 里的值写回同名 Parameter 的 default。"""
    if MARK not in source:
        raise ValueError("这个策略不是从 Freqtrade 导入的，暂时只能导出导入过的策略")
    body, _, tail = source.partition(MARK)
    original: list[str] = []
    for line in tail.splitlines():
        if line.startswith(HEADER):
            original = json.loads(line[len(HEADER) :])
    lines = body.rstrip().splitlines()
    imports = [i for i, ln in enumerate(lines) if ln.startswith("from quant_agent.sdk.ft import")]
    # 平台写的 import 行按顺序换回原文（导入时一条换一条，顺序一致）；多出来的原 import 放到最前面
    restored = list(original)
    for i in imports:
        lines[i] = restored.pop(0) if restored else ""
    text = "\n".join(lines) + "\n"
    if restored:
        text = "\n".join(restored) + "\n" + text
    if params:
        text = _write_defaults(text, params)
    return text


def _write_defaults(text: str, params: dict[str, Any]) -> str:
    tree = ast.parse(text)
    edits: list[tuple[int, int, int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and node.targets[0].id in params
        ):
            for kw in node.value.keywords:
                if kw.arg == "default":
                    v = kw.value
                    edits.append(
                        (v.lineno, v.col_offset, v.end_col_offset or v.col_offset, repr(params[node.targets[0].id]))
                    )
    lines = text.splitlines()
    for ln, a, b, new in sorted(edits, reverse=True):
        row = lines[ln - 1]
        lines[ln - 1] = row[:a] + new + row[b:]
    return "\n".join(lines) + "\n"
