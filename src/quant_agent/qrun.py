"""qrun —— 不依赖平台服务的策略运行器（命令行）。

和平台用的是**同一个运行内核**（`sdk.runtimes.BacktestRuntime` + `sdk.runner.StrategyRunner`），
所以同一个 .qstrat 在这里跑出的结果和平台上一致。团队成员可以在自己电脑上用任意 AI 编程工具
写策略、本地验证，再把 .qstrat 导入平台。

    qrun inspect  my.qstrat                 检查包：格式、安全检查、重跑标准答案
    qrun backtest my.qstrat                 用包内数据片段回测（离线即可）
    qrun backtest my.qstrat --market        用交易所公开行情回测（需联网）
    qrun paper    my.qstrat --minutes 60    用实时行情做纸面模拟（需联网）
    qrun demo     my.qstrat --exchange okx  交易所模拟盘（密钥从环境变量读取，见 --help）

注意：qrun 在本机进程里执行策略代码（只做静态安全检查，不做进程隔离）。
只运行你信任来源的策略包；来源不明的包请导入平台，平台会在隔离子进程里执行。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .strategies import package as P


def _load(path: str) -> P.Inspection:
    data = Path(path).read_bytes()
    return P.inspect(data, run=False)


def _print_checks(rep: P.Inspection) -> None:
    for c in rep.checks:
        mark = "✓" if c.ok else ("!" if c.level == "warning" else "✗")
        print(f"  {mark} {c.name}：{c.detail}")


def cmd_inspect(args: argparse.Namespace) -> int:
    import os

    os.environ.setdefault("QAGENT_SANDBOX", "inprocess")
    rep = P.inspect(Path(args.package).read_bytes(), run=True)
    m = rep.manifest
    print(f"{m.get('name')}  v{m.get('version') or '?'}  作者 {m.get('author') or '未署名'}")
    print(f"标的 {'、'.join(m.get('securities') or [])}  周期 {m.get('timeframe')}  SDK v{m.get('sdk_api')}")
    _print_checks(rep)
    print("可以导入" if rep.importable else "不能导入", "· 可以上模拟盘" if rep.demo_ready else "· 不能上模拟盘")
    return 0 if rep.importable else 1


def _module(rep: P.Inspection):
    from .strategies.lang import issues, load

    lang = rep.manifest.get("lang") or "python"
    found = issues(lang, rep.source)
    if found:
        raise SystemExit("安全检查未通过：" + "；".join(found))
    return load(lang, rep.source, name=str(rep.manifest.get("slug") or "strategy"), params=rep.params or None)


def _market_bars(securities: list[str], timeframe: str, limit: int, exchange: str) -> dict[str, list]:
    from .data.service import MarketDataService
    from .exchanges import public_adapter

    svc = MarketDataService(adapters={exchange: public_adapter(exchange)})
    out = {}
    for s in securities:
        bars, source = svc.get_bars(s, timeframe, limit=limit, exchange=exchange, source="exchange")
        out[s] = bars
        print(f"  {s}：{len(bars)} 根（{source}）")
    return out


def _market_funding(bars: dict[str, list], exchange: str) -> dict:
    """永续合约的历史资金费率（公共接口），回测按结算时刻逐笔结算。"""
    from .data.service import MarketDataService
    from .exchanges import public_adapter

    svc = MarketDataService(adapters={exchange: public_adapter(exchange)})
    out = {}
    for s, rows in bars.items():
        if not s.upper().endswith(":PERP") or not rows:
            continue
        got, label, err = svc.funding_rates(
            s, exchange=exchange, start=rows[0].open_time, end=rows[-1].close_time, source="exchange"
        )
        if got:
            out[f"funding:{s}"] = {"times": [t for t, _ in got], "rates": [r for _, r in got], "source": label}
            print(f"  {s} 资金费率：{len(got)} 条")
        else:
            print(f"  {s} 资金费率取不到（{err}），按 0 计")
    return out


def cmd_backtest(args: argparse.Namespace) -> int:
    from .sdk.runtimes import RuntimeCost, run_backtest

    rep = _load(args.package)
    if not rep.importable:
        _print_checks(rep)
        return 1
    tf = str(rep.manifest.get("timeframe") or "1h")
    research: dict = {}
    if args.market:
        print("拉取交易所公开行情…")
        bars = _market_bars(list(rep.data) or ["BTC/USDT"], tf, args.bars, args.exchange)
        research = _market_funding(bars, args.exchange)
    else:
        bars = rep.data
        print(f"使用包内数据片段（{sum(len(v) for v in bars.values())} 根，确定性行情，只用于核对，不代表策略好坏）")
    res = run_backtest(
        _module(rep),
        bars,
        initial_cash=args.cash,
        cost=RuntimeCost(taker_fee=args.fee, slippage_bps=args.slippage_bps) if args.market else None,
        warmup_bars=P.GOLDEN_WARMUP,
        timeframe=tf,
        research=research or None,
    )
    body = res.to_dict(max_points=None, max_rows=100_000)
    m = body.get("metrics") or {}
    fp = P.fingerprint(body)
    print(
        f"成交 {fp['fills']} 笔　期末权益 {fp['final_equity']}　总收益 {m.get('total_return', 0):+.2%}"
        f"　最大回撤 {m.get('max_drawdown', 0):.2%}"
    )
    if not args.market and rep.golden.get("hash"):
        same = fp["hash"] == rep.golden["hash"] and rep.code_match
        print("与作者的标准答案一致" if same else "与作者的标准答案不一致")
    for e in body.get("errors") or []:
        print("  错误：", e)
    if args.json:
        Path(args.json).write_text(json.dumps(body, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"完整结果已写入 {args.json}")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    """纸面模拟：真实行情 + 平台撮合。优先用 WebSocket 推送，失败自动退回 REST 轮询。"""
    from .engine.live import LIVE_WARMUP_BARS, LiveRunner
    from .sdk.runtimes import RuntimeCost

    rep = _load(args.package)
    if not rep.importable:
        _print_checks(rep)
        return 1
    tf = str(rep.manifest.get("timeframe") or "1h")
    securities = list(rep.data) or ["BTC/USDT"]
    print("加载预热行情…")
    history = _market_bars(securities, tf, LIVE_WARMUP_BARS, args.exchange)
    live = LiveRunner()
    sid = f"qrun-{int(time.time())}"
    live.start(
        sid,
        _module(rep),
        history,
        timeframe=tf,
        initial_cash=args.cash,
        cost=RuntimeCost(taker_fee=args.fee, slippage_bps=args.slippage_bps),
        started_at=int(time.time() * 1000),
    )
    feed = _feed(args.exchange, securities, tf)
    deadline = time.time() + args.minutes * 60
    print(f"开始纸面模拟（{feed.describe()}），按 Ctrl+C 结束")
    try:
        while time.time() < deadline:
            latest = feed.closed_bars(wait=min(args.poll, max(1.0, deadline - time.time())))
            if not latest:
                continue
            out = live.step(sid, latest, marks=feed.marks())
            for f in out["fills"]:
                print(
                    f"  成交 {time.strftime('%m-%d %H:%M', time.localtime(f['ts'] / 1000))} "
                    f"{f['side']} {f['security']} {float(f['amount']):.6f} @ {float(f['price']):.4f}"
                )
            for e in out["errors"]:
                print("  错误：", e)
            if out["new_bars"]:
                print(f"  {time.strftime('%H:%M:%S')} 新 bar {out['new_bars']} 根，权益 {out['equity']:.2f}")
    except KeyboardInterrupt:
        print("已停止")
    finally:
        feed.close()
    st = live._states[sid]
    print(f"结束：权益 {st.runtime.ctx.portfolio.total_value:.2f}，成交 {len(st.runtime.fills)} 笔")
    return 0


class _PollFeed:
    """REST 轮询行情：每次取最近几根已收盘 bar。"""

    def __init__(self, exchange: str, securities: list[str], timeframe: str) -> None:
        from .exchanges import public_adapter

        self.adapter = public_adapter(exchange)
        self.securities, self.timeframe = securities, timeframe

    def describe(self) -> str:
        return "REST 轮询"

    def closed_bars(self, wait: float) -> dict[str, list]:
        from .data.quality import TIMEFRAME_MS

        time.sleep(wait)
        step = TIMEFRAME_MS.get(self.timeframe, 3_600_000)
        now = int(time.time() * 1000)
        return {
            s: [b for b in self.adapter.fetch_bars(s, self.timeframe, now - 5 * step, now) if b.closed]
            for s in self.securities
        }

    def marks(self) -> dict[str, float]:
        return {}

    def close(self) -> None:
        pass


def _feed(exchange: str, securities: list[str], timeframe: str) -> Any:
    try:
        from .data.stream import StreamFeed

        feed = StreamFeed(exchange, securities, timeframe)
        feed.start()
        return feed
    except Exception as exc:  # 没有 websockets 库或连不上：退回轮询
        print(f"  WebSocket 不可用（{exc}），改用 REST 轮询")
        return _PollFeed(exchange, securities, timeframe)


def cmd_demo(args: argparse.Namespace) -> int:
    from .live.demo_cli import run_demo

    return run_demo(args, _load, _module, _market_bars, _feed)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qrun", description="策略运行器：和平台同一个运行内核")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("inspect", help="检查策略包")
    i.add_argument("package")
    i.set_defaults(func=cmd_inspect)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("package")
        sp.add_argument("--exchange", default="binance", choices=["binance", "okx", "bybit"])
        sp.add_argument("--cash", type=float, default=10_000.0)
        sp.add_argument("--fee", type=float, default=0.001)
        sp.add_argument("--slippage-bps", type=float, default=5.0)

    b = sub.add_parser("backtest", help="回测")
    common(b)
    b.add_argument("--market", action="store_true", help="用交易所公开行情，而不是包内数据片段")
    b.add_argument("--bars", type=int, default=1500)
    b.add_argument("--json", help="把完整结果写到这个文件")
    b.set_defaults(func=cmd_backtest)

    pp = sub.add_parser("paper", help="纸面模拟（真实行情 + 平台撮合）")
    common(pp)
    pp.add_argument("--minutes", type=float, default=60)
    pp.add_argument("--poll", type=float, default=5.0, help="检查新 bar 的间隔（秒）")
    pp.set_defaults(func=cmd_paper)

    d = sub.add_parser(
        "demo",
        help="交易所模拟盘",
        description="交易所模拟盘：真实行情 + 交易所模拟账户。密钥从环境变量读取："
        "QRUN_API_KEY、QRUN_API_SECRET（OKX 另需 QRUN_PASSPHRASE）。只接受模拟盘环境，没有实盘选项。",
    )
    common(d)
    d.add_argument("--minutes", type=float, default=60)
    d.add_argument("--poll", type=float, default=5.0)
    d.add_argument("--env", default="", help="binance: demo（默认）或 testnet；okx 只有模拟盘")
    d.add_argument("--max-order", type=float, default=1000.0, help="单笔最大金额（计价币）")
    d.set_defaults(func=cmd_demo)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
