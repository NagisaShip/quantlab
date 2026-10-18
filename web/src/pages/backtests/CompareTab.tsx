/** 策略对比：几个策略在同一份冻结条件（标的、区间、资金、成本）下各跑一次，曲线与指标并排。 */
import { useCallback, useEffect, useState } from 'react'
import { ago, v3, type StrategyFile } from '../../api/research'
import { compareApi, type CompareBatch, type CompareItem } from '../../apiCompare'
import { EmptyState, Loading, Panel } from '../../components/ui'
import type { PageProps } from '../types'
import { CompareView } from './CompareView'
import { DEFAULT_DRAFT, SettingsForm, toSpec, type SpecDraft } from './SettingsForm'
import { appConfirm } from '../../components/dialog'

export function CompareTab({ notify, onNavigate }: Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [batches, setBatches] = useState<CompareBatch[] | null>(null)
  const [strategies, setStrategies] = useState<StrategyFile[]>([])
  const [activeId, setActiveId] = useState('')
  const [view, setView] = useState<{ batch: CompareBatch; items: CompareItem[]; stress_items: CompareItem[] } | null>(null)
  const [creating, setCreating] = useState(false)
  const [picked, setPicked] = useState<string[]>([])
  const [draft, setDraft] = useState<SpecDraft>(DEFAULT_DRAFT)
  const [stress, setStress] = useState(true)
  const [progress, setProgress] = useState('')

  const load = useCallback(async () => {
    const r = await compareApi.list()
    setBatches(r.batches)
    return r.batches
  }, [])
  useEffect(() => {
    load().then((b) => { if (!b.length) setCreating(true); else setActiveId((x) => x || b[0].id) }).catch((e: Error) => notify(e.message, true))
    v3.strategies().then((r) => setStrategies(r.strategies)).catch(() => undefined)
  }, [load, notify])
  useEffect(() => {
    if (!activeId) { setView(null); return }
    compareApi.get(activeId).then(setView).catch((e: Error) => notify(e.message, true))
  }, [activeId, notify])

  const run = async () => {
    if (picked.length < 2) return
    try {
      setProgress('冻结条件…')
      const { batch } = await compareApi.create({ slugs: picked, spec: toSpec(draft), stress })
      const jobs = batch.members.flatMap((m) => [{ m, s: false }, ...(batch.stress_spec ? [{ m, s: true }] : [])])
      for (let i = 0; i < jobs.length; i++) {
        const { m, s } = jobs[i]
        setProgress(`回测 ${i + 1}/${jobs.length}：${m.name}${s ? '（2× 成本）' : ''}`)
        const r = await v3.runBacktest({ slug: m.slug, ...(s ? batch.stress_spec : batch.spec), label: `对比 · ${batch.name}${s ? ' · 2×成本' : ''}` })
        if (r.id) await compareApi.addResult(batch.id, m.slug, r.id, s)
      }
      setCreating(false)
      setPicked([])
      await load()
      setActiveId(batch.id)
      setView(await compareApi.get(batch.id))
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setProgress('')
    }
  }
  const remove = async (id: string) => {
    if (!await appConfirm('删除这个对比批次？回测记录会保留。')) return
    await compareApi.remove(id)
    const b = await load()
    setActiveId(b[0]?.id ?? '')
  }

  const spec = view?.batch.spec
  return (
    <div className="bt-split">
      <aside className="bt-side">
        <button className="button small primary" style={{ width: '100%' }} onClick={() => { setCreating(true); setActiveId('') }}>新建对比</button>
        <div className="bt-side-list">
          {!batches ? <Loading /> : batches.map((b) => (
            <button key={b.id} className={`bt-side-item${b.id === activeId && !creating ? ' on' : ''}`} onClick={() => { setCreating(false); setActiveId(b.id) }}>
              <span>{b.name}</span><small>{b.members.length} 个策略 · {ago(b.created_at)}</small>
            </button>
          ))}
        </div>
      </aside>

      <div className="bt-main">
        {creating ? (
          <Panel title="新建对比">
            <div className="bt-label">选择 2~8 个策略</div>
            {!strategies.length ? <div className="muted small">还没有策略。<a href="#/strategies">去新建</a></div> : (
              <div className="bt-picks">
                {strategies.map((s) => (
                  <label key={s.slug} className={`bt-pick${picked.includes(s.slug) ? ' on' : ''}`}>
                    <input type="checkbox" checked={picked.includes(s.slug)} disabled={!picked.includes(s.slug) && picked.length >= 8}
                      onChange={(e) => setPicked(e.target.checked ? [...picked, s.slug] : picked.filter((x) => x !== s.slug))} />
                    <span>{s.name}</span><small>{s.kind === 'dsl' ? '规则' : '代码'} · {s.version}</small>
                  </label>
                ))}
              </div>
            )}
            <div className="bt-label">统一条件（所有策略用同一份行情、资金与成本）</div>
            <SettingsForm value={draft} onChange={setDraft} />
            <label className="bt-check"><input type="checkbox" checked={stress} onChange={(e) => setStress(e.target.checked)} />同时跑一组 2× 手续费与滑点的压力回测</label>
            <div className="button-row" style={{ marginTop: 14 }}>
              <button className="button primary" disabled={picked.length < 2 || !!progress} onClick={run}>{progress || `开始对比（${picked.length}）`}</button>
              {batches && batches.length > 0 && <button className="button" disabled={!!progress} onClick={() => { setCreating(false); setActiveId(batches[0].id) }}>取消</button>}
            </div>
          </Panel>
        ) : !view ? (
          batches?.length ? <Loading /> : <EmptyState glyph="⚑" title="还没有对比" />
        ) : (
          <Panel title={view.batch.name} actions={<button className="button small danger" onClick={() => remove(view.batch.id)}>删除</button>}>
            <div className="bt-frozen">
              {spec?.securities?.join('、')} · {spec?.timeframe} · {spec?.start ? `${new Date(spec.start).toISOString().slice(0, 10)} → ` : `${spec?.bars_limit} 根 · 截至 `}{spec?.end ? new Date(spec.end).toISOString().slice(0, 10) : ''}
              {' · '}资金 {spec?.initial_cash} · 手续费 {spec?.taker_fee} · 滑点 {spec?.slippage_bps}bp · 条件指纹 {view.batch.digest}
            </div>
            <CompareView items={view.items} stress={view.stress_items} />
            <div className="bt-members">
              {view.batch.members.map((m) => m.backtest_id && (
                <button key={m.slug} className="link" onClick={() => onNavigate(`backtest/${m.backtest_id}`)}>{m.name} 的回测详情</button>
              ))}
            </div>
          </Panel>
        )}
      </div>
    </div>
  )
}
