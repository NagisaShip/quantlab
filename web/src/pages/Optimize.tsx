import { useCallback, useEffect, useMemo, useState } from 'react'
import { ago, fmtDate, fmtNum, fmtPct, v3, type FullMetrics, type Optimization, type Params, type StrategyFile } from '../api2'
import { Heatmap, Scatter } from '../components/charts2'
import { Callout, EmptyState, Heading, Loading, Panel, Segments, SourceBanner, Tag } from '../components/ui'
import type { PageProps } from './types'

const OBJECTIVES = [
  { value: 'sharpe', label: '夏普比率' },
  { value: 'total_return', label: '累计收益' },
  { value: 'calmar', label: 'Calmar' },
  { value: 'sortino', label: '索提诺' },
  { value: 'excess_return', label: '超额收益' },
]
const DAY = 86_400_000
// 智能调参会给出 0.0018131500459474949 这种长小数，展示时保留 4 位有效数字
const pv = (v: unknown) => (typeof v === 'number' && !Number.isInteger(v) ? Number(v.toPrecision(4)) : v)
const paramText = (p: Params | Record<string, unknown>, sep = ' ') => Object.entries(p).map(([k, v]) => `${k}=${pv(v)}`).join(sep)

interface Axis { on: boolean; from: string; to: string; step: string; list: string; manual: boolean }

function valuesOf(a: Axis, sample: number | string | boolean): (number | string)[] {
  if (a.manual) {
    return a.list.split(/[,，\s]+/).filter(Boolean).map((x) => (typeof sample === 'number' ? Number(x) : x)).filter((x) => x !== '' && !(typeof x === 'number' && Number.isNaN(x)))
  }
  const f = Number(a.from)
  const t = Number(a.to)
  const s = Number(a.step)
  if (!Number.isFinite(f) || !Number.isFinite(t) || !Number.isFinite(s) || s <= 0 || t < f) return []
  const out: number[] = []
  for (let v = f; v <= t + s * 1e-9 && out.length < 60; v += s) out.push(Number(v.toFixed(6)))
  return Number.isInteger(sample as number) ? out.map((v) => Math.round(v)).filter((v, i, arr) => arr.indexOf(v) === i) : out
}

function defaultAxis(v: number | string | boolean): Axis {
  if (typeof v !== 'number') return { on: false, from: '', to: '', step: '', list: String(v), manual: true }
  if (Number.isInteger(v)) {
    const step = Math.max(1, Math.round(v / 4))
    return { on: false, from: String(Math.max(1, v - 2 * step)), to: String(v + 2 * step), step: String(step), list: '', manual: false }
  }
  const step = Number((Math.abs(v) / 4 || 0.05).toPrecision(2))
  return { on: false, from: String(Number((v - 2 * step).toFixed(4))), to: String(Number((v + 2 * step).toFixed(4))), step: String(step), list: '', manual: false }
}

export default function OptimizePage({ slug, onNavigate, notify, setBusy }: PageProps & { slug: string }) {
  const [strategy, setStrategy] = useState<StrategyFile | null>(null)
  const [axes, setAxes] = useState<Record<string, Axis>>({})
  const [objective, setObjective] = useState('sharpe')
  const [split, setSplit] = useState(70)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [source, setSource] = useState<'auto' | 'exchange' | 'synthetic'>('auto')
  const [job, setJob] = useState<{ id: string; done: number; total: number } | null>(null)
  const [result, setResult] = useState<Optimization | null>(null)
  const [history, setHistory] = useState<{ id: string; objective: string; combinations: number; created_at: number; best_params: Params; method?: string }[]>([])
  const [error, setError] = useState('')
  const [view, setView] = useState<'in' | 'out'>('out')
  const [method, setMethod] = useState<'grid' | 'bayes'>('grid')
  const [trials, setTrials] = useState(40)
  const [multi, setMulti] = useState(false)

  useEffect(() => {
    v3.strategy(slug).then((r) => {
      setStrategy(r.strategy)
      const ax: Record<string, Axis> = {}
      Object.entries(r.strategy.params || {}).forEach(([k, v], i) => { ax[k] = { ...defaultAxis(v), on: i < 2 && typeof v === 'number' } })
      setAxes(ax)
      let st: { start?: string; end?: string; source?: 'auto' | 'exchange' | 'synthetic' } = {}
      try { st = JSON.parse(localStorage.getItem(`qa.run.${slug}`) || '{}') } catch { /* 忽略 */ }
      const now = Date.now()
      setStart(st.start || new Date(now - 180 * DAY).toISOString().slice(0, 10))
      setEnd(st.end || new Date(now).toISOString().slice(0, 10))
      setSource(st.source || 'auto')
    }).catch((e) => setError(e.message))
    v3.optimizations(slug).then((r) => setHistory(r.optimizations)).catch(() => undefined)
  }, [slug])

  const grid = useMemo(() => {
    const g: Record<string, (number | string)[]> = {}
    if (!strategy) return g
    for (const [k, a] of Object.entries(axes)) if (a.on) g[k] = valuesOf(a, strategy.params[k])
    return g
  }, [axes, strategy])
  const gridCombos = Object.values(grid).reduce((n, v) => n * Math.max(1, v.length), Object.keys(grid).length ? 1 : 0)
  // 智能调参：勾选的参数给范围（起、止），或手动列出候选值；optuna 在里面搜 trials 次
  const space = useMemo(() => {
    const sp: Record<string, unknown> = {}
    if (!strategy) return sp
    for (const [k, a] of Object.entries(axes)) {
      if (!a.on) continue
      if (a.manual) { const vals = valuesOf(a, strategy.params[k]); if (vals.length) sp[k] = { choices: vals }; continue }
      const lo = Number(a.from)
      const hi = Number(a.to)
      if (Number.isFinite(lo) && Number.isFinite(hi) && hi > lo) {
        const isInt = Number.isInteger(strategy.params[k] as number)
        sp[k] = isInt ? { low: Math.round(lo), high: Math.round(hi) } : { low: lo, high: hi }
      }
    }
    return sp
  }, [axes, strategy])
  const combos = method === 'bayes' ? (Object.keys(space).length ? trials : 0) : gridCombos

  const poll = useCallback(async (id: string) => {
    for (;;) {
      await new Promise((r) => setTimeout(r, 700))
      const j = await v3.job<{ id: string }>(id)
      setJob({ id, done: j.done, total: j.total })
      if (j.status === 'done' && j.result) {
        const opt = await v3.optimization(j.result.id)
        setResult(opt)
        setJob(null)
        v3.optimizations(slug).then((r) => setHistory(r.optimizations)).catch(() => undefined)
        return
      }
      if (j.status === 'failed') {
        setJob(null)
        notify(j.error || '优化失败', true)
        return
      }
    }
  }, [notify, slug])

  const run = async () => {
    if (!strategy) return
    if (!combos) return notify('至少勾选一个参数，并给出取值', true)
    if (combos > 150) return notify(`组合数 ${combos} 超过上限 150，请缩小范围或加大步长`, true)
    try {
      const r = await v3.optimize({
        slug,
        grid: method === 'bayes' ? {} : grid,
        method,
        ...(method === 'bayes' ? { space, n_trials: trials, multi_objective: multi } : {}),
        split: split / 100,
        objective,
        source,
        start: Date.parse(`${start}T00:00:00Z`),
        end: Math.min(Date.parse(`${end}T00:00:00Z`) + DAY, Date.now()),
        securities: strategy.securities,
        timeframe: strategy.timeframe,
      })
      setResult(null)
      setJob({ id: r.job_id, done: 0, total: r.combinations })
      poll(r.job_id)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const apply = async (params: Params) => {
    try {
      await v3.applyParams(slug, params)
      notify('参数已写回策略，生成新版本')
      onNavigate(`editor/${slug}`)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const backtestWith = async (params: Params) => {
    if (!result) return
    setBusy({ title: '用这组参数跑完整区间回测' })
    try {
      const r = await v3.runBacktest({ ...result.spec, slug, params, persist: true, label: `优化参数 ${Object.entries(params).map(([k, v]) => `${k}=${v}`).join(' ')}` })
      if (r.id) onNavigate(`backtest/${r.id}`)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(null)
    }
  }

  if (error) return <Callout tone="fail">{error}</Callout>
  if (!strategy) return <Loading />
  const keys = Object.keys(strategy.params || {})

  return (
    <>
      <Heading
        eyebrow="PARAMETER OPTIMIZATION"
        title={`参数优化 · ${strategy.name}`}
        description="区间被切成样本内（挑参数）与样本外（只验证）两段。好的参数应当在样本外也排名靠前；只看样本内最高的那一格，几乎一定会被过拟合骗到。"
        actions={<button className="button" onClick={() => onNavigate(`editor/${slug}`)}>← 回到编辑器</button>}
      />
      {!keys.length ? (
        <EmptyState glyph="⚙" title="策略没有声明 PARAMS">在策略顶部写 PARAMS = {'{...}'} 后才能做参数优化。</EmptyState>
      ) : (
        <div className="two-columns">
          <Panel title={method === 'bayes' ? '参数范围' : '参数网格'}
            subtitle={method === 'bayes' ? `智能调参 ${trials} 次 · 只看起止范围 · 每组在样本内与样本外各跑一次` : `组合数 ${combos}（上限 150）· 每个组合在样本内与样本外各跑一次`}
            actions={<Segments value={method} onChange={setMethod} ariaLabel="搜索方式" options={[{ value: 'grid', label: '网格穷举' }, { value: 'bayes', label: '智能调参' }]} />}>
            <table className="param-table">
              <thead><tr><th /><th>参数</th><th>当前</th><th>取值</th><th className="right">个数</th></tr></thead>
              <tbody>
                {keys.map((k) => {
                  const a = axes[k]
                  if (!a) return null
                  const vals = valuesOf(a, strategy.params[k])
                  const setA = (p: Partial<Axis>) => setAxes({ ...axes, [k]: { ...a, ...p } })
                  return (
                    <tr key={k}>
                      <td><input type="checkbox" checked={a.on} onChange={(e) => setA({ on: e.target.checked })} style={{ width: 'auto', margin: 0 }} aria-label={`优化 ${k}`} /></td>
                      <td><code className="inline">{k}</code></td>
                      <td>{String(strategy.params[k])}</td>
                      <td>
                        {a.manual ? (
                          <input value={a.list} disabled={!a.on} onChange={(e) => setA({ list: e.target.value })} placeholder="逗号分隔，如 10, 20, 30" />
                        ) : (
                          <div className="range-inputs">
                            <input value={a.from} disabled={!a.on} onChange={(e) => setA({ from: e.target.value })} aria-label="起" />
                            <span>→</span>
                            <input value={a.to} disabled={!a.on} onChange={(e) => setA({ to: e.target.value })} aria-label="止" />
                            {method !== 'bayes' && <>
                              <span>步长</span>
                              <input value={a.step} disabled={!a.on} onChange={(e) => setA({ step: e.target.value })} aria-label="步长" />
                            </>}
                          </div>
                        )}
                        <button className="link small" disabled={!a.on} onClick={() => setA({ manual: !a.manual, list: a.manual ? a.list : vals.join(', ') })}>{a.manual ? '改用区间' : '手动列出'}</button>
                      </td>
                      <td className="right">{!a.on ? '—' : method === 'bayes' ? (a.manual ? vals.length : '范围') : vals.length}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <div className="form-grid" style={{ marginTop: 16 }}>
              <label>开始<input type="date" value={start} onChange={(e) => setStart(e.target.value)} /></label>
              <label>结束<input type="date" value={end} onChange={(e) => setEnd(e.target.value)} /></label>
              <label>优化目标<select value={objective} onChange={(e) => setObjective(e.target.value)}>{OBJECTIVES.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</select></label>
              <label>行情来源<select value={source} onChange={(e) => setSource(e.target.value as typeof source)}><option value="auto">真实行情（缓存优先）</option><option value="exchange">只用真实行情</option></select></label>
            </div>
            {method === 'bayes' && (
              <div className="form-grid" style={{ marginTop: 12 }}>
                <label>试验次数<input type="number" min={5} max={150} value={trials} onChange={(e) => setTrials(Math.max(5, Math.min(150, Number(e.target.value) || 5)))} /></label>
                <label className="check-row" style={{ alignSelf: 'end' }}><input type="checkbox" checked={multi} onChange={(e) => setMulti(e.target.checked)} /> 同时压低最大回撤（多目标）</label>
              </div>
            )}
            {method === 'bayes' && <div className="hint">用 optuna 的贝叶斯搜索：先随机试几组，再往表现好的区域集中。参数多（3 个以上）时，同样的次数通常比网格搜到更好的区域。每一组试验都会记进试验台账，试得越多，晋级关卡的缩减夏普要求越严。</div>}
            <label style={{ marginTop: 14 }}>样本内比例：{split}%（样本外 {100 - split}%）
              <input type="range" min={40} max={90} step={5} value={split} onChange={(e) => setSplit(Number(e.target.value))} />
            </label>
            <div className="button-row" style={{ marginTop: 14 }}>
              <button className="button primary" disabled={Boolean(job) || !combos} onClick={run}>{job ? '优化中…' : method === 'bayes' ? `开始智能调参（${combos} 次）` : `开始优化（${combos} 组）`}</button>
            </div>
            {job && (
              <div style={{ marginTop: 14 }}>
                <div className="progress-track"><div style={{ width: `${job.total ? (job.done / job.total) * 100 : 3}%` }} /></div>
                <div className="hint">已完成 {job.done} / {job.total} 组</div>
              </div>
            )}
          </Panel>
          <div className="side-column">
            <Panel title="历史优化">
              {!history.length ? <div className="hint">还没有优化记录</div> : (
                <ul className="plain-list">
                  {history.map((h) => (
                    <li key={h.id}>
                      <button className="link" onClick={async () => setResult(await v3.optimization(h.id))}>
                        {ago(h.created_at)} · {h.method === 'bayes' ? '智能 · ' : ''}{OBJECTIVES.find((o) => o.value === h.objective)?.label} · {h.combinations} 组
                      </button>
                      <small className="mono">{paramText(h.best_params || {})}</small>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
            <Panel title="怎么读结果">
              <ul className="plain">
                <li><b>秩相关</b>：样本内排名与样本外排名的一致程度。接近 1 说明参数有效；接近 0 甚至为负，说明"好参数"多半是噪声。</li>
                <li><b>最优组合样本外排名</b>：样本内第一、样本外垫底是最典型的过拟合。</li>
                <li><b>热力图</b>：优先选"一片区域都不错"的参数，而不是孤立的最高点。</li>
              </ul>
            </Panel>
          </div>
        </div>
      )}

      {result && <OptResult opt={result} view={view} setView={setView} onApply={apply} onBacktest={backtestWith} />}
    </>
  )
}

function metricOf(m: Partial<FullMetrics> | undefined, key: string): number | null {
  const v = m?.[key as keyof FullMetrics]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function OptResult({ opt, view, setView, onApply, onBacktest }: {
  opt: Optimization
  view: 'in' | 'out'
  setView: (v: 'in' | 'out') => void
  onApply: (p: Params) => void
  onBacktest: (p: Params) => void
}) {
  const d = opt.diagnostics
  const keys = Object.keys(opt.grid).filter((k) => opt.grid[k].length > 1)
  const fmt = opt.objective === 'total_return' || opt.objective === 'excess_return' ? (v: number | null) => fmtPct(v) : (v: number | null) => fmtNum(v)
  const rows = [...opt.rows].sort((a, b) => (a.in_rank ?? 999) - (b.in_rank ?? 999))
  const heat = useMemo(() => {
    if (keys.length !== 2) return null
    const [rk, ck] = keys
    const rvals = opt.grid[rk]
    const cvals = opt.grid[ck]
    const values = rvals.map((r) => cvals.map((c) => {
      const hit = opt.rows.find((x) => x.params[rk] === r && x.params[ck] === c)
      return metricOf(view === 'in' ? hit?.in_sample : hit?.out_sample, opt.objective)
    }))
    const bp = opt.best?.params
    const best: [number, number] | null = bp ? [rvals.indexOf(bp[rk] as never), cvals.indexOf(bp[ck] as never)] : null
    return { rk, ck, rvals, cvals, values, best }
  }, [keys, opt, view])
  const points = opt.rows
    .map((r) => ({ x: metricOf(r.in_sample, opt.objective), y: metricOf(r.out_sample, opt.objective), label: paramText(r.params) }))
    .filter((p): p is { x: number; y: number; label: string } => p.x !== null && p.y !== null)
  const bestIdx = opt.best ? points.findIndex((p) => p.label === paramText(opt.best!.params)) : -1

  return (
    <>
      <SourceBanner
        tone={d.warnings.some((w) => w.includes('过拟合') || w.includes('噪声') || w.includes('只排')) ? 'warn' : 'real'}
        icon="⚖"
        title={`${opt.row.strategy_name} · ${opt.method === 'bayes' ? `智能调参${opt.multi_objective ? '（多目标）' : ''}` : '网格'} · 按${opt.objective_label}优化 · ${d.combinations} 组`}
        detail={`样本内 / 样本外切分于 ${fmtDate(opt.cut_ts)}（${Math.round(opt.split * 100)}% / ${Math.round((1 - opt.split) * 100)}%）· ${opt.datasets.map((x) => x.source_label).join('、')}`}
      />
      {opt.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
      <div className="metrics-grid">
        <div className="metric-card"><div className="metric-label"><span>样本内最优</span></div><div className="metric-number">{fmt(d.best_in ?? null)}</div><div className="metric-caption">{opt.best && paramText(opt.best.params, '  ')}</div></div>
        <div className="metric-card"><div className="metric-label"><span>同一组参数在样本外</span></div><div className={`metric-number ${d.best_out !== null && d.best_out !== undefined && d.best_out < 0 ? 'negative' : ''}`}>{fmt(d.best_out ?? null)}</div><div className="metric-caption">样本外排名 {d.best_out_rank ?? '—'} / {d.valid}</div></div>
        <div className="metric-card"><div className="metric-label"><span>样本内外秩相关</span></div><div className="metric-number">{fmtNum(d.rank_correlation ?? null)}</div><div className="metric-caption">越接近 1 越说明参数排序可靠</div></div>
        <div className="metric-card"><div className="metric-label"><span>样本外中位数</span></div><div className="metric-number">{fmt(d.median_out ?? null)}</div><div className="metric-caption">全部组合在样本外的中位表现</div></div>
      </div>
      <Panel title="诊断">
        <ul className="plain">{d.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
        {opt.best && (
          <div className="button-row" style={{ marginTop: 12 }}>
            <button className="button" onClick={() => onBacktest(opt.best!.params)}>用最优参数跑完整区间回测</button>
            <button className="button primary" onClick={() => onApply(opt.best!.params)}>写回策略代码</button>
          </div>
        )}
      </Panel>
      <div className="two-columns">
        {heat ? (
          <Panel title="参数热力图" subtitle={`${opt.objective_label}；方框为样本内最优`} actions={<Segments value={view} onChange={setView} options={[{ value: 'in', label: '样本内' }, { value: 'out', label: '样本外' }]} />}>
            <Heatmap rows={heat.rvals} cols={heat.cvals} values={heat.values} rowLabel={heat.rk} colLabel={heat.ck} format={opt.objective.includes('return') ? 'pct' : 'num'} best={heat.best} />
          </Panel>
        ) : (
          <Panel title="参数热力图"><div className="hint">{opt.method === 'bayes' ? '智能调参的取值不在网格上，没有热力图；看右边的散点和下面的列表。' : '恰好优化两个参数时显示热力图。'}</div></Panel>
        )}
        <Panel title="样本内 vs 样本外" subtitle="每个点是一组参数；理想情况是点沿对角线分布。橙色为样本内最优">
          <Scatter points={points} xLabel={`样本内 ${opt.objective_label}`} yLabel={`样本外 ${opt.objective_label}`} highlight={bestIdx >= 0 ? bestIdx : undefined} />
        </Panel>
      </div>
      <Panel title="全部组合" subtitle="按样本内排名排序" flush>
        <div className="table-wrap">
          <table>
            <thead><tr><th>内排名</th><th>外排名</th><th>参数</th><th className="right">内·{opt.objective_label}</th><th className="right">外·{opt.objective_label}</th><th className="right">内·收益</th><th className="right">外·收益</th><th className="right">外·回撤</th><th className="right">外·交易</th><th /></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td>{r.in_rank ?? '—'}</td>
                  <td>{r.out_rank ?? '—'}</td>
                  <td className="mono small">{paramText(r.params, '  ')}{r.error && <small className="negative">{r.error}</small>}</td>
                  <td className="right">{fmt(metricOf(r.in_sample, opt.objective))}</td>
                  <td className="right">{fmt(metricOf(r.out_sample, opt.objective))}</td>
                  <td className="right">{fmtPct(r.in_sample?.total_return)}</td>
                  <td className="right">{fmtPct(r.out_sample?.total_return)}</td>
                  <td className="right">{fmtPct(r.out_sample?.max_drawdown)}</td>
                  <td className="right">{r.out_sample?.trades ?? '—'}</td>
                  <td><button className="button small" onClick={() => onApply(r.params)}>写回</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
      <div className="hint" style={{ marginBottom: 20 }}><Tag>提示</Tag> 写回会生成策略新版本；已批准的版本因此需要重新批准。</div>
    </>
  )
}
