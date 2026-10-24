/**
 * P4 · Agent 任务五态与运行证据（纯函数，不依赖 React）。
 *
 * 唯一数据源是后端真实字段，绝不从自然语言里猜「完成 / 失败」：
 * - `LabProject.status` / `job`：回合在跑、当前阶段、启动时间；
 * - 消息 `data.streaming`：模型是否还在流式输出；
 * - 工具卡片 `data.status`（running / ok / error / stopped）；
 * - `workflow_view` / `task_view` 的 state（QUEUED / PENDING / RUNNING / COMPLETED / FAILED / CANCELED）；
 * - 回测卡片的 `bt_id`、报告卡片的 `id` / `run_id`、`artifact_view.artifact_hash`；
 * - 结构化内容块（ql:*）里的 `source.run_id` / `source.backtest_id` / `#/backtest/<id>`。
 *
 * 五态：等待输入 / 排队中 / 生成中 / 已生成 / 执行失败。
 * 严格约束：没有 run / 工件证据的纯文字回复只能显示「已回复」，不显示「已生成」。
 */
import type { LabMessage, LabProject, LabProjectBrief } from '../../apiLab'

export type AgentState = 'waiting' | 'queued' | 'generating' | 'generated' | 'failed'

/** 五态文案。`generated` 的展示文案由证据决定：有 run/工件 → 已生成，纯文字 → 已回复。 */
export const AGENT_STATE_LABEL: Record<AgentState, string> = {
  waiting: '等待输入',
  queued: '排队中',
  generating: '生成中',
  generated: '已生成',
  failed: '执行失败',
}

export type RunEvidenceKind = 'backtest' | 'run' | 'workflow' | 'task' | 'report' | 'artifact' | 'team'
export type RunEvidenceState = 'queued' | 'running' | 'done' | 'failed' | 'unknown' | 'waiting' | 'canceling' | 'canceled'

export const RUN_EVIDENCE_LABEL: Record<RunEvidenceKind, string> = {
  backtest: '回测',
  run: '运行',
  workflow: '工作流运行',
  task: '任务',
  report: '报告',
  artifact: '工件',
  team: '评审',
}

export const RUN_STATE_LABEL: Record<RunEvidenceState, string> = {
  queued: '排队中',
  running: '运行中',
  done: '已完成',
  failed: '失败',
  unknown: '未确认',
  waiting: '等待核对',
  canceling: '取消中',
  canceled: '已取消',
}

/** 一条可跳转 / 可探测的运行证据。key 在同一会话内唯一，重复出现时保留最新的状态。 */
export interface RunEvidence {
  key: string
  kind: RunEvidenceKind
  id: string
  /** 展示用短标签，例如「回测 bt_12ab…」 */
  label: string
  href?: string
  state: RunEvidenceState
  /** 来自哪条消息（seq），用于判断是不是本轮产生的证据 */
  seq: number
  /** 只读探测：用现有接口确认后台 run 是否还在排队 / 已完成（不改后端、不新建接口） */
  probe?: { url: string; kind: 'workflow' | 'task' | 'team' }
}

export interface EvidenceProbe { key: string; url: string; kind: 'workflow' | 'task' | 'team' }

/* ------------------------------------------------------------------ */
/* 基础工具                                                            */
/* ------------------------------------------------------------------ */

const isRecord = (v: unknown): v is Record<string, any> => !!v && typeof v === 'object' && !Array.isArray(v)
const text = (v: unknown): string => (typeof v === 'string' ? v.trim() : typeof v === 'number' ? String(v) : '')

/** 长 id 只留前 10 位，保证 390px 下标签不横向溢出。 */
export function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 10)}…` : id
}

const oneLine = (s: string, max = 180): string => s.replace(/\s+/g, ' ').trim().slice(0, max)

/** 运行 / 任务状态归一化。大小写与中英文都来自后端枚举，不做语义联想。 */
export function normalizeRunState(raw: unknown): RunEvidenceState {
  const s = text(raw).toUpperCase()
  if (!s) return 'unknown'
  if (['QUEUED', 'PENDING', 'WAITING', 'SCHEDULED', '排队', '排队中'].includes(s)) return 'queued'
  if (['RUNNING', 'RECOVERING', 'PAUSED', 'STARTED', '运行中', '正在执行', '恢复中'].includes(s)) return 'running'
  if (['COMPLETED', 'COMPLETE', 'SUCCEEDED', 'SUCCESS', 'DONE', 'FINISHED', 'OK', '已完成', '完成'].includes(s)) return 'done'
  if (['FAILED', 'FAILURE', 'ERROR', 'TIMEOUT', '失败', '出错'].includes(s)) return 'failed'
  return 'unknown'
}

export function normalizeTeamState(raw: unknown): RunEvidenceState {
  const value = text(raw).toUpperCase()
  if (value === 'WAITING') return 'waiting'
  if (value === 'CANCELING') return 'canceling'
  if (value === 'CANCELED') return 'canceled'
  return normalizeRunState(raw)
}

/** 模型 / 组合 / 执行研究的训练实验页（与 lab/model.tsx 的 onOpenRun 路由一致）。 */
function runHref(domain: LabProject['kind'] | undefined, id: string): string | undefined {
  const safe = encodeURIComponent(id)
  if (domain === 'model') return `#/modellib/run/${safe}`
  if (domain === 'portfolio') return `#/portfolio/run/${safe}`
  if (domain === 'execution') return `#/execution/run/${safe}`
  return undefined
}

export function evidenceLabel(e: RunEvidence): string {
  return e.label || `${RUN_EVIDENCE_LABEL[e.kind]} ${shortId(e.id)}`
}

/* ------------------------------------------------------------------ */
/* 单条消息：运行证据 + 消息级状态标签                                  */
/* ------------------------------------------------------------------ */

export interface EvidenceOptions {
  live?: Record<string, RunEvidenceState>
  projectId?: string
  domain?: LabProject['kind']
}

/**
 * 从一条消息里提取运行证据。只认结构化字段：
 * - `data` 里（含 preview 的 JSON）的 run_id / bt_id / backtest_id / workflow_view / task_view / artifact_view；
 * - report 卡片的 `data.id`；
 * - 正文里 ql:* 内容块的 `source.run_id` / `source.backtest_id`，以及 `bt:` / `#/backtest/` / `run_id=` 链接。
 * 自然语言里的「已完成 / 已生成」不算证据。
 */
export function messageEvidence(m: LabMessage, opts: EvidenceOptions = {}): RunEvidence[] {
  return scanMessages([m], { ...opts, afterSeq: 0 }).evidence
}

export interface MessageTag {
  label: string
  tone: 'generating' | 'generated' | 'submitted' | 'replied' | 'failed' | 'stopped'
  evidence: RunEvidence[]
}

/** 消息气泡上的状态标签：只有带 run/工件完成证据的回复才允许写「已生成」。 */
export function messageTag(m: LabMessage, opts: EvidenceOptions = {}): MessageTag | null {
  if (m.role === 'user') return null
  const d = isRecord(m.data) ? m.data : {}
  const evidence = applyLive(messageEvidence(m, opts), opts.live)
  const states = evidence.map((e) => e.state)
  const anyDone = states.includes('done')
  const anyActive = states.some((s) => s === 'queued' || s === 'running')
  const team = evidence.find((item) => item.kind === 'team')
  if (team?.state === 'waiting') return { label: '等待核对', tone: 'submitted', evidence }
  if (team?.state === 'canceling') return { label: '取消中', tone: 'submitted', evidence }
  if (team?.state === 'canceled') return { label: '已取消', tone: 'stopped', evidence }
  if (m.kind === 'error') return { label: '执行失败', tone: 'failed', evidence }
  if (m.kind === 'tool') {
    const status = text(d.status)
    if (status === 'running') return { label: '生成中', tone: 'generating', evidence }
    if (status === 'error') return { label: '执行失败', tone: 'failed', evidence }
    if (status === 'stopped') return { label: '已停止', tone: 'stopped', evidence }
    if (anyDone) return { label: '已生成', tone: 'generated', evidence }
    if (anyActive) return { label: '已提交', tone: 'submitted', evidence }
    if (states.includes('failed')) return { label: '执行失败', tone: 'failed', evidence }
    return null
  }
  if (m.kind === 'text' && m.role === 'assistant') {
    if (d.streaming === true) return { label: '生成中', tone: 'generating', evidence }
    if (d.stopped === true) return { label: '已停止', tone: 'stopped', evidence }
    if (anyDone) return { label: '已生成', tone: 'generated', evidence }
    if (anyActive) return { label: '已提交', tone: 'submitted', evidence }
    // 只是引用了失败的运行：回复本身仍是文字回复，证据标签用红色显示
    return { label: '已回复', tone: 'replied', evidence }
  }
  if (anyDone) return { label: '已生成', tone: 'generated', evidence }
  if (anyActive) return { label: '已提交', tone: 'submitted', evidence }
  if (states.includes('failed')) return { label: '执行失败', tone: 'failed', evidence }
  return null
}

/* ------------------------------------------------------------------ */
/* 会话扫描：证据、失败、回复、进行中的动作                              */
/* ------------------------------------------------------------------ */

export interface ScanOptions extends EvidenceOptions {
  /** 只看这个序号之后的消息（一般传最后一条用户消息的 seq，表示「本轮」）。 */
  afterSeq?: number
}

export interface MessageScan {
  evidence: RunEvidence[]
  lastFailure: { seq: number; reason: string } | null
  lastDoneSeq: number
  lastReply: { seq: number; content: string } | null
  active: { seq: number; label: string } | null
  pendingAsk: { seq: number; question: string } | null
  streaming: boolean
  stopped: boolean
}

export function scanMessages(messages: LabMessage[], opts: ScanOptions = {}): MessageScan {
  const rows = [...(messages || [])].sort((a, b) => a.seq - b.seq).filter((m) => m.seq > (opts.afterSeq ?? 0))
  const found = new Map<string, RunEvidence>()
  let lastFailure: { seq: number; reason: string } | null = null
  let lastDoneSeq = 0
  let lastReply: { seq: number; content: string } | null = null
  let active: { seq: number; label: string } | null = null
  let pendingAsk: { seq: number; question: string } | null = null
  let streaming = false
  let stopped = false

  const noteFailure = (seq: number, reason: string) => {
    if (!lastFailure || seq >= lastFailure.seq) lastFailure = { seq, reason: oneLine(reason) || '本轮执行失败' }
  }
  const put = (e: RunEvidence) => {
    const prev = found.get(e.key)
    // 同一个 run 在会话里被多次检查：状态以后一次为准，未知状态不覆盖已知状态
    const state = e.state === 'unknown' && prev ? prev.state : e.state
    if (!prev || e.seq >= prev.seq) found.set(e.key, { ...e, state })
  }
  const noteActive = (seq: number, label: string) => {
    if (!active || seq >= active.seq) active = { seq, label }
  }

  const putRun = (id: string, seq: number, rawState?: unknown) => {
    put({
      key: `run:${id}`,
      kind: 'run',
      id,
      label: `${RUN_EVIDENCE_LABEL.run} ${shortId(id)}`,
      href: runHref(opts.domain, id),
      state: rawState ? normalizeRunState(rawState) : 'done',
      seq,
    })
  }
  const putBacktest = (id: string, seq: number) => {
    put({
      key: `backtest:${id}`,
      kind: 'backtest',
      id,
      label: `${RUN_EVIDENCE_LABEL.backtest} ${shortId(id)}`,
      href: `#/backtest/${encodeURIComponent(id)}`,
      state: 'done',
      seq,
    })
  }
  const putWorkflow = (wf: Record<string, any>, seq: number) => {
    const runId = text(wf.run_id)
    if (!runId) return // 只保存未运行：不是完成证据
    const wid = text(wf.workflow_id)
    const rev = Number(wf.revision)
    const query = new URLSearchParams()
    if (Number.isFinite(rev) && rev > 0) query.set('revision', String(rev))
    query.set('run_id', runId)
    put({
      key: `workflow:${runId}`,
      kind: 'workflow',
      id: runId,
      label: `${RUN_EVIDENCE_LABEL.workflow} ${shortId(runId)}`,
      href: wid ? `#/workflows/${encodeURIComponent(wid)}?${query.toString()}` : undefined,
      state: normalizeRunState(wf.state),
      seq,
      probe: wid ? { url: `/api/workflows/${encodeURIComponent(wid)}/view?${query.toString()}`, kind: 'workflow' } : undefined,
    })
  }
  const putTask = (tv: Record<string, any>, seq: number) => {
    const id = text(tv.id)
    if (!id) return
    const title = text(tv.title)
    put({
      key: `task:${id}`,
      kind: 'task',
      id,
      label: `${RUN_EVIDENCE_LABEL.task} ${title ? shortId(title) : shortId(id)}`,
      state: normalizeRunState(tv.state),
      seq,
      probe: { url: `/api/runs/${encodeURIComponent(id)}`, kind: 'task' },
    })
  }
  const putArtifact = (hash: unknown, seq: number) => {
    const id = text(hash)
    if (!id) return
    put({
      key: `artifact:${id}`,
      kind: 'artifact',
      id,
      label: `${RUN_EVIDENCE_LABEL.artifact} ${shortId(id)}`,
      state: 'done',
      seq,
    })
  }

  /** 只沿可能藏运行引用的字段递归，避免扫描 metrics / equity 这类长数组。 */
  const collect = (value: unknown, seq: number, depth: number) => {
    if (value === null || value === undefined || depth > 4) return
    if (typeof value === 'string') {
      const s = value.trim()
      if (s.length < 20_000 && (s.startsWith('{') || s.startsWith('['))) {
        try {
          collect(JSON.parse(s), seq, depth + 1)
        } catch { /* preview 可能被截断：忽略 */ }
      }
      return
    }
    if (Array.isArray(value)) {
      value.slice(0, 40).forEach((item) => collect(item, seq, depth + 1))
      return
    }
    if (!isRecord(value)) return
    const runId = text(value.run_id)
    const teamId = text(value.runtime_run_id).startsWith('team-root-') ? (runId || text(value.id)) : ''
    if (teamId) put({ key: `team:${teamId}`, kind: 'team', id: teamId, label: `评审 ${shortId(teamId)}`,
      state: normalizeTeamState(value.status), seq,
      probe: { url: `/api/agents/team-runs/${encodeURIComponent(teamId)}`, kind: 'team' } })
    else if (runId) putRun(runId, seq, value.run_state ?? value.state)
    const bt = text(value.bt_id) || text(value.backtest_id)
    if (bt) putBacktest(bt, seq)
    if (isRecord(value.workflow_view)) putWorkflow(value.workflow_view, seq)
    if (isRecord(value.task_view)) putTask(value.task_view, seq)
    if (isRecord(value.artifact_view)) putArtifact(value.artifact_view.artifact_hash, seq)
    for (const key of ['result', 'preview', 'data', 'run', 'runs', 'rows', 'items', 'segments', 'children', 'outputs', 'summary']) {
      if (key in value) collect(value[key], seq, depth + 1)
    }
  }

  const contentEvidence = (m: LabMessage) => {
    const content = m.content || ''
    if (!content) return
    if (!/run_id|backtest|bt:/.test(content)) return
    const seen = new Set<string>()
    const add = (id: string, kind: RunEvidenceKind) => {
      if (!id || seen.has(`${kind}:${id}`)) return
      seen.add(`${kind}:${id}`)
      if (kind === 'backtest') putBacktest(id, m.seq)
      else putRun(id, m.seq)
    }
    for (const match of content.matchAll(/"(?:run_id|backtest_id|bt_id)"\s*:\s*"([A-Za-z0-9_.:-]{2,64})"/g)) {
      add(match[1], /"(?:backtest_id|bt_id)"/.test(match[0]) ? 'backtest' : 'run')
    }
    for (const match of content.matchAll(/#\/backtest\/([A-Za-z0-9_.:-]{2,64})/g)) add(match[1], 'backtest')
    for (const match of content.matchAll(/\bbt:([A-Za-z0-9_.:-]{2,64})/g)) add(match[1], 'backtest')
    for (const match of content.matchAll(/[?&]run_id=([A-Za-z0-9_.:-]{2,64})/g)) add(match[1], 'run')
  }

  for (const m of rows) {
    const d = isRecord(m.data) ? m.data : {}
    if (m.role === 'assistant' && m.kind === 'text') {
      if (d.streaming === true) {
        streaming = true
        noteActive(m.seq, '流式输出')
      } else if (d.stopped === true) {
        stopped = true
      } else if ((m.content || '').trim()) {
        lastReply = { seq: m.seq, content: (m.content || '').trim() }
      }
    }
    if (m.kind === 'error' && m.role !== 'tool') noteFailure(m.seq, m.content || '')
    if (m.kind === 'tool') {
      const status = text(d.status)
      if (status === 'running') noteActive(m.seq, text(d.label) || '工具执行中')
      else if (status === 'error') noteFailure(m.seq, text(d.error) || m.content || '')
      else if (status === 'stopped') stopped = true
      // 本地交付也是真实产物；只接受成功工具返回，不从助手正文推断。
      if (status === 'ok' && ['local_chart', 'workflow_import_data'].includes(text(d.name))) {
        let result: unknown = d.preview
        if (typeof result === 'string') {
          try { result = JSON.parse(result) } catch { result = null }
        }
        if (isRecord(result)) {
          if (d.name === 'workflow_import_data' && /^[a-f0-9]{64}$/i.test(text(result.sha256))) {
            putArtifact(result.sha256, m.seq)
          }
          if (d.name === 'local_chart' && Number(result.bytes) > 0 && text(result.path)
            && /^\/api\/local\/files\/image\?path=/.test(text(result.url))) {
            put({ key: `local-file:${result.path}`, kind: 'artifact', id: result.path,
              label: `图表 ${text(result.path).split(/[\\/]/).pop()}`, state: 'done', seq: m.seq })
          }
        }
      }
    }
    if (m.kind === 'subagents' && text(d.status) === 'running') noteActive(m.seq, '子 Agent 并行研究')
    if (m.kind === 'ask' && m.role === 'assistant' && !d.answered) {
      pendingAsk = { seq: m.seq, question: oneLine(m.content || '', 140) }
    }
    if (m.kind === 'report') {
      const rid = text(d.id)
      if (rid) {
        const title = text(d.title)
        put({
          key: `report:${rid}`,
          kind: 'report',
          id: rid,
          label: `${RUN_EVIDENCE_LABEL.report} ${title ? shortId(title) : shortId(rid)}`,
          href: opts.projectId ? `#/report/${encodeURIComponent(opts.projectId)}/${encodeURIComponent(rid)}` : undefined,
          state: 'done',
          seq: m.seq,
        })
      }
    }
    collect(d, m.seq, 0)
    contentEvidence(m)
  }

  const evidence = [...found.values()].sort((a, b) => a.seq - b.seq)
  for (const e of evidence) {
    if (e.state === 'done') lastDoneSeq = Math.max(lastDoneSeq, e.seq)
    if (e.state === 'running') noteActive(e.seq, e.label)
    // 失败证据只作为可见标签；全局「执行失败」只认显式错误信号与实时探测，
    // 避免「只是查看了历史失败运行」也被判成本轮失败。
  }
  return { evidence, lastFailure, lastDoneSeq, lastReply, active, pendingAsk, streaming, stopped }
}

/* ------------------------------------------------------------------ */
/* 会话级五态                                                          */
/* ------------------------------------------------------------------ */

export interface AgentStatus {
  state: AgentState
  /** 展示文案：通常是五态名；`generated` 且无运行证据时是「已回复」。 */
  label: string
  detail: string
  evidence: RunEvidence[]
  /** 是否有 run / 工件级完成证据 */
  artifacts: boolean
  failure?: string
  phase?: string
  startedAt?: number
}

export interface StatusInput extends EvidenceOptions {
  project: Pick<LabProject, 'status' | 'job' | 'plan' | 'updated_at'> | null | undefined
  messages: LabMessage[]
  /** 后台 run 的实时状态（由现有只读接口探测得到），覆盖消息里可能已经过期的 state */
  live?: Record<string, RunEvidenceState>
  /** 客户端待发送队列：只写进 detail，不改变五态（后端状态才是唯一数据源） */
  queueCount?: number
}

const QUEUE_PHASE = /排队|等待执行|等待运行|调度中|队列/

function applyLive(evidence: RunEvidence[], live?: Record<string, RunEvidenceState>): RunEvidence[] {
  if (!live) return evidence
  return evidence.map((e) => (live[e.key] ? { ...e, state: live[e.key] } : e))
}

const emptyStatus = (state: AgentState, label: string, detail: string): AgentStatus => ({
  state,
  label,
  detail,
  evidence: [],
  artifacts: false,
})

/** 会话级五态：先看后端回合状态，再看后台 run 证据，最后才是完成 / 失败 / 等待。 */
export function deriveAgentStatus(input: StatusInput): AgentStatus {
  const project = input.project || null
  const messages = input.messages || []
  const lastUserSeq = messages.reduce((a, m) => (m.role === 'user' ? Math.max(a, m.seq) : a), 0)
  const scan = scanMessages(messages, { ...input, afterSeq: lastUserSeq })
  const evidence = applyLive(scan.evidence, input.live)
  const done = evidence.filter((e) => e.state === 'done')
  const artifacts = done.length > 0
  const lastDoneSeq = done.reduce((a, e) => Math.max(a, e.seq), scan.lastDoneSeq)
  const probedFailed = input.live
    ? evidence
      .filter((e) => e.state === 'failed' && input.live?.[e.key] === 'failed')
      .reduce<RunEvidence | null>((a, e) => (!a || e.seq >= a.seq ? e : a), null)
    : null
  const failure = probedFailed && probedFailed.seq >= (scan.lastFailure?.seq ?? 0)
    ? { seq: probedFailed.seq, reason: `${RUN_EVIDENCE_LABEL[probedFailed.kind]} ${shortId(probedFailed.id)} 失败` }
    : scan.lastFailure
  const status = project?.status || 'idle'
  const job = project?.job || null
  const phase = (job?.phase || '').trim()
  const startedAt = job?.started_at

  // 1) 后端回合在跑：job.phase 或当前轮证据有排队 → 排队中，否则生成中
  if (status === 'running') {
    const queued = [...evidence].reverse().find((e) => e.state === 'queued')
    const queuedNow = (!!queued && queued.seq >= lastDoneSeq) || QUEUE_PHASE.test(phase)
    if (queuedNow) {
      return {
        state: 'queued', label: AGENT_STATE_LABEL.queued, evidence, artifacts, phase, startedAt,
        detail: queued ? evidenceLabel(queued) : (phase || '等待执行'),
      }
    }
    const working = phase || scan.active?.label || '正在生成'
    return {
      state: 'generating', label: AGENT_STATE_LABEL.generating, evidence, artifacts, phase, startedAt,
      detail: input.queueCount ? `${working} · 另有 ${input.queueCount} 条待发送` : working,
    }
  }

  const pendingTeam = [...evidence].reverse().find((item) => item.kind === 'team' && ['waiting', 'canceling', 'canceled'].includes(item.state))
  if (pendingTeam && pendingTeam.seq >= lastDoneSeq) {
    return { state: 'waiting', label: pendingTeam.state === 'canceled' ? '已取消' : pendingTeam.state === 'canceling' ? '取消中' : '等待核对',
      evidence, artifacts, phase, startedAt, detail: evidenceLabel(pendingTeam) }
  }

  // 2) 回合已结束，但后台 run（worker 任务 / 工作流运行）还在排队或运行：长任务恢复
  const background = [...evidence].reverse().find((e) => e.state === 'queued' || e.state === 'running')
  if (background && background.seq >= lastDoneSeq && (!failure || background.seq > failure.seq)) {
    const state: AgentState = background.state === 'running' ? 'generating' : 'queued'
    return {
      state, label: AGENT_STATE_LABEL[state], evidence, artifacts, phase, startedAt,
      detail: evidenceLabel(background),
    }
  }

  // 等待审批/回答是当前可操作状态，不能被前面的可恢复工具错误遮住。
  if (status === 'awaiting_user') {
    return {
      state: 'waiting', label: AGENT_STATE_LABEL.waiting, evidence, artifacts, phase, startedAt,
      detail: scan.pendingAsk ? (scan.pendingAsk.question || '等你回答') : '等你回答',
    }
  }

  // 3) 本轮有失败、且之后没有完成证据：执行失败（后续成功会以 done 证据出现）
  if (failure && failure.seq > lastDoneSeq) {
    return {
      state: 'failed', label: AGENT_STATE_LABEL.failed, evidence, artifacts, phase, startedAt,
      detail: failure.reason, failure: failure.reason,
    }
  }

  // 5) 有真实 run / 工件证据才算已生成
  if (artifacts) {
    return {
      state: 'generated', label: AGENT_STATE_LABEL.generated, evidence, artifacts, phase, startedAt,
      detail: done.length === 1 ? evidenceLabel(done[0]) : `${done.length} 条运行证据`,
    }
  }

  // 6) 只有文字回复：已回复（绝不写已生成）
  if (scan.lastReply) {
    return {
      state: 'generated', label: '已回复', evidence, artifacts: false, phase, startedAt,
      detail: '本轮只有文字回复，没有 run / 工件证据',
    }
  }

  // 7) 停止过的回合：回到等待输入，不再显示「生成中」
  return {
    state: 'waiting', label: AGENT_STATE_LABEL.waiting, evidence, artifacts, phase, startedAt,
    detail: scan.stopped ? '上一轮已停止，可以继续输入' : '可以继续输入',
  }
}

/** 侧栏会话列表只有 brief：能判断的只有进行中 / 等待 / 观察到的失败。 */
export function briefStatus(p: LabProjectBrief, observed?: ObservedFailure): AgentStatus {
  const phase = (p.job?.phase || '').trim()
  if (p.status === 'running') {
    if (QUEUE_PHASE.test(phase)) {
      return { ...emptyStatus('queued', AGENT_STATE_LABEL.queued, phase || '等待执行'), phase, startedAt: p.job?.started_at }
    }
    return {
      ...emptyStatus('generating', AGENT_STATE_LABEL.generating, phase || p.job?.kind || '正在生成'),
      phase, startedAt: p.job?.started_at,
    }
  }
  const failure = observedFailure(p, observed)
  if (failure) return { ...emptyStatus('failed', AGENT_STATE_LABEL.failed, failure), failure }
  return emptyStatus('waiting', AGENT_STATE_LABEL.waiting, p.status === 'awaiting_user' ? '等你回答' : '')
}

/** 只探测还在排队 / 运行的后台 run，同一轮最多 4 个，避免无谓请求。 */
export function evidenceProbes(evidence: RunEvidence[]): EvidenceProbe[] {
  const seen = new Set<string>()
  const out: EvidenceProbe[] = []
  for (const e of evidence) {
    if (!e.probe || !['queued', 'running', 'waiting', 'canceling'].includes(e.state)) continue
    if (seen.has(e.probe.url)) continue
    seen.add(e.probe.url)
    out.push({ key: e.key, ...e.probe })
    if (out.length >= 4) break
  }
  return out
}

/* ------------------------------------------------------------------ */
/* 侧栏的失败徽标：把已观察到的失败带到列表（仍以后端 updated_at 为准）   */
/* ------------------------------------------------------------------ */

export interface ObservedFailure { updatedAt: number; reason: string }
const OBSERVED_KEY = 'qa.lab.observedFailure'

export function readObservedFailures(): Record<string, ObservedFailure> {
  try {
    const raw = JSON.parse(localStorage.getItem(OBSERVED_KEY) || '{}')
    return isRecord(raw) ? (raw as Record<string, ObservedFailure>) : {}
  } catch {
    return {}
  }
}

function writeObservedFailures(all: Record<string, ObservedFailure>) {
  try {
    const entries = Object.entries(all).sort((a, b) => b[1].updatedAt - a[1].updatedAt).slice(0, 60)
    localStorage.setItem(OBSERVED_KEY, JSON.stringify(Object.fromEntries(entries)))
  } catch { /* 隐私模式 / 存储不可用：侧栏退化为只有进行中徽标 */ }
}

/** 记下「这个对话在 updated_at 这一刻的最后一轮是失败的」，用于侧栏徽标。 */
export function rememberObservedFailure(project: Pick<LabProject, 'id' | 'updated_at'>, reason: string): void {
  if (!project?.id) return
  const all = readObservedFailures()
  const prev = all[project.id]
  const text = oneLine(reason || '上一轮执行失败', 120)
  if (prev && prev.updatedAt === project.updated_at && prev.reason === text) return
  all[project.id] = { updatedAt: project.updated_at, reason: text }
  writeObservedFailures(all)
}

export function clearObservedFailure(id: string): void {
  if (!id) return
  const all = readObservedFailures()
  if (!(id in all)) return
  delete all[id]
  writeObservedFailures(all)
}

/** 只有项目从那之后没再更新（updated_at 相同）时才认这条失败记录，避免过期徽标。 */
export function observedFailure(p: LabProjectBrief, rec?: ObservedFailure): string | null {
  if (!rec || p.status !== 'idle') return null
  return rec.updatedAt === p.updated_at ? rec.reason : null
}
