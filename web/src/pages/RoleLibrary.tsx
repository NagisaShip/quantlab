/**
 * 角色与团队：角色库、团队编组、最近的团队讨论放在一页。
 * 点任意角色（或头像）打开编辑抽屉；团队成员直接从角色库里选。
 */
import { useCallback, useEffect, useState } from 'react'
import { api, shortTime, type Team } from '../api'
import { missions, type Mission } from '../apiLab'
import { Avatar } from '../components/Avatar'
import { Heading } from '../components/ui'
import { modelLabel } from '../components/chat/Composer'
import { loadRoles, openRoleEditor, putRole, useRoleBook } from '../components/roles'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

const MODE_LABEL: Record<string, string> = { sequential: '接力', parallel: '并行', debate: '对抗' }
const MODE_HELP: Record<string, string> = {
  sequential: '依次发言，后者能看到前者',
  parallel: '各自独立发言，避免相互锚定',
  debate: '先出观点，再由质疑者反驳',
}
const EFFORT_LABEL: Record<string, string> = { low: '低', medium: '中', high: '高', max: '极高' }
const STATUS_LABEL: Record<string, string> = {
  queued: '排队中', running: '讨论中', awaiting_approval: '等你确认', done: '已完成', failed: '出错', stopped: '已停止', idle: '待命',
}

export default function RoleLibraryPage({ notify, onNavigate }: PageProps) {
  const { roles, byId } = useRoleBook()
  const [teams, setTeams] = useState<Team[]>([])
  const [recent, setRecent] = useState<Mission[]>([])
  const [editingTeam, setEditingTeam] = useState<Partial<Team> | null>(null)
  const [draftReq, setDraftReq] = useState('')
  const [drafting, setDrafting] = useState(false)
  const [filter, setFilter] = useState<'all' | 'builtin' | 'custom'>('all')

  const refreshTeams = useCallback(async () => setTeams((await api.teams()).teams), [])
  useEffect(() => {
    loadRoles(true)
    refreshTeams().catch((e: Error) => notify(e.message, true))
    missions.list().then((r) => setRecent(r.missions.slice(0, 6))).catch(() => undefined)
  }, [refreshTeams, notify])

  const newRole = async () => {
    try {
      const r = await api.createRole({ name: '新角色', title: '', system_prompt: '', tools: [], tags: [] })
      putRole(r.role)
      openRoleEditor(r.role.id)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }
  const draft = async () => {
    if (!draftReq.trim()) return
    setDrafting(true)
    try {
      const r = await api.draftRole(draftReq.trim(), true)
      putRole(r.role)
      setDraftReq('')
      openRoleEditor(r.role.id)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setDrafting(false)
    }
  }
  const removeRole = async (id: string) => {
    if (!await appConfirm(`删除角色「${byId[id]?.name}」？`)) return
    try {
      await api.deleteRole(id)
      await loadRoles(true)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const saveTeam = async () => {
    if (!editingTeam?.name?.trim()) return
    try {
      if (editingTeam.id) await api.updateTeam(editingTeam.id, editingTeam)
      else await api.createTeam(editingTeam)
      setEditingTeam(null)
      await refreshTeams()
      notify('团队已保存')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }
  const removeTeam = async (t: Team) => {
    if (!await appConfirm(`删除团队「${t.name}」？`)) return
    try {
      await api.deleteTeam(t.id)
      setEditingTeam(null)
      await refreshTeams()
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const shown = roles.filter((r) => filter === 'all' || (filter === 'builtin' ? r.builtin : !r.builtin))

  return (
    <div className="rl">
      <Heading title="角色与团队" actions={<a className="button" href="#/lab" title="在对话里输入 /discuss 发起团队讨论">在研究室发起讨论</a>} />

      <section className="rl-section">
        <div className="rl-section-head">
          <h2>角色</h2>
          <div className="rl-seg">
            {(['all', 'builtin', 'custom'] as const).map((f) => (
              <button key={f} className={filter === f ? 'on' : ''} onClick={() => setFilter(f)}>{{ all: '全部', builtin: '内置', custom: '自定义' }[f]}</button>
            ))}
          </div>
          <span style={{ flex: 1 }} />
          <button className="button small" onClick={newRole}>新建角色</button>
        </div>
        <div className="rl-draft">
          <input value={draftReq} onChange={(e) => setDraftReq(e.target.value)} placeholder="用一句话描述想要的角色，AI 帮你起草，例如：专盯资金费率与基差的衍生品分析师"
            onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) draft() }} />
          <button className="button small" disabled={!draftReq.trim() || drafting} onClick={draft}>{drafting ? '起草中…' : 'AI 起草'}</button>
        </div>
        <div className="rl-grid">
          {shown.map((r) => (
            <div key={r.id} className="rl-role" role="button" tabIndex={0} onClick={() => openRoleEditor(r.id)}
              onKeyDown={(e) => { if (e.key === 'Enter') openRoleEditor(r.id) }}>
              <Avatar id={r.id} size={36} editable={false} />
              <div className="rl-role-text">
                <div className="rl-role-name">
                  <b>{r.name}</b>
                  {r.builtin ? <span className="rl-badge">{r.customized ? '内置 · 已修改' : '内置'}</span> : r.source === 'ai_draft' ? <span className="rl-badge">AI 起草</span> : null}
                </div>
                <div className="rl-role-title">{r.title || r.description || '—'}</div>
                <div className="rl-role-meta">
                  {modelLabel(r.model)}{r.effort ? ` · 思考${EFFORT_LABEL[r.effort] ?? r.effort}` : ''}{r.tools.length ? ` · ${r.tools.length} 个工具` : ''}
                </div>
              </div>
              {!r.builtin && (
                <button className="rl-x" title="删除角色" aria-label="删除角色" onClick={(e) => { e.stopPropagation(); removeRole(r.id) }}>×</button>
              )}
            </div>
          ))}
        </div>
      </section>

      <section className="rl-section">
        <div className="rl-section-head">
          <h2>团队</h2>
          <span className="rl-hint">单周期研究直接用这些团队；在 AI 研究室对话里用 /discuss 发起讨论</span>
          <span style={{ flex: 1 }} />
          <button className="button small" onClick={() => setEditingTeam({ name: '', description: '', mode: 'sequential', role_ids: [], synthesizer_id: 'research_lead', challenge_rounds: 1 })}>新建团队</button>
        </div>
        {editingTeam && !editingTeam.id && <TeamEditor team={editingTeam} onChange={setEditingTeam} onSave={saveTeam} onCancel={() => setEditingTeam(null)} />}
        <div className="rl-teams">
          {teams.map((t) => editingTeam?.id === t.id ? (
            <TeamEditor key={t.id} team={editingTeam} onChange={setEditingTeam} onSave={saveTeam} onCancel={() => setEditingTeam(null)} onDelete={() => removeTeam(t)} />
          ) : (
            <div key={t.id} className="rl-team">
              <div className="rl-team-avatars">
                {t.role_ids.map((r) => <Avatar key={r} id={r} size={26} />)}
                {t.synthesizer_id && <><span className="rl-arrow">→</span><Avatar id={t.synthesizer_id} size={26} /></>}
              </div>
              <div className="rl-team-text">
                <b>{t.name}</b>
                <span>{MODE_LABEL[t.mode] ?? t.mode} · {t.role_ids.length} 人{t.synthesizer_id ? ` · ${byId[t.synthesizer_id]?.name ?? t.synthesizer_id}汇总` : ''}{t.description ? ` · ${t.description}` : ''}</span>
              </div>
              <button className="button small" onClick={() => setEditingTeam({ ...t, role_ids: [...t.role_ids] })}>编辑</button>
            </div>
          ))}
        </div>
      </section>

      {recent.length > 0 && (
        <section className="rl-section">
          <div className="rl-section-head"><h2>最近的团队讨论</h2></div>
          <div className="rl-recent">
            {recent.map((m) => (
              <button key={m.id} className="rl-recent-item" onClick={() => onNavigate(`teams/${m.id}`)}>
                <span className="rl-recent-title">{m.directive}</span>
                <span className="rl-recent-meta">{m.mode === 'pipeline' ? '流水线' : '单周期'} · {STATUS_LABEL[m.status] ?? m.status} · {shortTime(m.created_at * 1000)}</span>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}

function TeamEditor({ team, onChange, onSave, onCancel, onDelete }: {
  team: Partial<Team>; onChange: (t: Partial<Team>) => void; onSave: () => void; onCancel: () => void; onDelete?: () => void
}) {
  const { roles, byId } = useRoleBook()
  const ids = team.role_ids ?? []
  const toggle = (id: string) => onChange({ ...team, role_ids: ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id] })
  return (
    <div className="rl-team-edit">
      <div className="rl-team-row">
        <input className="rl-input" value={team.name ?? ''} placeholder="团队名称" onChange={(e) => onChange({ ...team, name: e.target.value })} />
        <select className="rl-input" value={team.mode} onChange={(e) => onChange({ ...team, mode: e.target.value as Team['mode'] })}>
          {Object.keys(MODE_LABEL).map((m) => <option key={m} value={m}>{MODE_LABEL[m]}：{MODE_HELP[m]}</option>)}
        </select>
      </div>
      <input className="rl-input" value={team.description ?? ''} placeholder="一句话说明这支团队适合什么问题" onChange={(e) => onChange({ ...team, description: e.target.value })} />
      <div className="rl-label">成员（按发言顺序）</div>
      <div className="rl-members">
        {ids.map((id) => (
          <span key={id} className="cc-role"><Avatar id={id} size={22} /><span>{byId[id]?.name ?? id}</span>
            <button aria-label="移出" onClick={() => toggle(id)}>×</button></span>
        ))}
        <select className="cc-add" value="" onChange={(e) => e.target.value && toggle(e.target.value)} aria-label="添加成员">
          <option value="">＋ 从角色库添加</option>
          {roles.filter((r) => !ids.includes(r.id)).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
      </div>
      <div className="rl-team-row">
        <label className="cc-synth">汇总
          <select value={team.synthesizer_id ?? ''} onChange={(e) => onChange({ ...team, synthesizer_id: e.target.value })}>
            <option value="">不汇总</option>
            {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </label>
        {team.mode === 'debate' && (
          <label className="cc-synth">质疑轮数
            <select value={team.challenge_rounds ?? 1} onChange={(e) => onChange({ ...team, challenge_rounds: Number(e.target.value) })}>
              {[1, 2, 3].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
        )}
        <span style={{ flex: 1 }} />
        {onDelete && <button className="button small" onClick={onDelete}>删除</button>}
        <button className="button small" onClick={onCancel}>取消</button>
        <button className="button small primary" disabled={!team.name?.trim() || !ids.length} onClick={onSave}>保存</button>
      </div>
    </div>
  )
}
