import { useCallback, useEffect, useState } from 'react'
import { del, get, post } from '../../http'
import { appConfirm } from '../../components/dialog'
import './mcp.css'

type Health = { status: string; started: boolean; implemented: boolean; detail?: string }
type Server = {
  name: string
  transport: 'stdio' | 'http'
  command: string
  args: string[]
  url: string
  description: string
  enabled: boolean
  health: Health
}
type ProbeTool = { name: string; description?: string; inputSchema?: unknown }
type Probe = { tools: ProbeTool[]; tool_count: number }
type CallState = { tool: string; args: string; result?: unknown; error?: string; busy?: boolean }
const empty: Server = {
  name: '', transport: 'stdio', command: 'python', args: [], url: '', description: '', enabled: false,
  health: { status: 'disabled', started: false, implemented: true },
}

function parseObject(text: string): Record<string, unknown> {
  const value: unknown = JSON.parse(text || '{}')
  if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('参数须为 JSON 对象')
  return value as Record<string, unknown>
}

function json(value: unknown) {
  return JSON.stringify(value, null, 2)
}

export default function Mcp({ admin, notify }: { admin: boolean; notify: (text: string, error?: boolean) => void }) {
  const [servers, setServers] = useState<Server[]>([])
  const [allowed, setAllowed] = useState(false)
  const [draft, setDraft] = useState<Server | null>(null)
  const [args, setArgs] = useState('')
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [probes, setProbes] = useState<Record<string, Probe>>({})
  const [calls, setCalls] = useState<Record<string, CallState>>({})

  const load = useCallback(async () => {
    try {
      const data = await get<{ servers: Server[]; calls_allowed: boolean }>('/api/mcp/servers')
      setServers(data.servers)
      setAllowed(data.calls_allowed)
    } catch (e) { setError(String(e)) }
  }, [])
  useEffect(() => { void load() }, [load])

  async function run(action: () => Promise<void>) {
    setBusy(true); setError('')
    try { await action() } catch (e) { setError(String(e)); notify(String(e), true) } finally { setBusy(false) }
  }
  async function save() {
    if (!draft) return
    const parsed: unknown = JSON.parse(args)
    if (!Array.isArray(parsed) || !parsed.every(x => typeof x === 'string')) throw new Error('启动参数须为 JSON 字符串数组')
    await post('/api/mcp/servers', { ...draft, args: parsed })
    setProbes({}); setCalls({}); setDraft(null); await load(); notify('已保存')
  }
  async function toggle(server: Server) {
    await post('/api/mcp/servers', { ...server, enabled: !server.enabled })
    if (server.enabled) setProbes(p => { const next = { ...p }; delete next[server.name]; return next })
    await load()
    notify(server.enabled ? '已停用' : '已启用')
  }
  async function callTool(server: Server) {
    const current = calls[server.name]
    if (!current) return
    setCalls(c => ({ ...c, [server.name]: { ...current, busy: true, error: '' } }))
    try {
      const result = await post<{ result: unknown }>(`/api/mcp/servers/${encodeURIComponent(server.name)}/call`, {
        tool: current.tool, arguments: parseObject(current.args),
      })
      setCalls(c => ({ ...c, [server.name]: { ...current, busy: false, result: result.result } }))
    } catch (e) {
      setCalls(c => ({ ...c, [server.name]: { ...current, busy: false, error: String(e) } }))
    }
  }
  function setCall(server: Server, patch: Partial<CallState>) {
    const current = calls[server.name] || { tool: '', args: '{}' }
    setCalls(c => ({ ...c, [server.name]: { ...current, ...patch } }))
  }

  return <div className="mcp-settings">
    <div className="mcp-toolbar">
      <div><p>连接外部工具与数据服务</p>{servers.length > 0 && <small className="mcp-count">{servers.length} 个服务</small>}</div>
      <button className="button primary" disabled={!admin || busy} onClick={() => { setDraft({ ...empty }); setArgs('[]'); setEditing(false) }}>添加服务</button>
    </div>
    {!admin && <p className="mcp-note">只读</p>}
    {!allowed && <p className="mcp-note">连接测试需在本地服务中开启 QUANTLAB_ALLOW_MCP=1。</p>}
    {error && <p role="alert" className="rs-error">{error}</p>}
    {servers.length === 0 && <p>还没有添加 MCP 服务。</p>}
    {servers.map(server => {
      const probe = probes[server.name]
      const call = calls[server.name]
      return <section className="mcp-server" key={server.name}>
        <div className="mcp-toolbar">
          <div className="mcp-title"><strong>{server.name}</strong><span>{server.transport.toUpperCase()}</span><span className={`mcp-status ${server.enabled ? 'on' : ''}`}>{server.enabled ? '已启用' : '已停用'}</span></div>
          <div className="mcp-actions">
            <button className="button small" disabled={busy || !admin} onClick={() => void run(() => toggle(server))}>{server.enabled ? '停用' : '启用'}</button>
            <button className="button small" disabled={busy || !allowed || !server.enabled} onClick={() => void run(async () => {
              const result = await post<Probe>(`/api/mcp/servers/${encodeURIComponent(server.name)}/probe`, {})
              setProbes(p => ({ ...p, [server.name]: result }))
            })}>测试连接</button>
            <button className="button small" disabled={busy || !admin} onClick={() => { setDraft({ ...server }); setArgs(JSON.stringify(server.args)); setEditing(true) }}>编辑</button>
            <button className="button small" disabled={busy || !admin} onClick={() => void run(async () => {
              if (!await appConfirm(`删除「${server.name}」的连接配置？`)) return
              await del(`/api/mcp/servers/${encodeURIComponent(server.name)}`)
              setProbes(p => { const next = { ...p }; delete next[server.name]; return next })
              setCalls(c => { const next = { ...c }; delete next[server.name]; return next })
              await load()
            })}>删除</button>
          </div>
        </div>
        {server.description && <p>{server.description}</p>}
        <code>{server.transport === 'http' ? server.url : [server.command, ...server.args].join(' ')}</code>
        {server.enabled && !probe && <small className="mcp-health">{server.health.status === 'not_probed' ? '未测试' : server.health.status}</small>}
        {probe && <div className="mcp-probe" role="status">
          <div className="mcp-toolbar"><strong>连接成功 · {probe.tool_count} 个工具</strong></div>
          {probe.tools.length === 0 && <p>没有可用工具。</p>}
          {probe.tools.map(tool => <div key={tool.name} className="mcp-tool">
            <div className="mcp-tool-head"><code>{tool.name}</code><button type="button" className="button small" disabled={!allowed} onClick={() => setCall(server, { tool: tool.name, args: '{}', result: undefined, error: '' })}>调用</button></div>
            {tool.description && <p>{tool.description}</p>}
            <details><summary>参数</summary><pre>{json(tool.inputSchema || {})}</pre></details>
          </div>)}
        </div>}
        {call && <div className="mcp-call">
          <div className="mcp-toolbar"><strong>{call.tool}</strong><button type="button" className="button small" onClick={() => setCalls(c => { const next = { ...c }; delete next[server.name]; return next })}>取消</button></div>
          <label>参数<textarea value={call.args} onChange={e => setCall(server, { args: e.target.value })} /></label>
          <button type="button" className="button primary" disabled={call.busy || !allowed} onClick={() => void callTool(server)}>运行</button>
          {call.error && <p role="alert" className="rs-error">{call.error}</p>}
          {call.result !== undefined && <pre>{json(call.result)}</pre>}
        </div>}
      </section>
    })}
    {draft && <form className="mcp-server mcp-form" onSubmit={e => { e.preventDefault(); void run(save) }}>
      <h3>{editing ? '编辑服务' : '添加服务'}</h3>
      <label>名称<input required pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}" disabled={editing} value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })}/></label>
      <label>传输方式<select value={draft.transport} onChange={e => setDraft({ ...draft, transport: e.target.value as Server['transport'] })}><option value="stdio">本地进程（stdio）</option><option value="http">HTTP</option></select></label>
      {draft.transport === 'stdio' ? <><label>启动程序<input required value={draft.command} onChange={e => setDraft({ ...draft, command: e.target.value })}/></label><label>启动参数（JSON 数组）<textarea aria-label="启动参数（JSON 数组）" value={args} onChange={e => setArgs(e.target.value)} placeholder={'["-m", "your_mcp_server"]'}/></label></> : <label>服务地址<input required type="url" value={draft.url} onChange={e => setDraft({ ...draft, url: e.target.value })}/></label>}
      <label>描述<input value={draft.description} onChange={e => setDraft({ ...draft, description: e.target.value })}/></label>
      <label><input type="checkbox" checked={draft.enabled} onChange={e => setDraft({ ...draft, enabled: e.target.checked })}/>启用服务</label>
      <div className="mcp-actions"><button type="submit" className="button primary" disabled={busy}>保存</button><button type="button" className="button" disabled={busy} onClick={() => setDraft(null)}>取消</button></div>
    </form>}
  </div>
}
