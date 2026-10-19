import { useEffect, useRef, useState } from 'react'
import { v4, type Me, type OnboardingState } from '../api/platform'
import { QuantBot, type BotGesture, type BotState } from '../components/QuantBot'
import { t } from '../i18n'
import OnboardingSpotlight from './OnboardingSpotlight'
import OnboardingTutor from './OnboardingTutor'
import { DEFAULT_GOAL_PROMPT, TEAM_PROMPT, nextStep, stepsFor, type TourStepId } from './steps'

export default function OnboardingHost({ me, page, go, notify }: {
  me: Me
  page: string
  go: (key: string) => void
  notify: (message: string, error?: boolean) => void
}) {
  const [state, setState] = useState<OnboardingState>(me.onboarding)
  const [busy, setBusy] = useState(false)
  const [targetMissing, setTargetMissing] = useState(false)
  const prefilled = useRef('')
  const steps = stepsFor(state.track)
  const welcome = state.status === 'not_started' || (state.status === 'in_progress' && state.current_step === 'welcome')
  const active = state.status === 'not_started' || state.status === 'in_progress'
  const rawIndex = state.current_step === 'welcome' ? 0 : steps.findIndex((s) => s.id === state.current_step)
  const index = rawIndex >= 0 ? rawIndex : 0
  const step = steps[index]
  const target = step?.id === 'backtest' && page === 'backtest' ? 'backtest-review' : step?.target

  useEffect(() => { setState(me.onboarding) }, [me.onboarding])
  useEffect(() => {
    document.body.classList.toggle('onboarding-active', active)
    return () => document.body.classList.remove('onboarding-active')
  }, [active])

  useEffect(() => {
    if (!step?.target || welcome) { setTargetMissing(false); return }
    const timer = window.setTimeout(() => {
      setTargetMissing(!document.querySelector(`[data-tour="${target}"]`))
    }, 700)
    return () => window.clearTimeout(timer)
  }, [step?.id, target, page, welcome])

  useEffect(() => {
    if (!active || welcome || !step || step.id !== 'goal' || page !== 'lab') return
    if (prefilled.current === 'goal') return
    prefilled.current = 'goal'
    const timer = window.setTimeout(() => {
      window.dispatchEvent(new CustomEvent('qa:onboarding:prefill', { detail: { text: t(DEFAULT_GOAL_PROMPT) } }))
    }, 450)
    return () => window.clearTimeout(timer)
  }, [active, welcome, step?.id, page])

  const save = async (patch: Parameters<typeof v4.updateOnboarding>[0]) => {
    setBusy(true)
    try {
      const r = await v4.updateOnboarding(patch)
      setState(r.onboarding)
      return r.onboarding
    } catch (e) {
      notify((e as Error).message, true)
      return state
    } finally { setBusy(false) }
  }

  const advance = async (id: TourStepId) => {
    const next = nextStep(steps, id)
    if (!next) {
      await save({ completed_step: id, current_step: 'finish', status: 'completed' })
      notify('新手引导完成，之后可以在账号菜单重新打开')
      return
    }
    prefilled.current = ''
    await save({ completed_step: id, current_step: next.id, status: 'in_progress' })
    go(next.page)
  }

  const begin = async () => {
    const first = steps[0]
    await save({ current_step: first.id, status: 'in_progress' })
    go(first.page)
    if (first.id === 'goal') prefilled.current = ''
  }

  const skipToBacktest = async () => {
    await save({ completed_step: 'goal', current_step: 'backtest', status: 'in_progress' })
    prefilled.current = ''
    go('backtests')
  }

  const skip = async () => {
    await save({ status: 'skipped' })
    notify('已收起新手引导，可在账号菜单重新打开')
  }

  const restart = async () => {
    setBusy(true)
    try {
      const r = await v4.resetOnboarding()
      setState(r.onboarding)
      prefilled.current = ''
      go('home')
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }

  useEffect(() => {
    const onGoal = () => { if (state.status === 'in_progress' && state.current_step === 'goal') advance('goal') }
    const onTeam = () => { if (state.status === 'in_progress' && state.current_step === 'agents') advance('agents') }
    const onReview = () => { if (state.status === 'in_progress' && state.current_step === 'backtest') advance('backtest') }
    const onRestart = () => { restart() }
    window.addEventListener('qa:onboarding:goal-sent', onGoal)
    window.addEventListener('qa:onboarding:team-started', onTeam)
    window.addEventListener('qa:onboarding:review-opened', onReview)
    window.addEventListener('qa:onboarding:restart', onRestart)
    return () => {
      window.removeEventListener('qa:onboarding:goal-sent', onGoal)
      window.removeEventListener('qa:onboarding:team-started', onTeam)
      window.removeEventListener('qa:onboarding:review-opened', onReview)
      window.removeEventListener('qa:onboarding:restart', onRestart)
    }
  }) // 依赖在闭包里读取最新 state；每次渲染重绑，成本很低

  if (!active && state.status !== 'skipped') return null

  if (!active && state.status === 'skipped') {
    if (page !== 'home') return null
    return (
      <aside className="onboarding-resume anim-rise">
        <QuantBot size={42} state="idle" hands gesture="wave" motion="low" />
        <div><b>继续新手任务</b><small>用 3 分钟跑通第一条研究链路</small></div>
        <button type="button" className="button small primary" onClick={restart} disabled={busy}>继续</button>
        <button type="button" className="onboarding-resume-x" aria-label="关闭提示" onClick={() => save({ status: 'completed' })}>×</button>
      </aside>
    )
  }

  if (welcome) {
    return (
      <div className="onboarding-welcome-backdrop">
        <section className="onboarding-welcome-card" role="dialog" aria-modal="true" aria-labelledby="onboarding-welcome-title">
          <QuantBot size={96} state="happy" hands gesture="wave" entrance motion="full" />
          <div className="onboarding-kicker">QuantLab Bot · 首次见面</div>
          <h2 id="onboarding-welcome-title">你好，我是 QuantLab Bot</h2>
          <p>接下来 3 分钟，我们一起跑通第一条研究链路：提出目标、让 Agent 分工、回测、审查，最后得到一个可复核的结论。</p>
          <ul>
            <li>真实界面操作，不是看一组说明页</li>
            <li>没有模型 Key 也能先用模板完成回测</li>
            <li>不会自动发送消息，也不会开启真实资金通道</li>
          </ul>
          <div className="onboarding-welcome-actions">
            <button type="button" className="button primary" onClick={begin} disabled={busy}>{busy ? '准备中…' : '开始 3 分钟引导'}</button>
            <button type="button" className="button ghost" onClick={skip} disabled={busy}>先自己看看</button>
          </div>
        </section>
      </div>
    )
  }

  if (!step) return null
  const botState: BotState = step.id === 'agents' || step.id === 'backtest' ? 'focus' : step.id === 'finish' ? 'happy' : 'idle'
  const gesture = step.gesture as BotGesture
  const primary = () => {
    if (step.id === 'flow') return advance('flow')
    if (step.id === 'goal') return advance('goal')
    if (step.id === 'agents') {
      window.dispatchEvent(new CustomEvent('qa:onboarding:prefill', { detail: { text: t(TEAM_PROMPT) } }))
      notify('已填入多 Agent 评审指令，点击发送即可')
      return undefined
    }
    if (step.id === 'backtest') return advance('backtest')
    return save({ completed_step: 'finish', current_step: 'finish', status: 'completed' })
  }

  return (
    <>
      <OnboardingSpotlight target={welcome ? undefined : target} />
      <OnboardingTutor
        step={step}
        index={index}
        total={steps.length}
        state={botState}
        gesture={gesture}
        entrance
        busy={busy}
        missingTarget={targetMissing}
        onPrev={() => { const prev = steps[index - 1]; if (prev) { prefilled.current = ''; save({ current_step: prev.id, status: 'in_progress' }); go(prev.page) } }}
        onSkip={skip}
        secondary={step.id === 'goal' ? '跳过 AI，去回测' : step.id === 'agents' ? '我已了解' : step.id === 'backtest' ? '打开回测页' : undefined}
        onSecondary={step.id === 'goal' ? skipToBacktest : step.id === 'agents' ? () => advance('agents') : step.id === 'backtest' ? () => go('backtests') : undefined}
        onPrimary={primary}
      />
    </>
  )
}
