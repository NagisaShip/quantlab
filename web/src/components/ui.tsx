import { Component, useContext, useEffect, useRef, useState, type ErrorInfo, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { PageSlot } from './pageSlot'
import { QuantBot, QuantEyes } from './QuantBot'

/** 面板：白色圆角卡片 + 标题行。flush 表示内容自己管边距（常用于表格）。 */
export function Panel({
  title,
  subtitle,
  actions,
  flush,
  children,
}: {
  title?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  flush?: boolean
  children: ReactNode
}) {
  return (
    <section className="panel">
      {(title || actions) && (
        <div className="panel-heading">
          <div>
            {title && <h2>{title}{typeof subtitle === 'string' && <span className="panel-info" title={subtitle} aria-label={subtitle}>i</span>}</h2>}
            {subtitle && typeof subtitle !== 'string' && <p>{subtitle}</p>}
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </div>
      )}
      <div className={flush ? '' : 'panel-body'}>{children}</div>
    </section>
  )
}

/** 页头：小标题 + 大标题 + 绿色句点 + 说明 + 右侧动作。 */
export function Heading({
  eyebrow,
  title,
  description,
  actions,
}: {
  /** 保留参数以兼容旧调用；新设计不再显示英文小标题 */
  eyebrow?: string
  title: string
  description?: ReactNode
  actions?: ReactNode
}) {
  const slot = useContext(PageSlot)
  // 有统一页头时，标题由页头显示，这里只把操作按钮放进页头右侧
  if (slot) return actions ? createPortal(actions, slot) : null
  return (
    <div className="heading">
      <div>
        <h1 data-section={eyebrow}>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions}
    </div>
  )
}

/** 指标卡。带迷你柱的卡片用于"最好/最差"这类需要一点趋势暗示的场合。 */
export function MetricCard({
  label,
  glyph,
  value,
  unit,
  caption,
  foot,
  tone,
  bars,
  progress,
}: {
  label: string
  glyph?: string
  value: ReactNode
  unit?: string
  caption?: ReactNode
  foot?: ReactNode
  tone?: 'positive' | 'negative'
  bars?: boolean
  progress?: number
}) {
  return (
    <article className="metric-card">
      <div className="metric-label">
        <span>{label}</span>
        {glyph && <span>{glyph}</span>}
      </div>
      <div className={`metric-number ${tone ?? ''}`}>
        {value}
        {unit && <small>{unit}</small>}
      </div>
      {progress !== undefined && (
        <div className="progress-track">
          <div style={{ width: `${Math.max(0, Math.min(100, progress))}%` }} />
        </div>
      )}
      {caption && <div className="metric-caption">{caption}</div>}
      {foot && <div className="metric-foot">{foot}</div>}
      {bars && (
        <div className="mini-bars" aria-hidden="true">
          <i /><i /><i /><i /><i /><i /><i /><i />
        </div>
      )}
    </article>
  )
}

/** 数据来源横幅：每次实验/每个页面都必须说清数据是什么。 */
export function SourceBanner({
  tone = 'paper',
  icon = '◈',
  title,
  detail,
  right,
  warn,
}: {
  tone?: 'paper' | 'real' | 'warn'
  icon?: string
  title: ReactNode
  detail?: ReactNode
  right?: ReactNode
  warn?: ReactNode
}) {
  const cls = tone === 'real' ? 'source-banner real' : tone === 'warn' ? 'source-banner warn' : 'source-banner'
  return (
    <div className={cls}>
      <span className="status-icon">{icon}</span>
      <div>
        <strong>{title}</strong>
        {detail && <span>{detail}</span>}
        {warn && <span className="banner-warn">{warn}</span>}
      </div>
      {right && <span className="banner-right">{right}</span>}
    </div>
  )
}

export function Tag({ children, tone, mono }: { children: ReactNode; tone?: string; mono?: boolean }) {
  const cls = ['tag', tone ?? '', mono ? 'mono' : ''].filter(Boolean).join(' ')
  return <span className={cls}>{children}</span>
}

export function Callout({
  tone = 'info',
  title,
  children,
}: {
  tone?: 'info' | 'warn' | 'fail'
  title?: ReactNode
  children: ReactNode
}) {
  const cls = tone === 'warn' ? 'callout warn' : tone === 'fail' ? 'callout fail' : 'callout'
  return (
    <div className={cls}>
      {title && <strong>{title}　</strong>}
      {children}
    </div>
  )
}

export function EmptyState({
  glyph,
  title,
  children,
  action,
}: {
  glyph?: ReactNode
  title: string
  children?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="empty-state">
      {glyph === undefined ? <QuantBot size={34} state="idle" motion="low" title={title} /> : <span>{glyph}</span>}
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action}
    </div>
  )
}

export function Loading({ label = '加载中' }: { label?: string }) {
  return (
    <div className="bot-loading" role="status" aria-live="polite">
      <QuantEyes size={34} state="thinking" speed={1.2} title={label} className="bot-loading-eyes" />
      <h3>{label}…</h3>
    </div>
  )
}

/** 分段切换器：用于 7 天 / 30 天这类互斥视图切换。 */
export function Segments<T extends string>({
  value,
  options,
  onChange,
  ariaLabel,
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
  ariaLabel?: string
}) {
  return (
    <div className="segments" role="group" aria-label={ariaLabel}>
      {options.map((o) => (
        <button key={o.value} aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

export function Modal({
  title,
  onClose,
  footer,
  children,
  width,
}: {
  title: ReactNode
  onClose: () => void
  footer?: ReactNode
  children: ReactNode
  width?: number
}) {
  return (
    <div className="modal-backdrop" onClick={onClose} role="presentation">
      <div
        className="modal"
        style={width ? { maxWidth: width } : undefined}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <header>
          <h3>{title}</h3>
          <div className="spacer" />
          <button className="button small" onClick={onClose} aria-label="关闭">关闭</button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer>{footer}</footer>}
      </div>
    </div>
  )
}

export function Field({
  label,
  hint,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <label style={{ display: 'block' }}>
      {label}
      {children}
      {hint && <div className="hint">{hint}</div>}
    </label>
  )
}

/** 全屏忙碌遮罩：回测/团队调研这类要等几秒的操作都用它。 */
export function BusyOverlay({ title, note }: { title: string; note?: string }) {
  return (
    <div className="busy-overlay">
      <div className="busy-card">
        <QuantBot size={52} state="focus" title={title} />
        <strong>{title}</strong>
        {note && <p>{note}</p>}
      </div>
    </div>
  )
}

/** 底部提示条。错误停留更久，让人来得及读完。 */
export function Toast({ message, error, onDone }: { message: string; error?: boolean; onDone: () => void }) {
  return (
    <div
      className={`toast show${error ? ' error' : ''}`}
      role="status"
      aria-live="polite"
      onClick={onDone}
    >
      <QuantEyes size={18} state={error ? 'error' : 'happy'} motion="low" title={error ? '出错了' : '完成'} />
      {message}
    </div>
  )
}

/** 默认收起的说明：规则、边界这类读一次就够的内容，不该常驻占版面。 */
export function Fold({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <details className="fold">
      <summary><span>{title}</span>{count !== undefined && <small>{count} 条</small>}</summary>
      <div className="fold-body">{children}</div>
    </details>
  )
}

/** 把后端文案里的 **加粗** 渲染出来，其余原样显示。 */
export function Rich({ text }: { text: string }) {
  const parts = text.split(/\*\*(.+?)\*\*/g)
  return <>{parts.map((p, i) => (i % 2 ? <b key={i}>{p}</b> : p))}</>
}

/** 主按钮 + 下拉：一个最常用的动作放在外面，其余同类动作收进菜单，每项带一句说明。 */
export function SplitButton({ label, onClick, items }: {
  label: ReactNode
  onClick: () => void
  items: { label: string; note?: string; onClick: () => void }[]
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [open])
  return (
    <div className="split menu" ref={ref}>
      <button className="button primary" onClick={onClick}>{label}</button>
      <button className="button primary split-caret" aria-label="更多新建方式" aria-expanded={open} onClick={() => setOpen(!open)}>▾</button>
      {open && (
        <div className="menu-list split-list" role="menu">
          {items.map((it) => (
            <button key={it.label} role="menuitem" onClick={() => { setOpen(false); it.onClick() }}>
              <b>{it.label}</b>{it.note && <small>{it.note}</small>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** 页面级错误兜底：低动态的报错 Bot + 原因 + 重试，不让一个页面的异常拖垮整个应用。 */
export class PageErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) { return { error } }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error('页面出错', error, info.componentStack) }
  componentDidUpdate(prev: { resetKey?: string }) { if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null }) }
  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="bot-error-page anim-fade" role="alert">
        <QuantBot size={64} state="error" motion="low" title="页面出错了" />
        <h3>这个页面出错了</h3>
        <p>{this.state.error.message || '未知错误'}</p>
        <div className="button-row">
          <button className="button primary" onClick={() => this.setState({ error: null })}>重试</button>
          <a className="button" href="#/home">回到首页</a>
        </div>
      </div>
    )
  }
}
