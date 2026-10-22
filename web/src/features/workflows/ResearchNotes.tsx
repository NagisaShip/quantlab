import { useEffect, useRef, useState } from 'react'
import { get, put } from '../../http'
import { Markdown } from '../../components/markdown'
import type { ArtifactRow } from './ArtifactCenter'
import { downloadText } from './ChartEditor'
type Notes={version:number;text:string;run_ids:string[]}
export default function ResearchNotes({workflowId,runId,citation}:{workflowId:string;runId?:string;citation?:ArtifactRow&{at:number}}){
  const [saved,setSaved]=useState<Notes>({version:0,text:'',run_ids:[]})
  const [text,setText]=useState('')
  const [refs,setRefs]=useState<string[]>([])
  const [edit,setEdit]=useState(true)
  const [error,setError]=useState('')
  const [busy,setBusy]=useState(false)
  const [loaded,setLoaded]=useState(false)
  const lastCitation=useRef(0)
  useEffect(()=>{let active=true;setLoaded(false);get<Notes>(`/api/workflows/${workflowId}/notes`).then(v=>{if(active){setSaved(v);setText(v.text);setRefs(v.run_ids);setLoaded(true)}}).catch(e=>{if(active)setError(String(e))});return()=>{active=false}},[workflowId])
  useEffect(()=>{if(!citation||citation.workflow_id!==workflowId||!loaded||lastCitation.current===citation.at)return;lastCitation.current=citation.at;if(!refs.includes(citation.run_id)&&refs.length>=30){setError('最多引用30次运行，请先移除部分引用');return}setText(t=>t+`\n\n[${citation.title}](#/workflows/${workflowId}?run_id=${citation.run_id}&node=${citation.node_id}) · r${citation.revision}\n\n工件：${citation.hash}\n`);setRefs(r=>r.includes(citation.run_id)?r:[...r,citation.run_id]);setEdit(true)},[citation?.at,loaded,workflowId])
  const dirty=text!==saved.text||JSON.stringify(refs)!==JSON.stringify(saved.run_ids)
  useEffect(()=>{const warn=(event:BeforeUnloadEvent)=>{if(dirty){event.preventDefault();event.returnValue=''}};window.addEventListener('beforeunload',warn);return()=>window.removeEventListener('beforeunload',warn)},[dirty])
  return <section className="wf-section" aria-label="研究笔记"><div className="rs-modebar"><div><strong>研究笔记与结论</strong><small className="rs-muted"> · v{saved.version}{dirty?' · 未保存':''}</small></div><div className="rs-actions"><button className="button small" onClick={()=>setEdit(!edit)}>{edit?'预览':'编辑'}</button><button className="button small" disabled={!text} onClick={()=>downloadText(text+'\n\n证据运行：\n'+refs.join('\n'),'research-notes.md')}>导出笔记</button><button className="button small primary" disabled={busy||!loaded||!dirty} onClick={async()=>{setBusy(true);setError('');try{const v=await put<Notes>(`/api/workflows/${workflowId}/notes`,{text,run_ids:refs,expected_version:saved.version});setSaved(v)}catch(e){setError(String(e))}finally{setBusy(false)}}}>保存笔记</button></div></div>{error&&<p role="alert" className="rs-error">{error}</p>}{edit?<textarea aria-label="研究笔记内容" rows={7} style={{width:'100%',resize:'vertical'}} value={text} onChange={e=>setText(e.target.value)} placeholder="记录研究问题、主要发现、证据与局限。支持 Markdown。"/>:<Markdown text={text||'尚未记录研究结论。'}/>}
    <div className="rs-actions" style={{marginTop:10}}>{runId&&!refs.includes(runId)&&<button className="button small" disabled={refs.length>=30} onClick={()=>setRefs(r=>[...r,runId])}>＋ 引用最近查看的运行</button>}{refs.map(id=><span key={id} className="rs-muted"><a href={`#/workflows/${workflowId}?run_id=${id}`}>{id}</a> <button className="button small ghost" aria-label={`移除引用 ${id}`} onClick={()=>setRefs(r=>r.filter(x=>x!==id))}>×</button></span>)}</div>
  </section>
}
