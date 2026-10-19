import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type ConfigView, type Health } from './api/core'
import { BusyOverlay, Callout, PageErrorBoundary, Toast } from './components/ui'
import { RoleDrawer } from './components/roles'
import { Icon, LogoMark } from './components/icons'
import { UserAvatar } from './components/UserAvatar'
import Tape from './components/Tape'
import DataCenterPage from './pages/DataCenter'
import FactorsPage from './pages/Factors'
import ModelLibPage from './pages/ModelLib'
import PortfolioPage from './pages/Portfolio'
import ExecutionPage from './pages/Execution'
import AssemblePage from './pages/Assemble'
import DisciplinePage from './pages/Discipline'
import ValidationPage from './pages/Validation'
import { DiscussionCard } from './components/chat/Discussion'
import { DialogHost } from './components/dialog'
import StrategyLabPage from './pages/StrategyLab'
import RoleLibraryPage from './pages/RoleLibrary'
import LongRunningPage from './pages/LongRunning'
import PromptLibraryPage from './pages/PromptLibrary'
import ModelConfigPage from './pages/ModelConfig'
import HomePage from './pages/Home'
import ReportPage from './pages/Report'
import MobileBoardPage from './pages/MobileBoard'
import MyStrategiesPage from './pages/MyStrategies'
import EditorPage from './pages/Editor'
import BacktestDetailPage from './pages/BacktestDetail'
import BacktestListPage from './pages/BacktestList'
import OptimizePage from './pages/Optimize'
import PaperPage from './pages/Paper'
import CommunityPage, { ComposePage, PostPage } from './pages/Community'
import ProfilePage from './pages/Profile'
import FavoritesPage from './pages/Favorites'
import ApiDocsPage from './pages/ApiDocs'
import BrandPage from './pages/Brand'
import LoginPage from './pages/Login'
import SettingsPage from './pages/Settings'
import LivePage from './pages/Live'
import AccountsPage from './pages/Accounts'
import DemoAccountsPage from './pages/DemoAccounts'
import DslEditorPage from './pages/DslEditor'
import RunBanner from './components/RunBanner'
import MicroLivePage from './pages/MicroLive'
import RealPerpPage from './pages/RealPerp'
import AskAIPanel from './components/AskAI'
import OnboardingHost from './onboarding/OnboardingHost'
import GroupsPage from './pages/Groups'
import InboxPage from './pages/Inbox'
import IssuesPage, { NewIssueModal } from './pages/Issues'
import AdminPage from './pages/Admin'
import LineagePage from './pages/LineagePage'
import { InboxBell, MessageToasts, PlatformBanner, UnreadDot } from './components/Inbox'
import { v4, type Me } from './api/platform'
import { ApiError, currentWorkspace, setWorkspace } from './http'
import { Loading } from './components/ui'
import { PageHead } from './components/PageHead'
import { PageSlot } from './components/pageSlot'
import { SearchButton } from './components/SearchPalette'
import { SettingsShell, settingsKeyOf } from './components/SettingsShell'
import { SidebarConversations } from './pages/lab/Conversations'
import PrivacyPage from './pages/Privacy'
import ResearchFlowPage from './pages/ResearchFlow'
import PublicSharePage from './pages/PublicShare'
import MentorPage from './pages/Mentor'
import { SideToggle, SideResizer, useSidebar } from './components/sidebar'
import { lang, setLang, t } from './i18n'

type PageKey =
  | 'home'
  | 'board'
  | 'strategies'
  | 'factors'
  | 'modellib'
  | 'portfolio'
  | 'execution'
  | 'assemble'
  | 'discipline'
  | 'validation'
  | 'lab'
  | 'flow'
  | 'mentor'
  | 'backtests'
  | 'paper'
  | 'square'
  | 'data'
  | 'teams'
  | 'roles'
  | 'long-running'
  | 'prompts'
  | 'models'
  | 'docs'
  | 'brand'
  | 'live'
  | 'accounts'
  | 'demo'
  | 'microlive'
  | 'realperp'
  | 'settings'
  | 'groups'
  | 'inbox'
  | 'issues'
  | 'admin'
  | 'lineage'
  // 带参数的详情页（不出现在导航里）
  | 'dsl'
  | 'editor'
  | 'backtest'
  | 'optimize'
  | 'post'
  | 'report'
  | 'profile'
  | 'favorites'
  | 'privacy'

/** 这些页面在全屏「设置」里打开，不显示主侧栏。 */
const SETTINGS_PAGES = new Set<PageKey>(['settings', 'models', 'roles', 'prompts', 'docs', 'brand', 'admin'])

/** 对话类页面铺满主区域，不要页边距与页脚。 */
const FULL_BLEED = new Set<PageKey>(['lab', 'teams', 'groups'])

/**
 * 导航按使用顺序排：先研究素材，再做策略，再验证，最后运行。
 * 每组带编号，侧栏用一条竖线把四步连起来，读起来就是一条流水线。
 */
const NAV: { group: string; step?: number; items: { key: PageKey; label: string }[] }[] = [
  {
    group: '',
    items: [
      { key: 'home', label: '首页' },
      { key: 'lab', label: 'AI 研究室' },
      { key: 'flow', label: '研究流程' },
    ],
  },
  {
    group: '研究', step: 1,
    items: [
      { key: 'data', label: '行情与数据' },
      { key: 'factors', label: '因子' },
      { key: 'modellib', label: '预测模型' },
    ],
  },
  {
    group: '策略', step: 2,
    items: [
      { key: 'strategies', label: '我的策略' },
      { key: 'portfolio', label: '组合与执行' },
    ],
  },
  {
    group: '验证', step: 3,
    items: [
      { key: 'backtests', label: '回测' },
      { key: 'validation', label: '上线验证' },
      { key: 'discipline', label: '试验台账' },
    ],
  },
  {
    group: '运行', step: 4,
    items: [
      { key: 'paper', label: '纸面模拟' },
      { key: 'demo', label: '交易所模拟盘' },
      { key: 'microlive', label: '微实盘' },
      { key: 'realperp', label: '真实永续' },
      { key: 'live', label: '实盘对照' },
    ],
  },
  {
    group: '自动化',
    items: [
      { key: 'long-running', label: '定时任务' },
    ],
  },
  {
    group: '协作',
    items: [
      { key: 'groups', label: '群组与任务' },
      { key: 'square', label: '策略广场' },
      { key: 'favorites', label: '我的收藏' },
      { key: 'issues', label: '问题反馈' },
    ],
  },
]

/**
 * 一个导航入口下的几个相关页面：页面顶部出现一排子页签，切换时导航高亮不变。
 * 第一个是入口本身。
 */
const HUBS: Partial<Record<PageKey, { key: PageKey; label: string }[]>> = {
  strategies: [{ key: 'strategies', label: '策略列表' }, { key: 'lineage', label: '血缘与协作' }, { key: 'assemble', label: '积木搭建' }],
  portfolio: [{ key: 'portfolio', label: '组合与风控' }, { key: 'execution', label: '下单执行' }],
  live: [{ key: 'live', label: '实盘对照' }, { key: 'accounts', label: '只读账户' }],
}
const HUB_OF: Partial<Record<PageKey, PageKey>> = Object.fromEntries(
  Object.entries(HUBS).flatMap(([hub, tabs]) => tabs!.map((t) => [t.key, hub])),
) as Partial<Record<PageKey, PageKey>>

const PAGE_TITLES: Record<PageKey, string> = {
  home: '首页',
  board: '手机看板',
  strategies: '我的策略',
  factors: '因子',
  modellib: '预测模型',
  portfolio: '组合与风控',
  execution: '下单执行',
  assemble: '积木搭建',
  discipline: '试验台账',
  validation: '上线验证',
  lab: 'AI 研究室',
  flow: '研究流程',
  mentor: '研究导师',
  backtests: '回测',
  paper: '纸面模拟',
  live: '实盘对照',
  accounts: '只读账户',
  demo: '交易所模拟盘',
  microlive: '微实盘',
  realperp: '真实永续',
  settings: '设置',
  dsl: '规则策略编辑器',
  square: '策略广场',
  data: '行情与数据',
  teams: '团队讨论',
  roles: '角色与团队',
  'long-running': '定时任务',
  prompts: '提示词',
  models: '模型',
  docs: 'API 文档',
  brand: '视觉规范',
  editor: '策略编辑器',
  backtest: '回测详情',
  optimize: '参数优化',
  post: '帖子',
  report: '调研报告',
  profile: '用户主页',
  favorites: '我的收藏',
  groups: '群组与任务',
  inbox: '通知中心',
  issues: '问题反馈',
  admin: '运维控制台',
  lineage: '策略血缘',
  privacy: '隐私政策',
}

/** 详情页在导航里高亮哪个入口。 */
const PARENT: Partial<Record<PageKey, PageKey>> = {
  mentor: 'flow',
  board: 'paper',
  editor: 'strategies',
  backtest: 'backtests',
  optimize: 'strategies',
  post: 'square',
  dsl: 'strategies',
  report: 'lab',
  inbox: 'groups',
  profile: 'square',
}

const NAV_LABEL: Partial<Record<PageKey, string>> = Object.fromEntries(NAV.flatMap((g) => g.items.map((i) => [i.key, i.label])))

/** 当前页面在导航里对应哪一项：详情页看 PARENT，子页签看所属入口。 */
function navOf(page: PageKey): PageKey {
  const p = PARENT[page] ?? page
  return HUB_OF[p] ?? p
}

/** 入口下的子页签。对话类页面铺满主区域，子页签浮在顶部。 */
function HubTabs({ tabs, page }: { tabs: { key: PageKey; label: string }[]; page: PageKey }) {
  const cur = PARENT[page] ?? page
  return (
    <nav className={`hub-tabs${FULL_BLEED.has(page) ? ' floating' : ''}`} aria-label="子页面">
      {tabs.map((t) => (
        <a key={t.key} href={`#/${t.key}`} aria-current={cur === t.key ? 'page' : undefined}>{t.label}</a>
      ))}
    </nav>
  )
}

/** 每个页面一句话说明它是做什么的，显示在页头标题下面。 */
const PURPOSE: Partial<Record<PageKey, string>> = {
  flow: '把数据、因子、策略、模型、组合、执行、验证与运行串成一条可操作的研究流程',
  mentor: '用课程、检查项和红旗理解一笔研究为什么可信',
  data: '看行情、查数据质量、导入自己的 K 线',
  factors: '研究和管理选币信号，样本外通过才进库',
  modellib: '用因子训练预测模型，和线性基线比',
  strategies: '写、改、导入策略，批准后才能运行',
  assemble: '不写代码，用已有的因子和模型拼一个策略',
  portfolio: '把多个信号变成仓位权重，并写好风控规则',
  execution: '仿真拆单算法，按成交成本验收',
  backtests: '在历史行情上跑策略，看收益、回撤和成本',
  validation: '策略上线前要过的几道关，每一关都要你确认',
  discipline: '记下每一次试验，按试验次数修正最好成绩',
  paper: '用实时行情模拟成交，不下真实订单',
  demo: '接交易所的模拟账户，下模拟单、核对成交',
  microlive: '用很小的真实资金运行已毕业的策略',
  realperp: '独立真实永续通道：杠杆、保证金、强平与自动急停',
  live: '用真实成交核对模拟结果',
  accounts: '只读 Key 读取余额和成交，不能下单',
  square: '分享思路、复核别人的回测、一键克隆策略；成员与 Agent 发的帖子分开标注',
  groups: '自由组建群组：讨论、@ 成员、引用回测，摊派任务，共享策略，单独配模型与额度',
  inbox: '所有通知集中在这里，可以按人、群组、类型屏蔽',
  issues: '遇到问题或有建议，提交给管理员处理，支持附截图与文件',
  lineage: '每个策略的版本链与克隆分支：谁改的、跑过哪些回测',
  profile: '查看成员资料、关注关系、帖子与私信设置',
  favorites: '帖子、策略、回测等内容统一收藏',
  roles: 'AI 研究室里的角色和团队',
  models: '配置 AI 研究室用的大模型',
  prompts: '各角色的提示词，可以改写',
  'long-running': '每天按设定时间自动执行研究队列，结果写入晨报',
  settings: '工作区、成员、额度和运行节点',
  docs: '策略 SDK 的完整参考',
  brand: 'QuantLab Bot 的造型、表情语法与使用边界',
}

const VALID_PAGES = new Set<PageKey>(Object.keys(PAGE_TITLES) as PageKey[])

/** 原「统一赛道」页面已并入策略 / 回测 / 总览，旧网址跳到新位置。 */
const LEGACY: Record<string, string> = {
  overview: 'home', arena: 'backtests/compare', strategy: 'strategies', experiments: 'backtests/new',
}

function routeFromHash(): { page: PageKey; arg: string } {
  const head0 = window.location.hash.replace(/^#\/?/, '').split('/')[0]
  if (LEGACY[head0]) window.history.replaceState(null, '', `#/${LEGACY[head0]}`)
  const raw = window.location.hash.replace(/^#\/?/, '')
  const [head, ...rest] = raw.split('/')
  const page = VALID_PAGES.has(head as PageKey) ? (head as PageKey) : 'home'
  return { page, arg: decodeURIComponent(rest.join('/')) }
}

function Workbench({ me, onMeChanged }: { me: Me; onMeChanged: () => void }) {
  const [route, setRoute] = useState(() => routeFromHash())
  const page = route.page
  const [health, setHealth] = useState<Health | null>(null)
  const [config, setConfig] = useState<ConfigView | null>(null)
  const [toast, setToast] = useState<{ message: string; error?: boolean } | null>(null)
  const [busy, setBusy] = useState<{ title: string; note?: string } | null>(null)
  const [features, setFeatures] = useState<Record<string, boolean>>({})

  // 从 AI 对话里点链接跳到别的页面（回测、策略、报告……）时记下来源对话，目标页顶部显示「返回对话」
  const [returnTo, setReturnTo] = useState<{ route: string; title: string } | null>(() => {
    try { return JSON.parse(sessionStorage.getItem('qa.returnTo') || 'null') } catch { return null }
  })
  useEffect(() => {
    const onHash = (e: HashChangeEvent) => {
      const next = routeFromHash()
      const prev = (e.oldURL.split('#')[1] || '').replace(/^\/?/, '')
      if (next.page === 'lab') setReturnTo(null)
      else if (/^lab\/[\w-]+/.test(prev) && !SETTINGS_PAGES.has(next.page)) {
        setReturnTo({ route: prev.split('?')[0], title: document.title.replace(/ · QuantLab v0.1.0-preview.1$/, '') })
      }
      setRoute(next)
      // AI 研究室有自己的消息滚动容器：对话之间切换时保留窗口滚动；从其他页面进入时回到顶部。
      const prevPage = prev.split('/')[0]
      if (next.page !== 'lab' || prevPage !== 'lab') window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior })
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  useEffect(() => {
    try { if (returnTo) sessionStorage.setItem('qa.returnTo', JSON.stringify(returnTo)); else sessionStorage.removeItem('qa.returnTo') } catch { /* 私密模式 */ }
  }, [returnTo])

  const lastMain = useRef('home')
  useEffect(() => {
    if (!SETTINGS_PAGES.has(page)) lastMain.current = window.location.hash.replace(/^#\/?/, '') || 'home'
  }, [page, route.arg])

  const go = useCallback((key: string) => {
    window.location.hash = `/${key}`
  }, [])

  const notify = useCallback((message: string, error = false) => {
    setToast({ message, error })
    window.setTimeout(() => setToast(null), error ? 8500 : 4000)
  }, [])

  const refreshStatus = useCallback(async () => {
    try {
      const [h, c] = await Promise.all([api.health(), api.config()])
      setHealth(h)
      setConfig(c)
    } catch {
      /* 状态条失败不影响主内容 */
    }
  }, [])

  useEffect(() => {
    refreshStatus()
  }, [refreshStatus, page])

  useEffect(() => {
    v4.workspaceFeatures().then((r) => setFeatures(r.enabled)).catch(() => { /* 读取失败时由服务端继续强制校验 */ })
  }, [page, me.workspace?.id])

  useEffect(() => {
    document.title = `${t(PAGE_TITLES[page])} · QuantLab v0.1.0-preview.1`
  }, [page])

  const reachable = health ? Object.entries(health.adapters).filter(([, v]) => v.reachable).map(([k]) => k) : []
  const keyedProviders = config?.providers.filter((p) => p.has_key && p.name !== 'local').length ?? 0
  const llmReady = keyedProviders > 0

  const [slot, setSlot] = useState<HTMLElement | null>(null)
  const [flowPrefState, setFlowPrefState] = useState<boolean | null>(() => {
    try { const v = localStorage.getItem('qa.nav.flow'); return v === null ? null : v === '1' } catch { return null }
  })
  const flowPref = flowPrefState
  const setFlowPref = (v: boolean) => { setFlowPrefState(v); try { localStorage.setItem('qa.nav.flow', v ? '1' : '0') } catch { /* 私密模式等 */ } }
  const pageProps = { onNavigate: go, notify, setBusy, features, me }
  const navItems = (items: { key: PageKey; label: string }[]) => items.filter((i) => !(i.key === 'microlive' && features['execution.real_spot'] === false) && !(i.key === 'realperp' && features['execution.real_perp'] === false))
  const showHead = page !== 'home' && !FULL_BLEED.has(page) && !PARENT[page]
  const hub = PARENT[page] ? undefined : HUBS[HUB_OF[page] ?? page]
  const nav = navOf(page)
  const crumb = nav !== page ? nav : null
  // 研究四阶段合并成一个可折叠的「研究流程」：默认收起，给常驻的对话列表让位置；手动展开 / 收起会记住。
  // 每个页面同一个默认值，进出 AI 研究室时侧栏不变形
  const stageOf = NAV.find((g) => g.step && g.items.some((i) => i.key === nav))
  const flowOpen = flowPref ?? false
  const side = useSidebar()
  // 手机：侧栏变成从左侧拉出的抽屉（顶栏「≡」按钮或从屏幕左缘右滑打开），换页自动收起
  const [drawer, setDrawer] = useState(false)
  useEffect(() => { setDrawer(false) }, [page, route.arg])
  useEffect(() => {
    document.documentElement.classList.toggle('drawer-open', drawer)
    if (!drawer) return
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setDrawer(false) }
    document.addEventListener('keydown', esc)
    return () => document.removeEventListener('keydown', esc)
  }, [drawer])
  useEffect(() => {
    let start: { x: number; y: number } | null = null
    const down = (e: TouchEvent) => {
      const t = e.touches[0]
      start = window.innerWidth <= 720 && (t.clientX < 24 || document.documentElement.classList.contains('drawer-open')) ? { x: t.clientX, y: t.clientY } : null
    }
    const up = (e: TouchEvent) => {
      if (!start) return
      const t = e.changedTouches[0]
      const dx = t.clientX - start.x
      if (Math.abs(dx) > 60 && Math.abs(t.clientY - start.y) < 50) setDrawer(dx > 0)
      start = null
    }
    window.addEventListener('touchstart', down, { passive: true })
    window.addEventListener('touchend', up, { passive: true })
    return () => { window.removeEventListener('touchstart', down); window.removeEventListener('touchend', up) }
  }, [])

  const render = () => {
    const arg = route.arg
    switch (page) {
      case 'home':
        return <HomePage {...pageProps} />
      case 'board':
        return <MobileBoardPage {...pageProps} />
      case 'report':
        return <ReportPage arg={arg} {...pageProps} />
      case 'lab':
        return <StrategyLabPage key={arg || 'new'} arg={arg || undefined} {...pageProps} />
      case 'flow':
        return <ResearchFlowPage {...pageProps} />
      case 'mentor':
        return <MentorPage {...pageProps} />
      case 'strategies':
        return <MyStrategiesPage {...pageProps} />
      case 'factors':
        return <FactorsPage key={arg && !['new', 'trials', 'syntax'].includes(arg) ? 'detail' : 'tabs'} arg={arg || undefined} {...pageProps} />
      case 'modellib':
        return <ModelLibPage key={arg && !['train', 'runs'].includes(arg) ? `d-${arg}` : 'tabs'} arg={arg || undefined} {...pageProps} />
      case 'portfolio':
        return <PortfolioPage key={arg && !['design', 'risk', 'runs'].includes(arg) ? `d-${arg}` : 'tabs'} arg={arg || undefined} {...pageProps} />
      case 'execution':
        return <ExecutionPage key={arg && !['design', 'replay', 'radar', 'costs', 'record', 'runs'].includes(arg) ? `d-${arg}` : 'tabs'} arg={arg || undefined} {...pageProps} />
      case 'assemble':
        return <AssemblePage {...pageProps} />
      case 'validation':
        return <ValidationPage key={arg || 'gate'} arg={arg || undefined} {...pageProps} />
      case 'discipline':
        return <DisciplinePage key={arg || 'ledger'} arg={arg || undefined} {...pageProps} />
      case 'editor':
        return arg ? <EditorPage key={arg} slug={arg} {...pageProps} /> : <MyStrategiesPage {...pageProps} />
      case 'backtests':
        return <BacktestListPage key={arg || 'all'} slug={arg || undefined} {...pageProps} />
      case 'backtest':
        return <BacktestDetailPage key={arg} id={arg} {...pageProps} />
      case 'optimize':
        return <OptimizePage key={arg} slug={arg} {...pageProps} />
      case 'paper':
        return <PaperPage key={arg || 'list'} id={arg || undefined} {...pageProps} />
      case 'square':
        return <CommunityPage {...pageProps} />
      case 'profile':
        return <ProfilePage identifier={arg} {...pageProps} />
      case 'favorites':
        return <FavoritesPage {...pageProps} />
      case 'groups':
        return <GroupsPage arg={arg || undefined} {...pageProps} />
      case 'inbox':
        return <InboxPage {...pageProps} />
      case 'issues':
        return <IssuesPage key={arg || 'list'} arg={arg || undefined} {...pageProps} />
      case 'privacy':
        return <PrivacyPage />
      case 'admin':
        return <AdminPage key={arg || 'overview'} tab={arg || undefined} {...pageProps} />
      case 'lineage':
        return <LineagePage arg={arg || undefined} {...pageProps} />
      case 'post':
        // #/post/new/<类型> 发帖，#/post/<id>/edit 编辑：都是完整页面
        return arg.startsWith('new') || arg.endsWith('/edit')
          ? <ComposePage key={arg} arg={arg} {...pageProps} />
          : <PostPage key={arg} id={arg} {...pageProps} />
      case 'docs':
        return <ApiDocsPage {...pageProps} />
      case 'brand':
        return <BrandPage {...pageProps} />
      case 'data':
        return <DataCenterPage arg={arg || undefined} {...pageProps} />
      case 'teams':
        // 团队讨论已并入 AI 研究室（/discuss）；旧链接只读查看那次讨论
        if (!arg) { window.setTimeout(() => go('lab')); return null }
        return (
          <div className="mission-solo">
            <p className="hint">团队讨论已并入 AI 研究室：在任意对话里输入 <code>/discuss 议题</code> 发起。下面是这次讨论的记录。</p>
            <DiscussionCard missionId={arg} topic="团队讨论" notify={notify}
              onPin={(t) => { navigator.clipboard?.writeText(t).then(() => notify('结论已复制，可以粘贴进研究室的对话')) }} />
          </div>
        )
      case 'roles':
        return <RoleLibraryPage {...pageProps} />
      case 'long-running':
        return <LongRunningPage key={arg || 'night'} arg={arg || undefined} {...pageProps} />
      case 'prompts':
        return <PromptLibraryPage {...pageProps} />
      case 'models':
        return <ModelConfigPage {...pageProps} />
      case 'live':
        return <LivePage key={arg || 'live'} arg={arg || undefined} {...pageProps} />
      case 'realperp':
        if (features['execution.real_perp'] === false) return <Callout tone="warn">当前工作区已关闭「真实资金永续」。请工作区管理员在设置 → 功能开关中开启后再进入。</Callout>
        return <RealPerpPage isAdmin={me.workspace?.role === 'admin'} {...pageProps} />
      case 'microlive':
        if (features['execution.real_spot'] === false) return <Callout tone="warn">当前工作区已关闭「真实资金现货」。请工作区管理员在设置 → 功能开关中开启后再进入微实盘。</Callout>
        return <MicroLivePage isAdmin={me.workspace?.role === 'admin'} {...pageProps} />
      case 'demo':
        return <DemoAccountsPage readonly={me.workspace?.role === 'reviewer'} {...pageProps} />
      case 'accounts':
        return <AccountsPage readonly={me.workspace?.role === 'reviewer'} {...pageProps} />
      case 'settings':
        return <SettingsPage key={arg || 'ws'} tab={arg || undefined} onMeChanged={onMeChanged} {...pageProps} />
      case 'dsl':
        return <DslEditorPage key={arg || 'new'} slug={arg || undefined} {...pageProps} />
      default:
        return null
    }
  }

  if (SETTINGS_PAGES.has(page)) {
    return (
      <>
        <SettingsShell current={settingsKeyOf(page, route.arg)} onNavigate={go} onClose={() => go(lastMain.current)} badges={llmReady ? undefined : { models: '未配置' }} isAdmin={me.user.is_admin}>
          {render()}
        </SettingsShell>
        {busy && <BusyOverlay title={busy.title} note={busy.note} />}
        <RoleDrawer notify={notify} />
        <DialogHost />
        <OnboardingHost me={me} page={page} go={go} notify={notify} onMeChanged={onMeChanged} />
        <MessageToasts meName={me.user.name || me.user.username || ''} onNavigate={go} />
        {toast && <Toast message={toast.message} error={toast.error} onDone={() => setToast(null)} />}
      </>
    )
  }

  return (
    <>
      {drawer && <div className="drawer-backdrop" onClick={() => setDrawer(false)} aria-hidden />}
      <aside className={`sidebar${drawer ? ' open' : ''}`} aria-hidden={side.collapsed || undefined}>
        <div className="side-top">
          <a className="side-brand" href="#/home"><LogoMark size={20} /><span className="side-brand-copy"><span>QuantLab</span><small>v0.1.0-preview.1</small></span></a>
          <SearchButton onNavigate={go} />
          <SideToggle collapsed={false} onToggle={side.toggle} />
          <button type="button" className="drawer-close" onClick={() => setDrawer(false)} aria-label={t('关闭导航')}>
            <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden><path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
          </button>
        </div>

        <nav aria-label="主导航">
          {NAV.filter((g) => !g.step).slice(0, 1).map((group) => (
            <div key="top">
              {navItems(group.items).map((item) => (
                <button key={item.key} className="nav-item" data-tour={`nav-${item.key}`} aria-current={nav === item.key ? 'page' : undefined} onClick={() => go(item.key)}>
                  <Icon name={item.key} />
                  {item.label}
                </button>
              ))}
            </div>
          ))}
          <div className={`nav-flow${flowOpen ? ' open' : ''}`}>
            <button type="button" className="nav-flow-head" aria-expanded={flowOpen} onClick={() => setFlowPref(!flowOpen)}>
              <span>研究流程</span>
              {!flowOpen && stageOf && <em>{stageOf.step} · {stageOf.group}</em>}
              <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="m3.5 2 3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" /></svg>
            </button>
            {NAV.filter((g) => g.step).map((group) => {
              const items = flowOpen ? navItems(group.items) : navItems(group.items).filter((i) => nav === i.key)
              if (!items.length) return null
              return (
                <div key={group.group} className="nav-flow-stage">
                  {flowOpen && <div className="nav-flow-label"><i>{group.step}</i>{group.group}</div>}
                  {items.map((item) => (
                    <button key={item.key} className="nav-item" data-tour={`nav-${item.key}`} aria-current={nav === item.key ? 'page' : undefined} onClick={() => go(item.key)}>
                      <Icon name={item.key} />
                      {item.label}
                    </button>
                  ))}
                </div>
              )
            })}
          </div>
          {NAV.filter((g) => !g.step).slice(1).map((group) => (
            <div key={group.group || 'more'}>
              {group.group && <div className="nav-label">{group.group}</div>}
              {navItems(group.items).map((item) => (
                <button key={item.key} className="nav-item" data-tour={`nav-${item.key}`} aria-current={nav === item.key ? 'page' : undefined} onClick={() => go(item.key)}>
                  <Icon name={item.key} />
                  {item.label}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-slot">
          <SidebarConversations activeId={page === 'lab' ? route.arg : ''} notify={notify} />
          <div id="sidebar-slot" className="sidebar-slot-extra" />
        </div>

        <AccountMenu me={me} reachable={reachable} llmReady={llmReady} onNavigate={go} notify={notify} />
        <SideResizer width={side.width} onWidth={side.setWidth} />
      </aside>

      <div className="shell">
        <header className="topbar">
          <div>
            <button type="button" className="drawer-open-btn" onClick={() => setDrawer(true)} aria-label={t('打开导航')} aria-expanded={drawer}>
              <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden><path d="M3 5h12M3 9h12M3 13h8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
            </button>
            {side.collapsed && <SideToggle collapsed onToggle={side.toggle} />}
            <span className="breadcrumb">{me.workspace?.name ?? '工作区'}</span>
            <span className="separator">/</span>
            {crumb && (
              <>
                <a className="breadcrumb" href={`#/${crumb}`}>{NAV_LABEL[crumb] ?? PAGE_TITLES[crumb]}</a>
                <span className="separator">/</span>
              </>
            )}
            <span>{PAGE_TITLES[page]}</span>
          </div>
          <div className="topbar-right">
            <InboxBell onNavigate={go} />
            <Tape />
            <RunBanner onNavigate={go} llmReady={keyedProviders > 0} />
          </div>
        </header>

        <PlatformBanner />
        <main className={FULL_BLEED.has(page) ? 'full' : undefined}>
          {me.workspace?.role === 'reviewer' && <div className="callout warn" style={{ marginBottom: 14 }}>你在此工作区是<b>审阅者</b>：可以查看全部内容，但不能修改或发起运行。</div>}
          {returnTo && page !== 'lab' && (
            <div className="return-pill anim-in">
              <button type="button" className="return-pill-go" onClick={() => go(returnTo.route)} title="回到刚才的 AI 对话，滚动位置与草稿都保留">
                <span aria-hidden>←</span> 返回对话{returnTo.title ? `「${returnTo.title.slice(0, 24)}」` : ''}
              </button>
              <button type="button" className="return-pill-x" aria-label="不再提示" onClick={() => setReturnTo(null)}>×</button>
            </div>
          )}
          {showHead && <PageHead key={nav}title={NAV_LABEL[nav] ?? PAGE_TITLES[page]} purpose={PURPOSE[page]} page={page} onSlot={setSlot} onNavigate={go} />}
          {hub && <HubTabs tabs={hub} page={page} />}
          <PageSlot.Provider value={showHead ? slot : null}>
            <div className="page-enter" key={page}><PageErrorBoundary resetKey={`${page}/${route.arg}`}>{render()}</PageErrorBoundary></div>
          </PageSlot.Provider>
        </main>
      </div>

      {busy && <BusyOverlay title={busy.title} note={busy.note} />}
      <RoleDrawer notify={notify} />
      <DialogHost />
      <AskAIPanel />
      <OnboardingHost me={me} page={page} go={go} notify={notify} onMeChanged={onMeChanged} />
      <MessageToasts meName={me.user.name || me.user.username || ''} onNavigate={go} />
      {toast && <Toast message={toast.message} error={toast.error} onDone={() => setToast(null)} />}
    </>
  )
}


const ROLE_NAME: Record<string, string> = { admin: '管理员', member: '成员', reviewer: '审阅者' }

/** 侧栏底部账号菜单：账号、工作区切换、成员与邀请、退出合在一处。 */
function AccountMenu({ me, reachable, llmReady, onNavigate, notify }: { me: Me; reachable: string[]; llmReady: boolean; onNavigate: (k: string) => void; notify: (m: string, e?: boolean) => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const ws = me.workspace
  const name = me.user.name || me.user.email
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [open])
  const pick = (k: string) => { setOpen(false); onNavigate(k) }
  const [reporting, setReporting] = useState(false)
  return (
    <div className="user-row acct" ref={ref}>
      {reporting && <NewIssueModal onClose={() => setReporting(false)} notify={notify} onDone={(id) => { setReporting(false); onNavigate(`issues/${id}`) }} />}
      <button className="acct-trigger" onClick={() => setOpen(!open)} aria-expanded={open} aria-haspopup="menu">
        <span className="avatar"><UserAvatar src={me.user.avatar_url} color={me.user.avatar_color} size={30} title={name} /><UnreadDot /></span>
        <div className="acct-text">
          <div>{name}</div>
          <small>{me.user.quantlab_uid ? `${me.user.quantlab_uid} · ` : ''}{ws?.name ?? '未选择工作区'} · {ws?.is_primary_admin ? '主管理员' : ROLE_NAME[ws?.role ?? 'member']}</small>
        </div>
        <span className="acct-caret" aria-hidden="true">⌃</span>
      </button>
      {open && (
        <div className="ws-menu acct-menu" role="menu">
          <div className="acct-head">
            <b>{me.user.email || (me.user.username ? `@${me.user.username}` : me.user.name)}</b>
            <small>{me.user.is_admin ? '部署管理员 · ' : ''}{reachable.length ? `${reachable.join(' / ')} 行情已连接` : '行情未接入'}</small>
          </div>
          <hr />
          <div className="acct-label">工作区</div>
          {me.workspaces.map((w) => (
            <button key={w.id} role="menuitem" title={`${w.name} · ${ROLE_NAME[w.role]}`} aria-current={w.id === ws?.id} onClick={() => {
              setWorkspace(w.id)
              window.location.hash = '/home'
              window.location.reload()
            }}>
              <span>{w.name}</span><span className={`role-chip ${w.role}`}>{w.is_primary_admin ? '主管理员' : ROLE_NAME[w.role]}</span>
            </button>
          ))}
          <button role="menuitem" title="新建 / 加入工作区…" onClick={() => pick('settings/workspace')}>新建 / 加入工作区…</button>
          <button role="menuitem" title="成员与邀请…" onClick={() => pick('settings/members')}>成员与邀请…</button>
          <button role="menuitem" title="研究记录配额、倍率和自动清理…" onClick={() => pick('settings/retention')}>研究记录…</button>
          <hr />
          <button role="menuitem" title="通知中心与屏蔽…" onClick={() => pick('inbox')}>通知中心与屏蔽…</button>
          <button role="menuitem" title="查看平台隐私政策…" onClick={() => pick('privacy')}>隐私政策…</button>
          <button role="menuitem" title="报告问题…" onClick={() => { setOpen(false); setReporting(true) }}>报告问题…</button>
          {me.user.is_admin && <button role="menuitem" title="运维控制台 · 部署管理员" onClick={() => pick('admin/overview')}><span>运维控制台</span><span className="role-chip admin">部署管理员</span></button>}
          <hr />
          <div className="acct-label">语言</div>
          <div className="acct-lang" role="group" aria-label="界面语言">
            <button type="button" data-no-i18n aria-pressed={lang === 'zh'} onClick={() => lang !== 'zh' && setLang('zh')}>简体中文</button>
            <button type="button" aria-pressed={lang === 'en'} onClick={() => lang !== 'en' && setLang('en')}>English</button>
          </div>
          <hr />
          <button role="menuitem" title="重新播放新手引导" onClick={() => { setOpen(false); window.dispatchEvent(new CustomEvent('qa:onboarding:restart')) }}>
            <span>新手引导</span><span className="role-chip">重播</span>
          </button>
          <button role="menuitem" title={llmReady ? '设置' : '模型未配置'} onClick={() => pick(llmReady ? 'settings/account' : 'models')}>
            <span>设置</span>{!llmReady && <span className="role-chip warn">模型未配置</span>}
          </button>
          <hr />
          <button role="menuitem" className="acct-logout" onClick={async () => { await v4.logout().catch(() => undefined); setWorkspace(''); window.location.hash = '/home'; window.location.reload() }}>退出登录</button>
        </div>
      )}
    </div>
  )
}

/** 会话门：未登录显示登录页；登录后校正目标工作区（服务端校验成员关系）。 */
export default function App() {
  const [me, setMe] = useState<Me | null | 'anon'>(null)
  const [hash, setHash] = useState(() => window.location.hash)
  const [shareAuth, setShareAuth] = useState(false)
  useEffect(() => {
    const onHash = () => setHash(window.location.hash)
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const shareToken = /^#\/share\/([A-Za-z0-9_-]+)/.exec(hash)?.[1] || ''
  useEffect(() => { setShareAuth(false) }, [shareToken])
  const load = useCallback(async () => {
    try {
      let m = await v4.me()
      const wanted = currentWorkspace()
      if (!wanted || !m.workspaces.some((w) => w.id === wanted)) {
        if (m.workspace) setWorkspace(m.workspace.id)
        m = await v4.me()
      }
      setMe(m)
    } catch (e) {
      if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
        if (e.status === 403) {
          setWorkspace('')
          try { setMe(await v4.me()); return } catch { /* fallthrough */ }
        }
        setMe('anon')
      } else {
        setMe('anon')
      }
    }
  }, [])
  useEffect(() => {
    load()
    const onAuth = () => setMe('anon')
    const onWs = () => load()
    window.addEventListener('qa:auth-required', onAuth)
    window.addEventListener('qa:workspace-invalid', onWs)
    return () => {
      window.removeEventListener('qa:auth-required', onAuth)
      window.removeEventListener('qa:workspace-invalid', onWs)
    }
  }, [load])
  if (shareToken) {
    if (shareAuth) return <LoginPage onDone={() => { setShareAuth(false); setMe(null); load() }} />
    return <PublicSharePage token={shareToken} onLogin={() => setShareAuth(true)}
      onOpen={(route) => { window.location.hash = `/${route}` }} />
  }
  if (me === null) return <div style={{ padding: 80 }}><Loading label="正在验证会话" /></div>
  if (me === 'anon') return <LoginPage onDone={() => { setMe(null); load() }} />
  return <Workbench me={me} onMeChanged={load} />
}
