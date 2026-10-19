import { useMemo, useState } from 'react'
import { QuantBot, type BotGesture, type BotState } from '../components/QuantBot'
import type { TourStep } from './steps'

const TIPS = [
  '我在这里。高亮区域就是当前要操作的位置。',
  '不确定时可以先点「跳过引导」，之后在账号菜单里重新打开。',
  '我不会替你发送消息，也不会自动开启任何真实资金通道。',
  '完成一步后，我会继续带你往下走。',
]

export default function OnboardingTutor({
  step,
  index,
  total,
  state,
  gesture,
  entrance,
  busy,
  missingTarget,
  onPrev,
  onSkip,
  onPrimary,
  secondary,
  onSecondary,
}: {
  step: TourStep
  index: number
  total: number
  state: BotState
  gesture: BotGesture
  entrance?: boolean
  busy?: boolean
  missingTarget?: boolean
  onPrev: () => void
  onSkip: () => void
  onPrimary: () => void
  secondary?: string
  onSecondary?: () => void
}) {
  const [tip, setTip] = useState('')
  const tipText = useMemo(() => tip || (missingTarget ? '这一步的目标区域暂时没找到。可以直接跳过，或到对应页面后再继续。' : ''), [tip, missingTarget])

  return (
    <aside className={`onboarding-tutor anim-rise${missingTarget ? ' missing' : ''}`} aria-live="polite" aria-label="QuantLab Bot 新手导师">
      <div className="onboarding-tutor-main">
        <QuantBot
          size={64}
          state={state}
          gesture={gesture}
          hands
          interactive
          entrance={entrance}
          className="onboarding-bot"
          title="QuantLab Bot 新手导师"
          onActivate={() => setTip(TIPS[Math.floor(Math.random() * TIPS.length)])}
        />
        <div className="onboarding-bubble">
          <div className="onboarding-kicker">QuantLab Bot · {index + 1}/{total}</div>
          <h3>{step.title}</h3>
          <p>{step.body}</p>
          {tipText && <div className="onboarding-tip">{tipText}</div>}
          <div className="onboarding-progress" aria-hidden="true">
            {Array.from({ length: total }, (_, i) => <i key={i} className={i <= index ? 'on' : ''} />)}
          </div>
          <div className="onboarding-actions">
            <button type="button" className="button small ghost" onClick={onSkip} disabled={busy}>跳过引导</button>
            {index > 0 && <button type="button" className="button small" onClick={onPrev} disabled={busy}>上一步</button>}
            {secondary && <button type="button" className="button small" onClick={onSecondary} disabled={busy}>{secondary}</button>}
            <button type="button" className="button small primary" onClick={onPrimary} disabled={busy}>{busy ? '保存中…' : step.primary}</button>
          </div>
        </div>
      </div>
    </aside>
  )
}
