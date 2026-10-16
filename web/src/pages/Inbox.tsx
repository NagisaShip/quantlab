/**
 * 消息通知中心：全部通知（按类型筛选、只看未读、批量已读 / 清理），以及屏蔽设置
 * （屏蔽某位成员、某个群组、某类通知、某篇帖子的回复）。公告与问题报告的回复不能屏蔽。
 */
import { useCallback, useEffect, useState } from 'react'
import { collab, inbox, NOTIFY_KIND_LABEL, since, type Member, type Mute, type Notice } from '../apiCollab'
import { EmptyState, Loading, Panel } from '../components/ui'
import { pingInbox } from '../components/Inbox'
import type { PageProps } from './types'

const MUTE_LABEL: Record<string, string> = { user: '成员', group: '群组', kind: '通知类型', post: '帖子' }

export default function InboxPage({ onNavigate, notify }: PageProps) {
  const [kind, setKind] = useState('')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [items, setItems] = useState<Notice[] | null>(null)
  const [unread, setUnread] = useState<Record<string, number>>({})
  const [more, setMore] = useState(false)
  const load = useCallback(() => {
    inbox.list({ kind: kind || undefined, unread: unreadOnly }).then((r) => { setItems(r.items); setUnread(r.unread); setMore(r.items.length >= 50) }).catch((e) => notify(e.message, true))
  }, [kind, unreadOnly, notify])
  useEffect(load, [load])
  const loadMore = async () => {
    const last = items?.at(-1)?.id
    if (!last) return
    const r = await inbox.list({ kind: kind || undefined, unread: unreadOnly, before: last })
    setItems([...(items ?? []), ...r.items])
    setMore(r.items.length >= 50)
  }
  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    try { await fn(); if (ok) notify(ok); load(); pingInbox() } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <div className="inbox-page">
      <div>
        <div className="community-bar">
          <div className="tabs flat">
            <button aria-pressed={kind === ''} onClick={() => setKind('')}>全部{unread.total ? <small> {unread.total}</small> : null}</button>
            {Object.entries(NOTIFY_KIND_LABEL).map(([k, l]) => (
              <button key={k} aria-pressed={kind === k} onClick={() => setKind(k)}>{l}{unread[k] ? <small> {unread[k]}</small> : null}</button>
            ))}
          </div>
          <div className="community-tools">
            <label className="check"><input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />只看未读</label>
            <button className="button small" onClick={() => act(() => inbox.read({ kind: kind || undefined }), '已全部标为已读')}>全部已读</button>
            <button className="button small" onClick={() => act(() => inbox.clear({ read_only: true }), '已清理已读通知')}>清理已读</button>
          </div>
        </div>
        {!items ? <Loading /> : !items.length ? (
          <EmptyState title={unreadOnly ? '没有未读通知' : '还没有通知'}>有人 @ 你、给你指派任务、评论你的帖子、回复你的问题报告时，会出现在这里。</EmptyState>
        ) : (
          <div className="inbox-full stagger">
            {items.map((n) => (
              <div key={n.id} className={`inbox-row${n.read_at ? '' : ' unread'}`}>
                <span className={`inbox-ic k-${n.kind}`} aria-hidden>{n.kind === 'mention' ? '@' : n.kind === 'task' ? '☐' : n.kind === 'issue' ? '!' : n.kind === 'announce' ? '📣' : '·'}</span>
                <button type="button" className="inbox-row-main" onClick={async () => {
                  if (!n.read_at) await inbox.read({ ids: [n.id] }).catch(() => undefined)
                  pingInbox()
                  if (n.link) onNavigate(n.link); else load()
                }}>
                  <b>{n.title}</b>
                  {n.body && <span className="inbox-row-body">{n.body}</span>}
                  <small>{NOTIFY_KIND_LABEL[n.kind] ?? n.kind}{n.actor_name ? ` · ${n.actor_name}` : ''} · {since(n.created_at)}</small>
                </button>
                <div className="inbox-row-actions">
                  {!n.read_at && <button type="button" className="link-btn" onClick={() => act(() => inbox.read({ ids: [n.id] }))}>标为已读</button>}
                  {n.actor_id && n.kind !== 'announce' && n.kind !== 'issue' && (
                    <button type="button" className="link-btn" onClick={() => act(() => inbox.mute('user', n.actor_id!, n.actor_name), `已屏蔽 ${n.actor_name}`)}>屏蔽此人</button>
                  )}
                  {n.kind !== 'announce' && n.kind !== 'issue' && (
                    <button type="button" className="link-btn" onClick={() => act(() => inbox.mute('kind', n.kind, NOTIFY_KIND_LABEL[n.kind] ?? n.kind), `不再接收「${NOTIFY_KIND_LABEL[n.kind] ?? n.kind}」通知`)}>不再接收此类</button>
                  )}
                  <button type="button" className="link-btn" onClick={() => act(() => inbox.clear({ ids: [n.id] }))}>删除</button>
                </div>
              </div>
            ))}
            {more && <button type="button" className="button small" onClick={loadMore}>加载更多</button>}
          </div>
        )}
      </div>
      <aside className="community-side">
        <MuteSettings notify={notify} />
      </aside>
    </div>
  )
}

export function MuteSettings({ notify }: { notify: PageProps['notify'] }) {
  const [mutes, setMutes] = useState<Mute[] | null>(null)
  const [members, setMembers] = useState<Member[]>([])
  const [pick, setPick] = useState('')
  const load = useCallback(() => { inbox.mutes().then((r) => setMutes(r.mutes)).catch(() => setMutes([])) }, [])
  useEffect(() => { load(); collab.members().then((r) => setMembers(r.members)).catch(() => undefined) }, [load])
  const act = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); notify(ok); load() } catch (e) { notify((e as Error).message, true) } }
  const muteable = Object.entries(NOTIFY_KIND_LABEL).filter(([k]) => k !== 'announce' && k !== 'issue')
  const mutedKinds = new Set((mutes ?? []).filter((m) => m.target_type === 'kind').map((m) => m.target_id))
  return (
    <Panel title="屏蔽设置" subtitle="屏蔽某人后：他的 @、评论、点赞不再通知你，群聊里他的消息折叠显示，策略广场里不再显示他的帖子">
      <div className="hint" style={{ marginBottom: 6 }}>按类型接收通知</div>
      <div className="mute-kinds">
        {muteable.map(([k, l]) => {
          const on = !mutedKinds.has(k)
          return (
            <label key={k} className="mute-kind">
              <span>{l}</span>
              <button type="button" role="switch" aria-checked={on} aria-label={`接收「${l}」通知`} className={`switch${on ? ' on' : ''}`}
                onClick={() => act(() => (on ? inbox.mute('kind', k, l) : inbox.unmute('kind', k)), on ? `不再接收「${l}」` : `恢复接收「${l}」`)}><i /></button>
            </label>
          )
        })}
      </div>
      <div className="hint" style={{ margin: '14px 0 6px' }}>屏蔽成员</div>
      <div className="field-row">
        <select value={pick} onChange={(e) => setPick(e.target.value)} aria-label="选择成员">
          <option value="">选择成员…</option>
          {members.filter((m) => !m.muted).map((m) => <option key={m.user_id} value={m.user_id}>{m.name}</option>)}
        </select>
        <button type="button" className="button small" disabled={!pick} onClick={() => {
          const m = members.find((x) => x.user_id === pick)
          act(() => inbox.mute('user', pick, m?.name ?? ''), '已屏蔽').then(() => { setPick(''); collab.members().then((r) => setMembers(r.members)).catch(() => undefined) })
        }}>屏蔽</button>
      </div>
      <div className="hint" style={{ margin: '14px 0 6px' }}>已屏蔽</div>
      {!mutes ? <Loading /> : !mutes.filter((m) => m.target_type !== 'kind').length ? <div className="hint">没有屏蔽任何成员、群组或帖子</div> : (
        <ul className="plain-list mute-list stagger">
          {mutes.filter((m) => m.target_type !== 'kind').map((m) => (
            <li key={`${m.target_type}:${m.target_id}`}>
              <span><small className="scope-tag">{MUTE_LABEL[m.target_type]}</small> {m.label || m.target_id}</span>
              <button type="button" className="link-btn" onClick={() => act(() => inbox.unmute(m.target_type, m.target_id), '已取消屏蔽')}>取消屏蔽</button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}
