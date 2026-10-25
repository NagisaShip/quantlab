import { withWs } from '../../http'

const snapshotUrl = /^\/api\/local\/runtime-artifacts\/[A-Za-z0-9_-]+\/[a-f0-9]{64}$/

/** Only validated same-origin snapshot references become interactive content. */
export function ArtifactDelivery({ value }: { value?: unknown }) {
  let result: Record<string, unknown>
  try { result = typeof value === 'string' ? JSON.parse(value) : value as Record<string, unknown> } catch { return null }
  if (!result || typeof result !== 'object') return null
  const download = typeof result.download_url === 'string' && snapshotUrl.test(result.download_url) ? result.download_url : ''
  const pages = Array.isArray(result.pages) ? result.pages.slice(0, 8).filter((page): page is { page: number; url: string } =>
    !!page && typeof page === 'object' && Number.isInteger(page.page) && typeof page.url === 'string' && snapshotUrl.test(page.url)) : []
  if (!download && !pages.length) return null
  return <div className="cb-delivery">
    {download && <a className="button small" href={withWs(download)} download>下载文件</a>}
    {pages.map(page => <figure key={page.url}><img src={withWs(page.url)} alt={`第 ${page.page} 页`} loading="lazy" style={{ maxWidth: '100%', maxHeight: 640, objectFit: 'contain' }}/><figcaption>第 {page.page} 页</figcaption></figure>)}
  </div>
}
