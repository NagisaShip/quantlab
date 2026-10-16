/**
 * 模型库：已保存的模型（模型卡、健康度、在策略里使用）、训练台、实验记录与对比。
 * 路由：#/modellib（模型）· #/modellib/train（训练台）· #/modellib/runs（实验记录）· #/modellib/run/<id> · #/modellib/<id>
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { PythonCodeButton } from '../components/PythonCode'
import { factors, type Factor } from '../apiFactors'
import {
  models, usageSnippet,
  type CompareView, type FeatureSpec, type ModelMeta, type ModelRecord, type ModelReport, type ModelRun, type ModelStatus, type TrainJob, type TrainParams,
} from '../apiModels'
import { openInLab } from '../components/handoff'
import { f3, fp } from '../components/factorReport'
import { CurveChart, isVol, MODEL_STATUS_TONE, ModelCardView, ModelFullReport, UseInStrategy } from '../components/modelReport'
import { LineChart, Sparkline } from '../components/charts2'
import { EmptyState, Field, Heading, Loading, Panel, Segments, Tag } from '../components/ui'
import { QuantBot, QuantEyes, viewFromSignal } from '../components/QuantBot'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

type Tab = 'list' | 'train' | 'runs'
const TABS: { value: Tab; label: string }[] = [
  { value: 'list', label: '模型' }, { value: 'train', label: '训练台' }, { value: 'runs', label: '实验记录' },
]
const STATUS_FALLBACK: Record<ModelStatus, string> = { candidate: '候选', active: '上线', decaying: '衰减预警', retired: '退役' }
const day = (ms?: number | null) => (ms ? new Date(ms * (ms < 1e11 ? 1000 : 1)).toISOString().slice(0, 10) : '—')
const when = (ms?: number | null) => (ms ? new Date(ms * (ms < 1e11 ? 1000 : 1)).toISOString().slice(5, 16).replace('T', ' ') : '—')

export default function ModelLibPage({ arg, onNavigate, notify }: PageProps & { arg?: string }) {
  const [meta, setMeta] = useState<ModelMeta | null>(null)
  const loadMeta = useCallback(() => models.meta().then(setMeta).catch((e: Error) => notify(e.message, true)), [notify])
  useEffect(() => { loadMeta() }, [loadMeta])
  const runId = arg?.startsWith('run/') ? arg.slice(4) : null
  const tab: Tab | null = !arg ? 'list' : arg === 'train' || arg === 'runs' ? arg : null

  return (
    <>
      <Heading title="预测模型" description="用因子和特征训练预测模型。每次训练都是滚动样本外评估，和线性基线比较，并做泄漏检查；通过检查的模型可以在策略里用 predict() 调用。"
        actions={<button className="button primary" onClick={() => openInLab({ idea: '', kind: 'model' })}>✦ 让 AI 研究一个模型</button>} />
      <div className="fx-tabs">
        <Segments value={tab ?? (runId ? 'runs' : 'list')} options={TABS} onChange={(t) => onNavigate(t === 'list' ? 'modellib' : `modellib/${t}`)} ariaLabel="模型库视图" />
        {meta && <span className="hint">累计 {meta.runs} 种训练配置{meta.backends.torch ? '' : ' · 未安装 PyTorch，序列模型不可用'}{meta.backends.lightgbm ? '' : ' · 未安装 LightGBM，梯度提升树用内置实现'}</span>}
      </div>
      {tab === 'list' && <ModelList meta={meta} onOpen={(id) => onNavigate(`modellib/${id}`)} notify={notify} />}
      {tab === 'train' && <TrainBench meta={meta} notify={notify} onSaved={(id) => { loadMeta(); onNavigate(`modellib/${id}`) }} />}
      {tab === 'runs' && <Runs onOpen={(rid) => onNavigate(`modellib/run/${rid}`)} notify={notify} />}
      {runId && <RunDetail key={runId} id={runId} meta={meta} notify={notify} onSaved={(id) => onNavigate(`modellib/${id}`)} />}
      {tab === null && !runId && arg && <ModelDetail key={arg} id={arg} meta={meta} notify={notify} onDeleted={() => onNavigate('modellib')} />}
    </>
  )
}

/* ------------------------------------------------------------------ */
function ModelList({ meta, onOpen, notify }: { meta: ModelMeta | null; onOpen: (id: string) => void; notify: PageProps['notify'] }) {
  const [rows, setRows] = useState<ModelRecord[] | null>(null)
  const [status, setStatus] = useState<'' | ModelStatus>('')
  useEffect(() => { models.list(status || undefined).then((r) => setRows(r.models)).catch((e: Error) => notify(e.message, true)) }, [status, notify])
  const statuses = meta?.statuses ?? STATUS_FALLBACK
  const label = (id: string) => meta?.learners.find((l) => l.id === id)?.label ?? id
  const target = (id: string) => meta?.labels.find((l) => l.id === id)?.label ?? id
  return (
    <Panel flush title="模型" actions={
      <Segments value={status} onChange={setStatus} ariaLabel="状态"
        options={[{ value: '' as const, label: '全部' }, ...(Object.entries(statuses) as [ModelStatus, string][]).map(([value, l]) => ({ value, label: l }))]} />
    }>
      {rows === null ? <Loading label="正在读取模型库" /> : rows.length === 0 ? (
        <EmptyState glyph="◇" title={status ? '没有这个状态的模型' : '模型库还是空的'}>在「训练台」训练一个模型，或在 AI 研究室选择「模型」让 AI 设计特征与结构。</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="fx-table">
            <thead><tr>
              <th>模型</th><th>状态</th><th className="right">样本外 RankIC</th><th className="right">超出基线</th><th className="right">方向准确率</th>
              <th className="right">多空夏普</th><th>健康度</th><th>训练至</th>
            </tr></thead>
            <tbody>
              {rows.map((m) => {
                const h = (m.health || []).map((x) => x.live_rank_ic ?? 0)
                return (
                  <tr key={m.id} className="clickable" onClick={() => onOpen(m.id)}>
                    <td><strong>{m.name}</strong><span className="ml-sub">{label(m.spec.model)} · {target(m.spec.label)} · {m.spec.features.length} 个特征 · {m.spec.timeframe}</span></td>
                    <td><Tag tone={MODEL_STATUS_TONE[m.status]}>{statuses[m.status]}</Tag>{m.frozen && <Tag tone="muted">已冻结</Tag>}</td>
                    <td className={`right num ${(m.summary?.oos_rank_ic ?? 0) >= 0 ? 'positive' : 'negative'}`}>{f3(m.summary?.oos_rank_ic)}</td>
                    <td className="right num">{f3(m.summary?.beat_baseline)}</td>
                    <td className="right num">{isVol(m.summary) ? '—' : fp(m.summary?.dir_acc, 1)}</td>
                    <td className="right num">{isVol(m.summary) ? '—' : f3(m.summary?.ls_sharpe, 2)}</td>
                    <td>{h.length > 1 ? <Sparkline values={h} width={80} height={20} /> : <span className="muted">未检查</span>}</td>
                    <td className="muted">{day(m.train_end)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

/* ------------------------------------------------------------------ */
function ModelDetail({ id, meta, notify, onDeleted }: { id: string; meta: ModelMeta | null; notify: PageProps['notify']; onDeleted: () => void }) {
  const [m, setM] = useState<ModelRecord | null>(null)
  const [report, setReport] = useState<ModelReport | null>(null)
  const [missing, setMissing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [latest, setLatest] = useState<{ t: number; values: Record<string, number | null> } | null>(null)
  useEffect(() => {
    models.get(id).then((r) => {
      setM(r.model)
      models.report(id).then((x) => setReport(x.report)).catch(() => setReport(null))
    }).catch(() => setMissing(true))
  }, [id])
  if (missing) return <EmptyState glyph="◇" title="模型不存在">可能已被删除。</EmptyState>
  if (!m) return <Loading label="正在打开模型" />
  const statuses = meta?.statuses ?? STATUS_FALLBACK
  const health = m.health || []
  const update = async (p: Parameters<typeof models.update>[1]) => {
    try { setM((await models.update(id, p)).model) } catch (e) { notify((e as Error).message, true) }
  }
  const refresh = async () => {
    setBusy(true)
    try {
      const r = await models.refresh(id)
      setM(r.model); setLatest(r.latest)
      notify(r.flag === 'decaying' ? '训练区间之后的 RankIC 明显变弱，已标为衰减预警' : r.flag === 'drift' ? '特征分布有明显漂移，建议重训' : '健康度检查完成')
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const spec = JSON.stringify({ model: m.spec.model, label: m.spec.label, horizon: m.spec.horizon, features: m.spec.features, params: m.spec.params }, null, 2)
  const toLab = (idea: string) => openInLab({
    kind: 'model', idea, code: spec, title: `改进：${m.name}`, securities: m.spec.symbols, timeframe: m.spec.timeframe,
    attached: `模型「${m.name}」的训练规格`, pins: [{ kind: 'note', title: '模型卡', text: [m.card.purpose, `已知弱点：${m.card.weaknesses.join('；')}`].join('\n') }],
  })
  const lv = latest ?? report?.latest
  return (
    <>
      <Panel title={m.name} actions={<>
        <select value={m.status} aria-label="状态" onChange={(e) => update({ status: e.target.value as ModelStatus })}>
          {(Object.entries(statuses) as [ModelStatus, string][]).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <button className="button small" onClick={() => update({ frozen: !m.frozen })} title="冻结后，再次保存同名模型会另存一个新模型，不会覆盖">{m.frozen ? '取消冻结' : '冻结'}</button>
        <button className="button small" disabled={busy} onClick={refresh}>{busy ? <><span className="spinner" /> 检查中</> : '检查健康度'}</button>
        <button className="button small" onClick={() => toLab(`改进模型「${m.name}」：针对已知弱点调整特征或结构，必须跑赢基线`)}>交给 AI 改进</button>
        <PythonCodeButton kind="model" artifact={m.spec} name={m.name} />
        <button className="button small" onClick={() => toLab(`召集模型小组评审「${m.name}」：检查泄漏、过拟合和上线风险`)}>召集团队评审</button>
        <button className="button small danger" onClick={async () => { if (await appConfirm(`删除模型「${m.name}」？实验记录会保留。`)) { await models.remove(id); onDeleted() } }}>删除</button>
      </>}>
        <div className="fx-meta">
          <span>{m.spec.symbols.length} 个标的 · {m.spec.timeframe} · 预测 {m.spec.horizon} 期 · 训练至 {day(m.train_end)}</span>
          <span>来源：{m.created_by === 'ai' || m.project_id ? 'AI 研究室' : '训练台'}{m.project_id && <> · <a href={`#/lab/${m.project_id}`}>研究记录</a></>} · <a href={`#/modellib/run/${m.run_id}`}>训练实验</a></span>
          {health.length > 1 && <span className="fx-health">训练后 RankIC <Sparkline values={health.map((h) => h.live_rank_ic ?? 0)} width={100} height={22} /></span>}
        </div>
        {health.length > 0 && (() => {
          const h = health[health.length - 1]
          return <div className="hint">最近一次检查（{when(h.t)}）：{h.live_bars > 0 ? `训练区间之后 ${h.live_bars} 根 K 线的 RankIC ${f3(h.live_rank_ic)}，入库时 ${f3(m.summary?.oos_rank_ic)}` : '训练区间之后还没有新数据，暂时只能看特征漂移'}；最大 PSI {f3(h.psi_max)}。</div>
        })()}
        {m.note && <div className="hint">{m.note}</div>}
        <ModelCardView card={m.card} />
      </Panel>
      <div className="two-columns even">
        <Panel title="用于策略" subtitle="在策略里用名字调用；回测时按“滚动重训”生成预测，每段只用当时之前的数据训练。">
          <UseInStrategy code={usageSnippet('predict', m.name)} />
        </Panel>
        <Panel title="最新预测" subtitle={lv ? `截至 ${when(lv.t)} UTC` : undefined}>
          {lv ? (() => {
            const sig = { task: m.summary?.task, label: m.summary?.label }
            const rows = Object.entries(lv.values).sort((a, b) => (b[1] ?? -1e9) - (a[1] ?? -1e9))
            const views = rows.map(([, v]) => viewFromSignal(v, sig))
            const bull = views.filter((x) => x === 'bull').length
            const bear = views.filter((x) => x === 'bear').length
            // 整体观点：多数方向占一半以上才表达，否则中性；波动率模型永远中性
            const overall = bull > rows.length / 2 ? 'bull' : bear > rows.length / 2 ? 'bear' : 'neutral'
            return (
              <>
                <div className="ml-view">
                  <QuantBot size={30} state="idle" view={overall} />
                  {isVol(m.summary)
                    ? <span>这是波动率模型，不表达涨跌方向。</span>
                    : <span>模型观点：<b>{overall === 'bull' ? '看涨' : overall === 'bear' ? '看跌' : '中性'}</b> · {bull} 个看涨、{bear} 个看跌、{rows.length - bull - bear} 个中性{m.summary?.label === 'cs_rank' ? '（截面排名：相对强弱）' : ''}</span>}
                </div>
                <div className="ml-latest">
                  {rows.map(([s, v], i) => (
                    <div key={s}>
                      <QuantEyes size={15} state="idle" view={views[i]} motion="none" className="ml-eyes" />
                      <span>{s}</span>
                      <b className={`num ${(v ?? 0) >= 0 ? 'positive' : 'negative'}`}>{v === null ? '—' : isVol(m.summary) ? fp(v, 2) : v.toFixed(3)}</b>
                    </div>
                  ))}
                </div>
              </>
            )
          })() : <EmptyState glyph="◇" title="还没有预测">点「检查健康度」用最新数据算一次。</EmptyState>}
        </Panel>
      </div>
      <Panel title="训练报告">
        {report ? <ModelFullReport report={report} /> : <Loading label="正在读取训练报告" />}
      </Panel>
    </>
  )
}

/* ------------------------------------------------------------------ */
type FeatRow = FeatureSpec & { on: boolean }

function TrainBench({ meta, notify, onSaved }: { meta: ModelMeta | null; notify: PageProps['notify']; onSaved: (id: string) => void }) {
  const [p, setP] = useState<TrainParams>({ model: 'ridge', label: 'fwd_ret', horizon: 1, universe: 'major10', timeframe: '4h', bars_limit: 1500, source: 'auto', folds: 4, cost_bps: 10, seed: 7 })
  const [feats, setFeats] = useState<FeatRow[]>([])
  const [libFactors, setLibFactors] = useState<Factor[]>([])
  const [custom, setCustom] = useState('')
  const [paramsText, setParamsText] = useState('')
  const [job, setJob] = useState<TrainJob | null>(null)
  const [result, setResult] = useState<{ run: ModelRun; report: ModelReport } | null>(null)
  const [name, setName] = useState('')
  const timer = useRef<number | null>(null)

  useEffect(() => { if (meta && !feats.length) setFeats(meta.default_features.map((f) => ({ ...f, on: true }))) }, [meta, feats.length])
  useEffect(() => { factors.list().then((r) => setLibFactors(r.factors.filter((f) => f.status !== 'retired'))).catch(() => undefined) }, [])
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current) }, [])

  const learner = meta?.learners.find((l) => l.id === p.model)
  const volOnly = learner?.family === 'vol'
  useEffect(() => { if (volOnly && p.label !== 'volatility') setP((x) => ({ ...x, label: 'volatility' })) }, [volOnly, p.label])

  const poll = (id: string) => {
    timer.current = window.setTimeout(async () => {
      try {
        const r = await models.job(id)
        setJob(r.job)
        if (r.job.status === 'running') { poll(id); return }
        if (r.job.status === 'done' && r.job.run_id) setResult(await models.run(r.job.run_id))
        if (r.job.status === 'failed') notify(r.job.error || '训练失败', true)
      } catch (e) { notify((e as Error).message, true) }
    }, 1000)
  }
  const start = async () => {
    let params: Record<string, unknown> = {}
    if (paramsText.trim()) {
      try { params = JSON.parse(paramsText) } catch { notify('超参数需要是 JSON，例如 {"n_estimators": 300}', true); return }
    }
    setResult(null)
    try {
      const features = volOnly && p.model === 'har' ? [] : feats.filter((f) => f.on).map(({ name: n, expression, factor_id }) => (factor_id ? { factor_id } : { name: n, expression }))
      const r = await models.train({ ...p, params, features })
      setJob(r.job)
      poll(r.job.id)
    } catch (e) { notify((e as Error).message, true) }
  }
  const save = async (status: ModelStatus) => {
    if (!result) return
    try {
      const r = await models.save({ run_id: result.run.id, name: name || undefined, status })
      notify(`已存入模型库「${r.model.name}」`)
      onSaved(r.model.id)
    } catch (e) { notify((e as Error).message, true) }
  }
  const addCustom = () => {
    const expr = custom.trim()
    if (!expr) return
    setFeats([...feats, { name: expr.slice(0, 20), expression: expr, on: true }]); setCustom('')
  }
  const running = job?.status === 'running'

  return (
    <>
      <Panel title="训练一个模型" subtitle="扩张窗口滚动训练：每段只用之前的数据训练、在之后的数据上测试，中间留出持有期。每次训练都记入实验记录。">
        <div className="config-bar">
          <Field label="模型">
            <select value={p.model} onChange={(e) => setP({ ...p, model: e.target.value })}>
              {(meta?.learners ?? []).map((l) => <option key={l.id} value={l.id} disabled={!l.available}>{l.label}{l.available ? '' : '（需要 PyTorch）'}</option>)}
            </select>
          </Field>
          <Field label="预测目标">
            <select value={p.label} disabled={volOnly} onChange={(e) => setP({ ...p, label: e.target.value })}>
              {(meta?.labels ?? []).map((l) => <option key={l.id} value={l.id} disabled={volOnly && l.id !== 'volatility'}>{l.label}</option>)}
            </select>
          </Field>
          <Field label="预测期">
            <select value={p.horizon} onChange={(e) => setP({ ...p, horizon: Number(e.target.value) })}>{[1, 2, 3, 4, 6, 12, 24].map((n) => <option key={n} value={n}>{n} 根</option>)}</select>
          </Field>
          <Field label="标的池">
            <select value={p.universe} onChange={(e) => setP({ ...p, universe: e.target.value })}>
              {(meta?.universes ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}（{u.symbols.length}）</option>)}
            </select>
          </Field>
          <Field label="周期">
            <select value={p.timeframe} onChange={(e) => setP({ ...p, timeframe: e.target.value })}>{['1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}</select>
          </Field>
          <Field label="K 线数">
            <select value={p.bars_limit} onChange={(e) => setP({ ...p, bars_limit: Number(e.target.value) })}>{[800, 1500, 3000, 6000].map((n) => <option key={n} value={n}>{n}</option>)}</select>
          </Field>
          <Field label="滚动段数">
            <select value={p.folds} onChange={(e) => setP({ ...p, folds: Number(e.target.value) })}>{[3, 4, 5, 6].map((n) => <option key={n} value={n}>{n}</option>)}</select>
          </Field>
          <Field label="数据">
            <select value={p.source} onChange={(e) => setP({ ...p, source: e.target.value as TrainParams['source'] })}>
              <option value="auto">真实行情（缓存优先）</option><option value="exchange">只用真实数据</option>
            </select>
          </Field>
        </div>
        {learner && <div className="hint">{learner.hint}{meta?.labels.find((l) => l.id === p.label) ? ` · 目标：${meta.labels.find((l) => l.id === p.label)!.hint}` : ''}</div>}

        <h4 className="ml-h4">特征{p.model === 'har' && <span className="hint">（HAR 固定使用 1 / 6 / 24 根的已实现波动，不用下面的特征）</span>}</h4>
        <div className="ml-feats">
          {feats.map((f, i) => (
            <label key={`${f.name}-${i}`} className={`ml-feat${f.on ? ' on' : ''}`} title={f.expression}>
              <input type="checkbox" checked={f.on} onChange={() => setFeats(feats.map((x, j) => (j === i ? { ...x, on: !x.on } : x)))} />
              <span>{f.name}</span>{f.factor_id && <em>因子库</em>}
            </label>
          ))}
        </div>
        <div className="ml-feat-add">
          {libFactors.length > 0 && (
            <select value="" aria-label="从因子库加入" onChange={(e) => {
              const f = libFactors.find((x) => x.id === e.target.value)
              if (f && !feats.some((x) => x.factor_id === f.id)) setFeats([...feats, { name: f.name, expression: f.expression, factor_id: f.id, on: true }])
            }}>
              <option value="">从因子库加入…</option>
              {libFactors.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
            </select>
          )}
          <input className="fx-search" placeholder="或写一个表达式，例如 -ts_mean(funding, 9)" value={custom} onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') addCustom() }} aria-label="自定义特征表达式" />
          <button className="button small" onClick={addCustom} disabled={!custom.trim()}>加入</button>
        </div>
        <details className="ml-adv">
          <summary>超参数（可选）</summary>
          <div className="hint">默认值：<code>{JSON.stringify(learner?.params ?? {})}</code></div>
          <textarea className="fr-editor" rows={2} spellCheck={false} value={paramsText} placeholder='{"n_estimators": 300, "max_depth": 4}' onChange={(e) => setParamsText(e.target.value)} aria-label="超参数" />
        </details>
        <div className="ml-run">
          <button className="button primary" disabled={running || !learner?.available} onClick={start}>{running ? <><span className="spinner" /> {job?.phase || '训练中'}</> : '开始训练'}</button>
          {running && job && <button className="button small" onClick={() => models.cancel(job.id).catch(() => undefined)}>取消</button>}
          {job?.status === 'failed' && <span className="fr-err">{job.error}</span>}
          {job?.status === 'cancelled' && <span className="hint">已取消</span>}
        </div>
      </Panel>
      {result && (
        <Panel title="训练结果" actions={<>
          <input className="fx-search" placeholder="模型名称" value={name} onChange={(e) => setName(e.target.value)} aria-label="模型名称" />
          <button className="button small" onClick={() => save('candidate')}>存为候选</button>
          <button className="button small primary" disabled={!result.report.admitted} title={result.report.admitted ? '' : '泄漏检查或基线对照没有全部通过'} onClick={() => save('active')}>上线</button>
        </>}>
          <ModelFullReport report={result.report} />
        </Panel>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
function Runs({ onOpen, notify }: { onOpen: (id: string) => void; notify: PageProps['notify'] }) {
  const [data, setData] = useState<{ runs: ModelRun[]; running: TrainJob[]; trials: number } | null>(null)
  const [pick, setPick] = useState<string[]>([])
  const [cmp, setCmp] = useState<CompareView | null>(null)
  useEffect(() => { models.runs().then(setData).catch((e: Error) => notify(e.message, true)) }, [notify])
  useEffect(() => {
    if (pick.length !== 2) { setCmp(null); return }
    models.compare(pick[0], pick[1]).then(setCmp).catch((e: Error) => notify(e.message, true))
  }, [pick, notify])
  if (!data) return <Loading label="正在读取实验记录" />
  const toggle = (id: string) => setPick((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]))
  return (
    <>
      <Panel flush title={`实验记录（${data.runs.length}）`} subtitle="每次训练一笔，包括没通过检查的。勾选两笔可以对比。不同配置的数量会计入显著性门槛。">
        {data.running.length > 0 && <div className="ml-running">{data.running.map((j) => <span key={j.id}><span className="spinner" /> {j.model} · {j.phase}</span>)}</div>}
        {data.runs.length === 0 ? <EmptyState glyph="☰" title="还没有实验" /> : (
          <div className="table-wrap">
            <table className="fx-table">
              <thead><tr><th style={{ width: 28 }} /><th>实验</th><th className="right">样本外 RankIC</th><th className="right">基线</th><th className="right">多空夏普</th><th>检查</th><th>来源</th><th>时间</th></tr></thead>
              <tbody>{data.runs.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => onOpen(r.id)}>
                  <td onClick={(e) => e.stopPropagation()}><input type="checkbox" aria-label="加入对比" checked={pick.includes(r.id)} onChange={() => toggle(r.id)} /></td>
                  <td><strong>{r.model}</strong><span className="ml-sub">{r.label} · {r.features.length} 个特征 · {r.timeframe}{r.note ? ` · ${r.note}` : ''}</span></td>
                  <td className={`right num ${(r.summary.oos_rank_ic ?? 0) >= 0 ? 'positive' : 'negative'}`}>{f3(r.summary.oos_rank_ic)}</td>
                  <td className="right num muted">{f3(r.summary.baseline_rank_ic)}</td>
                  <td className="right num">{f3(r.summary.ls_sharpe, 2)}</td>
                  <td>{r.summary.leak_passed === 0 ? <Tag tone="warn">泄漏检查未过</Tag> : r.admitted ? <Tag tone="ok">可上线</Tag> : <Tag>未全过</Tag>}</td>
                  <td className="muted">{r.source === 'lab' ? 'AI 研究室' : '训练台'}</td>
                  <td className="muted">{when(r.created_at)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Panel>
      {cmp && <ComparePanel cmp={cmp} />}
    </>
  )
}

function ComparePanel({ cmp }: { cmp: CompareView }) {
  const LABEL: Record<string, string> = {
    oos_rank_ic: '样本外 RankIC', baseline_rank_ic: '基线 RankIC', beat_baseline: '超出基线', oos_icir: 'ICIR', dir_acc: '方向准确率', brier: 'Brier',
    ls_sharpe: '多空夏普', turnover: '换手', fold_positive: '各段为正', psi_max: '最大 PSI', leak_passed: '泄漏检查', samples: '样本', seconds: '用时（秒）',
  }
  const LOWER = new Set(['brier', 'turnover', 'psi_max', 'seconds'])
  const fmtv = (v: unknown) => (typeof v === 'number' ? (Number.isInteger(v) || Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(3)) : v === null || v === undefined ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v))
  const ca = cmp.curves.a || []
  const cb = cmp.curves.b || []
  const n = Math.max(ca.length, cb.length)
  const pad = (c: { val: number }[]) => Array.from({ length: n }, (_, i) => c[Math.min(i, c.length - 1)]?.val ?? NaN)
  const onlyA = cmp.features.a.filter((f) => !cmp.features.b.includes(f))
  const onlyB = cmp.features.b.filter((f) => !cmp.features.a.includes(f))
  return (
    <Panel title="对比" subtitle="A 是先勾选的一笔">
      <div className="fr-grid">
        <section>
          <table className="fr-table">
            <thead><tr><th>指标</th><th className="right">A</th><th className="right">B</th></tr></thead>
            <tbody>{cmp.metrics.map((m) => {
              const better = m.a !== null && m.b !== null && m.a !== m.b ? ((m.b > m.a) !== LOWER.has(m.key) ? 'b' : 'a') : ''
              return (
                <tr key={m.key}><td>{LABEL[m.key] ?? m.key}</td>
                  <td className={`right num${better === 'a' ? ' positive' : ''}`}>{fmtv(m.a)}</td>
                  <td className={`right num${better === 'b' ? ' positive' : ''}`}>{fmtv(m.b)}</td></tr>
              )
            })}</tbody>
          </table>
        </section>
        <section>
          <table className="fr-table">
            <thead><tr><th>规格</th><th>A</th><th>B</th></tr></thead>
            <tbody>{cmp.spec.map((s) => (
              <tr key={s.key} className={JSON.stringify(s.a) !== JSON.stringify(s.b) ? 'ml-diff' : ''}><td>{s.key}</td><td>{fmtv(s.a)}</td><td>{fmtv(s.b)}</td></tr>
            ))}</tbody>
          </table>
          {(onlyA.length > 0 || onlyB.length > 0) && <div className="hint">只在 A：{onlyA.join('、') || '—'}；只在 B：{onlyB.join('、') || '—'}</div>}
          {(ca.length > 1 || cb.length > 1) && (
            <>
              <h4 className="ml-h4">验证集损失</h4>
              <LineChart series={[...(ca.length > 1 ? [{ name: 'A', values: pad(ca) }] : []), ...(cb.length > 1 ? [{ name: 'B', values: pad(cb), dashed: true }] : [])]} width={560} timestamps={Array.from({ length: n }, (_, i) => i + 1)}
                xLabels={Array.from({ length: n }, (_, i) => `第 ${i + 1} 轮`)} format="num" height={160} zeroLine={false} ariaLabel="验证集损失对比" />
            </>
          )}
        </section>
      </div>
    </Panel>
  )
}

/* ------------------------------------------------------------------ */
function RunDetail({ id, meta, notify, onSaved }: { id: string; meta: ModelMeta | null; notify: PageProps['notify']; onSaved: (id: string) => void }) {
  const [data, setData] = useState<{ run: ModelRun; report: ModelReport } | null>(null)
  const [missing, setMissing] = useState(false)
  const [name, setName] = useState('')
  useEffect(() => { models.run(id).then(setData).catch(() => setMissing(true)) }, [id])
  const title = useMemo(() => {
    if (!data) return ''
    const l = meta?.learners.find((x) => x.id === data.run.model)?.label ?? data.run.model
    return `${l} · ${meta?.labels.find((x) => x.id === data.run.label)?.label ?? data.run.label}`
  }, [data, meta])
  if (missing) return <EmptyState glyph="☰" title="实验不存在">可能已被删除。</EmptyState>
  if (!data) return <Loading label="正在打开实验" />
  const save = async (status: ModelStatus) => {
    try {
      const r = await models.save({ run_id: id, name: name || undefined, status })
      notify(`已存入模型库「${r.model.name}」`); onSaved(r.model.id)
    } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <Panel title={title} subtitle={`${when(data.run.created_at)} · ${data.run.source === 'lab' ? 'AI 研究室' : '训练台'}`} actions={<>
      <input className="fx-search" placeholder="模型名称" value={name} onChange={(e) => setName(e.target.value)} aria-label="模型名称" />
      <button className="button small" onClick={() => save('candidate')}>存为候选</button>
      <button className="button small primary" disabled={!data.report.admitted} onClick={() => save('active')}>上线</button>
    </>}>
      {data.run.project_id && <div className="hint">来自 <a href={`#/lab/${data.run.project_id}`}>AI 研究室的项目</a></div>}
      <ModelFullReport report={data.report} />
      {data.report.fold_curves && data.report.fold_curves.length > 1 && (
        <section style={{ marginTop: 16 }}>
          <h4 className="ml-h4">各段学习曲线</h4>
          <div className="ml-curves">{data.report.fold_curves.map((c, i) => <div key={i}><span className="hint">第 {i + 1} 段</span><CurveChart curve={c} height={140} /></div>)}</div>
        </section>
      )}
    </Panel>
  )
}
