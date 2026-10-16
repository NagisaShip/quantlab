import { csvCell } from '../http'
import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  downloadText,
  fmtDate,
  fmtMoney,
  fmtQty,
  fmtNum,
  fmtPct,
  fmtTime,
  tone,
  v3,
  type BacktestResult,
  type CopilotResult,
  type DailyRow,
} from '../api2'
import { CodeView } from '../components/CodeEditor'
import { BarChart, COLORS, LineChart, seriesColor } from '../components/charts2'
import { Markdown } from '../components/markdown'
import { Callout, EmptyState, Loading, Modal, Panel, Tag } from '../components/ui'
import { cumulative } from './Editor'
import { openInLab } from '../components/handoff'
import { backtestAskContext, useAskAIContext } from '../components/AskAI'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

type Tab = 'fills' | 'breakdown' | 'daily' | 'logs' | 'code' | 'manifest' | 'ai' | 'ledger' | 'signals' | 'guards'
const COMPARE_KEY = 'qa.compare'

export function readCompare(): string[] {
  try {
    return JSON.parse(localStorage.getItem(COMPARE_KEY) || '[]')
  } catch {
    return []
  }
}
export function writeCompare(ids: string[]) {
  try {
    localStorage.setItem(COMPARE_KEY, JSON.stringify(ids.slice(-8)))
  } catch {
    /* 忽略 */
  }
}

export default function BacktestDetailPage({ id, onNavigate, notify }: PageProps & { id: string }) {
  const [bt, setBt] = useState<BacktestResult | null>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<Tab>('fills')
  const [chart, setChart] = useState<'returns' | 'excess' | 'drawdown' | 'daily' | 'records'>('returns')
  const [ai, setAi] = useState<CopilotResult | null>(null)
  const [aiBusy, setAiBusy] = useState(false)
  const [share, setShare] = useState(false)
  const [inCompare, setInCompare] = useState(() => readCompare().includes(id))

  useEffect(() => {
    v3.backtest(id).then(setBt).catch((e) => setError(e.message))
  }, [id])
  useAskAIContext(bt ? backtestAskContext(bt) : null)

  const series = useMemo(() => {
    if (!bt) return null
    const s = cumulative(bt.equity_curve)
    const b = cumulative(bt.benchmark_curve)
    const excess = s.map((v, i) => (1 + v) / (1 + (b[i] ?? 0)) - 1)
    return { s, b, excess, dd: bt.drawdown.map((d) => -d) }
  }, [bt])

  if (error) return <Callout tone="fail" title="无法读取回测">{error}</Callout>
  if (!bt || !series) return <Loading label="正在读取回测" />

  const m = bt.metrics
  const row = bt.row
  const synthetic = bt.datasets.some((d) => d.source === 'synthetic')

  const interpret = async () => {
    setTab('ai')
    if (ai) return
    setAiBusy(true)
    try {
      setAi(await v3.aiInterpret(id))
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setAiBusy(false)
    }
  }

  const toggleCompare = () => {
    const ids = readCompare()
    const next = ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]
    writeCompare(next)
    setInCompare(next.includes(id))
    notify(next.includes(id) ? `已加入对比（共 ${next.length} 个）` : '已移出对比')
  }

  const remove = async () => {
    if (!await appConfirm('删除这条回测记录？')) return
    await v3.deleteBacktest(id)
    notify('已删除')
    onNavigate(row?.slug ? `backtests/${row.slug}` : 'backtests')
  }

  const initCash = Number(row?.initial_cash ?? 0) || null
  // 手续费占初始资金：比策略收益本身还大时标红，提醒成本吃掉了收益
  const feeShare = initCash && m.total_fees !== null && m.total_fees !== undefined ? Number(m.total_fees) / initCash : null
  const overview: [string, string, string?][] = [
    ['策略收益', fmtPct(m.total_return, 2, true), tone(m.total_return)],
    ['策略年化收益', fmtPct(m.annualized_return, 2, true), tone(m.annualized_return)],
    ['基准收益', fmtPct(m.benchmark_return, 2, true)],
    ['超额收益', fmtPct(m.excess_return, 2, true), tone(m.excess_return)],
    ['Alpha', fmtNum(m.alpha, 3)],
    ['Beta', fmtNum(m.beta, 3)],
    ['夏普比率', fmtNum(m.sharpe, 3)],
    ['索提诺比率', fmtNum(m.sortino, 3)],
    ['信息比率', fmtNum(m.information_ratio, 3)],
    ['策略波动率', fmtPct(m.volatility)],
    ['基准波动率', fmtPct(m.benchmark_volatility)],
    ['最大回撤', fmtPct(m.max_drawdown)],
    ['胜率', fmtPct(m.win_rate)],
    ['日胜率', fmtPct(m.daily_win_rate)],
    ['盈亏比', fmtNum(m.payoff_ratio)],
    ['盈利天数占比', fmtPct(m.profit_day_ratio)],
    ['交易次数', `${m.trades} 笔 / ${m.fills} 次成交`],
    ['换手率', `${fmtNum(m.turnover)} 倍`],
    ['手续费', `${fmtMoney(m.total_fees)}${feeShare !== null ? `（初始资金的 ${(feeShare * 100).toFixed(1)}%）` : ''}`, feeShare !== null && feeShare > Math.abs(m.total_return ?? 0) ? 'negative' : undefined],
    ['期末净值', fmtMoney(m.final_equity)],
  ]
  const recordKeys = Object.keys(bt.records || {})

  return (
    <div>
      <div className="editor-toolbar">
        <button className="button small" onClick={() => onNavigate(row?.slug ? `editor/${row.slug}` : 'strategies')}>← {row?.slug ? '回到编辑器' : '我的策略'}</button>
        <h1 className="toolbar-title">{bt.strategy_name}</h1>
        <Tag mono>{row?.version ?? ''}</Tag>
        <span className={`table-pill ${bt.status === 'done' ? '' : 'fail'}`}>{bt.status === 'done' ? '回测完成' : '运行有错误'}</span>
        <div className="spacer" />
        <button className="button small" onClick={interpret}>✦ AI 解读</button>
        {bt.code && (
          <button className="button small" title="把这次回测的代码和成绩带进 AI 研究室继续改进"
            onClick={() => openInLab({
              idea: '根据这次回测的成绩改进策略，优先解决最明显的问题：', title: bt.strategy_name, code: bt.code,
              securities: bt.securities, timeframe: bt.timeframe, attached: `「${bt.strategy_name}」的回测代码与成绩`,
              pins: [{ kind: 'note', title: '来源回测成绩', text: overview.map(([k, v]) => `${k}：${v}`).join('\n') }],
            })}>用 AI 改进</button>
        )}
        <button className="button small" onClick={toggleCompare}>{inCompare ? '✓ 已加入对比' : '加入对比'}</button>
        <button className="button small" onClick={() => onNavigate('backtests')}>回测列表</button>
        <button className="button small primary" onClick={() => setShare(true)}>分享到广场</button>
        <button className="button small" onClick={() => downloadText(`${id}.json`, JSON.stringify(bt, null, 2), 'application/json')}>导出报告</button>
        <button className="button small danger" onClick={remove}>删除</button>
      </div>

      <div className={`source-banner ${synthetic ? 'warn' : 'real'}`}>
        <span className="status-icon">◈</span>
        <div>
          <strong>{bt.datasets.map((d) => `${d.symbol} · ${d.source_label}`).join('；')}</strong>
          <span>{fmtTime(bt.timestamps[0])} → {fmtTime(bt.timestamps[bt.timestamps.length - 1])} UTC · {bt.timeframe} · 初始资金 {fmtMoney(bt.initial_cash, 0)} · {String(bt.manifest.fill_model ?? '')}</span>
        </div>
        <span className="banner-right">基准 {bt.benchmark}</span>
      </div>
      {bt.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
      {bt.errors.length > 0 && <Callout tone="fail" title={`${bt.errors.length} 个运行错误`}>{bt.errors.slice(0, 5).join('；')}</Callout>}
      {bt.unfilled > 0 && <Callout>回测结束时有 {bt.unfilled} 笔委托因没有下一根 bar 而未成交，不影响以上指标。</Callout>}

      <Panel title="收益概述" subtitle="指标口径与聚宽一致：超额收益 = (1+策略)/(1+基准)−1；Alpha、Beta 按逐 bar 收益计算，无风险利率取 0。样本不足或零波动时显示 —">
        <div className="overview-grid">
          {overview.map(([label, value, cls]) => (
            <div key={label}><span>{label}</span><b className={cls ?? ''}>{value}</b></div>
          ))}
        </div>
        {m.notes?.length > 0 && <div className="hint" style={{ marginTop: 10 }}>{m.notes.join('；')}</div>}
      </Panel>

      <Panel
        title="收益曲线"
        actions={
          <div className="segments">
            {([
              ['returns', '累计收益'],
              ['excess', '超额收益'],
              ['drawdown', '回撤'],
              ['daily', '日收益'],
              ...(recordKeys.length ? [['records', `自定义 (${recordKeys.length})`]] : []),
            ] as [typeof chart, string][]).map(([k, l]) => (
              <button key={k} aria-pressed={chart === k} onClick={() => setChart(k)}>{l}</button>
            ))}
          </div>
        }
      >
        {chart === 'returns' && (
          <LineChart timestamps={bt.timestamps} series={[
            { name: '策略收益', values: series.s, color: COLORS.strategy, area: true },
            { name: `基准收益（${bt.benchmark}）`, values: series.b, color: COLORS.benchmark, dashed: true },
          ]} height={320} ariaLabel="策略与基准累计收益" />
        )}
        {chart === 'excess' && (
          <LineChart timestamps={bt.timestamps} series={[{ name: '超额收益', values: series.excess, color: COLORS.excess, area: true }]} height={320} />
        )}
        {chart === 'drawdown' && (
          <LineChart timestamps={bt.timestamps} series={[{ name: '回撤（自历史高点）', values: series.dd, color: 'var(--muted)', area: true }]} height={320} />
        )}
        {chart === 'daily' && (
          <BarChart values={bt.daily.map((d) => d.return)} labels={bt.daily.map((d) => d.date)} height={260} ariaLabel="每日收益" />
        )}
        {chart === 'records' && <RecordsChart records={bt.records} />}
      </Panel>

      <div className="tabs wide">
        {([
          ['fills', `交易详情 (${bt.fills_total})`],
          ['breakdown', '交易分解'],
          ...(guardsOf(bt) ? [['guards', '保护与标的池'] as [Tab, string]] : []),
          ['daily', `每日持仓&收益 (${bt.daily.length})`],
          ['logs', `日志输出 (${bt.logs_total})`],
          ['ledger', '账本与口径'],
          ['signals', `信号 (${bt.signals_total ?? bt.signals?.length ?? 0})`],
          ['code', '策略代码'],
          ['manifest', '运行清单'],
          ['ai', 'AI 解读'],
        ] as [Tab, string][]).map(([k, label]) => (
          <button key={k} aria-pressed={tab === k} onClick={() => (k === 'ai' ? interpret() : setTab(k))}>{label}</button>
        ))}
      </div>
      <section className="panel">
        <div className="panel-body tab-panel">
          {tab === 'fills' && <FillsTable bt={bt} />}
          {tab === 'breakdown' && <BreakdownView bt={bt} />}
          {tab === 'guards' && <GuardsView bt={bt} />}
          {tab === 'daily' && <DailyTable rows={bt.daily} />}
          {tab === 'logs' && (
            <div className="logs">
              {bt.logs.filter(Boolean).map((l, i) => <div key={i} className={`log-line ${l.level}`}><span>{fmtTime(l.ts)}</span><b>{l.level.toUpperCase()}</b>{l.message}</div>)}
              {!bt.logs.length && <EmptyState glyph="≡" title="策略没有输出日志">在策略里用 log.info("...") 输出关键判断，回测时会按时间记录在这里。</EmptyState>}
            </div>
          )}
          {tab === 'code' && (bt.code ? <CodeView code={bt.code} maxHeight={640} /> : <EmptyState title="没有保存源码快照" />)}
          {tab === 'manifest' && <Manifest bt={bt} />}
          {tab === 'ledger' && <LedgerView bt={bt} />}
          {tab === 'signals' && <SignalsTable bt={bt} />}
          {tab === 'ai' && (aiBusy ? <Loading label="AI 正在解读" /> : ai ? (
            <div>
              <div className="ai-result-head"><Tag tone={ai.mode === 'llm' ? 'agent' : 'warn'}>{ai.mode === 'llm' ? ai.model : '规则解读（未调用大模型）'}</Tag></div>
              <Markdown text={ai.content} />
            </div>
          ) : <EmptyState title="点击上方「AI 解读」" />)}
        </div>
      </section>

      {share && <ShareModal bt={bt} id={id} onClose={() => setShare(false)} onDone={(pid) => onNavigate(`post/${pid}`)} notify={notify} />}
    </div>
  )
}

function RecordsChart({ records }: { records: BacktestResult['records'] }) {
  const keys = Object.keys(records)
  const [pick, setPick] = useState<string[]>(keys.slice(0, 2))
  const active = pick.filter((k) => records[k]?.length > 1)
  const base = active.length ? records[active[0]] : []
  return (
    <div>
      <div className="chip-row" style={{ padding: '0 0 10px' }}>
        {keys.map((k) => (
          <button key={k} className={`chip${pick.includes(k) ? ' on' : ''}`} onClick={() => setPick(pick.includes(k) ? pick.filter((x) => x !== k) : [...pick, k])}>{k}</button>
        ))}
      </div>
      {active.length ? (
        <LineChart
          timestamps={base.map((p) => p.t)}
          format="num"
          zeroLine={false}
          series={active.map((k, i) => ({
            name: k,
            values: base.map((p) => {
              const hit = records[k].find((q) => q.t === p.t)
              return hit ? Number(hit.v) : NaN
            }),
            color: seriesColor(i),
          }))}
          height={300}
        />
      ) : <EmptyState title="选择要显示的 record 序列" />}
    </div>
  )
}

type BreakdownRow = { key: string; trades: number; win_rate: number; avg_return: number; pnl: string | number; avg_holding_hours: number | null }

const BREAKDOWN_DIMS: [string, string][] = [
  ['entry_tag', '入场标签'], ['exit_reason', '出场原因'], ['security', '标的'], ['weekday', '星期（UTC）'], ['hour', '开仓小时（UTC）'],
]

const EXIT_NAMES: Record<string, string> = {
  stop_loss: '固定止损', trailing_stop: '移动止损', custom_stop: '动态止损', exchange_stop: '交易所止损单',
  take_profit: '固定止盈', roi: '分段止盈', custom_exit: '自定义平仓', liquidation: '强平', liquidate: '一键清仓',
}

type GuardLock = { method: string; security: string; from: number; until: number; reason: string }
type GuardReport = {
  protections: { method: string }[]; locks: GuardLock[]; blocked: Record<string, number>
  pairlist: Record<string, unknown> | null; universe: string[] | null
  universe_history: { ts: number; selected: string[]; dropped: Record<string, string>; reason: string }[]
  delisted: Record<string, number>
}
const PROT_NAMES: Record<string, string> = { CooldownPeriod: '冷却期', StoplossGuard: '连续止损暂停', MaxDrawdown: '回撤暂停', LowProfitPairs: '低收益标的暂停' }
const BLOCK_NAMES: Record<string, string> = { PROTECTION_LOCK: '保护规则锁定中', NOT_IN_UNIVERSE: '不在标的池', DELISTED: '已公告下架' }
const guardsOf = (bt: BacktestResult) => (bt.metrics as unknown as { guards?: GuardReport }).guards

function GuardsView({ bt }: { bt: BacktestResult }) {
  const g = guardsOf(bt)
  if (!g) return null
  const blocked = Object.entries(g.blocked)
  const hist = g.universe_history.filter((h) => h.reason === 'delisting' || Object.keys(h.dropped).length || h === g.universe_history[0])
  return (
    <>
      <p className="hint">保护规则只拦开仓，不拦止损和平仓。标的池每次按当时能看到的数据重选，不会用到后来才知道的信息。</p>
      {blocked.length > 0 && (
        <div className="chips">{blocked.map(([k, n]) => <Tag key={k}>{BLOCK_NAMES[k] ?? k} · 拦下 {n} 次</Tag>)}</div>
      )}
      {g.protections.length > 0 && (
        <>
          <h3>锁定记录（{g.locks.length}）</h3>
          {g.locks.length ? (
            <div className="table-wrap">
              <table>
                <thead><tr><th>规则</th><th>范围</th><th>开始 UTC</th><th>结束 UTC</th><th>原因</th></tr></thead>
                <tbody>
                  {g.locks.map((lk, i) => (
                    <tr key={i}>
                      <td>{PROT_NAMES[lk.method] ?? lk.method}</td>
                      <td>{lk.security === '*' ? '全部标的' : lk.security}</td>
                      <td>{fmtTime(lk.from)}</td>
                      <td>{fmtTime(lk.until)}</td>
                      <td className="wrap">{lk.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="hint">设置了 {g.protections.map((p) => PROT_NAMES[p.method] ?? p.method).join('、')}，整段回测没有触发。</p>}
        </>
      )}
      {g.pairlist && (
        <>
          <h3>标的池变化</h3>
          {g.universe && <p>期末池子：{g.universe.length ? g.universe.join('、') : '空'}</p>}
          {Object.keys(g.delisted).length > 0 && <p className="hint">因下架公告移出：{Object.entries(g.delisted).map(([c, at]) => `${c}（${fmtTime(at)}）`).join('、')}</p>}
          <div className="table-wrap">
            <table>
              <thead><tr><th>时间 UTC</th><th>入选</th><th>移出</th></tr></thead>
              <tbody>
                {hist.slice(-60).map((h, i) => (
                  <tr key={i}>
                    <td>{fmtTime(h.ts)}</td>
                    <td className="wrap">{h.selected.join('、') || '空'}</td>
                    <td className="wrap">{Object.entries(h.dropped).map(([s, why]) => `${s}（${why}）`).join('、') || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}

function BreakdownView({ bt }: { bt: BacktestResult }) {
  const [dim, setDim] = useState('entry_tag')
  const bd = (bt.metrics as unknown as { breakdown?: Record<string, BreakdownRow[]> }).breakdown
  if (!bd) return <EmptyState title="这次回测没有交易分解">交易分解从这一版开始统计，重新运行回测即可看到。</EmptyState>
  const rows = [...(bd[dim] ?? [])].sort((a, b) => (dim === 'hour' || dim === 'weekday' ? 0 : b.trades - a.trades))
  if (!Object.values(bd).some((r) => r.length)) {
    return <EmptyState glyph="⇄" title="还没有完成的交易">开仓后平仓才算一笔完成交易；期末未平仓的不计入。</EmptyState>
  }
  return (
    <>
      <div className="filter-row">
        <div className="segments">
          {BREAKDOWN_DIMS.map(([k, l]) => <button key={k} aria-pressed={dim === k} onClick={() => setDim(k)}>{l}</button>)}
        </div>
        <span className="hint">按已完成交易统计。下单时写 reason="标签" 就能按标签分开看。</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>{BREAKDOWN_DIMS.find(([k]) => k === dim)?.[1]}</th><th className="right">交易数</th><th className="right">胜率</th><th className="right">平均收益</th><th className="right">盈亏合计</th><th className="right">平均持仓</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key}>
                <td>{dim === 'exit_reason' ? (EXIT_NAMES[r.key] ? `${EXIT_NAMES[r.key]}（${r.key}）` : r.key) : r.key}</td>
                <td className="right mono">{r.trades}</td>
                <td className="right mono">{fmtPct(r.win_rate, 1)}</td>
                <td className={`right mono ${tone(r.avg_return)}`}>{fmtPct(r.avg_return, 2, true)}</td>
                <td className={`right mono ${tone(Number(r.pnl))}`}>{fmtMoney(Number(r.pnl))}</td>
                <td className="right mono">{r.avg_holding_hours != null ? `${Number(r.avg_holding_hours).toFixed(1)} 小时` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function FillsTable({ bt }: { bt: BacktestResult }) {
  const [sec, setSec] = useState('')
  const [side, setSide] = useState('')
  const [page, setPage] = useState(0)
  const rows = bt.fills.filter((f) => (!sec || f.security === sec) && (!side || f.side === side))
  const size = 50
  const pages = Math.max(1, Math.ceil(rows.length / size))
  if (!bt.fills.length) return <EmptyState glyph="⇄" title="这次回测没有成交">检查信号条件是否过严、数据是否足够预热，或查看日志里的"低于容差"提示。</EmptyState>
  return (
    <>
      <div className="filter-row">
        <select value={sec} onChange={(e) => { setSec(e.target.value); setPage(0) }}><option value="">全部标的</option>{bt.securities.map((s) => <option key={s}>{s}</option>)}</select>
        <select value={side} onChange={(e) => { setSide(e.target.value); setPage(0) }}><option value="">买入与卖出</option><option value="buy">买入</option><option value="sell">卖出</option></select>
        <span className="hint">共 {rows.length} 次成交{bt.fills_total > bt.fills.length ? `（仅载入前 ${bt.fills.length} 条）` : ''}</span>
        <button className="button small" onClick={() => downloadText(`fills.csv`, ['时间,标的,方向,数量,成交价,成交额,手续费,平仓盈亏,原因,备注', ...rows.map((f) => [fmtTime(f.ts), f.security, f.side, f.amount, f.price, f.notional, f.fee, f.realized_pnl, f.reason, f.note].map(csvCell).join(','))].join('\n'), 'text/csv;charset=utf-8')}>导出 CSV</button>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr><th>成交时间 UTC</th><th>标的</th><th>方向</th><th className="right">数量</th><th className="right">成交价</th><th className="right">成交额</th><th className="right">手续费</th><th className="right">平仓盈亏</th><th>原因</th></tr></thead>
          <tbody>
            {rows.slice(page * size, page * size + size).map((f, i) => (
              <tr key={i}>
                <td>{fmtTime(f.ts)}</td>
                <td>{f.security}</td>
                <td><span className={`side ${f.side}`}>{f.side === 'buy' ? '买入' : '卖出'}</span>{f.partial && <small>部分成交</small>}</td>
                <td className="right">{fmtQty(f.amount)}</td>
                <td className="right">{fmtMoney(f.price, Number(f.price) > 100 ? 2 : 4)}</td>
                <td className="right">{fmtMoney(f.notional)}</td>
                <td className="right">{fmtMoney(f.fee, 4)}</td>
                <td className={`right ${tone(f.realized_pnl)}`}>{f.side === 'sell' ? fmtMoney(f.realized_pnl) : ''}</td>
                <td className="wrap">{f.reason}{f.note && <small>{f.note}</small>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="pager">
          <button className="button small" disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</button>
          <span>{page + 1} / {pages}</span>
          <button className="button small" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>下一页</button>
        </div>
      )}
    </>
  )
}

function DailyTable({ rows }: { rows: DailyRow[] }) {
  const [open, setOpen] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const ordered = [...rows].reverse()
  const size = 40
  const pages = Math.max(1, Math.ceil(ordered.length / size))
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th>日期</th><th className="right">当日收益</th><th className="right">基准当日</th><th className="right">累计收益</th><th className="right">总资产</th><th className="right">持仓市值</th><th className="right">现金</th><th>持仓</th></tr></thead>
          <tbody>
            {ordered.slice(page * size, page * size + size).map((d) => (
              <Fragment key={d.date}>
                <tr className="clickable" onClick={() => setOpen(open === d.date ? null : d.date)}>
                  <td>{d.date}</td>
                  <td className={`right ${tone(d.return)}`}>{fmtPct(d.return, 2, true)}</td>
                  <td className={`right ${tone(d.benchmark_return)}`}>{fmtPct(d.benchmark_return, 2, true)}</td>
                  <td className={`right ${tone(d.cum_return)}`}>{fmtPct(d.cum_return, 2, true)}</td>
                  <td className="right">{fmtMoney(d.equity)}</td>
                  <td className="right">{fmtMoney(d.positions_value)}</td>
                  <td className="right">{fmtMoney(d.cash)}</td>
                  <td>{d.positions.length ? d.positions.map((p) => p.security.split('/')[0]).join(', ') : <span className="muted">空仓</span>} {d.positions.length > 0 && <span className="muted">▾</span>}</td>
                </tr>
                {open === d.date && d.positions.length > 0 && (
                  <tr className="sub-row">
                    <td colSpan={8}>
                      <table>
                        <thead><tr><th>标的</th><th className="right">数量</th><th className="right">成本价</th><th className="right">收盘价</th><th className="right">市值</th><th className="right">浮动盈亏</th></tr></thead>
                        <tbody>
                          {d.positions.map((p) => (
                            <tr key={p.security}>
                              <td>{p.security}</td>
                              <td className="right">{fmtQty(p.amount)}</td>
                              <td className="right">{fmtMoney(p.avg_cost, 4)}</td>
                              <td className="right">{fmtMoney(p.last_price, 4)}</td>
                              <td className="right">{fmtMoney(p.value)}</td>
                              <td className={`right ${tone(p.unrealized_pnl)}`}>{fmtMoney(p.unrealized_pnl)}（{fmtPct(p.pnl_ratio, 2, true)}）</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="pager">
          <button className="button small" disabled={page === 0} onClick={() => setPage(page - 1)}>较新</button>
          <span>{page + 1} / {pages}</span>
          <button className="button small" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>较早</button>
        </div>
      )}
    </>
  )
}

function Manifest({ bt }: { bt: BacktestResult }) {
  const mf = bt.manifest as Record<string, unknown>
  return (
    <div className="manifest">
      <p className="hint">复现这次回测所需的全部条件。代码哈希与数据摘要相同，结果就应当相同。</p>
      <dl className="kv">
        <dt>平台版本</dt><dd>{String(mf.platform_version)}</dd>
        <dt>代码 SHA-256</dt><dd className="mono">{String(mf.code_sha256)}</dd>
        <dt>参数</dt><dd className="mono">{JSON.stringify(bt.params)}</dd>
        <dt>成交模型</dt><dd>{String(mf.fill_model)}</dd>
        <dt>成本模型</dt><dd className="mono">{JSON.stringify(bt.cost_model)}</dd>
        <dt>预热</dt><dd>{String(mf.warmup_bars)} 根 bar（不计成绩）</dd>
        <dt>耗时</dt><dd>{bt.duration_ms} ms</dd>
      </dl>
      <h4 style={{ margin: '18px 0 8px' }}>数据</h4>
      <table>
        <thead><tr><th>标的</th><th>来源</th><th className="right">bar 数</th><th>起止（含预热）</th><th>摘要</th></tr></thead>
        <tbody>
          {bt.datasets.map((d) => (
            <tr key={d.symbol}><td>{d.symbol}</td><td>{d.source_label}{d.exchange ? ` · ${d.exchange}` : ''}</td><td className="right">{d.bars}</td><td>{fmtDate(d.start_ts)} → {fmtDate(d.end_ts)}</td><td className="mono">{d.digest}</td></tr>
          ))}
        </tbody>
      </table>
      {bt.rejected.length > 0 && (
        <>
          <h4 style={{ margin: '18px 0 8px' }}>被拒绝的委托（前 {bt.rejected.length} 条）</h4>
          <ul className="plain">{bt.rejected.slice(0, 20).map((r, i) => <li key={i}>{fmtTime(r.ts)} {r.security} {r.side}：{r.reason}</li>)}</ul>
        </>
      )}
    </div>
  )
}

export function ShareModal({ bt, id, onClose, onDone, notify }: { bt: BacktestResult; id: string; onClose: () => void; onDone: (postId: string) => void; notify: PageProps['notify'] }) {
  const m = bt.metrics
  const [title, setTitle] = useState(`${bt.strategy_name}：${fmtPct(m.total_return, 1, true)}，最大回撤 ${fmtPct(m.max_drawdown, 1)}`)
  const [tags, setTags] = useState<string[]>([])
  const [suggested, setSuggested] = useState<string[]>([])
  const [review, setReview] = useState(true)
  const [body, setBody] = useState(
    `## 思路\n（一两句话说明策略在捕捉什么）\n\n## 参数\n${Object.entries(bt.params).map(([k, v]) => `- \`${k}\` = ${v}`).join('\n') || '- 无'}\n\n## 回测条件\n- 标的：${bt.securities.join('、')}，周期 ${bt.timeframe}\n- 区间：${fmtDate(bt.timestamps[0])} → ${fmtDate(bt.timestamps[bt.timestamps.length - 1])}\n- 数据：${bt.datasets.map((d) => d.source_label).join('、')}\n- 成本：手续费 ${fmtPct(Number(bt.cost_model.taker_fee), 2)}，滑点 ${bt.cost_model.slippage_bps} bps，${String(bt.manifest.fill_model ?? '')}\n\n## 局限\n（适用 / 不适用的行情、还没做的验证）\n`,
  )
  const [busy, setBusy] = useState(false)
  useEffect(() => { v3.tags().then((r) => setSuggested(r.suggested)).catch(() => undefined) }, [])
  const submit = async () => {
    setBusy(true)
    try {
      const r = await v3.createPost({ type: 'strategy', title, body, tags, backtest_id: id, ai_review: review })
      notify('已发布到策略广场')
      onDone(r.post.id)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal title="分享到策略广场" onClose={onClose} width={860}
      footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={busy || title.trim().length < 2} onClick={submit}>{busy ? '发布中…' : '发布'}</button></>}>
      <Callout>帖子会附上这次回测的<strong>冻结快照</strong>（代码哈希、参数、数据来源、成本与成交模型），读者看到的曲线与克隆到的代码是同一份。</Callout>
      <label>标题<input value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} /></label>
      <div className="chip-row" style={{ marginTop: 10 }}>
        {suggested.map((t) => <button key={t} className={`chip${tags.includes(t) ? ' on' : ''}`} onClick={() => setTags(tags.includes(t) ? tags.filter((x) => x !== t) : [...tags, t].slice(0, 8))}>{t}</button>)}
      </div>
      <label style={{ marginTop: 10 }}>正文（Markdown）<textarea rows={14} value={body} onChange={(e) => setBody(e.target.value)} /></label>
      <label className="check"><input type="checkbox" checked={review} onChange={(e) => setReview(e.target.checked)} /> 发布后请 AI 审稿人挑刺（检查交易次数、区间、成本假设、过拟合信号），意见会公开挂在帖子下</label>
    </Modal>
  )
}


const SIG_STATUS: Record<string, string> = {
  filled: '已成交', partial: '部分成交', rejected: '已拒绝', expired: '已过期', tolerance: '低于容差', canceled: '已撤销', accepted: '已受理',
}

/** 账本核对、回撤细节、完成交易与未平仓、口径声明（plan v2.0 报告要求）。 */
function LedgerView({ bt }: { bt: BacktestResult }) {
  const m = bt.metrics as unknown as Record<string, unknown>
  const dd = (m.drawdown_detail ?? {}) as { max_drawdown?: number; peak_ts?: number; trough_ts?: number; recovery_ts?: number | null; recovered?: boolean; drawdown_bars?: number; recovery_bars?: number | null }
  const wd = m.worst_day as { date: string; return: number } | null | undefined
  const L = bt.ledger
  const A = (bt.assumptions ?? {}) as Record<string, unknown>
  const counters = bt.counters ?? {}
  return (
    <div className="two-columns even">
      <div>
        <h3 className="section-title">Decimal 账本核对</h3>
        {L ? (
          <dl className="kv">
            <dt>借贷平衡</dt><dd>{L.balanced ? '✓ 每个币种分录之和为 0' : '✕ 不平衡（请报告）'}</dd>
            <dt>期末权益</dt><dd className="mono">{L.final_equity}</dd>
            <dt>现金</dt><dd className="mono">{L.cash}</dd>
            <dt>净投入资金</dt><dd className="mono">{L.net_contributions}</dd>
            <dt>已实现盈亏</dt><dd className="mono">{L.realized_pnl}</dd>
            <dt>手续费</dt><dd className="mono">{Object.entries(L.fees_by_currency).map(([c, v]) => `${v} ${c}`).join('；') || '0'}</dd>
            <dt>分录数</dt><dd>{L.entries}</dd>
          </dl>
        ) : <p className="hint">该回测生成于旧版本，没有账本核对信息。</p>}
        <h3 className="section-title" style={{ marginTop: 18 }}>最大回撤</h3>
        <dl className="kv">
          <dt>幅度</dt><dd>{dd.max_drawdown !== undefined ? fmtPct(dd.max_drawdown, 2) : '—'}</dd>
          <dt>峰值</dt><dd>{dd.peak_ts ? fmtTime(dd.peak_ts) : '—'}</dd>
          <dt>谷值</dt><dd>{dd.trough_ts ? fmtTime(dd.trough_ts) : '—'}（下跌 {dd.drawdown_bars ?? 0} 根）</dd>
          <dt>恢复</dt><dd>{dd.recovered ? `${fmtTime(dd.recovery_ts ?? 0)}（用时 ${dd.recovery_bars} 根）` : dd.recovered === false ? '未恢复（期末仍低于前高）' : '—'}</dd>
          <dt>最差一天</dt><dd>{wd ? `${wd.date} ${fmtPct(wd.return, 2, true)}` : '—'}</dd>
          <dt>平均持仓</dt><dd>{m.avg_holding_hours ? `${Number(m.avg_holding_hours).toFixed(1)} 小时（${m.completed_trades} 笔完成交易）` : '—'}</dd>
          <dt>手续费合计</dt><dd>{fmtMoney(Number(m.total_fees ?? 0))}</dd>
        </dl>
        {(bt.open_positions?.length ?? 0) > 0 && (
          <Callout tone="warn" title="期末未平仓">
            {bt.open_positions!.map((p) => `${p.security} ${p.amount} @ 均价 ${Number(p.avg_cost).toFixed(4)}，浮盈 ${fmtMoney(p.unrealized_pnl)}`).join('；')}。
            按最后价格估值，不计入完成交易、胜率与持仓时长。
          </Callout>
        )}
      </div>
      <div>
        <h3 className="section-title">口径声明</h3>
        <dl className="kv">
          {Object.entries(A).filter(([k]) => k !== 'cost').map(([k, v]) => <Fragment key={k}><dt>{ASSUME[k] ?? k}</dt><dd>{String(v)}</dd></Fragment>)}
        </dl>
        <h3 className="section-title" style={{ marginTop: 18 }}>撮合与风控计数</h3>
        <dl className="kv">
          <dt>限价触价歧义（未成交）</dt><dd>{counters.limit_touch_ambiguous ?? 0}</dd>
          <dt>同根止损止盈（按止损）</dt><dd>{counters.bracket_same_bar ?? 0}</dd>
          <dt>过期信号（未补成交）</dt><dd>{counters.expired_signals ?? 0}</dd>
          <dt>资金 / 持仓不足拒绝</dt><dd>{counters.insufficient_funds ?? 0}</dd>
          <dt>风控拦截</dt><dd>{counters.risk_rejections ?? 0}</dd>
          <dt>重复事件丢弃</dt><dd>{counters.duplicate_events ?? 0}</dd>
        </dl>
        {(bt.flows?.length ?? 0) > 0 && (
          <>
            <h3 className="section-title" style={{ marginTop: 18 }}>资金流（按单位净值申购 / 赎回）</h3>
            <table><tbody>{bt.flows!.map((f) => <tr key={f.ts}><td>{fmtTime(f.ts)}</td><td className="right mono">{f.amount}</td><td className="right mono muted">净值 {f.unit_nav}</td></tr>)}</tbody></table>
          </>
        )}
      </div>
    </div>
  )
}

const ASSUME: Record<string, string> = {
  sampling: '收益采样', periods_per_year: '年化周期数', annualization: '年化方式', risk_free_rate: '无风险利率',
  return_basis: '收益基准', drawdown_basis: '回撤口径', fill_model: '成交时点', insufficient_funds: '资金不足',
  limit_model: '限价模型', bracket_priority: '止损止盈', open_positions: '未平仓', warmup_bars: '预热 bar',
}

function SignalsTable({ bt }: { bt: BacktestResult }) {
  const rows = bt.signals ?? []
  if (!rows.length) return <EmptyState title="没有信号记录" />
  return (
    <div className="table-wrap" style={{ maxHeight: 560, overflow: 'auto' }}>
      <table>
        <thead><tr><th>决策时间 UTC</th><th>标的</th><th>目标</th><th>原因</th><th>状态</th><th>说明</th><th>signal_id</th></tr></thead>
        <tbody>
          {rows.slice().reverse().map((s) => (
            <tr key={s.signal_id}>
              <td>{fmtTime(s.decision_ts)}</td><td>{s.instrument_id}</td>
              <td className="mono">{s.target.kind} {s.target.value}{s.order_type === 'limit' ? ' · 限价' : ''}</td>
              <td>{s.reason}</td>
              <td><span className={`table-pill ${s.status === 'rejected' || s.status === 'expired' ? 'fail' : s.status === 'tolerance' ? 'pending' : ''}`}>{SIG_STATUS[s.status] ?? s.status}</span></td>
              <td className="wrap" style={{ fontSize: 12 }}>{s.reason_code && <code className="inline">{s.reason_code}</code>} {s.status_reason}</td>
              <td className="mono muted">{s.signal_id.slice(0, 12)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
