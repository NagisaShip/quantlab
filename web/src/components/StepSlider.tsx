import { useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'

/**
 * 分档滑块：可以左右拖动，也可以点刻度名、用方向键调整。思考努力程度等「有序档位」都用它。
 * 拖动时滑块跟手，松开后吸附到最近的档位（带缓动）。
 */
export interface StepOption<T extends string> { id: T; label: string; hint?: string }

export function StepSlider<T extends string>({ options, value, onChange, disabled, label, compact }: {
  options: StepOption<T>[]
  value: T
  onChange: (v: T) => void
  disabled?: boolean
  label?: string
  compact?: boolean
}) {
  const track = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<number | null>(null) // 拖动中的位置（0..1）
  const n = options.length
  const idx = Math.max(0, options.findIndex((o) => o.id === value))
  const pos = drag ?? (n > 1 ? idx / (n - 1) : 0)

  const ratioAt = (clientX: number) => {
    const r = track.current?.getBoundingClientRect()
    if (!r || r.width <= 0) return pos
    return Math.min(1, Math.max(0, (clientX - r.left) / r.width))
  }
  const snap = (ratio: number) => options[Math.round(ratio * (n - 1))]

  const onDown = (e: PointerEvent<HTMLDivElement>) => {
    if (disabled) return
    e.currentTarget.setPointerCapture(e.pointerId)
    const r = ratioAt(e.clientX)
    setDrag(r)
    const o = snap(r)
    if (o.id !== value) onChange(o.id)
  }
  const onMove = (e: PointerEvent<HTMLDivElement>) => {
    if (drag === null) return
    const r = ratioAt(e.clientX)
    setDrag(r)
    const o = snap(r)
    if (o.id !== value) onChange(o.id)
  }
  const onUp = () => setDrag(null)
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (disabled) return
    const step = e.key === 'ArrowRight' || e.key === 'ArrowUp' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? -1 : 0
    const to = e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : idx + step
    if (to === idx || (!step && e.key !== 'Home' && e.key !== 'End')) return
    e.preventDefault()
    onChange(options[Math.min(n - 1, Math.max(0, to))].id)
  }

  return (
    <div className={`step-slider${compact ? ' compact' : ''}${disabled ? ' disabled' : ''}${drag !== null ? ' dragging' : ''}`}>
      <div
        ref={track}
        className="step-track"
        role="slider"
        tabIndex={disabled ? -1 : 0}
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={n - 1}
        aria-valuenow={idx}
        aria-valuetext={options[idx]?.label}
        aria-disabled={disabled || undefined}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        onKeyDown={onKey}
      >
        <div className="step-rail" />
        <div className="step-fill" style={{ width: `${pos * 100}%` }} />
        {options.map((o, i) => (
          <span key={o.id} className={`step-dot${i <= idx ? ' on' : ''}`} style={{ left: `${n > 1 ? (i / (n - 1)) * 100 : 0}%` }} />
        ))}
        <span className="step-thumb" style={{ left: `${pos * 100}%` }} />
      </div>
      <div className="step-labels" style={{ gridTemplateColumns: `repeat(${n}, 1fr)` }}>
        {options.map((o) => (
          <button key={o.id} type="button" tabIndex={-1} disabled={disabled} className={o.id === value ? 'on' : ''}
            title={o.hint} onClick={() => onChange(o.id)}>{o.label}</button>
        ))}
      </div>
    </div>
  )
}
