/** 因子库与行情与数据的接口与类型。 */
import { del, get, post, request } from './http'
import type { FactorSummary, GoalCond, GoalMetric, Hypothesis } from './apiLab'

export type FactorStatus = 'candidate' | 'active' | 'decaying' | 'retired'

export interface AdmissionCheck { key: string; label: string; ok: boolean; required: boolean; detail: string }

export interface FactorReport {
  expression?: string
  summary: FactorSummary
  checks: AdmissionCheck[]
  admitted: boolean
  activation_eligible?: boolean
  activation_block_reason?: string
  decay: { horizon: number; rank_ic: number | null }[]
  regimes: { regime: string; rank_ic: number | null; periods: number }[]
  correlations: { name: string; corr: number }[]
  groups: { n: number; mean: (number | null)[]; nav?: Record<string, number>[] }
  ls_nav: { t: number; nav: number }[]
  ic_series: { t: number; rank_ic: number; cum: number }[]
  panel: { bars: number; symbols: string[]; timeframe: string; start: number | null; end: number | null
    sources: Record<string, string>; field_sources: Record<string, string>; synthetic: boolean; warnings: string[] }
  settings?: { symbols: string[]; timeframe: string; horizon: number; bars_limit: number; source: string; cost_bps: number }
}

export interface Factor {
  id: string
  name: string
  expression: string
  hypothesis: Hypothesis
  status: FactorStatus
  universe: string[]
  timeframe: string
  horizon: number
  tags: string[]
  created_by: string
  project_id: string | null
  note: string
  summary?: FactorSummary
  admitted?: boolean
  evaluated_at?: number
  health?: { t: number; rank_ic: number | null; oos_rank_ic: number | null }[]
  health_flag?: string | null
  pinned?: boolean
  created_at: number
  updated_at: number
}

export interface OperatorDoc { name: string; sig: string; group: string; doc: string }
export interface FactorMeta {
  catalog: { fields: { name: string; label: string; group: string }[]; operators: OperatorDoc[] }
  custom_fields: string[]
  universes: { id: string; name: string; symbols: string[] }[]
  categories: Record<string, string>
  statuses: Record<FactorStatus, string>
  admission: Record<string, number>
  goal: { metrics: GoalMetric[]; templates: { id: string; name: string; items: GoalCond[] }[] }
  seeds: { name: string; expression: string; hypothesis: Hypothesis }[]
  trials: number
  limits: { max_window: number; max_nodes: number; max_depth: number }
}

export interface EvalParams {
  expression: string
  universe?: string
  symbols?: string[]
  timeframe?: string
  horizon?: number
  bars_limit?: number
  source?: 'auto' | 'exchange' | 'synthetic'
  cost_bps?: number
  exclude?: string
}

const patch = <T,>(p: string, body: unknown) => request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })

export const factors = {
  meta: () => get<FactorMeta>('/api/factors/meta'),
  validate: (expression: string) =>
    post<{ ok: boolean; error?: string; expression?: string; complexity?: number; fields?: string[]; lookback?: number }>('/api/factors/validate', { expression }),
  evaluate: (p: EvalParams) => post<FactorReport>('/api/factors/evaluate', p),
  list: (status?: string, q = '') =>
    get<{ factors: Factor[]; trials: number }>(`/api/factors?${status ? `status=${status}&` : ''}q=${encodeURIComponent(q)}`),
  get: (id: string) => get<{ factor: Factor }>(`/api/factors/${id}`),
  create: (p: { name?: string; expression: string; hypothesis?: Hypothesis; status?: FactorStatus; universe?: string[]; timeframe?: string; horizon?: number; note?: string; evaluate?: boolean }) =>
    post<{ factor: Factor }>('/api/factors', p),
  update: (id: string, p: { name?: string; hypothesis?: Hypothesis; tags?: string[]; note?: string }) => patch<{ factor: Factor }>(`/api/factors/${id}`, p),
  remove: (id: string) => del<{ ok: boolean }>(`/api/factors/${id}`),
  report: (id: string) => get<{ report: FactorReport }>(`/api/factors/${id}/report`),
  setStatus: (id: string, status: FactorStatus) => post<{ factor: Factor }>(`/api/factors/${id}/status`, { status }),
  refresh: (id: string, source: 'auto' | 'exchange' | 'synthetic' = 'auto') =>
    post<{ factor: Factor; report: FactorReport }>(`/api/factors/${id}/refresh`, { source }),
  trials: (limit = 100) => get<{ trials: { id: string; expression: string; count: number; rank_ic: number | null; ic_t: number | null; last_at: number; source: string }[]; total: number }>(`/api/factors/trials?limit=${limit}`),
}

// ---------------------------------------------------------------------------
// 行情与数据
// ---------------------------------------------------------------------------
export interface Connector {
  id: string; name: string; category: string; needs_key: boolean; key_optional?: boolean; fields: string[]; scope: string; note: string; url: string
  has_key: boolean | null; datasets: number; last_sync: number | null; errors: number
}

export interface Dataset {
  key: string; field: string; symbol: string | null; source: string; connector?: string; rows: number
  start: number | null; end: number | null; status?: string; error?: string; updated_at: number; series?: string; name?: string
  lag_ms?: number; last_available_at?: number | null
}

export interface MarketEvent {
  id: string; source: string; kind: 'announcement' | 'news' | 'macro' | string; type: string; title: string
  ts: number; available_at: number; url?: string; coins: string[]; direction: number; strength: number
  tagger: { kind?: string; model?: string; prompt_version?: string; prompt_sha?: string; version?: string; field?: string }
  tagged_at: number; hindsight_risk: boolean; extra?: Record<string, unknown>
}

export interface LiqStats { running: boolean; messages?: number; last_ts?: number | null; error?: string | null; [k: string]: unknown }

export interface AutoSync {
  enabled: boolean
  interval_hours: number
  last_run: number | null
  last_result: { groups: number; datasets: number; errors: string[]; at: number } | null
  due?: boolean
}

export const datahub = {
  connectors: () => get<{ connectors: Connector[]; field_scope: Record<string, string>; can_edit_keys: boolean }>('/api/datahub/connectors'),
  datasets: () => get<{ datasets: Dataset[] }>('/api/datahub/datasets'),
  series: (key: string, limit = 400) => get<{ dataset: Dataset; rows: [number, number, number][]; total: number }>(`/api/datahub/datasets/${key}/series?limit=${limit}`),
  remove: (key: string) => del<{ ok: boolean }>(`/api/datahub/datasets/${key}`),
  sync: (p: { connector: string; symbols?: string[]; period?: string; series?: string; name?: string; start?: number; wait?: boolean }) =>
    post<{ ok: boolean; queued?: boolean; datasets?: Dataset[]; errors?: { key: string; error: string }[] }>('/api/datahub/sync', p),
  importCsv: (p: { name: string; text: string; symbol?: string; lag_hours?: number }) => post<{ dataset: Dataset }>('/api/datahub/import', p),
  auto: () => get<AutoSync>('/api/datahub/auto'),
  setAuto: (enabled: boolean, interval_hours: number) => post<AutoSync>('/api/datahub/auto', { enabled, interval_hours }),
  autoRun: () => post<{ ok: boolean; queued: boolean }>('/api/datahub/auto/run'),
  setKey: (connector: string, key: string) => post<{ ok: boolean; has_key: boolean }>('/api/datahub/keys', { connector, key }),
  events: (p: { kind?: string; coin?: string; limit?: number } = {}) => {
    const q = new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]))
    return get<{ events: MarketEvent[]; total: number }>(`/api/datahub/events?${q}`)
  },
  liquidations: () => get<{ stats: LiqStats; pending_buckets: number }>('/api/datahub/liquidations'),
  liquidationsControl: (action: 'start' | 'stop' | 'flush') => post<{ stats: LiqStats; pending_buckets: number }>('/api/datahub/liquidations', { action }),
}
