/**
 * 因子评估报告的展示部件：指标条、入库检查、IC 衰减、分组收益、累计 IC 与多空净值。
 * AI 研究室的报告卡片（紧凑）和因子库详情（完整）共用。
 */
import type { AdmissionCheck, FactorReport } from '../apiFactors'
import type { FactorSummary } from '../apiLab'
import { LineChart } from './charts2'

export const f3 = (v: number | null | undefined, d = 3) => (v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(d))
export const fp = (v: number | null | undefined, d = 0) => (v === null || v === undefined || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(d)}%`)

export const STATUS_TONE: Record<string, string> = { candidate: '', active: 'ok', decaying: 'warn', retired: 'muted' }

/** 一行关键指标。 */
export function FactorStats({ s, compact }: { s: Partial<FactorSummary> | null | undefined; compact?: boolean }) {
  if (!s) return null
  const sig = s.ic_t !== null && s.ic_t !== undefined && s.t_required ? Math.abs(s.ic_t) >= s.t_required : null
  const cells: [string, string, string?, string?][] = [
    ['RankIC', f3(s.rank_ic), (s.rank_ic ?? 0) >= 0 ? 'up' : 'down', '因子值与下一期收益的秩相关（逐期平均）'],
    ['ICIR', f3(s.icir, 2), undefined, 'IC 均值 / IC 标准差'],
    ['t 值', `${f3(s.ic_t, 2)}`, sig === null ? undefined : sig ? 'ok' : 'bad', `门槛 ${f3(s.t_required, 2)}（已试 ${s.trials ?? 1} 次，按次数修正）`],
    ['多空夏普', f3(s.ls_sharpe, 2), (s.ls_sharpe ?? 0) >= 0 ? 'up' : 'down', `扣除 ${s.cost_bps ?? 10} bps 单边成本`],
    ['换手', fp(s.turnover), undefined, '每次调仓的组合权重变化'],
    ['样本外保留', fp(s.oos_retention), (s.oos_retention ?? 0) >= 0.5 ? 'ok' : 'bad', '样本外 RankIC / 样本内 RankIC（最后 30%）'],
  ]
  if (!compact) cells.push(['库内最大相关', f3(s.max_corr, 2), (s.max_corr ?? 0) > 0.7 ? 'bad' : undefined, '与已入库因子的截面秩相关'])
  return (
    <div className={`fr-stats${compact ? ' compact' : ''}`}>
      {cells.map(([k, v, t, tip]) => <div key={k} title={tip}><span>{k}</span><b className={t}>{v}</b></div>)}
    </div>
  )
}

export function Checks({ checks }: { checks: AdmissionCheck[] }) {
  return (
    <ul className="fr-checks">
      {checks.map((c) => (
        <li key={c.key} className={c.ok ? 'ok' : c.required ? 'bad' : 'warn'}>
          <span className="fr-mark">{c.ok ? '✓' : c.required ? '✗' : '!'}</span>
          <span>{c.label}{!c.required && <em>（参考）</em>}</span>
          <small>{c.detail}</small>
        </li>
      ))}
    </ul>
  )
}

/** 小柱状图（正负），用于衰减与分组。 */
export function MiniBars({ values, labels, fmt, title }: { values: (number | null)[]; labels: string[]; fmt: (v: number) => string; title: string }) {
  const vals = values.map((v) => (v === null || !Number.isFinite(v) ? 0 : v))
  const max = Math.max(...vals.map(Math.abs), 1e-9)
  const W = 240
  const H = 84
  const mid = 40
  const bw = W / Math.max(1, vals.length)
  return (
    <figure className="fr-mini">
      <figcaption>{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title}>
        <line x1={0} x2={W} y1={mid} y2={mid} className="fr-zero" />
        {vals.map((v, i) => {
          const h = (Math.abs(v) / max) * 32
          return (
            <g key={i}>
              <rect x={i * bw + bw * 0.2} width={bw * 0.6} y={v >= 0 ? mid - h : mid} height={Math.max(0.6, h)} className={v >= 0 ? 'pos' : 'neg'}>
                <title>{`${labels[i]}：${values[i] === null ? '—' : fmt(v)}`}</title>
              </rect>
              <text x={i * bw + bw / 2} y={H - 4} textAnchor="middle" className="fr-lab">{labels[i]}</text>
            </g>
          )
        })}
      </svg>
    </figure>
  )
}

export function DecayAndGroups({ report }: { report: Pick<FactorReport, 'decay' | 'groups'> }) {
  const g = report.groups
  return (
    <div className="fr-minis">
      {report.decay?.length > 0 && (
        <MiniBars title="IC 衰减（持有期）" values={report.decay.map((d) => d.rank_ic)} labels={report.decay.map((d) => `${d.horizon}`)}
          fmt={(v) => v.toFixed(3)} />
      )}
      {g?.mean?.length > 0 && (
        <MiniBars title={`分组收益（低 → 高，每期 bps）`} values={g.mean} labels={g.mean.map((_, i) => `G${i + 1}`)}
          fmt={(v) => `${v.toFixed(1)} bps`} />
      )}
    </div>
  )
}

/** 完整报告（因子库详情、因子评估器）。 */
export function FullReport({ report }: { report: FactorReport }) {
  const s = report.summary
  const ic = report.ic_series || []
  const nav = report.ls_nav || []
  const groupsNav = report.groups?.nav || []
  const p = report.panel
  return (
    <div className="fr-full">
      <FactorStats s={s} />
      <div className="fr-badges">
        <span className={`cb-badge ${report.admitted ? 'ok' : 'warn'}`}>{report.admitted ? '满足入库条件' : '未满足入库条件'}</span>
        <span className="cb-badge">{s.mode === 'cross' ? '截面口径' : '时序口径'} · 持有 {s.horizon} 期 · {s.periods} 个独立样本</span>
        <span className="cb-badge">方向：{s.direction}</span>
        {p?.synthetic && <span className="cb-badge warn">含合成数据</span>}
      </div>
      {(p?.warnings || []).length > 0 && <ul className="fr-warn">{p.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
      <div className="fr-grid">
        <section>
          <h4>入库检查</h4>
          <Checks checks={report.checks} />
        </section>
        <section>
          <h4>衰减与分组</h4>
          <DecayAndGroups report={report} />
        </section>
      </div>
      {ic.length > 2 && (
        <section>
          <h4>累计 RankIC</h4>
          <LineChart series={[{ name: '累计 RankIC', values: ic.map((x) => x.cum), area: true }]} timestamps={ic.map((x) => x.t)}
            format="num" height={200} zeroLine ariaLabel="累计 RankIC" />
        </section>
      )}
      {nav.length > 2 && (
        <section>
          <h4>多空组合净值（扣成本）</h4>
          <LineChart series={[{ name: '多空', values: nav.map((x) => x.nav - 1) }]} timestamps={nav.map((x) => x.t)} format="pct" height={200} ariaLabel="多空净值" />
        </section>
      )}
      {groupsNav.length > 2 && (
        <section>
          <h4>分组净值</h4>
          <LineChart series={Object.keys(groupsNav[0]).filter((k) => k !== 't').map((k) => ({ name: k.toUpperCase(), values: groupsNav.map((r) => r[k] - 1) }))}
            timestamps={groupsNav.map((r) => r.t)} format="pct" height={220} ariaLabel="分组净值" />
        </section>
      )}
      <div className="fr-grid">
        {report.regimes?.length > 0 && (
          <section>
            <h4>分市场状态</h4>
            <table className="fr-table">
              <thead><tr><th>状态</th><th className="right">RankIC</th><th className="right">期数</th></tr></thead>
              <tbody>{report.regimes.map((r) => (
                <tr key={r.regime}><td>{r.regime}</td><td className={`right ${(r.rank_ic ?? 0) >= 0 ? 'positive' : 'negative'}`}>{f3(r.rank_ic)}</td><td className="right">{r.periods}</td></tr>
              ))}</tbody>
            </table>
          </section>
        )}
        <section>
          <h4>稳健性</h4>
          <table className="fr-table">
            <tbody>
              <tr><td>样本内 / 样本外 RankIC</td><td className="right">{f3(s.is_rank_ic)} / {f3(s.oos_rank_ic)}</td></tr>
              <tr><td>成交额前一半的币</td><td className="right">{f3(s.sub_rank_ic)}</td></tr>
              <tr><td>IC 为正比例</td><td className="right">{fp(s.ic_win)}</td></tr>
              <tr><td>多空年化 / 最大回撤</td><td className="right">{fp(s.ls_return, 1)} / {fp(s.ls_max_drawdown, 1)}</td></tr>
              <tr><td>覆盖率</td><td className="right">{fp(s.coverage)}</td></tr>
              {report.correlations?.slice(0, 3).map((c) => <tr key={c.name}><td>与「{c.name}」相关</td><td className="right">{f3(c.corr, 2)}</td></tr>)}
            </tbody>
          </table>
        </section>
      </div>
      {p && (
        <div className="hint">
          {p.symbols.length} 个标的 · {p.timeframe} · {p.bars} 根 K 线
          {p.start && p.end ? ` · ${new Date(p.start).toISOString().slice(0, 10)} → ${new Date(p.end).toISOString().slice(0, 10)}` : ''}
          {Object.keys(p.field_sources || {}).length > 0 && ` · 字段来源：${Object.entries(p.field_sources).map(([k, v]) => `${k}=${v}`).join('，')}`}
        </div>
      )}
    </div>
  )
}
