/**
 * 团队讨论（指挥台）：和策略室一样的对话布局。
 * 左侧是讨论记录；中间写下研究问题、选阵容（单周期团队或多阶段流水线），角色依次发言；
 * 关卡处停下等你确认，你可以随时插话或点名追问；最后由组长汇总，可一键交给研究室。
 * 阵容里的角色都来自角色库，点头像即可修改它的模型、努力程度和提示词。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, type Template } from '../api'
import {
  lab, mergeBySeq, missions,
  type LabProjectBrief, type Mission, type PipelinePreset, type Stage, type TeamLite, type Utterance,
} from '../apiLab'
import { Avatar } from '../components/Avatar'
import { AssistantText, EventLine, UserBubble, Working } from '../components/chat/Blocks'
import { Composer, usePopover } from '../components/chat/Composer'
import { useRoleBook } from '../components/roles'
import { store } from '../components/prefs'
import type { PageProps } from './types'

const MODE_LABEL: Record<string, string> = { sequential: '接力', parallel: '并行', debate: '对抗' }
const STATUS_LABEL: Record<string, string> = {
  queued: '排队中', running: '讨论中', awaiting_approval: '等你确认', done: '已完成', failed: '出错', stopped: '已停止', idle: '待命',
}
const EXAMPLES = [
  '评估 BTC/USDT 1 小时趋势策略是否值得继续投入',
  '为 ETH 设计一个适合当前行情的策略，并说清最大风险',
  '审查研究室里的策略：回测可信吗？上线前还缺什么？',
]

type Lineup = { kind: 'pipeline'; id: string } | { kind: 'team'; id: string }

export default function CommandCenterPage({ onNavigate, notify, arg }: PageProps & { arg?: string }) {
  const [history, setHistory] = useState<Mission[]>([])
  const [activeId, setActiveId] = useState(arg || '')
  const [railOpen, setRailOpen] = useState(() => store.get('qa.cc.rail') !== '0')
  const [filter, setFilter] = useState('')

  const refreshHistory = useCallback(async () => setHistory((await missions.list()).missions), [])
  useEffect(() => { refreshHistory().catch(() => undefined) }, [refreshHistory])
  useEffect(() => { if (arg) setActiveId(arg) }, [arg])

  const open = (id: string) => {
    setActiveId(id)
    window.history.replaceState(null, '', id ? `#/teams/${id}` : '#/teams')
  }
  const toggleRail = () => { const v = !railOpen; setRailOpen(v); store.set('qa.cc.rail', v ? '1' : '0') }
  const q = filter.trim()
  const shown = history.filter((h) => !q || h.directive.includes(q))

  return (
    <div className={`lab2${railOpen ? '' : ' rail-closed'}`}>
      <aside className="lab2-rail" aria-label="讨论记录">
        <div className="lab2-rail-top">
          <button className="lab2-new" onClick={() => open('')}>
            <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
            新讨论
          </button>
          <button className="lab2-icon" onClick={toggleRail} title="收起列表" aria-label="收起列表"><PanelIcon /></button>
        </div>
        <input className="lab2-search" placeholder="搜索" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="搜索讨论" />
        <div className="lab2-list">
          {shown.length === 0 && <div className="lab2-list-empty">{q ? '没有匹配的讨论' : '还没有讨论'}</div>}
          {shown.map((h) => (
            <button key={h.id} className={`lab2-item${h.id === activeId ? ' on' : ''}`} onClick={() => open(h.id)} title={h.directive}>
              <span className="lab2-item-title">{['running', 'awaiting_approval'].includes(h.status) && <i className={`dot ${h.status}`} />}{h.directive}</span>
              <span className="lab2-item-meta">{h.mode === 'pipeline' ? '流水线' : '单周期'} · {STATUS_LABEL[h.status] ?? h.status}</span>
            </button>
          ))}
        </div>
        <a href="#/roles" className="lab2-rail-foot cc-foot">角色库与团队</a>
      </aside>

      {activeId ? (
        <MissionView key={activeId} id={activeId} notify={notify} onNavigate={onNavigate} railOpen={railOpen} onToggleRail={toggleRail}
          onChanged={refreshHistory} onDeleted={() => { open(''); refreshHistory() }} />
      ) : (
        <NewMission notify={notify} railOpen={railOpen} onToggleRail={toggleRail}
          onCreated={(id) => { refreshHistory(); open(id) }} />
      )}
    </div>
  )
}

function PanelIcon() {
  return <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden><rect x="2" y="3" width="12" height="10" rx="2" fill="none" stroke="currentColor" strokeWidth="1.3" /><path d="M6 3v10" stroke="currentColor" strokeWidth="1.3" /></svg>
}
function RailToggle({ show, onClick }: { show: boolean; onClick: () => void }) {
  if (!show) return null
  return <button className="lab2-icon" onClick={onClick} title="展开列表" aria-label="展开列表"><PanelIcon /></button>
}

/* ------------------------------------------------------------------ */
/* 新讨论：问题 + 阵容 + 材料                                            */
/* ------------------------------------------------------------------ */
function NewMission({ notify, onCreated, railOpen, onToggleRail }: {
  notify: PageProps['notify']; onCreated: (id: string) => void; railOpen: boolean; onToggleRail: () => void
}) {
  const [pipelines, setPipelines] = useState<PipelinePreset[]>([])
  const [teams, setTeams] = useState<TeamLite[]>([])
  const [templates, setTemplates] = useState<Template[]>([])
  const [labProjects, setLabProjects] = useState<LabProjectBrief[]>([])
  const [directive, setDirective] = useState('')
  const [lineup, setLineup] = useState<Lineup>({ kind: 'pipeline', id: 'full' })
  const [stages, setStages] = useState<Stage[]>([])
  const [synth, setSynth] = useState('research_lead')
  const [editing, setEditing] = useState(false)
  const [symbol, setSymbol] = useState('BTC/USDT')
  const [timeframe, setTimeframe] = useState('1h')
  const [includeMarket, setIncludeMarket] = useState(true)
  const [strategy, setStrategy] = useState('')
  const [labProject, setLabProject] = useState('')
  const [launching, setLaunching] = useState(false)

  const loadPresets = useCallback(async () => {
    const p = await missions.presets()
    setPipelines(p.pipelines)
    setTeams(p.teams)
    return p
  }, [])

  useEffect(() => {
    loadPresets().then((p) => {
      const full = p.pipelines.find((x) => x.id === 'full') ?? p.pipelines[0]
      if (full) { setStages(clone(full.stages)); setSynth(full.synthesizer_id) }
    }).catch((e: Error) => notify(e.message, true))
    api.templates().then((t) => setTemplates(t.templates)).catch(() => undefined)
    lab.projects().then((r) => setLabProjects(r.projects)).catch(() => undefined)
  }, [notify, loadPresets])

  const pick = (l: Lineup) => {
    setLineup(l)
    if (l.kind === 'pipeline') {
      const p = pipelines.find((x) => x.id === l.id)
      if (p) { setStages(clone(p.stages)); setSynth(p.synthesizer_id) }
    } else {
      const t = teams.find((x) => x.id === l.id)
      if (t) { setStages([{ key: 'cycle', name: t.name, mode: t.mode as Stage['mode'], role_ids: [...t.role_ids], gate: false }]); setSynth(t.synthesizer_id) }
    }
  }

  const preset = lineup.kind === 'pipeline' ? pipelines.find((p) => p.id === lineup.id) : undefined
  const team = lineup.kind === 'team' ? teams.find((t) => t.id === lineup.id) : undefined
  const lineupName = preset?.name ?? team?.name ?? '阵容'
  const changed = preset ? JSON.stringify(stages.map(strip)) !== JSON.stringify(preset.stages.map(strip)) || synth !== preset.synthesizer_id : editing

  const launch = async () => {
    if (directive.trim().length < 2) return
    setLaunching(true)
    try {
      const custom = lineup.kind === 'team' && changed
      const v = await missions.create({
        directive: directive.trim(),
        mode: lineup.kind === 'pipeline' || custom ? 'pipeline' : 'single',
        team_id: lineup.kind === 'team' && !custom ? lineup.id : null,
        preset: lineup.kind === 'pipeline' ? lineup.id : null,
        stages: lineup.kind === 'pipeline' || custom ? stages.filter((s) => s.role_ids.length) : null,
        ...(lineup.kind === 'pipeline' || custom ? { synthesizer_id: synth } : {}),
        symbol, timeframe, include_market_data: includeMarket, strategy: strategy || null, lab_project_id: labProject || null,
      })
      onCreated(v.mission.id)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setLaunching(false)
    }
  }

  const saveTemplate = async () => {
    const name = window.prompt('模板名称', preset?.custom ? preset.name : `${lineupName}（自定义）`)
    if (!name?.trim()) return
    try {
      const r = await missions.savePipeline({ id: preset?.custom ? preset.id : undefined, name: name.trim(), stages: stages.filter((s) => s.role_ids.length), synthesizer_id: synth })
      await loadPresets()
      setLineup({ kind: 'pipeline', id: r.pipeline.id })
      notify('已存为模板')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }
  const deleteTemplate = async () => {
    if (!preset?.custom || !window.confirm(`删除模板「${preset.name}」？`)) return
    try {
      await missions.deletePipeline(preset.id)
      const p = await loadPresets()
      const full = p.pipelines.find((x) => x.id === 'full')
      setLineup({ kind: 'pipeline', id: 'full' })
      if (full) { setStages(clone(full.stages)); setSynth(full.synthesizer_id) }
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const people = [...new Set([...stages.flatMap((s) => s.role_ids), synth].filter(Boolean))]

  return (
    <div className="lab2-main">
      <div className="lab2-head"><RailToggle show={!railOpen} onClick={onToggleRail} /></div>
      <div className="lab2-welcome">
        <h1>要让团队讨论什么？</h1>
        <Composer value={directive} onChange={setDirective} onSend={launch} running={launching} autoFocus
          placeholder="写下研究问题，例如：评估 BTC 1 小时趋势策略是否值得继续投入" sendLabel="开始"
          left={<LineupMenu pipelines={pipelines} teams={teams} value={lineup} onPick={pick} name={lineupName} changed={changed} />}
          right={<button type="button" className={`cmp-chip${editing ? ' on' : ''}`} onClick={() => setEditing(!editing)}>调整阵容</button>} />

        <div className="cc-lineup">
          <div className="cc-people">
            {people.map((r) => <Avatar key={r} id={r} size={24} />)}
            <span className="cc-people-text">{describeStages(stages)}</span>
          </div>
          <div className="cc-materials">
            <select className="cc-pill" value={symbol} onChange={(e) => setSymbol(e.target.value)} aria-label="标的">
              {['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT'].map((s) => <option key={s}>{s}</option>)}</select>
            <select className="cc-pill" value={timeframe} onChange={(e) => setTimeframe(e.target.value)} aria-label="周期">
              {['15m', '1h', '4h', '1d'].map((s) => <option key={s}>{s}</option>)}</select>
            <select className="cc-pill" value={strategy} onChange={(e) => setStrategy(e.target.value)} aria-label="附带回测">
              <option value="">不附带回测</option>{templates.map((t) => <option key={t.name} value={t.name}>回测：{t.label}</option>)}</select>
            <select className="cc-pill" value={labProject} onChange={(e) => setLabProject(e.target.value)} aria-label="附带研究室项目">
              <option value="">不附带研究室项目</option>{labProjects.map((p) => <option key={p.id} value={p.id}>{p.title}（v{p.current_v}）</option>)}</select>
            <label className="cc-pill check"><input type="checkbox" checked={includeMarket} onChange={(e) => setIncludeMarket(e.target.checked)} />行情概览</label>
          </div>
        </div>

        {editing && (
          <RosterEditor stages={stages} onStages={setStages} synth={synth} onSynth={setSynth}
            onSave={saveTemplate} onDelete={preset?.custom ? deleteTemplate : undefined} />
        )}

        {!editing && (
          <div className="lab2-examples">
            {EXAMPLES.map((d) => <button key={d} className="lab2-example" onClick={() => setDirective(d)}>{d}</button>)}
          </div>
        )}
      </div>
    </div>
  )
}

const clone = (s: Stage[]) => s.map((x) => ({ ...x, role_ids: [...x.role_ids] }))
const strip = (s: Stage) => ({ name: s.name, mode: s.mode, role_ids: s.role_ids, gate: !!s.gate })

function describeStages(stages: Stage[]) {
  const live = stages.filter((s) => s.role_ids.length)
  if (live.length <= 1) return live[0] ? `${MODE_LABEL[live[0].mode]}讨论 · ${live[0].role_ids.length} 人，组长汇总` : '还没有选人'
  return `${live.length} 个阶段${live.some((s) => s.gate) ? '，关卡处等你确认' : ''}，组长汇总`
}

function LineupMenu({ pipelines, teams, value, onPick, name, changed }: {
  pipelines: PipelinePreset[]; teams: TeamLite[]; value: Lineup; onPick: (l: Lineup) => void; name: string; changed: boolean
}) {
  const pop = usePopover()
  const item = (l: Lineup, title: string, sub: string) => {
    const on = value.kind === l.kind && value.id === l.id
    return (
      <button key={`${l.kind}:${l.id}`} type="button" role="menuitemradio" aria-checked={on} className={`cmp-menu-item${on ? ' on' : ''}`}
        onClick={() => { onPick(l); pop.setOpen(false) }}>
        <span className="cmp-menu-main">{title}</span><span className="cmp-menu-sub">{sub}</span>
      </button>
    )
  }
  const builtin = pipelines.filter((p) => !p.custom)
  const saved = pipelines.filter((p) => p.custom)
  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button type="button" className="cmp-model" onClick={() => pop.setOpen(!pop.open)} aria-haspopup="menu" aria-expanded={pop.open} title="选择阵容">
        <span>{name}{changed ? '（已调整）' : ''}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
      </button>
      {pop.open && (
        <div className="cmp-menu cmp-menu-model cc-lineup-menu" role="menu">
          <div className="cmp-menu-label">流水线 · 多阶段接力</div>
          {builtin.map((p) => item({ kind: 'pipeline', id: p.id }, p.name, `${p.stages.length} 个阶段`))}
          {saved.length > 0 && <div className="cmp-menu-label">我的模板</div>}
          {saved.map((p) => item({ kind: 'pipeline', id: p.id }, p.name, `${p.stages.length} 个阶段`))}
          <div className="cmp-menu-sep" />
          <div className="cmp-menu-label">单周期 · 一支团队讨论一轮</div>
          {teams.map((t) => item({ kind: 'team', id: t.id }, t.name, `${MODE_LABEL[t.mode] ?? t.mode} · ${t.role_ids.length + (t.synthesizer_id ? 1 : 0)} 人`))}
          <div className="cmp-menu-sep" />
          <a className="cmp-menu-item" href="#/roles"><span className="cmp-menu-main">管理角色与团队…</span></a>
        </div>
      )}
    </div>
  )
}

function RosterEditor({ stages, onStages, synth, onSynth, onSave, onDelete }: {
  stages: Stage[]; onStages: (s: Stage[]) => void; synth: string; onSynth: (id: string) => void; onSave: () => void; onDelete?: () => void
}) {
  const { roles, byId } = useRoleBook()
  const patch = (i: number, p: Partial<Stage>) => onStages(stages.map((s, j) => (j === i ? { ...s, ...p } : s)))
  return (
    <div className="cc-roster">
      {stages.map((s, i) => (
        <div key={s.key + i} className="cc-stage">
          <div className="cc-stage-head">
            <span className="cc-stage-no">{i + 1}</span>
            <input className="cc-stage-name" value={s.name} onChange={(e) => patch(i, { name: e.target.value })} aria-label="阶段名称" />
            <select value={s.mode} onChange={(e) => patch(i, { mode: e.target.value as Stage['mode'] })} aria-label="发言方式">
              <option value="sequential">接力：后者看到前者</option>
              <option value="parallel">并行：各自独立</option>
              <option value="debate">对抗：先观点再质疑</option>
            </select>
            {stages.length > 1 && (
              <button className="lab2-icon" title="删除阶段" aria-label="删除阶段" onClick={() => onStages(stages.filter((_, j) => j !== i))}>
                <svg width="12" height="12" viewBox="0 0 14 14" aria-hidden><path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
              </button>
            )}
          </div>
          <div className="cc-stage-roles">
            {s.role_ids.map((r) => (
              <span key={r} className="cc-role">
                <Avatar id={r} size={22} /><span>{byId[r]?.name ?? r}</span>
                <button title="移出" aria-label={`移出${byId[r]?.name ?? r}`} onClick={() => patch(i, { role_ids: s.role_ids.filter((x) => x !== r) })}>×</button>
              </span>
            ))}
            <select className="cc-add" value="" onChange={(e) => e.target.value && patch(i, { role_ids: [...s.role_ids, e.target.value] })} aria-label="添加角色">
              <option value="">＋ 添加角色</option>
              {roles.filter((r) => !s.role_ids.includes(r.id)).map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
          </div>
          {i < stages.length - 1 && (
            <label className="cc-gate"><input type="checkbox" checked={!!s.gate} onChange={(e) => patch(i, { gate: e.target.checked })} />这一阶段结束后等我确认再继续</label>
          )}
        </div>
      ))}
      <div className="cc-roster-foot">
        <button className="button small" onClick={() => onStages([...stages, { key: `s${Date.now().toString(36)}`, name: `阶段 ${stages.length + 1}`, mode: 'sequential', role_ids: [], gate: false }])}>＋ 添加阶段</button>
        <label className="cc-synth">汇总
          <select value={synth} onChange={(e) => onSynth(e.target.value)}>
            {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
          </select>
        </label>
        <span style={{ flex: 1 }} />
        {onDelete && <button className="button small" onClick={onDelete}>删除模板</button>}
        <button className="button small" onClick={onSave}>存为模板</button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 讨论                                                                */
/* ------------------------------------------------------------------ */
function MissionView({ id, notify, onNavigate, onChanged, onDeleted, railOpen, onToggleRail }: {
  id: string; notify: PageProps['notify']; onNavigate: PageProps['onNavigate']; onChanged: () => void; onDeleted: () => void
  railOpen: boolean; onToggleRail: () => void
}) {
  const { byId } = useRoleBook()
  const [mission, setMission] = useState<Mission | null>(null)
  const [feed, setFeed] = useState<Utterance[]>([])
  const [text, setText] = useState('')
  const [target, setTarget] = useState('')
  const [replay, setReplay] = useState<number | null>(null)
  const cursor = useRef({ after: 0, since: 0 })
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

  const pull = useCallback(async (full = false) => {
    const c = full ? { after: 0, since: 0 } : cursor.current
    const v = await missions.view(id, c.after, c.since)
    setMission(v.mission)
    setFeed((prev) => (full ? v.feed : mergeBySeq(prev, v.feed)))
    cursor.current = { after: v.feed.reduce((a, u) => Math.max(a, u.seq), c.after), since: v.server_time }
  }, [id])

  useEffect(() => { pull(true).catch((e: Error) => notify(e.message, true)) }, [pull, notify])
  const running = mission?.status === 'running' || mission?.status === 'queued'
  useEffect(() => {
    if (!running) return
    const t = window.setInterval(() => { pull().catch(() => undefined) }, 1000)
    return () => window.clearInterval(t)
  }, [running, pull])
  const was = useRef(false)
  useEffect(() => { if (was.current && !running) onChanged(); was.current = running }, [running, onChanged])

  useEffect(() => {
    if (replay === null) return
    if (replay >= feed.length) { setReplay(null); return }
    const t = window.setTimeout(() => setReplay(replay + 1), 900)
    return () => window.clearTimeout(t)
  }, [replay, feed.length])

  const visible = replay === null ? feed : feed.slice(0, replay)
  useEffect(() => {
    const el = scroller.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [visible.length, feed])

  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); await pull() } catch (e) { notify((e as Error).message, true) }
  }

  const stats = useMemo(() => {
    const spoken = feed.filter((u) => ['speech', 'challenge', 'synthesis'].includes(u.kind) && u.status === 'done')
    return { count: spoken.length, fallback: spoken.some((u) => u.mode === 'fallback') }
  }, [feed])

  if (!mission) return <div className="lab2-main"><div className="lab2-empty">正在读取讨论…</div></div>

  const nextStage = mission.stages[mission.cursor]
  const done = mission.status === 'done' && replay === null
  const lastSynth = [...visible].reverse().find((u) => u.kind === 'synthesis' && u.status === 'done')
  const showConclusionAlone = done && !!mission.conclusion && lastSynth?.content.trim() !== mission.conclusion.trim()

  const handoff = async () => {
    try {
      const v = await lab.create({
        title: mission.directive.slice(0, 24), idea: mission.directive, securities: [mission.symbol], timeframe: mission.timeframe,
        pins: [{ kind: 'team', title: '团队讨论结论', text: mission.conclusion.slice(0, 6000) }],
      })
      await lab.autopilot(v.project.id, { research: 'none' })
      onNavigate(`lab/${v.project.id}`)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }
  const conclusionActions = (
    <span className="cc-actions">
      <button className="link" onClick={handoff}>交给研究室</button>
      <button className="link" onClick={() => navigator.clipboard?.writeText(mission.conclusion).then(() => notify('已复制'))}>复制</button>
    </span>
  )
  const send = () => {
    const t = text.trim()
    if (!t) return
    setText('')
    act(() => missions.say(id, t, target))
  }
  const remove = async () => {
    if (!window.confirm('删除这次讨论？')) return
    try { await missions.remove(id); onDeleted() } catch (e) { notify((e as Error).message, true) }
  }

  return (
    <div className="lab2-main">
      <div className="lab2-head">
        <RailToggle show={!railOpen} onClick={onToggleRail} />
        <span className="cc-title" title={mission.directive}>{mission.directive}</span>
        <span className="lab2-sub">{mission.mode === 'pipeline' ? '流水线' : '单周期'} · {mission.symbol} {mission.timeframe} · {STATUS_LABEL[mission.status] ?? mission.status}</span>
        <span className="lab2-head-gap" />
        <span className="cc-roster-mini">{mission.roster.slice(0, 8).map((r) => <Avatar key={r.id} id={r.id} size={22} />)}</span>
        {!running && feed.length > 3 && <button className="cmp-chip" onClick={() => { stick.current = true; setReplay(1) }}>回放</button>}
        <button className="lab2-icon" title="删除讨论" aria-label="删除讨论" onClick={remove}>
          <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 8.5h5.6l.7-8.5" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" /></svg>
        </button>
      </div>

      <div className="lab2-scroll" ref={scroller}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }}>
        <div className="lab2-thread">
          <UserBubble>{mission.directive}</UserBubble>
          {stats.fallback && (
            <EventLine tone="warn">部分发言为规则降级（没有可用模型）· <a href="#/models">配置模型</a></EventLine>
          )}
          {visible.map((u) => (
            <FeedItem key={u.seq} u={u} mission={mission} roleName={(rid) => byId[rid]?.name ?? rid}
              footer={done && u === lastSynth && mission.conclusion ? conclusionActions : undefined} />
          ))}
          {showConclusionAlone && (
            <div className="cc-msg">
              <div className="cc-who"><Avatar id={mission.synthesizer_id} size={22} /><b>{byId[mission.synthesizer_id]?.name ?? '汇总'}</b><span className="cc-kind">结论</span></div>
              <AssistantText content={mission.conclusion} footer={conclusionActions} />
            </div>
          )}
          {mission.status === 'awaiting_approval' && replay === null && nextStage && (
            <div className="cc-gate-card">
              <div>下一阶段是<b>「{nextStage.name}」</b>，由 {nextStage.role_ids.map((r) => byId[r]?.name ?? r).join('、')} 发言。可以先在下方补充要求，再继续。</div>
              <div className="cc-gate-actions">
                <button className="button small primary" onClick={() => act(() => missions.approve(id))}>继续</button>
                <button className="button small" onClick={() => act(() => missions.stop(id))}>到此为止</button>
              </div>
            </div>
          )}
          {(mission.status === 'stopped' || mission.status === 'failed') && replay === null && mission.cursor < mission.stages.length && (
            <div className="cc-gate-card">
              <div>{mission.status === 'failed' ? '讨论出错停下了。' : '讨论已停止。'}可以从「{nextStage?.name ?? '下一阶段'}」继续。</div>
              <div className="cc-gate-actions"><button className="button small" onClick={() => act(() => missions.approve(id))}>继续讨论</button></div>
            </div>
          )}
        </div>
      </div>

      <div className="lab2-dock">
        {running && <Working phase={`${stats.count} 条发言`} onStop={() => act(() => missions.stop(id))} />}
        <Composer value={text} onChange={setText} onSend={send}
          disabled={running && !!target}
          placeholder={target ? `追问 ${byId[target]?.name ?? ''}，对方会单独回应` : '插话：之后发言的角色都会看到'}
          left={
            <select className="cc-pill cc-target" value={target} onChange={(e) => setTarget(e.target.value)} aria-label="对谁说">
              <option value="">对全体</option>
              {mission.roster.map((r) => <option key={r.id} value={r.id}>@{r.name}</option>)}
            </select>
          } />
      </div>
    </div>
  )
}

function FeedItem({ u, mission, roleName, footer }: {
  u: Utterance; mission: Mission; roleName: (id: string) => string; footer?: ReactNode
}) {
  if (u.kind === 'system') return <EventLine>{u.content}</EventLine>
  if (u.kind === 'gate') return <EventLine tone="warn">{u.content}</EventLine>
  if (u.kind === 'stage') {
    return <EventLine>阶段 {(u.stage ?? 0) + 1}/{mission.stages.length} · {u.content}{u.data?.mode ? ` · ${MODE_LABEL[u.data.mode] ?? ''}` : ''}</EventLine>
  }
  if (u.kind === 'commander') {
    return <UserBubble>{u.data?.to_name && <span className="cc-at">@{u.data.to_name} </span>}{u.content}</UserBubble>
  }
  const typing = u.status === 'typing'
  return (
    <div className={`cc-msg ${u.kind}`}>
      <div className="cc-who">
        <Avatar id={u.role_id} size={22} speaking={typing} />
        <b>{u.role_name || roleName(u.role_id || '')}</b>
        {u.kind === 'challenge' && <span className="cc-kind">质疑 {(u.data?.reply_to || []).map((n: string) => `@${n}`).join(' ')}</span>}
        {u.kind === 'synthesis' && <span className="cc-kind">汇总</span>}
        {!typing && u.elapsed_ms ? <span className="cc-time">{(u.elapsed_ms / 1000).toFixed(1)}s</span> : null}
      </div>
      {typing ? <Working phase="正在发言" /> : (
        <AssistantText content={u.content.replace(/^【规则降级】/, '')} model={u.model} fallback={u.mode === 'fallback'} footer={footer} />
      )}
    </div>
  )
}
