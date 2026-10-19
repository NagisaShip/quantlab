/**
 * 用户主页：他人看你的资料、关注关系、帖子与研究成果；本人看自己的主页时可以直接跳去编辑。
 *
 * 展示哪些研究成果（因子 / 策略 / 回测结果 / 模型 / 模拟收益）由本人在「设置 · 我的账号」里决定，
 * 主页只按 /api/community/users/{id}/showcase 返回的配置渲染，隐藏的板块对任何人都不显示。
 */
import { useCallback, useEffect, useState } from 'react'
import { fmtPct, tone, v3, type UserShowcase } from '../api/research'
import { collab } from '../apiCollab'
import { appConfirm } from '../components/dialog'
import { Callout, EmptyState, Loading, Panel, Tag } from '../components/ui'
import { MetricGrid } from '../components/blocks/RichBlock'
import { LineChart, COLORS } from '../components/charts2'
import { UserAvatar } from '../components/UserAvatar'
import { PostCard } from './Community'
import { copyText, SharePublicButton } from '../components/PublicShare'
import type { PageProps } from './types'

const POLICY_LABEL: Record<'reply_only' | 'open' | 'none', string> = {
  reply_only: '对方回复后可继续发送',
  open: '允许自由私信',
  none: '不允许私信',
}

/** 板块里的一条成果：一行标题 + 副标题，回测/模拟收益再带几个数字。 */
function ShowcaseRow({ item, onOpen }: {
  item: UserShowcase['sections'][number]['items'][number]
  onOpen: (link: string) => void
}) {
  const labels: Record<string, string> = { total_return: '策略收益', sharpe: '夏普比率', max_drawdown: '最大回撤', rank_ic: 'Rank IC', oos_rank_ic: '样本外 Rank IC', ic_ir: 'IC IR', ic: 'IC', mse: 'MSE', accuracy: '准确率', r2: 'R²' }
  const metrics = Object.entries(item.metrics || {}).filter(([, v]) => v !== null)
  const curve = item.preview
  return (
    <li className="profile-result-card">
      <div className="profile-result-heading">
        <div><h3>{item.title || item.id}</h3>{item.sub && <p>{item.sub}</p>}</div>
        {item.approved && <Tag tone="ok">已批准</Tag>}
        {item.link && <button type="button" className="button small" onClick={() => onOpen(item.link!)}>查看详情</button>}
      </div>
      {metrics.length > 0 && <MetricGrid items={metrics.map(([key, value]) => ({ key, label: labels[key] || key, value, format: ['total_return', 'max_drawdown', 'accuracy'].includes(key) ? 'pct' : 'num', digits: 3 }))} />}
      {typeof item.pnl === 'number' && <div className={`profile-result-pnl num ${tone(item.pnl)}`}>{fmtPct(item.pnl, 2, true)}<small>模拟收益</small></div>}
      {curve?.timestamps?.length && curve.equity?.length ? <div className="profile-result-chart"><LineChart timestamps={curve.timestamps} series={[
        { name: '累计收益', values: curve.equity, color: COLORS.strategy, area: true },
        ...(curve.benchmark?.length ? [{ name: '基准收益', values: curve.benchmark, color: COLORS.benchmark, dashed: true }] : []),
      ]} height={220} width={640} ariaLabel={`${item.title}收益曲线`} /></div> : null}
    </li>
  )
}

export default function ProfilePage({ identifier, onNavigate, notify }: PageProps & { identifier: string }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof v3.userProfile>> | null>(null)
  const [showcase, setShowcase] = useState<UserShowcase | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const load = useCallback(() => {
    v3.userProfile(identifier).then((r) => { setData(r); setError('') }).catch((e: Error) => setError(e.message))
    v3.userShowcase(identifier).then(setShowcase).catch(() => setShowcase(null))
  }, [identifier])
  useEffect(load, [load])

  const act = async (label: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(label)
    try { await fn(); notify(ok); load() } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }

  if (error) return <Callout tone="fail" title="无法查看用户主页">{error}</Callout>
  if (!data) return <Loading label="正在读取用户主页" />
  const u = data.user
  const relationLocked = data.is_blocked || data.blocked_me

  const follow = () => act(
    'follow',
    () => data.is_following ? v3.unfollowUser(u.id) : v3.followUser(u.id),
    data.is_following ? '已取消关注' : '已关注',
  )
  const block = async () => {
    if (!data.is_blocked && !(await appConfirm(`拉黑 ${u.name}？`))) return
    await act(
      'block',
      () => data.is_blocked ? v3.unblockUser(u.id) : v3.blockUser(u.id),
      data.is_blocked ? '已取消拉黑' : '已拉黑，相关帖子将自动隐藏',
    )
  }
  const dm = async () => {
    setBusy('dm')
    try {
      const r = await collab.dm(u.id)
      onNavigate(`groups/${r.group.id}`)
    } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }

  const sections = showcase?.sections ?? []
  const limit = showcase?.showcase.limit ?? 5

  return (
    <div className="profile-page profile-research-page">
      <section className={`profile-hero${relationLocked ? ' locked' : ''}`}>
        <UserAvatar src={u.avatar_url} color={u.avatar_color} size={76} title={u.name} />
        <div className="profile-copy">
          <div className="profile-name"><h1>{u.name}</h1>{u.username && <span>@{u.username}</span>}{u.role === 'admin' && <Tag tone="warn">工作区管理员</Tag>}{data.is_me && <Tag tone="ok">我</Tag>}{u.public_profile && <Tag tone="ok">公开资料</Tag>}{data.is_mutual && <Tag tone="ok">互相关注</Tag>}</div>
          <button type="button" className="profile-uid" title="复制 QuantLab UID" onClick={() => { copyText(u.quantlab_uid).then(() => notify('QuantLab UID 已复制')).catch(() => undefined) }}><span>QuantLab UID</span><b>{u.quantlab_uid || '—'}</b><i>复制</i></button>
          <p>{u.bio || '还没有简介'}</p>
          <div className="profile-stats">
            <span><b>{u.posts}</b>帖子</span><span><b>{u.followers}</b>关注者</span><span><b>{u.following}</b>正在关注</span>
            <span><b>{u.likes}</b>获赞</span><span><b>{u.clones}</b>被克隆</span>
          </div>
          <div className="profile-dm-note">私信：{POLICY_LABEL[u.dm_policy]}{data.is_mutual ? ' · 互相关注，私聊已建好' : ''}</div>
        </div>
        <div className="profile-actions">
          {/* 看自己的主页：直接跳去设置里编辑（个人资料只有这一个入口） */}
          {data.is_me && <button type="button" className="button primary" onClick={() => onNavigate('settings/account')}>编辑资料</button>}
          {!data.is_me && !relationLocked && <button type="button" className={`button${data.is_following ? ' on' : ' primary'}`} disabled={!!busy} onClick={follow}>{data.is_following ? '已关注' : '关注'}</button>}
          {!data.is_me && !relationLocked && <button type="button" className="button" disabled={!!busy || !data.can_dm} title={data.dm_reason || '发起私聊'} onClick={dm}>{data.can_dm ? '私聊' : '不可私信'}</button>}
          {!data.is_me && u.role !== 'admin' && <button type="button" className={`button small${data.is_blocked ? ' danger' : ''}`} disabled={!!busy} onClick={block}>{data.is_blocked ? '取消拉黑' : '拉黑'}</button>}
          {(data.is_me || u.public_profile) && <SharePublicButton type="user" id={u.id} title={u.name} notify={notify} compact />}
          <button type="button" className="button small" onClick={() => onNavigate('square')}>返回广场</button>
        </div>
      </section>

      {data.blocked_me && <Callout tone="warn" title="无法关注或互动">对方已将你拉黑，因此看不到对方的帖子。</Callout>}
      {data.is_blocked && <Callout tone="warn" title="你已拉黑该用户">该用户的帖子会从策略广场自动隐藏。取消拉黑后可恢复。</Callout>}
      {showcase?.hidden_reason && <Callout tone="warn" title="研究成果已隐藏">{showcase.hidden_reason}</Callout>}

      <div className="community-layout profile-body">
        <div>
          <Panel
            title="研究成果"
            subtitle={sections.length ? `研究记录与可复核数据 · 每块最多 ${limit} 条` : undefined}
            actions={data.is_me ? <button className="button small" onClick={() => onNavigate('settings/account')}>调整展示</button> : undefined}
          >
            {!showcase ? <div className="hint">没有开启展示。</div> : !sections.length ? (
              <EmptyState title={data.is_me ? '还没有开启任何展示板块' : '对方没有公开研究成果'}>
                {data.is_me ? '在「设置 · 我的账号 · 主页展示」里勾选想展示的因子、策略、回测结果、模型与模拟收益。' : '对方可以在设置里选择展示哪些研究成果。'}
              </EmptyState>
            ) : (
              <div className="profile-showcase">
                <nav className="profile-section-nav" aria-label="研究成果板块">{sections.map(sec => <a key={sec.key} href={`#profile-section-${sec.key}`} onClick={e => { e.preventDefault(); document.getElementById(`profile-section-${sec.key}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }}>{sec.label}<span>{sec.count}</span></a>)}</nav>
                {sections.map((sec) => (
                  <section key={sec.key} id={`profile-section-${sec.key}`} className="profile-showcase-block">
                    <div className="profile-showcase-head">
                      <b>{sec.label}</b>
                      <span className="muted small">{sec.count > sec.items.length ? `${sec.items.length} / ${sec.count}` : `${sec.count}`}</span>
                    </div>
                    {sec.items.length ? (
                      <ul className="plain-list">
                        {sec.items.map((it) => <ShowcaseRow key={`${sec.key}-${it.id}`} item={it} onOpen={onNavigate} />)}
                      </ul>
                    ) : <div className="hint">还没有{sec.label}。</div>}
                  </section>
                ))}
              </div>
            )}
          </Panel>

          <Panel title={`帖子 · ${data.posts.length}`} subtitle={relationLocked ? '当前不可见' : '最近发布的策略、文章与问答'}>
            {data.posts.length ? (
              <div className="post-list profile-post-list">
                {data.posts.map((p) => <PostCard key={p.id} post={p} notify={notify} canReport={!data.is_me} onOpen={() => onNavigate(`post/${p.id}`)} onTag={() => undefined} />)}
              </div>
            ) : <EmptyState title={relationLocked ? '帖子已隐藏' : '还没有发布内容'}>发布第一篇策略或文章后会显示在这里。</EmptyState>}
          </Panel>
        </div>
        <aside className="community-side">
          {data.is_me ? (
            <Panel title="个人资料" subtitle="显示名、简介、头像与私信策略统一在设置里编辑">
              <ul className="hint profile-rule-list">
                <li>资料会显示在策略广场、群组和你的主页。</li>
                <li>主页展示哪些研究成果也在同一页设置。</li>
              </ul>
              <button type="button" className="button small primary" onClick={() => onNavigate('settings/account')}>去设置 · 我的账号</button>
            </Panel>
          ) : (
            <Panel title="关系">
              <div className="profile-relation-row"><span>关注</span><b>{data.is_following ? '已关注' : '未关注'}</b></div>
              <div className="profile-relation-row"><span>互相关注</span><b>{data.is_mutual ? '是（已建私聊）' : '否'}</b></div>
              <div className="profile-relation-row"><span>拉黑</span><b>{data.is_blocked ? '已拉黑' : '未拉黑'}</b></div>
              <div className="profile-relation-row"><span>私信</span><b>{data.can_dm ? POLICY_LABEL[u.dm_policy] : (data.dm_reason || '不可私信')}</b></div>
            </Panel>
          )}
          <Panel title="交流与协作" subtitle="关注研究进展，讨论思路与验证结果">
            <ul className="hint profile-rule-list">
              <li>默认情况下，未建立会话的人只能发一条消息；对方回复后可继续。</li>
              <li>互相关注后会自动建好两人对话，可以直接聊。</li>
              <li>设置为“不允许私信”后，新会话不能发送消息。</li>
              <li>拉黑会解除双方关注，并自动隐藏对方帖子和评论。</li>
            </ul>
          </Panel>
        </aside>
      </div>
    </div>
  )
}
