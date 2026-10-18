import { fmtValue } from './protocol'

export interface StressCheck {
  key: string
  label: string
  ok: boolean
  value: string
  note?: string
}

export interface StressTestData {
  status?: 'running' | 'done' | 'error'
  v?: number
  verdict?: 'pass' | 'warn' | 'fail'
  score?: number
  checks?: StressCheck[]
  walk_forward?: {
    verdict?: string
    positive_share?: number
    segments?: { segment?: number; total_return?: number | null; sharpe?: number | null; max_drawdown?: number | null; trades?: number | null; data_role?: string }[]
    reconcile?: { reconstructs?: boolean }
  }
  stability?: { flags?: string[]; sensitivity?: { fragile?: boolean; note?: string; runs?: unknown[] } }
  cost?: { limit?: { pass_max?: number | null; fail_min?: number | null }; summary?: string; rows?: { scale?: number; passed?: boolean; total_return?: number | null; sharpe?: number | null }[] }
  segments?: number
  windows?: number
  cost_scales?: number[]
  error?: string
  generated_at?: number
  [k: string]: unknown
}

const VERDICT: Record<string, { label: string; tone: string }> = {
  pass: { label: '通过', tone: 'ok' },
  warn: { label: '有告警', tone: 'warn' },
  fail: { label: '未通过', tone: 'bad' },
}

/** 过拟合压力测试：对话流里的实时卡片（running → done/error 原地更新）。 */
export function StressTestCard({ data }: { data: StressTestData }) {
  if (data.status === 'running') {
    return (
      <section className="qb-block qb-t-stress_test wide qb-running anim-rise">
        <header className="qb-head">
          <span className="qb-kind">压力测试</span>
          <b className="qb-title">过拟合压力测试</b>
          <span className="cb-badge">进行中</span>
        </header>
        <div className="qb-pending">
          <span className="cb-spin" />
          正在跑滚动分段、参数敏感性与 {String((data.cost_scales || [1, 2, 3]).map((x) => `${x}×`).join(' / '))} 成本压力…
        </div>
      </section>
    )
  }
  if (data.status === 'error') {
    return (
      <section className="qb-block qb-t-stress_test wide anim-rise">
        <header className="qb-head"><span className="qb-kind">压力测试</span><b className="qb-title">过拟合压力测试</b><span className="cb-badge warn">失败</span></header>
        <div className="qb-callout tone-warn">{data.error || '压力测试执行失败'}</div>
      </section>
    )
  }
  const v = data.verdict ? VERDICT[data.verdict] : null
  const checks = data.checks || []
  const segs = data.walk_forward?.segments || []
  const costs = data.cost?.rows || []
  return (
    <section className="qb-block qb-t-stress_test wide anim-rise">
      <header className="qb-head">
        <span className="qb-kind">压力测试</span>
        <b className="qb-title">过拟合压力测试{data.v ? ` · v${data.v}` : ''}</b>
        {v && <span className={`qb-param-verdict ${v.tone}`}>{v.label}</span>}
        {typeof data.score === 'number' && <span className="qb-faint">得分 {Math.round(data.score * 100)}</span>}
      </header>

      <div className="qb-stress-checks">
        {checks.map((c) => (
          <div key={c.key} className={`qb-stress-check ${c.ok ? 'ok' : 'bad'}`} title={c.note}>
            <span>{c.ok ? '✓' : '✗'}</span>
            <b>{c.label}</b>
            <small>{c.value}</small>
          </div>
        ))}
      </div>

      <div className="qb-stress-grid">
        <div>
          <div className="qb-sub">滚动分段 <span className="qb-faint">正收益 {Math.round((data.walk_forward?.positive_share || 0) * 100)}%</span></div>
          <div className="qb-table-scroll" style={{ maxHeight: 190 }}>
            <table className="qb-compare">
              <thead><tr><th>#</th><th className="num">收益</th><th className="num">Sharpe</th><th className="num">回撤</th><th className="num">交易</th></tr></thead>
              <tbody>
                {segs.map((s, i) => (
                  <tr key={i}>
                    <td>{s.segment ?? i + 1}</td>
                    <td className={`num ${(s.total_return || 0) >= 0 ? 'up' : 'down'}`}>{fmtValue(s.total_return, 'pct_signed')}</td>
                    <td className="num">{fmtValue(s.sharpe, 'ratio')}</td>
                    <td className="num">{fmtValue(s.max_drawdown, 'pct')}</td>
                    <td className="num">{fmtValue(s.trades, 'int')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <div className="qb-sub">成本压力 <span className="qb-faint">{data.cost?.limit?.pass_max !== null && data.cost?.limit?.pass_max !== undefined ? `最高通过 ${data.cost.limit.pass_max}×` : ''}</span></div>
          <div className="qb-table-scroll" style={{ maxHeight: 190 }}>
            <table className="qb-compare">
              <thead><tr><th>成本</th><th className="num">收益</th><th className="num">Sharpe</th><th>结果</th></tr></thead>
              <tbody>
                {costs.map((r, i) => (
                  <tr key={i} className={r.passed ? '' : 'qb-muted'}>
                    <td>{r.scale}×</td>
                    <td className="num">{fmtValue(r.total_return, 'pct_signed')}</td>
                    <td className="num">{fmtValue(r.sharpe, 'ratio')}</td>
                    <td>{r.passed ? '✓ 达标' : '✗ 不达标'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {(data.stability?.flags || []).length > 0 && (
        <div className="qb-callout tone-warn" style={{ marginTop: 8 }}>
          <b>稳定性告警</b>
          <ul>{data.stability!.flags!.slice(0, 5).map((x, i) => <li key={i}>{x}</li>)}</ul>
        </div>
      )}
      {data.cost?.summary && <p className="qb-note">{data.cost.summary}</p>}
    </section>
  )
}
