/**
 * 顶栏的通知铃铛：未读角标、弹出的通知列表（按类型筛选、全部已读、点开跳转），
 * 以及部署管理员发布的顶栏横幅。每 30 秒刷新一次角标；页面隐藏时不刷新。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { collab, inbox, NOTIFY_KIND_LABEL, since, type InboxSummary, type Notice } from '../apiCollab'
import { Icon } from './icons'
import { QuantEyes } from './QuantBot'

const POLL_MS = 20_000

/*
 * 角标数据全站共用一份：铃铛、头像红点、公告横幅都订阅它，只有一个轮询（有订阅者时才跑，页面隐藏时暂停）。
 */
let shared: InboxSummary | null = null
const subs = new Set<(s: InboxSummary | null) => void>()
let timer = 0
let inflight = false
function fetchSummary() {
  if (document.visibilityState === 'hidden' || inflight) return
  inflight = true
  inbox.summary().then((s) => { shared = s; subs.forEach((f) => f(s)) }).catch(() => undefined).finally(() => { inflight = false })
}
const onVisible = () => fetchSummary()
function subscribe(fn: (s: InboxSummary | null) => void) {
  subs.add(fn)
  if (subs.size === 1) {
    fetchSummary()
    timer = window.setInterval(fetchSummary, POLL_MS)
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('qa:inbox-changed', onVisible)
  }
  return () => {
    subs.delete(fn)
    if (!subs.size) {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('qa:inbox-changed', onVisible)
    }
  }
}

export function useInboxSummary() {
  const [sum, setSum] = useState<InboxSummary | null>(shared)
  useEffect(() => subscribe(setSum), [])
  const reload = useCallback(() => fetchSummary(), [])
  return { sum, reload }
}

export const pingInbox = () => window.dispatchEvent(new CustomEvent('qa:inbox-changed'))

/** 头像右上角的红点：未读通知 + 群聊 / 私聊未读消息的总数。 */
export function UnreadDot() {
  const { sum } = useInboxSummary()
  const n = sum?.unread.total ?? 0
  if (!n) return null
  return <span className="avatar-dot anim-pop" key={n} aria-label={`${n} 条未读`}>{n > 99 ? '99+' : n}</span>
}

const KIND_ICON: Record<string, string> = { mention: 'bell', dm: 'message', task: 'validation', group: 'groups', comment: 'message', like: 'square', follow: 'teams', share: 'square', issue: 'issues', announce: 'inbox', system: 'overview' }

export function InboxBell({ onNavigate }: { onNavigate: (k: string) => void }) {
  const { sum, reload } = useInboxSummary()
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState('')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [items, setItems] = useState<Notice[] | null>(null)
  const ref = useRef<HTMLDivElement>(null)
  const unread = sum?.unread.total ?? 0

  const load = useCallback(() => {
    inbox.list({ kind: kind || undefined, unread: unreadOnly }).then((r) => setItems(r.items)).catch(() => setItems([]))
  }, [kind, unreadOnly])
  useEffect(() => { if (open) load() }, [open, load])
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [open])

  const openItem = async (n: Notice) => {
    if (!n.read_at) await inbox.read({ ids: [n.id] }).catch(() => undefined)
    setOpen(false)
    reload()
    if (n.link) onNavigate(n.link)
  }
  const kinds = Object.entries(sum?.unread ?? {}).filter(([k, v]) => k !== 'total' && v > 0)

  return (
    <div className="inbox-anchor" ref={ref}>
      <button type="button" className={`inbox-bell${unread ? ' has' : ''}`} onClick={() => setOpen(!open)} aria-haspopup="dialog" aria-expanded={open}
        aria-label={unread ? `通知（${unread} 条未读）` : '通知'} title="消息通知">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M4 11.5V7a4 4 0 0 1 8 0v4.5l1 1.2H3z" /><path d="M6.6 14a1.5 1.5 0 0 0 2.8 0" />
        </svg>
        {unread > 0 && <span className="inbox-dot" key={unread}>{unread > 99 ? '99+' : unread}</span>}
      </button>
      {open && (
        <div className="inbox-pop" role="dialog" aria-label="通知">
          <div className="inbox-head">
            <b>通知</b>
            <label className="inbox-toggle"><input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />只看未读</label>
            <button type="button" className="link-btn" disabled={!unread} onClick={async () => { await inbox.read({ kind: kind || undefined }); reload(); load() }}>全部已读</button>
          </div>
          <div className="inbox-kinds">
            <button type="button" className={`chip small${kind === '' ? ' on' : ''}`} onClick={() => setKind('')}>全部</button>
            {['mention', 'dm', 'task', 'comment', 'group', 'follow', 'issue', 'announce'].map((k) => (
              <button key={k} type="button" className={`chip small${kind === k ? ' on' : ''}`} onClick={() => setKind(k)}>
                {NOTIFY_KIND_LABEL[k]}{(sum?.unread[k] ?? 0) > 0 && <small> {sum!.unread[k]}</small>}
              </button>
            ))}
          </div>
          <div className="inbox-list">
            {!items ? (
              <div className="inbox-empty"><QuantEyes size={22} state="thinking" /> 加载中…</div>
            ) : !items.length ? (
              <div className="inbox-empty"><QuantEyes size={22} state="idle" motion="low" />{unreadOnly ? '没有未读通知' : '还没有通知'}</div>
            ) : (
              <div className="stagger">
                {items.slice(0, 30).map((n) => (
                  <button key={n.id} type="button" className={`inbox-item${n.read_at ? '' : ' unread'}`} onClick={() => openItem(n)}>
                    <span className={`inbox-ic k-${n.kind}`} aria-hidden><Icon name={KIND_ICON[n.kind] ?? 'inbox'} size={14} /></span>
                    <span className="inbox-main">
                      <b>{n.title}</b>
                      {n.body && <small>{n.body}</small>}
                      <em>{NOTIFY_KIND_LABEL[n.kind] ?? n.kind} · {since(n.created_at)}</em>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <div className="inbox-foot">
            {kinds.length > 0 && <span className="hint">{kinds.map(([k, v]) => `${NOTIFY_KIND_LABEL[k] ?? k} ${v}`).join(' · ')}</span>}
            <span className="spacer" />
            <button type="button" className="link-btn" onClick={() => { setOpen(false); onNavigate('inbox') }}>通知中心与屏蔽设置 →</button>
          </div>
        </div>
      )}
    </div>
  )
}

/** 部署管理员发布的顶栏横幅：可以关掉（记在这台设备上，同一条不再显示）。 */
export function PlatformBanner() {
  const { sum } = useInboxSummary()
  const b = sum?.banner
  const key = b ? `qa.banner.${b.at ?? b.text}` : ''
  const [hidden, setHidden] = useState(false)
  useEffect(() => {
    try { setHidden(!!key && localStorage.getItem(key) === '1') } catch { setHidden(false) }
  }, [key])
  if (!b || hidden) return null
  return (
    <div className={`platform-banner anim-expand ${b.tone === 'warn' ? 'warn' : ''}`} role="status">
      <span className="pb-tag">公告</span>
      <b>{b.text}</b>
      {b.body && <span className="pb-body">{b.body}</span>}
      <button type="button" className="pb-x" aria-label="关闭公告" onClick={() => { try { localStorage.setItem(key, '1') } catch { /* */ } setHidden(true) }}>×</button>
    </div>
  )
}

interface MessageToastItem {
  key: string
  groupId: string
  kind: 'group' | 'dm'
  source: string
  author: string
  body: string
}

/** 全局新消息浮层：右下角显示来源与简介，4.5 秒后自动消失。 */
export function MessageToasts({ meName, onNavigate }: { meName: string; onNavigate: (k: string) => void }) {
  const [items, setItems] = useState<MessageToastItem[]>([])
  const baseline = useRef<Map<string, string> | null>(null)
  const mutedGroups = useRef(new Set<string>())
  const mutedUsers = useRef(new Set<string>())
  const timers = useRef<number[]>([])
  const dismiss = useCallback((key: string) => setItems((prev) => prev.filter((x) => x.key !== key)), [])
  const enqueue = useCallback((incoming: MessageToastItem[]) => {
    if (!incoming.length) return
    setItems((prev) => [...prev, ...incoming].slice(-3))
    for (const item of incoming) timers.current.push(window.setTimeout(() => dismiss(item.key), 4500))
  }, [dismiss])

  useEffect(() => {
    let alive = true
    const loadMutes = () => inbox.mutes().then((r) => {
      if (!alive) return
      const groups = new Set<string>()
      const users = new Set<string>()
      for (const m of r.mutes) {
        if (m.target_type === 'group') groups.add(m.target_id)
        if (m.target_type === 'user') users.add(m.label || m.target_id)
      }
      mutedGroups.current = groups
      mutedUsers.current = users
    }).catch(() => undefined)

    const tick = async () => {
      if (document.visibilityState === 'hidden') return
      try {
        const r = await collab.groups()
        if (!alive) return
        const seen = new Map<string, string>()
        const fresh: MessageToastItem[] = []
        for (const g of r.groups) {
          const m = g.last_message
          if (!m || !g.my_role) continue
          const key = `${m.at}|${m.author}|${m.body}`
          seen.set(g.id, key)
          if (!baseline.current || baseline.current.get(g.id) === key) continue
          if (m.author === meName || m.author === '系统') continue
          if (mutedGroups.current.has(g.id) || mutedUsers.current.has(m.author)) continue
          const current = window.location.hash.replace(/^#\/?/, '')
          if (current === `groups/${g.id}`) continue
          fresh.push({
            key: `${g.id}:${key}`,
            groupId: g.id,
            kind: g.kind === 'dm' ? 'dm' : 'group',
            source: g.kind === 'dm' ? `私聊 · ${g.peer?.name || g.name}` : `群组 · ${g.name}`,
            author: m.author,
            body: (m.body || '[引用或附件]').replace(/\s+/g, ' ').slice(0, 120),
          })
        }
        baseline.current = seen
        enqueue(fresh)
      } catch { /* 登录切换或网络波动时静默重试 */ }
    }

    loadMutes()
    void tick()
    const timer = window.setInterval(tick, 6000)
    const onVisible = () => { if (document.visibilityState !== 'hidden') { loadMutes(); void tick() } }
    const onInbox = () => { loadMutes() }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('qa:inbox-changed', onInbox)
    return () => {
      alive = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('qa:inbox-changed', onInbox)
      timers.current.forEach((t) => window.clearTimeout(t))
      timers.current = []
    }
  }, [enqueue, meName])

  if (!items.length) return null
  return (
    <div className="message-toasts" aria-live="polite" aria-label="新消息通知">
      {items.map((item) => (
        <button key={item.key} type="button" className="message-toast anim-in"
          onClick={() => { dismiss(item.key); onNavigate(`groups/${item.groupId}`) }}
          title={`打开 ${item.source}`}>
          <span className="message-toast-icon"><Icon name={item.kind === 'dm' ? 'message' : 'groups'} size={16} /></span>
          <span className="message-toast-main">
            <span className="message-toast-source">{item.source}</span>
            <b>{item.author}</b>
            <small>{item.body}</small>
          </span>
          <span className="message-toast-close" aria-hidden>×</span>
          <i className="message-toast-life" aria-hidden />
        </button>
      ))}
    </div>
  )
}
