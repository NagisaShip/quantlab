"""规则 ↔ 代码 统一：把规则 DSL v2 生成为带结构化中文注释的 Python 策略，并用 AST 判断代码是否仍是规则。

生成的代码顶部带两类结构化注释::

    # @rule
    # { ...规则 JSON（不含 params）... }
    # @endrule
    # @param fast = 12  # 指标 ma_fast（SMA）的周期

- `@rule` 块是规则本身（DSL v2，参数单独写在 `@param` 行里）；
- `@param` 行是规则参数的说明，真正参与运行的是代码里的 `PARAMS` 字典。

判断"代码是否仍是规则"（`code_to_rule`）：

1. 找到 `@rule` 块与 `@param` 行，还原出规则并校验；
2. 用同一份规则重新生成代码，与用户的代码做 **AST 比较**——注释、空白、文档字符串不参与比较，
   `PARAMS` 字典里的取值也不参与比较（只要求键一致），参数改动视为"规则参数变了"；
3. AST 完全一致 → 仍是规则，返回的规则以 `PARAMS` 的实际取值为准；
   不一致 → 用户改过的代码超出了规则能表达的范围，按代码策略处理。

生成的代码只用平台 SDK 的公开 API，与其他代码策略一样在沙箱里加载、走同一套回测引擎；
指标算法、预热长度、离场优先、轮动的排序与先卖后买都与 DSL 解释器（`dsl.DslModule`）同口径。
"""

from __future__ import annotations

import ast
import json
import math
import re
from typing import Any

from .dsl import (
    COMPARE_OPS,
    CROSS_OPS,
    DSL_VERSION,
    INDICATOR_TYPES,
    DslError,
    validate,
)

RULE_BEGIN = "@rule"
RULE_END = "@endrule"
CODE_ONLY_REASON = "代码已改动，超出规则能表达的范围，按代码策略处理"

# @rule 块里规则 JSON 的字段顺序（只影响可读性，不影响判断）
_KEY_ORDER = (
    "dsl_version",
    "name",
    "description",
    "instrument",
    "instruments",
    "timeframe",
    "indicators",
    "rank",
    "entry",
    "exit",
    "position",
    "risk",
)
_PY_OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_PRICE_CN = {"open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价"}
_PARAM_LINE = re.compile(r"^#\s*@param\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^#]*?)\s*(?:#\s*(.*))?$")

# 指标辅助函数：只输出规则里用到的那几个。算法与 dsl.compute_series 逐行一致。
_HELPERS: dict[str, str] = {
    "sma": """def sma(xs, p):
    # 简单移动平均：最近 p 根的算术平均；不足 p 根的位置为 None
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= p:
            s -= xs[i - p]
        if i >= p - 1:
            out[i] = s / p
    return out
""",
    "ema": """def ema(xs, p):
    # 指数移动平均：以前 p 根的简单平均为起点，平滑系数 2 / (p + 1)
    out = [None] * len(xs)
    if len(xs) < p:
        return out
    a = 2 / (p + 1)
    v = sum(xs[:p]) / p
    out[p - 1] = v
    for i in range(p, len(xs)):
        v = a * xs[i] + (1 - a) * v
        out[i] = v
    return out
""",
    "rsi": """def rsi(xs, p):
    # 相对强弱指数（Wilder 平滑）：0–100，只有上涨时为 100，完全不动时为 50
    out = [None] * len(xs)
    if len(xs) <= p:
        return out
    gains = [max(0.0, xs[i] - xs[i - 1]) for i in range(1, len(xs))]
    losses = [max(0.0, xs[i - 1] - xs[i]) for i in range(1, len(xs))]
    ag = sum(gains[:p]) / p
    al = sum(losses[:p]) / p

    def val(g, l_):
        if l_ == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l_)

    out[p] = val(ag, al)
    for i in range(p + 1, len(xs)):
        ag = (ag * (p - 1) + gains[i - 1]) / p
        al = (al * (p - 1) + losses[i - 1]) / p
        out[i] = val(ag, al)
    return out
""",
    "atr": """def atr(h, lo, c, p):
    # 平均真实波幅（Wilder 平滑）：真实波幅 = max(高-低, |高-昨收|, |低-昨收|)
    n = len(c)
    out = [None] * n
    if n <= p:
        return out
    tr = [h[0] - lo[0]] + [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, n)]
    v = sum(tr[1:p + 1]) / p
    out[p] = v
    for i in range(p + 1, n):
        v = (v * (p - 1) + tr[i]) / p
        out[i] = v
    return out
""",
    "highest": """def highest(xs, p):
    # 通道上沿：最近 p 根的最高值
    return [max(xs[i - p + 1:i + 1]) if i >= p - 1 else None for i in range(len(xs))]
""",
    "lowest": """def lowest(xs, p):
    # 通道下沿：最近 p 根的最低值
    return [min(xs[i - p + 1:i + 1]) if i >= p - 1 else None for i in range(len(xs))]
""",
    "return": """def pct_return(xs, p):
    # 区间收益率：当前值 / p 根之前的值 - 1
    return [xs[i] / xs[i - p] - 1 if i >= p and xs[i - p] else None for i in range(len(xs))]
""",
}


# ---------------------------------------------------------------------------
# 规则 → 代码
# ---------------------------------------------------------------------------
def _lit(v: Any) -> str:
    """数值字面量：整数保持整数，浮点用最短可往返表示。"""
    return repr(v)


def _val(v: Any) -> str:
    """规则里的取值：`$名字` → PARAMS["名字"]，其余为数值字面量。"""
    if isinstance(v, str) and v.startswith("$"):
        return f'PARAMS["{v[1:]}"]'
    return _lit(v)


def _val_cn(v: Any) -> str:
    if isinstance(v, str) and v.startswith("$"):
        return f"参数 {v[1:]}"
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _ordered(spec: dict[str, Any]) -> dict[str, Any]:
    out = {k: spec[k] for k in _KEY_ORDER if k in spec}
    out.update({k: v for k, v in spec.items() if k not in out})
    return out


class _Gen:
    """把一份已校验的规则翻译成 Python 源码片段。"""

    def __init__(self, spec: dict[str, Any], resolved: dict[str, Any]) -> None:
        self.spec = spec
        self.r = resolved
        self.inds = resolved["indicators"]
        self.raw_inds = spec.get("indicators") or {}
        self.loop = 0  # consecutive 循环变量编号

    # ---- 中文描述（写进注释） ----
    def operand_cn(self, o: dict[str, Any]) -> str:
        if "const" in o:
            base = _val_cn(o["const"])
        elif "price" in o:
            base = _PRICE_CN[o["price"]]
        else:
            i = self.inds[o["ref"]]
            base = f"{o['ref']}（{i['type'].upper()}）"
        if o.get("lag"):
            base = f"{o['lag']} 根前的{base}"
        if o.get("mul", 1) != 1:
            base = f"{base} × {o['mul']:g}"
        return base

    def cond_cn(self, c: dict[str, Any] | None) -> str:
        if c is None:
            return "（无）"
        op = c["op"]
        if op in COMPARE_OPS:
            return f"{self.operand_cn(c['left'])} {COMPARE_OPS[op]} {self.operand_cn(c['right'])}"
        if op in CROSS_OPS:
            return f"{self.operand_cn(c['left'])} {CROSS_OPS[op]} {self.operand_cn(c['right'])}"
        if op == "all":
            return "（" + " 且 ".join(self.cond_cn(x) for x in c["conds"]) + "）"
        if op == "any":
            return "（" + " 或 ".join(self.cond_cn(x) for x in c["conds"]) + "）"
        if op == "not":
            return f"并非 {self.cond_cn(c['cond'])}"
        return f"连续 {c['bars']} 根 bar 满足 {self.cond_cn(c['cond'])}"

    # ---- 表达式 ----
    def operand_py(self, o: dict[str, Any], lag: int, off: str) -> str:
        if "const" in o:
            return _val(o["const"])
        idx = f"{-1 - int(o.get('lag', 0)) - lag}{off}"
        base = f'bars["{o["price"]}"][{idx}]' if "price" in o else f'ind["{o["ref"]}"][{idx}]'
        mul = o.get("mul", 1)
        return base if mul == 1 else f"{base} * {_lit(mul)}"

    def cond_py(self, c: dict[str, Any], off: str = "") -> str:
        op = c["op"]
        if op in _PY_OPS:
            return f"{self.operand_py(c['left'], 0, off)} {_PY_OPS[op]} {self.operand_py(c['right'], 0, off)}"
        if op in CROSS_OPS:
            l0, r0 = self.operand_py(c["left"], 1, off), self.operand_py(c["right"], 1, off)
            l1, r1 = self.operand_py(c["left"], 0, off), self.operand_py(c["right"], 0, off)
            if op == "cross_above":
                return f"({l0} <= {r0} and {l1} > {r1})"
            return f"({l0} >= {r0} and {l1} < {r1})"
        if op in ("all", "any"):
            joiner = " and " if op == "all" else " or "
            return "(" + joiner.join(self.cond_py(x, off) for x in c["conds"]) + ")"
        if op == "not":
            return f"not {self.wrap(self.cond_py(c['cond'], off))}"
        # consecutive：最近 N 根 bar（往前数）都满足
        var = f"k{self.loop}"
        self.loop += 1
        inner = self.cond_py(c["cond"], f"{off} - {var}")
        return f"all({inner} for {var} in range({int(c['bars'])}))"

    @staticmethod
    def wrap(expr: str) -> str:
        return expr if expr.startswith("(") and expr.endswith(")") else f"({expr})"

    def signal_fn(self, fn: str, title: str, c: dict[str, Any] | None) -> str:
        """入场 / 离场判断函数。顶层的 all / any 拆成多行，每个子条件一行中文注释。"""
        head = f"def {fn}(bars, ind):\n    # {title}：{self.cond_cn(c)}\n"
        if c is None:
            return head + "    # 规则没有设置该条件，始终不满足（只靠止损 / 止盈退出）\n    return False\n"
        self.loop = 0
        if c["op"] in ("all", "any") and len(c["conds"]) > 1:
            joiner = "and" if c["op"] == "all" else "or"
            lines = [head, "    return (\n"]
            for i, x in enumerate(c["conds"]):
                lines.append(f"        # {'全部满足' if joiner == 'and' else '任一满足'} {i + 1}：{self.cond_cn(x)}\n")
                prefix = "" if i == 0 else f"{joiner} "
                lines.append(f"        {prefix}{self.wrap_if_mixed(self.cond_py(x))}\n")
            lines.append("    )\n")
            return "".join(lines)
        return head + f"    return {self.cond_py(c)}\n"

    def wrap_if_mixed(self, expr: str) -> str:
        # 子条件里若有顶层 and / or，加括号避免与外层运算优先级混淆
        if (" and " in expr or " or " in expr) and not (expr.startswith("(") and expr.endswith(")")):
            if not expr.startswith("all("):
                return f"({expr})"
        return expr

    # ---- 指标 ----
    def indicator_line(self, name: str) -> str:
        i = self.inds[name]
        raw_period = (self.raw_inds.get(name) or {}).get("period", i["period"])
        p = _val(raw_period)
        t, src = i["type"], i["source"]
        if t == "atr":
            expr = f'atr(bars["high"], bars["low"], bars["close"], {p})'
            note = f"ATR，周期={_val_cn(raw_period)}"
        else:
            fn = "pct_return" if t == "return" else t
            expr = f'{fn}(bars["{src}"], {p})'
            note = f"{INDICATOR_TYPES[t]}，{_PRICE_CN[src]}，周期={_val_cn(raw_period)}"
        return f'        "{name}": {expr},  # {note}\n'

    def lookback_expr(self) -> str:
        """预热长度：与 DSL 的 window 同口径（最长指标回看 + 条件回看 + 余量）。"""
        parts = []
        for name, i in self.inds.items():
            raw_period = (self.raw_inds.get(name) or {}).get("period", i["period"])
            p = _val(raw_period)
            parts.append(f"{p} * 3 + 1" if i["type"] in ("ema", "rsi", "atr") else f"{p} + 1")
        return f"max({', '.join(parts)})" if len(parts) > 1 else parts[0]


def _rule_json_lines(rule: dict[str, Any]) -> list[str]:
    """@rule 块里的 JSON：每个顶层字段一行，指标与组合条件再拆一层，便于阅读。"""
    dump = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731
    items = list(rule.items())
    lines = ["{"]
    for n, (k, v) in enumerate(items):
        comma = "," if n < len(items) - 1 else ""
        if k == "indicators" and isinstance(v, dict) and v:
            lines.append(f'  "{k}": {{')
            subs = list(v.items())
            for m, (ik, iv) in enumerate(subs):
                lines.append(f"    {dump(ik)}: {dump(iv)}" + ("," if m < len(subs) - 1 else ""))
            lines.append("  }" + comma)
        elif k in ("entry", "exit") and isinstance(v, dict) and isinstance(v.get("conds"), list) and v["conds"]:
            lines.append(f'  "{k}": {{"op": {dump(v.get("op"))}, "conds": [')
            for m, c in enumerate(v["conds"]):
                lines.append(f"    {dump(c)}" + ("," if m < len(v["conds"]) - 1 else ""))
            extra = {kk: vv for kk, vv in v.items() if kk not in ("op", "conds")}
            tail = "".join(f", {dump(kk)}: {dump(vv)}" for kk, vv in extra.items())
            lines.append(f"  ]{tail}}}" + comma)
        else:
            lines.append(f'  "{k}": {dump(v)}{comma}')
    lines.append("}")
    return lines


def _param_usage(spec: dict[str, Any]) -> dict[str, list[str]]:
    """每个参数在规则里的用途，写进 @param 行的中文说明。"""
    use: dict[str, list[str]] = {}

    def add(v: Any, text: str) -> None:
        if isinstance(v, str) and v.startswith("$"):
            use.setdefault(v[1:], [])
            if text not in use[v[1:]]:
                use[v[1:]].append(text)

    for name, ind in (spec.get("indicators") or {}).items():
        if isinstance(ind, dict):
            add(ind.get("period"), f"指标 {name}（{str(ind.get('type', '')).upper()}）的周期")

    def walk(c: Any, where: str) -> None:
        if not isinstance(c, dict):
            return
        for side in ("left", "right"):
            o = c.get(side)
            if isinstance(o, dict):
                add(o.get("const"), f"{where}中的比较值")
        for x in c.get("conds") or []:
            walk(x, where)
        walk(c.get("cond"), where)

    walk(spec.get("entry"), "入场条件")
    walk(spec.get("exit"), "离场条件")
    add((spec.get("position") or {}).get("value"), "目标仓位数值")
    return use


def rule_to_code(spec: dict[str, Any]) -> str:
    """把一份规则 DSL v2 生成为带 `@rule` / `@param` 注释、逐段中文说明的 Python 策略源码。

    规则不合法时抛 DslError（逐项问题），与 /api/dsl/validate 同口径。
    同一份规则总是生成完全相同的代码，`code_to_rule` 依赖这一点做 AST 比较。
    """
    compiled = validate(spec)
    raw = json.loads(json.dumps(spec, ensure_ascii=False))  # 深拷贝，保留 $参数 引用
    r = compiled.resolved
    params = dict(raw.get("params") or {})
    g = _Gen(raw, r)
    rotation = bool(r.get("rotation"))
    pos, risk = r["position"], r["risk"]
    name = str(raw.get("name") or "规则策略")
    safe_name = re.sub(r"\s+", " ", name).replace("\\", "").replace('"', "＂")

    rule_json = {k: v for k, v in _ordered(raw).items() if k != "params"}
    out: list[str] = []
    out.append(f'"""{safe_name}：由规则生成的策略代码。\n\n')
    out.append("保持代码与顶部 @rule 规则一致时，可以切回规则视图编辑；\n")
    out.append("改动超出规则能表达的范围后，按代码策略处理。\n")
    out.append('"""\n\n')

    # ---- 结构化注释：规则与参数 ----
    out.append(f"# {RULE_BEGIN}\n")
    for line in _rule_json_lines(rule_json):
        out.append(f"# {line}\n")
    out.append(f"# {RULE_END}\n")
    usage = _param_usage(raw)
    for k, v in params.items():
        note = "；".join(usage.get(k, [])) or "规则中未使用的参数"
        out.append(f"# @param {k} = {_lit(v)}  # {note}\n")
    out.append("\n")

    # ---- 参数 ----
    out.append("# 可调参数：参数表单与参数优化从这里读取；改这里的数值仍然是同一条规则\n")
    if params:
        out.append("PARAMS = {\n")
        for k, v in params.items():
            note = "；".join(usage.get(k, [])) or "规则中未使用的参数"
            out.append(f'    "{k}": {_lit(v)},  # {note}\n')
        out.append("}\n\n")
    else:
        out.append("PARAMS = {}\n\n")

    # ---- 标的与常量 ----
    out.append(f"# 行情周期：{r['timeframe']}（在每根 bar 收盘后判断，下一根开盘成交）\n")
    if rotation:
        rank = r["rank"]
        out.append(f"# 标的池：规则声明的 {len(r['instruments'])} 个标的\n")
        out.append(f"INSTRUMENTS = {json.dumps(r['instruments'], ensure_ascii=False)}\n")
        out.append(f"# 轮动：按指标 {rank['by']} 排序取前 {rank['top']} 名\n")
        out.append(f'RANK_BY = "{rank["by"]}"\n')
        out.append(f"RANK_TOP = {rank['top']}\n")
        out.append(f"# 排序方向：{'desc 得分高的优先' if rank['order'] == 'desc' else 'asc 得分低的优先'}\n")
        out.append(f'RANK_ORDER = "{rank["order"]}"\n')
        out.append(f"# 仓位分配：{'equal 前 N 名等权' if rank['weighting'] == 'equal' else 'score 按得分占比分配'}\n")
        out.append(f'RANK_WEIGHTING = "{rank["weighting"]}"\n')
        out.append("# 入选门槛：得分必须大于该值（None 表示不设门槛）\n")
        out.append(f"MIN_SCORE = {_lit(rank['min_score']) if rank.get('min_score') is not None else 'None'}\n\n")
    else:
        out.append("# 交易标的（运行配置里没有它时，退回标的池的第一个）\n")
        out.append(f'SECURITY = "{r["instrument"]}"\n\n')
    out.append('FIELDS = ["open", "high", "low", "close"]\n\n\n')

    # ---- 指标辅助函数 ----
    used = []
    for i in r["indicators"].values():
        if i["type"] not in used:
            used.append(i["type"])
    for t in [t for t in INDICATOR_TYPES if t in used]:
        out.append(_HELPERS[t])
        out.append("\n\n")

    extra = compiled.warmup_bars + 2 - _static_lookback(r)
    out.append("def bars_needed():\n")
    out.append("    # 需要的已收盘 bar 数：最长指标回看 + 条件回看（lag / 交叉 / 连续）+ 余量\n")
    out.append(f"    return {g.lookback_expr()} + {extra}\n\n\n")

    out.append("def indicators(bars):\n")
    out.append("    # 计算规则里定义的全部指标序列（只用已收盘的 bar，不读未来）\n")
    out.append("    return {\n")
    for nm in r["indicators"]:
        out.append(g.indicator_line(nm))
    out.append("    }\n\n\n")

    out.append(g.signal_fn("entry_signal", "入场条件", raw.get("entry")))
    out.append("\n\n")
    out.append(g.signal_fn("exit_signal", "离场条件", raw.get("exit")))
    out.append("\n\n")

    # ---- initialize ----
    out.append("def initialize(ctx):\n")
    out.append("    # 仓位约束：资金不足的处理、下单数量精度、单个标的的最大仓位\n")
    out.append(f'    set_option("insufficient_funds", "{pos["insufficient_funds"]}")\n')
    out.append(f'    set_option("qty_step", {_lit(pos["qty_step"])})\n')
    out.append(f'    set_option("max_position_weight", {_lit(pos["max_weight"])})\n')
    if risk["max_trades_per_day"]:
        out.append("    # 风控：每日最多成交笔数\n")
        out.append(f'    set_option("max_trades_per_day", {_lit(risk["max_trades_per_day"])})\n')
    if risk["drawdown_pause"]:
        out.append("    # 风控：回撤超过阈值后暂停开仓\n")
        out.append(f'    set_option("drawdown_pause", {_lit(risk["drawdown_pause"])})\n')
    brackets = []
    if risk["stop_loss"]:
        brackets.append(("set_stop_loss", risk["stop_loss"], "止损：相对持仓均价下跌"))
    if risk["take_profit"]:
        brackets.append(("set_take_profit", risk["take_profit"], "止盈：相对持仓均价上涨"))
    if rotation:
        if brackets:
            out.append("    # 止损 / 止盈覆盖标的池里的每一个标的（同一根 bar 同时触及时止损优先）\n")
            out.append("    for s in universe(ctx):\n")
            for fn, v, cn in brackets:
                out.append(f"        {fn}(s, {_lit(v)})  # {cn} {v:.1%}\n")
        out.append("    # 上一根 bar 的目标权重；检查点恢复时清空，让第一根 bar 重新对齐仓位\n")
        out.append("    g.last_targets = {}\n")
    else:
        for fn, v, cn in brackets:
            out.append(f"    {fn}(pick_security(ctx), {_lit(v)})  # {cn} {v:.1%}\n")
    out.append("\n\n")

    if rotation:
        out.append(
            _ROTATION_BODY.format(
                value=_val((raw.get("position") or {}).get("value", pos["value"])), max_weight=_lit(pos["max_weight"])
            )
        )
    else:
        raw_value = (raw.get("position") or {}).get("value", pos["value"])
        if pos["mode"] == "weight":
            order = f'order_target_percent(s, {_val(raw_value)}, reason="rule_entry")'
            note = f"按目标资金占比建仓（{_val_cn(raw_value)}）"
        else:
            order = f'order_target(s, {_val(raw_value)}, reason="rule_entry")'
            note = f"按固定数量建仓（{_val_cn(raw_value)}）"
        out.append(_SINGLE_BODY.format(order=order, note=note))
    return "".join(out)


def _static_lookback(r: dict[str, Any]) -> int:
    return max(
        (i["period"] * 3 + 1 if i["type"] in ("ema", "rsi", "atr") else i["period"] + 1)
        for i in r["indicators"].values()
    )


_SINGLE_BODY = """def pick_security(ctx):
    # 交易标的：优先用规则声明的标的；本次运行没有它时退回标的池第一个
    secs = list(ctx.securities or [])
    return SECURITY if SECURITY in secs or not secs else secs[0]


def handle_data(ctx):
    s = pick_security(ctx)
    need = bars_needed()
    bars = history(s, need, FIELDS)
    # 预热未完成：已收盘 bar 不够算出全部指标时不交易
    if len(bars["close"]) < need:
        return
    ind = indicators(bars)
    held = ctx.position(s).amount > 0
    if held and exit_signal(bars, ind):
        # 持仓且满足离场条件 → 清仓
        order_target(s, 0, reason="rule_exit")
    elif not held and entry_signal(bars, ind) and not exit_signal(bars, ind):
        # 空仓、满足入场且不满足离场（同时满足时离场优先）→ {note}
        {order}
"""

_ROTATION_BODY = """def universe(ctx):
    # 实际交易的标的池：规则声明的标的与本次运行有行情的标的取交集
    secs = [s for s in (ctx.securities or []) if s]
    if not secs:
        return list(INSTRUMENTS)
    return [s for s in INSTRUMENTS if s in secs]


def rank_weights(picks, scores):
    # 名次 → 权重：等权各 1/N；按得分加权时总得分非正则退回等权（现货不能做空）
    if not picks:
        return {{}}
    if RANK_WEIGHTING == "score":
        vals = [scores.get(p) or 0.0 for p in picks]
        total = sum(vals)
        if total > 0 and all(v >= 0 for v in vals):
            return {{p: v / total for p, v in zip(picks, vals)}}
    return {{p: 1.0 / len(picks) for p in picks}}


def handle_data(ctx):
    names = universe(ctx)
    need = bars_needed()
    data = {{}}
    for s in names:
        bars = history(s, need, FIELDS)
        # 预热未完成的标的不参与排名，也不调整它的仓位
        if len(bars["close"]) >= need:
            data[s] = (bars, indicators(bars))
    if not data:
        return
    scores = {{s: ind[RANK_BY][-1] for s, (bars, ind) in data.items()}}
    # 排名：得分为 None 的排最后；同分按标的代码字典序，保证名次可复现
    order = sorted(
        scores,
        key=lambda s: (1, 0.0, s) if scores[s] is None else (0, scores[s] if RANK_ORDER == "asc" else -scores[s], s),
    )
    picks = []
    for s in order:
        if scores[s] is None or (MIN_SCORE is not None and scores[s] <= MIN_SCORE):
            continue
        picks.append(s)
        if len(picks) >= RANK_TOP:
            break
    # 目标权重 = 名次权重 × 总仓位，再按单标的上限裁剪（不重新归一化）
    targets = {{s: min(w * {value}, {max_weight}) for s, w in rank_weights(picks, scores).items()}}

    previous = g.last_targets
    current = {{}}
    changes = []
    for s in sorted(names):
        if s not in data:
            continue
        bars, ind = data[s]
        held = ctx.position(s).amount > 0
        target = targets.get(s, 0.0)
        # 离场优先：仍入选但满足离场条件时清仓
        if target > 0 and exit_signal(bars, ind):
            target = 0.0
        # 尚未持有且入场条件不满足：不建仓
        if target > 0 and not held and not entry_signal(bars, ind):
            target = 0.0
        current[s] = target
        # 下单规则：目标变了；或目标为正但空仓（止损 / 止盈打掉后重新建仓）；或目标为零但仍持仓
        before = previous.get(s, 0.0)
        changed = target != before
        mismatch = (target > 0 and not held) or (target <= 0 and held)
        if not changed and not mismatch:
            continue
        changes.append((s, target, target < before))
    g.last_targets = current
    # 先卖后买（卖出回流的现金才能用于买入），组内按标的代码字典序
    for s, target, is_sell in sorted(changes, key=lambda c: (not c[2], c[0])):
        order_target_percent(s, target, reason="rule_rotation" if target > 0 else "rule_exit")
"""


# ---------------------------------------------------------------------------
# 代码 → 规则
# ---------------------------------------------------------------------------
def extract_rule_comments(code: str) -> tuple[dict[str, Any] | None, dict[str, Any], str]:
    """从代码注释里读出 `@rule` 块（规则 JSON）与 `@param` 行。

    返回 (规则 JSON 或 None, 参数, 出错说明)。没有 @rule 块时规则为 None、说明为空。
    """
    lines = code.splitlines()
    body: list[str] | None = None
    rule: dict[str, Any] | None = None
    params: dict[str, Any] = {}
    error = ""
    found = False
    for line in lines:
        s = line.strip()
        if body is not None:
            if s.startswith("#") and s.lstrip("#").strip() == RULE_END:
                try:
                    obj = json.loads("\n".join(body))
                    if isinstance(obj, dict):
                        rule = obj
                    else:
                        error = "@rule 块必须是一个 JSON 对象"
                except json.JSONDecodeError as exc:
                    error = f"@rule 块不是合法的 JSON：第 {exc.lineno} 行 {exc.msg}"
                body = None
                continue
            if not s.startswith("#"):
                error = "@rule 块没有以 # @endrule 结束"
                body = None
                continue
            text = s[1:]
            body.append(text[1:] if text.startswith(" ") else text)
            continue
        if s.startswith("#") and s.lstrip("#").strip() == RULE_BEGIN and not found:
            found = True
            body = []
            continue
        m = _PARAM_LINE.match(s)
        if m:
            try:
                v = ast.literal_eval(m.group(2).strip())
            except (ValueError, SyntaxError):
                error = error or f"@param {m.group(1)} 的取值不是数值"
                continue
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                error = error or f"@param {m.group(1)} 的取值必须是有限数值"
                continue
            params[m.group(1)] = v
    if body is not None:
        error = "@rule 块没有以 # @endrule 结束"
    if not found:
        return None, params, ""
    return rule, params, error


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            b = node.body
            first = b[0].value if b and isinstance(b[0], ast.Expr) else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                node.body = b[1:] or [ast.Pass()]


def _params_node(tree: ast.Module) -> ast.Assign | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PARAMS" for t in node.targets):
            return node
    return None


def _normalize(tree: ast.Module) -> ast.Module:
    """比较前的规范化：去掉文档字符串；PARAMS 字典只保留键（取值允许不同）。"""
    _strip_docstrings(tree)
    node = _params_node(tree)
    if node is not None and isinstance(node.value, ast.Dict):
        node.value = ast.Dict(keys=node.value.keys, values=[ast.Constant(None) for _ in node.value.values])
    return tree


def _stmt_label(node: ast.stmt | None) -> str:
    if node is None:
        return "（缺失）"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"函数 {node.name}"
    if isinstance(node, ast.ClassDef):
        return f"类 {node.name}"
    if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
        return f"变量 {node.targets[0].id}"
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return "导入语句"
    return f"第 {getattr(node, 'lineno', '?')} 行"


def _diff_line(a: ast.AST, b: ast.AST) -> int | None:
    """沿着两棵树往下走，返回最深一处不一致节点的行号（用户代码里的行号）。"""
    line = getattr(a, "lineno", None)
    while True:
        ca, cb = list(ast.iter_child_nodes(a)), list(ast.iter_child_nodes(b))
        if type(a) is not type(b) or len(ca) != len(cb):
            return line
        nxt = next(((x, y) for x, y in zip(ca, cb, strict=True) if ast.dump(x) != ast.dump(y)), None)
        if nxt is None:
            return line
        a, b = nxt
        line = getattr(a, "lineno", line)


def _first_diff(mine: ast.Module, ref: ast.Module) -> str:
    """找出第一处不一致的顶层语句，给出人能看懂的位置。"""
    n = max(len(mine.body), len(ref.body))
    for i in range(n):
        a = mine.body[i] if i < len(mine.body) else None
        b = ref.body[i] if i < len(ref.body) else None
        if a is None or b is None or ast.dump(a) != ast.dump(b):
            if a is None:
                return f"缺少{_stmt_label(b)}"
            if b is None:
                return f"多出{_stmt_label(a)}"
            line = _diff_line(a, b)
            return f"{_stmt_label(a)}" + (f"（第 {line} 行附近）" if line else "")
    return ""


def code_to_rule(code: str) -> dict[str, Any]:
    """判断一份 Python 策略是否仍然"恰好是"它顶部 @rule 注释描述的规则。

    返回 {"is_rule", "spec", "reason", "warnings", "diff"}：
    - is_rule=True：spec 是可在规则视图编辑的规则（params 以代码里 PARAMS 的实际取值为准）；
    - is_rule=False：reason 说明为什么按代码策略处理；diff 指出第一处差异的位置（若有）。
    """

    def no(reason: str, *, spec: dict[str, Any] | None = None, diff: str = "", problems: list | None = None) -> dict:
        out: dict[str, Any] = {"is_rule": False, "spec": spec, "reason": reason, "warnings": [], "diff": diff}
        if problems:
            out["problems"] = problems
        return out

    if not isinstance(code, str) or not code.strip():
        return no("代码为空")
    try:
        mine = ast.parse(code)
    except SyntaxError as exc:
        return no(f"代码有语法错误（第 {exc.lineno} 行：{exc.msg}），按代码策略处理")
    rule, comment_params, error = extract_rule_comments(code)
    if rule is None and not error:
        return no("代码里没有 @rule 注释块，按代码策略处理")
    if error:
        return no(f"{error}，按代码策略处理")
    assert rule is not None
    # 参数以 @param 行为准；@rule 块里若也写了 params，作为缺省值
    base_params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
    spec = {**{k: v for k, v in rule.items() if k != "params"}, "params": {**base_params, **comment_params}}
    spec.setdefault("dsl_version", DSL_VERSION)
    try:
        expected = rule_to_code(spec)
    except DslError as exc:
        return no(f"@rule 中的规则未通过校验：{exc}", spec=spec, problems=exc.problems)

    ref = _normalize(ast.parse(expected))
    actual_params_node = _params_node(mine)
    actual_params: dict[str, Any] = {}
    if actual_params_node is not None:
        try:
            actual_params = ast.literal_eval(actual_params_node.value)
        except (ValueError, SyntaxError):
            return no(CODE_ONLY_REASON, spec=spec, diff="变量 PARAMS 不是纯数值字面量")
        if not isinstance(actual_params, dict):
            return no(CODE_ONLY_REASON, spec=spec, diff="变量 PARAMS 不是字典")
    mine = _normalize(mine)
    if ast.dump(mine) != ast.dump(ref):
        return no(CODE_ONLY_REASON, spec=spec, diff=_first_diff(mine, ref))

    warnings: list[str] = []
    for k, v in actual_params.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return no(CODE_ONLY_REASON, spec=spec, diff=f"参数 {k} 的取值不是有限数值")
    final = {**spec, "params": {k: actual_params.get(k, v) for k, v in spec["params"].items()}}
    changed = [k for k in final["params"] if final["params"][k] != spec["params"].get(k)]
    if changed:
        warnings.append(f"@param 注释与 PARAMS 不一致（{', '.join(changed)}），以 PARAMS 为准")
    try:
        validate(final)
    except DslError as exc:
        return no(f"PARAMS 里的取值让规则不再合法：{exc}", spec=final, problems=exc.problems)
    return {"is_rule": True, "spec": final, "reason": "", "warnings": warnings, "diff": ""}
