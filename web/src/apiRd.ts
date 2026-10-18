/** 试验台账与自动化的接口：试验台账（DSR / PBO）、紧封测试集、血缘图、定时任务、AI 晨报、积木搭建、影子运行、研究流水线。 */
import { del, get, post, put } from './http'

export type Domain = 'strategy' | 'factor' | 'model' | 'portfolio' | 'execution'
export type Verdict = 'ok' | 'warn' | 'bad'

export interface Family {
  domain: Domain; family: string; label: string; n: number; project_id: string | null; last_at: number
  best_name?: string; best_sharpe?: number; sr_std?: number; dsr?: number; sr0_ann?: number; z?: number; periods?: number
  best_t?: number; t_needed?: number; best_value?: number | null; verdict?: Verdict
}
export interface Trial {
  domain: Domain; id: string; family: string; family_label: string; name: string; sharpe?: number | null
  t_stat?: number | null; value?: number | null; count?: number; created_at: number; ref: string
}
export interface HoldoutView { sealed: boolean; cutoff?: number; set_at?: number; set_by?: string; note?: string; days?: number; unseals?: number }
export interface Ledger {
  by_domain: Partial<Record<Domain, number>>; total: number; total_alpha: number; t_needed_global: number
  families: Family[]; recent: Trial[]; domain_labels: Record<Domain, string>; holdout: HoldoutView
}
export interface Pbo {
  title: string; versions: string[]; best: string; pbo: number; n: number; t: number; splits: number; combos: number
  logit_median: number; prob_oos_loss: number; degradation_slope: number | null
  hist: { x0: number; x1: number; n: number }[]; scatter: { is: number; oos: number }[]
  dsr: { dsr: number; sr0_ann: number; n_trials: number; periods: number }; verdict: Verdict; sharpe: Record<string, number | null>
}
export interface UnsealRecord {
  id: string; kind: 'strategy' | 'factor' | 'model' | 'portfolio'; ref: string; name: string; user: string; at: number; cutoff: number
  result: Record<string, number | string | null>
}
export interface HoldoutLog { id: string; action: 'seal' | 'change' | 'remove'; user: string; at: number; cutoff?: number; previous?: number; note?: string }

export interface LNode { id: string; layer: string; label: string; ref?: string; status?: string; updated_at?: number; route?: string; detail?: string; stale?: boolean; missing?: string[] }
export interface Lineage {
  nodes: LNode[]; edges: { from: string; to: string; how: string }[]
  stale: { id: string; label: string; layer: string; because: { id: string; label: string; layer: string }[] }[]
  layers: { id: string; label: string }[]; trace?: { upstream: string[]; downstream: string[] }
}

export interface NightConfig { enabled: boolean; hour: number; minute: number; max_trials: number; max_tokens: number; max_minutes: number; rounds: number; brief: boolean; channel: string }
export interface NightTask {
  id: string; kind: Domain; idea: string; executor: 'solo' | 'team'; symbols: string[]; timeframe: string | null; rounds: number | null
  status: 'queued' | 'running' | 'done' | 'stopped' | 'failed'; created_at: number; project_id: string | null
  result: { status: string; headline?: string; passed?: boolean; admitted?: boolean } | null
}
export interface ShiftItem { task_id: string; kind: Domain; idea: string; status: string; project_id?: string; headline?: string; passed?: boolean; admitted?: boolean; versions?: number; error?: string; awaiting?: boolean }
export interface Shift {
  id: string; status: 'running' | 'done' | 'interrupted'; trigger: string; started_at: number; ended_at?: number; heartbeat?: number
  budget: { max_trials: number; max_tokens: number; max_minutes: number; rounds: number }
  used: { trials?: number; tokens?: number; minutes?: number }; items: ShiftItem[]; stop_reason: string | null; left?: number
}
export interface Brief {
  id: string; at: number; title: string; headline: string; focus: string[]; source: string; pushed: string[]; text: string
  sections: { key: string; title: string; lines: string[]; tone?: string; note?: string }[]
}

export interface AssembleOptions {
  universes: { id: string; name: string; symbols: string[] }[]
  factors: { name: string; status: string; detail: string }[]; models: { name: string; status: string; detail: string }[]
  schemes: { name: string; status: string; detail: string }[]; executions: { name: string; status: string; detail: string }[]
  signals: { id: string; label: string }[]; builders: { id: string; label: string }[]; timeframes: string[]
}
export interface Blocks {
  name: string; symbols: string[]; timeframe: string
  signal: { type: string; name?: string; lookback?: number }; build: { type: string; name?: string; k?: number }
  risk: { max_weight: number; cash: number; stop_dd: number }; execution: string; every: number
}
export interface Assembled {
  code: string; config: Blocks; outline: { step: string; text: string }[]; warnings: string[]; issues: string[]
  strategy?: { slug: string; name: string }
}

export interface ShadowCompare {
  hours: number; need_hours: number; progress: number; ret_base: number | null; ret_cand: number | null; dd_base: number | null; dd_cand: number | null
  excess: number | null; t: number; n: number; verdict: string; tone: string; ready: boolean; curve?: { t: number; base: number; cand: number }[]
}
export interface Shadow {
  id: string; baseline_id: string; candidate_id: string; label: string; baseline_name: string; candidate_name: string; min_days: number
  status: 'running' | 'promoted' | 'ended'; created_at: number; compare: ShadowCompare | null; baseline_status?: string; candidate_status?: string
}

export interface Pipeline {
  factors: { active: number; decaying: number; total: number }; models: { active: number; total: number; live_ic: number | null }
  portfolio: { active: number; risk_usage: number | null }; execution: { runs: number; avg_is_bps: number | null; active: number }
  nightshift: { running: boolean; queued: number; last: { id: string; started_at: number; status: string; stop_reason: string | null } | null; passed: number }
  discipline: { trials: number; holdout: boolean }; brief: { id: string; at: number; headline: string } | null
}

export const rd = {
  ledger: () => get<Ledger>('/api/rd/ledger'),
  pbo: (p: { project_id?: string; slug?: string }) => post<Pbo>('/api/rd/ledger/pbo', p),
  holdout: () => get<HoldoutView & { records: UnsealRecord[]; log: HoldoutLog[] }>('/api/rd/holdout'),
  seal: (p: { days?: number; cutoff?: number; note?: string }) => post<HoldoutView & { records: UnsealRecord[]; log: HoldoutLog[] }>('/api/rd/holdout', p),
  removeSeal: (reason: string) => post<HoldoutView>('/api/rd/holdout/remove', { reason }),
  unseal: (kind: UnsealRecord['kind'], ref: string) => post<{ record: UnsealRecord; again: boolean }>('/api/rd/holdout/unseal', { kind, ref }),
  lineage: (node?: string) => get<Lineage>(`/api/rd/lineage${node ? `?node=${encodeURIComponent(node)}` : ''}`),
  night: () => get<{ config: NightConfig; tasks: NightTask[]; running: Shift | null; shifts: Shift[]; kinds: Record<string, string> }>('/api/rd/nightshift'),
  nightConfig: (p: Partial<NightConfig>) => put<{ config: NightConfig }>('/api/rd/nightshift/config', p),
  addTask: (p: { kind: Domain | 'strategy'; idea: string; executor?: 'solo' | 'team'; symbols?: string[]; timeframe?: string; rounds?: number; data_source?: string }) =>
    post<{ task: NightTask }>('/api/rd/nightshift/tasks', p),
  removeTask: (id: string) => del<{ ok: boolean }>(`/api/rd/nightshift/tasks/${id}`),
  runNight: () => post<{ shift: Shift | null }>('/api/rd/nightshift/run', {}),
  stopNight: () => post<{ ok: boolean }>('/api/rd/nightshift/stop', {}),
  brief: () => get<{ brief: Brief | null; history: Pick<Brief, 'id' | 'at' | 'title' | 'headline' | 'source' | 'pushed'>[] }>('/api/rd/brief'),
  briefOne: (id: string) => get<{ brief: Brief }>(`/api/rd/brief/${id}`),
  makeBrief: (p: { push?: boolean; channel?: string }) => post<{ brief: Brief }>('/api/rd/brief', p),
  assembleOptions: () => get<AssembleOptions>('/api/rd/assemble/options'),
  assemble: (blocks: Blocks, create = false) => post<Assembled>('/api/rd/assemble', { blocks, create }),
  shadows: () => get<{ shadows: Shadow[] }>('/api/rd/shadows'),
  shadow: (id: string) => get<{ shadow: Shadow; compare: ShadowCompare | null; baseline: Record<string, unknown>; candidate: Record<string, unknown> }>(`/api/rd/shadows/${id}`),
  startShadow: (p: { baseline_session_id: string; candidate_slug: string; min_days: number }) => post<{ shadow: Shadow }>('/api/rd/shadows', p),
  promote: (id: string, force = false) => post<{ shadow: Shadow }>(`/api/rd/shadows/${id}/promote`, { force }),
  endShadow: (id: string) => post<{ shadow: Shadow }>(`/api/rd/shadows/${id}/end`, {}),
  pipeline: () => get<Pipeline>('/api/rd/pipeline'),
}

export const DOMAIN_LABEL: Record<Domain, string> = { strategy: '策略回测', factor: '因子', model: '模型', portfolio: '组合', execution: '执行' }
export const VERDICT_TONE: Record<Verdict, string> = { ok: '', warn: 'warn', bad: 'fail' }
export const day = (ms?: number | null) => (ms ? new Date(ms).toISOString().slice(0, 10) : '—')
export const when = (s?: number | null) => (s ? new Date(s * (s < 1e11 ? 1000 : 1)).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—')
