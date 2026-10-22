/**
 * 首页：「今天研究什么？」——直接和 AI 对话；预测、策略、因子、组合、执行、完整流程、调研报告都是斜杠命令。
 * 右侧是可编辑的关注列表（多个标的），没有真实行情的标的如实显示「未接入数据」，可以一键同步。
 */
import { directBars } from '../../marketDirect'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useDraft } from '../../components/draft'
import { get } from '../../http'
import { lab, labPrefs, type Effort, type LabKind, type LabMeta } from '../../apiLab'
import { models } from '../../apiModels'
import { Composer, ModelMenu, type SlashCommand } from '../../components/chat/Composer'
import { usePlatformCommands } from '../../components/chat/platformCommands'
import { Sparkline } from '../../components/charts2'
import { QuantBot, QuantEyes, useBotFlash, viewFromSignal, type BotView } from '../../components/QuantBot'
import { store } from '../../components/prefs'
import { t } from '../../i18n'
import { pickExamples, RESEARCH_TYPES, type ResearchType } from '../lab/domains'
import { syncData } from '../lab/research'
import { WatchSettings, storedWatchRange, watchRange, WATCH_RANGE_KEY } from './WatchSettings'
import { labChanged } from '../lab/bus'

/** 斜杠命令 → 研究类型。中英文都认。 */
const TYPE_CMDS: { cmd: string; alias: string[]; type: ResearchType; help: string }[] = [
  { cmd: '/strategy', alias: ['/策略'], type: 'strategy', help: '策略：写策略代码并回测' },
  { cmd: '/factor', alias: ['/因子'], type: 'factor', help: '因子：挖掘信号，按 IC 与样本外评估' },
  { cmd: '/predict', alias: ['/预测', '/model', '/模型'], type: 'model', help: '预测：用因子训练预测模型' },
  { cmd: '/portfolio', alias: ['/组合'], type: 'portfolio', help: '组合：把预测变成权重并控制风险' },
  { cmd: '/execution', alias: ['/执行'], type: 'execution', help: '执行：拆单算法与成本仿真' },
  { cmd: '/pipeline', alias: ['/完整流程', '/全流程'], type: 'pipeline', help: '完整流程：选标的 → 因子 → 策略 → 模型 → 组合 → 执行' },
  { cmd: '/report', alias: ['/调研', '/报告'], type: 'report', help: '调研报告：联网与平台数据调研，导出 PDF' },
]
const TYPE_SHORT: Record<ResearchType, string> = {
  auto: 'AI 自动判断',
  strategy: '代码与回测',
  factor: 'IC 与样本外',
  model: '训练预测',
  portfolio: '权重与风险',
  execution: '拆单与成本',
  pipeline: '端到端研究',
  report: '联网调研报告',
}

const EXTRA_CMDS: SlashCommand[] = [
  { cmd: '/parallel', group: '协作', arg: '[方向1 | 方向2]', help: '唤起子 Agent 并行比较多个方向' },
  { cmd: '/plan', group: '协作', arg: '[任务]', help: 'AI 列分步计划并一步步做完' },
  { cmd: '/discuss', group: '协作', arg: '[议题]', help: '在研究室里开一场团队讨论' },
]

function parse(text: string): { type: ResearchType | null; rest: string; head: string } {
  const [head, ...rest] = text.trim().split(/\s+/)
  const hit = TYPE_CMDS.find((c) => c.cmd === head || c.alias.includes(head))
  return { type: hit?.type ?? null, rest: hit ? rest.join(' ') : text.trim(), head }
}

export function HomeAsk({ onNavigate, notify, brief }: { onNavigate: (k: string) => void; notify: (m: string, e?: boolean) => void; brief?: ReactNode }) {
  const [text, setText] = useDraft('home')
  const [busy, setBusy] = useState(false)
  // 超过 2 万字的内容自动转成 txt：作为钉住材料随新对话一起创建（AI 按需分段读取全文）
  const [longFiles, setLongFiles] = useState<{ title: string; text: string }[]>([])
  const [seed, setSeed] = useState(() => Math.random())
  const [hover, setHover] = useState(false)
  const [activeIdea, setActiveIdea] = useState(0)
  const ideasRef = useRef<HTMLDivElement>(null)
  const fx = useBotFlash()
  useEffect(() => {
    if (hover) return
    const t = window.setInterval(() => setActiveIdea((i) => (i + 1) % 5), 5200)
    return () => window.clearInterval(t)
  }, [hover])
  useEffect(() => {
    setActiveIdea(0)
    requestAnimationFrame(() => ideasRef.current?.scrollTo({ left: 0, behavior: 'smooth' }))
  }, [seed])
  useEffect(() => {
    const el = ideasRef.current
    const card = el?.children[activeIdea] as HTMLElement | undefined
    if (!el || !card) return
    const left = el.scrollLeft + card.getBoundingClientRect().left - el.getBoundingClientRect().left - 8
    el.scrollTo({ left: Math.max(0, left), behavior: 'smooth' })
  }, [activeIdea])
  // 建议：从所有研究类型里各取一些，随机换批
  const ideas = useMemo(() => {
    const types: ResearchType[] = ['strategy', 'factor', 'model', 'pipeline', 'report', 'portfolio']
    return types.map((t, i) => ({ type: t, text: pickExamples(t, 1, (seed * (i + 3)) % 1)[0] })).slice(0, 5)
  }, [seed])
  const { type } = parse(text)
  // 模型与思考程度：和 AI 研究室新对话共用同一份偏好（这台设备）
  const [meta, setMeta] = useState<LabMeta | null>(null)
  const [model, setModel] = useState(() => store.get('qa.lab.model', ''))
  const [effort, setEffort] = useState<Effort>(() => store.get('qa.lab.effort', 'medium') as Effort)
  useEffect(() => { lab.meta().then(setMeta).catch(() => undefined) }, [])
  const baseCmds: SlashCommand[] = [
    ...TYPE_CMDS.map((c) => ({ cmd: c.cmd, group: '研究类型', arg: '[想法]', help: `${c.help}（也可用 ${c.alias[0]}）` })),
    ...EXTRA_CMDS,
  ]
  // 平台上的每个功能都能从首页直接用斜杠命令发起（新对话里 AI 会直接调用对应工具）
  const commands = [...baseCmds, ...usePlatformCommands(baseCmds)]

  const chooseType = (id: ResearchType) => {
    fx.fire('tap')
    const rest = parse(text).rest
    if (id === 'auto') { setText(rest); return }
    const cmd = TYPE_CMDS.find((item) => item.type === id)
    if (cmd) setText(`${cmd.cmd} ${rest}`)
  }

  const start = async () => {
    const t = text.trim()
    if ((!t && !longFiles.length) || busy) return
    const { type: rt, rest, head } = parse(t)
    const body = rest || t
    const kind: LabKind = rt && rt !== 'pipeline' && rt !== 'report' && rt !== 'auto' ? rt : 'strategy'
    const workflow = rt === 'pipeline' || rt === 'report' ? rt : rt ? '' : 'auto'
    if (rt && !rest) { setText(`${head} `); notify(`在 ${head} 后面写上想研究的内容`); return }
    setBusy(true)
    fx.fire('start')
    try {
      const v = await lab.create({
        kind, idea: body.replace(/^\/\S+\s*/, '') || longFiles[0]?.title || '长文本', market_auto: true, workflow, suggest: store.get('qa.lab.suggest', '1') === '1',
        ...(longFiles.length ? { pins: longFiles.map((f) => ({ kind: 'file' as const, title: f.title, text: f.text })) } : {}),
        model, effort, ...labPrefs(),
      })
      const id = v.project.id
      if (workflow === 'report') await lab.report(id, body)
      else if (workflow === 'pipeline') await lab.plan(id, `完整流程：${body}`)
      else if (/^\/plan\b/.test(t)) await lab.plan(id, body.replace(/^\/plan\s*/, ''))
      else await lab.send(id, (rt ? body : t) || `请阅读我附带的「${longFiles[0]?.title}」，概括要点并告诉我可以怎么研究`)
      setLongFiles([])
      labChanged()
      setText('')
      onNavigate(`lab/${id}`)
    } catch (e) {
      notify((e as Error).message, true)
      setBusy(false)
    }
  }

  return (
    <div className="home-ask home-ask2">
      <div className="home-ask-head">
        <QuantBot size={44} state={busy ? 'strain' : text.trim() ? 'thinking' : 'idle'} flash={fx.flash} />
        <div>
          <h1>今天研究什么？</h1>
          {brief}
        </div>
      </div>
      <section className="home-type-picker" aria-label="研究主题选择">
        <div className="home-type-picker-head">
          <b>研究主题</b>
          <span>先选方向，再描述你的想法；自动模式会由 AI 判断该用哪些研究工具。</span>
        </div>
        <div className="home-type-options" role="group" aria-label="研究主题">
          <button type="button" className={`home-type-card${!type ? ' on' : ''}`} aria-pressed={!type}
            title={RESEARCH_TYPES.find((x) => x.id === 'auto')?.hint} onClick={() => chooseType('auto')}>
            <b>自动</b><small>{TYPE_SHORT.auto}</small>
          </button>
          {TYPE_CMDS.map((c) => (
            <button key={c.cmd} type="button" className={`home-type-card${type === c.type ? ' on' : ''}`} aria-pressed={type === c.type}
              title={c.help} onClick={() => chooseType(c.type)}>
              <b>{RESEARCH_TYPES.find((x) => x.id === c.type)?.label}</b><small>{TYPE_SHORT[c.type]}</small>
            </button>
          ))}
        </div>
      </section>
      <Composer value={text} onChange={setText} onSend={start} disabled={busy} commands={commands} sendLabel="开始" hasAttachments={longFiles.length > 0}
        onLongText={async (f) => { const text = await f.text(); setLongFiles((x) => [...x, { title: f.name, text }]) }}
        placeholder="描述你想研究的问题；研究主题已在上方选择"
        above={type ? <div className="cmp-attached on"><span>{RESEARCH_TYPES.find((x) => x.id === type)?.label} · {RESEARCH_TYPES.find((x) => x.id === type)?.hint}</span></div> : undefined}
        right={meta ? (
          <ModelMenu models={meta.models} value={model} defaultModel={meta.default_model} effort={effort} efforts={meta.efforts}
            onModel={(m) => { setModel(m); store.set('qa.lab.model', m) }}
            onEffort={(e) => { setEffort(e); store.set('qa.lab.effort', e) }} />
        ) : undefined} />
      <div className="home-idea-block" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
        <div className="home-idea-head">
          <span><QuantEyes size={14} state="thinking" /> 研究建议</span>
          <button type="button" className="link-btn" onClick={() => setSeed(Math.random())}>↻ 换一批</button>
        </div>
        <div className="home-ideas" ref={ideasRef} key={seed}>
          {ideas.map((x, i) => {
            const cmd = TYPE_CMDS.find((c) => c.type === x.type)!
            return (
              <button key={x.text} type="button" className={`home-idea-card${i === activeIdea ? ' on' : ''}`}
                style={{ animationDelay: `${i * 55}ms` }}
                onClick={() => setText(x.type === 'strategy' ? t(x.text) : `${cmd.cmd} ${t(x.text)}`)}>
                <em>{RESEARCH_TYPES.find((r) => r.id === x.type)?.label}</em>
                <span>{x.text}</span>
                <i aria-hidden="true">↗</i>
              </button>
            )
          })}
        </div>
        <div className="home-idea-dots" role="tablist" aria-label="研究建议">
          {ideas.map((x, i) => (
            <button key={x.text} type="button" role="tab" aria-selected={i === activeIdea}
              className={i === activeIdea ? 'on' : ''} aria-label={`研究建议 ${i + 1}`}
              onClick={() => setActiveIdea(i)} />
          ))}
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 关注列表                                                            */
/* ------------------------------------------------------------------ */
interface Quote { symbol: string; connected: boolean; source?: string; t?: number[]; c?: number[]; error?: string }
const WATCH_KEY = 'qa.home.watch'
const DEFAULT_WATCH = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']

/** 模型库里「启用中」模型的最新预测 → 每个标的的观点（只来自真实模型信号）。 */
function useModelViews() {
  const [views, setViews] = useState<Record<string, { view: BotView; model: string }>>({})
  useEffect(() => {
    let alive = true
    models.list('active').then(async (r) => {
      const out: Record<string, { view: BotView; model: string }> = {}
      for (const m of r.models.slice(0, 3)) {
        try {
          const rep = await models.report(m.id)
          const latest = rep.report?.latest
          if (!latest) continue
          const sig = { task: m.summary?.task, label: m.summary?.label }
          for (const [sym, v] of Object.entries(latest.values)) {
            if (!out[sym]) out[sym] = { view: viewFromSignal(v, sig), model: m.name }
          }
        } catch { /* 单个模型失败不影响其他 */ }
      }
      if (alive) setViews(out)
    }).catch(() => undefined)
    return () => { alive = false }
  }, [])
  return views
}

export function Watchlist({ notify, onNavigate }: { notify: (m: string, e?: boolean) => void; onNavigate: (k: string) => void }) {
  const [symbols, setSymbols] = useState<string[]>(() => {
    try { const v = JSON.parse(store.get(WATCH_KEY, '[]')); return Array.isArray(v) && v.length ? v : DEFAULT_WATCH } catch { return DEFAULT_WATCH }
  })
  const [rangeKey, setRangeKey] = useState(() => storedWatchRange((k, fallback) => store.get(k, fallback)))
  const range = watchRange(rangeKey)
  const [quotes, setQuotes] = useState<Quote[] | null>(null)
  const [syncing, setSyncing] = useState('')
  const views = useModelViews()
  const load = async () => {
    const limit = range.bars
    // 关注列表：浏览器直连交易所取行情；直连失败的标的再交给服务器（服务器有本地缓存与「未接入数据」说明）
    const direct = await Promise.all(symbols.map((s) => directBars(s, range.tf, limit)))
    const missing = symbols.filter((_, i) => !direct[i])
    let server: Quote[] = []
    if (missing.length) {
      try {
        server = (await get<{ quotes: Quote[] }>(`/api/market/quotes?symbols=${encodeURIComponent(missing.join(','))}&timeframe=${range.tf}&limit=${limit}`)).quotes
      } catch (e) {
        notify((e as Error).message, true)
      }
    }
    setQuotes(symbols.map((s, i) => {
      const d = direct[i]
      if (d) return { symbol: s, connected: true, source: 'exchange', t: d.map((b) => b.t), c: d.map((b) => b.c) }
      return server.find((q) => q.symbol === s.toUpperCase()) ?? { symbol: s, connected: false, error: '未接入数据' }
    }))
  }
  useEffect(() => { setQuotes(null); load() }, [symbols.join(','), rangeKey]) // eslint-disable-line react-hooks/exhaustive-deps

  const sync = async (sym: string) => {
    setSyncing(sym)
    try {
      const r = await syncData([sym], range.tf, range.days)
      const res = r.results[0]
      notify(res?.ok ? `${sym} 已同步 ${res.bars} 根 ${range.tf} 行情` : `${sym} 同步失败：${res?.errors?.[0] ?? res?.error ?? '交易所没有返回数据'}`, !res?.ok)
      load()
    } catch (e) { notify((e as Error).message, true) } finally { setSyncing('') }
  }
  // 点关注卡片：跳到行情与数据，并默认选中这张卡的标的、K 线周期与时间段（否则会落回默认的 BTC/USDT）
  const openData = (q: Quote) => onNavigate(`data/${q.symbol}/${range.tf}/range:${range.dataRange}`)
  return (
    <div className="watch">
      <div className="watch-head">
        <b>关注</b>
        <span className="muted small">{range.label} · 真实行情</span>
        <span className="lab2-card-gap" />
        <WatchSettings symbols={symbols} range={rangeKey} notify={notify}
          onChange={(s, r) => { setSymbols(s); setRangeKey(r); store.set(WATCH_KEY, JSON.stringify(s)); store.set(WATCH_RANGE_KEY, r) }} />
      </div>
      <div className="watch-grid stagger">
        {(quotes ?? symbols.map((s) => ({ symbol: s, connected: true } as Quote))).map((q) => {
          const c = q.c ?? []
          const last = c[c.length - 1]
          // 涨跌幅与折线用同一个区间（所选时间区间），颜色与数字自然一致
          const chg = c.length > 1 ? last / c[0] - 1 : null
          const mv = views[q.symbol]
          // 能点开（已接入数据、且已经加载完）才把整张卡做成按钮；否则保留「同步」按钮，避免嵌套交互
          const openable = q.connected && quotes !== null
          return (
            <div key={q.symbol}
              className={`watch-card${!q.connected ? ' off' : ''}${quotes === null ? ' loading' : ''}${openable ? ' openable' : ''}`}
              role={openable ? 'button' : undefined}
              tabIndex={openable ? 0 : undefined}
              aria-label={openable ? `打开 ${q.symbol} 的行情与数据` : undefined}
              title={openable ? `在行情与数据中查看 ${q.symbol} · ${range.tf}` : undefined}
              onClick={openable ? () => openData(q) : undefined}
              onKeyDown={openable ? (e) => {
                if (e.target !== e.currentTarget) return
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openData(q) }
              } : undefined}>
              <div className="watch-top">
                <QuantEyes size={16} state={quotes === null ? 'thinking' : !q.connected ? 'confused' : 'idle'} view={mv?.view ?? 'unknown'}
                  title={mv ? `模型「${mv.model}」的观点` : '还没有模型信号'} />
                <b>{q.symbol.replace('/USDT', '')}<small>/USDT</small></b>
                {chg !== null && <span className={`watch-chg ${chg >= 0 ? 'positive' : 'negative'}`} title={`所选区间（${range.label}）涨跌`}>{chg >= 0 ? '+' : ''}{(chg * 100).toFixed(2)}%<small>{range.short}</small></span>}
              </div>
              {quotes === null ? <div className="watch-skel" /> : q.connected && c.length > 1 ? (
                <>
                  <div className="watch-price num">{last.toLocaleString('en-US', { maximumFractionDigits: last < 1 ? 5 : 2 })}</div>
                  <div className="watch-spark" aria-hidden="true">
                    <Sparkline values={c} width={150} height={34} />
                  </div>
                </>
              ) : (
                <div className="watch-off">
                  <span>未接入数据</span>
                  <button type="button" className="link" disabled={syncing === q.symbol} onClick={(e) => { e.stopPropagation(); sync(q.symbol) }}>{syncing === q.symbol ? '同步中…' : '同步'}</button>
                </div>
              )}
            </div>
          )
        })}
      </div>
      {Object.keys(views).length > 0 && <small className="muted">眼睛颜色来自启用中模型的最新预测（看涨 / 看跌 / 中性）。</small>}
    </div>
  )
}
