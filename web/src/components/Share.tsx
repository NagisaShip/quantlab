/**
 * 可见性与编辑人员标记：
 * - ShareButton：策略 / 研究项目「仅自己 / 工作区内公开 / 指定群组内公开」的切换（作者或工作区管理员可改）；
 * - EditorStack：一排编辑者头像（人与 Agent 区分），悬停看谁改了几次、最近什么时候；
 * - EditHistory：逐条编辑记录（谁、人还是 Agent、哪个版本、派生自哪里）。
 */
import { useEffect, useRef, useState } from 'react'
import { collab, since, VIS_LABEL, type EditorMark, type EditRow, type ShareInfo, type Visibility } from '../apiCollab'
import { QuantBot } from './QuantBot'

const VIS_ICON: Record<Visibility, string> = { private: '🔒', workspace: '◎', group: '◍' }

export function VisibilityChip({ v, onClick }: { v: Visibility; onClick?: () => void }) {
  return (
    <button type="button" className={`vis-chip ${v}`} onClick={onClick} disabled={!onClick} title={VIS_LABEL[v]}>
      <span aria-hidden>{VIS_ICON[v]}</span>{v === 'workspace' ? '公开' : v === 'private' ? '仅自己' : '群组'}
    </button>
  )
}

export function ShareButton({ type, id, notify, onChanged, compact }: {
  type: 'strategy' | 'project'; id: string; notify: (m: string, e?: boolean) => void; onChanged?: (v: Visibility) => void; compact?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [info, setInfo] = useState<ShareInfo | null>(null)
  const [vis, setVis] = useState<Visibility>('workspace')
  const [groups, setGroups] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const load = () => collab.share(type, id).then((r) => { setInfo(r); setVis(r.share.visibility); setGroups(r.share.group_ids) }).catch(() => undefined)
  useEffect(() => { load() }, [type, id]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [open])
  const save = async () => {
    setSaving(true)
    try {
      await collab.setShare(type, id, vis, vis === 'group' ? groups : [])
      notify(`已设为${VIS_LABEL[vis]}`)
      onChanged?.(vis)
      setOpen(false)
      load()
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setSaving(false)
    }
  }
  const cur = info?.share.visibility ?? 'workspace'
  return (
    <div className="cmp-pop-anchor share-anchor" ref={ref}>
      <button type="button" className={`button small share-btn ${cur}`} onClick={() => setOpen(!open)} aria-haspopup="dialog" aria-expanded={open}
        title={info?.owner_name ? `作者：${info.owner_name}` : undefined}>
        <span aria-hidden>{VIS_ICON[cur]}</span>{compact ? (cur === 'workspace' ? '公开' : cur === 'private' ? '仅自己' : '群组') : VIS_LABEL[cur]}
      </button>
      {open && info && (
        <div className="share-pop" role="dialog" aria-label="可见性">
          <div className="share-pop-head">
            <b>谁能看到这个{type === 'strategy' ? '策略' : '研究项目'}</b>
            <small>{info.owner_name ? `作者 ${info.owner_name}` : '未登记作者'} · 工作区管理员始终可见</small>
          </div>
          {(['private', 'workspace', 'group'] as Visibility[]).map((v) => (
            <label key={v} className={`share-opt${vis === v ? ' on' : ''}${!info.can_edit ? ' ro' : ''}`}>
              <input type="radio" name={`vis-${id}`} checked={vis === v} disabled={!info.can_edit} onChange={() => setVis(v)} />
              <span className="share-ic" aria-hidden>{VIS_ICON[v]}</span>
              <span><b>{VIS_LABEL[v]}</b><small>{v === 'private' ? '只有你（和工作区管理员）能看到、能用' : v === 'workspace' ? '工作区所有成员都能看到、克隆、回测' : '只有所选群组的成员能看到'}</small></span>
            </label>
          ))}
          {vis === 'group' && (
            <div className="share-groups anim-expand">
              {!info.groups.length && <div className="hint">还没有群组，先去「群组」建一个。</div>}
              {info.groups.map((g) => (
                <label key={g.id} className="check">
                  <input type="checkbox" checked={groups.includes(g.id)} disabled={!info.can_edit}
                    onChange={(e) => setGroups(e.target.checked ? [...groups, g.id] : groups.filter((x) => x !== g.id))} />{g.name}
                </label>
              ))}
            </div>
          )}
          {info.can_edit ? (
            <div className="button-row" style={{ justifyContent: 'flex-end', marginTop: 8 }}>
              <button type="button" className="button small" onClick={() => setOpen(false)}>取消</button>
              <button type="button" className="button small primary" disabled={saving || (vis === 'group' && !groups.length)} onClick={save}>{saving ? '保存中…' : '保存'}</button>
            </div>
          ) : <div className="hint" style={{ marginTop: 6 }}>只有作者或工作区管理员可以修改。</div>}
        </div>
      )}
    </div>
  )
}

/** 编辑者头像：人用首字，Agent 用 QuantBot。最多显示 4 个。 */
export function EditorStack({ editors, max = 4 }: { editors: EditorMark[] | undefined; max?: number }) {
  if (!editors?.length) return null
  const shown = editors.slice(0, max)
  const title = editors.map((e) => `${e.actor_kind === 'agent' ? `QuantBot（代 ${e.name}）` : e.name}：${e.count} 次，最近 ${since(e.last_at)}`).join('\n')
  return (
    <span className="editor-stack" title={title} aria-label={`编辑者：${editors.map((e) => e.name).join('、')}`}>
      {shown.map((e, i) => (
        <span key={`${e.user_id}-${e.actor_kind}`} className={`ed-av${e.actor_kind === 'agent' ? ' agent' : ''}`} style={{ zIndex: max - i }}>
          {e.actor_kind === 'agent' ? <QuantBot size={14} variant="simple" /> : (e.name || '?').slice(0, 1)}
        </span>
      ))}
      {editors.length > max && <span className="ed-more">+{editors.length - max}</span>}
    </span>
  )
}

const ACTION_LABEL: Record<string, string> = { create: '创建', edit: '修改', clone: '克隆', backtest: '回测', approve: '批准', share: '共享' }

export function EditHistory({ type, id }: { type: string; id: string }) {
  const [rows, setRows] = useState<EditRow[] | null>(null)
  useEffect(() => { collab.edits(type, id).then((r) => setRows(r.edits)).catch(() => setRows([])) }, [type, id])
  if (!rows) return <div className="hint">加载中…</div>
  if (!rows.length) return <div className="hint">这份内容在启用编辑标记之前创建，还没有编辑记录。</div>
  return (
    <ol className="edit-log stagger">
      {[...rows].reverse().map((e) => (
        <li key={e.id}>
          <span className={`ed-av${e.actor_kind === 'agent' ? ' agent' : ''}`}>{e.actor_kind === 'agent' ? <QuantBot size={14} variant="simple" /> : (e.user_name || '?').slice(0, 1)}</span>
          <div>
            <b>{e.actor_kind === 'agent' ? `QuantBot（代 ${e.user_name}）` : e.user_name || '平台'}</b> {ACTION_LABEL[e.action] ?? e.action}
            {e.version && <> 出版本 <code>{e.version}</code></>}
            {e.parent_version && <small>（基于 <code>{e.parent_version}</code>）</small>}
            {e.derived_from && <small> · 派生自 {e.derived_from.replace(/^strategy:/, '策略 ').replace(/^post:/, '帖子 ')}</small>}
            <small className="muted"> · {since(e.created_at)}</small>
          </div>
        </li>
      ))}
    </ol>
  )
}
