/** 历史实验（只读）：原「统一赛道」留下的单次实验与批次成绩，保留查看，不再新增。 */
import { Fragment, useEffect, useState } from 'react'
import { api, pct, ratio, shortTime, type Leaderboard } from '../../api/core'
import { EmptyState, Loading, Panel } from '../../components/ui'

type RunRow = Awaited<ReturnType<typeof api.runs>>['runs'][number]
type BatchRow = Awaited<ReturnType<typeof api.batches>>['batches'][number]

export function HistoryTab() {
  const [runs, setRuns] = useState<RunRow[] | null>(null)
  const [batches, setBatches] = useState<BatchRow[] | null>(null)
  const [open, setOpen] = useState('')
  const [board, setBoard] = useState<Leaderboard | null>(null)

  useEffect(() => {
    api.runs().then((r) => setRuns(r.runs)).catch(() => setRuns([]))
    api.batches().then((r) => setBatches(r.batches)).catch(() => setBatches([]))
  }, [])
  useEffect(() => {
    setBoard(null)
    if (open) api.leaderboard(open, '30').then(setBoard).catch(() => undefined)
  }, [open])

  return (
    <>
      <Panel title="统一赛道批次" subtitle="旧版统一赛道跑出的批次，只读保留" flush>
        {!batches ? <Loading /> : !batches.length ? <EmptyState title="没有历史批次" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>批次</th><th>行情</th><th className="right">策略数</th><th>时间</th></tr></thead>
            <tbody>
              {batches.map((b) => (
                <Fragment key={b.id}>
                  <tr className="clickable" onClick={() => setOpen(open === b.id ? '' : b.id)}>
                    <td className="mono small">{b.id.slice(0, 10)}<small>指纹 {b.track_digest?.slice(0, 10)}</small></td>
                    <td>{b.symbol} · {b.timeframe}<small>{b.source_label}</small></td>
                    <td className="right">{b.count}</td>
                    <td>{shortTime(b.created_at)}</td>
                  </tr>
                  {open === b.id && (
                    <tr><td colSpan={4} style={{ background: 'var(--sunken)' }}>
                      {!board ? <Loading /> : (
                        <table>
                          <thead><tr><th>策略</th><th className="right">30 天收益</th><th className="right">超额</th><th className="right">回撤</th><th className="right">夏普</th><th className="right">交易</th></tr></thead>
                          <tbody>{board.rows.map((r) => (
                            <tr key={r.strategy_id}><td>{r.name}<small>{r.is_benchmark ? '基准' : r.team}</small></td>
                              <td className="right">{pct(r.return)}</td><td className="right">{pct(r.excess_return)}</td>
                              <td className="right">{pct(r.max_drawdown)}</td><td className="right">{ratio(r.sharpe)}</td><td className="right">{r.trades ?? '—'}</td></tr>
                          ))}</tbody>
                        </table>
                      )}
                    </td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table></div>
        )}
      </Panel>
      <Panel title="单次实验" subtitle="旧版单策略实验的记录，只读保留" flush>
        {!runs ? <Loading /> : !runs.length ? <EmptyState title="没有历史实验" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>策略</th><th>行情</th><th className="right">收益</th><th className="right">回撤</th><th className="right">夏普</th><th className="right">交易</th><th>时间</th></tr></thead>
            <tbody>{runs.map((r) => (
              <tr key={r.id}><td>{r.strategy}<small>{r.scenario}</small></td><td>{r.symbol} · {r.timeframe}<small>{r.data_source}</small></td>
                <td className="right">{pct(r.metrics.total_return)}</td><td className="right">{pct(r.metrics.max_drawdown)}</td>
                <td className="right">{ratio(r.metrics.sharpe)}</td><td className="right">{r.metrics.trades}</td><td>{shortTime(r.created_at * 1000)}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </Panel>
    </>
  )
}
