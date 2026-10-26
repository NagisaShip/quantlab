/** 组合与风控的接口与类型：方案评估、风险体检、试验记录、方案库。 */
import { del, get, post, request } from './http'
import type { AdmissionCheck } from './apiFactors'
import type { GoalCond, GoalMetric } from './apiLab'

export type SchemeStatus = 'candidate' | 'active' | 'retired'

export interface PortfolioConfig {
  optimizer: string
  risk_model: string
  expected: string
  lookback: number
  rebalance: number
  risk_aversion?: number
  kelly_fraction?: number
  ewma_halflife?: number
  factors?: number
  cvar_alpha?: number
  tau?: number
  views?: { symbol: string; ret: number; confidence: number }[]
  constraints: { long_only: boolean; max_weight: number; min_weight?: number; max_gross: number; max_turnover: number; top_k: number }
  risk: { vol_target: number | null; var_limit: number | null; dd_stop: number | null; cppi: { floor: number; multiplier: number } | null }
  timeframe?: string
}

export interface PortfolioSummary {
  ann_return: number | null
  ann_vol: number | null
  sharpe: number | null
  max_drawdown: number | null
  calmar: number | null
  cvar_95: number | null
  turnover: number | null
  effective_n: number | null
  ew_sharpe: number | null
  ew_max_drawdown: number | null
  ew_ann_return: number | null
  beat_ew: number | null
  dd_vs_ew: number | null
  sharpe_h1: number | null
  sharpe_h2: number | null
  rebalances: number
  avg_exposure: number | null
  optimizer: string
  risk_model: string
  bars: number
  triggers: Record<string, number>
  cost_bps: number
}

export interface StressRow {
  id: string; name: string; method: string; note: string; pnl: number; worst: string | null; worst_pnl: number | null
  shocks: Record<string, number>
}

export interface PortfolioReport {
  summary: PortfolioSummary
  config: PortfolioConfig
  nav: { t: number; nav: number; ew: number; dd: number }[]
  weights: Record<string, number>[]
  latest?: {
    t: number; weights: Record<string, number>; cash: number | null; risk_contrib: Record<string, number>
    vol: Record<string, number>; expected: Record<string, number> | null; notes: string[]
    bl?: { prior: Record<string, number>; posterior: Record<string, number> }
  }
  risk?: {
    ann_vol: number; var_95_bar: number; cvar_95_bar: number; var_99_bar: number; cvar_99_bar: number; var_95_day_param: number
    corr?: { symbols: string[]; values: number[][] }
  }
  stress?: StressRow[]
  frontier?: {
    points: { vol: number; ret: number }[]
    current: { vol: number; ret: number }
    equal: { vol: number; ret: number }
    assets: { symbol: string; vol: number; ret: number }[]
    mu_source: string
  }
  checks: AdmissionCheck[]
  admitted: boolean
  activation_eligible?: boolean
  activation_block_reason?: string
  panel: { bars: number; symbols: string[]; timeframe: string; start: number | null; end: number | null; synthetic: boolean; warnings: string[] }
}

export interface PortfolioRun {
  pinned?: boolean
  id: string; hash: string; optimizer: string; risk_model: string; expected: string; symbols: string[]; timeframe: string
  summary: PortfolioSummary; admitted: boolean; source: string; project_id: string; note: string; synthetic: boolean; created_at: number
}

export interface Scheme {
  pinned?: boolean
  id: string; name: string; config: PortfolioConfig; symbols: string[]; timeframe: string
  summary: PortfolioSummary | null; admitted: boolean | null; latest: PortfolioReport['latest'] | null
  status: SchemeStatus; note: string; project_id: string | null; run_id: string | null; created_by: string
  created_at: number; updated_at: number
}

export interface PortfolioMeta {
  optimizers: { id: string; label: string; hint: string; needs_mu: boolean }[]
  risk_models: { id: string; label: string; hint: string }[]
  expected: { id: string; label: string }[]
  models: { name: string; status: string; label: string }[]
  factors: { name: string; status: string }[]
  scenarios: { id: string; name: string; note: string }[]
  defaults: PortfolioConfig
  universes: { id: string; name: string; symbols: string[] }[]
  statuses: Record<SchemeStatus, string>
  goal: { metrics: GoalMetric[]; templates: { id: string; name: string; items: GoalCond[] }[] }
  trials: number
}

export interface EvalParams {
  config: Partial<PortfolioConfig>
  universe?: string
  symbols?: string[]
  timeframe?: string
  bars_limit?: number
  source?: 'auto' | 'exchange' | 'synthetic'
  cost_bps?: number
  note?: string
}

export interface RiskCheck {
  weights: Record<string, number>
  ann_vol: number; var_95_bar: number; cvar_95_bar: number; var_95_day_param: number
  risk_contrib: Record<string, number>
  stress: StressRow[]
  panel: { synthetic: boolean; timeframe: string }
}

const patch = <T,>(p: string, body: unknown) => request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })

export const portfolio = {
  meta: () => get<PortfolioMeta>('/api/portfolio/meta'),
  evaluate: (p: EvalParams) => post<{ run: PortfolioRun; report: PortfolioReport }>('/api/portfolio/evaluate', p),
  risk: (p: { weights: Record<string, number>; timeframe?: string; source?: string; lookback?: number }) => post<RiskCheck>('/api/portfolio/risk', p),
  runs: () => get<{ runs: PortfolioRun[]; trials: number }>('/api/portfolio/runs'),
  run: (id: string) => get<{ run: PortfolioRun; report: PortfolioReport }>(`/api/portfolio/runs/${id}`),
  list: (status?: string) => get<{ schemes: Scheme[]; trials: number }>(`/api/portfolio${status ? `?status=${status}` : ''}`),
  save: (p: { run_id: string; name?: string; status?: SchemeStatus; note?: string; scheme_id?: string }) => post<{ scheme: Scheme }>('/api/portfolio', p),
  get: (id: string) => get<{ scheme: Scheme }>(`/api/portfolio/${id}`),
  report: (id: string) => get<{ report: PortfolioReport | null }>(`/api/portfolio/${id}/report`),
  update: (id: string, p: { name?: string; status?: SchemeStatus; note?: string }) => patch<{ scheme: Scheme }>(`/api/portfolio/${id}`, p),
  remove: (id: string) => del<{ ok: boolean }>(`/api/portfolio/${id}`),
}
