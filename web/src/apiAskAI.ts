/**
 * 页面内「问 AI」接口：一次性问答、流式问答（POST + SSE）、面板元信息。
 * 流式接口是 POST（要带页面资料与历史），EventSource / openStream 只能 GET，这里用 fetch 自己读流。
 */
import { ApiError, get, post, targetWorkspace } from './http'
import type { Effort, EffortOption, LabView, ModelOption } from './apiLab'

/** 页面附带的一份资料：服务端会截断，并且只当作参考数据。 */
export interface AskContextItem { kind: string; title: string; text: string }
export interface AskTurn { role: 'user' | 'assistant'; content: string }

/** 可选、版本化的研究上下文。只含身份与输出引用，不内联图/数据集/二进制。 */
export interface AskResearchOutputRef { port: string; type: string; hash: string; run_id: string; node_id: string }
export interface AskResearchNode { id: string; title: string; kind: string; status: string; verified?: boolean }
export interface AskResearchRun {
  id: string; mode?: string; status?: string; revision?: number | null
  current?: boolean; historical?: boolean
  /** 显式显示意图；服务端会与真实 latest run 对照后再决定 canonical view。 */
  follows_latest?: boolean | null; view_mode?: 'latest' | 'frozen' | null
  is_latest?: boolean; latest_run_id?: string | null
}
export interface AskResearchContext {
  version: 1
  workspace_id?: string
  project_name?: string
  draft_id?: string
  workflow: { id: string; title: string; revision?: number | null; saved_revision?: number | null; saved: boolean; unsaved: boolean; saved_title?: string }
  selected_node?: AskResearchNode | null
  run?: AskResearchRun | null
  outputs: AskResearchOutputRef[]
  /** 服务端校验后返回的 canonical 身份；请求时忽略。 */
  workspace?: { id: string; project: string }
  format?: string
  limits?: { max_outputs: number }
}

export interface AskIn {
  question: string
  history: AskTurn[]
  context: AskContextItem[]
  model?: string
  effort?: Effort | ''
  /** 当前页面：后端据此默认加载哪些平台工具 */
  page?: string
  /** 「更多」菜单里勾选的工具场景；不传 = 按页面默认 */
  scenes?: string[] | null
  /** 可选研究上下文；非工作流页面不传，保持旧行为 */
  research?: AskResearchContext | null
}

/** 一次平台工具调用（需要确认的动作带 action_id，状态 pending）。 */
export interface ToolRecord {
  task_view?: import('./features/workflows/TaskResult').TaskView
  artifact_view?: import('./features/workflows/ArtifactPreview').ArtifactView
  workflow_view?: import('./components/chat/WorkflowTrace').WorkflowView
  tool: string; title: string; risk: 'read' | 'run' | 'confirm'; args: Record<string, unknown>
  status: 'done' | 'error' | 'pending' | 'canceled'; preview?: string; action_id?: string; summary?: string; ms?: number
}

export interface ToolCatalog {
  scenes: { id: string; label: string; hint: string }[]
  page_scenes: Record<string, string[]>
  tools: { name: string; title: string; scene: string; description: string; when: string; risk: string; risk_label: string }[]
}

export interface AiAction {
  id: string; tool: string; title: string; summary: string; status: 'pending' | 'done' | 'error' | 'canceled'
  result?: unknown
}

export interface AskFinal {
  mode: 'llm' | 'fallback'
  content: string
  model: string
  thinking: string
  code: string
  checks: Record<string, unknown>
  notes: string[]
  elapsed_ms: number
  tool_calls?: ToolRecord[]
  /** 服务端验证后的研究上下文；旧请求/非工作流页面为 null。 */
  research_context?: AskResearchContext | null
  /** 服务端判断这次问题更适合完整研究会话（只建议，不自动创建）。 */
  suggest_session?: boolean
  /** 建议升级的原因，配合 suggest_session 展示。 */
  reason?: string
}

/** 临时问答 → 研究会话：升级时带上的对话轮次（只保留有正文的问答）。 */
export interface AskUpgradeTurn { role: 'user' | 'assistant'; content: string; data?: Record<string, unknown> }

/** POST /api/lab/projects/from-ask 的请求体：把临时问答转成可保存的研究会话。 */
export interface AskUpgradeIn {
  title: string
  idea: string
  /**
   * 来源引用（服务端按字典限长保存）：页面/工作流身份、附带资料等。
   * 研究上下文里的工作流、运行、节点与输出引用由服务端从 research 补全。
   */
  context: Record<string, unknown>
  messages: AskUpgradeTurn[]
  research?: AskResearchContext | null
}

export type AskEvent =
  | { type: 'delta'; kind: 'text' | 'thinking'; text: string }
  | { type: 'reset' }
  | ({ type: 'final' } & AskFinal)
  | { type: 'error'; message: string }
  | ({ type: 'tool' } & ToolRecord)

export interface AskMeta {
  models: ModelOption[]
  default_model?: string
  efforts?: EffortOption[]
  llm_ready: boolean
}

export const askAI = {
  meta: () => get<AskMeta>('/api/ai/ask/meta'),
  ask: (body: AskIn) => post<AskFinal>('/api/ai/ask', body),
  tools: () => get<ToolCatalog>('/api/ai/tools'),
  decide: (id: string, approve: boolean) => post<{ action: AiAction }>(`/api/ai/actions/${id}`, { approve }),
  /**
   * 把临时问答升级成研究会话：服务端新建 kind=research 的项目并写入种子消息，
   * 归属当前成员、默认私有。返回新建会话的完整视图（project + messages）。
   */
  upgrade: (body: AskUpgradeIn) => post<LabView>('/api/lab/projects/from-ask', body),
}

/** 流式提问：逐个回调事件，读到 `event: done` 或连接关闭时结束。 */
export async function askStream(body: AskIn, onEvent: (e: AskEvent) => void, signal?: AbortSignal): Promise<void> {
  const h = new Headers({ 'Content-Type': 'application/json', Accept: 'text/event-stream', 'X-Requested-With': 'quantlab' })
  const ws = targetWorkspace()
  if (ws) h.set('X-Workspace-Id', ws)
  const res = await fetch('/api/ai/ask/stream', { method: 'POST', credentials: 'same-origin', headers: h, body: JSON.stringify(body), signal })
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    let code = 'HTTP_' + res.status
    try {
      const j = await res.json()
      if (j?.message) message = j.message
      else if (typeof j?.detail === 'string') message = j.detail
      if (j?.code) code = j.code
    } catch { /* 非 JSON 错误体 */ }
    if (res.status === 401) window.dispatchEvent(new CustomEvent('qa:auth-required'))
    throw new ApiError(res.status, code, message)
  }
  const reader = res.body?.getReader()
  if (!reader) return
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) return
    buf += decoder.decode(value, { stream: true })
    let cut: number
    while ((cut = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, cut)
      buf = buf.slice(cut + 2)
      let event = 'message'
      const lines: string[] = []
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''))
      }
      if (event === 'done') {
        reader.cancel().catch(() => undefined)
        return
      }
      if (lines.length) {
        try { onEvent(JSON.parse(lines.join('\n')) as AskEvent) } catch { /* 跳过坏块 */ }
      }
    }
  }
}
