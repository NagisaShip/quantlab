/**
 * 积木搭建：标的池 → 信号 → 组合构建 → 风控 → 执行，五块拼成一个策略，实时生成 SDK 代码。
 * 保存后就是一个普通策略，可以回测、交给 AI 改进、进入模拟。
 */
import { useEffect, useRef, useState } from 'react'
import { rd, type AssembleOptions, type Assembled, type Blocks } from '../apiRd'
import { CodeView } from '../components/CodeEditor'
import { Callout, Field, Heading, Loading, Panel } from '../components/ui'
import type { PageProps } from './types'

const DEFAULT: Blocks = {
  name: '积木策略', symbols: ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT'], timeframe: '4h',
  signal: { type: 'momentum', lookback: 20 }, build: { type: 'topk', k: 2 },
  risk: { max_weight: 0.5, cash: 0, stop_dd: 0 }, execution: '', every: 6,
}

export default function AssemblePage({ onNavigate, notify }: PageProps) {
  const [opt, setOpt] = useState<AssembleOptions | null>(null)
  const [b, setB] = useState<Blocks>(DEFAULT)
  const [universe, setUniverse] = useState('')
  const [out, setOut] = useState<Assembled | null>(null)
  const [err, setErr] = useState('')
  const [saving, setSaving] = useState(false)
  const seq = useRef(0)

  useEffect(() => { rd.assembleOptions().then(setOpt).catch((e: Error) => notify(e.message, true)) }, [notify])
  useEffect(() => {
    const my = ++seq.current
    const t = window.setTimeout(() => {
      rd.assemble(b).then((r) => { if (my === seq.current) { setOut(r); setErr('') } })
        .catch((e: Error) => { if (my === seq.current) setErr(e.message) })
    }, 250)
    return () => window.clearTimeout(t)
  }, [b])

  const set = <K extends keyof Blocks>(k: K, v: Blocks[K]) => setB((x) => ({ ...x, [k]: v }))
  const save = async () => {
    setSaving(true)
    try {
      const r = await rd.assemble(b, true)
      notify(`已保存为策略「${r.strategy?.name ?? b.name}」`)
      if (r.strategy) onNavigate(`editor/${r.strategy.slug}`)
    } catch (e) { notify((e as Error).message, true) } finally { setSaving(false) }
  }

  if (!opt) return <Loading label="正在读取因子、模型和方案" />
  const libFor = b.signal.type === 'factor' ? opt.factors : b.signal.type === 'model' ? opt.models : []
  const pct = (v: number) => Math.round(v * 100)

  return (
    <>
      <Heading title="积木搭建" description="不写代码，用研究台里已有的成果拼一个策略：选标的池，选一个信号（因子、模型或动量），选怎么变成仓位，加上风控和执行方案。右边实时生成代码，保存后就是一个普通策略。"
        actions={<button className="button primary" disabled={saving || !!err || !!out?.issues.length} onClick={save}>{saving ? '正在保存…' : '保存为策略'}</button>} />

      {out && (
        <div className="rd-outline" aria-label="组装流程">
          {out.outline.map((o, i) => (
            <div key={o.step} className="rd-step"><span>{i + 1} · {o.step}</span><b>{o.text}</b></div>
          ))}
        </div>
      )}

      <div className="rd-assemble">
        <div>
          <Panel title="1 · 标的池">
            <div className="rd-grid">
              <Field label="名称"><input value={b.name} onChange={(e) => set('name', e.target.value)} /></Field>
              <Field label="周期">
                <select value={b.timeframe} onChange={(e) => set('timeframe', e.target.value)}>{opt.timeframes.map((t) => <option key={t}>{t}</option>)}</select>
              </Field>
              <Field label="预设标的池">
                <select value={universe} onChange={(e) => { setUniverse(e.target.value); const u = opt.universes.find((x) => x.id === e.target.value); if (u) set('symbols', u.symbols) }}>
                  <option value="">自定义</option>
                  {opt.universes.map((u) => <option key={u.id} value={u.id}>{u.name}（{u.symbols.length}）</option>)}
                </select>
              </Field>
            </div>
            <Field label="标的"><input value={b.symbols.join(', ')} onChange={(e) => { setUniverse(''); set('symbols', e.target.value.split(/[,，\s]+/).filter(Boolean)) }} /></Field>
          </Panel>

          <Panel title="2 · 信号" subtitle="信号决定看好谁。因子和模型来自研究台里启用或候选的成果。">
            <div className="rd-grid">
              <Field label="信号来源">
                <select value={b.signal.type} onChange={(e) => {
                  const type = e.target.value
                  const lib = type === 'factor' ? opt.factors : type === 'model' ? opt.models : []
                  set('signal', { ...b.signal, type, name: lib[0]?.name ?? '' })
                  if (type === 'none' && b.build.type !== 'scheme') set('build', { ...b.build, type: 'equal' })
                }}>
                  {opt.signals.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
                </select>
              </Field>
              {(b.signal.type === 'factor' || b.signal.type === 'model') && (
                <Field label={b.signal.type === 'factor' ? '因子' : '模型'}>
                  <select value={b.signal.name ?? ''} onChange={(e) => set('signal', { ...b.signal, name: e.target.value })}>
                    {libFor.length === 0 && <option value="">（库里还没有可用的）</option>}
                    {libFor.map((x) => <option key={x.name} value={x.name}>{x.name}{x.status === 'active' ? '' : '（候选）'}</option>)}
                  </select>
                </Field>
              )}
              {b.signal.type === 'momentum' && (
                <Field label="动量回看（根）"><input type="number" min={2} max={500} value={b.signal.lookback ?? 20} onChange={(e) => set('signal', { ...b.signal, lookback: Number(e.target.value) })} /></Field>
              )}
            </div>
            {libFor.find((x) => x.name === b.signal.name)?.detail && <div className="hint"><code className="inline">{libFor.find((x) => x.name === b.signal.name)?.detail}</code></div>}
          </Panel>

          <Panel title="3 · 组合构建" subtitle="把信号变成每个标的的仓位。">
            <div className="rd-grid">
              <Field label="方式">
                <select value={b.build.type} onChange={(e) => set('build', { ...b.build, type: e.target.value, name: e.target.value === 'scheme' ? opt.schemes[0]?.name ?? '' : b.build.name })}>
                  {opt.builders.map((x) => <option key={x.id} value={x.id} disabled={b.signal.type === 'none' && (x.id === 'topk' || x.id === 'rank')}>{x.label}</option>)}
                </select>
              </Field>
              {b.build.type === 'scheme' && (
                <Field label="配置方案">
                  <select value={b.build.name ?? ''} onChange={(e) => set('build', { ...b.build, name: e.target.value })}>
                    {opt.schemes.length === 0 && <option value="">（还没有方案）</option>}
                    {opt.schemes.map((x) => <option key={x.name} value={x.name}>{x.name} · {x.detail}</option>)}
                  </select>
                </Field>
              )}
              {b.build.type === 'topk' && (
                <Field label="持有前几个"><input type="number" min={1} max={b.symbols.length || 1} value={b.build.k ?? 2} onChange={(e) => set('build', { ...b.build, k: Number(e.target.value) })} /></Field>
              )}
              <Field label="每隔几根调仓"><input type="number" min={1} max={500} value={b.every} onChange={(e) => set('every', Number(e.target.value))} /></Field>
            </div>
          </Panel>

          <Panel title="4 · 风控">
            <div className="rd-grid">
              <Field label="单个标的上限（%）"><input type="number" min={5} max={100} step={5} value={pct(b.risk.max_weight)} onChange={(e) => set('risk', { ...b.risk, max_weight: Number(e.target.value) / 100 })} /></Field>
              <Field label="保留现金（%）"><input type="number" min={0} max={90} step={5} value={pct(b.risk.cash)} onChange={(e) => set('risk', { ...b.risk, cash: Number(e.target.value) / 100 })} /></Field>
              <Field label="回撤清仓线（%）" hint="0 表示不设"><input type="number" min={0} max={80} step={1} value={pct(b.risk.stop_dd)} onChange={(e) => set('risk', { ...b.risk, stop_dd: Number(e.target.value) / 100 })} /></Field>
            </div>
          </Panel>

          <Panel title="5 · 执行" subtitle="选了执行方案，回测会按它的成本曲线估算成交价。">
            <Field label="执行方案">
              <select value={b.execution} onChange={(e) => set('execution', e.target.value)}>
                <option value="">平台默认成本模型</option>
                {opt.executions.map((x) => <option key={x.name} value={x.name}>{x.name} · {x.detail}</option>)}
              </select>
            </Field>
          </Panel>
        </div>

        <div className="rd-code">
          <Panel title="生成的代码" subtitle="保存后可以在编辑器里继续改，或交给 AI 研究室迭代。">
            {err && <Callout tone="warn" title="还拼不起来">{err}</Callout>}
            {out?.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
            {out?.issues.length ? <Callout tone="fail" title="代码检查没通过">{out.issues.join('；')}</Callout> : null}
            {out ? <CodeView code={out.code} maxHeight={720} /> : !err && <Loading label="正在生成" />}
          </Panel>
        </div>
      </div>
    </>
  )
}
