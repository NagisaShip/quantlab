import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { TF_MS } from '../timeframes'
import { api, dateOnly, shortTime, SOURCE_LABEL, type BarDTO, type Quality } from '../api/core'
import {
  Callout,
  EmptyState,
  Field,
  Heading,
  Loading,
  MetricCard,
  Panel,
  Segments,
  SourceBanner,
  Tag,
} from '../components/ui'
import DatahubSection from './data/Datahub'
import { LocalLibrary } from './data/LocalLibrary'
import KlineChart from '../components/KlineChart'
import type { PageProps } from './types'

const BARS_PER_YEAR: Record<string, number> = { '1m': 525_600, '5m': 105_120, '15m': 35_040, '30m': 17_520, '1h': 8760, '4h': 2190, '1d': 365 }
const DAY = 86_400_000
/** 时间段：按周期换算成要取的 K 线根数（接口单次最多 5000 根） */
const RANGES: { key: string; label: string; ms: number }[] = [
  { key: '1d', label: '1天', ms: DAY }, { key: '1w', label: '1周', ms: 7 * DAY }, { key: '1m', label: '1月', ms: 30 * DAY },
  { key: '3m', label: '3月', ms: 91 * DAY }, { key: '6m', label: '6月', ms: 182 * DAY }, { key: '1y', label: '1年', ms: 365 * DAY },
  { key: 'max', label: '最长', ms: Infinity },
]
const MAX_BARS = 5000
const barsFor = (range: string, tf: string) => {
  const r = RANGES.find((x) => x.key === range) ?? RANGES[2]
  return Number.isFinite(r.ms) ? Math.min(MAX_BARS, Math.max(30, Math.ceil(r.ms / (TF_MS[tf] ?? 3_600_000)))) : MAX_BARS
}
/** 周期切换时自动挑一个合适的时间段，避免 1 分钟线配 1 年 */
const DEFAULT_RANGE: Record<string, string> = { '1m': '1d', '5m': '1w', '15m': '1w', '30m': '1m', '1h': '1m', '4h': '6m', '1d': '1y' }
const fmtPx = (v: number) => v.toLocaleString('en-US', { maximumFractionDigits: v >= 100 ? 2 : 5 })

/**
 * 从路由里取默认标的、周期与时间段：
 * `#/data/ETH/USDT/1h`（首页关注卡片跳过来时带上标的与周期），
 * `#/data/ETH/USDT/1h/range:3m`（再带上「时间段」，用 range: 前缀避免和 1m / 1d 这类合法 K 线周期撞车），
 * `#/data` 用默认值，`#/data/hub` 是研究数据页。
 */
function dataRoute(arg?: string): { symbol?: string; timeframe?: string; range?: string } {
  if (!arg || arg === 'hub') return {}
  const parts = arg.split('/').filter(Boolean)
  let range = ''
  const tail = parts[parts.length - 1]
  if (tail && tail.startsWith('range:')) {
    const key = tail.slice('range:'.length)
    if (RANGES.some((r) => r.key === key)) range = key
    parts.pop()
  }
  let timeframe = ''
  const last = parts[parts.length - 1]
  if (parts.length > 1 && last && TF_MS[last]) {
    timeframe = last
    parts.pop()
  }
  const symbol = parts.join('/').toUpperCase()
  return { symbol: symbol || undefined, timeframe: timeframe || undefined, range: range || undefined }
}

export default function DataCenterPage({ notify, onNavigate, arg, features }: PageProps & { arg?: string }) {
  const route = dataRoute(arg)
  const tab = arg === 'hub' ? 'hub' : 'bars'
  const heading = (
    <>
      <Heading
        eyebrow="MARKET & DATA"
        title="行情与数据"
        description="行情 K 线与研究数据（衍生品、链上、情绪、宏观）都在这里。只用真实数据：拿不到时如实显示「未接入数据」，可以一键同步。"
      />
      <div className="fx-tabs">
        <Segments value={tab} onChange={(t) => onNavigate(t === 'hub' ? 'data/hub' : 'data')} ariaLabel="行情与数据视图"
          options={[{ value: 'bars', label: '行情 K 线' }, { value: 'hub', label: '研究数据' }]} />
      </div>
    </>
  )
  if (tab === 'hub') return <>{heading}<DatahubSection notify={notify} /></>
  return <BarsView notify={notify} heading={heading} features={features} initialSymbol={route.symbol} initialTimeframe={route.timeframe} initialRange={route.range} />
}

function BarsView({ notify, heading, features, initialSymbol, initialTimeframe, initialRange }: Pick<PageProps, 'notify' | 'features'> & { heading: ReactNode; initialSymbol?: string; initialTimeframe?: string; initialRange?: string }) {
  const [symbols, setSymbols] = useState<string[]>([])
  const [timeframes, setTimeframes] = useState<string[]>([])
  const [symbol, setSymbol] = useState(initialSymbol || 'BTC/USDT')
  const [market, setMarket] = useState<'spot' | 'perp'>('spot')
  const effectiveSymbol = market === 'perp' ? `${symbol.split(':')[0]}:PERP` : symbol.split(':')[0]
  const [timeframe, setTimeframe] = useState(initialTimeframe || '1h')
  const [exchange, setExchange] = useState('binance')
  // 从首页关注卡片跳进来时，标的、周期与时间段都按卡片上的设置来（而不是沿用上次看的时段）
  const [range, setRange] = useState(() => {
    if (initialRange && RANGES.some((r) => r.key === initialRange)) return initialRange
    try { return (initialTimeframe && DEFAULT_RANGE[initialTimeframe]) || localStorage.getItem('qa.kline.range') || '1m' } catch { return '1m' }
  })
  const [ma, setMa] = useState<number[]>(() => { try { return JSON.parse(localStorage.getItem('qa.kline.ma') || '[20,60]') } catch { return [20, 60] } })
  const pickRange = (r: string) => { setRange(r); try { localStorage.setItem('qa.kline.range', r) } catch { /* 私密模式 */ } }
  const pickTf = (tf: string) => { setTimeframe(tf); pickRange(DEFAULT_RANGE[tf] ?? range) }
  const toggleMa = (n: number) => {
    const next = ma.includes(n) ? ma.filter((x) => x !== n) : [...ma, n].sort((a, b) => a - b)
    setMa(next); try { localStorage.setItem('qa.kline.ma', JSON.stringify(next)) } catch { /* 私密模式 */ }
  }
  const [bars, setBars] = useState<BarDTO[]>([])
  const [source, setSource] = useState('')
  const [quality, setQuality] = useState<Quality | null>(null)
  const [staleNote, setStaleNote] = useState('')
  const [summary, setSummary] = useState<Record<string, number | string> | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // CSV 上传
  const [csvText, setCsvText] = useState('')
  const [csvName, setCsvName] = useState('')
  const [datasets, setDatasets] = useState<Awaited<ReturnType<typeof api.datasets>>['datasets']>([])

  const loadDatasets = useCallback(async () => {
    try {
      const d = await api.datasets()
      setDatasets(d.datasets)
    } catch {
      /* 数据集列表失败不影响主流程 */
    }
  }, [])

  useEffect(() => {
    api.symbols().then((d) => {
      setSymbols(d.symbols)
      setTimeframes(d.timeframes)
    })
    loadDatasets()
  }, [loadDatasets])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      // 三个请求互不依赖：任何一个失败（例如交易所超时）都不让其他两块一起空白
      const [b, q, s] = await Promise.allSettled([
        api.bars(effectiveSymbol, timeframe, barsFor(range, timeframe), exchange),
        api.quality(effectiveSymbol, timeframe, 1500, exchange),
        api.marketSummary(symbol, timeframe),
      ])
      if (b.status === 'fulfilled') { setBars(b.value.bars); setSource(b.value.source) }
      if (q.status === 'fulfilled') setQuality(q.value)
      if (s.status === 'fulfilled') setSummary(s.value)
      const failed = [b, q, s].filter((r): r is PromiseRejectedResult => r.status === 'rejected')
      const stale = (b.status === 'fulfilled' && b.value.warning) || (q.status === 'fulfilled' && q.value.warning) || ''
      if (failed.length) setError(`${failed.length === 3 ? '行情刷新失败' : '部分数据没刷新'}：${(failed[0].reason as Error).message}`)
      setStaleNote(stale ? `${stale}。稍后再刷新即可补齐最新 K 线。` : '')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [effectiveSymbol, timeframe, exchange, range])

  useEffect(() => {
    load()
  }, [load])

  const uploadCsv = async () => {
    if (!csvText.trim()) {
      notify('请先选择或粘贴 CSV 内容', true)
      return
    }
    try {
      const res = await api.uploadDataset({
        content: csvText,
        filename: csvName || 'pasted.csv',
        symbol,
        timeframe,
      })
      notify(`CSV 校验通过：${res.bars} 根，覆盖 ${res.dataset.coverage_hours} 小时`)
      setCsvText('')
      setCsvName('')
      await loadDatasets()
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const onFile = async (file: File | undefined) => {
    if (!file) return
    if (file.size > 2_000_000) {
      notify('CSV 最大 2 MB', true)
      return
    }
    setCsvText(await file.text())
    setCsvName(file.name)
    notify(`${file.name} 已载入，点「上传并校验」提交`)
  }

  const closes = bars.map((b) => b.c)
  const change = closes.length > 1 ? (closes[closes.length - 1] - closes[0]) / closes[0] : 0

  return (
    <>
      {heading}

      <SourceBanner
        tone={source === 'exchange' ? 'real' : source === 'csv' ? 'paper' : 'warn'}
        icon="⇄"
        title={source ? SOURCE_LABEL[source] ?? source : '正在读取行情'}
        detail={
          bars.length
            ? `${symbol} · ${timeframe} · ${bars.length} 根 K 线 · 最新收盘 ${closes[closes.length - 1]?.toFixed(2)}`
            : '—'
        }
        warn={source === 'synthetic' ? '合成数据只能验证流程与代码正确性，不能用来评估策略的真实表现' : error ? '未接入数据：可以在下方「本地行情库」同步，或换一个交易所' : undefined}
        right="只读公共行情"
      />

      <div className="config-bar">
        <Field label="交易对">
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {[...new Set([symbol, ...symbols])].map((s) => <option key={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="市场">
          <select value={market} onChange={(e) => setMarket(e.target.value as 'spot' | 'perp')}>
            <option value="spot">现货</option>
            <option value="perp" disabled={features?.['market.perp_linear'] === false}>线性永续</option>
          </select>
        </Field>
        <Field label="交易所">
          <select value={exchange} onChange={(e) => setExchange(e.target.value)}>
            <option value="binance">Binance</option>
            <option value="okx">OKX</option>
            <option value="bybit">Bybit</option>
          </select>
        </Field>
        <div style={{ display: 'flex', alignItems: 'flex-end' }}>
          <button className="button primary" onClick={load} disabled={loading}>
            {loading ? <><span className="spinner" /> 读取中</> : '⇄ 读取行情'}
          </button>
        </div>
      </div>

      {error && <Callout tone="fail" title="读取失败">{error}</Callout>}
      {!error && staleNote && <Callout tone="warn" title="数据可能不是最新">{staleNote}</Callout>}
      {loading && !bars.length && <Loading label="正在读取行情" />}

      {bars.length > 0 && (
        <>
          <div className="metrics-grid">
            <MetricCard
              label="最新收盘价"
              value={closes[closes.length - 1]?.toFixed(2)}
              caption={`区间涨跌 ${(change * 100).toFixed(2)}%`}
              tone={change >= 0 ? 'positive' : 'negative'}
              bars
            />
            <MetricCard
              label="区间最高"
              value={fmtPx(Math.max(...bars.map((b) => b.h)))}
              caption={`最低 ${fmtPx(Math.min(...bars.map((b) => b.l)))} · ${bars.length} 根 ${timeframe}`}
            />
            <MetricCard
              label="波动率"
              value={summary?.volatility_per_bar !== undefined ? `${(Number(summary.volatility_per_bar) * 100).toFixed(2)}%` : '—'}
              caption={summary?.volatility_per_bar !== undefined ? `每根 ${timeframe} · 年化约 ${(Number(summary.volatility_per_bar) * Math.sqrt(BARS_PER_YEAR[timeframe] ?? 8760) * 100).toFixed(0)}%` : '按 bar 收益率标准差'}
              foot="短区间波动率高不代表长期风险高"
            />
            <MetricCard
              label="未收盘 K 线"
              value={quality?.unclosed ?? 0}
              unit="根"
              caption={quality?.unclosed ? '不计入策略输入' : '全部为已收盘 K 线'}
              foot="策略只能使用已收盘信息"
            />
          </div>

          <Panel title={`价格走势（${symbol} · ${timeframe}）`} subtitle="鼠标移到 K 线上看开高低收、涨跌、振幅和成交量；滚轮缩放、拖动平移、双击回到最新">
            <div className="kline-toolbar">
              <div className="kt-group" role="group" aria-label="周期">
                {(timeframes.length ? timeframes : Object.keys(TF_MS)).map((tf) => (
                  <button key={tf} type="button" aria-pressed={tf === timeframe} onClick={() => pickTf(tf)}>{tf}</button>
                ))}
              </div>
              <div className="kt-range" role="group" aria-label="时间段">
                <div className="kt-range-current">
                  <span>时间段</span>
                  <b>{RANGES.find((r) => r.key === range)?.label || '1月'}</b>
                </div>
                <input
                  type="range"
                  min={0}
                  max={RANGES.length - 1}
                  step={1}
                  value={Math.max(0, RANGES.findIndex((r) => r.key === range))}
                  onChange={(e) => pickRange(RANGES[Number(e.target.value)]?.key ?? '1m')}
                  aria-label="价格走势时间段"
                  aria-valuetext={RANGES.find((r) => r.key === range)?.label || '1月'}
                />
                <div className="kt-range-ticks" aria-hidden>
                  {RANGES.map((r) => <span key={r.key} className={r.key === range ? 'on' : ''}>{r.label}</span>)}
                </div>
              </div>
              <div className="kt-group" role="group" aria-label="均线">
                {[7, 20, 60, 120].map((n) => (
                  <button key={n} type="button" aria-pressed={ma.includes(n)} onClick={() => toggleMa(n)}>MA{n}</button>
                ))}
              </div>
              <span className="kt-span">
                {dateOnly(bars[0].t)} → {dateOnly(bars[bars.length - 1].t)} · {bars.length.toLocaleString()} 根
                {bars.length < barsFor(range, timeframe) && barsFor(range, timeframe) < MAX_BARS ? ' · 数据源只有这么多' : barsFor(range, timeframe) >= MAX_BARS && range !== 'max' ? ' · 已到单次上限 5000 根' : ''}
              </span>
            </div>
            <KlineChart bars={bars} stepMs={TF_MS[timeframe] ?? 3_600_000} height={420} ma={ma} legend ariaLabel={`${symbol} ${timeframe} K 线`} />
          </Panel>

          <div className="two-columns">
            <Panel title="数据质量报告" subtitle="缺口 / 重复 / 未收盘逐项列出，不做任何填补">
              {quality ? (
                <table>
                  <tbody>
                    <tr><td>检查范围（K 线根数）</td><td className="right">{quality.bars}</td></tr>
                    <tr>
                      <td>缺口</td>
                      <td className="right">{quality.gaps} {quality.gaps > 0 && <Tag tone="warn">存在缺口</Tag>}</td>
                    </tr>
                    <tr>
                      <td>重复</td>
                      <td className="right">{quality.duplicates} {quality.duplicates > 0 && <Tag tone="fail">需去重</Tag>}</td>
                    </tr>
                    <tr>
                      <td>未收盘 K 线</td>
                      <td className="right">{quality.unclosed}</td>
                    </tr>
                    <tr>
                      <td>覆盖区间</td>
                      <td className="right">{dateOnly(quality.coverage_start)} → {dateOnly(quality.coverage_end)}</td>
                    </tr>
                    <tr><td>来源</td><td className="right">{SOURCE_LABEL[quality.source] ?? quality.source}</td></tr>
                  </tbody>
                </table>
              ) : (
                <EmptyState glyph="▦" title="暂无质量报告" />
              )}
              <div className="hint">
                数据质量问题必须显式报错或标注，不允许静默填补——填补出来的是人工数据，不是可交易报价。
              </div>
            </Panel>

            <Panel title="最近 12 根 K 线" flush>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>开盘时间 · UTC</th>
                      <th className="right">开</th>
                      <th className="right">高</th>
                      <th className="right">低</th>
                      <th className="right">收</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {bars.slice(-12).reverse().map((b) => (
                      <tr key={b.t}>
                        <td className="muted">{shortTime(b.t).slice(5, 16)}</td>
                        <td className="right">{b.o.toFixed(2)}</td>
                        <td className="right">{b.h.toFixed(2)}</td>
                        <td className="right">{b.l.toFixed(2)}</td>
                        <td className={`right ${b.c >= b.o ? 'positive' : 'negative'}`}>{b.c.toFixed(2)}</td>
                        <td>{b.closed ? <span className="table-pill">已收盘</span> : <span className="table-pill pending">未完成</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Panel>
          </div>
        </>
      )}

      <LocalLibrary notify={notify} />

      <Panel
        title="导入小时行情 CSV"
        subtitle="无法联网时也能完成整个验证流程。校验不通过会明确报错，不做任何填补。"
        actions={
          <>
            <a className="button small" href={api.sampleCsvUrl} download>↓ 教学 CSV 样本</a>
            <Tag mono>1H</Tag>
          </>
        }
      >
        <div className="two-columns even">
          <div>
            <Field label="选择 CSV 文件" hint="必需列：timestamp, open, high, low, close；volume 可选">
              <input
                type="file"
                accept=".csv,text/csv"
                onChange={(e) => onFile(e.target.files?.[0])}
                style={{ padding: 8, background: 'var(--sunken)' }}
              />
            </Field>
            {csvName && (
              <div className="hint">
                已载入 {csvName}（{Math.round(csvText.length / 1024)} KB）
              </div>
            )}
            <div style={{ marginTop: 14, display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <button className="button primary" onClick={uploadCsv} disabled={!csvText.trim()}>
                ⇧ 上传并校验
              </button>
              <button
                className="button"
                onClick={() => {
                  setCsvText('')
                  setCsvName('')
                }}
                disabled={!csvText}
              >
                清空
              </button>
            </div>
            <Callout>
              时间支持毫秒 / 秒时间戳，或带时区的 ISO 格式。裸时间（不带时区）会被拒绝——
              它在不同环境下含义不同。时间必须升序、无重复、无缺口，OHLC 关系必须成立。
            </Callout>
          </div>

          <div>
            <Field label="已保存的数据集">
              <select disabled={!datasets.length} value="">
                <option value="">
                  {datasets.length ? `共 ${datasets.length} 份` : '还没有上传过 CSV'}
                </option>
                {datasets.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.filename} · {d.bars} 根 · {d.coverage_hours} 小时
                  </option>
                ))}
              </select>
            </Field>
            {datasets.length > 0 && (
              <div className="table-wrap" style={{ marginTop: 12 }}>
                <table>
                  <thead>
                    <tr><th>文件</th><th className="right">K 线</th><th>摘要</th><th /></tr>
                  </thead>
                  <tbody>
                    {datasets.map((d) => (
                      <tr key={d.id}>
                        <td>
                          <strong>{d.filename}</strong>
                          <small>{dateOnly(d.start_ts)} → {dateOnly(d.end_ts)}</small>
                        </td>
                        <td className="right">{d.bars}</td>
                        <td><Tag mono>{d.digest.slice(0, 8)}</Tag></td>
                        <td className="right">
                          <button
                            className="button small danger"
                            onClick={async () => {
                              await api.deleteDataset(d.id)
                              await loadDatasets()
                              notify('数据集已删除')
                            }}
                          >
                            删除
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <Callout>
              上传的 CSV 会保存为一份带摘要的数据集，可在「回测 → 新建回测」里选作数据来源。
              CSV 的真实性、交易对与时间范围由导入者自行确认。
            </Callout>
          </div>
        </div>
      </Panel>
    </>
  )
}
