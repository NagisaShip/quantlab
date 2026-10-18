import type { QmojiReaction } from '../apiCollab'
import { Qmoji, QmojiPicker, QUICK_QMOJI, qmojiByCode, qmojiName } from './Qmoji'

/**
 * 快捷 Qmoji Reaction：帖子、回测分享与群聊消息共用。
 * 一排最常用的 5 枚 + 「更多」打开完整 Qmoji 面板；点击切换本人反应。
 */
export function QmojiReactionBar({ reactions, onToggle, compact, disabled, className }: {
  reactions: QmojiReaction[]
  onToggle: (emoji: string) => void | Promise<void>
  compact?: boolean
  disabled?: boolean
  className?: string
}) {
  const byCode = new Map(reactions.map((r) => [r.emoji, r]))
  const quick = QUICK_QMOJI.map((id) => `:qbot-${id}:`)
  const extra = reactions
    .filter((r) => !quick.includes(r.emoji) && !!qmojiByCode(r.emoji))
    .map((r) => r.emoji)
  const codes = [...quick, ...extra]
  return (
    <div className={`qmoji-reaction-bar${compact ? ' compact' : ''}${className ? ' ' + className : ''}`}
      onClick={(e) => e.stopPropagation()}>
      {codes.map((code) => {
        const q = qmojiByCode(code)
        if (!q) return null
        const r = byCode.get(code)
        return (
          <button key={code} type="button" className={`qmoji-reaction${r?.mine ? ' on' : ''}`} disabled={disabled}
            title={`${qmojiName(q.id)} · ${code}`} aria-label={qmojiName(q.id)} aria-pressed={!!r?.mine} onClick={() => onToggle(code)}>
            <Qmoji id={q.id} size={compact ? 16 : 18} animated={r?.mine ? 'hover' : false} />
            {r?.count ? <span>{r.count}</span> : null}
          </button>
        )
      })}
      <QmojiPicker onlyQmoji label="更多" title="更多 Qmoji" onPick={onToggle} />
    </div>
  )
}
