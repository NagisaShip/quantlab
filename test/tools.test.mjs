/**
 * lib/tools.js 单测：quantlab_api 白名单、服务未启动提示、行情摘要、工具定义形状。
 * defineTool 传最小 shim（真实 DSL 由 DSH 的 @deepseek-ai/dsh-tools 校验）。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  API_ALLOWED_PREFIXES,
  API_DENIED_PATHS,
  API_RESPONSE_LIMIT,
  SERVICE_NOT_RUNNING,
  buildToolDefinitions,
  checkApiRequest,
  requestPlatform,
  summarizeBars,
  truncateText,
} from '../lib/tools.js'

/** 最小 defineTool shim：保留 name/parameters/output/execute。 */
const defineTool = (spec) => spec

/** 假 fetch：按 url 返回固定 JSON。 */
function fakeFetch(routes, calls = []) {
  return async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || 'GET', headers: init.headers || {}, body: init.body })
    const path = String(url).replace(/^https?:\/\/[^/]+/, '')
    for (const [pattern, payload] of Object.entries(routes)) {
      if (path.startsWith(pattern)) {
        const status = payload && payload.__status ? payload.__status : 200
        const body = payload && payload.__body !== undefined ? payload.__body : JSON.stringify(payload)
        return { ok: status >= 200 && status < 300, status, async text() { return typeof body === 'string' ? body : JSON.stringify(body) } }
      }
    }
    return { ok: false, status: 404, async text() { return JSON.stringify({ message: '接口不存在' }) } }
  }
}

function buildTools({ baseUrl = 'http://127.0.0.1:8123', fetch, status } = {}) {
  const definitions = buildToolDefinitions({
    defineTool,
    getBaseUrl: () => baseUrl,
    getStatus: () => status || { state: 'running', url: baseUrl, port: 8123, pid: 1, projectDir: 'X', llmBridge: 'ready' },
    fetch,
    log: () => {},
  })
  const byName = Object.fromEntries(definitions.map((definition) => [definition.name, definition]))
  return { definitions, byName }
}

test('checkApiRequest：白名单前缀放行，危险端点/方法/路径拒绝', () => {
  assert.deepEqual(checkApiRequest('GET', '/api/market/bars?symbol=600519.SH'), {
    ok: true,
    method: 'GET',
    path: '/api/market/bars?symbol=600519.SH',
  })
  assert.equal(checkApiRequest('POST', '/api/backtests').ok, true)
  for (const prefix of API_ALLOWED_PREFIXES) assert.equal(checkApiRequest('GET', `${prefix}x`).ok, true, prefix)

  // 方法
  assert.equal(checkApiRequest('DELETE', '/api/strategies/1').ok, false)
  assert.equal(checkApiRequest('PUT', '/api/strategies/1').ok, false)
  assert.equal(checkApiRequest('', '/api/market/bars').ok, false)

  // 危险端点
  for (const denied of API_DENIED_PATHS) {
    const result = checkApiRequest('POST', denied)
    assert.equal(result.ok, false, denied)
    assert.match(result.error, /禁止|白名单/)
  }
  assert.equal(checkApiRequest('POST', '/api/local/shutdown').ok, false)
  assert.equal(checkApiRequest('GET', '/api/auth/me').ok, false)
  assert.equal(checkApiRequest('POST', '/api/admin/users').ok, false)
  assert.equal(checkApiRequest('GET', '/api/local/info').ok, false)

  // 路径形态
  assert.equal(checkApiRequest('GET', '').ok, false)
  assert.equal(checkApiRequest('GET', 'api/market/bars').ok, false)
  assert.equal(checkApiRequest('GET', 'http://evil.example/api/market/bars').ok, false)
  assert.equal(checkApiRequest('GET', '//evil.example/api/market/bars').ok, false)
  assert.equal(checkApiRequest('GET', '/api/market/../../local/shutdown').ok, false)
  assert.equal(checkApiRequest('GET', '/api\\market\\bars').ok, false)
  assert.equal(checkApiRequest('GET', '/api/somethingelse').ok, false)
})

test('truncateText：超过 8KB 截断并标注', () => {
  const short = 'x'.repeat(100)
  assert.equal(truncateText(short), short)
  const long = '中'.repeat(API_RESPONSE_LIMIT)
  const cut = truncateText(long)
  assert.ok(Buffer.byteLength(cut, 'utf8') <= API_RESPONSE_LIMIT + 40)
  assert.match(cut, /已截断到 8KB/)
})

test('buildToolDefinitions：四个工具、参数 DSL 形状正确', () => {
  const { definitions, byName } = buildTools({ fetch: fakeFetch({}) })
  assert.deepEqual(definitions.map((definition) => definition.name).sort(), [
    'quantlab_api',
    'quantlab_market_bars',
    'quantlab_search_stock',
    'quantlab_status',
  ])
  const bars = byName.quantlab_market_bars
  assert.equal(bars.parameters.symbol.required, true)
  assert.equal(bars.parameters.limit.type, 'number')
  assert.equal(bars.output.schema.type, 'json')
  const rendered = bars.output.render({}, { ok: true })
  assert.deepEqual(rendered, [{ type: 'text', text: '{\n  "ok": true\n}' }])
  assert.deepEqual(byName.quantlab_status.parameters, {})
  assert.equal(byName.quantlab_api.parameters.body.type, 'json')
})

test('服务未启动：工具返回中文错误（docs 4.5 的文案）', async () => {
  const { byName } = buildTools({ baseUrl: '', fetch: fakeFetch({}) })
  for (const name of ['quantlab_market_bars', 'quantlab_search_stock']) {
    const args = name === 'quantlab_market_bars' ? { symbol: '600519.SH' } : { q: '茅台' }
    const result = await byName[name].execute(args)
    assert.equal(result.ok, false)
    assert.equal(result.error, SERVICE_NOT_RUNNING)
    assert.match(result.error, /QuantLab 服务未启动/)
  }
  const apiResult = await byName.quantlab_api.execute({ method: 'GET', path: '/api/market/bars' })
  assert.equal(apiResult.ok, false)
  assert.equal(apiResult.error, SERVICE_NOT_RUNNING)
})

test('quantlab_status：汇总后端状态与模型桥', async () => {
  const { byName } = buildTools({
    status: { state: 'running', url: 'http://127.0.0.1:8123', port: 8123, pid: 42, projectDir: 'C:/p', llmBridge: 'ready', logTail: 'a\nb\nc' },
    fetch: fakeFetch({}),
  })
  const result = await byName.quantlab_status.execute({})
  assert.equal(result.ok, true)
  assert.equal(result.state, 'running')
  assert.equal(result.port, 8123)
  assert.equal(result.pid, 42)
  assert.equal(result.llmBridge, 'ready')
  assert.equal(result.logTail, 'a\nb\nc')

  const stopped = buildTools({
    status: { state: 'stopped', url: '', port: 0, pid: 0, projectDir: 'C:/p', llmBridge: 'unavailable', logTail: '', error: 'x' },
    fetch: fakeFetch({}),
  })
  const stoppedResult = await stopped.byName.quantlab_status.execute({})
  assert.equal(stoppedResult.ok, false)
  assert.equal(stoppedResult.error, 'x')
})

test('quantlab_market_bars：请求 /api/market/bars 并给出末 3 根 + 统计', async () => {
  const calls = []
  const bars = [100, 102, 101, 105, 110, 108].map((close, index) => ({
    t: 1_700_000_000_000 + index * 3_600_000,
    o: close - 1,
    h: close + 2,
    l: close - 2,
    c: close,
    v: 10 + index,
    closed: true,
  }))
  const fetch = fakeFetch(
    { '/api/market/bars': { symbol: '600519.SH', timeframe: '1d', source: 'exchange', count: bars.length, bars } },
    calls,
  )
  const { byName } = buildTools({ fetch })
  const result = await byName.quantlab_market_bars.execute({ symbol: '600519.SH', timeframe: '1d', exchange: 'stock', limit: 50 })
  assert.equal(result.ok, true)
  assert.equal(result.symbol, '600519.SH')
  assert.equal(result.count, 6)
  assert.equal(result.stats.lastClose, 108)
  assert.equal(result.stats.firstClose, 100)
  assert.equal(result.stats.changePct, 8)
  assert.equal(result.stats.maxClose, 110)
  assert.equal(result.stats.minClose, 100)
  assert.equal(result.last3.length, 3)
  assert.equal(result.last3[2].close, 108)
  assert.match(result.last3[0].time, /^\d{4}-\d{2}-\d{2}T/)

  const url = calls[0].url
  assert.match(url, /\/api\/market\/bars\?/)
  assert.match(url, /exchange=stock/)
  assert.match(url, /symbol=600519\.SH/)
  assert.match(url, /limit=50/)
})

test('quantlab_market_bars：limit 会被夹到 10–300，symbol 为空报错', async () => {
  const calls = []
  const { byName } = buildTools({ fetch: fakeFetch({ '/api/market/bars': { bars: [] } }, calls) })
  await byName.quantlab_market_bars.execute({ symbol: 'BTC/USDT', limit: 1 })
  assert.match(calls[0].url, /limit=10/)
  const tooBig = await buildTools({ fetch: fakeFetch({ '/api/market/bars': { bars: [] } }, calls) })
  await tooBig.byName.quantlab_market_bars.execute({ symbol: 'BTC/USDT', limit: 9999 })
  assert.match(calls[1].url, /limit=300/)
  const empty = await byName.quantlab_market_bars.execute({})
  assert.equal(empty.ok, false)
  assert.match(empty.error, /symbol/)
})

test('quantlab_market_bars：平台返回错误 → {ok:false,error}', async () => {
  const fetch = fakeFetch({ '/api/market/bars': { __status: 503, __body: JSON.stringify({ message: '交易所暂时不可用' }) } })
  const { byName } = buildTools({ fetch })
  const result = await byName.quantlab_market_bars.execute({ symbol: '600519.SH' })
  assert.equal(result.ok, false)
  assert.match(result.error, /交易所暂时不可用/)
  assert.match(result.error, /HTTP 503/)
})

test('quantlab_search_stock：走 /api/market/stocks/search', async () => {
  const calls = []
  const fetch = fakeFetch(
    { '/api/market/stocks/search': { items: [{ symbol: '600519.SH', name: '贵州茅台', market: 'SH' }] } },
    calls,
  )
  const { byName } = buildTools({ fetch })
  const result = await byName.quantlab_search_stock.execute({ q: '茅台' })
  assert.equal(result.ok, true)
  assert.equal(result.items[0].symbol, '600519.SH')
  assert.match(calls[0].url, /q=%E8%8C%85%E5%8F%B0/)
  const empty = await byName.quantlab_search_stock.execute({ q: '' })
  assert.equal(empty.ok, false)
})

test('quantlab_api：白名单内 GET/POST 成功，POST 带本地写头', async () => {
  const calls = []
  const fetch = fakeFetch({ '/api/strategies/list': { items: [{ id: 's1' }] }, '/api/backtests': { id: 'bt1' } }, calls)
  const { byName } = buildTools({ fetch })
  const got = await byName.quantlab_api.execute({ method: 'GET', path: '/api/strategies/list' })
  assert.equal(got.ok, true)
  assert.deepEqual(got.json, { items: [{ id: 's1' }] })

  const posted = await byName.quantlab_api.execute({ method: 'POST', path: '/api/backtests', body: { symbol: '600519.SH' } })
  assert.equal(posted.ok, true)
  assert.deepEqual(posted.json, { id: 'bt1' })
  assert.equal(calls[1].method, 'POST')
  assert.equal(calls[1].headers['x-requested-with'], 'quantlab-desktop')
  assert.equal(calls[1].body, JSON.stringify({ symbol: '600519.SH' }))
  assert.equal(calls[0].headers['x-requested-with'], undefined, 'GET 不带写头')
})

test('quantlab_api：白名单外直接拒绝，不发请求', async () => {
  const calls = []
  const { byName } = buildTools({ fetch: fakeFetch({}, calls) })
  for (const [method, path] of [
    ['POST', '/api/local/shutdown'],
    ['POST', '/api/auth/logout'],
    ['DELETE', '/api/strategies/1'],
    ['GET', '/api/unknown/x'],
  ]) {
    const result = await byName.quantlab_api.execute({ method, path })
    assert.equal(result.ok, false, `${method} ${path}`)
  }
  assert.equal(calls.length, 0)
})

test('quantlab_api：返回超过 8KB 时截断并给提示', async () => {
  const big = { rows: 'x'.repeat(12 * 1024) }
  const { byName } = buildTools({ fetch: fakeFetch({ '/api/data/list': big }) })
  const result = await byName.quantlab_api.execute({ method: 'GET', path: '/api/data/list' })
  assert.equal(result.ok, true)
  assert.equal(result.truncated, true)
  assert.match(result.note, /8KB/)
  assert.match(result.json, /已截断/)
})

test('requestPlatform：baseUrl 为空 / 连不上 / 非 JSON 都给出中文错误', async () => {
  await assert.rejects(() => requestPlatform({ fetch: fakeFetch({}), baseUrl: '', path: '/api/market/bars' }), /服务未启动/)
  await assert.rejects(
    () =>
      requestPlatform({
        baseUrl: 'http://127.0.0.1:1',
        path: '/api/market/bars',
        fetch: async () => {
          throw new Error('fetch failed')
        },
      }),
    /连不上 QuantLab 后端/,
  )
  await assert.rejects(
    () =>
      requestPlatform({
        baseUrl: 'http://127.0.0.1:8123',
        path: '/api/market/bars',
        fetch: async () => ({ ok: true, status: 200, async text() { return '<html>' } }),
      }),
    /不是 JSON/,
  )
})

test('summarizeBars：空数据也安全', () => {
  const summary = summarizeBars({})
  assert.equal(summary.ok, true)
  assert.equal(summary.count, 0)
  assert.deepEqual(summary.last3, [])
  assert.equal(summary.stats.firstClose, null)
})
