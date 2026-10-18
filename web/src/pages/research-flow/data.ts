import type { LabKind } from '../../apiLab'

export interface FlowItem {
  label: string
  note: string
  route?: string
  ai?: string
  tag?: string
  feature?: string
}

export interface FlowStage {
  no: string
  title: string
  desc: string
  outcome: string
  glyph: string
  labKind: LabKind
  ai: string
  wide?: boolean
  items: FlowItem[]
}

export const STAGES: FlowStage[] = [
  {
    no: '01',
    title: '数据与市场',
    desc: '先锁定交易所、标的池、周期、时点和覆盖范围，再讨论任何信号。',
    outcome: '可交易数据集 + 质量报告',
    glyph: 'data',
    labKind: 'factor',
    ai: '/data 研究数据：先检查交易所、标的池、周期、覆盖范围、时点可用性和数据质量，再给出下一步研究计划',
    items: [
      { label: '行情 K 线', note: '100ms–1d，多周期与盘口读数', route: 'data', tag: '行情' },
      { label: '研究数据目录', note: '衍生品、链上、情绪、宏观与覆盖范围', route: 'data/hub', tag: '目录' },
      { label: '数据质量', note: '缺口 / 重复 / 未收盘 / 覆盖逐项核验', route: 'data', tag: '核验' },
      { label: '本地缓存与补缺', note: '本地优先，只补缺失头尾与中间缺口', route: 'data/hub' },
      { label: '研究导师 · 数据时点', note: '理解 point-in-time、未来数据与样本起点', route: 'mentor', feature: 'research.mentor', tag: '学习' },
    ],
  },
  {
    no: '02',
    title: '假设与因子',
    desc: '把市场假设写成可计算表达式，先做 IC、分组、共线性和失败记录。',
    outcome: '可检验的因子与信号',
    glyph: 'factor',
    labKind: 'factor',
    ai: '/factor 从研究假设开始：提出可证伪的因子，做 IC、分组、共线性与样本外评估，并记录失败版本',
    items: [
      { label: '因子库', note: '表达式、评估、入库与版本', route: 'factors', tag: '研究' },
      { label: '新因子研究', note: '让 AI 提出假设并评估', route: 'factors/new', tag: 'AI' },
      { label: '试验记录', note: '失败版本、试验次数与入库检查', route: 'factors/trials' },
      { label: '表达式语法', note: '算子、窗口与因子写法速查', route: 'factors/syntax' },
      { label: '共线性诊断', note: '相关、VIF、条件数与聚类', ai: '/factor_correlation' },
      { label: '研究导师 · 信号因果', note: '避免把相关性误写成因果', route: 'mentor', feature: 'research.mentor' },
    ],
  },
  {
    no: '03',
    title: '策略构建',
    desc: '规则、代码、积木或外部策略都可以，但版本、参数和成交假设必须固定。',
    outcome: '冻结的策略版本',
    glyph: 'strategy',
    labKind: 'strategy',
    ai: '/strategy 基于已有因子和数据检查结果，写一版可复现策略，明确入场、出场、仓位、成本和失效条件',
    items: [
      { label: '我的策略', note: '代码、规则、版本与审批', route: 'strategies', tag: '核心' },
      { label: '积木搭建', note: '用因子 / 模型 / 组合方案拼策略', route: 'assemble' },
      { label: '参数优化', note: '样本内 / 样本外网格与贝叶斯', route: 'backtests', tag: '优化' },
      { label: '.qstrat 导入导出', note: '代码、数据、标准答案与研究包清单', route: 'strategies', feature: 'package.qstrat_perp', tag: '包' },
      { label: 'Freqtrade 导入', note: '粘贴 IStrategy，自动体检并转换', route: 'strategies', feature: 'import.freqtrade_short', tag: '导入' },
      { label: '策略血缘', note: '版本链、克隆分支、编辑者与回测', route: 'lineage' },
      { label: '版本与人工批准', note: 'DRAFT → VALIDATED → FROZEN', route: 'strategies', tag: '治理' },
    ],
  },
  {
    no: '04',
    title: '预测模型',
    desc: '用因子训练模型，和线性基线比，检查泄漏、滚动样本外和衰减重训。',
    outcome: '可解释的预测模型',
    glyph: 'model',
    labKind: 'model',
    ai: '/model 设计标签、特征、切分和基线，训练模型并检查泄漏、样本外、校准与衰减重训条件',
    items: [
      { label: '模型库', note: '样本外、版本与重训计划', route: 'modellib', tag: '模型' },
      { label: '训练新模型', note: '选特征、标签、切分与基线', route: 'modellib/train' },
      { label: '训练记录', note: '历次训练、诊断指标与样本外', route: 'modellib/runs' },
      { label: '模型评审', note: '让 AI 查泄漏、过拟合与稳定性', ai: '/discuss 模型评审：检查泄漏、样本外和过拟合' },
      { label: '衰减与重训', note: '模型衰减、重训计划与上线条件', route: 'validation/decay' },
    ],
  },
  {
    no: '05',
    title: '组合与执行',
    desc: '把信号变成仓位，再研究大单如何拆、冲击和成本如何压。',
    outcome: '组合权重 + 执行方案',
    glyph: 'execution',
    labKind: 'portfolio',
    ai: '/portfolio 把可用信号组合成仓位，比较风险平价、HRP 和均方差，再做执行成本、容量与压力测试',
    wide: true,
    items: [
      { label: '组合与风控', note: '权重、风险约束与组合回测', route: 'portfolio', tag: '组合' },
      { label: '组合设计', note: '风险平价、HRP、均方差与约束', route: 'portfolio/design' },
      { label: '组合风险', note: '相关性、VaR、压力与归因', route: 'portfolio/risk' },
      { label: '下单执行', note: 'TWAP / VWAP / POV / 冰山 / AC', route: 'execution', tag: '执行' },
      { label: '执行回放', note: '盘口逐步回放与成交偏差', route: 'execution/replay' },
      { label: '成本与容量', note: 'TCA、冲击曲线、容量与盘口录制', route: 'execution/costs', tag: 'TCA' },
      { label: '盘口录制', note: '把执行研究需要的盘口保存为可复现数据', route: 'execution/record' },
    ],
  },
  {
    no: '06',
    title: '验证与可复现',
    desc: '不是只看一条收益曲线：基准、样本外、成本、偏差、审查和重放必须同时成立。',
    outcome: '可审计的验证报告',
    glyph: 'verify',
    labKind: 'strategy',
    ai: '/overfit 对当前策略做上线前验证：双基准、样本外、walk-forward、成本压力、参数敏感性、审查卡和研究包重放',
    wide: true,
    items: [
      { label: '回测与对比', note: '净值、回撤、成本与多版本 Diff', route: 'backtests', tag: '验证' },
      { label: '策略对比', note: '同一冻结条件下并排比较', route: 'backtests/compare' },
      { label: '历史实验', note: '回测试验与参数组合检索', route: 'backtests/history' },
      { label: 'Research Review Gate', note: '自动审查前视、成本、过拟合与证据；高严重度阻断晋级', route: 'backtests', tag: '审查' },
      { label: '双基准', note: '策略 / 买入持有 / SMA 简单规则三条曲线', route: 'backtests', tag: '基准' },
      { label: '账本 v2 合约模式', note: '线性 / 反向 / 交割，逐仓 / 全仓，单向 / 对冲', route: 'backtests', tag: '账本' },
      { label: '过拟合压力测试', note: '滚动分段 + 样本外 + 参数 + 成本', ai: '/overfit', tag: 'AI' },
      { label: '参数热力图', note: '二维参数平原 / 孤岛尖峰', ai: '/heatmap fast slow', tag: 'AI' },
      { label: '分段回测', note: '把区间切成几段，检查是否只在某段有效', ai: '/walkforward 4' },
      { label: '策略体检', note: '未来函数、过拟合、成本敏感度', route: 'validation/checkup' },
      { label: '仿真真实度', note: '回测 vs 纸面 vs 模拟盘拆解', route: 'validation/fidelity' },
      { label: '压力演练', note: '断线、拒单、延迟、部分成交', route: 'validation/stress' },
      { label: '衰减与重训', note: '策略 / 模型衰减与重训计划', route: 'validation/decay' },
      { label: '试验台账', note: '记录每次试验，修正多重比较', route: 'discipline' },
      { label: '研究包导出', note: '代码、数据、资金费、提示词、结果与审查报告', route: 'backtests', tag: '复现' },
      { label: '研究包校验与重放', note: '导入后重跑并比对代码 / 数据 / 参数哈希', route: 'backtests', tag: '重放' },
      { label: '合约账单核验', note: '用交易所账单核验 MMR、资金费、标记价与强平', route: 'demo', tag: '核验' },
      { label: '晋级关卡', note: '逐道关卡与人工确认', route: 'validation' },
    ],
  },
  {
    no: '07',
    title: '运行与复盘',
    desc: '纸面、模拟盘或真实永续，都要经过审批、止损、限额和逐笔归因。',
    outcome: '运行记录 + 偏差归因',
    glyph: 'run',
    labKind: 'execution',
    ai: '/report 设计上线运行方案：纸面 / 模拟盘 / 真实永续的审批、止损、限额、急停、对账和复盘指标',
    items: [
      { label: '纸面模拟', note: '实时行情、平台撮合与检查点恢复', route: 'paper', tag: '运行' },
      { label: '交易所模拟盘', note: '模拟账户、真实撮合与对账', route: 'demo' },
      { label: '微实盘', note: '小资金、人工批准、交易所止损与急停', route: 'microlive', feature: 'execution.real_spot' },
      { label: '真实永续', note: '独立开关、确认码、杠杆 / 名义 / 日亏限制与自动急停', route: 'realperp', feature: 'execution.real_perp', tag: '真实资金' },
      { label: '实盘对照', note: '真实成交与模拟结果逐笔归因', route: 'live' },
      { label: '只读账户', note: '只读 Key 读取余额与成交', route: 'accounts' },
      { label: '一键急停与平仓', note: 'KILL 文件、工作区急停和交易所止损', route: 'realperp' },
      { label: '研究导师 · 永续风险', note: '杠杆、保证金、资金费与强平红旗', route: 'mentor', feature: 'research.mentor' },
    ],
  },
]

export const TOOL_GROUPS = [
  { id: 'quality', label: '研究保障', hint: '审查、核验、导师与可复现' },
  { id: 'package', label: '导入导出', hint: '策略包、研究包与外部策略' },
  { id: 'collab', label: '协作复用', hint: '团队评审、广场与血缘' },
  { id: 'config', label: '研究配置', hint: '角色、提示词、模型服务与接口' },
  { id: 'runtime', label: '运行与真实资金', hint: '纸面、模拟、微实盘与永续' },
] as const

export type ToolGroup = (typeof TOOL_GROUPS)[number]['id']

export interface ToolItem extends FlowItem { group: ToolGroup }

export const TOOLS: ToolItem[] = [
  { label: '研究导师课程', note: '七课检查项、练习与红旗，和审查卡共用边界', route: 'mentor', feature: 'research.mentor', tag: '学习', group: 'quality' },
  { label: '合约账单核验', note: '用交易所账户账单核对 MMR、资金费、标记价与强平', route: 'demo', tag: '核验', group: 'quality' },
  { label: '研究包校验与重放', note: '把结果交给别人也能重跑并比对哈希', route: 'backtests', tag: '复现', group: 'quality' },
  { label: '策略体检与红队', note: '未来函数、成本翻倍、参数平台和多角色质疑', route: 'validation/checkup', group: 'quality' },
  { label: '.qstrat 策略包', note: '线性 / 反向 / 交割策略包，导入前隔离试跑', route: 'strategies', feature: 'package.qstrat_perp', group: 'package' },
  { label: 'Freqtrade 导入', note: '兼容常见 IStrategy，自动体检并支持做空 / 杠杆入口', route: 'strategies', feature: 'import.freqtrade_short', group: 'package' },
  { label: '研究包导出', note: '代码、数据、资金费、提示词、工具调用、结果和审查报告', route: 'backtests', tag: '复现', group: 'package' },
  { label: '策略血缘', note: '版本链、克隆分支、编辑者与回测', route: 'lineage', group: 'collab' },
  { label: 'AI 研究室', note: '研究对话、工具调用、后台任务与结构化报告', route: 'lab', group: 'collab' },
  { label: '团队评审', note: '市场、成本执行、代码审计多角色辩论', ai: '/discuss 对当前策略做一次上线前评审', group: 'collab' },
  { label: '并行子 Agent', note: '多条研究路线同时探索，再由研究组长汇总', ai: '/parallel 并行检查数据、信号、成本和执行，最后汇总冲突', group: 'collab' },
  { label: '策略广场', note: '分享、复核、克隆社区策略', route: 'square', group: 'collab' },
  { label: '群组与任务', note: '多人协作、任务摊派、@ 成员与引用研究材料', route: 'groups', group: 'collab' },
  { label: '我的收藏', note: '集中复用收藏的策略、因子、报告与讨论', route: 'favorites', group: 'collab' },
  { label: '通知中心', note: '审批、任务、评审与后台研究结果提醒', route: 'inbox', group: 'collab' },
  { label: '问题反馈', note: '提交数据异常、复现问题与改进建议', route: 'issues', group: 'collab' },
  { label: '定时任务与晨报', note: '每天自动研究，结果写进晨报', route: 'long-running', group: 'collab' },
  { label: '调研报告生成', note: '联网与平台数据调研，生成结构化、可导出的报告', ai: '/report 调研最近一个月加密市场的主要事件与行情变化', group: 'collab' },
  { label: '研究角色库', note: '内置角色、任务模板与自定义评审分工', route: 'roles', group: 'config' },
  { label: 'Agent 提示词库', note: '系统提示词、任务模板、版本与跨团队复用', route: 'prompts', group: 'config' },
  { label: '模型服务与成本', note: '可切换服务商、模型限额、Token 用量与成本', route: 'models', group: 'config' },
  { label: 'API 与工具参考', note: 'Agent 工具、斜杠命令和接口契约', route: 'docs', group: 'config' },
  { label: '能力矩阵与功能开关', note: '查看现货、永续、真实资金和导入能力的真实状态', route: 'settings', group: 'config' },
  { label: '真实永续安全台', note: '独立开关、期货 Key、确认码、限额和自动急停', route: 'realperp', feature: 'execution.real_perp', tag: '安全', group: 'runtime' },
  { label: '微实盘', note: 'Binance / OKX 现货小额真实资金，止损必挂', route: 'microlive', feature: 'execution.real_spot', group: 'runtime' },
  { label: '纸面 / 模拟盘', note: '同一策略在平台撮合和交易所模拟盘运行', route: 'paper', group: 'runtime' },
  { label: '实盘对照与只读账户', note: '导入真实成交，逐笔归因执行缺口', route: 'live', group: 'runtime' },
]
