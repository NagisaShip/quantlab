/**
 * 运维控制台（部署管理员）：服务器总览、账号管理（封禁 / 解封 / 重置密码 / 强制下线 / 授权）、
 * 工作区（停用 / 恢复）、全站模型用量、公告与平台设置、全站审计。
 * 在全屏「设置」里打开；只有部署管理员能看到入口，接口也在服务端校验。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  admin, fmtBytes, since,
  type AdminOverview, type AdminUser, type AdminWorkspace, type UsageRow,
} from '../apiCollab'
import { Callout, EmptyState, Heading, Loading, Modal, Panel, Tag } from '../components/ui'
import { fmtCost, fmtTokens } from '../components/modelUsage'
import { appConfirm, appPrompt } from '../components/dialog'
import type { Me } from '../api/platform'
import type { PageProps } from './types'
import { tData } from '../i18n'

export const ADMIN_TABS: { key: string; label: string }[] = [
  { key: 'overview', label: '服务器总览' }, { key: 'users', label: '账号管理' }, { key: 'workspaces', label: '全部工作区' },
  { key: 'usage', label: '全站模型用量' }, { key: 'announce', label: '公告与平台设置' }, { key: 'audit', label: '全站审计' },
]

export default function AdminPage({ tab, me, onNavigate, notify }: PageProps & { tab?: string; me: Me }) {
  const cur = ADMIN_TABS.find((t) => t.key === tab)?.key ?? 'overview'
  if (!me.user.is_admin) return <Callout tone="warn">只有部署管理员（平台服务的部署方）可以使用运维控制台。</Callout>
  return (
    <>
      <Heading title={ADMIN_TABS.find((t) => t.key === cur)!.label} description="部署管理员 · 管理整台服务器：所有账号、所有工作区、全站用量。所有操作都写入审计日志。" />
      {cur === 'overview' && <Overview onNavigate={onNavigate} />}
      {cur === 'users' && <Users me={me} notify={notify} />}
      {cur === 'workspaces' && <Workspaces me={me} notify={notify} />}
      {cur === 'usage' && <Usage />}
      {cur === 'announce' && <Announce notify={notify} />}
      {cur === 'audit' && <Audit />}
    </>
  )
}

const dur = (s: number) => (s >= 86400 ? `${Math.floor(s / 86400)} 天 ${Math.floor((s % 86400) / 3600)} 小时` : s >= 3600 ? `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分` : `${Math.floor(s / 60)} 分钟`)

function Kpi({ label, value, hint, tone }: { label: string; value: React.ReactNode; hint?: React.ReactNode; tone?: string }) {
  return <div className={`adm-kpi ${tone ?? ''}`}><small>{label}</small><b>{value}</b>{hint && <span>{hint}</span>}</div>
}

function Overview({ onNavigate }: { onNavigate: (k: string) => void }) {
  const [d, setD] = useState<AdminOverview | null>(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => { admin.overview().then(setD).catch((e) => setErr(e.message)) }, [])
  useEffect(() => { load(); const t = window.setInterval(load, 20000); return () => window.clearInterval(t) }, [load])
  if (err) return <Callout tone="fail">{err}</Callout>
  if (!d) return <Loading />
  const disk = d.server.disk
  const diskPct = Math.round((disk.used / Math.max(1, disk.total)) * 100)
  const running = (d.runs.running ?? 0) + (d.runs.queued ?? 0) + (d.runs.pending ?? 0)
  return (
    <div className="adm">
      <div className="adm-kpis stagger">
        <Kpi label="账号" value={d.users.total} hint={`${d.users.active_7d} 人 7 天内登录 · ${d.users.sessions} 个在线会话`} />
        <Kpi label="封禁" value={d.users.banned} tone={d.users.banned ? 'warn' : ''} hint={`${d.users.admins} 名部署管理员`} />
        <Kpi label="工作区" value={d.workspaces.active} hint={`共 ${d.workspaces.total} 个 · ${d.groups} 个群组`} />
        <Kpi label="运行中 / 排队" value={running} hint={`worker 在线 ${d.workers.alive}${d.workers.stale_runs ? ` · ${d.workers.stale_runs} 个失联` : ''}`} tone={d.workers.stale_runs ? 'warn' : ''} />
        <Kpi label="模型费用 · 24 小时" value={fmtCost(d.llm['24h'].cost)} hint={`${fmtTokens(d.llm['24h'].tokens)} token · ${d.llm['24h'].calls} 次`} />
        <Kpi label="模型费用 · 30 天" value={fmtCost(d.llm['30d'].cost)} hint={`7 天 ${fmtCost(d.llm['7d'].cost)}`} />
        <Kpi label="待处理问题" value={d.issues.open ?? 0} tone={(d.issues.open ?? 0) > 0 ? 'warn' : ''} hint={<button type="button" className="link-btn" onClick={() => onNavigate('issues')}>去处理 →</button>} />
        <Kpi label="磁盘" value={`${diskPct}%`} tone={diskPct > 85 ? 'fail' : diskPct > 70 ? 'warn' : ''} hint={`剩余 ${fmtBytes(disk.free)} / ${fmtBytes(disk.total)}`} />
      </div>
      <div className="adm-grid">
        <Panel title="服务器">
          <dl className="kv">
            <dt>主机</dt><dd>{d.server.host} · PID {d.server.pid}</dd>
            <dt>系统</dt><dd>{d.server.platform}</dd>
            <dt>Python</dt><dd>{d.server.python}</dd>
            <dt>已运行</dt><dd>{dur(d.server.uptime_s)}</dd>
            <dt>数据库</dt><dd>{d.server.db === 'sqlite' ? `SQLite · ${fmtBytes(d.server.db_bytes)}` : 'PostgreSQL'}</dd>
            <dt>数据目录</dt><dd className="mono small">{d.server.data_dir}</dd>
            <dt>worker 模式</dt><dd>{d.server.worker_mode}</dd>
            <dt>注册方式</dt><dd>{d.server.open_registration ? <Tag tone="warn">开放注册</Tag> : <Tag tone="ok">凭邀请码</Tag>}</dd>
          </dl>
          <div className="adm-disk"><div className="lim-track"><i style={{ width: `${diskPct}%` }} /></div><small>磁盘已用 {fmtBytes(disk.used)}</small></div>
        </Panel>
        <Panel title="数据占用">
          <ul className="adm-bars">
            {Object.entries(d.server.folders).sort((a, b) => b[1] - a[1]).map(([k, v]) => {
              const max = Math.max(...Object.values(d.server.folders), 1)
              return <li key={k}><span>{({ workspaces: '工作区数据', results: '回测结果', market: '行情缓存', uploads: '上传文件', global: '平台配置' } as Record<string, string>)[k] ?? k}</span><i style={{ width: `${(v / max) * 100}%` }} /><b>{fmtBytes(v)}</b></li>
            })}
          </ul>
        </Panel>
        <Panel title="运行队列">
          {Object.keys(d.runs).length ? <ul className="adm-bars">{Object.entries(d.runs).map(([k, v]) => {
            const max = Math.max(...Object.values(d.runs), 1)
            return <li key={k}><span>{k}</span><i style={{ width: `${(v / max) * 100}%` }} /><b>{v}</b></li>
          })}</ul> : <div className="hint">还没有运行记录</div>}
        </Panel>
        <Panel title="worker 心跳">
          {d.workers.error && <Callout tone="warn">{d.workers.error}</Callout>}
          {d.workers.list.length ? (
            <ul className="plain-list">{d.workers.list.map((w) => <li key={w.id}><span><span className={`dot ${w.alive ? 'ok' : 'bad'}`} /> {w.host}</span><small>{since(w.last_seen)}</small></li>)}</ul>
          ) : <div className="hint">没有独立 worker（inline 模式下运行在 API 进程里）</div>}
        </Panel>
      </div>
    </div>
  )
}

function Users({ me, notify }: { me: Me; notify: PageProps['notify'] }) {
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('all')
  const [users, setUsers] = useState<AdminUser[] | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [secret, setSecret] = useState<{ title: string; value: string } | null>(null)
  const load = useCallback(() => { admin.users({ q, status }).then((r) => setUsers(r.users)).catch((e) => notify(e.message, true)) }, [q, status, notify])
  useEffect(() => { const t = window.setTimeout(load, q ? 250 : 0); return () => window.clearTimeout(t) }, [load, q])
  const act = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); notify(ok); load() } catch (e) { notify((e as Error).message, true) } }
  return (
    <div className="adm">
      <div className="community-bar">
        <div className="tabs flat">
          {[['all', '全部'], ['active', '正常'], ['banned', '已封禁'], ['admin', '部署管理员']].map(([k, l]) => <button key={k} aria-pressed={status === k} onClick={() => setStatus(k)}>{l}</button>)}
        </div>
        <div className="community-tools">
          <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索邮箱或名字" />
          <button className="button primary small" onClick={() => setCreating(true)}>＋ 创建账号</button>
        </div>
      </div>
      {!users ? <Loading /> : !users.length ? <EmptyState title="没有符合条件的账号" /> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>账号</th><th>状态</th><th className="right">工作区</th><th className="right">近 30 天模型</th><th>最近登录</th><th>注册</th><th /></tr></thead>
            <tbody className="stagger">
              {users.map((u) => (
                <tr key={u.id} className={u.banned_at ? 'row-muted' : undefined}>
                  <td className="member-cell">
                    <b><button type="button" className="link-btn" onClick={() => setOpen(u.id)}>{u.name || '—'}</button>{u.id === me.user.id && <small>（我）</small>}</b>
                    <small className="mono">{u.email}</small>
                  </td>
                  <td>
                    {u.banned_at ? <Tag tone="fail">已封禁</Tag> : <Tag tone="ok">正常</Tag>}
                    {u.is_admin && <> <Tag tone="agent">部署管理员</Tag></>}
                    {u.totp && <> <Tag>2FA</Tag></>}
                    {u.banned_reason && <small className="muted" style={{ display: 'block' }}>{u.banned_reason}</small>}
                  </td>
                  <td className="right">{u.workspaces}</td>
                  <td className="right mono" title={`${u.usage_30d?.calls ?? 0} 次 · ${fmtTokens(u.usage_30d?.tokens ?? 0)} token`}>{fmtCost(u.usage_30d?.cost ?? 0)}</td>
                  <td>{since(u.last_login_at)}</td>
                  <td>{since(u.created_at)}</td>
                  <td className="right">
                    {u.id !== me.user.id && <div className="row-actions">
                      {u.banned_at
                        ? <button className="button small" onClick={() => act(() => admin.unban(u.id), '已解封')}>解封</button>
                        : <button className="button small danger" onClick={async () => {
                          const reason = await appPrompt(`封禁 ${u.email}？对方会立即下线、无法登录。原因（登录时会提示）：`, '', { title: '封禁账号', okLabel: '封禁', placeholder: '例如：违反使用规范' })
                          if (reason !== null) act(() => admin.ban(u.id, reason), '已封禁')
                        }}>封禁</button>}
                      <button className="button small" onClick={() => setOpen(u.id)}>详情</button>
                    </div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && <UserDetail id={open} me={me} onClose={() => setOpen(null)} notify={notify} onChanged={load} onSecret={setSecret} />}
      {creating && <CreateUser onClose={() => setCreating(false)} notify={notify} onDone={(email, pw) => { setCreating(false); load(); setSecret({ title: `${email} 的临时密码`, value: pw }) }} />}
      {secret && (
        <Modal title={secret.title} onClose={() => setSecret(null)} width={520}>
          <p className="hint" style={{ marginTop: 0 }}>只显示这一次。请通过安全渠道发给对方，并提醒登录后立即在「我的账号」里修改。</p>
          <div className="secret-once">{secret.value}</div>
        </Modal>
      )}
    </div>
  )
}

function UserDetail({ id, me, onClose, notify, onChanged, onSecret }: {
  id: string; me: Me; onClose: () => void; notify: PageProps['notify']; onChanged: () => void; onSecret: (s: { title: string; value: string }) => void
}) {
  const [d, setD] = useState<Awaited<ReturnType<typeof admin.user>> | null>(null)
  const load = useCallback(() => { admin.user(id).then(setD).catch((e) => notify(e.message, true)) }, [id, notify])
  useEffect(load, [load])
  const act = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); notify(ok); load(); onChanged() } catch (e) { notify((e as Error).message, true) } }
  const self = id === me.user.id
  return (
    <Modal title={d ? `${d.user.name} · ${d.user.email}` : '账号详情'} onClose={onClose} width={860}>
      {!d ? <Loading /> : (
        <div className="adm-user">
          <div className="button-row">
            {!self && (d.user.banned_at
              ? <button className="button small" onClick={() => act(() => admin.unban(id), '已解封')}>解封</button>
              : <button className="button small danger" onClick={async () => { const r = await appPrompt('封禁原因（登录时会提示对方）：', '', { title: '封禁账号', okLabel: '封禁' }); if (r !== null) act(() => admin.ban(id, r), '已封禁') }}>封禁</button>)}
            {!self && <button className="button small" onClick={async () => { if (await appConfirm('作废该账号的全部会话，对方需要重新登录？')) act(() => admin.revoke(id), '已强制下线') }}>强制下线</button>}
            <button className="button small" onClick={async () => {
              if (!(await appConfirm('重置密码？会生成一个临时密码并作废该账号的全部会话。'))) return
              try { const r = await admin.resetPassword(id); onSecret({ title: `${d.user.email} 的临时密码`, value: r.temp_password }); load() } catch (e) { notify((e as Error).message, true) }
            }}>重置密码</button>
            {!self && <button className="button small" onClick={async () => {
              const to = !d.user.is_admin
              if (await appConfirm(to ? `授予 ${d.user.email} 部署管理员？对方将能管理整台服务器。` : `撤销 ${d.user.email} 的部署管理员？`)) act(() => admin.setAdmin(id, to), '已更新')
            }}>{d.user.is_admin ? '撤销部署管理员' : '设为部署管理员'}</button>}
          </div>
          {d.user.banned_at && <Callout tone="fail">已于 {new Date(d.user.banned_at).toLocaleString()} 封禁{d.user.banned_reason ? `：${d.user.banned_reason}` : ''}</Callout>}
          <div className="adm-grid two">
            <section>
              <h4>所在工作区</h4>
              <ul className="plain-list">{d.workspaces.map((w) => <li key={w.id}><span>{w.name} <Tag>{w.role}</Tag>{w.member_status !== 'active' && <Tag tone="warn">已移出</Tag>}{w.status !== 'active' && <Tag tone="fail">工作区已停用</Tag>}</span><small>{since(w.created_at)}</small></li>)}</ul>
              <h4>群组</h4>
              {d.groups.length ? <ul className="plain-list">{d.groups.map((g) => <li key={g.id}><span>{g.name}</span><small>{g.role}</small></li>)}</ul> : <div className="hint">没有加入群组</div>}
              <h4>近 30 天模型用量</h4>
              {d.usage_30d.length ? <ul className="plain-list">{d.usage_30d.map((u, i) => <li key={i}><span>{d.workspaces.find((w) => w.id === u.workspace_id)?.name ?? u.workspace_id} · {u.scope}</span><small>{fmtTokens(u.tokens)} · {fmtCost(u.cost)}</small></li>)}</ul> : <div className="hint">没有用量</div>}
            </section>
            <section>
              <h4>在线会话 · {d.sessions.length}</h4>
              {d.sessions.length ? <ul className="plain-list">{d.sessions.map((s) => <li key={s.id}><span className="mono small">{s.ip || '—'}</span><small title={s.user_agent}>{since(s.created_at)} 登录</small></li>)}</ul> : <div className="hint">没有在线会话</div>}
              <h4>最近操作</h4>
              <ul className="plain-list adm-audit-mini">{d.audit.map((a, i) => <li key={i}><span className="mono small">{a.action}</span><small>{since(a.ts)}</small></li>)}</ul>
            </section>
          </div>
        </div>
      )}
    </Modal>
  )
}

function CreateUser({ onClose, onDone, notify }: { onClose: () => void; onDone: (email: string, pw: string) => void; notify: PageProps['notify'] }) {
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [ws, setWs] = useState('')
  const [role, setRole] = useState('member')
  const [list, setList] = useState<AdminWorkspace[]>([])
  useEffect(() => { admin.workspaces().then((r) => setList(r.workspaces.filter((w) => w.status === 'active'))).catch(() => undefined) }, [])
  return (
    <Modal title="创建账号" onClose={onClose} width={560}
      footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={!email.includes('@')} onClick={async () => {
        try { const r = await admin.createUser({ email, name, workspace_id: ws || null, role }); onDone(email, r.temp_password) } catch (e) { notify((e as Error).message, true) }
      }}>创建</button></>}>
      <p className="hint" style={{ marginTop: 0 }}>直接创建账号并生成临时密码（只显示一次）。也可以在「设置 · 成员与邀请」里发邀请码，让对方自己注册。</p>
      <div className="form-grid">
        <label>邮箱<input value={email} onChange={(e) => setEmail(e.target.value)} autoFocus /></label>
        <label>名字<input value={name} onChange={(e) => setName(e.target.value)} placeholder="可选" /></label>
        <label>加入工作区<select value={ws} onChange={(e) => setWs(e.target.value)}>
          <option value="">不加入（为对方新建一个工作区）</option>
          {list.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select></label>
        <label>角色<select value={role} disabled={!ws} onChange={(e) => setRole(e.target.value)}>
          <option value="member">成员</option><option value="reviewer">审阅者（只读）</option><option value="admin">工作区管理员</option>
        </select></label>
      </div>
    </Modal>
  )
}

function Workspaces({ me, notify }: { me: Me; notify: PageProps['notify'] }) {
  const [rows, setRows] = useState<AdminWorkspace[] | null>(null)
  const load = useCallback(() => { admin.workspaces().then((r) => setRows(r.workspaces)).catch((e) => notify(e.message, true)) }, [notify])
  useEffect(load, [load])
  if (!rows) return <Loading />
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>工作区</th><th>所有者</th><th className="right">成员</th><th className="right">群组</th><th className="right">30 天运行</th><th className="right">30 天模型费用</th><th>状态</th><th /></tr></thead>
        <tbody className="stagger">
          {rows.map((w) => (
            <tr key={w.id} className={w.status !== 'active' ? 'row-muted' : undefined}>
              <td className="member-cell"><b>{w.name}{w.id === me.workspace?.id && <small>（当前）</small>}</b><small className="mono">{w.id} · {since(w.created_at)}</small></td>
              <td className="member-cell"><b>{w.owner_name || '—'}</b><small>{w.owner_email}</small></td>
              <td className="right">{w.members}</td><td className="right">{w.groups}</td><td className="right">{w.runs_30d}</td>
              <td className="right mono" title={`${w.usage_30d.calls} 次 · ${fmtTokens(w.usage_30d.tokens)} token`}>{fmtCost(w.usage_30d.cost)}</td>
              <td>{w.status === 'active' ? <Tag tone="ok">正常</Tag> : <Tag tone="fail">已停用</Tag>}{w.is_demo && <> <Tag>演示</Tag></>}</td>
              <td className="right">
                {w.status === 'active'
                  ? w.id !== me.workspace?.id && <button className="button small danger" onClick={async () => {
                    if (await appConfirm(`停用「${w.name}」？成员将无法进入，数据保留，可随时恢复。`, { danger: true })) {
                      try { await admin.setWorkspaceStatus(w.id, 'suspended'); notify('已停用'); load() } catch (e) { notify((e as Error).message, true) }
                    }
                  }}>停用</button>
                  : <button className="button small" onClick={async () => { try { await admin.setWorkspaceStatus(w.id, 'active'); notify('已恢复'); load() } catch (e) { notify((e as Error).message, true) } }}>恢复</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Usage() {
  const [days, setDays] = useState(30)
  const [d, setD] = useState<Awaited<ReturnType<typeof admin.usage>> | null>(null)
  const [metric, setMetric] = useState<'cost' | 'tokens'>('cost')
  useEffect(() => { setD(null); admin.usage(days).then(setD).catch(() => undefined) }, [days])
  if (!d) return <Loading />
  const vals = d.daily.map((x) => (metric === 'cost' ? Number(x.cost) : x.tokens))
  const max = Math.max(1e-9, ...vals)
  const table = (title: string, rows: UsageRow[], label: (r: UsageRow) => string) => (
    <Panel title={title}>
      {rows.length ? (
        <table className="adm-usage-table">
          <tbody>{rows.slice(0, 15).map((r, i) => {
            const m = Math.max(...rows.map((x) => Number(x.cost) || x.tokens / 1e6), 1e-9)
            return <tr key={i}><td>{label(r)}</td><td className="adm-bar-cell"><i style={{ width: `${((Number(r.cost) || r.tokens / 1e6) / m) * 100}%` }} /></td><td className="right mono">{fmtTokens(r.tokens)}</td><td className="right mono">{fmtCost(r.cost)}</td></tr>
          })}</tbody>
        </table>
      ) : <div className="hint">没有用量</div>}
    </Panel>
  )
  return (
    <div className="adm">
      <div className="board-bar">
        <div className="seg seg-sm">{[7, 30, 90].map((n) => <button key={n} type="button" aria-selected={days === n} onClick={() => setDays(n)}>{n} 天</button>)}</div>
        <div className="seg seg-sm">{(['cost', 'tokens'] as const).map((k) => <button key={k} type="button" aria-selected={metric === k} onClick={() => setMetric(k)}>{k === 'cost' ? '费用' : 'token'}</button>)}</div>
      </div>
      <Panel title="每日用量（全站）">
        <div className="usage-bars adm-daily">
          {d.daily.map((x, i) => <div key={x.day} className="usage-bar" title={`${x.day}：${fmtTokens(x.tokens)} token · ${fmtCost(x.cost)} · ${x.calls} 次`}><i style={{ height: `${(vals[i] / max) * 100}%`, animationDelay: `${i * 8}ms` }} /></div>)}
        </div>
      </Panel>
      <div className="adm-grid">
        {table('按账号', d.by_user, (r) => r.name || r.user_id || '')}
        {table('按工作区', d.by_workspace, (r) => r.name || r.workspace_id || '')}
        {table('按服务商', d.by_provider, (r) => r.provider || '')}
        {table('按来源', d.by_scope, (r) => ({ workspace: '工作区共享', personal: '个人', group: '群组' } as Record<string, string>)[r.scope || ''] ?? r.scope ?? '')}
      </div>
    </div>
  )
}

function Announce({ notify }: { notify: PageProps['notify'] }) {
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [ws, setWs] = useState('')
  const [banner, setBanner] = useState(true)
  const [hours, setHours] = useState(72)
  const [tone, setTone] = useState<'info' | 'warn'>('info')
  const [list, setList] = useState<AdminWorkspace[]>([])
  const [st, setSt] = useState<Awaited<ReturnType<typeof admin.settings>> | null>(null)
  useEffect(() => {
    admin.workspaces().then((r) => setList(r.workspaces)).catch(() => undefined)
    admin.settings().then(setSt).catch(() => undefined)
  }, [])
  const save = async (p: Parameters<typeof admin.saveSettings>[0], ok: string) => { try { setSt(await admin.saveSettings(p)); notify(ok) } catch (e) { notify((e as Error).message, true) } }
  return (
    <div className="adm adm-grid">
      <Panel title="发布公告" subtitle="站内通知发给全部账号（或某个工作区），不受屏蔽影响；可以同时挂在顶栏横幅">
        <label>标题<input value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} placeholder="例如：周六 02:00–02:30 停机维护" /></label>
        <label style={{ marginTop: 10 }}>内容<textarea rows={4} value={body} maxLength={2000} onChange={(e) => setBody(e.target.value)} /></label>
        <div className="form-grid" style={{ marginTop: 10 }}>
          <label>发给<select value={ws} onChange={(e) => setWs(e.target.value)}><option value="">全部账号</option>{list.map((w) => <option key={w.id} value={w.id}>工作区：{w.name}</option>)}</select></label>
          <label>样式<select value={tone} onChange={(e) => setTone(e.target.value as 'info' | 'warn')}><option value="info">普通</option><option value="warn">提醒（黄色）</option></select></label>
        </div>
        <label className="check" style={{ marginTop: 10 }}><input type="checkbox" checked={banner} onChange={(e) => setBanner(e.target.checked)} />同时显示为顶栏横幅，持续
          <input type="number" min={1} max={2160} value={hours} onChange={(e) => setHours(Number(e.target.value))} style={{ width: 70, margin: '0 6px' }} />小时</label>
        <div className="button-row" style={{ marginTop: 12 }}>
          <button className="button primary" disabled={title.trim().length < 2} onClick={async () => {
            if (!(await appConfirm(`发布公告「${title}」给${ws ? '该工作区成员' : '全部账号'}？`))) return
            try {
              const r = await admin.announce({ title, body, workspace_id: ws || null, banner, banner_hours: hours, tone })
              notify(`已发送给 ${r.recipients} 人`); setTitle(''); setBody('')
              admin.settings().then(setSt).catch(() => undefined)
            } catch (e) { notify((e as Error).message, true) }
          }}>发布</button>
        </div>
      </Panel>
      <Panel title="平台设置">
        {!st ? <Loading /> : (
          <>
            <div className="adm-setting">
              <div><b>开放注册</b><small>开启后任何人都能注册（不需要邀请码），每人自动获得一个自己的工作区。{st.env_open_registration ? '环境变量已开启。' : ''}</small></div>
              <button type="button" role="switch" aria-checked={st.open_registration} className={`switch${st.open_registration ? ' on' : ''}`}
                onClick={() => save({ open_registration: !st.open_registration }, st.open_registration ? '已改为凭邀请码注册' : '已开放注册')}><i /></button>
            </div>
            {st.open_registration_override !== null && <button type="button" className="link-btn" onClick={() => save({ open_registration: null }, '已恢复为环境变量设置')}>恢复为环境变量（QAGENT_OPEN_REGISTRATION）</button>}
            <div className="adm-setting">
              <div><b>顶栏横幅</b><small>{st.banner?.text ? `当前：${st.banner.text}${st.banner.until ? `（${new Date(st.banner.until).toLocaleString()} 到期）` : ''}` : '当前没有横幅'}</small></div>
              {st.banner?.text && <button type="button" className="button small" onClick={() => save({ banner_text: '' }, '已撤下横幅')}>撤下</button>}
            </div>
          </>
        )}
      </Panel>
    </div>
  )
}

function Audit() {
  const [action, setAction] = useState('')
  const [rows, setRows] = useState<Awaited<ReturnType<typeof admin.audit>>['items'] | null>(null)
  useEffect(() => { const t = window.setTimeout(() => admin.audit({ action: action || undefined }).then((r) => setRows(r.items)).catch(() => setRows([])), action ? 300 : 0); return () => window.clearTimeout(t) }, [action])
  return (
    <div className="adm">
      <div className="board-bar"><input className="search" value={action} onChange={(e) => setAction(e.target.value)} placeholder="按操作前缀筛选，例如 admin. / member. / group." /></div>
      {!rows ? <Loading /> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>时间</th><th>账号</th><th>操作</th><th>对象</th><th>工作区</th><th>IP</th><th>详情</th></tr></thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{new Date(r.ts).toLocaleString()}</td><td>{r.user_name || r.user_id}</td><td className="mono small">{r.action}</td>
                  <td className="mono small">{r.object_type}{r.object_id ? `:${r.object_id.slice(0, 16)}` : ''}</td>
                  <td className="mono small">{r.workspace_id?.slice(0, 8)}</td><td className="mono small">{r.ip}</td>
                  <td className="mono small adm-details" title={JSON.stringify(tData(r.details))}>{JSON.stringify(tData(r.details)).slice(0, 80)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
