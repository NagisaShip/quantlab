import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { api, dateOnly, shortTime, SOURCE_LABEL, type BarDTO, type Quality } from '../api'
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
import { PriceChart } from '../components/charts'
import type { PageProps } from './types'

export default function DataCenterPage({ notify, onNavigate, arg }: PageProps & { arg?: string }) {
  const tab = arg === 'hub' ? 'hub' : 'bars'
  const heading = (
    <>
      <Heading
        eyebrow="MARKET & DATA"
        title="行情与数据"
        description="行情 K 线与研究数据（衍生品、链上、情绪、宏观）都在这里。真实数据优先；拿不到时回退到可复现的合成序列并明确标注。"
      />
      <div className="fx-tabs">
        <Segments value={tab} onChange={(t) => onNavigate(t === 'hub' ? 'data/hub' : 'data')} ariaLabel="行情与数据视图"
          options={[{ value: 'bars', label: '行情 K 线' }, { value: 'hub', label: '研究数据' }]} />
      </div>
    </>
  )
  if (tab === 'hub') return <>{heading}<DatahubSection notify={notify} /></>
  return <BarsView notify={notify} heading={heading} />
}

function BarsView({ notify, heading }: Pick<PageProps, 'notify'> & { heading: ReactNode }) {
  const [symbols, setSymbols] = useState<string[]>([])
  const [timeframes, setTimeframes] = useState<string[]>([])
  const [symbol, setSymbol] = useState('BTC/USDT')
  const [timeframe, setTimeframe] = useState('1h')
  const [exchange, setExchange] = useState('binance')
  const [bars, setBars] = useState<BarDTO[]>([])
  const [source, setSource] = useState('')
  const [quality, setQuality] = useState<Quality | null>(null)
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
      const [b, q, s] = await Promise.all([
        api.bars(symbol, timeframe, 300, exchange),
        api.quality(symbol, timeframe, 1500, exchange),
        api.marketSummary(symbol, timeframe),
      ])
      setBars(b.bars)
      setSource(b.source)
      setQuality(q)
      setSummary(s)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [symbol, timeframe, exchange])

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
        warn={source === 'synthetic' ? '合成数据只能验证流程与代码正确性，不能用来评估策略的真实表现' : undefined}
        right="只读公共行情"
      />

      <div className="config-bar">
        <Field label="交易对">
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {(symbols.length ? symbols : [symbol]).map((s) => <option key={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="周期">
          <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
            {(timeframes.length ? timeframes : [timeframe]).map((t) => <option key={t}>{t}</option>)}
          </select>
        </Field>
        <Field label="交易所">
          <select value={exchange} onChange={(e) => setExchange(e.target.value)}>
            <option value="binance">Binance</option>
            <option value="okx">OKX</option>
          </select>
        </Field>
        <div style={{ display: 'flex', alignItems: 'flex-end' }}>
          <button className="button primary" onClick={load} disabled={loading}>
            {loading ? <><span className="spinner" /> 读取中</> : '⇄ 读取行情'}
          </button>
        </div>
      </div>

      {error && <Callout tone="fail" title="读取失败">{error}</Callout>}
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
              label="区间最高 / 最低"
              value={`${Math.max(...bars.map((b) => b.h)).toFixed(0)} / ${Math.min(...bars.map((b) => b.l)).toFixed(0)}`}
              caption={`${bars.length} 根 ${timeframe} K 线`}
            />
            <MetricCard
              label="单根波动率"
              value={summary?.volatility_per_bar !== undefined ? Number(summary.volatility_per_bar).toFixed(4) : '—'}
              caption="按 bar 收益率标准差"
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

          <Panel title={`价格走势（${symbol} · ${timeframe}）`} subtitle="简化 K 线：影线为高低价，实体为开盘到收盘">
            <PriceChart bars={bars} />
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
              上传的 CSV 会保存为一份带摘要的数据集，可在「统一赛道与比较」中直接选用。
              CSV 的真实性、交易对与时间范围由导入者自行确认。
            </Callout>
          </div>
        </div>
      </Panel>
    </>
  )
}
