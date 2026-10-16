import { useCallback, useEffect, useState } from 'react'
import { api, type Preset } from '../api'
import { v3, type LimitRule, type ModelConfigView, type ModelSetting, type ModelUsage, type ProviderScope, type ProviderView } from '../api2'
import { LimitBars, LimitEditor, UsageSummary } from '../components/modelUsage'
import { Callout, Heading, Loading } from '../components/ui'
import { get, request } from '../http'
import { store } from '../components/prefs'
import { LAB_PREF_KEYS } from '../apiLab'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

/**
 * 设置 · 模型。平台不内置服务商，模型只有两个来源：
 * - 工作区共享：管理员登记，成员（被允许时）的调用走管理员的 API；
 * - 我的：成员自己登记，只有自己可见、可用。
 * 每个服务商可填写价格（每百万 token，可选美元 + 汇率）用于费用折算，并设置按 token / 费用、按时间段的限额。
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
  scope: ProviderScope
  editing: string | null // 正在编辑的服务商名；null 表示新建
  name: string
  label: string
  type: string
  base_url: string
  api_key: string
  models: ModelRow[]
  has_key: boolean
  currency: 'CNY' | 'USD'
  fx_rate: string
  limits: LimitRule[]
}

/** 模型目录的一行：id 是目录 ID（路由里引用它），其余为可选设置，表单里都按字符串编辑。 */
interface ModelRow {
  id: string
  label: string
  request_model: string
  context_window: string
  effort: string
  inputs: string[]
  price_in: string
  price_out: string
  price_cache: string
  price_cache_write: string
}

const EFFORTS: [string, string][] = [['', '跟随对话'], ['low', '低'], ['medium', '中'], ['high', '高'], ['max', '极高']]
const INPUTS: [string, string][] = [['text', '文本'], ['image', '图片'], ['file', '文件'], ['audio', '音频'], ['video', '视频']]

const rowOf = (id: string, st?: ModelSetting): ModelRow => ({
  id, label: st?.label ?? '', request_model: st?.request_model ?? '',
  context_window: st?.context_window ? String(st.context_window) : '', effort: st?.effort ?? '', inputs: st?.inputs?.length ? st.inputs : ['text'],
  price_in: st?.price_in ?? '', price_out: st?.price_out ?? '',
  price_cache: st?.price_cache ?? '', price_cache_write: st?.price_cache_write ?? '',
})

const settingOf = (r: ModelRow): ModelSetting => ({
  label: r.label.trim(), request_model: r.request_model.trim(),
  context_window: Number(r.context_window) > 0 ? Math.round(Number(r.context_window)) : null,
  effort: r.effort as ModelSetting['effort'], inputs: r.inputs, price_in: r.price_in.trim(), price_out: r.price_out.trim(),
  price_cache: r.price_cache.trim(), price_cache_write: r.price_cache_write.trim(),
})

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

const blank = (scope: ProviderScope): Draft => ({
  mode: 'custom', scope, editing: null, name: '', label: '', type: 'openai_compatible', base_url: '', api_key: '',
  models: [rowOf('')], has_key: false, currency: 'CNY', fx_rate: '', limits: [],
})

function draftFromPreset(p: Preset, scope: ProviderScope, prev?: Draft): Draft {
  return {
    ...blank(scope), mode: 'preset', name: p.name, type: p.type === 'anthropic' ? 'anthropic' : 'openai_compatible',
    api_key: prev?.api_key ?? '', models: p.models.filter(Boolean).map((m) => rowOf(m)),
    currency: p.name === 'openai' || p.name === 'anthropic' ? 'USD' : 'CNY',
  }
}

export default function ModelConfigPage({ notify, onNavigate }: PageProps) {
  const [config, setConfig] = useState<ModelConfigView | null>(null)
  const [presets, setPresets] = useState<Preset[]>([])
  const [usage, setUsage] = useState<ModelUsage | null>(null)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [layer, setLayer] = useState<ProviderScope>('personal')
  const [routing, setRouting] = useState<Record<string, string>>({})
  const [fallback, setFallback] = useState<string[]>([])
  const [retries, setRetries] = useState('')   // '' = 沿用上一层（默认 2 次）

  const loadRouting = useCallback((c: ModelConfigView, which: ProviderScope) => {
    const src = c.routing_layers[which === 'workspace' ? 'workspace' : 'personal'] ?? {}
    const r: Record<string, string> = {}
    for (const [k] of ROLES) r[k] = String(src[k] ?? '')
    setRouting(r)
    setFallback(Array.isArray(src.fallback) ? (src.fallback as string[]) : [])
    setRetries(typeof src.retries === 'number' ? String(src.retries) : '')
  }, [])

  const refresh = useCallback(async () => {
    const [c, p, u] = await Promise.all([v3.config(), api.presets(), v3.modelUsage(30, 'me').catch(() => null)])
    // 前端更新了、后端服务还是旧进程时 /api/config 没有 me / routing_layers：给出能看懂的提示而不是白屏报错
    if (!c.me || !c.routing_layers) throw new Error('后端服务还是旧版本（/api/config 缺少成员权限与路由信息）。请重启 quant_agent 服务（serve 与 worker）后刷新页面。')
    setConfig(c)
    setPresets(p.presets)
    setUsage(u)
    const which: ProviderScope = c.me.is_ws_admin ? 'workspace' : 'personal'
    setLayer(which)
    loadRouting(c, which)
  }, [loadRouting])

  useEffect(() => { refresh().catch((e: Error) => setError(e.message)) }, [refresh])

  if (error) return <><Heading title="模型" /><Callout tone="fail" title="加载失败">{error}</Callout></>
  if (!config) return <><Heading title="模型" /><Loading /></>

  const me = config.me
  const shared = config.providers.filter((p) => p.scope === 'workspace')
  const mine = config.providers.filter((p) => p.scope === 'personal')
  const allModels = config.providers.flatMap((p) => p.models.filter(Boolean).map((m) => `${p.name}/${m}`))
  const labelOf = (target: string) => {
    const [prov, ...rest] = target.split('/')
    const label = config.providers.find((p) => p.name === prov)?.model_settings?.[rest.join('/')]?.label
    return label ? `${target}（${label}）` : target
  }
  const keyed = config.providers.filter((p) => p.has_key)
  const presetOf = (name: string) => presets.find((p) => p.name === name)
  const effective = config.routing

  const startNew = (scope: ProviderScope) => setDraft(presets[0] ? draftFromPreset(presets[0], scope) : blank(scope))

  const edit = (p: ProviderView) => {
    const pre = presetOf(p.name)
    setDraft({
      mode: pre ? 'preset' : 'custom', scope: p.scope, editing: p.name, name: p.name, label: p.label ?? '',
      type: protocolOf(p.type),
      base_url: pre && p.base_url === pre.base_url ? '' : p.base_url, api_key: '', has_key: p.has_key,
      models: p.models.filter(Boolean).map((m) => rowOf(m, p.model_settings?.[m])),
      currency: p.currency ?? 'CNY', fx_rate: p.fx_rate && p.currency === 'USD' ? p.fx_rate : '', limits: p.limits ?? [],
    })
  }

  const remove = async (p: ProviderView) => {
    const who = p.scope === 'workspace' ? '工作区共享服务商（所有成员都将不能再用它）' : '你的个人服务商'
    if (!await appConfirm(`删除${who}「${p.label || p.name}」与它的密钥？`)) return
    try {
      await v3.deleteProvider(p.name, p.scope)
      await refresh()
      notify('已删除')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const saveRouting = async () => {
    try {
      await v3.saveRouting({ default: routing.default, code_gen: routing.code_gen, reasoning: routing.reasoning, report: routing.report, fallback, retries: retries === '' ? null : Number(retries) }, layer)
      await refresh()
      notify(layer === 'workspace' ? '工作区默认路由已保存' : '你的路由已保存')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const card = (p: ProviderView, i: number) => {
    const pre = presetOf(p.name)
    const priced = Object.values(p.model_settings ?? {}).some((s) => s.price_in || s.price_out)
    return (
      <div key={`${p.scope}-${p.name}`} className="prov-card anim-rise" style={{ animationDelay: `${i * 35}ms` }}>
        <div className="prov-main">
          <div className="prov-name">
            <b>{p.label || (pre ? presetLabel(pre) : p.name)}</b>
            <span className={`scope-tag ${p.scope}`}>{p.scope === 'workspace' ? '工作区共享' : '我的'}</span>
            <i className={`prov-dot${p.has_key ? ' ok' : ''}`} title={p.has_key ? '已配置密钥' : '未配置密钥'} />
          </div>
          <small>
            {p.editable ? (p.has_key ? (p.key_masked || p.key_source) : '未配置密钥') : '由管理员提供 · 调用走工作区的 API'}
            {' · '}{p.models.filter(Boolean).length} 个模型{priced ? ` · 已设价格（${p.currency === 'USD' ? `USD × ${p.fx_rate}` : 'CNY'}）` : ''}
          </small>
          <LimitBars items={p.limit_status ?? []} compact />
        </div>
        <div className="prov-actions">
          {p.editable ? (
            <>
              <button className="button small" onClick={() => edit(p)}>{p.has_key ? '编辑' : '填写密钥'}</button>
              <button className="button small ghost danger-text" onClick={() => remove(p)}>删除</button>
            </>
          ) : <span className="prov-ro">只读</span>}
        </div>
      </div>
    )
  }

  return (
    <>
      <Heading
        title="模型"
        description={me.is_ws_admin
          ? '平台不内置模型。你登记的「工作区共享」服务商会提供给成员使用（调用走你的 API，可按成员设置权限与限额）；也可以添加只给自己用的服务商。'
          : '平台不内置模型。使用管理员共享给工作区的模型，或添加你自己的服务商。没有可用模型时，AI 功能走模板与规则降级。'}
      />

      {draft ? (
        <ProviderEditor
          draft={draft} setDraft={setDraft} presets={presets} existing={config.providers} isAdmin={me.is_ws_admin}
          onCancel={() => setDraft(null)}
          onSaved={async (msg) => { setDraft(null); await refresh(); notify(msg) }}
          notify={notify}
        />
      ) : (
        <div className="prov-groups anim-fade">
          {(me.is_ws_admin || shared.length > 0 || me.use_workspace_models) && (
            <div className="prov-group">
              <div className="prov-group-head">
                <h3>工作区共享</h3>
                <small>{me.is_ws_admin ? '成员调用这些模型时使用你的密钥；在「成员与邀请」里设置谁可以用、限额多少' : me.use_workspace_models ? '管理员提供，调用走工作区的 API' : '管理员没有开放给你'}</small>
              </div>
              <div className="prov-list">
                {shared.map(card)}
                {!shared.length && <div className="prov-empty">{me.is_ws_admin ? '还没有共享模型。添加一个，成员就能直接使用。' : '管理员还没有共享模型。'}</div>}
                {me.is_ws_admin && <button className="prov-add" onClick={() => startNew('workspace')}>＋ 添加工作区共享服务商</button>}
              </div>
            </div>
          )}
          {(me.allow_personal_models || mine.length > 0) && (
            <div className="prov-group">
              <div className="prov-group-head">
                <h3>我的</h3>
                <small>只有你自己可见、可用，费用记在你名下；同名时优先于工作区共享</small>
              </div>
              <div className="prov-list">
                {mine.map(card)}
                {me.allow_personal_models && <button className="prov-add" onClick={() => startNew('personal')}>＋ 添加我的服务商</button>}
              </div>
            </div>
          )}
          {!me.allow_personal_models && !shared.length && (
            <Callout tone="warn">管理员没有开放共享模型，也没有允许添加个人服务商。请联系工作区管理员。</Callout>
          )}
        </div>
      )}

      <section className="set-section">
        <div className="set-section-head">
          <h3>用途路由</h3>
          <div className="set-inline">
            {me.is_ws_admin && (
              <div className="seg seg-sm" role="tablist" aria-label="编辑哪一层路由">
                <button role="tab" aria-selected={layer === 'workspace'} onClick={() => { setLayer('workspace'); loadRouting(config, 'workspace') }}>工作区默认</button>
                <button role="tab" aria-selected={layer === 'personal'} onClick={() => { setLayer('personal'); loadRouting(config, 'personal') }}>我的覆盖</button>
              </div>
            )}
            <button className="button small primary" onClick={saveRouting}>保存</button>
          </div>
        </div>
        <p className="set-section-note">
          {layer === 'workspace' ? '工作区所有成员的默认路由；成员可以在「我的覆盖」里改成自己的模型。' : '只影响你自己；留空的用途使用工作区默认。'}
          主模型失败、没有密钥或超出限额时，按降级链依次尝试；整条链都失败则走模板降级。
        </p>
        {ROLES.map(([k, label, hint]) => (
          <div key={k} className="set-row">
            <div><b>{label}</b><small>{hint}</small></div>
            <select value={routing[k] ?? ''} onChange={(e) => setRouting({ ...routing, [k]: e.target.value })}>
              <option value="">{layer === 'personal' && effective[k] && !routing[k] ? `（工作区默认：${effective[k]}）` : '（使用默认）'}</option>
              {allModels.map((m) => <option key={m} value={m}>{labelOf(m)}{keyed.some((p) => m.startsWith(p.name + '/')) ? '' : '（无密钥）'}</option>)}
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
            {!allModels.length && <small className="muted">先添加一个服务商</small>}
          </div>
        </div>
        <div className="set-row">
          <div>
            <b>失败自动重试</b>
            <small>同一个模型遇到限流、服务端错误、超时或断流时，等一会儿（1、2、4 秒…，或按服务商要求的时间）再试；仍失败才换降级链里的下一个</small>
          </div>
          <select value={retries} onChange={(e) => setRetries(e.target.value)} aria-label="失败自动重试次数">
            <option value="">{layer === 'personal' && typeof effective.retries === 'number' ? `（工作区默认：${effective.retries} 次）` : '（默认：2 次）'}</option>
            {[0, 1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n === 0 ? '不重试' : `${n} 次`}</option>)}
          </select>
        </div>
      </section>

      {usage && (
        <section className="set-section">
          <div className="set-section-head">
            <h3>我的用量</h3>
            <button className="button small ghost" onClick={() => onNavigate('settings/quota')}>额度与用量 →</button>
          </div>
          <p className="set-section-note">近 30 天，按服务商上填写的价格折算成人民币。{usage.my_limits.length ? '' : '你使用工作区共享模型目前不限额。'}</p>
          <LimitBars items={usage.my_limits} />
          <UsageSummary data={usage} />
        </section>
      )}

      <ChatPrefs notify={notify} />
      <TestSection allModels={allModels} defaultTarget={keyed[0] ? `${keyed[0].name}/${keyed[0].models[0]}` : ''} />
      <SearchSection notify={notify} />
      <p className="set-foot">
        密钥加密存放在本机数据目录，接口只返回脱敏值。大模型密钥与行情、模拟盘、实盘密钥互不复用；AI 只产出代码草案与分析，不能下单。
      </p>
    </>
  )
}

/** 内嵌编辑卡：第三方预设只需选服务商、填密钥；自定义 API 填完整连接信息；下面是模型目录、计费与限额。 */
function ProviderEditor({ draft, setDraft, presets, existing, isAdmin, onCancel, onSaved, notify }: {
  draft: Draft; setDraft: (d: Draft) => void; presets: Preset[]; existing: ProviderView[]; isAdmin: boolean
  onCancel: () => void; onSaved: (msg: string) => void; notify: PageProps['notify']
}) {
  const [busy, setBusy] = useState(false)
  const [discovering, setDiscovering] = useState(false)
  const [found, setFound] = useState<string[] | null>(null)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState<number | null>(null)
  const pre = presets.find((p) => p.name === draft.name)
  const isPreset = draft.mode === 'preset'
  const baseUrl = draft.base_url.trim() || (isPreset ? pre?.base_url ?? '' : '')
  const models = draft.models.map((m) => m.id.trim()).filter(Boolean)
  const nameOk = /^[a-z0-9][a-z0-9_-]{0,30}$/.test(draft.name.trim().toLowerCase())
  const taken = !draft.editing && existing.some((p) => p.name === draft.name.trim().toLowerCase() && p.editable)
  const valid = nameOk && /^https?:\/\//.test(baseUrl) && models.length > 0 && !taken
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch })
  const setRow = (i: number, patch: Partial<ModelRow>) => set({ models: draft.models.map((r, j) => (j === i ? { ...r, ...patch } : r)) })
  const q = query.trim().toLowerCase()
  const shown = found?.filter((m) => !q || m.toLowerCase().includes(q)) ?? []
  const unit = draft.currency === 'USD' ? '$' : '¥'

  const discover = async () => {
    setDiscovering(true)
    try {
      const r = await v3.discoverModels({ name: draft.editing ?? draft.name, type: draft.type, base_url: baseUrl, api_key: draft.api_key.trim() })
      setFound(r.models)
      setQuery('')
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
      const model_settings = Object.fromEntries(draft.models.filter((r) => r.id.trim()).map((r) => [r.id.trim(), settingOf(r)]))
      await v3.saveProvider({
        name: draft.name.trim().toLowerCase(), scope: draft.scope, label: draft.label.trim(), type: draft.type, base_url: baseUrl,
        api_key: draft.api_key.trim(), models, model_settings, currency: draft.currency, fx_rate: draft.fx_rate.trim(),
        limits: draft.limits.filter((r) => Number(r.limit) > 0),
      })
      onSaved(draft.editing ? '已保存' : draft.scope === 'workspace' ? '已添加工作区共享服务商，成员现在可以使用了' : '已添加你的服务商')
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const keyPlaceholder = draft.has_key ? '已配置；留空表示不修改' : '输入 API 密钥'

  return (
    <div className="prov-editor anim-pop">
      <div className="prov-editor-top">
        <h3 className="prov-editor-title">
          {draft.editing ? `编辑 ${draft.label || (pre ? presetLabel(pre) : draft.name)}` : '添加模型服务商'}
        </h3>
        {!draft.editing && isAdmin ? (
          <div className="seg seg-sm" role="tablist" aria-label="归属">
            <button role="tab" aria-selected={draft.scope === 'workspace'} onClick={() => set({ scope: 'workspace' })}>工作区共享</button>
            <button role="tab" aria-selected={draft.scope === 'personal'} onClick={() => set({ scope: 'personal' })}>只给我用</button>
          </div>
        ) : <span className={`scope-tag ${draft.scope}`}>{draft.scope === 'workspace' ? '工作区共享' : '我的'}</span>}
      </div>
      {!draft.editing && (
        <div className="seg" role="tablist">
          <button role="tab" aria-selected={isPreset} onClick={() => presets[0] && setDraft(draftFromPreset(pre ?? presets[0], draft.scope, draft))}>第三方模型服务商</button>
          <button role="tab" aria-selected={!isPreset} onClick={() => setDraft({ ...blank(draft.scope), api_key: draft.api_key })}>自定义模型 API</button>
        </div>
      )}

      {isPreset ? (
        <>
          {!draft.editing && <p className="prov-note">从模板选择 DeepSeek、OpenAI、Anthropic、Kimi 等服务商，填入 API 密钥即可使用。</p>}
          {!draft.editing && (
            <label className="prov-field">服务商
              <select value={draft.name} onChange={(e) => { const p = presets.find((x) => x.name === e.target.value); if (p) { setFound(null); setDraft(draftFromPreset(p, draft.scope, draft)) } }}>
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
          <div className="prov-pair">
            <label className="prov-field">服务商 ID
              <input value={draft.name} disabled={!!draft.editing} onChange={(e) => set({ name: e.target.value })} placeholder="acme-gateway" />
              <small className={draft.name && !nameOk ? 'bad' : undefined}>{taken ? '这个 ID 已被使用' : '小写字母或数字开头，可含 - 和 _；路由里用它引用'}</small>
            </label>
            <label className="prov-field">显示名
              <input value={draft.label} onChange={(e) => set({ label: e.target.value })} placeholder="可选，例如 公司网关" maxLength={40} />
            </label>
          </div>
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
          <div><b>模型目录</b><small>第一个作为该服务商的默认模型；展开设置可填显示名、实际请求模型与价格</small></div>
          <button className="button small ghost" onClick={discover} disabled={discovering || !/^https?:\/\//.test(baseUrl)}>{discovering ? '获取中…' : '获取可用模型'}</button>
        </div>
        {draft.models.map((r, i) => (
          <div key={i} className={`prov-model-card${open === i ? ' open' : ''}`}>
            <div className="prov-model">
              <input value={r.id} onChange={(e) => setRow(i, { id: e.target.value })} placeholder="模型 ID，例如 deepseek-chat" aria-label="模型 ID" />
              {(r.price_in || r.price_out) && open !== i && (
                <span className="prov-model-label" title="每百万 token：输入 / 缓存命中 / 输出">
                  {unit}{r.price_in || 0}{r.price_cache ? ` · 缓存 ${unit}${r.price_cache}` : ''} / {unit}{r.price_out || 0}
                </span>
              )}
              {r.label.trim() && open !== i && <span className="prov-model-label" title="菜单显示名">{r.label}</span>}
              <button className={`icon-btn${open === i ? ' on' : ''}`} aria-label="模型设置" aria-expanded={open === i}
                title="显示名、实际请求模型、上下文窗口、默认思考等级、输入类型、价格" onClick={() => setOpen(open === i ? null : i)}>
                <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.4">
                  <path d="M2 4h7M12 4h2M2 12h2M7 12h7" /><circle cx="10.5" cy="4" r="1.6" /><circle cx="5.5" cy="12" r="1.6" />
                </svg>
              </button>
              <button className="icon-btn" aria-label="移除" onClick={() => { set({ models: draft.models.filter((_, j) => j !== i) }); setOpen(null) }}>×</button>
            </div>
            {open === i && (
              <div className="prov-model-set anim-expand">
                <label className="prov-field">菜单显示名
                  <input value={r.label} onChange={(e) => setRow(i, { label: e.target.value })} placeholder={r.id || '留空显示模型 ID'} maxLength={60} />
                </label>
                <label className="prov-field">实际请求模型
                  <input value={r.request_model} onChange={(e) => setRow(i, { request_model: e.target.value })}
                    placeholder={r.id ? `留空即 ${r.id}` : '留空与模型 ID 相同'} list={found?.length ? 'prov-found-ids' : undefined} />
                  <small>路由与对话里仍按模型 ID 引用，换模型只改这里</small>
                </label>
                <label className="prov-field">上下文窗口（tokens）
                  <input type="number" min={1024} step={1024} inputMode="numeric" value={r.context_window}
                    onChange={(e) => setRow(i, { context_window: e.target.value })} placeholder="留空自动估算，例如 128000" />
                </label>
                <label className="prov-field">默认思考等级
                  <select value={r.effort} onChange={(e) => setRow(i, { effort: e.target.value })}>
                    {EFFORTS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
                  </select>
                </label>
                <label className="prov-field">输入价格（{unit} / 百万 token）
                  <input type="number" min={0} step="0.01" inputMode="decimal" value={r.price_in} onChange={(e) => setRow(i, { price_in: e.target.value })} placeholder="例如 2" />
                </label>
                <label className="prov-field">输出价格（{unit} / 百万 token）
                  <input type="number" min={0} step="0.01" inputMode="decimal" value={r.price_out} onChange={(e) => setRow(i, { price_out: e.target.value })} placeholder="例如 8" />
                </label>
                <label className="prov-field">缓存命中输入（{unit} / 百万 token）
                  <input type="number" min={0} step="0.001" inputMode="decimal" value={r.price_cache} onChange={(e) => setRow(i, { price_cache: e.target.value })}
                    placeholder={r.price_in ? `留空按输入价 ${r.price_in}` : '例如 0.5'} />
                  <small>命中提示缓存的那部分输入；多数服务商是输入价的 10%–50%</small>
                </label>
                <label className="prov-field">缓存写入（{unit} / 百万 token）
                  <input type="number" min={0} step="0.001" inputMode="decimal" value={r.price_cache_write} onChange={(e) => setRow(i, { price_cache_write: e.target.value })}
                    placeholder={r.price_in ? `留空按输入价 ${r.price_in}` : '例如 2.5'} />
                  <small>Anthropic 等对写入缓存单独计价；没有就留空</small>
                </label>
                <div className="prov-field prov-wide">输入类型
                  <div className="prov-checks">
                    {INPUTS.map(([k, label]) => (
                      <label key={k} className="prov-check">
                        <input type="checkbox" checked={r.inputs.includes(k)} disabled={k === 'text'}
                          onChange={(e) => setRow(i, { inputs: e.target.checked ? [...r.inputs, k] : r.inputs.filter((x) => x !== k) })} />
                        {label}
                      </label>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        ))}
        {!draft.models.length && <div className="prov-empty">还没有模型。获取可用模型，或手动添加模型 ID。</div>}
        {found && found.length > 0 && (
          <div className="prov-found anim-fade">
            <div className="prov-found-head">
              <small>服务商返回 {found.length} 个模型{q ? `，匹配 ${shown.length} 个` : ''}，点击加入：</small>
              <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索模型名" aria-label="搜索模型名" />
            </div>
            <div className="chip-row">
              {shown.map((m) => {
                const on = models.includes(m)
                return <button key={m} className={`chip${on ? ' on' : ''}`} onClick={() => set({ models: on ? draft.models.filter((x) => x.id.trim() !== m) : [...draft.models.filter((x) => x.id.trim()), rowOf(m)] })}>{m}</button>
              })}
              {!shown.length && <small className="prov-found-none">没有匹配“{query}”的模型</small>}
            </div>
            <datalist id="prov-found-ids">{found.map((m) => <option key={m} value={m} />)}</datalist>
          </div>
        )}
        <button className="button small" onClick={() => { set({ models: [...draft.models, rowOf('')] }); setOpen(draft.models.length) }}>＋ 添加模型</button>
      </div>

      <div className="prov-catalog">
        <div className="prov-catalog-head">
          <div><b>计费与限额</b><small>价格在上方各模型的设置里填；这里设币种换算与该服务商的总限额</small></div>
        </div>
        <div className="prov-pair">
          <label className="prov-field">价格币种
            <select value={draft.currency} onChange={(e) => set({ currency: e.target.value as 'CNY' | 'USD' })}>
              <option value="CNY">人民币（¥）</option>
              <option value="USD">美元（$），按汇率折算</option>
            </select>
          </label>
          {draft.currency === 'USD' && (
            <label className="prov-field anim-in">汇率（1 美元 = ? 元）
              <input type="number" min={0} step="0.01" inputMode="decimal" value={draft.fx_rate} onChange={(e) => set({ fx_rate: e.target.value })} placeholder="7.2" />
            </label>
          )}
        </div>
        <div className="prov-field">限额
          <small>{draft.scope === 'workspace' ? '整个工作区在这个服务商上的用量上限（所有成员合计）；单个成员的限额在「成员与邀请」里设置' : '你自己在这个服务商上的用量上限'}；超额后该服务商被跳过，降级链里的下一个模型接着用。</small>
          <LimitEditor rules={draft.limits} onChange={(limits) => set({ limits })} />
        </div>
      </div>

      <div className="prov-foot">
        <button className="button" onClick={onCancel}>取消</button>
        <button className="button primary" onClick={save} disabled={busy || !valid}>{busy ? '保存中…' : draft.editing ? '保存' : '创建服务商'}</button>
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
        <div><b>测试目标</b><small>真实调用一次模型，验证密钥、网络与模型名（计入用量）</small></div>
        <div className="set-inline">
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">选择模型</option>
            {allModels.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <button className="button" onClick={run} disabled={testing || !target}>{testing ? '调用中…' : '测试'}</button>
        </div>
      </div>
      {testing && <div className="shimmer-line" aria-hidden />}
      {result && (
        <div className="anim-rise">
          <Callout tone={result.ok ? 'info' : 'fail'} title={result.ok ? `调用成功：${result.provider}/${result.model}` : '调用失败'}>
            <pre className="pre-wrap">{result.ok ? result.reply : result.error}</pre>
          </Callout>
        </div>
      )}
    </section>
  )
}

interface SearchProvider { id: string; label: string; signup: string; extra?: string; configured: boolean; key_masked: string; extra_value?: string | null }
interface SearchSource { id: string; label: string; url: string; kind: 'searxng' | 'rss'; added_by: 'admin' | 'agent' }
interface SearchStatus {
  configured: boolean; provider?: string | null; key_masked?: string
  providers?: SearchProvider[]; custom?: SearchSource[]; free_sources?: { id: string; label: string }[]
}

const PROVIDER_HINT: Record<string, { ph: string; note: string; extraLabel?: string }> = {
  tavily: { ph: 'tvly-…', note: '为 LLM 设计的搜索 API，返回摘要片段，适合研究问答' },
  serper: { ph: 'Serper API Key', note: 'Google 搜索结果（经 Serper 代理），source=google 时优先使用' },
  google_cse: { ph: 'Google API Key', note: 'Google 官方可编程搜索：还需要搜索引擎 ID（cx）', extraLabel: '搜索引擎 ID（cx）' },
  brave: { ph: 'Brave Subscription Token', note: 'Brave 独立索引，英文资料质量好' },
}

/** 联网搜索：AI 研究室与团队角色的 web_search / fetch_page 工具用它（部署级，部署管理员可改）。 */
function SearchSection({ notify }: { notify: PageProps['notify'] }) {
  const [st, setSt] = useState<SearchStatus | null>(null)
  const [keys, setKeys] = useState<Record<string, string>>({})
  const [extra, setExtra] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')
  const [src, setSrc] = useState<{ label: string; url: string; kind: 'searxng' | 'rss' }>({ label: '', url: '', kind: 'searxng' })
  useEffect(() => { get<SearchStatus>('/api/config/search').then(setSt).catch(() => undefined) }, [])
  const run = async (tag: string, fn: () => Promise<SearchStatus>, ok: string) => {
    setBusy(tag)
    try { setSt(await fn()); notify(ok) } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }
  const saveKey = (p: SearchProvider, value: string) => run(p.id, () => request<SearchStatus>('/api/config/search', {
    method: 'PUT', body: JSON.stringify({ provider: p.id, api_key: value, ...(p.extra ? { extra: value ? (extra[p.id] ?? p.extra_value ?? '') : '' } : {}) }),
  }), value ? `已保存 ${p.label} 密钥` : `已移除 ${p.label} 密钥`).then(() => { setKeys((k) => ({ ...k, [p.id]: '' })) })
  const addSource = () => run('src', () => request<SearchStatus>('/api/config/search/sources', { method: 'POST', body: JSON.stringify(src) }), '已添加搜索来源')
    .then(() => setSrc({ label: '', url: '', kind: src.kind }))
  const providers = st?.providers ?? []
  return (
    <section className="set-section">
      <div className="set-section-head"><h3>联网搜索</h3></div>
      <p className="hint" style={{ margin: '0 0 8px' }}>
        AI 研究室与团队角色用它联网搜索、读取网页，并在回答里引用来源。没有任何密钥时自动使用免密钥来源
        （{(st?.free_sources ?? []).map((f) => f.label).join('、') || '必应、百度、DuckDuckGo、Google 新闻、百度新闻'}）；
        选「Google」但没有配置 Google 密钥时，会如实说明并改用免密钥来源。
      </p>
      {providers.map((p) => {
        const hint = PROVIDER_HINT[p.id] || { ph: 'API Key', note: '' }
        return (
          <div className="set-row" key={p.id}>
            <div>
              <b>{p.label} <i className={`prov-dot${p.configured ? ' ok' : ''}`} /></b>
              <small>{p.configured ? `已配置 · ${p.key_masked}${p.extra && p.extra_value ? ` · cx ${p.extra_value}` : ''}` : hint.note} <a href={p.signup} target="_blank" rel="noreferrer">申请密钥</a></small>
            </div>
            <div className="set-inline">
              <input type="password" placeholder={hint.ph} value={keys[p.id] ?? ''} autoComplete="off" aria-label={`${p.label} 密钥`}
                onChange={(e) => setKeys({ ...keys, [p.id]: e.target.value })} />
              {p.extra && (
                <input placeholder={hint.extraLabel || p.extra} value={extra[p.id] ?? p.extra_value ?? ''} aria-label={hint.extraLabel || p.extra}
                  onChange={(e) => setExtra({ ...extra, [p.id]: e.target.value })} style={{ width: 150 }} />
              )}
              <button className="button" disabled={!!busy || !(keys[p.id] ?? '').trim() || (!!p.extra && !(extra[p.id] ?? p.extra_value ?? '').trim())}
                onClick={() => saveKey(p, (keys[p.id] ?? '').trim())}>{busy === p.id ? '保存中…' : '保存'}</button>
              {p.configured && <button className="button ghost" disabled={!!busy} onClick={() => saveKey(p, '')}>移除</button>}
            </div>
          </div>
        )
      })}
      <div className="set-row set-row-stack">
        <div>
          <b>自定义来源</b>
          <small>SearXNG 实例根地址（需在 settings.yml 开启 json 输出），或带 {'{q}'} 占位的 RSS 搜索地址。AI 也可以在对话里用 add_search_source 添加公网来源；web_search 的 source=custom / all 会用到。</small>
        </div>
        <div className="set-inline">
          <select value={src.kind} onChange={(e) => setSrc({ ...src, kind: e.target.value as 'searxng' | 'rss' })} aria-label="来源类型">
            <option value="searxng">SearXNG</option>
            <option value="rss">RSS 搜索</option>
          </select>
          <input placeholder="名称" value={src.label} onChange={(e) => setSrc({ ...src, label: e.target.value })} style={{ width: 110 }} aria-label="来源名称" />
          <input placeholder={src.kind === 'rss' ? 'https://example.com/rss?q={q}' : 'https://searx.example.org'} value={src.url} aria-label="来源地址"
            onChange={(e) => setSrc({ ...src, url: e.target.value })} style={{ width: 230 }} />
          <button className="button" disabled={!!busy || src.url.trim().length < 8} onClick={addSource}>{busy === 'src' ? '添加中…' : '添加'}</button>
        </div>
      </div>
      {(st?.custom ?? []).length > 0 && (
        <ul className="search-sources stagger">
          {(st?.custom ?? []).map((c) => (
            <li key={c.id}>
              <span className="tag mono">{c.kind === 'rss' ? 'RSS' : 'SearXNG'}</span>
              <b>{c.label}</b>
              <small className="muted" title={c.url}>{c.url}</small>
              <small className="muted">{c.added_by === 'agent' ? 'AI 添加' : '管理员添加'} · id {c.id}</small>
              <button className="button small ghost" disabled={!!busy} onClick={() => run(`del${c.id}`, () => request<SearchStatus>(`/api/config/search/sources/${c.id}`, { method: 'DELETE' }), '已删除来源')}>删除</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}


/** 对话偏好（本机）：AI 研究室里，AI 每次回答末尾附 2-3 条简短的后续建议，点一下就能接着问。 */
function ChatPrefs({ notify }: { notify: PageProps['notify'] }) {
  const [on, setOn] = useState(() => store.get('qa.lab.suggest', '1') === '1')
  const [team, setTeam] = useState(() => store.get(LAB_PREF_KEYS.teamCtx, '1') === '1')
  const [maxTools, setMaxTools] = useState(() => store.get(LAB_PREF_KEYS.maxTools, '1000'))
  const toggle = () => {
    const v = !on
    setOn(v)
    store.set('qa.lab.suggest', v ? '1' : '0')
    notify(v ? '已开启：AI 回答后会给出简短建议' : '已关闭 AI 建议')
  }
  return (
    <section className="set-section">
      <div className="set-section-head"><h3>对话偏好</h3></div>
      <div className="set-row">
        <div><b>AI 生成简短建议</b><small>在 AI 研究室里，每次回答末尾给出 2-3 条你可能想接着说的话（不额外调用模型，随回答一起生成）</small></div>
        <button type="button" role="switch" aria-checked={on} aria-label="AI 生成简短建议" className={`switch${on ? ' on' : ''}`} onClick={toggle}><i /></button>
      </div>
      <div className="set-row">
        <div><b>团队讨论结论自动加入上下文</b><small>/discuss、/team 的汇总结论自动钉进这段对话的上下文，之后每轮都带给模型</small></div>
        <button type="button" role="switch" aria-checked={team} aria-label="团队讨论结论自动加入上下文" className={`switch${team ? ' on' : ''}`}
          onClick={() => { const v = !team; setTeam(v); store.set(LAB_PREF_KEYS.teamCtx, v ? '1' : '0'); notify(v ? '团队结论会自动加入上下文' : '团队结论不再自动加入上下文') }}><i /></button>
      </div>
      <div className="set-row">
        <div><b>单轮工具调用上限</b><small>AI 一轮对话里最多调用多少次工具（查数据、回测、搜索…）；复杂任务需要更多次。默认 1000，范围 1–5000</small></div>
        <input type="number" className="set-num" min={1} max={5000} step={50} value={maxTools} aria-label="单轮工具调用上限"
          onChange={(e) => setMaxTools(e.target.value)}
          onBlur={() => {
            const n = Math.max(1, Math.min(5000, Math.round(Number(maxTools) || 1000)))
            setMaxTools(String(n)); store.set(LAB_PREF_KEYS.maxTools, String(n)); notify(`单轮工具调用上限：${n} 次`)
          }} />
      </div>
    </section>
  )
}
