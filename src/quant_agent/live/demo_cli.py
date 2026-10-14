"""`qrun demo`：在本机用交易所模拟盘跑策略包。

和平台的模拟盘执行是同一套东西：运行内核用 external 成交模式只产出委托，
`DemoOMS` 负责事前风控、送单、查单、对账，回报再交回内核记账。
密钥只从环境变量读，不落盘；只接受模拟环境，没有实盘选项。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from . import demo_broker as DB
from .oms import DemoOMS, OmsLimits


def _creds(exchange: str) -> dict[str, str]:
    creds = {
        "api_key": os.environ.get("QRUN_API_KEY", "").strip(),
        "api_secret": os.environ.get("QRUN_API_SECRET", "").strip(),
        "passphrase": os.environ.get("QRUN_PASSPHRASE", "").strip(),
    }
    need = ["api_key", "api_secret"] + (["passphrase"] if exchange == "okx" else [])
    missing = ["QRUN_" + k.upper() for k in need if not creds[k]]
    if missing:
        raise SystemExit("缺少环境变量：" + "、".join(missing) + "（填交易所模拟盘的 API Key）")
    return creds


def _print_reports(reports: list[dict[str, Any]]) -> None:
    for r in reports:
        if r.get("status") == "rejected":
            print(f"  拒单 {r.get('side', '')} {r.get('security', '')}：{r.get('message', '')}")


def run_demo(
    args: Any,
    load: Callable,
    module: Callable,
    market_bars: Callable,
    make_feed: Callable,
    *,
    broker: DB.DemoBroker | None = None,
) -> int:
    from ..engine.live import LIVE_WARMUP_BARS, LiveRunner
    from ..sdk.runtimes import RuntimeCost

    rep = load(args.package)
    if not rep.importable:
        for c in rep.checks:
            print(f"  {'✓' if c.ok else '✗'} {c.name}：{c.detail}")
        return 1
    env = args.env or "demo"
    if (args.exchange, env) not in DB.ENVIRONMENTS:
        raise SystemExit(f"不支持的模拟环境：{args.exchange}/{env}")
    meta = DB.ENVIRONMENTS[(args.exchange, env)]
    broker = broker or DB.make_broker(args.exchange, env, _creds(args.exchange))
    tf = str(rep.manifest.get("timeframe") or "1h")
    securities = list(rep.data) or ["BTC/USDT"]
    quote = securities[0].split("/")[-1].split(":")[0]
    has_perp = any(DB.is_perp(s) for s in securities)
    broker.use_futures = has_perp
    oms = DemoOMS(broker, OmsLimits(max_order_notional=args.max_order), initial_cash=args.cash)
    try:
        bal = oms.snapshot_baseline()
        have = float(bal.get(quote) or 0)
        print(f"已连接 {meta['label']}（{meta['base']}），{quote} 余额 {have:.2f}")
        if not meta.get("realistic"):
            print(f"  注意：{meta['note']}")
        if have < args.cash:
            print(f"余额少于 --cash {args.cash:.2f}，请调低 --cash 或在交易所重置模拟资金")
            return 1
        print("加载预热行情…")
        history = market_bars(securities, tf, LIVE_WARMUP_BARS, args.exchange)
        live = LiveRunner()
        sid = f"qrun-demo-{int(time.time())}"
        live.start(
            sid,
            module(rep),
            history,
            timeframe=tf,
            initial_cash=args.cash,
            cost=RuntimeCost(taker_fee=args.fee, slippage_bps=args.slippage_bps),
            started_at=int(time.time() * 1000),
            fill_mode="external",
        )
        feed = make_feed(args.exchange, securities, tf)
        deadline = time.time() + args.minutes * 60
        print(f"开始模拟盘（{feed.describe()}；成交走 {meta['label']}），按 Ctrl+C 结束")
        try:
            while time.time() < deadline:
                latest = feed.closed_bars(wait=min(args.poll, max(1.0, deadline - time.time())))
                if not latest:
                    continue
                out = live.step(sid, latest, marks=feed.marks())
                fills = list(out["fills"])
                outbox, awaiting = out.get("outbox") or [], live.awaiting(sid) if out.get("awaiting") else []
                protects = out.get("protects") or []
                for _ in range(3):
                    if not outbox and not awaiting and not protects:
                        break
                    reports = oms.process(outbox, awaiting, protects)
                    protects = []
                    _print_reports(reports)
                    if not reports:
                        break
                    res = live.apply_reports(sid, reports, marks=feed.marks())
                    fills += res["fills"]
                    out["equity"] = res["equity"]
                    outbox, awaiting = res.get("outbox") or [], []
                for f in fills:
                    print(f"  成交 {f['side']} {f['security']} {float(f['amount']):.6f} @ {float(f['price']):.4f}")
                for e in out["errors"]:
                    print("  错误：", e)
                reason = oms.observe_equity(float(out.get("equity") or 0))
                if reason:
                    print("  " + reason)
                if out["new_bars"]:
                    print(f"  {time.strftime('%H:%M:%S')} 新 bar {out['new_bars']} 根，权益 {out['equity']:.2f}")
        except KeyboardInterrupt:
            print("已停止（持仓留在模拟账户里，没有自动平仓）")
        finally:
            feed.close()
        rt = live._states[sid].runtime
        try:
            rec = oms.reconcile(
                rt.ledger.cash(),
                {k: h.qty for k, h in rt.ledger.holdings.items()},
                quote=quote,
                perps={k: p.qty for k, p in rt.ledger.perps.items() if p.qty} if has_perp else None,
                margin=sum((p.margin for p in rt.ledger.perps.values()), Decimal(0)),
            )
            print(
                "对账一致"
                if rec["ok"]
                else "对账不一致："
                + "；".join(
                    f"{r['asset']} 应有 {Decimal(r['expected']):.6g}，交易所 {Decimal(r['actual']):.6g}"
                    for r in rec["rows"]
                    if not r["ok"] and not r.get("soft")
                )
            )
        except Exception as exc:  # 结束时对账失败不影响退出
            print(f"对账失败：{exc}")
        st = oms.state.stats
        print(
            f"结束：权益 {rt.ctx.portfolio.total_value:.2f}，成交 {len(rt.fills)} 笔，"
            f"送单 {st.get('sent', 0)}，拒单 {st.get('rejected', 0)}"
        )
        return 0
    finally:
        broker.close()
