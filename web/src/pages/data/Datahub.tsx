/**
 * 研究数据（行情与数据）：衍生品、链上、情绪、宏观连接器的同步状态与数据集，自定义 CSV 导入。
 * 每条数据按「发布可用时间」对齐，研究时不会用到当时还没发布的数据。
 */
import { useCallback, useEffect, useState } from 'react'
import { datahub, type AutoSync, type Connector, type Dataset } from '../../apiFactors'
import { LineChart } from '../../components/charts2'
import { Callout, EmptyState, Field, Loading, Panel, Tag } from '../../components/ui'
import type { PageProps } from '../types'
import { appConfirm } from '../../components/dialog'

const day = (ms?: number | null) => (ms ? new Date(ms).toISOString().slice(0, 10) : '—')
const ago = (ms?: number | null) => {
  if (!ms) return '从未同步'
  const s = (Date.now() - ms) / 1000
  return s < 120 ? '刚刚' : s < 7200 ? `${Math.round(s / 60)} 分钟前` : s < 172800 ? `${Math.round(s / 3600)} 小时前` : `${Math.round(s / 86400)} 天前`
}

export default function DatahubSection({ notify }: Pick<PageProps, 'notify'>) {
  const [conns, setConns] = useState<Connector[] | null>(null)
  const [canKeys, setCanKeys] = useState(false)
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [syncing, setSyncing] = useState<string | null>(null)
  const [preview, setPreview] = useState<{ ds: Dataset; rows: [number, number, number][] } | null>(null)

  const load = useCallback(async () => {
    try {
      const [c, d] = await Promise.all([datahub.connectors(), datahub.datasets()])
      setConns(c.connectors)
      setCanKeys(c.can_edit_keys)
      setDatasets(d.datasets)
    } catch (e) { notify((e as Error).message, true) }
  }, [notify])
  useEffect(() => { load() }, [load])

  const sync = async (c: Connector, extra: { symbols?: string[]; series?: string; name?: string } = {}) => {
    setSyncing(c.id)
    try {
      const r = await datahub.sync({ connector: c.id, ...extra, wait: true })
      const n = r.datasets?.length ?? 0
      const errs = r.errors ?? []
      notify(errs.length ? `同步了 ${n} 个数据集，${errs.length} 个失败：${errs[0].error.slice(0, 80)}` : `同步完成：${n} 个数据集`, errs.length > 0 && n === 0)
      await load()
    } catch (e) { notify((e as Error).message, true) } finally { setSyncing(null) }
  }

  const open = async (ds: Dataset) => {
    try { const r = await datahub.series(ds.key, 600); setPreview({ ds: r.dataset, rows: r.rows }) } catch (e) { notify((e as Error).message, true) }
  }

  if (!conns) return <Loading label="正在读取行情与数据" />
  return (
    <>
      <Callout>
        研究数据都按「当时能不能拿到」对齐：日频数据按次日可用处理，资金费率按结算时刻可用。因子里直接用字段名引用，例如 <code>funding</code>、<code>fng</code>；
        自定义数据用 <code>x_名字</code>。没有同步过的字段，评估时会如实报「未接入数据」，先在这里同步。
      </Callout>
      <AutoSyncBar notify={notify} onSynced={load} />
      <div className="dh-grid">
        {conns.map((c) => <ConnectorCard key={c.id} c={c} canKeys={canKeys} busy={syncing === c.id} onSync={(x) => sync(c, x)}
          onKey={async (k) => { try { await datahub.setKey(c.id, k); notify(k ? '密钥已保存' : '密钥已清除'); load() } catch (e) { notify((e as Error).message, true) } }}
          onImported={() => load()} notify={notify} />)}
      </div>

      <Panel flush title={`数据集（${datasets.length}）`}>
        {datasets.length === 0 ? <EmptyState glyph="⇄" title="还没有研究数据">点上面任一连接器的「同步」开始。</EmptyState> : (
          <div className="table-wrap">
            <table className="fx-table">
              <thead><tr><th>字段</th><th>标的</th><th>来源</th><th className="right">数据点</th><th>覆盖</th><th>状态</th><th /></tr></thead>
              <tbody>
                {datasets.map((d) => (
                  <tr key={d.key} className="clickable" onClick={() => open(d)}>
                    <td><code>{d.field}</code></td>
                    <td>{d.symbol === '*' ? '全市场' : d.symbol}</td>
                    <td className="muted">{d.source}{d.series ? ` · ${d.series}` : ''}</td>
                    <td className="right num">{d.rows.toLocaleString()}</td>
                    <td className="muted">{day(d.start)} → {day(d.end)}</td>
                    <td>{d.status === 'error' ? <Tag tone="fail">失败</Tag> : d.status === 'syncing' ? <Tag tone="warn">同步中</Tag> : <Tag tone="ok">可用</Tag>}
                      {d.error && <small className="muted" title={d.error}>{d.error.slice(0, 40)}</small>}</td>
                    <td className="right">
                      <button className="button small danger" onClick={async (e) => {
                        e.stopPropagation()
                        if (!await appConfirm(`删除数据集 ${d.key}？`)) return
                        await datahub.remove(d.key); if (preview?.ds.key === d.key) setPreview(null); load()
                      }}>删除</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {preview && (
        <Panel title={`${preview.ds.field} · ${preview.ds.symbol === '*' ? '全市场' : preview.ds.symbol} · ${preview.ds.source}`}
          actions={<button className="button small" onClick={() => setPreview(null)}>关闭</button>}>
          <LineChart series={[{ name: preview.ds.field, values: preview.rows.map((r) => r[2]) }]} timestamps={preview.rows.map((r) => r[0])}
            format="num" zeroLine={false} height={220} ariaLabel="数据预览" />
          <div className="hint">共 {preview.rows.length} 个点（等间隔抽样）。发布滞后：中位数约 {lagText(preview.rows)}。</div>
        </Panel>
      )}
    </>
  )
}

function lagText(rows: [number, number, number][]) {
  if (!rows.length) return '—'
  const lags = rows.map((r) => r[1] - r[0]).sort((a, b) => a - b)
  const m = lags[Math.floor(lags.length / 2)]
  return m >= 86_400_000 ? `${(m / 86_400_000).toFixed(1)} 天` : m >= 3_600_000 ? `${(m / 3_600_000).toFixed(1)} 小时` : m > 0 ? `${Math.round(m / 60000)} 分钟` : '0（即时）'
}

function ConnectorCard({ c, canKeys, busy, onSync, onKey, onImported, notify }: {
  c: Connector; canKeys: boolean; busy: boolean
  onSync: (x: { symbols?: string[]; series?: string; name?: string }) => void
  onKey: (k: string) => void; onImported: () => void; notify: PageProps['notify']
}) {
  const [symbols, setSymbols] = useState('BTC/USDT, ETH/USDT, SOL/USDT')
  const [series, setSeries] = useState('')
  const [name, setName] = useState('')
  const [key, setKey] = useState('')
  const [csv, setCsv] = useState<{ text: string; file: string } | null>(null)
  const [lag, setLag] = useState(0)
  const perSymbol = c.scope === 'symbol'
  const needsKey = c.needs_key && !c.has_key

  if (c.id === 'csv') {
    return (
      <div className="dh-card">
        <div className="dh-head"><b>{c.name}</b><Tag>{c.category}</Tag><span className="dh-gap" /><span className="muted">{c.datasets} 个</span></div>
        <p className="dh-note">{c.note}</p>
        <div className="dh-form">
          <Field label="名字（英文）"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="例如 etf_flow" /></Field>
          <Field label="发布滞后（小时）"><input type="number" min={0} value={lag} onChange={(e) => setLag(Number(e.target.value))} /></Field>
        </div>
        <input type="file" accept=".csv,text/csv" onChange={async (e) => {
          const f = e.target.files?.[0]; if (!f) return
          if (f.size > 5_000_000) { notify('CSV 最大 5 MB', true); return }
          setCsv({ text: await f.text(), file: f.name })
          if (!name) setName(f.name.replace(/\.csv$/i, '').replace(/[^A-Za-z0-9_]/g, '_').toLowerCase())
        }} />
        <div className="dh-actions">
          <button className="button small primary" disabled={!csv || !name.trim()} onClick={async () => {
            try {
              const r = await datahub.importCsv({ name, text: csv!.text, lag_hours: lag })
              notify(`已导入 ${r.dataset.rows} 个点，用 ${r.dataset.field} 引用`); setCsv(null); onImported()
            } catch (e) { notify((e as Error).message, true) }
          }}>导入</button>
          {csv && <span className="muted">{csv.file}</span>}
        </div>
      </div>
    )
  }

  return (
    <div className="dh-card">
      <div className="dh-head">
        <b>{c.name}</b><Tag>{c.category}</Tag>{c.needs_key && <Tag tone={c.has_key ? 'ok' : 'warn'}>{c.has_key ? '已填密钥' : '需要密钥'}</Tag>}
        <span className="dh-gap" />
        {c.url && <a className="muted" href={c.url} target="_blank" rel="noreferrer">文档</a>}
      </div>
      <p className="dh-note">{c.note}</p>
      {c.fields.length > 0 && <div className="dh-fields">{c.fields.map((f) => <code key={f}>{f}</code>)}</div>}
      <div className="dh-form">
        {perSymbol && <Field label="标的"><input value={symbols} onChange={(e) => setSymbols(e.target.value)} /></Field>}
        {(c.id === 'fred' || c.id === 'glassnode') && <>
          <Field label={c.id === 'fred' ? '序列号' : '指标路径'}><input value={series} onChange={(e) => setSeries(e.target.value)} placeholder={c.id === 'fred' ? 'DGS10' : 'addresses/active_count'} /></Field>
          <Field label="引用名"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="可选，例如 us10y" /></Field>
        </>}
        {c.needs_key && canKeys && (
          <Field label="密钥"><input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={c.has_key ? '已保存（填写可替换）' : '粘贴密钥'} autoComplete="off" /></Field>
        )}
      </div>
      <div className="dh-actions">
        {c.needs_key && canKeys && <button className="button small" disabled={!key.trim()} onClick={() => { onKey(key); setKey('') }}>保存密钥</button>}
        <button className="button small primary" disabled={busy || needsKey} title={needsKey ? '先填写密钥' : ''}
          onClick={() => onSync({ symbols: perSymbol ? symbols.split(/[,，\s]+/).filter(Boolean) : undefined, series, name })}>
          {busy ? <><span className="spinner" /> 同步中</> : '同步'}
        </button>
        <span className="muted">{c.datasets} 个数据集 · {ago(c.last_sync)}{c.errors ? ` · ${c.errors} 个失败` : ''}</span>
      </div>
    </div>
  )
}

const INTERVALS = [1, 3, 6, 12, 24]

/** 自动同步：开启后 worker 按间隔把已有数据集补到最新；打开本页时如果已经到期，也会立即补一次。 */
function AutoSyncBar({ notify, onSynced }: Pick<PageProps, 'notify'> & { onSynced: () => void }) {
  const [st, setSt] = useState<AutoSync | null>(null)
  const [running, setRunning] = useState(false)
  const refresh = useCallback(() => datahub.auto().then(setSt).catch(() => undefined), [])
  useEffect(() => {
    datahub.auto().then((a) => {
      setSt(a)
      if (a.enabled && a.due) { setRunning(true); datahub.autoRun().catch(() => undefined) }
    }).catch(() => undefined)
  }, [])
  // 后台补数据时轮询，完成后刷新数据集列表
  useEffect(() => {
    if (!running) return
    const since = st?.last_run ?? 0
    const t = window.setInterval(async () => {
      const a = await datahub.auto().catch(() => null)
      if (a && (a.last_run ?? 0) > since) { setSt(a); setRunning(false); onSynced() }
    }, 3000)
    const stop = window.setTimeout(() => setRunning(false), 180_000)
    return () => { window.clearInterval(t); window.clearTimeout(stop) }
  }, [running]) // eslint-disable-line react-hooks/exhaustive-deps
  if (!st) return null
  const save = async (enabled: boolean, hours: number) => {
    try { setSt(await datahub.setAuto(enabled, hours)); notify(enabled ? `已开启自动同步：每 ${hours} 小时把已有数据集补到最新` : '已关闭自动同步') } catch (e) { notify((e as Error).message, true) }
  }
  const r = st.last_result
  return (
    <div className="dh-auto">
      <button type="button" role="switch" aria-checked={st.enabled} aria-label="自动同步" className={`switch${st.enabled ? ' on' : ''}`} onClick={() => save(!st.enabled, st.interval_hours)}><i /></button>
      <div className="dh-auto-text">
        <b>自动同步</b>
        <small>
          {st.enabled ? <>每 <select value={st.interval_hours} onChange={(e) => save(true, Number(e.target.value))}>
            {INTERVALS.map((h) => <option key={h} value={h}>{h} 小时</option>)}</select> 把已同步过的数据集补到最新</> : '关闭：数据只在你点「同步」时更新'}
          {st.last_run ? ` · 上次 ${ago(st.last_run)}${r ? `：${r.datasets} 个数据集${r.errors.length ? `，${r.errors.length} 个失败` : ''}` : ''}` : ''}
        </small>
      </div>
      <button type="button" className="button small" disabled={running} onClick={async () => { setRunning(true); try { await datahub.autoRun(); refresh() } catch (e) { setRunning(false); notify((e as Error).message, true) } }}>
        {running ? <><span className="spinner" /> 同步中</> : '立即补到最新'}
      </button>
    </div>
  )
}
