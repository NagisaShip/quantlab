/** 对话里的基础块：用户气泡、AI 正文（含可折叠思考）、一行工具调用、事件分隔。 */
import { useEffect, useState, type ReactNode } from 'react'
import { Markdown } from '../markdown'
import { modelLabel } from './Composer'
import { QuantBot, QuantEyes, type BotState } from '../QuantBot'

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
        {live && <QuantEyes size={15} state={(ms || 0) / 1000 >= STRAIN_AFTER_S ? 'strain' : 'thinking'} className="cb-working-eyes" />}
        <span>{live ? ((ms || 0) / 1000 >= STRAIN_AFTER_S ? '正在努力思考' : '正在思考') : `思考了 ${secs} 秒`}</span>
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
      {content && <div className={`cb-ai-text${streaming ? ' streaming' : ''}`}><Markdown text={content} compact streaming={streaming} /></div>}
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

/** 努力思考：同一阶段持续超过这么多秒，Bot 换成「全力」（> <），让人感知到它在硬啃。 */
export const STRAIN_AFTER_S = 20

/**
 * 阶段文字 + 已用时间 → Bot 状态：
 * - 模型调用失败、重试、缺数据：困惑；
 * - 多轮迭代、子 Agent 并行、压力测试、计划连续执行，或一个阶段超过 20 秒：全力（> <）；
 * - 回测 / 评估 / 训练 / 仿真 / 同步数据 / 联网搜索：专注（Tick 跳动）；
 * - 其余（读上下文、推理、写回答）：思考。
 */
export function botStateOf(phase?: string, secs?: number | null): BotState {
  if (!phase) return 'thinking'
  if (/失败|重试|出错|未接入|缺少/.test(phase)) return 'confused'
  if (/第\s*\d+\s*轮|迭代|冲刺|并行|子 Agent|压力|执行计划/.test(phase)) return 'strain'
  if ((secs ?? 0) >= STRAIN_AFTER_S) return 'strain'
  if (/回测|评估|训练|仿真|运行|试跑|同步|搜索|读网页|诊断|行情/.test(phase)) return 'focus'
  return 'thinking'
}

const STATE_WORD: Partial<Record<BotState, string>> = { strain: '努力思考', focus: '专注计算', confused: '遇到问题', thinking: '思考中' }

/** 等待模型时的占位：Bot（状态随阶段与用时变化）+「正在做什么」+ 补充说明，右侧是已用时间。 */
export function Working({ phase, detail, startedAt, onStop, state, size = 26 }: {
  phase?: string; detail?: string; startedAt?: number; onStop?: () => void; state?: BotState; size?: number
}) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!startedAt) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [startedAt])
  const secs = startedAt ? Math.max(0, Math.round((now - (startedAt < 1e12 ? startedAt * 1000 : startedAt)) / 1000)) : null
  const st = state ?? botStateOf(phase, secs)
  return (
    <div className={`cb-working st-${st}`}>
      <QuantBot size={size} state={st} title={phase || '处理中'} className="cb-working-bot lively" />
      <span className={`cb-state st-${st}`} key={st}>{STATE_WORD[st] ?? ''}</span>
      <div className="cb-working-text">
        <span className="cb-shimmer" key={phase}>{phase || '处理中'}…</span>
        {detail && <small className="cb-working-detail anim-fade" key={detail}>{detail}</small>}
      </div>
      {secs !== null && <span className="cb-working-time">{secs >= 60 ? `${Math.floor(secs / 60)} 分 ${secs % 60} 秒` : `${secs} 秒`}</span>}
      {onStop && <button type="button" className="link" onClick={onStop}>停止</button>}
    </div>
  )
}
