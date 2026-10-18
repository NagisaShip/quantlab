import { Fragment, useEffect, useMemo, useRef, useState, type PointerEvent as RPE } from 'react'
import { ChartExpandButton, ChartFullscreen } from '../ChartFullscreen'
import { DataTable } from './DataTable'
import { downloadBlob, fmtValue, looksLikeTs, toCsv, type Fmt } from './protocol'

/**
 * 原生交互式图表（不是图片）：折线、面积、柱状、散点、饼图、热力图。
 * - 悬浮查看数值（十字线 + 提示框）；点图例开关序列；
 * - 在绘图区拖动框选放大，双击还原；时间轴下方有「全部 / 近一半 / 近 1/4」范围切换；
 * - 「数据」查看底层数据表，「导出」下载 CSV —— 图表始终保留数据。
 */

export interface ChartSeries { name: string; data: (number | null)[] | [number, number][]; color?: string; kind?: 'line' | 'area' | 'bar' | 'scatter'; dashed?: boolean }
export interface ChartMarker { x: number | string; label?: string; tone?: 'up' | 'down' | 'info'; y?: number }
export interface ChartSpec {
  kind?: 'line' | 'area' | 'bar' | 'scatter' | 'pie' | 'heatmap'
  title?: string
  x?: (number | string)[]
  series?: ChartSeries[]
  y_format?: Fmt | string
  x_format?: Fmt | string
  y_label?: string
  x_label?: string
  stacked?: boolean
  markers?: ChartMarker[]
  baseline?: number
  height?: number
  // 饼图
  labels?: string[]
  values?: number[]
  // 热力图
  y?: (number | string)[]
  z?: (number | null)[][]
  /** 热力图最佳格点：画高亮边框，并在提示里标「最佳」。 */
  best?: { x?: number | string; y?: number | string }
  color_label?: string
}

export const PALETTE = ['var(--accent)', 'var(--amber)', '#0e9a9a', 'var(--violet)', 'var(--bench)', '#d0607f', '#6f7fa0', '#a3864f']

function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null)
  const [w, setW] = useState(640)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver((es) => setW(Math.max(260, Math.round(es[0].contentRect.width))))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

function niceTicks(lo: number, hi: number, n = 5): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return []
  if (lo === hi) { lo -= 1; hi += 1 }
  const span = hi - lo
  const step0 = span / n
  const mag = Math.pow(10, Math.floor(Math.log10(step0)))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag
  const out: number[] = []
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)))
  return out
}

export function Chart({ spec, height: hProp }: { spec: ChartSpec; height?: number }) {
  const [full, setFull] = useState(false)
  const kind = spec.kind || 'line'
  const height = hProp ?? spec.height ?? 260
  const body = (h: number, expand?: () => void) => {
    if (kind === 'pie') return <PieChart spec={spec} />
    if (kind === 'heatmap') return <HeatmapChart spec={spec} />
    return <XYChart spec={spec} height={h} onExpand={expand} />
  }
  if (full) {
    return <ChartFullscreen title={spec.title} onClose={() => setFull(false)}>
      {({ height: h }) => <div className="chart-fs-chart">{body(Math.max(260, h - 12))}</div>}
    </ChartFullscreen>
  }
  return (
    <div className="chart-expand-host">
      {body(height, () => setFull(true))}
      {kind !== 'line' && kind !== 'area' && kind !== 'bar' && kind !== 'scatter' && <ChartExpandButton onClick={() => setFull(true)} />}
    </div>
  )
}

// ---------------------------------------------------------------- XY

function XYChart({ spec, height = 260, onExpand }: { spec: ChartSpec; height?: number; onExpand?: () => void }) {
  const kind = spec.kind || 'line'
  const [ref, width] = useWidth<HTMLDivElement>()
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const [hover, setHover] = useState<number | null>(null)
  const [drag, setDrag] = useState<{ a: number; b: number } | null>(null)
  const [showData, setShowData] = useState(false)

  // 统一成按索引对齐的序列；散点用 (x, y) 对
  const norm = useMemo(() => {
    const series = (spec.series || []).filter((s) => s && Array.isArray(s.data))
    const pairs = kind === 'scatter' || series.some((s) => Array.isArray(s.data[0]))
    if (pairs) {
      const xs = series.flatMap((s) => (s.data as [number, number][]).map((p) => Number(p[0])))
      return { pairs: true, x: xs, series }
    }
    const n = Math.max(spec.x?.length || 0, ...series.map((s) => s.data.length))
    const x = spec.x && spec.x.length ? spec.x : Array.from({ length: n }, (_, i) => i + 1)
    return { pairs: false, x, series }
  }, [spec, kind])

  const n = norm.x.length
  const [range, setRange] = useState<[number, number]>([0, Math.max(0, n - 1)])
  useEffect(() => setRange([0, Math.max(0, n - 1)]), [n])
  const [i0, i1] = norm.pairs ? [0, n - 1] : range
  const timeAxis = !norm.pairs && norm.x.length > 0 && norm.x.every((v) => looksLikeTs(v))
  const catAxis = !norm.pairs && norm.x.some((v) => typeof v === 'string')

  const pad = { l: 56, r: 14, t: 10, b: 26 }
  const W = width
  const H = height
  const iw = W - pad.l - pad.r
  const ih = H - pad.t - pad.b
  const active = norm.series.filter((s) => !hidden.has(s.name))

  const { lo, hi } = useMemo(() => {
    let lo = Infinity, hi = -Infinity
    const stack = spec.stacked && (kind === 'bar' || kind === 'area')
    if (norm.pairs) {
      for (const s of active) for (const p of s.data as [number, number][]) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]) }
    } else {
      for (let i = i0; i <= i1; i++) {
        let acc = 0
        for (const s of active) {
          const v = (s.data as (number | null)[])[i]
          if (v === null || v === undefined || !Number.isFinite(v)) continue
          if (stack) { acc += v; lo = Math.min(lo, acc); hi = Math.max(hi, acc) } else { lo = Math.min(lo, v); hi = Math.max(hi, v) }
        }
      }
    }
    // 柱状图必须从 0 起；面积图只有数值接近 0 时才从 0 起（价格这类远离 0 的序列从 0 起会被压成一条线）
    if (kind === 'bar' || (kind === 'area' && (lo <= 0 || lo / (hi || 1) < 0.4))) { lo = Math.min(lo, 0); hi = Math.max(hi, 0) }
    if (spec.baseline !== undefined) { lo = Math.min(lo, spec.baseline); hi = Math.max(hi, spec.baseline) }
    if (!Number.isFinite(lo)) { lo = 0; hi = 1 }
    const padY = (hi - lo) * 0.06 || Math.abs(hi) * 0.1 || 1
    return { lo: kind === 'bar' && lo === 0 ? 0 : lo - padY, hi: hi + padY }
  }, [active, i0, i1, kind, norm.pairs, spec.baseline, spec.stacked])

  const xLo = norm.pairs ? Math.min(...norm.x.map(Number)) : i0
  const xHi = norm.pairs ? Math.max(...norm.x.map(Number)) : i1
  const sx = (v: number) => pad.l + (xHi === xLo ? iw / 2 : ((v - xLo) / (xHi - xLo)) * iw)
  const sy = (v: number) => pad.t + ih - ((v - lo) / (hi - lo || 1)) * ih
  const yt = niceTicks(lo, hi, Math.max(3, Math.round(ih / 50)))
  const pctFmt = String(spec.y_format || '').startsWith('pct')
  const fy = (v: number) => fmtValue(v, spec.y_format, pctFmt ? (Math.abs(hi - lo) < 0.02 ? 2 : 1) : Math.abs(hi - lo) < 0.05 ? 4 : undefined)
  const spanMs = timeAxis ? Math.abs(Number(norm.x[n - 1]) - Number(norm.x[0])) * (Number(norm.x[0]) < 1e11 ? 1000 : 1) : 0
  const fx = (v: number | string) => (typeof v === 'string' ? v : timeAxis ? fmtValue(v, spec.x_format || (spanMs > 86_400_000 * 3 ? 'date' : 'datetime')) : fmtValue(v, spec.x_format))

  const xTickIdx = useMemo(() => {
    if (norm.pairs) return niceTicks(xLo, xHi, Math.max(3, Math.round(iw / 110)))
    // 类别轴（分组、T+n）类别不多时每个都标出来
    if (catAxis && i1 - i0 + 1 <= Math.max(2, Math.floor(iw / 44))) return Array.from({ length: i1 - i0 + 1 }, (_, k) => i0 + k)
    const count = Math.max(2, Math.min(i1 - i0 + 1, Math.round(iw / 110)))
    const out: number[] = []
    for (let k = 0; k < count; k++) out.push(Math.round(i0 + ((i1 - i0) * k) / Math.max(1, count - 1)))
    return [...new Set(out)]
  }, [norm.pairs, xLo, xHi, iw, i0, i1, catAxis])

  const idxAt = (svg: SVGSVGElement, clientX: number, clientY: number) => {
    const ctm = svg.getScreenCTM?.()
    let x: number
    if (ctm) {
      const pt = svg.createSVGPoint()
      pt.x = clientX
      pt.y = clientY
      x = pt.matrixTransform(ctm.inverse()).x
    } else {
      x = clientX - svg.getBoundingClientRect().left
    }
    const t = (x - pad.l) / iw
    return Math.max(i0, Math.min(i1, Math.round(i0 + t * (i1 - i0))))
  }
  const onMove = (e: RPE<SVGSVGElement>) => {
    if (norm.pairs) return
    const i = idxAt(e.currentTarget, e.clientX, e.clientY)
    setHover(i)
    if (drag) setDrag({ ...drag, b: i })
  }
  const onDown = (e: RPE<SVGSVGElement>) => {
    if (norm.pairs || n < 6 || (e.pointerType === 'mouse' && e.button !== 0)) return
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* 旧浏览器 */ }
    const i = idxAt(e.currentTarget, e.clientX, e.clientY)
    setDrag({ a: i, b: i })
  }
  const onUp = (e?: RPE<SVGSVGElement>) => {
    try { if (e) e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* 已释放 */ }
    if (drag && Math.abs(drag.b - drag.a) >= 2) setRange([Math.min(drag.a, drag.b), Math.max(drag.a, drag.b)])
    setDrag(null)
  }
  const zoomed = !norm.pairs && (i0 > 0 || i1 < n - 1)

  const barGroupW = !norm.pairs ? iw / Math.max(1, i1 - i0 + 1) : 0
  const barKinds = active.filter((s) => (s.kind || kind) === 'bar')
  const barW = Math.max(1, Math.min(28, (barGroupW * 0.78) / Math.max(1, spec.stacked ? 1 : barKinds.length)))
  const colorOf = (s: ChartSeries, i: number) => s.color || PALETTE[(norm.series.indexOf(s) >= 0 ? norm.series.indexOf(s) : i) % PALETTE.length]
  const cx = (i: number) => (kind === 'bar' || barKinds.length ? pad.l + (i - i0 + 0.5) * barGroupW : sx(i))

  const paths = active.map((s, si) => {
    const sk = s.kind || kind
    const color = colorOf(s, si)
    if (norm.pairs) {
      return (
        <g key={s.name} fill={color}>
          {(s.data as [number, number][]).map((p, j) => (
            <circle key={j} cx={sx(Number(p[0]))} cy={sy(Number(p[1]))} r={3} opacity={0.75}>
              <title>{`${s.name}: (${fmtValue(p[0], spec.x_format)}, ${fy(Number(p[1]))})`}</title>
            </circle>
          ))}
        </g>
      )
    }
    const data = s.data as (number | null)[]
    if (sk === 'bar') {
      const bi = barKinds.indexOf(s)
      return (
        <g key={s.name} fill={color}>
          {data.slice(i0, i1 + 1).map((v, k) => {
            if (v === null || v === undefined || !Number.isFinite(v)) return null
            const i = i0 + k
            const off = spec.stacked ? -barW / 2 : (bi - (barKinds.length - 1) / 2) * barW - barW / 2
            let base = 0
            if (spec.stacked) for (const o of barKinds.slice(0, bi)) base += Number((o.data as number[])[i]) || 0
            const y0 = sy(base), y1 = sy(base + v)
            return <rect key={i} className={`qb-bar${v < 0 ? ' neg' : ''}`} style={{ animationDelay: `${Math.min(k, 40) * 12}ms` }} x={cx(i) + off} y={Math.min(y0, y1)} width={barW} height={Math.max(1, Math.abs(y1 - y0))} rx={1.5} opacity={hover === null || hover === i ? 0.92 : 0.55} />
          })}
        </g>
      )
    }
    let d = ''
    let started = false
    for (let i = i0; i <= i1; i++) {
      const v = data[i]
      if (v === null || v === undefined || !Number.isFinite(v)) { started = false; continue }
      d += `${started ? 'L' : 'M'}${cx(i).toFixed(1)},${sy(v).toFixed(1)}`
      started = true
    }
    return (
      <g key={s.name}>
        {sk === 'area' && d && <path d={`${d}L${cx(i1).toFixed(1)},${sy(lo < 0 ? 0 : lo).toFixed(1)}L${cx(i0).toFixed(1)},${sy(lo < 0 ? 0 : lo).toFixed(1)}Z`} fill={color} opacity={0.14} />}
        <path d={d} fill="none" stroke={color} strokeWidth={1.6} strokeDasharray={s.dashed ? '4 3' : undefined} vectorEffect="non-scaling-stroke"
          className={s.dashed ? 'qb-fade-line' : 'qb-draw'} pathLength={s.dashed ? undefined : 1} key={`${i0}-${i1}`} />
      </g>
    )
  })

  const markers = (spec.markers || []).map((m, k) => {
    const idx = typeof m.x === 'number' && !catAxis ? nearestIndex(norm.x as number[], m.x) : norm.x.indexOf(m.x)
    if (idx < i0 || idx > i1 || idx < 0) return null
    const ref = active[0] ? (active[0].data as (number | null)[])[idx] : null
    const y = m.y ?? (ref ?? (lo + hi) / 2)
    const tone = m.tone === 'up' ? 'var(--up)' : m.tone === 'down' ? 'var(--down)' : 'var(--amber)'
    const up = m.tone !== 'down'
    const px = cx(idx), py = sy(Number(y))
    return (
      <g key={k} className="qb-marker">
        <path d={up ? `M${px},${py + 4}l-4,7h8z` : `M${px},${py - 4}l-4,-7h8z`} fill={tone} />
        <title>{`${m.label || ''} ${fx(norm.x[idx])}`}</title>
      </g>
    )
  })

  const tip = hover !== null && !norm.pairs ? (
    <div className="qb-tip" style={{ left: Math.min(W - 180, Math.max(0, cx(hover) + 12)), top: 8 }}>
      <b>{fx(norm.x[hover])}</b>
      {active.map((s, si) => {
        const v = (s.data as (number | null)[])[hover]
        return <div key={s.name}><i style={{ background: colorOf(s, si) }} />{s.name}<span>{v === null || v === undefined ? '—' : fy(v)}</span></div>
      })}
    </div>
  ) : null

  const tableCols = [{ key: '__x', label: spec.x_label || 'x' }, ...norm.series.map((s) => ({ key: s.name, label: s.name }))]
  const tableRows = useMemo(() => {
    if (norm.pairs) return norm.series.flatMap((s) => (s.data as [number, number][]).map((p) => ({ __x: p[0], [s.name]: p[1] })))
    return norm.x.map((x, i) => {
      const r: Record<string, unknown> = { __x: timeAxis ? fx(x) : x }
      for (const s of norm.series) r[s.name] = (s.data as (number | null)[])[i]
      return r
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [norm, timeAxis])

  const exportCsv = () => downloadBlob(`${(spec.title || 'chart').replace(/\s+/g, '_')}.csv`, '﻿' + toCsv(tableCols.map((c) => c.label), tableRows.map((r) => tableCols.map((c) => r[c.key]))), 'text/csv;charset=utf-8')

  return (
    <div className="qb-chart" ref={ref}>
      <div className="qb-chart-tools">
        <div className="qb-legend">
          {norm.series.map((s, si) => (
            <button type="button" key={s.name} className={hidden.has(s.name) ? 'off' : ''} onClick={() => setHidden((h) => { const x = new Set(h); if (x.has(s.name)) x.delete(s.name); else x.add(s.name); return x })}>
              <i style={{ background: colorOf(s, si) }} />{s.name}
            </button>
          ))}
        </div>
        <span className="qb-grow" />
        {!norm.pairs && n > 20 && (
          <span className="qb-seg" role="group" aria-label="时间范围">
            {([['全部', 1], ['近 1/2', 0.5], ['近 1/4', 0.25]] as [string, number][]).map(([l, f]) => {
              const a = f === 1 ? 0 : Math.floor((n - 1) * (1 - f))
              const on = i0 === a && i1 === n - 1
              return <button type="button" key={l} className={on ? 'on' : ''} onClick={() => setRange([a, n - 1])}>{l}</button>
            })}
          </span>
        )}
        {zoomed && <button type="button" className="qb-icon-btn" onClick={() => setRange([0, n - 1])}>还原</button>}
        <button type="button" className={`qb-icon-btn${showData ? ' on' : ''}`} onClick={() => setShowData(!showData)}>数据</button>
        <button type="button" className="qb-icon-btn" onClick={exportCsv}>导出</button>
        {onExpand && <ChartExpandButton onClick={onExpand} inline label="全屏查看图表（默认横屏）" />}
      </div>
      <div className="qb-chart-plot">
        <svg width={W} height={H} role="img" aria-label={spec.title || '图表'} onPointerMove={onMove} onPointerDown={onDown} onPointerUp={onUp} onPointerCancel={onUp}
          onPointerLeave={() => { if (!drag) setHover(null) }} onDoubleClick={() => setRange([0, n - 1])}
          style={{ cursor: !norm.pairs && n >= 6 ? 'crosshair' : undefined, touchAction: !norm.pairs && n >= 6 ? 'none' : undefined }}>
          {yt.map((t) => (
            <g key={t}>
              <line x1={pad.l} x2={W - pad.r} y1={sy(t)} y2={sy(t)} className="qb-grid" />
              <text x={pad.l - 6} y={sy(t) + 3.5} textAnchor="end" className="qb-axis">{fy(t)}</text>
            </g>
          ))}
          {(spec.baseline !== undefined || (lo < 0 && hi > 0)) && <line x1={pad.l} x2={W - pad.r} y1={sy(spec.baseline ?? 0)} y2={sy(spec.baseline ?? 0)} className="qb-zero" />}
          {xTickIdx.map((t, k) => (
            <text key={k} x={norm.pairs ? sx(t) : cx(t)} y={H - 8} textAnchor={catAxis ? 'middle' : k === 0 ? 'start' : k === xTickIdx.length - 1 ? 'end' : 'middle'} className="qb-axis">
              {norm.pairs ? fmtValue(t, spec.x_format) : fx(norm.x[t])}
            </text>
          ))}
          {paths}
          {markers}
          {drag && Math.abs(drag.b - drag.a) > 0 && (
            <rect x={Math.min(cx(drag.a), cx(drag.b))} y={pad.t} width={Math.abs(cx(drag.b) - cx(drag.a))} height={ih} className="qb-brush" />
          )}
          {hover !== null && !norm.pairs && (
            <g>
              <line x1={cx(hover)} x2={cx(hover)} y1={pad.t} y2={pad.t + ih} className="qb-cross" />
              {active.filter((s) => (s.kind || kind) !== 'bar').map((s, si) => {
                const v = (s.data as (number | null)[])[hover]
                return v === null || v === undefined || !Number.isFinite(v) ? null : <circle key={s.name} cx={cx(hover)} cy={sy(v)} r={3.2} fill={colorOf(s, si)} stroke="var(--sheet)" strokeWidth={1.5} />
              })}
            </g>
          )}
        </svg>
        {tip}
      </div>
      {!norm.pairs && n >= 6 && <div className="qb-hint">拖动框选放大 · 双击还原{spec.y_label ? ` · 纵轴：${spec.y_label}` : ''}</div>}
      {showData && <div className="anim-expand"><DataTable columns={tableCols} rows={tableRows} maxHeight={260} compact /></div>}
    </div>
  )
}

function nearestIndex(xs: number[], v: number): number {
  const target = v < 1e11 && Number(xs[0]) > 1e11 ? v * 1000 : v
  let best = -1, bd = Infinity
  for (let i = 0; i < xs.length; i++) {
    const d = Math.abs(Number(xs[i]) - target)
    if (d < bd) { bd = d; best = i }
  }
  return best
}

// ---------------------------------------------------------------- Pie

function PieChart({ spec }: { spec: ChartSpec }) {
  const labels = spec.labels || (spec.x as string[] | undefined)?.map(String) || []
  const values = spec.values || ((spec.series?.[0]?.data as number[]) ?? [])
  const [hidden, setHidden] = useState<Set<number>>(new Set())
  const [hover, setHover] = useState<number | null>(null)
  const total = values.reduce((a, v, i) => a + (hidden.has(i) ? 0 : Math.max(0, Number(v) || 0)), 0) || 1
  const R = 86, r0 = 48, C = 100
  let a0 = -Math.PI / 2
  const arcs = values.map((v, i) => {
    if (hidden.has(i)) return null
    const frac = Math.max(0, Number(v) || 0) / total
    const a1 = a0 + frac * Math.PI * 2
    const large = a1 - a0 > Math.PI ? 1 : 0
    const out = hover === i ? 5 : 0
    const mid = (a0 + a1) / 2
    const ox = Math.cos(mid) * out, oy = Math.sin(mid) * out
    const p = (a: number, rr: number) => `${(C + ox + Math.cos(a) * rr).toFixed(2)},${(C + oy + Math.sin(a) * rr).toFixed(2)}`
    const d = frac >= 0.9999
      ? `M${C - R},${C}a${R},${R} 0 1,0 ${2 * R},0a${R},${R} 0 1,0 ${-2 * R},0M${C - r0},${C}a${r0},${r0} 0 1,1 ${2 * r0},0a${r0},${r0} 0 1,1 ${-2 * r0},0`
      : `M${p(a0, R)}A${R},${R} 0 ${large} 1 ${p(a1, R)}L${p(a1, r0)}A${r0},${r0} 0 ${large} 0 ${p(a0, r0)}Z`
    a0 = a1
    return <path key={i} d={d} fill={PALETTE[i % PALETTE.length]} fillRule="evenodd" onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} opacity={hover === null || hover === i ? 1 : 0.6} />
  })
  return (
    <div className="qb-chart qb-pie">
      <svg viewBox="0 0 200 200" width={200} height={200} role="img" aria-label={spec.title || '饼图'}>
        {arcs}
        <text x={C} y={C - 2} textAnchor="middle" className="qb-pie-big">{hover !== null ? fmtValue((Number(values[hover]) || 0) / total, 'pct', 1) : fmtValue(total, spec.y_format)}</text>
        <text x={C} y={C + 14} textAnchor="middle" className="qb-axis">{hover !== null ? labels[hover] : '合计'}</text>
      </svg>
      <div className="qb-legend col">
        {labels.map((l, i) => (
          <button type="button" key={i} className={hidden.has(i) ? 'off' : ''} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}
            onClick={() => setHidden((h) => { const x = new Set(h); if (x.has(i)) x.delete(i); else x.add(i); return x })}>
            <i style={{ background: PALETTE[i % PALETTE.length] }} />{l}<span>{fmtValue(values[i], spec.y_format)}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- Heatmap

export function HeatmapChart({ spec }: { spec: ChartSpec }) {
  const xs = (spec.x || []).map(String)
  const ys = (spec.y || xs).map(String)
  const z = spec.z || []
  const [hover, setHover] = useState<[number, number] | null>(null)
  const [ref, width] = useWidth<HTMLDivElement>()
  const flat = z.flat().filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  const lo = Math.min(...flat, 0), hi = Math.max(...flat, 0)
  const diverging = lo < 0 && hi > 0
  const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1
  const color = (v: number | null) => {
    if (v === null || v === undefined || !Number.isFinite(v)) return 'var(--sunken)'
    const t = diverging ? v / m : (v - lo) / (hi - lo || 1)
    const a = Math.min(1, Math.abs(t)) * 0.85 + 0.06
    return diverging ? (t >= 0 ? `rgba(46,69,200,${a})` : `rgba(210,59,59,${a})`) : `rgba(46,69,200,${a})`
  }
  const labelW = Math.min(120, Math.max(40, ...ys.map((y) => y.length * 7)))
  const cell = Math.max(14, Math.min(46, (width - labelW - 10) / Math.max(1, xs.length)))
  const showNum = cell >= 34
  const bx = spec.best?.x !== undefined ? xs.indexOf(String(spec.best.x)) : -1
  const by = spec.best?.y !== undefined ? ys.indexOf(String(spec.best.y)) : -1
  return (
    <div className="qb-chart" ref={ref}>
      {(spec.x_label || spec.y_label || spec.color_label) && (
        <div className="qb-heat-labels">
          {spec.y_label && <span>Y · {spec.y_label}</span>}
          {spec.x_label && <span>X · {spec.x_label}</span>}
          {spec.color_label && <span>{spec.color_label}</span>}
        </div>
      )}
      <div className="qb-heat" style={{ gridTemplateColumns: `${labelW}px repeat(${xs.length}, ${cell}px)` }}>
        <span />
        {xs.map((x) => <span key={x} className="qb-heat-x" title={x}>{x}</span>)}
        {ys.map((y, i) => (
          <Fragment key={`r${i}`}>
            <span className="qb-heat-y" title={y}>{y}</span>
            {xs.map((x, j) => {
              const v = z[i]?.[j] ?? null
              return (
                <span key={`${i}-${j}`} className={`qb-heat-cell${hover && (hover[0] === i || hover[1] === j) ? ' hl' : ''}${i === by && j === bx ? ' best' : ''}`}
                  style={{ background: color(v), height: cell, color: v !== null && Math.abs(diverging ? v / m : (v - lo) / (hi - lo || 1)) > 0.55 ? '#fff' : undefined }}
                  onMouseEnter={() => setHover([i, j])} onMouseLeave={() => setHover(null)} onClick={() => setHover([i, j])} title={`${y} × ${x}：${v === null ? '—' : fmtValue(v, spec.y_format, 3)}${i === by && j === bx ? '（最佳）' : ''}`}>
                  {showNum && v !== null ? fmtValue(v, spec.y_format, 2) : ''}
                </span>
              )
            })}
          </Fragment>
        ))}
      </div>
      <div className="qb-hint">
        {hover ? `${ys[hover[0]]} × ${xs[hover[1]]}：${fmtValue(z[hover[0]]?.[hover[1]] ?? null, spec.y_format, 3)}${hover[0] === by && hover[1] === bx ? ' · 最佳' : ''}` : `范围 ${fmtValue(lo, spec.y_format, 2)} ~ ${fmtValue(hi, spec.y_format, 2)}${diverging ? '（蓝正红负）' : ''}`}
      </div>
    </div>
  )
}
