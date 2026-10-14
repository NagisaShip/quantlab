/**
 * 角色簿：全站共用一份角色缓存（头像、模型、努力程度），以及点头像弹出的角色编辑抽屉。
 * 任何地方的 <Avatar id="risk_officer" /> 被点击都会打开同一个抽屉，保存后所有头像即时更新。
 */
import { useEffect, useState, useSyncExternalStore } from 'react'
import { api, type Role, type ToolInfo } from '../api'
import type { ModelOption } from '../apiLab'
import { AvatarFace } from './Avatar'
import { modelLabel } from './chat/Composer'

interface Book {
  roles: Role[]
  byId: Record<string, Role>
  tools: ToolInfo[]
  models: ModelOption[]
  loaded: boolean
}

let book: Book = { roles: [], byId: {}, tools: [], models: [], loaded: false }
let loading: Promise<void> | null = null
const listeners = new Set<() => void>()
const emit = () => listeners.forEach((fn) => fn())

function setRoles(roles: Role[], extra?: Partial<Book>) {
  book = { ...book, ...extra, roles, byId: Object.fromEntries(roles.map((r) => [r.id, r])), loaded: true }
  emit()
}

export function loadRoles(force = false): Promise<void> {
  if (loading && !force) return loading
  loading = api.roles()
    .then((d) => setRoles(d.roles, { tools: d.tool_catalog ?? d.tools.map((n) => ({ name: n, label: n, description: '' })), models: d.models as ModelOption[] }))
    .catch(() => { loading = null })
  return loading
}

export function putRole(role: Role) {
  const exists = book.byId[role.id]
  setRoles(exists ? book.roles.map((r) => (r.id === role.id ? role : r)) : [...book.roles, role])
}

export function useRoleBook(): Book {
  const b = useSyncExternalStore((fn) => { listeners.add(fn); return () => listeners.delete(fn) }, () => book)
  useEffect(() => { if (!b.loaded) loadRoles() }, [b.loaded])
  return b
}

/** 打开角色编辑抽屉。 */
export function openRoleEditor(id: string) {
  window.dispatchEvent(new CustomEvent('qa:role-edit', { detail: id }))
}

const EFFORTS = [
  { id: '', label: '默认' }, { id: 'low', label: '低' }, { id: 'medium', label: '中' }, { id: 'high', label: '高' }, { id: 'max', label: '极高' },
]
const AVATAR_PICKS = ['', '📈', '🧭', '🛠', '🧪', '🛡', '⚖', '🦊', '🐂', '🐻', '🔍', '⭐']

export function RoleDrawer({ notify }: { notify: (msg: string, error?: boolean) => void }) {
  const { byId, tools, models } = useRoleBook()
  const [id, setId] = useState('')
  const [draft, setDraft] = useState<Role | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const onOpen = (e: Event) => { setId(String((e as CustomEvent).detail || '')); loadRoles() }
    window.addEventListener('qa:role-edit', onOpen)
    return () => window.removeEventListener('qa:role-edit', onOpen)
  }, [])
  useEffect(() => { setDraft(id && byId[id] ? { ...byId[id] } : null) }, [id, byId])
  useEffect(() => {
    if (!id) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setId('') }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [id])

  if (!id) return null
  const close = () => setId('')
  const set = <K extends keyof Role>(k: K, v: Role[K]) => setDraft((d) => (d ? { ...d, [k]: v } : d))
  const original = byId[id]
  const dirty = !!draft && !!original && JSON.stringify(draft) !== JSON.stringify(original)
  const groups = models.reduce<Record<string, ModelOption[]>>((acc, m) => { (acc[m.provider] ||= []).push(m); return acc }, {})

  const save = async () => {
    if (!draft) return
    setSaving(true)
    try {
      const r = await api.updateRole(draft.id, draft)
      putRole(r.role)
      notify('角色已保存')
      close()
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setSaving(false)
    }
  }
  const reset = async () => {
    try {
      const r = await api.resetRole(id)
      putRole(r.role)
      notify('已恢复默认设置')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }
  const duplicate = async () => {
    try {
      const r = await api.duplicateRole(id)
      putRole(r.role)
      setId(r.role.id)
      notify('已复制为新角色')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  return (
    <div className="rd-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) close() }}>
      <aside className="rd" role="dialog" aria-label="编辑角色">
        {!draft ? (
          <div className="rd-body"><div className="muted">正在读取角色…</div></div>
        ) : (
          <>
            <header className="rd-head">
              <RoleFace role={draft} size={40} />
              <div className="rd-head-text">
                <b>{draft.name || '未命名角色'}</b>
                <span>{draft.builtin ? (original?.customized ? '内置角色 · 已修改' : '内置角色') : '自定义角色'}</span>
              </div>
              <button className="lab2-icon" onClick={close} aria-label="关闭">
                <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
              </button>
            </header>

            <div className="rd-body">
              <div className="rd-row2">
                <label className="rd-field"><span>名称</span>
                  <input value={draft.name} onChange={(e) => set('name', e.target.value)} /></label>
                <label className="rd-field"><span>定位</span>
                  <input value={draft.title} onChange={(e) => set('title', e.target.value)} /></label>
              </div>

              <div className="rd-field"><span>头像</span>
                <div className="rd-avatars">
                  {AVATAR_PICKS.map((a) => (
                    <button key={a || 'default'} type="button" className={(draft.avatar || '') === a ? 'on' : ''}
                      onClick={() => set('avatar', a)} title={a ? a : '默认头像'}>
                      {a ? a : <RoleFace role={{ ...draft, avatar: '' }} size={22} />}
                    </button>
                  ))}
                  <input className="rd-avatar-input" maxLength={2} placeholder="字" value={AVATAR_PICKS.includes(draft.avatar || '') ? '' : draft.avatar}
                    onChange={(e) => set('avatar', e.target.value)} aria-label="自定义头像文字" />
                </div>
              </div>

              <label className="rd-field"><span>模型</span>
                <select value={draft.model} onChange={(e) => set('model', e.target.value)}>
                  <option value="">跟随全局路由</option>
                  {Object.entries(groups).map(([p, items]) => (
                    <optgroup key={p} label={p}>
                      {items.map((m) => <option key={m.target} value={m.target}>{modelLabel(m.target)}{m.ready === false ? '（未配置）' : ''}</option>)}
                    </optgroup>
                  ))}
                  {draft.model && !models.some((m) => m.target === draft.model) && <option value={draft.model}>{draft.model}</option>}
                </select>
              </label>

              <div className="rd-field"><span>思考努力程度</span>
                <div className="cmp-seg" role="radiogroup">
                  {EFFORTS.map((e) => (
                    <button key={e.id} type="button" role="radio" aria-checked={(draft.effort || '') === e.id}
                      className={(draft.effort || '') === e.id ? 'on' : ''} onClick={() => set('effort', e.id)}>{e.label}</button>
                  ))}
                </div>
              </div>

              <label className="rd-field"><span>温度 <em>{draft.temperature.toFixed(1)}</em></span>
                <input type="range" min={0} max={1} step={0.1} value={draft.temperature} onChange={(e) => set('temperature', Number(e.target.value))} />
              </label>

              <div className="rd-field"><span>可用工具</span>
                <div className="rd-tools">
                  {tools.map((t) => (
                    <label key={t.name} className="rd-tool" title={t.description}>
                      <input type="checkbox" checked={draft.tools.includes(t.name)}
                        onChange={(e) => set('tools', e.target.checked ? [...draft.tools, t.name] : draft.tools.filter((x) => x !== t.name))} />
                      <span>{t.label}</span>
                    </label>
                  ))}
                </div>
              </div>

              <label className="rd-field"><span>系统提示词</span>
                <textarea rows={10} value={draft.system_prompt} onChange={(e) => set('system_prompt', e.target.value)} /></label>
              <label className="rd-field"><span>输出格式</span>
                <input value={draft.output_format} onChange={(e) => set('output_format', e.target.value)} /></label>
            </div>

            <footer className="rd-foot">
              {draft.builtin && original?.customized && <button className="button small" onClick={reset}>恢复默认</button>}
              <button className="button small" onClick={duplicate}>复制为新角色</button>
              <span style={{ flex: 1 }} />
              <button className="button small" onClick={close}>取消</button>
              <button className="button small primary" disabled={!dirty || saving || !draft.name.trim()} onClick={save}>{saving ? '保存中…' : '保存'}</button>
            </footer>
          </>
        )}
      </aside>
    </div>
  )
}

/** 角色头像本体：有自定义头像（emoji / 字）时显示它，否则用几何人物。 */
export function RoleFace({ role, size }: { role: Pick<Role, 'id' | 'avatar'>; size: number }) {
  return <AvatarFace id={role.id} avatar={role.avatar} size={size} />
}
