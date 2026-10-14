"""真实成交归因与逐笔对照。

匹配顺序（plan v2.0 第 135 行）：
1. client_order_id 或用户给出的显式映射（真实订单号 → 模拟 signal_id / fill_id）——置信度 1.0；
2. 交易所订单号 / 成交号与模拟侧记录一致（例如测试网成交）——置信度 1.0；
3. 缺标签时按 品种、方向、时间、数量 启发式匹配，输出 0–1 置信度，低于阈值不匹配。

无法归因的真实成交（例如手工交易）进入 `unattributed`，**不计入策略收益**；
策略的真实结果只由已归因成交在独立的 Decimal 账本上重算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..sdk.ledger import Ledger, LedgerError, dec, dstr

DEFAULT_WINDOW_MS = 2 * 3_600_000
MIN_CONFIDENCE = 0.5


def _public(d: dict[str, Any] | None) -> dict[str, Any] | None:
    """对外输出前去掉内部字段（`_keys` / `_order_key` 等只在匹配期使用）。"""
    if d is None:
        return None
    return {k: v for k, v in d.items() if not k.startswith("_")}


@dataclass
class Pair:
    live: dict[str, Any]
    sim: dict[str, Any] | None
    method: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "live": _public(self.live),
            "sim": _public(self.sim),
            "method": self.method,
            "confidence": round(self.confidence, 4),
        }
        if self.sim:
            lp, sp = dec(self.live["price"]), dec(self.sim["price"])
            lq, sq = dec(self.live["qty"]), dec(self.sim.get("amount") or self.sim.get("qty"))
            sign = 1 if self.live["side"] == "buy" else -1
            out.update(
                {
                    # 正数 = 真实成交比模拟更差（买得更贵 / 卖得更便宜）。
                    # 两侧价格都已是"按数量加权的订单均价"，所以这里的滑点是订单级滑点，
                    # 不会被同一订单里的小额部分成交带偏。
                    "slippage_bps": float((lp / sp - 1) * 10_000 * sign) if sp else None,
                    "qty_diff": dstr(lq - sq),
                    "fee_diff": dstr(
                        dec(self.live.get("fee") or 0) - dec(self.sim.get("fee_value") or self.sim.get("fee") or 0)
                    ),
                    "delay_ms": int(self.live["ts"]) - int(self.sim["ts"]),
                    # 一个订单在交易所被拆成几笔成交：>1 表示这一行是归并后的结果
                    "live_fills": int(self.live.get("fills") or 1),
                    "sim_fills": int(self.sim.get("fills") or 1),
                }
            )
        return out


@dataclass
class AttributionReport:
    pairs: list[Pair] = field(default_factory=list)
    unattributed: list[dict[str, Any]] = field(default_factory=list)
    unmatched_sim: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # 手续费折算汇率（{币种: 1 单位该币种 = 多少计价币}），由调用方显式给出；
    # 只有这里给了汇率，第三币种手续费才会进 attributed_result 的 fees，否则单列未折算。
    fee_rates: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        slips = [p.to_dict()["slippage_bps"] for p in self.pairs if p.sim]
        slips = [s for s in slips if s is not None]
        by_method: dict[str, int] = {}
        for p in self.pairs:
            by_method[p.method] = by_method.get(p.method, 0) + 1
        # live_fills 统计的是**成交笔数**（真实成交可能多于归因行数）；
        # orders 才是报告里的行数，两者分开才不会让"拆单"看起来像丢了成交。
        matched_fills = sum(int(p.live.get("fills") or 1) for p in self.pairs)
        unattr_fills = sum(int(f.get("fills") or 1) for f in self.unattributed)
        merged_orders = sum(1 for p in self.pairs if int(p.live.get("fills") or 1) > 1)
        return {
            "live_fills": matched_fills + unattr_fills,
            "orders": len(self.pairs) + len(self.unattributed),
            "attributed": len(self.pairs),
            "unattributed": len(self.unattributed),
            "unmatched_sim": len(self.unmatched_sim),
            "merged_orders": merged_orders,
            "by_method": by_method,
            "avg_confidence": round(sum(p.confidence for p in self.pairs) / len(self.pairs), 4) if self.pairs else None,
            "avg_slippage_bps": round(sum(slips) / len(slips), 3) if slips else None,
            "attributed_result": live_result([p.live for p in self.pairs], fee_rates=self.fee_rates),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "pairs": [p.to_dict() for p in self.pairs],
            "unattributed": [_public(f) for f in self.unattributed],
            "unmatched_sim": [_public(s) for s in self.unmatched_sim],
            "notes": self.notes,
        }


def live_result(fills: list[dict[str, Any]], *, fee_rates: dict[str, Any] | None = None) -> dict[str, Any]:
    """只用给定的真实成交在独立账本上重算已实现盈亏与手续费（不含未归因成交）。

    `fee_rates`：`{币种: 汇率}`，含义是"1 单位该币种 = 多少计价币"，由调用方显式给出
    （例如从交易所账单或当时的行情里取）。**账本不自己取价、不按 1:1 估算**：
    拿不到汇率的第三币种手续费（币安常见的 BNB 抵扣）记为"未折算"，
    在 `unconverted_fees` 里单列，既不进 `fees`，也不进已实现盈亏。
    """
    led = Ledger()
    led.deposit(Decimal(10) ** 12, ref="virtual")  # 归因账本只关心盈亏，不受资金规模影响
    realized = Decimal(0)
    skipped = 0
    other_fee = 0
    rates = {k.upper(): v for k, v in (fee_rates or {}).items()}
    for f in sorted(fills, key=lambda x: int(x["ts"])):
        base, quote = [*f["instrument"].split("/"), "USDT"][:2]
        fee_ccy = (f.get("fee_currency") or quote).upper()
        fee = f.get("fee") or 0
        # 卖出的手续费不能用标的币扣（现货卖出手里没有新增标的币）
        if fee_ccy == base and f["side"] == "sell":
            other_fee += 1
            fee, fee_ccy = 0, quote
        # 成交自带汇率时优先用它（更贴近成交时刻），否则回落到调用方给的币种级汇率
        rate = f.get("fee_rate") if f.get("fee_rate") is not None else rates.get(fee_ccy)
        source = str(f.get("fee_rate_source") or ("caller:fee_rates" if rate is not None else ""))
        try:
            b = led.trade(
                instrument=f["instrument"],
                side=f["side"],
                qty=f["qty"],
                price=f["price"],
                fee=fee,
                fee_currency=fee_ccy,
                fee_rate=rate,
                fee_rate_source=source,
                ts=int(f["ts"]),
                ref=str(f.get("fill_id") or f.get("order_id") or ""),
            )
            realized += b.realized
        except LedgerError:
            skipped += 1
    unconverted = led.unconverted_fees_report()
    note = "只统计已归因的真实成交；未平仓部分不计入已实现盈亏"
    if skipped:
        note += "；部分成交（卖出多于已归因买入）无法记账"
    if other_fee:
        note += f"；{other_fee} 笔以标的币扣除的卖出手续费无法在此折算，未计入"
    if unconverted["has_unconverted"]:
        note += "；" + unconverted["note"]
    return {
        "realized_pnl": dstr(realized),
        "fees": dstr(led.fees_value),
        "fees_by_currency": {k: dstr(v) for k, v in led.fees_by_currency.items()},
        "unconverted_fees": unconverted["by_currency"],
        "unconverted_fee_details": unconverted["details"],
        "has_unconverted_fees": unconverted["has_unconverted"],
        "open_positions": {k: dstr(h.qty) for k, h in led.holdings.items() if h.qty > 0},
        "skipped": skipped,
        "fees_in_other_currency": other_fee,
        "note": note,
    }


def _score(live: dict[str, Any], sim: dict[str, Any], window_ms: int) -> float:
    if live["instrument"] != sim.get("security", sim.get("instrument")) or live["side"] != sim["side"]:
        return 0.0
    # 账户维度：两侧都标了账户且不一致时，绝不匹配（同一策略在多个账户上跑时，
    # 少了这一维就会把 A 账户的成交算到 B 账户的模拟结果上）
    la, sa = live.get("account_id"), sim.get("account_id")
    if la and sa and str(la) != str(sa):
        return 0.0
    dt = abs(int(live["ts"]) - int(sim["ts"]))
    if dt > window_ms:
        return 0.0
    lq, sq = dec(live["qty"]), dec(sim.get("amount") or sim.get("qty"))
    if lq <= 0 or sq <= 0:
        return 0.0
    q_ratio = float(min(lq, sq) / max(lq, sq))
    if q_ratio < 0.5:
        return 0.0
    lp, sp = dec(live["price"]), dec(sim["price"])
    p_dev = float(abs(lp / sp - 1)) if sp else 1.0
    time_score = 1 - dt / window_ms
    price_score = max(0.0, 1 - p_dev / 0.02)  # 2% 以上价差视为不相关
    score = 0.5 * time_score + 0.3 * q_ratio + 0.2 * price_score
    if la and sa:
        score = min(1.0, score + 0.05)  # 账户一致时给一点加成，但不改变阈值语义
    return score


def _sim_keys(s: dict[str, Any]) -> list[str]:
    """模拟侧可用于匹配的键。"""
    return [str(k) for k in (s.get("fill_id"), s.get("signal_id"), s.get("client_order_id"), s.get("order_id")) if k]


def _live_keys(f: dict[str, Any]) -> list[str]:
    """真实侧可用于匹配的键。

    `fill_id` 是平台内部键（形如 `x:<交易所成交号>`），交易所原始成交号另存在
    `exchange_fill_id`。用户拿交易所导出文件里的成交号做映射时，两种写法都要能命中，
    否则会静默落到启发式匹配——那是最难排查的一类错配。
    """
    keys: list[str] = []
    for k in (f.get("client_order_id"), f.get("order_id"), f.get("fill_id"), f.get("exchange_fill_id")):
        if k:
            keys.append(str(k))
    fid = f.get("fill_id")
    if isinstance(fid, str) and fid.startswith("x:"):
        keys.append(fid[2:])  # 去掉内部前缀后的原始成交号
    return keys


# ---------------------------------------------------------------------------
# 订单级归并
#
# 交易所会把**一个策略订单拆成多笔部分成交**。若按逐笔匹配，第二笔之后的成交
# 找不到还没被占用的模拟成交，会掉进 unattributed —— 报告看起来像"策略少成交了"，
# 而且滑点被单笔小额成交主导。所以先按订单标识把两侧各自归并，再以订单为单位匹配，
# 价格一律取**数量加权均价**。
# ---------------------------------------------------------------------------
def _qty_of(f: dict[str, Any]) -> Decimal:
    """成交数量。两侧字段名不同：真实侧是 qty，模拟侧从库里读出来是 amount。"""
    v = f.get("qty")
    if v is None:
        v = f.get("amount")
    return dec(v or 0)


def _merge(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """同一订单的多笔成交 → 一行：数量求和、价格按数量加权、手续费求和、时间取最早。"""
    base = {k: v for k, v in fills[0].items() if not k.startswith("_")}
    total_qty = sum((_qty_of(f) for f in fills), Decimal(0))
    notional = sum((_qty_of(f) * dec(f["price"]) for f in fills), Decimal(0))
    avg = (notional / total_qty) if total_qty else Decimal(0)
    base["qty"] = dstr(total_qty)
    base["amount"] = dstr(total_qty)
    base["price"] = dstr(avg)
    total_fee = dstr(sum((dec(f.get("fee") or f.get("fee_value") or 0) for f in fills), Decimal(0)))
    # 两侧的手续费字段名不同（模拟侧叫 fee_value）；两个都写成合计，
    # 否则归并后 fee_value 会停留在第一笔上，被下游优先取用而少算手续费。
    base["fee"] = total_fee
    if any("fee_value" in f for f in fills):
        base["fee_value"] = total_fee
    base["ts"] = min(int(f["ts"]) for f in fills)
    base["fills"] = len(fills)
    return base


def _order_key(f: dict[str, Any]) -> str | None:
    """成交所属订单的标识。client_order_id 优先——它是策略自己生成的幂等键。"""
    for k in ("client_order_id", "order_id"):
        v = f.get(k)
        if v:
            return f"{k}:{v}"
    return None


def _group(fills: list[dict[str, Any]], *, side: str) -> list[dict[str, Any]]:
    """按订单标识归并；没有订单标识的成交只能自成一组。

    宁可多出一行，也不把两笔无关成交并成一行——错误的合并会伪造出一笔并不存在的
    "大额订单"，比多一行难排查得多。
    """
    keys_of = _live_keys if side == "live" else _sim_keys
    order: list[str] = []
    buckets: dict[str, list[dict[str, Any]]] = {}
    for i, f in enumerate(fills):
        key = _order_key(f) or f"~{i}"
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(f)
    out: list[dict[str, Any]] = []
    for key in order:
        members = buckets[key]
        merged = _merge(members)
        seen: list[str] = []
        for m in members:
            for k in keys_of(m):
                if k not in seen:
                    seen.append(k)
        merged["_keys"] = seen
        merged["_order_key"] = key
        out.append(merged)
    return out


def attribute(
    simulated: list[dict[str, Any]],
    live: list[dict[str, Any]],
    *,
    mapping: dict[str, str] | None = None,
    window_ms: int = DEFAULT_WINDOW_MS,
    min_confidence: float = MIN_CONFIDENCE,
    fee_rates: dict[str, Any] | None = None,
) -> AttributionReport:
    rep = AttributionReport(fee_rates=dict(fee_rates or {}))
    # 两侧都先按订单归并，再以"订单"为单位做一对一匹配
    sims = _group(simulated, side="sim")
    lives = _group(live, side="live")
    used: set[int] = set()
    by_key: dict[str, int] = {}
    for i, s in enumerate(sims):
        for k in s["_keys"]:
            by_key.setdefault(k, i)
    # 映射表允许指向模拟侧的任一键
    pending: list[dict[str, Any]] = []
    for f in sorted(lives, key=lambda x: int(x["ts"])):
        target = None
        method = ""
        cid = f.get("client_order_id")
        if cid and str(cid) in by_key:
            target, method = by_key[str(cid)], "client_order_id"
        elif mapping:
            key = None
            for lk in f["_keys"]:
                if lk in mapping:
                    key = mapping[lk]
                    break
            if key is not None and str(key) in by_key:
                target, method = by_key[str(key)], "explicit_mapping"
        if target is None:
            for lk in f["_keys"]:
                if lk in by_key:
                    target, method = by_key[lk], "exchange_order_id"
                    break
        if target is not None and target not in used:
            used.add(target)
            rep.pairs.append(Pair(live=f, sim=sims[target], method=method, confidence=1.0))
        else:
            pending.append(f)
    # 启发式：全局按得分从高到低一对一分配
    cands: list[tuple[float, int, int]] = []
    for li, f in enumerate(pending):
        for si, s in enumerate(sims):
            if si in used:
                continue
            sc = _score(f, s, window_ms)
            if sc >= min_confidence:
                cands.append((sc, li, si))
    cands.sort(reverse=True)
    taken_live: set[int] = set()
    for sc, li, si in cands:
        if li in taken_live or si in used:
            continue
        taken_live.add(li)
        used.add(si)
        rep.pairs.append(Pair(live=pending[li], sim=sims[si], method="heuristic", confidence=sc))
    rep.unattributed = [f for li, f in enumerate(pending) if li not in taken_live]
    rep.unmatched_sim = [s for si, s in enumerate(sims) if si not in used]
    rep.pairs.sort(key=lambda p: int(p.live["ts"]))
    if any(p.method == "heuristic" for p in rep.pairs):
        rep.notes.append("有成交缺少订单标签，按品种、方向、时间与数量启发式匹配，请结合置信度阅读")
    if rep.unattributed:
        n_unattr = sum(int(f.get("fills") or 1) for f in rep.unattributed)
        rep.notes.append(f"{n_unattr} 笔真实成交无法归因到该策略（可能是手工交易），未计入策略结果")
    merged = sum(1 for p in rep.pairs if int(p.live.get("fills") or 1) > 1)
    if merged:
        rep.notes.append(
            f"{merged} 个订单在交易所被拆成多笔成交，已按订单归并为一行，价格取数量加权均价；"
            "滑点是订单级滑点，不代表每一笔部分成交的成交价"
        )
    # 未折算手续费必须在 notes 里出现一次：只看 attributed_result 的人容易把
    # `fees` 当成全部成本，从而低估真实手续费。
    unconverted = live_result([p.live for p in rep.pairs], fee_rates=rep.fee_rates)
    if unconverted.get("has_unconverted_fees"):
        rep.notes.append(
            "部分真实成交的手续费以非计价币种支付且没有成交时刻汇率，未折算进总手续费（不估算）："
            + "、".join(f"{k} {v}" for k, v in sorted(unconverted["unconverted_fees"].items()))
        )
    return rep
