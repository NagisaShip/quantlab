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
  pickMetrics,
  requestPlatform,
  safeId,
  summarizeBars,
  truncateText,
  REPORT_FORMATS,
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

function buildTools({ baseUrl = 'http://127.0.0.1:8123', fetch, status, dshOrigin = 'http://127.0.0.1:9000' } = {}) {
  const definitions = buildToolDefinitions({
    defineTool,
    getBaseUrl: () => baseUrl,
    getStatus: () => status || { state: 'running', url: baseUrl, port: 8123, pid: 1, projectDir: 'X', llmBridge: 'ready' },
    getDshOrigin: () => dshOrigin,
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

test('buildToolDefinitions：七个工具、参数 DSL 形状正确', () => {
  const { definitions, byName } = buildTools({ fetch: fakeFetch({}) })
  assert.deepEqual(definitions.map((definition) => definition.name).sort(), [
    'quantlab_api',
    'quantlab_backtest',
    'quantlab_export_report',
    'quantlab_import_github',
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

// ─── F（DSH 体验）：回测报告 / 原生对话卡片 / GitHub 导入 ──────────────────

test('safeId：只接受安全短 id（拼进 URL 的不能带引号/斜杠）', () => {
  assert.equal(safeId('bt-123_ab'), 'bt-123_ab')
  assert.equal(safeId('  abc  '), 'abc')
  for (const bad of ['', '../etc', 'a/b', 'a"b', '<script>', '中文', 'x'.repeat(80), 'a b']) {
    assert.equal(safeId(bad), '', `应拒绝：${bad}`)
  }
})

test('pickMetrics：从嵌套结果里收指标，拿不到就不编', () => {
  const metrics = pickMetrics({ result: { metrics: { total_return_pct: 12.5, sharpe: 1.8, max_drawdown_pct: -6.2, trades: 42 } } })
  assert.deepEqual(metrics, { totalReturnPct: 12.5, sharpe: 1.8, maxDrawdownPct: -6.2, trades: 42 })
  assert.deepEqual(pickMetrics({}), {})
  assert.deepEqual(pickMetrics(null), {})
  assert.deepEqual(REPORT_FORMATS, ['png', 'pdf', 'html'])
})

test('quantlab_export_report：返回摘要 + markdown 图片 + 卡片标记 + 下载链接', async () => {
  const calls = []
  const fetch = fakeFetch(
    {
      '/api/backtests/bt-1/report': { path: 'C:/proj/.quantlab/exports/backtest-bt-1.png' },
      '/api/backtests/bt-1/report.png': { __body: 'PNG', __bytes: 4096 },
      '/api/backtests/bt-1': { result: { metrics: { total_return_pct: 8.5, sharpe: 1.4, trades: 12 } } },
    },
    calls,
  )
  const { byName } = buildTools({ fetch })
  const result = await byName.quantlab_export_report.execute({ backtest_id: 'bt-1', format: 'png' })
  assert.equal(result.ok, true, JSON.stringify(result))
  assert.equal(result.backtestId, 'bt-1')
  assert.equal(result.chartUrl, 'http://127.0.0.1:9000/dsh-quantlab/chart/bt-1.png')
  assert.equal(result.downloadUrl, 'http://127.0.0.1:9000/dsh-quantlab/report/bt-1.png')
  assert.equal(result.localPath, 'C:/proj/.quantlab/exports/backtest-bt-1.png')
  assert.deepEqual(result.metrics, { totalReturnPct: 8.5, sharpe: 1.4, trades: 12 })
  assert.match(result.markdown, /!\[回测 #bt-1 净值曲线\]\(http:\/\/127\.0\.0\.1:9000\/dsh-quantlab\/chart\/bt-1\.png\)/)
  assert.match(result.markdown, /<!-- quantlab-card \{"ok":true/)
  assert.match(result.markdown, /打开回测/)
  // 调了导出接口 + 兜底下载接口 + 详情接口
  assert.ok(calls.some((entry) => entry.method === 'POST' && entry.url.includes('/api/backtests/bt-1/report')))
  assert.ok(calls.some((entry) => entry.method === 'GET' && entry.url.includes('/report.png')))
})

test('quantlab_export_report：报告接口没就绪时给可读错误，并校验 id/format', async () => {
  const { byName } = buildTools({ fetch: fakeFetch({}) })
  const missing = await byName.quantlab_export_report.execute({ backtest_id: 'bt-404' })
  assert.equal(missing.ok, false)
  assert.match(missing.error, /导出失败/)
  assert.match(missing.error, /report-export|报告/)

  const badId = await byName.quantlab_export_report.execute({ backtest_id: '../x' })
  assert.equal(badId.ok, false)
  assert.match(badId.error, /不合法/)

  const badFormat = await byName.quantlab_export_report.execute({ backtest_id: 'bt-1', format: 'exe' })
  assert.equal(badFormat.ok, false)
  assert.match(badFormat.error, /png/)
})

test('quantlab_import_github：成功时给仓库摘要，失败时透出平台原因', async () => {
  const okFetch = fakeFetch({
    '/api/imports/github': {
      ok: true,
      import: {
        id: 'octocat-Hello-World-1',
        name: 'octocat/Hello-World',
        url: 'https://github.com/octocat/Hello-World',
        files: 3,
        bytes: 2048,
        dir: 'octocat-Hello-World-1',
        strategies_imported: [{ slug: 'demo', name: 'Demo' }],
        warnings: [],
      },
    },
  })
  const ok = await buildTools({ fetch: okFetch }).byName.quantlab_import_github.execute({
    url: 'https://github.com/octocat/Hello-World',
  })
  assert.equal(ok.ok, true, JSON.stringify(ok))
  assert.equal(ok.name, 'octocat/Hello-World')
  assert.equal(ok.strategies[0].slug, 'demo')
  assert.match(ok.markdown, /已导入 octocat\/Hello-World/)
  assert.match(ok.markdown, /<!-- quantlab-card/)

  const failFetch = fakeFetch({
    '/api/imports/github': { __status: 400, message: '仓库不存在或不是公开仓库' },
  })
  const failed = await buildTools({ fetch: failFetch }).byName.quantlab_import_github.execute({
    url: 'https://github.com/octocat/Nope',
  })
  assert.equal(failed.ok, false)
  assert.match(failed.error, /仓库不存在/)
})

test('quantlab_backtest：完成时给指标与面板链接，未完成时给 run_id', async () => {
  const completed = fakeFetch({
    '/api/studio/backtest': {
      run_id: 'run-1',
      result: { metrics: { total_return_pct: 3.2, sharpe: 0.9, trades: 5 } },
    },
  })
  const done = await buildTools({ fetch: completed }).byName.quantlab_backtest.execute({
    slug: 'demo', symbol: '600519.SH', exchange: 'stock',
  })
  assert.equal(done.ok, true, JSON.stringify(done))
  assert.equal(done.metrics.totalReturnPct, 3.2)
  assert.match(done.markdown, /打开回测/)
  assert.match(done.markdown, /<!-- quantlab-card/)

  const pending = fakeFetch({ '/api/studio/backtest': { pending: true, run_id: 'run-9', state: 'QUEUED' } })
  const queued = await buildTools({ fetch: pending }).byName.quantlab_backtest.execute({ code: 'pass' })
  assert.equal(queued.ok, true)
  assert.equal(queued.pending, true)
  assert.equal(queued.run_id, 'run-9')

  const empty = await buildTools({ fetch: fakeFetch({}) }).byName.quantlab_backtest.execute({})
  assert.equal(empty.ok, false)
  assert.match(empty.error, /slug|code/)
})

