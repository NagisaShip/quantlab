import { useCallback, useEffect, useState } from 'react'
import { api, money, pct, shortTime, type DigestData, type NotificationRecord } from '../api/core'
import {
  Callout,
  EmptyState,
  Field,
  Heading,
  Loading,
  MetricCard,
  Panel,
  SourceBanner,
  Tag,
} from '../components/ui'
import { BriefView, NightShiftView } from './NightShift'
import { Segments } from '../components/ui'
import type { PageProps } from './types'

type Tab = 'night' | 'brief' | 'push'
const TABS: { value: Tab; label: string }[] = [{ value: 'night', label: '任务计划' }, { value: 'brief', label: 'AI 晨报' }, { value: 'push', label: '通知与调度' }]

export default function LongRunningPage({ arg, onNavigate, notify, setBusy }: PageProps & { arg?: string }) {
  const tab: Tab = arg === 'brief' || arg === 'push' ? arg : 'night'
  return (
    <>
      <Heading title="定时任务" description="每天按设定时间自动执行研究队列，有试验次数、token 和时长上限；执行结束生成 AI 晨报。产出的都是候选，是否启用由你决定。" />
      <div className="fx-tabs">
        <Segments value={tab} options={TABS} onChange={(t) => onNavigate(t === 'night' ? 'long-running' : `long-running/${t}`)} ariaLabel="定时任务视图" />
      </div>
      {tab === 'night' && <NightShiftView notify={notify} onNavigate={onNavigate} />}
      {tab === 'brief' && <BriefView notify={notify} onNavigate={onNavigate} />}
      {tab === 'push' && <PushAndSchedule notify={notify} setBusy={setBusy} />}
    </>
  )
}

const JOB_KIND: Record<string, string> = { digest: '每日简报', maintainer: '策略维护', brief: 'AI 晨报', nightshift: '定时任务' }

export const CHANNELS = [
  { id: 'console', label: '控制台（调试）', hint: '只打到服务端日志，不发出任何消息' },
  { id: 'wecom', label: '企业微信群机器人', hint: '推荐：稳定、格式好、额度宽松。需在配置里填 Webhook' },
  { id: 'serverchan', label: 'Server 酱', hint: '个人号兜底通道，需填 SendKey' },
  { id: 'pushplus', label: 'PushPlus', hint: '微信推送，需填 token' },
]

function PushAndSchedule({ notify, setBusy }: Pick<PageProps, 'notify' | 'setBusy'>) {
  const [preview, setPreview] = useState<{ digest_data: DigestData; evidence: Record<string, unknown> } | null>(null)
  const [records, setRecords] = useState<NotificationRecord[]>([])
  const [pending, setPending] = useState<Record<string, string>[]>([])
  const [jobs, setJobs] = useState<Awaited<ReturnType<typeof api.schedules>>['jobs']>([])
  const [channel, setChannel] = useState('console')
  const [error, setError] = useState('')
  const [jobId, setJobId] = useState('morning_digest')
  const [cron, setCron] = useState('0 9 * * *')
  const [kind, setKind] = useState('digest')

  const refresh = useCallback(async () => {
    const [p, n, s] = await Promise.all([api.digestPreview(), api.notifications(), api.schedules()])
    setPreview(p)
    setRecords(n.notifications)
    setPending(n.pending)
    setJobs(s.jobs)
  }, [])

  useEffect(() => {
    refresh().catch((e: Error) => setError(e.message))
  }, [refresh])

  const act = async (label: string, title: string, note: string, fn: () => Promise<unknown>) => {
    setBusy({ title, note })
    setError('')
    try {
      await fn()
      await refresh()
      notify(label)
    } catch (e) {
      setError((e as Error).message)
      notify((e as Error).message, true)
    } finally {
      setBusy(null)
    }
  }

  const d = preview?.digest_data
  const evidence = preview?.evidence as { strategies?: Record<string, Record<string, number>> } | undefined

  return (
    <>
      {error && <Callout tone="fail" title="操作失败">{error}</Callout>}

      <SourceBanner
        icon="◷"
        title={d ? `简报数据日期 ${d.date}` : '正在读取简报数据'}
        detail="数据来自平台已存实验的聚合；接入真实净值与订单状态后会反映真实模拟盘运行情况"
        right="虚拟资金"
      />

      {!d ? (
        <Loading />
      ) : (
        <>
          <div className="metrics-grid">
            <MetricCard label="净值" value={money(d.nav, 0)} unit="USDT" caption={d.date} bars />
            <MetricCard
              label={d.period}
              value={pct(d.period_change)}
              tone={d.period_change >= 0 ? 'positive' : 'negative'}
              caption="基于已存实验聚合"
            />
            <MetricCard label="最大回撤" value={pct(d.max_drawdown)} tone="negative" caption="区间内峰值回落" />
            <MetricCard
              label="交易 / 胜率"
              value={`${d.trades} / ${pct(d.win_rate, 0)}`}
              caption="样本不足时胜率不适用"
            />
          </div>

          {d.alerts.length > 0 ? (
            <Callout tone="warn" title="检测到的异常">
              <ul>{d.alerts.map((a, i) => <li key={i}>{a}</li>)}</ul>
            </Callout>
          ) : (
            <Callout title="未检测到异常">订单状态、对账差异与任务失败均无告警。</Callout>
          )}
        </>
      )}

      <div className="two-columns even">
        <Panel title="通知型 Agent" subtitle="生成中文简报并推送到微信">
          <Field label="推送通道">
            <select value={channel} onChange={(e) => setChannel(e.target.value)}>
              {CHANNELS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </Field>
          <div className="hint" style={{ marginBottom: 14 }}>{CHANNELS.find((c) => c.id === channel)?.hint}</div>
          <div className="button-row">
            <button
              className="button"
              onClick={() => act('简报已生成（未推送）', '生成简报', '调用模型汇总净值、回撤与异常。', () => api.runDigest(channel, true))}
            >
              生成简报（不推送）
            </button>
            <button
              className="button primary"
              onClick={() => act('简报已推送', '生成并推送简报', '推送失败会进入待发队列，配置好通道后自动重试。', () => api.runDigest(channel, false))}
            >
              ▷ 生成并推送
            </button>
            <button
              className="button"
              onClick={() => act('测试消息已发送', '测试通知通道', '验证 Webhook / SendKey 是否可用。', () => api.testNotification(channel))}
            >
              测试通道
            </button>
          </div>
          {pending.length > 0 && (
            <Callout tone="warn" title={`有 ${pending.length} 条推送未成功`}>
              推送失败的消息会进入待发队列，配置好通道后会自动重试。
            </Callout>
          )}
        </Panel>

        <Panel title="策略维护型 Agent" subtitle="产出候选版本与差异报告，绝不自动上线">
          <div className="card-body" style={{ marginBottom: 14 }}>
            汇总已存实验的收益分布、回撤与参数，评估是否需要生成候选新版本。
            <strong> 它永远不会直接启用实盘。</strong>
          </div>
          <div className="button-row">
            <button
              className="button"
              onClick={() => act('评估已生成（未推送）', '策略维护评估', '汇总实验收益分布与参数稳定性。', () => api.runMaintainer(channel, true))}
            >
              生成评估（不推送）
            </button>
            <button
              className="button primary"
              onClick={() => act('评估已推送', '生成并推送评估', '只推给人审核，不会自动变更任何策略。', () => api.runMaintainer(channel, false))}
            >
              ▷ 生成并推送
            </button>
          </div>
          {evidence?.strategies && Object.keys(evidence.strategies).length > 0 && (
            <div className="table-wrap" style={{ marginTop: 16 }}>
              <table>
                <thead>
                  <tr><th>策略</th><th className="right">实验数</th><th className="right">平均收益</th><th className="right">平均回撤</th></tr>
                </thead>
                <tbody>
                  {Object.entries(evidence.strategies).map(([name, s]) => (
                    <tr key={name}>
                      <td><strong>{name}</strong></td>
                      <td className="right">{s.runs}</td>
                      <td className={`right ${(s.avg_return ?? 0) >= 0 ? 'positive' : 'negative'}`}>
                        {pct(s.avg_return)}
                      </td>
                      <td className="right">{pct(s.avg_max_drawdown)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="定时调度" subtitle="服务进程内每 60 秒检查一次，到点自动触发并推送">
        <div className="config-bar" style={{ border: 0, padding: 0, background: 'transparent' }}>
          <Field label="任务 ID">
            <input type="text" value={jobId} onChange={(e) => setJobId(e.target.value)} />
          </Field>
          <Field label="cron（分 时 日 月 周）" hint="本地时区。例：0 9 * * * 表示每天 09:00">
            <input type="text" value={cron} onChange={(e) => setCron(e.target.value)} />
          </Field>
          <Field label="类型">
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="digest">每日简报</option>
              <option value="maintainer">策略维护评估</option>
              <option value="brief">AI 晨报</option>
              <option value="nightshift">定时任务（按队列研究）</option>
            </select>
          </Field>
          <Field label="通道">
            <select value={channel} onChange={(e) => setChannel(e.target.value)}>
              {CHANNELS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </Field>
          <div style={{ display: 'flex', alignItems: 'flex-end' }}>
            <button
              className="button primary"
              onClick={() => act('调度已添加', '添加定时任务', '到点会自动触发并推送。', () => api.createSchedule({ id: jobId, cron, kind, channel }))}
            >
              ＋ 添加调度
            </button>
          </div>
        </div>

        {jobs.length === 0 ? (
          <EmptyState glyph="◷" title="还没有定时任务">添加一个之后，服务进程会按时自动触发并推送。</EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th>任务 ID</th><th>cron</th><th>类型</th><th>通道</th><th>状态</th><th /></tr></thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td><code className="inline">{j.id}</code></td>
                    <td>{j.cron}</td>
                    <td>{JOB_KIND[j.kind] ?? j.kind}</td>
                    <td>{j.channel}</td>
                    <td>{j.enabled ? <span className="table-pill">启用</span> : <span className="table-pill pending">暂停</span>}</td>
                    <td className="right">
                      <button
                        className="button small danger"
                        onClick={() => act('调度已删除', '删除定时任务', '该任务不再自动运行。', () => api.deleteSchedule(j.id))}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title={`推送记录（${records.length}）`} subtitle="每次生成都留档，含状态与推送目标">
        {records.length === 0 ? (
          <EmptyState glyph="◷" title="还没有推送记录">生成一次简报后会出现在这里。</EmptyState>
        ) : (
          records.slice(0, 10).map((r, i) => (
            <article className="role-card" key={i} style={{ marginBottom: 14 }}>
              <div className="card-tags">
                <Tag>{r.agent_id === 'daily_digest' ? '每日简报' : '策略维护'}</Tag>
                <Tag mono>{r.run_key}</Tag>
                {r.status === 'ok'
                  ? <Tag tone="ok">已推送</Tag>
                  : r.status === 'skipped' ? <Tag>已跳过</Tag> : <Tag tone="warn">待重试</Tag>}
                <span className="muted" style={{ fontSize: 10 }}>{shortTime(r.ts)}</span>
              </div>
              <pre style={{
                margin: '10px 0 0', padding: '12px 14px', background: 'var(--sunken)',
                border: '1px solid var(--line-2)', borderRadius: 7,
                whiteSpace: 'pre-wrap', fontFamily: 'var(--sans)', fontSize: 12, lineHeight: 1.8, color: 'var(--ink-2)',
              }}>
                {r.output}
              </pre>
            </article>
          ))
        )}
      </Panel>

      <Callout>
        推送文案只陈述数据、不预测未来，并统一附带「仅为研究结果，非投资建议」。
        个人微信通道仅使用官方或授权方式，不做爬虫登录。
      </Callout>
    </>
  )
}
