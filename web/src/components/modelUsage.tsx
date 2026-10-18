/**
 * 模型用量与限额的共用组件：限额规则编辑、限额进度条、用量统计（日趋势 + 按模型 / 成员拆分）。
 * 「设置 · 模型」「设置 · 额度与用量」「设置 · 成员与邀请」共用。
 */
import { useEffect, useState } from 'react'
import type { LimitMetric, LimitRule, LimitStatus, LimitWindow, ModelUsage } from '../api/research'

/** 时间段：自然周期（每日 / 每周 / 每月 / 累计，按工作区时区切分）与滚动时间段。 */
export const WINDOWS: [LimitWindow, string][] = [
  ['day', '每日'], ['week', '每周'], ['month', '每月'], ['total', '累计（不重置）'],
  ['1h', '滚动 1 小时'], ['5h', '滚动 5 小时'], ['1d', '滚动 24 小时'], ['7d', '滚动 7 天'], ['30d', '滚动 30 天'],
]
export const MODEL_METRICS: [LimitMetric, string][] = [['tokens', '按 token'], ['cost', '按费用（元）']]
export const MEMBER_METRICS: [LimitMetric, string][] = [
  ...MODEL_METRICS,
  ['cpu_seconds', '按 CPU 秒'],
  ['backtests', '按回测次数'],
  ['optimize_combinations', '按优化组合数'],
  ['storage_bytes', '按上传存储（MB）'],
  ['record_units', '按研究记录（单位）'],
]
export const METRICS = MODEL_METRICS
export const METRIC_LABEL: Record<LimitMetric, string> = {
  tokens: 'token', cost: '费用', cpu_seconds: 'CPU 秒', backtests: '回测次数', optimize_combinations: '优化组合数',
  storage_bytes: '上传存储', record_units: '研究记录',
}
const stockMetric = (m: LimitMetric) => m === 'storage_bytes' || m === 'record_units'
export const metricUnit = (m: LimitMetric) => m === 'cost' ? '元' : m === 'tokens' ? 'token' : m === 'cpu_seconds' ? '秒' : m === 'backtests' ? '次' : m === 'storage_bytes' ? '' : m === 'record_units' ? '单位' : '组'
export const editorUnit = (m: LimitMetric) => m === 'storage_bytes' ? 'MB' : metricUnit(m)
export const fmtStorageBytes = (bytes: string | number) => {
  let n = Math.max(0, Number(bytes))
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1 }
  return `${i === 0 ? n.toFixed(0) : n.toFixed(2)} ${units[i]}`
}
export const storageBytesFromInput = (value: string) => {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? String(Math.round(n * 1024 ** 2)) : ''
}
export const storageInputValue = (bytes: string | number) => {
  const n = Number(bytes) / 1024 ** 2
  return n > 0 ? String(Number(n.toFixed(3))) : ''
}
export const fmtLimitValue = (metric: LimitMetric, value: string | number) => metric === 'cost'
  ? fmtCost(value)
  : metric === 'tokens' ? fmtTokens(Number(value)) : metric === 'storage_bytes' ? fmtStorageBytes(value) : Number(value).toLocaleString('zh-CN')

export const fmtTokens = (n: number) =>
  n >= 1e9 ? `${(n / 1e9).toFixed(2)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}k` : String(n)
export const fmtCost = (v: string | number) => `¥${Number(v).toFixed(Number(v) >= 100 ? 2 : 4)}`

/** 输入控件保留用户正在敲的原始文本，避免把「0.」这类中间态立刻格式化掉。 */
function LimitValueInput({ rule, disabled, onChange }: { rule: LimitRule; disabled?: boolean; onChange: (v: string) => void }) {
  const asText = (r: LimitRule) => r.metric === 'storage_bytes' ? storageInputValue(r.limit) : r.limit
  const [draft, setDraft] = useState(() => asText(rule))
  useEffect(() => { setDraft(asText(rule)) }, [rule.metric, rule.limit])
  const stock = rule.metric === 'storage_bytes' || rule.metric === 'record_units'
  return (
    <input type="number" min={0}
      step={stock ? 1 : rule.metric === 'cost' ? 0.01 : rule.metric === 'tokens' ? 1000 : 1}
      inputMode="decimal" value={draft} disabled={disabled}
      onChange={(e) => {
        setDraft(e.target.value)
        onChange(rule.metric === 'storage_bytes' ? storageBytesFromInput(e.target.value) : e.target.value)
      }}
      placeholder={`上限（${editorUnit(rule.metric)}）`} aria-label="上限" />
  )
}

/** 限额规则编辑：每行「计量方式 × 时间段 × 上限」。 */
export function LimitEditor({ rules, onChange, disabled, empty = '不限额', metrics = METRICS }: {
  rules: LimitRule[]; onChange: (r: LimitRule[]) => void; disabled?: boolean; empty?: string; metrics?: [LimitMetric, string][]
}) {
  const set = (i: number, patch: Partial<LimitRule>) => onChange(rules.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const changeMetric = (i: number, metric: LimitMetric) => {
    const prev = rules[i]
    const limit = prev.metric === metric ? prev.limit : ''
    set(i, { metric, window: stockMetric(metric) ? 'total' : prev.window, limit })
  }
  return (
    <div className="lim-editor">
      {rules.map((r, i) => {
        const stock = r.metric === 'storage_bytes' || r.metric === 'record_units'
        return (
        <div key={i} className="lim-row anim-in">
          <select value={r.metric} disabled={disabled} onChange={(e) => changeMetric(i, e.target.value as LimitMetric)} aria-label="计量方式">
            {metrics.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
          <select value={r.window} disabled={disabled || stock} onChange={(e) => set(i, { window: e.target.value as LimitWindow })} aria-label="时间段">
            {stock ? <option value="total">累计（按当前占用）</option> : <>
              <optgroup label="自然周期">{WINDOWS.slice(0, 4).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</optgroup>
              <optgroup label="滚动时间段">{WINDOWS.slice(4).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</optgroup>
            </>}
          </select>
          <LimitValueInput rule={r} disabled={disabled} onChange={(limit) => set(i, { limit })} />
          {!disabled && <button type="button" className="icon-btn" aria-label="删除这条限额" onClick={() => onChange(rules.filter((_, j) => j !== i))}>×</button>}
        </div>
        )
      })}
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
              <span>{s.metric === 'storage_bytes' || s.metric === 'record_units' ? '当前占用' : WINDOWS.find(([k]) => k === s.window)?.[1]} · {METRIC_LABEL[s.metric]}</span>
              <b>{fmtLimitValue(s.metric, s.used)} / {fmtLimitValue(s.metric, s.limit)}{metricUnit(s.metric) ? ` ${metricUnit(s.metric)}` : ''}</b>
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
