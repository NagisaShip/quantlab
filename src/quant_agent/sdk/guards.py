"""组合保护与动态标的池：开新仓之前的两道闸。

两者都对标 Freqtrade（Protections / Pairlists），口径沿用本平台：
- **只看已经发生的事**。保护规则只数已经平仓的交易；标的池只用截至当前已收盘的 K 线和已发布的公告。
  回测时标的池按当时能看到的数据重建，不会把后来才下架、后来才上市的币提前算进去（避免幸存者偏差）。
- **只拦开仓，不拦减仓**。被锁住的标的、被移出池子的标的，已有持仓照常止损、止盈、平仓。
- 回测、纸面模拟、交易所模拟盘共用这一份逻辑，所以三种模式的锁定时刻一致。

保护规则（策略里写 `PROTECTIONS = [...]`，或在 initialize 里 `set_protections([...])`）：

    {"method": "CooldownPeriod", "stop_bars": 2}
        某标的平仓后冷却 stop_bars 根 bar 才能再开。
    {"method": "StoplossGuard", "lookback_bars": 24, "trade_limit": 3, "stop_bars": 12, "only_per_pair": False}
        lookback 内止损出场达到 trade_limit 笔，暂停开仓 stop_bars 根（only_per_pair=True 只锁那个标的）。
    {"method": "MaxDrawdown", "lookback_bars": 48, "trade_limit": 3, "max_allowed_drawdown": 0.15, "stop_bars": 24}
        lookback 内已平仓交易累计出的回撤超过上限，全部暂停开仓。
    {"method": "LowProfitPairs", "lookback_bars": 72, "trade_limit": 2, "required_profit": 0.0, "stop_bars": 48}
        某标的 lookback 内的交易收益合计低于 required_profit，锁住这个标的。

动态标的池（`PAIRLIST = {...}` 或 `set_pairlist({...})`）：

    {"candidates": [...],          # 候选，默认是 set_universe 的标的
     "refresh_bars": 24,           # 每隔几根 bar 重选一次
     "filters": [
        {"method": "AgeFilter", "min_bars": 720},                       # 上市（有数据）不足的不选
        {"method": "PriceFilter", "min_price": 0.01},
        {"method": "VolatilityFilter", "lookback_bars": 72, "min": 0.002, "max": 0.05},  # 单根收益率标准差
        {"method": "RangeFilter", "lookback_bars": 24, "max": 0.08},    # 平均振幅（价差的近似）
        {"method": "VolumeFilter", "lookback_bars": 24, "top": 5, "min_quote_volume": 0},  # 按成交额取前几名
     ],
     "delist_exit": False}         # 下架公告发布后是否立刻平掉持仓（默认只禁止开新仓）

下架公告由平台从交易所公告里识别（research/events.py），一到就从标的池移除，不等下一次重选。
"""

from __future__ import annotations

import math
from typing import Any

PROTECTIONS = ("CooldownPeriod", "StoplossGuard", "MaxDrawdown", "LowProfitPairs")
FILTERS = ("AgeFilter", "PriceFilter", "VolatilityFilter", "RangeFilter", "VolumeFilter")
STOP_EXITS = ("stop_loss", "trailing_stop", "custom_stop", "exchange_stop", "liquidate", "liquidation")
GLOBAL = "*"


def _int(d: dict[str, Any], k: str, default: int) -> int:
    v = int(d.get(k, default))
    if v < 0:
        raise ValueError(f"{d.get('method')}.{k} 不能为负")
    return v


def normalize_protections(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in items or []:
        if not isinstance(raw, dict) or raw.get("method") not in PROTECTIONS:
            raise ValueError(f"保护规则只能是 {'、'.join(PROTECTIONS)}，收到 {raw!r}")
        p = dict(raw)
        p["stop_bars"] = _int(p, "stop_bars", 1)
        if p["stop_bars"] < 1:
            raise ValueError(f"{p['method']}.stop_bars 至少为 1")
        if p["method"] != "CooldownPeriod":
            p["lookback_bars"] = max(1, _int(p, "lookback_bars", 24))
            p["trade_limit"] = max(1, _int(p, "trade_limit", 1 if p["method"] == "MaxDrawdown" else 2))
        if p["method"] == "MaxDrawdown":
            dd = float(p.get("max_allowed_drawdown", 0.2))
            if not 0 < dd < 1:
                raise ValueError("MaxDrawdown.max_allowed_drawdown 应在 (0, 1) 之间")
            p["max_allowed_drawdown"] = dd
        if p["method"] in ("LowProfitPairs", "StoplossGuard"):
            p["required_profit"] = float(p.get("required_profit", 0.0))
        p["only_per_pair"] = bool(p.get("only_per_pair", p["method"] in ("CooldownPeriod", "LowProfitPairs")))
        out.append(p)
    return out


def normalize_pairlist(cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    if not cfg:
        return None
    filters = []
    for f in cfg.get("filters") or []:
        if not isinstance(f, dict) or f.get("method") not in FILTERS:
            raise ValueError(f"标的池过滤器只能是 {'、'.join(FILTERS)}，收到 {f!r}")
        filters.append(dict(f))
    return {
        "candidates": [str(s) for s in cfg.get("candidates") or []],
        "refresh_bars": max(1, int(cfg.get("refresh_bars", 24))),
        "filters": filters,
        "delist_exit": bool(cfg.get("delist_exit", False)),
    }


def base_coin(security: str) -> str:
    return security.split("/")[0].upper()


class Guards:
    """挂在运行时上：保护规则的锁、动态标的池、下架名单。状态可快照。"""

    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.protections: list[dict[str, Any]] = []
        self.pairlist: dict[str, Any] | None = None
        self.delisted: dict[str, int] = {}  # 币 → 公告可见时刻（到了这个时刻才生效）
        self.wall = 0  # 实时运行时平台传入的当前时间
        self.seen: set[str] = set()  # 已经处理过的下架币
        self.universe: list[str] | None = None  # None：还没选过（或没配标的池）
        self.universe_ts = 0
        self.history: list[dict[str, Any]] = []  # 每次重选：时刻、选中、被筛掉的原因
        self.blocked: dict[str, int] = {}  # 被拦下的开仓：原因码 → 次数
        self._cache: tuple[int, list[dict[str, Any]]] | None = None

    # ------------------------------------------------------------------ 配置
    @property
    def active(self) -> bool:
        return bool(self.protections or self.pairlist or self.delisted)

    def set_protections(self, items: list[dict[str, Any]] | None) -> None:
        self.protections = normalize_protections(items)
        self._cache = None

    def set_pairlist(self, cfg: dict[str, Any] | None) -> None:
        self.pairlist = normalize_pairlist(cfg)
        self.universe = None

    def set_delistings(self, rows: list[dict[str, Any]] | None, wall: int | None = None) -> list[str]:
        """rows: [{"coin": "ABC", "available_at": ms}]。返回这次新生效的下架币。

        回测时一次给全，按公告可见时刻逐步生效；实时运行时 wall 传当前时间，已发布的立刻生效，
        不必等下一根 bar 收盘。
        """
        if wall:
            self.wall = max(self.wall, int(wall))
        for r in rows or []:
            coin = str(r.get("coin") or "").upper()
            at = int(r.get("available_at") or r.get("ts") or 0)
            if coin:
                self.delisted[coin] = min(at, self.delisted.get(coin, at))
        return sorted(self.delisted_now() - self.seen)

    def poll(self) -> list[str]:
        """新生效的下架币：从当前标的池里拿掉并记一笔。每根 bar 和每次收到公告时调用。"""
        new = sorted(self.delisted_now() - self.seen)
        if not new:
            return []
        self.seen.update(new)
        if self.universe is not None:
            gone = [s for s in self.universe if base_coin(s) in new]
            if gone:
                self.universe = [s for s in self.universe if s not in gone]
                self.history.append(
                    {
                        "ts": self.clock(),
                        "selected": list(self.universe),
                        "dropped": {s: "下架公告" for s in gone},
                        "reason": "delisting",
                    }
                )
        return new

    def clock(self) -> int:
        return max(int(self.rt.now or 0), self.wall)

    def delisted_now(self) -> set[str]:
        t = self.clock()
        return {c for c, at in self.delisted.items() if at <= t}

    def is_delisted(self, coin: str) -> bool:
        at = self.delisted.get(coin.upper())
        return at is not None and at <= self.clock()

    # ------------------------------------------------------------------ 保护规则
    def _bar_ms(self) -> int:
        secs = self.rt.securities()
        return self.rt.bar_ms(secs[0]) if secs else 3_600_000

    def locks(self) -> list[dict[str, Any]]:
        """由已平仓交易推出的全部锁（只依赖成交历史，回测与模拟盘一致）。按交易笔数缓存。"""
        trips = [t for t in self.rt.round_trips if t.close_ts]
        if self._cache and self._cache[0] == len(trips):
            return self._cache[1]
        bar = self._bar_ms()
        out: list[dict[str, Any]] = []
        initial = float(getattr(self.rt, "initial_cash", 0) or 0) or 1.0
        for p in self.protections:
            stop = p["stop_bars"] * bar
            for i, t in enumerate(trips):
                now = t.close_ts
                if p["method"] == "CooldownPeriod":
                    out.append(self._lock(p, t.security, now, now + stop, "平仓后冷却"))
                    continue
                window = [x for x in trips[: i + 1] if x.close_ts > now - p["lookback_bars"] * bar]
                if p["method"] == "StoplossGuard":
                    pool = [x for x in window if (not p["only_per_pair"] or x.security == t.security)]
                    hits = [x for x in pool if x.exit_reason in STOP_EXITS and x.ret < p["required_profit"]]
                    if t.exit_reason in STOP_EXITS and len(hits) >= p["trade_limit"]:
                        who = t.security if p["only_per_pair"] else GLOBAL
                        out.append(self._lock(p, who, now, now + stop, f"{len(hits)} 笔止损出场"))
                elif p["method"] == "LowProfitPairs":
                    pool = [x for x in window if x.security == t.security]
                    total = sum(x.ret for x in pool)
                    if len(pool) >= p["trade_limit"] and total < p["required_profit"]:
                        out.append(self._lock(p, t.security, now, now + stop, f"{len(pool)} 笔合计 {total:.2%}"))
                elif p["method"] == "MaxDrawdown" and len(window) >= p["trade_limit"]:
                    before = sum(float(x.realized) for x in trips if x.close_ts <= now - p["lookback_bars"] * bar)
                    base = initial + before
                    eq = peak = base
                    dd = 0.0
                    for x in window:
                        eq += float(x.realized)
                        peak = max(peak, eq)
                        dd = max(dd, (peak - eq) / peak if peak > 0 else 0.0)
                    if dd > p["max_allowed_drawdown"]:
                        out.append(self._lock(p, GLOBAL, now, now + stop, f"已平仓交易回撤 {dd:.1%}"))
        out.sort(key=lambda r: (r["from"], r["method"]))
        self._cache = (len(trips), out)
        return out

    @staticmethod
    def _lock(p: dict[str, Any], who: str, start: int, until: int, why: str) -> dict[str, Any]:
        return {"method": p["method"], "security": who, "from": start, "until": until, "reason": why}

    def active_lock(self, security: str) -> dict[str, Any] | None:
        now = self.rt.now
        for lk in self.locks():
            if lk["from"] <= now < lk["until"] and lk["security"] in (GLOBAL, security):
                return lk
        return None

    # ------------------------------------------------------------------ 标的池
    def candidates(self) -> list[str]:
        base = (self.pairlist or {}).get("candidates") or self.rt.securities()
        return list(base)

    def refresh(self, force: bool = False) -> list[str]:
        """按当前时刻能看到的数据重选。返回当前池子。"""
        if not self.pairlist:
            return [s for s in self.candidates() if not self.is_delisted(base_coin(s))]
        now = self.rt.now
        due = force or self.universe is None or now - self.universe_ts >= self.pairlist["refresh_bars"] * self._bar_ms()
        if not due:
            return list(self.universe or [])
        selected, dropped = self._select()
        self.universe, self.universe_ts = selected, now
        self.history.append({"ts": now, "selected": list(selected), "dropped": dropped, "reason": "refresh"})
        del self.history[:-200]
        return list(selected)

    def _select(self) -> tuple[list[str], dict[str, str]]:
        dropped: dict[str, str] = {}
        rows: list[tuple[str, list[dict[str, Any]]]] = []
        for s in self.candidates():
            if self.is_delisted(base_coin(s)):
                dropped[s] = "下架公告"
                continue
            need = max(
                [int(f.get("lookback_bars", 24)) for f in self.pairlist["filters"]]
                + [int(f.get("min_bars", 0)) for f in self.pairlist["filters"] if f["method"] == "AgeFilter"]
                + [2]
            )
            bars = self.rt.bars(s, need + 1)
            if not bars:
                dropped[s] = "当前没有行情（未上市或停牌）"
                continue
            rows.append((s, bars))
        volume_rank: list[tuple[float, str]] = []
        for s, bars in rows:
            why = ""
            for f in self.pairlist["filters"]:
                m = f["method"]
                lb = bars[-int(f.get("lookback_bars", 24)) :]
                if m == "AgeFilter" and len(bars) < int(f.get("min_bars", 0)):
                    why = f"上市不足 {f.get('min_bars')} 根 bar"
                elif m == "PriceFilter" and bars[-1]["close"] < float(f.get("min_price", 0)):
                    why = f"价格低于 {f.get('min_price')}"
                elif m == "VolatilityFilter":
                    rets = [
                        math.log(b["close"] / a["close"])
                        for a, b in zip(lb, lb[1:], strict=False)
                        if a["close"] > 0 and b["close"] > 0
                    ]
                    sd = _std(rets)
                    if sd < float(f.get("min", 0)) or sd > float(f.get("max", 1e9)):
                        why = f"波动率 {sd:.4f} 不在区间内"
                elif m == "RangeFilter":
                    rg = sum((b["high"] - b["low"]) / b["close"] for b in lb if b["close"] > 0) / max(1, len(lb))
                    if rg > float(f.get("max", 1e9)):
                        why = f"平均振幅 {rg:.2%} 太大"
                elif m == "VolumeFilter":
                    qv = sum(b["close"] * b["volume"] for b in lb)
                    if qv < float(f.get("min_quote_volume", 0)):
                        why = "成交额不足"
                if why:
                    break
            if why:
                dropped[s] = why
                continue
            qv = sum(b["close"] * b["volume"] for b in bars[-24:])
            volume_rank.append((qv, s))
        keep = [s for _, s in volume_rank]
        vf = next((f for f in self.pairlist["filters"] if f["method"] == "VolumeFilter" and f.get("top")), None)
        if vf:
            ranked = [s for _, s in sorted(volume_rank, key=lambda x: -x[0])]
            top = int(vf["top"])
            for s in ranked[top:]:
                dropped[s] = f"成交额不在前 {top}"
            keep = ranked[:top]
        else:
            keep = [s for s in self.candidates() if s in keep]
        return keep, dropped

    def in_universe(self, security: str) -> bool:
        if self.is_delisted(base_coin(security)):
            return False
        if not self.pairlist:
            return True
        return security in self.refresh()

    # ------------------------------------------------------------------ 开仓闸门
    def entry_block(self, security: str) -> tuple[str, str] | None:
        """返回 (原因码, 说明)；None 表示可以开。"""
        coin = base_coin(security)
        if self.is_delisted(coin):
            return "DELISTED", f"{coin} 已发布下架公告，不再开新仓"
        if self.pairlist and not self.in_universe(security):
            return "NOT_IN_UNIVERSE", f"{security} 不在当前标的池里"
        lk = self.active_lock(security) if self.protections else None
        if lk:
            who = "全部标的" if lk["security"] == GLOBAL else lk["security"]
            return "PROTECTION_LOCK", f"{lk['method']} 锁住了{who}（{lk['reason']}），到期前不开新仓"
        return None

    def note_block(self, code: str) -> None:
        self.blocked[code] = self.blocked.get(code, 0) + 1

    # ------------------------------------------------------------------ 快照与报告
    def snapshot(self) -> dict[str, Any]:
        return {
            "delisted": dict(self.delisted),
            "wall": self.wall,
            "seen": sorted(self.seen),
            "universe": self.universe,
            "universe_ts": self.universe_ts,
            "history": self.history[-50:],
            "blocked": dict(self.blocked),
        }

    def restore(self, snap: dict[str, Any] | None) -> None:
        snap = snap or {}
        self.delisted = {k: int(v) for k, v in (snap.get("delisted") or {}).items()}
        self.wall = int(snap.get("wall") or 0)
        self.seen = set(snap.get("seen") or [])
        self.universe = snap.get("universe")
        self.universe_ts = int(snap.get("universe_ts") or 0)
        self.history = list(snap.get("history") or [])
        self.blocked = dict(snap.get("blocked") or {})

    def report(self) -> dict[str, Any] | None:
        if not self.active:
            return None
        return {
            "protections": self.protections,
            "locks": self.locks()[-200:] if self.protections else [],
            "blocked": dict(self.blocked),
            "pairlist": self.pairlist,
            "universe": self.universe if self.pairlist else None,
            "universe_history": self.history[-100:],
            "delisted": {c: at for c, at in self.delisted.items() if at <= self.clock()},
        }


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
