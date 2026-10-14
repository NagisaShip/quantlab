import { useMemo } from 'react'
import { money, shortTime } from '../api'

const W = 850
const H = 270
const PAD = { l: 68, r: 24, t: 23, b: 40 }

/** 与实验三一致的 6 色序列，用于多策略曲线。 */
export const PALETTE = ['#4f66e0', '#c98a1b', '#0e9a9a', '#8b63d6', '#8c94a6', '#d0607f']

function scaleY(lo: number, hi: number, height: number, pad: typeof PAD) {
  const span = hi - lo || 1
  const inner = height - pad.t - pad.b
  return (v: number) => pad.t + (1 - (v - lo) / span) * inner
}

function xAt(i: number, n: number, width: number, pad: typeof PAD) {
  const inner = width - pad.l - pad.r
  return pad.l + (n <= 1 ? inner / 2 : (i / (n - 1)) * inner)
}

function ticks(lo: number, hi: number, count = 4) {
  return Array.from({ length: count + 1 }, (_, i) => lo + ((hi - lo) * i) / count)
}

function axisLabels(timestamps: number[], count = 6) {
  const n = timestamps.length
  if (!n) return []
  const spanMs = timestamps[n - 1] - timestamps[0]
  const fmt = (t: number) =>
    spanMs < 48 * 3_600_000 ? shortTime(t).slice(11, 16) : shortTime(t).slice(5, 10)
  return Array.from({ length: count + 1 }, (_, i) => {
    const idx = Math.min(n - 1, Math.round(((n - 1) * i) / count))
    return { x: xAt(idx, n, W, PAD), label: fmt(timestamps[idx]), first: i === 0, last: i === count }
  })
}

export interface Series {
  name: string
  equity: number[]
  color?: string
  dashed?: boolean
}

/** 多策略净值曲线。所有策略共用同一坐标轴，才谈得上"比较"。 */
export function MultiEquityChart({
  series,
  timestamps,
  unit = 'USDT',
}: {
  series: Series[]
  timestamps: number[]
  unit?: string
}) {
  const chart = useMemo(() => {
    const valid = series.filter((s) => s.equity.length > 1)
    if (!valid.length || timestamps.length < 2) return null
    const all = valid.flatMap((s) => s.equity)
    let lo = Math.min(...all)
    let hi = Math.max(...all)
    const margin = Math.max((hi - lo) * 0.15, hi * 0.006)
    lo -= margin
    hi += margin
    const y = scaleY(lo, hi, H, PAD)
    const paths = valid.map((s, i) => {
      const n = s.equity.length
      const d = s.equity
        .map((v, k) => `${k === 0 ? 'M' : 'L'}${xAt(k, n, W, PAD).toFixed(2)},${y(v).toFixed(2)}`)
        .join(' ')
      return {
        name: s.name,
        color: s.color ?? PALETTE[i % PALETTE.length],
        dashed: s.dashed,
        d,
        last: { x: xAt(n - 1, n, W, PAD), y: y(s.equity[n - 1]), v: s.equity[n - 1] },
      }
    })
    return { lo, hi, y, paths, yTicks: ticks(lo, hi), xLabels: axisLabels(timestamps) }
  }, [series, timestamps])

  if (!chart) return <div className="empty-state"><h3>数据点不足，无法绘制净值曲线</h3></div>

  return (
    <>
      <div className="legend">
        {chart.paths.map((p) => (
          <span key={p.name}>
            <i style={{ background: p.color }} />
            {p.name}
          </span>
        ))}
      </div>
      <div className="chart-wrap">
        <div className="chart-frame">
        <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`各策略净值曲线（${unit}）`}>
          {chart.yTicks.map((t, i) => (
            <g key={i}>
              <line className="chart-gridline" x1={PAD.l} x2={W - PAD.r} y1={chart.y(t)} y2={chart.y(t)} />
              <text className="chart-label" x={PAD.l - 12} y={chart.y(t) + 4} textAnchor="end">
                {money(t, 0)}
              </text>
            </g>
          ))}
          {chart.paths.map((p) => (
            <g key={p.name}>
              <path
                d={p.d}
                fill="none"
                style={{ stroke: p.color }}
                strokeWidth={p.dashed ? 1.5 : 1.8}
                strokeDasharray={p.dashed ? '5 4' : undefined}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <circle cx={p.last.x} cy={p.last.y} r={3} style={{ fill: p.color }} />
            </g>
          ))}
          <line className="chart-axis" x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} />
          {chart.xLabels.map((l, i) => (
            <text
              key={i}
              className="chart-label"
              x={l.x}
              y={H - 13}
              textAnchor={l.first ? 'start' : l.last ? 'end' : 'middle'}
            >
              {l.label}
            </text>
          ))}
        </svg>
        </div>
      </div>
    </>
  )
}

/** 单策略净值 + 可选基准。 */
export function EquityChart({
  equity,
  timestamps,
  benchmark,
}: {
  equity: number[]
  timestamps: number[]
  benchmark?: number[]
}) {
  const series: Series[] = [{ name: '策略净值', equity }]
  if (benchmark && benchmark.length === equity.length) {
    series.push({ name: '买入持有基准', equity: benchmark, color: 'var(--bench)', dashed: true })
  }
  return <MultiEquityChart series={series} timestamps={timestamps} />
}

/** 回撤（水下）曲线。 */
export function DrawdownChart({ drawdown, timestamps }: { drawdown: number[]; timestamps: number[] }) {
  const chart = useMemo(() => {
    if (drawdown.length < 2) return null
    const max = Math.max(...drawdown, 0.0001)
    const inner = H - PAD.t - PAD.b
    const y = (v: number) => PAD.t + (v / max) * inner
    const n = drawdown.length
    const d = drawdown
      .map((v, i) => `${i === 0 ? 'M' : 'L'}${xAt(i, n, W, PAD).toFixed(2)},${y(v).toFixed(2)}`)
      .join(' ')
    return {
      y,
      max,
      line: d,
      area: `${d} L${xAt(n - 1, n, W, PAD).toFixed(2)},${PAD.t} L${PAD.l},${PAD.t} Z`,
      xLabels: axisLabels(timestamps),
    }
  }, [drawdown, timestamps])

  if (!chart) return <div className="empty-state"><h3>数据点不足，无法绘制回撤曲线</h3></div>

  return (
    <>
      <div className="chart-wrap">
        <div className="chart-frame">
        <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="回撤曲线">
          {[0, 0.25, 0.5, 0.75, 1].map((f, i) => (
            <g key={i}>
              <line
                className="chart-gridline"
                x1={PAD.l} x2={W - PAD.r}
                y1={chart.y(chart.max * f)} y2={chart.y(chart.max * f)}
              />
              <text className="chart-label" x={PAD.l - 12} y={chart.y(chart.max * f) + 4} textAnchor="end">
                {`-${(chart.max * f * 100).toFixed(0)}%`}
              </text>
            </g>
          ))}
          <path d={chart.area} style={{ fill: 'var(--sunken)' }} stroke="none" />
          <path d={chart.line} fill="none" style={{ stroke: 'var(--muted)' }} strokeWidth={1.5} />
          <line className="chart-axis" x1={PAD.l} x2={W - PAD.r} y1={PAD.t} y2={PAD.t} />
          {chart.xLabels.map((l, i) => (
            <text
              key={i}
              className="chart-label"
              x={l.x}
              y={H - 13}
              textAnchor={l.first ? 'start' : l.last ? 'end' : 'middle'}
            >
              {l.label}
            </text>
          ))}
        </svg>
        </div>
      </div>
      <div className="legend">
        <span><i style={{ background: 'var(--muted)' }} />回撤（自历史峰值）</span>
      </div>
    </>
  )
}

/** 价格 K 线。 */
export function PriceChart({ bars }: { bars: { t: number; o: number; h: number; l: number; c: number }[] }) {
  const chart = useMemo(() => {
    if (bars.length < 2) return null
    const lo = Math.min(...bars.map((b) => b.l))
    const hi = Math.max(...bars.map((b) => b.h))
    const y = scaleY(lo, hi, H, PAD)
    const bw = Math.max(1, (W - PAD.l - PAD.r) / bars.length - 1)
    return { y, bw, yTicks: ticks(lo, hi) }
  }, [bars])

  if (!chart) return <div className="empty-state"><h3>数据点不足</h3></div>

  return (
    <div className="chart-wrap">
      <div className="chart-frame">
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="价格走势">
        {chart.yTicks.map((t, i) => (
          <g key={i}>
            <line className="chart-gridline" x1={PAD.l} x2={W - PAD.r} y1={chart.y(t)} y2={chart.y(t)} />
            <text className="chart-label" x={PAD.l - 12} y={chart.y(t) + 4} textAnchor="end">
              {t >= 1000 ? t.toFixed(0) : t.toFixed(2)}
            </text>
          </g>
        ))}
        {bars.map((b, i) => {
          const x = xAt(i, bars.length, W, PAD)
          const up = b.c >= b.o
          const color = up ? 'var(--up)' : 'var(--down)'
          const top = chart.y(Math.max(b.o, b.c))
          const bottom = chart.y(Math.min(b.o, b.c))
          return (
            <g key={i}>
              <line x1={x} x2={x} y1={chart.y(b.h)} y2={chart.y(b.l)} style={{ stroke: color }} strokeWidth={1} />
              <rect x={x - chart.bw / 2} y={top} width={chart.bw} height={Math.max(1, bottom - top)} style={{ fill: color }} />
            </g>
          )
        })}
        <line className="chart-axis" x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} />
      </svg>
      </div>
      <div className="legend">
        <span><i style={{ background: 'var(--up)' }} />收涨</span>
        <span><i style={{ background: 'var(--down)' }} />收跌</span>
      </div>
    </div>
  )
}

/** 横向条形对比：压力场景这类少量类目比较。 */
export function CompareBars({
  rows,
  format,
}: {
  rows: { label: string; value: number }[]
  format: (v: number) => string
}) {
  const max = Math.max(...rows.map((r) => Math.abs(r.value)), 0.0001)
  return (
    <table>
      <tbody>
        {rows.map((r) => (
          <tr key={r.label}>
            <td style={{ width: 130, padding: '10px 12px 10px 0', borderBottom: '1px solid var(--line-2)' }}>
              {r.label}
            </td>
            <td style={{ padding: '10px 0', borderBottom: '1px solid var(--line-2)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <div style={{ flex: 1, background: 'var(--line-2)', height: 8, borderRadius: 4 }}>
                  <div
                    style={{
                      width: `${(Math.abs(r.value) / max) * 100}%`,
                      height: '100%',
                      borderRadius: 4,
                      background: r.value >= 0 ? 'var(--up)' : 'var(--down)',
                    }}
                  />
                </div>
                <span className={r.value >= 0 ? 'positive' : 'negative'} style={{ minWidth: 70, textAlign: 'right' }}>
                  {format(r.value)}
                </span>
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** 各策略回撤对比条（右侧窄栏用）。 */
export function DrawdownBars({
  rows,
}: {
  rows: { name: string; mdd: number; color: string }[]
}) {
  const largest = Math.max(0.001, ...rows.map((r) => r.mdd))
  return (
    <div className="dd-list">
      {rows.map((r) => (
        <div className="dd-row" key={r.name}>
          <div className="dd-info">
            <span>{r.name}</span>
            <b>{(r.mdd * 100).toFixed(2)}%</b>
          </div>
          <div className="dd-track">
            <div style={{ width: `${(r.mdd / largest) * 100}%`, background: r.color }} />
          </div>
        </div>
      ))}
    </div>
  )
}
