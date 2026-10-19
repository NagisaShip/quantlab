/**
 * 上线验证：一个策略从回测走到模拟盘要过的几道关。
 * 晋级关卡（阶段与数字门槛）· 策略体检单（未来函数 / 预热 / 成本 / 参数平台 + 红队）· 仿真真实度 · 压力演练 · AI 值守员。
 * 路由：#/validation · #/validation/checkup · #/validation/fidelity · #/validation/stress · #/validation/watcher · #/validation/decay
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/core'
import { v3 } from '../api/research'
import type { PaperSessionView } from '../api/core'
import { LineChart } from '../components/charts2'
import { Markdown } from '../components/markdown'
import { Callout, EmptyState, Heading, Loading, MetricCard, Modal, Panel, Segments, Tag } from '../components/ui'
import {
  validation, type CheckupItem, type CheckupRun, type FidelityPart, type FidelityReport, type GradDetail, type GradRow, type StressRun, type StressScenario, type WatchReview,
} from '../apiValidation'
import type { PageProps } from './types'
import LifecycleView from './LifecycleView'
import { appConfirm } from '../components/dialog'

type Tab = 'gate' | 'checkup' | 'fidelity' | 'stress' | 'watcher' | 'decay'
const TABS: { value: Tab; label: string }[] = [
  { value: 'gate', label: '晋级关卡' }, { value: 'checkup', label: '策略体检' }, { value: 'fidelity', label: '仿真真实度' },
  { value: 'stress', label: '压力演练' }, { value: 'watcher', label: 'AI 值守员' }, { value: 'decay', label: '衰减与重训' },
]
const STAGES = ['backtest', 'paper', 'demo', 'graduated']
const STAGE_SHORT: Record<string, string> = { backtest: '回测', paper: '纸面模拟', demo: '模拟盘', graduated: '毕业' }
const when = (ms?: number | null) => (ms ? new Date(ms).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—')
const money = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${v >= 0 ? '+' : '−'}${Math.abs(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`)
const pct = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`)
const execLabel = (e: string) => (e === 'live' ? '微实盘' : e === 'demo' ? '交易所模拟盘' : '纸面模拟')

export default function ValidationPage({ arg, onNavigate, notify }: PageProps & { arg?: string }) {
  const [base, sub] = (arg || '').split(':')
  const tab: Tab = (['checkup', 'fidelity', 'stress', 'watcher', 'decay'] as Tab[]).includes(base as Tab) ? (base as Tab) : 'gate'
  return (
    <>
      <Heading title="上线验证" description="策略从回测走到交易所模拟盘要过的几道关：每一级都有数字门槛并由你确认；回测、纸面模拟和模拟盘的结果差在哪里一项项拆开；在历史上最极端的几天里加上断线和拒单演练一遍。毕业的策略可以上微实盘（小额真实资金，要另外确认）。" />
      <div className="fx-tabs">
        <Segments value={tab} options={TABS} onChange={(t) => onNavigate(t === 'gate' ? 'validation' : `validation/${t}`)} ariaLabel="上线验证视图" />
      </div>
      {tab === 'gate' && <GateView notify={notify} onNavigate={onNavigate} initialSlug={base === 'gate' ? sub : undefined} />}
      {tab === 'checkup' && <CheckupView notify={notify} initialSlug={sub} />}
      {tab === 'fidelity' && <FidelityView notify={notify} />}
      {tab === 'stress' && <StressView notify={notify} />}
      {tab === 'watcher' && <WatcherView notify={notify} />}
      {tab === 'decay' && <LifecycleView notify={notify} />}
    </>
  )
}

/* ------------------------------------------------------------------ */
/* 晋级关卡                                                             */
/* ------------------------------------------------------------------ */
function GateView({ notify, onNavigate, initialSlug }: Pick<PageProps, 'notify' | 'onNavigate'> & { initialSlug?: string }) {
  const [rows, setRows] = useState<GradRow[] | null>(null)
  const [cfg, setCfg] = useState<Record<string, number>>({})
  const [open, setOpen] = useState<GradDetail | null>(null)
  const load = useCallback(() => validation.board().then((r) => { setRows(r.board); setCfg(r.config) }).catch((e: Error) => notify(e.message, true)), [notify])
  useEffect(() => { load() }, [load])
  const show = useCallback(async (slug: string) => { try { setOpen(await validation.grad(slug)) } catch (e) { notify((e as Error).message, true) } }, [notify])
  const removeStrategy = useCallback(async (slug: string, name: string) => {
    if (!await appConfirm(`删除策略「${name}」？代码目录与全部历史版本会真实删除，不能恢复；已有回测和运行记录会保留。`, { danger: true })) return
    try {
      await v3.deleteStrategy(slug)
      if (open?.strategy === slug) setOpen(null)
      await load()
      notify('策略及代码目录已删除')
    } catch (e) { notify((e as Error).message, true) }
  }, [open, load, notify])
  // 从首页的流水线点进来：直接打开这个策略的晋级检查
  useEffect(() => { if (initialSlug) show(initialSlug) }, [initialSlug, show])
  if (!rows) return <Loading label="正在读取策略阶段" />
  const count = (s: string) => rows.filter((r) => r.stage === s).length
  return (
    <>
      <div className="metrics-grid">
        {STAGES.map((s) => <MetricCard key={s} label={STAGE_SHORT[s]} value={String(count(s))} unit="个" caption={
          s === 'backtest' ? `晋级要 DSR ≥ ${cfg.min_dsr ?? 0.95}` : s === 'paper' ? `跑满 ${cfg.paper_days ?? 7} 天才能上模拟盘` : s === 'demo' ? `跑满 ${cfg.demo_days ?? 14} 天才能毕业` : '可上微实盘'} />)}
      </div>
      <Panel flush title="策略阶段">
        {rows.length === 0 ? <EmptyState glyph="⌗" title="还没有策略">先在「策略」里写一个，回测之后回到这里。</EmptyState> : (
          <div className="table-wrap">
            <table className="fx-table">
              <thead><tr><th>策略</th><th>阶段</th><th>下一步</th><th>检查</th><th /></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.strategy} className="clickable" onClick={() => show(r.strategy)}>
                    <td><b>{r.name}</b></td>
                    <td><StageTrack stage={r.stage} /></td>
                    <td className="muted">{r.next ? `→ ${STAGE_SHORT[r.next]}` : '已毕业'}</td>
                    <td>{r.next ? (r.passed ? <Tag tone="ok">可以晋级</Tag> : <span className="muted">{r.progress} 通过</span>) : '—'}</td>
                    <td className="right">
                      <span className="button-row tight">
                        <button className="button small" onClick={(e) => { e.stopPropagation(); void show(r.strategy) }}>查看</button>
                        <button className="button small danger" onClick={(e) => { e.stopPropagation(); void removeStrategy(r.strategy, r.name) }}>删除</button>
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      {open && <GateModal d={open} name={rows.find((r) => r.strategy === open.strategy)?.name ?? open.strategy} onClose={() => setOpen(null)} notify={notify} onNavigate={onNavigate}
        onDelete={() => void removeStrategy(open.strategy, rows.find((r) => r.strategy === open.strategy)?.name ?? open.strategy)}
        onChanged={(d) => { setOpen(d); load() }} />}
    </>
  )
}

function StageTrack({ stage }: { stage: string }) {
  const i = STAGES.indexOf(stage)
  return (
    <span className="vd-track" aria-label={`当前阶段：${STAGE_SHORT[stage]}`}>
      {STAGES.map((s, k) => <span key={s} className={k < i ? 'done' : k === i ? 'here' : ''}>{STAGE_SHORT[s]}</span>)}
    </span>
  )
}

function GateModal({ d, name, onClose, onChanged, notify, onNavigate, onDelete }: {
  d: GradDetail; name: string; onClose: () => void; onChanged: (d: GradDetail) => void; onDelete: () => void
} & Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [busy, setBusy] = useState(false)
  const promote = async () => {
    if (!await appConfirm(`确认把这个策略从「${d.stage_label}」晋级到「${d.next_label}」？\n晋级会记入审计。毕业不会自动下实盘单。`)) return
    setBusy(true)
    try { onChanged(await validation.promote(d.strategy)); notify(`已晋级到 ${d.next_label}`) } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const demote = async () => {
    const prev = STAGES[Math.max(0, STAGES.indexOf(d.stage) - 1)]
    if (!await appConfirm(`退回到「${STAGE_SHORT[prev]}」？`)) return
    try { onChanged(await validation.demote(d.strategy, prev)) } catch (e) { notify((e as Error).message, true) }
  }
  const hint = d.next === 'paper' ? '去「回测」再跑一次，或到「试验台账」看试验次数' : d.next === 'demo' ? '去「纸面模拟」开一个纸面模拟，跑满后在「仿真真实度」生成报告' : d.next === 'graduated' ? '去「纸面模拟」用交易所模拟盘跑满后生成真实度报告' : ''
  return (
    <Modal title={<>{name} <StageTrack stage={d.stage} /></>} onClose={onClose} width={640}
      footer={<>
        {d.stage !== 'backtest' && <button className="button small" onClick={demote}>退回上一级</button>}
        <button className="button small danger" onClick={onDelete}>删除策略</button>
        <div className="spacer" />
        {d.next && <button className="button primary" disabled={!d.passed || busy} onClick={promote}
          title={d.passed ? '' : '还有检查没通过'}>晋级到{d.next_label}</button>}
      </>}>
      {d.next ? <>
        <p className="muted" style={{ marginTop: 0 }}>晋级到「{d.next_label}」需要全部通过：</p>
        <ul className="vd-checks">
          {d.checks.map((c) => (
            <li key={c.label} className={c.ok ? 'ok' : c.ok === false ? 'bad' : 'na'}>
              <span className="mark">{c.ok ? '✓' : c.ok === false ? '✕' : '…'}</span>
              <span className="lbl">{c.label}</span><span className="val">{c.value}</span><span className="need muted">{c.need}</span>
            </li>
          ))}
        </ul>
        {!d.passed && hint && <div className="hint">{hint}。<a href="#" onClick={(e) => { e.preventDefault(); onNavigate(d.next === 'paper' ? 'backtests' : 'paper') }}>前往</a></div>}
      </> : <Callout tone="info">已经毕业。平台的实盘下单仍然是关闭的，这个标记只说明它在模拟盘上经过了完整验证。</Callout>}
      {d.history.length > 0 && <>
        <h4 className="vd-h">记录</h4>
        <ul className="vd-hist">{d.history.slice().reverse().map((h, i) => (
          <li key={i}><span className="muted">{when(h.at)}</span> {STAGE_SHORT[h.from]} → {STAGE_SHORT[h.to]}{h.note ? ` · ${h.note}` : ''}</li>
        ))}</ul>
      </>}
    </Modal>
  )
}

/* ------------------------------------------------------------------ */
/* 仿真真实度                                                           */
/* ------------------------------------------------------------------ */
function useSessions(notify: PageProps['notify']) {
  const [sessions, setSessions] = useState<(PaperSessionView & { execution?: string })[] | null>(null)
  useEffect(() => { api.paperSessions().then((r) => setSessions(r.sessions)).catch((e: Error) => notify(e.message, true)) }, [notify])
  return sessions
}

function FidelityView({ notify }: Pick<PageProps, 'notify'>) {
  const sessions = useSessions(notify)
  const [picked, setPicked] = useState<string[]>([])
  const [rep, setRep] = useState<FidelityReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [history, setHistory] = useState<{ id: string; strategy_name: string; created_at: number; deviation: FidelityReport['deviation'] }[]>([])
  useEffect(() => { validation.fidelityList().then((r) => { setHistory(r.reports); if (r.reports[0]) validation.fidelityGet(r.reports[0].id).then((x) => setRep(x.report)).catch(() => {}) }).catch(() => {}) }, [])
  const withFills = (sessions ?? []).filter((s) => (s.fills?.length ?? 0) > 0)
  const strategy = picked.length ? withFills.find((s) => s.id === picked[0])?.strategy_id : null
  const toggle = (s: PaperSessionView) => setPicked((p) => p.includes(s.id) ? p.filter((x) => x !== s.id) : strategy && s.strategy_id !== strategy ? p : [...p, s.id].slice(-3))
  const run = async () => {
    setBusy(true)
    try { const r = await validation.fidelity(picked); setRep(r.report); notify('报告已生成') } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  if (!sessions) return <Loading label="正在读取实验" />
  return (
    <>
      <Callout>
        选同一个策略的纸面模拟和交易所模拟盘实验（可以只选一个）。平台会在它们实际跑过的那段行情上，用同一份冻结代码重跑一次回测，
        把收益差拆成延迟、滑点、手续费、拒单、部分成交和其他几项，并用模拟盘的真实成交给回测成本模型出校准建议。
      </Callout>
      <Panel flush title="选择实验" actions={<button className="button small primary" disabled={!picked.length || busy} onClick={run}>
        {busy ? <><span className="spinner" /> 正在重跑回测</> : '生成报告'}</button>}>
        {withFills.length === 0 ? <EmptyState glyph="◉" title="还没有带成交的实验">先在「纸面模拟」里跑一段，有了成交再来。</EmptyState> : (
          <div className="table-wrap"><table className="fx-table">
            <thead><tr><th /><th>策略</th><th>方式</th><th>行情</th><th className="right">成交</th><th>运行</th></tr></thead>
            <tbody>{withFills.map((s) => {
              const dis = !!strategy && s.strategy_id !== strategy
              return (
                <tr key={s.id} className={dis ? 'muted' : 'clickable'} onClick={() => !dis && toggle(s)}>
                  <td><input type="checkbox" checked={picked.includes(s.id)} disabled={dis} readOnly aria-label="选择" /></td>
                  <td>{s.strategy_name}</td><td>{execLabel(s.execution ?? 'paper')}</td>
                  <td className="muted">{s.source === 'demo' ? '演示行情' : '真实行情'}</td>
                  <td className="right num">{s.fills.length}</td>
                  <td className="muted">{(s.elapsed_hours / 24).toFixed(1)} 天</td>
                </tr>)
            })}</tbody>
          </table></div>
        )}
      </Panel>
      {rep && <FidelityReportView rep={rep} notify={notify} />}
      {history.length > 1 && (
        <Panel flush title="以前的报告">
          <div className="table-wrap"><table className="fx-table"><tbody>
            {history.slice(0, 10).map((h) => (
              <tr key={h.id} className="clickable" onClick={() => validation.fidelityGet(h.id).then((x) => setRep(x.report))}>
                <td>{h.strategy_name}</td><td className="muted">{when(h.created_at)}</td>
                <td>{Object.entries(h.deviation).map(([k, v]) => <span key={k} className="muted">{execLabel(k)} 偏差 {v.deviation_bps?.toFixed(0) ?? '—'} bp　</span>)}</td>
              </tr>))}
          </tbody></table></div>
        </Panel>
      )}
    </>
  )
}

function FidelityReportView({ rep, notify }: { rep: FidelityReport } & Pick<PageProps, 'notify'>) {
  const [tab, setTab] = useState(rep.parts[0]?.execution ?? 'paper')
  useEffect(() => { setTab(rep.parts[0]?.execution ?? 'paper') }, [rep.id, rep.parts])
  const part = rep.parts.find((p) => p.execution === tab) ?? rep.parts[0]
  const cal = rep.calibration
  return (
    <Panel title={`真实度报告 · ${rep.strategy_name}`} actions={<span className="muted">{when(rep.window.start)} → {when(rep.window.end)} · {rep.window.timeframe}</span>}>
      {rep.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
      <div className="metrics-grid">
        <MetricCard label="回测（同一段行情）" value={pct(rep.backtest.return, 2)} caption={`${rep.backtest.fills} 笔成交`} />
        {rep.parts.map((p) => <MetricCard key={p.execution} label={`${execLabel(p.execution)} vs 回测`} value={money(p.total)}
          caption={`${p.total_bps === null ? '—' : `${p.total_bps.toFixed(0)} bp`} · 配上 ${p.matched} 笔`} />)}
      </div>
      {rep.parts.length > 1 && <div style={{ margin: '6px 0 10px' }}><Segments value={tab} onChange={setTab}
        options={rep.parts.map((p) => ({ value: p.execution, label: execLabel(p.execution) }))} ariaLabel="对比对象" /></div>}
      {part && <Decomposition part={part} />}
      <h4 className="vd-h">成本校准</h4>
      {!cal.suggested ? <p className="muted">{cal.note}</p> : (
        <div className="vd-cal">
          <table className="fx-table"><thead><tr><th /><th className="right">现在的回测</th><th className="right">模拟盘实测</th><th className="right">建议</th></tr></thead>
            <tbody>
              <tr><td>滑点</td><td className="right num">{cal.current.slippage_bps} bp</td><td className="right num">{cal.observed?.slippage_bps_median} bp<small className="muted">（含半个价差）</small></td><td className="right num"><b>{cal.suggested.slippage_bps} bp</b></td></tr>
              <tr><td>手续费率</td><td className="right num">{(cal.current.taker_fee * 100).toFixed(3)}%</td><td className="right num">{cal.observed?.fee_rate !== null && cal.observed?.fee_rate !== undefined ? `${(cal.observed.fee_rate * 100).toFixed(3)}%` : '—'}</td><td className="right num"><b>{(cal.suggested.taker_fee * 100).toFixed(3)}%</b></td></tr>
              <tr><td>拒单率 / 部分成交</td><td className="right muted">不模拟</td><td className="right num">{pct(cal.observed?.reject_rate)} / {pct(cal.observed?.partial_rate)}</td><td /></tr>
              <tr><td>送单到成交</td><td className="right muted">下一根开盘</td><td className="right num">{cal.observed?.latency_ms_median !== null && cal.observed?.latency_ms_median !== undefined ? `${(cal.observed.latency_ms_median / 1000).toFixed(1)} 秒` : '—'}</td><td /></tr>
            </tbody></table>
          <div className="dh-actions">
            <button className="button small primary" onClick={async () => {
              if (!await appConfirm(`把这家交易所的回测成本设为：滑点 ${cal.suggested!.slippage_bps} bp、手续费 ${(cal.suggested!.taker_fee * 100).toFixed(3)}%？\n回测表单里可以一键套用。`)) return
              try { await validation.applyCalibration(rep.id); notify('已保存校准值') } catch (e) { notify((e as Error).message, true) }
            }}>用作回测成本</button>
            <span className="muted">{cal.note} · 可信度 {({ low: '低', medium: '中', high: '高', none: '—' } as const)[cal.confidence]}</span>
          </div>
        </div>
      )}
      <ul className="vd-notes">{rep.notes.map((n) => <li key={n}>{n}</li>)}</ul>
    </Panel>
  )
}

function Decomposition({ part }: { part: FidelityPart }) {
  const max = Math.max(1e-9, ...part.components.map((c) => Math.abs(c.value)))
  return (
    <>
      <div className="vd-bars" role="list" aria-label="收益差拆解">
        {part.components.map((c) => (
          <div key={c.key} className="vd-bar" role="listitem">
            <span className="lbl">{c.label}</span>
            <span className="track"><i className={c.value < 0 ? 'neg' : 'pos'} style={{ width: `${(Math.abs(c.value) / max) * 50}%`, [c.value < 0 ? 'right' : 'left']: '50%' } as React.CSSProperties} /></span>
            <span className={`num ${c.value < 0 ? 'negative' : c.value > 0 ? 'positive' : 'muted'}`}>{money(c.value)}</span>
            <span className="muted num">{c.bps === null ? '' : `${c.bps.toFixed(1)} bp`}</span>
          </div>
        ))}
        <div className="vd-bar total"><span className="lbl">合计</span><span className="track" /><span className="num"><b>{money(part.total)}</b></span>
          <span className="muted num">{part.total_bps === null ? '' : `${part.total_bps.toFixed(1)} bp`}</span></div>
      </div>
      <p className="hint">配上 {part.matched} 笔；只在回测里有 {part.only_backtest} 笔（其中拒单 {part.only_backtest - part.missed}）；只在实时里有 {part.only_live} 笔。正数表示实时比回测多赚。</p>
      {part.rows.length > 0 && (
        <details className="vd-rows"><summary>逐笔对照（{part.rows.length}）</summary>
          <div className="table-wrap"><table className="fx-table">
            <thead><tr><th>决策</th><th>标的</th><th>方向</th><th className="right">回测价</th><th className="right">实时价</th><th className="right">数量 回测/实时</th><th className="right">延迟</th><th className="right">滑点</th><th className="right">手续费</th></tr></thead>
            <tbody>{part.rows.slice().reverse().slice(0, 100).map((r, i) => (
              <tr key={i}>
                <td className="muted num">{when(r.decided_at)}</td><td>{r.security}</td><td>{r.side === 'buy' ? '买' : '卖'}</td>
                <td className="right num">{r.bt?.price.toLocaleString() ?? '—'}</td>
                <td className="right num">{r.live ? r.live.price.toLocaleString() : <Tag tone="fail">{r.rejected ? '被拒' : '无'}</Tag>}</td>
                <td className="right num">{r.bt?.qty ?? '—'} / {r.live?.qty ?? 0}</td>
                <td className="right num">{r.latency !== undefined ? money(r.latency) : ''}</td>
                <td className="right num">{r.slippage !== undefined ? money(r.slippage) : ''}</td>
                <td className="right num">{r.fees !== undefined ? money(r.fees) : r.rejections !== undefined ? `拒单 ${money(r.rejections)}` : ''}</td>
              </tr>))}</tbody>
          </table></div>
        </details>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
/* 压力演练                                                             */
/* ------------------------------------------------------------------ */
function StressView({ notify }: Pick<PageProps, 'notify'>) {
  const [strategies, setStrategies] = useState<{ slug: string; name: string }[] | null>(null)
  const [scen, setScen] = useState<StressScenario[]>([])
  const [slug, setSlug] = useState('')
  const [faults, setFaults] = useState(true)
  const [runs, setRuns] = useState<StressRun[]>([])
  const [open, setOpen] = useState<StressRun | null>(null)
  const openRun = useCallback((id: string) => validation.stressGet(id).then((r) => setOpen(r.run)).catch(() => {}), [])
  const load = useCallback(() => validation.stressList().then((r) => {
    setRuns(r.runs)
    setOpen((o) => {
      const cur = o ? r.runs.find((x) => x.id === o.id) : r.runs.find((x) => x.status === 'done')
      if (cur && cur.status !== 'running' && (!o || o.status === 'running' || !o.report?.scenarios)) openRun(cur.id)
      return o && cur?.status === 'running' ? { ...o, ...cur } : o
    })
  }).catch(() => {}), [openRun])
  useEffect(() => {
    v3.strategies().then((r) => { const xs = r.strategies.map((s) => ({ slug: s.slug, name: s.name })); setStrategies(xs); if (xs[0]) setSlug(xs[0].slug) })
      .catch((e: Error) => notify(e.message, true))
    validation.stressScenarios().then((r) => setScen(r.scenarios)).catch(() => {})
    load()
  }, [notify, load])
  const running = runs.some((r) => r.status === 'running')
  useEffect(() => { if (!running) return; const t = window.setInterval(load, 2000); return () => window.clearInterval(t) }, [running, load])
  const start = async () => {
    try { const r = await validation.stressStart({ slug, faults }); setOpen(r.run); load() } catch (e) { notify((e as Error).message, true) }
  }
  if (!strategies) return <Loading label="正在读取策略" />
  return (
    <>
      <Panel title="演练设置">
        <div className="vd-scen">{scen.map((s) => <div key={s.id} className="vd-scen-card"><b>{s.name}</b><span className="muted">{s.days.join(' ~ ')}</span><p>{s.description}</p></div>)}</div>
        <div className="dh-actions">
          <select value={slug} onChange={(e) => setSlug(e.target.value)} aria-label="策略">{strategies.map((s) => <option key={s.slug} value={s.slug}>{s.name}</option>)}</select>
          <label className="check-row"><input type="checkbox" checked={faults} onChange={(e) => setFaults(e.target.checked)} /> 注入断线、拒单、延迟和部分成交</label>
          <button className="button small primary" disabled={!slug || running} onClick={start}>{running ? <><span className="spinner" /> 演练中</> : '开始演练'}</button>
        </div>
        <div className="hint">用 1 分钟 K 线加速回放，走的是实盘模拟同一条下单链路（委托 → 模拟交易所回报 → 记账）。取不到历史数据时如实报「未接入数据」，不会用合成行情代替。</div>
      </Panel>
      {open && <StressReport run={open} />}
      {runs.length > 0 && (
        <Panel flush title="演练记录">
          <div className="table-wrap"><table className="fx-table"><tbody>
            {runs.slice(0, 12).map((r) => (
              <tr key={r.id} className="clickable" onClick={() => (r.status === 'running' ? setOpen(r) : openRun(r.id))}>
                <td>{r.strategy_name}</td><td className="muted">{when(r.created_at)}</td>
                <td>{r.status === 'running' ? <Tag tone="warn">{r.progress || '进行中'}</Tag> : r.status === 'failed' ? <Tag tone="fail">失败</Tag>
                  : r.report?.passed ? <Tag tone="ok">全部跑完</Tag> : <Tag tone="warn">有场景中断</Tag>}</td>
                <td className="muted">{r.report?.summary?.verdict ?? ''}</td>
              </tr>))}
          </tbody></table></div>
        </Panel>
      )}
    </>
  )
}

function StressReport({ run }: { run: StressRun }) {
  if (run.status === 'running') return <Panel title={`演练中 · ${run.strategy_name}`}><Loading label={run.progress || '正在回放'} /></Panel>
  if (run.status === 'failed') return <Callout tone="fail">演练失败：{run.error}</Callout>
  const sc = run.report?.scenarios ?? []
  return (
    <Panel title={`演练结果 · ${run.strategy_name}`} actions={run.report?.summary ? <span className="muted">{run.report.summary.verdict}</span> : undefined}>
      <div className="vd-stress">
        {sc.map((s) => (
          <div key={s.id} className="vd-stress-card">
            <div className="dh-head"><b>{s.name}</b><span className="muted">{s.days.join(' ~ ')}</span>{s.data_source === 'synthetic' && <Tag tone="warn">合成行情</Tag>}<span className="dh-gap" />
              <Tag tone={!s.completed ? 'fail' : s.drawdown_breach || s.liquidations || s.strategy_errors ? 'warn' : 'ok'}>{s.completed ? (s.drawdown_breach || s.liquidations ? '有风险' : '扛住了') : '中断'}</Tag></div>
            {s.completed ? <>
              <LineChart height={130} width={420} format="num" zeroLine={false} ariaLabel={`${s.name} 权益`}
                series={[{ name: '权益', values: s.equity_curve.map((p) => p[1]) }]} timestamps={s.equity_curve.map((p) => p[0])} />
              <div className="vd-kv">
                <span>收益 <b className={s.return_pct < 0 ? 'negative' : 'positive'}>{pct(s.return_pct, 2)}</b></span>
                <span>最大回撤 <b>{pct(s.max_drawdown)}</b></span>
                <span>成交 {s.fills} / 委托 {s.orders}</span>
                <span>拒单 {s.rejections} · 延迟 {s.delayed} · 部分成交 {s.partials}</span>
                <span>断线 {s.disconnects.length} 次{s.disconnects.length ? `，共 ${s.disconnects.reduce((a, d) => a + d.minutes, 0)} 分钟` : ''}</span>
                {s.liquidations > 0 && <span className="negative">强平 {s.liquidations} 次，损失 {s.liquidation_loss.toFixed(2)}</span>}
                {s.strategy_errors > 0 && <span className="negative">策略报错 {s.strategy_errors} 次</span>}
              </div>
              <div className="vd-verdict">{s.verdict}</div>
            </> : <Callout tone="fail">{s.error || s.verdict}</Callout>}
            {(s.warnings ?? []).map((w) => <div key={w} className="hint">{w}</div>)}
          </div>
        ))}
      </div>
    </Panel>
  )
}

/* ------------------------------------------------------------------ */
/* 策略体检单                                                           */
/* ------------------------------------------------------------------ */
const VERDICT: Record<string, { tone: 'ok' | 'warn' | 'fail' | 'muted'; label: string }> = {
  ok: { tone: 'ok', label: '通过' }, warn: { tone: 'warn', label: '注意' }, bad: { tone: 'fail', label: '严重' }, skip: { tone: 'muted', label: '未检查' },
}
const SEV: Record<string, { tone: 'ok' | 'warn' | 'fail' | 'muted'; label: string }> = {
  high: { tone: 'fail', label: '严重' }, medium: { tone: 'warn', label: '注意' }, low: { tone: 'muted', label: '提示' },
}
const CHECK_HELP: Record<string, string> = {
  lookahead: '在 6 个时间点截断数据重新计算（因子、模型也按截断后的数据重算），截断前的每个信号和 record() 指标必须和完整数据一致。',
  warmup: '同一个计分起点，给 1/4、1/2、1、2 倍预热，看信号是否一致。',
  cost: '手续费、滑点、价差都翻倍再跑一次。',
  plateau: '每个数值参数上下挪 10%、20%，看夏普还剩多少。',
}

function CheckupView({ notify, initialSlug }: Pick<PageProps, 'notify'> & { initialSlug?: string }) {
  const [strategies, setStrategies] = useState<{ slug: string; name: string }[] | null>(null)
  const [slug, setSlug] = useState(initialSlug || '')
  const [runs, setRuns] = useState<CheckupRun[]>([])
  const [open, setOpen] = useState<{ run: CheckupRun; stale: boolean } | null>(null)
  const openRun = useCallback((id: string) => validation.checkupGet(id).then(setOpen).catch(() => {}), [])
  const load = useCallback(() => validation.checkupList().then((r) => {
    setRuns(r.runs)
    setOpen((o) => {
      const cur = o ? r.runs.find((x) => x.id === o.run.id) : r.runs.find((x) => x.status === 'done')
      if (cur && cur.status !== 'running' && (!o || o.run.status === 'running' || !o.run.report)) openRun(cur.id)
      return o && cur?.status === 'running' ? { ...o, run: { ...o.run, ...cur } } : o
    })
  }).catch(() => {}), [openRun])
  useEffect(() => {
    v3.strategies().then((r) => {
      const xs = r.strategies.map((s) => ({ slug: s.slug, name: s.name })); setStrategies(xs)
      setSlug((cur) => cur || xs[0]?.slug || '')
    }).catch((e: Error) => notify(e.message, true))
    load()
  }, [notify, load])
  const running = runs.some((r) => r.status === 'running')
  useEffect(() => { if (!running) return; const t = window.setInterval(load, 2000); return () => window.clearInterval(t) }, [running, load])
  const start = async () => {
    try { const r = await validation.checkupStart({ slug }); setOpen({ run: r.run, stale: false }); load() } catch (e) { notify((e as Error).message, true) }
  }
  if (!strategies) return <Loading label="正在读取策略" />
  return (
    <>
      <Panel title="给策略做一次体检">
        <div className="dh-actions">
          <select value={slug} onChange={(e) => setSlug(e.target.value)} aria-label="策略">{strategies.map((s) => <option key={s.slug} value={s.slug}>{s.name}</option>)}</select>
          <button className="button small primary" disabled={!slug || running} onClick={start}>{running ? <><span className="spinner" /> 体检中</> : '开始体检'}</button>
        </div>
        <div className="hint">四项自动检查（未来函数、预热、成本翻倍、参数平台），再由 AI 红队列出这个策略最可能亏钱的原因。
          晋级关卡要求：当前这版代码体检过，并且没有"严重"项。</div>
      </Panel>
      {open && <CheckupReport run={open.run} stale={open.stale} />}
      {runs.length > 0 && (
        <Panel flush title="体检记录">
          <div className="table-wrap"><table className="fx-table"><tbody>
            {runs.slice(0, 12).map((r) => (
              <tr key={r.id} className="clickable" onClick={() => (r.status === 'running' ? setOpen({ run: r, stale: false }) : openRun(r.id))}>
                <td>{r.strategy_name}</td><td className="muted">{when(r.created_at)}</td>
                <td>{r.status === 'running' ? <Tag tone="warn">{r.progress || '进行中'}</Tag> : r.status === 'failed' ? <Tag tone="fail">失败</Tag>
                  : <Tag tone={VERDICT[r.overall ?? 'skip'].tone}>{VERDICT[r.overall ?? 'skip'].label}</Tag>}</td>
                <td className="vd-dots">{(r.items ?? []).map((i) => <span key={i.id} title={`${i.title}：${VERDICT[i.verdict].label}`} className={`vd-dot ${i.verdict}`} />)}</td>
              </tr>))}
          </tbody></table></div>
        </Panel>
      )}
    </>
  )
}

function CheckupReport({ run, stale }: { run: CheckupRun; stale: boolean }) {
  if (run.status === 'running') return <Panel title={`体检中 · ${run.strategy_name}`}><Loading label={run.progress || '正在检查'} /></Panel>
  if (run.status === 'failed') return <Callout tone="fail" title="体检没能完成">{run.error}</Callout>
  const rep = run.report
  if (!rep) return null
  const red = run.redteam
  const m = rep.metrics
  return (
    <>
      {stale && <Callout tone="warn" title="代码改过了">这份体检单是旧版本代码的结果，晋级关卡不认。重新体检一次即可。</Callout>}
      <Panel title={`体检单 · ${run.strategy_name}`} subtitle={`${run.securities?.join('、') ?? ''} · ${run.timeframe ?? ''} · 预热 ${rep.warmup} 根 · 用时 ${rep.elapsed_s} 秒`}
        actions={<Tag tone={VERDICT[rep.overall].tone}>{rep.overall === 'bad' ? '有严重问题' : rep.overall === 'warn' ? '有需要注意的地方' : rep.overall === 'ok' ? '全部通过' : '未完成'}</Tag>}>
        <div className="vd-kv" style={{ marginBottom: 10 }}>
          <span>收益 <b>{pct(m.total_return as number, 2)}</b></span><span>夏普 <b>{m.sharpe == null ? '—' : Number(m.sharpe).toFixed(2)}</b></span>
          <span>最大回撤 <b>{pct(m.max_drawdown as number)}</b></span><span>完成交易 <b>{m.trades ?? '—'}</b></span>
        </div>
        <div className="vd-ck-grid">
          {rep.items.map((i) => <CheckupCard key={i.id} item={i} />)}
        </div>
        {(run.warnings ?? []).map((w) => <div key={w} className="hint">{w}</div>)}
      </Panel>
      {red && (
        <Panel title="红队意见" subtitle={red.mode === 'llm' ? `由 ${red.model || '大模型'} 扮演红队` : (red.note || '规则清单（未调用大模型）')}>
          {red.content && <div className="vd-red-text"><Markdown text={red.content} /></div>}
          <ul className="vd-red">
            {red.points.map((p, i) => (
              <li key={i}><Tag tone={SEV[p.severity].tone}>{SEV[p.severity].label}</Tag><div><b>{p.title}</b><p>{p.detail}</p></div></li>
            ))}
          </ul>
        </Panel>
      )}
    </>
  )
}

function CheckupCard({ item }: { item: CheckupItem }) {
  const [more, setMore] = useState(false)
  const d = (item.details ?? {}) as Record<string, unknown>
  const rows = (d.rows as Record<string, unknown>[] | undefined) ?? []
  const cuts = (d.cuts as Record<string, unknown>[] | undefined) ?? []
  return (
    <div className={`vd-ck ${item.verdict}`}>
      <div className="dh-head"><b>{item.title}</b><span className="dh-gap" /><Tag tone={VERDICT[item.verdict].tone}>{VERDICT[item.verdict].label}</Tag></div>
      <p>{item.summary}</p>
      <div className="hint">{CHECK_HELP[item.id]}</div>
      {(rows.length > 0 || cuts.length > 0) && <button className="link" onClick={() => setMore((x) => !x)}>{more ? '收起明细' : '看明细'}</button>}
      {more && item.id === 'lookahead' && (
        <table className="fx-table compact"><thead><tr><th>截断点</th><th className="right">比较的信号</th><th>结果</th></tr></thead><tbody>
          {cuts.map((c, k) => <tr key={k}><td>{when(c.cut_ts as number)}</td><td className="right">{String(c.signals_compared)}</td><td>{c.same ? '一致' : <span className="negative">不一致</span>}</td></tr>)}
        </tbody></table>
      )}
      {more && item.id === 'warmup' && (
        <table className="fx-table compact"><thead><tr><th>预热</th><th className="right">信号</th><th className="right">一致率</th></tr></thead><tbody>
          {rows.map((r, k) => <tr key={k}><td>{String(r.warmup)} 根</td><td className="right">{String(r.signals)}</td><td className="right">{pct(r.agree as number, 0)}</td></tr>)}
        </tbody></table>
      )}
      {more && item.id === 'plateau' && (
        <table className="fx-table compact"><thead><tr><th>参数</th><th className="right">取值</th><th className="right">挪动</th><th className="right">夏普</th></tr></thead><tbody>
          {rows.map((r, k) => <tr key={k}><td>{String(r.param)}</td><td className="right">{String(r.base)} → {String(r.value)}</td><td className="right">{pct(r.step as number, 0)}</td>
            <td className="right">{r.sharpe == null ? '—' : Number(r.sharpe).toFixed(2)}</td></tr>)}
        </tbody></table>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* AI 值守员                                                            */
/* ------------------------------------------------------------------ */
function WatcherView({ notify }: Pick<PageProps, 'notify'>) {
  const sessions = useSessions(notify)
  const [sid, setSid] = useState('')
  const [rv, setRv] = useState<WatchReview | null>(null)
  const [busy, setBusy] = useState(false)
  const list = useMemo(() => (sessions ?? []).filter((s) => (s.fills?.length ?? 0) > 0 || s.status === 'running'), [sessions])
  useEffect(() => {
    if (sid || !list.length) return
    validation.watchList().then((r) => r.reviews.find((x) => list.some((s) => s.id === x.id))?.id).catch(() => undefined)
      .then((reviewed) => setSid(reviewed ?? (list.find((s) => s.fills.length > 0) ?? list[0]).id))
  }, [list, sid])
  useEffect(() => { if (sid) validation.watchGet(sid).then((r) => setRv(r.review)).catch(() => setRv(null)) }, [sid])
  const run = async () => {
    setBusy(true)
    try { setRv((await validation.watch(sid)).review) } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  if (!sessions) return <Loading label="正在读取实验" />
  const errors = rv?.flags.filter((f) => f.level === 'error').length ?? 0
  return (
    <>
      <Callout>值守员逐笔解释成交，并对照策略说明书（SPEC.md）找矛盾：只做多却出现空仓、超过杠杆或仓位上限、说明书说该停了还在开仓等。它只看不动，结论会进 AI 晨报。</Callout>
      <Panel title="选择实验" actions={<button className="button small primary" disabled={!sid || busy} onClick={run}>{busy ? <><span className="spinner" /> 正在看</> : '现在看一遍'}</button>}>
        {list.length === 0 ? <EmptyState glyph="◉" title="还没有可以看的实验">在「纸面模拟」里启动一个实验。</EmptyState> : (
          <select value={sid} onChange={(e) => setSid(e.target.value)} aria-label="实验">
            {list.map((s) => <option key={s.id} value={s.id}>{s.strategy_name} · {execLabel(s.execution ?? 'paper')} · {s.fills.length} 笔成交</option>)}
          </select>
        )}
      </Panel>
      {!rv && sid && <EmptyState glyph="◉" title="这个实验还没看过">点「现在看一遍」；运行中的实验每天早上也会自动看一遍，结论进 AI 晨报。</EmptyState>}
      {rv && (
        <Panel title={errors ? `发现 ${errors} 处和说明书矛盾` : rv.flags.length ? `${rv.flags.length} 条需要留意` : '没有发现问题'}
          actions={<span className="muted">{when(rv.reviewed_at)} · {rv.tagger.kind === 'llm' ? `大模型 ${rv.tagger.model ?? ''}` : '规则'}{rv.has_spec ? '' : ' · 没有说明书，用了自动生成的初稿'}</span>}>
          {rv.tagger.llm_error && <Callout tone="warn">模型没有返回结果（{rv.tagger.llm_error.slice(0, 80)}），下面只有规则检查。</Callout>}
          {Object.keys(rv.rules).length > 0 && <div className="dh-fields">{Object.entries(rv.rules).map(([k, v]) => <code key={k}>{RULE_LABEL[k] ?? k}{v === true ? '' : `：${Array.isArray(v) ? v.join('、') : String(v)}`}</code>)}</div>}
          {rv.flags.length > 0 && <ul className="vd-flags">{rv.flags.slice().reverse().map((f, i) => (
            <li key={i} className={f.level}><Tag tone={f.level === 'error' ? 'fail' : 'warn'}>{f.level === 'error' ? '矛盾' : '留意'}</Tag> {f.text}{f.ts ? <span className="muted"> · {when(f.ts)}</span> : null}</li>))}</ul>}
          <div className="table-wrap"><table className="fx-table">
            <thead><tr><th>时间</th><th>成交</th><th>解释</th></tr></thead>
            <tbody>{rv.trades.slice().reverse().slice(0, 60).map((t) => (
              <tr key={t.fill_id || t.ts}><td className="muted num">{when(t.ts)}</td><td>{t.side === 'buy' ? '买' : '卖'} {t.security}</td>
                <td>{t.explanation_ai ? <><b>{t.explanation_ai}</b><br /><small className="muted">{t.explanation}</small></> : t.explanation}</td></tr>))}</tbody>
          </table></div>
        </Panel>
      )}
    </>
  )
}

const RULE_LABEL: Record<string, string> = {
  long_only: '只做多', max_leverage: '杠杆上限', max_weight: '仓位上限', max_trades_per_day: '每天最多', stop_drawdown: '回撤停止线',
  stop_losing_streak: '连亏停止', securities: '标的', spot_only: '只做现货',
}
