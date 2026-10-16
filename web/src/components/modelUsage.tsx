/**
 * 模型用量与限额的共用组件：限额规则编辑、限额进度条、用量统计（日趋势 + 按模型 / 成员拆分）。
 * 「设置 · 模型」「设置 · 额度与用量」「设置 · 成员与邀请」共用。
 */
import { useState } from 'react'
import type { LimitMetric, LimitRule, LimitStatus, LimitWindow, ModelUsage } from '../api2'

/** 时间段：自然周期（每日 / 每周 / 每月 / 累计，按工作区时区切分）与滚动时间段。 */
export const WINDOWS: [LimitWindow, string][] = [
  ['day', '每日'], ['week', '每周'], ['month', '每月'], ['total', '累计（不重置）'],
  ['1h', '滚动 1 小时'], ['5h', '滚动 5 小时'], ['1d', '滚动 24 小时'], ['7d', '滚动 7 天'], ['30d', '滚动 30 天'],
]
export const METRICS: [LimitMetric, string][] = [['tokens', '按 token'], ['cost', '按费用（元）']]

export const fmtTokens = (n: number) =>
  n >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}k` : String(n)
export const fmtCost = (v: string | number) => `¥${Number(v).toFixed(Number(v) >= 100 ? 2 : 4)}`

/** 限额规则编辑：每行「计量方式 × 时间段 × 上限」。 */
export function LimitEditor({ rules, onChange, disabled, empty = '不限额' }: {
  rules: LimitRule[]; onChange: (r: LimitRule[]) => void; disabled?: boolean; empty?: string
}) {
  const set = (i: number, patch: Partial<LimitRule>) => onChange(rules.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  return (
    <div className="lim-editor">
      {rules.map((r, i) => (
        <div key={i} className="lim-row anim-in">
          <select value={r.metric} disabled={disabled} onChange={(e) => set(i, { metric: e.target.value as LimitMetric })} aria-label="计量方式">
            {METRICS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
          <select value={r.window} disabled={disabled} onChange={(e) => set(i, { window: e.target.value as LimitWindow })} aria-label="时间段">
            <optgroup label="自然周期">{WINDOWS.slice(0, 4).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</optgroup>
            <optgroup label="滚动时间段">{WINDOWS.slice(4).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</optgroup>
          </select>
          <input type="number" min={0} step={r.metric === 'cost' ? 0.01 : 1000} inputMode="decimal" value={r.limit} disabled={disabled}
            onChange={(e) => set(i, { limit: e.target.value })} placeholder={r.metric === 'cost' ? '上限（元）' : '上限（token）'} aria-label="上限" />
          {!disabled && <button type="button" className="icon-btn" aria-label="删除这条限额" onClick={() => onChange(rules.filter((_, j) => j !== i))}>×</button>}
        </div>
      ))}
      {!rules.length && <div className="lim-empty">{empty}</div>}
      {!disabled && (
        <button type="button" className="button small ghost" onClick={() => onChange([...rules, { metric: 'tokens', window: 'day', limit: '' }])}>＋ 添加限额</button>
      )}
    </div>
  )
}

/** 限额进度条。 */
export function LimitBars({ items, compact }: { items: LimitStatus[]; compact?: boolean }) {
  if (!items.length) return null
  return (
    <div className={`lim-bars${compact ? ' compact' : ''}`}>
      {items.map((s) => {
        const tone = s.exceeded ? 'over' : s.percent >= 80 ? 'warn' : ''
        return (
          <div key={`${s.metric}-${s.window}`} className={`lim-bar ${tone}`} title={s.label}>
            <div className="lim-bar-head">
              <span>{WINDOWS.find(([k]) => k === s.window)?.[1]}</span>
              <b>{s.metric === 'cost' ? `${fmtCost(s.used)} / ¥${s.limit}` : `${fmtTokens(Number(s.used))} / ${fmtTokens(Number(s.limit))}`}</b>
            </div>
            <div className="lim-track"><i style={{ width: `${Math.min(100, s.percent)}%` }} /></div>
          </div>
        )
      })}
    </div>
  )
}

/** 用量统计：总览数字 + 日趋势柱状图 + 按模型 / 按成员拆分。 */
export function UsageSummary({ data, showUsers }: { data: ModelUsage; showUsers?: boolean }) {
  // 没填价格时费用全是 0：默认看 token
  const [metric, setMetric] = useState<'cost' | 'tokens'>(Number(data.total.cost) > 0 ? 'cost' : 'tokens')
  const vals = data.daily.map((d) => (metric === 'cost' ? Number(d.cost) : d.tokens))
  const max = Math.max(1e-9, ...vals)
  return (
    <div className="usage-sum">
      <div className="usage-kpis">
        <div className="usage-kpi anim-rise"><small>调用次数</small><b>{data.total.calls.toLocaleString()}</b></div>
        <div className="usage-kpi anim-rise" style={{ animationDelay: '40ms' }}><small>token</small><b>{fmtTokens(data.total.tokens)}</b></div>
        <div className="usage-kpi anim-rise" style={{ animationDelay: '80ms' }}><small>费用（折合人民币）</small><b>{fmtCost(data.total.cost)}</b></div>
        <div className="usage-kpi anim-rise" style={{ animationDelay: '120ms' }} title="命中提示缓存的输入 / 全部输入">
          <small>输入缓存命中</small>
          <b>{data.total.cache_hit_rate === null || data.total.cache_hit_rate === undefined ? '—' : `${(data.total.cache_hit_rate * 100).toFixed(1)}%`}</b>
        </div>
      </div>
      <div className="usage-chart-head">
        <small>近 {data.days} 天（UTC 日）</small>
        <div className="seg seg-sm" role="tablist">
          <button role="tab" aria-selected={metric === 'cost'} onClick={() => setMetric('cost')}>费用</button>
          <button role="tab" aria-selected={metric === 'tokens'} onClick={() => setMetric('tokens')}>token</button>
        </div>
      </div>
      <div className="usage-chart" role="img" aria-label={`近 ${data.days} 天${metric === 'cost' ? '费用' : 'token'}趋势`}>
        {data.daily.map((d, i) => (
          <div key={d.day} className="usage-col" title={`${d.day} · ${d.calls} 次 · ${fmtTokens(d.tokens)} token · ${fmtCost(d.cost)}`}>
            <i style={{ height: `${Math.max(vals[i] > 0 ? 4 : 0, (vals[i] / max) * 100)}%`, animationDelay: `${i * 12}ms` }} />
          </div>
        ))}
      </div>
      {data.by_model.length > 0 ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>服务商 / 模型</th><th className="right">调用</th><th className="right">输入</th><th className="right" title="命中缓存的输入（含在「输入」里）">其中缓存</th><th className="right">输出</th><th className="right">费用</th></tr></thead>
            <tbody>
              {data.by_model.map((r) => (
                <tr key={`${r.scope}-${r.provider}-${r.model}`} className="anim-in">
                  <td><span className="mono">{r.provider}/{r.model}</span> <span className={`scope-tag ${r.scope}`}>{r.scope === 'workspace' ? '工作区' : '个人'}</span></td>
                  <td className="right mono">{r.calls}</td>
                  <td className="right mono">{fmtTokens(r.input_tokens ?? 0)}</td>
                  <td className="right mono" title={r.cache_write_tokens ? `写入缓存 ${fmtTokens(r.cache_write_tokens)}` : undefined}>
                    {r.cache_read_tokens ? fmtTokens(r.cache_read_tokens) : '—'}
                    {r.input_tokens && r.cache_read_tokens ? <small className="muted"> {((r.cache_read_tokens / r.input_tokens) * 100).toFixed(0)}%</small> : null}
                  </td>
                  <td className="right mono">{fmtTokens(r.output_tokens ?? 0)}</td>
                  <td className="right mono">{fmtCost(r.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <div className="lim-empty">这段时间还没有模型调用。</div>}
      {showUsers && data.by_user.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 12 }}>
          <table>
            <thead><tr><th>成员</th><th className="right">调用</th><th className="right">token</th><th className="right">费用</th></tr></thead>
            <tbody>
              {data.by_user.map((r) => (
                <tr key={r.user_id} className="anim-in">
                  <td>{r.name || r.user_id}</td>
                  <td className="right mono">{r.calls}</td>
                  <td className="right mono">{fmtTokens(r.tokens ?? 0)}</td>
                  <td className="right mono">{fmtCost(r.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
