/**
 * 页面内「问 AI」接口：一次性问答、流式问答（POST + SSE）、面板元信息。
 * 流式接口是 POST（要带页面资料与历史），EventSource / openStream 只能 GET，这里用 fetch 自己读流。
 */
import { ApiError, currentWorkspace, get, post } from './http'
import type { Effort, EffortOption, ModelOption } from './apiLab'

/** 页面附带的一份资料：服务端会截断，并且只当作参考数据。 */
export interface AskContextItem { kind: string; title: string; text: string }
export interface AskTurn { role: 'user' | 'assistant'; content: string }

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
}

/** 一次平台工具调用（需要确认的动作带 action_id，状态 pending）。 */
export interface ToolRecord {
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
}

/** 流式提问：逐个回调事件，读到 `event: done` 或连接关闭时结束。 */
export async function askStream(body: AskIn, onEvent: (e: AskEvent) => void, signal?: AbortSignal): Promise<void> {
  const h = new Headers({ 'Content-Type': 'application/json', Accept: 'text/event-stream', 'X-Requested-With': 'quantlab' })
  const ws = currentWorkspace()
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
