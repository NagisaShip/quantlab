/**
 * dsh-quantlab · DSH 工具（quantlab_status / quantlab_market_bars /
 * quantlab_search_stock / quantlab_api）
 * ============================================================================
 * 全部工具都走本地后端 HTTP（baseUrl 由宿主注入），不直接 import 平台代码。
 *
 * `defineTool` 从 `@deepseek-ai/dsh-tools` 取（宿主在 apply 里动态 import 后传进来）；
 * 单测里传一个最小 shim 就能跑（见 test/tools.test.mjs）。
 *
 * 参数 DSL（dsh-tools 的写法，不是整块 JSON Schema）：
 *   parameters: { 字段名: { type: 'string'|'number'|'boolean'|'json'|'object'|'array', required?: true, description? } }
 *   output: { schema: { type: 'json'|'string'|... }, render: (_args, value) => [{ type: 'text', text }] }
 */

/** 服务没起来时统一的工具返回值（中文，模型能直接读懂）。 */
export const SERVICE_NOT_RUNNING = 'QuantLab 服务未启动（打开侧栏 QuantLab 面板启动）'

/** quantlab_api 允许的路径前缀（docs 4.5）。 */
export const API_ALLOWED_PREFIXES = Object.freeze([
  '/api/market/',
  '/api/strategies/',
  '/api/backtests/',
  '/api/research/',
  '/api/lab/',
  '/api/factors/',
  '/api/data/',
  '/api/models/',
])

/** quantlab_api 明确禁止的路径（即便将来前缀放宽也不放行）。 */
export const API_DENIED_PATHS = Object.freeze(['/api/local/shutdown', '/api/auth', '/api/admin'])

/** quantlab_api 允许的方法。 */
export const API_ALLOWED_METHODS = Object.freeze(['GET', 'POST'])

/** quantlab_api 返回 JSON 的截断字节数（8KB）。 */
export const API_RESPONSE_LIMIT = 8 * 1024

/** 一次平台 HTTP 调用的默认超时。 */
const DEFAULT_TIMEOUT_MS = 20_000

/**
 * 校验 quantlab_api 的 method/path 是否在白名单内。
 * @param {string} method
 * @param {string} path
 * @returns {{ok: true, method: string, path: string} | {ok: false, error: string}}
 */
export function checkApiRequest(method, path) {
  const verb = String(method || '').toUpperCase()
  if (!API_ALLOWED_METHODS.includes(verb)) {
    return { ok: false, error: `quantlab_api 只允许 GET / POST，收到 ${verb || '（空）'}` }
  }
  const raw = String(path || '').trim()
  if (!raw) return { ok: false, error: 'quantlab_api 需要 path，例如 /api/market/bars?symbol=BTC/USDT' }
  if (!raw.startsWith('/')) return { ok: false, error: 'path 必须以 / 开头（本机相对路径）' }
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(raw) || raw.startsWith('//')) {
    return { ok: false, error: '只接受本机相对路径，不接受绝对 URL' }
  }
  if (raw.includes('\\') || raw.includes('..')) {
    return { ok: false, error: 'path 不能包含 .. 或反斜杠' }
  }
  const pathname = raw.split('?')[0].split('#')[0]
  for (const denied of API_DENIED_PATHS) {
    if (pathname === denied || pathname.startsWith(`${denied}/`) || pathname.startsWith(denied)) {
      return { ok: false, error: `禁止调用 ${denied}（不在 quantlab_api 白名单内）` }
    }
  }
  const allowed = API_ALLOWED_PREFIXES.some((prefix) => pathname === prefix.slice(0, -1) || pathname.startsWith(prefix))
  if (!allowed) {
    return {
      ok: false,
      error: `path 不在白名单内；允许的前缀：${API_ALLOWED_PREFIXES.join('、')}（禁止 ${API_DENIED_PATHS.join('、')}）`,
    }
  }
  return { ok: true, method: verb, path: raw }
}

/** 截断字符串到 maxBytes（按 UTF-8 字节，保留 JSON 可读性）。 */
export function truncateText(text, maxBytes = API_RESPONSE_LIMIT) {
  const value = typeof text === 'string' ? text : JSON.stringify(text)
  if (!value) return ''
  if (Buffer.byteLength(value, 'utf8') <= maxBytes) return value
  const truncated = Buffer.from(value, 'utf8').subarray(0, maxBytes).toString('utf8')
  return `${truncated}\n…（已截断到 ${Math.round(maxBytes / 1024)}KB）`
}

/**
 * 平台 HTTP 请求（返回解析后的 JSON；失败抛带中文信息的 Error）。
 * @param {object} options
 * @param {Function} options.fetch fetch 实现
 * @param {string} options.baseUrl 后端根地址（如 http://127.0.0.1:8000）
 * @param {string} options.method
 * @param {string} options.path 相对路径（可带 query）
 * @param {object} [options.body]
 * @param {number} [options.timeoutMs]
 */
export async function requestPlatform({ fetch, baseUrl, method = 'GET', path, body, timeoutMs = DEFAULT_TIMEOUT_MS }) {
  if (!baseUrl) throw new Error(SERVICE_NOT_RUNNING)
  if (typeof fetch !== 'function') throw new Error('没有可用的 fetch（Node 版本过低？）')
  const url = `${String(baseUrl).replace(/\/+$/, '')}${path.startsWith('/') ? '' : '/'}${path}`
  const verb = String(method || 'GET').toUpperCase()
  const controller = typeof AbortController === 'function' ? new AbortController() : null
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null
  if (timer && typeof timer.unref === 'function') timer.unref()
  let response
  try {
    response = await fetch(url, {
      method: verb,
      headers: {
        accept: 'application/json',
        ...(body === undefined
          ? {}
          : {
              'content-type': 'application/json; charset=utf-8',
              // 本地模式写接口要求这个头，否则 403（docs 4.4）
              'x-requested-with': 'quantlab-desktop',
            }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      ...(controller ? { signal: controller.signal } : {}),
    })
  } catch (error) {
    const message = String((error && error.message) || error)
    if (message.includes('abort') || message.includes('Abort')) throw new Error(`请求 QuantLab 超时（${timeoutMs}ms）：${path}`)
    throw new Error(`连不上 QuantLab 后端（${baseUrl}）：${message}`)
  } finally {
    if (timer) clearTimeout(timer)
  }
  const text = typeof response.text === 'function' ? await response.text() : ''
  let json = null
  try {
    json = text ? JSON.parse(text) : null
  } catch {
    json = null
  }
  if (response.ok !== true) {
    const detail =
      (json && (json.message || (json.error && (json.error.message || json.error)) || json.code)) ||
      text.slice(0, 300) ||
      `HTTP ${response.status}`
    throw new Error(`QuantLab 接口失败（${verb} ${path} → HTTP ${response.status}）：${typeof detail === 'string' ? detail : JSON.stringify(detail)}`)
  }
  if (json === null) throw new Error(`QuantLab 接口返回的不是 JSON（${verb} ${path}）`)
  return json
}

/** 从 bars 接口响应里挑「末 3 根 + 统计」，让模型能直接引用。 */
export function summarizeBars(payload) {
  const bars = Array.isArray(payload && payload.bars) ? payload.bars : []
  const numbers = (key) => bars.map((bar) => Number(bar && bar[key])).filter((n) => Number.isFinite(n))
  const closes = numbers('c')
  const first = closes.length ? closes[0] : null
  const last = closes.length ? closes[closes.length - 1] : null
  const changePct = first ? ((last - first) / first) * 100 : null
  const pick = (bar) =>
    bar
      ? {
          t: bar.t,
          time: typeof bar.t === 'number' ? new Date(bar.t).toISOString() : bar.t,
          open: bar.o,
          high: bar.h,
          low: bar.l,
          close: bar.c,
          volume: bar.v,
          closed: bar.closed,
        }
      : null
  return {
    ok: true,
    symbol: payload && payload.symbol,
    timeframe: payload && payload.timeframe,
    source: payload && payload.source,
    count: bars.length,
    stale: payload && payload.stale,
    warning: payload && payload.warning,
    error: payload && payload.error,
    stats: {
      firstClose: first,
      lastClose: last,
      changePct: changePct === null ? null : Number(changePct.toFixed(4)),
      maxClose: closes.length ? Math.max(...closes) : null,
      minClose: closes.length ? Math.min(...closes) : null,
      avgVolume: numbers('v').length ? Number((numbers('v').reduce((sum, n) => sum + n, 0) / numbers('v').length).toFixed(4)) : null,
    },
    last3: bars.slice(-3).map(pick),
  }
}

/** 工具未启动时的统一结果。 */
function notRunning() {
  return { ok: false, error: SERVICE_NOT_RUNNING }
}

/**
 * 构建 4 个 quantlab_* 工具定义。
 *
 * @param {object} options
 * @param {Function} options.defineTool `@deepseek-ai/dsh-tools` 的 defineTool（单测可传 shim）
 * @param {Function} options.getBaseUrl 返回当前后端根地址（未运行返回 ''）
 * @param {Function} options.getStatus 返回宿主状态快照（quantlab_status 用）
 * @param {Function} [options.fetch] fetch 实现（默认全局 fetch）
 * @param {Function} [options.log] 诊断日志
 * @returns {Array<object>} 已 defineTool 包装好的工具定义
 */
export function buildToolDefinitions({ defineTool, getBaseUrl, getStatus, fetch = globalThis.fetch, log = () => {} }) {
  if (typeof defineTool !== 'function') throw new Error('buildToolDefinitions 需要 defineTool')
  const baseUrl = () => {
    try {
      return String(getBaseUrl() || '')
    } catch {
      return ''
    }
  }
  const call = (method, path, body) => requestPlatform({ fetch, baseUrl: baseUrl(), method, path, body })

  const statusTool = defineTool({
    name: 'quantlab_status',
    description:
      '查看 QuantLab（本地量化研究台）服务状态：是否运行、后端地址/端口、项目目录、DSH 模型桥是否可用。' +
      '其它 quantlab_* 工具报「服务未启动」时先用它确认状态。',
    parameters: {},
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute() {
      let status = null
      try {
        status = typeof getStatus === 'function' ? getStatus() : null
      } catch (error) {
        return { ok: false, error: `读取状态失败：${(error && error.message) || error}` }
      }
      if (!status || typeof status !== 'object') return { ok: false, error: '插件状态不可用' }
      const tail = typeof status.logTail === 'string' ? status.logTail.split('\n').slice(-12).join('\n') : ''
      return {
        ok: status.state === 'running',
        state: status.state,
        url: status.url,
        port: status.port,
        pid: status.pid,
        projectDir: status.projectDir,
        startedAt: status.startedAt,
        llmBridge: status.llmBridge,
        error: status.error,
        logTail: tail ? truncateText(tail, 2000) : '',
      }
    },
  })

  const barsTool = defineTool({
    name: 'quantlab_market_bars',
    description:
      '读 QuantLab 行情 K 线，返回收盘价序列摘要（末 3 根 + 首末收盘/涨跌幅/最高最低/均量）。' +
      '股票用 exchange="stock"（A 股代码如 600519.SH / 000001.SZ，美股如 AAPL.US）；加密用 exchange="binance" 等。' +
      '服务没启动时会返回 {ok:false,error}。',
    parameters: {
      symbol: { type: 'string', required: true, description: '标的：600519.SH、AAPL.US、BTC/USDT 等' },
      timeframe: { type: 'string', description: '周期：1m/5m/15m/30m/1h/1d/1w/1M（默认 1d）' },
      exchange: { type: 'string', description: '场所：stock（股票，默认）/ binance / okx / bybit' },
      limit: { type: 'number', description: 'K 线根数（10–300，默认 120）' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      const symbol = String((args && args.symbol) || '').trim()
      if (!symbol) return { ok: false, error: 'symbol 不能为空' }
      const timeframe = String((args && args.timeframe) || '1d').trim() || '1d'
      const exchange = String((args && args.exchange) || 'stock').trim() || 'stock'
      const limit = Math.max(10, Math.min(300, Math.floor(Number(args && args.limit) || 120)))
      if (!baseUrl()) return notRunning()
      try {
        const query = new URLSearchParams({ exchange, symbol, timeframe, limit: String(limit) })
        const payload = await call('GET', `/api/market/bars?${query.toString()}`)
        return summarizeBars(payload)
      } catch (error) {
        return { ok: false, error: String((error && error.message) || error) }
      }
    },
  })

  const searchTool = defineTool({
    name: 'quantlab_search_stock',
    description:
      '在 QuantLab 里搜索股票（A 股 / 美股），返回匹配的 symbol、名称、市场。' +
      '拿到 symbol 后可用 quantlab_market_bars(exchange="stock") 查 K 线。',
    parameters: {
      q: { type: 'string', required: true, description: '关键词：股票名称/代码，如 茅台、600519、AAPL' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      const q = String((args && args.q) || '').trim()
      if (!q) return { ok: false, error: 'q 不能为空' }
      if (!baseUrl()) return notRunning()
      try {
        const payload = await call('GET', `/api/market/stocks/search?q=${encodeURIComponent(q)}`)
        return { ok: true, items: Array.isArray(payload && payload.items) ? payload.items : [], raw: payload }
      } catch (error) {
        return { ok: false, error: String((error && error.message) || error) }
      }
    },
  })

  const apiTool = defineTool({
    name: 'quantlab_api',
    description:
      '直接调 QuantLab 本机 API（白名单：GET/POST + 前缀 ' +
      API_ALLOWED_PREFIXES.join('、') +
      '）。用于查策略/回测/研究/因子/数据/模型等平台能力；' +
      `禁止 ${API_DENIED_PATHS.join('、')}。返回 JSON 截断到 8KB。`,
    parameters: {
      method: { type: 'string', required: true, description: 'GET 或 POST' },
      path: { type: 'string', required: true, description: '相对路径 + 查询串，如 /api/strategies/list 或 /api/backtests?limit=5' },
      body: { type: 'json', description: 'POST 时的 JSON body（GET 忽略）' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      const checked = checkApiRequest(args && args.method, args && args.path)
      if (!checked.ok) return { ok: false, error: checked.error }
      if (!baseUrl()) return notRunning()
      try {
        const payload = await call(checked.method, checked.path, checked.method === 'POST' ? (args && args.body) ?? {} : undefined)
        const text = JSON.stringify(payload, null, 2)
        const truncated = truncateText(text, API_RESPONSE_LIMIT)
        if (truncated !== text) {
          return {
            ok: true,
            truncated: true,
            note: `返回超过 ${Math.round(API_RESPONSE_LIMIT / 1024)}KB，已截断；可加 /limit 或分页参数再查`,
            json: truncated,
          }
        }
        return { ok: true, json: payload }
      } catch (error) {
        log(`quantlab_api 调用失败：${(error && error.message) || error}`)
        return { ok: false, error: String((error && error.message) || error) }
      }
    },
  })

  return [statusTool, barsTool, searchTool, apiTool]
}
