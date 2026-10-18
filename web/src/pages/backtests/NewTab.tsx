/** 新建回测：选策略、设条件、运行，完成后打开回测详情。（原「单策略实验」并入这里） */
import { useEffect, useState } from 'react'
import { v3, type StrategyFile } from '../../api/research'
import { Panel } from '../../components/ui'
import type { PageProps } from '../types'
import { DEFAULT_DRAFT, SettingsForm, toSpec, type SpecDraft } from './SettingsForm'

export function NewTab({ notify, onNavigate }: Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [strategies, setStrategies] = useState<StrategyFile[]>([])
  const [slug, setSlug] = useState('')
  const [draft, setDraft] = useState<SpecDraft>(DEFAULT_DRAFT)
  const [running, setRunning] = useState(false)

  useEffect(() => { v3.strategies().then((r) => setStrategies(r.strategies)).catch((e: Error) => notify(e.message, true)) }, [notify])
  const pick = (s: string) => {
    setSlug(s)
    const st = strategies.find((x) => x.slug === s)
    if (st) setDraft((d) => ({ ...d, securities: st.securities.join(', '), timeframe: st.timeframe }))
  }
  const run = async () => {
    setRunning(true)
    try {
      const r = await v3.runBacktest({ slug, ...toSpec(draft) })
      if (r.id) onNavigate(`backtest/${r.id}`)
      else notify('回测已完成，但没有保存记录', true)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setRunning(false)
    }
  }
  return (
    <Panel title="新建回测">
      <div className="bt-form" style={{ marginBottom: 12 }}>
        <label className="wide">策略
          <select value={slug} onChange={(e) => pick(e.target.value)}>
            <option value="">选择一个策略</option>
            {strategies.map((s) => <option key={s.slug} value={s.slug}>{s.name}（{s.version}）</option>)}
          </select>
        </label>
      </div>
      <SettingsForm value={draft} onChange={setDraft} />
      <div className="button-row" style={{ marginTop: 14 }}>
        <button className="button primary" disabled={!slug || running} onClick={run}>{running ? '回测中…' : '运行回测'}</button>
        {!strategies.length && <a className="button" href="#/strategies">先新建策略</a>}
      </div>
    </Panel>
  )
}
