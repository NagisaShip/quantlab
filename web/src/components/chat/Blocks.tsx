/** 对话里的基础块：用户气泡、AI 正文（含可折叠思考）、一行工具调用、事件分隔。 */
import { useEffect, useState, type ReactNode } from 'react'
import { Markdown } from '../markdown'
import { modelLabel } from './Composer'
import { QuantEyes, type BotState } from '../QuantBot'

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
  return (
    <div className={`cb-event${tone ? ' ' + tone : ''}`}>
      <span>{tone === 'error' && <QuantEyes size={15} state="error" motion="low" title="出错了" />}{children}</span>
    </div>
  )
}

/** 阶段文字 → Bot 状态：跑回测 / 训练 / 仿真是「专注」，多轮迭代冲刺是「全力」，其余是「思考」。 */
export function botStateOf(phase?: string): BotState {
  if (!phase) return 'thinking'
  if (/第\s*\d+\s*轮|迭代|冲刺/.test(phase)) return 'strain'
  if (/回测|评估|训练|仿真|运行|试跑|压力/.test(phase)) return 'focus'
  return 'thinking'
}

/** 等待模型时的占位：一行轻微闪动的「正在做什么」，下面一行补充说明（参数、步数），右侧是已用时间。 */
export function Working({ phase, detail, startedAt, onStop }: { phase?: string; detail?: string; startedAt?: number; onStop?: () => void }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!startedAt) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [startedAt])
  const secs = startedAt ? Math.max(0, Math.round((now - (startedAt < 1e12 ? startedAt * 1000 : startedAt)) / 1000)) : null
  return (
    <div className="cb-working">
      <QuantEyes size={20} state={botStateOf(phase)} title={phase || '处理中'} className="cb-working-eyes" />
      <div className="cb-working-text">
        <span className="cb-shimmer" key={phase}>{phase || '处理中'}…</span>
        {detail && <small className="cb-working-detail anim-fade" key={detail}>{detail}</small>}
      </div>
      {secs !== null && <span className="cb-working-time">{secs >= 60 ? `${Math.floor(secs / 60)} 分 ${secs % 60} 秒` : `${secs} 秒`}</span>}
      {onStop && <button type="button" className="link" onClick={onStop}>停止</button>}
    </div>
  )
}
