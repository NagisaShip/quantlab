/**
 * 群组：工作区内自由组建的讨论组（参照飞书群聊 + 任务）。
 *
 * 左侧：我的任务、我加入的群（未读角标、最后一条消息）、可加入的公开群、新建群组。
 * 右侧：讨论（Markdown + 引用 + @ 提及 + 回复 / 置顶 / 撤回 / 转任务）、任务看板（拖动改状态）、
 *       群内共享的策略与研究、群组模型与额度、群设置（成员、角色、可见性、解散）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { LimitRule } from '../api/research'
import {
  collab, groupProviders, inbox, PRIORITY_LABEL, since, TASK_COLUMNS,
  type DiscoverResult, type Group, type GroupRole, type GroupUsage, type Member, type Message, type Ref, type SharedItem, type Task, type TaskPriority, type TaskStatus,
} from '../apiCollab'
import { Callout, EmptyState, Loading, Modal, Tag } from '../components/ui'
import { RichBody, RichEditor } from '../components/RichText'
import { LimitBars, LimitEditor, fmtCost, fmtTokens } from '../components/modelUsage'
import { QuantBot } from '../components/QuantBot'
import { QmojiReactionBar } from '../components/QmojiReactionBar'
import { Icon } from '../components/icons'
import { UserAvatar } from '../components/UserAvatar'
import { appConfirm } from '../components/dialog'
import { pingInbox } from '../components/Inbox'
import { MemberPicker } from '../components/Social'
import { copyText, SharePublicButton } from '../components/PublicShare'
import type { Me } from '../api/platform'
import type { PageProps } from './types'

type Tab = 'chat' | 'tasks' | 'shared' | 'models' | 'settings'
const TABS: { key: Tab; label: string }[] = [
  { key: 'chat', label: '讨论' }, { key: 'tasks', label: '任务' }, { key: 'shared', label: '共享内容' },
  { key: 'models', label: '模型与额度' }, { key: 'settings', label: '设置' },
]
const ROLE_LABEL: Record<GroupRole, string> = { owner: '群主', admin: '群管理员', member: '成员' }
const POLL_MS = 4000

export default function GroupsPage({ arg, me, onNavigate, notify }: PageProps & { arg?: string; me: Me }) {
  const [gid, sub] = (arg || '').split('/')
  const [groups, setGroups] = useState<Group[] | null>(null)
  const [members, setMembers] = useState<Member[]>([])
  const [creating, setCreating] = useState(false)
  const [q, setQ] = useState('')
  const [myTasks, setMyTasks] = useState(0)
  const load = useCallback(() => {
    collab.groups().then((r) => setGroups(r.groups)).catch((e) => notify(e.message, true))
    collab.myTasks().then((r) => setMyTasks(r.tasks.length)).catch(() => undefined)
  }, [notify])
  const loadMembers = useCallback(() => {
    collab.members().then((r) => setMembers(r.members)).catch(() => undefined)
  }, [])
  useEffect(() => { load(); loadMembers() }, [load, loadMembers])
  useEffect(() => { const t = window.setInterval(load, 15000); return () => window.clearInterval(t) }, [load])

  const [dmOpen, setDmOpen] = useState(false)
  const [sideOpen, setSideOpen] = useState(false)
  const [directory, setDirectory] = useState<DiscoverResult | null>(null)
  const label = (g: Group) => (g.kind === 'dm' ? g.peer?.name ?? '私聊' : g.name)
  const hit = (g: Group) => {
    const query = q.trim().toLowerCase()
    return !query || `${label(g)} ${g.quantlab_no || ''} ${g.description || ''}`.toLowerCase().includes(query)
  }
  const dms = (groups ?? []).filter((g) => g.kind === 'dm' && hit(g))
  const mine = (groups ?? []).filter((g) => g.kind !== 'dm' && g.my_role && hit(g))
  const open = (groups ?? []).filter((g) => g.kind !== 'dm' && !g.my_role && hit(g))
  const current = groups?.find((g) => g.id === gid)
  const showTasks = gid === 'tasks'
  const startDm = async (uid: string) => {
    try {
      const r = await collab.dm(uid)
      setDmOpen(false); setSideOpen(false); load(); onNavigate(`groups/${r.group.id}`)
    } catch (e) { notify((e as Error).message, true) }
  }
  const goGroup = (id: string) => { setSideOpen(false); onNavigate(`groups/${id}`) }
  useEffect(() => {
    const query = q.trim()
    if (query.length < 2) { setDirectory(null); return }
    let alive = true
    const timer = window.setTimeout(() => {
      collab.discover(query).then((r) => { if (alive) setDirectory(r) }).catch(() => { if (alive) setDirectory(null) })
    }, 180)
    return () => { alive = false; window.clearTimeout(timer) }
  }, [q])

  return (
    <div className={`grp-layout${gid ? '' : ' grp-list-mode'}${sideOpen ? ' grp-drawer-open' : ''}`}>
      {sideOpen && <button type="button" className="grp-drawer-backdrop" aria-label="关闭群组列表" onClick={() => setSideOpen(false)} />}
      <aside className={`grp-side${sideOpen ? ' open' : ''}`} aria-label="群组列表">
        <div className="grp-side-head">
          <div><b>群组与任务</b><small>名称、QLG 群号或 QLU UID 都可搜索</small></div>
          <button type="button" className="grp-side-close" onClick={() => setSideOpen(false)} aria-label="关闭群组列表">×</button>
        </div>
        <div className="grp-side-top">
          <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索名称 / 群号 / UID" aria-label="搜索名称、QuantLab 群号或 UID" />
          <button type="button" className="button small primary" onClick={() => setCreating(true)}>＋ 群组</button>
          <button type="button" className="button small" onClick={() => setDmOpen(true)} title="和工作区里的某位成员私聊">私聊</button>
        </div>
        <button type="button" className={`grp-item tasks${showTasks ? ' on' : ''}`} onClick={() => { setSideOpen(false); onNavigate('groups/tasks') }}>
          <span className="grp-av tasks" aria-hidden>☐</span>
          <span className="grp-main"><b>我的任务</b><small>指派给我、还没完成的</small></span>
          {myTasks > 0 && <span className="grp-badge">{myTasks}</span>}
        </button>
        {!groups ? <Loading /> : (
          <>
            {dms.length > 0 && <div className="grp-label">私聊 · {dms.length}</div>}
            <div className="stagger">
              {dms.map((g) => <GroupItem key={g.id} g={g} on={g.id === gid} onClick={() => goGroup(g.id)} />)}
            </div>
            <div className="grp-label">我加入的群组 · {mine.length}</div>
            <div className="stagger">
              {mine.map((g) => <GroupItem key={g.id} g={g} on={g.id === gid} onClick={() => goGroup(g.id)} />)}
            </div>
            {!mine.length && <div className="grp-none">还没有加入群组。新建一个，或加入下面的公开群组。</div>}
            {open.length > 0 && <div className="grp-label">可加入的公开群组 · {open.length}</div>}
            <div className="stagger">
              {open.map((g) => <GroupItem key={g.id} g={g} on={g.id === gid} onClick={() => goGroup(g.id)} />)}
            </div>
            {directory && q.trim().length >= 2 && (
              <div className="grp-directory">
                <div className="grp-label">公开目录 · 名称 / 号码</div>
                {directory.users.map((u) => (
                  <button key={u.id} type="button" className="grp-directory-row" onClick={() => { setSideOpen(false); onNavigate(`profile/${u.id}`) }}>
                    <UserAvatar color={u.avatar_color} size={30} title={u.name} />
                    <span><b>{u.name}</b><small>{u.quantlab_uid} {u.username ? `· @${u.username}` : ''}</small></span>
                  </button>
                ))}
                {directory.groups.filter((g) => !groups?.some((x) => x.id === g.id)).map((g) => (
                  <button key={g.id} type="button" className="grp-directory-row" onClick={() => goGroup(g.id)}>
                    <span className="grp-av" aria-hidden>{g.name.slice(0, 1)}</span>
                    <span><b>{g.name}</b><small>{g.quantlab_no} · {g.member_count} 人</small></span>
                  </button>
                ))}
                {!directory.users.length && !directory.groups.length && <div className="grp-none">没有匹配的公开用户或群组。只有显式公开的用户会出现在目录里。</div>}
              </div>
            )}
          </>
        )}
      </aside>
      <section className="grp-main-pane">
        {showTasks ? <MyTasks onNavigate={onNavigate} notify={notify} onChanged={load} onOpenGroups={() => setSideOpen(true)} />
          : current ? <GroupView key={current.id} group={current} tab={(TABS.find((t) => t.key === sub)?.key ?? 'chat')} me={me} members={members}
            onNavigate={onNavigate} notify={notify} onChanged={load} onStartDm={startDm} onMembersChanged={loadMembers} onOpenGroups={() => setSideOpen(true)} />
            : groups && gid ? <EmptyState title="群组不存在或已解散" action={<button className="button" onClick={() => onNavigate('groups')}>返回</button>} />
              : (
                <EmptyState title="选一个群组开始讨论"
                  action={<button className="button primary" onClick={() => setCreating(true)}>新建群组</button>}>
                  群组可以自由组建：在群里讨论、@ 成员、引用回测和策略，摊派任务，共享策略，还可以给群组单独配模型与额度。
                </EmptyState>
              )}
      </section>
      {creating && <CreateGroupModal members={members} meId={me.user.id} onClose={() => setCreating(false)} notify={notify}
        onCreated={(g) => { setCreating(false); load(); onNavigate(`groups/${g.id}`) }} />}
      {dmOpen && (
        <Modal title="发起私聊" onClose={() => setDmOpen(false)} width={460}>
          <MemberPicker members={members} picked={[]} single exclude={[me.user.id]} autoFocus onChange={([uid]) => uid && startDm(uid)} />
          <p className="hint" style={{ marginBottom: 0 }}>私聊只有你们两人能看到；对方会收到通知，头像上出现红点。</p>
        </Modal>
      )}
    </div>
  )
}

function GroupItem({ g, on, onClick }: { g: Group; on: boolean; onClick: () => void }) {
  const dm = g.kind === 'dm'
  const name = dm ? g.peer?.name ?? '私聊' : g.name
  return (
    <button type="button" className={`grp-item${on ? ' on' : ''}${dm ? ' dm' : ''}`} onClick={onClick}>
      <span className={`grp-av${dm ? ' dm' : ''}`} aria-hidden>{name.slice(0, 1)}</span>
      <span className="grp-main">
        <b>{name}{!dm && g.visibility === 'private' && <span className="grp-lock" title="私密群组">🔒</span>}</b>
        <small>{!dm && g.quantlab_no ? `${g.quantlab_no} · ` : ''}{g.last_message ? `${g.last_message.author}：${g.last_message.body}` : g.description || `${g.member_count} 人`}</small>
      </span>
      <span className="grp-meta">
        {g.last_message && <em>{since(g.last_message.at)}</em>}
        {g.unread > 0 ? <span className="grp-badge">{g.unread > 99 ? '99+' : g.unread}</span> : g.open_tasks > 0 ? <span className="grp-tasks" title="进行中的任务">☐ {g.open_tasks}</span> : null}
      </span>
    </button>
  )
}

function CreateGroupModal({ members, meId, onClose, onCreated, notify }: {
  members: Member[]; meId: string; onClose: () => void; onCreated: (g: Group) => void; notify: PageProps['notify']
}) {
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [vis, setVis] = useState<'open' | 'private'>('open')
  const [picked, setPicked] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  return (
    <Modal title="新建群组" onClose={onClose} width={560}
      footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={!name.trim() || busy}
        onClick={async () => {
          setBusy(true)
          try { onCreated((await collab.createGroup({ name, description: desc, visibility: vis, members: picked })).group) } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
        }}>{busy ? '创建中…' : '创建'}</button></>}>
      <label>群组名称<input value={name} maxLength={40} autoFocus onChange={(e) => setName(e.target.value)} placeholder="例如：趋势策略小组" /></label>
      <label style={{ marginTop: 10 }}>简介（可选）<input value={desc} maxLength={500} onChange={(e) => setDesc(e.target.value)} placeholder="这个群讨论什么" /></label>
      <div className="seg" role="radiogroup" style={{ marginTop: 12 }}>
        <button type="button" role="radio" aria-selected={vis === 'open'} onClick={() => setVis('open')}>公开 · 工作区成员可发现并加入</button>
        <button type="button" role="radio" aria-selected={vis === 'private'} onClick={() => setVis('private')}>私密 · 只能邀请加入</button>
      </div>
      <div className="grp-pick">
        <div className="hint">邀请成员（可以之后再加）{picked.length > 0 && ` · 已选 ${picked.length} 人`}</div>
        <MemberPicker members={members} picked={picked} onChange={setPicked} exclude={[meId]} />
      </div>
    </Modal>
  )
}

/* ------------------------------------------------------------------ 群组视图 */
function GroupView({ group: g0, tab, me, members, onNavigate, notify, onChanged, onStartDm, onMembersChanged, onOpenGroups }: {
  group: Group; tab: Tab; me: Me; members: Member[]; onNavigate: (k: string) => void; notify: PageProps['notify']; onChanged: () => void
  onStartDm: (uid: string) => void; onMembersChanged: () => void; onOpenGroups: () => void
}) {
  const [g, setG] = useState<Group>(g0)
  const reload = useCallback(() => { collab.group(g0.id).then((r) => setG(r.group)).catch(() => undefined) }, [g0.id])
  useEffect(reload, [reload])
  useEffect(() => { if (tab === 'settings') onMembersChanged() }, [tab, onMembersChanged])
  const isMember = !!g.my_role
  const isMod = g.my_role === 'owner' || g.my_role === 'admin' || me.workspace?.role === 'admin'
  const go = (t: Tab) => onNavigate(`groups/${g.id}${t === 'chat' ? '' : `/${t}`}`)
  const dm = g.kind === 'dm'
  const peer = members.find((m) => m.user_id === g.peer?.user_id)
  const memberInfo = useMemo(() => new Map(members.map((m) => [m.user_id, m])), [members])
  const tabs = dm ? TABS.filter((t) => t.key === 'chat' || t.key === 'shared') : TABS
  return (
    <div className="grp-view">
      <header className="grp-head">
        <button type="button" className="grp-mobile-list" onClick={onOpenGroups} aria-label="打开群组列表">
          <Icon name="teams" size={16} /><span>群组</span>
        </button>
        {dm ? <UserAvatar color={peer?.avatar_color} size={42} title={g.peer?.name} /> : <span className="grp-av big" aria-hidden>{g.name.slice(0, 1)}</span>}
        <div className="grp-title">
          {dm ? (
            <>
              <h2>{g.peer?.name} <Tag>私聊</Tag></h2>
              <small>{peer?.username ? `@${peer.username} · ` : ''}{peer?.online ? '在线' : '离线'} · 只有你们两人能看到{g.dm_can_send === false && g.dm_reason ? ` · ${g.dm_reason}` : ''}</small>
            </>
          ) : (
            <>
              <h2><span className="grp-title-name">{g.name}</span> {g.visibility === 'private' ? <Tag>私密</Tag> : <Tag>公开</Tag>}</h2>
              <small className="grp-title-meta">
                <button type="button" className="grp-number-copy" title="复制 QuantLab 群号" onClick={() => { copyText(g.quantlab_no).then(() => notify('群号已复制')).catch(() => undefined) }}>{g.quantlab_no || 'QLG-—'}</button>
                <span>{g.description || '没有简介'} · {g.member_count} 名成员{g.my_role ? ` · 我是${ROLE_LABEL[g.my_role]}` : ''}</span>
              </small>
            </>
          )}
        </div>
        {!dm && (
          <div className="grp-avatars" title={(g.members ?? []).map((m) => m.name).join('、')}>
            {(g.members ?? []).slice(0, 6).map((m) => {
              const info = memberInfo.get(m.user_id)
              if (m.user_id === me.user.id) return <span key={m.user_id} className="grp-member-self" title={`${m.name}（我）`}><UserAvatar color={info?.avatar_color || m.avatar_color} size={28} title={m.name} /></span>
              return (
                <button key={m.user_id} type="button" className={`grp-member-av${info?.online ? ' online' : ''}`}
                  title={`与 ${m.name} 私聊`} aria-label={`与 ${m.name} 私聊`} onClick={() => onStartDm(m.user_id)}>
                  <UserAvatar color={info?.avatar_color || m.avatar_color} size={28} title={m.name} />
                </button>
              )
            })}
            {(g.members?.length ?? 0) > 6 && <span className="ed-more">+{(g.members?.length ?? 0) - 6}</span>}
          </div>
        )}
        {!isMember && g.visibility === 'open' && (
          <button type="button" className="button primary small" onClick={async () => {
            try { await collab.join(g.id); notify('已加入'); reload(); onChanged() } catch (e) { notify((e as Error).message, true) }
          }}>加入群组</button>
        )}
        {dm
          ? peer && <MemberMuteButton member={peer} notify={notify} onChanged={onMembersChanged} />
          : <div className="grp-head-actions">
              {tab !== 'settings' && <MuteGroupButton g={g} notify={notify} />}
              <SharePublicButton type="group" id={g.id} title={g.name} notify={notify} compact />
            </div>}
      </header>
      <nav className="grp-tabs tabs flat" aria-label="群组">
        {tabs.map((t) => <button key={t.key} type="button" aria-pressed={tab === t.key} onClick={() => go(t.key)}>{t.label}{t.key === 'tasks' && g.open_tasks > 0 && <small> {g.open_tasks}</small>}</button>)}
      </nav>
      <div className="grp-body page-enter" key={tab}>
        {tab === 'chat' && <Chat g={g} me={me} members={members} isMember={isMember} isMod={isMod} notify={notify} onChanged={() => { reload(); onChanged() }} onStartDm={onStartDm} />}
        {tab === 'tasks' && <TaskBoard g={g} me={me} isMember={isMember} isMod={isMod} notify={notify} />}
        {tab === 'shared' && <Shared g={g} onNavigate={onNavigate} />}
        {tab === 'models' && <GroupModels g={g} isWsAdmin={me.workspace?.role === 'admin'} notify={notify} onSaved={reload} />}
        {tab === 'settings' && <GroupSettings g={g} me={me} isMod={isMod} members={members} notify={notify} onStartDm={onStartDm}
          onMembersChanged={onMembersChanged} onSaved={() => { reload(); onChanged() }} onLeft={() => { onChanged(); onNavigate('groups') }} />}
      </div>
    </div>
  )
}

function MuteGroupButton({ g, notify, wide = false }: { g: Group; notify: PageProps['notify']; wide?: boolean }) {
  const [muted, setMuted] = useState<boolean | null>(null)
  useEffect(() => { inbox.mutes().then((r) => setMuted(r.mutes.some((m) => m.target_type === 'group' && m.target_id === g.id))).catch(() => undefined) }, [g.id])
  if (!g.my_role || muted === null) return null
  return (
    <button type="button" className={`button small grp-notify-btn${muted ? ' on' : ''}${wide ? ' wide' : ''}`}
      aria-pressed={muted} title="屏蔽后不再收到这个群的 @ 与群通知（任务指派照常通知）"
      onClick={async () => {
        try {
          if (muted) await inbox.unmute('group', g.id); else await inbox.mute('group', g.id, g.name)
          setMuted(!muted)
          notify(muted ? '已恢复群通知' : '已屏蔽群通知')
          pingInbox()
        } catch (e) { notify((e as Error).message, true) }
      }}>
      <Icon name={muted ? 'bell-off' : 'bell'} size={14} />
      <span>{muted ? '已屏蔽通知' : '群通知'}</span>
    </button>
  )
}

function MemberMuteButton({ member, notify, onChanged, compact = false }: {
  member: Pick<Member, 'user_id' | 'name' | 'muted'>; notify: PageProps['notify']; onChanged: () => void; compact?: boolean
}) {
  const [muted, setMuted] = useState<boolean>(!!member.muted)
  useEffect(() => { setMuted(!!member.muted) }, [member.user_id, member.muted])
  return (
    <button type="button" className={`button small grp-user-mute${muted ? ' on' : ''}${compact ? ' compact' : ''}`}
      aria-pressed={muted} title={muted ? `取消屏蔽 ${member.name}` : `屏蔽 ${member.name} 的 @ 与私聊提醒`}
      onClick={async () => {
        try {
          if (muted) await inbox.unmute('user', member.user_id)
          else await inbox.mute('user', member.user_id, member.name)
          setMuted(!muted)
          notify(muted ? `已取消屏蔽 ${member.name}` : `已屏蔽 ${member.name}`)
          pingInbox()
          onChanged()
        } catch (e) { notify((e as Error).message, true) }
      }}>
      <Icon name={muted ? 'bell-off' : 'bell'} size={14} />
      <span>{muted ? (compact ? '已屏蔽' : '取消屏蔽') : (compact ? '屏蔽' : '屏蔽提醒')}</span>
    </button>
  )
}

/* ------------------------------------------------------------------ 讨论 */
function Chat({ g, me, members, isMember, isMod, notify, onChanged, onStartDm }: {
  g: Group; me: Me; members: Member[]; isMember: boolean; isMod: boolean; notify: PageProps['notify']; onChanged: () => void; onStartDm: (uid: string) => void
}) {
  const adminIds = useMemo(() => new Set(members.filter((m) => m.role === 'admin').map((m) => m.user_id)), [members])
  const [msgs, setMsgs] = useState<Message[] | null>(null)
  const [pinned, setPinned] = useState<Message[]>([])
  const [text, setText] = useState('')
  const [refs, setRefs] = useState<Ref[]>([])
  const [reply, setReply] = useState<Message | null>(null)
  const [sending, setSending] = useState(false)
  const [showMuted, setShowMuted] = useState<Set<number>>(new Set())
  const [older, setOlder] = useState(true)
  const [taskFrom, setTaskFrom] = useState<Message | null>(null)
  const listRef = useRef<HTMLDivElement>(null)
  const lastId = useRef(0)
  const stick = useRef(true)

  const scrollEnd = () => requestAnimationFrame(() => { const el = listRef.current; if (el && stick.current) el.scrollTop = el.scrollHeight })
  const markRead = useCallback((id: number) => {
    if (!isMember || !id) return
    collab.markRead(g.id, id).then(() => { onChanged(); pingInbox() }).catch(() => undefined)
  }, [g.id, isMember, onChanged])

  useEffect(() => {
    let alive = true
    collab.messages(g.id).then((r) => {
      if (!alive) return
      setMsgs(r.messages)
      setPinned(r.pinned ?? [])
      setOlder(r.messages.length >= 60)
      lastId.current = r.messages.at(-1)?.id ?? 0
      markRead(lastId.current)
      scrollEnd()
    }).catch((e) => notify(e.message, true))
    const t = window.setInterval(() => {
      if (document.visibilityState === 'hidden') return
      collab.messages(g.id, { after: lastId.current }).then((r) => {
        if (!alive || !r.messages.length) return
        setMsgs((prev) => {
          const map = new Map((prev ?? []).map((m) => [m.id, m]))
          for (const m of r.messages) map.set(m.id, m)
          return [...map.values()].sort((a, b) => a.id - b.id)
        })
        lastId.current = r.messages.at(-1)!.id
        markRead(lastId.current)
        scrollEnd()
      }).catch(() => undefined)
    }, POLL_MS)
    return () => { alive = false; window.clearInterval(t) }
  }, [g.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const mentions = useMemo(() => [
    { id: 'all', name: '所有人', hint: '通知全部群成员' },
    ...(g.members ?? []).filter((m) => m.user_id !== me.user.id).map((m) => ({ id: m.user_id, name: m.name, hint: ROLE_LABEL[m.role] })),
  ], [g.members, me.user.id])

  const send = async () => {
    if ((!text.trim() && !refs.length) || sending) return
    setSending(true)
    try {
      const r = await collab.send(g.id, text, refs, reply?.id)
      setMsgs((prev) => [...(prev ?? []).filter((m) => m.id !== r.message.id), r.message])
      lastId.current = Math.max(lastId.current, r.message.id)
      setText(''); setRefs([]); setReply(null)
      try { localStorage.removeItem(`qa.draft.group.${g.id}`) } catch { /* */ }
      stick.current = true
      scrollEnd()
      onChanged()
    } catch (e) { notify((e as Error).message, true) } finally { setSending(false) }
  }
  const loadOlder = async () => {
    const first = msgs?.[0]?.id
    if (!first) return
    const el = listRef.current
    const h = el?.scrollHeight ?? 0
    const r = await collab.messages(g.id, { before: first })
    setOlder(r.messages.length >= 60)
    setMsgs((prev) => [...r.messages, ...(prev ?? [])])
    requestAnimationFrame(() => { if (el) el.scrollTop = el.scrollHeight - h })
  }
  const act = async (fn: () => Promise<unknown>, ok?: string) => {
    try { await fn(); if (ok) notify(ok); const r = await collab.messages(g.id); setMsgs(r.messages); setPinned(r.pinned ?? []) } catch (e) { notify((e as Error).message, true) }
  }
  const reactQmoji = async (mid: number, emoji: string) => {
    try {
      const r = await collab.qmojiMessage(mid, emoji)
      setMsgs((prev) => (prev ?? []).map((x) => (x.id === mid ? { ...x, reactions: r.reactions } : x)))
    } catch (e) { notify((e as Error).message, true) }
  }
  const byId = useMemo(() => new Map((msgs ?? []).map((m) => [m.id, m])), [msgs])

  if (!msgs) return <Loading />
  let lastDay = ''
  let lastAuthor = ''
  let lastAt = 0
  return (
    <div className="chat">
      {pinned.length > 0 && (
        <div className="chat-pinned anim-expand">
          <span className="chat-pin-tag">置顶</span>
          <span className="chat-pin-text">{pinned[0].author_name}：{pinned[0].body.slice(0, 80) || '（引用）'}</span>
          {pinned.length > 1 && <small>等 {pinned.length} 条</small>}
        </div>
      )}
      <div className="chat-list" ref={listRef} onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }}>
        {older && msgs.length >= 60 && <button type="button" className="link-btn chat-older" onClick={loadOlder}>加载更早的消息</button>}
        {!msgs.length && <div className="chat-empty"><QuantBot size={34} state="idle" motion="low" /><p>还没有消息。说点什么，或者引用一份回测开始讨论。</p></div>}
        {msgs.map((m) => {
          const day = new Date(m.created_at).toLocaleDateString()
          const sep = day !== lastDay
          lastDay = day
          if (m.author_kind === 'system') {
            lastAuthor = ''
            return (
              <div key={m.id}>
                {sep && <div className="chat-day"><span>{day}</span></div>}
                <div className="chat-sys anim-in">{m.body} · {new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</div>
              </div>
            )
          }
          const cont = !sep && lastAuthor === m.author_id + m.author_kind && m.created_at - lastAt < 5 * 60_000 && !m.reply_to
          lastAuthor = m.author_id + m.author_kind
          lastAt = m.created_at
          const mineMsg = m.author_id === me.user.id && m.author_kind === 'user'
          const parent = m.reply_to ? byId.get(m.reply_to) : null
          const hidden = m.muted && !showMuted.has(m.id)
          return (
            <div key={m.id}>
              {sep && <div className="chat-day"><span>{day}</span></div>}
              <div className={`chat-msg${cont ? ' cont' : ''}${m.author_kind === 'agent' ? ' agent' : ''}${m.pinned ? ' pinned' : ''} anim-in`}>
                <span className="chat-av">{cont ? '' : m.author_kind === 'agent' ? <QuantBot size={26} state="idle" motion="low" /> : mineMsg
                  ? <UserAvatar color={g.members?.find((x) => x.user_id === m.author_id)?.avatar_color} size={30} title={m.author_name} />
                  : <button type="button" className="chat-av-btn" title={`与 ${m.author_name} 私聊`} aria-label={`与 ${m.author_name} 私聊`} onClick={() => onStartDm(m.author_id)}><UserAvatar color={g.members?.find((x) => x.user_id === m.author_id)?.avatar_color} size={30} title={m.author_name} /></button>}</span>
                <div className="chat-content">
                  {!cont && (
                    <div className="chat-meta">
                      <b>{m.author_name}</b>
                      {m.author_kind === 'agent' && <span className="src-badge agent">Agent</span>}
                      <time>{new Date(m.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>
                      {m.edited_at && <small>（已编辑）</small>}
                      {m.pinned && <span className="chat-pin-tag small">置顶</span>}
                    </div>
                  )}
                  {parent && <div className="chat-quote">回复 {parent.author_name}：{parent.deleted ? '（已撤回）' : parent.body.slice(0, 80)}</div>}
                  {m.deleted ? <div className="chat-deleted">消息已撤回</div>
                    : hidden ? <button type="button" className="chat-muted" onClick={() => setShowMuted(new Set([...showMuted, m.id]))}><Icon name="bell-off" size={13} />已屏蔽 {m.author_name} 的消息 · 点击显示</button>
                      : <RichBody text={m.body} refs={m.refs} compact />}
                  {!m.deleted && isMember && (
                    <div className="chat-actions">
                      <button type="button" onClick={() => setReply(m)}>回复</button>
                      <button type="button" onClick={() => setTaskFrom(m)}>转为任务</button>
                      {!mineMsg && m.author_kind === 'user' && !adminIds.has(m.author_id) && (
                        <button type="button" onClick={() => act(
                          async () => { if (m.muted) await inbox.unmute('user', m.author_id); else await inbox.mute('user', m.author_id, m.author_name); pingInbox() },
                          m.muted ? `已取消屏蔽 ${m.author_name}` : `已屏蔽 ${m.author_name}`,
                        )}>
                          <Icon name={m.muted ? 'bell' : 'bell-off'} size={13} />{m.muted ? '取消屏蔽' : '屏蔽'}
                        </button>
                      )}
                      {isMod && <button type="button" onClick={() => act(() => collab.pinMessage(m.id), m.pinned ? '已取消置顶' : '已置顶')}>{m.pinned ? '取消置顶' : '置顶'}</button>}
                      {(mineMsg || isMod) && <button type="button" className="danger" onClick={async () => { if (await appConfirm('撤回这条消息？')) act(() => collab.deleteMessage(m.id), '已撤回') }}>撤回</button>}
                    </div>
                  )}
                  {!m.deleted && isMember && (
                    <div className="chat-qmoji-reactions">
                      <QmojiReactionBar compact reactions={m.reactions ?? []} onToggle={(emoji) => reactQmoji(m.id, emoji)} />
                    </div>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
      {isMember ? (g.kind === 'dm' && g.dm_can_send === false ? (
        <Callout tone="warn" title="暂时不能继续发送">{g.dm_reason || '对方尚未回复，或已关闭私信。等待对方回复后再继续。'}</Callout>
      ) : (
        <div className="chat-composer">
          {reply && <div className="chat-replybar anim-in">回复 <b>{reply.author_name}</b>：{reply.body.slice(0, 60)} <button type="button" className="link-btn" onClick={() => setReply(null)}>取消</button></div>}
          <RichEditor compact rows={3} value={text} onChange={setText} refs={refs} onRefs={setRefs} onSubmit={send} mentions={mentions}
            draftKey={`group.${g.id}`} placeholder={`发消息到「${g.name}」… 输入 @ 提及成员`} notify={notify}
            footer={<button type="button" className="button small primary" disabled={sending || (!text.trim() && !refs.length)} onClick={send}>{sending ? '发送中…' : '发送'}</button>} />
        </div>
      )) : <Callout>加入群组后可以发消息。</Callout>}
      {taskFrom && <TaskModal g={g} me={me} isMod={isMod} preset={{ title: taskFrom.body.split('\n')[0].slice(0, 60), description: `来自 ${taskFrom.author_name} 的消息：\n\n${taskFrom.body}`, refs: taskFrom.refs }}
        onClose={() => setTaskFrom(null)} notify={notify} onSaved={() => { setTaskFrom(null); notify('已创建任务') }} />}
    </div>
  )
}

/* ------------------------------------------------------------------ 任务 */
const PRIO_TONE: Record<TaskPriority, string> = { low: '', normal: '', high: 'warn', urgent: 'fail' }

function TaskBoard({ g, me, isMember, isMod, notify }: { g: Group; me: Me; isMember: boolean; isMod: boolean; notify: PageProps['notify'] }) {
  const [tasks, setTasks] = useState<Task[] | null>(null)
  const [filter, setFilter] = useState<'all' | 'mine' | 'created'>('all')
  const [edit, setEdit] = useState<Task | 'new' | null>(null)
  const [dragId, setDragId] = useState<string | null>(null)
  const [over, setOver] = useState<TaskStatus | null>(null)
  const [showCancelled, setShowCancelled] = useState(false)
  const load = useCallback(() => { collab.tasks(g.id).then((r) => setTasks(r.tasks)).catch((e) => notify(e.message, true)) }, [g.id, notify])
  useEffect(load, [load])
  const move = async (t: Task, status: TaskStatus) => {
    if (t.status === status) return
    setTasks((prev) => prev?.map((x) => (x.id === t.id ? { ...x, status } : x)) ?? null)
    try { await collab.updateTask(t.id, { status }); load() } catch (e) { notify((e as Error).message, true); load() }
  }
  if (!tasks) return <Loading />
  const shown = tasks.filter((t) => filter === 'all' || (filter === 'mine' ? t.assignee_id === me.user.id : t.created_by === me.user.id))
  const cancelled = shown.filter((t) => t.status === 'cancelled')
  return (
    <div className="board-wrap">
      <div className="board-bar">
        <div className="seg seg-sm" role="tablist">
          {([['all', '全部'], ['mine', '我负责的'], ['created', '我创建的']] as const).map(([k, l]) => (
            <button key={k} type="button" role="tab" aria-selected={filter === k} onClick={() => setFilter(k)}>{l}</button>
          ))}
        </div>
        <span className="hint">拖动卡片改状态；指派、改状态都会通知相关成员</span>
        <span className="spacer" />
        {isMember && <button type="button" className="button small primary" onClick={() => setEdit('new')}>＋ 新任务</button>}
      </div>
      <div className="board">
        {TASK_COLUMNS.map((c) => {
          const col = shown.filter((t) => t.status === c.key)
          return (
            <div key={c.key} className={`board-col${over === c.key ? ' over' : ''}`}
              onDragOver={(e) => { if (dragId) { e.preventDefault(); setOver(c.key) } }}
              onDragLeave={() => setOver(null)}
              onDrop={(e) => { e.preventDefault(); setOver(null); const t = tasks.find((x) => x.id === dragId); if (t) move(t, c.key); setDragId(null) }}>
              <div className="board-col-head"><b>{c.label}</b><span>{col.length}</span></div>
              <div className="board-cards stagger">
                {col.map((t) => (
                  <article key={t.id} className={`task-card${dragId === t.id ? ' dragging' : ''}${t.overdue ? ' overdue' : ''}`} draggable={isMember}
                    onDragStart={() => setDragId(t.id)} onDragEnd={() => { setDragId(null); setOver(null) }} onClick={() => setEdit(t)}>
                    <div className="task-top">
                      {t.priority !== 'normal' && <Tag tone={PRIO_TONE[t.priority]}>{PRIORITY_LABEL[t.priority]}</Tag>}
                      <b>{t.title}</b>
                    </div>
                    <div className="task-meta">
                      {t.assignee_name ? <span className="task-who"><span className="ed-av">{t.assignee_name.slice(0, 1)}</span>{t.assignee_name}</span> : <span className="muted">未指派</span>}
                      {t.due_at && <span className={t.overdue ? 'task-due over' : 'task-due'}>{t.overdue ? '已逾期 · ' : ''}{new Date(t.due_at).toLocaleDateString()}</span>}
                      {t.refs.length > 0 && <span className="muted">⎘ {t.refs.length}</span>}
                    </div>
                  </article>
                ))}
                {!col.length && <div className="board-empty">{c.key === 'todo' ? '没有待办' : '—'}</div>}
              </div>
            </div>
          )
        })}
      </div>
      {cancelled.length > 0 && (
        <button type="button" className="link-btn" style={{ marginTop: 10 }} onClick={() => setShowCancelled(!showCancelled)}>{showCancelled ? '收起' : `已取消的任务（${cancelled.length}）`}</button>
      )}
      {showCancelled && <ul className="plain-list anim-expand">{cancelled.map((t) => <li key={t.id}><button type="button" className="link-btn" onClick={() => setEdit(t)}>{t.title}</button><small>{t.assignee_name}</small></li>)}</ul>}
      {edit && <TaskModal g={g} me={me} isMod={isMod} task={edit === 'new' ? undefined : edit} onClose={() => setEdit(null)} notify={notify}
        onSaved={() => { setEdit(null); load() }} />}
    </div>
  )
}

function TaskModal({ g, me, isMod, task, preset, onClose, onSaved, notify }: {
  g: Group; me: Me; isMod: boolean; task?: Task; preset?: { title: string; description: string; refs: Ref[] }
  onClose: () => void; onSaved: () => void; notify: PageProps['notify']
}) {
  const [title, setTitle] = useState(task?.title ?? preset?.title ?? '')
  const [desc, setDesc] = useState(task?.description ?? preset?.description ?? '')
  const [refs, setRefs] = useState<Ref[]>(task?.refs ?? preset?.refs ?? [])
  const [assignee, setAssignee] = useState(task?.assignee_id ?? '')
  const [prio, setPrio] = useState<TaskPriority>(task?.priority ?? 'normal')
  const [status, setStatus] = useState<TaskStatus>(task?.status ?? 'todo')
  const [due, setDue] = useState(task?.due_at ? new Date(task.due_at).toISOString().slice(0, 10) : '')
  const [busy, setBusy] = useState(false)
  const canEdit = !task || isMod || [task.created_by, task.assignee_id].includes(me.user.id)
  const save = async () => {
    setBusy(true)
    const dueAt = due ? new Date(`${due}T23:59:00`).getTime() : null
    try {
      if (task) await collab.updateTask(task.id, { title, description: desc, assignee_id: assignee || null, priority: prio, status, due_at: dueAt, refs })
      else await collab.createTask(g.id, { title, description: desc, assignee_id: assignee || null, priority: prio, due_at: dueAt, refs })
      onSaved()
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  return (
    <Modal title={task ? '任务' : '新任务'} onClose={onClose} width={760}
      footer={<>
        {task && canEdit && (task.created_by === me.user.id || isMod) && <button className="button danger" onClick={async () => {
          if (await appConfirm('删除这个任务？')) { try { await collab.deleteTask(task.id); onSaved() } catch (e) { notify((e as Error).message, true) } }
        }}>删除</button>}
        <span className="spacer" />
        <button className="button" onClick={onClose}>{canEdit ? '取消' : '关闭'}</button>
        {canEdit && <button className="button primary" disabled={busy || !title.trim()} onClick={save}>{busy ? '保存中…' : task ? '保存' : '创建并指派'}</button>}
      </>}>
      {task && <p className="hint" style={{ marginTop: 0 }}>{task.creator_name} 创建于 {since(task.created_at)}{task.done_at ? ` · 完成于 ${since(task.done_at)}` : ''}</p>}
      <label>标题<input value={title} maxLength={120} disabled={!canEdit} onChange={(e) => setTitle(e.target.value)} placeholder="一句话说清楚要做什么" autoFocus={!task} /></label>
      <div className="form-grid four" style={{ marginTop: 10 }}>
        <label>负责人<select value={assignee} disabled={!canEdit} onChange={(e) => setAssignee(e.target.value)}>
          <option value="">未指派</option>
          {(g.members ?? []).map((m) => <option key={m.user_id} value={m.user_id}>{m.name}{m.user_id === me.user.id ? '（我）' : ''}</option>)}
        </select></label>
        <label>优先级<select value={prio} disabled={!canEdit} onChange={(e) => setPrio(e.target.value as TaskPriority)}>
          {(Object.keys(PRIORITY_LABEL) as TaskPriority[]).map((p) => <option key={p} value={p}>{PRIORITY_LABEL[p]}</option>)}
        </select></label>
        <label>截止日期<input type="date" value={due} disabled={!canEdit} onChange={(e) => setDue(e.target.value)} /></label>
        {task && <label>状态<select value={status} disabled={!canEdit} onChange={(e) => setStatus(e.target.value as TaskStatus)}>
          {[...TASK_COLUMNS, { key: 'cancelled' as TaskStatus, label: '已取消' }].map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
        </select></label>}
      </div>
      <div style={{ marginTop: 10 }}>
        {canEdit
          ? <RichEditor value={desc} onChange={setDesc} refs={refs} onRefs={setRefs} rows={8} placeholder="背景、验收标准、相关回测与策略（可以引用）" notify={notify} />
          : <RichBody text={desc} refs={refs} />}
      </div>
    </Modal>
  )
}

function MyTasks({ onNavigate, notify, onChanged, onOpenGroups }: { onNavigate: (k: string) => void; notify: PageProps['notify']; onChanged: () => void; onOpenGroups: () => void }) {
  const [tasks, setTasks] = useState<Task[] | null>(null)
  const [closed, setClosed] = useState(false)
  const load = useCallback(() => { collab.myTasks(closed).then((r) => setTasks(r.tasks)).catch((e) => notify(e.message, true)) }, [closed, notify])
  useEffect(load, [load])
  return (
    <div className="grp-view">
      <header className="grp-head">
        <button type="button" className="grp-mobile-list" onClick={onOpenGroups} aria-label="打开群组列表">
          <Icon name="teams" size={16} /><span>群组</span>
        </button>
        <span className="grp-av big tasks" aria-hidden>☐</span>
        <div className="grp-title"><h2>我的任务</h2><small>所有群组里指派给我的任务，按优先级和截止时间排序</small></div>
        <label className="check"><input type="checkbox" checked={closed} onChange={(e) => setClosed(e.target.checked)} />显示已完成</label>
      </header>
      <div className="grp-body">
        {!tasks ? <Loading /> : !tasks.length ? <EmptyState title="没有指派给你的任务">群组里有人给你指派任务时，会出现在这里，并进通知中心。</EmptyState> : (
          <div className="my-tasks stagger">
            {tasks.map((t) => (
              <div key={t.id} className={`my-task${t.overdue ? ' overdue' : ''}`}>
                <select value={t.status} aria-label="状态" onChange={async (e) => {
                  try { await collab.updateTask(t.id, { status: e.target.value as TaskStatus }); load(); onChanged() } catch (err) { notify((err as Error).message, true) }
                }}>
                  {[...TASK_COLUMNS, { key: 'cancelled' as TaskStatus, label: '已取消' }].map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
                </select>
                <div className="my-task-main">
                  <b>{t.priority !== 'normal' && <Tag tone={PRIO_TONE[t.priority]}>{PRIORITY_LABEL[t.priority]}</Tag>} {t.title}</b>
                  <small>{t.group_name} · {t.creator_name} 指派 · {since(t.created_at)}{t.due_at ? ` · 截止 ${new Date(t.due_at).toLocaleDateString()}` : ''}{t.overdue ? ' · 已逾期' : ''}</small>
                </div>
                <button type="button" className="button small" onClick={() => onNavigate(`groups/${t.group_id}/tasks`)}>打开群组</button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ 共享内容 */
function Shared({ g, onNavigate }: { g: Group; onNavigate: (k: string) => void }) {
  const [data, setData] = useState<{ strategies: SharedItem[]; projects: SharedItem[] } | null>(null)
  useEffect(() => { collab.shared(g.id).then(setData).catch(() => setData({ strategies: [], projects: [] })) }, [g.id])
  if (!data) return <Loading />
  if (!data.strategies.length && !data.projects.length) {
    return <EmptyState title="还没有在群内共享的内容">在策略编辑器或研究项目里，把可见性设为「指定群组内公开」并选这个群组，就会出现在这里。工作区内公开的内容所有人都能看到，不在此列。</EmptyState>
  }
  const row = (x: SharedItem, link: string) => (
    <li key={x.id}><a href={`#/${link}`} onClick={(e) => { e.preventDefault(); onNavigate(link) }}>{x.title}</a><small>{x.owner_name} 共享 · {since(x.updated_at)}</small></li>
  )
  return (
    <div className="shared-grid">
      <section><h3 className="section-title">策略 · {data.strategies.length}</h3><ul className="plain-list stagger">{data.strategies.map((x) => row(x, `editor/${x.id}`))}</ul></section>
      <section><h3 className="section-title">研究项目 · {data.projects.length}</h3><ul className="plain-list stagger">{data.projects.map((x) => row(x, `lab/${x.id}`))}</ul></section>
    </div>
  )
}

/* ------------------------------------------------------------------ 模型与额度 */
function GroupModels({ g, isWsAdmin, notify, onSaved }: { g: Group; isWsAdmin: boolean; notify: PageProps['notify']; onSaved: () => void }) {
  const [u, setU] = useState<GroupUsage | null>(null)
  const [memberLimits, setMemberLimits] = useState<LimitRule[]>([])
  const [wsLimits, setWsLimits] = useState<LimitRule[]>([])
  const [adding, setAdding] = useState(false)
  const load = useCallback(() => {
    collab.usage(g.id).then((r) => { setU(r); setMemberLimits(r.member_limits); setWsLimits(r.ws_limits) }).catch((e) => notify(e.message, true))
  }, [g.id, notify])
  useEffect(load, [load])
  if (!u) return <Loading />
  const clean = (r: LimitRule[]) => r.filter((x) => Number(x.limit) > 0)
  return (
    <div className="grp-models">
      <section className="set-section">
        <h3 className="section-title">群组模型服务商</h3>
        <p className="hint">群主 / 群管理员登记的服务商只给本群成员使用，密钥属于登记人，用量单独记在群组名下。成员在 AI 研究室的模型菜单里就能选到。</p>
        {u.providers.length ? (
          <div className="prov-list">
            {u.providers.map((p) => (
              <div key={p.name} className="prov-card">
                <div className="prov-editor-top"><b>{p.label || p.name}</b><span className="scope-tag">群组</span><code className="muted">{p.key_masked || '未配置密钥'}</code>
                  {u.can_manage && <button type="button" className="button small danger" onClick={async () => {
                    if (!(await appConfirm(`删除群组服务商「${p.name}」？`))) return
                    try { await groupProviders.remove(g.id, p.name); load() } catch (e) { notify((e as Error).message, true) }
                  }}>删除</button>}
                </div>
                <small className="muted">{p.base_url} · {p.models.join('、')}</small>
                <LimitBars items={p.limit_status} compact />
              </div>
            ))}
          </div>
        ) : <div className="hint">还没有群组服务商{u.can_manage ? '' : '（群主或群管理员可以添加）'}。</div>}
        {u.can_manage && (adding ? <GroupProviderForm groupId={g.id} notify={notify} onDone={() => { setAdding(false); load() }} onCancel={() => setAdding(false)} />
          : <button type="button" className="button small" style={{ marginTop: 10 }} onClick={() => setAdding(true)}>＋ 添加群组服务商</button>)}
      </section>

      <section className="set-section">
        <h3 className="section-title">每位成员使用群组模型的限额</h3>
        <p className="hint">群主设置，对每位群成员分别计算；支持每日 / 每周 / 每月 / 累计与滚动时间段，按 token 或费用。</p>
        <LimitEditor rules={memberLimits} onChange={setMemberLimits} disabled={!u.can_manage} />
        {u.can_manage && <button type="button" className="button small primary" style={{ marginTop: 8 }} onClick={async () => {
          try { await collab.updateGroup(g.id, { member_limits: clean(memberLimits) }); notify('已保存'); load(); onSaved() } catch (e) { notify((e as Error).message, true) }
        }}>保存成员限额</button>}
      </section>

      <section className="set-section">
        <h3 className="section-title">工作区共享模型分给本群的额度</h3>
        <p className="hint">工作区管理员设置：全体群成员使用工作区共享模型的用量合计不超过这里的额度（与每个人的成员限额同时生效）。</p>
        <LimitBars items={u.ws_limit_status} />
        <LimitEditor rules={wsLimits} onChange={setWsLimits} disabled={!isWsAdmin} empty="未分配（只按个人成员限额）" />
        {isWsAdmin && <button type="button" className="button small primary" style={{ marginTop: 8 }} onClick={async () => {
          try { await collab.setGroupQuota(g.id, clean(wsLimits)); notify('已保存'); load(); onSaved() } catch (e) { notify((e as Error).message, true) }
        }}>保存群组额度</button>}
      </section>

      <section className="set-section">
        <h3 className="section-title">近 30 天成员用量</h3>
        <div className="table-wrap">
          <table>
            <thead><tr><th>成员</th><th className="right">群组模型</th><th className="right">费用</th><th className="right">工作区共享模型</th><th className="right">费用</th><th>群组模型限额</th></tr></thead>
            <tbody className="stagger">
              {u.members.map((m) => (
                <tr key={m.user_id}>
                  <td>{m.name}</td>
                  <td className="right mono">{fmtTokens(m.group_tokens)}</td><td className="right mono">{fmtCost(m.group_cost)}</td>
                  <td className="right mono">{fmtTokens(m.ws_tokens)}</td><td className="right mono">{fmtCost(m.ws_cost)}</td>
                  <td>{m.member_limit_status.some((s) => s.exceeded) ? <Tag tone="fail">已超额</Tag> : m.member_limit_status.length ? <Tag tone="ok">正常</Tag> : <span className="muted">不限</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}

function GroupProviderForm({ groupId, notify, onDone, onCancel }: { groupId: string; notify: PageProps['notify']; onDone: () => void; onCancel: () => void }) {
  const [f, setF] = useState({ name: '', label: '', type: 'openai_compatible', base_url: 'https://api.deepseek.com/v1', api_key: '', models: 'deepseek-chat' })
  const [busy, setBusy] = useState(false)
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value })
  return (
    <div className="grp-prov-form anim-expand">
      <div className="form-grid">
        <label>服务商 ID<input value={f.name} onChange={set('name')} placeholder="小写字母，例如 grp-deepseek" /></label>
        <label>显示名<input value={f.label} onChange={set('label')} placeholder="例如 小组 DeepSeek" /></label>
        <label>接口类型<select value={f.type} onChange={set('type')}>
          <option value="openai_compatible">OpenAI 兼容</option><option value="anthropic">Anthropic</option><option value="openai_responses">OpenAI Responses</option>
        </select></label>
        <label>API 地址<input value={f.base_url} onChange={set('base_url')} /></label>
        <label>API 密钥<input type="password" autoComplete="off" value={f.api_key} onChange={set('api_key')} placeholder="加密保存，只回显脱敏值" /></label>
        <label>模型（逗号分隔）<input value={f.models} onChange={set('models')} /></label>
      </div>
      <div className="button-row" style={{ marginTop: 10 }}>
        <button type="button" className="button small" onClick={onCancel}>取消</button>
        <button type="button" className="button small primary" disabled={busy || !f.name || !f.base_url} onClick={async () => {
          setBusy(true)
          try {
            await groupProviders.save(groupId, { ...f, models: f.models.split(/[,，\s]+/).filter(Boolean) })
            notify('已添加群组服务商')
            onDone()
          } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
        }}>保存</button>
      </div>
      <p className="hint">价格与服务商级限额可以之后在「设置 · 模型」里补充。</p>
    </div>
  )
}

/* ------------------------------------------------------------------ 设置 */
function GroupSettings({ g, me, isMod, members, notify, onSaved, onLeft, onStartDm, onMembersChanged }: {
  g: Group; me: Me; isMod: boolean; members: Member[]; notify: PageProps['notify']; onSaved: () => void; onLeft: () => void
  onStartDm: (uid: string) => void; onMembersChanged: () => void
}) {
  const [name, setName] = useState(g.name)
  const [desc, setDesc] = useState(g.description)
  const [vis, setVis] = useState(g.visibility)
  const [adding, setAdding] = useState<string[]>([])
  const [memberQ, setMemberQ] = useState('')
  const [saving, setSaving] = useState(false)
  const directory = useMemo(() => new Map(members.map((m) => [m.user_id, m])), [members])
  const inGroup = new Set((g.members ?? []).map((m) => m.user_id))
  const showMembers = (g.members ?? []).filter((m) => {
    const q = memberQ.trim().toLowerCase()
    if (!q) return true
    const info = directory.get(m.user_id)
    return [m.name, info?.username, info?.role].some((v) => String(v ?? '').toLowerCase().includes(q))
  })
  const onlineCount = (g.members ?? []).filter((m) => directory.get(m.user_id)?.online).length
  const dirty = name !== g.name || desc !== g.description || vis !== g.visibility
  const canInvite = !!g.my_role && (isMod || g.visibility === 'open')
  const act = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); notify(ok); onSaved() } catch (e) { notify((e as Error).message, true) } }
  const reset = () => { setName(g.name); setDesc(g.description); setVis(g.visibility) }
  const save = async () => {
    if (!isMod || !dirty || saving) return
    setSaving(true)
    try {
      await collab.updateGroup(g.id, { name: name.trim(), description: desc.trim(), visibility: vis })
      notify('群组资料已保存')
      onSaved()
    } catch (e) { notify((e as Error).message, true) } finally { setSaving(false) }
  }
  return (
    <div className="grp-settings">
      <div className="grp-settings-intro">
        <div>
          <h3>群组设置</h3>
          <p>管理群组资料、通知偏好和成员权限。头像可发起私聊，铃铛按钮可屏蔽成员或群通知。</p>
        </div>
        <div className="grp-settings-stats" aria-label="群组概况">
          <span><b>{g.member_count}</b><small>成员</small></span>
          <span><b>{onlineCount}</b><small>在线</small></span>
          <span><b>{g.open_tasks}</b><small>进行中任务</small></span>
        </div>
      </div>

      <section className="set-section grp-set-card grp-identity-card">
        <div className="grp-set-head">
          <span className="grp-set-icon"><Icon name="teams" size={16} /></span>
          <div><h3 className="section-title">QuantLab 群号</h3><p>群号可在公开目录中按名称或号码搜索；重新生成后旧群号立即失效。</p></div>
        </div>
        <div className="grp-number-row">
          <code>{g.quantlab_no || 'QLG-—'}</code>
          <button type="button" className="button small" onClick={() => { copyText(g.quantlab_no).then(() => notify('群号已复制')).catch(() => undefined) }}>复制群号</button>
          {isMod && <button type="button" className="button small danger" onClick={async () => {
            if (!await appConfirm(`重新生成「${g.name}」的 QuantLab 群号？旧号码会立即失效，群组与消息不受影响。`, { danger: true })) return
            try { const r = await collab.rotateGroupNumber(g.id); notify(`新群号：${r.quantlab_no}`); onSaved() } catch (e) { notify((e as Error).message, true) }
          }}>销毁并重新生成</button>}
        </div>
      </section>

      <section className="set-section grp-set-card">
        <div className="grp-set-head">
          <span className="grp-set-icon"><Icon name="settings" size={16} /></span>
          <div><h3 className="section-title">群组资料</h3><p>名称和可见性决定成员如何发现、加入这个群组。</p></div>
        </div>
        <div className="form-grid">
          <label>名称<input value={name} disabled={!isMod} maxLength={40} onChange={(e) => setName(e.target.value)} /></label>
          <label>可见性<select value={vis} disabled={!isMod} onChange={(e) => setVis(e.target.value as 'open' | 'private')}>
            <option value="open">公开：工作区成员可发现、可加入</option><option value="private">私密：只能邀请加入</option>
          </select></label>
        </div>
        <label className="grp-desc-field">简介<textarea rows={3} value={desc} disabled={!isMod} maxLength={500} onChange={(e) => setDesc(e.target.value)} placeholder="这个群讨论什么、如何协作" />
          <small className="grp-char-count">{desc.length} / 500</small>
        </label>
        <div className="grp-set-meta">内部 ID <code>{g.id}</code> · {g.quantlab_no} · 创建于 {since(g.created_at)}</div>
        {isMod && (
          <div className="grp-savebar">
            <span className={dirty ? 'dirty' : ''}>{dirty ? '有未保存的修改' : '群组资料已是最新'}</span>
            <div className="button-row">
              <button type="button" className="button small" disabled={!dirty || saving} onClick={reset}>恢复</button>
              <button type="button" className="button small primary" disabled={!dirty || saving || !name.trim()} onClick={save}>{saving ? '保存中…' : '保存修改'}</button>
            </div>
          </div>
        )}
      </section>

      <section className="set-section grp-set-card">
        <div className="grp-set-head">
          <span className="grp-set-icon"><Icon name="bell" size={16} /></span>
          <div><h3 className="section-title">通知偏好</h3><p>只影响你自己的通知中心，不改变其他成员设置。</p></div>
        </div>
        <div className="grp-pref-row">
          <div className="grp-pref-copy">
            <span className="grp-pref-avatar"><Icon name="bell-off" size={15} /></span>
            <div><b>群通知</b><small>屏蔽后不再收到本群的 @ 与群通知；任务指派仍会通知。</small></div>
          </div>
          <MuteGroupButton g={g} notify={notify} wide />
        </div>
        <p className="hint grp-pref-hint">需要只屏蔽某个人时，在下方成员列表点铃铛，或在聊天消息的更多操作里屏蔽。</p>
      </section>

      <section className="set-section grp-set-card">
        <div className="grp-set-head">
          <span className="grp-set-icon"><Icon name="teams" size={16} /></span>
          <div><h3 className="section-title">成员管理 · {g.members?.length ?? 0}</h3><p>{onlineCount} 人在线。点击头像或“私聊”即可发起一对一聊天。</p></div>
        </div>
        <div className="grp-member-tools">
          <input className="search" value={memberQ} onChange={(e) => setMemberQ(e.target.value)} placeholder="搜索成员、用户名或角色" aria-label="搜索群组成员" />
          <span className="hint">共 {showMembers.length} 人</span>
        </div>
        <div className="table-wrap grp-members-table">
          <table>
            <thead><tr><th>成员</th><th>状态</th><th>群角色</th><th>加入时间</th><th className="right">操作</th></tr></thead>
            <tbody className="stagger">
              {showMembers.map((m) => {
                const info = directory.get(m.user_id)
                const self = m.user_id === me.user.id
                return (
                  <tr key={m.user_id}>
                    <td>
                      <div className="grp-member-cell">
                        {self
                          ? <UserAvatar color={info?.avatar_color} size={28} title={`${m.name}（我）`} />
                          : <button type="button" className={`grp-member-av${info?.online ? ' online' : ''}`} title={`与 ${m.name} 私聊`} aria-label={`与 ${m.name} 私聊`} onClick={() => onStartDm(m.user_id)}><UserAvatar color={info?.avatar_color || m.avatar_color} size={28} title={m.name} /></button>}
                        <div className="grp-member-copy"><b>{m.name}{self && <small>（我）</small>}</b><small>{info?.username ? `@${info.username}` : ROLE_LABEL[m.role]}</small></div>
                      </div>
                    </td>
                    <td>
                      <span className={`grp-member-status${info?.online ? ' online' : ''}`}>
                        <i aria-hidden />{info?.online ? '在线' : '离线'}
                        {info?.muted && <em><Icon name="bell-off" size={11} />已屏蔽</em>}
                      </span>
                    </td>
                    <td>{isMod && m.role !== 'owner' ? (
                      <select value={m.role} aria-label={`设置 ${m.name} 的群角色`} onChange={(e) => act(() => collab.setMemberRole(g.id, m.user_id, e.target.value as GroupRole), '已更新群角色')}>
                        <option value="admin">群管理员</option><option value="member">成员</option>
                      </select>
                    ) : <Tag tone={m.role === 'owner' ? 'ok' : undefined}>{ROLE_LABEL[m.role]}</Tag>}</td>
                    <td><span className="grp-member-joined">{since(m.joined_at)}</span></td>
                    <td className="right">
                      <div className="grp-member-actions">
                        {!self && <button type="button" className="button small grp-action-dm" onClick={() => onStartDm(m.user_id)}><Icon name="message" size={14} />私聊</button>}
                        {!self && info && info.role !== 'admin' && <MemberMuteButton member={info} notify={notify} onChanged={onMembersChanged} compact />}
                        {g.my_role === 'owner' && m.role !== 'owner' && <button type="button" className="button small" onClick={async () => { if (await appConfirm(`把群主转让给 ${m.name}？你会变成群管理员。`)) act(() => collab.setMemberRole(g.id, m.user_id, 'owner'), '已转让') }}>转让群主</button>}
                        {isMod && m.role !== 'owner' && !self && <button type="button" className="button small danger" onClick={async () => { if (await appConfirm(`把 ${m.name} 移出群组？`)) act(() => collab.removeMember(g.id, m.user_id), '已移出') }}>移出</button>}
                      </div>
                    </td>
                  </tr>
                )
              })}
              {!showMembers.length && <tr><td colSpan={5}><div className="grp-members-empty">没有匹配的成员</div></td></tr>}
            </tbody>
          </table>
        </div>
      </section>

      {canInvite && (
        <section className="set-section grp-set-card">
          <div className="grp-set-head">
            <span className="grp-set-icon"><Icon name="teams" size={16} /></span>
            <div><h3 className="section-title">邀请成员</h3><p>公开群所有成员都可邀请，私密群仅群主和群管理员可邀请。</p></div>
          </div>
          {members.every((m) => inGroup.has(m.user_id))
            ? <span className="hint">工作区成员都已在群里</span>
            : <MemberPicker members={members} picked={adding} onChange={setAdding} exclude={[...inGroup]} />}
          {adding.length > 0 && <div className="grp-invitebar"><span>已选择 {adding.length} 人</span><div className="button-row"><button type="button" className="button small" onClick={() => setAdding([])}>清空</button><button type="button" className="button small primary" onClick={() => act(async () => { await collab.addMembers(g.id, adding); setAdding([]) }, '已邀请')}>邀请加入</button></div></div>}
        </section>
      )}

      <section className="set-section grp-set-card danger-zone">
        <div className="grp-set-head">
          <span className="grp-set-icon danger"><Icon name="issues" size={16} /></span>
          <div><h3 className="section-title">离开与解散</h3><p>退出后需要重新加入；解散会关闭群组但保留数据库中的消息与任务。</p></div>
        </div>
        <div className="button-row">
          {g.my_role && g.my_role !== 'owner' && <button type="button" className="button danger" onClick={async () => {
            if (await appConfirm(`退出「${g.name}」？`)) { try { await collab.removeMember(g.id, me.user.id); onLeft() } catch (e) { notify((e as Error).message, true) } }
          }}>退出群组</button>}
          {(g.my_role === 'owner' || me.workspace?.role === 'admin') && <button type="button" className="button danger" onClick={async () => {
            if (await appConfirm(`解散「${g.name}」？消息与任务会保留在数据库里，但群组不再可用。`, { danger: true })) { try { await collab.archiveGroup(g.id); onLeft() } catch (e) { notify((e as Error).message, true) } }
          }}>解散群组</button>}
        </div>
      </section>
    </div>
  )
}
