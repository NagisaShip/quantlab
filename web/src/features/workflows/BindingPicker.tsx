import { useEffect, useState } from 'react'
import { get } from '../../http'
import type { ArtifactView } from './ArtifactPreview'
import type { Card, Definition, Flow, Report } from './types'
export default function BindingPicker({ card, spec, flow, report, definitions, onChange }: {card:Card;spec:Definition;flow:Flow;report:Report|null;definitions:Map<string,Definition>;onChange:(name:string,binding?:{input:string;path:string[]})=>void}) {
  const [samples,setSamples]=useState<Record<string,unknown>>({})
  useEffect(()=>{let active=true;setSamples({});for(const edge of flow.edges.filter(e=>e.target===card.id)){const ref=report?.nodes[edge.source]?.outputs[edge.source_port];if(ref)get<ArtifactView>(`/api/workflows/artifacts/${ref.hash}?limit=1`).then(v=>{if(active)setSamples(s=>({...s,[edge.target_port]:v.value}))}).catch(()=>{})}return()=>{active=false}},[card.id,report,flow.edges])
  const options=flow.edges.filter(e=>e.target===card.id).flatMap(edge=>{
    const source=flow.nodes.find(n=>n.id===edge.source)
    const def=source&&definitions.get(`${source.kind}@${source.version}`)
    const port=def?.outputs[edge.source_port]
    const sample=samples[edge.target_port]
    const props=(port?.schema?.properties||{}) as Record<string,{type?:string}>
    const fields=[...new Set([...Object.keys(props),...(sample&&typeof sample==='object'&&!Array.isArray(sample)?Object.keys(sample):[])])]
    return [{input:edge.target_port,path:[] as string[],label:`${source?.title||def?.title||edge.source} → ${edge.target_port}（整个输入）`,value:sample,type:port?.type||'未知'},...fields.map(key=>({input:edge.target_port,path:[key],label:`${source?.title||def?.title||edge.source} → ${edge.target_port}.${key}`,value:(sample as Record<string,unknown>|undefined)?.[key],type:props[key]?.type||typeof (sample as Record<string,unknown>|undefined)?.[key]}))]
  })
  return <details><summary>从上游数据引用参数</summary><p className="rs-muted">选择已连接的输入与字段。引用后，参数将随上游数据解析。</p>{Object.entries(spec.parameters.properties||{}).map(([name,field])=>{const binding=card.bindings?.[name];const encoded=binding?JSON.stringify(binding):'';const available=options.filter(o=>!field.type||o.type==='未知'||o.type==='undefined'||o.type===field.type||(field.type==='integer'&&o.type==='number')||(field.type==='object'&&o.type==='report'));const chosen=options.find(o=>JSON.stringify({input:o.input,path:o.path})===encoded);return <label key={name} className="rs-binding">{field.title||name}<select aria-label={`引用参数 ${name}`} value={encoded} onChange={e=>onChange(name,e.target.value?JSON.parse(e.target.value):undefined)}><option value="">手动填写 / 默认值</option>{binding&&!chosen&&<option value={encoded}>{binding.input}.{binding.path.join('.')}（保留现有引用）</option>}{available.map(o=><option key={JSON.stringify(o)} value={JSON.stringify({input:o.input,path:o.path})}>{o.label} · {o.type}</option>)}</select>{chosen&&<small>类型：{chosen.type} · 样例：{chosen.value===undefined?'尚未运行':JSON.stringify(chosen.value)?.slice(0,160)}</small>}<details><summary>高级字段路径</summary><input aria-label={`高级引用路径 ${name}`} placeholder="输入端口.字段.子字段" value={binding?[binding.input,...binding.path].join('.'):''} onChange={e=>{const [input,...path]=e.target.value.split('.');onChange(name,input?{input,path}:undefined)}}/></details></label>})}</details>
}
