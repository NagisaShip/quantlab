/**
 * 因子库：已入库 / 候选因子的列表与详情、因子评估器、试验台账、表达式语法。
 * 路由：#/factors（列表）· #/factors/new（评估器）· #/factors/trials · #/factors/syntax · #/factors/<id>
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { PythonCodeButton } from '../components/PythonCode'
import { factors, type EvalParams, type Factor, type FactorMeta, type FactorReport, type FactorStatus } from '../apiFactors'
import { openInLab } from '../components/handoff'
import { f3, fp, FullReport, STATUS_TONE } from '../components/factorReport'
import { Sparkline } from '../components/charts2'
import { EmptyState, Field, Heading, Loading, Panel, Segments, Tag } from '../components/ui'
import { HypothesisCard } from './lab/factor'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'
import { RecordPinButton } from '../components/RecordPin'
import { READ_ONLY_REASON_ID, ReadOnlyRouteGate, useReadOnlyCapability } from '../features/workflows/ReadOnlyCapability'

type Tab = 'list' | 'new' | 'trials' | 'syntax'
const TABS: { value: Tab; label: string }[] = [
  { value: 'list', label: '因子库' }, { value: 'new', label: '评估器' }, { value: 'trials', label: '试验台账' }, { value: 'syntax', label: '表达式语法' },
]
const day = (ms?: number | null) => (ms ? new Date(ms * (ms < 1e11 ? 1000 : 1)).toISOString().slice(0, 10) : '—')

export default function FactorsPage({ arg, onNavigate, notify }: PageProps & { arg?: string }) {
  const [meta, setMeta] = useState<FactorMeta | null>(null)
  const loadMeta = useCallback(() => { factors.meta().then(setMeta).catch((e: Error) => notify(e.message, true)) }, [notify])
  useEffect(loadMeta, [loadMeta])
  const ro = useReadOnlyCapability('factor')
  const readOnly = ro?.readOnly ?? false
  const tab: Tab | null = !arg ? 'list' : (['new', 'trials', 'syntax'] as string[]).includes(arg) ? (arg as Tab) : null

  return (
    <>
      <Heading title="因子" description="有经济学逻辑、通过样本外与多重检验的因子才进库。每次评估都记入试验台账，试得越多，显著性门槛越高。"
        actions={<button className="button primary" disabled={readOnly} title={readOnly ? ro?.reason : undefined}
          aria-describedby={readOnly ? READ_ONLY_REASON_ID : undefined}
          onClick={() => { if (!readOnly) openInLab({ idea: '', kind: 'factor' }) }}>✦ 让 AI 研究一个因子</button>} />
      <div className="fx-tabs">
        <Segments value={tab ?? 'list'} options={TABS} onChange={(t) => onNavigate(t === 'list' ? 'factors' : `factors/${t}`)} ariaLabel="因子库视图" />
        {meta && <span className="hint">已入库或候选因子之外，累计试过 {meta.trials} 个表达式</span>}
      </div>
      {tab === 'list' && <FactorList meta={meta} onOpen={(id) => onNavigate(`factors/${id}`)} notify={notify} />}
      {tab === 'new' && (readOnly
        ? <ReadOnlyRouteGate title="因子评估器暂时不可用" historyLabel="因子库" historyRoute="factors" onNavigate={onNavigate} />
        : <Evaluator meta={meta} notify={notify} onSaved={(id) => onNavigate(`factors/${id}`)} onEvaluated={loadMeta} />)}
      {tab === 'trials' && <Trials />}
      {tab === 'syntax' && <Syntax meta={meta} />}
      {tab === null && arg && <FactorDetail key={arg} id={arg} meta={meta} notify={notify} onDeleted={() => onNavigate('factors')} />}
    </>
  )
}

/* ------------------------------------------------------------------ */
function FactorList({ meta, onOpen, notify }: { meta: FactorMeta | null; onOpen: (id: string) => void; notify: PageProps['notify'] }) {
  const [rows, setRows] = useState<Factor[] | null>(null)
  const [status, setStatus] = useState<'' | FactorStatus>('')
  const [q, setQ] = useState('')
  useEffect(() => {
    const t = window.setTimeout(() => {
      factors.list(status || undefined, q).then((r) => setRows(r.factors)).catch((e: Error) => notify(e.message, true))
    }, 200)
    return () => window.clearTimeout(t)
  }, [status, q, notify])
  const statuses = meta?.statuses ?? { candidate: '候选', active: '入库', decaying: '衰减预警', retired: '退役' }
  return (
    <Panel flush title="因子" actions={
      <div className="fx-filters">
        <Segments value={status} onChange={setStatus} ariaLabel="状态"
          options={[{ value: '' as const, label: '全部' }, ...(Object.entries(statuses) as [FactorStatus, string][]).map(([value, label]) => ({ value, label }))]} />
        <input className="fx-search" placeholder="搜索名称、表达式、假设" value={q} onChange={(e) => setQ(e.target.value)} aria-label="搜索因子" />
      </div>
    }>
      {rows === null ? <Loading label="正在读取因子库" /> : rows.length === 0 ? (
        <EmptyState glyph="ƒ" title={q || status ? '没有匹配的因子' : '因子库还是空的'}>在 AI 研究室选择「因子」开始研究，或在评估器里直接写一个表达式试试。</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="fx-table">
            <thead><tr>
              <th>因子</th><th>状态</th><th className="right">RankIC</th><th className="right">ICIR</th><th className="right">样本外保留</th>
              <th className="right">换手</th><th className="right">多空夏普</th><th>评估于</th><th>保护</th>
            </tr></thead>
            <tbody>
              {rows.map((f) => (
                <tr key={f.id} className="clickable" onClick={() => onOpen(f.id)}>
                  <td><strong>{f.name}</strong><code className="fx-expr-sm">{f.expression}</code></td>
                  <td>
                    <Tag tone={STATUS_TONE[f.status]}>{statuses[f.status]}</Tag>
                    {f.health_flag === 'decaying' && f.status !== 'decaying' && <Tag tone="warn">IC 在衰减</Tag>}
                  </td>
                  <td className={`right num ${(f.summary?.rank_ic ?? 0) >= 0 ? 'positive' : 'negative'}`}>{f3(f.summary?.rank_ic)}</td>
                  <td className="right num">{f3(f.summary?.icir, 2)}</td>
                  <td className="right num">{fp(f.summary?.oos_retention)}</td>
                  <td className="right num">{fp(f.summary?.turnover)}</td>
                  <td className="right num">{f3(f.summary?.ls_sharpe, 2)}</td>
                  <td className="muted">{day(f.evaluated_at)}</td>
                  <td onClick={(e) => e.stopPropagation()}><RecordPinButton type="factor" id={f.id} pinned={f.pinned} notify={notify} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

/* ------------------------------------------------------------------ */
function FactorDetail({ id, meta, notify, onDeleted }: { id: string; meta: FactorMeta | null; notify: PageProps['notify']; onDeleted: () => void }) {
  const [f, setF] = useState<Factor | null>(null)
  const [report, setReport] = useState<FactorReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [missing, setMissing] = useState(false)
  const ro = useReadOnlyCapability('factor')
  const readOnly = ro?.readOnly ?? false
  const load = useCallback(async () => {
    try {
      const r = await factors.get(id)
      setF(r.factor)
      factors.report(id).then((x) => setReport(x.report)).catch(() => setReport(null))
    } catch { setMissing(true) }
  }, [id])
  useEffect(() => { load() }, [load])

  if (missing) return <EmptyState glyph="ƒ" title="因子不存在">可能已被删除。</EmptyState>
  if (!f) return <Loading label="正在打开因子" />
  const statuses = meta?.statuses ?? { candidate: '候选', active: '入库', decaying: '衰减预警', retired: '退役' }
  const health = (f.health || []).map((h) => h.rank_ic ?? 0)

  const refresh = async () => {
    if (readOnly) return
    setBusy(true)
    try {
      const r = await factors.refresh(id)
      setF(r.factor); setReport(r.report)
      notify(r.factor.health_flag === 'decaying' ? '重新评估完成：RankIC 明显低于入库时，已标为衰减预警' : '重新评估完成')
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  return (
    <>
      <Panel title={f.name} actions={<>
        <select value={f.status} aria-label="状态" disabled={readOnly} title={readOnly ? ro?.reason : undefined}
          aria-describedby={readOnly ? READ_ONLY_REASON_ID : undefined}
          onChange={async (e) => {
            if (readOnly) return
            try { setF((await factors.setStatus(id, e.target.value as FactorStatus)).factor) } catch (err) { notify((err as Error).message, true) }
          }}>
          {(Object.entries(statuses) as [FactorStatus, string][]).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <button className="button small" disabled={busy || readOnly} title={readOnly ? ro?.reason : undefined}
          aria-describedby={readOnly ? READ_ONLY_REASON_ID : undefined} onClick={refresh}>
          {busy ? <><span className="spinner" /> 评估中</> : '用最新数据重新评估'}
        </button>
        <button className="button small" disabled={readOnly} title={readOnly ? ro?.reason : undefined}
          aria-describedby={readOnly ? READ_ONLY_REASON_ID : undefined}
          onClick={() => { if (!readOnly) openInLab({
            kind: 'factor', idea: `改进因子「${f.name}」`, code: f.expression, title: `改进：${f.name}`, securities: f.universe, timeframe: f.timeframe,
            attached: `因子 ${f.expression}`, pins: f.hypothesis?.rationale ? [{ kind: 'note', title: '原假设', text: `${f.hypothesis.rationale}\n预期：${f.hypothesis.direction ?? ''}\n证伪：${f.hypothesis.falsify ?? ''}` }] : [],
          }) }}>在 AI 研究室中改进</button>
        <PythonCodeButton kind="factor" artifact={f.expression} name={f.name} />
        <RecordPinButton type="factor" id={f.id} pinned={f.pinned} notify={notify} />
        <button className="button small danger" disabled={readOnly} title={readOnly ? ro?.reason : undefined}
          aria-describedby={readOnly ? READ_ONLY_REASON_ID : undefined}
          onClick={async () => {
            if (readOnly) return
            if (await appConfirm(`删除因子「${f.name}」？试验台账会保留。`)) { await factors.remove(id); onDeleted() }
          }}>删除</button>
      </>}>
        <code className="fr-expr big">{f.expression}</code>
        <div className="fx-meta">
          <span>{f.universe.length} 个标的 · {f.timeframe} · 持有 {f.horizon} 期</span>
          <span>来源：{f.created_by === 'ai' ? 'AI 研究室' : '手动'}{f.project_id && <> · <a href={`#/lab/${f.project_id}`}>研究记录</a></>}</span>
          {health.length > 1 && <span className="fx-health">RankIC 历次评估 <Sparkline values={health} width={100} height={22} /></span>}
        </div>
        <HypothesisCard h={f.hypothesis} />
        {f.note && <div className="hint">{f.note}</div>}
      </Panel>
      <Panel title="评估报告" subtitle={report?.panel ? `评估于 ${day(f.evaluated_at)}` : undefined}>
        {report ? <FullReport report={report} /> : <EmptyState glyph="ρ" title="还没有评估报告">{readOnly ? '当前无法重新生成，启用后可重新评估。' : '点「用最新数据重新评估」生成。'}</EmptyState>}
      </Panel>
    </>
  )
}

/* ------------------------------------------------------------------ */
function Evaluator({ meta, notify, onSaved, onEvaluated }: { meta: FactorMeta | null; notify: PageProps['notify']; onSaved: (id: string) => void; onEvaluated?: () => void }) {
  const [p, setP] = useState<EvalParams>({ expression: '', universe: 'major10', timeframe: '4h', horizon: 1, bars_limit: 1500, source: 'auto', cost_bps: 10 })
  const [check, setCheck] = useState<{ ok: boolean; error?: string; fields?: string[]; complexity?: number; lookback?: number } | null>(null)
  const [report, setReport] = useState<FactorReport | null>(null)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  useEffect(() => {
    if (!p.expression.trim()) { setCheck(null); return }
    const t = window.setTimeout(() => factors.validate(p.expression).then(setCheck).catch(() => undefined), 300)
    return () => window.clearTimeout(t)
  }, [p.expression])
  const run = async () => {
    setBusy(true)
    try { setReport(await factors.evaluate(p)); onEvaluated?.() } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const save = async (status: FactorStatus) => {
    try {
      const u = meta?.universes.find((x) => x.id === p.universe)
      const r = await factors.create({ name: name || undefined, expression: p.expression, status, universe: u?.symbols, timeframe: p.timeframe, horizon: p.horizon, evaluate: true })
      notify(`已存入因子库「${r.factor.name}」`)
      onSaved(r.factor.id)
    } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <>
      <Panel title="评估一个表达式" subtitle="在标的池上算 IC、衰减、分组、多空、样本外和与库内因子的相关性。每次评估都计入试验台账。">
        <textarea className="fr-editor" rows={3} spellCheck={false} value={p.expression} placeholder="例如：-ts_mean(funding, 9)　或　returns(30) - returns(3)"
          onChange={(e) => setP({ ...p, expression: e.target.value })} aria-label="因子表达式" />
        {check && (check.ok
          ? <div className="hint">字段 {(check.fields || []).join('、')} · {check.complexity} 个节点 · 需要约 {check.lookback} 根历史</div>
          : <div className="fr-err">{check.error}</div>)}
        {meta && (
          <div className="fx-seeds">
            <span className="hint">种子假设：</span>
            {meta.seeds.map((s) => <button key={s.name} className="lab2-example" title={s.hypothesis.rationale} onClick={() => { setP({ ...p, expression: s.expression }); setName(s.name) }}>{s.name}</button>)}
          </div>
        )}
        <div className="config-bar">
          <Field label="标的池">
            <select value={p.universe} onChange={(e) => setP({ ...p, universe: e.target.value })}>
              {(meta?.universes ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}（{u.symbols.length}）</option>)}
            </select>
          </Field>
          <Field label="周期">
            <select value={p.timeframe} onChange={(e) => setP({ ...p, timeframe: e.target.value })}>{['1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}</select>
          </Field>
          <Field label="持有期">
            <select value={p.horizon} onChange={(e) => setP({ ...p, horizon: Number(e.target.value) })}>{[1, 2, 3, 4, 6, 12, 24].map((n) => <option key={n} value={n}>{n} 根</option>)}</select>
          </Field>
          <Field label="K 线数">
            <select value={p.bars_limit} onChange={(e) => setP({ ...p, bars_limit: Number(e.target.value) })}>{[600, 1500, 3000, 6000].map((n) => <option key={n} value={n}>{n}</option>)}</select>
          </Field>
          <Field label="数据">
            <select value={p.source} onChange={(e) => setP({ ...p, source: e.target.value as EvalParams['source'] })}>
              <option value="auto">真实行情（缓存优先）</option><option value="exchange">只用真实数据</option>
            </select>
          </Field>
          <Field label="单边成本 bps">
            <input type="number" min={0} max={200} value={p.cost_bps} onChange={(e) => setP({ ...p, cost_bps: Number(e.target.value) })} />
          </Field>
          <div style={{ display: 'flex', alignItems: 'flex-end' }}>
            <button className="button primary" disabled={busy || !check?.ok} onClick={run}>{busy ? <><span className="spinner" /> 评估中</> : 'ρ 评估'}</button>
          </div>
        </div>
      </Panel>
      {report && (
        <Panel title="评估结果" actions={<>
          <input className="fx-search" placeholder="因子名称" value={name} onChange={(e) => setName(e.target.value)} aria-label="因子名称" />
          <button className="button small" onClick={() => save('candidate')}>存为候选</button>
          <button className="button small primary" disabled={!report.admitted} title={report.admitted ? '' : '入库检查没有全部通过'} onClick={() => save('active')}>入库</button>
        </>}>
          <FullReport report={report} />
        </Panel>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
function Trials() {
  const [data, setData] = useState<Awaited<ReturnType<typeof factors.trials>> | null>(null)
  useEffect(() => { factors.trials(200).then(setData).catch(() => setData({ trials: [], total: 0 })) }, [])
  if (!data) return <Loading label="正在读取试验台账" />
  return (
    <Panel flush title={`试验台账（${data.total}）`} subtitle="评估过的每个表达式，包括失败的。用于多重检验修正，也告诉 AI 哪些方向已经试过、没用。">
      {data.trials.length === 0 ? <EmptyState glyph="☰" title="还没有试验记录" /> : (
        <div className="table-wrap">
          <table className="fx-table">
            <thead><tr><th>表达式</th><th className="right">RankIC</th><th className="right">t 值</th><th className="right">次数</th><th>来源</th><th>最近</th></tr></thead>
            <tbody>{data.trials.map((t) => (
              <tr key={t.id}>
                <td><code className="fx-expr-sm">{t.expression}</code></td>
                <td className="right num">{f3(t.rank_ic)}</td>
                <td className={`right num${t.ic_t !== null && Math.abs(t.ic_t) < 1 ? ' muted' : ''}`}>{f3(t.ic_t, 2)}</td>
                <td className="right num">{t.count}</td>
                <td className="muted">{t.source === 'lab' ? 'AI 研究室' : '评估器'}</td>
                <td className="muted">{day(t.last_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function Syntax({ meta }: { meta: FactorMeta | null }) {
  const groups = useMemo(() => {
    const g: Record<string, { name: string; sig: string; doc: string }[]> = {}
    for (const o of meta?.catalog.operators ?? []) (g[o.group] ||= []).push(o)
    return g
  }, [meta])
  const fieldGroups = useMemo(() => {
    const g: Record<string, { name: string; label: string }[]> = {}
    for (const f of meta?.catalog.fields ?? []) (g[f.group] ||= []).push(f)
    return g
  }, [meta])
  if (!meta) return <Loading />
  return (
    <div className="two-columns even">
      <Panel title="字段" subtitle="衍生品、情绪、链上字段需要先在「行情与数据」同步；没有真实数据时如实报「未接入数据」，不会用合成序列顶替。">
        {Object.entries(fieldGroups).map(([g, items]) => (
          <div key={g} className="fx-syn-group">
            <h4>{g}</h4>
            {items.map((f) => <div key={f.name} className="fx-syn-row"><code>{f.name}</code><span>{f.label}</span></div>)}
          </div>
        ))}
        <div className="fx-syn-group">
          <h4>自定义</h4>
          {meta.custom_fields.length ? meta.custom_fields.map((x) => <div key={x} className="fx-syn-row"><code>{x}</code><span>导入的数据</span></div>)
            : <div className="hint">在「行情与数据」导入 CSV 或同步 FRED / Glassnode 后，用 x_名字 引用。</div>}
        </div>
      </Panel>
      <Panel title="算子" subtitle={`窗口参数必须是正整数常量，最长 ${meta.limits.max_window}；表达式最多 ${meta.limits.max_nodes} 个节点。`}>
        {Object.entries(groups).map(([g, items]) => (
          <div key={g} className="fx-syn-group">
            <h4>{g}</h4>
            {items.map((o) => <div key={o.name} className="fx-syn-row"><code>{o.name}({o.sig})</code><span>{o.doc}</span></div>)}
          </div>
        ))}
      </Panel>
    </div>
  )
}
