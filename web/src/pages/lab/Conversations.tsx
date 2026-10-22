/**
 * AI 研究室的对话列表（渲染在全局左侧栏里）：
 * - 项目文件夹分组，可新建、重命名、解散；置顶的对话单独排在最上面；
 * - 分叉出来的对话以树状嵌套在原对话下面，可折叠；
 * - 每行最右侧「更多」，点击或在行上右键：重命名、置顶、分叉、移动到项目文件夹、删除。
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { lab, sessionKindOf, type LabProjectBrief, type SessionKind } from '../../apiLab'
import { collab, VIS_LABEL, type Visibility } from '../../apiCollab'
import { VisibilityChip } from '../../components/Share'
import { isLocalDeployment } from '../../deployment'
import { briefStatus, readObservedFailures } from '../../components/chat/status'
import { store } from '../../components/prefs'
import { appChoose, appConfirm, appPrompt } from '../../components/dialog'
import { labChanged, onLabChanged, requestNewChat } from './bus'
import { addFolder, folderOptions, removeFolder, readFolders, writeFolders } from './folders'

type Notify = (msg: string, error?: boolean) => void

const COLLAPSED_KEY = 'qa.lab.collapsed'
const GROUP_KEY = 'qa.lab.groupBy'

/** 按研究类型分组：完整流程与调研报告按流程归类，其余按研究室类型 */
const TYPES: { id: string; label: string }[] = [
  { id: 'pipeline', label: '完整流程' },
  { id: 'strategy', label: '策略' },
  { id: 'factor', label: '因子' },
  { id: 'model', label: '预测模型' },
  { id: 'portfolio', label: '组合' },
  { id: 'execution', label: '执行' },
  { id: 'research', label: '研究' },
  { id: 'workflow', label: '工作流' },
  { id: 'ask', label: '问答' },
  { id: 'report', label: '调研报告' },
]
const typeOf = (p: LabProjectBrief) => (p.workflow === 'pipeline' || p.workflow === 'report' ? p.workflow : p.kind || 'strategy')

/** 会话类型徽标：策略 / 因子 / 模型 / 组合 / 执行 / 研究 / 工作流 / 问答，与研究室命名一致。 */
const KIND_LABEL: Record<SessionKind, string> = {
  strategy: '策略', factor: '因子', model: '模型', portfolio: '组合', execution: '执行',
  research: '研究', workflow: '工作流', ask: '问答',
}
const kindLabel = (p: LabProjectBrief) => KIND_LABEL[sessionKindOf(p)]
/** 分享状态：没有分享记录时按「私有」处理（新建会话默认私有）。 */
type ShareMap = Record<string, Visibility>
const visOf = (shares: ShareMap, id: string): Visibility => shares[id] ?? 'private'

const readList = (k: string): string[] => { try { return JSON.parse(store.get(k, '[]')) as string[] } catch { return [] } }
const writeList = (k: string, v: string[]) => store.set(k, JSON.stringify([...new Set(v)]))

function ago(ts: number) {
  const ms = ts < 1e12 ? ts * 1000 : ts
  const d = (Date.now() - ms) / 1000
  if (d < 60) return '刚刚'
  if (d < 3600) return `${Math.floor(d / 60)} 分钟`
  if (d < 86400) return `${Math.floor(d / 3600)} 小时`
  if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天`
  return new Date(ms).toISOString().slice(5, 10)
}

interface Menu { id: string; x: number; y: number }

export function Conversations({ projects, activeId, onOpen, onNew, onChanged, notify, shares = {} }: {
  projects: LabProjectBrief[]
  activeId: string
  onOpen: (id: string) => void
  onNew: (folder?: string) => void
  onChanged: () => void
  notify: Notify
  /** 会话 id → 可见性（来自现有分享记录；缺省为私有）。 */
  shares?: ShareMap
}) {
  const [q, setQ] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const searchRef = useRef<HTMLInputElement>(null)
  const [menu, setMenu] = useState<Menu | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [folders, setFolders] = useState<string[]>(readFolders)
  const [collapsed, setCollapsed] = useState<string[]>(() => readList(COLLAPSED_KEY))
  const [groupBy, setGroupByState] = useState<'project' | 'type'>(() => (store.get(GROUP_KEY, 'project') === 'type' ? 'type' : 'project'))
  const setGroupBy = (g: 'project' | 'type') => { setGroupByState(g); store.set(GROUP_KEY, g) }
  const menuRef = useRef<HTMLDivElement>(null)
  // P4：进行中的任务在列表顶部全局可见；失败项提供「继续」（复用现有发送接口）
  const observed = useMemo(() => readObservedFailures(), [projects])
  const activeTasks = useMemo(() => {
    const rank = { generating: 0, queued: 1, failed: 2 } as Record<string, number>
    return projects
      .map((p) => ({ p, status: briefStatus(p, observed[p.id]) }))
      .filter((x) => x.status.state === 'generating' || x.status.state === 'queued' || x.status.state === 'failed')
      .sort((a, b) => (rank[a.status.state] - rank[b.status.state]) || (b.p.updated_at - a.p.updated_at))
  }, [projects, observed])
  const resumeTask = async (id: string) => {
    try {
      await lab.send(id, '继续')
      onChanged()
      notify('已继续这个对话')
      onOpen(id)
    } catch (e) { notify((e as Error).message, true) }
  }

  // 新对话页也可能新建项目文件夹：研究室一改动就把列表同步过来
  useEffect(() => onLabChanged(() => setFolders(readFolders())), [])

  useEffect(() => {
    if (!menu) return
    const close = (e: Event) => { if (!menuRef.current?.contains(e.target as Node)) setMenu(null) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(null) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    window.addEventListener('blur', () => setMenu(null), { once: true })
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [menu])

  const allFolders = useMemo(() => folderOptions(projects.map((p) => p.folder)), [folders, projects])
  const toggle = (key: string) => {
    const next = collapsed.includes(key) ? collapsed.filter((k) => k !== key) : [...collapsed, key]
    setCollapsed(next)
    writeList(COLLAPSED_KEY, next)
  }

  const needle = q.trim().toLowerCase()
  const match = (p: LabProjectBrief) => !needle || `${p.title} ${p.idea}`.toLowerCase().includes(needle)
  const byId = new Map(projects.map((p) => [p.id, p]))
  // 有父对话且父对话在列表里的，作为子节点；其余是根
  const children = new Map<string, LabProjectBrief[]>()
  for (const p of projects) {
    if (p.parent_id && byId.has(p.parent_id)) children.set(p.parent_id, [...(children.get(p.parent_id) ?? []), p])
  }
  const subtreeMatches = (p: LabProjectBrief): boolean => match(p) || (children.get(p.id) ?? []).some(subtreeMatches)
  const roots = projects.filter((p) => !(p.parent_id && byId.has(p.parent_id)) && subtreeMatches(p))
  const sortRows = (rows: LabProjectBrief[]) => [...rows].sort((a, b) => b.updated_at - a.updated_at)
  const pinned = sortRows(roots.filter((p) => p.pinned))
  const loose = sortRows(roots.filter((p) => !p.pinned && !p.folder))

  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    setMenu(null)
    try { await fn(); if (ok) notify(ok); onChanged() } catch (e) { notify((e as Error).message, true) }
  }
  const openMenu = (e: ReactMouseEvent, id: string) => {
    e.preventDefault()
    e.stopPropagation()
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
    const right = e.type === 'contextmenu'
    setMenu({ id, x: right ? e.clientX : r.right - 4, y: right ? e.clientY : r.bottom + 4 })
  }
  const newFolder = async () => {
    const name = (await appPrompt('新建项目文件夹（用来归类对话）', '', { placeholder: '例如：BTC 趋势研究' }))?.trim().slice(0, 40)
    if (!name) return
    setFolders(addFolder(name))
    return name
  }
  const renameFolder = async (old: string) => {
    const name = (await appPrompt('项目文件夹名称', old))?.trim().slice(0, 40)
    if (!name || name === old) return
    await Promise.all(projects.filter((p) => p.folder === old).map((p) => lab.update(p.id, { folder: name })))
    setFolders(writeFolders([...folders.filter((f) => f !== old), name])); onChanged()
  }
  const dissolveFolder = async (name: string) => {
    if (!await appConfirm(`解散项目文件夹「${name}」？里面的对话会移到「未分类」下，不会删除。`)) return
    await Promise.all(projects.filter((p) => p.folder === name).map((p) => lab.update(p.id, { folder: '' })))
    setFolders(removeFolder(name)); onChanged()
  }

  const row = (p: LabProjectBrief, depth = 0): JSX.Element => {
    const kids = sortRows(children.get(p.id) ?? []).filter(subtreeMatches)
    const open = !collapsed.includes(p.id) || !!needle
    const kind = sessionKindOf(p)
    const st = briefStatus(p, observed[p.id])
    const showBadge = st.state === 'generating' || st.state === 'queued' || st.state === 'failed'
    return (
      <Fragment key={p.id}>
        <div className={`conv-row${p.id === activeId ? ' on' : ''}${menu?.id === p.id ? ' menu' : ''}`} style={{ paddingLeft: 6 + depth * 14 }}
          onContextMenu={(e) => openMenu(e, p.id)} title={p.idea}>
          {kids.length ? (
            <button type="button" className={`conv-caret${open ? ' open' : ''}`} aria-label={open ? '收起分叉' : '展开分叉'} onClick={() => toggle(p.id)}>
              <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="m3.5 2 3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </button>
          ) : depth > 0 ? <span className="conv-branch" aria-hidden>↳</span> : <span className="conv-caret-sp" />}
          {renaming === p.id ? (
            <input className="conv-rename" autoFocus defaultValue={p.title} maxLength={60}
              onBlur={(e) => { const v = e.target.value.trim(); setRenaming(null); if (v && v !== p.title) act(() => lab.update(p.id, { title: v })) }}
              onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') setRenaming(null) }} />
          ) : (
            <button type="button" className="conv-main" onClick={() => {
              const selected = window.getSelection()?.toString().trim()
              if (!selected) onOpen(p.id)
            }}>
              {showBadge
                ? <span className={`conv-state ${st.state}`} title={`${st.label}${st.detail ? ` · ${st.detail}` : ''}`}>{st.label}</span>
                : p.status !== 'idle' && <i className={`dot ${p.status}`} />}
              {p.pinned && depth === 0 && <span className="conv-pin" aria-label="已置顶"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true"><path d="m16 3 5 5-4 1-4 4-1 4-5-5 4-1 4-4zM7 17l-4 4" /></svg></span>}
              <span className="conv-title">{p.title}</span>
              <span className={`conv-kind is-${kind}`} title={`${kindLabel(p)}会话`}>{kindLabel(p)}</span>
            </button>
          )}
          {/* 私有 / 共享状态：复用现有可见性记录，缺省私有；悬停看完整说明 */}
          <span className="conv-share" title={VIS_LABEL[visOf(shares, p.id)]}>
            <VisibilityChip v={visOf(shares, p.id)} />
          </span>
          <span className="conv-time">{ago(p.updated_at)}</span>
          <button type="button" className="conv-more" aria-label="更多" aria-haspopup="menu" aria-expanded={menu?.id === p.id} onClick={(e) => openMenu(e, p.id)}>
            <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden><circle cx="3.5" cy="8" r="1.2" fill="currentColor" /><circle cx="8" cy="8" r="1.2" fill="currentColor" /><circle cx="12.5" cy="8" r="1.2" fill="currentColor" /></svg>
          </button>
        </div>
        {open && kids.map((k) => row(k, depth + 1))}
      </Fragment>
    )
  }

  const target = menu ? byId.get(menu.id) : undefined
  const [moveOpen, setMoveOpen] = useState(false)
  useEffect(() => setMoveOpen(false), [menu?.id])
  useEffect(() => {
    if (!searchOpen) return
    const t = window.setTimeout(() => searchRef.current?.focus(), 20)
    return () => window.clearTimeout(t)
  }, [searchOpen])
  useEffect(() => { if (q) setSearchOpen(true) }, [q])

  return (
    <div className="conv">
      <div className="conv-head">
        <span className="conv-label">对话</span>
        <button type="button" className="conv-icon" title="新建项目文件夹" aria-label="新建项目文件夹" onClick={() => newFolder()}>
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" aria-hidden><path d="M2 4.5h4l1.2 1.3H14v6.7H2Z" /><path d="M8 7.8v3M6.5 9.3h3" strokeLinecap="round" /></svg>
        </button>
        <button type="button" className="conv-icon" title="新对话" aria-label="新对话" onClick={() => onNew()}>
          <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
        </button>
        <button type="button" className={`conv-icon${searchOpen ? ' on' : ''}`} title="搜索对话" aria-label="搜索对话" aria-expanded={searchOpen}
          onClick={() => { if (searchOpen) { setQ(''); setSearchOpen(false) } else setSearchOpen(true) }}>
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.35" strokeLinecap="round" aria-hidden><circle cx="7" cy="7" r="4.2" /><path d="m10.2 10.2 3 3" /></svg>
        </button>
      </div>
      {activeTasks.length > 0 && (
        <div className="lab-taskbar" aria-label="进行中的研究任务">
          <div className="lab-taskbar-head"><span>进行中</span><em>{activeTasks.length}</em></div>
          {activeTasks.slice(0, 3).map(({ p, status }) => (
            <div key={p.id} className={`lab-taskbar-row ${status.state}`}>
              <button type="button" className="lab-taskbar-main" onClick={() => onOpen(p.id)}
                title={`${p.title} · ${status.label}${status.detail ? ` · ${status.detail}` : ''}`}>
                <i className={`lab-taskbar-dot ${status.state}`} aria-hidden />
                <span className="lab-taskbar-title">{p.title}</span>
                <span className="lab-taskbar-state">{status.label}</span>
              </button>
              {status.state === 'failed' && (
                <button type="button" className="lab-taskbar-act" title="发送「继续」接着跑" onClick={() => resumeTask(p.id)}>继续</button>
              )}
            </div>
          ))}
          {activeTasks.length > 3 && <div className="lab-taskbar-more">还有 {activeTasks.length - 3} 个任务…</div>}
        </div>
      )}
      <div className={`conv-tools${searchOpen ? ' searching' : ''}`}>
        {searchOpen && (
          <input ref={searchRef} className="conv-search" placeholder="搜索对话" value={q} onChange={(e) => setQ(e.target.value)}
            aria-label="搜索对话" onKeyDown={(e) => { if (e.key === 'Escape') { setQ(''); setSearchOpen(false) } }} />
        )}
        <div className="conv-groupby" role="group" aria-label="分组方式">
          <button type="button" aria-pressed={groupBy === 'project'} onClick={() => setGroupBy('project')} title="按项目文件夹分组">项目文件夹</button>
          <button type="button" aria-pressed={groupBy === 'type'} onClick={() => setGroupBy('type')} title="按研究类型自动分组">类型</button>
        </div>
      </div>
      <div className="conv-list">
        {pinned.length > 0 && (
          <div className="conv-group">
            <div className="conv-group-label">置顶</div>
            {pinned.map((p) => row(p))}
          </div>
        )}
        {groupBy === 'type' && TYPES.map((tp) => {
          const rows = sortRows(roots.filter((p) => !p.pinned && typeOf(p) === tp.id))
          if (!rows.length) return null
          const key = `type:${tp.id}`
          const open = !collapsed.includes(key) || !!needle
          return (
            <div key={key} className="conv-group">
              <div className="conv-folder">
                <button type="button" className={`conv-caret${open ? ' open' : ''}`} onClick={() => toggle(key)} aria-label={open ? '收起' : '展开'}>
                  <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="m3.5 2 3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
                </button>
                <button type="button" className="conv-folder-name" onClick={() => toggle(key)}>
                  <span className={`conv-type-dot ${tp.id}`} aria-hidden /><span>{tp.label}</span><em>{rows.length}</em>
                </button>
              </div>
              {open && rows.map((p) => row(p, 1))}
            </div>
          )
        })}
        {groupBy === 'project' && allFolders.map((f) => {
          const rows = sortRows(roots.filter((p) => !p.pinned && p.folder === f))
          if (needle && !rows.length) return null
          const key = `folder:${f}`
          const open = !collapsed.includes(key) || !!needle
          return (
            <div key={f} className="conv-group">
              <div className="conv-folder" onContextMenu={(e) => { e.preventDefault(); renameFolder(f) }}>
                <button type="button" className={`conv-caret${open ? ' open' : ''}`} onClick={() => toggle(key)} aria-label={open ? '收起项目文件夹' : '展开项目文件夹'}>
                  <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="m3.5 2 3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
                </button>
                <button type="button" className="conv-folder-name" onClick={() => toggle(key)}>
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" aria-hidden><path d="M2 4.5h4l1.2 1.3H14v6.7H2Z" /></svg>
                  <span>{f}</span><em>{rows.length || ''}</em>
                </button>
                <button type="button" className="conv-more" title={`在项目文件夹「${f}」里新对话`} aria-label="在项目文件夹里新对话" onClick={() => onNew(f)}>
                  <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
                </button>
                <button type="button" className="conv-more" title="重命名 / 解散项目文件夹" aria-label="项目文件夹操作"
                  onClick={async () => { const v = await appPrompt(`项目文件夹「${f}」：输入新名称；清空后确定 = 解散项目文件夹`, f); if (v === null) return; if (!v.trim()) dissolveFolder(f); else renameFolder(f) }}>
                  <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden><circle cx="3.5" cy="8" r="1.2" fill="currentColor" /><circle cx="8" cy="8" r="1.2" fill="currentColor" /><circle cx="12.5" cy="8" r="1.2" fill="currentColor" /></svg>
                </button>
              </div>
              {open && (rows.length ? rows.map((p) => row(p, 1)) : <div className="conv-empty">空项目文件夹：点 ＋ 新建对话，或把对话移进来</div>)}
            </div>
          )
        })}
        <div className="conv-group">
          {groupBy === 'project' && (allFolders.length > 0 || pinned.length > 0) && <div className="conv-group-label">未分类</div>}
          {groupBy === 'project' && loose.map((p) => row(p))}
          {!projects.length && <div className="conv-empty">还没有对话</div>}
          {projects.length > 0 && needle && !pinned.length && !loose.length && !allFolders.some((f) => roots.some((p) => p.folder === f)) && <div className="conv-empty">没有匹配的对话</div>}
        </div>
      </div>

      {menu && target && (
        <div ref={menuRef} className="cmp-menu conv-menu" role="menu"
          style={{ position: 'fixed', left: Math.min(menu.x, window.innerWidth - 230), top: Math.min(menu.y, window.innerHeight - 280) }}>
          <button type="button" role="menuitem" className="act-item" onClick={() => { setMenu(null); setRenaming(target.id) }}>
            <span className="act-text"><span className="act-label">重命名</span></span>
          </button>
          <button type="button" role="menuitem" className="act-item" onClick={() => act(() => lab.update(target.id, { pinned: !target.pinned }), target.pinned ? '已取消置顶' : '已置顶')}>
            <span className="act-text"><span className="act-label">{target.pinned ? '取消置顶' : '置顶'}</span></span>
          </button>
          <button type="button" role="menuitem" className="act-item" onClick={() => act(async () => { const r = await lab.fork(target.id); onOpen(r.project.id) }, '已分叉出新对话')}>
            <span className="act-text"><span className="act-label">分叉</span><span className="act-hint">复制整段对话，作为子对话继续</span></span>
          </button>
          <button type="button" role="menuitem" className={`act-item${moveOpen ? ' open' : ''}`} aria-expanded={moveOpen} onClick={() => setMoveOpen(!moveOpen)}>
            <span className="act-text"><span className="act-label">移动到项目文件夹</span><span className="act-hint">{target.folder ? `现在在项目文件夹「${target.folder}」` : '现在不在任何项目文件夹里'}</span></span>
            <span className="act-chev"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="m4.5 2.5 3.5 3.5-3.5 3.5" /></svg></span>
          </button>
          {moveOpen && (
            <div className="act-sub anim-expand">
              {allFolders.filter((f) => f !== target.folder).map((f) => (
                <button key={f} type="button" role="menuitem" className="act-item" onClick={() => act(() => lab.update(target.id, { folder: f }), `已移到项目文件夹「${f}」`)}>
                  <span className="act-text"><span className="act-label">{f}</span></span>
                </button>
              ))}
              <button type="button" role="menuitem" className="act-item" onClick={async () => { const f = await newFolder(); if (f) act(() => lab.update(target.id, { folder: f }), `已移到项目文件夹「${f}」`) }}>
                <span className="act-text"><span className="act-label">＋ 新建项目文件夹…</span></span>
              </button>
              {target.folder && (
                <button type="button" role="menuitem" className="act-item" onClick={() => act(() => lab.update(target.id, { folder: '' }), '已移出项目文件夹（未分类）')}>
                  <span className="act-text"><span className="act-label">移出项目文件夹</span></span>
                </button>
              )}
            </div>
          )}
          <div className="cmp-menu-sep" />
          <button type="button" role="menuitem" className="act-item danger"
            onClick={async () => {
              setMenu(null)
              const forks = children.get(target.id)?.length ?? 0
              const base = `删除对话「${target.title}」？已保存到策略库 / 因子库的内容不受影响。`
              const choice = forks
                ? await appChoose(`${base}\n它有 ${forks} 个分叉。`, [
                  { value: 'one', label: '只删这个（分叉保留）', danger: true },
                  { value: 'all', label: `连同 ${forks} 个分叉一起删除`, danger: true },
                ])
                : (await appConfirm(base)) ? 'one' : null
              if (!choice) return
              act(async () => {
                const r = await lab.remove(target.id, choice === 'all')
                if (target.id === activeId || (r.deleted ?? []).includes(activeId ?? '')) onOpen('')
              }, choice === 'all' ? `已删除 ${forks + 1} 个对话` : '已删除')
            }}>
            <span className="act-text"><span className="act-label">删除</span></span>
          </button>
        </div>
      )}
    </div>
  )
}

/**
 * 全局左侧栏里常驻的对话列表：每个页面都在，进出 AI 研究室时侧栏不变形。
 * 当前对话按网址 #/lab/<id> 高亮；研究室里有变化时通过 bus 通知刷新。
 */
export function SidebarConversations({ activeId, notify }: { activeId: string; notify: Notify }) {
  const [projects, setProjects] = useState<LabProjectBrief[]>([])
  const [shares, setShares] = useState<ShareMap>({})
  const refresh = useCallback(() => { lab.projects().then((r) => setProjects(r.projects)).catch(() => undefined) }, [])
  useEffect(() => { refresh(); return onLabChanged(refresh) }, [refresh])
  // 分享状态来自现有的可见性记录（一次取全部会话），缺省按私有；本地部署没有分享概念，跳过请求。
  const idsKey = projects.map((p) => p.id).join(',')
  useEffect(() => {
    if (!idsKey || isLocalDeployment()) return
    let alive = true
    collab.editors('project').then((r) => {
      if (!alive) return
      const next: ShareMap = {}
      for (const [id, info] of Object.entries(r.visibility ?? {})) next[id] = info.visibility
      setShares(next)
    }).catch(() => undefined)
    return () => { alive = false }
  }, [idsKey])
  // 有对话在跑时轮询，标题、状态点跟着变
  const running = projects.some((p) => p.status === 'running')
  useEffect(() => {
    const t = window.setInterval(() => { if (!document.hidden) refresh() }, running ? 2500 : 5000)
    return () => window.clearInterval(t)
  }, [running, refresh])
  return (
    <Conversations projects={projects} activeId={activeId} notify={notify} onChanged={labChanged} shares={shares}
      onOpen={(id) => { window.location.hash = id ? `/lab/${id}` : '/lab' }}
      onNew={(folder) => { requestNewChat(folder ?? ''); window.location.hash = '/lab' }} />
  )
}
