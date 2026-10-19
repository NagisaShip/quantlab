import type { CSSProperties } from 'react'
import { QuantBot } from './QuantBot'

export const AVATAR_COLORS = [
  { color: '#2E5C9A', label: '量化蓝' },
  { color: '#236B68', label: '研究青' },
  { color: '#6A5ACD', label: '模型紫' },
  { color: '#A26A10', label: '琥珀金' },
  { color: '#B42318', label: '风控红' },
  { color: '#3F4A3C', label: '石墨绿' },
]

export function UserAvatar({ color = '#2E5C9A', size = 30, title, src, className = '' }: {
  src?: string
  color?: string
  size?: number
  title?: string
  className?: string
}) {
  const style = {
    '--qb-head': color,
    '--qb-head-hi': color,
    width: size,
    height: size,
  } as CSSProperties
  return (
    <span className={`user-avatar ${className}`.trim()} style={style} title={title}>
      {src ? <img src={src} alt={title || '用户头像'} style={{ width: '100%', height: '100%', objectFit: 'cover' }} /> : <QuantBot size={Math.round(size * .7)} state="idle" motion="none" variant={size >= 32 ? 'full' : 'simple'} title={title || 'QuantLab Bot 用户头像'} />}
    </span>
  )
}

export function AvatarColorPicker({ value, onChange }: { value: string; onChange: (color: string) => void }) {
  return (
    <div className="avatar-color-picker">
      <div className="avatar-color-presets" role="radiogroup" aria-label="头像颜色预设">
        {AVATAR_COLORS.map((c) => (
          <button key={c.color} type="button" role="radio" aria-checked={value.toLowerCase() === c.color.toLowerCase()}
            className={`avatar-color-swatch${value.toLowerCase() === c.color.toLowerCase() ? ' on' : ''}`}
            style={{ background: c.color }} title={c.label} aria-label={c.label} onClick={() => onChange(c.color)} />
        ))}
      </div>
      <label className="avatar-color-custom">
        <span>自定义</span>
        <input type="color" value={value} onChange={(e) => onChange(e.target.value)} aria-label="自定义头像颜色" />
        <code>{value.toUpperCase()}</code>
      </label>
    </div>
  )
}
