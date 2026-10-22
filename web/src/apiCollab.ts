/**
 * 工作区协作（群组 / 任务 / 可见性 / 编辑标记与血缘）、通知中心与屏蔽、问题报告与上传、部署运维控制台。
 */
import { ApiError, del, get, post, put, targetWorkspace } from './http'
import type { LimitRule, LimitStatus } from './api/research'

/** report：AI 研究室调研报告（id = 对话/报告）；link：站内任意页面（id 是不带 #/ 的路由） */
export type RefType = 'backtest' | 'strategy' | 'code' | 'file' | 'post' | 'task' | 'project' | 'message' | 'report' | 'link'
export interface Ref {
  type: RefType
  id: string
  title?: string
  summary?: Record<string, string | number | null>
  code?: string
  lang?: string
  mime?: string
  size?: number
}

export interface Member { user_id: string; name: string; username?: string; role: string; muted?: boolean; online?: boolean; avatar_url?: string; avatar_color?: string }
export type GroupRole = 'owner' | 'admin' | 'member'
export interface Group {
  id: string
  name: string
  /** dm：两人私聊 */
  kind?: 'group' | 'dm'
  peer?: { user_id: string; name: string } | null
  dm_can_send?: boolean
  dm_reason?: string
  dm_policy?: 'reply_only' | 'open' | 'none'
  description: string
  visibility: 'open' | 'private'
  quantlab_no: string
  owner_id: string
  created_at: number
  archived: boolean
  member_count: number
  my_role: GroupRole | null
  unread: number
  open_tasks: number
  last_message: { body: string; author: string; at: number } | null
  ws_limits: LimitRule[]
  member_limits: LimitRule[]
  members?: { user_id: string; name: string; role: GroupRole; joined_at: number; avatar_url?: string; avatar_color?: string; kind?: 'user' | 'bot'; is_bot?: boolean }[]
}
export interface BotModelOption {
  target: string
  provider: string
  model: string
  label: string
  scope: string
  ready: boolean
}
export interface GroupBotConfig {
  enabled: boolean
  name: string
  member_id: string
  model_target: string
  system_prompt: string
  personality: string
  qmoji_enabled: boolean
  /** 回复别人时默认带上 @对方；策略广场 Bot 沿用同一开关 */
  reply_default_mention: boolean
  created_by: string
  created_at: number
  updated_at: number
  updated_by: string
}
export interface QmojiReaction { emoji: string; count: number; mine: boolean }

export interface Message {
  id: number
  group_id: string
  author_id: string
  author_name: string
  author_kind: 'user' | 'agent' | 'system'
  body: string
  refs: Ref[]
  reply_to: number | null
  pinned: boolean
  created_at: number
  edited_at: number | null
  deleted: boolean
  muted?: boolean
  reactions: QmojiReaction[]
}
export type TaskStatus = 'todo' | 'doing' | 'review' | 'done' | 'cancelled'
export type TaskPriority = 'low' | 'normal' | 'high' | 'urgent'
export interface Task {
  id: string
  group_id: string
  group_name?: string
  title: string
  description: string
  assignee_id: string | null
  assignee_name: string
  created_by: string
  creator_name: string
  status: TaskStatus
  status_label: string
  priority: TaskPriority
  due_at: number | null
  refs: Ref[]
  created_at: number
  updated_at: number
  done_at: number | null
  overdue: boolean
}
export type Visibility = 'private' | 'workspace' | 'group'
export interface ShareInfo {
  share: { visibility: Visibility; group_ids: string[]; owner_id: string | null }
  owner_name: string
  can_edit: boolean
  groups: { id: string; name: string }[]
}
export interface EditorMark { user_id: string; name: string; actor_kind: 'human' | 'agent'; count: number; last_at: number }
export interface EditRow {
  id: number; user_id: string; user_name: string; actor_kind: 'human' | 'agent'; action: string; version: string
  parent_version: string; derived_from: string; note: string; created_at: number
}
export interface LineageNode {
  id: string
  kind: 'version' | 'post'
  slug: string
  version: string
  name: string
  exists: boolean
  current: boolean
  status: string
  author: { user_id: string; name: string; actor_kind: 'human' | 'agent' } | null
  editors: { user_id: string; name: string; actor_kind: 'human' | 'agent' }[]
  at: number | null
  action: string
  note: string
  backtests: { id: string; created_at: number; by: string; status: string; data_sources: string[]; metrics: Record<string, number | null> }[]
}
export interface LineageEdge { from: string; to: string; kind: 'edit' | 'clone' }

export interface Notice {
  id: number
  kind: string
  title: string
  body: string
  link: string
  actor_id: string | null
  actor_name: string
  source_type: string
  source_id: string
  workspace_id: string | null
  created_at: number
  read_at: number | null
}
export interface Mute { target_type: 'user' | 'group' | 'kind' | 'post'; target_id: string; label: string; created_at: number }
export interface InboxSummary {
  unread: Record<string, number>
  banner: { text: string; body?: string; tone?: 'info' | 'warn'; until?: number | null; at?: number } | null
  tasks?: number
  group_unread?: number
  open_issues?: number
}

export interface Upload { id: string; filename: string; mime: string; size: number; url?: string; thumb_url?: string | null }
export interface Issue {
  id: string
  workspace_id: string | null
  user_id: string
  user_name: string
  title: string
  body: string
  category: string
  category_label: string
  severity: 'low' | 'normal' | 'high' | 'critical'
  status: 'open' | 'replied' | 'resolved' | 'closed'
  status_label: string
  attachments: Upload[]
  context: Record<string, string>
  created_at: number
  updated_at: number
  reply_count: number
  replies?: { id: number; user_id: string; user_name: string; is_staff: boolean; body: string; attachments: Upload[]; created_at: number }[]
}

export const NOTIFY_KIND_LABEL: Record<string, string> = {
  mention: '@我的', dm: '私聊', task: '任务', group: '群组', comment: '评论与回复', like: '点赞与克隆', follow: '关注', share: '共享',
  issue: '问题报告', announce: '公告', system: '系统',
}
export const TASK_COLUMNS: { key: TaskStatus; label: string }[] = [
  { key: 'todo', label: '待办' }, { key: 'doing', label: '进行中' }, { key: 'review', label: '待验收' }, { key: 'done', label: '已完成' },
]
export const PRIORITY_LABEL: Record<TaskPriority, string> = { low: '低', normal: '普通', high: '高', urgent: '紧急' }
export const VIS_LABEL: Record<Visibility, string> = { private: '仅自己', workspace: '工作区内公开', group: '指定群组内公开' }

const q = (o: Record<string, unknown>) => {
  const s = Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== false)
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join('&')
  return s ? `?${s}` : ''
}

export type PublicShareType = 'group' | 'user' | 'post' | 'strategy'
export interface PublicShareCreated { token: string; title: string; route: string; type: PublicShareType }
export interface DirectoryUser {
  id: string
  name: string
  username: string
  quantlab_uid: string
  bio: string
  avatar_url?: string; avatar_color: string
  role: string
}
export interface DiscoverResult { query: string; groups: Group[]; users: DirectoryUser[] }

export const collab = {
  members: (query = '') => get<{ members: Member[] }>(`/api/collab/members${q({ q: query || undefined })}`),
  /** 打开（没有就新建）与某位成员的私聊 */
  dm: (user_id: string) => post<{ group: Group }>('/api/collab/dm', { user_id }),
  /** 把任意内容分享到群组 / 私聊 / 策略广场 */
  shareTo: (p: { target: 'group' | 'dm' | 'square'; target_id?: string; ref: Ref; note?: string; title?: string }) =>
    post<{ ok: boolean; link: string }>('/api/collab/share-to', p),
  /** 在线心跳：返回同时在看 / 编辑这个对象的其他成员 */
  presence: (p: { object_type: string; object_id: string; editing?: boolean; leave?: boolean }) =>
    post<{ others: { user_id: string; name: string; editing: boolean }[] }>('/api/collab/presence', p),
  groups: () => get<{ groups: Group[] }>('/api/collab/groups'),
  discover: (query = '') => get<DiscoverResult>(`/api/collab/discover${q({ q: query || undefined })}`),
  group: (id: string) => get<{ group: Group }>(`/api/collab/groups/${id}`),
  createGroup: (p: { name: string; description?: string; visibility: 'open' | 'private'; members?: string[] }) =>
    post<{ group: Group }>('/api/collab/groups', p),
  updateGroup: (id: string, p: Partial<{ name: string; description: string; visibility: 'open' | 'private'; member_limits: LimitRule[] }>) =>
    put<{ group: Group }>(`/api/collab/groups/${id}`, p),
  setGroupQuota: (id: string, ws_limits: LimitRule[]) => put<{ group: Group }>(`/api/collab/groups/${id}/quota`, { ws_limits }),
  archiveGroup: (id: string) => del<{ ok: boolean }>(`/api/collab/groups/${id}`),
  rotateGroupNumber: (id: string) => post<{ quantlab_no: string }>(`/api/collab/groups/${id}/number/rotate`, {}),
  createPublicShare: (object_type: PublicShareType, object_id: string) => post<{ share: PublicShareCreated }>('/api/collab/public-shares', { object_type, object_id }),
  revokePublicShare: (token: string) => del<{ ok: boolean }>(`/api/collab/public-shares/${encodeURIComponent(token)}`),
  join: (id: string) => post<{ group: Group }>(`/api/collab/groups/${id}/join`),
  addMembers: (id: string, user_ids: string[]) => post<{ added: string[]; group: Group }>(`/api/collab/groups/${id}/members`, { user_ids }),
  removeMember: (id: string, uid: string) => del<{ ok: boolean }>(`/api/collab/groups/${id}/members/${uid}`),
  setMemberRole: (id: string, uid: string, role: GroupRole) => put<{ group: Group }>(`/api/collab/groups/${id}/members/${uid}`, { role }),
  messages: (id: string, o: { after?: number; before?: number } = {}) =>
    get<{ messages: Message[]; pinned: Message[] | null }>(`/api/collab/groups/${id}/messages${q(o)}`),
  send: (id: string, body: string, refs: Ref[], reply_to?: number | null) =>
    post<{ message: Message; bot_message?: Message | null }>(`/api/collab/groups/${id}/messages`, { body, refs, reply_to: reply_to ?? null }),
  editMessage: (mid: number, body: string) => put<{ message: Message }>(`/api/collab/messages/${mid}`, { body }),
  deleteMessage: (mid: number) => del<{ ok: boolean }>(`/api/collab/messages/${mid}`),
  pinMessage: (mid: number) => post<{ pinned: boolean }>(`/api/collab/messages/${mid}/pin`),
  qmojiMessage: (mid: number, emoji: string) => post<{ emoji: string; on: boolean; count: number; reactions: QmojiReaction[] }>(`/api/collab/messages/${mid}/qmoji`, { emoji }),
  markRead: (id: string, last_id: number) => post<{ ok: boolean }>(`/api/collab/groups/${id}/read`, { last_id }),
  tasks: (id: string) => get<{ tasks: Task[] }>(`/api/collab/groups/${id}/tasks`),
  myTasks: (include_closed = false) => get<{ tasks: Task[] }>(`/api/collab/tasks/mine${q({ include_closed })}`),
  createTask: (id: string, p: { title: string; description?: string; assignee_id?: string | null; priority?: TaskPriority; due_at?: number | null; refs?: Ref[] }) =>
    post<{ task: Task }>(`/api/collab/groups/${id}/tasks`, p),
  updateTask: (tid: string, p: Partial<{ title: string; description: string; assignee_id: string | null; priority: TaskPriority; status: TaskStatus; due_at: number | null; refs: Ref[] }>) =>
    put<{ task: Task }>(`/api/collab/tasks/${tid}`, p),
  deleteTask: (tid: string) => del<{ ok: boolean }>(`/api/collab/tasks/${tid}`),
  usage: (id: string) => get<GroupUsage>(`/api/collab/groups/${id}/usage`),
  bot: (id: string) => get<{ bot: GroupBotConfig | null; models: BotModelOption[]; can_manage: boolean; defaults: { personality: string; qmoji_enabled: boolean; member_id: string; name: string } }>(`/api/collab/groups/${id}/bot`),
  saveBot: (id: string, p: { enabled: boolean; model_target: string; system_prompt: string; personality: string; qmoji_enabled: boolean; reply_default_mention: boolean }) =>
    put<{ bot: GroupBotConfig; models: BotModelOption[]; group: Group }>(`/api/collab/groups/${id}/bot`, p),
  removeBot: (id: string) => del<{ ok: boolean; group: Group }>(`/api/collab/groups/${id}/bot`),
  shared: (id: string) => get<{ strategies: SharedItem[]; projects: SharedItem[] }>(`/api/collab/groups/${id}/shared`),
  share: (type: 'strategy' | 'project', id: string) => get<ShareInfo>(`/api/collab/shares/${type}/${encodeURIComponent(id)}`),
  setShare: (type: 'strategy' | 'project', id: string, visibility: Visibility, group_ids: string[] = []) =>
    put<{ share: ShareInfo['share'] }>(`/api/collab/shares/${type}/${encodeURIComponent(id)}`, { visibility, group_ids }),
  edits: (type: string, id: string) => get<{ edits: EditRow[] }>(`/api/collab/edits/${type}/${encodeURIComponent(id)}`),
  editors: (type: string) =>
    get<{ editors: Record<string, EditorMark[]>; visibility: Record<string, { visibility: Visibility; owner_id: string; owner_name: string; groups: string[] }> }>(`/api/collab/editors/${type}`),
  lineage: (slug?: string) => get<{ nodes: LineageNode[]; edges: LineageEdge[] }>(`/api/collab/lineage${q({ slug })}`),
  searchRefs: (type: 'backtest' | 'strategy' | 'post' | 'file' | 'task', query = '') =>
    get<{ items: (Ref & { hint?: string; at?: number })[] }>(`/api/collab/refs/search${q({ type, q: query })}`),
}
/** 新成员默认限额：没有单独设过限额的成员按它计算（工作区管理员）。 */
export const memberDefaults = {
  get: () => get<{ limits: LimitRule[] }>('/api/config/member-defaults'),
  save: (limits: LimitRule[]) => put<{ limits: LimitRule[] }>('/api/config/member-defaults', { limits }),
}

/** 群组模型服务商：走「设置 · 模型」同一套接口，scope=group。 */
export const groupProviders = {
  save: (groupId: string, p: { name: string; label: string; type: string; base_url: string; api_key: string; models: string[] }) =>
    post<{ ok: boolean }>('/api/config/providers', { ...p, scope: 'group', group_id: groupId }),
  remove: (groupId: string, name: string) => del<{ ok: boolean }>(`/api/config/providers/${encodeURIComponent(name)}?scope=group&group_id=${groupId}`),
}
export interface SharedItem { id: string; title: string; owner_id: string; owner_name: string; updated_at: number }
export interface GroupUsage {
  providers: { name: string; label: string; models: string[]; has_key: boolean; key_masked: string; base_url: string; limits: LimitRule[]; limit_status: LimitStatus[] }[]
  ws_limits: LimitRule[]
  ws_limit_status: LimitStatus[]
  member_limits: LimitRule[]
  members: { user_id: string; name: string; group_tokens: number; group_cost: string; ws_tokens: number; ws_cost: string; member_limit_status: LimitStatus[] }[]
  can_manage: boolean
  is_ws_admin: boolean
}

export const inbox = {
  list: (o: { unread?: boolean; kind?: string; before?: number } = {}) =>
    get<{ items: Notice[]; kinds: Record<string, string>; unread: Record<string, number> }>(`/api/inbox${q(o)}`),
  summary: () => get<InboxSummary>('/api/inbox/summary'),
  read: (p: { ids?: number[]; kind?: string } = {}) => post<{ updated: number }>('/api/inbox/read', p),
  clear: (p: { ids?: number[]; read_only?: boolean }) => post<{ deleted: number }>('/api/inbox/clear', p),
  mutes: () => get<{ mutes: Mute[]; kinds: Record<string, string> }>('/api/inbox/mutes'),
  mute: (target_type: Mute['target_type'], target_id: string, label = '') => post<{ ok: boolean }>('/api/inbox/mutes', { target_type, target_id, label }),
  unmute: (target_type: string, target_id: string) => del<{ ok: boolean }>(`/api/inbox/mutes/${target_type}/${encodeURIComponent(target_id)}`),
}

export const issues = {
  list: (scope: 'mine' | 'all' = 'mine', status?: string) =>
    get<{ items: Issue[]; categories: Record<string, string>; statuses: Record<string, string>; is_staff: boolean; counts: Record<string, number> | null }>(`/api/issues${q({ scope, status })}`),
  get: (id: string) => get<{ issue: Issue; is_staff: boolean; statuses: Record<string, string> }>(`/api/issues/${id}`),
  create: (p: { title: string; body: string; category: string; severity: string; attachments: string[]; context: Record<string, string> }) =>
    post<{ issue: Issue }>('/api/issues', p),
  reply: (id: string, body: string, attachments: string[] = []) => post<{ issue: Issue }>(`/api/issues/${id}/replies`, { body, attachments }),
  status: (id: string, status: Issue['status']) => put<{ issue: Issue }>(`/api/issues/${id}/status`, { status }),
}

/** 上传一个文件（原始字节）。返回文件 id 与访问地址。 */
export async function uploadFile(file: File): Promise<Upload> {
  const headers: Record<string, string> = { 'Content-Type': file.type || 'application/octet-stream', 'X-Requested-With': 'quantlab' }
  const ws = targetWorkspace()
  if (ws) headers['X-Workspace-Id'] = ws
  const res = await fetch(`/api/uploads?filename=${encodeURIComponent(file.name)}`, { method: 'POST', credentials: 'same-origin', headers, body: file })
  if (!res.ok) {
    let msg = `${res.status}`
    try { const b = await res.json(); msg = b.message || msg } catch { /* 非 JSON */ }
    throw new ApiError(res.status, 'UPLOAD_FAILED', msg)
  }
  return (await res.json()).file as Upload
}
export const uploadUrl = (id: string) => `/api/uploads/${id}`
export const uploadThumbUrl = (id: string) => `/api/uploads/${id}/thumb`
export const fmtSize = (n: number) => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`)

/* ------------------------------------------------------------------ 运维控制台 */
export interface WorkerInfo {
  id: string; host: string; pid?: number | null; started_at?: number | null; last_seen: number; alive: boolean
  slots: number; busy: number; resident: number
  rss_bytes: number; private_bytes: number; peak_rss_bytes?: number; cpu_seconds: number; threads: number
  kinds?: string[]
}
export interface WorkerConfig {
  slots: number; min: number; max: number; default: number; configured: boolean
  updated_at?: number | null; updated_by?: string
}
export interface AdminOverview {
  server: {
    python: string; platform: string; host: string; pid: number; uptime_s: number; data_dir: string; db: string
    db_bytes: number | null; disk: { total: number; used: number; free: number }; folders: Record<string, number>
    worker_mode: string; open_registration: boolean
    memory: { total: number; available: number; used: number; percent: number }
    process: { rss_bytes: number; private_bytes: number; peak_rss_bytes?: number; cpu_seconds: number; threads: number }
  }
  users: { total: number; active: number; banned: number; admins: number; active_7d: number; sessions: number }
  workspaces: { total: number; active: number }
  groups: number
  runs: Record<string, number>
  workers: { alive: number; list: WorkerInfo[]; stale_runs?: number; error?: string; queued?: number; slots_total?: number; busy_total?: number }
  worker_config: WorkerConfig
  llm: Record<'24h' | '7d' | '30d', { calls: number; tokens: number; cost: string }>
  issues: Record<string, number>
  uploads_bytes: number
}

export interface AdminUser {
  id: string; email: string; name: string; is_admin: boolean; is_active: boolean; created_at: number
  last_login_at: number | null; banned_at: number | null; banned_reason: string; workspaces?: number; totp?: boolean
  is_primary_admin?: boolean
  usage_30d?: { calls: number; tokens: number; cost: string }
}
export interface AdminWorkspace {
  id: string; name: string; status: string; is_demo: boolean; created_at: number; owner_id: string; owner_email: string | null
  owner_name: string | null; members: number; groups: number; runs_30d: number; usage_30d: { calls: number; tokens: number; cost: string }
}
export interface UsageRow { calls: number; tokens: number; cost: string; name?: string; user_id?: string; workspace_id?: string; provider?: string; scope?: string }

export const admin = {
  overview: () => get<AdminOverview>('/api/admin/overview'),
  users: (o: { q?: string; status?: string } = {}) => get<{ users: AdminUser[] }>(`/api/admin/users${q(o)}`),
  user: (id: string) => get<{
    user: AdminUser
    workspaces: { id: string; name: string; status: string; role: string; member_status: string; created_at: number }[]
    sessions: { id: string; created_at: number; expires_at: number; ip: string; user_agent: string }[]
    usage_30d: { workspace_id: string; scope: string; calls: number; tokens: number; cost: string }[]
    audit: { ts: number; workspace_id: string; action: string; object_type: string; object_id: string; ip: string }[]
    groups: { id: string; name: string; workspace_id: string; role: string }[]
  }>(`/api/admin/users/${id}`),
  ban: (id: string, reason: string) => post<{ user: AdminUser }>(`/api/admin/users/${id}/ban`, { reason }),
  unban: (id: string) => post<{ user: AdminUser }>(`/api/admin/users/${id}/unban`),
  setAdmin: (id: string, is_admin: boolean) => post<{ user: AdminUser }>(`/api/admin/users/${id}/admin`, { is_admin }),
  revoke: (id: string) => post<{ revoked: number }>(`/api/admin/users/${id}/sessions/revoke`),
  resetPassword: (id: string) => post<{ temp_password: string; note: string }>(`/api/admin/users/${id}/reset-password`),
  createUser: (p: { email: string; name?: string; workspace_id?: string | null; role?: string }) =>
    post<{ user: AdminUser; temp_password: string }>('/api/admin/users', p),
  workspaces: () => get<{ workspaces: AdminWorkspace[] }>('/api/admin/workspaces'),
  setWorkspaceStatus: (id: string, status: 'active' | 'suspended') => put<{ ok: boolean }>(`/api/admin/workspaces/${id}/status`, { status }),
  usage: (days = 30) => get<{ days: number; daily: { day: string; tokens: number; calls: number; cost: string }[]; by_user: UsageRow[]; by_workspace: UsageRow[]; by_provider: UsageRow[]; by_scope: UsageRow[] }>(`/api/admin/usage?days=${days}`),
  announce: (p: { title: string; body: string; workspace_id?: string | null; banner: boolean; banner_hours?: number; tone?: 'info' | 'warn' }) =>
    post<{ recipients: number }>('/api/admin/announce', p),
  settings: () => get<{ open_registration: boolean; open_registration_override: boolean | null; env_open_registration: boolean; banner: InboxSummary['banner'] }>('/api/admin/settings'),
  saveSettings: (p: { open_registration?: boolean | null; banner_text?: string }) =>
    put<{ open_registration: boolean; open_registration_override: boolean | null; env_open_registration: boolean; banner: InboxSummary['banner'] }>('/api/admin/settings', p),
  workerConfig: () => get<{ config: WorkerConfig }>('/api/admin/worker-config'),
  saveWorkerConfig: (slots: number) => put<{ config: WorkerConfig }>('/api/admin/worker-config', { slots }),
  audit: (o: { action?: string; user_id?: string; workspace_id?: string; before_id?: number } = {}) =>
    get<{ items: { id: number; ts: number; workspace_id: string; user_id: string; user_name: string; action: string; object_type: string; object_id: string; ip: string; details: Record<string, unknown> }[] }>(`/api/admin/audit${q(o)}`),
}

/** 毫秒或秒时间戳 → 「3 分钟前」。 */
export function since(ts: number | null | undefined): string {
  if (!ts) return '—'
  const ms = ts < 1e12 ? ts * 1000 : ts
  const s = Math.max(0, (Date.now() - ms) / 1000)
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`
  return new Date(ms).toLocaleDateString()
}
export const fmtBytes = (n: number | null | undefined) => {
  if (n === null || n === undefined) return '—'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`
}
