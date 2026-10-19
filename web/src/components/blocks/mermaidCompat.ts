/**
 * Mermaid 中文兼容层。
 *
 * Mermaid 11 的**部分**图类型词法器只接受 ASCII，中文会被直接拒绝，表现为：
 *
 * - `quadrantChart`：`Lexical error on line N. Unrecognized text.`（轴标签、象限名、数据点名字）
 * - `xychart-beta`：同样的词法错误（分类轴标签）
 * - `sankey-beta`：`Parse error: Expecting 'NEWLINE', 'EOF', 'COMMA', got 'NON_ESCAPED_TEXT'`
 *   —— 它的 `NON_ESCAPED_TEXT` / `ESCAPED_TEXT` 规则就是 `[\u0020-\u0021\u0023-\u002B\u002D-\u007E]`，
 *   纯 ASCII 字符集，连加双引号也救不了。
 *
 * 其它图类型（flowchart / sequenceDiagram / pie / gantt / mindmap / timeline / classDiagram /
 * stateDiagram / gitGraph / block-beta …）本身支持中文，这里原样透传。
 *
 * 处理办法（对作者完全透明，源码不用改）：
 *
 * 1. `quadrantChart` / `xychart-beta`：给含中文的片段补上双引号——Mermaid 语法本来就支持引号，
 *    渲染出的标签不带引号，属于无损改写。
 * 2. `sankey-beta`：把节点名换成 ASCII 占位符交给 Mermaid，渲染完成后再在 SVG 文本里把占位符
 *    还原成中文。这样既绕开词法器限制，又保留 Mermaid 自己的桑基布局与配色。
 *
 * 该模块不依赖 React / DOM，可以在 Node 里直接单测（见 `scripts/qa-mermaid.cjs`）。
 */

/** 需要人工引号保护的表意文字范围：CJK、假名、全角标点、CJK 符号等。 */
const CJK = /[\u2e80-\u2eff\u3000-\u303f\u3040-\u30ff\u3100-\u312f\u3200-\u32ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]/

/** 非 ASCII 任意字符：`sankey-beta` 的字段判定用它。 */
const NON_ASCII = /[^\x20-\x7E]/

/** 片段分隔符：空白、CSV/语法标点、括号、各种箭头。 */
const BREAK = /[\s,;:\[\](){}<>|"'`\u2192\u2013\u2014]/

export interface PreparedMermaid {
  /** 改写后交给 Mermaid 的源码。 */
  code: string
  /** 渲染成功后的 SVG：把占位符还原回中文（没有占位符时原样返回）。 */
  restore: (svg: string) => string
}

/**
 * 给一行里含中文、且没被引号包围的片段加双引号。
 * 已加引号的部分、纯 ASCII 片段、数字与 `-->` 之类的语法符号都不动。
 */
export function quoteCjkText(line: string): string {
  let out = ''
  let quoted = false
  let i = 0
  while (i < line.length) {
    const ch = line[i]
    if (ch === '"') { quoted = !quoted; out += ch; i++; continue }
    if (quoted) { out += ch; i++; continue }
    if (BREAK.test(ch)) { out += ch; i++; continue }
    let run = ''
    let j = i
    while (j < line.length && !BREAK.test(line[j])) { run += line[j]; j++ }
    out += CJK.test(run) ? `"${run}"` : run
    i = j
  }
  return out
}

/** 图类型：只看第一条有效行。 */
export function mermaidKind(code: string): 'quadrant' | 'xychart' | 'sankey' | 'other' {
  const head = code
    .split('\n')
    .map((l) => l.trim())
    .find((l) => l && !l.startsWith('%%'))
  if (!head) return 'other'
  if (/^quadrantChart\b/i.test(head)) return 'quadrant'
  if (/^xychart-beta\b/i.test(head)) return 'xychart'
  if (/^sankey-beta\b/i.test(head)) return 'sankey'
  return 'other'
}

/**
 * 逐行改写：注释行保持原样；`title` 行按图类型决定要不要加引号
 * —— `xychart-beta` 的 title 必须加引号（加了不会显示出来），
 *    `quadrantChart` 的 title 加了引号反而会把引号画在图上，所以保持原样。
 */
function quoteLines(code: string, quoteTitle: boolean): string {
  return code
    .split('\n')
    .map((line) => {
      const trimmed = line.trim()
      if (!trimmed || trimmed.startsWith('%%')) return line
      if (!quoteTitle && /^title\b/i.test(trimmed)) return line
      return quoteCjkText(line)
    })
    .join('\n')
}

/** CSV 单行切分：支持双引号包裹与 `""` 转义（sankey 的官方写法）。 */
export function splitSankeyLine(line: string): string[] {
  const out: string[] = []
  let cur = ''
  let quoted = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (quoted) {
      if (ch === '"') {
        if (line[i + 1] === '"') { cur += '"'; i++ } else quoted = false
      } else cur += ch
    } else if (ch === '"') quoted = true
    else if (ch === ',') { out.push(cur); cur = '' }
    else cur += ch
  }
  out.push(cur)
  return out
}

export function escapeXmlText(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

/** sankey：节点名 → ASCII 占位符，渲染后还原。 */
function prepareSankey(code: string): PreparedMermaid {
  const restoreMap = new Map<string, string>()
  const byName = new Map<string, string>()
  let seq = 0
  const lines = code.split('\n')
  const out = lines.map((line, idx) => {
    const trimmed = line.trim()
    if (idx === 0 || !trimmed || trimmed.startsWith('%%') || /^sankey-beta\b/i.test(trimmed)) return line
    const fields = splitSankeyLine(line)
    if (fields.length !== 3) return line
    const mapField = (raw: string) => {
      const name = raw.trim()
      if (!name || !NON_ASCII.test(name)) return raw
      let token = byName.get(name)
      if (!token) {
        do { token = `QLSANK${seq++}X` } while (code.includes(token))
        byName.set(name, token)
        restoreMap.set(token, name)
      }
      return raw.replace(name, token)
    }
    const csv = (v: string) => /[,"\r\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v
    return [mapField(fields[0]), mapField(fields[1]), fields[2]].map(csv).join(',')
  })
  return {
    code: out.join('\n'),
    restore: restoreMap.size
      ? (svg) => {
        let text = svg
        for (const [token, name] of restoreMap) text = text.split(token).join(escapeXmlText(name))
        return text
      }
      : (svg) => svg,
  }
}

/** 交给 Mermaid 之前调用：把中文源码改写成 Mermaid 词法器能接受的等价源码。 */
export function prepareMermaid(code: string): PreparedMermaid {
  switch (mermaidKind(code)) {
    case 'quadrant':
      return { code: quoteLines(code, false), restore: (svg) => svg }
    case 'xychart':
      return { code: quoteLines(code, true), restore: (svg) => svg }
    case 'sankey':
      return prepareSankey(code)
    default:
      return { code, restore: (svg) => svg }
  }
}

/* ------------------------------------------------------------------ */
/* SVG → PNG                                                          */
/* ------------------------------------------------------------------ */

export interface SvgSize { width: number; height: number }

/** 从 SVG 的 viewBox / width / height 里取尺寸（Mermaid 的 width 常是 100%）。 */
export function svgSize(svg: string, fallback: SvgSize = { width: 900, height: 600 }): SvgSize {
  const vb = /viewBox="([-\d.eE]+)[\s,]+([-\d.eE]+)[\s,]+([\d.eE]+)[\s,]+([\d.eE]+)"/.exec(svg)
  if (vb) {
    const w = Number(vb[3])
    const h = Number(vb[4])
    if (Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0) return { width: w, height: h }
  }
  const px = (v: string | undefined) => {
    const n = Number((v || '').replace(/px$/, ''))
    return Number.isFinite(n) && n > 0 ? n : 0
  }
  const w = px(/\swidth="([^"]+)"/.exec(svg)?.[1])
  const h = px(/\sheight="([^"]+)"/.exec(svg)?.[1])
  if (w && h) return { width: w, height: h }
  return fallback
}

/** 保证 SVG 自带命名空间，否则塞进 <img> / canvas 会加载失败。 */
export function standaloneSvg(svg: string): string {
  return /<svg[^>]*\sxmlns=/.test(svg) ? svg : svg.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"')
}

/**
 * SVG → PNG。scale 为像素密度倍数（默认 2 倍，导出图片在高分屏上也清晰）。
 * background 为空表示透明底。
 */
export async function svgToPngBlob(svg: string, opts: { scale?: number; background?: string } = {}): Promise<Blob> {
  const scale = opts.scale ?? 2
  const { width, height } = svgSize(svg)
  const source = standaloneSvg(svg)
  const url = URL.createObjectURL(new Blob([source], { type: 'image/svg+xml;charset=utf-8' }))
  try {
    const img = new Image()
    img.decoding = 'sync'
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve()
      img.onerror = () => reject(new Error('SVG 转图片失败'))
      img.src = url
    })
    const canvas = document.createElement('canvas')
    canvas.width = Math.max(1, Math.round(width * scale))
    canvas.height = Math.max(1, Math.round(height * scale))
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('浏览器不支持 canvas')
    if (opts.background) { ctx.fillStyle = opts.background; ctx.fillRect(0, 0, canvas.width, canvas.height) }
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('PNG 生成失败'))), 'image/png')
    })
  } finally {
    URL.revokeObjectURL(url)
  }
}
