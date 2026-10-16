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
import { store } from '../components/prefs'

type Group = 'chat' | 'strategy' | 'none'
interface RowGroup { key: string; title: string; link?: string; rows: BacktestRow[]; best: number | null }

/** 按对话 / 策略分组：AI 研究室的回测按来源对话归在一起，其他回测按策略名。组按最近一次回测排序。 */
function groupRows(rows: BacktestRow[], by: Group): RowGroup[] {
  if (by === 'none') return [{ key: 'all', title: '全部', rows, best: null }]
  const map = new Map<string, RowGroup>()
  for (const r of rows) {
    const fromLab = !!r.lab_project_id || /^策略室|^分段回测/.test(r.label || '')
    const key = by === 'chat'
      ? (r.lab_project_id ? `lab:${r.lab_project_id}` : fromLab ? 'lab:?' : `other:${r.slug || r.strategy_name}`)
      : `s:${r.slug || r.strategy_name}`
    const title = by === 'chat'
      ? (r.lab_project_id ? `对话 · ${r.lab_title || r.strategy_name}` : fromLab ? 'AI 研究室（较早的记录，未记下对话）' : `策略 · ${r.strategy_name}`)
      : r.strategy_name
    const g = map.get(key) ?? { key, title, link: r.lab_project_id ? `#/lab/${r.lab_project_id}` : undefined, rows: [], best: null }
    g.rows.push(r)
    const sh = r.metrics.sharpe
    if (sh !== null && sh !== undefined && (g.best === null || sh > g.best)) g.best = sh
    map.set(key, g)
  }
  return [...map.values()].sort((a, b) => Math.max(...b.rows.map((r) => r.created_at)) - Math.max(...a.rows.map((r) => r.created_at)))
}

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
  const [group, setGroup] = useState<Group>(() => (store.get('qa.bt.group', 'chat') as Group))
  const [manage, setManage] = useState(false)
  const [picked, setPicked] = useState<string[]>([])
  const [folded, setFolded] = useState<string[]>([])

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
  const removeMany = async () => {
    if (!picked.length || !await appConfirm(`删除选中的 ${picked.length} 条回测记录？删除后不能恢复。`)) return
    try {
      const r = await v3.deleteBacktests(picked)
      notify(`已删除 ${r.deleted} 条回测记录`)
      const next = selected.filter((x) => !picked.includes(x))
      setSelected(next); writeCompare(next); setPicked([]); setManage(false); load()
    } catch (e) { notify((e as Error).message, true) }
  }
  const groups = groupRows(rows ?? [], group)
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

      <Panel title={`记录（${rows?.length ?? 0}）`} flush actions={
        <div className="button-row bt-manage">
          <label className="bt-group">分组
            <select value={group} onChange={(e) => { setGroup(e.target.value as Group); store.set('qa.bt.group', e.target.value) }}>
              <option value="chat">按对话</option><option value="strategy">按策略</option><option value="none">不分组</option>
            </select>
          </label>
          {manage ? <>
            <span className="muted small">已选 {picked.length}</span>
            <button className="button small" onClick={() => setPicked(picked.length === (rows?.length ?? 0) ? [] : (rows ?? []).map((r) => r.id))}>{picked.length === (rows?.length ?? 0) && picked.length ? '全不选' : '全选'}</button>
            <button className="button small danger" disabled={!picked.length} onClick={removeMany}>删除所选</button>
            <button className="button small ghost" onClick={() => { setManage(false); setPicked([]) }}>完成</button>
          </> : <button className="button small" onClick={() => setManage(true)} disabled={!rows?.length}>批量管理</button>}
        </div>
      }>
        {!rows ? <Loading /> : !rows.length ? (
          <EmptyState glyph="∿" title="还没有回测记录" action={<button className="button primary" onClick={() => onNavigate('strategies')}>去我的策略</button>}>
            在策略编辑器里点击「运行回测」，结果会出现在这里。
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="bt-table">
              <thead>
                <tr><th /><th>策略</th><th>区间</th><th>数据</th><th className="right">收益</th><th className="right">基准</th><th className="right">超额</th><th className="right">回撤</th><th className="right">夏普</th><th className="right">交易</th><th>运行</th><th /></tr>
              </thead>
              {groups.map((g) => {
                const ids = g.rows.map((r) => r.id)
                const all = ids.every((x) => picked.includes(x))
                const closed = folded.includes(g.key)
                return (
                  <tbody key={g.key} className="bt-group-body">
                    {group !== 'none' && (
                      <tr className="bt-group-row">
                        <td>{manage && <input type="checkbox" checked={all} aria-label={`选择「${g.title}」的全部回测`} style={{ margin: 0, width: 'auto' }}
                          onChange={() => setPicked(all ? picked.filter((x) => !ids.includes(x)) : [...new Set([...picked, ...ids])])} />}</td>
                        <td colSpan={11}>
                          <button type="button" className="bt-group-title" aria-expanded={!closed} onClick={() => setFolded(closed ? folded.filter((x) => x !== g.key) : [...folded, g.key])}>
                            <svg className={closed ? '' : 'up'} width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
                            <b>{g.title}</b><span className="muted small">{g.rows.length} 次回测{g.best !== null ? ` · 最佳夏普 ${fmtNum(g.best)}` : ''}</span>
                          </button>
                          {g.link && <a className="link small" href={g.link}>打开对话</a>}
                        </td>
                      </tr>
                    )}
                    {!closed && g.rows.map((r) => (
                      <tr key={r.id} className={picked.includes(r.id) ? 'picked' : undefined}>
                        <td>{manage
                          ? <input type="checkbox" checked={picked.includes(r.id)} onChange={() => setPicked(picked.includes(r.id) ? picked.filter((x) => x !== r.id) : [...picked, r.id])} aria-label="选择这条回测" style={{ margin: 0, width: 'auto' }} />
                          : <input type="checkbox" checked={selected.includes(r.id)} onChange={() => toggle(r.id)} aria-label="加入对比" style={{ margin: 0, width: 'auto' }} />}</td>
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
                )
              })}
            </table>
          </div>
        )}
      </Panel>
    </>
  )
}
