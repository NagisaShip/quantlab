/**
 * 执行仿真报告的展示部件：成本指标条、成本分解、按市场状态的成本、成本-风险前沿、规模-成本曲线、逐步回放图、提示列表。
 * AI 研究室的报告卡片（紧凑）、下单执行页面（完整）共用。
 */
import { useMemo, useState } from 'react'
import type { ExecAlert, ExecFill, ExecReport, ExecSummary, FrontierPoint, TimelineRow } from '../apiExecution'
import { Checks } from './factorReport'

export const EXEC_STATUS_TONE: Record<string, string> = { candidate: '', active: 'ok', retired: 'muted' }
export const STATE_COLOR = ['var(--faint)', 'var(--up)', 'var(--down)', 'var(--amber)', 'var(--violet)']
export const bp = (v: number | null | undefined, d = 2) => (v === null || v === undefined || !Number.isFinite(v) ? '—' : `${v.toFixed(d)}`)
const pc = (v: number | null | undefined, d = 0) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`)

export function ExecStats({ s, compact }: { s: Partial<ExecSummary> | null | undefined; compact?: boolean }) {
  if (!s) return null
  const beat = s.beat_twap_bps ?? null
  const cells: [string, string, string?, string?][] = [
    [compact ? '成本 bp' : '执行落差 bp', bp(s.is_bps), undefined, '相对到达中间价，含手续费与没做完的机会成本'],
    ['比 TWAP 省', bp(beat), beat === null ? undefined : beat >= 0 ? 'ok' : 'bad', `TWAP ${bp(s.twap_is_bps)} bp · 市价 ${bp(s.market_is_bps)} bp`],
    ['相对 VWAP', bp(s.vs_vwap_bps), undefined, '成交均价比区间 VWAP 贵多少 bp'],
    ['成本波动', bp(s.is_std_bps, 1), undefined, '各片段成本的标准差'],
    ['完成率', pc(s.fill_rate, 1)],
    [compact ? '被动' : '被动成交', pc(s.maker_ratio), undefined, '挂单成交占比'],
  ]
  if (!compact) cells.push(['markout(5)', bp(s.markout_5), (s.markout_5 ?? 0) < -3 ? 'bad' : undefined, '成交后 5 步价格朝有利方向走了多少 bp；负数 = 被逆向选择'])
  return (
    <div className={`fr-stats${compact ? ' compact' : ''}`}>
      {cells.map(([k, v, t, tip]) => <div key={k} title={tip}><span>{k}</span><b className={t}>{v}</b></div>)}
    </div>
  )
}

/** 成本分解：价差、冲击、择时、手续费、机会成本，加起来 = 执行落差。 */
export function CostBreakdown({ s }: { s: Pick<ExecSummary, 'spread_bps' | 'impact_bps' | 'timing_bps' | 'fee_bps' | 'opportunity_bps' | 'is_bps'> }) {
  const rows: [string, number, string][] = [
    ['价差', s.spread_bps, '成交价 − 被自己推过的中间价；挂单成交为负 = 赚到价差'],
    ['冲击', s.impact_bps, '自己的单子把中间价推走的部分'],
    ['择时', s.timing_bps, '执行期间市场自己的漂移'],
    ['手续费', s.fee_bps, '挂单 / 吃单费率'],
    ['机会成本', s.opportunity_bps, '没做完的部分按结束时价格计'],
  ]
  const max = Math.max(...rows.map((r) => Math.abs(r[1] ?? 0)), Math.abs(s.is_bps ?? 0), 0.01)
  return (
    <div className="ex-break">
      {rows.map(([k, v, tip]) => (
        <div key={k} className="ex-brow" title={tip}>
          <span>{k}</span>
          <span className="ex-axis">
            <i className={(v ?? 0) >= 0 ? 'pos' : 'neg'} style={{ width: `${(Math.abs(v ?? 0) / max) * 50}%` }} />
          </span>
          <b className="num">{bp(v)}</b>
        </div>
      ))}
      <div className="ex-brow total"><span>合计</span>
        <span className="ex-axis"><i className={(s.is_bps ?? 0) >= 0 ? 'pos' : 'neg'} style={{ width: `${(Math.abs(s.is_bps ?? 0) / max) * 50}%` }} /></span>
        <b className="num">{bp(s.is_bps)}</b></div>
      <div className="hint">单位 bp，正数是成本。片段之间择时成本波动很大，比较方案时看同一批片段上的差值。</div>
    </div>
  )
}

/** 成本-风险前沿：横轴成本波动，纵轴平均成本。越靠左下越好。 */
export function FrontierScatter({ points, current }: { points: FrontierPoint[]; current?: string }) {
  const [hover, setHover] = useState<string | null>(null)
  if (points.length < 2) return null
  const W = 420
  const H = 240
  const P = { l: 46, r: 12, t: 12, b: 34 }
  const xs = points.map((p) => p.is_std_bps)
  const ys = points.map((p) => p.is_bps)
  const x0 = Math.min(...xs) * 0.9
  const x1 = Math.max(...xs) * 1.08
  const ylo = Math.min(...ys, 0)
  const yhi = Math.max(...ys, 0)
  const pad = (yhi - ylo) * 0.12 || 1
  const X = (v: number) => P.l + ((v - x0) / (x1 - x0 || 1)) * (W - P.l - P.r)
  const Y = (v: number) => P.t + (1 - (v - (ylo - pad)) / (yhi + pad - (ylo - pad))) * (H - P.t - P.b)
  const ticks = (lo: number, hi: number) => Array.from({ length: 5 }, (_, i) => lo + ((hi - lo) * i) / 4)
  // 标签避让：和已放好的标签挨得太近就往下挪一行
  const placed: [number, number][] = []
  const labelY = points.map((p) => {
    const x = X(p.is_std_bps)
    let y = Y(p.is_bps) + 4
    while (placed.some(([px, py]) => Math.abs(px - x) < 56 && Math.abs(py - y) < 12)) y += 12
    placed.push([x, y])
    return y
  })
  return (
    <figure className="pf-frontier">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="成本-风险前沿">
        {ticks(ylo - pad, yhi + pad).map((t) => <g key={'y' + t}><line className="chart-gridline" x1={P.l} x2={W - P.r} y1={Y(t)} y2={Y(t)} /><text className="chart-label" x={P.l - 6} y={Y(t) + 4} textAnchor="end">{t.toFixed(1)}</text></g>)}
        {ticks(x0, x1).map((t) => <text key={'x' + t} className="chart-label" x={X(t)} y={H - P.b + 14} textAnchor="middle">{t.toFixed(1)}</text>)}
        <text className="chart-label" x={(W + P.l) / 2} y={H - 4} textAnchor="middle">成本波动（bp）</text>
        {points.map((p, i) => {
          const on = p.label === current
          return (
            <g key={p.label} onMouseEnter={() => setHover(`${p.label}：平均 ${bp(p.is_bps)} bp，波动 ${bp(p.is_std_bps, 1)} bp，平均用时 ${p.duration.toFixed(0)} 步`)} onMouseLeave={() => setHover(null)}>
              <circle cx={X(p.is_std_bps)} cy={Y(p.is_bps)} r={on ? 6 : 4} style={{ fill: on ? 'var(--amber)' : 'var(--accent)', stroke: 'var(--sheet)' }} strokeWidth={on ? 2 : 1} />
              <text className="chart-label" x={X(p.is_std_bps) + 7} y={labelY[i]}>{p.label}</text>
            </g>
          )
        })}
      </svg>
      <div className="hint">{hover ?? '纵轴是平均成本（bp），越低越好；横轴是成本的波动，越小越可预期。同一批片段上比较。'}</div>
    </figure>
  )
}

/** 母单规模 vs 成本，以及平方根拟合。 */
export function CostCurveChart({ c }: { c: ExecReport['cost_curve'] }) {
  if (!c?.points?.length) return null
  const W = 420
  const H = 220
  const P = { l: 46, r: 12, t: 12, b: 34 }
  const xmax = Math.max(...c.points.map((p) => p.size_pct)) * 1.05
  const ys = [...c.points.map((p) => p.ex_fee_bps), 0]
  const ylo = Math.min(...ys)
  const yhi = Math.max(...ys, c.a_bps + c.b_bps * Math.sqrt(xmax))
  const pad = (yhi - ylo) * 0.1 || 1
  const X = (v: number) => P.l + (v / xmax) * (W - P.l - P.r)
  const Y = (v: number) => P.t + (1 - (v - (ylo - pad)) / (yhi + pad - (ylo - pad))) * (H - P.t - P.b)
  const fit = Array.from({ length: 40 }, (_, i) => (xmax * (i + 1)) / 40).map((x, i) => `${i ? 'L' : 'M'}${X(x).toFixed(1)},${Y(c.a_bps + c.b_bps * Math.sqrt(x)).toFixed(1)}`).join(' ')
  const ticks = (lo: number, hi: number) => Array.from({ length: 5 }, (_, i) => lo + ((hi - lo) * i) / 4)
  return (
    <figure className="pf-frontier">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="母单规模与成本">
        {ticks(ylo - pad, yhi + pad).map((t) => <g key={'y' + t}><line className="chart-gridline" x1={P.l} x2={W - P.r} y1={Y(t)} y2={Y(t)} /><text className="chart-label" x={P.l - 6} y={Y(t) + 4} textAnchor="end">{t.toFixed(1)}</text></g>)}
        {ticks(0, xmax).map((t) => <text key={'x' + t} className="chart-label" x={X(t)} y={H - P.b + 14} textAnchor="middle">{(t * 100).toFixed(0)}%</text>)}
        <text className="chart-label" x={(W + P.l) / 2} y={H - 4} textAnchor="middle">母单 / 执行期成交量</text>
        <path d={fit} fill="none" style={{ stroke: 'var(--accent)' }} strokeWidth={1.6} />
        {c.points.map((p) => <circle key={p.size_pct} cx={X(p.size_pct)} cy={Y(p.ex_fee_bps)} r={4} style={{ fill: 'var(--amber)' }}><title>{`${(p.size_pct * 100).toFixed(0)}%：${bp(p.ex_fee_bps)} bp（不含手续费）`}</title></circle>)}
      </svg>
      <div className="hint">不含手续费的成本 ≈ {bp(c.a_bps)} + {bp(c.b_bps)} × √参与率（R² {c.r2.toFixed(2)}）。在策略里 set_execution() 用的就是这条曲线。</div>
    </figure>
  )
}

/** 逐步回放：上面是价格（中间价、被自己推过的中间价、挂单价、成交点）和市场状态色带，下面是计划与实际的完成进度。 */
export function ReplayChart({ timeline, fills, alerts, context, offset, states, height = 300, cursor }: {
  timeline: TimelineRow[]; fills: ExecFill[]; alerts?: ExecAlert[]
  context?: { t: number; mid: number; state: number }[]; offset?: number; states: string[]; height?: number; cursor?: number | null
}) {
  const [hover, setHover] = useState<number | null>(null)
  const W = 720
  const H = height
  const P = { l: 64, r: 12, t: 10, b: 24 }
  const topH = (H - P.t - P.b) * 0.66
  const gap = 18
  const botH = H - P.t - P.b - topH - gap
  const pre = context && offset !== undefined ? offset : 0
  const total = context ? context.length : timeline.length
  const xi = (i: number) => P.l + (i / Math.max(total - 1, 1)) * (W - P.l - P.r)
  const prices = useMemo(() => {
    const v = [...timeline.map((r) => r.mid), ...timeline.map((r) => r.mid_shift), ...fills.map((f) => f.price),
      ...timeline.map((r) => r.order_px).filter((x): x is number => x !== null), ...(context ?? []).map((c) => c.mid)]
    return [Math.min(...v), Math.max(...v)]
  }, [timeline, fills, context])
  const pad = (prices[1] - prices[0]) * 0.08 || prices[0] * 1e-4
  const Yp = (v: number) => P.t + (1 - (v - (prices[0] - pad)) / (prices[1] - prices[0] + 2 * pad)) * topH
  const yb0 = P.t + topH + gap
  const Yb = (v: number) => yb0 + (1 - v) * botH
  const line = (vals: [number, number][]) => vals.map(([i, v], k) => `${k ? 'L' : 'M'}${xi(i).toFixed(1)},${v.toFixed(1)}`).join(' ')
  const mid0 = context ? context.map((c, i) => [i, Yp(c.mid)] as [number, number]) : timeline.map((r, i) => [i, Yp(r.mid)] as [number, number])
  const shifted = timeline.map((r) => [pre + r.j, Yp(r.mid_shift)] as [number, number])
  const target = timeline.map((r) => [pre + r.j, Yb(r.target)] as [number, number])
  const filled = timeline.map((r) => [pre + r.j, Yb(r.filled)] as [number, number])
  const bandStates = context ? context.map((c) => c.state) : timeline.map((r) => r.state)
  const fmtP = (v: number) => (v >= 1000 ? v.toFixed(1) : v >= 1 ? v.toFixed(3) : v.toPrecision(4))
  const hi = hover ?? cursor ?? null
  const row = hi !== null ? timeline.find((r) => pre + r.j === hi) : undefined
  const stepW = (W - P.l - P.r) / Math.max(total - 1, 1)
  return (
    <figure className="ex-replay">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="执行回放"
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect()
          const x = ((e.clientX - r.left) / r.width) * W
          setHover(Math.max(0, Math.min(total - 1, Math.round(((x - P.l) / (W - P.l - P.r)) * (total - 1)))))
        }} onMouseLeave={() => setHover(null)}>
        {bandStates.map((s, i) => s ? <rect key={i} x={xi(i) - stepW / 2} y={P.t + topH - 5} width={stepW + 0.5} height={5} style={{ fill: STATE_COLOR[s] }} opacity={0.7} /> : null)}
        {context && <rect x={xi(pre)} y={P.t} width={xi(pre + timeline.length - 1) - xi(pre)} height={topH} style={{ fill: 'var(--accent)' }} opacity={0.05} />}
        {[0, 0.5, 1].map((f) => { const v = prices[0] - pad + (prices[1] - prices[0] + 2 * pad) * (1 - f); return <g key={f}><line className="chart-gridline" x1={P.l} x2={W - P.r} y1={P.t + f * topH} y2={P.t + f * topH} /><text className="chart-label" x={P.l - 6} y={P.t + f * topH + 4} textAnchor="end">{fmtP(v)}</text></g> })}
        <path d={line(mid0)} fill="none" style={{ stroke: 'var(--muted)' }} strokeWidth={1.2} />
        <path d={line(shifted)} fill="none" style={{ stroke: 'var(--accent)' }} strokeWidth={1.2} strokeDasharray="4 3" />
        {timeline.map((r) => r.order_px !== null ? <rect key={'o' + r.j} x={xi(pre + r.j) - 2} y={Yp(r.order_px) - 0.75} width={4} height={1.5} style={{ fill: 'var(--amber)' }} /> : null)}
        {fills.map((f, i) => (
          <circle key={'f' + i} cx={xi(pre + f.j)} cy={Yp(f.price)} r={Math.max(2, Math.min(6, Math.sqrt(f.qty) * 10))}
            style={{ fill: f.maker ? 'var(--sheet)' : 'var(--accent)', stroke: f.maker ? 'var(--up)' : 'var(--accent)' }} strokeWidth={1.4}>
            <title>{`第 ${f.j + 1} 步 ${f.maker ? '挂单成交' : '吃单'} ${(f.qty * 100).toFixed(1)}% @ ${fmtP(f.price)}`}</title>
          </circle>
        ))}
        {[0, 0.5, 1].map((f) => <g key={'b' + f}><line className="chart-gridline" x1={P.l} x2={W - P.r} y1={Yb(f)} y2={Yb(f)} /><text className="chart-label" x={P.l - 6} y={Yb(f) + 4} textAnchor="end">{f * 100}%</text></g>)}
        <path d={line(target)} fill="none" style={{ stroke: 'var(--faint)' }} strokeWidth={1.4} strokeDasharray="4 3" />
        <path d={line(filled)} fill="none" style={{ stroke: 'var(--accent)' }} strokeWidth={1.8} />
        {(alerts ?? []).filter((a) => a.level !== 'info').map((a, i) => <text key={'a' + i} x={xi(pre + a.k)} y={yb0 - 4} className="chart-label" textAnchor="middle" style={{ fill: 'var(--danger)' }}>!</text>)}
        {hi !== null && <line x1={xi(hi)} x2={xi(hi)} y1={P.t} y2={Yb(0)} style={{ stroke: 'var(--line-strong, var(--faint))' }} strokeWidth={1} />}
        <text className="chart-label" x={P.l} y={H - 6}>{context ? `执行前 ${pre} 步` : '第 1 步'}</text>
        <text className="chart-label" x={W - P.r} y={H - 6} textAnchor="end">{context ? `执行后 ${total - pre - timeline.length} 步` : `第 ${timeline.length} 步`}</text>
      </svg>
      <figcaption className="legend">
        <span><i style={{ background: 'var(--muted)' }} />中间价</span>
        <span><i style={{ background: 'var(--accent)' }} />被自己推过的中间价 / 实际完成</span>
        <span><i style={{ background: 'var(--amber)' }} />挂单价</span>
        <span><i style={{ background: 'var(--faint)' }} />计划完成</span>
        {states.slice(1).map((s, i) => <span key={s}><i style={{ background: STATE_COLOR[i + 1] }} />{s}</span>)}
      </figcaption>
      <div className="hint ex-hover">{row
        ? `第 ${row.j + 1} 步 · ${states[row.state]} · 中间价 ${fmtP(row.mid)} · 计划 ${(row.target * 100).toFixed(0)}% · 完成 ${(row.filled * 100).toFixed(0)}%${row.order_px ? ` · 挂单 ${fmtP(row.order_px)}` : ''}${row.actions.length ? ` · ${row.actions.join('、')}` : ''}`
        : '空心圆 = 挂单成交，实心 = 吃单；下方虚线是计划完成进度，实线是实际进度。'}</div>
    </figure>
  )
}

export function AlertList({ alerts }: { alerts: ExecAlert[] }) {
  if (!alerts.length) return <div className="hint">这个片段没有触发提示。</div>
  return (
    <ul className="ex-alerts">
      {alerts.map((a, i) => <li key={i} className={a.level}><span className="num">第 {a.k + 1} 步</span>{a.text}</li>)}
    </ul>
  )
}

export function StateTable({ rows }: { rows: ExecReport['by_state'] }) {
  if (!rows.length) return null
  return (
    <table className="fr-table">
      <thead><tr><th>开始时的市场状态</th><th className="right">片段</th><th className="right">本方案 bp</th><th className="right">TWAP bp</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.state}><td><i className="ex-dot" style={{ background: STATE_COLOR[['平静', '趋势上行', '趋势下行', '剧烈波动', '流动性枯竭'].indexOf(r.state)] }} />{r.state}</td>
          <td className="right num">{r.n}</td><td className="right num">{bp(r.is_bps)}</td><td className="right num muted">{bp(r.twap_is_bps)}</td></tr>
      ))}</tbody>
    </table>
  )
}

export function ExecutionFullReport({ report, states }: { report: ExecReport; states: string[] }) {
  const s = report.summary
  const ex = report.example
  return (
    <div className="fr-full">
      <ExecStats s={s} />
      <div className="fr-badges">
        <span className={`cb-badge ${report.admitted ? 'ok' : 'warn'}`}>{report.admitted ? '满足启用条件' : '未满足启用条件'}</span>
        <span className="cb-badge">{s.episodes} 个片段 · 平均 {s.duration.toFixed(0)} 步做完 · {s.children.toFixed(0)} 笔子单</span>
        <span className="cb-badge">平均母单 {Math.round(s.notional_avg).toLocaleString()} USDT · 参与率 {pc(s.participation, 1)}</span>
        {report.tape.kline && <span className="cb-badge warn">盘口由 K 线推算</span>}
        {report.tape.synthetic && <span className="cb-badge warn">含合成盘口</span>}
      </div>
      {report.tape.warnings.length > 0 && <ul className="fr-warn">{report.tape.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
      <div className="fr-grid">
        <section><h4>成本分解（bp）</h4><CostBreakdown s={s} /></section>
        <section><h4>检查</h4><Checks checks={report.checks} />
          <h4 style={{ marginTop: 14 }}>按市场状态</h4><StateTable rows={report.by_state} /></section>
      </div>
      {ex && (
        <section>
          <h4>一个片段的回放（{ex.symbol} · {ex.side > 0 ? '买入' : '卖出'} · 成本 {bp(ex.is_bps)} bp）</h4>
          <ReplayChart timeline={ex.timeline} fills={ex.fills} alerts={ex.alerts} states={states} />
          <AlertList alerts={ex.alerts} />
        </section>
      )}
      <div className="fr-grid">
        <section><h4>成本-风险前沿</h4><FrontierScatter points={report.frontier} current={report.frontier.find((p) => p.algo === s.algo)?.label} /></section>
        <section><h4>母单规模与成本</h4><CostCurveChart c={report.cost_curve} /></section>
      </div>
      {report.per_symbol.length > 1 && (
        <section>
          <h4>各标的</h4>
          <table className="fr-table">
            <thead><tr><th>标的</th><th>盘口</th><th className="right">成本 bp</th><th className="right">波动</th><th className="right">完成</th><th className="right">被动</th><th className="right">价差 bp</th></tr></thead>
            <tbody>{report.per_symbol.map((r) => (
              <tr key={r.symbol}><td>{r.symbol}</td><td className="muted">{SOURCE_LABEL[r.source] ?? r.source}</td><td className="right num">{bp(r.is_bps)}</td>
                <td className="right num">{bp(r.is_std_bps, 1)}</td><td className="right num">{pc(r.fill_rate, 1)}</td><td className="right num">{pc(r.maker_ratio)}</td>
                <td className="right num muted">{bp(r.spread_bps)}</td></tr>
            ))}</tbody>
          </table>
        </section>
      )}
    </div>
  )
}

export const SOURCE_LABEL: Record<string, string> = { recording: '录制盘口', kline: 'K 线推算', synthetic: '合成' }
