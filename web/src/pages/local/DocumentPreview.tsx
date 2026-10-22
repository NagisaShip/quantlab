import { useState } from 'react'
import { post, withWs } from '../../http'

type Preview = { pages: { page: number; url: string }[]; total_pages: number; next_page: number | null }
export default function DocumentPreview({ path }: { path: string }) {
  const [preview, setPreview] = useState<Preview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function load(start = 1) {
    setBusy(true); setError('')
    try { setPreview(await post<Preview>(`/api/local/files/document-preview?start=${start}`, { path })) }
    catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  return <div className="lp-block">
    <div className="lp-inline"><button className="button" disabled={busy} onClick={() => void load()}>查看文档页面</button><a className="button" href={withWs(`/api/local/files/download?path=${encodeURIComponent(path)}`)} download>下载文件</a></div>
    {busy && <p role="status">正在渲染页面…</p>}{error && <p role="alert">{error}</p>}
    {preview && <><p>共 {preview.total_pages} 页</p>{preview.pages.map(page => <figure key={page.page}><img src={withWs(page.url)} alt={`第 ${page.page} 页`} style={{ width: '100%', maxWidth: 1000, height: 'auto' }}/><figcaption>第 {page.page} 页</figcaption></figure>)}<div className="lp-inline"><button className="button" disabled={busy || preview.pages[0]?.page === 1} onClick={() => void load(Math.max(1, preview.pages[0].page - 4))}>上一组</button><button className="button" disabled={busy || !preview.next_page} onClick={() => void load(preview.next_page!)}>下一组</button></div></>}
  </div>
}
