import { useEffect, useState } from 'react'
import { CodeEditor, CodeView } from '../../components/CodeEditor'
import { DiffView } from '../../components/Diff'
import { useAskAIContext } from '../../components/AskAI'
import { Heading } from '../../components/ui'
import { get, post } from '../../http'
import type { PageProps } from '../../pages/types'
import { applyDefinition } from './session'
import { uid, type Definition } from './types'
import './workflows.css'

type Source = { path: string; code: string; sha256: string; kind?: string; version?: string }
const initial = 'NODE = {"kind":"user.transform","version":"1","title":"自定义处理","level":"function","inputs":{"data":{"type":"table"}},"outputs":{"data":{"type":"table","role_from":"data"}},"parameters":{"type":"object","properties":{},"additionalProperties":False},"requirements":[]}\n\ndef execute(ctx, inputs, params):\n    ctx.log("处理数据")\n    return {"data": inputs["data"]}\n'
export default function FunctionPage({ arg, onNavigate, notify }: PageProps & { arg: string }) {
  const [path, query = ''] = arg.split('?'); const [kind, version] = path.split('/')
  const params = new URLSearchParams(query); const node = params.get('node') || ''; const run = params.get('run') || ''
  const [definition, setDefinition] = useState<Definition | null>(null)
  const [files, setFiles] = useState<Source[]>([])
  const [active, setActive] = useState('NODE.py')
  const [source, setSource] = useState('')
  const [before, setBefore] = useState('')
  const [editing, setEditing] = useState(kind === 'new')
  const [checked, setChecked] = useState(false)
  const [difference, setDifference] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const draftKey = `quantlab.function.${kind}.${version}.${node}.${run}`
  useEffect(() => {
    let live = true
    if (kind === 'new') { setSource(sessionStorage.getItem(draftKey) || initial); setBefore(initial); return }
    get<{ definition: Definition; files: Source[] }>(`/api/workflows/definitions/${encodeURIComponent(kind)}/${encodeURIComponent(version)}/sources${run ? `?run_id=${encodeURIComponent(run)}` : ''}`)
      .then(value => { if (!live) return; setDefinition(value.definition); setFiles(value.files); setBefore(value.definition.code); const draft=sessionStorage.getItem(draftKey); setSource(draft || value.definition.code); if(draft)setEditing(true) }).catch(e => setError(String(e)))
    return () => { live = false }
  }, [kind, version, run, draftKey])
  const action = async (fn: () => Promise<void>) => { setBusy(true); setError(''); try { await fn() } catch (e) { setError(String(e)) } finally { setBusy(false) } }
  const change = (value: string) => { setSource(value); setChecked(false); sessionStorage.setItem(draftKey, value) }
  const fork = () => {
    const text = source.replace(/(["'])kind\1\s*:\s*(["'])[^"']+\2/, `"kind": "user.${uid('function')}"`).replace(/(["'])version\1\s*:\s*(["'])[^"']+\2/, '"version": "1"')
    change(text); setActive('NODE.py'); setEditing(true)
  }
  const check = () => action(async () => {
    const value = await post<{ changed: unknown }>('/api/workflows/definitions/preview', { code: source, old_kind: definition?.kind || '', old_version: definition?.version || '' })
    setDifference(value.changed); setChecked(true); notify('语法与接口检查通过；执行前仍需全链路自检')
  })
  const publish = () => action(async () => {
    if (!checked) throw Error('先检查当前代码与接口')
    const value = await post<Definition>('/api/workflows/definitions', { code: source })
    if (node) applyDefinition(node, value.kind, String(value.version))
    sessionStorage.removeItem(draftKey); notify(node ? '新版本已应用到画布草稿，返回后可检查并保存' : '自定义函数已加入卡片库'); onNavigate('workflows')
  })
  const selected = files.find(file => file.path === active)
  useAskAIContext({kind:'workflow',title:`研究函数 ${definition?.title || kind}`,text:JSON.stringify({kind,version,run,node,active,parameters:definition?.parameters}),code:selected?.code || source})
  return <div className="wf-page wf-code-page">
    <Heading title={definition?.title || '自定义研究函数'} description={run ? `冻结运行 ${run} 的代码 · 修改时会创建独立版本` : '函数定义、参数契约与真实实现文件'} />
    <div className="wf-toolbar"><h2>{definition?.title || '自定义研究函数'}</h2><button className="button" onClick={() => onNavigate('workflows')}>返回研究画布</button><span>{definition?.kind}@{definition?.version}</span><button className="button" onClick={fork} disabled={busy}>复制为自定义函数</button>{editing && <><button className="button" disabled={busy} onClick={check}>检查语法与接口</button><button className="button primary" disabled={busy || !checked} onClick={publish}>发布{node ? '并应用到此卡片' : '到函数库'}</button></>}</div>
    {error && <p className="wf-error" role="alert">{error}</p>}
    <div className="wf-code-layout"><aside className="wf-source-list"><button className={active === 'NODE.py' ? 'on' : ''} onClick={() => setActive('NODE.py')}>NODE.py · 接口与入口</button>{files.map(file => <button className={active === file.path ? 'on' : ''} key={file.path} onClick={() => setActive(file.path)}>{file.path}</button>)}</aside><section>
      {active === 'NODE.py' ? editing ? <CodeEditor value={source} onChange={change} onSave={check} minHeight={560} /> : <CodeView code={source} maxHeight={700} /> : <><p>实现包文件 · {selected?.sha256}<br />复制后的入口可以调用、组合或替换这里的函数；包文件为只读来源证据。</p>{selected?.kind && <button className="button" onClick={()=>onNavigate(`function/${selected.kind}/${selected.version}${run ? `?run=${encodeURIComponent(run)}` : ''}`)}>打开此内部函数并自定义</button>}<CodeView code={selected?.code || ''} maxHeight={700} /></>}
      {editing && <details><summary>与来源版本比较</summary><DiffView before={before} after={source} /></details>}
      {difference !== null && <details open><summary>接口变化</summary><pre>{JSON.stringify(difference, null, 2)}</pre></details>}
    </section><aside className="wf-source-contract"><h2>函数契约</h2><p>{definition?.description}</p>{definition && <pre>{JSON.stringify({ inputs: definition.inputs, outputs: definition.outputs, parameters: definition.parameters, requirements: definition.requirements, resources: definition.resources }, null, 2)}</pre>}<p>参数与端口由 NODE 声明统一生成；版本不可覆盖。运行记录保留原版本。</p></aside></div>
  </div>
}
