import { useState } from 'react'
import { get, put } from '../../http'
import { appConfirm } from '../../components/dialog'
import { useResearchPlugins, type ResearchPlugin } from '../../features/workflows/plugins'
import '../../features/workflows/researchStudio.css'
export default function Plugins({ admin, notify }: {admin:boolean;notify:(text:string,error?:boolean)=>void}) {
  const plugins=useResearchPlugins()
  const available=(p:ResearchPlugin):boolean=>p.enabled&&p.agent&&p.dependencies.every(id=>{const dependency=plugins.find(x=>x.id===id);return !!dependency&&available(dependency)})
  const [query,setQuery]=useState('')
  const [busy,setBusy]=useState('')
  const [error,setError]=useState('')
  async function change(plugin:ResearchPlugin,key:'enabled'|'agent'|'visible'){
    setBusy(plugin.id);setError('')
    try {
      if(key==='enabled'&&plugin.enabled){const impact=await get<{workflows:{id:string;title:string}[]}>(`/api/auth/research-plugins/${plugin.id}/impact`);if(!await appConfirm(`停用「${plugin.name}」？${impact.workflows.length?`\n影响 ${impact.workflows.length} 个研究：${impact.workflows.map(r=>r.title).join('、')}。已有结果仍可查看，相关节点再次运行前需要重新启用。`:'\n已有产物仍可查看。'}`))return}
      await put(`/api/auth/research-plugins/${plugin.id}`,{[key]:!plugin[key]});window.dispatchEvent(new Event('qa:plugins-changed'));notify('插件设置已更新')
    }catch(e){setError(String(e));notify(String(e),true)}finally{setBusy('')}
  }
  return <div><div className="rs-plugin-summary"><div><strong>{plugins.filter(p=>p.enabled).length}</strong><small>已启用能力</small></div><div><strong>{plugins.filter(p=>p.visible&&p.pages.length).length}</strong><small>显示导航的插件</small></div><div><strong>{plugins.filter(p=>available(p)).length}</strong><small>Agent 可调用</small></div></div><input className="rs-search" aria-label="搜索插件" value={query} onChange={e=>setQuery(e.target.value)} placeholder="搜索插件名称或用途…"/><p className="rs-muted">工作区开关由管理员设置；导航与 Agent 偏好仅影响自己。</p>{error&&<p role="alert" className="rs-error">{error}</p>}<div className="rs-table-wrap rs-plugin-scroll"><table className="rs-table rs-plugin-table"><thead><tr><th>插件与用途</th><th>依赖 / 状态</th><th>工作区启用</th><th>显示导航</th><th>Agent 调用</th></tr></thead><tbody>{plugins.filter(p=>(p.name+p.description).includes(query)).map(p=><tr key={p.id}><td><strong>{p.name}{p.core?' · 核心':''}</strong><small>{p.description}</small></td><td><span>{!p.enabled?'已停用':p.configured?'已就绪':'环境待配置'}</span>{p.configuration_note&&<small><a href="#/settings/env">{p.configuration_note} · 配置环境 ↗</a></small>}<small>{p.dependencies.map(d=>plugins.find(x=>x.id===d)?.name).join('、')||'无前置依赖'}</small></td>{(['enabled','visible','agent'] as const).map(key=><td key={key}>{key==='visible'&&!p.pages.length?<small>工作流内入口</small>:<button role="switch" aria-checked={p[key]} aria-label={`${p.name} ${key==='enabled'?'启用':key==='visible'?'显示导航':'Agent 调用'}`} className="rs-switch" disabled={busy===p.id||(key==='enabled'&&(!admin||p.core))} onClick={()=>change(p,key)}/>}</td>)}</tr>)}</tbody></table></div></div>
}
