/** v2.0 接口：账号与工作区、运行（run）、规则 DSL 与版本、真实记录与只读账户、网络与横幅。 */

import { del, get, idemKey, post, put, withWs } from './http'

export type Dec = string
export type Role = 'admin' | 'member' | 'reviewer'

export interface Me {
  user: { id: string; email: string; name: string; is_admin: boolean }
  workspaces: { id: string; name: string; role: Role; is_demo?: boolean }[]
  workspace: { id: string; name: string; role: Role; is_demo: boolean; settings: WorkspaceSettings } | null
  live_enabled: boolean
}
export interface WorkspaceSettings {
  timezone?: string
  quotas: {
    max_concurrent_runs: number
    max_runs_per_day: number
    max_bars_per_run: number
    max_paper_runs: number
    allow_python_strategies: boolean
  }
}
export interface Member { user_id: string; name: string; email: string; role: Role; status: string; created_at: number; last_login_at: number | null }
export interface Invite { id: string; role: Role; email?: string | null; expires_at: number; created_at: number; code?: string; used_at?: number | null }
export interface AuditItem { id: number; ts: number; action: string; object_type: string; object_id: string; user_name: string | null; ip: string; trace_id: string; details: Record<string, unknown> }

export type RunState = 'QUEUED' | 'RUNNING' | 'PAUSED' | 'COMPLETED' | 'FAILED' | 'CANCELED' | 'RECOVERING'
export interface Run {
  id: string
  kind: string
  mode: string
  state: RunState
  title: string
  strategy_version_id: string | null
  lease_owner: string | null
  lease_expires_at: number | null
  attempts: number
  progress: { done: number; total: number }
  summary: Record<string, unknown>
  error: string | null
  error_code: string | null
  created_at: number
  started_at: number | null
  ended_at: number | null
  manifest: Record<string, unknown>
  terminal: boolean
}
export interface RunEvent { id: number; ts: number; type: string; data: Record<string, unknown> }

export interface DslProblem { path: string; message: string }
export interface DslValidation { ok: boolean; problems: DslProblem[]; warmup_bars?: number; summary?: string[]; warnings?: string[]; code_hash?: string }
export interface StrategyVersion {
  id: string
  strategy_slug: string
  version_no: number
  kind: string
  name: string
  code_hash: string
  params: Record<string, unknown>
  status: 'DRAFT' | 'VALIDATED' | 'FROZEN' | 'ARCHIVED'
  validation: { issues?: string[] }
  created_at: number
  code?: string
}

export interface LiveSource { id: string; mode: string; kind: string; title: string; created_at: number; fills: number; summary: Record<string, unknown> }
export interface LiveStatus {
  live_enabled: boolean
  has_live_data: boolean
  live_fills: number
  last_live_fill_ts: number | null
  message: string
  sources: LiveSource[]
  test_sources: LiveSource[]
  balances: { currency: string; free: Dec; locked: Dec; ts: number; source: string }[]
  connections: Connection[]
}
export interface ImportItem {
  id: string
  kind: 'fills' | 'flows' | 'balances'
  filename: string
  rows: number
  status: string
  run_id: string
  created_at: number
  mapping: Record<string, string>
  report: { instruments?: string[]; period?: [number, number] | null; warnings?: string[]; environment?: string; saved?: number; exchange?: string }
}
export interface ImportRowError { row: number; field: string; message: string }
export interface Connection {
  id: string
  connection_id: string
  exchange: string
  environment: 'live' | 'testnet'
  label: string
  key_mask: string
  status: 'active' | 'rejected' | 'expired' | 'revoked'
  scope: { read: boolean; trade: boolean; withdraw: boolean; account?: string }
  ip_restricted: boolean
  last_test: { at?: number; checks?: { name: string; ok: boolean; detail: string }[]; reasons?: string[]; error?: string }
  last_sync_at: number | null
  last_error: string | null
  created_at: number
}
export interface Guide { name: string; doc: string; steps: string[] }
export interface ComparisonPair {
  live: Record<string, unknown> & { ts: number; instrument: string; side: string; price: Dec; qty: Dec; fills?: number }
  sim: (Record<string, unknown> & { ts: number; price: Dec }) | null
  method: string
  confidence: number
  slippage_bps?: number | null
  qty_diff?: Dec
  fee_diff?: Dec
  delay_ms?: number
}
export interface Comparison {
  id: string
  sim_run_id?: string
  live_run_ids?: string[]
  summary: {
    live_fills: number
    attributed: number
    unattributed: number
    unmatched_sim: number
    by_method: Record<string, number>
    avg_confidence: number | null
    avg_slippage_bps: number | null
    // T08：在交易所被拆成多笔成交、已按订单归并为一行（价格取数量加权均价）的订单数。
    merged_orders: number
    attributed_result: { realized_pnl: Dec; fees: Dec; open_positions: Record<string, Dec>; note: string }
  }
  pairs: ComparisonPair[]
  unattributed: Record<string, unknown>[]
  unmatched_sim: Record<string, unknown>[]
  notes: string[]
  labels: { test: boolean; sim_mode: string; live_modes: string[] }
  flows: { count: number; net: Dec }
}
export interface NetworkStatus {
  overall: 'PENDING' | 'AVAILABLE' | 'DEGRADED' | 'UNREACHABLE'
  label: string
  note: string
  providers: { provider: string; verdict: string; observed_hours: number; samples_72h: number; success_rate_72h: number | null; p50_latency_ms: number | null; recent_errors: string[] }[]
}
export interface UsageItem {
  metric: string
  label: string
  unit: string
  period: 'daily' | 'total'
  used: Dec
  limit: Dec | null
  remaining: Dec | null
  used_percent: Dec | null
  exhausted: boolean
  quota_key: string
}
export interface UsagePanel {
  workspace_id: string
  day: string
  items: UsageItem[]
  history: { day: string; metric: string; value: Dec }[]
  note: string
}
export interface TotpStatus {
  enabled: boolean
  recovery_codes_left: number
  enabled_at: number | null
  locked_until: number | null
  required_for_admins: boolean
  setup_required: boolean
}
export interface Banner {
  mode_label: string
  live_enabled: boolean
  market: { source: string; exchange: string; symbol: string; last_event_ts: number | null; last_receive_ts: number | null; error: string }
  network: { overall: string; label: string }
  worker: { mode: string; online: number }
  isolation: { verified: boolean; mode: string }
  server_time: number
}

export const v4 = {
  // ---- 账号 ----
  authStatus: () => get<{ bootstrapped: boolean; open_registration: boolean }>('/api/auth/status'),
  register: (p: { email: string; password: string; name?: string; invite_code?: string }) => post<{ ok: boolean; workspace_id: string }>('/api/auth/register', p),
  login: (email: string, password: string, totp_code?: string) =>
    post<{ ok: boolean }>('/api/auth/login', { email, password, totp_code: totp_code || undefined }),
  logout: () => post<{ ok: boolean }>('/api/auth/logout'),
  me: () => get<Me>('/api/auth/me'),
  changePassword: (old_password: string, new_password: string) => post<{ ok: boolean }>('/api/auth/password', { old_password, new_password }),
  // ---- 双因素认证（T11）----
  // 明文密钥只在 setup 的响应里出现一次；恢复码明文只在 enable 的响应里出现一次。
  totpStatus: () => get<{ totp: TotpStatus }>('/api/auth/totp'),
  totpSetup: () => post<{ secret: string; otpauth_uri: string; digits: number; period: number; algorithm: string; note: string }>('/api/auth/totp/setup'),
  totpEnable: (code: string) => post<{ enabled: boolean; recovery_codes: string[]; note: string }>('/api/auth/totp/enable', { code }),
  totpDisable: (password: string, code: string) => post<{ enabled: boolean }>('/api/auth/totp/disable', { password, code }),
  totpRecoveryCodes: () => post<{ recovery_codes: string[]; note: string }>('/api/auth/totp/recovery-codes'),
  // ---- 工作区 ----
  createWorkspace: (name: string) => post<{ id: string }>('/api/workspaces', { name }),
  joinWorkspace: (code: string) => post<{ id: string }>('/api/workspaces/join', { code }),
  members: () => get<{ members: Member[]; invites: Invite[] }>('/api/workspace/members'),
  invite: (role: Role, email?: string) => post<{ invite: Invite; note: string }>('/api/workspace/invites', { role, email: email || undefined }),
  revokeInvite: (id: string) => del<{ ok: boolean }>(`/api/workspace/invites/${id}`),
  setRole: (userId: string, role: Role) => put<{ ok: boolean }>(`/api/workspace/members/${userId}`, { role }),
  removeMember: (userId: string) => del<{ ok: boolean }>(`/api/workspace/members/${userId}`),
  updateSettings: (p: { name?: string; timezone?: string; quotas?: Partial<WorkspaceSettings['quotas']> }) => put<{ workspace: unknown }>('/api/workspace/settings', p),
  // T07：用量面板。所有数值都是十进制字符串，前端只展示、不做浮点运算。
  usage: (days = 30) => get<UsagePanel>(`/api/workspace/usage?days=${days}`),
  /** 管理员自定义每日 / 存量上限；值为 null 表示不限制（单位与用量面板一致） */
  setUsageLimits: (p: Record<string, string | number | null>) => put<{ workspace_id: string; limits: Record<string, string | null> }>('/api/workspace/usage/limits', p),
  audit: (action?: string, before_id?: number) => get<{ items: AuditItem[] }>(`/api/workspace/audit?limit=200${action ? `&action=${encodeURIComponent(action)}` : ''}${before_id ? `&before_id=${before_id}` : ''}`),
  // ---- 运行 ----
  runs: (q: { kind?: string; mode?: string; state?: string } = {}) =>
    get<{ items: Run[]; worker_mode: string }>(`/api/runs?${new URLSearchParams(Object.entries(q).filter(([, v]) => v) as [string, string][]).toString()}`),
  run: (id: string) => get<{ run: Run; commands: { id: string; command: string; status: string; message: string; created_at: number }[] }>(`/api/runs/${id}`),
  runEvents: (id: string, after = 0) => get<{ items: RunEvent[]; cursor: number }>(`/api/runs/${id}/events?after=${after}`),
  runCommand: (id: string, command: 'pause' | 'resume' | 'cancel' | 'liquidate_and_stop' | 'tick') =>
    post<{ run: Run }>(`/api/runs/${id}/commands`, { command, idempotency_key: idemKey(command) }),
  workers: () => get<{ workers: { id: string; host: string; pid: number; last_seen: number; info: string }[]; mode: string; wake: string }>('/api/runs/workers'),
  // ---- DSL 与版本 ----
  dslSchema: () => get<{ schema: Record<string, unknown>; indicators: { type: string; label: string }[]; examples: Record<string, Record<string, unknown>>; notes: string[] }>('/api/dsl/schema'),
  dslValidate: (spec: unknown, params?: Record<string, number>) => post<DslValidation>('/api/dsl/validate', { spec, params }),
  dslSave: (p: { name: string; spec: unknown; slug?: string; notes?: string }) =>
    post<{ strategy: { slug: string; name: string }; version: StrategyVersion; compiled: DslValidation }>('/api/dsl/strategies', p, { 'Idempotency-Key': idemKey('dsl') }),
  versions: (slug: string) => get<{ versions: StrategyVersion[]; current: string }>(`/api/strategies/${slug}/versions`),
  version: (id: string) => get<{ version: StrategyVersion }>(`/api/strategy-versions/${id}`),
  freezeVersion: (id: string) => post<{ version: StrategyVersion }>(`/api/strategy-versions/${id}/freeze`),
  archiveVersion: (id: string) => post<{ version: StrategyVersion }>(`/api/strategy-versions/${id}/archive`),
  // ---- 真实记录 ----
  liveStatus: () => get<LiveStatus>('/api/live/status'),
  importSpec: () => get<{ kinds: Record<string, { required: string[]; optional: string[] }>; max_bytes: number; notes: string[] }>('/api/imports/spec'),
  importCsv: (p: { kind: string; filename: string; content: string; environment: string; exchange?: string; mapping?: Record<string, string> }) =>
    post<{ import: ImportItem }>('/api/imports', p, { 'Idempotency-Key': idemKey('import') }),
  imports: () => get<{ items: ImportItem[] }>('/api/imports'),
  importDetail: (id: string) => get<{ import: ImportItem; rows: Record<string, unknown>[] }>(`/api/imports/${id}`),
  deleteImport: (id: string) => del<{ ok: boolean }>(`/api/imports/${id}`),
  importExportUrl: (id: string) => withWs(`/api/imports/${id}/export.csv`),
  compare: (p: { sim_run_id: string; live_run_ids: string[]; window_minutes?: number }) =>
    post<{ run_id: string; comparison: Comparison | null }>('/api/comparisons', p),
  comparisons: () => get<{ items: { id: string; sim_run_id: string; live_run_id: string; created_at: number; summary: Comparison['summary']; labels: Comparison['labels'] }[] }>('/api/comparisons'),
  comparison: (id: string) => get<{ comparison: Comparison }>(`/api/comparisons/${id}`),
  // ---- 只读账户 ----
  accountGuide: () => get<{ exchanges: Record<string, Guide>; notes: string[]; live_enabled: boolean }>('/api/accounts/guide'),
  connections: () => get<{ items: Connection[] }>('/api/accounts/connections'),
  connect: (p: { exchange: string; environment: string; label: string; api_key: string; api_secret: string; passphrase?: string }) =>
    post<{ connection: Connection }>('/api/accounts/connections', p),
  testConnection: (id: string) => post<{ connection: Connection }>(`/api/accounts/connections/${id}/test`),
  syncConnection: (id: string, symbols: string[]) => post<{ run: { id: string; state: RunState; summary: Record<string, unknown> } }>(`/api/accounts/connections/${id}/sync`, { symbols }),
  revokeConnection: (id: string) => del<{ connection: Connection; note: string }>(`/api/accounts/connections/${id}`),
  // ---- 网络与横幅 ----
  banner: () => get<Banner>('/api/market/banner'),
  network: () => get<NetworkStatus>('/api/market/network'),
  probe: () => post<{ status: NetworkStatus }>('/api/market/network/probe'),
}

export const RUN_STATE: Record<RunState, [string, string]> = {
  QUEUED: ['排队中', 'pending'],
  RUNNING: ['运行中', ''],
  PAUSED: ['已暂停', 'pending'],
  COMPLETED: ['已完成', ''],
  FAILED: ['失败', 'fail'],
  CANCELED: ['已取消', 'pending'],
  RECOVERING: ['恢复中', 'pending'],
}
export const MODE_LABEL: Record<string, string> = {
  backtest: '历史回测',
  paper: '纸面模拟',
  imported_live: '导入实盘',
  readonly_live: '只读实盘',
  testnet: '测试网',
  analysis: '分析',
  agent: 'Agent',
}
export const KIND_LABEL: Record<string, string> = {
  backtest: '回测',
  optimize: '参数优化',
  paper: '纸面模拟',
  template_backtest: '模板回测',
  agent_job: '定时任务',
  account_sync: '账户同步',
  compare: '实盘对照',
  import: '导入',
}
