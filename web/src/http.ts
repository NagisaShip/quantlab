/**
 * 统一的 HTTP 客户端（v2.0）。
 *
 * - 会话：HttpOnly Cookie（qa_session），前端拿不到也不保存 token；
 * - 跨站防护：所有请求带 `X-Requested-With`，后端对 Cookie 认证的写请求强制校验；
 * - 工作区：`X-Workspace-Id` 只是“目标工作区”，服务端会校验成员关系；
 * - 错误契约：后端统一返回 {code, message, trace_id, details}，这里转成 ApiError；
 * - 401 时广播 `qa:auth-required`，App 回到登录页。
 */

import { isLocalDeployment } from './deployment'

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

/** 本地项目只有一个工作区，且身份由 QUANTLAB_PROJECT 决定；不要带出账号版遗留的 qa.workspace。 */
export function targetWorkspace(): string {
  return isLocalDeployment() ? '' : currentWorkspace()
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
  const ws = targetWorkspace()
  if (!ws) return url
  return url + (url.includes('?') ? '&' : '?') + 'ws=' + encodeURIComponent(ws)
}

function headers(extra?: HeadersInit): Headers {
  const h = new Headers(extra)
  if (!h.has('Content-Type')) h.set('Content-Type', 'application/json')
  h.set('X-Requested-With', 'quantlab')
  const ws = targetWorkspace()
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
  // 只有中间件明确拒绝了请求头里的 workspace_id 时，才清空本地工作区。
  // 不能仅凭 403 + 文案里出现“工作区”判断：功能开关关闭也使用 PERMISSION_DENIED，
  // 误清空会把用户悄悄切回另一个工作区，并让“已关闭”状态看起来像仍然开启。
  if (res.status === 403 && code === 'PERMISSION_DENIED' && details.workspace_id) {
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

/** 导出的 CSV 单元格公式中和（与后端 csvsafe 同规则）。 */
export function csvCell(v: unknown): string {
  let s = v === null || v === undefined ? '' : String(v)
  if (/^[=+\-@\t\r]/.test(s) && !/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(s.trim())) s = "'" + s
  if (/[",\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"'
  return s
}

// ---------------------------------------------------------------------------
// 错误 → 人话 + 修复动作
// ---------------------------------------------------------------------------

/** 面向用户的错误说明：标题、说明、修复动作，以及可展开的技术详情。 */
export interface ErrorExplanation {
  title: string
  message: string
  action?: string
  detail: string
}

/** 后端偶尔把 detail 直接 JSON.stringify 进 message；展示前拆掉 JSON 外壳。 */
function readableMessage(message: string): string {
  const text = (message || '').trim()
  const wrapped = (text.startsWith('{') && text.endsWith('}')) || (text.startsWith('[') && text.endsWith(']'))
  if (!wrapped) return text
  try {
    const body = JSON.parse(text) as { message?: unknown; detail?: unknown }
    const inner = typeof body?.message === 'string' ? body.message : typeof body?.detail === 'string' ? body.detail : ''
    const trimmed = inner.trim()
    if (trimmed && !trimmed.startsWith('{') && !trimmed.startsWith('[')) return trimmed
  } catch {
    /* 不是合法 JSON：按原文保留 */
  }
  return ''
}

/** 技术详情：错误码、HTTP 状态、trace_id 与后端原文，供排障时展开查看。 */
function technicalDetail(error: ApiError): string {
  const lines = [
    error.code ? `code=${error.code}` : '',
    `HTTP ${error.status}`,
    error.traceId ? `trace_id=${error.traceId}` : '',
    error.message,
  ].filter(Boolean)
  if (error.details && Object.keys(error.details).length) lines.push(JSON.stringify(error.details))
  return lines.join('\n')
}

/** 表单校验失败的字段列表（后端 details.fields）。 */
function fieldSummary(error: ApiError): string {
  const fields = (error.details as { fields?: { field?: string; message?: string }[] }).fields
  if (!fields?.length) return ''
  return fields.map((field) => `${field.field ?? ''}: ${field.message ?? ''}`).join('; ')
}

/** 依赖缺失：环境检查/解释器相关错误统一引导到「研究环境」。 */
function looksLikeDependency(error: ApiError): boolean {
  const raw = `${error.message} ${JSON.stringify(error.details ?? {})}`
  return /psutil|venv|conda|wheel|pip\b|pandas|numpy|scipy|torch|依赖|解释器/i.test(raw)
}

/**
 * 把 ApiError / 网络错误翻译成「人话标题 + 修复动作」。
 * 技术细节保留在 detail，由错误块用可展开区域展示，不再把 JSON 直接甩给用户。
 */
export function explainError(error: unknown): ErrorExplanation {
  if (!(error instanceof ApiError)) {
    if (error instanceof TypeError) {
      return {
        title: '连不上本机服务',
        message: '请求没有到达后端：可能后端已退出，或端口被占用。',
        action: '确认后端仍在运行；桌面版可以重启窗口，浏览器版刷新页面后重试。',
        detail: String(error.message || error),
      }
    }
    return {
      title: '操作失败',
      message: String(error ?? '').trim() || '请求没有完成。',
      detail: String(error ?? ''),
    }
  }
  const detail = [technicalDetail(error), fieldSummary(error)].filter(Boolean).join('\n')
  const message = readableMessage(error.message)
  const feature = (error.details as { feature?: unknown }).feature
  switch (error.code) {
    case 'INVALID_PARAMETER':
      return {
        title: '参数不完整或有误',
        message: message || '提交的内容没有通过校验。',
        action: '按提示修正后再试。',
        detail,
      }
    case 'PERMISSION_DENIED':
      if (typeof feature === 'string' && feature) {
        return {
          title: '该能力尚未开启',
          message: message || '当前工作区没有开启这个能力。',
          action: '到 设置 → 本地项目 打开它；账号模式需要工作区管理员在功能开关里开启。',
          detail,
        }
      }
      if ((error.details as { workspace_id?: unknown }).workspace_id) {
        return {
          title: '需要重新选择工作区',
          message: '当前工作区已失效，或不属于这个账号。',
          action: '重新选择工作区后再试。',
          detail,
        }
      }
      return {
        title: '没有权限执行这一步',
        message: message || '当前账号没有这个操作的权限。',
        action: '切换到有权限的工作区，或请管理员授权。',
        detail,
      }
    case 'NOT_FOUND':
      return {
        title: '目标不存在或已被删除',
        message: message || '要访问的内容已经不在了。',
        action: '刷新列表后重新选择。',
        detail,
      }
    case 'CONFLICT':
    case 'IDEMPOTENCY_CONFLICT':
    case 'CREDENTIAL_REVOKED':
    case 'MARKET_DATA_STALE':
      return {
        title: '操作与当前状态冲突',
        message: message || '数据已经变化，这次操作没有生效。',
        action: '刷新页面确认最新状态后重试。',
        detail,
      }
    case 'RATE_LIMITED':
    case 'UPSTREAM_RATE_LIMITED':
    case 'QUOTA_EXHAUSTED':
      return {
        title: '请求过于频繁或额度用尽',
        message: message || '请稍后再试。',
        action: '稍等片刻；额度问题到 设置 → 额度 查看。',
        detail,
      }
    case 'UNAUTHENTICATED':
      return {
        title: '登录已失效',
        message: message || '需要重新登录。',
        action: '重新登录后再试。',
        detail,
      }
    default:
      break
  }
  if (looksLikeDependency(error)) {
    return {
      title: '运行环境缺少依赖',
      message: message || '研究环境缺少运行所需的依赖或解释器。',
      action: '到 设置 → 研究环境 点「检查并修复环境」。',
      detail,
    }
  }
  if (error.status >= 500) {
    return {
      title: '服务端出错',
      message: message || '后端处理时发生异常。',
      action: '稍后重试；持续失败请把 trace_id 发给管理员。',
      detail,
    }
  }
  return {
    title: '操作失败',
    message: message || '请求没有完成。',
    action: '检查输入或稍后重试。',
    detail,
  }
}
