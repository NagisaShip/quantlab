import type { Definition } from './types'
import { useEffect, useState } from 'react'
import { currentWorkspace, get } from '../../http'
export type ResearchPlugin={id:string;name:string;description:string;core?:boolean;pages:string[];dependencies:string[];enabled:boolean;agent:boolean;visible:boolean;configured:boolean;configuration_note?:string}
export type ResearchPolicyStatus='loading'|'ready'|'error'
type PolicySnapshot={workspace:string;plugins:ResearchPlugin[];status:ResearchPolicyStatus}
/**
 * 研究能力策略按工作区隔离：切工作区时不能沿用上一个工作区的 enabled 快照。
 * 读取中/失败都保持只读，由页面显示紧凑状态与重试；历史读取仍可继续。
 */
export function useResearchPluginState(){
  const workspace=currentWorkspace()
  const [attempt,setAttempt]=useState(0)
  const [snapshot,setSnapshot]=useState<PolicySnapshot>({workspace,plugins:[],status:'loading'})
  useEffect(()=>{
    let active=true
    setSnapshot({workspace,plugins:[],status:'loading'})
    const load=()=>{
      if(active)setSnapshot((prev)=>prev.workspace===workspace?{...prev,status:'loading'}:prev)
      get<{plugins:ResearchPlugin[]}>('/api/auth/research-plugins')
        .then(v=>{if(active)setSnapshot({workspace,plugins:v.plugins,status:'ready'})})
        .catch(()=>{if(active)setSnapshot({workspace,plugins:[],status:'error'})})
    }
    void load()
    window.addEventListener('qa:plugins-changed',load)
    return()=>{active=false;window.removeEventListener('qa:plugins-changed',load)}
  },[workspace,attempt])
  const scoped:PolicySnapshot=snapshot.workspace===workspace?snapshot:{workspace,plugins:[],status:'loading'}
  return {plugins:scoped.plugins,status:scoped.status,retry:()=>setAttempt((n)=>n+1)}
}
export function useResearchPlugins(){ return useResearchPluginState().plugins }
export function modulePlugin(kind:string,definition?:Definition){
  const category=definition?.category||''
  for(const [label,id] of [['因子','factor'],['组合','portfolio'],['执行','execution'],['模型','model'],['神经','model']])if(category.includes(label))return id
  if(Object.values(definition?.outputs||{}).some(p=>['model','network'].includes(p.type)))return 'model'
  if(kind.includes('chart'))return 'charts'
  if(kind.startsWith('source.')||kind.startsWith('table.'))return 'data'
  for(const id of ['factor','model','portfolio','execution'])if(['research.','asset.','native.','module.','module.native_','research.native_'].some(prefix=>kind.startsWith(prefix+id)))return id
  return 'workflow'
}
