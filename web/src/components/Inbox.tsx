/**
 * 顶栏的通知铃铛：未读角标、弹出的通知列表（按类型筛选、全部已读、点开跳转），
 * 以及部署管理员发布的顶栏横幅。每 30 秒刷新一次角标；页面隐藏时不刷新。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { inbox, NOTIFY_KIND_LABEL, since, type InboxSummary, type Notice } from '../apiCollab'
import { QuantEyes } from './QuantBot'

const POLL_MS = 30_000

export function useInboxSummary() {
  const [sum, setSum] = useState<InboxSummary | null>(null)
  const load = useCallback(() => {
    if (document.visibilityState === 'hidden') return
    inbox.summary().then(setSum).catch(() => undefined)
  }, [])
  useEffect(() => {
    load()
    const t = window.setInterval(load, POLL_MS)
    const onVis = () => load()
    const onPing = () => load()
    document.addEventListener('visibilitychange', onVis)
    window.addEventListener('qa:inbox-changed', onPing)
    return () => { window.clearInterval(t); document.removeEventListener('visibilitychange', onVis); window.removeEventListener('qa:inbox-changed', onPing) }
  }, [load])
  return { sum, reload: load }
}

export const pingInbox = () => window.dispatchEvent(new CustomEvent('qa:inbox-changed'))

const KIND_ICON: Record<string, string> = { mention: '@', task: '☐', group: '◎', comment: '✎', like: '▲', share: '⇪', issue: '!', announce: '📣', system: '·' }

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
            {['mention', 'task', 'comment', 'group', 'issue', 'announce'].map((k) => (
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
                    <span className={`inbox-ic k-${n.kind}`} aria-hidden>{KIND_ICON[n.kind] ?? '·'}</span>
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
