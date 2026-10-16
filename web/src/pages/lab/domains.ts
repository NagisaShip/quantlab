/** AI 研究室：各研究类型的文案、示例、快捷指令与研究库入口。策略之外的类型都是“多标的、有研究库”的研究。 */
import type { LabKind } from '../../apiLab'

export interface DomainUI {
  noun: string                 // 因子 / 模型 / 组合方案 / 执行方案
  welcome: string
  placeholder: string
  replyPlaceholder: string
  examples: string[]
  panelLabel: string
  evaluate: string             // 评估 / 训练
  libraryName: string          // 因子库 / 模型库
  saveVerb: string             // 入库 / 上线
  libraryField: 'factor_id' | 'model_id' | 'library_id'
  libraryRoute: (id: string) => string
  autoHint: string
  teamHint: string
}

export const DOMAIN_UI: Partial<Record<LabKind, DomainUI>> = {
  factor: {
    noun: '因子', welcome: '想研究什么因子？',
    placeholder: '描述一个市场现象或假设，也可以直接写表达式，例如 -ts_mean(funding, 9)',
    replyPlaceholder: '回复，或直接输入因子表达式…',
    examples: ['资金费率持续偏高的币，之后会不会跑输', '成交量放大但价格不涨，是不是出货信号', '恐惧贪婪指数极端时，高贝塔币的表现'],
    panelLabel: '因子', evaluate: '评估', libraryName: '因子库', saveVerb: '入库', libraryField: 'factor_id',
    libraryRoute: (id) => `factors/${id}`,
    autoHint: '开启后：提出假设 → 写表达式 → 评估 → 对照目标改进，最后问你是否入库',
    teamHint: '研究小组：数据工程师、因子研究员、研究审计员先讨论假设，再由 AI 实现与评估',
  },
  model: {
    noun: '模型', welcome: '想预测什么？',
    placeholder: '描述预测目标和想用的信息，例如：用资金费率和动量预测主流币未来 4 小时的相对强弱',
    replyPlaceholder: '回复，或粘贴一份 JSON 训练规格…',
    examples: ['用价量因子预测主流币下一根 K 线的截面排名', '预测 BTC 未来 24 小时波动率，用于仓位控制', '比较梯度提升树和 GRU 在涨跌方向上的表现'],
    panelLabel: '模型', evaluate: '训练', libraryName: '模型库', saveVerb: '上线', libraryField: 'model_id',
    libraryRoute: (id) => `modellib/${id}`,
    autoHint: '开启后：线性基线 → 树模型 → 神经网络 → 精简特征，每一版都和基线比、做泄漏检查，最后问你是否上线',
    teamHint: '模型小组：数据工程师定标签与切分、机器学习工程师设计模型、研究审计员查泄漏，再由 AI 训练与评估',
  },
  portfolio: {
    noun: '组合方案', welcome: '想怎么配置这篮子币？',
    placeholder: '描述配置目标与风控要求，例如：主流币风险平价，年化波动控制在 40% 以内，回撤超过 20% 降仓',
    replyPlaceholder: '回复，或粘贴一份 JSON 配置方案…',
    examples: ['主流十个币做风险平价，单币不超过 25%', '用模型预测做 Black-Litterman，并控制目标波动', '比较 HRP 和最小 CVaR 在 2022 年这类行情下的回撤'],
    panelLabel: '方案', evaluate: '评估', libraryName: '方案库', saveVerb: '启用', libraryField: 'library_id',
    libraryRoute: (id) => `portfolio/${id}`,
    autoHint: '开启后：风险平价 → HRP → 最小方差 → 加风控规则，每一版都和等权基准比、做压力测试，最后问你是否启用',
    teamHint: '组合小组：风险经理定约束、组合经理选优化器、研究审计员查过拟合，再由 AI 回测与压力测试',
  },
  execution: {
    noun: '执行方案', welcome: '想怎么把单子做进去？',
    placeholder: '描述执行目标，例如：BTC 和 ETH 每笔约 5% 日量，30 分钟内做完，比 TWAP 便宜，剧烈波动时放慢',
    replyPlaceholder: '回复，或粘贴一份 JSON 执行方案…',
    examples: ['大单 30 分钟内做完，成本比 TWAP 低', '流动性差的小币用冰山单，参与率不超过 15%', '比较 Almgren-Chriss 和自适应算法在剧烈波动时的表现'],
    panelLabel: '方案', evaluate: '仿真', libraryName: '执行方案库', saveVerb: '启用', libraryField: 'library_id',
    libraryRoute: (id) => `execution/${id}`,
    autoHint: '开启后：TWAP → 自适应 → VWAP → 被动挂单 → Almgren-Chriss，每一版都和 TWAP 与市价单比成本，最后问你是否启用。全程仿真，不会真实下单',
    teamHint: '执行小组：交易员定执行约束、算法工程师设计拆单、研究审计员查仿真偏差，再由 AI 回放仿真',
  },
}

/**
 * 研究类型滑块：五个研究室 + 两种研究流程。流程类（完整流程、调研报告）走策略对话，附加对应的工作方式。
 */
export type ResearchType = LabKind | 'pipeline' | 'report'
export const RESEARCH_TYPES: { id: ResearchType; label: string; hint: string }[] = [
  { id: 'strategy', label: '策略', hint: '写策略代码并回测' },
  { id: 'factor', label: '因子', hint: '挖掘因子，按 IC 与样本外评估' },
  { id: 'model', label: '模型', hint: '用因子训练预测模型' },
  { id: 'portfolio', label: '组合', hint: '把预测变成权重，控制风险' },
  { id: 'execution', label: '执行', hint: '拆单算法与成本研究（仿真）' },
  { id: 'pipeline', label: '完整流程', hint: '选标的 → 因子 → 策略 → 模型 → 组合 → 执行，一次走完并出报告' },
  { id: 'report', label: '调研报告', hint: '联网 + 平台数据调研，最后生成可导出 PDF 的报告' },
]
export const kindOfType = (t: ResearchType): LabKind => (t === 'pipeline' || t === 'report' ? 'strategy' : t)
export const workflowOfType = (t: ResearchType): '' | 'pipeline' | 'report' => (t === 'pipeline' || t === 'report' ? t : '')

/** 每种研究的建议（新对话页随机展示几条，可换一批）。 */
export const EXAMPLE_POOL: Record<ResearchType, string[]> = {
  strategy: [
    'BTC 1 小时，20 和 60 均线金叉做多，止损 5%',
    'ETH 4 小时 RSI 均值回归，回撤控制在 15% 以内',
    'BTC、ETH、SOL 每周动量轮动，只持有最强的一个',
    'BTC 4 小时唐奇安 55 根突破，ATR 两倍移动止损',
    '主流币资金费率极端时反向开仓，验证样本外是否有效',
    'SOL 15 分钟布林带收口后突破，成交量放大才入场',
    'BTC 日线定投 + 均线过滤，对比纯定投的回撤',
    '多币种横截面动量：做多前 3、空仓其余，周度调仓',
    'ETH/BTC 比值均值回归，比值偏离 2 倍标准差时切换持仓',
    '波动率目标仓位：BTC 年化波动控制在 40%',
  ],
  factor: [
    '资金费率持续偏高的币，之后会不会跑输',
    '成交量放大但价格不涨，是不是出货信号',
    '恐惧贪婪指数极端时，高贝塔币的表现',
    '20 日动量和 5 日反转能不能同时有效',
    '一次评估 10 个价量因子，并检查它们的共线性',
    '波动率偏度因子在主流币横截面上的 IC',
    '持仓量变化与价格背离作为因子',
  ],
  model: [
    '用价量因子预测主流币下一根 K 线的截面排名',
    '预测 BTC 未来 24 小时波动率，用于仓位控制',
    '比较梯度提升树和 GRU 在涨跌方向上的表现',
    '用 20 个因子训练 Lasso，看哪些因子被保留',
    '三重障碍标签 + 树模型，预测 4 小时方向',
  ],
  portfolio: [
    '主流十个币做风险平价，单币不超过 25%',
    '用模型预测做 Black-Litterman，并控制目标波动',
    '比较 HRP 和最小 CVaR 在 2022 年这类行情下的回撤',
    '动量组合加入相关性约束，避免同涨同跌',
  ],
  execution: [
    '大单 30 分钟内做完，成本比 TWAP 低',
    '流动性差的小币用冰山单，参与率不超过 15%',
    '比较 Almgren-Chriss 和自适应算法在剧烈波动时的表现',
  ],
  pipeline: [
    '从主流十个币里选标的，做一套多因子趋势策略，完整走到组合和执行',
    '围绕资金费率做一次完整研究：因子 → 策略 → 模型 → 组合',
    '为 BTC/ETH 做一套中频策略，从因子到执行全流程，并给出报告',
    '用 15 个候选因子做多因子选币，处理共线性后构建组合',
  ],
  report: [
    '调研比特币现货 ETF 资金流对 BTC 价格的影响',
    '调研最近一个月加密市场的主要事件与行情变化',
    '对比主流交易所的手续费与流动性，评估对中频策略的影响',
    '调研资金费率套利的原理、历史收益与主要风险',
    '调研以太坊质押收益率变化对 ETH 价格的影响',
    '整理“多因子共线性”的常见处理方法并给出建议',
  ],
}

/** 从建议池里随机取 n 条（换一批）。 */
export function pickExamples(t: ResearchType, n = 4, seed = Math.random()): string[] {
  const pool = [...(EXAMPLE_POOL[t] ?? EXAMPLE_POOL.strategy)]
  let s = Math.floor(seed * 2 ** 31) || 1
  for (let i = pool.length - 1; i > 0; i--) {
    s = (s * 48271) % 2147483647
    const j = s % (i + 1)
    ;[pool[i], pool[j]] = [pool[j], pool[i]]
  }
  return pool.slice(0, n)
}

export const domainUI = (kind?: LabKind): DomainUI | undefined => (kind && kind !== 'strategy' ? DOMAIN_UI[kind] : undefined)

export const libraryIdOf = (p: { factor_id?: string | null; model_id?: string | null; library_id?: string | null }, ui?: DomainUI) =>
  ui ? (p[ui.libraryField] || p.library_id || null) : null
