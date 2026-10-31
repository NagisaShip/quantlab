/**
 * dsh-quantlab · Host 半端 cordis 插件
 * ============================================================================
 * 一个插件把 QuantLab 本地研究台接进 DSH：
 *   1. 后端生命周期：自动起 `python -m quant_agent.local.launcher --serve`
 *      （详见 lib/backend.js），dispose 时停掉子进程；
 *   2. 控制路由（docs 4.3，注册在 ctx.webServer）：
 *        GET  /dsh-quantlab/status
 *        POST /dsh-quantlab/start | /stop | /restart
 *        GET/POST /dsh-quantlab/config
 *        GET  /dsh-quantlab/logs?lines=80
 *   3. 模型桥（docs 4.4，OpenAI 兼容，注册在 ctx.webServer / LLM 见 lib/llm-bridge.js）：
 *        POST /dsh-quantlab/llm/v1/chat/completions
 *        GET  /dsh-quantlab/llm/v1/models
 *   4. DSH 工具（docs 4.5，lib/tools.js）：
 *        quantlab_status / quantlab_market_bars / quantlab_search_stock / quantlab_api
 *   5. 平台模型服务自动登记：后端就绪后把模型桥登记成平台 provider
 *      「DSH · Harness」（name=dsh-harness，base_url=<DSH origin>/dsh-quantlab/llm/v1）。
 *      本地模式写接口必须带 `X-Requested-With: quantlab-desktop`；读接口是
 *      GET /api/config（providers 在里面），路由覆盖走 PUT /api/config/routing。
 *
 * 配置：$DSH_HOME/quantlab/config.json（首次启动写默认值），环境变量
 * QUANTLAB_PROJECT / QUANTLAB_PYTHON / QUANTLAB_SRC 可覆盖。
 * 诊断：$DSH_HOME/quantlab/diag.log（>200KB 轮转到 diag.log.1）。
 *
 * 可注入点（单测用）：home/configFile/diagFile/backend/bridge/defineTool/fetchImpl/spawn/sleep。
 */

import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { homedir } from 'node:os'
import { spawn as nodeSpawn } from 'node:child_process'
import { dirname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createBackend, LOCAL_WRITE_HEADER, LOCAL_WRITE_VALUE } from './backend.js'
import {
  createLlmBridge,
  DEFAULT_MODEL_ID,
  readJsonBody,
  writeError,
  writeJson,
} from './llm-bridge.js'
import { buildToolDefinitions } from './tools.js'

/** cordis：插件名（cordis.patch.yml 里的 id 是 quantlab，name 是包名 dsh-quantlab）。 */
export const name = 'quantlab'
/** 依赖的 DSH 服务；llm 用于模型桥（fish-time 也这么写）。 */
export const inject = ['webServer', 'tools', 'llm']

const LIB_DIR = dirname(fileURLToPath(import.meta.url))
const PKG_DIR = resolve(LIB_DIR, '..')
/** dsh-quantlab 所在的 QuantLab 仓库根目录（package 在 <repo>/dsh-quantlab）。 */
export const REPO_DIR = resolve(PKG_DIR, '..')

/** 平台模型服务登记用的固定 id / 名称。 */
export const PROVIDER_KEY = 'dsh-harness'
export const DEFAULT_PROVIDER_NAME = 'DSH · Harness'

/** diag.log 轮转阈值。 */
const DIAG_MAX_BYTES = 200 * 1024

/** 默认配置（config.defaults.json 是给人看的同款副本）。 */
export function defaultConfig(env = process.env) {
  return {
    projectDir: '',
    python: env.QUANTLAB_PYTHON || 'python',
    srcDir: '',
    autoStart: true,
    llmBridge: true,
    providerName: DEFAULT_PROVIDER_NAME,
    model: '',
    pollMs: 5000,
  }
}

/** 探测 srcDir：仓库根 /src 或包上一级 /src。 */
export function detectSrcDir(repoDir = REPO_DIR) {
  const candidates = [join(repoDir, 'src'), join(repoDir, 'dsh-quantlab', 'src')]
  for (const candidate of candidates) {
    try {
      if (existsSync(candidate)) return candidate
    } catch {
      /* ignore */
    }
  }
  return candidates[0]
}

/** 把配置补全成可用的完整值（默认值 + 校验）。 */
export function normalizeConfig(patch = {}, base = defaultConfig()) {
  const next = { ...base }
  const source = patch && typeof patch === 'object' ? patch : {}
  const text = (value) => (typeof value === 'string' ? value.trim() : '')
  if (Object.hasOwn(source, 'projectDir')) {
    const dir = text(source.projectDir)
    next.projectDir = dir ? resolve(dir) : ''
  }
  if (Object.hasOwn(source, 'python') && text(source.python)) next.python = text(source.python)
  if (Object.hasOwn(source, 'srcDir')) {
    const dir = text(source.srcDir)
    next.srcDir = dir ? resolve(dir) : ''
  }
  if (Object.hasOwn(source, 'autoStart')) next.autoStart = source.autoStart !== false
  if (Object.hasOwn(source, 'llmBridge')) next.llmBridge = source.llmBridge !== false
  if (Object.hasOwn(source, 'providerName') && text(source.providerName)) next.providerName = text(source.providerName).slice(0, 60)
  if (Object.hasOwn(source, 'model')) next.model = text(source.model).slice(0, 120)
  if (Object.hasOwn(source, 'pollMs')) {
    const ms = Number(source.pollMs)
    if (Number.isFinite(ms)) next.pollMs = Math.max(1000, Math.min(60_000, Math.floor(ms)))
  }
  if (!next.projectDir) next.projectDir = join(REPO_DIR, '.dsh-project')
  if (!next.srcDir) next.srcDir = detectSrcDir()
  return next
}

/** 环境变量覆盖（不落盘）。 */
export function applyEnvOverrides(config, env = process.env) {
  const next = { ...config }
  if (env.QUANTLAB_PROJECT && String(env.QUANTLAB_PROJECT).trim()) next.projectDir = resolve(String(env.QUANTLAB_PROJECT).trim())
  if (env.QUANTLAB_PYTHON && String(env.QUANTLAB_PYTHON).trim()) next.python = String(env.QUANTLAB_PYTHON).trim()
  if (env.QUANTLAB_SRC && String(env.QUANTLAB_SRC).trim()) next.srcDir = resolve(String(env.QUANTLAB_SRC).trim())
  return next
}

/** 配置默认路径。 */
export function configPathFor(home) {
  return join(home || process.env.DSH_HOME || join(homedir(), '.dsh'), 'quantlab', 'config.json')
}

/**
 * 读配置（文件不存在时用默认值并落盘）。
 * 优先级：默认值 ← 插件 config ← 配置文件 ← 环境变量。
 */
export function loadConfig({ file = configPathFor(), env = process.env, pluginConfig = {}, save = true } = {}) {
  const base = normalizeConfig(pluginConfig, normalizeConfig(defaultConfig(env)))
  let fromFile = {}
  try {
    if (existsSync(file)) {
      const raw = JSON.parse(readFileSync(file, 'utf8'))
      if (raw && typeof raw === 'object') fromFile = raw
    }
  } catch {
    fromFile = {}
  }
  const merged = normalizeConfig(fromFile, base)
  const withEnv = normalizeConfig(applyEnvOverrides(merged, env), merged)
  if (save && !existsSync(file)) {
    try {
      writeConfigFile(file, withEnv)
    } catch {
      /* 写不了也不影响启动 */
    }
  }
  return withEnv
}

/** 原子写配置文件。 */
export function writeConfigFile(file, config) {
  mkdirSync(dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`
  writeFileSync(tmp, `${JSON.stringify(config, null, 2)}\n`, 'utf8')
  renameSync(tmp, file)
  return config
}

/**
 * 诊断日志（追加 + 超过 200KB 轮转）。
 * @param {{file: string, maxBytes?: number}} options
 * @returns {(message: string) => void}
 */
export function createDiag({ file, maxBytes = DIAG_MAX_BYTES }) {
  return (message) => {
    try {
      mkdirSync(dirname(file), { recursive: true })
      if (existsSync(file) && statSync(file).size > maxBytes) {
        try {
          renameSync(file, `${file}.1`)
        } catch {
          writeFileSync(file, '')
        }
      }
      const d = new Date()
      const p = (n) => String(n).padStart(2, '0')
      const stamp = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
      appendFileSync(file, `[${stamp}] ${message}\n`, 'utf8')
    } catch {
      /* 诊断日志不能影响主流程 */
    }
  }
}

/** 安全地取一个 DSH 服务（ctx.get 优先，其次 ctx[name]）。 */
function serviceOf(ctx, serviceName) {
  try {
    if (ctx && typeof ctx.get === 'function') {
      const service = ctx.get(serviceName)
      if (service) return service
    }
  } catch {
    /* 服务没挂载 */
  }
  try {
    return ctx ? ctx[serviceName] : undefined
  } catch {
    return undefined
  }
}

function readQuery(req) {
  try {
    return new URL(String((req && req.url) || '/'), 'http://127.0.0.1').searchParams
  } catch {
    return new URLSearchParams()
  }
}

function isPost(req) {
  return String((req && req.method) || 'GET').toUpperCase() === 'POST'
}

function methodNotAllowed(res, allowed) {
  writeJson(res, 405, { ok: false, error: `只用 ${allowed}` })
}

/**
 * 组装插件宿主（apply 调它；测试也直接调它，注入假 backend/bridge/defineTool）。
 *
 * @param {object} ctx cordis 上下文（需要 webServer / tools / effect）
 * @param {object} [options]
 * @returns {Promise<object>} host
 */
export async function setup(ctx, options = {}) {
  const home = options.home || process.env.DSH_HOME || join(homedir(), '.dsh')
  const env = options.env || process.env
  const configFile = options.configFile || configPathFor(home)
  const diagFile = options.diagFile || join(home, 'quantlab', 'diag.log')
  const log = options.log || createDiag({ file: diagFile })
  const diag = (message) => {
    try {
      log(`[dsh-quantlab] ${message}`)
    } catch {
      /* ignore */
    }
  }
  const fetchImpl = options.fetch || globalThis.fetch
  const sleep = options.sleep || ((ms) => new Promise((done) => setTimeout(done, ms)))

  let config = loadConfig({ file: configFile, env, pluginConfig: options.config || {} })
  diag(`插件加载：projectDir=${config.projectDir} srcDir=${config.srcDir} python=${config.python} autoStart=${config.autoStart}`)

  // ── 后端 ────────────────────────────────────────────────────────────────
  const makeBackend = (nextConfig) =>
    createBackend({
      projectDir: nextConfig.projectDir,
      python: nextConfig.python,
      srcDir: nextConfig.srcDir,
      logFile: join(nextConfig.projectDir, '.quantlab', 'dsh-quantlab.log'),
      ...(options.spawn ? { spawn: options.spawn } : {}),
      ...(options.backendFetch ? { fetch: options.backendFetch } : {}),
      ...(options.isPidAlive ? { isPidAlive: options.isPidAlive } : {}),
      ...(options.getFreePort ? { getFreePort: options.getFreePort } : {}),
      ...(options.killPid ? { killPid: options.killPid } : {}),
      ...(options.healthTimeoutMs ? { healthTimeoutMs: options.healthTimeoutMs } : {}),
      ...(options.pollIntervalMs ? { pollIntervalMs: options.pollIntervalMs } : {}),
      onState: (status) => diag(`后端状态 → ${status.state}${status.error ? `（${status.error}）` : ''}`),
    })
  let backend = options.backend || makeBackend(config)

  // ── 模型桥 ──────────────────────────────────────────────────────────────
  const bridge =
    options.bridge ||
    createLlmBridge({
      getLlm: () => serviceOf(ctx, 'llm'),
      getDefaultModel: () => serviceOf(ctx, 'agentDefaultModel'),
      model: config.model,
      modelId: DEFAULT_MODEL_ID,
      log: (message) => diag(message),
      ...(options.now ? { now: options.now } : {}),
    })

  // DSH 的 provider 拓扑变化（HMR / 设置里改模型服务商）时清空模型解析缓存，
  // 否则会拿着「已经没注册适配器」的旧 provider 打到 NO_ADAPTER。
  try {
    if (typeof ctx.on === 'function' && typeof ctx.effect === 'function') {
      ctx.effect(() =>
        ctx.on('llm/adapters-updated', () => {
          try {
            bridge.invalidate?.()
          } catch {
            /* ignore */
          }
          diag('DSH 模型适配器拓扑变化：已清空模型解析缓存')
        }),
      )
    }
  } catch (error) {
    diag(`监听 llm/adapters-updated 失败：${(error && error.message) || error}`)
  }

  // ── 状态 / 平台登记状态 ─────────────────────────────────────────────────
  let providerState = { ok: false, skipped: '尚未尝试', at: 0 }
  let providerKey = ''
  let providerPromise = null
  // 模型名说人话：DSH 解析出的真实目标 → 「DSH · deepseek-v4.1-flash」
  let resolvedLabel = ''

  const origin = () => {
    try {
      return String(ctx.webServer.getOrigin() || '').replace(/\/+$/, '')
    } catch {
      return ''
    }
  }

  const hostStatus = () => {
    const base = typeof backend.status === 'function' ? backend.status() : {}
    let llmBridge = 'unavailable'
    try {
      llmBridge = bridge.available() ? 'ready' : 'unavailable'
    } catch {
      llmBridge = 'unavailable'
    }
    let resolvedTarget = null
    try {
      resolvedTarget = bridge.lastTarget || null
    } catch {
      resolvedTarget = null
    }
    return {
      ...base,
      ok: base.state === 'running',
      llmBridge,
      providerName: config.providerName,
      provider: PROVIDER_KEY,
      model: DEFAULT_MODEL_ID,
      savedModel: config.model || '',
      ...(resolvedTarget ? { resolvedTarget } : {}),
      resolvedTargetLabel: resolvedLabel,
      configPath: configFile,
      projectDir: config.projectDir,
      providerRegistration: { ...providerState },
    }
  }
  const logLines = (lines = 80) => {
    const n = Math.max(1, Math.min(2000, Number(lines) || 80))
    try {
      if (typeof backend.logTail === 'function') return backend.logTail(n)
    } catch {
      /* ignore */
    }
    return []
  }

  // ── 平台模型服务登记 ────────────────────────────────────────────────────
  async function platformRequest(url, { method = 'GET', body } = {}) {
    if (typeof fetchImpl !== 'function') throw new Error('没有可用的 fetch')
    const headers = { accept: 'application/json' }
    if (body !== undefined) {
      headers['content-type'] = 'application/json; charset=utf-8'
      headers[LOCAL_WRITE_HEADER] = LOCAL_WRITE_VALUE
    }
    const response = await fetchImpl(url, {
      method,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    const text = typeof response.text === 'function' ? await response.text() : ''
    let json = null
    try {
      json = text ? JSON.parse(text) : null
    } catch {
      json = null
    }
    return { ok: response.ok === true, status: Number(response.status) || 0, json, text }
  }

  async function readProviders(baseUrl) {
    const primary = await platformRequest(`${baseUrl}/api/config`)
    if (primary.ok && primary.json && Array.isArray(primary.json.providers)) return primary.json.providers
    const fallback = await platformRequest(`${baseUrl}/api/config/providers`)
    if (fallback.ok && fallback.json && Array.isArray(fallback.json.providers)) return fallback.json.providers
    return null
  }

  // 顺手解析一次 provider/model，方便在状态/日志里看到模型桥到底走哪个模型
  /**
   * 把 DSH 解析出的 {provider, model} 变成人话标签，例如：
   *   「DSH · deepseek-v4.1-flash」（providerName 取的插件配置，默认 DSH · Harness）。
   * 优先用 ctx.llm.resolveModelInfo 的 displayName/label，拿不到就用原始 model id。
   */
  async function resolveTargetLabel(target) {
    if (!target || !target.model) return ''
    let displayName = ''
    try {
      const llm = serviceOf(ctx, 'llm')
      if (llm && typeof llm.resolveModelInfo === 'function') {
        const info = await llm.resolveModelInfo(target.provider, target.model)
        displayName = String((info && (info.displayName || info.label || info.name)) || '').trim()
      }
    } catch {
      displayName = ''
    }
    const modelName = displayName || String(target.model)
    const providerName = String(config.providerName || DEFAULT_PROVIDER_NAME).trim() || DEFAULT_PROVIDER_NAME
    return `${providerName} · ${modelName}`.slice(0, 80)
  }

  async function resolveAndLogTarget() {
    try {
      const target = await bridge.resolveTarget?.()
      if (target) {
        resolvedLabel = (await resolveTargetLabel(target)) || resolvedLabel
        diag(`模型桥目标：${target.provider}/${target.model}${target.reasoningEffort ? `（effort=${target.reasoningEffort}）` : ''}${resolvedLabel ? ` → ${resolvedLabel}` : ''}`)
      }
    } catch (error) {
      diag(`模型桥暂时解析不出模型：${(error && error.message) || error}`)
    }
  }

  /**
   * 平台登记模型服务时填的上下文窗口：优先问 DSH 解析出的模型能力，
   * 问不到就保守给 128k（平台默认 32k 会让 Agent 过早压缩历史）。
   */
  async function resolveModelContextWindow() {
    try {
      const target = bridge.lastTarget || (await bridge.resolveTarget?.())
      const llm = serviceOf(ctx, 'llm')
      if (target && llm && typeof llm.resolveModelInfo === 'function') {
        const info = await llm.resolveModelInfo(target.provider, target.model)
        const size = Number(info && (info.contextWindow || info.context_window))
        if (Number.isFinite(size) && size >= 8000) return Math.floor(size)
      }
    } catch {
      /* 用保守默认值 */
    }
    return 128000
  }

  async function registerPlatformProvider({ force = false } = {}) {
    if (config.llmBridge === false) {
      providerState = { ok: false, skipped: 'llmBridge 已关闭', at: Date.now() }
      return providerState
    }
    if (!bridge.available()) {
      providerState = { ok: false, skipped: 'DSH 模型服务不可用（ctx.llm 未挂载）', at: Date.now() }
      diag('平台模型登记跳过：ctx.llm 不可用')
      return providerState
    }
    const status = hostStatus()
    if (status.state !== 'running' || !status.url) {
      providerState = { ok: false, skipped: '后端未运行', at: Date.now() }
      return providerState
    }
    const dshOrigin = origin()
    if (!dshOrigin) {
      providerState = { ok: false, skipped: '读不到 DSH origin（ctx.webServer.getOrigin 不可用）', at: Date.now() }
      return providerState
    }
    const baseUrl = status.url
    const bridgeUrl = `${dshOrigin}/dsh-quantlab/llm/v1`
    const key = `${baseUrl}|${bridgeUrl}|${config.providerName}`
    if (!force && key === providerKey && providerState.ok) return providerState
    if (providerPromise) return providerPromise

    providerPromise = (async () => {
      let lastError = ''
      for (let attempt = 1; attempt <= 3; attempt++) {
        try {
          const providers = await readProviders(baseUrl)
          if (providers === null) throw new Error('读不到平台 provider 清单（/api/config）')
          const existing = providers.find((entry) => String(entry && entry.name) === PROVIDER_KEY)
          const existingSettings = existing && existing.model_settings && existing.model_settings[DEFAULT_MODEL_ID]
          const existingWindow = Number(existingSettings && (existingSettings.context_window || existingSettings.contextWindow))
          const existingLabel = String((existingSettings && existingSettings.label) || '')
          // 模型名说人话：用 resolvedTarget（如 opencode-go/deepseek-v4.1-flash）生成
          // 「DSH · deepseek-v4.1-flash」。标签变了也要重新登记，否则界面一直显示旧名字。
          let targetLabel = ''
          try {
            const target = await bridge.resolveTarget?.()
            if (target) targetLabel = await resolveTargetLabel(target)
          } catch {
            targetLabel = ''
          }
          if (targetLabel) resolvedLabel = targetLabel
          const wantedLabel = targetLabel || 'DSH 模型桥'
          // 能解析出真实模型名时，标签也要一致（旧版本登记的是「DSH 模型桥」，要升级）；
          // 解析不出（没有 ctx.llm / 单测假 bridge）就不拿标签当跳过条件。
          const labelMatches = !targetLabel || existingLabel === wantedLabel
          if (
            !force &&
            existing &&
            String(existing.base_url || '') === bridgeUrl &&
            existingWindow >= 64000 &&
            labelMatches
          ) {
            diag(`平台模型登记：已有 ${PROVIDER_KEY} → ${bridgeUrl}（context_window=${existingWindow}，label=${existingLabel}），跳过`)
            providerState = { ok: true, skipped: '已登记（base_url 一致）', at: Date.now() }
            providerKey = key
            await resolveAndLogTarget()
            return providerState
          }
          const contextWindow = await resolveModelContextWindow()
          const payload = {
            name: PROVIDER_KEY,
            label: config.providerName,
            type: 'openai_compatible',
            base_url: bridgeUrl,
            api_key: PROVIDER_KEY,
            models: [DEFAULT_MODEL_ID],
            model_settings: {
              [DEFAULT_MODEL_ID]: { label: wantedLabel, context_window: contextWindow, inputs: ['text'] },
            },
            scope: 'workspace',
          }
          const upsert = await platformRequest(`${baseUrl}/api/config/providers`, { method: 'POST', body: payload })
          if (!upsert.ok) throw new Error(`POST /api/config/providers → HTTP ${upsert.status}：${(upsert.text || '').slice(0, 300)}`)

          // 路由：只在 default 为空或指向不存在的 provider 时覆盖，别改用户选择
          const modelsInfo = await platformRequest(`${baseUrl}/api/config/models`)
          const currentDefault = String((modelsInfo.json && modelsInfo.json.routing && modelsInfo.json.routing.default) || '')
          const targets = new Set(
            ((modelsInfo.json && modelsInfo.json.models) || []).map((entry) => String((entry && entry.target) || '')),
          )
          const wanted = `${PROVIDER_KEY}/${DEFAULT_MODEL_ID}`
          if (!currentDefault || !targets.has(currentDefault)) {
            const routing = await platformRequest(`${baseUrl}/api/config/routing?scope=workspace`, {
              method: 'PUT',
              body: { default: wanted },
            })
            if (!routing.ok) diag(`平台模型路由设置失败（HTTP ${routing.status}）：${(routing.text || '').slice(0, 200)}`)
          }

          // 校验 ready
          const check = await platformRequest(`${baseUrl}/api/config/models`)
          const entry = ((check.json && check.json.models) || []).find(
            (model) => String(model && model.provider) === PROVIDER_KEY && String(model && model.model) === DEFAULT_MODEL_ID,
          )
          providerState = {
            ok: Boolean(entry && entry.ready),
            ready: Boolean(entry && entry.ready),
            baseUrl: bridgeUrl,
            at: Date.now(),
            ...(entry ? {} : { warning: '登记成功但 /api/config/models 里还没出现该模型' }),
          }
          providerKey = key
          diag(`平台模型登记完成：${bridgeUrl}（ready=${providerState.ok}）`)
          await resolveAndLogTarget()
          return providerState
        } catch (error) {
          lastError = String((error && error.message) || error)
          diag(`平台模型登记失败（第 ${attempt}/3 次）：${lastError}`)
          if (attempt < 3) await sleep(800 * attempt)
        }
      }
      providerState = { ok: false, error: lastError || '未知错误', at: Date.now() }
      return providerState
    })()
    try {
      return await providerPromise
    } finally {
      providerPromise = null
    }
  }

  // ── 控制路由 ────────────────────────────────────────────────────────────
  const disposers = []
  const addRoute = (kind, path, handler) => {
    try {
      if (!ctx.webServer || typeof ctx.webServer.register !== 'function') return
      if (typeof ctx.effect === 'function') {
        disposers.push(ctx.effect(() => ctx.webServer.register({ kind, path, handler })))
      } else {
        disposers.push(ctx.webServer.register({ kind, path, handler }))
      }
    } catch (error) {
      diag(`路由注册失败 ${path}：${(error && error.message) || error}`)
    }
  }

  addRoute('exact', '/dsh-quantlab/status', async (req, res) => {
    if (isPost(req)) return methodNotAllowed(res, 'GET')
    writeJson(res, 200, hostStatus())
  })

  addRoute('exact', '/dsh-quantlab/start', async (req, res) => {
    if (!isPost(req)) return methodNotAllowed(res, 'POST')
    diag('收到启动请求')
    try {
      await backend.start()
    } catch (error) {
      diag(`启动异常：${(error && error.message) || error}`)
    }
    writeJson(res, 200, hostStatus())
    if (backend.running) void registerPlatformProvider()
  })

  addRoute('exact', '/dsh-quantlab/stop', async (req, res) => {
    if (!isPost(req)) return methodNotAllowed(res, 'POST')
    diag('收到停止请求')
    try {
      await backend.stop()
    } catch (error) {
      diag(`停止异常：${(error && error.message) || error}`)
    }
    writeJson(res, 200, hostStatus())
  })

  addRoute('exact', '/dsh-quantlab/restart', async (req, res) => {
    if (!isPost(req)) return methodNotAllowed(res, 'POST')
    diag('收到重启请求')
    try {
      await backend.restart()
    } catch (error) {
      diag(`重启异常：${(error && error.message) || error}`)
    }
    writeJson(res, 200, hostStatus())
    if (backend.running) void registerPlatformProvider()
  })

  addRoute('exact', '/dsh-quantlab/config', async (req, res) => {
    if (!isPost(req)) {
      writeJson(res, 200, { ok: true, config, status: hostStatus() })
      return
    }
    const parsed = await readJsonBody(req, 64 * 1024)
    if (!parsed.ok) return writeError(res, 400, parsed.error, 'invalid_config')
    const before = config
    config = normalizeConfig(parsed.body, before)
    try {
      writeConfigFile(configFile, config)
    } catch (error) {
      diag(`写配置失败：${(error && error.message) || error}`)
      return writeError(res, 500, `无法保存配置：${(error && error.message) || error}`, 'config_write_failed')
    }
    diag(`配置已更新：${JSON.stringify(parsed.body).slice(0, 400)}`)
    if (bridge && 'model' in bridge) bridge.model = config.model
    try {
      bridge.invalidate?.()
    } catch {
      /* ignore */
    }

    const backendChanged =
      before.projectDir !== config.projectDir ||
      before.python !== config.python ||
      before.srcDir !== config.srcDir ||
      before.autoStart !== config.autoStart
    if (backendChanged) {
      const wasRunning = Boolean(backend && backend.running)
      try {
        if (wasRunning) await backend.stop()
        if (typeof backend.configure === 'function') {
          backend.configure({ projectDir: config.projectDir, python: config.python, srcDir: config.srcDir })
        } else {
          try {
            backend.dispose?.()
          } catch {
            /* ignore */
          }
          backend = makeBackend(config)
        }
        if (wasRunning || config.autoStart) await backend.start()
      } catch (error) {
        diag(`应用新配置时重启后端失败：${(error && error.message) || error}`)
      }
    }
    if (backend && backend.running) void registerPlatformProvider({ force: true })
    writeJson(res, 200, { ok: true, config: { ...config }, ...hostStatus() })
  })

  addRoute('exact', '/dsh-quantlab/logs', async (req, res) => {
    if (isPost(req)) return methodNotAllowed(res, 'GET')
    const query = readQuery(req)
    const lines = Number(query.get('lines') || 80)
    const tail = logLines(lines)
    writeJson(res, 200, { ok: true, lines: tail.length, logTail: tail, text: tail.join('\n') })
  })

  // ── 平台代理（面板 / 对话卡片用） ───────────────────────────────────────
  // DSH 页面和平台后端不同端口，面板直接 fetch 平台会被 CORS 拦；
  // 这里用和页面同源的 /dsh-quantlab/* 转发，顺便收紧可访问的路径。
  const SERVICE_OFFLINE = 'QuantLab 服务未启动（打开侧栏 QuantLab 面板启动）'
  const platformBase = () => String((backend && backend.running && backend.url) || '').replace(/\/+$/, '')
  const pathnameOf = (req) => {
    try {
      return new URL(String((req && req.url) || '/'), 'http://127.0.0.1').pathname
    } catch {
      return '/'
    }
  }

  /** 读请求体 JSON（有上限；空体返回 undefined）。 */
  function readAnyBody(req, limit = 256 * 1024) {
    return new Promise((done) => {
      if (!req || typeof req.on !== 'function') {
        done({ ok: true, body: undefined })
        return
      }
      let size = 0
      const chunks = []
      req.on('data', (chunk) => {
        size += chunk.length
        if (size <= limit) chunks.push(chunk)
      })
      req.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        if (!text.trim()) return done({ ok: true, body: undefined })
        try {
          done({ ok: true, body: JSON.parse(text) })
        } catch {
          done({ ok: false, error: '请求体不是合法 JSON' })
        }
      })
      req.on('error', () => done({ ok: false, error: '请求体读取失败' }))
    })
  }

  /** 转发到平台 JSON 接口（读接口不需要写头；写接口补本地写头）。 */
  async function proxyJson(res, method, platformPath, body) {
    const base = platformBase()
    if (!base) return writeJson(res, 503, { ok: false, error: SERVICE_OFFLINE })
    const verb = String(method || 'GET').toUpperCase()
    const headers = { accept: 'application/json' }
    const hasBody = body !== undefined
    if (hasBody) headers['content-type'] = 'application/json; charset=utf-8'
    if (hasBody || verb !== 'GET') headers[LOCAL_WRITE_HEADER] = LOCAL_WRITE_VALUE
    let response
    try {
      response = await fetchImpl(`${base}${platformPath}`, {
        method: verb,
        headers,
        ...(hasBody ? { body: JSON.stringify(body) } : {}),
      })
    } catch (error) {
      return writeJson(res, 502, { ok: false, error: `连不上 QuantLab 后端：${(error && error.message) || error}` })
    }
    const text = await response.text().catch(() => '')
    let data = null
    try {
      data = text ? JSON.parse(text) : null
    } catch {
      data = null
    }
    if (data === null) {
      return writeJson(res, response.status || 502, { ok: false, error: (text || '平台返回的不是 JSON').slice(0, 400) })
    }
    return writeJson(res, response.status || 200, data)
  }

  const BINARY_TYPES = { png: 'image/png', pdf: 'application/pdf', html: 'text/html; charset=utf-8' }

  /** 转发平台的二进制接口（图表 PNG / 报告 PDF）。 */
  async function proxyBinary(res, platformPath, { downloadName = '', cacheControl = 'no-store' } = {}) {
    const base = platformBase()
    if (!base) return writeJson(res, 503, { ok: false, error: SERVICE_OFFLINE })
    let response
    try {
      response = await fetchImpl(`${base}${platformPath}`, { headers: { accept: '*/*' } })
    } catch (error) {
      return writeJson(res, 502, { ok: false, error: `连不上 QuantLab 后端：${(error && error.message) || error}` })
    }
    if (response.ok !== true) {
      let detail = ''
      try {
        detail = (await response.text()).slice(0, 400)
      } catch {
        /* ignore */
      }
      return writeJson(res, response.status || 502, {
        ok: false,
        error: `平台接口失败（HTTP ${response.status}）：${detail || platformPath}`,
      })
    }
    const buffer = Buffer.from(await response.arrayBuffer())
    let contentType = 'application/octet-stream'
    try {
      contentType = String(response.headers?.get?.('content-type') || '').split(';')[0] || contentType
    } catch {
      /* ignore */
    }
    const headers = {
      'content-type': contentType,
      'content-length': buffer.length,
      'cache-control': cacheControl,
    }
    if (downloadName) headers['content-disposition'] = `attachment; filename="${downloadName}"`
    try {
      res.writeHead(200, headers)
      res.end(buffer)
    } catch {
      /* 连接已断 */
    }
  }

  /** 在系统文件管理器里打开一个目录（只给面板上的「打开文件夹」用）。 */
  function openInFileManager(target) {
    const command = process.platform === 'win32' ? 'explorer.exe' : process.platform === 'darwin' ? 'open' : 'xdg-open'
    const child = nodeSpawn(command, [target], { detached: true, stdio: 'ignore' })
    if (child && typeof child.unref === 'function') child.unref()
  }

  // GitHub 导入代理：GET/POST/DELETE /dsh-quantlab/imports... → 平台 /api/imports/github...
  // （客户端用 /imports、/imports/github、/imports/{id}；平台统一在 /api/imports/github 下）
  addRoute('prefix', '/dsh-quantlab/imports', async (req, res) => {
    const suffix = pathnameOf(req).replace(/^\/dsh-quantlab\/imports/, '') || ''
    const rest = suffix.replace(/^\/github/, '')
    const platformPath = `/api/imports/github${rest}`
    const method = String((req && req.method) || 'GET').toUpperCase()
    if (method === 'GET') return proxyJson(res, 'GET', platformPath)
    if (method === 'DELETE') return proxyJson(res, 'DELETE', platformPath)
    if (method === 'POST') {
      const parsed = await readAnyBody(req)
      if (!parsed.ok) return writeJson(res, 400, { ok: false, error: parsed.error })
      return proxyJson(res, 'POST', platformPath, parsed.body ?? {})
    }
    return methodNotAllowed(res, 'GET/POST/DELETE')
  })

  // 打开项目内的目录（导入目录等）：只允许项目目录下的路径。
  addRoute('exact', '/dsh-quantlab/open', async (req, res) => {
    if (!isPost(req)) return methodNotAllowed(res, 'POST')
    const parsed = await readAnyBody(req)
    if (!parsed.ok) return writeJson(res, 400, { ok: false, error: parsed.error })
    const target = String((parsed.body && parsed.body.path) || '').trim()
    if (!target) return writeJson(res, 400, { ok: false, error: '缺少 path' })
    let resolved = ''
    try {
      resolved = resolve(target)
    } catch {
      return writeJson(res, 400, { ok: false, error: 'path 不合法' })
    }
    const projectRoot = resolve(config.projectDir || REPO_DIR)
    if (resolved !== projectRoot && !resolved.startsWith(projectRoot + sep)) {
      return writeJson(res, 403, { ok: false, error: '只能打开项目目录内的路径' })
    }
    if (!existsSync(resolved)) return writeJson(res, 404, { ok: false, error: `目录不存在：${resolved}` })
    try {
      openInFileManager(resolved)
      return writeJson(res, 200, { ok: true, path: resolved })
    } catch (error) {
      return writeJson(res, 500, { ok: false, error: `打开文件管理器失败：${(error && error.message) || error}` })
    }
  })

  // 回测报告图：GET /dsh-quantlab/chart/<backtest_id>.png（转发平台 report.png，带缓存头）
  addRoute('prefix', '/dsh-quantlab/chart', async (req, res) => {
    const matched = pathnameOf(req).match(/^\/dsh-quantlab\/chart\/([A-Za-z0-9_-]{1,64})\.png$/)
    if (!matched) return writeJson(res, 400, { ok: false, error: '路径应为 /dsh-quantlab/chart/<backtest_id>.png' })
    return proxyBinary(res, `/api/backtests/${matched[1]}/report.png`, { cacheControl: 'public, max-age=30' })
  })

  // 报告下载：GET /dsh-quantlab/report/<backtest_id>.<png|pdf|html>（卡片「下载报告」用）
  addRoute('prefix', '/dsh-quantlab/report', async (req, res) => {
    const matched = pathnameOf(req).match(/^\/dsh-quantlab\/report\/([A-Za-z0-9_-]{1,64})\.(png|pdf|html)$/)
    if (!matched) return writeJson(res, 400, { ok: false, error: '路径应为 /dsh-quantlab/report/<backtest_id>.<png|pdf|html>' })
    const id = matched[1]
    const format = matched[2]
    const download = format === 'html' ? '' : `backtest-${id}.${format}`
    return proxyBinary(res, `/api/backtests/${id}/report.${format}`, {
      cacheControl: 'no-store',
      downloadName: download,
    })
  })

  // ── 模型桥路由 ──────────────────────────────────────────────────────────
  addRoute('exact', '/dsh-quantlab/llm/v1/chat/completions', async (req, res) => {
    if (!isPost(req)) return methodNotAllowed(res, 'POST')
    try {
      await bridge.handleCompletions(req, res)
    } catch (error) {
      diag(`模型桥异常：${(error && error.message) || error}`)
      if (!res.headersSent) writeError(res, 500, `模型桥内部错误：${(error && error.message) || error}`, 'bridge_error')
    }
  })
  addRoute('exact', '/dsh-quantlab/llm/v1/models', async (req, res) => {
    try {
      bridge.handleModels(req, res)
    } catch (error) {
      if (!res.headersSent) writeError(res, 500, `模型桥内部错误：${(error && error.message) || error}`, 'bridge_error')
    }
  })
  addRoute('prefix', '/dsh-quantlab/llm', async (req, res) => {
    bridge.handleNotFound(req, res)
  })

  // ── DSH 工具 ────────────────────────────────────────────────────────────
  const toolDisposers = []
  let toolNames = []
  try {
    if (ctx.tools && typeof ctx.tools.register === 'function') {
      let defineTool = options.defineTool
      if (typeof defineTool !== 'function') {
        try {
          const mod = await import('@deepseek-ai/dsh-tools')
          defineTool = mod && mod.defineTool
        } catch (error) {
          diag(`工具没注册：@deepseek-ai/dsh-tools 不可用（${(error && error.message) || error}）`)
        }
      }
      if (typeof defineTool === 'function') {
        const definitions = buildToolDefinitions({
          defineTool,
          getBaseUrl: () => (backend && backend.running ? backend.url : ''),
          getStatus: () => hostStatus(),
          getDshOrigin: () => origin(),
          fetch: fetchImpl,
          log: diag,
        })
        for (const definition of definitions) {
          try {
            const off = ctx.tools.register(definition)
            toolDisposers.push(off)
            toolNames.push(definition.name)
          } catch (error) {
            diag(`注册工具 ${definition.name} 失败：${(error && error.message) || error}`)
          }
        }
        diag(`已注册 DSH 工具：${toolNames.join(' / ') || '（无）'}`)
      }
    }
  } catch (error) {
    diag(`工具注册流程失败：${(error && error.message) || error}`)
  }

  // ── 卸载：注销路由/工具 + 停后端 ────────────────────────────────────────
  const dispose = () => {
    for (const off of toolDisposers.splice(0)) {
      try {
        if (typeof off === 'function') off()
        else off?.dispose?.()
      } catch {
        /* ignore */
      }
    }
    for (const off of disposers.splice(0)) {
      try {
        if (typeof off === 'function') off()
        else off?.dispose?.()
      } catch {
        /* ignore */
      }
    }
    try {
      backend.dispose?.()
    } catch {
      /* ignore */
    }
    diag('插件已卸载（后端已停）')
  }
  if (typeof ctx.effect === 'function') {
    try {
      ctx.effect(() => () => dispose())
    } catch {
      /* ignore */
    }
  }

  const host = {
    get config() {
      return config
    },
    get backend() {
      return backend
    },
    bridge,
    status: hostStatus,
    logTail: logLines,
    ensureProvider: () => registerPlatformProvider({ force: true }),
    refreshProvider: () => registerPlatformProvider(),
    registerPlatformProvider,
    toolNames: () => toolNames.slice(),
    dispose,
    configFile,
    autoStartPromise: null,
  }

  // ── 自动启动 ────────────────────────────────────────────────────────────
  if (config.autoStart && options.autoStart !== false) {
    host.autoStartPromise = (async () => {
      try {
        await backend.start()
        if (backend.running) await registerPlatformProvider()
      } catch (error) {
        diag(`自动启动失败：${(error && error.message) || error}`)
      }
      return hostStatus()
    })()
  }

  diag('插件就绪')
  return host
}

/**
 * cordis 插件入口。
 * @param {object} ctx
 * @param {object} [config] cordis.patch.yml 里的 config
 */
export function apply(ctx, config) {
  return setup(ctx, { config: config || {} })
}
