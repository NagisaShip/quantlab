/** AI 研究室里组合研究专用的卡片与面板：配置方案卡、组合评估卡、右侧「方案」面板、版本成绩。 */
import { useState } from 'react'
import type { GoalMetric, LabMessage, LabProject, LabVersion } from '../../apiLab'
import type { PortfolioSummary } from '../../apiPortfolio'
import { DiffView } from '../../components/Diff'
import { Checks, f3, fp } from '../../components/factorReport'
import { NavCharts, PortfolioStats, StressTable, WeightsTable } from '../../components/portfolioReport'
import { fmtGoal } from './goal'
import { JsonSpecPanel } from './model'

const RISK_RULE: Record<string, (v: any) => string> = {
  vol_target: (v) => `目标波动 ${fp(v, 0)}`,
  var_limit: (v) => `VaR ≤ ${fp(v, 1)}`,
  dd_stop: (v) => `回撤止损 ${fp(v, 0)}`,
  cppi: (v) => `CPPI 底线 ${fp(v?.floor, 0)}`,
}

export function PortfolioSpecCard({ m, onOpen }: { m: LabMessage; onOpen: () => void }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  const spec = (d.spec || {}) as { expected?: string; lookback?: number; rebalance?: number; constraints?: Record<string, any>; risk?: Record<string, any> }
  const c = spec.constraints || {}
  const rules = Object.entries(spec.risk || {}).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => RISK_RULE[k]?.(v) ?? k)
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>{d.optimizer_label} · {d.risk_label}</b>
        <span className="lab2-card-gap" />
        {d.before && <button className="link" onClick={() => setOpen(!open)}>{open ? '收起改动' : '改动'}</button>}
        <button className="link" onClick={onOpen}>配置</button>
      </div>
      {m.content && <div className="lab2-faint">{m.content}</div>}
      <div className="lab2-params">
        {spec.expected && spec.expected !== 'none' && <code>预期：{spec.expected}</code>}
        {spec.lookback && <code>回看 {spec.lookback}</code>}
        {spec.rebalance && <code>每 {spec.rebalance} 根调仓</code>}
        {c.max_weight !== undefined && <code>单币 ≤ {fp(c.max_weight, 0)}</code>}
        {!!c.top_k && <code>前 {c.top_k} 名</code>}
        {rules.map((r) => <code key={r}>{r}</code>)}
      </div>
      {open && <DiffView before={d.before || ''} after={d.after || ''} maxHeight={320} />}
    </div>
  )
}

export function PortfolioReportCard({ m, metrics, onOpenRun }: { m: LabMessage; metrics: GoalMetric[]; onOpenRun: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  const s = d.summary as PortfolioSummary
  const ev = d.evaluation as { passed: boolean; items: { key: string; metric: string; label: string; op: string; target: number; actual: number | null; ok: boolean; priority?: string }[] } | undefined
  const triggers = Object.entries(s?.triggers || {}).filter(([, n]) => n > 0)
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>组合回测与风险评估</b>
        {ev && <span className={`cb-badge ${ev.passed ? 'ok' : 'warn'}`}>{ev.passed ? '达标' : '未达标'}</span>}
        <span className={`cb-badge ${d.admitted ? 'ok' : ''}`}>{d.admitted ? '可启用' : '检查未全过'}</span>
        <span className="lab2-card-gap" />
        {d.panel?.synthetic && <span className="lab2-faint">含合成数据</span>}
        <button className="link" onClick={() => setOpen(!open)}>{open ? '收起' : '详情'}</button>
        {d.run_id && <button className="link" onClick={() => onOpenRun(d.run_id)}>试验</button>}
      </div>
      <PortfolioStats s={s} compact />
      <div className="lab2-checks">
        {ev?.items.map((i) => (
          <span key={i.key} className={`${i.ok ? 'ok' : 'bad'}${i.priority === 'try' ? ' try' : ''}`}>
            {i.ok ? '✓' : '✗'} {i.label} {i.op} {fmtGoal(i.metric, i.target, metrics)}
            <small>（{fmtGoal(i.metric, i.actual, metrics)}）</small>
          </span>
        ))}
        {triggers.map(([k, n]) => <span key={k} className="try">风控触发 {k} × {n}</span>)}
      </div>
      {open && (
        <div className="fr-card-more">
          <Checks checks={d.checks || []} />
          <NavCharts nav={d.nav || []} height={150} width={600} />
          {d.latest && <WeightsTable latest={d.latest} />}
          {(d.stress || []).length > 0 && <StressTable rows={d.stress} />}
          <div className="lab2-faint">
            {s.optimizer} · {s.risk_model} · 调仓 {s.rebalances} 次 · 换手 {f3(s.turnover, 2)} · 成本 {s.cost_bps} bp
            {d.cost_scale && d.cost_scale !== 1 ? ` · ${d.cost_scale}× 成本` : ''}
          </div>
        </div>
      )}
    </div>
  )
}

export function PortfolioVersionMetrics({ v, metrics }: { v: LabVersion; metrics: GoalMetric[] }) {
  const s = v.metrics as unknown as PortfolioSummary | null
  if (!s) return <div className="lab2-faint">尚未评估</div>
  return (
    <div className="lab2-version-metrics">
      <span>夏普 <b>{fmtGoal('sharpe', s.sharpe, metrics)}</b></span>
      <span>超等权 <b>{f3(s.beat_ew, 2)}</b></span>
      <span>回撤 <b>{fp(s.max_drawdown, 1)}</b></span>
      <span>波动 <b>{fp(s.ann_vol, 1)}</b></span>
      {v.admitted && <span className="cb-badge ok">可启用</span>}
    </div>
  )
}

export function PortfolioPanel({ project, running, onSave, onRun, onSaveLibrary, onOpenScheme }: {
  project: LabProject; running: boolean
  onSave: (code: string) => void; onRun: () => void; onSaveLibrary: () => void; onOpenScheme: (id: string) => void
}) {
  const cur = project.versions.find((v) => v.v === project.current_v)
  return (
    <JsonSpecPanel project={project} running={running} noun="方案" runLabel="评估" libraryName="方案库" libId={project.library_id || null}
      stats={<PortfolioStats s={cur?.metrics as unknown as PortfolioSummary} compact />}
      placeholder={'{\n  "optimizer": "risk_parity",\n  "risk_model": "ledoit_wolf",\n  "lookback": 120,\n  "rebalance": 6,\n  "constraints": {"max_weight": 0.3},\n  "risk": {"vol_target": 0.4}\n}'}
      hint="字段：optimizer（equal / inverse_vol / min_var / mean_var / risk_parity / hrp / max_div / min_cvar / kelly / black_litterman）、risk_model（sample / ledoit_wolf / ewma / factor）、expected（none / momentum / mean / model:名称 / factor:名称）、lookback、rebalance、constraints、risk（vol_target / var_limit / dd_stop / cppi）。"
      onSave={onSave} onRun={onRun} onSaveLibrary={onSaveLibrary} onOpenLibrary={onOpenScheme} />
  )
}
