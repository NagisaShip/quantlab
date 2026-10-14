/* 规则 DSL v2 的逐项表单：规则编辑页（DslEditor）与策略编辑器的「规则」视图共用。 */
import { Field } from '../../components/ui'

/* ---------- 规则 DSL v2 的前端类型（与后端 DSL_SCHEMA 同一份结构） ---------- */
export type Operand = { ref?: string; price?: string; const?: number | string; lag?: number; mul?: number }
export type Cond =
  | { op: 'gt' | 'gte' | 'lt' | 'lte' | 'cross_above' | 'cross_below'; left: Operand; right: Operand }
  | { op: 'all' | 'any'; conds: Cond[] }
  | { op: 'not'; cond: Cond }
  | { op: 'consecutive'; bars: number; cond: Cond }
export interface Indicator { type: string; period: number | string; source?: string }
/* 轮动排名：按某个指标排序取前 N。by 必须是 indicators 里已定义的指标名（后端逐项校验）。 */
export interface Rank { by: string; top: number; order?: 'desc' | 'asc'; weighting?: 'equal' | 'score'; min_score?: number | null }
export interface RuleSpec {
  dsl_version: 2
  name?: string
  description?: string
  /* instrument 与 instruments 二选一：单标的用前者，多标的轮动用后者 */
  instrument?: string
  instruments?: string[]
  timeframe: string
  params: Record<string, number>
  indicators: Record<string, Indicator>
  rank?: Rank | null
  entry: Cond
  exit?: Cond | null
  position: { mode: 'weight' | 'quantity'; value: number | string; max_weight?: number; qty_step?: number; insufficient_funds?: 'reject' | 'reduce' }
  risk?: { stop_loss?: number; take_profit?: number; max_trades_per_day?: number; drawdown_pause?: number }
}

const OPS: { value: string; label: string }[] = [
  { value: 'gt', label: '大于' }, { value: 'gte', label: '大于等于' }, { value: 'lt', label: '小于' }, { value: 'lte', label: '小于等于' },
  { value: 'cross_above', label: '上穿' }, { value: 'cross_below', label: '下穿' },
  { value: 'all', label: '全部满足' }, { value: 'any', label: '任一满足' }, { value: 'not', label: '并非' }, { value: 'consecutive', label: '连续满足' },
]
const PRICE = [['close', '收盘价'], ['open', '开盘价'], ['high', '最高价'], ['low', '最低价']]
const TFS = ['1m', '5m', '15m', '1h', '4h', '1d']

export const defaultCompare = (): Cond => ({ op: 'gt', left: { price: 'close' }, right: { const: 0 } })

function convert(c: Cond, op: string): Cond {
  if (['gt', 'gte', 'lt', 'lte', 'cross_above', 'cross_below'].includes(op)) {
    const base = 'left' in c ? c : defaultCompare()
    return { op: op as 'gt', left: (base as { left: Operand }).left, right: (base as { right: Operand }).right }
  }
  if (op === 'all' || op === 'any') return { op, conds: 'conds' in c ? c.conds : [c] }
  if (op === 'not') return { op: 'not', cond: 'cond' in c ? c.cond : c }
  return { op: 'consecutive', bars: 3, cond: 'cond' in c ? c.cond : c }
}

/** 由校验问题生成"某个字段路径是否出错"的判断函数。 */
export function makeErrAt(problems: { path: string }[]) {
  return (path: string) => problems.some((p) => p.path === path || p.path.startsWith(path + '.') || p.path.startsWith(path + '['))
}

/**
 * 规则表单（基本 / 轮动排名 / 参数 / 指标 / 入场 / 离场 / 仓位 / 风控）。
 * 传了 onNameChange 才显示策略名称输入框（策略编辑器里名称在顶栏编辑）。
 */
export function RuleForm({ spec, onChange, errAt, indicatorTypes, name, onNameChange }: {
  spec: RuleSpec
  onChange: (s: RuleSpec) => void
  errAt: (path: string) => boolean
  indicatorTypes: { type: string; label: string }[]
  name?: string
  onNameChange?: (name: string) => void
}) {
  const update = (patch: Partial<RuleSpec>) => onChange({ ...spec, ...patch })
  /* 渲染兜底：JSON 视图可能写入结构不完整的规则（例如缺 indicators / position），
     直接读会抛错导致白屏。这里只给渲染补默认值，不改 spec 本身——校验与保存仍以原文为准。 */
  const raw = spec as Partial<RuleSpec>
  const form: RuleSpec = {
    ...raw,
    dsl_version: 2,
    timeframe: raw.timeframe ?? '1h',
    params: raw.params ?? {},
    indicators: raw.indicators ?? {},
    entry: raw.entry ?? defaultCompare(),
    position: raw.position ?? { mode: 'weight', value: 0.8 },
  }
  const params = Object.keys(form.params)
  const indicatorNames = Object.keys(form.indicators)
  /* 轮动与否只看 instruments 是否存在，不另设前端状态，避免与 JSON 视图不一致 */
  const multi = Array.isArray(form.instruments)
  const universe = form.instruments ?? (form.instrument ? [form.instrument] : [])

  /* 切换单 / 多标的时删掉另一个字段：二者同时存在会被后端拒绝 */
  const setMulti = (on: boolean) => {
    const next: RuleSpec = { ...spec }
    if (on) {
      const list = form.instrument ? [form.instrument] : universe.slice(0, 3)
      next.instruments = list.length < 2 ? [...list, list[0] === 'ETH/USDT' ? 'BTC/USDT' : 'ETH/USDT'] : list
      delete next.instrument
      next.rank = form.rank ?? { by: indicatorNames[0] ?? '', top: 1, order: 'desc', weighting: 'equal' }
      if (next.position?.mode === 'quantity') next.position = { ...next.position, mode: 'weight', value: 0.8 }
    } else {
      next.instrument = universe[0] ?? 'BTC/USDT'
      delete next.instruments
      delete next.rank
    }
    onChange(next)
  }
  const setRank = (patch: Partial<Rank>) => update({ rank: { ...(form.rank ?? { by: indicatorNames[0] ?? '', top: 1 }), ...patch } })

  return (
    <>
      <div className="dsl-section">
        <h3>基本</h3>
        <div className="form-grid">
          {onNameChange && <Field label="策略名称"><input value={name ?? ''} maxLength={60} onChange={(e) => onNameChange(e.target.value)} /></Field>}
          <Field label="标的模式">
            <select value={multi ? 'multi' : 'single'} onChange={(e) => setMulti(e.target.value === 'multi')}>
              <option value="single">单标的</option>
              <option value="multi">多标的轮动</option>
            </select>
          </Field>
          {!multi && (
            <Field label="标的"><input className={errAt('instrument') ? 'field-error' : ''} value={form.instrument ?? ''} onChange={(e) => update({ instrument: e.target.value.toUpperCase() })} /></Field>
          )}
          <Field label="周期"><select value={form.timeframe} onChange={(e) => update({ timeframe: e.target.value })}>{TFS.map((t) => <option key={t}>{t}</option>)}</select></Field>
        </div>
        {multi && (
          <>
            <p className={`hint${errAt('instruments') && !universe.some((_, i) => errAt(`instruments[${i}]`)) ? ' field-error' : ''}`}>标的池（至少 2 个）</p>
            {universe.map((s, i) => (
              <div key={i} className="dsl-row" style={{ gridTemplateColumns: '160px auto' }}>
                <input className={errAt(`instruments[${i}]`) ? 'field-error' : ''} value={s}
                  onChange={(e) => update({ instruments: universe.map((x, j) => (j === i ? e.target.value.toUpperCase() : x)) })} />
                <button className="button small ghost" disabled={universe.length <= 1}
                  onClick={() => update({ instruments: universe.filter((_, j) => j !== i) })}>删除</button>
              </div>
            ))}
            <button className="button small" onClick={() => update({ instruments: [...universe, ''] })}>＋ 标的</button>
          </>
        )}
      </div>
      {multi && (
        <div className="dsl-section">
          <h3>轮动排名 <small>按指标排序取前 N；同分按标的代码排序，结果可复现</small></h3>
          <div className="form-grid">
            <Field label="排序依据">
              <select className={errAt('rank.by') ? 'field-error' : ''} value={form.rank?.by ?? ''} onChange={(e) => setRank({ by: e.target.value })}>
                {!form.rank?.by && <option value="">选择指标</option>}
                {indicatorNames.map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </Field>
            <Field label="取前 N 名">
              <input type="number" min={1} className={errAt('rank.top') ? 'field-error' : ''} value={form.rank?.top ?? 1}
                onChange={(e) => setRank({ top: Number(e.target.value) })} />
            </Field>
            <Field label="排序方向">
              <select className={errAt('rank.order') ? 'field-error' : ''} value={form.rank?.order ?? 'desc'} onChange={(e) => setRank({ order: e.target.value as 'desc' })}>
                <option value="desc">从高到低</option><option value="asc">从低到高</option>
              </select>
            </Field>
            <Field label="仓位分配">
              <select className={errAt('rank.weighting') ? 'field-error' : ''} value={form.rank?.weighting ?? 'equal'} onChange={(e) => setRank({ weighting: e.target.value as 'equal' })}>
                <option value="equal">等权</option><option value="score">按得分加权</option>
              </select>
            </Field>
            <Field label="入选门槛（得分须大于）">
              <input type="number" step="0.001" className={errAt('rank.min_score') ? 'field-error' : ''} value={form.rank?.min_score ?? ''} placeholder="不设"
                onChange={(e) => {
                  if (e.target.value !== '') { setRank({ min_score: Number(e.target.value) }); return }
                  const r = { ...(form.rank ?? { by: indicatorNames[0] ?? '', top: 1 }) }
                  delete r.min_score
                  update({ rank: r })
                }} />
            </Field>
          </div>
        </div>
      )}
      <div className="dsl-section">
        <h3>参数 <small>用 $名字 在指标周期或常量中引用</small></h3>
        {params.map((p) => (
          <div key={p} className="dsl-row" style={{ gridTemplateColumns: '120px 120px auto' }}>
            <input value={p} readOnly className="mono" />
            <input type="number" className={errAt(`params.${p}`) ? 'field-error' : ''} value={form.params[p]}
              onChange={(e) => update({ params: { ...form.params, [p]: Number(e.target.value) } })} />
            <button className="button small ghost" onClick={() => { const n = { ...form.params }; delete n[p]; update({ params: n }) }}>删除</button>
          </div>
        ))}
        <button className="button small" onClick={() => {
          const k = window.prompt('参数名（小写字母、数字、下划线）')
          if (k && /^[a-z][a-z0-9_]{0,23}$/.test(k)) update({ params: { ...form.params, [k]: 10 } })
        }}>＋ 参数</button>
      </div>
      <div className="dsl-section">
        <h3>指标</h3>
        {Object.entries(form.indicators).map(([key, ind]) => (
          <div key={key} className={`dsl-row${errAt(`indicators.${key}`) ? ' field-error' : ''}`}>
            <input value={key} readOnly className="mono" />
            <select value={ind.type} onChange={(e) => update({ indicators: { ...form.indicators, [key]: { ...ind, type: e.target.value } } })}>
              {indicatorTypes.map((t) => <option key={t.type} value={t.type}>{t.type.toUpperCase()} · {t.label}</option>)}
            </select>
            <input value={String(ind.period)} title="整数周期或 $参数"
              onChange={(e) => {
                const v = e.target.value
                update({ indicators: { ...form.indicators, [key]: { ...ind, period: v.startsWith('$') ? v : Number(v) } } })
              }} />
            <select value={ind.source ?? ''} disabled={ind.type === 'atr'}
              onChange={(e) => { const n = { ...ind, source: e.target.value || undefined }; if (!n.source) delete n.source; update({ indicators: { ...form.indicators, [key]: n } }) }}>
              <option value="">默认</option>{PRICE.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
            <button className="button small ghost" onClick={() => { const n = { ...form.indicators }; delete n[key]; update({ indicators: n }) }}>删除</button>
          </div>
        ))}
        <button className="button small" onClick={() => {
          const k = window.prompt('指标名（小写字母、数字、下划线），例如 ma20')
          if (k && /^[a-z][a-z0-9_]{0,23}$/.test(k)) update({ indicators: { ...form.indicators, [k]: { type: 'sma', period: 20 } } })
        }}>＋ 指标</button>
      </div>
      <div className="dsl-section">
        <h3>入场条件 <small>空仓时满足 → 建仓</small></h3>
        <CondEditor c={form.entry} path="entry" errAt={errAt} refs={Object.keys(form.indicators)} onChange={(c) => update({ entry: c })} />
      </div>
      <div className="dsl-section">
        <h3>离场条件 <small>持仓时满足 → 清仓；与入场同时满足时离场优先</small></h3>
        {form.exit ? (
          <>
            <CondEditor c={form.exit} path="exit" errAt={errAt} refs={Object.keys(form.indicators)} onChange={(c) => update({ exit: c })} />
            <button className="button small ghost" onClick={() => update({ exit: null })}>移除离场条件</button>
          </>
        ) : <button className="button small" onClick={() => update({ exit: defaultCompare() })}>＋ 离场条件</button>}
      </div>
      <div className="dsl-section">
        <h3>仓位 {multi && <small>轮动时是整个组合的目标占比，按名次分给前 N 名，每个再受最大仓位限制</small>}</h3>
        <div className="form-grid">
          <Field label="模式"><select value={form.position.mode} onChange={(e) => update({ position: { ...form.position, mode: e.target.value as 'weight' } })}>
            <option value="weight">目标资金占比</option><option value="quantity" disabled={multi}>固定数量{multi ? '（轮动不支持）' : ''}</option></select></Field>
          <Field label={form.position.mode === 'weight' ? (multi ? '组合总占比（0–1）' : '占权益比例（0–1）') : '数量（标的币）'}>
            <input className={errAt('position.value') ? 'field-error' : ''} value={String(form.position.value)}
              onChange={(e) => update({ position: { ...form.position, value: e.target.value.startsWith('$') ? e.target.value : Number(e.target.value) } })} />
          </Field>
          <Field label="最大仓位（占权益）"><input type="number" step="0.05" className={errAt('position.max_weight') ? 'field-error' : ''} value={form.position.max_weight ?? 1}
            onChange={(e) => update({ position: { ...form.position, max_weight: Number(e.target.value) } })} /></Field>
          <Field label="数量精度"><input type="number" step="0.0001" value={form.position.qty_step ?? 0.0001}
            onChange={(e) => update({ position: { ...form.position, qty_step: Number(e.target.value) } })} /></Field>
          <Field label="资金不足时"><select value={form.position.insufficient_funds ?? 'reject'}
            onChange={(e) => update({ position: { ...form.position, insufficient_funds: e.target.value as 'reject' } })}>
            <option value="reject">拒绝（默认）</option><option value="reduce">按可负担数量成交</option></select></Field>
        </div>
      </div>
      <div className="dsl-section">
        <h3>风控 <small>同一根 bar 同时触及止损与止盈时止损优先</small></h3>
        <div className="form-grid">
          {([['stop_loss', '止损（0.05 = 5%）'], ['take_profit', '止盈'], ['max_trades_per_day', '每日最多成交笔数（0 = 不限）'], ['drawdown_pause', '回撤暂停开仓阈值（0 = 关）']] as const).map(([k, l]) => (
            <Field key={k} label={l}><input type="number" step="0.01" className={errAt(`risk.${k}`) ? 'field-error' : ''} value={form.risk?.[k] ?? 0}
              onChange={(e) => update({ risk: { ...form.risk, [k]: Number(e.target.value) } })} /></Field>
          ))}
        </div>
      </div>
    </>
  )
}

/* ---------- 条件编辑器（递归） ---------- */
export function CondEditor({ c, path, onChange, refs, errAt, onRemove }: {
  c: Cond; path: string; onChange: (c: Cond) => void; refs: string[]; errAt: (p: string) => boolean; onRemove?: () => void
}) {
  const logic = c.op === 'all' || c.op === 'any' || c.op === 'not' || c.op === 'consecutive'
  return (
    <div className={`cond-node${logic ? ' logic' : ''}${errAt(path) ? ' field-error' : ''}`}>
      <div className="cond-head">
        {'left' in c && <OperandEditor o={c.left} refs={refs} bad={errAt(`${path}.left`)} onChange={(o) => onChange({ ...c, left: o })} />}
        <select value={c.op} onChange={(e) => onChange(convert(c, e.target.value))}>
          {OPS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        {'right' in c && <OperandEditor o={c.right} refs={refs} bad={errAt(`${path}.right`)} onChange={(o) => onChange({ ...c, right: o })} />}
        {c.op === 'consecutive' && (
          <><input type="number" min={1} max={50} style={{ width: 60 }} value={c.bars} onChange={(e) => onChange({ ...c, bars: Number(e.target.value) })} /> 根 bar</>
        )}
        {onRemove && <button className="button small ghost" onClick={onRemove}>删除</button>}
      </div>
      {(c.op === 'all' || c.op === 'any') && (
        <div className="cond-children">
          {c.conds.map((x, i) => (
            <CondEditor key={i} c={x} path={`${path}.conds[${i}]`} refs={refs} errAt={errAt}
              onChange={(n) => onChange({ ...c, conds: c.conds.map((y, j) => (j === i ? n : y)) })}
              onRemove={c.conds.length > 1 ? () => onChange({ ...c, conds: c.conds.filter((_, j) => j !== i) }) : undefined} />
          ))}
          <button className="button small" onClick={() => onChange({ ...c, conds: [...c.conds, defaultCompare()] })}>＋ 条件</button>
        </div>
      )}
      {(c.op === 'not' || c.op === 'consecutive') && (
        <div className="cond-children">
          <CondEditor c={c.cond} path={`${path}.cond`} refs={refs} errAt={errAt} onChange={(n) => onChange({ ...c, cond: n })} />
        </div>
      )}
    </div>
  )
}

function OperandEditor({ o, refs, onChange, bad }: { o: Operand; refs: string[]; onChange: (o: Operand) => void; bad: boolean }) {
  const kind = 'ref' in o ? 'ref' : 'price' in o ? 'price' : 'const'
  const extras = { lag: o.lag, mul: o.mul }
  const clean = (x: Operand) => {
    const out: Operand = { ...x }
    if (!out.lag) delete out.lag
    if (out.mul === undefined || out.mul === 1) delete out.mul
    return out
  }
  return (
    <span className={`operand${bad ? ' field-error' : ''}`}>
      <select value={kind} onChange={(e) => {
        const k = e.target.value
        onChange(clean(k === 'ref' ? { ref: refs[0] ?? '', ...extras } : k === 'price' ? { price: 'close', ...extras } : { const: 0 }))
      }}>
        <option value="ref">指标</option><option value="price">价格</option><option value="const">常量</option>
      </select>
      {kind === 'ref' && <select value={o.ref} onChange={(e) => onChange(clean({ ...o, ref: e.target.value }))}>{refs.map((r) => <option key={r}>{r}</option>)}</select>}
      {kind === 'price' && <select value={o.price} onChange={(e) => onChange(clean({ ...o, price: e.target.value }))}>{PRICE.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>}
      {kind === 'const' && <input value={String(o.const ?? 0)} style={{ width: 72 }} onChange={(e) => onChange({ const: e.target.value.startsWith('$') ? e.target.value : Number(e.target.value) })} />}
      {kind !== 'const' && (
        <>
          <input className="lag" type="number" min={0} max={50} title="lag：几根 bar 之前（0 = 最近一根已收盘）" value={o.lag ?? 0} onChange={(e) => onChange(clean({ ...o, lag: Number(e.target.value) }))} />
          <input type="number" step="0.01" style={{ width: 60 }} title="mul：乘数（默认 1）" value={o.mul ?? 1} onChange={(e) => onChange(clean({ ...o, mul: Number(e.target.value) }))} />
        </>
      )}
    </span>
  )
}
