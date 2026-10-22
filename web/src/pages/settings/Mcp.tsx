import { useCallback, useEffect, useState } from 'react'
import { del, get, post } from '../../http'
import { appConfirm } from '../../components/dialog'
import './mcp.css'

type Server = { name: string; transport: 'stdio' | 'http'; command: string; args: string[]; url: string; description: string; enabled: boolean }
type Probe = { tools: { name: string; description?: string; inputSchema?: unknown }[]; tool_count: number }
const empty: Server = { name: '', transport: 'stdio', command: 'python', args: [], url: '', description: '', enabled: false }

export default function Mcp({ admin, notify }: { admin: boolean; notify: (text: string, error?: boolean) => void }) {
  const [servers, setServers] = useState<Server[]>([])
  const [allowed, setAllowed] = useState(false)
  const [draft, setDraft] = useState<Server | null>(null)
  const [args, setArgs] = useState('')
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [probes, setProbes] = useState<Record<string, Probe>>({})
  const load = useCallback(async () => {
    try { const data = await get<{ servers: Server[]; calls_allowed: boolean }>('/api/mcp/servers'); setServers(data.servers); setAllowed(data.calls_allowed) }
    catch (e) { setError(String(e)) }
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
    setProbes({}); setDraft(null); await load(); notify('已保存')
  }
  return <div className="mcp-settings">
    <div className="mcp-toolbar"><p>连接外部工具与数据服务</p><button className="button primary" disabled={!admin || busy} onClick={() => { setDraft({ ...empty }); setArgs('[]'); setEditing(false) }}>添加服务</button></div>
    {!allowed && <p className="muted">连接测试需在本地服务中开启 QUANTLAB_ALLOW_MCP=1。</p>}
    {error && <p role="alert" className="rs-error">{error}</p>}
    {servers.length === 0 && <p>还没有添加 MCP 服务。</p>}
    {servers.map(server => <section className="mcp-server" key={server.name}>
      <div className="mcp-toolbar"><div><strong>{server.name}</strong> <span>{server.transport.toUpperCase()} · {server.enabled ? '已启用' : '已停用'}</span></div><div className="mcp-actions">
        <button className="button small" disabled={busy || !allowed || !server.enabled} onClick={() => void run(async () => { const result = await post<Probe>(`/api/mcp/servers/${encodeURIComponent(server.name)}/probe`, {}); setProbes(p => ({ ...p, [server.name]: result })) })}>测试连接</button>
        <button className="button small" disabled={busy || !admin} onClick={() => { setDraft({ ...server }); setArgs(JSON.stringify(server.args)); setEditing(true) }}>编辑</button>
        <button className="button small" disabled={busy || !admin} onClick={() => void run(async () => { if (!await appConfirm(`删除「${server.name}」的连接配置？`)) return; await del(`/api/mcp/servers/${encodeURIComponent(server.name)}`); setProbes(p => { const next = { ...p }; delete next[server.name]; return next }); await load() })}>删除</button>
      </div></div>
      {server.description && <p>{server.description}</p>}
      <code>{server.transport === 'http' ? server.url : [server.command, ...server.args].join(' ')}</code>
      {probes[server.name] && <div role="status"><p>连接成功 · {probes[server.name].tool_count} 个工具</p>{probes[server.name].tools.map(tool => <details key={tool.name}><summary>{tool.name}</summary><p>{tool.description}</p><pre>{JSON.stringify(tool.inputSchema, null, 2)}</pre></details>)}</div>}
    </section>)}
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
