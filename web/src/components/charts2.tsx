import { useMemo, useRef, useState, type PointerEvent as RPE, type ReactNode } from 'react'
import { fmtMoney, fmtNum, fmtPct, fmtTime } from '../api/research'
import { ChartExpandButton, ChartFullscreen } from './ChartFullscreen'

/** 曲线配色全部取自主题令牌：策略用品牌蓝，基准用石板灰，涨跌用 --up / --down（随红涨绿跌设置切换）。 */
export const COLORS = {
  strategy: 'var(--accent)',
  benchmark: 'var(--bench)',
  excess: 'var(--amber)',
  red: 'var(--down)',
  loss: 'var(--down)',
  up: 'var(--up)',
  down: 'var(--down)',
  amber: 'var(--amber)',
  purple: 'var(--violet)',
}
/** 多序列对比用的中性色板：避开红绿，以免和涨跌混淆；深浅主题下都可读。 */
const EXTRA = ['#4f66e0', '#c98a1b', '#0e9a9a', '#8b63d6', '#8c94a6', '#d0607f', '#6f7fa0', '#a3864f']

export interface LineSeries {
  name: string
  values: number[]
  color?: string
  dashed?: boolean
  area?: boolean
  width?: number
}

type Fmt = 'pct' | 'money' | 'num'
const fmt = (v: number, f: Fmt) => (f === 'pct' ? fmtPct(v, 2) : f === 'money' ? fmtMoney(v, 0) : fmtNum(v, 2))

function niceTicks(lo: number, hi: number, n = 5) {
  const span = hi - lo || Math.abs(hi) || 1
  const raw = span / n
  const mag = Math.pow(10, Math.floor(Math.log10(raw)))
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => span / s <= n) ?? mag * 10
  const start = Math.floor(lo / step) * step
  const out: number[] = []
  for (let v = start; v <= hi + step * 0.001; v += step) out.push(Number(v.toFixed(12)))
  return out
}

/** 多序列折线图，带十字准线与悬停数值。所有序列共用时间轴。 */
export function LineChart({
  series,
  timestamps,
  format = 'pct',
  height = 280,
  width = 900,
  zeroLine = true,
  ariaLabel = '折线图',
  xLabels,
  footer,
}: {
  series: LineSeries[]
  timestamps: number[]
  /** 不是时间轴时（例如训练轮次），用这些文字代替时间刻度。 */
  xLabels?: string[]
  format?: Fmt
  height?: number
  /** 坐标系宽度。容器越窄就给越小的值，文字才不会被缩得看不清。 */
  width?: number
  zeroLine?: boolean
  ariaLabel?: string
  footer?: ReactNode
}) {
  const W = width
  const H = height
  const P = { l: W < 600 ? 54 : 64, r: 14, t: 14, b: 30 }
  const ref = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)
  const [full, setFull] = useState(false)
  const [zoom, setZoom] = useState<[number, number] | null>(null)
  const [drag, setDrag] = useState<{ a: number; b: number } | null>(null)
  const valid = series.filter((s) => s.values.length > 1)
  const n = timestamps.length

  const geo = useMemo(() => {
    if (!valid.length || n < 2) return null
    const a = zoom ? Math.max(0, Math.min(zoom[0], n - 2)) : 0
    const b = zoom ? Math.max(a + 1, Math.min(zoom[1], n - 1)) : n - 1
    const all = valid.flatMap((s) => s.values.slice(a, b + 1).filter((v) => Number.isFinite(v)))
    let lo = Math.min(...all, zeroLine && format === 'pct' ? 0 : Infinity)
    let hi = Math.max(...all, zeroLine && format === 'pct' ? 0 : -Infinity)
    const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.05 || 0.01
    lo -= pad
    hi += pad
    const ticks = niceTicks(lo, hi, 5).filter((t) => t >= lo && t <= hi)
    const span = Math.max(1, b - a)
    const x = (i: number) => P.l + ((i - a) / span) * (W - P.l - P.r)
    const y = (v: number) => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b)
    const paths = valid.map((s, si) => {
      const pts = s.values.slice(a, b + 1).map((v, k) => `${k === 0 ? 'M' : 'L'}${x(a + k).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
      const area = s.area ? `${pts} L${x(b).toFixed(1)},${y(Math.max(lo, 0)).toFixed(1)} L${x(a).toFixed(1)},${y(Math.max(lo, 0)).toFixed(1)} Z` : ''
      return { ...s, color: s.color ?? EXTRA[si % EXTRA.length], d: pts, areaD: area }
    })
    const ticksX = W < 600 ? 3 : 5
    const xt = Array.from({ length: ticksX + 1 }, (_, k) => Math.round(a + (span * k) / ticksX))
    return { lo, hi, ticks, x, y, paths, xt, a, b }
  }, [valid, n, H, W, format, zeroLine, zoom])

  if (!geo) return <div className="empty-state small"><h3>数据点不足，无法绘图</h3></div>

  const idxAt = (clientX: number, clientY: number) => {
    const svg = ref.current
    if (!svg) return 0
    // 全屏横屏时 SVG 被 CSS 旋转 90°，必须用 screenCTM 反变换，不能用 clientX 直接减 left。
    const ctm = svg.getScreenCTM?.()
    let px: number
    if (ctm) {
      const pt = svg.createSVGPoint()
      pt.x = clientX
      pt.y = clientY
      px = pt.matrixTransform(ctm.inverse()).x
    } else {
      const box = svg.getBoundingClientRect()
      px = ((clientX - box.left) / Math.max(1, box.width)) * W
    }
    const t = Math.max(0, Math.min(1, (px - P.l) / (W - P.l - P.r)))
    return Math.round(geo!.a + t * (geo!.b - geo!.a))
  }
  const onMove = (e: RPE<SVGSVGElement>) => {
    if (!geo) return
    const i = idxAt(e.clientX, e.clientY)
    setHover(Math.max(geo.a, Math.min(geo.b, i)))
    if (drag) setDrag({ ...drag, b: i })
  }
  const onDown = (e: RPE<SVGSVGElement>) => {
    if (!geo || (e.pointerType === 'mouse' && e.button !== 0)) return
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* 旧浏览器 */ }
    const i = idxAt(e.clientX, e.clientY)
    setHover(i)
    setDrag({ a: i, b: i })
  }
  const onUp = (e?: RPE<SVGSVGElement>) => {
    try { if (e) e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
    if (drag && Math.abs(drag.b - drag.a) >= 2) setZoom([Math.min(drag.a, drag.b), Math.max(drag.a, drag.b)])
    setDrag(null)
  }
  const spanMs = timestamps[n - 1] - timestamps[0]
  const tlabel = (t: number) => (spanMs < 3 * 86_400_000 ? fmtTime(t).slice(5, 16) : fmtTime(t).slice(0, 10))
  const label = (i: number) => (xLabels ? xLabels[i] ?? '' : tlabel(timestamps[i]))

  const body = (
    <div className="chart2">
      <div className="legend">
        {geo.paths.map((p) => (
          <span key={p.name}>
            <i style={{ background: p.color, opacity: p.dashed ? 0.7 : 1 }} />
            {p.name}
          </span>
        ))}
        {zoom && <button type="button" className="chart-reset" onClick={() => setZoom(null)}>还原区间</button>}
        {!zoom && <small className="chart-drag-hint">拖动选择区间</small>}
      </div>
      <div className="chart2-frame" style={{ aspectRatio: `${W} / ${H}` }}>
        <svg ref={ref} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={ariaLabel} onPointerMove={onMove} onPointerDown={onDown}
          onPointerUp={onUp} onPointerCancel={onUp} onPointerLeave={() => { if (!drag) setHover(null) }} onDoubleClick={() => setZoom(null)}
          style={{ touchAction: 'none', cursor: 'crosshair' }}>
          {geo.ticks.map((t) => (
            <g key={t}>
              <line className={t === 0 && format === 'pct' ? 'chart-zero' : 'chart-gridline'} x1={P.l} x2={W - P.r} y1={geo.y(t)} y2={geo.y(t)} />
              <text className="chart-label" x={P.l - 10} y={geo.y(t) + 4} textAnchor="end">{fmt(t, format)}</text>
            </g>
          ))}
          {geo.paths.map((p) => p.areaD && <path key={p.name + 'a'} d={p.areaD} style={{ fill: p.color }} opacity={0.07} />)}
          {geo.paths.map((p) => (
            <path key={p.name} d={p.d} fill="none" style={{ stroke: p.color }} strokeWidth={p.width ?? (p.dashed ? 1.4 : 1.8)}
              strokeDasharray={p.dashed ? '5 4' : undefined} strokeLinejoin="round" strokeLinecap="round" />
          ))}
          {geo.xt.map((i, k) => (
            <text key={k} className="chart-label" x={geo.x(i)} y={H - 10} textAnchor={k === 0 ? 'start' : k === geo.xt.length - 1 ? 'end' : 'middle'}>
              {label(i)}
            </text>
          ))}
          {drag && Math.abs(drag.b - drag.a) > 0 && (
            <rect className="chart-brush" x={geo.x(Math.min(drag.a, drag.b))} y={P.t}
              width={Math.max(1, Math.abs(geo.x(drag.b) - geo.x(drag.a)))} height={H - P.t - P.b} />
          )}
          {hover !== null && (
            <g>
              <line x1={geo.x(hover)} x2={geo.x(hover)} y1={P.t} y2={H - P.b} style={{ stroke: 'var(--faint)' }} strokeDasharray="3 3" />
              {geo.paths.map((p) => Number.isFinite(p.values[hover]) && (
                <circle key={p.name} cx={geo.x(hover)} cy={geo.y(p.values[hover])} r={3.5} style={{ fill: 'var(--sheet)', stroke: p.color }} strokeWidth={2} />
              ))}
            </g>
          )}
        </svg>
        {hover !== null && (
          <div className="chart-tip" style={{ left: `${(geo.x(hover) / W) * 100}%` }}>
            <b>{xLabels ? xLabels[hover] : `${fmtTime(timestamps[hover])} UTC`}</b>
            {geo.paths.map((p) => (
              <span key={p.name}><i style={{ background: p.color }} />{p.name}<em>{fmt(p.values[hover], format)}</em></span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
  if (full) return <ChartFullscreen title={ariaLabel} onClose={() => setFull(false)} footer={footer}>{() => body}</ChartFullscreen>
  return <div className="chart-expand-host">{body}<ChartExpandButton onClick={() => setFull(true)} label="全屏查看曲线（默认横屏）" /></div>
}

/** 正负柱状图（日收益等）。 */
export function BarChart({
  values,
  labels,
  format = 'pct',
  height = 200,
  ariaLabel = '柱状图',
}: {
  values: number[]
  labels: string[]
  format?: Fmt
  height?: number
  ariaLabel?: string
}) {
  const W = 900
  const H = height
  const P = { l: 64, r: 18, t: 12, b: 28 }
  const [hover, setHover] = useState<number | null>(null)
  const [full, setFull] = useState(false)
  if (!values.length) return <div className="empty-state small"><h3>暂无数据</h3></div>
  const max = Math.max(...values.map(Math.abs), 1e-9)
  const mid = P.t + (H - P.t - P.b) / 2
  const scale = (H - P.t - P.b) / 2 / max
  const bw = (W - P.l - P.r) / values.length
  const body = (
    <div className="chart2">
      <div className="chart2-frame" style={{ aspectRatio: `${W} / ${H}` }}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={ariaLabel} onMouseLeave={() => setHover(null)}>
          {[max, max / 2, 0, -max / 2, -max].map((t) => (
            <g key={t}>
              <line className={t === 0 ? 'chart-zero' : 'chart-gridline'} x1={P.l} x2={W - P.r} y1={mid - t * scale} y2={mid - t * scale} />
              <text className="chart-label" x={P.l - 10} y={mid - t * scale + 4} textAnchor="end">{fmt(t, format)}</text>
            </g>
          ))}
          {values.map((v, i) => (
            <rect key={i} x={P.l + i * bw + bw * 0.15} width={Math.max(1, bw * 0.7)} y={v >= 0 ? mid - v * scale : mid}
              height={Math.max(0.5, Math.abs(v) * scale)} style={{ fill: v >= 0 ? COLORS.up : COLORS.down }}
              opacity={hover === null || hover === i ? 0.85 : 0.4} onMouseEnter={() => setHover(i)} />
          ))}
          {[0, Math.floor(values.length / 2), values.length - 1].map((i, k) => (
            <text key={k} className="chart-label" x={P.l + i * bw + bw / 2} y={H - 8} textAnchor={k === 0 ? 'start' : k === 2 ? 'end' : 'middle'}>
              {labels[i]}
            </text>
          ))}
        </svg>
        {hover !== null && (
          <div className="chart-tip" style={{ left: `${((P.l + hover * bw + bw / 2) / W) * 100}%` }}>
            <b>{labels[hover]}</b>
            <span><em>{fmt(values[hover], format)}</em></span>
          </div>
        )}
      </div>
    </div>
  )
  if (full) return <ChartFullscreen title={ariaLabel} onClose={() => setFull(false)}>{() => body}</ChartFullscreen>
  return <div className="chart-expand-host">{body}<ChartExpandButton onClick={() => setFull(true)} label="全屏查看曲线（默认横屏）" /></div>
}

/** 二维热力图（参数优化：行 × 列）。 */
export function Heatmap({
  rows,
  cols,
  values,
  rowLabel,
  colLabel,
  format = 'num',
  best,
  range,
}: {
  range?: [number, number]
  rows: (number | string)[]
  cols: (number | string)[]
  values: (number | null)[][]
  rowLabel: string
  colLabel: string
  format?: Fmt
  best?: [number, number] | null
}) {
  const [full, setFull] = useState(false)
  const flat = values.flat().filter((v): v is number => v !== null && Number.isFinite(v))
  const lo = range ? range[0] : Math.min(...flat, 0)
  const hi = range ? range[1] : Math.max(...flat, 0)
  const color = (v: number | null) => {
    if (v === null || !Number.isFinite(v)) return 'var(--sunken)'
    const a = v >= 0 ? (hi ? v / hi : 0) : (lo ? v / lo : 0)
    const pct = Math.round(12 + a * 58)
    return `color-mix(in srgb, ${v >= 0 ? 'var(--up)' : 'var(--down)'} ${pct}%, var(--sheet))`
  }
  const body = (
    <div className="heatmap">
      {(rowLabel || colLabel) && <div className="heatmap-axis">{rowLabel} ↓　{colLabel} →</div>}
      <table>
        <thead>
          <tr>
            <th />
            {cols.map((c) => <th key={String(c)}>{String(c)}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={String(r)}>
              <th>{String(r)}</th>
              {cols.map((c, j) => (
                <td key={String(c)} style={{ background: color(values[i][j]) }} className={best && best[0] === i && best[1] === j ? 'best' : undefined}>
                  {values[i][j] === null ? '—' : fmt(values[i][j] as number, format)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
  if (full) return <ChartFullscreen title={`${rowLabel || '参数'} × ${colLabel || '指标'} 热力图`} onClose={() => setFull(false)}>{() => body}</ChartFullscreen>
  return <div className="chart-expand-host">{body}<ChartExpandButton onClick={() => setFull(true)} label="全屏查看热力图（默认横屏）" /></div>
}

/** 散点图：样本内 vs 样本外。 */
export function Scatter({
  points,
  xLabel,
  yLabel,
  highlight,
}: {
  points: { x: number; y: number; label: string }[]
  xLabel: string
  yLabel: string
  highlight?: number
}) {
  const W = 420
  const H = 300
  const P = { l: 54, r: 14, t: 14, b: 40 }
  const [hover, setHover] = useState<number | null>(null)
  if (points.length < 2) return <div className="empty-state small"><h3>点数不足</h3></div>
  const xs = points.map((p) => p.x)
  const ys = points.map((p) => p.y)
  const pad = (a: number[]) => { const lo = Math.min(...a, 0); const hi = Math.max(...a, 0); const d = (hi - lo) * 0.1 || 0.1; return [lo - d, hi + d] }
  const [x0, x1] = pad(xs)
  const [y0, y1] = pad(ys)
  const X = (v: number) => P.l + ((v - x0) / (x1 - x0)) * (W - P.l - P.r)
  const Y = (v: number) => P.t + (1 - (v - y0) / (y1 - y0)) * (H - P.t - P.b)
  return (
    <div className="chart2">
      <div className="chart2-frame" style={{ aspectRatio: `${W} / ${H}` }}>
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${xLabel} 与 ${yLabel} 散点图`}>
          <line className="chart-zero" x1={X(0)} x2={X(0)} y1={P.t} y2={H - P.b} />
          <line className="chart-zero" x1={P.l} x2={W - P.r} y1={Y(0)} y2={Y(0)} />
          {niceTicks(x0, x1, 4).filter((t) => t >= x0 && t <= x1).map((t) => (
            <text key={'x' + t} className="chart-label" x={X(t)} y={H - P.b + 14} textAnchor="middle">{fmtNum(t, 2)}</text>
          ))}
          {niceTicks(y0, y1, 4).filter((t) => t >= y0 && t <= y1).map((t) => (
            <text key={'y' + t} className="chart-label" x={P.l - 8} y={Y(t) + 4} textAnchor="end">{fmtNum(t, 2)}</text>
          ))}
          <text className="chart-label" x={(W + P.l) / 2} y={H - 4} textAnchor="middle">{xLabel}</text>
          <text className="chart-label" x={12} y={(H - P.b) / 2} textAnchor="middle" transform={`rotate(-90 12 ${(H - P.b) / 2})`}>{yLabel}</text>
          {points.map((p, i) => (
            <circle key={i} cx={X(p.x)} cy={Y(p.y)} r={i === highlight ? 6.5 : 4}
              style={{ fill: i === highlight ? COLORS.amber : COLORS.strategy, stroke: i === highlight ? 'var(--sheet)' : 'none' }}
              opacity={i === highlight ? 1 : 0.7} strokeWidth={2}
              onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} />
          ))}
        </svg>
        {hover !== null && (
          <div className="chart-tip" style={{ left: `${(X(points[hover].x) / W) * 100}%` }}>
            <b>{points[hover].label}</b>
            <span>{xLabel}<em>{fmtNum(points[hover].x, 3)}</em></span>
            <span>{yLabel}<em>{fmtNum(points[hover].y, 3)}</em></span>
          </div>
        )}
      </div>
    </div>
  )
}

/** 迷你曲线（卡片、列表用）。颜色按整段首尾比较；调用方保证区间与旁边的涨跌幅口径一致。 */
export function Sparkline({ values, width = 120, height = 34, benchmark }: { values: number[]; width?: number; height?: number; benchmark?: number[] }) {
  if (!values || values.length < 2) return <span className="muted">—</span>
  const all = [...values, ...(benchmark ?? [])]
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const path = (vs: number[]) =>
    vs.map((v, i) => `${i === 0 ? 'M' : 'L'}${((i / (vs.length - 1)) * width).toFixed(1)},${(height - 2 - ((v - lo) / (hi - lo || 1)) * (height - 4)).toFixed(1)}`).join(' ')
  const up = values[values.length - 1] >= values[0]
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="sparkline" aria-hidden="true">
      {benchmark && benchmark.length > 1 && <path d={path(benchmark)} fill="none" style={{ stroke: COLORS.benchmark }} strokeWidth={1} strokeDasharray="3 2" />}
      <path d={path(values)} fill="none" style={{ stroke: up ? COLORS.up : COLORS.down }} strokeWidth={1.6} />
    </svg>
  )
}

export function seriesColor(i: number) {
  return EXTRA[i % EXTRA.length]
}
