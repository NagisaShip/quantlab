"""信息源中心：外部数据的连接器与「时点」存储。

每条记录同时保存三样东西：
- ``ts``：事件时间（这条数据描述的是哪个时刻）；
- ``available_at``：平台最早能拿到它的时间（结算时刻、统计周期结束、次日发布……）；
- ``value``：数值。

回测与因子评估只按 ``available_at`` 做 as-of 对齐——一根 K 线收盘时只能看到在那之前已经发布的数据，
这是防止未来数据泄漏的关键。连接器按数据源保守地设置发布延迟。

数据集键：``<字段>:<标的或 *>:<来源>``，例如 ``funding:BTC/USDT:binance``、``fng:*:alternative_me``。
没有网络时，因子研究可以退回**明确标注的合成数据**（与行情服务的口径一致），合成数据的结论不能作为研究结论。
"""

from __future__ import annotations

import csv
import io
import math
import threading
import time
import zlib
from collections.abc import Callable
from typing import Any

import httpx
import numpy as np

CATALOG = "datahub_datasets"
BLOB = "datahub"
DAY = 86_400_000
HOUR = 3_600_000

# 字段 → (标签, 作用范围)。作用范围 symbol = 每个标的一条序列；market = 全市场一条
FIELD_SCOPE = {
    "funding": "symbol",
    "oi": "symbol",
    "ls_ratio": "symbol",
    "taker_ratio": "symbol",
    "basis": "symbol",
    "fng": "market",
    "stablecoin": "market",
    "tvl": "market",
    "liq_long": "symbol",
    "liq_short": "symbol",
    "dvol": "symbol",
    "iv_skew": "symbol",
    "ev_listing": "symbol",
    "ev_delisting": "symbol",
    "ev_maintenance": "symbol",
    "ev_news": "symbol",
    "ev_macro": "market",
}

# FRED 常用序列与发布滞后：月度数据通常在下个月中旬才公布，按日频处理会提前看到数据
FRED_LAG_DAYS = {
    "DGS10": 1,
    "DGS2": 1,
    "DFF": 1,
    "DTWEXBGS": 1,
    "T10Y2Y": 1,
    "VIXCLS": 1,
    "SP500": 1,
    "WALCL": 2,
    "RRPONTSYD": 1,
    "CPIAUCSL": 45,
    "CPILFESL": 45,
    "PCEPI": 60,
    "UNRATE": 37,
    "PAYEMS": 37,
    "M2SL": 30,
    "FEDFUNDS": 32,
}

CONNECTORS: list[dict[str, Any]] = [
    {
        "id": "binance_futures",
        "name": "币安合约公开数据",
        "category": "衍生品",
        "needs_key": False,
        "fields": ["funding", "oi", "ls_ratio", "taker_ratio", "basis"],
        "scope": "symbol",
        "note": "资金费率与永续溢价可取较长历史；持仓量、多空比、主动买卖比交易所只提供最近 30 天，建议定期同步积累",
        "url": "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api",
    },
    {
        "id": "okx",
        "name": "OKX 公开数据",
        "category": "衍生品",
        "needs_key": False,
        "fields": ["funding"],
        "scope": "symbol",
        "note": "资金费率历史（约最近 3 个月）；币安不可用时的备选",
        "url": "https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate-history",
    },
    {
        "id": "defillama",
        "name": "DefiLlama",
        "category": "链上",
        "needs_key": False,
        "fields": ["stablecoin", "tvl"],
        "scope": "market",
        "note": "稳定币总市值与 DeFi 总锁仓，日频，按次日可用处理",
        "url": "https://defillama.com/docs/api",
    },
    {
        "id": "alternative_me",
        "name": "恐惧贪婪指数",
        "category": "情绪",
        "needs_key": False,
        "fields": ["fng"],
        "scope": "market",
        "note": "alternative.me 每日发布，按次日可用处理",
        "url": "https://alternative.me/crypto/fear-and-greed-index/",
    },
    {
        "id": "announcements",
        "name": "交易所公告",
        "category": "事件",
        "needs_key": False,
        "fields": ["ev_listing", "ev_delisting", "ev_maintenance"],
        "scope": "events",
        "note": "Binance、OKX 的上币、下架、维护公告；按发布时间 + 5 分钟可用，物化成按标的的衰减强度",
        "url": "https://www.binance.com/en/support/announcement",
    },
    {
        "id": "news",
        "name": "新闻与情绪",
        "category": "事件",
        "needs_key": False,
        "key_optional": True,
        "fields": ["ev_news"],
        "scope": "events",
        "note": "CoinDesk、Cointelegraph 公开 RSS；填 CryptoPanic 密钥可多一路聚合新闻。用大模型标注类型、"
        "币种、方向和强度（没配模型时用规则词典），标注器记进血缘",
        "url": "https://cryptopanic.com/developers/api/",
    },
    {
        "id": "liquidations",
        "name": "强平流",
        "category": "衍生品",
        "needs_key": False,
        "fields": ["liq_long", "liq_short"],
        "scope": "stream",
        "note": (
            "订阅币安全市场强平推送（forceOrder），按小时汇总多头 / 空头被强平的美元金额；交易所不给历史，"
            "从开始记录起积累"
        ),
        "url": "https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Liquidation-Order-Streams",
    },
    {
        "id": "deribit",
        "name": "Deribit 期权波动率",
        "category": "衍生品",
        "needs_key": False,
        "fields": ["dvol", "iv_skew"],
        "scope": "symbol",
        "note": (
            "DVOL 隐含波动率指数（BTC、ETH，小时级历史）与约 30 天期限的看跌 − 看涨偏度（只有当前快照，定期同步积累）"
        ),
        "url": "https://docs.deribit.com/#public-get_volatility_index_data",
    },
    {
        "id": "macro_calendar",
        "name": "经济日历",
        "category": "宏观",
        "needs_key": False,
        "fields": ["ev_macro"],
        "scope": "events",
        "note": "本周美国高 / 中重要性数据与议息日程（CPI、非农、FOMC…），日程提前公布，按抓到时可用",
        "url": "https://www.forexfactory.com/calendar",
    },
    {
        "id": "fred",
        "name": "FRED 宏观数据",
        "category": "宏观",
        "needs_key": True,
        "fields": [],
        "scope": "market",
        "note": "美债利率、美元指数、CPI 等；填序列号（如 DGS10、CPIAUCSL），在因子里用 x_<名字> 引用；"
        "月度数据按实际发布滞后（CPI 约 45 天）处理；免费申请密钥",
        "url": "https://fred.stlouisfed.org/docs/api/api_key.html",
    },
    {
        "id": "glassnode",
        "name": "Glassnode 链上指标",
        "category": "链上",
        "needs_key": True,
        "fields": [],
        "scope": "symbol",
        "note": "交易所净流入、活跃地址等；填指标路径（如 transactions/transfers_volume_exchanges_net）",
        "url": "https://docs.glassnode.com/basic-api/endpoints",
    },
    {
        "id": "csv",
        "name": "自定义 CSV",
        "category": "自定义",
        "needs_key": False,
        "fields": [],
        "scope": "any",
        "note": "上传 ts,value（可选 available_at）三列；在因子里用 x_<名字> 引用",
        "url": "",
    },
]
CONNECTOR_BY_ID = {c["id"]: c for c in CONNECTORS}


def _now() -> int:
    return int(time.time() * 1000)


def _ms(t: Any) -> int:
    """存储层会把 updated_at 改写成秒；统一成毫秒。"""
    t = float(t or 0)
    return int(t * 1000) if 0 < t < 1e12 else int(t)


def ds_key(field: str, symbol: str | None, source: str) -> str:
    return f"{field}:{symbol or '*'}:{source}"


def _plain(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").upper()


class DataHub:
    """数据集目录 + 时点序列存取 + 连接器同步。

    store 是工作区的 JsonStore（目录在 collection，序列在 blob）；secrets_fn 返回已登记的密钥。
    """

    def __init__(
        self,
        store: Any,
        *,
        secrets_fn: Callable[[], dict[str, str]] | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
        llm: Callable[[str, str], Any] | None = None,
    ) -> None:
        self.store = store
        self.secrets_fn = secrets_fn or (lambda: {})
        self.transport = transport
        self.timeout = timeout
        self.llm = llm

    # ------------------------------------------------------------------ 目录
    def datasets(self) -> list[dict[str, Any]]:
        rows = self.store.list(CATALOG)
        return sorted(rows, key=lambda r: (r.get("field", ""), r.get("symbol", ""), r.get("source", "")))

    def dataset(self, key: str) -> dict[str, Any] | None:
        return self.store.get(CATALOG, key)

    def series(self, key: str) -> list[list[float]]:
        blob = self.store.get_blob(BLOB, key.replace("/", "_").replace(":", "~")) or {}
        return blob.get("rows") or []

    def delete(self, key: str) -> bool:
        self.store.delete_blob(BLOB, key.replace("/", "_").replace(":", "~"))
        return self.store.delete(CATALOG, key)

    def put_series(
        self,
        *,
        field: str,
        symbol: str | None,
        source: str,
        rows: list[tuple[int, int, float]],
        meta: dict[str, Any] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        """写入（并与已有数据合并）一条时点序列。rows: (ts, available_at, value)。"""
        key = ds_key(field, symbol, source)
        old = {int(r[0]): r for r in self.series(key)} if merge else {}
        for ts, avail, val in rows:
            if val is None or (isinstance(val, float) and not math.isfinite(val)):
                continue
            old[int(ts)] = [int(ts), int(max(avail, ts)), float(val)]
        merged = sorted(old.values(), key=lambda r: r[0])
        lags = sorted(r[1] - r[0] for r in merged[-500:])
        self.store.put_blob(BLOB, key.replace("/", "_").replace(":", "~"), {"rows": merged})
        rec = {
            "id": key,
            "key": key,
            "field": field,
            "symbol": symbol or "*",
            "source": source,
            "rows": len(merged),
            "start": merged[0][0] if merged else None,
            "end": merged[-1][0] if merged else None,
            "synthetic": source == "synthetic",
            "updated_at": _now(),
            "synced_at": _now(),
            "status": "ok",
            "error": "",
            "lag_ms": lags[len(lags) // 2] if lags else 0,
            "last_available_at": max((r[1] for r in merged), default=None),
            **(meta or {}),
        }
        prev = self.dataset(key) or {}
        rec["created_at"] = prev.get("created_at") or rec["updated_at"]
        self.store.upsert(CATALOG, rec)
        return rec

    def _mark(self, key: str, **changes: Any) -> None:
        rec = self.dataset(key)
        if rec:
            rec.update(changes)
            self.store.upsert(CATALOG, rec)

    def find(self, field: str, symbol: str | None, *, allow_synthetic: bool = True) -> dict[str, Any] | None:
        """挑一个数据集：真实来源优先，同来源取行数多的。全市场字段忽略 symbol。"""
        want = "*" if FIELD_SCOPE.get(field) == "market" else (symbol or "*")
        cands = [
            d for d in self.datasets() if d.get("field") == field and d.get("symbol") in (want, "*") and d.get("rows")
        ]
        if not allow_synthetic:
            cands = [d for d in cands if not d.get("synthetic")]
        if not cands:
            return None
        cands.sort(key=lambda d: (bool(d.get("synthetic")), d.get("symbol") == "*", -(d.get("rows") or 0)))
        return cands[0]

    def asof(self, key: str, times: np.ndarray) -> np.ndarray:
        """按 available_at ≤ t 对齐：返回每个时刻能看到的最新值。"""
        rows = self.series(key)
        out = np.full(len(times), np.nan)
        if not rows:
            return out
        arr = np.array(rows, dtype=float)
        arr = arr[np.argsort(arr[:, 1], kind="mergesort")]
        idx = np.searchsorted(arr[:, 1], times, side="right") - 1
        ok = idx >= 0
        out[ok] = arr[idx[ok], 2]
        return out

    # ------------------------------------------------------------------ 同步
    def _client(self, base: str) -> httpx.Client:
        return httpx.Client(
            base_url=base,
            timeout=self.timeout,
            transport=self.transport,
            headers={"User-Agent": "QuantLab-DataHub/1.0"},
        )

    def sync(
        self,
        connector: str,
        *,
        symbols: list[str] | None = None,
        start: int | None = None,
        end: int | None = None,
        period: str = "1h",
        series: str = "",
        name: str = "",
    ) -> dict[str, Any]:
        """同步一个连接器。返回 {ok, datasets: [...], errors: [...]}。单个数据集失败不影响其它。"""
        spec = CONNECTOR_BY_ID.get(connector)
        if spec is None:
            raise ValueError(f"未知连接器 {connector}")
        end = end or _now()
        start = start or end - 365 * DAY
        out: dict[str, Any] = {"ok": True, "connector": connector, "datasets": [], "errors": []}

        def run(
            field: str,
            symbol: str | None,
            source: str,
            fn: Callable[[], list[tuple[int, int, float]]],
            meta: dict[str, Any] | None = None,
        ) -> None:
            key = ds_key(field, symbol, source)
            if self.dataset(key):
                self._mark(key, status="syncing")
            try:
                rows = fn()
                if not rows:
                    raise RuntimeError("接口没有返回数据")
                rec = self.put_series(
                    field=field, symbol=symbol, source=source, rows=rows, meta={"connector": connector, **(meta or {})}
                )
                out["datasets"].append(rec)
            except Exception as exc:  # 网络、地域限制、交易对不存在……
                msg = f"{type(exc).__name__}: {exc}"[:300]
                out["errors"].append({"key": key, "error": msg})
                if self.dataset(key):
                    self._mark(key, status="error", error=msg)

        syms = [s.upper() for s in (symbols or ["BTC/USDT"])]
        if connector in ("announcements", "news", "macro_calendar"):
            from .events import EventHub

            ev = EventHub(self.store, self, transport=self.transport, llm=self.llm, timeout=self.timeout)
            res = ev.sync(
                connector, key=self.secrets_fn().get(f"datahub:{connector}", "") if connector == "news" else ""
            )
            return {**out, **res}
        if connector == "liquidations":
            raise ValueError("强平流是实时推送：用「开始记录」，不需要同步")
        if connector == "binance_futures":
            for s in syms:
                run("funding", s, "binance", lambda s=s: self._binance_funding(s, start, end))
                run("basis", s, "binance", lambda s=s: self._binance_premium(s, start, end, period))
                for field, path, col in (
                    ("oi", "/futures/data/openInterestHist", "sumOpenInterestValue"),
                    ("ls_ratio", "/futures/data/globalLongShortAccountRatio", "longShortRatio"),
                    ("taker_ratio", "/futures/data/takerlongshortRatio", "buySellRatio"),
                ):
                    run(
                        field,
                        s,
                        "binance",
                        lambda s=s, path=path, col=col: self._binance_stats(s, path, col, start, end, period),
                    )
        elif connector == "okx":
            for s in syms:
                run("funding", s, "okx", lambda s=s: self._okx_funding(s, start, end))
        elif connector == "defillama":
            run("stablecoin", None, "defillama", self._llama_stablecoins)
            run("tvl", None, "defillama", self._llama_tvl)
        elif connector == "alternative_me":
            run("fng", None, "alternative_me", self._fng)
        elif connector == "deribit":
            from .events import deribit_dvol, deribit_skew

            for ccy in sorted({s.split("/")[0] for s in syms} & {"BTC", "ETH"}) or ["BTC"]:
                sym = f"{ccy}/USDT"
                with self._client("https://www.deribit.com") as c:
                    run("dvol", sym, "deribit", lambda c=c, ccy=ccy: deribit_dvol(c, ccy, start, end))
                    run(
                        "iv_skew",
                        sym,
                        "deribit",
                        lambda c=c, ccy=ccy: [r] if (r := deribit_skew(c, ccy, _now())) else [],
                    )
        elif connector == "fred":
            key = self.secrets_fn().get("datahub:fred", "")
            if not key:
                raise ValueError("FRED 需要密钥：在「行情与数据」页填写后再同步")
            sid = (series or "DGS10").upper()
            field = "x_" + (name or sid).lower()
            lag = FRED_LAG_DAYS.get(sid, 1)
            run(field, None, "fred", lambda: self._fred(sid, key, lag), meta={"series": sid, "lag_days": lag})
        elif connector == "glassnode":
            key = self.secrets_fn().get("datahub:glassnode", "")
            if not key:
                raise ValueError("Glassnode 需要密钥：在「行情与数据」页填写后再同步")
            path = series.strip("/") or "addresses/active_count"
            field = "x_" + (name or path.split("/")[-1]).lower()
            for s in syms:
                run(
                    field,
                    s,
                    "glassnode",
                    lambda s=s: self._glassnode(path, s.split("/")[0], key),
                    meta={"series": path},
                )
        else:
            raise ValueError("这个连接器通过上传文件导入")
        out["ok"] = not out["errors"]
        return out

    def sync_async(self, connector: str, **kw: Any) -> None:
        import contextvars

        ctx = contextvars.copy_context()
        threading.Thread(
            target=ctx.run, args=(lambda: self._safe_sync(connector, **kw),), daemon=True, name=f"datahub-{connector}"
        ).start()

    def _safe_sync(self, connector: str, **kw: Any) -> None:
        try:
            self.sync(connector, **kw)
        except Exception:  # 后台同步失败已写进数据集状态
            pass

    # ---- 币安合约 ----
    def _binance_funding(self, symbol: str, start: int, end: int) -> list[tuple[int, int, float]]:
        rows: list[tuple[int, int, float]] = []
        with self._client("https://fapi.binance.com") as c:
            cur = start
            for _ in range(60):
                r = c.get(
                    "/fapi/v1/fundingRate",
                    params={"symbol": _plain(symbol), "startTime": cur, "endTime": end, "limit": 1000},
                )
                r.raise_for_status()
                data = r.json()
                if not data:
                    break
                for d in data:
                    ts = int(d["fundingTime"])
                    rows.append((ts, ts, float(d["fundingRate"])))
                last = int(data[-1]["fundingTime"])
                if len(data) < 1000 or last >= end:
                    break
                cur = last + 1
        return rows

    def _binance_premium(self, symbol: str, start: int, end: int, period: str) -> list[tuple[int, int, float]]:
        rows: list[tuple[int, int, float]] = []
        interval = period if period in ("1h", "4h", "1d") else "1h"
        with self._client("https://fapi.binance.com") as c:
            cur = start
            for _ in range(60):
                r = c.get(
                    "/fapi/v1/premiumIndexKlines",
                    params={
                        "symbol": _plain(symbol),
                        "interval": interval,
                        "startTime": cur,
                        "endTime": end,
                        "limit": 1500,
                    },
                )
                r.raise_for_status()
                data = r.json()
                if not data:
                    break
                for k in data:
                    rows.append((int(k[0]), int(k[6]) + 1, float(k[4])))
                last = int(data[-1][0])
                if len(data) < 1500 or last >= end:
                    break
                cur = last + 1
        return rows

    def _binance_stats(
        self, symbol: str, path: str, col: str, start: int, end: int, period: str
    ) -> list[tuple[int, int, float]]:
        per = period if period in ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d") else "1h"
        per_ms = {
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": HOUR,
            "2h": 2 * HOUR,
            "4h": 4 * HOUR,
            "6h": 6 * HOUR,
            "12h": 12 * HOUR,
            "1d": DAY,
        }[per]
        start = max(start, _now() - 29 * DAY)  # 交易所只保留最近 30 天
        rows: list[tuple[int, int, float]] = []
        with self._client("https://fapi.binance.com") as c:
            cur = start
            for _ in range(40):
                r = c.get(
                    path,
                    params={"symbol": _plain(symbol), "period": per, "startTime": cur, "endTime": end, "limit": 500},
                )
                r.raise_for_status()
                data = r.json()
                if not data:
                    break
                for d in data:
                    ts = int(d["timestamp"])
                    rows.append((ts, ts + per_ms, float(d[col])))
                last = int(data[-1]["timestamp"])
                if len(data) < 500 or last >= end:
                    break
                cur = last + 1
        return rows

    # ---- OKX ----
    def _okx_funding(self, symbol: str, start: int, end: int) -> list[tuple[int, int, float]]:
        base, quote = [*symbol.split("/"), "USDT"][:2]
        inst = f"{base}-{quote}-SWAP"
        rows: list[tuple[int, int, float]] = []
        with self._client("https://www.okx.com") as c:
            after = end
            for _ in range(40):
                r = c.get("/api/v5/public/funding-rate-history", params={"instId": inst, "after": after, "limit": 100})
                r.raise_for_status()
                data = r.json().get("data") or []
                if not data:
                    break
                for d in data:
                    ts = int(d["fundingTime"])
                    rate = d.get("realizedRate") or d.get("fundingRate")
                    if ts >= start and rate not in (None, ""):
                        rows.append((ts, ts, float(rate)))
                oldest = min(int(d["fundingTime"]) for d in data)
                if oldest <= start or len(data) < 100:
                    break
                after = oldest
        return rows

    # ---- DefiLlama / alternative.me ----
    def _llama_stablecoins(self) -> list[tuple[int, int, float]]:
        with self._client("https://stablecoins.llama.fi") as c:
            r = c.get("/stablecoincharts/all")
            r.raise_for_status()
            rows = []
            for d in r.json():
                ts = int(d["date"]) * 1000
                v = (d.get("totalCirculatingUSD") or {}).get("peggedUSD")
                if v is not None:
                    rows.append((ts, ts + DAY, float(v)))
            return rows

    def _llama_tvl(self) -> list[tuple[int, int, float]]:
        with self._client("https://api.llama.fi") as c:
            r = c.get("/v2/historicalChainTvl")
            r.raise_for_status()
            return [(int(d["date"]) * 1000, int(d["date"]) * 1000 + DAY, float(d["tvl"])) for d in r.json()]

    def _fng(self) -> list[tuple[int, int, float]]:
        with self._client("https://api.alternative.me") as c:
            r = c.get("/fng/", params={"limit": 0, "format": "json"})
            r.raise_for_status()
            return [
                (int(d["timestamp"]) * 1000, int(d["timestamp"]) * 1000 + DAY, float(d["value"]))
                for d in r.json().get("data") or []
            ]

    # ---- 需要密钥的 ----
    def _fred(self, series: str, key: str, lag_days: int = 1) -> list[tuple[int, int, float]]:
        from datetime import datetime, timezone

        with self._client("https://api.stlouisfed.org") as c:
            r = c.get("/fred/series/observations", params={"series_id": series, "api_key": key, "file_type": "json"})
            r.raise_for_status()
            rows = []
            for o in r.json().get("observations") or []:
                if o.get("value") in (None, "", "."):
                    continue
                ts = int(datetime.strptime(o["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
                rows.append((ts, ts + lag_days * DAY, float(o["value"])))
            return rows

    def _glassnode(self, path: str, asset: str, key: str) -> list[tuple[int, int, float]]:
        with self._client("https://api.glassnode.com") as c:
            r = c.get(f"/v1/metrics/{path}", params={"a": asset, "i": "24h", "api_key": key})
            r.raise_for_status()
            return [
                (int(d["t"]) * 1000, int(d["t"]) * 1000 + DAY, float(d["v"]))
                for d in r.json()
                if d.get("v") is not None
            ]

    # ------------------------------------------------------------------ CSV
    def import_csv(self, text: str, *, name: str, symbol: str | None = None, lag_ms: int = 0) -> dict[str, Any]:
        """导入 ts,value[,available_at]。ts 可以是毫秒、秒或 YYYY-MM-DD。拒绝以 = + - @ 开头的公式单元格。"""
        from datetime import datetime, timezone

        field = "x_" + "".join(ch for ch in name.lower() if ch.isalnum() or ch == "_")[:30]
        if field == "x_":
            raise ValueError("请给数据起一个英文名字")
        reader = csv.reader(io.StringIO(text))
        rows: list[tuple[int, int, float]] = []
        for n, rec in enumerate(reader):
            if not rec or not rec[0].strip():
                continue
            if any(c.strip()[:1] in ("=", "+", "@") for c in rec):
                raise ValueError(f"第 {n + 1} 行含有公式字符，已拒绝导入")
            head = rec[0].strip()
            try:
                if head.replace(".", "", 1).isdigit():
                    ts = int(float(head))
                    ts = ts * 1000 if ts < 10**11 else ts
                else:
                    ts = int(datetime.fromisoformat(head[:19]).replace(tzinfo=timezone.utc).timestamp() * 1000)
                val = float(rec[1])
            except (ValueError, IndexError):
                if n == 0:
                    continue  # 表头
                raise ValueError(f"第 {n + 1} 行格式不对，应为 ts,value[,available_at]") from None
            avail = ts + lag_ms
            if len(rec) > 2 and rec[2].strip():
                a = int(float(rec[2]))
                avail = a * 1000 if a < 10**11 else a
            rows.append((ts, avail, val))
        if not rows:
            raise ValueError("文件里没有数据行")
        return self.put_series(
            field=field, symbol=symbol, source="csv", rows=rows, merge=False, meta={"connector": "csv", "name": name}
        )

    # ------------------------------------------------------------------ 合成
    def synthetic(self, field: str, times: np.ndarray, close: np.ndarray, symbol: str) -> np.ndarray:
        """没有真实数据时的教学合成序列：由价格路径确定性生成（同一标的同一时间永远相同），并带噪声。"""
        seed = zlib.crc32(f"{field}|{symbol}".encode())
        rng = np.random.default_rng(seed)
        n = len(times)
        with np.errstate(all="ignore"):
            r = np.r_[np.nan, np.diff(np.log(close))]
        r = np.nan_to_num(r)
        mom = np.convolve(r, np.ones(24) / 24, mode="full")[:n]
        noise = rng.normal(0, 1, n)
        if field == "funding":
            return 0.0001 + 0.0004 * np.tanh(mom * 400) + 0.00005 * noise
        if field == "oi":
            return 1e9 * np.exp(np.cumsum(0.6 * r + 0.004 * noise))
        if field == "ls_ratio":
            return 1.0 + 0.4 * np.tanh(mom * 200) + 0.05 * noise
        if field == "taker_ratio":
            return 1.0 + 0.3 * np.tanh(r * 80) + 0.05 * noise
        if field == "basis":
            return 0.0002 * np.tanh(mom * 300) + 0.00005 * noise
        if field == "fng":
            return np.clip(50 + 30 * np.tanh(mom * 150) + 5 * noise, 0, 100)
        if field in ("stablecoin", "tvl"):
            return 1e11 * np.exp(np.cumsum(0.0002 + 0.001 * noise))
        if field in ("liq_long", "liq_short"):
            side = -1 if field == "liq_long" else 1
            return 2e6 * np.exp(np.clip(side * r * 150, -3, 6) + 0.5 * noise)
        if field == "dvol":
            vol = np.sqrt(np.convolve(r**2, np.ones(72) / 72, mode="full")[:n] * 24 * 365) * 100
            return np.clip(vol * 1.1 + 2 * noise, 10, 200)
        if field == "iv_skew":
            return -40 * np.tanh(mom * 200) + 0.5 * noise
        if field.startswith("ev_"):
            shocks = (rng.random(n) < 0.004) * rng.choice([-1.0, 1.0], n) * 0.6
            if field in ("ev_listing", "ev_maintenance", "ev_macro"):
                shocks = np.abs(shocks)
            if field == "ev_delisting":
                shocks = -np.abs(shocks)
            decay = 0.5 ** (1 / 24)
            out = np.zeros(n)
            for i in range(n):
                out[i] = (out[i - 1] * decay if i else 0.0) + shocks[i]
            return out
        return np.full(n, np.nan)


def connectors_view(hub: DataHub, secrets: dict[str, str]) -> list[dict[str, Any]]:
    ds = hub.datasets()
    out = []
    for c in CONNECTORS:
        mine = [d for d in ds if d.get("connector") == c["id"] or (c["id"] == "csv" and d.get("source") == "csv")]
        if c["id"] in ("announcements", "news", "macro_calendar"):
            mine = [d for d in ds if d.get("source") == "events" and d.get("field") in c["fields"]]
        out.append(
            {
                **c,
                "has_key": bool(secrets.get(f"datahub:{c['id']}")) if c["needs_key"] or c.get("key_optional") else None,
                "datasets": len(mine),
                "last_sync": max((_ms(d.get("synced_at") or d.get("updated_at")) for d in mine), default=None),
                "errors": sum(1 for d in mine if d.get("status") == "error"),
            }
        )
    return out


__all__ = ["CONNECTORS", "FIELD_SCOPE", "DataHub", "connectors_view", "ds_key"]
