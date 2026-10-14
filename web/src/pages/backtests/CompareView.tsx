/** 回测对比视图：累计收益率曲线叠加 + 指标并排（最好的一格加粗）。记录页与策略对比共用。 */
import { fmtNum, fmtPct, type FullMetrics } from '../../api2'
import type { CompareItem } from '../../apiCompare'
import { LineChart, seriesColor } from '../../components/charts2'

const METRICS: [keyof FullMetrics, string, 'pct' | 'num' | 'int'][] = [
  ['total_return', '策略收益', 'pct'],
  ['annualized_return', '年化收益', 'pct'],
  ['benchmark_return', '基准收益', 'pct'],
  ['excess_return', '超额收益', 'pct'],
  ['max_drawdown', '最大回撤', 'pct'],
  ['sharpe', '夏普', 'num'],
  ['sortino', '索提诺', 'num'],
  ['alpha', 'Alpha', 'num'],
  ['beta', 'Beta', 'num'],
  ['information_ratio', '信息比率', 'num'],
  ['win_rate', '胜率', 'pct'],
  ['trades', '交易次数', 'int'],
  ['total_fees', '手续费', 'num'],
]

export function CompareView({ items, stress }: { items: CompareItem[]; stress?: CompareItem[] }) {
  if (!items.length) return null
  const longest = items.reduce((a, b) => (b.timestamps.length > a.timestamps.length ? b : a), items[0])
  const stressOf = (c: CompareItem) => stress?.find((s) => s.strategy_name === c.strategy_name)
  return (
    <>
      <LineChart
        timestamps={longest.timestamps}
        series={items.map((c, i) => ({
          name: `${c.strategy_name}${c.version ? ` · ${c.version}` : ''}`,
          values: c.timestamps.length === longest.timestamps.length ? c.returns : longest.timestamps.map((t) => {
            const k = c.timestamps.findIndex((x) => x >= t)
            return k >= 0 ? c.returns[k] : NaN
          }),
          color: seriesColor(i),
        }))}
        height={300}
      />
      <div className="table-wrap" style={{ marginTop: 12 }}>
        <table>
          <thead>
            <tr><th>指标</th>{items.map((c, i) => <th key={c.id} className="right"><i className="dot" style={{ background: seriesColor(i) }} /> {c.strategy_name}<small>{c.version}</small></th>)}</tr>
          </thead>
          <tbody>
            <tr><td>参数</td>{items.map((c) => <td key={c.id} className="right wrap mono small">{Object.entries(c.params || {}).map(([k, v]) => `${k}=${v}`).join(' ') || '—'}</td>)}</tr>
            {METRICS.map(([k, label, f]) => {
              const vals = items.map((c) => c.metrics[k] as number | null | undefined)
              const nums = vals.filter((v): v is number => typeof v === 'number')
              const best = k === 'max_drawdown' || k === 'total_fees' ? Math.min(...nums) : Math.max(...nums)
              return (
                <tr key={k}>
                  <td>{label}</td>
                  {vals.map((v, i) => (
                    <td key={i} className={`right ${v === best && nums.length > 1 ? 'best-cell' : ''}`}>
                      {f === 'pct' ? fmtPct(v) : f === 'int' ? (v ?? '—') : fmtNum(v)}
                    </td>
                  ))}
                </tr>
              )
            })}
            {stress && stress.length > 0 && (
              <>
                <tr><td>2× 成本收益</td>{items.map((c) => <td key={c.id} className="right">{fmtPct(stressOf(c)?.metrics.total_return)}</td>)}</tr>
                <tr><td>2× 成本回撤</td>{items.map((c) => <td key={c.id} className="right">{fmtPct(stressOf(c)?.metrics.max_drawdown)}</td>)}</tr>
              </>
            )}
          </tbody>
        </table>
      </div>
    </>
  )
}
