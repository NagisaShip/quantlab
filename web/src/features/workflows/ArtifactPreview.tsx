import { useEffect, useState } from 'react'
import { currentWorkspace, get, withWs } from '../../http'
import './researchStudio.css'

export type ArtifactView = { format?: string; workspace_id?: string; artifact_hash: string; value: unknown; total?: number | null; offset?: number; truncated: boolean; next_offset?: number | null; files?: {file:string;name:string;bytes?:number;image?:boolean;url?:string}[]; download?: string }
const text = (v: unknown) => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v)
function metricEntries(value:unknown,prefix='',depth=0):[string,number|boolean][] {
  if(!value||typeof value!=='object'||Array.isArray(value)||depth>3)return []
  return Object.entries(value).flatMap(([key,v])=>{
    if(['files','config','environment','inputs','code_hash','sha256'].includes(key))return []
    const label=prefix?`${prefix} / ${key}`:key
    return typeof v==='number'||typeof v==='boolean'?[[label,v] as [string,number|boolean]]:metricEntries(v,label,depth+1)
  })
}

const isImageFile = (file: { file:string; image?:boolean }) => file.image || /\.(png|jpe?g|webp|gif)$/i.test(file.file)


export default function ArtifactPreview({ hash, initial, fileName, title = '研究产物', provenance, compact = false }: { hash?:string; initial?:ArtifactView; fileName?:string; title?:string; provenance?:string; compact?:boolean }) {
  const digest = hash || initial?.artifact_hash || ''
  const [value, setValue] = useState<ArtifactView | null>(initial || null)
  const [offset, setOffset] = useState(initial?.offset || 0)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [zoom, setZoom] = useState('')
  const [filePreview,setFilePreview]=useState('')
  const [rowQuery,setRowQuery]=useState('')
  const [details,setDetails]=useState(false)
  const workspace = currentWorkspace()
  const allowed = !initial?.workspace_id || initial.workspace_id === workspace
  useEffect(() => { setOffset(0); setRowQuery(''); setValue(initial || null); setDetails(false) }, [digest, fileName, workspace])
  useEffect(() => {
    if ((!digest && !fileName) || !allowed) return
    let active = true
    setLoading(true); setError('')
    get<ArtifactView>(fileName?`/api/workflows/files/preview?name=${encodeURIComponent(fileName)}&offset=${offset}&limit=20`:`/api/workflows/artifacts/${encodeURIComponent(digest)}?offset=${offset}&limit=20`)
      .then(v => { if(active) setValue(v) }).catch(e => { if(active) {setValue(null);setError(String(e))} }).finally(() => {if(active) setLoading(false)})
    return () => {active = false}
  }, [digest, fileName, offset, allowed, workspace])
  useEffect(() => { if(!zoom) return; const key=(e:KeyboardEvent)=>{if(e.key==='Escape')setZoom('')};window.addEventListener('keydown',key);return()=>window.removeEventListener('keydown',key) }, [zoom])
  if(!allowed) return <p className="rs-muted">请切回产物所属工作区查看。</p>
  const rows = Array.isArray(value?.value) ? value.value : null
  const table = rows?.every(row => row !== null && typeof row === 'object' && !Array.isArray(row))
  const columns = table ? [...new Set(rows!.flatMap(row => Object.keys(row as object)))].slice(0,40) : []
  const data = value?.value
  const metrics = metricEntries(data)
  const visibleRows=(table?rows||[]:[]).map((row,i)=>({row,index:offset+i+1})).filter(({row})=>!rowQuery||Object.values(row as Record<string,unknown>).some(v=>text(v).toLowerCase().includes(rowQuery.toLowerCase())))
  const download = fileName?`/api/workflows/files/download?name=${encodeURIComponent(fileName)}`:value?.download || `/api/workflows/artifacts/${digest}/download`
  const compactMode = compact && !details
  const imageFiles = (value?.files || []).filter(isImageFile)
  const firstImage = imageFiles[0]
  const firstImageUrl = firstImage ? withWs(firstImage.url || `/api/workflows/files/download?name=${encodeURIComponent(firstImage.file)}`) : ''
  return <section className={`rs-artifact${compactMode?' rs-artifact-compact':''}`} aria-label={title} aria-busy={loading}>
    <header className="rs-artifact-head"><div className="rs-artifact-title" title={provenance ? `${title} · ${provenance}` : title}><strong>{title}</strong>{!compactMode && provenance && <small>{provenance}</small>}</div><div className="rs-actions"><a className="button small" href={withWs(download)}>{fileName?'下载文件':'下载 JSON'}</a>{table && !fileName && <a className="button small" href={withWs(download+'?format=csv')}>下载 CSV</a>}{compact && <button type="button" className="button small rs-artifact-details" aria-pressed={details} onClick={()=>setDetails(v=>!v)}>{details?'收起详情':'详情'}</button>}</div></header>
    {loading && <p role="status" className="rs-muted">正在读取产物…</p>}{error && <p role="alert" className="rs-error">{error}</p>}
    {!loading && value && (compactMode ? <>
      {firstImage ? <div className="rs-files"><figure className="rs-file is-image"><button type="button" className="rs-image" onClick={()=>setZoom(firstImageUrl)} aria-label={`放大 ${firstImage.name}`}><img src={firstImageUrl} alt={firstImage.name} loading="lazy" /></button></figure></div>
        : table ? <div className="rs-table-wrap"><table className="rs-table rs-table-compact"><thead><tr><th scope="col">行</th>{columns.map(key=><th scope="col" key={key}>{key}</th>)}</tr></thead><tbody>{rows!.slice(0,6).map((row,index)=><tr key={index}><td>{offset+index+1}</td>{columns.map(key=><td key={key} title={text(row[key])}>{text(row[key]).slice(0,300)}</td>)}</tr>)}</tbody></table></div>
        : metrics.length ? <div className="rs-metrics rs-metrics-compact">{metrics.slice(0,4).map(([key,v])=><div key={key}><small>{key}</small><strong>{typeof v==='number'?Number(v.toPrecision(6)).toLocaleString():text(v)}</strong></div>)}</div>
        : typeof data === 'string' ? <pre className="rs-content rs-content-compact">{data.slice(0,1600)}</pre>
        : <p className="rs-muted">这是一份结构化产物。展开「详情」查看完整字段与下载。</p>}
    </> : <>
      {!!value.files?.length && <div className="rs-files">{value.files.map(file => { const url=withWs(file.url || `/api/workflows/files/download?name=${encodeURIComponent(file.file)}`);const isImage=isImageFile(file); return <figure key={file.file} className={`rs-file ${isImage?'is-image':'is-download'}`}>{isImage && <button type="button" className="rs-image" onClick={()=>setZoom(url)} aria-label={`放大 ${file.name}`}><img src={url} alt={file.name} loading="lazy" /></button>}{/\.pdf$/i.test(file.file) && <iframe title={file.name} src={url+'&preview=true'} className="rs-pdf" loading="lazy"/>}<figcaption><span>{file.name}<small>{file.bytes ? `${(file.bytes/1024).toFixed(1)} KB` : '研究文件'}</small></span><div className="rs-actions">{/\.(csv|tsv|xlsx|xlsm|parquet|jsonl|ndjson|json|txt|md|py|log)$/i.test(file.file)&&<button className="button small" onClick={()=>setFilePreview(p=>p===file.file?'':file.file)}>预览内容</button>}<a href={url} className="button small">下载文件 ↗</a></div></figcaption></figure>})}</div>}
      {filePreview&&<ArtifactPreview key={filePreview} fileName={filePreview} title={filePreview.split('/').pop()} provenance={provenance}/>}
      {!!metrics.length && <div className="rs-metrics">{metrics.slice(0,12).map(([key,v])=><div key={key}><small>{key}</small><strong>{typeof v==='number'?Number(v.toPrecision(6)).toLocaleString():text(v)}</strong></div>)}</div>}
      {table ? <><div className="rs-table-filter"><input aria-label="筛选当前页数据" placeholder="筛选当前页数据…" value={rowQuery} onChange={e=>setRowQuery(e.target.value)}/><small>仅筛选当前预览页 · {visibleRows.length}/{rows!.length} 行</small></div><div className="rs-table-wrap"><table className="rs-table"><thead><tr><th scope="col">行</th>{columns.map(key=><th scope="col" key={key}>{key}</th>)}</tr></thead><tbody>{visibleRows.map(({row,index})=><tr key={index}><td>{index}</td>{columns.map(key=><td key={key} title={text(row[key])}>{text(row[key]).slice(0,300)}</td>)}</tr>)}</tbody></table></div><footer className="rs-pagination"><span>{value.total!=null ? `共 ${value.total.toLocaleString()} 行 · 当前 ${rows!.length?offset+1:0}–${offset+rows!.length}` : `当前 ${offset+1}–${offset+rows!.length} 行 · 总行数未扫描`}{columns.length===40?' · 最多显示40列，下载查看全部':''}</span><div className="rs-actions"><button className="button small" disabled={offset===0} onClick={()=>setOffset(Math.max(0,offset-20))}>上一页</button><button className="button small" disabled={value.next_offset==null} onClick={()=>setOffset(value.next_offset!)}>下一页</button></div></footer></> : typeof data === 'string' ? <pre className="rs-content">{data}</pre> : <details open={!value.files?.length && !metrics.length}><summary>完整内容与字段</summary><pre className="rs-content">{JSON.stringify(data,null,2)}</pre></details>}
      {!table&&rows&&<footer className="rs-pagination"><span>当前 {offset+1}–{offset+rows.length} / {value.total??'未知'}</span><div className="rs-actions"><button className="button small" disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-20))}>上一页</button><button className="button small" disabled={value.next_offset==null} onClick={()=>setOffset(value.next_offset!)}>下一页</button></div></footer>}
    </>)}
    {zoom && <div className="rs-lightbox" role="dialog" aria-modal="true" aria-label="图表大图" onClick={()=>setZoom('')}><button autoFocus className="button" onClick={()=>setZoom('')}>关闭 ×</button><img src={zoom} alt="研究图表大图" onClick={e=>e.stopPropagation()} /></div>}
  </section>
}
