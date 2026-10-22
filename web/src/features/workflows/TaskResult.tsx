import { useEffect, useState } from 'react'
import { currentWorkspace, get, post, withWs } from '../../http'
import './researchStudio.css'
export type TaskView={id:string;workspace_id:string;title?:string;state:string;summary?:{download?:string;filename?:string};progress?:{done:number;total:number};error_message?:string}
const states:Record<string,string>={QUEUED:'排队中',PENDING:'等待执行',RUNNING:'正在执行',COMPLETED:'已完成',FAILED:'失败',CANCELED:'已取消'}
export default function TaskResult({value}:{value:TaskView}){
  const [run,setRun]=useState(value)
  const [error,setError]=useState('')
  const [busy,setBusy]=useState(false)
  const workspace=currentWorkspace()
  const allowed=workspace===value.workspace_id
  useEffect(()=>{if(!allowed)return;let active=true;let timer:ReturnType<typeof setTimeout>;const read=async()=>{try{const v=await get<{run:TaskView}>(`/api/runs/${value.id}`);if(active){setRun({...v.run,workspace_id:value.workspace_id});if(!['COMPLETED','FAILED','CANCELED'].includes(v.run.state))timer=setTimeout(read,1800)}}catch(e){if(active)setError(String(e))}};void read();return()=>{active=false;clearTimeout(timer)}},[value.id,allowed])
  if(!allowed)return <p className="rs-muted">请切回任务所属工作区查看。</p>
  const active=!['COMPLETED','FAILED','CANCELED'].includes(run.state)
  return <section className="rs-artifact" aria-label="研究任务与文件"><header className="rs-artifact-head"><div><strong>{run.title||'研究任务'}</strong><small>{states[run.state]||run.state} · {run.id}</small></div><div className="rs-actions">{active&&<button className="button small" disabled={busy} onClick={async()=>{setBusy(true);try{await post(`/api/runs/${run.id}/commands`,{command:'cancel'});const v=await get<{run:TaskView}>(`/api/runs/${run.id}`);setRun({...v.run,workspace_id:value.workspace_id})}catch(e){setError(String(e))}finally{setBusy(false)}}}>取消任务</button>}{run.summary?.download&&<a className="button small primary" href={withWs(run.summary.download)}>下载 {run.summary.filename||'研究文件'} ↗</a>}</div></header>{active&&!!run.progress?.total&&<progress style={{width:'calc(100% - 32px)',margin:16,accentColor:'var(--accent)'}} max={run.progress.total} value={run.progress.done} aria-label="任务进度"/>}{(error||run.error_message)&&<p role="alert" className="rs-error">{error||run.error_message}</p>}</section>
}
