/**
 * AskAI 顶部的研究上下文条：只显示身份与可移除的输出引用，不渲染第二套对话 UI。
 * 身份（工作区/研究/节点/运行）始终可见；输出证据可以按次选择或移除。
 */
import type { AskResearchContext, AskResearchNode, AskResearchOutputRef } from '../apiAskAI'

const WORDS: Record<string, string> = {
  not_run: '未运行', running: '运行中', completed: '完成', cached: '复用缓存', pinned: '调试固定',
  failed: '失败', error: '失败', skipped: '未执行', blocked: '上游阻断', canceled: '已取消',
  QUEUED: '排队中', PENDING: '等待中', RUNNING: '运行中', RECOVERING: '恢复中', PAUSED: '已暂停',
  COMPLETED: '已完成', FAILED: '失败', CANCELED: '已取消', UNKNOWN: '未知',
}

const word = (value?: string) => WORDS[value || ''] || value || '未运行'
const TYPE_WORDS: Record<string, string> = {
  table: '表格', chart: '图表', model: '模型', network: '网络', json: 'JSON', text: '文本', file: '文件',
}
const hashPreview = (hash: string) => hash.length > 14 ? `${hash.slice(0, 8)}…${hash.slice(-4)}` : hash
export const outputKey = (ref: AskResearchOutputRef) => `${ref.run_id}:${ref.node_id}:${ref.port}:${ref.hash}`
const outputLabel = (ref: AskResearchOutputRef) => `${ref.port} · ${TYPE_WORDS[ref.type] || ref.type}`

export default function ResearchContextStrip({ research, attached, hasCode = false, onToggle, onReference }: {
  research: AskResearchContext
  attached: AskResearchOutputRef[]
  hasCode?: boolean
  onToggle: (ref: AskResearchOutputRef) => void
  onReference: (node: AskResearchNode) => void
}) {
  const workflow = research.workflow
  const selected = research.selected_node || null
  const run = research.run || null
  const available = research.outputs || []
  const selectedKeys = new Set(attached.map(outputKey))
  const project = research.project_name || research.workspace?.project || '当前工作区'
  const revision = workflow.revision ? `r${workflow.revision}` : '草稿'
  const runTone = run ? (run.current ? 'current' : 'historical') : 'none'
  const runText = run
    ? `${run.current ? '当前运行' : '冻结历史运行'} · ${word(run.status)}`
    : '未关联运行'
  return (
    <section className="ask-research" aria-label="研究上下文">
      <div className="ask-research-ident">
        <span className="ask-research-kicker">研究上下文</span>
        <b className="ask-research-title" title={workflow.title}>{workflow.title || '未命名研究'}</b>
        <span className="ask-research-ws" title={project}>{project}</span>
      </div>
      <div className="ask-research-meta">
        <span className="ask-chip" title={workflow.id ? `研究 ID：${workflow.id}` : '尚未保存为研究'}>{workflow.id ? '已保存研究' : '草稿'} · {revision}</span>
        <span className={`ask-chip${workflow.unsaved ? ' warn' : ''}`} title={workflow.unsaved ? '画布修改尚未保存；它们不是运行结果' : '当前研究已保存'}>
          {workflow.unsaved ? '有未保存修改' : '已保存'}
        </span>
        <span className={`ask-chip ask-run ${runTone}`} title={run ? `运行 ${run.id} · ${run.mode || 'research'} · 修订 ${run.revision ? `r${run.revision}` : '未知'}` : '尚未关联真实运行'}>
          {runText}
        </span>
        {hasCode && <span className="ask-chip" title="选中节点的代码会作为本轮参考资料发送">含节点代码</span>}
        {selected && (
          <span className="ask-node-wrap">
            <span className="ask-chip ask-node" title={`${selected.title} · ${selected.kind} · ${selected.id}`}>
              节点 {selected.title || selected.id} · {word(selected.status)}
            </span>
            <button type="button" className="ask-ref-btn" onClick={() => onReference(selected)}
              title="把选中节点写进本次问题" aria-label={`在问题中引用节点 ${selected.title || selected.id}`}>引用</button>
          </span>
        )}
      </div>
      <div className="ask-research-evidence" role="group" aria-label="输出证据引用">
        <span className="ask-evidence-label">证据 {attached.length}/{available.length}</span>
        {attached.map((ref) => (
          <button key={outputKey(ref)} type="button" className="ask-ref"
            onClick={() => onToggle(ref)}
            title={`${outputLabel(ref)} · ${ref.node_id} · ${ref.hash}`}
            aria-label={`移除输出引用 ${outputLabel(ref)}，节点 ${ref.node_id}`}>
            <span>{outputLabel(ref)}</span><span aria-hidden className="ask-ref-x">×</span>
          </button>
        ))}
        {!attached.length && <span className="ask-evidence-none">无输出证据</span>}
        {available.length > 0 && (
          <details className="ask-ref-picker">
            <summary title="选择随本次提问发送的输出引用">选择输出</summary>
            <div className="ask-ref-pop" role="group" aria-label="可用输出引用">
              {available.map((ref) => {
                const on = selectedKeys.has(outputKey(ref))
                return (
                  <button key={outputKey(ref)} type="button" className={`ask-ref-option${on ? ' on' : ''}`}
                    aria-pressed={on} onClick={() => onToggle(ref)}
                    title={`${ref.node_id} · ${ref.port} · ${ref.type} · ${ref.hash}`}>
                    <span aria-hidden>{on ? '✓' : '+'}</span>
                    <span>{outputLabel(ref)}</span>
                    <small>{hashPreview(ref.hash)}</small>
                  </button>
                )
              })}
            </div>
          </details>
        )}
      </div>
    </section>
  )
}
