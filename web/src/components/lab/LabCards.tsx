import type { ReactNode } from 'react'
import type { LabPlan } from '../../apiLab'

/** 目标卡片：研究室内与介绍页共用同一套目标条件、区间与操作区。 */
export interface GoalChipItem { text: string; priority?: string }
export interface GoalRowItem { label: string; value: ReactNode }

export function GoalCardView({ chips, rows, note, actions, className }: {
  chips: GoalChipItem[]
  rows: GoalRowItem[]
  note?: ReactNode
  actions?: ReactNode
  className?: string
}) {
  return (
    <div className={`goal-sum${className ? ' ' + className : ''}`}>
      <div className="goal-sum-chips">
        {chips.map((c, i) => <span key={i} className={`goal-chip${c.priority === 'try' ? ' try' : ''}`}>{c.text}</span>)}
      </div>
      <dl className="goal-sum-dl">
        {rows.map((r) => <div key={r.label}><dt>{r.label}</dt><dd>{r.value}</dd></div>)}
      </dl>
      {note && <div className="goal-sum-note">{note}</div>}
      {actions && <div className="goal-sum-actions">{actions}</div>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* /plan 计划卡片                                                     */
/* ------------------------------------------------------------------ */
export const STEP_MARK: Record<string, string> = { pending: '', doing: '', done: '✓', skipped: '–', failed: '!' }

export function planMeta(plan: LabPlan) {
  const done = plan.steps.filter((s) => s.status === 'done' || s.status === 'skipped').length
  return plan.status === 'done' ? `已完成 ${done}/${plan.steps.length}` : `${done}/${plan.steps.length}${plan.status === 'stopped' || !plan.autorun ? ' · 已暂停' : ''}`
}

export function PlanSteps({ plan }: { plan: LabPlan }) {
  return (
    <ol className="plan-steps stagger">
      {plan.steps.map((s, i) => (
        <li key={i} className={`plan-step ${s.status}`}>
          <span className="plan-mark" aria-label={s.status}>{s.status === 'doing' ? <span className="cb-spin" /> : STEP_MARK[s.status] || i + 1}</span>
          <span className="plan-text">
            {s.text}
            {s.note && <small>{s.note}</small>}
          </span>
        </li>
      ))}
    </ol>
  )
}

/** 对话里的计划卡片：live 是项目上的最新计划（进度随时更新），没有时用消息里存的那份。 */
export function PlanCard({ plan, live }: { plan: LabPlan; live: LabPlan | null }) {
  const p = live ?? plan
  if (!p?.steps?.length) return null
  const done = p.steps.filter((s) => s.status === 'done' || s.status === 'skipped').length
  return (
    <div className={`lab2-card plan-card ${p.status}`}>
      <div className="lab2-card-head">
        <b>计划</b>
        <span className="plan-task">{p.task}</span>
        <span className="lab2-card-gap" />
        <span className={`cb-badge ${p.status === 'done' ? 'ok' : p.status === 'stopped' || !p.autorun ? 'warn' : ''}`}>
          {p.status === 'done' ? '已完成' : p.status === 'stopped' || !p.autorun ? '已暂停' : '执行中'} · {done}/{p.steps.length}
        </span>
      </div>
      <div className="plan-bar" aria-hidden><i style={{ width: `${(done / p.steps.length) * 100}%` }} /></div>
      <PlanSteps plan={p} />
      {p.done_when && <div className="lab2-faint">完成标准：{p.done_when}</div>}
    </div>
  )
}
