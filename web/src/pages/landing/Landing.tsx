import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { QuantBot, useBotFlash, type BotState } from '../../components/QuantBot'
import { Markdown } from '../../components/markdown'
import { CodeBlock } from '../../components/blocks/CodeBlock'
import { directBars, type DirectBar } from '../../marketDirect'
import '../../stylesLanding.css'

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

/** 取不到真实行情时的示意 K 线（只用于画面，页面上明确标注「示意」）。 */
function sketchBars(n = 60): DirectBar[] {
  const out: DirectBar[] = []
  let p = 100
  for (let i = 0; i < n; i++) {
    const o = p
    const c = o * (1 + Math.sin(i / 4.3) * 0.012 + Math.cos(i / 2.1) * 0.006)
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

// ---------------------------------------------------------------- 实际功能展示：AI 对话（真实组件 + 实时数据）
function ChatDemo({ bars, live }: { bars: Bars; live: boolean }) {
  const md = useMemo(() => {
    if (!bars || !live) {
      return '暂时连不上交易所行情，这里不展示任何数字。登录后，AI 研究室会先同步真实行情，再给出回测与指标；缺数据时如实说「未接入数据」。'
    }
    const s = stats(bars)
    const first = new Date(bars[0].t).toLocaleDateString('zh-CN'), last = new Date(bars[bars.length - 1].t).toLocaleDateString('zh-CN')
    const metrics = {
      items: [
        { label: '区间涨跌', value: s.change, format: 'pct_signed', evidence: 'measured', window: `${first} ~ ${last}` },
        { label: '年化波动', value: s.vol, format: 'pct', definition: '小时收益率标准差 × √8760' },
        { label: '最大回撤', value: s.mdd, format: 'pct', definition: '持有期内从高点到低点的最大跌幅' },
        { label: '上涨小时占比', value: s.up, format: 'pct' },
      ],
      source: 'Binance 公共行情 · 浏览器实时获取',
    }
    const chart = { kind: 'area', title: 'BTC/USDT 收盘价（近 7 天）', x: bars.map((b) => b.t), series: [{ name: '收盘价', data: bars.map((b) => b.c) }], height: 180 }
    return [
      `近 7 天 BTC 区间${s.change >= 0 ? '上涨' : '下跌'} **${(Math.abs(s.change) * 100).toFixed(2)}%**，最大回撤 ${(s.mdd * 100).toFixed(2)}% [1]。下面的数字都由行情直接计算（实测），不是策略回测：`,
      '```ql:metrics', JSON.stringify(metrics), '```',
      '```ql:chart', JSON.stringify(chart), '```',
      '想把它变成策略，我会先列计划，再回测、做 2 倍成本压力测试和样本外检验，达标前不停。',
      '```ql:citation', JSON.stringify({ sources: [{ id: 1, title: 'Binance 现货 1 小时 K 线', url: 'https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=168', kind: 'data' }] }), '```',
    ].join('\n')
  }, [bars, live])
  const [shown, setShown] = useState(false)
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) setShown(true) }, { threshold: 0.3 })
    if (box.current) io.observe(box.current)
    return () => io.disconnect()
  }, [])
  return (
    <div className="lp-chat" ref={box}>
      <div className="lp-msg user"><span>最近一周 BTC 走势怎么样？帮我看看能不能做个策略</span></div>
      {shown ? (
        <div className="lp-msg ai anim-rise">
          <div className="lp-ai-head"><QuantBot size={22} state="happy" /> QuantLab Bot</div>
          <Markdown text={md} compact />
        </div>
      ) : <div className="lp-msg ai typing"><QuantBot size={22} state="thinking" /> 正在思考…</div>}
    </div>
  )
}

// ---------------------------------------------------------------- 相关性热力图（实时）
function CorrDemo() {
  const syms = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
  const [md, setMd] = useState('')
  useEffect(() => {
    Promise.all(syms.map((s) => directBars(s, '1h', 168))).then((all) => {
      if (all.some((b) => !b || b.length < 50)) { setMd(''); return }
      const n = Math.min(...all.map((b) => b!.length))
      const rets = all.map((b) => { const c = b!.slice(-n).map((x) => x.c); return c.slice(1).map((v, i) => v / c[i] - 1) })
      const corr = rets.map((a) => rets.map((b) => {
        const ma = a.reduce((s, x) => s + x, 0) / a.length, mb = b.reduce((s, x) => s + x, 0) / b.length
        let num = 0, da = 0, db = 0
        for (let i = 0; i < a.length; i++) { num += (a[i] - ma) * (b[i] - mb); da += (a[i] - ma) ** 2; db += (b[i] - mb) ** 2 }
        return +(num / Math.sqrt(da * db || 1)).toFixed(2)
      }))
      setMd(['```ql:chart', JSON.stringify({ kind: 'heatmap', title: '主流币小时收益相关性（近 7 天，实时）', x: syms.map((s) => s.split('/')[0]), z: corr, source: 'Binance 公共行情' }), '```'].join('\n'))
    })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return md ? <Markdown text={md} compact /> : <div className="lp-placeholder">相关性需要实时行情，暂未连上</div>
}

// ---------------------------------------------------------------- 页面
const FEATURES: { icon: string; title: string; text: string }[] = [
  { icon: '◆', title: 'AI 研究室', text: '用中文描述想法，Agent 自己选工具：同步行情、写策略、回测、压力测试、样本外检验，列计划并一步步做完。' },
  { icon: '▤', title: '可追溯的结论', text: '回答里的每个数字都来自工具结果，区分「实测 / 推断 / 外部来源」，引用可点击，结论能一路追到原始记录。' },
  { icon: '◎', title: '团队评审', text: '市场、成本执行、审计等角色并行发言、互相质疑，研究组长按证据质量汇总，不按人数投票。' },
  { icon: '⧉', title: '跨对话协作', text: 'Agent 能查找、阅读、调起你的其他对话，把子问题拆出去并行研究，再把结果带回来。' },
  { icon: '≋', title: '从毫秒到日线', text: '100ms、1s 到日线任意周期：亚秒级由逐笔成交聚合，回测、模拟、实盘对照用同一套 Decimal 账本。' },
  { icon: '⛨', title: '安全边界', text: 'Python 策略在隔离进程中运行；工作区隔离、审计留痕；平台没有真实下单路径，实盘只读对照。' },
]

const PIPELINE = ['选标的', '因子', '策略', '模型', '组合', '执行']

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

export default function Landing({ auth, autoOpen }: { auth: (close: () => void) => ReactNode; autoOpen?: boolean }) {
  const [open, setOpen] = useState(false)
  const [closing, setClosing] = useState(false)
  const { bars, state } = useLive('BTC/USDT', '1h', 168)
  const heroBars = useMemo(() => (bars && state === 'live' ? bars.slice(-60) : sketchBars()), [bars, state])
  const [step, setStep] = useState(0)
  useReveal()
  useEffect(() => { if (autoOpen) setOpen(true) }, [autoOpen])
  useEffect(() => { const t = window.setInterval(() => setStep((s) => (s + 1) % PIPELINE.length), 1600); return () => window.clearInterval(t) }, [])
  const close = () => { setClosing(true); window.setTimeout(() => { setOpen(false); setClosing(false) }, 220) }
  const go = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  return (
    <div className="lp">
      <nav className="lp-nav">
        <a className="lp-brand" href="#top" onClick={(e) => { e.preventDefault(); window.scrollTo({ top: 0, behavior: 'smooth' }) }}>
          <QuantBot size={26} state="idle" title="QuantLab" /> <b>QuantLab</b>
        </a>
        <span className="lp-grow" />
        <button type="button" className="lp-link" onClick={() => go('features')}>特性</button>
        <button type="button" className="lp-link" onClick={() => go('demo')}>实际演示</button>
        <button type="button" className="lp-link" onClick={() => go('pipeline')}>研究流程</button>
        <button type="button" className="lp-link" onClick={() => go('api')}>API</button>
        <button type="button" className="button primary small" onClick={() => setOpen(true)}>登录 / 注册</button>
      </nav>

      <header className="lp-hero" id="top">
        <div className="lp-hero-text">
          <span className="lp-kicker">加密货币量化研究平台</span>
          <h1>先在纸面上<em>证明</em>，<br />再谈真金白银。</h1>
          <p>和 QuantLab Bot 一起，从一句想法到可回测、可审计、可复现的策略。每个结论都能追溯到原始数据。</p>
          <div className="lp-cta">
            <button type="button" className="button primary" onClick={() => setOpen(true)}>开始研究</button>
            <button type="button" className="button ghost" onClick={() => go('demo')}>看看它怎么工作 ↓</button>
          </div>
          <ul className="lp-badges">
            <li>不下真实订单</li><li>工作区隔离</li><li>审计留痕</li><li>毫秒级回测</li>
          </ul>
        </div>
        <div className="lp-hero-art">
          <HeroBot />
          <HeroScene bars={heroBars} live={state === 'live'} />
        </div>
      </header>

      <section className="lp-section" id="features">
        <h2 data-reveal>为严肃研究设计</h2>
        <p className="lp-sub" data-reveal>不是又一个聊天框：Agent 能用平台上的每一项功能，而你能检查它做的每一步。</p>
        <div className="lp-features">
          {FEATURES.map((f, i) => (
            <article key={f.title} className="lp-feature" data-reveal style={{ transitionDelay: `${i * 70}ms` }}>
              <span className="lp-feature-icon" aria-hidden>{f.icon}</span>
              <h3>{f.title}</h3>
              <p>{f.text}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="lp-section lp-demo" id="demo">
        <h2 data-reveal>实际功能展示</h2>
        <p className="lp-sub" data-reveal>下面用的就是平台里的真实组件；数据由你的浏览器直接从交易所公共接口实时获取。</p>
        <div className="lp-demo-grid">
          <div data-reveal><ChatDemo bars={bars} live={state === 'live'} /></div>
          <div className="lp-demo-side" data-reveal>
            <CorrDemo />
            <div className="lp-mini">
              <h3>结论分得清</h3>
              <Markdown compact text={['```ql:claims', JSON.stringify({ claims: [
                { text: '回测与样本外数字来自平台运行记录', type: 'measured' },
                { text: '「为什么有效」的解释标成推断，等数据验证', type: 'inferred' },
                { text: '新闻与研报只作参考，注明来源', type: 'external' },
              ] }), '```'].join('\n')} />
            </div>
          </div>
        </div>
      </section>

      <section className="lp-section" id="pipeline">
        <h2 data-reveal>一次走完的研究流程</h2>
        <p className="lp-sub" data-reveal>选「完整流程」，Agent 依次把每一阶段交给对应的研究室真正执行，最后出一份可导出 PDF 的报告。</p>
        <ol className="lp-pipe" data-reveal>
          {PIPELINE.map((p, i) => (
            <li key={p} className={i === step ? 'on' : i < step ? 'done' : ''}>
              <span>{i + 1}</span><b>{p}</b>
            </li>
          ))}
        </ol>
      </section>

      <section className="lp-section lp-trust">
        <div className="lp-trust-grid">
          {[['Decimal 复式账本', '每一分钱都能逐笔核对；回测、模拟、实盘同一口径'], ['独立 worker 执行', '租约、检查点、断点恢复，重启不重复买卖'], ['服务端数据库缓存', '同样的行情与搜索不重复请求外部接口'], ['浏览器直连行情', '展示用行情由你的浏览器取，服务器 IP 不被限流']].map(([t, d]) => (
            <div key={t} data-reveal><b>{t}</b><span>{d}</span></div>
          ))}
        </div>
      </section>

      <section className="lp-section lp-api" id="api">
        <h2 data-reveal>API 与 SDK</h2>
        <p className="lp-sub" data-reveal>所有功能都有 REST 接口；完整说明见 <a href="/docs" target="_blank" rel="noreferrer">/docs</a>（OpenAPI：<a href="/openapi.json" target="_blank" rel="noreferrer">/openapi.json</a>）。</p>
        <div className="lp-api-grid">
          <div className="lp-api-list" data-reveal>
            {API_GROUPS.map((g) => (
              <div key={g.name} className="lp-api-group">
                <h3>{g.name}</h3>
                <ul>{g.items.map(([ep, d]) => <li key={ep}><code>{ep}</code><span>{d}</span></li>)}</ul>
              </div>
            ))}
          </div>
          <div data-reveal>
            <CodeBlock code={SDK_SNIPPET} info="python title=ma_cross.py" />
            <CodeBlock code={'curl -b cookies.txt "$HOST/api/market/bars?symbol=BTC/USDT&timeframe=250ms&limit=500"'} info="bash title=请求示例" />
          </div>
        </div>
      </section>

      <footer className="lp-foot">
        <span><QuantBot size={18} state="idle" /> QuantLab · 私有部署 · 仅用于研究，不构成投资建议</span>
        <button type="button" className="lp-link" onClick={() => setOpen(true)}>登录 / 注册</button>
      </footer>

      {open && (
        <div className={`lp-drawer-wrap${closing ? ' closing' : ''}`} onClick={(e) => { if (e.target === e.currentTarget) close() }}>
          <aside className="lp-drawer" role="dialog" aria-modal="true" aria-label="登录或注册">
            <button type="button" className="lp-drawer-x" onClick={close} aria-label="关闭">✕</button>
            <div className="lp-drawer-inner">
              <div className="lp-drawer-bot"><QuantBot size={56} state="happy" title="QuantLab Bot" /></div>
              {auth(close)}
            </div>
          </aside>
        </div>
      )}
    </div>
  )
}
