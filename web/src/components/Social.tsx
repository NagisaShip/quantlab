/**
 * 协作小组件（全站复用）：
 * - MemberPicker：搜索工作区成员（名字 / 用户名 / 邮箱），单选或多选，带在线绿点；
 * - ShareToButton：把任意内容（回测、策略、报告、帖子、站内页面）分享到群组 / 私聊 / 策略广场；
 * - usePresence + PresenceBar：谁也在看 / 正在编辑这个对象（心跳 20 秒）。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { collab, type Group, type Member, type Ref } from '../apiCollab'
import { v4 } from '../api/platform'
import { Modal } from './ui'
import { RefCard } from './RichText'

/* ------------------------------------------------------------------ 成员选择 */
let memberCache: { at: number; list: Member[] } | null = null
export function useMembers() {
  const [list, setList] = useState<Member[]>(memberCache?.list ?? [])
  useEffect(() => {
    if (memberCache && Date.now() - memberCache.at < 30_000) return
    collab.members().then((r) => { memberCache = { at: Date.now(), list: r.members }; setList(r.members) }).catch(() => undefined)
  }, [])
  return list
}

export function matchMember(m: Member, q: string) {
  const s = q.trim().toLowerCase()
  return !s || m.name.toLowerCase().includes(s) || (m.username || '').toLowerCase().includes(s)
}

export function MemberPicker({ members, picked, onChange, exclude = [], single, placeholder = '搜索成员：名字或用户名', autoFocus }: {
  members: Member[]; picked: string[]; onChange: (ids: string[]) => void; exclude?: string[]; single?: boolean; placeholder?: string; autoFocus?: boolean
}) {
  const [q, setQ] = useState('')
  const pool = members.filter((m) => !exclude.includes(m.user_id))
  const shown = pool.filter((m) => matchMember(m, q))
  const toggle = (id: string) => onChange(single ? [id] : picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id])
  return (
    <div className="mpick">
      <input className="search" value={q} autoFocus={autoFocus} onChange={(e) => setQ(e.target.value)} placeholder={placeholder} aria-label="搜索成员" />
      {!single && picked.length > 0 && (
        <div className="chip-row mpick-picked">
          {picked.map((id) => { const m = members.find((x) => x.user_id === id); return m ? <button key={id} type="button" className="chip on" onClick={() => toggle(id)}>{m.name} ×</button> : null })}
        </div>
      )}
      <div className="mpick-list" role="listbox" aria-multiselectable={!single}>
        {shown.slice(0, 60).map((m) => (
          <button key={m.user_id} type="button" role="option" aria-selected={picked.includes(m.user_id)} className={`mpick-item${picked.includes(m.user_id) ? ' on' : ''}`} onClick={() => toggle(m.user_id)}>
            <span className="ed-av">{m.name.slice(0, 1)}{m.online && <i className="online-dot" title="在线" />}</span>
            <span className="mpick-name"><b>{m.name}</b>{m.username && <small>@{m.username}</small>}</span>
            {picked.includes(m.user_id) && <span className="mpick-check" aria-hidden>✓</span>}
          </button>
        ))}
        {!shown.length && <div className="hint" style={{ padding: 8 }}>{pool.length ? '没有匹配的成员' : '工作区里还没有其他成员'}</div>}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ 分享到 */
type Target = 'group' | 'dm' | 'square'
const TARGET_LABEL: Record<Target, string> = { group: '群组', dm: '私聊', square: '策略广场' }

export function ShareToButton({ item, notify, onNavigate, label = '分享', small = true }: {
  item: Ref; notify: (m: string, e?: boolean) => void; onNavigate?: (k: string) => void; label?: string; small?: boolean
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className={`button${small ? ' small' : ''} share-to-btn`} onClick={() => setOpen(true)} title="分享到群组、私聊或策略广场">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M8 10V2.5M5 5.5l3-3 3 3" /><path d="M3 9v3.5c0 .6.4 1 1 1h8c.6 0 1-.4 1-1V9" /></svg>
        {label}
      </button>
      {open && <ShareToModal item={item} notify={notify} onNavigate={onNavigate} onClose={() => setOpen(false)} />}
    </>
  )
}

export function ShareToModal({ item, notify, onNavigate, onClose }: { item: Ref; notify: (m: string, e?: boolean) => void; onNavigate?: (k: string) => void; onClose: () => void }) {
  const [target, setTarget] = useState<Target>('group')
  const [groups, setGroups] = useState<Group[] | null>(null)
  const [picked, setPicked] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [title, setTitle] = useState(item.title || '')
  const [busy, setBusy] = useState(false)
  const members = useMembers()
  const [meId, setMeId] = useState('')
  useEffect(() => {
    collab.groups().then((r) => setGroups(r.groups.filter((g) => g.my_role && g.kind !== 'dm'))).catch(() => setGroups([]))
    v4.me().then((m) => setMeId(m.user.id)).catch(() => undefined)
  }, [])
  useEffect(() => setPicked([]), [target])
  const ready = target === 'square' ? title.trim().length >= 2 : picked.length > 0
  const send = async () => {
    setBusy(true)
    try {
      let link = ''
      for (const id of target === 'square' ? [''] : picked) {
        link = (await collab.shareTo({ target, target_id: id || undefined, ref: item, note, title: target === 'square' ? title : undefined })).link
      }
      notify(`已分享到${TARGET_LABEL[target]}${target !== 'square' && picked.length > 1 ? `（${picked.length} 处）` : ''}`)
      onClose()
      if (onNavigate && link && target === 'square') onNavigate(link)
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  return (
    <Modal title="分享" onClose={onClose} width={560}
      footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={!ready || busy} onClick={send}>{busy ? '发送中…' : `分享到${TARGET_LABEL[target]}`}</button></>}>
      <div className="share-to-item"><RefCard r={item} small /></div>
      <div className="seg" role="tablist" aria-label="分享到" style={{ margin: '12px 0' }}>
        {(Object.keys(TARGET_LABEL) as Target[]).map((t) => <button key={t} type="button" role="tab" aria-selected={target === t} onClick={() => setTarget(t)}>{TARGET_LABEL[t]}</button>)}
      </div>
      {target === 'group' && (
        !groups ? <div className="hint">加载中…</div> : !groups.length ? <div className="hint">你还没有加入任何群组，先去「群组」新建或加入一个。</div> : (
          <div className="mpick-list">
            {groups.map((g) => (
              <button key={g.id} type="button" className={`mpick-item${picked.includes(g.id) ? ' on' : ''}`} onClick={() => setPicked(picked.includes(g.id) ? picked.filter((x) => x !== g.id) : [...picked, g.id])}>
                <span className="grp-av" aria-hidden>{g.name.slice(0, 1)}</span>
                <span className="mpick-name"><b>{g.name}</b><small>{g.member_count} 人</small></span>
                {picked.includes(g.id) && <span className="mpick-check" aria-hidden>✓</span>}
              </button>
            ))}
          </div>
        )
      )}
      {target === 'dm' && <MemberPicker members={members} picked={picked} onChange={setPicked} exclude={meId ? [meId] : []} autoFocus />}
      {target === 'square' && (
        <label>帖子标题<input value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} placeholder="给这次分享起个标题" /></label>
      )}
      <label style={{ marginTop: 12 }}>附言（可选，支持 Markdown）
        <textarea rows={3} value={note} maxLength={4000} onChange={(e) => setNote(e.target.value)} placeholder={target === 'square' ? '写几句说明：结论、口径、想讨论的问题' : '说点什么…'} />
      </label>
    </Modal>
  )
}

/* ------------------------------------------------------------------ 在线状态 */
export interface Presence { user_id: string; name: string; editing: boolean }

/** 心跳：我在看 / 编辑这个对象；返回同时在场的其他成员。离开页面时通知服务端。 */
export function usePresence(objectType: string, objectId: string | undefined, editing = false): Presence[] {
  const [others, setOthers] = useState<Presence[]>([])
  const editingRef = useRef(editing)
  editingRef.current = editing
  useEffect(() => {
    if (!objectId) return
    let alive = true
    const beat = () => {
      if (document.visibilityState === 'hidden') return
      collab.presence({ object_type: objectType, object_id: objectId, editing: editingRef.current })
        .then((r) => { if (alive) setOthers(r.others) }).catch(() => undefined)
    }
    beat()
    const t = window.setInterval(beat, 20_000)
    document.addEventListener('visibilitychange', beat)
    return () => {
      alive = false
      window.clearInterval(t)
      document.removeEventListener('visibilitychange', beat)
      collab.presence({ object_type: objectType, object_id: objectId, leave: true }).catch(() => undefined)
    }
  }, [objectType, objectId])
  // 编辑状态变了立刻报一次，别人马上看到「正在编辑」
  useEffect(() => {
    if (objectId) collab.presence({ object_type: objectType, object_id: objectId, editing }).then((r) => setOthers(r.others)).catch(() => undefined)
  }, [editing, objectType, objectId])
  return others
}

export function PresenceBar({ others }: { others: Presence[] }) {
  const editing = useMemo(() => others.filter((o) => o.editing), [others])
  if (!others.length) return null
  const title = others.map((o) => `${o.name}${o.editing ? '（正在编辑）' : '（正在查看）'}`).join('\n')
  return (
    <span className="presence-bar anim-fade" title={title} aria-label={title}>
      {others.slice(0, 4).map((o) => <span key={o.user_id} className={`ed-av${o.editing ? ' editing' : ''}`}>{o.name.slice(0, 1)}</span>)}
      <small>{editing.length ? `${editing.map((o) => o.name).slice(0, 2).join('、')}${editing.length > 2 ? ` 等 ${editing.length} 人` : ''} 正在编辑` : `${others.length} 人在看`}</small>
    </span>
  )
}
