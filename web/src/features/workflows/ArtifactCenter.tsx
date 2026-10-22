import { useEffect, useMemo, useState } from 'react'
import { get } from '../../http'
import ArtifactPreview from './ArtifactPreview'
import ArtifactReader, { type ReaderRow } from './ArtifactReader'
import {
  artifactPath,
  listPins,
  pinArtifact,
  unpinArtifact,
  versionKey,
  type ArtifactPin,
} from './artifactPins'

export type ArtifactRow = { hash: string; title: string; type: string; bytes: number; workflow_id: string; workflow_title: string; revision: number; run_id: string; node_id: string; port: string; mode: string; status: string; created_at?: number; origin?: string }

const pinToRow = (pin: ArtifactPin): ReaderRow => ({
  hash: pin.hash,
  title: pin.title,
  type: pin.type,
  workflow_id: pin.workflow_id,
  revision: 0,
  run_id: pin.run_id,
  node_id: pin.node_id,
  port: pin.port,
  mode: 'pinned',
  status: 'COMPLETED',
  origin: `固定于 ${new Date(pin.pinned_at * 1000).toLocaleString()}`,
})

export default function ArtifactCenter({
  workflowId,
  runId,
  refresh,
  onLocate,
  onCite,
}: {
  workflowId?: string
  runId?: string
  refresh?: string
  onLocate?: (row: ArtifactRow) => void
  onCite?: (row: ArtifactRow) => void
}) {
  const [scope, setScope] = useState<'current' | 'all' | 'pinned'>('current')
  const [query, setQuery] = useState('')
  const [rows, setRows] = useState<ArtifactRow[]>([])
  const [pins, setPins] = useState<ArtifactPin[]>([])
  const [selected, setSelected] = useState<ArtifactRow | null>(null)
  const [reader, setReader] = useState<ReaderRow | null>(null)
  const [error, setError] = useState('')
  const [total, setTotal] = useState(0)
  const [next, setNext] = useState<number | null>(null)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [pinBusy, setPinBusy] = useState(false)
  const [notice, setNotice] = useState('')

  const loadPins = () =>
    listPins()
      .then((value) => setPins(value.pins))
      .catch(() => setPins([]))

  useEffect(() => {
    setOffset(0)
    setSelected(null)
  }, [query, scope, workflowId, runId])

  useEffect(() => {
    let active = true
    if (scope === 'pinned') {
      setLoading(true)
      setError('')
      loadPins()
        .then(() => {
          if (active) setLoading(false)
        })
      return () => {
        active = false
      }
    }
    setLoading(true)
    setError('')
    const params = new URLSearchParams({ query, offset: String(offset), limit: '40' })
    if (scope === 'current' && workflowId) params.set('workflow_id', workflowId)
    if (runId) params.set('run_id', runId)
    const timer = setTimeout(() => {
      get<{ artifacts: ArtifactRow[]; total: number; next_offset: number | null }>(`/api/workflows/artifacts?${params}`)
        .then((value) => {
          if (active) {
            setRows(value.artifacts)
            setTotal(value.total)
            setNext(value.next_offset)
            setSelected((current) => value.artifacts.find((row) => row.hash === current?.hash && row.run_id === current?.run_id) || value.artifacts[0] || null)
          }
        })
        .catch((err) => {
          if (active) setError(String(err))
        })
        .finally(() => {
          if (active) setLoading(false)
        })
    }, 180)
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [query, scope, workflowId, runId, offset, refresh])

  useEffect(() => {
    void loadPins()
  }, [refresh, workflowId])

  const pinnedRows = useMemo(
    () => pins.filter((pin) => !workflowId || pin.workflow_id === workflowId).map(pinToRow),
    [pins, workflowId],
  )

  const currentPin = (row: ArtifactRow | null) => {
    if (!row) return null
    const key = versionKey(row)
    return pins.find((pin) => pin.hash === row.hash && pin.version_key === key) || null
  }

  const togglePin = async (row: ArtifactRow) => {
    if (pinBusy) return
    setPinBusy(true)
    setError('')
    setNotice('')
    try {
      const pin = currentPin(row)
      if (pin) {
        await unpinArtifact(pin.id)
        setNotice('已取消固定；缓存清理可以重新回收这个版本。')
      } else {
        await pinArtifact(row)
        setNotice('已固定；之后清理缓存会保留这个版本。')
      }
      await loadPins()
    } catch (err) {
      setError(String(err))
    } finally {
      setPinBusy(false)
    }
  }

  const openReader = (row: ArtifactRow) => {
    void artifactPath(row.hash)
    setReader({ ...row, bytes: row.bytes })
  }

  const visibleRows: ReaderRow[] = scope === 'pinned' ? pinnedRows.filter((row) => !query || `${row.title} ${row.workflow_id} ${row.type}`.toLowerCase().includes(query.toLowerCase())) : rows

  return (
    <section aria-label="研究产物中心">
      <div className="rs-modebar">
        <div className="rs-actions">
          <input className="rs-search" aria-label="搜索研究产物" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索图表、数据、指标或报告…" />
          {!runId && (
            <select aria-label="产物范围" value={scope} onChange={(event) => setScope(event.target.value as typeof scope)}>
              <option value="current">当前研究</option>
              <option value="all">全部研究</option>
              <option value="pinned">已固定</option>
            </select>
          )}
        </div>
        <small className="rs-muted">{scope === 'pinned' ? `已固定 ${pinnedRows.length} 项` : `最近500次运行 · ${total} 项产物`}</small>
      </div>
      {error && <p role="alert" className="rs-error">{error}</p>}
      {notice && <p role="status" className="rs-muted">{notice}</p>}
      {loading && <p role="status" className="rs-muted">正在检索产物…</p>}
      <div className="rs-center">
        <div className="rs-center-list">
          {scope === 'pinned'
            ? pinnedRows.map((row) => (
                <button className={`rs-center-item ${reader?.hash === row.hash ? 'on' : ''}`} key={`${row.hash}-${row.run_id}`} onClick={() => setReader(row)}>
                  <strong>{row.title}</strong>
                  <small>{row.workflow_id} · {row.run_id}</small>
                  <small>{row.type} · 已固定</small>
                </button>
              ))
            : rows.map((row) => {
                const pin = pinnedRows.find((pin) => pin.hash === row.hash && pin.run_id === row.run_id)
                return (
                  <button
                    className={`rs-center-item ${selected === row ? 'on' : ''}`}
                    key={`${row.run_id}-${row.node_id}-${row.title}`}
                    onClick={() => setSelected(row)}
                    onDoubleClick={() => openReader(row)}
                    title="双击打开专注阅读"
                  >
                    <strong>{row.title}{pin ? ' · 已固定' : ''}</strong>
                    <small>{row.workflow_title} · r{row.revision}</small>
                    <small>{row.type} · {row.mode === 'preview' ? '独立预览' : row.mode === 'debug' ? '调试产物' : '研究产物'} · {(row.bytes / 1024).toFixed(1)} KB</small>
                    <small>{row.run_id}</small>
                  </button>
                )
              })}
          {!visibleRows.length && !loading && (
            <div className="rs-empty">
              <strong>{scope === 'pinned' ? '还没有固定成果' : '还没有匹配的产物'}</strong>
              {scope === 'pinned' ? '在数据与日志或产物中心里固定一个真实运行产物，它会在这里长期保留。' : '运行研究后，数据、图表和文件会归档在这里。'}
            </div>
          )}
        </div>
        <div>
          {selected && scope !== 'pinned' && (
            <>
              <div className="rs-modebar">
                <span className="rs-muted">{selected.mode === 'preview' ? '独立图表预览，尚未关联研究运行' : selected.mode === 'debug' ? '调试输出，不作为正式研究证据' : '来自冻结运行的研究证据'}</span>
                <div className="rs-actions">
                  {onCite && selected.workflow_id === workflowId && selected.run_id && <button className="button small" onClick={() => onCite(selected)}>引用到研究笔记</button>}
                  {onLocate && selected.run_id && <button className="button small" onClick={() => onLocate(selected)}>定位来源节点 ↗</button>}
                  <button className="button small" disabled={pinBusy || !selected.run_id} onClick={() => void togglePin(selected)}>{currentPin(selected) ? '取消固定' : '固定成果'}</button>
                  <button className="button small primary" onClick={() => openReader(selected)}>专注阅读</button>
                </div>
              </div>
              <ArtifactPreview key={`${selected.run_id}-${selected.hash}`} hash={selected.hash} title={selected.title} provenance={`r${selected.revision} · ${selected.run_id}`} />
            </>
          )}
          {scope === 'pinned' && !pinnedRows.length && !loading && <div className="rs-empty"><strong>固定成果会出现在这里</strong>固定只保护对应 hash 的版本，不会被缓存清理回收。</div>}
        </div>
      </div>
      {scope !== 'pinned' && (
        <div className="rs-pagination">
          <span>{offset + 1}–{Math.min(offset + rows.length, total)} / {total}</span>
          <div className="rs-actions">
            <button className="button small" disabled={!offset || loading} onClick={() => setOffset(Math.max(0, offset - 40))}>上一页</button>
            <button className="button small" disabled={next == null || loading} onClick={() => setOffset(next!)}>下一页</button>
          </div>
        </div>
      )}
      {reader && <ArtifactReader row={reader} onClose={() => setReader(null)} onCite={onCite as ((row: ReaderRow) => void) | undefined} onLocate={onLocate as ((row: ReaderRow) => void) | undefined} />}
    </section>
  )
}
