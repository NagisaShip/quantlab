import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'

/** K 线：t 是开盘时间（毫秒）。 */
export interface KBar { t: number; o: number; h: number; l: number; c: number; v?: number }
/** 成交标记：画在成交所在那根 K 线上。 */
export interface KMark { ts: number; side: 'buy' | 'sell' | string; price?: number; amount?: number; text?: string }

/** 从 CSS 变量取色：图表库要具体颜色，主题切换时重新取。 */
function palette() {
  const css = getComputedStyle(document.documentElement)
  const v = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback
  return {
    bg: v('--sheet', '#ffffff'),
    ink: v('--muted', '#6e6a62'),
    line: v('--line-2', '#eeebe3'),
    up: v('--up', '#0f8f62'),
    down: v('--down', '#d23b3b'),
    accent: v('--accent', '#c15f3c'),
  }
}


/**
 * 图表时间轴：lightweight-charts 以「秒」为最小单位。秒级以下的 K 线（100ms、250ms…）同一秒里有好几根，
 * 直接按秒会重叠，所以按「第几个周期」编号（ms / stepMs），刻度与读数再换回真实的毫秒时间。
 */
export function toChartTime(ms: number, stepMs: number): UTCTimestamp {
  return (stepMs < 1000 ? Math.floor(ms / stepMs) : Math.floor(ms / 1000)) as UTCTimestamp
}
export function fromChartTime(t: number, stepMs: number): number {
  return stepMs < 1000 ? t * stepMs : t * 1000
}
function fmtFine(ms: number, withMs: boolean): string {
  const d = new Date(ms)
  const p = (x: number, n = 2) => String(x).padStart(n, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}${withMs ? `.${p(d.getMilliseconds(), 3)}` : ''}`
}

function alpha(color: string, a: number) {
  const m = color.match(/^#([0-9a-f]{6})$/i)
  if (!m) return color
  const n = parseInt(m[1], 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
}

/**
 * 可缩放、可拖动的 K 线图：成交量 + 买卖标记 + 正在形成的那根（forming）。
 * 滚轮缩放、拖动平移、双击回到最新。
 */
export default function KlineChart({
  bars,
  marks = [],
  forming,
  height = 380,
  stepMs,
  ariaLabel = 'K 线图',
  ma = [],
  legend = false,
}: {
  bars: KBar[]
  marks?: KMark[]
  forming?: KBar | null
  height?: number
  /** 一根 K 线的毫秒数：用来把成交时间对到所在 K 线 */
  stepMs: number
  ariaLabel?: string
  /** 叠加的均线周期，如 [20, 60] */
  ma?: number[]
  /** 显示十字光标读数：时间、开高低收、涨跌、振幅、成交量（鼠标不在图上时显示最新一根） */
  legend?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  const tt = (ms: number) => toChartTime(ms, stepMs)
  const chart = useRef<IChartApi | null>(null)
  const candles = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null)
  const fitted = useRef(false)
  const fitKey = useRef('')
  const maSeries = useRef<ISeriesApi<'Line'>[]>([])
  const [hoverT, setHoverT] = useState<number | null>(null)
  const sorted = useMemo(() => {
    const rows = [...bars].sort((a, b) => a.t - b.t)
    const seen = new Set<number>()
    return rows.filter((b) => (seen.has(b.t) ? false : (seen.add(b.t), true)))
  }, [bars])
  const index = useMemo(() => new Map(sorted.map((b, i) => [tt(b.t) as number, i])), [sorted])

  // 建图一次；主题变化时换色
  useEffect(() => {
    if (!box.current) return
    const p = palette()
    const c = createChart(box.current, {
      height,
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: p.bg }, textColor: p.ink, fontSize: 11, fontFamily: 'inherit' },
      grid: { vertLines: { color: p.line }, horzLines: { color: p.line } },
      rightPriceScale: { borderVisible: false },
      timeScale: {
        borderVisible: false, timeVisible: true, secondsVisible: stepMs < 60_000, rightOffset: 4,
        // 秒级 / 毫秒级：刻度显示到秒或毫秒（时间轴编号要换回真实时间）
        ...(stepMs < 60_000 ? { tickMarkFormatter: (t: number) => fmtFine(fromChartTime(t, stepMs), stepMs < 1000) } : {}),
      },
      crosshair: { mode: CrosshairMode.Normal },
      localization: { locale: 'zh-CN', ...(stepMs < 60_000 ? { timeFormatter: (t: number) => fmtFine(fromChartTime(t, stepMs), stepMs < 1000) } : {}) },
    })
    const cs = c.addCandlestickSeries({
      upColor: p.up, downColor: p.down, wickUpColor: p.up, wickDownColor: p.down, borderVisible: false,
    })
    const vs = c.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false })
    c.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
    cs.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.22 } })
    chart.current = c
    candles.current = cs
    volume.current = vs

    const restyle = () => {
      const q = palette()
      c.applyOptions({
        layout: { background: { type: ColorType.Solid, color: q.bg }, textColor: q.ink },
        grid: { vertLines: { color: q.line }, horzLines: { color: q.line } },
      })
      cs.applyOptions({ upColor: q.up, downColor: q.down, wickUpColor: q.up, wickDownColor: q.down })
    }
    const mo = new MutationObserver(restyle)
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'data-updown'] })
    c.subscribeCrosshairMove((param) => setHoverT(param.time !== undefined && param.point ? (param.time as number) : null))
    const dbl = () => c.timeScale().scrollToRealTime()
    box.current.addEventListener('dblclick', dbl)
    const el = box.current
    return () => {
      mo.disconnect()
      el.removeEventListener('dblclick', dbl)
      c.remove()
      maSeries.current = []
      chart.current = null
      fitted.current = false
    }
  }, [height])

  // 已收盘 K 线 + 成交标记
  useEffect(() => {
    const cs = candles.current
    const vs = volume.current
    if (!cs || !vs) return
    const p = palette()
    const uniq = sorted
    cs.setData(uniq.map((b) => ({ time: tt(b.t), open: b.o, high: b.h, low: b.l, close: b.c })))
    vs.setData(uniq.map((b) => ({ time: tt(b.t), value: b.v ?? 0, color: alpha(b.c >= b.o ? p.up : p.down, 0.35) })))
    if (uniq.length) {
      const first = uniq[0].t
      const last = uniq[uniq.length - 1].t
      const ms: SeriesMarker<Time>[] = marks
        .filter((m) => m.ts >= first && m.ts < last + 2 * stepMs)
        .map((m) => {
          const buy = m.side === 'buy'
          const t = Math.min(last, first + Math.floor((m.ts - first) / stepMs) * stepMs)
          return {
            time: tt(t),
            position: buy ? 'belowBar' : 'aboveBar',
            color: buy ? p.up : p.down,
            shape: buy ? 'arrowUp' : 'arrowDown',
            text: m.text ?? (buy ? '买' : '卖'),
          } as SeriesMarker<Time>
        })
        .sort((a, b) => (a.time as number) - (b.time as number))
      cs.setMarkers(ms)
    }
    // 换了周期或区间（第一根变了）就重新铺满：数据不多时整段显示，太多时先看最近 240 根
    const key = uniq.length ? `${uniq[0].t}` : ''
    if (uniq.length && (!fitted.current || fitKey.current !== key)) {
      const ts = chart.current?.timeScale()
      if (uniq.length <= 400) ts?.fitContent()
      else ts?.setVisibleLogicalRange({ from: uniq.length - 240, to: uniq.length + 4 })
      fitted.current = true
      fitKey.current = key
    }
  }, [sorted, marks, stepMs])

  // 均线：周期变了就重建线
  useEffect(() => {
    const c = chart.current
    if (!c) return
    maSeries.current.forEach((x) => c.removeSeries(x))
    maSeries.current = ma.map((n, k) => {
      const line = c.addLineSeries({ color: MA_COLORS[k % MA_COLORS.length], lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false })
      line.setData(movingAverage(sorted, n).map(([t, v]) => ({ time: tt(t), value: v })))
      return line
    })
  }, [sorted, ma.join(',')]) // eslint-disable-line react-hooks/exhaustive-deps

  // 正在形成的那根：只更新最后一根，不重画
  useEffect(() => {
    const cs = candles.current
    if (!cs || !forming || !bars.length) return
    const last = bars.reduce((m, b) => Math.max(m, b.t), 0)
    if (forming.t < last) return
    try {
      cs.update({ time: tt(forming.t), open: forming.o, high: forming.h, low: forming.l, close: forming.c })
      const p = palette()
      volume.current?.update({ time: tt(forming.t), value: forming.v ?? 0, color: alpha(forming.c >= forming.o ? p.up : p.down, 0.2) })
    } catch {
      /* 时间早于最后一根：忽略 */
    }
  }, [forming, bars])

  const shown = legend && sorted.length ? sorted[hoverT !== null && index.has(hoverT) ? index.get(hoverT)! : sorted.length - 1] : null
  const prev = shown ? sorted[sorted.indexOf(shown) - 1] : undefined
  const mas = shown ? ma.map((n) => movingAverage(sorted.slice(Math.max(0, sorted.indexOf(shown) - n + 1), sorted.indexOf(shown) + 1), n).pop()?.[1]) : []
  return (
    <div className="kline-wrap">
      <div ref={box} className="kline" style={{ height }} role="img" aria-label={ariaLabel} />
      {shown && <KLegend bar={shown} prev={prev} ma={ma} maValues={mas} hovering={hoverT !== null} />}
    </div>
  )
}

const MA_COLORS = ['#d9a21b', '#6449c7', '#0f8f9a', '#c2410c']

function movingAverage(rows: KBar[], n: number): [number, number][] {
  const out: [number, number][] = []
  let sum = 0
  rows.forEach((b, i) => {
    sum += b.c
    if (i >= n) sum -= rows[i - n].c
    if (i >= n - 1) out.push([b.t, sum / n])
  })
  return out
}

const px = (v: number) => v.toLocaleString('en-US', { maximumFractionDigits: v >= 1000 ? 2 : v >= 1 ? 4 : 6 })
const vol = (v: number) => (v >= 1e9 ? `${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `${(v / 1e6).toFixed(2)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(2)}K` : v.toFixed(2))

function KLegend({ bar, prev, ma, maValues, hovering }: { bar: KBar; prev?: KBar; ma: number[]; maValues: (number | undefined)[]; hovering: boolean }) {
  const base = prev?.c ?? bar.o
  const chg = base ? bar.c / base - 1 : 0
  const amp = bar.l ? (bar.h - bar.l) / bar.l : 0
  const tone = chg >= 0 ? 'positive' : 'negative'
  const d = new Date(bar.t)
  const when = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')} UTC`
  return (
    <div className={`kline-legend${hovering ? ' on' : ''}`} aria-live="polite">
      <span className="kl-time">{when}</span>
      <span>开 <b className="num">{px(bar.o)}</b></span>
      <span>高 <b className="num">{px(bar.h)}</b></span>
      <span>低 <b className="num">{px(bar.l)}</b></span>
      <span>收 <b className={`num ${tone}`}>{px(bar.c)}</b></span>
      <span>涨跌 <b className={`num ${tone}`}>{chg >= 0 ? '+' : ''}{(chg * 100).toFixed(2)}%</b></span>
      <span>振幅 <b className="num">{(amp * 100).toFixed(2)}%</b></span>
      {bar.v !== undefined && <span>量 <b className="num">{vol(bar.v)}</b></span>}
      {ma.map((n, k) => maValues[k] !== undefined && (
        <span key={n} style={{ color: MA_COLORS[k % MA_COLORS.length] }}>MA{n} <b className="num">{px(maValues[k]!)}</b></span>
      ))}
    </div>
  )
}
