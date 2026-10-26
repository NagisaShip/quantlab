import TaskResult from '../features/workflows/TaskResult'
/**
 * 页面内「问 AI」：右下角一个安静的入口，点开是右侧面板（像 Claude 的侧边栏）。
 *
 * - 任何页面用 `useAskAIContext({ title, text, code })` 登记自己的内容，面板输入框上方显示「已附带：…」，可移除；
 * - 页面资料随每次提问一起发给后端，后端把它当作参考数据（其中的指令一律忽略），并截断到合理长度；
 * - 对话、思考、模型选择复用策略室的 Composer / ModelMenu / ContextRing 与对话块；
 * - 「在研究室继续」把对话记录作为钉住材料、把策略代码作为 code 带进 AI 研究室。
 *
 * 登记表放在模块级的小 store 里，所以 App 里只需挂一个 <AskAIPanel />，页面不需要被 Provider 包住。
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { useDraft } from './draft'
import { askStream, askAI, type AiAction, type AskContextItem, type AskMeta, type AskResearchContext, type AskResearchOutputRef, type AskTurn, type AskUpgradeIn, type AskUpgradeTurn, type ToolCatalog, type ToolRecord } from '../apiAskAI'
import { fmtMoney, fmtNum, fmtPct, type BacktestResult } from '../api/research'
import type { ContextView, Effort } from '../apiLab'
import { Composer, ContextRing, ModelMenu, PlusMenu } from './chat/Composer'
import { AssistantText, EventLine, UserBubble, Working } from './chat/Blocks'
import WorkflowTrace from './chat/WorkflowTrace'
import ArtifactPreview from '../features/workflows/ArtifactPreview'
import ResearchContextStrip, { outputKey } from './ResearchContextStrip'
import { openInLab } from './handoff'
import { QuantBot } from './QuantBot'
import { BotMark, EphemeralChip, BOT_NAME, BOT_VOICE, upgradeFlashKey } from './Brand'
import { labChanged } from '../pages/lab/bus'
import { currentWorkspace } from '../http'

/* ------------------------------------------------------------------ */
/* 页面资料登记                                                         */
/* ------------------------------------------------------------------ */
export interface AskAIContext {
  /** 显示在「已附带：」后面，例如 回测「ETH RSI」的成绩与参数 */
  title: string
  /** 资料正文（成绩、参数等纯文本） */
  text: string
  /** 策略代码：单独作为一份资料发送，「在研究室继续」时也会带过去 */
  code?: string
  /** 资料类型：backtest / strategy / page … */
  kind?: string
  /** 可选、版本化的研究上下文：身份 + 真实输出引用，不内联完整编排图。 */
  research?: AskResearchContext
}

let seq = 0
let entries: { id: number; ctx: AskAIContext }[] = []
const listeners = new Set<() => void>()
const emit = () => listeners.forEach((l) => l())
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } }
const snapshot = () => entries

/** 页面登记自己的内容；传 null 表示暂时没有（例如还在加载）。离开页面时自动撤销。 */
export function useAskAIContext(ctx: AskAIContext | null | undefined) {
  const title = ctx?.title ?? ''
  const text = ctx?.text ?? ''
  const code = ctx?.code ?? ''
  const kind = ctx?.kind ?? ''
  const research = ctx?.research
  const researchKey = research ? JSON.stringify(research) : ''
  const active = !!ctx && !!(text.trim() || code.trim() || research)
  useEffect(() => {
    if (!active) return
    const id = ++seq
    entries = [...entries, { id, ctx: { title, text, code, kind, ...(research ? { research } : {}) } }]
    emit()
    return () => {
      entries = entries.filter((e) => e.id !== id)
      emit()
    }
  }, [active, title, text, code, kind, researchKey])
}

/** 回测详情页的资料：成绩、参数、成本假设与数据来源，代码另附。 */
export function backtestAskContext(bt: BacktestResult): AskAIContext {
  const m = bt.metrics
  const day = (ts?: number) => (ts ? new Date(ts).toISOString().slice(0, 10) : '—')
  const start = bt.row?.start_ts ?? bt.timestamps[0]
  const end = bt.row?.end_ts ?? bt.timestamps[bt.timestamps.length - 1]
  const lines = [
    `策略：${bt.strategy_name}`,
    `标的：${bt.securities.join('、')} · 周期 ${bt.timeframe} · 区间 ${day(start)} 至 ${day(end)} · 交易天数 ${m.trading_days}`,
    `数据来源：${bt.datasets.map((d) => d.source).filter(Boolean).join('、') || '—'}`,
    '',
    '成绩：',
    `策略收益 ${fmtPct(m.total_return, 2, true)}；年化 ${fmtPct(m.annualized_return, 2, true)}；基准收益 ${fmtPct(m.benchmark_return, 2, true)}；超额 ${fmtPct(m.excess_return, 2, true)}`,
    `夏普 ${fmtNum(m.sharpe, 3)}；索提诺 ${fmtNum(m.sortino, 3)}；卡玛 ${fmtNum(m.calmar, 3)}；信息比率 ${fmtNum(m.information_ratio, 3)}；Alpha ${fmtNum(m.alpha, 3)}；Beta ${fmtNum(m.beta, 3)}`,
    `最大回撤 ${fmtPct(m.max_drawdown)}（持续 ${m.max_drawdown_duration} 根）；波动率 ${fmtPct(m.volatility)}；连续亏损 ${m.consecutive_losses} 笔`,
    `胜率 ${fmtPct(m.win_rate)}；日胜率 ${fmtPct(m.daily_win_rate)}；盈亏比 ${fmtNum(m.payoff_ratio)}；利润因子 ${fmtNum(m.profit_factor)}`,
    `交易 ${m.trades} 笔 / ${m.fills} 次成交；换手 ${fmtNum(m.turnover)} 倍；手续费 ${fmtMoney(m.total_fees)}；初始 ${fmtMoney(m.initial_equity)} → 期末 ${fmtMoney(m.final_equity)}`,
    '',
    `参数：${JSON.stringify(bt.params ?? {})}`,
    `成本假设：${JSON.stringify(bt.cost_model ?? {})}`,
  ]
  if (bt.warnings?.length) lines.push(`警告：${bt.warnings.slice(0, 8).join('；')}`)
  if (bt.errors?.length) lines.push(`错误：${bt.errors.slice(0, 8).join('；')}`)
  if (m.notes?.length) lines.push(`备注：${m.notes.slice(0, 6).join('；')}`)
  return { kind: 'backtest', title: `回测「${bt.strategy_name}」的成绩与参数`, text: lines.join('\n'), code: bt.code }
}

/* ------------------------------------------------------------------ */
/* 面板                                                                */
/* ------------------------------------------------------------------ */
type Msg =
  | { role: 'user'; text: string; attached: string[] }
  | {
      role: 'assistant'; content: string; thinking: string; thinkingMs?: number; streaming: boolean
      model?: string; fallback?: boolean; code?: string; error?: string; stopped?: boolean; tools?: ToolRecord[]
      /** 服务端建议升级为研究会话（只建议，用户点按钮才创建）。 */
      suggest?: { reason: string }
    }

const HIDDEN_ON = new Set(['lab', 'teams'])   // 全屏对话页自己就是对话，不再叠一个入口
const PREF_KEY = 'qa.askai.prefs'
const SYSTEM_TOKENS = 2600                   // 系统提示 + SDK 速查的大致 token 数
const MAX_TURNS = 12

/** 每个工作流一份内存会话；非工作流页面继续共用 global，保持旧行为。 */
const conversations = new Map<string, Msg[]>()

/** 已经升级过的临时问答：scope → 研究会话 id。重开面板时显示「已升级 · 打开会话」。 */
const upgradedScopes = new Map<string, string>()

/** 各页面的起手问题：点一下填进输入框（可以再改） */
const STARTERS: Record<string, string[]> = {
  default: ['这个页面怎么用？', '这里的数字应该怎么解读？', '下一步我该做什么？'],
  home: ['今天市场有什么值得研究的？', '我的策略哪一个最接近能上线？', '帮我规划这周的研究'],
  backtests: ['这些回测里哪一次最可信？为什么？', '超额收益为负说明了什么？', '哪些回测数据不足、只能算非正式？'],
  backtest: ['这次回测最大的问题是什么？', '手续费和换手对收益的影响有多大？', '怎么判断是不是过拟合？'],
  data: ['数据有缺口会影响回测吗？', '我该同步哪些标的、多长的历史？', '波动率怎么解读？'],
  factors: ['这个因子为什么没通过入库？', '多个因子高度相关时怎么办？', 'IC 和多空夏普哪个更重要？'],
  modellib: ['模型比线性基线好多少才算有意义？', '怎么检查特征泄漏？', '什么时候该重训？'],
  portfolio: ['风险平价和等权差在哪？', '目标波动该设多少？', '怎么控制相关性带来的集中风险？'],
  editor: ['这段代码有没有未来函数？', '止损逻辑在跳空时怎么成交？', '帮我把参数改成可优化的'],
  validation: ['为什么不能晋级？差在哪里？', 'DSR 是什么意思？', '纸面模拟要观察哪些指标？'],
  paper: ['模拟和回测差多少算正常？', '怎么判断滑点设得合不合理？'],
  square: ['怎么判断别人分享的回测靠不靠谱？', '克隆后应该先检查什么？'],
}

/** 页面没有登记资料时，取主区域的可见文字作为上下文（去掉按钮、导航等噪声）。 */
function pageSnapshot(route: string): AskAIContext | null {
  const main = document.querySelector('main')
  if (!main) return null
  const clone = main.cloneNode(true) as HTMLElement
  clone.querySelectorAll('button, nav, script, style, svg, .ask-fab-group, .hub-tabs').forEach((n) => n.remove())
  const text = (clone.innerText || clone.textContent || '').replace(/\n{3,}/g, '\n\n').trim()
  if (!text) return null
  const title = document.title.replace(/ · QuantLab v0.1.0-preview.2$/, '') || route
  return { kind: 'page', title: `「${title}」页面内容`, text: `页面：${title}（#/${window.location.hash.replace(/^#\/?/, '')}）\n\n${text.slice(0, 6000)}` }
}

const routeHead = () => window.location.hash.replace(/^#\/?/, '').split('/')[0]
const estTokens = (s: string) => Math.ceil(s.length / 2)

interface Prefs { model: string; effort: Effort; scenes?: string[] | null }

function readPrefs(): Prefs {
  try {
    const p = JSON.parse(localStorage.getItem(PREF_KEY) || '{}')
    return { model: typeof p.model === 'string' ? p.model : '', effort: (['low', 'medium', 'high', 'max'] as const).includes(p.effort) ? p.effort : 'medium',
      scenes: Array.isArray(p.scenes) ? p.scenes.filter((x: unknown) => typeof x === 'string') : null }
  } catch {
    return { model: '', effort: 'medium', scenes: null }
  }
}

function writePrefs(p: Prefs) {
  try { localStorage.setItem(PREF_KEY, JSON.stringify(p)) } catch { /* 隐私模式 */ }
}

/** 页面资料 → 发给后端的条目（代码单独一条；研究上下文走版本化 envelope，不重复内联）。 */
function toItems(list: AskAIContext[]): AskContextItem[] {
  const out: AskContextItem[] = []
  for (const c of list) {
    const text = c.research?.version === 1 ? '' : c.text
    if (text.trim()) out.push({ kind: c.kind || 'page', title: c.title, text })
    if (c.code?.trim()) out.push({ kind: 'code', title: text.trim() ? `${c.title}（代码）` : c.title, text: c.code })
  }
  return out
}

const chipLabel = (c: AskAIContext) => (c.code?.trim() && c.text.trim() && !c.title.includes('代码') ? `${c.title} · 代码` : c.title)

/**
 * 研究室 handoff 的研究资料：只带身份、选中节点与实际选中的输出引用。
 * 不内联编排图/数据/二进制，也不生成新引用；已移除的输出不会出现。
 */
export function researchHandoffPin(research: AskResearchContext | null | undefined, outputs: AskResearchOutputRef[]) {
  if (!research) return null
  const workflow = research.workflow
  const node = research.selected_node
  const run = research.run
  const payload = {
    format: 'quantlab.research-handoff/1',
    workspace_id: research.workspace_id || '',
    project_name: research.project_name || '',
    workflow: {
      id: workflow.id,
      title: workflow.title,
      revision: workflow.revision ?? null,
      saved_revision: workflow.saved_revision ?? null,
      saved: workflow.saved,
      unsaved: workflow.unsaved,
    },
    selected_node: node ? {
      id: node.id,
      title: node.title,
      kind: node.kind,
      status: node.status,
      ...(typeof node.verified === 'boolean' ? { verified: node.verified } : {}),
    } : null,
    run: run ? {
      id: run.id,
      mode: run.mode ?? '',
      status: run.status ?? '',
      revision: run.revision ?? null,
      view_mode: run.view_mode ?? (run.current ? 'latest' : 'frozen'),
      follows_latest: run.follows_latest ?? null,
      ...(typeof run.is_latest === 'boolean' ? { is_latest: run.is_latest } : {}),
    } : null,
    outputs: outputs.slice(0, 6).map(({ port, type, hash, run_id, node_id }) => ({ port, type, hash, run_id, node_id })),
  }
  const title = `研究上下文 · ${workflow.title || '当前研究'}`.slice(0, 120)
  return { kind: 'note', title, text: JSON.stringify(payload, null, 2).slice(0, 8000) }
}

export interface AskAIPanelProps {
  /**
   * 嵌入模式：内容渲染进宿主（研究工作区右栏）的页签里，不再渲染固定浮层；
   * 入口按钮通过 `entryHost` 挂到宿主右上角。打开/关闭由宿主控制。
   */
  embedded?: boolean
  /** 嵌入模式的打开态（右栏「问 AI」页签是否激活）。 */
  open?: boolean
  /** 嵌入模式：入口按钮请求打开页签。 */
  onOpen?: () => void
  /** 嵌入模式：关闭按钮 / Esc 请求回到检查器页签。 */
  onClose?: () => void
  /** 嵌入模式：入口按钮（`.ask-fab-group`）的 portal 挂载点。 */
  entryHost?: HTMLElement | null
  /** 可选：宿主托管「随本次提问发送的输出引用」的移除状态，供上下文页签共享。 */
  removedOutputs?: Record<string, string[]>
  onToggleOutput?: (scope: string, ref: AskResearchOutputRef) => void
  /** 可选：从上下文页签请求把选中节点写进问题。nonce 保证重复点击也生效。 */
  referenceRequest?: { node: { id: string; title: string }; nonce: number } | null
}

export function AskAIPanel({
  embedded = false, open: openProp = false, onOpen, onClose, entryHost = null,
  removedOutputs: controlledRemovedOutputs, onToggleOutput, referenceRequest = null,
}: AskAIPanelProps = {}) {
  const registered = useSyncExternalStore(subscribe, snapshot, snapshot)
  const researchEntry = useMemo(() => registered.find((e) => e.ctx.research?.version === 1), [registered])
  const researchCtx = researchEntry?.ctx.research ?? null
  const scope = useMemo(() => {
    if (!researchCtx) return 'global'
    const workspace = researchCtx.workspace_id || currentWorkspace() || 'ws'
    const identity = researchCtx.workflow.id || researchCtx.draft_id || 'draft'
    return `wf:${workspace}:${identity}`
  }, [researchCtx])
  const [route, setRoute] = useState(routeHead)
  const [openState, setOpenState] = useState(false)
  // 嵌入模式由宿主页签控制打开态；浮层模式沿用自身的开合状态。
  const open = embedded ? openProp : openState
  const [msgs, setMsgs] = useState<Msg[]>(() => conversations.get(scope) ?? [])
  const [input, setInput] = useDraft(scope === 'global' ? 'ask' : `ask:${scope}`)
  const [runningScopes, setRunningScopes] = useState<string[]>([])
  const [dismissed, setDismissed] = useState<string[]>([])
  const [localRemovedOutputs, setLocalRemovedOutputs] = useState<Record<string, string[]>>({})
  const removedOutputs = controlledRemovedOutputs ?? localRemovedOutputs
  const setRemovedOutputs = setLocalRemovedOutputs
  const [meta, setMeta] = useState<AskMeta | null>(null)
  const [prefs, setPrefs] = useState(readPrefs)
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null)
  const [showTools, setShowTools] = useState(false)
  const controllers = useRef(new Map<string, AbortController>())
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const previousScope = useRef(scope)
  const mounted = useRef(true)
  // 嵌入模式随 WorkflowPage 按 arg 重新挂载：工作流切换不是“离开对话”，
  // 不能让卸载把进行中的流 abort 成“已停止”。真正离开工作区时也让长任务继续跑，
  // 完成结果写入模块级 conversations，回到该 scope 时可见。
  const embeddedRef = useRef(embedded)
  embeddedRef.current = embedded
  const feed = useRef<HTMLDivElement>(null)
  const fabRef = useRef<HTMLButtonElement>(null)
  const previousFocus = useRef<HTMLElement | null>(null)
  const running = runningScopes.includes(scope)
  // 临时问答 → 研究会话：已升级的目标会话、升级中/失败状态与建议卡忽略列表
  const [upgradedPid, setUpgradedPid] = useState<string | null>(() => upgradedScopes.get(scope) ?? null)
  const [upgradeBusy, setUpgradeBusy] = useState(false)
  const [upgradeError, setUpgradeError] = useState('')
  const [upgradeDone, setUpgradeDone] = useState(false)
  const [suggestDismissed, setSuggestDismissed] = useState<number[]>([])

  const closePanel = useCallback(() => {
    if (embedded) {
      // 关闭 = 回到检查器页签；焦点交还给入口按钮，保持与浮层一致的键盘行为。
      onClose?.()
      requestAnimationFrame(() => {
        const target = previousFocus.current?.isConnected ? previousFocus.current : fabRef.current
        target?.focus({ preventScroll: true })
      })
      return
    }
    setOpenState(false)
    requestAnimationFrame(() => {
      const target = previousFocus.current?.isConnected ? previousFocus.current : fabRef.current
      target?.focus({ preventScroll: true })
    })
  }, [embedded, onClose])
  const openPanel = useCallback(() => {
    if (embedded) {
      onOpen?.()
      return
    }
    previousFocus.current = document.activeElement as HTMLElement | null
    setOpenState(true)
  }, [embedded, onOpen])

  useEffect(() => {
    document.documentElement.dataset.askaiOpen = open ? 'true' : ''
    return () => { delete document.documentElement.dataset.askaiOpen }
  }, [open])

  useEffect(() => {
    const onHash = () => { setRoute(routeHead()); setDismissed([]) }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const hidden = HIDDEN_ON.has(route)
  useEffect(() => {
    if (!hidden) return
    if (embedded) onClose?.()
    else setOpenState(false)
  }, [hidden, embedded, onClose])

  useEffect(() => {
    if (!open || meta) return
    askAI.meta().then(setMeta).catch(() => undefined)
    askAI.tools().then(setCatalog).catch(() => undefined)
  }, [open, meta])

  // 本轮加载的工具场景：用户在「更多」里改过就用用户的，否则按页面默认
  const pageScenes = catalog?.page_scenes[route] ?? []
  const scenes = prefs.scenes ?? pageScenes
  const toggleScene = (id: string) => {
    const next = scenes.includes(id) ? scenes.filter((x) => x !== id) : [...scenes, id]
    const p = { ...prefs, scenes: next }
    setPrefs(p); writePrefs(p)
  }

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !document.querySelector('.cmp-menu')) closePanel() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, closePanel])

  useEffect(() => {
    const el = feed.current
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight
  }, [msgs])

  useEffect(() => {
    if (previousScope.current === scope) return
    previousScope.current = scope
    setMsgs(conversations.get(scope) ?? [])
    // 切换工作流/会话：升级状态跟着 scope 走，避免把 A 的升级结果显示在 B 上
    setUpgradedPid(upgradedScopes.get(scope) ?? null)
    setUpgradeBusy(false)
    setUpgradeError('')
    setUpgradeDone(false)
    setSuggestDismissed([])
  }, [scope])

  // React StrictMode replays mount setup/cleanup in development. Registering the
  // mounted flag in setup (not only clearing it in cleanup) keeps later state/stream
  // routing effective after that replay and still blocks updates after a real unmount.
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      if (embeddedRef.current) return
      controllers.current.forEach((ctrl, target) => {
        ctrl.abort()
        const list = conversations.get(target) ?? []
        conversations.set(target, list.map((m) => m.role === 'assistant' && m.streaming ? { ...m, streaming: false, stopped: true } : m))
      })
      controllers.current.clear()
    }
  }, [])

  const applyMsgs = useCallback((target: string, update: (list: Msg[]) => Msg[]) => {
    const next = update(conversations.get(target) ?? [])
    conversations.set(target, next)
    if (mounted.current && scopeRef.current === target) setMsgs(next)
  }, [])

  const legacyAttached = useMemo(
    () => registered.map((e) => e.ctx).filter((c) => !c.research && !dismissed.includes(c.title)),
    [registered, dismissed],
  )
  const attached = useMemo(
    () => (researchEntry ? [researchEntry.ctx, ...legacyAttached] : legacyAttached),
    [researchEntry, legacyAttached],
  )
  const items = useMemo(() => toItems(attached), [attached])
  const availableOutputs = useMemo(() => (researchCtx?.outputs ?? []).slice(0, 6), [researchCtx])
  const removedKeys = removedOutputs[scope] ?? []
  const attachedOutputs = useMemo(
    () => availableOutputs.filter((ref) => !removedKeys.includes(outputKey(ref))),
    [availableOutputs, removedKeys],
  )
  const toggleOutput = (ref: AskResearchOutputRef) => {
    if (onToggleOutput) {
      onToggleOutput(scope, ref)
      return
    }
    const key = outputKey(ref)
    setRemovedOutputs((previous) => {
      const current = previous[scope] ?? []
      const next = current.includes(key) ? current.filter((item) => item !== key) : [...current, key]
      return { ...previous, [scope]: next }
    })
  }
  const referenceNode = (node: { id: string; title: string }) => {
    const token = `（请结合选中节点「${node.title || node.id}」/ ${node.id} 回答）`
    setInput(input.includes(token) ? input : `${input}${input.trim() ? ' ' : ''}${token}`)
    requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('.ask-panel textarea')?.focus())
  }
  // 上下文页签的「引用」：由宿主转成 referenceRequest，这里复用同一段插入逻辑。
  const referenceNonce = referenceRequest?.nonce ?? 0
  useEffect(() => {
    if (!embedded || !referenceRequest) return
    const token = `（请结合选中节点「${referenceRequest.node.title || referenceRequest.node.id}」/ ${referenceRequest.node.id} 回答）`
    setInput(input.includes(token) ? input : `${input}${input.trim() ? ' ' : ''}${token}`)
    requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('.ask-panel textarea')?.focus())
    // 只在宿主发起新的引用请求时执行（nonce 变化）。
  }, [embedded, referenceNonce])

  const history = useCallback((list: Msg[]): AskTurn[] => list
    .filter((m) => (m.role === 'user' ? m.text : m.content && !m.error))
    .map((m) => (m.role === 'user' ? { role: 'user' as const, content: m.text } : { role: 'assistant' as const, content: m.content }))
    .slice(-MAX_TURNS * 2), [])

  // 升级时带上的问答：只保留有正文的轮次（流式中、报错、空回复都不算）
  const upgradeTurns = useMemo<AskUpgradeTurn[]>(() => msgs.flatMap((m): AskUpgradeTurn[] => {
    if (m.role === 'user') return m.text.trim() ? [{ role: 'user', content: m.text.trim() }] : []
    return m.content.trim() && !m.error && !m.streaming ? [{ role: 'assistant', content: m.content.trim() }] : []
  }), [msgs])
  const canUpgrade = !!researchCtx && upgradeTurns.length > 0
  // 最后一条回答给出的升级建议（只提示，不自动创建；可被用户忽略）
  const lastAssistantIndex = useMemo(() => {
    for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === 'assistant') return i
    return -1
  }, [msgs])
  const lastAssistant = lastAssistantIndex >= 0 ? msgs[lastAssistantIndex] as Extract<Msg, { role: 'assistant' }> : null
  const suggestion = !upgradedPid && lastAssistant?.suggest && !lastAssistant.streaming && !suggestDismissed.includes(lastAssistantIndex)
    ? lastAssistant.suggest : null

  /** 临时问答 → 研究会话：带着对话、页面资料与研究上下文调 from-ask，成功后跳到新会话。 */
  const upgradeToSession = async () => {
    if (!researchCtx || upgradeBusy || !upgradeTurns.length) return
    setUpgradeBusy(true)
    setUpgradeError('')
    const firstQuestion = msgs.find((m) => m.role === 'user' && m.text.trim())
    const idea = (firstQuestion?.role === 'user' ? firstQuestion.text.trim() : '') || researchCtx.workflow.title
    const title = (researchCtx.workflow.title || idea).replace(/\s+/g, ' ').trim().slice(0, 60) || '研究会话'
    const body: AskUpgradeIn = {
      title,
      idea: idea.slice(0, 2000),
      // 来源引用：页面与附带资料放进 context；工作流/运行/节点/输出由服务端从 research 提炼
      context: {
        source: 'ask',
        page: route,
        ...(items.length ? { items } : {}),
      },
      messages: upgradeTurns,
      research: {
        ...researchCtx,
        outputs: attachedOutputs.map(({ port, type, hash, run_id, node_id }) => ({ port, type, hash, run_id, node_id })),
      },
    }
    try {
      const view = await askAI.upgrade(body)
      const pid = view.project.id
      upgradedScopes.set(scope, pid)
      setUpgradedPid(pid)
      setUpgradeDone(true)
      labChanged()
      try { sessionStorage.setItem(upgradeFlashKey(pid), BOT_VOICE.upgraded) } catch { /* 存储不可用 */ }
      // 先让「已升级」状态可见，再关闭浮层并跳到新会话
      window.setTimeout(() => {
        closePanel()
        window.location.hash = `/lab/${pid}`
      }, 900)
    } catch (e) {
      // 升级失败：会话与消息原样保留，只提示错误
      setUpgradeError((e as Error).message || BOT_VOICE.upgradeFailed)
    } finally {
      setUpgradeBusy(false)
    }
  }

  const send = async () => {
    const question = input.trim()
    if (!question || running || controllers.current.has(scope)) return
    const target = scope
    // 继续追问后，这段问答不再算「已升级」：允许把扩展后的对话再次升级
    if (upgradedScopes.has(target)) { upgradedScopes.delete(target); setUpgradedPid(null); setUpgradeDone(false) }
    setUpgradeError('')
    setSuggestDismissed([])
    const prior = history(msgs)
    const requestContext = items
    const requestAttached = attached
    const requestResearch: AskResearchContext | undefined = researchCtx ? {
      ...researchCtx,
      outputs: attachedOutputs.map(({ port, type, hash, run_id, node_id }) => ({ port, type, hash, run_id, node_id })),
    } : undefined
    const requestRoute = route
    const requestPrefs = prefs
    const updateLastFor = (fn: (m: Extract<Msg, { role: 'assistant' }>) => Partial<Extract<Msg, { role: 'assistant' }>>) =>
      applyMsgs(target, (list) => {
        const last = list[list.length - 1]
        if (!last || last.role !== 'assistant') return list
        return [...list.slice(0, -1), { ...last, ...fn(last) }]
      })
    setInput('')
    applyMsgs(target, (list) => [...list,
      { role: 'user', text: question, attached: requestAttached.map(chipLabel) },
      { role: 'assistant', content: '', thinking: '', streaming: true }])
    const ctrl = new AbortController()
    controllers.current.set(target, ctrl)
    if (mounted.current) setRunningScopes((list) => list.includes(target) ? list : [...list, target])
    const t0 = performance.now()
    try {
      await askStream({ question, history: prior, context: requestContext, model: requestPrefs.model, effort: requestPrefs.effort, page: requestRoute, scenes: requestPrefs.scenes ?? null, research: requestResearch }, (e) => {
        if (e.type === 'tool') {
          const { type: _t, ...rec } = e
          updateLastFor((m) => ({ tools: [...(m.tools ?? []), rec] }))
        } else if (e.type === 'delta') {
          if (e.kind === 'thinking') updateLastFor((m) => ({ thinking: m.thinking + e.text, thinkingMs: performance.now() - t0 }))
          else updateLastFor((m) => ({ content: m.content + e.text }))
        } else if (e.type === 'reset') {
          updateLastFor(() => ({ content: '', thinking: '' }))
        } else if (e.type === 'final') {
          updateLastFor((m) => ({
            content: e.content, thinking: e.thinking || m.thinking, streaming: false, model: e.model,
            fallback: e.mode === 'fallback', code: e.code || undefined, tools: e.tool_calls?.length ? e.tool_calls : m.tools,
            suggest: e.suggest_session ? { reason: e.reason || '' } : undefined,
          }))
        } else if (e.type === 'error') {
          updateLastFor(() => ({ streaming: false, error: e.message }))
        }
      }, ctrl.signal)
      updateLastFor((m) => (m.streaming ? { streaming: false, error: m.content ? undefined : '连接已断开' } : {}))
    } catch (err) {
      if (ctrl.signal.aborted) updateLastFor(() => ({ streaming: false, stopped: true }))
      else updateLastFor(() => ({ streaming: false, error: (err as Error).message || '请求失败' }))
    } finally {
      if (controllers.current.get(target) === ctrl) controllers.current.delete(target)
      if (mounted.current) setRunningScopes((list) => list.filter((item) => item !== target))
    }
  }

  const stop = () => controllers.current.get(scope)?.abort()

  const reset = () => {
    controllers.current.get(scope)?.abort()
    applyMsgs(scope, () => [])
    setInput('')
    upgradedScopes.delete(scope)
    setUpgradedPid(null)
    setUpgradeDone(false)
    setUpgradeError('')
    setSuggestDismissed([])
  }

  const continueInLab = () => {
    const turns = history(msgs)
    let transcript = turns.map((t) => `${t.role === 'user' ? '我' : 'AI'}：${t.content}`).join('\n\n')
    if (transcript.length > 8000) transcript = '…（更早的对话已省略）\n\n' + transcript.slice(-8000)
    const lastCode = [...msgs].reverse().find((m) => m.role === 'assistant' && m.code)
    const code = (lastCode?.role === 'assistant' && lastCode.code) || attached.find((c) => c.code?.trim())?.code || undefined
    const name = attached.find((c) => c.code?.trim())?.title.match(/「(.+?)」/)?.[1]
    const researchPin = researchHandoffPin(researchCtx, attachedOutputs)
    const pins = [
      ...(transcript ? [{ kind: 'note', title: '「问 AI」对话记录', text: transcript }] : []),
      ...(researchPin ? [researchPin] : []),
      ...attached.filter((c) => !c.research && c.text.trim()).map((c) => ({ kind: 'note', title: c.title, text: c.text.slice(0, 8000) })),
    ]
    if (embedded) onClose?.()
    else setOpenState(false)
    openInLab({
      idea: input.trim() || '接着「问 AI」里的讨论继续：',
      title: name,
      code,
      pins,
      attached: ['「问 AI」的对话', ...attached.map((c) => c.title)].slice(0, 3).join('、') + (code ? '，以及策略代码' : ''),
    })
  }

  /** 主入口：跳到 AI 研究室的新对话，当前页面登记的资料（没有就取页面正文摘要）钉成上下文。 */
  const newConversation = () => {
    const list = attached.length ? attached : [pageSnapshot(route)].filter((c): c is AskAIContext => !!c)
    const code = list.find((c) => c.code?.trim())?.code || undefined
    const name = list.find((c) => c.code?.trim())?.title.match(/「(.+?)」/)?.[1]
    const researchPin = researchHandoffPin(researchCtx, attachedOutputs)
    openInLab({
      idea: '',
      title: name,
      code,
      pins: [
        ...(researchPin ? [researchPin] : []),
        ...list.filter((c) => !c.research && c.text.trim()).map((c) => ({ kind: 'note', title: c.title, text: c.text.slice(0, 8000) })),
      ],
      attached: list.map((c) => c.title).slice(0, 3).join('、') + (code ? '，以及代码' : ''),
    })
  }

  const context: ContextView = useMemo(() => {
    const target = prefs.model || meta?.default_model || ''
    const budget = meta?.models.find((m) => m.target === target)?.context_window || 128_000
    const hist = history(msgs).reduce((n, t) => n + estTokens(t.content.slice(0, 6000)), 0)
    const list = [
      { key: 'system', title: '系统提示与 SDK 速查', tokens: SYSTEM_TOKENS, fixed: true },
      ...(researchCtx ? [{ key: 'research', title: '研究上下文（身份与输出引用）', tokens: estTokens(JSON.stringify(researchCtx).slice(0, 8000)), fixed: false }] : []),
      ...items.map((it, i) => ({ key: `ctx-${i}`, title: it.title, tokens: estTokens(it.text.slice(0, 8000)), fixed: false })),
      { key: 'history', title: '对话', tokens: hist, fixed: false },
    ]
    return { items: list, total: list.reduce((n, it) => n + it.tokens, 0), history_tokens: hist, budget, model: target }
  }, [prefs.model, meta, items, msgs, history, researchCtx])

  if (hidden) return null

  const canHandoff = msgs.some((m) => m.role === 'assistant' && m.content) || attached.some((c) => c.code?.trim())

  // 入口按钮：浮层模式挂在右下角；嵌入模式 portal 到宿主（研究工作区右上角），
  // 点击小入口切换到右栏「问 AI」页签，主按钮仍进入完整 AI 研究室。
  const entry = (
    <div className={`ask-fab-group${route === 'groups' ? ' ask-fab-safe' : ''}${embedded ? ' ask-fab-inline' : ''}`}>
      <button ref={fabRef} type="button" className="ask-fab-mini" onClick={openPanel} title="在侧栏快速提问（不离开本页）" aria-label="在侧栏快速提问">
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><rect x="1.5" y="2" width="11" height="10" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" /><path d="M8.5 2v10" stroke="currentColor" strokeWidth="1.3" /></svg>
      </button>
      <button type="button" className="ask-fab" onClick={openPanel} title="打开临时问答（不保存，需要留存时升级为研究会话）">
        <span aria-hidden>✦</span>问 AI
      </button>
    </div>
  )

  const panel = (
        <aside className={`ask-panel${embedded ? ' ask-panel-embedded' : ''}`} role="complementary" aria-label={`${BOT_NAME} 临时问答`}>
          <header className="ask-head">
            <div className="ask-head-id">
              <BotMark size={20} state={running ? 'thinking' : 'idle'} />
              <EphemeralChip />
            </div>
            <div className="ask-head-actions">
              {!researchCtx && canHandoff && (
                <button type="button" className="ask-head-btn" onClick={continueInLab} title="把对话和代码带进 AI 研究室">在研究室继续</button>
              )}
              <button type="button" className="ask-head-btn" onClick={() => { closePanel(); newConversation() }} title="新建一个持久研究项目（需要保存时）">新建研究</button>
              {msgs.length > 0 && <button type="button" className="ask-head-btn" onClick={reset} title="清空对话">新对话</button>}
              <button type="button" className="ask-icon" onClick={closePanel} aria-label="关闭" title="关闭（Esc）" aria-keyshortcuts="Escape">
                <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
              </button>
            </div>
          </header>

          {/* 临时问答 → 研究会话：有研究上下文时常驻一条升级条；服务端建议升级时变成建议卡 */}
          {researchCtx && (
            <div className={`ask-upgrade${suggestion ? ' suggest' : ''}${upgradeDone || upgradedPid ? ' done' : ''}`} role="note">
              {suggestion ? (
                <>
                  <span className="ask-suggest-mark" aria-hidden>✦</span>
                  <span className="ask-upgrade-text ask-suggest-copy" title={suggestion.reason || undefined}>
                    <b className="ask-suggest-title-full">{BOT_VOICE.suggestTitle}</b>
                    <b className="ask-suggest-title-short">{BOT_VOICE.suggestTitleShort}</b>
                    {suggestion.reason && <small>{suggestion.reason}</small>}
                  </span>
                  <button type="button" className="ask-upgrade-btn" disabled={upgradeBusy} onClick={upgradeToSession} title={BOT_VOICE.upgradeHint}>
                    {upgradeBusy ? BOT_VOICE.upgrading : BOT_VOICE.upgrade}
                  </button>
                  <button type="button" className="ask-suggest-x" aria-label="忽略这条建议" title="忽略" onClick={() => setSuggestDismissed((d) => [...d, lastAssistantIndex])}>×</button>
                </>
              ) : (
                <>
                  <span className="ask-upgrade-text">
                    {upgradedPid ? BOT_VOICE.upgraded : canUpgrade ? BOT_VOICE.upgradeBanner : BOT_VOICE.ephemeralHint}
                  </span>
                  {upgradedPid ? (
                    <a className="ask-upgrade-btn" href={`#/lab/${upgradedPid}`} onClick={closePanel}>{BOT_VOICE.upgraded} · 打开 →</a>
                  ) : (
                    <button type="button" className="ask-upgrade-btn" disabled={!canUpgrade || upgradeBusy} onClick={upgradeToSession} title={BOT_VOICE.upgradeHint}>
                      {upgradeBusy ? BOT_VOICE.upgrading : BOT_VOICE.upgrade}
                    </button>
                  )}
                </>
              )}
            </div>
          )}
          {upgradeError && <div className="ask-upgrade-err" role="alert">✕ {upgradeError}</div>}

          <div className="ask-feed" ref={feed}>
            {msgs.length === 0 && (
              <div className="ask-empty">
                <QuantBot size={34} state={running ? 'thinking' : 'idle'} motion="low" className="ask-empty-bot" />
                <span>{researchCtx ? '关于这次研究，想问什么？' : '关于这个页面，想问什么？'}</span>
                <div className="ask-starters stagger">
                  {(STARTERS[route] ?? STARTERS.default).map((q) => (
                    <button key={q} type="button" className="chip" onClick={() => setInput(q)}>{q}</button>
                  ))}
                </div>
              </div>
            )}
            {msgs.map((m, i) => m.role === 'user' ? (
              <div key={i}>
                <UserBubble>{m.text}</UserBubble>
                {m.attached.length > 0 && <div className="ask-sent-ctx">附带：{m.attached.join('、')}</div>}
              </div>
            ) : (
              <div key={i}>
                {m.streaming && !m.content && !m.thinking ? (
                  <Working phase="正在思考" onStop={stop} />
                ) : (
                  <AssistantText content={m.content} thinking={m.thinking} thinkingMs={m.thinkingMs} streaming={m.streaming}
                    model={m.model} fallback={m.fallback} />
                )}
                {m.tools && m.tools.length > 0 && <ToolTrail tools={m.tools} />}
                {m.stopped && <EventLine tone="warn">已停止</EventLine>}
                {m.error && <EventLine tone="error">{m.error}</EventLine>}
              </div>
            ))}
          </div>

          {showTools && catalog && (
            <div className="ask-tools" role="dialog" aria-label="平台工具">
              <div className="ask-tools-head"><b>平台工具</b><span>勾选的场景会在提问时加载；标「需要你确认」的操作会先给你一张确认卡片</span>
                <button type="button" className="ask-icon" onClick={() => setShowTools(false)} aria-label="关闭">×</button></div>
              {catalog.scenes.map((sc) => (
                <section key={sc.id}>
                  <h5><label><input type="checkbox" checked={scenes.includes(sc.id)} onChange={() => toggleScene(sc.id)} /> {sc.label}</label></h5>
                  <ul>{catalog.tools.filter((t) => t.scene === sc.id).map((t) => (
                    <li key={t.name}><b>{t.title}</b>{t.risk === 'confirm' && <span className="ask-risk">需要你确认</span>}
                      <span>{t.description}</span><small>可用时机：{t.when}</small></li>))}</ul>
                </section>
              ))}
            </div>
          )}

          <div className="ask-foot">
            <Composer value={input} onChange={setInput} onSend={send} onStop={stop} running={running} autoFocus
              placeholder={researchCtx ? '就这次研究提问…' : attached.length ? '就当前页面提问…' : '问点什么…'}
              above={(researchCtx || legacyAttached.length > 0) ? <>
                {researchCtx && (
                  <ResearchContextStrip research={researchCtx} attached={attachedOutputs}
                    hasCode={!!researchEntry?.ctx.code?.trim()} onToggle={toggleOutput} onReference={referenceNode} />
                )}
                {legacyAttached.map((c) => (
                  <div key={c.title} className="cmp-attached ask-attached">
                    <span title={chipLabel(c)}>已附带：{chipLabel(c)}</span>
                    <button type="button" aria-label="移除附带内容" onClick={() => setDismissed((d) => [...d, c.title])}>×</button>
                  </div>
                ))}
              </> : undefined}
              left={<PlusMenu items={[
                ...(catalog?.scenes ?? []).map((sc) => ({
                  label: sc.label, hint: sc.hint, section: '让 AI 可以用的平台工具', keepOpen: true, checked: scenes.includes(sc.id), onClick: () => toggleScene(sc.id),
                })),
                ...(prefs.scenes ? [{ label: '恢复按页面自动选择', section: '设置', hint: pageScenes.length ? `这个页面默认：${pageScenes.map((x) => catalog?.scenes.find((c) => c.id === x)?.label ?? x).join('、')}` : '这个页面默认不加载工具', onClick: () => { const p = { ...prefs, scenes: null }; setPrefs(p); writePrefs(p) } }] : []),
                { label: '查看全部工具说明', section: '设置', hint: '用途、何时可用、风险级别', onClick: () => setShowTools(true) },
              ]} />}
              right={<>
                <ContextRing context={context} />
                <ModelMenu models={meta?.models ?? []} value={prefs.model} defaultModel={meta?.default_model}
                  effort={prefs.effort} efforts={meta?.efforts}
                  onModel={(model) => { const p = { ...prefs, model }; setPrefs(p); writePrefs(p) }}
                  onEffort={(effort) => { const p = { ...prefs, effort }; setPrefs(p); writePrefs(p) }} />
              </>} />
          </div>
        </aside>
  )

  if (embedded) {
    return (
      <>
        {entryHost ? createPortal(entry, entryHost) : null}
        {panel}
      </>
    )
  }

  return (
    <>
      {!open && entry}
      {open && panel}
    </>
  )
}

/** 工具调用记录：只读 / 新建记录直接显示结果；需要确认的显示确认卡片。 */
function ToolTrail({ tools }: { tools: ToolRecord[] }) {
  return (
    <div className="ask-trail">
      {tools.map((t, i) => t.status === 'pending' || t.action_id ? <ConfirmCard key={i} rec={t} /> : (
        <div key={i}>
        <div className={`ask-tool ${t.status}`} title={t.preview}>
          <span className="mark">{t.status === 'error' ? '✕' : '✓'}</span>{t.title}
          {t.preview && <span className="pv">{t.preview}</span>}
        </div>
        <WorkflowTrace value={t.workflow_view} />
        {t.artifact_view && <ArtifactPreview initial={t.artifact_view} />}{t.task_view&&<TaskResult value={t.task_view}/>}
        </div>
      ))}
    </div>
  )
}

function ConfirmCard({ rec }: { rec: ToolRecord }) {
  const [act, setAct] = useState<AiAction | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const status = act?.status ?? rec.status
  const decide = async (approve: boolean) => {
    if (!rec.action_id) return
    setBusy(true); setErr('')
    try { setAct((await askAI.decide(rec.action_id, approve)).action) } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  const result = act?.result as { error?: string } | undefined
  return (
    <div className={`ask-confirm ${status}`}>
      <div className="ask-confirm-title"><span className="ask-risk">需要你确认</span>{rec.title}</div>
      <div className="ask-confirm-body">{rec.summary}</div>
      {status === 'pending' ? (
        <div className="ask-confirm-actions">
          <button type="button" className="button small primary" disabled={busy} onClick={() => decide(true)}>确认执行</button>
          <button type="button" className="button small" disabled={busy} onClick={() => decide(false)}>取消</button>
        </div>
      ) : (
        <div className="ask-confirm-done">{status === 'done' ? '✓ 已执行' : status === 'canceled' ? '已取消' : `✕ 没有成功：${result?.error ?? ''}`}</div>
      )}
      {err && <div className="ask-confirm-done">✕ {err}</div>}
    </div>
  )
}

export default AskAIPanel
