/** 模型库的接口与类型：训练（实验）、实验记录与对比、模型库、健康度。 */
import { del, get, post, request } from './http'
import type { AdmissionCheck } from './apiFactors'
import type { GoalCond, GoalMetric } from './apiLab'

export type ModelStatus = 'candidate' | 'active' | 'decaying' | 'retired'

export interface FeatureSpec { name: string; expression: string; factor_id?: string }

export interface ModelSpec {
  model: string
  label: string
  features: FeatureSpec[]
  horizon: number
  params: Record<string, unknown>
  symbols: string[]
  timeframe: string
  bars_limit: number
  start?: number | null
  end?: number | null
  source: 'auto' | 'exchange' | 'synthetic'
  cost_bps: number
  folds: number
  seed: number
  barrier_k?: number
  shuffle_test?: boolean
}

export interface ModelSummary {
  oos_rank_ic: number | null
  oos_ic?: number | null
  oos_icir?: number | null
  oos_ic_t?: number | null
  ls_sharpe?: number | null
  turnover?: number | null
  baseline_rank_ic?: number | null
  beat_baseline?: number | null
  dir_acc?: number | null
  brier?: number | null
  qlike?: number | null
  fold_positive?: number | null
  leak_passed?: number | null
  shuffle_rank_ic?: number | null
  psi_max?: number | null
  mode?: string
  model: string
  label: string
  task?: string
  horizon: number
  samples?: number
  features?: number
  trials?: number
  seconds?: number
  backend?: string | null
}

export interface CurvePoint { epoch: number; train: number; val: number }
export interface FoldRow { fold: number; train: number; val: number; test: number; start: number; end: number; rank_ic: number | null }

export interface ModelReport {
  summary: ModelSummary
  checks: AdmissionCheck[]
  admitted: boolean
  folds: FoldRow[]
  curve: CurvePoint[]
  fold_curves?: CurvePoint[][]
  importance: { name: string; drop: number | null }[]
  coefficients?: Record<string, number> | null
  psi: { name: string; psi: number | null }[]
  lookahead: { name: string; ok: boolean; max_diff: number | null }[]
  baseline: string
  latest?: { t: number; values: Record<string, number | null> }
  calibration?: { bin: number; pred: number; actual: number; n: number }[] | null
  decay?: { horizon: number; rank_ic: number | null }[]
  regimes?: { regime: string; rank_ic: number | null; periods: number }[]
  groups?: { n: number; mean: (number | null)[]; nav?: Record<string, number>[] }
  ls_nav?: { t: number; nav: number }[]
  ic_series?: { t: number; rank_ic: number; cum: number }[]
  panel: { bars: number; symbols: string[]; timeframe: string; start: number | null; end: number | null; synthetic: boolean; warnings: string[] }
  spec: ModelSpec
}

export interface ModelRun {
  id: string
  spec_hash: string
  model: string
  label: string
  horizon: number
  features: string[]
  symbols: string[]
  timeframe: string
  summary: ModelSummary
  admitted?: boolean
  source?: string
  project_id?: string | null
  note?: string
  created_at: number
}

export interface ModelCard {
  name: string
  purpose: string
  model: string
  params: Record<string, unknown>
  data: string
  features: string[]
  validation: string
  metrics: Record<string, number | null>
  drivers: string[]
  weaknesses: string[]
  seed?: number
}

export interface ModelRecord {
  id: string
  name: string
  run_id: string
  spec: ModelSpec
  summary: ModelSummary
  checks: AdmissionCheck[]
  admitted: boolean
  card: ModelCard
  status: ModelStatus
  frozen: boolean
  note: string
  project_id: string | null
  created_by: string
  train_end: number | null
  versions: { run_id: string; at: number; oos_rank_ic: number | null }[]
  health?: { t: number; live_rank_ic: number | null; live_bars: number; psi_max: number | null }[]
  created_at: number
  updated_at: number
}

export interface Learner {
  id: string; label: string; hint: string; family: string; params: Record<string, unknown>; available: boolean; needs?: string | null
}

export interface ModelMeta {
  learners: Learner[]
  labels: { id: string; label: string; task: string; hint: string }[]
  default_features: FeatureSpec[]
  backends: Record<string, boolean>
  universes: { id: string; name: string; symbols: string[] }[]
  statuses: Record<ModelStatus, string>
  goal: { metrics: GoalMetric[]; templates: { id: string; name: string; items: GoalCond[] }[] }
  runs: number
}

export interface TrainJob {
  id: string
  status: 'running' | 'done' | 'failed' | 'cancelled'
  phase: string
  model: string
  label: string
  error?: string
  run_id?: string
  run?: ModelRun
  started_at: number
  finished_at?: number
}

export interface TrainParams {
  model: string
  label?: string
  horizon?: number
  features?: (FeatureSpec | { factor_id: string } | string)[]
  params?: Record<string, unknown>
  universe?: string
  symbols?: string[]
  timeframe?: string
  bars_limit?: number
  source?: 'auto' | 'exchange' | 'synthetic'
  cost_bps?: number
  folds?: number
  seed?: number
  note?: string
}

export interface CompareView {
  metrics: { key: string; a: number | null; b: number | null }[]
  spec: { key: string; a: unknown; b: unknown }[]
  features: { a: string[]; b: string[] }
  curves: { a: CurvePoint[]; b: CurvePoint[] }
  folds: { a: FoldRow[]; b: FoldRow[] }
}

const patch = <T,>(p: string, body: unknown) => request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })

export const models = {
  meta: () => get<ModelMeta>('/api/models/meta'),
  train: (p: TrainParams) => post<{ job: TrainJob }>('/api/models/train', p),
  job: (id: string) => get<{ job: TrainJob }>(`/api/models/jobs/${id}`),
  cancel: (id: string) => post<{ job: TrainJob }>(`/api/models/jobs/${id}/cancel`),
  runs: (limit = 200) => get<{ runs: ModelRun[]; running: TrainJob[]; trials: number }>(`/api/models/runs?limit=${limit}`),
  run: (id: string) => get<{ run: ModelRun; report: ModelReport }>(`/api/models/runs/${id}`),
  removeRun: (id: string) => del<{ ok: boolean }>(`/api/models/runs/${id}`),
  compare: (a: string, b: string) => get<CompareView>(`/api/models/compare?a=${a}&b=${b}`),
  list: (status?: string) => get<{ models: ModelRecord[]; runs: number }>(`/api/models${status ? `?status=${status}` : ''}`),
  save: (p: { run_id: string; name?: string; status?: ModelStatus; note?: string; model_id?: string }) => post<{ model: ModelRecord }>('/api/models', p),
  get: (id: string) => get<{ model: ModelRecord }>(`/api/models/${id}`),
  report: (id: string) => get<{ report: ModelReport | null }>(`/api/models/${id}/report`),
  update: (id: string, p: { name?: string; status?: ModelStatus; note?: string; frozen?: boolean }) => patch<{ model: ModelRecord }>(`/api/models/${id}`, p),
  remove: (id: string) => del<{ ok: boolean }>(`/api/models/${id}`),
  refresh: (id: string, source: 'auto' | 'exchange' | 'synthetic' = 'auto') =>
    post<{ model: ModelRecord; entry: { live_rank_ic: number | null; live_bars: number; psi_max: number | null }; flag: string | null; psi: { name: string; psi: number | null }[]; latest: { t: number; values: Record<string, number | null> } }>(`/api/models/${id}/refresh`, { source }),
}

/** 在策略里引用研究成果的代码片段。 */
export const usageSnippet = (kind: 'factor' | 'predict' | 'optimize' | 'execution', name: string) => {
  const q = name.replace(/'/g, "\\'")
  if (kind === 'execution') {
    return `def initialize(ctx):\n    set_execution('${q}')     # 回测成交价按这个执行方案的成本曲线：下单越大、滑点越大\n    run_daily(rebalance, time="every_bar")\n\ndef rebalance(ctx):\n    order_target_weights({'BTC/USDT': 0.5, 'ETH/USDT': 0.5})\n`
  }
  if (kind === 'factor') {
    return `def rebalance(ctx):\n    score = factor('${q}')            # {标的: 因子值}，只用当前 bar 及以前的数据\n    if score:\n        top = sorted(score, key=score.get)[-3:]\n        order_target_weights({s: 1 / len(top) for s in top})\n\ndef initialize(ctx):\n    run_daily(rebalance, time="every_bar")\n`
  }
  if (kind === 'predict') {
    return `def rebalance(ctx):\n    mu = predict('${q}')              # {标的: 预测值}，回测时按滚动重训生成\n    if mu:\n        top = sorted(mu, key=mu.get)[-3:]\n        order_target_weights({s: 1 / len(top) for s in top})\n\ndef initialize(ctx):\n    run_daily(rebalance, time="every_bar")\n`
  }
  return `def rebalance(ctx):\n    mu = predict('你的模型')           # 可选：预期收益\n    w = optimize('${q}', expected=mu) # {标的: 目标权重}，已应用约束与风控\n    order_target_weights(w)\n\ndef initialize(ctx):\n    run_daily(rebalance, time="every_bar")\n`
}
