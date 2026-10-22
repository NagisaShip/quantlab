import { useState } from 'react'
import { ReactFlow, ReactFlowProvider, Background, Controls, Handle, Position, type Node, type NodeProps, type Connection } from '@xyflow/react'
import { CodeView } from '../../components/CodeEditor'
import { post } from '../../http'
import WorkbenchDialog from './WorkbenchDialog'

type Component = { id: string; type: string; inputs?: string[]; shape?: number[]; out_features?: number; hidden_size?: number; return_sequences?: boolean; bias?: boolean; p?: number; position?: { x: number; y: number } }
export type NetworkGraph = { nodes: Component[]; output: string }
type Shape = { id: string; shape: number[]; parameters: number }
type Inspection = { components: Shape[]; parameter_count: number; output_shape: number[]; code: string }
type LayerNode = Node<{ component: Component; shape?: Shape; output: boolean }, 'layer'>
function Layer({ data, selected }: NodeProps<LayerNode>) {
  return <div className={`wf-layer ${selected ? 'selected' : ''}`}><Handle type="target" position={Position.Left} isConnectable={data.component.type !== 'Input'} /><strong>{data.component.type}</strong><code>{data.component.id}{data.output ? ' · 输出' : ''}</code><small>{data.shape ? `[batch, ${data.shape.shape.join(', ')}] · ${data.shape.parameters.toLocaleString()} 参数` : '等待检查'}</small><Handle type="source" position={Position.Right} /></div>
}
const types = { layer: Layer }
const kinds = ['Input', 'Linear', 'ReLU', 'GELU', 'Sigmoid', 'Tanh', 'Dropout', 'LayerNorm', 'Flatten', 'Add', 'Concat', 'GRU', 'Identity']
export default function NetworkEditor({ value, layout, onApply, onClose }: { value: NetworkGraph; layout?: Record<string,{x:number;y:number}>; onApply: (graph: NetworkGraph,layout:Record<string,{x:number;y:number}>) => void; onClose: () => void }) {
  const [graph, setGraph] = useState<NetworkGraph>(() => ({...structuredClone(value),nodes:value.nodes.map(n=>({...structuredClone(n),position:layout?.[n.id] || n.position}))}))
  const [selected, setSelected] = useState(graph.nodes[0]?.id || '')
  const [selectedEdge,setSelectedEdge]=useState('')
  const [inspection, setInspection] = useState<Inspection | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const layer = graph.nodes.find(n => n.id === selected)
  const mutate = (next: NetworkGraph) => { setGraph(next); setInspection(null); setError('') }
  const set = (values: Partial<Component>) => mutate({ ...graph, nodes: graph.nodes.map(n => n.id === selected ? { ...n, ...values } : n) })
  const check = async () => { setBusy(true); setError(''); try { const result = await post<Inspection>('/api/workflows/network/inspect', { graph }); setInspection(result); return true } catch (e) { setError(String(e)); return false } finally { setBusy(false) } }

  function add(kind: string) {
    const id = `layer_${crypto.randomUUID().slice(0, 8).replaceAll('-', '_')}`
    const layer: Component = { id, type: kind, inputs: [], position: { x: graph.nodes.length * 40 + 30, y: graph.nodes.length * 35 + 80 } }
    if (kind === 'Input') layer.shape = [8]
    if (kind === 'Linear') layer.out_features = 32
    if (kind === 'GRU') layer.hidden_size = 32
    if (kind === 'Dropout') layer.p = .1
    mutate({ ...graph, nodes: [...graph.nodes, layer] }); setSelected(id)
  }
  const connect = (connection: Connection) => {
    const node = graph.nodes.find(n => n.id === connection.target)
    if (!node || node.type === 'Input' || connection.source === connection.target) return
    const inputs = node.type === 'Add' || node.type === 'Concat' ? [...new Set([...(node.inputs || []), connection.source])] : [connection.source]
    mutate({ ...graph, nodes: graph.nodes.map(n => n.id === node.id ? { ...n, inputs } : n) })
  }
  return <WorkbenchDialog title="神经网络组件编辑器" onClose={onClose}>
    <div className="wf-toolbar"><h2>神经网络组件图</h2><button className="button" onClick={onClose}>返回卡片</button><button className="button" disabled={busy} onClick={check}>检查形状与参数量</button><button className="button primary" disabled={busy} onClick={async () => { if (await check()) { onApply({...graph,nodes:graph.nodes.map(n=>{const copy={...n};delete copy.position;return copy})},Object.fromEntries(graph.nodes.map((n,i)=>[n.id,n.position || {x:i*250,y:120}]))); onClose() } }}>检查并应用</button>{inspection && <span>{inspection.parameter_count.toLocaleString()} 个参数 · 输出 [batch, {inspection.output_shape.join(', ')}]</span>}</div>
    <p>拖动组件与连线修改真实网络。输入形状不含 batch；Add 与 Concat 可接多个上游，顺序见右侧。组件结构与训练参数分别保存。</p>
    {error && <p className="wf-error" role="alert">{error}</p>}
    <div className="wf-network-layout"><aside className="wf-layer-library">{kinds.map(kind => <button className="button" key={kind} onClick={() => add(kind)}>{kind}</button>)}</aside><div className="wf-network-canvas"><ReactFlowProvider><ReactFlow<LayerNode> nodes={graph.nodes.map((n,i) => ({ id: n.id, position: n.position || { x: i * 250, y: 120 }, type: 'layer', selected: n.id === selected, data: { component: n, shape: inspection?.components.find(s => s.id === n.id), output: n.id === graph.output } }))} edges={graph.nodes.flatMap(n => (n.inputs || []).map((p,i) => ({ id: `${p}-${n.id}-${i}`, source: p, target: n.id, label: String(i + 1), type: 'smoothstep',selected:selectedEdge===`${p}-${n.id}-${i}` })))} nodeTypes={types} onEdgeClick={(_,edge)=>{setSelectedEdge(edge.id);setSelected('')}} onPaneClick={()=>{setSelected('');setSelectedEdge('')}} onConnect={connect} onNodesChange={changes=>{const removed=new Set(changes.filter(c=>c.type==='remove').map(c=>c.id));setGraph(g=>({...g,nodes:g.nodes.filter(n=>!removed.has(n.id)).map(n=>{const move=changes.find(c=>c.type==='position'&&c.id===n.id);return {...n,position:move?.type==='position'&&move.position?move.position:n.position,inputs:n.inputs?.filter(p=>!removed.has(p))}})}));if(removed.size)setInspection(null)}} onNodeClick={(_,n) => {setSelected(n.id);setSelectedEdge('')}} onNodeDragStop={(_,n) => setGraph(g => ({ ...g, nodes: g.nodes.map(c => c.id === n.id ? { ...c, position: n.position } : c) }))} onEdgesDelete={edges => mutate({ ...graph, nodes: graph.nodes.map(n => ({ ...n, inputs: n.inputs?.filter(p => !edges.some(e => e.source === p && e.target === n.id)) })) })} fitView minZoom={.15} maxZoom={2} panOnDrag={[0,1,2]}><Background /><Controls /></ReactFlow></ReactFlowProvider></div><aside className="wf-network-settings">
      {layer && <><h2>{layer.type}</h2><code>{layer.id}</code>{layer.type === 'Input' && <label>输入形状<input aria-label="输入形状" value={(layer.shape || []).join(',')} onChange={e => set({ shape: e.target.value.split(',').map(Number) })} /></label>}{layer.type === 'Linear' && <><label>输出维度<input type="number" min={1} value={layer.out_features || 1} onChange={e => set({ out_features: Number(e.target.value) })} /></label><label><input type="checkbox" checked={layer.bias !== false} onChange={e => set({ bias: e.target.checked })} />使用偏置</label></>}{layer.type === 'GRU' && <><label>隐藏维度<input type="number" value={layer.hidden_size || 32} onChange={e => set({ hidden_size: Number(e.target.value) })} /></label><label><input type="checkbox" checked={!!layer.return_sequences} onChange={e => set({ return_sequences: e.target.checked })} />返回序列</label></>}{layer.type === 'Dropout' && <label>丢弃概率<input type="number" min={0} max={.99} step={.05} value={layer.p ?? .1} onChange={e => set({ p: Number(e.target.value) })} /></label>}
      <label>按顺序接收的输入{(layer.inputs || []).map((p,i) => <button key={p} className="button" onClick={() => set({ inputs: layer.inputs?.filter(k => k !== p) })}>{i + 1}. {p} ×</button>)}</label><button className="button" onClick={() => mutate({ ...graph, output: layer.id })}>设为网络输出</button><button className="button" onClick={() => { mutate({ ...graph, nodes: graph.nodes.filter(n => n.id !== layer.id).map(n => ({ ...n, inputs: n.inputs?.filter(p => p !== layer.id) })) }); setSelected('') }}>删除组件</button></>}
      {inspection && <details><summary>生成的执行代码</summary><CodeView code={inspection.code} maxHeight={420} /></details>}
    </aside></div>
  </WorkbenchDialog>
}
