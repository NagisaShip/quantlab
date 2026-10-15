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
from ..agents.team import TeamRunner, ToolRegistry, builtin_teams
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
            market=MarketDataService(adapters=cls._build_adapters(config), cache_root=home / "market" / "bars"),
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
        from ..live.accounts import AccountService
        from ..live.service import LiveService

        services.live = LiveService(db, services.runs)
        services.accounts = AccountService(db, services.secrets, home)
        from ..live.demo_accounts import DemoConnectionService

        services.demo = DemoConnectionService(services.store, services.secrets, home)
        services.quota = QuotaService(db)
        store.initializer = services._init_workspace_store
        services._register_tools(tools)
        services._migrate_plain_secrets()
        services.apply_runtime_config()
        return services

    def _init_workspace_store(self, workspace_id: str, store: JsonStore) -> None:
        """新工作区第一次被访问：写入内置角色与预设团队。"""
        self.roles._seed_builtin(store)
        existing = {t["id"] for t in store.list(Collections.TEAMS)}
        for team in builtin_teams():
            if team.id not in existing:
                store.upsert(Collections.TEAMS, team.to_dict())

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
        """把界面里登记的 Provider、密钥与路由叠加到配置文件之上，并刷新模型客户端。

        优先级：界面登记 > 用户级 config.toml > 项目级 agent.config.toml > 内置默认。
        密钥加密后存放在数据目录 global/llm_secrets.enc.json（主密钥另存，接口只返回脱敏值）。
        """
        from ..llm.router import ProviderConfig

        secrets = self.read_secrets()
        for row in self.global_store.list(Collections.PROVIDERS):
            name = row.get("name")
            if not name:
                continue
            existing = self.config.providers.get(name)
            self.config.providers[name] = ProviderConfig(
                name=name,
                type=row.get("type") or (existing.type if existing else "openai_compatible"),
                base_url=row.get("base_url") or (existing.base_url if existing else ""),
                api_key=secrets.get(name) or (existing.api_key if existing else ""),
                models=row.get("models") or (existing.models if existing else []),
                model_settings=row.get("model_settings") or (existing.model_settings if existing else {}),
            )
        for name, key in secrets.items():
            if name in self.config.providers and key:
                self.config.providers[name].api_key = key
        routing = self.global_store.get(Collections.SETTINGS, "routing")
        if routing:
            for k in ("default", "code_gen", "reasoning", "report"):
                if routing.get(k):
                    self.config.routing[k] = routing[k]
            if isinstance(routing.get("fallback"), list):
                self.config.routing["fallback"] = routing["fallback"]
        self.router._clients.clear()

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
        existing = {t.id for t in self.teams.list_teams()}
        for team in builtin_teams():
            if team.id not in existing:
                self.teams.save_team(team)

    def _register_tools(self, tools: ToolRegistry) -> None:
        """把平台能力暴露给角色（角色卡里的 tools 字段引用这些名字）。"""
        tools.register(
            "market_summary", self.market_summary, label="行情概览", description="标的近期价格结构、波动与成交量摘要"
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
        from ..llm.websearch import WebSearch

        self.websearch = WebSearch(self.read_secrets)
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
            description="联网搜索公开资料（研报、指标定义、市场事件），返回标题、链接与摘要",
            parameters={
                **obj,
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                    "topic": {"type": "string", "enum": ["general", "news"]},
                },
                "required": ["query"],
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
    def market_summary(self, symbol: str = "BTC/USDT", timeframe: str = "1h") -> dict:
        bars, source = self.market.get_bars(symbol, timeframe, limit=500)
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
