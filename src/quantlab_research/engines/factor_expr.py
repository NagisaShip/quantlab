"""因子表达式：解析、校验与在面板数据上求值。

写法参考 WorldQuant BRAIN / Qlib 的公式风格，例如：

    cs_rank(-ts_mean(funding, 3)) * ts_zscore(delta(oi, 6) / delay(oi, 6), 30)
    -ts_corr(close, volume, 20)
    where(fng < 25, 1, -1) * returns(5)

面板数据是 `{字段: ndarray[T, N]}`（T 根 K 线 × N 个标的），所有算子都只使用当前及过去的数据，
时间窗口必须是正整数常量，因此表达式本身无法偷看未来。缺失值用 NaN 传播。

解析器是手写的递归下降，不使用 eval，表达式里只能出现目录中的字段、算子与数字。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

import numpy as np

MAX_WINDOW = 500
MAX_NODES = 60
MAX_DEPTH = 12

# ---------------------------------------------------------------------------
# 字段目录
# ---------------------------------------------------------------------------
FIELDS: dict[str, dict[str, str]] = {
    "open": {"label": "开盘价", "group": "行情"},
    "high": {"label": "最高价", "group": "行情"},
    "low": {"label": "最低价", "group": "行情"},
    "close": {"label": "收盘价", "group": "行情"},
    "volume": {"label": "成交量（币）", "group": "行情"},
    "amount": {"label": "成交额（≈ 收盘价 × 成交量）", "group": "行情"},
    "vwap": {"label": "典型价（高+低+收）/3", "group": "行情"},
    "ret": {"label": "单期收益（收盘对收盘）", "group": "行情"},
    "funding": {"label": "资金费率（最近一次结算）", "group": "衍生品"},
    "oi": {"label": "持仓量（美元）", "group": "衍生品"},
    "ls_ratio": {"label": "多空账户比", "group": "衍生品"},
    "taker_ratio": {"label": "主动买卖量比", "group": "衍生品"},
    "basis": {"label": "永续溢价（相对现货/指数）", "group": "衍生品"},
    "fng": {"label": "恐惧贪婪指数（全市场）", "group": "情绪"},
    "stablecoin": {"label": "稳定币总市值（全市场）", "group": "链上"},
    "tvl": {"label": "DeFi 总锁仓（全市场）", "group": "链上"},
    "mkt_ret": {"label": "市场等权单期收益", "group": "行情"},
    "liq_long": {"label": "多头被强平金额（美元，小时）", "group": "衍生品"},
    "liq_short": {"label": "空头被强平金额（美元，小时）", "group": "衍生品"},
    "dvol": {"label": "Deribit 隐含波动率指数 DVOL", "group": "期权"},
    "iv_skew": {"label": "30 天期权偏度（看跌 − 看涨 IV）", "group": "期权"},
    "ev_listing": {"label": "上币公告强度（24 小时半衰）", "group": "事件"},
    "ev_delisting": {"label": "下架公告强度（负值）", "group": "事件"},
    "ev_maintenance": {"label": "维护公告强度", "group": "事件"},
    "ev_news": {"label": "新闻情绪（方向 × 强度，衰减求和）", "group": "事件"},
    "ev_macro": {"label": "未来 24 小时重要宏观事件（全市场）", "group": "事件"},
}
PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount", "vwap", "ret", "mkt_ret")


# ---------------------------------------------------------------------------
# 算子目录：名字 → (参数签名, 说明)。签名里 x=序列，d=窗口整数，k=数字常量
# ---------------------------------------------------------------------------
OPERATORS: dict[str, dict[str, Any]] = {
    # 时序
    "delay": {"sig": "x,d", "group": "时序", "doc": "d 期之前的值"},
    "delta": {"sig": "x,d", "group": "时序", "doc": "x - delay(x, d)"},
    "pct_change": {"sig": "x,d", "group": "时序", "doc": "x / delay(x, d) - 1"},
    "returns": {"sig": "d", "group": "时序", "doc": "过去 d 期收益 close / delay(close, d) - 1"},
    "ts_mean": {"sig": "x,d", "group": "时序", "doc": "过去 d 期均值"},
    "ts_sum": {"sig": "x,d", "group": "时序", "doc": "过去 d 期求和"},
    "ts_std": {"sig": "x,d", "group": "时序", "doc": "过去 d 期标准差"},
    "ts_min": {"sig": "x,d", "group": "时序", "doc": "过去 d 期最小值"},
    "ts_max": {"sig": "x,d", "group": "时序", "doc": "过去 d 期最大值"},
    "ts_rank": {"sig": "x,d", "group": "时序", "doc": "当前值在过去 d 期中的分位（0~1）"},
    "ts_zscore": {"sig": "x,d", "group": "时序", "doc": "(x - 均值) / 标准差，窗口 d"},
    "ts_skew": {"sig": "x,d", "group": "时序", "doc": "过去 d 期偏度"},
    "ts_corr": {"sig": "x,x,d", "group": "时序", "doc": "两序列过去 d 期相关系数"},
    "ts_cov": {"sig": "x,x,d", "group": "时序", "doc": "两序列过去 d 期协方差"},
    "ts_argmax": {"sig": "x,d", "group": "时序", "doc": "过去 d 期最大值距今几期"},
    "ts_argmin": {"sig": "x,d", "group": "时序", "doc": "过去 d 期最小值距今几期"},
    "decay_linear": {"sig": "x,d", "group": "时序", "doc": "线性衰减加权均值（越近权重越大）"},
    "ema": {"sig": "x,d", "group": "时序", "doc": "指数移动平均，跨度 d"},
    "ts_beta": {"sig": "x,x,d", "group": "时序", "doc": "x 对 y 的滚动回归斜率"},
    # 截面
    "rank": {"sig": "x", "group": "截面", "doc": "同一时刻所有标的间的分位（0~1）"},
    "cs_rank": {"sig": "x", "group": "截面", "doc": "同 rank"},
    "cs_zscore": {"sig": "x", "group": "截面", "doc": "截面标准化"},
    "cs_demean": {"sig": "x", "group": "截面", "doc": "减去截面均值"},
    "scale": {"sig": "x", "group": "截面", "doc": "缩放到绝对值之和为 1"},
    "winsorize": {"sig": "x,k", "group": "截面", "doc": "截面上把超过 k 倍标准差的值截断"},
    # 逐元素
    "abs": {"sig": "x", "group": "数学", "doc": "绝对值"},
    "log": {"sig": "x", "group": "数学", "doc": "自然对数（非正数为空）"},
    "sqrt": {"sig": "x", "group": "数学", "doc": "平方根（负数为空）"},
    "sign": {"sig": "x", "group": "数学", "doc": "符号"},
    "signed_power": {"sig": "x,k", "group": "数学", "doc": "sign(x) × |x|^k"},
    "max": {"sig": "x,x", "group": "数学", "doc": "逐元素较大值"},
    "min": {"sig": "x,x", "group": "数学", "doc": "逐元素较小值"},
    "clip": {"sig": "x,k,k", "group": "数学", "doc": "截断到 [lo, hi]"},
    "where": {"sig": "x,x,x", "group": "数学", "doc": "条件为真取第二个，否则第三个"},
    "fillna": {"sig": "x,k", "group": "数学", "doc": "把空值填成常数"},
}


class FactorExprError(ValueError):
    """表达式写错了。消息面向使用者。"""


# ---------------------------------------------------------------------------
# 词法与语法
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r"\s*(?:(\d+\.\d*|\.\d+|\d+)|([A-Za-z_][A-Za-z_0-9]*)|(<=|>=|==|!=|&&|\|\||[-+*/^(),<>!&|?:]))")


@dataclass
class Node:
    kind: str  # num | field | call | bin | neg | not
    value: Any = None  # 数字 / 字段名 / 函数名 / 运算符
    args: tuple[Node, ...] = ()

    def __str__(self) -> str:
        if self.kind == "num":
            v = self.value
            return str(int(v)) if float(v).is_integer() else f"{v:g}"
        if self.kind == "field":
            return self.value
        if self.kind == "call":
            return f"{self.value}({', '.join(str(a) for a in self.args)})"
        if self.kind == "neg":
            return f"-{_paren(self.args[0])}"
        if self.kind == "not":
            return f"!{_paren(self.args[0])}"
        return f"{_paren(self.args[0])} {self.value} {_paren(self.args[1])}"


def _paren(n: Node) -> str:
    return f"({n})" if n.kind in ("bin",) else str(n)


def _tokenize(text: str) -> list[str]:
    pos, out = 0, []
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise FactorExprError(f"第 {pos + 1} 个字符附近无法识别：{text[pos : pos + 10]!r}")
        tok = next(g for g in m.groups() if g is not None)
        out.append(tok)
        pos = m.end()
    return out


_PREC = [("||",), ("&&",), ("<", ">", "<=", ">=", "==", "!="), ("+", "-"), ("*", "/"), ("^",)]


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.t = tokens
        self.i = 0

    def peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def eat(self, tok: str | None = None) -> str:
        cur = self.peek()
        if cur is None or (tok is not None and cur != tok):
            raise FactorExprError(f"缺少 {tok!r}" if tok else "表达式不完整")
        self.i += 1
        return cur

    def parse(self) -> Node:
        node = self.binary(0)
        if self.peek() is not None:
            raise FactorExprError(f"多余的内容：{' '.join(self.t[self.i : self.i + 4])}")
        return node

    def binary(self, level: int) -> Node:
        if level == len(_PREC):
            return self.unary()
        left = self.binary(level + 1)
        while self.peek() in _PREC[level]:
            op = self.eat()
            right = self.binary(level + 1)
            left = Node("bin", op, (left, right))
        return left

    def unary(self) -> Node:
        if self.peek() == "-":
            self.eat()
            return Node("neg", None, (self.unary(),))
        if self.peek() == "+":
            self.eat()
            return self.unary()
        if self.peek() == "!":
            self.eat()
            return Node("not", None, (self.unary(),))
        return self.atom()

    def atom(self) -> Node:
        tok = self.peek()
        if tok is None:
            raise FactorExprError("表达式不完整")
        if tok == "(":
            self.eat("(")
            node = self.binary(0)
            self.eat(")")
            return node
        if re.fullmatch(r"\d+\.\d*|\.\d+|\d+", tok):
            self.eat()
            return Node("num", float(tok))
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", tok):
            self.eat()
            name = tok.lower()
            if self.peek() == "(":
                self.eat("(")
                args: list[Node] = []
                if self.peek() != ")":
                    args.append(self.binary(0))
                    while self.peek() == ",":
                        self.eat(",")
                        args.append(self.binary(0))
                self.eat(")")
                return Node("call", name, tuple(args))
            return Node("field", name)
        raise FactorExprError(f"这里不应该出现 {tok!r}")


def parse(text: str) -> Node:
    if not text or not text.strip():
        raise FactorExprError("表达式为空")
    if len(text) > 600:
        raise FactorExprError("表达式太长（上限 600 个字符），复杂因子请拆成几个再组合")
    node = _Parser(_tokenize(text)).parse()
    validate(node)
    return node


def _walk(node: Node, depth: int = 0):
    yield node, depth
    for a in node.args:
        yield from _walk(a, depth + 1)


def validate(node: Node) -> None:
    nodes = list(_walk(node))
    if len(nodes) > MAX_NODES:
        raise FactorExprError(f"表达式太复杂（{len(nodes)} 个节点，上限 {MAX_NODES}），越复杂越容易过拟合")
    if max(d for _, d in nodes) > MAX_DEPTH:
        raise FactorExprError(f"嵌套太深（上限 {MAX_DEPTH} 层）")
    for n, _ in nodes:
        if n.kind == "field" and n.value not in FIELDS and not re.fullmatch(r"x_[a-z0-9_]{1,30}", n.value):
            raise FactorExprError(f"未知字段 {n.value}。可用字段：{', '.join(FIELDS)}；自定义数据用 x_<名字>")
        if n.kind == "call":
            spec = OPERATORS.get(n.value)
            if spec is None:
                raise FactorExprError(f"未知算子 {n.value}()。可用算子见算子表")
            sig = spec["sig"].split(",")
            if len(n.args) != len(sig):
                raise FactorExprError(f"{n.value}() 需要 {len(sig)} 个参数（{spec['sig']}），给了 {len(n.args)} 个")
            for kind, arg in zip(sig, n.args, strict=True):  # 上面刚校验过参数个数
                if kind == "d":
                    if arg.kind != "num" or not float(arg.value).is_integer() or arg.value < 1:
                        raise FactorExprError(f"{n.value}() 的窗口必须是正整数常量（不能为负，表达式不能看未来）")
                    if arg.value > MAX_WINDOW:
                        raise FactorExprError(f"{n.value}() 的窗口 {int(arg.value)} 超过上限 {MAX_WINDOW}")
                if kind == "k" and arg.kind not in ("num", "neg"):
                    raise FactorExprError(f"{n.value}() 的这个参数必须是数字常量")


def complexity(node: Node) -> int:
    return len(list(_walk(node)))


def fields_used(node: Node) -> list[str]:
    used = {n.value for n, _ in _walk(node) if n.kind == "field"}
    if any(n.kind == "call" and n.value == "returns" for n, _ in _walk(node)):
        used.add("close")
    return sorted(used)


def max_lookback(node: Node) -> int:
    """表达式需要的最长历史（窗口嵌套相加的粗略上界），用于预热。"""
    if node.kind == "call":
        spec = OPERATORS[node.value]["sig"].split(",")
        own = sum(int(a.value) for k, a in zip(spec, node.args, strict=False) if k == "d")
        return own + max((max_lookback(a) for a in node.args), default=0)
    return max((max_lookback(a) for a in node.args), default=0)


def normalize(text: str) -> str:
    """规范写法（用于去重与哈希）。"""
    return str(parse(text))


# ---------------------------------------------------------------------------
# 求值
# ---------------------------------------------------------------------------
def _const(v: float, shape: tuple[int, int]) -> np.ndarray:
    return np.full(shape, float(v))


def _rolling(x: np.ndarray, d: int) -> np.ndarray:
    """返回 [T, N, d] 的滑动窗口视图（前 d-1 行用 NaN 补齐）。"""
    T, N = x.shape
    pad = np.full((d - 1, N), np.nan)
    xp = np.vstack([pad, x])
    shape = (T, N, d)
    strides = (xp.strides[0], xp.strides[1], xp.strides[0])
    return np.lib.stride_tricks.as_strided(xp, shape=shape, strides=strides, writeable=False)


def _nanwindow(fn, x: np.ndarray, d: int, min_frac: float = 0.8) -> np.ndarray:
    w = _rolling(x, d)
    valid = np.sum(~np.isnan(w), axis=2)
    with np.errstate(all="ignore"), _quiet():
        out = fn(w, axis=2)
    out = np.asarray(out, dtype=float)
    out[valid < max(1, math.ceil(d * min_frac))] = np.nan
    return out


class _quiet:
    def __enter__(self):
        import warnings

        self._w = warnings.catch_warnings()
        self._w.__enter__()
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return self

    def __exit__(self, *exc):
        return self._w.__exit__(*exc)


def _delay(x: np.ndarray, d: int) -> np.ndarray:
    out = np.full_like(x, np.nan)
    if d < x.shape[0]:
        out[d:] = x[:-d]
    return out


def _ts_rank(x: np.ndarray, d: int) -> np.ndarray:
    w = _rolling(x, d)
    cur = x[:, :, None]
    with np.errstate(all="ignore"):
        less = np.sum(w < cur, axis=2).astype(float)
        eq = np.sum(w == cur, axis=2).astype(float)
        valid = np.sum(~np.isnan(w), axis=2).astype(float)
        out = (less + 0.5 * (eq - 1)) / np.maximum(valid - 1, 1)
    out[(valid < d * 0.8) | np.isnan(x)] = np.nan
    return out


def _ts_corr(x: np.ndarray, y: np.ndarray, d: int, *, cov: bool = False, beta: bool = False) -> np.ndarray:
    wx, wy = _rolling(x, d), _rolling(y, d)
    mask = ~(np.isnan(wx) | np.isnan(wy))
    n = mask.sum(axis=2).astype(float)
    with np.errstate(all="ignore"), _quiet():
        xs = np.where(mask, wx, 0.0)
        ys = np.where(mask, wy, 0.0)
        mx = xs.sum(axis=2) / n
        my = ys.sum(axis=2) / n
        dx = np.where(mask, wx - mx[:, :, None], 0.0)
        dy = np.where(mask, wy - my[:, :, None], 0.0)
        c = (dx * dy).sum(axis=2) / np.maximum(n - 1, 1)
        if cov:
            out = c
        elif beta:
            vy = (dy * dy).sum(axis=2) / np.maximum(n - 1, 1)
            out = c / vy
        else:
            sx = np.sqrt((dx * dx).sum(axis=2) / np.maximum(n - 1, 1))
            sy = np.sqrt((dy * dy).sum(axis=2) / np.maximum(n - 1, 1))
            out = c / (sx * sy)
    out[(n < d * 0.8) | ~np.isfinite(out)] = np.nan
    return out


def _cs_rank(x: np.ndarray) -> np.ndarray:
    out = np.full_like(x, np.nan)
    for t in range(x.shape[0]):
        row = x[t]
        ok = ~np.isnan(row)
        k = int(ok.sum())
        if k == 0:
            continue
        if k == 1:
            out[t, ok] = 0.5
            continue
        vals = row[ok]
        order = vals.argsort(kind="mergesort")
        ranks = np.empty(k)
        ranks[order] = np.arange(k, dtype=float)
        # 并列取平均名次
        uniq, inv = np.unique(vals, return_inverse=True)
        if len(uniq) < k:
            sums = np.bincount(inv, weights=ranks)
            cnts = np.bincount(inv)
            ranks = (sums / cnts)[inv]
        out[t, ok] = ranks / (k - 1)
    return out


def _cs(fn, x: np.ndarray) -> np.ndarray:
    with np.errstate(all="ignore"), _quiet():
        return fn(x)


def _cs_zscore(x: np.ndarray) -> np.ndarray:
    def f(v):
        m = np.nanmean(v, axis=1, keepdims=True)
        s = np.nanstd(v, axis=1, keepdims=True)
        out = (v - m) / s
        out[~np.isfinite(out)] = np.nan
        return out

    return _cs(f, x)


def _decay_linear(x: np.ndarray, d: int) -> np.ndarray:
    w = _rolling(x, d)
    weights = np.arange(1, d + 1, dtype=float)
    mask = ~np.isnan(w)
    with np.errstate(all="ignore"):
        num = np.nansum(np.where(mask, w, 0.0) * weights, axis=2)
        den = np.sum(mask * weights, axis=2)
        out = num / den
    out[mask.sum(axis=2) < d * 0.8] = np.nan
    return out


def _ema(x: np.ndarray, d: int) -> np.ndarray:
    a = 2.0 / (d + 1)
    out = np.full_like(x, np.nan)
    prev = np.full(x.shape[1], np.nan)
    count = np.zeros(x.shape[1])
    for t in range(x.shape[0]):
        cur = x[t]
        ok = ~np.isnan(cur)
        prev = np.where(ok & np.isnan(prev), cur, prev)
        prev = np.where(ok, a * cur + (1 - a) * prev, prev)
        count = np.where(ok, count + 1, count)
        out[t] = np.where(count >= d, prev, np.nan)
    return out


def evaluate(node: Node | str, panel: dict[str, np.ndarray]) -> np.ndarray:
    """在面板上求值，返回 [T, N] 数组。"""
    if isinstance(node, str):
        node = parse(node)
    # Integer price arrays must still represent warm-up windows as NaN.
    panel = {key: np.asarray(value, dtype=float) for key, value in panel.items()}
    shape = panel["close"].shape
    return np.asarray(_eval(node, panel, shape), dtype=float)


def _eval(n: Node, P: dict[str, np.ndarray], shape: tuple[int, int]) -> np.ndarray:
    if n.kind == "num":
        return _const(n.value, shape)
    if n.kind == "field":
        if n.value not in P:
            label = FIELDS.get(n.value, {}).get("label", "自定义数据")
            raise FactorExprError(f"当前数据里没有字段 {n.value}（{label}），请先在「行情与数据」里同步或导入对应数据")
        return P[n.value]
    if n.kind == "neg":
        return -_eval(n.args[0], P, shape)
    if n.kind == "not":
        v = _eval(n.args[0], P, shape)
        return np.where(np.isnan(v), np.nan, (v == 0).astype(float))
    if n.kind == "bin":
        a, b = _eval(n.args[0], P, shape), _eval(n.args[1], P, shape)
        with np.errstate(all="ignore"):
            if n.value == "+":
                out = a + b
            elif n.value == "-":
                out = a - b
            elif n.value == "*":
                out = a * b
            elif n.value == "/":
                out = a / np.where(b == 0, np.nan, b)
            elif n.value == "^":
                out = np.power(a, b)
            elif n.value in ("<", ">", "<=", ">=", "==", "!="):
                cmp = {
                    "<": np.less,
                    ">": np.greater,
                    "<=": np.less_equal,
                    ">=": np.greater_equal,
                    "==": np.equal,
                    "!=": np.not_equal,
                }[n.value](a, b).astype(float)
                out = np.where(np.isnan(a) | np.isnan(b), np.nan, cmp)
            elif n.value == "&&":
                out = np.where(np.isnan(a) | np.isnan(b), np.nan, ((a != 0) & (b != 0)).astype(float))
            elif n.value == "||":
                out = np.where(np.isnan(a) | np.isnan(b), np.nan, ((a != 0) | (b != 0)).astype(float))
            else:  # pragma: no cover - 语法已限制
                raise FactorExprError(f"不支持的运算符 {n.value}")
        out = np.asarray(out, dtype=float)
        out[~np.isfinite(out)] = np.nan
        return out
    name, args = n.value, n.args
    sig = OPERATORS[name]["sig"].split(",")
    vals: list[Any] = []
    for kind, a in zip(sig, args, strict=False):
        if kind == "d":
            vals.append(int(a.value))
        elif kind == "k":
            vals.append(float(a.value) if a.kind == "num" else -float(a.args[0].value))
        else:
            vals.append(_eval(a, P, shape))
    with np.errstate(all="ignore"), _quiet():
        out = _call(name, vals, P)
    out = np.asarray(out, dtype=float)
    out[~np.isfinite(out)] = np.nan
    return out


def _call(name: str, v: list[Any], P: dict[str, np.ndarray]) -> np.ndarray:
    if name == "delay":
        return _delay(v[0], v[1])
    if name == "delta":
        return v[0] - _delay(v[0], v[1])
    if name == "pct_change":
        prev = _delay(v[0], v[1])
        return v[0] / np.where(prev == 0, np.nan, prev) - 1
    if name == "returns":
        c = P["close"]
        prev = _delay(c, v[0])
        return c / np.where(prev == 0, np.nan, prev) - 1
    if name == "ts_mean":
        return _nanwindow(np.nanmean, v[0], v[1])
    if name == "ts_sum":
        return _nanwindow(np.nansum, v[0], v[1])
    if name == "ts_std":
        return _nanwindow(lambda w, axis: np.nanstd(w, axis=axis, ddof=1), v[0], v[1])
    if name == "ts_min":
        return _nanwindow(np.nanmin, v[0], v[1])
    if name == "ts_max":
        return _nanwindow(np.nanmax, v[0], v[1])
    if name == "ts_rank":
        return _ts_rank(v[0], v[1])
    if name == "ts_zscore":
        m = _nanwindow(np.nanmean, v[0], v[1])
        s = _nanwindow(lambda w, axis: np.nanstd(w, axis=axis, ddof=1), v[0], v[1])
        return (v[0] - m) / np.where(s == 0, np.nan, s)
    if name == "ts_skew":

        def skew(w, axis):
            m = np.nanmean(w, axis=axis, keepdims=True)
            s = np.nanstd(w, axis=axis, keepdims=True)
            return np.nanmean(((w - m) / s) ** 3, axis=axis)

        return _nanwindow(skew, v[0], v[1])
    if name == "ts_corr":
        return _ts_corr(v[0], v[1], v[2])
    if name == "ts_cov":
        return _ts_corr(v[0], v[1], v[2], cov=True)
    if name == "ts_beta":
        return _ts_corr(v[0], v[1], v[2], beta=True)
    if name in ("ts_argmax", "ts_argmin"):
        w = _rolling(v[0], v[1])
        filled = np.where(np.isnan(w), -np.inf if name == "ts_argmax" else np.inf, w)
        idx = filled.argmax(axis=2) if name == "ts_argmax" else filled.argmin(axis=2)
        out = (v[1] - 1 - idx).astype(float)
        out[np.sum(~np.isnan(w), axis=2) < v[1] * 0.8] = np.nan
        return out
    if name == "decay_linear":
        return _decay_linear(v[0], v[1])
    if name == "ema":
        return _ema(v[0], v[1])
    if name in ("rank", "cs_rank"):
        return _cs_rank(v[0])
    if name == "cs_zscore":
        return _cs_zscore(v[0])
    if name == "cs_demean":
        return v[0] - np.nanmean(v[0], axis=1, keepdims=True)
    if name == "scale":
        s = np.nansum(np.abs(v[0]), axis=1, keepdims=True)
        return v[0] / np.where(s == 0, np.nan, s)
    if name == "winsorize":
        m = np.nanmean(v[0], axis=1, keepdims=True)
        s = np.nanstd(v[0], axis=1, keepdims=True)
        return np.clip(v[0], m - v[1] * s, m + v[1] * s)
    if name == "abs":
        return np.abs(v[0])
    if name == "log":
        return np.log(np.where(v[0] > 0, v[0], np.nan))
    if name == "sqrt":
        return np.sqrt(np.where(v[0] >= 0, v[0], np.nan))
    if name == "sign":
        return np.sign(v[0])
    if name == "signed_power":
        return np.sign(v[0]) * np.power(np.abs(v[0]), v[1])
    if name == "max":
        return np.maximum(v[0], v[1])
    if name == "min":
        return np.minimum(v[0], v[1])
    if name == "clip":
        return np.clip(v[0], min(v[1], v[2]), max(v[1], v[2]))
    if name == "where":
        cond = v[0]
        return np.where(np.isnan(cond), np.nan, np.where(cond != 0, v[1], v[2]))
    if name == "fillna":
        return np.where(np.isnan(v[0]), v[1], v[0])
    raise FactorExprError(f"算子 {name} 未实现")  # pragma: no cover


def catalog() -> dict[str, Any]:
    return {
        "fields": [{"name": k, **v} for k, v in FIELDS.items()],
        "operators": [{"name": k, **v} for k, v in OPERATORS.items()],
        "limits": {"max_window": MAX_WINDOW, "max_nodes": MAX_NODES, "max_depth": MAX_DEPTH},
    }


__all__ = [
    "FIELDS",
    "OPERATORS",
    "FactorExprError",
    "Node",
    "catalog",
    "complexity",
    "evaluate",
    "fields_used",
    "max_lookback",
    "normalize",
    "parse",
]
