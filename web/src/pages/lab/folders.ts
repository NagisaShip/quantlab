/**
 * 项目文件夹（对话的项目）的本地持久化，侧栏对话列表与新对话页共用：
 * - `qa.lab.folders`：用户建过的项目文件夹名（沿用旧 key，老数据照常可读）；
 * - `qa.lab.lastFolder`：最近一次用过的项目文件夹，用来给下一次新对话做默认值。
 *   存成 JSON：`{ folder, at }`，folder 为 `''` 表示用户明确选了「未分类」，
 *   与「还没记录过」区分开（后者会继续往更早的对话 / 本地项目名兜底）。
 */
import { store } from '../../components/prefs'

export const FOLDERS_KEY = 'qa.lab.folders'
export const LAST_FOLDER_KEY = 'qa.lab.lastFolder'
export const FOLDER_NAME_MAX = 40

/** 名称规整：去首尾空白、截断到 40 字（与侧栏新建/重命名一致）。 */
export function cleanFolder(name: unknown): string {
  return typeof name === 'string' ? name.trim().slice(0, FOLDER_NAME_MAX) : ''
}

const dedupe = (list: unknown[]): string[] => [...new Set(list.map(cleanFolder).filter(Boolean))]

/** 已建的项目文件夹列表（读失败时当作空列表，不影响页面）。 */
export function readFolders(): string[] {
  try {
    const raw = store.get(FOLDERS_KEY, '[]')
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? dedupe(parsed) : []
  } catch {
    return []
  }
}

/** 写回项目文件夹列表，返回去重后的结果。 */
export function writeFolders(list: readonly string[]): string[] {
  const next = dedupe([...list])
  store.set(FOLDERS_KEY, JSON.stringify(next))
  return next
}

/** 新建一个项目文件夹（重名不重复加），返回最新列表。 */
export function addFolder(name: string): string[] {
  const n = cleanFolder(name)
  const list = readFolders()
  return n ? writeFolders([...list, n]) : list
}

/** 删掉一个项目文件夹（解散：对话由调用方移出，这里只管列表）。 */
export function removeFolder(name: string): string[] {
  const n = cleanFolder(name)
  return writeFolders(readFolders().filter((f) => f !== n))
}

/** 项目文件夹候选：本地列表 ∪ 对话里出现过的 folder，按名称排序。 */
export function folderOptions(extra: Iterable<string | null | undefined> = []): string[] {
  const set = new Set(readFolders())
  for (const f of extra) {
    const n = cleanFolder(f)
    if (n) set.add(n)
  }
  return [...set].sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'))
}

/**
 * 最近一次用过的项目文件夹：
 * - 返回 `''` 表示最近明确选了「未分类」；
 * - 返回 `null` 表示还没有记录，应该继续往「最近对话的 folder」兜底。
 */
export function readLastFolder(): string | null {
  try {
    const raw = store.get(LAST_FOLDER_KEY, '')
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed === 'string') return cleanFolder(parsed)
    const folder = (parsed as { folder?: unknown } | null)?.folder
    return typeof folder === 'string' ? cleanFolder(folder) : null
  } catch {
    return null
  }
}

/** 记住这次用过的项目文件夹（`''` = 未分类），下次新对话默认用它。 */
export function rememberFolder(name: string): void {
  store.set(LAST_FOLDER_KEY, JSON.stringify({ folder: cleanFolder(name), at: Date.now() }))
}

/** 对话列表里最近一次有项目文件夹的对话所用的 folder（按 updated_at 取最新）。 */
export function latestFolderOf(projects: readonly { folder?: string; updated_at: number }[]): string {
  const withFolder = projects.filter((p) => cleanFolder(p.folder))
  if (!withFolder.length) return ''
  const latest = withFolder.reduce((a, b) => (b.updated_at > a.updated_at ? b : a))
  return cleanFolder(latest.folder)
}

/** 本地模式：当前项目就是工作区，它的名字（项目目录名）默认给新对话当项目文件夹。 */
export function localProjectFolder(me?: {
  mode?: string
  workspace?: { name?: string } | null
  local?: { project?: { name?: string } } | null
}): string {
  if (me?.mode !== 'local') return ''
  return cleanFolder(me.workspace?.name) || cleanFolder(me.local?.project?.name)
}

/**
 * 新对话页的默认项目文件夹，优先顺序：
 * 侧栏显式传入 → 最近用过（可能是明确的「未分类」）→ 最近对话的 folder → 本地模式的项目名 → 未分类。
 */
export function resolveNewChatFolder(o: {
  explicit?: string | null
  lastFolder?: string | null
  recentProjectFolder?: string
  localFolder?: string
}): string {
  const explicit = cleanFolder(o.explicit)
  if (explicit) return explicit
  if (o.lastFolder !== null && o.lastFolder !== undefined) return cleanFolder(o.lastFolder)
  const recent = cleanFolder(o.recentProjectFolder)
  if (recent) return recent
  return cleanFolder(o.localFolder)
}
