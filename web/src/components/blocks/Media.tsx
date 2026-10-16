import { useEffect, useId, useState, type ReactNode } from 'react'
import { viewImages } from '../ImageViewer'
import { withWs } from '../../http'
import { downloadBlob, safeImageSrc } from './protocol'

/**
 * 公式、Mermaid 与图片。KaTeX / Mermaid 都按需加载（第一次遇到公式或图时才下载对应的包），
 * 不影响首屏体积；加载前先显示源码，加载失败也退回源码。
 */

// ---------------------------------------------------------------- KaTeX
type Katex = typeof import('katex')
let katexMod: Katex | null = null
let katexLoading: Promise<Katex> | null = null
function loadKatex(): Promise<Katex> {
  if (katexMod) return Promise.resolve(katexMod)
  katexLoading ||= Promise.all([import('katex'), import('katex/dist/katex.min.css')]).then(([m]) => {
    katexMod = ((m as unknown as { default?: Katex }).default || m) as Katex
    return katexMod
  })
  return katexLoading
}

export function MathTex({ tex, display }: { tex: string; display?: boolean }) {
  const [html, setHtml] = useState<string | null>(() => (katexMod ? render(katexMod, tex, display) : null))
  useEffect(() => {
    let alive = true
    loadKatex().then((k) => alive && setHtml(render(k, tex, display))).catch(() => alive && setHtml(''))
    return () => { alive = false }
  }, [tex, display])
  if (!html) return display ? <pre className="qb-math-src">{tex}</pre> : <code className="inline">{tex}</code>
  // KaTeX 输出的是它自己生成的受限 HTML（trust: false，不允许 \href / \htmlClass 等），可以安全插入
  return display
    ? <div className="qb-math display" dangerouslySetInnerHTML={{ __html: html }} />
    : <span className="qb-math" dangerouslySetInnerHTML={{ __html: html }} />
}

function render(k: Katex, tex: string, display?: boolean): string {
  try {
    return k.renderToString(tex, { displayMode: !!display, throwOnError: false, trust: false, strict: 'ignore', output: 'html' })
  } catch {
    return ''
  }
}

// ---------------------------------------------------------------- Mermaid
type MermaidApi = { initialize: (c: Record<string, unknown>) => void; render: (id: string, code: string) => Promise<{ svg: string }> }
let mermaidMod: Promise<MermaidApi> | null = null
function loadMermaid(): Promise<MermaidApi> {
  // 用 Mermaid 自带的已压缩、自包含构建（内部已按图类型拆分），避免把 cytoscape 等大依赖拼成超过 1MB 的 chunk
  mermaidMod ||= import('mermaid/dist/mermaid.esm.min.mjs').then((m) => {
    const api = (m as unknown as { default: MermaidApi }).default
    const dark = document.documentElement.dataset.theme === 'dark'
    api.initialize({ startOnLoad: false, securityLevel: 'strict', theme: dark ? 'dark' : 'neutral', fontFamily: 'inherit' })
    return api
  })
  return mermaidMod
}

export function Mermaid({ code }: { code: string }) {
  const id = 'mm' + useId().replace(/[^a-zA-Z0-9]/g, '')
  const [svg, setSvg] = useState('')
  const [err, setErr] = useState('')
  const [showSrc, setShowSrc] = useState(false)
  useEffect(() => {
    let alive = true
    const t = window.setTimeout(() => {
      loadMermaid()
        .then((m) => m.render(id, code))
        .then((r) => { if (alive) { setSvg(r.svg); setErr('') } })
        .catch((e) => { if (alive) setErr(String((e as Error).message || e).slice(0, 200)) })
    }, 120) // 流式输出时等内容稳定一下再画
    return () => { alive = false; window.clearTimeout(t) }
  }, [code, id])
  return (
    <figure className="qb-mermaid">
      <figcaption className="qb-code-head">
        <span className="qb-code-name">流程图</span>
        <span className="qb-grow" />
        {svg && <button type="button" className="qb-icon-btn" onClick={() => downloadBlob('diagram.svg', svg, 'image/svg+xml')}>下载 SVG</button>}
        <button type="button" className="qb-icon-btn" onClick={() => setShowSrc(!showSrc)}>{showSrc ? '看图' : '源码'}</button>
      </figcaption>
      {showSrc || (!svg && err) ? <pre className="qb-math-src">{code}{err && `\n\n# 渲染失败：${err}`}</pre> : svg
        ? <div className="qb-mermaid-svg anim-fade" dangerouslySetInnerHTML={{ __html: svg }} /> // securityLevel=strict：Mermaid 自己净化过
        : <div className="shimmer-line" aria-hidden />}
    </figure>
  )
}

// ---------------------------------------------------------------- 图片与画廊

/** 兼容旧调用：画廊由全站查看器按容器自动收集（components/ImageViewer.tsx），这里只透传。 */
export function GalleryProvider({ children }: { children: ReactNode }) {
  return <>{children}</>
}

export function saveImage(src: string, name = '') {
  const fname = name || src.split('/').pop()?.split('?')[0] || 'image.png'
  if (src.startsWith('data:')) {
    const a = document.createElement('a')
    a.href = src
    a.download = /\.\w+$/.test(fname) ? fname : 'image.png'
    a.click()
    return
  }
  fetch(src, { credentials: src.startsWith('/') ? 'include' : 'omit' })
    .then((r) => r.blob())
    .then((b) => downloadBlob(/\.\w+$/.test(fname) ? fname : `${fname}.png`, b, b.type))
    .catch(() => window.open(src, '_blank', 'noopener'))
}

/** 外链图片走服务端代理：第一次显示时下载并缓存进工作区（自动下载），之后链接失效也能看。 */
export function proxied(src: string): string {
  return /^https?:\/\//i.test(src) ? withWs(`/api/media/image?url=${encodeURIComponent(src)}`) : src
}

export function ImageView({ src, alt }: { src: string; alt?: string }) {
  const original = safeImageSrc(src)
  const [state, setState] = useState<'loading' | 'ok' | 'error'>('loading')
  const [direct, setDirect] = useState(false) // 代理失败（例如服务端连不上外网）时退回浏览器直连
  const safe = original && !direct ? proxied(original) : original
  if (!safe) return <span className="qb-muted">[图片地址不受支持：{String(src).slice(0, 60)}]</span>
  return (
    <span className={`qb-img ${state}`}>
      {/* 点击由全站查看器在捕获阶段接管（.md img），在当前页面大屏打开 */}
      <img src={safe} alt={alt || ''} loading="lazy" referrerPolicy="no-referrer" onLoad={() => setState('ok')}
        onError={() => { if (!direct && original && safe !== original) setDirect(true); else setState('error') }} />
      {state === 'error' && <span className="qb-muted">图片加载失败 · <a href={original} target="_blank" rel="noreferrer noopener">打开原图</a></span>}
      {state === 'ok' && (
        <span className="qb-img-bar">
          {alt && <span className="qb-img-alt">{alt}</span>}
          <button type="button" onClick={() => viewImages([{ src: safe, name: alt }])}>放大</button>
          <button type="button" onClick={() => saveImage(safe, alt)}>下载</button>
        </span>
      )}
    </span>
  )
}
