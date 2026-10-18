import { useCallback, useEffect, useState } from 'react'
import { v3 } from '../api/research'
import { v4 } from '../api/platform'
import { collab } from '../apiCollab'
import { appConfirm } from '../components/dialog'
import { Callout, EmptyState, Loading, Modal, Panel, Tag } from '../components/ui'
import { AvatarColorPicker, UserAvatar } from '../components/UserAvatar'
import { PostCard } from './Community'
import { copyText, SharePublicButton } from '../components/PublicShare'
import type { PageProps } from './types'

const POLICY_LABEL: Record<'reply_only' | 'open' | 'none', string> = {
  reply_only: '对方回复后可继续发送',
  open: '允许自由私信',
  none: '不允许私信',
}

export default function ProfilePage({ identifier, onNavigate, notify }: PageProps & { identifier: string }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof v3.userProfile>> | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [name, setName] = useState('')
  const [bio, setBio] = useState('')
  const [policy, setPolicy] = useState<'reply_only' | 'open' | 'none'>('reply_only')
  const [avatarColor, setAvatarColor] = useState('#2E5C9A')
  const [publicProfile, setPublicProfile] = useState(false)
  const [rotateOpen, setRotateOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [rotating, setRotating] = useState(false)
  const load = useCallback(() => {
    v3.userProfile(identifier).then((r) => {
      setData(r); setError(''); setName(r.user.name); setBio(r.user.bio); setPolicy(r.user.dm_policy); setAvatarColor(r.user.avatar_color || '#2E5C9A'); setPublicProfile(!!r.user.public_profile)
    }).catch((e: Error) => setError(e.message))
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
  const save = () => act('save', () => v3.saveProfile({ name, bio, dm_policy: policy, avatar_color: avatarColor, public_profile: publicProfile }), '个人资料已保存')

  return (
    <div className="profile-page">
      <section className={`profile-hero${relationLocked ? ' locked' : ''}`}>
        <UserAvatar color={u.avatar_color} size={76} title={u.name} />
        <div className="profile-copy">
          <div className="profile-name"><h1>{u.name}</h1>{u.username && <span>@{u.username}</span>}{u.role === 'admin' && <Tag tone="warn">工作区管理员</Tag>}{data.is_me && <Tag tone="ok">我</Tag>}{u.public_profile && <Tag tone="ok">公开资料</Tag>}</div>
          <button type="button" className="profile-uid" title="复制 QuantLab UID" onClick={() => { copyText(u.quantlab_uid).then(() => notify('QuantLab UID 已复制')).catch(() => undefined) }}><span>QuantLab UID</span><b>{u.quantlab_uid || '—'}</b><i>复制</i></button>
          <p>{u.bio || '还没有简介'}</p>
          <div className="profile-stats">
            <span><b>{u.posts}</b>帖子</span><span><b>{u.followers}</b>关注者</span><span><b>{u.following}</b>正在关注</span>
            <span><b>{u.likes}</b>获赞</span><span><b>{u.clones}</b>被克隆</span>
          </div>
          <div className="profile-dm-note">私信：{POLICY_LABEL[u.dm_policy]}</div>
        </div>
        <div className="profile-actions">
          {!data.is_me && !relationLocked && <button type="button" className={`button${data.is_following ? ' on' : ' primary'}`} disabled={!!busy} onClick={follow}>{data.is_following ? '已关注' : '关注'}</button>}
          {!data.is_me && !relationLocked && <button type="button" className="button" disabled={!!busy || !data.can_dm} title={data.dm_reason || '发起私聊'} onClick={dm}>{data.can_dm ? '私聊' : '不可私信'}</button>}
          {!data.is_me && u.role !== 'admin' && <button type="button" className={`button small${data.is_blocked ? ' danger' : ''}`} disabled={!!busy} onClick={block}>{data.is_blocked ? '取消拉黑' : '拉黑'}</button>}
          {(data.is_me || u.public_profile) && <SharePublicButton type="user" id={u.id} title={u.name} notify={notify} compact />}
          <button type="button" className="button small" onClick={() => onNavigate('square')}>返回广场</button>
        </div>
      </section>

      {data.blocked_me && <Callout tone="warn" title="无法关注或互动">对方已将你拉黑，因此看不到对方的帖子。</Callout>}
      {data.is_blocked && <Callout tone="warn" title="你已拉黑该用户">该用户的帖子会从策略广场自动隐藏。取消拉黑后可恢复。</Callout>}

      <div className="community-layout profile-body">
        <div>
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
            <Panel title="编辑个人资料" subtitle="资料会显示在策略广场和你的主页">
              <label>显示名<input value={name} maxLength={30} onChange={(e) => setName(e.target.value)} /></label>
              <label style={{ marginTop: 8 }}>简介<textarea rows={3} maxLength={500} value={bio} onChange={(e) => setBio(e.target.value)} /></label>
              <div style={{ marginTop: 10 }}><div className="hint">头像颜色</div><AvatarColorPicker value={avatarColor} onChange={setAvatarColor} /></div>
              <label style={{ marginTop: 8 }}>谁能私信我
                <select value={policy} onChange={(e) => setPolicy(e.target.value as typeof policy)}>
                  <option value="reply_only">回复后可继续（默认：陌生人只能发一条）</option>
                  <option value="open">允许自由私信</option>
                  <option value="none">不允许私信</option>
                </select>
              </label>
              <label className="profile-public-toggle">
                <input type="checkbox" checked={publicProfile} onChange={(e) => setPublicProfile(e.target.checked)} />
                <span><b>在公开目录中显示我</b><small>开启后，其他成员可通过显示名、用户名或 QuantLab UID 找到你。关闭不影响你主动分享的主页链接。</small></span>
              </label>
              <div className="profile-id-actions">
                <span><small>QuantLab UID</small><code>{u.quantlab_uid || '—'}</code></span>
                <button type="button" className="button small" onClick={() => { setPassword(''); setRotateOpen(true) }}>销毁并重新生成</button>
              </div>
              <button type="button" className="button small primary" style={{ marginTop: 10 }} disabled={busy === 'save' || !name.trim()} onClick={save}>{busy === 'save' ? '保存中…' : '保存资料'}</button>
            </Panel>
          ) : (
            <Panel title="关系">
              <div className="profile-relation-row"><span>关注</span><b>{data.is_following ? '已关注' : '未关注'}</b></div>
              <div className="profile-relation-row"><span>拉黑</span><b>{data.is_blocked ? '已拉黑' : '未拉黑'}</b></div>
              <div className="profile-relation-row"><span>私信</span><b>{data.can_dm ? POLICY_LABEL[u.dm_policy] : (data.dm_reason || '不可私信')}</b></div>
            </Panel>
          )}
          <Panel title="私信规则" subtitle="服务端会按规则拦截，不能靠前端绕过">
            <ul className="hint profile-rule-list">
              <li>默认情况下，未建立会话的人只能发一条消息；对方回复后可继续。</li>
              <li>设置为“不允许私信”后，新会话不能发送消息。</li>
              <li>拉黑会解除双方关注，并自动隐藏对方帖子和评论。</li>
            </ul>
          </Panel>
        </aside>
      </div>
      {rotateOpen && (
        <Modal title="销毁并重新生成 QuantLab UID" onClose={() => setRotateOpen(false)} width={460}
          footer={<><button type="button" className="button" onClick={() => setRotateOpen(false)}>取消</button><button type="button" className="button danger" disabled={rotating || !password} onClick={async () => {
            setRotating(true)
            try { const r = await v4.rotateQuantlabUid(password); notify(`新 UID：${r.account.quantlab_uid}`); setRotateOpen(false); setPassword(''); load() } catch (e) { notify((e as Error).message, true) } finally { setRotating(false) }
          }}>{rotating ? '正在生成…' : '确认销毁并生成'}</button></>}>
          <p className="hint" style={{ marginTop: 0 }}>旧 UID 会立即失效，其他成员无法再用旧号码搜索到你。你已有的帖子、策略和分享链接不受影响。</p>
          <label>输入当前密码确认<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus /></label>
        </Modal>
      )}
    </div>
  )
}
