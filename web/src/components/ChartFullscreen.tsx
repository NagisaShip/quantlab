import { useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

export interface ChartSize { width: number; height: number }

function useBox<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [size, setSize] = useState<ChartSize>({ width: 640, height: 420 })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect
      if (r) setSize({ width: Math.max(280, Math.round(r.width)), height: Math.max(240, Math.round(r.height)) })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, size] as const
}

/** 图表右上角的放大按钮。 */
export function ChartExpandButton({ onClick, inline, label = '全屏查看图表' }: { onClick: () => void; inline?: boolean; label?: string }) {
  const enter = () => {
    // 在真实手机上争取真正的系统横屏；失败时由全屏层做旋转兜底。
    try { document.documentElement.requestFullscreen?.().catch(() => undefined) } catch { /* 不支持全屏 */ }
    onClick()
  }
  return (
    <button type="button" className={inline ? 'qb-icon-btn chart-expand-icon' : 'chart-expand-icon'} onClick={enter} title={label} aria-label={label}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <path d="M5.5 2.5H2.5v3M10.5 2.5h3v3M5.5 13.5h-3v-3M10.5 13.5h3v-3" />
        <path d="M2.8 5.8 5.8 2.8M13.2 5.8 10.2 2.8M2.8 10.2l3 3M13.2 10.2l-3 3" opacity=".55" />
      </svg>
    </button>
  )
}

/**
 * 图表全屏容器：默认横屏；在竖屏手机上会把图表区域旋转 90° 使用。
 * 用户可在顶部切换横屏 / 竖屏，并随时关闭回到页面。
 */
export function ChartFullscreen({ title, onClose, children, footer }: { title?: string; onClose: () => void; children: (size: ChartSize) => ReactNode; footer?: ReactNode }) {
  const [orientation, setOrientation] = useState<'landscape' | 'portrait'>('landscape')
  const [portraitDevice, setPortraitDevice] = useState(() => window.matchMedia?.('(orientation: portrait)').matches ?? false)
  const [box, size] = useBox<HTMLDivElement>()
  useEffect(() => {
    const mq = window.matchMedia?.('(orientation: portrait)')
    if (!mq) return
    const onChange = () => setPortraitDevice(mq.matches)
    onChange()
    mq.addEventListener?.('change', onChange)
    return () => mq.removeEventListener?.('change', onChange)
  }, [])
  useEffect(() => {
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    document.documentElement.classList.add('chart-fs-open')
    const orientation = screen.orientation as ScreenOrientation & { lock?: (o: string) => Promise<void>; unlock?: () => void }
    orientation?.lock?.('landscape').catch(() => undefined)
    return () => {
      document.body.style.overflow = prev
      document.documentElement.classList.remove('chart-fs-open')
      try { orientation?.unlock?.() } catch { /* 忽略 */ }
      if (document.fullscreenElement) document.exitFullscreen?.().catch(() => undefined)
    }
  }, [])
  const rotate = orientation === 'landscape' && portraitDevice
  const contentSize = rotate ? { width: size.height, height: size.width } : size
  return createPortal(
    <div className="chart-fs" role="dialog" aria-modal="true" aria-label={title || '图表全屏'}>
      <header className="chart-fs-head">
        <b>{title || '图表'}</b>
        <span className="chart-fs-hint">{rotate ? '横屏显示 · 图表已旋转' : orientation === 'landscape' ? '横屏显示' : '竖屏显示'}</span>
        <span className="chart-fs-grow" />
        <button type="button" className="chart-fs-btn" onClick={() => setOrientation((x) => (x === 'landscape' ? 'portrait' : 'landscape'))}>
          {orientation === 'landscape' ? '竖屏' : '横屏'}
        </button>
        <button type="button" className="chart-fs-btn close" onClick={onClose} aria-label="关闭全屏">×</button>
      </header>
      <div ref={box} className={`chart-fs-main${rotate ? ' rotated' : ''}`}>
        <div className="chart-fs-content" style={rotate ? { width: size.height, height: size.width } : undefined}>{children(contentSize)}</div>
      </div>
      {footer && <div className="chart-fs-foot">{footer}</div>}
    </div>,
    document.body,
  )
}
