"""执行研究（仿真，不下真实委托）：盘口回放、市场状态感知、拆单算法、成交模拟、交易成本分析（TCA）。

口径
----
- **盘口带（Tape）**：每一步一个盘口快照（买卖各若干档）+ 这一步内的成交（量、主动买量、最高最低成交价）。
  来源三种：录制的真实盘口与逐笔成交（最好）；1 分钟 K 线推算（盘口为估计，报告里会标明）；合成数据（只验证流程）。
- **时序**：算法在第 k 步开始时看得到快照 k 与第 k-1 步及以前的成交，下单；市价单立即按快照 k 的盘口逐档吃单；
  限价单在第 k 步的成交里按“排队 + 穿价”规则成交。看不到未来。
- **自身冲击**：吃单把中间价推走（瞬时冲击按指数衰减恢复，一部分永久留下），后续子单在被推过的价格上成交。
- **成本分解**（相对到达中间价，单位 bp，恒等式成立）：
  价差成本（成交价 − 被推过的中间价，被动成交时为负 = 赚到价差）+ 冲击成本（自己推动的中间价）
  + 择时成本（市场自身的漂移）+ 机会成本（没成交部分按结束时中间价计）+ 手续费 = 执行落差（IS）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

LEVELS = 10

ALGOS: dict[str, dict[str, str]] = {
    "market": {"label": "立即市价", "hint": "一次吃完：最快，冲击最大，作为成本上限的参照"},
    "twap": {"label": "TWAP 时间均匀", "hint": "把母单按时间均匀切片；每片先挂单，片末没成交的部分补单"},
    "vwap": {"label": "VWAP 跟量", "hint": "按历史成交量分布切片，量大时多做；目标是贴近区间 VWAP"},
    "pov": {"label": "POV 参与率", "hint": "每一步按上一步市场成交量的固定比例下单，不保证做完"},
    "iceberg": {"label": "冰山单", "hint": "在买一 / 卖一挂一小部分，成交后补上；价格跑开时追价"},
    "almgren_chriss": {"label": "Almgren-Chriss 最优轨迹", "hint": "在冲击成本与价格风险之间折中：越急越前置"},
    "adaptive": {"label": "自适应限价", "hint": "默认被动挂单赚价差；落后计划或行情对自己不利时转主动，流动性差时放慢"},
}
STYLES = {"passive": "被动（只挂单，片末不补）", "neutral": "中性（挂单 + 片末补单）", "aggressive": "主动（直接吃单）"}
STATES = ("平静", "趋势上行", "趋势下行", "剧烈波动", "流动性枯竭")

DEFAULT_CONFIG: dict[str, Any] = {
    "algo": "twap",
    "horizon": 30,  # 母单要在多少步内做完（1 分钟盘口带上 = 30 分钟）
    "slices": 6,
    "style": "neutral",
    "participation": 0.1,  # POV 目标参与率
    "max_participation": 0.25,  # 任何一步主动成交不超过上一步市场成交量的比例
    "urgency": 0.5,  # Almgren-Chriss：0 = 均匀，1 = 很急（κT≈6）
    "display": 0.2,  # 冰山单：每次显示母单的比例
    "reprice_bps": 5.0,
    "finish": True,  # 到期没做完的部分是否市价补齐
    "state_model": "rules",  # rules | hmm
    "adapt": {"behind": 0.08, "ahead": 0.08, "pause_illiquid": True},
    "order": {"side": "both", "size_pct": 0.05},  # size_pct = 母单占预计期间成交量的比例；或给 notional
    "fees": {"maker_bps": 1.0, "taker_bps": 5.0},
    "impact": {"resilience": 1.0, "permanent": 0.3},  # 瞬时冲击的恢复时间（步）、永久部分占比
    "episodes": 30,
}


class ExecutionError(ValueError):
    pass


def normalize_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(raw or {})
    cfg = {**DEFAULT_CONFIG, **{k: v for k, v in raw.items() if k in DEFAULT_CONFIG}}
    for k in ("order", "fees", "impact", "adapt"):
        cfg[k] = {**DEFAULT_CONFIG[k], **(raw.get(k) or {})}
    if cfg["algo"] not in ALGOS:
        raise ExecutionError(f"不认识的执行算法：{cfg['algo']}（可选 {', '.join(ALGOS)}）")
    if cfg["style"] not in STYLES:
        raise ExecutionError(f"style 只能是 {', '.join(STYLES)}")
    if cfg["state_model"] not in ("rules", "hmm"):
        raise ExecutionError("state_model 只能是 rules 或 hmm")
    try:
        cfg["horizon"] = int(min(max(int(cfg["horizon"]), 2), 600))
        cfg["slices"] = int(min(max(int(cfg["slices"]), 1), cfg["horizon"]))
        cfg["episodes"] = int(min(max(int(cfg["episodes"]), 4), 200))
        for k, lo, hi in (
            ("participation", 0.005, 0.5),
            ("max_participation", 0.01, 1.0),
            ("urgency", 0.0, 1.0),
            ("display", 0.01, 1.0),
            ("reprice_bps", 0.0, 200.0),
        ):
            cfg[k] = float(min(max(float(cfg[k]), lo), hi))
        o = cfg["order"]
        if o.get("side") not in ("buy", "sell", "both"):
            raise ExecutionError("order.side 只能是 buy / sell / both")
        if o.get("notional") is not None:
            o["notional"] = float(o["notional"])
            if o["notional"] <= 0:
                raise ExecutionError("order.notional 必须为正")
        o["size_pct"] = float(min(max(float(o.get("size_pct") or 0.05), 0.001), 2.0))
        cfg["fees"] = {k: float(cfg["fees"][k]) for k in ("maker_bps", "taker_bps")}
        cfg["impact"] = {
            "resilience": float(min(max(float(cfg["impact"]["resilience"]), 0.2), 200)),
            "permanent": float(min(max(float(cfg["impact"]["permanent"]), 0.0), 1.0)),
        }
        a = cfg["adapt"]
        cfg["adapt"] = {
            "behind": float(a["behind"]),
            "ahead": float(a["ahead"]),
            "pause_illiquid": bool(a["pause_illiquid"]),
        }
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ExecutionError):
            raise
        raise ExecutionError(f"参数格式不对：{exc}") from exc
    cfg["finish"] = bool(cfg["finish"])
    return cfg


# ---------------------------------------------------------------------------
# 盘口带
# ---------------------------------------------------------------------------
@dataclass
class Tape:
    symbol: str
    t: np.ndarray  # [T] 毫秒
    bid_px: np.ndarray  # [T, L]
    bid_qty: np.ndarray
    ask_px: np.ndarray
    ask_qty: np.ndarray
    volume: np.ndarray  # [T] 这一步的成交量（基础币）
    buy_vol: np.ndarray  # 主动买成交量
    high: np.ndarray  # 这一步的最高 / 最低成交价
    low: np.ndarray
    step_ms: int
    source: str  # recording | kline | synthetic
    note: str = ""
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t)

    @property
    def mid(self) -> np.ndarray:
        return (self.bid_px[:, 0] + self.ask_px[:, 0]) / 2

    @property
    def spread(self) -> np.ndarray:
        return self.ask_px[:, 0] - self.bid_px[:, 0]

    def depth(self, levels: int = 5) -> np.ndarray:
        return self.bid_qty[:, :levels].sum(1) + self.ask_qty[:, :levels].sum(1)

    def slice(self, a: int, b: int) -> Tape:
        return Tape(
            self.symbol,
            self.t[a:b],
            self.bid_px[a:b],
            self.bid_qty[a:b],
            self.ask_px[a:b],
            self.ask_qty[a:b],
            self.volume[a:b],
            self.buy_vol[a:b],
            self.high[a:b],
            self.low[a:b],
            self.step_ms,
            self.source,
            self.note,
            list(self.warnings),
        )

    def summary(self) -> dict[str, Any]:
        mid = self.mid
        sp = self.spread / mid * 1e4
        return {
            "symbol": self.symbol,
            "steps": len(self),
            "step_ms": self.step_ms,
            "source": self.source,
            "start": int(self.t[0]) if len(self) else None,
            "end": int(self.t[-1]) if len(self) else None,
            "spread_bps": round(float(np.median(sp)), 3) if len(self) else None,
            "depth5_usd": round(float(np.median(self.depth(5) * mid)), 0) if len(self) else None,
            "volume_usd_per_step": round(float(np.mean(self.volume * mid)), 0) if len(self) else None,
            "note": self.note,
            "warnings": self.warnings,
        }


def _book(
    mid: np.ndarray, spread: np.ndarray, l1: np.ndarray, levels: int = LEVELS, tick: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """按中间价、价差、一档深度生成对称盘口：档间距 = max(价差/2, 最小价位)，越往外越厚。"""
    T = len(mid)
    gap = np.maximum(spread / 2, tick if tick is not None else mid * 1e-5)
    k = np.arange(levels)
    bid_px = (mid - spread / 2)[:, None] - gap[:, None] * k[None, :]
    ask_px = (mid + spread / 2)[:, None] + gap[:, None] * k[None, :]
    shape = 1 + 0.35 * k  # 外档更厚
    qty = l1[:, None] * shape[None, :]
    return bid_px, qty.copy(), ask_px, qty.copy() if T else qty


def corwin_schultz(high: np.ndarray, low: np.ndarray) -> np.ndarray:
    """Corwin-Schultz (2012) 用相邻两根 K 线的高低价估计买卖价差（比例）。负值截为 0。"""
    h, l_ = np.log(np.maximum(high, 1e-12)), np.log(np.maximum(low, 1e-12))
    beta = (h[1:] - l_[1:]) ** 2 + (h[:-1] - l_[:-1]) ** 2
    gamma = (np.maximum(h[1:], h[:-1]) - np.minimum(l_[1:], l_[:-1])) ** 2
    k = 3 - 2 * math.sqrt(2)
    alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
    s = 2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha))
    s = np.maximum(s, 0)
    return np.concatenate([[s[0] if len(s) else 0.0], s])


def _ewm(x: np.ndarray, hl: float) -> np.ndarray:
    a = 1 - 0.5 ** (1 / max(hl, 1e-9))
    out = np.empty_like(x, dtype=float)
    acc = float(x[0]) if len(x) else 0.0
    for i, v in enumerate(x):
        acc = acc + a * (float(v) - acc)
        out[i] = acc
    return out


def tape_from_bars(
    symbol: str, bars: list[Any], *, step_ms: int = 60_000, depth_frac: float = 0.12, min_spread_bps: float = 0.5
) -> Tape:
    """1 分钟 K 线推算的盘口带：价差用 Corwin-Schultz 估计（平滑并设下限），一档深度 ≈ 近期每步成交量 × depth_frac。

    这是**估计**：真实盘口的排队与撤单无法从 K 线还原。报告里会标明来源。
    """
    if len(bars) < 30:
        raise ExecutionError(f"{symbol} 的 K 线太少（{len(bars)} 根）")
    o = np.array([float(b.open) for b in bars])
    h = np.array([float(b.high) for b in bars])
    lo = np.array([float(b.low) for b in bars])
    v = np.array([max(float(b.volume), 0.0) for b in bars])
    t = np.array([int(b.open_time) for b in bars], dtype=np.int64)
    cs = _ewm(corwin_schultz(h, lo), 30)
    spread = o * np.maximum(cs * 0.5, min_spread_bps / 1e4)  # C-S 在 1 分钟上偏大，打五折再设下限
    vol_ma = _ewm(np.maximum(v, 1e-12), 30)
    l1 = np.maximum(vol_ma * depth_frac, 1e-9)
    bp, bq, ap, aq = _book(o, spread, l1)
    # 没有逐笔方向：按 K 线收盘位置估计主动买比例
    c = np.array([float(b.close) for b in bars])
    pos = np.where(h > lo, (c - lo) / np.maximum(h - lo, 1e-12), 0.5)
    return Tape(
        symbol,
        t,
        bp,
        bq,
        ap,
        aq,
        v,
        v * pos,
        h,
        lo,
        step_ms,
        "kline",
        note="盘口按 1 分钟 K 线推算：价差用 Corwin-Schultz 估计，深度按近期成交量估计；排队与撤单无法还原",
        warnings=["盘口为 K 线推算，被动成交与冲击的结论只能作为参考"],
    )


def tape_from_recording(rec: dict[str, Any]) -> Tape:
    """录制的盘口快照 + 逐笔成交 → 盘口带。第 k 步的成交 = 快照 k 与快照 k+1 之间的逐笔。"""
    snaps = rec.get("snapshots") or []
    trades = sorted(rec.get("trades") or [], key=lambda x: x["t"])
    if len(snaps) < 20:
        raise ExecutionError(f"录制只有 {len(snaps)} 个快照，至少需要 20 个")
    T = len(snaps)
    L = min(LEVELS, min(len(s["bids"]) for s in snaps), min(len(s["asks"]) for s in snaps))
    if L < 1:
        raise ExecutionError("录制里有空盘口")
    t = np.array([int(s["t"]) for s in snaps], dtype=np.int64)
    bp = np.array([[float(x[0]) for x in s["bids"][:L]] for s in snaps])
    bq = np.array([[float(x[1]) for x in s["bids"][:L]] for s in snaps])
    ap = np.array([[float(x[0]) for x in s["asks"][:L]] for s in snaps])
    aq = np.array([[float(x[1]) for x in s["asks"][:L]] for s in snaps])
    if L < LEVELS:  # 补外档，避免大单吃穿
        gap = (ap[:, -1] - bp[:, -1]) / 2 / max(L, 1)
        extra = LEVELS - L
        k = np.arange(1, extra + 1)
        bp = np.hstack([bp, bp[:, -1:] - gap[:, None] * k])
        ap = np.hstack([ap, ap[:, -1:] + gap[:, None] * k])
        bq = np.hstack([bq, np.repeat(bq[:, -1:], extra, 1)])
        aq = np.hstack([aq, np.repeat(aq[:, -1:], extra, 1)])
    vol, bvol = np.zeros(T), np.zeros(T)
    mid = (bp[:, 0] + ap[:, 0]) / 2
    hi, lo = mid.copy(), mid.copy()
    j = 0
    for k in range(T):
        end = t[k + 1] if k + 1 < T else t[k] + (t[k] - t[k - 1] if k else 1000)
        first = True
        while j < len(trades) and trades[j]["t"] < end:
            tr = trades[j]
            if tr["t"] >= t[k]:
                p, q = float(tr["p"]), float(tr["q"])
                vol[k] += q
                if tr.get("side") == "buy":
                    bvol[k] += q
                if first:
                    hi[k], lo[k], first = p, p, False
                else:
                    hi[k], lo[k] = max(hi[k], p), min(lo[k], p)
            j += 1
    step = int(np.median(np.diff(t))) if T > 1 else 1000
    return Tape(
        rec.get("symbol") or "",
        t,
        bp,
        bq,
        ap,
        aq,
        vol,
        bvol,
        hi,
        lo,
        step,
        "recording",
        note=f"录制的真实盘口（{rec.get('exchange') or ''}，每 {step / 1000:g} 秒一个快照，{L} 档）",
    )


def synthetic_tape(
    symbol: str = "BTC/USDT", steps: int = 3000, *, seed: int = 7, price: float = 60_000.0, step_ms: int = 60_000
) -> Tape:
    """合成盘口带：三种状态（平静 / 剧烈 / 流动性差）马尔可夫切换，价差、深度、成交量随状态变化，带日内成交量形态。"""
    rng = np.random.default_rng(seed)
    P = np.array([[0.985, 0.01, 0.005], [0.04, 0.95, 0.01], [0.05, 0.01, 0.94]])
    sig = np.array([0.0006, 0.0018, 0.0012])
    spr = np.array([1.0, 3.0, 6.0]) / 1e4
    dep = np.array([1.0, 0.6, 0.25])
    s = np.zeros(steps, dtype=int)
    for i in range(1, steps):
        s[i] = rng.choice(3, p=P[s[i - 1]])
    drift = np.cumsum(rng.normal(0, 1, steps)) * 0
    r = rng.normal(0, sig[s]) + 0.00002 * np.sin(np.arange(steps) / 400) + drift * 0
    mid = price * np.exp(np.cumsum(r))
    t = 1_700_000_000_000 + np.arange(steps, dtype=np.int64) * step_ms
    minute = (t // 60_000) % 1440
    prof = 1 + 0.6 * np.cos((minute - 14 * 60) / 1440 * 2 * np.pi)  # UTC 14 点附近量最大
    vol_usd = 2.5e6 * prof * dep[s] ** 0.5 * rng.lognormal(0, 0.5, steps)
    v = vol_usd / mid
    spread = mid * spr[s] * rng.uniform(0.7, 1.3, steps)
    l1 = np.maximum(v * 0.1 * dep[s], 1e-6)
    bp, bq, ap, aq = _book(mid, spread, l1)
    nxt = np.concatenate([mid[1:], mid[-1:]])
    amp = np.abs(rng.normal(0, sig[s] * 0.8, steps)) * mid
    hi = np.maximum(mid, nxt) + amp + spread / 2  # 最高成交价 = 路径最高中间价上的卖一
    lo = np.minimum(mid, nxt) - amp - spread / 2
    bv = v * np.clip(0.5 + (nxt - mid) / np.maximum(mid * sig[s] * 4, 1e-9), 0.05, 0.95)
    return Tape(
        symbol,
        t,
        bp,
        bq,
        ap,
        aq,
        v,
        bv,
        hi,
        lo,
        step_ms,
        "synthetic",
        note="合成盘口（只用来验证流程）",
        warnings=["使用了合成盘口，结论不能作为研究结论"],
    )


# ---------------------------------------------------------------------------
# 市场状态
# ---------------------------------------------------------------------------
def state_features(tape: Tape) -> dict[str, np.ndarray]:
    """第 k 个值只用快照 k 与第 k-1 步及以前的成交（不看未来）。"""
    mid = tape.mid
    r = np.concatenate([[0.0], np.diff(np.log(mid))])
    vol_s = np.sqrt(_ewm(r**2, 10))
    vol_l = np.sqrt(_ewm(r**2, 240))
    trend = _ewm(r, 10)
    spread = tape.spread / mid
    sp_l = _ewm(spread, 240)
    d = tape.depth(5)
    d_l = _ewm(d, 240)
    v_prev = np.concatenate([[tape.volume[0]], tape.volume[:-1]])
    v_l = _ewm(v_prev, 240)
    bv_prev = np.concatenate([[tape.buy_vol[0]], tape.buy_vol[:-1]])
    ofi = _ewm((2 * bv_prev - v_prev) / np.maximum(v_prev, 1e-12), 5)
    return {
        "ret": r,
        "vol_ratio": vol_s / np.maximum(vol_l, 1e-12),
        "trend_z": trend / np.maximum(vol_s / math.sqrt(10), 1e-12),
        "spread_ratio": spread / np.maximum(sp_l, 1e-12),
        "depth_ratio": d / np.maximum(d_l, 1e-12),
        "volume_ratio": v_prev / np.maximum(v_l, 1e-12),
        "ofi": ofi,
        "vol_bps": vol_s * 1e4,
        "spread_bps": spread * 1e4,
    }


def rule_states(f: dict[str, np.ndarray]) -> np.ndarray:
    T = len(f["ret"])
    out = np.zeros(T, dtype=int)
    for k in range(T):
        if f["depth_ratio"][k] < 0.5 or f["spread_ratio"][k] > 2.2:
            out[k] = 4
        elif f["vol_ratio"][k] > 1.8:
            out[k] = 3
        elif f["trend_z"][k] > 1.6:
            out[k] = 1
        elif f["trend_z"][k] < -1.6:
            out[k] = 2
    return out


def hmm_fit(x: np.ndarray, n: int = 3, iters: int = 40, seed: int = 0) -> dict[str, np.ndarray]:
    """一维高斯 HMM（Baum-Welch，缩放前后向）。用于把收益按波动分成 n 个状态。"""
    x = np.asarray(x, dtype=float)
    T = len(x)
    if T < 50:
        raise ExecutionError("样本太少，无法拟合 HMM")
    qs = np.quantile(np.abs(x), np.linspace(0.2, 0.9, n))
    mu = np.zeros(n)
    var = np.maximum(qs**2, 1e-12)
    A = np.full((n, n), 0.05 / (n - 1))
    np.fill_diagonal(A, 0.95)
    pi = np.full(n, 1 / n)
    for _ in range(iters):
        B = np.exp(-((x[:, None] - mu) ** 2) / (2 * var)) / np.sqrt(2 * np.pi * var) + 1e-300
        alpha = np.zeros((T, n))
        c = np.zeros(T)
        alpha[0] = pi * B[0]
        c[0] = alpha[0].sum()
        alpha[0] /= c[0]
        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ A) * B[t]
            c[t] = alpha[t].sum()
            alpha[t] /= c[t]
        beta = np.ones((T, n))
        for t in range(T - 2, -1, -1):
            beta[t] = (A @ (B[t + 1] * beta[t + 1])) / c[t + 1]
        gamma = alpha * beta
        gamma /= gamma.sum(1, keepdims=True)
        xi = np.zeros((n, n))
        for t in range(T - 1):
            m = alpha[t][:, None] * A * (B[t + 1] * beta[t + 1])[None, :] / c[t + 1]
            xi += m
        A = xi / np.maximum(xi.sum(1, keepdims=True), 1e-300)
        pi = gamma[0]
        w = gamma.sum(0)
        mu = (gamma * x[:, None]).sum(0) / np.maximum(w, 1e-300)
        var = np.maximum((gamma * (x[:, None] - mu) ** 2).sum(0) / np.maximum(w, 1e-300), 1e-14)
    order = np.argsort(var)
    return {"A": A[np.ix_(order, order)], "mu": mu[order], "var": var[order], "pi": pi[order]}


def hmm_filter(x: np.ndarray, model: dict[str, np.ndarray]) -> np.ndarray:
    """前向滤波：第 t 个概率只用到 t 及以前的数据。"""
    A, mu, var, pi = model["A"], model["mu"], model["var"], model["pi"]
    p = pi.copy()
    out = np.zeros((len(x), len(mu)))
    for t, v in enumerate(x):
        b = np.exp(-((v - mu) ** 2) / (2 * var)) / np.sqrt(2 * np.pi * var) + 1e-300
        p = (p @ A if t else p) * b
        p /= p.sum()
        out[t] = p
    return out


def detect_states(tape: Tape, model: str = "rules", fit_upto: int | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    """返回每一步的状态编号（STATES 的下标）与说明。HMM 只用 fit_upto 之前的数据拟合，之后只做前向滤波。"""
    f = state_features(tape)
    states = rule_states(f)
    info: dict[str, Any] = {"model": "rules"}
    if model == "hmm":
        n_fit = fit_upto or max(200, len(tape) // 3)
        try:
            m = hmm_fit(f["ret"][1:n_fit])
            prob = hmm_filter(f["ret"], m)
            hi_vol = prob[:, -1] > 0.6
            liq = states == 4
            trend = np.where(f["trend_z"] > 1.6, 1, np.where(f["trend_z"] < -1.6, 2, 0))
            states = np.where(liq, 4, np.where(hi_vol, 3, trend))
            info = {
                "model": "hmm",
                "vol_bps": [round(float(math.sqrt(v) * 1e4), 2) for v in m["var"]],
                "stay": [round(float(m["A"][i, i]), 3) for i in range(len(m["var"]))],
                "fit_steps": n_fit,
            }
        except ExecutionError:
            info = {"model": "rules", "note": "样本太少，HMM 退回规则"}
    return states, info


# ---------------------------------------------------------------------------
# 成交模拟
# ---------------------------------------------------------------------------
@dataclass
class Fill:
    k: int
    qty: float
    price: float
    maker: bool
    mid: float  # 成交时被推过的中间价
    mid0: float  # 成交时未受自身影响的中间价
    fee: float


@dataclass
class SimState:
    side: int
    Q: float
    filled: float = 0.0
    impact: float = 0.0  # 瞬时冲击（价格单位，买为正）
    perm: float = 0.0  # 永久冲击
    order_px: float | None = None
    order_qty: float = 0.0
    ahead: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(self.Q - self.filled, 0.0)


class Simulator:
    def __init__(self, tape: Tape, cfg: dict[str, Any]) -> None:
        self.tape = tape
        self.cfg = cfg
        self.mid = tape.mid
        self.decay = math.exp(-1 / cfg["impact"]["resilience"])
        self.perm_frac = cfg["impact"]["permanent"]
        self.maker_fee = cfg["fees"]["maker_bps"] / 1e4
        self.taker_fee = cfg["fees"]["taker_bps"] / 1e4

    def shift(self, st: SimState) -> float:
        return st.impact + st.perm

    def market(self, st: SimState, k: int, qty: float) -> float:
        """立即按快照 k 的盘口（叠加自身冲击）逐档吃单。返回成交量。"""
        qty = min(qty, st.remaining)
        if qty <= 1e-12:
            return 0.0
        sh = self.shift(st)
        px = (self.tape.ask_px[k] if st.side > 0 else self.tape.bid_px[k]) + sh
        avail = self.tape.ask_qty[k] if st.side > 0 else self.tape.bid_qty[k]
        left, cost = qty, 0.0
        last = px[0]
        at_best = min(qty, float(avail[0]))
        for p, a in zip(px, avail, strict=True):
            take = min(left, a)
            cost += take * p
            left -= take
            last = p
            if left <= 1e-12:
                break
        if left > 1e-12:  # 吃穿了：剩余按最外档再往外一档成交
            gap = abs(px[-1] - px[-2]) if len(px) > 1 else px[0] * 1e-4
            p = px[-1] + st.side * gap
            cost += left * p
            last = p
        done = qty
        avg = cost / done
        m_shift = self.mid[k] + sh
        st.fills.append(Fill(k, done, avg, False, m_shift, self.mid[k], done * avg * self.taker_fee))
        st.filled += done
        # 冲击：吃穿的档位直接移动中间价；在一档内按吃掉的比例推半个价差。分成瞬时（会恢复）与永久两部分
        half = abs(px[0] - m_shift)
        push = st.side * (abs(last - px[0]) + half * at_best / max(float(avail[0]), 1e-12))
        st.impact += push * (1 - self.perm_frac)
        st.perm += push * self.perm_frac
        return done

    def place_limit(self, st: SimState, k: int, price: float, qty: float) -> None:
        """挂（或改）一张限价单。价格在本方最优价时排在已有挂单之后；改善价格（进入价差）则排第一。"""
        qty = min(qty, st.remaining)
        if qty <= 1e-12:
            self.cancel(st)
            return
        sh = self.shift(st)
        bid, ask = self.tape.bid_px[k, 0] + sh, self.tape.ask_px[k, 0] + sh
        if (st.side > 0 and price >= ask) or (st.side < 0 and price <= bid):
            self.cancel(st)
            self.market(st, k, qty)  # 可成交的限价 = 吃单
            return
        if st.order_px is not None and abs(st.order_px - price) < 1e-12:
            st.order_qty = qty  # 只改数量，不丢队列位置
            return
        st.order_px, st.order_qty = price, qty
        own = self.tape.bid_px[k] + sh if st.side > 0 else self.tape.ask_px[k] + sh
        qtys = self.tape.bid_qty[k] if st.side > 0 else self.tape.ask_qty[k]
        hit = np.where(np.abs(own - price) <= abs(price) * 1e-9)[0]
        st.ahead = float(qtys[hit[0]]) if len(hit) else 0.0

    def cancel(self, st: SimState) -> None:
        st.order_px, st.order_qty, st.ahead = None, 0.0, 0.0

    def step_limit(self, st: SimState, k: int) -> None:
        """第 k 步的成交对挂单的影响：穿价 → 全部成交；触价 → 先消耗前面的队列，再轮到我们。"""
        if st.order_px is None or st.order_qty <= 1e-12:
            return
        sh = self.shift(st)
        p = st.order_px
        V = self.tape.volume[k]
        opp = V - self.tape.buy_vol[k] if st.side > 0 else self.tape.buy_vol[k]  # 打到我们这边的主动成交
        lo, hi = self.tape.low[k] + sh, self.tape.high[k] + sh
        own = self.tape.bid_px[k] if st.side > 0 else self.tape.ask_px[k]
        gap = abs(own[0] - own[1]) if len(own) > 1 else abs(p) * 1e-4
        eps = abs(p) * 1e-9
        fill = 0.0
        if (st.side > 0 and lo < p - gap + eps) or (st.side < 0 and hi > p + gap - eps):
            fill = st.order_qty  # 成交价穿过我们这一档：前面的队列已清空，全部成交
            st.ahead = 0.0
        elif (st.side > 0 and lo <= p + eps) or (st.side < 0 and hi >= p - eps):
            at_touch = max(opp, 0.0) * 0.5  # 触价：这一步打到本方的主动成交约一半落在这一档
            if at_touch > st.ahead:
                fill = min(st.order_qty, at_touch - st.ahead)
                st.ahead = 0.0
            else:
                st.ahead -= at_touch
        fill = min(fill, st.remaining)
        if fill > 1e-12:
            st.fills.append(Fill(k, fill, p, True, self.mid[k] + sh, self.mid[k], fill * p * self.maker_fee))
            st.filled += fill
            st.order_qty -= fill
            if st.order_qty <= 1e-12:
                self.cancel(st)

    def decay_impact(self, st: SimState) -> None:
        st.impact *= self.decay


# ---------------------------------------------------------------------------
# 算法
# ---------------------------------------------------------------------------
def volume_profile(tape: Tape, upto: int, horizon: int, start: int) -> tuple[np.ndarray, str]:
    """按“一天中的分钟”统计 upto 之前的平均成交量，取 [start, start+horizon) 对应的形态。数据不足 3 天时用均匀分布。"""
    steps_per_day = int(86_400_000 // tape.step_ms) if tape.step_ms else 0
    if steps_per_day and upto >= steps_per_day * 3:
        minute = ((tape.t[:upto] // tape.step_ms) % steps_per_day).astype(int)
        prof = np.bincount(minute, weights=tape.volume[:upto], minlength=steps_per_day) / np.maximum(
            np.bincount(minute, minlength=steps_per_day), 1
        )
        idx = ((tape.t[start : start + horizon] // tape.step_ms) % steps_per_day).astype(int)
        w = prof[idx]
        if w.sum() > 0:
            return w / w.sum(), "historical"
    return np.full(horizon, 1 / horizon), "uniform"


def ac_trajectory(horizon: int, urgency: float) -> np.ndarray:
    """Almgren-Chriss 剩余持仓轨迹 x_k / X = sinh(κ(T-k)) / sinh(κT)，返回累计完成比例（长度 horizon+1）。"""
    kT = 0.1 + 6.0 * urgency
    kappa = kT / horizon
    k = np.arange(horizon + 1)
    rem = np.sinh(kappa * (horizon - k)) / math.sinh(kT)
    return 1 - rem


def target_curve(cfg: dict[str, Any], tape: Tape, start: int) -> tuple[np.ndarray, dict[str, Any]]:
    """计划累计完成比例：curve[j] = 第 j 步结束时应该完成的比例（j = 0..H-1）。"""
    H = cfg["horizon"]
    algo = cfg["algo"]
    info: dict[str, Any] = {}
    if algo == "vwap" or (algo == "adaptive" and start >= 3 * 1440):
        w, how = volume_profile(tape, start, H, start)
        info["profile"] = how
        curve = np.cumsum(w)
    elif algo == "almgren_chriss":
        curve = ac_trajectory(H, cfg["urgency"])[1:]
    elif algo == "market":
        curve = np.ones(H)
    else:
        S = cfg["slices"]
        edges = np.linspace(0, H, S + 1).round().astype(int)
        curve = np.zeros(H)
        for i in range(S):
            curve[edges[i] : edges[i + 1]] = (i + 1) / S
        if algo in ("adaptive", "iceberg", "pov"):
            curve = (np.arange(H) + 1) / H
    return np.clip(curve, 0, 1), info


def run_episode(
    tape: Tape, cfg: dict[str, Any], start: int, side: int, Q: float, states: np.ndarray, *, record: bool = False
) -> dict[str, Any]:
    sim = Simulator(tape, cfg)
    st = SimState(side=side, Q=Q)
    H = cfg["horizon"]
    algo, style = cfg["algo"], cfg["style"]
    curve, info = target_curve(cfg, tape, start)
    edges = set(np.linspace(0, H, cfg["slices"] + 1).round().astype(int)[1:] - 1)
    cap = cfg["max_participation"]
    timeline: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    pov_target = 0.0
    last_state = int(states[start])
    for j in range(H):
        k = start + j
        sh = sim.shift(st)
        bid, ask = tape.bid_px[k, 0] + sh, tape.ask_px[k, 0] + sh
        touch = bid if side > 0 else ask
        prev_v = tape.volume[k - 1] if k > 0 else tape.volume[k]
        cap_qty = cap * prev_v if algo != "market" else float("inf")
        s = int(states[k])
        want = curve[j] * Q
        behind = want - st.filled
        actions: list[str] = []
        if algo == "market":
            if j == 0:
                sim.market(st, k, Q)
                actions.append("市价全部")
        elif algo == "pov":
            pov_target += cfg["participation"] * prev_v
            need = min(pov_target, Q) - st.filled
            if need > 0:
                sim.place_limit(st, k, touch, need)
                if need > cfg["participation"] * prev_v * 2:
                    sim.market(st, k, min(need - cfg["participation"] * prev_v, cap_qty))
                    actions.append("落后补单")
        elif algo == "iceberg":
            show = max(Q * cfg["display"], 1e-12)
            if st.order_px is not None and abs(st.order_px - touch) / touch * 1e4 > cfg["reprice_bps"]:
                sim.cancel(st)
                actions.append("追价")
            if st.order_px is None:
                sim.place_limit(st, k, touch, min(show, st.remaining))
        elif algo in ("twap", "vwap", "almgren_chriss"):
            if style == "aggressive" or algo == "almgren_chriss":
                if behind > 0:
                    sim.market(st, k, min(behind, cap_qty))
            else:
                slice_left = max(want - st.filled, 0.0)
                nxt = curve[min(j + 1, H - 1)] * Q - st.filled
                if slice_left > 0 or nxt > 0:
                    sim.place_limit(st, k, touch, max(slice_left, nxt, 0.0))
                if style == "neutral" and j in edges and behind > Q * 0.002:
                    sim.cancel(st)
                    sim.market(st, k, min(behind, cap_qty))
                    actions.append("片末补单")
        elif algo == "adaptive":
            a = cfg["adapt"]
            b = behind / Q
            adverse = (side > 0 and s == 1) or (side < 0 and s == 2)
            favorable = (side > 0 and s == 2) or (side < 0 and s == 1)
            illiquid = s == 4 and a["pause_illiquid"]
            if adverse:
                b += 0.1  # 行情对自己不利：提前做
            if favorable:
                b -= 0.05  # 行情对自己有利：等一等
            spread = ask - bid
            if illiquid and b < 2 * a["behind"]:
                sim.place_limit(st, k, touch, max(behind, 0) + Q * 0.02)
                actions.append("流动性差，只挂单")
            elif b > a["behind"]:
                sim.market(st, k, min(behind + (0.1 * Q if adverse else 0), cap_qty))
                sim.place_limit(st, k, touch, max(curve[min(j + 1, H - 1)] * Q - st.filled, 0))
                actions.append("转主动")
            elif b > -a["ahead"]:
                px = touch + side * spread * 0.4 if spread > touch * 2e-4 else touch
                sim.place_limit(st, k, px, max(curve[min(j + 2, H - 1)] * Q - st.filled, Q * 0.01))
            else:
                sim.cancel(st)
        if j == H - 1 and cfg["finish"] and st.remaining > 1e-12:
            sim.cancel(st)
            sim.market(st, k, st.remaining)
            actions.append("到期补齐")
        sim.step_limit(st, k)
        sim.decay_impact(st)
        if record:
            if s != last_state:
                alerts.append({"k": j, "level": "info", "text": f"市场状态：{STATES[last_state]} → {STATES[s]}"})
            dev = (st.filled - want) / Q
            if dev < -0.15 and not any(x.get("kind") == "behind" and j - x["k"] < 10 for x in alerts):
                alerts.append({"k": j, "level": "warn", "kind": "behind", "text": f"落后计划 {abs(dev):.0%}"})
            timeline.append(
                {
                    "j": j,
                    "t": int(tape.t[k]),
                    "mid": float(tape.mid[k]),
                    "mid_shift": float(tape.mid[k] + sim.shift(st)),
                    "target": float(want / Q),
                    "filled": float(st.filled / Q),
                    "state": s,
                    "order_px": st.order_px,
                    "actions": actions,
                }
            )
        last_state = s
        if st.remaining <= 1e-12 and algo != "pov":
            if record:
                for jj in range(j + 1, H):
                    kk = start + jj
                    timeline.append(
                        {
                            "j": jj,
                            "t": int(tape.t[kk]),
                            "mid": float(tape.mid[kk]),
                            "mid_shift": float(tape.mid[kk] + sim.shift(st)),
                            "target": float(curve[jj]),
                            "filled": 1.0,
                            "state": int(states[kk]),
                            "order_px": None,
                            "actions": [],
                        }
                    )
                    sim.decay_impact(st)
            break
    end = min(start + H, len(tape) - 1)
    out = tca(tape, st, start, end, Q, side)
    out["profile"] = info.get("profile")
    if record:
        out["timeline"] = timeline
        out["alerts"] = alerts
        out["fills"] = [{"j": f.k - start, "qty": f.qty / Q, "price": f.price, "maker": f.maker} for f in st.fills]
    return out


# ---------------------------------------------------------------------------
# 交易成本分析
# ---------------------------------------------------------------------------
def tca(tape: Tape, st: SimState, start: int, end: int, Q: float, side: int) -> dict[str, Any]:
    m0 = float(tape.mid[start])
    notional = Q * m0
    filled = sum(f.qty for f in st.fills)
    spread_c = sum(f.qty * side * (f.price - f.mid) for f in st.fills) / notional * 1e4
    impact_c = sum(f.qty * side * (f.mid - f.mid0) for f in st.fills) / notional * 1e4
    timing_c = sum(f.qty * side * (f.mid0 - m0) for f in st.fills) / notional * 1e4
    fee_c = sum(f.fee for f in st.fills) / notional * 1e4
    m_end = float(tape.mid[end]) + st.perm + st.impact
    opp_c = (Q - filled) * side * (m_end - m0) / notional * 1e4
    is_bps = spread_c + impact_c + timing_c + fee_c + opp_c
    avg = sum(f.qty * f.price for f in st.fills) / filled if filled > 0 else None
    seg = slice(start, end + 1)
    tp = (tape.high[seg] + tape.low[seg] + tape.mid[seg]) / 3
    vw = float((tp * tape.volume[seg]).sum() / max(tape.volume[seg].sum(), 1e-12))
    mk = {}
    for h in (1, 5, 15):
        vals = [(f.qty, side * (tape.mid[min(f.k + h, len(tape) - 1)] - f.price) / f.price * 1e4) for f in st.fills]
        mk[h] = sum(q * v for q, v in vals) / max(sum(q for q, _ in vals), 1e-12) if vals else None
    last_k = max((f.k for f in st.fills), default=start)
    mvol = float(tape.volume[start : last_k + 1].sum())
    return {
        "is_bps": is_bps,
        "spread_bps": spread_c,
        "impact_bps": impact_c,
        "timing_bps": timing_c,
        "fee_bps": fee_c,
        "opportunity_bps": opp_c,
        "vs_vwap_bps": side * (avg - vw) / vw * 1e4 if avg else None,
        "vs_arrival_bps": side * (avg - m0) / m0 * 1e4 if avg else None,
        "fill_rate": filled / Q,
        "maker_ratio": sum(f.qty for f in st.fills if f.maker) / max(filled, 1e-12),
        "children": len(st.fills),
        "duration": (last_k - start + 1) if st.fills else 0,
        "participation": filled / max(mvol, 1e-12),
        "markout_1": mk[1],
        "markout_5": mk[5],
        "markout_15": mk[15],
        "arrival": m0,
        "avg_price": avg,
        "vwap": vw,
        "side": side,
        "qty": Q,
        "notional": notional,
    }


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------
def order_qty(tape: Tape, cfg: dict[str, Any], start: int) -> float:
    o = cfg["order"]
    m0 = float(tape.mid[start])
    if o.get("notional"):
        return o["notional"] / m0
    look = tape.volume[max(0, start - 240) : start]
    per_step = float(look.mean()) if len(look) else float(tape.volume[:50].mean())
    return max(per_step * cfg["horizon"] * o["size_pct"], 1e-9)


def episode_starts(tape: Tape, cfg: dict[str, Any], warmup: int = 240) -> list[int]:
    H = cfg["horizon"]
    lo = min(warmup, max(len(tape) // 5, 20))
    hi = len(tape) - H - 16
    if hi <= lo:
        raise ExecutionError(f"盘口带太短：{len(tape)} 步，执行期 {H} 步加上预热至少需要 {lo + H + 16} 步")
    n = min(cfg["episodes"], max(1, (hi - lo) // max(H // 2, 1)))
    return [int(x) for x in np.linspace(lo, hi, n).round()]


KEYS = (
    "is_bps",
    "spread_bps",
    "impact_bps",
    "timing_bps",
    "fee_bps",
    "opportunity_bps",
    "vs_vwap_bps",
    "fill_rate",
    "maker_ratio",
    "participation",
    "markout_1",
    "markout_5",
    "markout_15",
    "children",
    "duration",
)


def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in KEYS:
        v = [r[k] for r in rows if r.get(k) is not None]
        out[k] = float(np.mean(v)) if v else None
    isv = np.array([r["is_bps"] for r in rows])
    out["is_std_bps"] = float(isv.std(ddof=1)) if len(isv) > 1 else 0.0
    out["is_p95_bps"] = float(np.quantile(isv, 0.95)) if len(isv) else None
    out["episodes"] = len(rows)
    return out


def simulate(
    tape: Tape,
    cfg: dict[str, Any],
    *,
    starts: list[int] | None = None,
    states: np.ndarray | None = None,
    record_idx: int | None = None,
) -> list[dict[str, Any]]:
    starts = starts or episode_starts(tape, cfg)
    if states is None:
        states, _ = detect_states(tape, cfg["state_model"], fit_upto=starts[0])
    rows = []
    for i, s in enumerate(starts):
        side = {"buy": 1, "sell": -1}.get(cfg["order"]["side"], 1 if i % 2 == 0 else -1)
        r = run_episode(tape, cfg, s, side, order_qty(tape, cfg, s), states, record=(i == record_idx))
        r["start"], r["state"] = s, int(states[s])
        rows.append(r)
    return rows


def evaluate_scheme(cfg: dict[str, Any], tapes: list[Tape], *, stop: Any = None) -> dict[str, Any]:
    """在一组盘口带上跑多个执行片段：算法本身 + 市价与 TWAP 两个基准（同一批片段），汇总 TCA 并给出准入检查。"""
    cfg = normalize_config(cfg)
    all_rows: list[dict[str, Any]] = []
    bench: dict[str, list[dict[str, Any]]] = {"market": [], "twap": []}
    per_symbol = []
    example = None
    state_info: dict[str, Any] = {}
    frontier = []
    for ti, tape in enumerate(tapes):
        if stop:
            stop()
        starts = episode_starts(tape, cfg)
        states, state_info = detect_states(tape, cfg["state_model"], fit_upto=starts[0])
        rows = simulate(tape, cfg, starts=starts, states=states, record_idx=len(starts) // 2 if ti == 0 else None)
        for r in rows:
            r["symbol"] = tape.symbol
        all_rows += rows
        for b in bench:
            bc = normalize_config({**cfg, "algo": b, "style": "neutral"})
            bench[b] += simulate(tape, bc, starts=starts, states=states)
        a = _agg(rows)
        per_symbol.append(
            {
                "symbol": tape.symbol,
                "source": tape.source,
                **{k: a[k] for k in ("is_bps", "is_std_bps", "fill_rate", "maker_ratio", "vs_vwap_bps")},
                **tape.summary(),
            }
        )
        if ti == 0:
            ex = next((r for r in rows if "timeline" in r), None)
            if ex:
                example = {
                    k: ex[k]
                    for k in (
                        "timeline",
                        "alerts",
                        "fills",
                        "is_bps",
                        "spread_bps",
                        "impact_bps",
                        "timing_bps",
                        "fee_bps",
                        "opportunity_bps",
                        "side",
                        "arrival",
                        "avg_price",
                        "vwap",
                        "fill_rate",
                        "maker_ratio",
                        "start",
                    )
                }
                example["symbol"] = tape.symbol
                example["t0"] = int(tape.t[ex["start"]])
            # 成本-风险前沿：几种算法在同一批片段上的平均成本与波动
            for algo, extra, label in (
                ("market", {}, "市价"),
                ("twap", {"style": "neutral"}, "TWAP"),
                ("twap", {"style": "passive"}, "TWAP 被动"),
                ("vwap", {}, "VWAP"),
                ("pov", {}, "POV 10%"),
                ("almgren_chriss", {"urgency": 0.2}, "AC 缓"),
                ("almgren_chriss", {"urgency": 0.8}, "AC 急"),
                ("adaptive", {}, "自适应"),
            ):
                try:
                    fc = normalize_config({**cfg, "algo": algo, **extra})
                except ExecutionError:
                    continue
                fr = (
                    rows
                    if (algo == cfg["algo"] and all(cfg.get(k) == v for k, v in extra.items()))
                    else simulate(tape, fc, starts=starts, states=states)
                )
                ag = _agg(fr)
                frontier.append(
                    {
                        "label": label,
                        "algo": algo,
                        "is_bps": ag["is_bps"],
                        "is_std_bps": ag["is_std_bps"],
                        "duration": ag["duration"],
                    }
                )
    s = _agg(all_rows)
    bm, bt = _agg(bench["market"]), _agg(bench["twap"])
    s["beat_twap_bps"] = bt["is_bps"] - s["is_bps"]
    s["beat_market_bps"] = bm["is_bps"] - s["is_bps"]
    s["twap_is_bps"], s["market_is_bps"] = bt["is_bps"], bm["is_bps"]
    s["algo"], s["style"], s["horizon"] = cfg["algo"], cfg["style"], cfg["horizon"]
    s["score"] = -s["is_bps"] / 4
    half = len(all_rows) // 2
    s["is_h1"] = float(np.mean([r["is_bps"] for r in all_rows[:half]])) if half else None
    s["is_h2"] = float(np.mean([r["is_bps"] for r in all_rows[half:]])) if half else None
    s["notional_avg"] = float(np.mean([r["notional"] for r in all_rows]))
    by_state = []
    for i, name in enumerate(STATES):
        rs = [r for r in all_rows if r["state"] == i]
        if rs:
            ts = [b for b, r in zip(bench["twap"], all_rows, strict=True) if r["state"] == i]
            by_state.append(
                {
                    "state": name,
                    "n": len(rs),
                    "is_bps": float(np.mean([r["is_bps"] for r in rs])),
                    "twap_is_bps": float(np.mean([r["is_bps"] for r in ts])) if ts else None,
                }
            )
    sizes = cost_curve(tapes[0], cfg, stop=stop)
    sources = sorted({t.source for t in tapes})
    warnings = [w for t in tapes for w in t.warnings]
    report = {
        "summary": s,
        "config": cfg,
        "per_symbol": per_symbol,
        "by_state": by_state,
        "frontier": frontier,
        "cost_curve": sizes,
        "example": example,
        "state_model": state_info,
        "episodes": [
            {
                "symbol": r["symbol"],
                "start": int(r["start"]),
                "side": r["side"],
                "state": STATES[r["state"]],
                "is_bps": r["is_bps"],
                "fill_rate": r["fill_rate"],
                "maker_ratio": r["maker_ratio"],
            }
            for r in all_rows
        ],
        "tape": {
            "sources": sources,
            "synthetic": "synthetic" in sources,
            "kline": "kline" in sources,
            "warnings": list(dict.fromkeys(warnings)),
            "symbols": [t.symbol for t in tapes],
        },
    }
    report["checks"] = quality_checks(report)
    report["admitted"] = all(c["ok"] for c in report["checks"] if c["required"])
    return report


def cost_curve(
    tape: Tape, cfg: dict[str, Any], sizes: tuple[float, ...] = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4), stop: Any = None
) -> dict[str, Any]:
    """不同母单规模（占期间成交量的比例）下的平均成本，并拟合平方根冲击模型 cost = a + b·√参与率。"""
    starts = episode_starts(tape, cfg)[:12]
    states, _ = detect_states(tape, cfg["state_model"], fit_upto=starts[0])
    pts = []
    for p in sizes:
        if stop:
            stop()
        c = normalize_config({**cfg, "order": {**cfg["order"], "size_pct": p, "notional": None}})
        rows = simulate(tape, c, starts=starts, states=states)
        pts.append(
            {
                "size_pct": p,
                "is_bps": float(np.mean([r["is_bps"] for r in rows])),
                "ex_fee_bps": float(np.mean([r["is_bps"] - r["fee_bps"] for r in rows])),
                "notional": float(np.mean([r["notional"] for r in rows])),
            }
        )
    x = np.sqrt([q["size_pct"] for q in pts])
    y = np.array([q["ex_fee_bps"] for q in pts])  # 不含手续费：回测里手续费单独计
    X = np.vstack([np.ones_like(x), x]).T
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ coef
    r2 = 1 - ((y - pred) ** 2).sum() / max(((y - y.mean()) ** 2).sum(), 1e-12)
    return {
        "points": pts,
        "a_bps": float(coef[0]),
        "b_bps": float(coef[1]),
        "r2": float(r2),
        "note": "不含手续费的成本 ≈ a + b·√(母单 / 期间成交量)",
    }


def quality_checks(report: dict[str, Any]) -> list[dict[str, Any]]:
    s = report["summary"]
    out = []

    def add(key: str, label: str, ok: bool, detail: str, required: bool = True) -> None:
        out.append({"key": key, "label": label, "ok": bool(ok), "detail": detail, "required": required})

    add("fill", "基本做完", (s["fill_rate"] or 0) >= 0.95, f"平均完成 {s['fill_rate'] or 0:.1%}")
    add(
        "twap", "不比 TWAP 差", s["beat_twap_bps"] >= -0.5, f"成本 {s['is_bps']:.2f} bp，TWAP {s['twap_is_bps']:.2f} bp"
    )
    add("episodes", "片段足够", s["episodes"] >= 10, f"{s['episodes']} 个执行片段")
    stable = s["is_h1"] is None or abs(s["is_h1"] - s["is_h2"]) <= max(3.0, abs(s["is_bps"]))
    add(
        "stable",
        "前后两半一致",
        stable,
        f"前半 {s['is_h1'] or 0:.2f} bp，后半 {s['is_h2'] or 0:.2f} bp",
        required=False,
    )
    adverse = s.get("markout_5")
    add(
        "markout",
        "被动成交没有严重逆向选择",
        adverse is None or adverse > -5 or (s["maker_ratio"] or 0) < 0.2,
        f"成交后 5 步平均 markout {adverse if adverse is not None else 0:.2f} bp",
        required=False,
    )
    t = report["tape"]
    add(
        "real",
        "使用真实盘口",
        not t["synthetic"] and not t["kline"],
        "录制的盘口" if not (t["synthetic"] or t["kline"]) else ("含合成盘口" if t["synthetic"] else "盘口由 K 线推算"),
        required=False,
    )
    return out


# ---------------------------------------------------------------------------
# 成本校准、容量、雷达
# ---------------------------------------------------------------------------
def calibrate(fills: list[dict[str, Any]], refs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """用真实成交校准平方根冲击模型：slippage_bp = a + Y · σ_日(bp) · √(成交额 / 日均成交额)。

    fills: [{ts, symbol, side, qty, price}]；refs[symbol] = {"ref": (ts → 参考中间价的函数), "adv_usd", "sigma_d"}。
    参考价取成交所在分钟的开盘价（没有时退到小时线）。
    """
    xs, ys, rows = [], [], []
    for f in fills:
        r = refs.get(f["symbol"])
        if not r:
            continue
        ref = r["ref"](int(f["ts"]))
        if not ref:
            continue
        side = 1 if str(f["side"]).lower() in ("buy", "b", "买", "买入") else -1
        px, q = float(f["price"]), float(f["qty"])
        slip = side * (px - ref) / ref * 1e4
        part = q * px / max(r["adv_usd"], 1e-9)
        x = r["sigma_d"] * 1e4 * math.sqrt(part)
        xs.append(x)
        ys.append(slip)
        rows.append(
            {
                "ts": int(f["ts"]),
                "symbol": f["symbol"],
                "side": side,
                "notional": q * px,
                "slip_bps": round(slip, 3),
                "x": round(x, 4),
                "part": part,
            }
        )
    n = len(xs)
    if n < 5:
        raise ExecutionError(f"可用于校准的成交只有 {n} 笔（至少 5 笔，且需要对应时段的行情）")
    x, y = np.array(xs), np.array(ys)
    # 稳健一点：截掉两端 2% 的离群值
    lo, hi = np.quantile(y, [0.02, 0.98])
    keep = (y >= lo) & (y <= hi)
    X = np.vstack([np.ones(keep.sum()), x[keep]]).T
    coef, *_ = np.linalg.lstsq(X, y[keep], rcond=None)
    pred = X @ coef
    resid = y[keep] - pred
    r2 = 1 - (resid**2).sum() / max(((y[keep] - y[keep].mean()) ** 2).sum(), 1e-12)
    se = math.sqrt((resid**2).sum() / max(keep.sum() - 2, 1) / max(((x[keep] - x[keep].mean()) ** 2).sum(), 1e-12))
    return {
        "a_bps": float(coef[0]),
        "Y": float(coef[1]),
        "Y_se": float(se),
        "r2": float(r2),
        "n": int(n),
        "used": int(keep.sum()),
        "mean_slip_bps": float(y.mean()),
        "points": rows[:2000],
        "note": "slippage ≈ a + Y·σ·√(成交额/日均成交额)；文献里 Y 通常在 0.5~1 之间",
    }


def model_cost_bps(model: dict[str, Any], notional: float, adv_usd: float, sigma_d: float) -> float:
    return float(model.get("a_bps") or 0) + float(model.get("Y") or 0.7) * sigma_d * 1e4 * math.sqrt(
        max(notional, 0) / max(adv_usd, 1e-9)
    )


def capacity(
    model: dict[str, Any],
    assets: list[dict[str, Any]],
    *,
    turnover: float,
    alpha: float,
    rebalances: float,
    fee_bps: float = 5.0,
) -> dict[str, Any]:
    """容量估计。turnover = 年换手（单边，倍）；alpha = 扣成本前年化收益；rebalances = 每年调仓次数。
    assets: [{symbol, weight, adv_usd, sigma_d}]。每次调仓的成交额 = AUM × turnover / rebalances × weight。
    """
    if alpha <= 0:
        raise ExecutionError("扣成本前收益必须为正，才谈得上容量")
    grid = np.logspace(4, 10, 61)
    curve = []
    for aum in grid:
        per = aum * turnover / max(rebalances, 1e-9)
        cost_bps = sum(
            a["weight"] * (model_cost_bps(model, per * a["weight"], a["adv_usd"], a["sigma_d"]) + fee_bps)
            for a in assets
        ) / max(sum(a["weight"] for a in assets), 1e-9)
        annual_cost = cost_bps / 1e4 * turnover
        curve.append(
            {
                "aum": float(aum),
                "cost_bps": float(cost_bps),
                "annual_cost": float(annual_cost),
                "net": float(alpha - annual_cost),
            }
        )

    def cross(level: float) -> float | None:
        for a, b in zip(curve, curve[1:], strict=False):
            if a["annual_cost"] <= level < b["annual_cost"]:
                w = (level - a["annual_cost"]) / max(b["annual_cost"] - a["annual_cost"], 1e-12)
                return float(math.exp(math.log(a["aum"]) + w * (math.log(b["aum"]) - math.log(a["aum"]))))
        return None if curve[0]["annual_cost"] < level else float(curve[0]["aum"])

    return {
        "curve": curve,
        "half_alpha_aum": cross(alpha / 2),
        "zero_alpha_aum": cross(alpha),
        "fixed_cost": curve[0]["annual_cost"],
        "alpha": alpha,
        "turnover": turnover,
        "rebalances": rebalances,
    }


def radar_row(symbol: str, bars: list[Any], tf_ms: int) -> dict[str, Any]:
    """一个标的的雷达读数：价格变化、波动比、量比、估计价差、市场状态与提示。"""
    if len(bars) < 60:
        return {"symbol": symbol, "error": "数据不足"}
    tape = tape_from_bars(symbol, bars, step_ms=tf_ms)
    f = state_features(tape)
    st = rule_states(f)
    c = np.array([float(b.close) for b in bars])
    per_h = max(1, int(3_600_000 // tf_ms))
    per_d = max(1, int(86_400_000 // tf_ms))
    r = np.diff(np.log(c))
    sig = float(r[-240:].std()) if len(r) > 10 else 0.0
    ret_h = float(c[-1] / c[-1 - min(per_h, len(c) - 1)] - 1)
    ret_d = float(c[-1] / c[-1 - min(per_d, len(c) - 1)] - 1)
    v = np.array([float(b.volume) for b in bars])
    vz = float((v[-1] - v[-240:].mean()) / max(v[-240:].std(), 1e-12))
    alerts = []
    if f["vol_ratio"][-1] > 2:
        alerts.append({"level": "warn", "text": f"短期波动是常态的 {f['vol_ratio'][-1]:.1f} 倍"})
    if vz > 3:
        alerts.append({"level": "warn", "text": f"成交量异常放大（{vz:.1f}σ）"})
    if sig > 0 and abs(ret_h) > 4 * sig * math.sqrt(per_h):
        alerts.append({"level": "alert", "text": f"一小时内{'急涨' if ret_h > 0 else '急跌'} {ret_h:.1%}"})
    if f["spread_ratio"][-1] > 2:
        alerts.append({"level": "warn", "text": "估计价差明显变宽，流动性变差"})
    recent = st[-6:]
    if len(set(recent.tolist())) > 1 and recent[-1] != recent[-2]:
        alerts.append({"level": "info", "text": f"状态切换：{STATES[int(recent[-2])]} → {STATES[int(recent[-1])]}"})
    return {
        "symbol": symbol,
        "price": float(c[-1]),
        "ret_1h": ret_h,
        "ret_24h": ret_d,
        "vol_ratio": float(f["vol_ratio"][-1]),
        "volume_z": vz,
        "spread_bps": float(f["spread_bps"][-1]),
        "trend_z": float(f["trend_z"][-1]),
        "state": STATES[int(st[-1])],
        "alerts": alerts,
        "spark": [float(x) for x in c[-120:][::2]],
        "t": int(bars[-1].open_time),
    }


__all__ = [
    "ALGOS",
    "DEFAULT_CONFIG",
    "STATES",
    "STYLES",
    "ExecutionError",
    "Tape",
    "calibrate",
    "capacity",
    "cost_curve",
    "detect_states",
    "evaluate_scheme",
    "hmm_filter",
    "hmm_fit",
    "model_cost_bps",
    "normalize_config",
    "radar_row",
    "run_episode",
    "simulate",
    "synthetic_tape",
    "tape_from_bars",
    "tape_from_recording",
    "tca",
]
