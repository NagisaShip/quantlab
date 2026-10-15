/**
 * AI 研究室的对话列表（渲染在全局左侧栏里）：
 * - 项目（文件夹）分组，可新建、重命名、解散；置顶的对话单独排在最上面；
 * - 分叉出来的对话以树状嵌套在原对话下面，可折叠；
 * - 每行最右侧「更多」，点击或在行上右键：重命名、置顶、分叉、移动到项目、删除。
 */
import { Fragment, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { lab, type LabProjectBrief } from '../../apiLab'
import { store } from '../../components/prefs'
import { domainUI } from './domains'

type Notify = (msg: string, error?: boolean) => void

const FOLDERS_KEY = 'qa.lab.folders'
const COLLAPSED_KEY = 'qa.lab.collapsed'

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

export function Conversations({ projects, activeId, onOpen, onNew, onChanged, notify }: {
  projects: LabProjectBrief[]
  activeId: string
  onOpen: (id: string) => void
  onNew: (folder?: string) => void
  onChanged: () => void
  notify: Notify
}) {
  const [q, setQ] = useState('')
  const [menu, setMenu] = useState<Menu | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [folders, setFolders] = useState<string[]>(() => readList(FOLDERS_KEY))
  const [collapsed, setCollapsed] = useState<string[]>(() => readList(COLLAPSED_KEY))
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!menu) return
    const close = (e: Event) => { if (!menuRef.current?.contains(e.target as Node)) setMenu(null) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setMenu(null) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    window.addEventListener('blur', () => setMenu(null), { once: true })
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [menu])

  const allFolders = useMemo(() => [...new Set([...folders, ...projects.map((p) => p.folder || '').filter(Boolean)])].sort(), [folders, projects])
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
  const newFolder = () => {
    const name = window.prompt('新项目名称（用来归类对话）')?.trim().slice(0, 40)
    if (!name) return
    const next = [...folders, name]
    setFolders(next)
    writeList(FOLDERS_KEY, next)
    return name
  }
  const renameFolder = async (old: string) => {
    const name = window.prompt('项目名称', old)?.trim().slice(0, 40)
    if (!name || name === old) return
    await Promise.all(projects.filter((p) => p.folder === old).map((p) => lab.update(p.id, { folder: name })))
    const next = [...folders.filter((f) => f !== old), name]
    setFolders(next); writeList(FOLDERS_KEY, next); onChanged()
  }
  const dissolveFolder = async (name: string) => {
    if (!window.confirm(`解散项目「${name}」？里面的对话会移到「对话」下，不会删除。`)) return
    await Promise.all(projects.filter((p) => p.folder === name).map((p) => lab.update(p.id, { folder: '' })))
    const next = folders.filter((f) => f !== name)
    setFolders(next); writeList(FOLDERS_KEY, next); onChanged()
  }

  const row = (p: LabProjectBrief, depth = 0): JSX.Element => {
    const kids = sortRows(children.get(p.id) ?? []).filter(subtreeMatches)
    const open = !collapsed.includes(p.id) || !!needle
    const ui = domainUI(p.kind)
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
            <button type="button" className="conv-main" onClick={() => onOpen(p.id)}>
              {p.status !== 'idle' && <i className={`dot ${p.status}`} />}
              {p.pinned && depth === 0 && <span className="conv-pin" aria-label="已置顶">📌</span>}
              <span className="conv-title">{p.title}</span>
              {ui && <span className="conv-kind">{ui.noun}</span>}
            </button>
          )}
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

  return (
    <div className="conv">
      <div className="conv-head">
        <span className="conv-label">对话</span>
        <button type="button" className="conv-icon" title="新建项目（文件夹）" aria-label="新建项目" onClick={() => newFolder()}>
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" aria-hidden><path d="M2 4.5h4l1.2 1.3H14v6.7H2Z" /><path d="M8 7.8v3M6.5 9.3h3" strokeLinecap="round" /></svg>
        </button>
        <button type="button" className="conv-icon" title="新对话" aria-label="新对话" onClick={() => onNew()}>
          <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
        </button>
      </div>
      <input className="conv-search" placeholder="搜索对话" value={q} onChange={(e) => setQ(e.target.value)} aria-label="搜索对话" />
      <div className="conv-list">
        {pinned.length > 0 && (
          <div className="conv-group">
            <div className="conv-group-label">置顶</div>
            {pinned.map((p) => row(p))}
          </div>
        )}
        {allFolders.map((f) => {
          const rows = sortRows(roots.filter((p) => !p.pinned && p.folder === f))
          if (needle && !rows.length) return null
          const key = `folder:${f}`
          const open = !collapsed.includes(key) || !!needle
          return (
            <div key={f} className="conv-group">
              <div className="conv-folder" onContextMenu={(e) => { e.preventDefault(); renameFolder(f) }}>
                <button type="button" className={`conv-caret${open ? ' open' : ''}`} onClick={() => toggle(key)} aria-label={open ? '收起项目' : '展开项目'}>
                  <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="m3.5 2 3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
                </button>
                <button type="button" className="conv-folder-name" onClick={() => toggle(key)}>
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" aria-hidden><path d="M2 4.5h4l1.2 1.3H14v6.7H2Z" /></svg>
                  <span>{f}</span><em>{rows.length || ''}</em>
                </button>
                <button type="button" className="conv-more" title={`在「${f}」里新对话`} aria-label="在项目里新对话" onClick={() => onNew(f)}>
                  <svg width="13" height="13" viewBox="0 0 16 16" aria-hidden><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
                </button>
                <button type="button" className="conv-more" title="重命名 / 解散项目" aria-label="项目操作"
                  onClick={() => { const v = window.prompt(`项目「${f}」：输入新名称；清空后确定 = 解散项目`, f); if (v === null) return; if (!v.trim()) dissolveFolder(f); else renameFolder(f) }}>
                  <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden><circle cx="3.5" cy="8" r="1.2" fill="currentColor" /><circle cx="8" cy="8" r="1.2" fill="currentColor" /><circle cx="12.5" cy="8" r="1.2" fill="currentColor" /></svg>
                </button>
              </div>
              {open && (rows.length ? rows.map((p) => row(p, 1)) : <div className="conv-empty">空项目：点 ＋ 新建对话，或把对话移进来</div>)}
            </div>
          )
        })}
        <div className="conv-group">
          {(allFolders.length > 0 || pinned.length > 0) && <div className="conv-group-label">对话</div>}
          {loose.map((p) => row(p))}
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
            <span className="act-text"><span className="act-label">移动到项目</span><span className="act-hint">{target.folder ? `现在在「${target.folder}」` : '现在不在任何项目里'}</span></span>
            <span className="act-chev"><svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="m4.5 2.5 3.5 3.5-3.5 3.5" /></svg></span>
          </button>
          {moveOpen && (
            <div className="act-sub anim-expand">
              {allFolders.filter((f) => f !== target.folder).map((f) => (
                <button key={f} type="button" role="menuitem" className="act-item" onClick={() => act(() => lab.update(target.id, { folder: f }), `已移到「${f}」`)}>
                  <span className="act-text"><span className="act-label">{f}</span></span>
                </button>
              ))}
              <button type="button" role="menuitem" className="act-item" onClick={() => { const f = newFolder(); if (f) act(() => lab.update(target.id, { folder: f }), `已移到「${f}」`) }}>
                <span className="act-text"><span className="act-label">＋ 新建项目…</span></span>
              </button>
              {target.folder && (
                <button type="button" role="menuitem" className="act-item" onClick={() => act(() => lab.update(target.id, { folder: '' }), '已移出项目')}>
                  <span className="act-text"><span className="act-label">移出项目</span></span>
                </button>
              )}
            </div>
          )}
          <div className="cmp-menu-sep" />
          <button type="button" role="menuitem" className="act-item danger"
            onClick={() => { setMenu(null); if (window.confirm(`删除对话「${target.title}」？已保存到策略库 / 因子库的内容不受影响。${children.get(target.id)?.length ? '\n它的分叉会保留，变成独立对话。' : ''}`)) act(async () => { await lab.remove(target.id); if (target.id === activeId) onOpen('') }, '已删除') }}>
            <span className="act-text"><span className="act-label">删除</span></span>
          </button>
        </div>
      )}
    </div>
  )
}
