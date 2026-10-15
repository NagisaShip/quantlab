import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { ago, fmtDate, fmtNum, fmtPct, tone, v3, type BacktestRow } from '../api2'
import { Callout, EmptyState, Heading, Loading, Panel, Tag } from '../components/ui'
import { readCompare, writeCompare } from './BacktestDetail'
import type { CompareItem } from '../apiCompare'
import { CompareTab } from './backtests/CompareTab'
import { CompareView } from './backtests/CompareView'
import { HistoryTab } from './backtests/HistoryTab'
import { NewTab } from './backtests/NewTab'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

const TABS = [['', '记录'], ['compare', '策略对比'], ['new', '新建回测'], ['history', '历史实验']] as const

export default function BacktestListPage({ slug: arg, onNavigate, notify }: PageProps & { slug?: string }) {
  const tab = arg === 'compare' || arg === 'new' || arg === 'history' ? arg : ''
  const tabs = (
    <div className="bt-tabs" role="tablist">
      {TABS.map(([k, label]) => (
        <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => onNavigate(k ? `backtests/${k}` : 'backtests')}>{label}</button>
      ))}
    </div>
  )
  if (tab) {
    return (
      <>
        <Heading title="回测" />
        {tabs}
        {tab === 'compare' && <CompareTab notify={notify} onNavigate={onNavigate} />}
        {tab === 'new' && <NewTab notify={notify} onNavigate={onNavigate} />}
        {tab === 'history' && <HistoryTab />}
      </>
    )
  }
  return <Records slug={arg} onNavigate={onNavigate} notify={notify} tabs={tabs} />
}

function Records({ slug, onNavigate, notify, tabs }: Pick<PageProps, 'onNavigate' | 'notify'> & { slug?: string; tabs: ReactNode }) {
  const [rows, setRows] = useState<BacktestRow[] | null>(null)
  const [selected, setSelected] = useState<string[]>(() => readCompare())
  const [compare, setCompare] = useState<CompareItem[] | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    v3.backtests(slug, 300).then((r) => setRows(r.backtests)).catch((e) => setError(e.message))
  }, [slug])
  useEffect(load, [load])

  const toggle = (id: string) => {
    const next = selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id].slice(-8)
    setSelected(next)
    writeCompare(next)
  }

  const runCompare = async () => {
    if (selected.length < 2) {
      notify('至少选择 2 次回测', true)
      return
    }
    try {
      const r = await v3.compare(selected)
      setCompare(r.items)
      window.scrollTo({ top: 0, behavior: 'smooth' })
    } catch (e) {
      notify((e as Error).message, true)
      setSelected([])
      writeCompare([])
    }
  }

  const remove = async (id: string) => {
    if (!await appConfirm('删除这条回测记录？')) return
    await v3.deleteBacktest(id)
    toggleOff(id)
    load()
  }
  const toggleOff = (id: string) => {
    const next = selected.filter((x) => x !== id)
    setSelected(next)
    writeCompare(next)
  }

  return (
    <>
      <Heading
        title={slug ? '这个策略的回测记录' : '回测'}
        actions={
          <div className="button-row">
            {slug && <button className="button" onClick={() => onNavigate(`editor/${slug}`)}>回到编辑器</button>}
            {slug && <button className="button" onClick={() => onNavigate('backtests')}>全部回测</button>}
            <button className="button primary" disabled={selected.length < 2} onClick={runCompare}>对比所选（{selected.length}）</button>
          </div>
        }
      />
      {!slug && tabs}
      {error && <Callout tone="fail">{error}</Callout>}

      {compare && (
        <Panel title="回测对比" subtitle="累计收益率曲线；不同区间的回测只能看形状，不能直接比较数值" actions={<button className="button small" onClick={() => setCompare(null)}>收起</button>}>
          <CompareView items={compare} />
        </Panel>
      )}

      <Panel title={`记录（${rows?.length ?? 0}）`} flush>
        {!rows ? <Loading /> : !rows.length ? (
          <EmptyState glyph="∿" title="还没有回测记录" action={<button className="button primary" onClick={() => onNavigate('strategies')}>去我的策略</button>}>
            在策略编辑器里点击「运行回测」，结果会出现在这里。
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th /><th>策略</th><th>区间</th><th>数据</th><th className="right">收益</th><th className="right">基准</th><th className="right">超额</th><th className="right">回撤</th><th className="right">夏普</th><th className="right">交易</th><th>运行</th><th /></tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td><input type="checkbox" checked={selected.includes(r.id)} onChange={() => toggle(r.id)} aria-label="加入对比" style={{ margin: 0, width: 'auto' }} /></td>
                    <td className="wrap"><a className="link-strong" href={`#/backtest/${r.id}`}>{r.strategy_name}</a><small>{r.version}{r.label ? ` · ${r.label}` : ''}</small></td>
                    <td>{fmtDate(r.start_ts)}<small>→ {fmtDate(r.end_ts)} · {r.timeframe}</small></td>
                    <td>{r.data_sources.map((s) => <Tag key={s} tone={s === 'synthetic' ? 'warn' : 'ok'}>{s === 'synthetic' ? '合成' : s === 'exchange' ? '真实' : s}</Tag>)}</td>
                    <td className={`right ${tone(r.metrics.total_return)}`}>{fmtPct(r.metrics.total_return, 2, true)}</td>
                    <td className="right">{fmtPct(r.metrics.benchmark_return, 2, true)}</td>
                    <td className={`right ${tone(r.metrics.excess_return)}`}>{fmtPct(r.metrics.excess_return, 2, true)}</td>
                    <td className="right">{fmtPct(r.metrics.max_drawdown)}</td>
                    <td className="right">{fmtNum(r.metrics.sharpe)}</td>
                    <td className="right">{r.metrics.trades ?? '—'}</td>
                    <td>{ago(r.created_at)}{r.status !== 'done' && <small className="negative">有错误</small>}</td>
                    <td><button className="button small danger" onClick={() => remove(r.id)}>删除</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </>
  )
}
