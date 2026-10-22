import { useEffect, useRef, useState } from 'react'
import ArtifactPreview from './ArtifactPreview'
import { statusName, type Report, type Flow, type Definition } from './types'

const clamp = (h:number) => Math.max(160, Math.min(700, Math.round(h)))

export default function ResultDock({ report, flow, selected, stale, definitions, onChart, onSelect, onClose, height, expanded, onHeight, onToggleExpand }: {
  report:Report|null
  flow:Flow
  selected:string
  stale:Set<string>
  definitions:Map<string,Definition>
  onChart?:(node:string,port:string,hash:string)=>void
  onSelect:(id:string)=>void
  onClose:()=>void
  height:number
  expanded:boolean
  onHeight:(height:number)=>void
  onToggleExpand:()=>void
}) {
  const [panel,setPanel]=useState<'output'|'input'|'logs'>('output')
  const [port,setPort]=useState('')
  const drag=useRef<{y:number;height:number}|null>(null)
  const closeRef=useRef<HTMLButtonElement>(null)
  useEffect(()=>{ closeRef.current?.focus({preventScroll:true}) },[])
  const nodeId=report?.nodes[selected]?selected:report?.workflow.nodes.filter(n=>report.nodes[n.id]).at(-1)?.id||''
  const node=report?.nodes[nodeId]
  const label=(id:string)=>{const n=report?.workflow.nodes.find(n=>n.id===id)||flow.nodes.find(n=>n.id===id);return n?.title||(n&&definitions.get(`${n.kind}@${n.version}`)?.title)||id}
  const portLabel:Record<string,string>={data:'数据',report:'报告',chart:'图表',evidence:'研究证据',model:'模型',predictions:'预测',transformer:'变换器'}
  const refs=panel==='input'?node?.inputs:node?.outputs
  const key=refs?.[port]?port:refs?.data?'data':Object.keys(refs||{})[0]||''
  const title=report?.workflow?.title||flow.title
  return <section id="wf-result-dock" className={`rs-dock${expanded?' is-expanded':''}`} aria-label="最近运行结果">
    <div role="separator" aria-label="调整结果面板高度" aria-orientation="horizontal" aria-valuemin={160} aria-valuemax={700} aria-valuenow={height} tabIndex={0} onKeyDown={e=>{if(e.key==='ArrowUp'||e.key==='ArrowDown'){e.preventDefault();onHeight(clamp(height+(e.key==='ArrowUp'?30:-30)))}}} onPointerDown={e=>{drag.current={y:e.clientY,height};e.currentTarget.setPointerCapture(e.pointerId)}} onPointerMove={e=>{if(drag.current)onHeight(clamp(drag.current.height+drag.current.y-e.clientY))}} onPointerUp={()=>{drag.current=null}} onLostPointerCapture={()=>{drag.current=null}}/>
    <header className="rs-dock-head">
      <div className="rs-dock-title"><strong>运行结果</strong><small title={title}>{title}{report?` · ${report.mode==='debug'?'调试运行':'正式研究'}`:''}</small></div>
      <div className="rs-tabs">{(['output','input','logs'] as const).map(p=><button key={p} className={panel===p?'on':''} onClick={()=>{setPanel(p);setPort('')}}>{p==='output'?'输出':p==='input'?'输入':'日志'}</button>)}</div>
      <div className="rs-dock-actions">
        <button type="button" className="button small ghost rs-dock-expand" aria-pressed={expanded} title={expanded?'返回画布':'专注阅读结果'} onClick={onToggleExpand}>{expanded?'返回画布':'专注结果'}</button>
        <button ref={closeRef} type="button" className="button small ghost rs-dock-close" onClick={onClose} aria-label="收起运行结果">收起 ↓</button>
      </div>
    </header>
    <div className={`rs-dock-body${expanded?'':' compact'}`} style={expanded?undefined:{height}}>{node&&report?<><div className={`rs-dock-summary${expanded?'':' compact'}`}><select aria-label="查看节点结果" value={nodeId} onChange={e=>{onSelect(e.target.value);setPort('')}}>{Object.keys(report.nodes).map(id=><option key={id} value={id}>{label(id)}</option>)}</select><span>{statusName[node.status]||node.status}</span>{expanded&&<span>{report.mode==='debug'?'调试运行':'正式研究'} · {report.id}</span>}{stale.has(nodeId)&&<span className="rs-error">当前配置已改变，以下为历史结果</span>}{panel==='output'&&refs?.[key]?.type==='table'&&onChart&&<button className="button small" onClick={()=>onChart(nodeId,key,refs[key].hash)}>由此生成图表 ↗</button>}</div>{node.error&&<p className="rs-error">{node.error}</p>}{panel==='logs'?<pre className="rs-content">{[...(node.logs||[]),node.stdout,node.traceback].filter(Boolean).join('\n')||'本节点没有日志。'}</pre>:<>{Object.keys(refs||{}).length>1&&<div className="rs-modebar"><select aria-label="选择产物端口" value={key} onChange={e=>setPort(e.target.value)}>{Object.keys(refs!).map(k=><option key={k}>{k}</option>)}</select></div>}{refs?.[key]?<ArtifactPreview key={`${report.id}-${nodeId}-${key}-${panel}`} hash={refs[key].hash} title={`${label(nodeId)} · ${portLabel[key]||key}`} provenance={`${report.mode==='debug'?'调试证据':'研究证据'} · ${report.id}`} compact={!expanded}/>:<div className="rs-empty">本节点尚无{panel==='input'?'输入':'输出'}产物。</div>}</>}</>:<div className="rs-empty"><strong>结果将在这里呈现</strong>运行研究后，可查看节点输入、输出、图表与日志。</div>}</div>
  </section>
}
