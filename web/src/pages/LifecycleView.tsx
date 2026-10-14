/** 衰减与重训：运行中的实验跑没跑出回测的正常区间（建议降级要你确认）、模型定期重训、按市场状态的建议权重。 */
import { useCallback, useEffect, useState } from 'react'
import { get, post } from '../http'
import { Callout, EmptyState, Loading, Panel, Tag } from '../components/ui'
import type { PageProps } from './types'

interface DecayMetric { key: string; label: string; value: number | null; badness: number; band: (number | null)[]; bad: boolean }
interface DecayReport {
  id: string; strategy_id: string; strategy_name?: string; execution: string; status: 'warming' | 'ok' | 'watch' | 'decay'; label: string
  days: number; metrics: DecayMetric[]; reasons: string[]; score?: { value: number; watch: number; decay: number }
  stage: string; suggest_demote_to: string | null; checked_at: number; trades?: number
}
interface ModelRow {
  id: string; name: string; status: string; frozen: boolean; versions: number; due: boolean; last_retrain: number
  plan: { enabled: boolean; every_days: number; min_ratio: number }
  last: { at: number; accepted: boolean; why: string; oos_rank_ic: number | null } | null
}
interface Overview {
  decay: DecayReport[]; models: ModelRow[]
  regime: { regime: string | null; label: string; symbol: string; as_of?: number | null; source?: string }
  weights: { session_id: string; strategy?: string; weight: number; multipliers: Record<string, number> }[]
  regimes: Record<string, string>
}

const STAGE: Record<string, string> = { backtest: '回测', paper: '纸面模拟', demo: '模拟盘', graduated: '毕业' }
const TONE: Record<string, string> = { decay: 'fail', watch: 'warn', ok: 'ok', warming: '' }
const pct = (v: number | null | undefined, d = 1) => (v == null ? '—' : `${(v * 100).toFixed(d)}%`)
const when = (s?: number | null) => (s ? new Date(s > 1e11 ? s : s * 1000).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—')

export default function LifecycleView({ notify }: Pick<PageProps, 'notify'>) {
  const [d, setD] = useState<Overview | null>(null)
  const [busy, setBusy] = useState('')
  const load = useCallback(() => { get<Overview>('/api/lifecycle').then(setD).catch((e) => notify(e.message, true)) }, [notify])
  useEffect(load, [load])
  if (!d) return <Loading />

  const act = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key)
    try { await fn(); notify(ok); load() } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }

  return (
    <div className="stack">
      <Panel title="策略衰减" subtitle="拿运行中的表现对照回测重采样出的正常区间。持续跑输就累积分数，越过衰减线时给出降级建议；正常策略两周里被误标的概率约 5%。"
        actions={<button className="button small" disabled={!!busy} onClick={() => act('check', () => post('/api/lifecycle/check'), '已重新判定')}>{busy === 'check' ? '判定中…' : '立刻判定'}</button>}>
        {d.decay.length === 0 ? <EmptyState glyph="◷" title="还没有判定记录">有纸面模拟或模拟盘实验在跑时，每小时判定一次；运行满 3 天后才给结论。</EmptyState> : (
          <div className="lc-list">
            {d.decay.map((r) => (
              <article key={r.id} className={`lc-card ${r.status}`}>
                <header>
                  <strong>{r.strategy_name || r.strategy_id}</strong>
                  <Tag tone={TONE[r.status]}>{r.label}</Tag>
                  <span className="hint">{r.execution === 'live' ? '微实盘' : r.execution === 'demo' ? '模拟盘' : '纸面'} · 已运行 {r.days.toFixed(1)} 天 · 当前阶段 {STAGE[r.stage] ?? r.stage}</span>
                  <span className="spacer" />
                  <small className="hint">{when(r.checked_at)} 判定</small>
                </header>
                {r.score && (
                  <div className="lc-meter" title={`分数 ${r.score.value}，关注线 ${r.score.watch}，衰减线 ${r.score.decay}`}>
                    <div className="lc-fill" style={{ width: `${Math.min(100, (r.score.value / Math.max(r.score.decay, 0.01)) * 100)}%` }} />
                    <span className="lc-tick" style={{ left: `${Math.min(100, (r.score.watch / Math.max(r.score.decay, 0.01)) * 100)}%` }} />
                  </div>
                )}
                {r.metrics.length > 0 && (
                  <table className="lc-table">
                    <thead><tr><th>指标</th><th className="right">实际</th><th className="right">正常区间（5%～95%）</th><th className="right">比多少情形差</th></tr></thead>
                    <tbody>{r.metrics.map((m) => (
                      <tr key={m.key} className={m.bad ? 'negative' : ''}>
                        <td>{m.label}</td><td className="right mono">{pct(m.value, m.key === 'trade_mean' ? 2 : 1)}</td>
                        <td className="right mono">{m.key === 'max_drawdown' ? `${pct(m.band[0])} ～ ${pct(m.band[2])}` : `${pct(m.band[0], m.key === 'trade_mean' ? 2 : 1)} ～ ${pct(m.band[2], m.key === 'trade_mean' ? 2 : 1)}`}</td>
                        <td className="right mono">{(100 - m.badness * 100).toFixed(0)}%</td>
                      </tr>
                    ))}</tbody>
                  </table>
                )}
                {r.reasons.length > 0 && <ul className="lc-reasons">{r.reasons.map((x) => <li key={x}>{x}</li>)}</ul>}
                {r.suggest_demote_to && (
                  <div className="filter-row">
                    <Callout tone="warn">建议从「{STAGE[r.stage]}」退回「{STAGE[r.suggest_demote_to]}」。平台不会自动停策略或降级。</Callout>
                    <button className="button small danger" disabled={!!busy} onClick={() => {
                      if (window.confirm(`把「${r.strategy_name || r.strategy_id}」退回「${STAGE[r.suggest_demote_to!]}」？`)) act(r.id, () => post(`/api/lifecycle/${r.id}/demote`), '已退回上一阶段')
                    }}>确认降级</button>
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
      </Panel>

      <div className="two-columns even">
        <Panel title="模型定期重训" subtitle="到期后只用重训时刻以前的数据训练一个新版本；样本外不差于旧版本的设定比例才接替，冻结的模型另存为候选。">
          {d.models.length === 0 ? <p className="hint">模型库里还没有模型。</p> : (
            <table>
              <thead><tr><th>模型</th><th>计划</th><th>上次</th><th /></tr></thead>
              <tbody>{d.models.map((m) => (
                <tr key={m.id}>
                  <td>{m.name}<div className="hint">{m.versions} 个版本{m.frozen ? ' · 已冻结' : ''}</div></td>
                  <td>
                    <label className="inline-check"><input type="checkbox" checked={m.plan.enabled} onChange={(e) => act(m.id, () => post(`/api/models/${m.id}/retrain-plan`, { enabled: e.target.checked }), e.target.checked ? '已开启定期重训' : '已关闭定期重训')} /> 每</label>
                    <select value={m.plan.every_days} onChange={(e) => act(m.id, () => post(`/api/models/${m.id}/retrain-plan`, { every_days: Number(e.target.value) }), '已更新重训间隔')}>
                      {[1, 3, 7, 14, 30].map((n) => <option key={n} value={n}>{n} 天</option>)}
                    </select>
                    {m.due && <Tag tone="warn">到期</Tag>}
                  </td>
                  <td>{m.last ? <><Tag tone={m.last.accepted ? 'ok' : ''}>{m.last.accepted ? '已采用' : '未采用'}</Tag><div className="hint">{when(m.last.at)} · {m.last.why}</div></> : <span className="hint">还没重训过</span>}</td>
                  <td className="right"><button className="button small" disabled={!!busy} onClick={() => act(`rt-${m.id}`, () => post(`/api/models/${m.id}/retrain`, {}), '重训完成')}>{busy === `rt-${m.id}` ? '重训中…' : '立刻重训'}</button></td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </Panel>
        <Panel title="市场状态与建议权重" subtitle="按 BTC 日线判断趋势 / 震荡 / 高波动；每个策略按它在这种状态下的历史表现和近期表现加减权重。只是建议，不会改仓位。">
          <p><Tag tone="ok">{d.regime.label}</Tag> <span className="hint">{d.regime.symbol}{d.regime.as_of ? ` · ${when(d.regime.as_of)}` : ''}{d.regime.source === 'synthetic' ? ' · 教学合成数据' : ''}</span></p>
          {d.weights.length === 0 ? <p className="hint">有两个以上实验在跑时，这里给出分配建议。</p> : (
            <table>
              <thead><tr><th>实验</th><th>各状态倍数</th><th className="right">建议权重</th></tr></thead>
              <tbody>{d.weights.map((w) => (
                <tr key={w.session_id}>
                  <td>{w.strategy || w.session_id}</td>
                  <td className="hint">{Object.keys(w.multipliers).length ? Object.entries(w.multipliers).map(([k, v]) => `${d.regimes[k] ?? k} ×${v}`).join('、') : '回测太短，不调'}</td>
                  <td className="right mono">{pct(w.weight)}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  )
}
