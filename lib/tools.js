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

// ─── 回测报告 / GitHub 导入（原生对话里展示） ──────────────────────────────

/** 回测 id 只接受安全字符（会拼进 URL/卡片属性，不能带引号或斜杠）。 */
export function safeId(value) {
  const text = String(value ?? '').trim()
  if (!text) return ''
  return /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(text) ? text : ''
}

/** 报告格式白名单（与 report-export 的 GET /api/backtests/{id}/report.{fmt} 对齐）。 */
export const REPORT_FORMATS = Object.freeze(['png', 'pdf', 'html'])

/** 结果里留给 client.js 工具卡片解析的一行（markdown 渲染时看不见）。 */
function cardMarker(card) {
  return `<!-- quantlab-card ${JSON.stringify(card)} -->`
}

/** 把宽松的回测结果收成卡片要的 4 个指标（拿不到就不显示）。 */
export function pickMetrics(payload) {
  const candidates = [
    payload && payload.metrics,
    payload && payload.summary,
    payload && payload.stats,
    payload && payload.result && payload.result.metrics,
    payload && payload.result && payload.result.summary,
    payload,
  ].filter((item) => item && typeof item === 'object')
  const pick = (...keys) => {
    for (const source of candidates) {
      for (const key of keys) {
        const value = source[key]
        if (value !== undefined && value !== null && value !== '') return value
      }
    }
    return undefined
  }
  const out = {}
  const totalReturnPct = pick('total_return_pct', 'totalReturnPct', 'return_pct', 'total_return')
  const sharpe = pick('sharpe', 'sharpe_ratio')
  const maxDrawdownPct = pick('max_drawdown_pct', 'maxDrawdownPct', 'max_drawdown', 'max_dd_pct')
  const trades = pick('trades', 'trade_count', 'num_trades', 'n_trades')
  if (totalReturnPct !== undefined) out.totalReturnPct = totalReturnPct
  if (sharpe !== undefined) out.sharpe = sharpe
  if (maxDrawdownPct !== undefined) out.maxDrawdownPct = maxDrawdownPct
  if (trades !== undefined) out.trades = trades
  return out
}

/**
 * 构建 quantlab_* 工具定义。
 *
 * @param {object} options
 * @param {Function} options.defineTool `@deepseek-ai/dsh-tools` 的 defineTool（单测可传 shim）
 * @param {Function} options.getBaseUrl 返回当前后端根地址（未运行返回 ''）
 * @param {Function} options.getStatus 返回宿主状态快照（quantlab_status 用）
 * @param {Function} [options.getDshOrigin] 返回 DSH web 安全根（http://127.0.0.1:<dshPort>），
 *        用来拼 /dsh-quantlab/chart/{id}.png 这类同源代理链接
 * @param {Function} [options.fetch] fetch 实现（默认全局 fetch）
 * @param {Function} [options.log] 诊断日志
 * @returns {Array<object>} 已 defineTool 包装好的工具定义
 */
export function buildToolDefinitions({
  defineTool,
  getBaseUrl,
  getStatus,
  getDshOrigin = () => '',
  fetch = globalThis.fetch,
  log = () => {},
}) {
  if (typeof defineTool !== 'function') throw new Error('buildToolDefinitions 需要 defineTool')
  const baseUrl = () => {
    try {
      return String(getBaseUrl() || '')
    } catch {
      return ''
    }
  }
  const call = (method, path, body) => requestPlatform({ fetch, baseUrl: baseUrl(), method, path, body })
  const dshOrigin = () => {
    try {
      return String(getDshOrigin() || '').replace(/\/+$/, '')
    } catch {
      return ''
    }
  }
  const platformUrl = (path) => {
    const base = baseUrl()
    if (!base) return ''
    return `${base.replace(/\/+$/, '')}${path.startsWith('/') ? '' : '/'}${path}`
  }

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

  // ── quantlab_export_report：导出回测报告，结果带 markdown 图片 + 卡片 ──────
  const exportTool = defineTool({
    name: 'quantlab_export_report',
    description:
      '导出一次回测的报告（PNG 图片 / PDF / HTML），返回可直接展示的 markdown：'
      + '摘要 + 净值曲线图片 + 「在 QuantLab 打开」链接 + 本地文件位置。'
      + '结果里带一行 <!-- quantlab-card ... --> 给 DSH 对话里的卡片渲染器用（人会看到图片和按钮）。'
      + '报告接口没就绪/回测不存在时返回 {ok:false,error}，error 里有原因和下一步。',
    parameters: {
      backtest_id: { type: 'string', required: true, description: '回测记录 id（/api/backtests 列表里的 id）' },
      format: { type: 'string', description: `报告格式：${REPORT_FORMATS.join(' / ')}（默认 png）` },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: (value && value.markdown) || JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      const id = safeId(args && (args.backtest_id ?? args.backtestId ?? args.id))
      if (!id) return { ok: false, error: 'backtest_id 不合法（只接受字母/数字/下划线/短横线）' }
      const format = String((args && args.format) || 'png').toLowerCase().trim() || 'png'
      if (!REPORT_FORMATS.includes(format)) {
        return { ok: false, error: `format 只能是 ${REPORT_FORMATS.join(' / ')}，收到「${format}」` }
      }
      if (!baseUrl()) return notRunning()

      // ① 如果 report-export 提供了 JSON 导出接口（POST /api/backtests/{id}/report {format}），
      //    拿它的本地路径；没有就退回契约里的下载接口。
      let localPath = ''
      try {
        const exported = await call('POST', `/api/backtests/${id}/report`, { format })
        const path = exported && (exported.path || exported.file || exported.local_path)
        if (typeof path === 'string' && path) localPath = path
      } catch (error) {
        log(`quantlab_export_report：JSON 导出接口不可用，退回下载接口（${(error && error.message) || error}）`)
      }

      // ② 契约接口：GET /api/backtests/{id}/report.<format>。只确认能拿到，不把整份 PDF 塞进对话。
      let size = 0
      try {
        const response = await fetch(platformUrl(`/api/backtests/${id}/report.${format}`), {
          headers: { accept: '*/*' },
        })
        if (response.ok !== true) {
          let detail = ''
          try {
            detail = String(await response.text()).slice(0, 300)
          } catch {
            detail = ''
          }
          return {
            ok: false,
            error: `导出失败：平台没有给出这份报告（HTTP ${response.status}${detail ? '：' + detail : ''}）。`
              + `请确认回测 #${id} 存在，且 report-export 的 GET /api/backtests/{id}/report.${format} 已就绪。`,
          }
        }
        size = Number((response.headers && typeof response.headers.get === 'function' ? response.headers.get('content-length') : 0) || 0)
        try {
          await response.body?.cancel?.()
        } catch {
          /* ignore */
        }
      } catch (error) {
        return { ok: false, error: `连不上报告接口：${(error && error.message) || error}` }
      }

      // ③ 指标（可选）：回测详情拿不到就不显示
      let metrics = {}
      try {
        metrics = pickMetrics(await call('GET', `/api/backtests/${id}`))
      } catch (error) {
        log(`quantlab_export_report：读回测详情失败（${(error && error.message) || error}）`)
      }

      const origin = dshOrigin()
      const chartUrl = origin
        ? `${origin}/dsh-quantlab/chart/${id}.png`
        : platformUrl(`/api/backtests/${id}/report.png`)
      const downloadUrl = origin
        ? `${origin}/dsh-quantlab/report/${id}.${format}`
        : platformUrl(`/api/backtests/${id}/report.${format}`)
      const panelRoute = `#/backtests/${id}`
      const sizeText = size ? `，约 ${Math.max(1, Math.round(size / 1024))} KB` : ''
      const summary = `回测 #${id} 的报告已就绪（${format.toUpperCase()}${sizeText}）`
      const card = {
        ok: true,
        backtestId: id,
        format,
        chartUrl,
        downloadUrl,
        panelRoute,
        summary,
        metrics,
        ...(localPath ? { localPath } : {}),
      }
      const markdown = [
        summary,
        '',
        `![回测 #${id} 净值曲线](${chartUrl})`,
        '',
        `- 本地文件：${localPath || `平台导出目录（<项目>/.quantlab/exports/）里的 backtest-${id}-*.${format}`}`,
        `- 打开回测：${platformUrl(`/#/backtests/${id}`)}`,
        `- 分享：下载图片/PDF 后直接发出去；127.0.0.1 链接只在这台机器上能打开。`,
        '',
        cardMarker(card),
      ].join('\n')
      return { ...card, markdown }
    },
  })

  // ── quantlab_backtest：用已保存策略或源码同步跑一次回测 ──────────────────
  const backtestTool = defineTool({
    name: 'quantlab_backtest',
    description:
      '在 QuantLab 里跑一次回测（同步等待结果），返回指标摘要 + 「在 QuantLab 打开」链接；'
      + '有回测记录 id 时结果里也带净值图 markdown 和 <!-- quantlab-card -->（对话里显示卡片）。'
      + '股票用 exchange="stock"（如 600519.SH / AAPL.US），加密用 binance/okx/bybit。'
      + '后台 worker 没在当前进程内执行时可能返回 {ok:true,pending:true,run_id}，此时用 quantlab_api 轮询 /api/runs/{run_id}。',
    parameters: {
      slug: { type: 'string', description: '已保存策略的 slug（与 code 二选一）' },
      code: { type: 'string', description: '策略源码（与 slug 二选一；Python 或 DSL JSON）' },
      name: { type: 'string', description: '源码策略的名字（可选）' },
      symbol: { type: 'string', description: '标的：BTC/USDT、600519.SH 等（默认 BTC/USDT）' },
      timeframe: { type: 'string', description: '周期（默认 1h）' },
      bars_limit: { type: 'number', description: 'K 线根数 102–5000（默认 1500）' },
      exchange: { type: 'string', description: '场所：stock / binance / okx / bybit（默认按标的推断）' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: (value && value.markdown) || JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      const slug = String((args && args.slug) || '').trim()
      const code = typeof (args && args.code) === 'string' ? args.code : ''
      if (!slug && !code) return { ok: false, error: '请给 slug（已保存策略）或 code（策略源码）' }
      if (!baseUrl()) return notRunning()
      const symbol = String((args && args.symbol) || 'BTC/USDT').trim() || 'BTC/USDT'
      const timeframe = String((args && args.timeframe) || '1h').trim() || '1h'
      const barsLimit = Math.max(102, Math.min(5000, Math.floor(Number(args && args.bars_limit) || 1500)))
      const body = {
        ...(slug ? { slug } : { code }),
        ...(args && args.name ? { name: String(args.name).slice(0, 80) } : {}),
        symbol,
        timeframe,
        bars_limit: barsLimit,
      }
      let result
      try {
        result = await call('POST', '/api/studio/backtest', body)
      } catch (error) {
        return { ok: false, error: `回测失败：${(error && error.message) || error}` }
      }
      if (result && result.pending === true) {
        return {
          ok: true,
          pending: true,
          run_id: result.run_id || (result.run && result.run.id) || '',
          summary: `回测已提交（run ${result.run_id || '?'}），后台 worker 完成后可用 quantlab_api 查 /api/runs/${result.run_id || '{id}'}`,
          markdown: `回测已提交（状态 ${result.state || 'QUEUED'}，run_id=${result.run_id || '?'}）。\n\n`
            + `等 worker 完成后，用 quantlab_api 查 \`GET /api/runs/${result.run_id || '{run_id}'}\` 或 \`GET /api/backtests\` 找结果。`,
        }
      }
      const backtestId = safeId(result && (result.backtest_id || result.backtestId || result.id))
      const metrics = pickMetrics(result)
      const origin = dshOrigin()
      const panelRoute = backtestId ? `#/backtests/${backtestId}` : '#/backtests'
      const fmt = (value, digits = 2) => {
        const num = Number(value)
        return Number.isFinite(num) ? num.toFixed(digits) : String(value)
      }
      const headline = `回测完成：${symbol} · ${timeframe}${slug ? ` · ${slug}` : ''}`
      const bits = []
      if (metrics.totalReturnPct !== undefined) bits.push(`总收益 ${fmt(metrics.totalReturnPct)}%`)
      if (metrics.sharpe !== undefined) bits.push(`夏普 ${fmt(metrics.sharpe)}`)
      if (metrics.maxDrawdownPct !== undefined) bits.push(`最大回撤 ${fmt(metrics.maxDrawdownPct)}%`)
      if (metrics.trades !== undefined) bits.push(`交易 ${metrics.trades} 笔`)
      const parts = [headline]
      if (bits.length) parts.push('', bits.join(' · '))
      const card = {
        ok: true,
        backtestId: backtestId || '',
        symbol,
        timeframe,
        panelRoute,
        // 卡片标题下面只放一句摘要；指标由卡片自己的 metrics 行格式化显示，避免重复+长小数
        summary: headline,
        metrics,
        ...(backtestId && origin ? { chartUrl: `${origin}/dsh-quantlab/chart/${backtestId}.png` } : {}),
        ...(backtestId ? { downloadUrl: platformUrl(`/api/backtests/${backtestId}/report.png`) } : {}),
      }
      const markdown = [
        ...parts,
        '',
        ...(card.chartUrl ? [`![回测净值曲线](${card.chartUrl})`, ''] : []),
        `- 打开回测：${platformUrl(`/${backtestId ? `#/backtests/${backtestId}` : '#/backtests'}`)}`,
        cardMarker(card),
      ].join('\n')
      return { ...card, markdown }
    },
  })

  // ── quantlab_import_github：把公开仓库导入项目 ───────────────────────────
  const importTool = defineTool({
    name: 'quantlab_import_github',
    description:
      '把公开 GitHub 仓库导入当前 QuantLab 项目（浅克隆 --depth 1，上限 200MB，超时 120s）。'
      + '仓库里带 .qstrat 时会自动走策略包导入（导入后仍未批准）。'
      + '返回仓库名、文件数、大小、本地目录、导入到的策略；失败时 error 是中文原因（地址不合法/仓库不存在/网络不通/太大）。',
    parameters: {
      url: { type: 'string', required: true, description: 'https://github.com/<owner>/<repo>（公开仓库，可带 .git）' },
      note: { type: 'string', description: '备注（可选）' },
    },
    output: {
      schema: { type: 'json' },
      render: (_args, value) => [{ type: 'text', text: (value && value.markdown) || JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      const url = String((args && args.url) || '').trim()
      if (!url) return { ok: false, error: 'url 不能为空（https://github.com/<owner>/<repo>）' }
      if (!baseUrl()) return notRunning()
      let payload
      try {
        payload = await call('POST', '/api/imports/github', { url, note: String((args && args.note) || '').slice(0, 200) })
      } catch (error) {
        return { ok: false, error: `导入失败：${(error && error.message) || error}` }
      }
      const rec = payload && payload.import
      if (!rec) return { ok: false, error: '平台没有返回导入记录（接口可能还没接线）' }
      const strategies = Array.isArray(rec.strategies_imported) ? rec.strategies_imported : []
      const warnings = Array.isArray(rec.warnings) ? rec.warnings : []
      const sizeKb = Number(rec.bytes || 0) / 1024
      const dirPath = rec.dir ? `${(getStatus() || {}).projectDir || '<项目>'}/.quantlab/imports/github/${rec.dir}` : ''
      const summary = `已导入 ${rec.name}：${rec.files || 0} 个文件，${sizeKb.toFixed(1)} KB`
        + (strategies.length ? `，导入策略 ${strategies.map((item) => item.slug || item.name).join('、')}` : '')
      const markdown = [
        summary,
        '',
        `- 仓库：${rec.url || url}`,
        ...(dirPath ? [`- 本地目录：${dirPath}`] : []),
        ...(warnings.length ? [`- 提示：${warnings.slice(0, 3).join('；')}`] : []),
        '- 在面板「导入 GitHub」里可以打开文件夹或删除这次导入。',
        '',
        cardMarker({ ok: true, kind: 'github-import', name: rec.name, id: rec.id, files: rec.files, bytes: rec.bytes, strategies }),
      ].join('\n')
      return { ok: true, id: rec.id, name: rec.name, files: rec.files, bytes: rec.bytes, dir: rec.dir, strategies, warnings, markdown }
    },
  })

  return [statusTool, barsTool, searchTool, apiTool, backtestTool, exportTool, importTool]
}
