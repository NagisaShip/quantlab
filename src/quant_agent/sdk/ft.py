"""Freqtrade 策略兼容层：让 IStrategy 写法的策略在本平台内核里逐 bar 运行。

导入时（strategies/freqtrade_import.py）把策略里的
``from freqtrade.strategy import IStrategy``、``import talib.abstract as ta``、
``import freqtrade.vendor.qtpylib.indicators as qtpylib``、``from pandas import DataFrame`` 等改成从这里导入，
再在文件末尾接上 ``initialize`` / ``handle_data``。

运行方式与 Freqtrade 回测一致：每根 bar 收盘时，用截至这根的 K 线（最近 ``window`` 根）构造 DataFrame，
依次调用 populate_indicators → populate_entry_trend → populate_exit_trend，读**最后一根**的
enter_long / exit_long；下单在下一根开盘成交。minimal_roi / stoploss / trailing_stop 映射到平台的
set_roi / set_stop_loss / set_trailing_stop，custom_stoploss / custom_exit / confirm_trade_entry 映射到同名回调。

平台没有 pandas，这里用 numpy 实现了 Freqtrade 策略常用的 DataFrame / Series 子集，以及 TA-Lib、qtpylib
的常用指标（口径按 TA-Lib：前面不够周期的位置是 NaN，EMA 用前 n 根的简单平均起步）。
用到不支持的写法时导入检查会列出来，可以让 AI 改写成平台原生写法。
"""

from __future__ import annotations

import math
import warnings
from datetime import UTC, datetime
from typing import Any

import numpy as np

NAN = float("nan")


def _arr(x: Any, n: int | None = None) -> np.ndarray:
    if isinstance(x, Series):
        return x.values
    if isinstance(x, np.ndarray):
        return x
    if isinstance(x, (list, tuple)):
        return np.asarray(x)
    if n is None:
        return np.asarray(x)
    return np.full(n, x, dtype=object if isinstance(x, str) else float if not isinstance(x, bool) else bool)


def _num(a: np.ndarray) -> np.ndarray:
    if a.dtype == bool:
        return a.astype(float)
    if a.dtype == object:
        try:
            return a.astype(float)
        except (TypeError, ValueError):
            return a
    return a


# ======================================================================== Series
class Series:
    __array_priority__ = 1000

    def __init__(self, values: Any = None, name: str | None = None, index: Any = None, dtype: Any = None) -> None:
        if isinstance(values, Series):
            values = values.values
        a = np.asarray([] if values is None else values)
        if dtype is not None:
            a = a.astype(dtype)
        self.values = a
        self.name = name

    # ---- 基本 ----
    def __len__(self) -> int:
        return len(self.values)

    def __array__(self, dtype: Any = None, copy: Any = None) -> np.ndarray:
        return self.values if dtype is None else self.values.astype(dtype)

    def __array_ufunc__(self, ufunc: Any, method: str, *inputs: Any, **kw: Any) -> Any:
        args = [i.values if isinstance(i, Series) else i for i in inputs]
        with np.errstate(all="ignore"):
            out = getattr(ufunc, method)(*args, **kw)
        return Series(out) if isinstance(out, np.ndarray) and out.ndim == 1 else out

    def __iter__(self):
        return iter(self.values.tolist())

    def __repr__(self) -> str:
        return f"Series({self.values!r})"

    @property
    def dtype(self) -> Any:
        return self.values.dtype

    @property
    def iloc(self) -> _SIloc:
        return _SIloc(self)

    @property
    def loc(self) -> _SIloc:
        return _SIloc(self)

    @property
    def dt(self) -> _Dt:
        return _Dt(self)

    @property
    def str(self) -> Any:
        raise NotImplementedError("Series.str 不支持")

    @property
    def index(self) -> np.ndarray:
        return np.arange(len(self.values))

    @property
    def size(self) -> int:
        return len(self.values)

    @property
    def empty(self) -> bool:
        return len(self.values) == 0

    def to_numpy(self, *a: Any, **k: Any) -> np.ndarray:
        return self.values

    def tolist(self) -> list:
        return self.values.tolist()

    to_list = tolist

    def copy(self) -> Series:
        return Series(self.values.copy(), self.name)

    def astype(self, t: Any) -> Series:
        if t in ("int", int):
            return Series(np.nan_to_num(_num(self.values)).astype(int))
        return Series(self.values.astype(t))

    def __getitem__(self, k: Any) -> Any:
        if isinstance(k, Series):
            k = k.values
        r = self.values[k]
        return Series(r) if isinstance(r, np.ndarray) else _py(r)

    def __setitem__(self, k: Any, v: Any) -> None:
        if isinstance(k, Series):
            k = k.values
        self.values = _assign(self.values, k, v)

    # ---- 运算 ----
    def _op(self, other: Any, fn: Any) -> Series:
        o = other.values if isinstance(other, Series) else other
        a = self.values
        with np.errstate(all="ignore"):
            if a.dtype == bool and fn in (np.add, np.subtract, np.multiply, np.true_divide):
                a = a.astype(float)
            return Series(fn(_num(a) if a.dtype == object else a, o))

    def __add__(self, o):
        return self._op(o, np.add)

    def __radd__(self, o):
        return Series(o)._op(self, np.add) if isinstance(o, Series) else self._op(o, lambda a, b: b + a)

    def __sub__(self, o):
        return self._op(o, np.subtract)

    def __rsub__(self, o):
        return self._op(o, lambda a, b: b - a)

    def __mul__(self, o):
        return self._op(o, np.multiply)

    def __rmul__(self, o):
        return self._op(o, np.multiply)

    def __truediv__(self, o):
        return self._op(o, np.true_divide)

    def __rtruediv__(self, o):
        return self._op(o, lambda a, b: b / a)

    def __floordiv__(self, o):
        return self._op(o, np.floor_divide)

    def __mod__(self, o):
        return self._op(o, np.mod)

    def __pow__(self, o):
        return self._op(o, np.power)

    def __rpow__(self, o):
        return self._op(o, lambda a, b: b**a)

    def __neg__(self):
        return Series(-_num(self.values))

    def __abs__(self):
        return Series(np.abs(_num(self.values)))

    def __gt__(self, o):
        return self._cmp(o, np.greater)

    def __ge__(self, o):
        return self._cmp(o, np.greater_equal)

    def __lt__(self, o):
        return self._cmp(o, np.less)

    def __le__(self, o):
        return self._cmp(o, np.less_equal)

    def __eq__(self, o):
        return self._cmp(o, np.equal)  # type: ignore[override]

    def __ne__(self, o):
        return self._cmp(o, np.not_equal)  # type: ignore[override]

    __hash__ = None  # type: ignore[assignment]

    def _cmp(self, other: Any, fn: Any) -> Series:
        o = other.values if isinstance(other, Series) else other
        a = self.values
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if a.dtype == object and not isinstance(o, str):
                a = _num(a)
            r = fn(a, o)
        return Series(np.asarray(r, dtype=bool))

    def _bool(self) -> np.ndarray:
        a = self.values
        if a.dtype == bool:
            return a
        with np.errstate(all="ignore"):
            f = _num(a)
            return np.where(np.isnan(f.astype(float)), False, f != 0) if f.dtype != object else f.astype(bool)

    def __and__(self, o):
        return Series(self._bool() & (o._bool() if isinstance(o, Series) else np.asarray(o, dtype=bool)))

    def __rand__(self, o):
        return self.__and__(o)

    def __or__(self, o):
        return Series(self._bool() | (o._bool() if isinstance(o, Series) else np.asarray(o, dtype=bool)))

    def __ror__(self, o):
        return self.__or__(o)

    def __xor__(self, o):
        return Series(self._bool() ^ (o._bool() if isinstance(o, Series) else np.asarray(o, dtype=bool)))

    def __invert__(self):
        return Series(~self._bool())

    def __bool__(self) -> bool:
        raise ValueError("Series 的真假不明确：请用 .any() / .all()，或 .iloc[-1] 取最后一个值")

    gt = __gt__
    ge = __ge__
    lt = __lt__
    le = __le__
    eq = __eq__
    ne = __ne__
    add = __add__
    sub = __sub__
    mul = __mul__
    div = truediv = __truediv__

    # ---- 时间序列 ----
    def shift(self, periods: int = 1, fill_value: Any = None) -> Series:
        a = self.values
        n = len(a)
        if a.dtype == bool:
            out = np.full(n, False if fill_value is None else bool(fill_value))
        elif a.dtype == object:
            out = np.full(n, fill_value, dtype=object)
        else:
            out = np.full(n, NAN if fill_value is None else fill_value, dtype=float)
        p = int(periods)
        if p > 0:
            out[p:] = a[: n - p] if p < n else out[p:]
        elif p < 0:
            out[: n + p] = a[-p:]
        else:
            out = a.copy()
        return Series(out, self.name)

    def diff(self, periods: int = 1) -> Series:
        return Series(_num(self.values).astype(float) - self.shift(periods).values.astype(float))

    def pct_change(self, periods: int = 1) -> Series:
        prev = self.shift(periods).values.astype(float)
        with np.errstate(all="ignore"):
            return Series(_num(self.values).astype(float) / prev - 1.0)

    def rolling(self, window: int, min_periods: int | None = None, center: bool = False, **_: Any) -> Rolling:
        return Rolling(self, int(window), min_periods)

    def ewm(
        self,
        com: float | None = None,
        span: float | None = None,
        halflife: float | None = None,
        alpha: float | None = None,
        adjust: bool = True,
        min_periods: int = 0,
        **_: Any,
    ) -> Ewm:
        if alpha is None:
            if span is not None:
                alpha = 2.0 / (float(span) + 1.0)
            elif com is not None:
                alpha = 1.0 / (1.0 + float(com))
            elif halflife is not None:
                alpha = 1.0 - math.exp(-math.log(2) / float(halflife))
            else:
                raise ValueError("ewm 需要 span / com / alpha / halflife 之一")
        return Ewm(self, float(alpha), adjust, int(min_periods or 0))

    def expanding(self, min_periods: int = 1) -> Rolling:
        return Rolling(self, len(self.values) or 1, min_periods, expanding=True)

    def cumsum(self) -> Series:
        return Series(np.nancumsum(_num(self.values).astype(float)))

    def cummax(self) -> Series:
        return Series(np.fmax.accumulate(_num(self.values).astype(float)))

    def cummin(self) -> Series:
        return Series(np.fmin.accumulate(_num(self.values).astype(float)))

    def cumprod(self) -> Series:
        return Series(np.nancumprod(_num(self.values).astype(float)))

    # ---- 缺失值 ----
    def isna(self) -> Series:
        a = self.values
        if a.dtype == object:
            return Series(np.array([v is None or (isinstance(v, float) and math.isnan(v)) for v in a], dtype=bool))
        if a.dtype.kind in "fc":
            return Series(np.isnan(a))
        return Series(np.zeros(len(a), dtype=bool))

    isnull = isna

    def notna(self) -> Series:
        return ~self.isna()

    notnull = notna

    def fillna(self, value: Any = None, method: str | None = None, inplace: bool = False, **_: Any) -> Series | None:
        if method in ("ffill", "pad"):
            out = self.ffill()
        elif method in ("bfill", "backfill"):
            out = self.bfill()
        else:
            a = self.values.copy()
            m = self.isna().values
            if a.dtype.kind in "biu" and m.any():
                a = a.astype(float)
            if isinstance(value, Series):
                a[m] = value.values[m]
            else:
                if a.dtype.kind in "fc" and isinstance(value, str):
                    a = a.astype(object)
                a[m] = value
            out = Series(a, self.name)
        if inplace:
            self.values = out.values
            return None
        return out

    def ffill(self, **_: Any) -> Series:
        a = _num(self.values).astype(float)
        idx = np.where(~np.isnan(a), np.arange(len(a)), 0)
        np.maximum.accumulate(idx, out=idx)
        out = a[idx]
        out[np.isnan(a) & (np.cumsum(~np.isnan(a)) == 0)] = NAN
        return Series(out, self.name)

    def bfill(self, **_: Any) -> Series:
        return Series(Series(self.values[::-1]).ffill().values[::-1], self.name)

    def dropna(self) -> Series:
        return Series(self.values[~self.isna().values], self.name)

    def replace(self, a: Any, b: Any = None) -> Series:
        v = self.values.copy()
        pairs = a.items() if isinstance(a, dict) else [(a, b)]
        for x, y in pairs:
            if isinstance(x, float) and math.isnan(x):
                v[self.isna().values] = y
            elif isinstance(x, float) and math.isinf(x):
                v[_num(v).astype(float) == x] = y
            else:
                v[v == x] = y
        return Series(v, self.name)

    # ---- 逐元素 ----
    def where(self, cond: Any, other: Any = NAN) -> Series:
        c = cond._bool() if isinstance(cond, Series) else np.asarray(cond, dtype=bool)
        o = other.values if isinstance(other, Series) else other
        return Series(np.where(c, self.values, o), self.name)

    def mask(self, cond: Any, other: Any = NAN) -> Series:
        c = cond._bool() if isinstance(cond, Series) else np.asarray(cond, dtype=bool)
        return self.where(~c, other)

    def clip(self, lower: Any = None, upper: Any = None) -> Series:
        a = _num(self.values).astype(float)
        lo = -np.inf if lower is None else _arr(lower)
        hi = np.inf if upper is None else _arr(upper)
        return Series(np.clip(a, lo, hi), self.name)

    def abs(self) -> Series:
        return abs(self)

    def round(self, decimals: int = 0) -> Series:
        return Series(np.round(_num(self.values).astype(float), decimals))

    def between(self, left: Any, right: Any, inclusive: str = "both") -> Series:
        if inclusive == "neither":
            return (self > left) & (self < right)
        return (self >= left) & (self <= right)

    def apply(self, fn: Any, **_: Any) -> Series:
        return Series(np.array([fn(v) for v in self.values.tolist()]))

    map = apply

    def isin(self, values: Any) -> Series:
        s = set(values)
        return Series(np.array([v in s for v in self.values.tolist()], dtype=bool))

    def rank(self, pct: bool = False, **_: Any) -> Series:
        a = _num(self.values).astype(float)
        order = a.argsort().argsort().astype(float) + 1
        order[np.isnan(a)] = NAN
        return Series(order / np.sum(~np.isnan(a)) if pct else order)

    # ---- 汇总 ----
    def _f(self) -> np.ndarray:
        return _num(self.values).astype(float)

    def mean(self, **_: Any) -> float:
        return _py(np.nanmean(self._f())) if len(self) else NAN

    def sum(self, **_: Any) -> float:
        return _py(np.nansum(self._f()))

    def max(self, **_: Any) -> float:
        return _py(np.nanmax(self._f())) if len(self) else NAN

    def min(self, **_: Any) -> float:
        return _py(np.nanmin(self._f())) if len(self) else NAN

    def median(self, **_: Any) -> float:
        return _py(np.nanmedian(self._f())) if len(self) else NAN

    def std(self, ddof: int = 1, **_: Any) -> float:
        return _py(np.nanstd(self._f(), ddof=ddof)) if len(self) > ddof else NAN

    def var(self, ddof: int = 1, **_: Any) -> float:
        return _py(np.nanvar(self._f(), ddof=ddof)) if len(self) > ddof else NAN

    def count(self) -> int:
        return int((~self.isna().values).sum())

    def any(self) -> bool:
        return bool(self._bool().any())

    def all(self) -> bool:
        return bool(self._bool().all())

    def idxmax(self) -> int:
        return int(np.nanargmax(self._f()))

    def idxmin(self) -> int:
        return int(np.nanargmin(self._f()))

    def nunique(self) -> int:
        return len(set(self.values.tolist()))

    def quantile(self, q: float = 0.5, **_: Any) -> float:
        return _py(np.nanquantile(self._f(), q))

    def value_counts(self) -> dict:
        out: dict = {}
        for v in self.values.tolist():
            out[v] = out.get(v, 0) + 1
        return out

    def head(self, n: int = 5) -> Series:
        return Series(self.values[:n])

    def tail(self, n: int = 5) -> Series:
        return Series(self.values[-n:] if n else self.values[:0])


def _py(v: Any) -> Any:
    if isinstance(v, np.generic):
        return v.item()
    return v


def _assign(a: np.ndarray, k: Any, v: Any) -> np.ndarray:
    vv = v.values if isinstance(v, Series) else v
    if isinstance(vv, str) and a.dtype != object:
        a = a.astype(object)
    elif (
        a.dtype == bool
        and not isinstance(vv, (bool, np.bool_))
        and not (isinstance(vv, np.ndarray) and vv.dtype == bool)
    ):
        a = a.astype(float)
    elif a.dtype.kind in "iu" and isinstance(vv, float):
        a = a.astype(float)
    elif isinstance(vv, np.ndarray) and vv.dtype == object and a.dtype != object:
        a = a.astype(object)
    a = a.copy()
    if isinstance(k, np.ndarray) and k.dtype == bool and isinstance(vv, np.ndarray) and len(vv) == len(a):
        a[k] = vv[k]
    else:
        a[k] = vv
    return a


class _SIloc:
    def __init__(self, s: Series) -> None:
        self.s = s

    def __getitem__(self, k: Any) -> Any:
        return self.s[k]

    def __setitem__(self, k: Any, v: Any) -> None:
        self.s[k] = v


class _Dt:
    def __init__(self, s: Series) -> None:
        self.a = s.values.astype("datetime64[ms]")

    def _part(self, fn: Any) -> Series:
        return Series(np.array([fn(datetime.fromtimestamp(int(x) / 1000, UTC)) for x in self.a.astype("int64")]))

    @property
    def hour(self) -> Series:
        return Series(((self.a.astype("int64") // 3_600_000) % 24).astype(int))

    @property
    def minute(self) -> Series:
        return Series(((self.a.astype("int64") // 60_000) % 60).astype(int))

    @property
    def dayofweek(self) -> Series:
        return Series(((self.a.astype("int64") // 86_400_000 + 3) % 7).astype(int))

    weekday = dayofweek

    @property
    def day(self) -> Series:
        return self._part(lambda d: d.day)

    @property
    def month(self) -> Series:
        return self._part(lambda d: d.month)

    @property
    def year(self) -> Series:
        return self._part(lambda d: d.year)

    @property
    def date(self) -> Series:
        return Series(self.a.astype("datetime64[D]"))


class Rolling:
    def __init__(self, s: Series, w: int, min_periods: int | None, expanding: bool = False) -> None:
        self.a = _num(s.values).astype(float)
        self.w = max(1, w)
        self.mp = self.w if min_periods is None else max(1, int(min_periods))
        self.expanding = expanding

    def _apply(self, fn: Any) -> Series:
        a, n, w = self.a, len(self.a), self.w
        out = np.full(n, NAN)
        if n == 0:
            return Series(out)
        if self.expanding:
            for i in range(n):
                seg = a[: i + 1]
                if np.sum(~np.isnan(seg)) >= self.mp:
                    out[i] = fn(seg[~np.isnan(seg)])
            return Series(out)
        nan = np.isnan(a)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if n >= w:
                view = np.lib.stride_tricks.sliding_window_view(a, w)
                cnt = w - np.lib.stride_tricks.sliding_window_view(nan, w).sum(axis=1)
                if nan.any():
                    vals = (
                        np.array([fn(r[~np.isnan(r)]) if c else NAN for r, c in zip(view, cnt, strict=True)])
                        if (cnt < w).any()
                        else fn(view, axis=1)
                    )
                else:
                    vals = fn(view, axis=1)
                out[w - 1 :] = np.where(cnt >= self.mp, vals, NAN)
            # 窗口还没填满的前 w-1 根：只有 min_periods < w 时才有值
            if self.mp < w:
                for i in range(min(w - 1, n)):
                    seg = a[: i + 1]
                    seg = seg[~np.isnan(seg)]
                    if len(seg) >= self.mp:
                        out[i] = fn(seg)
        return Series(out)

    def mean(self) -> Series:
        if not self.expanding and len(self.a) >= self.w and self.mp >= self.w and not np.isnan(self.a).any():
            c = np.cumsum(np.insert(self.a, 0, 0.0))
            out = np.full(len(self.a), NAN)
            out[self.w - 1 :] = (c[self.w :] - c[: -self.w]) / self.w
            return Series(out)
        return self._apply(lambda x, axis=None: np.mean(x, axis=axis))

    def sum(self) -> Series:
        return self._apply(lambda x, axis=None: np.sum(x, axis=axis))

    def max(self) -> Series:
        return self._apply(lambda x, axis=None: np.max(x, axis=axis))

    def min(self) -> Series:
        return self._apply(lambda x, axis=None: np.min(x, axis=axis))

    def median(self) -> Series:
        return self._apply(lambda x, axis=None: np.median(x, axis=axis))

    def std(self, ddof: int = 1) -> Series:
        return self._apply(lambda x, axis=None: np.std(x, axis=axis, ddof=ddof))

    def var(self, ddof: int = 1) -> Series:
        return self._apply(lambda x, axis=None: np.var(x, axis=axis, ddof=ddof))

    def count(self) -> Series:
        return self._apply(lambda x, axis=None: np.sum(~np.isnan(x), axis=axis).astype(float))

    def quantile(self, q: float) -> Series:
        return self._apply(lambda x, axis=None: np.quantile(x, q, axis=axis))

    def apply(self, fn: Any, raw: bool = False, **_: Any) -> Series:
        a, n, w = self.a, len(self.a), self.w
        out = np.full(n, NAN)
        for i in range(w - 1, n):
            seg = a[i - w + 1 : i + 1]
            out[i] = fn(seg if raw else Series(seg))
        return Series(out)


class Ewm:
    def __init__(self, s: Series, alpha: float, adjust: bool, min_periods: int) -> None:
        self.a = _num(s.values).astype(float)
        self.alpha, self.adjust, self.mp = alpha, adjust, min_periods

    def mean(self) -> Series:
        a, al = self.a, self.alpha
        n = len(a)
        out = np.full(n, NAN)
        num = den = 0.0
        prev = NAN
        seen = 0
        for i in range(n):
            x = a[i]
            if self.adjust:
                if not math.isnan(x):
                    num = num * (1 - al) + x
                    den = den * (1 - al) + 1
                    seen += 1
                else:
                    num *= 1 - al
                    den *= 1 - al
                v = num / den if den else NAN
            else:
                if math.isnan(prev):
                    prev = x
                elif not math.isnan(x):
                    prev = (1 - al) * prev + al * x
                if not math.isnan(x):
                    seen += 1
                v = prev
            out[i] = v if seen >= max(1, self.mp) else NAN
        return Series(out)

    def std(self) -> Series:
        m = self.mean().values
        sq = Ewm(Series(self.a**2), self.alpha, self.adjust, self.mp).mean().values
        with np.errstate(all="ignore"):
            return Series(np.sqrt(np.maximum(sq - m**2, 0)))


# ======================================================================== DataFrame
class Row(dict):
    def __getattr__(self, k: str) -> Any:
        try:
            return self[k]
        except KeyError as exc:
            raise AttributeError(k) from exc


class DataFrame:
    def __init__(self, data: Any = None, columns: list[str] | None = None, index: Any = None) -> None:
        cols: dict[str, Series] = {}
        if isinstance(data, DataFrame):
            cols = {k: v.copy() for k, v in data._c.items()}
        elif isinstance(data, Series):
            cols = {str(data.name if data.name is not None else 0): data.copy()}
        elif isinstance(data, dict):
            n = max((len(v) for v in data.values() if hasattr(v, "__len__") and not isinstance(v, str)), default=0)
            for k, v in data.items():
                cols[str(k)] = Series(_arr(v, n)) if not isinstance(v, Series) else v.copy()
        object.__setattr__(self, "_c", cols)
        if columns:
            object.__setattr__(self, "_c", {c: cols.get(c, Series(np.full(len(self), NAN))) for c in columns})

    def __len__(self) -> int:
        for v in self._c.values():
            return len(v)
        return 0

    @property
    def columns(self) -> list[str]:
        return list(self._c)

    @property
    def index(self) -> np.ndarray:
        return np.arange(len(self))

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self), len(self._c))

    @property
    def empty(self) -> bool:
        return len(self) == 0

    @property
    def loc(self) -> _Loc:
        return _Loc(self)

    @property
    def iloc(self) -> _ILoc:
        return _ILoc(self)

    def __contains__(self, k: str) -> bool:
        return k in self._c

    def __iter__(self):
        return iter(self._c)

    def __getattr__(self, k: str) -> Series:
        c = object.__getattribute__(self, "_c")
        if k in c:
            return c[k]
        raise AttributeError(k)

    def __setattr__(self, k: str, v: Any) -> None:
        if k in self._c:
            self[k] = v
        else:
            object.__setattr__(self, k, v)

    def __getitem__(self, k: Any) -> Any:
        if isinstance(k, str):
            if k not in self._c:
                raise KeyError(f"DataFrame 里没有列 {k!r}")
            return self._c[k]
        if isinstance(k, list):
            return DataFrame({c: self._c[c] for c in k})
        if isinstance(k, Series):
            m = k._bool()
            return DataFrame({c: Series(v.values[m]) for c, v in self._c.items()})
        if isinstance(k, slice):
            return DataFrame({c: Series(v.values[k]) for c, v in self._c.items()})
        raise KeyError(k)

    def __setitem__(self, k: Any, v: Any) -> None:
        n = len(self)
        if isinstance(k, list):
            src = v if isinstance(v, DataFrame) else None
            for i, c in enumerate(k):
                self[c] = src[src.columns[i]] if src is not None else (v[i] if isinstance(v, (list, tuple)) else v)
            return
        if isinstance(v, DataFrame):
            v = v[v.columns[0]]
        if isinstance(v, Series):
            arr = v.values
        elif isinstance(v, (np.ndarray, list, tuple)):
            arr = np.asarray(v)
        else:
            arr = _arr(v, n)
        if n and len(arr) != n:
            raise ValueError(f"列 {k!r} 长度 {len(arr)} 与 DataFrame 行数 {n} 不一致")
        self._c[str(k)] = Series(arr, str(k))

    def copy(self, deep: bool = True) -> DataFrame:
        return DataFrame(self)

    def head(self, n: int = 5) -> DataFrame:
        return self[slice(0, n)]

    def tail(self, n: int = 5) -> DataFrame:
        return self[slice(max(0, len(self) - n), None)]

    def fillna(self, value: Any = None, inplace: bool = False, method: str | None = None, **_: Any) -> DataFrame | None:
        out = DataFrame({c: s.fillna(value, method=method) for c, s in self._c.items()})
        if inplace:
            object.__setattr__(self, "_c", out._c)
            return None
        return out

    def ffill(self, **_: Any) -> DataFrame:
        return DataFrame({c: s.ffill() if s.dtype.kind in "fiub" else s for c, s in self._c.items()})

    def bfill(self, **_: Any) -> DataFrame:
        return DataFrame({c: s.bfill() if s.dtype.kind in "fiub" else s for c, s in self._c.items()})

    def replace(self, a: Any, b: Any = None, inplace: bool = False) -> DataFrame | None:
        out = DataFrame({c: s.replace(a, b) for c, s in self._c.items()})
        if inplace:
            object.__setattr__(self, "_c", out._c)
            return None
        return out

    def rename(self, columns: dict[str, str] | None = None, inplace: bool = False, **_: Any) -> DataFrame | None:
        m = columns or {}
        out = DataFrame({m.get(c, c): s for c, s in self._c.items()})
        if inplace:
            object.__setattr__(self, "_c", out._c)
            return None
        return out

    def drop(
        self, labels: Any = None, columns: Any = None, axis: int = 0, inplace: bool = False, **_: Any
    ) -> DataFrame | None:
        cols = columns if columns is not None else labels
        cols = [cols] if isinstance(cols, str) else list(cols or [])
        out = DataFrame({c: s for c, s in self._c.items() if c not in cols})
        if inplace:
            object.__setattr__(self, "_c", out._c)
            return None
        return out

    def assign(self, **kw: Any) -> DataFrame:
        out = self.copy()
        for k, v in kw.items():
            out[k] = v(out) if callable(v) else v
        return out

    def get(self, k: str, default: Any = None) -> Any:
        return self._c.get(k, default)

    def mean(self, axis: int = 0, **_: Any) -> Any:
        if axis == 1:
            return Series(np.nanmean(np.vstack([_num(s.values).astype(float) for s in self._c.values()]), axis=0))
        return {c: s.mean() for c, s in self._c.items()}

    def max(self, axis: int = 0, **_: Any) -> Any:
        if axis == 1:
            return Series(np.nanmax(np.vstack([_num(s.values).astype(float) for s in self._c.values()]), axis=0))
        return {c: s.max() for c, s in self._c.items()}

    def min(self, axis: int = 0, **_: Any) -> Any:
        if axis == 1:
            return Series(np.nanmin(np.vstack([_num(s.values).astype(float) for s in self._c.values()]), axis=0))
        return {c: s.min() for c, s in self._c.items()}

    def sum(self, axis: int = 0, **_: Any) -> Any:
        if axis == 1:
            return Series(np.nansum(np.vstack([_num(s.values).astype(float) for s in self._c.values()]), axis=0))
        return {c: s.sum() for c, s in self._c.items()}

    def all(self, axis: int = 0) -> Any:
        if axis == 1:
            return Series(np.all(np.vstack([s._bool() for s in self._c.values()]), axis=0))
        return {c: s.all() for c, s in self._c.items()}

    def any(self, axis: int = 0) -> Any:
        if axis == 1:
            return Series(np.any(np.vstack([s._bool() for s in self._c.values()]), axis=0))
        return {c: s.any() for c, s in self._c.items()}

    def __repr__(self) -> str:
        return f"DataFrame({len(self)} 行 × {self.columns})"


class _Loc:
    def __init__(self, df: DataFrame) -> None:
        self.df = df

    @staticmethod
    def _rows(k: Any, n: int) -> Any:
        if isinstance(k, Series):
            return k._bool()
        if isinstance(k, np.ndarray) and k.dtype == bool:
            return k
        if isinstance(k, slice) and k == slice(None):
            return slice(None)
        return k

    def __getitem__(self, k: Any) -> Any:
        if isinstance(k, tuple):
            rows, cols = k
            r = self._rows(rows, len(self.df))
            if isinstance(cols, str):
                v = self.df[cols].values[r]
                return Series(v) if isinstance(v, np.ndarray) else _py(v)
            return DataFrame({c: Series(self.df[c].values[r]) for c in cols})
        r = self._rows(k, len(self.df))
        if isinstance(r, (int, np.integer)):
            return _ILoc(self.df)[int(r)]
        return DataFrame({c: Series(s.values[r]) for c, s in self.df._c.items()})

    def __setitem__(self, k: Any, v: Any) -> None:
        if not isinstance(k, tuple):
            raise NotImplementedError("loc 赋值请写成 dataframe.loc[条件, '列名'] = 值")
        rows, cols = k
        r = self._rows(rows, len(self.df))
        n = len(self.df)
        names = [cols] if isinstance(cols, str) else list(cols)
        vals = list(v) if isinstance(v, (list, tuple)) and len(names) > 1 else [v] * len(names)
        for c, val in zip(names, vals, strict=True):
            if c not in self.df._c:
                init = np.full(n, None, dtype=object) if isinstance(val, str) else np.full(n, NAN)
                self.df._c[c] = Series(init, c)
            s = self.df._c[c]
            s.values = _assign(s.values, r, val)


class _ILoc:
    def __init__(self, df: DataFrame) -> None:
        self.df = df

    def __getitem__(self, k: Any) -> Any:
        if isinstance(k, tuple):
            r, c = k
            col = self.df.columns[c] if isinstance(c, int) else c
            return self.df[col].iloc[r]
        if isinstance(k, (int, np.integer)):
            return Row({c: _py(s.values[int(k)]) for c, s in self.df._c.items()})
        return DataFrame({c: Series(s.values[k]) for c, s in self.df._c.items()})


# ---------------------------------------------------------------------- pandas 子集
class _Pd:
    """策略里常见的 ``pd.xxx`` 调用。"""

    Series = Series
    DataFrame = DataFrame
    NA = NAN
    NaT = None

    @staticmethod
    def concat(objs: list[Any], axis: int = 0, **_: Any) -> Any:
        if axis == 1:
            out = DataFrame()
            for o in objs:
                if isinstance(o, Series):
                    out[o.name or f"c{len(out.columns)}"] = o
                else:
                    for c in o.columns:
                        out[c] = o[c]
            return out
        if all(isinstance(o, Series) for o in objs):
            return Series(np.concatenate([o.values for o in objs]))
        cols = objs[0].columns
        return DataFrame({c: np.concatenate([o[c].values for o in objs]) for c in cols})

    @staticmethod
    def isna(x: Any) -> Any:
        if isinstance(x, Series):
            return x.isna()
        return x is None or (isinstance(x, float) and math.isnan(x))

    isnull = isna

    @staticmethod
    def notna(x: Any) -> Any:
        r = _Pd.isna(x)
        return ~r if isinstance(r, Series) else not r

    notnull = notna

    @staticmethod
    def to_datetime(x: Any, unit: str | None = None, utc: bool = True, **_: Any) -> Any:
        if isinstance(x, Series):
            return Series(x.values.astype("datetime64[ms]"))
        return x


pd = _Pd()


# ======================================================================== TA-Lib 子集
def _src(x: Any, price: str = "close") -> np.ndarray:
    if isinstance(x, DataFrame):
        return _num(x[price].values).astype(float)
    return _num(_arr(x)).astype(float)


def _hlc(df: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return _src(df, "high"), _src(df, "low"), _src(df, "close")


def _first_valid(a: np.ndarray) -> int:
    ok = np.where(~np.isnan(a))[0]
    return int(ok[0]) if len(ok) else len(a)


def _sma(a: np.ndarray, n: int) -> np.ndarray:
    return Rolling(Series(a), n, None).mean().values


def _ema(a: np.ndarray, n: int, *, alpha: float | None = None) -> np.ndarray:
    """TA-Lib EMA：从第一个有效值起，用前 n 个的简单平均作为第一个 EMA。"""
    out = np.full(len(a), NAN)
    s = _first_valid(a)
    if len(a) - s < n:
        return out
    k = alpha if alpha is not None else 2.0 / (n + 1)
    prev = float(np.mean(a[s : s + n]))
    out[s + n - 1] = prev
    for i in range(s + n, len(a)):
        prev = prev + k * (a[i] - prev)
        out[i] = prev
    return out


def _wilder_sum(a: np.ndarray, n: int, start: int) -> np.ndarray:
    """TA-Lib 的 Wilder 平滑（用于 ATR / ADX 系列）：第一个值是前 n 个的和 / 均值。"""
    out = np.full(len(a), NAN)
    if len(a) - start < n:
        return out
    prev = float(np.sum(a[start : start + n]))
    out[start + n - 1] = prev
    for i in range(start + n, len(a)):
        prev = prev - prev / n + a[i]
        out[i] = prev
    return out


def _out(x: np.ndarray, name: str | None = None) -> Series:
    return Series(x, name)


def _multi(**cols: np.ndarray) -> DataFrame:
    return DataFrame({k: Series(v) for k, v in cols.items()})


def _tp(kw: dict[str, Any], args: tuple, default: int, key: str = "timeperiod") -> int:
    if key in kw:
        return int(kw[key])
    if args:
        return int(args[0])
    return default


class _TA:
    """``import talib.abstract as ta`` 的替身。参数名与 TA-Lib 一致。"""

    def SMA(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        return _out(_sma(_src(x, price), _tp(k, a, 30)))

    def EMA(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        return _out(_ema(_src(x, price), _tp(k, a, 30)))

    def WMA(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 30)
        s = _src(x, price)
        w = np.arange(1, n + 1, dtype=float)
        out = np.full(len(s), NAN)
        if len(s) >= n:
            view = np.lib.stride_tricks.sliding_window_view(s, n)
            out[n - 1 :] = view @ w / w.sum()
        return _out(out)

    def DEMA(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 30)
        e1 = _ema(_src(x, price), n)
        e2 = _ema(e1, n)
        return _out(2 * e1 - e2)

    def TEMA(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 30)
        e1 = _ema(_src(x, price), n)
        e2 = _ema(e1, n)
        e3 = _ema(e2, n)
        return _out(3 * e1 - 3 * e2 + e3)

    def TRIMA(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 30)
        if n % 2:
            m = (n + 1) // 2
            return _out(_sma(_sma(_src(x, price), m), m))
        return _out(_sma(_sma(_src(x, price), n // 2), n // 2 + 1))

    def KAMA(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 30)
        s = _src(x, price)
        out = np.full(len(s), NAN)
        fast, slow = 2 / 3, 2 / 31
        if len(s) <= n:
            return _out(out)
        prev = s[n - 1]
        for i in range(n, len(s)):
            change = abs(s[i] - s[i - n])
            vol = np.sum(np.abs(np.diff(s[i - n : i + 1])))
            er = change / vol if vol else 0.0
            sc = (er * (fast - slow) + slow) ** 2
            prev = prev + sc * (s[i] - prev)
            out[i] = prev
        return _out(out)

    def MA(self, x: Any, *a: Any, matype: int = 0, **k: Any) -> Series:
        return [self.SMA, self.EMA, self.WMA, self.DEMA, self.TEMA, self.TRIMA, self.KAMA][int(matype)](x, *a, **k)

    def RSI(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 14)
        s = _src(x, price)
        out = np.full(len(s), NAN)
        st = _first_valid(s)
        if len(s) - st <= n:
            return _out(out)
        d = np.diff(s[st:])
        g, l_ = np.clip(d, 0, None), np.clip(-d, 0, None)
        ag, al = float(np.mean(g[:n])), float(np.mean(l_[:n]))
        out[st + n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        for i in range(n, len(d)):
            ag = (ag * (n - 1) + g[i]) / n
            al = (al * (n - 1) + l_[i]) / n
            out[st + i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        return _out(out)

    def MACD(
        self, x: Any, fastperiod: int = 12, slowperiod: int = 26, signalperiod: int = 9, price: str = "close"
    ) -> DataFrame:
        s = _src(x, price)
        slow = _ema(s, slowperiod)
        # TA-Lib：快线从慢线第一个值的位置往前取 fastperiod 根起步，两条线同时开始
        fast = np.full(len(s), NAN)
        st = _first_valid(s) + slowperiod - fastperiod
        if st >= 0 and len(s) - st >= fastperiod:
            fast[st:] = _ema(s[st:], fastperiod)
        macd = fast - slow
        sig = _ema(macd, signalperiod)
        macd = np.where(np.isnan(sig), NAN, macd)
        return _multi(macd=macd, macdsignal=sig, macdhist=macd - sig)

    def BBANDS(
        self,
        x: Any,
        timeperiod: int = 5,
        nbdevup: float = 2.0,
        nbdevdn: float = 2.0,
        matype: int = 0,
        price: str = "close",
    ) -> DataFrame:
        s = _src(x, price)
        mid = self.MA(s, timeperiod, matype=matype).values
        sd = Rolling(Series(s), timeperiod, None).std(ddof=0).values
        return _multi(upperband=mid + nbdevup * sd, middleband=mid, lowerband=mid - nbdevdn * sd)

    def STDDEV(self, x: Any, timeperiod: int = 5, nbdev: float = 1.0, price: str = "close") -> Series:
        return _out(Rolling(Series(_src(x, price)), timeperiod, None).std(ddof=0).values * nbdev)

    def VAR(self, x: Any, timeperiod: int = 5, nbdev: float = 1.0, price: str = "close") -> Series:
        return _out(Rolling(Series(_src(x, price)), timeperiod, None).var(ddof=0).values * nbdev)

    def MAX(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        return _out(Rolling(Series(_src(x, price)), _tp(k, a, 30), None).max().values)

    def MIN(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        return _out(Rolling(Series(_src(x, price)), _tp(k, a, 30), None).min().values)

    def SUM(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        return _out(Rolling(Series(_src(x, price)), _tp(k, a, 30), None).sum().values)

    def MOM(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 10)
        s = _src(x, price)
        return _out(s - Series(s).shift(n).values)

    def ROC(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 10)
        s = _src(x, price)
        p = Series(s).shift(n).values
        with np.errstate(all="ignore"):
            return _out((s / p - 1) * 100)

    def ROCP(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        return _out(self.ROC(x, *a, price=price, **k).values / 100)

    def TRANGE(self, df: Any) -> Series:
        h, l_, c = _hlc(df)
        pc = Series(c).shift(1).values
        tr = np.fmax(h - l_, np.fmax(np.abs(h - pc), np.abs(l_ - pc)))
        tr[0] = NAN
        return _out(tr)

    def ATR(self, df: Any, *a: Any, **k: Any) -> Series:
        n = _tp(k, a, 14)
        tr = self.TRANGE(df).values
        out = np.full(len(tr), NAN)
        if len(tr) <= n:
            return _out(out)
        prev = float(np.mean(tr[1 : n + 1]))
        out[n] = prev
        for i in range(n + 1, len(tr)):
            prev = (prev * (n - 1) + tr[i]) / n
            out[i] = prev
        return _out(out)

    def NATR(self, df: Any, *a: Any, **k: Any) -> Series:
        with np.errstate(all="ignore"):
            return _out(self.ATR(df, *a, **k).values / _src(df) * 100)

    def _dm(self, df: Any, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        h, l_, _c = _hlc(df)
        up = np.diff(h, prepend=NAN)
        dn = -np.diff(l_, prepend=NAN)
        pdm = np.where((up > dn) & (up > 0), up, 0.0)
        mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
        tr = self.TRANGE(df).values
        pdm[0] = mdm[0] = tr[0] = 0.0
        return _wilder_sum(pdm, n, 1), _wilder_sum(mdm, n, 1), _wilder_sum(tr, n, 1)

    def PLUS_DI(self, df: Any, *a: Any, **k: Any) -> Series:
        p, _m, t = self._dm(df, _tp(k, a, 14))
        with np.errstate(all="ignore"):
            return _out(100 * p / t)

    def MINUS_DI(self, df: Any, *a: Any, **k: Any) -> Series:
        _p, m, t = self._dm(df, _tp(k, a, 14))
        with np.errstate(all="ignore"):
            return _out(100 * m / t)

    def PLUS_DM(self, df: Any, *a: Any, **k: Any) -> Series:
        return _out(self._dm(df, _tp(k, a, 14))[0])

    def MINUS_DM(self, df: Any, *a: Any, **k: Any) -> Series:
        return _out(self._dm(df, _tp(k, a, 14))[1])

    def DX(self, df: Any, *a: Any, **k: Any) -> Series:
        n = _tp(k, a, 14)
        p, m, t = self._dm(df, n)
        with np.errstate(all="ignore"):
            pdi, mdi = 100 * p / t, 100 * m / t
            return _out(100 * np.abs(pdi - mdi) / (pdi + mdi))

    def ADX(self, df: Any, *a: Any, **k: Any) -> Series:
        n = _tp(k, a, 14)
        dx = self.DX(df, n).values
        out = np.full(len(dx), NAN)
        st = _first_valid(dx)
        if len(dx) - st < n:
            return _out(out)
        prev = float(np.mean(dx[st : st + n]))
        out[st + n - 1] = prev
        for i in range(st + n, len(dx)):
            prev = (prev * (n - 1) + dx[i]) / n
            out[i] = prev
        return _out(out)

    def ADXR(self, df: Any, *a: Any, **k: Any) -> Series:
        n = _tp(k, a, 14)
        adx = self.ADX(df, n).values
        return _out((adx + Series(adx).shift(n - 1).values) / 2)

    def CCI(self, df: Any, *a: Any, **k: Any) -> Series:
        n = _tp(k, a, 14)
        h, l_, c = _hlc(df)
        tp = (h + l_ + c) / 3
        out = np.full(len(tp), NAN)
        if len(tp) >= n:
            view = np.lib.stride_tricks.sliding_window_view(tp, n)
            m = view.mean(axis=1)
            md = np.abs(view - m[:, None]).mean(axis=1)
            with np.errstate(all="ignore"):
                out[n - 1 :] = (tp[n - 1 :] - m) / (0.015 * md)
        return _out(out)

    def MFI(self, df: Any, *a: Any, **k: Any) -> Series:
        n = _tp(k, a, 14)
        h, l_, c = _hlc(df)
        v = _src(df, "volume")
        tp = (h + l_ + c) / 3
        mf = tp * v
        d = np.diff(tp, prepend=NAN)
        pos = np.where(d > 0, mf, 0.0)
        neg = np.where(d < 0, mf, 0.0)
        out = np.full(len(tp), NAN)
        for i in range(n, len(tp)):
            p, q = pos[i - n + 1 : i + 1].sum(), neg[i - n + 1 : i + 1].sum()
            out[i] = 100.0 if q == 0 else 100 - 100 / (1 + p / q)
        return _out(out)

    def WILLR(self, df: Any, *a: Any, **k: Any) -> Series:
        n = _tp(k, a, 14)
        h, l_, c = _hlc(df)
        hh = Rolling(Series(h), n, None).max().values
        ll = Rolling(Series(l_), n, None).min().values
        with np.errstate(all="ignore"):
            return _out(-100 * (hh - c) / (hh - ll))

    def _stoch_k(self, h: np.ndarray, l_: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
        hh = Rolling(Series(h), n, None).max().values
        ll = Rolling(Series(l_), n, None).min().values
        with np.errstate(all="ignore"):
            return np.where(hh - ll == 0, 0.0, 100 * (c - ll) / (hh - ll))

    def STOCHF(self, df: Any, fastk_period: int = 5, fastd_period: int = 3, fastd_matype: int = 0) -> DataFrame:
        h, l_, c = _hlc(df)
        k = self._stoch_k(h, l_, c, fastk_period)
        d = self.MA(k, fastd_period, matype=fastd_matype).values
        return _multi(fastk=np.where(np.isnan(d), NAN, k), fastd=d)

    def STOCH(
        self,
        df: Any,
        fastk_period: int = 5,
        slowk_period: int = 3,
        slowk_matype: int = 0,
        slowd_period: int = 3,
        slowd_matype: int = 0,
    ) -> DataFrame:
        h, l_, c = _hlc(df)
        k = self._stoch_k(h, l_, c, fastk_period)
        sk = self.MA(k, slowk_period, matype=slowk_matype).values
        sd = self.MA(sk, slowd_period, matype=slowd_matype).values
        return _multi(slowk=np.where(np.isnan(sd), NAN, sk), slowd=sd)

    def STOCHRSI(
        self,
        x: Any,
        timeperiod: int = 14,
        fastk_period: int = 5,
        fastd_period: int = 3,
        fastd_matype: int = 0,
        price: str = "close",
    ) -> DataFrame:
        r = self.RSI(_src(x, price), timeperiod).values
        k = self._stoch_k(r, r, r, fastk_period)
        k = np.where(np.isnan(Rolling(Series(r), fastk_period, None).max().values), NAN, k)
        d = self.MA(k, fastd_period, matype=fastd_matype).values
        return _multi(fastk=np.where(np.isnan(d), NAN, k), fastd=d)

    def OBV(self, df: Any, *a: Any, **k: Any) -> Series:
        c, v = _src(df), _src(df, "volume")
        d = np.sign(np.diff(c, prepend=c[0] if len(c) else 0))
        obv = np.cumsum(d * v)
        if len(obv):
            obv = obv - obv[0] + v[0]
        return _out(obv)

    def AD(self, df: Any) -> Series:
        h, l_, c = _hlc(df)
        v = _src(df, "volume")
        with np.errstate(all="ignore"):
            clv = np.where(h - l_ == 0, 0.0, ((c - l_) - (h - c)) / (h - l_))
        return _out(np.cumsum(clv * v))

    def ADOSC(self, df: Any, fastperiod: int = 3, slowperiod: int = 10) -> Series:
        ad = self.AD(df).values
        return _out(_ema(ad, fastperiod) - _ema(ad, slowperiod))

    def SAR(self, df: Any, acceleration: float = 0.02, maximum: float = 0.2) -> Series:
        h, l_ = _src(df, "high"), _src(df, "low")
        n = len(h)
        out = np.full(n, NAN)
        if n < 2:
            return _out(out)
        long = h[1] - h[0] > l_[0] - l_[1] or True
        af, ep = acceleration, h[1] if long else l_[1]
        sar = l_[0] if long else h[0]
        for i in range(1, n):
            if long:
                if l_[i] < sar:
                    long, sar, ep, af = False, ep, l_[i], acceleration
                else:
                    if h[i] > ep:
                        ep, af = h[i], min(af + acceleration, maximum)
            else:
                if h[i] > sar:
                    long, sar, ep, af = True, ep, h[i], acceleration
                else:
                    if l_[i] < ep:
                        ep, af = l_[i], min(af + acceleration, maximum)
            out[i] = sar
            sar = sar + af * (ep - sar)
            if long:
                sar = min(sar, l_[i], l_[i - 1])
            else:
                sar = max(sar, h[i], h[i - 1])
        return _out(out)

    def AROON(self, df: Any, *a: Any, **k: Any) -> DataFrame:
        n = _tp(k, a, 14)
        h, l_ = _src(df, "high"), _src(df, "low")
        up = np.full(len(h), NAN)
        dn = np.full(len(h), NAN)
        for i in range(n, len(h)):
            wh, wl = h[i - n : i + 1], l_[i - n : i + 1]
            up[i] = 100 * int(np.argmax(wh)) / n
            dn[i] = 100 * int(np.argmin(wl)) / n
        return _multi(aroondown=dn, aroonup=up)

    def AROONOSC(self, df: Any, *a: Any, **k: Any) -> Series:
        r = self.AROON(df, *a, **k)
        return r["aroonup"] - r["aroondown"]

    def CMO(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        r = self.RSI(x, *a, price=price, **k).values
        return _out(2 * r - 100)

    def PPO(self, x: Any, fastperiod: int = 12, slowperiod: int = 26, matype: int = 0, price: str = "close") -> Series:
        s = _src(x, price)
        f, sl = self.MA(s, fastperiod, matype=matype).values, self.MA(s, slowperiod, matype=matype).values
        with np.errstate(all="ignore"):
            return _out((f - sl) / sl * 100)

    def TRIX(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 30)
        e = _ema(_ema(_ema(_src(x, price), n), n), n)
        return _out(Series(e).pct_change().values * 100)

    def ULTOSC(self, df: Any, timeperiod1: int = 7, timeperiod2: int = 14, timeperiod3: int = 28) -> Series:
        h, l_, c = _hlc(df)
        pc = Series(c).shift(1).values
        bp = c - np.fmin(l_, pc)
        tr = np.fmax(h, pc) - np.fmin(l_, pc)

        def avg(n: int) -> np.ndarray:
            b = Rolling(Series(bp), n, None).sum().values
            t = Rolling(Series(tr), n, None).sum().values
            with np.errstate(all="ignore"):
                return b / t

        out = 100 * (4 * avg(timeperiod1) + 2 * avg(timeperiod2) + avg(timeperiod3)) / 7
        out[:timeperiod3] = NAN
        return _out(out)

    def BOP(self, df: Any) -> Series:
        o, h, l_, c = _src(df, "open"), *_hlc(df)
        with np.errstate(all="ignore"):
            return _out(np.where(h - l_ == 0, 0.0, (c - o) / (h - l_)))

    def TYPPRICE(self, df: Any) -> Series:
        h, l_, c = _hlc(df)
        return _out((h + l_ + c) / 3)

    def MEDPRICE(self, df: Any) -> Series:
        return _out((_src(df, "high") + _src(df, "low")) / 2)

    def AVGPRICE(self, df: Any) -> Series:
        return _out((_src(df, "open") + _src(df, "high") + _src(df, "low") + _src(df, "close")) / 4)

    def LINEARREG_SLOPE(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 14)
        s = _src(x, price)
        out = np.full(len(s), NAN)
        t = np.arange(n, dtype=float)
        tm = t.mean()
        den = ((t - tm) ** 2).sum()
        if len(s) >= n:
            view = np.lib.stride_tricks.sliding_window_view(s, n)
            out[n - 1 :] = ((view - view.mean(axis=1)[:, None]) * (t - tm)).sum(axis=1) / den
        return _out(out)

    def LINEARREG(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 14)
        s = _src(x, price)
        slope = self.LINEARREG_SLOPE(s, n).values
        mean = _sma(s, n)
        return _out(mean + slope * (n - 1) / 2)

    def MIDPOINT(self, x: Any, *a: Any, price: str = "close", **k: Any) -> Series:
        n = _tp(k, a, 14)
        s = Series(_src(x, price))
        return _out((s.rolling(n).max().values + s.rolling(n).min().values) / 2)

    def MIDPRICE(self, df: Any, *a: Any, **k: Any) -> Series:
        n = _tp(k, a, 14)
        return _out(
            (Series(_src(df, "high")).rolling(n).max().values + Series(_src(df, "low")).rolling(n).min().values) / 2
        )

    def __getattr__(self, name: str) -> Any:
        raise NotImplementedError(f"指标 ta.{name} 还不支持（可以让 AI 改写成平台原生写法）")


ta = _TA()
SUPPORTED_TA = sorted(n for n in dir(_TA) if n.isupper() or n in ("MA",))


# ======================================================================== qtpylib 子集
def _s(x: Any) -> Series:
    return x if isinstance(x, Series) else Series(_arr(x))


class _Qt:
    @staticmethod
    def crossed_above(a: Any, b: Any) -> Series:
        a = _s(a)
        b = b if not isinstance(b, (list, np.ndarray)) else Series(b)
        bs = b.shift(1) if isinstance(b, Series) else b
        return (a > b) & (a.shift(1) <= bs)

    @staticmethod
    def crossed_below(a: Any, b: Any) -> Series:
        a = _s(a)
        b = b if not isinstance(b, (list, np.ndarray)) else Series(b)
        bs = b.shift(1) if isinstance(b, Series) else b
        return (a < b) & (a.shift(1) >= bs)

    @staticmethod
    def crossed(a: Any, b: Any) -> Series:
        return _Qt.crossed_above(a, b) | _Qt.crossed_below(a, b)

    @staticmethod
    def typical_price(df: DataFrame) -> Series:
        return (df["high"] + df["low"] + df["close"]) / 3

    @staticmethod
    def mid_price(df: DataFrame) -> Series:
        return (df["high"] + df["low"]) / 2

    @staticmethod
    def rolling_mean(s: Any, window: int = 200, min_periods: int | None = None) -> Series:
        return _s(s).rolling(window, min_periods=min_periods).mean()

    sma = rolling_mean

    @staticmethod
    def rolling_std(s: Any, window: int = 200, min_periods: int | None = None) -> Series:
        return _s(s).rolling(window, min_periods=min_periods).std()

    @staticmethod
    def rolling_min(s: Any, window: int = 14, min_periods: int | None = None) -> Series:
        return _s(s).rolling(window, min_periods=min_periods).min()

    @staticmethod
    def rolling_max(s: Any, window: int = 14, min_periods: int | None = None) -> Series:
        return _s(s).rolling(window, min_periods=min_periods).max()

    @staticmethod
    def rolling_weighted_mean(s: Any, window: int = 200, min_periods: int | None = None) -> Series:
        return ta.WMA(_s(s), window)

    wma = rolling_weighted_mean

    @staticmethod
    def hull_moving_average(s: Any, window: int = 200, min_periods: int | None = None) -> Series:
        s = _s(s)
        half = ta.WMA(s, max(1, window // 2))
        full = ta.WMA(s, window)
        return ta.WMA(2 * half - full, max(1, int(math.sqrt(window))))

    hma = hull_moving_average

    @staticmethod
    def bollinger_bands(s: Any, window: int = 20, stds: float = 2) -> DataFrame:
        s = _s(s)
        ma = s.rolling(window, min_periods=1).mean()
        sd = s.rolling(window, min_periods=1).std()
        return DataFrame({"upper": ma + sd * stds, "mid": ma, "lower": ma - sd * stds})

    @staticmethod
    def weighted_bollinger_bands(s: Any, window: int = 20, stds: float = 2) -> DataFrame:
        s = _s(s)
        ma = ta.WMA(s, window)
        sd = s.rolling(window, min_periods=1).std()
        return DataFrame({"upper": ma + sd * stds, "mid": ma, "lower": ma - sd * stds})

    @staticmethod
    def keltner_channel(df: DataFrame, window: int = 14, atrs: float = 2) -> DataFrame:
        tp = _Qt.typical_price(df)
        mid = tp.rolling(window, min_periods=1).mean()
        atr = ta.ATR(df, window)
        return DataFrame({"upper": mid + atrs * atr, "mid": mid, "lower": mid - atrs * atr})

    @staticmethod
    def heikinashi(df: DataFrame) -> DataFrame:
        o, h, l_, c = (_src(df, k) for k in ("open", "high", "low", "close"))
        hc = (o + h + l_ + c) / 4
        ho = np.empty(len(o))
        if len(o):
            ho[0] = (o[0] + c[0]) / 2
            for i in range(1, len(o)):
                ho[i] = (ho[i - 1] + hc[i - 1]) / 2
        return DataFrame(
            {
                "open": ho,
                "high": np.fmax(h, np.fmax(ho, hc)),
                "low": np.fmin(l_, np.fmin(ho, hc)),
                "close": hc,
                "volume": _src(df, "volume"),
            }
        )

    @staticmethod
    def vwap(df: DataFrame) -> Series:
        tp = _Qt.typical_price(df)
        v = df["volume"]
        return (tp * v).cumsum() / v.cumsum()

    @staticmethod
    def rolling_vwap(df: DataFrame, window: int = 200, min_periods: int | None = None) -> Series:
        tp = _Qt.typical_price(df)
        v = df["volume"]
        return (tp * v).rolling(window, min_periods=min_periods).sum() / v.rolling(
            window, min_periods=min_periods
        ).sum()

    @staticmethod
    def zscore(df: Any, window: int = 20, stds: float = 1, col: str = "close") -> Series:
        s = df[col] if isinstance(df, DataFrame) else _s(df)
        return (s - s.rolling(window).mean()) / (s.rolling(window).std() * stds)

    @staticmethod
    def atr(df: DataFrame, window: int = 14, exp: bool = False) -> Series:
        return ta.ATR(df, window)

    @staticmethod
    def rsi(s: Any, window: int = 14) -> Series:
        return ta.RSI(_s(s), window)

    @staticmethod
    def macd(s: Any, fast: int = 3, slow: int = 10, smooth: int = 16) -> DataFrame:
        s = _s(s)
        m = s.ewm(span=fast).mean() - s.ewm(span=slow).mean()
        sig = m.rolling(smooth).mean()
        return DataFrame({"macd": m, "signal": sig, "histogram": m - sig})

    @staticmethod
    def stoch(df: DataFrame, window: int = 14, d: int = 3, k: int = 3, fast: bool = False) -> DataFrame:
        r = ta.STOCHF(df, window, d) if fast else ta.STOCH(df, window, k, 0, d)
        cols = r.columns
        return DataFrame({"slow_k": r[cols[0]], "slow_d": r[cols[1]]})

    @staticmethod
    def chopiness(df: DataFrame, window: int = 14) -> Series:
        tr = ta.TRANGE(df)
        hh = df["high"].rolling(window).max()
        ll = df["low"].rolling(window).min()
        return 100 * np.log10(tr.rolling(window).sum() / (hh - ll)) / math.log10(window)

    def __getattr__(self, name: str) -> Any:
        raise NotImplementedError(f"qtpylib.{name} 还不支持（可以让 AI 改写成平台原生写法）")


qtpylib = _Qt()


# ======================================================================== logging 替身
class _Logger:
    def __init__(self, name: str = "") -> None:
        self.name = name

    def _log(self, level: str, msg: Any, *args: Any, **_: Any) -> None:
        from . import api

        try:
            text = str(msg) % args if args else str(msg)
        except (TypeError, ValueError):
            text = " ".join(str(x) for x in (msg, *args))
        try:
            getattr(api.log, level)(text)
        except Exception:
            pass

    def debug(self, msg: Any, *a: Any, **k: Any) -> None:
        return None

    def info(self, msg: Any, *a: Any, **k: Any) -> None:
        self._log("info", msg, *a)

    def warning(self, msg: Any, *a: Any, **k: Any) -> None:
        self._log("warning", msg, *a)

    warn = warning

    def error(self, msg: Any, *a: Any, **k: Any) -> None:
        self._log("error", msg, *a)

    exception = critical = error


class _Logging:
    DEBUG, INFO, WARNING, ERROR = 10, 20, 30, 40

    @staticmethod
    def getLogger(name: str = "") -> _Logger:
        return _Logger(name)


logging = _Logging()


# ======================================================================== 策略基类与参数
class BaseParameter:
    def __init__(
        self, *, default: Any, space: str | None = None, optimize: bool = True, load: bool = True, **_: Any
    ) -> None:
        self.value = default
        self.default = default
        self.space = space
        self.optimize = optimize
        self.load = load

    def range_spec(self) -> dict[str, Any] | None:
        return None


class IntParameter(BaseParameter):
    def __init__(self, low: Any, high: int | None = None, *, default: int, **kw: Any) -> None:
        if high is None and isinstance(low, (list, tuple)):
            low, high = low
        super().__init__(default=default, **kw)
        self.low, self.high = int(low), int(high if high is not None else low)

    def range_spec(self) -> dict[str, Any]:
        return {"low": self.low, "high": self.high, "step": 1}


class DecimalParameter(BaseParameter):
    def __init__(self, low: Any, high: float | None = None, *, default: float, decimals: int = 3, **kw: Any) -> None:
        if high is None and isinstance(low, (list, tuple)):
            low, high = low
        super().__init__(default=default, **kw)
        self.low, self.high, self.decimals = float(low), float(high if high is not None else low), decimals

    def range_spec(self) -> dict[str, Any]:
        return {"low": self.low, "high": self.high, "step": 10**-self.decimals}


class RealParameter(DecimalParameter):
    pass


class CategoricalParameter(BaseParameter):
    def __init__(self, categories: list[Any], *, default: Any = None, **kw: Any) -> None:
        super().__init__(default=categories[0] if default is None else default, **kw)
        self.categories = list(categories)

    def range_spec(self) -> dict[str, Any]:
        return {"choices": self.categories}


class BooleanParameter(CategoricalParameter):
    def __init__(self, *, default: bool = True, **kw: Any) -> None:
        super().__init__([True, False], default=default, **kw)


def informative(timeframe: str, asset: str = "", fmt: Any = None, *, ffill: bool = True, **_: Any) -> Any:
    """``@informative("1h")``：被修饰的 populate_indicators_xx 用大周期数据算，结果按 ``列名_1h`` 并回本周期。"""

    def deco(fn: Any) -> Any:
        fn.ft_informative = {"timeframe": timeframe, "asset": asset, "fmt": fmt}
        return fn

    return deco


def timeframe_to_minutes(tf: str) -> int:
    from ..data.quality import TIMEFRAME_MS

    return TIMEFRAME_MS[tf] // 60_000


def timeframe_to_prev_date(tf: str, date: Any = None) -> Any:
    return date


def stoploss_from_open(
    open_relative_stop: float, current_profit: float, is_short: bool = False, leverage: float = 1.0
) -> float:
    if current_profit == -1:
        return 1
    stop = 1 - ((1 + open_relative_stop) / (1 + current_profit))
    return max(stop, 0.0)


def stoploss_from_absolute(
    stop_rate: float, current_rate: float, is_short: bool = False, leverage: float = 1.0
) -> float:
    if current_rate == 0:
        return 1
    return max(0.0, 1 - stop_rate / current_rate)


def merge_informative_pair(
    df: DataFrame,
    informative_df: DataFrame,
    timeframe: str,
    timeframe_inf: str,
    ffill: bool = True,
    append_timeframe: bool = True,
    date_column: str = "date",
    suffix: str | None = None,
) -> DataFrame:
    """把大周期 DataFrame 并到本周期：只用**已收盘**的大周期 bar（与 Freqtrade 相同的防未来对齐）。"""
    from ..data.quality import TIMEFRAME_MS

    inf_ms = TIMEFRAME_MS[timeframe_inf]
    base_ms = TIMEFRAME_MS[timeframe]
    t_inf = informative_df[date_column].values.astype("datetime64[ms]").astype("int64")
    t = df[date_column].values.astype("datetime64[ms]").astype("int64")
    # 大周期 bar 在 open+inf_ms 收盘；本周期 bar 在 open+base_ms 收盘之后才能看到它
    avail = t_inf + inf_ms - base_ms
    pos = np.searchsorted(avail, t, side="right") - 1
    out = df.copy()
    tag = suffix or timeframe_inf
    for c in informative_df.columns:
        vals = informative_df[c].values
        col = np.full(len(t), NAN, dtype=float if vals.dtype.kind in "fiub" else object)
        ok = pos >= 0
        col[ok] = vals[pos[ok]]
        name = f"{c}_{tag}" if append_timeframe else c
        out[name] = col
    return out


class _Dp:
    """DataProvider 替身：get_pair_dataframe / current_whitelist。只给截至当前的已收盘数据。"""

    def __init__(self, adapter: FtAdapter) -> None:
        self.adapter = adapter

    def get_pair_dataframe(self, pair: str, timeframe: str | None = None, candle_type: str = "") -> DataFrame:
        return self.adapter.frame(pair, timeframe)

    historic_ohlcv = get_pair_dataframe

    def get_analyzed_dataframe(self, pair: str, timeframe: str | None = None) -> tuple[DataFrame, Any]:
        return self.adapter.last_frames.get(pair, DataFrame()), None

    def current_whitelist(self) -> list[str]:
        from .runtime import current

        return list(current().securities())

    def runmode(self) -> Any:
        return "backtest"


class IStrategy:
    INTERFACE_VERSION = 3
    minimal_roi: dict[str, float] = {"0": 10.0}
    stoploss: float = -0.99
    trailing_stop = False
    trailing_stop_positive: float | None = None
    trailing_stop_positive_offset = 0.0
    trailing_only_offset_is_reached = False
    timeframe = "5m"
    startup_candle_count = 0
    can_short = False
    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    exit_profit_offset = 0.0
    ignore_roi_if_entry_signal = False
    use_custom_stoploss = False
    position_adjustment_enable = False
    order_types: dict[str, Any] = {}
    order_time_in_force: dict[str, Any] = {}
    plot_config: dict[str, Any] = {}
    max_open_trades: int | None = None

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.dp: Any = None
        self.wallets = None

    def informative_pairs(self) -> list[tuple[str, str]]:
        return []

    def bot_start(self, **kw: Any) -> None:
        return None

    def bot_loop_start(self, **kw: Any) -> None:
        return None

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe


class Trade:
    """custom_stoploss / custom_exit 里的 trade 对象（只读）。"""

    def __init__(self, t: Any) -> None:
        self.pair = getattr(t, "security", "")
        self.open_rate = float(getattr(t, "entry_price", 0) or 0)
        ts = int(getattr(t, "open_ts", 0) or 0)
        self.open_date_utc = datetime.fromtimestamp(ts / 1000, UTC) if ts else None
        self.open_date = self.open_date_utc
        self.amount = float(getattr(t, "amount", 0) or 0)
        self.stake_amount = self.amount * self.open_rate
        self.enter_tag = getattr(t, "entry_tag", "") or None
        self.is_short = bool(getattr(t, "is_short", False))
        self.nr_of_successful_entries = int(getattr(t, "entries", 1) or 1)
        self.max_rate = self.open_rate * (1 + float(getattr(t, "max_profit_ratio", 0) or 0))
        self.min_rate = self.open_rate * (1 + float(getattr(t, "min_profit_ratio", 0) or 0))
        self.leverage = 1.0

    def calc_profit_ratio(self, rate: float) -> float:
        return rate / self.open_rate - 1 if self.open_rate else 0.0


# ======================================================================== 运行适配
class FtAdapter:
    """把一个 IStrategy 子类接到平台内核。每个策略文件一个实例（放在 g 上，随检查点保存的只有参数）。"""

    def __init__(self, cls: type, params: dict[str, Any] | None = None, *, record: bool = False) -> None:
        self.cls = cls
        self.s = cls()
        self.s.dp = _Dp(self)
        self.record = record
        self.signals: list[tuple[int, str, int, int]] = []
        self.last_frames: dict[str, DataFrame] = {}
        self._cache: dict[tuple[str, str], tuple[int, dict[str, np.ndarray]]] = {}
        self.params: dict[str, Any] = {}
        self.offline: DataFrame | None = None  # 不在平台内核里时（整段向量化参照）用这份数据
        self._short_scope_warned = False
        self.configure(params)

    def configure(self, params: dict[str, Any] | None) -> None:
        """平台传入的参数（调参 / 运行配置）覆盖 Parameter 的默认值。"""
        for name, p in parameters(self.cls).items():
            if params and name in params and params[name] is not None:
                p2 = type(p).__new__(type(p))
                p2.__dict__.update(p.__dict__)
                p2.value = params[name]
                setattr(self.s, name, p2)
        self.params = dict(params or {})

    # ---- 数据 ----
    def window(self) -> int:
        st = int(getattr(self.s, "startup_candle_count", 0) or 0)
        # 指标都是因果的：窗口取预热长度的 5 倍（至少 500 根），EMA 起点的影响已小到可以忽略
        return min(2000, max(500, st * 5))

    def frame(self, pair: str, timeframe: str | None = None, count: int | None = None) -> DataFrame:
        from .runtime import current

        if self.offline is not None:
            if timeframe and timeframe != self.s.timeframe:
                return _resample(self.offline, timeframe)
            return self.offline
        rt = current()
        n = count or self.window()
        base_tf = getattr(rt, "timeframe", None) or self.s.timeframe
        if timeframe and timeframe != base_tf:
            bars = rt.bars(
                pair,
                max(50, n // max(1, timeframe_to_minutes(timeframe) // max(1, timeframe_to_minutes(base_tf)))),
                timeframe,
            )
            cols = {k: np.array([b[k] for b in bars], dtype=float) for k in ("open", "high", "low", "close", "volume")}
            cols["date"] = np.array([b["t"] for b in bars], dtype="int64").astype("datetime64[ms]")
            return DataFrame(cols)
        full = rt.bars_by_security.get(pair) or []
        idx = rt._index.get(pair, -1)
        key = (pair, base_tf or "")
        hit = self._cache.get(key)
        if hit is None or hit[0] != len(full):
            arrs = {
                "date": np.array([b.open_time for b in full], dtype="int64").astype("datetime64[ms]"),
                "open": np.array([float(b.open) for b in full]),
                "high": np.array([float(b.high) for b in full]),
                "low": np.array([float(b.low) for b in full]),
                "close": np.array([float(b.close) for b in full]),
                "volume": np.array([float(b.volume) for b in full]),
            }
            self._cache[key] = hit = (len(full), arrs)
        if idx < 0:
            return DataFrame({k: v[:0] for k, v in hit[1].items()})
        lo = max(0, idx - n + 1)
        return DataFrame({k: v[lo : idx + 1] for k, v in hit[1].items()})

    def analyze(self, df: DataFrame, pair: str) -> DataFrame:
        md = {"pair": pair, "timeframe": self.s.timeframe}
        df = self.s.populate_indicators(df, md) or df
        df = self._informative(df, pair)
        enter = getattr(self.s, "populate_entry_trend", None) or getattr(self.s, "populate_buy_trend", None)
        exit_ = getattr(self.s, "populate_exit_trend", None) or getattr(self.s, "populate_sell_trend", None)
        short_enter = getattr(self.s, "populate_short_trend", None)
        short_exit = getattr(self.s, "populate_cover_trend", None)
        if enter:
            df = enter(df, md) or df
        if exit_:
            df = exit_(df, md) or df
        if short_enter:
            df = short_enter(df, md) or df
        if short_exit:
            df = short_exit(df, md) or df
        return df

    def _informative(self, df: DataFrame, pair: str) -> DataFrame:
        for name in dir(type(self.s)):
            fn = getattr(type(self.s), name, None)
            spec = getattr(fn, "ft_informative", None)
            if not spec:
                continue
            inf = self.frame(spec["asset"] or pair, spec["timeframe"])
            inf = fn(self.s, inf, {"pair": spec["asset"] or pair, "timeframe": spec["timeframe"]}) or inf
            df = merge_informative_pair(df, inf, self.s.timeframe, spec["timeframe"], ffill=True)
        return df

    @staticmethod
    def _signal_row(df: DataFrame, i: int = -1) -> dict[str, Any]:
        def col(*names: str) -> Any:
            for n in names:
                if n in df:
                    return df[n].values[i]
            return None

        def on(v: Any) -> int:
            try:
                return 1 if v is not None and not (isinstance(v, float) and math.isnan(v)) and float(v) == 1 else 0
            except (TypeError, ValueError):
                return 0

        def tag(*names: str) -> str:
            v = col(*names)
            return v if isinstance(v, str) else ""

        return {
            "enter_long": on(col("enter_long", "buy")),
            "exit_long": on(col("exit_long", "sell")),
            "enter_short": on(col("enter_short")),
            "exit_short": on(col("exit_short")),
            "enter_tag": tag("enter_tag", "buy_tag"),
            "exit_long_tag": tag("exit_long_tag", "exit_tag", "sell_tag"),
            "exit_short_tag": tag("exit_short_tag", "exit_tag"),
        }

    @staticmethod
    def last_signals(df: DataFrame, i: int = -1) -> tuple[int, int, str, str]:
        row = FtAdapter._signal_row(df, i)
        return row["enter_long"], row["exit_long"], row["enter_tag"], row["exit_long_tag"]

    # ---- 生命周期 ----
    def initialize(self, ctx: Any) -> None:
        from . import api

        s = self.s
        roi = {int(float(k)): float(v) for k, v in (s.minimal_roi or {}).items()}
        if roi and not (len(roi) == 1 and roi.get(0, 0) >= 5):
            api.set_roi(roi)
        sl = float(s.stoploss or -0.99)
        securities = list(ctx.securities)
        for sec in securities:
            if sl > -0.99:
                api.set_stop_loss(sec, abs(sl))
            if s.trailing_stop:
                pos = s.trailing_stop_positive
                if pos is None:
                    api.set_trailing_stop(sec, abs(sl))
                else:
                    off = float(s.trailing_stop_positive_offset or 0)
                    api.set_trailing_stop(sec, float(pos), activate=off if off > 0 else 0.0)
            if sec.upper().endswith(":PERP") and callable(getattr(s, "leverage", None)):
                lev = 1.0
                for pos_side in ("long", "short"):
                    try:
                        raw = s.leverage(
                            pair=sec,
                            current_time=_dt(ctx.current_dt),
                            current_rate=1.0,
                            proposed_leverage=1.0,
                            max_leverage=125.0,
                            entry_tag=None,
                            side=pos_side,
                        )
                        if raw is not None:
                            lev = max(lev, float(raw))
                    except TypeError:
                        continue
                    except Exception as exc:
                        api.log.warning(f"Freqtrade leverage() 调用失败，按 1 倍运行：{exc}")
                        break
                if lev > 1:
                    api.set_leverage(sec, min(125.0, lev))
        try:
            s.bot_start()
        except Exception:
            pass

    def max_trades(self, ctx: Any) -> int:

        n = self.params.get("max_open_trades")
        if n is None:
            n = self.s.max_open_trades or self.s.config.get("max_open_trades") or min(3, len(ctx.securities))
        return max(1, int(n))

    def handle_data(self, ctx: Any) -> None:
        from . import api

        s = self.s
        try:
            s.bot_loop_start(current_time=_dt(ctx.current_dt))
        except Exception:
            pass
        n = self.max_trades(ctx)
        positions = ctx.portfolio.positions
        open_now = sum(1 for p in positions.values() if abs(float(p.amount or 0)) > 0)
        for pair in ctx.securities:
            df = self.analyze(self.frame(pair), pair)
            self.last_frames[pair] = df
            if not len(df):
                continue
            row = self._signal_row(df)
            if self.record:
                self.signals.append((int(ctx.current_dt), pair, row["enter_long"], row["exit_long"]))
            held = float(getattr(positions.get(pair), "amount", 0) or 0)
            perp = pair.upper().endswith(":PERP")
            short_ok = bool(getattr(s, "can_short", False)) and perp
            if getattr(s, "can_short", False) and not perp and not self._short_scope_warned:
                self._short_scope_warned = True
                api.log.warning(f"{pair} 是现货标的：Freqtrade can_short 的做空信号被忽略；做空请使用 :PERP 标的")
            if held > 0 and row["exit_long"] and s.use_exit_signal:
                if s.exit_profit_only:
                    p = positions[pair]
                    price = api.get_current_data()[pair]["last_price"]
                    if float(price) / float(p.avg_cost or price) - 1 <= float(s.exit_profit_offset or 0):
                        continue
                api.order_target(pair, 0, reason=row["exit_long_tag"] or "exit_signal")
            elif held < 0 and row["exit_short"] and s.use_exit_signal:
                if s.exit_profit_only:
                    p = positions[pair]
                    price = api.get_current_data()[pair]["last_price"]
                    if 1 - float(price) / float(p.avg_cost or price) <= float(s.exit_profit_offset or 0):
                        continue
                api.order_target(pair, 0, reason=row["exit_short_tag"] or "exit_signal")
            elif not held and open_now < n:
                side = (
                    "long"
                    if row["enter_long"] and not row["enter_short"]
                    else "short"
                    if short_ok and row["enter_short"] and not row["enter_long"]
                    else ""
                )
                if not side:
                    continue
                ok = True
                confirm = getattr(s, "confirm_trade_entry", None)
                if callable(confirm):
                    try:
                        price = float(df["close"].values[-1])
                        ok = bool(
                            confirm(
                                pair=pair,
                                order_type="market",
                                amount=0.0,
                                rate=price,
                                time_in_force="GTC",
                                current_time=_dt(ctx.current_dt),
                                entry_tag=row["enter_tag"] or None,
                                side=side,
                            )
                        )
                    except TypeError:
                        ok = True
                if ok:
                    target = 0.99 / n if side == "long" else -0.99 / n
                    api.order_target_percent(pair, target, reason=row["enter_tag"] or f"enter_{side}")
                    open_now += 1

    def custom_stoploss(self, ctx: Any, trade: Any) -> Any:
        if not getattr(self.s, "use_custom_stoploss", False) or not hasattr(self.s, "custom_stoploss"):
            return None
        t = Trade(trade)
        rate = float(getattr(trade, "current_price", 0) or 0)
        r = self.s.custom_stoploss(
            pair=t.pair,
            trade=t,
            current_time=_dt(ctx.current_dt),
            current_rate=rate,
            current_profit=t.calc_profit_ratio(rate),
            after_fill=False,
        )
        if r is None:
            return None
        return abs(float(r))

    def custom_exit(self, ctx: Any, trade: Any) -> Any:
        fn = getattr(self.s, "custom_exit", None) or getattr(self.s, "custom_sell", None)
        if not callable(fn):
            return None
        t = Trade(trade)
        rate = float(getattr(trade, "current_price", 0) or 0)
        return fn(
            pair=t.pair,
            trade=t,
            current_time=_dt(ctx.current_dt),
            current_rate=rate,
            current_profit=t.calc_profit_ratio(rate),
        )


def _resample(df: DataFrame, timeframe: str) -> DataFrame:
    """把小周期 K 线合成大周期，只保留走完的桶（和交易所给的大周期 K 线一致）。"""
    step = timeframe_to_minutes(timeframe) * 60_000
    t = df["date"].values.astype("datetime64[ms]").astype("int64")
    if not len(t):
        return DataFrame({k: df[k].values[:0] for k in ("date", "open", "high", "low", "close", "volume")})
    base = int(t[1] - t[0]) if len(t) > 1 else step
    key = t - t % step
    cols: dict[str, list[float]] = {k: [] for k in ("date", "open", "high", "low", "close", "volume")}
    o, h, lo, c, v = (df[k].values for k in ("open", "high", "low", "close", "volume"))
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(t)]
    for a, b in zip(starts, ends, strict=True):
        if t[b - 1] + base < key[a] + step:
            continue  # 没走完的桶
        cols["date"].append(key[a])
        cols["open"].append(o[a])
        cols["high"].append(h[a:b].max())
        cols["low"].append(lo[a:b].min())
        cols["close"].append(c[b - 1])
        cols["volume"].append(v[a:b].sum())
    out = {k: np.array(vals, dtype=float) for k, vals in cols.items() if k != "date"}
    out["date"] = np.array(cols["date"], dtype="int64").astype("datetime64[ms]")
    return DataFrame(out)


def _dt(ms: Any) -> datetime:
    return datetime.fromtimestamp(int(ms or 0) / 1000, UTC)


def parameters(cls: type) -> dict[str, BaseParameter]:
    out: dict[str, BaseParameter] = {}
    for k in dir(cls):
        if k.startswith("_"):
            continue
        v = getattr(cls, k, None)
        if isinstance(v, BaseParameter):
            out[k] = v
    return out


def param_space(cls: type) -> tuple[dict[str, Any], dict[str, Any]]:
    """hyperopt 参数 → 平台的 PARAMS（默认值）与 PARAM_SPACE（调参范围）。"""
    params: dict[str, Any] = {}
    space: dict[str, Any] = {}
    for k, p in parameters(cls).items():
        params[k] = p.default
        spec = p.range_spec()
        if spec and p.optimize:
            space[k] = spec
    return params, space


def vectorized_signals(
    cls: type, df: DataFrame, pair: str = "BTC/USDT", params: dict[str, Any] | None = None
) -> list[tuple[int, int]]:
    """参照：像 Freqtrade 回测那样，在整段数据上一次算完信号（用来核对逐 bar 运行没有偏差）。"""
    ad = FtAdapter(cls, params)
    ad.offline = df
    out = ad.analyze(df, pair)
    return [FtAdapter.last_signals(out, i)[:2] for i in range(len(out))]


__all__ = [
    "BaseParameter",
    "BooleanParameter",
    "CategoricalParameter",
    "DataFrame",
    "DecimalParameter",
    "FtAdapter",
    "IStrategy",
    "IntParameter",
    "RealParameter",
    "Series",
    "Trade",
    "informative",
    "logging",
    "merge_informative_pair",
    "param_space",
    "pd",
    "qtpylib",
    "stoploss_from_absolute",
    "stoploss_from_open",
    "ta",
    "timeframe_to_minutes",
    "timeframe_to_prev_date",
    "vectorized_signals",
]
