import type { OnboardingStep } from '../api/platform'
import type { BotGesture } from '../components/QuantBot'

export type TourStepId = Exclude<OnboardingStep, 'welcome'>

export interface TourStep {
  id: TourStepId
  title: string
  body: string
  page: 'home' | 'lab' | 'backtests' | 'backtest'
  target?: string
  gesture: BotGesture
  primary: string
}

export const DEFAULT_GOAL_PROMPT =
  '用 BTC/USDT 1 小时数据，研究 20/60 均线趋势策略；和买入持有比较，并检查样本外稳健性。不要使用未来数据，先给出回测和审查结论。'

export const TEAM_PROMPT =
  '/discuss 请对刚才的研究做一次上线前评审：市场逻辑、成本与执行、代码实现、过拟合风险分别给出结论，并汇总共识与分歧。'

const FLOW: TourStep = {
  id: 'flow',
  title: '先认识研究闭环',
  body: '数据 → 因子 / 模型 → 策略 → 回测 → 审查 → 验证与运行。每一步都留下可复核的记录。',
  page: 'home',
  target: 'home-flow',
  gesture: 'point-right',
  primary: '去 AI 研究室',
}

const GOAL: TourStep = {
  id: 'goal',
  title: '提出第一个研究目标',
  body: '我已经把示例填进输入框。你可以改成自己的问题；确认后点发送，我不会替你自动发送。',
  page: 'lab',
  target: 'lab-composer',
  gesture: 'point-left',
  primary: '我发送了，继续',
}

const AGENTS: TourStep = {
  id: 'agents',
  title: '看多 Agent 如何分工',
  body: '研究组长会把任务分给市场、成本执行、代码审计等角色。它们独立发言，再汇总共识、分歧和下一步行动。',
  page: 'lab',
  target: 'lab-composer',
  gesture: 'point-right',
  primary: '填入多 Agent 评审指令',
}

const BACKTEST: TourStep = {
  id: 'backtest',
  title: '回测不是只看收益',
  body: '先看基准、回撤、成本，再看 Review Gate 是否阻断晋级。收益曲线漂亮但审查不通过，结论仍然不可信。',
  page: 'backtests',
  target: 'backtest-new',
  gesture: 'think',
  primary: '我已看过审查',
}

const FINISH: TourStep = {
  id: 'finish',
  title: '第一条研究链路完成',
  body: '你可以保存这份研究、启动纸面模拟，或回到 AI 研究室继续追问。纸面模拟不会产生真实订单。',
  page: 'home',
  gesture: 'clap',
  primary: '完成引导',
}

export const OWNER_STEPS: TourStep[] = [FLOW, GOAL, AGENTS, BACKTEST, FINISH]
export const MEMBER_STEPS: TourStep[] = [GOAL, AGENTS, BACKTEST, FINISH]

export function stepsFor(track: 'owner' | 'member'): TourStep[] {
  return track === 'member' ? MEMBER_STEPS : OWNER_STEPS
}

export function nextStep(steps: TourStep[], id: TourStepId): TourStep | null {
  const i = steps.findIndex((s) => s.id === id)
  return i >= 0 && i + 1 < steps.length ? steps[i + 1] : null
}
