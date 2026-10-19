/** v0.3 新增接口：研究中心、AI 助手、策略广场、纸面模拟、AI 模型。 */

import { del, get, idemKey, post, put, requestText, withWs } from '../http'
import { ago, downloadText, fmtDate, fmtMoney, fmtNum, fmtPct, fmtQty, fmtTime, qs, toNum } from '../format'

export { ago, downloadText, fmtDate, fmtMoney, fmtNum, fmtPct, fmtQty, fmtTime }

/* ---------------- 类型 ---------------- */
export type Params = Record<string, number | string | boolean>

export interface StrategyFile {
  id: string
  slug: string
  name: string
  team: string
  kind: string
  source: string
  status: string
  human_approved: boolean
  version: string
  symbol: string
  timeframe: string
  securities: string[]
  notes: string
  created_at: number
  updated_at: number
  params: Params
  version_count: number
  code?: string
  code_lines?: number
}

export interface SdkTemplate {
  id: string
  name: string
  category: string
  description: string
  securities: string[]
  timeframe: string
  tags: string[]
  difficulty: string
  code?: string
}

export interface Spec {
  securities: string[]
  timeframe: string
  start?: number | null
  end?: number | null
  bars_limit: number
  warmup_bars: number
  initial_cash: number
  initial_currency?: string
  margin_mode?: 'isolated' | 'cross'
  position_mode?: 'one-way' | 'hedge'
  contract_type?: 'linear' | 'inverse' | 'delivery'
  expiry_ts?: number | null
  ledger_v2?: boolean
  taker_fee: number
  slippage_bps: number
  spread_bps: number
  rebalance_tolerance: number
  fill_at: 'next_open' | 'close'
  execution_mode: 'bar' | 'tick'
  dynamic_slippage: boolean
  vol_slippage: number
  impact_a_bps: number
  impact_b_bps: number
  limit_move_pct: number
  min_liquidity_usd: number
  exchange: 'binance' | 'okx' | 'bybit'
  source: 'auto' | 'exchange' | 'synthetic'
  benchmark?: string | null
  params: Params
}

export interface FullMetrics {
  total_return: number
  annualized_return: number | null
  max_drawdown: number
  max_drawdown_duration: number
  recovery_bars: number | null
  volatility: number | null
  sharpe: number | null
  sortino: number | null
  calmar: number | null
  win_rate: number | null
  profit_factor: number | null
  payoff_ratio: number | null
  turnover: number
  trades: number
  fills: number
  total_fees: number
  consecutive_losses: number
  final_equity: number
  initial_equity: number
  benchmark_return: number | null
  benchmark_annualized: number | null
  benchmark_volatility: number | null
  excess_return: number | null
  alpha: number | null
  beta: number | null
  information_ratio: number | null
  tracking_error: number | null
  daily_win_rate: number | null
  profit_day_ratio: number | null
  trading_days: number
  notes: string[]
}

/** 十进制字符串：金额、数量、价格在 JSON 中一律以字符串传输（服务端 Decimal 记账）。展示前用 num() 转换。 */
export type Dec = string

export interface FillRow {
  fill_id?: string
  signal_id?: string
  ts: number
  security: string
  side: 'buy' | 'sell'
  amount: Dec
  price: Dec
  fee: Dec
  fee_currency?: string
  fee_value?: Dec
  notional: Dec
  liquidity?: 'maker' | 'taker'
  reason: string
  partial: boolean
  note: string
  realized_pnl: Dec
}

export interface PositionRow {
  security: string
  amount: Dec
  avg_cost: Dec
  last_price: Dec
  value: Dec
  unrealized_pnl: Dec
  pnl_ratio: number
  realized_pnl: Dec
  opened_ts?: number
  note?: string
}

export interface SignalRow {
  signal_id: string
  instrument_id: string
  event_id: string
  decision_ts: number
  expires_at: number
  reason: string
  status: string
  status_reason: string
  reason_code: string
  order_type: string
  target: { kind: string; value: Dec; unit: string; valid_until: number }
}

export interface RoundTripRow {
  security: string
  open_ts: number
  close_ts: number
  holding_ms: number | null
  cost_in: Dec
  realized: Dec
  fees: Dec
  return: number
}

export interface PortfolioDec {
  total_value: Dec
  available_cash: Dec
  positions_value: Dec
  total_fees: Dec
  position_ratio: number
  realized_pnl?: Dec
  net_contributions?: Dec
  fees_by_currency?: Record<string, Dec>
  positions: Record<string, PositionRow>
}

export interface DailyRow {
  date: string
  equity: number
  benchmark: number
  cash: number
  positions_value: number
  fees: number
  return: number
  benchmark_return: number
  cum_return: number
  positions: PositionRow[]
}

export interface LogRow { ts: number; bar: number; level: string; message: string }

export interface DatasetSummary {
  symbol: string
  timeframe: string
  bars: number
  source: string
  source_label: string
  digest: string
  start_ts: number
  end_ts: number
  coverage_warning: string
  exchange?: string
}

export interface ReviewItem {
  id: string
  title: string
  severity: 'block' | 'warn' | 'unknown' | 'info'
  ok: boolean
  detail: string
  evidence?: Record<string, unknown>
}
export interface ReviewCard {
  id: string
  verdict: 'pass' | 'conditional' | 'insufficient' | 'fail'
  blocks_promotion: boolean
  summary: { block: number; warn: number; unknown: number; pass: number }
  items: ReviewItem[]
  generated_at?: number
  model?: string
}

export interface BacktestResult {
  id?: string
  strategy: string
  strategy_name: string
  initial_cash: number
  equity_curve: number[]
  timestamps: number[]
  drawdown: number[]
  benchmark_curve: number[]
  simple_benchmark_curve?: number[]
  simple_benchmark?: string
  simple_benchmark_metrics?: { total_return?: number | null; label?: string; fast?: number; slow?: number; costs?: string }
  contract_specs?: Record<string, { contract_type?: string; settlement?: string; max_leverage?: number; mmr_tiers?: { upper: number; mmr: number }[]; source?: string; liquidation_fee_bps?: number; liquidation_slippage_bps?: number }>
  review?: ReviewCard
  benchmark: string
  fills: FillRow[]
  fills_total: number
  logs: LogRow[]
  logs_total: number
  records: Record<string, { t: number; v: number }[]>
  metrics: FullMetrics
  errors: string[]
  cost_model: Record<string, unknown>
  portfolio: PortfolioDec
  unit_nav?: number[]
  signals?: SignalRow[]
  signals_total?: number
  round_trips?: RoundTripRow[]
  open_positions?: PositionRow[]
  flows?: { ts: number; amount: Dec; unit_nav: Dec; units: Dec }[]
  ledger?: { balanced: boolean; trial_balance: Record<string, Dec>; final_equity: Dec; cash: Dec; net_contributions: Dec;
    fees_by_currency: Record<string, Dec>; realized_pnl: Dec; entries: number }
  assumptions?: Record<string, unknown>
  counters?: Record<string, number>
  risk?: Record<string, number>
  run_id?: string
  globals: Record<string, unknown>
  daily: DailyRow[]
  rejected: { ts: number; security: string; side: string; reason: string }[]
  params: Params
  securities: string[]
  timeframe: string
  unfilled: number
  timed_out: boolean
  datasets: DatasetSummary[]
  warnings: string[]
  spec: Spec
  manifest: Record<string, unknown>
  duration_ms: number
  status: 'done' | 'failed'
  code?: string
  row?: BacktestRow
}

export interface BacktestRow {
  id: string
  slug: string | null
  strategy_name: string
  version: string
  label: string
  securities: string[]
  timeframe: string
  start_ts: number
  end_ts: number
  initial_cash: number
  data_sources: string[]
  metrics: Partial<FullMetrics>
  status: string
  errors: number
  created_by?: string | null
  run_id?: string
  pinned?: boolean
  duration_ms: number
  created_at: number
  /** 来自 AI 研究室对话的回测：对话 id 与标题（用于按对话分组） */
  lab_project_id?: string | null
  lab_title?: string | null
}

export interface Job<T = unknown> {
  id: string
  kind: string
  title: string
  status: 'running' | 'done' | 'failed'
  done: number
  total: number
  result: T | null
  error: string
}

export interface OptRow {
  params: Params
  in_sample?: Partial<FullMetrics>
  out_sample?: Partial<FullMetrics>
  error?: string
  errors?: string[]
  in_rank?: number
  out_rank?: number
}

export interface Optimization {
  id: string
  objective: string
  objective_label: string
  split: number
  cut_ts: number
  grid: Record<string, (number | string)[]>
  method?: 'grid' | 'bayes'
  space?: Record<string, { low?: number; high?: number; choices?: (number | string)[] }> | null
  n_trials?: number
  multi_objective?: boolean
  rows: OptRow[]
  best: OptRow | null
  diagnostics: {
    combinations: number
    valid: number
    warnings: string[]
    rank_correlation?: number | null
    best_out_rank?: number | null
    best_in?: number | null
    best_out?: number | null
    median_out?: number | null
  }
  datasets: DatasetSummary[]
  warnings: string[]
  spec: Spec
  row: { slug: string; strategy_name: string; version: string }
}

export interface CopilotResult {
  action: string
  mode: 'llm' | 'fallback'
  content: string
  code: string
  params: Params
  checks: { ok?: boolean; stage?: string; issues?: string[]; errors?: string[]; fills?: number; note?: string }
  attempts: number
  model: string
  notes: string[]
  elapsed_ms: number
  securities: string[]
  timeframe: string
  name: string
  strategy?: StrategyFile
}

export interface ChatTurn { role: 'user' | 'assistant'; content: string }

export interface LlmStatus {
  ready: boolean
  hint: string
  roles: { role: string; available: string[]; local: string[] }[]
}

export type ProviderScope = 'workspace' | 'personal'
export type LimitMetric = 'tokens' | 'cost' | 'cpu_seconds' | 'backtests' | 'optimize_combinations' | 'storage_bytes' | 'record_units'
export type LimitWindow = '1h' | '5h' | '1d' | '7d' | '30d' | 'day' | 'week' | 'month' | 'total'
export interface LimitRule { metric: LimitMetric; window: LimitWindow; limit: string }
export interface LimitStatus extends LimitRule { used: string; percent: number; exceeded: boolean; label: string }

export interface ProviderQuotaWindow {
  calls: number; tokens: number; input_tokens: number; output_tokens: number; cache_read_tokens: number; cost: string
}
export interface ProviderRemoteQuotaWindow {
  label: string
  used_pct: number
  remaining_pct: number
  resets_at: number | null
  resets_at_text: string
  status: string
}

export interface ProviderRemoteQuota {
  available: boolean
  reason?: string
  source?: string
  timezone?: string
  updated_at: number
  updated_at_text?: string
  windows: Partial<Record<'5h' | 'week' | 'month', ProviderRemoteQuotaWindow>>
}

export interface ProviderQuotaCard {
  name: string; label: string; scope: ProviderScope; type: string; models: string[]
  billing: 'subscription' | 'api'; has_key: boolean; state: 'active' | 'near' | 'exhausted' | 'missing'
  windows: { '5h': ProviderQuotaWindow; week: ProviderQuotaWindow; month: ProviderQuotaWindow; total: ProviderQuotaWindow }
  total: ProviderQuotaWindow & { last_used: number }
  limits: LimitStatus[]
  remote?: ProviderRemoteQuota | null
  avg_cost_per_call: number
  last_used: number
}

export interface ProviderView {
  name: string
  label?: string
  scope: ProviderScope
  owner_id?: string
  editable?: boolean
  type: string
  base_url: string
  has_key: boolean
  key_source: string
  key_masked: string
  models: string[]
  model_settings?: Record<string, ModelSetting>
  currency?: 'CNY' | 'USD'
  fx_rate?: string
  limits?: LimitRule[]
  limit_status?: LimitStatus[]
  custom: boolean
}

/** 目录模型的可选设置；routing 里仍用目录 ID 引用，请求时换成 request_model。价格按「每百万 token」、服务商币种。 */
export interface ModelSetting {
  label?: string
  request_model?: string
  context_window?: number | null
  effort?: '' | 'low' | 'medium' | 'high' | 'max'
  inputs?: string[]
  price_in?: string
  price_out?: string
  /** 缓存命中的输入价格；留空按普通输入价 */
  price_cache?: string
  /** 缓存写入价格（Anthropic 等单独计费）；留空按普通输入价 */
  price_cache_write?: string
}

export interface ModelConfigView {
  providers: ProviderView[]
  routing: Record<string, string | string[] | number>
  routing_layers: { workspace: Record<string, string | string[] | number>; personal: Record<string, string | string[] | number> }
  me: { role: string; is_ws_admin: boolean; use_workspace_models: boolean; allow_personal_models: boolean }
  config_path: string | null
}

export interface UsageRow { provider?: string; scope?: ProviderScope; model?: string; user_id?: string; name?: string; calls: number; tokens?: number; input_tokens?: number; output_tokens?: number; cache_read_tokens?: number; cache_write_tokens?: number; cost: string }
export interface StorageUsage {
  used_bytes: number
  original_bytes: number
  thumbnail_bytes: number
  file_count: number
  latest_at: number | null
}

export interface ModelUsage {
  days: number
  view: 'me' | 'workspace'
  total: {
    calls: number; tokens: number; cost: string
    input_tokens?: number; output_tokens?: number
    cache_read_tokens?: number; cache_write_tokens?: number
    cache_hit_rate?: number | null
  }
  by_model: UsageRow[]
  by_user: UsageRow[]
  daily: { day: string; tokens: number; calls: number; cost: string }[]
  my_limits: LimitStatus[]
  storage: StorageUsage
  tasks: { backtests: string; cpu_seconds: string; optimize_combinations: string }
  research_records: RecordRetentionUsage
}

export interface MemberModelPolicy {
  user_id: string; name: string; email: string; role: string
  use_workspace_models: boolean; allow_personal_models: boolean
  custom_limits: boolean
  limits: LimitRule[]; limit_status: LimitStatus[]
  storage: StorageUsage
  usage_30d: { calls: number; tokens: number; cost: string }
}

export interface QuotaRequest extends LimitRule {
  id: string; user_id: string; user_name?: string; reason: string
  status: 'pending' | 'approved' | 'denied'; note?: string; created_ms: number; decided_ms?: number
}

export interface BacktestRetentionPrefs {
  auto_delete_oldest: boolean
  per_strategy_limit: number
  auto_delete_strategy_oldest: boolean
}

export interface BacktestRetentionUsage {
  total: number
  pinned: number
  unpinned: number
  max_records_per_user: number
  remaining: number
  strategies: { key: string; name: string; count: number; pinned: number; limit: number; over_limit: boolean }[]
}

export interface RecordTypeUsage {
  key: string
  label: string
  count: number
  pinned: number
  multiplier: number
  default_multiplier: number
  approx_bytes?: number
  units: number
}

export interface RecordRetentionUsage {
  total_records: number
  pinned_records: number
  total_units: number
  approx_bytes: number
  max_units_per_user: number
  remaining_units: number
  types: RecordTypeUsage[]
  strategies?: { key: string; name: string; count: number; pinned: number; limit: number; over_limit: boolean }[]
}

export interface RecordRetention {
  workspace: {
    max_units_per_user: number; max_records_per_user?: number
    hard_max_units?: number; default_max_units?: number
    disk_total_bytes?: number; reserved_bytes?: number; reserved_ratio?: number; record_unit_bytes?: number
  }
  user: BacktestRetentionPrefs
  usage: RecordRetentionUsage
  is_workspace_admin?: boolean
}

export interface ResearchRecordRow {
  id: string
  record_type: string
  record_type_label: string
  record_title: string
  record_units: number
  created_at?: number
  updated_at?: number
  created_by?: string | null
  pinned?: boolean
  [key: string]: unknown
}

export interface BacktestRetention {
  workspace: { max_records_per_user: number }
  user: BacktestRetentionPrefs
  usage: BacktestRetentionUsage
  is_workspace_admin?: boolean
}

export interface PostSummary {
  id: string
  type: 'article' | 'question' | 'strategy' | 'notice'
  type_label: string
  title: string
  excerpt: string
  tags: string[]
  author: string
  created_at: number
  updated_at: number
  views: number
  clones: number
  comment_count: number
  like_count: number
  dislike_count: number
  favorite_count: number
  liked: boolean
  disliked: boolean
  favorited: boolean
  pinned: boolean
  featured: boolean
  last_reply_at: number | null
  accepted_comment: string | null
  author_id?: string
  author_color?: string
  author_role?: string
  /** 来源：成员手写 / Agent 代发 / 平台 */
  source?: { kind: 'human' | 'agent' | 'system'; label?: string; on_behalf_of?: string; conversation?: string }
  reactions?: import('../apiCollab').QmojiReaction[]
  refs?: import('../apiCollab').Ref[]
  strategy?: { name: string; securities: string[]; timeframe: string; version: string; code?: string; code_sha256?: string; params?: Params; origin?: string }
  backtest?: {
    id?: string
    metrics: Partial<FullMetrics>
    equity: number[]
    benchmark?: number[]
    timestamps?: number[]
    initial_cash?: number
    securities?: string[]
    timeframe?: string
    params?: Params
    manifest?: Record<string, unknown>
    data_sources?: string[]
    warnings?: string[]
  }
}

export interface PostReactionResult {
  liked: boolean
  disliked: boolean
  like_count: number
  dislike_count: number
}

export interface QmojiReactionResult {
  emoji: string
  on: boolean
  count: number
  reactions: import('../apiCollab').QmojiReaction[]
}

export interface PostReport {
  id: string
  post_id: string
  reporter_id: string
  reporter_name: string
  reason: 'spam' | 'abuse' | 'misinformation' | 'duplicate' | 'other'
  reason_label: string
  detail: string
  status: 'open' | 'resolved' | 'dismissed'
  status_label: string
  action_taken: string
  resolution: string
  reviewed_by: string | null
  reviewed_at: number | null
  created_at: number
  updated_at: number
  post_exists: boolean
  post_title: string
  post_author: string
  post_author_id: string | null
  post_excerpt: string
  same_post_reports: number
}

export interface PostReportPage {
  items: PostReport[]
  counts: { open: number; resolved: number; dismissed: number; all: number }
  status: 'open' | 'resolved' | 'dismissed' | 'all'
}
export interface Post extends PostSummary {
  body: string
  edited_at?: number
}
export interface AuthorRelation {
  author_id: string
  is_following: boolean
  is_blocked: boolean
  can_dm: boolean
  dm_policy: 'reply_only' | 'open' | 'none'
  dm_reason: string
  author_role?: string
}

export interface FavoriteItem {
  id: string
  object_type: 'post' | 'strategy' | 'backtest' | 'factor' | 'model' | 'project' | 'link'
  object_id: string
  title: string
  link: string
  meta: Record<string, unknown>
  created_at: number
}

export interface UserProfile {
  user: {
    id: string
    username: string
    quantlab_uid: string
    name: string
    bio: string
    public_profile: boolean
    avatar_color: string
    role: string
    dm_policy: 'reply_only' | 'open' | 'none'
    dm_policy_label: string
    followers: number
    following: number
    posts: number
    likes: number
    clones: number
    comments: number
  }
  is_me: boolean
  is_following: boolean
  is_blocked: boolean
  blocked_me: boolean
  can_dm: boolean
  dm_reason: string
  posts: PostSummary[]
}

export interface CommentRow {
  id: string
  post_id: string
  parent_id: string | null
  author: string
  author_color?: string
  author_role?: string
  body: string
  created_at: number
  like_count: number
  liked: boolean
  is_ai: boolean
  meta: { mode?: string; model?: string; bot?: boolean }
  author_id?: string
  muted?: boolean
}

export interface PaperSessionView {
  id: string
  strategy_id: string
  strategy_name: string
  strategy_version: string
  symbol: string
  securities: string[]
  timeframe: string
  initial_cash: number
  started_at: number
  status: string
  last_update: number
  last_bar_ts: number
  elapsed_hours: number
  source: 'exchange' | 'demo'
  exchange: string
  params: Params
  cost: Record<string, number | string>
  observations: number
  errors: number
  events: { ts: number; level: string; message: string }[]
  equity_points: { t: number; v: number }[]
  fills: FillRow[]
  logs: LogRow[]
  portfolio: BacktestResult['portfolio'] | Record<string, never>
  pending: number
  message: string
  checkpoints: { days: number; hours: number; progress: number; complete: boolean }[]
  live?: boolean
}

/* ---------------- 接口 ---------------- */
export const v3 = {
  /* 研究中心 */
  templates: () => get<{ templates: SdkTemplate[] }>('/api/research/templates'),
  template: (id: string) => get<{ template: SdkTemplate; params: Params }>(`/api/research/templates/${id}`),
  strategies: () => get<{ strategies: StrategyFile[] }>('/api/studio/strategies'),
  strategy: (slug: string) =>
    get<{ strategy: StrategyFile; versions: { version: string; file: string; saved_at: number; lines: number }[]; issues: string[] }>(
      `/api/studio/strategies/${slug}`,
    ),
  version: (slug: string, file: string) => requestText(`/api/studio/strategies/${slug}/versions/${encodeURIComponent(file)}`),
  newStrategy: (p: { name: string; template_id?: string | null; code?: string; securities?: string[]; timeframe?: string; notes?: string; origin?: string }) =>
    post<{ strategy: StrategyFile; issues: string[] }>('/api/research/strategies', p),
  saveStrategy: (p: {
    slug: string
    name: string
    code: string
    params: Params
    team: string
    kind: string
    source: string
    symbol: string
    timeframe: string
    securities: string[]
    notes: string
  }) => post<{ strategy: StrategyFile; issues: string[] }>('/api/studio/strategies', { ...p, allow_issues: true }),
  cloneStrategy: (slug: string, name?: string) =>
    post<{ strategy: StrategyFile }>(`/api/research/strategies/${slug}/clone`, { name }),
  deleteStrategy: (slug: string) => del<{ ok: boolean }>(`/api/studio/strategies/${slug}`),
  approveStrategy: (slug: string) => post<{ strategy: StrategyFile }>(`/api/studio/strategies/${slug}/approve`),
  checkStrategy: (slug: string) =>
    post<{ ok: boolean; issues: string[]; functions?: string[] }>(`/api/studio/strategies/${slug}/check`),
  params: (slug: string) => get<{ params: Params }>(`/api/research/strategies/${slug}/params`),
  applyParams: (slug: string, params: Params) =>
    post<{ strategy: StrategyFile }>(`/api/research/strategies/${slug}/apply-params`, { params }),
  exportUrl: (slug: string) => withWs(`/api/studio/strategies/${slug}/export`),

  /** 回测在 worker 上执行：短任务直接返回结果；超过等待时间返回 202 + run_id，这里轮询到完成为止。 */
  runBacktest: async (p: Partial<Spec> & { slug?: string; code?: string; name?: string; persist?: boolean; label?: string }) => {
    const r = await post<{ id: string | null; result: BacktestResult | null; row?: BacktestRow; run_id?: string; state?: string }>(
      '/api/research/backtests', { wait_seconds: 25, ...p }, { 'Idempotency-Key': idemKey('bt') })
    if (r.result || !r.run_id) return r as { id: string | null; result: BacktestResult; row?: BacktestRow; run_id?: string }
    for (;;) {
      await new Promise((ok) => setTimeout(ok, 1500))
      const { run } = await get<{ run: { state: string; error: string | null; summary: { bt_id?: string; row?: BacktestRow } } }>(`/api/runs/${r.run_id}`)
      if (run.state === 'FAILED') throw new Error(run.error || '回测失败')
      if (run.state === 'CANCELED') throw new Error('回测已取消')
      if (run.state === 'COMPLETED') {
        const res = await get<{ result: BacktestResult }>(`/api/runs/${r.run_id}/result`)
        return { id: run.summary.bt_id ?? null, result: res.result, row: run.summary.row, run_id: r.run_id }
      }
    }
  },
  backtests: (slug?: string, limit = 100) =>
    get<{ backtests: BacktestRow[]; total: number; retention: BacktestRetentionUsage }>(`/api/research/backtests?${qs({ slug, limit })}`),
  backtest: (id: string) => get<BacktestResult>(`/api/research/backtests/${id}`),
  deleteBacktest: (id: string) => del<{ ok: boolean }>(`/api/research/backtests/${id}`),
  deleteBacktests: (ids: string[]) => post<{ ok: boolean; deleted: number; skipped_pinned: number; skipped_forbidden: number }>('/api/research/backtests/batch-delete', { ids }),
  pinBacktest: (id: string) => post<{ pinned: boolean; row: BacktestRow }>(`/api/research/backtests/${id}/pin`),
  recordRetention: () => get<RecordRetention>('/api/research/record-retention'),
  saveRecordRetention: (p: BacktestRetentionPrefs) => put<RecordRetention>('/api/research/record-retention', p),
  saveRecordWorkspaceRetention: (maxUnitsPerUser: number) =>
    put<RecordRetention>('/api/research/record-retention/workspace', { max_records_per_user: maxUnitsPerUser }),
  saveRecordMultipliers: (multipliers: Record<string, number>) =>
    put<{ multipliers: Record<string, number>; types: RecordTypeUsage[] }>('/api/research/record-retention/multipliers', { multipliers }),
  recordRetentionRecords: (q: { type?: string; q?: string; before_time?: number; before_id?: string; limit?: number } = {}) => {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(q)) if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
    return get<{ items: ResearchRecordRow[]; types: RecordTypeUsage[]; has_more: boolean; next_cursor: { time: number; id: string } | null }>(`/api/research/record-retention/records?${params.toString()}`)
  },
  pinRetentionRecord: (type: string, id: string) =>
    post<{ pinned: boolean; record: ResearchRecordRow }>(`/api/research/record-retention/${encodeURIComponent(type)}/${encodeURIComponent(id)}/pin`),
  deleteRetentionRecord: (type: string, id: string) =>
    del<{ ok: boolean }>(`/api/research/record-retention/${encodeURIComponent(type)}/${encodeURIComponent(id)}`),
  batchDeleteRetentionRecords: (items: { type: string; id: string }[]) =>
    post<{ deleted: string[]; failed: { type: string; id: string; error: string }[] }>(
      '/api/research/record-retention/records/batch-delete', { items },
    ),
  batchPinRetentionRecords: (items: { type: string; id: string }[], pinned: boolean) =>
    post<{ updated: string[]; failed: { type: string; id: string; error: string }[] }>(
      '/api/research/record-retention/records/batch-pin', { items, pinned },
    ),
  cleanupRecordRetention: () => post<{ deleted: number; usage: RecordRetentionUsage }>('/api/research/record-retention/cleanup', {}),
  backtestRetention: () => get<BacktestRetention>('/api/research/backtest-retention'),
  saveBacktestRetention: (p: BacktestRetentionPrefs) => put<BacktestRetention>('/api/research/backtest-retention', p),
  saveWorkspaceBacktestRetention: (maxRecordsPerUser: number) =>
    put<BacktestRetention>('/api/research/backtest-retention/workspace', { max_records_per_user: maxRecordsPerUser }),
  cleanupBacktestRetention: () => post<BacktestRetention & { deleted: number }>('/api/research/backtest-retention/cleanup', {}),
  compare: (ids: string[]) =>
    post<{ items: { id: string; strategy_name: string; version: string; params: Params; metrics: Partial<FullMetrics>; timestamps: number[]; returns: number[] }[] }>(
      '/api/research/backtests/compare',
      { ids },
    ),

  optimize: (p: Partial<Spec> & { slug: string; grid: Record<string, (number | string)[]>; split: number; objective: string;
    method?: 'grid' | 'bayes'; space?: Record<string, unknown>; n_trials?: number; multi_objective?: boolean }) =>
    post<{ job_id: string; combinations: number }>('/api/research/optimize', p),
  job: <T,>(id: string) => get<Job<T>>(`/api/research/jobs/${id}`),
  optimizations: (slug?: string) =>
    get<{ optimizations: { id: string; slug: string; strategy_name: string; objective: string; combinations: number; best_params: Params; method?: string; diagnostics: Optimization['diagnostics']; created_at: number }[] }>(
      `/api/research/optimizations?${qs({ slug })}`,
    ),
  optimization: (id: string) => get<Optimization>(`/api/research/optimizations/${id}`),

  /* AI */
  aiGenerate: (p: { description: string; securities?: string[]; timeframe?: string; history?: ChatTurn[]; save?: boolean; name?: string }) =>
    post<CopilotResult>('/api/ai/generate', p),
  aiEdit: (p: { code: string; instruction: string; securities: string[]; timeframe: string; history?: ChatTurn[] }) =>
    post<CopilotResult>('/api/ai/edit', p),
  aiFix: (p: { code: string; errors: string[]; securities: string[]; timeframe: string }) =>
    post<CopilotResult>('/api/ai/fix', p),
  aiExplain: (code: string) => post<CopilotResult>('/api/ai/explain', { code }),
  aiInterpret: (backtest_id: string) => post<CopilotResult>('/api/ai/interpret', { backtest_id }),
  aiReview: (p: { code?: string; backtest_id?: string; title?: string }) => post<CopilotResult>('/api/ai/review', p),
  aiChat: (p: { question: string; code?: string; history?: ChatTurn[] }) => post<CopilotResult>('/api/ai/chat', p),
  llmStatus: () => get<LlmStatus>('/api/config/status'),

  /* AI 模型 */
  config: () => get<ModelConfigView>('/api/config'),
  saveProvider: (p: {
    name: string; scope?: ProviderScope; label?: string; type: string; base_url: string; api_key: string; models: string[]
    model_settings?: Record<string, ModelSetting>; currency?: 'CNY' | 'USD'; fx_rate?: string; limits?: LimitRule[]
  }) => post<{ ok: boolean; provider: ProviderView }>('/api/config/providers', p),
  deleteProvider: (name: string, scope?: ProviderScope) => del<{ ok: boolean }>(`/api/config/providers/${name}${scope ? `?scope=${scope}` : ''}`),
  discoverModels: (p: { name: string; type: string; base_url: string; api_key: string }) =>
    post<{ models: string[] }>('/api/config/providers/discover', p),
  modelUsage: (days = 30, view: 'me' | 'workspace' = 'me') => get<ModelUsage>(`/api/config/usage?days=${days}&view=${view}`),
  providerQuotas: (refresh = false) => get<{ cards: ProviderQuotaCard[]; note: string }>(`/api/config/provider-quotas${refresh ? '?refresh=1' : ''}`),
  memberPolicies: () => get<{ members: MemberModelPolicy[] }>('/api/config/members'),
  setMemberPolicy: (userId: string, p: { use_workspace_models?: boolean; allow_personal_models?: boolean; limits?: LimitRule[] }) =>
    put<{ ok: boolean }>(`/api/config/members/${userId}`, p),
  quotaRequests: () => get<{ requests: QuotaRequest[] }>('/api/config/quota-requests'),
  requestQuota: (p: LimitRule & { reason: string }) => post<{ ok: boolean; request: QuotaRequest }>('/api/config/quota-requests', p),
  decideQuota: (id: string, approve: boolean, note = '') => post<{ ok: boolean }>(`/api/config/quota-requests/${id}/decide`, { approve, note }),
  saveRouting: (p: { default: string; code_gen: string; reasoning: string; report: string; fallback: string[]; retries?: number | null }, scope?: ProviderScope) =>
    put<{ ok: boolean }>(`/api/config/routing${scope ? `?scope=${scope}` : ''}`, p),

  /* 纸面模拟 */
  paperSessions: () =>
    get<{ sessions: PaperSessionView[]; conventions: string[]; tick_seconds: number }>('/api/studio/paper?limit=50'),
  paperStart: (p: { slug: string; securities?: string[]; timeframe?: string; initial_cash: number; source: 'exchange' | 'demo'; exchange: string; params?: Params; taker_fee?: number; slippage_bps?: number; execution?: 'paper' | 'demo'; demo_connection_id?: string; max_order_notional?: number; max_orders_per_minute?: number; max_daily_loss?: number }) =>
    post<{ session: PaperSessionView }>('/api/studio/paper/start', p),
  paperTick: (id: string) => post<{ session: PaperSessionView; tick: Record<string, unknown> }>(`/api/studio/paper/${id}/tick`),
  paperStop: (id: string) => post<{ session: PaperSessionView }>(`/api/studio/paper/${id}/stop`),
  paperPause: (id: string) => post<{ session: PaperSessionView }>(`/api/studio/paper/${id}/pause`),
  paperResume: (id: string) => post<{ session: PaperSessionView }>(`/api/studio/paper/${id}/resume`),
  paperLiquidate: (id: string) => post<{ session: PaperSessionView }>(`/api/studio/paper/${id}/liquidate`),
  paperDelete: (id: string) => del<{ ok: boolean }>(`/api/studio/paper/${id}`),
  paperReport: (id: string) =>
    get<{ session: PaperSessionView; windows: { windows: Record<string, { label: string; complete: boolean; hours: number; metrics: Partial<FullMetrics> }> }; conventions: string[]; live: boolean }>(
      `/api/studio/paper/${id}/report`,
    ),

  /* 行情缓存 */
  cache: () =>
    get<{ entries: { exchange: string; symbol: string; timeframe: string; bars: number; start_ts: number; end_ts: number; bytes: number }[]; root: string; last_errors: Record<string, string> }>(
      '/api/market/cache',
    ),
  prefetch: (p: { symbol: string; timeframe: string; days: number; exchange: string }) =>
    post<{ ok: boolean; bars: number }>(`/api/market/cache/fetch?${qs(p)}`),
  clearCache: (p: { exchange: string; symbol: string; timeframe: string }) =>
    del<{ ok: boolean }>(`/api/market/cache?${qs(p)}`),

  /* 策略广场 */
  favorites: (type?: string) => get<{ items: FavoriteItem[]; types: Record<string, string> }>(`/api/community/favorites${type ? `?type=${encodeURIComponent(type)}` : ''}`),
  isFavorite: (type: string, id: string) => get<{ favorite: boolean }>(`/api/community/favorites/${encodeURIComponent(type)}/${encodeURIComponent(id)}`),
  toggleFavorite: (p: { object_type: FavoriteItem['object_type']; object_id: string; title?: string; link?: string; meta?: Record<string, unknown> }) =>
    post<{ favorite: boolean; added: boolean }>('/api/community/favorites', p),
  removeFavorite: (type: string, id: string) => del<{ favorite: boolean; removed: boolean }>(`/api/community/favorites/${encodeURIComponent(type)}/${encodeURIComponent(id)}`),
  profile: () => get<{ profile: { name: string; username: string; quantlab_uid: string; bio: string; avatar_color: string; dm_policy: 'reply_only' | 'open' | 'none'; dm_policy_label: string; public_profile: boolean } }>('/api/community/profile'),
  saveProfile: (p: { name: string; bio: string; dm_policy: 'reply_only' | 'open' | 'none'; avatar_color: string; public_profile: boolean }) => put<{ profile: { name: string; username: string; quantlab_uid: string; bio: string; avatar_color: string; dm_policy: 'reply_only' | 'open' | 'none'; dm_policy_label: string; public_profile: boolean } }>('/api/community/profile', p),
  userProfile: (identifier: string) => get<UserProfile>(`/api/community/users/${encodeURIComponent(identifier)}`),
  followUser: (identifier: string) => post<{ following: boolean | number; added: boolean; followers: number }>(`/api/community/users/${encodeURIComponent(identifier)}/follow`, {}),
  unfollowUser: (identifier: string) => del<{ following: boolean | number; removed: boolean; followers: number }>(`/api/community/users/${encodeURIComponent(identifier)}/follow`),
  blockUser: (identifier: string) => post<{ blocked: boolean; added: boolean }>(`/api/community/users/${encodeURIComponent(identifier)}/block`, {}),
  unblockUser: (identifier: string) => del<{ blocked: boolean; removed: boolean }>(`/api/community/users/${encodeURIComponent(identifier)}/block`),
  posts: (p: { type?: string; tag?: string; q?: string; sort?: string; featured?: boolean; mine?: boolean; favorited?: boolean; following?: boolean; source?: string; page?: number; size?: number }) =>
    get<{ items: PostSummary[]; total: number; page: number; size: number; viewer: string; viewer_id: string }>(`/api/community/posts?${qs(p)}`),
  post: (id: string) =>
    get<{ post: Post; comments: CommentRow[]; related: PostSummary[]; viewer: string; viewer_id: string; can_moderate: boolean; post_muted: boolean; author_muted: boolean; author_relation: AuthorRelation }>(`/api/community/posts/${id}`),
  createPost: (p: { type: string; title: string; body: string; tags: string[]; strategy_slug?: string | null; backtest_id?: string | null; ai_review?: boolean; refs?: import('../apiCollab').Ref[] }) =>
    post<{ post: Post }>('/api/community/posts', p),
  updatePost: (id: string, p: { title?: string; body?: string; tags?: string[]; refs?: import('../apiCollab').Ref[] }) => put<{ post: Post }>(`/api/community/posts/${id}`, p),
  deletePost: (id: string) => del<{ ok: boolean }>(`/api/community/posts/${id}`),
  likePost: (id: string) => post<PostReactionResult & { on: boolean; count: number }>(`/api/community/posts/${id}/like`),
  dislikePost: (id: string) => post<PostReactionResult & { on: boolean; count: number }>(`/api/community/posts/${id}/dislike`),
  qmojiPost: (id: string, emoji: string) => post<QmojiReactionResult>(`/api/community/posts/${id}/qmoji`, { emoji }),
  reportPost: (id: string, p: { reason: PostReport['reason']; detail: string }) =>
    post<{ report: PostReport; already_reported: boolean }>(`/api/community/posts/${id}/report`, p),
  postReports: (status: 'open' | 'resolved' | 'dismissed' | 'all' = 'open') =>
    get<PostReportPage>(`/api/community/reports?status=${status}`),
  decidePostReport: (id: string, status: 'resolved' | 'dismissed', note = '') =>
    post<{ report: PostReport }>(`/api/community/reports/${id}/decide`, { status, note }),
  favoritePost: (id: string) => post<{ on: boolean; count: number }>(`/api/community/posts/${id}/favorite`),
  featurePost: (id: string) => post<{ featured: boolean }>(`/api/community/posts/${id}/feature`),
  pinPost: (id: string) => post<{ pinned: boolean }>(`/api/community/posts/${id}/pin`),
  clonePost: (id: string) => post<{ strategy: StrategyFile }>(`/api/community/posts/${id}/clone`),
  aiReviewPost: (id: string) => post<{ comment: CommentRow }>(`/api/community/posts/${id}/ai-review`),
  comment: (id: string, body: string, parent_id?: string | null) =>
    post<{ comment: CommentRow; bot_comment?: CommentRow | null }>(`/api/community/posts/${id}/comments`, { body, parent_id }),
  deleteComment: (id: string) => del<{ ok: boolean }>(`/api/community/comments/${id}`),
  likeComment: (id: string) => post<{ on: boolean; count: number }>(`/api/community/comments/${id}/like`),
  accept: (postId: string, commentId: string) =>
    post<{ accepted_comment: string | null }>(`/api/community/posts/${postId}/accept/${commentId}`),
  tags: () => get<{ hot: { tag: string; count: number }[]; suggested: string[] }>('/api/community/tags'),
  stats: () =>
    get<{ ranking: { author: string; posts: number; likes: number; clones: number; comments: number; score: number }[]; counts: Record<string, number> }>(
      '/api/community/stats',
    ),
}

/* ---------------- 格式化 ---------------- */
export const tone = (v: number | string | null | undefined) => {
  const n = toNum(v)
  return n === null ? '' : n > 0 ? 'positive' : n < 0 ? 'negative' : ''
}

export const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  tested: '已批准',
  backtested: '已回测',
  live: '模拟中',
  retired: '已归档',
}
export const ORIGIN_LABEL: Record<string, string> = {
  user: '手写',
  agent: 'AI 生成',
  template: '模板',
  community: '广场克隆',
  builtin: '内置',
  import: '策略包导入',
  freqtrade: 'Freqtrade 导入',
}
