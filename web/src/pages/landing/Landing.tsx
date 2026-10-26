import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { QuantBot, useBotFlash, type BotState } from '../../components/QuantBot'
import { Markdown } from '../../components/markdown'
import { CodeBlock } from '../../components/blocks/CodeBlock'
import { directBars, type DirectBar } from '../../marketDirect'
import '../../stylesLanding.css'
import { ReadmePage } from './ReadmePage'
import { AssistantText, ToolLine } from '../../components/chat/Blocks'
import { GoalCardView, PlanCard } from '../../components/lab/LabCards'
import type { LabPlan } from '../../apiLab'
import ApiDocsPage from '../ApiDocs'
import AboutDialog from '../../components/AboutDialog'
import { lang, setLang, t } from '../../i18n'

/**
 * 未登录时的首页：平台介绍 + 实际功能展示。
 * - 开场：QuantLab Bot 从天而降、弹跳落地，眼睛跟着鼠标转；之后拿放大镜在真实的 BTC K 线上找线索；
 * - 展示区用的都是平台真实组件（Markdown + 结构化内容块），数据由浏览器直接从交易所公共接口实时获取；
 *   取不到行情时只画示意图并明确标注，不编造任何回测成绩；
 * - API 说明放在页面底部。登录 / 注册在右侧抽屉里完成。
 */

type Bars = DirectBar[] | null

function useLive(symbol: string, tf: string, limit: number) {
  const [bars, setBars] = useState<Bars>(null)
  const [state, setState] = useState<'loading' | 'live' | 'offline'>('loading')
  useEffect(() => {
    let alive = true
    directBars(symbol, tf, limit).then((b) => {
      if (!alive) return
      setBars(b)
      setState(b && b.length > 10 ? 'live' : 'offline')
    })
    return () => { alive = false }
  }, [symbol, tf, limit])
  return { bars, state }
}

const LANDING_SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']

function useMultiLive(tf: string, limit: number) {
  const [data, setData] = useState<Record<string, DirectBar[]>>({})
  useEffect(() => {
    let alive = true
    Promise.all(LANDING_SYMBOLS.map(async (symbol) => [symbol, await directBars(symbol, tf, limit)] as const))
      .then((rows) => {
        if (!alive) return
        const next: Record<string, DirectBar[]> = {}
        rows.forEach(([symbol, bars]) => { if (bars?.length) next[symbol] = bars })
        setData(next)
      })
    return () => { alive = false }
  }, [tf, limit])
  return data
}

/** 取不到真实行情时的示意 K 线（只用于画面，页面上明确标注「示意」）。 */
function sketchBars(n = 60, seed = 0): DirectBar[] {
  const out: DirectBar[] = []
  let p = 100 + seed * 13
  for (let i = 0; i < n; i++) {
    const o = p
    const c = o * (1 + Math.sin((i + seed * 3) / 4.3) * 0.012 + Math.cos((i + seed) / 2.1) * 0.006)
    out.push({ t: i, o, c, h: Math.max(o, c) * 1.006, l: Math.min(o, c) * 0.994, v: 1, closed: true })
    p = c
  }
  return out
}

function stats(bars: DirectBar[]) {
  const c = bars.map((b) => b.c)
  const r = c.slice(1).map((v, i) => v / c[i] - 1)
  const mean = r.reduce((a, x) => a + x, 0) / Math.max(1, r.length)
  const sd = Math.sqrt(r.reduce((a, x) => a + (x - mean) ** 2, 0) / Math.max(1, r.length - 1))
  let peak = c[0], mdd = 0
  for (const v of c) { peak = Math.max(peak, v); mdd = Math.max(mdd, 1 - v / peak) }
  const up = r.filter((x) => x > 0).length / Math.max(1, r.length)
  return { change: c[c.length - 1] / c[0] - 1, vol: sd * Math.sqrt(8760), mdd, up, last: c[c.length - 1], hi: Math.max(...bars.map((b) => b.h)), lo: Math.min(...bars.map((b) => b.l)) }
}

// ---------------------------------------------------------------- 滚动出现
function useReveal() {
  useEffect(() => {
    const els = document.querySelectorAll<HTMLElement>('[data-reveal]')
    const io = new IntersectionObserver((es) => es.forEach((e) => { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target) } }), { threshold: 0.15 })
    els.forEach((el) => io.observe(el))
    return () => io.disconnect()
  }, [])
}

// ---------------------------------------------------------------- 开场：Bot + 放大镜看 K 线
function HeroScene({ bars, live }: { bars: DirectBar[]; live: boolean }) {
  const W = 620, H = 330, pad = 18
  const lo = Math.min(...bars.map((b) => b.l)), hi = Math.max(...bars.map((b) => b.h))
  const bw = (W - pad * 2) / bars.length
  const x = (i: number) => pad + (i + 0.5) * bw
  const y = (v: number) => pad + (H - pad * 2) * (1 - (v - lo) / (hi - lo || 1))
  const [lens, setLens] = useState({ x: W * 0.55, y: H * 0.45, user: false })
  const raf = useRef(0)
  const svg = useRef<SVGSVGElement>(null)

  // 没人操作时，Bot 拿着放大镜沿着 K 线慢慢扫
  useEffect(() => {
    if (lens.user) return
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduce) return
    const t0 = performance.now()
    const tick = (t: number) => {
      const k = ((t - t0) / 9000) % 1
      const px = pad + 40 + (W - pad * 2 - 80) * (0.5 - 0.5 * Math.cos(k * Math.PI * 2))
      const i = Math.max(0, Math.min(bars.length - 1, Math.floor((px - pad) / bw)))
      setLens((l) => (l.user ? l : { x: px, y: y((bars[i].h + bars[i].l) / 2), user: false }))
      raf.current = requestAnimationFrame(tick)
    }
    raf.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf.current)
  }, [lens.user, bars]) // eslint-disable-line react-hooks/exhaustive-deps

  const onMove = (e: React.PointerEvent) => {
    const r = svg.current!.getBoundingClientRect()
    setLens({ x: ((e.clientX - r.left) / r.width) * W, y: ((e.clientY - r.top) / r.height) * H, user: true })
  }
  const idx = Math.max(0, Math.min(bars.length - 1, Math.floor((lens.x - pad) / bw)))
  const b = bars[idx]
  const chg = b ? b.c / b.o - 1 : 0
  const R = 52
  const candles = (
    <g>
      {bars.map((k, i) => {
        const up = k.c >= k.o
        return (
          <g key={i} className={`lp-candle ${up ? 'up' : 'down'}`} style={{ animationDelay: `${0.9 + i * 0.012}s` }}>
            <line x1={x(i)} x2={x(i)} y1={y(k.h)} y2={y(k.l)} />
            <rect x={x(i) - bw * 0.32} width={bw * 0.64} y={y(Math.max(k.o, k.c))} height={Math.max(1, Math.abs(y(k.o) - y(k.c)))} rx={0.6} />
          </g>
        )
      })}
    </g>
  )
  return (
    <div className="lp-scene">
      <svg ref={svg} viewBox={`0 0 ${W} ${H}`} className="lp-chart" onPointerMove={onMove} onPointerLeave={() => setLens((l) => ({ ...l, user: false }))}
        role="img" aria-label={live ? 'BTC/USDT 实时 K 线' : 'K 线示意图'}>
        <defs>
          <clipPath id="lp-lens"><circle cx={lens.x} cy={lens.y} r={R - 3} /></clipPath>
          <radialGradient id="lp-glass" cx="35%" cy="30%" r="80%">
            <stop offset="0" stopColor="#fff" stopOpacity=".55" />
            <stop offset=".6" stopColor="#fff" stopOpacity=".06" />
            <stop offset="1" stopColor="#fff" stopOpacity="0" />
          </radialGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((f) => <line key={f} className="lp-grid" x1={pad} x2={W - pad} y1={pad + (H - pad * 2) * f} y2={pad + (H - pad * 2) * f} />)}
        {candles}
        {/* 放大镜：镜片里是同一组 K 线放大 2.2 倍 */}
        <g className="lp-lens">
          <circle cx={lens.x} cy={lens.y} r={R} className="lp-lens-bg" />
          <g clipPath="url(#lp-lens)">
            <g transform={`translate(${lens.x} ${lens.y}) scale(2.2) translate(${-lens.x} ${-lens.y})`}>{candles}</g>
          </g>
          <circle cx={lens.x} cy={lens.y} r={R} fill="url(#lp-glass)" className="lp-lens-rim" />
          <line x1={lens.x + R * 0.72} y1={lens.y + R * 0.72} x2={lens.x + R * 1.45} y2={lens.y + R * 1.45} className="lp-lens-handle" />
        </g>
      </svg>
      {b && (
        <div className="lp-bubble" style={{ left: `${(Math.min(lens.x + R + 8, W - 190) / W) * 100}%`, top: `${(Math.max(lens.y - R - 10, 8) / H) * 100}%` }}>
          {live ? (
            <>
              <b>{new Date(b.t).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</b>
              <span className={chg >= 0 ? 'up' : 'down'}>{chg >= 0 ? '▲' : '▼'} {(chg * 100).toFixed(2)}%</span>
              <small>收 {b.c.toLocaleString('en-US', { maximumFractionDigits: 2 })}</small>
            </>
          ) : <b>示意图 · 未连上行情</b>}
        </div>
      )}
      <div className="lp-chart-tag">{live ? <><i className="lp-live" /> BTC/USDT · 1 小时 · 实时（浏览器直连 Binance）</> : '示意图：没有连上交易所行情'}</div>
    </div>
  )
}

function HeroBot() {
  const ref = useRef<HTMLButtonElement>(null)
  const fx = useBotFlash()
  const [state, setState] = useState<BotState>('idle')
  const [jump, setJump] = useState(0)
  // 眼睛跟着鼠标
  useEffect(() => {
    const on = (e: PointerEvent) => {
      const el = ref.current
      if (!el) return
      const r = el.getBoundingClientRect()
      const dx = (e.clientX - (r.left + r.width / 2)) / window.innerWidth
      const dy = (e.clientY - (r.top + r.height / 2)) / window.innerHeight
      el.style.setProperty('--ex', `${Math.max(-1, Math.min(1, dx * 2.4)) * 1.6}px`)
      el.style.setProperty('--ey', `${Math.max(-1, Math.min(1, dy * 2.4)) * 1.2}px`)
    }
    window.addEventListener('pointermove', on)
    return () => window.removeEventListener('pointermove', on)
  }, [])
  // 开场后切到「专注」看 K 线，偶尔开心一下
  useEffect(() => {
    const t1 = window.setTimeout(() => setState('focus'), 2600)
    const t2 = window.setInterval(() => { fx.fire('done'); setJump((n) => n + 1) }, 9000)
    return () => { window.clearTimeout(t1); window.clearInterval(t2) }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <button ref={ref} type="button" className="lp-bot" aria-label="QuantLab Bot：点我" onClick={() => { fx.fire('done'); setJump((n) => n + 1) }}>
      <span className="lp-bot-shadow" />
      <span className="lp-bot-body" key={jump} data-hop={jump > 0 ? '' : undefined}>
        <QuantBot size={168} state={state} flash={fx.flash} title="QuantLab Bot" />
      </span>
    </button>
  )
}

// ---------------------------------------------------------------- 页面
const FEATURES: { icon: string; title: string; tag: string; text: string; points: string[] }[] = [
  { icon: '◆', title: 'AI 研究室', tag: '研究入口', text: '用中文描述想法，Agent 自己选工具：同步行情、写策略、回测、压力测试、样本外检验，列计划并一步步做完。', points: ['自然语言发起', '自动编排工具', '计划与进度可见'] },
  { icon: '▤', title: '可追溯的结论', tag: '证据链', text: '回答里的每个数字都来自工具结果，区分「实测 / 推断 / 外部来源」，引用可点击，结论能一路追到原始记录。', points: ['实测 / 推断 / 外部来源', '引用可跳转', '回到原始记录'] },
  { icon: '◎', title: '团队评审', tag: '协作', text: '市场、成本执行、审计等角色并行发言、互相质疑，研究组长按证据质量汇总，不按人数投票。', points: ['多角色并行', '互相质疑', '按证据汇总'] },
  { icon: '⧉', title: '跨对话协作', tag: '协作', text: 'Agent 能查找、阅读、调起你的其他对话，把子问题拆出去并行研究，再把结果带回来。', points: ['查找历史对话', '拆分子问题', '结果带回主线'] },
  { icon: '≋', title: '从毫秒到日线', tag: '数据', text: '100ms、1s 到日线任意周期：亚秒级由逐笔成交聚合，回测、模拟、实盘对照用同一套 Decimal 账本。', points: ['100ms–1d', '逐笔聚合成 bar', '同一套账本'] },
  { icon: '⛨', title: '安全边界', tag: '治理', text: 'Python 策略在隔离进程中运行；工作区隔离、审计留痕；默认只读与模拟，微实盘必须显式开启。', points: ['隔离进程', '工作区隔离', '默认只读与模拟'] },
  { icon: '⌗', title: '因子与模型资产', tag: '资产', text: '因子库、模型库与实验记录统一沉淀；模型卡保留样本外表现、特征重要性、校准与漂移，方便复盘和复用。', points: ['因子库 / 模型库', '模型卡与特征重要性', '衰减与漂移监控'] },
  { icon: '◫', title: '执行与运行', tag: '运行', text: '从组合权重到拆单、成本仿真、纸面模拟和模拟盘对照，执行缺口与滑点都能逐笔回看。', points: ['拆单与成本仿真', '纸面 / 模拟盘', '执行缺口逐笔对照'] },
]

const FLOW_STEPS = [
  { icon: '◎', title: '选标的', desc: '先约束研究边界：交易所、标的池、周期、回测区间与数据质量。', outputs: ['标的池', '周期', '数据质量'], preview: 'goal' as const },
  { icon: '⌗', title: '因子', desc: '从经济假设出发构造可运行表达式，检查 IC、分组收益与衰减。', outputs: ['因子表达式', 'IC 报告', '分组收益'], preview: 'tool' as const },
  { icon: '⌁', title: '策略', desc: '把信号写成可执行规则，补齐仓位、止损与交易成本。', outputs: ['策略代码', '参数', '风控规则'], preview: 'plan' as const },
  { icon: '◇', title: '模型', desc: '训练预测模型并滚动验证，保留样本外、校准与特征重要性。', outputs: ['模型卡', '样本外', '特征重要性'], preview: 'model' as const },
  { icon: '◫', title: '组合', desc: '把预测转成权重，加入风险预算、暴露与换手约束。', outputs: ['目标权重', '风险归因', '换手'], preview: 'portfolio' as const },
  { icon: '↗', title: '执行', desc: '用拆单与成本仿真进入模拟盘，逐笔对照滑点和执行缺口。', outputs: ['拆单计划', '滑点', '执行报告'], preview: 'execution' as const },
]

const SCREENS = [
  { id: 'top', label: '开场' },
  { id: 'features', label: '能力' },
  { id: 'scenarios', label: '场景' },
  { id: 'demo', label: '功能' },
  { id: 'pipeline', label: '流程' },
  { id: 'validation', label: '验证' },
  { id: 'api', label: 'API' },
  { id: 'closing', label: '开始' },
]

const HERO_METRICS: [string, string][] = [
  ['5', '核心研究室'],
  ['100ms–1d', '全周期数据'],
  ['6', '道验证门'],
  ['0', '默认真实订单'],
]

const SCENARIOS: {
  id: string; icon: string; label: string; hint: string; kicker: string; title: string; text: string
  steps: string[]; outputs: string[]
}[] = [
  {
    id: 'strategy', icon: '⌁', label: '策略研究', hint: '规则 → 回测 → 准入', kicker: 'STRATEGY RESEARCH',
    title: '把交易直觉，写成可以被证伪的规则。',
    text: '用中文描述想法，Agent 会补齐标的、周期与风控，生成策略代码，再用真实行情回测、成本压力测试和样本外检验。',
    steps: ['定义假设', '生成策略', '运行回测', '压力与样本外', '进入纸面模拟'],
    outputs: ['策略代码', '资金曲线', '成交明细', '版本对比', '审计记录'],
  },
  {
    id: 'factor', icon: '⌗', label: '因子研究', hint: '假设 → IC → 衰减', kicker: 'FACTOR RESEARCH',
    title: '不是“找到一个信号”，而是验证它为什么可能有效。',
    text: '从经济直觉出发，生成可运行的因子表达式，按 IC、RankIC、分组收益和不同市场阶段评估，并持续监控衰减。',
    steps: ['提出假设', '构造因子', 'IC / 分组', '稳健性检查', '入库与监控'],
    outputs: ['因子表达式', 'IC 报告', '分组收益', '衰减预警', '因子卡片'],
  },
  {
    id: 'model', icon: '◇', label: '模型研究', hint: '标签 → 训练 → 漂移', kicker: 'MODEL RESEARCH',
    title: '让预测能力有边界，也有可解释的体检报告。',
    text: '从标签设计到训练、滚动验证、特征重要性与漂移监控，模型不只给出预测，还附带样本外表现和使用边界。',
    steps: ['定义标签', '选择模型', '滚动验证', '解释与校准', '上线监控'],
    outputs: ['训练记录', '样本外指标', '特征重要性', '校准曲线', '模型卡'],
  },
  {
    id: 'portfolio', icon: '◫', label: '组合与执行', hint: '预测 → 权重 → 成交', kicker: 'PORTFOLIO & EXECUTION',
    title: '把信号变成仓位，再把仓位变成可解释的成交。',
    text: '预测先经过风险预算与约束变成权重，再通过拆单算法与成本仿真进入模拟盘；组合风险和执行缺口都能逐笔回看。',
    steps: ['输入预测', '风险预算', '生成权重', '拆单仿真', '对照成交'],
    outputs: ['目标权重', '风险归因', '拆单计划', '滑点分析', '执行回放'],
  },
]

const GATES: { no: string; icon: string; title: string; text: string; preview: 'quality' | 'backtest' | 'stress' | 'oos' | 'paper' | 'demo' }[] = [
  { no: '01', icon: '◇', title: '数据体检', text: '先检查缺口、重复与未收盘；缺数据就明确标注，不用假设补齐。', preview: 'quality' },
  { no: '02', icon: '▤', title: '回测复现', text: '成本、滑点与基准进入同一套账本，任何结论都能重新跑一遍。', preview: 'backtest' },
  { no: '03', icon: '≋', title: '压力测试', text: '把成本翻倍、改变区间与参数，观察策略是否仍然成立。', preview: 'stress' },
  { no: '04', icon: '◫', title: '样本外检验', text: '锁住最后一段数据，避免在同一段历史里反复自我说服。', preview: 'oos' },
  { no: '05', icon: '◷', title: '纸面模拟', text: '按真实时间推进，先跑满观察期，再考虑进入下一步。', preview: 'paper' },
  { no: '06', icon: '◎', title: '模拟盘对照', text: '验证执行、延迟和真实滑点，通过后仍需你确认才能晋级。', preview: 'demo' },
]

type ScenarioMarket = { symbol: string; bars: DirectBar[] }[]

const meanOf = (xs: number[]) => xs.length ? xs.reduce((a, x) => a + x, 0) / xs.length : 0
const stdOf = (xs: number[]) => {
  if (xs.length < 2) return 0
  const m = meanOf(xs)
  return Math.sqrt(xs.reduce((a, x) => a + (x - m) ** 2, 0) / (xs.length - 1))
}
const pearson = (a: number[], b: number[]) => {
  if (a.length !== b.length || a.length < 2) return 0
  const ma = meanOf(a), mb = meanOf(b)
  let num = 0, da = 0, db = 0
  for (let i = 0; i < a.length; i++) { num += (a[i] - ma) * (b[i] - mb); da += (a[i] - ma) ** 2; db += (b[i] - mb) ** 2 }
  return Math.sqrt(da * db) ? num / Math.sqrt(da * db) : 0
}
const maxDrawdown = (equity: number[]) => {
  let peak = equity[0] || 1, mdd = 0
  for (const v of equity) { peak = Math.max(peak, v); mdd = Math.max(mdd, 1 - v / peak) }
  return mdd
}
function summarizeReturns(rets: number[]) {
  const equity = [1]
  for (const r of rets) equity.push(equity[equity.length - 1] * (1 + r))
  const mean = meanOf(rets), sd = stdOf(rets)
  return {
    total: equity[equity.length - 1] - 1,
    vol: sd * Math.sqrt(8760),
    sharpe: sd ? mean / sd * Math.sqrt(8760) : 0,
    mdd: maxDrawdown(equity),
    win: rets.length ? rets.filter((r) => r > 0).length / rets.length : 0,
    trades: rets.length,
  }
}
function buildScenarioMarket(bars: Bars, multi: Record<string, DirectBar[]>): ScenarioMarket {
  const rows = LANDING_SYMBOLS.map((symbol, i) => {
    const real = multi[symbol]?.length ? multi[symbol] : symbol === 'BTC/USDT' && bars?.length ? bars : sketchBars(168, i + 1)
    return { symbol, bars: real }
  })
  const n = Math.min(...rows.map((r) => r.bars.length))
  return rows.map((r) => ({ ...r, bars: r.bars.slice(-n) }))
}
function maStrategy(bars: DirectBar[]) {
  const close = bars.map((b) => b.c)
  const fast = 12, slow = 30
  if (close.length < slow + 12) return null
  const times = [bars[slow - 1].t], equity = [1], bench = [1], rets: number[] = []
  let pos = 0, trades = 0
  for (let i = slow; i < close.length; i++) {
    const f = meanOf(close.slice(i - fast, i)), s = meanOf(close.slice(i - slow, i))
    const next = f > s ? 1 : 0
    if (next !== pos) trades++
    pos = next
    const r = pos * (close[i] / close[i - 1] - 1)
    rets.push(r)
    times.push(bars[i].t)
    equity.push(equity[equity.length - 1] * (1 + r))
    bench.push(close[i] / close[slow - 1] - 1)
  }
  return { times, equity, bench, rets, ...summarizeReturns(rets), trades }
}
function crossSection(market: ScenarioMarket, lookback: number, lag = 1) {
  const n = Math.min(...market.map((m) => m.bars.length))
  if (n <= lookback + lag + 2) return null
  const close = market.map((m) => m.bars.slice(-n).map((b) => b.c))
  const times = market[0].bars.slice(-n).map((b) => b.t)
  const ics: number[] = [], ls: number[] = []
  for (let t = lookback; t < n - lag; t++) {
    const sig = close.map((c) => c[t] / c[t - lookback] - 1)
    const fwd = close.map((c) => c[t + lag] / c[t] - 1)
    ics.push(pearson(sig, fwd))
    const order = sig.map((v, i) => [v, i] as const).sort((a, b) => a[0] - b[0])
    const k = Math.max(1, Math.floor(order.length / 2))
    const low = meanOf(order.slice(0, k).map(([, i]) => fwd[i]))
    const high = meanOf(order.slice(-k).map(([, i]) => fwd[i]))
    ls.push(high - low)
  }
  return { ics, ls, times: times.slice(lookback, n - lag), summary: summarizeReturns(ls) }
}
function portfolioSnapshot(market: ScenarioMarket) {
  const n = Math.min(...market.map((m) => m.bars.length))
  const rets = market.map((m) => {
    const c = m.bars.slice(-n).map((b) => b.c)
    return c.slice(1).map((v, i) => v / c[i] - 1)
  })
  const vol = rets.map((r) => stdOf(r) * Math.sqrt(8760))
  const inv = vol.map((v) => 1 / Math.max(v, 0.01))
  const total = inv.reduce((a, x) => a + x, 0)
  const weights = inv.map((v) => v / total)
  const cov = rets.map((a) => rets.map((b) => {
    const ma = meanOf(a), mb = meanOf(b)
    return a.reduce((s, x, i) => s + (x - ma) * (b[i] - mb), 0) / Math.max(1, a.length - 1)
  }))
  let variance = 0
  for (let i = 0; i < weights.length; i++) for (let j = 0; j < weights.length; j++) variance += weights[i] * weights[j] * cov[i][j]
  const rawSd = rets.map((r) => stdOf(r))
  let corrSum = 0, corrN = 0
  for (let i = 0; i < rawSd.length; i++) for (let j = i + 1; j < rawSd.length; j++) {
    if (rawSd[i] && rawSd[j]) { corrSum += cov[i][j] / (rawSd[i] * rawSd[j]); corrN++ }
  }
  return { vol: Math.sqrt(Math.max(0, variance)) * Math.sqrt(8760), weights, maxWeight: Math.max(...weights), avgCorr: corrN ? corrSum / corrN : 0, rows: market.map((m, i) => ({ symbol: m.symbol, volatility: vol[i], weight: weights[i] })) }
}
const scenarioBlock = (kind: string, value: unknown) => ['```ql:' + kind, JSON.stringify(value), '```'].join('\n')
function scenarioMarkdown(id: string, market: ScenarioMarket, live: boolean) {
  const source = live ? 'Binance 公共行情 · 浏览器即时计算' : '演示数据 · 当前未连接多标的行情'
  const note = live ? '由浏览器直连行情即时计算，仅用于展示内容块与指标口径。' : '当前未连接行情，使用演示数据展示平台真实内容块。'
  if (id === 'strategy') {
    const st = maStrategy(market[0].bars)
    if (!st) return scenarioBlock('callout', { tone: 'warn', title: '行情样本不足', text: '连接行情后会在这里展示策略净值与指标。' })
    return [
      scenarioBlock('metrics', { title: '简单均线示例', source, note, items: [
        { label: '示例策略收益', value: st.total, format: 'pct_signed', evidence: live ? 'measured' : undefined },
        { label: '最大回撤', value: st.mdd, format: 'pct', evidence: live ? 'measured' : undefined },
        { label: '夏普比率', value: st.sharpe, format: 'ratio', evidence: live ? 'measured' : undefined },
        { label: '均线切换', value: st.trades, format: 'int', evidence: live ? 'measured' : undefined },
      ] }),
      scenarioBlock('chart', { kind: 'line', title: '策略净值 vs 持有基准', x: st.times, y_format: 'pct_signed', source,
        series: [{ name: '均线示例', data: st.equity.map((v) => v - 1) }, { name: '持有 BTC', data: st.bench, dashed: true }] }),
    ].join('\n\n')
  }
  if (id === 'factor') {
    const f = crossSection(market, 12)
    if (!f) return scenarioBlock('callout', { tone: 'warn', title: '行情样本不足', text: '连接行情后会在这里展示因子 IC 与衰减。' })
    const decay = [1, 2, 4, 8, 12].map((lag) => {
      const d = crossSection(market, 12, lag)
      return { lag, ic: d ? meanOf(d.ics) : 0 }
    })
    return scenarioBlock('factor_report', { title: '横截面动量 · 1h', name: '12h 动量', source, note, dates: f.times, rolling_window: 12, factors: [{ name: '12h 动量', ic: f.ics }], decay })
  }
  if (id === 'model') {
    const a = crossSection(market, 12), b = crossSection(market, 24)
    if (!a || !b) return scenarioBlock('callout', { tone: 'warn', title: '行情样本不足', text: '连接行情后会在这里展示实验对比。' })
    const metrics = (s: ReturnType<typeof summarizeReturns>) => ({ total_return: s.total, sharpe: s.sharpe, max_drawdown: s.mdd, win_rate: s.win, trades: s.trades })
    return scenarioBlock('compare', { title: '信号参数实验 · 横截面动量', source, note,
      metrics: ['total_return', 'sharpe', 'max_drawdown', 'win_rate', 'trades'],
      versions: [
        { label: '24h 回看', metrics: metrics(b.summary), changes: ['较长回看窗口', '换手相对较低'] },
        { label: '12h 回看', metrics: metrics(a.summary), changes: ['缩短回看窗口', '响应更快，换手更高'] },
      ],
      explanation: '这里比较的是两组信号计算，不代表已经训练完成的预测模型；上线前仍需完成样本外与稳定性检查。' })
  }
  const p = portfolioSnapshot(market)
  return [
    scenarioBlock('metrics', { title: '等风险贡献组合 · 演示', source, note, items: [
      { label: '组合年化波动', value: p.vol, format: 'pct', evidence: live ? 'measured' : undefined },
      { label: '最大单币权重', value: p.maxWeight, format: 'pct', evidence: live ? 'measured' : undefined },
      { label: '平均两两相关', value: p.avgCorr, format: 'ratio', evidence: live ? 'measured' : undefined },
      { label: '资产数量', value: p.rows.length, format: 'int' },
    ] }),
    scenarioBlock('data_table', { title: '风险预算权重', source, columns: [{ key: 'symbol', label: '标的' }, { key: 'volatility', label: '年化波动', format: 'pct' }, { key: 'weight', label: '目标权重', format: 'pct' }], rows: p.rows }),
    scenarioBlock('callout', { tone: 'info', title: '从权重到成交还有一段路', text: '真实执行还需要拆单、滑点、冲击成本与模拟盘对照；这些记录会在组合与执行研究中逐笔保留。' }),
  ].join('\n\n')
}

const CAPABILITY_CATEGORIES: {
  id: string; label: string; hint: string; title: string; text: string
  items: { icon: string; name: string; desc: string; tag?: string }[]
}[] = [
  {
    id: 'research', label: '研究引擎', hint: '从问题到研究产出',
    title: '四间研究室，覆盖从想法到执行的全过程。',
    text: '每个入口都不是孤立工具：研究记录、代码版本、指标、图表和证据会沿着同一条链路继续往下走。',
    items: [
      { icon: '◆', name: 'AI 研究室', desc: '自然语言发起，Agent 自动选择工具、列计划并推进。', tag: '核心' },
      { icon: '⌁', name: '策略工作台', desc: '代码、参数、版本与回测记录放在同一页。' },
      { icon: '⌗', name: '因子研究', desc: '因子表达式、IC、分组收益、衰减一起评估。' },
      { icon: '◇', name: '预测模型', desc: '标签、训练、滚动验证与模型卡可复现。' },
      { icon: '◫', name: '组合构建', desc: '风险预算、约束、权重与归因形成闭环。' },
      { icon: '↗', name: '执行仿真', desc: '拆单、滑点、冲击与成交回放。' },
      { icon: '⇥', name: '完整流程', desc: '标的 → 因子 → 策略 → 模型 → 组合 → 执行。' },
      { icon: '◧', name: '调研报告', desc: '联网调研 + 平台数据，最终导出 PDF。' },
    ],
  },
  {
    id: 'data', label: '数据与模型', hint: '真实数据、资产与实验',
    title: '先把数据变得可信，再让模型开始工作。',
    text: '行情、数据质量、缓存、因子库、模型库和实验记录共用一套可追溯的数据底座。',
    items: [
      { icon: '≋', name: '行情与数据', desc: '100ms、1s 到日线，多周期统一接入。' },
      { icon: '✓', name: '数据质量', desc: '缺口、重复、未收盘与异常检查。' },
      { icon: '⟳', name: '历史同步', desc: '真实历史回填，服务端与浏览器各自取数。' },
      { icon: '▤', name: '服务端缓存', desc: '行情与搜索不重复请求外部接口。' },
      { icon: '⌗', name: '因子库', desc: '因子版本、假设、评估与衰减预警。' },
      { icon: '◇', name: '模型库', desc: '启用模型、最新预测与模型卡。' },
      { icon: '⇄', name: '实验对比', desc: '版本、样本外、成本与指标并列比较。' },
      { icon: '⌁', name: '浏览器直连', desc: '展示行情由浏览器获取，降低服务器 IP 压力。' },
    ],
  },
  {
    id: 'validation', label: '验证与运行', hint: '先证明，再晋级',
    title: '回测只是开始，六道门决定能不能继续。',
    text: '同一套 Decimal 账本贯穿回测、压力测试、样本外、纸面模拟与模拟盘，晋级前始终需要你确认。',
    items: [
      { icon: '▤', name: '回测复现', desc: '成本、滑点、基准与成交明细完整保留。' },
      { icon: '≋', name: '压力测试', desc: '成本翻倍、区间变化与参数扰动。' },
      { icon: '◫', name: '样本外检验', desc: '紧封测试集，避免反复自我说服。' },
      { icon: '◇', name: '上线验证', desc: '按阶段检查策略是否满足晋级条件。' },
      { icon: '◷', name: '纸面模拟', desc: '按真实时间推进，跨过观察期。' },
      { icon: '◎', name: '交易所模拟盘', desc: '执行、延迟与滑点真实对照。' },
      { icon: '⛨', name: '微实盘', desc: '必须显式开启，并有急停与安全边界。' },
      { icon: '⇆', name: '实盘对照', desc: '只读对照真实成交与平台口径。' },
    ],
  },
  {
    id: 'collaboration', label: '协作与治理', hint: '团队、工作区与审计',
    title: '研究不是一个人的记忆，而是一组可检查的记录。',
    text: '角色、对话、工作区、权限和审计统一在一个平台上，让团队结论既能并行产生，也能被追到证据。',
    items: [
      { icon: '◎', name: '团队评审', desc: '多角色并行发言、互相质疑、汇总证据。' },
      { icon: '⧉', name: '跨对话协作', desc: '查找历史对话，拆分子问题并带回结论。' },
      { icon: '⌂', name: '工作区隔离', desc: '成员、数据、模型与回测互不串线。' },
      { icon: '⚿', name: '成员与权限', desc: '共享模型、个人模型与工作区限额。' },
      { icon: '▤', name: '审计留痕', desc: '关键操作保留时间、对象与操作者。' },
      { icon: '◷', name: '后台任务', desc: '租约、检查点、断点恢复与状态回传。' },
      { icon: '✉', name: '通知机器人', desc: 'Telegram / 飞书接收研究进度与提醒。' },
      { icon: '⌁', name: 'API / SDK', desc: '所有能力都能通过 REST 接口调用。' },
    ],
  },
]

function categoryMarkdown(id: string, bars: Bars, live: boolean) {
  const data = bars && live ? bars : sketchBars(168, 2)
  const source = live ? 'Binance 公共行情 · 浏览器实时获取' : '演示数据 · 当前未连接行情'
  if (id === 'research') {
    const s = stats(data)
    const first = new Date(data[0].t).toLocaleDateString('zh-CN'), last = new Date(data[data.length - 1].t).toLocaleDateString('zh-CN')
    return [
      ['```ql:metrics', JSON.stringify({ title: '实时研究摘要', source, evidences: live ? 'measured' : undefined, items: [
        { label: '最新价', value: s.last, format: 'money' },
        { label: '区间涨跌', value: s.change, format: 'pct_signed', window: first + ' ~ ' + last },
        { label: '年化波动', value: s.vol, format: 'pct' },
        { label: '上涨小时占比', value: s.up, format: 'pct' },
      ] }), '```'].join('\n'),
      ['```ql:claims', JSON.stringify({ title: '证据类型', claims: [
        { text: '指标由浏览器直连行情计算', type: 'measured' },
        { text: '机制解释仍需进一步验证', type: 'inferred' },
        { text: '外部资料仅作参考', type: 'external' },
      ] }), '```'].join('\n'),
    ].join('\n\n')
  }
  if (id === 'data') {
    const s = stats(data)
    const first = new Date(data[0].t).toLocaleDateString('zh-CN'), last = new Date(data[data.length - 1].t).toLocaleDateString('zh-CN')
    return [
      ['```ql:metrics', JSON.stringify({ title: '行情概览', source, evidence: live ? 'measured' : undefined, items: [
        { label: '最新价', value: s.last, format: 'money' },
        { label: '区间涨跌', value: s.change, format: 'pct_signed', window: first + ' ~ ' + last },
      ] }), '```'].join('\n'),
      ['```ql:chart', JSON.stringify({ kind: 'area', height: 100, title: 'BTC/USDT 收盘价', x: data.map((b) => b.t), series: [{ name: '收盘价', data: data.map((b) => b.c) }], source }), '```'].join('\n'),
    ].join('\n\n')
  }
  if (id === 'validation') {
    return ['```ql:steps', JSON.stringify({ title: '上线验证路径', items: [
      { title: '数据体检', status: 'done', note: '缺口、重复、未收盘检查通过' },
      { title: '回测与成本压力', status: 'done', note: '同一账本，成本翻倍复核' },
      { title: '样本外检验', status: 'done', note: '紧封数据只使用一次' },
      { title: '纸面模拟', status: 'pending', note: '等待跑满观察期' },
      { title: '模拟盘对照', status: 'pending', note: '通过后仍需你确认晋级' },
    ], source: '平台验证记录 · 结构示意' }), '```'].join('\n')
  }
  return ['```ql:claims', JSON.stringify({ title: '结论与证据', claims: [
    { text: '团队结论引用平台运行记录', type: 'measured' },
    { text: '对机制的解释标注为推断', type: 'inferred' },
    { text: '外部研报只作为参考', type: 'external' },
  ] }), '```'].join('\n')
}

function CapabilityCatalog({ bars, barsLive }: { bars: Bars; barsLive: boolean }) {
  const [active, setActive] = useState(0)
  const c = CAPABILITY_CATEGORIES[active]
  const md = useMemo(() => categoryMarkdown(c.id, bars, barsLive), [c.id, bars, barsLive])
  const liveCategory = (c.id === 'research' || c.id === 'data') && barsLive
  const move = (dir: number) => setActive((n) => (n + dir + CAPABILITY_CATEGORIES.length) % CAPABILITY_CATEGORIES.length)
  return (
    <div className="lp-catalog">
      <div className="lp-catalog-tabs" role="tablist" aria-label="实际功能分类">
        {CAPABILITY_CATEGORIES.map((x, i) => (
          <button key={x.id} type="button" role="tab" aria-selected={i === active} className={i === active ? 'on' : ''}
            id={`lp-catalog-tab-${x.id}`} aria-controls={`lp-catalog-panel-${x.id}`} onClick={() => setActive(i)}>
            <span>{String(i + 1).padStart(2, '0')}</span><b>{x.label}</b><small>{x.hint}</small>
          </button>
        ))}
      </div>
      <article key={c.id} id={`lp-catalog-panel-${c.id}`} role="tabpanel" aria-labelledby={`lp-catalog-tab-${c.id}`} className="lp-catalog-page">
        <div className="lp-catalog-head">
          <div><span className="lp-catalog-kicker">{c.label}</span><h3>{c.title}</h3><p>{c.text}</p></div>
          <div className="lp-catalog-pager">
            <button type="button" aria-label="上一类功能" onClick={() => move(-1)}>←</button>
            <span><b>{String(active + 1).padStart(2, '0')}</b> / {String(CAPABILITY_CATEGORIES.length).padStart(2, '0')}</span>
            <button type="button" aria-label="下一类功能" onClick={() => move(1)}>→</button>
          </div>
        </div>
        <div className="lp-catalog-grid">
          {c.items.map((it, i) => (
            <article key={it.name} className="lp-catalog-item" style={{ animationDelay: `${i * 45}ms` }}>
              <span className="lp-catalog-icon" aria-hidden>{it.icon}</span>
              <div><b>{it.name}</b>{it.tag && <em>{it.tag}</em>}<p>{it.desc}</p></div>
            </article>
          ))}
        </div>
        <div className="lp-catalog-showcase">
          <div className="lp-catalog-showcase-head"><span>平台真实界面 · 内容块示例</span><i className={liveCategory ? 'live' : ''} /><em>{liveCategory ? '实时数据' : c.id === 'research' || c.id === 'data' ? '演示数据' : '结构示意'}</em></div>
          <div className="lp-catalog-blocks"><Markdown text={md} compact /></div>
        </div>
      </article>
    </div>
  )
}

const TRUST_LAYERS = [
  { no: '01', title: '数据层', text: '交易所行情 · 数据质量 · 本地缓存' },
  { no: '02', title: '执行层', text: 'ADR · worker 租约 · 检查点 · 模拟盘' },
  { no: '03', title: '证据层', text: '版本 · 指标 · 来源 · 审批 · 审计日志' },
]

const API_GROUPS: { name: string; items: [string, string][] }[] = [
  { name: '认证与工作区', items: [['POST /api/auth/login', '登录（Cookie 会话，可开启双因素）'], ['GET /api/workspace/usage', '工作区用量与上限']] },
  { name: '行情与数据', items: [['GET /api/market/bars', 'K 线：周期 100ms ~ 1d，只返回已收盘'], ['GET /api/market/quality', '数据质量：缺口、重复、未收盘'], ['POST /api/market/sync', '同步一段真实历史进本地缓存']] },
  { name: 'AI 研究室', items: [['POST /api/lab/projects', '新建对话（策略 / 因子 / 模型 / 组合 / 执行）'], ['POST /api/lab/projects/{id}/messages', '发送消息，Agent 带工具协作'], ['GET /api/lab/projects/{id}/events', '实时推送（SSE）：增量消息与状态']] },
  { name: '回测与研究', items: [['POST /api/research/backtests', '运行回测（成本、滑点、基准）'], ['GET /api/research/backtests/{id}', '完整回测记录：净值、成交、指标'], ['POST /api/missions', '团队讨论 / 流水线']] },
]

const SDK_SNIPPET = `PARAMS = {"fast": 20, "slow": 60}

def initialize(ctx):
    ctx.symbol = "BTC/USDT"

def handle_data(ctx, data):
    fast = data.ma(ctx.symbol, PARAMS["fast"])
    slow = data.ma(ctx.symbol, PARAMS["slow"])
    if fast > slow:
        order_target_percent(ctx.symbol, 1.0)
    else:
        order_target_percent(ctx.symbol, 0.0)`

function FeatureCarousel() {
  const [active, setActive] = useState(0)
  const [paused, setPaused] = useState(false)
  const [shift, setShift] = useState(0)
  const viewportRef = useRef<HTMLDivElement>(null)
  const reduce = useRef(false)
  useLayoutEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    const update = () => {
      const card = viewport.querySelectorAll<HTMLElement>('.lp-feature-stage')[active]
      if (!card) return
      setShift(viewport.clientWidth / 2 - (card.offsetLeft + card.offsetWidth / 2))
    }
    update()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update)
    observer?.observe(viewport)
    window.addEventListener('resize', update)
    return () => { observer?.disconnect(); window.removeEventListener('resize', update) }
  }, [active])
  useEffect(() => { reduce.current = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false }, [])
  useEffect(() => {
    if (paused || reduce.current) return
    const t = window.setInterval(() => setActive((n) => (n + 1) % FEATURES.length), 6200)
    return () => window.clearInterval(t)
  }, [paused])
  const move = (dir: number) => setActive((n) => (n + dir + FEATURES.length) % FEATURES.length)
  return (
    <div className="lp-feature-carousel" role="region" aria-roledescription="carousel" aria-label="平台核心能力"
      onMouseEnter={() => setPaused(true)} onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)} onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setPaused(false) }}
      onKeyDown={(e) => {
        if (e.key === 'ArrowLeft') { e.preventDefault(); move(-1) }
        if (e.key === 'ArrowRight') { e.preventDefault(); move(1) }
      }}>
      <div className="lp-feature-viewport" ref={viewportRef}>
        <div className="lp-feature-track" style={{ transform: `translate3d(${shift}px, 0, 0)` }}>
          {FEATURES.map((x, i) => (
            <article key={x.title} className={`lp-feature-stage${i === active ? ' active' : ''}`} aria-hidden={i === active ? undefined : true}>
              <div className="lp-feature-copy">
                <span className="lp-feature-tag">{x.tag}</span>
                <span className="lp-feature-icon" aria-hidden>{x.icon}</span>
                <h3>{x.title}</h3>
                <p>{x.text}</p>
                <ul>{x.points.map((point) => <li key={point}>{point}</li>)}</ul>
              </div>
              <div className="lp-feature-visual" aria-hidden>
                <span className="lp-feature-ghost">{String(i + 1).padStart(2, '0')}</span>
                <span className="lp-feature-core">{x.icon}</span>
                <div className="lp-feature-chips">{x.points.map((point, j) => <span key={point} style={{ animationDelay: `${j * 120}ms` }}>{point}</span>)}</div>
              </div>
            </article>
          ))}
        </div>
      </div>
      <div className="lp-feature-controls">
        <div className="lp-feature-dots" role="tablist" aria-label="选择能力卡片">
          {FEATURES.map((x, i) => (
            <button key={x.title} type="button" role="tab" aria-label={`第 ${i + 1} 张：${x.title}`} aria-selected={i === active}
              className={i === active ? 'on' : ''} onClick={() => setActive(i)} />
          ))}
        </div>
        <span className="lp-feature-count"><b>{String(active + 1).padStart(2, '0')}</b> / {String(FEATURES.length).padStart(2, '0')}</span>
        <div className="lp-feature-arrows">
          <button type="button" aria-label="上一张能力卡片" onClick={() => move(-1)}>←</button>
          <button type="button" aria-label="下一张能力卡片" onClick={() => move(1)}>→</button>
        </div>
      </div>
      <span className={`lp-feature-timer${paused ? ' paused' : ''}`} aria-hidden><i key={active} /></span>
    </div>
  )
}

function ScenarioExplorer({ bars, multi, barsLive, multiLive }: { bars: Bars; multi: Record<string, DirectBar[]>; barsLive: boolean; multiLive: boolean }) {
  const [active, setActive] = useState(0)
  const s = SCENARIOS[active]
  const market = useMemo(() => buildScenarioMarket(bars, multi), [bars, multi])
  const live = s.id === 'strategy' ? barsLive : multiLive
  const md = useMemo(() => scenarioMarkdown(s.id, market, live), [s.id, market, live])
  return (
    <div className="lp-scenario">
      <div className="lp-scenario-tabs" role="tablist" aria-label="平台研究场景">
        {SCENARIOS.map((x, i) => (
          <button key={x.id} type="button" role="tab" id={`lp-tab-${x.id}`} aria-selected={i === active} aria-controls={`lp-panel-${x.id}`}
            tabIndex={i === active ? 0 : -1} className={`lp-scenario-tab${i === active ? ' on' : ''}`}
            onClick={() => setActive(i)}
            onKeyDown={(e) => {
              if (e.key !== 'ArrowDown' && e.key !== 'ArrowRight' && e.key !== 'ArrowUp' && e.key !== 'ArrowLeft') return
              e.preventDefault()
              const dir = e.key === 'ArrowDown' || e.key === 'ArrowRight' ? 1 : -1
              const next = (active + dir + SCENARIOS.length) % SCENARIOS.length
              setActive(next)
              e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
            }}>
            <span className="lp-scenario-icon" aria-hidden>{x.icon}</span>
            <span><b>{x.label}</b><small>{x.hint}</small></span>
            <i>{String(i + 1).padStart(2, '0')}</i>
          </button>
        ))}
      </div>
      <article key={s.id} id={`lp-panel-${s.id}`} role="tabpanel" aria-labelledby={`lp-tab-${s.id}`} className="lp-scenario-stage">
        <div className="lp-scenario-copy">
          <span className="lp-scenario-kicker">{s.kicker}</span>
          <h3>{s.title}</h3>
          <p>{s.text}</p>
          <ol className="lp-scenario-steps">
            {s.steps.map((step, i) => <li key={step}><i>{i + 1}</i><span>{step}</span></li>)}
          </ol>
          <div className="lp-scenario-tags" aria-label="产出">
            {s.outputs.map((x) => <span key={x}>{x}</span>)}
          </div>
        </div>
        <div className="lp-scenario-demo">
          <div className="lp-scenario-demo-head">
            <span className="lp-board-dot" /> 平台真实内容块
            <em>{live ? '浏览器实时计算' : '演示数据'}</em>
          </div>
          <div className="lp-scenario-demo-body"><Markdown text={md} compact /></div>
          <small className="lp-scenario-demo-foot">与研究室内使用同一套内容块组件；示例计算不构成投资建议。</small>
        </div>
      </article>
    </div>
  )
}

/** 平台介绍页里的翻面内容：直接复用平台组件（目标卡、计划卡、对话、Markdown 内容块）。 */
const qlBlock = (kind: string, data: unknown) => '```ql:' + kind + '\n' + JSON.stringify(data) + '\n```'

const DEMO_PLAN: LabPlan = {
  task: '均线交叉 · 可复现研究',
  done_when: '样本外与成本压力均通过',
  steps: [
    { text: '同步 BTC/USDT 1h 行情', status: 'done', note: '168 根已收盘 K 线' },
    { text: '生成策略并运行回测', status: 'done', note: 'v3 · 成本与滑点入账' },
    { text: '成本压力与样本外检查', status: 'done', note: '同一账本复核' },
    { text: '整理报告与下一步建议', status: 'done', note: '证据链已归档' },
  ],
  status: 'done',
  autorun: true,
  turns: 4,
}

const FACTOR_MD = [
  qlBlock('metrics', {
    title: '因子评估 · 12h 动量', source: '平台因子记录 · 结构示意', evidence: 'measured',
    items: [
      { label: 'IC 均值', value: 0.041, format: 'ratio' },
      { label: 'ICIR', value: 0.62, format: 'ratio' },
      { label: '分组多空', value: 0.087, format: 'pct_signed' },
    ],
  }),
].join('\n\n')

const MODEL_MD = [
  qlBlock('metrics', {
    title: '模型卡 · 滚动验证', source: '平台模型记录 · 结构示意', evidence: 'measured',
    items: [
      { label: '样本外 IC', value: 0.038, format: 'ratio' },
      { label: '校准误差', value: 0.021, format: 'pct' },
      { label: '漂移告警', value: 0, format: 'int' },
    ],
  }),

].join('\n\n')

const PORTFOLIO_MD = [
  qlBlock('data_table', {
    title: '组合权重与风险贡献',
    columns: [
      { key: 'symbol', label: '标的' },
      { key: 'weight', label: '权重' },
      { key: 'risk', label: '风险贡献' },
      { key: 'turnover', label: '换手' },
    ],
    rows: [
      { symbol: 'BTC/USDT', weight: '35%', risk: '31%', turnover: '12%' },
      { symbol: 'ETH/USDT', weight: '30%', risk: '28%', turnover: '9%' },
      { symbol: 'SOL/USDT', weight: '20%', risk: '23%', turnover: '7%' },
      { symbol: 'BNB/USDT', weight: '15%', risk: '18%', turnover: '5%' },
    ],
    source: '平台组合快照 · 结构示意',
  }),
].join('\n\n')

const EXECUTION_MD = [
  qlBlock('status', {
    title: '执行仿真', state: 'ok', evidence: 'measured',
    detail: '拆单计划、滑点与模拟盘对照已写入同一份研究记录。',
    metrics: [
      { label: '执行缺口', value: 0.0018, format: 'pct' },
      { label: '平均滑点', value: 0.0011, format: 'pct' },
      { label: '成交笔数', value: 12, format: 'int' },
    ],
    source: '平台执行记录 · 结构示意',
  }),
].join('\n\n')

const QUALITY_MD = [
  qlBlock('status', {
    title: '数据质量', state: 'ok', evidence: 'measured',
    detail: '缺口、重复与未收盘检查通过，数据来源已记录。',
    metrics: [
      { label: '缺口', value: 0, format: 'int' },
      { label: '重复', value: 0, format: 'int' },
      { label: '未收盘', value: 0, format: 'int' },
    ],
    source: '平台数据质量记录 · 结构示意',
  }),
].join('\n\n')

const BACKTEST_MD = [
  qlBlock('metrics', {
    title: '回测复现 · 同一账本', source: '平台回测记录 · 结构示意', evidence: 'measured',
    items: [
      { label: '累计收益', value: 0.187, format: 'pct_signed' },
      { label: '最大回撤', value: 0.064, format: 'pct' },
      { label: 'Sharpe', value: 1.42, format: 'ratio' },
      { label: '成交', value: 28, format: 'int' },
    ],
  }),

].join('\n\n')

const STRESS_MD = [
  qlBlock('data_table', {
    title: '压力测试对照',
    columns: [
      { key: 'case', label: '场景' },
      { key: 'ret', label: '收益' },
      { key: 'sharpe', label: 'Sharpe' },
      { key: 'verdict', label: '结论' },
    ],
    rows: [
      { case: '基准回测', ret: '+18.7%', sharpe: '1.42', verdict: '通过' },
      { case: '成本翻倍', ret: '+13.2%', sharpe: '1.08', verdict: '通过' },
      { case: '区间扰动', ret: '+11.5%', sharpe: '0.96', verdict: '观察' },
      { case: '参数扰动', ret: '+9.8%', sharpe: '0.88', verdict: '观察' },
    ],
    source: '平台压力测试记录 · 结构示意',
  }),
].join('\n\n')

const OOS_MD = [
  qlBlock('steps', {
    title: '样本外检验', source: '平台验证记录 · 结构示意',
    items: [
      { title: '锁定最后一段数据', status: 'done', note: '只允许使用一次' },
      { title: '冻结参数与成本口径', status: 'done', note: '与开发区间一致' },
      { title: '运行并归档结果', status: 'done', note: '结果写入证据链' },
    ],
  }),
].join('\n\n')

const PAPER_MD = [
  qlBlock('steps', {
    title: '纸面模拟观察期', source: '平台纸面记录 · 结构示意',
    items: [
      { title: '按真实时钟推进', status: 'done', note: '不加速、不回填' },
      { title: '记录成交与持仓', status: 'running', note: '观察期进行中' },
      { title: '对比回测口径', status: 'pending', note: '跑满后自动复核' },
    ],
  }),

].join('\n\n')

const DEMO_ACCOUNT_MD = [
  qlBlock('status', {
    title: '模拟盘对照', state: 'pending', evidence: 'measured',
    detail: '执行、延迟与真实滑点已对照；通过后仍需工作区管理员确认。',
    metrics: [
      { label: '执行缺口', value: 0.0021, format: 'pct' },
      { label: '平均延迟', value: 84, format: 'int' },
      { label: '真实滑点', value: 0.0013, format: 'pct' },
    ],
    source: '平台模拟盘记录 · 结构示意',
  }),
].join('\n\n')

function PlatformCardPreview({ kind }: { kind: string }) {
  if (kind === 'goal') {
    return (
      <div className="lp-platform-preview"><GoalCardView
        className="lp-goal-preview"
        chips={[
          { text: '年化收益 ≥ 15%' },
          { text: '最大回撤 ≤ 10%' },
          { text: '换手 ≤ 40%', priority: 'try' },
        ]}
        rows={[
          { label: '标的', value: 'BTC/USDT · 1h' },
          { label: '区间', value: '最近 168 根 K 线' },
          { label: '轮数', value: '最多 4 轮' },
        ]}
      /></div>
    )
  }
  if (kind === 'plan') return <div className="lp-platform-preview"><PlanCard plan={DEMO_PLAN} live={null} /></div>
  if (kind === 'tool') {
    return (
      <div className="lp-platform-preview"><div className="lp-platform-chat">
        <ToolLine icon="↻" label="market.sync" args={{ symbol: 'BTC/USDT', timeframe: '1h' }} status="ok" elapsedMs={820} />
        <ToolLine icon="⌗" label="factor.evaluate" args={{ factor: '12h 动量' }} status="ok" elapsedMs={1410} />
        <AssistantText content={FACTOR_MD} model="researcher" footer={<span>因子记录已归档</span>} />
      </div></div>
    )
  }
  if (kind === 'model') return <div className="lp-platform-preview"><AssistantText content={MODEL_MD} model="researcher" footer={<span>模型卡已生成</span>} /></div>
  if (kind === 'portfolio') return <div className="lp-platform-preview"><Markdown text={PORTFOLIO_MD} compact /></div>
  if (kind === 'execution') {
    return (
      <div className="lp-platform-preview"><div className="lp-platform-chat">
        <ToolLine icon="↗" label="execution.simulate" args={{ orders: 12, venue: 'demo' }} status="ok" elapsedMs={2380} />
        <AssistantText content={EXECUTION_MD} model="executor" footer={<span>执行报告已归档</span>} />
      </div></div>
    )
  }
  if (kind === 'quality') return <div className="lp-platform-preview"><Markdown text={QUALITY_MD} compact /></div>
  if (kind === 'backtest') return <div className="lp-platform-preview"><Markdown text={BACKTEST_MD} compact /></div>
  if (kind === 'stress') return <div className="lp-platform-preview"><Markdown text={STRESS_MD} compact /></div>
  if (kind === 'oos') return <div className="lp-platform-preview"><Markdown text={OOS_MD} compact /></div>
  if (kind === 'paper') return <div className="lp-platform-preview"><Markdown text={PAPER_MD} compact /></div>
  if (kind === 'demo') return <div className="lp-platform-preview"><Markdown text={DEMO_ACCOUNT_MD} compact /></div>
  return null
}

function ResearchFlowDeck({ step }: { step: number }) {
  const [flipped, setFlipped] = useState<Set<number>>(() => new Set())
  const toggle = (i: number) => setFlipped((prev) => {
    const next = new Set(prev)
    if (next.has(i)) next.delete(i)
    else next.add(i)
    return next
  })
  return (
    <>
      <div className="lp-flow-grid">
        {FLOW_STEPS.map((s, i) => (
          <div key={s.title} className={`lp-flip-card${flipped.has(i) ? ' flipped' : ''}`} role="button" tabIndex={0} aria-pressed={flipped.has(i)}
            onClick={() => toggle(i)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(i) } }}>
            <div className="lp-flip-inner">
              <div className="lp-flip-face front" aria-hidden={flipped.has(i)}>
                <div className={`lp-flow-card${i === step ? ' on' : ''}`}>
                  <span className="lp-flow-index">{String(i + 1).padStart(2, '0')}</span>
                  <span className="lp-flow-icon" aria-hidden>{s.icon}</span>
                  <h3>{s.title}</h3>
                  <p>{s.desc}</p>
                  <div className="lp-flow-outputs">{s.outputs.map((x) => <span key={x}>{x}</span>)}</div>
                  <small className="lp-flip-hint">点击查看平台组件</small>
                </div>
              </div>
              <div className="lp-flip-face back" aria-hidden={!flipped.has(i)}>
                <span className="lp-flip-label">实际平台组件</span>
                <PlatformCardPreview kind={s.preview} />
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="lp-flow-deliverable" data-reveal>
        <div><b>一条完整证据链</b><span>代码版本 · 指标 · 图表 · 来源 · 审批记录 · PDF 报告</span></div>
        <div className="lp-flow-chain">{FLOW_STEPS.map((s, i) => <span key={s.title} className={i === step ? 'on' : ''}>{s.title}</span>)}</div>
      </div>
    </>
  )
}

function ValidationDeck() {
  const [flipped, setFlipped] = useState<Set<number>>(() => new Set())
  const toggle = (i: number) => setFlipped((prev) => {
    const next = new Set(prev)
    if (next.has(i)) next.delete(i)
    else next.add(i)
    return next
  })
  return (
    <>
      <div className="lp-gates-grid">
        {GATES.map((g, i) => (
          <div key={g.no} className={`lp-flip-card${flipped.has(i) ? ' flipped' : ''}`} role="button" tabIndex={0} aria-pressed={flipped.has(i)}
            onClick={() => toggle(i)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(i) } }}>
            <div className="lp-flip-inner">
              <div className="lp-flip-face front" aria-hidden={flipped.has(i)}>
                <div className="lp-gate-card">
                  <div className="lp-gate-top"><span>{g.no}</span><i aria-hidden>{g.icon}</i></div>
                  <h3>{g.title}</h3>
                  <p>{g.text}</p>
                  <small className="lp-flip-hint">点击查看实际记录</small>
                </div>
              </div>
              <div className="lp-flip-face back" aria-hidden={!flipped.has(i)}>
                <span className="lp-flip-label">平台记录预览</span>
                <PlatformCardPreview kind={g.preview} />
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="lp-validation-note" data-reveal>
        <span className="lp-validation-mark" aria-hidden>✓</span>
        <div><b>默认只读，晋级需要你确认</b><p>研究、回测和纸面模拟都在安全边界内运行；交易所模拟盘可随时对照，微实盘必须由你显式开启。</p></div>
        <div className="lp-validation-chips"><span>纸面模拟</span><span>交易所模拟盘</span><span>微实盘</span></div>
      </div>
      <div className="lp-validation-base" data-reveal>
        {TRUST_LAYERS.map((x) => (
          <div key={x.no} className="lp-validation-base-item"><span>{x.no}</span><b>{x.title}</b><small>{x.text}</small></div>
        ))}
      </div>
    </>
  )
}

export default function Landing({ auth, autoOpen }: { auth: (close: () => void) => ReactNode; autoOpen?: boolean }) {
  const [open, setOpen] = useState(false)
  const [about, setAbout] = useState(false)
  const [readmeOpen, setReadmeOpen] = useState(false)
  const [docsOpen, setDocsOpen] = useState(() => {
    try { return sessionStorage.getItem('qa.docsOverlay') === '1' } catch { return false }
  })
  useEffect(() => { try { sessionStorage.removeItem('qa.docsOverlay') } catch { /* private mode */ } }, [])
  useEffect(() => { document.title = t('QuantLab v0.1.0-preview.2') }, [])
  const [closing, setClosing] = useState(false)
  const { bars, state } = useLive('BTC/USDT', '1h', 168)
  const multi = useMultiLive('1h', 168)
  const marketLive = state === 'live' && LANDING_SYMBOLS.every((s) => (multi[s]?.length || 0) > 50)
  const heroBars = useMemo(() => (bars && state === 'live' ? bars.slice(-60) : sketchBars()), [bars, state])
  const [step, setStep] = useState(0)
  const [screen, setScreen] = useState(0)
  const [wheelDir, setWheelDir] = useState<'up' | 'down' | null>(null)
  const screenRef = useRef(0)
  const wheelAcc = useRef(0)
  const wheelLock = useRef(0)
  const wheelTimer = useRef(0)
  const navRef = useRef<HTMLElement>(null)
  const progressRef = useRef<HTMLElement>(null)
  const deckRef = useRef<HTMLDivElement>(null)
  /** 登录抽屉：根节点 / 遮罩 / 面板，用于滚动锁、inert 背景与焦点管理。 */
  const rootRef = useRef<HTMLDivElement>(null)
  const drawerWrapRef = useRef<HTMLDivElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  /** 打开抽屉前的触发元素，关闭后焦点回到它（可访问性）。 */
  const restoreFocusRef = useRef<HTMLElement | null>(null)
  /** 指针是否按在遮罩上：只有「按下 + 松开都在遮罩」才算点击遮罩，避免在输入框里拖选文本时误关抽屉。 */
  const backdropDownRef = useRef(false)
  useReveal()
  useEffect(() => {
    const deck = deckRef.current
    if (!deck) return
    let frame = 0
    const update = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const max = deck.scrollHeight - deck.clientHeight
        const ratio = max > 0 ? Math.min(1, Math.max(0, deck.scrollTop / max)) : 0
        if (progressRef.current) progressRef.current.style.transform = `scaleX(${ratio})`
        navRef.current?.classList.toggle('scrolled', deck.scrollTop > 8)
        const screens = [...deck.querySelectorAll<HTMLElement>('.lp-screen')]
        const deckTop = deck.getBoundingClientRect().top
        let index = 0
        let nearest = Infinity
        screens.forEach((el, i) => {
          const distance = Math.abs(el.getBoundingClientRect().top - deckTop)
          if (distance < nearest) { nearest = distance; index = i }
        })
        screenRef.current = index
        setScreen(index)
        screens.forEach((el, i) => el.classList.toggle('active', i === index))
      })
    }
    update()
    deck.addEventListener('scroll', update, { passive: true })
    window.addEventListener('resize', update)
    return () => {
      cancelAnimationFrame(frame)
      deck.removeEventListener('scroll', update)
      window.removeEventListener('resize', update)
    }
  }, [])
  useEffect(() => { if (autoOpen) setOpen(true) }, [autoOpen])
  useEffect(() => { const t = window.setInterval(() => setStep((s) => (s + 1) % FLOW_STEPS.length), 1600); return () => window.clearInterval(t) }, [])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (open) return
      const target = e.target as HTMLElement
      if (target.closest('input, textarea, select, [contenteditable], .lp-scenario-demo-body, .lp-catalog-blocks')) return
      if (e.key === 'ArrowDown' || e.key === 'PageDown') { e.preventDefault(); slideTo(screen + 1) }
      if (e.key === 'ArrowUp' || e.key === 'PageUp') { e.preventDefault(); slideTo(screen - 1) }
      if (e.key === 'Home') { e.preventDefault(); slideTo(0) }
      if (e.key === 'End') { e.preventDefault(); slideTo(SCREENS.length - 1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, screen])
  useEffect(() => {
    const deck = deckRef.current
    if (!deck || open || window.innerWidth <= 960) return
    const onWheel = (e: WheelEvent) => {
      if (window.innerWidth <= 960) return
      const target = e.target as HTMLElement
      const inner = target.closest<HTMLElement>('.lp-scenario-demo-body, .lp-catalog-blocks, .lp-api-code')
      if (inner) {
        const down = inner.scrollTop + inner.clientHeight < inner.scrollHeight - 1
        const up = inner.scrollTop > 0
        if ((e.deltaY > 0 && down) || (e.deltaY < 0 && up)) return
      }
      if (!e.deltaY) return
      e.preventDefault()
      const now = performance.now()
      if (now < wheelLock.current) return
      wheelAcc.current += e.deltaY
      if (Math.abs(wheelAcc.current) < 30) return
      const dir = wheelAcc.current > 0 ? 1 : -1
      const next = screenRef.current + dir
      wheelAcc.current = 0
      if (next < 0 || next >= SCREENS.length) return
      wheelLock.current = now + 820
      setWheelDir(dir > 0 ? 'down' : 'up')
      window.clearTimeout(wheelTimer.current)
      wheelTimer.current = window.setTimeout(() => setWheelDir(null), 520)
      slideTo(next)
    }
    deck.addEventListener('wheel', onWheel, { passive: false })
    return () => {
      deck.removeEventListener('wheel', onWheel)
      window.clearTimeout(wheelTimer.current)
    }
  }, [open])
  const close = () => { if (closing) return; setClosing(true); window.setTimeout(() => { setOpen(false); setClosing(false) }, 220) }
  /** 打开抽屉：记录触发元素（Safari 点击按钮不会自动 focus，必须用 currentTarget），关闭后焦点回到它。 */
  const openDrawer = (e?: { currentTarget: EventTarget | null }) => {
    const trigger = e?.currentTarget
    if (trigger instanceof HTMLElement) restoreFocusRef.current = trigger
    else {
      const active = document.activeElement
      restoreFocusRef.current = active instanceof HTMLElement && active !== document.body ? active : null
    }
    setOpen(true)
  }

  /**
   * 抽屉打开期间：锁背景滚动、把背景内容设为 inert（不可 tab）、焦点进入首个输入框；
   * 关闭 / 卸载时恢复滚动、恢复 inert、焦点回到触发按钮。
   * 注意：landing 的滚动发生在 .lp-deck 内部，锁 html/body 不会改动 deck 的 scrollTop；
   * 这里仍记录一次并在关闭时做兜底恢复，避免某些 WebView 在 body overflow 变化时重置内部滚动。
   */
  useEffect(() => {
    if (!open) return
    const root = rootRef.current
    const wrap = drawerWrapRef.current
    const drawer = drawerRef.current
    const deck = deckRef.current
    const html = document.documentElement
    const body = document.body
    // 触发元素已在 openDrawer 里记录；autoOpen（邀请链接/首次部署）时退化为「记录当前焦点」
    if (!restoreFocusRef.current) {
      const active = document.activeElement
      restoreFocusRef.current = active instanceof HTMLElement && active !== body && /^(BUTTON|A|INPUT|TEXTAREA|SELECT|SUMMARY)$/.test(active.tagName) ? active : null
    }
    const deckScroll = deck ? deck.scrollTop : 0
    const prevHtmlOverflow = html.style.overflow
    const prevBodyOverflow = body.style.overflow

    html.classList.add('lp-scroll-lock')
    body.classList.add('lp-scroll-lock')

    // inert（Chrome 102+/WebView2 支持；旧内核忽略该属性，由下面的 Tab 焦点环兜底）
    const inerted: HTMLElement[] = []
    if (root) {
      for (const child of Array.from(root.children)) {
        if (!(child instanceof HTMLElement) || child === wrap) continue
        if (!child.hasAttribute('inert')) { child.setAttribute('inert', ''); inerted.push(child) }
      }
    }

    // 焦点进入首个输入框（登录/注册/两步验证都适用）；没有输入框时落到抽屉本身
    const focusFrame = window.requestAnimationFrame(() => {
      const firstInput = drawer?.querySelector<HTMLElement>('input:not([type="hidden"]):not([disabled]), textarea:not([disabled])')
      ;(firstInput ?? drawer)?.focus({ preventScroll: false })
    })

    return () => {
      window.cancelAnimationFrame(focusFrame)
      html.classList.remove('lp-scroll-lock')
      body.classList.remove('lp-scroll-lock')
      html.style.overflow = prevHtmlOverflow
      body.style.overflow = prevBodyOverflow
      for (const el of inerted) el.removeAttribute('inert')
      if (deck && Math.abs(deck.scrollTop - deckScroll) > 1) {
        window.requestAnimationFrame(() => { deck.scrollTop = deckScroll })
      }
      const target = restoreFocusRef.current
      restoreFocusRef.current = null
      if (target && target.isConnected) target.focus({ preventScroll: true })
    }
  }, [open])

  /** Escape 关闭 + Tab 焦点环（inert 不支持时的兼容兜底）。 */
  useEffect(() => {
    if (!open) return
    const drawer = drawerRef.current
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        e.stopPropagation()
        if (!closing) close()
        return
      }
      if (e.key !== 'Tab' || !drawer) return
      const items = Array.from(drawer.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]'))
        .filter((el) => el.getClientRects().length > 0)
      if (!items.length) { e.preventDefault(); drawer.focus(); return }
      const first = items[0]
      const last = items[items.length - 1]
      const current = document.activeElement
      const inside = current instanceof Node && drawer.contains(current)
      if (e.shiftKey && (!inside || current === first || current === drawer)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && (!inside || current === last)) {
        e.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [open, closing])

  const slideTo = (index: number) => {
    const deck = deckRef.current
    if (!deck) return
    const next = Math.max(0, Math.min(SCREENS.length - 1, index))
    const target = deck.querySelectorAll<HTMLElement>('.lp-screen')[next]
    if (!target) return
    const top = target.getBoundingClientRect().top - deck.getBoundingClientRect().top + deck.scrollTop
    deck.scrollTo({ top, behavior: 'smooth' })
  }
  const go = (id: string) => {
    const index = SCREENS.findIndex((s) => s.id === id)
    if (index >= 0) slideTo(index)
  }

  return (
    <div className="lp" data-i18n ref={rootRef}>
      <nav ref={navRef} className="lp-nav">
        <i ref={progressRef} className="lp-progress" aria-hidden />
        <a className="lp-brand" href="#top" title="关于 QuantLab" aria-haspopup="dialog" onClick={(e) => { e.preventDefault(); setAbout(true) }}>
          <QuantBot size={26} state="idle" title="QuantLab" /> <span className="lp-brand-copy"><b>QuantLab</b><em>v0.1.0-preview.2</em></span>
        </a>
        <span className="lp-grow" />
        <button type="button" className="lp-link" onClick={() => go('features')}>能力</button>
        <button type="button" className="lp-link" onClick={() => go('scenarios')}>场景</button>
        <button type="button" className="lp-link" onClick={() => go('demo')}>演示</button>
        <button type="button" className="lp-link" onClick={() => go('validation')}>验证</button>
        <button type="button" className="lp-link" onClick={() => go('api')}>API</button>
        <div className="lp-lang" role="group" aria-label={t('界面语言')} data-no-i18n>
          <button type="button" aria-pressed={lang === 'zh'} className={lang === 'zh' ? 'on' : ''} title={t('简体中文')} onClick={() => lang !== 'zh' && setLang('zh')}>ZH</button>
          <button type="button" aria-pressed={lang === 'en'} className={lang === 'en' ? 'on' : ''} title="English" onClick={() => lang !== 'en' && setLang('en')}>EN</button>
        </div>
        <button type="button" className="button primary small" onClick={(e) => openDrawer(e)}>登录 / 注册</button>
      </nav>

      <div className="lp-deck" ref={deckRef}>
      <header className="lp-hero lp-screen" id="top">
        <div className="lp-hero-text">
          <h1>先在纸面上<em>证明</em>，<br />再谈真金白银。</h1>
          <p>和 QuantLab Bot 一起，从一句想法到可回测、可审计、可复现的策略。每个结论都能追溯到原始数据。</p>
          <div className="lp-cta">
            <button type="button" className="button primary" onClick={(e) => openDrawer(e)}>开始研究</button>
            <button type="button" className="button ghost" onClick={() => go('demo')}>看看它怎么工作 ↓</button>
          </div>
          <div className="lp-hero-metrics">
            {HERO_METRICS.map(([value, label]) => <div key={label}><b>{value}</b><span>{label}</span></div>)}
          </div>
          <ul className="lp-badges">
            <li>不下真实订单</li><li>工作区隔离</li><li>审计留痕</li><li>毫秒级回测</li>
          </ul>
        </div>
        <div className="lp-hero-art">
          <HeroBot />
          <HeroScene bars={heroBars} live={state === 'live'} />
        </div>
        <div className="lp-hero-strip">
          <div><b>研究</b><span>AI 研究室 · 策略 · 因子 · 模型</span></div>
          <div><b>验证</b><span>回测 · 压力 · 样本外 · 准入</span></div>
          <div><b>运行</b><span>纸面 · 模拟盘 · 微实盘</span></div>
          <div><b>证据</b><span>版本 · 来源 · 审批 · 审计</span></div>
        </div>
      </header>

      <section className="lp-section lp-screen" id="features">
        <h2 data-reveal>为严肃研究设计</h2>
        <p className="lp-sub" data-reveal>不是又一个聊天框：Agent 能用平台上的每一项功能，而你能检查它做的每一步。</p>
        <div data-reveal><FeatureCarousel /></div>
      </section>

      <section className="lp-section lp-scenarios lp-screen" id="scenarios">
        <h2 data-reveal>四间研究室，共用一条证据链</h2>
        <p className="lp-sub" data-reveal>从策略、因子到模型、组合与执行，每个场景都不是孤立工具，而是同一份研究记录里的连续步骤。</p>
        <div data-reveal><ScenarioExplorer bars={bars} multi={multi} barsLive={state === 'live'} multiLive={marketLive} /></div>
      </section>

      <section className="lp-section lp-demo lp-screen" id="demo">
        <h2 data-reveal>实际功能展示</h2>
        <p className="lp-sub" data-reveal>按研究、数据、验证与协作四类浏览平台能力；每一页都复用真实内容块与实时行情组件，并列出更多可用的工作入口。</p>
        <div data-reveal><CapabilityCatalog bars={bars} barsLive={state === 'live'} /></div>
      </section>

      <section className="lp-section lp-screen" id="pipeline">
        <h2 data-reveal>一次走完的研究流程</h2>
        <p className="lp-sub" data-reveal>点击任一阶段，翻面查看该阶段在平台里实际使用的计划、目标、工具和记录组件。</p>
        <ResearchFlowDeck step={step} />
      </section>

      <section className="lp-section lp-validation lp-screen" id="validation">
        <h2 data-reveal>上线之前，六道门都要过</h2>
        <p className="lp-sub" data-reveal>每张卡片翻面后显示对应的平台记录；每一步都能回到原始数据、成本和版本重新运行。</p>
        <ValidationDeck />
      </section>

      <section className="lp-section lp-api lp-screen" id="api">
        <h2 data-reveal>API 与 SDK</h2>
        <p className="lp-sub" data-reveal>所有功能都有 REST 接口。完整的策略 SDK 与接口说明在平台内 <button type="button" className="link" onClick={() => setDocsOpen(true)}>设置 → API 文档</button>；项目总览可查看 <button type="button" className="link" onClick={() => setReadmeOpen(true)}>README</button>。</p>
        <div className="lp-api-grid">
          <div className="lp-api-list" data-reveal>
            {API_GROUPS.map((g) => (
              <div key={g.name} className="lp-api-group">
                <h3>{g.name}</h3>
                <ul>{g.items.map(([ep, d]) => <li key={ep}><code>{ep}</code><span>{d}</span></li>)}</ul>
              </div>
            ))}
          </div>
          <div className="lp-api-code" data-reveal>
            <CodeBlock code={SDK_SNIPPET} info="python title=ma_cross.py" />
            <CodeBlock code={'curl -b cookies.txt "$HOST/api/market/bars?symbol=BTC/USDT&timeframe=250ms&limit=500"'} info="bash title=请求示例" />
          </div>
        </div>
      </section>

      <section className="lp-screen lp-final" id="closing">
        <div className="lp-closing" data-reveal>
          <span className="lp-closing-kicker">YOUR FIRST REPRODUCIBLE RESEARCH</span>
          <h2>把下一句想法，变成一条证据链。</h2>
          <p>从真实行情、策略代码到回测、验证和纸面模拟，QuantLab 把每一步都留在同一份研究记录里。</p>
          <div className="lp-cta lp-closing-cta">
            <button type="button" className="button primary" onClick={(e) => openDrawer(e)}>进入 QuantLab</button>
            <button type="button" className="button ghost" onClick={() => go('demo')}>再看一遍实际演示</button>
          </div>
          <div className="lp-closing-points"><span>不下真实订单</span><span>工作区隔离</span><span>结论可追溯</span><span>可私有部署</span></div>
        </div>
        <footer className="lp-foot">
          <div className="lp-foot-main">
            <span className="lp-foot-brand"><QuantBot size={18} state="idle" /> QuantLab v0.1.0-preview.2 · 私有部署 · 仅用于研究，不构成投资建议</span>
            <div className="lp-foot-links">
              <button type="button" className="lp-link" onClick={() => setDocsOpen(true)}>API 文档</button>
              <button type="button" className="lp-link" onClick={() => setReadmeOpen(true)}>项目 README</button>
              <button type="button" className="lp-link" onClick={(e) => openDrawer(e)}>登录 / 注册</button>
            </div>
          </div>
          <div className="lp-foot-dev">
            <span>开发者 / 联系方式</span>
            <a href="https://github.com/NagisaShip" target="_blank" rel="noreferrer">GitHub · NagisaShip</a>
            <a href="https://github.com/jamesusa99" target="_blank" rel="noreferrer">GitHub · jamesusa99</a>
            <a href="mailto:luoyangyuan110@gmail.com">luoyangyuan110@gmail.com</a>
          </div>
        </footer>
      </section>

      </div>

      <nav className="lp-screen-nav" aria-label={t('分屏位置')}>
        <div className="lp-screen-dots">
          {SCREENS.map((s, i) => (
            <button key={s.id} type="button" className={i === screen ? 'on' : ''} aria-label={`第 ${i + 1} 屏：${s.label}`} aria-current={i === screen ? 'page' : undefined} onClick={() => slideTo(i)}>
              <i />
            </button>
          ))}
        </div>
      </nav>

      <button type="button" className={`lp-screen-jump top${wheelDir === 'up' ? ' pulse' : ''}`} onClick={() => slideTo(screen - 1)} disabled={screen <= 0} aria-label="上一屏">
        <i aria-hidden>↑</i><span>上一屏</span>
      </button>
      <button type="button" className={`lp-screen-jump bottom${wheelDir === 'down' ? ' pulse' : ''}`} onClick={() => slideTo(screen + 1)} disabled={screen >= SCREENS.length - 1} aria-label="下一屏">
        <span>下一屏</span><i aria-hidden>↓</i>
      </button>

      {readmeOpen && <ReadmePage onClose={() => setReadmeOpen(false)} />}
      {docsOpen && (
        <section className="lp-readme lp-docs" role="dialog" aria-modal="true" aria-label={lang === 'zh' ? 'API 文档' : 'API Documentation'}>
          <header className="lp-readme-bar">
            <button type="button" className="lp-readme-back" onClick={() => setDocsOpen(false)}>{lang === 'zh' ? '← 返回介绍页' : '← Back to overview'}</button>
            <div className="lp-readme-title"><b>{lang === 'zh' ? 'API 文档' : 'API Documentation'}</b><span>{lang === 'zh' ? '设置 → API 文档 · 无需登录' : 'Settings → API Documentation · No login required'}</span></div>
            <div className="lp-readme-lang" role="group" aria-label={lang === 'zh' ? '文档语言' : 'Documentation language'}>
              <button type="button" aria-pressed={lang === 'zh'} className={lang === 'zh' ? 'on' : ''} onClick={() => { if (lang === 'zh') return; try { sessionStorage.setItem('qa.docsOverlay', '1') } catch { /* private mode */ } setLang('zh') }}>ZH</button>
              <button type="button" aria-pressed={lang === 'en'} className={lang === 'en' ? 'on' : ''} onClick={() => { if (lang === 'en') return; try { sessionStorage.setItem('qa.docsOverlay', '1') } catch { /* private mode */ } setLang('en') }}>EN</button>
            </div>
          </header>
          <div className="lp-readme-scroll">
            <ApiDocsPage />
          </div>
        </section>
      )}

      {open && (
        <div
          ref={drawerWrapRef}
          className={`lp-drawer-wrap${closing ? ' closing' : ''}`}
          onPointerDown={(e) => { backdropDownRef.current = e.target === e.currentTarget }}
          onClick={(e) => {
            // 只有真正点在遮罩上才关闭；点在抽屉/输入框里（包括在输入框拖选文本后松开）绝不关闭
            const onBackdrop = e.target === e.currentTarget && backdropDownRef.current
            backdropDownRef.current = false
            if (onBackdrop) close()
          }}
        >
          <aside
            ref={drawerRef}
            className="lp-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="登录或注册"
            tabIndex={-1}
            onClick={(e) => e.stopPropagation()}
          >
            <button type="button" className="lp-drawer-x" onClick={close} aria-label="关闭">✕</button>
            <div className="lp-drawer-scroll">
              <div className="lp-drawer-inner">
                <div className="lp-drawer-bot"><QuantBot size={56} state="happy" title="QuantLab Bot" /></div>
                {auth(close)}
              </div>
            </div>
          </aside>
        </div>
      )}

      <AboutDialog open={about} onClose={() => setAbout(false)} />
    </div>
  )
}
