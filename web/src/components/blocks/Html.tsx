import DOMPurify from 'dompurify'
import { useMemo, useRef, useState } from 'react'
import { copyText, downloadBlob, safeHref, safeImageSrc } from './protocol'
import { proxied } from './Media'
import { useReportRenderError } from './renderErrors'

/**
 * 原生 HTML 与内联 SVG：帖子、AI 回答、报告里允许写 <details>、<table>、<kbd>、<svg> 等。
 * 全部经 DOMPurify 净化：去掉脚本、样式表、iframe、表单、事件属性与危险链接；
 * style 属性只保留不会跳出容器的声明（禁止 position: fixed / sticky、url()、expression）。
 * 容器用 CSS contain 隔离，净化后的内容也盖不住页面其他部分。
 */

const FORBID_TAGS = [
  'script', 'style', 'iframe', 'frame', 'frameset', 'object', 'embed', 'applet', 'link', 'meta', 'base',
  'form', 'button', 'textarea', 'select', 'option', 'foreignObject', 'template', 'noscript', 'portal',
]

let hooked = false
function installHooks() {
  if (hooked) return
  hooked = true
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    const el = node as Element
    if (el.tagName === 'A') {
      const href = safeHref(el.getAttribute('href') || '') || (/^mailto:/i.test(el.getAttribute('href') || '') ? el.getAttribute('href') : null)
      if (href) {
        el.setAttribute('href', href)
        if (!href.startsWith('#')) { el.setAttribute('target', '_blank'); el.setAttribute('rel', 'noreferrer noopener') }
      } else el.removeAttribute('href')
    }
    if (el.tagName === 'IMG') {
      const src = safeImageSrc(el.getAttribute('src') || '')
      if (src) { el.setAttribute('src', proxied(src)); el.setAttribute('loading', 'lazy'); el.setAttribute('referrerpolicy', 'no-referrer') } else el.removeAttribute('src')
    }
    if (el.tagName === 'INPUT') {
      // 只保留只读复选框（任务列表）
      if ((el.getAttribute('type') || '').toLowerCase() !== 'checkbox') el.remove()
      else el.setAttribute('disabled', '')
    }
    const style = el.getAttribute('style')
    if (style) {
      const kept = style.split(';').filter((d) => d.trim() && !/position\s*:\s*(fixed|sticky)|url\s*\(|expression\s*\(|behavior\s*:|-moz-binding|@import/i.test(d))
      if (kept.length) el.setAttribute('style', kept.join(';'))
      else el.removeAttribute('style')
    }
  })
}

export function sanitizeHtml(html: string, opts: { svg?: boolean } = {}): string {
  installHooks()
  return DOMPurify.sanitize(html, {
    USE_PROFILES: opts.svg ? { svg: true, svgFilters: true } : { html: true, svg: true, svgFilters: true },
    FORBID_TAGS,
    FORBID_ATTR: ['srcdoc', 'formaction', 'xlink:href'],
    ADD_ATTR: ['target', 'open'],
    ALLOW_DATA_ATTR: false,
  }) as unknown as string
}

/** 表格转成制表符分隔文本：粘进 Excel / 飞书表格能保持行列。 */
export function tableToTsv(table: HTMLTableElement): string {
  return [...table.rows].map((r) => [...r.cells].map((c) => (c.innerText || '').replace(/\s*\n\s*/g, ' ').trim()).join('\t')).join('\n')
}

export function CopyTableButton({ getTable, label = '复制表格' }: { getTable: () => HTMLTableElement | null; label?: string }) {
  const [done, setDone] = useState(false)
  return (
    <button type="button" className="qb-icon-btn md-copy-table" onClick={async () => {
      const t = getTable()
      if (t && (await copyText(tableToTsv(t)))) { setDone(true); setTimeout(() => setDone(false), 1200) }
    }}>{done ? '已复制' : label}</button>
  )
}

/** 一段块级 HTML（表格、div、figure……）。含表格时给出「复制表格」。 */
export function HtmlBlock({ html }: { html: string }) {
  const clean = useMemo(() => sanitizeHtml(html), [html])
  const box = useRef<HTMLDivElement>(null)
  const hasTable = /<table[\s>]/i.test(clean)
  useReportRenderError(html.trim() && !clean.trim() ? { kind: 'html', message: 'HTML 内容被安全过滤后为空（脚本、iframe、表单等不允许）', source: html.slice(0, 400) } : null)
  if (!clean.trim()) return null
  return (
    <div className={`md-html${hasTable ? ' has-table' : ''}`}>
      {hasTable && <div className="md-html-tools"><CopyTableButton getTable={() => box.current?.querySelector('table') ?? null} /></div>}
      <div ref={box} className="md-html-body" dangerouslySetInnerHTML={{ __html: clean }} />
    </div>
  )
}

/** 行内 HTML 片段（<kbd>、<sub>、<mark>、<br> …）。 */
export function InlineHtml({ html }: { html: string }) {
  const clean = useMemo(() => sanitizeHtml(html), [html])
  return <span className="md-inline-html" dangerouslySetInnerHTML={{ __html: clean }} />
}

/** ```svg 代码块或独立的 <svg>…</svg>：净化后直接显示，可切换源码、下载。 */
export function SvgBlock({ code }: { code: string }) {
  const clean = useMemo(() => {
    const m = code.match(/<svg[\s\S]*<\/svg>/i)
    return m ? sanitizeHtml(m[0], { svg: true }) : ''
  }, [code])
  const [src, setSrc] = useState(false)
  const ok = /<svg[\s>]/i.test(clean)
  useReportRenderError(ok ? null : { kind: 'svg', message: /<svg/i.test(code) ? 'SVG 解析失败或被安全过滤为空' : '代码块里没有完整的 <svg>…</svg>', source: code.slice(0, 600) })
  return (
    <figure className="qb-mermaid md-svg">
      <figcaption className="qb-code-head">
        <span className="qb-code-name">示意图</span>
        <span className="qb-grow" />
        {ok && <button type="button" className="qb-icon-btn" onClick={() => downloadBlob('figure.svg', clean, 'image/svg+xml')}>下载 SVG</button>}
        <button type="button" className="qb-icon-btn" onClick={() => setSrc(!src)}>{src ? '看图' : '源码'}</button>
      </figcaption>
      {src || !ok
        ? <pre className="qb-math-src">{code}{!ok && '\n\n# 渲染失败：没有可显示的 SVG'}</pre>
        : <div className="qb-mermaid-svg md-svg-body anim-fade" dangerouslySetInnerHTML={{ __html: clean }} />}
    </figure>
  )
}
