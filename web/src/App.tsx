import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type ConfigView, type Health } from './api'
import { BusyOverlay, PageErrorBoundary, Toast } from './components/ui'
import { RoleDrawer } from './components/roles'
import { Icon, LogoMark } from './components/icons'
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
import CommunityPage, { PostPage } from './pages/Community'
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
import AskAIPanel from './components/AskAI'
import { v4, type Me } from './api3'
import { ApiError, currentWorkspace, setWorkspace } from './http'
import { Loading } from './components/ui'
import { PageHead } from './components/PageHead'
import { PageSlot } from './components/pageSlot'
import { SearchButton } from './components/SearchPalette'
import { SettingsShell, settingsKeyOf } from './components/SettingsShell'
import { SidebarConversations } from './pages/lab/Conversations'
import { SideToggle, SideResizer, useSidebar } from './components/sidebar'

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
  | 'settings'
  // 带参数的详情页（不出现在导航里）
  | 'dsl'
  | 'editor'
  | 'backtest'
  | 'optimize'
  | 'post'
  | 'report'

/** 这些页面在全屏「设置」里打开，不显示主侧栏。 */
const SETTINGS_PAGES = new Set<PageKey>(['settings', 'models', 'roles', 'prompts', 'long-running', 'docs', 'brand'])

/** 对话类页面铺满主区域，不要页边距与页脚。 */
const FULL_BLEED = new Set<PageKey>(['lab', 'teams'])

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
      { key: 'live', label: '实盘对照' },
    ],
  },
  {
    group: '更多',
    items: [
      { key: 'square', label: '策略广场' },
    ],
  },
]

/**
 * 一个导航入口下的几个相关页面：页面顶部出现一排子页签，切换时导航高亮不变。
 * 第一个是入口本身。
 */
const HUBS: Partial<Record<PageKey, { key: PageKey; label: string }[]>> = {
  strategies: [{ key: 'strategies', label: '策略列表' }, { key: 'assemble', label: '积木搭建' }],
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
  backtests: '回测',
  paper: '纸面模拟',
  live: '实盘对照',
  accounts: '只读账户',
  demo: '交易所模拟盘',
  microlive: '微实盘',
  settings: '设置',
  dsl: '规则策略编辑器',
  square: '策略广场',
  data: '行情与数据',
  teams: '团队讨论',
  roles: '角色与团队',
  'long-running': '夜班',
  prompts: '提示词',
  models: '模型',
  docs: '策略 API 文档',
  brand: '视觉规范',
  editor: '策略编辑器',
  backtest: '回测详情',
  optimize: '参数优化',
  post: '帖子',
  report: '调研报告',
}

/** 详情页在导航里高亮哪个入口。 */
const PARENT: Partial<Record<PageKey, PageKey>> = {
  board: 'paper',
  editor: 'strategies',
  backtest: 'backtests',
  optimize: 'strategies',
  post: 'square',
  dsl: 'strategies',
  report: 'lab',
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
  live: '用真实成交核对模拟结果',
  accounts: '只读 Key 读取余额和成交，不能下单',
  square: '分享思路、复核别人的回测、一键克隆策略',
  roles: 'AI 研究室里的角色和团队',
  models: '配置 AI 研究室用的大模型',
  prompts: '各角色的提示词，可以改写',
  'long-running': '你休息时按队列自动做研究',
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

  useEffect(() => {
    const onHash = () => {
      setRoute(routeFromHash())
      window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior })
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

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
    document.title = `${PAGE_TITLES[page]} · QuantLab`
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
  const pageProps = { onNavigate: go, notify, setBusy }
  const showHead = page !== 'home' && !FULL_BLEED.has(page) && !PARENT[page]
  const hub = PARENT[page] ? undefined : HUBS[HUB_OF[page] ?? page]
  const nav = navOf(page)
  const crumb = nav !== page ? nav : null
  // 研究四阶段合并成一个可折叠的「研究流程」：默认收起，给常驻的对话列表让位置；手动展开 / 收起会记住。
  // 每个页面同一个默认值，进出 AI 研究室时侧栏不变形
  const stageOf = NAV.find((g) => g.step && g.items.some((i) => i.key === nav))
  const flowOpen = flowPref ?? false
  const side = useSidebar()

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
        return <StrategyLabPage arg={arg || undefined} {...pageProps} />
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
      case 'post':
        return <PostPage key={arg} id={arg} {...pageProps} />
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
      case 'microlive':
        return <MicroLivePage isAdmin={me.workspace?.role === 'admin'} {...pageProps} />
      case 'demo':
        return <DemoAccountsPage readonly={me.workspace?.role === 'reviewer'} {...pageProps} />
      case 'accounts':
        return <AccountsPage readonly={me.workspace?.role === 'reviewer'} {...pageProps} />
      case 'settings':
        return <SettingsPage key={arg || 'ws'} tab={arg || undefined} me={me} onMeChanged={onMeChanged} {...pageProps} />
      case 'dsl':
        return <DslEditorPage key={arg || 'new'} slug={arg || undefined} {...pageProps} />
      default:
        return null
    }
  }

  if (SETTINGS_PAGES.has(page)) {
    return (
      <>
        <SettingsShell current={settingsKeyOf(page, route.arg)} onNavigate={go} onClose={() => go(lastMain.current)} badges={llmReady ? undefined : { models: '未配置' }}>
          {render()}
        </SettingsShell>
        {busy && <BusyOverlay title={busy.title} note={busy.note} />}
        <RoleDrawer notify={notify} />
        <DialogHost />
        {toast && <Toast message={toast.message} error={toast.error} onDone={() => setToast(null)} />}
      </>
    )
  }

  return (
    <>
      <aside className="sidebar" aria-hidden={side.collapsed || undefined}>
        <div className="side-top">
          <a className="side-brand" href="#/home"><LogoMark size={20} />QuantLab</a>
          <SearchButton onNavigate={go} />
          <SideToggle collapsed={false} onToggle={side.toggle} />
        </div>

        <nav aria-label="主导航">
          {NAV.filter((g) => !g.step).slice(0, 1).map((group) => (
            <div key="top">
              {group.items.map((item) => (
                <button key={item.key} className="nav-item" aria-current={nav === item.key ? 'page' : undefined} onClick={() => go(item.key)}>
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
              const items = flowOpen ? group.items : group.items.filter((i) => nav === i.key)
              if (!items.length) return null
              return (
                <div key={group.group} className="nav-flow-stage">
                  {flowOpen && <div className="nav-flow-label"><i>{group.step}</i>{group.group}</div>}
                  {items.map((item) => (
                    <button key={item.key} className="nav-item" aria-current={nav === item.key ? 'page' : undefined} onClick={() => go(item.key)}>
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
              {group.items.map((item) => (
                <button key={item.key} className="nav-item" aria-current={nav === item.key ? 'page' : undefined} onClick={() => go(item.key)}>
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

        <AccountMenu me={me} reachable={reachable} llmReady={llmReady} onNavigate={go} />
        <SideResizer width={side.width} onWidth={side.setWidth} />
      </aside>

      <div className="shell">
        <header className="topbar">
          <div>
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
            <Tape />
            <RunBanner onNavigate={go} llmReady={keyedProviders > 0} />
          </div>
        </header>

        <main className={FULL_BLEED.has(page) ? 'full' : undefined}>
          {me.workspace?.role === 'reviewer' && <div className="callout warn" style={{ marginBottom: 14 }}>你在此工作区是<b>审阅者</b>：可以查看全部内容，但不能修改或发起运行。</div>}
          {showHead && <PageHead key={nav} title={NAV_LABEL[nav] ?? PAGE_TITLES[page]} purpose={PURPOSE[page]} page={page} onSlot={setSlot} onNavigate={go} />}
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
      {toast && <Toast message={toast.message} error={toast.error} onDone={() => setToast(null)} />}
    </>
  )
}


const ROLE_NAME: Record<string, string> = { admin: '管理员', member: '成员', reviewer: '审阅者' }

/** 侧栏底部账号菜单：账号、工作区切换、成员与邀请、退出合在一处。 */
function AccountMenu({ me, reachable, llmReady, onNavigate }: { me: Me; reachable: string[]; llmReady: boolean; onNavigate: (k: string) => void }) {
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
  return (
    <div className="user-row acct" ref={ref}>
      <button className="acct-trigger" onClick={() => setOpen(!open)} aria-expanded={open} aria-haspopup="menu">
        <span className="avatar">{name.slice(0, 1).toUpperCase()}</span>
        <div className="acct-text">
          <div>{name}</div>
          <small>{ws?.name ?? '未选择工作区'} · {ROLE_NAME[ws?.role ?? 'member']}</small>
        </div>
        <span className="acct-caret" aria-hidden="true">⌃</span>
      </button>
      {open && (
        <div className="ws-menu acct-menu" role="menu">
          <div className="acct-head">
            <b>{me.user.email}</b>
            <small>{me.user.is_admin ? '部署管理员 · ' : ''}{reachable.length ? `${reachable.join(' / ')} 行情已连接` : '行情未接入'}</small>
          </div>
          <hr />
          <div className="acct-label">工作区</div>
          {me.workspaces.map((w) => (
            <button key={w.id} role="menuitem" aria-current={w.id === ws?.id} onClick={() => {
              setWorkspace(w.id)
              window.location.hash = '/home'
              window.location.reload()
            }}>
              <span>{w.name}</span><span className={`role-chip ${w.role}`}>{ROLE_NAME[w.role]}</span>
            </button>
          ))}
          <button role="menuitem" onClick={() => pick('settings/workspace')}>新建 / 加入工作区…</button>
          <button role="menuitem" onClick={() => pick('settings/members')}>成员与邀请…</button>
          <hr />
          <button role="menuitem" onClick={() => pick(llmReady ? 'settings/account' : 'models')}>
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
  if (me === null) return <div style={{ padding: 80 }}><Loading label="正在验证会话" /></div>
  if (me === 'anon') return <LoginPage onDone={() => { setMe(null); load() }} />
  return <Workbench me={me} onMeChanged={load} />
}
