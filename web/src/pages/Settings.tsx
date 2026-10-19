import { useCallback, useEffect, useRef, useState } from 'react'
import { fmtTime, v3, type LimitMetric, type LimitRule, type LimitWindow, type MemberModelPolicy, type ModelUsage, type ProviderQuotaCard, type QuotaRequest, type RecordRetention, type RecordTypeUsage, type ResearchRecordRow } from '../api/research'
import {
  KIND_LABEL, MODE_LABEL, RUN_STATE, v4,
  type Account, type AuditItem, type Invite, type Me, type Member, type NetworkStatus, type Role, type Run, type RunEvent,
  type TotpStatus, type UsageItem, type UsagePanel as UsagePanelData,
} from '../api/platform'
import { LimitBars, LimitEditor, MEMBER_METRICS, METRIC_LABEL, RecordComposition, UsageComposition, UsageSummary, WINDOWS, fmtCost, fmtTokens, fmtLimitValue, metricUnit, fmtStorageBytes, editorUnit, storageBytesFromInput, storageInputValue } from '../components/modelUsage'
import { Callout, EmptyState, Field, Heading, Loading, Modal, Panel, Tag } from '../components/ui'
import { SETTINGS_GROUPS } from '../components/SettingsShell'
import NodeTab from './NodeTab'
import QRCode from '../components/QRCode'
import LabPrefsTab from './settings/LabPrefs'
import PrivacyTab from './settings/Privacy'
import type { PageProps } from './types'
import { appConfirm, appPrompt } from '../components/dialog'
import { tData } from '../i18n'
import { admin, memberDefaults } from '../apiCollab'
import { useDebouncedValue } from '../useDebouncedValue'

type Tab = 'workspace' | 'quota' | 'retention' | 'members' | 'runs' | 'node' | 'audit' | 'system' | 'account' | 'lab' | 'privacy'
// 从设置侧栏的单一导航定义派生页面标签，避免再次出现“页面已实现但入口没加”的漂移。
const TABS: { key: Tab; label: string }[] = SETTINGS_GROUPS
  .flatMap((group) => group.items)
  .filter((item) => item.key.startsWith('settings/'))
  .map((item) => ({ key: item.key.slice('settings/'.length) as Tab, label: item.label }))
const ROLE_LABEL: Record<Role, string> = { admin: '管理员', member: '成员', reviewer: '审阅者（只读）' }
const INVITE_STATUS_LABEL: Record<Invite['status'], string> = { active: '可使用', used: '已用尽', expired: '已过期', revoked: '已撤销' }
const INVITE_STATUS_TONE: Record<Invite['status'], string> = { active: 'ok', used: '', expired: 'warn', revoked: 'fail' }
const INVITE_EXPIRY_OPTIONS = [
  { hours: 1, label: '1 小时' },
  { hours: 6, label: '6 小时' },
  { hours: 24, label: '1 天' },
  { hours: 72, label: '3 天' },
  { hours: 168, label: '7 天' },
  { hours: 720, label: '30 天' },
  { hours: 2160, label: '90 天' },
]
function inviteRemainingText(i: Invite) {
  if (i.status !== 'active') return ''
  const hours = Math.max(1, Math.ceil((i.remaining_ms ?? Math.max(0, i.expires_at - Date.now())) / 3_600_000))
  return hours >= 48 ? `剩余约 ${Math.ceil(hours / 24)} 天` : `剩余约 ${hours} 小时`
}

function inviteRemainingUses(i: Invite): string {
  if (i.max_uses === 0) return '不限'
  return `${Math.max(0, i.remaining_uses ?? i.max_uses - i.invited_count)} 人`
}

export default function SettingsPage({ tab, me, onNavigate, notify, onMeChanged }: PageProps & { tab?: string; me: Me; onMeChanged: () => void }) {
  const current = (TABS.find((t) => t.key === tab)?.key ?? 'workspace') as Tab
  const isAdmin = me.workspace?.role === 'admin'
  return (
    <div className="settings-page">
      <Heading
        title={TABS.find((t) => t.key === current)!.label}
        description={current === 'account' ? undefined : current === 'lab' ? '只保存在这台设备的浏览器里，和对话框底栏里的开关同步' : <>工作区「{me.workspace?.name}」· 你的角色：{me.workspace?.is_primary_admin ? '主管理员' : ROLE_LABEL[me.workspace?.role ?? 'member']}。</>}
      />
      {current === 'workspace' && <WorkspaceTab me={me} isAdmin={isAdmin} notify={notify} onSaved={onMeChanged} />}
      {current === 'quota' && <QuotaTab me={me} isAdmin={isAdmin} notify={notify} onSaved={onMeChanged} onNavigate={onNavigate} />}
      {current === 'retention' && <RecordRetentionTab isAdmin={isAdmin} notify={notify} />}
      {current === 'members' && <MembersTab me={me} isAdmin={isAdmin} notify={notify} onNavigate={onNavigate} />}
      {current === 'runs' && <RunsTab notify={notify} onNavigate={onNavigate} readonly={me.workspace?.role === 'reviewer'} />}
      {current === 'node' && <NodeTab notify={notify} isAdmin={isAdmin} onNavigate={onNavigate} />}
      {current === 'audit' && (isAdmin ? <AuditTab notify={notify} /> : <Callout tone="warn">只有工作区管理员可以查看审计日志。</Callout>)}
      {current === 'system' && <SystemTab notify={notify} />}
      {current === 'account' && <AccountTab me={me} notify={notify} onSaved={onMeChanged} />}
      {current === 'lab' && <LabPrefsTab notify={notify} />}
      {current === 'privacy' && (isAdmin ? <PrivacyTab me={me} isAdmin={isAdmin} notify={notify} onSaved={onMeChanged} /> : <Callout tone="warn">只有工作区管理员可以查看隐私与存储设置。</Callout>)}
    </div>
  )
}

function WorkspaceTab({ me, isAdmin, notify, onSaved }: { me: Me; isAdmin: boolean; notify: PageProps['notify']; onSaved: () => void }) {
  const ws = me.workspace!
  const [name, setName] = useState(ws.name)
  const [tz, setTz] = useState(ws.settings.timezone ?? 'UTC')
  const [newWs, setNewWs] = useState('')
  const [code, setCode] = useState('')
  const [featureData, setFeatureData] = useState<Awaited<ReturnType<typeof v4.workspaceFeatures>> | null>(null)
  const [featureBusy, setFeatureBusy] = useState('')
  const loadFeatures = useCallback(async () => {
    try { setFeatureData(await v4.workspaceFeatures()) } catch { /* 非管理员也应能读取状态 */ }
  }, [])
  useEffect(() => { loadFeatures() }, [loadFeatures])
  const toggleFeature = async (key: string, on: boolean) => {
    setFeatureBusy(key)
    try {
      const r = await v4.updateWorkspaceFeatures({ [key]: on })
      setFeatureData((cur) => cur ? { ...cur, enabled: r.enabled } : cur)
      notify(on ? '功能已开启' : '功能已关闭')
    } catch (e) { notify((e as Error).message, true) } finally { setFeatureBusy('') }
  }
  const save = async () => {
    try {
      await v4.updateSettings({ name, timezone: tz })
      notify('已保存')
      onSaved()
    } catch (e) { notify((e as Error).message, true) }
  }
  const featureRows = featureData ? Object.values(featureData.registry) : []
  const featureGroups = featureRows.reduce<Record<string, typeof featureRows>>((out, row) => {
    (out[row.group] ||= []).push(row)
    return out
  }, {})
  const statusTone: Record<string, string> = { ready: 'pass', research: 'warn', partial: 'warn', not_implemented: 'fail', blocked: 'fail' }
  return (
    <>
    <div className="two-columns anim-fade">
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
        {isAdmin ? <div className="button-row" style={{ marginTop: 16 }}><button className="button primary" onClick={save}>保存</button></div>
          : <Callout tone="warn">只有工作区管理员可以修改设置。</Callout>}
      </div>
      <div>
        <h3 className="section-title">我的工作区</h3>
        <ul className="checklist stagger">
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
    </div>
    <Panel title="功能开关与能力边界" subtitle="工作区管理员可以动态开关现货、永续与导入能力；未实现或已阻断的能力不会被开放，也不会用近似模型冒充。">
      {!featureData ? <Loading label="读取能力开关" /> : (
        <div className="feature-switches">
          {Object.entries(featureGroups).map(([group, rows]) => (
            <section key={group} className="feature-group">
              <h4>{group}</h4>
              {rows.map((row) => {
                const on = !!featureData.enabled[row.key]
                const locked = !isAdmin || row.implementation === 'not_implemented' || row.implementation === 'blocked'
                return (
                  <div className="feature-row" key={row.key}>
                    <div className="feature-copy">
                      <div><b>{row.label}</b> <Tag tone={statusTone[row.implementation]}>{row.implementation}</Tag></div>
                      {row.note && <small>{row.note}</small>}
                    </div>
                    <button type="button" role="switch" aria-checked={on} aria-label={`${row.label}${on ? '已开启' : '已关闭'}`}
                      className={`switch${on ? ' on' : ''}`} disabled={locked || featureBusy === row.key}
                      title={locked ? (isAdmin ? '该能力尚未实现或已阻断' : '只有工作区管理员可以修改') : ''}
                      onClick={() => toggleFeature(row.key, !on)}><i /></button>
                  </div>
                )
              })}
            </section>
          ))}
          <Callout tone="info">
            关闭永续后，Agent、回测、策略包导入和 Freqtrade 做空/杠杆入口都会在服务端拒绝。
            真实资金永续保持阻断；微实盘目前只支持 Binance / OKX 现货。
          </Callout>
        </div>
      )}
    </Panel>
    </>
  )
}

const STATUS_LABEL: Record<QuotaRequest['status'], [string, string]> = { pending: ['待处理', 'warn'], approved: ['已批准', 'pass'], denied: ['已拒绝', 'fail'] }
const ruleText = (r: LimitRule) =>
  `${WINDOWS.find(([k]) => k === r.window)?.[1]} ${METRIC_LABEL[r.metric]} ${fmtLimitValue(r.metric, r.limit)}${r.metric === 'cost' ? '' : metricUnit(r.metric)}`

const PROVIDER_STATE: Record<ProviderQuotaCard['state'], [string, string]> = {
  active: ['正常', 'pass'], near: ['接近上限', 'warn'], exhausted: ['已超限', 'fail'], missing: ['未配置密钥', 'warn'],
}

function ProviderQuotaCards({ cards, note, loading }: { cards: ProviderQuotaCard[]; note: string; loading: boolean }) {
  if (loading && !cards.length) return <Loading label="正在汇总模型来源额度" />
  if (!cards.length) return <Callout tone="warn">还没有可查询的模型来源，请先在「模型」里添加服务商。</Callout>
  return (
    <>
      <div className="pq-grid">
        {cards.map((c) => (
          <article className="pq-card anim-rise" key={`${c.scope}-${c.name}`}>
            <header className="pq-head">
              <div><b>{c.label}</b><small>{c.name} · {c.models.length} 个模型</small></div>
              <span className={`scope-tag ${c.scope}`}>{c.scope === 'workspace' ? '工作区' : '个人'}</span>
              <Tag tone={PROVIDER_STATE[c.state][1]}>{PROVIDER_STATE[c.state][0]}</Tag>
            </header>
            <div className="pq-billing">
              <span className={`pq-bill ${c.billing}`}>{c.billing === 'subscription' ? (c.remote?.available ? '订阅额度 · 实时' : '订阅额度') : 'API 按量计费'}</span>
              <span>均价 {c.avg_cost_per_call ? fmtCost(c.avg_cost_per_call) : '—'} / 次</span>
            </div>
            {c.remote?.available ? (
              <div className="pq-remote">
                <div className="pq-remote-head">
                  <span><i className={`pq-live-dot ${c.remote.windows.month?.status === 'ok' ? 'ok' : 'warn'}`} />OpenCode 实时额度</span>
                  <small>{c.remote.updated_at_text || fmtTime(c.remote.updated_at)} 更新</small>
                </div>
                <div className="pq-windows">
                  {(['5h', 'week', 'month'] as const).map((k) => {
                    const w = c.remote?.windows[k]
                    if (!w) return null
                    const pct = Math.max(0, Math.min(100, w.used_pct))
                    return (
                      <div key={k} className={`pq-window pq-window-quota ${pct >= 80 ? 'warn' : ''}`}>
                        <small>{w.label || ({ '5h': '5 小时', week: '本周', month: '本月' }[k])}</small>
                        <b>{Math.round(w.remaining_pct)}%</b>
                        <span>剩余 · 已用 {Math.round(w.used_pct)}%</span>
                        <div className="pq-meter" title={`已用 ${pct.toFixed(1)}%`}><i style={{ width: `${pct}%` }} /></div>
                        <span className="pq-reset">{w.resets_at_text ? `刷新 ${w.resets_at_text}` : '刷新时间未知'}</span>
                      </div>
                    )
                  })}
                </div>
                <div className="pq-remote-meta">
                  数据来自 OpenCode Go{c.remote.timezone ? ` · ${c.remote.timezone}` : ''}
                </div>
              </div>
            ) : (
              <>
                <div className="pq-windows">
                  {([['5h', '5 小时'], ['week', '本周'], ['month', '本月']] as const).map(([k, label]) => {
                    const w = c.windows[k]
                    return <div key={k} className="pq-window"><small>{label}</small><b>{fmtCost(w.cost)}</b><span>{w.calls} 次 · {fmtTokens(w.tokens)} token</span></div>
                  })}
                </div>
                {c.remote && !c.remote.available && <div className="pq-remote-error">实时额度暂不可用：{c.remote.reason || '服务商未返回数据'}</div>}
              </>
            )}
            {c.remote?.available && (
              <div className="pq-platform-line">
                平台记账：5 小时 {fmtCost(c.windows['5h'].cost)} · 本周 {fmtCost(c.windows.week.cost)} · 本月 {fmtCost(c.windows.month.cost)}
              </div>
            )}
            <div className="pq-total">
              <span>累计计费</span><b>{fmtCost(c.total.cost)}</b>
              <small>{c.total.calls.toLocaleString()} 次 · {fmtTokens(c.total.tokens)} token</small>
            </div>
            <div className="pq-mix">
              输入 {fmtTokens(c.total.input_tokens)} · 输出 {fmtTokens(c.total.output_tokens)}
              {c.total.input_tokens > 0 && <> · 缓存命中 {(c.total.cache_read_tokens / c.total.input_tokens * 100).toFixed(1)}%</>}
            </div>
            {c.limits.length > 0 && <LimitBars items={c.limits} compact />}
            <footer>最近使用：{c.last_used ? fmtTime(c.last_used) : '暂无调用'}</footer>
          </article>
        ))}
      </div>
      <p className="hint" style={{ marginTop: 10 }}>{note}</p>
    </>
  )
}

/**
 * 额度与用量：AI 模型额度——成员看自己的限额并申请提升，管理员审批（批准后自动更新该成员的限额）
 * 并查看整个工作区的模型用量与费用；下面是运行额度（管理员设置）与平台用量。
 */
function QuotaTab({ me, isAdmin, notify, onSaved, onNavigate }: {
  me: Me; isAdmin: boolean; notify: PageProps['notify']; onSaved: () => void; onNavigate: (k: string) => void
}) {
  const [q, setQ] = useState(me.workspace!.settings.quotas)
  const [mine, setMine] = useState<ModelUsage | null>(null)
  const [wsUsage, setWsUsage] = useState<ModelUsage | null>(null)
  const [requests, setRequests] = useState<QuotaRequest[]>([])
  const [providerCards, setProviderCards] = useState<ProviderQuotaCard[]>([])
  const [providerNote, setProviderNote] = useState('')
  const [providerLoading, setProviderLoading] = useState(true)
  const [ask, setAsk] = useState<{ metric: LimitMetric; window: LimitWindow; limit: string; reason: string }>({ metric: 'tokens', window: '1d', limit: '', reason: '' })
  const load = useCallback((refresh = false) => {
    v3.modelUsage(30, 'me').then(setMine).catch(() => undefined)
    v3.quotaRequests().then((r) => setRequests(r.requests)).catch(() => undefined)
    if (isAdmin) v3.modelUsage(30, 'workspace').then(setWsUsage).catch(() => undefined)
    setProviderLoading(true)
    v3.providerQuotas(refresh).then((r) => { setProviderCards(r.cards); setProviderNote(r.note) }).catch((e) => notify((e as Error).message, true)).finally(() => setProviderLoading(false))
  }, [isAdmin, notify])
  useEffect(() => { load(false) }, [load])
  const num = (k: keyof typeof q) => (
    <input type="number" min={0} value={Number(q[k])} disabled={!isAdmin} onChange={(e) => setQ({ ...q, [k]: Number(e.target.value) })} />
  )
  const saveQuotas = async () => {
    try { await v4.updateSettings({ quotas: q }); notify('运行额度已保存'); onSaved() } catch (e) { notify((e as Error).message, true) }
  }
  const submit = async () => {
    try {
      const limit = ask.metric === 'storage_bytes' ? storageBytesFromInput(ask.limit) : ask.limit
      if (!limit) throw new Error('请填写希望的上限')
      await v3.requestQuota({ ...ask, limit })
      setAsk({ ...ask, limit: '', reason: '' })
      notify('已提交，等待管理员审批')
      load()
    } catch (e) { notify((e as Error).message, true) }
  }
  const decide = async (r: QuotaRequest, approve: boolean) => {
    const note = await appPrompt(
      approve ? '批准这条额度申请？可以附一句给申请人的说明（可选）。' : '拒绝这条额度申请？可以说明原因（可选）。',
      '',
      { title: '处理额度申请', okLabel: approve ? '批准' : '拒绝', placeholder: '给申请人的回复（可选）' },
    )
    if (note === null) return
    try {
      await v3.decideQuota(r.id, approve, note)
      notify(approve ? `已批准，${r.user_name || '该成员'}的限额已更新` : '已拒绝')
      load()
    } catch (e) { notify((e as Error).message, true) }
  }

  return (
    <div className="quota-page anim-fade">
      <section className="set-section">
        <div className="set-section-head">
          <h3>模型来源额度</h3>
          <button className="button small ghost" disabled={providerLoading} onClick={() => load(true)}>{providerLoading ? '更新中…' : '更新'}</button>
        </div>
        <p className="set-section-note">OpenCode 来源显示服务商实时 5 小时 / 本周 / 本月剩余额度与刷新时间；其他来源按平台真实记账数据汇总 5 小时、本周、本月与累计调用。</p>
        <ProviderQuotaCards cards={providerCards} note={providerNote} loading={providerLoading} />
      </section>

      <section className="set-section">
        <div className="set-section-head">
          <h3>AI 模型额度</h3>
          <button className="button small ghost" onClick={() => onNavigate('models')}>模型设置 →</button>
        </div>
        <p className="set-section-note">
          {isAdmin
            ? '你是管理员：使用工作区共享模型不受成员限额约束（服务商自身的限额仍生效）。成员的模型权限与限额在「成员与邀请」里设置，成员提交的提升申请在下面审批。'
            : '这是你使用工作区共享模型的限额；用自己的服务商不受这里约束。额度不够时可以申请提升，管理员批准后立即生效。'}
        </p>
        {mine && (mine.my_limits.length ? <LimitBars items={mine.my_limits} /> : <div className="lim-empty">{isAdmin ? '管理员不受成员限额约束。' : '目前不限额。'}</div>)}
        {!isAdmin && me.workspace?.role !== 'reviewer' && (
          <div className="quota-ask anim-rise">
            <b>申请提升额度</b>
            <div className="quota-ask-row">
              <select value={ask.metric} onChange={(e) => {
                const metric = e.target.value as LimitMetric
                setAsk({ ...ask, metric, window: metric === 'storage_bytes' ? 'total' : ask.window })
              }} aria-label="计量方式">
                {MEMBER_METRICS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
              </select>
              <select value={ask.window} disabled={ask.metric === 'storage_bytes'} onChange={(e) => setAsk({ ...ask, window: e.target.value as LimitWindow })} aria-label="时间段">
                {WINDOWS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
              </select>
              <input type="number" min={0} value={ask.limit} onChange={(e) => setAsk({ ...ask, limit: e.target.value })}
                placeholder={`希望的上限（${editorUnit(ask.metric)}）`} aria-label="希望的上限" />
            </div>
            <textarea rows={2} value={ask.reason} maxLength={300} onChange={(e) => setAsk({ ...ask, reason: e.target.value })} placeholder="用途说明（可选），例如：本周要跑多轮策略调研" />
            <div className="button-row"><button className="button primary small" disabled={!(Number(ask.limit) > 0)} onClick={submit}>提交申请</button></div>
          </div>
        )}
        {requests.length > 0 ? (
          <div className="table-wrap" style={{ marginTop: 14 }}>
            <table>
              <thead><tr>{isAdmin && <th>成员</th>}<th>申请</th><th>说明</th><th>时间</th><th>状态</th>{isAdmin && <th />}</tr></thead>
              <tbody>
                {requests.map((r) => (
                  <tr key={r.id} className="anim-in">
                    {isAdmin && <td>{r.user_name || r.user_id}</td>}
                    <td className="mono">{ruleText(r)}</td>
                    <td className="muted">{r.reason || '—'}{r.note && <small className="invite-sub">回复：{r.note}</small>}</td>
                    <td>{fmtTime(r.created_ms)}</td>
                    <td><Tag tone={STATUS_LABEL[r.status][1]}>{STATUS_LABEL[r.status][0]}</Tag></td>
                    {isAdmin && (
                      <td className="right">{r.status === 'pending' && (
                        <span className="button-row tight">
                          <button className="button small primary" onClick={() => decide(r, true)}>批准</button>
                          <button className="button small ghost" onClick={() => decide(r, false)}>拒绝</button>
                        </span>
                      )}</td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : isAdmin && <p className="hint">没有额度申请。</p>}
      </section>

      {isAdmin && wsUsage && (
        <section className="set-section">
          <div className="set-section-head"><h3>工作区模型用量</h3></div>
          <p className="set-section-note">所有成员近 30 天的调用，按各服务商上填写的价格折算成人民币。</p>
          <UsageSummary data={wsUsage} showUsers />
        </section>
      )}
      {!isAdmin && mine && (
        <section className="set-section">
          <div className="set-section-head"><h3>我的模型用量</h3></div>
          <UsageSummary data={mine} />
        </section>
      )}

      <section className="set-section">
        <div className="set-section-head">
          <h3>运行额度</h3>
          {isAdmin && <button className="button small primary" onClick={saveQuotas}>保存</button>}
        </div>
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
        {!isAdmin && <Callout tone="warn">只有工作区管理员可以修改运行额度。</Callout>}
      </section>
      <UsagePanel isAdmin={isAdmin} notify={notify} />
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
function fmtBytesWithExact(v: string): { text: string; exact: string } {
  const n = Number(v)
  if (!Number.isFinite(n) || n < 1024) return { text: `${v} 字节`, exact: `${v} 字节` }
  const units = ['KB', 'MB', 'GB', 'TB']
  let x = n / 1024
  let i = 0
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1 }
  return { text: `${x.toFixed(1)} ${units[i]}`, exact: `${v} 字节` }
}

/** 字节类上限按 GB 编辑，其余按展示单位编辑；空值 = 不限制。 */
const GB = 1024 ** 3
function limitDraft(it: UsageItem): string {
  if (it.limit === null) return ''
  return it.unit === '字节' ? String(Number((Number(it.limit) / GB).toFixed(3))) : it.limit
}

function UsagePanel({ isAdmin, notify }: { isAdmin: boolean; notify: PageProps['notify'] }) {
  const [data, setData] = useState<UsagePanelData | null>(null)
  const [err, setErr] = useState('')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const load = useCallback(() => {
    v4.usage(30).then((r) => { setData(r); setErr('') }).catch((e) => setErr((e as Error).message))
  }, [])
  useEffect(() => { load(); const t = setInterval(load, 30000); return () => clearInterval(t) }, [load])

  const startEdit = () => {
    if (!data) return
    setDraft(Object.fromEntries(data.items.filter((it) => it.quota_key).map((it) => [it.quota_key, limitDraft(it)])))
    setEditing(true)
  }
  const save = async () => {
    if (!data) return
    const patch: Record<string, string | null> = {}
    for (const it of data.items) {
      if (!it.quota_key || !(it.quota_key in draft)) continue
      const raw = draft[it.quota_key].trim()
      if (raw === limitDraft(it)) continue
      if (!raw) { patch[it.quota_key] = null; continue }
      const n = Number(raw)
      if (!Number.isFinite(n) || n < 0) { notify(`「${it.label}」上限要填非负数字，留空表示不限制`, true); return }
      patch[it.quota_key] = it.unit === '字节' ? String(Math.round(n * GB)) : String(n)
    }
    if (!Object.keys(patch).length) { setEditing(false); return }
    setSaving(true)
    try {
      await v4.setUsageLimits(patch)
      notify(`已更新 ${Object.keys(patch).length} 项上限`)
      setEditing(false)
      load()
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setSaving(false)
    }
  }

  if (err) return <Panel title="用量"><Callout tone="fail">用量加载失败：{err}<button className="button small ghost" style={{ marginLeft: 10 }} onClick={load}>重试</button></Callout></Panel>
  if (!data) return <Panel title="用量"><Loading /></Panel>
  return (
    <Panel title="用量" subtitle={`按工作区计量（${data.day}，UTC 自然日）· 跨工作区互不可见`}
      actions={isAdmin ? (editing ? <>
        <button className="button small ghost" onClick={() => setEditing(false)} disabled={saving}>取消</button>
        <button className="button small" onClick={save} disabled={saving}>{saving ? '保存中…' : '保存上限'}</button>
      </> : <button className="button small ghost" onClick={startEdit}>自定义上限</button>) : undefined}>
      <div className="table-wrap">
        <table>
          <thead><tr><th>计量项</th><th className="right">已用</th><th className="right">上限{editing && <small className="muted">（留空 = 不限制）</small>}</th><th className="right">占比</th><th>口径</th></tr></thead>
          <tbody>
            {data.items.map((it) => {
              const isBytes = it.unit === '字节'
              const used = isBytes ? fmtBytesWithExact(it.used) : { text: `${it.used} ${it.unit}`, exact: `${it.used} ${it.unit}` }
              return (
                <tr key={it.metric}>
                  <td>{it.label}</td>
                  <td className="right mono" title={used.exact}>{used.text}</td>
                  <td className="right mono">
                    {editing && it.quota_key ? (
                      <span className="usage-limit-edit anim-fade">
                        <input type="number" min={0} step="any" value={draft[it.quota_key] ?? ''} placeholder="不限" aria-label={`${it.label}上限`}
                          onChange={(e) => setDraft({ ...draft, [it.quota_key]: e.target.value })} style={{ width: 120, textAlign: 'right' }} />
                        <small className="muted"> {isBytes ? 'GB' : it.unit}</small>
                      </span>
                    ) : it.limit === null ? '不限' : (isBytes ? fmtBytesWithExact(it.limit).text : it.limit)}
                  </td>
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
      <p className="hint" style={{ marginTop: 10 }}>{data.note}{isAdmin ? ' 管理员可点「自定义上限」调整每一项；改动记入审计日志。' : ' 上限由工作区管理员设定。'}</p>
    </Panel>
  )
}

function MembersTab({ me, isAdmin, notify, onNavigate }: {
  me: Me; isAdmin: boolean; notify: PageProps['notify']; onNavigate: (k: string) => void
}) {
  const [data, setData] = useState<{ members: Member[]; invites: Invite[] } | null>(null)
  const [mine, setMine] = useState<ModelUsage | null>(null)
  const [role, setRole] = useState<Role>('member')
  const [email, setEmail] = useState('')
  const [expiryHours, setExpiryHours] = useState(168)
  const [maxUses, setMaxUses] = useState(10)
  const [unlimitedUses, setUnlimitedUses] = useState(false)
  const [inviteFilter, setInviteFilter] = useState<'all' | Invite['status']>('all')
  const [created, setCreated] = useState<Invite | null>(null)
  const [inviteDetail, setInviteDetail] = useState<Invite | null>(null)
  const [registration, setRegistration] = useState<Awaited<ReturnType<typeof admin.settings>> | null>(null)
  const [policies, setPolicies] = useState<Record<string, MemberModelPolicy>>({})
  const [editing, setEditing] = useState<{ m: MemberModelPolicy; limits: LimitRule[] } | null>(null)
  const load = useCallback(() => {
    v4.members().then(setData).catch((e) => notify(e.message, true))
    v3.modelUsage(30, 'me').then(setMine).catch(() => undefined)
    if (isAdmin) v3.memberPolicies().then((r) => setPolicies(Object.fromEntries(r.members.map((m) => [m.user_id, m])))).catch(() => undefined)
  }, [notify, isAdmin])
  useEffect(load, [load])
  useEffect(() => {
    if (me.user.is_admin) admin.settings().then(setRegistration).catch(() => undefined)
  }, [me.user.is_admin])
  if (!data) return <Loading />
  const inviteCounts = data.invites.reduce<Record<Invite['status'], number>>((acc, i) => ({ ...acc, [i.status]: acc[i.status] + 1 }), { active: 0, used: 0, expired: 0, revoked: 0 })
  const totalInvited = data.invites.reduce((n, i) => n + i.invited_count, 0)
  const visibleInvites = inviteFilter === 'all' ? data.invites : data.invites.filter((i) => i.status === inviteFilter)
  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try { await fn(); notify(ok); load() } catch (e) { notify((e as Error).message, true) }
  }
  const toggle = (pol: MemberModelPolicy, key: 'use_workspace_models' | 'allow_personal_models') =>
    act(() => v3.setMemberPolicy(pol.user_id, { [key]: !pol[key] }), '模型权限已更新')
  const setStorageLimit = (bytes: string) => {
    if (!editing) return
    const limits = editing.limits.filter((r) => r.metric !== 'storage_bytes')
    if (bytes) limits.push({ metric: 'storage_bytes', window: 'total', limit: bytes })
    setEditing({ ...editing, limits })
  }
  return (
    <div className="anim-fade members-page">
      <p className="hint" style={{ marginTop: 0 }}>
        成员配额按“所有规则同时生效”计算：可以组合 token、费用、CPU 秒、回测次数、优化组合数与上传存储空间。
        {isAdmin ? ' 单独设置的成员使用自己的规则；没有单独设置的成员继承“新成员默认限额”。首个成员是主管理员，不可降级或移出。' : ' 你可以在下方查看自己的真实占用；完整模型用量在「额度与用量」中。'}
      </p>

      {mine && <MemberUsagePanel data={mine} isAdmin={isAdmin} onNavigate={onNavigate} />}

      <div className="table-wrap members-table-wrap">
        <table className={`settings-table members-table${isAdmin ? '' : ' compact'}`}>
          <thead>
            <tr>
              <th>成员</th><th>角色</th>
              {isAdmin && <><th>共享模型</th><th>个人服务商</th><th>存储占用</th><th>配额</th><th className="right">近 30 天模型</th></>}
              <th>最近登录</th>{isAdmin && <th />}
            </tr>
          </thead>
          <tbody className="stagger">
            {data.members.map((m) => {
              const pol = policies[m.user_id]
              return (
              <tr key={m.user_id}>
                <td className="member-cell" title={`加入于 ${fmtTime(m.created_at)}`}>
                  <b>{m.name || '—'}{m.user_id === me.user.id && <small> （我）</small>}</b>
                  {m.is_primary_admin && <Tag tone="ok">主管理员</Tag>}
                  <small className="mono">{m.email}</small>
                </td>
                <td>
                  {m.is_primary_admin ? <span className="role-chip admin">主管理员</span> : isAdmin ? (
                    <select value={m.role} onChange={(e) => act(() => v4.setRole(m.user_id, e.target.value as Role), '角色已更新')}>
                      {(['admin', 'member', 'reviewer'] as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
                    </select>
                  ) : <span className={`role-chip ${m.role}`}>{ROLE_LABEL[m.role]}</span>}
                </td>
                {isAdmin && (pol ? (
                  <>
                    <td>
                      {m.role === 'admin' ? <span className="muted">管理员</span> : (
                        <button type="button" role="switch" aria-checked={pol.use_workspace_models} aria-label="使用工作区共享模型"
                          className={`switch${pol.use_workspace_models ? ' on' : ''}`} onClick={() => toggle(pol, 'use_workspace_models')}><i /></button>
                      )}
                    </td>
                    <td>
                      <button type="button" role="switch" aria-checked={pol.allow_personal_models} aria-label="允许添加个人服务商"
                        className={`switch${pol.allow_personal_models ? ' on' : ''}`} onClick={() => toggle(pol, 'allow_personal_models')}><i /></button>
                    </td>
                    <td className="member-storage-cell">
                      <b>{fmtStorageBytes(pol.storage?.used_bytes ?? 0)}</b>
                      <small>{pol.storage?.file_count ?? 0} 个文件 · 含缩略图</small>
                      {pol.limit_status.some((s) => s.metric === 'storage_bytes' && s.exceeded) && <Tag tone="fail">已超限</Tag>}
                    </td>
                    <td>
                      {m.role === 'admin' ? <span className="muted">管理员不受成员配额约束</span> : (
                        <button type="button" className="quota-link" onClick={() => setEditing({ m: pol, limits: pol.limits })}>
                          <b>{pol.limits.length ? `${pol.limits.length} 条叠加规则` : '不限额'}</b>
                          <small>{pol.custom_limits ? '单独设置' : '继承默认'}</small>
                        </button>
                      )}
                      {pol.limit_status.some((s) => s.exceeded) && m.role !== 'admin' && <> <Tag tone="fail">已超额</Tag></>}
                    </td>
                    <td className="right mono" title={`${pol.usage_30d.calls} 次调用 · ${fmtTokens(pol.usage_30d.tokens)} token`}>{fmtCost(pol.usage_30d.cost)}</td>
                  </>
                ) : <><td /><td /><td /><td /><td /></>)}
                <td>{m.last_login_at ? fmtTime(m.last_login_at) : '—'}</td>
                {isAdmin && <td className="right">{m.user_id !== me.user.id && !m.is_primary_admin && (
                  <button className="button small danger" onClick={async () => await appConfirm(`移出 ${m.email}？`) && act(() => v4.removeMember(m.user_id), '已移出')}>移出</button>
                )}</td>}
              </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {isAdmin && <DefaultLimits notify={notify} />}
      {isAdmin && (
        <>
          <h3 className="section-title" style={{ marginTop: 22 }}>邀请码管理</h3>
          <p className="hint" style={{ marginTop: 0 }}>多用途邀请码；明文仅显示一次，可随时撤销。</p>
          {me.user.is_admin && registration && (
            <div className="registration-mode">
              <div>
                <b>注册方式</b>
                <small>开放注册时无需邀请码；邀请码仍可继续使用。</small>
              </div>
              <select value={registration.open_registration ? 'open' : 'invite'} onChange={async (e) => {
                const open = e.target.value === 'open'
                try {
                  setRegistration(await admin.saveSettings({ open_registration: open }))
                  notify(open ? '已开启开放注册' : '已改为凭邀请码注册')
                } catch (err) {
                  notify((err as Error).message, true)
                }
              }}>
                <option value="invite">凭邀请码注册</option>
                <option value="open">开放注册（无需邀请码）</option>
              </select>
            </div>
          )}
          <div className="invite-stats" aria-label="邀请码统计">
            <span><b>{inviteCounts.active}</b><small>可用邀请码</small></span>
            <span><b>{totalInvited}</b><small>已邀请人数</small></span>
            <span><b>{inviteCounts.used}</b><small>已用尽</small></span>
            <span><b>{inviteCounts.expired + inviteCounts.revoked}</b><small>失效或撤销</small></span>
          </div>
          <div className="field-row invite-create-row">
            <Field label="角色"><select value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {(['member', 'reviewer', 'admin'] as Role[]).map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
            </select></Field>
            <Field label="限定邮箱（可选）"><input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="只允许该邮箱使用" /></Field>
            <Field label="有效期"><select value={expiryHours} onChange={(e) => setExpiryHours(Number(e.target.value))}>
              {INVITE_EXPIRY_OPTIONS.map((o) => <option key={o.hours} value={o.hours}>{o.label}</option>)}
            </select></Field>
            <Field label="最多邀请人数">
              <div className="invite-limit-control">
                <input type="number" min={1} max={1000} step={1} value={maxUses} disabled={unlimitedUses}
                  onChange={(e) => setMaxUses(Math.max(1, Math.min(1000, Math.floor(Number(e.target.value) || 1))))} />
                <label className="check"><input type="checkbox" checked={unlimitedUses} onChange={(e) => setUnlimitedUses(e.target.checked)} />不限</label>
              </div>
            </Field>
          </div>
          <div className="button-row" style={{ marginTop: 10 }}>
            <button className="button primary small" onClick={async () => {
              try { const r = await v4.invite(role, email, expiryHours, unlimitedUses ? 0 : maxUses); setCreated(r.invite); setEmail(''); load() } catch (e) { notify((e as Error).message, true) }
            }}>生成邀请码</button>
          </div>
          {data.invites.length > 0 && (
            <>
              <div className="seg seg-sm invite-filter" role="tablist" aria-label="筛选邀请码">
                {(['all', 'active', 'used', 'expired', 'revoked'] as const).map((key) => (
                  <button key={key} type="button" role="tab" aria-selected={inviteFilter === key} onClick={() => setInviteFilter(key)}>
                    {key === 'all' ? '全部' : INVITE_STATUS_LABEL[key]}
                    {key !== 'all' && inviteCounts[key] > 0 ? ` ${inviteCounts[key]}` : ''}
                  </button>
                ))}
              </div>
              <div className="table-wrap invite-table-wrap">
                <table className="invite-table" data-mobile-cards="true">
                  <thead><tr><th>邀请码</th><th>角色</th><th>限定邮箱</th><th>有效期</th><th>已邀请人数</th><th>剩余名额</th><th>状态</th><th /></tr></thead>
                  <tbody>
                    {visibleInvites.map((i) => {
                      const inviteeNames = i.invitees.slice(0, 3).map((u) => u.name || u.email || '成员').join('、')
                      const extraInvitees = Math.max(0, i.invitees.length - 3)
                      return (
                        <tr key={i.id}>
                          <td className="mono mobile-card-title" data-label="邀请码" title={i.id}>{i.id.slice(0, 8)}<small className="invite-sub">{i.created_by_name ? `由 ${i.created_by_name} 创建` : ''}</small></td>
                          <td data-label="角色">{ROLE_LABEL[i.role]}</td>
                          <td data-label="限定邮箱">{i.email ?? '不限'}</td>
                          <td data-label="有效期">{fmtTime(i.expires_at)}<small className="invite-sub">{inviteRemainingText(i)}</small></td>
                          <td data-label="已邀请人数"><b>{i.invited_count} 人</b><small className="invite-sub">{inviteeNames || '尚未使用'}{extraInvitees ? ` 等 ${i.invitees.length} 人` : ''}</small></td>
                          <td data-label="剩余名额"><b>{inviteRemainingUses(i)}</b><small className="invite-sub">{i.max_uses === 0 ? '可继续邀请' : `上限 ${i.max_uses} 人`}</small></td>
                          <td className="mobile-card-leading" data-label="状态"><Tag tone={INVITE_STATUS_TONE[i.status]}>{INVITE_STATUS_LABEL[i.status]}</Tag></td>
                          <td className="right mobile-card-actions" data-label="操作">
                            <div className="invite-actions">
                              <button className="button small" onClick={() => setInviteDetail(i)}>详情</button>
                              {(i.status === 'active' || i.status === 'expired') && (
                                <button className="button small danger" onClick={() => act(() => v4.revokeInvite(i.id), '邀请码已撤销')}>撤销</button>
                              )}
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                    {!visibleInvites.length && <tr><td colSpan={8} className="muted">没有符合筛选条件的邀请码。</td></tr>}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
      {created && (
        <Modal title="邀请码（只显示这一次）" onClose={() => setCreated(null)} width={520}>
          <p className="hint" style={{ marginTop: 0 }}>请通过安全渠道发给对方；最多可邀请 {created.max_uses === 0 ? "不限人数" : `${created.max_uses} 人`}。有效期至 {fmtTime(created.expires_at)}，关闭后无法再次查看。</p>
          <div className="secret-once">{created.code}</div>
          <p className="hint">对方在登录页选择“凭邀请码注册”，或已有账号的在“设置 → 工作区”里加入。</p>
        </Modal>
      )}
      {inviteDetail && (
        <Modal title="邀请码详情" onClose={() => setInviteDetail(null)} width={620}>
          <div className="invite-detail-grid">
            <span><small>已邀请人数</small><b>{inviteDetail.invited_count} 人</b></span>
            <span><small>剩余名额</small><b>{inviteRemainingUses(inviteDetail)}</b></span>
            <span><small>最多邀请</small><b>{inviteDetail.max_uses === 0 ? '不限' : `${inviteDetail.max_uses} 人`}</b></span>
            <span><small>状态</small><b>{INVITE_STATUS_LABEL[inviteDetail.status]}</b></span>
            <span><small>有效期至</small><b>{fmtTime(inviteDetail.expires_at)}</b></span>
            <span><small>创建者</small><b>{inviteDetail.created_by_name || '—'}</b></span>
          </div>
          <p className="hint" style={{ marginTop: 0 }}>邀请码 <code>{inviteDetail.id}</code>。明文只在创建时显示一次，平台保存的是哈希。</p>
          <h4 className="invite-detail-title">已邀请成员 · {inviteDetail.invitees.length}</h4>
          {inviteDetail.invitees.length ? (
            <div className="invite-invitees">
              {inviteDetail.invitees.map((u) => (
                <div key={u.user_id} className="invite-invitee">
                  <span><b>{u.name || u.email || '成员'}</b><small>{u.email || u.user_id}</small></span>
                  <time>{fmtTime(u.used_at)}</time>
                </div>
              ))}
            </div>
          ) : <p className="hint">还没有人使用这个邀请码。</p>}
        </Modal>
      )}
      {editing && (
        <Modal title={`${editing.m.name || editing.m.email} · 配额设置`} onClose={() => setEditing(null)} width={660}>
          <p className="hint" style={{ marginTop: 0 }}>
            所有规则同时生效，任一规则超限即拒绝。存储配额按当前上传原文件与缩略图的合计占用计算；模型与计算类规则按各自时间窗口计算。
          </p>
          <div className="member-quota-overview">
            <span><small>上传存储</small><b>{fmtStorageBytes(editing.m.storage?.used_bytes ?? 0)}</b><em>{editing.m.storage?.file_count ?? 0} 个文件</em></span>
            <span><small>近 30 天模型调用</small><b>{editing.m.usage_30d.calls.toLocaleString()}</b><em>{fmtTokens(editing.m.usage_30d.tokens)} token · {fmtCost(editing.m.usage_30d.cost)}</em></span>
          </div>
          <LimitBars items={editing.m.limit_status} />
          <StorageQuotaQuick
            value={editing.limits.find((r) => r.metric === 'storage_bytes')?.limit ?? ''}
            usedBytes={editing.m.storage?.used_bytes ?? 0}
            fileCount={editing.m.storage?.file_count ?? 0}
            onChange={setStorageLimit}
          />
          <div className="lim-editor-head"><b>叠加限额</b><small>每条规则独立判定；同一计量方式可设置每日、每周、累计或滚动窗口。</small></div>
          <LimitEditor rules={editing.limits.filter((r) => r.metric !== 'storage_bytes')} metrics={MEMBER_METRICS.filter(([k]) => k !== 'storage_bytes')}
            onChange={(limits) => setEditing({
              ...editing,
              limits: [...limits, ...editing.limits.filter((r) => r.metric === 'storage_bytes')],
            })} />
          <div className="button-row" style={{ marginTop: 14, justifyContent: 'flex-end' }}>
            <button className="button" onClick={() => setEditing(null)}>取消</button>
            <button className="button primary" onClick={async () => {
              await act(() => v3.setMemberPolicy(editing.m.user_id, { limits: editing.limits.filter((r) => Number(r.limit) > 0) }), '配额已更新')
              setEditing(null)
            }}>保存</button>
          </div>
        </Modal>
      )}
    </div>
  )
}

function MemberUsagePanel({ data, isAdmin, onNavigate }: { data: ModelUsage; isAdmin: boolean; onNavigate: (k: string) => void }) {
  const manage = (metric: LimitMetric) => {
    if (metric === 'storage_bytes') return onNavigate(isAdmin ? 'settings/privacy' : 'settings/quota')
    if (metric === 'record_units') return onNavigate(isAdmin ? 'settings/retention' : 'settings/quota')
    return onNavigate('settings/quota')
  }
  return (
    <section className="set-section member-self-usage">
      <div className="set-section-head">
        <div><h3>我的用量</h3><p className="set-section-note">服务端按当前登录成员统计，其他人的用量不会在这里暴露。限额条显示已用与剩余比例。</p></div>
        <button className="button small ghost" onClick={() => onNavigate('settings/quota')}>额度与用量 →</button>
      </div>
      <div className="usage-kpis">
        <div className="usage-kpi anim-rise"><small>上传存储</small><b>{fmtStorageBytes(data.storage?.used_bytes ?? 0)}</b><em>{data.storage?.file_count ?? 0} 个文件（含缩略图）</em></div>
        <div className="usage-kpi anim-rise" style={{ animationDelay: '40ms' }}><small>近 30 天模型调用</small><b>{data.total.calls.toLocaleString()}</b><em>{fmtCost(data.total.cost)}</em></div>
        <div className="usage-kpi anim-rise" style={{ animationDelay: '80ms' }}><small>近 30 天 token</small><b>{fmtTokens(data.total.tokens)}</b><em>输入缓存命中 {data.total.cache_hit_rate == null ? '—' : `${(data.total.cache_hit_rate * 100).toFixed(1)}%`}</em></div>
        <div className="usage-kpi anim-rise" style={{ animationDelay: '120ms' }}><small>研究记录占用</small><b>{fmtStorageBytes(data.research_records?.approx_bytes ?? 0)}</b><em>{Number(data.research_records?.total_records ?? 0).toLocaleString()} 条 · {Number(data.research_records?.total_units ?? 0).toLocaleString()} / {Number(data.research_records?.max_units_per_user ?? 0).toLocaleString()} 单位</em></div>
        <div className="usage-kpi anim-rise" style={{ animationDelay: '160ms' }}><small>近 30 天计算任务</small><b>{Number(data.tasks?.backtests ?? 0).toLocaleString()} 次回测</b><em>CPU {Number(data.tasks?.cpu_seconds ?? 0).toFixed(1)} 秒 · 优化 {Number(data.tasks?.optimize_combinations ?? 0)} 组</em></div>
      </div>
      {data.research_records && <RecordComposition usage={data.research_records} />}
      <UsageComposition data={data} />
      {data.my_limits.length ? (
        <LimitBars items={data.my_limits} action={(s) => (
          <button type="button" className="button small ghost" title={isAdmin ? '打开对应设置' : '向管理员申请更多额度'} onClick={() => manage(s.metric)}>{isAdmin ? '管理' : '申请'}</button>
        )} />
      ) : (
        <p className="hint member-usage-note">{isAdmin ? '主管理员与管理员不受成员限额约束；可在「隐私与存储」设置自动清理。' : '当前没有设置限额；需要更多额度时可在「额度与用量」向管理员申请。'}</p>
      )}
      {!isAdmin && <div className="member-usage-manage"><button type="button" className="button small" onClick={() => onNavigate('settings/quota')}>申请提升额度</button><button type="button" className="button small ghost" onClick={() => onNavigate('settings/retention')}>清理研究记录</button></div>}
    </section>
  )
}

function StorageQuotaQuick({ value, usedBytes, fileCount, onChange, disabled }: {
  value: string; usedBytes: number; fileCount: number; onChange: (bytes: string) => void; disabled?: boolean
}) {
  const [draft, setDraft] = useState(() => storageInputValue(value))
  useEffect(() => { setDraft(storageInputValue(value)) }, [value])
  const presets: [string, number][] = [['512 MB', 512], ['1 GB', 1024], ['5 GB', 5120], ['10 GB', 10240]]
  return (
    <div className="storage-quota-box">
      <div className="storage-quota-head">
        <div><b>上传存储限额</b><small>当前 {fmtStorageBytes(usedBytes)} · {fileCount} 个文件；统计原文件 + 服务端缩略图</small></div>
        <span className="tag">存量配额</span>
      </div>
      <div className="storage-quota-row">
        <input type="number" min={0} step={1} inputMode="decimal" value={draft} disabled={disabled}
          onChange={(e) => { setDraft(e.target.value); onChange(storageBytesFromInput(e.target.value)) }}
          placeholder="例如 512" aria-label="上传存储上限（MB）" />
        <span>MB</span>
        <button type="button" className="button small ghost" disabled={disabled || !value} onClick={() => onChange('')}>不限额</button>
      </div>
      <div className="button-row tight">
        {presets.map(([label, gb]) => (
          <button type="button" key={label} className="button small ghost" disabled={disabled}
            onClick={() => onChange(String(Math.round(gb * 1024 ** 2)))}>{label}</button>
        ))}
      </div>
    </div>
  )
}

/** 新成员默认限额：邀请来的成员没有单独设过限额时，按这里计算（每日 / 每周 / 每月 / 累计 / 滚动）。 */
function DefaultLimits({ notify }: { notify: PageProps['notify'] }) {
  const [rules, setRules] = useState<LimitRule[] | null>(null)
  useEffect(() => { memberDefaults.get().then((r) => setRules(r.limits)).catch(() => setRules([])) }, [])
  if (!rules) return null
  return (
    <section className="set-section" style={{ marginTop: 22 }}>
      <h3 className="section-title">新成员默认限额</h3>
      <p className="hint" style={{ marginTop: 0 }}>没有单独设置过配额的成员继承这里；新成员默认研究记录 200 单位。多条规则会同时生效，例如「每日 ¥5 + 每周 ¥20 + 累计 ¥200 + 上传存储 2 GB + 研究记录 500 单位」。研究记录按回测、因子、模型、组合和执行产物的倍率折算；PIN 只防止自动清理/删除，不会减少已占用单位。单独设置过的成员以成员表格里的配额为准。</p>
      <LimitEditor rules={rules} metrics={MEMBER_METRICS} onChange={setRules} empty="不限额" />
      <button type="button" className="button small primary" style={{ marginTop: 8 }} onClick={async () => {
        try { setRules((await memberDefaults.save(rules.filter((r) => Number(r.limit) > 0))).limits); notify('默认配额已保存') } catch (e) { notify((e as Error).message, true) }
      }}>保存默认配额</button>
    </section>
  )
}

function RecordRetentionTab({ isAdmin, notify }: { isAdmin: boolean; notify: PageProps['notify'] }) {
  const [data, setData] = useState<RecordRetention | null>(null)
  const [records, setRecords] = useState<ResearchRecordRow[]>([])
  const [recordType, setRecordType] = useState('')
  const [query, setQuery] = useState('')
  const debouncedQuery = useDebouncedValue(query)
  const [recordsLoading, setRecordsLoading] = useState(false)
  const [recordsHasMore, setRecordsHasMore] = useState(false)
  const [recordsNext, setRecordsNext] = useState<{ time: number; id: string } | null>(null)
  const recordsRequest = useRef(0)
  const [autoDelete, setAutoDelete] = useState(false)
  const [perStrategy, setPerStrategy] = useState(5)
  const [autoStrategy, setAutoStrategy] = useState(true)
  const [adminMax, setAdminMax] = useState(100000)
  const [multipliers, setMultipliers] = useState<Record<string, number>>({})
  const [saving, setSaving] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const apply = (next: RecordRetention) => {
    setData(next)
    setAutoDelete(next.user.auto_delete_oldest)
    setPerStrategy(next.user.per_strategy_limit)
    setAutoStrategy(next.user.auto_delete_strategy_oldest)
    setAdminMax(next.workspace.max_units_per_user)
    setMultipliers(Object.fromEntries(next.usage.types.map((item) => [item.key, item.multiplier])))
  }

  const loadData = useCallback(async () => {
    setSaving('load')
    try {
      apply(await v3.recordRetention())
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setSaving('')
    }
  }, [notify])

  const loadRecords = useCallback(async (cursor?: { time: number; id: string } | null, append = false) => {
    const request = ++recordsRequest.current
    setRecordsLoading(true)
    try {
      const page = await v3.recordRetentionRecords({
        type: recordType || undefined,
        q: debouncedQuery || undefined,
        before_time: cursor?.time,
        before_id: cursor?.id,
        limit: 100,
      })
      if (request !== recordsRequest.current) return
      setRecords((prev) => append ? [...prev, ...page.items] : page.items)
      setRecordsHasMore(page.has_more)
      setRecordsNext(page.next_cursor)
    } catch (e) {
      if (request === recordsRequest.current) notify((e as Error).message, true)
    } finally {
      if (request === recordsRequest.current) setRecordsLoading(false)
    }
  }, [debouncedQuery, notify, recordType])

  useEffect(() => { void loadData() }, [loadData])
  useEffect(() => { void loadRecords() }, [loadRecords])

  if (!data) return <Loading label="读取研究记录设置" />
  const u = data.usage
  const fmtUnits = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 2 })
  const fmtApprox = (bytes?: number) => bytes ? `约 ${(bytes / 1024).toFixed(0)} KB` : '—'
  const over = u.total_units > u.max_units_per_user
  const hardMax = data.workspace.hard_max_units ?? data.workspace.default_max_units ?? 100_000
  const reservedBytes = data.workspace.reserved_bytes ?? 0

  const saveUser = async () => {
    setSaving('user')
    try {
      apply(await v3.saveRecordRetention({
        auto_delete_oldest: autoDelete,
        per_strategy_limit: Math.max(1, Math.min(100, Math.round(perStrategy || 5))),
        auto_delete_strategy_oldest: autoStrategy,
      }))
      notify('研究记录清理设置已保存')
    } catch (e) { notify((e as Error).message, true) } finally { setSaving('') }
  }

  const saveWorkspace = async () => {
    setSaving('workspace')
    try {
      apply(await v3.saveRecordWorkspaceRetention(Math.max(1, Math.min(hardMax, Math.round(adminMax || 100000)))))
      notify('每名成员的研究记录单位上限已保存')
    } catch (e) { notify((e as Error).message, true) } finally { setSaving('') }
  }

  const saveMultipliers = async () => {
    setSaving('multipliers')
    try {
      const normalized = Object.fromEntries(
        Object.entries(multipliers).map(([key, value]) => [
          key,
          Math.round(Math.max(0.01, Math.min(20, Number(value) || 0.01)) * 1000) / 1000,
        ]),
      )
      await v3.saveRecordMultipliers(normalized)
      await Promise.all([loadData(), loadRecords()])
      notify('记录类型倍率已保存')
    } catch (e) { notify((e as Error).message, true) } finally { setSaving('') }
  }

  const cleanup = async () => {
    if (!await appConfirm('按当前设置立即删除超出记录单位上限的最旧未 PIN 记录？此操作会同时清理报告与结果文件，不能恢复。', { danger: true })) return
    setSaving('cleanup')
    try {
      const result = await v3.cleanupRecordRetention()
      await Promise.all([loadData(), loadRecords()])
      notify(result.deleted ? `已清理 ${result.deleted} 条记录` : '当前没有可自动清理的记录')
    } catch (e) { notify((e as Error).message, true) } finally { setSaving('') }
  }

  const togglePin = async (row: ResearchRecordRow) => {
    try {
      await v3.pinRetentionRecord(row.record_type, row.id)
      await Promise.all([loadData(), loadRecords()])
      notify(row.pinned ? '已取消 PIN 保护' : '已 PIN 保护')
    } catch (e) { notify((e as Error).message, true) }
  }

  const recordKey = (row: ResearchRecordRow) => `${row.record_type}:${row.id}`
  const selectedRows = records.filter((row) => selected.has(recordKey(row)))
  const allSelected = records.length > 0 && selectedRows.length === records.length
  const toggleSelect = (row: ResearchRecordRow) => setSelected((prev) => {
    const next = new Set(prev)
    const key = recordKey(row)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(records.map(recordKey)))
  const batchPin = async (pinned: boolean) => {
    if (!selectedRows.length) return
    try {
      const r = await v3.batchPinRetentionRecords(selectedRows.map((row) => ({ type: row.record_type, id: row.id })), pinned)
      setSelected(new Set())
      await Promise.all([loadData(), loadRecords()])
      notify(r.failed.length ? `已更新 ${r.updated.length} 条，${r.failed.length} 条失败` : `已${pinned ? 'PIN' : '取消 PIN'} ${r.updated.length} 条记录`)
    } catch (e) { notify((e as Error).message, true) }
  }
  const batchRemove = async () => {
    if (!selectedRows.length) return
    if (!await appConfirm(`批量删除 ${selectedRows.length} 条研究记录？报告、状态与结果文件会真实删除，不能恢复。`, { danger: true })) return
    try {
      const r = await v3.batchDeleteRetentionRecords(selectedRows.map((row) => ({ type: row.record_type, id: row.id })))
      setSelected(new Set())
      await Promise.all([loadData(), loadRecords()])
      notify(r.failed.length ? `已删除 ${r.deleted.length} 条，${r.failed.length} 条失败` : `已删除 ${r.deleted.length} 条记录`)
    } catch (e) { notify((e as Error).message, true) }
  }

  const remove = async (row: ResearchRecordRow) => {
    if (!await appConfirm(`删除「${row.record_title || row.id}」？报告与结果文件会一并清理，不能恢复。`, { danger: true })) return
    try {
      await v3.deleteRetentionRecord(row.record_type, row.id)
      await Promise.all([loadData(), loadRecords()])
      notify('记录已删除')
    } catch (e) { notify((e as Error).message, true) }
  }

  return (
    <div className="quota-page anim-fade">
      <section className="set-section">
        <div className="set-section-head"><h3>研究记录总览</h3><button className="button small ghost" disabled={saving === 'load'} onClick={() => void Promise.all([loadData(), loadRecords()])}>{saving === 'load' ? '刷新中…' : '刷新'}</button></div>
        <p className="set-section-note">回测、参数优化、因子、模型、组合和执行记录统一折算为「记录单位」。倍率默认按当前工作区的典型落盘体积估算，管理员可在下方逐项调整。PIN 保护的记录不会被自动清理。</p>
        <div className="usage-kpis">
          <div className="usage-kpi anim-rise"><small>记录单位占用</small><b>{fmtUnits(u.total_units)}</b><em>上限 {fmtUnits(u.max_units_per_user)} 单位</em></div>
          <div className="usage-kpi anim-rise" style={{ animationDelay: '40ms' }}><small>剩余配额</small><b>{fmtUnits(u.remaining_units)}</b><em>{over ? '已超额，需要清理' : '按工作区上限计算'}</em></div>
          <div className="usage-kpi anim-rise" style={{ animationDelay: '80ms' }}><small>记录总数</small><b>{u.total_records.toLocaleString()}</b><em>跨 {u.types.filter((t) => t.count > 0).length} 种记录类型</em></div>
          <div className="usage-kpi anim-rise" style={{ animationDelay: '120ms' }}><small>PIN 保护</small><b>{u.pinned_records.toLocaleString()}</b><em>不参与自动清理</em></div>
        </div>
        <RecordComposition usage={u} />
        {over && <Callout tone="warn">当前已超过工作区单位上限。自动清理关闭时，新研究记录会在创建前被拒绝，直到删除足够的历史记录。</Callout>}
      </section>

      <section className="set-section">
        <div className="set-section-head">
          <h3>按记录类型统计</h3>
          {isAdmin && <button className="button small primary" disabled={saving === 'multipliers'} onClick={saveMultipliers}>{saving === 'multipliers' ? '保存中…' : '保存倍率'}</button>}
        </div>
        <p className="set-section-note">倍率是相对回测记录的存储成本。默认以约 350 KB / 回测为 1.0 单位；其他类型按报告、状态和索引文件合计体积估算。范围为 0.01–20。</p>
        <div className="table-wrap">
          <table>
            <thead><tr><th>类型</th><th className="right">数量</th><th className="right">PIN</th><th className="right">当前倍率</th><th className="right">默认倍率</th><th className="right">占用单位</th><th>典型体积</th></tr></thead>
            <tbody>
              {u.types.map((row: RecordTypeUsage) => (
                <tr key={row.key} className="anim-in">
                  <td><b>{row.label}</b><div className="hint mono" style={{ margin: '2px 0 0' }}>{row.key}</div></td>
                  <td className="right mono">{row.count.toLocaleString()}</td>
                  <td className="right mono">{row.pinned.toLocaleString()}</td>
                  <td className="right">
                    {isAdmin ? (
                      <input
                        type="number"
                        min={0.01}
                        max={20}
                        step={0.01}
                        aria-label={`${row.label}倍率`}
                        value={multipliers[row.key] ?? row.multiplier}
                        onChange={(e) => setMultipliers((cur) => ({ ...cur, [row.key]: Number(e.target.value) }))}
                        style={{ width: 88, textAlign: 'right' }}
                      />
                    ) : <span className="mono">{row.multiplier.toFixed(2)}</span>}
                  </td>
                  <td className="right mono muted">{row.default_multiplier.toFixed(2)}</td>
                  <td className="right mono">{fmtUnits(row.units)}</td>
                  <td className="muted">{fmtApprox(row.approx_bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="set-section">
        <div className="set-section-head"><h3>我的自动清理设置</h3><button className="button small primary" disabled={saving === 'user'} onClick={saveUser}>{saving === 'user' ? '保存中…' : '保存设置'}</button></div>
        <div className="retention-switches">
          <label className="retention-switch">
            <span><b>总量达到上限时自动删除最久远记录</b><small>超过工作区记录单位上限时，自动删除自己最旧的未 PIN 记录；关闭则必须先手动删除。</small></span>
            <button type="button" role="switch" aria-checked={autoDelete} className={`switch${autoDelete ? ' on' : ''}`} onClick={() => setAutoDelete(!autoDelete)}><i /></button>
          </label>
          <label className="retention-switch">
            <span><b>单策略回测自动清理最旧记录</b><small>同一策略达到下方上限后，自动删除该策略最旧的未 PIN 回测。</small></span>
            <button type="button" role="switch" aria-checked={autoStrategy} className={`switch${autoStrategy ? ' on' : ''}`} onClick={() => setAutoStrategy(!autoStrategy)}><i /></button>
          </label>
          <Field label="单策略保留回测数" hint="默认 5 次；仅影响回测记录">
            <input type="number" min={1} max={100} value={perStrategy} onChange={(e) => setPerStrategy(Number(e.target.value))} />
          </Field>
        </div>
        <div className="button-row" style={{ marginTop: 12 }}>
          <button className="button small" disabled={saving === 'cleanup'} onClick={cleanup}>{saving === 'cleanup' ? '清理中…' : '按当前设置立即清理'}</button>
        </div>
      </section>

      {isAdmin && (
        <section className="set-section">
          <div className="set-section-head"><h3>工作区记录单位上限</h3><button className="button small primary" disabled={saving === 'workspace'} onClick={saveWorkspace}>{saving === 'workspace' ? '保存中…' : '保存上限'}</button></div>
          <p className="set-section-note">每名成员独立计算。默认 100,000 单位；硬上限取 100,000 与服务器空间 20% 的较小值（当前约 {fmtUnits(hardMax)} 单位 / {fmtStorageBytes(reservedBytes)} 预留空间）。新成员默认 200 单位，可在「新成员默认限额」中调整。</p>
          <Field label="每名成员记录单位上限" hint={`1–${fmtUnits(hardMax)} 单位；默认 100,000，受服务器空间 20% 限制`}><input type="number" min={1} max={hardMax} value={adminMax} onChange={(e) => setAdminMax(Number(e.target.value))} /></Field>
        </section>
      )}

      {!!u.strategies?.length && (
        <section className="set-section">
          <div className="set-section-head"><h3>回测按策略使用情况</h3></div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>策略</th><th className="right">记录数</th><th className="right">PIN</th><th className="right">单策略上限</th><th>状态</th></tr></thead>
              <tbody>
                {u.strategies.map((row) => (
                  <tr key={row.key}>
                    <td>{row.name}</td><td className="right mono">{row.count}</td><td className="right mono">{row.pinned}</td><td className="right mono">{row.limit}</td>
                    <td>{row.over_limit ? <Tag tone="warn">超出上限</Tag> : <Tag tone="pass">正常</Tag>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="set-section">
        <div className="set-section-head settings-record-head">
          <h3>记录明细</h3>
          <div className="settings-filter-row">
            <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索标题、ID、类型或策略" aria-label="搜索研究记录" />
            <select value={recordType} onChange={(e) => setRecordType(e.target.value)} aria-label="按记录类型筛选">
              <option value="">全部类型</option>
              {u.types.map((row) => <option key={row.key} value={row.key}>{row.label}</option>)}
            </select>
          </div>
        </div>
        {recordsLoading && !records.length ? <Loading label="正在查找研究记录" /> : !records.length ? (
          <EmptyState title={debouncedQuery || recordType ? '没有匹配的研究记录' : '还没有研究记录'}>回测、因子、模型、组合和执行产物会在这里统一显示。</EmptyState>
        ) : (
          <>
            <div className="retention-batch-bar">
              <span>已选 {selectedRows.length} 条</span>
              <button type="button" className="button small ghost" disabled={!selectedRows.length} onClick={() => void batchPin(true)}>批量 PIN</button>
              <button type="button" className="button small ghost" disabled={!selectedRows.length} onClick={() => void batchPin(false)}>取消 PIN</button>
              <button type="button" className="button small danger" disabled={!selectedRows.length} onClick={() => void batchRemove()}>批量删除</button>
            </div>
            <div className="table-wrap" aria-busy={recordsLoading}>
              <table className="settings-table records-table">
                <thead><tr><th className="retention-check"><input type="checkbox" checked={allSelected} onChange={toggleAll} aria-label="全选当前记录" /></th><th>记录</th><th>类型</th><th>创建时间</th><th className="right">单位</th><th>保护</th><th /></tr></thead>
                <tbody>
                  {records.map((row) => (
                    <tr key={`${row.record_type}:${row.id}`} className="anim-in">
                      <td className="retention-check"><input type="checkbox" checked={selected.has(recordKey(row))} onChange={() => toggleSelect(row)} aria-label={`选择 ${row.record_title || row.id}`} /></td>
                      <td><b>{row.record_title || row.id}</b><div className="hint mono" style={{ margin: '2px 0 0' }}>{row.id}</div></td>
                      <td><Tag>{row.record_type_label}</Tag></td>
                      <td className="muted">{fmtTime(((row.created_at ?? row.updated_at ?? 0) * 1000))}</td>
                      <td className="right mono">{row.record_units.toFixed(2)}</td>
                      <td>{row.pinned ? <Tag tone="warn">已 PIN</Tag> : <span className="muted">普通</span>}</td>
                      <td className="right">
                        <span className="button-row tight">
                          <button className="button small ghost" onClick={() => void togglePin(row)}>{row.pinned ? '取消 PIN' : 'PIN'}</button>
                          <button className="button small danger" disabled={!isAdmin && !!row.pinned} onClick={() => void remove(row)}>删除</button>
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="settings-pagination">
              <span className="hint">已显示 {records.length} 条{recordsHasMore ? '，还有更多记录' : ''}</span>
              {recordsHasMore && <button className="button small" disabled={recordsLoading} onClick={() => void loadRecords(recordsNext, true)}>{recordsLoading ? '加载中…' : '加载更多'}</button>}
            </div>
          </>
        )}
      </section>
    </div>
  )
}

function RunsTab({ notify, onNavigate, readonly }: { notify: PageProps['notify']; onNavigate: (k: string) => void; readonly: boolean }) {
  const [runs, setRuns] = useState<Run[] | null>(null)
  const [mode, setMode] = useState('')
  const [query, setQuery] = useState('')
  const debouncedQuery = useDebouncedValue(query)
  const [loading, setLoading] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [nextCursor, setNextCursor] = useState<{ created_at: number; id: string } | null>(null)
  const requestRef = useRef(0)
  const [open, setOpen] = useState<Run | null>(null)
  const [events, setEvents] = useState<RunEvent[]>([])

  const loadPage = useCallback(async (cursor?: { created_at: number; id: string } | null, append = false) => {
    const request = ++requestRef.current
    setLoading(true)
    try {
      const page = await v4.runs({
        mode: mode || undefined,
        q: debouncedQuery || undefined,
        before_created_at: cursor?.created_at,
        before_id: cursor?.id,
        limit: 50,
      })
      if (request !== requestRef.current) return
      setRuns((prev) => append ? [...(prev ?? []), ...page.items] : page.items)
      setHasMore(page.has_more)
      setNextCursor(page.next_cursor)
    } catch (e) {
      if (request === requestRef.current) notify((e as Error).message, true)
    } finally {
      if (request === requestRef.current) setLoading(false)
    }
  }, [debouncedQuery, mode, notify])

  useEffect(() => {
    setRuns(null)
    void loadPage()
  }, [loadPage])
  useEffect(() => { if (open) v4.runEvents(open.id).then((r) => setEvents(r.items)).catch(() => setEvents([])) }, [open])

  const cmd = async (r: Run, c: 'pause' | 'resume' | 'cancel' | 'liquidate_and_stop') => {
    try { await v4.runCommand(r.id, c); notify('命令已提交'); await loadPage() } catch (e) { notify((e as Error).message, true) }
  }
  if (!runs) return <Loading />
  return (
    <>
      <div className="button-row settings-filter-row" style={{ marginBottom: 12 }}>
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索运行标题、ID、类型或错误" aria-label="搜索运行记录" />
        <select value={mode} onChange={(e) => setMode(e.target.value)} aria-label="按模式筛选运行">
          <option value="">全部模式</option>
          {Object.entries(MODE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <button className="button small" disabled={loading} onClick={() => void loadPage()}>{loading ? '刷新中…' : '刷新'}</button>
        <span className="hint" style={{ margin: 0 }}>长任务由独立 worker 执行；每位成员同时只持有一个租约，崩溃后从检查点恢复。</span>
      </div>
      {runs.length === 0 ? <EmptyState title={debouncedQuery || mode ? '没有匹配的运行记录' : '还没有运行记录'} /> : (
        <>
          <div className="table-wrap" aria-busy={loading}>
            <table className="settings-table runs-table">
              <thead><tr><th>运行</th><th>类型</th><th>模式</th><th>状态</th><th>进度</th><th>认领</th><th>创建</th><th /></tr></thead>
              <tbody>
                {runs.map((r) => {
                  const [label, cls] = RUN_STATE[r.state]
                  return (
                    <tr key={r.id}>
                      <td><button className="button ghost small" onClick={() => setOpen(r)}>{r.title || r.id}</button></td>
                      <td>{KIND_LABEL[r.kind] ?? r.kind}</td>
                      <td><Tag tone={r.mode === 'testnet' ? 'warn' : undefined}>{MODE_LABEL[r.mode] ?? r.mode}</Tag></td>
                      <td><span className={`table-pill ${cls}`}>{label}</span>{r.state === 'QUEUED' && r.queue_position ? <small> · 第 {r.queue_position} 位</small> : null}{r.error && <small title={r.error}> {r.error_code}</small>}</td>
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
          <div className="settings-pagination">
            <span className="hint">已显示 {runs.length} 条{hasMore ? '，还有更多运行' : ''}</span>
            {hasMore && <button className="button small" disabled={loading} onClick={() => void loadPage(nextCursor, true)}>{loading ? '加载中…' : '加载更多'}</button>}
          </div>
        </>
      )}
      {open && (
        <Modal title={open.title || open.id} onClose={() => setOpen(null)} width={820}>
          <dl className="kv">
            <dt>状态</dt><dd>{RUN_STATE[open.state]?.[0] ?? open.state}</dd>
            <dt>类型</dt><dd>{KIND_LABEL[open.kind] ?? open.kind}</dd>
            <dt>模式</dt><dd>{MODE_LABEL[open.mode] ?? open.mode}</dd>
            <dt>创建</dt><dd>{fmtTime(open.created_at)}</dd>
            <dt>认领次数</dt><dd>{open.attempts}</dd>
            {open.error && <><dt>错误</dt><dd className="negative">{open.error}</dd></>}
          </dl>
          <h3 className="set-sub">事件</h3>
          <div className="table-wrap" style={{ maxHeight: 320, overflow: 'auto' }}>
            <table>
              <thead><tr><th>时间</th><th>类型</th><th>数据</th></tr></thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id}><td>{fmtTime(e.ts)}</td><td>{e.type}</td><td className="mono wrap" style={{ fontSize: 11.5 }}>{JSON.stringify(e.data).slice(0, 220)}</td></tr>
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
  const [query, setQuery] = useState('')
  const debouncedQuery = useDebouncedValue(query)
  const [loading, setLoading] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [nextBeforeId, setNextBeforeId] = useState<number | null>(null)
  const requestRef = useRef(0)

  const loadPage = useCallback(async (beforeId?: number | null, append = false) => {
    const request = ++requestRef.current
    setLoading(true)
    try {
      const page = await v4.audit({
        action: action || undefined,
        q: debouncedQuery || undefined,
        before_id: beforeId ?? undefined,
        limit: 100,
      })
      if (request !== requestRef.current) return
      setItems((prev) => append ? [...(prev ?? []), ...page.items] : page.items)
      setHasMore(page.has_more)
      setNextBeforeId(page.next_before_id)
    } catch (e) {
      if (request === requestRef.current) notify((e as Error).message, true)
    } finally {
      if (request === requestRef.current) setLoading(false)
    }
  }, [action, debouncedQuery, notify])

  useEffect(() => {
    setItems(null)
    void loadPage()
  }, [loadPage])

  return (
    <>
      <div className="button-row settings-filter-row" style={{ marginBottom: 12 }}>
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索操作、对象、操作者、trace_id 或详情" aria-label="搜索审计日志" />
        <select value={action} onChange={(e) => setAction(e.target.value)} aria-label="按操作类型筛选">
          <option value="">全部操作</option>
          {['user.', 'member.', 'workspace.', 'run.', 'strategy.', 'account.', 'live.', 'config.'].map((a) => <option key={a} value={a}>{a}*</option>)}
        </select>
        <button className="button small" disabled={loading} onClick={() => void loadPage()}>{loading ? '查找中…' : '刷新'}</button>
        <span className="hint" style={{ margin: 0 }}>输入停止 350ms 后才查询；记录按游标分页，每次最多读取 100 条。</span>
      </div>
      {!items ? <Loading label="正在查找审计记录" /> : items.length === 0 ? <EmptyState title={debouncedQuery || action ? '没有匹配的审计记录' : '暂无审计记录'} /> : (
        <>
          <div className="table-wrap" aria-busy={loading}>
            <table className="settings-table audit-table">
              <thead><tr><th>时间 UTC</th><th>操作者</th><th>操作</th><th>对象</th><th>IP</th><th>trace_id</th><th>详情</th></tr></thead>
              <tbody>
                {items.map((i) => (
                  <tr key={i.id}>
                    <td>{fmtTime(i.ts)}</td><td>{i.user_name ?? '—'}</td><td className="mono">{i.action}</td>
                    <td className="mono">{i.object_type}{i.object_id ? `:${i.object_id}` : ''}</td><td className="mono">{i.ip}</td>
                    <td className="mono">{i.trace_id}</td>
                    <td className="mono wrap" style={{ fontSize: 11.5 }}>{Object.keys(i.details).length ? JSON.stringify(tData(i.details)) : ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="settings-pagination">
            <span className="hint">已显示 {items.length} 条{hasMore ? '，还有更多记录' : ''}</span>
            {hasMore && <button className="button small" disabled={loading} onClick={() => void loadPage(nextBeforeId, true)}>{loading ? '加载中…' : '加载更多'}</button>}
          </div>
        </>
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
              <div className="table-wrap">
                <table className="settings-table network-table">
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
              </div>
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

function AccountTab({ me, notify, onSaved }: { me: Me; notify: PageProps['notify']; onSaved: () => void }) {
  const [oldPw, setOld] = useState('')
  const [newPw, setNew] = useState('')
  return (
    <div style={{ maxWidth: 520 }}>
      <ProfileSection me={me} notify={notify} onSaved={onSaved} />
      <EmailSection notify={notify} onSaved={onSaved} />
      <h3 className="set-sub">密码</h3>
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

/** 用户名（可用来登录）与显示名。 */
function ProfileSection({ me, notify, onSaved }: { me: Me; notify: PageProps['notify']; onSaved: () => void }) {
  const [username, setUsername] = useState(me.user.username || '')
  const [name, setName] = useState(me.user.name)
  const [busy, setBusy] = useState(false)
  const dirty = username !== (me.user.username || '') || name !== me.user.name
  return (
    <>
      <h3 className="set-sub">账号</h3>
      <div className="field-row">
        <Field label="用户名" hint="可以用用户名或已绑定的邮箱登录"><input value={username} maxLength={32} onChange={(e) => setUsername(e.target.value.trim())} /></Field>
        <Field label="显示名称"><input value={name} maxLength={40} onChange={(e) => setName(e.target.value)} /></Field>
      </div>
      <dl className="kv" style={{ margin: '10px 0' }}>
        <dt>部署管理员</dt><dd>{me.user.is_admin ? '是（可配置模型服务）' : '否'}</dd>
      </dl>
      <div className="button-row">
        <button className="button primary" disabled={!dirty || busy || username.length < 2} onClick={async () => {
          setBusy(true)
          try { await v4.updateAccount({ username, name }); notify('已保存'); onSaved() } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
        }}>保存</button>
      </div>
    </>
  )
}

/** 绑定 / 更换邮箱：先发验证码到邮箱，填对后才算绑定成功。 */
function EmailSection({ notify, onSaved }: { notify: PageProps['notify']; onSaved: () => void }) {
  const [acc, setAcc] = useState<Account | null>(null)
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [sentTo, setSentTo] = useState('')
  const [wait, setWait] = useState(0)
  const [busy, setBusy] = useState(false)
  useEffect(() => { v4.account().then((r) => setAcc(r.account)).catch(() => undefined) }, [])
  useEffect(() => {
    if (wait <= 0) return
    const t = window.setTimeout(() => setWait(wait - 1), 1000)
    return () => window.clearTimeout(t)
  }, [wait])
  const send = async () => {
    setBusy(true)
    try {
      const r = await v4.sendEmailCode(email)
      setSentTo(r.email); setWait(r.cooldown); setCode('')
      notify(r.via === 'outbox' ? '验证码已写入服务器 outbox（开发模式，未真正发信）' : `验证码已发送到 ${r.email}`)
    } catch (e) {
      const err = e as Error & { details?: { retry_after?: number } }
      if (err.details?.retry_after) setWait(err.details.retry_after)
      notify(err.message, true)
    } finally { setBusy(false) }
  }
  const verify = async () => {
    setBusy(true)
    try {
      const r = await v4.verifyEmail(sentTo, code)
      setAcc(r.account); setSentTo(''); setEmail(''); setCode('')
      notify('邮箱绑定成功，之后可以用它登录'); onSaved()
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  return (
    <>
      <h3 className="set-sub">邮箱</h3>
      <p className="hint" style={{ marginTop: 0 }}>
        {acc?.email
          ? <>当前绑定：<b>{acc.email}</b> {acc.email_verified ? <Tag tone="ok">已验证</Tag> : <Tag tone="warn">未验证</Tag>}</>
          : '还没有绑定邮箱。绑定后可以用邮箱登录、接收通知。'}
      </p>
      <div className="field-row">
        <Field label={acc?.email ? '更换为' : '邮箱地址'}>
          <input type="email" value={email} placeholder="name@example.com" onChange={(e) => { setEmail(e.target.value); if (sentTo && e.target.value !== sentTo) setSentTo('') }} />
        </Field>
        <div style={{ alignSelf: 'flex-end' }}>
          <button className="button" disabled={busy || wait > 0 || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)} onClick={send}>
            {wait > 0 ? `${wait} 秒后可重发` : sentTo ? '重新发送' : '发送验证码'}
          </button>
        </div>
      </div>
      {sentTo && (
        <div className="field-row anim-in" style={{ marginTop: 10 }}>
          <Field label={`发到 ${sentTo} 的 6 位验证码`} hint="10 分钟内有效">
            <input inputMode="numeric" autoComplete="one-time-code" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} />
          </Field>
          <div style={{ alignSelf: 'flex-end' }}>
            <button className="button primary" disabled={busy || code.length !== 6} onClick={verify}>验证并绑定</button>
          </div>
        </div>
      )}
    </>
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
          <div className="totp-qr-setup">
            <QRCode value={setup.otpauth_uri} size={188} label="扫描绑定 QuantLab 双因素认证" />
            <div className="totp-qr-copy">
              <b>用验证器扫码绑定</b>
              <p className="hint">支持 Google Authenticator、1Password、Microsoft Authenticator 等。密钥只显示这一次，确认前不会启用。</p>
              <details className="totp-manual">
                <summary>无法扫码？手动录入</summary>
                <Field label="密钥"><input className="mono" readOnly value={setup.secret} /></Field>
                <div className="mono totp-uri">{setup.otpauth_uri}</div>
              </details>
            </div>
          </div>
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
