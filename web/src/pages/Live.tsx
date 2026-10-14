import { useCallback, useEffect, useState } from 'react'
import { fmtMoney, fmtTime, tone } from '../api2'
import { MODE_LABEL, v4, type Comparison, type ImportItem, type ImportRowError, type LiveStatus, type Run } from '../api3'
import { ApiError } from '../http'
import { Callout, EmptyState, Field, Heading, Loading, Panel, Segments, Tag } from '../components/ui'
import type { PageProps } from './types'

const KIND_LABEL: Record<string, string> = { fills: '成交', flows: '资金流水', balances: '余额快照' }
const METHOD_LABEL: Record<string, string> = {
  client_order_id: '订单标签', explicit_mapping: '显式映射', exchange_order_id: '交易所订单号', heuristic: '启发式',
}

/**
 * 实盘对照：真实记录（导入 CSV / 只读账户同步）与模拟运行逐笔核对。
 * 红线：没有真实成交时只显示“未接入真实成交”，不展示任何收益曲线，绝不用模拟曲线补位。
 */
export default function LivePage({ notify, onNavigate, arg }: PageProps & { arg?: string }) {
  const [status, setStatus] = useState<LiveStatus | null>(null)
  const [error, setError] = useState('')
  const [imports, setImports] = useState<ImportItem[]>([])
  const load = useCallback(() => {
    /* 首屏加载失败必须能看出来、并且能重试。
       原来失败只 notify 一下就返回，status 永远是 null → `if (!status) return <Loading />`
       变成**永久转圈**：用户既看不到错，也没有重试入口，只能刷新页面。
       把失败显式记进 state，页面才有机会说出"加载失败 + 重试"。 */
    setError('')
    v4.liveStatus().then(setStatus).catch((e) => {
      setError((e as Error).message || '加载失败')
      notify((e as Error).message, true)
    })
    v4.imports().then((r) => setImports(r.items.filter((i) => i.status === 'done'))).catch(() => undefined)
  }, [notify])
  useEffect(load, [load])

  if (error && !status) {
    return (
      <>
        <Heading title="实盘对照" description="真实记录与模拟运行逐笔核对。" />
        <Callout tone="fail">
          实盘状态加载失败：{error}
          <div className="button-row" style={{ marginTop: 10 }}>
            <button className="button small" onClick={load}>重试</button>
          </div>
        </Callout>
      </>
    )
  }
  if (!status) return <Loading />
  return (
    <>
      <Heading title="实盘对照" description="真实记录与模拟运行逐笔核对。真实数据与模拟数据分开存放、分别核算；哪些成交属于策略由归因决定，并给出置信度。" />
      {!status.has_live_data ? (
        <div className="empty-live" style={{ marginBottom: 18 }}>
          <strong>未接入真实成交</strong>
          <p>{status.message.replace(/^未接入真实成交[：:]/, '')}。本页不会用模拟或回测曲线代替实盘结果。你可以导入交易所导出的成交 CSV，或在“只读账户”里连接一个只读 API Key。</p>
          <div className="button-row" style={{ justifyContent: 'center', marginTop: 16 }}>
            <a className="button primary small" href="#import">导入 CSV</a>
            <button className="button small" onClick={() => onNavigate('accounts')}>连接只读账户</button>
          </div>
        </div>
      ) : (
        <LiveSummary status={status} />
      )}
      <div className="two-columns">
        <ComparePanel status={status} notify={notify} initial={arg} />
        <ImportPanel imports={imports} notify={notify} onDone={load} />
      </div>
    </>
  )
}

function LiveSummary({ status }: { status: LiveStatus }) {
  return (
    <Panel title="已接入的真实记录" subtitle={`共 ${status.live_fills} 笔真实成交 · 最近一笔 ${status.last_live_fill_ts ? fmtTime(status.last_live_fill_ts) + ' UTC' : '—'}`}>
      <div className="two-columns even">
        <div className="table-wrap">
          <table>
            <thead><tr><th>来源</th><th>模式</th><th className="right">成交</th><th>时间</th></tr></thead>
            <tbody>
              {status.sources.map((s) => (
                <tr key={s.id}><td>{s.title}</td><td><Tag tone={s.mode === 'readonly_live' ? 'info' : undefined}>{MODE_LABEL[s.mode] ?? s.mode}</Tag></td>
                  <td className="right">{s.fills}</td><td>{fmtTime(s.created_at)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <h3 className="section-title">最新余额快照</h3>
          {status.balances.length === 0 ? <p className="hint">没有余额快照：只读账户同步或导入余额 CSV 后显示。</p> : (
            <table>
              <tbody>{status.balances.map((b) => (
                <tr key={b.currency + b.source}><td>{b.currency}</td><td className="right mono">{b.free}</td><td className="right mono muted">冻结 {b.locked}</td></tr>
              ))}</tbody>
            </table>
          )}
        </div>
      </div>
    </Panel>
  )
}

function ComparePanel({ status, notify, initial }: { status: LiveStatus; notify: PageProps['notify']; initial?: string }) {
  const [sims, setSims] = useState<Run[]>([])
  const [sim, setSim] = useState('')
  const [live, setLive] = useState<string[]>([])
  const [windowMin, setWindowMin] = useState(120)
  const [report, setReport] = useState<Comparison | null>(null)
  const [history, setHistory] = useState<Awaited<ReturnType<typeof v4.comparisons>>['items']>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    Promise.all([v4.runs({ mode: 'paper' }), v4.runs({ mode: 'backtest', state: 'COMPLETED' })]).then(([p, b]) => {
      const list = [...b.items.filter((r) => r.kind === 'backtest'), ...p.items]
      setSims(list)
      if (list[0]) setSim(list[0].id)
    }).catch(() => undefined)
    v4.comparisons().then((r) => setHistory(r.items)).catch(() => undefined)
    if (initial) v4.comparison(initial).then((r) => setReport(r.comparison)).catch(() => undefined)
  }, [initial])
  const sources = [...status.sources, ...(status.test_sources ?? [])]
  const run = async () => {
    setBusy(true)
    try {
      const r = await v4.compare({ sim_run_id: sim, live_run_ids: live, window_minutes: windowMin })
      if (r.comparison) setReport(r.comparison)
      else notify('对照已排队，稍后在历史里查看')
      v4.comparisons().then((x) => setHistory(x.items)).catch(() => undefined)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div>
      <Panel title="生成对照报告" subtitle="匹配顺序：订单标签 / 显式映射 → 交易所订单号 → 按品种、方向、时间、数量启发式匹配（带置信度）。">
        {sources.length === 0 ? (
          <p className="hint">还没有真实记录来源。先导入 CSV 或同步只读账户。</p>
        ) : (
          <>
            <div className="form-grid">
              <Field label="模拟运行（纸面模拟或回测）">
                <select value={sim} onChange={(e) => setSim(e.target.value)}>
                  {sims.map((r) => <option key={r.id} value={r.id}>{MODE_LABEL[r.mode]} · {r.title} · {fmtTime(r.created_at)}</option>)}
                </select>
              </Field>
              <Field label="启发式时间窗（分钟）"><input type="number" min={1} value={windowMin} onChange={(e) => setWindowMin(Number(e.target.value))} /></Field>
            </div>
            <Field label="真实记录来源">
              <div style={{ display: 'grid', gap: 4, marginTop: 6 }}>
                {sources.map((s) => (
                  <label key={s.id} className="check-row">
                    <input type="checkbox" checked={live.includes(s.id)} onChange={(e) => setLive(e.target.checked ? [...live, s.id] : live.filter((x) => x !== s.id))} />
                    {s.title} <Tag>{MODE_LABEL[s.mode]}</Tag> <span className="muted">{s.fills} 笔</span>
                  </label>
                ))}
              </div>
            </Field>
            <div className="button-row" style={{ marginTop: 14 }}>
              <button className="button primary" disabled={busy || !sim || live.length === 0} onClick={run}>{busy ? '对照中…' : '生成对照'}</button>
            </div>
          </>
        )}
      </Panel>
      {report && <ReportView r={report} />}
      {history.length > 0 && (
        <Panel title="历史对照" flush>
          <table>
            <thead><tr><th>时间</th><th className="right">真实成交</th><th className="right">已归因</th><th className="right">未归因</th><th className="right">平均滑点</th><th /></tr></thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.id}>
                  <td>{fmtTime(h.created_at)} {h.labels?.test && <Tag tone="warn">测试</Tag>}</td>
                  <td className="right">{h.summary?.live_fills}</td><td className="right">{h.summary?.attributed}</td>
                  <td className="right">{h.summary?.unattributed}</td>
                  <td className="right">{h.summary?.avg_slippage_bps === null ? '—' : `${h.summary?.avg_slippage_bps} bps`}</td>
                  <td className="right"><button className="button small ghost" onClick={() => v4.comparison(h.id).then((r) => setReport(r.comparison))}>查看</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  )
}

/** 模拟价按真实价的小数位显示（只影响显示，原值保留在报告 JSON 里）。 */
function samePrecision(value: string, like: string): string {
  const dp = like.includes('.') ? like.split('.')[1].length : 0
  const n = Number(value)
  return Number.isFinite(n) ? n.toFixed(Math.max(dp, 2)) : value
}

function ReportView({ r }: { r: Comparison }) {
  const s = r.summary
  return (
    <Panel title={<>对照报告 {r.labels?.test && <Tag tone="warn">测试网 · 不代表实盘</Tag>}</>} subtitle={`报告 ${r.id}`}>
      {r.notes.map((n) => <Callout key={n} tone="warn">{n}</Callout>)}
      <div className="metrics-grid" style={{ margin: '12px 0' }}>
        <article className="metric-card"><div className="metric-label"><span>已归因 / 真实成交</span></div><div className="metric-number">{s.attributed}<small>/ {s.live_fills}</small></div></article>
        <article className="metric-card"><div className="metric-label"><span>平均置信度</span></div><div className="metric-number">{s.avg_confidence === null ? '—' : s.avg_confidence.toFixed(2)}</div></article>
        <article className="metric-card"><div className="metric-label"><span>平均滑点</span></div><div className="metric-number">{s.avg_slippage_bps ?? '—'}<small>bps</small></div></article>
        <article className="metric-card"><div className="metric-label"><span>已归因已实现盈亏</span></div><div className={`metric-number ${tone(s.attributed_result.realized_pnl)}`}>{fmtMoney(s.attributed_result.realized_pnl)}</div>
          <div className="metric-foot">手续费 {fmtMoney(s.attributed_result.fees, 4)} · 只统计已归因成交</div></article>
        {/* T08：拆单。交易所把 1 个订单拆成多笔成交时，这里显示"归并了多少个订单"，
            避免把"成交笔数 > 报告行数"误读成丢了成交。 */}
        {s.merged_orders > 0 && (
          <article className="metric-card">
            <div className="metric-label"><span>被拆单的订单</span></div>
            <div className="metric-number">{s.merged_orders}<small>个</small></div>
            <div className="metric-foot">已按订单归并为一行，价格取数量加权均价</div>
          </article>
        )}
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>真实成交</th><th>方向</th><th className="right">真实价</th><th className="right">模拟价</th><th className="right">滑点</th><th className="right">延迟</th><th>匹配方式</th><th>置信度</th></tr></thead>
          <tbody>
            {r.pairs.map((p, i) => (
              <tr key={i}>
                <td>{fmtTime(p.live.ts)} · {p.live.instrument}
                  {(p.live.fills ?? 1) > 1 && <span className="muted"> · {p.live.fills} 笔合并</span>}</td>
                <td><span className={`side ${p.live.side}`}>{p.live.side === 'buy' ? '买入' : '卖出'}</span></td>
                <td className="right mono">{p.live.price}</td>
                <td className="right mono">{p.sim?.price === undefined || p.sim?.price === null ? '—' : samePrecision(String(p.sim.price), String(p.live.price))}</td>
                <td className={`right ${p.slippage_bps && p.slippage_bps > 0 ? 'negative' : ''}`}>{p.slippage_bps === null || p.slippage_bps === undefined ? '—' : `${p.slippage_bps.toFixed(1)} bps`}</td>
                <td className="right">{p.delay_ms === undefined ? '—' : `${Math.round(p.delay_ms / 1000)} 秒`}</td>
                <td>{METHOD_LABEL[p.method] ?? p.method}</td>
                <td><span className="conf-bar"><i style={{ width: `${p.confidence * 100}%` }} /></span>{p.confidence.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {r.unattributed.length > 0 && (
        <>
          <h3 className="section-title" style={{ marginTop: 18 }}>无法归因的真实成交（未计入策略结果）</h3>
          <div className="table-wrap"><table>
            <thead><tr><th>时间</th><th>品种</th><th>方向</th><th className="right">数量</th><th className="right">价格</th></tr></thead>
            <tbody>{r.unattributed.map((f, i) => (
              <tr key={i}><td>{fmtTime(Number(f.ts))}</td><td>{String(f.instrument)}</td>
                <td><span className={`side ${String(f.side)}`}>{f.side === 'buy' ? '买入' : '卖出'}</span></td>
                <td className="mono right">{String(f.qty)}</td><td className="mono right">{String(f.price)}</td></tr>
            ))}</tbody>
          </table></div>
        </>
      )}
      {r.unmatched_sim.length > 0 && <p className="hint">另有 {r.unmatched_sim.length} 笔模拟成交没有对应的真实成交（例如没有执行、执行在时间窗之外）。</p>}
    </Panel>
  )
}

function ImportPanel({ imports, notify, onDone }: { imports: ImportItem[]; notify: PageProps['notify']; onDone: () => void }) {
  const [kind, setKind] = useState<'fills' | 'flows' | 'balances'>('fills')
  const [env, setEnv] = useState<'live' | 'testnet'>('live')
  const [exchange, setExchange] = useState('binance')
  const [file, setFile] = useState<{ name: string; text: string } | null>(null)
  const [errors, setErrors] = useState<ImportRowError[]>([])
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    if (!file) return
    setBusy(true)
    setErrors([])
    try {
      const r = await v4.importCsv({ kind, filename: file.name, content: file.text, environment: env, exchange })
      notify(`已导入 ${r.import.rows} 行`)
      setFile(null)
      onDone()
    } catch (e) {
      if (e instanceof ApiError && Array.isArray((e.details as { errors?: ImportRowError[] }).errors)) setErrors((e.details as { errors: ImportRowError[] }).errors)
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div id="import">
      <Panel title="导入真实记录" subtitle="交易所导出的 CSV。全有或全无：任何一行不合法都不入库；以 = + - @ 开头的非数字单元格视为公式注入并拒绝。">
        <div style={{ display: 'grid', gap: 12 }}>
          <Segments value={kind} onChange={setKind} options={[{ value: 'fills', label: '成交' }, { value: 'flows', label: '资金流水' }, { value: 'balances', label: '余额' }]} />
          <div className="form-grid">
            <Field label="环境"><select value={env} onChange={(e) => setEnv(e.target.value as 'live' | 'testnet')}>
              <option value="live">实盘账户</option><option value="testnet">测试网（结果标为测试）</option>
            </select></Field>
            <Field label="交易所"><select value={exchange} onChange={(e) => setExchange(e.target.value)}>
              <option value="binance">Binance</option><option value="okx">OKX</option><option value="">其它</option>
            </select></Field>
          </div>
          <Field label="CSV 文件" hint="列名自动识别（含中英文常见列名），时间按 UTC 解释，金额按十进制精确保存。">
            <input type="file" accept=".csv,text/csv" onChange={async (e) => {
              const f = e.target.files?.[0]
              if (f) setFile({ name: f.name, text: await f.text() })
            }} />
          </Field>
          <div className="button-row"><button className="button primary" disabled={!file || busy} onClick={submit}>{busy ? '校验中…' : '校验并导入'}</button></div>
          {errors.length > 0 && (
            <div className="callout fail">
              {errors.slice(0, 12).map((er, i) => <div key={i}>第 {er.row} 行 {er.field && `[${er.field}]`}：{er.message}</div>)}
              {errors.length > 12 && <div>…共 {errors.length} 处</div>}
            </div>
          )}
        </div>
      </Panel>
      <Panel title="导入记录" flush>
        {imports.length === 0 ? <EmptyState title="还没有导入" glyph="⇩" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>文件</th><th>类型</th><th className="right">行数</th><th>区间</th><th /></tr></thead>
            <tbody>
              {imports.map((i) => (
                <tr key={i.id}>
                  <td>{i.filename} {i.report.environment === 'testnet' && <Tag tone="warn">测试</Tag>}{i.report.warnings?.map((w) => <small key={w} className="muted" style={{ display: 'block' }}>{w}</small>)}</td>
                  <td>{KIND_LABEL[i.kind]}</td><td className="right">{i.rows}</td>
                  <td>{i.report.period ? `${fmtTime(i.report.period[0]).slice(0, 10)} → ${fmtTime(i.report.period[1]).slice(0, 10)}` : '—'}</td>
                  <td className="right">
                    {i.kind === 'fills' && <a className="button small ghost" href={v4.importExportUrl(i.id)}>导出</a>}
                    <button className="button small danger" onClick={async () => {
                      if (!window.confirm('删除这次导入及其全部数据？')) return
                      try { await v4.deleteImport(i.id); onDone() } catch (e) { notify((e as Error).message, true) }
                    }}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>
        )}
      </Panel>
    </div>
  )
}
