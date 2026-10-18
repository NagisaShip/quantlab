/** 回测条件：标的、周期、区间、资金、成本、数据来源。新建回测与策略对比共用。 */
import type { Spec } from '../../api/research'

export type SpecDraft = {
  securities: string; timeframe: string; range: 'bars' | 'dates'; bars_limit: number; start: string; end: string
  initial_cash: number; initial_currency: string; taker_fee: number; slippage_bps: number; source: Spec['source']
  exchange: 'binance' | 'okx' | 'bybit'; margin_mode: 'isolated' | 'cross'; position_mode: 'one-way' | 'hedge'
  contract_type: 'linear' | 'inverse' | 'delivery'; expiry: string; ledger_v2: boolean
  execution_mode: 'bar' | 'tick'; dynamic_slippage: boolean; vol_slippage: number
  impact_a_bps: number; impact_b_bps: number; limit_move_pct: number; min_liquidity_usd: number
}

export const DEFAULT_DRAFT: SpecDraft = {
  securities: 'BTC/USDT', timeframe: '1h', range: 'bars', bars_limit: 1500, start: '', end: '',
  initial_cash: 10000, initial_currency: 'USDT', taker_fee: 0.001, slippage_bps: 5, source: 'auto',
  exchange: 'binance', margin_mode: 'isolated', position_mode: 'one-way', contract_type: 'linear', expiry: '', ledger_v2: false,
  execution_mode: 'bar', dynamic_slippage: false, vol_slippage: 0.25,
  impact_a_bps: 0, impact_b_bps: 0, limit_move_pct: 0, min_liquidity_usd: 0,
}

const ms = (d: string) => (d ? Date.parse(`${d}T00:00:00Z`) : null)

export function toSpec(d: SpecDraft): Partial<Spec> {
  const out: Partial<Spec> = {
    securities: d.securities.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
    timeframe: d.timeframe, initial_cash: d.initial_cash, initial_currency: d.initial_currency,
    taker_fee: d.taker_fee, slippage_bps: d.slippage_bps, source: d.source, exchange: d.exchange,
    margin_mode: d.margin_mode, position_mode: d.position_mode, contract_type: d.contract_type,
    ledger_v2: d.ledger_v2, expiry_ts: d.expiry ? Date.parse(`${d.expiry}T00:00:00Z`) : null,
    execution_mode: d.execution_mode, dynamic_slippage: d.dynamic_slippage, vol_slippage: d.vol_slippage,
    impact_a_bps: d.impact_a_bps, impact_b_bps: d.impact_b_bps, limit_move_pct: d.limit_move_pct,
    min_liquidity_usd: d.min_liquidity_usd,
  }
  if (d.range === 'dates') { out.start = ms(d.start); out.end = ms(d.end) } else out.bars_limit = d.bars_limit
  return out
}

export function SettingsForm({ value, onChange, hideSecurities }: { value: SpecDraft; onChange: (d: SpecDraft) => void; hideSecurities?: boolean }) {
  const set = <K extends keyof SpecDraft>(k: K, v: SpecDraft[K]) => onChange({ ...value, [k]: v })
  return (
    <div className="bt-form">
      {!hideSecurities && <label>标的<input value={value.securities} onChange={(e) => set('securities', e.target.value)} placeholder="BTC/USDT, ETH/USDT" /></label>}
      <label>周期<select value={value.timeframe} onChange={(e) => set('timeframe', e.target.value)}>{['250ms', '1s', '5s', '1m', '15m', '1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}</select></label>
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
      <label>初始资金<input type="number" min={1} step={100} value={value.initial_cash} onChange={(e) => set('initial_cash', Number(e.target.value))} /></label>
      <label>资金币种<input value={value.initial_currency} onChange={(e) => set('initial_currency', e.target.value.toUpperCase())} placeholder="USDT / BTC" /></label>
      <label>交易所<select value={value.exchange} onChange={(e) => set('exchange', e.target.value as SpecDraft['exchange'])}><option value="binance">Binance</option><option value="okx">OKX</option><option value="bybit">Bybit</option></select></label>
      <label>保证金<select value={value.margin_mode} onChange={(e) => set('margin_mode', e.target.value as SpecDraft['margin_mode'])}><option value="isolated">逐仓</option><option value="cross">全仓</option></select></label>
      <label>持仓模式<select value={value.position_mode} onChange={(e) => set('position_mode', e.target.value as SpecDraft['position_mode'])}><option value="one-way">单向</option><option value="hedge">对冲</option></select></label>
      <label>合约类型<select value={value.contract_type} onChange={(e) => set('contract_type', e.target.value as SpecDraft['contract_type'])}><option value="linear">线性永续</option><option value="inverse">币本位反向</option><option value="delivery">交割</option></select></label>
      {value.contract_type === 'delivery' && <label>到期日<input type="date" value={value.expiry} onChange={(e) => set('expiry', e.target.value)} /></label>}
      <label className="wide" style={{ display: 'flex', gap: 8, alignItems: 'center' }}><input type="checkbox" checked={value.ledger_v2} onChange={(e) => set('ledger_v2', e.target.checked)} />强制启用账本 v2（跨仓/对冲/反向/交割会自动启用）</label>
      <label>手续费<input type="number" min={0} max={0.05} step={0.0001} value={value.taker_fee} onChange={(e) => set('taker_fee', Number(e.target.value))} /></label>
      <label>滑点（bp）<input type="number" min={0} max={500} step={1} value={value.slippage_bps} onChange={(e) => set('slippage_bps', Number(e.target.value))} /></label>
      <label>数据
        <select value={value.source} onChange={(e) => set('source', e.target.value as Spec['source'])}>
          <option value="auto">真实行情（缓存优先）</option><option value="exchange">只用真实行情</option>
        </select>
      </label>
      <details style={{ gridColumn: '1 / -1' }}>
        <summary>微结构与极端行情（逐笔撮合 / 动态滑点 / 流动性）</summary>
        <div className="form-grid" style={{ marginTop: 10 }}>
          <label>撮合模式
            <select value={value.execution_mode} onChange={(e) => set('execution_mode', e.target.value as SpecDraft['execution_mode'])}>
              <option value="bar">Bar 级：下一根开盘成交</option>
              <option value="tick">逐笔路径：OHLC 盘中撮合</option>
            </select>
          </label>
          <label className="check-row" style={{ alignSelf: 'end' }}>
            <input type="checkbox" checked={value.dynamic_slippage} onChange={(e) => set('dynamic_slippage', e.target.checked)} />
            动态滑点（按本根振幅放大）
          </label>
          <label>振幅滑点系数<input type="number" min={0} max={10} step={0.05} value={value.vol_slippage} onChange={(e) => set('vol_slippage', Number(e.target.value))} /></label>
          <label>冲击成本 a（bp）<input type="number" min={0} max={500} step={0.1} value={value.impact_a_bps} onChange={(e) => set('impact_a_bps', Number(e.target.value))} /></label>
          <label>冲击成本 b<input type="number" min={0} max={2000} step={0.5} value={value.impact_b_bps} onChange={(e) => set('impact_b_bps', Number(e.target.value))} /></label>
          <label>涨跌停幅度<input type="number" min={0} max={1} step={0.01} value={value.limit_move_pct} onChange={(e) => set('limit_move_pct', Number(e.target.value))} /></label>
          <label>最低成交额（USD）<input type="number" min={0} step={1000} value={value.min_liquidity_usd} onChange={(e) => set('min_liquidity_usd', Number(e.target.value))} /></label>
        </div>
        <p className="hint" style={{ margin: '8px 0 0' }}>
          逐笔模式用 OHLC 路径模拟盘中成交；把周期设为 50ms / 250ms / 1s 时会直接使用子秒级数据撮合。
          涨跌停幅度 0 = 关闭（加密现货默认无涨跌停）；最低成交额用于模拟流动性枯竭。
        </p>
      </details>
      <p className="hint" style={{ gridColumn: '1 / -1', margin: 0 }}>
        这里的成本是平台默认值；如果策略代码里显式调用了 <code className="inline">set_order_cost(...)</code>，
        以代码为准（会覆盖这里的默认值），回测报告顶部会标注「当前使用代码自定义费率」。
      </p>
    </div>
  )
}
