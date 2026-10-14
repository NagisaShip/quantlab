/** 上线验证：仿真真实度、晋级关卡、AI 值守员、压力演练。 */
import { get, post } from './http'

export interface FidelityComponent { key: string; label: string; value: number; bps: number | null }
export interface FidelityRow {
  security: string; side: string; decided_at: number
  bt: { ts?: number; qty: number; price: number; market?: number; fee?: number } | null
  live: { ts?: number; qty: number; price: number; market?: number; fee?: number } | null
  latency?: number; slippage?: number; fees?: number; partials?: number; rejections?: number; rejected?: string
}
export interface FidelityPart {
  label: string; execution: 'paper' | 'demo' | string; session_id: string; end_ts: number
  live_end: number | null; backtest_end: number | null; total: number; total_bps: number | null
  components: FidelityComponent[]; matched: number; only_backtest: number; only_live: number; missed: number; rows: FidelityRow[]
}
export interface Calibration {
  samples: number; current: { taker_fee: number; slippage_bps: number; spread_bps: number }
  observed?: { slippage_bps_median: number; slippage_bps_p75: number; fee_rate: number | null; latency_ms_median: number | null; reject_rate: number; partial_rate: number }
  suggested: { taker_fee: number; slippage_bps: number; spread_bps: number } | null
  confidence: 'none' | 'low' | 'medium' | 'high'; note: string
}
export interface FidelityReport {
  id: string; strategy_id: string; strategy_name: string
  sessions: { id: string; execution: string; started_at: number; last_update: number; exchange: string; source: string }[]
  window: { start: number; end: number; timeframe: string }
  backtest: { fills: number; return: number | null; equity: { t: number; v: number }[] }
  parts: FidelityPart[]; calibration: Calibration
  deviation: Record<string, { deviation_bps: number | null; total_bps: number | null; matched: number }>
  warnings: string[]; notes: string[]; created_at: number
}

export interface GradCheck { label: string; ok: boolean | null; value: string; need: string }
export interface GradDetail {
  strategy: string; stage: string; stage_label: string; next: string | null; next_label: string
  checks: GradCheck[]; passed: boolean; evidence: Record<string, unknown>
  history: { from: string; to: string; at: number; by: string; note?: string }[]
  config: Record<string, number>; live_trading: 'closed'
}
export interface GradRow { strategy: string; name: string; stage: string; stage_label: string; next: string | null; next_label: string; passed: boolean; progress: string }

export interface WatchFlag { level: 'error' | 'warn' | string; text: string; fill_id?: string; ts?: number | null; rule: string }
export interface WatchReview {
  id: string; strategy_name: string; execution: string; has_spec: boolean; rules: Record<string, unknown>
  trades: { fill_id: string; ts: number; security: string; side: string; qty: number; price: number; explanation: string; explanation_ai?: string }[]
  flags: WatchFlag[]; tagger: { kind: string; model?: string; prompt_version?: string; llm_error?: string }; reviewed_at: number
}

export type Verdict = 'ok' | 'warn' | 'bad' | 'skip'
export interface CheckupItem { id: string; title: string; verdict: Verdict; summary: string; details?: Record<string, unknown> }
export interface RedTeamPoint { severity: 'high' | 'medium' | 'low'; title: string; detail: string }
export interface CheckupRun {
  id: string; strategy_id: string; strategy_name: string; version?: string; status: 'running' | 'done' | 'failed'
  progress?: string; error?: string; overall?: Verdict; created_at: number; finished_at?: number; timeframe?: string
  securities?: string[]; items?: CheckupItem[]; warnings?: string[]
  report?: { items: CheckupItem[]; overall: Verdict; metrics: Record<string, number | null>; warmup: number; params: Record<string, unknown>; elapsed_s: number }
  redteam?: { mode: 'llm' | 'rules'; model: string; points: RedTeamPoint[]; content: string; note: string }
}
export interface StressScenario { id: string; name: string; days: string[]; description: string }
export interface StressResult {
  id: string; name: string; days: string[]; status: string; completed: boolean; error?: string
  data_source: string; warnings?: string[]; bars: number; timeframe: string
  start_equity: number; end_equity: number; return_pct: number; max_drawdown: number; worst_1h?: number
  orders: number; fills: number; rejections: number; partials: number; delayed: number
  liquidations: number; liquidation_loss: number; strategy_errors: number; drawdown_breach: boolean
  disconnects: { start_ts: number; end_ts: number; minutes: number; orders_after_reconnect?: number }[]
  equity_curve: [number, number][]; price_curve: [number, number][]; price_symbol?: string; verdict: string
}
export interface StressRun {
  id: string; strategy_id: string; strategy_name: string; status: 'running' | 'done' | 'failed'; progress?: string
  error?: string; created_at: number; finished_at?: number; timeframe?: string
  report?: { scenarios?: StressResult[]; summary: { passed: boolean; verdict: string; worst_drawdown?: number } | null; passed?: boolean } | null
}

export const validation = {
  fidelity: (session_ids: string[]) => post<{ report: FidelityReport }>('/api/validation/fidelity', { session_ids }),
  fidelityList: (strategy?: string) => get<{ reports: Pick<FidelityReport, 'id' | 'strategy_id' | 'strategy_name' | 'sessions' | 'window' | 'deviation' | 'created_at'>[] }>(
    `/api/validation/fidelity${strategy ? `?strategy=${encodeURIComponent(strategy)}` : ''}`),
  fidelityGet: (id: string) => get<{ report: FidelityReport }>(`/api/validation/fidelity/${id}`),
  applyCalibration: (id: string) => post<{ calibration: Record<string, unknown> }>(`/api/validation/fidelity/${id}/calibration`, { confirm: true }),
  calibrations: () => get<{ calibrations: { id: string; exchange: string; taker_fee: number; slippage_bps: number; spread_bps: number; updated_at: number }[] }>('/api/validation/calibration'),
  board: () => get<{ board: GradRow[]; config: Record<string, number>; stages: Record<string, string> }>('/api/validation/graduation'),
  grad: (slug: string) => get<GradDetail>(`/api/validation/graduation/${slug}`),
  promote: (slug: string, note = '') => post<GradDetail>(`/api/validation/graduation/${slug}/promote`, { confirm: true, note }),
  demote: (slug: string, to: string) => post<GradDetail>(`/api/validation/graduation/${slug}/demote`, { to }),
  watch: (sessionId: string) => post<{ review: WatchReview }>(`/api/validation/watcher/${sessionId}`),
  watchGet: (sessionId: string) => get<{ review: WatchReview | null }>(`/api/validation/watcher/${sessionId}`),
  watchList: () => get<{ reviews: { id: string; reviewed_at: number; flag_count: number }[] }>('/api/validation/watcher'),
  checkupStart: (p: { slug: string; bars_limit?: number; use_llm?: boolean }) => post<{ run: CheckupRun }>('/api/validation/checkup', p),
  checkupList: (slug?: string) => get<{ runs: CheckupRun[] }>(`/api/validation/checkup${slug ? `?slug=${encodeURIComponent(slug)}` : ''}`),
  checkupGet: (id: string) => get<{ run: CheckupRun; stale: boolean }>(`/api/validation/checkup/${id}`),
  stressScenarios: () => get<{ scenarios: StressScenario[] }>('/api/validation/stress/scenarios'),
  stressStart: (p: { slug: string; scenarios?: string[]; faults?: boolean }) => post<{ run: StressRun }>('/api/validation/stress', p),
  stressList: () => get<{ runs: StressRun[] }>('/api/validation/stress'),
  stressGet: (id: string) => get<{ run: StressRun }>(`/api/validation/stress/${id}`),
}
