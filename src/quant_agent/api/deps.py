"""应用服务容器：把各层组装成一个可注入的依赖对象。

API 层只依赖这里，方便测试时整体替换（例如换成内存 store 或 stub router）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..agents.roles import RoleStore
from ..agents.team import TeamRunner, ToolRegistry, sync_builtin_teams
from ..data.service import MarketDataService
from ..engine.backtest import BacktestEngine, BacktestResult, CostModel, stress_scenarios
from ..exchanges.binance import BinanceAdapter
from ..exchanges.bybit import BybitAdapter
from ..exchanges.okx import OKXAdapter
from ..llm.router import AgentConfig, ModelRouter
from ..notify.channels import NotificationHub, build_hub
from ..platform.audit import AuditLog
from ..platform.auth import AuthService
from ..platform.db import Database, default_url
from ..platform.quota import QuotaService
from ..platform.runs import RunService
from ..platform.secretbox import SecretBox, SecretError
from ..platform.versions import VersionService
from ..platform.wake import build as wake_build
from ..store import Collections, JsonStore, WorkspaceStore
from ..strategies.templates import build_strategy

#: 行情摘要默认取多少根 K 线：与 AI 研究室回测默认窗口（项目 bars_limit，默认 1500）一致
MARKET_WINDOW = 1500


def default_home() -> Path:
    """数据目录。可用 QAGENT_HOME 指定；默认 ~/.quant_agent。"""
    env = os.environ.get("QAGENT_HOME", "").strip()
    return Path(env) if env else Path.home() / ".quant_agent"


def default_store_root() -> Path:  # 兼容旧调用
    return default_home()


@dataclass
class AppServices:
    config: AgentConfig
    router: ModelRouter
    store: WorkspaceStore  # 按工作区隔离（由请求上下文决定目录）
    roles: RoleStore
    teams: TeamRunner
    market: MarketDataService
    hub: NotificationHub
    config_path: str | None = None
    home: Path | None = None
    db: Database | None = None
    global_store: JsonStore | None = None  # 部署级配置：模型服务、路由
    auth: AuthService | None = None
    audit: AuditLog | None = None
    secrets: SecretBox | None = None
    runs: RunService | None = None
    versions: VersionService | None = None
    live: Any = None  # LiveService：真实记录导入、同步、对照
    accounts: Any = None  # AccountService：只读账户连接（凭证独立加密）
    demo: Any = None  # DemoConnectionService：交易所模拟盘连接（模拟盘 Key，加密）
    quota: Any = None  # QuotaService：按工作区计量用量并判定配额（T07）
    websearch: Any = None  # WebSearch：联网搜索与网页抓取（密钥在模型配置页登记）
    model_hub: Any = None  # ModelHub：工作区 / 个人模型服务商、成员模型权限与限额、用量计量
    collab: Any = None  # CollabService：群组、任务、可见性、编辑标记、通知中心、问题报告
    dbcache: Any = None  # DbCache：外部接口结果的数据库缓存（行情摘要、搜索结果、网页正文）

    @classmethod
    def build(
        cls, *, config_path: str | None = None, store_root: str | None = None, database_url: str | None = None
    ) -> AppServices:
        home = Path(store_root) if store_root else default_home()
        home.mkdir(parents=True, exist_ok=True)
        config = AgentConfig.load(config_path)
        db = Database(database_url or default_url(home))
        db.migrate()
        store = WorkspaceStore(home)
        global_store = JsonStore(home / "global")
        router = ModelRouter(config)
        roles = RoleStore(store, seed=False)
        hub = build_hub(config.notify)
        tools = ToolRegistry()
        # 一个 SecretBox 实例同时服务交易所凭证与 TOTP 密钥：主密钥只有一把，
        # 传同一个对象可以避免测试里出现两套密钥文件。
        secrets = SecretBox(home)
        services = cls(
            config=config,
            router=router,
            store=store,
            roles=roles,
            teams=TeamRunner(router, roles, store=store, tools=tools),
            # 合成行情默认停用：取不到真实行情就如实报「未接入数据」
            # QAGENT_ALLOW_SYNTHETIC=1 时才兜底（测试 / 离线演示）
            market=MarketDataService(
                adapters=cls._build_adapters(config),
                cache_root=home / "market" / "bars",
                allow_synthetic=os.environ.get("QAGENT_ALLOW_SYNTHETIC", "0") == "1",
            ),
            hub=hub,
            config_path=config_path,
            home=home,
            db=db,
            global_store=global_store,
            auth=AuthService(db, secrets=secrets),
            audit=AuditLog(db),
            secrets=secrets,
            runs=RunService(db, wake=wake_build()),
            versions=VersionService(db),
        )
        services.auth.backfill_usernames()  # 用户名登录之前注册的账号：按邮箱补一个用户名
        from ..live.accounts import AccountService
        from ..live.service import LiveService

        services.live = LiveService(db, services.runs)
        services.accounts = AccountService(db, services.secrets, home)
        from ..live.demo_accounts import DemoConnectionService

        services.demo = DemoConnectionService(services.store, services.secrets, home)
        services.quota = QuotaService(db)
        from ..platform.collab import CollabService

        services.collab = CollabService(db, home)
        services.auth.settings_reader = lambda: global_store.get(Collections.SETTINGS, "platform_settings")
        services._register_collab_hooks()
        store.initializer = services._init_workspace_store
        services._register_tools(tools)
        services._migrate_plain_secrets()
        services.apply_runtime_config()
        return services

    def _register_collab_hooks(self) -> None:
        """策略每存出一个新版本，登记一笔编辑人员标记；新策略登记作者（默认工作区内公开）。"""
        from ..platform import context as _context
        from ..strategies import repo as _repo

        home = self.home.resolve()

        def on_save(repo: Any, rec: Any, previous_version: str, origin: str) -> None:
            ctx = _context.current()
            ws = ctx.workspace_id
            if not ws:
                return
            if Path(repo.root).resolve() != (home / "workspaces" / ws / "store" / "strategies").resolve():
                return
            user_id = ctx.user_id or "system"
            agent = origin in ("agent", "lab") or ctx.user_name.startswith("agent:")
            self.collab.mark_edit(
                ws,
                "strategy",
                rec.meta.slug,
                user_id=user_id,
                user_name=ctx.user_name or ("平台" if user_id == "system" else ""),
                action="edit" if previous_version else "create",
                version=rec.meta.version,
                parent_version=previous_version,
                actor_kind="agent" if agent else "human",
                note=origin,
            )
            if not previous_version:
                self.collab.claim(ws, "strategy", rec.meta.slug, user_id)

        on_save._quantlab = True  # type: ignore[attr-defined]
        _repo.SAVE_HOOKS[:] = [h for h in _repo.SAVE_HOOKS if not getattr(h, "_quantlab", False)]
        _repo.SAVE_HOOKS.append(on_save)

    def _init_workspace_store(self, workspace_id: str, store: JsonStore) -> None:
        """新工作区第一次被访问：写入内置角色与预设团队。"""
        self.roles._seed_builtin(store)
        sync_builtin_teams(store)

    # ---------------- 大模型密钥（部署级，加密存放） ----------------
    @property
    def secrets_path(self) -> Path:
        return self.global_store.root / "llm_secrets.enc.json"

    def _migrate_plain_secrets(self) -> None:
        """v0.3 把密钥明文写在 store/secrets.json；升级后加密迁移并删除明文文件。"""
        import json

        for legacy in (self.home / "secrets.json", self.home / "store" / "secrets.json"):
            if legacy.exists():
                try:
                    plain = json.loads(legacy.read_text(encoding="utf-8"))
                    merged = {**plain, **self.read_secrets()}
                    self.write_secrets(merged)
                    legacy.unlink()
                except (OSError, ValueError):
                    pass

    def read_secrets(self) -> dict[str, str]:
        import json

        try:
            data = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out: dict[str, str] = {}
        for name, token in data.items():
            try:
                out[name] = self.secrets.decrypt(token)
            except SecretError:
                continue
        return out

    def write_secrets(self, secrets: dict[str, str]) -> None:
        import json

        path = self.secrets_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        enc = {k: self.secrets.encrypt(v) for k, v in secrets.items() if v}
        tmp.write_text(json.dumps(enc, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)

    def apply_runtime_config(self) -> None:
        """接上工作区模型中心：服务商与路由按请求上下文（工作区 + 成员）解析，调用前查限额、调用后记账。

        Web 平台不读配置文件里的 provider（那只给命令行用）：模型只来自用户自己添加的
        个人服务商，或工作区管理员共享的服务商。旧版存在部署级 global store 的服务商
        在第一次启动时迁到部署管理员的工作区。
        """
        from ..llm.hub import ModelHub

        if self.model_hub is None:
            self.model_hub = ModelHub(
                store=self.store, secrets=self.secrets, db=self.db, auth=self.auth, audit=self.audit
            )
            self.model_hub.migrate_legacy(self.global_store, self.read_secrets())
        hub = self.model_hub
        self.router.resolver = hub.scoped_config

        def guard(pc, model):
            """服务商 / 成员限额之外，再查工作区当日配额（LLM 调用次数与 token）：用完就不再调用模型。"""
            reason = hub.guard(pc, model)
            if reason:
                return reason
            return self.llm_quota_block()

        self.router.guard = guard

        def on_usage(pc, model, usage):
            hub.record(pc, model, usage)
            if self.quota is not None:
                from ..llm.hub import usage_tokens

                inp, out = usage_tokens(usage)
                self.quota.add("llm_calls", 1)
                if inp + out:
                    self.quota.add("llm_tokens", inp + out)

        self.router.on_usage = on_usage
        self.router._clients.clear()

    def llm_quota_block(self) -> str | None:
        """工作区当日 LLM 配额用完时返回原因（中文），否则 None。只读，不产生用量。

        token 按「已用 ≥ 上限」判定（再要 1 个 token 就超），调用次数按「已用 + 本次 1 次 > 上限」判定。
        """
        from ..platform import context
        from ..platform.errors import AppError

        if self.quota is None or not context.current().workspace_id:
            return None
        try:
            self.quota.check("llm_tokens", 1)
            self.quota.check("llm_calls", 1)
        except AppError as exc:
            return exc.message
        except Exception:  # 计量表读不到时不拦截对话（计量失败不该让平台不可用）
            return None
        return None

    @staticmethod
    def _build_adapters(config: AgentConfig) -> dict[str, Any]:
        simulated = bool(config.exchange.get("simulated", True))
        adapters: dict[str, Any] = {}
        try:
            adapters["binance"] = BinanceAdapter(
                simulated=simulated,
                api_key=config.exchange.get("binance_api_key") or None,
            )
        except Exception:
            pass
        try:
            okx_kwargs = {}
            okx_base = config.exchange.get("okx_base") or os.environ.get("QUANTLAB_OKX_BASE")
            if okx_base:
                okx_kwargs["base_url"] = okx_base
            adapters["okx"] = OKXAdapter(
                **okx_kwargs,
                simulated=simulated,
                api_key=config.exchange.get("okx_api_key") or None,
            )
        except Exception:
            pass
        try:
            # Bybit 只接公共行情（下单走「交易所模拟盘」里的 Demo Trading 连接）
            adapters["bybit"] = BybitAdapter()
        except Exception:
            pass
        return adapters

    def _seed_teams(self) -> None:
        sync_builtin_teams(self.teams.store)

    def _register_tools(self, tools: ToolRegistry) -> None:
        """把平台能力暴露给角色（角色卡里的 tools 字段引用这些名字）。"""
        tools.register(
            "market_summary",
            self.market_summary,
            label="行情概览",
            description=f"标的近期价格结构、波动与成交量摘要。bars 默认 {MARKET_WINDOW} 根，与 AI 研究室回测窗口一致；"
            "评审某个研究项目时，请用材料里给出的标的、周期与 K 线根数，保证和回测看的是同一段数据",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "如 BTC/USDT"},
                    "timeframe": {"type": "string", "description": "周期：数字+单位，例如 100ms、250ms、1s、5s、1m、15m、1h、4h、1d（秒级 / 毫秒级只有 Binance 现货，由 1s K 线或逐笔成交聚合）"},
                    "bars": {"type": "integer", "description": f"K 线根数 100-5000，默认 {MARKET_WINDOW}"},
                },
                "required": ["symbol"],
            },
        )
        tools.register("query_runs", self.query_runs, label="查询回测记录", description="按策略或标的查历史回测成绩")
        tools.register(
            "search_templates", self.search_templates, label="搜索策略模板", description="在模板库里按关键词找策略"
        )
        tools.register("get_metrics", self.get_metrics, label="读取回测指标", description="读取某次回测的完整指标")
        tools.register(
            "check_strategy_code",
            self.check_strategy_code,
            label="检查策略代码",
            description="静态检查策略代码的语法与安全规则",
        )
        tools.register("risk_check", self.risk_check, label="风控检查", description="按风控规则检查回测结果")
        # 新式工具：带参数 schema，策略室 / 角色 / 指挥台共用
        from ..llm.websearch import SOURCE_CHOICES, WebSearch

        self.websearch = WebSearch(self.read_secrets)
        if self.dbcache is None:
            from ..data.dbcache import DbCache

            self.dbcache = DbCache(self.home / "cache" / "api_cache.sqlite3")
        self.websearch.cache = self.dbcache
        obj = {"type": "object"}
        tools.register(
            "search_platform",
            self.search_platform,
            label="站内搜索",
            description="搜索本工作区的策略、回测记录、策略室项目与内置模板",
            parameters={
                **obj,
                "properties": {
                    "query": {"type": "string"},
                    "kind": {"type": "string", "enum": ["all", "strategy", "backtest", "lab", "template"]},
                },
                "required": ["query"],
            },
        )
        tools.register(
            "web_search",
            self.websearch.search,
            label="联网搜索",
            description=(
                "联网搜索公开资料（研报、指标定义、市场事件、用户提到的新闻与名词），返回标题、链接与摘要。"
                "source：auto 自动；google（谷歌，需密钥，没有时自动改用免密钥来源并说明）；brave；custom（自定义来源）；"
                "baidu（百度，中文资料优先）；bing（必应）；duckduckgo；"
                "news（新闻：多个新闻源合并）；google_news / baidu_news；all（多个来源合并去重，适合交叉印证）"
            ),
            parameters={
                **obj,
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                    "topic": {"type": "string", "enum": ["general", "news"]},
                    "source": {"type": "string", "enum": SOURCE_CHOICES},
                },
                "required": ["query"],
            },
        )
        tools.register(
            "add_search_source",
            lambda url="", label="", kind="searxng": self.websearch.add_custom(
                self.write_secrets, label=label, url=url, kind=kind, trusted=False
            ),
            label="添加搜索来源",
            description=(
                "登记一个自定义联网搜索来源，之后 web_search 的 source=custom / all 会用到它。"
                "kind=searxng：SearXNG 实例根地址（需开启 JSON 输出）；kind=rss：带 {q} 占位的 RSS 搜索地址。"
                "只接受公网 http(s) 地址；密钥类服务（Google、Brave、Tavily）请研究者在 设置 → 模型 → 联网搜索 里填"
            ),
            parameters={
                **obj,
                "properties": {
                    "url": {"type": "string"},
                    "label": {"type": "string"},
                    "kind": {"type": "string", "enum": ["searxng", "rss"]},
                },
                "required": ["url"],
            },
        )
        tools.register(
            "list_search_sources",
            lambda: {
                k: v for k, v in self.websearch.status().items() if k in ("providers", "custom", "free_sources")
            },
            label="搜索来源",
            description="查看联网搜索可用的来源：哪些付费 API 已配置密钥、自定义来源、免密钥来源",
            parameters={**obj, "properties": {}},
        )
        tools.register(
            "sync_market_data",
            self.sync_market_data,
            label="同步行情",
            description=(
                "从交易所公共接口同步一段 K 线到本地（分段拉取、增量补齐、校验缺口）。"
                "回测数据不足、data_check 提示覆盖不够或数据不新鲜时先调用它；days 为回看天数"
            ),
            parameters={
                **obj,
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": '如 ["BTC/USDT"]'},
                    "timeframe": {"type": "string", "description": "周期：数字+单位，例如 100ms、250ms、1s、5s、1m、15m、1h、4h、1d（秒级 / 毫秒级只有 Binance 现货，由 1s K 线或逐笔成交聚合）"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 1500},
                    "exchange": {"type": "string", "enum": ["binance", "okx", "bybit"]},
                },
                "required": ["symbols", "timeframe"],
            },
        )
        tools.register(
            "data_coverage",
            self.data_coverage,
            label="数据覆盖",
            description="查看本地已同步的行情：每个标的 / 周期的起止时间、根数、缺口与最后更新时间",
            parameters={
                **obj,
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "timeframe": {"type": "string"},
                },
            },
        )
        tools.register(
            "fetch_page",
            self.websearch.fetch,
            label="读取网页",
            description="读取一个公开网页的正文（只支持 http/https，不能访问内网）",
            parameters={
                **obj,
                "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer"}},
                "required": ["url"],
            },
        )

    # ---------------- 平台能力（同时是角色工具）----------------
    def sync_market_data(
        self, symbols: list[str] | str = "", timeframe: str = "1h", days: int = 180, exchange: str = "binance"
    ) -> dict:
        """同步若干标的的真实 K 线到本地缓存（Agent 工具 / 数据页共用）。"""
        if isinstance(symbols, str):
            symbols = [x for x in symbols.replace("，", ",").split(",") if x.strip()]
        syms = [s.strip().upper() for s in symbols if s.strip()][:20]
        if not syms:
            return {"ok": False, "error": "至少给出一个标的"}
        end = int(time.time() * 1000)
        start = end - max(1, min(int(days or 180), 1500)) * 86_400_000
        rows = [self.market.sync(s, timeframe, start=start, end=end, exchange=exchange) for s in syms]
        return {
            "ok": any(r.get("ok") for r in rows),
            "results": rows,
            "note": "coverage < 0.9 或 gaps > 0 时说明交易所历史不全（新币、停牌或接口限制），回测会被标记为非正式",
        }

    def data_coverage(self, symbols: list[str] | None = None, timeframe: str = "") -> dict:
        entries = self.market.cache.entries() if self.market.cache else []
        want = {s.upper() for s in symbols or []}
        rows = [
            e
            for e in entries
            if (not want or e["symbol"].upper() in want) and (not timeframe or e["timeframe"] == timeframe)
        ]
        return {"entries": rows[:60], "total": len(rows), "last_errors": self.market.last_error}

    def market_summary(self, symbol: str = "BTC/USDT", timeframe: str = "1h", bars: int = MARKET_WINDOW) -> dict:
        """bars：取最近多少根 K 线。默认与 AI 研究室回测窗口一致（1500），单 Agent 与团队看到的是同一段数据。"""
        from ..data.service import DataUnavailable

        limit = max(100, min(int(bars or MARKET_WINDOW), 5000))
        # 同一标的 / 周期 / 窗口的摘要在一根 K 线内不会变：放进数据库缓存，多个对话、多个角色反复看行情时不再重算、不再请求交易所
        from ..data.quality import TIMEFRAME_MS

        if self.dbcache is not None:
            ttl = max(2.0, min(TIMEFRAME_MS.get(timeframe, 3_600_000) / 1000, 300.0))
            return self.dbcache.remember(
                "market_summary",
                f"{symbol}|{timeframe}|{limit}",
                ttl,
                lambda: self._market_summary(symbol, timeframe, limit),
                keep=lambda v: bool(v) and not v.get("error"),
            )
        return self._market_summary(symbol, timeframe, limit)

    def _market_summary(self, symbol: str, timeframe: str, limit: int) -> dict:
        from ..data.service import DataUnavailable

        try:
            bars, source = self.market.get_bars(symbol, timeframe, limit=limit)
        except DataUnavailable as exc:
            return {"symbol": symbol, "timeframe": timeframe, "error": str(exc), "connected": False}
        if not bars:
            return {"symbol": symbol, "error": "无数据"}
        closes = [b.close for b in bars]
        peak = max(closes)
        trough = min(closes)
        first, last = closes[0], closes[-1]
        rets = [(b.close - a.close) / a.close for a, b in zip(bars, bars[1:], strict=False) if a.close]
        mean_r = sum(rets) / len(rets) if rets else 0.0
        var = sum((r - mean_r) ** 2 for r in rets) / len(rets) if rets else 0.0
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": source,
            "bars": len(bars),
            "last_price": last,
            "period_return": (last - first) / first if first else 0.0,
            "high": peak,
            "low": trough,
            "volatility_per_bar": var**0.5,
            "coverage": {"start": bars[0].open_time, "end": bars[-1].close_time},
        }

    def query_runs(self, query: str = "", limit: int = 10) -> list[dict]:
        runs = self.store.list(Collections.RUNS, limit=limit)
        if not query:
            return runs
        return [
            r
            for r in runs
            if query.lower() in str(r.get("strategy", "")).lower() or query.lower() in str(r.get("symbol", "")).lower()
        ]

    def search_templates(self, query: str = "") -> list[dict]:
        from ..strategies.templates import describe_templates

        templates = describe_templates()
        if not query:
            return templates
        return [
            t
            for t in templates
            if query.lower() in t["name"].lower()
            or query.lower() in t["label"].lower()
            or query.lower() in t["description"].lower()
        ]

    def search_platform(self, query: str = "", kind: str = "all", limit: int = 12) -> dict:
        """站内搜索：按关键词在策略、回测、策略室项目、模板里找（不区分大小写，标题与说明都匹配）。"""
        from ..strategies.repo import StrategyRepository

        q = (query or "").strip().lower()
        hits: list[dict] = []

        def match(*texts: object) -> bool:
            return not q or any(q in str(t or "").lower() for t in texts)

        if kind in ("all", "strategy"):
            repo = StrategyRepository(self.store.root / "strategies")
            for rec in repo.list() if repo.root.exists() else []:
                m = rec.meta
                if match(m.name, m.slug, getattr(m, "notes", ""), getattr(m, "symbol", "")):
                    hits.append(
                        {
                            "type": "strategy",
                            "id": m.slug,
                            "title": m.name,
                            "detail": f"{getattr(m, 'symbol', '')} {getattr(m, 'timeframe', '')} v{m.version}".strip(),
                            "link": f"#/editor/{m.slug}",
                        }
                    )
        if kind in ("all", "backtest"):
            for r in self.store.list(Collections.BACKTESTS)[:300]:
                if match(r.get("name"), r.get("label"), r.get("slug"), r.get("symbol")):
                    met = r.get("metrics") or {}
                    hits.append(
                        {
                            "type": "backtest",
                            "id": r.get("id"),
                            "title": r.get("label") or r.get("name") or r.get("slug"),
                            "detail": f"夏普 {met.get('sharpe')} · 回撤 {met.get('max_drawdown')}",
                            "link": f"#/backtest/{r.get('id')}",
                        }
                    )
        if kind in ("all", "lab"):
            for p in self.store.list("lab_projects"):
                if match(p.get("title"), p.get("idea")):
                    hits.append(
                        {
                            "type": "lab",
                            "id": p.get("id"),
                            "title": p.get("title"),
                            "detail": (p.get("idea") or "")[:80],
                            "link": f"#/lab/{p.get('id')}",
                        }
                    )
        if kind in ("all", "template"):
            for t in self.search_templates(query):
                hits.append(
                    {
                        "type": "template",
                        "id": t.get("name"),
                        "title": t.get("label") or t.get("name"),
                        "detail": (t.get("description") or "")[:80],
                    }
                )
        return {"ok": True, "query": query, "results": hits[: max(1, min(int(limit or 12), 30))], "total": len(hits)}

    def get_metrics(self, run_id: str = "") -> dict:
        run = self.store.get(Collections.RUNS, run_id)
        return run.get("metrics", {}) if run else {}

    def check_strategy_code(self, code: str = "") -> dict:
        """静态契约检查：策略不得越界。对应 CONTRIBUTING 里的铁律。"""
        from ..strategies.sdk import FORBIDDEN_IN_STRATEGY

        issues: list[str] = []
        lowered = code
        for token in FORBIDDEN_IN_STRATEGY:
            if token in lowered and f"def {token}" in lowered:
                issues.append(f"检测到被禁用的调用：{token}")
        for token in ("import requests", "import socket", "import http", "urllib", "aiohttp"):
            if token in lowered:
                issues.append(f"检测到网络相关导入：{token}")
        if "set_target_position" not in code:
            issues.append("未发现 set_target_position：策略必须通过目标仓位表达意图")
        if "on_bar" not in code and "on_market_event" not in code:
            issues.append("缺少 on_bar / on_market_event 回调")
        return {"ok": not issues, "issues": issues}

    def risk_check(self, payload: str = "") -> dict:
        """基础风控校验：研究用示例阈值（见 plan.md 第 9 章）。"""
        import json

        try:
            cfg = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            cfg = {}
        findings: list[str] = []
        max_position = float(cfg.get("max_position", 1.0))
        if max_position > 0.1:
            findings.append(f"单策略资金占比 {max_position:.0%} 超过研究示例上限 10%，多策略叠加可能导致账户整体超限")
        if not cfg.get("stop_loss"):
            findings.append("未设置止损，回撤控制仅靠仓位上限")
        if float(cfg.get("max_drawdown_stop", 0.1)) > 0.15:
            findings.append("策略回撤停用阈值高于研究示例的 10%")
        return {
            "findings": findings,
            "verdict": "通过" if not findings else "有条件通过",
            "note": "阈值仅为测试配置示例，实际由负责人按策略频率、资本与压力结果确定",
        }

    # ---------------- 回测 ----------------
    def run_backtest(
        self,
        *,
        strategy_name: str,
        params: dict | None = None,
        symbol: str = "BTC/USDT",
        timeframe: str = "1h",
        initial_cash: float = 10_000.0,
        bars_limit: int = 1500,
        exchange: str = "binance",
        cost_scale: float = 1.0,
        scenario: str | None = None,
        persist: bool = True,
    ) -> BacktestResult:
        bars, source = self.market.get_bars(symbol, timeframe, limit=bars_limit, exchange=exchange)
        base = CostModel().scale(cost_scale)
        cost = stress_scenarios(base).get(scenario or "baseline", base)
        engine = BacktestEngine(initial_cash=initial_cash, cost_model=cost)
        strategy = build_strategy(strategy_name, params or {})
        run_id = f"bt_{int(time.time() * 1000)}"
        result = engine.run(strategy, bars, run_id=run_id, symbol=symbol, timeframe=timeframe)
        result.notes.insert(0, f"数据来源：{source}")
        if persist:
            payload = result.to_dict(max_points=1200)
            payload["id"] = run_id
            payload["strategy"] = strategy_name
            payload["data_source"] = source
            payload["cost_scale"] = cost_scale
            payload["scenario"] = scenario or "baseline"
            self.store.upsert(Collections.RUNS, payload)
        return result


_services: AppServices | None = None


def get_services() -> AppServices:
    global _services
    if _services is None:
        _services = AppServices.build()
    return _services


def set_services(services: AppServices) -> None:
    """测试用：注入替身。"""
    global _services
    _services = services


def reset_services() -> None:
    global _services
    _services = None
