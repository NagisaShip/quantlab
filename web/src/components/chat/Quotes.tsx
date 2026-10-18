/**
 * 对话框里的「引用」：引用本对话或其他对话里的某条 AI 回答，或者引用整个对话。
 * 引用以小卡片排在输入框上方，点开向上展开看全文；发送时拼成 Markdown 引用块交给 AI
 * （整个对话只给 id 与标题，AI 用 read_conversation 自己去读，不把全文塞进上下文）。
 */
import { useEffect, useMemo, useState } from 'react'
import { lab, type LabMessage, type LabProjectBrief } from '../../apiLab'
import { Markdown } from '../markdown'
import { Modal } from '../ui'

export interface Quote {
  key: string
  /** 引用的对话 */
  pid: string
  title: string
  /** 引用某条消息时的序号；不填表示引用整个对话 */
  seq?: number
  text?: string
}

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n)}…` : s)

/** 发送时拼到用户消息前面的引用块。 */
export function quotePrefix(quotes: Quote[], currentPid: string): string {
  if (!quotes.length) return ''
  const parts = quotes.map((q) => {
    const where = q.pid === currentPid ? '本对话' : `对话「${q.title}」（id: ${q.pid}）`
    if (q.seq === undefined) {
      return `> 【引用对话】${where}：需要时用 read_conversation 读取它的内容，再结合我的问题回答。`
    }
    const body = clip(q.text || '', 4000).split('\n').map((l) => `> ${l}`).join('\n')
    return `> 【引用】${where}中第 ${q.seq} 条 AI 回答：\n${body}`
  })
  return parts.join('\n\n') + '\n\n'
}

/** 输入框上方的引用卡片：点一下向上展开全文，× 移除。 */
export function QuoteTray({ quotes, onRemove }: { quotes: Quote[]; onRemove: (key: string) => void }) {
  const [open, setOpen] = useState<string | null>(null)
  if (!quotes.length) return null
  const cur = quotes.find((q) => q.key === open)
  return (
    <div className="quote-tray">
      {cur && (
        <div className="quote-pop anim-pop" role="dialog" aria-label="引用内容">
          <div className="quote-pop-head">
            <b>{cur.seq === undefined ? `整个对话「${cur.title}」` : `「${cur.title}」第 ${cur.seq} 条`}</b>
            <button type="button" className="link-btn" onClick={() => setOpen(null)}>收起</button>
          </div>
          <div className="quote-pop-body">
            {cur.seq === undefined ? <p className="hint">AI 会用 read_conversation 读取这个对话的内容。</p> : <Markdown text={cur.text || ''} compact />}
          </div>
        </div>
      )}
      {quotes.map((q) => (
        <span key={q.key} className={`quote-chip${open === q.key ? ' on' : ''}`}>
          <button type="button" className="quote-chip-main" onClick={() => setOpen(open === q.key ? null : q.key)} title="点开查看引用的全文">
            <span aria-hidden>❝</span>
            <span className="quote-chip-text">{q.seq === undefined ? `对话「${clip(q.title, 16)}」` : clip((q.text || '').replace(/\s+/g, ' '), 32)}</span>
          </button>
          <button type="button" className="quote-chip-x" aria-label="移除引用" onClick={() => { onRemove(q.key); if (open === q.key) setOpen(null) }}>×</button>
        </span>
      ))}
    </div>
  )
}

/** 选择要引用的对话，再选「整个对话」或其中某条 AI 回答。 */
export function QuotePicker({ currentId, onPick, onClose }: { currentId: string; onPick: (q: Quote) => void; onClose: () => void }) {
  const [list, setList] = useState<LabProjectBrief[] | null>(null)
  const [q, setQ] = useState('')
  const [pid, setPid] = useState<string>(currentId)
  const [msgs, setMsgs] = useState<LabMessage[] | null>(null)
  useEffect(() => { lab.projects().then((r) => setList(r.projects)).catch(() => setList([])) }, [])
  useEffect(() => {
    setMsgs(null)
    lab.view(pid).then((r) => setMsgs(r.messages)).catch(() => setMsgs([]))
  }, [pid])
  const shown = useMemo(() => (list ?? []).filter((p) => !q || p.title.toLowerCase().includes(q.toLowerCase())), [list, q])
  const title = list?.find((p) => p.id === pid)?.title || '当前对话'
  const answers = (msgs ?? []).filter((m) => m.role === 'assistant' && m.kind === 'text' && m.content.trim())
  return (
    <Modal title="引用对话或消息" onClose={onClose} width={820}>
      <div className="quote-picker">
        <div className="quote-picker-list">
          <input className="search" value={q} autoFocus onChange={(e) => setQ(e.target.value)} placeholder="搜索对话" />
          <div className="mpick-list" style={{ maxHeight: 420 }}>
            {!list ? <div className="hint" style={{ padding: 8 }}>加载中…</div> : shown.map((p) => (
              <button key={p.id} type="button" className={`mpick-item${p.id === pid ? ' on' : ''}`} onClick={() => setPid(p.id)}>
                <span className="mpick-name"><b>{p.title}{p.id === currentId && <small>（当前）</small>}</b><small>{p.securities.length ? p.securities.join(', ') : '标的自动'} · {p.timeframe}</small></span>
              </button>
            ))}
          </div>
        </div>
        <div className="quote-picker-msgs">
          {pid !== currentId && (
            <button type="button" className="button small" onClick={() => onPick({ key: `c:${pid}`, pid, title })}>{`引用整个对话「${clip(title, 18)}」`}</button>
          )}
          <div className="hint" style={{ margin: '8px 0 6px' }}>或者引用其中一条 AI 回答：</div>
          <div className="quote-msg-list">
            {!msgs ? <div className="hint">加载中…</div> : !answers.length ? <div className="hint">这个对话还没有 AI 回答</div> : [...answers].reverse().slice(0, 40).map((m) => (
              <button key={m.seq} type="button" className="quote-msg" onClick={() => onPick({ key: `m:${pid}:${m.seq}`, pid, title, seq: m.seq, text: m.content })}>
                <small>#{m.seq}</small>
                <span>{clip(m.content.replace(/\s+/g, ' '), 160)}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </Modal>
  )
}
