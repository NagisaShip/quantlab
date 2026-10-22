import { useEffect, useMemo, useState } from 'react'
import type { LabMessage } from '../../apiLab'
import WorkflowTrace, { type WorkflowView } from './WorkflowTrace'

export default function SessionWorkflows({messages,requested}:{messages:LabMessage[];requested:string}) {
  const views=useMemo(()=>{
    const map=new Map<string,WorkflowView>()
    for(const m of messages){const v=m.data?.workflow_view as WorkflowView|undefined;if(v?.format==='quantlab.workflow-view/1')map.set(v.workflow_id,v)}
    return Array.from(map.values()).reverse()
  },[messages])
  const [chosen,setChosen]=useState(requested)
  useEffect(()=>{if(requested)setChosen(requested)},[requested])
  const value=views.find(v=>v.workflow_id===chosen)||views[0]
  return <div aria-label="会话研究工作流"><p style={{fontSize:12,color:'var(--muted)'}}>来自本会话的保存与运行记录。草稿、已保存修订和真实证据分别标记。</p>{views.length>1&&<select aria-label="会话中的工作流" value={value?.workflow_id||''} onChange={e=>setChosen(e.target.value)}>{views.map(v=><option key={v.workflow_id} value={v.workflow_id}>{v.title} · r{v.revision}</option>)}</select>}{value?<WorkflowTrace value={value} compact/>:<p>本会话尚未创建或检查工作流。Agent调用创建、校验、运行或查看工具后，会在这里显示真实编排。</p>}</div>
}
