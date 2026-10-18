/**
 * 结构化内容协议（QuantLab Blocks v1）。
 *
 * Agent 在回答里用代码块输出有明确类型的数据：
 *
 *     ```ql:chart
 *     {"title": "净值", "kind": "line", "x": [...], "series": [{"name": "策略", "data": [...]}]}
 *     ```
 *
 * 语言标记写 `ql:<类型>`，内容是一个 JSON 对象；前端按类型渲染成可交互组件，解析失败就原样显示 JSON。
 * 所有块都可以带 `source`（数据从哪来）与 `evidence`（measured 实测 / inferred 推断 / external 外部来源），
 * 用来把 Agent 的推断与实际测量区分开。后端提示词（agents/blocks_protocol.py）与这里保持同一份类型表。
 */

export type Evidence = 'measured' | 'inferred' | 'external'

/** 数据来源：文字说明 + 可选跳转（回测、运行记录、网页、文件）。 */
export interface SourceRef {
  label?: string
  href?: string
  backtest_id?: string
  run_id?: string
  url?: string
  window?: string
}

export interface BlockBase {
  title?: string
  note?: string
  source?: string | SourceRef
  evidence?: Evidence
}

export type BlockType =
  | 'chart' | 'data_table' | 'metrics' | 'backtest_report' | 'kline_review' | 'factor_report' | 'parameter_heatmap' | 'stress_report' | 'compare'
  | 'citation' | 'claims' | 'file' | 'status' | 'steps' | 'callout' | 'choices' | 'form' | 'confirm' | 'run'

export const BLOCK_TYPES: BlockType[] = [
  'chart', 'data_table', 'metrics', 'backtest_report', 'kline_review', 'factor_report', 'parameter_heatmap', 'stress_report', 'compare',
  'citation', 'claims', 'file', 'status', 'steps', 'callout', 'choices', 'form', 'confirm', 'run',
]

export const BLOCK_LABEL: Record<BlockType, string> = {
  chart: '图表', data_table: '数据表', metrics: '指标', backtest_report: '回测报告', kline_review: 'K 线复盘',
  factor_report: '因子研究', parameter_heatmap: '参数热力图', stress_report: '压力测试', compare: '实验对比', citation: '来源', claims: '证据链', file: '文件', status: '状态',
  steps: '步骤', callout: '提示', choices: '选项', form: '表单', confirm: '确认', run: '运行结果',
}

export type ParsedBlock = { type: BlockType; data: Record<string, unknown> } | { type: BlockType; error: string; raw: string; partial?: boolean }

/** 宽松 JSON：允许尾逗号与 // 注释（模型偶尔会写），其余按严格 JSON。 */
export function looseJson(raw: string): unknown {
  const text = raw.trim()
  try {
    return JSON.parse(text)
  } catch {
    const cleaned = text
      .replace(/^\s*\/\/.*$/gm, '')
      .replace(/,\s*([}\]])/g, '$1')
    return JSON.parse(cleaned)
  }
}

export function parseBlock(lang: string, body: string, streaming = false): ParsedBlock | null {
  const m = lang.match(/^ql:([a-z_]+)$/i)
  if (!m) return null
  const type = m[1].toLowerCase() as BlockType
  if (!BLOCK_TYPES.includes(type)) return { type: 'callout', error: `未知的内容块类型 ql:${type}`, raw: body }
  try {
    const data = looseJson(body)
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      // 允许直接给数组：表格行 / 来源列表 / 步骤
      if (Array.isArray(data)) {
        const key = type === 'data_table' ? 'rows' : type === 'citation' ? 'sources' : type === 'steps' ? 'items' : type === 'metrics' ? 'items' : 'items'
        return { type, data: { [key]: data } }
      }
      return { type, error: '内容不是 JSON 对象', raw: body }
    }
    return { type, data: data as Record<string, unknown> }
  } catch (e) {
    return { type, error: (e as Error).message, raw: body, partial: streaming }
  }
}

// ---------------------------------------------------------------- 格式化

export type Fmt = 'num' | 'pct' | 'pct_signed' | 'money' | 'int' | 'bp' | 'ratio' | 'date' | 'datetime' | 'text'

export function fmtValue(v: unknown, fmt?: Fmt | string, digits?: number): string {
  if (v === null || v === undefined || v === '') return '—'
  if (typeof v === 'string' && fmt !== 'date' && fmt !== 'datetime' && Number.isNaN(Number(v))) return v
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return String(v)
  switch (fmt) {
    case 'pct': return `${(n * 100).toFixed(digits ?? 2)}%`
    case 'pct_signed': return `${n > 0 ? '+' : ''}${(n * 100).toFixed(digits ?? 2)}%`
    case 'bp': return `${(n * 10000).toFixed(digits ?? 1)} bp`
    case 'money': return n.toLocaleString(undefined, { maximumFractionDigits: digits ?? 2 })
    case 'int': return Math.round(n).toLocaleString()
    case 'ratio': return n.toFixed(digits ?? 2)
    case 'date': return fmtTs(n, false)
    case 'datetime': return fmtTs(n, true)
    case 'text': return String(v)
    default: {
      if (Number.isInteger(n) && Math.abs(n) >= 1000) return n.toLocaleString()
      const abs = Math.abs(n)
      return n.toFixed(digits ?? (abs >= 100 ? 1 : abs >= 1 ? 3 : 4)).replace(/\.?0+$/, (s) => (s.includes('.') ? '' : s))
    }
  }
}

/** 毫秒 / 秒时间戳 → 本地日期。 */
export function fmtTs(v: number, withTime = true): string {
  const ms = v < 1e11 ? v * 1000 : v
  const d = new Date(ms)
  if (Number.isNaN(d.getTime())) return String(v)
  const p = (x: number) => String(x).padStart(2, '0')
  const date = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
  return withTime ? `${date} ${p(d.getHours())}:${p(d.getMinutes())}` : date
}

/** 看起来像时间戳（2001 年之后的秒或毫秒）。 */
export function looksLikeTs(v: unknown): boolean {
  return typeof v === 'number' && ((v > 978_307_200 && v < 4_102_444_800) || (v > 978_307_200_000 && v < 4_102_444_800_000))
}

export function sourceText(src: BlockBase['source']): { label: string; href?: string } | null {
  if (!src) return null
  if (typeof src === 'string') {
    if (/^bt:/.test(src)) return { label: `回测 ${src.slice(3)}`, href: `#/backtest/${src.slice(3)}` }
    if (/^https?:\/\//.test(src)) return { label: hostOf(src), href: src }
    return { label: src }
  }
  const href = src.href || (src.backtest_id ? `#/backtest/${src.backtest_id}` : undefined) || src.url
  const label = src.label || (src.backtest_id ? `回测 ${src.backtest_id}` : src.run_id ? `运行 ${src.run_id}` : src.url ? hostOf(src.url) : '来源')
  return { label: src.window ? `${label} · ${src.window}` : label, href }
}

export function hostOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return url }
}

/** 只允许安全的链接：http(s)、站内 hash 路由、站内 /api 下载。 */
export function safeHref(href: unknown): string | undefined {
  if (typeof href !== 'string') return undefined
  const h = href.trim()
  if (/^https?:\/\//i.test(h) || h.startsWith('#/') || h.startsWith('/api/')) return h
  return undefined
}

export function safeImageSrc(src: unknown): string | undefined {
  if (typeof src !== 'string') return undefined
  const s = src.trim()
  if (/^https?:\/\//i.test(s) || s.startsWith('/api/') || /^data:image\/(png|jpe?g|gif|webp);base64,/i.test(s)) return s
  return undefined
}

// ---------------------------------------------------------------- 导出

export function toCsv(columns: string[], rows: unknown[][]): string {
  const cell = (v: unknown) => {
    const s = v === null || v === undefined ? '' : String(v)
    const safe = /^[=+\-@\t\r]/.test(s) ? `'${s}` : s // 防 CSV 公式注入
    return /[",\n]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe
  }
  return [columns.map(cell).join(','), ...rows.map((r) => r.map(cell).join(','))].join('\n')
}

export function downloadBlob(name: string, content: BlobPart, type = 'text/plain;charset=utf-8') {
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}

export function copyText(text: string): Promise<boolean> {
  if (!navigator.clipboard) return Promise.resolve(false)
  return navigator.clipboard.writeText(text).then(() => true, () => false)
}

/** 块里的按钮要把一句话发回对话：研究室监听这个事件（pages/lab/bus.ts）。 */
export const ASK_EVENT = 'qa:chat-ask'
export function askChat(text: string) {
  window.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: text }))
}

// ---------------------------------------------------------------- 消息流清理
//
// Agent 有时会先输出一个 ql:summary / ql:metrics，再输出 ql:backtest_report。
// 后者本身已经包含指标卡，前者只会把同一组数字重复渲染一遍；旧版 ql:summary 还会
// 因为类型不认识显示成「内容块解析失败」的残留提示。这里在 Markdown 解析前做一次
// 无损合并：能识别的 Summary 转成标准块，重复指标 / 重复报告直接去掉。

const BACKTEST_METRIC_KEYS = new Set([
  'total_return', 'annualized_return', 'excess_return', 'sharpe', 'sortino', 'calmar',
  'max_drawdown', 'win_rate', 'trades', 'total_fees', 'profit_factor', 'volatility',
  'benchmark_return', 'turnover', 'information_ratio', 'final_equity',
])

interface QlFence {
  start: number
  close: number
  marker: string
  indent: string
  info: string
  type: string
  body: string
  bodyStart: number
  closed: boolean
}

function scanQlFences(lines: string[]): QlFence[] {
  const out: QlFence[] = []
  const fence = /^ {0,3}(`{3,}|~{3,})\s*(.*)$/
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(fence)
    if (!m) continue
    const marker = m[1]
    const info = m[2].trim()
    const ql = info.match(/(?:^|\s)ql:([a-z_]+)\b/i)
    if (!ql) continue
    const body: string[] = []
    let j = i + 1
    let close = lines.length
    while (j < lines.length) {
      const t = lines[j].trim()
      if (t.startsWith(marker[0].repeat(marker.length)) && t.replace(/[`~]/g, '') === '') {
        close = j
        break
      }
      body.push(lines[j])
      j++
    }
    out.push({
      start: i, close, marker, indent: lines[i].match(/^ */)?.[0] || '', info,
      type: ql[1].toLowerCase(), body: body.join('\n'), bodyStart: i + 1,
      closed: close < lines.length,
    })
    i = close
  }
  return out
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function backtestIdOf(data: Record<string, unknown> | null): string {
  if (!data) return ''
  const direct = data.backtest_id || data.id
  if (direct) return String(direct)
  const items = Array.isArray(data.items) ? data.items : Array.isArray(data.sources) ? data.sources : []
  for (const raw of items) {
    const it = asRecord(raw)
    if (!it) continue
    if (it.backtest_id) return String(it.backtest_id)
    const src = it.source
    if (typeof src === 'string') {
      const m = src.match(/^bt:([^·\s]+)/)
      if (m) return m[1]
      const h = src.match(/#\/backtest\/([^?\s]+)/)
      if (h) return h[1]
    }
    const so = asRecord(src)
    if (so?.backtest_id) return String(so.backtest_id)
  }
  return ''
}

function metricItems(data: unknown): { key?: string; value?: unknown; source?: unknown }[] {
  if (Array.isArray(data)) return data.filter((x): x is Record<string, unknown> => !!asRecord(x))
  const d = asRecord(data)
  if (!d) return []
  if (Array.isArray(d.items)) return d.items.filter((x): x is Record<string, unknown> => !!asRecord(x))
  const metrics = asRecord(d.metrics)
  if (metrics) return Object.entries(metrics).map(([key, value]) => ({ key, value }))
  const direct = Object.entries(d).filter(([k, v]) => BACKTEST_METRIC_KEYS.has(k) && (typeof v === 'number' || typeof v === 'string'))
  return direct.map(([key, value]) => ({ key, value }))
}

/** 旧版 ql:summary / 指标对象 → 标准 ql:metrics；纯文字摘要 → ql:callout。 */
function normalizeOldBlock(type: string, body: string): { type: string; body: string } | null {
  if (type !== 'summary') return null
  let data: unknown
  try { data = looseJson(body) } catch { return null }
  const items = metricItems(data)
  if (items.length) {
    const source = asRecord(data)?.source
    const title = asRecord(data)?.title
    return {
      type: 'metrics',
      body: JSON.stringify({ ...(title ? { title } : {}), ...(source ? { source } : {}), items }),
    }
  }
  const d = asRecord(data)
  const text = d?.text ?? d?.content ?? d?.summary
  if (typeof text === 'string' && text.trim()) {
    return { type: 'callout', body: JSON.stringify({ tone: d?.tone || 'info', title: d?.title, text: text.trim() }) }
  }
  return null
}

/**
 * 合并重复内容块：
 * - ql:summary → ql:metrics / ql:callout，避免旧类型触发渲染失败；
 * - 同一消息里已经有 ql:backtest_report 时，丢掉重复的 ql:metrics / ql:summary；
 * - ql:backtest_report 已内含 K 线复盘，丢掉同 backtest_id 的独立 ql:kline_review；
 * - 完全相同的 ql 块只保留第一份。
 */
export function mergeRedundantBlocks(src: string): string {
  if (!src.includes('```') && !src.includes('~~~')) return src
  const lines = src.split('\n')
  const fences = scanQlFences(lines)
  if (!fences.length) return src
  const drops = new Set<number>()
  const aliases = new Map<number, { type: string; body: string }>()
  const parsed = new Map<number, Record<string, unknown> | null>()
  const reports = new Set<string>()
  let hasReport = false

  for (const f of fences) {
    let data: Record<string, unknown> | null = null
    try { data = asRecord(looseJson(f.body)) } catch { data = null }
    parsed.set(f.start, data)
    if (f.type === 'backtest_report') {
      hasReport = true
      const id = backtestIdOf(data)
      if (id) reports.add(id)
    }
  }

  const seen = new Set<string>()
  const seenReport = new Set<string>()
  for (const f of fences) {
    if (!f.closed) continue // 流式输出中的半个块：先别动，等闭合后再合并
    let type = f.type
    let body = f.body
    const old = normalizeOldBlock(type, body)
    if (old) {
      type = old.type
      body = old.body
      aliases.set(f.start, old)
    }
    const data = type === f.type ? parsed.get(f.start) ?? null : asRecord(looseJson(body))
    if (type === 'summary') { drops.add(f.start); continue } // 无法识别的旧 Summary：不留下报错占位
    const key = `${type}|${body.trim()}`
    if (seen.has(key)) { drops.add(f.start); continue }
    seen.add(key)
    const id = backtestIdOf(data)
    if (type === 'backtest_report') {
      if (id && seenReport.has(id)) { drops.add(f.start); continue }
      if (id) seenReport.add(id)
    }
    if (type === 'kline_review' && id && reports.has(id)) { drops.add(f.start); continue }
    if (type === 'metrics' && hasReport) {
      const items = metricItems(data)
      const standard = items.length > 0 && items.every((it) => !it.key || BACKTEST_METRIC_KEYS.has(String(it.key)))
      if ((id && reports.has(id)) || (!id && standard)) { drops.add(f.start); continue }
    }
  }

  const out = [...lines]
  for (const f of fences) {
    if (!f.closed) continue
    if (drops.has(f.start)) {
      for (let i = f.start; i <= f.close; i++) out[i] = ''
      continue
    }
    const alias = aliases.get(f.start)
    if (!alias) continue
    out[f.start] = `${f.indent}${f.marker}ql:${alias.type}`
    for (let i = f.bodyStart; i < f.close; i++) out[i] = ''
    out[f.bodyStart] = alias.body
  }
  let text = out.join('\n').replace(/\n{3,}/g, '\n\n')
  // 旧截图里出现过一行「[交互组件有问题]」的残留占位。它只可能是渲染失败后的
  // 兜底提示；同一条消息已经有正式报告时直接清掉，避免它卡在报告上方。
  if (hasReport) {
    text = text
      .split('\n')
      .filter((line) => !/^\s*[\[【(（]?\s*交互组件(?:渲染)?有问题\s*[\]】)）]?\s*$/.test(line))
      .join('\n')
  }
  return text
}
