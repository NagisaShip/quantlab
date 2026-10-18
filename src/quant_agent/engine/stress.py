"""压力演练：把历史上几个极端行情日的 1 分钟行情，按加速时钟灌进**实盘同一条执行路径**。

为什么不是回测：回测验证的是"策略在历史上赚不赚钱"；压力演练验证的是"极端行情 + 交易所出故障时，
平台的实时执行链路和策略会不会崩、会亏成什么样"。所以这里：

- 策略跑在 `LiveRunner`（实时模拟用的同一个运行时）里，`fill_mode="external"`：
  委托进 outbox，由模拟交易所 `FaultyExchange` 消费、产出成交回报，再经 `apply_reports` 记账——
  与交易所模拟盘（PaperDriver → OMS）完全同一条路径，只是交易所换成了一个会出故障的模拟器；
- 时钟按 1 分钟 bar 推进、不等真实时间；策略周期大于 1 分钟时把 1 分钟线聚合成策略周期，
  但成交仍按 1 分钟分辨率模拟（下单时刻的下一根 1 分钟线开盘价 + 随波动放大的滑点）；
- 故障注入（全部按种子确定，可复现）：
  * 断线：若干分钟内策略收不到行情，恢复后错过的 bar 一次性补发（与实时网关断线重连后的回补一致）；
  * 拒单：暴跌最剧烈的一小时内按较高概率拒单（交易所过载 / -1003 限流等），其余时间低概率；
  * 延迟：成交推迟 1–3 分钟，按那时的 1 分钟开盘价 + 滑点成交；
  * 部分成交：只成交一部分，其余作废；
- 永续合约：按时间点喂资金费率（真实优先，取不到用合成并标注），强平由运行时按强平价执行。

**不会下任何真实订单，也不读取任何密钥**：只用行情服务取公开 K 线 / 资金费率。
取不到真实行情时用按事件形状生成的合成 1 分钟路径兜底，并在报告里明确标注 `data_source: "synthetic"`。
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..data.quality import TIMEFRAME_MS
from ..exchanges.base import Bar
from ..sdk.runtimes import RiskConfig, RuntimeCost
from .live import LIVE_WARMUP_BARS, LiveRunner

MIN_MS = 60_000
DAY_MS = 86_400_000
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT"]
DEFAULT_DRAWDOWN_THRESHOLD = 0.30
MAX_CURVE_POINTS = 600

# ---------------------------------------------------------------------------- 场景
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "covid-2020-03-12",
        "name": "3·12 新冠暴跌",
        "days": ["2020-03-12"],
        "description": "新冠疫情引发全球资产抛售，BTC 单日最深跌约 40%，BitMEX 一度宕机，交易所普遍过载、拒单。",
    },
    {
        "id": "may19-2021-05-19",
        "name": "5·19 大跌",
        "days": ["2021-05-19"],
        "description": "监管消息叠加高杠杆连环爆仓，BTC 盘中跌约 30% 后快速反弹，多家交易所 API 拥堵。",
    },
    {
        "id": "ftx-2022-11-08",
        "name": "FTX 崩盘",
        "days": ["2022-11-08", "2022-11-09"],
        "description": "FTX 挤兑与收购告吹，两天内 BTC 累计跌约 25%，ETH 跌得更深，流动性持续恶化。",
    },
    {
        "id": "yen-carry-2024-08-05",
        "name": "日元套息平仓",
        "days": ["2024-08-05"],
        "description": "日本加息引发套息交易平仓，亚洲时段全球风险资产同步急跌，BTC 跌约 15%、ETH 跌约 20%。",
    },
]

# 合成路径的形状（只在取不到真实行情时使用）：起始价、下跌段（起始分钟、持续分钟、跌幅）、
# 收盘前收复的比例、ETH 相对 BTC 的跌幅放大倍数、暴跌期波动放大倍数。
_SYNTH_PROFILES: dict[str, dict[str, Any]] = {
    "covid-2020-03-12": {
        "start": {"BTC": 7_900.0, "ETH": 194.0},
        "legs": [(9 * 60, 240, 0.22), (20 * 60, 180, 0.13)],
        "recover": 0.15,
        "beta": {"ETH": 1.15},
        "vol_boost": 6.0,
    },
    "may19-2021-05-19": {
        "start": {"BTC": 42_800.0, "ETH": 3_400.0},
        "legs": [(12 * 60, 150, 0.30)],
        "recover": 0.45,
        "beta": {"ETH": 1.3},
        "vol_boost": 7.0,
    },
    "ftx-2022-11-08": {
        "start": {"BTC": 20_500.0, "ETH": 1_570.0},
        "legs": [(14 * 60, 420, 0.12), (24 * 60 + 13 * 60, 360, 0.13)],
        "recover": 0.10,
        "beta": {"ETH": 1.25},
        "vol_boost": 4.0,
    },
    "yen-carry-2024-08-05": {
        "start": {"BTC": 58_000.0, "ETH": 2_700.0},
        "legs": [(0 * 60 + 30, 360, 0.15)],
        "recover": 0.40,
        "beta": {"ETH": 1.35},
        "vol_boost": 5.0,
    },
}

REJECT_MESSAGES = [
    "-1003 请求过多：交易所过载，下单被拒",
    "-1008 服务器繁忙：交易所过载，下单被拒，请稍后重试",
    "-1001 内部错误：交易所撮合引擎繁忙，连接已断开",
    "-1003 TOO_MANY_REQUESTS：IP 权重超限，下单被拒",
]


def scenario_by_id(scenario_id: str) -> dict[str, Any] | None:
    return next((s for s in SCENARIOS if s["id"] == scenario_id), None)


def scenario_window(sc: dict[str, Any]) -> tuple[int, int]:
    """场景的 UTC 时间窗口 [start, end)。"""

    def day_ms(d: str) -> int:
        return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

    return day_ms(sc["days"][0]), day_ms(sc["days"][-1]) + DAY_MS


# ---------------------------------------------------------------------------- 故障计划
@dataclass
class FaultPlan:
    """故障注入计划。全部按 seed 确定：同一种子、同一份行情、同一份策略 → 同一份报告。"""

    seed: int = 20200312
    # None = 自动放在最陡下跌处；[] = 不断线；或 [{"offset_min": 距窗口开始的分钟数, "minutes": 时长}, ...]
    disconnects: list[dict[str, int]] | None = None
    disconnect_count: int = 2
    disconnect_minutes: tuple[int, int] = (3, 10)
    reject_rate: float = 0.10  # 暴跌最剧烈的一小时内
    base_reject_rate: float = 0.02  # 其余时间
    delay_rate: float = 0.30
    delay_minutes: tuple[int, int] = (1, 3)
    partial_rate: float = 0.05
    partial_fill: tuple[float, float] = (0.3, 0.8)
    slippage_bps: float = 2.0  # 基础滑点
    range_slippage: float = 0.25  # 再加上成交那根 1 分钟线振幅 (high-low)/open 的这个比例
    perp_fee: float = 0.0005  # 永续吃单手续费；现货用运行时成本模型的 taker_fee

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["disconnect_minutes"] = list(self.disconnect_minutes)
        d["delay_minutes"] = list(self.delay_minutes)
        d["partial_fill"] = list(self.partial_fill)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any] | FaultPlan | None) -> FaultPlan:
        if isinstance(d, FaultPlan):
            return d
        d = d or {}
        kw: dict[str, Any] = {}
        for k in cls.__dataclass_fields__:
            if k not in d or (d[k] is None and k != "disconnects"):
                continue
            v = d[k]
            if k in ("disconnect_minutes", "delay_minutes"):
                lo, hi = (int(x) for x in v)
                v = (max(0, min(lo, hi)), max(lo, hi))
            elif k == "partial_fill":
                lo, hi = (float(x) for x in v)
                v = (max(0.01, min(lo, hi)), min(0.99, max(lo, hi)))
            elif k == "disconnects":
                v = (
                    None
                    if v is None
                    else [
                        {"offset_min": int(x.get("offset_min", 0)), "minutes": max(1, int(x.get("minutes", 1)))}
                        for x in v
                    ]
                )
            elif k in ("seed", "disconnect_count"):
                v = int(v)
            else:
                v = float(v)
                if k.endswith("_rate"):
                    v = min(1.0, max(0.0, v))
            kw[k] = v
        return cls(**kw)


# ---------------------------------------------------------------------------- 模拟交易所
@dataclass
class _Order:
    intent: dict[str, Any]
    submit_idx: int
    fill_idx: int
    delay: int
    partial: float  # 0 = 全部成交；否则成交比例


class FaultyExchange:
    """会出故障的模拟交易所：消费运行时 outbox 里的委托意图，产出 `apply_external` 认的回报。

    - 委托在第 idx 分钟收盘后送达 → 在第 idx+1+delay 根 1 分钟线开盘成交（delay 为注入的延迟分钟数）；
    - 成交价 = 开盘价 ×(1 ± (基础滑点 + range_slippage × 该分钟振幅))，买入向上、卖出向下；
    - 拒单在送达时立即回报；部分成交按比例向下取整；
    - 余额不足按交易所口径拒单（-2010），不会让本地账本记出负数。
    不碰任何真实接口。
    """

    def __init__(
        self,
        plan: FaultPlan,
        minutes: dict[str, list[Bar]],
        *,
        rng: random.Random,
        crash_hour: tuple[int, int],
        spot_fee: float,
        ledger: Any,
    ) -> None:
        self.plan = plan
        self.minutes = minutes
        self.rng = rng
        self.crash_hour = crash_hour
        self.spot_fee = Decimal(str(spot_fee))
        self.perp_fee = Decimal(str(plan.perp_fee))
        self.ledger = ledger
        self.queue: list[_Order] = []
        self.held: list[dict[str, Any]] = []  # 已产生、还没送达平台的回报（断线期间积压）
        self._held_spend = Decimal(0)
        self._held_qty: dict[str, Decimal] = {}  # 已成交未送达的现货数量变动（带符号）
        self.seq = 0
        self.log: list[dict[str, Any]] = []
        self.stats = {
            "orders": 0,
            "filled": 0,
            "partial": 0,
            "rejected": 0,
            "delayed": 0,
            "insufficient": 0,
            "slippage_bps_sum": 0.0,
            "slippage_bps_max": 0.0,
        }

    @property
    def n(self) -> int:
        return len(next(iter(self.minutes.values())))

    def submit(self, intents: list[dict[str, Any]], idx: int) -> list[dict[str, Any]]:
        """委托送达交易所。返回立即产生的回报（拒单）；其余进入成交队列。"""
        out = []
        for it in intents:
            self.stats["orders"] += 1
            in_crash = self.crash_hour[0] <= idx < self.crash_hour[1]
            rate = self.plan.reject_rate if in_crash else self.plan.base_reject_rate
            if self.rng.random() < rate:
                msg = self.rng.choice(REJECT_MESSAGES)
                self.stats["rejected"] += 1
                out.append(self._report(it, "rejected", Decimal(0), Decimal(0), idx, msg))
                continue
            delay = 0
            if self.rng.random() < self.plan.delay_rate:
                lo, hi = self.plan.delay_minutes
                delay = self.rng.randint(lo, hi)
            partial = 0.0
            if self.rng.random() < self.plan.partial_rate:
                partial = self.rng.uniform(*self.plan.partial_fill)
            self.queue.append(_Order(it, idx, idx + 1 + delay, delay, partial))
        return out

    def due(self, idx: int, *, flush: bool = False) -> list[dict[str, Any]]:
        """第 idx 根 1 分钟线开盘时到期的委托成交（flush=True：窗口结束，剩余的按最后收盘价成交）。"""
        ready = [o for o in self.queue if o.fill_idx <= idx or flush]
        if not ready:
            return []
        self.queue = [o for o in self.queue if not (o.fill_idx <= idx or flush)]
        out = []
        for o in ready:
            out.append(self._fill(o, min(idx, self.n - 1), at_close=flush or idx >= self.n))
        return out

    def _fill(self, o: _Order, idx: int, *, at_close: bool) -> dict[str, Any]:
        it = o.intent
        sec = it["security"]
        bar = self.minutes[sec][idx]
        base = Decimal(str(bar.close if at_close else bar.open))
        rng_frac = (bar.high - bar.low) / bar.open if bar.open else 0.0
        slip = self.plan.slippage_bps / 1e4 + self.plan.range_slippage * rng_frac
        sign = 1 if it["side"] == "buy" else -1
        px = base * (Decimal(1) + Decimal(str(sign * slip)))
        qty = Decimal(it["qty"])
        if o.partial:
            qty = (qty * Decimal(str(o.partial))).quantize(Decimal("1e-8"), rounding="ROUND_DOWN")
        perp = bool(it.get("perp"))
        fee_rate = self.perp_fee if perp else self.spot_fee
        fee = (qty * px * fee_rate).quantize(Decimal("1e-8"))
        # 余额检查：交易所按真实余额拒单（本地账本 + 已成交未送达的回报）
        if perp:
            lev = Decimal(it.get("leverage") or 1)
            open_q = min(qty, Decimal(it.get("open_qty") or 0))
            spend = open_q * px / lev + fee
        elif it["side"] == "buy":
            spend = qty * px + fee
        else:
            spend = -(qty * px - fee)
        avail = self.ledger.cash() - self._held_spend
        ts = bar.close_time if at_close else bar.open_time
        if spend > 0 and spend > avail:
            self.stats["rejected"] += 1
            self.stats["insufficient"] += 1
            return self._report(
                it,
                "rejected",
                Decimal(0),
                Decimal(0),
                idx,
                "-2010 账户余额不足：成交价随行情恶化，可用资金不够，下单被拒",
                ts=ts,
            )
        if not perp and it["side"] == "sell":
            h = self.ledger.holdings.get(sec)
            have = (h.qty if h is not None else Decimal(0)) + self._held_qty.get(sec, Decimal(0))
            if qty > have:
                # 延迟 / 部分成交打乱了先后：卖单先于买单到达，交易所按实际持仓拒单
                self.stats["rejected"] += 1
                self.stats["insufficient"] += 1
                return self._report(
                    it,
                    "rejected",
                    Decimal(0),
                    Decimal(0),
                    idx,
                    f"-2010 账户余额不足：持仓 {have} 不够卖出 {qty}，下单被拒",
                    ts=ts,
                )
        if qty <= 0:
            self.stats["rejected"] += 1
            return self._report(it, "rejected", Decimal(0), Decimal(0), idx, "可成交数量为 0", ts=ts)
        self._held_spend += spend
        if not perp:
            self._held_qty[sec] = self._held_qty.get(sec, Decimal(0)) + (qty if it["side"] == "buy" else -qty)
        ref = Decimal(it.get("ref_price") or base)
        bps = float(abs(px - ref) / ref * 10_000) if ref else 0.0
        self.stats["slippage_bps_sum"] += bps
        self.stats["slippage_bps_max"] = max(self.stats["slippage_bps_max"], bps)
        if o.delay:
            self.stats["delayed"] += 1
        status = "partial" if o.partial else "filled"
        self.stats[status] += 1
        msg = []
        if o.delay:
            msg.append(f"交易所延迟 {o.delay} 分钟成交")
        if o.partial:
            msg.append(f"流动性不足，只成交 {o.partial:.0%}")
        return self._report(it, status, qty, px, idx, "；".join(msg), fee=fee, ts=ts, delay=o.delay, slip=bps)

    def _report(
        self,
        it: dict[str, Any],
        status: str,
        qty: Decimal,
        px: Decimal,
        idx: int,
        msg: str,
        *,
        fee: Decimal = Decimal(0),
        ts: int | None = None,
        delay: int = 0,
        slip: float = 0.0,
    ) -> dict[str, Any]:
        self.seq += 1
        sec = it["security"]
        quote = sec.split("/")[-1].split(":")[0].upper() if "/" in sec else "USDT"
        bar = self.minutes[sec][min(idx, self.n - 1)]
        rep = {
            "client_order_id": it["client_order_id"],
            "status": status,
            "filled_qty": str(qty),
            "avg_price": str(px.quantize(Decimal("1e-8"))) if px else "0",
            "fee": str(fee),
            "fee_currency": quote,
            "ts": int(ts if ts is not None else bar.close_time),
            "exchange_order_id": f"stress{self.seq}",
            "message": msg,
        }
        if len(self.log) < 500:
            self.log.append(
                {
                    "ts": rep["ts"],
                    "security": sec,
                    "side": it["side"],
                    "qty": it["qty"],
                    "status": status,
                    "filled_qty": rep["filled_qty"],
                    "price": rep["avg_price"],
                    "ref_price": it.get("ref_price"),
                    "delay_min": delay,
                    "slippage_bps": round(slip, 2),
                    "message": msg,
                }
            )
        return rep

    def hold(self, reports: list[dict[str, Any]]) -> None:
        self.held.extend(reports)

    def deliver(self) -> list[dict[str, Any]]:
        out, self.held = self.held, []
        self._held_spend = Decimal(0)
        self._held_qty = {}
        return out


# ---------------------------------------------------------------------------- 行情
def _norm(bars: list[Bar], symbol: str, timeframe: str, step: int) -> list[Bar]:
    """统一成 close_time = open_time + step（交易所给的是 +step-1），按开盘时间去重排序。"""
    seen: dict[int, Bar] = {}
    for b in bars:
        seen.setdefault(int(b.open_time), b)
    out = []
    for t in sorted(seen):
        b = seen[t]
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=t,
                close_time=t + step,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
                closed=True,
                source=b.source,
                instrument_id=b.instrument_id,
                raw_ref=b.raw_ref,
            )
        )
    return out


def _grid(bars: list[Bar], symbol: str, start: int, end: int) -> tuple[list[Bar], int]:
    """对齐到 1 分钟网格；缺失的分钟用前值补一根 0 成交量的平 bar（交易所当时没有成交），返回 (bars, 补了几根)。"""
    by = {b.open_time: b for b in bars}
    out: list[Bar] = []
    filled = 0
    prev = bars[0].open if bars else 0.0
    for t in range(start, end, MIN_MS):
        b = by.get(t)
        if b is None:
            filled += 1
            b = Bar(
                symbol=symbol,
                timeframe="1m",
                open_time=t,
                close_time=t + MIN_MS,
                open=prev,
                high=prev,
                low=prev,
                close=prev,
                volume=0.0,
                closed=True,
                source="gap-fill",
            )
        out.append(b)
        prev = b.close
    return out, filled


class _Market:
    """行情来源适配：接受 services（取 .market）或 MarketDataService 本身。"""

    def __init__(self, services_or_market: Any, exchange: str) -> None:
        self.market = getattr(services_or_market, "market", None) or services_or_market
        self.exchange = exchange
        self.network_down = ""  # 一次网络失败后，本次演练的其余请求不再重试（避免每个场景都等超时）

    def _adapter(self) -> Any:
        adapters = getattr(self.market, "adapters", None) or {}
        return adapters.get(self.exchange)

    def fetch(self, symbol: str, timeframe: str, start: int, end: int, tag: str) -> list[Bar]:
        """取 [start, end) 的已收盘 K 线：先查演练专用缓存，缺了再向交易所拉公开 K 线并写回缓存。

        缓存键带场景标签：历史窗口和日常缓存的"最近一段"分开存，避免触发"补齐中间几年"的大请求。
        """
        step = TIMEFRAME_MS[timeframe]
        expected = (end - start) // step
        cache = getattr(self.market, "cache", None)
        key = f"{self.exchange}-stress-{tag}"
        if cache is not None:
            try:
                cached = [b for b in cache.load(key, symbol, timeframe) if start <= b.open_time < end]
                if len(cached) >= expected * 0.98:
                    return _norm(cached, symbol, timeframe, step)
            except Exception:  # 缓存损坏就当没有
                pass
        if self.network_down:
            raise RuntimeError(self.network_down)
        adapter = self._adapter()
        if adapter is None:
            raise RuntimeError(f"未配置交易所适配器 {self.exchange}")
        try:
            pull = getattr(self.market, "_pull", None)
            bars = (
                pull(adapter, self.exchange, symbol, timeframe, start, end)
                if pull
                else adapter.fetch_bars(symbol, timeframe, start, end)
            )
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"[:300]
            if not isinstance(exc, (ValueError, KeyError)):
                self.network_down = msg
            raise RuntimeError(msg) from exc
        bars = [b for b in bars if start <= b.open_time < end]
        if len(bars) < expected * 0.9:
            raise RuntimeError(f"{self.exchange} 只返回了 {len(bars)}/{expected} 根 {symbol} {timeframe} 行情")
        if cache is not None:
            try:
                cache.merge(key, symbol, timeframe, [b for b in bars if b.closed])
            except Exception:
                pass
        return _norm(bars, symbol, timeframe, step)

    def funding(self, symbol: str, start: int, end: int) -> tuple[list[tuple[int, str]], str]:
        fr = getattr(self.market, "funding_rates", None)
        if fr is None or self.network_down:
            return [], self.network_down or "行情服务不支持资金费率"
        try:
            rows, _src, err = fr(symbol, exchange=self.exchange, start=start, end=end, source="exchange")
        except Exception as exc:
            return [], f"{type(exc).__name__}: {exc}"[:200]
        return [(int(t), str(r)) for t, r in rows], err


def _seed_float(*parts: Any) -> random.Random:
    return random.Random(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest())


def _synthetic_allowed() -> bool:
    """合成路径只在测试 / 离线演示时允许（QAGENT_ALLOW_SYNTHETIC=1），平台默认如实报「未接入数据」。"""
    import os

    return os.environ.get("QAGENT_ALLOW_SYNTHETIC", "0") == "1"


def synthetic_crash_minutes(scenario_id: str, symbol: str, start: int, end: int) -> list[Bar]:
    """按事件形状生成确定性的合成 1 分钟路径：平静 → 一段或几段加速下跌（波动放大、长下影）→ 部分收复。

    只用于网络不可用时让演练流程能跑通，**不是**真实行情；报告会标注 data_source = synthetic。
    """
    prof = _SYNTH_PROFILES.get(scenario_id) or {
        "start": {},
        "legs": [(12 * 60, 180, 0.25)],
        "recover": 0.3,
        "beta": {},
        "vol_boost": 5.0,
    }
    spot = symbol.split(":")[0]
    asset = spot.split("/")[0].upper()
    from ..data.service import SYNTHETIC_START

    p0 = float(prof["start"].get(asset) or SYNTHETIC_START.get(asset, 100.0))
    beta = float(prof.get("beta", {}).get(asset, 1.0))
    n = (end - start) // MIN_MS
    legs = prof["legs"]
    total = 1.0
    for _s, _d, drop in legs:
        total *= 1 - min(0.95, drop * beta)
    last_end = max(s + d for s, d, _ in legs)
    rng = _seed_float("stress-synth", scenario_id, spot)

    def target(i: int) -> float:
        lp = 0.0
        for s, d, drop in legs:
            x = min(1.0, max(0.0, (i - s) / d))
            shape = x * x * (3 - 2 * x)  # 先慢后快再缓：平滑阶跃
            lp += math.log(1 - min(0.95, drop * beta)) * shape
        if i > last_end and n > last_end:
            lp -= math.log(total) * prof["recover"] * min(1.0, (i - last_end) / max(1, n - last_end))
        return lp

    def in_crash(i: int) -> bool:
        return any(s <= i < s + d for s, d, _ in legs)

    base_vol = 0.0009
    noise = 0.0
    out: list[Bar] = []
    price = p0
    for i in range(n):
        vol = base_vol * (prof["vol_boost"] if in_crash(i) else (2.0 if i > last_end and i < last_end + 240 else 1.0))
        noise = noise * 0.97 + rng.gauss(0, vol)
        o = price
        c = p0 * math.exp(target(i + 1) + noise)
        wick_dn = abs(rng.gauss(0, vol)) * (3.0 if in_crash(i) and rng.random() < 0.08 else 1.0)
        wick_up = abs(rng.gauss(0, vol))
        hi = max(o, c) * (1 + wick_up)
        lo = min(o, c) * (1 - wick_dn)
        t = start + i * MIN_MS
        vol_units = (abs(rng.gauss(50, 15)) + 5) * (8 if in_crash(i) else 1)
        out.append(
            Bar(
                symbol=symbol,
                timeframe="1m",
                open_time=t,
                close_time=t + MIN_MS,
                open=round(o, 6),
                high=round(hi, 6),
                low=round(lo, 6),
                close=round(c, 6),
                volume=round(vol_units, 4),
                closed=True,
                source="synthetic",
                instrument_id=f"synthetic:{symbol}",
                raw_ref=f"stress-synthetic:{scenario_id}:{symbol}",
            )
        )
        price = c
    return out


def _synthetic_warmup(
    scenario_id: str, symbol: str, timeframe: str, end: int, first_open: float, count: int
) -> list[Bar]:
    """预热用的合成历史（策略周期）：从窗口开盘价倒推的小波动随机游走，确定性。"""
    step = TIMEFRAME_MS[timeframe]
    rng = _seed_float("stress-warmup", scenario_id, symbol.split(":")[0], timeframe)
    vol = 0.0009 * math.sqrt(step / MIN_MS)
    closes = [first_open]
    for _ in range(count):
        closes.append(closes[-1] / math.exp(rng.gauss(0, vol)))
    closes.reverse()  # closes[-1] == first_open
    out = []
    for i in range(count):
        o, c = closes[i], closes[i + 1]
        t = end - (count - i) * step
        out.append(
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                open_time=t,
                close_time=t + step,
                open=o,
                high=max(o, c) * (1 + abs(rng.gauss(0, vol / 2))),
                low=min(o, c) * (1 - abs(rng.gauss(0, vol / 2))),
                close=c,
                volume=100.0,
                closed=True,
                source="synthetic",
            )
        )
    return out


def _perp_from_spot(scenario_id: str, perp: str, spot_bars: list[Bar]) -> list[Bar]:
    """永续行情缺失时由现货推出：叠加很小的确定性基差（暴跌时永续通常贴水，这里按 -0.05%~+0.03% 摆动）。"""
    rng = _seed_float("stress-basis", scenario_id, perp)
    out = []
    basis = 0.0
    for b in spot_bars:
        basis = max(-0.0015, min(0.0008, basis * 0.98 + rng.gauss(-0.00001, 0.00008)))
        k = 1 + basis
        out.append(
            Bar(
                symbol=perp,
                timeframe=b.timeframe,
                open_time=b.open_time,
                close_time=b.close_time,
                open=b.open * k,
                high=b.high * k,
                low=b.low * k,
                close=b.close * k,
                volume=b.volume,
                closed=True,
                source=f"derived:{b.source}",
                instrument_id=f"derived:{perp}",
            )
        )
    return out


def _aggregate(minutes: list[Bar], timeframe: str) -> dict[int, Bar]:
    """1 分钟线 → 策略周期 bar。返回 {收盘那根 1 分钟线的序号: 聚合 bar}，只收完整的周期。"""
    if timeframe == "1m":
        return {
            i: Bar(
                symbol=b.symbol,
                timeframe="1m",
                open_time=b.open_time,
                close_time=b.close_time,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                closed=True,
                source=b.source,
            )
            for i, b in enumerate(minutes)
        }
    step = TIMEFRAME_MS[timeframe]
    per = step // MIN_MS
    out: dict[int, Bar] = {}
    i = 0
    n = len(minutes)
    while i < n:
        t0 = minutes[i].open_time - minutes[i].open_time % step
        j = i
        while j < n and minutes[j].open_time < t0 + step:
            j += 1
        grp = minutes[i:j]
        if len(grp) == per:
            out[j - 1] = Bar(
                symbol=grp[0].symbol,
                timeframe=timeframe,
                open_time=t0,
                close_time=t0 + step,
                open=grp[0].open,
                high=max(b.high for b in grp),
                low=min(b.low for b in grp),
                close=grp[-1].close,
                volume=sum(b.volume for b in grp),
                closed=True,
                source=grp[0].source,
            )
        i = j
    return out


def load_scenario_data(market: _Market, sc: dict[str, Any], securities: list[str], timeframe: str) -> dict[str, Any]:
    """取场景窗口的 1 分钟线（对齐网格）与策略周期的预热历史。真实取不到 → 整个场景用合成路径。"""
    start, end = scenario_window(sc)
    step = TIMEFRAME_MS[timeframe]
    tag = sc["id"]
    warnings: list[str] = []
    notes: list[str] = []
    spots = [s for s in securities if not s.upper().endswith(":PERP")]
    perps = [s for s in securities if s.upper().endswith(":PERP")]
    # 永续的现货母本也要取（用于缺永续数据时推导），但不交给策略
    bases = list(dict.fromkeys(spots + [p.split(":")[0] for p in perps]))
    minutes: dict[str, list[Bar]] = {}
    warm: dict[str, list[Bar]] = {}
    source = "exchange"
    err = ""
    try:
        for s in bases:
            raw = market.fetch(s, "1m", start, end, tag)
            minutes[s], gaps = _grid(raw, s, start, end)
            if gaps:
                notes.append(f"{s} 有 {gaps} 分钟没有成交（交易所当时缺数据），按前值补了 0 成交量的 bar")
            if s in spots:
                warm[s] = market.fetch(s, timeframe, start - LIVE_WARMUP_BARS * step, start, tag)
    except Exception as exc:
        err = str(exc)
        if not _synthetic_allowed():
            raise RuntimeError(
                f"未接入数据：压力场景「{sc.get('name') or tag}」的历史 1 分钟行情取不到（{err[:160]}）。"
                "请检查网络或交易所接口后重试；平台不再用合成路径代替真实行情"
            ) from exc
        source = "synthetic"
    if source == "synthetic":
        warnings.append(
            f"真实行情取不到（{err or '网络不可用'}），本场景使用按事件形状生成的合成 1 分钟路径——"
            "只能检验执行链路与故障处理，不能代表策略在真实行情中的表现"
        )
        minutes, warm = {}, {}
        for s in bases:
            minutes[s] = synthetic_crash_minutes(sc["id"], s, start, end)
            if s in spots:
                warm[s] = _synthetic_warmup(sc["id"], s, timeframe, start, minutes[s][0].open, LIVE_WARMUP_BARS)
    for p in perps:
        base = p.split(":")[0]
        got = None
        if source == "exchange":
            try:
                raw = market.fetch(p, "1m", start, end, tag)
                got, gaps = _grid(raw, p, start, end)
                warm[p] = market.fetch(p, timeframe, start - LIVE_WARMUP_BARS * step, start, tag)
            except Exception as exc:
                got = None
                warnings.append(f"{p} 永续 K 线取不到（{str(exc)[:120]}），由现货 {base} 叠加小基差推导")
        if got is None:
            got = _perp_from_spot(sc["id"], p, minutes[base])
            if base in warm:
                warm[p] = _perp_from_spot(sc["id"], p, warm[base])
            elif source == "exchange":
                try:
                    wb = market.fetch(base, timeframe, start - LIVE_WARMUP_BARS * step, start, tag)
                    warm[p] = _perp_from_spot(sc["id"], p, wb)
                except Exception:
                    warm[p] = _synthetic_warmup(sc["id"], p, timeframe, start, got[0].open, LIVE_WARMUP_BARS)
            else:
                warm[p] = _synthetic_warmup(sc["id"], p, timeframe, start, got[0].open, LIVE_WARMUP_BARS)
            if source == "exchange":
                notes.append(f"{p} 由现货推导（基差为合成）")
        minutes[p] = got
    # 资金费率：真实优先，取不到用合成并标注
    funding: dict[str, list[tuple[int, str]]] = {}
    for p in perps:
        rows: list[tuple[int, str]] = []
        ferr = ""
        if source == "exchange":
            rows, ferr = market.funding(p, start - 8 * 3_600_000, end)
        if not rows and not _synthetic_allowed():
            warnings.append(f"{p} 资金费率取不到（{ferr or '无数据'}），资金费按 0 计——资金费相关结论不可信")
        elif not rows:
            from ..data.service import synthetic_funding

            rows = synthetic_funding(p, start - 8 * 3_600_000, end)
            if source == "exchange":
                warnings.append(f"{p} 资金费率取不到（{ferr or '无数据'}），按合成费率结算")
        funding[p] = rows
    return {
        "start": start,
        "end": end,
        "minutes": {s: minutes[s] for s in securities},
        "warmup": warm,
        "data_source": source,
        "warnings": warnings,
        "notes": notes,
        "funding": funding,
    }


# ---------------------------------------------------------------------------- 故障放置
def _crash_points(closes: list[float]) -> tuple[tuple[int, int], list[int]]:
    """(最剧烈下跌的一小时 [i0, i1), 15 分钟跌幅从陡到缓排好的结束序号)。"""
    n = len(closes)
    w = min(60, max(1, n - 1))
    best, i0 = 0.0, 0
    for i in range(0, n - w):
        r = closes[i + w] / closes[i] - 1 if closes[i] else 0.0
        if r < best:
            best, i0 = r, i
    k = min(15, max(1, n - 1))
    drops = sorted(range(k, n), key=lambda i: (closes[i] / closes[i - k] - 1) if closes[i - k] else 0.0)
    return (i0, i0 + w), drops


def plan_disconnects(plan: FaultPlan, closes: list[float], rng: random.Random) -> list[tuple[int, int]]:
    """断线窗口 [(开始序号, 分钟数)]。自动模式：放在最陡的下跌段上，两段之间至少隔 90 分钟。"""
    n = len(closes)
    if plan.disconnects is not None:
        return sorted((max(1, min(n - 2, d["offset_min"])), d["minutes"]) for d in plan.disconnects)
    _hour, drops = _crash_points(closes)
    out: list[tuple[int, int]] = []
    for end_i in drops:
        if len(out) >= plan.disconnect_count:
            break
        s = max(1, end_i - 10)
        if any(abs(s - o) < 90 for o, _ in out):
            continue
        lo, hi = plan.disconnect_minutes
        out.append((s, rng.randint(lo, hi)))
    return sorted(out)


# ---------------------------------------------------------------------------- 运行
def _securities(source: str, securities: list[str] | None) -> list[str]:
    secs = [s.strip() for s in (securities or []) if s and s.strip()] or list(DEFAULT_SYMBOLS)
    if ":PERP" in source or "set_leverage(" in source:
        for s in list(secs):
            if not s.upper().endswith(":PERP") and f"{s}:PERP" not in secs:
                secs.append(f"{s}:PERP")
    for lit in re.findall(r"[A-Z0-9]{2,10}/USDT:PERP", source):
        if lit not in secs:
            secs.append(lit)
    return list(dict.fromkeys(secs))


def _shadow_equity(ledger: Any, prices: dict[str, float]) -> float:
    """按 1 分钟收盘价估值（只读账本，不改运行时状态——断线期间平台本来也看不到价格）。"""
    eq = ledger.cash()
    for k, h in ledger.holdings.items():
        if h.qty:
            px = prices.get(k)
            eq += h.qty * (Decimal(str(px)) if px else h.last_price)
    for k, p in ledger.perps.items():
        if p.qty:
            px = prices.get(k)
            px_d = Decimal(str(px)) if px else p.last_price
            eq += max(Decimal(0), p.margin + p.qty * (px_d - p.entry))
    return float(eq)


def _downsample(points: list[list[float]], limit: int = MAX_CURVE_POINTS) -> list[list[float]]:
    """等距抽样到 limit 个点，但每段保留最低点（回撤图不能把最深的坑抽掉）。"""
    n = len(points)
    if n <= limit:
        return points
    buckets = limit // 2
    size = n / buckets
    out = []
    for b in range(buckets):
        seg = points[int(b * size) : int((b + 1) * size)] or [points[-1]]
        lo = min(seg, key=lambda p: p[1])
        last = seg[-1]
        out.extend(sorted({id(x): x for x in (lo, last)}.values(), key=lambda p: p[0]))
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out[:limit] if len(out) > limit else out


def _max_drawdown(values: list[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, 1 - v / peak)
    return mdd


def run_scenario(
    market: _Market,
    sc: dict[str, Any],
    *,
    source: str,
    params: dict[str, Any] | None,
    securities: list[str],
    timeframe: str,
    initial_cash: float,
    plan: FaultPlan,
    lang: str = "python",
    cost: RuntimeCost | None = None,
    risk: RiskConfig | None = None,
    drawdown_threshold: float = DEFAULT_DRAWDOWN_THRESHOLD,
    progress: Callable[[float], None] | None = None,
    preloaded: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """跑一个场景，返回该场景的报告。引擎层面的异常不向外抛，记为 status = crashed。"""
    t_begin = time.monotonic()
    report: dict[str, Any] = {
        "id": sc["id"],
        "name": sc["name"],
        "days": list(sc["days"]),
        "description": sc["description"],
        "status": "running",
        "timeframe": timeframe,
        "securities": list(securities),
        "faults": plan.to_dict(),
    }
    try:
        data = preloaded if preloaded is not None else load_scenario_data(market, sc, securities, timeframe)
    except Exception as exc:
        report.update(
            status="crashed",
            error=f"行情准备失败：{type(exc).__name__}: {exc}",
            verdict="引擎异常中断：行情准备失败",
            completed=False,
        )
        return report
    report.update(data_source=data["data_source"], warnings=list(data["warnings"]), notes=list(data["notes"]))
    minutes: dict[str, list[Bar]] = data["minutes"]
    first = securities[0]
    n = len(minutes[first])
    closes = [b.close for b in minutes[first]]
    crash_hour, _ = _crash_points(closes)
    rng = _seed_float("stress-faults", plan.seed, sc["id"])
    windows = plan_disconnects(plan, closes, rng)
    down = [False] * (n + 1)
    for s, m in windows:
        for i in range(s, min(n, s + m)):
            down[i] = True

    from ..strategies.lang import load

    module = load(lang, source, name="stress", params=params or None)
    live = LiveRunner()
    sid = f"stress-{sc['id']}"
    cost = cost or RuntimeCost()
    agg = {s: _aggregate(minutes[s], timeframe) for s in securities}
    hist = {s: list(data["warmup"][s]) for s in securities}
    state = live.start(
        sid,
        module,
        hist,
        timeframe=timeframe,
        initial_cash=initial_cash,
        cost=cost,
        started_at=data["start"],
        risk=risk or RiskConfig(),
        fill_mode="external",
    )
    rt = state.runtime
    ex = FaultyExchange(
        plan,
        minutes,
        rng=_seed_float("stress-exchange", plan.seed, sc["id"]),
        crash_hour=crash_hour,
        spot_fee=cost.taker_fee,
        ledger=rt.ledger,
    )
    funding = {p: [[t, r] for t, r in rows] for p, rows in data["funding"].items()} or None

    errors: list[str] = []
    rejected: list[dict[str, Any]] = []
    equity: list[list[float]] = []
    backlog: dict[str, list[Bar]] = {s: [] for s in securities}
    disc_rows: list[dict[str, Any]] = []
    liq_gaps: list[dict[str, Any]] = []
    cur_disc: dict[str, Any] | None = None
    watch_after: dict[str, Any] | None = None  # 重连后再观察一个 bar（运行时下一根才把策略的反应送出去）
    steps = 0
    bursts = 0

    def apply(reports: list[dict[str, Any]], idx: int) -> None:
        if not reports:
            return
        res = live.apply_reports(sid, reports)
        rejected.extend(res.get("rejected") or [])
        errors.extend(res.get("errors") or [])
        if res.get("outbox"):  # on_trade 回调里又下了单
            ex.hold(ex.submit(res["outbox"], idx))

    status, crash_err = "completed", ""
    try:
        for i in range(n):
            # 1) 交易所侧：到期委托在这根 1 分钟线开盘成交（断线也照常成交，只是回报送不到平台）
            ex.hold(ex.due(i))
            connected = not down[i]
            if connected and cur_disc is not None:
                # 重连：断线时长按分钟算；补发情况等下一次把积压的 bar 交给策略时再记
                cur_disc.update(
                    end_ts=minutes[first][i].open_time,
                    minutes=i - cur_disc["start_idx"],
                    price_after=minutes[first][i - 1].close,
                    awaiting_burst=True,
                )
                cur_disc = None
            if not connected and cur_disc is None:
                cur_disc = {
                    "start_ts": minutes[first][i].open_time,
                    "start_idx": i,
                    "price_before": minutes[first][max(0, i - 1)].close,
                    "reports_held": 0,
                }
                disc_rows.append(cur_disc)
            if connected:
                apply(ex.deliver(), i)
            elif cur_disc is not None:
                cur_disc["reports_held"] = len(ex.held)
            # 2) 行情侧：策略周期 bar 收盘 → 送策略（断线期间积压，重连后一次性补发）
            for s in securities:
                b = agg[s].get(i)
                if b is not None:
                    backlog[s].append(b)
            if connected and any(backlog.values()):
                latest = {s: list(v) for s, v in backlog.items() if v}
                burst = max(len(v) for v in latest.values())
                pre_perps = {k: (p.qty, p.liquidation_price) for k, p in rt.ledger.perps.items() if p.qty}
                exp_before = rt.counters.get("expired_signals", 0)
                marks = {s: minutes[s][i].close for s in securities}
                # 资金费率只在第一步整批交给内核（内核按结算时刻去重、按时间点结算）
                res = live.step(sid, latest, marks=marks, funding=funding if steps == 0 else None)
                steps += 1
                for v in backlog.values():
                    v.clear()
                errors.extend(res.get("errors") or [])
                rejected.extend(res.get("rejected") or [])
                outbox = res.get("outbox") or []
                if burst > 1:
                    bursts += 1
                    # 运行时只在补发的第一根上撮合 / 检查强平：后面几根若击穿强平价，这里记一笔"漏检"
                    for k, (q0, liq) in pre_perps.items():
                        bars_k = latest.get(k) or []
                        pos = rt.ledger.perps.get(k)
                        if not bars_k[1:] or pos is None or not pos.qty or (pos.qty > 0) != (q0 > 0):
                            continue
                        hit = any(
                            (q0 > 0 and Decimal(str(b.low)) <= liq) or (q0 < 0 and Decimal(str(b.high)) >= liq)
                            for b in bars_k[1:]
                        )
                        if hit:
                            liq_gaps.append(
                                {
                                    "security": k,
                                    "ts": bars_k[-1].close_time,
                                    "liquidation_price": float(liq),
                                    "bars_in_burst": len(bars_k),
                                }
                            )
                if disc_rows and disc_rows[-1].pop("awaiting_burst", False):
                    d = disc_rows[-1]
                    d.update(
                        backfill_bars=burst,
                        skipped_bars=int(res.get("skipped_bars") or 0),
                        expired_signals=rt.counters.get("expired_signals", 0) - exp_before,
                        orders_in_burst=len(outbox),
                        errors=len(res.get("errors") or []),
                    )
                    watch_after = d
                elif watch_after is not None:
                    watch_after["orders_after_reconnect"] = watch_after.get("orders_in_burst", 0) + len(outbox)
                    watch_after = None
                if outbox:
                    apply(ex.submit(outbox, i), i)
            prices = {s: minutes[s][i].close for s in securities}
            equity.append([minutes[first][i].close_time, _shadow_equity(rt.ledger, prices)])
            if progress and i % 240 == 0:
                progress(i / n)
        # 窗口结束：还在途的委托按最后收盘价成交并送达
        ex.hold(ex.due(n, flush=True))
        apply(ex.deliver(), n - 1)
        prices = {s: minutes[s][-1].close for s in securities}
        equity[-1] = [minutes[first][-1].close_time, _shadow_equity(rt.ledger, prices)]
    except Exception as exc:  # 引擎崩溃：这是演练要抓的头号问题
        status = "crashed"
        crash_err = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc(limit=6)[-3000:]
    finally:
        live.drop(sid)

    eq_vals = [p[1] for p in equity] or [float(initial_cash)]
    end_eq = eq_vals[-1]
    mdd = _max_drawdown([float(initial_cash), *eq_vals])
    worst_1h = 0.0
    for k in range(60, len(eq_vals)):
        if eq_vals[k - 60] > 0:
            worst_1h = min(worst_1h, eq_vals[k] / eq_vals[k - 60] - 1)
    liqs = int(rt.counters.get("liquidations", 0))
    liq_loss = float(rt.ledger.liquidation_loss)
    by_code: dict[str, int] = {}
    for r in rejected:
        by_code[r.get("code") or "UNKNOWN"] = by_code.get(r.get("code") or "UNKNOWN", 0) + 1
    fills = [f for f in rt.fills]
    st = ex.stats
    filled_n = st["filled"] + st["partial"]
    price_curve = _downsample([[b.close_time, b.close] for b in minutes[first]])
    disconnects = []
    for d in disc_rows:
        disconnects.append(
            {
                "start_ts": d["start_ts"],
                "end_ts": d.get("end_ts"),
                "minutes": d.get("minutes"),
                "price_change": round(d["price_after"] / d["price_before"] - 1, 6) if d.get("price_after") else None,
                "backfill_bars": d.get("backfill_bars", 0),
                "skipped_bars": d.get("skipped_bars", 0),
                "reports_held": d.get("reports_held", 0),
                "expired_signals": d.get("expired_signals", 0),
                "orders_in_burst": d.get("orders_in_burst", 0),
                "orders_after_reconnect": d.get("orders_after_reconnect", d.get("orders_in_burst", 0)),
                "strategy_errors": d.get("errors", 0),
            }
        )
    uniq_errors = list(dict.fromkeys(str(e) for e in errors))
    if liq_gaps:
        report["warnings"].append(
            f"断线补发的 {len(liq_gaps)} 段行情里有 bar 击穿了强平价，但运行时只在补发的第一根上检查强平——"
            "真实交易所会在断线期间直接强平，平台账本会和交易所对不上，需要靠对账发现"
        )
    report.update(
        status=status,
        completed=status == "completed",
        error=crash_err,
        bars=n,
        strategy_bars=steps,
        start_ts=data["start"],
        end_ts=data["end"],
        start_equity=float(initial_cash),
        end_equity=round(end_eq, 6),
        return_pct=round(end_eq / initial_cash - 1, 6) if initial_cash else 0.0,
        max_drawdown=round(mdd, 6),
        worst_1h=round(worst_1h, 6),
        crash_hour={
            "start_ts": minutes[first][crash_hour[0]].open_time,
            "end_ts": minutes[first][min(n - 1, crash_hour[1] - 1)].close_time,
            "price_change": round(closes[min(n - 1, crash_hour[1])] / closes[crash_hour[0]] - 1, 6),
        },
        price_change=round(closes[-1] / minutes[first][0].open - 1, 6),
        intraday_low=round(min(b.low for b in minutes[first]) / minutes[first][0].open - 1, 6),
        orders=st["orders"],
        fills=filled_n,
        rejections=st["rejected"],
        insufficient_rejections=st["insufficient"],
        partials=st["partial"],
        delayed=st["delayed"],
        avg_slippage_bps=round(st["slippage_bps_sum"] / filled_n, 2) if filled_n else 0.0,
        max_slippage_bps=round(st["slippage_bps_max"], 2),
        platform_rejections=by_code,
        expired_signals=int(rt.counters.get("expired_signals", 0)),
        ledger_mismatch=int(rt.counters.get("ledger_mismatch", 0)),
        liquidations=liqs,
        liquidation_loss=round(liq_loss, 6),
        liquidation_gaps=liq_gaps,
        funding=rt.ledger.funding_report() if rt.ledger.perps else None,
        disconnects=disconnects,
        backfill_bursts=bursts,
        strategy_errors=len(errors),
        error_messages=uniq_errors[:20],
        trades=len(fills),
        exchange_log=ex.log[:200],
        equity_curve=_downsample(equity),
        price_curve=price_curve,
        price_symbol=first,
        elapsed_s=round(time.monotonic() - t_begin, 3),
    )
    report["verdict"] = _verdict(report, drawdown_threshold)
    report["drawdown_breach"] = mdd > drawdown_threshold
    return report


def _verdict(r: dict[str, Any], threshold: float) -> str:
    if r["status"] != "completed":
        return f"引擎异常中断：{r.get('error') or '未知错误'}"
    parts = []
    if r["liquidations"]:
        parts.append(f"被强平 {r['liquidations']} 次，强平损失 {r['liquidation_loss']:.2f}")
    if r["max_drawdown"] > threshold:
        parts.append(f"回撤 {r['max_drawdown']:.1%} 超过阈值 {threshold:.0%}")
    if r["strategy_errors"]:
        parts.append(f"策略报错 {r['strategy_errors']} 次")
    if r.get("liquidation_gaps"):
        parts.append("断线期间强平漏检")
    if not parts and not r.get("fills"):
        # 场景里一笔成交都没有：没有持仓就谈不上「扛住」，不能当成抗压成功
        return f"无法判断：场景内没有成交（下单 {r.get('orders', 0)} 次），策略在这段行情里没有持仓"
    if not parts:
        return f"扛住了：最大回撤 {r['max_drawdown']:.1%}，收益 {r['return_pct']:+.1%}"
    return "；".join(parts) + f"（最大回撤 {r['max_drawdown']:.1%}，收益 {r['return_pct']:+.1%}）"


def run_stress(
    services_or_market: Any,
    *,
    source: str,
    params: dict[str, Any] | None = None,
    securities: list[str] | None = None,
    timeframe: str = "1m",
    initial_cash: float = 10_000.0,
    scenarios: list[str] | None = None,
    faults: dict[str, Any] | FaultPlan | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    lang: str = "python",
    cost: RuntimeCost | dict[str, Any] | None = None,
    risk: RiskConfig | dict[str, Any] | None = None,
    drawdown_threshold: float = DEFAULT_DRAWDOWN_THRESHOLD,
    exchange: str = "binance",
    preloaded: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """压力演练入口：逐个场景跑完，返回总报告。不会下真实订单、不读取密钥。

    验收口径：每个场景都跑完（引擎不崩溃）即 passed；回撤超过阈值、被强平只标记，不算失败。
    策略代码本身加载失败（语法错误等）直接抛 StrategyLoadError / ValueError——那不是演练能回答的问题。
    """
    if timeframe not in TIMEFRAME_MS or TIMEFRAME_MS[timeframe] > DAY_MS or TIMEFRAME_MS[timeframe] % MIN_MS:
        raise ValueError(f"压力演练不支持周期 {timeframe}（支持 1 分钟到 1 天）")
    if initial_cash <= 0:
        raise ValueError("初始资金必须为正")
    ids = scenarios or [s["id"] for s in SCENARIOS]
    chosen = []
    for sid in ids:
        sc = scenario_by_id(sid)
        if sc is None:
            raise ValueError(f"未知场景：{sid}；可选：{', '.join(s['id'] for s in SCENARIOS)}")
        chosen.append(sc)
    plan = FaultPlan.from_dict(faults)
    if isinstance(cost, dict):
        cost = RuntimeCost(**{k: v for k, v in cost.items() if k in RuntimeCost.__dataclass_fields__})
    if isinstance(risk, dict):
        risk = RiskConfig.from_dict(risk)
    secs = _securities(source, securities)
    # 先加载一次：策略本身有问题就早点报，不要跑到一半
    from ..strategies.lang import load

    load(lang, source, name="stress", params=params or None)
    market = _Market(services_or_market, exchange)
    t0 = time.monotonic()
    reports = []
    total = len(chosen)
    for k, sc in enumerate(chosen):

        def tick(frac: float, k: int = k, sc: dict[str, Any] = sc) -> None:
            if progress:
                progress(
                    {
                        "scenario": sc["id"],
                        "name": sc["name"],
                        "index": k,
                        "total": total,
                        "fraction": round((k + frac) / total, 4),
                        "message": f"{sc['name']}：回放中 {frac:.0%}",
                    }
                )

        tick(0.0)
        reports.append(
            run_scenario(
                market,
                sc,
                source=source,
                params=params,
                securities=secs,
                timeframe=timeframe,
                initial_cash=float(initial_cash),
                plan=plan,
                lang=lang,
                cost=cost,
                risk=risk,
                drawdown_threshold=drawdown_threshold,
                progress=tick,
                preloaded=(preloaded or {}).get(sc["id"]),
            )
        )
        if progress:
            r = reports[-1]
            progress(
                {
                    "scenario": sc["id"],
                    "name": sc["name"],
                    "index": k,
                    "total": total,
                    "fraction": round((k + 1) / total, 4),
                    "message": f"{sc['name']}：{r['verdict']}",
                }
            )
    completed = all(r.get("completed") for r in reports)
    flags = []
    for r in reports:
        if not r.get("completed"):
            flags.append({"scenario": r["id"], "kind": "crashed", "message": r.get("error") or ""})
            continue
        if r["drawdown_breach"]:
            msg = f"{r['name']}：最大回撤 {r['max_drawdown']:.1%} 超过阈值 {drawdown_threshold:.0%}"
            flags.append({"scenario": r["id"], "kind": "drawdown", "message": msg})
        if r["liquidations"]:
            flags.append(
                {"scenario": r["id"], "kind": "liquidation", "message": f"{r['name']}：被强平 {r['liquidations']} 次"}
            )
        if r["strategy_errors"]:
            flags.append(
                {
                    "scenario": r["id"],
                    "kind": "strategy_error",
                    "message": f"{r['name']}：策略报错 {r['strategy_errors']} 次",
                }
            )
        if r["data_source"] == "synthetic":
            flags.append(
                {
                    "scenario": r["id"],
                    "kind": "synthetic_data",
                    "message": f"{r['name']}：使用合成行情，结论只针对执行链路",
                }
            )
    done = [r for r in reports if r.get("completed")]
    idle = [r for r in done if not r.get("fills")]
    for r in idle:
        flags.append({"scenario": r["id"], "kind": "no_trades", "message": f"{r['name']}：场景内没有成交，结论无效"})
    summary = {
        "passed": completed and len(idle) < len(done),
        "inconclusive": len(idle),
        "completed": sum(1 for r in reports if r.get("completed")),
        "total": total,
        "worst_drawdown": max((r["max_drawdown"] for r in done), default=0.0),
        "worst_return": min((r["return_pct"] for r in done), default=0.0),
        "liquidations": sum(r["liquidations"] for r in done),
        "drawdown_threshold": drawdown_threshold,
        "drawdown_breaches": sum(1 for r in done if r["drawdown_breach"]),
        "data_sources": sorted({r.get("data_source") or "" for r in reports} - {""}),
        "flags": flags,
        "elapsed_s": round(time.monotonic() - t0, 3),
    }
    if not completed:
        summary["verdict"] = f"未通过：{total - summary['completed']} 个场景引擎异常中断"
    elif done and len(idle) == len(done):
        summary["verdict"] = "无法判断：所有场景都没有成交，策略在极端行情里没有持仓，不能当成抗压成功"
    elif summary["liquidations"]:
        summary["verdict"] = f"通过（全部场景跑完），但被强平 {summary['liquidations']} 次"
    elif summary["drawdown_breaches"]:
        summary["verdict"] = f"通过（全部场景跑完），{summary['drawdown_breaches']} 个场景回撤超过阈值"
    elif idle:
        summary["verdict"] = f"通过（全部场景跑完，回撤在阈值内），但 {len(idle)} 个场景没有成交、那几个场景的结论无效"
    else:
        summary["verdict"] = "通过：全部场景跑完，回撤在阈值内"
    return {
        "summary": summary,
        "scenarios": reports,
        "config": {
            "securities": secs,
            "timeframe": timeframe,
            "initial_cash": float(initial_cash),
            "faults": plan.to_dict(),
            "lang": lang,
            "exchange": exchange,
            "cost": (cost or RuntimeCost()).to_dict(),
        },
        "generated_at": int(time.time() * 1000),
    }


__all__ = [
    "SCENARIOS",
    "FaultPlan",
    "FaultyExchange",
    "load_scenario_data",
    "plan_disconnects",
    "run_scenario",
    "run_stress",
    "scenario_by_id",
    "scenario_window",
    "synthetic_crash_minutes",
]


# ---------------------------------------------------------------------------- 隔离运行
def strategy_securities(source: str, securities: list[str] | None) -> list[str]:
    return _securities(source, securities)


def prepare_data(
    services_or_market: Any,
    *,
    source: str,
    securities: list[str] | None,
    timeframe: str,
    scenarios: list[str] | None = None,
    exchange: str = "binance",
) -> dict[str, dict[str, Any]]:
    """在有网络的进程里先把各场景的行情取好（含合成兜底），再交给隔离子进程跑策略。返回可 JSON 化的数据。"""
    from ..platform.sandbox_exec import bars_to_wire

    market = _Market(services_or_market, exchange)
    secs = _securities(source, securities)
    out: dict[str, dict[str, Any]] = {}
    for sid in scenarios or [s["id"] for s in SCENARIOS]:
        sc = scenario_by_id(sid)
        if sc is None:
            raise ValueError(f"未知场景：{sid}")
        d = load_scenario_data(market, sc, secs, timeframe)
        out[sid] = {
            **d,
            "minutes": bars_to_wire(d["minutes"]),
            "warmup": bars_to_wire(d["warmup"]),
            "funding": {k: [[int(t), str(r)] for t, r in v] for k, v in d["funding"].items()},
        }
    return out


def data_from_wire(wire: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from ..platform.sandbox_exec import bars_from_wire

    return {
        sid: {
            **d,
            "minutes": bars_from_wire(d["minutes"]),
            "warmup": bars_from_wire(d["warmup"]),
            "funding": {k: [(int(t), str(r)) for t, r in v] for k, v in d["funding"].items()},
        }
        for sid, d in wire.items()
    }
