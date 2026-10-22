import { useEffect, useMemo, useState } from 'react'
import { get } from '../http'
import { Heading, Tag } from '../components/ui'
import { openInLab } from '../components/handoff'
import type { PageProps } from './types'
import { STAGES, TOOLS, TOOL_GROUPS, type FlowItem, type ToolGroup } from './research-flow/data'

interface MentorLite { progress?: number; completed?: string[]; total?: number }
interface RealPerpLite { status?: { enabled?: boolean; ready?: boolean; killed?: boolean; blockers?: string[] } }

function StageGlyph({ kind }: { kind: string }) {
  const common = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.5, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  if (kind === 'data') return <svg viewBox="0 0 24 24" aria-hidden><ellipse cx="12" cy="6" rx="7" ry="3" {...common} /><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" {...common} /></svg>
  if (kind === 'factor') return <svg viewBox="0 0 24 24" aria-hidden><path d="M4 18V6M4 18h16M7 15l3-4 3 2 4-6" {...common} /><circle cx="7" cy="15" r="1.2" {...common} /><circle cx="10" cy="11" r="1.2" {...common} /><circle cx="13" cy="13" r="1.2" {...common} /><circle cx="17" cy="7" r="1.2" {...common} /></svg>
  if (kind === 'model') return <svg viewBox="0 0 24 24" aria-hidden><circle cx="6" cy="6" r="2" {...common} /><circle cx="18" cy="6" r="2" {...common} /><circle cx="6" cy="18" r="2" {...common} /><circle cx="18" cy="18" r="2" {...common} /><path d="M8 6h8M8 18h8M6 8v8M18 8v8" {...common} /></svg>
  if (kind === 'execution') return <svg viewBox="0 0 24 24" aria-hidden><path d="M4 17h3l3-9h4l3 9h3M8 5h8M8 19h8" {...common} /></svg>
  if (kind === 'verify') return <svg viewBox="0 0 24 24" aria-hidden><path d="M12 3 5 6v5c0 4.3 2.8 8 7 10 4.2-2 7-5.7 7-10V6l-7-3Z" {...common} /><path d="m8.5 12 2.2 2.2 4.8-5" {...common} /></svg>
  if (kind === 'run') return <svg viewBox="0 0 24 24" aria-hidden><path d="M5 5v14l14-7L5 5Z" {...common} /></svg>
  return <svg viewBox="0 0 24 24" aria-hidden><path d="M4 19V5h16v14H4Z" {...common} /><path d="m7 15 3-4 3 2 4-6" {...common} /></svg>
}

export default function ResearchFlowPage({ onNavigate, notify, features }: PageProps) {
  const [query, setQuery] = useState('')
  const [toolGroup, setToolGroup] = useState<'all' | ToolGroup>('all')
  const [mentor, setMentor] = useState<MentorLite | null>(null)
  const [realperp, setRealperp] = useState<RealPerpLite | null>(null)

  useEffect(() => {
    let alive = true
    if (features?.['research.mentor'] !== false) {
      get<MentorLite>('/api/mentor/course')
        .then((value) => { if (alive) setMentor(value) })
        .catch(() => undefined)
    }
    if (features?.['execution.real_perp'] !== false) {
      get<RealPerpLite>('/api/realperp')
        .then((value) => { if (alive) setRealperp(value) })
        .catch(() => undefined)
    }
    return () => { alive = false }
  }, [features])

  const go = (item: FlowItem) => {
    if (item.ai) {
      openInLab({ idea: item.ai, kind: 'strategy', title: item.label, attached: `研究流程 · ${item.label}` })
      return
    }
    if (item.route) onNavigate(item.route)
    else notify('这个功能还没有配置入口', true)
  }

  const q = query.trim().toLowerCase()
  const visibleStages = useMemo(() => STAGES.map((stage) => ({
    ...stage,
    items: q
      ? stage.items.filter((item) => `${item.label} ${item.note} ${item.tag ?? ''}`.toLowerCase().includes(q))
      : stage.items,
  })).filter((stage) => stage.items.length > 0), [q])
  const visibleTools = useMemo(() => TOOLS.filter((item) =>
    (toolGroup === 'all' || item.group === toolGroup)
    && (!q || `${item.label} ${item.note} ${item.tag ?? ''}`.toLowerCase().includes(q)),
  ), [q, toolGroup])
  const total = visibleStages.reduce((n, stage) => n + stage.items.length, 0) + visibleTools.length
  const mentorDisabled = features?.['research.mentor'] === false
  const realPerpDisabled = features?.['execution.real_perp'] === false
  const mentorPct = mentor?.progress !== undefined ? Math.round(mentor.progress * 100) : null
  const rp = realperp?.status
  const rpLabel = realPerpDisabled ? '已关闭' : rp?.killed ? '已急停' : rp?.ready ? '可启动' : rp?.enabled ? '待自检' : '未开启'
  const jumpToStage = (no: string) => document.getElementById(`rf-stage-${no}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  const featureOff = (item: FlowItem) => !!item.feature && features?.[item.feature] === false
  const itemBadges = (item: FlowItem) => (
    <>
      {item.tag && <Tag tone={item.tag === 'AI' || item.tag === '真实资金' ? 'warn' : undefined}>{item.tag}</Tag>}
      {featureOff(item) && <span className="rf-off">未开启</span>}
    </>
  )

  return (
    <>
      <Heading
        eyebrow="RESEARCH FLOW"
        title="研究流程"
        description="从数据、假设和因子，到策略、模型、组合、执行、验证与运行。不是功能清单，而是一条可执行、可审查、可复现的研究闭环。"
        actions={<button className="button primary" onClick={() => openInLab({ idea: '/plan 完整流程：从数据、因子、策略、模型、组合到执行，帮我列一份可验证的研究计划', kind: 'strategy', title: '完整研究流程' })}>✦ 让 AI 统筹整个流程</button>}
      />

      <section className="rf-hero">
        <div className="rf-hero-copy">
          <span className="rf-eyebrow">RESEARCH OPERATING SYSTEM</span>
          <h2>把“看起来不错”变成“可以被复核”。</h2>
          <p>每个阶段都有明确产物：数据质量报告、可检验信号、冻结版本、样本外结果、审查卡和可重放研究包。你可以逐页深入，也可以把阶段交给 AI 统筹。</p>
          <div className="rf-hero-actions">
            <button className="button" onClick={() => onNavigate('workflows')}>打开函数工作流</button>
            <button className="button primary" onClick={() => openInLab({ idea: '/plan 从研究假设开始，先检查数据，再设计策略、验证并给出研究结论', kind: 'strategy', title: '从假设开始' })}>✦ 从假设开始</button>
            <button className="button" onClick={() => onNavigate('mentor')}>研究导师课程</button>
            <button className="button" onClick={() => onNavigate('backtests')}>查看验证记录</button>
          </div>
        </div>
        <div className="rf-hero-orbit" aria-hidden>
          <span className="rf-orbit-ring r1" /><span className="rf-orbit-ring r2" /><span className="rf-orbit-ring r3" />
          <span className="rf-orbit-core">Q</span>
          <span className="rf-orbit-dot d1" /><span className="rf-orbit-dot d2" /><span className="rf-orbit-dot d3" />
        </div>
        <div className="rf-stat-grid">
          <div><b>07</b><span>研究阶段</span></div>
          <div><b className={mentorDisabled ? 'bad' : ''}>{mentorDisabled ? '已关闭' : mentorPct === null ? '—' : `${mentorPct}%`}</b><span>导师进度</span></div>
          <div><b className={realPerpDisabled || rp?.killed ? 'bad' : rp?.ready ? 'ok' : ''}>{rpLabel}</b><span>真实永续</span></div>
          <div><b>{TOOLS.length}</b><span>跨阶段能力</span></div>
        </div>
      </section>

      <nav className="rf-journey" aria-label="研究阶段导航">
        {STAGES.map((stage) => (
          <button key={stage.no} type="button" onClick={() => jumpToStage(stage.no)}>
            <i>{stage.no}</i><span>{stage.title}</span><em>{stage.items.length}</em>
          </button>
        ))}
      </nav>

      <div className="rf-filter-bar">
        <label className="rf-search">
          <span aria-hidden>⌕</span>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索功能、页面或研究能力…" aria-label="搜索研究流程能力" />
          {query && <button type="button" onClick={() => setQuery('')} aria-label="清除搜索">×</button>}
        </label>
        <span className="rf-filter-count">{total} 个入口</span>
        <span className="rf-filter-hint">按阶段浏览，或直接搜索“研究包 / 全仓 / 导师 / 急停”</span>
      </div>

      {visibleStages.length === 0 && visibleTools.length === 0 && <div className="rf-empty">没有匹配的研究入口。换个关键词，或清空搜索继续浏览。</div>}

      <div className="rf-stage-grid">
        {visibleStages.map((stage) => (
          <article key={stage.no} id={`rf-stage-${stage.no}`} className={`rf-stage-card tone-${stage.glyph}${stage.wide ? ' wide' : ''}`}>
            <header className="rf-stage-head">
              <div className="rf-stage-glyph"><StageGlyph kind={stage.glyph} /></div>
              <div className="rf-stage-title">
                <span>{stage.no}</span>
                <h2>{stage.title}</h2>
              </div>
              <span className="rf-stage-count">{stage.items.length} 个入口</span>
            </header>
            <p className="rf-stage-desc">{stage.desc}</p>
            <div className="rf-stage-items">
              {stage.items.map((item) => (
                <button key={`${stage.no}-${item.label}`} type="button" className={`rf-item${featureOff(item) ? ' off' : ''}`} onClick={() => go(item)}>
                  <span className="rf-item-title">{item.label}{itemBadges(item)}</span>
                  <small>{item.note}</small>
                </button>
              ))}
            </div>
            <footer className="rf-stage-foot">
              <span>产出：{stage.outcome}</span>
              <button type="button" className="rf-ai-link" onClick={() => openInLab({ idea: stage.ai, kind: stage.labKind, title: `${stage.title} · AI 统筹`, attached: `研究流程 · ${stage.title}` })}>让 AI 统筹 →</button>
            </footer>
          </article>
        ))}
      </div>

      <section className="rf-tools">
        <header className="rf-tools-head">
          <div>
            <span className="rf-eyebrow">CROSS-STAGE CAPABILITIES</span>
            <h2>跨阶段能力</h2>
            <p>按研究保障、导入导出、协作复用、研究配置和运行入口集中浏览。</p>
          </div>
          <div className="rf-tool-tabs" role="tablist" aria-label="跨阶段能力分类">
            <button type="button" aria-selected={toolGroup === 'all'} className={toolGroup === 'all' ? 'on' : ''} onClick={() => setToolGroup('all')}>全部</button>
            {TOOL_GROUPS.map((group) => <button key={group.id} type="button" aria-selected={toolGroup === group.id} className={toolGroup === group.id ? 'on' : ''} onClick={() => setToolGroup(group.id)}>{group.label}<small>{TOOLS.filter((item) => item.group === group.id).length}</small></button>)}
          </div>
        </header>
        {visibleTools.length === 0 ? <div className="rf-empty">这个分类下没有匹配能力。</div> : (
          <div className="rf-tool-grid">
            {visibleTools.map((item) => (
              <button key={item.label} type="button" className={`rf-tool${featureOff(item) ? ' off' : ''}`} onClick={() => go(item)}>
                <span className="rf-tool-kicker">{TOOL_GROUPS.find((g) => g.id === item.group)?.label}</span>
                <b>{item.label}{item.tag && <Tag tone={item.tag === 'AI' || item.tag === '真实资金' ? 'warn' : undefined}>{item.tag}</Tag>}</b>
                <small>{item.note}</small>
                <span className="rf-tool-action">{item.route ? '打开页面 →' : '交给 AI →'}</span>
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="rf-finish">
        <div>
          <span className="rf-eyebrow">DISCIPLINE BEFORE CONFIDENCE</span>
          <h2>先问“这份结果为什么可能错”，再问“它能赚多少”。</h2>
          <p>研究流程不是保证盈利的流水线，而是把数据、假设、实现、成本和验证逐层钉住，让漂亮曲线不能绕过审查。</p>
        </div>
        <div className="rf-finish-actions">
          <button className="button" onClick={() => onNavigate('discipline')}>试验台账</button>
          <button className="button" onClick={() => onNavigate('validation')}>上线验证</button>
          <button className="button primary" onClick={() => openInLab({ idea: '/discuss 对当前研究做一次红队评审：最可能错在哪里？哪些证据还不够？', kind: 'strategy', title: '红队评审' })}>发起红队评审</button>
        </div>
      </section>
    </>
  )
}
