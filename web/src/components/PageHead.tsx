import { useEffect, useState } from 'react'
import { v3 } from '../api/research'
import { validation } from '../apiValidation'
import { store } from './prefs'

/** 关掉的「下一步」提示：记住它的文字，下一步变了才重新出现 */
const DISMISS_KEY = 'qa.nextstep.dismissed'

type Step = { text: string; go: string }

/** 全平台最该做的一件事：晋级 > 批准 > 配置 AI > 写第一个策略。当前页就是去处时不显示。 */
function useNextStep(page: string): Step | null {
  const [step, setStep] = useState<Step | null>(null)
  useEffect(() => {
    let alive = true
    Promise.all([
      validation.board().catch(() => null),
      v3.strategies().catch(() => null),
      v3.llmStatus().catch(() => null),
    ]).then(([board, strats, llm]) => {
      if (!alive) return
      const promote = (board?.board ?? []).find((r) => r.next && r.passed)
      const pending = (strats?.strategies ?? []).find((s) => !s.human_approved && s.status !== 'draft')
      const steps: Step[] = [
        ...(promote ? [{ text: `「${promote.name}」可以晋级到${promote.next_label}`, go: `validation/gate:${promote.strategy}` }] : []),
        ...(pending ? [{ text: `审阅「${pending.name}」的回测，决定是否批准`, go: `editor/${pending.slug}` }] : []),
        ...(llm && !llm.ready ? [{ text: '配置一个 AI 模型', go: 'models' }] : []),
        ...(strats && !strats.strategies.length ? [{ text: '写第一个策略', go: 'strategies' }] : []),
      ]
      setStep(steps.find((s) => s.go.split('/')[0] !== page) ?? null)
    })
    return () => { alive = false }
  }, [page])
  return step
}

/** 统一页头：标题 + 一句话说明 + 右侧操作；下一行是「下一步」提示。 */
export function PageHead({ title, purpose, page, onSlot, onNavigate }: {
  title: string
  purpose?: string
  page: string
  onSlot: (el: HTMLElement | null) => void
  onNavigate: (key: string) => void
}) {
  const next = useNextStep(page)
  const [dismissed, setDismissed] = useState(() => store.get(DISMISS_KEY, ''))
  return (
    <header className="page-head" data-tour={`page-${page}`}>
      <div className="page-head-main">
        <h1>{title}</h1>
        {purpose && <p>{purpose}</p>}
      </div>
      <div className="page-head-actions" ref={onSlot} />
      {next && dismissed !== next.text && (
        <span className="next-step-wrap anim-fade">
          <button className="next-step" onClick={() => onNavigate(next.go)}>
            <span className="next-step-k">下一步</span>{next.text}<span aria-hidden="true">→</span>
          </button>
          <button type="button" className="next-step-x" aria-label="不再提示这一步" title="不再提示这一步"
            onClick={() => { setDismissed(next.text); store.set(DISMISS_KEY, next.text) }}>×</button>
        </span>
      )}
    </header>
  )
}
