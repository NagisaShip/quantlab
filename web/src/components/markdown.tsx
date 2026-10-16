import { Fragment, useId, useMemo, type ReactNode } from 'react'
import { CodeBlock, parseFence } from './blocks/CodeBlock'
import { DataTable } from './blocks/DataTable'
import { GalleryProvider, ImageView, MathTex, Mermaid } from './blocks/Media'
import { CitationCtx, CiteMark, RichBlock, collectCitations } from './blocks/RichBlock'
import { parseBlock, safeHref } from './blocks/protocol'
import '../stylesBlocks.css'

/**
 * Markdown 渲染：标题、段落、列表、引用、表格（增强型）、代码块（高亮 / 行号 / Diff）、行内格式、
 * 图片（放大 / 画廊 / 下载）、数学公式（$…$、$$…$$）、Mermaid，以及 Agent 的结构化内容块（```ql:<类型>```）。
 * 全部生成 React 元素，不拼接不可信的 HTML —— 帖子与 AI 输出都是不可信文本；
 * 只有 KaTeX / Mermaid 输出的受限 HTML 例外（见 blocks/Media.tsx）。
 */
export function Markdown({ text, compact, streaming }: { text: string; compact?: boolean; streaming?: boolean }) {
  const anchor = 'md' + useId().replace(/[^a-zA-Z0-9]/g, '')
  const src = text || ''
  const sources = useMemo(() => collectCitations(src), [src])
  const body = useMemo(() => renderBlocks(src, !!streaming), [src, streaming])
  return (
    <CitationCtx.Provider value={{ sources, anchor }}>
      <GalleryProvider>
        <div className={`md${compact ? ' compact' : ''}`}>{body}</div>
      </GalleryProvider>
    </CitationCtx.Provider>
  )
}

function renderBlocks(src: string, streaming: boolean): ReactNode[] {
  const lines = src.replace(/\r\n?/g, '\n').split('\n')
  const out: ReactNode[] = []
  let i = 0
  let key = 0
  while (i < lines.length) {
    const line = lines[i]
    // 代码块 / 结构化块 / Mermaid
    const fence = line.match(/^\s*(```+|~~~+)\s*(.*)$/)
    if (fence) {
      const marker = fence[1]
      const info = fence[2].trim()
      const buf: string[] = []
      i++
      let closed = false
      while (i < lines.length) {
        if (lines[i].trim().startsWith(marker) && lines[i].trim().replace(/[`~]/g, '') === '') { closed = true; i++; break }
        buf.push(lines[i++])
      }
      const body = buf.join('\n')
      const lang = parseFence(info).lang.toLowerCase()
      const block = parseBlock(lang, body, streaming && !closed)
      if (block) out.push(<RichBlock key={key++} block={block} streaming={streaming && !closed} />)
      else if (lang === 'mermaid') out.push(closed ? <Mermaid key={key++} code={body} /> : <CodeBlock key={key++} code={body} info="mermaid" />)
      else if (lang === 'math' || lang === 'latex' || lang === 'tex') out.push(<MathTex key={key++} tex={body} display />)
      else out.push(<CodeBlock key={key++} code={body} info={info} />)
      continue
    }
    if (!line.trim()) {
      i++
      continue
    }
    // 独立公式块：$$ … $$ 或 \[ … \]
    const mathOpen = line.trim().match(/^(\$\$|\\\[)(.*)$/)
    if (mathOpen) {
      const close = mathOpen[1] === '$$' ? '$$' : '\\]'
      let rest = mathOpen[2]
      const buf: string[] = []
      if (rest.trim().endsWith(close) && rest.trim().length > close.length - 1) {
        buf.push(rest.trim().slice(0, -close.length))
        i++
      } else {
        if (rest.trim()) buf.push(rest)
        i++
        while (i < lines.length && !lines[i].trim().endsWith(close)) buf.push(lines[i++])
        if (i < lines.length) { rest = lines[i].trim(); buf.push(rest.slice(0, -close.length)); i++ }
      }
      out.push(<MathTex key={key++} tex={buf.join('\n').trim()} display />)
      continue
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/)
    if (h) {
      const level = h[1].length
      const content = inline(h[2])
      out.push(
        level === 1 ? <h2 key={key++}>{content}</h2> : level === 2 ? <h3 key={key++}>{content}</h3> : <h4 key={key++}>{content}</h4>,
      )
      i++
      continue
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) {
      out.push(<hr key={key++} />)
      i++
      continue
    }
    if (/^\s*>/.test(line)) {
      const buf: string[] = []
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''))
      out.push(<blockquote key={key++}>{renderBlocks(buf.join('\n'), streaming)}</blockquote>)
      continue
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const header = splitRow(line)
      const aligns = splitRow(lines[i + 1]).map((c) => (/-+:$/.test(c) ? 'right' : undefined))
      i += 2
      const rows: string[][] = []
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(splitRow(lines[i++]))
      const columns = header.map((c, j) => ({ key: `c${j}`, label: c.replace(/\*\*/g, ''), align: aligns[j] as 'right' | undefined }))
      const objs = rows.map((r) => Object.fromEntries(header.map((_, j) => [`c${j}`, r[j] ?? ''])))
      if (rows.length <= 4 && header.length <= 4) {
        // 小表格保持轻量：不加工具栏
        out.push(
          <div className="table-wrap md-table" key={key++}>
            <table>
              <thead><tr>{header.map((c, j) => <th key={j}>{inline(c)}</th>)}</tr></thead>
              <tbody>{rows.map((r, ri) => <tr key={ri}>{header.map((_, j) => <td key={j} className="wrap">{inline(r[j] ?? '')}</td>)}</tr>)}</tbody>
            </table>
          </div>,
        )
      } else {
        out.push(<div className="md-table-x" key={key++}><DataTable columns={columns} rows={objs} renderCell={(r, c) => inline(String(r[c.key] ?? ''))} /></div>)
      }
      continue
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line)
      const items: string[] = []
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        let item = lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, '')
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          item += ' ' + lines[i++].trim()
        }
        items.push(item)
      }
      const lis = items.map((it, j) => {
        const task = it.match(/^\[( |x|X)\]\s+(.*)$/)
        return task
          ? <li key={j} className="md-task"><input type="checkbox" checked={task[1] !== ' '} readOnly aria-label="任务" />{inline(task[2])}</li>
          : <li key={j}>{inline(it)}</li>
      })
      out.push(ordered ? <ol key={key++}>{lis}</ol> : <ul key={key++}>{lis}</ul>)
      continue
    }
    const buf: string[] = []
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*(```|~~~|#{1,4}\s|>|([-*+]|\d+[.)])\s+|\$\$|\\\[)/.test(lines[i]) &&
      !/^\s*\|.*\|\s*$/.test(lines[i])
    ) {
      buf.push(lines[i++])
    }
    if (!buf.length) {
      buf.push(lines[i++])
    }
    out.push(<p key={key++}>{buf.map((b, j) => <Fragment key={j}>{j > 0 && <br />}{inline(b)}</Fragment>)}</p>)
  }
  return out
}

function splitRow(line: string): string[] {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'))
}

// 行内：代码、图片、链接、粗体、斜体、删除线、公式（$…$ 与 \(…\)）、引用标记 [1] / [^1]
const TOKEN_SRC = [
  /(`[^`]+`)/.source,                                                  // 1 代码
  /(!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\))/.source,               // 2-4 图片
  /(\[([^\]]+)\]\((https?:\/\/[^)\s]+|#\/[^)\s]*|\/api\/[^)\s]*)\))/.source, // 5-7 链接
  /(\*\*[^*]+\*\*)/.source,                                            // 8 粗体
  /(~~[^~]+~~)/.source,                                                // 9 删除线
  /(\*[^*\s][^*]*\*)/.source,                                          // 10 斜体
  /(\\\((.+?)\\\))/.source,                                            // 11-12 \( \)
  /((?<![\\$\w])\$(?![\s$\d])([^$\n]+?)(?<![\s\\])\$(?![\d$]))/.source, // 13-14 $…$（避开「$100 与 $200」这类金额）
  /(\[\^?(\d{1,3})\](?!\())/.source,                                  // 15-16 引用
  /(https?:\/\/[^\s<>()，。；、]+[^\s<>()，。；、.,;:!?'"])/.source,      // 17 裸链接
].join('|')

function inline(text: string): ReactNode[] {
  // 每次调用新建正则：粗体内容会递归调用 inline，共享 lastIndex 会导致死循环
  const TOKEN = new RegExp(TOKEN_SRC, 'g')
  const out: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  let k = 0
  while ((m = TOKEN.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index))
    const t = m[0]
    if (m[1]) out.push(<code key={k++} className="inline">{t.slice(1, -1)}</code>)
    else if (m[2]) out.push(<ImageView key={k++} src={m[4]} alt={m[3]} />)
    else if (m[5]) {
      const href = safeHref(m[7])
      out.push(href
        ? <a key={k++} href={href} target={href.startsWith('#') ? undefined : '_blank'} rel="noreferrer noopener">{inline(m[6])}</a>
        : <Fragment key={k++}>{m[6]}</Fragment>)
    } else if (m[8]) out.push(<strong key={k++}>{inline(t.slice(2, -2))}</strong>)
    else if (m[9]) out.push(<del key={k++}>{t.slice(2, -2)}</del>)
    else if (m[10]) out.push(<em key={k++}>{t.slice(1, -1)}</em>)
    else if (m[11]) out.push(<MathTex key={k++} tex={m[12]} />)
    else if (m[13]) out.push(<MathTex key={k++} tex={m[14]} />)
    else if (m[15]) out.push(<CiteMark key={k++} n={m[16]} />)
    else if (m[17]) out.push(<a key={k++} href={t} target="_blank" rel="noreferrer noopener">{t.length > 60 ? t.slice(0, 57) + '…' : t}</a>)
    last = m.index + t.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}
