import { useCallback, useEffect, useRef, useState, type PointerEvent as RPE, type WheelEvent as RWE } from 'react'
import { createPortal } from 'react-dom'

/**
 * 全站统一的图片大屏查看器：点击对话、帖子、问题反馈里的任何图片，都在当前页面上方全屏打开，不离开页面。
 * - 滚轮 / 双击 / 按钮缩放，拖动平移，← → 切换，Esc 关闭，底部缩略图条；
 * - 下载走 fetch → Blob，不会跳转到新页面。
 * 用法：viewImages([url1, url2], 1)；挂载点 <ImageViewerHost /> 在 main.tsx 里放一次。
 */

export interface ViewerImage { src: string; name?: string }
const EVENT = 'qa:view-images'

export function viewImages(images: (string | ViewerImage)[], index = 0) {
  const list = images.map((x) => (typeof x === 'string' ? { src: x } : x)).filter((x) => x.src)
  if (!list.length) return
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { list, index: Math.max(0, Math.min(index, list.length - 1)) } }))
}

/** 点击这些位置的图片都在大屏里打开（全局捕获，新页面里的图片也自动生效）。 */
const ZOOMABLE = '.md img, .cb-user-images img, .ref-image img, .issue-thumb img, .cmp-attach-item img, [data-zoomable] img, img[data-zoomable]'
/** 同一组图片的容器：一条消息、一个帖子、一组附件 */
const GALLERY = '[data-gallery], .md, .cb-user-images, .issue-files, .cmp-attach, .ref-image'

/** 把一个容器里的 <img> 收成一组画廊：点击其中一张，从它开始看。 */
export function viewFromContainer(img: HTMLImageElement) {
  const root = img.closest(GALLERY) || img.parentElement
  const imgs = root ? [...root.querySelectorAll<HTMLImageElement>('img')].filter((x) => !x.closest('.iv')) : [img]
  viewImages(imgs.map((x) => ({ src: x.currentSrc || x.src, name: x.alt })), Math.max(0, imgs.indexOf(img)))
}

async function download(img: ViewerImage) {
  const name = (img.name || img.src.split('/').pop()?.split('?')[0] || 'image').replace(/[\\/:*?"<>|]+/g, '_')
  try {
    const res = await fetch(img.src, { credentials: img.src.startsWith('/') ? 'include' : 'omit' })
    const blob = await res.blob()
    const ext = /\.\w{2,5}$/.test(name) ? '' : `.${(blob.type.split('/')[1] || 'png').replace('jpeg', 'jpg').replace('svg+xml', 'svg')}`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name + ext
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 2000)
  } catch {
    const a = document.createElement('a')
    a.href = img.src
    a.download = name
    a.click()
  }
}

const MIN = 0.2
const MAX = 8

export function ImageViewerHost() {
  const [state, setState] = useState<{ list: ViewerImage[]; index: number } | null>(null)
  const [scale, setScale] = useState(1)
  const [pos, setPos] = useState({ x: 0, y: 0 })
  const [closing, setClosing] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const drag = useRef<{ x: number; y: number; px: number; py: number; moved: boolean } | null>(null)

  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<{ list: ViewerImage[]; index: number }>).detail
      setState(d)
      setClosing(false)
    }
    // 捕获阶段拦截：比图片外层 <a target="_blank"> 先处理，所以不会再打开新页面
    const onClick = (e: MouseEvent) => {
      if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return
      const img = (e.target as Element | null)?.closest?.('img') as HTMLImageElement | null
      if (!img || img.closest('.iv, .qb-mermaid, .no-zoom') || !img.matches(ZOOMABLE)) return
      e.preventDefault()
      e.stopPropagation()
      viewFromContainer(img)
    }
    window.addEventListener(EVENT, on)
    document.addEventListener('click', onClick, true)
    return () => { window.removeEventListener(EVENT, on); document.removeEventListener('click', onClick, true) }
  }, [])

  const reset = useCallback(() => { setScale(1); setPos({ x: 0, y: 0 }) }, [])
  useEffect(() => { reset(); setLoaded(false) }, [state?.index, state?.list, reset])

  const close = useCallback(() => {
    setClosing(true)
    window.setTimeout(() => { setState(null); setClosing(false) }, 180)
  }, [])
  const go = useCallback((d: number) => setState((s) => (s ? { ...s, index: (s.index + d + s.list.length) % s.list.length } : s)), [])
  const zoom = useCallback((f: number) => setScale((s) => Math.min(MAX, Math.max(MIN, +(s * f).toFixed(3)))), [])

  useEffect(() => {
    if (!state) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close()
      else if (e.key === 'ArrowRight') go(1)
      else if (e.key === 'ArrowLeft') go(-1)
      else if (e.key === '+' || e.key === '=') zoom(1.25)
      else if (e.key === '-') zoom(0.8)
      else if (e.key === '0') reset()
    }
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('keydown', onKey); document.body.style.overflow = prev }
  }, [state, close, go, zoom, reset])

  if (!state) return null
  const img = state.list[state.index]
  const many = state.list.length > 1

  const onWheel = (e: RWE) => { zoom(e.deltaY < 0 ? 1.12 : 1 / 1.12) }
  const onDown = (e: RPE) => {
    (e.target as Element).setPointerCapture?.(e.pointerId)
    drag.current = { x: e.clientX, y: e.clientY, px: pos.x, py: pos.y, moved: false }
  }
  const onMove = (e: RPE) => {
    const d = drag.current
    if (!d) return
    const dx = e.clientX - d.x, dy = e.clientY - d.y
    if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true
    setPos({ x: d.px + dx, y: d.py + dy })
  }
  const onUp = () => { drag.current = null }

  return createPortal(
    <div className={`iv${closing ? ' closing' : ''}`} role="dialog" aria-modal="true" aria-label="查看图片"
      onClick={(e) => { if (e.target === e.currentTarget) close() }}>
      <div className="iv-top" onClick={(e) => e.stopPropagation()}>
        <span className="iv-name">{img.name || '图片'}{many && <em> · {state.index + 1} / {state.list.length}</em>}</span>
        <span className="iv-grow" />
        <button type="button" onClick={() => zoom(0.8)} aria-label="缩小" title="缩小（-）">−</button>
        <button type="button" className="iv-pct" onClick={reset} title="还原（0）">{Math.round(scale * 100)}%</button>
        <button type="button" onClick={() => zoom(1.25)} aria-label="放大" title="放大（+）">+</button>
        <button type="button" onClick={() => download(img)} title="下载">下载</button>
        <button type="button" onClick={close} aria-label="关闭" title="关闭（Esc）">✕</button>
      </div>
      <div className="iv-stage" onWheel={onWheel} onClick={(e) => { if (e.target === e.currentTarget) close() }}>
        {!loaded && <span className="iv-spin" aria-hidden />}
        <img key={img.src} src={img.src} alt={img.name || ''} draggable={false} className={`iv-img${loaded ? ' on' : ''}`}
          style={{ transform: `translate(${pos.x}px, ${pos.y}px) scale(${scale})`, cursor: scale > 1 ? 'grab' : 'zoom-in' }}
          onLoad={() => setLoaded(true)}
          onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}
          onDoubleClick={() => (scale === 1 ? setScale(2) : reset())}
          onClick={(e) => { e.stopPropagation(); if (!drag.current?.moved && scale === 1) setScale(2) }} />
        {many && <button type="button" className="iv-nav prev" onClick={(e) => { e.stopPropagation(); go(-1) }} aria-label="上一张">‹</button>}
        {many && <button type="button" className="iv-nav next" onClick={(e) => { e.stopPropagation(); go(1) }} aria-label="下一张">›</button>}
      </div>
      {many && (
        <div className="iv-strip" onClick={(e) => e.stopPropagation()}>
          {state.list.map((x, i) => (
            <button type="button" key={x.src + i} className={i === state.index ? 'on' : ''} onClick={() => setState({ ...state, index: i })} aria-label={`第 ${i + 1} 张`}>
              <img src={x.src} alt="" loading="lazy" />
            </button>
          ))}
        </div>
      )}
      <div className="iv-hint">滚轮缩放 · 拖动平移 · 双击放大 / 还原 · ← → 切换 · Esc 关闭</div>
    </div>,
    document.body,
  )
}
