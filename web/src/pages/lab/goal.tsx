/**
 * /goal：目标表单、目标标签、格式化工具。
 * 表单边填边做规则检查（不调模型）；「让模型复核」才会调用模型。不合理的字段旁边给出建议值，可一键采用。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { lab, type GoalCheck, type GoalCond, type GoalIssue, type GoalMetric, type GoalSpec, type LabKind, type LabMeta, type LabProject } from '../../apiLab'

export const FALLBACK_METRICS: GoalMetric[] = [
  { metric: 'sharpe', label: '夏普', unit: 'num', op: '>=' },
  { metric: 'max_drawdown', label: '最大回撤', unit: 'pct', op: '<=' },
  { metric: 'trades', label: '交易笔数', unit: 'int', op: '>=' },
  { metric: 'excess_return', label: '超额收益', unit: 'pct', op: '>=' },
  { metric: 'annualized_return', label: '年化收益', unit: 'pct', op: '>=' },
  { metric: 'win_rate', label: '胜率', unit: 'pct', op: '>=' },
]

const LEGACY: Record<string, [string, '>=' | '<=']> = {
  min_sharpe: ['sharpe', '>='], max_drawdown: ['max_drawdown', '<='], min_trades: ['trades', '>='], min_excess: ['excess_return', '>='],
}

/** 按研究类型取目标指标目录：因子研究用因子指标（RankIC、ICIR……），其余用回测指标。 */
export function domainMeta(meta: LabMeta | null, kind: LabKind | undefined): LabMeta | null {
  const d = kind && kind !== 'strategy' ? meta?.domains?.[kind] : undefined
  if (!meta || !d) return meta
  return { ...meta, goal_metrics: d.metrics, goal_templates: d.templates }
}

export function metricsOf(meta: LabMeta | null): GoalMetric[] {
  return meta?.goal_metrics?.length ? meta.goal_metrics : FALLBACK_METRICS
}

export function unitOf(metric: string, metrics: GoalMetric[]): 'pct' | 'num' | 'int' | 'bps' {
  return metrics.find((m) => m.metric === metric)?.unit ?? (metric === 'max_drawdown' ? 'pct' : 'num')
}

export function fmtGoal(metric: string, v: number | null | undefined, metrics: GoalMetric[]): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const u = unitOf(metric, metrics)
  if (u === 'pct') return `${+(v * 100).toFixed(1)}%`
  if (u === 'int') return String(Math.round(v))
  if (u === 'bps') return `${+v.toFixed(1)} bps`
  return String(Math.abs(v) < 0.1 && v !== 0 ? +v.toFixed(3) : +v.toFixed(2))
}

/** 项目当前生效的条件：优先结构化目标，没有就从旧字段转。 */
export function projectItems(p: LabProject | null): GoalCond[] {
  if (!p) return []
  if (p.goal?.items?.length) return p.goal.items
  return Object.entries(p.goals || {})
    .filter(([k, v]) => v !== null && v !== undefined && LEGACY[k])
    .map(([k, v]) => ({ metric: LEGACY[k][0], op: LEGACY[k][1], target: v as number, priority: 'must' as const }))
}

export function projectSpec(p: LabProject): GoalSpec {
  return {
    items: projectItems(p), symbols: p.securities, timeframe: p.timeframe,
    start: p.start ?? null, end: p.end ?? null, bars_limit: p.bars_limit, max_rounds: p.max_rounds, note: p.goal?.note || '',
    ...(p.kind === 'factor' ? { horizon: p.horizon ?? 1 } : {}),
  }
}

export function condText(c: GoalCond, metrics: GoalMetric[]) {
  const label = metrics.find((m) => m.metric === c.metric)?.label ?? c.metric
  return `${label} ${c.op === '>=' ? '≥' : '≤'} ${fmtGoal(c.metric, c.target, metrics)}`
}

/** 输入框上方的目标标签：点一下打开表单。 */
export function GoalChips({ items, metrics, onOpen }: { items: GoalCond[]; metrics: GoalMetric[]; onOpen: () => void }) {
  return (
    <button type="button" className="goal-chips" onClick={onOpen} title="编辑目标（/goal）">
      <span className="goal-chips-k">目标</span>
      {items.length ? items.map((c, i) => (
        <span key={i} className={`goal-chip${c.priority === 'try' ? ' try' : ''}`}>{condText(c, metrics)}</span>
      )) : <span className="goal-chip empty">未设置</span>}
    </button>
  )
}

const day = (ms: number | null) => (ms ? new Date(ms).toISOString().slice(0, 10) : '')
const toMs = (d: string) => (d ? Date.parse(d + 'T00:00:00Z') : null)

/* ------------------------------------------------------------------ */
/* 表单                                                                */
/* ------------------------------------------------------------------ */
export function GoalForm({ meta, initial, projectId, unparsed, onCancel, onSubmit, submitting, compact, startLabel = '保存并开始迭代', domain }: {
  meta: LabMeta | null
  domain?: LabKind
  initial: GoalSpec
  projectId?: string
  unparsed?: string
  onCancel?: () => void
  onSubmit: (spec: GoalSpec, start: boolean) => void
  submitting?: boolean
  compact?: boolean         // 嵌在侧边面板里：不显示标题与取消
  startLabel?: string
}) {
  const metrics = metricsOf(meta)
  const [spec, setSpec] = useState<GoalSpec>(initial)
  const [check, setCheck] = useState<GoalCheck | null>(null)
  const [reviewing, setReviewing] = useState(false)
  const [rangeMode, setRangeMode] = useState<'bars' | 'dates'>(initial.start ? 'dates' : 'bars')
  const timer = useRef<number>()

  useEffect(() => { setSpec(initial); setRangeMode(initial.start ? 'dates' : 'bars') }, [initial])

  // 边填边检查（规则，不调模型）
  useEffect(() => {
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      lab.goalCheck(spec, projectId, false, domain).then((r) => setCheck(r.check)).catch(() => undefined)
    }, 350)
    return () => window.clearTimeout(timer.current)
  }, [spec, projectId, domain])
  const factor = domain === 'factor'

  const issuesFor = (field: string) => (check?.issues || []).filter((i) => i.field === field)
  const setItem = (idx: number, patch: Partial<GoalCond>) =>
    setSpec({ ...spec, items: spec.items.map((c, i) => (i === idx ? { ...c, ...patch } : c)) })
  const addItem = () => {
    const used = new Set(spec.items.map((c) => c.metric))
    const m = metrics.find((x) => !used.has(x.metric)) ?? metrics[0]
    const target = m.unit === 'pct' ? (factor ? 0.5 : 0.1) : m.unit === 'int' ? 10 : factor ? (m.op === '<=' ? 0.7 : 0.03) : 1
    setSpec({ ...spec, items: [...spec.items, { metric: m.metric, op: m.op, target, priority: 'must' }] })
  }
  const adopt = (issue: GoalIssue) => {
    const m = /^items\.(\d+)\.target$/.exec(issue.field)
    if (m && typeof issue.suggest === 'number') setItem(Number(m[1]), { target: issue.suggest })
    else if (issue.field === 'timeframe' && typeof issue.suggest === 'string') setSpec({ ...spec, timeframe: issue.suggest })
    else if (issue.field === 'max_rounds' && typeof issue.suggest === 'number') setSpec({ ...spec, max_rounds: issue.suggest })
    else if (issue.field === 'items' && issue.suggest && typeof issue.suggest === 'object') {
      const items = (issue.suggest as { items?: GoalCond[] }).items
      if (items) setSpec({ ...spec, items })
    }
  }
  const review = async () => {
    setReviewing(true)
    try { setCheck((await lab.goalCheck(spec, projectId, true, domain)).check) } catch { /* 规则结果仍在 */ } finally { setReviewing(false) }
  }

  const tone = check?.verdict === 'unreasonable' ? 'bad' : check?.verdict === 'warn' ? 'warn' : 'ok'
  const general = (check?.issues || []).filter((i) => !/^items\.\d+\.target$/.test(i.field) && i.field !== 'timeframe' && i.field !== 'max_rounds')

  return (
    <div className={`goal-form${compact ? ' compact' : ''}`} role="form" aria-label="目标">
      {!compact && (
        <div className="goal-form-head">
          <b>目标</b>
          <span className="goal-form-sub">{factor ? '达标就停；不达标带着评估报告继续改' : '达标就停；不达标带着回测结果继续改'}</span>
        </div>
      )}
      {unparsed && <div className="goal-note">没识别的部分：{unparsed}</div>}

      <div className="goal-section-label">指标条件</div>
      <div className="goal-rows">
        {spec.items.map((c, idx) => {
          const u = unitOf(c.metric, metrics)
          const shown = u === 'pct' ? +(c.target * 100).toFixed(2) : c.target
          const iss = issuesFor(`items.${idx}.target`)
          return (
            <div key={idx} className="goal-row-wrap">
              <div className="goal-row">
                <select value={c.metric} aria-label="指标" onChange={(e) => {
                  const m = metrics.find((x) => x.metric === e.target.value)!
                  setItem(idx, { metric: m.metric, op: m.op })
                }}>
                  {metrics.map((m) => <option key={m.metric} value={m.metric}>{m.label}</option>)}
                </select>
                <button type="button" className="goal-op" onClick={() => setItem(idx, { op: c.op === '>=' ? '<=' : '>=' })} title="切换方向">
                  {c.op === '>=' ? '≥' : '≤'}
                </button>
                <span className="goal-num">
                  <input type="number" step={u === 'int' ? 1 : u === 'pct' ? 1 : factor ? 0.01 : 0.1} value={Number.isFinite(shown) ? shown : ''} aria-label="目标值"
                    onChange={(e) => {
                      const v = e.target.value === '' ? 0 : Number(e.target.value)
                      setItem(idx, { target: u === 'pct' ? v / 100 : v })
                    }} />
                  {u === 'pct' && <em>%</em>}{u === 'int' && <em>笔</em>}{u === 'bps' && <em>bps</em>}
                </span>
                <div className="goal-pri" role="radiogroup" aria-label="优先级">
                  <button type="button" role="radio" aria-checked={c.priority === 'must'} className={c.priority === 'must' ? 'on' : ''}
                    onClick={() => setItem(idx, { priority: 'must' })}>必达</button>
                  <button type="button" role="radio" aria-checked={c.priority === 'try'} className={c.priority === 'try' ? 'on' : ''}
                    onClick={() => setItem(idx, { priority: 'try' })}>尽量</button>
                </div>
                <button type="button" className="goal-del" aria-label="删除条件" onClick={() => setSpec({ ...spec, items: spec.items.filter((_, i) => i !== idx) })}>×</button>
              </div>
              {iss.map((i, k) => <IssueLine key={k} issue={i} metric={c.metric} metrics={metrics} onAdopt={() => adopt(i)} />)}
            </div>
          )
        })}
      </div>
      <div className="goal-add-row">
        <button type="button" className="link" onClick={addItem}>＋ 添加条件</button>
        {meta?.goal_templates?.length ? (
          <span className="goal-tpls">
            {meta.goal_templates.map((t) => (
              <button key={t.id} type="button" className="goal-tpl" onClick={() => setSpec({ ...spec, items: t.items })}>{t.name}</button>
            ))}
          </span>
        ) : null}
      </div>

      <div className="goal-section-label">{factor ? '评估范围' : '回测范围'}</div>
      <div className="goal-grid">
        <label>标的<input value={spec.symbols.join(', ')} onChange={(e) => setSpec({ ...spec, symbols: e.target.value.split(/[,，\s]+/).filter(Boolean) })} /></label>
        <label>周期
          <select value={spec.timeframe} onChange={(e) => setSpec({ ...spec, timeframe: e.target.value })}>
            {['15m', '30m', '1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
        <label>区间
          <span className="goal-range">
            <select value={rangeMode} onChange={(e) => {
              const v = e.target.value as 'bars' | 'dates'
              setRangeMode(v)
              if (v === 'bars') setSpec({ ...spec, start: null, end: null })
              else setSpec({ ...spec, start: spec.start ?? Date.now() - 365 * 86400000, end: null })
            }}>
              <option value="bars">最近 N 根 K 线</option><option value="dates">按日期</option>
            </select>
            {rangeMode === 'bars' ? (
              <input type="number" min={200} max={20000} step={100} value={spec.bars_limit}
                onChange={(e) => setSpec({ ...spec, bars_limit: Number(e.target.value) || 1500 })} aria-label="K 线数" />
            ) : (
              <>
                <input type="date" value={day(spec.start)} onChange={(e) => setSpec({ ...spec, start: toMs(e.target.value) })} aria-label="开始日期" />
                <input type="date" value={day(spec.end)} onChange={(e) => setSpec({ ...spec, end: toMs(e.target.value) })} aria-label="结束日期" title="留空表示到现在" />
              </>
            )}
          </span>
        </label>
        {factor && (
          <label>持有期
            <select value={spec.horizon ?? 1} onChange={(e) => setSpec({ ...spec, horizon: Number(e.target.value) })}>
              {[1, 2, 3, 4, 6, 12, 24].map((n) => <option key={n} value={n}>{n} 根 K 线</option>)}
            </select>
          </label>
        )}
        <label>最多轮数
          <select value={spec.max_rounds} onChange={(e) => setSpec({ ...spec, max_rounds: Number(e.target.value) })}>
            {[1, 2, 3, 4, 5, 6, 8].map((n) => <option key={n} value={n}>{n} 轮</option>)}
          </select>
        </label>
      </div>
      {['timeframe', 'max_rounds'].flatMap((f) => issuesFor(f)).map((i, k) => <IssueLine key={k} issue={i} metrics={metrics} onAdopt={() => adopt(i)} />)}

      {check && (
        <div className={`goal-verdict ${tone}`}>
          <span>{check.summary}{check.bars ? `（约 ${check.bars.toLocaleString()} 根 K 线）` : ''}</span>
          {general.map((i, k) => <IssueLine key={k} issue={i} metrics={metrics} onAdopt={i.suggest ? () => adopt(i) : undefined} />)}
          {check.model_summary && <span className="goal-model">模型复核：{check.model_summary}</span>}
        </div>
      )}

      <div className="goal-actions">
        <button type="button" className="button small ghost" disabled={reviewing || !projectId} onClick={review}
          title={projectId ? '让模型判断目标是否合理' : '创建项目后可用'}>{reviewing ? '模型复核中…' : '让模型复核'}</button>
        <span className="goal-gap" />
        {onCancel && <button type="button" className="button small ghost" onClick={onCancel}>取消</button>}
        <button type="button" className="button small" disabled={submitting || !spec.items.length} onClick={() => onSubmit(spec, false)}>保存目标</button>
        <button type="button" className="button small primary" disabled={submitting || !spec.items.length} onClick={() => onSubmit(spec, true)}>{startLabel}</button>
      </div>
    </div>
  )
}

function IssueLine({ issue, metric, metrics, onAdopt }: { issue: GoalIssue; metric?: string; metrics: GoalMetric[]; onAdopt?: () => void }) {
  const sug = issue.suggest
  const sugText = typeof sug === 'number' && metric ? fmtGoal(metric, sug, metrics) : typeof sug === 'number' || typeof sug === 'string' ? String(sug) : ''
  return (
    <div className={`goal-issue ${issue.level}`}>
      <span>{issue.problem}{issue.by === 'model' ? '（模型）' : ''}</span>
      {onAdopt && sug !== null && sug !== undefined && (
        <button type="button" className="link" onClick={onAdopt}>{sugText ? `改为 ${sugText}` : '采用建议'}</button>
      )}
    </div>
  )
}

export function useDefaultSpec(meta: LabMeta | null, kind: LabKind = 'strategy'): GoalSpec {
  return useMemo(() => kind !== 'strategy' ? {
    items: meta?.domains?.[kind]?.templates?.[0]?.items ?? (kind === 'factor' ? [{ metric: 'rank_ic', op: '>=', target: 0.03, priority: 'must' }] : []),
    symbols: meta?.universes?.[0]?.symbols ?? ['BTC/USDT', 'ETH/USDT'], timeframe: '4h', start: null, end: null,
    bars_limit: 1500, max_rounds: 4, horizon: 1,
  } : ({
    items: meta?.goal_templates?.[0]?.items ?? [
      { metric: 'sharpe', op: '>=', target: 1, priority: 'must' },
      { metric: 'max_drawdown', op: '<=', target: 0.25, priority: 'must' },
      { metric: 'trades', op: '>=', target: 10, priority: 'must' },
    ],
    symbols: ['BTC/USDT'], timeframe: '1h', start: null, end: null, bars_limit: 1500, max_rounds: 4,
  }), [meta, kind])
}
