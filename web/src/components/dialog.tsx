/**
 * 应用内确认 / 输入对话框，替代 window.confirm / window.prompt。
 *
 * 为什么不用原生对话框：在桌面应用内嵌的浏览器、部分 WebView 和被浏览器拦截的情况下，
 * window.confirm 会直接返回 false、window.prompt 直接返回 null，且不弹任何东西——
 * 用户点「删除」后什么都没发生（对话删不掉就是这个原因）。这里的对话框在任何环境都能显示，
 * 也跟平台的动效和 Bot 视觉统一。
 *
 * 用法：if (!(await appConfirm('删除？', { danger: true }))) return
 *       const name = await appPrompt('新名称', '默认值')   // 取消返回 null
 */
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { QuantBot } from './QuantBot'

interface Req {
  id: number
  kind: 'confirm' | 'prompt'
  message: string
  title?: string
  okLabel?: string
  danger?: boolean
  value?: string
  placeholder?: string
  resolve: (v: boolean | string | null) => void
}

let queue: Req[] = []
let seq = 0
const subs = new Set<() => void>()
const emit = () => subs.forEach((f) => f())

function open(r: Omit<Req, 'id' | 'resolve'>): Promise<boolean | string | null> {
  return new Promise((resolve) => {
    queue = [...queue, { ...r, id: ++seq, resolve }]
    emit()
  })
}

/** 确认框：返回是否确认。删除类操作传 danger: true（红色按钮）。 */
export function appConfirm(message: string, opts: { title?: string; okLabel?: string; danger?: boolean } = {}): Promise<boolean> {
  const danger = opts.danger ?? /删除|撤销|移出|解散|停止|擦除|退回|解除/.test(message)
  return open({ kind: 'confirm', message, danger, ...opts }) as Promise<boolean>
}

/** 输入框：返回输入的文字（未修剪），取消返回 null。 */
export function appPrompt(message: string, value = '', opts: { title?: string; okLabel?: string; placeholder?: string } = {}): Promise<string | null> {
  return open({ kind: 'prompt', message, value, ...opts }) as Promise<string | null>
}

function done(r: Req, v: boolean | string | null) {
  queue = queue.filter((x) => x.id !== r.id)
  emit()
  r.resolve(v)
}

/** 挂在应用根部一次即可。 */
export function DialogHost() {
  const items = useSyncExternalStore((f) => { subs.add(f); return () => { subs.delete(f) } }, () => queue)
  const r = items[0]
  if (!r) return null
  return createPortal(<DialogView key={r.id} r={r} />, document.body)
}

function DialogView({ r }: { r: Req }) {
  const [value, setValue] = useState(r.value ?? '')
  const ok = useRef<HTMLButtonElement>(null)
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (r.kind === 'prompt') { input.current?.focus(); input.current?.select() } else ok.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); done(r, r.kind === 'prompt' ? null : false) } }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [r])
  const cancel = () => done(r, r.kind === 'prompt' ? null : false)
  const accept = () => done(r, r.kind === 'prompt' ? value : true)
  const lines = r.message.split('\n')
  return (
    <div className="modal-backdrop app-dialog-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) cancel() }}>
      <div className="modal app-dialog" role={r.kind === 'confirm' ? 'alertdialog' : 'dialog'} aria-modal="true" aria-label={r.title ?? lines[0]}>
        <div className="app-dialog-body">
          <QuantBot size={34} state={r.danger ? 'confused' : 'idle'} motion="low" />
          <div className="app-dialog-text">
            {r.title && <b>{r.title}</b>}
            {lines.map((l, i) => (l ? <p key={i} className={i === 0 && !r.title ? 'lead' : undefined}>{l}</p> : <div key={i} className="gap" />))}
            {r.kind === 'prompt' && (
              <input ref={input} value={value} placeholder={r.placeholder} onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); accept() } }} />
            )}
          </div>
        </div>
        <div className="app-dialog-foot">
          <button type="button" className="button" onClick={cancel}>取消</button>
          <button ref={ok} type="button" className={`button ${r.danger ? 'danger-solid' : 'primary'}`} onClick={accept}>
            {r.okLabel ?? (r.danger ? '确定' : r.kind === 'prompt' ? '确定' : '确定')}
          </button>
        </div>
      </div>
    </div>
  )
}
