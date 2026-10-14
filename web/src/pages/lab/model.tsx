/** AI 研究室里模型研究专用的卡片与面板：训练规格卡、训练报告卡、右侧「模型」面板、版本成绩。 */
import { useEffect, useState, type ReactNode } from 'react'
import type { GoalMetric, LabMessage, LabProject, LabVersion } from '../../apiLab'
import type { ModelSummary } from '../../apiModels'
import { DiffView } from '../../components/Diff'
import { Checks, f3, fp } from '../../components/factorReport'
import { CurveChart, FoldsTable, HBars, isVol, ModelStats } from '../../components/modelReport'
import { LineChart } from '../../components/charts2'
import { fmtGoal } from './goal'

type Feature = { name: string; expression?: string; factor_id?: string }

function parseSpec(code: string): Record<string, any> | null {
  try { return JSON.parse(code) } catch { return null }
}

export function ModelSpecCard({ m, onOpen }: { m: LabMessage; onOpen: () => void }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  const spec = (d.spec || {}) as { features?: Feature[]; horizon?: number; params?: Record<string, unknown> }
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>{d.model_label} · 预测{d.label_label}{spec.horizon ? `（${spec.horizon} 期）` : ''}</b>
        <span className="lab2-card-gap" />
        {d.before && <button className="link" onClick={() => setOpen(!open)}>{open ? '收起改动' : '改动'}</button>}
        <button className="link" onClick={onOpen}>规格</button>
      </div>
      {m.content && <div className="lab2-faint">{m.content}</div>}
      <div className="lab2-params">
        {(spec.features || []).slice(0, 12).map((f) => <code key={f.name} title={f.expression}>{f.name}</code>)}
        {Object.entries(spec.params || {}).slice(0, 6).map(([k, v]) => <code key={k}>{k}={JSON.stringify(v)}</code>)}
      </div>
      {open && <DiffView before={d.before || ''} after={d.after || ''} maxHeight={320} />}
    </div>
  )
}

export function ModelReportCard({ m, metrics, onOpenRun }: { m: LabMessage; metrics: GoalMetric[]; onOpenRun: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  const s = d.summary as ModelSummary
  const ev = d.evaluation as { passed: boolean; items: { key: string; metric: string; label: string; op: string; target: number; actual: number | null; ok: boolean; priority?: string }[] } | undefined
  const ic = (d.ic_series || []) as { t: number; cum: number }[]
  const leak = (d.checks || []).filter((c: { required: boolean }) => c.required)
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>训练与样本外评估</b>
        {ev && <span className={`cb-badge ${ev.passed ? 'ok' : 'warn'}`}>{ev.passed ? '达标' : '未达标'}</span>}
        <span className={`cb-badge ${d.admitted ? 'ok' : ''}`}>{d.admitted ? '可上线' : '检查未全过'}</span>
        <span className="lab2-card-gap" />
        {d.panel?.synthetic && <span className="lab2-faint">含合成数据</span>}
        <button className="link" onClick={() => setOpen(!open)}>{open ? '收起' : '详情'}</button>
        {d.run_id && <button className="link" onClick={() => onOpenRun(d.run_id)}>实验</button>}
      </div>
      <ModelStats s={s} compact />
      <div className="lab2-checks">
        {leak.map((c: { key: string; label: string; ok: boolean }) => <span key={c.key} className={c.ok ? 'ok' : 'bad'}>{c.ok ? '✓' : '✗'} {c.label}</span>)}
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
          <div className="fr-grid">
            <section><h4 className="ml-h4">学习曲线</h4><CurveChart curve={d.curve || []} height={150} /></section>
            <section><h4 className="ml-h4">特征重要性</h4><HBars rows={(d.importance || []).map((x: { name: string; drop: number | null }) => ({ name: x.name, v: x.drop }))} fmt={(v) => v.toFixed(3)} /></section>
          </div>
          <FoldsTable folds={d.folds || []} />
          {!isVol(s) && ic.length > 2 && (
            <LineChart series={[{ name: '样本外累计 RankIC', values: ic.map((x) => x.cum), area: true }]} timestamps={ic.map((x) => x.t)}
              format="num" height={150} zeroLine width={600} ariaLabel="样本外累计 RankIC" />
          )}
          {(d.regimes || []).length > 0 && (
            <div className="fr-regimes">{d.regimes.map((r: { regime: string; rank_ic: number | null }) => (
              <span key={r.regime}>{r.regime} <b className={(r.rank_ic ?? 0) >= 0 ? 'up' : 'down'}>{f3(r.rank_ic)}</b></span>
            ))}</div>
          )}
          <div className="lab2-faint">基线：{d.baseline} · {s.samples} 个样本 · {s.features} 个特征{s.backend ? ` · ${s.backend}` : ''}</div>
        </div>
      )}
    </div>
  )
}

export function ModelVersionMetrics({ v, metrics }: { v: LabVersion; metrics: GoalMetric[] }) {
  const s = v.metrics as unknown as ModelSummary | null
  if (!s) return <div className="lab2-faint">尚未训练</div>
  return (
    <div className="lab2-version-metrics">
      <span>{isVol(s) ? '预测相关' : '样本外 RankIC'} <b>{fmtGoal('oos_rank_ic', s.oos_rank_ic, metrics)}</b></span>
      <span>超出基线 <b>{f3(s.beat_baseline)}</b></span>
      {!isVol(s) && <span>方向 <b>{fp(s.dir_acc, 1)}</b></span>}
      {s.leak_passed === 0 && <span className="cb-badge warn">泄漏检查未过</span>}
      {v.admitted && <span className="cb-badge ok">可上线</span>}
    </div>
  )
}

/** 右侧规格面板（模型 / 组合 / 执行共用）：当前 JSON 规格可改、最近一次成绩、操作。 */
export function JsonSpecPanel({ project, running, stats, noun, runLabel, libraryName, libId, hint, placeholder, onSave, onRun, onSaveLibrary, onOpenLibrary }: {
  project: LabProject; running: boolean; stats: ReactNode; noun: string; runLabel: string; libraryName: string; libId: string | null
  hint: ReactNode; placeholder: string
  onSave: (code: string) => void; onRun: () => void; onSaveLibrary: () => void; onOpenLibrary: (id: string) => void
}) {
  const [text, setText] = useState(project.code)
  const [dirty, setDirty] = useState(false)
  useEffect(() => { if (!dirty) setText(project.code) }, [project.code, dirty])
  const parsed = text.trim() ? parseSpec(text) : null
  const cur = project.versions.find((v) => v.v === project.current_v)
  return (
    <div className="lab2-code fr-panel">
      <div className="lab2-code-head">
        <b>{project.current_v ? `v${project.current_v}` : `新${noun}`}</b>
        {cur?.name && <span className="lab2-faint">{cur.name}</span>}
        {dirty && <span className="cb-badge warn">未保存</span>}
        <span className="lab2-card-gap" />
        <button className="button small" disabled={!dirty || !parsed || running} onClick={() => { onSave(text); setDirty(false) }}>保存并{runLabel}</button>
      </div>
      <textarea className="fr-editor" spellCheck={false} rows={14} value={text} placeholder={placeholder}
        onChange={(e) => { setText(e.target.value); setDirty(true) }} aria-label={`${noun}规格`} />
      {text.trim() && !parsed && <div className="fr-err">不是合法的 JSON</div>}
      {cur?.metrics && <><div className="fr-sec">最近一次{runLabel}</div>{stats}</>}
      <div className="button-row" style={{ marginTop: 12 }}>
        <button className="button small" disabled={running || !project.code} onClick={onRun}>重新{runLabel}</button>
        <button className="button small primary" disabled={running || !project.code} onClick={onSaveLibrary}>{libId ? '更新' : '存入'}{libraryName}</button>
        {libId && <button className="button small" onClick={() => onOpenLibrary(libId)}>在{libraryName}中打开</button>}
      </div>
      <div className="hint">{hint}</div>
    </div>
  )
}

export function ModelPanel({ project, running, onSave, onTrain, onSaveLibrary, onOpenModel }: {
  project: LabProject; running: boolean
  onSave: (code: string) => void; onTrain: () => void; onSaveLibrary: () => void; onOpenModel: (id: string) => void
}) {
  const cur = project.versions.find((v) => v.v === project.current_v)
  return (
    <JsonSpecPanel project={project} running={running} noun="模型" runLabel="训练" libraryName="模型库" libId={project.model_id || project.library_id || null}
      stats={<ModelStats s={cur?.metrics as unknown as ModelSummary} compact />}
      placeholder={'{\n  "model": "gbdt",\n  "label": "fwd_ret",\n  "horizon": 1,\n  "features": [{"name": "短期反转", "expression": "-returns(3)"}]\n}'}
      hint="字段：model（ridge / gbdt / mlp / lstm / gru / tcn / transformer / har / garch）、label（fwd_ret / cs_rank / direction / triple_barrier / volatility）、horizon、features（表达式或 factor_id）、params。"
      onSave={onSave} onRun={onTrain} onSaveLibrary={onSaveLibrary} onOpenLibrary={onOpenModel} />
  )
}
