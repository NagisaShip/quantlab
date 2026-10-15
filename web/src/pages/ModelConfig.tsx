import { useCallback, useEffect, useState } from 'react'
import { api, type Preset } from '../api'
import { v3, type ProviderView } from '../api2'
import { Callout, Heading, Loading } from '../components/ui'
import { get, request } from '../http'
import type { PageProps } from './types'

/**
 * 设置 · 模型：服务商卡片列表 + 内嵌编辑卡（第三方预设 / 自定义 API），下面是用途路由、连通性测试、联网搜索。
 * 密钥只回显脱敏值；编辑已有服务商时密钥留空表示不修改。
 */

const ROLES: [string, string, string][] = [
  ['default', '默认', '问答、角色调研等未单独指定的用途'],
  ['code_gen', '代码生成', '策略生成、修改、修复，建议用代码能力强的模型'],
  ['reasoning', '复杂推理', 'AI 审稿、对抗质疑'],
  ['report', '报告解读', '回测解读、每日简报，可用便宜模型'],
]

type Mode = 'preset' | 'custom'
interface Draft {
  mode: Mode
  editing: string | null // 正在编辑的服务商名；null 表示新建
  name: string
  type: string
  base_url: string
  api_key: string
  models: string[]
  has_key: boolean
}

const presetLabel = (p: Preset) => p.label.replace('[OI]', 'OpenAI')

const PROTOCOLS: [string, string][] = [
  ['openai_compatible', 'OpenAI Chat Completions（DeepSeek / 通义 / vLLM / Ollama…）'],
  ['openai_responses', 'OpenAI Responses'],
  ['anthropic', 'Anthropic Messages'],
]
const protocolOf = (t: string) => (PROTOCOLS.some(([k]) => k === t) ? t : 'openai_compatible')

function ProtocolSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <label className="prov-field">API 协议
      <select value={protocolOf(value)} onChange={(e) => onChange(e.target.value)}>
        {PROTOCOLS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
      </select>
    </label>
  )
}

function draftFromPreset(p: Preset, prev?: Draft): Draft {
  return {
    mode: 'preset', editing: null, name: p.name, type: p.type === 'anthropic' ? 'anthropic' : 'openai_compatible',
    base_url: '', api_key: prev?.api_key ?? '', models: p.models.filter(Boolean), has_key: false,
  }
}

export default function ModelConfigPage({ notify }: PageProps) {
  const [config, setConfig] = useState<{ providers: ProviderView[]; routing: Record<string, string | string[]>; config_path: string | null } | null>(null)
  const [presets, setPresets] = useState<Preset[]>([])
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [routing, setRouting] = useState<Record<string, string>>({})
  const [fallback, setFallback] = useState<string[]>([])

  const refresh = useCallback(async () => {
    const [c, p] = await Promise.all([v3.config(), api.presets()])
    setConfig(c)
    setPresets(p.presets)
    const r: Record<string, string> = {}
    for (const [k] of ROLES) r[k] = String(c.routing[k] ?? '')
    setRouting(r)
    setFallback(Array.isArray(c.routing.fallback) ? (c.routing.fallback as string[]) : [])
  }, [])

  useEffect(() => { refresh().catch((e: Error) => setError(e.message)) }, [refresh])

  const allModels = config?.providers.flatMap((p) => p.models.filter(Boolean).map((m) => `${p.name}/${m}`)) ?? []
  const keyed = config?.providers.filter((p) => p.has_key && p.name !== 'local') ?? []
  const presetOf = (name: string) => presets.find((p) => p.name === name)

  const edit = (p: ProviderView) => {
    const pre = presetOf(p.name)
    setDraft({
      mode: pre ? 'preset' : 'custom', editing: p.name, name: p.name,
      type: protocolOf(p.type),
      base_url: pre && p.base_url === pre.base_url ? '' : p.base_url, api_key: '', models: p.models.filter(Boolean), has_key: p.has_key,
    })
  }

  const remove = async (p: ProviderView) => {
    if (!window.confirm(`删除「${p.name}」在界面里登记的配置与密钥？配置文件里的同名服务商会恢复为文件中的设置。`)) return
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
      notify('用途路由已保存')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  return (
    <>
      <Heading title="模型" description="填入各服务商的 API 密钥即可使用其模型。没有任何密钥时，AI 功能会走模板与规则降级，其余功能照常可用。" />
      {error && <Callout tone="fail" title="加载失败">{error}</Callout>}
      {!config ? <Loading /> : (
        <>
          {draft ? (
            <ProviderEditor
              draft={draft} setDraft={setDraft} presets={presets} existing={config.providers}
              onCancel={() => setDraft(null)}
              onSaved={async (msg) => { setDraft(null); await refresh(); notify(msg) }}
              notify={notify}
            />
          ) : (
            <div className="prov-list">
              {config.providers.map((p) => {
                const pre = presetOf(p.name)
                return (
                  <div key={p.name} className="prov-card">
                    <div className="prov-main">
                      <div className="prov-name">
                        <b>{pre ? presetLabel(pre) : p.name}</b>
                        {p.custom && !pre && <span className="prov-tag">自定义</span>}
                        <i className={`prov-dot${p.has_key ? ' ok' : ''}`} title={p.has_key ? '已配置密钥' : '未配置密钥'} />
                      </div>
                      <small>{p.has_key ? (p.key_masked || p.key_source) : '未配置密钥'} · {p.models.filter(Boolean).length} 个模型</small>
                    </div>
                    <div className="prov-actions">
                      <button className="button small" onClick={() => edit(p)}>{p.has_key ? '编辑' : '填写密钥'}</button>
                      {p.custom && <button className="button small ghost danger-text" onClick={() => remove(p)}>删除</button>}
                    </div>
                  </div>
                )
              })}
              <button className="prov-add" onClick={() => setDraft(presets[0] ? draftFromPreset(presets[0]) : { mode: 'custom', editing: null, name: '', type: 'openai_compatible', base_url: '', api_key: '', models: [''], has_key: false })}>
                ＋ 添加模型服务商
              </button>
            </div>
          )}

          <section className="set-section">
            <div className="set-section-head">
              <h3>用途路由</h3>
              <button className="button small primary" onClick={saveRouting}>保存</button>
            </div>
            <p className="set-section-note">主模型失败或没有密钥时，按降级链依次尝试；整条链都失败则走模板降级。</p>
            {ROLES.map(([k, label, hint]) => (
              <div key={k} className="set-row">
                <div><b>{label}</b><small>{hint}</small></div>
                <select value={routing[k] ?? ''} onChange={(e) => setRouting({ ...routing, [k]: e.target.value })}>
                  <option value="">（使用默认）</option>
                  {allModels.map((m) => <option key={m} value={m}>{m}{keyed.some((p) => m.startsWith(p.name + '/')) ? '' : '（无密钥）'}</option>)}
                </select>
              </div>
            ))}
            <div className="set-row stack">
              <div><b>降级链</b><small>按点击顺序依次尝试</small></div>
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
          </section>

          <TestSection allModels={allModels} defaultTarget={keyed[0] ? `${keyed[0].name}/${keyed[0].models[0]}` : ''} />
          <SearchSection notify={notify} />
          <p className="set-foot">
            密钥来源优先级：界面填写 &gt; 环境变量 &gt; 配置文件{config.config_path ? `（${config.config_path}）` : ''}。大模型密钥与行情、模拟盘、实盘密钥互不复用；AI 只产出代码草案与分析，不能下单。
          </p>
        </>
      )}
    </>
  )
}

/** 内嵌编辑卡：第三方预设只需选服务商、填密钥；自定义 API 填完整连接信息。 */
function ProviderEditor({ draft, setDraft, presets, existing, onCancel, onSaved, notify }: {
  draft: Draft; setDraft: (d: Draft) => void; presets: Preset[]; existing: ProviderView[]
  onCancel: () => void; onSaved: (msg: string) => void; notify: PageProps['notify']
}) {
  const [busy, setBusy] = useState(false)
  const [discovering, setDiscovering] = useState(false)
  const [found, setFound] = useState<string[] | null>(null)
  const pre = presets.find((p) => p.name === draft.name)
  const isPreset = draft.mode === 'preset'
  const baseUrl = draft.base_url.trim() || (isPreset ? pre?.base_url ?? '' : '')
  const models = draft.models.map((m) => m.trim()).filter(Boolean)
  const nameOk = /^[a-z0-9][a-z0-9_-]{0,30}$/.test(draft.name.trim().toLowerCase())
  const taken = !draft.editing && existing.some((p) => p.name === draft.name.trim().toLowerCase() && p.custom)
  const valid = nameOk && /^https?:\/\//.test(baseUrl) && models.length > 0 && !taken
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch })

  const discover = async () => {
    setDiscovering(true)
    try {
      const r = await v3.discoverModels({ name: draft.editing ?? draft.name, type: draft.type, base_url: baseUrl, api_key: draft.api_key.trim() })
      setFound(r.models)
      if (!r.models.length) notify('服务商没有返回模型列表，可以手动添加模型 ID')
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setDiscovering(false)
    }
  }

  const save = async () => {
    setBusy(true)
    try {
      await v3.saveProvider({ name: draft.name.trim().toLowerCase(), type: draft.type, base_url: baseUrl, api_key: draft.api_key.trim(), models })
      onSaved(draft.editing ? '已保存' : '已添加服务商。密钥存放在本机数据目录，接口只返回脱敏值')
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const keyPlaceholder = draft.has_key ? '已配置；留空表示不修改' : isPreset && pre?.key_env ? `输入 API 密钥，或留空使用环境变量 ${pre.key_env}` : '输入 API 密钥'

  return (
    <div className="prov-editor">
      {!draft.editing && (
        <div className="seg" role="tablist">
          <button role="tab" aria-selected={isPreset} onClick={() => presets[0] && setDraft(draftFromPreset(pre ?? presets[0], draft))}>第三方模型服务商</button>
          <button role="tab" aria-selected={!isPreset} onClick={() => setDraft({ ...draft, mode: 'custom', name: '', base_url: '', models: [''] })}>自定义模型 API</button>
        </div>
      )}
      {draft.editing && <h3 className="prov-editor-title">编辑 {pre ? presetLabel(pre) : draft.name}</h3>}

      {isPreset ? (
        <>
          {!draft.editing && <p className="prov-note">从内置目录选择 DeepSeek、OpenAI、Anthropic、Kimi 等服务商，填入 API 密钥即可使用。</p>}
          {!draft.editing && (
            <label className="prov-field">服务商
              <select value={draft.name} onChange={(e) => { const p = presets.find((x) => x.name === e.target.value); if (p) { setFound(null); setDraft(draftFromPreset(p, draft)) } }}>
                {presets.map((p) => <option key={p.name} value={p.name}>{presetLabel(p)}</option>)}
              </select>
            </label>
          )}
          <label className="prov-field">API 密钥
            <input type="password" autoComplete="off" value={draft.api_key} onChange={(e) => set({ api_key: e.target.value })} placeholder={keyPlaceholder} />
          </label>
          <details className="prov-more" open={!!draft.base_url || (!!pre && protocolOf(draft.type) !== protocolOf(pre.type))}>
            <summary>自定义设置</summary>
            <label className="prov-field">API 地址
              <input value={draft.base_url} onChange={(e) => set({ base_url: e.target.value })} placeholder={pre?.base_url || '服务商默认'} />
            </label>
            <ProtocolSelect value={draft.type} onChange={(type) => set({ type })} />
          </details>
        </>
      ) : (
        <>
          <label className="prov-field">服务商 ID
            <input value={draft.name} disabled={!!draft.editing} onChange={(e) => set({ name: e.target.value })} placeholder="acme-gateway" />
            <small className={draft.name && !nameOk ? 'bad' : undefined}>{taken ? '这个 ID 已被使用' : '小写字母或数字开头，可含 - 和 _；用途路由里用它引用'}</small>
          </label>
          <label className="prov-field">API 地址
            <input value={draft.base_url} onChange={(e) => set({ base_url: e.target.value })} placeholder="https://gateway.example/v1" />
          </label>
          <ProtocolSelect value={draft.type} onChange={(type) => set({ type })} />
          <label className="prov-field">API 密钥
            <input type="password" autoComplete="off" value={draft.api_key} onChange={(e) => set({ api_key: e.target.value })} placeholder={keyPlaceholder} />
          </label>
        </>
      )}

      <div className="prov-catalog">
        <div className="prov-catalog-head">
          <div><b>模型目录</b><small>第一个作为该服务商的默认模型</small></div>
          <button className="button small ghost" onClick={discover} disabled={discovering || !/^https?:\/\//.test(baseUrl)}>{discovering ? '获取中…' : '获取可用模型'}</button>
        </div>
        {draft.models.map((m, i) => (
          <div key={i} className="prov-model">
            <input value={m} onChange={(e) => set({ models: draft.models.map((x, j) => (j === i ? e.target.value : x)) })} placeholder="模型 ID，例如 deepseek-chat" />
            <button className="icon-btn" aria-label="移除" onClick={() => set({ models: draft.models.filter((_, j) => j !== i) })}>×</button>
          </div>
        ))}
        {!draft.models.length && <div className="prov-empty">还没有模型。获取可用模型，或手动添加模型 ID。</div>}
        {found && found.length > 0 && (
          <div className="prov-found">
            <small>服务商返回 {found.length} 个模型，点击加入：</small>
            <div className="chip-row">
              {found.map((m) => {
                const on = models.includes(m)
                return <button key={m} className={`chip${on ? ' on' : ''}`} onClick={() => set({ models: on ? draft.models.filter((x) => x.trim() !== m) : [...draft.models.filter((x) => x.trim()), m] })}>{m}</button>
              })}
            </div>
          </div>
        )}
        <button className="button small" onClick={() => set({ models: [...draft.models, ''] })}>＋ 添加模型</button>
      </div>

      <div className="prov-foot">
        <button className="button" onClick={onCancel}>取消</button>
        <button className="button primary" onClick={save} disabled={busy || !valid}>{busy ? '保存中…' : draft.editing ? '保存' : isPreset ? '保存' : '创建服务商'}</button>
      </div>
    </div>
  )
}

function TestSection({ allModels, defaultTarget }: { allModels: string[]; defaultTarget: string }) {
  const [target, setTarget] = useState(defaultTarget)
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<{ ok: boolean; reply?: string; error?: string; model?: string; provider?: string } | null>(null)
  const run = async () => {
    setTesting(true)
    setResult(null)
    try {
      setResult(await api.testProvider(target, '用一句话介绍你自己，并说明你是哪个模型。'))
    } catch (e) {
      setResult({ ok: false, error: (e as Error).message })
    } finally {
      setTesting(false)
    }
  }
  return (
    <section className="set-section">
      <div className="set-section-head"><h3>连通性测试</h3></div>
      <div className="set-row">
        <div><b>测试目标</b><small>真实调用一次模型，验证密钥、网络与模型名</small></div>
        <div className="set-inline">
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">选择模型</option>
            {allModels.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <button className="button" onClick={run} disabled={testing || !target}>{testing ? '调用中…' : '测试'}</button>
        </div>
      </div>
      {result && (
        <Callout tone={result.ok ? 'info' : 'fail'} title={result.ok ? `调用成功：${result.provider}/${result.model}` : '调用失败'}>
          <pre className="pre-wrap">{result.ok ? result.reply : result.error}</pre>
        </Callout>
      )}
    </section>
  )
}

interface SearchStatus { configured: boolean; provider?: string | null; key_masked?: string }

/** 联网搜索：AI 研究室与团队角色的 web_search / fetch_page 工具用它。 */
function SearchSection({ notify }: { notify: PageProps['notify'] }) {
  const [st, setSt] = useState<SearchStatus | null>(null)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => { get<SearchStatus>('/api/config/search').then(setSt).catch(() => undefined) }, [])
  const save = async (value: string) => {
    setBusy(true)
    try {
      setSt(await request<SearchStatus>('/api/config/search', { method: 'PUT', body: JSON.stringify({ provider: 'tavily', api_key: value }) }))
      setKey('')
      notify(value ? '已保存联网搜索密钥' : '已移除联网搜索密钥')
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="set-section">
      <div className="set-section-head"><h3>联网搜索</h3></div>
      <div className="set-row">
        <div>
          <b>Tavily <i className={`prov-dot${st?.configured ? ' ok' : ''}`} /></b>
          <small>{st?.configured ? `已配置 · ${st.key_masked}` : 'AI 研究室与团队角色用它联网搜索、读取网页。'}<a href="https://tavily.com" target="_blank" rel="noreferrer">申请密钥</a></small>
        </div>
        <div className="set-inline">
          <input type="password" placeholder="tvly-…" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" />
          <button className="button" disabled={busy || !key.trim()} onClick={() => save(key.trim())}>保存</button>
          {st?.configured && <button className="button ghost" disabled={busy} onClick={() => save('')}>移除</button>}
        </div>
      </div>
    </section>
  )
}
