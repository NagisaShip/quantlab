import { Fragment, createContext, useContext, useId, useMemo, useRef, type ReactNode } from 'react'
import { CodeBlock, parseFence } from './blocks/CodeBlock'
import { DataTable } from './blocks/DataTable'
import { CopyTableButton, HtmlBlock, InlineHtml, SvgBlock } from './blocks/Html'
import { GalleryProvider, ImageView, MathTex, Mermaid } from './blocks/Media'
import { CitationCtx, CiteMark, RichBlock, collectCitations } from './blocks/RichBlock'
import { mergeRedundantBlocks, parseBlock, safeHref } from './blocks/protocol'
import { Qmoji, qmojiById } from './Qmoji'
import '../stylesBlocks.css'

/**
 * Markdown 渲染（GFM 为主）：
 * - 块：ATX / Setext 标题（六级）、段落、嵌套列表（有序起始号、任务列表、列表里的代码块）、引用与
 *   GitHub 提示框（> [!NOTE]）、表格（左 / 中 / 右对齐，可复制）、分隔线、脚注、代码块（高亮 / 行号 / Diff）、
 *   数学公式（$…$、$$…$$、\(…\)、\[…\]）、Mermaid、```svg、Agent 的结构化内容块（```ql:<类型>```）；
 * - 原生 HTML：<details>（里面照样写 Markdown）、<table>、<div> 等块级标签，以及 <kbd> <sub> <sup> <mark> 等行内标签，
 *   全部经 DOMPurify 净化（blocks/Html.tsx）；
 * - 行内：代码、图片、链接 / 自动链接、粗体、斜体、粗斜体、删除线、==高亮==、转义字符、引用标记 [1]、脚注 [^x]。
 * 其余部分生成 React 元素，不拼接不可信的 HTML。
 */

interface Ctx { streaming: boolean; notes: Map<string, number> }
const NoteCtx = createContext<{ anchor: string; notes: Map<string, number> } | null>(null)

export function Markdown({ text, compact, streaming }: { text: string; compact?: boolean; streaming?: boolean }) {
  const anchor = 'md' + useId().replace(/[^a-zA-Z0-9]/g, '')
  const src = useMemo(() => mergeRedundantBlocks(text || ''), [text])
  const sources = useMemo(() => collectCitations(src), [src])
  const { body, footnotes, notes } = useMemo(() => {
    const { rest, defs } = extractFootnotes(src)
    const notes = new Map<string, number>([...defs.keys()].map((k, i) => [k, i + 1]))
    return { body: renderBlocks(rest, { streaming: !!streaming, notes }), footnotes: defs, notes }
  }, [src, streaming])
  return (
    <CitationCtx.Provider value={{ sources, anchor }}>
      <NoteCtx.Provider value={{ anchor, notes }}>
        <GalleryProvider>
          <div className={`md${compact ? ' compact' : ''}`}>
            {body}
            {footnotes.size > 0 && (
              <section className="md-footnotes" aria-label="脚注">
                <ol>
                  {[...footnotes.entries()].map(([k, v]) => (
                    <li key={k} id={`${anchor}-fn-${k}`}>{inline(v)}</li>
                  ))}
                </ol>
              </section>
            )}
          </div>
        </GalleryProvider>
      </NoteCtx.Provider>
    </CitationCtx.Provider>
  )
}

/* ------------------------------------------------------------------ 块级 */

const FENCE = /^ {0,3}(`{3,}|~{3,})\s*(.*)$/
const ATX = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/
const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/
const QUOTE = /^ {0,3}>/
const BULLET = /^( *)([-*+])([ \t]+|$)(.*)$/
const ORDERED = /^( *)(\d{1,9})([.)])([ \t]+|$)(.*)$/
const TABLE_DELIM = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/
const MATH_OPEN = /^\s*(\$\$|\\\[)(.*)$/
const CUSTOM_CALLOUT = /^ {0,3}<callout(?:\s+([^>]*))?>(.*)$/i
const CUSTOM_DIFFICULTY = /^ {0,3}<difficulty>(.*)$/i
/** 块级 HTML 起始标签：这些标签开头的一段按 HTML 处理（行内标签如 <b> 留给段落） */
const HTML_BLOCK = /^ {0,3}<(\/?)(details|div|table|thead|tbody|tr|svg|figure|figcaption|section|article|aside|center|dl|dt|dd|pre|p|blockquote|ul|ol|li|h[1-6]|hr|img|picture|video|audio|nav|header|footer|main|address|fieldset|caption|colgroup|col)(\s|>|\/>|$)|^ {0,3}<!--/i
const VOID = new Set(['hr', 'img', 'col', 'br'])

function isBlockStart(line: string, next?: string): boolean {
  return FENCE.test(line) || ATX.test(line) || HR.test(line) || QUOTE.test(line) || BULLET.test(line)
    || ORDERED.test(line) || MATH_OPEN.test(line) || HTML_BLOCK.test(line)
    || (line.includes('|') && next !== undefined && TABLE_DELIM.test(next))
}

function renderBlocks(src: string, ctx: Ctx): ReactNode[] {
  const lines = src.replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n')
  const out: ReactNode[] = []
  let i = 0
  let key = 0
  while (i < lines.length) {
    const line = lines[i]
    // 代码块 / 结构化块 / Mermaid / SVG / 公式
    const fence = line.match(FENCE)
    if (fence) {
      const marker = fence[1]
      const indent = line.length - line.trimStart().length
      const info = fence[2].trim()
      const buf: string[] = []
      i++
      let closed = false
      while (i < lines.length) {
        const t = lines[i].trim()
        if (t.startsWith(marker[0].repeat(marker.length)) && t.replace(/[`~]/g, '') === '') { closed = true; i++; break }
        buf.push(indent ? lines[i].replace(new RegExp(`^ {0,${indent}}`), '') : lines[i])
        i++
      }
      const body = buf.join('\n')
      const lang = parseFence(info).lang.toLowerCase()
      const partial = ctx.streaming && !closed
      const block = parseBlock(lang, body, partial)
      if (block) out.push(<RichBlock key={key++} block={block} streaming={partial} />)
      else if (lang === 'mermaid') out.push(closed ? <Mermaid key={key++} code={body} /> : <CodeBlock key={key++} code={body} info="mermaid" />)
      else if (lang === 'svg' && closed) out.push(<SvgBlock key={key++} code={body} />)
      else if (lang === 'math' || lang === 'latex' || lang === 'tex' || lang === 'katex') out.push(<MathTex key={key++} tex={body} display />)
      else out.push(<CodeBlock key={key++} code={body} info={info} />)
      continue
    }
    if (!line.trim()) { i++; continue }
    // 独立公式块：$$ … $$ 或 \[ … \]
    const mathOpen = line.match(MATH_OPEN)
    if (mathOpen) {
      const close = mathOpen[1] === '$$' ? '$$' : '\\]'
      const rest = mathOpen[2]
      const buf: string[] = []
      if (rest.trim().endsWith(close) && rest.trim().length >= close.length) {
        buf.push(rest.trim().slice(0, -close.length))
        i++
      } else {
        if (rest.trim()) buf.push(rest)
        i++
        while (i < lines.length && !lines[i].trim().endsWith(close)) buf.push(lines[i++])
        if (i < lines.length) { const t = lines[i].trim(); buf.push(t.slice(0, -close.length)); i++ }
      }
      out.push(<MathTex key={key++} tex={buf.join('\n').trim()} display />)
      continue
    }
    const h = line.match(ATX)
    if (h) {
      out.push(<Heading key={key++} level={h[1].length}>{inline(h[2] || '')}</Heading>)
      i++
      continue
    }
    if (HR.test(line)) { out.push(<hr key={key++} />); i++; continue }
    // 模型有时会写轻量的 <callout 说明> / <difficulty> 标签；优先转成平台提示块，
    // 避免把标签原文直接显示在回答里。
    if (CUSTOM_CALLOUT.test(line) || CUSTOM_DIFFICULTY.test(line)) {
      const { node, next } = customTag(lines, i, ctx, key++)
      out.push(node)
      i = next
      continue
    }
    // 原生 HTML 块
    if (HTML_BLOCK.test(line)) {
      const { node, next } = htmlBlock(lines, i, ctx, key++)
      if (node) out.push(node)
      i = next
      continue
    }
    if (QUOTE.test(line)) {
      const buf: string[] = []
      while (i < lines.length && (QUOTE.test(lines[i]) || (lines[i].trim() && buf.length && !isBlockStart(lines[i], lines[i + 1])))) {
        buf.push(lines[i++].replace(/^ {0,3}>\s?/, ''))
      }
      const alert = buf[0]?.trim().match(/^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(.*)$/i)
      if (alert) {
        const kind = alert[1].toLowerCase()
        const rest = [alert[2], ...buf.slice(1)].join('\n')
        out.push(
          <div key={key++} className={`md-alert md-alert-${kind}`} role="note">
            <div className="md-alert-title">{ALERT_LABEL[kind]}</div>
            {renderBlocks(rest, ctx)}
          </div>,
        )
      } else out.push(<blockquote key={key++}>{renderBlocks(buf.join('\n'), ctx)}</blockquote>)
      continue
    }
    // 表格：表头行 + 分隔行
    if (line.includes('|') && i + 1 < lines.length && TABLE_DELIM.test(lines[i + 1]) && !BULLET.test(line)) {
      const header = splitRow(line)
      const aligns = splitRow(lines[i + 1]).map((c) => (/^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'right' : /^:-/.test(c) ? 'left' : undefined))
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) rows.push(splitRow(lines[i++]))
      out.push(<MdTable key={key++} header={header} aligns={aligns} rows={rows} />)
      continue
    }
    if (BULLET.test(line) || ORDERED.test(line)) {
      const { node, next } = list(lines, i, ctx, key++)
      out.push(node)
      i = next
      continue
    }
    // 段落（Setext 标题：下一行全是 = 或 -）
    const buf: string[] = []
    while (i < lines.length && lines[i].trim()) {
      if (buf.length && isBlockStart(lines[i], lines[i + 1]) && !/^ {0,3}(=+|-+)\s*$/.test(lines[i])) break
      if (buf.length && /^ {0,3}=+\s*$/.test(lines[i])) { out.push(<Heading key={key++} level={1}>{inline(buf.join(' '))}</Heading>); buf.length = 0; i++; break }
      if (buf.length && /^ {0,3}-+\s*$/.test(lines[i])) { out.push(<Heading key={key++} level={2}>{inline(buf.join(' '))}</Heading>); buf.length = 0; i++; break }
      buf.push(lines[i++])
    }
    if (buf.length) {
      out.push(<p key={key++}>{buf.map((b, j) => {
        const hard = /( {2,}|\\)$/.test(b)
        return <Fragment key={j}>{j > 0 && <br />}{inline(hard ? b.replace(/( {2,}|\\)$/, '') : b.trimStart())}</Fragment>
      })}</p>)
    } else if (i < lines.length && lines[i].trim() && !out.length) {
      out.push(<p key={key++}>{inline(lines[i++])}</p>) // 保险：不会卡死在无法识别的行
    }
  }
  return out
}

const ALERT_LABEL: Record<string, string> = { note: '说明', tip: '提示', important: '重要', warning: '注意', caution: '警告' }

function Heading({ level, children }: { level: number; children: ReactNode }) {
  // 保留 Markdown 原始语义层级：# -> h1、## -> h2 …，样式仍由 md-h1…md-h6 控制。
  const n = Math.max(1, Math.min(6, level))
  const Tag = `h${n}` as 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'h6'
  return <Tag className={`md-h md-h${n}`}>{children}</Tag>
}

/* ------------------------------------------------------------------ 列表 */

function list(lines: string[], start: number, ctx: Ctx, key: number): { node: ReactNode; next: number } {
  const first = lines[start]
  const ordered = ORDERED.test(first) && !BULLET.test(first)
  const baseIndent = first.length - first.trimStart().length
  const startNo = ordered ? Number(first.match(ORDERED)![2]) : 1
  const items: { body: string[]; loose: boolean }[] = []
  let i = start
  let sawBlank = false
  while (i < lines.length) {
    const line = lines[i]
    const m = ordered ? line.match(ORDERED) : line.match(BULLET)
    const indent = line.length - line.trimStart().length
    if (m && indent >= baseIndent && indent < baseIndent + 4 && (ordered ? !BULLET.test(line) : true)) {
      // 新的一项：内容缩进 = 标记后第一个非空字符的位置
      const markerLen = ordered ? m[2].length + m[3].length : 1
      const spaces = (ordered ? m[4] : m[3]).length
      const contentIndent = indent + markerLen + (spaces > 4 || spaces === 0 ? 1 : spaces)
      const firstText = ordered ? m[5] : m[4]
      if (items.length && sawBlank) items[items.length - 1].loose = true
      items.push({ body: [firstText], loose: false })
      sawBlank = false
      i++
      // 收这一项的后续行
      while (i < lines.length) {
        const l = lines[i]
        if (!l.trim()) {
          // 空行：后面还缩进在本项里就继续，否则结束
          let j = i + 1
          while (j < lines.length && !lines[j].trim()) j++
          if (j < lines.length && lines[j].length - lines[j].trimStart().length >= contentIndent) {
            for (let k = i; k < j; k++) items[items.length - 1].body.push('')
            items[items.length - 1].loose = true
            i = j
            continue
          }
          sawBlank = true
          i = j
          break
        }
        const ind = l.length - l.trimStart().length
        if (ind >= contentIndent) { items[items.length - 1].body.push(l.slice(contentIndent)); i++; continue }
        // 懒续行：上一行是普通文字、这一行也不是新块
        const prev = items[items.length - 1].body[items[items.length - 1].body.length - 1] ?? ''
        if (prev.trim() && !isBlockStart(l, lines[i + 1]) && !FENCE.test(prev)) { items[items.length - 1].body.push(l.trim()); i++; continue }
        break
      }
      if (i < lines.length && sawBlank) {
        const l = lines[i]
        const mm = ordered ? l.match(ORDERED) : l.match(BULLET)
        const ind = l.length - l.trimStart().length
        if (!mm || ind < baseIndent || ind >= baseIndent + 4) break
      }
      continue
    }
    break
  }
  const loose = items.some((x) => x.loose)
  const lis = items.map((it, j) => {
    const task = it.body[0].match(/^\[( |x|X)\]\s+(.*)$/)
    const body = task ? [task[2], ...it.body.slice(1)] : it.body
    const simple = !loose && body.length === 1
    const content = simple ? inline(body[0]) : renderBlocks(body.join('\n'), ctx)
    return task
      ? <li key={j} className={`md-task${loose ? ' loose' : ''}`}><input type="checkbox" checked={task[1] !== ' '} readOnly aria-label="任务" /><div className="md-task-body">{content}</div></li>
      : <li key={j} className={loose ? 'loose' : undefined}>{content}</li>
  })
  return {
    node: ordered ? <ol key={key} start={startNo !== 1 ? startNo : undefined}>{lis}</ol> : <ul key={key}>{lis}</ul>,
    next: i,
  }
}

/* ------------------------------------------------------------------ 表格 */

function splitRow(line: string): string[] {
  const cells: string[] = []
  let cur = ''
  let code = false
  const s = line.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '')
  for (let k = 0; k < s.length; k++) {
    const ch = s[k]
    if (ch === '\\' && s[k + 1] === '|') { cur += '|'; k++; continue }
    if (ch === '`') code = !code
    if (ch === '|' && !code) { cells.push(cur.trim()); cur = ''; continue }
    cur += ch
  }
  cells.push(cur.trim())
  return cells
}

function MdTable({ header, aligns, rows }: { header: string[]; aligns: (string | undefined)[]; rows: string[][] }) {
  const ref = useRef<HTMLTableElement>(null)
  if (rows.length > 8 || header.length > 6) {
    // 大表：排序、筛选、点击复制、导出 CSV
    const columns = header.map((c, j) => ({ key: `c${j}`, label: c.replace(/\*\*/g, ''), align: aligns[j] === 'right' ? ('right' as const) : undefined }))
    const objs = rows.map((r) => Object.fromEntries(header.map((_, j) => [`c${j}`, r[j] ?? ''])))
    return <div className="md-table-x"><DataTable columns={columns} rows={objs} renderCell={(r, c) => inline(String(r[c.key] ?? ''))} /></div>
  }
  const style = (j: number) => (aligns[j] ? { textAlign: aligns[j] as 'left' | 'center' | 'right' } : undefined)
  return (
    <div className="table-wrap md-table">
      <div className="md-html-tools"><CopyTableButton getTable={() => ref.current} label="复制" /></div>
      <table ref={ref}>
        <thead><tr>{header.map((c, j) => <th key={j} style={style(j)}>{inline(c)}</th>)}</tr></thead>
        <tbody>{rows.map((r, ri) => <tr key={ri}>{header.map((_, j) => <td key={j} className="wrap" style={style(j)}>{inline(r[j] ?? '')}</td>)}</tr>)}</tbody>
      </table>
    </div>
  )
}

/* ------------------------------------------------------------------ 原生 HTML */

function htmlBlock(lines: string[], start: number, ctx: Ctx, key: number): { node: ReactNode; next: number } {
  const first = lines[start].trim()
  // 注释：整段跳过
  if (first.startsWith('<!--')) {
    let i = start
    while (i < lines.length && !lines[i].includes('-->')) i++
    return { node: null, next: i + 1 }
  }
  const tag = (first.match(/^<\/?([a-zA-Z][\w-]*)/)?.[1] || '').toLowerCase()
  if (tag === 'details' && /^<details(?:\s|>)/i.test(first)) return details(lines, start, ctx, key)
  // 找到与起始标签配平的结束标签；没有结束标签（或是空元素）就到空行为止
  const open = new RegExp(`<${tag}(\\s|>|/>)`, 'gi')
  const close = new RegExp(`</${tag}\\s*>`, 'gi')
  let depth = 0
  let i = start
  const buf: string[] = []
  const balanced = !VOID.has(tag) && close.test(lines.slice(start).join('\n'))
  close.lastIndex = 0
  while (i < lines.length) {
    const l = lines[i]
    if (!balanced && !l.trim()) break
    buf.push(l)
    i++
    if (balanced) {
      depth += (l.match(open) || []).length - (l.match(close) || []).length
      if (depth <= 0) break
    }
  }
  const html = buf.join('\n')
  if (tag === 'svg') return { node: <SvgBlock key={key} code={html} />, next: i }
  return { node: <HtmlBlock key={key} html={html} />, next: i }
}

/** 轻量自定义标签：<callout 说明>…</callout>、<difficulty>…</difficulty>。 */
function customTag(lines: string[], start: number, ctx: Ctx, key: number): { node: ReactNode; next: number } {
  const line = lines[start]
  const callout = line.match(CUSTOM_CALLOUT)
  const difficulty = line.match(CUSTOM_DIFFICULTY)
  const close = callout ? '</callout>' : '</difficulty>'
  let body = (callout ? callout[2] : difficulty?.[1] || '').trim()
  let i = start + 1
  if (body.includes(close)) {
    body = body.slice(0, body.indexOf(close)).trim()
  } else {
    const parts = body ? [body] : []
    while (i < lines.length) {
      const raw = lines[i]
      if (raw.includes(close)) {
        parts.push(raw.slice(0, raw.indexOf(close)))
        i++
        break
      }
      if (!raw.trim() || CUSTOM_CALLOUT.test(raw) || CUSTOM_DIFFICULTY.test(raw)) break
      parts.push(raw)
      i++
    }
    body = parts.join('\n').trim()
  }
  if (difficulty) {
    return {
      node: (
        <div key={key} className="md-callout md-callout-difficulty" role="note">
          <div className="md-callout-label">行动建议</div>
          {renderBlocks(body, ctx)}
        </div>
      ),
      next: i,
    }
  }
  const rawTitle = (callout?.[1] || '说明').trim().replace(/^["']|["']$/g, '')
  const tone = /警告|注意|风险|warn/i.test(rawTitle) ? 'warn' : /错误|失败|danger/i.test(rawTitle) ? 'danger' : 'note'
  return {
    node: (
      <div key={key} className={`md-callout md-callout-${tone}`} role="note">
        <div className="md-callout-label">{rawTitle || '说明'}</div>
        {renderBlocks(body, ctx)}
      </div>
    ),
    next: i,
  }
}

/** <details><summary>标题</summary> …Markdown… </details>：可折叠，正文照样按 Markdown 渲染（支持嵌套）。 */
function details(lines: string[], start: number, ctx: Ctx, key: number): { node: ReactNode; next: number } {
  let text = lines.slice(start).join('\n')
  const head = text.match(/^\s*<details(\s+open)?[^>]*>/i)
  if (!head) return { node: <HtmlBlock key={key} html={text} />, next: lines.length }
  const isOpen = !!head[1]
  text = text.slice(head[0].length)
  // 配平 </details>
  const re = /<details[\s>]|<\/details\s*>/gi
  let depth = 1
  let end = -1
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    depth += m[0].startsWith('</') ? -1 : 1
    if (depth === 0) { end = m.index; break }
  }
  const inner = end >= 0 ? text.slice(0, end) : text
  const consumed = head[0].length + (end >= 0 ? end + (m ? m[0].length : 0) : text.length)
  const all = lines.slice(start).join('\n').slice(0, consumed)
  const next = start + all.split('\n').length
  const sm = inner.match(/^\s*<summary[^>]*>([\s\S]*?)<\/summary>/i)
  const summary = sm ? sm[1].trim() : '详情'
  const body = (sm ? inner.slice(sm.index! + sm[0].length) : inner).replace(/^\n+/, '')
  return {
    node: (
      <details key={key} className="md-details" open={isOpen}>
        <summary>{inline(summary)}</summary>
        <div className="md-details-body">{renderBlocks(dedent(body), ctx)}</div>
      </details>
    ),
    next,
  }
}

function dedent(s: string): string {
  const ls = s.split('\n')
  const ind = Math.min(...ls.filter((l) => l.trim()).map((l) => l.length - l.trimStart().length), 99)
  return ind && ind < 99 ? ls.map((l) => l.slice(Math.min(ind, l.length - l.trimStart().length))).join('\n') : s
}

/* ------------------------------------------------------------------ 脚注 */

function extractFootnotes(src: string): { rest: string; defs: Map<string, string> } {
  const defs = new Map<string, string>()
  if (!src.includes('[^')) return { rest: src, defs }
  const lines = src.replace(/\r\n?/g, '\n').split('\n')
  const keep: string[] = []
  let fence = false
  for (let i = 0; i < lines.length; i++) {
    if (FENCE.test(lines[i])) fence = !fence
    const m = !fence && lines[i].match(/^\[\^([^\]\s]+)\]:\s*(.*)$/)
    if (m) {
      let body = m[2]
      while (i + 1 < lines.length && /^ {2,}\S/.test(lines[i + 1])) body += ' ' + lines[++i].trim()
      defs.set(m[1], body)
    } else keep.push(lines[i])
  }
  return { rest: keep.join('\n'), defs }
}

function FootRef({ id }: { id: string }) {
  const ctx = useContext(NoteCtx)
  const n = ctx?.notes.get(id)
  if (!ctx || !n) return <CiteMark n={id} />
  return (
    <sup className="md-fnref">
      <a href={`#${ctx.anchor}-fn-${id}`} onClick={(e) => { e.preventDefault(); document.getElementById(`${ctx.anchor}-fn-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }}>{n}</a>
    </sup>
  )
}

/* ------------------------------------------------------------------ 行内 */

const INLINE_TAGS = 'b|i|u|s|em|strong|sub|sup|kbd|mark|small|abbr|span|ins|del|code|q|cite|var|samp|font|a|time|bdi|ruby|rt|rp'
const TOKEN_SRC = [
  /(\\([!-/:-@[-`{-~]))/.source,                                       // 1-2 转义
  '((`+)([\\s\\S]*?[^`])\\4(?!`))',                               // 3-5 代码（支持 ``a`b``；\4 回引用反引号串）
  /(!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\))/.source,                 // 6-8 图片
  /(\[([^\]]+)\]\(((?:https?:\/\/|mailto:|#\/|\/api\/)[^)\s]*)(?:\s+"[^"]*")?\))/.source, // 9-11 链接
  /(<(https?:\/\/[^>\s]+)>)/.source,                                    // 12-13 自动链接
  `(<(${INLINE_TAGS})(?:\\s[^<>]*)?>[\\s\\S]*?<\\/\\15\\s*>|<(?:br|wbr|img)(?:\\s[^<>]*)?\\/?>)`, // 14-15 行内 HTML（\15 回引用标签名）
  /(\*\*\*([^*]+)\*\*\*)/.source,                                       // 16-17 粗斜体
  /(\*\*([^*]+(?:\*(?!\*)[^*]*)*)\*\*|(?<![\w])__([^_]+)__(?![\w]))/.source, // 18-20 粗体
  /(~~([^~]+)~~)/.source,                                               // 21-22 删除线
  /(==([^=\s][^=]*?)==)/.source,                                        // 23-24 高亮
  /(\*([^*\s][^*]*?)\*|(?<![\w])_([^_\s][^_]*?)_(?![\w]))/.source,      // 25-27 斜体
  /(\\\((.+?)\\\))/.source,                                             // 28-29 \( \)
  /((?<![\\$\w])\$(?![\s$\d])([^$\n]+?)(?<![\s\\])\$(?![\d$]))/.source,   // 30-31 $…$（避开「$100 与 $200」这类金额）
  /(\[\^([^\]\s]+)\](?!\(|:))/.source,                                  // 32-33 脚注 / 引用 [^x]
  /(\[(\d{1,3})\](?!\())/.source,                                       // 34-35 引用 [n]
  /(https?:\/\/[^\s<>()，。；、]+[^\s<>()，。；、.,;:!?'"])/.source,       // 36 裸链接
  /(:qbot-([a-z0-9-]+):)/.source,                                      // 37-38 QuantLab Qmoji 短码
].join('|')

export function inline(text: string): ReactNode[] {
  // 每次调用新建正则：粗体内容会递归调用 inline，共享 lastIndex 会导致死循环
  const TOKEN = new RegExp(TOKEN_SRC, 'g')
  const out: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  let k = 0
  while ((m = TOKEN.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const t = m[0]
    if (m[1]) out.push(m[2])
    else if (m[3]) out.push(<code key={k++} className="inline">{m[5].replace(/^ (.*) $/, '$1')}</code>)
    else if (m[6]) out.push(<ImageView key={k++} src={m[8]} alt={m[7]} />)
    else if (m[9]) {
      const raw = m[11]
      const href = safeHref(raw) || (/^mailto:/i.test(raw) ? raw : undefined)
      out.push(href
        ? <a key={k++} href={href} target={href.startsWith('#') || href.startsWith('mailto:') ? undefined : '_blank'} rel="noreferrer noopener">{inline(m[10])}</a>
        : <Fragment key={k++}>{m[10]}</Fragment>)
    } else if (m[12]) out.push(<a key={k++} href={m[13]} target="_blank" rel="noreferrer noopener">{m[13]}</a>)
    else if (m[14]) out.push(<InlineHtml key={k++} html={t} />)
    else if (m[16]) out.push(<strong key={k++}><em>{inline(m[17])}</em></strong>)
    else if (m[18]) out.push(<strong key={k++}>{inline(m[19] ?? m[20])}</strong>)
    else if (m[21]) out.push(<del key={k++}>{inline(m[22])}</del>)
    else if (m[23]) out.push(<mark key={k++} className="md-mark">{inline(m[24])}</mark>)
    else if (m[25]) out.push(<em key={k++}>{inline(m[26] ?? m[27])}</em>)
    else if (m[28]) out.push(<MathTex key={k++} tex={m[29]} />)
    else if (m[30]) out.push(<MathTex key={k++} tex={m[31]} />)
    else if (m[32]) out.push(<FootRef key={k++} id={m[33]} />)
    else if (m[34]) out.push(<CiteMark key={k++} n={m[35]} />)
    else if (m[36]) out.push(<a key={k++} href={t} target="_blank" rel="noreferrer noopener">{t.length > 60 ? t.slice(0, 57) + '…' : t}</a>)
    else if (m[37]) {
      const q = qmojiById(m[38])
      out.push(q ? <Qmoji key={k++} id={q.id} size={18} animated="once" /> : t)
    }
    last = m.index + t.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}
