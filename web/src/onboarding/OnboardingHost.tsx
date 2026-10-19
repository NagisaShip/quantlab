import { useEffect, useRef, useState } from 'react'
import { v4, type Me, type OnboardingState } from '../api/platform'
import { v3 } from '../api/research'
import { collab } from '../apiCollab'
import { QuantBot, type BotGesture, type BotState } from '../components/QuantBot'
import { t } from '../i18n'
import OnboardingSpotlight from './OnboardingSpotlight'
import OnboardingTutor from './OnboardingTutor'
import { BUILTIN_GROUP_NAME, DEFAULT_GOAL_PROMPT, TEAM_PROMPT, nextStep, stepsFor, type TourStepId } from './steps'

export default function OnboardingHost({ me, page, go, notify, onMeChanged }: {
  me: Me
  page: string
  go: (key: string) => void
  notify: (message: string, error?: boolean) => void
  onMeChanged?: () => void | Promise<void>
}) {
  const fallbackOnboarding: OnboardingState = {
    version: 1,
    status: 'completed',
    current_step: 'finish',
    track: me.user.is_admin ? 'owner' : 'member',
    completed_steps: [],
    started_at: null,
    completed_at: null,
    updated_at: null,
  }
  const [state, setState] = useState<OnboardingState>(me.onboarding ?? fallbackOnboarding)
  const [busy, setBusy] = useState(false)
  const [targetMissing, setTargetMissing] = useState(false)
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 720px)').matches)
  const prefilled = useRef('')
  const steps = stepsFor(state.track)
  const welcome = state.status === 'not_started' || (state.status === 'in_progress' && state.current_step === 'welcome')
  const active = state.status === 'not_started' || state.status === 'in_progress'
  const rawIndex = state.current_step === 'welcome' ? 0 : steps.findIndex((s) => s.id === state.current_step)
  const index = rawIndex >= 0 ? rawIndex : 0
  const step = steps[index]
  // 策略广场的分享详情是独立路由（post/...），此时不再把高亮落在侧栏“策略广场”上。
  // 回测详情仍保留 backtest-review 锚点，直接指向审查卡。
  const rawTarget = step?.id === 'square' && page === 'post'
    ? undefined
    : step?.id === 'backtest' && page === 'backtest' ? 'backtest-review' : step?.target
  // 手机端侧栏收进抽屉，导航项不可见；把高亮落到当前页的页头，保证每一步都有明确落点。
  const target = mobile && rawTarget?.startsWith('nav-') ? `page-${step?.page ?? page}` : rawTarget

  useEffect(() => { if (me.onboarding) setState(me.onboarding) }, [me.onboarding])
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 720px)')
    const sync = () => setMobile(mq.matches)
    sync()
    mq.addEventListener('change', sync)
    return () => mq.removeEventListener('change', sync)
  }, [])
  useEffect(() => {
    document.body.classList.toggle('onboarding-active', active)
    return () => document.body.classList.remove('onboarding-active')
  }, [active])

  useEffect(() => {
    if (!target || welcome) { setTargetMissing(false); return }
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

  const configureModels = async () => {
    const next = await save({ status: 'skipped' })
    if (next.status !== 'skipped') return
    // 模型配置页属于设置分支，会重挂载引导宿主；先刷新 me.onboarding，
    // 避免它用登录时的旧状态把欢迎层重新显示出来。
    await onMeChanged?.()
    go('models')
    notify('已打开模型 API 配置页；配置完成后可在首页继续新手任务')
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
    // 打开报告只表示开始阅读；由「我已看过审查」确认完成，避免自动前进与点击竞争。
    const onRestart = () => { restart() }
    window.addEventListener('qa:onboarding:goal-sent', onGoal)
    window.addEventListener('qa:onboarding:team-started', onTeam)
    window.addEventListener('qa:onboarding:restart', onRestart)
    return () => {
      window.removeEventListener('qa:onboarding:goal-sent', onGoal)
      window.removeEventListener('qa:onboarding:team-started', onTeam)
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
          <p>接下来几分钟，我们一起跑通第一条研究链路，并快速走完全页面链路：提出目标、让 Agent 分工、回测、审查、数据、因子、模型、组合、验证与运行。</p>
          <div className="onboarding-welcome-actions">
            <button type="button" className="button primary" onClick={begin} disabled={busy}>{busy ? '准备中…' : '开始全链路引导'}</button>
            <button type="button" className="button ghost" onClick={configureModels} disabled={busy}>去配置模型 Key，获得更佳体验</button>
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
    if (step.id === 'agents') {
      window.dispatchEvent(new CustomEvent('qa:onboarding:prefill', { detail: { text: t(TEAM_PROMPT) } }))
      notify('已填入多 Agent 评审指令，点击发送即可')
      return undefined
    }
    return advance(step.id)
  }

  const openDetail = async () => {
    if (!step.detail) return
    if (step.detail.route) { go(step.detail.route); return }
    setBusy(true)
    try {
      if (step.id === 'backtest') {
        const r = await v3.backtests()
        const row = r.backtests.find((x) => x.slug === 'builtin-ma-cross')
          ?? r.backtests.find((x) => x.strategy_name.includes('内置'))
        if (!row) { notify('暂时没有内置回测，请先完成一次回测', true); return }
        go(`backtest/${row.id}`)
      } else if (step.id === 'square') {
        // 旧库里的种子帖可能是随机 id；先按稳定 id 取，失败再按标题 / 类型回退。
        let postId = ''
        try {
          postId = (await v3.post('seed-momentum-rotation')).post.id
        } catch {
          const r = await v3.posts({ q: '多币种动量轮动', size: 10 })
          const post = r.items.find((x) => x.title.includes('多币种动量轮动'))
            ?? r.items.find((x) => x.type === 'strategy')
          if (post) postId = post.id
        }
        if (!postId) { notify('暂时没有找到内置策略帖', true); return }
        go(`post/${postId}`)
      } else if (step.id === 'groups') {
        const r = await collab.groups()
        const g = r.groups.find((x) => x.name === BUILTIN_GROUP_NAME)
        if (!g) { notify('暂时没有找到内置群组示例', true); return }
        if (!g.my_role) await collab.join(g.id)
        go(`groups/${g.id}/tasks`)
      }
    } catch (e) {
      notify((e as Error).message, true)
    } finally { setBusy(false) }
  }

  const secondary = step.id === 'goal' ? '跳过 AI，去回测'
    : step.id === 'agents' ? '我已了解'
      : step.detail?.label
  const onSecondary = step.id === 'goal' ? skipToBacktest
    : step.id === 'agents' ? () => advance('agents')
      : step.detail ? openDetail
        : undefined

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
        secondary={secondary}
        onSecondary={onSecondary}
        onPrimary={primary}
      />
    </>
  )
}
