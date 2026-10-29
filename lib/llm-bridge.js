/**
 * dsh-quantlab · OpenAI 兼容模型桥（Host 半端，可注入 llm 服务单测）
 * ============================================================================
 * 把平台（Python 后端 / AI 研究室）用的 OpenAI 协议翻译成 DSH 的 `ctx.llm.stream`：
 *   POST /dsh-quantlab/llm/v1/chat/completions
 *     · stream=true → SSE：逐块 `data: {...}`，最后 `data: [DONE]`
 *     · stream=false → 标准 `choices[0].message`
 *   GET  /dsh-quantlab/llm/v1/models → `{object:"list", data:[{id:"dsh-default"}]}`
 *
 * provider/model 解析顺序（docs 4.4）：
 *   1. 插件配置 `model`（`provider/model` 或 `model`）
 *   2. DSH 默认模型（`ctx.agentDefaultModel.currentSelection()`）
 *   3. `ctx.llm.listProviders()` 第一个可用 provider 的 `listModels()` 第一个
 *   4. 都不行 → 502 `{error:{message}}`；`ctx.llm` 不可用 → 503
 *
 * chunk 映射（dsh-llm StreamChunk → OpenAI）：
 *   text-delta      → delta.content
 *   reasoning-delta → delta.reasoning_content（DeepSeek 兼容字段）
 *   tool-call-delta → delta.tool_calls[{index,id,type:function,function:{name,arguments}}]
 *   finish.reason   → finish_reason（tool-calls→tool_calls / stop→stop /
 *                     max-tokens→length / aborted|error→error）
 *   usage           → 末尾 usage 对象（prompt_tokens / completion_tokens / total_tokens）
 *
 * 无 DSH 依赖：llm 服务由调用方以 getter 注入，HTTP 层用 node:http 的 req/res。
 */

import { randomUUID } from 'node:crypto'

/** 平台登记时用的模型 id（默认模型名）。 */
export const DEFAULT_MODEL_ID = 'dsh-default'

/** 一次生成请求最多读多大的 body。 */
const MAX_BODY_BYTES = 8 * 1024 * 1024

/** provider/model 清单缓存时间（docs：不超过 60s）。 */
const MODEL_CACHE_MS = 60_000

/** DSH finish reason → OpenAI finish_reason。 */
export const FINISH_REASONS = Object.freeze({
  stop: 'stop',
  'tool-calls': 'tool_calls',
  'max-tokens': 'length',
  aborted: 'error',
  error: 'error',
})

/** 模型桥错误：带 HTTP 状态，路由层直接回 JSON。 */
export class BridgeError extends Error {
  constructor(message, { status = 500, code = 'llm_error' } = {}) {
    super(message)
    this.name = 'BridgeError'
    this.status = status
    this.code = code
  }
}

/** 唯一 sessionId（每次请求一个，便于 DSH 侧区分请求）。 */
export function newSessionId() {
  try {
    return `dsh-quantlab-${randomUUID()}`
  } catch {
    return `dsh-quantlab-${Date.now()}-${Math.random().toString(16).slice(2)}`
  }
}

function randomId(prefix) {
  try {
    return `${prefix}-${randomUUID().replace(/-/g, '').slice(0, 24)}`
  } catch {
    return `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
  }
}

// ---------------------------------------------------------------------------
// OpenAI → DSH
// ---------------------------------------------------------------------------

/**
 * 把 OpenAI content（字符串或 parts 数组）拼成纯文本。
 * 图片等非文本 part 只留一个占位符：DSH 这里是纯文本桥。
 */
export function contentToText(content) {
  if (typeof content === 'string') return content
  if (content === null || content === undefined) return ''
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === 'string') return part
        if (!part || typeof part !== 'object') return ''
        if (part.type === 'text' || part.type === 'input_text') return String(part.text ?? '')
        if (part.type === 'image_url' || part.type === 'input_image') return '[图片]'
        if (typeof part.text === 'string') return part.text
        return ''
      })
      .join('')
  }
  if (typeof content === 'object') {
    if (typeof content.text === 'string') return content.text
    return ''
  }
  return String(content)
}

/** 把 OpenAI content 转成 DSH 内容块数组（只有 text 块）。 */
export function contentToDshBlocks(content) {
  const text = contentToText(content)
  return text ? [{ type: 'text', text }] : []
}

/**
 * OpenAI messages → DSH 请求消息。
 *
 * · system / developer → role:system + source:{kind:'system-prompt'}
 * · user → role:user + source:{kind:'user'}
 * · assistant → content 文本 + tool_calls 转 {type:'tool-call'} 块，
 *   source:{kind:'model', provider, model}（dsh-llm 读 assistant.source）
 * · tool → role:tool + toolCallId + source:{kind:'tool', callId}
 *   （DeepSeek 适配器靠 message.toolCallId 关联 tool_result）
 *
 * @param {Array<object>} messages OpenAI messages
 * @param {{provider?: string, model?: string}} [target] 用于 assistant.source
 * @returns {Array<object>}
 */
export function toDshMessages(messages, target = {}) {
  const provider = target.provider || 'dsh-quantlab'
  const model = target.model || DEFAULT_MODEL_ID
  const out = []
  for (const message of Array.isArray(messages) ? messages : []) {
    if (!message || typeof message !== 'object') continue
    const role = String(message.role || '')
    if (role === 'system' || role === 'developer') {
      out.push({
        id: randomId('msg'),
        role: 'system',
        source: { kind: 'system-prompt' },
        content: contentToDshBlocks(message.content),
      })
      continue
    }
    if (role === 'user') {
      out.push({
        id: randomId('msg'),
        role: 'user',
        source: { kind: 'user' },
        content: contentToDshBlocks(message.content),
      })
      continue
    }
    if (role === 'assistant') {
      const content = []
      const text = contentToText(message.content)
      if (text) content.push({ type: 'text', text })
      for (const call of Array.isArray(message.tool_calls) ? message.tool_calls : []) {
        if (!call || typeof call !== 'object') continue
        const fn = call.function && typeof call.function === 'object' ? call.function : {}
        const rawArgs = fn.arguments
        const args =
          typeof rawArgs === 'string' ? rawArgs : rawArgs === undefined || rawArgs === null ? '{}' : JSON.stringify(rawArgs)
        content.push({
          type: 'tool-call',
          id: String(call.id || randomId('call')),
          name: String(fn.name || ''),
          arguments: args,
        })
      }
      // 空 assistant 消息适配器会跳过；保留（带 tool-call 时不是空）
      out.push({
        id: randomId('msg'),
        role: 'assistant',
        source: { kind: 'model', provider, model },
        content,
      })
      continue
    }
    if (role === 'tool' || role === 'function') {
      const callId = String(message.tool_call_id || message.toolCallId || '')
      out.push({
        id: randomId('msg'),
        role: 'tool',
        toolCallId: callId,
        source: { kind: 'tool', callId },
        ...(message.is_error === true ? { isError: true } : {}),
        content: contentToDshBlocks(message.content),
      })
    }
  }
  return out
}

/**
 * OpenAI tools → DSH ToolSchema[]（`{name, description, parameters}`）。
 * parameters 原样透传（DSH 的 ToolSchema.parameters 就是 JSON Schema）。
 */
export function toDshTools(tools) {
  const out = []
  for (const tool of Array.isArray(tools) ? tools : []) {
    if (!tool || typeof tool !== 'object') continue
    const fn = tool.function && typeof tool.function === 'object' ? tool.function : tool
    const name = String(fn.name || tool.name || '').trim()
    if (!name) continue
    const parameters =
      fn.parameters && typeof fn.parameters === 'object' && !Array.isArray(fn.parameters)
        ? fn.parameters
        : { type: 'object', properties: {} }
    out.push({
      name,
      description: String(fn.description || ''),
      parameters,
    })
  }
  return out
}

// ---------------------------------------------------------------------------
// DSH chunk → OpenAI
// ---------------------------------------------------------------------------

/** DSH usage → OpenAI usage 对象。 */
export function toOpenAiUsage(usage) {
  if (!usage || typeof usage !== 'object') return undefined
  const prompt = Number(usage.inputTokens) || 0
  const completion = Number(usage.outputTokens) || 0
  const total = Number.isFinite(Number(usage.totalTokens)) ? Number(usage.totalTokens) : prompt + completion
  return {
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: total,
    ...(usage.cacheReadTokens === undefined ? {} : { prompt_cache_hit_tokens: Number(usage.cacheReadTokens) || 0 }),
    ...(usage.reasoningTokens === undefined ? {} : { completion_tokens_details: { reasoning_tokens: Number(usage.reasoningTokens) || 0 } }),
  }
}

/** DSH finish reason → OpenAI finish_reason 字符串。 */
export function toOpenAiFinishReason(reason) {
  const kind = String((reason && reason.kind) || 'stop')
  return FINISH_REASONS[kind] || 'stop'
}

/**
 * chunk 映射器的累积状态。
 * @returns {object}
 */
export function createMapperState() {
  return {
    /** DSH block index → { index: OpenAI tool index, nameSent: boolean, args: string, id: string, name: string } */
    tools: new Map(),
    nextToolIndex: 0,
    text: '',
    reasoning: '',
    toolCalls: [],
    finishReason: null,
    finished: false,
    usage: null,
    error: null,
  }
}

/**
 * 一个 DSH StreamChunk → 0..N 个 OpenAI chunk（不含 SSE 包装）。
 * 纯函数（只改 state），便于单测。
 * @param {object} state createMapperState() 的返回值
 * @param {object} chunk dsh-llm StreamChunk
 * @returns {Array<object>}
 */
export function mapStreamChunk(state, chunk) {
  if (!chunk || typeof chunk !== 'object') return []
  const events = []
  switch (chunk.type) {
    case 'text-delta': {
      const text = String(chunk.text ?? '')
      if (!text) break
      state.text += text
      events.push({ choices: [{ index: 0, delta: { content: text } }] })
      break
    }
    case 'reasoning-delta': {
      const text = String(chunk.text ?? '')
      if (!text) break
      state.reasoning += text
      events.push({ choices: [{ index: 0, delta: { reasoning_content: text } }] })
      break
    }
    case 'tool-call-delta': {
      const slot = touchTool(state, chunk.index, chunk.id, chunk.name)
      const argsDelta = String(chunk.argumentsDelta ?? '')
      if (argsDelta) slot.args += argsDelta
      const fn = { arguments: argsDelta }
      const delta = { index: slot.index, type: 'function', function: fn }
      if (!slot.nameSent && chunk.name) {
        fn.name = String(chunk.name)
        slot.name = String(chunk.name)
        slot.nameSent = true
      }
      if (!slot.idSent && chunk.id) {
        delta.id = String(chunk.id)
        slot.id = String(chunk.id)
        slot.idSent = true
      }
      events.push({ choices: [{ index: 0, delta: { tool_calls: [delta] } }] })
      break
    }
    case 'block-end': {
      const block = chunk.block
      if (block && block.type === 'tool-call') {
        const slot = touchTool(state, chunk.index, block.id, block.name)
        if (!slot.argumentsComplete) {
          const full = String(block.arguments ?? '')
          slot.args = full
          slot.argumentsComplete = true
          const delta = { index: slot.index, type: 'function', function: { arguments: full } }
          if (!slot.idSent && block.id) {
            delta.id = String(block.id)
            slot.id = String(block.id)
            slot.idSent = true
          }
          if (!slot.nameSent && block.name) {
            delta.function.name = String(block.name)
            slot.name = String(block.name)
            slot.nameSent = true
          }
          events.push({ choices: [{ index: 0, delta: { tool_calls: [delta] } }] })
        }
      }
      break
    }
    case 'usage': {
      state.usage = chunk.usage || null
      break
    }
    case 'finish': {
      state.finished = true
      state.finishReason = toOpenAiFinishReason(chunk.reason)
      if (chunk.reason && chunk.reason.failure) state.error = chunk.reason.failure
      const event = { choices: [{ index: 0, delta: {}, finish_reason: state.finishReason }] }
      if (state.error && state.error.message) event.error = { message: String(state.error.message), code: String(state.error.code || 'llm_error') }
      events.push(event)
      break
    }
    default:
      break
  }
  return events
}

function touchTool(state, blockIndex, id, name) {
  const key = Number.isFinite(Number(blockIndex)) ? Number(blockIndex) : 0
  let slot = state.tools.get(key)
  if (!slot) {
    slot = {
      index: state.nextToolIndex++,
      args: '',
      name: '',
      id: '',
      nameSent: false,
      idSent: false,
      argumentsComplete: false,
      dshIndex: key,
    }
    state.tools.set(key, slot)
    state.toolCalls[slot.index] = slot
  }
  return slot
}

/** 流结束后从 state 拼出 OpenAI 非流式 message。 */
export function stateToMessage(state) {
  const toolCalls = []
  for (const slot of state.toolCalls) {
    if (!slot) continue
    toolCalls.push({
      id: slot.id || randomId('call'),
      type: 'function',
      function: { name: slot.name || '', arguments: slot.args || '{}' },
    })
  }
  const message = {
    role: 'assistant',
    content: state.text ? state.text : toolCalls.length ? null : '',
  }
  if (state.reasoning) message.reasoning_content = state.reasoning
  if (toolCalls.length) message.tool_calls = toolCalls
  return message
}

// ---------------------------------------------------------------------------
// HTTP 层
// ---------------------------------------------------------------------------

/** 写 JSON 响应。 */
export function writeJson(res, status, payload) {
  try {
    const body = JSON.stringify(payload)
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'content-length': Buffer.byteLength(body),
    })
    res.end(body)
  } catch {
    /* 连接已断 */
  }
}

/** 写 OpenAI 风格错误：`{error:{message,type,code}}`。 */
export function writeError(res, status, message, code = 'llm_error') {
  writeJson(res, status, { error: { message: String(message || '模型桥错误'), type: status === 503 ? 'service_unavailable' : 'invalid_request_error', code } })
}

/** 读并解析 JSON body（有大小上限）。 */
export function readJsonBody(req, maxBytes = MAX_BODY_BYTES) {
  return new Promise((done) => {
    if (!req || typeof req.on !== 'function') {
      done({ ok: false, error: '请求体不可读' })
      return
    }
    let size = 0
    const chunks = []
    const finish = (value) => {
      req.removeListener?.('data', onData)
      req.removeListener?.('end', onEnd)
      done(value)
    }
    const onData = (chunk) => {
      size += chunk.length
      if (size > maxBytes) {
        finish({ ok: false, error: '请求体过大' })
        return
      }
      chunks.push(chunk)
    }
    const onEnd = () => {
      const raw = Buffer.concat(chunks).toString('utf8').trim()
      if (!raw) return finish({ ok: true, body: {} })
      try {
        const body = JSON.parse(raw)
        if (!body || typeof body !== 'object' || Array.isArray(body)) return finish({ ok: false, error: '请求体必须是 JSON 对象' })
        return finish({ ok: true, body })
      } catch {
        return finish({ ok: false, error: '请求体不是合法 JSON' })
      }
    }
    req.on('data', onData)
    req.on('end', onEnd)
    req.on('error', () => finish({ ok: false, error: '读取请求体失败' }))
  })
}

/** DSH 终止 chunk 是否是「provider 没有注册适配器」（拓扑变化，可换目标重试）。 */
export function isNoAdapterFinish(chunk) {
  return Boolean(
    chunk &&
      chunk.type === 'finish' &&
      chunk.reason &&
      chunk.reason.kind === 'error' &&
      chunk.reason.failure &&
      chunk.reason.failure.code === 'NO_ADAPTER',
  )
}

/**
 * 模型桥。
 */
export class LlmBridge {
  /**
   * @param {object} options
   * @param {Function} options.getLlm 返回 DSH llm 服务（可能为空）
   * @param {Function} [options.getDefaultModel] 返回 DSH `agentDefaultModel` 服务
   * @param {string} [options.model] 插件配置的模型（`provider/model` 或 `model`）
   * @param {string} [options.modelId] 平台侧模型 id，默认 `dsh-default`
   * @param {Function} [options.log] 诊断日志
   * @param {Function} [options.now] 时间源（测试）
   * @param {number} [options.modelCacheMs] 模型清单缓存毫秒
   */
  constructor(options = {}) {
    this._getLlm = options.getLlm || (() => null)
    this._getDefaultModel = options.getDefaultModel || (() => null)
    this.model = String(options.model || '').trim()
    this.modelId = String(options.modelId || DEFAULT_MODEL_ID).trim() || DEFAULT_MODEL_ID
    this._log = typeof options.log === 'function' ? options.log : () => {}
    this._now = typeof options.now === 'function' ? options.now : () => Date.now()
    this.modelCacheMs = Number.isFinite(Number(options.modelCacheMs)) ? Number(options.modelCacheMs) : MODEL_CACHE_MS
    this._targetCache = null
    this._targetCachedAt = 0
    /** @type {{provider:string,model:string}|null} 最近一次解析出的目标（诊断用） */
    this.lastTarget = null
  }

  /** llm 服务是否可用。 */
  llm() {
    try {
      const llm = this._getLlm()
      return llm && typeof llm.stream === 'function' ? llm : null
    } catch {
      return null
    }
  }

  /** 模型桥是否可用（状态路由 / 自动登记据此判断）。 */
  available() {
    return Boolean(this.llm())
  }

  /** 清掉解析缓存（配置变更 / 手动刷新时用）。 */
  invalidate() {
    this._targetCache = null
    this._targetCachedAt = 0
  }

  /**
   * 解析 provider/model。
   * @param {{fresh?: boolean}} [options]
   * @returns {Promise<{provider: string, model: string, reasoningEffort?: string}>}
   * @throws {BridgeError} llm 不可用（503）或解析不出模型（502）
   */
  async resolveTarget(options = {}) {
    const llm = this.llm()
    if (!llm) {
      throw new BridgeError('DSH 模型服务不可用（插件没拿到 ctx.llm）', { status: 503, code: 'llm_unavailable' })
    }
    const fresh = options.fresh === true
    if (!fresh && this._targetCache && this._now() - this._targetCachedAt < this.modelCacheMs) {
      return this._targetCache
    }

    const explicit = this.model
    const selection = await this._defaultSelection()
    let target = null

    if (explicit) {
      const slash = explicit.indexOf('/')
      if (slash > 0) {
        target = { provider: explicit.slice(0, slash), model: explicit.slice(slash + 1) }
      } else if (selection && selection.model === explicit && selection.provider) {
        target = { provider: selection.provider, model: explicit }
      } else if (selection && selection.provider) {
        target = { provider: selection.provider, model: explicit }
      } else {
        const first = await this._firstProviderModel(llm)
        if (first) target = { provider: first.provider, model: explicit }
      }
    } else if (selection && selection.provider && selection.model) {
      target = { provider: selection.provider, model: selection.model }
      if (selection.reasoningEffort) target.reasoningEffort = selection.reasoningEffort
    } else {
      const first = await this._firstProviderModel(llm)
      if (first) target = { provider: first.provider, model: first.model }
    }

    if (!target || !target.provider || !target.model) {
      throw new BridgeError(
        '解析不出可用的 DSH 模型：请在 DSH 设置里选一个模型，或在插件配置 model 里指定 provider/model',
        { status: 502, code: 'no_model' },
      )
    }
    this._targetCache = target
    this._targetCachedAt = this._now()
    this.lastTarget = target
    return target
  }

  /** 读 DSH 默认模型（agentDefaultModel.currentSelection()）。 */
  async _defaultSelection() {
    try {
      const service = this._getDefaultModel()
      if (!service) return null
      const current = typeof service.currentSelection === 'function' ? service.currentSelection() : null
      const selection = current && typeof current.then === 'function' ? await current : current
      if (!selection || typeof selection !== 'object') return null
      const provider = String(selection.provider || '').trim()
      const model = String(selection.model || '').trim()
      if (!provider || !model) return null
      const out = { provider, model }
      if (selection.reasoningEffort) out.reasoningEffort = String(selection.reasoningEffort)
      return out
    } catch {
      return null
    }
  }

  /** 第一个可用 provider 的第一个模型。 */
  async _firstProviderModel(llm) {
    let providers = []
    try {
      providers = await llm.listProviders()
    } catch {
      providers = []
    }
    for (const provider of Array.isArray(providers) ? providers : []) {
      const id = String((provider && (provider.id || provider.provider)) || '').trim()
      if (!id) continue
      try {
        const models = await llm.listModels(id)
        const first = Array.isArray(models) ? models.find((model) => model && (model.id || model.model)) : null
        if (first) return { provider: id, model: String(first.id || first.model) }
      } catch (error) {
        this._log(`模型桥：listModels(${id}) 失败：${(error && error.message) || error}`)
      }
    }
    return null
  }

  /** 换一个 provider（跳过已失败的）：适配器热更新后原路由暂时没注册时用。 */
  async _alternateTarget(excludeProvider) {
    const llm = this.llm()
    if (!llm) return null
    let providers = []
    try {
      providers = await llm.listProviders()
    } catch {
      return null
    }
    for (const provider of Array.isArray(providers) ? providers : []) {
      const id = String((provider && (provider.id || provider.provider)) || '').trim()
      if (!id || id === excludeProvider) continue
      try {
        const models = await llm.listModels(id)
        const first = Array.isArray(models) ? models.find((model) => model && (model.id || model.model)) : null
        if (first) return { provider: id, model: String(first.id || first.model) }
      } catch {
        /* 换下一个 provider */
      }
    }
    return null
  }

  /**
   * 打开一个 DSH 流并先读第一个 chunk（用于在写出任何 SSE 字节前识别 NO_ADAPTER）。
   * @param {object} llm
   * @param {object} params
   * @returns {Promise<{iterator: AsyncIterator<object>, firstChunk: object|undefined}>}
   */
  async _openStream(llm, params) {
    const stream = llm.stream(params)
    const iterator = stream && typeof stream[Symbol.asyncIterator] === 'function' ? stream[Symbol.asyncIterator]() : null
    if (!iterator) throw new Error('ctx.llm.stream 没有返回异步迭代器')
    const first = await iterator.next()
    return { iterator, firstChunk: first && !first.done ? first.value : undefined }
  }

  /** 平台可登记的模型 id 列表。 */
  modelIds() {
    return [this.modelId]
  }

  /** GET /v1/models 响应体。 */
  modelsPayload() {
    const created = Math.floor(this._now() / 1000)
    return {
      object: 'list',
      data: this.modelIds().map((id) => ({ id, object: 'model', created, owned_by: 'dsh-harness' })),
    }
  }

  /**
   * 处理 POST /v1/chat/completions。
   * @param {object} req node:http IncomingMessage
   * @param {object} res node:http ServerResponse
   */
  async handleCompletions(req, res) {
    const parsed = await readJsonBody(req)
    if (!parsed.ok) {
      writeError(res, 400, parsed.error, 'invalid_request')
      return
    }
    if (!this.available()) {
      writeError(res, 503, 'DSH 模型服务不可用：插件没有拿到 ctx.llm（DSH 未加载模型服务）', 'llm_unavailable')
      return
    }
    const body = parsed.body
    const messages = Array.isArray(body.messages) ? body.messages : []
    if (messages.length === 0) {
      writeError(res, 400, 'messages 不能为空', 'invalid_request')
      return
    }
    let target
    try {
      target = await this.resolveTarget()
    } catch (error) {
      const status = Number(error && error.status) || 502
      writeError(res, status, (error && error.message) || '解析不出 DSH 模型', (error && error.code) || 'no_model')
      return
    }
    const llm = this.llm()
    const wantsStream = body.stream === true
    const requestModel = String(body.model || this.modelId)
    const id = randomId('chatcmpl')
    const created = Math.floor(this._now() / 1000)
    const controller = typeof AbortController === 'function' ? new AbortController() : null
    let closed = false
    const onClose = () => {
      if (!res.writableEnded) {
        closed = true
        try {
          controller?.abort()
        } catch {
          /* ignore */
        }
      }
    }
    res.on?.('close', onClose)

    const buildParams = (resolved) => {
      const params = {
        provider: resolved.provider,
        model: resolved.model,
        sessionId: newSessionId(),
        messages: toDshMessages(messages, resolved),
        tools: toDshTools(body.tools),
        ...(Number.isFinite(Number(body.max_tokens)) && Number(body.max_tokens) > 0 ? { maxTokens: Number(body.max_tokens) } : {}),
        ...(Number.isFinite(Number(body.temperature)) ? { temperature: Number(body.temperature) } : {}),
        ...(resolved.reasoningEffort ? { reasoningEffort: resolved.reasoningEffort } : {}),
        ...(controller ? { signal: controller.signal } : {}),
      }
      if (params.tools.length === 0) delete params.tools
      return params
    }
    let params = buildParams(target)

    // 打开流（先读第一个 chunk）：适配器拓扑可能在运行中变化（HMR / 设置里改 provider），
    // DSH 会以 finish{kind:"error",failure.code:"NO_ADAPTER"} 立即失败。
    // 这时还没写出任何 SSE 字节，可以清缓存重新解析一次再试。
    let opened
    try {
      opened = await this._openStream(llm, params)
      if (isNoAdapterFinish(opened.firstChunk)) {
        try {
          await opened.iterator.return?.()
        } catch {
          /* ignore */
        }
        this.invalidate()
        let retryTarget = await this.resolveTarget({ fresh: true }).catch(() => null)
        if (!retryTarget || (retryTarget.provider === target.provider && retryTarget.model === target.model)) {
          retryTarget = await this._alternateTarget(target.provider)
        }
        if (retryTarget && (retryTarget.provider !== target.provider || retryTarget.model !== target.model)) {
          this._log(
            `模型桥：${target.provider}/${target.model} 没有已注册适配器，改用 ${retryTarget.provider}/${retryTarget.model} 重试`,
          )
          target = retryTarget
          this._targetCache = retryTarget
          this._targetCachedAt = this._now()
          this.lastTarget = retryTarget
          params = buildParams(retryTarget)
          opened = await this._openStream(llm, params)
        }
      }
    } catch (error) {
      writeError(res, 502, `DSH 模型调用失败：${(error && error.message) || error}`, 'llm_error')
      return
    }

    const iterator = opened.iterator
    const state = createMapperState()
    if (wantsStream) {
      this._writeSseHead(res)
      this._writeSseChunk(res, {
        id,
        object: 'chat.completion.chunk',
        created,
        model: requestModel,
        choices: [{ index: 0, delta: { role: 'assistant', content: '' }, finish_reason: null }],
      })
    }
    // 打开流时已经读到的第一个 chunk 先处理掉
    if (opened.firstChunk !== undefined) {
      for (const event of mapStreamChunk(state, opened.firstChunk)) {
        if (wantsStream) {
          this._writeSseChunk(res, { id, object: 'chat.completion.chunk', created, model: requestModel, ...event })
        }
      }
    }

    try {
      for (;;) {
        const next = await iterator.next()
        if (next.done) break
        const events = mapStreamChunk(state, next.value)
        for (const event of events) {
          if (wantsStream) {
            this._writeSseChunk(res, { id, object: 'chat.completion.chunk', created, model: requestModel, ...event })
          }
        }
        if (closed) break
      }
    } catch (error) {
      const message = String((error && error.message) || error)
      state.error = { message, code: 'llm_error' }
      state.finished = true
      state.finishReason = 'error'
      if (wantsStream) {
        this._writeSseChunk(res, {
          id,
          object: 'chat.completion.chunk',
          created,
          model: requestModel,
          choices: [{ index: 0, delta: {}, finish_reason: 'error' }],
          error: { message, code: 'llm_error' },
        })
      }
      this._log(`模型桥：流式调用失败：${message}`)
    } finally {
      try {
        await iterator.return?.()
      } catch {
        /* ignore */
      }
    }

    const usage = toOpenAiUsage(state.usage)
    if (wantsStream) {
      if (usage) this._writeSseChunk(res, { id, object: 'chat.completion.chunk', created, model: requestModel, choices: [], usage })
      try {
        res.write('data: [DONE]\n\n')
      } catch {
        /* ignore */
      }
      try {
        res.end()
      } catch {
        /* ignore */
      }
      return
    }

    if (!res.writableEnded && !res.headersSent) {
      // 什么都没产出的失败按 502 回，平台会显示「模型不可用」而不是空回复
      const emptyFailure = state.error && !state.text && state.toolCalls.length === 0
      if (emptyFailure) {
        writeError(res, 502, state.error.message || 'DSH 模型调用失败', state.error.code || 'llm_error')
        return
      }
      writeJson(res, 200, {
        id,
        object: 'chat.completion',
        created,
        model: requestModel,
        choices: [
          {
            index: 0,
            message: stateToMessage(state),
            finish_reason: state.finishReason || 'stop',
          },
        ],
        ...(usage ? { usage } : {}),
        ...(state.error && state.error.message ? { error: { message: String(state.error.message), code: String(state.error.code || 'llm_error') } } : {}),
      })
    }
  }

  /** GET /v1/models。llm 不可用时也返回清单（平台添加服务商时会先探测）。 */
  handleModels(req, res) {
    if (req && req.method && String(req.method).toUpperCase() !== 'GET') {
      writeError(res, 405, '只用 GET', 'method_not_allowed')
      return
    }
    writeJson(res, 200, this.modelsPayload())
  }

  /** 兜底：其它 /v1/* 请求回 404 JSON。 */
  handleNotFound(req, res) {
    writeError(res, 404, `模型桥没有这个接口：${String((req && req.url) || '')}`, 'not_found')
  }

  _writeSseHead(res) {
    try {
      res.writeHead(200, {
        'content-type': 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache, no-transform',
        connection: 'keep-alive',
        'x-accel-buffering': 'no',
      })
      if (typeof res.flushHeaders === 'function') res.flushHeaders()
    } catch {
      /* ignore */
    }
  }

  _writeSseChunk(res, payload) {
    try {
      res.write(`data: ${JSON.stringify(payload)}\n\n`)
    } catch {
      /* 连接已断 */
    }
  }
}

/**
 * 工厂。
 * @param {ConstructorParameters<typeof LlmBridge>[0]} options
 * @returns {LlmBridge}
 */
export function createLlmBridge(options = {}) {
  return new LlmBridge(options)
}
