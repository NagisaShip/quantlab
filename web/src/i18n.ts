/**
 * 界面语言：简体中文（源语言）与 English。
 *
 * - 源码里的文案就是中文；英文从 i18n.en.ts 的「中文 → 英文」词典里查，查不到原样显示中文。
 * - t(中文) 给需要在 JS 里拿到译文的地方（document.title、aria-label、确认框…）。
 * - 其余静态文案由 startDomTranslator() 在渲染后逐个文本节点替换，组件不必逐个改写；
 *   代码编辑器、Markdown 正文（AI 输出）、数值等打了 data-no-i18n 的区域不翻译。
 * - 切换语言后整页重载：中文是唯一的「真值」，不做反向翻译。
 */

export type Lang = 'zh' | 'en'
const KEY = 'qa.lang'

function readLang(): Lang {
  try {
    const v = localStorage.getItem(KEY)
    if (v === 'en' || v === 'zh') return v
  } catch { /* 私密模式 */ }
  return 'zh'
}

export const lang: Lang = readLang()

export function setLang(v: Lang) {
  try { localStorage.setItem(KEY, v) } catch { /* 私密模式 */ }
  window.location.reload()
}

let EN: Record<string, string> = {}
let PATTERNS: [RegExp, string][] = []

/** 英文界面下先加载词典再渲染（词典单独打包，中文界面不下载）。 */
export async function loadLang() {
  if (lang === 'zh') return
  const m = await import('./i18n.en')
  EN = m.EN
  PATTERNS = m.EN_PATTERNS.map(([re, out]) => [new RegExp(`^${re}$`, 'u'), out])
}

/** 中文 → 当前语言。英文词典里没有的原样返回。 */
export function t(zh: string): string {
  if (lang === 'zh' || !zh) return zh
  const hit = EN[zh]
  if (hit !== undefined) return hit
  for (const [re, out] of PATTERNS) {
    const m = zh.match(re)
    if (m) return out.replace(/\$(\d)/g, (_, i) => t(m[Number(i)] ?? ''))
  }
  return zh
}

const CJK = /[一-鿿]/
// AI 输出（.md）只跳过正文（段落、列表、引用、标题、表格单元格）；结构化内容块的按钮与标签照常翻译
const SKIP = 'script,style,textarea,code,pre,.md > p,.md > ul,.md > ol,.md > blockquote,.md .md-h,.md td,.md th,.md-details,.md-html,.md-alert,.md-footnotes,.qb-claims li > span,.qb-snippet,.qb-note,.cm-editor,.code-editor,[data-no-i18n],[contenteditable="true"]'
const ATTRS = ['placeholder', 'title', 'aria-label'] as const
// 属性（占位符、提示）只在代码编辑器等区域里不翻译；输入框本身的占位符要翻译
const SKIP_ATTR = '.cm-editor,.code-editor,[data-no-i18n],[contenteditable="true"]'

function translateAttrs(e: Element) {
  if (e.closest(SKIP_ATTR)) return
  for (const a of ATTRS) {
    const v = e.getAttribute(a)
    if (v) { const out = translateText(v); if (out !== null) e.setAttribute(a, out) }
  }
}

function translateText(raw: string): string | null {
  if (!CJK.test(raw)) return null
  const lead = raw.match(/^\s*/)![0]
  const tail = raw.match(/\s*$/)![0]
  const core = raw.trim()
  const out = t(core)
  return out === core ? null : lead + out + tail
}

function translateNode(node: Node) {
  if (node.nodeType === Node.TEXT_NODE) {
    const p = node.parentElement
    if (!p || p.closest(SKIP)) return
    const out = translateText(node.nodeValue ?? '')
    if (out !== null && out !== node.nodeValue) node.nodeValue = out
    return
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return
  const el = node as Element
  translateAttrs(el)
  if (el.closest(SKIP)) return
  if (el instanceof HTMLInputElement && (el.type === 'button' || el.type === 'submit') && el.value) {
    const out = translateText(el.value); if (out !== null) el.value = out
  }
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT)
  let n = walker.nextNode()
  while (n) {
    if (n.nodeType === Node.ELEMENT_NODE) {
      const e = n as Element
      translateAttrs(e)
      if (e.matches(SKIP)) { n = skipSubtree(walker, e); continue }
    } else {
      const out = translateText(n.nodeValue ?? '')
      if (out !== null && out !== n.nodeValue) n.nodeValue = out
    }
    n = walker.nextNode()
  }
}

function skipSubtree(walker: TreeWalker, el: Element): Node | null {
  let n: Node | null = el
  while (n) {
    if (n.nextSibling) { walker.currentNode = n.nextSibling; return n.nextSibling }
    n = n.parentNode
    if (!n || n === walker.root) return null
  }
  return null
}

/** 英文界面下启动：先翻一遍整页，再跟着 React 的每次渲染翻新增或改动的节点。 */
export function startDomTranslator() {
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN'
  document.documentElement.dataset.lang = lang
  if (lang === 'zh') return
  translateNode(document.body)
  const obs = new MutationObserver((list) => {
    for (const m of list) {
      if (m.type === 'characterData') translateNode(m.target)
      else if (m.type === 'attributes') translateNode(m.target)
      else m.addedNodes.forEach(translateNode)
    }
  })
  obs.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: [...ATTRS] })
}
