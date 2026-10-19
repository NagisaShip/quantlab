/**
 * 富文本：策略广场的帖子、群聊消息、任务、问题报告共用，和 AI 研究室对话一样的 Markdown 渲染，
 * 再加上「引用」——回测、策略、代码片段、文件 / 图片、帖子、任务。
 *
 * 正文里单独一行 `{{ref:类型:id}}` 表示在这里插入引用卡片；没在正文里出现的引用放在末尾「附件与引用」。
 * 引用的摘要（回测指标、策略版本、代码哈希）由服务端在发布时冻结，读者看到的就是当时那一份。
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { collab, fmtSize, uploadFile, uploadUrl, type Ref } from '../apiCollab'
import { Markdown } from './markdown'
import { linkifyMentions, type MentionTarget } from './mentions'
import { MentionList, filterMentions, mentionQuery, type MentionOption } from './chat/Composer'
import { QmojiPicker } from './Qmoji'
import { UPLOAD_ACCEPT } from './uploadAccept'
import { Modal } from './ui'

const TOKEN = /^\s*\{\{ref:([a-z]+):([^}\s]+)\}\}\s*$/
export const refToken = (r: Pick<Ref, 'type' | 'id'>) => `{{ref:${r.type}:${r.id}}}`
const keyOf = (r: Pick<Ref, 'type' | 'id'>) => `${r.type}:${r.id}`

const pct = (v: unknown) => (typeof v === 'number' ? `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%` : '—')
const num = (v: unknown, d = 2) => (typeof v === 'number' ? v.toFixed(d) : '—')
const tone = (v: unknown) => (typeof v === 'number' ? (v > 0 ? 'up' : v < 0 ? 'down' : '') : '')

/* ------------------------------------------------------------------ 渲染 */
export function RichBody({ text, refs = [], compact, attachmentsTitle = '附件与引用', mentions }: {
  text: string; refs?: Ref[]; compact?: boolean; attachmentsTitle?: string; mentions?: MentionTarget[]
}) {
  const byKey = useMemo(() => new Map(refs.map((r) => [keyOf(r), r])), [refs])
  const { blocks, used } = useMemo(() => {
    const out: { md?: string; ref?: string }[] = []
    const seen = new Set<string>()
    let buf: string[] = []
    let fence = false
    for (const line of (text || '').replace(/\r\n?/g, '\n').split('\n')) {
      if (/^\s*```/.test(line)) fence = !fence
      const m = !fence && TOKEN.exec(line)
      if (m) {
        if (buf.length) out.push({ md: buf.join('\n') })
        buf = []
        out.push({ ref: `${m[1]}:${m[2]}` })
        seen.add(`${m[1]}:${m[2]}`)
      } else buf.push(line)
    }
    if (buf.length) out.push({ md: buf.join('\n') })
    return { blocks: out, used: seen }
  }, [text])
  const rest = refs.filter((r) => !used.has(keyOf(r)))
  return (
    <div className={`rich${compact ? ' compact' : ''}`}>
      {blocks.map((b, i) =>
        b.md !== undefined
          ? b.md.trim()
            ? <Markdown key={i} text={mentions?.length ? linkifyMentions(b.md, mentions) : b.md} compact={compact} />
            : null
          : <Fragment key={i}>{byKey.get(b.ref!) ? <RefCard r={byKey.get(b.ref!)!} /> : <div className="ref-card missing">引用已失效或你无权查看</div>}</Fragment>,
      )}
      {rest.length > 0 && (
        <div className="rich-attach">
          {!compact && <div className="rich-attach-title">{attachmentsTitle}</div>}
          <div className="rich-attach-list">{rest.map((r) => <RefCard key={keyOf(r)} r={r} small />)}</div>
        </div>
      )}
    </div>
  )
}

const REF_LABEL: Record<string, string> = { backtest: '回测', strategy: '策略', code: '代码', file: '文件', post: '帖子', task: '任务', project: '研究对话', message: '消息', report: '调研报告', link: '页面' }
const REF_GLYPH: Record<string, string> = { backtest: '↗', strategy: '⌗', code: '{}', file: '⎙', post: '✎', task: '☐', project: '✦', message: '“', report: '▤', link: '⇱' }

export function RefCard({ r, small, onRemove }: { r: Ref; small?: boolean; onRemove?: () => void }) {
  const s = r.summary || {}
  const remove = onRemove && <button type="button" className="ref-x" aria-label="移除引用" onClick={(e) => { e.preventDefault(); e.stopPropagation(); onRemove() }}>×</button>
  if (r.type === 'file' && (r.mime || '').startsWith('image/') && !small) {
    return (
      <figure className="ref-image anim-fade">
        <a href={uploadUrl(r.id)} target="_blank" rel="noreferrer"><img src={uploadUrl(r.id)} alt={r.title || '图片'} loading="lazy" /></a>
        <figcaption>{r.title}{r.size ? ` · ${fmtSize(r.size)}` : ''}</figcaption>
        {remove}
      </figure>
    )
  }
  if (r.type === 'code' && !small) {
    return (
      <div className="ref-card code anim-fade">
        <div className="ref-head"><span className="ref-glyph">{'{}'}</span><b>{r.title || '代码片段'}</b><span className="ref-kind">{r.lang || 'python'}</span>{remove}</div>
        <pre className="code md-code" data-lang={r.lang || 'python'}>{r.code}</pre>
      </div>
    )
  }
  const href = r.type === 'backtest' ? `#/backtest/${r.id}` : r.type === 'strategy' ? `#/editor/${r.id}` : r.type === 'post' ? `#/post/${r.id}`
    : r.type === 'file' ? uploadUrl(r.id) : r.type === 'project' ? `#/lab/${r.id}` : r.type === 'task' ? '#/groups/tasks'
      : r.type === 'report' ? `#/report/${r.id}` : r.type === 'link' && /^[a-z][\w\-./:]*$/.test(r.id) ? `#/${r.id}` : undefined
  const body = (
    <>
      <div className="ref-head">
        <span className="ref-glyph" aria-hidden>{r.type === 'file' && (r.mime || '').startsWith('image/') ? '▣' : REF_GLYPH[r.type]}</span>
        <b>{r.title || r.id}</b>
        <span className="ref-kind">{REF_LABEL[r.type]}</span>
        {remove}
      </div>
      {!small && r.type === 'backtest' && (
        <div className="ref-metrics">
          <span>收益 <b className={tone(s.total_return)}>{pct(s.total_return)}</b></span>
          <span>超额 <b className={tone(s.excess_return)}>{pct(s.excess_return)}</b></span>
          <span>夏普 <b>{num(s.sharpe)}</b></span>
          <span>回撤 <b>{pct(s.max_drawdown)}</b></span>
          <span>交易 <b>{s.trades ?? '—'}</b></span>
          {s.sources && <span className="ref-src">{String(s.sources).includes('synthetic') || String(s.sources).includes('合成') ? '合成数据 · 非正式' : String(s.sources)}</span>}
        </div>
      )}
      {!small && r.type === 'strategy' && (
        <div className="ref-metrics"><span>{s.securities} {s.timeframe}</span><span>版本 <b className="mono">{s.version}</b></span><span>代码 <b className="mono">{s.code_sha256}</b></span></div>
      )}
      {r.type === 'task' && s.status && <div className="ref-metrics"><span>{s.status}</span><span>{s.assignee || '未指派'}</span></div>}
      {r.type === 'report' && s.project && !small && <div className="ref-metrics"><span>来自对话「{s.project}」</span></div>}
      {r.type === 'file' && <div className="ref-metrics"><span>{r.mime}</span>{r.size ? <span>{fmtSize(r.size)}</span> : null}</div>}
    </>
  )
  return href
    ? <a className={`ref-card ${r.type}${small ? ' small' : ''} anim-fade`} href={href} target={r.type === 'file' ? '_blank' : undefined} rel="noreferrer">{body}</a>
    : <div className={`ref-card ${r.type}${small ? ' small' : ''} anim-fade`}>{body}</div>
}

/* ------------------------------------------------------------------ 引用选择器 */
type PickType = 'backtest' | 'strategy' | 'post' | 'file' | 'task'
const PICK_TABS: { key: PickType; label: string }[] = [
  { key: 'backtest', label: '回测' }, { key: 'strategy', label: '策略' }, { key: 'post', label: '帖子' }, { key: 'file', label: '文件' }, { key: 'task', label: '任务' },
]

export function RefPicker({ initial = 'backtest', onPick, onClose }: { initial?: PickType; onPick: (r: Ref) => void; onClose: () => void }) {
  const [type, setType] = useState<PickType>(initial)
  const [q, setQ] = useState('')
  const [items, setItems] = useState<(Ref & { hint?: string; at?: number })[] | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    setItems(null)
    const t = window.setTimeout(() => {
      collab.searchRefs(type, q).then((r) => setItems(r.items)).catch((e) => setError((e as Error).message))
    }, q ? 220 : 0)
    return () => window.clearTimeout(t)
  }, [type, q])
  return (
    <Modal title="插入引用" onClose={onClose} width={620}>
      <div className="ref-pick-top">
        <div className="seg seg-sm" role="tablist">
          {PICK_TABS.map((t) => <button key={t.key} type="button" role="tab" aria-selected={type === t.key} onClick={() => setType(t.key)}>{t.label}</button>)}
        </div>
        <input className="search" autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索名称" />
      </div>
      {error && <div className="hint">{error}</div>}
      <div className="ref-pick-list stagger" key={type}>
        {!items ? <div className="hint">加载中…</div> : !items.length ? <div className="hint">没有可引用的{PICK_TABS.find((t) => t.key === type)?.label}</div> : items.map((it) => (
          <button key={it.id} type="button" className="ref-pick-item" onClick={() => onPick({ type: it.type, id: it.id, title: it.title, mime: it.mime, size: it.size })}>
            <span className="ref-glyph">{REF_GLYPH[it.type]}</span>
            <span className="ref-pick-main"><b>{it.title}</b>{it.hint && <small>{it.hint}</small>}</span>
          </button>
        ))}
      </div>
      <p className="hint" style={{ marginBottom: 0 }}>回测与策略在发布时冻结摘要（指标、版本、代码哈希），之后改动不会影响已发布的内容。</p>
    </Modal>
  )
}

function CodeSnippetModal({ onInsert, onClose }: { onInsert: (r: Ref) => void; onClose: () => void }) {
  const [title, setTitle] = useState('')
  const [lang, setLang] = useState('python')
  const [code, setCode] = useState('')
  return (
    <Modal title="插入代码片段" onClose={onClose} width={720}
      footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={!code.trim()}
        onClick={() => onInsert({ type: 'code', id: `c${Date.now().toString(36)}`, title: title || '代码片段', code, lang })}>插入</button></>}>
      <div className="field-row">
        <label>标题<input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="例如：止损逻辑" maxLength={120} /></label>
        <label>语言<select value={lang} onChange={(e) => setLang(e.target.value)}>{['python', 'json', 'sql', 'text'].map((l) => <option key={l}>{l}</option>)}</select></label>
      </div>
      <label style={{ marginTop: 10 }}>代码<textarea className="mono" rows={14} value={code} onChange={(e) => setCode(e.target.value)} spellCheck={false} /></label>
    </Modal>
  )
}

/* ------------------------------------------------------------------ 编辑器 */
type ViewMode = 'edit' | 'split' | 'preview'

export interface RichEditorProps {
  value: string
  onChange: (v: string) => void
  refs: Ref[]
  onRefs: (r: Ref[]) => void
  placeholder?: string
  rows?: number
  /** 紧凑模式：群聊输入框，无分屏、工具栏收成一行图标 */
  compact?: boolean
  onSubmit?: () => void
  /** 本地草稿键：输入会自动保存在这台设备，下次打开可以恢复 */
  draftKey?: string
  mentions?: MentionOption[]
  autoFocus?: boolean
  footer?: ReactNode
  notify?: (m: string, error?: boolean) => void
  minimalToolbar?: boolean
  /** 打开时的视图：编辑 / 分屏（左写右看）/ 预览 */
  initialMode?: ViewMode
}

export function RichEditor({
  value, onChange, refs, onRefs, placeholder, rows = 12, compact, onSubmit, draftKey, mentions, autoFocus, footer, notify, minimalToolbar, initialMode = 'edit',
}: RichEditorProps) {
  const ta = useRef<HTMLTextAreaElement>(null)
  const [mode, setMode] = useState<ViewMode>(initialMode)
  const [picker, setPicker] = useState<PickType | null>(null)
  const [snippet, setSnippet] = useState(false)
  const [uploading, setUploading] = useState(0)
  const [drag, setDrag] = useState(false)
  const [draft, setDraft] = useState<{ value: string; refs: Ref[]; at: number } | null>(null)
  const [mention, setMention] = useState<{ q: string; active: number } | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const latest = useRef({ value, refs })
  latest.current = { value, refs }

  // 草稿：打开时提示恢复；输入停顿 600ms 自动保存
  const storeKey = draftKey ? `qa.draft.${draftKey}` : ''
  useEffect(() => {
    if (!storeKey) return
    try {
      const raw = localStorage.getItem(storeKey)
      if (raw) {
        const d = JSON.parse(raw)
        if (d.value && d.value !== value) setDraft(d)
      }
    } catch { /* 私密模式 */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [storeKey])
  useEffect(() => {
    if (!storeKey || draft) return
    const t = window.setTimeout(() => {
      try {
        if (value.trim()) localStorage.setItem(storeKey, JSON.stringify({ value, refs, at: Date.now() }))
        else localStorage.removeItem(storeKey)
      } catch { /* 私密模式 */ }
    }, 600)
    return () => window.clearTimeout(t)
  }, [storeKey, value, refs, draft])

  /** 在光标处插入 / 包裹文本。 */
  const edit = useCallback((fn: (sel: string) => { text: string; select?: [number, number] }, block = false) => {
    const el = ta.current
    const v = latest.current.value
    const start = el ? el.selectionStart : v.length
    const end = el ? el.selectionEnd : v.length
    const sel = v.slice(start, end)
    let { text, select } = fn(sel)
    let before = v.slice(0, start)
    if (block && before && !before.endsWith('\n')) { before += '\n'; select = select && [select[0] + 1, select[1] + 1] }
    const after = v.slice(end)
    const tail = block && after && !after.startsWith('\n') ? '\n' : ''
    onChange(before + text + tail + after)
    requestAnimationFrame(() => {
      if (!el) return
      el.focus()
      const base = before.length
      if (select) el.setSelectionRange(base + select[0], base + select[1])
      else el.setSelectionRange(base + text.length, base + text.length)
    })
  }, [onChange])

  const wrap = (l: string, r = l, hint = '') => edit((sel) => {
    const inner = sel || hint
    return { text: l + inner + r, select: [l.length, l.length + inner.length] }
  })
  const linePrefix = (p: string) => edit((sel) => {
    const lines = (sel || '').split('\n')
    const text = lines.map((x, i) => (p === '1. ' ? `${i + 1}. ` : p) + x).join('\n')
    return { text, select: sel ? undefined : [p.length, p.length] }
  }, true)

  const addRef = useCallback((r: Ref, inline = true) => {
    const cur = latest.current.refs
    if (!cur.some((x) => keyOf(x) === keyOf(r))) onRefs([...cur, r])
    if (inline) edit(() => ({ text: refToken(r) + '\n' }), true)
  }, [edit, onRefs])

  const upload = useCallback(async (files: File[]) => {
    for (const f of files.slice(0, 6)) {
      setUploading((n) => n + 1)
      try {
        const up = await uploadFile(f)
        addRef({ type: 'file', id: up.id, title: up.filename, mime: up.mime, size: up.size }, true)
      } catch (e) {
        notify?.((e as Error).message, true)
      } finally {
        setUploading((n) => n - 1)
      }
    }
  }, [addRef, notify])

  const removeRef = (r: Ref) => {
    onRefs(refs.filter((x) => keyOf(x) !== keyOf(r)))
    onChange(value.split('\n').filter((l) => l.trim() !== refToken(r)).join('\n'))
  }

  const mentionList = mention && mentions ? filterMentions(mentions, mention.q) : []
  const pickMention = (o: MentionOption) => {
    const el = ta.current
    const pos = el ? el.selectionStart : value.length
    const head = value.slice(0, pos)
    const mq = mentionQuery(head)
    if (!mq) return
    const rest = head.slice(0, mq[0]) + value.slice(pos)
    if (o.name === 'QuantLab Bot' && rest.includes('@QuantLab Bot')) {
      onChange(rest); setMention(null); el?.focus(); return
    }
    onChange(head.slice(0, mq[0]) + `@${o.name} ` + value.slice(pos))
    setMention(null)
    requestAnimationFrame(() => { el?.focus(); const p = mq[0] + o.name.length + 2; el?.setSelectionRange(p, p) })
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (mention && mentionList.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const d = e.key === 'ArrowDown' ? 1 : -1
        setMention({ ...mention, active: (mention.active + d + mentionList.length) % mentionList.length })
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pickMention(mentionList[mention.active]); return }
      if (e.key === 'Escape') { setMention(null); return }
    }
    // @提及整体删除：光标紧贴在 `@名字`（含后面的一个空格）两侧时，一次退格 / 删除键整段去掉，
    // 不用逐字删；两个方向的删除都支持。
    if ((e.key === 'Backspace' || e.key === 'Delete') && !e.altKey && !e.ctrlKey && !e.metaKey && mentions?.length) {
      const el = ta.current
      if (el && el.selectionStart === el.selectionEnd) {
        const pos = el.selectionStart
        const names = [...mentions].sort((a, b) => b.name.length - a.name.length)
        const hit = e.key === 'Backspace'
          ? (() => {
            const head = value.slice(0, pos)
            const tail = head.endsWith(' ') ? head.slice(0, -1) : head
            const m = names.find((o) => tail.endsWith(`@${o.name}`))
            return m ? { start: tail.length - m.name.length - 1, end: pos } : null
          })()
          : (() => {
            const rest = value.slice(pos)
            const m = names.find((o) => rest.startsWith(`@${o.name}`))
            if (!m) return null
            const cut = rest.startsWith(`@${m.name} `) ? m.name.length + 2 : m.name.length + 1
            return { start: pos, end: pos + cut }
          })()
        if (hit) {
          e.preventDefault()
          onChange(value.slice(0, hit.start) + value.slice(hit.end))
          requestAnimationFrame(() => { el.focus(); el.setSelectionRange(hit.start, hit.start) })
          return
        }
      }
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b') { e.preventDefault(); wrap('**', '**', '粗体') }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'i') { e.preventDefault(); wrap('*', '*', '斜体') }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); wrap('[', '](https://)', '链接文字') }
    else if (e.key === 'Enter' && onSubmit && (compact ? !e.shiftKey && !e.nativeEvent.isComposing : e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      onSubmit()
    }
  }
  const onInput = (v: string, pos: number) => {
    onChange(v)
    if (mentions?.length) {
      const mq = mentionQuery(v.slice(0, pos))
      setMention(mq ? { q: mq[1], active: 0 } : null)
    }
  }

  const tools: { label: string; title: string; run: () => void; hideCompact?: boolean }[] = [
    { label: 'B', title: '粗体（Ctrl+B）', run: () => wrap('**', '**', '粗体') },
    { label: 'I', title: '斜体（Ctrl+I）', run: () => wrap('*', '*', '斜体') },
    { label: 'H', title: '小标题', run: () => linePrefix('## '), hideCompact: true },
    { label: '•', title: '列表', run: () => linePrefix('- ') },
    { label: '1.', title: '编号列表', run: () => linePrefix('1. '), hideCompact: true },
    { label: '❝', title: '引用块', run: () => linePrefix('> ') },
    { label: '</>', title: '代码块', run: () => edit((sel) => ({ text: '```python\n' + (sel || '') + '\n```', select: [10, 10 + (sel || '').length] }), true) },
    { label: '⊞', title: '表格', run: () => edit(() => ({ text: '| 指标 | 数值 |\n| --- | --- |\n| 夏普 | |\n' }), true), hideCompact: true },
    { label: '🔗', title: '链接（Ctrl+K）', run: () => wrap('[', '](https://)', '链接文字'), hideCompact: true },
  ]

  const preview = <div className="rich-preview">{value.trim() || refs.length ? <RichBody text={value} refs={refs} /> : <span className="hint">预览会显示在这里</span>}</div>
  return (
    <div className={`rich-editor${compact ? ' compact' : ''}${drag ? ' drag' : ''}`}
      onDragOver={(e) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDrag(true) } }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => { if (e.dataTransfer.files.length) { e.preventDefault(); setDrag(false); upload([...e.dataTransfer.files]) } }}>
      {draft && (
        <div className="rich-draft anim-in">
          <span>有一份 {new Date(draft.at).toLocaleString()} 未发布的草稿</span>
          <button type="button" className="link-btn" onClick={() => { onChange(draft.value); onRefs(draft.refs || []); setDraft(null) }}>恢复</button>
          <button type="button" className="link-btn" onClick={() => { try { localStorage.removeItem(storeKey) } catch { /* */ } setDraft(null) }}>丢弃</button>
        </div>
      )}
      <div className="rich-toolbar" role="toolbar" aria-label="格式">
        {!minimalToolbar && tools.filter((t) => !(compact && t.hideCompact)).map((t) => (
          <button key={t.title} type="button" className="rt-btn" title={t.title} aria-label={t.title} onMouseDown={(e) => e.preventDefault()} onClick={t.run}>{t.label}</button>
        ))}
        {!minimalToolbar && <span className="rt-sep" />}
        <QmojiPicker small={compact} onPick={(code) => edit(() => ({ text: code }))} />
        <RefMenu onPick={(t) => (t === 'code' ? setSnippet(true) : setPicker(t))} />
        <button type="button" className="rt-btn wide" title="上传图片或文件（也可以直接粘贴 / 拖进来）" onClick={() => fileInput.current?.click()}>
          {uploading ? '上传中…' : '⎙ 上传'}
        </button>
        <input ref={fileInput} type="file" multiple style={{ position: 'fixed', left: -9999, top: 0, width: 1, height: 1, opacity: 0 }} accept={UPLOAD_ACCEPT} aria-label="上传图片或文件"
          onChange={(e) => { const f = [...(e.target.files || [])]; e.target.value = ''; if (f.length) upload(f) }} />
        {!compact && (
          <div className="rt-modes seg seg-sm" role="tablist" aria-label="视图">
            {(['edit', 'split', 'preview'] as ViewMode[]).map((m) => (
              <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => setMode(m)}>{m === 'edit' ? '编辑' : m === 'split' ? '分屏' : '预览'}</button>
            ))}
          </div>
        )}
      </div>
      <div className={`rich-panes ${mode}`}>
        {mode !== 'preview' && (
          <div className="rich-input">
            <textarea ref={ta} rows={rows} value={value} placeholder={placeholder} autoFocus={autoFocus} spellCheck={false}
              onChange={(e) => onInput(e.target.value, e.target.selectionStart)}
              onKeyDown={onKeyDown}
              onPaste={(e) => {
                const files = [...e.clipboardData.files]
                if (files.length) { e.preventDefault(); upload(files) }
              }} />
            {mention && mentions && <MentionList options={mentions} query={mention.q} active={mention.active} onHover={(i) => setMention({ ...mention, active: i })} onPick={pickMention} />}
          </div>
        )}
        {mode !== 'edit' && preview}
      </div>
      {refs.length > 0 && (
        <div className="rich-refs">
          {refs.map((r) => <RefCard key={keyOf(r)} r={r} small onRemove={() => removeRef(r)} />)}
        </div>
      )}
      <div className="rich-foot">
        <span className="hint">Markdown · 粘贴 / 拖入图片自动上传 · {compact ? 'Enter 发送，Shift+Enter 换行' : 'Ctrl+Enter 提交'}{mentions?.length ? ' · @ 提及成员' : ''}</span>
        <span className="rich-count">{value.length.toLocaleString()} 字</span>
        {footer}
      </div>
      {picker && <RefPicker initial={picker} onClose={() => setPicker(null)} onPick={(r) => { setPicker(null); addRef(r) }} />}
      {snippet && <CodeSnippetModal onClose={() => setSnippet(false)} onInsert={(r) => { setSnippet(false); addRef(r) }} />}
    </div>
  )
}

/** 「＋ 引用」下拉：回测 / 策略 / 代码片段 / 帖子 / 文件 / 任务。 */
function RefMenu({ onPick }: { onPick: (t: PickType | 'code') => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', esc)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', esc) }
  }, [open])
  const items: { k: PickType | 'code'; label: string; hint: string }[] = [
    { k: 'backtest', label: '回测结果', hint: '冻结收益、夏普、回撤、交易数' },
    { k: 'strategy', label: '策略', hint: '版本与代码哈希' },
    { k: 'code', label: '代码片段', hint: '贴一段代码' },
    { k: 'file', label: '已上传的文件', hint: '图片、PDF、CSV…' },
    { k: 'post', label: '帖子', hint: '策略广场里的其他帖子' },
    { k: 'task', label: '任务', hint: '群组任务' },
  ]
  return (
    <div className="cmp-pop-anchor" ref={ref}>
      <button type="button" className="rt-btn wide" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>＋ 引用</button>
      {open && (
        <div className="menu-list rt-menu" role="menu">
          {items.map((i) => (
            <button key={i.k} type="button" role="menuitem" onClick={() => { setOpen(false); onPick(i.k) }}>
              <span className="ref-glyph">{REF_GLYPH[i.k]}</span><span><b>{i.label}</b><small>{i.hint}</small></span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** 来源徽标：成员 / Agent 代发 / 平台。 */
export function SourceBadge({ source }: { source?: { kind: string; label?: string; on_behalf_of?: string } | null }) {
  if (!source || source.kind === 'human') return <span className="src-badge human" title="成员手写">成员</span>
  if (source.kind === 'agent') return <span className="src-badge agent" title={source.on_behalf_of ? `由 ${source.label || 'QuantBot'} 代 ${source.on_behalf_of} 发布` : 'Agent 发布'}>✦ Agent{source.on_behalf_of ? ` · 代 ${source.on_behalf_of}` : ''}</span>
  return <span className="src-badge system">平台</span>
}
