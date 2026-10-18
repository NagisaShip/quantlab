/** 研究导师：把研究方法拆成可勾选的课程；进度保存在工作区/用户下。 */
import { useEffect, useState } from 'react'
import { get, put } from '../../http'
import { Callout, Loading, Panel, Tag } from '../../components/ui'
import type { PageProps } from '../types'

interface Lesson {
  id: string
  title: string
  summary: string
  checks: string[]
  practice: string
  red_flags: string[]
  completed: boolean
}

interface Course {
  title: string
  principle: string
  modules: Lesson[]
  progress: number
  completed: string[]
  total: number
}

export default function MentorCourse({ notify, onNavigate, features }: Pick<PageProps, 'notify' | 'onNavigate' | 'features'>) {
  const [course, setCourse] = useState<Course | null>(null)
  const [busy, setBusy] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const [loadError, setLoadError] = useState('')
  const disabled = features?.['research.mentor'] === false

  useEffect(() => {
    if (disabled) return
    let alive = true
    setLoadError('')
    get<Course>('/api/mentor/course')
      .then((value) => { if (alive) setCourse(value) })
      .catch((e) => { if (alive) setLoadError((e as Error).message || '研究导师课程暂时无法读取') })
    return () => { alive = false }
  }, [disabled])

  const toggle = async (id: string, done: boolean) => {
    if (!course) return
    const completed = done ? [...course.completed, id] : course.completed.filter((x) => x !== id)
    setBusy(id)
    try {
      const next = await put<Course>('/api/mentor/progress', { completed })
      setCourse(next)
    } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }

  if (disabled) return (
    <Callout tone="warn" title="研究导师课程已关闭">
      当前工作区已关闭「研究导师课程」。课程进度不会丢失；工作区管理员可在设置 → 功能开关中重新开启。
      <div className="mentor-off-actions"><button className="button small" onClick={() => onNavigate('settings')}>打开功能开关</button></div>
    </Callout>
  )
  if (!course && loadError) return (
    <Callout tone="warn" title="研究导师课程暂时不可用">
      {loadError}
      <div className="mentor-off-actions"><button className="button small" onClick={() => onNavigate('settings')}>查看功能开关</button></div>
    </Callout>
  )
  if (!course) return <Loading label="读取研究导师课程" />
  return (
    <Panel title="研究导师课程" subtitle={course.principle}
      actions={<button className="button small" onClick={() => onNavigate('lab')}>进入 AI 研究室</button>}>
      <div className="mentor-progress"><i style={{ width: `${course.progress * 100}%` }} /><span>{Math.round(course.progress * 100)}% · {course.completed.length}/{course.total}</span></div>
      <div className="mentor-grid">
        {course.modules.map((m, i) => (
          <article key={m.id} className={`mentor-card${m.completed ? ' done' : ''}`}>
            <header>
              <button className="mentor-title" onClick={() => setOpen(open === m.id ? null : m.id)}>
                <i>{i + 1}</i><b>{m.title}</b>
              </button>
              <button type="button" role="checkbox" aria-checked={m.completed} aria-label={`标记完成：${m.title}`}
                className={`mentor-check${m.completed ? ' on' : ''}`} disabled={busy === m.id}
                onClick={() => toggle(m.id, !m.completed)}>{m.completed ? '✓' : ''}</button>
            </header>
            <p>{m.summary}</p>
            {open === m.id && (
              <div className="mentor-detail anim-expand">
                <div><b>检查项</b>{m.checks.map((x) => <Tag key={x}>{x}</Tag>)}</div>
                <p><b>练习：</b>{m.practice}</p>
                <p className="mentor-red"><b>红旗：</b>{m.red_flags.join('；')}</p>
              </div>
            )}
          </article>
        ))}
      </div>
      <Callout>
        这里的检查项和回测审查卡、Agent 系统提示使用同一套边界：样本外、成本、基准、多重检验、资金费与强平。
        完成课程不会自动晋级策略，但会帮助你解释一份回测为什么可能不可靠。
      </Callout>
    </Panel>
  )
}
