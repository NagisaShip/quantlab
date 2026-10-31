/**
 * lib/index.js 单测：配置读写默认值、本地控制路由、工具注册、平台模型服务登记、卸载清理。
 * 用假 backend / 假 bridge / 假 defineTool，不起 Python、不连网络。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import {
  DEFAULT_PROVIDER_NAME,
  PROVIDER_KEY,
  REPO_DIR,
  apply,
  configPathFor,
  defaultConfig,
  detectSrcDir,
  inject,
  loadConfig,
  name,
  normalizeConfig,
  setup,
  writeConfigFile,
} from '../lib/index.js'
import { tempDir, mockCtx, mockReq, mockRes, callRoute } from './helpers.mjs'

/** 假后端（状态机由测试直接拨）。 */
function fakeBackend({ state = 'stopped', url = 'http://127.0.0.1:8123', projectDir = 'C:/proj' } = {}) {
  const backend = {
    calls: [],
    _state: state,
    projectDir,
    get running() {
      return backend._state === 'running'
    },
    get url() {
      return backend._state === 'running' ? url : ''
    },
    status() {
      return {
        ok: backend._state === 'running',
        state: backend._state,
        url: backend._state === 'running' ? url : '',
        port: backend._state === 'running' ? 8123 : 0,
        pid: backend._state === 'running' ? 4321 : 0,
        projectDir: backend.projectDir,
        startedAt: backend._state === 'running' ? 1_700_000_000_000 : 0,
        error: '',
        reused: false,
        logTail: 'backend log line',
      }
    },
    async start() {
      backend.calls.push('start')
      backend._state = 'running'
      return backend.status()
    },
    async stop() {
      backend.calls.push('stop')
      backend._state = 'stopped'
      return backend.status()
    },
    async restart() {
      backend.calls.push('restart')
      backend._state = 'running'
      return backend.status()
    },
    logTail(lines = 60) {
      return ['line-1', `line-${lines}`]
    },
    configure(patch) {
      backend.calls.push({ configure: patch })
      if (patch.projectDir) backend.projectDir = patch.projectDir
    },
    dispose() {
      backend.calls.push('dispose')
      backend._state = 'stopped'
    },
  }
  return backend
}

/** 假模型桥。 */
function fakeBridge({ available = true } = {}) {
  const calls = []
  return {
    calls,
    model: '',
    available: () => available,
    invalidate() {
      calls.push('invalidate')
    },
    async handleCompletions(req, res) {
      calls.push('completions')
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ bridge: 'completions' }))
    },
    handleModels(req, res) {
      calls.push('models')
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ bridge: 'models' }))
    },
    handleNotFound(req, res) {
      calls.push('notfound')
      res.writeHead(404, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ bridge: 'notfound' }))
    },
  }
}

const defineTool = (spec) => spec

/** 搭一个 host（路由/工具都注册到假 ctx 里）。 */
async function makeHost({ backend, bridge, fetch, config, autoStart = false, home } = {}) {
  const tmp = tempDir()
  const ctx = mockCtx()
  const host = await setup(ctx, {
    home: home || tmp.dir,
    backend: backend || fakeBackend(),
    bridge: bridge || fakeBridge(),
    defineTool,
    autoStart,
    fetch,
    env: {},
    sleep: async () => {},
  })
  return { tmp, ctx, host }
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

test('defaultConfig：默认 projectDir 是 <repo>/.dsh-project，srcDir 自动探测到仓库 src', () => {
  const config = normalizeConfig(defaultConfig({}))
  assert.equal(config.projectDir, join(REPO_DIR, '.dsh-project'))
  assert.equal(config.srcDir, detectSrcDir())
  assert.ok(config.srcDir.endsWith('src'))
  assert.ok(existsSync(config.srcDir))
  assert.equal(config.python, 'python')
  assert.equal(config.autoStart, true)
  assert.equal(config.llmBridge, true)
  assert.equal(config.providerName, DEFAULT_PROVIDER_NAME)
})

test('normalizeConfig：布尔/字符串/数值校验 + pollMs 夹取', () => {
  const base = normalizeConfig(defaultConfig({}))
  const next = normalizeConfig(
    { autoStart: false, llmBridge: 'no', providerName: 'X', model: 'a/b', pollMs: 5, python: 'py -3' },
    base,
  )
  assert.equal(next.autoStart, false)
  assert.equal(next.llmBridge, true, '非 false 一律视为开')
  assert.equal(next.providerName, 'X')
  assert.equal(next.model, 'a/b')
  assert.equal(next.pollMs, 1000, 'pollMs 最小 1s')
  assert.equal(next.python, 'py -3')
})

test('loadConfig：文件不存在时写默认值；文件值覆盖默认；环境变量覆盖文件', () => {
  const tmp = tempDir()
  try {
    const file = configPathFor(tmp.dir)
    const config = loadConfig({ file, env: {} })
    assert.ok(existsSync(file), '首次应写出默认配置')
    const saved = JSON.parse(readFileSync(file, 'utf8'))
    assert.equal(saved.projectDir, config.projectDir)
    assert.equal(saved.llmBridge, true)

    writeConfigFile(file, { ...saved, projectDir: join(tmp.dir, 'custom'), python: 'python3' })
    const reloaded = loadConfig({ file, env: {} })
    assert.equal(reloaded.projectDir, join(tmp.dir, 'custom'))
    assert.equal(reloaded.python, 'python3')

    const withEnv = loadConfig({
      file,
      env: {
        QUANTLAB_PROJECT: join(tmp.dir, 'from-env'),
        QUANTLAB_PYTHON: 'env-python',
        QUANTLAB_SRC: join(tmp.dir, 'env-src'),
      },
    })
    assert.equal(withEnv.projectDir, join(tmp.dir, 'from-env'))
    assert.equal(withEnv.python, 'env-python')
    assert.equal(withEnv.srcDir, join(tmp.dir, 'env-src'))

    // 坏的配置文件不炸，退回默认
    writeFileSync(file, '{not json', 'utf8')
    const broken = loadConfig({ file, env: {} })
    assert.equal(broken.python, 'python')
  } finally {
    tmp.cleanup()
  }
})

// ---------------------------------------------------------------------------
// 插件元信息 / 入口
// ---------------------------------------------------------------------------

test('插件元信息：name=quantlab，inject 含 webServer/tools/llm，apply 是函数', () => {
  assert.equal(name, 'quantlab')
  assert.deepEqual(inject, ['webServer', 'tools', 'llm'])
  assert.equal(typeof apply, 'function')
})

// ---------------------------------------------------------------------------
// 控制路由
// ---------------------------------------------------------------------------

test('GET /dsh-quantlab/status：返回 docs 4.3 的字段 + llmBridge/provider', async () => {
  const backend = fakeBackend({ state: 'running' })
  const { tmp, ctx, host } = await makeHost({ backend })
  try {
    assert.equal(ctx.route('exact', '/dsh-quantlab/status').handler.constructor.name, 'AsyncFunction')
    const res = await callRoute(ctx.route('exact', '/dsh-quantlab/status').handler, { method: 'GET' })
    assert.equal(res.statusCode, 200)
    const payload = res.json()
    assert.equal(payload.ok, true)
    assert.equal(payload.state, 'running')
    assert.equal(payload.url, 'http://127.0.0.1:8123')
    assert.equal(payload.port, 8123)
    assert.equal(payload.pid, 4321)
    assert.equal(payload.llmBridge, 'ready')
    assert.equal(payload.provider, PROVIDER_KEY)
    assert.equal(payload.providerName, DEFAULT_PROVIDER_NAME)
    assert.equal(typeof payload.logTail, 'string')
    assert.equal(typeof payload.configPath, 'string')
    assert.equal(payload.providerRegistration.ok, false, '还没登记前是 false')
    assert.equal(host.toolNames().length, 7)
    assert.deepEqual(ctx.tools.map((tool) => tool.name).sort(), [
      'quantlab_api',
      'quantlab_backtest',
      'quantlab_export_report',
      'quantlab_import_github',
      'quantlab_market_bars',
      'quantlab_search_stock',
      'quantlab_status',
    ])
    assert.equal(typeof payload.resolvedTargetLabel, 'string', '模型名说人话：状态里带 resolvedTargetLabel')
  } finally {
    tmp.cleanup()
  }
})

test('POST /dsh-quantlab/start|stop|restart：驱动 backend 并返回最新状态', async () => {
  const backend = fakeBackend({ state: 'stopped' })
  const { tmp, ctx } = await makeHost({ backend })
  try {
    const start = await callRoute(ctx.route('exact', '/dsh-quantlab/start').handler, { method: 'POST' })
    assert.equal(start.json().state, 'running')
    assert.ok(backend.calls.includes('start'))

    const stop = await callRoute(ctx.route('exact', '/dsh-quantlab/stop').handler, { method: 'POST' })
    assert.equal(stop.json().state, 'stopped')
    assert.ok(backend.calls.includes('stop'))

    const restart = await callRoute(ctx.route('exact', '/dsh-quantlab/restart').handler, { method: 'POST' })
    assert.equal(restart.json().state, 'running')
    assert.ok(backend.calls.includes('restart'))

    // GET 一把 start 应 405
    const wrong = await callRoute(ctx.route('exact', '/dsh-quantlab/start').handler, { method: 'GET' })
    assert.equal(wrong.statusCode, 405)
  } finally {
    tmp.cleanup()
  }
})

test('GET/POST /dsh-quantlab/config：读配置；写配置落盘、更新 bridge.model、重启后端', async () => {
  const tmpHome = tempDir()
  const backend = fakeBackend({ state: 'running' })
  const bridge = fakeBridge()
  const { tmp, ctx } = await makeHost({ backend, bridge, home: tmpHome.dir })
  try {
    const got = await callRoute(ctx.route('exact', '/dsh-quantlab/config').handler, { method: 'GET' })
    assert.equal(got.statusCode, 200)
    const configPayload = got.json()
    assert.equal(configPayload.ok, true)
    assert.equal(configPayload.config.autoStart, true)
    assert.equal(configPayload.status.state, 'running')

    const newProject = join(tmpHome.dir, 'project-2')
    const posted = await callRoute(ctx.route('exact', '/dsh-quantlab/config').handler, {
      method: 'POST',
      body: { projectDir: newProject, autoStart: false, llmBridge: false, model: 'opencode-go/deepseek-v4.1-flash' },
    })
    assert.equal(posted.statusCode, 200)
    const payload = posted.json()
    assert.equal(payload.config.projectDir, newProject)
    assert.equal(payload.config.autoStart, false)
    assert.equal(payload.config.llmBridge, false)
    assert.equal(bridge.model, 'opencode-go/deepseek-v4.1-flash')
    assert.ok(bridge.calls.includes('invalidate'))
    assert.ok(backend.calls.some((call) => call && call.configure && call.configure.projectDir === newProject))

    const saved = JSON.parse(readFileSync(join(tmpHome.dir, 'quantlab', 'config.json'), 'utf8'))
    assert.equal(saved.projectDir, newProject)
    assert.equal(saved.llmBridge, false)

    // POST 非法 JSON → 400
    const bad = await callRoute(ctx.route('exact', '/dsh-quantlab/config').handler, { method: 'POST', body: '{oops' })
    assert.equal(bad.statusCode, 400)
  } finally {
    tmp.cleanup()
    tmpHome.cleanup()
  }
})

test('GET /dsh-quantlab/logs：返回文本尾 + 数组', async () => {
  const { tmp, ctx } = await makeHost({})
  try {
    const res = await callRoute(ctx.route('exact', '/dsh-quantlab/logs').handler, {
      method: 'GET',
      url: '/dsh-quantlab/logs?lines=20',
    })
    assert.equal(res.statusCode, 200)
    const payload = res.json()
    assert.deepEqual(payload.logTail, ['line-1', 'line-20'])
    assert.equal(payload.text, 'line-1\nline-20')
    assert.equal(payload.lines, 2)

    const wrong = await callRoute(ctx.route('exact', '/dsh-quantlab/logs').handler, { method: 'POST' })
    assert.equal(wrong.statusCode, 405)
  } finally {
    tmp.cleanup()
  }
})

test('模型桥路由：/llm/v1/chat/completions 与 /models 转给 bridge，其它 /llm/* 走 404', async () => {
  const bridge = fakeBridge()
  const { tmp, ctx } = await makeHost({ bridge })
  try {
    const completions = await callRoute(ctx.route('exact', '/dsh-quantlab/llm/v1/chat/completions').handler, {
      method: 'POST',
      body: { messages: [{ role: 'user', content: 'hi' }] },
    })
    assert.equal(completions.json().bridge, 'completions')

    const models = await callRoute(ctx.route('exact', '/dsh-quantlab/llm/v1/models').handler, { method: 'GET' })
    assert.equal(models.json().bridge, 'models')

    const missing = await callRoute(ctx.route('prefix', '/dsh-quantlab/llm').handler, {
      method: 'GET',
      url: '/dsh-quantlab/llm/v1/unknown',
    })
    assert.equal(missing.json().bridge, 'notfound')
    assert.ok(bridge.calls.includes('notfound'))
  } finally {
    tmp.cleanup()
  }
})

// ---------------------------------------------------------------------------
// 平台模型服务登记
// ---------------------------------------------------------------------------

/** 假平台：记录 provider 登记调用。 */
function platformFetch({ existing = [], ready = true, routingDefault = '' } = {}) {
  const calls = []
  const reply = (status, body) => ({
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return JSON.stringify(body)
    },
  })
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || 'GET', headers: init.headers || {}, body: init.body })
    if (url.includes('/api/config/providers')) return reply(200, { ok: true, provider: { name: PROVIDER_KEY } })
    if (url.includes('/api/config/routing')) return reply(200, { ok: true })
    if (url.includes('/api/config/models')) {
      return reply(200, {
        models: [{ provider: PROVIDER_KEY, model: 'dsh-default', target: `${PROVIDER_KEY}/dsh-default`, ready }],
        routing: { default: routingDefault },
      })
    }
    if (url.endsWith('/api/config')) return reply(200, { providers: existing })
    return reply(404, {})
  }
  return { calls, fetchImpl }
}

test('平台模型登记：已有 provider 且 base_url 一致、上下文窗口已登记 → 跳过', async () => {
  const platform = platformFetch({
    existing: [
      {
        name: PROVIDER_KEY,
        base_url: 'http://127.0.0.1:56389/dsh-quantlab/llm/v1',
        model_settings: { 'dsh-default': { context_window: 128000 } },
      },
    ],
    routingDefault: `${PROVIDER_KEY}/dsh-default`,
  })
  const { tmp, host } = await makeHost({ backend: fakeBackend({ state: 'running' }), fetch: platform.fetchImpl })
  try {
    const result = await host.registerPlatformProvider()
    assert.equal(result.ok, true)
    assert.match(String(result.skipped), /已登记/)
    assert.equal(platform.calls.filter((call) => call.method === 'POST').length, 0, '不该重复 POST')
  } finally {
    tmp.cleanup()
  }
})

test('平台模型登记：POST /api/config/providers 带本地写头 + 正确 body；routing 为空时补默认路由', async () => {
  const platform = platformFetch({ existing: [], routingDefault: '' })
  const { tmp, host } = await makeHost({ backend: fakeBackend({ state: 'running' }), fetch: platform.fetchImpl })
  try {
    const result = await host.registerPlatformProvider()
    assert.equal(result.ok, true)
    const post = platform.calls.find((call) => call.method === 'POST')
    assert.ok(post)
    assert.match(post.url, /\/api\/config\/providers$/)
    assert.equal(post.headers['X-Requested-With'], 'quantlab-desktop')
    assert.match(String(post.headers['content-type']), /charset=utf-8/)
    const body = JSON.parse(post.body)
    const { model_settings: modelSettings, ...rest } = body
    assert.deepEqual(rest, {
      name: PROVIDER_KEY,
      label: DEFAULT_PROVIDER_NAME,
      type: 'openai_compatible',
      base_url: 'http://127.0.0.1:56389/dsh-quantlab/llm/v1',
      api_key: PROVIDER_KEY,
      models: ['dsh-default'],
      scope: 'workspace',
    })
    // 平台默认 32k 会让 Agent 过早压缩历史：登记时显式给上下文窗口与展示名。
    assert.ok(modelSettings && modelSettings['dsh-default'], 'body 应带 model_settings')
    assert.equal(modelSettings['dsh-default'].label, 'DSH 模型桥')
    assert.ok(Number(modelSettings['dsh-default'].context_window) >= 8000)
    const routing = platform.calls.find((call) => call.url.includes('/api/config/routing'))
    assert.ok(routing, '默认路由为空时应 PUT routing')
    assert.equal(routing.method, 'PUT')
    assert.deepEqual(JSON.parse(routing.body), { default: `${PROVIDER_KEY}/dsh-default` })
  } finally {
    tmp.cleanup()
  }
})

test('平台模型登记：已有默认路由指向有效 provider 时不覆盖', async () => {
  // models 里存在 x/y 且 routing.default=x/y → 视为用户有效选择，不 PUT routing
  const calls = []
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || 'GET', body: init.body })
    const reply = (body) => ({ ok: true, status: 200, async text() { return JSON.stringify(body) } })
    if (url.includes('/api/config/providers')) return reply({ ok: true })
    if (url.includes('/api/config/models')) {
      return reply({
        models: [
          { provider: 'x', model: 'y', target: 'x/y', ready: true },
          { provider: PROVIDER_KEY, model: 'dsh-default', target: `${PROVIDER_KEY}/dsh-default`, ready: true },
        ],
        routing: { default: 'x/y' },
      })
    }
    if (url.includes('/api/config/routing')) return reply({ ok: true })
    if (url.endsWith('/api/config')) return reply({ providers: [] })
    return { ok: false, status: 404, async text() { return '{}' } }
  }
  const { tmp, host } = await makeHost({ backend: fakeBackend({ state: 'running' }), fetch: fetchImpl })
  try {
    const result = await host.registerPlatformProvider()
    assert.equal(result.ok, true)
    assert.equal(result.ready, true)
    assert.ok(!calls.some((call) => call.url.includes('/api/config/routing')), '不该覆盖用户已有的有效默认路由')
    assert.ok(calls.some((call) => call.method === 'POST' && call.url.includes('/api/config/providers')))
  } finally {
    tmp.cleanup()
  }
})

test('平台模型登记：llmBridge 关闭 / 后端未运行 / bridge 不可用 → 跳过且不报错', async () => {
  const homeTmp = tempDir()
  try {
    const platform = platformFetch({})
    const offBridge = fakeBridge()
    writeConfigFile(join(homeTmp.dir, 'quantlab', 'config.json'), {
      ...normalizeConfig(defaultConfig({})),
      llmBridge: false,
    })
    const { ctx: ctx1, host: host1 } = await makeHost({
      backend: fakeBackend({ state: 'running' }),
      bridge: offBridge,
      fetch: platform.fetchImpl,
      home: homeTmp.dir,
    })
    void ctx1
    const off = await host1.registerPlatformProvider()
    assert.equal(off.ok, false)
    assert.match(String(off.skipped), /llmBridge/)

    const hostTmp = tempDir()
    try {
      const { ctx: ctx2, host: host2 } = await makeHost({
        backend: fakeBackend({ state: 'stopped' }),
        fetch: platform.fetchImpl,
        home: hostTmp.dir,
      })
      void ctx2
      const stopped = await host2.registerPlatformProvider()
      assert.equal(stopped.ok, false)
      assert.match(String(stopped.skipped), /后端未运行/)

      const unavailable = await makeHost({ backend: fakeBackend({ state: 'running' }), bridge: fakeBridge({ available: false }), fetch: platform.fetchImpl })
      try {
        const result = await unavailable.host.registerPlatformProvider()
        assert.equal(result.ok, false)
        assert.match(String(result.skipped), /模型服务不可用/)
      } finally {
        unavailable.tmp.cleanup()
      }
    } finally {
      hostTmp.cleanup()
    }
  } finally {
    homeTmp.cleanup()
  }
})

test('自动启动：autoStart=true 时 setup 会 start 后端并尝试登记 provider', async () => {
  const platform = platformFetch({ existing: [{ name: PROVIDER_KEY, base_url: 'http://127.0.0.1:56389/dsh-quantlab/llm/v1' }] })
  const tmpHome = tempDir()
  try {
    writeConfigFile(join(tmpHome.dir, 'quantlab', 'config.json'), {
      ...normalizeConfig(defaultConfig({})),
      autoStart: true,
    })
    const ctx = mockCtx()
    const backend = fakeBackend({ state: 'stopped' })
    const host = await setup(ctx, {
      home: tmpHome.dir,
      backend,
      bridge: fakeBridge(),
      defineTool,
      fetch: platform.fetchImpl,
      env: {},
      sleep: async () => {},
    })
    assert.ok(host.autoStartPromise, 'autoStart 应产生 promise')
    const status = await host.autoStartPromise
    assert.equal(status.state, 'running')
    assert.ok(backend.calls.includes('start'))
    assert.equal(host.status().providerRegistration.ok, true)
  } finally {
    tmpHome.cleanup()
  }
})

// ---------------------------------------------------------------------------
// 卸载
// ---------------------------------------------------------------------------

test('dispose：注销路由/工具并停掉后端', async () => {
  const backend = fakeBackend({ state: 'running' })
  const { tmp, ctx, host } = await makeHost({ backend })
  try {
    assert.equal(ctx.tools.length, 7, '7 个工具：status/market_bars/search_stock/api/backtest/export_report/import_github')
    assert.ok(ctx.routes.size >= 8)
    host.dispose()
    assert.ok(backend.calls.includes('dispose'))
    assert.equal(ctx.tools.length, 0, '工具应被注销')
    assert.equal(ctx.routes.size, 0, '路由应被注销')
    // ctx.effect 注册的卸载器也应生效（再调一次不抛）
    for (const disposer of ctx.disposers) disposer()
    assert.equal(ctx.tools.length, 0)
  } finally {
    tmp.cleanup()
  }
})

test('llm/adapters-updated：DSH 模型拓扑变化时清空模型解析缓存', async () => {
  const bridge = fakeBridge()
  const { tmp, ctx, host } = await makeHost({ bridge })
  try {
    assert.equal(typeof ctx.emit, 'function')
    ctx.emit('llm/adapters-updated')
    assert.ok(bridge.calls.includes('invalidate'), '应调用 bridge.invalidate()')
    assert.equal(host.status().state, 'stopped')
  } finally {
    tmp.cleanup()
  }
})

test('ctx 缺少 webServer/tools 时不炸（只做配置）', async () => {
  const tmp = tempDir()
  try {
    const ctx = { effect: (fn) => fn(), get: () => null }
    const host = await setup(ctx, {
      home: tmp.dir,
      backend: fakeBackend(),
      bridge: fakeBridge(),
      autoStart: false,
      env: {},
    })
    assert.equal(host.toolNames().length, 0)
    assert.equal(host.status().state, 'stopped')
  } finally {
    tmp.cleanup()
  }
})

test('mockReq/mockRes 自身可用（helpers sanity）', async () => {
  const res = mockRes()
  res.writeHead(200, { 'content-type': 'application/json' })
  res.end(JSON.stringify({ hello: 'world' }))
  assert.equal(mockReq({ method: 'GET' }).method, 'GET')
  assert.deepEqual(res.json(), { hello: 'world' })
})
