/**
 * 成果专注阅读器（P3）。
 *
 * 打开一个真实运行工件的大图/大表阅读面，并提供：固定/取消固定、同版本历史（version_key）、
 * 下载、本机打开文件夹 / 系统应用、引用到笔记、定位来源节点。
 * 账号模式没有本机路径（/api/local/artifacts/{hash}/path 404），本机动作自动隐藏。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { desktopKind, openWithSystem, revealPath } from '../../native'
import ArtifactPreview from './ArtifactPreview'
import {
  artifactPath,
  evalPinState,
  listVersions,
  pinArtifact,
  unpinArtifact,
  versionKey,
  type ArtifactPathInfo,
  type ArtifactPin,
  type ArtifactVersion,
} from './artifactPins'
import './artifactReader.css'

export type ReaderRow = {
  hash: string
  title: string
  type: string
  bytes?: number
  workflow_id: string
  workflow_title?: string
  revision: number
  run_id: string
  node_id: string
  port: string
  mode: string
  status: string
  created_at?: number
  origin?: string
}

const when = (ts?: number) => (ts ? new Date(ts * 1000).toLocaleString() : '—')
const size = (bytes?: number) => (bytes == null ? '—' : bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`)

export default function ArtifactReader({
  row,
  onClose,
  onCite,
  onLocate,
}: {
  row: ReaderRow
  onClose: () => void
  onCite?: (row: ReaderRow) => void
  onLocate?: (row: ReaderRow) => void
}) {
  const [pin, setPin] = useState<ArtifactPin | null>(null)
  const [versions, setVersions] = useState<ArtifactVersion[]>([])
  const [versionNote, setVersionNote] = useState('')
  const [pathInfo, setPathInfo] = useState<ArtifactPathInfo | null>(null)
  const [previewHash, setPreviewHash] = useState(row.hash)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const closeRef = useRef<HTMLButtonElement>(null)
  const native = desktopKind() === 'tauri'
  const key = useMemo(() => versionKey(row), [row])

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    let active = true
    setLoading(true)
    setError('')
    Promise.allSettled([evalPinState(row), listVersions(row.hash), artifactPath(row.hash)])
      .then(([pinResult, versionResult, pathResult]) => {
        if (!active) return
        if (pinResult.status === 'fulfilled') setPin(pinResult.value)
        if (versionResult.status === 'fulfilled') {
          setVersions(versionResult.value.versions)
          setVersionNote(versionResult.value.reason || '')
        }
        if (pathResult.status === 'fulfilled') setPathInfo(pathResult.value)
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [row])

  const togglePin = async () => {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      if (pin) {
        await unpinArtifact(pin.id)
        setPin(null)
        setNotice('已取消固定；缓存清理可以重新回收这个版本。')
      } else {
        const created = await pinArtifact(row)
        setPin(created)
        setNotice('已固定；之后清理缓存会保留这个版本。')
      }
      const refreshed = await listVersions(row.hash)
      setVersions(refreshed.versions)
    } catch (err) {
      setError(String(err))
    } finally {
      setBusy(false)
    }
  }

  const openLocal = async (kind: 'reveal' | 'open') => {
    if (!pathInfo) return
    const ok = kind === 'reveal' ? await revealPath(pathInfo.path) : await openWithSystem(pathInfo.path)
    if (!ok) setError('本机打开失败：请确认使用桌面应用并在本地项目中。')
  }

  return (
    <div className="ar-overlay" role="dialog" aria-modal="true" aria-label={`专注阅读：${row.title}`} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <section className="ar-panel">
        <header className="ar-head">
          <div className="ar-title">
            <small>成果专注阅读</small>
            <h2 title={row.title}>{row.title}</h2>
            <p>
              {row.workflow_title || row.workflow_id} · r{row.revision} · {row.run_id} · {row.type} · {size(row.bytes)}
            </p>
          </div>
          <div className="ar-actions">
            {onCite && row.run_id && <button type="button" className="button small" onClick={() => onCite(row)}>引用到研究笔记</button>}
            {onLocate && row.run_id && <button type="button" className="button small" onClick={() => onLocate(row)}>定位来源节点</button>}
            <button type="button" className="button small" disabled={busy} onClick={() => void togglePin()}>{pin ? '取消固定' : '固定成果'}</button>
            <a className="button small" href={`/api/workflows/artifacts/${encodeURIComponent(row.hash)}/download`}>下载 JSON</a>
            {pathInfo && native && (
              <>
                <button type="button" className="button small" onClick={() => void openLocal('reveal')}>打开文件夹</button>
                <button type="button" className="button small" onClick={() => void openLocal('open')}>用系统应用打开</button>
              </>
            )}
            <button ref={closeRef} type="button" className="button small primary" onClick={onClose} aria-label="关闭专注阅读">关闭 ×</button>
          </div>
        </header>

        {error && <p role="alert" className="ar-error">{error}</p>}
        {notice && <p role="status" className="ar-notice">{notice}</p>}

        <div className="ar-body">
          <div className="ar-main">
            {previewHash !== row.hash && (
              <div className="ar-compare">
                <span>正在查看历史版本 <code>{previewHash.slice(0, 12)}…</code></span>
                <button type="button" className="button small" onClick={() => setPreviewHash(row.hash)}>返回当前版本</button>
              </div>
            )}
            <ArtifactPreview key={previewHash} hash={previewHash} title={previewHash === row.hash ? row.title : `历史版本 · ${previewHash.slice(0, 10)}`} provenance={`r${row.revision} · ${row.run_id}`} />
          </div>

          <aside className="ar-side">
            <div className="ar-card">
              <h3>版本历史</h3>
              <p className="ar-muted">同一节点端口（{key || '—'}）的历史运行产物{loading ? ' · 正在读取…' : ` · ${versions.length} 个`}</p>
              {versionNote && <p className="ar-muted">{versionNote}</p>}
              <ul className="ar-versions">
                {versions.map((version) => (
                  <li key={`${version.run_id}-${version.hash}`} className={version.hash === previewHash ? 'on' : ''}>
                    <button type="button" onClick={() => setPreviewHash(version.hash)}>
                      <span>
                        <strong>r{version.revision}</strong> · {when(version.created_at)}
                        {version.hash === row.hash && <em> · 当前</em>}
                        {version.pinned && <em> · 已固定</em>}
                        {version.available === false && <em> · 已清理</em>}
                      </span>
                      <small>{version.run_id} · {version.type || row.type} · {size(version.bytes)}</small>
                    </button>
                  </li>
                ))}
                {!versions.length && !loading && <li className="ar-muted">还没有同版本历史。</li>}
              </ul>
            </div>

            <div className="ar-card">
              <h3>证据与来源</h3>
              <dl>
                <dt>工作区</dt><dd>{row.workflow_id}</dd>
                <dt>运行</dt><dd>{row.run_id}</dd>
                <dt>节点 / 端口</dt><dd>{row.node_id} / {row.port}</dd>
                <dt>类型 / 状态</dt><dd>{row.type} / {row.status}</dd>
                <dt>产物哈希</dt><dd><code>{row.hash}</code></dd>
                {row.origin && <><dt>来源</dt><dd>{row.origin}</dd></>}
                {pathInfo && <><dt>本机文件</dt><dd className="ar-path">{pathInfo.path}</dd></>}
              </dl>
              {pin && <p className="ar-muted">固定于 {when(pin.pinned_at)} · {pin.pinned_by}</p>}
            </div>
          </aside>
        </div>
      </section>
    </div>
  )
}
