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
  | 'chart' | 'data_table' | 'metrics' | 'backtest_report' | 'kline_review' | 'factor_report' | 'compare'
  | 'citation' | 'claims' | 'file' | 'status' | 'steps' | 'callout' | 'choices' | 'form' | 'confirm' | 'run'

export const BLOCK_TYPES: BlockType[] = [
  'chart', 'data_table', 'metrics', 'backtest_report', 'kline_review', 'factor_report', 'compare',
  'citation', 'claims', 'file', 'status', 'steps', 'callout', 'choices', 'form', 'confirm', 'run',
]

export const BLOCK_LABEL: Record<BlockType, string> = {
  chart: '图表', data_table: '数据表', metrics: '指标', backtest_report: '回测报告', kline_review: 'K 线复盘',
  factor_report: '因子研究', compare: '实验对比', citation: '来源', claims: '证据链', file: '文件', status: '状态',
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
