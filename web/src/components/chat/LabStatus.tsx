/** P4 · Agent 五态与运行证据的统一展示组件（页头 / 消息 / 侧栏共用）。 */
import type { AgentStatus, MessageTag, RunEvidence, RunEvidenceState } from './status'
import { RUN_EVIDENCE_LABEL, RUN_STATE_LABEL, evidenceLabel } from './status'
import './labStatus.css'

/** 页头状态药丸：等待输入 / 排队中 / 生成中 / 已生成 / 执行失败。 */
export function AgentStatePill({ status, compact = false }: { status: AgentStatus; compact?: boolean }) {
  const title = status.detail ? `${status.label} · ${status.detail}` : status.label
  return (
    <span className={`lab-state-pill ${status.state}${compact ? ' compact' : ''}`} data-state={status.state}
      data-label={status.label} role="status" title={title}>
      <i className="lab-state-dot" aria-hidden />
      <span className="lab-state-text">{status.label}</span>
      {status.state === 'generated' && status.artifacts && status.evidence.length > 0 && (
        <em className="lab-state-count" aria-label={`${status.evidence.length} 条运行证据`}>{status.evidence.length}</em>
      )}
    </span>
  )
}

/** 运行证据标签：id 可见、可跳转；没有 href 的只读证据也显示，便于核对。 */
export function EvidenceChips({ items, live, max = 6 }: {
  items: RunEvidence[]
  live?: Record<string, RunEvidenceState>
  max?: number
}) {
  if (!items.length) return null
  const shown = items.slice(-Math.max(1, max))
  return (
    <span className="lab-evidence" aria-label="运行证据">
      {shown.map((item) => {
        const state = live?.[item.key] ?? item.state
        const label = evidenceLabel(item)
        const title = `${item.id} · ${RUN_STATE_LABEL[state]}`
        return item.href
          ? <a key={item.key} className={`lab-evidence-chip ${state}`} href={item.href} title={title}>{label}</a>
          : <span key={item.key} className={`lab-evidence-chip ${state}`} title={title}>{label}</span>
      })}
      {items.length > shown.length && <span className="lab-evidence-more">+{items.length - shown.length}</span>}
    </span>
  )
}

/** 消息气泡上的状态标签（已生成 / 已回复 / 生成中 / 执行失败 / 已停止）。 */
export function MessageStateTag({ tag, live }: { tag: MessageTag; live?: Record<string, RunEvidenceState> }) {
  return (
    <span className={`lab-msg-state ${tag.tone}`} data-tone={tag.tone} role="note"
      title={tag.tone === 'replied' ? '本轮没有 run / 工件证据，只是文字回复'
        : tag.tone === 'submitted' && tag.label === '已提交' ? '运行已提交，仍在排队或运行中' : undefined}>
      <span className="lab-msg-state-label">{tag.label}</span>
      {tag.tone === 'replied' && <span className="lab-msg-state-hint">无运行证据</span>}
      <EvidenceChips items={tag.evidence} live={live} />
    </span>
  )
}

/** 消息里出现的运行证据摘要（供卡片 / 工具结果复用）。 */
export function EvidenceLine({ items, live }: { items: RunEvidence[]; live?: Record<string, RunEvidenceState> }) {
  if (!items.length) return null
  return (
    <div className="lab-evidence-line">
      <span className="lab-evidence-line-label">{RUN_EVIDENCE_LABEL[items[items.length - 1].kind]}绑定</span>
      <EvidenceChips items={items} live={live} />
    </div>
  )
}
