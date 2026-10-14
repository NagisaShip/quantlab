/** 回测条件：标的、周期、区间、资金、成本、数据来源。新建回测与策略对比共用。 */
import type { Spec } from '../../api2'

export type SpecDraft = {
  securities: string; timeframe: string; range: 'bars' | 'dates'; bars_limit: number; start: string; end: string
  initial_cash: number; taker_fee: number; slippage_bps: number; source: Spec['source']
}

export const DEFAULT_DRAFT: SpecDraft = {
  securities: 'BTC/USDT', timeframe: '1h', range: 'bars', bars_limit: 1500, start: '', end: '',
  initial_cash: 10000, taker_fee: 0.001, slippage_bps: 5, source: 'auto',
}

const ms = (d: string) => (d ? Date.parse(`${d}T00:00:00Z`) : null)

export function toSpec(d: SpecDraft): Partial<Spec> {
  const out: Partial<Spec> = {
    securities: d.securities.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
    timeframe: d.timeframe, initial_cash: d.initial_cash, taker_fee: d.taker_fee, slippage_bps: d.slippage_bps, source: d.source,
  }
  if (d.range === 'dates') { out.start = ms(d.start); out.end = ms(d.end) } else out.bars_limit = d.bars_limit
  return out
}

export function SettingsForm({ value, onChange, hideSecurities }: { value: SpecDraft; onChange: (d: SpecDraft) => void; hideSecurities?: boolean }) {
  const set = <K extends keyof SpecDraft>(k: K, v: SpecDraft[K]) => onChange({ ...value, [k]: v })
  return (
    <div className="bt-form">
      {!hideSecurities && <label>标的<input value={value.securities} onChange={(e) => set('securities', e.target.value)} placeholder="BTC/USDT, ETH/USDT" /></label>}
      <label>周期<select value={value.timeframe} onChange={(e) => set('timeframe', e.target.value)}>{['15m', '1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}</select></label>
      <label>区间
        <select value={value.range} onChange={(e) => set('range', e.target.value as SpecDraft['range'])}>
          <option value="bars">最近 N 根 K 线</option><option value="dates">指定日期</option>
        </select>
      </label>
      {value.range === 'bars' ? (
        <label>K 线根数<input type="number" min={200} max={20000} step={100} value={value.bars_limit} onChange={(e) => set('bars_limit', Number(e.target.value))} /></label>
      ) : (
        <>
          <label>开始<input type="date" value={value.start} onChange={(e) => set('start', e.target.value)} /></label>
          <label>结束<input type="date" value={value.end} onChange={(e) => set('end', e.target.value)} /></label>
        </>
      )}
      <label>初始资金<input type="number" min={100} step={1000} value={value.initial_cash} onChange={(e) => set('initial_cash', Number(e.target.value))} /></label>
      <label>手续费<input type="number" min={0} max={0.05} step={0.0001} value={value.taker_fee} onChange={(e) => set('taker_fee', Number(e.target.value))} /></label>
      <label>滑点（bp）<input type="number" min={0} max={500} step={1} value={value.slippage_bps} onChange={(e) => set('slippage_bps', Number(e.target.value))} /></label>
      <label>数据
        <select value={value.source} onChange={(e) => set('source', e.target.value as Spec['source'])}>
          <option value="auto">自动（真实优先）</option><option value="exchange">只用真实行情</option><option value="synthetic">合成数据</option>
        </select>
      </label>
    </div>
  )
}
