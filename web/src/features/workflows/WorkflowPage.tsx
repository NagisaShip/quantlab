import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { ReactFlow, ReactFlowProvider, Background, Controls, MiniMap, applyNodeChanges, applyEdgeChanges, useReactFlow, type NodeChange, type EdgeChange, type Connection, type Edge } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import './workflows.css'
import { get, post, put, currentWorkspace, targetWorkspace, withWs } from '../../http'
import { AskAIPanel, useAskAIContext } from '../../components/AskAI'
import ResearchContextStrip, { outputKey } from '../../components/ResearchContextStrip'
import type { AskResearchContext, AskResearchOutputRef } from '../../apiAskAI'
import type { PageProps } from '../../pages/types'
import PanelHost, { clampPanelWidth, type RightState, type RightTab } from './PanelHost'
import ExperimentPanel from './ExperimentPanel'
import ResultDock from './ResultDock'
import ResearchNotes from './ResearchNotes'
import ResearchStart from './ResearchStart'
import ArtifactCenter, { type ArtifactRow } from './ArtifactCenter'
import BindingPicker from './BindingPicker'
import { affectedNodes } from './impact'
import { useResearchPlugins, modulePlugin } from './plugins'
import './researchStudio.css'
import NetworkEditor, { type NetworkGraph } from './NetworkEditor'
import ChartEditor from './ChartEditor'
import { readDraft, writeDraft } from './session'
import FunctionCard, { type FunctionNode } from './FunctionCard'
import { compatible, statusName, uid, type Card, type Catalog, type Check, type Definition, type Flow, type PlatformRun, type Report, type Saved } from './types'

const nodeTypes = { function: FunctionCard }
const empty: Flow = { format: 'quantlab.workflow/1', title: '新研究工作流', nodes: [], edges: [], settings: { seed: 7 } }
const stringify = (v: unknown) => JSON.stringify(v, null, 2)
const parseField = (value: string, type?: string): unknown => type === 'number' || type === 'integer' ? Number(value) : type === 'array' || type === 'object' ? JSON.parse(value) : value
// Agent graphs may omit presentation-only positions and edge IDs.
const displayFlow = (flow: Flow): Flow => ({...flow,
  nodes: flow.nodes.map((n,i) => ({...n,params:n.params || {},position:n.position && Number.isFinite(n.position.x) && Number.isFinite(n.position.y) ? n.position : {x:i*280,y:80}})),
  edges:(flow.edges || []).map((e,i)=>({...e,id:e.id || `edge-${i}`})),
})

type DockPref = { open:boolean; height:number; expanded:boolean }
const DOCK_COMPACT = 196
const dockKey = (workflowId:string) => `qa.wf.result.${currentWorkspace()}.${workflowId}`
const dockMax = () => typeof window === 'undefined' ? 700 : Math.max(160, Math.min(700, window.innerHeight - 568))
/**
 * 实际可用高度：路径视口高度 - 画布最小高度 - Dock 头部/分隔条/边框 - 外边距。
 * 不能只用 innerHeight 估算：顶栏、通知条、标题栏和结果头部都会吃掉空间。
 */
function dockBodyMax(): number {
  if (typeof document === 'undefined') return dockMax()
  if (typeof window !== 'undefined' && window.innerWidth <= 1100) return dockMax()
  const path = document.querySelector<HTMLElement>('.wf-path-view')
  const dock = document.querySelector<HTMLElement>('.rs-dock')
  const body = document.querySelector<HTMLElement>('.rs-dock-body')
  const workspace = document.querySelector<HTMLElement>('.wf-workspace')
  if (!path || !dock || !body || !workspace) return dockMax()
  const pathStyle = getComputedStyle(path)
  const dockStyle = getComputedStyle(dock)
  const pathPadding = (parseFloat(pathStyle.paddingTop) || 0) + (parseFloat(pathStyle.paddingBottom) || 0)
  const dockMargin = (parseFloat(dockStyle.marginTop) || 0) + (parseFloat(dockStyle.marginBottom) || 0)
  const dockChrome = Math.max(0, dock.getBoundingClientRect().height - body.getBoundingClientRect().height)
  const canvasMin = parseFloat(getComputedStyle(workspace).minHeight) || 220
  let available = path.clientHeight - pathPadding - canvasMin - dockChrome - dockMargin - 2
  // 页面顶栏/通知条等误差的最直接兜底：当前 Dock 已越过视口时，按越界量回收。
  if (typeof window !== 'undefined' && window.innerWidth > 1100) {
    const overflow = dock.getBoundingClientRect().bottom - window.innerHeight
    if (overflow > 0) available = Math.min(available, body.getBoundingClientRect().height - overflow - 2)
  }
  return Math.max(160, Math.min(700, Math.floor(available)))
}
function readDockPref(workflowId:string): DockPref | null {
  try {
    const raw = JSON.parse(localStorage.getItem(dockKey(workflowId)) || 'null') as Partial<DockPref> | null
    if (!raw || typeof raw !== 'object') return null
    const height = typeof raw.height === 'number' && Number.isFinite(raw.height) ? Math.min(dockMax(), Math.max(160, Math.round(raw.height))) : DOCK_COMPACT
    return { open: raw.open === true, height, expanded: raw.expanded === true }
  } catch {
    return null
  }
}
function writeDockPref(workflowId:string, pref:DockPref) {
  try { localStorage.setItem(dockKey(workflowId), JSON.stringify(pref)) } catch { /* 私密模式 / 配额 */ }
}

/**
 * 工作区布局偏好：按工作区隔离（`qa.ws.layout.<workspace>`），损坏或越界的值回默认。
 * 结果 Dock 仍保留原有的按工作流偏好键（`qa.wf.result.<ws>.<workflow>`），
 * 这里的 dock 只作为「新建/草稿研究」的初始值，保证旧行为与既有测试不回归。
 */
type WorkspaceLayout = {
  mode: 'path' | 'experiments' | 'artifacts'
  panel: { tab: RightTab; state: RightState; width: number; open: boolean }
  libraryOpen: boolean
  dock: { open: boolean; height: number; expanded: boolean }
}
const WS_LAYOUT_MODES = ['path', 'experiments', 'artifacts'] as const
const WS_LAYOUT_TABS: RightTab[] = ['inspector', 'ask', 'context']
const WS_LAYOUT_STATES: RightState[] = ['compact', 'expanded', 'focus']
const wsLayoutKey = () => `qa.ws.layout.${currentWorkspace() || 'default'}`
// 工作流切换会按 arg 重新挂载 WorkflowPage；被用户移除的输出引用属于“本轮会话选择”，
// 放模块级 Map 里保留，避免切回 A 研究时又被重新附加（与 AskAI 的 conversations 同寿命）。
const removedAskByScope: Record<string, string[]> = {}
const isRecord = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const finite = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null
function readWorkspaceLayout(): WorkspaceLayout | null {
  try {
    const raw = JSON.parse(localStorage.getItem(wsLayoutKey()) || 'null') as unknown
    if (!isRecord(raw) || raw.v !== 1) return null
    const panel = isRecord(raw.panel) ? raw.panel : {}
    const dock = isRecord(raw.dock) ? raw.dock : {}
    const mode = typeof raw.mode === 'string' && (WS_LAYOUT_MODES as readonly string[]).includes(raw.mode)
      ? raw.mode as WorkspaceLayout['mode'] : 'path'
    const tab = typeof panel.tab === 'string' && WS_LAYOUT_TABS.includes(panel.tab as RightTab)
      ? panel.tab as RightTab : 'inspector'
    const state = typeof panel.state === 'string' && WS_LAYOUT_STATES.includes(panel.state as RightState)
      ? panel.state as RightState : 'compact'
    const width = finite(panel.width)
    const height = finite(dock.height)
    return {
      mode,
      panel: {
        tab,
        state,
        width: width === null ? 420 : clampPanelWidth(width),
        open: typeof panel.open === 'boolean' ? panel.open : true,
      },
      libraryOpen: typeof raw.libraryOpen === 'boolean' ? raw.libraryOpen : true,
      dock: {
        open: dock.open === true,
        height: height === null ? DOCK_COMPACT : Math.max(160, Math.min(700, Math.round(height))),
        expanded: dock.expanded === true,
      },
    }
  } catch {
    return null
  }
}
function writeWorkspaceLayout(value: WorkspaceLayout) {
  try { localStorage.setItem(wsLayoutKey(), JSON.stringify({ v: 1, ...value })) } catch { /* 私密模式 / 配额 */ }
}

type QuickNav = 'project' | 'recent' | 'favorites'

function Editor({ notify, onNavigate, me, arg = '' }: PageProps & { arg?: string }) {
  const plugins = useResearchPlugins()
  const [booted,setBooted] = useState(false)
  const [bootNonce,setBootNonce] = useState(0)
  const [bootError,setBootError] = useState('')
  const [routeError,setRouteError] = useState('')
  const [showStart,setShowStart] = useState(false)
  // 工作区布局偏好（按工作区隔离）。读取失败/损坏一律回默认，不阻塞渲染。
  const workspaceLayout = useRef<WorkspaceLayout | null>(readWorkspaceLayout())
  const [mode,setMode] = useState<'path'|'experiments'|'artifacts'>(() => workspaceLayout.current?.mode || 'path')
  const [rightTab,setRightTab] = useState<RightTab>(() => workspaceLayout.current?.panel.tab || 'inspector')
  const [rightState,setRightState] = useState<RightState>(() => workspaceLayout.current?.panel.state || 'compact')
  const [rightWidth,setRightWidth] = useState(() => clampPanelWidth(workspaceLayout.current?.panel.width ?? 420))
  const [quickNav,setQuickNav] = useState<QuickNav | null>(null)
  const [dockOpen,setDockOpen] = useState(false)
  const [dockHeight,setDockHeight] = useState(DOCK_COMPACT)
  const [dockExpanded,setDockExpanded] = useState(false)
  const pathDock = dockOpen && mode === 'path'
  const resultLinkRef = useRef<HTMLButtonElement>(null)
  const [followLatest,setFollowLatest]=useState(!arg.includes('run_id='))
  const [remote,setRemote]=useState<Saved|null>(null)
  const [citation,setCitation]=useState<(ArtifactRow&{at:number})|undefined>()
  const [libraryScope,setLibraryScope] = useState('all')
  const [task,setTask] = useState('all')
  const [recent,setRecent] = useState<string[]>(()=>{try{return JSON.parse(localStorage.getItem('qa.wf.recent.'+currentWorkspace())||'[]')}catch{return []}})
  const [favorites,setFavorites] = useState<string[]>(()=>{try{return JSON.parse(localStorage.getItem('qa.wf.favorites.'+currentWorkspace())||'[]')}catch{return []}})
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [assets, setAssets] = useState<{domain:string;id:string;name:string;page:string;mode:string}[]>([])
  const [saved, setSaved] = useState<Saved[]>([])
  const [record, setRecord] = useState<Saved | null>(null)
  const [flow, setFlow] = useState<Flow>(empty)
  const [selected, setSelected] = useState('')
  const [selectedEdge, setSelectedEdge] = useState('')
  const [query, setQuery] = useState('')
  const [level, setLevel] = useState('module')
  const [category, setCategory] = useState('')
  const [visualEditor, setVisualEditor] = useState<'network' | 'chart' | null>(null)
  const [chartSource, setChartSource] = useState('')
  const [groupTitle, setGroupTitle] = useState('我的研究模板')
  const [exposed, setExposed] = useState<string[]>([])
  const initialDraft = useRef(readDraft())
  const askDraftId = useRef('')
  if (!askDraftId.current) askDraftId.current = uid('draft')
  const [seeds, setSeeds] = useState('7,17,27')
  const [batchRuns, setBatchRuns] = useState<PlatformRun[]>([])
  const [view, setView] = useState<'canvas' | 'steps'>('canvas')
  const [tab, setTab] = useState<'params' | 'code' | 'results'>('params')
  const [checks, setChecks] = useState<Check[]>([])
  const [run, setRun] = useState<PlatformRun | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [pins, setPins] = useState<string[]>([])
  const [preview, setPreview] = useState('')
  const [artifactFiles, setArtifactFiles] = useState<{file:string;name:string;sha256:string;bytes?:number}[]>([])
  const [files, setFiles] = useState<{ name: string; bytes: number }[]>([])
  const [busy, setBusy] = useState('')
  const [dirty, setDirty] = useState(false)
  const [groupIds, setGroupIds] = useState<string[]>([])
  const [experiments, setExperiments] = useState<{id:string;status:string;revision:number;origin:string}[]>([])
  const history = useRef<{ past: Flow[]; future: Flow[] }>({ past: [], future: [] })
  const [historyTick, setHistoryTick] = useState(0)
  const canvas = useReactFlow<FunctionNode>()
  const canvasRef = useRef(canvas); canvasRef.current = canvas
  const savedViewport = useRef<{ x:number; y:number; zoom:number } | null>(null)
  const dockLayout = useRef<{ open:boolean; expanded:boolean }>({ open:false, expanded:false })
  const dockDesired = useRef(DOCK_COMPACT)
  const dragSnapshot = useRef<Flow | null>(null)
  const graphImport = useRef<HTMLInputElement>(null)
  const latest = useRef(flow); latest.current = flow
  const [libOpen, setLibOpen] = useState(() => workspaceLayout.current?.libraryOpen ?? !(typeof window !== 'undefined' && window.matchMedia('(max-width: 860px)').matches))
  const [inspOpen, setInspOpen] = useState(() => workspaceLayout.current?.panel.open ?? true)
  const [askEntryHost, setAskEntryHost] = useState<HTMLElement | null>(null)
  const askEntryRef = useCallback((element: HTMLDivElement | null) => setAskEntryHost(element), [])
  // 输出引用选择提升到工作区，让「问 AI」页签与「上下文」页签共享同一份状态。
  const [removedAskOutputs, setRemovedAskOutputs] = useState<Record<string, string[]>>(() => ({ ...removedAskByScope }))
  const [referenceRequest, setReferenceRequest] = useState<{ node: { id: string; title: string }; nonce: number } | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  useEffect(()=>{if(mode==='experiments'&&plugins.some(p=>p.id==='experiments'&&!p.enabled))setMode('path')},[plugins,mode])
  const moreMenu = useRef<HTMLDetailsElement>(null)
  const closeMore = () => { if (moreMenu.current) moreMenu.current.open = false }

  const definitions = useMemo(() => new Map((catalog?.cards || []).map(d => [`${d.kind}@${d.version}`, d])), [catalog])
  const definition = useCallback((n: Card) => definitions.get(`${n.kind}@${n.version}`), [definitions])
  const card = flow.nodes.find(n => n.id === selected)
  const spec = card && definition(card)
  const selectedRun = report?.nodes[selected]
  const researchContext = useMemo<AskResearchContext | null>(() => {
    if (!booted || showStart) return null
    const selectedCard = selected ? flow.nodes.find(n => n.id === selected) : undefined
    const selectedDefinition = selectedCard ? definition(selectedCard) : undefined
    const actual = selected ? report?.nodes?.[selected] : undefined
    const runMatches = !!report && !!run && run.id === report.id
    const current = !!report && !!run && followLatest && runMatches
    const status = runMatches ? run!.state : report?.status || 'not_run'
    return {
      version: 1,
      workspace_id: me?.workspace?.id || currentWorkspace(),
      project_name: me?.workspace?.name || '',
      draft_id: askDraftId.current,
      workflow: {
        id: record?.id || '',
        title: flow.title,
        revision: record?.revision ?? null,
        saved: !!record,
        unsaved: dirty,
      },
      selected_node: selectedCard ? {
        id: selectedCard.id,
        title: selectedCard.title || selectedDefinition?.title || selectedCard.kind,
        kind: selectedCard.kind,
        status: actual?.status || (run && ['QUEUED', 'PENDING', 'RUNNING', 'RECOVERING', 'PAUSED'].includes(run.state) ? 'running' : 'not_run'),
      } : null,
      run: report ? {
        id: report.id,
        mode: report.mode,
        status,
        revision: record?.revision ?? null,
        current,
        historical: !current,
        follows_latest: followLatest,
        view_mode: followLatest ? 'latest' : 'frozen',
      } : null,
      outputs: actual ? Object.entries(actual.outputs || {}).slice(0, 6).map(([port, ref]) => ({
        port, type: ref.type, hash: ref.hash, run_id: report!.id, node_id: selected,
      })) : [],
    }
  }, [booted, showStart, flow, record, selected, report, run, dirty, definition, followLatest, me?.workspace?.id, me?.workspace?.name])
  useAskAIContext({kind:'workflow',title:flow.title,text:'',code:spec?.code,research:researchContext ?? undefined})
  // 工作区右栏：问 AI / 上下文共享同一个 scope 与「随提问发送的输出引用」选择。
  const askScope = useMemo(() => {
    if (!researchContext) return 'global'
    const workspace = researchContext.workspace_id || currentWorkspace() || 'ws'
    const identity = researchContext.workflow.id || researchContext.draft_id || 'draft'
    return `wf:${workspace}:${identity}`
  }, [researchContext])
  const removedAskKeys = removedAskOutputs[askScope] ?? []
  const contextAttached = useMemo(() => (researchContext?.outputs ?? []).filter(ref => !removedAskKeys.includes(outputKey(ref))),
    [researchContext, removedAskKeys])
  const toggleAskOutput = useCallback((scope: string, ref: AskResearchOutputRef) => {
    const key = outputKey(ref)
    setRemovedAskOutputs(previous => {
      const current = previous[scope] ?? []
      const next = current.includes(key) ? current.filter(item => item !== key) : [...current, key]
      removedAskByScope[scope] = next
      return { ...previous, [scope]: next }
    })
  }, [])
  const openAskPanel = useCallback(() => { setMode('path'); setRightTab('ask'); setInspOpen(true) }, [])
  const closeAskPanel = useCallback(() => setRightTab('inspector'), [])
  const toggleInspector = useCallback(() => {
    if (inspOpen && rightState === 'focus') setRightState('expanded')
    setInspOpen(open => !open)
  }, [inspOpen, rightState])
  // 布局持久化：按工作区隔离；写失败/损坏只影响下次默认布局，不阻塞界面。
  const layoutSnapshot = useRef<WorkspaceLayout | null>(null)
  layoutSnapshot.current = {
    mode,
    panel: { tab: rightTab, state: rightState, width: rightWidth, open: inspOpen },
    libraryOpen: libOpen,
    dock: { open: dockOpen, height: dockHeight, expanded: dockExpanded },
  }
  useEffect(() => {
    // 立即写入而不是 debounce：工作流切换会按 arg 重新挂载页面，旧实例的卸载 flush
    // 晚于新实例的首次 render；若延迟写入，新实例会读到上一次的页签/开合状态。
    if (layoutSnapshot.current) writeWorkspaceLayout(layoutSnapshot.current)
  }, [mode, rightTab, rightState, rightWidth, inspOpen, libOpen, dockOpen, dockHeight, dockExpanded])
  useEffect(() => () => { if (layoutSnapshot.current) writeWorkspaceLayout(layoutSnapshot.current) }, [])
  // 上次的工作视图只在首次进入（无 run_id 深链）时恢复；深链固定落在研究路径。
  const restoredMode = useRef(false)
  useEffect(() => {
    if (!booted || restoredMode.current) return
    restoredMode.current = true
    const target = workspaceLayout.current?.mode
    if (!target || target === 'path') return
    if (arg.includes('run_id=') || showStart || routeError) return
    setMode(target)
  }, [booted, arg, showStart, routeError])
  const stale = useMemo(()=>affectedNodes(flow,report),[flow,report])
  async function openRun(id:string){ setFollowLatest(false); const v=await get<{run:PlatformRun;report:Report|null}>(`/api/workflows/runs/${encodeURIComponent(id)}`);setRun(v.run);setReport(v.report);setDockOpen(true);setMode('path') }
  async function latestResult(){setFollowLatest(true);if(record){const activity=await get<{runs:{id:string}[]}>(`/api/workflows/${record.id}/activity`);if(activity.runs[0]){await openRun(activity.runs[0].id);setFollowLatest(true);return}}if(run&&!['COMPLETED','FAILED','CANCELED'].includes(run.state)){setDockOpen(true);setMode('path');return}if(experiments[0]&&experiments[0].id!==report?.id)await openRun(experiments[0].id);else setDockOpen(true);setMode('path')}
  useEffect(()=>{if(!record||report||!experiments.length)return;let active=true;get<{run:PlatformRun;report:Report|null}>(`/api/workflows/runs/${experiments[0].id}`).then(v=>{if(active){setReport(v.report);setRun(v.run)}}).catch(e=>{if(active)notify(String(e),true)});return()=>{active=false}},[record?.id,experiments,report,notify])
  const stepNode = useCallback((id: string) => { setSelected(id); setSelectedEdge(''); setTab('params'); setPreview(''); setArtifactFiles([]); setInspOpen(true) }, [])

  const edit = useCallback((fn: (f: Flow) => Flow, checkpoint = true) => {
    setFlow(previous => {
      if (checkpoint) { history.current.past.push(previous); history.current.past = history.current.past.slice(-40); history.current.future = []; setHistoryTick(x => x + 1) }
      return fn(previous)
    })
    setDirty(true); setChecks([])
  }, [])

  // 串行写入项目偏好，避免快速切换研究时旧请求后到、把 last_workflow_id 写回旧值。
  const preferenceWrites = useRef<Promise<unknown>>(Promise.resolve())
  const rememberWorkflow = useCallback((id: string) => {
    if (me?.mode !== 'local' || !id) return
    const write = preferenceWrites.current.catch(() => undefined)
      .then(() => post('/api/local/preferences', { last_workflow_id: id }))
      .catch(() => { /* 偏好写入失败不阻断研究 */ })
    preferenceWrites.current = write
  }, [me?.mode])

  const load = useCallback((r: Saved) => {
    const pref = readDockPref(r.id)
    const prefOpen = pref?.open ?? false
    setRecord(r); setFlow(displayFlow(r.workflow)); setDirty(false); setSelected(''); setSelectedEdge(''); setChecks([]); setReport(null); setRun(null); setPins([]);setExperiments([]);setRemote(null);setFollowLatest(true);setMode('path')
    setDockOpen(prefOpen); dockDesired.current = pref?.height ?? DOCK_COMPACT; setDockHeight(pref?.height ?? DOCK_COMPACT); setDockExpanded(prefOpen && !!pref?.expanded)
    history.current = { past: [], future: [] }; setHistoryTick(x => x + 1)
    setTimeout(() => canvasRef.current.fitView({ padding: 0.06 }), 50)
  }, [])

  const openSaved = useCallback((savedWorkflow: Saved) => {
    load(savedWorkflow)
    setShowStart(false)
    setRouteError('')
    setBootError('')
    rememberWorkflow(savedWorkflow.id)
  }, [load, rememberWorkflow])

  useEffect(() => {
    if (!record) return
    const timer = setTimeout(() => writeDockPref(record.id, { open: dockOpen, height: dockHeight, expanded: dockExpanded }), 250)
    return () => clearTimeout(timer)
  }, [record?.id, dockOpen, dockHeight, dockExpanded])

  const closeDock = useCallback(() => {
    setDockOpen(false); setDockExpanded(false)
    requestAnimationFrame(() => resultLinkRef.current?.focus({ preventScroll: true }))
  }, [])
  const toggleDockExpanded = useCallback(() => setDockExpanded(value => !value), [])
  const setDockBodyHeight = useCallback((value:number) => {
    const requested = Math.max(160, Math.min(700, Math.round(value)))
    dockDesired.current = requested
    setDockHeight(Math.min(requested, dockBodyMax()))
  }, [])

  // 视口/路径高度变化时按实际可用空间收敛；变大时恢复到用户期望高度（默认 196）。
  useEffect(() => {
    if (!pathDock || dockExpanded || window.innerWidth <= 1100) return
    let frame = 0
    const clamp = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => requestAnimationFrame(() => {
        const max = dockBodyMax()
        const target = Math.min(dockDesired.current, max)
        setDockHeight(height => height === target ? height : target)
      }))
    }
    clamp()
    window.addEventListener('resize', clamp)
    const path = document.querySelector<HTMLElement>('.wf-path-view')
    const ro = path && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(clamp) : null
    if (path && ro) ro.observe(path)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', clamp)
      ro?.disconnect()
    }
  }, [pathDock, dockExpanded])

  // 拖动/键盘或首次恢复后，若仍越过视口，按实际越界量再收一次；只缩不涨。
  useEffect(() => {
    if (!pathDock || dockExpanded || window.innerWidth <= 1100) return
    let frame = 0
    const check = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const dock = document.querySelector<HTMLElement>('.rs-dock')
        if (!dock) return
        const overflow = dock.getBoundingClientRect().bottom - window.innerHeight
        if (overflow > 0) setDockHeight(height => Math.max(160, Math.floor(height - overflow - 2)))
      })
    }
    check()
    return () => cancelAnimationFrame(frame)
  }, [dockHeight, pathDock, dockExpanded])

  // 只在结果区开合/专注切换时重排视口；拖动高度或普通渲染不抢用户的手动平移。
  useEffect(() => {
    const previous = dockLayout.current
    dockLayout.current = { open: pathDock, expanded: dockExpanded }
    let cancelled = false
    let timer: number | null = null
    const later = (action: () => void) => {
      if (timer) window.clearTimeout(timer)
      requestAnimationFrame(() => requestAnimationFrame(() => {
        timer = window.setTimeout(() => { if (!cancelled) action() }, 60)
      }))
    }
    const fit = () => canvasRef.current.fitView({ padding: 0.12, maxZoom: 1.2, duration: 0 })
    if (pathDock && !previous.open) {
      savedViewport.current = canvasRef.current.getViewport()
      later(fit)
    } else if (pathDock && previous.open && dockExpanded !== previous.expanded) {
      if (!dockExpanded) later(fit)
    } else if (!pathDock && previous.open) {
      const viewport = savedViewport.current
      later(() => { if (viewport) canvasRef.current.setViewport(viewport); else fit() })
    }
    return () => {
      cancelled = true
      if (timer) window.clearTimeout(timer)
    }
  }, [pathDock, dockExpanded])

  useEffect(() => {
    let active = true
    setBootError(''); setRouteError('')
    ;(async () => {
      try {
        const [c, list, f, a] = await Promise.all([
          get<Catalog>('/api/workflows/catalog'),
          get<{ workflows: Saved[] }>('/api/workflows'),
          get<{ files: { name: string; bytes: number }[] }>('/api/workflows/files'),
          get<{ assets: { domain: string; id: string; name: string; page: string; mode: string }[] }>('/api/workflows/assets'),
        ])
        if (!active) return
        setCatalog(c); setAssets(a.assets); setSaved(list.workflows); setFiles(f.files)
        if (arg) {
          const [wid, search = ''] = arg.split('?'); const params = new URLSearchParams(search)
          try {
            let opened = await get<Saved>(`/api/workflows/${encodeURIComponent(wid)}${params.has('revision') ? `?revision=${encodeURIComponent(params.get('revision')!)}` : ''}`)
            const rid = params.get('run_id')
            const result = rid ? await get<{ run: PlatformRun & { manifest: { workflow_id: string; revision: number; workflow: Flow } }; report: Report | null }>(`/api/workflows/runs/${encodeURIComponent(rid)}`) : null
            if (!active) return
            if (result) {
              if (result.run.manifest.workflow_id !== opened.id) throw new Error('该运行不属于指定工作流')
              opened = { ...opened, revision: result.run.manifest.revision, workflow: result.run.manifest.workflow }
            }
            load(opened); setSelected(params.get('node') || '')
            if (result) { setRun(result.run); setReport(result.report); setTab('results'); setDockOpen(true); setFollowLatest(false) }
            rememberWorkflow(opened.id)
            setShowStart(false); setBooted(true)
          } catch (reason) {
            if (active) {
              const message = reason instanceof Error ? reason.message : String(reason)
              setRouteError(message); setBooted(true); notify(message, true)
            }
          }
          return
        }
        const draft = initialDraft.current
        if (draft) {
          setRecord(draft.record); setFlow(draft.flow); setSelected(draft.selected); setDirty(draft.dirty); setReport(draft.report); setView(draft.view)
          setTimeout(() => canvasRef.current.setViewport(draft.viewport), 100)
          setShowStart(false); setBooted(true)
          return
        }
        let target = list.workflows[0] || null
        if (me?.mode === 'local') {
          try {
            const prefs = await get<{ last_workflow_id?: string }>('/api/local/preferences')
            const last = typeof prefs.last_workflow_id === 'string' ? prefs.last_workflow_id : ''
            if (last) target = list.workflows.find(item => item.id === last) || target
          } catch { /* 偏好不可读取时仍可进入研究列表 */ }
        }
        if (!active) return
        if (target) { load(target); setShowStart(false) } else { setFlow(empty); setShowStart(true) }
        setBooted(true)
      } catch (reason) {
        if (!active) return
        const message = reason instanceof Error ? reason.message : String(reason)
        setBootError(message); setBooted(true); notify(message, true)
      }
    })()
    return () => { active = false }
  }, [arg, bootNonce, load, me?.mode, notify, rememberWorkflow])

  useEffect(()=>{
    if(!record)return
    let active=true
    const timer=setInterval(()=>{
      get<Saved>(`/api/workflows/${record.id}`).then(next=>{if(!active||next.revision<=record.revision)return;if(dirty||!followLatest)setRemote(next);else{setRecord(next);setFlow(displayFlow(next.workflow));setChecks([]);setRemote(null)}}).catch(()=>{})
      if(followLatest)get<{runs:PlatformRun[]}>(`/api/workflows/${record.id}/activity`).then(async activity=>{const newest=activity.runs[0];if(!active||!newest)return;const value=await get<{run:PlatformRun;report:Report|null}>(`/api/workflows/runs/${newest.id}`);if(active){setRun(value.run);if(value.report)setReport(value.report)}}).catch(()=>{})
      get<{experiments:typeof experiments}>(`/api/workflows/experiments?workflow_id=${record.id}`).then(v=>{if(active)setExperiments(v.experiments)}).catch(()=>{})
    },4000)
    return()=>{active=false;clearInterval(timer)}
  },[record?.id,record?.revision,dirty,followLatest])
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  useEffect(() => {
    if (!record) return
    let active=true
    get<{experiments:{id:string;status:string;revision:number;origin:string}[]}>(`/api/workflows/experiments?workflow_id=${encodeURIComponent(record.id)}`)
      .then(v=>{if(active)setExperiments(v.experiments)}).catch(e=>notify(String(e),true))
    return ()=>{active=false}
  }, [record,run?.state,notify])

  useEffect(() => {
    if (!run || ['COMPLETED', 'FAILED', 'CANCELED'].includes(run.state)) return
    let active = true
    const timer = setInterval(() => {
      get<{ run: PlatformRun; report: Report | null }>(`/api/workflows/runs/${run.id}`).then(value => {
        if (!active) return
        setRun(value.run); if (value.report) setReport(value.report)
        if (value.run.state === 'COMPLETED' && value.run.summary?.download) notify('离线包构建完成，可以下载')
      }).catch(e => { if (active) notify(String(e), true) })
    }, 1000)
    return () => { active = false; clearInterval(timer) }
  }, [run, notify])

  const persist = useRef(() => {})
  persist.current = () => writeDraft({ flow, record, selected, dirty, report, view, viewport: canvasRef.current.getViewport(), updated: Date.now() })
  useEffect(() => () => { persist.current() }, [])

  async function save(): Promise<Saved> {
    const result = record ? await put<Saved>(`/api/workflows/${record.id}`, { workflow: flow, expected_revision: record.revision }) : await post<Saved>('/api/workflows', { workflow: flow })
    setRecord(result); setDirty(false); setSaved(previous => [result, ...previous.filter(r => r.id !== result.id)])
    rememberWorkflow(result.id)
    return result
  }
  const perform = async (name: string, action: () => Promise<unknown>) => { setBusy(name); try { await action() } catch (e) { notify(String(e), true) } finally { setBusy('') } }
  const ensureSaved = async () => dirty || !record ? save() : record

  async function check(target?: string) {
    const s = await ensureSaved()
    const value = await post<{ ok: boolean; checks: Check[]; scope: string }>(`/api/workflows/${s.id}/validate`, { target })
    setChecks(value.checks); notify(value.ok ? '静态自检通过；尚未代表试运行或研究方法审查通过' : '检查发现问题，请查看检查列表', !value.ok)
  }

  async function execute(target?: string, debug = false) {
    setFollowLatest(true)
    const s = await ensureSaved()
    let frozen: Record<string, unknown> | undefined
    if (pins.length) {
      frozen = Object.fromEntries(pins.map(id => [id, report?.nodes[id]?.outputs]).filter(([, value]) => !!value))
      if (!debug) throw Error('有固定输出时只能调试运行；正式运行请先取消固定')
    }
    const r = await post<PlatformRun>(`/api/workflows/${s.id}/run`, { target, mode: debug ? 'debug' : 'research', pins: frozen })
    setRun(r)
    const value = await get<{ run: PlatformRun; report: Report | null }>(`/api/workflows/runs/${r.id}`)
    setRun(value.run); setReport(value.report);setDockOpen(true)
  }

  async function executeBatch() {
    if (pins.length) throw Error('批量正式实验前请取消固定输出')
    const values = [...new Set(seeds.split(/[,，\s]+/).filter(Boolean).map(Number))]
    if (!values.length || values.length > 8 || values.some(v => !Number.isInteger(v) || v < 0 || v > 4294967295)) throw Error('请输入1至8个不同的整数种子，范围0至4294967295')
    const s = await ensureSaved()
    setBatchRuns([])
    for (const seed of values) {
      const r = await post<PlatformRun>(`/api/workflows/${s.id}/run`, { revision: s.revision, mode: 'research', seed })
      setBatchRuns(previous => [...previous, r]); setRun(r)
    }
    notify(`已登记${values.length}次实验，每次保留独立记录；队列仍受平台配额约束`)
  }

  useEffect(() => {
    if (!batchRuns.some(r => !['COMPLETED','FAILED','CANCELED'].includes(r.state))) return
    const timer = setInterval(() => {
      Promise.all(batchRuns.map(r => get<{run:PlatformRun}>(`/api/workflows/runs/${r.id}`).then(v=>v.run)))
        .then(setBatchRuns).catch(e=>notify(String(e),true))
    }, 1500)
    return () => clearInterval(timer)
  }, [batchRuns, notify])

  async function debugSelected() {
    if (!card || !report) throw Error('先选择一次历史运行和需要调试的卡片')
    const parents=new Set<string>()
    function visit(id:string){for(const e of flow.edges.filter(x=>x.target===id)){if(!parents.has(e.source)){parents.add(e.source);visit(e.source)}}}
    visit(card.id)
    const frozen:Record<string,unknown>={}
    for(const id of parents){if(!report.nodes[id]?.outputs || !Object.keys(report.nodes[id].outputs).length)throw Error(`历史运行缺少${id}的输出`);frozen[id]=report.nodes[id].outputs}
    const s=await ensureSaved()
    const r=await post<PlatformRun>(`/api/workflows/${s.id}/run`,{target:card.id,mode:'debug',pins:frozen})
    setRun(r)
    const value=await get<{run:PlatformRun;report:Report|null}>(`/api/workflows/runs/${r.id}`)
    setRun(value.run);setReport(value.report)
  }

  async function groupSelected() {
    if(!groupIds.length)throw Error('请先选择要封装的卡片')
    const value=await post<{definition:Definition;workflow:Flow}>('/api/workflows/subflows/encapsulate',{
      workflow:flow,selected:groupIds,kind:`user.${uid('group')}`,title:groupTitle,exposed:exposed.filter(key=>groupIds.includes(key.split('::')[0])).map(key=>{const [node,parameter]=key.split('::');return {node,parameter}})})
    setCatalog(c=>c?{...c,cards:[...c.cards,value.definition]}:c)
    edit(()=>value.workflow);setGroupIds([]);setExposed([]);setSelected('')
  }

  async function expandSelected() {
    if(!card)return
    const value=await post<{workflow:Flow}>('/api/workflows/subflows/expand',{workflow:flow,node_id:card.id})
    edit(()=>value.workflow);setSelected('')
  }

  function add(d: Definition, position?: { x: number; y: number }) {
    const next=[d.kind,...recent.filter(k=>k!==d.kind)].slice(0,12);setRecent(next);try{localStorage.setItem('qa.wf.recent.'+currentWorkspace(),JSON.stringify(next))}catch{}
    const n: Card = { id: uid('n'), kind: d.kind, version: String(d.version), params: {}, position: position || { x: flow.nodes.length * 60 + 80, y: flow.nodes.length * 40 + 80 } }
    edit(f => ({ ...f, nodes: [...f.nodes, n] })); stepNode(n.id)
  }

  const nodes: FunctionNode[] = useMemo(() => flow.nodes.map(n => {
    const d = definition(n) || { kind: n.kind, version: n.version, title: '缺少卡片定义', inputs: {}, outputs: {}, parameters: {}, requirements: [], code: '', code_hash: '' }
    const ghostInputs = [...new Set(flow.edges.filter(e => e.target === n.id && !d.inputs[e.target_port]).map(e => e.target_port))]
    const ghostOutputs = [...new Set(flow.edges.filter(e => e.source === n.id && !d.outputs[e.source_port]).map(e => e.source_port))]
    // 卡片宽度固定、高度按端口数估算：给 MiniMap 提供初始尺寸（真实测量在其他交互里由 React Flow 负责）。
    const initialHeight = Math.max(150, 90 + Math.max(Object.keys(d.inputs).length + ghostInputs.length, Object.keys(d.outputs).length + ghostOutputs.length) * 28)
    return { id: n.id, type: 'function', position: n.position, selected: selected === n.id, initialWidth: 245, initialHeight, data: {
      definition: d, params: n.params, title: n.title || d.title, flipped: !!n.flipped, status: report?.nodes[n.id], pinned: pins.includes(n.id), stale: stale.has(n.id),
      ghostInputs, ghostOutputs,
      inspect: () => stepNode(n.id), flip: () => edit(f => ({ ...f, nodes: f.nodes.map(c => c.id === n.id ? { ...c, flipped: !c.flipped } : c) })),
    } }
  }), [flow, definition, selected, report, pins, stepNode, edit, stale])
  const edges: Edge[] = useMemo(() => flow.edges.map(e => ({ id: e.id, source: e.source, target: e.target, sourceHandle: e.source_port, targetHandle: e.target_port, selected: e.id === selectedEdge, label: e.source_port, type: 'smoothstep' })), [flow.edges, selectedEdge])

  function validConnection(connection: Connection | Edge): boolean {
    const a = flow.nodes.find(n => n.id === connection.source); const b = flow.nodes.find(n => n.id === connection.target)
    if (!a || !b || a.id === b.id) return false
    const out = definition(a)?.outputs[connection.sourceHandle || '']; const input = definition(b)?.inputs[connection.targetHandle || '']
    return !!out && !!input && compatible(out, input) && !flow.edges.some(e => e.target === b.id && e.target_port === connection.targetHandle)
  }

  function connect(connection: Connection) {
    if (!validConnection(connection)) { notify('连接不兼容、输入已占用或引用自身', true); return }
    edit(f => ({ ...f, edges: [...f.edges, { id: uid('e'), source: connection.source, target: connection.target, source_port: connection.sourceHandle!, target_port: connection.targetHandle! }] }))
  }

  function onNodesChange(changes: NodeChange<FunctionNode>[]) {
    if (!changes.some(c => c.type === 'position' || c.type === 'remove')) return
    const next = applyNodeChanges(changes, nodes)
    edit(f => ({ ...f, nodes: f.nodes.filter(n => next.some(nn => nn.id === n.id)).map(n => ({ ...n, position: next.find(nn => nn.id === n.id)!.position })), edges: f.edges.filter(e => next.some(n => n.id === e.source) && next.some(n => n.id === e.target)) }), false)
  }
  function onEdgesChange(changes: EdgeChange[]) {
    if (!changes.some(c => c.type === 'remove')) return
    const next = applyEdgeChanges(changes, edges)
    edit(f => ({ ...f, edges: f.edges.filter(e => next.some(ne => ne.id === e.id)) }))
  }
  function undo(redo = false) {
    const h = history.current; const from = redo ? h.future : h.past; const to = redo ? h.past : h.future
    const next = from.pop(); if (!next) return; to.push(flow); setFlow(next); setDirty(true); setHistoryTick(x => x + 1)
  }

  function openCode(frozen = false) {
    if (!spec || !card) return
    persist.current()
    const original = frozen ? report?.workflow.nodes.find(n=>n.id===card.id) : card
    if (!original) return
    onNavigate(`function/${original.kind}/${original.version}?node=${encodeURIComponent(card.id)}${frozen && report ? `&run=${encodeURIComponent(report.id)}` : ''}`)
  }

  function chartFrom(source: string, output: string, hash: string) {
    const d = catalog?.cards.find(d=>d.kind==='research.chart')
    if (!d) return
    const parent=flow.nodes.find(n=>n.id===source)!
    const node:Card={id:uid('chart'),kind:d.kind,version:d.version,params:{},position:{x:parent.position.x+340,y:parent.position.y+180}}
    edit(f=>({...f,nodes:[...f.nodes,node],edges:[...f.edges,{id:uid('e'),source,source_port:output,target:node.id,target_port:'data'}]}))
    stepNode(node.id);setChartSource(hash);setVisualEditor('chart')
  }

  function insert(d: Definition) {
    const edge = flow.edges.find(e => e.id === selectedEdge); if (!edge) return
    const a = flow.nodes.find(n => n.id === edge.source)!; const b = flow.nodes.find(n => n.id === edge.target)!
    const source = definition(a)?.outputs[edge.source_port]; const target = definition(b)?.inputs[edge.target_port]
    const inp = Object.entries(d.inputs).find(([, p]) => source && compatible(source, p))
    const out = Object.entries(d.outputs).find(([, p]) => target && compatible(p, target))
    if (!inp || !out) throw Error('该卡片不能插入此连接')
    const node: Card = { id: uid('n'), kind: d.kind, version: String(d.version), params: {}, position: { x: (a.position.x + b.position.x) / 2, y: (a.position.y + b.position.y) / 2 + 100 } }
    edit(f => ({ ...f, nodes: [...f.nodes, node], edges: [...f.edges.filter(e => e.id !== edge.id), { id: uid('e'), source: a.id, source_port: edge.source_port, target: node.id, target_port: inp[0] }, { id: uid('e'), source: node.id, source_port: out[0], target: b.id, target_port: edge.target_port }] }))
    stepNode(node.id)
  }

  async function upload(file: File, parameter: string) {
    const response = await fetch(`/api/workflows/files?name=${encodeURIComponent(file.name)}`, { method: 'POST', credentials: 'same-origin', headers: { 'X-Requested-With': 'quantlab', ...(targetWorkspace() ? { 'X-Workspace-Id': targetWorkspace() } : {}), 'Content-Type': 'application/octet-stream' }, body: file })
    if (!response.ok) throw Error((await response.text()).slice(0, 600))
    const value = await response.json() as { name: string; bytes: number }
    setFiles(previous => [...previous, value])
    if (card) setParam(parameter, value.name)
    notify('输入文件已导入，原有文件不会被覆盖')
  }
  function setParam(name: string, value: unknown) { if (card) edit(f => ({ ...f, nodes: f.nodes.map(n => n.id === card.id ? { ...n, params: { ...n.params, [name]: value } } : n) })) }

  async function exportPackage() {
    const s = await ensureSaved()
    const r = await post<PlatformRun>(`/api/workflows/${s.id}/export`, { include_wheels: true })
    setRun(r); notify('已登记导出任务：将包含输入、源码、依赖锁和离线wheel')
  }

  /** 空工作区的新建动作：不伪造已保存记录，也不打开任何结果。 */
  function startBlank() {
    askDraftId.current = uid('draft')
    setRecord(null); setFlow(structuredClone(empty)); setDirty(true); setSelected(''); setSelectedEdge('')
    setChecks([]); setReport(null); setRun(null); setPins([]); setExperiments([]); setRemote(null); setFollowLatest(true)
    const dockPref = workspaceLayout.current?.dock
    setDockOpen(dockPref?.open ?? false); dockDesired.current = dockPref?.height ?? DOCK_COMPACT
    setDockHeight(dockPref?.height ?? DOCK_COMPACT); setDockExpanded(dockPref?.open ? !!dockPref.expanded : false)
    setRouteError(''); setShowStart(false)
  }

  /** 换模板等于新建一份未保存文档，保留所有既有清理动作。 */
  function applyTemplate(index: number) {
    const template = catalog?.templates[index]
    if (!template) return
    askDraftId.current = uid('draft')
    setRecord(null); setFlow(structuredClone(template)); setDirty(true); setSelected(''); setSelectedEdge('')
    setChecks([]); setReport(null); setRun(null); setPins([]);setExperiments([]);setRemote(null);setFollowLatest(true)
    const dockPref = workspaceLayout.current?.dock
    setDockOpen(dockPref?.open ?? false); dockDesired.current = dockPref?.height ?? DOCK_COMPACT
    setDockHeight(dockPref?.height ?? DOCK_COMPACT); setDockExpanded(dockPref?.open ? !!dockPref.expanded : false)
    setRouteError(''); setShowStart(false)
  }
  function downloadWorkflowJson() {
    const blob = new Blob([stringify(flow)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = 'workflow.json'; a.click(); URL.revokeObjectURL(url)
  }
  function focusLibrary() {
    setLibOpen(true)
    setTimeout(() => searchRef.current?.focus(), 30)
  }
  const downloading = run?.summary?.download
  const runTone = !run ? 'idle' : run.state === 'FAILED' ? 'danger' : run.state === 'COMPLETED' ? 'ok' : ['CANCELED'].includes(run.state) ? 'idle' : 'busy'
  const runLabel = !run ? '未运行' : ({ QUEUED: '排队中', PENDING: '等待中', RUNNING: '运行中', COMPLETED: '已完成', FAILED: '失败', CANCELED: '已取消' } as Record<string, string>)[run.state] || run.state
  const library = (catalog?.cards || []).filter(d => !plugins.some(p=>p.id===modulePlugin(d.kind,d)&&!p.enabled) && (libraryScope==='all'||(libraryScope==='recent'?recent.includes(d.kind):favorites.includes(d.kind))) && (task==='all'||d.kind.startsWith('source.')||modulePlugin(d.kind,d)===task||d.category?.includes(task)) && d.level !== 'internal' && (level === 'all' || level === 'module' ? level === 'all' || d.level === 'module' || d.kind.startsWith('source.') : d.level !== 'module') && (!category || d.category === category) && `${d.title} ${d.kind} ${d.category} ${d.description}`.toLowerCase().includes(query.toLowerCase()))
  const executing = run && !['COMPLETED', 'FAILED', 'CANCELED'].includes(run.state)
  void historyTick

  const hasNotice = !!executing || run?.state === 'FAILED' || !!downloading || (!!report && (dirty || report.mode !== 'debug')) || !!pins.length
  const retryBoot = () => { setBooted(false); setBootNonce(value => value + 1) }

  if (!booted) {
    return <ResearchStart variant="loading" catalog={catalog} onCreate={startBlank} onTemplate={applyTemplate} />
  }
  if (bootError) {
    return <ResearchStart variant="error" catalog={catalog} message={bootError} recent={saved[0] || null}
      onCreate={startBlank} onTemplate={applyTemplate} onRetry={retryBoot}
      onOpenRecent={saved[0] ? () => openSaved(saved[0]) : undefined} />
  }
  if (routeError) {
    return <ResearchStart variant="missing" catalog={catalog} message={routeError} recent={saved[0] || null}
      onCreate={startBlank} onTemplate={applyTemplate} onRetry={retryBoot}
      onOpenRecent={saved[0] ? () => openSaved(saved[0]) : undefined} />
  }
  if (showStart) {
    return <ResearchStart variant="empty" catalog={catalog} onCreate={startBlank} onTemplate={applyTemplate} />
  }

  return <div className={`wf-page rs-studio ${pathDock?'has-results':''} ${pathDock&&dockExpanded?'dock-expanded':''}`} data-result-layout={pathDock?(dockExpanded?'focus':'compact'):'closed'}>
    <div className="rs-studio-head"><div><span className="rs-eyebrow">RESEARCH STUDIO</span><h2>沿着问题，走向证据</h2><p>组织研究路径，检查每一步的数据，让结论有据可循。</p></div><button className="button small ghost" onClick={()=>onNavigate('settings/plugins')}>管理研究插件 ↗</button></div>
    <div className="rs-modebar"><div className="rs-tabs" role="tablist" aria-label="研究工作视图">{(['path','experiments','artifacts'] as const).filter(m=>m!=='experiments'||!plugins.some(p=>p.id==='experiments'&&!p.enabled)).map(m=><button key={m} role="tab" aria-selected={mode===m} className={mode===m?'on':''} onClick={()=>setMode(m)}>{m==='path'?'研究路径':m==='experiments'?'实验与比较':'研究成果'}</button>)}</div><div className="rs-modebar-right"><div ref={askEntryRef} className="wf-ask-entry" /><button ref={resultLinkRef} type="button" className="rs-result-link" aria-expanded={pathDock} aria-controls="wf-result-dock" onClick={()=>void perform('打开最近结果',latestResult)}><span className="rs-dot"/>{executing?'查看运行进度':report||experiments.length?'最近一次结果 ↗':'运行结果'}{report&&<small> · {report.mode==='debug'?'调试':'研究'}</small>}</button></div></div>
    {remote&&<div className="rs-impact"><span>Agent 或其他窗口已保存 r{remote.revision}。当前草稿保留，请检查差异后合并。</span><details><summary>查看新版本</summary><pre className="rs-content">{stringify(remote.workflow)}</pre></details><button className="button small" onClick={()=>{history.current.past.push(flow);setFlow(displayFlow(remote.workflow));setRecord(remote);setDirty(false);setRemote(null)}}>载入新版本</button></div>}
    {stale.size>0&&<div className="rs-impact"><span>配置已改变：{stale.size} 个节点需要更新结果，其余节点由运行器判断缓存是否可复用。</span><button className="button small" onClick={()=>{setMode('path');stepNode([...stale][0]);canvas.fitView({nodes:[{id:[...stale][0]}],padding:.15,maxZoom:1})}}>定位受影响节点</button></div>}

    <div className="wf-sticky">
      <div className="wf-toolbar wf-main-toolbar" role="toolbar" aria-label="研究工作流操作">
        <div className="wf-toolbar-group">
          <select aria-label="打开已保存工作流" value={record?.id || ''} onChange={e => { const r = saved.find(x => x.id === e.target.value); if (r) openSaved(r) }}><option value="">未保存的工作流</option>{saved.map(r => <option key={r.id} value={r.id}>{r.title} · r{r.revision}</option>)}</select>
          <select aria-label="从模板新建" value="" onChange={e => applyTemplate(Number(e.target.value))}><option value="" disabled>新建研究…</option>{catalog?.templates.map((t, i) => <option value={i} key={t.title}>{t.title}</option>)}</select>
        </div>
        <div className="wf-toolbar-group">
          <div className="wf-seg" role="group" aria-label="编辑视图">
            <button type="button" className={`button small ${view === 'canvas' ? 'on' : ''}`} aria-pressed={view === 'canvas'} onClick={() => setView('canvas')}>画布</button>
            <button type="button" className={`button small ${view === 'steps' ? 'on' : ''}`} aria-pressed={view === 'steps'} onClick={() => setView('steps')}>步骤</button>
          </div>
          <button type="button" className="button small ghost" aria-label="撤销" title="撤销上一次修改" onClick={() => undo()} disabled={!history.current.past.length}>撤销</button>
          <button type="button" className="button small ghost" aria-label="重做" title="重做" onClick={() => undo(true)} disabled={!history.current.future.length}>重做</button>
        </div>
        <div className="wf-toolbar-actions">
          <button type="button" className="button" onClick={() => perform('保存', save)} disabled={!!busy}>保存{dirty ? ' *' : ''}</button>
          <button type="button" className="button" onClick={() => perform('自检', () => check())} disabled={!!busy}>全链路自检</button>
          <button type="button" className="button primary wf-run-btn" onClick={() => perform('运行', () => execute(undefined, !!pins.length))} disabled={!!busy || !!executing}>{executing ? '运行中…' : pins.length ? '调试运行' : '运行研究'}</button>
          <details className="wf-more" ref={moreMenu}>
            <summary className="button wf-more-trigger" aria-label="更多操作" title="更多操作">更多<span aria-hidden="true">▾</span></summary>
            <div className="wf-more-menu" role="menu" aria-label="更多操作">
              <button type="button" role="menuitem" className="wf-more-item" onClick={() => { closeMore(); void perform('导出', exportPackage) }} disabled={!!busy}>导出独立运行包</button>
              <button type="button" role="menuitem" className="wf-more-item" onClick={() => { closeMore(); graphImport.current?.click() }}>导入工作流</button>
              <button type="button" role="menuitem" className="wf-more-item" onClick={() => { closeMore(); downloadWorkflowJson() }}>导出工作流JSON</button>
              <hr />
              <button type="button" role="menuitem" className="wf-more-item" onClick={() => { closeMore(); onNavigate('settings/local') }}>执行环境与本地文件</button>
            </div>
          </details>
          <input ref={graphImport} type="file" accept=".json" hidden style={{ display: 'none' }} onChange={e => { const f = e.target.files?.[0]; if (f) perform('导入', async () => { const value = JSON.parse(await f.text()) as Flow; if (value.format !== empty.format) throw Error('工作流格式不兼容'); setRecord(null); setFlow(value); setDirty(true); setPins([]) }) }} />
        </div>
      </div>
      <div className="wf-title-row">
        <input aria-label="研究名称" value={flow.title} onChange={e => edit(f => ({ ...f, title: e.target.value }))} />
        <div className="wf-meta">
          <span className={`wf-chip ${record ? 'saved' : 'draft'}`} title="保存状态">{record ? `${arg.includes('run_id=')&&!followLatest?'冻结修订':'已保存'} r${record.revision}` : '草稿'}{record && dirty ? ' · 未保存修改' : ''}</span>
          <span className="wf-chip">{flow.nodes.length} 张卡片</span>
          <span className="wf-chip">{flow.edges.length} 条连接</span>
          <span className={`wf-chip wf-run-state ${runTone}`} title={run ? `运行记录 ${run.id}` : '尚未提交运行'}>运行：{runLabel}</span>
          {busy && <span className="wf-chip wf-busy" role="status">{busy}…</span>}
        </div>
      </div>
    </div>
    {hasNotice && <div className="wf-notices">
      {executing && run && <div className="wf-run-banner" role="status"><span className="wf-banner-k">后台任务</span>{run.id} · {run.state} · {run.progress?.done || 0}/{run.progress?.total || '?'} <button type="button" className="button small" onClick={() => perform('取消', async () => { await post(`/api/runs/${run.id}/commands`, { command: 'cancel' }); notify('已请求取消') })}>取消</button></div>}
      {run?.state === 'FAILED' && <div className="wf-run-banner danger" role="alert"><span className="wf-banner-k">任务失败</span>{run.error_message || '请查看节点日志'}</div>}
      {downloading && <div className="wf-run-banner ok"><span className="wf-banner-k">离线包</span>完整离线包已就绪 <a className="button small" href={withWs(downloading)}>下载ZIP</a></div>}
      {report && dirty && <div className="wf-run-banner muted">显示上次运行结果；当前修改尚未重新执行。</div>}
      {report && report.mode !== 'debug' && <div className="wf-run-banner"><span className="wf-banner-k">运行证据</span>{report.id}<button type="button" className="button small" disabled={!!busy || !!executing} onClick={() => perform('导出冻结运行', async () => { const r = await post<PlatformRun>(`/api/workflows/runs/${report.id}/export`, { include_wheels: true }); setRun(r); notify('将使用本次运行冻结的代码、工作流与输入构建独立包') })}>导出本次运行</button></div>}
      {!!pins.length && <div className="wf-run-banner warn"><span className="wf-banner-k">调试固定</span>{pins.join('、')}；当前输出不用于正式研究。<button type="button" className="button small" onClick={() => setPins([])}>全部取消固定</button></div>}
    </div>}
    <div className="wf-path-view" style={{display:mode==='path'?'flex':'none'}}>
    <div
      className={`wf-workspace ${libOpen ? '' : 'lib-off'} ${inspOpen ? '' : 'insp-off'} ${inspOpen && rightState !== 'compact' ? `panel-${rightState}` : ''}`}
      style={inspOpen && rightState === 'expanded' ? ({ '--wf-panel-w': `${rightWidth}px` } as CSSProperties) : undefined}
    >
      <aside className={`wf-library ${libOpen ? '' : 'is-collapsed'}`} aria-label="研究模块库">
        <div className="wf-pane-head">
          <h2>研究模块库</h2>
          <button type="button" className="wf-pane-toggle" aria-label={libOpen ? '收起模块库' : '展开模块库'} aria-expanded={libOpen} title={libOpen ? '收起模块库' : '展开模块库'} onClick={() => setLibOpen(open => !open)}>{libOpen ? '«' : '»'}</button>
        </div>
        {libOpen && <div className="wf-pane-body">
          <div className="wf-quicknav" role="group" aria-label="工作区入口">
            {([['project', '项目'], ['recent', '近期研究'], ['favorites', '收藏']] as const).map(([key, label]) => (
              <button key={key} type="button" className={quickNav === key ? 'on' : ''} aria-expanded={quickNav === key} aria-controls="wf-quicknav-panel" onClick={() => setQuickNav(current => current === key ? null : key)}>{label}</button>
            ))}
          </div>
          {quickNav && <section id="wf-quicknav-panel" className="wf-quicknav-panel" aria-label={quickNav === 'project' ? '当前项目' : quickNav === 'recent' ? '近期研究' : '收藏'}>
            {quickNav === 'project' && <>
              <div className="wf-quicknav-row static"><span>项目</span><b>{me?.workspace?.name || '当前工作区'}</b></div>
              <div className="wf-quicknav-row static"><span>模式</span><b>{me?.mode === 'local' ? '本地项目' : '账号工作区'}</b></div>
              <div className="wf-quicknav-row static"><span>当前研究</span><b>{flow.title || '未命名研究'}</b></div>
              <div className="wf-quicknav-row static"><span>保存状态</span><b>{record ? `${dirty ? '有未保存修改' : '已保存'} · r${record.revision}` : '未保存草稿'}</b></div>
              <div className="wf-quicknav-row static"><span>运行状态</span><b>{runLabel}</b></div>
              <button type="button" className="button small" onClick={() => { setQuickNav(null); onNavigate(me?.mode === 'local' ? 'settings/local' : 'settings') }}>执行环境与本地文件 ↗</button>
            </>}
            {quickNav === 'recent' && <>
              <p className="wf-quicknav-hint">最近保存的 5 项研究；带「最近结果就绪」的是当前研究的最近一次运行。</p>
              {saved.slice(0, 5).map(item => {
                const ready = item.id === record?.id && !!report
                return <button key={item.id} type="button" className={`wf-quicknav-row${item.id === record?.id ? ' on' : ''}${ready ? ' ready' : ''}`} onClick={() => { openSaved(item); setQuickNav(null) }}>
                  <b>{item.title}</b><small>r{item.revision}{ready ? ' · 最近结果就绪' : ''}</small>
                </button>
              })}
              {!saved.length && <p className="wf-empty-hint">还没有保存的研究。先在画布搭一条路径并保存。</p>}
            </>}
            {quickNav === 'favorites' && <>
              <p className="wf-quicknav-hint">收藏的模块卡片（本机记录，可一键加入画布）；成果固定与版本请到「研究成果」查看。</p>
              {(catalog?.cards || []).filter(d => favorites.includes(d.kind)).map(d => (
                <div key={`${d.kind}@${d.version}`} className="wf-quicknav-row">
                  <b>{d.title}</b><small>{d.category || '自定义'} · v{d.version}</small>
                  <button type="button" className="button small" onClick={() => { add(d); setQuickNav(null) }}>加入画布</button>
                </div>
              ))}
              {!favorites.length && <p className="wf-empty-hint">还没有收藏的模块卡片。在模块列表点 ☆ 把它加入常用。</p>}
              <button type="button" className="button small" disabled={!record} onClick={() => { setMode('artifacts'); setQuickNav(null) }}>打开研究成果</button>
            </>}
          </section>}
          <select aria-label="模块浏览层级" value={level} onChange={e=>{setLevel(e.target.value);setCategory('')}}><option value="module">完整研究模块</option><option value="function">基础函数</option><option value="all">全部模块与函数</option></select><select aria-label="函数分类" value={category} onChange={e=>setCategory(e.target.value)}><option value="">所有分类</option>{[...new Set((catalog?.cards||[]).map(d=>d.category).filter(Boolean))].map(c=><option key={c}>{c}</option>)}</select><input ref={searchRef} aria-label="搜索函数卡片" placeholder="搜索卡片或函数" value={query} onChange={e => setQuery(e.target.value)} />
          <div className="rs-library-tags">{[['all','全部'],['recent','最近'],['favorites','常用']].map(([value,label])=><button key={value} className={libraryScope===value?'on':''} onClick={()=>setLibraryScope(value)}>{label}</button>)}</div>
          <select aria-label="按研究任务浏览" value={task} onChange={e=>setTask(e.target.value)}><option value="all">所有研究任务</option><option value="model">模型研究</option><option value="factor">因子研究</option><option value="portfolio">组合研究</option><option value="charts">图表与成果</option></select>
          <div className="wf-library-list">{library.map(d => <div key={`${d.kind}@${d.version}`} className="rs-library-entry"><button type="button" className="wf-library-item" key={`${d.kind}@${d.version}`} onClick={() => add(d)} draggable onDragStart={e => e.dataTransfer.setData('application/quantlab-card', `${d.kind}@${d.version}`)}><b>{d.title}</b><small>{d.category || '自定义'} · v{d.version}</small></button><button className="rs-favorite" aria-label={`${favorites.includes(d.kind)?'取消常用':'设为常用'} ${d.title}`} aria-pressed={favorites.includes(d.kind)} onClick={()=>{const next=favorites.includes(d.kind)?favorites.filter(k=>k!==d.kind):[...favorites,d.kind];setFavorites(next);try{localStorage.setItem('qa.wf.favorites.'+currentWorkspace(),JSON.stringify(next))}catch{}}}>{favorites.includes(d.kind)?'★':'☆'}</button></div>)}</div>
          {!library.length && <p className="wf-empty-hint">没有匹配的卡片，试试切换浏览层级或清空搜索。</p>}
          <button type="button" className="button" onClick={() => { persist.current(); onNavigate('function/new/1') }}>编写自定义卡片</button>
          <details className="wf-group"><summary>从已有研究资产选择</summary><p>因子、组合和执行共用原引擎；原生策略与模型保留定义快照及执行协议。</p>{assets.map(asset=><div key={asset.domain+asset.id}><b>{asset.name}</b><small>{asset.domain} · {asset.mode === 'snapshot' ? '定义快照' : '可执行模块'}</small><div className="wf-btnrow"><button className="button small" disabled={!!busy} onClick={()=>perform('导入研究资产',async()=>{const d=await post<Definition>('/api/workflows/assets/snapshot',{domain:asset.domain,asset_id:asset.id});setCatalog(c=>c?{...c,cards:[...c.cards,d]}:c);add(d)})}>加入画布</button><button className="button small" onClick={()=>{persist.current();onNavigate(asset.page)}}>专业页</button></div></div>)}{!assets.length && <p>此工作区暂无已保存资产。</p>}</details>
          <details className="wf-group"><summary>封装为我的模板</summary><label>模板名称<input value={groupTitle} onChange={e=>setGroupTitle(e.target.value)} /></label>{flow.nodes.map(n=><div key={n.id}><label><input type="checkbox" checked={groupIds.includes(n.id)} onChange={e=>setGroupIds(ids=>e.target.checked?[...ids,n.id]:ids.filter(id=>id!==n.id))}/>{n.title||definition(n)?.title||n.id}</label>{groupIds.includes(n.id) && <details><summary>公开参数</summary>{Object.entries(definition(n)?.parameters.properties || {}).filter(([key])=>!n.bindings?.[key]).map(([key,field])=><label key={key}><input type="checkbox" checked={exposed.includes(n.id+'::'+key)} onChange={e=>setExposed(keys=>e.target.checked?[...keys,n.id+'::'+key]:keys.filter(k=>k!==n.id+'::'+key))}/>{field.title || key}</label>)}</details>}</div>)}<button type="button" className="button" disabled={!groupIds.length || !groupTitle.trim()} onClick={()=>perform('封装研究模板',groupSelected)}>封装选中卡片与参数</button><p>边界连接自动生成；未公开参数保留当前值，展开后仍可修改。</p></details>
        </div>}
      </aside>
      <section className={`wf-canvas wf-canvas-${view}`} aria-label="可拖拽研究画布" onDragOver={e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy' }} onDrop={e => { e.preventDefault(); const d = definitions.get(e.dataTransfer.getData('application/quantlab-card')); if (d) add(d, canvas.screenToFlowPosition({ x: e.clientX, y: e.clientY })) }}>
        {view === 'canvas' ? <><ReactFlow<FunctionNode> nodes={nodes} edges={edges} nodeTypes={nodeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={connect} isValidConnection={validConnection} onNodeClick={(_, n) => stepNode(n.id)} onEdgeClick={(_, e) => { setSelectedEdge(e.id); setSelected(''); setInspOpen(true) }} onNodeDragStart={() => { dragSnapshot.current = flow }} onNodeDragStop={() => { if (dragSnapshot.current) { history.current.past.push(dragSnapshot.current); history.current.future = []; setHistoryTick(x => x + 1); dragSnapshot.current = null } }} fitView={!initialDraft.current} defaultViewport={initialDraft.current?.viewport} fitViewOptions={{ padding: 0.06 }} minZoom={0.15} maxZoom={2.5} selectionOnDrag={false} selectionKeyCode="Shift" panOnDrag={[0, 1, 2]} deleteKeyCode={['Backspace', 'Delete']}><Background /><Controls />{!!flow.nodes.length && <MiniMap pannable zoomable position="top-right" nodeColor="var(--accent)" nodeStrokeColor="var(--line)" maskColor="color-mix(in srgb, var(--canvas) 40%, transparent)" style={{ background: 'var(--canvas)', width: 150, height: 110 }} />}</ReactFlow>
          {!flow.nodes.length && <div className="wf-canvas-empty" role="note">
            <b>画布还是空的</b>
            <p>从左侧模块库点击或拖入卡片，拖动端口把卡片连起来；空白处拖动平移，滚轮缩放。</p>
            <div className="wf-btnrow">
              <button type="button" className="button small" onClick={focusLibrary}>浏览模块库</button>
              <button type="button" className="button small" onClick={() => applyTemplate(0)} disabled={!catalog?.templates.length}>载入内置模板</button>
            </div>
          </div>}</>
          : <div className="wf-steps">{flow.nodes.map(n => <button type="button" className={`wf-step ${selected === n.id ? 'on' : ''}`} key={n.id} onClick={() => stepNode(n.id)}><b>{n.title || definition(n)?.title || n.kind}</b><small>{report?.nodes[n.id] ? statusName[report.nodes[n.id].status] : '尚未运行'}</small></button>)}</div>}
      </section>
      <PanelHost
        open={inspOpen}
        onToggleOpen={toggleInspector}
        tab={rightTab}
        onTab={setRightTab}
        state={inspOpen ? rightState : 'compact'}
        onState={setRightState}
        width={rightWidth}
        onWidth={setRightWidth}
        inspector={<>
        {selectedEdge && <><p className="wf-hint">选中连接后可插入兼容卡片，自动保留两端连接。</p><select aria-label="在线路中插入卡片" value="" onChange={e => { const d = definitions.get(e.target.value); if (d) perform('插入', async () => insert(d)) }}><option value="">选择卡片…</option>{library.map(d => <option key={`${d.kind}@${d.version}`} value={`${d.kind}@${d.version}`}>{d.title}</option>)}</select><button type="button" className="button" onClick={() => edit(f => ({ ...f, edges: f.edges.filter(e => e.id !== selectedEdge) }))}>删除连接</button></>}
        {(card || tab === 'code') && <><div className="wf-tabs"><button className={tab === 'params' ? 'on' : ''} onClick={() => setTab('params')}>参数</button><button className={tab === 'code' ? 'on' : ''} onClick={() => { if (spec) openCode() }}>代码</button><button className={tab === 'results' ? 'on' : ''} onClick={() => {setTab('results');setDockOpen(true)}}>数据与日志</button></div>
          {tab === 'params' && card && spec && <>
            <h2>{spec.title}</h2><p>{spec.description}</p><code>{spec.kind}@{spec.version}</code>
            <label>显示名称<input value={card.title || ''} onChange={e => edit(f => ({ ...f, nodes: f.nodes.map(n => n.id === card.id ? { ...n, title: e.target.value } : n) }))} /></label>
            {Object.entries(spec.parameters.properties || {}).filter(([,field])=>!field.visible_when || Object.entries(field.visible_when).every(([key,values])=>values.includes(card.params[key] ?? spec.parameters.properties?.[key]?.default))).filter(([key])=>!(spec.view === 'network' && key === 'graph')).map(([key, field]) => <ParameterField key={`${card.id}-${key}`} name={key} field={field} value={card.params[key] ?? field.default} bound={card.bindings?.[key]} customized={Object.hasOwn(card.params,key)} onChange={v => setParam(key, v)} onReset={()=>edit(f=>({...f,nodes:f.nodes.map(n=>{if(n.id!==card.id)return n;const params={...n.params};delete params[key];return {...n,params}})}))} />)}
            {spec.view === 'network' && <button className="button primary" onClick={()=>setVisualEditor('network')}>编辑神经网络组件图</button>}
            {spec.view === 'chart' && <button className="button primary" onClick={()=>{setChartSource(report?.nodes[card.id]?.inputs?.data?.hash || (()=>{const wire=flow.edges.find(e=>e.target===card.id && e.target_port==='data');return wire ? report?.nodes[wire.source]?.outputs[wire.source_port]?.hash || '' : ''})());setVisualEditor('chart')}}>打开图表工作台</button>}
            <BindingPicker key={card.id} card={card} spec={spec} flow={flow} report={report} definitions={definitions} onChange={(name,binding)=>edit(f=>({...f,nodes:f.nodes.map(n=>{if(n.id!==card.id)return n;const bindings={...n.bindings};if(binding)bindings[name]=binding;else delete bindings[name];return {...n,bindings}})}))}/>
            {spec.file_params?.map(name => <div key={name}><label>{name} · 已导入文件<select aria-label={`选择文件参数 ${name}`} disabled={!!card.bindings?.[name]} value={String(card.params[name] || '')} onChange={e => setParam(name, e.target.value)}><option value="">选择文件…</option>{files.map(f => <option key={f.name}>{f.name}</option>)}</select></label><label>{name} · 导入新文件<input type="file" disabled={!!card.bindings?.[name]} onChange={e => { const f = e.target.files?.[0]; if (f) perform('上传文件', () => upload(f, name)) }} /></label></div>)}
            <details><summary>连接到其他卡片</summary>{Object.keys(spec.outputs).map(out => <label key={out}>{out}<select value="" onChange={e => { const [target, handle] = e.target.value.split('::'); connect({ source: card.id, sourceHandle: out, target, targetHandle: handle }) }}><option value="">选择兼容输入…</option>{flow.nodes.filter(n => n.id !== card.id).flatMap(n => Object.entries(definition(n)?.inputs || {}).filter(([, p]) => compatible(spec.outputs[out], p)).map(([name]) => <option key={`${n.id}-${name}`} value={`${n.id}::${name}`}>{n.title || definition(n)?.title} / {name}</option>))}</select></label>)}</details>
            <details><summary>高级：替换函数或版本</summary><label>替换函数版本<select value={`${card.kind}@${card.version}`} onChange={e => { const d = definitions.get(e.target.value); if (d) edit(f => ({ ...f, nodes: f.nodes.map(n => n.id === card.id ? { ...n, kind: d.kind, version: String(d.version) } : n) })) }}>
              {catalog?.cards.map(d => <option key={`${d.kind}@${d.version}`} value={`${d.kind}@${d.version}`}>{d.title} · {d.kind}@{d.version}</option>)}
            </select></label></details>
            <div className="wf-inspector-actions"><button className="button" onClick={() => perform('卡片自检', () => check(card.id))}>卡片自检</button><button className="button" onClick={() => perform('局部运行', () => execute(card.id, !!pins.length))}>执行到此</button><button className="button" onClick={() => openCode()}>查看代码与自定义</button><button className="button" onClick={() => edit(f => ({ ...f, nodes: f.nodes.filter(n => n.id !== card.id), edges: f.edges.filter(e => e.source !== card.id && e.target !== card.id) }))}>删除卡片</button></div>
            {spec.subflow!==undefined && <button className="button" onClick={()=>perform('展开子流程',expandSelected)}>展开为可编辑卡片</button>}
          </>}
          {tab === 'results' && <><button className="button primary" onClick={()=>setDockOpen(true)}>在底部查看输入、输出与日志 ↓</button><h3 className="wf-subhead">数据与运行证据</h3>{report && <button type="button" className="button" onClick={()=>openCode(true)}>查看本次运行冻结代码</button>}{selectedRun ? <><p>{statusName[selectedRun.status]} · {report?.mode === 'debug' ? '调试结果' : '研究运行'}</p>{selectedRun.error && <p role="alert" className="wf-error">{selectedRun.error}</p>}<details><summary>本次实际参数与输入</summary><pre>{stringify({ params: selectedRun.params, inputs: selectedRun.inputs, code_hash: selectedRun.code_hash, file_hashes: selectedRun.file_hashes, environment: selectedRun.environment })}</pre></details>{Object.entries(selectedRun.outputs).map(([name, ref]) => <div key={name} className="wf-output"><b>{name}</b><small>{ref.type} · {ref.bytes.toLocaleString()}字节</small><button className="button" onClick={() => perform('读取工件', async () => { const v = await get<{ value: unknown;total?:number;truncated:boolean; files?:{file:string;name:string;sha256:string;bytes?:number}[] }>(`/api/workflows/artifacts/${ref.hash}`); setArtifactFiles(v.files||[]); setPreview((v.truncated?`预览前100行 / 共${v.total}行\n`:'')+stringify(v.value)) })}>查看数据</button>{ref.type==='table' && <button className="button" onClick={()=>chartFrom(card!.id,name,ref.hash)}>由此生成图表</button>}</div>)}{selectedRun.child_run_id && <button className="button" onClick={()=>perform('读取模块内部证据',async()=>{const child=await get<Report>(`/api/workflows/runs/${report!.id}/children/${selectedRun.child_run_id}`);setPreview(stringify(child));setArtifactFiles([])})}>查看内部函数运行证据</button>}<button className="button" onClick={() => setPins(p => p.includes(card!.id) ? p.filter(x => x !== card!.id) : [...p, card!.id])}>{pins.includes(card!.id) ? '取消固定输出' : '固定本次输出供调试'}</button><button className="button" onClick={()=>perform('使用历史输入调试',debugSelected)}>使用历史输入调试此卡片</button><pre>{[...(selectedRun.logs || []), selectedRun.stdout || '', selectedRun.traceback || ''].join('\n')}</pre></> : <p>尚无该卡片的运行记录。</p>}{artifactFiles.length>0 && <details className="wf-file-list" open><summary>可下载的研究文件（{artifactFiles.length}）</summary>{artifactFiles.map((file,i)=><div key={`${file.file}-${i}`}><a href={withWs(`/api/workflows/files/download?name=${encodeURIComponent(file.file)}`)}>{file.name}</a>{/\.png$/i.test(file.file)&&<img alt={file.name} loading="lazy" style={{maxWidth:'100%',display:'block',marginTop:8}} src={withWs(`/api/workflows/files/download?name=${encodeURIComponent(file.file)}`)}/>}</div>)}</details>}{preview && <pre className="wf-preview">{preview}</pre>}</>}
        </>}
        {!card && !selectedEdge && tab !== 'code' && <div className="wf-inspector-empty">
          <b>未选中卡片</b>
          <p>点击画布上的卡片查看参数、代码与运行数据；点击连线可插入卡片或删除连接。</p>
          <p>空白处拖动平移，滚轮缩放，Shift 拖动框选，Delete 删除选中项。</p>
          <div className="wf-btnrow">
            <button type="button" className="button small" onClick={focusLibrary}>浏览模块库</button>
            <button type="button" className="button small" onClick={() => applyTemplate(0)} disabled={!catalog?.templates.length}>载入内置模板</button>
          </div>
        </div>}
        </>}
        ask={<AskAIPanel
          embedded
          open={inspOpen && rightTab === 'ask'}
          onOpen={openAskPanel}
          onClose={closeAskPanel}
          entryHost={askEntryHost}
          removedOutputs={removedAskOutputs}
          onToggleOutput={toggleAskOutput}
          referenceRequest={referenceRequest}
        />}
        context={researchContext ? (
          rightTab === 'context' ? (
            <div className="wf-context-tab">
              <p className="wf-hint">这里是「问 AI」实际会收到的研究上下文：项目、研究、修订、保存状态、运行、选中节点与输出引用。改动会同步到「问 AI」页签。</p>
              <ResearchContextStrip
                research={researchContext}
                attached={contextAttached}
                hasCode={!!spec?.code?.trim()}
                onToggle={(ref) => toggleAskOutput(askScope, ref)}
                onReference={(node) => { setReferenceRequest({ node, nonce: Date.now() }); setRightTab('ask') }}
              />
              <details className="wf-context-raw"><summary>查看发送给 AI 的结构化上下文</summary><pre>{stringify(researchContext)}</pre></details>
              <button type="button" className="button small" onClick={() => setRightTab('ask')}>在「问 AI」中继续 ↗</button>
            </div>
          ) : (
            // 同一份上下文条只在当前可见的页签里挂载，避免「问 AI」页签与上下文页签各渲染一份；
            // 也避免 E2E / 读屏把隐藏副本当成第二个上下文条。
            <div className="wf-inspector-empty" aria-label="研究上下文（未激活页签）">
              <b>研究上下文</b>
              <p>切到「上下文」页签查看并调整随提问发送的项目、研究、运行、节点与输出引用。</p>
            </div>
          )
        ) : (
          <div className="wf-inspector-empty" aria-label="研究上下文空态">
            <b>还没有研究上下文</b>
            <p>打开或新建一项研究后，这里会显示项目、研究、修订、保存状态、运行、选中节点与输出引用；它们也是「问 AI」随提问发送的资料。</p>
          </div>
        )}
      />
    </div>
      {!!checks.length && <section className="wf-section wf-check-card" aria-label="检查结果">
        <h2>运行前检查</h2>
        <div className="wf-check-list">{checks.map((c, i) => <div key={i} className={`wf-check ${c.passed ? 'pass' : 'fail'}`}><span className="wf-check-mark" aria-hidden="true">{c.passed ? '✓' : '✕'}</span><div className="wf-check-text"><b>{c.name}</b>{c.node&&<button className="button small ghost" onClick={()=>{stepNode(c.node!);canvas.fitView({nodes:[{id:c.node!}],padding:.15,maxZoom:1})}}>定位节点 ↗</button>}{c.node && <small>卡片 {c.node}</small>}{c.message && <small>{c.message}</small>}</div><span className="wf-check-state">{c.passed ? '通过' : '未通过'}</span></div>)}</div>
        <small className="wf-section-note">静态自检、实际执行成功、研究方法可信是不同层次。</small>
      </section>}
    {pathDock&&<ResultDock report={report} flow={flow} selected={selected} stale={stale} definitions={definitions} onChart={chartFrom} onSelect={id=>{stepNode(id);canvas.fitView({nodes:[{id}],padding:.15,maxZoom:1})}} onClose={closeDock} height={dockHeight} expanded={dockExpanded} onHeight={setDockBodyHeight} onToggleExpand={toggleDockExpanded}/>}
    </div>
    <div style={{display:mode==='artifacts'?'block':'none'}}>{record&&<ResearchNotes key={record.id} workflowId={record.id} runId={report?.id} citation={citation}/>}</div>
    {mode==='artifacts'&&<ArtifactCenter onCite={row=>{setCitation({...row,at:Date.now()});document.querySelector('[aria-label="研究笔记"]')?.scrollIntoView({block:'nearest'})}} workflowId={record?.id} refresh={run?.state} onLocate={row=>{if(row.workflow_id!==record?.id){persist.current();onNavigate(`workflows/${row.workflow_id}?run_id=${row.run_id}&node=${row.node_id}`)}else void perform('定位产物',async()=>{await openRun(row.run_id);stepNode(row.node_id);setTimeout(()=>canvas.fitView({nodes:[{id:row.node_id}],padding:.15,maxZoom:1}),80)})}}/>}
    <section className="wf-sections" aria-label="实验与检查" style={{display:mode==='experiments'?'grid':'none'}}>
      <ExperimentPanel flow={flow} definitions={definitions} ensureSaved={ensureSaved} experiments={experiments} notify={notify} onOpen={id=>void perform('查看实验',()=>openRun(id))} />
      <details className="wf-section">
        <summary>多种子实验</summary>
        <p className="wf-section-hint">使用同一修订与数据提交独立实验，最多8次；失败也保留，逐项查看指标后比较。</p>
        <div className="wf-field-row">
          <label className="wf-inline-field">种子列表<input aria-label="实验种子列表" value={seeds} onChange={e=>setSeeds(e.target.value)} /></label>
          <button type="button" className="button" disabled={!!busy || !!executing} onClick={()=>perform('批量实验',executeBatch)}>提交多种子实验</button>
        </div>
        {batchRuns.map(r => <div key={r.id} className="wf-run-row"><code>{r.id}</code><span>{r.state}</span><button type="button" className="button small" onClick={()=>perform('查看实验',async()=>{const v=await get<{run:PlatformRun;report:Report|null}>(`/api/workflows/runs/${r.id}`);setRun(v.run);setReport(v.report)})}>查看结果</button></div>)}
      </details>
      {record && <details className="wf-section">
        <summary>历史实验与版本</summary>
        <div className="wf-field-row">
          <label className="wf-inline-field">历史实验<select aria-label="打开历史实验" value="" onChange={e=>perform('读取实验',async()=>{const v=await get<{run:PlatformRun;report:Report|null}>(`/api/workflows/runs/${e.target.value}`);setRun(v.run);setReport(v.report)})}><option value="">选择运行记录…</option>{experiments.map(x=><option key={x.id} value={x.id}>{x.id} · r{x.revision} · {statusName[x.status]||x.status} · {x.origin}</option>)}</select></label>
          <label className="wf-inline-field">恢复历史内容<select aria-label="恢复历史工作流内容" value="" onChange={e=>perform('恢复历史',async()=>{const v=await get<Saved>(`/api/workflows/${record.id}?revision=${e.target.value}`);edit(()=>v.workflow);notify('已载入历史内容；保存时会生成新版本')})}><option value="">选择版本…</option>{Array.from({length:record.revision},(_,i)=><option key={i} value={i+1}>r{i+1}</option>)}</select></label>
        </div>
      </details>}
      {report && <details className="wf-section">
        <summary>本次实验与全部分支状态 · {report.id}</summary>
        <p className="wf-section-hint">固定定义哈希：<code>{report.workflow_hash}</code></p>
        <div className="wf-node-list">{Object.entries(report.nodes).map(([id, r]) => <div key={id} className="wf-node-row"><code>{id}</code><span>{statusName[r.status]}</span>{r.error && <small className="wf-error">{r.error}</small>}</div>)}</div>
      </details>}
    </section>
    {visualEditor === 'network' && card && spec && <NetworkEditor value={(card.params.graph ?? spec.parameters.properties?.graph?.default) as NetworkGraph} layout={card.ui?.network_layout} onApply={(graph,layout)=>edit(f=>({...f,nodes:f.nodes.map(n=>n.id===card.id?{...n,params:{...n.params,graph},ui:{...n.ui,network_layout:layout}}:n)}))} onClose={()=>setVisualEditor(null)} />}
    {visualEditor === 'chart' && card && <ChartEditor value={card.params} artifact={chartSource} onApply={params=>edit(f=>({...f,nodes:f.nodes.map(n=>n.id===card.id?{...n,params:{...n.params,...params}}:n)}))} onClose={()=>setVisualEditor(null)} />}
  </div>
}

function ParameterField({ name, field, value, bound, customized, onChange, onReset }: { name: string; field: import('./types').Field; value: unknown; customized?: boolean; bound?: { input: string; path: string[] }; onChange: (value: unknown) => void; onReset: () => void }) {
  const display = field.widget === 'json' ? stringify(value) : typeof value === 'object' ? stringify(value) : value === undefined ? '' : String(value)
  const [text, setText] = useState(display)
  const [error, setError] = useState('')
  useEffect(() => { setText(display); setError('') }, [display])
  const commit = () => { if(text===display)return; try { onChange(field.widget === 'json' ? JSON.parse(text) : parseField(text, field.type)); setError('') } catch { setError('请输入有效JSON') } }
  if(field.type==='object' && field.properties){
    const object=(value && typeof value==='object' && !Array.isArray(value)?value:{}) as Record<string,unknown>
    const entries=Object.entries(field.properties)
    const renderChild=([key,child]:[string,typeof field])=><ParameterField key={key} name={name+'.'+key} field={child} value={object[key] ?? child.default} bound={bound} customized={Object.hasOwn(object,key)} onChange={v=>onChange({...object,[key]:v})} onReset={()=>{const copy={...object};delete copy[key];onChange(copy)}} />
    const main=field.primary ? entries.filter(([key])=>field.primary!.includes(key)) : entries
    const advanced=field.primary ? entries.filter(([key])=>!field.primary!.includes(key)) : []
    return <div className="wf-param-object"><h3>{field.title || name}</h3><small>{bound ? '来自输入绑定' : customized ? '手动配置与引擎默认' : '引擎默认配置'}</small>{main.map(renderChild)}{!!advanced.length && <details><summary>更多参数（{advanced.length}组）</summary>{advanced.map(renderChild)}</details>}<details><summary>完整配置JSON（含自定义字段）</summary><textarea aria-label={`参数 ${name}`} disabled={!!bound} rows={6} value={text} onChange={e=>setText(e.target.value)} onBlur={commit}/></details>{customized && !bound && <button className="button" onClick={onReset}>恢复整组默认</button>}{error && <small className="wf-error">{error}</small>}</div>
  }
  return <label>{field.title || name}<small>{customized ? '手动设置' : '模块默认'} · {name}{field.minimum !== undefined ? ` · ≥${field.minimum}` : ''}{field.maximum !== undefined ? ` · ≤${field.maximum}` : ''}</small>{field.description && <small>{field.description}</small>}{bound && <small>绑定：{[bound.input, ...bound.path].join('.')}</small>}
    {field.type === 'boolean' ? <input type="checkbox" checked={!!value} disabled={!!bound} onChange={e => onChange(e.target.checked)} />
      : field.enum ? <select disabled={!!bound} value={String(value ?? '')} onChange={e => onChange(parseField(e.target.value, field.type))}>{field.enum.map(v => <option key={String(v)}>{String(v)}</option>)}</select>
        : field.type === 'object' || field.type === 'array' || field.widget === 'json' ? <textarea disabled={!!bound} aria-label={`参数 ${name}`} rows={Math.min(8,text.split('\n').length+1)} value={text} onChange={e=>setText(e.target.value)} onBlur={commit} /> : <input disabled={!!bound} aria-label={`参数 ${name}`} value={text} onChange={e => setText(e.target.value)} onBlur={commit} onKeyDown={e => { if (e.key === 'Enter') commit() }} />}
    {customized && !bound && field.default !== undefined && <button type="button" className="button ghost" onClick={e=>{e.preventDefault();onReset()}}>恢复默认</button>}{error && <small className="wf-error">{error}</small>}
  </label>
}

export default function WorkflowPage(props: PageProps & {arg?:string}) { return <ReactFlowProvider><Editor {...props} /></ReactFlowProvider> }
