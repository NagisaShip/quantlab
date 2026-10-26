import { useEffect, useMemo, useState } from 'react'
import { publicApi, type PublicSharePayload, type PublicShareObjectType } from '../apiPublic'
import { v4 } from '../api/platform'
import { LogoMark } from '../components/icons'
import QRCode from '../components/QRCode'
import { Callout, Loading } from '../components/ui'

const TYPE_META: Record<PublicShareObjectType, { label: string; eyebrow: string; glyph: string }> = {
  group: { label: 'QuantLab 群组', eyebrow: '研究协作群', glyph: '群' },
  user: { label: 'QuantLab 研究员', eyebrow: '公开个人主页', glyph: '研' },
  post: { label: '策略广场', eyebrow: '研究帖子', glyph: '帖' },
  strategy: { label: '策略档案', eyebrow: '可复核策略', glyph: '策' },
}

function num(value: unknown, digits = 2): string {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n.toFixed(digits) : '—'
}
function pct(value: unknown): string {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '—'
}
function when(value: unknown): string {
  const n = Number(value)
  if (!Number.isFinite(n) || !n) return '—'
  return new Date(n > 1e12 ? n : n * 1000).toLocaleDateString('zh-CN', { year: 'numeric', month: 'short', day: 'numeric' })
}
function publicLink(token: string): string {
  const url = new URL(window.location.href)
  url.hash = `/share/${token}`
  return url.toString()
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="pub-metric"><span>{label}</span><b>{value}</b></div>
}

export default function PublicSharePage({ token, onLogin, onOpen }: {
  token: string
  onLogin: () => void
  onOpen: (route: string) => void
}) {
  const [data, setData] = useState<PublicSharePayload | null>(null)
  const [error, setError] = useState('')
  const [loggedIn, setLoggedIn] = useState(false)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    document.title = '公开分享 · QuantLab v0.1.0-preview.2'
    let alive = true
    setData(null); setError('')
    publicApi.share(token).then((r) => { if (alive) setData(r) }).catch((e: Error) => { if (alive) setError(e.message) })
    v4.me().then(() => { if (alive) setLoggedIn(true) }).catch(() => { if (alive) setLoggedIn(false) })
    return () => { alive = false }
  }, [token])

  const url = useMemo(() => publicLink(token), [token])
  if (error) return <div className="public-share-page"><Callout tone="fail" title="这个分享链接不可用">{error}</Callout></div>
  if (!data) return <div className="public-share-page public-loading"><Loading label="正在打开公开预览" /></div>
  const share = data.share
  const obj = share.object as Record<string, unknown>
  const meta = TYPE_META[share.type]
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1800)
    } catch { /* 浏览器不允许时保持原样 */ }
  }

  return (
    <div className="public-share-page">
      <header className="pub-top">
        <a className="pub-brand" href="#/home" aria-label="QuantLab 首页"><LogoMark size={24} /><span className="pub-brand-copy"><span>QuantLab</span><small>v0.1.0-preview.2</small></span></a>
        <div className="pub-top-actions">
          <span className="pub-public-pill"><i />公开预览</span>
          {loggedIn
            ? <button type="button" className="button small primary" onClick={() => onOpen(share.route)}>进入完整页面</button>
            : <button type="button" className="button small primary" onClick={onLogin}>登录 / 注册</button>}
        </div>
      </header>

      <main className="pub-main">
        <section className="pub-hero">
          <div className="pub-hero-copy">
            <div className="pub-eyebrow"><span>{meta.glyph}</span>{meta.eyebrow}</div>
            <h1>{share.title}</h1>
            <p>{share.type === 'group' ? '一个围绕策略研究、任务分工与共享回测的 QuantLab 协作空间。' : share.type === 'user' ? '查看这位研究员的公开身份与研究足迹。' : '这份内容由 QuantLab 成员分享，未登录也可以查看公开摘要。'}</p>
            <div className="pub-hero-tags">
              <span>{meta.label}</span>
              <span>发布于 {when(share.created_at)}</span>
              <span>受限预览</span>
            </div>
          </div>
          <div className="pub-qr-card">
            <QRCode value={url} size={142} label="打开公开分享二维码" />
            <div><b>手机扫码打开</b><small>二维码仅指向公开预览，可随时撤销</small></div>
          </div>
        </section>

        {share.type === 'group' && (
          <section className="pub-panel pub-group-panel">
            <div className="pub-panel-head"><div><small>群组身份</small><h2>{String(obj.name || '')}</h2></div><span className="pub-number">{String(obj.quantlab_no || '')}</span></div>
            <p className="pub-description">{String(obj.description || '这个群组还没有公开简介。')}</p>
            <div className="pub-metrics">
              <Metric label="成员" value={num(obj.member_count, 0)} />
              <Metric label="进行中任务" value={num(obj.open_tasks, 0)} />
              <Metric label="群主" value={String(obj.owner_name || '—')} />
              <Metric label="可见性" value={obj.visibility === 'open' ? '公开群组' : '私密群组'} />
            </div>
            <div className="pub-note"><b>登录后可以</b>查看完整讨论、任务看板与群内共享内容；公开群可直接申请加入。</div>
          </section>
        )}

        {share.type === 'user' && (
          <section className="pub-panel pub-user-panel">
            <div className="pub-user-head">
              <span className="pub-avatar" style={{ background: String(obj.avatar_color || '#2E5C9A') }}>{String(obj.name || '?').slice(0, 1)}</span>
              <div><small>QuantLab UID · {String(obj.quantlab_uid || '—')}</small><h2>{String(obj.name || '')}</h2><span>{obj.username ? `@${String(obj.username)}` : '公开研究员'}{obj.public_profile ? ' · 已公开' : ' · 链接分享'}</span></div>
            </div>
            <p className="pub-description">{String(obj.bio || '这位研究员还没有公开简介。')}</p>
            <div className="pub-metrics">
              <Metric label="关注者" value={num(obj.followers, 0)} />
              <Metric label="正在关注" value={num(obj.following, 0)} />
              <Metric label="公开帖子" value={num(obj.posts, 0)} />
            </div>
            <div className="pub-note"><b>隐私边界</b>公开预览不包含邮箱、私信、关注关系明细或未公开帖子。</div>
          </section>
        )}

        {share.type === 'post' && (
          <section className="pub-panel pub-post-panel">
            <div className="pub-panel-head"><div><small>{String(obj.type_label || '帖子')}</small><h2>{String(obj.title || share.title)}</h2></div><span className="pub-author"><i style={{ background: String(obj.author_color || '#2E5C9A') }} />{String(obj.author || 'QuantLab 成员')}</span></div>
            {Object.keys((obj.metrics as Record<string, unknown>) || {}).length > 0 && (
              <div className="pub-metrics">
                <Metric label="总收益" value={pct((obj.metrics as Record<string, unknown>).total_return)} />
                <Metric label="最大回撤" value={pct((obj.metrics as Record<string, unknown>).max_drawdown)} />
                <Metric label="Sharpe" value={num((obj.metrics as Record<string, unknown>).sharpe)} />
                <Metric label="交易" value={num((obj.metrics as Record<string, unknown>).trades, 0)} />
              </div>
            )}
            <article className="pub-excerpt">{(String(obj.body || obj.excerpt || '')).slice(0, 1200)}{String(obj.body || '').length > 1200 ? '…' : ''}</article>
            <div className="pub-hero-tags">{(obj.tags as string[] || []).map((tag) => <span key={tag}>{tag}</span>)}</div>
            <div className="pub-metrics small">
              <Metric label="点赞" value={num(obj.likes, 0)} />
              <Metric label="评论" value={num(obj.comments, 0)} />
            </div>
            <div className="pub-note"><b>登录后可以</b>阅读完整正文、评论、策略快照，并在有权限时克隆策略。</div>
          </section>
        )}

        {share.type === 'strategy' && (
          <section className="pub-panel pub-strategy-panel">
            <div className="pub-panel-head"><div><small>{String(obj.kind || '策略')} · v{String(obj.version || '—')}</small><h2>{String(obj.name || share.title)}</h2></div><span className={`pub-status ${String(obj.status || '')}`}>{String(obj.status || 'draft')}</span></div>
            <p className="pub-description">{String(obj.notes || '这份策略还没有公开说明。')}</p>
            <div className="pub-metrics">
              <Metric label="标的" value={String(obj.symbol || '—')} />
              <Metric label="周期" value={String(obj.timeframe || '—')} />
              <Metric label="作者" value={String(obj.author || '—')} />
              <Metric label="代码行数" value={num(obj.code_lines, 0)} />
            </div>
            {Boolean(obj.params) && Object.keys(obj.params as Record<string, unknown>).length > 0 && (
              <div className="pub-param-list">{(Object.entries(obj.params as Record<string, unknown>)).slice(0, 12).map(([k, v]) => <span key={k}><b>{k}</b>{String(v)}</span>)}</div>
            )}
            <div className="pub-note"><b>安全预览</b>策略源码、完整回测记录和实时配置不会出现在未登录页面。</div>
          </section>
        )}

        <section className="pub-cta">
          <div><small>READY TO GO DEEPER?</small><h2>{loggedIn ? '你已登录，继续进入完整内容' : '注册 QuantLab，进入完整研究现场'}</h2><p>{loggedIn ? '完整页面会继续校验你的工作区权限。' : '查看完整讨论、回测曲线、评论与协作记录。注册后还可创建自己的策略和群组。'}</p></div>
          <div className="pub-cta-actions">
            {loggedIn
              ? <button type="button" className="button primary" onClick={() => onOpen(share.route)}>进入完整页面 →</button>
              : <><button type="button" className="button primary" onClick={onLogin}>登录</button><button type="button" className="button" onClick={onLogin}>免费注册</button></>}
            <button type="button" className="button small" onClick={copy}>{copied ? '已复制' : '复制分享链接'}</button>
          </div>
        </section>
        <div className="pub-trust"><span>◈</span>受限预览不含私聊、群消息、任务详情、邮箱与策略源码。内容所有者可随时撤销分享。</div>
      </main>

      <footer className="pub-footer"><span><LogoMark size={16} /> QuantLab v0.1.0-preview.2 · 让策略研究可复核、可协作</span><span>公开分享页</span></footer>
    </div>
  )
}
