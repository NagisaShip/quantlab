/**
 * 团队讨论卡片：在 AI 研究室对话里用 /discuss 发起，角色依次发言（气泡展示）、关卡处等你确认、
 * 你可以插话或点名追问，最后由组长汇总；结论可以一键钉进这段对话的上下文。
 * 原来单独的「团队讨论」页面已经并入这里。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { mergeBySeq, missions, type Mission, type Utterance } from '../../apiLab'
import { Avatar } from '../Avatar'
import { QuantEyes } from '../QuantBot'
import { AssistantText, EventLine, UserBubble, Working } from './Blocks'
import { filterMentions, findMention, MentionList, mentionQuery, type MentionOption } from './Composer'

const MODE_LABEL: Record<string, string> = { sequential: '接力', parallel: '并行', debate: '对抗' }
const STATUS: Record<string, string> = {
  queued: '排队中', running: '讨论中', awaiting_approval: '等你确认', done: '已完成', failed: '出错', stopped: '已停止', idle: '待命',
}

/** 一条发言：角色头像 + 名字 + 气泡；质疑与汇总带标记。 */
export function FeedItem({ u, mission }: { u: Utterance; mission: Mission }) {
  if (u.kind === 'system') return <EventLine>{u.content}</EventLine>
  if (u.kind === 'gate') return <EventLine tone="warn">{u.content}</EventLine>
  if (u.kind === 'stage') {
    return <EventLine>阶段 {(u.stage ?? 0) + 1}/{mission.stages.length} · {u.content}{u.data?.mode ? ` · ${MODE_LABEL[u.data.mode] ?? ''}` : ''}</EventLine>
  }
  if (u.kind === 'commander') {
    return <UserBubble>{u.data?.to_name && <span className="cc-at">@{u.data.to_name} </span>}{u.content}</UserBubble>
  }
  const typing = u.status === 'typing'
  const name = u.role_name || mission.roster.find((r) => r.id === u.role_id)?.name || u.role_id || ''
  return (
    <div className={`cc-msg ${u.kind}`}>
      <div className="cc-who">
        <Avatar id={u.role_id} size={22} speaking={typing} />
        <b>{name}</b>
        {u.kind === 'challenge' && <span className="cc-kind">质疑 {(u.data?.reply_to || []).map((n: string) => `@${n}`).join(' ')}</span>}
        {u.kind === 'synthesis' && <span className="cc-kind">汇总</span>}
        {!typing && u.elapsed_ms ? <span className="cc-time">{(u.elapsed_ms / 1000).toFixed(1)}s</span> : null}
      </div>
      {typing ? <Working phase="正在发言" /> : (
        <AssistantText content={u.content.replace(/^【规则降级】/, '')} model={u.model} fallback={u.mode === 'fallback'} />
      )}
    </div>
  )
}

/** 插话 / 追问输入框：输入 @ 弹出团队角色列表，选中后该角色会单独回应。 */
function SayBox({ value, onChange, roster, onSubmit }: {
  value: string; onChange: (v: string) => void; roster: Mission['roster']
  onSubmit: (text: string, roleId: string | null) => void
}) {
  const [active, setActive] = useState(0)
  const ref = useRef<HTMLInputElement>(null)
  const options: MentionOption[] = roster.map((r) => ({ id: r.id, name: r.name, hint: r.title, avatar: <Avatar id={r.id} size={16} /> }))
  const mq = mentionQuery(value)
  const list = mq ? filterMentions(options, mq[1]) : []
  const pick = (o: MentionOption) => { if (mq) onChange(`${value.slice(0, mq[0])}@${o.name} `); setActive(0); ref.current?.focus() }
  const target = findMention(value, options)
  return (
    <form className="disc-say" onSubmit={(e) => {
      e.preventDefault()
      const t = value.trim()
      if (!t) return
      // 点名的角色由气泡前缀显示（@名字），正文里不再重复
      const body = target ? t.replace(`@${target.name}`, '').trim() : t
      onSubmit(body || t, target?.id ?? null)
    }}>
      {mq && list.length > 0 && <MentionList options={options} query={mq[1]} active={Math.min(active, list.length - 1)} onHover={setActive} onPick={pick} />}
      <div className="disc-say-row">
        <button type="button" className="disc-at" title="点名一个角色" aria-label="点名一个角色"
          onClick={() => { onChange(value && !value.endsWith(' ') ? `${value} @` : `${value}@`); ref.current?.focus() }}>@</button>
        <input ref={ref} value={value} onChange={(e) => { onChange(e.target.value); setActive(0) }}
          placeholder="插话或追问团队，输入 @ 选择角色点名"
          onKeyDown={(e) => {
            if (!list.length) return
            if (e.key === 'ArrowDown') { e.preventDefault(); setActive((active + 1) % list.length) }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((active - 1 + list.length) % list.length) }
            else if ((e.key === 'Enter' || e.key === 'Tab') && !e.nativeEvent.isComposing) { e.preventDefault(); pick(list[Math.min(active, list.length - 1)]) }
          }} />
        {target && <span className="disc-target anim-pop"><Avatar id={target.id} size={16} />{target.name} 会回应</span>}
        <button type="submit" className="button small" disabled={!value.trim()}>发送</button>
      </div>
    </form>
  )
}

export function DiscussionCard({ missionId, topic, onPin, notify }: {
  missionId: string; topic: string; onPin: (conclusion: string) => void; notify: (msg: string, error?: boolean) => void
}) {
  const [mission, setMission] = useState<Mission | null>(null)
  const [feed, setFeed] = useState<Utterance[]>([])
  const [open, setOpen] = useState(true)
  const [say, setSay] = useState('')
  const cursor = useRef({ after: 0, since: 0 })
  const gone = useRef(false)

  const pull = useCallback(async () => {
    const v = await missions.view(missionId, cursor.current.after, cursor.current.since)
    setMission(v.mission)
    setFeed((prev) => mergeBySeq(prev, v.feed))
    cursor.current = { after: v.feed.reduce((a, u) => Math.max(a, u.seq), cursor.current.after), since: v.server_time }
    return v.mission
  }, [missionId])

  useEffect(() => {
    gone.current = false
    let t: number | undefined
    const loop = async () => {
      try {
        const m = await pull()
        if (!gone.current && (m.status === 'running' || m.status === 'queued')) t = window.setTimeout(loop, 1200)
      } catch (e) {
        if (!gone.current) setMission(null)
        void e
      }
    }
    loop()
    return () => { gone.current = true; window.clearTimeout(t) }
  }, [pull])

  const act = async (fn: () => Promise<unknown>) => {
    try { await fn(); const m = await pull(); if (m.status === 'running' || m.status === 'queued') window.setTimeout(() => { pull().catch(() => undefined) }, 1200) } catch (e) { notify((e as Error).message, true) }
  }
  // 运行中持续轮询（插话 / 继续之后重新进入运行态）
  useEffect(() => {
    if (!mission || !(mission.status === 'running' || mission.status === 'queued')) return
    const t = window.setInterval(() => { pull().catch(() => undefined) }, 1200)
    return () => window.clearInterval(t)
  }, [mission?.status, pull]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!mission) {
    return <div className="disc-card"><div className="disc-head"><QuantEyes size={16} state="thinking" /><b>团队讨论</b><span>{topic}</span></div></div>
  }
  const running = mission.status === 'running' || mission.status === 'queued'
  const spoken = feed.filter((u) => ['speech', 'challenge', 'synthesis'].includes(u.kind) && u.status === 'done').length
  const lastSynth = [...feed].reverse().find((u) => u.kind === 'synthesis' && u.status === 'done')
  const showConclusion = mission.status === 'done' && !!mission.conclusion && lastSynth?.content.trim() !== mission.conclusion.trim()
  return (
    <div className={`disc-card${running ? ' live' : ''}`}>
      <button type="button" className="disc-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <QuantEyes size={16} state={running ? 'thinking' : mission.status === 'done' ? 'idle' : mission.status === 'failed' ? 'error' : 'confused'} motion={running ? 'full' : 'low'} />
        <b>团队讨论</b>
        <span className="disc-topic">{mission.directive || topic}</span>
        <span className="disc-meta">{STATUS[mission.status] ?? mission.status} · {mission.roster.length} 个角色 · {spoken} 条发言</span>
        <svg className="disc-chev" width="12" height="12" viewBox="0 0 12 12" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="m3 4.5 3 3 3-3" /></svg>
      </button>
      {open && (
        <div className="disc-body anim-expand">
          <div className="disc-roster">
            {mission.roster.map((r) => <span key={r.id} className="disc-role" title={r.title}><Avatar id={r.id} size={18} />{r.name}</span>)}
          </div>
          {feed.map((u) => <FeedItem key={u.seq} u={u} mission={mission} />)}
          {showConclusion && <FeedItem u={{ seq: -1, kind: 'synthesis', role_id: mission.synthesizer_id, content: mission.conclusion, status: 'done', created_at: 0, updated_at: 0 }} mission={mission} />}
          {mission.status === 'awaiting_approval' && (
            <div className="disc-gate">
              <span>这一阶段说完了，继续下一阶段？</span>
              <button className="button small primary" onClick={() => act(() => missions.approve(missionId))}>继续</button>
              <button className="button small" onClick={() => act(() => missions.stop(missionId))}>到此为止</button>
            </div>
          )}
          {running && <Working phase={`${spoken} 条发言`} onStop={() => act(() => missions.stop(missionId))} />}
          <div className="disc-foot">
            {(running || mission.status === 'awaiting_approval' || mission.status === 'done' || mission.status === 'idle' || mission.status === 'stopped') && (
              <SayBox value={say} onChange={setSay} roster={mission.roster}
                onSubmit={(t, roleId) => { setSay(''); act(() => missions.say(missionId, t, roleId)) }} />
            )}
            {mission.status === 'done' && mission.conclusion && (
              <>
                <button className="button small" onClick={() => onPin(mission.conclusion)}>结论钉进上下文</button>
                <button className="link" onClick={() => navigator.clipboard?.writeText(mission.conclusion).then(() => notify('已复制'))}>复制结论</button>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
