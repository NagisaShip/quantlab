/** AI 研究室里因子研究专用的卡片与面板：表达式卡、评估报告卡、右侧「因子」面板、版本成绩。 */
import { useEffect, useState } from 'react'
import type { FactorSummary, GoalMetric, LabMessage, LabProject, LabVersion } from '../../apiLab'
import { factors } from '../../apiFactors'
import { DiffView } from '../../components/Diff'
import { Checks, DecayAndGroups, f3, FactorStats, fp } from '../../components/factorReport'
import { LineChart } from '../../components/charts2'
import { fmtGoal } from './goal'

export const CATEGORY_LABEL: Record<string, string> = {
  risk_premium: '风险溢价', behavioral: '行为偏差', microstructure: '市场微观结构', structural: '结构性', other: '其他',
}

export function HypothesisCard({ h }: { h?: { category?: string; rationale?: string; direction?: string; falsify?: string } }) {
  if (!h || !h.rationale) return null
  return (
    <dl className="fr-hyp">
      {h.category && <><dt>类别</dt><dd>{CATEGORY_LABEL[h.category] ?? h.category}</dd></>}
      <dt>逻辑</dt><dd>{h.rationale}</dd>
      {h.direction && <><dt>预期</dt><dd>{h.direction}</dd></>}
      {h.falsify && <><dt>证伪</dt><dd>{h.falsify}</dd></>}
    </dl>
  )
}

export function FactorCodeCard({ m, onOpen }: { m: LabMessage; onOpen: () => void }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>{d.name ? `${d.name} · ` : ''}{m.content || '新因子'}</b>
        <span className="lab2-card-gap" />
        {d.before && <button className="link" onClick={() => setOpen(!open)}>{open ? '收起改动' : '改动'}</button>}
        <button className="link" onClick={onOpen}>因子</button>
      </div>
      <code className="fr-expr">{d.after}</code>
      <div className="lab2-faint">{(d.fields || []).join('、')} · {d.complexity} 个节点</div>
      {open && <DiffView before={d.before || ''} after={d.after || ''} maxHeight={200} />}
      {d.hypothesis?.rationale && !d.before && <HypothesisCard h={d.hypothesis} />}
    </div>
  )
}

export function FactorReportCard({ m, metrics }: { m: LabMessage; metrics: GoalMetric[] }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  const s = d.summary as FactorSummary
  const ev = d.evaluation as { passed: boolean; items: { key: string; metric: string; label: string; op: string; target: number; actual: number | null; ok: boolean; priority?: string }[] } | undefined
  const stress = (d.cost_scale ?? 1) !== 1
  const ic = (d.ic_series || []) as { t: number; cum: number }[]
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>{stress ? `${d.cost_scale}× 成本评估` : '因子评估'}</b>
        {!stress && ev && <span className={`cb-badge ${ev.passed ? 'ok' : 'warn'}`}>{ev.passed ? '达标' : '未达标'}</span>}
        {!stress && <span className={`cb-badge ${d.admitted ? 'ok' : ''}`}>{d.admitted ? '可入库' : '入库检查未全过'}</span>}
        <span className="lab2-card-gap" />
        {d.panel?.synthetic && <span className="lab2-faint">含合成数据</span>}
        {!stress && <button className="link" onClick={() => setOpen(!open)}>{open ? '收起' : '详情'}</button>}
      </div>
      <FactorStats s={s} compact />
      {!stress && ev && (
        <div className="lab2-checks">
          {ev.items.map((i) => (
            <span key={i.key} className={`${i.ok ? 'ok' : 'bad'}${i.priority === 'try' ? ' try' : ''}`}>
              {i.ok ? '✓' : '✗'} {i.label} {i.op} {fmtGoal(i.metric, i.target, metrics)}
              <small>（{fmtGoal(i.metric, i.actual, metrics)}）</small>
            </span>
          ))}
        </div>
      )}
      {open && (
        <div className="fr-card-more">
          <Checks checks={d.checks || []} />
          <DecayAndGroups report={{ decay: d.decay || [], groups: d.groups || { n: 0, mean: [] } }} />
          {ic.length > 2 && (
            <LineChart series={[{ name: '累计 RankIC', values: ic.map((x) => x.cum), area: true }]} timestamps={ic.map((x) => x.t)}
              format="num" height={150} width={560} ariaLabel="累计 RankIC" />
          )}
          {(d.regimes || []).length > 0 && (
            <div className="fr-regimes">
              {(d.regimes as { regime: string; rank_ic: number | null }[]).map((r) => (
                <span key={r.regime}>{r.regime} <b className={(r.rank_ic ?? 0) >= 0 ? 'up' : 'down'}>{f3(r.rank_ic)}</b></span>
              ))}
            </div>
          )}
        </div>
      )}
      {(d.panel?.warnings || []).length > 0 && <div className="lab2-faint">{(d.panel.warnings as string[]).slice(0, 2).join('；')}</div>}
    </div>
  )
}

export function FactorVersionMetrics({ v, metrics }: { v: LabVersion; metrics: GoalMetric[] }) {
  const s = v.metrics as unknown as FactorSummary | null
  if (!s) return <div className="lab2-faint">尚未评估</div>
  return (
    <div className="lab2-version-metrics">
      <span>RankIC <b>{fmtGoal('rank_ic', s.rank_ic, metrics)}</b></span>
      <span>ICIR <b>{f3(s.icir, 2)}</b></span>
      <span>样本外 <b>{fp(s.oos_retention)}</b></span>
      <span>换手 <b>{fp(s.turnover)}</b></span>
      {v.admitted && <span className="cb-badge ok">可入库</span>}
    </div>
  )
}

/** 右侧「因子」面板：当前表达式（可改）、假设卡、校验、操作。 */
export function FactorPanel({ project, running, onSave, onEvaluate, onSaveLibrary, onOpenFactor }: {
  project: LabProject; running: boolean
  onSave: (expr: string) => void; onEvaluate: () => void; onSaveLibrary: () => void; onOpenFactor: (id: string) => void
}) {
  const [expr, setExpr] = useState(project.code)
  const [dirty, setDirty] = useState(false)
  const [check, setCheck] = useState<{ ok: boolean; error?: string; expression?: string; complexity?: number; fields?: string[]; lookback?: number } | null>(null)
  useEffect(() => { if (!dirty) setExpr(project.code) }, [project.code, dirty])
  useEffect(() => {
    if (!expr.trim()) { setCheck(null); return }
    const t = window.setTimeout(() => { factors.validate(expr).then(setCheck).catch(() => undefined) }, 300)
    return () => window.clearTimeout(t)
  }, [expr])
  const cur = project.versions.find((v) => v.v === project.current_v)
  const s = cur?.metrics as unknown as FactorSummary | null
  return (
    <div className="lab2-code fr-panel">
      <div className="lab2-code-head">
        <b>{project.current_v ? `v${project.current_v}` : '新因子'}</b>
        {cur?.name && <span className="lab2-faint">{cur.name}</span>}
        {dirty && <span className="cb-badge warn">未保存</span>}
        <span className="lab2-card-gap" />
        <button className="button small" disabled={!dirty || !check?.ok || running} onClick={() => { onSave(expr); setDirty(false) }}>保存并评估</button>
      </div>
      <textarea className="fr-editor" spellCheck={false} rows={4} value={expr} placeholder="例如：-ts_mean(funding, 9)"
        onChange={(e) => { setExpr(e.target.value); setDirty(true) }} aria-label="因子表达式" />
      {check && (check.ok
        ? <div className="lab2-faint">字段 {(check.fields || []).join('、')} · {check.complexity} 个节点 · 需要约 {check.lookback} 根历史</div>
        : <div className="fr-err">{check.error}</div>)}
      {s && <><div className="fr-sec">最近一次评估</div><FactorStats s={s} compact /></>}
      {project.hypothesis?.rationale && <><div className="fr-sec">假设卡</div><HypothesisCard h={project.hypothesis} /></>}
      <div className="button-row" style={{ marginTop: 12 }}>
        <button className="button small" disabled={running || !project.code} onClick={onEvaluate}>重新评估</button>
        <button className="button small primary" disabled={running || !project.code} onClick={onSaveLibrary}>{project.factor_id ? '更新因子库' : '存入因子库'}</button>
        {project.factor_id && <button className="button small" onClick={() => onOpenFactor(project.factor_id!)}>在因子库中打开</button>}
      </div>
      <div className="hint">表达式在面板数据上逐元素计算，窗口参数必须是正整数常量；完整语法见「因子库 → 语法」。</div>
    </div>
  )
}
