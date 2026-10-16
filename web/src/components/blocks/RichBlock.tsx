import { Fragment, createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { get } from '../../http'
import { v3, type BacktestResult } from '../../api2'
import { TF_MS } from '../../timeframes'
import KlineChart, { type KBar, type KMark } from '../KlineChart'
import { Chart, HeatmapChart, type ChartSpec } from './Chart'
import { CodeBlock, RunResult } from './CodeBlock'
import { DataTable, tableFromBlock } from './DataTable'
import { ImageView } from './Media'
import {
  BLOCK_LABEL, askChat, copyText, downloadBlob, fmtTs, fmtValue, hostOf, safeHref, sourceText,
  type BlockBase, type Evidence, type ParsedBlock,
} from './protocol'

/** 一条消息里的引用来源：正文里的 [1]、[2] 通过它变成可点的引用标记。 */
export interface CiteSource { id: number | string; title?: string; url?: string; snippet?: string; kind?: string; ref?: string; publisher?: string; date?: string }
export const CitationCtx = createContext<{ sources: CiteSource[]; anchor: string } | null>(null)

export function collectCitations(text: string): CiteSource[] {
  const out: CiteSource[] = []
  const re = /```\s*ql:citation\s*\n([\s\S]*?)```/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    try {
      const d = JSON.parse(m[1].replace(/,\s*([}\]])/g, '$1'))
      const list = Array.isArray(d) ? d : d.sources
      if (Array.isArray(list)) list.forEach((s: CiteSource, i: number) => out.push({ ...s, id: s.id ?? out.length + i + 1 }))
    } catch { /* 流式未写完 */ }
  }
  return out
}

/** 正文里的引用标记 [n]：悬停显示来源摘要，点击跳到来源列表或打开网页。 */
export function CiteMark({ n }: { n: string }) {
  const ctx = useContext(CitationCtx)
  const src = ctx?.sources.find((s) => String(s.id) === n)
  const [open, setOpen] = useState(false)
  if (!src) return <>[{n}]</>
  const href = safeHref(src.url) || safeHref(src.ref)
  return (
    <span className="qb-cite-wrap" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <a className="qb-cite" href={href || `#${ctx!.anchor}-src-${n}`} target={href && !href.startsWith('#') ? '_blank' : undefined} rel="noreferrer noopener"
        onClick={(e) => { if (!href) { e.preventDefault(); document.getElementById(`${ctx!.anchor}-src-${n}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }) } }}>
        {n}
      </a>
      {open && (
        <span className="qb-cite-pop anim-pop" role="tooltip">
          <b>{src.title || (src.url ? hostOf(src.url) : `来源 ${n}`)}</b>
          {src.snippet && <span>{src.snippet.slice(0, 220)}</span>}
          <small>{[kindLabel(src.kind), src.publisher, src.date, src.url && hostOf(src.url)].filter(Boolean).join(' · ')}</small>
        </span>
      )}
    </span>
  )
}

const KIND_LABEL: Record<string, string> = { web: '网页', file: '文件', run: '运行记录', data: '平台数据', backtest: '回测', paper: '论文', doc: '文档' }
const kindLabel = (k?: string) => (k ? KIND_LABEL[k] || k : '')

const EVIDENCE: Record<Evidence, { label: string; tone: string; tip: string }> = {
  measured: { label: '实测', tone: 'ok', tip: '由平台回测 / 计算直接得出，可复现' },
  inferred: { label: '推断', tone: 'warn', tip: 'Agent 的推理或解释，尚未被数据验证' },
  external: { label: '外部来源', tone: 'info', tip: '来自外部资料，可信度取决于来源' },
}

export function EvidenceTag({ e }: { e?: Evidence | string }) {
  const x = e && EVIDENCE[e as Evidence]
  if (!x) return null
  return <span className={`qb-ev ${x.tone}`} title={x.tip}>{x.label}</span>
}

function SourceLine({ source }: { source: BlockBase['source'] }) {
  const s = sourceText(source)
  if (!s) return null
  const href = safeHref(s.href)
  return (
    <div className="qb-source">
      数据来源：{href ? <a href={href} target={href.startsWith('#') ? undefined : '_blank'} rel="noreferrer noopener">{s.label}</a> : s.label}
    </div>
  )
}

function Frame({ type, data, children, actions, wide }: { type: string; data: BlockBase; children: ReactNode; actions?: ReactNode; wide?: boolean }) {
  return (
    <section className={`qb-block qb-t-${type}${wide ? ' wide' : ''} anim-rise`}>
      {(data.title || actions || data.evidence) && (
        <header className="qb-head">
          <span className="qb-kind">{BLOCK_LABEL[type as keyof typeof BLOCK_LABEL] || type}</span>
          {data.title && <b className="qb-title">{data.title}</b>}
          <EvidenceTag e={data.evidence} />
          <span className="qb-grow" />
          {actions}
        </header>
      )}
      {children}
      {data.note && <p className="qb-note">{data.note}</p>}
      <SourceLine source={data.source} />
    </section>
  )
}

// ---------------------------------------------------------------- 入口

export function RichBlock({ block, streaming }: { block: ParsedBlock; streaming?: boolean }) {
  if ('error' in block) {
    if (block.partial || streaming) return <div className="qb-block qb-pending"><span className="cb-spin" /> 正在生成{BLOCK_LABEL[block.type] || '内容块'}…</div>
    return (
      <div className="qb-block qb-bad">
        <div className="qb-muted">「{BLOCK_LABEL[block.type] || block.type}」内容块解析失败：{block.error}</div>
        <CodeBlock code={block.raw} info="json" />
      </div>
    )
  }
  const d = block.data as Record<string, unknown> & BlockBase
  try {
    switch (block.type) {
      case 'chart': return <Frame type="chart" data={d}><Chart spec={d as ChartSpec} /></Frame>
      case 'data_table': {
        const t = tableFromBlock(d)
        return <Frame type="data_table" data={{ ...d, title: undefined }}><DataTable columns={t.columns} rows={t.rows} title={d.title} /></Frame>
      }
      case 'metrics': return <Frame type="metrics" data={d}><MetricGrid items={(d.items as MetricItem[]) || []} /></Frame>
      case 'backtest_report': return <BacktestReport data={d} />
      case 'kline_review': return <KlineReview data={d} />
      case 'factor_report': return <FactorBlock data={d} />
      case 'compare': return <CompareBlock data={d} />
      case 'citation': return <CitationList data={d} />
      case 'claims': return <ClaimsBlock data={d} />
      case 'file': return <FileBlock data={d} />
      case 'status': return <StatusBlock data={d} />
      case 'steps': return <StepsBlock data={d} />
      case 'callout': return (
        <div className={`qb-callout tone-${String(d.tone || 'info')} anim-rise`}>
          {d.title && <b>{d.title}</b>}
          <div>{String(d.text ?? d.content ?? '')}</div>
        </div>
      )
      case 'choices': return <ChoicesBlock data={d} />
      case 'form': return <FormBlock data={d} />
      case 'confirm': return <ConfirmBlock data={d} />
      case 'run': return <RunResult data={d} />
    }
  } catch (e) {
    return <div className="qb-block qb-bad qb-muted">渲染失败：{(e as Error).message}</div>
  }
  return null
}

// ---------------------------------------------------------------- 指标卡

export interface MetricItem { key?: string; label?: string; value: unknown; format?: string; digits?: number; good?: 'high' | 'low'; target?: number; definition?: string; window?: string; source?: BlockBase['source']; evidence?: Evidence; delta?: number }

/** 常用指标的定义：卡片上的「?」展开定义、计算区间与数据来源。 */
export const METRIC_DEFS: Record<string, { label: string; format: string; good: 'high' | 'low'; def: string }> = {
  total_return: { label: '总收益率', format: 'pct_signed', good: 'high', def: '期末权益 / 期初权益 − 1，含手续费与滑点' },
  annualized_return: { label: '年化收益', format: 'pct_signed', good: 'high', def: '按回测区间长度把总收益折算到一年（复利）' },
  sharpe: { label: 'Sharpe', format: 'ratio', good: 'high', def: '超额收益均值 / 收益标准差 × √年化周期数；无风险利率按 0' },
  sortino: { label: 'Sortino', format: 'ratio', good: 'high', def: '与 Sharpe 相同，但分母只计下行波动' },
  max_drawdown: { label: '最大回撤', format: 'pct', good: 'low', def: '净值从历史高点到随后低点的最大跌幅' },
  win_rate: { label: '胜率', format: 'pct', good: 'high', def: '盈利的完整交易笔数 / 完整交易总笔数' },
  trades: { label: '交易次数', format: 'int', good: 'high', def: '完整交易（开仓到平仓）笔数' },
  total_fees: { label: '手续费', format: 'money', good: 'low', def: '所有成交的手续费合计（计价货币）' },
  profit_factor: { label: '盈亏因子', format: 'ratio', good: 'high', def: '盈利交易总额 / 亏损交易总额' },
  calmar: { label: 'Calmar', format: 'ratio', good: 'high', def: '年化收益 / 最大回撤' },
  volatility: { label: '波动率', format: 'pct', good: 'low', def: '收益率标准差（年化）' },
  excess_return: { label: '超额收益', format: 'pct_signed', good: 'high', def: '策略收益 − 基准收益' },
  turnover: { label: '换手率', format: 'ratio', good: 'low', def: '累计成交额 / 平均权益' },
  ic: { label: 'IC 均值', format: 'ratio', good: 'high', def: '因子值与下一期收益的截面秩相关系数的均值' },
  icir: { label: 'ICIR', format: 'ratio', good: 'high', def: 'IC 均值 / IC 标准差' },
}

function MetricCardX({ m }: { m: MetricItem }) {
  const [open, setOpen] = useState(false)
  const def = m.key ? METRIC_DEFS[m.key] : undefined
  const label = m.label || def?.label || m.key || ''
  const format = m.format || def?.format
  const good = m.good || def?.good
  const n = typeof m.value === 'number' ? m.value : Number(m.value)
  const passed = m.target !== undefined && Number.isFinite(n) ? (good === 'low' ? n <= m.target : n >= m.target) : null
  const tone = passed === false ? 'bad' : passed ? 'good' : format?.startsWith('pct_signed') && Number.isFinite(n) ? (n > 0 ? 'up' : n < 0 ? 'down' : '') : ''
  const src = sourceText(m.source)
  const info = m.definition || def?.def || m.window || src
  return (
    <div className={`qb-metric ${tone}`}>
      <div className="qb-metric-label">
        <span>{label}</span>
        <EvidenceTag e={m.evidence} />
        {info && <button type="button" className="qb-q" aria-expanded={open} onClick={() => setOpen(!open)} title="定义、区间与来源">?</button>}
      </div>
      <div className="qb-metric-value"><CountUp value={m.value} fmt={(v) => fmtValue(v, format, m.digits)} /></div>
      {(m.target !== undefined || m.delta !== undefined) && (
        <div className="qb-metric-sub">
          {m.target !== undefined && <span>目标 {good === 'low' ? '≤' : '≥'} {fmtValue(m.target, format, m.digits)} {passed ? '· 达标' : '· 未达标'}</span>}
          {m.delta !== undefined && <span className={m.delta > 0 ? 'up' : m.delta < 0 ? 'down' : ''}>{m.delta > 0 ? '+' : ''}{fmtValue(m.delta, format === 'int' ? 'int' : format, m.digits)}</span>}
        </div>
      )}
      {open && (
        <div className="qb-metric-def anim-expand">
          {(m.definition || def?.def) && <div><b>定义</b>{m.definition || def?.def}</div>}
          {m.window && <div><b>区间</b>{m.window}</div>}
          {src && <div><b>来源</b>{safeHref(src.href) ? <a href={src.href}>{src.label}</a> : src.label}</div>}
        </div>
      )}
    </div>
  )
}

/** 数字从 0 滚到目标值（约 0.7 秒，缓出）；非数字或「减少动态效果」时直接显示。 */
function CountUp({ value, fmt }: { value: unknown; fmt: (v: unknown) => string }) {
  const n = typeof value === 'number' ? value : Number(value)
  const [shown, setShown] = useState<number | null>(Number.isFinite(n) ? 0 : null)
  useEffect(() => {
    if (!Number.isFinite(n)) { setShown(null); return }
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) { setShown(n); return }
    const t0 = performance.now()
    let raf = 0
    const tick = (t: number) => {
      const k = Math.min(1, (t - t0) / 700)
      setShown(n * (1 - Math.pow(1 - k, 3)))
      if (k < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [n])
  return <>{shown === null ? fmt(value) : fmt(shown === n ? value : shown)}</>
}

export function MetricGrid({ items }: { items: MetricItem[] }) {
  return <div className="qb-metrics stagger">{items.map((m, i) => <MetricCardX key={m.key || m.label || i} m={m} />)}</div>
}

// ---------------------------------------------------------------- 回测报告

const cache = new Map<string, Promise<BacktestResult>>()
function loadBacktest(id: string): Promise<BacktestResult> {
  if (!cache.has(id)) cache.set(id, v3.backtest(id).catch((e) => { cache.delete(id); throw e }))
  return cache.get(id)!
}

function useBacktest(id: string | undefined) {
  const [bt, setBt] = useState<BacktestResult | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    if (!id) return
    let alive = true
    loadBacktest(id).then((b) => alive && setBt(b), (e) => alive && setErr((e as Error).message))
    return () => { alive = false }
  }, [id])
  return { bt, err }
}

function windowOf(bt: BacktestResult): string {
  const ts = bt.timestamps
  return ts?.length ? `${fmtTs(ts[0], false)} ~ ${fmtTs(ts[ts.length - 1], false)}（${ts.length} 根）` : ''
}

function datasetLabel(bt: BacktestResult): string {
  const d = bt.datasets?.[0]
  return d ? `${d.source_label || d.source}${d.exchange ? ` · ${d.exchange}` : ''} · ${bt.securities?.join(', ')} ${bt.timeframe}` : ''
}

function BacktestReport({ data }: { data: Record<string, unknown> & BlockBase }) {
  const id = String(data.backtest_id || data.id || '')
  const { bt, err } = useBacktest(id)
  const [view, setView] = useState<'equity' | 'drawdown' | 'kline'>('equity')
  if (!id) return <div className="qb-block qb-bad qb-muted">回测报告缺少 backtest_id</div>
  if (err) return <div className="qb-block qb-bad qb-muted">读取回测 {id} 失败：{err}</div>
  if (!bt) return <div className="qb-block qb-pending"><span className="cb-spin" /> 正在读取回测 {id}…</div>
  const m = bt.metrics
  const win = windowOf(bt)
  const src = { label: `回测 ${id} · ${datasetLabel(bt)}`, href: `#/backtest/${id}` }
  const keys: (keyof typeof m)[] = ['total_return', 'sharpe', 'max_drawdown', 'win_rate', 'trades', 'total_fees', 'annualized_return', 'excess_return']
  const goal = (data.targets || {}) as Record<string, number>
  const items: MetricItem[] = keys.map((k) => ({ key: k, value: m[k] as number, window: win, source: src, evidence: 'measured', target: goal[k] }))
  const eq0 = bt.equity_curve[0] || 1
  const b0 = bt.benchmark_curve?.[0] || 1
  const trades = bt.round_trips || []
  const markers = trades.slice(0, 400).flatMap((t) => [{ x: t.open_ts, tone: 'up' as const, label: '开仓' }, { x: t.close_ts, tone: 'down' as const, label: `平仓 ${fmtValue(t.return, 'pct_signed')}` }])
  const warnings = [...(bt.warnings || []), ...(bt.errors || [])]
  const synthetic = bt.datasets?.some((d) => d.source === 'synthetic')
  return (
    <Frame type="backtest_report" wide data={{ ...data, title: String(data.title || `${bt.strategy_name || bt.strategy} · ${bt.securities?.join(', ')} ${bt.timeframe}`), source: src }}
      actions={<>
        <a className="qb-icon-btn" href={`#/backtest/${id}`}>打开完整报告</a>
        <button type="button" className="qb-icon-btn" onClick={() => askChat(`/explain 解读回测 ${id} 的结果与风险`)}>让 AI 解读</button>
      </>}>
      <div className={`qb-status-line st-${bt.status === 'done' ? (warnings.length ? 'warn' : 'ok') : 'error'}`}>
        <span className={`qb-dot st-${bt.status === 'done' ? (warnings.length ? 'warn' : 'ok') : 'error'}`} />
        {bt.status === 'done' ? '回测完成' : '回测失败'} · {win} · 用时 {(bt.duration_ms / 1000).toFixed(1)}s
        {synthetic && <span className="qb-ev warn">合成数据 · 非正式回测</span>}
      </div>
      {warnings.length > 0 && (
        <details className="qb-warn">
          <summary>{warnings.length} 条警告</summary>
          <ul>{warnings.slice(0, 20).map((w, i) => <li key={i}>{w}</li>)}</ul>
        </details>
      )}
      <MetricGrid items={items} />
      <div className="qb-tabs" role="tablist">
        {([['equity', '净值 vs 基准'], ['drawdown', '回撤'], ['kline', 'K 线复盘']] as const).map(([k, l]) => (
          <button type="button" role="tab" key={k} aria-selected={view === k} className={view === k ? 'on' : ''} onClick={() => setView(k)}>{l}</button>
        ))}
      </div>
      {view === 'equity' && (
        <Chart spec={{
          kind: 'line', x: bt.timestamps, y_format: 'pct_signed', markers,
          series: [
            { name: '策略', data: bt.equity_curve.map((v) => v / eq0 - 1) },
            ...(bt.benchmark_curve?.length ? [{ name: `基准 ${bt.benchmark || ''}`.trim(), data: bt.benchmark_curve.map((v) => v / b0 - 1), color: 'var(--bench)', dashed: true }] : []),
          ],
        }} />
      )}
      {view === 'drawdown' && <Chart spec={{ kind: 'area', x: bt.timestamps, y_format: 'pct', series: [{ name: '回撤', data: bt.drawdown.map((d) => -Math.abs(d)), color: 'var(--down)' }] }} />}
      {view === 'kline' && <KlineReview data={{ backtest_id: id }} embedded />}
    </Frame>
  )
}

// ---------------------------------------------------------------- K 线与交易复盘

const STEP = TF_MS

function KlineReview({ data, embedded }: { data: Record<string, unknown> & BlockBase; embedded?: boolean }) {
  const id = String(data.backtest_id || '')
  const { bt, err } = useBacktest(id || undefined)
  const [bars, setBars] = useState<KBar[] | null>(null)
  const [barErr, setBarErr] = useState('')
  const [sel, setSel] = useState<number | null>(null)
  const symbol = String(data.symbol || bt?.securities?.[0] || '')
  const timeframe = String(data.timeframe || bt?.timeframe || '1h')
  useEffect(() => {
    if (!bt || !symbol) return
    const ts = bt.timestamps
    const start = ts[0], end = ts[ts.length - 1]
    const exchange = bt.datasets?.[0]?.exchange || 'binance'
    const q = new URLSearchParams({ symbol, timeframe, exchange, limit: String(Math.min(5000, ts.length + 5)), start: String(start), end: String(end + (STEP[timeframe] || 3600e3)) })
    get<{ bars: { t: number; o: number; h: number; l: number; c: number; v: number }[] }>(`/api/market/bars?${q}`)
      .then((r) => setBars(r.bars))
      .catch((e) => setBarErr((e as Error).message))
  }, [bt, symbol, timeframe])
  const trades = bt?.round_trips || []
  const fills = bt?.fills || []
  const marks: KMark[] = useMemo(() => fills.filter((f) => !symbol || f.security === symbol).slice(0, 1500).map((f) => ({ ts: f.ts, side: f.side, price: Number(f.price), amount: Number(f.amount), text: f.reason })), [fills, symbol])
  if (!id) return <div className="qb-block qb-bad qb-muted">K 线复盘需要 backtest_id</div>
  const body = err ? <div className="qb-muted">读取回测失败：{err}</div> : !bt ? <div className="qb-pending"><span className="cb-spin" /> 正在读取回测…</div> : (
    <>
      {barErr ? <div className="qb-callout tone-warn">K 线取不到（未接入数据）：{barErr}</div>
        : bars ? <KlineChart bars={bars} marks={marks} stepMs={STEP[timeframe] || 3600e3} height={320} legend ma={(data.ma as number[]) || [20]} ariaLabel={`${symbol} 交易复盘`} />
        : <div className="shimmer-line" aria-hidden />}
      {trades.length > 0 && (
        <div className="qb-trades">
          <div className="qb-muted">完整交易 {trades.length} 笔{trades.length > 200 ? '（显示前 200 笔）' : ''} · 点一笔展开入场依据、出场原因与成本</div>
          <div className="qb-table-scroll" style={{ maxHeight: 300 }}>
            <table>
              <thead><tr><th>#</th><th>标的</th><th>开仓</th><th>平仓</th><th className="num">持有</th><th className="num">手续费</th><th className="num">盈亏</th><th className="num">收益率</th></tr></thead>
              <tbody>
                {trades.slice(0, 200).map((t, i) => {
                  const entry = fills.find((f) => f.security === t.security && Math.abs(f.ts - t.open_ts) < 1000)
                  const exit = fills.find((f) => f.security === t.security && Math.abs(f.ts - t.close_ts) < 1000 && f !== entry)
                  const slip = bt.cost_model?.slippage ?? bt.cost_model?.slippage_bps
                  return (
                    <Fragment key={i}>
                      <tr className={`qb-trade${sel === i ? ' on' : ''}`} onClick={() => setSel(sel === i ? null : i)}>
                        <td>{i + 1}</td><td>{t.security}</td><td>{fmtTs(t.open_ts)}</td><td>{fmtTs(t.close_ts)}</td>
                        <td className="num">{t.holding_ms ? `${(t.holding_ms / 3600e3).toFixed(1)}h` : '—'}</td>
                        <td className="num">{fmtValue(t.fees, 'money', 4)}</td>
                        <td className={`num ${Number(t.realized) > 0 ? 'up' : Number(t.realized) < 0 ? 'down' : ''}`}>{fmtValue(t.realized, 'money')}</td>
                        <td className={`num ${t.return > 0 ? 'up' : t.return < 0 ? 'down' : ''}`}>{fmtValue(t.return, 'pct_signed')}</td>
                      </tr>
                      {sel === i && (
                        <tr className="qb-trade-detail"><td colSpan={8}>
                          <div className="anim-expand qb-trade-grid">
                            <div><b>入场依据</b>{entry?.reason || entry?.note || '策略信号（未记录原因）'}{entry && <small> @ {fmtValue(entry.price, 'money')} × {fmtValue(entry.amount)}</small>}</div>
                            <div><b>出场原因</b>{exit?.reason || exit?.note || '—'}{exit && <small> @ {fmtValue(exit.price, 'money')}</small>}</div>
                            <div><b>滑点设定</b>{slip !== undefined ? String(slip) : '—'}{entry?.liquidity && <small> · {entry.liquidity}</small>}</div>
                            <div><b>费用</b>{fmtValue(t.fees, 'money', 4)}（开仓成本 {fmtValue(t.cost_in, 'money')}）</div>
                            <div><b>盈亏</b>{fmtValue(t.realized, 'money')} · {fmtValue(t.return, 'pct_signed')}</div>
                          </div>
                        </td></tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  )
  if (embedded) return <div className="qb-kline">{body}</div>
  return <Frame type="kline_review" wide data={{ ...data, title: String(data.title || `${symbol} ${timeframe} 交易复盘`), source: { backtest_id: id } }}>{body}</Frame>
}

// ---------------------------------------------------------------- 因子研究

interface FactorSeries { name: string; ic: (number | null)[] }
function FactorBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const dates = (data.dates as (number | string)[]) || []
  const factors: FactorSeries[] = Array.isArray(data.factors) ? (data.factors as FactorSeries[]) : data.ic ? [{ name: String(data.name || '因子'), ic: data.ic as number[] }] : []
  const win = Number(data.rolling_window || 20)
  const [range, setRange] = useState<'all' | 'h1' | 'h2' | 'last'>('all')
  const [on, setOn] = useState<Set<string>>(new Set(factors.slice(0, 3).map((f) => f.name)))
  const n = dates.length || factors[0]?.ic.length || 0
  const [a, b] = range === 'h1' ? [0, Math.floor(n / 2)] : range === 'h2' ? [Math.floor(n / 2), n] : range === 'last' ? [Math.floor(n * 0.75), n] : [0, n]
  const sel = factors.filter((f) => on.has(f.name))
  const rolling = (xs: (number | null)[]) => xs.map((_, i) => {
    if (i + 1 < win) return null
    const w = xs.slice(i + 1 - win, i + 1).filter((v): v is number => typeof v === 'number')
    return w.length ? w.reduce((s, v) => s + v, 0) / w.length : null
  })
  const stats = (xs: (number | null)[]) => {
    const v = xs.filter((x): x is number => typeof x === 'number' && Number.isFinite(x))
    const mean = v.reduce((s, x) => s + x, 0) / (v.length || 1)
    const sd = Math.sqrt(v.reduce((s, x) => s + (x - mean) ** 2, 0) / Math.max(1, v.length - 1))
    return { mean, ir: sd ? mean / sd : 0, pos: v.filter((x) => x > 0).length / (v.length || 1), n: v.length }
  }
  // 分三段看 IC 均值：只有一段显著为正 → 「只在某段行情有效」
  const regime = (xs: (number | null)[]) => {
    const k = Math.floor(xs.length / 3) || 1
    const parts = [xs.slice(0, k), xs.slice(k, 2 * k), xs.slice(2 * k)].map(stats)
    const good = parts.filter((p) => p.mean > 0.02 && p.ir > 0.1).length
    const verdict = good === 3 ? '整个样本都有效' : good === 0 ? '整个样本都不显著' : `只在 ${good} / 3 段有效，表现依赖行情阶段`
    return { parts, verdict }
  }
  const sliceX = dates.slice(a, b)
  return (
    <Frame type="factor_report" wide data={{ ...data, title: String(data.title || data.name || '因子研究') }}>
      <div className="qb-chart-tools">
        <div className="qb-legend">
          {factors.map((f, i) => (
            <button type="button" key={f.name} className={on.has(f.name) ? '' : 'off'} onClick={() => setOn((s) => { const x = new Set(s); if (x.has(f.name)) x.delete(f.name); else x.add(f.name); return x })}>
              <i style={{ background: ['var(--accent)', 'var(--amber)', '#0e9a9a', 'var(--violet)', '#d0607f'][i % 5] }} />{f.name}
            </button>
          ))}
        </div>
        <span className="qb-grow" />
        <span className="qb-seg">
          {([['all', '全样本'], ['h1', '前半段'], ['h2', '后半段'], ['last', '最近 1/4']] as const).map(([k, l]) => (
            <button type="button" key={k} className={range === k ? 'on' : ''} onClick={() => setRange(k)}>{l}</button>
          ))}
        </span>
      </div>
      <div className="qb-factor-stats">
        {sel.map((f) => {
          const s = stats(f.ic.slice(a, b))
          const r = regime(f.ic)
          return (
            <div key={f.name} className="qb-factor-stat">
              <b>{f.name}</b>
              <span>IC 均值 <em className={s.mean > 0 ? 'up' : 'down'}>{fmtValue(s.mean, 'ratio', 4)}</em></span>
              <span>ICIR <em>{fmtValue(s.ir, 'ratio', 2)}</em></span>
              <span>IC&gt;0 占比 <em>{fmtValue(s.pos, 'pct', 0)}</em></span>
              <span className="qb-regime" title={r.parts.map((p, i) => `第 ${i + 1} 段 IC ${p.mean.toFixed(4)}`).join('；')}>分段：{r.verdict}</span>
            </div>
          )
        })}
      </div>
      {sel.length > 0 && (
        <Chart spec={{
          kind: 'line', x: sliceX.length ? sliceX : undefined, baseline: 0, title: 'IC 序列',
          series: [
            ...(sel.length === 1 ? [{ name: `${sel[0].name} IC`, data: sel[0].ic.slice(a, b), kind: 'bar' as const, color: 'var(--bench)' }] : []),
            ...sel.map((f, i) => ({ name: `${f.name} 滚动 IC(${win})`, data: rolling(f.ic).slice(a, b), color: ['var(--accent)', 'var(--amber)', '#0e9a9a', 'var(--violet)', '#d0607f'][i % 5] })),
          ],
        }} />
      )}
      <div className="qb-factor-grid">
        {Array.isArray(data.groups) && (
          <div><div className="qb-sub">分组收益</div>
            <Chart height={180} spec={{ kind: 'bar', x: (data.groups as { name?: string; label?: string }[]).map((g, i) => g.name || g.label || `G${i + 1}`), y_format: 'pct_signed', series: [{ name: '平均收益', data: (data.groups as { ret?: number; return?: number }[]).map((g) => g.ret ?? g.return ?? null) }] }} />
          </div>
        )}
        {Array.isArray(data.decay) && (
          <div><div className="qb-sub">因子衰减</div>
            <Chart height={180} spec={{ kind: 'bar', x: (data.decay as { lag?: number }[]).map((d, i) => `T+${d.lag ?? i + 1}`), series: [{ name: 'IC', data: (data.decay as { ic?: number }[]).map((d) => d.ic ?? null) }] }} />
          </div>
        )}
        {Array.isArray(data.turnover) && (
          <div><div className="qb-sub">换手率</div>
            <Chart height={180} spec={{ kind: 'area', x: dates.length === (data.turnover as unknown[]).length ? dates : undefined, y_format: 'pct', series: [{ name: '换手率', data: data.turnover as number[] }] }} />
          </div>
        )}
      </div>
      {!!data.corr && typeof data.corr === 'object' && (
        <div><div className="qb-sub">因子相关性</div>
          <HeatmapChart spec={{ kind: 'heatmap', x: (data.corr as { labels: string[] }).labels, z: (data.corr as { matrix: number[][] }).matrix }} />
        </div>
      )}
    </Frame>
  )
}

// ---------------------------------------------------------------- 实验对比

interface Version { label: string; backtest_id?: string; metrics?: Record<string, number | null>; oos?: Record<string, number | null>; costs?: Record<string, number | null>; changes?: string[] | string }
function CompareBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const versions = (data.versions as Version[]) || []
  const [loaded, setLoaded] = useState<Record<string, BacktestResult>>({})
  useEffect(() => {
    versions.filter((v) => v.backtest_id && !v.metrics).forEach((v) => {
      loadBacktest(v.backtest_id!).then((bt) => setLoaded((x) => ({ ...x, [v.backtest_id!]: bt }))).catch(() => undefined)
    })
  }, [JSON.stringify(versions.map((v) => v.backtest_id))]) // eslint-disable-line react-hooks/exhaustive-deps
  const keys = (data.metrics as string[]) || ['total_return', 'sharpe', 'max_drawdown', 'win_rate', 'trades', 'total_fees']
  const metricsOf = (v: Version) => v.metrics || (v.backtest_id && loaded[v.backtest_id] ? (loaded[v.backtest_id].metrics as unknown as Record<string, number>) : {})
  const base = versions[0] ? metricsOf(versions[0]) : {}
  const hasOos = versions.some((v) => v.oos)
  const cell = (k: string, val: number | null | undefined, ref: number | null | undefined, first: boolean) => {
    const def = METRIC_DEFS[k]
    const fmt = def?.format || 'num'
    const delta = !first && typeof val === 'number' && typeof ref === 'number' ? val - ref : null
    const better = delta === null ? null : def?.good === 'low' ? delta < 0 : delta > 0
    return (
      <td className="num">
        {fmtValue(val, fmt)}
        {delta !== null && Math.abs(delta) > 1e-12 && <small className={better ? 'good' : 'bad'}>{delta > 0 && fmt !== 'pct_signed' ? '+' : ''}{fmtValue(delta, fmt)}</small>}
      </td>
    )
  }
  return (
    <Frame type="compare" wide data={data}>
      <div className="qb-table-scroll">
        <table className="qb-compare">
          <thead>
            <tr><th>指标</th>{versions.map((v) => <th key={v.label} className="num">{v.backtest_id ? <a href={`#/backtest/${v.backtest_id}`}>{v.label}</a> : v.label}</th>)}</tr>
          </thead>
          <tbody>
            <tr className="qb-group"><td colSpan={versions.length + 1}>样本内 <EvidenceTag e="measured" /></td></tr>
            {keys.map((k) => (
              <tr key={k}><td title={METRIC_DEFS[k]?.def}>{METRIC_DEFS[k]?.label || k}</td>{versions.map((v, i) => <Fragment key={v.label}>{cell(k, metricsOf(v)[k], base[k], i === 0)}</Fragment>)}</tr>
            ))}
            {hasOos && <tr className="qb-group"><td colSpan={versions.length + 1}>样本外 <EvidenceTag e="measured" /></td></tr>}
            {hasOos && keys.filter((k) => versions.some((v) => v.oos?.[k] !== undefined)).map((k) => (
              <tr key={`oos-${k}`}><td>{METRIC_DEFS[k]?.label || k}</td>{versions.map((v, i) => <Fragment key={v.label}>{cell(k, v.oos?.[k], versions[0].oos?.[k], i === 0)}</Fragment>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
      {versions.some((v) => v.changes) && (
        <>
          <div className="qb-sub">规则改动</div>
          <div className="qb-changes">
          {versions.map((v) => v.changes ? (
            <div key={v.label} className="qb-change"><b>{v.label}</b>
              <ul>{(Array.isArray(v.changes) ? v.changes : [v.changes]).map((c, i) => <li key={i}>{c}</li>)}</ul>
            </div>
          ) : null)}
          </div>
        </>
      )}
      {typeof data.explanation === 'string' && data.explanation && (
        <div className="qb-explain">
          <div className="qb-sub">Agent 的解释 <EvidenceTag e="inferred" /></div>
          <p>{data.explanation}</p>
          <small className="qb-muted">以上是对「为什么有效」的推测，不是验证结论；以表格里的实测结果为准。</small>
        </div>
      )}
    </Frame>
  )
}

// ---------------------------------------------------------------- 引用与证据链

function CitationList({ data }: { data: Record<string, unknown> & BlockBase }) {
  const ctx = useContext(CitationCtx)
  const sources = ((Array.isArray(data.sources) ? data.sources : data.items) as CiteSource[]) || []
  return (
    <Frame type="citation" data={{ ...data, title: String(data.title || `来源（${sources.length}）`) }}>
      <ol className="qb-sources">
        {sources.map((s, i) => {
          const id = String(s.id ?? i + 1)
          const href = safeHref(s.url) || safeHref(s.ref)
          return (
            <li key={id} id={ctx ? `${ctx.anchor}-src-${id}` : undefined}>
              <span className="qb-src-no">{id}</span>
              <div>
                {href ? <a href={href} target={href.startsWith('#') ? undefined : '_blank'} rel="noreferrer noopener">{s.title || hostOf(s.url || href)}</a> : <b>{s.title || `来源 ${id}`}</b>}
                <small className="qb-muted"> {[kindLabel(s.kind), s.publisher, s.date, s.url && hostOf(s.url)].filter(Boolean).join(' · ')}</small>
                {s.snippet && <SnippetFold text={s.snippet} />}
              </div>
            </li>
          )
        })}
      </ol>
    </Frame>
  )
}

function SnippetFold({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  if (text.length <= 140) return <p className="qb-snippet">{text}</p>
  return (
    <p className="qb-snippet">
      {open ? text : text.slice(0, 140) + '…'} <button type="button" className="link" onClick={() => setOpen(!open)}>{open ? '收起' : '展开'}</button>
    </p>
  )
}

interface Claim { text: string; type?: Evidence; ref?: string; cite?: (number | string)[]; confidence?: string }
function ClaimsBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const claims = ((data.claims || data.items) as Claim[]) || []
  const [filter, setFilter] = useState<Evidence | 'all'>('all')
  const shown = filter === 'all' ? claims : claims.filter((c) => (c.type || 'inferred') === filter)
  const count = (t: Evidence) => claims.filter((c) => (c.type || 'inferred') === t).length
  return (
    <Frame type="claims" data={{ ...data, title: String(data.title || '结论与证据') }}
      actions={<span className="qb-seg">
        <button type="button" className={filter === 'all' ? 'on' : ''} onClick={() => setFilter('all')}>全部 {claims.length}</button>
        {(Object.keys(EVIDENCE) as Evidence[]).map((t) => <button type="button" key={t} className={filter === t ? 'on' : ''} onClick={() => setFilter(t)}>{EVIDENCE[t].label} {count(t)}</button>)}
      </span>}>
      <ul className="qb-claims">
        {shown.map((c, i) => {
          const ref = c.ref ? (c.ref.startsWith('bt:') ? `#/backtest/${c.ref.slice(3)}` : safeHref(c.ref)) : undefined
          return (
            <li key={i} className={`ev-${c.type || 'inferred'}`}>
              <EvidenceTag e={c.type || 'inferred'} />
              <span>{c.text}{c.cite?.map((n) => <CiteMark key={String(n)} n={String(n)} />)}</span>
              {ref && <a className="qb-icon-btn" href={ref} target={ref.startsWith('#') ? undefined : '_blank'} rel="noreferrer noopener">原始记录</a>}
            </li>
          )
        })}
      </ul>
    </Frame>
  )
}

// ---------------------------------------------------------------- 文件与附件

function FileBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const name = String(data.name || '文件')
  const url = safeHref(data.url)
  const kind = String(data.kind || name.split('.').pop() || 'txt').toLowerCase()
  const [content, setContent] = useState<string | null>(typeof data.content === 'string' ? data.content : null)
  const [err, setErr] = useState('')
  const [full, setFull] = useState(false)
  useEffect(() => {
    if (content !== null || !url || ['pdf', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'image'].includes(kind)) return
    fetch(url, { credentials: url.startsWith('/') ? 'include' : 'omit' }).then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`)))).then((t) => setContent(t.slice(0, 2_000_000)), (e) => setErr((e as Error).message))
  }, [url, kind, content])
  const download = () => { if (url) { const a = document.createElement('a'); a.href = url; a.download = name; a.click() } else if (content !== null) downloadBlob(name, content) }
  const preview = (() => {
    if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'image'].includes(kind)) return url ? <ImageView src={url} alt={name} /> : null
    if (kind === 'pdf') return url ? <iframe className="qb-pdf" src={url} title={name} /> : <div className="qb-muted">PDF 需要 url 才能预览</div>
    if (err) return <div className="qb-muted">读取失败：{err}</div>
    if (content === null) return <div className="shimmer-line" aria-hidden />
    if (kind === 'csv' || kind === 'tsv') {
      const sep = kind === 'tsv' ? '\t' : ','
      const lines = content.split(/\r?\n/).filter(Boolean)
      const header = splitCsv(lines[0] || '', sep)
      const rows = lines.slice(1, full ? undefined : 2000).map((l) => Object.fromEntries(splitCsv(l, sep).map((v, i) => [header[i] ?? `c${i}`, v])))
      return <DataTable columns={header.map((h) => ({ key: h, label: h }))} rows={rows} note={lines.length > 2001 && !full ? `只预览前 2000 行（共 ${lines.length - 1} 行）` : undefined} />
    }
    if (kind === 'json') {
      let pretty = content
      try { pretty = JSON.stringify(JSON.parse(content), null, 2) } catch { /* 原样显示 */ }
      return <CodeBlock code={full ? pretty : pretty.slice(0, 20000)} info={`json title=${name}`} />
    }
    if (kind === 'md' || kind === 'markdown') return <pre className="qb-run-out">{full ? content : content.slice(0, 8000)}</pre>
    const isLog = kind === 'log'
    const text = full ? content : content.slice(-20000)
    return <CodeBlock code={text} info={`${isLog ? 'log' : kind} title=${name}`} errorLines={isLog ? text.split('\n').map((l, i) => (/error|exception|traceback|失败|错误/i.test(l) ? i + 1 : 0)).filter(Boolean).slice(0, 30) : undefined} />
  })()
  return (
    <Frame type="file" data={{ ...data, title: name }} actions={<>
      {content !== null && <button type="button" className="qb-icon-btn" onClick={() => copyText(content)}>复制内容</button>}
      <button type="button" className="qb-icon-btn" onClick={download}>下载</button>
      {url ? <a className="qb-icon-btn" href={url} target="_blank" rel="noreferrer noopener">打开完整文件</a>
        : content !== null && content.length > 8000 && <button type="button" className="qb-icon-btn" onClick={() => setFull(!full)}>{full ? '只看预览' : '查看全部'}</button>}
    </>}>
      {typeof data.size === 'number' && <div className="qb-muted">{kind.toUpperCase()} · {(Number(data.size) / 1024).toFixed(1)} KB</div>}
      {preview}
    </Frame>
  )
}

function splitCsv(line: string, sep: string): string[] {
  const out: string[] = []
  let cur = '', q = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (q) { if (ch === '"' && line[i + 1] === '"') { cur += '"'; i++ } else if (ch === '"') q = false; else cur += ch }
    else if (ch === '"') q = true
    else if (ch === sep) { out.push(cur); cur = '' }
    else cur += ch
  }
  out.push(cur)
  return out
}

// ---------------------------------------------------------------- 状态 / 步骤 / 选项 / 表单 / 确认

interface Action { label: string; prompt?: string; href?: string; primary?: boolean }
function Actions({ actions }: { actions?: Action[] }) {
  if (!actions?.length) return null
  return (
    <div className="qb-actions">
      {actions.map((a, i) => {
        const href = safeHref(a.href)
        return href
          ? <a key={i} className={`button small${a.primary ? '' : ' ghost'}`} href={href} target={href.startsWith('#') ? undefined : '_blank'} rel="noreferrer noopener">{a.label}</a>
          : <button key={i} type="button" className={`button small${a.primary ? '' : ' ghost'}`} onClick={() => a.prompt && askChat(a.prompt)}>{a.label}</button>
      })}
    </div>
  )
}

const STATE_LABEL: Record<string, string> = { ok: '成功', done: '完成', running: '进行中', warn: '有警告', error: '失败', failed: '失败', pending: '等待中', skipped: '已跳过' }

function StatusBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const state = String(data.state || data.status || 'ok')
  return (
    <div className={`qb-block qb-status st-${state} anim-rise`}>
      <div className="qb-status-head">
        <span className={`qb-dot st-${state}`} />
        <b>{String(data.title || STATE_LABEL[state] || state)}</b>
        <span className={`qb-state st-${state}`}>{STATE_LABEL[state] || state}</span>
        <EvidenceTag e={data.evidence} />
      </div>
      {typeof data.detail === 'string' && <p className="qb-note">{data.detail}</p>}
      {Array.isArray(data.metrics) && <MetricGrid items={data.metrics as MetricItem[]} />}
      {Array.isArray(data.warnings) && data.warnings.length > 0 && (
        <ul className="qb-warn-list">{(data.warnings as string[]).map((w, i) => <li key={i}>{w}</li>)}</ul>
      )}
      {Array.isArray(data.details) && (
        <details className="qb-warn"><summary>详情</summary><ul>{(data.details as string[]).map((w, i) => <li key={i}>{w}</li>)}</ul></details>
      )}
      <Actions actions={data.actions as Action[]} />
      <SourceLine source={data.source} />
    </div>
  )
}

function StepsBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const items = ((data.items || data.steps) as { title: string; status?: string; note?: string; time?: string }[]) || []
  return (
    <Frame type="steps" data={data}>
      <ol className="qb-steps">
        {items.map((s, i) => (
          <li key={i} className={`st-${s.status || 'pending'}`}>
            <span className={`qb-step-dot st-${s.status || 'pending'}`}>{s.status === 'done' || s.status === 'ok' ? '✓' : s.status === 'failed' || s.status === 'error' ? '!' : i + 1}</span>
            <div>
              <b>{s.title}</b>{s.time && <small className="qb-muted"> · {s.time}</small>}
              {s.note && <p>{s.note}</p>}
            </div>
          </li>
        ))}
      </ol>
    </Frame>
  )
}

function ChoicesBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const [picked, setPicked] = useState<string | null>(null)
  const opts = ((data.options || data.items) as (string | { label: string; prompt?: string; description?: string })[]) || []
  return (
    <div className="qb-block qb-choices anim-rise">
      {data.question !== undefined && <b>{String(data.question)}</b>}
      <div className="qb-choice-list">
        {opts.map((o, i) => {
          const label = typeof o === 'string' ? o : o.label
          const prompt = typeof o === 'string' ? o : o.prompt || o.label
          return (
            <button key={i} type="button" className={`qb-choice${picked === label ? ' on' : ''}`} disabled={picked !== null} onClick={() => { setPicked(label); askChat(prompt) }}>
              <span>{label}</span>
              {typeof o !== 'string' && o.description && <small>{o.description}</small>}
            </button>
          )
        })}
      </div>
    </div>
  )
}

interface FormField { key: string; label?: string; type?: 'text' | 'number' | 'select' | 'checkbox'; options?: string[]; default?: unknown; placeholder?: string }
function FormBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const fields = (data.fields as FormField[]) || []
  const [vals, setVals] = useState<Record<string, unknown>>(() => Object.fromEntries(fields.map((f) => [f.key, f.default ?? (f.type === 'checkbox' ? false : '')])))
  const [sent, setSent] = useState(false)
  const submit = () => {
    const lines = fields.map((f) => `${f.label || f.key}：${f.type === 'checkbox' ? (vals[f.key] ? '是' : '否') : String(vals[f.key] ?? '')}`)
    askChat(`${String(data.prompt || data.title || '提交表单')}\n${lines.join('\n')}`)
    setSent(true)
  }
  return (
    <Frame type="form" data={data}>
      <div className="qb-form">
        {fields.map((f) => (
          <label key={f.key} className="qb-field">
            <span>{f.label || f.key}</span>
            {f.type === 'select' ? (
              <select value={String(vals[f.key] ?? '')} onChange={(e) => setVals({ ...vals, [f.key]: e.target.value })} disabled={sent}>
                {(f.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : f.type === 'checkbox' ? (
              <input type="checkbox" checked={!!vals[f.key]} onChange={(e) => setVals({ ...vals, [f.key]: e.target.checked })} disabled={sent} />
            ) : (
              <input type={f.type === 'number' ? 'number' : 'text'} value={String(vals[f.key] ?? '')} placeholder={f.placeholder} onChange={(e) => setVals({ ...vals, [f.key]: f.type === 'number' ? e.target.value : e.target.value })} disabled={sent} />
            )}
          </label>
        ))}
      </div>
      <div className="qb-actions"><button type="button" className="button small" disabled={sent} onClick={submit}>{sent ? '已提交' : String(data.submit_label || '提交')}</button></div>
    </Frame>
  )
}

function ConfirmBlock({ data }: { data: Record<string, unknown> & BlockBase }) {
  const [done, setDone] = useState<'yes' | 'no' | null>(null)
  return (
    <div className={`qb-block qb-confirm tone-${String(data.tone || 'warn')} anim-rise`}>
      <b>{String(data.title || '需要你确认')}</b>
      {typeof data.detail === 'string' && <p className="qb-note">{data.detail}</p>}
      <div className="qb-actions">
        {done ? <span className="qb-muted">{done === 'yes' ? '已确认' : '已取消'}</span> : <>
          <button type="button" className="button small" onClick={() => { setDone('yes'); askChat(String(data.prompt || data.confirm_prompt || '确认，继续')) }}>{String(data.confirm_label || '确认')}</button>
          <button type="button" className="button small ghost" onClick={() => { setDone('no'); askChat(String(data.cancel_prompt || '先不要，取消这个操作')) }}>{String(data.cancel_label || '取消')}</button>
        </>}
      </div>
    </div>
  )
}
