/**
 * 研究流程覆盖审计。
 *
 * 目标：平台新增页面时，必须明确进入研究流程，或明确声明为“非研究流程 / 详情页”。
 * 防止以后新增研究能力后，只在侧栏出现、研究流程长期漏展示。
 */
const fs = require('fs')
const path = require('path')

const ROOT = path.resolve(__dirname, '..')
const APP = path.join(ROOT, 'web', 'src', 'App.tsx')
const DATA = path.join(ROOT, 'web', 'src', 'pages', 'research-flow', 'data.ts')

const app = fs.readFileSync(APP, 'utf8')
const data = fs.readFileSync(DATA, 'utf8')

const union = app.match(/type PageKey =([\s\S]*?)\r?\n\r?\n\/\*\*/)
if (!union) throw new Error('无法读取 App.tsx 的 PageKey 联合类型')
const pageKeys = [...union[1].matchAll(/'([^']+)'/g)].map((m) => m[1])
const routeRoots = new Set([...data.matchAll(/route:\s*'([^']+)'/g)].map((m) => m[1].split('/')[0]))

// 详情页通过阶段 / 工具上下文打开；它们不需要再占一个重复入口。
const CONTEXTUAL = {
  flow: '研究流程页面本身',
  dsl: '我的策略 → 规则编辑',
  editor: '我的策略 → 代码编辑',
  backtest: '回测 → 详情',
  optimize: '参数优化 → 详情',
  post: '策略广场 / 调研报告 → 内容详情',
  report: 'AI 研究室 → 调研报告',
  teams: 'AI 研究室 → 团队讨论记录',
}

// 这些页面不是研究流程本身：产品入口、个人资料、法务与部署运维。
const NON_RESEARCH = {
  home: '首页与全局导航',
  board: '同一批数据 / 策略的手机看板',
  brand: '视觉规范与设计令牌',
  profile: '个人资料主页',
  privacy: '隐私政策',
  admin: '部署运维控制台',
}

const missing = pageKeys.filter((key) => !routeRoots.has(key) && !(key in CONTEXTUAL) && !(key in NON_RESEARCH))
const staleRoutes = [...routeRoots].filter((key) => !pageKeys.includes(key))

if (missing.length || staleRoutes.length) {
  if (missing.length) console.error(`研究流程漏掉页面：${missing.join(', ')}`)
  if (staleRoutes.length) console.error(`研究流程引用了不存在的 PageKey：${staleRoutes.join(', ')}`)
  process.exit(1)
}

const researchCount = pageKeys.filter((key) => routeRoots.has(key)).length
console.log(`研究流程覆盖审计通过：${researchCount}/${pageKeys.length} 个页面直接可达，${Object.keys(CONTEXTUAL).length} 个详情页按上下文打开，${Object.keys(NON_RESEARCH).length} 个非研究页面明确排除。`)
