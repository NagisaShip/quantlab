/**
 * 统一的 HTTP 客户端（v2.0）。
 *
 * - 会话：HttpOnly Cookie（qa_session），前端拿不到也不保存 token；
 * - 跨站防护：所有请求带 `X-Requested-With`，后端对 Cookie 认证的写请求强制校验；
 * - 工作区：`X-Workspace-Id` 只是“目标工作区”，服务端会校验成员关系；
 * - 错误契约：后端统一返回 {code, message, trace_id, details}，这里转成 ApiError；
 * - 401 时广播 `qa:auth-required`，App 回到登录页。
 */

export class ApiError extends Error {
  code: string
  status: number
  traceId: string
  details: Record<string, unknown>
  constructor(status: number, code: string, message: string, traceId = '', details: Record<string, unknown> = {}) {
    super(message)
    this.status = status
    this.code = code
    this.traceId = traceId
    this.details = details
  }
}

const WS_KEY = 'qa.workspace'

export function currentWorkspace(): string {
  try {
    return localStorage.getItem(WS_KEY) || ''
  } catch {
    return ''
  }
}

export function setWorkspace(id: string) {
  try {
    if (id) localStorage.setItem(WS_KEY, id)
    else localStorage.removeItem(WS_KEY)
  } catch {
    /* 隐私模式下忽略 */
  }
}

/** 给下载链接（新窗口打开）带上目标工作区。 */
export function withWs(url: string): string {
  const ws = currentWorkspace()
  if (!ws) return url
  return url + (url.includes('?') ? '&' : '?') + 'ws=' + encodeURIComponent(ws)
}

function headers(extra?: HeadersInit): Headers {
  const h = new Headers(extra)
  if (!h.has('Content-Type')) h.set('Content-Type', 'application/json')
  h.set('X-Requested-With', 'quantlab')
  const ws = currentWorkspace()
  if (ws) h.set('X-Workspace-Id', ws)
  return h
}

async function toError(res: Response): Promise<ApiError> {
  let code = 'HTTP_' + res.status
  let message = `${res.status} ${res.statusText}`
  let trace = res.headers.get('x-trace-id') || ''
  let details: Record<string, unknown> = {}
  try {
    const body = await res.json()
    if (body?.code) code = body.code
    if (body?.message) message = body.message
    else if (body?.detail) message = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    if (body?.trace_id) trace = body.trace_id
    if (body?.details) details = body.details
    const fields = (details as { fields?: { field: string; message: string }[] }).fields
    if (fields?.length) message = fields.map((f) => `${f.field}：${f.message}`).join('；')
  } catch {
    /* 非 JSON 错误体 */
  }
  if (res.status === 401) window.dispatchEvent(new CustomEvent('qa:auth-required'))
  if (res.status === 403 && code === 'PERMISSION_DENIED' && /工作区/.test(message)) {
    setWorkspace('')
    window.dispatchEvent(new CustomEvent('qa:workspace-invalid'))
  }
  return new ApiError(res.status, code, message, trace, details)
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { credentials: 'same-origin', ...init, headers: headers(init?.headers) })
  if (!res.ok) throw await toError(res)
  return res.json() as Promise<T>
}

export async function requestText(path: string, init?: RequestInit): Promise<string> {
  const res = await fetch(path, { credentials: 'same-origin', ...init, headers: headers(init?.headers) })
  if (!res.ok) throw await toError(res)
  return res.text()
}

/** 打开一个流式响应（SSE 等）：带上与普通请求相同的头，出错时抛 ApiError。 */
export async function openStream(path: string, signal?: AbortSignal): Promise<Response> {
  const res = await fetch(path, { credentials: 'same-origin', signal, headers: headers({ Accept: 'text/event-stream' }) })
  if (!res.ok) throw await toError(res)
  return res
}

export const get = <T,>(p: string) => request<T>(p)
export const post = <T,>(p: string, body?: unknown, extra?: HeadersInit) =>
  request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body), headers: extra })
export const put = <T,>(p: string, body: unknown) => request<T>(p, { method: 'PUT', body: JSON.stringify(body) })
export const del = <T,>(p: string) => request<T>(p, { method: 'DELETE' })

/** 写操作的幂等键：同一次用户操作重试不会重复创建。 */
export function idemKey(prefix = 'op'): string {
  const rnd = (globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2)) as string
  return `${prefix}-${rnd}`
}

/** 金额一律以十进制字符串传输；展示前统一转数字。 */
export const num = (v: unknown): number => {
  if (typeof v === 'number') return v
  if (typeof v === 'string' && v.trim() !== '') return Number(v)
  return 0
}

/** 导出 CSV 的单元格公式中和（与后端 csvsafe 同规则）。 */
export function csvCell(v: unknown): string {
  let s = v === null || v === undefined ? '' : String(v)
  if (/^[=+\-@\t\r]/.test(s) && !/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(s.trim())) s = "'" + s
  if (/[",\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"'
  return s
}
