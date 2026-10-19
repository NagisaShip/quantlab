/**
 * @提及：把正文里的 `@成员名` 变成蓝色、可点击跳成员主页的链接。
 *
 * 为什么写成 Markdown 链接而不是内联 HTML：正文最终由 `components/markdown.tsx` 渲染，
 * 而 Markdown 链接（`[@名字](#/profile/id)`）走的是既有的安全链接规则（只允许 `#/`、`/api/`、
 * `http(s)`），不需要放宽 HTML 白名单，也不会和「整行以标签开头 = HTML 块」的解析产生歧义。
 * 样式由 `a[href^="#/profile/"]` 命中（stylesCollab.css 的 .md-mention）。
 *
 * 代码块与行内代码里的 `@` 一律不动。
 */

export interface MentionTarget {
  /** 用户 id；为空表示「只知道名字、不能跳转」 */
  id: string
  name: string
}

const NAME_TAIL = /[\w\u3400-\u9fff\uf900-\ufaff]/ // 名字后面紧跟这些字符时，说明匹配短了
const BEFORE_AT = /[\w\u3400-\u9fff\uf900-\ufaff@]/

/** Markdown 里需要转义的字符（`[` / `]` 例外：它们会破坏链接文本的边界）。 */
function escapeMd(text: string): string {
  return text.replace(/([\\`*_{}()#+\-.!|>~])/g, '\\$1')
}

function linkifySegment(segment: string, targets: MentionTarget[]): string {
  if (!segment.includes('@')) return segment
  let out = ''
  let i = 0
  while (i < segment.length) {
    const at = segment.indexOf('@', i)
    if (at < 0) { out += segment.slice(i); break }
    out += segment.slice(i, at)
    const prev = at > 0 ? segment[at - 1] : ''
    const rest = segment.slice(at + 1)
    const hit = prev && BEFORE_AT.test(prev) ? undefined : targets.find((t) => rest.startsWith(t.name) && !NAME_TAIL.test(rest[t.name.length] ?? ''))
    if (!hit || !hit.id) { out += '@'; i = at + 1; continue }
    out += `[@${escapeMd(hit.name)}](#/profile/${encodeURIComponent(hit.id)})`
    i = at + 1 + hit.name.length
  }
  return out
}

/** 把一段正文里的 @成员 变成 Markdown 链接；没有候选或没有 @ 时原样返回。 */
export function linkifyMentions(text: string, targets: MentionTarget[]): string {
  const list = (targets || []).filter((t) => t.id && t.name && t.name.trim() && !t.name.includes(']'))
  if (!text || !list.length || !text.includes('@')) return text
  const sorted = [...list].sort((a, b) => b.name.length - a.name.length)
  let fence = false
  return text
    .split('\n')
    .map((line) => {
      if (/^\s{0,3}(?:`{3,}|~{3,})/.test(line)) { fence = !fence; return line }
      if (fence) return line
      // 行内代码里的 @ 不动：按反引号切成「代码 / 非代码」交替片段
      return line.split(/(`+[^`]*`+)/).map((seg, idx) => (idx % 2 ? seg : linkifySegment(seg, sorted))).join('')
    })
    .join('\n')
}

/** 群成员 / 互关成员 → 提及候选（名字去重，长名字优先匹配）。 */
export function mentionTargets(people: { id: string; name?: string }[]): MentionTarget[] {
  const seen = new Set<string>()
  const out: MentionTarget[] = []
  for (const p of people) {
    const name = (p.name || '').trim()
    if (!p.id || !name || seen.has(name)) continue
    seen.add(name)
    out.push({ id: p.id, name })
  }
  return out
}
