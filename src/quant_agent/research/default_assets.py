"""工作区内置研究资产。

内置资产不是硬编码的“已上线”记录：因子、模型、组合会先跑平台自己的评估器，
策略会先经过源码检查、平台回测与研究审查，只有检查通过的记录才会被写入。
教学行情由固定种子生成，报告会明确标记为合成数据，不能据此判断真实策略优劣。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import numpy as np

from ..data.quality import DataSet
from ..engine.research import BacktestSpec
from ..exchanges.base import Bar
from ..platform import context
from ..platform.runs import COMPLETED, FAILED
from ..sdk import check_source, declared_params
from ..store import Collections
from ..strategies.repo import StrategyRepository
from ..strategies.sdk_templates import get_template
from . import factor_expr as FX
from .factor_eval import evaluate_factor
from .factor_lib import FactorLibrary, expr_hash
from .model_lib import ModelLibrary
from .models import normalize_spec, train_and_evaluate
from .panel import UNIVERSES, Panel
from .portfolio import evaluate_scheme, normalize_config
from .portfolio_lib import PortfolioLibrary

logger = logging.getLogger("quantlab.default_assets")

SEED_VERSION = "builtin-assets-v1"
SEED_SETTING_ID = "builtin_assets_v1"
TEACHING_DATASET_ID = "builtin-teaching-v1"
SYMBOLS = UNIVERSES["major20"]["symbols"][:12]
TIMEFRAME = "4h"
PANEL_BARS = 1800
STRATEGY_BARS = 4000
_LOCK = threading.Lock()

_FACTORS = (
    {
        "id": "builtin_factor_reversal_5",
        "name": "内置 · 5期短期反转",
        "expression": "-returns(5)",
        "hypothesis": {
            "category": "microstructure",
            "rationale": "短期价格冲击更多来自流动性需求，冲击结束后价格会部分回吐",
            "direction": "过去 5 期相对强势的标的，下一期相对跑输",
            "falsify": "扣除成本后样本外 RankIC 不显著，或与库内反转因子高度重复",
        },
        "tags": ["内置", "反转", "流动性"],
    },
    {
        "id": "builtin_factor_momentum_30_5",
        "name": "内置 · 中期动量（剔除短期）",
        "expression": "returns(30) - returns(5)",
        "hypothesis": {
            "category": "behavioral",
            "rationale": "信息扩散和羊群行为使中期趋势延续，剔除最近 5 期以避开短期反转",
            "direction": "过去 30 期较强且最近 5 期未过热的标的继续跑赢",
            "falsify": "样本外方向反转，或与短期反转因子无法区分",
        },
        "tags": ["内置", "动量", "趋势"],
    },
    {
        "id": "builtin_factor_low_vol",
        "name": "内置 · 低波动异象",
        "expression": "-ts_std(ret, 30)",
        "hypothesis": {
            "category": "behavioral",
            "rationale": "彩票偏好可能让投资者为高波动资产支付过高价格，低波动资产风险调整后更优",
            "direction": "过去 30 期波动较低的标的相对跑赢",
            "falsify": "控制市场贝塔后样本外不再显著",
        },
        "tags": ["内置", "低波动", "风险溢价"],
    },
)

_MODELS = (
    {
        "id": "builtin_model_ridge_multi",
        "name": "内置 · 线性多因子收益模型",
        "model": "ridge",
        "label": "fwd_ret",
        "features": ["-returns(5)", "returns(30) - returns(5)", "-ts_std(ret, 30)"],
        "params": {},
        "note": "短期反转、中期动量与低波动三类特征的线性基线，适合作为后续模型的对照",
    },
    {
        "id": "builtin_model_garch_vol",
        "name": "内置 · GARCH 波动率模型",
        "model": "garch",
        "label": "volatility",
        "features": [],
        "params": {},
        "note": "波动聚集基线；用于仓位缩放和风险预算，不用于预测收益方向",
    },
)

_PORTFOLIOS = (
    {
        "id": "builtin_portfolio_risk_parity",
        "name": "内置 · 主流币风险平价",
        "config": {
            "optimizer": "risk_parity",
            "risk_model": "ledoit_wolf",
            "lookback": 120,
            "rebalance": 6,
        },
        "note": "等风险贡献的主流币配置，作为稳健的多资产起点",
    },
    {
        "id": "builtin_portfolio_min_var",
        "name": "内置 · 主流币最小方差",
        "config": {
            "optimizer": "min_var",
            "risk_model": "ledoit_wolf",
            "lookback": 120,
            "rebalance": 6,
            "constraints": {"max_weight": 0.3},
        },
        "note": "在约束下最小化组合波动，作为风险平价方案的对照",
    },
)

_STRATEGIES = (
    {
        "slug": "builtin-ma-cross",
        "name": "内置 · 双均线趋势",
        "template": "ma_cross",
        "notes": "平台内置示例：快慢均线交叉、成本止损与仓位上限。",
    },
    {
        "slug": "builtin-donchian-breakout",
        "name": "内置 · 唐奇安通道突破",
        "template": "donchian",
        "notes": "平台内置示例：突破 N 日高点入场，跌破 M 日低点或 ATR 止损离场。",
    },
)


def _enabled() -> bool:
    return os.environ.get("QAGENT_SEED_BUILTINS", "1").strip().lower() not in ("0", "false", "off", "no")


def teaching_panel(*, bars: int = PANEL_BARS, symbols: list[str] | None = None, seed: int = 20261018) -> Panel:
    """固定种子的教学面板：含短期反转、中期动量、低波动和波动聚集信号。"""
    rng = np.random.default_rng(seed)
    names = list(symbols or SYMBOLS)
    n_symbols = len(names)
    base_vol = rng.uniform(0.006, 0.018, n_symbols)
    log_vol = np.zeros((bars, n_symbols))
    log_vol[0] = np.log(base_vol)
    for t in range(1, bars):
        log_vol[t] = 0.95 * log_vol[t - 1] + 0.05 * np.log(base_vol) + rng.normal(0.0, 0.05, n_symbols)
    vol = np.exp(log_vol)
    drift = 0.002 * (0.012 / base_vol)
    ret = rng.normal(0.0, 1.0, (bars, n_symbols)) * vol + drift
    ret += 0.0015 * np.sin(2 * np.pi * np.arange(bars)[:, None] / 40)
    for t in range(6, bars):
        past = ret[t - 5 : t].sum(axis=0)
        ret[t] -= 0.12 * (past - past.mean())

    close = 100.0 * np.exp(np.cumsum(ret, axis=0))
    open_ = np.vstack([close[:1], close[:-1]])
    high = np.maximum(open_, close) * (1.001 + rng.uniform(0.0, 0.002, (bars, n_symbols)))
    low = np.minimum(open_, close) * (0.997 + rng.uniform(0.0, 0.002, (bars, n_symbols)))
    volume = rng.uniform(100.0, 200.0, (bars, n_symbols))
    data = {
        "close": close,
        "open": open_,
        "high": high,
        "low": low,
        "volume": volume,
        "amount": close * volume,
        "vwap": close,
        "ret": close / np.vstack([np.full((1, n_symbols), np.nan), close[:-1]]) - 1,
    }
    with FX._quiet():
        data["mkt_ret"] = np.repeat(np.nanmean(data["ret"], axis=1, keepdims=True), n_symbols, axis=1)
    times = np.arange(bars, dtype=np.int64) * 14_400_000 + 1_700_000_000_000
    return Panel(
        times,
        names,
        data,
        TIMEFRAME,
        {symbol: "synthetic" for symbol in names},
        {"close/open/high/low/volume": "内置教学合成数据"},
        ["内置教学数据只用于验证平台流程，不能作为真实研究结论"],
    )


def _seed_factors(store: Any, panel: Panel) -> dict[str, Any]:
    lib = FactorLibrary(store)
    created: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for item in _FACTORS:
        fid = item["id"]
        if lib.get(fid) or lib.by_name(item["name"]):
            skipped.append(fid)
            continue
        expr = str(FX.parse(item["expression"]))
        trials = lib.trial_count() + (0 if store.get("research_trials", expr_hash(expr)) else 1)
        try:
            report = evaluate_factor(
                expr,
                panel,
                horizon=1,
                cost_bps=10.0,
                library=lib.library_values(panel, exclude=fid),
                trials=max(1, trials),
            )
        except Exception as exc:  # 单个默认资产失败不应阻断工作区初始化
            errors.append(f"factor:{fid}:{type(exc).__name__}:{exc}")
            continue
        lib.record_trial(expr, report["summary"], source="builtin")
        report["expression"] = expr
        report["settings"] = {
            "symbols": panel.symbols,
            "timeframe": panel.timeframe,
            "horizon": 1,
            "bars_limit": len(panel.times),
            "source": "synthetic",
            "cost_bps": 10.0,
            "builtin": True,
        }
        report["builtin_review"] = {"version": SEED_VERSION, "mechanism": "factor_evaluate"}
        if not report.get("admitted"):
            failed = [c["key"] for c in report.get("checks") or [] if c.get("required") and not c.get("ok")]
            errors.append(f"factor:{fid}:admission:{','.join(failed)}")
            continue
        lib.save(
            {
                "id": fid,
                "name": item["name"],
                "expression": expr,
                "hypothesis": item["hypothesis"],
                "status": "candidate",
                "universe": panel.symbols,
                "timeframe": panel.timeframe,
                "horizon": 1,
                "tags": item["tags"],
                "created_by": "builtin",
                "note": "平台内置示例；基于固定种子的教学合成数据通过因子审核。",
            },
            report=report,
        )
        created.append(fid)
    return {"created": created, "skipped": skipped, "errors": errors}


def _seed_models(store: Any, panel: Panel) -> dict[str, Any]:
    lib = ModelLibrary(store)
    created: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for item in _MODELS:
        mid = item["id"]
        if lib.get(mid) or lib.by_name(item["name"]):
            skipped.append(mid)
            continue
        try:
            spec = normalize_spec(
                {
                    "model": item["model"],
                    "label": item["label"],
                    "features": item["features"],
                    "params": item["params"],
                    "symbols": panel.symbols,
                    "timeframe": panel.timeframe,
                    "bars_limit": len(panel.times),
                    "source": "synthetic",
                    "folds": 4,
                    "seed": 7,
                }
            )
            out = train_and_evaluate(spec, panel, trials=lib.trial_count() + 1)
            report = out["report"]
            run = lib.record_run(spec, report, out["state"], source="builtin", note=item["note"])
        except Exception as exc:
            errors.append(f"model:{mid}:{type(exc).__name__}:{exc}")
            continue
        if not report.get("admitted"):
            failed = [c["key"] for c in report.get("checks") or [] if c.get("required") and not c.get("ok")]
            errors.append(f"model:{mid}:admission:{','.join(failed)}")
            continue
        lib.save_from_run(
            run["id"],
            name=item["name"],
            status="candidate",
            note=item["note"] + "；平台内置示例，基于固定种子的教学合成数据通过模型审核。",
            model_id=mid,
            created_by="builtin",
        )
        created.append(mid)
    return {"created": created, "skipped": skipped, "errors": errors}


def _seed_portfolios(store: Any, panel: Panel) -> dict[str, Any]:
    lib = PortfolioLibrary(store)
    created: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for item in _PORTFOLIOS:
        sid = item["id"]
        if lib.get(sid) or lib.by_name(item["name"]):
            skipped.append(sid)
            continue
        try:
            cfg = normalize_config(item["config"])
            report = evaluate_scheme(cfg, panel, cost_bps=10.0)
            run = lib.record_run(
                cfg,
                report,
                symbols=panel.symbols,
                timeframe=panel.timeframe,
                source="builtin",
                note=item["note"],
            )
        except Exception as exc:
            errors.append(f"portfolio:{sid}:{type(exc).__name__}:{exc}")
            continue
        if not report.get("admitted"):
            failed = [c["key"] for c in report.get("checks") or [] if c.get("required") and not c.get("ok")]
            errors.append(f"portfolio:{sid}:admission:{','.join(failed)}")
            continue
        lib.save(
            name=item["name"],
            config=cfg,
            report=report,
            symbols=panel.symbols,
            timeframe=panel.timeframe,
            status="candidate",
            note=item["note"] + "；平台内置示例，基于固定种子的教学合成数据通过组合审核。",
            scheme_id=sid,
            run_id=run["id"],
            created_by="builtin",
        )
        created.append(sid)
    return {"created": created, "skipped": skipped, "errors": errors}


def _teaching_dataset(store: Any) -> dict[str, Any]:
    saved = store.get(Collections.DATASETS, TEACHING_DATASET_ID)
    if saved:
        return saved
    rng = np.random.default_rng(20261019)
    t = np.arange(STRATEGY_BARS)
    close = 100.0 * np.exp(
        0.18 * np.sin(2 * np.pi * t / 80)
        + 0.025 * np.sin(2 * np.pi * t / 17)
        + 0.00002 * t
        + np.cumsum(rng.normal(0.0, 0.003, STRATEGY_BARS))
    )
    open_ = np.r_[close[0], close[:-1]] * (1.0 + rng.normal(0.0, 0.001, STRATEGY_BARS))
    high = np.maximum(open_, close) * (1.001 + rng.uniform(0.0, 0.002, STRATEGY_BARS))
    low = np.minimum(open_, close) * (0.997 + rng.uniform(0.0, 0.002, STRATEGY_BARS))
    volume = 1000.0 * (1.0 + rng.uniform(0.0, 0.4, STRATEGY_BARS))
    step = 14_400_000
    start = 1_700_000_000_000
    bars: list[Bar] = [
        Bar(
            symbol="BTC/USDT",
            timeframe=TIMEFRAME,
            open_time=start + i * step,
            close_time=start + (i + 1) * step,
            open=float(open_[i]),
            high=float(high[i]),
            low=float(low[i]),
            close=float(close[i]),
            volume=float(volume[i]),
        )
        for i in range(STRATEGY_BARS)
    ]
    ds = DataSet("BTC/USDT", TIMEFRAME, bars, source="csv").freeze("内置教学合成数据")
    rec = {
        "id": TEACHING_DATASET_ID,
        "filename": "内置教学数据-4h.csv",
        "symbol": "BTC/USDT",
        "timeframe": TIMEFRAME,
        "content": ds.to_csv(),
        "builtin_teaching": True,
        "created_by": "builtin",
        **ds.summary(),
    }
    return store.upsert(Collections.DATASETS, rec)


def _run_strategy_backtest(
    services: Any,
    *,
    slug: str,
    name: str,
    code: str,
    version_id: str,
    dataset_id: str,
    sequence: int,
) -> dict[str, Any]:
    from ..api import tasks
    from ..platform.worker import TaskCtl

    spec = BacktestSpec(
        securities=["BTC/USDT"],
        timeframe=TIMEFRAME,
        initial_cash=10_000.0,
        bars_limit=STRATEGY_BARS,
        source="csv",
    )
    kind = f"builtin_review_{sequence}"
    manifest = {
        "lang": "python",
        "source": code,
        "module_name": slug,
        "name": name,
        "slug": slug,
        "spec": spec.to_dict(),
        "persist": True,
        "label": "内置策略审核",
        "dataset_id": dataset_id,
        "mark_backtested": True,
        "version": "builtin",
    }
    run = services.runs.create(
        workspace_id=context.current_workspace(),
        user_id=context.current().user_id or "system",
        kind=kind,
        mode="backtest",
        title=f"内置审核 · {name}",
        manifest=manifest,
        strategy_version_id=version_id,
    )
    owner = f"builtin-review-{sequence}-{run['id'][-8:]}"
    claimed = services.runs.claim(owner, kinds=[kind])
    if claimed is None:
        raise RuntimeError("内置策略审核任务未能启动")
    ctl = TaskCtl(run=claimed, runs=services.runs, owner=owner, services=services)
    try:
        summary = tasks.handle_backtest(ctl)
    except Exception as exc:
        services.runs.set_state(
            ctl.run_id,
            owner,
            FAILED,
            error=f"{type(exc).__name__}: {exc}",
            error_code="BUILTIN_REVIEW_FAILED",
        )
        raise
    state = COMPLETED if summary.get("status") == "done" else FAILED
    services.runs.set_state(ctl.run_id, owner, state, summary=summary)
    return summary


def _seed_strategies(services: Any, store: Any) -> dict[str, Any]:
    from ..api.routes_dsl import register_version
    from ..api.routes_studio import approve_strategy

    repo = StrategyRepository(store.root / "strategies")
    dataset = _teaching_dataset(store)
    created: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    for idx, item in enumerate(_STRATEGIES, start=1):
        slug = item["slug"]
        tpl = get_template(item["template"])
        if tpl is None:
            errors.append(f"strategy:{slug}:template_missing")
            continue
        rec = repo.get(slug)
        if rec is not None:
            # 已完成的不重复写；上一次中途失败且源码仍是内置原版时允许续跑。
            finished = rec.meta.source == "builtin" and rec.meta.human_approved and rec.meta.status == "backtested"
            resumable = rec.meta.source == "builtin" and rec.source == tpl.code and not rec.meta.human_approved
            if finished or not resumable:
                skipped.append(slug)
                continue
        else:
            issues = check_source(tpl.code)
            if issues:
                errors.append(f"strategy:{slug}:code_check:{';'.join(issues)}")
                continue
            rec = repo.save(
                name=item["name"],
                source=tpl.code,
                params=declared_params(tpl.code),
                team="平台内置",
                kind="code",
                origin="builtin",
                symbol="BTC/USDT",
                timeframe=TIMEFRAME,
                securities=["BTC/USDT"],
                notes=item["notes"],
                slug=slug,
                author="平台内置",
            )
        try:
            version = register_version(rec)
            summary = _run_strategy_backtest(
                services,
                slug=slug,
                name=item["name"],
                code=tpl.code,
                version_id=version["id"],
                dataset_id=dataset["id"],
                sequence=idx,
            )
        except Exception as exc:
            errors.append(f"strategy:{slug}:backtest:{type(exc).__name__}:{exc}")
            continue
        row = summary.get("row") or {}
        if summary.get("status") != "done" or (row.get("review") or {}).get("blocks_promotion"):
            errors.append(f"strategy:{slug}:review_blocked")
            continue
        approve_strategy(slug)
        repo.update_meta(slug, status="backtested")
        created.append(slug)
    return {"created": created, "skipped": skipped, "errors": errors}


def seed_default_assets(
    services: Any,
    store: Any,
    workspace_id: str,
    *,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """为工作区写入默认研究资产；固定 ID、幂等，不覆盖用户记录。"""
    if not _enabled():
        return {"enabled": False, "complete": True}
    marker = store.get(Collections.SETTINGS, SEED_SETTING_ID)
    if marker and marker.get("complete"):
        return marker
    with _LOCK:
        marker = store.get(Collections.SETTINGS, SEED_SETTING_ID)
        if marker and marker.get("complete"):
            return marker
        owner = owner_id or "system"
        with context.bind(workspace_id, user_id=owner, role="admin"):
            panel = teaching_panel()
            result = {
                "version": SEED_VERSION,
                "workspace_id": workspace_id,
                "factors": _seed_factors(store, panel),
                "models": _seed_models(store, panel),
                "portfolios": _seed_portfolios(store, panel),
                "strategies": _seed_strategies(services, store),
            }
        errors = [
            error
            for section in ("factors", "models", "portfolios", "strategies")
            for error in (result[section].get("errors") or [])
        ]
        result.update(
            complete=not errors,
            errors=errors,
            updated_at=time.time(),
        )
        if errors:
            logger.warning("内置资产仅部分写入：%s", "；".join(errors[:8]))
        store.upsert(Collections.SETTINGS, {"id": SEED_SETTING_ID, **result})
        return result


__all__ = ["SEED_SETTING_ID", "TEACHING_DATASET_ID", "seed_default_assets", "teaching_panel"]
