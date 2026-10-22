/** P3 固定成果 / 版本历史 API 与类型。与后端 workflows/artifacts.py 对齐。 */
import { del, get, post } from '../../http'

export type ArtifactPin = {
  id: string
  hash: string
  run_id: string
  workflow_id: string
  node_id: string
  port: string
  title: string
  type: string
  note: string
  version_key: string
  pinned_at: number
  pinned_by: string
}

export type ArtifactVersion = {
  hash: string
  run_id: string
  revision: number
  created_at?: number
  status?: string
  pinned?: boolean
  pinned_at?: number | null
  available?: boolean
  type?: string
  role?: string
  bytes?: number
  origin?: string
}

export type ArtifactPathInfo = {
  hash: string
  path: string
  directory: string
  name: string
  bytes: number
  project_root: string
  mode: string
}

export const versionKey = (row: { workflow_id: string; node_id: string; port: string }) =>
  `${row.workflow_id}:${row.node_id}:${row.port}`

export async function listPins(versionKeyOrEmpty = '') {
  const query = versionKeyOrEmpty ? `?version_key=${encodeURIComponent(versionKeyOrEmpty)}&limit=200` : '?limit=200'
  return get<{ pins: ArtifactPin[]; total: number }>(`/api/workflows/artifacts/pins${query}`)
}

export async function evalPinState(row: {
  hash: string
  run_id: string
  workflow_id: string
  node_id: string
  port: string
}) {
  const { pins } = await listPins(versionKey(row))
  return pins.find((pin) => pin.hash === row.hash) ?? null
}

export async function pinArtifact(row: {
  hash: string
  run_id: string
  workflow_id: string
  node_id: string
  port: string
  title: string
  type: string
}, note = '') {
  return post<ArtifactPin>('/api/workflows/artifacts/pins', {
    hash: row.hash,
    run_id: row.run_id,
    workflow_id: row.workflow_id,
    node_id: row.node_id,
    port: row.port,
    title: row.title,
    type: row.type,
    note,
  })
}

export async function unpinArtifact(pinId: string) {
  return del<{ deleted: boolean; id: string }>(`/api/workflows/artifacts/pins/${encodeURIComponent(pinId)}`)
}

export async function listVersions(hash: string) {
  return get<{
    version_key: string | null
    workflow_id?: string
    node_id?: string
    port?: string
    versions: ArtifactVersion[]
    total: number
    reason?: string
  }>(`/api/workflows/artifacts/versions?hash=${encodeURIComponent(hash)}&limit=50`)
}

/** 本地模式可解析真实路径；账号模式返回 404，前端据此隐藏本机动作。 */
export async function artifactPath(hash: string) {
  try {
    return await get<ArtifactPathInfo>(`/api/local/artifacts/${encodeURIComponent(hash)}/path`)
  } catch {
    return null
  }
}
