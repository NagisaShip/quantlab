/** AI 研究室里执行研究专用的卡片与面板：执行方案卡、仿真报告卡、右侧「方案」面板、版本成绩。 */
import { useState } from 'react'
import type { GoalMetric, LabMessage, LabProject, LabVersion } from '../../apiLab'
import type { ExecSummary } from '../../apiExecution'
import { DiffView } from '../../components/Diff'
import { Checks, fp } from '../../components/factorReport'
import { AlertList, CostBreakdown, ExecStats, FrontierScatter, ReplayChart, SOURCE_LABEL, StateTable, bp } from '../../components/executionReport'
import { fmtGoal } from './goal'
import { JsonSpecPanel } from './model'

export const EXEC_STATES = ['平静', '趋势上行', '趋势下行', '剧烈波动', '流动性枯竭']

export function ExecutionSpecCard({ m, onOpen }: { m: LabMessage; onOpen: () => void }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  const spec = (d.spec || {}) as Record<string, any>
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>{d.algo_label} · {d.style_label}</b>
        <span className="lab2-card-gap" />
        {d.before && <button className="link" onClick={() => setOpen(!open)}>{open ? '收起改动' : '改动'}</button>}
        <button className="link" onClick={onOpen}>配置</button>
      </div>
      {m.content && <div className="lab2-faint">{m.content}</div>}
      <div className="lab2-params">
        {spec.horizon && <code>执行期 {spec.horizon} 步</code>}
        {spec.slices && ['twap', 'vwap', 'iceberg'].includes(spec.algo) && <code>{spec.slices} 片</code>}
        {spec.algo === 'pov' && <code>参与率 {fp(spec.participation, 0)}</code>}
        {spec.algo === 'almgren_chriss' && <code>紧迫度 {spec.urgency}</code>}
        {spec.algo === 'iceberg' && <code>露出 {fp(spec.display, 0)}</code>}
        {spec.max_participation && <code>参与上限 {fp(spec.max_participation, 0)}</code>}
        {spec.state_model && <code>状态：{spec.state_model === 'hmm' ? 'HMM' : '阈值规则'}</code>}
        {spec.order?.size_pct && <code>单笔 {fp(spec.order.size_pct, 1)} 日量</code>}
      </div>
      {open && <DiffView before={d.before || ''} after={d.after || ''} maxHeight={320} />}
    </div>
  )
}

export function ExecutionReportCard({ m, metrics, onOpenRun }: { m: LabMessage; metrics: GoalMetric[]; onOpenRun: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  const s = d.summary as ExecSummary
  const ev = d.evaluation as { passed: boolean; items: { key: string; metric: string; label: string; op: string; target: number; actual: number | null; ok: boolean; priority?: string }[] } | undefined
  const ex = d.example as null | { timeline: any[]; alerts: any[]; fills: any[]; side: number; symbol: string; is_bps: number }
  const sources = ((d.tape?.sources || []) as string[]).map((x) => SOURCE_LABEL[x] ?? x).join('、')
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>盘口回放仿真</b>
        {ev && <span className={`cb-badge ${ev.passed ? 'ok' : 'warn'}`}>{ev.passed ? '达标' : '未达标'}</span>}
        <span className={`cb-badge ${d.admitted ? 'ok' : ''}`}>{d.admitted ? '可启用' : '检查未全过'}</span>
        <span className="lab2-card-gap" />
        {sources && <span className="lab2-faint">{sources}</span>}
        <button className="link" onClick={() => setOpen(!open)}>{open ? '收起' : '详情'}</button>
        {d.run_id && <button className="link" onClick={() => onOpenRun(d.run_id)}>试验</button>}
      </div>
      <ExecStats s={s} compact />
      <div className="lab2-checks">
        {ev?.items.map((i) => (
          <span key={i.key} className={`${i.ok ? 'ok' : 'bad'}${i.priority === 'try' ? ' try' : ''}`}>
            {i.ok ? '✓' : '✗'} {i.label} {i.op} {fmtGoal(i.metric, i.target, metrics)}
            <small>（{fmtGoal(i.metric, i.actual, metrics)}）</small>
          </span>
        ))}
      </div>
      {open && (
        <div className="fr-card-more">
          <Checks checks={d.checks || []} />
          <CostBreakdown s={s} />
          {ex && ex.timeline?.length > 0 && (
            <>
              <div className="lab2-faint">示例片段：{ex.symbol} {ex.side > 0 ? '买入' : '卖出'}，成本 {bp(ex.is_bps)} bp</div>
              <ReplayChart timeline={ex.timeline} fills={ex.fills} alerts={ex.alerts} states={EXEC_STATES} height={240} />
              {ex.alerts?.length > 0 && <AlertList alerts={ex.alerts.slice(0, 6)} />}
            </>
          )}
          {(d.by_state || []).length > 0 && <StateTable rows={d.by_state} />}
          {(d.frontier || []).length > 0 && <FrontierScatter points={d.frontier} current={(d.frontier as { label: string; algo: string }[]).find((p) => p.algo === s.algo)?.label} />}
          {(d.tape?.warnings || []).length > 0 && <ul className="lab2-warn">{d.tape.warnings.slice(0, 3).map((w: string) => <li key={w}>{w}</li>)}</ul>}
          {d.cost_scale && d.cost_scale !== 1 ? <div className="lab2-faint">{d.cost_scale}× 成本压力</div> : null}
        </div>
      )}
    </div>
  )
}

export function ExecutionVersionMetrics({ v, metrics }: { v: LabVersion; metrics: GoalMetric[] }) {
  const s = v.metrics as unknown as ExecSummary | null
  if (!s) return <div className="lab2-faint">尚未仿真</div>
  return (
    <div className="lab2-version-metrics">
      <span>成本 <b>{fmtGoal('is_bps', s.is_bps, metrics)}</b></span>
      <span>比 TWAP 省 <b>{bp(s.beat_twap_bps)}</b></span>
      <span>完成 <b>{fp(s.fill_rate, 0)}</b></span>
      <span>被动 <b>{fp(s.maker_ratio, 0)}</b></span>
      {v.admitted && <span className="cb-badge ok">可启用</span>}
    </div>
  )
}

export function ExecutionPanel({ project, running, onSave, onRun, onSaveLibrary, onOpenScheme }: {
  project: LabProject; running: boolean
  onSave: (code: string) => void; onRun: () => void; onSaveLibrary: () => void; onOpenScheme: (id: string) => void
}) {
  const cur = project.versions.find((v) => v.v === project.current_v)
  return (
    <JsonSpecPanel project={project} running={running} noun="执行方案" runLabel="仿真" libraryName="执行方案库" libId={project.library_id || null}
      stats={<ExecStats s={cur?.metrics as unknown as ExecSummary} compact />}
      placeholder={'{\n  "algo": "adaptive",\n  "style": "neutral",\n  "horizon": 30,\n  "max_participation": 0.25,\n  "state_model": "hmm"\n}'}
      hint="字段：algo（market / twap / vwap / pov / iceberg / almgren_chriss / adaptive）、style（passive / neutral / aggressive）、horizon、slices、participation、max_participation、urgency、display、reprice_bps、state_model（rules / hmm）、adapt、order、fees。全部在仿真盘口上运行，不会真实下单。"
      onSave={onSave} onRun={onRun} onSaveLibrary={onSaveLibrary} onOpenLibrary={onOpenScheme} />
  )
}
