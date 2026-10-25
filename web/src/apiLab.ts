/** AI 策略室 + 指挥台 的接口与类型。 */
import { del, get, post, request, withWs } from './http'
import { streamSSE } from './components/chat/sse'

export type Effort = 'low' | 'medium' | 'high' | 'max'

export type Goals = { min_sharpe: number | null; max_drawdown: number | null; min_trades: number | null; min_excess: number | null }

export type Metrics = Partial<Record<
  'total_return' | 'annualized_return' | 'max_drawdown' | 'sharpe' | 'sortino' | 'volatility' | 'win_rate' |
  'benchmark_return' | 'excess_return' | 'trades' | 'total_fees' | 'calmar' | 'profit_factor', number | null>>

export interface GoalItem {
  key: string; label: string; op: string; target: number; actual: number | null; ok: boolean
  metric?: string; priority?: 'must' | 'try'; unit?: 'pct' | 'num' | 'int'
}

/** 结构化目标（/goal） */
export interface GoalCond { metric: string; op: '>=' | '<='; target: number; priority: 'must' | 'try' }
export interface GoalSpec {
  items: GoalCond[]; symbols: string[]; timeframe: string
  start: number | null; end: number | null; bars_limit: number; max_rounds: number; note?: string
  horizon?: number          // 因子研究：持有期（K 线数）
}
export interface GoalIssue { field: string; level: 'error' | 'warn'; problem: string; suggest: unknown; by?: string }
export interface GoalCheck {
  verdict: 'ok' | 'warn' | 'unreasonable'; issues: GoalIssue[]; summary: string; bars?: number | null
  model?: string; model_summary?: string
}
export interface GoalMetric { metric: string; label: string; unit: 'pct' | 'num' | 'int' | 'bps'; op: '>=' | '<='; hint?: string }
export interface Evaluation { passed: boolean; items: GoalItem[]; score: number }

export interface LabVersion {
  v: number
  code: string
  source: 'ai' | 'fallback' | 'user' | 'template'
  note: string
  model: string
  params: Record<string, number>
  checks_ok: boolean | null
  created_at: number
  metrics: Metrics | null
  bt_id: string | null
  evaluation: Evaluation | null
  name?: string
  hypothesis?: Hypothesis
  admitted?: boolean | null
}

export interface Pin { id: string; kind: string; title: string; text: string; created_at: number }

export type LabKind = 'strategy' | 'factor' | 'model' | 'portfolio' | 'execution'
export type Workflow = '' | 'auto' | 'pipeline' | 'report'
export interface ReportBrief { id: string; title: string; summary: string; created_at: number }
export interface Report extends ReportBrief {
  project_id: string; markdown: string; sources: string[]; securities: string[]; timeframe?: string; model?: string
}
/** 规格类研究（模型、组合、执行）：代码是一份 JSON 规格，提交后由平台训练 / 评估。 */
export const isSpecKind = (k?: LabKind) => k === 'model' || k === 'portfolio' || k === 'execution'
export type Executor = 'solo' | 'team'
/** 权限模式：自动 / 手动（改动与运行先问）/ 接受编辑 / 计划 / 跳过一切确认 */
export type Permission = 'auto' | 'manual' | 'accept_edits' | 'plan' | 'bypass'
/** 审批类别：完全权限下研究者自选哪些仍要先批准 */
export type ApprovalCategory = 'delete' | 'trading' | 'config' | 'save' | 'edit' | 'run'
export const APPROVAL_CATEGORIES: { id: ApprovalCategory; label: string; hint: string }[] = [
  { id: 'delete', label: '删除记录', hint: '删除策略、回测、因子等（不可恢复）' },
  { id: 'trading', label: '模拟交易与晋级', hint: '启动 / 停止模拟、晋级、微实盘急停' },
  { id: 'config', label: '改平台配置', hint: '校准回测成本等全局设置' },
  { id: 'save', label: '保存到库', hint: '保存为平台策略、入因子库 / 模型库' },
  { id: 'edit', label: '改代码与目标', hint: '提交新版本、修改目标与标的' },
  { id: 'run', label: '运行计算', hint: '回测、评估、训练、团队评审' },
]

/** 因子假设卡 */
export interface Hypothesis { category?: string; rationale?: string; direction?: string; falsify?: string }

/** 统一会话类型：五个研究室 + 从问答/工作流升级的研究或流程会话。 */
export type SessionKind = LabKind | 'research' | 'workflow' | 'ask'
/** 会话来源：普通新建、从临时问答升级、从工作流升级。 */
export type SessionOrigin = 'created' | 'ask_upgrade' | 'workflow_escalation'

/**
 * 列表/页头里的统一会话类型：
 * - 完整流程 → 工作流；调研报告 → 研究；
 * - 因子 / 模型 / 组合 / 执行 → 各自研究室；
 * - 其余（含问答升级后的 research 会话）按 kind，未知按策略。
 */
export function sessionKindOf(p: { kind?: string; workflow?: string }): SessionKind {
  if (p.workflow === 'pipeline') return 'workflow'
  if (p.workflow === 'report') return 'research'
  const k = p.kind
  if (k === 'factor' || k === 'model' || k === 'portfolio' || k === 'execution') return k
  if (k === 'research' || k === 'workflow' || k === 'ask') return k
  return 'strategy'
}

export interface LabProject {
  runtime_wait_reason?: 'admission' | ''
  runtime_chat_id?: string
  runtime_pending_ids?: string[]
  id: string
  kind?: LabKind
  universe?: string
  horizon?: number
  executor?: Executor
  permission?: Permission
  /** 完全权限（bypass）下仍要先审批的类别 */
  approvals?: ApprovalCategory[]
  team_id?: string
  cost_bps?: number
  hypothesis?: Hypothesis
  factor_id?: string | null
  model_id?: string | null
  library_id?: string | null
  title: string
  idea: string
  /** 会话来源：从临时问答升级的会话带 origin=ask_upgrade，页头显示「从问答升级」。 */
  origin?: SessionOrigin
  /** 创建来源引用（工作流/研究身份），由服务端写入，前端只读。 */
  context?: Record<string, unknown>
  securities: string[]
  timeframe: string
  model: string
  effort: Effort
  compacted_before?: number
  /** 对话管理：置顶、所属项目（文件夹）、分叉来源；设过目标后才能自动迭代；上下文超额时自动压缩 */
  pinned?: boolean
  folder?: string
  parent_id?: string | null
  fork_seq?: number | null
  goal_set?: boolean
  /** /plan 的分步计划：AI 一步步执行，直到全部完成且目标达标 */
  plan?: LabPlan | null
  auto_compact?: boolean
  /** 自动压缩阈值：模型上下文窗口的比例，默认 0.8（80%） */
  auto_compact_ratio?: number
  market_auto?: boolean
  suggest?: boolean
  /** 单轮最多调用工具的次数（设置里可改，默认 1000） */
  max_tool_calls?: number
  /** 研究流程：完整流程（选标的→因子→策略→模型→组合→执行）/ 调研报告 */
  workflow?: Workflow
  /** 团队讨论结论自动加入上下文（默认开） */
  auto_team_context?: boolean
  reports?: ReportBrief[]
  /** 子 Agent 的子对话：由哪个对话唤起 */
  subagent_of?: string | null
  goals: Goals
  goal?: (Partial<GoalSpec> & { items: GoalCond[] }) | null
  start?: number | null
  end?: number | null
  max_rounds: number
  bars_limit: number
  data_source: 'auto' | 'exchange' | 'synthetic'
  code: string
  current_v: number
  versions: LabVersion[]
  pins: Pin[]
  status: 'idle' | 'running' | 'awaiting_user'
  job: { kind: string; started_at: number; phase: string; detail?: string } | null
  slug: string | null
  archived: boolean
  created_at: number
  updated_at: number
}

export interface LabProjectBrief extends Omit<LabProject, 'versions' | 'code' | 'pins'> {
  versions_count: number
  metrics: (Metrics & Partial<FactorSummary>) | null
  passed: boolean | null
}

/** 统一上下文包（GET /api/lab/projects/{pid}/context-pack）：本轮会进入模型的只读投影。 */
export interface ContextPackItem {
  key: string
  title: string
  tokens: number
  fixed?: boolean
  /** 项目规则条目的全文；其余条目只有标题与 token。 */
  text?: string
  /** 来源引用条目的汇总（只有标题与 ID）。 */
  summary?: string
  pin_id?: string
  kind?: string
}

export interface ContextPack {
  format: string
  project_id: string
  kind: string
  title: string
  instructions: string
  context: Record<string, unknown>
  sources: string
  items: ContextPackItem[]
  total: number
  budget: number
  history_budget: number
  auto_compact_tokens: number
  model: string
}

export type PlanStepStatus = 'pending' | 'doing' | 'done' | 'skipped' | 'failed'
export interface LabPlan {
  task: string
  done_when: string
  steps: { text: string; status: PlanStepStatus; note: string }[]
  status: 'active' | 'done' | 'stopped'
  autorun: boolean
  turns: number
  seq?: number
}

export interface LabMessage {
  seq: number
  role: 'user' | 'assistant' | 'tool' | 'event'
  kind: 'text' | 'tool' | 'code' | 'backtest' | 'round' | 'team' | 'ask' | 'error' | 'saved' | 'goal' | 'walkforward' |
    'factor_code' | 'factor_report' | 'model_spec' | 'model_report' | 'portfolio_spec' | 'portfolio_report' | 'execution_spec' | 'execution_report' | 'discussion' | 'plan' |
    'report' | 'subagents' | 'cost_sweep' | 'stability' | 'parameter_heatmap' | 'stress_test'
  content: string
  data: Record<string, any>
  mode?: 'llm' | 'fallback'
  model?: string
  feedback?: { rating: 1 | -1; comment?: string; score?: number }
  created_at: number
  updated_at?: number
  rev?: number
}

export interface ContextItem { key: string; title: string; tokens: number; fixed: boolean; pin_id?: string; kind?: string }
export interface ContextView {
  items: ContextItem[]; total: number; history_tokens: number
  budget: number            // 当前模型的上下文窗口
  history_budget?: number   // 对话原文的保留额度
  auto_compact?: boolean
  auto_compact_ratio?: number
  auto_compact_tokens?: number
  model?: string
}

export interface LabView { project: LabProject; messages: LabMessage[]; context: ContextView; server_time: number }

export interface ModelOption {
  provider: string; model: string; target: string; type: string
  ready?: boolean; effort?: 'native' | 'budget' | 'switch' | 'auto' | 'none'; context_window?: number
  label?: string; request_model?: string; default_effort?: Effort | ''; inputs?: string[]
}
export interface EffortOption { id: Effort; label: string; hint: string }

export interface LabMeta {
  models: ModelOption[]
  default_model?: string
  efforts?: EffortOption[]
  routing: Record<string, string | string[]>
  llm_ready: boolean
  teams: { id: string; name: string; mode: string; roles: number }[]
  default_goals: Goals
  templates: { id: string; name: string }[]
  goal_metrics?: GoalMetric[]
  goal_templates?: { id: string; name: string; items: GoalCond[] }[]
  search?: { configured: boolean; provider?: string | null }
  domains?: Record<string, { key: string; label: string; metrics: GoalMetric[]; templates: { id: string; name: string; items: GoalCond[] }[]; defaults: Record<string, unknown> }>
  kinds?: { id: LabKind; label: string; hint: string }[]
  executors?: { id: Executor; label: string }[]
  universes?: { id: string; name: string; symbols: string[] }[]
}

/** 因子评估摘要（与后端 evaluate_factor.summary 对应） */
export interface FactorSummary {
  mode: 'cross' | 'time_series'; horizon: number; periods: number
  ic: number | null; rank_ic: number | null; ic_std: number | null; icir: number | null; ic_t: number | null; ic_win: number | null
  ls_sharpe: number | null; ls_return: number | null; ls_max_drawdown: number | null; turnover: number | null
  coverage: number | null; max_corr: number | null; is_rank_ic: number | null; oos_rank_ic: number | null
  oos_retention: number | null; sub_rank_ic: number | null; t_required: number; trials: number; direction: string; cost_bps: number
}

const patch = <T,>(p: string, body: unknown) => request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })

/** 设置 · AI 研究室 的本机偏好：单轮工具调用上限、团队结论自动进上下文。随每条消息带给服务端。 */
export const LAB_PREF_KEYS = { maxTools: 'qa.lab.maxTools', teamCtx: 'qa.lab.teamContext' }
export function labPrefs(): { max_tool_calls: number; auto_team_context: boolean } {
  let max = 1000
  let team = true
  try {
    max = Math.max(1, Math.min(5000, Number(localStorage.getItem(LAB_PREF_KEYS.maxTools) || 1000) || 1000))
    team = (localStorage.getItem(LAB_PREF_KEYS.teamCtx) ?? '1') === '1'
  } catch { /* 存储不可用：用默认值 */ }
  return { max_tool_calls: max, auto_team_context: team }
}

export const lab = {
  runtime: (id: string, runId?: string) => get<{ run: { id: string; state: string; seq: number; steps: { id: string; kind: string; state: string; payload: { name?: string; arguments?: Record<string, unknown> } }[] } }>(`/api/lab/projects/${id}/runtime${runId ? '?run_id=' + encodeURIComponent(runId) : ''}`),
  resumeRuntime: (id: string, runId?: string) => post<LabView>(`/api/lab/projects/${id}/runtime/resume${runId ? '?run_id=' + encodeURIComponent(runId) : ''}`, { wait: false }),
  resolveEffect: (id: string, payload: { step_id: string; signal_id: string; outcome: 'succeeded' | 'absent'; result?: unknown }, runId?: string) =>
    post<{ accepted: boolean; state: string }>(`/api/lab/projects/${id}/runtime/effect-resolution${runId ? '?run_id=' + encodeURIComponent(runId) : ''}`, payload),
  resolveModelUsage: (id: string, payload: { step_id: string; usage?: { input_tokens: number; output_tokens: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number }; no_charge_confirmed?: boolean }, runId?: string) =>
    post<{ accepted: boolean; state: string }>(`/api/lab/projects/${id}/runtime/model-usage${runId ? '?run_id=' + encodeURIComponent(runId) : ''}`, payload),
  meta: () => get<LabMeta>('/api/lab/meta'),
  projects: (archived = false) => get<{ projects: LabProjectBrief[] }>(`/api/lab/projects?archived=${archived}`),
  create: (p: Partial<Omit<LabProject, 'goals' | 'pins' | 'versions'>> & { goals?: Partial<Goals>; template_id?: string; pins?: { kind?: string; title: string; text: string }[] }) =>
    post<LabView>('/api/lab/projects', p),
  view: (id: string, after = 0, since = 0) => get<LabView>(`/api/lab/projects/${id}?after=${after}&since=${since}`),
  /** 统一上下文包：系统规则、项目规则、来源引用与预算（只读，不改变提示词）。 */
  contextPack: (id: string) => get<ContextPack>(`/api/lab/projects/${encodeURIComponent(id)}/context-pack`),
  update: (id: string, p: Partial<Omit<LabProject, 'goals'>> & { goals?: Partial<Goals> }) => patch<{ project: LabProject }>(`/api/lab/projects/${id}`, p),
  /** cascade：连同它的全部分叉一起删除 */
  remove: (id: string, cascade = false) => del<{ ok: boolean; deleted?: string[] }>(`/api/lab/projects/${id}${cascade ? '?cascade=true' : ''}`),
  send: (id: string, text: string, opts: { model?: string; effort?: Effort; suggest?: boolean; images?: { data_url: string; name?: string }[] } = {}) =>
    post<LabView>(`/api/lab/projects/${id}/messages`, { text, ...labPrefs(), ...opts }),
  feedback: (id: string, seq: number, rating: 1 | -1, comment = '', score?: number) =>
    post<{ ok: boolean; feedback: { rating: 1 | -1; comment: string; score?: number } | null }>(`/api/lab/projects/${id}/messages/${seq}/feedback`, { rating, comment, score }),
  /** /report：调研报告流程（列提纲 → 搜集资料 → 分析 → 生成报告） */
  report: (id: string, text = '') => post<LabView>(`/api/lab/projects/${id}/report`, { text }),
  getReport: (id: string, rid: string) => get<{ report: Report; project: Pick<LabProject, 'id' | 'title' | 'securities' | 'timeframe'> }>(`/api/lab/projects/${id}/reports/${rid}`),
  imageUrl: (id: string, imageId: string) => withWs(`/api/lab/projects/${id}/images/${encodeURIComponent(imageId)}`),
  goalParse: (text: string, project_id?: string, domain?: LabKind) =>
    post<{ spec: GoalSpec; parsed_fields: string[]; unparsed: string; check: GoalCheck }>('/api/lab/goal/parse', { text, project_id, domain }),
  goalCheck: (spec: Partial<GoalSpec>, project_id?: string, review = false, domain?: LabKind) =>
    post<{ spec: GoalSpec; check: GoalCheck; suggested: GoalSpec }>('/api/lab/goal/check', { spec, project_id, review, domain }),
  setGoal: (id: string, spec: Partial<GoalSpec>, opts: { start?: boolean; review?: boolean; research?: 'none' | 'market' | 'team' } = {}) =>
    post<LabView & { goal: { spec: GoalSpec; check: GoalCheck } }>(`/api/lab/projects/${id}/goal`, { spec, review: true, ...opts }),
  walkForward: (id: string, segments = 3) => post<LabView>(`/api/lab/projects/${id}/walkforward`, { segments }),
  compact: (id: string) => post<LabView>(`/api/lab/projects/${id}/compact`),
  fork: (id: string, seq?: number, title = '') => post<{ project: LabProjectBrief }>(`/api/lab/projects/${id}/fork`, { seq, title }),
  rewind: (id: string, seq: number, text?: string) => post<LabView>(`/api/lab/projects/${id}/rewind`, { seq, text }),
  /** /plan：text 为空时继续执行当前计划 */
  plan: (id: string, text = '') => post<LabView>(`/api/lab/projects/${id}/plan`, { text }),
  discussion: (id: string, mission_id: string, topic: string) => post<LabView>(`/api/lab/projects/${id}/discussion`, { mission_id, topic }),
  /** 实时推送：回合结束（或超时）后流自动关闭；返回 Promise 在关闭时 resolve。 */
  events: (id: string, after: number, since: number, onData: (v: Partial<LabView> & { project: LabProject; messages: LabMessage[]; server_time: number }) => void, signal?: AbortSignal) =>
    streamSSE(`/api/lab/projects/${id}/events?after=${after}&since=${since}`, onData, signal),
  autopilot: (id: string, p: { rounds?: number; research?: 'none' | 'market' | 'team'; team_id?: string | null }) =>
    post<LabView>(`/api/lab/projects/${id}/autopilot`, p),
  decide: (id: string, choice: string, note = '') => post<LabView>(`/api/lab/projects/${id}/decision`, { choice, note }),
  stop: (id: string) => post<{ ok: boolean }>(`/api/lab/projects/${id}/stop`),
  /** 回答里的内容块渲染失败：交回 AI 自动修正（服务端保证每条消息只修一次） */
  renderErrors: (id: string, seq: number, errors: { kind: string; message: string; source: string }[]) =>
    post<{ ok?: boolean; skipped?: boolean }>(`/api/lab/projects/${id}/render-errors`, { seq, errors }),
  backtest: (id: string, cost_scale = 1) => post<LabView>(`/api/lab/projects/${id}/backtest`, { cost_scale }),
  /** 成本压力扫描：并行回测多档成本倍数，找出不再满足目标的极限 */
  costSweep: (id: string, scales: number[] = []) => post<LabView>(`/api/lab/projects/${id}/cost-sweep`, { scales }),
  parameterHeatmap: (id: string, p: { x_param?: string; y_param?: string; x_values?: number[]; y_values?: number[]; metric?: string } = {}) =>
    post<LabView>(`/api/lab/projects/${id}/parameter-heatmap`, p),
  stressTest: (id: string, p: { segments?: number; windows?: number; cost_scales?: number[]; sensitivity?: boolean } = {}) =>
    post<LabView>(`/api/lab/projects/${id}/stress-test`, p),
  saveCode: (id: string, code: string, note = '手动编辑') => post<LabView>(`/api/lab/projects/${id}/code`, { code, note }),
  restore: (id: string, v: number) => post<LabView>(`/api/lab/projects/${id}/versions/${v}/restore`),
  pin: (id: string, p: { kind?: string; title: string; text: string }) => post<{ pin: Pin; context: ContextView }>(`/api/lab/projects/${id}/pins`, p),
  unpin: (id: string, pinId: string) => del<{ ok: boolean; context: ContextView }>(`/api/lab/projects/${id}/pins/${pinId}`),
  saveStrategy: (id: string) => post<{ strategy: { slug: string; name: string; version: string } }>(`/api/lab/projects/${id}/save`),
  saveFactor: (id: string) => post<{ factor: { id: string; name: string } }>(`/api/lab/projects/${id}/save`),
  saveDomain: (id: string) => post<Record<string, { id: string; name: string; status?: string }>>(`/api/lab/projects/${id}/save`),
}

// ---------------------------------------------------------------------------
// 指挥台
// ---------------------------------------------------------------------------
export interface Stage { key: string; name: string; mode: 'sequential' | 'parallel' | 'debate'; role_ids: string[]; gate: boolean; status?: 'pending' | 'running' | 'done' }
export interface RosterRole { id: string; name: string; title: string; model: string; effort?: string; avatar?: string; tags: string[] }

export interface Mission {
  id: string
  directive: string
  context: string
  mode: 'single' | 'pipeline'
  preset: string | null
  team_id: string | null
  stages: Stage[]
  synthesizer_id: string
  roster: RosterRole[]
  symbol: string
  timeframe: string
  lab_project_id: string | null
  cursor: number
  status: 'queued' | 'running' | 'awaiting_approval' | 'done' | 'failed' | 'stopped' | 'idle'
  conclusion: string
  created_at: number
  updated_at: number
}

export interface Utterance {
  seq: number
  kind: 'speech' | 'challenge' | 'synthesis' | 'commander' | 'system' | 'stage' | 'gate'
  stage?: number
  role_id?: string
  role_name?: string
  content: string
  status: 'typing' | 'done'
  model?: string
  mode?: 'llm' | 'fallback'
  elapsed_ms?: number
  data?: Record<string, any>
  created_at: number
  updated_at: number
}

export interface PipelinePreset { id: string; name: string; description: string; stages: Stage[]; synthesizer_id: string; custom?: boolean }
export interface TeamLite { id: string; name: string; description: string; mode: string; role_ids: string[]; synthesizer_id: string }
export interface RoleLite { id: string; name: string; title: string; tags: string[]; model: string; effort?: string; avatar?: string }

export interface MissionView { mission: Mission; feed: Utterance[]; server_time: number }

export const missions = {
  presets: () => get<{ pipelines: PipelinePreset[]; teams: TeamLite[]; roles: RoleLite[] }>('/api/missions/presets'),
  list: () => get<{ missions: Mission[] }>('/api/missions'),
  create: (p: {
    directive: string; context?: string; mode: 'single' | 'pipeline'; team_id?: string | null; preset?: string | null
    stages?: Stage[] | null; symbol?: string; timeframe?: string; include_market_data?: boolean; strategy?: string | null
    lab_project_id?: string | null; synthesizer_id?: string
  }) => post<MissionView>('/api/missions', p),
  view: (id: string, after = 0, since = 0) => get<MissionView>(`/api/missions/${id}?after=${after}&since=${since}`),
  approve: (id: string) => post<MissionView>(`/api/missions/${id}/approve`, {}),
  say: (id: string, text: string, to_role?: string | null) => post<MissionView>(`/api/missions/${id}/say`, { text, to_role: to_role || null }),
  stop: (id: string) => post<{ ok: boolean }>(`/api/missions/${id}/stop`),
  remove: (id: string) => del<{ ok: boolean }>(`/api/missions/${id}`),
  savePipeline: (p: { id?: string; name: string; description?: string; stages: Stage[]; synthesizer_id: string }) =>
    post<{ pipeline: PipelinePreset }>('/api/missions/pipelines', p),
  deletePipeline: (id: string) => del<{ ok: boolean }>(`/api/missions/pipelines/${id}`),
}

/** 增量合并：按 seq 覆盖（工具卡片 / 占位气泡会被原地更新）。 */
export function mergeBySeq<T extends { seq: number }>(prev: T[], incoming: T[]): T[] {
  if (!incoming.length) return prev
  const map = new Map(prev.map((m) => [m.seq, m]))
  incoming.forEach((m) => map.set(m.seq, m))
  return [...map.values()].sort((a, b) => a.seq - b.seq)
}
