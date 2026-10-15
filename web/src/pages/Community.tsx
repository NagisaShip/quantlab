import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ago,
  fmtNum,
  fmtPct,
  tone,
  v3,
  type BacktestRow,
  type CommentRow,
  type Post,
  type PostSummary,
  type StrategyFile,
} from '../api2'
import { CodeView } from '../components/CodeEditor'
import { COLORS, LineChart, Sparkline } from '../components/charts2'
import { Markdown } from '../components/markdown'
import { Callout, EmptyState, Heading, Loading, Modal, Panel, Tag } from '../components/ui'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

const TABS: { key: string; label: string; q: Record<string, unknown> }[] = [
  { key: 'all', label: '全部', q: {} },
  { key: 'strategy', label: '策略分享', q: { type: 'strategy' } },
  { key: 'article', label: '文章', q: { type: 'article' } },
  { key: 'question', label: '问答', q: { type: 'question' } },
  { key: 'featured', label: '精华', q: { featured: true } },
  { key: 'mine', label: '我的', q: { mine: true } },
  { key: 'fav', label: '收藏', q: { favorited: true } },
]
const SORTS = [
  { value: 'recommend', label: '推荐' },
  { value: 'latest', label: '最新发帖' },
  { value: 'reply', label: '最新回复' },
  { value: 'hot', label: '最热' },
]

export default function CommunityPage({ onNavigate, notify }: PageProps) {
  const [tab, setTab] = useState('all')
  const [sort, setSort] = useState('recommend')
  const [tag, setTag] = useState('')
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState<{ items: PostSummary[]; total: number; viewer: string } | null>(null)
  const [tags, setTags] = useState<{ tag: string; count: number }[]>([])
  const [stats, setStats] = useState<{ ranking: { author: string; posts: number; likes: number; clones: number; score: number }[]; counts: Record<string, number> } | null>(null)
  const [compose, setCompose] = useState<null | 'article' | 'question' | 'strategy'>(null)
  const [profile, setProfile] = useState<{ name: string; bio: string } | null>(null)
  const [editProfile, setEditProfile] = useState(false)

  const load = useCallback(() => {
    const t = TABS.find((x) => x.key === tab)?.q ?? {}
    v3.posts({ ...t, tag: tag || undefined, q: query || undefined, sort, page, size: 15 }).then(setData).catch((e) => notify(e.message, true))
  }, [tab, tag, query, sort, page, notify])
  useEffect(load, [load])
  useEffect(() => {
    v3.tags().then((r) => setTags(r.hot)).catch(() => undefined)
    v3.stats().then(setStats).catch(() => undefined)
    v3.profile().then((r) => setProfile(r.profile)).catch(() => undefined)
  }, [])

  const pages = data ? Math.max(1, Math.ceil(data.total / 15)) : 1

  return (
    <>
      <Heading title="策略广场" actions={
        <div className="button-row">
          <button className="button" onClick={() => setCompose('question')}>提问题</button>
          <button className="button" onClick={() => setCompose('article')}>写文章</button>
          <button className="button primary" onClick={() => setCompose('strategy')}><span>＋</span>分享策略</button>
        </div>
      } />

      <div className="community-layout">
        <div>
          <div className="community-bar">
            <div className="tabs flat">
              {TABS.map((t) => <button key={t.key} aria-pressed={tab === t.key} onClick={() => { setTab(t.key); setPage(1) }}>{t.label}</button>)}
            </div>
            <div className="community-tools">
              <select value={sort} onChange={(e) => { setSort(e.target.value); setPage(1) }} aria-label="排序">{SORTS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}</select>
              <input className="search" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { setQuery(q); setPage(1) } }} placeholder="搜索标题、正文、标签（回车）" />
            </div>
          </div>
          {tag && <div className="filter-chip">标签：<b>{tag}</b> <button className="link" onClick={() => setTag('')}>清除</button></div>}

          {!data ? <Loading /> : !data.items.length ? (
            <EmptyState glyph="✎" title="这里还没有内容">{tab === 'fav' ? '收藏的帖子会出现在这里。' : '来发第一篇吧。'}</EmptyState>
          ) : (
            <div className="post-list">
              {data.items.map((p) => <PostCard key={p.id} post={p} onOpen={() => onNavigate(`post/${p.id}`)} onTag={(t) => { setTag(t); setPage(1) }} />)}
            </div>
          )}
          {pages > 1 && (
            <div className="pager">
              <button className="button small" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
              <span>{page} / {pages} · 共 {data?.total} 条</span>
              <button className="button small" disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button>
            </div>
          )}
        </div>

        <aside className="community-side">
          <Panel title="我">
            {profile && !editProfile && (
              <div className="me-card">
                <span className="avatar big">{profile.name.slice(0, 1)}</span>
                <div><b>{profile.name}</b><small>{profile.bio || '还没有简介'}</small></div>
                <button className="button small" onClick={() => setEditProfile(true)}>编辑</button>
              </div>
            )}
            {profile && editProfile && <ProfileForm profile={profile} onSaved={(p) => { setProfile(p); setEditProfile(false); load() }} notify={notify} />}
            {stats && (
              <div className="stat-row">
                <div><b>{stats.counts.posts}</b><span>帖子</span></div>
                <div><b>{stats.counts.strategies}</b><span>策略</span></div>
                <div><b>{stats.counts.comments}</b><span>评论</span></div>
                <div><b>{stats.counts.clones}</b><span>克隆</span></div>
              </div>
            )}
          </Panel>
          <Panel title="热门标签">
            <div className="chip-row">
              {tags.map((t) => <button key={t.tag} className={`chip${tag === t.tag ? ' on' : ''}`} onClick={() => { setTag(tag === t.tag ? '' : t.tag); setPage(1) }}>{t.tag} <small>{t.count}</small></button>)}
              {!tags.length && <span className="hint">暂无</span>}
            </div>
          </Panel>
          <Panel title="贡献榜" subtitle="发帖 ×5 + 获赞 ×2 + 被克隆 ×3 + 评论 ×1">
            {stats?.ranking.length ? (
              <ol className="ranking">
                {stats.ranking.map((r) => <li key={r.author}><span>{r.author}</span><b>{r.score}</b></li>)}
              </ol>
            ) : <div className="hint">还没有人上榜</div>}
          </Panel>
        </aside>
      </div>

      {compose && <ComposeModal initialType={compose} onClose={() => setCompose(null)} onDone={(id) => onNavigate(`post/${id}`)} notify={notify} />}
    </>
  )
}

function PostCard({ post, onOpen, onTag }: { post: PostSummary; onOpen: () => void; onTag: (t: string) => void }) {
  const m = post.backtest?.metrics
  return (
    <article className="post-card" onClick={onOpen}>
      <div className="post-main">
        <div className="post-title">
          {post.pinned && <span className="badge pin">置顶</span>}
          {post.featured && <span className="badge feat">精华</span>}
          <span className={`badge type-${post.type}`}>{post.type_label}</span>
          <h3>{post.title}</h3>
        </div>
        <p className="post-excerpt">{post.excerpt}</p>
        {m && (
          <div className="post-metrics">
            <span>收益 <b className={tone(m.total_return)}>{fmtPct(m.total_return, 1, true)}</b></span>
            <span>超额 <b className={tone(m.excess_return)}>{fmtPct(m.excess_return, 1, true)}</b></span>
            <span>回撤 <b>{fmtPct(m.max_drawdown, 1)}</b></span>
            <span>夏普 <b>{fmtNum(m.sharpe)}</b></span>
            <span>交易 <b>{m.trades ?? '—'}</b></span>
            {post.backtest?.data_sources?.some((s) => s.includes('合成')) && <Tag tone="warn">合成数据</Tag>}
          </div>
        )}
        <div className="post-meta">
          <span className="avatar sm">{post.author.slice(0, 1)}</span>
          <span>{post.author}</span>
          <span>· {ago(post.created_at)}</span>
          {post.strategy && <span>· {post.strategy.securities?.join(', ')} {post.strategy.timeframe}</span>}
          <span className="post-tags" onClick={(e) => e.stopPropagation()}>
            {post.tags.map((t) => <button key={t} className="chip small" onClick={() => onTag(t)}>{t}</button>)}
          </span>
        </div>
      </div>
      <div className="post-side">
        {post.backtest?.equity && post.backtest.equity.length > 1 && <Sparkline values={post.backtest.equity} width={140} height={44} />}
        <div className="post-counts">
          <span title="浏览">👁 {post.views}</span>
          <span title="点赞" className={post.liked ? 'on' : ''}>▲ {post.like_count}</span>
          <span title="评论">💬 {post.comment_count}</span>
          {post.strategy && <span title="克隆">⎘ {post.clones}</span>}
        </div>
      </div>
    </article>
  )
}

function ProfileForm({ profile, onSaved, notify }: { profile: { name: string; bio: string }; onSaved: (p: { name: string; bio: string }) => void; notify: PageProps['notify'] }) {
  const [name, setName] = useState(profile.name)
  const [bio, setBio] = useState(profile.bio)
  return (
    <div>
      <label>作者名<input value={name} maxLength={30} onChange={(e) => setName(e.target.value)} /></label>
      <label style={{ marginTop: 8 }}>简介<input value={bio} maxLength={200} onChange={(e) => setBio(e.target.value)} /></label>
      <button className="button small primary" style={{ marginTop: 10 }} onClick={async () => {
        try { onSaved((await v3.saveProfile({ name, bio })).profile) } catch (e) { notify((e as Error).message, true) }
      }}>保存</button>
    </div>
  )
}

/* ---------------- 发帖 ---------------- */
export function ComposeModal({ initialType, onClose, onDone, notify, presetBacktest }: {
  initialType: 'article' | 'question' | 'strategy'
  onClose: () => void
  onDone: (id: string) => void
  notify: PageProps['notify']
  presetBacktest?: string
}) {
  const [type, setType] = useState(initialType)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [tags, setTags] = useState<string[]>([])
  const [suggested, setSuggested] = useState<string[]>([])
  const [strategies, setStrategies] = useState<StrategyFile[]>([])
  const [backtests, setBacktests] = useState<BacktestRow[]>([])
  const [slug, setSlug] = useState('')
  const [btId, setBtId] = useState(presetBacktest ?? '')
  const [review, setReview] = useState(true)
  const [preview, setPreview] = useState(false)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    v3.tags().then((r) => setSuggested(r.suggested)).catch(() => undefined)
    v3.strategies().then((r) => setStrategies(r.strategies)).catch(() => undefined)
    v3.backtests(undefined, 100).then((r) => setBacktests(r.backtests)).catch(() => undefined)
  }, [])
  const btOptions = useMemo(() => (slug ? backtests.filter((b) => b.slug === slug) : backtests), [backtests, slug])

  const submit = async () => {
    setBusy(true)
    try {
      const r = await v3.createPost({
        type,
        title,
        body,
        tags,
        strategy_slug: type === 'strategy' && slug ? slug : null,
        backtest_id: type === 'strategy' && btId ? btId : null,
        ai_review: type === 'strategy' && review,
      })
      notify('已发布')
      onDone(r.post.id)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="发帖" onClose={onClose} width={900}
      footer={<><button className="button" onClick={() => setPreview(!preview)}>{preview ? '继续编辑' : '预览'}</button><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={busy || title.trim().length < 2 || (type === 'strategy' && !slug && !btId)} onClick={submit}>{busy ? '发布中…' : '发布'}</button></>}>
      <div className="segments" style={{ display: 'inline-flex', marginBottom: 12 }}>
        {(['strategy', 'article', 'question'] as const).map((t) => (
          <button key={t} aria-pressed={type === t} onClick={() => setType(t)}>{t === 'strategy' ? '策略分享' : t === 'article' ? '文章' : '问答'}</button>
        ))}
      </div>
      {preview ? (
        <div className="md-preview"><h2>{title || '（无标题）'}</h2><Markdown text={body} /></div>
      ) : (
        <>
          <label>标题<input value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} placeholder={type === 'question' ? '一句话说清你的问题' : '标题'} /></label>
          {type === 'strategy' && (
            <div className="form-grid" style={{ marginTop: 10 }}>
              <label>附加策略（冻结当前版本代码）
                <select value={slug} onChange={(e) => { setSlug(e.target.value); setBtId('') }}>
                  <option value="">— 选择策略 —</option>
                  {strategies.map((s) => <option key={s.slug} value={s.slug}>{s.name}（{s.version}）</option>)}
                </select>
              </label>
              <label>附加回测（推荐）
                <select value={btId} onChange={(e) => setBtId(e.target.value)}>
                  <option value="">— 不附回测 —</option>
                  {btOptions.map((b) => <option key={b.id} value={b.id}>{b.strategy_name} · {fmtPct(b.metrics.total_return, 1, true)} · {ago(b.created_at)}</option>)}
                </select>
              </label>
            </div>
          )}
          <div className="chip-row" style={{ marginTop: 10 }}>
            {suggested.map((t) => <button key={t} className={`chip${tags.includes(t) ? ' on' : ''}`} onClick={() => setTags(tags.includes(t) ? tags.filter((x) => x !== t) : [...tags, t].slice(0, 8))}>{t}</button>)}
          </div>
          <label style={{ marginTop: 10 }}>正文（支持 Markdown：标题、列表、表格、代码块）
            <textarea rows={12} value={body} onChange={(e) => setBody(e.target.value)} placeholder={type === 'strategy' ? '思路、参数、回测条件、局限……' : ''} />
          </label>
          {type === 'strategy' && <label className="check"><input type="checkbox" checked={review} onChange={(e) => setReview(e.target.checked)} /> 发布后请 AI 审稿人公开挑刺</label>}
        </>
      )}
    </Modal>
  )
}

/* ---------------- 帖子详情 ---------------- */
export function PostPage({ id, onNavigate, notify }: PageProps & { id: string }) {
  const [data, setData] = useState<{ post: Post; comments: CommentRow[]; related: PostSummary[]; viewer: string } | null>(null)
  const [error, setError] = useState('')
  const [text, setText] = useState('')
  const [replyTo, setReplyTo] = useState<CommentRow | null>(null)
  const [reviewing, setReviewing] = useState(false)
  const [editing, setEditing] = useState(false)

  const load = useCallback(() => {
    v3.post(id).then(setData).catch((e) => setError(e.message))
  }, [id])
  useEffect(load, [load])

  if (error) return <Callout tone="fail">{error}</Callout>
  if (!data) return <Loading />
  const { post, comments, viewer } = data
  const mine = post.author === viewer
  const bt = post.backtest
  const roots = comments.filter((c) => !c.parent_id)
  const children = (pid: string) => comments.filter((c) => c.parent_id === pid)

  const act = async (fn: () => Promise<unknown>, msg?: string) => {
    try {
      await fn()
      if (msg) notify(msg)
      load()
    } catch (e) {
      notify((e as Error).message, true)
    }
  }
  const send = async () => {
    if (!text.trim()) return
    await act(() => v3.comment(id, text.trim(), replyTo?.id), '已发表')
    setText('')
    setReplyTo(null)
  }
  const clone = async () => {
    try {
      const r = await v3.clonePost(id)
      notify(`已克隆到我的策略：${r.strategy.name}`)
      onNavigate(`editor/${r.strategy.slug}`)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }
  const review = async () => {
    setReviewing(true)
    await act(() => v3.aiReviewPost(id), 'AI 审稿意见已发布')
    setReviewing(false)
  }

  const renderComment = (c: CommentRow, depth = 0) => (
    <div key={c.id} className={`comment${c.is_ai ? ' ai' : ''}${post.accepted_comment === c.id ? ' accepted' : ''}`} style={{ marginLeft: depth ? 34 : 0 }}>
      <div className="comment-head">
        <span className={`avatar sm${c.is_ai ? ' ai' : ''}`}>{c.is_ai ? '✦' : c.author.slice(0, 1)}</span>
        <b>{c.author}</b>
        {c.is_ai && <Tag tone={c.meta?.mode === 'llm' ? 'agent' : 'warn'}>{c.meta?.mode === 'llm' ? c.meta.model : '规则审稿'}</Tag>}
        {post.accepted_comment === c.id && <Tag tone="ok">已采纳</Tag>}
        <span className="muted">{ago(c.created_at)}</span>
      </div>
      <div className="comment-body"><Markdown text={c.body} compact /></div>
      <div className="comment-actions">
        <button className={`link${c.liked ? ' on' : ''}`} onClick={() => act(() => v3.likeComment(c.id))}>▲ {c.like_count}</button>
        <button className="link" onClick={() => { setReplyTo(c); document.getElementById('comment-box')?.focus() }}>回复</button>
        {post.type === 'question' && mine && !c.is_ai && <button className="link" onClick={() => act(() => v3.accept(id, c.id))}>{post.accepted_comment === c.id ? '取消采纳' : '采纳'}</button>}
        {(c.author === viewer || c.is_ai) && <button className="link danger" onClick={() => act(() => v3.deleteComment(c.id))}>删除</button>}
      </div>
      {children(c.id).map((x) => renderComment(x, depth + 1))}
    </div>
  )

  return (
    <div className="post-page">
      <div className="editor-toolbar">
        <button className="button small" onClick={() => onNavigate('square')}>← 策略广场</button>
        <div className="spacer" />
        <button className={`button small${post.liked ? ' on' : ''}`} onClick={() => act(() => v3.likePost(id))}>▲ 点赞 {post.like_count}</button>
        <button className={`button small${post.favorited ? ' on' : ''}`} onClick={() => act(() => v3.favoritePost(id))}>{post.favorited ? '★ 已收藏' : '☆ 收藏'} {post.favorite_count}</button>
        {post.strategy && <button className="button small primary" onClick={clone}>⎘ 克隆策略</button>}
        {post.strategy && <button className="button small" disabled={reviewing} onClick={review}>{reviewing ? '审稿中…' : '✦ AI 审稿'}</button>}
        <button className="button small" onClick={() => act(() => v3.featurePost(id))}>{post.featured ? '取消精华' : '设为精华'}</button>
        <button className="button small" onClick={() => act(() => v3.pinPost(id))}>{post.pinned ? '取消置顶' : '置顶'}</button>
        {mine && <button className="button small" onClick={() => setEditing(true)}>编辑</button>}
        {mine && <button className="button small danger" onClick={async () => { if (await appConfirm('删除这篇帖子？')) { await v3.deletePost(id); onNavigate('square') } }}>删除</button>}
      </div>

      <div className="community-layout">
        <div>
          <article className="panel post-article">
            <div className="panel-body" style={{ paddingTop: 24 }}>
              <div className="post-title">
                {post.pinned && <span className="badge pin">置顶</span>}
                {post.featured && <span className="badge feat">精华</span>}
                <span className={`badge type-${post.type}`}>{post.type_label}</span>
              </div>
              <h1 className="post-h1">{post.title}</h1>
              <div className="post-meta">
                <span className="avatar sm">{post.author.slice(0, 1)}</span><span>{post.author}</span>
                <span>· {ago(post.created_at)}{post.edited_at ? '（已编辑）' : ''}</span>
                <span>· 浏览 {post.views}</span>
                <span className="post-tags">{post.tags.map((t) => <span key={t} className="chip small">{t}</span>)}</span>
              </div>
              {bt && (
                <div className="post-backtest">
                  <div className="post-metrics big">
                    <span>策略收益 <b className={tone(bt.metrics.total_return)}>{fmtPct(bt.metrics.total_return, 2, true)}</b></span>
                    <span>基准 <b>{fmtPct(bt.metrics.benchmark_return, 2, true)}</b></span>
                    <span>超额 <b className={tone(bt.metrics.excess_return)}>{fmtPct(bt.metrics.excess_return, 2, true)}</b></span>
                    <span>最大回撤 <b>{fmtPct(bt.metrics.max_drawdown)}</b></span>
                    <span>夏普 <b>{fmtNum(bt.metrics.sharpe)}</b></span>
                    <span>Alpha <b>{fmtNum(bt.metrics.alpha, 3)}</b></span>
                    <span>Beta <b>{fmtNum(bt.metrics.beta, 3)}</b></span>
                    <span>交易 <b>{bt.metrics.trades ?? '—'}</b></span>
                  </div>
                  {bt.timestamps && bt.timestamps.length > 1 && (
                    <LineChart timestamps={bt.timestamps} height={240} series={[
                      { name: '策略收益', values: bt.equity.map((v) => v / bt.equity[0] - 1), color: COLORS.strategy, area: true },
                      ...(bt.benchmark?.length ? [{ name: '基准', values: bt.benchmark.map((v) => v / bt.benchmark![0] - 1), color: COLORS.benchmark, dashed: true }] : []),
                    ]} />
                  )}
                  <div className="hint">
                    {bt.securities?.join('、')} · {bt.timeframe} · {bt.data_sources?.join('、')} · 成交模型：{String((bt.manifest as Record<string, unknown>)?.fill_model ?? '')}
                    {bt.manifest && <> · 代码 <span className="mono">{String((bt.manifest as Record<string, unknown>).code_sha256 ?? '').slice(0, 12)}</span></>}
                  </div>
                  {bt.warnings?.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
                </div>
              )}
              <div className="post-body"><Markdown text={post.body} /></div>
              {post.strategy?.code && (
                <details className="post-code" open={!bt}>
                  <summary>策略代码 · {post.strategy.name} · {post.strategy.securities?.join(', ')} {post.strategy.timeframe} · <span className="mono">{post.strategy.code_sha256?.slice(0, 12)}</span></summary>
                  <CodeView code={post.strategy.code} maxHeight={520} />
                  <div className="button-row" style={{ marginTop: 10 }}><button className="button small primary" onClick={clone}>⎘ 克隆到我的策略</button></div>
                </details>
              )}
            </div>
          </article>

          <Panel title={`讨论（${comments.length}）`}>
            {roots.map((c) => renderComment(c))}
            {!comments.length && <div className="hint">还没有评论</div>}
            <div className="comment-box">
              {replyTo && <div className="hint">回复 {replyTo.author}：{replyTo.body.slice(0, 40)}… <button className="link" onClick={() => setReplyTo(null)}>取消</button></div>}
              <textarea id="comment-box" rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="说点什么（支持 Markdown，Ctrl+Enter 发送）" onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send() }} />
              <button className="button small primary" disabled={!text.trim()} onClick={send}>发表</button>
            </div>
          </Panel>
        </div>
        <aside className="community-side">
          {post.strategy && (
            <Panel title="附带策略">
              <dl className="kv">
                <dt>名称</dt><dd>{post.strategy.name}</dd>
                <dt>标的</dt><dd>{post.strategy.securities?.join(', ')}</dd>
                <dt>周期</dt><dd>{post.strategy.timeframe}</dd>
                <dt>版本</dt><dd className="mono">{post.strategy.version}</dd>
                <dt>参数</dt><dd className="mono small">{Object.entries(post.strategy.params ?? {}).map(([k, v]) => `${k}=${v}`).join(' ') || '—'}</dd>
                <dt>被克隆</dt><dd>{post.clones} 次</dd>
              </dl>
            </Panel>
          )}
          <Panel title="相关帖子">
            {data.related.length ? (
              <ul className="plain-list">{data.related.map((r) => <li key={r.id}><a href={`#/post/${r.id}`}>{r.title}</a><small>{r.author} · {ago(r.created_at)}</small></li>)}</ul>
            ) : <div className="hint">暂无</div>}
          </Panel>
        </aside>
      </div>

      {editing && <EditPostModal post={post} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); load() }} notify={notify} />}
    </div>
  )
}

function EditPostModal({ post, onClose, onSaved, notify }: { post: Post; onClose: () => void; onSaved: () => void; notify: PageProps['notify'] }) {
  const [title, setTitle] = useState(post.title)
  const [body, setBody] = useState(post.body)
  const [tags, setTags] = useState(post.tags.join(', '))
  return (
    <Modal title="编辑帖子" onClose={onClose} width={860}
      footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" onClick={async () => {
        try { await v3.updatePost(post.id, { title, body, tags: tags.split(/[,，]+/).map((t) => t.trim()).filter(Boolean) }); onSaved() } catch (e) { notify((e as Error).message, true) }
      }}>保存</button></>}>
      <Callout>附带的策略代码与回测快照不可修改，这是为了让读者看到的始终是发布时那一份。</Callout>
      <label>标题<input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label style={{ marginTop: 10 }}>标签（逗号分隔）<input value={tags} onChange={(e) => setTags(e.target.value)} /></label>
      <label style={{ marginTop: 10 }}>正文<textarea rows={14} value={body} onChange={(e) => setBody(e.target.value)} /></label>
    </Modal>
  )
}
