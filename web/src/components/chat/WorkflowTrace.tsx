import { useEffect, useId, useMemo, useState } from 'react'
import { currentWorkspace, get, post, put } from '../../http'
import './workflowTrace.css'
import ArtifactPreview from '../../features/workflows/ArtifactPreview'
import ArtifactCenter from '../../features/workflows/ArtifactCenter'

type Output = { hash: string; type: string; bytes?: number }
type TraceNode = { id: string; kind: string; version: string; title: string; status: string; parameters: Record<string, unknown>; inputs: string[]; declared_outputs: string[]; outputs: Record<string, Output>; error?: string; child_run_id?: string; has_subflow?:boolean }
type Wire = { source: string; source_port: string; target: string; target_port: string }
export type WorkflowView = { format: 'quantlab.workflow-view/1'; workspace_id: string; workflow_id: string; revision: number; module_path?:string; title: string; nodes: TraceNode[]; edges: Wire[]; run_id?: string; state: string; report_status?: string; evidence: 'saved' | 'run' | 'draft'; proposal?:{workflow:unknown;workflow_id:string;expected_revision:number;changes:unknown}; validation?: { ok: boolean; checks: { name: string; passed: boolean; message?: string }[] } }
const words: Record<string, string> = { draft: '待确认提案', not_run: '未运行', running: '运行中', completed: '完成', cached: '复用缓存', pinned: '调试固定', failed: '失败', error: '失败', skipped: '未执行', blocked: '已阻断', canceled: '已取消', saved: '已保存', QUEUED: '已排队', PENDING: '等待执行', RUNNING: '运行中', COMPLETED: '已结束', FAILED: '失败', CANCELED: '已取消' }
const json = (value: unknown) => JSON.stringify(value, null, 2)

/** A read-only projection of actual saved/running workflow data, never inferred from prose. */
export default function WorkflowTrace({ value, compact=false }: { value?: WorkflowView; compact?:boolean }) {
  if (!value || value.format !== 'quantlab.workflow-view/1') return null
  return <Trace key={`${value.workspace_id}:${value.workflow_id}:${value.revision}:${value.run_id || ''}`} value={value} compact={compact} />
}

function Trace({ value, compact }: { value: WorkflowView;compact:boolean }) {
  const [current, setCurrent] = useState(value)
  const [selected, setSelected] = useState(value.nodes[0]?.id || '')
  const [expanded, setExpanded] = useState(false)
  const [detail, setDetail] = useState(false)
  const [error, setError] = useState('')
  const [evidence, setEvidence] = useState('')
  const [outputHash,setOutputHash] = useState('')
  const [showResults,setShowResults] = useState(false)
  const [busy, setBusy] = useState(false)
  const [inside,setInside] = useState<WorkflowView|null>(null)
  const workspace = currentWorkspace()
  const marker = useId().replace(/:/g, '')
  const allowed = workspace === value.workspace_id
  const path = `/api/workflows/${encodeURIComponent(value.workflow_id)}/view?revision=${value.revision}${value.run_id ? `&run_id=${encodeURIComponent(value.run_id)}` : ''}`
  useEffect(() => { setCurrent(value) }, [value])
  useEffect(() => {
    if (!allowed || !value.run_id || value.module_path) return
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const read = async () => {
      try {
        const next = await get<WorkflowView>(path)
        if (!live) return
        setCurrent(next); setError('')
        if (!['COMPLETED', 'FAILED', 'CANCELED'].includes(next.state)) timer = setTimeout(read, 2500)
      } catch (e) { if (live) setError(String(e)) }
    }
    void read()
    return () => { live = false; clearTimeout(timer) }
  }, [allowed, path, value.run_id])
  const shown = current.nodes.slice(0, expanded ? 160 : 8)
  const layout = useMemo(() => {
    const depth = new Map<string, number>()
    const ids = new Set(shown.map(n => n.id))
    for (let pass = 0; pass < shown.length; pass++) for (const node of shown) {
      if (depth.has(node.id)) continue
      const parents = current.edges.filter(e => e.target === node.id && ids.has(e.source)).map(e => e.source)
      if (parents.every(id => depth.has(id))) depth.set(node.id, parents.length ? Math.max(...parents.map(id => depth.get(id)!)) + 1 : 0)
    }
    const rows = new Map<number, number>()
    const positions = new Map<string, { x: number; y: number }>()
    for (const node of shown) {
      const col = depth.get(node.id) ?? 0; const row = rows.get(col) || 0
      rows.set(col, row + 1); positions.set(node.id, compact ? {x:12+row*180,y:12+col*90} : { x: 12 + col * 190, y: 12 + row * 90 })
    }
    return { positions, width: Math.max(330, ...Array.from(positions.values(), p => p.x + 174)), height: Math.max(100, ...Array.from(positions.values(), p => p.y + 78)) }
  }, [shown.map(n => n.id).join('|'), current.edges,compact])
  const node = current.nodes.find(n => n.id === selected)
  const read = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError('')
    try { setEvidence(json(await fn())); setDetail(true) } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  const principalOutputs=current.nodes.flatMap(n=>Object.entries(n.outputs).map(([port,ref])=>({node:n,port,ref})))
  const figures=principalOutputs.filter(o=>/chart|figure|plot|image/i.test(o.port+' '+o.node.kind))
  const primary=[...figures,...principalOutputs.filter(o=>!figures.includes(o)).slice(-1)].slice(0,3)
  const href = `#/workflows/${encodeURIComponent(current.workflow_id)}?revision=${current.revision}${current.run_id ? `&run_id=${encodeURIComponent(current.run_id)}` : ''}${node ? `&node=${encodeURIComponent(node.id)}` : ''}`
  if (!allowed) return <p className="cw-muted">工作流属于其他工作区，请切回原工作区查看。</p>
  return <section className="cw-trace" aria-label={`工作流编排：${current.title}`}>
    <header><div><strong>{current.title}</strong><small>修订 {current.revision} · {current.nodes.length} 张卡片 · {current.edges.length} 条连接</small></div><span className={`cw-state ${current.report_status || ''}`}>{words[current.report_status || current.state] || current.state}</span></header>
    <p className="cw-muted">{current.evidence==='draft'?'拟议修改，尚未保存或运行':current.run_id ? `运行 ${current.run_id} · ${current.evidence === 'run' ? '来自真实运行证据' : '尚无节点运行证据'}` : '已保存的编排，尚未运行'} · 编排预览</p>
    {current.proposal&&<div className="cw-details"><details open><summary>保存前的改动差异</summary><pre>{json(current.proposal.changes)}</pre></details><button className="button small" disabled={busy} onClick={async()=>{setBusy(true);setError('');try{const p=current.proposal!;const r=p.workflow_id?await put<{id:string;revision:number}>(`/api/workflows/${p.workflow_id}`,{workflow:p.workflow,expected_revision:p.expected_revision}):await post<{id:string;revision:number}>('/api/workflows',{workflow:p.workflow});setCurrent(await get<WorkflowView>(`/api/workflows/${r.id}/view?revision=${r.revision}`))}catch(e){setError(String(e))}finally{setBusy(false)}}}>保存这份提案</button></div>}
    <div className="cw-scroll" tabIndex={0} aria-label="工作流连接图，可横向滚动"><div className="cw-graph" style={{ width: layout.width, height: layout.height }}>
      <svg width={layout.width} height={layout.height} aria-hidden="true"><defs><marker id={marker} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="currentColor" /></marker></defs>{current.edges.map((edge, i) => {
        const a = layout.positions.get(edge.source); const b = layout.positions.get(edge.target)
        if (!a || !b) return null
        return <path key={i} d={compact ? `M${a.x+80},${a.y+68} C${a.x+80},${a.y+78} ${b.x+80},${b.y-10} ${b.x+80},${b.y}` : `M${a.x + 160},${a.y + 32} C${a.x + 175},${a.y + 32} ${b.x - 15},${b.y + 32} ${b.x},${b.y + 32}`} markerEnd={`url(#${marker})`} />
      })}</svg>
      {shown.map(n => { const p = layout.positions.get(n.id)!; return <button key={n.id} type="button" className={`cw-node ${n.status} ${n.id === selected ? 'selected' : ''}`} style={{ left: p.x, top: p.y }} aria-pressed={n.id === selected} onClick={() => { setSelected(n.id); setOutputHash(''); setEvidence(''); setInside(null); setDetail(true) }}><strong>{n.title}</strong><small>{n.kind} @{n.version}</small><span>{words[n.status] || n.status}</span></button> })}
    </div></div>
    {current.nodes.length > 8 && <button className="cw-action" type="button" onClick={() => setExpanded(!expanded)}>{expanded ? '收起到前8张' : `展开全部${current.nodes.length}张卡片`}</button>}
    <div className="cw-actions">{current.run_id&&!current.module_path&&<button className="cw-action" type="button" onClick={()=>setShowResults(!showResults)} aria-expanded={showResults}>研究产物与运行结果</button>}<button className="cw-action" type="button" onClick={() => setDetail(!detail)} aria-expanded={detail}>参数与证据</button>{!current.proposal&&!current.module_path&&<><button className="cw-action" type="button" disabled={busy} onClick={() => { setBusy(true); get<WorkflowView>(`/api/workflows/${current.workflow_id}/view?revision=${current.revision}${current.run_id?`&run_id=${current.run_id}`:''}`).then(v => { setCurrent(v); setError('') }).catch(e => setError(String(e))).finally(() => setBusy(false)) }}>刷新状态</button><a href={href} target="_blank" rel="noopener noreferrer">在完整画布打开 ↗</a></>}</div>
    {showResults&&current.run_id&&<ArtifactCenter runId={current.run_id} workflowId={current.workflow_id} refresh={current.state}/>}
    {!compact&&!showResults&&current.evidence==='run'&&primary.length>0&&<div className="cw-primary-results">{primary.map(o=><ArtifactPreview key={`${current.run_id}-${o.ref.hash}`} hash={o.ref.hash} title={`${o.node.title} · ${o.port}`} provenance={`r${current.revision} · ${current.run_id}`}/>)}</div>}
    {error && <p role="alert" className="cw-error">{error}</p>}
    {current.validation && <details><summary>自检：{current.validation.ok ? '通过' : '未通过'}</summary>{current.validation.checks.map((c, i) => <p key={i}>{c.passed ? '✓' : '×'} {c.name} {c.message}</p>)}</details>}
    {detail && node && <div className="cw-details"><strong>{node.title} · {node.id}</strong><p className="cw-muted">输入：{node.inputs.join('、') || '无'} · 输出：{node.declared_outputs.join('、') || '无'}</p>{node.error && <p className="cw-error">{node.error}</p>}<details><summary>实际运行参数 / 未运行时的保存参数</summary><pre>{json(node.parameters)}</pre></details><details><summary>连接关系</summary>{current.edges.filter(e => e.source === node.id || e.target === node.id).map((e, i) => <p key={i}>{e.source}.{e.source_port} → {e.target}.{e.target_port}</p>)}</details>
      {Object.entries(node.outputs).map(([port, ref]) => <div className="cw-output" key={port}><span>{port} · {ref.type} <code>{ref.hash.slice(0, 10)}…</code></span><button className="cw-action" type="button" disabled={busy} onClick={() => setOutputHash(ref.hash)}>查看输出</button></div>)}
      {node.has_subflow && <button className="cw-action" type="button" disabled={busy} onClick={async()=>{setBusy(true);setError('');try{const path=[current.module_path,node.id].filter(Boolean).join('/');setInside(await get<WorkflowView>(`/api/workflows/${current.workflow_id}/module-view?revision=${current.revision}&node_path=${encodeURIComponent(path)}${current.run_id?`&run_id=${current.run_id}`:''}`))}catch(e){setError(String(e))}finally{setBusy(false)}}}>展开内部模块</button>}
      <a className="cw-action" href={`#/function/${encodeURIComponent(node.kind)}/${node.version}`} target="_blank" rel="noopener noreferrer">查看函数代码 ↗</a>
      {outputHash&&<ArtifactPreview key={outputHash} hash={outputHash} provenance={`r${current.revision} · ${current.run_id||''}`}/>}
      {inside&&<WorkflowTrace value={inside} compact={compact}/>}
      {node.child_run_id && current.run_id && <button className="cw-action" type="button" disabled={busy} onClick={() => read(() => get(`/api/workflows/runs/${current.run_id}/children/${node.child_run_id}`))}>查看内部函数证据</button>}
      {!Object.keys(node.outputs).length && <p className="cw-muted">尚无输出证据。</p>}{evidence && <pre className="cw-evidence">{evidence}</pre>}
    </div>}
  </section>
}
