/**
 * 研究工作区右栏宿主（P2）。
 *
 * 把原来的单一「研究检查器」升级为三个页签的辅助区：
 * - 检查器：原 `wf-inspector` 内容原样搬入；
 * - 问 AI：嵌入 AskAI 面板（同一个会话 Map / scope / 草稿 / 流式路由）；
 * - 上下文：把 AskAI 的 ResearchContextStrip 提到独立页签，输出引用与聊天共享。
 *
 * 三态：
 * - compact：默认宽度（304px，窄屏由 CSS 断点收敛）；
 * - expanded：可拖宽 360–480px（键盘方向键同样可调）；
 * - focus：隐藏左侧模块库与画布，右栏占满工作区，提供「返回画布」。
 *
 * 收起按钮沿用原 `inspOpen` 行为（`.is-collapsed` + 40px 竖条）。宿主只负责布局与
 * 页签切换，内容由 WorkflowPage 传入，避免复制检查器/聊天逻辑。
 */
import { useCallback, useEffect, useRef, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'

export type RightTab = 'inspector' | 'ask' | 'context'
export type RightState = 'compact' | 'expanded' | 'focus'

export const RIGHT_TABS: { id: RightTab; label: string }[] = [
  { id: 'inspector', label: '检查器' },
  { id: 'ask', label: '问 AI' },
  { id: 'context', label: '上下文' },
]

export const PANEL_MIN_WIDTH = 360
export const PANEL_MAX_WIDTH = 480

export const clampPanelWidth = (value: number) =>
  Math.max(PANEL_MIN_WIDTH, Math.min(PANEL_MAX_WIDTH, Math.round(Number.isFinite(value) ? value : 420)))

export default function PanelHost({
  open,
  onToggleOpen,
  tab,
  onTab,
  state,
  onState,
  width,
  onWidth,
  inspector,
  ask,
  context,
}: {
  /** 对应原 inspOpen：收起后只保留 40px 竖条与展开按钮。 */
  open: boolean
  onToggleOpen: () => void
  tab: RightTab
  onTab: (tab: RightTab) => void
  state: RightState
  onState: (state: RightState) => void
  width: number
  onWidth: (width: number) => void
  inspector: ReactNode
  ask: ReactNode
  context: ReactNode
}) {
  const dragging = useRef<{ startX: number; startWidth: number } | null>(null)
  const cleanup = useRef<(() => void) | null>(null)
  const widthRef = useRef(width)
  widthRef.current = width
  const onWidthRef = useRef(onWidth)
  onWidthRef.current = onWidth

  useEffect(() => () => { cleanup.current?.() }, [])

  const endDrag = useCallback(() => {
    cleanup.current?.()
    cleanup.current = null
    dragging.current = null
  }, [])

  const resize = useCallback((clientX: number) => {
    const drag = dragging.current
    if (!drag) return
    onWidthRef.current(clampPanelWidth(drag.startWidth + (drag.startX - clientX)))
  }, [])

  const startDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (state !== 'expanded' || event.button !== 0) return
    event.preventDefault()
    dragging.current = { startX: event.clientX, startWidth: widthRef.current }
    const move = (e: globalThis.PointerEvent) => resize(e.clientX)
    const up = () => endDrag()
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', up)
    cleanup.current = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      window.removeEventListener('pointercancel', up)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }

  const resizeKeys = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 48 : 16
    let next: number | null = null
    if (event.key === 'ArrowLeft') next = widthRef.current + step
    else if (event.key === 'ArrowRight') next = widthRef.current - step
    else if (event.key === 'Home') next = PANEL_MIN_WIDTH
    else if (event.key === 'End') next = PANEL_MAX_WIDTH
    if (next === null) return
    event.preventDefault()
    onWidth(clampPanelWidth(next))
  }

  return (
    <aside
      className={`wf-inspector wf-panel-host ${open ? '' : 'is-collapsed'}`}
      aria-label="研究工作区辅助区"
      data-panel-state={open ? state : 'collapsed'}
    >
      {open ? (
        <div className="wf-pane-head wf-panel-head">
          <div className="wf-panel-tabrow">
            <div className="wf-panel-tabs" role="tablist" aria-label="右侧辅助区页签">
              {RIGHT_TABS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  id={`wf-panel-tab-${item.id}`}
                  role="tab"
                  className={tab === item.id ? 'on' : ''}
                  aria-selected={tab === item.id}
                  aria-controls={`wf-panel-panel-${item.id}`}
                  onClick={() => onTab(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <div className="wf-panel-tools">
              {state === 'focus' ? (
                <button type="button" className="wf-pane-icon" onClick={() => onState('expanded')} aria-label="返回画布与模块库" title="返回画布与模块库">↙</button>
              ) : (
                <button type="button" className="wf-pane-icon" onClick={() => onState('focus')} aria-label="全屏显示右栏" title="全屏显示右栏">⤢</button>
              )}
              {state !== 'expanded' && state !== 'focus' && (
                <button type="button" className="wf-pane-icon" onClick={() => onState('expanded')} aria-label="加宽右栏" title="加宽右栏">⇤</button>
              )}
              {state === 'expanded' && (
                <button type="button" className="wf-pane-icon" onClick={() => onState('compact')} aria-label="还原右栏宽度" title="还原右栏宽度">⇥</button>
              )}
              <button
                type="button"
                className="wf-pane-toggle"
                aria-label="收起辅助区"
                aria-expanded={open}
                title="收起辅助区"
                onClick={onToggleOpen}
              >
                »
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="wf-pane-head">
          <h2>研究辅助区</h2>
          <button
            type="button"
            className="wf-pane-toggle"
            aria-label="展开辅助区"
            aria-expanded={open}
            title="展开辅助区"
            onClick={onToggleOpen}
          >
            «
          </button>
        </div>
      )}
      {/* 页签内容始终挂载（只切换显示），这样切走「问 AI」不会中断进行中的流式回答。 */}
      <div
        className="wf-pane-body wf-panel-body"
        id="wf-panel-panel-inspector"
        role="tabpanel"
        aria-labelledby="wf-panel-tab-inspector"
        hidden={!open || tab !== 'inspector'}
      >
        {inspector}
      </div>
      <div
        className="wf-pane-body wf-panel-body is-ask"
        id="wf-panel-panel-ask"
        role="tabpanel"
        aria-labelledby="wf-panel-tab-ask"
        hidden={!open || tab !== 'ask'}
      >
        {ask}
      </div>
      <div
        className="wf-pane-body wf-panel-body"
        id="wf-panel-panel-context"
        role="tabpanel"
        aria-labelledby="wf-panel-tab-context"
        hidden={!open || tab !== 'context'}
      >
        {context}
      </div>
      {open && state === 'expanded' && (
        <div
          className="wf-panel-resizer"
          role="separator"
          aria-label="调整右栏宽度"
          aria-orientation="vertical"
          aria-valuemin={PANEL_MIN_WIDTH}
          aria-valuemax={PANEL_MAX_WIDTH}
          aria-valuenow={width}
          tabIndex={0}
          title="拖动调整右栏宽度（360–480）"
          onPointerDown={startDrag}
          onKeyDown={resizeKeys}
        />
      )}
    </aside>
  )
}
