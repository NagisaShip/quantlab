"""事件类信息源：交易所公告、新闻与情绪、宏观日历、强平流、期权波动率。

和 datahub 的数值序列一样，每条事件都带两个时间：
- ``ts``：事件发生 / 发布的时间；
- ``available_at``：平台最早能拿到它的时间（发布时间 + 抓取延迟）。回测只能用 available_at 之前的事件。

事件先存成一条条记录（集合 ``datahub_events``），再**物化**成因子能直接引用的时点序列：
- ``ev_listing`` / ``ev_delisting`` / ``ev_maintenance`` / ``ev_news``：按标的，每小时一个点，
  值 = 已发布事件的 方向 × 强度，按 24 小时半衰期衰减求和（刚出的事件影响最大，之后逐渐淡去）；
- ``ev_macro``：全市场，未来 24 小时内已公布日程的重要宏观事件（高 = 1，中 = 0.5）。

新闻的结构化标注（类型、涉及币种、方向、强度）优先用大模型，取不到模型时用规则词典。
每条事件记下标注器（模型名、提示词版本与哈希），数据集元信息里也有，研究血缘能追到。
**回看偏差**：大模型给多年前的新闻打标签时，可能"知道"后来发生了什么。标注时间晚于发布时间一天以上的事件
会标 ``hindsight_risk``，数据中心里能看到；这类历史标签只适合做探索，不适合当回测结论。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

EVENTS = "datahub_events"
HOUR = 3_600_000
DAY = 86_400_000
HALF_LIFE_MS = 24 * HOUR
FETCH_LAG_MS = 5 * 60_000  # 抓取 / 推送延迟：按 5 分钟保守处理

EVENT_FIELDS = {
    "ev_listing": "上币公告（衰减强度）",
    "ev_delisting": "下架公告（衰减强度，负值）",
    "ev_maintenance": "维护公告（衰减强度）",
    "ev_news": "新闻情绪（方向 × 强度，衰减求和）",
    "ev_macro": "未来 24 小时重要宏观事件（高 1 / 中 0.5）",
}
TYPE_FIELD = {"listing": "ev_listing", "delisting": "ev_delisting", "maintenance": "ev_maintenance", "news": "ev_news"}

PROMPT_VERSION = "news-tag-v1"
TAG_PROMPT = """你是加密市场的新闻标注员。对下面每条标题输出一个 JSON 对象，组成一个 JSON 数组，不要输出别的文字。
字段：
- i：标题序号（整数）
- type：listing | delisting | maintenance | regulation | hack | macro | partnership | product | flows | market | other
- coins：涉及的币种代码数组（如 ["BTC","ETH"]），与具体币种无关就给 []
- direction：对这些币种价格的方向，1 利好、-1 利空、0 中性
- strength：0 到 1 的影响强度（0.2 小、0.5 中、0.8 大）
只依据标题本身判断，不要用你知道的后续行情。"""
PROMPT_SHA = hashlib.sha256(TAG_PROMPT.encode()).hexdigest()[:16]

COIN_NAMES = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "ether": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "xrp": "XRP",
    "ripple": "XRP",
    "bnb": "BNB",
    "dogecoin": "DOGE",
    "doge": "DOGE",
    "cardano": "ADA",
    "ada": "ADA",
    "toncoin": "TON",
    "avalanche": "AVAX",
    "avax": "AVAX",
    "chainlink": "LINK",
    "link": "LINK",
    "polkadot": "DOT",
    "tron": "TRX",
    "litecoin": "LTC",
    "shiba": "SHIB",
    "pepe": "PEPE",
    "sui": "SUI",
    "arbitrum": "ARB",
    "optimism": "OP",
    "aptos": "APT",
    "near": "NEAR",
    "uniswap": "UNI",
}
STOP = {
    "USDT",
    "USDC",
    "USD",
    "API",
    "BINANCE",
    "OKX",
    "SPOT",
    "FUTURES",
    "UTC",
    "ETF",
    "SEC",
    "CEO",
    "THE",
    "AND",
    "FOR",
    "WILL",
    "NEW",
    "ALL",
    "FDUSD",
    "TRY",
    "EUR",
    "BRL",
    "PERP",
    "VIP",
    "AMA",
    "NFT",
    "APR",
    "APY",
}
POSITIVE = {
    "approve": 0.6,
    "approval": 0.6,
    "approved": 0.6,
    "etf inflow": 0.5,
    "inflows": 0.4,
    "record high": 0.5,
    "all-time high": 0.5,
    "partnership": 0.4,
    "launch": 0.3,
    "upgrade": 0.3,
    "adopt": 0.4,
    "surge": 0.4,
    "rally": 0.4,
    "buys": 0.3,
    "accumulat": 0.3,
    "list": 0.4,
    "integrat": 0.3,
}
NEGATIVE = {
    "hack": 0.8,
    "exploit": 0.8,
    "stolen": 0.7,
    "ban": 0.6,
    "lawsuit": 0.5,
    "sues": 0.5,
    "charged": 0.5,
    "outflow": 0.4,
    "crash": 0.6,
    "plunge": 0.5,
    "delist": 0.7,
    "bankrupt": 0.8,
    "insolven": 0.8,
    "liquidat": 0.4,
    "probe": 0.4,
    "halt": 0.5,
    "sell-off": 0.5,
    "selloff": 0.5,
    "fraud": 0.7,
}


@dataclass
class Event:
    id: str
    source: str  # binance | okx | coindesk | cointelegraph | cryptopanic | forexfactory …
    kind: str  # announcement | news | macro
    type: str  # listing | delisting | maintenance | news | macro | …
    title: str
    ts: int
    available_at: int
    url: str = ""
    coins: list[str] = field(default_factory=list)
    direction: int = 0
    strength: float = 0.0
    tagger: dict[str, Any] = field(default_factory=dict)
    tagged_at: int = 0
    hindsight_risk: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def event_id(source: str, key: str) -> str:
    return hashlib.sha1(f"{source}|{key}".encode()).hexdigest()[:20]


def _now() -> int:
    return int(time.time() * 1000)


def coins_in(text: str) -> list[str]:
    """从标题里找币种：括号里的代码、XXX/USDT、XXXUSDT 交易对，以及常见币名。"""
    found: list[str] = []
    for tok in re.findall(r"\(([A-Z0-9]{2,10})\)", text):
        found.append(tok)
    for tok in re.findall(r"\b([A-Z0-9]{2,10})[/-]?(?:USDT|USDC|FDUSD)\b", text):
        found.append(tok)
    # "Binance Will Delist BAR, BAZ on ..." / "OKX to list ABC"：动词后面逗号分隔的大写代码
    m = re.search(
        r"\b(?:[Dd]e)?[Ll]ist(?:ing)?\s+((?:[A-Z0-9]{2,10}(?:\s*,\s*|\s+and\s+|\s*&\s*))*[A-Z0-9]{2,10})\b", text
    )
    if m:
        found.extend(re.findall(r"[A-Z0-9]{2,10}", m.group(1)))
    low = text.lower()
    for name, code in COIN_NAMES.items():
        if len(name) <= 4:
            if re.search(rf"\b{re.escape(name)}\b", low) and (name.upper() in text or len(name) > 3):
                found.append(code)
        elif name in low:
            found.append(code)
    out: list[str] = []
    for c in found:
        c = c.upper()
        if c not in STOP and c not in out and not c.isdigit():
            out.append(c)
    return out[:8]


def classify_announcement(title: str) -> str:
    t = title.lower()
    if "delist" in t or "will remove" in t or "cease trading" in t or "下架" in title:
        return "delisting"
    if "maintenance" in t or ("upgrade" in t and "network" in t) or "维护" in title or "suspend" in t:
        return "maintenance"
    if (
        "will list" in t
        or " to list" in t
        or re.search(r"\blists?\b", t)
        or "listing" in t
        or "launchpool" in t
        or "new listing" in t
        or "上线" in title
    ):
        return "listing"
    return "other"


ANNOUNCEMENT_TAG = {"listing": (1, 0.6), "delisting": (-1, 0.8), "maintenance": (0, 0.3), "other": (0, 0.1)}


def rule_tag(title: str) -> dict[str, Any]:
    """规则词典标注：没有大模型时的退路，结果偏粗，界面会标出来。"""
    low = title.lower()
    pos = max((w for k, w in POSITIVE.items() if k in low), default=0.0)
    neg = max((w for k, w in NEGATIVE.items() if k in low), default=0.0)
    direction = 1 if pos > neg else -1 if neg > pos else 0
    typ = (
        "hack"
        if any(k in low for k in ("hack", "exploit", "stolen"))
        else "regulation"
        if any(k in low for k in ("sec ", "regulat", "ban", "lawsuit", "sues", "law"))
        else "flows"
        if any(k in low for k in ("etf", "inflow", "outflow"))
        else "news"
    )
    return {"type": typ, "coins": coins_in(title), "direction": direction, "strength": round(max(pos, neg, 0.1), 2)}


RULES_TAGGER = {"kind": "rules", "version": "lexicon-v1"}


def delisting_rows(store: Any) -> list[dict[str, Any]]:
    """已存的下架公告 → 内核要的 [{"coin", "available_at"}]（按公告可见时刻，不按事后整理的时间）。"""
    out: list[dict[str, Any]] = []
    for r in store.list(EVENTS):
        if r.get("type") != "delisting":
            continue
        at = int(r.get("available_at") or r.get("ts") or 0)
        out.extend({"coin": c, "available_at": at} for c in (r.get("coins") or []) if c)
    return out


def uses_pairlist(source: str) -> bool:
    return "PAIRLIST" in (source or "") or "set_pairlist(" in (source or "")


# ---------------------------------------------------------------------------
class EventHub:
    """事件抓取、标注、存储与物化。"""

    def __init__(
        self,
        store: Any,
        hub: Any,
        *,
        transport: httpx.BaseTransport | None = None,
        llm: Callable[[str, str], Any] | None = None,
        secrets_fn: Callable[[], dict[str, str]] | None = None,
        timeout: float = 15.0,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.hub = hub
        self.transport = transport
        self.llm = llm  # llm(system, user) -> ChatResult（content, model, provider）
        self.secrets_fn = secrets_fn or (lambda: {})
        self.timeout = timeout
        self.now = clock or _now

    def _client(self, base: str) -> httpx.Client:
        return httpx.Client(
            base_url=base,
            timeout=self.timeout,
            transport=self.transport,
            headers={"User-Agent": "Mozilla/5.0 QuantLab-DataHub/1.0"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ 存取
    def events(self, *, kind: str | None = None, coin: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.store.list(EVENTS)
        if kind:
            rows = [r for r in rows if r.get("kind") == kind]
        if coin:
            rows = [r for r in rows if coin.upper() in (r.get("coins") or [])]
        rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        return rows[:limit]

    def save(self, events: list[Event]) -> int:
        new = 0
        for e in events:
            old = self.store.get(EVENTS, e.id)
            if old is None:
                new += 1
            elif old.get("tagger", {}).get("kind") == "llm" and e.tagger.get("kind") != "llm":
                continue  # 不用规则标注覆盖已有的大模型标注
            self.store.upsert(EVENTS, e.to_dict())
        return new

    # ------------------------------------------------------------------ 交易所公告
    BINANCE_CATALOGS = {48: "listing", 161: "delisting", 157: "maintenance"}

    def fetch_binance_announcements(self, pages: int = 1) -> list[Event]:
        out: list[Event] = []
        with self._client("https://www.binance.com") as c:
            for cat, hint in self.BINANCE_CATALOGS.items():
                for page in range(1, pages + 1):
                    r = c.get(
                        "/bapi/composite/v1/public/cms/article/list/query",
                        params={"type": 1, "catalogId": cat, "pageNo": page, "pageSize": 50},
                    )
                    r.raise_for_status()
                    cats = (r.json().get("data") or {}).get("catalogs") or []
                    for a in (cats[0].get("articles") if cats else []) or []:
                        ts = int(a.get("releaseDate") or 0)
                        title = a.get("title") or ""
                        typ = classify_announcement(title)
                        typ = typ if typ != "other" else hint
                        d, s = ANNOUNCEMENT_TAG[typ]
                        out.append(
                            Event(
                                id=event_id("binance", str(a.get("code") or a.get("id") or title)),
                                source="binance",
                                kind="announcement",
                                type=typ,
                                title=title,
                                ts=ts,
                                available_at=ts + FETCH_LAG_MS,
                                url=f"https://www.binance.com/en/support/announcement/{a.get('code', '')}",
                                coins=coins_in(title),
                                direction=d,
                                strength=s,
                                tagger=dict(RULES_TAGGER),
                                tagged_at=self.now(),
                            )
                        )
        return out

    OKX_TYPES = ("announcements-new-listings", "announcements-delistings", "announcements-latest-announcements")

    def fetch_okx_announcements(self, pages: int = 1) -> list[Event]:
        out: list[Event] = []
        with self._client("https://www.okx.com") as c:
            for ann in self.OKX_TYPES:
                for page in range(1, pages + 1):
                    r = c.get("/api/v5/support/announcements", params={"annType": ann, "page": page})
                    r.raise_for_status()
                    data = r.json().get("data") or []
                    for a in (data[0].get("details") if data else []) or []:
                        ts = int(a.get("pTime") or 0)
                        title = a.get("title") or ""
                        typ = classify_announcement(title)
                        if typ == "other":
                            typ = {
                                "announcements-new-listings": "listing",
                                "announcements-delistings": "delisting",
                            }.get(ann, "other")
                        if typ == "other":
                            continue
                        d, s = ANNOUNCEMENT_TAG[typ]
                        out.append(
                            Event(
                                id=event_id("okx", a.get("url") or title),
                                source="okx",
                                kind="announcement",
                                type=typ,
                                title=title,
                                ts=ts,
                                available_at=ts + FETCH_LAG_MS,
                                url=a.get("url") or "",
                                coins=coins_in(title),
                                direction=d,
                                strength=s,
                                tagger=dict(RULES_TAGGER),
                                tagged_at=self.now(),
                            )
                        )
        return out

    # ------------------------------------------------------------------ 新闻
    RSS = {
        "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "cointelegraph": "https://cointelegraph.com/rss",
    }

    def fetch_rss(self, source: str, url: str) -> list[Event]:
        base, _, path = url.partition("://")[2].partition("/")
        with self._client(f"https://{base}") as c:
            r = c.get("/" + path)
            r.raise_for_status()
            text = r.text
        if "<!ENTITY" in text:  # 拒绝带实体声明的 XML（防实体膨胀）
            raise ValueError("RSS 含有实体声明，已拒绝解析")
        root = ET.fromstring(text)
        out: list[Event] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = item.findtext("pubDate") or ""
            try:
                ts = int(parsedate_to_datetime(pub).timestamp() * 1000)
            except (TypeError, ValueError):
                continue
            if not title:
                continue
            out.append(
                Event(
                    id=event_id(source, link or title),
                    source=source,
                    kind="news",
                    type="news",
                    title=title,
                    ts=ts,
                    available_at=ts + FETCH_LAG_MS,
                    url=link,
                )
            )
        return out

    def fetch_cryptopanic(self, key: str) -> list[Event]:
        with self._client("https://cryptopanic.com") as c:
            r = c.get("/api/developer/v2/posts/", params={"auth_token": key, "public": "true", "kind": "news"})
            r.raise_for_status()
            rows = r.json().get("results") or []
        out: list[Event] = []
        for p in rows:
            try:
                ts = int(datetime.fromisoformat(str(p["published_at"]).replace("Z", "+00:00")).timestamp() * 1000)
            except (KeyError, ValueError):
                continue
            votes = p.get("votes") or {}
            out.append(
                Event(
                    id=event_id("cryptopanic", str(p.get("id") or p.get("slug") or p.get("title"))),
                    source="cryptopanic",
                    kind="news",
                    type="news",
                    title=p.get("title") or "",
                    ts=ts,
                    available_at=ts + FETCH_LAG_MS,
                    url=p.get("original_url") or p.get("url") or "",
                    coins=[c["code"] for c in p.get("currencies") or [] if c.get("code")],
                    extra={"votes": {k: votes.get(k) for k in ("positive", "negative", "important") if k in votes}},
                )
            )
        return out

    def tag(self, events: list[Event]) -> dict[str, Any]:
        """结构化标注。大模型优先，失败或没配置时用规则词典。返回所用标注器。"""
        now = self.now()
        tagger = dict(RULES_TAGGER)
        tags: dict[int, dict[str, Any]] = {}
        if self.llm is not None and events:
            for start in range(0, len(events), 20):
                batch = events[start : start + 20]
                user = "\n".join(f"{i}. {e.title}" for i, e in enumerate(batch))
                try:
                    res = self.llm(TAG_PROMPT, user)
                    m = re.search(r"\[.*\]", res.content or "", re.S)
                    arr = json.loads(m.group(0)) if m else []
                    for t in arr:
                        i = int(t.get("i", -1))
                        if 0 <= i < len(batch):
                            tags[start + i] = t
                    tagger = {
                        "kind": "llm",
                        "model": getattr(res, "model", ""),
                        "provider": getattr(res, "provider", ""),
                        "prompt_version": PROMPT_VERSION,
                        "prompt_sha": PROMPT_SHA,
                    }
                except Exception as exc:  # 模型不可用：这一批走规则
                    tagger = {**RULES_TAGGER, "fallback_reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
        for i, e in enumerate(events):
            t = tags.get(i)
            if t:
                e.type = str(t.get("type") or "news")[:20]
                e.coins = [str(c).upper() for c in (t.get("coins") or []) if str(c).upper() not in STOP][:8] or e.coins
                e.direction = max(-1, min(1, int(t.get("direction") or 0)))
                e.strength = max(0.0, min(1.0, float(t.get("strength") or 0)))
                e.tagger = (
                    {k: v for k, v in tagger.items() if k != "fallback_reason"} if tagger["kind"] == "llm" else tagger
                )
            else:
                rt = rule_tag(e.title)
                e.type, e.direction, e.strength = rt["type"], rt["direction"], rt["strength"]
                e.coins = e.coins or rt["coins"]
                e.tagger = dict(RULES_TAGGER) if tagger["kind"] == "llm" else tagger
            e.tagged_at = now
            e.hindsight_risk = e.tagger.get("kind") == "llm" and now - e.ts > DAY
        return tagger

    # ------------------------------------------------------------------ 宏观日历
    def fetch_macro_calendar(self) -> list[Event]:
        """本周经济日历（ForexFactory 公开 JSON）。只留美元的高 / 中重要性事件。"""
        with self._client("https://nfs.faireconomy.media") as c:
            r = c.get("/ff_calendar_thisweek.json")
            r.raise_for_status()
            rows = r.json()
        now = self.now()
        out: list[Event] = []
        for x in rows:
            if x.get("country") != "USD" or x.get("impact") not in ("High", "Medium"):
                continue
            try:
                ts = int(datetime.fromisoformat(str(x["date"])).timestamp() * 1000)
            except (KeyError, ValueError):
                continue
            # 日程是提前公布的：在我们抓到它的时候就已可用（但不早于这周一）
            week = ts - (datetime.fromtimestamp(ts / 1000, tz=timezone.utc).weekday() * DAY) - ts % DAY
            out.append(
                Event(
                    id=event_id("forexfactory", f"{x.get('title')}|{x.get('date')}"),
                    source="forexfactory",
                    kind="macro",
                    type="macro",
                    title=x.get("title") or "",
                    ts=ts,
                    available_at=max(week, min(now, ts)),
                    coins=[],
                    direction=0,
                    strength=1.0 if x.get("impact") == "High" else 0.5,
                    tagger={"kind": "source", "field": "impact"},
                    tagged_at=now,
                    extra={k: x.get(k) for k in ("impact", "forecast", "previous")},
                )
            )
        return out

    # ------------------------------------------------------------------ 同步入口
    def sync(self, connector: str, *, key: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True, "connector": connector, "events": 0, "new": 0, "datasets": [], "errors": []}
        got: list[Event] = []

        def run(label: str, fn: Callable[[], list[Event]]) -> None:
            try:
                got.extend(fn())
            except Exception as exc:
                out["errors"].append({"key": label, "error": f"{type(exc).__name__}: {exc}"[:300]})

        if connector == "announcements":
            run("binance", self.fetch_binance_announcements)
            run("okx", self.fetch_okx_announcements)
        elif connector == "news":
            for src, url in self.RSS.items():
                run(src, lambda src=src, url=url: self.fetch_rss(src, url))
            if key:
                run("cryptopanic", lambda: self.fetch_cryptopanic(key))
            fresh = [e for e in got if self.store.get(EVENTS, e.id) is None]
            out["tagger"] = self.tag(fresh)
            got = fresh
        elif connector == "macro_calendar":
            run("forexfactory", self.fetch_macro_calendar)
        else:
            raise ValueError(f"未知事件连接器 {connector}")
        out["events"] = len(got)
        out["new"] = self.save(got)
        out["datasets"] = self.materialize()
        out["ok"] = not out["errors"] or bool(got)
        return out

    # ------------------------------------------------------------------ 物化成因子字段
    def materialize(self, *, grid_ms: int = HOUR, horizon_days: int = 400) -> list[dict[str, Any]]:
        """把事件变成按小时的时点序列（available_at = 网格时刻，只包含当时已发布的事件）。"""
        rows = self.store.list(EVENTS)
        if not rows:
            return []
        now = self.now()
        start = max(min(r["available_at"] for r in rows), now - horizon_days * DAY)
        grid = list(range(start // grid_ms * grid_ms, now + 1, grid_ms))
        if not grid:
            return []
        lam = math.log(2) / HALF_LIFE_MS
        recs: list[dict[str, Any]] = []
        by: dict[tuple[str, str], list[tuple[int, float]]] = {}
        for r in rows:
            fld = TYPE_FIELD.get(r.get("type") or "") or ("ev_news" if r.get("kind") == "news" else "")
            if not fld:
                continue
            score = float(r.get("direction") or 0) * float(r.get("strength") or 0)
            if fld == "ev_maintenance":
                score = float(r.get("strength") or 0)
            if fld == "ev_listing":
                score = abs(score) or float(r.get("strength") or 0)
            for coin in r.get("coins") or []:
                by.setdefault((fld, f"{coin}/USDT"), []).append((int(r["available_at"]), score))
        for (fld, sym), evs in sorted(by.items()):
            evs.sort()
            vals: list[tuple[int, int, float]] = []
            j, acc, last = 0, 0.0, grid[0]
            for g in grid:
                acc *= math.exp(-lam * (g - last))
                last = g
                while j < len(evs) and evs[j][0] <= g:
                    acc += evs[j][1] * math.exp(-lam * (g - evs[j][0]))
                    j += 1
                vals.append((g, g, round(acc, 6)))
            recs.append(
                self.hub.put_series(
                    field=fld,
                    symbol=sym,
                    source="events",
                    rows=vals,
                    merge=False,
                    meta={"connector": "events", "tagger": self._taggers(fld, sym)},
                )
            )
        macro = sorted(
            (int(r["ts"]), int(r["available_at"]), float(r.get("strength") or 0))
            for r in rows
            if r.get("kind") == "macro"
        )
        if macro:
            vals = []
            for g in grid:
                v = max((s for ts, av, s in macro if av <= g and g <= ts <= g + DAY), default=0.0)
                vals.append((g, g, v))
            recs.append(
                self.hub.put_series(
                    field="ev_macro",
                    symbol=None,
                    source="events",
                    rows=vals,
                    merge=False,
                    meta={"connector": "events", "tagger": {"kind": "source"}},
                )
            )
        return recs

    def _taggers(self, fld: str, sym: str) -> dict[str, Any]:
        coin = sym.split("/")[0]
        kinds: dict[str, int] = {}
        model = ""
        for r in self.store.list(EVENTS):
            if coin in (r.get("coins") or []) and (
                TYPE_FIELD.get(r.get("type") or "") == fld or (fld == "ev_news" and r.get("kind") == "news")
            ):
                t = r.get("tagger") or {}
                kinds[t.get("kind", "?")] = kinds.get(t.get("kind", "?"), 0) + 1
                model = model or t.get("model", "")
        return {
            "kinds": kinds,
            "model": model,
            "prompt_version": PROMPT_VERSION if model else "",
            "prompt_sha": PROMPT_SHA if model else "",
        }


# ---------------------------------------------------------------------------
# 强平流（Binance forceOrder）
# ---------------------------------------------------------------------------
LIQ_WS = "wss://fstream.binance.com/ws/!forceOrder@arr"


def parse_force_order(msg: dict[str, Any]) -> dict[str, Any] | None:
    """一条强平推送 → {symbol, side: long|short（被强平的方向）, notional, ts}。卖单 = 多头被强平。"""
    o = msg.get("o") if msg.get("e") == "forceOrder" else None
    if not o:
        return None
    sym = str(o.get("s") or "")
    if not sym.endswith("USDT"):
        return None
    qty = float(o.get("z") or o.get("q") or 0)
    px = float(o.get("ap") or o.get("p") or 0)
    return {
        "symbol": f"{sym[:-4]}/USDT",
        "side": "long" if o.get("S") == "SELL" else "short",
        "notional": qty * px,
        "ts": int(o.get("T") or msg.get("E") or 0),
    }


class LiquidationRecorder:
    """订阅全市场强平流，按小时汇总成 liq_long / liq_short（美元名义），available_at = 小时结束。

    交易所不提供强平历史，只能从开始记录的那一刻往后积累。没有 websockets 库或网络时不启动。
    """

    def __init__(self, hub: Any, *, bucket_ms: int = HOUR) -> None:
        self.hub = hub
        self.bucket_ms = bucket_ms
        self.buckets: dict[tuple[str, str, int], float] = {}
        self.stats = {"running": False, "messages": 0, "started_at": 0, "last_ts": 0, "error": "", "flushed": 0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def add(self, msg: dict[str, Any]) -> None:
        row = parse_force_order(msg)
        if row is None:
            return
        b = row["ts"] // self.bucket_ms * self.bucket_ms
        with self._lock:
            k = (row["symbol"], row["side"], b)
            self.buckets[k] = self.buckets.get(k, 0.0) + row["notional"]
            self.stats["messages"] += 1
            self.stats["last_ts"] = row["ts"]

    def flush(self, now: int | None = None) -> list[dict[str, Any]]:
        """把已经结束的小时写进数据集。"""
        now = now or _now()
        with self._lock:
            done = {k: v for k, v in self.buckets.items() if k[2] + self.bucket_ms <= now}
            for k in done:
                self.buckets.pop(k)
        series: dict[tuple[str, str], list[tuple[int, int, float]]] = {}
        for (sym, side, b), v in done.items():
            series.setdefault((sym, side), []).append((b, b + self.bucket_ms, round(v, 2)))
        recs = []
        for (sym, side), rows in series.items():
            recs.append(
                self.hub.put_series(
                    field=f"liq_{side}", symbol=sym, source="binance_ws", rows=rows, meta={"connector": "liquidations"}
                )
            )
        self.stats["flushed"] += len(done)
        return recs

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        try:
            import websockets.sync.client  # noqa: F401
        except ImportError:
            self.stats["error"] = "服务器没有安装 websockets，无法订阅强平流"
            return False
        self._stop.clear()
        self.stats.update(running=True, started_at=_now(), error="")
        self._thread = threading.Thread(target=self._run, daemon=True, name="liquidations")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        self.stats["running"] = False

    def _run(self) -> None:
        from websockets.sync.client import connect

        backoff = 1.0
        last_flush = time.monotonic()
        while not self._stop.is_set():
            try:
                with connect(LIQ_WS, open_timeout=10, close_timeout=2) as ws:
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            raw = ws.recv(timeout=5)
                            msg = json.loads(raw)
                            for m in msg if isinstance(msg, list) else [msg]:
                                self.add(m)
                        except TimeoutError:
                            pass
                        if time.monotonic() - last_flush > 60:
                            self.flush()
                            last_flush = time.monotonic()
            except Exception as exc:  # 断线：退避重连
                self.stats["error"] = f"{type(exc).__name__}: {exc}"[:200]
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
        self.flush()


# ---------------------------------------------------------------------------
# Deribit 期权波动率
# ---------------------------------------------------------------------------
def deribit_dvol(client: httpx.Client, currency: str, start: int, end: int) -> list[tuple[int, int, float]]:
    rows: list[tuple[int, int, float]] = []
    cur_end = end
    for _ in range(60):
        r = client.get(
            "/api/v2/public/get_volatility_index_data",
            params={"currency": currency, "start_timestamp": start, "end_timestamp": cur_end, "resolution": "3600"},
        )
        r.raise_for_status()
        res = r.json().get("result") or {}
        data = res.get("data") or []
        for d in data:
            rows.append((int(d[0]), int(d[0]) + HOUR, float(d[4])))  # 小时 K 线收盘才可用
        cont = res.get("continuation")
        if not data or not cont or int(cont) <= start:
            break
        cur_end = int(cont)
    return sorted(set(rows))


def deribit_skew(client: httpx.Client, currency: str, now: int) -> tuple[int, int, float] | None:
    """约 30 天期限的偏度：虚值看跌（行权价 ≈ 0.9 × 现价）IV − 虚值看涨（≈ 1.1 × 现价）IV，单位是波动率点。

    正数 = 看跌期权更贵（市场在买保护）。只能取当前快照，历史靠定期同步积累。
    """
    r = client.get("/api/v2/public/get_book_summary_by_currency", params={"currency": currency, "kind": "option"})
    r.raise_for_status()
    rows = r.json().get("result") or []
    opts = []
    for x in rows:
        parts = str(x.get("instrument_name", "")).split("-")
        if len(parts) != 4 or not x.get("mark_iv") or not x.get("underlying_price"):
            continue
        try:
            exp = int(datetime.strptime(parts[1], "%d%b%y").replace(tzinfo=timezone.utc).timestamp() * 1000) + 8 * HOUR
        except ValueError:
            continue
        opts.append((exp, float(parts[2]), parts[3], float(x["mark_iv"]), float(x["underlying_price"])))
    future = sorted({o[0] for o in opts if o[0] - now >= 7 * DAY})
    if not future:
        return None
    exp = min(future, key=lambda e: abs(e - now - 30 * DAY))
    chain = [o for o in opts if o[0] == exp]
    spot = chain[0][4]
    puts = [o for o in chain if o[2] == "P"]
    calls = [o for o in chain if o[2] == "C"]
    if not puts or not calls:
        return None
    put = min(puts, key=lambda o: abs(o[1] - 0.9 * spot))
    call = min(calls, key=lambda o: abs(o[1] - 1.1 * spot))
    return (now, now, round(put[3] - call[3], 4))


__all__ = [
    "EVENTS",
    "EVENT_FIELDS",
    "PROMPT_SHA",
    "PROMPT_VERSION",
    "Event",
    "EventHub",
    "LiquidationRecorder",
    "classify_announcement",
    "coins_in",
    "deribit_dvol",
    "deribit_skew",
    "parse_force_order",
    "rule_tag",
]
