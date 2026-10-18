/** 下单执行的接口与类型（全部是仿真）：执行方案仿真与 TCA、片段回放、盘口录制、成本校准、容量、市场雷达。 */
import { del, get, post, request } from './http'
import type { AdmissionCheck } from './apiFactors'
import type { GoalCond, GoalMetric } from './apiLab'

export type ExecStatus = 'candidate' | 'active' | 'retired'
export type ExecSource = 'auto' | 'kline' | 'synthetic' | 'recording'

export interface ExecConfig {
  algo: string
  horizon: number
  slices: number
  style: 'passive' | 'neutral' | 'aggressive'
  participation: number
  max_participation: number
  urgency: number
  display: number
  reprice_bps: number
  finish: boolean
  state_model: 'rules' | 'hmm'
  adapt: { behind: number; ahead: number; pause_illiquid: boolean }
  order: { side: 'buy' | 'sell' | 'both'; size_pct: number; notional?: number | null }
  fees: { maker_bps: number; taker_bps: number }
  impact: { resilience: number; permanent: number }
  episodes: number
}

export interface ExecSummary {
  is_bps: number; is_std_bps: number; is_p95_bps: number | null
  spread_bps: number; impact_bps: number; timing_bps: number; fee_bps: number; opportunity_bps: number
  vs_vwap_bps: number | null; fill_rate: number; maker_ratio: number; participation: number
  markout_1: number | null; markout_5: number | null; markout_15: number | null
  children: number; duration: number; episodes: number
  beat_twap_bps: number; beat_market_bps: number; twap_is_bps: number; market_is_bps: number
  algo: string; style: string; horizon: number; is_h1: number | null; is_h2: number | null; notional_avg: number
}

export interface TimelineRow { j: number; t: number; mid: number; mid_shift: number; target: number; filled: number; state: number; order_px: number | null; actions: string[] }
export interface ExecAlert { k: number; level: 'info' | 'warn' | 'alert'; text: string }
export interface ExecFill { j: number; qty: number; price: number; maker: boolean }
export interface FrontierPoint { label: string; algo: string; is_bps: number; is_std_bps: number; duration: number }
export interface TapeInfo { sources: string[]; synthetic: boolean; kline: boolean; warnings: string[]; symbols: string[] }

export interface ExecReport {
  summary: ExecSummary
  config: ExecConfig
  per_symbol: { symbol: string; source: string; is_bps: number; is_std_bps: number; fill_rate: number; maker_ratio: number; vs_vwap_bps: number | null; spread_bps: number | null; depth5_usd: number | null; volume_usd_per_step: number | null; note: string }[]
  by_state: { state: string; n: number; is_bps: number; twap_is_bps: number | null }[]
  frontier: FrontierPoint[]
  cost_curve: { points: { size_pct: number; is_bps: number; ex_fee_bps: number; notional: number }[]; a_bps: number; b_bps: number; r2: number; note: string }
  example: null | {
    timeline: TimelineRow[]; alerts: ExecAlert[]; fills: ExecFill[]; is_bps: number; spread_bps: number; impact_bps: number
    timing_bps: number; fee_bps: number; opportunity_bps: number; side: number; arrival: number; avg_price: number | null
    vwap: number; fill_rate: number; maker_ratio: number; symbol: string; t0: number
  }
  state_model: Record<string, unknown>
  episodes: { symbol: string; start: number; side: number; state: string; is_bps: number; fill_rate: number; maker_ratio: number }[]
  tape: TapeInfo
  checks: AdmissionCheck[]
  admitted: boolean
}

export interface ExecRun {
  pinned?: boolean
  id: string; hash: string; algo: string; style: string; horizon: number; symbols: string[]; summary: ExecSummary
  admitted: boolean; tape_sources: string[]; source: string; project_id: string; note: string; synthetic: boolean; created_at: number
}

export interface ExecScheme {
  pinned?: boolean
  id: string; name: string; config: ExecConfig; symbols: string[]; summary: ExecSummary | null; admitted: boolean | null
  cost_model: { a_bps: number; b_bps: number; r2: number } | null
  status: ExecStatus; note: string; project_id: string | null; run_id: string | null; created_by: string; created_at: number; updated_at: number
}

export interface Recording {
  id: string; exchange: string; symbol: string; seconds: number; interval: number; depth: number
  status: 'recording' | 'done' | 'stopped' | 'error'; snapshots: number; trades: number; error: string; created_at: number; finished_at: number | null
}

export interface CostModel {
  id: string | null; name: string; a_bps: number; Y: number; Y_se?: number; r2?: number; n?: number; used?: number
  mean_slip_bps?: number; symbols?: string[]; source?: string; active?: boolean; created_at?: number; note?: string
  points?: { ts: number; symbol: string; side: number; notional: number; slip_bps: number; x: number; part: number }[]
}

export interface ExecMeta {
  algos: { id: string; label: string; hint: string }[]
  styles: { id: string; label: string }[]
  states: string[]
  defaults: ExecConfig
  universes: { id: string; name: string; symbols: string[] }[]
  statuses: Record<ExecStatus, string>
  goal: { metrics: GoalMetric[]; templates: { id: string; name: string; items: GoalCond[] }[] }
  recordings: Recording[]
  cost_model: CostModel
  trials: number
}

export interface SimParams {
  config: Partial<ExecConfig>
  symbols: string[]
  source: ExecSource
  recording_id?: string | null
  steps?: number
  note?: string
}

export interface Replay {
  episode: number; episodes: number; symbol: string; source: string
  tape: { spread_bps: number | null; depth5_usd: number | null; volume_usd_per_step: number | null; note: string; warnings: string[] }
  result: Omit<NonNullable<ExecReport['example']>, 'timeline' | 'alerts' | 'fills' | 'symbol' | 't0'> & { vs_vwap_bps: number | null; markout_5: number | null }
  timeline: TimelineRow[]; alerts: ExecAlert[]; fills: ExecFill[]
  context: { t: number; mid: number; spread_bps: number; volume: number; state: number }[]
  offset: number; twap_is_bps: number; states: string[]
}

export interface RadarRow {
  symbol: string; price?: number; ret_1h?: number; ret_24h?: number; vol_ratio?: number; volume_z?: number; spread_bps?: number
  trend_z?: number; state?: string; alerts?: { level: 'info' | 'warn' | 'alert'; text: string }[]; spark?: number[]; t?: number
  synthetic?: boolean; error?: string
}

export interface Capacity {
  curve: { aum: number; cost_bps: number; annual_cost: number; net: number }[]
  half_alpha_aum: number | null; zero_alpha_aum: number | null; fixed_cost: number; alpha: number; turnover: number
  assets: { symbol: string; weight: number; adv_usd: number; sigma_d: number; synthetic?: boolean }[]
  model: { id: string | null; name: string; a_bps: number; Y: number }; synthetic: boolean
}

const patch = <T,>(p: string, body: unknown) => request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })

export const execution = {
  meta: () => get<ExecMeta>('/api/execution/meta'),
  evaluate: (p: SimParams) => post<{ run: ExecRun; report: ExecReport }>('/api/execution/evaluate', p),
  replay: (p: SimParams & { episode?: number; side?: 'buy' | 'sell' }) => post<Replay>('/api/execution/replay', p),
  runs: () => get<{ runs: ExecRun[]; trials: number }>('/api/execution/runs'),
  run: (id: string) => get<{ run: ExecRun; report: ExecReport }>(`/api/execution/runs/${id}`),
  list: (status?: string) => get<{ schemes: ExecScheme[]; trials: number }>(`/api/execution${status ? `?status=${status}` : ''}`),
  save: (p: { run_id: string; name?: string; status?: ExecStatus; note?: string }) => post<{ scheme: ExecScheme }>('/api/execution', p),
  get: (id: string) => get<{ scheme: ExecScheme }>(`/api/execution/${id}`),
  report: (id: string) => get<{ report: ExecReport | null }>(`/api/execution/${id}/report`),
  update: (id: string, p: { name?: string; status?: ExecStatus; note?: string }) => patch<{ scheme: ExecScheme }>(`/api/execution/${id}`, p),
  remove: (id: string) => del<{ ok: boolean }>(`/api/execution/${id}`),
  recordings: () => get<{ recordings: Recording[] }>('/api/execution/recordings'),
  record: (p: { exchange: string; symbol: string; seconds: number; interval: number }) => post<{ recording: Recording }>('/api/execution/recordings', p),
  stopRecording: (id: string) => post<{ ok: boolean }>(`/api/execution/recordings/${id}/stop`, {}),
  deleteRecording: (id: string) => del<{ ok: boolean }>(`/api/execution/recordings/${id}`),
  costs: () => get<{ models: CostModel[]; active: CostModel }>('/api/execution/costs'),
  calibrate: (p: { import_id: string; name?: string }) => post<{ model: CostModel }>('/api/execution/costs/calibrate', p),
  cost: (id: string) => get<{ model: CostModel }>(`/api/execution/costs/${id}`),
  activateCost: (id: string) => post<{ model: CostModel }>(`/api/execution/costs/${id}/activate`, {}),
  deleteCost: (id: string) => del<{ ok: boolean }>(`/api/execution/costs/${id}`),
  capacity: (p: { symbols: string[]; turnover: number; alpha: number; rebalances: number; fee_bps: number; cost_model_id?: string | null }) =>
    post<Capacity>('/api/execution/capacity', p),
  radar: (p: { universe?: string; timeframe?: string; source?: string }) =>
    get<{ rows: RadarRow[]; timeframe: string; synthetic: boolean; alerts: number }>(`/api/execution/radar?universe=${p.universe ?? 'major10'}&timeframe=${p.timeframe ?? '5m'}&source=${p.source ?? 'auto'}`),
}
