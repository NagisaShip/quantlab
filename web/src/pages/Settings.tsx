import { useCallback, useEffect, useState } from 'react'
import { fmtTime } from '../api2'
import {
  KIND_LABEL, MODE_LABEL, RUN_STATE, v4,
  type AuditItem, type Invite, type Me, type Member, type NetworkStatus, type Role, type Run, type RunEvent,
  type TotpStatus, type UsagePanel as UsagePanelData,
} from '../api3'
import { Callout, EmptyState, Field, Heading, Loading, Modal, Panel, Tag } from '../components/ui'
import NodeTab from './NodeTab'
import type { PageProps } from './types'

type Tab = 'workspace' | 'members' | 'runs' | 'node' | 'audit' | 'system' | 'account'
const TABS: { key: Tab; label: string }[] = [
  { key: 'workspace', label: '工作区与额度' },
  { key: 'members', label: '成员与邀请' },
  { key: 'runs', label: '任务与运行' },
  { key: 'node', label: '运行节点与机器人' },
  { key: 'audit', label: '审计日志' },
  { key: 'system', label: '网络与隔离' },
  { key: 'account', label: '我的账号' },
]
const ROLE_LABEL: Record<Role, string> = { admin: '管理员', member: '成员', reviewer: '审阅者（只读）' }

export default function SettingsPage({ tab, me, onNavigate, notify, onMeChanged }: PageProps & { tab?: string; me: Me; onMeChanged: () => void }) {
  const current = (TABS.find((t) => t.key === tab)?.key ?? 'workspace') as Tab
  const isAdmin = me.workspace?.role === 'admin'
  return (
    <>
      <Heading title="设置" description={<>工作区「{me.workspace?.name}」· 你的角色：{ROLE_LABEL[me.workspace?.role ?? 'member']}。权限在服务端校验，客户端声明的工作区只表示目标。</>} />
      <div className="tabs wide">
        {TABS.map((t) => (
          <button key={t.key} aria-pressed={t.key === current} onClick={() => onNavigate(`settings/${t.key}`)}>{t.label}</button>
        ))}
      </div>
      <section className="panel"><div className="panel-body" style={{ paddingTop: 18 }}>
        {current === 'workspace' && <WorkspaceTab me={me} isAdmin={isAdmin} notify={notify} onSaved={onMeChanged} />}
        {current === 'members' && <MembersTab me={me} isAdmin={isAdmin} notify={notify} />}
        {current === 'runs' && <RunsTab notify={notify} onNavigate={onNavigate} readonly={me.workspace?.role === 'reviewer'} />}
        {current === 'node' && <NodeTab notify={notify} isAdmin={isAdmin} onNavigate={onNavigate} />}
        {current === 'audit' && (isAdmin ? <AuditTab notify={notify} /> : <Callout tone="warn">只有工作区管理员可以查看审计日志。</Callout>)}
        {current === 'system' && <SystemTab notify={notify} />}
        {current === 'account' && <AccountTab me={me} notify={notify} />}
      </div></section>
    </>
  )
}

function WorkspaceTab({ me, isAdmin, notify, onSaved }: { me: Me; isAdmin: boolean; notify: PageProps['notify']; onSaved: () => void }) {
  const ws = me.workspace!
  const [name, setName] = useState(ws.name)
  const [tz, setTz] = useState(ws.settings.timezone ?? 'UTC')
  const [q, setQ] = useState(ws.settings.quotas)
  const [newWs, setNewWs] = useState('')
  const [code, setCode] = useState('')
  const save = async () => {
    try {
      await v4.updateSettings({ name, timezone: tz, quotas: q })
      notify('已保存')
      onSaved()
    } catch (e) { notify((e as Error).message, true) }
  }
  const num = (k: keyof typeof q) => (
    <input type="number" min={0} value={Number(q[k])} disabled={!isAdmin} onChange={(e) => setQ({ ...q, [k]: Number(e.target.value) })} />
  )
  return (
    <div className="two-columns">
      <div>
        <h3 className="section-title">基本信息</h3>
        <div className="form-grid">
          <Field label="工作区名称"><input value={name} disabled={!isAdmin} onChange={(e) => setName(e.target.value)} /></Field>
          <Field label="报表时区" hint="行情与账本一律按 UTC 记录；时区只影响展示与日度切分说明">
            <select value={tz} disabled={!isAdmin} onChange={(e) => setTz(e.target.value)}>
              {['UTC', 'Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Singapore', 'Europe/London', 'America/New_York'].map((z) => <option key={z}>{z}</option>)}
            </select>
          </Field>
        </div>
        <h3 className="section-title" style={{ marginTop: 22 }}>运行额度</h3>
        <div className="form-grid">
          <Field label="同时进行的任务" hint="回测、优化等排队 + 运行中的上限">{num('max_concurrent_runs')}</Field>
          <Field label="每日运行次数">{num('max_runs_per_day')}</Field>
          <Field label="单次运行最多 bar 数">{num('max_bars_per_run')}</Field>
          <Field label="同时运行的纸面模拟">{num('max_paper_runs')}</Field>
        </div>
        <label className="checkbox" style={{ display: 'flex', gap: 8, marginTop: 14, fontSize: 13 }}>
          <input type="checkbox" checked={q.allow_python_strategies} disabled={!isAdmin}
            onChange={(e) => setQ({ ...q, allow_python_strategies: e.target.checked })} />
          允许 Python 策略（在隔离子进程中运行：清空环境变量、资源上限、降权、断网）。关闭后只能使用规则 DSL。
        </label>
        {isAdmin ? <div className="button-row" style={{ marginTop: 16 }}><button className="button primary" onClick={save}>保存</button></div>
          : <Callout tone="warn">只有工作区管理员可以修改设置。</Callout>}
      </div>
      <div>
        <h3 className="section-title">我的工作区</h3>
        <ul className="checklist">
          {me.workspaces.map((w) => <li key={w.id}>{w.name} <span className={`role-chip ${w.role}`}>{ROLE_LABEL[w.role]}</span></li>)}
        </ul>
        <div className="form-grid" style={{ marginTop: 16 }}>
          <Field label="新建工作区"><input value={newWs} onChange={(e) => setNewWs(e.target.value)} placeholder="名称" /></Field>
          <Field label="凭邀请码加入"><input value={code} onChange={(e) => setCode(e.target.value.trim())} placeholder="邀请码" /></Field>
        </div>
        <div className="button-row" style={{ marginTop: 10 }}>
          <button className="button small" disabled={!newWs} onClick={async () => {
            try { await v4.createWorkspace(newWs); setNewWs(''); notify('已创建'); onSaved() } catch (e) { notify((e as Error).message, true) }
          }}>创建</button>
          <button className="button small" disabled={!code} onClick={async () => {
            try { await v4.joinWorkspace(code); setCode(''); notify('已加入'); onSaved() } catch (e) { notify((e as Error).message, true) }
          }}>加入</button>
        </div>
      </div>
      <UsagePanel />
    </div>
  )
}

/**
 * T07 用量面板：显示每个计量项的已用 / 上限 / 占比。
 *
 * 数值全部来自服务端（十进制字符串），前端**只做展示、不做浮点运算**——
 * 占比也由后端算好，因为浮点会让 99.9% 显示成 100%，看起来像已耗尽。
 * 用量按工作区计量，服务端从会话上下文取工作区，前端无法指定，所以看不到别人的用量。
 */
/**
 * 字节数的展示格式。**只影响显示**：后端给的是十进制字符串，这里除以 1024 只为可读，
 * 页面上仍同时保留原始字节数（`title`），避免"看起来约等于"掩盖真实用量。
 */
function fmtBytes(v: string): { text: string; exact: string } {
  const n = Number(v)
  if (!Number.isFinite(n) || n < 1024) return { text: `${v} 字节`, exact: `${v} 字节` }
  const units = ['KB', 'MB', 'GB', 'TB']
  let x = n / 1024
  let i = 0
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1 }
  return { text: `${x.toFixed(1)} ${units[i]}`, exact: `${v} 字节` }
}

function UsagePanel() {
  const [data, setData] = useState<UsagePanelData | null>(null)
  const [err, setErr] = useState('')
  const load = useCallback(() => {
    v4.usage(30).then((r) => { setData(r); setErr('') }).catch((e) => setErr((e as Error).message))
  }, [])
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [load])

  if (err) return <Panel title="用量"><Callout tone="fail">用量加载失败：{err}<button className="button small ghost" style={{ marginLeft: 10 }} onClick={load}>重试</button></Callout></Panel>
  if (!data) return <Panel title="用量"><Loading /></Panel>
  return (
    <Panel title="用量" subtitle={`按工作区计量（${data.day}，UTC 自然日）· 跨工作区互不可见`}>
      <div className="table-wrap">
        <table>
          <thead><tr><th>计量项</th><th className="right">已用</th><th className="right">上限</th><th className="right">占比</th><th>口径</th></tr></thead>
          <tbody>
            {data.items.map((it) => {
              const isBytes = it.unit === '字节'
              const used = isBytes ? fmtBytes(it.used) : { text: `${it.used} ${it.unit}`, exact: `${it.used} ${it.unit}` }
              return (
                <tr key={it.metric}>
                  <td>{it.label}</td>
                  <td className="right mono" title={used.exact}>{used.text}</td>
                  <td className="right mono">{it.limit === null ? '不限' : (isBytes ? fmtBytes(it.limit).text : it.limit)}</td>
                  <td className="right">
                    {it.used_percent === null ? '—' : (
                      <span className={it.exhausted ? 'negative' : undefined}>
                        {it.used_percent}%{it.exhausted && <> <Tag tone="fail">已耗尽</Tag></>}
                      </span>
                    )}
                  </td>
                  <td className="muted">{it.period === 'daily' ? '当日' : '累计'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="hint" style={{ marginTop: 10 }}>{data.note}</p>
    </Panel>
  )
}

function MembersTab({ me, isAdmin, notify }: { me: Me; isAdmin: boolean; notify: PageProps['notify'] }) {
  const [data, setData] = useState<{ members: Member[]; invites: Invite[] } | null>(null)
  const [role, setRole] = useState<Role>('member')
  const [email, setEmail] = useState('')
  const [created, setCreated] = useState<Invite | null>(null)
  const load = useCallback(() => { v4.members().then(setData).catch((e) => notify(e.message, true)) }, [notify])
  useEffect(load, [load])
  if (!data) return <Loading />
  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try { await fn(); notify(ok); load() } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th>成员</th><th>邮箱</th><th>角色</th><th>加入时间</th><th>最近登录</th>{isAdmin && <th />}</tr></thead>
          <tbody>
            {data.members.map((m) => (
              <tr key={m.user_id}>
                <td>{m.name || '—'}{m.user_id === me.user.id && <small> （我）</small>}</td>
                <td className="mono">{m.email}</td>
                <td>
                  {isAdmin ? (
                    <select value={m.role} onChange={(e) => act(() => v4.setRole(m.user_id, e.target.value as Role), '角色已更新')}>
                      {(['admin', 'member', 'reviewer'] as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
                    </select>
                  ) : <span className={`role-chip ${m.role}`}>{ROLE_LABEL[m.role]}</span>}
                </td>
                <td>{fmtTime(m.created_at)}</td>
                <td>{m.last_login_at ? fmtTime(m.last_login_at) : '—'}</td>
                {isAdmin && <td className="right">{m.user_id !== me.user.id && (
                  <button className="button small danger" onClick={() => window.confirm(`移出 ${m.email}？`) && act(() => v4.removeMember(m.user_id), '已移出')}>移出</button>
                )}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {isAdmin && (
        <>
          <h3 className="section-title" style={{ marginTop: 22 }}>邀请成员</h3>
          <div className="field-row" style={{ maxWidth: 640 }}>
            <Field label="角色"><select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {(['member', 'reviewer', 'admin'] as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
            </select></Field>
            <Field label="限定邮箱（可选）"><input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="只允许该邮箱使用" /></Field>
          </div>
          <div className="button-row" style={{ marginTop: 10 }}>
            <button className="button primary small" onClick={async () => {
              try { const r = await v4.invite(role, email); setCreated(r.invite); setEmail(''); load() } catch (e) { notify((e as Error).message, true) }
            }}>生成邀请码</button>
          </div>
          {data.invites.length > 0 && (
            <div className="table-wrap" style={{ marginTop: 14 }}>
              <table>
                <thead><tr><th>邀请</th><th>角色</th><th>限定邮箱</th><th>到期</th><th>状态</th><th /></tr></thead>
                <tbody>
                  {data.invites.map((i) => (
                    <tr key={i.id}>
                      <td className="mono">{i.id}</td><td>{ROLE_LABEL[i.role]}</td><td>{i.email ?? '—'}</td><td>{fmtTime(i.expires_at)}</td>
                      <td>{i.used_at ? '已使用' : '有效'}</td>
                      <td className="right">{!i.used_at && <button className="button small" onClick={() => act(() => v4.revokeInvite(i.id), '已撤销')}>撤销</button>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      {created && (
        <Modal title="邀请码（只显示这一次）" onClose={() => setCreated(null)} width={520}>
          <p className="hint" style={{ marginTop: 0 }}>请通过安全渠道发给对方，7 天内有效。平台只保存它的哈希，关闭后无法再次查看。</p>
          <div className="secret-once">{created.code}</div>
          <p className="hint">对方在登录页选择“凭邀请码注册”，或已有账号的在“设置 → 工作区”里加入。</p>
        </Modal>
      )}
    </>
  )
}

function RunsTab({ notify, onNavigate, readonly }: { notify: PageProps['notify']; onNavigate: (k: string) => void; readonly: boolean }) {
  const [runs, setRuns] = useState<Run[] | null>(null)
  const [mode, setMode] = useState('')
  const [open, setOpen] = useState<Run | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])
  const load = useCallback(() => {
    v4.runs({ mode }).then((r) => setRuns(r.items)).catch((e) => notify(e.message, true))
  }, [mode, notify])
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t) }, [load])
  useEffect(() => { if (open) v4.runEvents(open.id).then((r) => setEvents(r.items)).catch(() => setEvents([])) }, [open])
  const cmd = async (r: Run, c: 'pause' | 'resume' | 'cancel' | 'liquidate_and_stop') => {
    try { await v4.runCommand(r.id, c); notify('命令已提交'); load() } catch (e) { notify((e as Error).message, true) }
  }
  if (!runs) return <Loading />
  return (
    <>
      <div className="button-row" style={{ marginBottom: 12 }}>
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          <option value="">全部模式</option>
          {Object.entries(MODE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <span className="hint" style={{ margin: 0 }}>长任务都由独立 worker 执行；每个运行同时只有一个 worker 持有租约，崩溃后从检查点恢复。</span>
      </div>
      {runs.length === 0 ? <EmptyState title="还没有运行记录" /> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>运行</th><th>类型</th><th>模式</th><th>状态</th><th>进度</th><th>认领</th><th>创建</th><th /></tr></thead>
            <tbody>
              {runs.map((r) => {
                const [label, cls] = RUN_STATE[r.state]
                return (
                  <tr key={r.id}>
                    <td><button className="button ghost small" onClick={() => setOpen(r)}>{r.title || r.id}</button></td>
                    <td>{KIND_LABEL[r.kind] ?? r.kind}</td>
                    <td><Tag tone={r.mode === 'testnet' ? 'warn' : undefined}>{MODE_LABEL[r.mode] ?? r.mode}</Tag></td>
                    <td><span className={`table-pill ${cls}`}>{label}</span>{r.error && <small title={r.error}> {r.error_code}</small>}</td>
                    <td>{r.progress.total ? `${r.progress.done}/${r.progress.total}` : '—'}</td>
                    <td>{r.attempts}</td>
                    <td>{fmtTime(r.created_at)}</td>
                    <td className="right">
                      {!readonly && !r.terminal && r.kind === 'paper' && (r.state === 'PAUSED'
                        ? <button className="button small" onClick={() => cmd(r, 'resume')}>恢复</button>
                        : <button className="button small" onClick={() => cmd(r, 'pause')}>暂停</button>)}
                      {!readonly && !r.terminal && <button className="button small danger" onClick={() => cmd(r, 'cancel')}>取消</button>}
                      {r.kind === 'paper' && <button className="button small ghost" onClick={() => onNavigate(`paper/${r.id}`)}>打开</button>}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {open && (
        <Modal title={open.title || open.id} onClose={() => setOpen(null)} width={760}>
          <dl className="kv">
            <dt>运行 ID</dt><dd className="mono">{open.id}</dd>
            <dt>状态</dt><dd>{RUN_STATE[open.state][0]}（第 {open.attempts} 次认领）</dd>
            <dt>策略版本</dt><dd className="mono">{open.strategy_version_id ?? '—'}</dd>
            <dt>租约</dt><dd className="mono">{open.lease_owner ?? '—'}</dd>
            {open.error && <><dt>错误</dt><dd>{open.error_code}：{open.error}</dd></>}
          </dl>
          <h3 className="section-title" style={{ marginTop: 16 }}>事件流</h3>
          <div className="table-wrap" style={{ maxHeight: 320, overflow: 'auto' }}>
            <table>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}><td className="mono">{e.id}</td><td>{fmtTime(e.ts)}</td><td>{e.type}</td>
                    <td className="mono wrap" style={{ fontSize: 11.5 }}>{JSON.stringify(e.data).slice(0, 220)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </Modal>
      )}
    </>
  )
}

function AuditTab({ notify }: { notify: PageProps['notify'] }) {
  const [items, setItems] = useState<AuditItem[] | null>(null)
  const [action, setAction] = useState('')
  useEffect(() => { v4.audit(action || undefined).then((r) => setItems(r.items)).catch((e) => notify(e.message, true)) }, [action, notify])
  return (
    <>
      <div className="button-row" style={{ marginBottom: 12 }}>
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">全部操作</option>
          {['user.', 'member.', 'workspace.', 'run.', 'strategy.', 'account.', 'live.', 'config.'].map((a) => <option key={a} value={a}>{a}*</option>)}
        </select>
        <span className="hint" style={{ margin: 0 }}>审计记录写入前统一过滤凭证类字段，导出与错误信息中不含明文密钥。</span>
      </div>
      {!items ? <Loading /> : items.length === 0 ? <EmptyState title="暂无审计记录" /> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>时间 UTC</th><th>操作者</th><th>操作</th><th>对象</th><th>IP</th><th>trace_id</th><th>详情</th></tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.id}>
                  <td>{fmtTime(i.ts)}</td><td>{i.user_name ?? '—'}</td><td className="mono">{i.action}</td>
                  <td className="mono">{i.object_type}{i.object_id ? `:${i.object_id}` : ''}</td><td className="mono">{i.ip}</td>
                  <td className="mono">{i.trace_id}</td>
                  <td className="mono wrap" style={{ fontSize: 11.5 }}>{Object.keys(i.details).length ? JSON.stringify(i.details) : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

function SystemTab({ notify }: { notify: PageProps['notify'] }) {
  const [net, setNet] = useState<NetworkStatus | null>(null)
  const [workers, setWorkers] = useState<Awaited<ReturnType<typeof v4.workers>> | null>(null)
  const [probing, setProbing] = useState(false)
  useEffect(() => {
    v4.network().then(setNet).catch((e) => notify(e.message, true))
    v4.workers().then(setWorkers).catch(() => undefined)
  }, [notify])
  const VERDICT: Record<string, [string, string]> = {
    PENDING: ['待验证', 'pending'], AVAILABLE: ['可用', ''], DEGRADED: ['不稳定', 'pending'], UNREACHABLE: ['不可达', 'fail'],
  }
  return (
    <div className="two-columns even">
      <Panel title="网络可用性实测（72 小时）" subtitle={net?.note} actions={
        <button className="button small" disabled={probing} onClick={async () => {
          setProbing(true)
          try { setNet((await v4.probe()).status) } catch (e) { notify((e as Error).message, true) } finally { setProbing(false) }
        }}>立即探测</button>
      }>
        {!net ? <Loading /> : (
          <>
            <Callout tone={net.overall === 'AVAILABLE' ? 'info' : 'warn'} title={net.label}>
              worker 每 5 分钟探测一次各行情接口；连续 72 小时成功率 ≥ 99% 才判定为可用。
            </Callout>
            {net.providers.length === 0 ? <p className="hint">还没有探测记录：启动 worker 或点击“立即探测”。</p> : (
              <table>
                <thead><tr><th>接口</th><th>判定</th><th>已观测</th><th>72h 成功率</th><th>中位延迟</th></tr></thead>
                <tbody>
                  {net.providers.map((p) => (
                    <tr key={p.provider}>
                      <td>{p.provider}</td><td><span className={`table-pill ${VERDICT[p.verdict]?.[1] ?? ''}`}>{VERDICT[p.verdict]?.[0] ?? p.verdict}</span></td>
                      <td>{p.observed_hours} 小时</td><td>{p.success_rate_72h === null ? '—' : `${(p.success_rate_72h * 100).toFixed(2)}%`}</td>
                      <td>{p.p50_latency_ms ?? '—'} ms</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </Panel>
      <Panel title="worker 与策略隔离">
        {!workers ? <Loading /> : (
          <>
            <dl className="kv">
              <dt>执行方式</dt><dd>{workers.mode === 'inline' ? '进程内（开发 / 测试）' : '独立 worker 进程'}</dd>
              <dt>唤醒</dt><dd>{workers.wake === 'redis' ? 'Redis 即时唤醒' : '数据库轮询（未配置 Redis）'}</dd>
              <dt>在线 worker</dt><dd>{workers.workers.length}</dd>
            </dl>
            {workers.workers.map((w) => <div key={w.id} className="hint mono">{w.id} · 心跳 {fmtTime(w.last_seen)}</div>)}
            {workers.mode !== 'inline' && workers.workers.length === 0 && (
              <Callout tone="warn">没有在线的 worker：回测与纸面模拟会停在“排队中”。请运行 <code className="inline">qagent worker</code> 或 docker compose 中的 worker 服务。</Callout>
            )}
          </>
        )}
      </Panel>
    </div>
  )
}

function AccountTab({ me, notify }: { me: Me; notify: PageProps['notify'] }) {
  const [oldPw, setOld] = useState('')
  const [newPw, setNew] = useState('')
  return (
    <div style={{ maxWidth: 460 }}>
      <dl className="kv" style={{ marginBottom: 18 }}>
        <dt>邮箱</dt><dd>{me.user.email}</dd>
        <dt>名称</dt><dd>{me.user.name}</dd>
        <dt>部署管理员</dt><dd>{me.user.is_admin ? '是（可配置模型服务）' : '否'}</dd>
      </dl>
      <Field label="当前密码"><input type="password" value={oldPw} onChange={(e) => setOld(e.target.value)} /></Field>
      <div style={{ height: 10 }} />
      <Field label="新密码" hint="修改后所有会话失效，需要重新登录"><input type="password" value={newPw} onChange={(e) => setNew(e.target.value)} /></Field>
      <div className="button-row" style={{ marginTop: 12 }}>
        <button className="button primary" disabled={!oldPw || newPw.length < 8} onClick={async () => {
          try { await v4.changePassword(oldPw, newPw); notify('密码已修改，请重新登录'); window.location.reload() } catch (e) { notify((e as Error).message, true) }
        }}>修改密码</button>
      </div>
      <TotpSection notify={notify} />
    </div>
  )
}

/**
 * T11 双因素认证（TOTP）。
 *
 * 三步：开始启用（拿到密钥/otpauth 链接，此时**还没启用**）→ 用一次真实验证码确认 →
 * 一次性显示恢复码。明文密钥与恢复码都只出现一次，之后平台只保留密文与哈希，
 * 所以这里必须把"只显示这一次"讲清楚，否则用户关掉页面就再也拿不到了。
 *
 * 关闭需要同时给密码与验证码：少了任何一个，拿到一个未锁屏的浏览器就能关掉 2FA。
 */
function TotpSection({ notify }: { notify: PageProps['notify'] }) {
  const [st, setSt] = useState<TotpStatus | null>(null)
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null)
  const [code, setCode] = useState('')
  const [codes, setCodes] = useState<string[] | null>(null)
  const [pw, setPw] = useState('')
  const [offCode, setOffCode] = useState('')
  const [err, setErr] = useState('')
  const load = useCallback(() => {
    v4.totpStatus().then((r) => setSt(r.totp)).catch((e) => setErr((e as Error).message))
  }, [])
  useEffect(load, [load])

  const begin = async () => {
    setErr(''); setCodes(null)
    try { setSetup(await v4.totpSetup()) } catch (e) { setErr((e as Error).message) }
  }
  const confirm = async () => {
    setErr('')
    try {
      const r = await v4.totpEnable(code)
      setCodes(r.recovery_codes); setSetup(null); setCode(''); load()
    } catch (e) { setErr((e as Error).message) }
  }
  const turnOff = async () => {
    setErr('')
    try {
      await v4.totpDisable(pw, offCode)
      setPw(''); setOffCode(''); setCodes(null); notify('双因素认证已关闭'); load()
    } catch (e) { setErr((e as Error).message) }
  }
  const regen = async () => {
    setErr('')
    try { const r = await v4.totpRecoveryCodes(); setCodes(r.recovery_codes); load() } catch (e) { setErr((e as Error).message) }
  }

  if (!st) return <Panel title="双因素认证"><Loading /></Panel>
  return (
    <Panel title="双因素认证"
      subtitle={st.enabled
        ? `已启用（剩余恢复码 ${st.recovery_codes_left} 个）`
        : '用验证器（Google Authenticator、1Password 等）生成一次性验证码'}>
      {st.setup_required && (
        <Callout tone="warn">部署要求工作区管理员启用双因素认证，请尽快完成绑定。</Callout>
      )}
      {err && <Callout tone="fail">{err}</Callout>}
      {codes && (
        <Callout tone="warn">
          <b>恢复码只显示这一次</b>，请立刻离线保存。每个恢复码只能使用一次。
          <div className="mono" style={{ marginTop: 8, lineHeight: 1.9 }}>
            {codes.map((c) => <div key={c}>{c}</div>)}
          </div>
        </Callout>
      )}

      {!st.enabled && !setup && (
        <div className="button-row"><button className="button primary" onClick={begin}>开始启用</button></div>
      )}

      {!st.enabled && setup && (
        <div>
          <p className="hint">用验证器扫描下面的链接，或手工录入密钥（密钥只显示这一次）：</p>
          <div className="mono" style={{ wordBreak: 'break-all', fontSize: 12, margin: '8px 0' }}>{setup.otpauth_uri}</div>
          <Field label="密钥"><input className="mono" readOnly value={setup.secret} /></Field>
          <Field label="验证器显示的 6 位验证码" hint="确认成功前不会启用，扫错码不会把你锁在门外">
            <input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value.trim())} />
          </Field>
          <div className="button-row" style={{ marginTop: 12 }}>
            <button className="button primary" disabled={!code} onClick={confirm}>确认并启用</button>
            <button className="button ghost" onClick={() => { setSetup(null); setCode(''); setErr('') }}>取消</button>
          </div>
        </div>
      )}

      {st.enabled && (
        <div>
          {st.locked_until && st.locked_until > Date.now() && (
            <Callout tone="fail">因验证码连续错误，双因素校验已锁定至 {fmtTime(st.locked_until)}。</Callout>
          )}
          <div className="button-row"><button className="button small" onClick={regen}>重新生成恢复码</button></div>
          <h3 className="section-title" style={{ marginTop: 20, fontSize: 14 }}>关闭双因素</h3>
          <p className="hint">需要同时提供密码与当前验证码。</p>
          <Field label="当前密码"><input type="password" value={pw} onChange={(e) => setPw(e.target.value)} /></Field>
          <Field label="验证码或恢复码"><input value={offCode} onChange={(e) => setOffCode(e.target.value.trim())} /></Field>
          <div className="button-row" style={{ marginTop: 12 }}>
            <button className="button danger" disabled={!pw || !offCode} onClick={turnOff}>关闭双因素</button>
          </div>
        </div>
      )}
    </Panel>
  )
}
