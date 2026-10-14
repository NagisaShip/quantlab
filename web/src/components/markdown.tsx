import { Fragment, type ReactNode } from 'react'

/**
 * 轻量 Markdown 渲染：标题、段落、列表、引用、代码块、表格、行内代码 / 粗体 / 斜体 / 链接。
 * 全部生成 React 元素，不使用 innerHTML —— 帖子与 AI 输出都是不可信文本。
 */
export function Markdown({ text, compact }: { text: string; compact?: boolean }) {
  return <div className={`md${compact ? ' compact' : ''}`}>{renderBlocks(text || '')}</div>
}

function renderBlocks(src: string): ReactNode[] {
  const lines = src.replace(/\r\n?/g, '\n').split('\n')
  const out: ReactNode[] = []
  let i = 0
  let key = 0
  while (i < lines.length) {
    const line = lines[i]
    // 代码块
    const fence = line.match(/^\s*```\s*([\w+-]*)/)
    if (fence) {
      const buf: string[] = []
      i++
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++])
      i++
      out.push(
        <pre key={key++} className="code md-code" data-lang={fence[1] || undefined}>
          {buf.join('\n')}
        </pre>,
      )
      continue
    }
    if (!line.trim()) {
      i++
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
      out.push(<blockquote key={key++}>{renderBlocks(buf.join('\n'))}</blockquote>)
      continue
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const header = splitRow(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(splitRow(lines[i++]))
      out.push(
        <div className="table-wrap md-table" key={key++}>
          <table>
            <thead>
              <tr>{header.map((c, j) => <th key={j}>{inline(c)}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>{r.map((c, j) => <td key={j} className="wrap">{inline(c)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
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
      const lis = items.map((it, j) => <li key={j}>{inline(it)}</li>)
      out.push(ordered ? <ol key={key++}>{lis}</ol> : <ul key={key++}>{lis}</ul>)
      continue
    }
    const buf: string[] = []
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*(```|#{1,4}\s|>|([-*+]|\d+[.)])\s+)/.test(lines[i]) &&
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
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim())
}

const TOKEN_SRC = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\((https?:\/\/[^)\s]+|#\/[^)\s]*)\))/.source

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
    else if (m[2]) out.push(<strong key={k++}>{inline(t.slice(2, -2))}</strong>)
    else if (m[3]) out.push(<em key={k++}>{t.slice(1, -1)}</em>)
    else if (m[4]) {
      const label = t.slice(1, t.indexOf(']('))
      const href = m[5]
      out.push(
        <a key={k++} href={href} target={href.startsWith('#') ? undefined : '_blank'} rel="noreferrer noopener">
          {label}
        </a>,
      )
    }
    last = m.index + t.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}
