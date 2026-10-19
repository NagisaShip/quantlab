import { useEffect, useState } from 'react'
import { directBars } from '../marketDirect'
import { get } from '../http'
import { ago, fmtNum, fmtPct, tone, v3, type BacktestRow, type LlmStatus, type PaperSessionView, type StrategyFile } from '../api/research'
import { HomeAsk, Watchlist } from './home/HomeAsk'
import MentorCourse from './home/MentorCourse'
import { EmptyState, Panel } from '../components/ui'
import { NewStrategyModal } from './MyStrategies'
import { missions, type Mission } from '../apiLab'
import { rd, when, type Pipeline } from '../apiRd'
import { validation, type GradRow } from '../apiValidation'
import type { PageProps } from './types'

const MISSION_STATUS: Record<string, string> = { queued: '排队中', running: '讨论中', awaiting_approval: '等你确认', done: '已完成', failed: '出错', stopped: '已停止', idle: '待命' }


export default function HomePage({ onNavigate, notify, features }: PageProps) {
  const [strategies, setStrategies] = useState<StrategyFile[]>([])
  const [backtests, setBacktests] = useState<BacktestRow[]>([])
  const [sessions, setSessions] = useState<PaperSessionView[]>([])
  const [llm, setLlm] = useState<LlmStatus | null>(null)
  const [modal, setModal] = useState<'' | 'new'>('')
  const [recentMissions, setRecentMissions] = useState<Mission[]>([])
  const [market, setMarket] = useState<{ t: number[]; c: number[]; source: string } | null>(null)
  const [pipe, setPipe] = useState<Pipeline | null>(null)
  const [board, setBoard] = useState<{ rows: GradRow[]; cfg: Record<string, number> } | null>(null)

  useEffect(() => {
    v3.strategies().then((r) => setStrategies(r.strategies)).catch(() => undefined)
    v3.backtests(undefined, 8).then((r) => setBacktests(r.backtests)).catch(() => undefined)
    v3.paperSessions().then((r) => setSessions(r.sessions)).catch(() => undefined)
    v3.llmStatus().then(setLlm).catch(() => undefined)
    missions.list().then((r) => setRecentMissions(r.missions)).catch(() => undefined)
    rd.pipeline().then(setPipe).catch(() => undefined)
    validation.board().then((r) => setBoard({ rows: r.board, cfg: r.config })).catch(() => undefined)
    directBars('BTC/USDT', '1h', 168).then((d) => {
      if (d) { setMarket({ t: d.map((b) => b.t), c: d.map((b) => b.c), source: 'exchange' }); return }
      get<{ quotes: { connected: boolean; source?: string; t?: number[]; c?: number[] }[] }>('/api/market/quotes?symbols=BTC%2FUSDT&timeframe=1h&limit=168')
        .then((r) => { const q = r.quotes[0]; if (q?.connected && q.t && q.c) setMarket({ t: q.t, c: q.c, source: q.source || 'exchange' }) })
        .catch(() => undefined)
    })
  }, [])

  const running = sessions.filter((s) => s.status === 'running')
  const pending = strategies.filter((s) => !s.human_approved && s.status !== 'draft')
  const todos: { text: string; note: string; go: string; tone?: string }[] = [
    ...(board?.rows ?? []).filter((r) => r.next && r.passed).map((r) => ({ text: `「${r.name}」可以晋级到${r.next_label}`, note: '检查全部通过，等你确认', go: `validation/gate:${r.strategy}`, tone: 'ok' })),
    ...pending.slice(0, 4).map((s) => ({ text: `「${s.name}」等你审阅`, note: '回测可信、样本外通过后再批准；批准后才能进纸面模拟', go: `editor/${s.slug}` })),
    ...(llm && !llm.ready ? [{ text: '还没配置 AI 模型', note: 'AI 功能现在是规则降级', go: 'models', tone: 'warn' }] : []),
    ...(!strategies.length ? [{ text: '写第一个策略', note: '从模板开始，或让 AI 研究室来写', go: 'strategies' }] : []),
  ]

  return (
    <>
      <section className="home-hero home-hero2">
        <HomeAsk onNavigate={onNavigate} notify={notify} brief={<Brief market={market} board={board?.rows ?? null} running={running.length} />} />
        <Watchlist notify={notify} onNavigate={onNavigate} />
      </section>

      <FlowBoard board={board} onNavigate={onNavigate} />
      {features?.['research.mentor'] !== false && <MentorCourse onNavigate={onNavigate} notify={notify} />}
      {pipe && <PipelineRow p={pipe} onNavigate={onNavigate} />}

      <div className="two-columns">
        <Panel title="最近回测" actions={<button className="button small" onClick={() => onNavigate('backtests')}>全部</button>} flush>
          {!backtests.length ? (
            <EmptyState glyph="∿" title="还没有回测" action={<button className="button primary" onClick={() => setModal('new')}>从模板开始</button>}>选一个模板，30 秒跑出第一份回测报告。</EmptyState>
          ) : (
            <div className="table-wrap">
            <table>
              <thead><tr><th>策略</th><th>区间</th><th className="right">收益</th><th className="right">超额</th><th className="right">回撤</th><th className="right">夏普</th></tr></thead>
              <tbody>
                {backtests.map((b) => (
                  <tr key={b.id} className="clickable" onClick={() => onNavigate(`backtest/${b.id}`)}>
                    <td><strong>{b.strategy_name}</strong><small>{ago(b.created_at)}{b.data_sources.includes('synthetic') ? ' · 合成数据（旧记录）' : ''}</small></td>
                    <td>{b.timeframe}<small>{b.securities.join(', ')}</small></td>
                    <td className={`right ${tone(b.metrics.total_return)}`}>{fmtPct(b.metrics.total_return, 2, true)}</td>
                    <td className={`right ${tone(b.metrics.excess_return)}`}>{fmtPct(b.metrics.excess_return, 2, true)}</td>
                    <td className="right">{fmtPct(b.metrics.max_drawdown)}</td>
                    <td className="right">{fmtNum(b.metrics.sharpe)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          )}
        </Panel>
        <div className="side-column">
          <Panel title="待办">
            {!todos.length ? <div className="hint">没有需要你处理的事。</div> : (
              <ul className="plain-list home-todo">
                {todos.map((t) => (
                  <li key={t.text} className={`clickable ${t.tone ?? ''}`} onClick={() => onNavigate(t.go)}>
                    <b>{t.text}</b><small>{t.note}</small>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          {recentMissions.length > 0 && (
            <Panel title="最近团队讨论" actions={<button className="button small" onClick={() => onNavigate('lab')} title="在 AI 研究室的对话里输入 /discuss 发起">发起讨论</button>}>
              <ul className="plain-list">
                {recentMissions.slice(0, 4).map((m) => (
                  <li key={m.id} className="clickable" onClick={() => onNavigate(`teams/${m.id}`)}>
                    <div className="row-between"><b className="ellipsis">{m.directive}</b></div>
                    <small>{m.mode === 'pipeline' ? '流水线' : '单周期'} · {MISSION_STATUS[m.status] ?? m.status}</small>
                  </li>
                ))}
              </ul>
            </Panel>
          )}
          <Panel title="运行中的模拟" actions={<button className="button small" onClick={() => onNavigate('paper')}>管理</button>}>
            {!running.length ? <div className="hint">没有运行中的实验。批准一个策略版本后即可启动。</div> : (
              <ul className="plain-list">
                {running.map((s) => {
                  const pts = s.equity_points
                  const eq = pts.length ? pts[pts.length - 1].v : s.initial_cash
                  return (
                    <li key={s.id} className="clickable" onClick={() => onNavigate(`paper/${s.id}`)}>
                      <div className="row-between"><b>{s.strategy_name}</b><span className={tone(eq / s.initial_cash - 1)}>{fmtPct(eq / s.initial_cash - 1, 2, true)}</span></div>
                      <small>{fmtNum(s.elapsed_hours, 1)} 小时 · {s.source === 'demo' ? '演示行情' : s.exchange}</small>
                    </li>
                  )
                })}
              </ul>
            )}
          </Panel>
        </div>
      </div>

      {modal === 'new' && <NewStrategyModal onClose={() => setModal('')} onCreated={(slug) => onNavigate(`editor/${slug}`)} notify={notify} />}
    </>
  )
}

const FLOW = [
  { stage: 'backtest', label: '回测', rule: (c: Record<string, number>) => `缩减夏普 DSR ≥ ${c.min_dsr ?? 0.95}、体检无严重项` },
  { stage: 'paper', label: '纸面模拟', rule: (c: Record<string, number>) => `跑满 ${c.paper_days ?? 7} 天` },
  { stage: 'demo', label: '交易所模拟盘', rule: (c: Record<string, number>) => `跑满 ${c.demo_days ?? 14} 天、真实度达标` },
  { stage: 'graduated', label: '已毕业', rule: () => '实盘下单仍关闭' },
]

/** 策略流水线：每个策略在哪一关、离下一关还差什么。点卡片打开它的晋级检查。 */
function FlowBoard({ board, onNavigate }: { board: { rows: GradRow[]; cfg: Record<string, number> } | null; onNavigate: (k: string) => void }) {
  return (
    <section className="sf" data-tour="home-flow" aria-label="策略流水线">
      <div className="sf-head">
        <h2>策略流水线</h2>
        <span className="muted small">每个策略从回测一关关走到交易所模拟盘，每一关都要你确认</span>
        <a className="sf-more" href="#/validation">上线验证 →</a>
      </div>
      <div className="sf-cols">
        {FLOW.map((f, i) => {
          const rows = (board?.rows ?? []).filter((r) => r.stage === f.stage)
          return (
            <div key={f.stage} className="sf-col">
              <div className="sf-col-head"><i>{i + 1}</i><b>{f.label}</b><span className="sf-n">{rows.length}</span></div>
              <small className="sf-rule">{board ? f.rule(board.cfg) : ' '}</small>
              {rows.map((r) => {
                const [a, b] = r.progress.split('/').map(Number)
                return (
                  <button key={r.strategy} className="sf-card" onClick={() => onNavigate(`validation/gate:${r.strategy}`)}>
                    <b>{r.name}</b>
                    {r.next ? <>
                      <span className="sf-bar"><span style={{ width: `${b ? (a / b) * 100 : 0}%` }} /></span>
                      <small className={r.passed ? 'sf-ready' : undefined}>{r.passed ? `可以晋级到${r.next_label}` : `${r.progress} 项通过`}</small>
                    </> : <small>已完成全部验证</small>}
                  </button>
                )
              })}
              {!rows.length && <div className="sf-empty">{i === 0 ? '还没有策略' : '暂无'}</div>}
            </div>
          )
        })}
      </div>
    </section>
  )
}

/** 研究流水线：因子 → 模型 → 组合 → 执行，加上定时任务和试验台账，一格一个，点进对应页面。 */
function PipelineRow({ p, onNavigate }: { p: Pipeline; onNavigate: (k: string) => void }) {
  const ns = p.nightshift
  const cells: { key: string; label: string; value: string; note: string; warn?: boolean }[] = [
    { key: 'factors', label: '因子', value: `${p.factors.active} 入库`, note: p.factors.decaying ? `${p.factors.decaying} 个衰减预警` : `共 ${p.factors.total} 个`, warn: p.factors.decaying > 0 },
    { key: 'modellib', label: '模型', value: `${p.models.active} 上线`, note: p.models.live_ic !== null ? `线上 IC ${fmtNum(p.models.live_ic, 3)}` : `共 ${p.models.total} 个` },
    { key: 'portfolio', label: '组合', value: `${p.portfolio.active} 启用`, note: p.portfolio.risk_usage !== null ? `风险预算用了 ${fmtPct(p.portfolio.risk_usage, 0)}` : '未设目标波动', warn: (p.portfolio.risk_usage ?? 0) > 1 },
    { key: 'execution', label: '执行', value: p.execution.avg_is_bps !== null ? `${fmtNum(p.execution.avg_is_bps, 1)} bps` : '—', note: `近 ${p.execution.runs} 次仿真平均执行缺口` },
    { key: 'long-running', label: '定时任务', value: ns.running ? '执行中' : `${ns.queued} 排队`, note: ns.last ? `上次 ${when(ns.last.started_at)} · 达标 ${ns.passed} 个` : '还没执行过' },
    { key: 'discipline', label: '试验台账', value: `${p.discipline.trials} 次试验`, note: p.discipline.holdout ? '紧封测试集已设置' : '没有紧封测试集', warn: !p.discipline.holdout && p.discipline.trials > 20 },
  ]
  return (
    <section className="rd-pipe" aria-label="研究流水线">
      <div className="rd-pipe-cells">
        {cells.map((c, i) => (
          <button key={c.key} className="rd-pipe-cell" onClick={() => onNavigate(c.key)}>
            <span className="rd-pipe-label">{c.label}{i < 3 && <i aria-hidden="true">→</i>}</span>
            <b>{c.value}</b>
            <small className={c.warn ? 'rd-warn' : undefined}>{c.note}</small>
          </button>
        ))}
      </div>
      {p.brief && (
        <button className="rd-pipe-brief" onClick={() => onNavigate('long-running/brief')}>
          <span>AI 晨报 · {when(p.brief.at)}</span><b>{p.brief.headline}</b>
        </button>
      )}
    </section>
  )
}

/** 首页一句话简报：行情、流水线、运行中的实验，各一个数。 */
function Brief({ market, board, running }: { market: { c: number[] } | null; board: GradRow[] | null; running: number }) {
  const parts: string[] = []
  if (market && market.c.length > 24) {
    const c = market.c
    parts.push(`BTC 24 小时 ${fmtPct(c[c.length - 1] / c[c.length - 25] - 1, 2, true)}`)
  }
  if (board) {
    const at = (st: string) => board.filter((r) => r.stage === st).length
    const seg = [['backtest', '在回测'], ['paper', '在纸面模拟'], ['demo', '在模拟盘'], ['graduated', '已毕业']]
      .map(([k, l]) => [at(k), l] as const).filter(([n]) => n > 0).map(([n, l]) => `${n} 个${l}`)
    parts.push(seg.length ? `策略 ${seg.join('、')}` : '还没有策略进入流水线')
  }
  parts.push(running ? `${running} 个实验在跑` : '没有实验在跑')
  return <p className="home-brief">{parts.join(' · ')}</p>
}
