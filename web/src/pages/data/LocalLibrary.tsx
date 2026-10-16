/**
 * 本地行情库：已经同步到本机的真实 K 线（每个标的 / 周期一份），能看到覆盖区间、根数与最后更新时间，
 * 一键补齐到最新或按天数回填；同步会分段拉取并补上中间的缺口。AI 研究室的 Agent 也用同一套同步。
 */
import { useCallback, useEffect, useState } from 'react'
import { del, get } from '../../http'
import { EmptyState, Loading, Panel } from '../../components/ui'
import { syncData, useSymbols, type SyncResult } from '../lab/research'
import { appConfirm } from '../../components/dialog'

interface Entry { exchange: string; symbol: string; timeframe: string; bars: number; start_ts: number; end_ts: number; bytes: number; format: string }

const TF_MS: Record<string, number> = { '1m': 60_000, '5m': 300_000, '15m': 900_000, '30m': 1_800_000, '1h': 3_600_000, '4h': 14_400_000, '1d': 86_400_000 }
const day = (ms: number) => (ms ? new Date(ms).toISOString().slice(0, 10) : '—')
const ago = (ms: number) => {
  const h = (Date.now() - ms) / 3_600_000
  return h < 1 ? '最新' : h < 48 ? `${Math.round(h)} 小时前` : `${Math.round(h / 24)} 天前`
}

export function LocalLibrary({ notify }: { notify: (m: string, e?: boolean) => void }) {
  const [rows, setRows] = useState<Entry[] | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')
  const symbols = useSymbols()
  const [form, setForm] = useState({ symbols: ['BTC/USDT', 'ETH/USDT'], timeframe: '1h', days: 180 })
  const [last, setLast] = useState<SyncResult[] | null>(null)

  const load = useCallback(() => {
    get<{ entries: Entry[]; last_errors: Record<string, string> }>('/api/market/cache')
      .then((r) => { setRows(r.entries.sort((a, b) => a.symbol.localeCompare(b.symbol) || (TF_MS[a.timeframe] ?? 0) - (TF_MS[b.timeframe] ?? 0))); setErrors(r.last_errors || {}) })
      .catch(() => setRows([]))
  }, [])
  useEffect(load, [load])

  const run = async (key: string, syms: string[], tf: string, days: number) => {
    setBusy(key)
    try {
      const r = await syncData(syms, tf, days)
      setLast(r.results)
      const bad = r.results.filter((x) => !x.ok)
      notify(bad.length ? `${bad.length} 个标的同步失败：${bad[0].errors?.[0] ?? bad[0].error ?? '交易所没有返回数据'}` : `已同步 ${r.results.length} 个标的的 ${tf} 行情`, bad.length > 0)
      load()
    } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }
  const stale = (e: Entry) => Date.now() - e.end_ts > 3 * (TF_MS[e.timeframe] ?? 3_600_000)

  return (
    <Panel title="本地行情库" subtitle="已同步到本机的真实 K 线。同步会分段拉取并补齐中间的缺口；回测与 AI 研究室都优先读这里。"
      actions={<button className="button small" onClick={load}>刷新</button>}>
      <form className="lib-sync" onSubmit={(e) => { e.preventDefault(); run('form', form.symbols, form.timeframe, form.days) }}>
        <div className="lib-chips">
          {symbols.map((s) => (
            <button key={s} type="button" className={`chip${form.symbols.includes(s) ? ' on' : ''}`} aria-pressed={form.symbols.includes(s)}
              onClick={() => setForm({ ...form, symbols: form.symbols.includes(s) ? form.symbols.filter((x) => x !== s) : [...form.symbols, s] })}>{s.replace('/USDT', '')}</button>
          ))}
        </div>
        <label>周期
          <select value={form.timeframe} onChange={(e) => setForm({ ...form, timeframe: e.target.value })}>
            {Object.keys(TF_MS).map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
        <label>回看
          <select value={form.days} onChange={(e) => setForm({ ...form, days: Number(e.target.value) })}>
            {[30, 90, 180, 365, 730, 1095].map((d) => <option key={d} value={d}>{d >= 365 ? `${d / 365} 年` : `${d} 天`}</option>)}
          </select>
        </label>
        <button className="button primary" disabled={!!busy || !form.symbols.length}>{busy === 'form' ? '同步中…' : `同步 ${form.symbols.length} 个标的`}</button>
      </form>
      {last && (
        <div className="lib-last anim-fade">
          {last.map((r) => (
            <span key={r.symbol} className={`cb-badge ${r.ok && r.coverage >= 0.9 && !r.gaps ? 'ok' : 'warn'}`}>
              {r.symbol} {r.ok ? `${r.bars} 根 · 覆盖 ${Math.round(r.coverage * 100)}%${r.gaps ? ` · ${r.gaps} 处缺口` : ''}` : '失败'}
            </span>
          ))}
        </div>
      )}
      {Object.entries(errors).filter(([, v]) => v).map(([k, v]) => <div key={k} className="hint">最近一次 {k} 拉取失败：{v}</div>)}
      {!rows ? <Loading /> : !rows.length ? (
        <EmptyState glyph="⇄" title="本地还没有行情">选好标的和周期点「同步」，或者在 AI 研究室里让 AI 自己同步。</EmptyState>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>标的</th><th>周期</th><th>覆盖区间（UTC）</th><th className="right">K 线</th><th>更新</th><th /></tr></thead>
            <tbody>
              {rows.map((e) => {
                const key = `${e.exchange}:${e.symbol}:${e.timeframe}`
                const span = Math.max(1, Math.ceil((Date.now() - e.start_ts) / 86_400_000))
                return (
                  <tr key={key}>
                    <td><b>{e.symbol}</b><small>{e.exchange} · {(e.bytes / 1024).toFixed(0)} KB</small></td>
                    <td>{e.timeframe}</td>
                    <td>{day(e.start_ts)} → {day(e.end_ts)}<small>约 {Math.round((e.end_ts - e.start_ts) / 86_400_000)} 天</small></td>
                    <td className="right num">{e.bars.toLocaleString()}</td>
                    <td className={stale(e) ? 'negative' : 'muted'}>{ago(e.end_ts)}</td>
                    <td className="right">
                      <div className="button-row" style={{ justifyContent: 'flex-end' }}>
                        <button className="button small" disabled={!!busy} onClick={() => run(key, [e.symbol], e.timeframe, Math.min(1500, span))}>
                          {busy === key ? '同步中…' : stale(e) ? '补到最新' : '检查缺口'}
                        </button>
                        <button className="button small ghost" disabled={!!busy} onClick={async () => {
                          if (!await appConfirm(`删除本地缓存的 ${e.symbol} ${e.timeframe} 行情？之后需要时会重新从交易所拉取。`)) return
                          await del(`/api/market/cache?exchange=${e.exchange}&symbol=${encodeURIComponent(e.symbol)}&timeframe=${e.timeframe}`)
                          load()
                        }}>删除</button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}
