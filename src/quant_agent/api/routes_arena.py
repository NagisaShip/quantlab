"""统一赛道、团队比较、CSV 数据与规则策略的路由。

对应 plan.md 第 4 章（团队公平比较）与第 5 章（观察口径）。
重点是所有查询都必须显式带上"这是哪个批次、哪个窗口、什么数据来源"，
不允许把不同赛道的结果混成一个排名。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

from ..data.quality import DataError, DataSet, parse_csv, sample_csv
from ..engine.arena import DEFAULT_TRACK, Arena, Entrant
from ..engine.windows import WARMUP_BARS, window_status_text
from ..store import Collections
from ..strategies.rule_strategy import RuleStrategy
from ..strategies.rules import (
    INDICATOR_NAMES,
    RULE_TEMPLATE,
    RuleError,
    rule_summary,
    validate_rule_spec,
)
from ..strategies.templates import build_strategy
from .deps import get_services
from .schemas import (
    ArenaRunIn,
    RuleStrategyIn,
    TrackIn,
)


def _legacy_notice(response: Response) -> None:
    """统一赛道已并入策略 / 回测页：旧接口保留一个版本，响应头提示将下线。"""
    response.headers["Deprecation"] = "true"
    response.headers["X-Replaced-By"] = "/api/studio/strategies, /api/research/backtests, /api/research/compare-batches"


router = APIRouter(prefix="/api/arena", tags=["统一赛道与比较"], dependencies=[Depends(_legacy_notice)])


# ---------------------------------------------------------------------------
# 赛道
# ---------------------------------------------------------------------------
@router.get("/track/defaults")
def track_defaults() -> dict:
    """默认赛道配置。这是所有团队共用的同一组条件。"""
    return {
        "track": DEFAULT_TRACK,
        "note": "以上均为实验参数，不代表交易所实际费率，也不是投资建议",
    }


@router.post("/track/validate")
def validate_track(payload: TrackIn) -> dict:
    """校验一份赛道覆盖项，返回合成后的赛道与摘要（不落库）。"""
    try:
        track = Arena.build_track(payload.overrides or None)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    from ..engine.arena import track_digest

    return {"track": track, "digest": track_digest(track)}


# ---------------------------------------------------------------------------
# 参赛者
# ---------------------------------------------------------------------------
def _build_entrants(services, payload: ArenaRunIn) -> list[Entrant]:
    """把请求里的参赛者描述解析成可运行的策略实例。

    - `template`：平台内置模板
    - `rule`：JSON 规则（AST 白名单，不执行用户 Python）
    - `strategy_id`：已保存的策略版本（模板或 Agent 生成的代码）
    """
    entrants: list[Entrant] = []
    for item in payload.entrants:
        if item.kind == "template":
            if item.template not in {"ma_trend", "mean_reversion", "grid_bounded", "dca_rebalance", "buy_hold", "cash"}:
                raise HTTPException(400, f"未知模板：{item.template}")
            entrants.append(
                Entrant(
                    strategy_id=item.id or f"tpl:{item.template}",
                    name=item.name or item.template,
                    team=item.team or "未标注团队",
                    instance=build_strategy(item.template, item.params),
                    origin=item.origin,
                    version=item.version or "builtin",
                    kind="template",
                    params=item.params,
                )
            )
        elif item.kind == "rule":
            try:
                spec = validate_rule_spec({**RULE_TEMPLATE, **(item.rule or {})})
            except RuleError as e:
                raise HTTPException(400, f"规则策略 {item.name or ''} 不合法：{e}") from e
            entrants.append(
                Entrant(
                    strategy_id=item.id or f"rule:{spec.id}",
                    name=item.name or spec.name,
                    team=item.team or spec.team,
                    instance=RuleStrategy(spec),
                    origin=item.origin,
                    version=spec.version,
                    kind="rule",
                    params=item.params or spec.to_dict(),
                )
            )
        else:
            raise HTTPException(400, f"不支持的参赛者类型：{item.kind}")
    return entrants


def _resolve_dataset(services, payload: ArenaRunIn) -> DataSet:
    """取行情：优先 CSV 快照，其次交易所，最后合成数据（并明确标注来源）。"""
    if payload.csv_dataset_id:
        saved = services.store.get(Collections.DATASETS, payload.csv_dataset_id)
        if saved is None:
            raise HTTPException(404, "CSV 数据集不存在，请重新上传")
        try:
            bars = parse_csv(
                saved["content"],
                symbol=saved.get("symbol", payload.symbol),
                timeframe=saved.get("timeframe", payload.timeframe),
            )
        except DataError as e:
            raise HTTPException(400, str(e)) from e
        return DataSet(
            symbol=saved.get("symbol", payload.symbol),
            timeframe=saved.get("timeframe", payload.timeframe),
            bars=bars,
            source="csv",
        ).freeze(f"用户 CSV · {saved.get('filename', '未命名')}（来源自行确认）")
    return services.market.dataset(
        payload.symbol, payload.timeframe, limit=payload.bars_limit, exchange=payload.exchange
    )


@router.post("/run")
def run_arena(payload: ArenaRunIn) -> dict:
    """跑一个统一赛道批次，返回这一批次的比较结果。

    改参数或改代码 → 新批次；原批次成绩不被覆盖。
    """
    services = get_services()
    entrants = _build_entrants(services, payload)
    if not entrants:
        raise HTTPException(400, "至少需要一个参赛策略")

    data = _resolve_dataset(services, payload)
    overrides = dict(payload.track or {})
    if payload.symbol:
        overrides.setdefault("symbols", [payload.symbol])
    if payload.timeframe:
        overrides.setdefault("timeframe", payload.timeframe)

    arena = Arena(store=services.store)
    try:
        batch = arena.run(
            entrants,
            data,
            track_overrides=overrides or None,
            with_scenarios=payload.with_scenarios,
        )
    except (ValueError, DataError) as e:
        raise HTTPException(400, str(e)) from e

    return {
        "batch": batch.to_dict(with_series=True),
        "leaderboard": {w: _leaderboard_from_batch(batch, w) for w in ("7", "30")},
    }


def _leaderboard_from_batch(batch, window: str) -> dict:
    """从内存里的批次直接算排行榜（与存储版结果一致，只是数据来源不同）。"""
    standings = [s.to_dict() for s in batch.standings]
    bench_return = None
    for s in standings:
        if s.get("strategy_id") == "bench:buy_hold":
            w = ((s.get("windows") or {}).get("windows") or {}).get(window)
            if w:
                bench_return = (w.get("metrics") or {}).get("total_return")
    rows = []
    for s in standings:
        w = ((s.get("windows") or {}).get("windows") or {}).get(window)
        if w is None:
            continue
        m = w.get("metrics") or {}
        ret = m.get("total_return")
        rows.append(
            {
                "strategy_id": s.get("strategy_id"),
                "name": s.get("name"),
                "team": s.get("team"),
                "origin": s.get("origin"),
                "version": s.get("version"),
                "kind": s.get("kind"),
                "params": s.get("params"),
                "is_benchmark": s.get("is_benchmark"),
                "window": w.get("label"),
                "hours": w.get("hours"),
                "complete": w.get("complete"),
                "return": ret,
                "max_drawdown": m.get("max_drawdown"),
                "excess_return": (ret - bench_return) if (ret is not None and bench_return is not None) else None,
                "trades": m.get("trades"),
                "win_rate": m.get("win_rate"),
                "sharpe": m.get("sharpe"),
                "completeness": s.get("completeness"),
                "notes": m.get("notes", []),
            }
        )
    rows.sort(key=lambda r: (r["return"] is None, -(r["return"] or 0)))
    return {
        "batch_id": batch.id,
        "window": window,
        "benchmark_return": bench_return,
        "rows": rows,
        "track": batch.track,
        "dataset": batch.dataset,
        "track_digest": batch.track_digest,
        "saved_at": batch.started_at,
    }


@router.get("/batches")
def list_batches(limit: int = Query(30, ge=1, le=200)) -> dict:
    """历史批次列表。每个批次是冻结的，切换参数不会影响已有成绩。"""
    services = get_services()
    batches = services.store.list(Collections.BATCHES, limit=limit)
    return {
        "batches": [
            {
                "id": b.get("id"),
                "created_at": b.get("started_at"),
                "count": b.get("count"),
                "track_digest": b.get("track_digest"),
                "symbol": (b.get("dataset") or {}).get("symbol"),
                "timeframe": (b.get("dataset") or {}).get("timeframe"),
                "source_label": (b.get("dataset") or {}).get("source_label"),
                "tracks": (b.get("dataset") or {}).get("bars"),
            }
            for b in batches
        ],
        "note": "不同实体的接口、不同赛道、不同成本规则的成绩不合并成一个收益排名",
    }


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str, window: str = Query("30", pattern="^(7|30)$")) -> dict:
    services = get_services()
    batch = services.store.get(Collections.BATCHES, batch_id)
    if batch is None:
        raise HTTPException(404, "批次不存在")
    return {"batch": batch, "window": window}


@router.get("/batches/{batch_id}/leaderboard")
def batch_leaderboard(
    batch_id: str,
    window: str = Query("30", pattern="^(7|30)$"),
    include_benchmarks: bool = True,
    only_complete: bool = False,
) -> dict:
    """批次排行榜。只排序净收益，但并列呈现回撤、超额与完整性。

    直接从存储的批次记录计算，不依赖内存里的 Standing 对象——
    这样重启服务后历史批次的排行榜依然可查。
    """
    services = get_services()
    saved = services.store.get(Collections.BATCHES, batch_id)
    if saved is None:
        raise HTTPException(404, "批次不存在")

    standings = saved.get("standings", [])

    def window_of(s: dict) -> dict | None:
        return ((s.get("windows") or {}).get("windows") or {}).get(window)

    bench_return = None
    for s in standings:
        if s.get("strategy_id") == "bench:buy_hold":
            w = window_of(s)
            if w:
                bench_return = (w.get("metrics") or {}).get("total_return")

    rows: list[dict] = []
    for s in standings:
        if s.get("is_benchmark") and not include_benchmarks:
            continue
        w = window_of(s)
        if w is None:
            continue
        if only_complete and not w.get("complete"):
            continue
        m = w.get("metrics") or {}
        ret = m.get("total_return")
        rows.append(
            {
                "strategy_id": s.get("strategy_id"),
                "name": s.get("name"),
                "team": s.get("team"),
                "origin": s.get("origin"),
                "version": s.get("version"),
                "kind": s.get("kind"),
                "params": s.get("params"),
                "is_benchmark": s.get("is_benchmark"),
                "window": w.get("label"),
                "hours": w.get("hours"),
                "complete": w.get("complete"),
                "return": ret,
                "max_drawdown": m.get("max_drawdown"),
                "excess_return": (ret - bench_return) if (ret is not None and bench_return is not None) else None,
                "trades": m.get("trades"),
                "win_rate": m.get("win_rate"),
                "sharpe": m.get("sharpe"),
                "completeness": s.get("completeness"),
                "notes": m.get("notes", []),
            }
        )
    rows.sort(key=lambda r: (r["return"] is None, -(r["return"] or 0)))

    return {
        "batch_id": batch_id,
        "window": window,
        "benchmark_return": bench_return,
        "rows": rows,
        "track": saved.get("track"),
        "dataset": saved.get("dataset"),
        "track_digest": saved.get("track_digest"),
        "saved_at": saved.get("started_at"),
        "conventions": [
            "排行榜并列呈现净收益、最大回撤、超额收益与运行完整性，不合并为单一分数",
            "同一窗口内收益以区间起点净值为分母，回撤在该区间内重新计算",
            "超额收益相对同币种、同资金、同成本、同区间的买入持有基准",
            f"{window} 天检查点为起点后的前 {int(window) * 24} 小时；未满窗口标注为进行中",
            "样本不足或无成交时，胜率 / 夏普等指标显示为不适用",
        ],
    }


# ---------------------------------------------------------------------------
# 规则策略
# ---------------------------------------------------------------------------
@router.get("/rules/meta")
def rules_meta() -> dict:
    """规则策略的可用指标、内置模板与写法说明。"""
    return {
        "indicators": [
            {"name": "close", "desc": "最近一根已收盘 bar 的收盘价"},
            {"name": "sma_fast", "desc": "快周期简单移动平均"},
            {"name": "sma_slow", "desc": "慢周期简单移动平均"},
            {"name": "rsi", "desc": "指定周期涨跌幅简单平均 RSI（非 Wilder 平滑）"},
            {"name": "position", "desc": "当前持仓占净值比例，0–1"},
        ],
        "operators": ["<", "<=", ">", ">=", "==", "!="],
        "logic": ["and", "or", "not", "括号"],
        "template": RULE_TEMPLATE,
        "constraints": [
            "买入与卖出条件同时满足时，卖出优先（保守假设）",
            "无信号时保持当前持仓数量，不做无意义调仓",
            "目标仓位范围 0–1，平台统一处理资金、成本与风控",
            "不支持函数调用、属性访问、下标、算术运算；不执行任意 Python 代码",
            "全角符号（＞ ＜ ＝）会自动转换为半角",
        ],
    }


@router.post("/rules/validate")
def validate_rule(payload: RuleStrategyIn) -> dict:
    """校验一份规则策略，返回规范化结果与版本摘要。"""
    try:
        spec = validate_rule_spec({**RULE_TEMPLATE, **payload.model_dump(exclude_none=True)})
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    return {"spec": spec.to_dict(), "summary": rule_summary(spec), "indicators": sorted(INDICATOR_NAMES)}


@router.post("/rules/save")
def save_rule(payload: RuleStrategyIn) -> dict:
    """把规则策略存进策略库，供后续批次选择。"""
    services = get_services()
    try:
        spec = validate_rule_spec({**RULE_TEMPLATE, **payload.model_dump(exclude_none=True)})
    except RuleError as e:
        raise HTTPException(400, str(e)) from e
    saved = services.store.upsert(
        Collections.STRATEGIES,
        {
            "id": spec.id,
            "name": spec.name,
            "team": spec.team,
            "template": "rule",
            "kind": "rule",
            "params": spec.to_dict(),
            "code": f"# 规则策略（非 Python 代码，由平台 AST 解释器执行）\n# 买入：{spec.buy}\n# 卖出：{spec.sell}",
            "symbol": payload.symbol,
            "timeframe": payload.timeframe,
            "source": "user",
            "status": "draft",
            "human_approved": False,
            "version": spec.version,
            "summary": rule_summary(spec),
        },
    )
    return {"strategy": saved}


@router.get("/rules")
def list_rules() -> dict:
    services = get_services()
    items = [s for s in services.store.list(Collections.STRATEGIES, limit=200) if s.get("kind") == "rule"]
    return {"rules": items}


# ---------------------------------------------------------------------------
# CSV 数据集
# ---------------------------------------------------------------------------
@router.post("/datasets/upload")
def upload_dataset(payload: dict) -> dict:
    """上传并校验一份 CSV 行情。校验不通过就明确报错，不做任何填补。"""
    services = get_services()
    content = payload.get("content")
    filename = payload.get("filename") or "未命名.csv"
    symbol = payload.get("symbol") or "BTC/USDT"
    timeframe = payload.get("timeframe") or "1h"
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(400, "CSV 内容为空")
    try:
        bars = parse_csv(content, symbol=symbol, timeframe=timeframe)
    except DataError as e:
        raise HTTPException(400, str(e)) from e

    ds = DataSet(symbol=symbol, timeframe=timeframe, bars=bars, source="csv").freeze(
        f"用户 CSV · {filename}（来源自行确认）"
    )
    saved = services.store.upsert(
        Collections.DATASETS,
        {
            "filename": filename,
            "symbol": symbol,
            "timeframe": timeframe,
            "content": content,
            **ds.summary(),
        },
    )
    return {
        "dataset": {k: v for k, v in saved.items() if k != "content"},
        "bars": len(bars),
        "range": {"start_ts": bars[0].open_time, "end_ts": bars[-1].close_time},
    }


@router.get("/datasets")
def list_datasets(limit: int = Query(30, ge=1, le=200)) -> dict:
    services = get_services()
    items = services.store.list(Collections.DATASETS, limit=limit)
    return {"datasets": [{k: v for k, v in d.items() if k != "content"} for d in items]}


@router.delete("/datasets/{dataset_id}")
def delete_dataset(dataset_id: str) -> dict:
    services = get_services()
    if not services.store.delete(Collections.DATASETS, dataset_id):
        raise HTTPException(404, "数据集不存在")
    return {"ok": True}


@router.get("/datasets/sample.csv", response_class=PlainTextResponse)
def download_sample(symbol: str = "BTC/USDT", timeframe: str = "1h", bars: int = 300) -> PlainTextResponse:
    """教学 CSV 样本，用来先跑通导入流程。"""
    text = sample_csv(symbol, timeframe, bars)
    return PlainTextResponse(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="sample-1h.csv"'},
    )


@router.get("/windows/meta")
def windows_meta() -> dict:
    """观察窗口口径说明，直接展示在界面上，避免口径被误读。"""
    return {
        "warmup_bars": WARMUP_BARS,
        "checkpoints": [
            {"days": 7, "hours": 168, "label": "7 天检查点", "desc": "起点之后的完整 168 小时"},
            {"days": 30, "hours": 720, "label": "30 天检查点", "desc": "起点之后的完整 720 小时"},
        ],
        "rules": [
            "前 100 根 bar 仅用于指标预热，不计成绩",
            "7 天 = 起点后的前 168 小时，30 天 = 起点后的前 720 小时；这是固定起点窗口，不是滚动近 N 天",
            "滚动近 7 天 / 近 30 天视图单独计算，与固定窗口分开呈现",
            "窗口未满时标注为「进行中」并给出实际观测时长，不冒充完整结果",
            "模拟加速与历史重放不能替代真实经过 168 / 720 小时的前向观察",
        ],
        "status_example": window_status_text(type("W", (), {"days": 7, "complete": False, "hours": 96.0})()),
    }
