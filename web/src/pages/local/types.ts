/** 设置 · 本地项目各面板共用的接口类型（与 src/quant_agent/api/routes_local.py 对齐）。 */

export type Permission = { path: string; mode: string }

export type Info = {
  mode: string
  root: string
  home?: string
  project: { name: string; id: string }
  permissions: Permission[]
  preferences: { close_action: string; last_workflow_id?: string }
  upload_byte_limit?: number | null
}

export type EnvCheck = { requirement: string; ok: boolean; version?: string; message?: string }

export type Env = {
  python: string
  version: string
  server_python: string
  ready: boolean
  checks: EnvCheck[]
  note: string
  selected?: string
}

export type EnvCandidate = { python: string; version?: string; ok: boolean; message?: string }

export type Entry = { path: string; name: string; directory: boolean; bytes: number | null }

export type Job = {
  id: string
  kind?: string
  state: string
  error?: string
  log_tail?: string
  result?: Record<string, unknown>
}

export type RunRow = { id: string; kind: string; state: string; title: string }
export type ConversationRow = { id: string; title: string; status: string }
export type Tasks = { runs: RunRow[]; conversations: ConversationRow[] }

export type CacheInfo = { entries: number; bytes: number; effect: string; active_runs?: number }

export type LibraryModule = { id: string; kind: string; version: string; title: string }
export type CatalogCard = { kind: string; version: string; title: string }

export type Resource = {
  cpu_percent?: number | null
  disk_free?: number
  active_runs?: number
  memory?: { used: number; total: number } | null
  process?: { pid: number; rss: number; children: number }
  gpu?: string[][]
  project?: string
  resource_note?: string
}

/** 数据预览：CSV/Excel/Parquet/JSONL 返回 fields+data，其他结果按 JSON 展示。 */
export type PreviewResult = { title: string; value: unknown }

export type PreviewTable = {
  fields: string[]
  data: Record<string, unknown>[]
  offset?: number
  limit?: number
  total?: number | null
  has_more?: boolean
  scope?: string
  note?: string
}
