import { useCallback, useEffect, useState } from 'react'
import { fmtMoney, fmtNum, fmtPct, fmtTime, tone, v3, type PaperSessionView, type StrategyFile } from '../api2'
import { COLORS, LineChart } from '../components/charts2'
import { Callout, EmptyState, Fold, Heading, Loading, Panel, SourceBanner, Tag } from '../components/ui'
import { useEventRefresh, useNewestActiveRunId, useRunEvents, type RunEvent, type RunEventStatus } from '../hooks/useRunEvents'
import { rd, type Shadow, type ShadowCompare } from '../apiRd'
import TradingTerminal from '../components/TradingTerminal'
import { demo, type DemoConnection, type TerminalSession } from '../apiDemo'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

const STATUS: Record<string, [string, string]> = {
  running: ['运行中', ''],
  paused: ['已暂停', 'pending'],
  stopped: ['已停止', 'pending'],
  interrupted: ['已中断', 'fail'],
  completed: ['已完成 30 天', ''],
  queued: ['排队中（等待 worker）', 'pending'],
  recovering: ['恢复中（从检查点继续）', 'pending'],
  failed: ['失败', 'fail'],
}

const STREAM_LABEL: Record<RunEventStatus, string> = {
  idle: '未订阅',
  connecting: '连接中',
  live: '已连接',
  reconnecting: '重连中',
  offline: '网络已断开',
  hidden: '后台暂停',
  done: '已结束',
  error: '连接失败',
  closed: '已关闭',
}

/** 事件流面板最多渲染多少行：内存里保留的事件更多，但 DOM 不该无限膨胀。 */
const STREAM_ROWS = 300

/** 把一条 run 事件压成一行中文摘要；未知类型退化成一段截断的 JSON。 */
function describeEvent(e: RunEvent): string {
  const d = e.data || {}
  switch (e.type) {
    case 'state':
      return String(d.state ?? '')
    case 'command':
      return String(d.command ?? '')
    case 'fill':
      return `${d.side === 'buy' ? '买入' : '卖出'} ${d.amount ?? ''} @ ${d.price ?? ''}`
    case 'bar':
      return `bar ${fmtTime(Number(d.last_bar_ts) || 0)} · 本次成交 ${d.new_fills ?? 0}`
    case 'error':
    case 'data_error':
      return String(d.message ?? '')
    case 'phase':
      return String(d.phase ?? '')
    case 'recovered':
      return `已从检查点恢复（${d.rows_done ?? 0} 行）`
    default:
      return JSON.stringify(d).slice(0, 80)
  }
}

function equityOf(s: PaperSessionView) {
  const pts = s.equity_points
  return pts.length ? pts[pts.length - 1].v : Number((s.portfolio as { total_value?: string })?.total_value ?? s.initial_cash)
}

export default function PaperPage({ id, onNavigate, notify }: PageProps & { id?: string }) {
  if (id) return <PaperDetail id={id} onNavigate={onNavigate} notify={notify} />
  return <PaperList onNavigate={onNavigate} notify={notify} />
}

function PaperList({ onNavigate, notify }: Pick<PageProps, 'onNavigate' | 'notify'>) {
  const [sessions, setSessions] = useState<PaperSessionView[] | null>(null)
  const [conventions, setConventions] = useState<string[]>([])
  const [tickSeconds, setTickSeconds] = useState(30)
  const [strategies, setStrategies] = useState<StrategyFile[]>([])
  const [slug, setSlug] = useState('')
  const [source, setSource] = useState<'exchange' | 'demo'>('exchange')
  const [exchange, setExchange] = useState('binance')
  const [cash, setCash] = useState(10_000)
  const [fee, setFee] = useState(0.1)
  const [slip, setSlip] = useState(5)
  const [starting, setStarting] = useState(false)
  const [execution, setExecution] = useState<'paper' | 'demo'>('paper')
  const [conns, setConns] = useState<DemoConnection[]>([])
  const [connId, setConnId] = useState('')
  const [maxOrder, setMaxOrder] = useState(1000)
  const [perMin, setPerMin] = useState(10)
  const [dayLoss, setDayLoss] = useState(5)

  const load = useCallback(() => {
    v3.paperSessions().then((r) => { setSessions(r.sessions); setConventions(r.conventions); setTickSeconds(r.tick_seconds) }).catch((e) => notify(e.message, true))
  }, [notify])

  // 列表本身没有 runId，但"哪个实验在动"正是被事件改变的。
  // 订阅最新一个未结束的 run：有事件就刷新列表，取代原来的 15 秒轮询。
  const { runId: activeRun } = useNewestActiveRunId()
  const refresh = useEventRefresh(load, 400)
  const stream = useRunEvents({
    runId: activeRun,
    onEvents: () => refresh(),
    onDone: () => load(),
  })

  useEffect(() => {
    demo.connections().then((r) => setConns(r.connections.filter((c) => c.status === 'active'))).catch(() => undefined)
  }, [])
  const usable = conns.filter((c) => c.exchange === exchange).sort((a, b) => Number(b.realistic) - Number(a.realistic))
  useEffect(() => { if (!usable.some((c) => c.id === connId)) setConnId(usable[0]?.id ?? '') }, [exchange, conns.length]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load()
    v3.strategies().then((r) => {
      setStrategies(r.strategies)
      const first = r.strategies.find((s) => s.human_approved)
      if (first) setSlug(first.slug)
    }).catch(() => undefined)
  }, [load])

  // 兜底轮询：没有活跃 run（没有事件可推）或事件流没连上时才跑，而且频率比原来的 15 秒低得多。
  // 事件流正常时它完全不工作——这正是"用推送替代轮询"的意思。
  useEffect(() => {
    if (activeRun && stream.connected) return
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [load, activeRun, stream.connected])

  const approved = strategies.filter((s) => s.human_approved)
  const start = async () => {
    if (!slug) return
    setStarting(true)
    try {
      const demoOpts = execution === 'demo'
        ? { execution, demo_connection_id: connId, max_order_notional: maxOrder, max_orders_per_minute: perMin, max_daily_loss: dayLoss / 100 }
        : {}
      const r = await v3.paperStart({ slug, initial_cash: cash, source: execution === 'demo' ? 'exchange' : source, exchange, taker_fee: fee / 100, slippage_bps: slip, ...demoOpts })
      notify('实验已启动')
      onNavigate(`paper/${r.session.id}`)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setStarting(false)
    }
  }

  return (
    <>
      <Heading
        eyebrow="PAPER TRADING"
        title="纸面模拟"
        description="用真实行情驱动虚拟账户：策略在每根新收盘 bar 上运行一次，委托按下一根 bar 开盘价加成本成交——与回测同一套撮合代码。7 天 / 30 天检查点必须真实经过时间。"
      />
      <SourceBanner
        tone="paper"
        icon="◉"
        title={`后台每 ${tickSeconds} 秒自动采集一次`}
        detail="服务需要保持运行；重启后进行中的实验会被标记为中断，不会补算停机期间的成交"
      />
      <div className="two-columns">
        <Panel title={`实验（${sessions?.length ?? 0}）`} flush>
          {!sessions ? <Loading /> : !sessions.length ? (
            <EmptyState glyph="◉" title="还没有模拟实验">在右侧选择一个已批准的策略启动。策略需要先在编辑器里「批准版本」。</EmptyState>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>策略</th><th>行情</th><th>状态</th><th className="right">净值</th><th className="right">收益</th><th className="right">成交</th><th>已运行</th><th>7 天 / 30 天</th></tr></thead>
                <tbody>
                  {sessions.map((s) => {
                    const eq = equityOf(s)
                    const ret = eq / s.initial_cash - 1
                    const [label, cls] = STATUS[s.status] ?? [s.status, 'pending']
                    return (
                      <tr key={s.id} className="clickable" onClick={() => onNavigate(`paper/${s.id}`)}>
                        <td className="wrap"><strong>{s.strategy_name}</strong><small>{s.strategy_version} · {(s.securities?.length ? s.securities : [s.symbol]).join(', ')} · {s.timeframe}</small></td>
                        <td><Tag tone={s.source === 'demo' ? 'warn' : 'ok'}>{s.source === 'demo' ? '演示行情' : `真实 · ${s.exchange}`}</Tag>{(s as TerminalSession).execution === 'demo' && <small>交易所模拟盘</small>}{(s as TerminalSession).execution === 'live' && <small className="negative">微实盘 · 真实资金</small>}</td>
                        <td><span className={`table-pill ${cls}`}>{label}</span>{s.live && <small className="positive">● 运行时在线</small>}</td>
                        <td className="right">{fmtMoney(eq)}</td>
                        <td className={`right ${tone(ret)}`}>{fmtPct(ret, 2, true)}</td>
                        <td className="right">{s.fills.length}</td>
                        <td>{fmtNum(s.elapsed_hours, 1)} 小时<small>自 {fmtTime(s.started_at)}</small></td>
                        <td style={{ minWidth: 120 }}>
                          {s.checkpoints.map((c) => (
                            <div key={c.days} className="mini-progress" title={`${c.days} 天：${c.progress}%`}>
                              <span>{c.days}d</span><div><i style={{ width: `${c.progress}%` }} /></div>
                            </div>
                          ))}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
        <div className="side-column">
          <Panel title="启动新实验" subtitle="只有人工批准过的策略版本可以进入纸面模拟">
            {!approved.length ? (
              <Callout tone="warn">还没有已批准的策略。打开一个策略，回测满意后点击编辑器右上角「批准版本」。<br /><button className="button small" style={{ marginTop: 8 }} onClick={() => onNavigate('strategies')}>去我的策略</button></Callout>
            ) : (
              <>
                <label>策略
                  <select value={slug} onChange={(e) => setSlug(e.target.value)}>
                    {approved.map((s) => <option key={s.slug} value={s.slug}>{s.name}（{s.version} · {s.securities.join(',')} · {s.timeframe}）</option>)}
                  </select>
                </label>
                <label style={{ marginTop: 10, display: 'block' }}>成交方式
                  <select value={execution} onChange={(e) => setExecution(e.target.value as 'paper' | 'demo')}>
                    <option value="paper">平台撮合（纸面模拟）</option>
                    <option value="demo">交易所模拟盘（真实撮合 + 模拟资金）</option>
                  </select>
                </label>
                <div className="form-grid" style={{ marginTop: 10 }}>
                  <label>行情
                    <select value={execution === 'demo' ? 'exchange' : source} disabled={execution === 'demo'} onChange={(e) => setSource(e.target.value as 'exchange' | 'demo')}>
                      <option value="exchange">真实行情</option>
                      <option value="demo">演示行情（离线体验）</option>
                    </select>
                  </label>
                  <label>交易所
                    <select value={exchange} disabled={source === 'demo' && execution !== 'demo'} onChange={(e) => setExchange(e.target.value)}>
                      <option value="binance">Binance</option>
                      <option value="okx">OKX</option>
                      <option value="bybit">Bybit</option>
                    </select>
                  </label>
                  <label>{execution === 'demo' ? '本次动用资金' : '虚拟资金'}<input type="number" min={100} value={cash} onChange={(e) => setCash(Number(e.target.value))} /></label>
                  {execution === 'paper' && <>
                    <label>手续费 %<input type="number" step={0.01} value={fee} onChange={(e) => setFee(Number(e.target.value))} /></label>
                    <label>滑点 bps<input type="number" value={slip} onChange={(e) => setSlip(Number(e.target.value))} /></label>
                  </>}
                </div>
                {execution === 'demo' && (
                  <div className="demo-start">
                    {usable.length ? (
                      <label>模拟盘连接
                        <select value={connId} onChange={(e) => setConnId(e.target.value)}>
                          {usable.map((c) => <option key={c.id} value={c.id}>{c.label} · {c.key_mask}{c.realistic ? '' : '（仅联调）'}</option>)}
                        </select>
                      </label>
                    ) : (
                      <Callout tone="warn">还没有可用的 {({ okx: 'OKX', bybit: 'Bybit' } as Record<string, string>)[exchange] ?? 'Binance'} 模拟盘连接。<button className="button small" style={{ marginLeft: 8 }} onClick={() => onNavigate('demo')}>去接入</button></Callout>
                    )}
                    <div className="form-grid" style={{ marginTop: 10 }}>
                      <label>单笔上限<input type="number" min={10} value={maxOrder} onChange={(e) => setMaxOrder(Number(e.target.value))} /></label>
                      <label>每分钟笔数<input type="number" min={1} max={120} value={perMin} onChange={(e) => setPerMin(Number(e.target.value))} /></label>
                      <label>当日亏损熔断 %<input type="number" min={0} max={50} step={0.5} value={dayLoss} onChange={(e) => setDayLoss(Number(e.target.value))} /></label>
                    </div>
                    <p className="hint">成交价和手续费以交易所回报为准。触发熔断或对账不一致时只减仓、不开新仓，要你手动解除。</p>
                  </div>
                )}
                {source === 'demo' && execution === 'paper' && <Callout tone="warn">演示行情由时间函数生成，只用于体验流程，<strong>不能</strong>用于判断策略好坏。</Callout>}
                <button className="button primary" style={{ marginTop: 14 }} disabled={starting || !slug || (execution === 'demo' && !connId)} onClick={start}>{starting ? '启动中…' : '启动实验'}</button>
              </>
            )}
          </Panel>
          <Fold title="纸面模拟的规则" count={conventions.length}>
            <ul className="plain">{conventions.map((c) => <li key={c}>{c}</li>)}</ul>
          </Fold>
        </div>
      </div>
      <div style={{ marginTop: 16 }} />
      <ShadowRuns sessions={sessions ?? []} approved={approved} notify={notify} onNavigate={onNavigate} onChanged={load} />
    </>
  )
}

/** 影子运行：候选版本和线上实验在同样的实时行情里并行模拟，稳定跑赢才替换。 */
function ShadowRuns({ sessions, approved, notify, onNavigate, onChanged }: {
  sessions: PaperSessionView[]; approved: StrategyFile[]; onChanged: () => void
} & Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [rows, setRows] = useState<Shadow[] | null>(null)
  const [base, setBase] = useState('')
  const [cand, setCand] = useState('')
  const [days, setDays] = useState(7)
  const [open, setOpen] = useState<string | null>(null)
  const [detail, setDetail] = useState<ShadowCompare | null>(null)
  const load = useCallback(() => rd.shadows().then((r) => setRows(r.shadows)).catch(() => setRows([])), [])
  useEffect(() => { load() }, [load])
  const running = sessions.filter((x) => ['running', 'queued', 'paused'].includes(x.status))
  useEffect(() => { if (!base && running[0]) setBase(running[0].id) }, [running.length]) // eslint-disable-line react-hooks/exhaustive-deps
  const candidates = approved   // 同一个策略批准了新版本，也可以拿新版本和线上旧版本比
  useEffect(() => { if (!candidates.some((c) => c.slug === cand)) setCand(candidates[0]?.slug ?? '') }, [base, candidates.length]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    setDetail(null)
    if (open) rd.shadow(open).then((r) => setDetail(r.compare)).catch(() => undefined)
  }, [open])

  const start = async () => {
    try { await rd.startShadow({ baseline_session_id: base, candidate_slug: cand, min_days: days }); notify('影子运行已开始'); load(); onChanged() } catch (e) { notify((e as Error).message, true) }
  }
  const act = async (fn: () => Promise<unknown>, msg: string) => {
    try { await fn(); notify(msg); load(); onChanged() } catch (e) { notify((e as Error).message, true) }
  }
  const promote = async (sh: Shadow) => {
    const ready = sh.compare?.ready
    if (!await appConfirm(ready ? `用「${sh.candidate_name}」替换「${sh.baseline_name}」？基准实验会停止，候选继续运行。仍是模拟，不会真实下单。`
      : `还没满足替换条件（${sh.compare?.verdict ?? '没有数据'}）。确定强制替换吗？`)) return
    act(() => rd.promote(sh.id, !ready), '已替换：基准实验已停止')
  }

  return (
    <Panel title="影子运行" subtitle="想换掉一个正在跑的实验？先让候选版本用完全相同的设置并行模拟。观察满设定天数，并且每小时收益差的 t 值 ≥ 1.65、回撤不超过基准的 1.2 倍，才建议替换。">
      <div className="rd-shadow-form">
        <label>线上实验（基准）
          <select value={base} onChange={(e) => setBase(e.target.value)}>
            {running.length === 0 && <option value="">（没有运行中的实验）</option>}
            {running.map((x) => <option key={x.id} value={x.id}>{x.strategy_name} · {x.strategy_version} · {x.timeframe}</option>)}
          </select>
        </label>
        <label>候选策略
          <select value={cand} onChange={(e) => setCand(e.target.value)}>
            {candidates.length === 0 && <option value="">（没有已批准的策略）</option>}
            {candidates.map((x) => <option key={x.slug} value={x.slug}>{x.name}（{x.version}）</option>)}
          </select>
        </label>
        <label>至少观察
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>{[3, 7, 14, 30].map((d) => <option key={d} value={d}>{d} 天</option>)}</select>
        </label>
        <button className="button primary" disabled={!base || !cand} onClick={start}>开始影子运行</button>
      </div>
      {rows === null ? <Loading /> : rows.length === 0 ? <div className="hint" style={{ marginTop: 12 }}>还没有影子运行。</div> : (
        <ul className="plain-list rd-shifts" style={{ marginTop: 12 }}>
          {rows.map((sh) => {
            const c = sh.compare
            return (
              <li key={sh.id}>
                <button className="rd-shift-head" onClick={() => setOpen(open === sh.id ? null : sh.id)} aria-expanded={open === sh.id}>
                  <span><b>{sh.candidate_name}</b> <span className="muted">vs</span> {sh.baseline_name}</span>
                  <span>{sh.status === 'running' ? <Tag tone={c?.tone === 'bad' ? 'fail' : c?.tone || undefined}>{c?.verdict ?? '等待数据'}</Tag> : <Tag>{sh.status === 'promoted' ? '已替换' : '已结束'}</Tag>}</span>
                </button>
                {c && (
                  <small>
                    观察 {fmtNum(c.hours / 24, 1)}/{sh.min_days} 天 · 收益 {fmtPct(c.ret_cand, 2, true)} vs {fmtPct(c.ret_base, 2, true)} · 收益差 t {fmtNum(c.t, 2)} · 回撤 {fmtPct(c.dd_cand)} vs {fmtPct(c.dd_base)}
                  </small>
                )}
                {open === sh.id && (
                  <div className="rd-shadow-detail">
                    {detail?.curve && detail.curve.length > 1 ? (
                      <LineChart timestamps={detail.curve.map((p) => p.t)} format="num" zeroLine={false} height={220}
                        series={[{ name: `候选 · ${sh.candidate_name}`, values: detail.curve.map((p) => p.cand), color: COLORS.strategy },
                          { name: `基准 · ${sh.baseline_name}`, values: detail.curve.map((p) => p.base), color: COLORS.benchmark, dashed: true }]} ariaLabel="影子运行净值对比" />
                    ) : <div className="hint">两边都至少跑满两个小时后才有对比曲线。</div>}
                    <div className="button-row">
                      <button className="button small" onClick={() => onNavigate(`paper/${sh.candidate_id}`)}>候选实验</button>
                      <button className="button small" onClick={() => onNavigate(`paper/${sh.baseline_id}`)}>基准实验</button>
                      {sh.status === 'running' && <>
                        <button className="button small primary" onClick={() => promote(sh)}>替换</button>
                        <button className="button small danger" onClick={() => act(() => rd.endShadow(sh.id), '已结束：候选实验已停止')}>结束</button>
                      </>}
                    </div>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </Panel>
  )
}

function PaperDetail({ id, onNavigate, notify }: { id: string } & Pick<PageProps, 'onNavigate' | 'notify'>) {
  const [s, setS] = useState<PaperSessionView | null>(null)
  const [windows, setWindows] = useState<Record<string, { label: string; complete: boolean; hours: number; metrics: Record<string, number | null> }>>({})
  const [live, setLive] = useState(false)
  const [error, setError] = useState('')
  const [ticking, setTicking] = useState(false)

  const load = useCallback(() => {
    v3.paperReport(id).then((r) => { setS(r.session); setWindows(r.windows.windows as never); setLive(r.live); setError('') }).catch((e) => setError(e.message))
  }, [id])

  // 事件驱动：run 上有新事件（bar / fill / state / command …）才重新拉一次报告。
  // 断线补齐会一次回来几十条事件，用 useEventRefresh 合并成一次请求。
  const refresh = useEventRefresh(load, 400)
  const stream = useRunEvents({
    runId: id,
    onEvents: () => refresh(),
    onDone: () => load(),
  })

  useEffect(() => { load() }, [load])

  // 有旧数据时不要因为一次失败就把整页清空——断网期间用户看到的应该还是最后一次的状态。
  if (error && !s) return <Callout tone="fail">{error}</Callout>
  if (!s) return <Loading />
  const isDemo = (s as TerminalSession).execution === 'demo' || (s as TerminalSession).execution === 'live'
  const isLive = (s as TerminalSession).execution === 'live'
  const [label, cls] = STATUS[s.status] ?? [s.status, 'pending']
  const streamRows = stream.events.slice(Math.max(0, stream.events.length - STREAM_ROWS))

  const tick = async () => {
    setTicking(true)
    try {
      const r = await v3.paperTick(id)
      const t = r.tick as { status?: string; new_fills?: number }
      notify(`已请求 worker 立即采集（当前状态：${STATUS[t.status ?? '']?.[0] ?? t.status}，累计成交 ${t.new_fills ?? 0} 笔）`)
      load()
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setTicking(false)
    }
  }
  const stop = async () => {
    if (!await appConfirm('停止实验？持仓不会自动平仓，按最后价格估值。')) return
    await v3.paperStop(id)
    load()
  }
  const remove = async () => {
    if (!await appConfirm('删除实验记录？')) return
    await v3.paperDelete(id)
    onNavigate('paper')
  }

  return (
    <>
      <div className="editor-toolbar">
        <button className="button small" onClick={() => onNavigate('paper')}>← 纸面模拟</button>
        <h1 className="toolbar-title">{s.strategy_name}</h1>
        <Tag mono>{s.strategy_version}</Tag>
        <span className={`table-pill ${cls}`}>{label}</span>
        {live && <Tag tone="ok">运行时在线</Tag>}
        <Tag
          tone={stream.connected ? 'ok' : stream.status === 'offline' || stream.status === 'error' ? 'fail' : 'warn'}
        >
          <span data-testid="paper-event-connection" data-status={stream.status} data-connects={stream.connects}>
            事件流 {STREAM_LABEL[stream.status]}
          </span>
        </Tag>
        <div className="spacer" />
        <button className="button small" disabled={!['running', 'recovering', 'queued'].includes(s.status) || ticking} onClick={tick}>{ticking ? '采集中…' : '立即采集'}</button>
        <button className="button small" onClick={() => onNavigate(`editor/${s.strategy_id}`)}>查看策略</button>
        {s.status === 'paused'
          ? <button className="button small" onClick={async () => { try { await v3.paperResume(id); load() } catch (e) { notify((e as Error).message, true) } }}>恢复</button>
          : <button className="button small" disabled={s.status !== 'running'} onClick={async () => { try { await v3.paperPause(id); load() } catch (e) { notify((e as Error).message, true) } }}>暂停</button>}
        <button className="button small" disabled={!['running', 'paused'].includes(s.status)} onClick={stop}>停止（不平仓）</button>
        <button className="button small primary" disabled={!['running', 'paused'].includes(s.status)} onClick={async () => {
          if (!await appConfirm(isDemo
            ? (isLive ? '一键停止？平台会先熔断（不再开仓），再把全部持仓的市价卖单送到交易所实盘（真实成交），然后结束实验。' : '一键停止？平台会先熔断（不再开仓），再把全部持仓的市价卖单送到交易所模拟盘，然后结束实验。')
            : '模拟平仓并结束？将撤销全部委托，并按最新价格 ± 成本卖出全部持仓（模拟成交，不是真实下单）。')) return
          try { await v3.paperLiquidate(id); notify(isLive ? '已一键停止：平仓单已送到交易所' : isDemo ? '已一键停止：平仓单已送到模拟盘' : '已模拟平仓并结束'); load() } catch (e) { notify((e as Error).message, true) }
        }}>{isDemo ? '一键停止并平仓' : '模拟平仓并结束'}</button>
        <button className="button small danger" onClick={remove}>删除</button>
      </div>
      {error && <Callout tone="warn">刷新失败（{error}）；下面显示的是最后一次成功获取的数据。事件流会在网络恢复后自动补齐。</Callout>}
      <SourceBanner
        tone={s.source === 'demo' ? 'warn' : 'real'}
        icon="◉"
        title={s.source === 'demo' ? '演示行情（时间函数生成，只用于体验流程）' : `真实行情 · ${s.exchange}${isLive ? ' · 微实盘：真实资金，成交走交易所实盘' : isDemo ? ' · 成交走交易所模拟盘' : ' · 平台撮合'}`}
        detail={`${(s.securities?.length ? s.securities : [s.symbol]).join(', ')} · ${s.timeframe} · 自 ${fmtTime(s.started_at)} UTC · 最近一根 bar ${fmtTime(s.last_bar_ts)}`}
        right={`更新于 ${fmtTime(s.last_update)}`}
      />
      {s.message && <Callout>{s.message}</Callout>}
      <TradingTerminal id={id} session={s as TerminalSession} version={s.last_update} notify={notify} onChanged={load} />
      <div className="two-columns">
        <Panel title="净值" subtitle="每分钟最多记录一个观测点；中断期间没有观测点，曲线不会补齐">
          {s.equity_points.length > 1 ? (
            <LineChart timestamps={s.equity_points.map((p) => p.t)} series={[{ name: '账户净值', values: s.equity_points.map((p) => p.v), color: COLORS.strategy, area: true }]} format="money" zeroLine={false} height={240} />
          ) : <EmptyState glyph="◷" title="还没有足够的观测点">新 K 线收盘后策略才会运行；也可以点「立即采集」。</EmptyState>}
        </Panel>
        <Panel title="观察窗口" subtitle="与回测同口径；未满时标注为进行中">
          <div className="mini-progress-row">
            <span>已运行 {fmtNum(s.elapsed_hours, 1)} 小时</span>
            {s.checkpoints.map((c) => (
              <div key={c.days} className="mini-progress" title={`${c.days} 天：${c.progress}%`}>
                <span>{c.days}d</span><div><i style={{ width: `${c.progress}%` }} /></div>
              </div>
            ))}
          </div>
          <table>
            <thead><tr><th>窗口</th><th>状态</th><th className="right">收益</th><th className="right">回撤</th></tr></thead>
            <tbody>
              {Object.entries(windows).map(([k, w]) => (
                <tr key={k}><td>{w.label}</td><td>{w.complete ? '完整' : `进行中（${fmtNum(w.hours, 1)} 小时）`}</td><td className={`right ${tone(w.metrics?.total_return)}`}>{fmtPct(w.metrics?.total_return, 2, true)}</td><td className="right">{fmtPct(w.metrics?.max_drawdown)}</td></tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
      <Panel
        title="实时事件流（WebSocket）"
        subtitle="按事件 id 升序；断线期间用 events?after=游标 补齐后再重连，所以这里的序号必须连续且不重复"
        actions={<button className="button small ghost" onClick={stream.retry}>立即重连</button>}
      >
        <div
          className="logs"
          style={{ maxHeight: 360 }}
          data-testid="paper-event-stream"
          data-cursor={stream.cursor}
          data-status={stream.status}
          data-connects={stream.connects}
          data-count={stream.events.length}
        >
          {stream.lastError && <div className="log-line warning"><span>—</span><b>提示</b>{stream.lastError}</div>}
          {!streamRows.length ? (
            <EmptyState glyph="◌" title="还没有事件">实验推进时会在这里按 id 顺序出现事件。</EmptyState>
          ) : (
            streamRows.map((e) => (
              <div
                key={e.id}
                className={`log-line ${e.type === 'error' || e.type === 'data_error' ? 'error' : 'info'}`}
                data-testid="paper-event"
                data-event-id={e.id}
                data-event-type={e.type}
              >
                <span>{fmtTime(e.ts)}</span>
                <b>#{e.id}</b>
                {e.type} · {describeEvent(e)}
              </div>
            ))
          )}
        </div>
      </Panel>
    </>
  )
}
