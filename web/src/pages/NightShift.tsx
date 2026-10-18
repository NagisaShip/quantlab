/** 定时任务（每天按时间自动执行研究队列，带预算）与 AI 晨报。 */
import { useCallback, useEffect, useState } from 'react'
import { rd, when, type Brief, type Domain, type NightConfig, type NightTask, type Shift } from '../apiRd'
import { Callout, EmptyState, Field, Loading, Panel, Tag } from '../components/ui'
import { CHANNELS } from './LongRunning'
import type { PageProps } from './types'

const TASK_STATUS: Record<NightTask['status'], [string, string]> = {
  queued: ['排队', ''], running: ['进行中', 'agent'], done: ['完成', 'ok'], stopped: ['预算用完停下', 'warn'], failed: ['出错', 'fail'],
}
const SHIFT_STATUS: Record<Shift['status'], [string, string]> = { running: ['进行中', 'agent'], done: ['已结束', 'ok'], interrupted: ['中断', 'fail'] }
const KIND_FALLBACK: Record<string, string> = { strategy: '策略', factor: '因子', model: '模型', portfolio: '组合', execution: '执行' }
const pad = (n: number) => String(n).padStart(2, '0')
const nextRun = (cfg: NightConfig) => {
  const now = new Date()
  const target = new Date(now)
  target.setHours(cfg.hour, cfg.minute, 0, 0)
  if (target.getTime() <= now.getTime()) target.setDate(target.getDate() + 1)
  return `${target.toDateString() === now.toDateString() ? '今天' : '明天'} ${pad(cfg.hour)}:${pad(cfg.minute)}`
}

type Night = Awaited<ReturnType<typeof rd.night>>

export function NightShiftView({ notify, onNavigate }: Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [d, setD] = useState<Night | null>(null)
  const [cfg, setCfg] = useState<NightConfig | null>(null)
  const [kind, setKind] = useState<Domain>('factor')
  const [idea, setIdea] = useState('')
  const [executor, setExecutor] = useState<'solo' | 'team'>('solo')
  const [open, setOpen] = useState<string | null>(null)
  const load = useCallback(() => rd.night().then((r) => { setD(r); setCfg((c) => c ?? r.config) }).catch((e: Error) => notify(e.message, true)), [notify])
  useEffect(() => { load() }, [load])
  useEffect(() => {
    if (!d?.running) return
    const t = window.setInterval(load, 5000)
    return () => window.clearInterval(t)
  }, [d?.running, load])

  if (!d || !cfg) return <Loading label="正在读取定时任务" />
  const kinds = d.kinds ?? KIND_FALLBACK
  const queued = d.tasks.filter((t) => t.status === 'queued')
  const run = d.running
  const saveCfg = async (patch: Partial<NightConfig>) => {
    try { const r = await rd.nightConfig(patch); setCfg(r.config); notify('定时任务计划已保存') } catch (e) { notify((e as Error).message, true) }
  }
  const add = async () => {
    if (idea.trim().length < 2) return
    try { await rd.addTask({ kind, idea: idea.trim(), executor }); setIdea(''); await load(); notify('已加入定时任务队列') } catch (e) { notify((e as Error).message, true) }
  }
  const startNow = async () => {
    try { await rd.runNight(); notify('定时任务已开始执行'); window.setTimeout(load, 800) } catch (e) { notify((e as Error).message, true) }
  }
  const stop = async () => {
    try { await rd.stopNight(); notify('已请求停止，当前项目保留到最近一版'); window.setTimeout(load, 800) } catch (e) { notify((e as Error).message, true) }
  }

  return (
    <>
      {run ? (
        <Callout title={`定时任务执行中 · 开始于 ${when(run.started_at)}`}>
          <ShiftBudget s={run} /> <button className="button small danger" onClick={stop} style={{ marginLeft: 8 }}>停止</button>
        </Callout>
      ) : cfg.enabled ? (
        <Callout title={`每天 ${pad(cfg.hour)}:${pad(cfg.minute)} 自动执行 · 下次 ${nextRun(cfg)}`}>当前有 {queued.length} 个待执行任务。到点由后台 worker 自动执行；也可以立即执行一次。</Callout>
      ) : (
        <Callout tone="warn" title="定时任务未开启">开启后会每天按设定时间自动执行队列，预算用完就停。只产出候选成果，是否启用由你决定。</Callout>
      )}

      <div className="two-columns even">
        <Panel title="每日任务队列" subtitle="每个任务会在 AI 研究室里新建一个项目并自动迭代，按顺序执行。"
          actions={<button className="button small primary" disabled={!!run || !queued.length} onClick={startNow}>▷ 立即执行一次</button>}>
          <div className="rd-addtask">
            <select value={kind} onChange={(e) => setKind(e.target.value as Domain)} aria-label="研究类型">
              {Object.entries(kinds).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <select value={executor} onChange={(e) => setExecutor(e.target.value as 'solo' | 'team')} aria-label="执行者">
              <option value="solo">单个 AI</option><option value="team">研究小组</option>
            </select>
            <input value={idea} onChange={(e) => setIdea(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') add() }}
              placeholder="一句研究想法，例如：资金费率极端时的反转因子" />
            <button className="button" disabled={idea.trim().length < 2} onClick={add}>加入</button>
          </div>
          {d.tasks.length === 0 ? (
            <EmptyState glyph="◷" title="还没有定时任务">在这里添加任务，或在 AI 研究室用 /schedule 把想法排进每日计划。</EmptyState>
          ) : (
            <ul className="plain-list rd-tasks">
              {d.tasks.slice(0, 30).map((t) => (
                <li key={t.id}>
                  <div className="row-between">
                    <span className="ellipsis"><Tag>{kinds[t.kind] ?? t.kind}</Tag> {t.idea}</span>
                    <span className="rd-task-act">
                      <Tag tone={TASK_STATUS[t.status][1]}>{TASK_STATUS[t.status][0]}</Tag>
                      {t.status === 'queued' && <button className="link danger" onClick={async () => { await rd.removeTask(t.id); load() }}>移除</button>}
                    </span>
                  </div>
                  <small>
                    {t.executor === 'team' ? '研究小组' : '单个 AI'} · 加入于 {when(t.created_at)}
                    {t.result?.headline ? ` · ${t.result.headline}` : ''}
                    {t.project_id && <> · <a href={`#/lab/${t.project_id}`}>研究记录</a></>}
                  </small>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="执行计划与预算" subtitle="设置每天自动执行的时间；任一预算用完，当前项目停在最近一版，剩余任务留到下次执行。">
          <label className="rd-check"><input type="checkbox" checked={cfg.enabled} onChange={(e) => saveCfg({ enabled: e.target.checked })} /> 启用每日自动执行</label>
          <div className="rd-grid">
            <Field label="每日执行时间">
              <span className="rd-time">
                <select value={cfg.hour} onChange={(e) => setCfg({ ...cfg, hour: Number(e.target.value) })}>{Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{pad(h)}</option>)}</select>
                :
                <select value={cfg.minute} onChange={(e) => setCfg({ ...cfg, minute: Number(e.target.value) })}>{[0, 15, 30, 45].map((m) => <option key={m} value={m}>{pad(m)}</option>)}</select>
              </span>
            </Field>
            <Field label="试验次数上限"><input type="number" min={1} max={500} value={cfg.max_trials} onChange={(e) => setCfg({ ...cfg, max_trials: Number(e.target.value) })} /></Field>
            <Field label="token 上限（千）"><input type="number" min={1} max={50000} value={Math.round(cfg.max_tokens / 1000)} onChange={(e) => setCfg({ ...cfg, max_tokens: Number(e.target.value) * 1000 })} /></Field>
            <Field label="时长上限（分钟）"><input type="number" min={5} max={1440} value={cfg.max_minutes} onChange={(e) => setCfg({ ...cfg, max_minutes: Number(e.target.value) })} /></Field>
            <Field label="每个任务最多几轮"><input type="number" min={1} max={8} value={cfg.rounds} onChange={(e) => setCfg({ ...cfg, rounds: Number(e.target.value) })} /></Field>
            <Field label="晨报推送">
              <select value={cfg.brief ? cfg.channel : ''} onChange={(e) => setCfg({ ...cfg, brief: !!e.target.value, channel: e.target.value || cfg.channel })}>
                <option value="">不生成晨报</option>
                {CHANNELS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
              </select>
            </Field>
          </div>
          <div className="button-row"><button className="button primary" onClick={() => saveCfg(cfg)}>保存计划</button></div>
        </Panel>
      </div>

      <Panel flush title="执行记录">
        {d.shifts.length === 0 ? <EmptyState glyph="◷" title="还没有执行记录">执行一次后，每个任务的结果和预算用量都会记在这里。</EmptyState> : (
          <ul className="plain-list rd-shifts">
            {d.shifts.map((s) => (
              <li key={s.id}>
                <button className="rd-shift-head" onClick={() => setOpen(open === s.id ? null : s.id)} aria-expanded={open === s.id}>
                  <span><b>{when(s.started_at)}</b> · {s.trigger === 'manual' ? '手动' : '定时'} · {s.items.length} 个任务 · 通过 {s.items.filter((i) => i.passed).length} 个</span>
                  <span><Tag tone={SHIFT_STATUS[s.status][1]}>{SHIFT_STATUS[s.status][0]}</Tag></span>
                </button>
                <small><ShiftBudget s={s} />{s.stop_reason ? ` · ${s.stop_reason}` : ''}</small>
                {open === s.id && (
                  <div className="table-wrap" style={{ marginTop: 8 }}>
                    <table className="fx-table">
                      <thead><tr><th>任务</th><th>结果</th><th className="right">版本</th><th /></tr></thead>
                      <tbody>{s.items.map((it) => (
                        <tr key={it.task_id}>
                          <td><Tag>{kinds[it.kind] ?? it.kind}</Tag> <span className="ellipsis rd-cell">{it.idea}</span></td>
                          <td>{it.error ? <span className="negative">{it.error}</span> : it.headline ?? it.status}{it.passed ? <> <Tag tone="ok">达标</Tag></> : null}{it.awaiting ? <> <Tag tone="warn">等你决定</Tag></> : null}</td>
                          <td className="right num">{it.versions ?? '—'}</td>
                          <td className="right">{it.project_id && <button className="button small ghost" onClick={() => onNavigate(`lab/${it.project_id}`)}>查看</button>}</td>
                        </tr>
                      ))}</tbody>
                    </table>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </>
  )
}

function ShiftBudget({ s }: { s: Shift }) {
  const u = s.used || {}
  return <span>试验 {u.trials ?? 0}/{s.budget.max_trials} · token {Math.round((u.tokens ?? 0) / 1000)}k/{Math.round(s.budget.max_tokens / 1000)}k · {Math.round(u.minutes ?? 0)}/{s.budget.max_minutes} 分钟</span>
}

/* ------------------------------------------------------------------ */
type BriefList = Awaited<ReturnType<typeof rd.brief>>

export function BriefView({ notify }: Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [d, setD] = useState<BriefList | null>(null)
  const [cur, setCur] = useState<Brief | null>(null)
  const [channel, setChannel] = useState('console')
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => rd.brief().then((r) => { setD(r); setCur((c) => c ?? r.brief) }).catch((e: Error) => notify(e.message, true)), [notify])
  useEffect(() => { load() }, [load])
  const make = async (push: boolean) => {
    setBusy(true)
    try { const r = await rd.makeBrief({ push, channel }); setCur(r.brief); await load(); notify(push ? '晨报已生成并推送' : '晨报已生成') } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  if (!d) return <Loading label="正在读取晨报" />
  return (
    <div className="rd-brief-layout">
      <div>
        <Panel title={cur ? cur.title : 'AI 晨报'} subtitle="汇总市场状态、因子/模型健康、组合风险、执行成本、模拟和影子运行、定时任务成果，以及等你决定的事。只陈述数据，不预测。"
          actions={<>
            <select value={channel} onChange={(e) => setChannel(e.target.value)} aria-label="推送通道">{CHANNELS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}</select>
            <button className="button small" disabled={busy} onClick={() => make(false)}>生成</button>
            <button className="button small primary" disabled={busy} onClick={() => make(true)}>生成并推送</button>
          </>}>
          {!cur ? <EmptyState glyph="☀" title="还没有晨报">点「生成」先看一份；开启定时任务后，每次执行结束会自动生成。</EmptyState> : (
            <>
              <p className="rd-headline">{cur.headline}</p>
              {cur.focus.length > 0 && (
                <div className="rd-focus"><b>今天先看</b><ol>{cur.focus.map((f, i) => <li key={i}>{f}</li>)}</ol></div>
              )}
              <div className="rd-sections">
                {cur.sections.map((s) => (
                  <section key={s.key} className={`rd-section${s.tone ? ` ${s.tone}` : ''}`}>
                    <h3>{s.title}{s.note && <span className="muted"> · {s.note}</span>}</h3>
                    <ul>{s.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
                  </section>
                ))}
              </div>
              <div className="hint">{when(cur.at)} · {cur.source === 'nightshift' ? '定时任务结束后自动生成' : cur.source === 'schedule' ? '定时生成' : '手动生成'}{cur.pushed.length ? ` · 已推送到 ${cur.pushed.join('、')}` : ''}</div>
            </>
          )}
        </Panel>
      </div>
      <Panel title="往期">
        {d.history.length === 0 ? <div className="hint">还没有往期。</div> : (
          <ul className="plain-list">
            {d.history.map((h) => (
              <li key={h.id} className={`clickable${cur?.id === h.id ? ' rd-on' : ''}`} onClick={async () => setCur((await rd.briefOne(h.id)).brief)}>
                <div className="row-between"><b>{when(h.at)}</b>{h.pushed.length ? <Tag tone="ok">已推送</Tag> : null}</div>
                <small className="ellipsis">{h.headline}</small>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  )
}

