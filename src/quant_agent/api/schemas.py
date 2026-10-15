"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------- 模型配置 ----------------
class ModelSettingIn(BaseModel):
    label: str = Field("", max_length=60, description="菜单里显示的名字，留空用模型 ID")
    request_model: str = Field("", max_length=200, description="实际请求的模型名，留空与目录 ID 相同")
    context_window: int | None = Field(None, ge=1024, le=10_000_000)
    effort: Literal["", "low", "medium", "high", "max"] = ""
    inputs: list[Literal["text", "image", "audio", "video", "file"]] = Field(default_factory=lambda: ["text"])


class ProviderIn(BaseModel):
    name: str
    type: Literal["openai_compatible", "openai_responses", "anthropic", "local"] = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    model_settings: dict[str, ModelSettingIn] = Field(
        default_factory=dict, description="按模型 ID 的显示名、实际请求模型、上下文窗口、默认思考等级与输入类型"
    )


class RoutingIn(BaseModel):
    default: str = ""
    code_gen: str = ""
    reasoning: str = ""
    report: str = ""
    fallback: list[str] = Field(default_factory=list)


class ConfigIn(BaseModel):
    providers: list[ProviderIn] = Field(default_factory=list)
    routing: RoutingIn = RoutingIn()
    notify: dict[str, Any] = Field(default_factory=dict)


class ProviderTestIn(BaseModel):
    target: str = Field(description="provider/model，例如 deepseek/deepseek-chat")
    prompt: str = "回复 OK 两个字符即可。"


# ---------------- 行情 ----------------
class BarsQuery(BaseModel):
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    limit: int = 500
    exchange: str = "binance"


# ---------------- 策略 ----------------
class StrategyVersionIn(BaseModel):
    name: str
    template: str
    params: dict[str, Any] = Field(default_factory=dict)
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    notes: str = ""
    source: Literal["user", "agent"] = "user"
    code: str = ""


class GuidanceIn(BaseModel):
    guidance_id: str
    answers: dict[str, str] = Field(default_factory=dict)


class StrategyGenIn(BaseModel):
    description: str
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    guidance_id: str | None = None


# ---------------- 回测 ----------------
class BacktestIn(BaseModel):
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    initial_cash: float = 10_000.0
    bars_limit: int = 1500
    exchange: str = "binance"
    cost_scale: float = 1.0
    scenario: str | None = None
    compare_scenarios: bool = False


# ---------------- Agent 角色 ----------------
class RoleIn(BaseModel):
    id: str | None = None
    name: str
    title: str = ""
    description: str = ""
    system_prompt: str = ""
    model: str = ""
    temperature: float = 0.3
    effort: str = ""
    avatar: str = ""
    tools: list[str] = Field(default_factory=list)
    output_format: str = ""
    tags: list[str] = Field(default_factory=list)


class RoleDraftIn(BaseModel):
    requirement: str
    save: bool = False


# ---------------- Agent 团队 ----------------
class TeamIn(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    mode: Literal["sequential", "parallel", "debate"] = "sequential"
    role_ids: list[str] = Field(default_factory=list)
    synthesizer_id: str = ""
    challenge_rounds: int = 1


class TeamRunIn(BaseModel):
    topic: str
    context: str = ""
    include_market_data: bool = True
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    strategy: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


# ---------------- 长期 Agent ----------------
class DigestRunIn(BaseModel):
    date: str | None = None
    channel: str = "console"
    dry_run: bool = True


class MaintainerRunIn(BaseModel):
    period: str | None = None
    channel: str = "console"
    dry_run: bool = True


# ---------------- 统一赛道与比较 ----------------
class TrackIn(BaseModel):
    """赛道的部分覆盖项。只允许覆盖已知字段，避免悄悄换掉实验条件。"""

    overrides: dict[str, Any] = Field(default_factory=dict)


class EntrantIn(BaseModel):
    """一个参赛策略。kind 决定用哪个字段解析。"""

    id: str | None = None
    kind: Literal["template", "rule"] = "template"
    name: str = ""
    team: str = ""
    origin: Literal["user", "agent", "builtin"] = "user"
    version: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    template: str = "ma_trend"
    rule: dict[str, Any] | None = None


class ArenaRunIn(BaseModel):
    """跑一个统一赛道批次的请求。"""

    entrants: list[EntrantIn] = Field(default_factory=list)
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    exchange: str = "binance"
    bars_limit: int = Field(1500, ge=102, le=5000)
    csv_dataset_id: str | None = None
    track: dict[str, Any] = Field(default_factory=dict)
    with_scenarios: bool = False


class RuleStrategyIn(BaseModel):
    """规则策略提交。字段与 RULE_TEMPLATE 对齐，给默认值便于最小化提交。"""

    name: str = "趋势过滤 RSI"
    team: str = "第四研究组"
    weight: float = 0.8
    fast: int = 12
    slow: int = 48
    period: int = 14
    buy: str = "sma_fast > sma_slow and rsi < 60"
    sell: str = "sma_fast < sma_slow or rsi > 75"
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"


# ---------------- 策略工作室 ----------------
class StrategyFileIn(BaseModel):
    """保存一份策略源码。"""

    name: str
    code: str
    slug: str | None = None
    team: str = "未标注团队"
    kind: Literal["code", "rule", "template"] = "code"
    source: Literal["user", "agent", "builtin", "template", "community"] = "user"
    params: dict[str, Any] = Field(default_factory=dict)
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    securities: list[str] = Field(default_factory=list)
    notes: str = ""
    human_approved: bool = False
    allow_issues: bool = False


class StrategyFromRuleIn(BaseModel):
    """把 JSON 规则编译成 Python 策略。"""

    rule: dict[str, Any] = Field(default_factory=dict)
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"


class StudioBacktestIn(BaseModel):
    """跑一份策略源码或已保存的策略。"""

    slug: str | None = None
    code: str | None = None
    name: str = ""
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    bars_limit: int = Field(1500, ge=102, le=5000)
    dataset_id: str | None = None
    securities: list[str] = Field(default_factory=list)
    initial_cash: float = 10_000.0
    warmup_bars: int = Field(100, ge=0, le=500)
    taker_fee: float = 0.001
    slippage_bps: float = 5.0
    spread_bps: float = 2.0
    rebalance_tolerance: float = 0.005


class ReconcileIn(BaseModel):
    """实盘对照请求。"""

    slug: str | None = None
    simulated_fills: list[dict[str, Any]] = Field(default_factory=list)
    live_csv: str | None = None
    paper_session_id: str | None = None
    symbol: str = "BTC/USDT"
    timeframe: str = "1h"
    bars_limit: int = Field(1500, ge=102, le=5000)
    dataset_id: str | None = None
    initial_cash: float = 10_000.0
    warmup_bars: int = 100


class LiveFillsIn(BaseModel):
    """导入实盘成交 CSV。"""

    content: str
    filename: str = "live-fills.csv"
    note: str = ""


class OrderTicketIn(BaseModel):
    """向交易所模拟盘提交一张订单（接口联调）。"""

    exchange: Literal["binance", "okx"] = "binance"
    symbol: str = "BTC/USDT"
    side: Literal["buy", "sell"] = "buy"
    qty: float = Field(gt=0)
    order_type: Literal["market", "limit"] = "market"
    price: float | None = None
    client_order_id: str = ""
    run_id: str = "manual"
    signal_id: str = "manual"
    simulated: bool = True


# ---------------- 研究中心（v0.3） ----------------
TIMEFRAMES = Literal["1m", "5m", "15m", "1h", "4h", "1d"]


class RiskIn(BaseModel):
    """风控层参数（0 = 不限制）。"""

    max_position_weight: float = Field(1.0, ge=0, le=1)
    max_gross_exposure: float = Field(1.0, ge=0, le=1)
    max_trades_per_day: int = Field(0, ge=0, le=10_000)
    max_price_deviation: float = Field(0.2, ge=0, le=1)
    drawdown_pause: float = Field(0.0, ge=0, le=1)
    stop_loss: float = Field(0.0, ge=0, lt=1)
    take_profit: float = Field(0.0, ge=0, le=100)


class CashFlowIn(BaseModel):
    ts: int
    amount: float = Field(..., description="正数入金、负数出金")
    ref: str = ""


class SpecIn(BaseModel):
    """回测规格：标的、区间、资金、成本、成交模型、数据来源。"""

    securities: list[str] = Field(default_factory=list)
    timeframe: TIMEFRAMES = "1h"
    start: int | None = Field(None, description="计分起点（毫秒，UTC）。不填则按 bars_limit 回看")
    end: int | None = Field(None, description="结束时间（毫秒，UTC）。不填为当前")
    bars_limit: int = Field(1500, ge=50, le=20_000)
    warmup_bars: int = Field(100, ge=0, le=2000)
    initial_cash: float = Field(10_000.0, gt=0, le=1e10)
    taker_fee: float = Field(0.001, ge=0, le=0.05)
    slippage_bps: float = Field(5.0, ge=0, le=500)
    spread_bps: float = Field(2.0, ge=0, le=500)
    rebalance_tolerance: float = Field(0.005, ge=0, le=0.2)
    fill_at: Literal["next_open", "close"] = "next_open"
    exchange: Literal["binance", "okx"] = "binance"
    source: Literal["auto", "exchange", "synthetic"] = "auto"
    benchmark: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    insufficient_funds: Literal["reject", "reduce"] = "reject"
    fee_currency: Literal["quote", "received"] = "quote"
    risk: RiskIn | None = None
    cash_flows: list[CashFlowIn] = Field(default_factory=list, max_length=1000)
    recurring_deposit: float = Field(0.0, ge=0, le=1e9, description="定期定额入金（定投）金额；0 = 不入金")
    recurring_every_days: int = Field(0, ge=0, le=366, description="定投间隔天数")


class ResearchBacktestIn(SpecIn):
    slug: str | None = None
    code: str | None = None
    name: str = ""
    persist: bool = True
    label: str = ""
    wait_seconds: float = Field(30.0, ge=0, le=120, description="同步等待结果的秒数；超时返回 202 与 run_id")
    lang: Literal["dsl", "python"] | None = Field(
        None, description="只给 code 时的语言；缺省按内容判断（以 { 开头为 DSL）"
    )


class NewStrategyIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    template_id: str | None = "blank"
    code: str | None = None
    securities: list[str] = Field(default_factory=list)
    timeframe: TIMEFRAMES | None = None
    team: str = "我的策略"
    notes: str = ""
    origin: Literal["user", "agent", "community", "template"] = "user"


class CloneIn(BaseModel):
    name: str | None = None


class ApplyParamsIn(BaseModel):
    params: dict[str, Any]


class CompareIn(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=8)


class OptimizeIn(SpecIn):
    slug: str
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    split: float = Field(0.7, ge=0.4, le=0.9)
    objective: Literal["sharpe", "total_return", "calmar", "sortino", "excess_return"] = "sharpe"
    embargo_bars: int = Field(0, ge=0, le=500, description="切点后空出不计分的 bar 数（purge/embargo）")
    holdout: float = Field(0.0, ge=0.0, le=0.4, description="末尾锁定测试区间比例；调参不可见，只评估最终参数一次")
    walk_forward_folds: int = Field(0, ge=0, le=8, description="0 = 单次切分；2~8 = 滚动前推折数")
    method: Literal["grid", "bayes"] = Field("grid", description="grid = 网格穷举；bayes = optuna 智能调参")
    space: dict[str, Any] = Field(
        default_factory=dict,
        description="智能调参的参数范围；空则读策略里的 PARAM_SPACE。{名字: {low, high, step?} | {choices: [...]}}",
    )
    n_trials: int = Field(50, ge=5, le=150, description="智能调参的试验次数")
    multi_objective: bool = Field(False, description="同时最大化目标、最小化最大回撤（NSGA-II）")


class PaperStartIn(BaseModel):
    """启动实时模拟。source=exchange 用真实行情；demo 用时间确定性的演示行情（只用于看流程）。"""

    slug: str
    securities: list[str] = Field(default_factory=list)
    symbol: str | None = None
    timeframe: TIMEFRAMES | None = None
    initial_cash: float = Field(10_000.0, gt=0)
    source: Literal["exchange", "demo"] = "exchange"
    exchange: Literal["binance", "okx", "bybit"] = "binance"
    params: dict[str, Any] = Field(default_factory=dict)
    taker_fee: float = Field(0.001, ge=0, le=0.05)
    slippage_bps: float = Field(5.0, ge=0, le=500)
    risk: RiskIn | None = None
    # 成交去哪里：paper = 平台撮合；demo = 交易所模拟盘（需要先在「模拟盘连接」里接好 Key）
    execution: Literal["paper", "demo"] = "paper"
    demo_connection_id: str | None = None
    max_order_notional: float = Field(1_000.0, gt=0, le=10_000_000)
    max_orders_per_minute: int = Field(10, ge=1, le=600)
    max_daily_loss: float = Field(0.05, ge=0, le=1)


class DemoConnectIn(BaseModel):
    exchange: Literal["binance", "okx", "bybit"]
    env: Literal["demo", "testnet"] = "demo"
    label: str = Field("", max_length=60)
    api_key: str = Field(..., min_length=8, max_length=256)
    api_secret: str = Field(..., min_length=8, max_length=256)
    passphrase: str = Field("", max_length=128)


# ---------------- AI 助手 ----------------
class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=20_000)


class AiGenerateIn(BaseModel):
    description: str = Field(..., min_length=2, max_length=4000)
    securities: list[str] = Field(default_factory=list)
    timeframe: TIMEFRAMES | None = None
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)
    save: bool = False
    name: str | None = None


class AiEditIn(BaseModel):
    code: str = Field(..., max_length=60_000)
    instruction: str = Field(..., min_length=1, max_length=4000)
    securities: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    timeframe: TIMEFRAMES = "1h"
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


class AiFixIn(BaseModel):
    code: str = Field(..., max_length=60_000)
    errors: list[str] = Field(default_factory=list, max_length=50)
    securities: list[str] = Field(default_factory=lambda: ["BTC/USDT"])
    timeframe: TIMEFRAMES = "1h"


class AiCodeIn(BaseModel):
    code: str = Field(..., max_length=60_000)


class AiInterpretIn(BaseModel):
    backtest_id: str | None = None
    result: dict[str, Any] | None = None


class AiReviewIn(BaseModel):
    code: str = Field("", max_length=60_000)
    backtest_id: str | None = None
    title: str = ""


class AiChatIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=8000)
    code: str = Field("", max_length=60_000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=20)


# ---------------- 策略广场 ----------------
POST_TYPES = Literal["article", "question", "strategy", "notice"]


class PostIn(BaseModel):
    type: POST_TYPES = "article"
    title: str = Field(..., min_length=2, max_length=120)
    body: str = Field("", max_length=50_000)
    tags: list[str] = Field(default_factory=list, max_length=8)
    strategy_slug: str | None = None
    backtest_id: str | None = None
    ai_review: bool = False


class PostUpdateIn(BaseModel):
    title: str | None = Field(None, min_length=2, max_length=120)
    body: str | None = Field(None, max_length=50_000)
    tags: list[str] | None = Field(None, max_length=8)


class CommentIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=10_000)
    parent_id: str | None = None


class ProfileIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=30)
    bio: str = Field("", max_length=200)
