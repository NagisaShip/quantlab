/**
 * 角色头像：几何风人物（头 + 肩），用角色色区分，再加一个职责小配件
 * （分析师的眼镜、风控官的盾牌、反方的小角……）。纯 SVG，深浅主题都清楚。
 */
import { openRoleEditor, useRoleBook } from './roles'

type Persona = { color: string; hair: string; acc: string; short: string }

export const PERSONAS: Record<string, Persona> = {
  market_analyst: { color: '#3b6fd8', hair: '#2a2f45', acc: 'glasses', short: '析' },
  strategy_designer: { color: '#7b5ad6', hair: '#3a2a20', acc: 'pencil', short: '策' },
  quant_engineer: { color: '#1a9a8a', hair: '#1f2433', acc: 'headset', short: '码' },
  backtest_analyst: { color: '#c98a17', hair: '#4a3020', acc: 'chart', short: '测' },
  risk_officer: { color: '#d0443e', hair: '#22262f', acc: 'shield', short: '险' },
  devil_advocate: { color: '#8d2f52', hair: '#16161d', acc: 'horns', short: '辩' },
  compliance_officer: { color: '#5f6b80', hair: '#2b2b33', acc: 'tie', short: '规' },
  research_lead: { color: '#2e45c8', hair: '#3b3b46', acc: 'star', short: '长' },
  execution_specialist: { color: '#1d8f5a', hair: '#2d2218', acc: 'cap', short: '执' },
  commander: { color: '#b88a1b', hair: '#1b2236', acc: 'commander', short: '令' },
  ai: { color: '#2e45c8', hair: '#2e45c8', acc: 'bot', short: 'AI' },
}

const FALLBACK = ['#3b6fd8', '#7b5ad6', '#1a9a8a', '#c98a17', '#d0443e', '#1d8f5a', '#5f6b80']

export function personaOf(id: string | undefined): Persona {
  if (id && PERSONAS[id]) return PERSONAS[id]
  let h = 0
  for (const ch of id || '?') h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return { color: FALLBACK[h % FALLBACK.length], hair: '#2a2f45', acc: '', short: '' }
}

/**
 * 角色头像。传入的是角色库里的角色时，点击会打开角色编辑抽屉（模型、努力程度、提示词……）；
 * editable={false} 关闭这个行为（比如头像本身在一个更大的可点击区域里）。
 */
export function Avatar({ id, size = 36, speaking, title, editable = true }: {
  id?: string; size?: number; speaking?: boolean; title?: string; editable?: boolean
}) {
  const { byId } = useRoleBook()
  const role = id ? byId[id] : undefined
  if (!role || !editable) return <AvatarFace id={id} avatar={role?.avatar} size={size} speaking={speaking} title={title ?? role?.name} />
  return (
    <span role="button" tabIndex={0} className="avatar-btn" title={`${title ?? role.name} · 点击编辑角色`}
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); openRoleEditor(role.id) }}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); openRoleEditor(role.id) } }}>
      <AvatarFace id={id} avatar={role.avatar} size={size} speaking={speaking} />
    </span>
  )
}

export function AvatarFace({ id, avatar, size = 36, speaking, title }: { id?: string; avatar?: string; size?: number; speaking?: boolean; title?: string }) {
  const p = personaOf(id)
  if (avatar) {
    const emoji = /\p{Extended_Pictographic}/u.test(avatar)
    return (
      <span className={`avatar-svg avatar-text${speaking ? ' speaking' : ''}${emoji ? ' emoji' : ''}`} title={title}
        style={{ width: size, height: size, fontSize: Math.round(size * (emoji ? 0.56 : 0.42)), ['--persona' as string]: p.color }}>
        {avatar}
      </span>
    )
  }
  return (
    <span className={`avatar-svg${speaking ? ' speaking' : ''}`} style={{ width: size, height: size, ['--persona' as string]: p.color }} title={title}>
      <svg viewBox="0 0 40 40" width={size} height={size} aria-hidden="true">
        <circle cx="20" cy="20" r="20" fill={p.color} />
        {p.acc === 'bot' ? <Bot /> : (
          <>
            {/* 肩 */}
            <path d="M7 40c0-8 5.8-13 13-13s13 5 13 13z" fill="#fff" opacity=".92" />
            {p.acc === 'tie' && <path d="M20 28l-1.6 2.4L20 38l1.6-7.6z" fill={p.color} />}
            {p.acc === 'commander' && <path d="M12 31.5h16" stroke={p.color} strokeWidth="2" />}
            {/* 头 */}
            <circle cx="20" cy="17" r="7.4" fill="#f3d2b5" />
            {/* 发型 */}
            <path d="M12.6 16.4c0-4.6 3.3-7.6 7.4-7.6s7.4 3 7.4 7.6c-1.6-2.4-4.3-3.6-7.4-3.6s-5.8 1.2-7.4 3.6z" fill={p.hair} />
            <Accessory kind={p.acc} color={p.color} />
          </>
        )}
      </svg>
    </span>
  )
}

function Bot() {
  return (
    <g fill="#fff">
      <rect x="11" y="13" width="18" height="15" rx="5" />
      <circle cx="16.5" cy="20.5" r="1.8" fill="#2e45c8" />
      <circle cx="23.5" cy="20.5" r="1.8" fill="#2e45c8" />
      <rect x="19" y="8" width="2" height="5" rx="1" />
      <circle cx="20" cy="8" r="1.6" />
    </g>
  )
}

function Accessory({ kind, color }: { kind: string; color: string }) {
  switch (kind) {
    case 'glasses':
      return (
        <g fill="none" stroke="#1d2233" strokeWidth="1.1">
          <circle cx="17" cy="17.6" r="2.1" /><circle cx="23" cy="17.6" r="2.1" /><path d="M19.1 17.6h1.8" />
        </g>
      )
    case 'pencil':
      return <path d="M28.5 25.5l5-9 1.6.9-5 9-2 .9z" fill="#fff" stroke="#1d2233" strokeWidth=".7" />
    case 'headset':
      return (
        <g fill="none" stroke="#1d2233" strokeWidth="1.4">
          <path d="M12 18a8 8 0 0 1 16 0" /><rect x="10.6" y="16.6" width="2.6" height="4.4" rx="1" fill="#1d2233" />
          <rect x="26.8" y="16.6" width="2.6" height="4.4" rx="1" fill="#1d2233" />
        </g>
      )
    case 'chart':
      return (
        <g>
          <rect x="26" y="26" width="10" height="8" rx="1.5" fill="#fff" stroke={color} strokeWidth=".8" />
          <path d="M27.5 32.5l2.4-2.6 2 1.4 2.6-3.2" fill="none" stroke={color} strokeWidth="1.1" />
        </g>
      )
    case 'shield':
      return <path d="M31 25l4.5 1.6v3.4c0 2.8-2 4.6-4.5 5.6-2.5-1-4.5-2.8-4.5-5.6v-3.4z" fill="#fff" stroke={color} strokeWidth="1" />
    case 'horns':
      return <g fill="#1d2233"><path d="M13.4 11.6l-1.6-4 3.4 2.6z" /><path d="M26.6 11.6l1.6-4-3.4 2.6z" /></g>
    case 'star':
      return <path d="M20 4.6l1.2 2.5 2.7.4-2 1.9.5 2.7-2.4-1.3-2.4 1.3.5-2.7-2-1.9 2.7-.4z" fill="#ffd34d" />
    case 'cap':
      return <path d="M12.4 14.2c.6-3.6 3.8-5.8 7.6-5.8s7 2.2 7.6 5.8zM26 13.6h4.6v1.4H26z" fill="#1d2233" />
    case 'commander':
      return <path d="M11.8 12.6c1.4-3 4.6-4.8 8.2-4.8s6.8 1.8 8.2 4.8zM18.6 9.4l1.4-1.4 1.4 1.4-1.4 1.4z" fill="#1b2236" />
    default:
      return null
  }
}
