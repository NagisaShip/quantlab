import { Handle, Position, type Node, type NodeProps } from '@xyflow/react'
import type { Definition, NodeRun, Port } from './types'
import { statusName } from './types'

export type CardData = {
  definition: Definition; params: Record<string,unknown>; title: string; flipped: boolean; status?: NodeRun; pinned?: boolean; stale?: boolean
  ghostInputs: string[]; ghostOutputs: string[]; inspect: () => void; flip: () => void
}
export type FunctionNode = Node<CardData, 'function'>

function summary(value: unknown): string {
  if (value === undefined) return '待填写'
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const object = value as Record<string,unknown>
    return Array.isArray(object.nodes) ? `${object.nodes.length} 个组件` : `{${Object.keys(object).slice(0,4).join(', ')}}`
  }
  const text = JSON.stringify(value)
  return text.length > 80 ? text.slice(0,77) + '…' : text
}

export default function FunctionCard({ data, selected }: NodeProps<FunctionNode>) {
  const d = data.definition
  const ins: [string, Port | null][] = [...Object.entries(d.inputs), ...data.ghostInputs.map(n => [n, null] as [string, null])]
  const outs: [string, Port | null][] = [...Object.entries(d.outputs), ...data.ghostOutputs.map(n => [n, null] as [string, null])]
  const height = Math.max(150, 90 + Math.max(ins.length, outs.length) * 28)
  return <div className={`wf-card ${selected ? 'selected' : ''} ${data.flipped ? 'flipped' : ''} ${data.stale?'stale':''} state-${data.status?.status||'not-run'}`} style={{ minHeight: height }}>
    <div className="wf-card-header"><b>{data.title}</b><span>{d.level === 'module' ? '模块 · ' : ''}{d.version}</span></div>
    <div className="wf-card-status" aria-live="polite">{data.stale ? '配置已改变 · 结果待更新' : data.pinned ? '调试固定输出' : data.status ? statusName[data.status.status] || data.status.status : '尚未运行'}</div>
    {data.flipped ? <div className="wf-card-back nodrag"><code>{d.kind}</code><p>{d.description}</p>{Object.entries(d.parameters.properties || {}).slice(0,5).map(([key,field])=><small key={key} style={{display:'block'}}>{field.title || key}: {summary(data.params[key] ?? field.default)}</small>)}<small>{d.requirements.length ? d.requirements.join(', ') : '仅Python标准库'}</small></div>
      : <div className="wf-card-ports" style={{ minHeight: Math.max(ins.length, outs.length) * 28 }}>
        <div>{ins.map(([name, p]) => <span key={name} className={!p ? 'invalid' : ''}>{name}<small>{p ? p.role || p.type : '旧端口已失效'}</small></span>)}</div>
        <div>{outs.map(([name, p]) => <span key={name} className={!p ? 'invalid' : ''}>{name}<small>{p ? p.role || p.type : '旧端口已失效'}</small></span>)}</div>
      </div>}
    {ins.map(([name, p], i) => <Handle key={`i-${name}`} type="target" id={name} position={Position.Left} style={{ top: 84 + i * 28, background: p ? 'var(--accent)' : 'var(--danger)' }} />)}
    {outs.map(([name, p], i) => <Handle key={`o-${name}`} type="source" id={name} position={Position.Right} style={{ top: 84 + i * 28, background: p ? 'var(--accent)' : 'var(--danger)' }} />)}
    <div className="rs-node-summary">{Object.entries(d.parameters.properties||{}).slice(0,2).map(([key,f])=><span key={key}>{f.title||key} <b>{summary(data.params[key]??f.default)}</b></span>)}{data.status&&<small>{Object.keys(data.status.outputs||{}).length} 项输出 · {Object.values(data.status.outputs||{}).reduce((sum,ref)=>sum+ref.bytes,0).toLocaleString()} 字节</small>}</div>
    <div className="wf-card-actions nodrag"><button type="button" className="button ghost" onClick={data.flip}>{data.flipped?'返回端口':'查看接口'}</button><button type="button" className="button ghost" onClick={data.inspect}>配置</button></div>
  </div>
}
