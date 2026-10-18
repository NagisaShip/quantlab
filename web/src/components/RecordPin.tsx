import { useEffect, useState } from 'react'
import { v3 } from '../api/research'
import type { PageProps } from '../pages/types'

/** 研究流程页面上的快速 PIN：不离开当前列表/详情，直接保护记录不被自动清理或删除。 */
export function RecordPinButton({ type, id, pinned = false, notify, onChanged, className = '' }: {
  type: string
  id: string
  pinned?: boolean
  notify: PageProps['notify']
  onChanged?: (pinned: boolean) => void
  className?: string
}) {
  const [on, setOn] = useState(Boolean(pinned))
  const [busy, setBusy] = useState(false)
  useEffect(() => { setOn(Boolean(pinned)) }, [pinned])
  const toggle = async () => {
    setBusy(true)
    try {
      const r = await v3.pinRetentionRecord(type, id)
      setOn(Boolean(r.pinned))
      onChanged?.(Boolean(r.pinned))
      notify(r.pinned ? '已 PIN，自动清理不会删除这条记录' : '已取消 PIN')
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  return (
    <button
      type="button"
      className={`button small${on ? ' on' : ''}${className ? ` ${className}` : ''}`}
      disabled={busy}
      title={on ? '取消 PIN 保护' : 'PIN 保护，自动清理时不会删除'}
      onClick={(e) => { e.stopPropagation(); void toggle() }}
    >
      {busy ? '…' : on ? '取消 PIN' : 'PIN'}
    </button>
  )
}
