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

export const domainUI = (kind?: LabKind): DomainUI | undefined => (kind && kind !== 'strategy' ? DOMAIN_UI[kind] : undefined)

export const libraryIdOf = (p: { factor_id?: string | null; model_id?: string | null; library_id?: string | null }, ui?: DomainUI) =>
  ui ? (p[ui.libraryField] || p.library_id || null) : null
