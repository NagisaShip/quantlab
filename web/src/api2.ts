/** v0.3 新增接口：研究中心、AI 助手、策略广场、纸面模拟、AI 模型。 */

import { del, get, idemKey, post, put, requestText, withWs } from './http'

const qs = (o: Record<string, unknown>) =>
  Object.entries(o)
    .filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== false)
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
    .join('&')

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
  taker_fee: number
  slippage_bps: number
  spread_bps: number
  rebalance_tolerance: number
  fill_at: 'next_open' | 'close'
  exchange: 'binance' | 'okx'
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

export interface BacktestResult {
  id?: string
  strategy: string
  strategy_name: string
  initial_cash: number
  equity_curve: number[]
  timestamps: number[]
  drawdown: number[]
  benchmark_curve: number[]
  benchmark: string
  fills: FillRow[]
  fills_total: number
  logs: LogRow[]
  logs_total: number
  records: Record<string, { t: number; v: number }[]>
  metrics: FullMetrics
  errors: string[]
  cost_model: Record<string, number | string>
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
  duration_ms: number
  created_at: number
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

export interface ProviderView {
  name: string
  type: string
  base_url: string
  has_key: boolean
  key_source: string
  key_masked: string
  models: string[]
  model_settings?: Record<string, ModelSetting>
  custom: boolean
}

/** 目录模型的可选设置；routing 里仍用目录 ID 引用，请求时换成 request_model。 */
export interface ModelSetting {
  label?: string
  request_model?: string
  context_window?: number | null
  effort?: '' | 'low' | 'medium' | 'high' | 'max'
  inputs?: string[]
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
  favorite_count: number
  liked: boolean
  favorited: boolean
  pinned: boolean
  featured: boolean
  last_reply_at: number | null
  accepted_comment: string | null
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

export interface Post extends PostSummary {
  body: string
  edited_at?: number
}

export interface CommentRow {
  id: string
  post_id: string
  parent_id: string | null
  author: string
  body: string
  created_at: number
  like_count: number
  liked: boolean
  is_ai: boolean
  meta: { mode?: string; model?: string }
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
    get<{ backtests: BacktestRow[]; total: number }>(`/api/research/backtests?${qs({ slug, limit })}`),
  backtest: (id: string) => get<BacktestResult>(`/api/research/backtests/${id}`),
  deleteBacktest: (id: string) => del<{ ok: boolean }>(`/api/research/backtests/${id}`),
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
  config: () =>
    get<{ providers: ProviderView[]; routing: Record<string, string | string[]>; config_path: string | null }>('/api/config'),
  saveProvider: (p: { name: string; type: string; base_url: string; api_key: string; models: string[]; model_settings?: Record<string, ModelSetting> }) =>
    post<{ ok: boolean; provider: ProviderView }>('/api/config/providers', p),
  deleteProvider: (name: string) => del<{ ok: boolean }>(`/api/config/providers/${name}`),
  discoverModels: (p: { name: string; type: string; base_url: string; api_key: string }) =>
    post<{ models: string[] }>('/api/config/providers/discover', p),
  saveRouting: (p: { default: string; code_gen: string; reasoning: string; report: string; fallback: string[] }) =>
    put<{ ok: boolean }>('/api/config/routing', p),

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
  profile: () => get<{ profile: { name: string; bio: string } }>('/api/community/profile'),
  saveProfile: (p: { name: string; bio: string }) => put<{ profile: { name: string; bio: string } }>('/api/community/profile', p),
  posts: (p: { type?: string; tag?: string; q?: string; sort?: string; featured?: boolean; mine?: boolean; favorited?: boolean; page?: number; size?: number }) =>
    get<{ items: PostSummary[]; total: number; page: number; size: number; viewer: string }>(`/api/community/posts?${qs(p)}`),
  post: (id: string) =>
    get<{ post: Post; comments: CommentRow[]; related: PostSummary[]; viewer: string }>(`/api/community/posts/${id}`),
  createPost: (p: { type: string; title: string; body: string; tags: string[]; strategy_slug?: string | null; backtest_id?: string | null; ai_review?: boolean }) =>
    post<{ post: Post }>('/api/community/posts', p),
  updatePost: (id: string, p: { title?: string; body?: string; tags?: string[] }) => put<{ post: Post }>(`/api/community/posts/${id}`, p),
  deletePost: (id: string) => del<{ ok: boolean }>(`/api/community/posts/${id}`),
  likePost: (id: string) => post<{ on: boolean; count: number }>(`/api/community/posts/${id}/like`),
  favoritePost: (id: string) => post<{ on: boolean; count: number }>(`/api/community/posts/${id}/favorite`),
  featurePost: (id: string) => post<{ featured: boolean }>(`/api/community/posts/${id}/feature`),
  pinPost: (id: string) => post<{ pinned: boolean }>(`/api/community/posts/${id}/pin`),
  clonePost: (id: string) => post<{ strategy: StrategyFile }>(`/api/community/posts/${id}/clone`),
  aiReviewPost: (id: string) => post<{ comment: CommentRow }>(`/api/community/posts/${id}/ai-review`),
  comment: (id: string, body: string, parent_id?: string | null) =>
    post<{ comment: CommentRow }>(`/api/community/posts/${id}/comments`, { body, parent_id }),
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
export const fmtPct = (v: number | null | undefined, digits = 2, sign = false) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${sign && v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
export const fmtNum = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(digits)
const toNum = (v: number | string | null | undefined): number | null =>
  v === null || v === undefined || v === '' ? null : typeof v === 'number' ? v : Number(v)
/** 金额展示：接受数字或十进制字符串（服务端 Decimal）。 */
export const fmtMoney = (v: number | string | null | undefined, digits = 2) => {
  const n = toNum(v)
  return n === null || !Number.isFinite(n)
    ? '—'
    : n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}
/** 数量展示：十进制字符串原样显示（精确），数字保留 6 位有效数字。 */
export const fmtQty = (v: number | string | null | undefined) =>
  v === null || v === undefined || v === '' ? '—' : typeof v === 'string' ? v : Number(v.toPrecision(6)).toString()
export const fmtTime = (ms: number | null | undefined) => {
  if (!ms) return '—'
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
}
export const fmtDate = (ms: number | null | undefined) => (ms ? fmtTime(ms).slice(0, 10) : '—')
/** 把秒级时间戳（store 的 created_at）转成相对时间。 */
export const ago = (sec: number | null | undefined) => {
  if (!sec) return ''
  const diff = Date.now() / 1000 - sec
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`
  return fmtDate(sec * 1000)
}
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

export function downloadText(filename: string, text: string, type = 'text/plain;charset=utf-8') {
  const blob = new Blob([text], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
