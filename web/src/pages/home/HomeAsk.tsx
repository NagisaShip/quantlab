/**
 * 首页：「今天研究什么？」——直接和 AI 对话；预测、策略、因子、组合、执行、完整流程、调研报告都是斜杠命令。
 * 右侧是可编辑的关注列表（多个标的），没有真实行情的标的如实显示「未接入数据」，可以一键同步。
 */
import { directBars } from '../../marketDirect'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useDraft } from '../../components/draft'
import { get } from '../../http'
import { lab, labPrefs, type Effort, type LabKind, type LabMeta } from '../../apiLab'
import { models } from '../../apiModels'
import { Composer, ModelMenu, type SlashCommand } from '../../components/chat/Composer'
import { Sparkline } from '../../components/charts2'
import { QuantBot, QuantEyes, useBotFlash, viewFromSignal, type BotView } from '../../components/QuantBot'
import { store } from '../../components/prefs'
import { pickExamples, RESEARCH_TYPES, type ResearchType } from '../lab/domains'
import { MarketSwitcher, syncData } from '../lab/research'
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
  const fx = useBotFlash()
  useEffect(() => {
    if (hover) return
    const t = window.setInterval(() => setSeed(Math.random()), 10_000)
    return () => window.clearInterval(t)
  }, [hover])
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
  const commands: SlashCommand[] = [
    ...TYPE_CMDS.map((c) => ({ cmd: c.cmd, group: '研究类型', arg: '[想法]', help: `${c.help}（也可用 ${c.alias[0]}）` })),
    ...EXTRA_CMDS,
  ]

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
      <Composer value={text} onChange={setText} onSend={start} disabled={busy} commands={commands} sendLabel="开始" hasAttachments={longFiles.length > 0}
        onLongText={async (f) => { const text = await f.text(); setLongFiles((x) => [...x, { title: f.name, text }]) }}
        placeholder="和 AI 聊聊你的想法；输入 / 选择 预测、策略、因子、完整流程、调研报告…"
        above={type ? <div className="cmp-attached on"><span>{RESEARCH_TYPES.find((x) => x.id === type)?.label} · {RESEARCH_TYPES.find((x) => x.id === type)?.hint}</span></div> : undefined}
        right={meta ? (
          <ModelMenu models={meta.models} value={model} defaultModel={meta.default_model} effort={effort} efforts={meta.efforts}
            onModel={(m) => { setModel(m); store.set('qa.lab.model', m) }}
            onEffort={(e) => { setEffort(e); store.set('qa.lab.effort', e) }} />
        ) : undefined}
        left={<div className="home-types">
          {/* 不指定研究类型时就是「自动」：AI 按内容判断该用哪些工具 */}
          <button type="button" className={`chip${!type ? ' on' : ''}`} title={RESEARCH_TYPES.find((x) => x.id === 'auto')?.hint}
            onClick={() => { fx.fire('tap'); setText(parse(text).rest) }}>自动</button>
          {TYPE_CMDS.slice(0, 7).map((c) => (
            <button key={c.cmd} type="button" className={`chip${type === c.type ? ' on' : ''}`} title={c.help}
              onClick={() => { fx.fire('tap'); setText(`${c.cmd} ${parse(text).rest}`) }}>{RESEARCH_TYPES.find((x) => x.id === c.type)?.label}</button>
          ))}
        </div>} />
      <div className="home-ideas stagger" key={seed} onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
        {ideas.map((x) => {
          const cmd = TYPE_CMDS.find((c) => c.type === x.type)!
          return (
            <button key={x.text} type="button" className="lab2-example" onClick={() => setText(x.type === 'strategy' ? x.text : `${cmd.cmd} ${x.text}`)}>
              <em>{RESEARCH_TYPES.find((r) => r.id === x.type)?.label}</em>{x.text}
            </button>
          )
        })}
        <button type="button" className="lab2-example lab2-shuffle" onClick={() => setSeed(Math.random())}>↻ 换一批</button>
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
  const [tf, setTf] = useState(() => store.get('qa.home.watchTf', '1h'))
  const [quotes, setQuotes] = useState<Quote[] | null>(null)
  const [syncing, setSyncing] = useState('')
  const views = useModelViews()
  const load = async () => {
    const limit = tf === '1d' ? 90 : 168
    // 关注列表：浏览器直连交易所取行情；直连失败的标的再交给服务器（服务器有本地缓存与「未接入数据」说明）
    const direct = await Promise.all(symbols.map((s) => directBars(s, tf, limit)))
    const missing = symbols.filter((_, i) => !direct[i])
    let server: Quote[] = []
    if (missing.length) {
      try {
        server = (await get<{ quotes: Quote[] }>(`/api/market/quotes?symbols=${encodeURIComponent(missing.join(','))}&timeframe=${tf}&limit=${limit}`)).quotes
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
  useEffect(() => { setQuotes(null); load() }, [symbols.join(','), tf]) // eslint-disable-line react-hooks/exhaustive-deps

  const sync = async (sym: string) => {
    setSyncing(sym)
    try {
      const r = await syncData([sym], tf, 30)
      const res = r.results[0]
      notify(res?.ok ? `${sym} 已同步 ${res.bars} 根 ${tf} 行情` : `${sym} 同步失败：${res?.errors?.[0] ?? res?.error ?? '交易所没有返回数据'}`, !res?.ok)
      load()
    } catch (e) { notify((e as Error).message, true) } finally { setSyncing('') }
  }
  const span = tf === '1d' ? '90 天' : tf === '4h' ? '28 天' : tf === '1h' ? '7 天' : `${168} 根`
  return (
    <div className="watch">
      <div className="watch-head">
        <b>关注</b>
        <span className="muted small">{span} · 真实行情</span>
        <span className="lab2-card-gap" />
        <MarketSwitcher symbols={symbols} timeframe={tf} multi compact notify={notify}
          onChange={(s, t) => { setSymbols(s); setTf(t); store.set(WATCH_KEY, JSON.stringify(s)); store.set('qa.home.watchTf', t) }} />
      </div>
      <div className="watch-grid stagger">
        {(quotes ?? symbols.map((s) => ({ symbol: s, connected: true } as Quote))).map((q) => {
          const c = q.c ?? []
          const last = c[c.length - 1]
          const back = tf === '1h' ? 24 : tf === '4h' ? 6 : tf === '1d' ? 1 : 24
          const chg = c.length > back ? last / c[c.length - 1 - back] - 1 : null
          const mv = views[q.symbol]
          return (
            <div key={q.symbol} className={`watch-card${!q.connected ? ' off' : ''}${quotes === null ? ' loading' : ''}`}>
              <div className="watch-top">
                <QuantEyes size={16} state={quotes === null ? 'thinking' : !q.connected ? 'confused' : 'idle'} view={mv?.view ?? 'unknown'}
                  title={mv ? `模型「${mv.model}」的观点` : '还没有模型信号'} />
                <b>{q.symbol.replace('/USDT', '')}<small>/USDT</small></b>
                {chg !== null && <span className={`watch-chg ${chg >= 0 ? 'positive' : 'negative'}`} title={tf === '1d' ? '最近 1 天涨跌' : '最近 24 小时涨跌'}>{chg >= 0 ? '+' : ''}{(chg * 100).toFixed(2)}%<small>{tf === '1d' ? '1d' : '24h'}</small></span>}
              </div>
              {quotes === null ? <div className="watch-skel" /> : q.connected && c.length > 1 ? (
                <>
                  <div className="watch-price num">{last.toLocaleString('en-US', { maximumFractionDigits: last < 1 ? 5 : 2 })}</div>
                  <button type="button" className="watch-spark" onClick={() => onNavigate('data')} title="打开行情与数据"><Sparkline values={c} width={150} height={34} /></button>
                </>
              ) : (
                <div className="watch-off">
                  <span>未接入数据</span>
                  <button type="button" className="link" disabled={syncing === q.symbol} onClick={() => sync(q.symbol)}>{syncing === q.symbol ? '同步中…' : '同步'}</button>
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
