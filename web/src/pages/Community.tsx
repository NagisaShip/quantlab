import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ago,
  fmtNum,
  fmtPct,
  tone,
  v3,
  type BacktestRow,
  type CommentRow,
  type Post,
  type PostReactionResult,
  type PostReport,
  type PostReportPage,
  type PostSummary,
  type StrategyFile,
} from '../api/research'
import { CodeView } from '../components/CodeEditor'
import { COLORS, LineChart, Sparkline } from '../components/charts2'
import { Markdown } from '../components/markdown'
import { Callout, EmptyState, Heading, Loading, Modal, Panel, Tag } from '../components/ui'
import { PresenceBar, ShareToButton, usePresence } from '../components/Social'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'
import { RichBody, RichEditor, SourceBadge } from '../components/RichText'
import { collab, inbox, uploadThumbUrl, type QmojiReaction, type Ref } from '../apiCollab'
import { AvatarColorPicker, UserAvatar } from '../components/UserAvatar'
import { QmojiPicker } from '../components/Qmoji'
import { QmojiReactionBar } from '../components/QmojiReactionBar'
import { lang } from '../i18n'
import { Icon } from '../components/icons'
import { SharePublicButton } from '../components/PublicShare'

interface CommunityProfile { name: string; username?: string; bio: string; avatar_color: string; dm_policy: 'reply_only' | 'open' | 'none'; dm_policy_label?: string; public_profile?: boolean }
const SEED_POST_EN: Record<string, { title: string; body: string; tags: string[] }> = {
  'seed-guide': {
    title: 'Strategy Square beginner guide',
    tags: ['Getting started'],
    body: `Welcome to Strategy Square. Every strategy share includes a **frozen code version and backtest checklist**, so the equity curve you see and the code you clone come from the same run.

## How to use it
1. Write a strategy in Strategy Research or let AI generate one, then run a full backtest.
2. On the backtest detail page, click **Share to Square**, choose a post type, and describe the idea and limitations.
3. Readers can clone it into their own strategy library in one click, then change parameters or the date range and retest.
4. You can ask the **AI reviewer** to challenge it: are there enough trades, are cost assumptions realistic, and are there signs of overfitting?

## Posting tips
- State the symbol, timeframe, date range, data source (real or synthetic), fees, and slippage assumptions.
- Include **out-of-sample** or paper-trading results; they are more persuasive than only the best-looking curve.
- Discuss drawdown and trade count before discussing returns.

> Everything here is research discussion, not investment advice.`,
  },
  'seed-conventions': {
    title: 'Shared conventions for backtests and paper trading (required reading)',
    tags: ['Backtest conventions', 'Getting started'],
    body: `All platform backtests and paper runs use the same conventions. Check these assumptions before reading someone else’s post:

| Item | Convention |
| --- | --- |
| Decision time | Only **closed** bars; future data is never read |
| Fill price | Next bar **open** ± (half spread + slippage) |
| Fees | Default taker 0.1%, charged on both buys and sells |
| Volume | One bar can fill at most 10% of that bar’s volume |
| Rebalance tolerance | Differences below 0.5% of equity or 10 USDT are not traded |
| Warm-up | The first N bars are used only for indicator warm-up and do not count |
| Benchmark | Buy and hold the first symbol in the universe with the same capital (no costs) |
| Excess return | (1 + strategy return) / (1 + benchmark return) − 1 |

**Synthetic data** can validate code and workflow only, not a strategy conclusion. Paper-trading 7-day / 30-day checkpoints must pass in real elapsed time.`,
  },
  'seed-overfit': {
    title: 'How not to fool yourself in parameter optimization: in-sample / out-of-sample and rank correlation',
    tags: ['Optimization', 'Overfitting', 'Research'],
    body: `The easiest person to fool in parameter optimization is yourself. By default, the platform splits the range into **in-sample / out-of-sample** segments:

- Choose parameters only on the in-sample segment; use out-of-sample only for validation.
- Check **rank correlation**: do parameters that rank well in-sample also rank well out-of-sample? A correlation near 0 suggests the “good parameters” may be noise.
- Check **the best combination’s out-of-sample rank**: first in-sample and last out-of-sample is the classic overfitting pattern.
- The more combinations you try, the more likely the “best” one is luck (multiple testing).

Recommendation: use a coarse parameter grid; prefer a region that works reasonably well over an isolated peak; only then move on to paper trading.`,
  },
  'seed-momentum-rotation': {
    title: 'Template share: multi-coin momentum rotation (trend-quality score)',
    tags: ['Rotation', 'Multi-symbol', 'Getting started'],
    body: `Score each coin using **range return × linear-regression R²**, and hold only the highest-scoring coin. A new candidate must score more than 1.2× the current holding before switching, reducing turnover cost. Stay flat when every score is negative.

This is only a starting point: **no backtest is included**. Clone it, validate it yourself on real market data, and try different scoring windows.`,
  },
}

function localizeSeedPost<T extends { id: string; title: string; tags: string[]; body?: string; excerpt?: string }>(post: T): T {
  const tr = lang === 'en' ? SEED_POST_EN[post.id] : undefined
  if (!tr) return post
  const excerpt = tr.body.replace(/[#*`>|]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 220)
  return {
    ...post,
    title: tr.title,
    tags: tr.tags,
    ...(post.body !== undefined ? { body: tr.body } : {}),
    ...(post.excerpt !== undefined ? { excerpt } : {}),
  }
}


const TABS: { key: string; label: string; q: Record<string, unknown> }[] = [
  { key: 'all', label: '全部', q: {} },
  { key: 'strategy', label: '策略分享', q: { type: 'strategy' } },
  { key: 'article', label: '文章', q: { type: 'article' } },
  { key: 'question', label: '问答', q: { type: 'question' } },
  { key: 'agent', label: 'Agent 发布', q: { source: 'agent' } },
  { key: 'following', label: '关注', q: { following: true } },
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
function localPostTime(ms: number | null | undefined) {
  if (!ms) return '—'
  return new Date(ms).toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  })
}
const ADMIN_TAB = { key: 'reports', label: '举报审核', q: {} as Record<string, unknown> }
const REPORT_REASONS: [PostReport['reason'], string][] = [
  ['spam', '垃圾广告 / 引流'],
  ['abuse', '攻击谩骂 / 骚扰'],
  ['misinformation', '虚假或误导信息'],
  ['duplicate', '重复发布 / 抄袭'],
  ['other', '其他问题'],
]

export default function CommunityPage({ onNavigate, notify, me }: PageProps) {
  const isAdmin = me?.workspace?.role === 'admin'
  const [tab, setTab] = useState('all')
  const [sort, setSort] = useState('recommend')
  const [tag, setTag] = useState('')
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [data, setData] = useState<{ items: PostSummary[]; total: number; viewer: string; viewer_id: string } | null>(null)
  const [tags, setTags] = useState<{ tag: string; count: number }[]>([])
  const [stats, setStats] = useState<{ ranking: { author: string; posts: number; likes: number; clones: number; score: number }[]; counts: Record<string, number> } | null>(null)
  const [profile, setProfile] = useState<CommunityProfile | null>(null)
  const [editProfile, setEditProfile] = useState(false)
  const [reports, setReports] = useState<PostReportPage | null>(null)
  const [reportStatus, setReportStatus] = useState<'open' | 'resolved' | 'dismissed' | 'all'>('open')
  const visibleTabs = isAdmin ? [...TABS, ADMIN_TAB] : TABS
  const reportCount = reports?.counts.open ?? 0

  const load = useCallback(() => {
    if (tab === 'reports') {
      v3.postReports(reportStatus).then(setReports).catch((e) => notify(e.message, true))
      return
    }
    const t = TABS.find((x) => x.key === tab)?.q ?? {}
    v3.posts({ ...t, tag: tag || undefined, q: query || undefined, sort, page, size: 15 }).then((r) => setData({ ...r, items: r.items.map(localizeSeedPost) })).catch((e) => notify(e.message, true))
  }, [tab, tag, query, sort, page, reportStatus, notify])
  useEffect(load, [load])
  useEffect(() => {
    v3.tags().then((r) => setTags(r.hot)).catch(() => undefined)
    v3.stats().then(setStats).catch(() => undefined)
    v3.profile().then((r) => setProfile(r.profile)).catch(() => undefined)
  }, [])
  useEffect(() => {
    if (isAdmin && tab !== 'reports') v3.postReports('open').then(setReports).catch(() => undefined)
  }, [isAdmin, tab])

  const pages = data ? Math.max(1, Math.ceil(data.total / 15)) : 1

  return (
    <>
      <Heading title="策略广场" actions={
        <div className="button-row">
          <button className="button" onClick={() => onNavigate('post/new/question')}>提问题</button>
          <button className="button" onClick={() => onNavigate('post/new/article')}>写文章</button>
          <button className="button primary" onClick={() => onNavigate('post/new/strategy')}><span>＋</span>分享策略</button>
        </div>
      } />

      <div className={`community-layout${tab === 'reports' ? ' moderation-layout' : ''}`}>
        <div>
          <div className="community-bar">
            <div className="tabs flat">
              {visibleTabs.map((t) => <button key={t.key} aria-pressed={tab === t.key} onClick={() => { setTab(t.key); setPage(1) }}>{t.key === 'reports' && reportCount > 0 ? `${t.label} ${reportCount}` : t.label}</button>)}
            </div>
            <div className="community-tools">
              {tab === 'reports' ? (
                <select value={reportStatus} onChange={(e) => setReportStatus(e.target.value as typeof reportStatus)} aria-label="举报状态">
                  <option value="open">待处理</option><option value="resolved">已处理</option><option value="dismissed">已忽略</option><option value="all">全部</option>
                </select>
              ) : <>
                <select value={sort} onChange={(e) => { setSort(e.target.value); setPage(1) }} aria-label="排序">{SORTS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}</select>
                <input className="search" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { setQuery(q); setPage(1) } }} placeholder="搜索标题、正文、标签（回车）" />
              </>}
            </div>
          </div>
          {tab === 'reports' ? (
            <ModerationPanel data={reports} status={reportStatus} notify={notify} onOpen={(id) => onNavigate(`post/${id}`)} onChanged={load} />
          ) : <>
            {tag && <div className="filter-chip">标签：<b>{tag}</b> <button className="link" onClick={() => setTag('')}>清除</button></div>}
            {!data ? <Loading /> : !data.items.length ? (
              <EmptyState glyph="✎" title="这里还没有内容">{tab === 'fav' ? '收藏的帖子会出现在这里。' : '来发第一篇吧。'}</EmptyState>
            ) : (
              <div className="post-list">
                {data.items.map((p) => <PostCard key={p.id} post={p} notify={notify} canReport={p.author_id !== data.viewer_id} onOpen={() => onNavigate(`post/${p.id}`)} onAuthor={p.author_id ? () => onNavigate(`profile/${p.author_id}`) : undefined} onTag={(t) => { setTag(t); setPage(1) }} />)}
              </div>
            )}
            {pages > 1 && (
              <div className="pager">
                <button className="button small" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</button>
                <span>{page} / {pages} · 共 {data?.total} 条</span>
                <button className="button small" disabled={page >= pages} onClick={() => setPage(page + 1)}>下一页</button>
              </div>
            )}
          </>}
        </div>

        {tab !== 'reports' && <aside className="community-side">
          <Panel title="我">
            {profile && !editProfile && (
              <div className="me-card">
                <UserAvatar color={profile.avatar_color} size={46} title={profile.name} />
                <div><b>{profile.name}</b><small>{profile.bio || '还没有简介'}</small></div>
                <button className="button small" onClick={() => onNavigate(`profile/${profile.username}`)}>我的主页</button>
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
        </aside>}
      </div>

    </>
  )
}

export function PostCard({ post, onOpen, onTag, onAuthor, notify, canReport = true }: {
  post: PostSummary; onOpen: () => void; onTag: (t: string) => void; onAuthor?: () => void; notify?: PageProps['notify']; canReport?: boolean
}) {
  const m = post.backtest?.metrics
  const published = localPostTime(post.created_at * 1000)
  const [reactions, setReactions] = useState<QmojiReaction[]>(post.reactions ?? [])
  useEffect(() => setReactions(post.reactions ?? []), [post.reactions])
  const reactQmoji = async (emoji: string) => {
    try {
      const r = await v3.qmojiPost(post.id, emoji)
      setReactions(r.reactions)
    } catch (e) {
      notify?.((e as Error).message, true)
    }
  }
  return (
    <article className="post-card" onClick={onOpen}>
      <div className="post-main">
        <div className="post-headline">
          <div className="post-title">
            {post.pinned && <span className="badge pin">置顶</span>}
            {post.featured && <span className="badge feat">精华</span>}
            <span className={`badge type-${post.type}`}>{post.type_label}</span>
            <h3>{post.title}</h3>
          </div>
          <time className="post-time" dateTime={new Date(post.created_at * 1000).toISOString()} title={`发布于 ${published}`}>
            <Icon name="clock" size={13} /><span>{published}</span>
          </time>
        </div>
        <p className="post-excerpt">{post.excerpt}</p>
        {(() => {
          const images = (post.refs ?? []).filter((r) => r.type === 'file' && (r.mime || '').startsWith('image/')).slice(0, 4)
          if (!images.length) return null
          return <div className={`post-images n-${images.length}`}>{images.map((r) => <img key={r.id} src={uploadThumbUrl(r.id)} alt={r.title || '帖子图片'} loading="lazy" />)}</div>
        })()}
        {(post.refs?.length ?? 0) > 0 && <div className="post-refs-hint">引用了 {post.refs!.map((r) => r.title || r.type).slice(0, 3).join('、')}{post.refs!.length > 3 ? ` 等 ${post.refs!.length} 项` : ''}</div>}
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
          <button type="button" className="post-author-link" onClick={(e) => { e.stopPropagation(); onAuthor?.() }}>
            <UserAvatar color={post.author_color} size={26} title={post.author} />
            <span>{post.author}</span>
          </button>
          <SourceBadge source={post.source} />
          <span>· {ago(post.created_at)}</span>
          {post.strategy && <span>· {post.strategy.securities?.join(', ')} {post.strategy.timeframe}</span>}
          <span className="post-tags" onClick={(e) => e.stopPropagation()}>
            {post.tags.map((t) => <button key={t} className="chip small" onClick={() => onTag(t)}>{t}</button>)}
          </span>
        </div>
        <QmojiReactionBar compact reactions={reactions} onToggle={reactQmoji} />
      </div>
      <div className="post-side">
        {post.backtest?.equity && post.backtest.equity.length > 1 && <Sparkline values={post.backtest.equity} width={140} height={44} />}
        <PostReactionBar post={post} notify={notify} compact />
        <div className="post-counts">
          <span title="浏览">浏览 {post.views}</span>
          <span title="评论">评论 {post.comment_count}</span>
          {post.strategy && <span title="克隆">克隆 {post.clones}</span>}
          {canReport && post.author_id !== 'system' && <PostReportButton postId={post.id} title={post.title} notify={notify} compact />}
        </div>
      </div>
    </article>
  )
}

function PostReactionBar({ post, notify, compact, onChanged }: {
  post: PostSummary; notify?: PageProps['notify']; compact?: boolean; onChanged?: () => void
}) {
  const [state, setState] = useState<PostReactionResult>({
    liked: post.liked, disliked: post.disliked,
    like_count: post.like_count, dislike_count: post.dislike_count ?? 0,
  })
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    setState({ liked: post.liked, disliked: post.disliked, like_count: post.like_count, dislike_count: post.dislike_count ?? 0 })
  }, [post.liked, post.disliked, post.like_count, post.dislike_count])
  const react = async (target: 'like' | 'dislike', event?: { stopPropagation: () => void }) => {
    event?.stopPropagation()
    if (busy) return
    setBusy(true)
    try {
      const r = target === 'like' ? await v3.likePost(post.id) : await v3.dislikePost(post.id)
      setState({ liked: r.liked, disliked: r.disliked, like_count: r.like_count, dislike_count: r.dislike_count })
      onChanged?.()
    } catch (e) {
      notify?.((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className={`post-reactions${compact ? ' compact' : ''}`} onClick={(e) => e.stopPropagation()}>
      <button type="button" disabled={busy} className={`post-react${state.liked ? ' on like' : ''}`} title="点赞" onClick={(e) => react('like', e)}>
        <Icon name="up" size={14} /><span>{state.like_count}</span>
      </button>
      <button type="button" disabled={busy} className={`post-react${state.disliked ? ' on dislike' : ''}`} title="点踩" onClick={(e) => react('dislike', e)}>
        <Icon name="down" size={14} /><span>{state.dislike_count}</span>
      </button>
    </div>
  )
}

function PostReportButton({ postId, title, notify, compact }: {
  postId: string; title: string; notify?: PageProps['notify']; compact?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState<PostReport['reason']>('spam')
  const [detail, setDetail] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    setBusy(true)
    try {
      const r = await v3.reportPost(postId, { reason, detail })
      notify?.(r.already_reported ? '你已经举报过这篇帖子' : '举报已提交，等待管理员审核')
      setOpen(false)
      setDetail('')
    } catch (e) {
      notify?.((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  return (
    <>
      <button type="button" className={`post-report-btn${compact ? ' compact' : ''}`} title="举报这篇帖子"
        onClick={(e) => { e.stopPropagation(); setOpen(true) }}><Icon name="flag" size={13} /><span>举报</span></button>
      {open && (
        <Modal title="举报帖子" onClose={() => setOpen(false)} width={520}>
          <p className="hint" style={{ marginTop: 0 }}>举报会提交给本工作区管理员审核。恶意或重复举报也会被记录。</p>
          <div className="report-target"><b>{title}</b><small>请选择最符合的问题类型</small></div>
          <div className="report-reasons">
            {REPORT_REASONS.map(([value, label]) => (
              <button type="button" key={value} className={`report-reason${reason === value ? ' on' : ''}`} onClick={() => setReason(value)}>{label}</button>
            ))}
          </div>
          <label className="report-detail">补充说明
            <textarea rows={3} maxLength={500} value={detail} onChange={(e) => setDetail(e.target.value)} placeholder="可选：说明具体问题、误导之处或相关上下文" />
          </label>
          <div className="button-row" style={{ marginTop: 14, justifyContent: 'flex-end' }}>
            <button className="button" onClick={() => setOpen(false)}>取消</button>
            <button className="button danger" disabled={busy} onClick={submit}>{busy ? '提交中…' : '提交举报'}</button>
          </div>
        </Modal>
      )}
    </>
  )
}

function ModerationPanel({ data, status, notify, onOpen, onChanged }: {
  data: PostReportPage | null; status: 'open' | 'resolved' | 'dismissed' | 'all'
  notify: PageProps['notify']; onOpen: (id: string) => void; onChanged: () => void
}) {
  if (!data) return <Loading label="读取举报记录" />
  const decide = async (id: string, next: 'resolved' | 'dismissed') => {
    try {
      await v3.decidePostReport(id, next, next === 'resolved' ? '管理员已处理' : '管理员忽略该举报')
      notify(next === 'resolved' ? '举报已标记为已处理' : '举报已忽略')
      onChanged()
    } catch (e) { notify((e as Error).message, true) }
  }
  const removePost = async (id: string) => {
    if (!await appConfirm('删除这篇帖子？举报记录会一并标记为已处理，操作会写入审计日志。', { danger: true })) return
    try { await v3.deletePost(id); notify('帖子已删除'); onChanged() } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <div className="moderation-panel">
      <div className="moderation-kpis">
        <div><small>待处理</small><b>{data.counts.open}</b></div>
        <div><small>已处理</small><b>{data.counts.resolved}</b></div>
        <div><small>已忽略</small><b>{data.counts.dismissed}</b></div>
        <div><small>全部记录</small><b>{data.counts.all}</b></div>
      </div>
      {!data.items.length ? <EmptyState glyph="✓" title="没有对应的举报记录">{status === 'open' ? '当前没有待处理举报。' : '这个状态下没有记录。'}</EmptyState> : (
        <div className="moderation-list">
          {data.items.map((r) => (
            <article key={r.id} className={`moderation-card status-${r.status}`}>
              <header>
                <span className="moderation-reason"><Icon name="flag" size={14} />{r.reason_label}</span>
                <span className={`table-pill ${r.status === 'open' ? 'warn' : 'pass'}`}>{r.status_label}</span>
                <time>{localPostTime(r.created_at)}</time>
              </header>
              <button type="button" className="moderation-post" disabled={!r.post_exists} onClick={() => onOpen(r.post_id)}>
                <b>{r.post_title}</b><small>{r.post_author} · {r.post_excerpt}</small>
              </button>
              {r.detail && <p className="moderation-detail">{r.detail}</p>}
              <div className="moderation-meta">
                <span>举报人：{r.reporter_name || r.reporter_id}</span>
                {r.same_post_reports > 1 && <span>同一帖子待处理 {r.same_post_reports} 条</span>}
                {r.reviewed_at && <span>审核于 {localPostTime(r.reviewed_at)}</span>}
                {r.resolution && <span>结论：{r.resolution}</span>}
              </div>
              <div className="button-row tight">
                {r.post_exists && <button className="button small" onClick={() => onOpen(r.post_id)}>查看帖子</button>}
                {r.status === 'open' && <>
                  <button className="button small" onClick={() => decide(r.id, 'resolved')}>标记已处理</button>
                  <button className="button small ghost" onClick={() => decide(r.id, 'dismissed')}>忽略举报</button>
                  {r.post_exists && <button className="button small danger" onClick={() => removePost(r.post_id)}>删除帖子</button>}
                </>}
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  )
}

function ProfileForm({ profile, onSaved, notify }: {
  profile: CommunityProfile
  onSaved: (p: CommunityProfile) => void
  notify: PageProps['notify']
}) {
  const [name, setName] = useState(profile.name)
  const [bio, setBio] = useState(profile.bio)
  const [policy, setPolicy] = useState(profile.dm_policy)
  const [avatarColor, setAvatarColor] = useState(profile.avatar_color || '#2E5C9A')
  return (
    <div>
      <label>作者名<input value={name} maxLength={30} onChange={(e) => setName(e.target.value)} /></label>
      <label style={{ marginTop: 8 }}>简介<textarea rows={3} maxLength={500} value={bio} onChange={(e) => setBio(e.target.value)} /></label>
      <div style={{ marginTop: 10 }}><div className="hint">头像颜色</div><AvatarColorPicker value={avatarColor} onChange={setAvatarColor} /></div>
      <label style={{ marginTop: 8 }}>谁能私信我
        <select value={policy} onChange={(e) => setPolicy(e.target.value as typeof policy)}>
          <option value="reply_only">回复后可继续（默认）</option>
          <option value="open">允许自由私信</option>
          <option value="none">不允许私信</option>
        </select>
      </label>
      <button className="button small primary" style={{ marginTop: 10 }} onClick={async () => {
        try { onSaved((await v3.saveProfile({ name, bio, dm_policy: policy, avatar_color: avatarColor, public_profile: !!profile.public_profile })).profile) } catch (e) { notify((e as Error).message, true) }
      }}>保存</button>
    </div>
  )
}

/* ---------------- 发帖 / 编辑：完整页面（#/post/new/<类型>、#/post/<id>/edit） ---------------- */
type PostType = 'article' | 'question' | 'strategy'
const TYPE_LABEL: Record<PostType, string> = { strategy: '策略分享', article: '文章', question: '问答' }

export function ComposePage({ arg, onNavigate, notify }: PageProps & { arg: string }) {
  const parts = arg.split('/')
  const editId = parts[1] === 'edit' ? parts[0] : ''
  const [type, setType] = useState<PostType>((['article', 'question', 'strategy'] as PostType[]).find((t) => t === parts[1]) ?? 'article')
  const [orig, setOrig] = useState<Post | null>(null)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [refs, setRefs] = useState<Ref[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [tagInput, setTagInput] = useState('')
  const [suggested, setSuggested] = useState<string[]>([])
  const [strategies, setStrategies] = useState<StrategyFile[]>([])
  const [backtests, setBacktests] = useState<BacktestRow[]>([])
  const [slug, setSlug] = useState('')
  const [btId, setBtId] = useState(() => new URLSearchParams(window.location.hash.split('?')[1] || '').get('bt') ?? '')
  const [review, setReview] = useState(true)
  const [busy, setBusy] = useState(false)
  const others = usePresence('post', editId || undefined, true)

  useEffect(() => {
    v3.tags().then((r) => setSuggested(r.suggested)).catch(() => undefined)
    if (editId) {
      v3.post(editId).then((r) => {
        setOrig(r.post); setTitle(r.post.title); setBody(r.post.body); setRefs(r.post.refs ?? []); setTags(r.post.tags)
      }).catch((e) => notify((e as Error).message, true))
      return
    }
    v3.strategies().then((r) => setStrategies(r.strategies)).catch(() => undefined)
    v3.backtests(undefined, 100).then((r) => setBacktests(r.backtests)).catch(() => undefined)
  }, [editId, notify])
  const btOptions = useMemo(() => (slug ? backtests.filter((b) => b.slug === slug) : backtests), [backtests, slug])
  const addTag = (t: string) => { const v = t.trim().slice(0, 12); if (v && !tags.includes(v)) setTags([...tags, v].slice(0, 8)) }

  const submit = async () => {
    setBusy(true)
    try {
      if (editId) {
        await v3.updatePost(editId, { title, body, refs, tags })
        try { localStorage.removeItem(`qa.draft.post.${editId}`) } catch { /* 私密模式 */ }
        notify('已保存')
        onNavigate(`post/${editId}`)
        return
      }
      const r = await v3.createPost({
        type, title, body, tags, refs,
        strategy_slug: type === 'strategy' && slug ? slug : null,
        backtest_id: type === 'strategy' && btId ? btId : null,
        ai_review: type === 'strategy' && review,
      })
      try { localStorage.removeItem('qa.draft.post.new') } catch { /* 私密模式 */ }
      notify('已发布')
      onNavigate(`post/${r.post.id}`)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  const back = () => onNavigate(editId ? `post/${editId}` : 'square')
  const canSubmit = !busy && title.trim().length >= 2 && (editId || type !== 'strategy' || !!slug || !!btId)

  return (
    <div className="compose-page compose-full anim-in">
      <div className="editor-toolbar">
        <button className="button small" onClick={back}>← {editId ? '返回帖子' : '策略广场'}</button>
        <h1 className="toolbar-title">{editId ? '编辑帖子' : `发帖 · ${TYPE_LABEL[type]}`}</h1>
        <PresenceBar others={others} />
        <div className="spacer" />
        <span className="hint">以你的名义发布，标记为「成员」；Agent 代发的帖子会单独标注</span>
        <button className="button small" onClick={back}>取消</button>
        <button className="button small primary" disabled={!canSubmit} onClick={submit}>{busy ? '提交中…' : editId ? '保存' : '发布'}</button>
      </div>
      <div className="compose-body">
        <div className="compose-main">
          <input className="compose-title" value={title} maxLength={120} autoFocus={!editId} onChange={(e) => setTitle(e.target.value)}
            placeholder={type === 'question' ? '一句话说清你的问题' : '标题'} aria-label="标题" />
          <RichEditor value={body} onChange={setBody} refs={refs} onRefs={setRefs} rows={28} draftKey={editId ? `post.${editId}` : 'post.new'} notify={notify}
            initialMode="split"
            placeholder={type === 'strategy' ? '思路、参数、回测条件、局限……\n\n可以用「＋ 引用」插入回测结果、策略、代码片段和文件，或者直接粘贴截图。支持完整 Markdown、公式、Mermaid、<details> 折叠与 HTML 表格。' : '正文（Markdown）。可以引用回测、策略、代码与文件，或直接粘贴截图。支持公式、Mermaid、<details> 折叠与 HTML 表格。'} />
        </div>
        <aside className="compose-side">
          {!editId && (
            <Panel title="类型">
              <div className="segments" style={{ display: 'flex' }}>
                {(['strategy', 'article', 'question'] as const).map((t) => (
                  <button key={t} aria-pressed={type === t} onClick={() => setType(t)}>{TYPE_LABEL[t]}</button>
                ))}
              </div>
            </Panel>
          )}
          {!editId && type === 'strategy' && (
            <Panel title="附加策略与回测">
              <label>策略（冻结当前版本代码）
                <select value={slug} onChange={(e) => { setSlug(e.target.value); setBtId('') }}>
                  <option value="">— 选择策略 —</option>
                  {strategies.map((s) => <option key={s.slug} value={s.slug}>{s.name}（{s.version}）</option>)}
                </select>
              </label>
              <label style={{ marginTop: 10 }}>回测（推荐）
                <select value={btId} onChange={(e) => setBtId(e.target.value)}>
                  <option value="">— 不附回测 —</option>
                  {btOptions.map((b) => <option key={b.id} value={b.id}>{b.strategy_name} · {fmtPct(b.metrics.total_return, 1, true)} · {ago(b.created_at)}</option>)}
                </select>
              </label>
              <label className="check" style={{ marginTop: 10 }}><input type="checkbox" checked={review} onChange={(e) => setReview(e.target.checked)} /> 发布后请 AI 审稿人公开挑刺</label>
            </Panel>
          )}
          {editId && orig && (orig.strategy || orig.backtest) && <Callout>附带的策略代码与回测快照不可修改，读者看到的始终是发布时那一份。</Callout>}
          <Panel title="标签" subtitle="最多 8 个">
            <div className="chip-row">
              {tags.map((t) => <button key={t} className="chip on" onClick={() => setTags(tags.filter((x) => x !== t))}>{t} ×</button>)}
            </div>
            <input value={tagInput} maxLength={12} placeholder="输入标签后回车" style={{ marginTop: 8 }}
              onChange={(e) => setTagInput(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) { e.preventDefault(); addTag(tagInput); setTagInput('') } }} />
            <div className="chip-row" style={{ marginTop: 8 }}>
              {suggested.filter((t) => !tags.includes(t)).map((t) => <button key={t} className="chip" onClick={() => addTag(t)}>{t}</button>)}
            </div>
          </Panel>
          <Panel title="写作提示">
            <ul className="hint compose-tips">
              <li>用 #、##、### 分清层级，读者可以快速跳读</li>
              <li>指标定义写成公式：{'$\\frac{\\mu}{\\sigma}\\sqrt{N}$'} 这样的 LaTeX</li>
              <li>流程用 ```mermaid，长附录放进 &lt;details&gt; 折叠</li>
              <li>引用回测会冻结当时的指标，之后改动不影响帖子</li>
              <li>表情面板里有 QuantLab Qmoji；输入 :qbot-agree: 这样的短码也会显示</li>
            </ul>
          </Panel>
        </aside>
      </div>
    </div>
  )
}

/* ---------------- 帖子详情 ---------------- */
export function PostPage({ id, onNavigate, notify }: PageProps & { id: string }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof v3.post>> | null>(null)
  const [showMuted, setShowMuted] = useState<Set<string>>(new Set())
  const [error, setError] = useState('')
  const [text, setText] = useState('')
  const [replyTo, setReplyTo] = useState<CommentRow | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [reviewing, setReviewing] = useState(false)
  const [reactions, setReactions] = useState<QmojiReaction[]>([])
  const commentRef = useRef<HTMLTextAreaElement>(null)

  const load = useCallback(() => {
    v3.post(id).then((r) => {
      setData({ ...r, post: localizeSeedPost(r.post), related: r.related.map(localizeSeedPost) })
      setReactions(r.post.reactions ?? [])
    }).catch((e) => setError(e.message))
  }, [id])
  useEffect(load, [load])

  const commentRows = data?.comments ?? []
  const childrenMap = useMemo(() => {
    const map = new Map<string, CommentRow[]>()
    for (const c of commentRows) {
      if (!c.parent_id) continue
      const arr = map.get(c.parent_id) ?? []
      arr.push(c)
      map.set(c.parent_id, arr)
    }
    return map
  }, [commentRows])
  const replyCounts = useMemo(() => {
    const parentOf = new Map(commentRows.map((c) => [c.id, c.parent_id]))
    const counts = new Map<string, number>()
    for (const c of commentRows) {
      let p = c.parent_id
      let guard = 0
      while (p && guard++ < 100) {
        counts.set(p, (counts.get(p) ?? 0) + 1)
        p = parentOf.get(p) ?? null
      }
    }
    return counts
  }, [commentRows])

  if (error) return <Callout tone="fail">{error}</Callout>
  if (!data) return <Loading />
  const { post, comments, viewer } = data
  const mine = post.author_id ? post.author_id === data.viewer_id : post.author === viewer
  const bt = post.backtest
  const roots = comments.filter((c) => !c.parent_id)
  const children = (pid: string) => childrenMap.get(pid) ?? []
  const toggleReplies = (cid: string) => setExpanded((prev) => {
    const next = new Set(prev)
    if (next.has(cid)) next.delete(cid)
    else next.add(cid)
    return next
  })
  const expandThread = (cid: string) => setExpanded((prev) => {
    const next = new Set(prev)
    let cur = comments.find((c) => c.id === cid)
    let guard = 0
    while (cur && guard++ < 100) {
      next.add(cur.id)
      cur = cur.parent_id ? comments.find((c) => c.id === cur!.parent_id) : undefined
    }
    return next
  })

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
    const target = replyTo
    await act(() => v3.comment(id, text.trim(), target?.id), '已发表')
    if (target) expandThread(target.id)
    setText('')
    setReplyTo(null)
  }
  const reactQmoji = async (emoji: string) => {
    try {
      const r = await v3.qmojiPost(id, emoji)
      setReactions(r.reactions)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }
  const insertEmoji = (token: string) => {
    const el = commentRef.current
    const start = el ? el.selectionStart : text.length
    const end = el ? el.selectionEnd : text.length
    setText(text.slice(0, start) + token + text.slice(end))
    requestAnimationFrame(() => {
      el?.focus()
      const pos = start + token.length
      el?.setSelectionRange(pos, pos)
    })
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
  const relation = data.author_relation
  const authorIsAdmin = relation.author_role === 'admin'
  const canSocial = !mine && !!post.author_id && post.author_id !== 'system' && post.source?.kind !== 'agent'
  const toggleFollow = () => act(
    () => relation.is_following ? v3.unfollowUser(post.author_id!) : v3.followUser(post.author_id!),
    relation.is_following ? `已取消关注 ${post.author}` : `已关注 ${post.author}`,
  )
  const toggleBlock = async () => {
    if (!relation.is_blocked && !(await appConfirm(`拉黑 ${post.author}？拉黑后会自动隐藏对方的帖子与评论。`))) return
    try {
      if (relation.is_blocked) await v3.unblockUser(post.author_id!)
      else await v3.blockUser(post.author_id!)
      notify(relation.is_blocked ? `已取消拉黑 ${post.author}` : `已拉黑 ${post.author}`)
      if (!relation.is_blocked) onNavigate('square')
      else load()
    } catch (e) { notify((e as Error).message, true) }
  }
  const openDm = async () => {
    try {
      const r = await collab.dm(post.author_id!)
      onNavigate(`groups/${r.group.id}`)
    } catch (e) { notify((e as Error).message, true) }
  }

  const renderComment = (c: CommentRow, depth = 0) => {
    const replies = children(c.id)
    const count = replyCounts.get(c.id) ?? 0
    const open = expanded.has(c.id)
    return (
    <div key={c.id} className={`comment${c.is_ai ? ' ai' : ''}${post.accepted_comment === c.id ? ' accepted' : ''}`} style={{ marginLeft: depth ? 34 : 0 }}>
      <div className="comment-head">
        <UserAvatar color={c.author_color || '#2E5C9A'} size={26} title={c.author} />
        <b>{c.author}</b>
        {c.is_ai && <Tag tone={c.meta?.mode === 'llm' ? 'agent' : 'warn'}>{c.meta?.mode === 'llm' ? c.meta.model : '规则审稿'}</Tag>}
        {post.accepted_comment === c.id && <Tag tone="ok">已采纳</Tag>}
        <span className="muted">{ago(c.created_at)}</span>
      </div>
      <div className="comment-body">{c.muted && !showMuted.has(c.id)
        ? <button type="button" className="chat-muted" onClick={() => setShowMuted(new Set([...showMuted, c.id]))}>已屏蔽 {c.author} 的评论 · 点击显示</button>
        : <Markdown text={c.body} compact />}</div>
      <div className="comment-actions">
        <button className={`link${c.liked ? ' on' : ''}`} onClick={() => act(() => v3.likeComment(c.id))}>▲ {c.like_count}</button>
        <button className="link" onClick={() => { setReplyTo(c); document.getElementById('comment-box')?.focus() }}>回复</button>
        {post.type === 'question' && mine && !c.is_ai && <button className="link" onClick={() => act(() => v3.accept(id, c.id))}>{post.accepted_comment === c.id ? '取消采纳' : '采纳'}</button>}
        {(c.author === viewer || c.is_ai) && <button className="link danger" onClick={() => act(() => v3.deleteComment(c.id))}>删除</button>}
      </div>
      {count > 0 && (
        <button type="button" className="comment-replies-toggle" aria-expanded={open} aria-controls={`replies-${c.id}`}
          onClick={() => toggleReplies(c.id)}>
          {open ? `收起 ${count} 条回复` : `${count} 条回复`}
          <span className="comment-replies-chev" aria-hidden="true">{open ? '▴' : '▾'}</span>
        </button>
      )}
      {open && replies.length > 0 && (
        <div className="comment-replies" id={`replies-${c.id}`}>
          {replies.map((x) => renderComment(x, depth + 1))}
        </div>
      )}
    </div>
  )
  }

  return (
    <div className="post-page">
      <div className="editor-toolbar">
        <button className="button small" onClick={() => onNavigate('square')}>← 策略广场</button>
        <div className="spacer" />
        <PostReactionBar post={post} notify={notify} onChanged={load} />
        <button className={`button small${post.favorited ? ' on' : ''}`} onClick={() => act(() => v3.favoritePost(id))}>{post.favorited ? '已收藏' : '收藏'} {post.favorite_count}</button>
        {!mine && <PostReportButton postId={id} title={post.title} notify={notify} />}
        {post.strategy && <button className="button small primary" onClick={clone}>⎘ 克隆策略</button>}
        {post.strategy && <button className="button small" disabled={reviewing} onClick={review}>{reviewing ? '审稿中…' : '✦ AI 审稿'}</button>}
        <ShareToButton item={{ type: 'post', id, title: post.title }} notify={notify} onNavigate={onNavigate} />
        <SharePublicButton type="post" id={id} title={post.title} notify={notify} compact />
        {canSocial && <button className={`button small${relation.is_following ? ' on' : ' primary'}`} onClick={toggleFollow}>{relation.is_following ? '已关注' : '关注作者'}</button>}
        {canSocial && <button className="button small" disabled={!relation.can_dm} title={relation.dm_reason || '发起私聊'} onClick={openDm}>{relation.can_dm ? '私聊作者' : '不可私信'}</button>}
        {canSocial && !authorIsAdmin && <button className="button small danger" onClick={toggleBlock}>{relation.is_blocked ? '取消拉黑' : '拉黑'}</button>}
        {data.can_moderate && <button className="button small" onClick={() => act(() => v3.featurePost(id))}>{post.featured ? '取消精华' : '设为精华'}</button>}
        {data.can_moderate && <button className="button small" onClick={() => act(() => v3.pinPost(id))}>{post.pinned ? '取消置顶' : '置顶'}</button>}
        {!mine && <button className={`button small${data.post_muted ? ' on' : ''}`} title="屏蔽后，这篇帖子的新回复、点赞不再通知你"
          onClick={() => act(() => (data.post_muted ? inbox.unmute('post', id) : inbox.mute('post', id, post.title)), data.post_muted ? '已恢复通知' : '已屏蔽这篇帖子的通知')}>{data.post_muted ? '🔕 已屏蔽' : '屏蔽通知'}</button>}
        {!mine && !authorIsAdmin && post.author_id && post.author_id !== 'system' && post.source?.kind !== 'agent' && <button className="button small" title="不再看到此人的帖子与通知"
          onClick={() => act(() => (data.author_muted ? inbox.unmute('user', post.author_id!) : inbox.mute('user', post.author_id!, post.author)), data.author_muted ? `已取消屏蔽 ${post.author}` : `已屏蔽 ${post.author}`)}>{data.author_muted ? `取消屏蔽 ${post.author}` : `屏蔽 ${post.author}`}</button>}
        {mine && <button className="button small" onClick={() => onNavigate(`post/${id}/edit`)}>编辑</button>}
        {(mine || data.can_moderate) && <button className="button small danger" title={mine ? undefined : '工作区管理员删除他人的帖子'} onClick={async () => {
          if (await appConfirm(mine ? '删除这篇帖子？' : `以工作区管理员身份删除「${post.author}」的这篇帖子？此操作会记入审计日志。`, { danger: true })) {
            try { await v3.deletePost(id); notify('已删除'); onNavigate('square') } catch (e) { notify((e as Error).message, true) }
          }
        }}>删除</button>}
      </div>

      <div className="community-layout">
        <div>
          <article className="panel post-article">
            <div className="panel-body" style={{ paddingTop: 24 }}>
              <div className="post-headline post-detail-headline">
                <div className="post-title-stack">
                  <div className="post-title">
                    {post.pinned && <span className="badge pin">置顶</span>}
                    {post.featured && <span className="badge feat">精华</span>}
                    <span className={`badge type-${post.type}`}>{post.type_label}</span>
                  </div>
                  <h1 className="post-h1">{post.title}</h1>
                </div>
                <time className="post-time" dateTime={new Date(post.created_at * 1000).toISOString()} title={`发布于 ${localPostTime(post.created_at * 1000)}`}>
                  <Icon name="clock" size={14} /><span>{localPostTime(post.created_at * 1000)}</span>
                </time>
              </div>
              <div className="post-meta">
                <button type="button" className="post-author-link" onClick={() => post.author_id && onNavigate(`profile/${post.author_id}`)}>
                  <UserAvatar color={post.author_color} size={26} title={post.author} /><span>{post.author}</span>
                </button>
                <SourceBadge source={post.source} />
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
              {post.source?.kind === 'agent' && <Callout>这篇帖子由 {post.source.label || 'QuantBot'} 代 {post.source.on_behalf_of || post.author} 撰写并发布，结论请结合附带的回测自行判断。</Callout>}
              <div className="post-body"><RichBody text={post.body} refs={post.refs ?? []} /></div>
              {post.strategy?.code && (
                <details className="post-code" open={!bt}>
                  <summary>策略代码 · {post.strategy.name} · {post.strategy.securities?.join(', ')} {post.strategy.timeframe} · <span className="mono">{post.strategy.code_sha256?.slice(0, 12)}</span></summary>
                  <CodeView code={post.strategy.code} maxHeight={520} />
                  <div className="button-row" style={{ marginTop: 10 }}><button className="button small primary" onClick={clone}>⎘ 克隆到我的策略</button></div>
                </details>
              )}
              <div className="post-reaction-foot"><QmojiReactionBar reactions={reactions} onToggle={reactQmoji} /></div>
            </div>
          </article>

          <Panel title={`讨论（${comments.length}）`}>
            {roots.map((c) => renderComment(c))}
            {!comments.length && <div className="hint">还没有评论</div>}
            <div className="comment-box">
              {replyTo && <div className="hint"><span>{`回复 @${replyTo.author}：`}</span>{replyTo.body.slice(0, 40)}… <button className="link" onClick={() => setReplyTo(null)}>取消</button></div>}
              <div className="comment-input">
                <textarea ref={commentRef} id="comment-box" rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="说点什么（支持 Markdown、Qmoji，Ctrl+Enter 发送）" onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send() }} />
                <div className="comment-tools">
                  <QmojiPicker small placement="up" onPick={insertEmoji} />
                  <span className="hint">输入 <code>:qbot-agree:</code> 也会显示 · 支持 Markdown</span>
                </div>
                {/:qbot-[a-z0-9-]+:/.test(text) && <div className="comment-preview"><Markdown text={text} compact /></div>}
              </div>
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

    </div>
  )
}
