/** 后端 API 客户端。所有请求走 /api 前缀（开发时由 Vite 代理到 8000）。 */

import { directBars } from '../marketDirect'
import { del, get, post, put, requestText, withWs } from '../http'
import { fmtMoney } from '../format'

/** 有些端点直接返回文本（源码预览），不能用 JSON 解析。 */
async function postText(path: string, body: unknown): Promise<string> {
  return requestText(path, { method: 'POST', body: JSON.stringify(body) })
}

/* ---------------- 类型 ---------------- */
export interface Provider {
  name: string
  type: string
  base_url: string
  has_key: boolean
  key_masked: string
  models: string[]
}
export interface ConfigView {
  providers: Provider[]
  routing: Record<string, string | string[]>
  limits: Record<string, unknown>
  trace: Record<string, unknown>
  notify: Record<string, string>
  exchange: Record<string, unknown>
  config_path: string | null
}
export interface ModelOption {
  provider: string
  model: string
  target: string
  type: string
}
export interface Preset {
  name: string
  label: string
  type: string
  base_url: string
  models: string[]
  key_env: string
  currency?: 'CNY' | 'USD'
}

export interface BarDTO { t: number; o: number; h: number; l: number; c: number; v: number; closed: boolean }
export interface BarsResponse {
  symbol: string
  timeframe: string
  source: string
  count: number
  bars: BarDTO[]
  /** browser：由浏览器直连交易所取得；不写则是服务器取得 */
  via?: 'browser' | 'server'
  stale?: boolean
  warning?: string
}
export interface Quality {
  symbol: string
  timeframe: string
  bars: number
  gaps: number
  duplicates: number
  unclosed: number
  source: string
  coverage_start: number
  coverage_end: number
  /** 交易所暂时连不上，按本地缓存出的报告 */
  stale?: boolean
  warning?: string
}

export interface ParamField {
  key: string
  label: string
  type: string
  default: number
  min?: number
  max?: number
}
export interface Template {
  name: string
  label: string
  description: string
  defaults: Record<string, number>
  param_schema: ParamField[]
}
export interface Guidance {
  id: string
  title: string
  description: string
  template_name: string
  prompt_id: string
  questions: { key: string; text: string; default: string | null; choices: string[] }[]
}

export interface Metrics {
  observations: number
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
  consecutive_losses: number
  final_equity: number
  initial_equity: number
  notes: string[]
}
export interface BacktestRun {
  id: string
  run_id: string
  symbol: string
  timeframe: string
  strategy: string
  params: Record<string, number>
  initial_cash: number
  mode: string
  metrics: Metrics
  equity_curve: number[]
  timestamps: number[]
  drawdown: number[]
  trades: {
    entry_ts: number
    exit_ts: number
    entry_price: number
    exit_price: number
    qty: number
    gross_return: number
    net_return: number
    fees: number
    reason: string
  }[]
  cost_model: Record<string, number>
  notes: string[]
  warmup_bars?: number
  windows?: WindowsPayload
  data_source?: string
  scenario?: string
}
export interface ScenarioRow {
  total_return: number
  max_drawdown: number
  sharpe: number | null
  trades: number
  final_equity: number
}
export interface BacktestResponse {
  run: BacktestRun
  scenarios?: Record<string, ScenarioRow>
}

export interface StrategyRecord {
  id: string
  name: string
  template: string
  kind?: string
  params: Record<string, number>
  symbol: string
  timeframe: string
  code: string
  source: string
  status: string
  human_approved: boolean
  checks?: { ok: boolean; issues: string[] }
  version?: string
  summary?: string
  team?: string
  created_at: number
}

export interface Role {
  id: string
  name: string
  title: string
  description: string
  system_prompt: string
  model: string
  temperature: number
  effort?: string
  avatar?: string
  tools: string[]
  /** true：只允许 tools 里勾选的工具；默认 false：AI 自动调用全部平台工具 */
  restrict_tools?: boolean
  output_format: string
  tags: string[]
  builtin: boolean
  source: string
  customized?: boolean
}
export interface ToolInfo { name: string; label: string; description: string }
export interface Team {
  id: string
  name: string
  description: string
  mode: 'sequential' | 'parallel' | 'debate'
  role_ids: string[]
  synthesizer_id: string
  challenge_rounds: number
}
export interface TeamRunStep {
  role_id: string
  role_name: string
  model: string
  status: 'ok' | 'error' | 'waiting' | 'blocked' | 'running' | 'queued' | 'canceled'
  output: string
  elapsed_ms: number
  error: string
}
export interface TeamRun {
  id: string
  team_id: string
  team_name: string
  topic: string
  mode: string
  context: string
  steps: TeamRunStep[]
  synthesis: string
  status: string
  created_at: number
  runtime_run_id?: string
  runtime_event_seq?: number
  can_manage?: boolean
}

export interface DigestData {
  date: string
  nav: number
  period_change: number
  period: string
  max_drawdown: number
  trades: number
  win_rate: number
  alerts: string[]
}
export interface NotificationRecord {
  run_key: string
  agent_id: string
  ts: number
  input_digest: string
  output: string
  pushed_to: string[]
  status: string
}

export interface PromptDoc {
  id: string
  version?: string
  title?: string
  temperature?: string
  variables?: string
  file: string
  kind?: 'task' | 'role'
  model?: string
  output_format?: string
  tools?: string[]
  source?: string
  body: string
}

/* ---------------- 统一赛道与比较 ---------------- */
export interface Track {
  market: string
  exchange: string
  symbols: string[]
  initial_cash: number
  leverage: number
  direction: string
  timeframe: string
  warmup_bars: number
  cost: {
    taker_fee: number
    maker_fee: number
    slippage_bps: number
    spread_bps: number
    max_volume_participation: number
  }
  timezone: string
  note: string
}

export interface WindowPayload {
  days: number
  label: string
  complete: boolean
  hours: number
  cutoff_ts: number
  metrics: Metrics
}

export interface WindowsPayload {
  start_ts: number
  end_ts: number
  warmup_bars: number
  scored_bars: number
  windows: Record<string, WindowPayload>
  rolling: Record<string, WindowPayload>
}

export interface Completeness {
  scored_bars: number
  warmup_bars: number
  hours: number
  window_7_complete: boolean
  window_30_complete: boolean
  has_trades: boolean
  no_data_gaps: boolean
}

export interface Standing {
  strategy_id: string
  name: string
  team: string
  origin: string
  version: string
  kind: string
  params: Record<string, unknown>
  metrics: Metrics
  windows: WindowsPayload
  completeness: Completeness
  equity_curve: number[]
  timestamps: number[]
  drawdown: number[]
  orders: {
    entry_ts: number
    exit_ts: number
    entry_price: number
    exit_price: number
    qty: number
    gross_return: number
    net_return: number
    fees: number
    reason: string
  }[]
  is_benchmark: boolean
  return_7d: number | null
  return_30d: number | null
}

export interface DatasetSummary {
  symbol: string
  timeframe: string
  bars: number
  source: string
  source_label: string
  digest: string
  start_ts: number
  end_ts: number
  coverage_hours: number
  requested_bars: number
  coverage_warning: string
}

export interface Batch {
  id: string
  track: Track
  track_digest: string
  dataset: DatasetSummary
  started_at: number
  finished_at: number
  count: number
  standings: Standing[]
  scenarios: Record<string, Record<string, ScenarioRow>>
  notes: string[]
  window_labels: Record<string, string>
}

export interface LeaderboardRow {
  strategy_id: string
  name: string
  team: string
  origin: string
  version: string
  kind: string
  params: Record<string, unknown>
  is_benchmark: boolean
  window: string
  hours: number
  complete: boolean
  return: number | null
  max_drawdown: number | null
  excess_return: number | null
  trades: number | null
  win_rate: number | null
  sharpe: number | null
  completeness: Completeness
  notes: string[]
}

export interface Leaderboard {
  batch_id: string
  window: string
  benchmark_return: number | null
  rows: LeaderboardRow[]
  track?: Track
  dataset?: DatasetSummary
  track_digest?: string
  saved_at?: number
  conventions?: string[]
}

export interface RuleMeta {
  indicators: { name: string; desc: string }[]
  operators: string[]
  logic: string[]
  template: Record<string, unknown>
  constraints: string[]
}

export interface RuleSpecView {
  id: string
  name: string
  team: string
  kind: string
  weight: number
  fast: number
  slow: number
  period: number
  buy: string
  sell: string
  version: string
  indicators: string[]
}

/* ---------------- 策略工作室 ---------------- */
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
  path: string
  params: Record<string, unknown>
  version_count: number
  issues: string[]
  code?: string
  code_lines?: number
}

export interface StrategyVersion {
  version: string
  file: string
  saved_at: number
  bytes: number
  lines: number
}

export interface StudioBacktestResult {
  strategy: string
  strategy_name: string
  initial_cash: number
  equity_curve: number[]
  timestamps: number[]
  drawdown: number[]
  fills: {
    ts: number
    security: string
    side: string
    amount: number
    price: number
    fee: number
    notional: number
    reason: string
    partial: boolean
    note: string
  }[]
  logs: { ts: number; bar: number; level: string; message: string }[]
  records: Record<string, { t: number; v: number }[]>
  metrics: Metrics
  errors: string[]
  cost_model: Record<string, number>
  portfolio: PortfolioView
  globals: Record<string, unknown>
  dataset: DatasetSummary
  symbols: string[]
}

/** 金额字段为十进制字符串（服务端 Decimal 记账）；展示前用 num() 转换。 */
export interface PortfolioView {
  starting_cash: string
  available_cash: string
  frozen_cash: string
  positions_value: string
  total_value: string
  total_return: number
  position_ratio: number
  total_fees: string
  positions: Record<string, {
    security: string
    amount: string
    avg_cost: string
    last_price: string
    value: string
    unrealized_pnl: string
    pnl_ratio: number
    realized_pnl: string
  }>
}

export interface PaperSessionView {
  id: string
  strategy_id: string
  strategy_name: string
  strategy_version: string
  symbol: string
  timeframe: string
  initial_cash: number
  started_at: number
  status: string
  last_update: number
  last_bar_ts: number
  elapsed_hours: number
  source: string
  cost: Record<string, number>
  observations: number
  errors: number
  events: { ts: number; level: string; message: string }[]
  equity_points: { t: number; v: number }[]
  fills: Record<string, unknown>[]
  message: string
  checkpoints: { days: number; hours: number; progress: number; complete: boolean }[]
  note: string
}

export interface ReconcileReport {
  simulated_fills: number
  live_fills: number
  matched: {
    simulated: Record<string, unknown>
    live: Record<string, unknown>
    price_diff_bps: number
    amount_diff_ratio: number
    fee_diff: number
    time_diff_ms: number
  }[]
  unmatched_simulated: Record<string, unknown>[]
  unmatched_live: Record<string, unknown>[]
  match_rate: number
  layers: Record<string, Record<string, unknown>>
  residual: Record<string, number | string>
  notes: string[]
}

export interface Capability {
  key: string
  supported: boolean
  note: string
}

export interface Overview {
  counts: Record<string, number>
  latest_run: {
    id: string | null
    strategy: string | null
    symbol: string | null
    timeframe: string | null
    data_source: string | null
    metrics: Partial<Metrics>
  }
  recent_runs: {
    id: string
    strategy: string
    symbol: string
    timeframe: string
    data_source: string
    created_at: number
    total_return: number | null
    max_drawdown: number | null
  }[]
  pending_strategies: StrategyRecord[]
  recent_team_runs: {
    id: string
    team_name: string
    topic: string
    mode: string
    status: string
    created_at: number
  }[]
  models: ModelOption[]
  routing: Record<string, string | string[]>
  disclaimer: string
}

export interface Health {
  status: string
  time: number
  adapters: Record<string, { reachable: boolean; error?: string }>
  synthetic_fallback: boolean
}

/* ---------------- 接口 ---------------- */
export const api = {
  health: () => get<Health>('/api/health'),
  overview: () => get<Overview>('/api/overview'),

  config: () => get<ConfigView>('/api/config'),
  presets: () => get<{ presets: Preset[] }>('/api/config/presets'),
  models: () => get<{ models: ModelOption[]; routing: Record<string, string | string[]> }>('/api/config/models'),
  addProvider: (p: Partial<Provider> & { api_key?: string }) => post<{ ok: boolean }>('/api/config/providers', p),
  testProvider: (target: string, prompt?: string) =>
    post<{ ok: boolean; reply?: string; error?: string; model?: string; provider?: string }>('/api/config/test', {
      target,
      ...(prompt ? { prompt } : {}),
    }),

  symbols: () => get<{ symbols: string[]; timeframes: string[] }>('/api/market/symbols'),
  /** 展示用 K 线：先由浏览器直连交易所（不占服务器 IP 的请求额度），拿不到再走服务器 */
  bars: async (symbol: string, timeframe: string, limit = 400, exchange = 'binance'): Promise<BarsResponse> => {
    const direct = await directBars(symbol, timeframe, limit, exchange)
    if (direct) return { symbol, timeframe, source: 'exchange', count: direct.length, bars: direct, via: 'browser' }
    return get<BarsResponse>(
      `/api/market/bars?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=${limit}&exchange=${exchange}`,
    )
  },
  quality: (symbol: string, timeframe: string, limit = 1500, exchange = 'binance') =>
    get<Quality>(
      `/api/market/quality?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=${limit}&exchange=${exchange}`,
    ),
  marketSummary: (symbol: string, timeframe: string) =>
    get<Record<string, number | string>>(
      `/api/market/summary?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}`,
    ),

  templates: () => get<{ templates: Template[] }>('/api/templates'),
  guidances: () => get<{ guidances: Guidance[] }>('/api/guidances'),
  renderGuidance: (guidance_id: string, answers: Record<string, string>) =>
    post<{ prompt: string; template: string; answers: Record<string, string> }>('/api/guidances/render', {
      guidance_id,
      answers,
    }),

  strategies: () => get<{ strategies: StrategyRecord[] }>('/api/strategies'),
  createStrategy: (p: {
    name: string
    template: string
    params: Record<string, number>
    symbol: string
    timeframe: string
    notes?: string
  }) => post<{ strategy: StrategyRecord }>('/api/strategies', p),
  approveStrategy: (id: string) => post<{ strategy: StrategyRecord }>(`/api/strategies/${id}/approve`),
  deleteStrategy: (id: string) => del<{ ok: boolean }>(`/api/strategies/${id}`),
  generateStrategy: (p: { description: string; symbol: string; timeframe: string }) =>
    post<{ strategy: StrategyRecord; clarifications: string[] }>('/api/strategies/generate', p),

  backtest: (p: Record<string, unknown>) => post<BacktestResponse>('/api/backtests', p),
  runs: () =>
    get<{
      runs: {
        id: string
        strategy: string
        symbol: string
        timeframe: string
        created_at: number
        data_source: string
        scenario: string
        metrics: Metrics
      }[]
    }>('/api/template-runs'),
  run: (id: string) => get<{ run: BacktestRun }>(`/api/template-runs/${id}`),
  deleteRun: (id: string) => del<{ ok: boolean }>(`/api/template-runs/${id}`),

  roles: () =>
    get<{ roles: Role[]; tools: string[]; tool_catalog: ToolInfo[]; models: ModelOption[]; efforts: { id: string; label: string }[] }>('/api/agents/roles'),
  resetRole: (id: string) => post<{ role: Role }>(`/api/agents/roles/${id}/reset`),
  createRole: (r: Partial<Role>) => post<{ role: Role }>('/api/agents/roles', r),
  updateRole: (id: string, r: Partial<Role>) => put<{ role: Role }>(`/api/agents/roles/${id}`, r),
  deleteRole: (id: string) => del<{ ok: boolean }>(`/api/agents/roles/${id}`),
  duplicateRole: (id: string) => post<{ role: Role }>(`/api/agents/roles/duplicate/${id}`),
  draftRole: (requirement: string, save = false) =>
    post<{ role: Role; saved: boolean }>('/api/agents/roles/draft', { requirement, save }),

  teams: () => get<{ teams: Team[] }>('/api/agents/teams'),
  createTeam: (t: Partial<Team>) => post<{ team: Team }>('/api/agents/teams', t),
  updateTeam: (id: string, t: Partial<Team>) => put<{ team: Team }>(`/api/agents/teams/${id}`, t),
  deleteTeam: (id: string) => del<{ ok: boolean }>(`/api/agents/teams/${id}`),
  runTeam: (id: string, p: Record<string, unknown>) => post<{ run: TeamRun }>(`/api/agents/teams/${id}/run`, p),
  teamRuns: () => get<{ runs: TeamRun[] }>('/api/agents/team-runs'),
  teamRun: (id: string) => get<{ run: TeamRun }>(`/api/agents/team-runs/${id}`),
  resumeTeamRun: (id: string) => post<{ run: TeamRun }>(`/api/agents/team-runs/${id}/resume`, {}),
  cancelTeamRun: (id: string, signal_id: string) => post<{ run: TeamRun }>(`/api/agents/team-runs/${id}/cancel`, { signal_id }),
  runRole: (id: string, p: Record<string, unknown>) => post<{ run: TeamRun }>(`/api/agents/roles/${id}/run`, p),
  prompts: () => get<{ prompts: PromptDoc[] }>('/api/agents/prompts'),

  digestPreview: () =>
    get<{ digest_data: DigestData; evidence: Record<string, unknown> }>('/api/long-running/preview'),
  runDigest: (channel: string, dry_run: boolean) =>
    post<{ record: NotificationRecord }>('/api/long-running/digest/run', { channel, dry_run }),
  runMaintainer: (channel: string, dry_run: boolean) =>
    post<{ record: NotificationRecord }>('/api/long-running/maintainer/run', { channel, dry_run }),
  notifications: () =>
    get<{ notifications: NotificationRecord[]; pending: Record<string, string>[] }>('/api/notifications'),
  testNotification: (channel: string) =>
    post<{ ok: boolean; channel: string }>(`/api/notifications/test?channel=${channel}`),
  schedules: () => get<{ jobs: { id: string; cron: string; kind: string; channel: string; enabled: boolean }[] }>(
    '/api/schedules',
  ),
  createSchedule: (p: { id: string; cron: string; kind: string; channel: string }) =>
    post<{ id: string }>('/api/schedules', p),
  deleteSchedule: (id: string) => del<{ ok: boolean }>(`/api/schedules/${id}`),

  /* ---------------- 统一赛道与比较 ---------------- */
  trackDefaults: () => get<{ track: Track; note: string }>('/api/arena/track/defaults'),
  validateTrack: (overrides: Record<string, unknown>) =>
    post<{ track: Track; digest: string }>('/api/arena/track/validate', { overrides }),

  rulesMeta: () => get<RuleMeta>('/api/arena/rules/meta'),
  validateRule: (payload: Record<string, unknown>) =>
    post<{ spec: RuleSpecView; summary: string; indicators: string[] }>('/api/arena/rules/validate', payload),
  saveRule: (payload: Record<string, unknown>) =>
    post<{ strategy: StrategyRecord }>('/api/arena/rules/save', payload),
  rules: () => get<{ rules: StrategyRecord[] }>('/api/arena/rules'),

  uploadDataset: (p: { content: string; filename: string; symbol?: string; timeframe?: string }) =>
    post<{ dataset: DatasetSummary & { id: string; filename: string }; bars: number }>(
      '/api/arena/datasets/upload',
      p,
    ),
  datasets: () => get<{ datasets: (DatasetSummary & { id: string; filename: string })[] }>('/api/arena/datasets'),
  deleteDataset: (id: string) => del<{ ok: boolean }>(`/api/arena/datasets/${id}`),
  sampleCsvUrl: '/api/arena/datasets/sample.csv?bars=300',

  runArena: (p: Record<string, unknown>) =>
    post<{ batch: Batch; leaderboard: Record<string, Leaderboard> }>('/api/arena/run', p),
  batches: () =>
    get<{
      batches: {
        id: string
        created_at: number
        count: number
        track_digest: string
        symbol: string
        timeframe: string
        source_label: string
        tracks: number
      }[]
    }>('/api/arena/batches'),
  batch: (id: string, window: '7' | '30') =>
    get<{ batch: Batch; window: string }>(`/api/arena/batches/${id}?window=${window}`),
  leaderboard: (id: string, window: '7' | '30', includeBenchmarks = true) =>
    get<Leaderboard>(
      `/api/arena/batches/${id}/leaderboard?window=${window}&include_benchmarks=${includeBenchmarks}`,
    ),
  windowsMeta: () =>
    get<{
      warmup_bars: number
      checkpoints: { days: number; hours: number; label: string; desc: string }[]
      rules: string[]
      status_example: string
    }>('/api/arena/windows/meta'),

  /* ---------------- 策略工作室 ---------------- */
  studioStrategies: (kind?: string) =>
    get<{ strategies: StrategyFile[]; root: string; layout: string[] }>(
      `/api/studio/strategies${kind ? `?kind=${kind}` : ''}`,
    ),
  studioStrategy: (slug: string) =>
    get<{ strategy: StrategyFile; versions: StrategyVersion[]; issues: string[] }>(
      `/api/studio/strategies/${slug}`,
    ),
  studioSave: (p: {
    name: string
    code: string
    slug?: string
    team?: string
    kind?: string
    source?: string
    params?: Record<string, unknown>
    symbol?: string
    timeframe?: string
    notes?: string
    allow_issues?: boolean
  }) => post<{ strategy: StrategyFile; issues: string[] }>('/api/studio/strategies', p),
  studioDelete: (slug: string) => del<{ ok: boolean }>(`/api/studio/strategies/${slug}`),
  studioApprove: (slug: string) => post<{ strategy: StrategyFile }>(`/api/studio/strategies/${slug}/approve`),
  studioCheck: (slug: string) =>
    post<{ ok: boolean; issues: string[]; functions?: string[] }>(`/api/studio/strategies/${slug}/check`),
  studioExportUrl: (slug: string) => withWs(`/api/studio/strategies/${slug}/export`),
  studioVersionUrl: (slug: string, filename: string) =>
    `/api/studio/strategies/${slug}/versions/${filename}`,
  studioFromRule: (rule: Record<string, unknown>, symbol = 'BTC/USDT', timeframe = '1h') =>
    post<{ strategy: StrategyFile; rule_version: string }>('/api/studio/strategies/from-rule', {
      rule,
      symbol,
      timeframe,
    }),
  rulePreviewPython: (rule: Record<string, unknown>) =>
    postText('/api/studio/rules/preview-python', { rule }),
  studioBacktest: (p: Record<string, unknown>) => post<StudioBacktestResult>('/api/studio/backtest', p),

  paperSessions: () =>
    get<{ sessions: PaperSessionView[]; conventions: string[] }>('/api/studio/paper'),
  paperStart: (p: { slug: string; initial_cash?: number; symbol?: string; timeframe?: string }) =>
    post<{ session: PaperSessionView; strategy: StrategyFile }>('/api/studio/paper/start', p),
  paperTick: (id: string) =>
    post<{ session: PaperSessionView; tick: Record<string, unknown>; data_source: string }>(
      `/api/studio/paper/${id}/tick`,
    ),
  paperStop: (id: string) => post<{ session: PaperSessionView }>(`/api/studio/paper/${id}/stop`),
  paperRecover: (id: string) => post<{ session: PaperSessionView }>(`/api/studio/paper/${id}/recover`),
  paperReport: (id: string) =>
    get<{
      session: PaperSessionView
      windows: WindowsPayload
      window_status: Record<string, string>
      conventions: string[]
    }>(`/api/studio/paper/${id}/report`),
  paperDelete: (id: string) => del<{ ok: boolean }>(`/api/studio/paper/${id}`),

  reconcile: (p: Record<string, unknown>) => post<ReconcileReport>('/api/studio/reconcile', p),
  submitOrder: (p: Record<string, unknown>) =>
    post<{ order: Record<string, unknown> }>('/api/studio/order', p),
  capabilities: () =>
    get<{ exchanges: Record<string, Capability[]>; note: string }>('/api/studio/exchanges/capabilities'),
}

/* ---------------- 格式化 ---------------- */
export const pct = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? '不适用' : `${(v * 100).toFixed(digits)}%`

export const ratio = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? '不适用' : v.toFixed(digits)

export const money = (v: number | string | null | undefined, digits = 2) => fmtMoney(v, digits)

export const shortTime = (ms: number) => {
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export const dateOnly = (ms: number) => {
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export const SOURCE_LABEL: Record<string, string> = {
  synthetic: '教学合成数据',
  exchange: '交易所真实历史行情',
  csv: '用户 CSV · 来源自行确认',
  unknown: '未知来源',
}

/** 窗口状态文案。未跑满时给出实际观测时长，不冒充完整窗口。 */
export const windowStatus = (w: { days: number; complete: boolean; hours: number } | undefined) => {
  if (!w) return '—'
  if (w.complete) return `${w.days} 天窗口完整`
  return `${w.days} 天窗口未满 · 已观测 ${w.hours.toFixed(0)} 小时`
}

/** 样本不足时统一显示"不适用"，而不是伪造一个 0。 */
export const orNA = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v) ? '不适用' : v.toFixed(digits)
