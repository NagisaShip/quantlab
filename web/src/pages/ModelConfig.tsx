import { useCallback, useEffect, useState } from 'react'
import { api, type Preset } from '../api'
import { v3, type LlmStatus, type ProviderView } from '../api2'
import { Callout, Field, Heading, Loading, Modal, Panel, SourceBanner, Tag } from '../components/ui'
import { get, request } from '../http'
import type { PageProps } from './types'

interface Draft { name: string; type: string; base_url: string; models: string; api_key: string; editing: boolean; has_key: boolean }
const BLANK: Draft = { name: '', type: 'openai_compatible', base_url: '', models: '', api_key: '', editing: false, has_key: false }

const ROLES: [string, string, string][] = [
  ['default', '默认', '问答、角色调研等未单独指定的用途'],
  ['code_gen', '代码生成', '策略生成、修改、修复——建议用代码能力强、温度低的模型'],
  ['reasoning', '复杂推理', 'AI 审稿、对抗质疑'],
  ['report', '报告解读', '回测解读、每日简报——可用便宜模型'],
]

export default function ModelConfigPage({ notify }: PageProps) {
  const [config, setConfig] = useState<{ providers: ProviderView[]; routing: Record<string, string | string[]>; config_path: string | null } | null>(null)
  const [notifyCfg, setNotifyCfg] = useState<Record<string, string>>({})
  const [exchange, setExchange] = useState<Record<string, unknown>>({})
  const [presets, setPresets] = useState<Preset[]>([])
  const [status, setStatus] = useState<LlmStatus | null>(null)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [routing, setRouting] = useState<Record<string, string>>({})
  const [fallback, setFallback] = useState<string[]>([])
  const [testTarget, setTestTarget] = useState('')
  const [testResult, setTestResult] = useState<{ ok: boolean; reply?: string; error?: string; model?: string; provider?: string } | null>(null)
  const [testing, setTesting] = useState(false)

  const refresh = useCallback(async () => {
    const [c, p, s, old] = await Promise.all([v3.config(), api.presets(), v3.llmStatus(), api.config()])
    setConfig(c)
    setPresets(p.presets)
    setStatus(s)
    setNotifyCfg(old.notify)
    setExchange(old.exchange)
    const r: Record<string, string> = {}
    for (const [k] of ROLES) r[k] = String(c.routing[k] ?? '')
    setRouting(r)
    setFallback(Array.isArray(c.routing.fallback) ? (c.routing.fallback as string[]) : [])
    const firstKeyed = c.providers.find((x) => x.has_key && x.name !== 'local')
    setTestTarget((prev) => prev || (firstKeyed ? `${firstKeyed.name}/${firstKeyed.models[0]}` : ''))
  }, [])

  useEffect(() => {
    refresh().catch((e: Error) => setError(e.message))
  }, [refresh])

  const allModels = config?.providers.flatMap((p) => p.models.filter(Boolean).map((m) => `${p.name}/${m}`)) ?? []
  const keyed = config?.providers.filter((p) => p.has_key && p.name !== 'local') ?? []

  const saveProvider = async () => {
    if (!draft) return
    try {
      await v3.saveProvider({
        name: draft.name.trim().toLowerCase(),
        type: draft.type,
        base_url: draft.base_url.trim(),
        models: draft.models.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean),
        api_key: draft.api_key.trim(),
      })
      setDraft(null)
      await refresh()
      notify('已保存。密钥存放在本机数据目录的 secrets.json，接口只返回脱敏值')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const removeProvider = async (p: ProviderView) => {
    if (!window.confirm(`删除「${p.name}」在界面里登记的配置与密钥？配置文件里的同名 Provider 会恢复为文件中的设置。`)) return
    try {
      await v3.deleteProvider(p.name)
      await refresh()
      notify('已删除')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const saveRouting = async () => {
    try {
      await v3.saveRouting({ default: routing.default, code_gen: routing.code_gen, reasoning: routing.reasoning, report: routing.report, fallback })
      await refresh()
      notify('路由已保存')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const runTest = async () => {
    if (!testTarget) return
    setTesting(true)
    setTestResult(null)
    try {
      setTestResult(await api.testProvider(testTarget, '用一句话介绍你自己，并说明你是哪个模型。'))
    } catch (e) {
      setTestResult({ ok: false, error: (e as Error).message })
    } finally {
      setTesting(false)
    }
  }

  return (
    <>
      <Heading
        eyebrow="MODEL CONFIG"
        title="AI 模型"
        description="登记任意兼容 OpenAI 接口的大模型服务（DeepSeek、通义、Kimi、智谱、vLLM、Ollama…）或 Anthropic，然后按用途分配：代码生成、复杂推理、报告解读。没有任何密钥时，AI 功能会走模板与规则降级，平台其余功能完全可用。"
        actions={<button className="button primary" onClick={() => setDraft({ ...BLANK })}>＋ 添加模型服务</button>}
      />
      {error && <Callout tone="fail" title="加载失败">{error}</Callout>}
      {!config ? <Loading /> : (
        <>
          <SourceBanner
            tone={status?.ready ? 'real' : 'warn'}
            icon="⚙"
            title={status?.ready ? `AI 已就绪：${keyed.length} 个云端服务有密钥` : '还没有可用的云端模型密钥'}
            detail={status?.ready ? `代码生成 → ${routing.code_gen || routing.default}` : status?.hint}
            right={config.config_path ? `配置文件 ${config.config_path}` : '内置默认 + 界面配置'}
          />

          <Panel title="模型服务" subtitle="密钥来源优先级：界面填写 > 环境变量 > 配置文件。界面只回显脱敏值；${ENV} 占位符只有在环境变量存在时才算已配置" flush>
            <div className="table-wrap">
              <table>
                <thead><tr><th>名称</th><th>接口</th><th>Base URL</th><th>密钥</th><th>模型</th><th /></tr></thead>
                <tbody>
                  {config.providers.map((p) => (
                    <tr key={p.name}>
                      <td><strong>{p.name}</strong>{p.custom && <small>界面登记</small>}</td>
                      <td><Tag mono>{p.type === 'anthropic' ? 'anthropic' : 'openai 兼容'}</Tag></td>
                      <td className="mono small">{p.base_url || '—'}</td>
                      <td>
                        {p.has_key ? <Tag tone="ok" mono>{p.key_masked || '无需密钥'}</Tag> : <span className="table-pill pending">未配置</span>}
                        <small>{p.key_source}</small>
                      </td>
                      <td className="mono small wrap">{p.models.filter(Boolean).join(', ') || '—'}</td>
                      <td>
                        <div className="row-actions">
                          <button className="button small" onClick={() => setDraft({ name: p.name, type: p.type === 'anthropic' ? 'anthropic' : 'openai_compatible', base_url: p.base_url, models: p.models.join(', '), api_key: '', editing: true, has_key: p.has_key })}>{p.has_key ? '编辑' : '填写密钥'}</button>
                          {p.custom && <button className="button small danger" onClick={() => removeProvider(p)}>删除</button>}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>

          <div className="two-columns even">
            <Panel title="用途路由" subtitle="主模型失败或无密钥时，按降级链依次尝试；整条链失败则走模板 / 规则降级" actions={<button className="button small primary" onClick={saveRouting}>保存路由</button>}>
              {ROLES.map(([k, label, hint]) => (
                <Field key={k} label={label} hint={hint}>
                  <select value={routing[k] ?? ''} onChange={(e) => setRouting({ ...routing, [k]: e.target.value })}>
                    <option value="">（使用默认）</option>
                    {allModels.map((m) => <option key={m} value={m}>{m}{keyed.some((p) => m.startsWith(p.name + '/')) ? '' : '（无密钥）'}</option>)}
                  </select>
                </Field>
              ))}
              <div style={{ marginTop: 12 }}>
                <div className="hint" style={{ marginBottom: 6 }}>降级链（按点击顺序）</div>
                <div className="chip-row">
                  {allModels.map((m) => {
                    const idx = fallback.indexOf(m)
                    return (
                      <button key={m} className={`chip${idx >= 0 ? ' on' : ''}`} onClick={() => setFallback(idx >= 0 ? fallback.filter((x) => x !== m) : [...fallback, m])}>
                        {idx >= 0 ? `${idx + 1}. ` : ''}{m}
                      </button>
                    )
                  })}
                </div>
              </div>
            </Panel>

            <Panel title="连通性测试" subtitle="真实调用一次模型，验证密钥、网络与模型名">
              <Field label="测试目标">
                <select value={testTarget} onChange={(e) => setTestTarget(e.target.value)}>
                  <option value="">选择模型</option>
                  {allModels.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              </Field>
              <button className="button primary" onClick={runTest} disabled={testing || !testTarget}>{testing ? <><span className="spinner" /> 调用中</> : '发送测试请求'}</button>
              {testResult && (
                <Callout tone={testResult.ok ? 'info' : 'fail'} title={testResult.ok ? `调用成功：${testResult.provider}/${testResult.model}` : '调用失败'}>
                  <pre className="pre-wrap">{testResult.ok ? testResult.reply : testResult.error}</pre>
                </Callout>
              )}
              <div className="hint" style={{ marginTop: 10 }}>国内网络访问海外服务可能需要代理；本地 Ollama 需要先运行 <code className="inline">ollama serve</code>。</div>
            </Panel>
          </div>

          <SearchPanel notify={notify} />

          <div className="two-columns even">
            <Panel title="通知通道" subtitle="长期 Agent 推送时使用；值均脱敏显示">
              <table>
                <tbody>
                  <tr><td>默认通道</td><td className="right">{notifyCfg.channel ?? 'console'}</td></tr>
                  <tr><td>企业微信 Webhook</td><td className="right muted">{notifyCfg.wecom_webhook || '未配置'}</td></tr>
                  <tr><td>Server 酱 SendKey</td><td className="right muted">{notifyCfg.serverchan_key || '未配置'}</td></tr>
                  <tr><td>PushPlus token</td><td className="right muted">{notifyCfg.pushplus_token || '未配置'}</td></tr>
                </tbody>
              </table>
              <div className="hint">在 agent.config.toml 的 [notify] 段或环境变量中设置。</div>
            </Panel>
            <Panel title="交易所接入" subtitle="一期只用公共行情与模拟盘">
              <table>
                <tbody>
                  <tr><td>主交易所</td><td className="right">{String(exchange.primary ?? 'binance')}</td></tr>
                  <tr><td>模拟盘</td><td className="right">{exchange.simulated ? <span className="table-pill">启用</span> : <span className="table-pill pending">关闭</span>}</td></tr>
                  <tr><td>OKX 公共域名</td><td className="right mono small">{String(exchange.okx_base ?? 'openapi.okx.com')}</td></tr>
                </tbody>
              </table>
              <div className="hint">访问受限地区可在配置文件 [exchange] 段设置 okx_base，或设置环境变量 QUANTLAB_OKX_BASE（只允许 OKX 官方域名）。</div>
            </Panel>
          </div>
        </>
      )}

      {draft && (
        <Modal title={draft.editing ? `编辑 ${draft.name}` : '添加模型服务'} onClose={() => setDraft(null)}
          footer={<><button className="button" onClick={() => setDraft(null)}>取消</button><button className="button primary" onClick={saveProvider} disabled={!draft.name || !draft.base_url || !draft.models}>保存</button></>}>
          {!draft.editing && (
            <Field label="从预设快速填充">
              <div className="chip-row">
                {presets.map((p) => (
                  <button key={p.name} className="chip" onClick={() => setDraft({ ...draft, name: p.name, type: p.type === 'anthropic' ? 'anthropic' : 'openai_compatible', base_url: p.base_url, models: p.models.filter(Boolean).join(', ') || (p.type === 'anthropic' ? 'claude-opus-5-5' : '') })}>
                    {p.label.replace('[OI]', 'OpenAI')}
                  </button>
                ))}
              </div>
            </Field>
          )}
          <div className="form-grid">
            <Field label="名称" hint="小写字母、数字、-、_；路由里用它引用">
              <input value={draft.name} disabled={draft.editing} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
            </Field>
            <Field label="接口类型">
              <select value={draft.type} onChange={(e) => setDraft({ ...draft, type: e.target.value })}>
                <option value="openai_compatible">OpenAI 兼容（DeepSeek / 通义 / Kimi / 智谱 / vLLM / Ollama）</option>
                <option value="anthropic">Anthropic</option>
              </select>
            </Field>
          </div>
          <Field label="Base URL"><input value={draft.base_url} onChange={(e) => setDraft({ ...draft, base_url: e.target.value })} placeholder="https://api.deepseek.com/v1" /></Field>
          <Field label="模型名" hint="逗号分隔，第一个作为该服务的默认模型"><input value={draft.models} onChange={(e) => setDraft({ ...draft, models: e.target.value })} placeholder="deepseek-chat, deepseek-reasoner" /></Field>
          <Field label="API Key" hint={draft.has_key ? '已有密钥；留空表示不修改' : '保存在本机数据目录的 secrets.json，不进入 git，接口只回显脱敏值'}>
            <input type="password" autoComplete="off" value={draft.api_key} onChange={(e) => setDraft({ ...draft, api_key: e.target.value })} placeholder={draft.has_key ? '••••••（不修改）' : 'sk-...'} />
          </Field>
        </Modal>
      )}

      <Callout>
        密钥分级：大模型密钥、只读行情密钥、模拟盘密钥与实盘密钥互不复用；实盘密钥不进入 Agent 环境。AI 只产出代码草案与分析，不能下单。
      </Callout>
    </>
  )
}

interface SearchStatus { configured: boolean; provider?: string | null; key_masked?: string; providers?: { id: string; label: string; signup: string }[] }

/** 联网搜索：策略室与团队角色的 web_search / fetch_page 工具用它。 */
function SearchPanel({ notify }: { notify: PageProps['notify'] }) {
  const [st, setSt] = useState<SearchStatus | null>(null)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { get<SearchStatus>('/api/config/search').then(setSt).catch(() => undefined) }, [])
  const save = async (value: string) => {
    setBusy(true)
    try {
      const r = await request<SearchStatus>('/api/config/search', { method: 'PUT', body: JSON.stringify({ provider: 'tavily', api_key: value }) })
      setSt(r)
      setKey('')
      notify(value ? '已保存联网搜索密钥' : '已移除联网搜索密钥')
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Panel title="联网搜索" subtitle="AI 研究室与团队角色可调用联网搜索、读取网页；未配置时这两个工具会提示不可用">
      <div className="search-config">
        <span className={`pill${st?.configured ? ' real' : ''}`}><i />{st?.configured ? `Tavily 已配置 · ${st.key_masked}` : '未配置'}</span>
        <input type="password" placeholder="Tavily API Key（tvly-…）" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" />
        <button className="button small primary" disabled={busy || !key.trim()} onClick={() => save(key.trim())}>保存</button>
        {st?.configured && <button className="button small ghost" disabled={busy} onClick={() => save('')}>移除</button>}
      </div>
      <div className="hint" style={{ marginTop: 8 }}>
        密钥加密存放，只有部署管理员可以修改。申请地址：<a href="https://tavily.com" target="_blank" rel="noreferrer">tavily.com</a>
      </div>
    </Panel>
  )
}
