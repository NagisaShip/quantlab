"""qagent CLI —— 对标 [CC] / codex 的使用体验。

命令：
  qagent init                    初始化配置
  qagent market ping|bars        交易所连通性与 K 线
  qagent backtest                运行一次回测（真实行情优先）
  qagent strategy new            生成策略骨架
  qagent strategy guide          引导式生成
  qagent research                调研任务
  qagent roles                   列出角色库
  qagent roles draft "需求"       AI 辅助创建角色
  qagent teams                   列出调研团队
  qagent teams run <id> "主题"    运行团队调研
  qagent agent run daily         手动触发长期 Agent（dry-run）
  qagent serve                   启动 API 服务（控制面：只受理与查询，不跑长任务）
  qagent worker                  启动独立 worker（回测 / 优化 / 实时模拟 / 账户同步）
  qagent migrate                 执行数据库迁移
  qagent gen-key                 生成凭证加密主密钥 QAGENT_SECRET_KEY
"""

from __future__ import annotations

import argparse
import sys

from .agents.guidance import GUIDANCES, render_guidance_prompt
from .agents.long_running import DailyDigestAgent, DigestData
from .agents.research import ResearchAgent
from .agents.roles import RoleDesigner, RoleStore
from .agents.strategy_gen import StrategyGenAgent
from .agents.team import TeamRunner, ToolRegistry
from .context.manager import LongTermMemory
from .data.service import MarketDataService
from .engine.backtest import BacktestEngine, CostModel, stress_scenarios
from .exchanges.binance import BinanceAdapter
from .exchanges.okx import OKXAdapter
from .llm.router import AgentConfig, ModelRouter
from .notify.channels import build_hub
from .strategies.templates import build_strategy, describe_templates


def _adapter(name: str, simulated: bool):
    if name == "okx":
        return OKXAdapter(simulated=simulated)
    return BinanceAdapter(simulated=simulated)


def _services():
    """CLI 侧的轻量装配（与 API 侧共用同一套组件）。"""
    from .store import JsonStore

    config = AgentConfig.load()
    store = JsonStore()
    router = ModelRouter(config)
    roles = RoleStore(store)
    runner = TeamRunner(router, roles, store=store, tools=ToolRegistry())
    return config, store, router, roles, runner


def cmd_init(args) -> int:
    print("初始化步骤：")
    print("  1) 复制 agent.config.example.toml 为 agent.config.toml")
    print("  2) 设置环境变量（任选其一即可）：")
    print("       DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY / DASHSCOPE_API_KEY")
    print("  3) 通知通道（可选）：WECOM_WEBHOOK_URL / SERVERCHAN_KEY / PUSHPLUS_TOKEN")
    print("\n没有密钥也能启动：行情会走合成数据，Agent 会走模板降级。")
    return 0


def cmd_market(args) -> int:
    adapter = _adapter("binance", simulated=False)
    if args.action == "ping":
        print("ping ok" if adapter.ping() else "ping failed")
    elif args.action == "bars":
        bars = adapter.fetch_bars(args.symbol, args.timeframe, 0, 2_000_000_000_000)
        for b in bars[-5:]:
            print(f"{b.open_time} O{b.open} H{b.high} L{b.low} C{b.close} closed={b.closed}")
    return 0


def cmd_backtest(args) -> int:
    svc = MarketDataService(adapters={"binance": BinanceAdapter()})
    bars, source = svc.get_bars(args.symbol, args.timeframe, limit=args.bars)
    print(f"数据来源：{source}　K 线 {len(bars)} 根")
    scenarios = stress_scenarios(CostModel()) if args.stress else {"baseline": CostModel()}
    for label, cost in scenarios.items():
        engine = BacktestEngine(initial_cash=args.cash, cost_model=cost)
        result = engine.run(
            build_strategy(args.strategy),
            bars,
            run_id=f"cli_{label}",
            symbol=args.symbol,
            timeframe=args.timeframe,
        )
        m = result.metrics
        sharpe = f"{m.sharpe:.2f}" if m.sharpe is not None else "N/A"
        print(f"{label:14s} 收益 {m.total_return:+.2%}　回撤 {m.max_drawdown:.2%}　Sharpe {sharpe}　交易 {m.trades}")
    return 0


def cmd_strategy(args) -> int:
    if args.action == "guide":
        g = GUIDANCES.get(args.name or "trend")
        if g is None:
            print(f"未知引导: {args.name}")
            return 1
        print(f"[引导: {g.title}]")
        print(render_guidance_prompt(g, g.filled_defaults()))
        return 0
    if args.action == "list":
        for t in describe_templates():
            print(f"{t['name']:16s} {t['label']:8s} {t['description']}")
        return 0
    _, _, router, _, _ = _services()
    result = StrategyGenAgent(router).generate(args.description or "均线突破策略")
    print("=== 生成的策略 ===")
    print(f"name: {result.name}  params: {result.params}")
    print(result.code)
    return 0


def cmd_research(args) -> int:
    _, _, router, _, _ = _services()
    print(ResearchAgent(router).research(args.topic).summary)
    return 0


def cmd_roles(args) -> int:
    _, _, router, roles, _ = _services()
    if args.action == "list":
        for r in roles.list():
            src = "预设" if r.builtin else ("AI 起草" if r.source == "ai_draft" else "自定义")
            print(f"{r.id:22s} {r.name:12s} [{src}] {r.model or '跟随全局路由':28s} {r.title}")
        return 0
    if args.action == "draft":
        role = RoleDesigner(router).draft(args.requirement)
        print(f"起草完成：{role.name}（{role.id}）")
        print(f"定位：{role.title}")
        print(f"温度：{role.temperature}　模型：{role.model or '跟随全局路由'}")
        print(f"工具：{', '.join(role.tools) or '无'}")
        print("\n--- 系统提示词 ---")
        print(role.system_prompt)
        return 0
    return 0


def cmd_teams(args) -> int:
    _, _, _, _, runner = _services()
    if args.action == "list":
        for t in runner.list_teams():
            print(f"{t.id:24s} {t.name:14s} [{t.mode}] {len(t.role_ids)} 角色　{t.description}")
        return 0
    if args.action == "run":
        team = runner.get_team(args.team_id)
        if team is None:
            print(f"未知团队: {args.team_id}")
            return 1
        run = runner.run(team, args.topic)
        print(f"状态：{run.status}　环节：{len(run.steps)}")
        for s in run.steps:
            mark = "OK " if s.status == "ok" else "ERR"
            print(f"  [{mark}] {s.role_name:14s} {s.model:28s} {s.elapsed_ms}ms")
            if s.status != "ok":
                print(f"        {s.error[:160]}")
        print("\n--- 团队结论 ---")
        print(run.synthesis or "（无）")
        return 0
    return 0


def cmd_agent(args) -> int:
    config, _, router, _, _ = _services()
    hub = build_hub(config.notify)

    def collect() -> DigestData:
        # 占位数据；接入平台存储后替换为真实净值/回撤/异常
        return DigestData(
            date="2026-10-12",
            nav=10120.5,
            period_change=0.012,
            max_drawdown=0.008,
            trades=14,
            win_rate=0.57,
            alerts=[],
        )

    if args.action == "run":
        agent = DailyDigestAgent(router=router, hub=hub, collect_data=collect, memory=LongTermMemory())
        record = agent.run_once(dry_run=True)
        print(f"[{record.status}] {record.output}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("quant_agent.api.app:app", host=args.host, port=args.port)
    return 0


def cmd_worker(args) -> int:
    """独立 worker：认领并执行回测、参数优化与实时模拟（与 API 分开部署）。"""
    import logging
    import signal

    from .api.deps import AppServices, set_services
    from .api.tasks import build_worker
    from .platform import isolation

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    services = AppServices.build(database_url=args.database_url or None)
    set_services(services)
    worker = build_worker(services, slots=args.slots, poll_seconds=args.poll)
    rep = isolation.probe()
    logging.getLogger("quantlab.worker").info(
        "策略隔离：%s（进程=%s 资源上限=%s 降权=%s 断网=%s）",
        "已验证" if rep["verified"] else "部分生效",
        rep["process"],
        rep["rlimits"],
        rep["uid_drop"],
        rep["netns"],
    )

    def stop(*_):
        worker.stop_event.set()
        wake = getattr(services.runs, "wake", None)
        if wake is not None:
            wake.notify()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run_forever()
    return 0


def cmd_migrate(args) -> int:
    """执行数据库迁移（API 与 worker 启动时也会自动执行）。"""
    from .api.deps import default_home
    from .platform.db import Database, default_url

    db = Database(args.database_url or default_url(default_home()))
    applied = db.migrate()
    print("已执行迁移：", applied or "无（已是最新）")
    return 0


def cmd_gen_key(args) -> int:
    """生成 QAGENT_SECRET_KEY（Fernet 主密钥，32 字节 urlsafe base64）。"""
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode("ascii"))
    return 0


def cmd_config(args) -> int:
    config, _, router, _, _ = _services()
    print("providers:", list(config.providers))
    print("routing:", config.routing)
    print("可用模型:", [m["target"] for m in router.available_models()] or "（无，未配置密钥）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qagent", description="量化仿真平台 AI Agent CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    m = sub.add_parser("market")
    m.add_argument("action", choices=["ping", "bars"])
    m.add_argument("--symbol", default="BTC/USDT")
    m.add_argument("--timeframe", default="1h")
    m.set_defaults(func=cmd_market)

    b = sub.add_parser("backtest")
    b.add_argument("--strategy", default="ma_trend")
    b.add_argument("--symbol", default="BTC/USDT")
    b.add_argument("--timeframe", default="1h")
    b.add_argument("--bars", type=int, default=1500)
    b.add_argument("--cash", type=float, default=10000)
    b.add_argument("--stress", action="store_true", help="同时跑四档压力场景")
    b.set_defaults(func=cmd_backtest)

    s = sub.add_parser("strategy")
    s.add_argument("action", choices=["new", "guide", "list"])
    s.add_argument("--description", default=None)
    s.add_argument("--name", default="trend")
    s.set_defaults(func=cmd_strategy)

    r = sub.add_parser("research")
    r.add_argument("topic")
    r.set_defaults(func=cmd_research)

    ro = sub.add_parser("roles")
    ro.add_argument("action", choices=["list", "draft"])
    ro.add_argument("requirement", nargs="?", default="")
    ro.set_defaults(func=cmd_roles)

    t = sub.add_parser("teams")
    t.add_argument("action", choices=["list", "run"])
    t.add_argument("team_id", nargs="?", default="default_research_team")
    t.add_argument("topic", nargs="?", default="评估当前策略是否值得继续投入")
    t.set_defaults(func=cmd_teams)

    a = sub.add_parser("agent")
    a.add_argument("action", choices=["run"])
    a.set_defaults(func=cmd_agent)

    sub.add_parser("gen-key", help="生成凭证加密主密钥").set_defaults(func=cmd_gen_key)

    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)

    w = sub.add_parser("worker", help="启动独立 worker（执行回测、优化、实时模拟）")
    w.add_argument("--slots", type=int, default=2, help="同时执行的批处理任务数")
    w.add_argument("--poll", type=float, default=3.0, help="没有 Redis 唤醒时的数据库轮询间隔（秒）")
    w.add_argument("--database-url", default="")
    w.set_defaults(func=cmd_worker)

    mg = sub.add_parser("migrate", help="执行数据库迁移")
    mg.add_argument("--database-url", default="")
    mg.set_defaults(func=cmd_migrate)

    sub.add_parser("config").set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
