import { useEffect, useRef } from 'react'
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

const sec = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp

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
}: {
  bars: KBar[]
  marks?: KMark[]
  forming?: KBar | null
  height?: number
  /** 一根 K 线的毫秒数：用来把成交时间对到所在 K 线 */
  stepMs: number
  ariaLabel?: string
}) {
  const box = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const candles = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null)
  const fitted = useRef(false)

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
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 4 },
      crosshair: { mode: CrosshairMode.Normal },
      localization: { locale: 'zh-CN' },
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
    const dbl = () => c.timeScale().scrollToRealTime()
    box.current.addEventListener('dblclick', dbl)
    const el = box.current
    return () => {
      mo.disconnect()
      el.removeEventListener('dblclick', dbl)
      c.remove()
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
    const rows = [...bars].sort((a, b) => a.t - b.t)
    const seen = new Set<number>()
    const uniq = rows.filter((b) => (seen.has(b.t) ? false : (seen.add(b.t), true)))
    cs.setData(uniq.map((b) => ({ time: sec(b.t), open: b.o, high: b.h, low: b.l, close: b.c })))
    vs.setData(uniq.map((b) => ({ time: sec(b.t), value: b.v ?? 0, color: alpha(b.c >= b.o ? p.up : p.down, 0.35) })))
    if (uniq.length) {
      const first = uniq[0].t
      const last = uniq[uniq.length - 1].t
      const ms: SeriesMarker<Time>[] = marks
        .filter((m) => m.ts >= first && m.ts < last + 2 * stepMs)
        .map((m) => {
          const buy = m.side === 'buy'
          const t = Math.min(last, first + Math.floor((m.ts - first) / stepMs) * stepMs)
          return {
            time: sec(t),
            position: buy ? 'belowBar' : 'aboveBar',
            color: buy ? p.up : p.down,
            shape: buy ? 'arrowUp' : 'arrowDown',
            text: m.text ?? (buy ? '买' : '卖'),
          } as SeriesMarker<Time>
        })
        .sort((a, b) => (a.time as number) - (b.time as number))
      cs.setMarkers(ms)
    }
    if (!fitted.current && uniq.length) {
      chart.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, uniq.length - 120), to: uniq.length + 4 })
      fitted.current = true
    }
  }, [bars, marks, stepMs])

  // 正在形成的那根：只更新最后一根，不重画
  useEffect(() => {
    const cs = candles.current
    if (!cs || !forming || !bars.length) return
    const last = bars.reduce((m, b) => Math.max(m, b.t), 0)
    if (forming.t < last) return
    try {
      cs.update({ time: sec(forming.t), open: forming.o, high: forming.h, low: forming.l, close: forming.c })
      const p = palette()
      volume.current?.update({ time: sec(forming.t), value: forming.v ?? 0, color: alpha(forming.c >= forming.o ? p.up : p.down, 0.2) })
    } catch {
      /* 时间早于最后一根：忽略 */
    }
  }, [forming, bars])

  return <div ref={box} className="kline" style={{ height }} role="img" aria-label={ariaLabel} />
}
