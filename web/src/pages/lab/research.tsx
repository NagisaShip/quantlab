/**
 * AI 研究室的研究纪律与研究工具卡片：
 * - 数据核验徽标（正式 / 非正式回测、指标口径、一致性检查、零交易诊断）
 * - 实验记录（修改假设、改动类型、预期影响、代码差异）
 * - 调研报告卡片（/report，查看与导出 PDF）
 * - 子 Agent 并行研究卡片（/parallel）
 * - 标的与周期切换（对话顶部「BTC/USDT · 4h」下拉，支持多标的、同步数据）
 */
import { useEffect, useState } from 'react'
import { get, post } from '../../http'
import type { LabMessage } from '../../apiLab'
import { usePopover } from '../../components/chat/Composer'
import { QuantEyes } from '../../components/QuantBot'

const pct = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? '—' : `${(v * 100).toFixed(d)}%`)
const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? '—' : v.toFixed(d))

/* ------------------------------------------------------------------ */
/* 数据核验                                                            */
/* ------------------------------------------------------------------ */
interface DataCheck {
  formal: boolean; label: string; issues: string[]
  requested: { start: string; end: string; bars: number; timeframe: string }
  actual: { symbol: string; source: string; bars: number; scored_bars: number; start: string; end: string; coverage: number; days: number }[]
  warmup_bars: number
}

export function DataCheckBadge({ check, definitions, consistency, zero }: {
  check: DataCheck; definitions?: Record<string, string>
  consistency?: { ok: boolean; checks: { check: string; ok: boolean; detail: string }[] }
  zero?: { hint: string; caution: string; counts: Record<string, { bars: number; true_or_positive: number }> } | null
}) {
  const [open, setOpen] = useState(!check.formal)
  return (
    <div className={`rg-check${check.formal ? ' ok' : ' warn'}`}>
      <button type="button" className="rg-check-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <QuantEyes size={14} state={check.formal ? 'idle' : 'confused'} motion="low" />
        <b>{check.formal ? '数据核验通过' : '非正式回测'}</b>
        <span>请求 {check.requested.start} → {check.requested.end}（{check.requested.bars} 根 {check.requested.timeframe}）</span>
        {consistency && !consistency.ok && <span className="cb-badge warn">指标需复核</span>}
        <svg className={open ? 'up' : ''} width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
      </button>
      {open && (
        <div className="rg-check-body anim-expand">
          {check.issues.length > 0 && <ul className="rg-issues">{check.issues.map((x) => <li key={x}>{x}</li>)}</ul>}
          <table className="rg-table">
            <thead><tr><th>标的</th><th>来源</th><th>实际起止（UTC）</th><th className="right">K 线</th><th className="right">覆盖</th></tr></thead>
            <tbody>
              {check.actual.map((a) => (
                <tr key={a.symbol}>
                  <td>{a.symbol}</td><td>{a.source}</td><td>{a.start} → {a.end}</td>
                  <td className="right num">{a.scored_bars}<small>（共 {a.bars}，约 {a.days} 天）</small></td>
                  <td className={`right num ${a.coverage < 0.9 ? 'negative' : ''}`}>{pct(a.coverage, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {consistency && consistency.checks.length > 0 && (
            <ul className="rg-consist">
              {consistency.checks.map((c) => <li key={c.check} className={c.ok ? 'ok' : 'bad'}><b>{c.ok ? '✓' : '!'} {c.check}</b><span>{c.detail}</span></li>)}
            </ul>
          )}
          {zero && (
            <div className="rg-zero">
              <b>零交易诊断</b><span>{zero.hint}</span>
              {Object.keys(zero.counts).length > 0 && (
                <div className="lab2-params">{Object.entries(zero.counts).map(([k, v]) => <code key={k}>{k}: {v.true_or_positive}/{v.bars}</code>)}</div>
              )}
              <small>{zero.caution}</small>
            </div>
          )}
          {definitions && (
            <details className="rg-defs">
              <summary>指标口径（初始资金、费率单位、成交与止损规则、超额公式）</summary>
              <dl>{Object.entries(definitions).map(([k, v]) => <div key={k}><dt>{DEF_LABEL[k] ?? k}</dt><dd>{v}</dd></div>)}</dl>
            </details>
          )}
        </div>
      )}
    </div>
  )
}

const DEF_LABEL: Record<string, string> = {
  initial_cash: '初始资金', fees: '手续费', slippage: '滑点与价差', round_trip_cost: '往返成本', fill_model: '成交模型',
  signal_timing: '信号时点', stop_rule: '止损与跳空', total_return: '总收益', benchmark_return: '基准收益',
  excess_return: '超额收益', max_drawdown: '最大回撤', sharpe: '夏普',
}

/* ------------------------------------------------------------------ */
/* 实验记录                                                            */
/* ------------------------------------------------------------------ */
const CHANGE_LABEL: Record<string, string> = {
  initial: '初版', entry: '入场', exit: '出场', filter: '过滤条件', risk: '风控', params: '参数', other: '其他',
}

export function ExperimentNote({ experiment, diff }: {
  experiment?: { hypothesis?: string; change_type?: string; expected?: string } | null
  diff?: { added: number; removed: number; functions: string[] } | null
}) {
  return (
    <div className="rg-exp">
      {experiment?.change_type && <span className="cb-badge">改动：{CHANGE_LABEL[experiment.change_type] ?? experiment.change_type}</span>}
      {diff && (diff.added + diff.removed > 0) && (
        <span className="rg-diff"><b className="add">+{diff.added}</b> <b className="del">−{diff.removed}</b>{diff.functions.length ? ` · ${diff.functions.join('、')}` : ''}</span>
      )}
      {experiment?.hypothesis && <div><small>假设</small>{experiment.hypothesis}</div>}
      {experiment?.expected && <div><small>预期</small>{experiment.expected}</div>}
      {!experiment?.hypothesis && diff && diff.removed > 0 && <div className="rg-missing">这一版没有写修改假设与预期影响</div>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 调研报告                                                            */
/* ------------------------------------------------------------------ */
export function ReportCard({ m, projectId }: { m: LabMessage; projectId: string }) {
  const d = m.data
  const href = `#/report/${projectId}/${d.id}`
  return (
    <div className="lab2-card rg-report anim-in">
      <div className="lab2-card-head">
        <span className="rg-doc" aria-hidden>
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3"><path d="M4 1.8h5.2L12.5 5v9.2H4z" /><path d="M9 1.8V5h3.5M6 8h4.5M6 10.5h4.5" /></svg>
        </span>
        <b>{d.title || m.content}</b>
        <span className="lab2-card-gap" />
        <span className="lab2-faint">{Math.round((d.chars || 0) / 100) / 10}k 字</span>
      </div>
      {d.summary && <p className="rg-summary">{d.summary}</p>}
      <div className="button-row">
        <a className="button small primary" href={href}>查看报告</a>
        <a className="button small" href={`${href}?print=1`} target="_blank" rel="noreferrer">导出 PDF</a>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 子 Agent 并行研究                                                   */
/* ------------------------------------------------------------------ */
interface ChildRow {
  id: string; variant: string; title: string; v?: number; formal?: boolean | null
  metrics?: Record<string, number | null>; summary?: string
}

export function SubagentsCard({ m, onOpen }: { m: LabMessage; onOpen: (id: string) => void }) {
  const d = m.data
  const rows: ChildRow[] = d.children || []
  const running = d.status === 'running'
  const best = running ? null : rows.reduce<ChildRow | null>((a, r) => ((r.metrics?.sharpe ?? -1e9) > (a?.metrics?.sharpe ?? -1e9) ? r : a), null)
  return (
    <div className={`lab2-card rg-sub${running ? ' live' : ''}`}>
      <div className="lab2-card-head">
        <QuantEyes size={16} state={running ? 'strain' : 'happy'} motion={running ? 'full' : 'low'} />
        <b>子 Agent 并行研究</b>
        <span className="lab2-faint">{m.content}</span>
        <span className="lab2-card-gap" />
        <span className="lab2-faint">{running ? `${rows.length} 个方向进行中…` : `${rows.length} 个方向已完成`}</span>
      </div>
      <div className="rg-sub-grid stagger">
        {rows.map((r, i) => (
          <button key={r.id} type="button" className={`rg-sub-item${best?.id === r.id ? ' best' : ''}`} onClick={() => onOpen(r.id)} title="打开子对话">
            <span className="rg-sub-n">{i + 1}</span>
            <b>{r.variant}</b>
            {running && !r.metrics ? <span className="lab2-faint">研究中…</span> : r.metrics ? (
              <span className="rg-sub-m">
                收益 <i className={(r.metrics.total_return ?? 0) >= 0 ? 'up' : 'down'}>{pct(r.metrics.total_return)}</i>
                · 夏普 {num(r.metrics.sharpe)} · 回撤 {pct(r.metrics.max_drawdown)} · {r.metrics.trades ?? 0} 笔
                {r.formal === false && <em className="cb-badge warn">非正式</em>}
              </span>
            ) : <span className="lab2-faint">没有回测结果</span>}
            {r.summary && <small className="rg-sub-sum">{r.summary.replace(/\s+/g, ' ').slice(0, 160)}</small>}
          </button>
        ))}
      </div>
      {!running && <div className="lab2-faint">夏普最高不代表最好：对比时先看是否为正式回测、样本外与 2 倍成本表现。</div>}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 标的与周期切换                                                      */
/* ------------------------------------------------------------------ */
const TIMEFRAMES = ['250ms', '1s', '5s', '1m', '5m', '15m', '30m', '1h', '4h', '1d']
let symbolCache: string[] | null = null

export function useSymbols() {
  const [list, setList] = useState<string[]>(symbolCache ?? ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'DOGE/USDT'])
  useEffect(() => {
    if (symbolCache) return
    get<{ symbols: string[] }>('/api/market/symbols').then((r) => { symbolCache = r.symbols; setList(r.symbols) }).catch(() => undefined)
  }, [])
  return list
}

export interface SyncResult { symbol: string; ok: boolean; bars: number; coverage: number; gaps: number; errors: string[]; error?: string }

export async function syncData(symbols: string[], timeframe: string, days = 180) {
  const q = new URLSearchParams({ symbols: symbols.join(','), timeframe, days: String(days) })
  return post<{ ok: boolean; results: SyncResult[] }>(`/api/market/sync?${q}`)
}

export function MarketSwitcher({ symbols, timeframe, auto, onChange, multi, disabled, notify, compact }: {
  symbols: string[]; timeframe: string; auto?: boolean; onChange: (symbols: string[], timeframe: string) => void
  multi?: boolean; disabled?: boolean; notify: (msg: string, error?: boolean) => void; compact?: boolean
}) {
  const pop = usePopover()
  const all = useSymbols()
  const initiallyAuto = !!auto
  const [sel, setSel] = useState<string[]>(initiallyAuto ? [] : symbols)
  const [tf, setTf] = useState(timeframe)
  const [autoMode, setAutoMode] = useState(initiallyAuto)
  const [custom, setCustom] = useState('')
  const [syncing, setSyncing] = useState(false)
  useEffect(() => {
    if (!pop.open) return
    const isAuto = !!auto
    setAutoMode(isAuto)
    setSel(isAuto ? [] : symbols)
    setTf(timeframe)
  }, [pop.open]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (pop.open) return
    setAutoMode(!!auto)
    setSel(auto ? [] : symbols)
    setTf(timeframe)
  }, [auto, symbols.join('|'), timeframe]) // eslint-disable-line react-hooks/exhaustive-deps
  const choose = (s: string) => {
    setAutoMode(false)
    setSel((cur) => (multi ? (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]) : [s]))
  }
  const options = [...new Set([...symbols, ...all])]
  const label = !symbols.length ? '未设置' : symbols.length > 2 ? `${symbols[0]} 等 ${symbols.length} 个` : symbols.join(', ')
  const doSync = async () => {
    setSyncing(true)
    try {
      const r = await syncData(sel, tf)
      const bad = r.results.filter((x) => !x.ok || x.coverage < 0.9)
      notify(bad.length
        ? `已同步，${bad.map((x) => `${x.symbol} 覆盖 ${Math.round((x.coverage || 0) * 100)}%`).join('；')}`
        : `已同步 ${r.results.length} 个标的近 180 天 ${tf} 行情`, bad.some((x) => !x.ok))
    } catch (e) { notify(`同步失败：${(e as Error).message}`, true) } finally { setSyncing(false) }
  }
  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button type="button" className={`mk-switch${compact ? ' compact' : ''}${autoMode ? ' auto' : ''}`} disabled={disabled} onClick={() => pop.setOpen(!pop.open)}
        aria-haspopup="dialog" aria-expanded={pop.open} aria-label={autoMode ? '标的自动，点击设置' : `标的 ${label}，周期 ${timeframe}`}
        title={autoMode ? '标的自动：AI 根据对话选择；点击可手动设置' : `当前 ${label} · ${timeframe}；点击切换标的与周期`}>
        <span className="mk-ico" aria-hidden>
          {autoMode
            ? <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"><path d="M6 1.6v2M6 8.4v2M1.6 6h2M8.4 6h2M3 3l1.4 1.4M7.6 7.6 9 9M9 3 7.6 4.4M4.4 7.6 3 9" /></svg>
            : <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"><path d="M3 1.8v2.4M3 7.8v2.4M9 1.8v2.4M9 7.8v2.4M1.8 4.2h2.4M7.8 4.2h2.4M1.8 7.8h2.4M7.8 7.8h2.4" /></svg>}
        </span>
        <span className="mk-sym">{autoMode ? '自动' : label}</span>{!autoMode && <span className="mk-tf">{timeframe}</span>}
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
      </button>
      {pop.open && (
        <div ref={pop.menuRef} className="cmp-menu cmp-menu-right mk-menu" style={pop.menuStyle} data-placement={pop.placement} role="dialog" aria-label="标的与周期">
          <div className="cmp-menu-label">{multi ? '标的（可多选；自动=AI 按对话选择）' : '标的与周期（自动=AI 按对话选择）'}</div>
          <div className="mk-chips">
            <button type="button" className={`chip auto${autoMode ? ' on' : ''}`} aria-pressed={autoMode}
              onClick={() => { setAutoMode(true); setSel([]) }}>自动</button>
            {options.map((s) => (
              <button key={s} type="button" className={`chip${!autoMode && sel.includes(s) ? ' on' : ''}`} aria-pressed={!autoMode && sel.includes(s)} onClick={() => choose(s)}>{s.replace('/USDT', '')}</button>
            ))}
          </div>
          {autoMode && <div className="mk-auto-note">标的与周期都由 AI 根据对话决定；选择任一标的后即可手动设置周期。</div>}
          <form className="mk-add" onSubmit={(e) => {
            e.preventDefault()
            const v = custom.trim().toUpperCase()
            if (!v) return
            const s = v.includes('/') ? v : `${v}/USDT`
            setAutoMode(false)
            setSel((cur) => (multi ? [...new Set([...cur, s])] : [s]))
            setCustom('')
          }}>
            <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="添加其他标的，如 TON 或 ARB/USDT" />
            <button type="submit" className="button small">添加</button>
          </form>
          {!autoMode && (
            <>
              <div className="cmp-menu-label">周期</div>
              <div className="cmp-seg" role="group" aria-label="周期" style={{ gridTemplateColumns: `repeat(${TIMEFRAMES.length}, 1fr)` }}>
                {TIMEFRAMES.map((t) => <button key={t} type="button" className={tf === t ? 'on' : ''} aria-pressed={tf === t} onClick={() => setTf(t)}>{t}</button>)}
              </div>
            </>
          )}
          <div className="mk-foot">
            <button type="button" className="button small ghost" disabled={syncing || !sel.length} onClick={doSync}
              title="从交易所同步近 180 天真实行情到本地（补齐缺口）">{syncing ? '同步中…' : '同步数据'}</button>
            <span className="lab2-card-gap" />
            <button type="button" className="button small" onClick={() => pop.setOpen(false)}>取消</button>
            <button type="button" className="button small primary"
              onClick={() => { onChange(autoMode ? [] : sel, tf); pop.setOpen(false) }}>应用</button>
          </div>
        </div>
      )}
    </div>
  )
}
