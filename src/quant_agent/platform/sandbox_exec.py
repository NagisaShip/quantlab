"""隔离子进程的入口：在受限进程里执行 Python 策略（回测 / 参数优化 / 实时模拟会话）。

父进程（worker）负责取数、鉴权、落库；这里只拿到“源码 + 行情 + 参数”，算完把结果写回 stdout。
这个进程：
- 环境变量被清空（没有数据库地址、没有加密密钥、没有交易所凭证）；
- 以 nobody 身份运行（父进程为 root 时），读不到密钥文件；
- 处在独立的网络命名空间（权限允许时），没有任何网络；
- 有 CPU 时间、内存、文件大小、文件句柄上限；
- 工作目录是一次性临时目录。

协议：stdin / stdout 每行一个 JSON。批处理：读一条请求，输出若干 {"progress":…} 与最终 {"result":…}。
会话（paper）：循环读请求，逐条回复 {"result":…} 或 {"error":…}。
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any


def _out(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def bars_from_wire(data: dict[str, list[list[Any]]]):
    from ..exchanges.base import Bar

    out = {}
    for sec, rows in data.items():
        out[sec] = [
            Bar(
                symbol=sec,
                timeframe=r[7] if len(r) > 7 else "1h",
                open_time=int(r[0]),
                close_time=int(r[1]),
                open=float(r[2]),
                high=float(r[3]),
                low=float(r[4]),
                close=float(r[5]),
                volume=float(r[6]),
                closed=True,
            )
            for r in rows
        ]
    return out


def bars_to_wire(bars: dict[str, list[Any]]) -> dict[str, list[list[Any]]]:
    return {
        sec: [[b.open_time, b.close_time, b.open, b.high, b.low, b.close, b.volume, b.timeframe] for b in rows]
        for sec, rows in bars.items()
    }


def _spec(d: dict[str, Any]):
    from ..engine.research import BacktestSpec

    allowed = BacktestSpec.__dataclass_fields__
    return BacktestSpec(**{k: v for k, v in d.items() if k in allowed})


def op_backtest(req: dict[str, Any]) -> dict[str, Any]:
    from ..engine.research import execute

    spec = _spec(req["spec"])
    result = execute(
        req["source"],
        name=req.get("name") or "strategy",
        bars=bars_from_wire(req["bars"]),
        spec=spec,
        params=req.get("params"),
        time_limit=req.get("time_limit") or 60.0,
        run_id=req.get("run_id") or "local",
        strategy_version_id=req.get("strategy_version_id") or "",
        lang=req.get("lang") or "python",
        research=req.get("research"),
    )
    return result.to_dict(max_points=req.get("max_points", 800), max_rows=req.get("max_rows", 2000))


def op_checkup(req: dict[str, Any], emit: Any = None) -> dict[str, Any]:
    """策略体检：截断重算、预热、成本翻倍、参数平台。研究数据由父进程按截断点算好传进来。"""
    from ..engine.checkup import run_checkup

    emit = emit or _out
    by_cut = {int(k): v for k, v in (req.get("research_by_cut") or {}).items()} if req.get("research_by_cut") else None
    return run_checkup(
        req["source"],
        name=req.get("name") or "strategy",
        bars=bars_from_wire(req["bars"]),
        spec=_spec(req["spec"]),
        lang=req.get("lang") or "python",
        params=req.get("params"),
        research=req.get("research"),
        research_by_cut=by_cut,
        progress=lambda d, t, label: emit({"progress": {"done": d, "total": t, "label": label}}),
    )


def op_stress(req: dict[str, Any], emit: Any = None) -> dict[str, Any]:
    """压力演练：行情由父进程取好传进来，这里只跑策略（子进程里没有密钥、也不需要网络）。"""
    from ..engine import stress

    emit = emit or _out
    return stress.run_stress(
        None,
        source=req["source"],
        params=req.get("params"),
        securities=req.get("securities"),
        timeframe=req.get("timeframe") or "1m",
        initial_cash=float(req.get("initial_cash") or 10_000),
        scenarios=req.get("scenarios"),
        faults=req.get("faults"),
        lang=req.get("lang") or "python",
        cost=req.get("cost"),
        risk=req.get("risk"),
        exchange=req.get("exchange") or "binance",
        preloaded=stress.data_from_wire(req["data"]),
        progress=lambda p: emit({"progress": p}),
    )


def op_optimize(req: dict[str, Any], emit: Any = None) -> dict[str, Any]:
    from ..engine.research import optimize, walk_forward

    spec = _spec(req["spec"])
    bars = bars_from_wire(req["bars"])
    done_rows = {json.dumps(r["params"], sort_keys=True): r for r in req.get("done_rows") or []}

    emit = emit or _out
    lang = req.get("lang") or "python"

    def progress(d, t):
        emit({"progress": [d, t]})

    def on_row(row):
        emit({"row": row})

    out = optimize(
        req["source"],
        name=req.get("name") or "strategy",
        bars=bars,
        spec=spec,
        grid=req["grid"],
        split=req.get("split", 0.7),
        objective=req.get("objective", "sharpe"),
        progress=progress,
        embargo_bars=req.get("embargo_bars", 0),
        holdout=req.get("holdout", 0.0),
        done_rows=done_rows,
        on_row=on_row,
        lang=lang,
        method=req.get("method") or "grid",
        space=req.get("space"),
        n_trials=int(req.get("n_trials") or 0),
        multi_objective=bool(req.get("multi_objective")),
    )
    if req.get("walk_forward_folds") and (req.get("method") or "grid") == "grid":
        out["walk_forward"] = walk_forward(
            req["source"],
            name=req.get("name") or "strategy",
            bars=bars,
            spec=spec,
            grid=req["grid"],
            folds=req["walk_forward_folds"],
            objective=req.get("objective", "sharpe"),
            embargo_bars=req.get("embargo_bars", 0),
            lang=lang,
        )
    return out


class PaperSessionHost:
    """在隔离进程里托管一个实时模拟运行时。"""

    def __init__(self) -> None:
        self.live = None
        self.sid = ""

    def start(self, req: dict[str, Any]) -> dict[str, Any]:
        from ..engine.live import LiveRunner
        from ..sdk.runtimes import RiskConfig, RuntimeCost
        from ..strategies.lang import load

        module = load(
            req.get("lang") or "python",
            req["source"],
            name=req.get("name") or "strategy",
            params=req.get("params") or None,
        )
        cost = RuntimeCost(
            **{k: v for k, v in (req.get("cost") or {}).items() if k in RuntimeCost.__dataclass_fields__}
        )
        self.live = LiveRunner()
        self.sid = req["run_id"]
        state = self.live.start(
            self.sid,
            module,
            bars_from_wire(req["history"]),
            timeframe=req["timeframe"],
            initial_cash=req["initial_cash"],
            cost=cost,
            started_at=req.get("started_at") or 0,
            risk=RiskConfig.from_dict(req.get("risk")),
            strategy_version_id=req.get("strategy_version_id") or "",
            fill_mode=req.get("fill_mode") or "internal",
        )
        if req.get("snapshot"):
            self.live.restore(self.sid, req["snapshot"])
        return {
            "last_bar_ts": state.last_bar_ts,
            "awaiting": self.live.awaiting(self.sid),
            "protects": list(state.runtime.protects.values()),
            "portfolio": state.runtime.ctx.portfolio.to_dict(),
            "params": dict(module.params),
            "snapshot": self.live.snapshot(self.sid),
        }

    def step(self, req: dict[str, Any]) -> dict[str, Any]:
        out = self.live.step(
            self.sid,
            bars_from_wire(req["latest"]),
            marks=req.get("marks") or None,
            call_strategy=req.get("call_strategy", True),
            funding=req.get("funding") or None,
            delistings=req.get("delistings") or None,
        )
        out["snapshot"] = self.live.snapshot(self.sid)
        return out

    def apply(self, req: dict[str, Any]) -> dict[str, Any]:
        out = self.live.apply_reports(self.sid, req.get("reports") or [], marks=req.get("marks") or None)
        out["snapshot"] = self.live.snapshot(self.sid)
        return out

    def liquidate(self, req: dict[str, Any]) -> dict[str, Any]:
        out = self.live.liquidate(self.sid, reason=req.get("reason") or "liquidate")
        out["snapshot"] = self.live.snapshot(self.sid)
        return out


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "batch"
    if mode == "batch":
        req = json.loads(sys.stdin.readline() or "{}")
        try:
            fn = {"backtest": op_backtest, "optimize": op_optimize, "stress": op_stress, "checkup": op_checkup}[
                req.get("op")
            ]
            _out({"result": fn(req)})
        except Exception as exc:
            _out(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "kind": type(exc).__name__,
                    "trace": traceback.format_exc(limit=3)[-1500:],
                }
            )
        return 0
    host = PaperSessionHost()
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        op = req.get("op")
        if op == "exit":
            break
        try:
            fn = {"start": host.start, "step": host.step, "apply": host.apply, "liquidate": host.liquidate}[op]
            _out({"result": fn(req)})
        except Exception as exc:
            _out({"error": f"{type(exc).__name__}: {exc}", "kind": type(exc).__name__})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
