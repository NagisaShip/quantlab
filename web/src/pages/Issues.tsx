/**
 * 问题报告：成员提交缺陷 / 建议 / 使用问题 / 数据问题，可附截图与文件（粘贴、拖入、选择），
 * 自动附带页面与环境信息；部署管理员与工作区管理员在同一页面处理：回复、改状态。
 * 报告和回复是一条时间线，像工单一样。
 */
import { Fragment, useCallback, useEffect, useState } from 'react'
import { issues, since, uploadUrl, fmtSize, type Issue, type Ref, type Upload } from '../apiCollab'
import { Callout, EmptyState, Loading, Modal, Tag } from '../components/ui'
import { RichBody, RichEditor } from '../components/RichText'
import { QuantBot } from '../components/QuantBot'
import { UserAvatar } from '../components/UserAvatar'
import { pingInbox } from '../components/Inbox'
import type { Me } from '../api/platform'
import type { PageProps } from './types'

const STATUS_TONE: Record<string, string> = { open: 'warn', replied: 'agent', resolved: 'ok', closed: '' }
const SEV_LABEL: Record<string, string> = { low: '轻微', normal: '一般', high: '严重', critical: '紧急' }
const CATS: [string, string, string][] = [
  ['bug', '缺陷', '页面报错、结果不对、按钮没反应'], ['feature', '功能建议', '希望平台增加或改进的功能'],
  ['question', '使用问题', '不知道怎么用、看不懂'], ['data', '数据问题', '行情缺失、数据对不上'], ['other', '其他', ''],
]

export default function IssuesPage({ arg, me, onNavigate, notify }: PageProps & { arg?: string; me: Me }) {
  if (arg && arg !== 'new') return <IssueDetail key={arg} id={arg} me={me} onNavigate={onNavigate} notify={notify} />
  return <IssueList me={me} onNavigate={onNavigate} notify={notify} startNew={arg === 'new'} />
}

function IssueList({ me, onNavigate, notify, startNew }: { me: Me; onNavigate: (k: string) => void; notify: PageProps['notify']; startNew: boolean }) {
  const [scope, setScope] = useState<'mine' | 'all'>('mine')
  const [status, setStatus] = useState('')
  const [data, setData] = useState<Awaited<ReturnType<typeof issues.list>> | null>(null)
  const [creating, setCreating] = useState(startNew)
  const load = useCallback(() => { issues.list(scope, status || undefined).then(setData).catch((e) => notify(e.message, true)) }, [scope, status, notify])
  useEffect(load, [load])
  useEffect(() => { if (data?.is_staff && scope === 'mine' && me.user.is_admin && !startNew) setScope('all') }, [data?.is_staff]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="issues-page">
      <div className="community-bar">
        <div className="tabs flat">
          <button aria-pressed={scope === 'mine'} onClick={() => setScope('mine')}>我提交的</button>
          {data?.is_staff && <button aria-pressed={scope === 'all'} onClick={() => setScope('all')}>待我处理{data.counts?.open ? <small> {data.counts.open}</small> : null}</button>}
        </div>
        <div className="community-tools">
          <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="状态">
            <option value="">全部状态</option>
            {Object.entries(data?.statuses ?? {}).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
          <button className="button primary" onClick={() => setCreating(true)}>＋ 报告问题</button>
        </div>
      </div>
      {data?.is_staff && scope === 'all' && <Callout>你是{me.user.is_admin ? '部署管理员，可以看到并处理全部问题报告' : '工作区管理员，可以处理本工作区成员提交的报告'}。回复后提交者会收到通知。</Callout>}
      {!data ? <Loading /> : !data.items.length ? (
        <EmptyState title={scope === 'mine' ? '你还没有提交过问题' : '没有待处理的问题'} action={<button className="button primary" onClick={() => setCreating(true)}>报告问题</button>}>
          遇到报错、数据不对或有功能建议，都可以在这里告诉管理员，支持附截图和文件。
        </EmptyState>
      ) : (
        <div className="issue-list stagger">
          {data.items.map((i) => (
            <button key={i.id} type="button" className="issue-row" onClick={() => {
              const selected = window.getSelection()?.toString().trim()
              if (!selected) onNavigate(`issues/${i.id}`)
            }}>
              <span className={`issue-sev s-${i.severity}`} title={`严重程度：${SEV_LABEL[i.severity]}`} />
              <span className="issue-main">
                <b>{i.title}</b>
                <small>{i.category_label} · {scope === 'all' ? `${i.user_name} · ` : ''}{since(i.created_at)} 提交 · {i.reply_count} 条回复{i.attachments.length ? ` · ${i.attachments.length} 个附件` : ''}</small>
              </span>
              <Tag tone={STATUS_TONE[i.status]}>{i.status_label}</Tag>
              <span className="issue-at">{since(i.updated_at)}</span>
            </button>
          ))}
        </div>
      )}
      {creating && <NewIssueModal onClose={() => setCreating(false)} notify={notify} onDone={(id) => { setCreating(false); onNavigate(`issues/${id}`) }} />}
    </div>
  )
}

function refsToIds(refs: Ref[]) { return refs.filter((r) => r.type === 'file').map((r) => r.id) }

export function NewIssueModal({ onClose, onDone, notify, preset }: { onClose: () => void; onDone: (id: string) => void; notify: PageProps['notify']; preset?: { title?: string; body?: string } }) {
  const [title, setTitle] = useState(preset?.title ?? '')
  const [body, setBody] = useState(preset?.body ?? '')
  const [refs, setRefs] = useState<Ref[]>([])
  const [cat, setCat] = useState('bug')
  const [sev, setSev] = useState('normal')
  const [withEnv, setWithEnv] = useState(true)
  const [busy, setBusy] = useState(false)
  const env = {
    page: (document.referrer && new URL(document.referrer).hash) || window.location.hash || '',
    browser: navigator.userAgent.slice(0, 200),
    screen: `${window.innerWidth}×${window.innerHeight}`,
    lang: document.documentElement.lang || navigator.language,
    theme: document.documentElement.dataset.theme || 'light',
    time: new Date().toString(),
  }
  const submit = async () => {
    setBusy(true)
    try {
      // 正文里的文件引用转成附件；其他引用（回测、策略）保留为正文里的引用标记
      const r = await issues.create({ title, body, category: cat, severity: sev, attachments: refsToIds(refs), context: withEnv ? env : {} })
      try { localStorage.removeItem('qa.draft.issue.new') } catch { /* */ }
      notify('已提交，管理员回复后会通知你')
      pingInbox()
      onDone(r.issue.id)
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  return (
    <Modal title="报告问题" onClose={onClose} width={820}
      footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={busy || title.trim().length < 2} onClick={submit}>{busy ? '提交中…' : '提交'}</button></>}>
      <div className="issue-cats">
        {CATS.map(([k, l, h]) => (
          <button key={k} type="button" className={`issue-cat${cat === k ? ' on' : ''}`} onClick={() => setCat(k)}><b>{l}</b>{h && <small>{h}</small>}</button>
        ))}
      </div>
      <div className="form-grid" style={{ marginTop: 12 }}>
        <label>标题<input value={title} maxLength={120} autoFocus onChange={(e) => setTitle(e.target.value)} placeholder="一句话描述问题，例如：回测详情页打开白屏" /></label>
        <label>严重程度<select value={sev} onChange={(e) => setSev(e.target.value)}>{Object.entries(SEV_LABEL).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label>
      </div>
      <div style={{ marginTop: 10 }}>
        <RichEditor value={body} onChange={setBody} refs={refs} onRefs={setRefs} rows={9} draftKey="issue.new" notify={notify}
          placeholder={cat === 'bug' ? '怎么复现：\n1. 打开…\n2. 点击…\n\n期望看到：\n实际看到：\n\n（截图可以直接粘贴进来）' : '详细描述…'} />
      </div>
      <label className="check" style={{ marginTop: 10 }}><input type="checkbox" checked={withEnv} onChange={(e) => setWithEnv(e.target.checked)} />
        附带环境信息（当前页面、浏览器、窗口尺寸、界面语言与主题），方便管理员复现</label>
      {withEnv && <div className="hint mono issue-env">{Object.entries(env).map(([k, v]) => `${k}: ${v}`).join('\n')}</div>}
    </Modal>
  )
}

function Attachments({ files }: { files: Upload[] }) {
  if (!files.length) return null
  return (
    <div className="issue-files">
      {files.map((f) => f.mime.startsWith('image/') ? (
        <a key={f.id} href={uploadUrl(f.id)} target="_blank" rel="noreferrer" className="issue-thumb"><img src={uploadUrl(f.id)} alt={f.filename} loading="lazy" /></a>
      ) : (
        <a key={f.id} href={uploadUrl(f.id)} target="_blank" rel="noreferrer" className="ref-card file small"><div className="ref-head"><span className="ref-glyph">⎙</span><b>{f.filename}</b><span className="ref-kind">{fmtSize(f.size)}</span></div></a>
      ))}
    </div>
  )
}

function IssueDetail({ id, me, onNavigate, notify }: { id: string; me: Me; onNavigate: (k: string) => void; notify: PageProps['notify'] }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof issues.get>> | null>(null)
  const [error, setError] = useState('')
  const [text, setText] = useState('')
  const [refs, setRefs] = useState<Ref[]>([])
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => { issues.get(id).then(setData).catch((e) => setError(e.message)) }, [id])
  useEffect(load, [load])
  if (error) return <Callout tone="fail">{error}</Callout>
  if (!data) return <Loading />
  const { issue: i, is_staff } = data
  const mine = i.user_id === me.user.id
  const reply = async () => {
    setBusy(true)
    try {
      await issues.reply(id, text, refsToIds(refs))
      setText(''); setRefs([])
      try { localStorage.removeItem(`qa.draft.issue.${id}`) } catch { /* */ }
      load(); pingInbox()
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const setStatus = async (s: Issue['status']) => {
    try { await issues.status(id, s); notify('状态已更新'); load() } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <div className="issue-detail">
      <div className="editor-toolbar">
        <button className="button small" onClick={() => onNavigate('issues')}>← 问题报告</button>
        <div className="spacer" />
        {is_staff && !mine ? (
          <select value={i.status} onChange={(e) => setStatus(e.target.value as Issue['status'])} aria-label="状态">
            {Object.entries(data.statuses).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        ) : mine && (i.status === 'closed'
          ? <button className="button small" onClick={() => setStatus('open')}>重新打开</button>
          : <button className="button small" onClick={() => setStatus('closed')}>关闭报告</button>)}
      </div>
      <article className="panel issue-card">
        <div className="panel-body">
          <div className="issue-title-row">
            <span className={`issue-sev s-${i.severity}`} />
            <h1 className="post-h1">{i.title}</h1>
            <Tag tone={STATUS_TONE[i.status]}>{i.status_label}</Tag>
          </div>
          <div className="post-meta"><UserAvatar size={26} title={i.user_name} /><span>{i.user_name}</span><span>· {i.category_label} · {SEV_LABEL[i.severity]} · {since(i.created_at)}</span></div>
          <div className="post-body"><RichBody text={i.body || '（没有描述）'} refs={i.attachments.map((f) => ({ type: 'file', id: f.id, title: f.filename, mime: f.mime, size: f.size }))} /></div>
          {Object.keys(i.context).length > 0 && (
            <details className="issue-ctx"><summary>环境信息</summary><dl className="kv">{Object.entries(i.context).map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd className="mono small">{v}</dd></Fragment>)}</dl></details>
          )}
        </div>
      </article>
      <div className="issue-thread stagger">
        {(i.replies ?? []).map((r) => (
          <div key={r.id} className={`issue-reply${r.is_staff ? ' staff' : ''}`}>
            <span className="issue-reply-av">{r.is_staff ? <QuantBot size={22} variant="simple" /> : r.user_name.slice(0, 1)}</span>
            <div className="issue-reply-body">
              <div className="chat-meta"><b>{r.user_name}</b>{r.is_staff && <span className="src-badge agent">管理员</span>}<time>{since(r.created_at)}</time></div>
              <RichBody text={r.body} compact />
              <Attachments files={r.attachments} />
            </div>
          </div>
        ))}
        {!(i.replies ?? []).length && <div className="hint">还没有回复。{mine ? '管理员回复后会通知你。' : ''}</div>}
      </div>
      {(mine || is_staff) && i.status !== 'closed' && (
        <div className="issue-composer">
          <RichEditor value={text} onChange={setText} refs={refs} onRefs={setRefs} rows={4} draftKey={`issue.${id}`} notify={notify} onSubmit={reply}
            placeholder={is_staff && !mine ? '回复提交者（Ctrl+Enter 发送）' : '补充信息（Ctrl+Enter 发送）'}
            footer={<button className="button small primary" disabled={busy || (!text.trim() && !refs.length)} onClick={reply}>{busy ? '发送中…' : is_staff && !mine ? '回复' : '补充'}</button>} />
          {is_staff && !mine && <div className="button-row" style={{ marginTop: 8 }}>
            <button className="button small" onClick={async () => { await reply(); await setStatus('resolved') }} disabled={busy || !text.trim()}>回复并标为已解决</button>
          </div>}
        </div>
      )}
    </div>
  )
}
