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
import { askStream, askAI, type AiAction, type AskContextItem, type AskMeta, type AskTurn, type ToolCatalog, type ToolRecord } from '../apiAskAI'
import { fmtMoney, fmtNum, fmtPct, type BacktestResult } from '../api2'
import type { ContextView, Effort } from '../apiLab'
import { Composer, ContextRing, ModelMenu, PlusMenu } from './chat/Composer'
import { AssistantText, EventLine, UserBubble, Working } from './chat/Blocks'
import { openInLab } from './handoff'

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
  const active = !!ctx && !!(text.trim() || code.trim())
  useEffect(() => {
    if (!active) return
    const id = ++seq
    entries = [...entries, { id, ctx: { title, text, code, kind } }]
    emit()
    return () => {
      entries = entries.filter((e) => e.id !== id)
      emit()
    }
  }, [active, title, text, code, kind])
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
    }

const HIDDEN_ON = new Set(['lab', 'teams'])   // 全屏对话页自己就是对话，不再叠一个入口
const PREF_KEY = 'qa.askai.prefs'
const SYSTEM_TOKENS = 2600                   // 系统提示 + SDK 速查的大致 token 数
const MAX_TURNS = 12

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

/** 页面资料 → 发给后端的条目（代码单独一条）。 */
function toItems(list: AskAIContext[]): AskContextItem[] {
  const out: AskContextItem[] = []
  for (const c of list) {
    if (c.text.trim()) out.push({ kind: c.kind || 'page', title: c.title, text: c.text })
    if (c.code?.trim()) out.push({ kind: 'code', title: c.text.trim() ? `${c.title}（代码）` : c.title, text: c.code })
  }
  return out
}

const chipLabel = (c: AskAIContext) => (c.code?.trim() && c.text.trim() && !c.title.includes('代码') ? `${c.title} · 代码` : c.title)

export function AskAIPanel() {
  const registered = useSyncExternalStore(subscribe, snapshot, snapshot)
  const [route, setRoute] = useState(routeHead)
  const [open, setOpen] = useState(false)
  const [msgs, setMsgs] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [running, setRunning] = useState(false)
  const [dismissed, setDismissed] = useState<string[]>([])
  const [meta, setMeta] = useState<AskMeta | null>(null)
  const [prefs, setPrefs] = useState(readPrefs)
  const [catalog, setCatalog] = useState<ToolCatalog | null>(null)
  const [showTools, setShowTools] = useState(false)
  const abort = useRef<AbortController | null>(null)
  const feed = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const onHash = () => { setRoute(routeHead()); setDismissed([]) }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const hidden = HIDDEN_ON.has(route)
  useEffect(() => { if (hidden) setOpen(false) }, [hidden])

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
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !document.querySelector('.cmp-menu')) setOpen(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open])

  useEffect(() => {
    const el = feed.current
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight
  }, [msgs])

  useEffect(() => () => abort.current?.abort(), [])

  const attached = useMemo(() => registered.map((e) => e.ctx).filter((c) => !dismissed.includes(c.title)), [registered, dismissed])
  const items = useMemo(() => toItems(attached), [attached])

  const history = useCallback((list: Msg[]): AskTurn[] => list
    .filter((m) => (m.role === 'user' ? m.text : m.content && !m.error))
    .map((m) => (m.role === 'user' ? { role: 'user' as const, content: m.text } : { role: 'assistant' as const, content: m.content }))
    .slice(-MAX_TURNS * 2), [])

  const updateLast = (fn: (m: Extract<Msg, { role: 'assistant' }>) => Partial<Extract<Msg, { role: 'assistant' }>>) =>
    setMsgs((list) => {
      const last = list[list.length - 1]
      if (!last || last.role !== 'assistant') return list
      return [...list.slice(0, -1), { ...last, ...fn(last) }]
    })

  const send = async () => {
    const question = input.trim()
    if (!question || running) return
    const prior = history(msgs)
    setInput('')
    setMsgs((list) => [...list,
      { role: 'user', text: question, attached: attached.map(chipLabel) },
      { role: 'assistant', content: '', thinking: '', streaming: true }])
    setRunning(true)
    const ctrl = new AbortController()
    abort.current = ctrl
    const t0 = performance.now()
    try {
      await askStream({ question, history: prior, context: items, model: prefs.model, effort: prefs.effort, page: route, scenes: prefs.scenes ?? null }, (e) => {
        if (e.type === 'tool') {
          const { type: _t, ...rec } = e
          updateLast((m) => ({ tools: [...(m.tools ?? []), rec] }))
        } else if (e.type === 'delta') {
          if (e.kind === 'thinking') updateLast((m) => ({ thinking: m.thinking + e.text, thinkingMs: performance.now() - t0 }))
          else updateLast((m) => ({ content: m.content + e.text }))
        } else if (e.type === 'reset') {
          updateLast(() => ({ content: '', thinking: '' }))
        } else if (e.type === 'final') {
          updateLast((m) => ({
            content: e.content, thinking: e.thinking || m.thinking, streaming: false, model: e.model,
            fallback: e.mode === 'fallback', code: e.code || undefined, tools: e.tool_calls?.length ? e.tool_calls : m.tools,
          }))
        } else if (e.type === 'error') {
          updateLast(() => ({ streaming: false, error: e.message }))
        }
      }, ctrl.signal)
      updateLast((m) => (m.streaming ? { streaming: false, error: m.content ? undefined : '连接已断开' } : {}))
    } catch (err) {
      if (ctrl.signal.aborted) updateLast(() => ({ streaming: false, stopped: true }))
      else updateLast(() => ({ streaming: false, error: (err as Error).message || '请求失败' }))
    } finally {
      abort.current = null
      setRunning(false)
    }
  }

  const stop = () => abort.current?.abort()

  const reset = () => {
    abort.current?.abort()
    setMsgs([])
    setInput('')
  }

  const continueInLab = () => {
    const turns = history(msgs)
    let transcript = turns.map((t) => `${t.role === 'user' ? '我' : 'AI'}：${t.content}`).join('\n\n')
    if (transcript.length > 8000) transcript = '…（更早的对话已省略）\n\n' + transcript.slice(-8000)
    const lastCode = [...msgs].reverse().find((m) => m.role === 'assistant' && m.code)
    const code = (lastCode?.role === 'assistant' && lastCode.code) || attached.find((c) => c.code?.trim())?.code || undefined
    const name = attached.find((c) => c.code?.trim())?.title.match(/「(.+?)」/)?.[1]
    const pins = [
      ...(transcript ? [{ kind: 'note', title: '「问 AI」对话记录', text: transcript }] : []),
      ...attached.filter((c) => c.text.trim()).map((c) => ({ kind: 'note', title: c.title, text: c.text.slice(0, 8000) })),
    ]
    setOpen(false)
    openInLab({
      idea: input.trim() || '接着「问 AI」里的讨论继续：',
      title: name,
      code,
      pins,
      attached: ['「问 AI」的对话', ...attached.map((c) => c.title)].slice(0, 3).join('、') + (code ? '，以及策略代码' : ''),
    })
  }

  const context: ContextView = useMemo(() => {
    const target = prefs.model || meta?.default_model || ''
    const budget = meta?.models.find((m) => m.target === target)?.context_window || 128_000
    const hist = history(msgs).reduce((n, t) => n + estTokens(t.content.slice(0, 6000)), 0)
    const list = [
      { key: 'system', title: '系统提示与 SDK 速查', tokens: SYSTEM_TOKENS, fixed: true },
      ...items.map((it, i) => ({ key: `ctx-${i}`, title: it.title, tokens: estTokens(it.text.slice(0, 8000)), fixed: false })),
      { key: 'history', title: '对话', tokens: hist, fixed: false },
    ]
    return { items: list, total: list.reduce((n, it) => n + it.tokens, 0), history_tokens: hist, budget, model: target }
  }, [prefs.model, meta, items, msgs, history])

  if (hidden) return null

  const canHandoff = msgs.some((m) => m.role === 'assistant' && m.content) || attached.some((c) => c.code?.trim())

  return (
    <>
      {!open && (
        <button type="button" className="ask-fab" onClick={() => setOpen(true)} title="问 AI（附带当前页面内容）">
          <span aria-hidden>✦</span>问 AI
        </button>
      )}
      {open && (
        <aside className="ask-panel" role="complementary" aria-label="问 AI">
          <header className="ask-head">
            <b>问 AI</b>
            <div className="ask-head-actions">
              {canHandoff && (
                <button type="button" className="ask-head-btn" onClick={continueInLab} title="把对话和代码带进 AI 研究室">在研究室继续</button>
              )}
              {msgs.length > 0 && <button type="button" className="ask-head-btn" onClick={reset} title="清空对话">新对话</button>}
              <button type="button" className="ask-icon" onClick={() => setOpen(false)} aria-label="关闭" title="关闭（Esc）">
                <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
              </button>
            </div>
          </header>

          <div className="ask-feed" ref={feed}>
            {msgs.length === 0 && <div className="ask-empty">关于这个页面，想问什么？</div>}
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
              placeholder={attached.length ? '就当前页面提问…' : '问点什么…'}
              above={attached.length > 0 ? <>
                {attached.map((c) => (
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
      )}
    </>
  )
}

/** 工具调用记录：只读 / 新建记录直接显示结果；需要确认的显示确认卡片。 */
function ToolTrail({ tools }: { tools: ToolRecord[] }) {
  return (
    <div className="ask-trail">
      {tools.map((t, i) => t.status === 'pending' || t.action_id ? <ConfirmCard key={i} rec={t} /> : (
        <div key={i} className={`ask-tool ${t.status}`} title={t.preview}>
          <span className="mark">{t.status === 'error' ? '✕' : '✓'}</span>{t.title}
          {t.preview && <span className="pv">{t.preview}</span>}
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
