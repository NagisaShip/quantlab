"""持仓管理：多周期数据、移动止损、分段止盈（ROI）、动态止损、加减仓、入场确认、交易所端保护止损。

这些功能对标 Freqtrade 的策略回调，但沿用本平台的口径：
- **决策只用已收盘数据**。每根 bar 收盘后更新最高/最低价、调用策略回调，算出的新止损价从**下一根 bar**
  开始生效；同一根 bar 里不会"先看到最高价、再按最高价设止损、再被这根的最低价打掉"。
- 止损只会收紧，不会放宽（移动止损与 custom_stoploss 都按棘轮处理）。
- 回测、纸面模拟、交易所模拟盘共用这一份逻辑；模拟盘额外把止损单挂到交易所，平台停了止损也还在。

策略里可以定义的回调（都是可选的，不写就不调用）：

    def custom_stoploss(ctx, trade):   # 返回止损距离（相对当前收盘价的比例，如 0.03）或 None
    def custom_exit(ctx, trade):       # 返回平仓原因字符串（或 True）表示平掉；None / False 不动
    def adjust_position(ctx, trade):   # 返回要加仓（正）或减仓（负）的金额（计价币），None 不动
    def confirm_entry(ctx, security, side, amount, price, tag):  # 返回 False 否决这笔开仓

`trade` 是只读的 `Trade`：标的、方向、数量、开仓均价、开仓时间、持有根数、当前收益率、
期间最大/最小收益率、入场标签、开仓次数、当前止损价。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .ledger import ZERO, dec, is_perp

UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 7 * 86_400_000}
STOP_REASONS = ("stop_loss", "trailing_stop", "custom_stop", "exchange_stop", "liquidate", "liquidation")
EXIT_REASONS = (*STOP_REASONS, "take_profit", "roi", "custom_exit")
TIF = ("GTC", "IOC", "post_only")
ON_EXPIRE = ("cancel", "market", "chase")
MAX_CHASES = 3
PROTECT_MOVE = Decimal("0.001")  # 止损价变化不到 0.1% 不去交易所改单，省限频


def unit_ms(unit: str) -> int:
    """'4h' → 毫秒。支持 m / h / d / w。"""
    u = (unit or "").strip().lower()
    if len(u) < 2 or u[-1] not in UNIT_MS or not u[:-1].isdigit() or int(u[:-1]) <= 0:
        raise ValueError(f"周期写法不对：{unit!r}（例：15m、4h、1d）")
    return int(u[:-1]) * UNIT_MS[u[-1]]


# --------------------------------------------------------------------------- 多周期
def resample(rt: Any, security: str, count: int, unit: str) -> list[dict[str, Any]]:
    """把策略自身周期的 K 线合成更大周期，只返回**已经完整收盘**的大周期 bar。

    例：1h 策略读 4h，在 13:00 收盘时只看得到 08:00–12:00 那根，12:00–16:00 还没走完就不给。
    大周期按 UTC 整点对齐（与交易所一致）；基础 bar 有缺口的那根大周期 bar 照样合成，volume 只算到手的部分。
    """
    big = unit_ms(unit)
    base = rt.bar_ms(security)
    if big % base:
        raise ValueError(f"{unit} 不是 {security} 当前周期的整数倍，无法合成")
    if big == base:
        return rt.bars(security, count)
    idx = rt._index.get(security, -1)
    if idx < 0:
        return []
    bars = rt.bars_by_security[security]
    cache = rt._rs.setdefault((security, big), {"upto": -1, "rows": [], "cur": None})
    start = cache["upto"] + 1
    if start > idx:  # 时间不会倒退；重放同一时刻直接用缓存
        pass
    for i in range(start, idx + 1):
        b = bars[i]
        g = b.open_time - b.open_time % big
        cur = cache["cur"]
        if cur is None or cur["t"] != g:
            cur = {
                "t": g,
                "close_time": g + big - 1,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            cache["cur"] = cur
        else:
            cur["high"] = max(cur["high"], b.high)
            cur["low"] = min(cur["low"], b.low)
            cur["close"] = b.close
            cur["volume"] += b.volume
        if b.close_time + 1 >= g + big:
            cache["rows"].append(dict(cur))
            cache["cur"] = None
            if len(cache["rows"]) > 5_000:
                del cache["rows"][:1_000]
    cache["upto"] = max(cache["upto"], idx)
    return cache["rows"][-int(count) :] if count > 0 else []


# --------------------------------------------------------------------------- 持仓视图
@dataclass(frozen=True)
class Trade:
    """策略回调拿到的持仓快照（只读）。收益率按开仓均价计，空头方向已换算（价格下跌为正）。"""

    security: str
    side: str  # long | short
    amount: float  # 数量（空头为负）
    entry_price: float
    open_ts: int
    bars_held: int
    current_price: float
    profit_ratio: float
    max_profit_ratio: float
    min_profit_ratio: float
    entry_tag: str
    entries: int  # 开仓 / 加仓成交次数
    stop_price: float  # 当前生效的止损价（0 = 没有）

    @property
    def is_short(self) -> bool:
        return self.side == "short"


def positions(rt: Any) -> list[tuple[str, Decimal, Decimal]]:
    """(标的, 带符号数量, 开仓均价)。"""
    out = [(k, h.qty, h.avg_entry) for k, h in rt.ledger.holdings.items() if h.qty > 0]
    out += [(k, p.qty, p.entry) for k, p in rt.ledger.perps.items() if p.qty]
    return out


def _ensure(rt: Any, sec: str, qty: Decimal, avg: Decimal) -> dict[str, Any]:
    st = rt.trade_state.get(sec)
    short = qty < 0
    if st is None or st.get("short") != short:
        trip = rt._open_trips.get(sec)
        st = {
            "short": short,
            "entry_ts": trip.open_ts if trip else rt.now,
            "tag": trip.entry_tag if trip else "",
            "hwm": str(avg),
            "lwm": str(avg),
            "stop": "0",
            "stop_kind": "",
            "bars": 0,
        }
        rt.trade_state[sec] = st
    return st


def _trail(rt: Any, sec: str, st: dict[str, Any], avg: Decimal) -> None:
    trail = (rt.brackets.get(sec) or {}).get("trail") or (rt.brackets.get("*") or {}).get("trail")
    if not trail:
        return
    act = dec(trail.get("activate") or 0)
    pct = dec(trail["pct"])
    if st["short"]:
        best = dec(st["lwm"])
        if act <= 0 or best <= avg * (1 - act):
            tighten(rt, sec, best * (1 + pct), "trailing_stop")
    else:
        best = dec(st["hwm"])
        if act <= 0 or best >= avg * (1 + act):
            tighten(rt, sec, best * (1 - pct), "trailing_stop")


def sync(rt: Any) -> None:
    """每根 bar 收盘后：建立 / 清理持仓状态，用这根 bar 的高低点更新最高 / 最低价，推进移动止损。"""
    live = {}
    for sec, qty, avg in positions(rt):
        live[sec] = (qty, avg)
        st = _ensure(rt, sec, qty, avg)
        bar = rt.current_bar(sec)
        if bar is None:
            continue
        st["bars"] = int(st.get("bars", 0)) + 1
        st["hwm"] = str(max(dec(st["hwm"]), dec(bar.high)))
        st["lwm"] = str(min(dec(st["lwm"]), dec(bar.low)))
        _trail(rt, sec, st, avg)
    for sec in [s for s in rt.trade_state if s not in live]:
        rt.trade_state.pop(sec, None)


def tighten(rt: Any, security: str, price: Decimal, kind: str) -> bool:
    """收紧止损价（多头只上移、空头只下移）。返回是否改动。"""
    st = rt.trade_state.get(security)
    if st is None or price <= 0:
        return False
    old = dec(st.get("stop") or 0)
    better = (old == 0) or (price < old if st["short"] else price > old)
    if better:
        st["stop"] = str(rt._round_price(price))
        st["stop_kind"] = kind
    return better


def view(rt: Any, security: str) -> Trade | None:
    st = rt.trade_state.get(security)
    pos = next(((q, a) for s, q, a in positions(rt) if s == security), None)
    if st is None or pos is None:
        return None
    qty, avg = pos
    px = dec(rt.price(security) or avg)
    sign = Decimal(-1) if qty < 0 else Decimal(1)

    def ratio(p: Decimal) -> float:
        return float((p / avg - 1) * sign) if avg else 0.0

    hi, lo = dec(st["hwm"]), dec(st["lwm"])
    best, worst = (lo, hi) if qty < 0 else (hi, lo)
    trip = rt._open_trips.get(security)
    return Trade(
        security=security,
        side="short" if qty < 0 else "long",
        amount=float(qty),
        entry_price=float(avg),
        open_ts=int(st["entry_ts"]),
        bars_held=int(st.get("bars", 0)),
        current_price=float(px),
        profit_ratio=ratio(px),
        max_profit_ratio=ratio(best),
        min_profit_ratio=ratio(worst),
        entry_tag=st.get("tag") or "",
        entries=trip.entries if trip else 1,
        stop_price=float(dec(st.get("stop") or 0)),
    )


# --------------------------------------------------------------------------- 止损 / 止盈价
def roi_threshold(rt: Any, security: str, held_ms: int) -> float | None:
    """分段止盈表：{持有分钟: 目标收益}，取持有时间已达到的最大一档。"""
    table = rt.roi.get(security) or rt.roi.get("*")
    if not table:
        return None
    mins = held_ms / 60_000
    hit = [r for m, r in table if mins >= m]
    return hit[-1] if hit else None


def exit_levels(rt: Any, security: str, qty: Decimal, avg: Decimal) -> tuple[list, list]:
    """该持仓当前生效的止损候选 [(价, 原因)] 与止盈候选 [(价, 原因)]。"""
    short = qty < 0
    b = rt.brackets.get(security, {})
    sl = b.get("sl", rt.risk.stop_loss)
    tp = b.get("tp", rt.risk.take_profit)
    stops, takes = [], []
    if sl:
        stops.append((avg * (1 + dec(sl)) if short else avg * (1 - dec(sl)), "stop_loss"))
    st = rt.trade_state.get(security)
    if st and dec(st.get("stop") or 0) > 0:
        stops.append((dec(st["stop"]), st.get("stop_kind") or "custom_stop"))
    if tp:
        takes.append((avg * (1 - dec(tp)) if short else avg * (1 + dec(tp)), "take_profit"))
    if st:
        roi = roi_threshold(rt, security, max(0, rt.now - int(st["entry_ts"])))
        if roi is not None:
            takes.append((avg * (1 - dec(roi)) if short else avg * (1 + dec(roi)), "roi"))
    # 最近的那个先触发：多头止损取最高、止盈取最低；空头相反
    stops.sort(key=lambda x: x[0], reverse=not short)
    takes.sort(key=lambda x: x[0], reverse=short)
    return stops[:1], takes[:1]


def effective_stop(rt: Any, security: str) -> tuple[Decimal, str] | None:
    pos = next(((q, a) for s, q, a in positions(rt) if s == security), None)
    if pos is None:
        return None
    stops, _ = exit_levels(rt, security, *pos)
    return stops[0] if stops else None


# --------------------------------------------------------------------------- 策略回调
CALLBACKS = ("custom_stoploss", "custom_exit", "adjust_position")


def run_callbacks(runner: Any) -> None:
    """每根 bar 策略主逻辑之后调用：先更新状态，再按 custom_exit → adjust_position → custom_stoploss 的顺序问策略。"""
    rt = runner.runtime
    if not hasattr(rt, "trade_state"):
        return
    sync(rt)
    has = [n for n in CALLBACKS if runner.strategy.has(n)]
    if not has:
        return
    from . import api

    for sec in list(rt.trade_state):
        t = view(rt, sec)
        if t is None:
            continue
        if "custom_exit" in has:
            why = runner.call(runner.strategy.fn("custom_exit"), t)
            if why:
                api.order_target(sec, 0, reason=why if isinstance(why, str) else "custom_exit")
                continue
        if "adjust_position" in has:
            amt = runner.call(runner.strategy.fn("adjust_position"), t)
            if amt:
                v = float(amt)
                api.order_value(sec, -v if t.is_short else v, reason="adjust_add" if v > 0 else "adjust_reduce")
        if "custom_stoploss" in has:
            d = runner.call(runner.strategy.fn("custom_stoploss"), t)
            if d is not None and 0 < float(d) < 1:
                px = dec(t.current_price)
                tighten(rt, sec, px * (1 + dec(d)) if t.is_short else px * (1 - dec(d)), "custom_stop")


def is_entry(rt: Any, req: Any, bar: Any) -> bool:
    """这笔委托是否扩大了敞口（开仓、加仓、反手）。用决策时的收盘价估算目标数量。"""
    held = rt._projected_amount(req.security)
    target = rt._target_amount(req, held=held, price=dec(bar.close), equity=rt.ledger.equity())
    if held == 0:
        return target != 0
    if (held > 0) != (target > 0) and target != 0:
        return True
    return abs(target) > abs(held)


# --------------------------------------------------------------------------- 交易所端保护止损
def protect_id(rt: Any, security: str, n: int) -> str:
    h = hashlib.sha256(f"{rt.run_id}|protect|{security}|{n}".encode()).hexdigest()[:20]
    return f"qp{h}"


def sync_protects(rt: Any) -> None:
    """交易所模拟盘：让交易所上挂着的保护止损与平台当前的止损价、持仓数量一致。

    放进 outbox 的是 {"kind": "protect", "action": "place" | "cancel", ...}，OMS 先撤后挂。
    挂单只当"平台停了也还在的保险"，平台自己的止损逻辑照常运行；平台先触发时会先撤掉这张单再平仓。
    """
    if rt.fill_mode != "external":
        return
    want: dict[str, dict[str, Any]] = {}
    for sec, qty, avg in positions(rt):
        if sec not in rt.trade_state:  # 刚成交回来的新仓：按开仓价先挂上初始止损，不等下一根 bar
            _trail(rt, sec, _ensure(rt, sec, qty, avg), avg)
        if rt._ext_qty.get(sec):  # 有在途委托，数量还没定，下一轮再说
            want[sec] = rt.protects.get(sec) or {}
            continue
        stop = effective_stop(rt, sec)
        if stop is None:
            continue
        px, kind = stop
        want[sec] = {
            "security": sec,
            "side": "buy" if qty < 0 else "sell",
            "qty": dstr_abs(qty),
            "trigger": str(rt._round_price(px)),
            "stop_kind": kind,
            "perp": is_perp(sec),
        }
    for sec, cur in list(rt.protects.items()):
        w = want.get(sec)
        if w is cur:
            continue
        if not w or w.get("qty") != cur.get("qty") or _moved(cur.get("trigger"), w.get("trigger")):
            rt.outbox.insert(0, {**cur, "kind": "protect", "action": "cancel"})
            rt.protects.pop(sec, None)
    for sec, w in want.items():
        if not w or sec in rt.protects:
            continue
        rt.protect_seq += 1
        rec = {**w, "client_order_id": protect_id(rt, sec, rt.protect_seq), "placed_at": rt.now}
        rt.protects[sec] = rec
        rt.outbox.append({**rec, "kind": "protect", "action": "place"})


def cancel_protect(rt: Any, security: str) -> None:
    """平台自己要平这个仓：先撤掉交易所上的保护止损（现货止损单会锁住币，不撤卖不出去）。"""
    cur = rt.protects.pop(security, None)
    if cur:
        rt.outbox.insert(0, {**cur, "kind": "protect", "action": "cancel"})


def _moved(a: Any, b: Any) -> bool:
    a, b = dec(a or 0), dec(b or 0)
    return a <= 0 or abs(b / a - 1) > PROTECT_MOVE


def dstr_abs(q: Decimal) -> str:
    return format(abs(q).normalize(), "f") if q else "0"


# --------------------------------------------------------------------------- 交易分解
def breakdown(round_trips: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """已完成交易按入场标签、出场原因、标的、星期、小时（UTC，按开仓时间）分组统计。"""
    import time as _t

    keys = {
        "entry_tag": lambda t: t.entry_tag or "（未标注）",
        "exit_reason": lambda t: t.exit_reason or "（未标注）",
        "security": lambda t: t.security,
        "weekday": lambda t: "一二三四五六日"[_t.gmtime(t.open_ts / 1000).tm_wday],
        "hour": lambda t: f"{_t.gmtime(t.open_ts / 1000).tm_hour:02d}",
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for name, fn in keys.items():
        groups: dict[str, list[Any]] = {}
        for t in round_trips:
            groups.setdefault(fn(t), []).append(t)
        rows = []
        for k, ts in groups.items():
            rets = [t.ret for t in ts]
            pnl = sum((t.realized for t in ts), ZERO)
            held = [t.close_ts - t.open_ts for t in ts if t.close_ts]
            rows.append(
                {
                    "key": k,
                    "trades": len(ts),
                    "win_rate": round(sum(r > 0 for r in rets) / len(rets), 4),
                    "avg_return": round(sum(rets) / len(rets), 6),
                    "pnl": float(pnl),
                    "avg_holding_hours": round(sum(held) / len(held) / 3_600_000, 2) if held else 0.0,
                }
            )
        rows.sort(key=lambda r: r["key"] if name in ("weekday", "hour") else -r["trades"])
        if name == "weekday":
            rows.sort(key=lambda r: "一二三四五六日".index(r["key"]))
        out[name] = rows
    return out
