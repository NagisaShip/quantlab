import TaskResult from '../features/workflows/TaskResult'
import ArtifactPreview from '../features/workflows/ArtifactPreview'
import { isLocalDeployment } from '../deployment'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { PythonCodeButton, type PyKind } from '../components/PythonCode'
import { useDraft } from '../components/draft'
import { createPortal } from 'react-dom'
import { lab, labPrefs, mergeBySeq, missions, type ContextView, type Effort, type Executor, type FactorSummary, type GoalSpec, type LabKind, type LabMessage, type LabMeta, type LabPlan, type LabProject, type Metrics, type Permission, APPROVAL_CATEGORIES, type ApprovalCategory } from '../apiLab'
import { get } from '../http'
import { api, type TeamRun } from '../api/core'
import { AgentStatePill, MessageStateTag } from '../components/chat/LabStatus'
import { RuntimeRecovery } from '../components/lab/RuntimeRecovery'
import { TeamRecovery } from '../components/lab/TeamRecovery'
import {
  clearObservedFailure, deriveAgentStatus, evidenceProbes, messageTag, normalizeRunState, normalizeTeamState, rememberObservedFailure,
  type RunEvidenceState,
} from '../components/chat/status'
import { Avatar } from '../components/Avatar'
import { store } from '../components/prefs'
import { clearLabDraft, peekLabDraft, type LabDraft } from '../components/handoff'
import { AssistantText, EventLine, ToolLine, UserBubble, Working } from '../components/chat/Blocks'
import SessionWorkflows from '../components/chat/SessionWorkflows'
import { Composer, ContextRing, ModelMenu, usePopover, type SlashCommand } from '../components/chat/Composer'
import { ActionMenu, AutoIterateRow, ContextStats, Icons, PermissionMenu, useFilePicker, type ActionItem } from '../components/chat/LabControls'
import { CodeEditor } from '../components/CodeEditor'
import { DiffView, lineDiff } from '../components/Diff'
import { Markdown } from '../components/markdown'
import { GoalCardView, PlanCard, PlanSteps, planMeta } from '../components/lab/LabCards'
import { RenderErrorCtx, type RenderError } from '../components/blocks/renderErrors'
import { ParameterHeatmapCard } from '../components/blocks/ParameterHeatmap'
import { StressTestCard } from '../components/blocks/StressTestCard'
import { Loading, Modal } from '../components/ui'
import { condText, domainMeta, fmtGoal, GoalChips, GoalForm, metricsOf, projectItems, projectSpec, useDefaultSpec } from './lab/goal'
import { FactorCodeCard, FactorPanel, FactorReportCard, FactorVersionMetrics } from './lab/factor'
import { FactorStats } from '../components/factorReport'
import { ModelStats } from '../components/modelReport'
import { ModelPanel, ModelReportCard, ModelSpecCard, ModelVersionMetrics } from './lab/model'
import { PortfolioStats } from '../components/portfolioReport'
import { PortfolioPanel, PortfolioReportCard, PortfolioSpecCard, PortfolioVersionMetrics } from './lab/portfolio'
import { ExecutionPanel, ExecutionReportCard, ExecutionSpecCard, ExecutionVersionMetrics } from './lab/execution'
import { ExecStats } from '../components/executionReport'
import { domainUI, kindOfType, libraryIdOf, pickExamples, RESEARCH_TYPES, WORKFLOW_LABEL, workflowOfType, type ResearchType } from './lab/domains'
import { labChanged, onChatAsk, onNewChat, takeNewChat } from './lab/bus'
import { addFolder, folderOptions, latestFolderOf, localProjectFolder, readFolders, readLastFolder, rememberFolder, resolveNewChatFolder } from './lab/folders'
import { QuantBot, useBotFlash } from '../components/QuantBot'
import { BOT_NAME, BOT_VOICE, upgradeFlashKey } from '../components/Brand'
import { StepSlider } from '../components/StepSlider'
import { ReportCard, SubagentsCard, DataCheckBadge, ExperimentNote, MarketSwitcher } from './lab/research'
import { DiscussionCard } from '../components/chat/Discussion'
import { rd } from '../apiRd'
import type { PageProps } from './types'
import { appConfirm, appPrompt } from '../components/dialog'
import { ShareButton } from '../components/Share'
import { PresenceBar, ShareToButton, ShareToModal, usePresence } from '../components/Social'
import { QuotePicker, QuoteTray, quotePrefix, type Quote } from '../components/chat/Quotes'
import { usePlatformCommands } from '../components/chat/platformCommands'
import { lang, t } from '../i18n'

export const SYMBOL_PICKS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT', 'LINK/USDT', 'TON/USDT']
/** 设置 · 模型 里的「AI 生成简短建议」开关（本机偏好，默认开） */
export const suggestOn = () => store.get('qa.lab.suggest', '1') === '1'

const PERMISSION_ALIAS: Record<string, Permission> = {
  auto: 'auto', manual: 'manual', edits: 'accept_edits', accept: 'accept_edits', accept_edits: 'accept_edits', plan: 'plan', bypass: 'bypass',
}

const LEGACY_METRIC: Record<string, string> = { min_sharpe: 'sharpe', max_drawdown: 'max_drawdown', min_trades: 'trades', min_excess: 'excess_return' }

const SOURCE_LABEL: Record<string, string> = { ai: 'AI', fallback: '规则', user: '手动', template: '模板' }
const TOOL_ICON: Record<string, string> = {
  run_backtest: '∿', parameter_heatmap: '▦', stress_test: '⚖', cost_sweep: '≋', get_market_summary: '⇄', search_templates: '⌕', write_strategy: '✎', convene_team: '⛬',
  calculator: '∑', list_trades: '☷', trade_breakdown: '☷',
  generate: '✦', revise: '↻', explain: '?', save_strategy: '⇩', ask_user: '?',
  search_platform: '⌕', web_search: '⊕', fetch_page: '↗', set_goal: '◎', check_goals: '◎', walk_forward: '⫶',
  write_factor: 'ƒ', evaluate_factor: 'ρ', search_factor_library: '⌕', list_fields: '☰', list_versions: '☰',
  set_factor_goal: '◎', propose_factor: '✦', save_factor: '⇩',
  write_model_spec: '◇', train_model: '∿', search_model_library: '⌕', list_features: '☰', set_model_goal: '◎', save_model: '⇩',
}

/** ＋菜单里的「项目文件夹」图标（不依赖 LabControls 的类型） */
const FolderIcon = (
  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" aria-hidden>
    <path d="M2 4.5h4l1.2 1.3H14v6.7H2Z" /><path d="M10.5 8.2v3M9 9.7h3" strokeLinecap="round" />
  </svg>
)

type PanelTab = 'code' | 'versions' | 'context' | 'goals' | 'plan' | 'workflow'

const pct = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`)
const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '—' : v.toFixed(d))

export default function StrategyLabPage({ onNavigate, notify, arg, me }: PageProps & { arg?: string }) {
  const [meta, setMeta] = useState<LabMeta | null>(null)
  // 当前对话跟着网址 #/lab/<id> 走；左侧栏的对话列表在 App 里常驻（SidebarConversations）
  const activeId = arg || ''
  const [newFolder, setNewFolder] = useState('')
  const [newKey, setNewKey] = useState(0)
  const [slot, setSlot] = useState<HTMLElement | null>(null)
  useEffect(() => { setSlot(document.getElementById('sidebar-slot')) }, [])
  const refreshList = useCallback(() => labChanged(), [])

  useEffect(() => {
    lab.meta().then(setMeta).catch((e: Error) => notify(e.message, true))
  }, [notify])

  /*
   * 新对话默认放进哪个项目文件夹，优先顺序：
   * 侧栏显式传入（例：在某个项目文件夹里点 ＋）→ 最近用过（可能是明确的「未分类」）
   * → 最近一次有项目文件的对话 → 本地模式下的项目名 → 未分类。
   * 解析是异步的（要读最近对话的 folder），期间的选项变更用 seq 挡住，避免被覆盖。
   */
  const meRef = useRef(me)
  meRef.current = me
  const folderSeq = useRef(0)
  const folderResolved = useRef(false)
  // 侧栏点「新对话」时先派发事件、再改网址：旧页面（对话页）不能把 pending 文件夹吃掉，
  // 要留给重挂载后的新对话页，否则「在某个项目文件夹里新对话」会丢文件夹
  const activeRef = useRef(activeId)
  activeRef.current = activeId
  /** 用户在弹窗里手动选的：立刻生效，且压过还在路上的默认值解析 */
  const chooseNewFolder = useCallback((folder: string) => { folderSeq.current += 1; setNewFolder(folder) }, [])
  const resolveNewFolder = useCallback(async (explicit: string | null) => {
    const seq = ++folderSeq.current
    const last = readLastFolder()
    if (explicit || last !== null) {
      setNewFolder(resolveNewChatFolder({ explicit, lastFolder: last, localFolder: localProjectFolder(meRef.current) }))
      return
    }
    const recent = await lab.projects().then((r) => latestFolderOf(r.projects)).catch(() => '')
    if (seq !== folderSeq.current) return
    setNewFolder(resolveNewChatFolder({ explicit, lastFolder: last, recentProjectFolder: recent, localFolder: localProjectFolder(meRef.current) }))
  }, [])
  useEffect(() => {
    // StrictMode 下挂载 effect 会跑两次：第二次 takeNewChat() 已经是 null，
    // 不能再解析一遍（否则会把侧栏显式传入的文件夹覆盖掉）
    if (folderResolved.current) return
    folderResolved.current = true
    resolveNewFolder(takeNewChat())
  }, [resolveNewFolder])
  // 已经在研究室里时，侧栏「新对话」也要重置欢迎页（重新解析文件夹、清掉草稿）
  useEffect(() => onNewChat(() => {
    if (activeRef.current) return   // 在对话页：等路由切到 #/lab 重挂载后由新页面消费 pending 文件夹
    setNewKey((k) => k + 1)
    resolveNewFolder(takeNewChat())
  }), [resolveNewFolder])

  const open = (id: string) => { window.location.hash = id ? `/lab/${id}` : '/lab' }

  return (
    <div className="lab2 rail-closed">
      {meta && !meta.llm_ready && slot && createPortal(<a href="#/models" className="conv-foot">未配置模型，当前为规则降级 · 去配置</a>, slot)}

      {activeId ? (
        <ProjectView key={activeId} id={activeId} meta={meta} notify={notify} onNavigate={onNavigate}
          canResolveRuntime={me?.workspace?.role === 'admin' || Boolean(me?.user?.is_admin)}
          feedbackCfg={me?.workspace?.settings?.feedback}
          onChanged={refreshList} onDeleted={() => { open(''); refreshList() }} onOpenProject={(pid) => { refreshList(); open(pid) }} />
      ) : (
        <Welcome key={newKey} meta={meta} notify={notify} folder={newFolder} onFolder={chooseNewFolder}
          onCreated={(id) => { setNewFolder(''); refreshList(); open(id) }} />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 输入框的「+」菜单与斜杠命令：新对话和对话中共用同一套                   */
/* ------------------------------------------------------------------ */
const LATER = '开始对话后可用'
type Handler = (() => void) | undefined
type ArgHandler = ((arg: string) => void) | undefined

interface PlusOpts {
  ui: ReturnType<typeof domainUI>
  running?: boolean
  fileHint: string
  onFiles: () => void
  onSlash: () => void
  onPlan: () => void
  executorTeam?: boolean
  onExecutor?: () => void
  onDiscuss: () => void
  onTeam: () => void
  onNight: () => void
  onPin: () => void
  onSave: Handler
  saveLabel: string
  saveHint?: string
  onModels: () => void
  /** 引用其他对话或其中某条 AI 回答（新对话页面没有） */
  onQuote?: () => void
  /** 项目文件夹：只有新对话页传，点了打开选择弹窗 */
  onFolder?: () => void
  folder?: string
  auto: ReactNode
}

function labPlusItems(o: PlusOpts): ActionItem[] {
  const run = !!o.running
  return [
    { label: '添加文件或照片', icon: Icons.attach, hint: o.fileHint, onClick: o.onFiles },
    ...(o.onQuote ? [{ label: '引用对话或消息', icon: Icons.pin, hint: '引用其他对话、或其中某条 AI 回答', onClick: o.onQuote }] : []),
    { label: '斜杠命令', icon: Icons.slash, hint: '/backtest、/goal、/plan、/discuss…', onClick: o.onSlash },
    { label: '分步计划', icon: Icons.loop, hint: '/plan：AI 列计划并一步步做完，达标前不停', onClick: o.onPlan, disabled: !!o.running },
    ...(o.onFolder ? [{
      label: o.folder ? `项目文件夹 · ${o.folder}` : '项目文件夹',
      icon: FolderIcon, divider: true,
      hint: o.folder ? `当前：${o.folder} · 点击更换或移出` : '未分类 · 选择一个已有的或新建',
      onClick: o.onFolder,
    }] : []),
    {
      label: '插件', icon: Icons.plugin, submenu: [
        ...(o.ui && o.onExecutor ? [{ label: '研究小组执行', icon: Icons.team, hint: o.ui.teamHint || '自动迭代时由小组讨论后再实现', checked: !!o.executorTeam, onClick: o.onExecutor, disabled: run }] : []),
        { label: '团队讨论', icon: Icons.team, hint: '/discuss：多个角色发言、质疑、汇总', onClick: o.onDiscuss, disabled: run },
        { label: '快速评审', icon: Icons.team, hint: '/team：召集团队给一份评审结论', onClick: o.onTeam, disabled: run },
        { label: '定时任务', icon: Icons.loop, hint: '/schedule：加入每日任务队列，按时间自动执行，结果写进晨报', onClick: o.onNight },
        { label: '钉一段材料', icon: Icons.pin, hint: '/pin：钉进上下文，每轮都带给模型', onClick: o.onPin },
        { label: o.saveLabel, icon: Icons.save, hint: o.saveHint ?? '/save', onClick: o.onSave, disabled: run || !o.onSave },
        { label: '联网搜索与模型…', icon: Icons.search, hint: '在设置里配置', onClick: o.onModels },
      ],
    },
    { label: '自动迭代', divider: true, render: () => o.auto },
  ]
}

interface CmdOpts {
  ui: ReturnType<typeof domainUI>
  kind?: LabKind
  running?: boolean
  hasGoal: boolean
  rounds: number
  executorTeam?: boolean
  saveLabel: string
  compacting?: boolean
  backtest: Handler
  stress: ArgHandler
  sweep?: ArgHandler
  heatmap?: ArgHandler
  stressTest?: ArgHandler
  walkForward: ArgHandler
  auto: ArgHandler
  plan: ArgHandler
  night: ArgHandler
  explain: Handler
  team: ArgHandler
  discuss: ArgHandler
  crew: Handler
  pin: ArgHandler
  compact: Handler
  context: Handler
  save: Handler
  mode: ArgHandler
  report: ArgHandler
  parallel: ArgHandler
  share?: Handler
  quote?: Handler
}

/** 没有处理函数的命令（新对话里还用不了）置灰，并注明「开始对话后可用」。 */
function labCommands(o: CmdOpts): SlashCommand[] {
  const run = !!o.running
  const c = (cmd: string, group: string, help: string, fn: Handler | ArgHandler, extra: Partial<SlashCommand> = {}): SlashCommand => ({
    cmd, group, help: fn ? help : `${help}（${LATER}）`,
    run: fn ? (a: string) => (fn as (x: string) => void)(a) : undefined,
    disabled: !fn || run, ...extra,
  })
  const evalLabel = o.ui ? `${o.ui.evaluate}当前${o.ui.noun}` : '回测当前版本'
  return [
    c(o.ui ? '/eval' : '/backtest', '运行', evalLabel, o.backtest),
    ...(!o.ui || o.kind === 'factor' ? [c('/stress', '运行', o.ui ? '成本评估（默认 2×，只看，不改成绩）' : '成本压力测试（默认 2×，可写倍数，如 /stress 3）', o.stress, { arg: '[倍数]' })] : []),
    ...(!o.ui ? [c('/sweep', '运行', '成本压力扫描：并行跑多档成本倍数，找出不再满足目标的极限（可写倍数，如 /sweep 1 2 3 5）', o.sweep, { arg: '[倍数…]' })] : []),
    ...(!o.ui ? [c('/heatmap', '运行', '二维参数敏感度热力图：网格回测 X × Y 参数，识别稳健平原或孤岛尖峰（可写参数名，如 /heatmap fast slow）', o.heatmap, { arg: '[X参数 Y参数]' })] : []),
    ...(!o.ui ? [c('/overfit', '运行', '一键过拟合压力测试：滚动分段 + 参数敏感性 + 2×/3× 成本压力', o.stressTest)] : []),
    ...(!o.ui ? [c('/walkforward', '运行', '分段回测：检查是否只在某段有效', o.walkForward, { arg: '[段数]' })] : []),
    c('/auto', '迭代', o.hasGoal ? `按目标自动迭代（现在 ${o.rounds} 轮）` : '需要先设定目标（/goal）；没有目标就直接对话', o.auto,
      { arg: '[轮数]', disabled: !o.auto || run || !o.hasGoal }),
    { cmd: '/goal', group: '迭代', arg: '[描述]', help: '设定目标：指标条件、回测范围、轮数（设了目标才能自动迭代）' },
    c('/plan', '迭代', 'AI 列分步计划并一步步执行，全部完成且目标达标才停；不带内容时继续当前计划', o.plan, { arg: '[任务]', disabled: !o.plan || run }),
    c('/schedule', '迭代', '加入每日定时任务：按设定时间自动研究，结果写进晨报（旧命令 /night 仍可用）', o.night, { arg: '[想法]', disabled: !o.night }),
    c('/report', '研究', '调研报告：列提纲 → 联网与平台数据调研 → 生成可导出 PDF 的报告', o.report, { arg: '[主题]', disabled: !o.report || run }),
    c('/parallel', '研究', '唤起子 Agent 并行探究多个方向，最后并排对比', o.parallel, { arg: '[方向1 | 方向2 | …]', disabled: !o.parallel || run }),
    c('/explain', '协作', o.ui ? `解释当前${o.ui.noun}的逻辑与风险` : '逐段解释当前策略', o.explain),
    c('/team', '协作', o.ui ? '召集研究小组评审' : '召集团队评审', o.team, { arg: '[议题]' }),
    c('/discuss', '协作', '团队讨论：多个角色依次发言、相互质疑，最后汇总（发言以气泡显示在对话里）', o.discuss, { arg: '[议题]' }),
    c('/share', '协作', '把这个对话分享到群组、私聊或策略广场', o.share, { disabled: !o.share }),
    c('/quote', '协作', '引用其他对话或其中某条 AI 回答', o.quote, { disabled: !o.quote }),
    ...(o.ui ? [c('/crew', '协作', o.executorTeam ? '自动迭代改为单个 AI 执行' : '自动迭代改为研究小组执行', o.crew)] : []),
    c('/pin', '上下文', '钉一段材料到上下文', o.pin, { arg: '[内容]', disabled: !o.pin }),
    c('/compact', '上下文', '把较早的对话压缩成摘要', o.compact, { disabled: !o.compact || run || !!o.compacting }),
    c('/context', '上下文', '查看上下文用量', o.context, { disabled: !o.context }),
    c('/save', '项目', o.saveLabel, o.save),
    c('/mode', '项目', '切换权限模式', o.mode, { arg: '[auto|manual|edits|plan|bypass]', disabled: !o.mode }),
  ]
}

/** /discuss：建一场团队讨论并挂进对话（发言以气泡显示）。 */
async function startDiscussionIn(project: Pick<LabProject, 'id' | 'title' | 'idea' | 'team_id' | 'securities' | 'timeframe' | 'market_auto'>, topic: string) {
  const directive = (topic || `评审「${project.title}」：${project.idea || '当前研究'}；逻辑是否自洽、证据是否可信、最大风险在哪里、下一步怎么做`).slice(0, 600)
  const presets = await missions.presets()
  // 默认用并行的「研究评审组」：市场、成本执行、代码审计各自独立发言，研究组长汇总
  const teamId = project.team_id || presets.teams.find((t) => t.id === 'lab_review')?.id || presets.teams[0]?.id || null
  const hasMarket = !project.market_auto && project.securities.length > 0
  const v = await missions.create({
    directive, mode: 'single', team_id: teamId, lab_project_id: project.id,
    ...(hasMarket ? { symbol: project.securities[0], include_market_data: true } : { include_market_data: false }),
    timeframe: project.timeframe,
  })
  await lab.discussion(project.id, v.mission.id, directive)
}

/* ------------------------------------------------------------------ */
/* 新对话                                                              */
/* ------------------------------------------------------------------ */
/** 项目文件夹选择弹窗：已有文件夹 / 新建 / 移出（未分类）。 */
function FolderPicker({ value, folders, onPick, onClose }: {
  value: string; folders: string[]; onPick: (folder: string) => void; onClose: () => void
}) {
  const [name, setName] = useState('')
  const create = () => {
    const n = name.trim().slice(0, 40)
    if (n) onPick(n)
  }
  return (
    <Modal title="项目文件夹" onClose={onClose} width={430}>
      <p className="folder-pick-hint">新对话会放进选中的项目文件夹，之后可以在左侧栏按「项目文件夹」分组查看、移动或解散。</p>
      <div className="folder-pick-list" role="listbox" aria-label="项目文件夹">
        <button type="button" role="option" aria-selected={!value} className={`folder-pick${value ? '' : ' on'}`} onClick={() => onPick('')}>
          {FolderIcon}
          <span className="folder-pick-name">未分类</span>
          <span className="folder-pick-note">不放进任何项目文件夹</span>
        </button>
        {folders.map((f) => (
          <button key={f} type="button" role="option" aria-selected={value === f} className={`folder-pick${value === f ? ' on' : ''}`} onClick={() => onPick(f)}>
            {FolderIcon}
            <span className="folder-pick-name">{f}</span>
            {value === f && <span className="folder-pick-note">当前</span>}
          </button>
        ))}
      </div>
      {!folders.length && <div className="folder-pick-empty">还没有项目文件夹，下面输入名称就能新建。</div>}
      <div className="folder-pick-new">
        <input value={name} maxLength={40} placeholder="新建项目文件夹，例如：BTC 趋势研究" aria-label="新项目文件夹名称"
          onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') create() }} />
        <button type="button" className="button small" disabled={!name.trim()} onClick={create}>新建并选择</button>
      </div>
    </Modal>
  )
}

function Welcome({ meta, notify, onCreated, folder = '', onFolder }: {
  meta: LabMeta | null; notify: PageProps['notify']; onCreated: (id: string) => void; folder?: string
  onFolder: (folder: string) => void
}) {
  const [draft, setDraft] = useState<LabDraft | null>(peekLabDraft)   // 从别的页面带过来的想法 / 代码
  // 研究类型：五个研究室 + 完整流程 + 调研报告（流程类走策略对话）
  const [rtype, setRtypeState] = useState<ResearchType>(() => (draft?.kind ?? (store.get('qa.lab.rtype', 'auto') as ResearchType)))
  const kind = kindOfType(rtype)
  const workflow = workflowOfType(rtype)
  const setRtype = (t: ResearchType) => { setRtypeState(t); store.set('qa.lab.rtype', t); store.set('qa.lab.kind', kindOfType(t)); setSeed(Math.random()) }
  const [seed, setSeed] = useState(() => Math.random())
  const [hoverEx, setHoverEx] = useState(false)
  // 建议每 12 秒随机换一批（鼠标停在上面时不换）
  useEffect(() => {
    if (hoverEx) return
    const t = window.setInterval(() => setSeed(Math.random()), 12_000)
    return () => window.clearInterval(t)
  }, [hoverEx])
  const examples = useMemo(() => pickExamples(rtype, 4, seed), [rtype, seed])
  const ui = domainUI(kind)
  const isFactor = !!ui            // 多标的研究（因子 / 模型 / …）
  const dmeta = domainMeta(meta, kind)
  const defaults = useDefaultSpec(meta, kind)
  const [executor, setExecutor] = useState<Executor>(() => store.get('qa.lab.executor', 'solo') as Executor)
  const [universe, setUniverse] = useState('major10')
  const [idea, setIdea] = useDraft('lab:new', draft?.idea)
  const [spec, setSpec] = useState<GoalSpec>(defaults)
  const [model, setModel] = useState(() => store.get('qa.lab.model', ''))
  const [effort, setEffort] = useState<Effort>(() => store.get('qa.lab.effort', 'medium') as Effort)
  const [research, setResearch] = useState<'none' | 'market' | 'team'>('market')
  const [source, setSource] = useState<'auto' | 'exchange'>('auto')
  const [permission, setPermissionState] = useState<Permission>(() => store.get('qa.lab.permission', 'auto') as Permission)
  const setPermission = (p: Permission) => { setPermissionState(p); store.set('qa.lab.permission', p) }
  const [approvals, setApprovalsState] = useState<ApprovalCategory[]>(() => { try { return JSON.parse(store.get('qa.lab.approvals', '["delete"]')) } catch { return ['delete'] } })
  const setApprovals = (a: ApprovalCategory[]) => { setApprovalsState(a); store.set('qa.lab.approvals', JSON.stringify(a)) }
  // 标的：默认交给 AI 根据对话决定（带了标的过来、或手动选过时用手动）
  const [marketAuto, setMarketAuto] = useState(() => !draft?.securities?.length)
  const [files, setFiles] = useState<{ title: string; text: string; kind?: 'file' | 'note' }[]>([])
  const [auto, setAuto] = useState(false)
  const [busy, setBusy] = useState(false)
  const [goalOpen, setGoalOpen] = useState<{ initial: GoalSpec; unparsed?: string } | null>(null)
  const [goalSet, setGoalSet] = useState(false)   // 用 /goal 设过目标才能自动迭代；没有目标就是普通对话
  const market = usePopover()
  const metrics = metricsOf(dmeta)
  const botFx = useBotFlash()
  // 项目文件夹：当前选择由页面级状态管（folder prop），这里只管弹窗与候选列表
  const [folderOpen, setFolderOpen] = useState(false)
  const [folders, setFolders] = useState<string[]>(readFolders)
  const openFolderPicker = useCallback(() => {
    setFolderOpen(true)
    setFolders(folderOptions())
    // 顺带把对话里已经用过的文件夹也列出来（本地列表可能还没收录）
    lab.projects().then((r) => setFolders(folderOptions(r.projects.map((p) => p.folder)))).catch(() => undefined)
  }, [])
  const pickFolder = (name: string) => {
    const n = name.trim().slice(0, 40)
    if (n && !folders.includes(n)) setFolders(addFolder(n))
    onFolder(n)
    rememberFolder(n)   // 下次新对话默认沿用
    setFolderOpen(false)
    labChanged()        // 左侧栏立刻出现新文件夹
  }

  useEffect(() => {
    setSpec(draft?.securities?.length || draft?.timeframe
      ? { ...defaults, symbols: draft.securities?.length ? draft.securities : defaults.symbols, timeframe: draft.timeframe || defaults.timeframe }
      : defaults)
  }, [defaults, draft])
  useEffect(() => { clearLabDraft() }, [])
  useEffect(() => {
    const onPrefill = (e: Event) => {
      const text = (e as CustomEvent<{ text?: string }>).detail?.text
      if (text) setIdea(text)
    }
    window.addEventListener('qa:onboarding:prefill', onPrefill)
    return () => window.removeEventListener('qa:onboarding:prefill', onPrefill)
  }, [setIdea])

  const queueNight = async (text: string) => {
    setBusy(true)
    try {
      await rd.addTask({ kind, idea: text, executor: isFactor ? executor : 'solo', symbols: spec.symbols, timeframe: spec.timeframe, rounds: Math.min(8, spec.max_rounds), data_source: source })
      notify('已加入每日定时任务，到点自动研究，结果在晨报里')
      setIdea('')
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }

  const onFiles = useCallback(async (list: File[]) => {
    const texts = list.filter((f) => !f.type.startsWith('image/') && f.size <= 2_000_000)
    const read = await Promise.all(texts.map(async (f) => ({ title: f.name.slice(0, 60), text: (await f.text()).slice(0, 400_000), kind: 'file' as const })))
    if (read.length) { setFiles((prev) => [...prev, ...read]); notify(`已附带 ${read.length} 个文件，会钉进新对话的上下文`) }
    if (list.some((f) => f.type.startsWith('image/'))) notify('图片请在对话创建后再发送', true)
  }, [notify])
  const pickFiles = useFilePicker(onFiles)

  const create = async () => {
    const goal = spec
    const startAuto = auto && goalSet
    // 只附带了文件（例如超长内容自动转成的 txt）也能开始：默认请 AI 先读材料
    const text = idea.trim() || (files.length ? `请阅读我附带的「${files[0].title}」，概括要点并告诉我可以怎么研究` : '')
    if (busy || !text) return
    const [head, ...restParts] = text.split(/\s+/)
    const local = welcomeCommands.find((c) => c.cmd === head)
    if (local && (local.run || local.disabled)) {
      if (local.disabled) { notify(local.help.includes(LATER) ? `${local.cmd}：${LATER}` : `${local.cmd} 现在不可用`, true); return }
      setIdea('')
      local.run!(restParts.join(' ').trim())
      return
    }
    if (text.startsWith('/goal')) {
      const rest = text.replace(/^\/goal\s*/, '')
      setIdea('')
      if (!rest) { setGoalOpen({ initial: spec }); return }
      try {
        const r = await lab.goalParse(rest, undefined, kind)
        setGoalOpen({ initial: { ...r.spec, symbols: r.parsed_fields.includes('symbols') ? r.spec.symbols : spec.symbols }, unparsed: r.unparsed })
        setMarketAuto(!r.parsed_fields.includes('symbols') && marketAuto)
      } catch (e) { notify((e as Error).message, true) }
      return
    }
    if (text.startsWith('/schedule') || text.startsWith('/night')) {
      const rest = text.replace(/^\/(?:schedule|night)\s*/, '').trim()
      if (!rest) { notify('用法：/schedule 你的研究想法（旧命令 /night 仍可用）', true); return }
      await queueNight(rest)
      return
    }
    const planned = /^\/plan\b/.test(text)
    const planTask = planned ? text.replace(/^\/plan\s*/, '').trim() : ''
    if (planned && !planTask) { notify('用法：/plan 要完成的任务', true); return }
    const discuss = /^\/discuss\b/.test(text)
    const topic = discuss ? text.replace(/^\/discuss\s*/, '').trim() : ''
    if (discuss && !topic) { notify('用法：/discuss 讨论的议题', true); return }
    const reportCmd = /^\/(report|调研|报告)\b/.test(text)
    const reportTopic = reportCmd ? text.replace(/^\/\S+\s*/, '').trim() : ''
    if (reportCmd && !reportTopic) { notify('用法：/report 调研主题', true); return }
    const wf = reportCmd ? 'report' : workflow
    setBusy(true)
    try {
      const pins = [...(draft?.pins ?? []), ...files.map((f) => ({ kind: f.kind ?? 'file', title: f.title, text: f.text }))]
      const ideaText = discuss ? topic : planned ? planTask : reportCmd ? reportTopic : text.replace(/^\/(team|parallel)\s*/, '') || text
      const v = await lab.create({
        kind: reportCmd ? 'strategy' : kind, idea: ideaText, securities: marketAuto && !goalSet ? [] : goal.symbols, timeframe: goal.timeframe, model, effort, max_rounds: goal.max_rounds,
        bars_limit: goal.bars_limit, data_source: source, permission, approvals, market_auto: marketAuto && !goalSet, suggest: suggestOn(),
        workflow: wf, ...labPrefs(),
        ...(isFactor && !reportCmd ? { horizon: goal.horizon ?? 1, executor, universe } : {}),
        ...(draft?.title ? { title: draft.title } : {}), ...(draft?.code ? { code: draft.code } : {}), ...(pins.length ? { pins } : {}),
        ...(folder ? { folder } : {}),
      })
      const id = v.project.id
      if (goalSet) await lab.setGoal(id, goal, { review: false, start: startAuto, research: isFactor ? 'none' : research })
      if (discuss) await startDiscussionIn(v.project, topic)
      else if (planned) await lab.plan(id, planTask)
      else if (wf === 'report') await lab.report(id, reportCmd ? reportTopic : text)
      else if (wf === 'pipeline' && !startAuto) await lab.plan(id, `完整流程：${text}`)
      else if (!startAuto) await lab.send(id, text, { suggest: suggestOn() })
      setIdea('')
      onCreated(id)
      window.dispatchEvent(new CustomEvent('qa:onboarding:goal-sent', { detail: { projectId: id } }))
    } catch (e) {
      notify((e as Error).message, true)
      setBusy(false)
    }
  }

  const stripCmd = (t: string) => t.replace(/^\/\S*\s*/, '')
  const addNote = async (text?: string) => {
    const t = (text ?? (await appPrompt('钉一段材料到新对话的上下文（每轮都会带给模型）', '', { title: '钉一段材料', placeholder: '例如：只做多、单笔风险不超过 1%' })) ?? '').trim()
    if (t) { setFiles((prev) => [...prev, { title: t.replace(/\s+/g, ' ').slice(0, 24), text: t.slice(0, 4000), kind: 'note' }]); notify('已钉住，会带进新对话的上下文') }
  }
  const welcomeCommands = labCommands({
    ui, kind, hasGoal: goalSet, rounds: spec.max_rounds, executorTeam: executor === 'team', saveLabel: ui ? `存入${ui.libraryName}` : '保存为平台策略',
    backtest: undefined, stress: undefined, sweep: undefined, heatmap: undefined, stressTest: undefined, walkForward: undefined, explain: undefined, compact: undefined, context: undefined, save: undefined,
    auto: (a) => { if (Number(a)) setSpec({ ...spec, max_rounds: Math.min(8, Math.max(1, Number(a))) }); setAuto(true) },
    plan: undefined,
    night: (a) => { if (a) queueNight(a); else notify('用法：/schedule 你的研究想法（旧命令 /night 仍可用）', true) },
    team: (a) => setIdea(`/team ${a}`), discuss: (a) => setIdea(`/discuss ${a}`),
    report: (a) => setIdea(`/report ${a}`), parallel: (a) => setIdea(`/parallel ${a}`),
    crew: isFactor ? () => { const v = executor === 'team' ? 'solo' : 'team'; setExecutor(v); store.set('qa.lab.executor', v) } : undefined,
    pin: (a) => { addNote(a || undefined) },
    mode: (a) => (PERMISSION_ALIAS[a.toLowerCase()] ? setPermission(PERMISSION_ALIAS[a.toLowerCase()]) : notify('用法：/mode auto | manual | edits | plan | bypass', true)),
  }).map((c) => (['/team', '/discuss', '/plan', '/report', '/parallel'].includes(c.cmd)
    ? { ...c, run: undefined, disabled: false, help: c.help.replace(`（${LATER}）`, '') } : c)) // 这几个随第一条消息一起发出
  const plusItems = labPlusItems({
    ui,
    fileHint: '文本、CSV、JSON 会钉进新对话的上下文；图片在对话开始后发送',
    onFiles: pickFiles, onSlash: () => setIdea('/'), onPlan: () => setIdea(`/plan ${stripCmd(idea)}`),
    executorTeam: executor === 'team', onExecutor: isFactor ? () => { const v = executor === 'team' ? 'solo' : 'team'; setExecutor(v); store.set('qa.lab.executor', v) } : undefined,
    onDiscuss: () => setIdea(`/discuss ${stripCmd(idea)}`), onTeam: () => setIdea(`/team ${stripCmd(idea)}`), onNight: () => setIdea(`/schedule ${stripCmd(idea)}`),
    onPin: () => { addNote() }, onSave: undefined, saveLabel: ui ? `存入${ui.libraryName}` : '保存为平台策略', saveHint: '有代码后可用 · /save',
    onModels: () => { window.location.hash = '#/models' },
    folder, onFolder: openFolderPicker,
    auto: <AutoIterateRow rounds={spec.max_rounds} needsGoal={!goalSet} onSetGoal={() => setGoalOpen({ initial: spec })}
      onRounds={(n) => setSpec({ ...spec, max_rounds: n })} onStart={(n) => { setSpec({ ...spec, max_rounds: n }); setAuto(true) }} />,
  })
  const kinds = meta?.kinds ?? [{ id: 'strategy' as LabKind, label: '策略', hint: '' }, { id: 'factor' as LabKind, label: '因子', hint: '' }]
  const marketLabel = marketAuto ? '标的：自动' : `${isFactor ? `${spec.symbols.length} 个标的` : spec.symbols[0] || '标的'} · ${spec.timeframe}${isFactor ? ` · 持有 ${spec.horizon ?? 1} 期` : ''}`

  return (
    <section className="lab2-main">
      {/* 和对话中同一个布局：上面是内容区，输入框停靠在底部，「+」菜单、斜杠命令都向上展开 */}
      <div className="lab2-scroll lab2-welcome-scroll">
        <div className="lab2-welcome">
          <QuantBot size={52} state={busy ? 'strain' : idea.trim() ? 'thinking' : 'idle'} flash={botFx.flash} className="lab2-welcome-bot" />
          {/* 问候语与 Logo 固定不变：拖动研究类型滑条只换下面的说明与建议，页面主体不跳动 */}
          <h1>今天研究什么？</h1>
          <div className="lab2-type" aria-label="研究类型">
            <StepSlider options={RESEARCH_TYPES} value={rtype} onChange={(t) => setRtype(t)} label="研究类型" compact />
            <small className="lab2-type-hint anim-fade" key={rtype}>{RESEARCH_TYPES.find((t) => t.id === rtype)?.hint}</small>
          </div>
          <div className="lab2-examples stagger" key={`${rtype}-${seed}`} onMouseEnter={() => setHoverEx(true)} onMouseLeave={() => setHoverEx(false)}>
            {examples.map((x) => <button key={x} className="lab2-example" onClick={() => setIdea(x)}>{x}</button>)}
            <button type="button" className="lab2-example lab2-shuffle" onClick={() => setSeed(Math.random())} title="换一批建议">↻ 换一批</button>
          </div>
        </div>
      </div>
      <div className="lab2-dock" data-tour="lab-composer">
        {goalOpen && (
          <div className="lab2-goal-pop">
            <GoalForm meta={dmeta} domain={kind} initial={goalOpen.initial} unparsed={goalOpen.unparsed} onCancel={() => setGoalOpen(null)}
              submitting={busy} startLabel="保存并开启自动迭代"
              onSubmit={(g, start) => { setSpec(g); setGoalSet(true); setMarketAuto(false); setGoalOpen(null); if (start) setAuto(true) }} />
          </div>
        )}
        <Composer value={idea} onChange={setIdea} onSend={() => create()} disabled={busy} autoFocus commands={welcomeCommands} onLongText={(f) => onFiles([f])} hasAttachments={files.length > 0}
          placeholder={ui ? ui.placeholder : '描述一个想法，例如：BTC 4 小时突破 55 根新高做多，ATR 止损'}
          sendLabel={auto && goalSet ? '开始自动迭代' : '发送'}
          above={<>
            {draft?.attached && (
              <div className="cmp-attached">
                <span>已附带：{draft.attached}</span>
                <button type="button" aria-label="移除附带内容" onClick={() => setDraft(null)}>×</button>
              </div>
            )}
            {files.map((f, i) => (
              <div key={i} className="cmp-attached">
                <span>{f.kind === 'note' ? '📌' : '📎'} {f.title}</span>
                <button type="button" aria-label="移除文件" onClick={() => setFiles(files.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
            {goalSet && <GoalChips items={spec.items} metrics={metrics} onOpen={() => setGoalOpen(goalOpen ? null : { initial: spec })} />}
            {auto && goalSet && (
              <div className="cmp-attached on">
                <span>发送后自动迭代 · 最多 {spec.max_rounds} 轮</span>
                <button type="button" aria-label="取消自动迭代" onClick={() => setAuto(false)}>×</button>
              </div>
            )}
          </>}
          left={<>
            <ActionMenu items={plusItems} />
            <PermissionMenu value={permission} onChange={setPermission} approvals={approvals} onApprovals={setApprovals} />
          </>}
          right={<>
            <div className="cmp-pop-anchor" ref={market.ref}>
              <button type="button" className={`cmp-chip${marketAuto ? '' : ' on'}`} onClick={() => market.setOpen(!market.open)} aria-expanded={market.open}
                title={marketAuto ? '由 AI 根据对话决定标的与周期；点开可以手动选' : '研究类型、标的与周期'}>
                {ui ? `${ui.noun} · ` : ''}{marketLabel}
              </button>
              {market.open && (
                <div ref={market.menuRef} className="cmp-menu cmp-menu-form cmp-menu-right market-menu" style={market.menuStyle} data-placement={market.placement} role="dialog">
                  <div className="cmp-menu-label">研究类型</div>
                  <StepSlider options={RESEARCH_TYPES.filter((t) => t.id === 'pipeline' || t.id === 'report' || kinds.some((k) => k.id === t.id))}
                    value={rtype} onChange={setRtype} label="研究类型" compact />
                  <label className="market-auto">
                    <input type="checkbox" checked={marketAuto} onChange={(e) => setMarketAuto(e.target.checked)} />
                    <span>由 AI 根据对话决定标的与周期</span>
                  </label>
                  {!marketAuto && (
                    <>
                      {isFactor && meta?.universes && (
                        <label>标的池
                          <select value={universe} onChange={(e) => {
                            const u = meta.universes!.find((x) => x.id === e.target.value)
                            setUniverse(e.target.value)
                            if (u) setSpec({ ...spec, symbols: u.symbols })
                          }}>
                            {meta.universes.map((u) => <option key={u.id} value={u.id}>{u.name}（{u.symbols.length}）</option>)}
                          </select>
                        </label>
                      )}
                      <label>标的
                        <input list="lab-symbols" value={spec.symbols.join(', ')} placeholder="BTC/USDT, ETH/USDT"
                          onChange={(e) => setSpec({ ...spec, symbols: e.target.value.split(/[,，\s]+/).filter(Boolean) })} />
                        <datalist id="lab-symbols">{SYMBOL_PICKS.map((x) => <option key={x} value={x} />)}</datalist>
                      </label>
                      <label>周期
                        <select value={spec.timeframe} onChange={(e) => setSpec({ ...spec, timeframe: e.target.value })}>
                          {['250ms', '1s', '5s', '1m', '5m', '15m', '1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}
                        </select>
                      </label>
                      {isFactor && (
                        <label>持有期
                          <select value={spec.horizon ?? 1} onChange={(e) => setSpec({ ...spec, horizon: Number(e.target.value) })}>
                            {[1, 2, 3, 4, 6, 12, 24].map((n) => <option key={n} value={n}>{n} 根 K 线</option>)}
                          </select>
                        </label>
                      )}
                    </>
                  )}
                  <label>{ui ? `${ui.evaluate}数据` : '回测数据'}
                    <select value={source} onChange={(e) => setSource(e.target.value as typeof source)}>
                      <option value="auto">真实行情（缓存优先）</option><option value="exchange">只从交易所实时拉取</option>
                    </select>
                  </label>
                  {!isFactor && <label>开始前调研
                    <select value={research} onChange={(e) => setResearch(e.target.value as typeof research)}>
                      <option value="none">不调研</option><option value="market">查看行情</option><option value="team">行情 + 团队</option>
                    </select>
                  </label>}
                </div>
              )}
            </div>
            {meta && (
              <ModelMenu models={meta.models} value={model} defaultModel={meta.default_model} effort={effort} efforts={meta.efforts}
                onModel={(m) => { setModel(m); store.set('qa.lab.model', m) }}
                onEffort={(e) => { setEffort(e); store.set('qa.lab.effort', e) }} />
            )}
          </>} />
      </div>
      {folderOpen && (
        <FolderPicker value={folder}
          folders={folder && !folders.includes(folder) ? [folder, ...folders] : folders}
          onPick={pickFolder} onClose={() => setFolderOpen(false)} />
      )}
    </section>
  )
}

/* ------------------------------------------------------------------ */
/* 项目对话                                                            */
/* ------------------------------------------------------------------ */
function ProjectView({ id, meta, notify, onNavigate, onChanged, onDeleted, onOpenProject, feedbackCfg, canResolveRuntime }: {
  id: string; meta: LabMeta | null; notify: PageProps['notify']; onNavigate: PageProps['onNavigate']
  onChanged: () => void; onDeleted: () => void; onOpenProject: (id: string) => void
  feedbackCfg?: { enabled: boolean; every_n_messages: number; auto_close_seconds: number }
  canResolveRuntime: boolean
}) {
  const [project, setProject] = useState<LabProject | null>(null)
  const [messages, setMessages] = useState<LabMessage[]>([])
  const [context, setContext] = useState<ContextView | null>(null)
  const [input, setInput] = useDraft(`lab:${id}`)
  const [panel, setPanel] = useState<PanelTab | null>(null)
  const [activeWorkflow,setActiveWorkflow]=useState('')
  const [compacting, setCompacting] = useState(false)
  const [density, setDensity] = useState<'detailed' | 'concise'>(() => store.get('qa.lab.density', 'detailed') === 'concise' ? 'concise' : 'detailed')
  const [goalOpen, setGoalOpen] = useState<{ initial: GoalSpec; unparsed?: string } | null>(null)
  const [goalBusy, setGoalBusy] = useState(false)
  const [ctxAt, setCtxAt] = useState<number | undefined>()
  const [editorOpen, setEditorOpen] = useState(false)
  // 从别的对话「从此处分叉」过来时，把那条用户消息预填进输入框
  useEffect(() => {
    try {
      const k = `qa.lab.prefill.${id}`
      const v = sessionStorage.getItem(k)
      if (v) { setInput(v); sessionStorage.removeItem(k) }
    } catch { /* 存储不可用 */ }
  }, [id])
  useEffect(() => {
    const onPrefill = (e: Event) => {
      const text = (e as CustomEvent<{ text?: string }>).detail?.text
      if (text) setInput(text)
    }
    window.addEventListener('qa:onboarding:prefill', onPrefill)
    return () => window.removeEventListener('qa:onboarding:prefill', onPrefill)
  }, [setInput])
  const [rounds, setRounds] = useState<number | null>(null)
  // 回合从「运行中」变成结束：短暂显示 Bot 打勾（或报错）
  const [finishedAt, setFinishedAt] = useState(0)
  const wasRunning = useRef(false)
  const nowRunning = project?.status === 'running'
  useEffect(() => {
    if (wasRunning.current && !nowRunning) setFinishedAt(Date.now())
    wasRunning.current = nowRunning
  }, [nowRunning])
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [sendQueue, setSendQueue] = useState<{ text: string; images: Attachment[] }[]>([])
  const [queueTick, setQueueTick] = useState(0)
  const sendingQueued = useRef(false)
  const cursor = useRef({ after: 0, since: 0 })
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const following = useRef<AbortController | null>(null)
  const panelInit = useRef(false)
  const knownSubagents = useRef(new Set<string>())
  const [ratingPrompt, setRatingPrompt] = useState<number | null>(null)
  // 从临时问答升级过来：读取一次性提示（Ask 面板写入），展示后清掉，8 秒自动收起
  const [upgradeFlash, setUpgradeFlash] = useState('')
  useEffect(() => {
    try {
      const key = upgradeFlashKey(id)
      const v = sessionStorage.getItem(key)
      if (v) { setUpgradeFlash(v); sessionStorage.removeItem(key) }
    } catch { /* 存储不可用 */ }
  }, [id])
  useEffect(() => {
    if (!upgradeFlash) return
    const t = window.setTimeout(() => setUpgradeFlash(''), 8000)
    return () => window.clearTimeout(t)
  }, [upgradeFlash])
  // P4：后台 run 的实时状态（用现有只读接口探测），刷新 / 切回页面后也能恢复未完成状态
  const [liveEvidence, setLiveEvidence] = useState<Record<string, RunEvidenceState>>({})
  const agentStatus = useMemo(
    () => deriveAgentStatus({ project, messages, projectId: id, domain: project?.kind, live: liveEvidence, queueCount: sendQueue.length }),
    [project, messages, id, liveEvidence, sendQueue.length],
  )
  // 用签名字符串做依赖，避免每次渲染都重开探测轮询
  const probeSignature = agentStatus.evidence
    .filter((e) => e.probe && ['queued', 'running', 'waiting', 'canceling'].includes(e.state))
    .map((e) => e.key)
    .join('|')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const probes = useMemo(() => evidenceProbes(agentStatus.evidence), [probeSignature])

  const apply = useCallback((v: { project: LabProject; messages: LabMessage[]; server_time: number; context?: ContextView }, full = false) => {
    setProject(v.project)
    if (v.context) { setContext(v.context); setCtxAt(Date.now()) }
    setMessages((prev) => (full ? v.messages : mergeBySeq(prev, v.messages)))
    const maxSeq = v.messages.reduce((a, m) => Math.max(a, m.seq), full ? 0 : cursor.current.after)
    cursor.current = { after: maxSeq, since: v.server_time }
    const kids = v.messages.flatMap((m) => (m.kind === 'subagents' ? (m.data?.children || []) : []))
    const fresh = kids.filter((c: { id?: string }) => c.id && !knownSubagents.current.has(c.id))
    if (fresh.length) {
      fresh.forEach((c: { id?: string }) => c.id && knownSubagents.current.add(c.id))
      onChanged()
    }
  }, [onChanged])

  const pull = useCallback(async (full = false) => {
    const c = full ? { after: 0, since: 0 } : cursor.current
    const v = await lab.view(id, c.after, c.since)
    apply(v, full)
    return v
  }, [id, apply])

  /** 跟随一个回合：SSE 推送到回合结束；推送失败时退回每秒拉取。 */
  const follow = useCallback(() => {
    if (following.current) return
    const ac = new AbortController()
    following.current = ac
    let status = ''
    const run = async () => {
      try {
        for (let i = 0; i < 20 && !ac.signal.aborted; i++) {
          await lab.events(id, cursor.current.after, cursor.current.since, (v) => { status = v.project.status; apply(v) }, ac.signal)
          if (status !== 'running') break
        }
      } catch {
        while (!ac.signal.aborted) {
          const v = await pull().catch(() => null)
          if (v && v.project.status !== 'running') break
          await new Promise((r) => setTimeout(r, 1000))
        }
      } finally {
        if (following.current === ac) following.current = null
        if (!ac.signal.aborted) { pull().catch(() => undefined); onChanged() }
      }
    }
    run()
  }, [id, apply, pull, onChanged])

  useEffect(() => {
    pull(true).then((v) => { if (v.project.status === 'running') follow() }).catch((e: Error) => notify(e.message, true))
    return () => { following.current?.abort(); following.current = null }
  }, [pull, follow, notify])

  // 长任务恢复：后台 run 还在排队 / 运行时，用现有只读接口轮询到终态（刷新、切会话、离开再回来都适用）
  useEffect(() => {
    if (!probes.length) return
    let alive = true
    const read = () => {
      for (const probe of probes) {
        get<Record<string, any>>(probe.url).then((v) => {
          if (!alive) return
          const next = probe.kind === 'team' ? normalizeTeamState(v?.run?.status) : normalizeRunState(probe.kind === 'workflow' ? v?.state : (v?.run?.state ?? v?.state))
          setLiveEvidence((prev) => (prev[probe.key] === next ? prev : { ...prev, [probe.key]: next }))
          if (next === 'done' || next === 'failed' || next === 'canceled') { pull().catch(() => undefined); onChanged() }
        }).catch(() => undefined)
      }
    }
    read()
    const timer = window.setInterval(read, 4000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [probes, pull, onChanged])

  // 切回页面 / 窗口重新聚焦：补一次增量，仍在跑就继续跟 SSE（离开期间流可能已经关闭）
  useEffect(() => {
    const onVisible = () => {
      if (document.hidden) return
      pull().then((v) => { if (v.project.status === 'running') follow() }).catch(() => undefined)
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
    }
  }, [pull, follow])

  // 把「这一轮失败了」带到侧栏列表（只在后端 updated_at 没有变化时有效，重试后会自然消失）
  useEffect(() => {
    if (!project?.id) return
    if (agentStatus.state === 'failed') rememberObservedFailure(project, agentStatus.failure || '上一轮执行失败')
    else clearObservedFailure(project.id)
    if (agentStatus.state === 'failed' || agentStatus.state === 'queued') onChanged()
  }, [project?.id, project?.updated_at, agentStatus.state, agentStatus.failure, onChanged])

  // 打开 / 切换已有对话时记住它的项目文件夹：下一次新对话默认沿用（含明确的「未分类」）
  useEffect(() => {
    if (project) rememberFolder(project.folder || '')
  }, [project?.id, project?.folder])

  // 有代码且屏幕够宽时，第一次打开项目默认展开右侧代码面板
  useEffect(() => {
    if (panelInit.current || !project) return
    panelInit.current = true
    const saved = store.get('qa.lab.panel', '')
    if (saved === 'none') return
    if (window.innerWidth >= 1500) setPanel((saved as PanelTab) || 'code')
  }, [project])

  useEffect(() => {
    const el = scroller.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [messages])

  // 结构化内容块里的按钮（选项、确认、表单、「让 AI 解读」）把一句话发回这段对话
  const askRef = useRef<((text: string) => void) | null>(null)
  useEffect(() => onChatAsk((text) => askRef.current?.(text)), [])

  const openPanel = (t: PanelTab | null) => { setPanel(t); store.set('qa.lab.panel', t ?? 'none') }
  const openEditor = () => setEditorOpen(true)

  /** full：编辑 / 重试会删掉后面的消息，要整段重新拉，不能只合并增量 */
  const act = async (fn: () => Promise<unknown>, full = false) => {
    try {
      await fn()
      await pull(full)
      follow()
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const dispatchSend = async (raw: string, imgs: Attachment[], fromQueue = false) => {
    const t = raw.trim()
    if ((!t && !imgs.length) || !project) return
    const [head, ...restParts] = t.split(/\s+/)
    const local = commands.find((c) => c.cmd === head && c.run)
    if (local) {
      if (!fromQueue) setInput('')
      if (local.disabled) { notify(`${local.cmd} 现在不可用`, true); return }
      local.run!(restParts.join(' ').trim())
      return
    }
    if (/^\/goal\b/.test(t)) {
      if (!fromQueue) setInput('')
      const rest = t.replace(/^\/goal\s*/, '')
      if (!rest) { setGoalOpen({ initial: projectSpec(project) }); return }
      try {
        const r = await lab.goalParse(rest, id)
        setGoalOpen({ initial: r.spec, unparsed: r.unparsed })
      } catch (e) { notify((e as Error).message, true) }
      return
    }
    if (!fromQueue) setInput('')
    stick.current = true
    if (!imgs.length) { await act(() => lab.send(id, t, { suggest: suggestOn() })); return }
    if (!fromQueue) setAttachments([])
    try {
      await lab.send(id, t, { suggest: suggestOn(), images: imgs.map((a) => ({ data_url: a.url, name: a.name })) })
      await pull()
      follow()
    } catch (e) {
      if (!fromQueue) {
        setAttachments(imgs) // 发送失败：附件留在输入框里，可以换模型后重发
        setInput(t)
      }
      notify((e as Error).message, true)
    }
  }

  const send = async (text?: string) => {
    const raw = (text ?? input).trim()
    const imgs = text === undefined ? attachments : []
    if ((!raw && !imgs.length) || !project) return
    // 引用：拼成 Markdown 引用块放在消息前面（整个对话只给 id，AI 自己用 read_conversation 读）
    const quoted = quotes.length ? quotePrefix(quotes, id) + raw : raw
    if (running) {
      setSendQueue((q) => [...q, { text: quoted, images: imgs }])
      setInput('')
      setAttachments([])
      setQuotes([])
      notify(`已加入发送队列（${sendQueue.length + 1} 条），当前任务结束后自动发送`)
      return
    }
    if (quotes.length) setQuotes([])
    await dispatchSend(quoted, imgs)
  }

  const patch = (p: Partial<LabProject>) => act(() => lab.update(id, p))

  const saveGoal = async (spec: GoalSpec, start: boolean) => {
    setGoalBusy(true)
    stick.current = true
    await act(async () => {
      const r = await lab.setGoal(id, spec, { start, review: !start })
      setGoalOpen(null)
      if (!start && r.goal.check.verdict !== 'ok') notify(r.goal.check.summary)
    })
    setGoalBusy(false)
  }

  const compact = async () => {
    setCompacting(true)
    try { apply(await lab.compact(id)) } catch (e) { notify((e as Error).message, true) } finally { setCompacting(false) }
  }

  const imageModel = meta?.models.find((m) => m.target === (project?.model || meta.default_model))
  const imageBlock = project && domainUI(project.kind)
    ? '这类研究的对话暂不支持图片，请在策略研究里使用'
    : !imageModel?.inputs?.includes('image')
      ? `当前模型${imageModel ? `「${imageModel.label || imageModel.model}」` : ''}未开启图片输入：可在 设置 → 模型 里为它勾选「图片」，或换一个支持图片的模型`
      : ''
  const onImages = useCallback(async (images: File[]) => {
    if (imageBlock) { notify(imageBlock, true); return }
    const room = MAX_IMAGES - attachments.length
    if (images.length > room) notify(`一条消息最多带 ${MAX_IMAGES} 张图片，多出的已跳过`, true)
    const added: Attachment[] = []
    for (const f of images.slice(0, Math.max(0, room))) {
      if (!IMAGE_TYPES.includes(f.type)) { notify(`${f.name} 不是 PNG / JPEG / GIF / WebP，已跳过`, true); continue }
      if (f.size > MAX_IMAGE_BYTES) { notify(`${f.name} 太大（超过 5 MB），已跳过`, true); continue }
      added.push({ key: `${Date.now()}-${Math.random()}`, name: f.name || '粘贴的图片', url: await readDataUrl(f) })
    }
    if (added.length) setAttachments((prev) => [...prev, ...added].slice(0, MAX_IMAGES))
  }, [imageBlock, attachments.length, notify])

  const onFiles = useCallback(async (files: File[]) => {
    const images = files.filter((f) => f.type.startsWith('image/'))
    const texts = files.filter((f) => !f.type.startsWith('image/'))
    for (const f of texts) {
      if (f.size > 2_000_000) { notify(`${f.name} 太大（超过 2 MB），已跳过`, true); continue }
      const text = await f.text()
      try {
        await lab.pin(id, { kind: 'file', title: f.name.slice(0, 60), text: text.slice(0, 400_000) })
        notify(text.length > 2000 ? `已附带 ${f.name}（${text.length.toLocaleString()} 字，AI 会按需分段读取全文）` : `已钉住 ${f.name}`)
      } catch (e) { notify((e as Error).message, true) }
    }
    if (images.length) await onImages(images)
    if (texts.length) pull().catch(() => undefined)
  }, [id, notify, pull, onImages])
  const pickFiles = useFilePicker(onFiles)

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const panelSpec = useMemo(() => (project ? projectSpec(project) : null), [project?.updated_at, project?.id])
  const [quotes, setQuotes] = useState<Quote[]>([])
  const [quotePicking, setQuotePicking] = useState(false)
  const [sharing, setSharing] = useState(false)
  const platformCmds = usePlatformCommands([])
  const addQuote = (q: Quote) => { setQuotes((prev) => (prev.some((x) => x.key === q.key) ? prev : [...prev, q].slice(-6))); setQuotePicking(false) }
  // 多人协作：谁也在看这个对话；有人在输入（草稿非空）时显示为「正在编辑」
  const presence = usePresence('project', id, !!input.trim())
  // 浏览器标题用对话名：从对话跳到其他页面时，「返回对话」能写出是哪个对话
  useEffect(() => { if (project?.title) document.title = `${project.title} · QuantLab v0.1.0-preview.1` }, [project?.title])
  // 必须在任何条件 return 之前注册，避免 hooks 数量在加载前后不一致（React #310）。
  useEffect(() => {
    if (nowRunning || sendingQueued.current || !sendQueue.length) return
    sendingQueued.current = true
    const next = sendQueue[0]
    setSendQueue((q) => q.slice(1))
    void dispatchSend(next.text, next.images, true).finally(() => {
      sendingQueued.current = false
      setQueueTick((t) => t + 1)
    })
  }, [nowRunning, sendQueue, queueTick]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!feedbackCfg?.enabled || nowRunning) return
    const done = messages.filter((m) => m.role === 'assistant' && m.kind === 'text' && m.content && !m.data?.streaming)
    const last = done[done.length - 1]
    if (!last) return
    const every = Math.max(1, Number(feedbackCfg.every_n_messages) || 10)
    if (done.length % every !== 0) return
    try {
      const key = `qa.lab.rating.${id}`
      if (Number(localStorage.getItem(key) || 0) >= last.seq) return
    } catch { /* 私密模式 */ }
    setRatingPrompt(last.seq)
  }, [feedbackCfg?.enabled, feedbackCfg?.every_n_messages, messages, nowRunning, id])

  if (!project || !panelSpec) return <section className="lab2-main"><Loading label="正在打开项目" /></section>

  const running = project.status === 'running'
  // 失败 / 排队横幅里的「查看运行」入口：只跳真实存在的运行证据
  const evidenceLink = [...agentStatus.evidence].reverse().find((e) => e.href)

  const ui = domainUI(project.kind)
  const isFactor = !!ui
  const isModel = project.kind === 'model'
  const libId = libraryIdOf(project, ui)
  const dmeta = domainMeta(meta, project.kind)
  const saveOut = () => act(async () => {
    if (project.kind === 'factor') { const r = await lab.saveFactor(id); notify(`已存入因子库「${r.factor.name}」`) } else if (ui) {
      const r = await lab.saveDomain(id); const rec = Object.values(r)[0]; notify(`已存入${ui.libraryName}「${rec?.name ?? ''}」`)
    } else {
      const r = await lab.saveStrategy(id); notify(`已保存为策略「${r.strategy.name}」`)
    }
  })
  const roundsNow = rounds ?? project.max_rounds
  const hasGoal = !!project.goal?.items?.length || !!project.goal_set
  const startAuto = (n: number) => {
    if (project.market_auto || !project.securities.length) { notify('请先设置标的与周期，再开始自动迭代', true); return }
    if (!hasGoal) { setGoalOpen({ initial: projectSpec(project) }); return }
    act(async () => {
      if (n !== project.max_rounds) await lab.update(id, { max_rounds: n })
      await lab.autopilot(id, { rounds: n })
    })
  }
  const setPermission = (p: Permission) => { patch({ permission: p }); notify(`权限模式：${({ auto: '自动', manual: '手动', accept_edits: '接受编辑', plan: '计划', bypass: '完全权限' } as Record<Permission, string>)[p]}`) }
  const setApprovals = (a: ApprovalCategory[]) => patch({ approvals: a })
  const noCode = !project.code
  const startDiscussion = (topic: string) => act(async () => {
    await startDiscussionIn(project, topic)
    stick.current = true
  })
  const queueNight = (idea: string) => {
    if (project.market_auto || !project.securities.length) { notify('请先设置标的与周期，再加入定时任务', true); return }
    act(async () => {
      await rd.addTask({
        kind: (project.kind ?? 'strategy') as 'strategy', idea: (idea || project.idea || project.title).slice(0, 2000),
        executor: project.executor ?? 'solo', symbols: project.securities, timeframe: project.timeframe,
        rounds: Math.min(8, project.max_rounds), data_source: project.data_source,
      })
      notify('已加入每日定时任务：到点自动研究，结果写进晨报')
    })
  }
  const saveLabel = ui ? `${libId ? '更新' : '存入'}${ui.libraryName}` : (project.slug ? '更新平台策略' : '保存为平台策略')
  const toggleCrew = () => patch({ executor: project.executor === 'team' ? 'solo' : 'team' })
  const commands = labCommands({
    ui, kind: project.kind, running, hasGoal, rounds: roundsNow, executorTeam: project.executor === 'team', saveLabel, compacting,
    backtest: noCode ? undefined : () => act(() => lab.backtest(id)),
    stress: noCode ? undefined : (a) => act(() => lab.backtest(id, Math.min(20, Math.max(0.5, Number(a) || 2)))),
    sweep: noCode ? undefined : (a) => act(() => lab.costSweep(id, (a.match(/\d+(?:\.\d+)?/g) || []).map(Number).slice(0, 8))),
    heatmap: noCode ? undefined : (a) => {
      const names = a.split(/\s+/).filter((x) => /^[A-Za-z_]\w*$/.test(x))
      return act(() => lab.parameterHeatmap(id, { x_param: names[0] || '', y_param: names[1] || '' }))
    },
    stressTest: noCode ? undefined : (a) => {
      const nums = (a.match(/\d+(?:\.\d+)?/g) || []).map(Number)
      return act(() => lab.stressTest(id, nums.length ? { segments: Math.max(2, Math.min(6, nums[0])) } : {}))
    },
    walkForward: noCode ? undefined : (a) => act(() => lab.walkForward(id, Math.min(6, Math.max(2, Number(a) || 3)))),
    auto: (a) => startAuto(Math.min(8, Math.max(1, Number(a) || roundsNow))),
    plan: (a) => { stick.current = true; act(() => lab.plan(id, a)) },
    night: (a) => queueNight(a),
    explain: noCode ? undefined : () => send('/explain'),
    team: (a) => send(a ? `/team ${a}` : '/team'),
    discuss: (a) => startDiscussion(a),
    crew: toggleCrew,
    pin: (a) => (a ? act(() => lab.pin(id, { kind: 'note', title: a.slice(0, 24), text: a })) : openPanel('context')),
    compact: () => compact(),
    context: () => openPanel('context'),
    save: noCode ? undefined : () => saveOut(),
    mode: (a) => (PERMISSION_ALIAS[a.toLowerCase()] ? setPermission(PERMISSION_ALIAS[a.toLowerCase()]) : notify('用法：/mode auto | manual | edits | plan | bypass', true)),
    report: (a) => { stick.current = true; act(() => lab.report(id, a)) },
    parallel: (a) => send(`/parallel ${a}`.trim()),
    share: isLocalDeployment() ? undefined : () => setSharing(true),
    quote: () => setQuotePicking(true),
  })
  const plusItems = labPlusItems({
    ui, running,
    fileHint: imageBlock ? '文本、CSV、JSON 会钉进上下文（当前模型不收图片）' : '文本钉进上下文，图片随下一条消息发给模型',
    onFiles: pickFiles, onSlash: () => setInput('/'), onPlan: () => setInput('/plan '),
    executorTeam: project.executor === 'team', onExecutor: toggleCrew,
    onDiscuss: () => startDiscussion(''), onTeam: () => send('/team'), onNight: () => queueNight(''),
    onPin: () => openPanel('context'), onSave: noCode ? undefined : saveOut, saveLabel, saveHint: noCode ? '有代码后可用 · /save' : '/save',
    onQuote: () => setQuotePicking(true),
    onModels: () => onNavigate('models'),
    auto: <AutoIterateRow rounds={roundsNow} disabled={running} needsGoal={!hasGoal} onSetGoal={() => setGoalOpen({ initial: projectSpec(project) })}
      onRounds={(n) => { setRounds(n); lab.update(id, { max_rounds: n }).catch(() => undefined) }} onStart={startAuto} />,
  })
  const forkAt = async (m: LabMessage) => {
    try {
      // 用户消息：分叉到它之前，并把它预填进新对话的输入框；AI 消息：分叉到它为止
      const r = await lab.fork(id, m.role === 'user' ? Math.max(1, m.seq - 1) : m.seq)
      if (m.role === 'user') { try { sessionStorage.setItem(`qa.lab.prefill.${r.project.id}`, m.content) } catch { /* 忽略 */ } }
      notify('已分叉出新对话')
      onOpenProject(r.project.id)
    } catch (e) { notify((e as Error).message, true) }
  }
  const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant' && m.kind === 'text')?.seq
  const msgActions: MessageActions = {
    running,
    notify,
    lastSeq: lastAssistant,
    onSuggest: (text) => { if (text.startsWith('/')) setInput(text + ' '); else send(text) },
    onPinText: (title, text) => act(async () => { await lab.pin(id, { kind: 'team', title, text: text.slice(0, 4000) }); notify('已钉进上下文') }),
    onEdit: (m, text) => { stick.current = true; act(() => lab.rewind(id, m.seq, text), true) },
    onRetry: (m) => { stick.current = true; act(() => lab.rewind(id, m.seq), true) },
    onFork: forkAt,
    onPin: (m) => act(async () => { await lab.pin(id, { kind: 'note', title: m.content.replace(/\s+/g, ' ').slice(0, 24), text: m.content.slice(0, 4000) }); notify('已钉进上下文') }),
    onCopy: (m) => { navigator.clipboard?.writeText(m.content).then(() => notify('已复制'), () => notify('复制失败', true)) },
    onQuote: (m) => addQuote({ key: `m:${id}:${m.seq}`, pid: id, title: project.title, seq: m.seq, text: m.content }),
    onFeedback: (m, rating) => {
      lab.feedback(id, m.seq, rating).then((r) => {
        setMessages((prev) => prev.map((x) => (x.seq === m.seq ? { ...x, feedback: r.feedback ?? undefined } : x)))
        notify(r.feedback ? (rating === 1 ? '已点赞' : '已点踩') : '已取消评价')
      }).catch((e) => notify((e as Error).message, true))
    },
    onRenderErrors: (m, errors) => { stick.current = true; lab.renderErrors(id, m.seq, errors).then(() => pull(false)).catch(() => undefined) },
  }
  askRef.current = (text) => send(text)

  const changes = changeStats(project)
  const visibleMessages = density === 'concise'
    ? messages.filter((m) => m.role === 'user' || m.role === 'assistant' || m.kind === 'error' || m.kind === 'ask' || m.kind === 'goal')
    : messages
  const hiddenMessages = messages.length - visibleMessages.length
  const lastAsk = [...messages].reverse().find((m) => m.kind === 'ask')
  const streamingNow = messages.some((m) => m.data?.streaming)
  const finishRating = (seq: number) => {
    try { localStorage.setItem(`qa.lab.rating.${id}`, String(seq)) } catch { /* 私密模式 */ }
    setRatingPrompt(null)
  }

  return (
    <>
      <section className="lab2-main">
        <header className="lab2-head">
          <span className="lab2-bot" title={BOT_NAME}>
            <QuantBot size={18} state={project.status === 'running' ? 'thinking' : agentStatus.state === 'failed' ? 'error' : 'idle'} />
          </span>
          <div className="lab2-title-row">
            <input className="lab2-title" defaultValue={project.title} aria-label="项目名称"
              onBlur={(e) => e.target.value.trim() && e.target.value !== project.title && patch({ title: e.target.value.trim() })} />
            {project.workflow && <span className="cb-badge lab2-head-tag">{WORKFLOW_LABEL[project.workflow] || project.workflow}</span>}
            {project.origin === 'ask_upgrade' && <span className="cb-badge lab2-head-tag lab2-origin" title={BOT_VOICE.upgraded}>{BOT_VOICE.upgradedTag}</span>}
            <AgentStatePill status={agentStatus} />
          </div>
          <span className="lab2-sub">
            {isFactor
              ? <>{ui!.noun}研究 · {project.market_auto || !project.securities.length ? '标的自动' : `${project.securities.length} 个标的`} · {project.timeframe} · 持有 {project.horizon ?? 1} 期 · {project.executor === 'team' ? '研究小组' : '单个 AI'}</>
              : null}
            {project.subagent_of && <> · <a href={`#/lab/${project.subagent_of}`}>子 Agent · 返回主对话</a></>}
            {project.slug && <> · <a href={`#/editor/${project.slug}`}>已存为策略</a></>}
            {ui && libId && <> · <a href={`#/${ui.libraryRoute(libId)}`}>已存入{ui.libraryName}</a></>}
          </span>
          <span className="lab2-head-gap" />
          <div className="lab2-density" role="group" aria-label="对话视图">
            <button type="button" className={density === 'detailed' ? 'on' : ''} aria-pressed={density === 'detailed'}
              onClick={() => { setDensity('detailed'); store.set('qa.lab.density', 'detailed') }}>详细</button>
            <button type="button" className={density === 'concise' ? 'on' : ''} aria-pressed={density === 'concise'}
              onClick={() => { setDensity('concise'); store.set('qa.lab.density', 'concise') }}>简略</button>
          </div>
          <MarketSwitcher symbols={project.securities} timeframe={project.timeframe} auto={!!project.market_auto || !project.securities.length} disabled={running} multi
            onChange={(securities, timeframe) => {
              patch({ securities, timeframe, market_auto: securities.length === 0 })
              notify(securities.length ? `标的改为 ${securities.join(', ')} · ${timeframe}` : '标的与周期改为自动')
            }}
            notify={notify} />
          <PresenceBar others={presence} />
          <ShareButton type="project" id={id} notify={notify} compact />
          <ShareToButton item={{ type: 'project', id, title: project.title }} notify={notify} onNavigate={onNavigate} />
          <button type="button" className={`lab2-sessbtn${panel ? ' on' : ''}`} aria-pressed={!!panel} onClick={() => openPanel(panel ? null : 'code')}
            title="会话栏：更改、迭代、上下文、目标">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden><rect x="2" y="2.5" width="12" height="11" rx="2" /><path d="M10 2.5v11" /></svg>
            {changes.add + changes.del > 0 && <span className="lab2-sessbtn-diff"><b className="add">+{changes.add}</b><b className="del">−{changes.del}</b></span>}
          </button>
          <ProjectMenu onDelete={async () => await appConfirm(ui ? `删除这个${ui.noun}研究？已存入${ui.libraryName}的内容不受影响。` : '删除这个策略项目？已保存的平台策略不受影响。') && lab.remove(id).then(onDeleted)}
            onSave={saveOut} hasCode={!!project.code} saved={!!(project.slug || libId)}
            saveLabel={ui ? `${libId ? '更新' : '存入'}${ui.libraryName}` : undefined} />
        </header>

        {/* 从临时问答升级过来的一次性提示：说明这段会话的来源与已保存状态 */}
        {upgradeFlash && (
          <div className="lab-upgrade-flash" role="status">
            <QuantBot size={16} state="happy" title={BOT_NAME} />
            <span>{upgradeFlash}</span>
            <button type="button" onClick={() => setUpgradeFlash('')} aria-label="收起提示" title="收起">×</button>
          </div>
        )}

        {/* P4：五态里需要用户动作的两种（失败 / 排队、后台仍在跑）在页头下方给出一条任务横幅 */}
        {agentStatus.state === 'failed' && (
          <div className="lab-task-banner failed" role="alert">
            <b>执行失败</b>
            <span className="lab-task-banner-detail" title={agentStatus.failure}>{agentStatus.failure || '上一轮没有完成'}</span>
            {evidenceLink && <a className="button small" href={evidenceLink.href}>查看运行</a>}
            <div className="lab-task-banner-actions">
              <button type="button" className="button small primary" disabled={running}
                onClick={() => { stick.current = true; send('继续') }}>继续</button>
              <button type="button" className="button small" disabled={running}
                onClick={() => { stick.current = true; send('重试上一轮：修复刚才失败的原因，继续完成原目标') }}>重试上一轮</button>
            </div>
          </div>
        )}
        {agentStatus.state !== 'failed' && (agentStatus.state === 'queued' || (agentStatus.state === 'generating' && !running)) && (
          <div className={`lab-task-banner${agentStatus.state === 'queued' ? ' queued' : ''}`} role="status">
            <b>{agentStatus.label}</b>
            <span className="lab-task-banner-detail" title={agentStatus.detail}>{agentStatus.detail}</span>
            {evidenceLink && <a className="button small" href={evidenceLink.href}>查看运行</a>}
            <div className="lab-task-banner-actions">
              <button type="button" className="button small" disabled={running}
                onClick={() => { stick.current = true; send('继续') }}>继续</button>
            </div>
          </div>
        )}

        <div className="lab2-scroll" ref={scroller}
          onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }}>
          <div className="lab2-thread">
            {density === 'concise' && hiddenMessages > 0 && (
              <div className="lab2-density-note">简略视图已隐藏 {hiddenMessages} 条工具与过程记录，切换回「详细」可查看</div>
            )}
            {visibleMessages.map((m) => (
              <MessageView key={m.seq} m={m} project={project} meta={dmeta} active={m === lastAsk && project.status === 'awaiting_user'} actions={msgActions}
                liveEvidence={liveEvidence}
                onOpenWorkflow={(wid)=>{setActiveWorkflow(wid);openPanel('workflow')}}
                concise={density === 'concise'}
                onDecide={(choice, note) => act(() => lab.decide(id, choice, note))}
                onOpenBacktest={(btId) => onNavigate(`backtest/${btId}`)} onOpenEditor={(slug) => onNavigate(`editor/${slug}`)}
                onOpenCode={openEditor} onOpenLibrary={(route) => onNavigate(route)} />
            ))}
            {running && !streamingNow && <Working phase={project.job?.phase} detail={project.job?.detail} startedAt={project.job?.started_at} />}
            {!running && finishedAt > 0 && (
              <div className="cb-finish" key={finishedAt}>
                <QuantBot size={22} state={agentStatus.state === 'failed' ? 'error' : 'happy'} />
                <span>
                  {agentStatus.state === 'failed' ? '这一轮出了问题，见上方说明'
                    : project.status === 'awaiting_user' ? '等你回答'
                      : agentStatus.artifacts ? '这一轮完成了，运行证据见上方标签'
                        : '这一轮已结束（只有文字回复）'}
                </span>
              </div>
            )}
            {!messages.length && !running && (
              <div className="lab2-empty lab2-empty-bot">
                <QuantBot size={26} state="idle" title={BOT_NAME} />
                <span>{BOT_VOICE.emptyLab}</span>
              </div>
            )}
          </div>
        </div>

        <div className="lab2-dock" data-tour="lab-composer">
          <RuntimeRecovery projectId={id} runId={project.runtime_pending_ids?.[0] || project.runtime_chat_id} running={running}
            revision={project.updated_at} canResolve={canResolveRuntime} onChanged={() => pull(false)}
            onError={(message) => notify(message, true)} />
          {goalOpen && (
            <div className="lab2-goal-pop">
              <GoalForm meta={dmeta} domain={project.kind} initial={goalOpen.initial} unparsed={goalOpen.unparsed} projectId={id}
                submitting={goalBusy || running} onCancel={() => setGoalOpen(null)} onSubmit={saveGoal} />
            </div>
          )}
          <Composer value={input} onChange={setInput} onSend={() => send()} running={running} onStop={() => act(() => lab.stop(id))} onLongText={(f) => onFiles([f])}
            commands={[...commands, ...platformCmds.filter((p) => !commands.some((c) => c.cmd === p.cmd))]}
            placeholder={running ? '正在处理；输入消息会加入队列，完成后自动发送' : ui ? ui.replyPlaceholder : '回复…'}
            hasAttachments={attachments.length > 0}
            queueCount={sendQueue.length}
            onPasteFiles={(files) => onFiles(files)}
            above={(goalOpen || (running && project.job?.kind === 'autopilot') || attachments.length || quotes.length || sendQueue.length) ? (<>
              {sendQueue.length > 0 && (
                <div className="lab-send-queue">
                  <span><b>待发送 {sendQueue.length} 条</b> · 当前任务结束后自动发送</span>
                  <small>{sendQueue[0].text.replace(/^> .*\n\n/, '').slice(0, 80)}</small>
                  <button type="button" className="link" onClick={() => setSendQueue([])}>清空队列</button>
                </div>
              )}
              <QuoteTray quotes={quotes} onRemove={(key) => setQuotes((prev) => prev.filter((q) => q.key !== key))} />
              {attachments.length > 0 && (
                <AttachTray items={attachments} warning={imageBlock}
                  onRemove={(key) => setAttachments((prev) => prev.filter((a) => a.key !== key))} />
              )}
              {(goalOpen || (running && project.job?.kind === 'autopilot')) && (
                <GoalChips items={projectItems(project)} metrics={metricsOf(dmeta)}
                  onOpen={() => setGoalOpen(goalOpen ? null : { initial: projectSpec(project) })} />
              )}
            </>) : undefined}
            left={<>
              <ActionMenu items={plusItems} />
              <PermissionMenu value={project.permission} onChange={setPermission} disabled={running} approvals={project.approvals} onApprovals={setApprovals} />
            </>}
            right={<>
              {meta && (
                <ModelMenu models={meta.models} value={project.model} defaultModel={meta.default_model}
                  effort={project.effort || 'medium'} efforts={meta.efforts}
                  onModel={(m) => patch({ model: m })} onEffort={(e) => patch({ effort: e })} />
              )}
              <ContextRing context={context} updatedAt={ctxAt} onCompact={compact} compacting={compacting} onManage={() => openPanel('context')} />
            </>} />
        </div>
      </section>

      {panel && (
        <aside className="lab2-panel" aria-label="会话栏">
          <div className="lab2-panel-head">
            <b>会话</b>
            <button className="lab2-icon" onClick={() => openPanel(null)} aria-label="关闭会话栏" title="关闭">
              <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
            </button>
          </div>
          <div className="lab2-panel-body side-cards">
            <SideCard id="workflow" focus={panel} title="工作流编排" meta="当前会话"><SessionWorkflows messages={messages} requested={activeWorkflow}/></SideCard>
            <SideCard id="code" focus={panel} title="更改" meta={project.code ? `v${project.current_v}` : ''}>
              <ChangesCard project={project} stats={changes} fileName={fileNameOf(project.kind)} onOpen={openEditor} />
            </SideCard>
            <SideCard id="versions" focus={panel} title="迭代" meta={project.versions.length ? `${project.versions.length} 个版本` : '还没有'}>
              <VersionsTab project={project} metrics={metricsOf(dmeta)} onRestore={(v) => act(() => lab.restore(id, v))}
                onOpenBacktest={(btId) => onNavigate(`backtest/${btId}`)} />
            </SideCard>
            <SideCard id="context" focus={panel} title="上下文"
              meta={context ? `已使用 ${Math.round((context.total / Math.max(1, context.budget)) * 100)}%` : ''}>
              {context && <ContextTab context={context} updatedAt={ctxAt} compacting={compacting} onCompact={compact}
                autoCompact={project.auto_compact !== false} onAutoCompact={(v) => patch({ auto_compact: v })}
                onAutoCompactRatio={(v) => patch({ auto_compact_ratio: v })}
                onUnpin={(pid) => act(() => lab.unpin(id, pid))}
                onPin={(title, text) => act(() => lab.pin(id, { kind: 'note', title, text }))} />}
            </SideCard>
            {project.plan && (
              <SideCard id="plan" focus={panel} title="计划" meta={planMeta(project.plan)}>
                <PlanSteps plan={project.plan} />
                <div className="button-row" style={{ marginTop: 10 }}>
                  {project.plan.status !== 'done' && (
                    <button className="button small primary" disabled={running} onClick={() => { stick.current = true; act(() => lab.plan(id)) }}>
                      {project.plan.autorun && project.plan.status === 'active' ? '继续执行' : '接着执行'}
                    </button>
                  )}
                  <button className="button small" disabled={running} onClick={() => setInput('/plan ')}>重新列计划</button>
                </div>
              </SideCard>
            )}
            <SideCard id="goals" focus={panel} title="目标" meta={hasGoal ? `${projectItems(project).length} 个条件` : '未设置'}>
              <GoalSummary project={project} metrics={metricsOf(dmeta)} hasGoal={hasGoal} running={running} rounds={roundsNow}
                onEdit={() => setGoalOpen({ initial: projectSpec(project) })} onStart={() => startAuto(roundsNow)} />
            </SideCard>
          </div>
        </aside>
      )}

      {ratingPrompt !== null && feedbackCfg?.enabled && (
        <RatingPrompt projectId={id} seq={ratingPrompt} cfg={feedbackCfg} notify={notify}
          onRated={(feedback) => setMessages((prev) => prev.map((m) => (m.seq === ratingPrompt ? { ...m, feedback } : m)))}
          onDone={() => finishRating(ratingPrompt)} />
      )}
      {quotePicking && <QuotePicker currentId={id} onPick={addQuote} onClose={() => setQuotePicking(false)} />}
      {sharing && <ShareToModal item={{ type: 'project', id, title: project.title }} notify={notify} onNavigate={onNavigate} onClose={() => setSharing(false)} />}
      {editorOpen && (
        <Modal title={<>{fileNameOf(project.kind)} · v{project.current_v}{project.kind && project.kind !== 'strategy' && project.code && (
          <span style={{ marginLeft: 12, fontWeight: 400 }}><PythonCodeButton kind={project.kind as PyKind} artifact={project.code} name={project.title} /></span>)}</>}
          onClose={() => setEditorOpen(false)} width={960}>
          <div className="lab2-editor-modal">
            {isModel && <ModelPanel project={project} running={running}
              onSave={(code) => act(() => lab.saveCode(id, code, '手动修改训练规格'))} onSaveLibrary={saveOut}
              onTrain={() => act(() => lab.backtest(id))} onOpenModel={(mid) => onNavigate(`modellib/${mid}`)} />}
            {project.kind === 'portfolio' && <PortfolioPanel project={project} running={running}
              onSave={(code) => act(() => lab.saveCode(id, code, '手动修改配置方案'))} onSaveLibrary={saveOut}
              onRun={() => act(() => lab.backtest(id))} onOpenScheme={(sid) => onNavigate(`portfolio/${sid}`)} />}
            {project.kind === 'execution' && <ExecutionPanel project={project} running={running}
              onSave={(code) => act(() => lab.saveCode(id, code, '手动修改执行方案'))} onSaveLibrary={saveOut}
              onRun={() => act(() => lab.backtest(id))} onOpenScheme={(sid) => onNavigate(`execution/${sid}`)} />}
            {project.kind === 'factor' && <FactorPanel project={project} running={running}
              onSave={(code) => act(() => lab.saveCode(id, code, '手动修改表达式'))} onSaveLibrary={saveOut}
              onEvaluate={() => act(() => lab.backtest(id))} onOpenFactor={(fid) => onNavigate(`factors/${fid}`)} />}
            {!isFactor && <CodeTab project={project} onSave={(code) => act(() => lab.saveCode(id, code))}
              onSaveStrategy={saveOut} onOpenEditor={(slug) => onNavigate(`editor/${slug}`)} />}
          </div>
        </Modal>
      )}
    </>
  )
}

function ProjectMenu({ onDelete, onSave, hasCode, saved, saveLabel }: { onDelete: () => void; onSave: () => void; hasCode: boolean; saved: boolean; saveLabel?: string }) {
  const pop = usePopover()
  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button className="lab2-icon" onClick={() => pop.setOpen(!pop.open)} aria-label="更多" aria-expanded={pop.open} title="更多">
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden><circle cx="3.5" cy="8" r="1.2" fill="currentColor" /><circle cx="8" cy="8" r="1.2" fill="currentColor" /><circle cx="12.5" cy="8" r="1.2" fill="currentColor" /></svg>
      </button>
      {pop.open && (
        <div ref={pop.menuRef} className="cmp-menu cmp-menu-right" style={pop.menuStyle} data-placement={pop.placement} role="menu">
          <button className="cmp-menu-item" role="menuitem" disabled={!hasCode} onClick={() => { onSave(); pop.setOpen(false) }}>
            <span className="cmp-menu-main">{saveLabel ?? (saved ? '更新平台策略' : '保存为平台策略')}</span>
          </button>
          <button className="cmp-menu-item danger" role="menuitem" onClick={() => { pop.setOpen(false); onDelete() }}>
            <span className="cmp-menu-main">删除项目</span>
          </button>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 图片附件                                                           */
/* ------------------------------------------------------------------ */
interface Attachment { key: string; name: string; url: string }
const MAX_IMAGES = 4
const MAX_IMAGE_BYTES = 5 * 1024 * 1024
const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']

function readDataUrl(f: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(r.error)
    r.readAsDataURL(f)
  })
}

function AttachTray({ items, warning, onRemove }: { items: Attachment[]; warning?: string; onRemove: (key: string) => void }) {
  return (
    <div className="cmp-attach">
      {items.map((a) => (
        <div key={a.key} className="cmp-attach-item anim-pop" title={a.name}>
          <img src={a.url} alt={a.name} />
          <button type="button" onClick={(e) => { e.stopPropagation(); onRemove(a.key) }} aria-label={`移除 ${a.name}`} title="移除">
            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="M2 2l6 6M8 2l-6 6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
          </button>
        </div>
      ))}
      {warning && <span className="cmp-attach-warn anim-fade">{warning}</span>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 消息                                                               */
/* ------------------------------------------------------------------ */
interface MessageActions {
  running: boolean
  notify: PageProps['notify']
  lastSeq?: number
  onSuggest: (text: string) => void
  onPinText: (title: string, text: string) => void
  onEdit: (m: LabMessage, text: string) => void
  onRetry: (m: LabMessage) => void
  onFork: (m: LabMessage) => void
  onPin: (m: LabMessage) => void
  onCopy: (m: LabMessage) => void
  onQuote: (m: LabMessage) => void
  onFeedback: (m: LabMessage, rating: 1 | -1) => void
  onRenderErrors: (m: LabMessage, errors: RenderError[]) => void
}

/**
 * 最新一条已完成的 AI 回答：里面的 Mermaid / 公式 / ql 块 / SVG 渲染失败时，
 * 收集 1.5 秒内的全部错误一次性交回 AI 修正（服务端再保证每条消息只修一次）。
 */
function RenderRepair({ enabled, onErrors, children }: { enabled: boolean; onErrors: (e: RenderError[]) => void; children: ReactNode }) {
  const bag = useRef<RenderError[]>([])
  const timer = useRef(0)
  const sent = useRef(false)
  const cb = useRef(onErrors)
  cb.current = onErrors
  const report = useCallback((e: RenderError) => {
    if (sent.current || bag.current.some((x) => x.kind === e.kind && x.message === e.message && x.source === e.source)) return
    bag.current.push(e)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      if (sent.current || !bag.current.length) return
      sent.current = true
      cb.current(bag.current.slice(0, 8))
    }, 1500)
  }, [])
  useEffect(() => () => window.clearTimeout(timer.current), [])
  return enabled ? <RenderErrorCtx.Provider value={report}>{children}</RenderErrorCtx.Provider> : <>{children}</>
}

/** 消息完成时间：流式消息按最后一次更新（结束）算。 */
function msgTime(m: LabMessage) {
  const t = m.updated_at ?? m.created_at
  const d = new Date(t < 1e12 ? t * 1000 : t)
  const today = new Date().toDateString() === d.toDateString()
  const hm = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
  return { short: today ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`, full: d.toLocaleString('zh-CN', { hour12: false }) }
}

const MI = {
  edit: <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M10.5 2.8 13.2 5.5 6 12.7l-3.2.5.5-3.2Z" /></svg>,
  retry: <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M13 8a5 5 0 1 1-1.5-3.6" /><path d="M13 2.5v3h-3" /></svg>,
  fork: <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><circle cx="4.5" cy="3.5" r="1.5" /><circle cx="11.5" cy="3.5" r="1.5" /><circle cx="8" cy="12.5" r="1.5" /><path d="M4.5 5v1.5c0 1.4 1 2.2 2.3 2.6L8 9.5M11.5 5v1.5c0 1.4-1 2.2-2.3 2.6L8 9.5V11" /></svg>,
  copy: <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" aria-hidden><rect x="5" y="5" width="8.5" height="8.5" rx="1.6" /><path d="M11 5V3.6c0-.6-.5-1.1-1.1-1.1H3.6c-.6 0-1.1.5-1.1 1.1v6.3c0 .6.5 1.1 1.1 1.1H5" /></svg>,
  pin: <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M9.8 2.5 13.5 6.2 11 7.4 8.6 9.8 8.2 12.4 3.6 7.8 6.2 7.4 8.6 5Z" /><path d="m5.8 10.2-3.3 3.3" /></svg>,
  quote: <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M3 9.5c0-2.5 1-4.2 3-5M3 9.5h2.6v3H3zM9 9.5c0-2.5 1-4.2 3-5M9 9.5h2.6v3H9z" /></svg>,
  up: <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M5.5 7.2 8 2.6c.4-.7 1.5-.5 1.6.4l.2 2.4h2.8c.9 0 1.5.8 1.3 1.6l-1 4.6c-.1.5-.6.8-1.1.8H5.5" /><path d="M5.5 7.2v5.2H2.8V7.2z" /></svg>,
  down: <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M5.5 8.8 8 13.4c.4.7 1.5.5 1.6-.4l.2-2.4h2.8c.9 0 1.5-.8 1.3-1.6l-1-4.6c-.1-.5-.6-.8-1.1-.8H5.5" /><path d="M5.5 8.8V3.6H2.8v5.2z" /></svg>,
}

function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden
      fill={filled ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round">
      <path d="m12 3.2 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9Z" />
    </svg>
  )
}

/** Agent 完成一段回答后的轻量评分卡：可评分、写意见、主动关闭或按管理员策略自动关闭。 */
function RatingPrompt({ projectId, seq, cfg, notify, onDone, onRated }: {
  projectId: string
  seq: number
  cfg: { enabled: boolean; every_n_messages: number; auto_close_seconds: number }
  notify: PageProps['notify']
  onDone: () => void
  onRated: (feedback: { rating: 1 | -1; comment: string; score?: number }) => void
}) {
  const [score, setScore] = useState(0)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [thanks, setThanks] = useState(false)
  const seconds = Math.max(0, Number(cfg.auto_close_seconds) || 0)
  const [remaining, setRemaining] = useState(seconds)
  const doneRef = useRef(onDone)
  doneRef.current = onDone

  useEffect(() => {
    if (!seconds) return
    const due = Date.now() + seconds * 1000
    const timer = window.setInterval(() => {
      const left = Math.max(0, Math.ceil((due - Date.now()) / 1000))
      setRemaining(left)
      if (left <= 0) {
        window.clearInterval(timer)
        doneRef.current()
      }
    }, 250)
    return () => window.clearInterval(timer)
  }, [seconds])

  useEffect(() => {
    if (!thanks) return
    const timer = window.setTimeout(() => doneRef.current(), 1800)
    return () => window.clearTimeout(timer)
  }, [thanks])

  const submit = async () => {
    if (!score || busy) return
    setBusy(true)
    try {
      const r = await lab.feedback(projectId, seq, score >= 3 ? 1 : -1, comment.trim(), score)
      if (r.feedback) onRated(r.feedback)
      setThanks(true)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const closeHint = seconds > 0 ? (lang === 'en' ? `${remaining}s until close` : `${remaining} 秒后自动关闭`) : ''
  return (
    <aside className="rating-prompt" role="dialog" aria-modal="false" aria-labelledby="rating-prompt-title">
      <div className="rating-prompt-head">
        <div>
          <b id="rating-prompt-title">{t('评价这次回答')}</b>
          <small>{t('这个评价会保存到工作区，帮助我们改进模型。')}</small>
        </div>
        <button type="button" className="rating-prompt-x" onClick={() => doneRef.current()} aria-label={t('关闭')} title={t('关闭')}>×</button>
      </div>
      {thanks ? (
        <p className="rating-prompt-thanks">{t('感谢您的评价！您的建议将支持我们改进模型')}</p>
      ) : (
        <>
          <div className="rating-stars" role="radiogroup" aria-label={t('评价这次回答')}>
            {[1, 2, 3, 4, 5].map((n) => (
              <button key={n} type="button" role="radio" aria-checked={score === n} aria-label={`${n} ${t('星')}`}
                className={score >= n ? 'on' : ''} onClick={() => setScore(n)}>
                <StarIcon filled={score >= n} />
              </button>
            ))}
          </div>
          <textarea value={comment} onChange={(e) => setComment(e.target.value.slice(0, 2000))}
            placeholder={t('可以补充具体意见（可选）')} aria-label={t('可以补充具体意见（可选）')} rows={3} />
          <div className="rating-prompt-foot">
            <span>{closeHint}</span>
            <button type="button" className="button small ghost" onClick={() => doneRef.current()}>{t('稍后再说')}</button>
            <button type="button" className="button small primary" disabled={!score || busy} onClick={submit}>
              {busy ? (lang === 'en' ? 'Submitting…' : '提交中…') : t('提交评价')}
            </button>
          </div>
        </>
      )}
    </aside>
  )
}

function MsgBar({ m, items, align }: { m: LabMessage; items: [keyof typeof MI, string, () => void, boolean?, boolean?][]; align: 'left' | 'right' }) {
  const t = msgTime(m)
  return (
    <div className={`msg-bar ${align}`}>
      {items.map(([icon, label, fn, disabled, active]) => (
        <button key={label} type="button" className={`msg-act${active ? ' on' : ''}`} title={label} aria-label={label} disabled={disabled} onClick={fn}>{MI[icon]}</button>
      ))}
      <time className="msg-time" dateTime={new Date((m.updated_at ?? m.created_at) * (m.created_at < 1e12 ? 1000 : 1)).toISOString()} title={t.full}>{t.short}</time>
    </div>
  )
}

function UserMessage({ m, actions, projectId }: { m: LabMessage; actions: MessageActions; projectId: string }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(m.content)
  if (editing) {
    return (
      <div className="cb-user msg-editing anim-in">
        <textarea autoFocus rows={Math.min(8, Math.max(2, text.split('\n').length))} value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') setEditing(false)
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && text.trim()) { e.preventDefault(); setEditing(false); actions.onEdit(m, text.trim()) }
          }} />
        <div className="msg-edit-foot">
          <span>编辑后会删掉这条消息之后的对话，从这里重新开始{(m.data?.images?.length ?? 0) > 0 ? '；附图会一起重新发送' : ''}</span>
          <button type="button" className="button small ghost" onClick={() => { setEditing(false); setText(m.content) }}>取消</button>
          <button type="button" className="button small primary" disabled={!text.trim() || actions.running} onClick={() => { setEditing(false); actions.onEdit(m, text.trim()) }}>发送</button>
        </div>
      </div>
    )
  }
  const images = (m.data?.images ?? []) as { id: string; name?: string }[]
  return (
    <div className="msg-wrap user">
      {images.length > 0 && (
        <div className="cb-user with-images">
          <div className="cb-user-images stagger">
            {images.map((im) => (
              <a key={im.id} href={lab.imageUrl(projectId, im.id)} target="_blank" rel="noreferrer" title={im.name || '查看原图'}>
                <img src={lab.imageUrl(projectId, im.id)} alt={im.name || '附图'} loading="lazy" />
              </a>
            ))}
          </div>
        </div>
      )}
      {(m.content || m.data?.edited) && <UserBubble>{m.content}{m.data?.edited && <span className="msg-edited">已编辑</span>}</UserBubble>}
      <MsgBar m={m} align="right" items={[
        ['copy', '复制', () => actions.onCopy(m)],
        ['edit', '编辑', () => { setText(m.content); setEditing(true) }, actions.running],
        ['retry', '重试', () => actions.onRetry(m), actions.running],
        ['fork', '从此处分叉', () => actions.onFork(m)],
      ]} />
    </div>
  )
}

function MessageView({ m, project, meta, active, actions, concise, onDecide, onOpenBacktest, onOpenEditor, onOpenCode, onOpenLibrary, onOpenWorkflow, liveEvidence }: {
  m: LabMessage; project: LabProject; meta: LabMeta | null; active: boolean; actions: MessageActions; concise?: boolean
  onOpenLibrary: (route: string) => void
  onOpenWorkflow:(wid:string)=>void
  onDecide: (choice: string, note: string) => Promise<unknown> | void
  onOpenBacktest: (id: string) => void
  onOpenEditor: (slug: string) => void
  onOpenCode: () => void
  liveEvidence?: Record<string, RunEvidenceState>
}) {
  if (m.role === 'user' && m.kind === 'text') return <UserMessage m={m} actions={actions} projectId={project.id} />
  if (m.role === 'user' && m.kind === 'code') return <UserBubble>手动保存为 v{m.data.v}：{m.content}</UserBubble>
  const d = m.data || {}
  // P4：气泡状态标签 + 运行证据。已生成只在有 run / 工件证据时出现，纯文字回复标「已回复」。
  const tag = messageTag(m, { projectId: project.id, domain: project.kind, live: liveEvidence })
  const stateLine = tag && m.kind !== 'text' && m.kind !== 'error' &&
    tag.tone !== 'generating' && tag.tone !== 'failed' && tag.tone !== 'stopped'
    ? <MessageStateTag tag={tag} live={liveEvidence} />
    : null
  const view = (() => {
  switch (m.kind) {
    case 'text':
      if (m.role === 'event') return <EventLine>{m.content}{typeof d.link === 'string' && <> · <a href={`#/${d.link}`}>打开</a></>}</EventLine>
      // 只调用了工具、没有正文也没有思考的那一步：不占一块空白
      if (!d.streaming && !m.content && !d.thinking) return null
      return (
        <div className="msg-wrap ai">
          <RenderRepair enabled={!d.streaming && m.seq === actions.lastSeq && !actions.running && !d.render_repair}
            onErrors={(errs) => actions.onRenderErrors(m, errs)}>
            <AssistantText content={m.content} thinking={concise ? undefined : d.thinking} thinkingMs={concise ? undefined : d.thinking_ms} streaming={!!d.streaming}
              model={m.model} fallback={m.mode === 'fallback'} />
          </RenderRepair>
          {tag && <MessageStateTag tag={tag} live={liveEvidence} />}
          {!d.streaming && m.seq === actions.lastSeq && !actions.running && Array.isArray(d.suggestions) && d.suggestions.length > 0 && (
            <div className="msg-suggest anim-in">
              {(d.suggestions as string[]).map((x) => <button key={x} type="button" className="chip" onClick={() => actions.onSuggest(x)}>{x}</button>)}
            </div>
          )}
          {!d.streaming && m.content && (
            <MsgBar m={m} align="left" items={[
              ['up', m.feedback?.rating === 1 ? '已点赞' : '点赞', () => actions.onFeedback(m, 1), false, m.feedback?.rating === 1],
              ['down', m.feedback?.rating === -1 ? '已点踩' : '点踩', () => actions.onFeedback(m, -1), false, m.feedback?.rating === -1],
              ['copy', '复制', () => actions.onCopy(m)],
              ['quote', '引用到输入框', () => actions.onQuote(m)],
              ['fork', '从这里分叉', () => actions.onFork(m)],
              ['pin', '钉进上下文', () => actions.onPin(m)],
            ]} />
          )}
        </div>
      )
    case 'error':
      return <EventLine tone="error">{m.content}</EventLine>
    case 'tool':
      return <><ToolLine icon={TOOL_ICON[d.name]} label={d.label} args={d.args} status={d.status} elapsedMs={d.elapsed_ms}
        detail={d.error || (d.preview ? prettyJson(d.preview) : undefined)} />{d.artifact_view&&<ArtifactPreview initial={d.artifact_view}/>}{d.task_view&&<TaskResult value={d.task_view}/>}{d.workflow_view&&<button type="button" className="button small" onClick={()=>onOpenWorkflow(d.workflow_view.workflow_id)}>查看右侧编排 · {d.workflow_view.title}</button>}</>
    case 'code':
      return <CodeCard m={m} onOpenCode={onOpenCode} />
    case 'factor_code':
      return <FactorCodeCard m={m} onOpen={onOpenCode} />
    case 'factor_report':
      return <FactorReportCard m={m} metrics={metricsOf(meta)} />
    case 'model_spec':
      return <ModelSpecCard m={m} onOpen={onOpenCode} />
    case 'model_report':
      return <ModelReportCard m={m} metrics={metricsOf(meta)} onOpenRun={(rid) => onOpenLibrary(`modellib/run/${rid}`)} />
    case 'portfolio_spec':
      return <PortfolioSpecCard m={m} onOpen={onOpenCode} />
    case 'portfolio_report':
      return <PortfolioReportCard m={m} metrics={metricsOf(meta)} onOpenRun={(rid) => onOpenLibrary(`portfolio/run/${rid}`)} />
    case 'execution_spec':
      return <ExecutionSpecCard m={m} onOpen={onOpenCode} />
    case 'execution_report':
      return <ExecutionReportCard m={m} metrics={metricsOf(meta)} onOpenRun={(rid) => onOpenLibrary(`execution/run/${rid}`)} />
    case 'backtest':
      return <BacktestCard m={m} onOpen={onOpenBacktest} metrics={metricsOf(meta)} />
    case 'cost_sweep':
      return <CostSweepCard m={m} onOpen={onOpenBacktest} />
    case 'parameter_heatmap':
      return <ParameterHeatmapCard data={d} />
    case 'stress_test':
      return <StressTestCard data={d} />
    case 'round':
      return <RoundCard m={m} metrics={metricsOf(meta)} />
    case 'team':
      return <TeamCard m={m} concise={concise} />
    case 'ask':
      return <AskCard m={m} active={active} onDecide={onDecide} />
    case 'goal':
      return <GoalEvent m={m} metrics={metricsOf(meta)} />
    case 'walkforward':
      return <WalkForwardCard m={m} />
    case 'stability':
      return <StabilityCard m={m} />
    case 'plan':
      return <PlanCard plan={d.plan as LabPlan} live={project.plan?.seq === m.seq ? project.plan : null} />
    case 'report':
      return <ReportCard m={m} projectId={project.id} />
    case 'subagents':
      return <SubagentsCard m={m} onOpen={(cid) => onOpenLibrary(`lab/${cid}`)} />
    case 'discussion':
      return <DiscussionCard missionId={d.mission_id} topic={m.content} notify={actions.notify} concise={concise}
        onPin={(text) => actions.onPinText(`团队讨论结论 · ${m.content.slice(0, 16)}`, text)} />
    case 'saved':
      return (
        <EventLine tone="ok">
          {d.kind && d.kind !== 'strategy' ? m.content : <>{m.content}「{d.name}」</>}
          {d.slug && <button className="link" onClick={() => onOpenEditor(d.slug)}>打开</button>}
          {d.factor_id && <button className="link" onClick={() => onOpenLibrary(`factors/${d.factor_id}`)}>打开</button>}
          {d.model_id && <button className="link" onClick={() => onOpenLibrary(`modellib/${d.model_id}`)}>打开</button>}
          {d.scheme_id && <button className="link" onClick={() => onOpenLibrary(`portfolio/${d.scheme_id}`)}>打开</button>}
          {d.exec_id && <button className="link" onClick={() => onOpenLibrary(`execution/${d.exec_id}`)}>打开</button>}
        </EventLine>
      )
    default:
      return <EventLine>{m.content || project.title}</EventLine>
  }
  })()
  return <>{view}{stateLine}</>
}

function prettyJson(s: string) {
  try { return JSON.stringify(JSON.parse(s), null, 2).slice(0, 3000) } catch { return s }
}

function CodeCard({ m, onOpenCode }: { m: LabMessage; onOpenCode: () => void }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>{m.content || '新版本'}</b>
        <span className="lab2-card-gap" />
        {d.before && <button className="link" onClick={() => setOpen(!open)}>{open ? '收起改动' : '改动'}</button>}
        <button className="link" onClick={onOpenCode}>代码</button>
      </div>
      {Object.keys(d.params || {}).length > 0 && (
        <div className="lab2-params">{Object.entries(d.params || {}).slice(0, 8).map(([k, v]) => <code key={k}>{k}={String(v)}</code>)}</div>
      )}
      {(d.experiment || d.diff) && <ExperimentNote experiment={d.experiment} diff={d.diff} />}
      {(d.warnings || []).length > 0 && (
        <div className="lab2-warn">
          {d.warnings.map((w: string) => <div key={w}>性能提示：{w}</div>)}
        </div>
      )}
      {open && <DiffView before={d.before || ''} after={d.after || ''} maxHeight={360} />}
    </div>
  )
}

function Sparkline({ a, b }: { a: number[]; b?: number[] }) {
  if (!a?.length) return null
  const all = [...a, ...(b || [])]
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const w = 220
  const h = 48
  const path = (s: number[]) => s.map((v, i) => `${i ? 'L' : 'M'}${((i / Math.max(1, s.length - 1)) * w).toFixed(1)},${(h - ((v - lo) / (hi - lo || 1)) * (h - 4) - 2).toFixed(1)}`).join('')
  return (
    <svg className="lab2-spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-label="策略与基准净值">
      {b?.length ? <path d={path(b)} className="bench" /> : null}
      <path d={path(a)} className="strat" />
    </svg>
  )
}

function MetricsStrip({ m }: { m: Metrics }) {
  const cells: [string, string, string?][] = [
    ['总收益', pct(m.total_return), (m.total_return ?? 0) >= 0 ? 'up' : 'down'],
    ['超额', pct(m.excess_return), (m.excess_return ?? 0) >= 0 ? 'up' : 'down'],
    ['夏普', num(m.sharpe)],
    ['最大回撤', pct(m.max_drawdown)],
    ['交易', String(m.trades ?? 0)],
    ['胜率', pct(m.win_rate, 0)],
  ]
  return (
    <div className="lab2-metrics">
      {cells.map(([k, v, t]) => <div key={k}><span>{k}</span><b className={t}>{v}</b></div>)}
    </div>
  )
}

function BacktestCard({ m, onOpen, metrics }: { m: LabMessage; onOpen: (id: string) => void; metrics: ReturnType<typeof metricsOf> }) {
  const d = m.data
  const ev = d.evaluation
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>{d.cost_scale > 1 ? `${d.cost_scale}× 成本回测` : '回测'}</b>
        {ev && <span className={`cb-badge ${ev.passed ? 'ok' : 'warn'}`}>{ev.passed ? '达标' : '未达标'}</span>}
        {d.data_check && <span className={`cb-badge ${d.data_check.formal ? 'ok' : 'warn'}`} title={d.data_check.label}>{d.data_check.formal ? '正式回测' : '非正式回测'}</span>}
        {(d.cost_source === 'strategy' || d.cost_model?.cost_source === 'strategy') && <span className="cb-badge warn" title="策略里的 set_order_cost 已覆盖 UI 默认成本">代码自定义费率</span>}
        {d.cost_model?.execution_mode === 'tick' && <span className="cb-badge" title="用 OHLC 路径模拟盘中逐笔成交">逐笔撮合</span>}
        {d.cost_model?.dynamic_slippage === true && <span className="cb-badge" title="滑点按本根振幅与参与率放大">动态滑点</span>}
        <span className="lab2-card-gap" />
        <span className="lab2-faint">{(d.data_sources || []).join(' / ')}</span>
        {d.bt_id && <button className="link" onClick={() => onOpen(d.bt_id)}>详情</button>}
      </div>
      <div className="lab2-bt">
        <MetricsStrip m={d.metrics || {}} />
        <Sparkline a={d.equity} b={d.benchmark} />
      </div>
      {ev && d.cost_scale === 1 && (
        <div className="lab2-checks">
          {ev.items.map((i: { key: string; metric?: string; label: string; op: string; target: number; actual: number | null; ok: boolean; priority?: string }) => {
            const metric = i.metric || LEGACY_METRIC[i.key] || i.key
            return (
              <span key={i.key} className={`${i.ok ? 'ok' : 'bad'}${i.priority === 'try' ? ' try' : ''}`}>
                {i.ok ? '✓' : '✗'} {i.label} {i.op} {fmtGoal(metric, i.target, metrics)}
                <small>（{fmtGoal(metric, i.actual, metrics)}）</small>
              </span>
            )
          })}
        </div>
      )}
      {d.fee_check && <FeeCheck c={d.fee_check} />}
      {d.cost_audit && <CostAudit a={d.cost_audit} />}
      {(d.warnings || []).length > 0 && <div className="lab2-faint">{d.warnings.join('；')}</div>}
      {d.data_check && <DataCheckBadge check={d.data_check} definitions={d.definitions} consistency={d.consistency} zero={d.zero_trade_diagnosis} />}
    </div>
  )
}

interface FeeCheckData { scale: number; total_ratio: number; per_trade_ratio: number | null; note: string }

/** 压力回测的手续费口径：总手续费 = 成交额 × 费率，成交额会变，所以按每笔平均手续费核对放大倍数。 */
function FeeCheck({ c }: { c: FeeCheckData }) {
  return (
    <div className="lab2-faint fee-check" title="费率在撮合时按倍数放大；总手续费还取决于成交额（净值缩水会让仓位变小）和交易笔数">
      手续费核对：总额是 1× 的 {c.total_ratio.toFixed(2)} 倍
      {c.per_trade_ratio !== null && <>，每笔平均 {c.per_trade_ratio.toFixed(2)} 倍（费率 {c.scale}×）</>}
      {c.note && <>；{c.note}</>}
    </div>
  )
}

interface SweepRow {
  scale: number; ok: boolean; error: string | null; passed: boolean; failed: string[]; bt_id?: string
  metrics: Partial<Metrics> & { total_fees?: number | null }
  fee_check: FeeCheckData | null
}

/** 成本压力扫描：每档倍数一行，标出最后一档满足和第一档不满足，之间就是成本极限。 */
function CostSweepCard({ m, onOpen }: { m: LabMessage; onOpen: (id: string) => void }) {
  const d = m.data as { v: number; criterion: string; rows: SweepRow[]; limit: { pass_max: number | null; fail_min: number | null }; summary: string }
  return (
    <div className="lab2-card sweep-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>成本压力扫描</b>
        {d.limit.pass_max !== null && d.limit.fail_min !== null && <span className="cb-badge warn">极限 {d.limit.pass_max}×–{d.limit.fail_min}×</span>}
        {d.limit.fail_min === null && d.limit.pass_max !== null && <span className="cb-badge ok">全部满足</span>}
        <span className="lab2-card-gap" />
        <span className="lab2-faint" title="判定标准">{d.criterion}</span>
      </div>
      <div className="table-wrap">
        <table className="sweep-table">
          <thead><tr><th>成本</th><th className="right">总收益</th><th className="right">夏普</th><th className="right">最大回撤</th><th className="right">交易</th><th className="right">手续费</th><th>结果</th></tr></thead>
          <tbody>
            {d.rows.map((r) => (
              <tr key={r.scale} className={`${r.passed ? 'ok' : 'bad'}${r.scale === d.limit.pass_max || r.scale === d.limit.fail_min ? ' edge' : ''}`}>
                <td className="mono">{r.scale}×</td>
                {r.ok ? <>
                  <td className="right mono">{pct(r.metrics.total_return)}</td>
                  <td className="right mono">{num(r.metrics.sharpe)}</td>
                  <td className="right mono">{pct(r.metrics.max_drawdown)}</td>
                  <td className="right mono">{r.metrics.trades ?? 0}</td>
                  <td className="right mono" title={r.fee_check ? `总额 ${r.fee_check.total_ratio}×，每笔 ${r.fee_check.per_trade_ratio ?? '—'}×` : undefined}>
                    {num(r.metrics.total_fees ?? null)}{r.fee_check && r.scale !== 1 ? <small className="muted"> ×{(r.fee_check.per_trade_ratio ?? r.fee_check.total_ratio).toFixed(2)}</small> : null}
                  </td>
                  <td>{r.passed ? '✓ 满足' : `✗ ${r.failed.slice(0, 2).join('、') || '收益转负'}`}{r.bt_id && <button className="link" onClick={() => onOpen(r.bt_id!)}>详情</button>}</td>
                </> : <td colSpan={6} className="lab2-faint">{r.error}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="lab2-faint">{d.summary} 手续费列的 ×N 是每笔平均手续费相对 1× 的倍数（总额还会随成交额与笔数变化）。</div>
    </div>
  )
}

function RoundCard({ m, metrics }: { m: LabMessage; metrics: ReturnType<typeof metricsOf> }) {
  const d = m.data
  const factor = !!d.domain && d.domain !== 'strategy'
  if (d.stage === 'start') {
    return <EventLine>自动迭代 · {d.goals_text} · {d.range ? `${d.range} · ` : ''}最多 {d.rounds} 轮{d.research !== 'none' ? ` · 先${d.research === 'team' ? '团队调研' : '看行情'}` : ''}</EventLine>
  }
  if (d.stage === 'round') {
    return (
      <div className={`lab2-round${d.passed ? ' ok' : ''}`}>
        <span>第 {d.round} 轮 · v{d.v}</span>
        {(d.items || []).map((i: { key: string; metric?: string; label: string; ok: boolean; actual?: number | null }) => (
          <span key={i.key} className={i.ok ? 'ok' : 'bad'}>{i.ok ? '✓' : '✗'} {i.label}{factor && i.metric ? ` ${fmtGoal(i.metric, i.actual, metrics)}` : ''}</span>
        ))}
        <span className="lab2-card-gap" />
        <span>{d.passed ? '达标' : '继续改进'}</span>
      </div>
    )
  }
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <b>{d.passed ? '迭代完成，已达成验收目标' : '迭代完成，选出成绩最好的一版'}</b>
        {factor && d.admitted !== undefined && <span className={`cb-badge ${d.admitted ? 'ok' : 'warn'}`}>{d.admitted ? (d.domain === 'model' ? '可上线' : d.domain === 'portfolio' || d.domain === 'execution' ? '可启用' : '可入库') : '检查未全过'}</span>}
        <span className="lab2-card-gap" />
        <span className="lab2-ver">最佳 v{d.best_v}</span>
      </div>
      {d.domain === 'model' ? (
        <div className="lab2-finale"><div><ModelStats s={d.metrics} compact /></div></div>
      ) : d.domain === 'execution' ? (
        <div className="lab2-finale">
          <div><span className="lab2-faint">基准成本</span><ExecStats s={d.metrics} compact /></div>
          {d.stress && <div><span className="lab2-faint">2× 成本</span><ExecStats s={d.stress} compact /></div>}
        </div>
      ) : d.domain === 'portfolio' ? (
        <div className="lab2-finale">
          <div><span className="lab2-faint">基准成本</span><PortfolioStats s={d.metrics} compact /></div>
          {d.stress && <div><span className="lab2-faint">2× 成本</span><PortfolioStats s={d.stress} compact /></div>}
        </div>
      ) : factor ? (
        <div className="lab2-finale">
          <div><span className="lab2-faint">基准成本</span><FactorStats s={d.metrics as FactorSummary} compact /></div>
          {d.stress && <div><span className="lab2-faint">2× 成本</span><FactorStats s={d.stress as FactorSummary} compact /></div>}
        </div>
      ) : <div className="lab2-finale">
        <div><span className="lab2-faint">基准成本</span><MetricsStrip m={d.metrics || {}} /></div>
        {d.stress && <div><span className="lab2-faint">2× 成本</span><MetricsStrip m={d.stress} /></div>}
      </div>}
      {(d.warnings || []).length > 0 && <ul className="lab2-warn">{d.warnings.map((w: string) => <li key={w}>{w}</li>)}</ul>}
    </div>
  )
}

function TeamCard({ m, concise }: { m: LabMessage; concise?: boolean }) {
  const [open, setOpen] = useState(!concise)
  const d = m.data
  const [current, setCurrent] = useState<TeamRun | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [cancelId] = useState(() => crypto.randomUUID())
  const id = String(d.run_id || '')
  const durable = Boolean(d.runtime_run_id)
  const status = current?.status || String(d.status || '')
  const pending = ['waiting', 'running', 'queued', 'canceling'].includes(status)
  useEffect(() => {
    if (!id || !durable) return
    let alive = true
    const refresh = () => api.teamRun(id).then(({ run }) => { if (alive) setCurrent(run) }).catch((e) => { if (alive) setError(String(e)) })
    void refresh()
    const timer = pending ? window.setInterval(refresh, 5000) : undefined
    return () => { alive = false; if (timer) window.clearInterval(timer) }
  }, [id, durable, pending])
  const act = async (cancel: boolean) => {
    if (busy) return
    setBusy(true); setError('')
    try { const result = await (cancel ? api.cancelTeamRun(id, cancelId) : api.resumeTeamRun(id)); setCurrent(result.run) }
    catch (e) { setError(String(e)) }
    finally { setBusy(false) }
  }
  const summary = current ? current.synthesis : m.content
  const steps = (current?.steps || d.steps || []) as { role_id: string; role_name: string; status: string; model: string; output: string }[]
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-avatars">{steps.slice(0, 6).map((s, i) => <Avatar key={i} id={s.role_id} size={20} title={s.role_name} />)}</span>
        <b>{d.team_name} 评审</b>
        <span className="lab2-faint lab2-ellipsis">{d.topic}</span>
        <span className="lab2-card-gap" />
        {durable && <span className="cc-kind">{status === 'queued' ? '排队中' : status === 'waiting' ? '等待核对' : status === 'canceling' ? '取消中' : status === 'canceled' ? '已取消' : status === 'done' ? '已完成' : status === 'failed' ? '失败' : '处理中'}</span>}
        {pending && current?.can_manage && status !== 'canceling' && <>
          <button className="link" disabled={busy} onClick={() => void act(false)}>继续评审</button>
          <button className="link" disabled={busy} onClick={() => void act(true)}>取消评审</button>
        </>}
        <button className="link" onClick={() => setOpen(!open)}>{open ? '收起' : `${steps.length} 条发言`}</button>
      </div>
      {open && (
        <div className="lab2-team">
          {steps.map((s, i) => (
            <div key={i} className={`lab2-team-row ${s.status === 'error' ? 'error' : ''}`}>
              <Avatar id={s.role_id} size={22} />
              <div className="lab2-team-bubble">
                <div className="lab2-team-who"><b>{s.role_name}</b>{s.status === 'error' && <span className="cc-kind">失败</span>}{s.status === 'waiting' && <span className="cc-kind">等待核对</span>}{s.status === 'blocked' && <span className="cc-kind">等待前序角色</span>}{s.status === 'canceled' && <span className="cc-kind">已取消</span>}{s.status === 'queued' && <span className="cc-kind">排队中</span>}{s.status === 'running' && <span className="cc-kind">进行中</span>}{s.model && <span>{s.model}</span>}</div>
                {['ok', 'error'].includes(s.status) && <Markdown text={s.output || '（没有产出内容）'} compact />}
              </div>
            </div>
          ))}
        </div>
      )}
      {durable && ['waiting', 'canceling'].includes(status) && <TeamRecovery runId={id} revision={current?.runtime_event_seq || 0} onRun={setCurrent} onError={setError} />}
      {error && <div role="alert" className="lab2-faint">{error}</div>}
      {summary && <div className="lab2-team-sum"><Markdown text={summary} compact /></div>}
    </div>
  )
}

function AskCard({ m, active, onDecide }: { m: LabMessage; active: boolean; onDecide: (choice: string, note: string) => Promise<unknown> | void }) {
  const [note, setNote] = useState('')
  const [sent, setSent] = useState(false)
  const decide = (choice: string, extra: string) => {
    if (sent) return
    setSent(true)
    Promise.resolve(onDecide(choice, extra)).finally(() => setSent(false))
  }
  // 只写了自己的回答、没点选项：把这段话作为回答发出去（AI 接着做）
  const custom = () => { const t = note.trim(); if (t) decide(t, '') }
  const noteBox = (placeholder: string) => (
    <div className="lab2-ask-custom">
      <input className="lab2-ask-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder={placeholder} disabled={sent}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); custom() } }} />
      <button type="button" className="button small primary" disabled={!note.trim() || sent} onClick={custom} title="按你写的发送（不选上面的选项）">发送</button>
    </div>
  )
  const answered = m.data.answered
  const approval = m.data.approval as { tool: string; label: string; args: Record<string, unknown>; category?: string }[] | undefined
  if (approval) {
    const catLabel = (c?: string) => APPROVAL_CATEGORIES.find((x) => x.id === c)?.label
    return (
      <div className={`lab2-ask approval${active ? ' active' : ''}`}>
        <div className="approval-mark" aria-hidden>
          <QuantBot size={30} state={active ? 'thinking' : 'idle'} title="QuantLab Bot" />
          {active && <span className="approval-q">?</span>}
        </div>
        <div className="approval-body">
        <div className="approval-head">
          <b>需要你批准</b><span className="lab2-faint">{active ? '权限规则要求这些操作先由你确认' : '审批已处理'}</span>
        </div>
        <ul className="approval-list">
          {approval.map((a, i) => (
            <li key={i}>
              {catLabel(a.category) && <span className={`approval-cat ${a.category}`}>{catLabel(a.category)}</span>}
              <span>{a.label}</span>
              {typeof a.args.code === 'string' && a.args.code && (
                <details><summary>查看代码</summary><pre className="approval-code">{a.args.code}</pre></details>
              )}
            </li>
          ))}
        </ul>
        <div className="lab2-ask-options">
          {(m.data.options || []).map((o: string) => (
            <button key={o} className={`button small${answered === o || (active && o === '批准') ? ' primary' : ''}`} disabled={!active || sent}
              onClick={() => decide(o, note.trim())}>{o}</button>
          ))}
        </div>
        {active && noteBox('或者写下你的要求后直接发送，例如“先别回测，改一下止损”')}
        {answered && <div className="lab2-faint">已选择：{answered}</div>}
        </div>
      </div>
    )
  }
  return (
    <div className={`lab2-ask${active ? ' active' : ''}`}>
      <Markdown text={m.content} compact />
      <div className="lab2-ask-options">
        {(m.data.options || []).map((o: string) => (
          <button key={o} className={`button small${answered === o ? ' primary' : ''}`} disabled={!active || sent}
            onClick={() => decide(o, note.trim())}>{o}</button>
        ))}
      </div>
      {active && noteBox('都不合适？写下你自己的方向后发送；点选项时这里的内容会作为补充一起发出')}
      {answered && <div className="lab2-faint">已选择：{answered}</div>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 右侧面板                                                           */
/* ------------------------------------------------------------------ */
function CodeTab({ project, onSave, onSaveStrategy, onOpenEditor }: {
  project: LabProject; onSave: (code: string) => void; onSaveStrategy: () => void; onOpenEditor: (slug: string) => void
}) {
  const [code, setCode] = useState(project.code)
  const [dirty, setDirty] = useState(false)
  useEffect(() => { if (!dirty) setCode(project.code) }, [project.code, dirty])
  if (!project.code) return <div className="lab2-panel-empty">还没有代码</div>
  return (
    <div className="lab2-code">
      <div className="lab2-code-head">
        <b>v{project.current_v}</b>
        {dirty && <span className="cb-badge warn">未保存</span>}
        <span className="lab2-card-gap" />
        <button className="button small" disabled={!dirty} onClick={() => { onSave(code); setDirty(false) }}>保存为新版本</button>
      </div>
      <CodeEditor value={code} onChange={(v) => { setCode(v); setDirty(true) }} onSave={() => { onSave(code); setDirty(false) }} minHeight={420} />
      <div className="button-row" style={{ marginTop: 10 }}>
        <button className="button small primary" onClick={onSaveStrategy}>{project.slug ? '更新平台策略' : '保存为平台策略'}</button>
        {project.slug && <button className="button small" onClick={() => onOpenEditor(project.slug!)}>在编辑器中打开</button>}
      </div>
    </div>
  )
}

function VersionsTab({ project, onRestore, onOpenBacktest, metrics }: { project: LabProject; onRestore: (v: number) => void; onOpenBacktest: (id: string) => void; metrics: ReturnType<typeof metricsOf> }) {
  const versions = [...project.versions].reverse()
  const best = useMemo(() => {
    const scored = project.versions.filter((v) => v.evaluation)
    return scored.length ? scored.reduce((a, b) => (b.evaluation!.score > a.evaluation!.score ? b : a)).v : null
  }, [project.versions])
  if (!versions.length) return <div className="lab2-panel-empty">还没有版本</div>
  return (
    <div className="lab2-versions">
      {versions.map((v) => (
        <div key={v.v} className={`lab2-version${v.v === project.current_v ? ' on' : ''}`}>
          <div className="lab2-version-head">
            <b>v{v.v}</b>
            <span className="lab2-faint">{SOURCE_LABEL[v.source] ?? v.source}</span>
            {v.v === best && <span className="cb-badge ok">最佳</span>}
            {v.v === project.current_v && <span className="cb-badge">当前</span>}
            <span className="lab2-card-gap" />
            {v.v !== project.current_v && <button className="link" onClick={() => onRestore(v.v)}>切换</button>}
          </div>
          <div className="lab2-version-note">{v.name ? <b>{v.name}</b> : null}{v.name ? ' · ' : ''}{v.note}</div>
          {project.kind === 'model' ? (
            <ModelVersionMetrics v={v} metrics={metrics} />
          ) : project.kind === 'portfolio' ? (
            <PortfolioVersionMetrics v={v} metrics={metrics} />
          ) : project.kind === 'execution' ? (
            <ExecutionVersionMetrics v={v} metrics={metrics} />
          ) : project.kind === 'factor' ? (
            <FactorVersionMetrics v={v} metrics={metrics} />
          ) : v.metrics ? (
            <div className="lab2-version-metrics">
              <span>夏普 <b>{num(v.metrics.sharpe)}</b></span>
              <span>回撤 <b>{pct(v.metrics.max_drawdown)}</b></span>
              <span>收益 <b>{pct(v.metrics.total_return)}</b></span>
              <span>交易 <b>{v.metrics.trades ?? 0}</b></span>
              {v.bt_id && <button className="link" onClick={() => onOpenBacktest(v.bt_id!)}>详情</button>}
            </div>
          ) : <div className="lab2-faint">尚未回测</div>}
        </div>
      ))}
    </div>
  )
}

const fmtCtxTokens = (n: number) => n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : `${Math.round(n / 1000)}k`

function AutoCompactThreshold({ ratio, budget, onChange }: { ratio: number; budget: number; onChange: (v: number) => void }) {
  const [pct, setPct] = useState(Math.round(ratio * 100))
  const pctRef = useRef(pct)
  useEffect(() => { setPct(Math.round(ratio * 100)); pctRef.current = Math.round(ratio * 100) }, [ratio])
  const set = (v: number) => { pctRef.current = v; setPct(v) }
  const commit = () => {
    const next = pctRef.current
    if (Math.abs(next / 100 - ratio) > 0.001) onChange(next / 100)
  }
  return (
    <div className="auto-compact-threshold">
      <div className="auto-compact-threshold-head"><span>触发阈值</span><b>{pct}%</b></div>
      <input type="range" min={50} max={95} step={5} value={pct} aria-label="自动压缩阈值"
        onChange={(e) => set(Number(e.target.value))} onPointerUp={commit} onKeyUp={commit} onBlur={commit} />
      <div className="auto-compact-threshold-note">约 {fmtCtxTokens(Math.round(budget * pct / 100))} tokens 时压缩 · 默认 80%</div>
    </div>
  )
}

function ContextTab({ context, updatedAt, onUnpin, onPin, onCompact, compacting, autoCompact, onAutoCompact, onAutoCompactRatio }: {
  context: ContextView; updatedAt?: number; onUnpin: (id: string) => void; onPin: (title: string, text: string) => void; onCompact: () => void; compacting: boolean
  autoCompact: boolean; onAutoCompact: (v: boolean) => void; onAutoCompactRatio: (v: number) => void
}) {
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const pins = context.items.filter((it) => it.pin_id)
  return (
    <div className="lab2-ctx">
      <ContextStats context={context} updatedAt={updatedAt} />
      <div className="side-switch-row">
        <div>
          <b>自动压缩</b>
          <small>{autoCompact
            ? '上下文总量达到下方阈值时，下一回合前先把较早的对话压成摘要'
            : '已关闭：较早的对话只按条截断，不生成摘要；可随时手动「压缩历史」'}</small>
        </div>
        <button type="button" role="switch" aria-checked={autoCompact} aria-label="自动压缩" className={`switch${autoCompact ? ' on' : ''}`} onClick={() => onAutoCompact(!autoCompact)}><i /></button>
      </div>
      {autoCompact && (
        <AutoCompactThreshold ratio={context.auto_compact_ratio ?? 0.8} budget={context.budget}
          onChange={onAutoCompactRatio} />
      )}
      <div className="side-sub">
        <div className="side-sub-head"><b>钉住的材料</b><span>{pins.length ? `${pins.length} 份` : '没有'}</span></div>
        {pins.length > 0 && (
          <ul className="lab2-ctx-list">
            {pins.map((it) => (
              <li key={it.key} className="anim-in">
                <span>{it.title}</span>
                <span className="lab2-faint">{it.tokens.toLocaleString()}</span>
                <button className="link" onClick={() => onUnpin(it.pin_id!)}>移除</button>
              </li>
            ))}
          </ul>
        )}
        <details className="lab2-ctx-add">
          <summary>＋ 钉一段材料</summary>
          <input placeholder="标题" value={title} onChange={(e) => setTitle(e.target.value)} />
          <textarea rows={4} placeholder="调研结论、交易限制、观察到的现象" value={text} onChange={(e) => setText(e.target.value)} />
          <button className="button small" disabled={!title.trim() || !text.trim()} onClick={() => { onPin(title, text); setTitle(''); setText('') }}>钉住</button>
        </details>
      </div>
      <div className="button-row" style={{ justifyContent: 'flex-end' }}>
        <button className="button small" disabled={compacting} onClick={onCompact}>{compacting ? '正在压缩…' : '压缩历史'}</button>
      </div>
    </div>
  )
}

const FILE_NAMES: Record<string, string> = { strategy: 'strategy.py', factor: 'factor.expr', model: 'model_spec.json', portfolio: 'portfolio.json', execution: 'execution.json' }
const fileNameOf = (kind?: LabKind) => FILE_NAMES[kind ?? 'strategy'] ?? 'strategy.py'

/** 改动统计：相对空文件（整个对话累计）与相对上一版。 */
function changeStats(project: LabProject) {
  const code = project.code || ''
  const versions = project.versions || []
  const cur = versions.find((v) => v.v === project.current_v)
  const prev = versions.filter((v) => v.v < project.current_v).slice(-1)[0]
  const count = (a: string, b: string) => {
    const ops = lineDiff(a, b)
    return { add: ops.filter((o) => o.kind === 'add').length, del: ops.filter((o) => o.kind === 'del').length }
  }
  const total = code ? count('', code) : { add: 0, del: 0 }
  const last = cur && prev ? count(prev.code || '', cur.code || code) : null
  return { ...total, last, prevV: prev?.v ?? null }
}

/** 更改：只显示对文件做了多少改动，点开到文件编辑。 */
function ChangesCard({ project, stats, fileName, onOpen }: {
  project: LabProject; stats: ReturnType<typeof changeStats>; fileName: string; onOpen: () => void
}) {
  if (!project.code) return <div className="lab2-panel-empty small">还没有改动。AI 写出第一版后会显示在这里。</div>
  return (
    <div className="chg">
      <button type="button" className="chg-row" onClick={onOpen} title="打开文件编辑">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" aria-hidden><path d="M4 1.8h5.5L13 5.3v8.9H4Z" /><path d="M9.5 1.8v3.5H13" /></svg>
        <span className="chg-file">{fileName}</span>
        <span className="chg-num"><b className="add">+{stats.add}</b><b className="del">−{stats.del}</b></span>
      </button>
      {stats.last && (
        <div className="chg-sub">相对 v{stats.prevV}：<b className="add">+{stats.last.add}</b> <b className="del">−{stats.last.del}</b></div>
      )}
    </div>
  )
}

/** 目标概览：条件、范围、轮数；编辑在输入框上方弹出，不在侧栏里塞表单。 */
function GoalSummary({ project, metrics, hasGoal, running, rounds, onEdit, onStart }: {
  project: LabProject; metrics: ReturnType<typeof metricsOf>; hasGoal: boolean; running: boolean; rounds: number
  onEdit: () => void; onStart: () => void
}) {
  if (!hasGoal) {
    return (
      <div className="goal-sum empty">
        <p>还没有目标。现在是普通对话；设定目标后才能自动迭代。</p>
        <button type="button" className="button small primary" onClick={onEdit}>设定目标</button>
      </div>
    )
  }
  const spec = projectSpec(project)
  const range = spec.start || spec.end
    ? `${spec.start ? new Date(spec.start).toISOString().slice(0, 10) : '最早'} 至 ${spec.end ? new Date(spec.end).toISOString().slice(0, 10) : '最新'}`
    : `最近 ${spec.bars_limit.toLocaleString()} 根 K 线`
  return (
    <GoalCardView
      chips={spec.items.map((c) => ({ text: condText(c, metrics), priority: c.priority }))}
      rows={[
        { label: '标的', value: `${spec.symbols.join('、')} · ${spec.timeframe}` },
        { label: '区间', value: range },
        { label: '轮数', value: `最多 ${rounds} 轮` },
        ...(spec.note ? [{ label: '备注', value: spec.note }] : []),
      ]}
      actions={<>
        <button type="button" className="button small" onClick={onEdit}>编辑目标</button>
        <button type="button" className="button small primary" disabled={running} onClick={onStart}>开始自动迭代</button>
      </>}
    />
  )
}

/** 侧栏卡片：标题行可折叠（状态记在本机），被面板标签点名时自动展开并滚到可见处。 */
function SideCard({ id, title, meta, focus, children }: { id: PanelTab; title: string; meta?: string; focus: PanelTab | null; children: ReactNode }) {
  const [open, setOpen] = useState(() => store.get(`qa.lab.card.${id}`, id === 'code' || id === 'context' ? '1' : '0') === '1')
  const ref = useRef<HTMLElement>(null)
  useEffect(() => {
    if (focus !== id) return
    setOpen(true)
    requestAnimationFrame(() => ref.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
  }, [focus, id])
  const toggle = () => { setOpen(!open); store.set(`qa.lab.card.${id}`, open ? '0' : '1') }
  return (
    <section ref={ref} className={`side-card card-${id}${open ? ' open' : ''}${focus === id ? ' focus' : ''}`}>
      <button type="button" className="side-card-head" aria-expanded={open} onClick={toggle}>
        <span className="side-card-title">{title}</span>
        <svg className="side-card-chev" width="12" height="12" viewBox="0 0 12 12" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="m3 4.5 3 3 3-3" /></svg>
        <span className="side-card-meta">{meta}</span>
      </button>
      {open && <div className="side-card-body anim-expand">{children}</div>}
    </section>
  )
}

/* ------------------------------------------------------------------ */
/* /plan 计划卡片                                                     */
/* ------------------------------------------------------------------ */
function GoalEvent({ m, metrics }: { m: LabMessage; metrics: ReturnType<typeof metricsOf> }) {
  const d = m.data
  if (d.stage === 'early_stop') {
    return (
      <div className="lab2-card warn">
        <div className="lab2-card-head"><b>提前结束迭代</b></div>
        <p className="lab2-card-p">{d.reason}</p>
      </div>
    )
  }
  const check = d.check as { verdict: string; summary: string; issues: { problem: string; level: string }[]; model_summary?: string } | undefined
  const items = (d.spec?.items || []) as Parameters<typeof condText>[0][]
  return (
    <div className={`lab2-card${check?.verdict === 'unreasonable' ? ' warn' : ''}`}>
      <div className="lab2-card-head">
        <b>{d.stage === 'check' ? '开始前的目标检查' : d.source === 'model' ? 'AI 设定了目标' : '目标已更新'}</b>
        <span className="lab2-card-gap" />
        {check && <span className={`cb-badge ${check.verdict === 'ok' ? 'ok' : 'warn'}`}>{check.verdict === 'ok' ? '合理' : check.verdict === 'warn' ? '可执行' : '需调整'}</span>}
      </div>
      <div className="goal-chips static">
        {items.map((c, i) => <span key={i} className={`goal-chip${c.priority === 'try' ? ' try' : ''}`}>{condText(c, metrics)}</span>)}
        {d.range && <span className="goal-chip plain">{d.spec?.symbols?.join(', ')} · {d.spec?.timeframe} · {d.range}</span>}
      </div>
      {check && check.issues.length > 0 && (
        <ul className="lab2-issues">{check.issues.map((i, k) => <li key={k} className={i.level}>{i.problem}</li>)}</ul>
      )}
      {check?.model_summary && <div className="lab2-faint">模型复核：{check.model_summary}</div>}
    </div>
  )
}

interface Semantics {
  mode: string; label: string; continuous_compounding: boolean; reset_capital: boolean
  cross_segment_positions: string; trade_attribution_label: string; reconstructs_full: boolean; reconstruct_note: string
}

/** 回测口径：是否连续复利、是否重置资金、跨段持仓、交易归段、能否复原全区间收益 */
function SemanticsBox({ s }: { s: Semantics }) {
  const yes = (v: boolean) => <b className={v ? 'ok' : 'no'}>{v ? '是' : '否'}</b>
  return (
    <details className="lab2-sem">
      <summary>口径：{s.label}</summary>
      <dl>
        <dt>连续复利</dt><dd>{yes(s.continuous_compounding)}</dd>
        <dt>每段重置初始资金</dt><dd>{yes(s.reset_capital)}</dd>
        <dt>跨段持仓</dt><dd>{s.cross_segment_positions}</dd>
        <dt>交易归段</dt><dd>{s.trade_attribution_label}</dd>
        <dt>各段收益能否复原全区间</dt><dd>{yes(s.reconstructs_full)}　<span className="lab2-faint">{s.reconstruct_note}</span></dd>
      </dl>
    </details>
  )
}

function WalkForwardCard({ m }: { m: LabMessage }) {
  const d = m.data
  const rows = (d.segments || []) as { segment: number; start: number; end: number; ok: boolean; error?: string; metrics: Metrics; data_role?: string; data_role_label?: string; carried_position_in?: boolean }[]
  const day = (ms: number) => new Date(ms).toISOString().slice(0, 10)
  const rec = d.reconcile as { chained_return?: number | null; full_return?: number | null; reconstructs?: boolean; issues?: string[] } | undefined
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>分段回测</b>
        <span className={`cb-badge ${d.verdict === '稳定' ? 'ok' : 'warn'}`}>{d.verdict}</span>
      </div>
      {d.note && <div className="lab2-faint" style={{ marginTop: 4 }}>{d.note}</div>}
      {d.semantics && <SemanticsBox s={d.semantics as Semantics} />}
      <table className="lab2-wf">
        <thead><tr><th>区间</th><th>收益</th><th>夏普</th><th>回撤</th><th>交易</th><th>数据角色</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.segment}>
              <td>{day(r.start)} – {day(r.end)}{r.carried_position_in && <span className="lab2-faint" title="段首带着上一段未平仓的持仓"> ↪</span>}</td>
              {r.ok ? <>
                <td className={(r.metrics.total_return ?? 0) >= 0 ? 'up' : 'down'}>{pct(r.metrics.total_return)}</td>
                <td>{num(r.metrics.sharpe)}</td><td>{pct(r.metrics.max_drawdown)}</td><td>{r.metrics.trades ?? 0}</td>
              </> : <td colSpan={4} className="lab2-faint">{r.error || '失败'}</td>}
              <td>{r.data_role && <span className={`wf-role ${r.data_role}`} title={r.data_role_label}>{r.data_role === 'development' ? '已参与开发' : '开发中未见'}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rec && (
        <div className={`wf-rec${rec.issues?.length ? ' warn' : ''}`}>
          {rec.chained_return != null && rec.full_return != null && <span>各段连乘 {pct(rec.chained_return)} · 全区间 {pct(rec.full_return)} · {rec.reconstructs ? '可复原 ✓' : '不可复原'}</span>}
          {rec.issues?.map((x) => <div key={x}>⚠ {x}</div>)}
        </div>
      )}
    </div>
  )
}

/** 成本一致性：基础成本与压力成本的实际扣费、费率、滑点与净值变化是否对得上 */
function CostAudit({ a }: { a: { consistent: boolean; checks?: { name: string; ok: boolean; detail: string }[]; anomalies?: string[]; rule?: string; conclusion_allowed?: { cost_not_binding?: boolean } } }) {
  return (
    <details className={`lab2-sem cost-audit${a.consistent ? '' : ' bad'}`} open={!a.consistent}>
      <summary>{a.consistent ? '成本核对通过' : '成本口径异常'}{a.conclusion_allowed?.cost_not_binding === false && ' · 不能下「成本不是约束」的结论'}</summary>
      <ul>
        {(a.checks ?? []).map((c) => <li key={c.name} className={c.ok ? 'ok' : 'no'}>{c.ok ? '✓' : '✗'} {c.name}：{c.detail}</li>)}
        {!a.checks?.length && (a.anomalies ?? []).map((x) => <li key={x} className="no">✗ {x}</li>)}
      </ul>
    </details>
  )
}

/** 稳定性报告：滚动窗口、收益分布、基准对照、参数敏感性 */
function StabilityCard({ m }: { m: LabMessage }) {
  const d = m.data as {
    v?: number; flags: string[]; note?: string
    windows: { window: number; start?: number; end?: number; return: number; benchmark_return?: number | null; excess?: number | null; sharpe?: number | null }[]
    distribution: Record<string, number | null>
    sensitivity?: { note?: string; fragile?: boolean; runs?: { param: string; value: unknown; sharpe?: number | null; total_return?: number | null; ok?: boolean }[] }
  }
  const day = (ms?: number) => (ms ? new Date(ms).toISOString().slice(0, 10) : '—')
  const dist = d.distribution || {}
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        {d.v != null && <span className="lab2-ver">v{d.v}</span>}
        <b>稳定性报告</b>
        <span className={`cb-badge ${d.flags.length ? 'warn' : 'ok'}`}>{d.flags.length ? `${d.flags.length} 处需注意` : '稳定'}</span>
      </div>
      {d.flags.map((f) => <div key={f} className="wf-rec warn">⚠ {f}</div>)}
      <table className="lab2-wf">
        <thead><tr><th>滚动窗口</th><th>收益</th><th>基准</th><th>超额</th><th>夏普</th></tr></thead>
        <tbody>
          {d.windows.map((w) => (
            <tr key={w.window}>
              <td>{day(w.start)} – {day(w.end)}</td>
              <td className={w.return >= 0 ? 'up' : 'down'}>{pct(w.return)}</td>
              <td>{pct(w.benchmark_return ?? undefined)}</td>
              <td className={(w.excess ?? 0) >= 0 ? 'up' : 'down'}>{pct(w.excess ?? undefined)}</td>
              <td>{num(w.sharpe ?? undefined)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="stab-dist">
        <span>单根收益 P5 <b>{pct(dist.p05 ?? undefined)}</b></span>
        <span>P50 <b>{pct(dist.p50 ?? undefined)}</b></span>
        <span>P95 <b>{pct(dist.p95 ?? undefined)}</b></span>
        <span>偏度 <b>{num(dist.skew ?? undefined)}</b></span>
        <span>超额峰度 <b>{num(dist.excess_kurtosis ?? undefined)}</b></span>
        <span>最好 5% 贡献 <b>{pct(dist.top5pct_share_of_gains ?? undefined)}</b></span>
      </div>
      {d.sensitivity?.runs?.length ? (
        <details className="lab2-sem">
          <summary>参数敏感性：{d.sensitivity.note}</summary>
          <table className="lab2-wf">
            <thead><tr><th>参数</th><th>取值</th><th>夏普</th><th>收益</th></tr></thead>
            <tbody>
              {d.sensitivity.runs.map((r, i) => (
                <tr key={i}><td>{r.param}</td><td>{String(r.value)}</td><td>{num(r.sharpe ?? undefined)}</td>
                  <td className={(r.total_return ?? 0) >= 0 ? 'up' : 'down'}>{pct(r.total_return ?? undefined)}</td></tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}
      {d.note && <div className="lab2-faint" style={{ marginTop: 6 }}>{d.note}</div>}
    </div>
  )
}
