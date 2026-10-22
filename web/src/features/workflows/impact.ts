import type { Flow, Report } from './types'
const signature=(node:Flow['nodes'][number])=>JSON.stringify({kind:node.kind,version:node.version,params:node.params,bindings:node.bindings})
export function affectedNodes(flow:Flow, report:Report|null):Set<string> {
  if(!report)return new Set()
  const old=new Map(report.workflow.nodes.map(n=>[n.id,n]))
  const affected=new Set(flow.nodes.filter(n=>!old.has(n.id)||signature(n)!==signature(old.get(n.id)!)).map(n=>n.id))
  const wires=(f:Flow,id:string)=>JSON.stringify(f.edges.filter(e=>e.target===id).map(e=>[e.source,e.source_port,e.target_port]).sort())
  for(const node of flow.nodes)if(wires(flow,node.id)!==wires(report.workflow,node.id))affected.add(node.id)
  if(flow.settings?.seed!==report.workflow.settings?.seed)flow.nodes.forEach(n=>affected.add(n.id))
  let changed=true;while(changed){changed=false;for(const wire of flow.edges)if(affected.has(wire.source)&&!affected.has(wire.target)){affected.add(wire.target);changed=true}}
  return affected
}
