import type { OnboardingStep } from '../api/platform'
import type { BotGesture } from '../components/QuantBot'

export type TourStepId = Exclude<OnboardingStep, 'welcome'>

export interface TourStep {
  id: TourStepId
  title: string
  body: string
  page: 'home' | 'lab' | 'backtests' | 'backtest' | 'data' | 'factors' | 'modellib' | 'portfolio' | 'validation' | 'paper' | 'square' | 'groups'
  target?: string
  gesture: BotGesture
  primary: string
  /** 打开平台内置内容：route 为空时由 OnboardingHost 动态解析（回测 / 群组）。 */
  detail?: { label: string; route?: string }
}

/** 与后端 collab_seed.BUILTIN_GROUP_NAME 保持一致。 */
export const BUILTIN_GROUP_NAME = '平台内置 · 研究协作示例'

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
  gesture: 'think',
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
  detail: { label: '看内置回测' },
}

const DATA: TourStep = {
  id: 'data',
  title: '行情与数据',
  body: '先看数据源、缺口、未收盘与覆盖范围。没有真实数据时，平台会明确显示「未接入数据」，不会用合成数据冒充。',
  page: 'data',
  target: 'nav-data',
  gesture: 'point-right',
  primary: '下一站：因子',
}

const FACTORS: TourStep = {
  id: 'factors',
  title: '因子研究',
  body: '用表达式提出市场假设，检查 IC、分层、共线性与样本外；失败版本也会进入试验台账。',
  page: 'factors',
  target: 'nav-factors',
  gesture: 'point-right',
  primary: '下一站：预测模型',
  detail: { label: '看内置因子', route: 'factors/builtin_factor_reversal_5' },
}

const MODEL: TourStep = {
  id: 'modellib',
  title: '预测模型',
  body: '训练模型并与线性基线比较，检查泄漏、滚动样本外和衰减重训；通过检查的模型才能在策略里调用。',
  page: 'modellib',
  target: 'nav-modellib',
  gesture: 'point-right',
  primary: '下一站：组合与执行',
  detail: { label: '看内置模型', route: 'modellib/builtin_model_ridge_multi' },
}

const PORTFOLIO: TourStep = {
  id: 'portfolio',
  title: '组合与执行',
  body: '把信号变成权重，再把大单拆成子单。风险分解、压力测试、盘口回放和 TCA 都在同一条链路上。',
  page: 'portfolio',
  target: 'nav-portfolio',
  gesture: 'point-right',
  primary: '下一站：上线验证',
  detail: { label: '看内置组合', route: 'portfolio/builtin_portfolio_risk_parity' },
}

const VALIDATION: TourStep = {
  id: 'validation',
  title: '上线验证',
  body: '回测、纸面模拟和模拟盘逐关确认；高严重度审查会阻断晋级。每一关都由你确认，不自动放行。',
  page: 'validation',
  target: 'nav-validation',
  gesture: 'point-right',
  primary: '下一站：纸面模拟',
  detail: { label: '看内置验证', route: 'validation/gate:builtin-ma-cross' },
}

const PAPER: TourStep = {
  id: 'paper',
  title: '纸面模拟',
  body: '用真实行情驱动虚拟账户，每根新 bar 调用一次策略并写检查点；不会产生任何真实订单。',
  page: 'paper',
  target: 'nav-paper',
  gesture: 'point-right',
  primary: '下一站：协作',
}

const SQUARE: TourStep = {
  id: 'square',
  title: '协作与复用',
  body: '策略广场把研究变成团队可复用的资产；分享详情会展示冻结版本、回测清单与讨论。',
  page: 'square',
  target: 'nav-square',
  gesture: 'point-right',
  primary: '下一站：群组与任务',
  detail: { label: '看内置策略帖' },
}

const GROUPS: TourStep = {
  id: 'groups',
  title: '群组与任务',
  body: '群组把讨论、@ 成员、引用回测和任务看板放在一起；任务有负责人、优先级和验收状态，下一步不再散落在聊天里。',
  page: 'groups',
  target: 'nav-groups',
  gesture: 'point-right',
  primary: '完成链路',
  detail: { label: '看内置群组' },
}

const FINISH: TourStep = {
  id: 'finish',
  title: '第一条研究链路完成',
  body: '你可以保存这份研究、启动纸面模拟，或回到 AI 研究室继续追问。纸面模拟不会产生真实订单。',
  page: 'home',
  gesture: 'clap',
  primary: '完成引导',
}

export const OWNER_STEPS: TourStep[] = [FLOW, GOAL, AGENTS, BACKTEST, DATA, FACTORS, MODEL, PORTFOLIO, VALIDATION, PAPER, SQUARE, GROUPS, FINISH]
export const MEMBER_STEPS: TourStep[] = OWNER_STEPS

export function stepsFor(_track: 'owner' | 'member'): TourStep[] {
  return OWNER_STEPS
}

export function nextStep(steps: TourStep[], id: TourStepId): TourStep | null {
  const i = steps.findIndex((s) => s.id === id)
  return i >= 0 && i + 1 < steps.length ? steps[i + 1] : null
}
