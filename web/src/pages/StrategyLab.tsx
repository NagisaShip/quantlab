import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  lab, mergeBySeq, missions,
  type ContextView, type Effort, type Executor, type FactorSummary, type GoalSpec, type LabKind, type LabMessage, type LabMeta,
  type LabProject, type LabProjectBrief, type Metrics, type Permission,
} from '../apiLab'
import { Avatar } from '../components/Avatar'
import { store } from '../components/prefs'
import { clearLabDraft, peekLabDraft, type LabDraft } from '../components/handoff'
import { AssistantText, EventLine, ToolLine, UserBubble, Working } from '../components/chat/Blocks'
import { Composer, ContextRing, ModelMenu, usePopover, type SlashCommand } from '../components/chat/Composer'
import { ActionMenu, AutoIterateRow, ContextStats, Icons, PermissionMenu, useFilePicker, type ActionItem } from '../components/chat/LabControls'
import { CodeEditor } from '../components/CodeEditor'
import { DiffView, lineDiff } from '../components/Diff'
import { Markdown } from '../components/markdown'
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
import { domainUI, libraryIdOf } from './lab/domains'
import { Conversations } from './lab/Conversations'
import { QuantBot } from '../components/QuantBot'
import { DiscussionCard } from '../components/chat/Discussion'
import { rd } from '../apiRd'
import type { PageProps } from './types'

const EXAMPLES = [
  'BTC 1 小时，20 和 60 均线金叉做多，止损 5%',
  'ETH 4 小时 RSI 均值回归，回撤控制在 15% 以内',
  'BTC、ETH、SOL 每周动量轮动，只持有最强的一个',
]

const SYMBOL_PICKS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT', 'LINK/USDT', 'TON/USDT']
/** 设置 · 模型 里的「AI 生成简短建议」开关（本机偏好，默认开） */
export const suggestOn = () => store.get('qa.lab.suggest', '1') === '1'

const PERMISSION_ALIAS: Record<string, Permission> = {
  auto: 'auto', manual: 'manual', edits: 'accept_edits', accept: 'accept_edits', accept_edits: 'accept_edits', plan: 'plan', bypass: 'bypass',
}

const LEGACY_METRIC: Record<string, string> = { min_sharpe: 'sharpe', max_drawdown: 'max_drawdown', min_trades: 'trades', min_excess: 'excess_return' }

const SOURCE_LABEL: Record<string, string> = { ai: 'AI', fallback: '规则', user: '手动', template: '模板' }
const TOOL_ICON: Record<string, string> = {
  run_backtest: '∿', get_market_summary: '⇄', search_templates: '⌕', write_strategy: '✎', convene_team: '⛬',
  generate: '✦', revise: '↻', explain: '?', save_strategy: '⇩', ask_user: '?',
  search_platform: '⌕', web_search: '⊕', fetch_page: '↗', set_goal: '◎', check_goals: '◎', walk_forward: '⫶',
  write_factor: 'ƒ', evaluate_factor: 'ρ', search_factor_library: '⌕', list_fields: '☰', list_versions: '☰',
  set_factor_goal: '◎', propose_factor: '✦', save_factor: '⇩',
  write_model_spec: '◇', train_model: '∿', search_model_library: '⌕', list_features: '☰', set_model_goal: '◎', save_model: '⇩',
}

type PanelTab = 'code' | 'versions' | 'context' | 'goals'

const pct = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`)
const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '—' : v.toFixed(d))

export default function StrategyLabPage({ onNavigate, notify, arg }: PageProps & { arg?: string }) {
  const [meta, setMeta] = useState<LabMeta | null>(null)
  const [projects, setProjects] = useState<LabProjectBrief[]>([])
  const [activeId, setActiveId] = useState<string>(arg || '')
  const [newFolder, setNewFolder] = useState('')
  const [slot, setSlot] = useState<HTMLElement | null>(null)
  useEffect(() => { setSlot(document.getElementById('sidebar-slot')) }, [])

  const refreshList = useCallback(async () => {
    const r = await lab.projects()
    setProjects(r.projects)
  }, [])

  useEffect(() => {
    lab.meta().then(setMeta).catch((e: Error) => notify(e.message, true))
    refreshList().catch(() => undefined)
  }, [refreshList, notify])

  useEffect(() => { if (arg) setActiveId(arg) }, [arg])

  const open = (id: string) => {
    setActiveId(id)
    window.history.replaceState(null, '', id ? `#/lab/${id}` : '#/lab')
  }

  return (
    <div className="lab2 rail-closed">
      {slot && createPortal(
        <Conversations projects={projects} activeId={activeId} notify={notify} onChanged={refreshList}
          onOpen={open} onNew={(folder) => { setNewFolder(folder ?? ''); open('') }} />,
        slot,
      )}
      {meta && !meta.llm_ready && slot && createPortal(<a href="#/models" className="conv-foot">未配置模型，当前为规则降级 · 去配置</a>, slot)}

      {activeId ? (
        <ProjectView key={activeId} id={activeId} meta={meta} notify={notify} onNavigate={onNavigate}
          onChanged={refreshList} onDeleted={() => { open(''); refreshList() }} onOpenProject={(pid) => { refreshList(); open(pid) }} />
      ) : (
        <Welcome key={newFolder} meta={meta} notify={notify} folder={newFolder}
          onCreated={(id) => { setNewFolder(''); refreshList(); open(id) }} />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 新对话                                                              */
/* ------------------------------------------------------------------ */
function Welcome({ meta, notify, onCreated, folder }: {
  meta: LabMeta | null; notify: PageProps['notify']; onCreated: (id: string) => void; folder?: string
}) {
  const [draft, setDraft] = useState<LabDraft | null>(peekLabDraft)   // 从别的页面带过来的想法 / 代码
  const [kind, setKindState] = useState<LabKind>(() => draft?.kind ?? (store.get('qa.lab.kind', 'strategy') as LabKind))
  const setKind = (k: LabKind) => { setKindState(k); store.set('qa.lab.kind', k) }
  const ui = domainUI(kind)
  const isFactor = !!ui            // 多标的研究（因子 / 模型 / …）
  const dmeta = domainMeta(meta, kind)
  const defaults = useDefaultSpec(meta, kind)
  const [executor, setExecutor] = useState<Executor>(() => store.get('qa.lab.executor', 'solo') as Executor)
  const [universe, setUniverse] = useState('major10')
  const [idea, setIdea] = useState(() => draft?.idea ?? '')
  const [spec, setSpec] = useState<GoalSpec>(defaults)
  const [model, setModel] = useState(() => store.get('qa.lab.model', ''))
  const [effort, setEffort] = useState<Effort>(() => store.get('qa.lab.effort', 'medium') as Effort)
  const [research, setResearch] = useState<'none' | 'market' | 'team'>('market')
  const [source, setSource] = useState<'auto' | 'exchange' | 'synthetic'>('auto')
  const [permission, setPermissionState] = useState<Permission>(() => store.get('qa.lab.permission', 'auto') as Permission)
  const setPermission = (p: Permission) => { setPermissionState(p); store.set('qa.lab.permission', p) }
  // 标的：默认交给 AI 根据对话决定（带了标的过来、或手动选过时用手动）
  const [marketAuto, setMarketAuto] = useState(() => !draft?.securities?.length)
  const [files, setFiles] = useState<{ title: string; text: string }[]>([])
  const [auto, setAuto] = useState(false)
  const [busy, setBusy] = useState(false)
  const [goalOpen, setGoalOpen] = useState<{ initial: GoalSpec; unparsed?: string } | null>(null)
  const [goalSet, setGoalSet] = useState(false)   // 用 /goal 设过目标才能自动迭代；没有目标就是普通对话
  const market = usePopover()
  const metrics = metricsOf(dmeta)

  useEffect(() => {
    setSpec(draft?.securities?.length || draft?.timeframe
      ? { ...defaults, symbols: draft.securities?.length ? draft.securities : defaults.symbols, timeframe: draft.timeframe || defaults.timeframe }
      : defaults)
  }, [defaults, draft])
  useEffect(() => { clearLabDraft() }, [])

  const queueNight = async (text: string) => {
    setBusy(true)
    try {
      await rd.addTask({ kind, idea: text, executor: isFactor ? executor : 'solo', symbols: spec.symbols, timeframe: spec.timeframe, rounds: Math.min(8, spec.max_rounds), data_source: source })
      notify('已加入夜班队列，开班后自动研究，结果在晨报里')
      setIdea('')
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }

  const onFiles = useCallback(async (list: File[]) => {
    const texts = list.filter((f) => !f.type.startsWith('image/') && f.size <= 2_000_000)
    const read = await Promise.all(texts.map(async (f) => ({ title: f.name.slice(0, 60), text: (await f.text()).slice(0, 4000) })))
    if (read.length) { setFiles((prev) => [...prev, ...read]); notify(`已附带 ${read.length} 个文件，会钉进新对话的上下文`) }
    if (list.some((f) => f.type.startsWith('image/'))) notify('图片请在对话创建后再发送', true)
  }, [notify])
  const pickFiles = useFilePicker(onFiles)

  const create = async () => {
    const goal = spec
    const startAuto = auto && goalSet
    const text = idea.trim()
    if (busy || !text) return
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
    if (text.startsWith('/night')) {
      const rest = text.replace(/^\/night\s*/, '').trim()
      if (!rest) { notify('用法：/night 你的研究想法', true); return }
      await queueNight(rest)
      return
    }
    setBusy(true)
    try {
      const pins = [...(draft?.pins ?? []), ...files.map((f) => ({ kind: 'file', ...f }))]
      const v = await lab.create({
        kind, idea: text, securities: goal.symbols, timeframe: goal.timeframe, model, effort, max_rounds: goal.max_rounds,
        bars_limit: goal.bars_limit, data_source: source, permission, market_auto: marketAuto && !goalSet, suggest: suggestOn(),
        ...(isFactor ? { horizon: goal.horizon ?? 1, executor, universe } : {}),
        ...(draft?.title ? { title: draft.title } : {}), ...(draft?.code ? { code: draft.code } : {}), ...(pins.length ? { pins } : {}),
        ...(folder ? { folder } : {}),
      })
      const id = v.project.id
      if (goalSet) await lab.setGoal(id, goal, { review: false, start: startAuto, research: isFactor ? 'none' : research })
      if (!startAuto) await lab.send(id, text, { suggest: suggestOn() })
      onCreated(id)
    } catch (e) {
      notify((e as Error).message, true)
      setBusy(false)
    }
  }

  const welcomeCommands: SlashCommand[] = [
    { cmd: '/goal', group: '迭代', arg: '[描述]', help: '设定目标：指标条件、回测范围、轮数（设了目标才能自动迭代）' },
    { cmd: '/night', group: '迭代', arg: '[想法]', help: '放进夜班队列：无人值守地自动研究，结果写进晨报' },
  ]
  const plusItems: ActionItem[] = [
    { label: '添加文件或照片', icon: Icons.attach, hint: '文本、CSV、JSON 会钉进新对话的上下文', onClick: pickFiles },
    { label: '斜杠命令', icon: Icons.slash, hint: '/goal、/night', onClick: () => setIdea('/') },
    {
      label: '插件', icon: Icons.plugin, submenu: [
        ...(isFactor ? [{ label: '研究小组执行', icon: Icons.team, hint: ui?.teamHint ?? '', checked: executor === 'team', onClick: () => { const v = executor === 'team' ? 'solo' : 'team'; setExecutor(v); store.set('qa.lab.executor', v) } }] : []),
        { label: '夜班', icon: Icons.loop, hint: '/night：把这个想法放进夜班队列', onClick: () => setIdea(`/night ${idea.replace(/^\/\S*\s*/, '')}`) },
        { label: '联网搜索与模型…', icon: Icons.search, hint: '在设置里配置', onClick: () => { window.location.hash = '#/models' } },
      ],
    },
    { label: '自动迭代', divider: true, render: () => (
      <AutoIterateRow rounds={spec.max_rounds} needsGoal={!goalSet} onSetGoal={() => setGoalOpen({ initial: spec })}
        onRounds={(n) => setSpec({ ...spec, max_rounds: n })} onStart={(n) => { setSpec({ ...spec, max_rounds: n }); setAuto(true) }} />
    ) },
  ]
  const kinds = meta?.kinds ?? [{ id: 'strategy' as LabKind, label: '策略', hint: '' }, { id: 'factor' as LabKind, label: '因子', hint: '' }]
  const marketLabel = marketAuto ? '标的：自动' : `${isFactor ? `${spec.symbols.length} 个标的` : spec.symbols[0] || '标的'} · ${spec.timeframe}${isFactor ? ` · 持有 ${spec.horizon ?? 1} 期` : ''}`

  return (
    <section className="lab2-main">
      <header className="lab2-head">{folder && <span className="lab2-sub">新对话将放进项目「{folder}」</span>}</header>
      <div className="lab2-welcome">
        <QuantBot size={52} state={busy ? 'thinking' : 'idle'} className="lab2-welcome-bot" />
        <h1>{ui ? ui.welcome : '有什么交易想法？'}</h1>
        {goalOpen && (
          <div className="lab2-goal-pop">
            <GoalForm meta={dmeta} domain={kind} initial={goalOpen.initial} unparsed={goalOpen.unparsed} onCancel={() => setGoalOpen(null)}
              submitting={busy} startLabel="保存并开启自动迭代"
              onSubmit={(g, start) => { setSpec(g); setGoalSet(true); setMarketAuto(false); setGoalOpen(null); if (start) setAuto(true) }} />
          </div>
        )}
        <Composer value={idea} onChange={setIdea} onSend={() => create()} disabled={busy} autoFocus commands={welcomeCommands}
          placeholder={ui ? ui.placeholder : '描述一个想法，例如：BTC 4 小时突破 55 根新高做多，ATR 止损'}
          sendLabel={auto && goalSet ? '开始自动迭代' : '发送'}
          above={(draft?.attached || files.length || goalSet || (auto && goalSet)) ? <>
            {draft?.attached && (
              <div className="cmp-attached">
                <span>已附带：{draft.attached}</span>
                <button type="button" aria-label="移除附带内容" onClick={() => setDraft(null)}>×</button>
              </div>
            )}
            {files.map((f, i) => (
              <div key={i} className="cmp-attached">
                <span>📎 {f.title}</span>
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
          </> : undefined}
          left={<>
            <ActionMenu items={plusItems} />
            <PermissionMenu value={permission} onChange={setPermission} />
          </>}
          right={<>
            <div className="cmp-pop-anchor" ref={market.ref}>
              <button type="button" className={`cmp-chip${marketAuto ? '' : ' on'}`} onClick={() => market.setOpen(!market.open)} aria-expanded={market.open}
                title={marketAuto ? '由 AI 根据对话决定标的与周期；点开可以手动选' : '研究类型、标的与周期'}>
                {ui ? `${ui.noun} · ` : ''}{marketLabel}
              </button>
              {market.open && (
                <div className="cmp-menu cmp-menu-form cmp-menu-right market-menu">
                  <div className="cmp-menu-label">研究类型</div>
                  <div className="cmp-seg" role="group" aria-label="研究类型" style={{ gridTemplateColumns: `repeat(${kinds.length}, 1fr)` }}>
                    {kinds.map((k) => (
                      <button key={k.id} type="button" className={kind === k.id ? 'on' : ''} aria-pressed={kind === k.id} title={k.hint} onClick={() => setKind(k.id)}>{k.label}</button>
                    ))}
                  </div>
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
                          {['5m', '15m', '1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}
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
                      <option value="auto">自动（真实优先）</option><option value="exchange">只用真实行情</option><option value="synthetic">合成数据</option>
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
        <div className="lab2-examples">
          {(ui ? ui.examples : EXAMPLES).map((x) => <button key={x} className="lab2-example" onClick={() => setIdea(x)}>{x}</button>)}
        </div>
      </div>
    </section>
  )
}

/* ------------------------------------------------------------------ */
/* 项目对话                                                            */
/* ------------------------------------------------------------------ */
function ProjectView({ id, meta, notify, onNavigate, onChanged, onDeleted, onOpenProject }: {
  id: string; meta: LabMeta | null; notify: PageProps['notify']; onNavigate: PageProps['onNavigate']
  onChanged: () => void; onDeleted: () => void; onOpenProject: (id: string) => void
}) {
  const [project, setProject] = useState<LabProject | null>(null)
  const [messages, setMessages] = useState<LabMessage[]>([])
  const [context, setContext] = useState<ContextView | null>(null)
  const [input, setInput] = useState('')
  const [panel, setPanel] = useState<PanelTab | null>(null)
  const [compacting, setCompacting] = useState(false)
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
  const [rounds, setRounds] = useState<number | null>(null)
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const cursor = useRef({ after: 0, since: 0 })
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const following = useRef<AbortController | null>(null)
  const panelInit = useRef(false)

  const apply = useCallback((v: { project: LabProject; messages: LabMessage[]; server_time: number; context?: ContextView }, full = false) => {
    setProject(v.project)
    if (v.context) { setContext(v.context); setCtxAt(Date.now()) }
    setMessages((prev) => (full ? v.messages : mergeBySeq(prev, v.messages)))
    const maxSeq = v.messages.reduce((a, m) => Math.max(a, m.seq), full ? 0 : cursor.current.after)
    cursor.current = { after: maxSeq, since: v.server_time }
  }, [])

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

  const openPanel = (t: PanelTab | null) => { setPanel(t); store.set('qa.lab.panel', t ?? 'none') }
  const openEditor = () => setEditorOpen(true)

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn()
      await pull()
      follow()
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const send = async (text?: string) => {
    const t = (text ?? input).trim()
    const imgs = text === undefined ? attachments : []
    if ((!t && !imgs.length) || !project) return
    const [head, ...restParts] = t.split(/\s+/)
    const local = text === undefined ? commands.find((c) => c.cmd === head && c.run) : undefined
    if (local) {
      setInput('')
      if (local.disabled) { notify(`${local.cmd} 现在不可用`, true); return }
      local.run!(restParts.join(' ').trim())
      return
    }
    if (/^\/goal\b/.test(t)) {
      setInput('')
      const rest = t.replace(/^\/goal\s*/, '')
      if (!rest) { setGoalOpen({ initial: projectSpec(project) }); return }
      try {
        const r = await lab.goalParse(rest, id)
        setGoalOpen({ initial: r.spec, unparsed: r.unparsed })
      } catch (e) { notify((e as Error).message, true) }
      return
    }
    setInput('')
    stick.current = true
    if (!imgs.length) { await act(() => lab.send(id, t, { suggest: suggestOn() })); return }
    setAttachments([])
    try {
      await lab.send(id, t, { suggest: suggestOn(), images: imgs.map((a) => ({ data_url: a.url, name: a.name })) })
      await pull()
      follow()
    } catch (e) {
      setAttachments(imgs) // 发送失败：附件留在输入框里，可以换模型后重发
      setInput(t)
      notify((e as Error).message, true)
    }
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
        await lab.pin(id, { kind: 'file', title: f.name.slice(0, 60), text: text.slice(0, 4000) })
        notify(text.length > 4000 ? `已钉住 ${f.name}（只保留前 4000 字）` : `已钉住 ${f.name}`)
      } catch (e) { notify((e as Error).message, true) }
    }
    if (images.length) await onImages(images)
    if (texts.length) pull().catch(() => undefined)
  }, [id, notify, pull, onImages])
  const pickFiles = useFilePicker(onFiles)

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const panelSpec = useMemo(() => (project ? projectSpec(project) : null), [project?.updated_at, project?.id])

  if (!project || !panelSpec) return <section className="lab2-main"><Loading label="正在打开项目" /></section>

  const running = project.status === 'running'
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
  const startAuto = (n: number) => !hasGoal ? setGoalOpen({ initial: projectSpec(project) }) : act(async () => {
    if (n !== project.max_rounds) await lab.update(id, { max_rounds: n })
    await lab.autopilot(id, { rounds: n })
  })
  const setPermission = (p: Permission) => { patch({ permission: p }); notify(`权限模式：${({ auto: '自动', manual: '手动', accept_edits: '接受编辑', plan: '计划', bypass: '跳过权限确认' } as Record<Permission, string>)[p]}`) }
  const noCode = !project.code
  const startDiscussion = (topic: string) => act(async () => {
    const directive = (topic || `评审「${project.title}」：${project.idea || '当前研究'}；逻辑是否自洽、证据是否可信、最大风险在哪里、下一步怎么做`).slice(0, 600)
    const presets = await missions.presets()
    const teamId = project.team_id || presets.teams[0]?.id || null
    const v = await missions.create({
      directive, mode: 'single', team_id: teamId, lab_project_id: id,
      symbol: project.securities[0], timeframe: project.timeframe, include_market_data: true,
    })
    await lab.discussion(id, v.mission.id, directive)
    stick.current = true
  })
  const queueNight = (idea: string) => act(async () => {
    await rd.addTask({
      kind: (project.kind ?? 'strategy') as 'strategy', idea: (idea || project.idea || project.title).slice(0, 2000),
      executor: project.executor ?? 'solo', symbols: project.securities, timeframe: project.timeframe,
      rounds: Math.min(8, project.max_rounds), data_source: project.data_source,
    })
    notify('已加入夜班队列：开班后无人值守地研究，结果写进晨报')
  })
  const evalLabel = ui ? `${ui.evaluate}当前${ui.noun}` : '回测当前版本'
  const commands: SlashCommand[] = [
    { cmd: ui ? '/eval' : '/backtest', group: '运行', help: evalLabel, run: () => act(() => lab.backtest(id)), disabled: running || noCode },
    ...(!ui || project.kind === 'factor' ? [{ cmd: '/stress', group: '运行', help: ui ? '2× 成本评估（只看，不改成绩）' : '2× 成本压力测试', run: () => act(() => lab.backtest(id, 2)), disabled: running || noCode }] : []),
    ...(!ui ? [{ cmd: '/walkforward', group: '运行', arg: '[段数]', help: '分段回测：检查是否只在某段有效', run: (a: string) => act(() => lab.walkForward(id, Math.min(6, Math.max(2, Number(a) || 3)))), disabled: running || noCode }] : []),
    { cmd: '/auto', group: '迭代', arg: '[轮数]', help: hasGoal ? `按目标自动迭代（现在 ${roundsNow} 轮）` : '需要先设定目标（/goal）；没有目标就直接对话', run: (a: string) => startAuto(Math.min(8, Math.max(1, Number(a) || roundsNow))), disabled: running || !hasGoal },
    { cmd: '/goal', group: '迭代', arg: '[描述]', help: '设定目标：指标条件、回测范围、轮数' },
    { cmd: '/night', group: '迭代', arg: '[想法]', help: '放进夜班队列：无人值守地自动研究，结果写进晨报', run: (a: string) => queueNight(a) },
    { cmd: '/explain', group: '协作', help: ui ? `解释当前${ui.noun}的逻辑与风险` : '逐段解释当前策略', run: () => send('/explain'), disabled: running || noCode },
    { cmd: '/team', group: '协作', arg: '[议题]', help: ui ? '召集研究小组评审' : '召集团队评审', run: (a: string) => send(a ? `/team ${a}` : '/team'), disabled: running },
    { cmd: '/discuss', group: '协作', arg: '[议题]', help: '团队讨论：多个角色依次发言、相互质疑，最后汇总（发言以气泡显示在对话里）', run: (a: string) => startDiscussion(a), disabled: running },
    ...(ui ? [{ cmd: '/crew', group: '协作', help: project.executor === 'team' ? '自动迭代改为单个 AI 执行' : '自动迭代改为研究小组执行', run: () => patch({ executor: project.executor === 'team' ? 'solo' : 'team' }), disabled: running }] : []),
    { cmd: '/pin', group: '上下文', arg: '[内容]', help: '钉一段材料到上下文', run: (a: string) => (a ? act(() => lab.pin(id, { kind: 'note', title: a.slice(0, 24), text: a })) : openPanel('context')) },
    { cmd: '/compact', group: '上下文', help: '把较早的对话压缩成摘要', run: () => compact(), disabled: running || compacting },
    { cmd: '/context', group: '上下文', help: '查看上下文用量', run: () => openPanel('context') },
    { cmd: '/save', group: '项目', help: ui ? `存入${ui.libraryName}` : (project.slug ? '更新平台策略' : '保存为平台策略'), run: () => saveOut(), disabled: running || noCode },
    { cmd: '/mode', group: '项目', arg: '[auto|manual|edits|plan|bypass]', help: '切换权限模式', run: (a: string) => (PERMISSION_ALIAS[a.toLowerCase()] ? setPermission(PERMISSION_ALIAS[a.toLowerCase()]) : notify('用法：/mode auto | manual | edits | plan | bypass', true)) },
  ]
  const plusItems: ActionItem[] = [
    { label: '添加文件或照片', icon: Icons.attach, hint: imageBlock ? '文本、CSV、JSON 会钉进上下文（当前模型不收图片）' : '文本钉进上下文，图片随下一条消息发给模型', onClick: pickFiles },
    { label: '斜杠命令', icon: Icons.slash, hint: '/backtest、/auto、/team…', onClick: () => setInput('/') },
    {
      label: '插件', icon: Icons.plugin, submenu: [
        ...(ui ? [{ label: '研究小组执行', icon: Icons.team, hint: '自动迭代时由小组讨论后再实现', checked: project.executor === 'team', onClick: () => patch({ executor: project.executor === 'team' ? 'solo' : 'team' }), disabled: running }] : []),
        { label: '团队讨论', icon: Icons.team, hint: '/discuss：多个角色发言、质疑、汇总', onClick: () => startDiscussion(''), disabled: running },
        { label: '快速评审', icon: Icons.team, hint: '/team：召集团队给一份评审结论', onClick: () => send('/team'), disabled: running },
        { label: '夜班', icon: Icons.loop, hint: '/night：放进夜班队列，结果写进晨报', onClick: () => queueNight('') },
        { label: '钉一段材料', icon: Icons.pin, hint: '/pin', onClick: () => openPanel('context') },
        { label: ui ? `存入${ui.libraryName}` : '保存为平台策略', icon: Icons.save, hint: '/save', onClick: saveOut, disabled: running || noCode },
        { label: '联网搜索与模型…', icon: Icons.search, hint: '在设置里配置', onClick: () => onNavigate('models') },
      ],
    },
    { label: '自动迭代', divider: true, render: () => (
      <AutoIterateRow rounds={roundsNow} disabled={running} needsGoal={!hasGoal} onSetGoal={() => setGoalOpen({ initial: projectSpec(project) })} onRounds={(n) => { setRounds(n); lab.update(id, { max_rounds: n }).catch(() => undefined) }} onStart={startAuto} />
    ) },
  ]
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
    onEdit: (m, text) => { stick.current = true; act(() => lab.rewind(id, m.seq, text)) },
    onRetry: (m) => { stick.current = true; act(() => lab.rewind(id, m.seq)) },
    onFork: forkAt,
    onPin: (m) => act(async () => { await lab.pin(id, { kind: 'note', title: m.content.replace(/\s+/g, ' ').slice(0, 24), text: m.content.slice(0, 4000) }); notify('已钉进上下文') }),
    onCopy: (m) => { navigator.clipboard?.writeText(m.content).then(() => notify('已复制'), () => notify('复制失败', true)) },
  }
  const changes = changeStats(project)
  const lastAsk = [...messages].reverse().find((m) => m.kind === 'ask')
  const streamingNow = messages.some((m) => m.data?.streaming)

  return (
    <>
      <section className="lab2-main">
        <header className="lab2-head">
          <input className="lab2-title" defaultValue={project.title} aria-label="项目名称"
            onBlur={(e) => e.target.value.trim() && e.target.value !== project.title && patch({ title: e.target.value.trim() })} />
          <span className="lab2-sub">
            {isFactor
              ? <>{ui!.noun}研究 · {project.securities.length} 个标的 · {project.timeframe} · 持有 {project.horizon ?? 1} 期 · {project.executor === 'team' ? '研究小组' : '单个 AI'}</>
              : <>{project.securities.join(', ')} · {project.timeframe}</>}
            {project.data_source === 'synthetic' && ' · 合成数据'}
            {project.slug && <> · <a href={`#/editor/${project.slug}`}>已存为策略</a></>}
            {ui && libId && <> · <a href={`#/${ui.libraryRoute(libId)}`}>已存入{ui.libraryName}</a></>}
          </span>
          <span className="lab2-head-gap" />
          <button type="button" className={`lab2-sessbtn${panel ? ' on' : ''}`} aria-pressed={!!panel} onClick={() => openPanel(panel ? null : 'code')}
            title="会话栏：更改、迭代、上下文、目标">
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden><rect x="2" y="2.5" width="12" height="11" rx="2" /><path d="M10 2.5v11" /></svg>
            {changes.add + changes.del > 0 && <span className="lab2-sessbtn-diff"><b className="add">+{changes.add}</b><b className="del">−{changes.del}</b></span>}
          </button>
          <ProjectMenu onDelete={() => window.confirm(ui ? `删除这个${ui.noun}研究？已存入${ui.libraryName}的内容不受影响。` : '删除这个策略项目？已保存的平台策略不受影响。') && lab.remove(id).then(onDeleted)}
            onSave={saveOut} hasCode={!!project.code} saved={!!(project.slug || libId)}
            saveLabel={ui ? `${libId ? '更新' : '存入'}${ui.libraryName}` : undefined} />
        </header>

        <div className="lab2-scroll" ref={scroller}
          onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }}>
          <div className="lab2-thread">
            {messages.map((m) => (
              <MessageView key={m.seq} m={m} project={project} meta={dmeta} active={m === lastAsk && project.status === 'awaiting_user'} actions={msgActions}
                onDecide={(choice, note) => act(() => lab.decide(id, choice, note))}
                onOpenBacktest={(btId) => onNavigate(`backtest/${btId}`)} onOpenEditor={(slug) => onNavigate(`editor/${slug}`)}
                onOpenCode={openEditor} onOpenLibrary={(route) => onNavigate(route)} />
            ))}
            {running && !streamingNow && <Working phase={project.job?.phase} />}
            {!messages.length && !running && <div className="lab2-empty">输入 / 查看快捷指令</div>}
          </div>
        </div>

        <div className="lab2-dock">
          {goalOpen && (
            <div className="lab2-goal-pop">
              <GoalForm meta={dmeta} domain={project.kind} initial={goalOpen.initial} unparsed={goalOpen.unparsed} projectId={id}
                submitting={goalBusy || running} onCancel={() => setGoalOpen(null)} onSubmit={saveGoal} />
            </div>
          )}
          <Composer value={input} onChange={setInput} onSend={() => send()} running={running} onStop={() => act(() => lab.stop(id))}
            commands={commands} placeholder={running ? '正在处理，可随时停止' : ui ? ui.replyPlaceholder : '回复…'}
            hasAttachments={attachments.length > 0}
            onPasteFiles={(files) => { const imgs = files.filter((f) => f.type.startsWith('image/')); if (imgs.length) onImages(imgs) }}
            above={(goalOpen || (running && project.job?.kind === 'autopilot') || attachments.length) ? (<>
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
              <PermissionMenu value={project.permission} onChange={setPermission} disabled={running} />
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
                onUnpin={(pid) => act(() => lab.unpin(id, pid))}
                onPin={(title, text) => act(() => lab.pin(id, { kind: 'note', title, text }))} />}
            </SideCard>
            <SideCard id="goals" focus={panel} title="目标" meta={hasGoal ? `${projectItems(project).length} 个条件` : '未设置'}>
              <GoalSummary project={project} metrics={metricsOf(dmeta)} hasGoal={hasGoal} running={running} rounds={roundsNow}
                onEdit={() => setGoalOpen({ initial: projectSpec(project) })} onStart={() => startAuto(roundsNow)} />
            </SideCard>
          </div>
        </aside>
      )}

      {editorOpen && (
        <Modal title={`${fileNameOf(project.kind)} · v${project.current_v}`} onClose={() => setEditorOpen(false)} width={960}>
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
        <div className="cmp-menu cmp-menu-right" role="menu">
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
}

function MsgBar({ m, items, align }: { m: LabMessage; items: [keyof typeof MI, string, () => void, boolean?][]; align: 'left' | 'right' }) {
  const t = msgTime(m)
  return (
    <div className={`msg-bar ${align}`}>
      {items.map(([icon, label, fn, disabled]) => (
        <button key={label} type="button" className="msg-act" title={label} aria-label={label} disabled={disabled} onClick={fn}>{MI[icon]}</button>
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
        ['edit', '编辑', () => { setText(m.content); setEditing(true) }, actions.running],
        ['retry', '重试', () => actions.onRetry(m), actions.running],
        ['fork', '从此处分叉', () => actions.onFork(m)],
      ]} />
    </div>
  )
}

function MessageView({ m, project, meta, active, actions, onDecide, onOpenBacktest, onOpenEditor, onOpenCode, onOpenLibrary }: {
  m: LabMessage; project: LabProject; meta: LabMeta | null; active: boolean; actions: MessageActions
  onOpenLibrary: (route: string) => void
  onDecide: (choice: string, note: string) => void
  onOpenBacktest: (id: string) => void
  onOpenEditor: (slug: string) => void
  onOpenCode: () => void
}) {
  if (m.role === 'user' && m.kind === 'text') return <UserMessage m={m} actions={actions} projectId={project.id} />
  if (m.role === 'user' && m.kind === 'code') return <UserBubble>手动保存为 v{m.data.v}：{m.content}</UserBubble>
  const d = m.data || {}
  switch (m.kind) {
    case 'text':
      if (m.role === 'event') return <EventLine>{m.content}</EventLine>
      return (
        <div className="msg-wrap ai">
          <AssistantText content={m.content} thinking={d.thinking} thinkingMs={d.thinking_ms} streaming={!!d.streaming}
            model={m.model} fallback={m.mode === 'fallback'} />
          {!d.streaming && m.seq === actions.lastSeq && !actions.running && Array.isArray(d.suggestions) && d.suggestions.length > 0 && (
            <div className="msg-suggest anim-in">
              {(d.suggestions as string[]).map((x) => <button key={x} type="button" className="chip" onClick={() => actions.onSuggest(x)}>{x}</button>)}
            </div>
          )}
          {!d.streaming && m.content && (
            <MsgBar m={m} align="left" items={[
              ['copy', '复制', () => actions.onCopy(m)],
              ['fork', '从这里分叉', () => actions.onFork(m)],
              ['pin', '钉进上下文', () => actions.onPin(m)],
            ]} />
          )}
        </div>
      )
    case 'error':
      return <EventLine tone="error">{m.content}</EventLine>
    case 'tool':
      return <ToolLine icon={TOOL_ICON[d.name]} label={d.label} args={d.args} status={d.status} elapsedMs={d.elapsed_ms}
        detail={d.error || (d.preview ? prettyJson(d.preview) : undefined)} />
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
    case 'round':
      return <RoundCard m={m} metrics={metricsOf(meta)} />
    case 'team':
      return <TeamCard m={m} />
    case 'ask':
      return <AskCard m={m} active={active} onDecide={onDecide} />
    case 'goal':
      return <GoalEvent m={m} metrics={metricsOf(meta)} />
    case 'walkforward':
      return <WalkForwardCard m={m} />
    case 'discussion':
      return <DiscussionCard missionId={d.mission_id} topic={m.content} notify={actions.notify}
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
      {(d.warnings || []).length > 0 && <div className="lab2-faint">{d.warnings.join('；')}</div>}
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

function TeamCard({ m }: { m: LabMessage }) {
  const [open, setOpen] = useState(false)
  const d = m.data
  const steps = (d.steps || []) as { role_id: string; role_name: string; status: string; model: string; output: string }[]
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-avatars">{steps.slice(0, 6).map((s, i) => <Avatar key={i} id={s.role_id} size={20} title={s.role_name} />)}</span>
        <b>{d.team_name} 评审</b>
        <span className="lab2-faint lab2-ellipsis">{d.topic}</span>
        <span className="lab2-card-gap" />
        <button className="link" onClick={() => setOpen(!open)}>{open ? '收起' : `${steps.length} 条发言`}</button>
      </div>
      {open && (
        <div className="lab2-team">
          {steps.map((s, i) => (
            <div key={i} className="lab2-team-row">
              <Avatar id={s.role_id} size={22} />
              <div><b>{s.role_name}</b><p>{s.output}</p></div>
            </div>
          ))}
        </div>
      )}
      {m.content && <div className="lab2-team-sum"><Markdown text={m.content} compact /></div>}
    </div>
  )
}

function AskCard({ m, active, onDecide }: { m: LabMessage; active: boolean; onDecide: (choice: string, note: string) => void }) {
  const [note, setNote] = useState('')
  const answered = m.data.answered
  const approval = m.data.approval as { tool: string; label: string; args: Record<string, unknown> }[] | undefined
  if (approval) {
    return (
      <div className={`lab2-ask approval${active ? ' active' : ''}`}>
        <div className="approval-head">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" aria-hidden><path d="M8 2 3 4v3.6c0 3 2.1 5.4 5 6.4 2.9-1 5-3.4 5-6.4V4Z" /></svg>
          <b>需要你批准</b><span className="lab2-faint">权限模式要求先确认</span>
        </div>
        <ul className="approval-list">
          {approval.map((a, i) => (
            <li key={i}>
              <span>{a.label}</span>
              {typeof a.args.code === 'string' && a.args.code && (
                <details><summary>查看代码</summary><pre className="approval-code">{a.args.code}</pre></details>
              )}
            </li>
          ))}
        </ul>
        <div className="lab2-ask-options">
          {(m.data.options || []).map((o: string) => (
            <button key={o} className={`button small${answered === o || (active && o === '批准') ? ' primary' : ''}`} disabled={!active}
              onClick={() => onDecide(o, note)}>{o}</button>
          ))}
        </div>
        {active && <input className="lab2-ask-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="可选：补充说明，例如“先别回测，改一下止损”" />}
        {answered && <div className="lab2-faint">已选择：{answered}</div>}
      </div>
    )
  }
  return (
    <div className={`lab2-ask${active ? ' active' : ''}`}>
      <Markdown text={m.content} compact />
      <div className="lab2-ask-options">
        {(m.data.options || []).map((o: string) => (
          <button key={o} className={`button small${answered === o ? ' primary' : ''}`} disabled={!active}
            onClick={() => onDecide(o, note)}>{o}</button>
        ))}
      </div>
      {active && (
        <input className="lab2-ask-note" value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="可选：补充要求，例如“回撤再压低一些”" />
      )}
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

function ContextTab({ context, updatedAt, onUnpin, onPin, onCompact, compacting, autoCompact, onAutoCompact }: {
  context: ContextView; updatedAt?: number; onUnpin: (id: string) => void; onPin: (title: string, text: string) => void; onCompact: () => void; compacting: boolean
  autoCompact: boolean; onAutoCompact: (v: boolean) => void
}) {
  const [title, setTitle] = useState('')
  const [text, setText] = useState('')
  const pins = context.items.filter((it) => it.pin_id)
  return (
    <div className="lab2-ctx">
      <ContextStats context={context} updatedAt={updatedAt} />
      <div className="side-toggle">
        <div>
          <b>自动压缩</b>
          <small>{autoCompact
            ? `对话原文超过 ${Math.round((context.history_budget ?? 0) * 1.25 / 1000)}k tokens 时，下一回合前先把较早的对话压成摘要`
            : '已关闭：较早的对话只按条截断，不生成摘要；可随时手动「压缩历史」'}</small>
        </div>
        <button type="button" role="switch" aria-checked={autoCompact} aria-label="自动压缩" className={`switch${autoCompact ? ' on' : ''}`} onClick={() => onAutoCompact(!autoCompact)}><i /></button>
      </div>
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
    <div className="goal-sum">
      <div className="goal-sum-chips">
        {spec.items.map((c, i) => <span key={i} className={`goal-chip${c.priority === 'try' ? ' try' : ''}`}>{condText(c, metrics)}</span>)}
      </div>
      <dl className="goal-sum-dl">
        <div><dt>标的</dt><dd>{spec.symbols.join('、')} · {spec.timeframe}</dd></div>
        <div><dt>区间</dt><dd>{range}</dd></div>
        <div><dt>轮数</dt><dd>最多 {rounds} 轮</dd></div>
        {spec.note ? <div><dt>备注</dt><dd>{spec.note}</dd></div> : null}
      </dl>
      <div className="goal-sum-actions">
        <button type="button" className="button small" onClick={onEdit}>编辑目标</button>
        <button type="button" className="button small primary" disabled={running} onClick={onStart}>开始自动迭代</button>
      </div>
    </div>
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

function WalkForwardCard({ m }: { m: LabMessage }) {
  const d = m.data
  const rows = (d.segments || []) as { segment: number; start: number; end: number; ok: boolean; error?: string; metrics: Metrics }[]
  const day = (ms: number) => new Date(ms).toISOString().slice(0, 10)
  return (
    <div className="lab2-card">
      <div className="lab2-card-head">
        <span className="lab2-ver">v{d.v}</span>
        <b>分段回测</b>
        <span className={`cb-badge ${d.verdict === '稳定' ? 'ok' : 'warn'}`}>{d.verdict}</span>
      </div>
      {d.note && <div className="lab2-faint" style={{ marginTop: 4 }}>{d.note}</div>}
      <table className="lab2-wf">
        <thead><tr><th>区间</th><th>收益</th><th>夏普</th><th>回撤</th><th>交易</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.segment}>
              <td>{day(r.start)} – {day(r.end)}</td>
              {r.ok ? <>
                <td className={(r.metrics.total_return ?? 0) >= 0 ? 'up' : 'down'}>{pct(r.metrics.total_return)}</td>
                <td>{num(r.metrics.sharpe)}</td><td>{pct(r.metrics.max_drawdown)}</td><td>{r.metrics.trades ?? 0}</td>
              </> : <td colSpan={4} className="lab2-faint">{r.error || '失败'}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
