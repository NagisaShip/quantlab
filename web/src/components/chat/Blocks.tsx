/** 对话里的基础块：用户气泡、AI 正文（含可折叠思考）、一行工具调用、事件分隔。 */
import { useState, type ReactNode } from 'react'
import { Markdown } from '../markdown'
import { modelLabel } from './Composer'

export function UserBubble({ children }: { children: ReactNode }) {
  return <div className="cb-user"><div className="cb-user-bubble">{children}</div></div>
}

/** 思考过程：流式输出时展开显示末尾，结束后折叠为「思考了 N 秒」。 */
export function Thinking({ text, ms, live }: { text: string; ms?: number; live?: boolean }) {
  const [open, setOpen] = useState(false)
  if (!text && !live) return null
  const secs = Math.max(1, Math.round((ms || 0) / 1000))
  return (
    <div className={`cb-think${live ? ' live' : ''}`}>
      <button type="button" className="cb-think-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span>{live ? '正在思考' : `思考了 ${secs} 秒`}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden className={open ? 'up' : ''}><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
      </button>
      {(open || live) && text && (
        <div className="cb-think-body">{live && !open ? text.slice(-600) : text}</div>
      )}
    </div>
  )
}

export function AssistantText({ content, thinking, thinkingMs, streaming, model, fallback, footer }: {
  content: string; thinking?: string; thinkingMs?: number; streaming?: boolean; model?: string; fallback?: boolean; footer?: ReactNode
}) {
  const thinkingLive = !!streaming && !content
  return (
    <div className="cb-ai">
      {(thinking || thinkingLive) && <Thinking text={thinking || ''} ms={thinkingMs} live={thinkingLive} />}
      {content && <div className={`cb-ai-text${streaming ? ' streaming' : ''}`}><Markdown text={content} compact /></div>}
      {!streaming && (model || fallback || footer) && (
        <div className="cb-ai-meta">
          {fallback ? <span className="cb-badge warn">规则降级</span> : model ? <span>{modelLabel(model)}</span> : null}
          {footer}
        </div>
      )}
    </div>
  )
}

export function ToolLine({ icon, label, args, status, elapsedMs, detail }: {
  icon?: string; label: string; args?: Record<string, unknown>; status: 'running' | 'ok' | 'error' | string
  elapsedMs?: number; detail?: string
}) {
  const [open, setOpen] = useState(false)
  const argText = Object.entries(args || {}).map(([k, v]) => `${k} ${String(v)}`).join(' · ')
  return (
    <div className={`cb-tool ${status}`}>
      <button type="button" className="cb-tool-row" onClick={() => detail && setOpen(!open)} aria-expanded={open} disabled={!detail}>
        <span className="cb-tool-icon" aria-hidden>{status === 'running' ? <span className="cb-spin" /> : icon ?? '•'}</span>
        <span className="cb-tool-label">{label}</span>
        {argText && <span className="cb-tool-args">{argText}</span>}
        <span className="cb-tool-end">
          {status === 'error' ? '失败' : status === 'ok' && elapsedMs !== undefined ? `${(elapsedMs / 1000).toFixed(1)}s` : ''}
        </span>
      </button>
      {open && detail && <pre className="cb-tool-detail">{detail}</pre>}
    </div>
  )
}

export function EventLine({ children, tone }: { children: ReactNode; tone?: 'ok' | 'error' | 'warn' }) {
  return <div className={`cb-event${tone ? ' ' + tone : ''}`}><span>{children}</span></div>
}

/** 等待模型时的占位：一行轻微闪动的阶段文字。 */
export function Working({ phase, onStop }: { phase?: string; onStop?: () => void }) {
  return (
    <div className="cb-working">
      <span className="cb-shimmer">{phase || '处理中'}…</span>
      {onStop && <button type="button" className="link" onClick={onStop}>停止</button>}
    </div>
  )
}
