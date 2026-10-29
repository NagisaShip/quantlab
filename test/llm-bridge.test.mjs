/**
 * lib/llm-bridge.js 单测：OpenAI ↔ DSH 消息/工具转换、流式 SSE、非流式、503/502。
 * 用假 llm.stream 产出 dsh-llm 的 StreamChunk，完全离线。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  BridgeError,
  DEFAULT_MODEL_ID,
  LlmBridge,
  contentToText,
  createLlmBridge,
  createMapperState,
  isNoAdapterFinish,
  mapStreamChunk,
  stateToMessage,
  toDshMessages,
  toDshTools,
  toOpenAiFinishReason,
  toOpenAiUsage,
  writeError,
} from '../lib/llm-bridge.js'
import { mockReq, mockRes } from './helpers.mjs'

/** 假 DSH llm：listProviders/listModels/stream 都可控。 */
function fakeLlm({ providers = [{ id: 'opencode-go', name: 'OpenCode Go' }], models = { 'opencode-go': [{ id: 'deepseek-v4.1-flash' }] }, chunks } = {}) {
  return {
    calls: [],
    stream(options) {
      this.calls.push(options)
      const script = chunks || [
        { type: 'block-start', index: 0, blockType: 'text' },
        { type: 'text-delta', index: 0, text: '你好' },
        { type: 'text-delta', index: 0, text: '，QuantLab' },
        { type: 'usage', usage: { inputTokens: 11, outputTokens: 5, totalTokens: 16 } },
        { type: 'finish', reason: { kind: 'stop' } },
      ]
      return (async function* stream() {
        for (const chunk of script) yield chunk
      })()
    },
    async listProviders() {
      return providers
    },
    async listModels(provider) {
      return models[provider] || []
    },
  }
}

function bridgeWith(llm, options = {}) {
  return createLlmBridge({
    getLlm: () => llm,
    getDefaultModel: () => null,
    ...options,
  })
}

/** 发一次 /v1/chat/completions。 */
async function postCompletions(bridge, body) {
  const req = mockReq({ method: 'POST', url: '/dsh-quantlab/llm/v1/chat/completions', body })
  const res = mockRes()
  await bridge.handleCompletions(req, res)
  return res
}

/** 把 SSE body 解析成事件数组（去掉 [DONE]）。 */
function parseSse(text) {
  return text
    .split('\n\n')
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => {
      assert.ok(block.startsWith('data: '), `非法 SSE 块：${block.slice(0, 60)}`)
      const payload = block.slice(6)
      return payload === '[DONE]' ? '[DONE]' : JSON.parse(payload)
    })
}

test('contentToText：字符串 / parts 数组 / 图片占位', () => {
  assert.equal(contentToText('hi'), 'hi')
  assert.equal(contentToText([{ type: 'text', text: 'a' }, { type: 'text', text: 'b' }]), 'ab')
  assert.equal(contentToText([{ type: 'image_url', image_url: { url: 'x' } }]), '[图片]')
  assert.equal(contentToText(null), '')
})

test('toDshMessages：system/user/assistant(tool_calls)/tool 都转成 DSH 消息', () => {
  const messages = toDshMessages(
    [
      { role: 'system', content: '你是量化助手' },
      { role: 'user', content: '600519.SH 最近怎么样？' },
      {
        role: 'assistant',
        content: '',
        tool_calls: [{ id: 'call_1', type: 'function', function: { name: 'quantlab_market_bars', arguments: '{"symbol":"600519.SH"}' } }],
      },
      { role: 'tool', tool_call_id: 'call_1', content: '{"ok":true}' },
    ],
    { provider: 'opencode-go', model: 'deepseek-v4.1-flash' },
  )
  assert.equal(messages.length, 4)
  assert.equal(messages[0].role, 'system')
  assert.deepEqual(messages[0].source, { kind: 'system-prompt' })
  assert.deepEqual(messages[0].content, [{ type: 'text', text: '你是量化助手' }])
  assert.equal(messages[1].role, 'user')
  assert.deepEqual(messages[1].source, { kind: 'user' })
  assert.equal(messages[2].role, 'assistant')
  assert.deepEqual(messages[2].source, { kind: 'model', provider: 'opencode-go', model: 'deepseek-v4.1-flash' })
  assert.equal(messages[2].content.length, 1)
  assert.deepEqual(messages[2].content[0], {
    type: 'tool-call',
    id: 'call_1',
    name: 'quantlab_market_bars',
    arguments: '{"symbol":"600519.SH"}',
  })
  assert.equal(messages[3].role, 'tool')
  assert.equal(messages[3].toolCallId, 'call_1')
  assert.deepEqual(messages[3].source, { kind: 'tool', callId: 'call_1' })
  assert.deepEqual(messages[3].content, [{ type: 'text', text: '{"ok":true}' }])
  for (const message of messages) assert.equal(typeof message.id, 'string')
})

test('toDshTools：OpenAI function 工具 → ToolSchema', () => {
  const tools = toDshTools([
    { type: 'function', function: { name: 'quantlab_api', description: '调平台', parameters: { type: 'object', properties: { path: { type: 'string' } } } } },
    { type: 'function', function: { name: 'no_params' } },
    { type: 'function', function: { name: '' } },
    null,
  ])
  assert.equal(tools.length, 2)
  assert.deepEqual(tools[0], {
    name: 'quantlab_api',
    description: '调平台',
    parameters: { type: 'object', properties: { path: { type: 'string' } } },
  })
  assert.deepEqual(tools[1].parameters, { type: 'object', properties: {} })
})

test('mapStreamChunk：text / reasoning / tool-call / finish / usage 映射', () => {
  const state = createMapperState()
  const events = []
  for (const chunk of [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text: '你好' },
    { type: 'reasoning-delta', index: 1, text: '想一想' },
    { type: 'tool-call-delta', index: 2, id: 'call_9', name: 'quantlab_status', argumentsDelta: '' },
    { type: 'tool-call-delta', index: 2, id: 'call_9', argumentsDelta: '{"a":1}' },
    { type: 'usage', usage: { inputTokens: 7, outputTokens: 3, totalTokens: 10 } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
  ]) {
    events.push(...mapStreamChunk(state, chunk))
  }
  assert.equal(events[0].choices[0].delta.content, '你好')
  assert.equal(events[1].choices[0].delta.reasoning_content, '想一想')
  const toolDeltas = events.filter((event) => event.choices[0] && event.choices[0].delta.tool_calls).map((event) => event.choices[0].delta.tool_calls[0])
  assert.equal(toolDeltas.length, 2)
  assert.equal(toolDeltas[0].id, 'call_9')
  assert.equal(toolDeltas[0].function.name, 'quantlab_status')
  assert.equal(toolDeltas[0].function.arguments, '')
  assert.equal(toolDeltas[1].function.arguments, '{"a":1}')
  assert.equal(toolDeltas[1].id, undefined, '只有第一块带 id，避免客户端重复拼接')
  assert.equal(toolDeltas[1].function.name, undefined, '只有第一块带 name')
  assert.equal(events[events.length - 1].choices[0].finish_reason, 'tool_calls')
  assert.equal(state.usage.totalTokens, 10)

  const message = stateToMessage(state)
  assert.equal(message.role, 'assistant')
  assert.equal(message.content, '你好')
  assert.equal(message.reasoning_content, '想一想')
  assert.equal(message.tool_calls.length, 1)
  assert.deepEqual(message.tool_calls[0], {
    id: 'call_9',
    type: 'function',
    function: { name: 'quantlab_status', arguments: '{"a":1}' },
  })
})

test('toOpenAiFinishReason / toOpenAiUsage', () => {
  assert.equal(toOpenAiFinishReason({ kind: 'stop' }), 'stop')
  assert.equal(toOpenAiFinishReason({ kind: 'tool-calls' }), 'tool_calls')
  assert.equal(toOpenAiFinishReason({ kind: 'max-tokens' }), 'length')
  assert.equal(toOpenAiFinishReason({ kind: 'error', failure: { message: 'x', code: 'AUTH' } }), 'error')
  assert.equal(toOpenAiFinishReason(undefined), 'stop')
  assert.deepEqual(toOpenAiUsage({ inputTokens: 3, outputTokens: 2 }), { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 })
  assert.equal(toOpenAiUsage(null), undefined)
})

test('resolveTarget：配置 model 写成 provider/model 时直接用', async () => {
  const llm = fakeLlm()
  const bridge = bridgeWith(llm, { model: 'opencode-go/deepseek-v4.1-flash' })
  const target = await bridge.resolveTarget()
  assert.deepEqual(target, { provider: 'opencode-go', model: 'deepseek-v4.1-flash' })
})

test('resolveTarget：没配置时用 agentDefaultModel.currentSelection()', async () => {
  const llm = fakeLlm()
  const bridge = createLlmBridge({
    getLlm: () => llm,
    getDefaultModel: () => ({ currentSelection: () => ({ provider: 'opencode-go', model: 'deepseek-v4.1-flash', reasoningEffort: 'high' }) }),
  })
  const target = await bridge.resolveTarget()
  assert.deepEqual(target, { provider: 'opencode-go', model: 'deepseek-v4.1-flash', reasoningEffort: 'high' })
})

test('resolveTarget：只有 model 时借默认 provider；再不行借 listProviders 第一个', async () => {
  const llm = fakeLlm()
  const withSelection = createLlmBridge({
    getLlm: () => llm,
    getDefaultModel: () => ({ currentSelection: () => ({ provider: 'opencode-go', model: 'other' }) }),
    model: 'deepseek-v4.1-flash',
  })
  assert.deepEqual(await withSelection.resolveTarget(), { provider: 'opencode-go', model: 'deepseek-v4.1-flash' })

  const bridge = bridgeWith(llm, { model: 'deepseek-v4.1-flash' })
  assert.deepEqual(await bridge.resolveTarget(), { provider: 'opencode-go', model: 'deepseek-v4.1-flash' })
})

test('resolveTarget：llm 不可用 → 503 BridgeError；解析不出模型 → 502', async () => {
  const noLlm = createLlmBridge({ getLlm: () => null })
  await assert.rejects(() => noLlm.resolveTarget(), (error) => {
    assert.ok(error instanceof BridgeError)
    assert.equal(error.status, 503)
    assert.equal(error.code, 'llm_unavailable')
    return true
  })
  assert.equal(noLlm.available(), false)

  const empty = createLlmBridge({ getLlm: () => fakeLlm({ providers: [] }) })
  await assert.rejects(() => empty.resolveTarget(), (error) => {
    assert.equal(error.status, 502)
    assert.equal(error.code, 'no_model')
    return true
  })
})

test('resolveTarget：模型清单缓存 60s，invalidate 后可再解析', async () => {
  let now = 1_000
  let calls = 0
  const llm = {
    stream: () => (async function* () {})(),
    async listProviders() {
      calls += 1
      return [{ id: 'p1' }]
    },
    async listModels() {
      return [{ id: 'm1' }]
    },
  }
  const bridge = createLlmBridge({ getLlm: () => llm, now: () => now })
  assert.deepEqual(await bridge.resolveTarget(), { provider: 'p1', model: 'm1' })
  assert.deepEqual(await bridge.resolveTarget(), { provider: 'p1', model: 'm1' })
  assert.equal(calls, 1, '60s 内应命中缓存')
  now += 61_000
  await bridge.resolveTarget()
  assert.equal(calls, 2)
  bridge.invalidate()
  await bridge.resolveTarget()
  assert.equal(calls, 3)
})

test('POST /v1/chat/completions（流式）：SSE data 块 + [DONE]，含 content/finish/usage', async () => {
  const llm = fakeLlm()
  const bridge = bridgeWith(llm, { model: 'opencode-go/deepseek-v4.1-flash' })
  const res = await postCompletions(bridge, {
    model: 'dsh-default',
    stream: true,
    messages: [{ role: 'user', content: '你好' }],
  })
  assert.equal(res.statusCode, 200)
  assert.match(String(res.headers['content-type']), /text\/event-stream/)
  const events = parseSse(res.body)
  assert.equal(events[events.length - 1], '[DONE]')
  const chunks = events.slice(0, -1)
  assert.equal(chunks[0].object, 'chat.completion.chunk')
  assert.equal(chunks[0].choices[0].delta.role, 'assistant')
  const text = chunks.map((chunk) => (chunk.choices[0] && chunk.choices[0].delta && chunk.choices[0].delta.content) || '').join('')
  assert.equal(text, '你好，QuantLab')
  const finish = chunks.find((chunk) => chunk.choices[0] && chunk.choices[0].finish_reason === 'stop')
  assert.ok(finish, '应有 finish_reason=stop 的块')
  const usageChunk = chunks.find((chunk) => chunk.usage)
  assert.deepEqual(usageChunk.usage, { prompt_tokens: 11, completion_tokens: 5, total_tokens: 16 })

  // 送给 DSH 的参数
  const sent = llm.calls[0]
  assert.equal(sent.provider, 'opencode-go')
  assert.equal(sent.model, 'deepseek-v4.1-flash')
  assert.match(sent.sessionId, /^dsh-quantlab-/)
  assert.equal(sent.messages[0].role, 'user')
  assert.deepEqual(sent.messages[0].content, [{ type: 'text', text: '你好' }])
  assert.equal(sent.tools, undefined, '没传 tools 就不带 tools 字段')
  assert.equal(sent.maxTokens, undefined)
})

test('POST /v1/chat/completions（流式）：tools 转换 + tool_calls delta', async () => {
  const llm = fakeLlm({
    chunks: [
      { type: 'block-start', index: 0, blockType: 'tool-call' },
      { type: 'tool-call-delta', index: 0, id: 'call_2', name: 'quantlab_api', argumentsDelta: '{"method":"GET","path":"/api/market/bars"}' },
      { type: 'finish', reason: { kind: 'tool-calls' } },
    ],
  })
  const bridge = bridgeWith(llm, { model: 'opencode-go/deepseek-v4.1-flash' })
  const res = await postCompletions(bridge, {
    stream: true,
    messages: [{ role: 'user', content: '查行情' }],
    tools: [{ type: 'function', function: { name: 'quantlab_api', description: '调平台', parameters: { type: 'object', properties: {} } } }],
    max_tokens: 512,
    temperature: 0.2,
  })
  const chunks = parseSse(res.body).filter((chunk) => chunk !== '[DONE]')
  const toolDelta = chunks.find((chunk) => chunk.choices[0] && chunk.choices[0].delta.tool_calls)
  assert.ok(toolDelta)
  assert.equal(toolDelta.choices[0].delta.tool_calls[0].id, 'call_2')
  assert.equal(toolDelta.choices[0].delta.tool_calls[0].function.name, 'quantlab_api')
  assert.equal(toolDelta.choices[0].delta.tool_calls[0].function.arguments, '{"method":"GET","path":"/api/market/bars"}')
  const finish = chunks.find((chunk) => chunk.choices[0] && chunk.choices[0].finish_reason)
  assert.equal(finish.choices[0].finish_reason, 'tool_calls')
  const sent = llm.calls[0]
  assert.equal(sent.maxTokens, 512)
  assert.equal(sent.temperature, 0.2)
  assert.deepEqual(sent.tools, [{ name: 'quantlab_api', description: '调平台', parameters: { type: 'object', properties: {} } }])
})

test('POST /v1/chat/completions（非流式）：返回标准 choices[0].message', async () => {
  const llm = fakeLlm()
  const bridge = bridgeWith(llm, { model: 'opencode-go/deepseek-v4.1-flash' })
  const res = await postCompletions(bridge, { messages: [{ role: 'user', content: '你好' }] })
  assert.equal(res.statusCode, 200)
  assert.match(String(res.headers['content-type']), /application\/json/)
  const payload = res.json()
  assert.equal(payload.object, 'chat.completion')
  assert.equal(payload.model, 'dsh-default')
  assert.equal(payload.choices[0].message.role, 'assistant')
  assert.equal(payload.choices[0].message.content, '你好，QuantLab')
  assert.equal(payload.choices[0].finish_reason, 'stop')
  assert.equal(payload.usage.total_tokens, 16)
})

test('POST /v1/chat/completions：ctx.llm 不可用 → 503 JSON', async () => {
  const bridge = createLlmBridge({ getLlm: () => null })
  const res = await postCompletions(bridge, { messages: [{ role: 'user', content: 'hi' }] })
  assert.equal(res.statusCode, 503)
  const payload = res.json()
  assert.match(payload.error.message, /DSH 模型服务不可用/)
  assert.equal(payload.error.code, 'llm_unavailable')
})

test('POST /v1/chat/completions：解析不出模型 → 502 JSON', async () => {
  const bridge = createLlmBridge({ getLlm: () => fakeLlm({ providers: [] }) })
  const res = await postCompletions(bridge, { messages: [{ role: 'user', content: 'hi' }] })
  assert.equal(res.statusCode, 502)
  assert.equal(res.json().error.code, 'no_model')
})

test('POST /v1/chat/completions：messages 为空 → 400', async () => {
  const bridge = bridgeWith(fakeLlm())
  const res = await postCompletions(bridge, { messages: [] })
  assert.equal(res.statusCode, 400)
  assert.equal(res.json().error.code, 'invalid_request')
})

test('POST /v1/chat/completions：llm.stream 抛错 → 502 JSON', async () => {
  const llm = fakeLlm()
  llm.stream = () => {
    throw new Error('provider exploded')
  }
  const bridge = bridgeWith(llm, { model: 'opencode-go/deepseek-v4.1-flash' })
  const res = await postCompletions(bridge, { messages: [{ role: 'user', content: 'hi' }] })
  assert.equal(res.statusCode, 502)
  assert.match(res.json().error.message, /provider exploded/)
})

test('GET /v1/models：返回 dsh-default', async () => {
  const bridge = bridgeWith(fakeLlm())
  const req = mockReq({ method: 'GET', url: '/dsh-quantlab/llm/v1/models' })
  const res = mockRes()
  bridge.handleModels(req, res)
  assert.equal(res.statusCode, 200)
  const payload = res.json()
  assert.equal(payload.object, 'list')
  assert.equal(payload.data[0].id, DEFAULT_MODEL_ID)
  assert.equal(payload.data[0].object, 'model')

  const postRes = mockRes()
  bridge.handleModels(mockReq({ method: 'POST' }), postRes)
  assert.equal(postRes.statusCode, 405)
})

test('writeError：标准 OpenAI 错误体', () => {
  const res = mockRes()
  writeError(res, 503, '模型不可用', 'llm_unavailable')
  assert.equal(res.statusCode, 503)
  assert.equal(res.json().error.code, 'llm_unavailable')
})

test('LlmBridge：modelIds / available / llm()', () => {
  const bridge = new LlmBridge({ getLlm: () => fakeLlm(), modelId: 'dsh-default' })
  assert.deepEqual(bridge.modelIds(), ['dsh-default'])
  assert.equal(bridge.available(), true)
  assert.ok(bridge.llm())
  const empty = new LlmBridge({})
  assert.equal(empty.available(), false)
  assert.equal(empty.llm(), null)
})

// ---------------------------------------------------------------------------
// NO_ADAPTER：DSH provider 拓扑热更新后自动换 provider 重试
// ---------------------------------------------------------------------------

/** 第一个 provider 立即 NO_ADAPTER 失败、第二个 provider 正常的假 llm。 */
function flakyLlm() {
  const calls = []
  return {
    calls,
    stream(options) {
      calls.push({ provider: options.provider, model: options.model })
      const failing = options.provider === 'p1'
      return (async function* stream() {
        if (failing) {
          yield {
            type: 'finish',
            reason: { kind: 'error', failure: { message: 'no adapter registered for provider "p1"', code: 'NO_ADAPTER' } },
          }
          return
        }
        yield { type: 'block-start', index: 0, blockType: 'text' }
        yield { type: 'text-delta', index: 0, text: '备用模型OK' }
        yield { type: 'usage', usage: { inputTokens: 4, outputTokens: 3, totalTokens: 7 } }
        yield { type: 'finish', reason: { kind: 'stop' } }
      })()
    },
    async listProviders() {
      return [{ id: 'p1' }, { id: 'p2' }]
    },
    async listModels(provider) {
      return provider === 'p1' ? [{ id: 'm1' }] : [{ id: 'm2' }]
    },
  }
}

test('isNoAdapterFinish：只认 finish{error, code:NO_ADAPTER}', () => {
  assert.equal(isNoAdapterFinish({ type: 'finish', reason: { kind: 'error', failure: { code: 'NO_ADAPTER' } } }), true)
  assert.equal(isNoAdapterFinish({ type: 'finish', reason: { kind: 'error', failure: { code: 'AUTH' } } }), false)
  assert.equal(isNoAdapterFinish({ type: 'finish', reason: { kind: 'stop' } }), false)
  assert.equal(isNoAdapterFinish(null), false)
})

test('NO_ADAPTER：流式请求清缓存换 provider 重试，最终正常出文本', async () => {
  const llm = flakyLlm()
  const bridge = createLlmBridge({ getLlm: () => llm, model: 'p1/m1' })
  const res = await postCompletions(bridge, { stream: true, messages: [{ role: 'user', content: 'hi' }] })
  assert.equal(res.statusCode, 200)
  const chunks = parseSse(res.body).filter((chunk) => chunk !== '[DONE]')
  const text = chunks
    .map((chunk) => (chunk.choices[0] && chunk.choices[0].delta && chunk.choices[0].delta.content) || '')
    .join('')
  assert.equal(text, '备用模型OK')
  assert.equal(llm.calls.length, 2, '应该重试一次')
  assert.equal(llm.calls[0].provider, 'p1')
  assert.equal(llm.calls[1].provider, 'p2')
  assert.equal(llm.calls[1].model, 'm2')
  const finish = chunks.find((chunk) => chunk.choices[0] && chunk.choices[0].finish_reason)
  assert.equal(finish.choices[0].finish_reason, 'stop')
  // 缓存更新成备用目标
  assert.deepEqual(bridge.lastTarget, { provider: 'p2', model: 'm2' })
})

test('NO_ADAPTER：非流式请求同样重试一次并返回正文', async () => {
  const llm = flakyLlm()
  const bridge = createLlmBridge({ getLlm: () => llm, model: 'p1/m1' })
  const res = await postCompletions(bridge, { messages: [{ role: 'user', content: 'hi' }] })
  assert.equal(res.statusCode, 200)
  const payload = res.json()
  assert.equal(payload.choices[0].message.content, '备用模型OK')
  assert.equal(payload.choices[0].finish_reason, 'stop')
  assert.equal(llm.calls.length, 2)
})

test('NO_ADAPTER 且没有备用 provider：保留原始错误（502）', async () => {
  const llm = {
    stream() {
      return (async function* stream() {
        yield { type: 'finish', reason: { kind: 'error', failure: { message: 'no adapter registered', code: 'NO_ADAPTER' } } }
      })()
    },
    async listProviders() {
      return [{ id: 'p1' }]
    },
    async listModels() {
      return [{ id: 'm1' }]
    },
  }
  const bridge = createLlmBridge({ getLlm: () => llm, model: 'p1/m1' })
  const res = await postCompletions(bridge, { messages: [{ role: 'user', content: 'hi' }] })
  assert.equal(res.statusCode, 502)
  assert.equal(res.json().error.code, 'NO_ADAPTER')
})
