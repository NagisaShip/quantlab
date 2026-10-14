import { useCallback, useEffect, useMemo, useState } from 'react'
import { fmtMoney, fmtNum, fmtPct, fmtQty, fmtTime, tone } from '../api2'
import { demo, ORDER_STATUS, type LiveQuote, type Terminal, type TerminalSession } from '../apiDemo'
import { num } from '../http'
import KlineChart from './KlineChart'
import { Callout, EmptyState, Tag } from './ui'

const TF_MS: Record<string, number> = {
  '1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000,
  '1h': 3_600_000, '2h': 7_200_000, '4h': 14_400_000, '6h': 21_600_000, '12h': 43_200_000, '1d': 86_400_000,
}

type Tab = 'positions' | 'orders' | 'fills' | 'logs'

interface Position { security: string; amount: number; avg_cost: number; last_price: number; value: number; unrealized_pnl: number; pnl_ratio: number }

/**
 * 交易终端：K 线（含买卖点和正在形成的那根）+ 持仓 / 委托 / 成交 / 日志 + 账户与风控。
 * 纸面模拟和交易所模拟盘用同一个终端；模拟盘多出委托回报、滑点、熔断和对账。
 */
export default function TradingTerminal({ id, session, version, notify, onChanged }: {
  id: string
  session: TerminalSession
  /** 会话有更新时变化：终端跟着重新取数 */
  version: number
  notify: (m: string, error?: boolean) => void
  onChanged: () => void
}) {
  const secs = session.securities?.length ? session.securities : [session.symbol]
  const [security, setSecurity] = useState(secs[0])
  const [t, setT] = useState<Terminal | null>(null)
  const [quote, setQuote] = useState<LiveQuote | null>(null)
  const [tab, setTab] = useState<Tab>(session.execution === 'demo' || session.execution === 'live' ? 'orders' : 'positions')
  const live = ['running', 'paused', 'queued', 'recovering'].includes(session.status)
  const isDemo = session.execution === 'demo' || session.execution === 'live'

  const load = useCallback(() => {
    demo.terminal(id, security).then(setT).catch((e) => notify((e as Error).message, true))
  }, [id, security, notify])
  useEffect(() => { load() }, [load, version])

  // 正在形成的那根：开着页面时每 3 秒问一次 API 进程里的行情网关
  useEffect(() => {
    if (!t?.live_endpoint || !live) { setQuote(null); return }
    let stop = false
    const poll = () => demo.live(t.live_endpoint).then((q) => { if (!stop) setQuote(q) }).catch(() => undefined)
    poll()
    const h = window.setInterval(() => { if (!document.hidden) poll() }, 3000)
    return () => { stop = true; window.clearInterval(h) }
  }, [t?.live_endpoint, live])

  const step = TF_MS[session.timeframe] ?? 3_600_000
  const marks = useMemo(() => (t?.fills ?? []).map((f) => ({ ts: f.ts, side: f.side })), [t?.fills])
  const pf = session.portfolio as { positions?: Record<string, Position>; available_cash?: string | number; positions_value?: string | number; total_value?: string | number; total_fees?: string | number }
  const positions = Object.values(pf?.positions ?? {})
  const dv = session.demo ?? {}
  const orders = [...(t?.orders ?? dv.orders ?? [])].reverse()
  const feed = t?.feed.status ?? session.feed_status ?? {}
  const lastPrice = quote?.forming?.c ?? (t?.bars.length ? t.bars[t.bars.length - 1].c : null)

  const unhalt = async () => {
    if (!window.confirm('解除熔断，允许策略重新开仓？请先确认触发原因已经处理。')) return
    try { await demo.unhalt(id); notify('已解除熔断'); onChanged() } catch (e) { notify((e as Error).message, true) }
  }

  return (
    <div className="terminal">
      {isDemo && dv.halted && (
        <div className="callout fail terminal-halt">
          <div><strong>已熔断：只减仓、不开新仓</strong><br />{dv.halt_reason}</div>
          {live && <button className="button small" onClick={unhalt}>我已确认，解除熔断</button>}
        </div>
      )}
      <div className="terminal-grid">
        <section className="panel terminal-chart">
          <div className="terminal-chart-head">
            {secs.length > 1 ? (
              <select value={security} onChange={(e) => setSecurity(e.target.value)} aria-label="标的">
                {secs.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            ) : <b>{security}</b>}
            <span className="muted">{session.timeframe}</span>
            {lastPrice != null && <b className="terminal-price">{fmtMoney(lastPrice, lastPrice < 10 ? 5 : 2)}</b>}
            <div className="spacer" />
            <FeedBadge mode={feed.mode} healthy={quote?.healthy ?? feed.healthy} lag={quote?.lag_ms ?? feed.lag_ms} source={session.source} />
          </div>
          {!t ? <div className="kline kline-empty" style={{ height: 380 }}>加载中…</div>
            : t.bars.length ? <KlineChart bars={t.bars} marks={marks} forming={quote?.forming ?? null} stepMs={step} ariaLabel={`${security} K 线`} />
              : <div className="kline kline-empty" style={{ height: 380 }}>{t.bars_error ? `取不到行情：${t.bars_error}` : '还没有 K 线'}</div>}
          <div className="terminal-legend">
            <span><i className="dot up" />买入成交</span><span><i className="dot down" />卖出成交</span>
            <span className="muted">滚轮缩放 · 拖动平移 · 双击回到最新</span>
          </div>
        </section>

        <aside className="terminal-side">
          <section className="panel">
            <div className="panel-body terminal-account">
              <div><small>账户净值</small><b>{fmtMoney(num(pf?.total_value) || session.initial_cash)}</b></div>
              <div><small>收益</small><b className={tone((num(pf?.total_value) || session.initial_cash) / session.initial_cash - 1)}>{fmtPct((num(pf?.total_value) || session.initial_cash) / session.initial_cash - 1, 2, true)}</b></div>
              <div><small>现金</small><b>{fmtMoney(num(pf?.available_cash ?? session.initial_cash))}</b></div>
              <div><small>持仓市值</small><b>{fmtMoney(num(pf?.positions_value))}</b></div>
              <div><small>手续费</small><b>{fmtMoney(num(pf?.total_fees), 4)}</b></div>
              <div><small>成交</small><b>{session.fills.length} 笔</b></div>
            </div>
          </section>
          <section className="panel">
            <div className="panel-heading"><div><h2>成交方式</h2></div></div>
            <div className="panel-body terminal-exec">
              {isDemo ? (
                <>
                  <p><Tag tone={dv.connection?.realistic ? 'ok' : 'warn'}>{dv.connection?.env_label ?? '交易所模拟盘'}</Tag></p>
                  <small className="mono muted">Key {dv.connection?.key_mask}</small>
                  {dv.connection && !dv.connection.realistic && <small className="muted">{dv.connection.env_note}</small>}
                  <dl className="kv">
                    <dt>单笔上限</dt><dd>{fmtMoney(dv.limits?.max_order_notional ?? 0, 0)}</dd>
                    <dt>每分钟</dt><dd>{dv.limits?.max_orders_per_minute ?? '—'} 笔</dd>
                    <dt>当日亏损熔断</dt><dd>{fmtPct(dv.limits?.max_daily_loss ?? 0, 0)}</dd>
                    <dt>送单 / 拒单</dt><dd>{dv.stats?.sent ?? 0} / {dv.stats?.rejected ?? 0}{dv.stats?.unknown ? ` · 查单 ${dv.stats.unknown}` : ''}</dd>
                    <dt>平均滑点</dt><dd>{dv.slippage?.avg_bps == null ? '—' : `${fmtNum(dv.slippage.avg_bps, 1)} bps`}<small className="muted">（相对平台撮合模型）</small></dd>
                    <dt>对账</dt><dd>{!dv.reconcile || !('at' in dv.reconcile) ? '还没对过账' : dv.reconcile.ok ? <span>一致 · {fmtTime(dv.reconcile.at)}</span> : <b className="negative">不一致 · {fmtTime(dv.reconcile.at)}</b>}</dd>
                  </dl>
                </>
              ) : (
                <p className="muted" style={{ margin: 0 }}>平台撮合：按下一根 K 线开盘价加手续费和滑点模拟成交，和回测同一套代码。想用交易所的真实撮合，启动时选「交易所模拟盘」。</p>
              )}
            </div>
          </section>
        </aside>
      </div>

      <section className="panel">
        <div className="tabs" role="tablist" style={{ padding: '0 10px' }}>
          {([['positions', `持仓（${positions.length}）`], ...(isDemo ? [['orders', `委托（${orders.length}）`]] : []), ['fills', `成交（${session.fills.length}）`], ['logs', '日志']] as [Tab, string][]).map(([k, l]) => (
            <button key={k} aria-pressed={tab === k} onClick={() => setTab(k)}>{l}</button>
          ))}
        </div>
        <div className="table-wrap terminal-table">
          {tab === 'positions' && (positions.length ? (
            <table>
              <thead><tr><th>标的</th><th className="right">数量</th><th className="right">成本</th><th className="right">现价</th><th className="right">市值</th><th className="right">浮动盈亏</th></tr></thead>
              <tbody>{positions.map((p) => (
                <tr key={p.security}><td>{p.security}</td><td className="right">{fmtQty(p.amount)}</td><td className="right">{fmtMoney(p.avg_cost, 4)}</td><td className="right">{fmtMoney(p.last_price, 4)}</td><td className="right">{fmtMoney(p.value)}</td><td className={`right ${tone(p.unrealized_pnl)}`}>{fmtMoney(p.unrealized_pnl)}（{fmtPct(p.pnl_ratio, 2, true)}）</td></tr>
              ))}</tbody>
            </table>
          ) : <EmptyState glyph="○" title="空仓" />)}
          {tab === 'orders' && (orders.length ? (
            <table>
              <thead><tr><th>时间</th><th>标的</th><th>方向</th><th className="right">数量</th><th className="right">参考价</th><th className="right">成交均价</th><th className="right">滑点</th><th>状态</th></tr></thead>
              <tbody>{orders.map((o) => {
                const [label, cls] = ORDER_STATUS[o.status] ?? [o.status, 'pending']
                return (
                  <tr key={o.client_order_id} title={`客户端订单号 ${o.client_order_id}${o.exchange_order_id ? ` · 交易所订单号 ${o.exchange_order_id}` : ''}`}>
                    <td>{fmtTime(o.submitted_at)}</td><td>{o.security}</td>
                    <td><span className={`side ${o.side}`}>{o.side === 'buy' ? '买入' : '卖出'}</span></td>
                    <td className="right">{fmtQty(o.filled_qty !== '0' ? o.filled_qty : o.qty)}</td>
                    <td className="right">{fmtMoney(o.ref_price, 4)}</td>
                    <td className="right">{num(o.avg_price) ? fmtMoney(o.avg_price, 4) : '—'}</td>
                    <td className={`right ${o.slippage_bps == null ? '' : o.slippage_bps > 0 ? 'negative' : 'positive'}`}>{o.slippage_bps == null ? '—' : `${fmtNum(o.slippage_bps, 1)} bps`}</td>
                    <td><span className={`table-pill ${cls}`}>{label}</span>{o.message && <small>{o.message}</small>}</td>
                  </tr>
                )
              })}</tbody>
            </table>
          ) : <EmptyState glyph="⇄" title="还没有委托">策略发出信号后，委托会在下一根 K 线开盘时送到模拟盘。</EmptyState>)}
          {tab === 'fills' && (session.fills.length ? (
            <table>
              <thead><tr><th>时间</th><th>标的</th><th>方向</th><th className="right">数量</th><th className="right">价格</th><th className="right">手续费</th><th>备注</th></tr></thead>
              <tbody>{[...session.fills].reverse().map((f, i) => (
                <tr key={f.fill_id ?? i}><td>{fmtTime(f.ts)}</td><td>{f.security}</td><td><span className={`side ${f.side}`}>{f.side === 'buy' ? '买入' : '卖出'}</span></td><td className="right">{fmtQty(f.amount)}</td><td className="right">{fmtMoney(f.price, 4)}</td><td className="right">{fmtMoney(f.fee, 4)} {f.fee_currency ?? ''}</td><td className="wrap"><small>{(f as { note?: string }).note || f.reason}</small></td></tr>
              ))}</tbody>
            </table>
          ) : <EmptyState glyph="⇄" title="还没有成交" />)}
          {tab === 'logs' && (
            <div className="logs" style={{ maxHeight: 340, margin: 12 }}>
              {[...session.events.map((e) => ({ ts: e.ts, level: e.level, message: e.message, kind: '事件' })), ...session.logs.map((l) => ({ ...l, kind: '策略' })),
                ...(feed.events ?? []).map((e) => ({ ...e, kind: '行情' }))]
                .sort((a, b) => b.ts - a.ts)
                .map((l, i) => <div key={i} className={`log-line ${l.level}`}><span>{fmtTime(l.ts)}</span><b>{l.kind}</b>{l.message}</div>)}
            </div>
          )}
        </div>
      </section>
      {t?.bars_error && t.bars.length > 0 && <Callout tone="warn">{t.bars_error}</Callout>}
    </div>
  )
}

function FeedBadge({ mode, healthy, lag, source }: { mode?: string; healthy?: boolean; lag?: number; source: string }) {
  if (source === 'demo') return <Tag tone="warn">演示行情</Tag>
  if (mode === 'ws' && healthy) return <Tag tone="ok">实时推送{lag ? ` · 延迟 ${(lag / 1000).toFixed(1)}s` : ''}</Tag>
  if (mode === 'ws') return <Tag tone="warn">推送中断，改用轮询</Tag>
  return <Tag>轮询行情</Tag>
}
