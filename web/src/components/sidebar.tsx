/**
 * 左侧栏的开关与宽度：
 * - 收起 / 展开：侧栏顶部按钮、收起后顶栏左侧按钮，或 Ctrl/⌘ + B；
 * - 拖动右边缘调宽度（200–420px），双击恢复默认；
 * 都记在本机偏好里。宽度写到 CSS 变量 --rail-w，主区域跟着让位。
 */
import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { store } from './prefs'

const W_KEY = 'qa.side.w'
const C_KEY = 'qa.side.collapsed'
export const SIDE_MIN = 200
export const SIDE_MAX = 420
export const SIDE_DEFAULT = 232

const clamp = (w: number) => Math.min(SIDE_MAX, Math.max(SIDE_MIN, Math.round(w)))

export function useSidebar() {
  const [width, setWidthState] = useState(() => clamp(Number(store.get(W_KEY, String(SIDE_DEFAULT))) || SIDE_DEFAULT))
  const [collapsed, setCollapsed] = useState(() => store.get(C_KEY, '0') === '1')

  useEffect(() => {
    const root = document.documentElement
    root.style.setProperty('--rail-w', collapsed ? '0px' : `${width}px`)
    root.style.setProperty('--rail-open-w', `${width}px`)
    root.classList.toggle('side-collapsed', collapsed)
  }, [width, collapsed])
  useEffect(() => () => {
    const root = document.documentElement
    root.style.removeProperty('--rail-w')
    root.style.removeProperty('--rail-open-w')
    root.classList.remove('side-collapsed')
  }, [])

  const setWidth = useCallback((w: number) => { const v = clamp(w); setWidthState(v); store.set(W_KEY, String(v)) }, [])
  const toggle = useCallback(() => setCollapsed((c) => { store.set(C_KEY, c ? '0' : '1'); return !c }), [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.shiftKey || e.altKey || e.key.toLowerCase() !== 'b') return
      if ((e.target as HTMLElement | null)?.isContentEditable) return
      e.preventDefault()
      toggle()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggle])

  return { width, setWidth, collapsed, toggle }
}

export function SideToggle({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
  const label = collapsed ? '展开侧栏（Ctrl+B）' : '收起侧栏（Ctrl+B）'
  return (
    <button type="button" className={`side-toggle${collapsed ? ' floating' : ''}`} onClick={onToggle} title={label} aria-label={label} aria-expanded={!collapsed}>
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden>
        <rect x="2" y="2.5" width="12" height="11" rx="2" />
        <path d="M6 2.5v11" />
        {collapsed ? <path d="m8.6 6.4 1.6 1.6-1.6 1.6" strokeLinecap="round" strokeLinejoin="round" /> : <path d="M10.6 6.4 9 8l1.6 1.6" strokeLinecap="round" strokeLinejoin="round" />}
      </svg>
    </button>
  )
}

/** 侧栏右边缘的拖动条。 */
export function SideResizer({ width, onWidth }: { width: number; onWidth: (w: number) => void }) {
  const drag = useRef<{ x: number; w: number } | null>(null)
  const [live, setLive] = useState<number | null>(null)

  const move = (e: PointerEvent) => {
    if (!drag.current) return
    const w = clamp(drag.current.w + e.clientX - drag.current.x)
    setLive(w)
    document.documentElement.style.setProperty('--rail-w', `${w}px`)
  }
  const up = (e: PointerEvent) => {
    if (!drag.current) return
    const w = clamp(drag.current.w + e.clientX - drag.current.x)
    drag.current = null
    setLive(null)
    document.documentElement.classList.remove('side-resizing')
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    onWidth(w)
  }
  const down = (e: ReactPointerEvent) => {
    if (e.button !== 0) return
    e.preventDefault()
    drag.current = { x: e.clientX, w: width }
    document.documentElement.classList.add('side-resizing')
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }
  useEffect(() => () => {
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    document.documentElement.classList.remove('side-resizing')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return (
    <div className={`side-resizer${live !== null ? ' on' : ''}`} role="separator" aria-orientation="vertical" aria-label="调整侧栏宽度"
      aria-valuemin={SIDE_MIN} aria-valuemax={SIDE_MAX} aria-valuenow={live ?? width} tabIndex={0} title="拖动调整宽度，双击恢复默认"
      onPointerDown={down} onDoubleClick={() => onWidth(SIDE_DEFAULT)}
      onKeyDown={(e) => {
        if (e.key === 'ArrowLeft') { e.preventDefault(); onWidth(width - 16) }
        if (e.key === 'ArrowRight') { e.preventDefault(); onWidth(width + 16) }
      }} />
  )
}
