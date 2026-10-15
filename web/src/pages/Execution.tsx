/**
 * 下单执行（全部是仿真，没有任何下单路径）：执行方案库、仿真设计、回放监控、市场雷达、成本与容量、盘口录制、试验记录。
 * 路由：#/execution · /design · /replay · /radar · /costs · /record · /runs · /run/<id> · /<id>
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { v4, type ImportItem } from '../api3'
import {
  execution, type Capacity, type CostModel, type ExecConfig, type ExecMeta, type ExecReport, type ExecRun, type ExecScheme,
  type ExecSource, type ExecStatus, type RadarRow, type Recording, type Replay,
} from '../apiExecution'
import { usageSnippet } from '../apiModels'
import { openInLab } from '../components/handoff'
import {
  AlertList, bp, CostBreakdown, EXEC_STATUS_TONE, ExecutionFullReport, ReplayChart, SOURCE_LABEL, STATE_COLOR,
} from '../components/executionReport'
import { UseInStrategy } from '../components/modelReport'
import { EmptyState, Field, Heading, Loading, Panel, Segments, Tag } from '../components/ui'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

type Tab = 'list' | 'design' | 'replay' | 'radar' | 'costs' | 'record' | 'runs'
const TABS: { value: Tab; label: string }[] = [
  { value: 'list', label: '方案' }, { value: 'design', label: '仿真设计' }, { value: 'replay', label: '回放监控' },
  { value: 'radar', label: '市场雷达' }, { value: 'costs', label: '成本与容量' }, { value: 'record', label: '盘口录制' }, { value: 'runs', label: '试验记录' },
]
const STATUS_FALLBACK: Record<ExecStatus, string> = { candidate: '候选', active: '启用', retired: '退役' }
const when = (ms?: number | null) => (ms ? new Date(ms * (ms < 1e11 ? 1000 : 1)).toISOString().slice(5, 16).replace('T', ' ') : '—')
const pc = (v: number | null | undefined, d = 0) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`)
const money = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v >= 1e9 ? `${(v / 1e9).toFixed(1)}B` : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}K` : v.toFixed(0))
const REC_KEY = 'qa.exec.recording'

/** 比 TWAP 省多少：差距在半个 bp 内算持平，不上色。 */
const beatTone = (v?: number | null) => (v === null || v === undefined || Math.abs(v) < 0.5 ? '' : v > 0 ? 'ex-good' : 'ex-bad')

export default function ExecutionPage({ arg, onNavigate, notify }: PageProps & { arg?: string }) {
  const [meta, setMeta] = useState<ExecMeta | null>(null)
  useEffect(() => { execution.meta().then(setMeta).catch((e: Error) => notify(e.message, true)) }, [notify])
  const runId = arg?.startsWith('run/') ? arg.slice(4) : null
  const tab: Tab | null = !arg ? 'list' : (TABS.map((t) => t.value) as string[]).includes(arg) ? (arg as Tab) : null
  const algoLabel = (id: string) => meta?.algos.find((o) => o.id === id)?.label ?? id
  return (
    <>
      <Heading title="下单执行" description="把目标仓位变成一串子单：在盘口回放里仿真拆单算法，按交易成本分析（TCA）验收；感知市场状态、监控成交进度，并用真实成交校准成本。全部是仿真，不会下真实委托。"
        actions={<button className="button primary" onClick={() => openInLab({ idea: '', kind: 'execution' })}>✦ 让 AI 设计执行方案</button>} />
      <div className="fx-tabs">
        <Segments value={tab ?? (runId ? 'runs' : 'list')} options={TABS} onChange={(t) => onNavigate(t === 'list' ? 'execution' : `execution/${t}`)} ariaLabel="下单执行视图" />
        {meta && <span className="hint">累计试过 {meta.trials} 种执行方案</span>}
      </div>
      {tab === 'list' && <SchemeList meta={meta} algoLabel={algoLabel} onOpen={(id) => onNavigate(`execution/${id}`)} notify={notify} />}
      {tab === 'design' && <Designer meta={meta} notify={notify} onSaved={(id) => onNavigate(`execution/${id}`)} />}
      {tab === 'replay' && <ReplayView meta={meta} notify={notify} />}
      {tab === 'radar' && <Radar meta={meta} notify={notify} />}
      {tab === 'costs' && <Costs notify={notify} />}
      {tab === 'record' && <Recorder notify={notify} onUse={(id) => { try { sessionStorage.setItem(REC_KEY, id) } catch { /* 忽略 */ } onNavigate('execution/design') }} />}
      {tab === 'runs' && <Runs algoLabel={algoLabel} onOpen={(id) => onNavigate(`execution/run/${id}`)} notify={notify} />}
      {runId && <RunDetail key={runId} id={runId} meta={meta} algoLabel={algoLabel} notify={notify} onSaved={(id) => onNavigate(`execution/${id}`)} />}
      {tab === null && !runId && arg && <SchemeDetail key={arg} id={arg} meta={meta} algoLabel={algoLabel} notify={notify} onDeleted={() => onNavigate('execution')} />}
    </>
  )
}

/* ------------------------------------------------------------------ */
function SchemeList({ meta, algoLabel, onOpen, notify }: { meta: ExecMeta | null; algoLabel: (id: string) => string; onOpen: (id: string) => void; notify: PageProps['notify'] }) {
  const [rows, setRows] = useState<ExecScheme[] | null>(null)
  const [status, setStatus] = useState<'' | ExecStatus>('')
  useEffect(() => { execution.list(status || undefined).then((r) => setRows(r.schemes)).catch((e: Error) => notify(e.message, true)) }, [status, notify])
  const statuses = meta?.statuses ?? STATUS_FALLBACK
  return (
    <Panel flush title="执行方案" actions={
      <Segments value={status} onChange={setStatus} ariaLabel="状态"
        options={[{ value: '' as const, label: '全部' }, ...(Object.entries(statuses) as [ExecStatus, string][]).map(([value, label]) => ({ value, label }))]} />
    }>
      {rows === null ? <Loading label="正在读取执行方案" /> : rows.length === 0 ? (
        <EmptyState glyph="⇶" title={status ? '没有这个状态的方案' : '执行方案库还是空的'}>在「仿真设计」里评估一个方案，或在 AI 研究室选择「执行」让 AI 设计。</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="fx-table">
            <thead><tr><th>方案</th><th>状态</th><th className="right">成本 bp</th><th className="right">比 TWAP 省</th><th className="right">相对 VWAP</th><th className="right">完成率</th><th className="right">被动成交</th><th>更新</th></tr></thead>
            <tbody>{rows.map((s) => (
              <tr key={s.id} className="clickable" onClick={() => onOpen(s.id)}>
                <td><strong>{s.name}</strong><span className="ml-sub">{algoLabel(s.config.algo)} · {s.config.horizon} 步 · {s.symbols.join(', ')}</span></td>
                <td><Tag tone={EXEC_STATUS_TONE[s.status]}>{statuses[s.status]}</Tag></td>
                <td className="right num">{bp(s.summary?.is_bps)}</td>
                <td className={`right num ${beatTone(s.summary?.beat_twap_bps)}`}>{bp(s.summary?.beat_twap_bps)}</td>
                <td className="right num muted">{bp(s.summary?.vs_vwap_bps)}</td>
                <td className="right num">{pc(s.summary?.fill_rate, 1)}</td>
                <td className="right num">{pc(s.summary?.maker_ratio)}</td>
                <td className="muted">{when(s.updated_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function SchemeDetail({ id, meta, algoLabel, notify, onDeleted }: { id: string; meta: ExecMeta | null; algoLabel: (id: string) => string; notify: PageProps['notify']; onDeleted: () => void }) {
  const [s, setS] = useState<ExecScheme | null>(null)
  const [report, setReport] = useState<ExecReport | null>(null)
  const [missing, setMissing] = useState(false)
  useEffect(() => {
    execution.get(id).then((r) => { setS(r.scheme); execution.report(id).then((x) => setReport(x.report)).catch(() => undefined) }).catch(() => setMissing(true))
  }, [id])
  if (missing) return <EmptyState glyph="⇶" title="执行方案不存在">可能已被删除。</EmptyState>
  if (!s) return <Loading label="正在打开执行方案" />
  const statuses = meta?.statuses ?? STATUS_FALLBACK
  const spec = JSON.stringify(s.config, null, 2)
  const toLab = (idea: string) => openInLab({ kind: 'execution', idea, code: spec, title: `改进：${s.name}`, securities: s.symbols, attached: `执行方案「${s.name}」的配置` })
  const c = s.config
  return (
    <>
      <Panel title={s.name} actions={<>
        <select value={s.status} aria-label="状态" onChange={async (e) => { try { setS((await execution.update(id, { status: e.target.value as ExecStatus })).scheme) } catch (err) { notify((err as Error).message, true) } }}>
          {(Object.entries(statuses) as [ExecStatus, string][]).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <button className="button small" onClick={() => toLab(`改进执行方案「${s.name}」：在完成率不降的前提下降低成本`)}>交给 AI 改进</button>
        <button className="button small" onClick={() => toLab(`召集执行小组评审「${s.name}」：成本分解、逆向选择、不同市场状态下的表现`)}>召集团队评审</button>
        <button className="button small danger" onClick={async () => { if (await appConfirm(`删除执行方案「${s.name}」？试验记录会保留。`)) { await execution.remove(id); onDeleted() } }}>删除</button>
      </>}>
        <div className="fx-meta">
          <span>{algoLabel(c.algo)} · {meta?.styles.find((x) => x.id === c.style)?.label ?? c.style} · {s.symbols.join(', ')}</span>
          <span>执行期 {c.horizon} 步 · {c.slices} 片 · 单步主动成交 ≤ 上一步成交量的 {pc(c.max_participation)}</span>
          {s.cost_model && <span>成本曲线：{bp(s.cost_model.a_bps)} + {bp(s.cost_model.b_bps)}·√参与率 bp</span>}
          {s.project_id && <span><a href={`#/lab/${s.project_id}`}>研究记录</a></span>}
        </div>
        {s.note && <div className="hint">{s.note}</div>}
      </Panel>
      <Panel title="用于策略" subtitle="set_execution() 让回测的成交价按这个方案的成本曲线计：下单量相对当根成交量越大，滑点越大（手续费仍按回测设置）。">
        <UseInStrategy code={usageSnippet('execution', s.name)} />
      </Panel>
      <Panel title="仿真报告">{report ? <ExecutionFullReport report={report} states={meta?.states ?? []} /> : <Loading label="正在读取报告" />}</Panel>
    </>
  )
}

/* ------------------------------------------------------------------ */
interface Env { symbols: string; source: ExecSource; recording_id: string; steps: number }

function readRecording(): string {
  try { const v = sessionStorage.getItem(REC_KEY) || ''; sessionStorage.removeItem(REC_KEY); return v } catch { return '' }
}

function ConfigForm({ meta, cfg, setCfg, env, setEnv, compact }: {
  meta: ExecMeta; cfg: ExecConfig; setCfg: (c: ExecConfig) => void; env: Env; setEnv: (e: Env) => void; compact?: boolean
}) {
  const set = (p: Partial<ExecConfig>) => setCfg({ ...cfg, ...p })
  const algo = meta.algos.find((a) => a.id === cfg.algo)
  const recs = meta.recordings.filter((r) => r.status !== 'recording' && r.snapshots >= 20)
  return (
    <>
      <div className="config-bar">
        <Field label="算法"><select value={cfg.algo} onChange={(e) => set({ algo: e.target.value })}>{meta.algos.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}</select></Field>
        {['twap', 'vwap'].includes(cfg.algo) && <Field label="挂单风格"><select value={cfg.style} onChange={(e) => set({ style: e.target.value as ExecConfig['style'] })}>{meta.styles.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></Field>}
        <Field label="执行期（步）"><input type="number" min={2} max={600} value={cfg.horizon} onChange={(e) => set({ horizon: Number(e.target.value) })} /></Field>
        {['twap', 'vwap'].includes(cfg.algo) && <Field label="切片数"><input type="number" min={1} max={cfg.horizon} value={cfg.slices} onChange={(e) => set({ slices: Number(e.target.value) })} /></Field>}
        {cfg.algo === 'pov' && <Field label="参与率 %"><input type="number" min={0.5} max={50} step={0.5} value={+(cfg.participation * 100).toFixed(2)} onChange={(e) => set({ participation: Number(e.target.value) / 100 })} /></Field>}
        {cfg.algo === 'almgren_chriss' && <Field label="急迫度 0~1"><input type="number" min={0} max={1} step={0.1} value={cfg.urgency} onChange={(e) => set({ urgency: Number(e.target.value) })} /></Field>}
        {cfg.algo === 'iceberg' && <Field label="显示比例 %"><input type="number" min={1} max={100} value={+(cfg.display * 100).toFixed(1)} onChange={(e) => set({ display: Number(e.target.value) / 100 })} /></Field>}
        {cfg.algo === 'iceberg' && <Field label="追价阈值 bp"><input type="number" min={0} max={200} value={cfg.reprice_bps} onChange={(e) => set({ reprice_bps: Number(e.target.value) })} /></Field>}
        {cfg.algo === 'adaptive' && <Field label="落后多少转主动 %"><input type="number" min={1} max={50} value={+(cfg.adapt.behind * 100).toFixed(1)} onChange={(e) => set({ adapt: { ...cfg.adapt, behind: Number(e.target.value) / 100 } })} /></Field>}
        <Field label="母单方向"><select value={cfg.order.side} onChange={(e) => set({ order: { ...cfg.order, side: e.target.value as ExecConfig['order']['side'] } })}><option value="both">买卖交替</option><option value="buy">买入</option><option value="sell">卖出</option></select></Field>
        <Field label="母单 / 期间成交量 %"><input type="number" min={0.1} max={200} step={0.5} value={+(cfg.order.size_pct * 100).toFixed(2)} onChange={(e) => set({ order: { ...cfg.order, size_pct: Number(e.target.value) / 100, notional: null } })} /></Field>
        <Field label="标的（逗号分隔）"><input value={env.symbols} onChange={(e) => setEnv({ ...env, symbols: e.target.value })} /></Field>
        <Field label="盘口来源">
          <select value={env.source} onChange={(e) => setEnv({ ...env, source: e.target.value as ExecSource, recording_id: e.target.value === 'recording' ? env.recording_id || recs[0]?.id || '' : '' })}>
            <option value="auto">自动（真实 1 分钟 K 线推算）</option><option value="kline">只用真实 K 线</option><option value="synthetic">合成盘口</option>
            <option value="recording" disabled={!recs.length}>录制的盘口{recs.length ? '' : '（还没有）'}</option>
          </select>
        </Field>
        {env.source === 'recording' && <Field label="录制"><select value={env.recording_id} onChange={(e) => setEnv({ ...env, recording_id: e.target.value })}>{recs.map((r) => <option key={r.id} value={r.id}>{r.symbol} · {when(r.created_at)} · {r.snapshots} 个快照</option>)}</select></Field>}
        {!compact && env.source !== 'recording' && <Field label="K 线数"><select value={env.steps} onChange={(e) => setEnv({ ...env, steps: Number(e.target.value) })}>{[1500, 3000, 6000].map((n) => <option key={n} value={n}>{n}</option>)}</select></Field>}
      </div>
      {algo && <div className="hint">{algo.hint}</div>}
      {!compact && (
        <div className="pf-groups">
          <fieldset>
            <legend>成本参数</legend>
            <Field label="挂单手续费 bp"><input type="number" min={-5} max={50} step={0.5} value={cfg.fees.maker_bps} onChange={(e) => set({ fees: { ...cfg.fees, maker_bps: Number(e.target.value) } })} /></Field>
            <Field label="吃单手续费 bp"><input type="number" min={0} max={50} step={0.5} value={cfg.fees.taker_bps} onChange={(e) => set({ fees: { ...cfg.fees, taker_bps: Number(e.target.value) } })} /></Field>
          </fieldset>
          <fieldset>
            <legend>冲击模型</legend>
            <Field label="瞬时冲击恢复（步）"><input type="number" min={0.2} max={200} step={0.5} value={cfg.impact.resilience} onChange={(e) => set({ impact: { ...cfg.impact, resilience: Number(e.target.value) } })} /></Field>
            <Field label="永久冲击占比 %"><input type="number" min={0} max={100} value={+(cfg.impact.permanent * 100).toFixed(0)} onChange={(e) => set({ impact: { ...cfg.impact, permanent: Number(e.target.value) / 100 } })} /></Field>
          </fieldset>
          <fieldset>
            <legend>执行规则</legend>
            <Field label="单步主动成交上限 %"><input type="number" min={1} max={100} value={+(cfg.max_participation * 100).toFixed(0)} onChange={(e) => set({ max_participation: Number(e.target.value) / 100 })} /></Field>
            <Field label="市场状态识别"><select value={cfg.state_model} onChange={(e) => set({ state_model: e.target.value as ExecConfig['state_model'] })}><option value="rules">阈值规则</option><option value="hmm">隐马尔可夫模型</option></select></Field>
            <label className="pf-check"><input type="checkbox" checked={cfg.finish} onChange={(e) => set({ finish: e.target.checked })} />到期没做完的部分市价补齐</label>
            <Field label="片段数"><input type="number" min={4} max={200} value={cfg.episodes} onChange={(e) => set({ episodes: Number(e.target.value) })} /></Field>
          </fieldset>
        </div>
      )}
    </>
  )
}

function useConfig(meta: ExecMeta | null) {
  const [cfg, setCfg] = useState<ExecConfig | null>(null)
  const [env, setEnv] = useState<Env>(() => { const rec = readRecording(); return { symbols: 'BTC/USDT, ETH/USDT', source: rec ? 'recording' : 'auto', recording_id: rec, steps: 3000 } })
  useEffect(() => { if (meta && !cfg) setCfg(JSON.parse(JSON.stringify(meta.defaults))) }, [meta, cfg])
  const params = () => ({
    config: cfg!, symbols: env.symbols.split(/[,，\s]+/).map((x) => x.trim().toUpperCase()).filter(Boolean),
    source: env.source, recording_id: env.source === 'recording' ? env.recording_id : null, steps: env.steps,
  })
  return { cfg, setCfg, env, setEnv, params }
}

function Designer({ meta, notify, onSaved }: { meta: ExecMeta | null; notify: PageProps['notify']; onSaved: (id: string) => void }) {
  const { cfg, setCfg, env, setEnv, params } = useConfig(meta)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ run: ExecRun; report: ExecReport } | null>(null)
  const [name, setName] = useState('')
  const [showJson, setShowJson] = useState(false)
  if (!meta || !cfg) return <Loading />
  const run = async () => {
    setBusy(true)
    try { setResult(await execution.evaluate(params())) } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const save = async (status: ExecStatus) => {
    if (!result) return
    try { const r = await execution.save({ run_id: result.run.id, name: name || undefined, status }); notify(`已存入执行方案库「${r.scheme.name}」`); onSaved(r.scheme.id) } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <>
      <Panel title="设计一个执行方案" subtitle="在同一批执行片段（买卖交替）上跑方案、TWAP 和市价；算法只看得到当时的盘口和已发生的成交。成本相对到达中间价。">
        <ConfigForm meta={meta} cfg={cfg} setCfg={setCfg} env={env} setEnv={setEnv} />
        <div className="ml-run">
          <button className="button primary" disabled={busy} onClick={run}>{busy ? <><span className="spinner" /> 仿真中</> : '⇶ 仿真'}</button>
          <button className="button small" onClick={() => setShowJson(!showJson)}>{showJson ? '收起 JSON' : '查看 JSON'}</button>
        </div>
        {showJson && <pre className="mr-code" style={{ marginTop: 10 }}>{JSON.stringify(cfg, null, 2)}</pre>}
      </Panel>
      {result && (
        <Panel title="仿真结果" actions={<>
          <input className="fx-search" placeholder="方案名称" value={name} onChange={(e) => setName(e.target.value)} aria-label="方案名称" />
          <button className="button small" onClick={() => save('candidate')}>存为候选</button>
          <button className="button small primary" disabled={!result.report.admitted} title={result.report.admitted ? '' : '准入检查没有全部通过'} onClick={() => save('active')}>启用</button>
        </>}>
          <ExecutionFullReport report={result.report} states={meta.states} />
        </Panel>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
function ReplayView({ meta, notify }: { meta: ExecMeta | null; notify: PageProps['notify'] }) {
  const { cfg, setCfg, env, setEnv, params } = useConfig(meta)
  const [episode, setEpisode] = useState(-1)
  const [side, setSide] = useState<'buy' | 'sell'>('buy')
  const [busy, setBusy] = useState(false)
  const [data, setData] = useState<Replay | null>(null)
  const [cursor, setCursor] = useState<number | null>(null)
  const timer = useRef<number | null>(null)
  useEffect(() => () => { if (timer.current) window.clearInterval(timer.current) }, [])
  if (!meta || !cfg) return <Loading />
  const load = async (ep = episode) => {
    setBusy(true)
    stop()
    try { const r = await execution.replay({ ...params(), episode: ep, side }); setData(r); setEpisode(r.episode); setCursor(null) } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const stop = () => { if (timer.current) { window.clearInterval(timer.current); timer.current = null } }
  const play = () => {
    if (!data) return
    stop()
    setCursor(0)
    timer.current = window.setInterval(() => {
      setCursor((c) => {
        const n = (c ?? 0) + 1
        if (n >= data.timeline.length) { stop(); return null }
        return n
      })
    }, 120)
  }
  const visible = data && cursor !== null ? data.timeline.slice(0, cursor + 1) : data?.timeline ?? []
  const fills = data ? data.fills.filter((f) => cursor === null || f.j <= cursor) : []
  const alerts = data ? data.alerts.filter((a) => cursor === null || a.k <= cursor) : []
  const last = visible[visible.length - 1]
  const r = data?.result
  return (
    <>
      <Panel title="回放一个执行片段" subtitle="逐步看算法在做什么：计划与实际进度、挂单价、成交、市场状态切换与提示。点「播放」模拟实时监控。">
        <ConfigForm meta={meta} cfg={cfg} setCfg={setCfg} env={env} setEnv={setEnv} compact />
        <div className="ml-run">
          <Field label="方向"><select value={side} onChange={(e) => setSide(e.target.value as 'buy' | 'sell')}><option value="buy">买入</option><option value="sell">卖出</option></select></Field>
          <button className="button primary" disabled={busy} onClick={() => load(-1)}>{busy ? <><span className="spinner" /> 计算中</> : '回放'}</button>
          {data && <>
            <button className="button small" disabled={busy || episode <= 0} onClick={() => load(episode - 1)}>上一个片段</button>
            <span className="hint">第 {episode + 1} / {data.episodes} 个</span>
            <button className="button small" disabled={busy || episode >= data.episodes - 1} onClick={() => load(episode + 1)}>下一个片段</button>
            <button className="button small" onClick={timer.current ? stop : play}>{timer.current ? '暂停' : '▶ 播放'}</button>
          </>}
        </div>
      </Panel>
      {data && r && (
        <Panel title={`${data.symbol} · ${side === 'buy' ? '买入' : '卖出'} · ${SOURCE_LABEL[data.source] ?? data.source}`}
          subtitle={`盘口价差约 ${bp(data.tape.spread_bps)} bp · 前五档深度约 ${money(data.tape.depth5_usd)} USDT · 每步成交约 ${money(data.tape.volume_usd_per_step)} USDT`}>
          <div className="ex-monitor">
            <div><span>进度</span><b className="num">{last ? `${(last.filled * 100).toFixed(0)}% / 计划 ${(last.target * 100).toFixed(0)}%` : '—'}</b></div>
            <div><span>市场状态</span><b><i className="ex-dot" style={{ background: STATE_COLOR[last?.state ?? 0] }} />{last ? data.states[last.state] : '—'}</b></div>
            <div><span>成本 bp</span><b className="num">{cursor === null ? bp(r.is_bps) : '播放中'}</b></div>
            <div><span>同片段 TWAP</span><b className="num">{bp(data.twap_is_bps)}</b></div>
            <div><span>被动成交</span><b className="num">{pc(r.maker_ratio)}</b></div>
          </div>
          <ReplayChart timeline={visible} fills={fills} alerts={alerts} context={data.context} offset={data.offset} states={data.states} height={320} />
          <div className="fr-grid">
            <section><h4>提示</h4><AlertList alerts={alerts} /></section>
            {cursor === null && <section><h4>成本分解（bp）</h4><CostBreakdown s={r} /></section>}
          </div>
          {data.tape.warnings.length > 0 && <ul className="fr-warn">{data.tape.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
        </Panel>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return null
  const lo = Math.min(...values)
  const hi = Math.max(...values)
  const d = values.map((v, i) => `${i ? 'L' : 'M'}${((i / (values.length - 1)) * 100).toFixed(1)},${(22 - ((v - lo) / (hi - lo || 1)) * 20).toFixed(1)}`).join(' ')
  const up = values[values.length - 1] >= values[0]
  return <svg className="ex-spark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden><path d={d} fill="none" style={{ stroke: up ? 'var(--up)' : 'var(--down)' }} strokeWidth={1.2} vectorEffect="non-scaling-stroke" /></svg>
}

function Radar({ meta, notify }: { meta: ExecMeta | null; notify: PageProps['notify'] }) {
  const [universe, setUniverse] = useState('major10')
  const [tf, setTf] = useState('5m')
  const [source, setSource] = useState('auto')
  const [auto, setAuto] = useState(false)
  const [data, setData] = useState<{ rows: RadarRow[]; synthetic: boolean; alerts: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [at, setAt] = useState<number | null>(null)
  const load = async () => {
    setBusy(true)
    try { setData(await execution.radar({ universe, timeframe: tf, source })); setAt(Date.now()) } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load() }, [universe, tf, source])
  useEffect(() => {
    if (!auto) return
    const t = window.setInterval(load, 60_000)
    return () => window.clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auto, universe, tf, source])
  const all = (data?.rows ?? []).flatMap((r) => (r.alerts ?? []).map((a) => ({ ...a, symbol: r.symbol })))
  return (
    <>
      <Panel title="市场雷达" subtitle="看一组标的的波动、量能、估计价差与市场状态；异常会列在上面。价差由 K 线高低价估计。" actions={<>
        <select value={universe} onChange={(e) => setUniverse(e.target.value)} aria-label="标的池">{(meta?.universes ?? [{ id: 'major10', name: '主流 10', symbols: [] }]).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}</select>
        <select value={tf} onChange={(e) => setTf(e.target.value)} aria-label="周期">{['1m', '5m', '15m', '1h'].map((t) => <option key={t}>{t}</option>)}</select>
        <select value={source} onChange={(e) => setSource(e.target.value)} aria-label="数据"><option value="auto">自动</option><option value="exchange">只用真实</option><option value="synthetic">合成</option></select>
        <label className="pf-check"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />每分钟刷新</label>
        <button className="button small" disabled={busy} onClick={load}>{busy ? '刷新中' : '刷新'}</button>
      </>}>
        {!data ? <Loading label="正在扫描" /> : (
          <>
            {all.length > 0 ? (
              <ul className="ex-alerts">{all.slice(0, 12).map((a, i) => <li key={i} className={a.level}><b>{a.symbol.replace('/USDT', '')}</b>{a.text}</li>)}</ul>
            ) : <div className="hint">暂时没有异常。</div>}
            <div className="hint">{at ? `更新于 ${new Date(at).toLocaleTimeString()}` : ''}{data.synthetic ? ' · 含合成行情' : ''}</div>
          </>
        )}
      </Panel>
      {data && (
        <Panel flush title="各标的">
          <div className="table-wrap">
            <table className="fx-table">
              <thead><tr><th>标的</th><th className="right">价格</th><th className="right">1 小时</th><th className="right">24 小时</th><th>走势</th><th className="right">波动比</th><th className="right">量能 σ</th><th className="right">估计价差 bp</th><th>状态</th></tr></thead>
              <tbody>{data.rows.map((r) => r.error ? (
                <tr key={r.symbol}><td>{r.symbol}</td><td colSpan={8} className="muted">{r.error}</td></tr>
              ) : (
                <tr key={r.symbol}>
                  <td><strong>{r.symbol.replace('/USDT', '')}</strong>{(r.alerts ?? []).length > 0 && <span className="ml-sub">{r.alerts!.map((a) => a.text).join('；')}</span>}</td>
                  <td className="right num">{r.price! >= 1 ? r.price!.toFixed(2) : r.price!.toPrecision(4)}</td>
                  <td className={`right num ${(r.ret_1h ?? 0) >= 0 ? 'positive' : 'negative'}`}>{pc(r.ret_1h, 2)}</td>
                  <td className={`right num ${(r.ret_24h ?? 0) >= 0 ? 'positive' : 'negative'}`}>{pc(r.ret_24h, 1)}</td>
                  <td><Spark values={r.spark ?? []} /></td>
                  <td className={`right num ${(r.vol_ratio ?? 0) > 2 ? 'negative' : ''}`}>{(r.vol_ratio ?? 0).toFixed(2)}</td>
                  <td className="right num">{(r.volume_z ?? 0).toFixed(1)}</td>
                  <td className="right num muted">{bp(r.spread_bps, 1)}</td>
                  <td><i className="ex-dot" style={{ background: STATE_COLOR[(meta?.states ?? []).indexOf(r.state ?? '')] ?? 'var(--faint)' }} />{r.state}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </Panel>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
function Costs({ notify }: { notify: PageProps['notify'] }) {
  const [models, setModels] = useState<{ models: CostModel[]; active: CostModel } | null>(null)
  const [imports, setImports] = useState<ImportItem[]>([])
  const [imp, setImp] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [detail, setDetail] = useState<CostModel | null>(null)
  const reload = () => execution.costs().then(setModels).catch((e: Error) => notify(e.message, true))
  useEffect(() => {
    reload()
    v4.imports().then((r) => { const f = r.items.filter((x) => x.kind === 'fills'); setImports(f); if (f[0]) setImp(f[0].id) }).catch(() => undefined)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const calibrate = async () => {
    setBusy(true)
    try { const r = await execution.calibrate({ import_id: imp, name }); setDetail(r.model); notify(`已校准「${r.model.name}」：Y = ${r.model.Y.toFixed(2)}`); reload() } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  return (
    <>
      <div className="two-columns even">
        <Panel title="用真实成交校准成本" subtitle="拿已导入的成交记录，对比成交所在分钟的开盘价算滑点，拟合平方根冲击模型：滑点 ≈ a + Y·σ·√(成交额 / 日均成交额)。">
          {imports.length === 0 ? (
            <EmptyState glyph="⇄" title="还没有导入成交记录">在「实盘对照 → 导入」里上传交易所导出的成交 CSV，再回来校准。</EmptyState>
          ) : (
            <div className="ml-run">
              <Field label="成交记录"><select value={imp} onChange={(e) => setImp(e.target.value)}>{imports.map((i) => <option key={i.id} value={i.id}>{i.filename} · {i.rows} 行</option>)}</select></Field>
              <Field label="名称"><input value={name} placeholder="例如 我的 Binance 成交" onChange={(e) => setName(e.target.value)} /></Field>
              <button className="button primary" disabled={busy || !imp} onClick={calibrate}>{busy ? <><span className="spinner" /> 校准中</> : '校准'}</button>
            </div>
          )}
          {detail && <CalibrationView m={detail} />}
        </Panel>
        <Panel flush title="成本模型" subtitle="启用的模型用于容量估计。没有校准时用文献经验值（a = 2bp，Y = 0.7）。">
          {!models ? <Loading /> : (
            <div className="table-wrap">
              <table className="fx-table">
                <thead><tr><th>模型</th><th className="right">a bp</th><th className="right">Y</th><th className="right">R²</th><th className="right">成交</th><th /></tr></thead>
                <tbody>
                  {models.models.length === 0 && <tr><td>{models.active.name}</td><td className="right num">{bp(models.active.a_bps)}</td><td className="right num">{models.active.Y.toFixed(2)}</td><td className="right muted">—</td><td className="right muted">—</td><td><Tag tone="ok">使用中</Tag></td></tr>}
                  {models.models.map((m) => (
                    <tr key={m.id ?? m.name}>
                      <td><button className="link" onClick={async () => setDetail((await execution.cost(m.id!)).model)}>{m.name}</button><span className="ml-sub">{(m.symbols ?? []).join(', ')} · {when(m.created_at)}</span></td>
                      <td className="right num">{bp(m.a_bps)}</td><td className="right num">{m.Y.toFixed(2)}</td><td className="right num">{(m.r2 ?? 0).toFixed(2)}</td><td className="right num">{m.n}</td>
                      <td className="right">{m.active ? <Tag tone="ok">使用中</Tag> : <>
                        <button className="button small" onClick={async () => { await execution.activateCost(m.id!); reload() }}>启用</button>
                        <button className="button small danger" onClick={async () => { await execution.deleteCost(m.id!); reload() }}>删除</button></>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
      <CapacityView active={models?.active ?? null} notify={notify} />
    </>
  )
}

function CalibrationView({ m }: { m: CostModel }) {
  const pts = m.points ?? []
  const W = 420
  const H = 220
  const P = { l: 46, r: 12, t: 12, b: 34 }
  if (!pts.length) return null
  const xmax = Math.max(...pts.map((p) => p.x)) * 1.05 || 1
  const ys = pts.map((p) => p.slip_bps)
  const lo = Math.min(...ys, 0)
  const hi = Math.max(...ys, 1)
  const X = (v: number) => P.l + (v / xmax) * (W - P.l - P.r)
  const Y = (v: number) => P.t + (1 - (v - lo) / (hi - lo || 1)) * (H - P.t - P.b)
  return (
    <figure className="pf-frontier" style={{ marginTop: 12 }}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="滑点散点">
        {[lo, (lo + hi) / 2, hi].map((t) => <g key={t}><line className="chart-gridline" x1={P.l} x2={W - P.r} y1={Y(t)} y2={Y(t)} /><text className="chart-label" x={P.l - 6} y={Y(t) + 4} textAnchor="end">{t.toFixed(1)}</text></g>)}
        <text className="chart-label" x={(W + P.l) / 2} y={H - 4} textAnchor="middle">σ·√(成交额/日均成交额)（bp）</text>
        {pts.slice(0, 800).map((p, i) => <circle key={i} cx={X(p.x)} cy={Y(p.slip_bps)} r={2.2} style={{ fill: 'var(--accent)' }} opacity={0.45} />)}
        <path d={`M${X(0)},${Y(m.a_bps)} L${X(xmax)},${Y(m.a_bps + m.Y * xmax)}`} style={{ stroke: 'var(--amber)' }} strokeWidth={1.8} />
      </svg>
      <div className="hint">{m.name}：a = {bp(m.a_bps)} bp，Y = {m.Y.toFixed(2)}{m.Y_se ? ` ± ${m.Y_se.toFixed(2)}` : ''}，R² {(m.r2 ?? 0).toFixed(2)}，用了 {m.used ?? m.n} 笔成交。文献里 Y 通常在 0.5~1 之间。</div>
    </figure>
  )
}

function CapacityView({ active, notify }: { active: CostModel | null; notify: PageProps['notify'] }) {
  const [form, setForm] = useState({ symbols: 'BTC/USDT, ETH/USDT, SOL/USDT', turnover: 30, alpha: 30, rebalances: 365, fee_bps: 5 })
  const [res, setRes] = useState<Capacity | null>(null)
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    try {
      setRes(await execution.capacity({ symbols: form.symbols.split(/[,，\s]+/).filter(Boolean).map((s) => s.toUpperCase()), turnover: form.turnover, alpha: form.alpha / 100, rebalances: form.rebalances, fee_bps: form.fee_bps }))
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const chart = useMemo(() => {
    if (!res) return null
    const W = 720
    const H = 240
    const P = { l: 54, r: 12, t: 12, b: 34 }
    const lx = (v: number) => Math.log10(v)
    const x0 = lx(res.curve[0].aum)
    const x1 = lx(res.curve[res.curve.length - 1].aum)
    const ymax = Math.max(res.alpha * 1.3, ...res.curve.map((c) => Math.min(c.annual_cost, res.alpha * 1.5)))
    const X = (v: number) => P.l + ((lx(v) - x0) / (x1 - x0)) * (W - P.l - P.r)
    const Y = (v: number) => P.t + (1 - Math.min(v, ymax) / ymax) * (H - P.t - P.b)
    return { W, H, P, X, Y, ymax }
  }, [res])
  return (
    <Panel title="容量估计" subtitle={`资金规模越大，每次调仓的冲击越大。用当前成本模型（${active?.name ?? '默认'}）估计成本吃掉一半 / 全部超额收益时的规模。`}>
      <div className="config-bar">
        <Field label="标的（等权）"><input value={form.symbols} onChange={(e) => setForm({ ...form, symbols: e.target.value })} /></Field>
        <Field label="年换手（倍）"><input type="number" min={0.1} max={1000} value={form.turnover} onChange={(e) => setForm({ ...form, turnover: Number(e.target.value) })} /></Field>
        <Field label="扣成本前年化收益 %"><input type="number" min={1} max={1000} value={form.alpha} onChange={(e) => setForm({ ...form, alpha: Number(e.target.value) })} /></Field>
        <Field label="每年调仓次数"><input type="number" min={1} max={100000} value={form.rebalances} onChange={(e) => setForm({ ...form, rebalances: Number(e.target.value) })} /></Field>
        <Field label="手续费 bp"><input type="number" min={0} max={100} value={form.fee_bps} onChange={(e) => setForm({ ...form, fee_bps: Number(e.target.value) })} /></Field>
      </div>
      <div className="ml-run"><button className="button primary" disabled={busy} onClick={run}>{busy ? <><span className="spinner" /> 计算中</> : '估计容量'}</button></div>
      {res && chart && (
        <>
          <div className="ex-monitor">
            <div><span>成本吃掉一半收益</span><b className="num">{money(res.half_alpha_aum)} USDT</b></div>
            <div><span>成本吃掉全部收益</span><b className="num">{money(res.zero_alpha_aum)} USDT</b></div>
            <div><span>小资金时的年成本</span><b className="num">{pc(res.fixed_cost, 1)}</b></div>
          </div>
          <figure className="ex-replay">
            <svg viewBox={`0 0 ${chart.W} ${chart.H}`} role="img" aria-label="资金规模与成本">
              {[0, 0.5, 1].map((f) => { const v = chart.ymax * f; return <g key={f}><line className="chart-gridline" x1={chart.P.l} x2={chart.W - chart.P.r} y1={chart.Y(v)} y2={chart.Y(v)} /><text className="chart-label" x={chart.P.l - 6} y={chart.Y(v) + 4} textAnchor="end">{pc(v)}</text></g> })}
              {[1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10].map((v) => <text key={v} className="chart-label" x={chart.X(v)} y={chart.H - chart.P.b + 14} textAnchor="middle">{money(v)}</text>)}
              <text className="chart-label" x={(chart.W + chart.P.l) / 2} y={chart.H - 4} textAnchor="middle">资金规模（USDT，对数轴）</text>
              <line x1={chart.P.l} x2={chart.W - chart.P.r} y1={chart.Y(res.alpha)} y2={chart.Y(res.alpha)} style={{ stroke: 'var(--up)' }} strokeDasharray="4 3" />
              <path d={res.curve.map((c, i) => `${i ? 'L' : 'M'}${chart.X(c.aum).toFixed(1)},${chart.Y(c.annual_cost).toFixed(1)}`).join(' ')} fill="none" style={{ stroke: 'var(--accent)' }} strokeWidth={1.8} />
              {res.half_alpha_aum && <circle cx={chart.X(res.half_alpha_aum)} cy={chart.Y(res.alpha / 2)} r={4} style={{ fill: 'var(--amber)' }} />}
            </svg>
            <figcaption className="legend"><span><i style={{ background: 'var(--accent)' }} />年化交易成本</span><span><i style={{ background: 'var(--up)' }} />扣成本前收益</span><span><i style={{ background: 'var(--amber)' }} />成本 = 一半收益</span></figcaption>
          </figure>
          <div className="hint">日均成交额：{res.assets.map((a) => `${a.symbol.replace('/USDT', '')} ${money(a.adv_usd)}`).join('，')}{res.synthetic ? '（部分为经验值或合成数据）' : ''}。这是粗估：没有考虑多个策略同时交易、流动性随行情变化。</div>
        </>
      )}
    </Panel>
  )
}

/* ------------------------------------------------------------------ */
function Recorder({ notify, onUse }: { notify: PageProps['notify']; onUse: (id: string) => void }) {
  const [rows, setRows] = useState<Recording[] | null>(null)
  const [form, setForm] = useState({ exchange: 'binance', symbol: 'BTC/USDT', minutes: 30, interval: 2 })
  const reload = () => execution.recordings().then((r) => setRows(r.recordings)).catch((e: Error) => notify(e.message, true))
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { reload() }, [])
  useEffect(() => {
    if (!rows?.some((r) => r.status === 'recording')) return
    const t = window.setInterval(reload, 5000)
    return () => window.clearInterval(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows])
  const start = async () => {
    try { await execution.record({ exchange: form.exchange, symbol: form.symbol.toUpperCase(), seconds: form.minutes * 60, interval: form.interval }); notify('开始录制'); reload() } catch (e) { notify((e as Error).message, true) }
  }
  const STATUS: Record<string, [string, string]> = { recording: ['录制中', 'warn'], done: ['完成', 'ok'], stopped: ['已停止', ''], error: ['出错', 'bad'] }
  return (
    <>
      <Panel title="录制真实盘口" subtitle="按固定间隔抓交易所的公开盘口快照与逐笔成交（只读行情，不需要密钥）。录完可以在仿真里回放，被动成交与排队的结论比 K 线推算可靠得多。">
        <div className="config-bar">
          <Field label="交易所"><select value={form.exchange} onChange={(e) => setForm({ ...form, exchange: e.target.value })}><option value="binance">Binance</option><option value="okx">OKX</option></select></Field>
          <Field label="标的"><input value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value })} /></Field>
          <Field label="时长（分钟）"><input type="number" min={1} max={240} value={form.minutes} onChange={(e) => setForm({ ...form, minutes: Number(e.target.value) })} /></Field>
          <Field label="间隔（秒）"><input type="number" min={0.5} max={60} step={0.5} value={form.interval} onChange={(e) => setForm({ ...form, interval: Number(e.target.value) })} /></Field>
        </div>
        <div className="ml-run"><button className="button primary" onClick={start}>● 开始录制</button>
          <span className="hint">录制在服务器后台进行，关掉页面不影响；服务重启会中断。</span></div>
      </Panel>
      <Panel flush title="录制记录">
        {rows === null ? <Loading /> : rows.length === 0 ? <EmptyState glyph="●" title="还没有录制" /> : (
          <div className="table-wrap">
            <table className="fx-table">
              <thead><tr><th>标的</th><th>状态</th><th className="right">快照</th><th className="right">逐笔</th><th>间隔</th><th>开始</th><th /></tr></thead>
              <tbody>{rows.map((r) => (
                <tr key={r.id}>
                  <td><strong>{r.symbol}</strong><span className="ml-sub">{r.exchange}{r.error ? ` · ${r.error}` : ''}</span></td>
                  <td><Tag tone={STATUS[r.status]?.[1]}>{STATUS[r.status]?.[0] ?? r.status}</Tag></td>
                  <td className="right num">{r.snapshots}</td><td className="right num">{r.trades}</td>
                  <td className="muted">{r.interval} 秒 · {Math.round(r.seconds / 60)} 分钟</td>
                  <td className="muted">{when(r.created_at)}</td>
                  <td className="right">
                    {r.status === 'recording' && <button className="button small" onClick={async () => { await execution.stopRecording(r.id); reload() }}>停止</button>}
                    {r.status !== 'recording' && r.snapshots >= 20 && <button className="button small" onClick={() => onUse(r.id)}>用于仿真</button>}
                    <button className="button small danger" onClick={async () => { if (await appConfirm('删除这段录制？')) { await execution.deleteRecording(r.id); reload() } }}>删除</button>
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  )
}

/* ------------------------------------------------------------------ */
function Runs({ algoLabel, onOpen, notify }: { algoLabel: (id: string) => string; onOpen: (id: string) => void; notify: PageProps['notify'] }) {
  const [data, setData] = useState<{ runs: ExecRun[]; trials: number } | null>(null)
  useEffect(() => { execution.runs().then(setData).catch((e: Error) => notify(e.message, true)) }, [notify])
  if (!data) return <Loading label="正在读取试验记录" />
  return (
    <Panel flush title={`试验记录（${data.runs.length}）`} subtitle="每次仿真一笔，包括没通过的。">
      {data.runs.length === 0 ? <EmptyState glyph="☰" title="还没有试验" /> : (
        <div className="table-wrap">
          <table className="fx-table">
            <thead><tr><th>方案</th><th className="right">成本 bp</th><th className="right">比 TWAP 省</th><th className="right">完成率</th><th>盘口</th><th>检查</th><th>来源</th><th>时间</th></tr></thead>
            <tbody>{data.runs.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => onOpen(r.id)}>
                <td><strong>{algoLabel(r.algo)}</strong><span className="ml-sub">{r.horizon} 步 · {r.symbols.join(', ')}{r.note ? ` · ${r.note}` : ''}</span></td>
                <td className="right num">{bp(r.summary.is_bps)}</td>
                <td className={`right num ${beatTone(r.summary.beat_twap_bps)}`}>{bp(r.summary.beat_twap_bps)}</td>
                <td className="right num">{pc(r.summary.fill_rate, 1)}</td>
                <td className="muted">{(r.tape_sources ?? []).map((s) => SOURCE_LABEL[s] ?? s).join('、')}</td>
                <td>{r.admitted ? <Tag tone="ok">可启用</Tag> : <Tag>未通过</Tag>}</td>
                <td className="muted">{r.source === 'lab' ? 'AI 研究室' : '仿真设计'}</td>
                <td className="muted">{when(r.created_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function RunDetail({ id, meta, algoLabel, notify, onSaved }: { id: string; meta: ExecMeta | null; algoLabel: (id: string) => string; notify: PageProps['notify']; onSaved: (id: string) => void }) {
  const [data, setData] = useState<{ run: ExecRun; report: ExecReport } | null>(null)
  const [missing, setMissing] = useState(false)
  const [name, setName] = useState('')
  useEffect(() => { execution.run(id).then(setData).catch(() => setMissing(true)) }, [id])
  if (missing) return <EmptyState glyph="☰" title="试验不存在" />
  if (!data) return <Loading label="正在打开试验" />
  const save = async (status: ExecStatus) => {
    try { const r = await execution.save({ run_id: id, name: name || undefined, status }); notify(`已存入执行方案库「${r.scheme.name}」`); onSaved(r.scheme.id) } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <Panel title={algoLabel(data.run.algo)} subtitle={`${when(data.run.created_at)} · ${data.run.horizon} 步 · ${data.run.symbols.join(', ')}`} actions={<>
      <input className="fx-search" placeholder="方案名称" value={name} onChange={(e) => setName(e.target.value)} aria-label="方案名称" />
      <button className="button small" onClick={() => save('candidate')}>存为候选</button>
      <button className="button small primary" disabled={!data.report.admitted} onClick={() => save('active')}>启用</button>
    </>}>
      {data.run.project_id && <div className="hint">来自 <a href={`#/lab/${data.run.project_id}`}>AI 研究室的项目</a></div>}
      <ExecutionFullReport report={data.report} states={meta?.states ?? []} />
    </Panel>
  )
}

