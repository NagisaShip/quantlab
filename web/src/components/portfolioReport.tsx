/**
 * 组合评估报告的展示部件：指标条、净值与回撤、权重与风险贡献、持仓变化、压力测试、有效前沿、相关性。
 * AI 研究室的报告卡片（紧凑）、组合与风控页面（完整）共用。
 */
import { Fragment, useMemo, useState } from 'react'
import type { PortfolioReport, PortfolioSummary, StressRow } from '../apiPortfolio'
import { Checks, f3, fp } from './factorReport'
import { Heatmap, LineChart, seriesColor } from './charts2'

export const SCHEME_STATUS_TONE: Record<string, string> = { candidate: '', active: 'ok', retired: 'muted' }
const TRIGGER_LABEL: Record<string, string> = { vol_target: '目标波动降仓', var_limit: 'VaR 上限降仓', dd_stop: '回撤止损', cppi: 'CPPI 降仓' }

export function PortfolioStats({ s, compact }: { s: Partial<PortfolioSummary> | null | undefined; compact?: boolean }) {
  if (!s) return null
  const beat = s.beat_ew ?? null
  const cells: [string, string, string?, string?][] = [
    ['夏普', f3(s.sharpe, 2), (s.sharpe ?? 0) >= 0 ? 'up' : 'down', '滚动组合回测，扣换手成本'],
    [compact ? '超等权' : '夏普超出等权', f3(beat, 2), beat === null ? undefined : beat >= 0 ? 'ok' : 'bad', `同期等权夏普 ${f3(s.ew_sharpe, 2)}`],
    ['年化', fp(s.ann_return, 1), (s.ann_return ?? 0) >= 0 ? 'up' : 'down'],
    ['波动', fp(s.ann_vol, 1), undefined, '年化波动'],
    ['最大回撤', fp(s.max_drawdown, 1), undefined, `等权 ${fp(s.ew_max_drawdown, 1)}`],
    [compact ? '有效持仓' : '有效持仓数', f3(s.effective_n, 1), undefined, '1 / Σw²，越大越分散'],
  ]
  if (!compact) {
    cells.push(['CVaR 95%', fp(s.cvar_95, 2), undefined, '最差 5% 的 K 线平均亏损'])
  }
  return (
    <div className={`fr-stats${compact ? ' compact' : ''}`}>
      {cells.map(([k, v, t, tip]) => <div key={k} title={tip}><span>{k}</span><b className={t}>{v}</b></div>)}
    </div>
  )
}

export function NavCharts({ nav, height = 220, width }: { nav: PortfolioReport['nav']; height?: number; width?: number }) {
  if (!nav || nav.length < 3) return null
  return (
    <>
      <LineChart series={[{ name: '组合', values: nav.map((x) => x.nav - 1) }, { name: '等权', values: nav.map((x) => x.ew - 1), dashed: true }]}
        timestamps={nav.map((x) => x.t)} format="pct" height={height} width={width} ariaLabel="组合与等权净值" />
      <LineChart series={[{ name: '回撤', values: nav.map((x) => -x.dd), color: 'var(--down)', area: true }]}
        timestamps={nav.map((x) => x.t)} format="pct" height={Math.max(100, Math.round(height * 0.45))} width={width} ariaLabel="组合回撤" />
    </>
  )
}

/** 最新权重与风险贡献并排：权重大但风险贡献更大的币一眼可见。 */
export function WeightsTable({ latest }: { latest: NonNullable<PortfolioReport['latest']> }) {
  const rows = Object.keys(latest.weights).sort((a, b) => latest.weights[b] - latest.weights[a])
  const maxW = Math.max(...rows.map((s) => Math.abs(latest.weights[s])), ...rows.map((s) => Math.abs(latest.risk_contrib[s] ?? 0)), 1e-9)
  return (
    <div className="pf-weights">
      <div className="pf-wrow head"><span>标的</span><span>权重</span><span>风险贡献</span><span className="num">年化波动</span>{latest.expected && <span className="num">预期年化</span>}</div>
      {rows.map((s) => (
        <div key={s} className="pf-wrow">
          <span className="pf-sym">{s}</span>
          <span className="pf-bar"><i style={{ width: `${(Math.abs(latest.weights[s]) / maxW) * 100}%` }} /><b className="num">{fp(latest.weights[s], 1)}</b></span>
          <span className="pf-bar rc"><i style={{ width: `${(Math.abs(latest.risk_contrib[s] ?? 0) / maxW) * 100}%` }} /><b className="num">{fp(latest.risk_contrib[s], 1)}</b></span>
          <span className="num muted">{fp(latest.vol[s], 0)}</span>
          {latest.expected && <span className={`num ${(latest.expected[s] ?? 0) >= 0 ? 'positive' : 'negative'}`}>{fp(latest.expected[s], 1)}</span>}
        </div>
      ))}
      {latest.cash !== null && latest.cash > 0.005 && (
        <div className="pf-wrow"><span className="pf-sym muted">现金</span><span className="pf-bar cash"><i style={{ width: `${(latest.cash / maxW) * 100}%` }} /><b className="num">{fp(latest.cash, 1)}</b></span><span /><span /></div>
      )}
      {latest.notes.length > 0 && <div className="hint">这一期触发了：{latest.notes.map((n) => TRIGGER_LABEL[n] ?? n).join('、')}</div>}
    </div>
  )
}

/** 持仓随时间的变化（堆叠面积）。 */
export function WeightsArea({ rows, height = 160 }: { rows: Record<string, number>[]; height?: number }) {
  const syms = useMemo(() => {
    const tot: Record<string, number> = {}
    rows.forEach((r) => Object.entries(r).forEach(([k, v]) => { if (k !== 't') tot[k] = (tot[k] ?? 0) + Math.abs(v) }))
    return Object.keys(tot).sort((a, b) => tot[b] - tot[a])
  }, [rows])
  const [hover, setHover] = useState<string | null>(null)
  if (rows.length < 2 || !syms.length) return null
  const W = 600
  const H = height
  const x = (i: number) => (i / (rows.length - 1)) * W
  const stack = rows.map(() => 0)
  const paths = syms.map((s) => {
    const lo = [...stack]
    rows.forEach((r, i) => { stack[i] += Math.max(0, r[s] ?? 0) })
    const top = rows.map((_, i) => `${x(i).toFixed(1)},${(H - stack[i] * H).toFixed(1)}`)
    const bot = rows.map((_, i) => `${x(i).toFixed(1)},${(H - lo[i] * H).toFixed(1)}`).reverse()
    return { s, d: `M${top.join(' L')} L${bot.join(' L')} Z` }
  })
  return (
    <figure className="pf-area">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="持仓变化">
        {paths.map((p, i) => (
          <path key={p.s} d={p.d} style={{ fill: seriesColor(i) }} opacity={hover === null || hover === p.s ? 0.75 : 0.25}
            onMouseEnter={() => setHover(p.s)} onMouseLeave={() => setHover(null)}><title>{p.s}</title></path>
        ))}
      </svg>
      <figcaption className="legend">{syms.slice(0, 12).map((s, i) => <span key={s}><i style={{ background: seriesColor(i) }} />{s.replace('/USDT', '')}</span>)}</figcaption>
    </figure>
  )
}

export function StressTable({ rows }: { rows: StressRow[] }) {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <table className="fr-table pf-stress">
      <thead><tr><th>情景</th><th>方法</th><th className="right">组合损益</th><th>拖累最大</th></tr></thead>
      <tbody>{rows.map((r) => (
        <Fragment key={r.id}>
          <tr className={Object.keys(r.shocks).length ? 'clickable' : ''} onClick={() => setOpen(open === r.id ? null : r.id)} title={r.note}>
            <td>{r.name}<span className="ml-sub">{r.note}</span></td>
            <td className="muted">{r.method}</td>
            <td className={`right num ${r.pnl >= 0 ? 'positive' : 'negative'}`}>{fp(r.pnl, 1)}</td>
            <td className="muted">{r.worst ? `${r.worst.replace('/USDT', '')} ${fp(r.worst_pnl, 1)}` : '—'}</td>
          </tr>
          {open === r.id && (
            <tr><td colSpan={4}><div className="pf-shocks">{Object.entries(r.shocks).map(([s, v]) => <span key={s}>{s.replace('/USDT', '')} <b className={v >= 0 ? 'positive' : 'negative'}>{fp(v, 0)}</b></span>)}</div></td></tr>
          )}
        </Fragment>
      ))}</tbody>
    </table>
  )
}

/** 有效前沿：前沿曲线、各币、当前组合与等权。 */
export function FrontierChart({ f }: { f: NonNullable<PortfolioReport['frontier']> }) {
  const [hover, setHover] = useState<string | null>(null)
  const pts = [...f.points, f.current, f.equal, ...f.assets]
  const xs = pts.map((p) => p.vol)
  const ys = pts.map((p) => p.ret)
  const W = 420
  const H = 260
  const P = { l: 50, r: 12, t: 12, b: 34 }
  const x0 = 0
  const x1 = Math.max(...xs) * 1.08 || 1
  const yl = Math.min(...ys, 0)
  const yh = Math.max(...ys, 0)
  const dy = (yh - yl) * 0.1 || 0.1
  const X = (v: number) => P.l + ((v - x0) / (x1 - x0)) * (W - P.l - P.r)
  const Y = (v: number) => P.t + (1 - (v - (yl - dy)) / (yh + dy - (yl - dy))) * (H - P.t - P.b)
  const line = f.points.map((p, i) => `${i ? 'L' : 'M'}${X(p.vol).toFixed(1)},${Y(p.ret).toFixed(1)}`).join(' ')
  const ticks = (lo: number, hi: number) => Array.from({ length: 5 }, (_, i) => lo + ((hi - lo) * i) / 4)
  return (
    <figure className="pf-frontier">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="有效前沿">
        {ticks(x0, x1).map((t) => <text key={'x' + t} className="chart-label" x={X(t)} y={H - P.b + 14} textAnchor="middle">{fp(t, 0)}</text>)}
        {ticks(yl - dy, yh + dy).map((t) => <g key={'y' + t}><line className="chart-gridline" x1={P.l} x2={W - P.r} y1={Y(t)} y2={Y(t)} /><text className="chart-label" x={P.l - 6} y={Y(t) + 4} textAnchor="end">{fp(t, 0)}</text></g>)}
        <text className="chart-label" x={(W + P.l) / 2} y={H - 4} textAnchor="middle">年化波动</text>
        <path d={line} fill="none" style={{ stroke: 'var(--accent)' }} strokeWidth={1.8} />
        {f.assets.map((a) => (
          <g key={a.symbol} onMouseEnter={() => setHover(`${a.symbol}：波动 ${fp(a.vol, 0)}，预期 ${fp(a.ret, 1)}`)} onMouseLeave={() => setHover(null)}>
            <circle cx={X(a.vol)} cy={Y(a.ret)} r={3.5} style={{ fill: 'var(--faint)' }} />
            <text className="chart-label" x={X(a.vol) + 5} y={Y(a.ret) - 4}>{a.symbol.replace('/USDT', '')}</text>
          </g>
        ))}
        <rect x={X(f.equal.vol) - 4} y={Y(f.equal.ret) - 4} width={8} height={8} style={{ fill: 'var(--bench)' }}
          onMouseEnter={() => setHover(`等权：波动 ${fp(f.equal.vol, 0)}，预期 ${fp(f.equal.ret, 1)}`)} onMouseLeave={() => setHover(null)} />
        <circle cx={X(f.current.vol)} cy={Y(f.current.ret)} r={6} style={{ fill: 'var(--amber)', stroke: 'var(--sheet)' }} strokeWidth={2}
          onMouseEnter={() => setHover(`当前组合：波动 ${fp(f.current.vol, 0)}，预期 ${fp(f.current.ret, 1)}`)} onMouseLeave={() => setHover(null)} />
      </svg>
      <figcaption className="legend">
        <span><i style={{ background: 'var(--accent)' }} />有效前沿</span><span><i style={{ background: 'var(--amber)' }} />当前组合</span>
        <span><i style={{ background: 'var(--bench)' }} />等权</span><span><i style={{ background: 'var(--faint)' }} />单个币</span>
      </figcaption>
      <div className="hint">{hover ?? `纵轴是${f.mu_source}，前沿按当前约束计算。`}</div>
    </figure>
  )
}

export function RiskBox({ r }: { r: NonNullable<PortfolioReport['risk']> }) {
  const cells: [string, string, string][] = [
    ['年化波动', fp(r.ann_vol, 1), '按当前风险模型估计'],
    ['单日 VaR 95%', fp(r.var_95_day_param, 2), '参数法：1.645 × 单日波动'],
    ['单根 VaR / CVaR 95%', `${fp(r.var_95_bar, 2)} / ${fp(r.cvar_95_bar, 2)}`, '历史模拟法：最近 lookback 根'],
    ['单根 VaR / CVaR 99%', `${fp(r.var_99_bar, 2)} / ${fp(r.cvar_99_bar, 2)}`, '历史模拟法'],
  ]
  return <div className="pf-risk">{cells.map(([k, v, tip]) => <div key={k} title={tip}><span>{k}</span><b className="num">{v}</b></div>)}</div>
}

export function PortfolioFullReport({ report }: { report: PortfolioReport }) {
  const s = report.summary
  const p = report.panel
  const trig = Object.entries(s.triggers || {})
  return (
    <div className="fr-full">
      <PortfolioStats s={s} />
      <div className="fr-badges">
        <span className={`cb-badge ${report.admitted ? 'ok' : 'warn'}`}>{report.admitted ? '满足启用条件' : '未满足启用条件'}</span>
        <span className="cb-badge">调仓 {s.rebalances} 次 · 平均换手 {fp(s.turnover)} · 平均仓位 {fp(s.avg_exposure)}</span>
        <span className="cb-badge">前半夏普 {f3(s.sharpe_h1, 2)} · 后半 {f3(s.sharpe_h2, 2)}</span>
        {trig.map(([k, n]) => <span key={k} className="cb-badge">{TRIGGER_LABEL[k] ?? k} {n} 次</span>)}
        {p?.synthetic && <span className="cb-badge warn">含合成数据</span>}
      </div>
      {(p?.warnings || []).length > 0 && <ul className="fr-warn">{p.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
      <section>
        <h4>净值与回撤（虚线为等权）</h4>
        <NavCharts nav={report.nav} />
      </section>
      <div className="fr-grid">
        {report.latest && (
          <section>
            <h4>最新权重与风险贡献</h4>
            <WeightsTable latest={report.latest} />
          </section>
        )}
        <section>
          <h4>检查</h4>
          <Checks checks={report.checks} />
          {report.risk && <><h4 style={{ marginTop: 14 }}>风险度量</h4><RiskBox r={report.risk} /></>}
        </section>
      </div>
      {report.weights.length > 2 && (
        <section>
          <h4>持仓变化</h4>
          <WeightsArea rows={report.weights} />
        </section>
      )}
      <div className="fr-grid">
        {report.stress && (
          <section>
            <h4>压力测试（按最新权重）</h4>
            <StressTable rows={report.stress} />
            <div className="hint">数据覆盖情景区间时直接重放历史；否则按各币对 BTC 的贝塔推算。点击行查看各币冲击。</div>
          </section>
        )}
        {report.frontier && (
          <section>
            <h4>有效前沿</h4>
            <FrontierChart f={report.frontier} />
          </section>
        )}
      </div>
      {report.latest?.bl && (
        <section>
          <h4>Black-Litterman：先验与后验（年化）</h4>
          <table className="fr-table">
            <thead><tr><th>标的</th><th className="right">先验（等权隐含）</th><th className="right">后验</th></tr></thead>
            <tbody>{Object.keys(report.latest.bl.prior).map((k) => (
              <tr key={k}><td>{k}</td><td className="right num">{fp(report.latest!.bl!.prior[k], 1)}</td><td className="right num">{fp(report.latest!.bl!.posterior[k], 1)}</td></tr>
            ))}</tbody>
          </table>
        </section>
      )}
      {report.risk?.corr && report.risk.corr.symbols.length <= 12 && (
        <section className="pf-corr">
          <h4>相关性（最近窗口，样本相关）</h4>
          <Heatmap rows={report.risk.corr.symbols.map((x) => x.replace('/USDT', ''))} cols={report.risk.corr.symbols.map((x) => x.replace('/USDT', ''))}
            values={report.risk.corr.values} rowLabel="" colLabel="" format="num" range={[-1, 1]} />
        </section>
      )}
    </div>
  )
}
