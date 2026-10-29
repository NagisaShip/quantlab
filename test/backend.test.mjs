/**
 * lib/backend.js 单测：状态机、复用 instance.json、健康检查超时、停止/兜底 kill。
 * 全部用注入的假 spawn/fetch/sleep/端口/进程探测，不真起 Python。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

import { createBackend, QuantLabBackend, isPidAlive } from '../lib/backend.js'
import { tempDir } from './helpers.mjs'

/** 假子进程。 */
class FakeChild extends EventEmitter {
  constructor(pid = 4242) {
    super()
    this.pid = pid
    this.killed = false
    this.unrefs = 0
  }

  unref() {
    this.unrefs += 1
  }

  kill() {
    this.killed = true
  }
}

/** 假 fetch 响应。 */
function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async text() {
      return typeof body === 'string' ? body : JSON.stringify(body ?? {})
    },
  }
}

/**
 * 搭一个可控的环境。
 * @param {object} [opts]
 */
function harness(opts = {}) {
  const tmp = tempDir()
  const projectDir = join(tmp.dir, 'project')
  mkdirSync(join(projectDir, '.quantlab'), { recursive: true })
  const calls = []
  const state = {
    health: opts.health ?? 'ok', // ok | fail | down
    infoRoot: opts.infoRoot === undefined ? projectDir : opts.infoRoot,
    infoMode: opts.infoMode === undefined ? 'local' : opts.infoMode,
    alive: opts.alive ?? false,
    pid: opts.pid ?? 4242,
    now: 1_000_000,
    shutdownOk: opts.shutdownOk !== false,
    child: null,
  }
  const fetch = async (url, init = {}) => {
    calls.push({ url: String(url), method: init.method || 'GET', headers: init.headers || {}, body: init.body })
    if (url.includes('/api/healthz')) {
      if (opts.exitOnStart && state.child) {
        const child = state.child
        state.child = null
        child.emit('exit', 1, null)
        return response(503, { status: 'down' })
      }
      if (state.health === 'ok') return response(200, { status: 'ok' })
      if (state.health === 'down') throw new Error('ECONNREFUSED')
      return response(503, { status: 'starting' })
    }
    if (url.includes('/api/local/info')) {
      if (state.infoMode === 'down') throw new Error('ECONNREFUSED')
      return response(200, { mode: state.infoMode, root: state.infoRoot, pid: state.pid })
    }
    if (url.includes('/api/local/shutdown')) {
      if (!state.shutdownOk) return response(500, { ok: false })
      state.alive = false
      return response(200, { ok: true })
    }
    return response(404, {})
  }
  const spawn = (cmd, args, options) => {
    calls.push({ spawn: true, cmd, args, options })
    const child = new FakeChild(state.pid)
    state.child = child
    if (opts.exitOnStart) process.nextTick(() => child.emit('exit', 1, null))
    return child
  }
  const backend = createBackend({
    projectDir,
    python: 'python',
    srcDir: join(tmp.dir, 'src'),
    spawn,
    fetch,
    sleep: async () => {},
    getFreePort: async () => opts.port ?? 49_123,
    isPidAlive: () => state.alive,
    killPid: (pid) => {
      calls.push({ kill: pid })
      state.alive = false
      return true
    },
    onState: () => {},
    platform: 'win32',
    healthTimeoutMs: opts.healthTimeoutMs ?? 10_000,
    pollIntervalMs: 100,
    requestTimeoutMs: 1_000,
    now: () => {
      state.now += 50
      return state.now
    },
  })
  return { tmp, projectDir, backend, state, calls }
}

test('start：空闲端口 → spawn Python → 健康检查通过 → running', async () => {
  const h = harness()
  try {
    const status = await h.backend.start()
    assert.equal(status.state, 'running')
    assert.equal(status.url, 'http://127.0.0.1:49123')
    assert.equal(status.port, 49_123)
    assert.equal(status.pid, 4242)
    assert.equal(status.ok, true)
    assert.equal(h.backend.running, true)

    const spawnCall = h.calls.find((call) => call.spawn)
    assert.ok(spawnCall, '应该 spawn 了 Python')
    assert.equal(spawnCall.cmd, 'python')
    assert.deepEqual(spawnCall.args.slice(0, 4), ['-m', 'quant_agent.local.launcher', '--serve', '--project'])
    assert.equal(spawnCall.args[5], '--port')
    assert.equal(spawnCall.args[6], '49123')
    assert.equal(spawnCall.options.env.QUANTLAB_MODE, 'local')
    assert.equal(spawnCall.options.env.QAGENT_SEED_BUILTINS, '0')
    assert.ok(String(spawnCall.options.env.PYTHONPATH).endsWith('src'))
    assert.equal(spawnCall.options.windowsHide, true)
    assert.equal(spawnCall.options.detached, false, 'Windows 不 detached')

    const health = h.calls.filter((call) => String(call.url || '').includes('/api/healthz'))
    assert.ok(health.length >= 1)

    // 第二次 start 直接返回 running，不重复 spawn
    const again = await h.backend.start()
    assert.equal(again.state, 'running')
    assert.equal(h.calls.filter((call) => call.spawn).length, 1)
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('start：复用 instance.json 里仍存活、且 root 一致的实例（不再 spawn）', async () => {
  const h = harness({ alive: true, pid: 7788 })
  try {
    writeFileSync(
      join(h.projectDir, '.quantlab', 'instance.json'),
      JSON.stringify({ pid: 7788, port: 49_555, started_at: 1 }),
      'utf8',
    )
    const status = await h.backend.start()
    assert.equal(status.state, 'running')
    assert.equal(status.reused, true)
    assert.equal(status.port, 49_555)
    assert.equal(status.pid, 7788)
    assert.equal(h.calls.filter((call) => call.spawn).length, 0, '复用不该 spawn')
    assert.ok(h.calls.some((call) => String(call.url || '').includes('/api/local/info')))
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('start：instance.json 的 root 不一致 → 不复用，改 spawn 新实例', async () => {
  const h = harness({ alive: true, pid: 7788, infoRoot: 'C:\\somewhere\\else' })
  try {
    writeFileSync(
      join(h.projectDir, '.quantlab', 'instance.json'),
      JSON.stringify({ pid: 7788, port: 49_555 }),
      'utf8',
    )
    const status = await h.backend.start()
    assert.equal(status.state, 'running')
    assert.equal(status.reused, false)
    assert.equal(h.calls.filter((call) => call.spawn).length, 1)
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('start：健康检查超时 → state=error、带错误信息、并清理进程', async () => {
  const h = harness({ health: 'fail', healthTimeoutMs: 400 })
  try {
    const status = await h.backend.start()
    assert.equal(status.state, 'error')
    assert.match(status.error, /等待后端就绪超时/)
    assert.equal(h.backend.running, false)
    assert.ok(h.calls.some((call) => call.kill === 4242), '超时后应兜底 kill')
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('start：子进程启动后立即退出 → state=error，错误里带退出码', async () => {
  const h = harness({ health: 'fail', exitOnStart: true })
  try {
    const status = await h.backend.start()
    assert.equal(status.state, 'error')
    assert.match(status.error, /立即退出/)
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('stop：先 POST /api/local/shutdown（带本地写头），进程退出后 stopped', async () => {
  const h = harness()
  try {
    await h.backend.start()
    const status = await h.backend.stop()
    assert.equal(status.state, 'stopped')
    assert.equal(status.url, '')
    assert.equal(status.pid, 0)
    const shutdown = h.calls.find((call) => String(call.url || '').includes('/api/local/shutdown'))
    assert.ok(shutdown, '应该调 shutdown')
    assert.equal(shutdown.method, 'POST')
    assert.equal(shutdown.headers['X-Requested-With'], 'quantlab-desktop')
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('stop：shutdown 失败且进程不退出 → kill 兜底，状态仍是 stopped', async () => {
  const h = harness({ shutdownOk: false, alive: true })
  try {
    // 走复用分支：不 spawn、没有 exit 事件，只能靠 kill 兜底
    writeFileSync(
      join(h.projectDir, '.quantlab', 'instance.json'),
      JSON.stringify({ pid: 7788, port: 49_555 }),
      'utf8',
    )
    h.state.pid = 7788
    await h.backend.start()
    assert.equal(h.backend.state, 'running')
    h.backend.stopTimeoutMs = 300
    const status = await h.backend.stop()
    assert.equal(status.state, 'stopped')
    assert.ok(h.calls.some((call) => call.kill === 7788), '应该 kill 兜底')
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('restart：先停再起', async () => {
  const h = harness()
  try {
    await h.backend.start()
    const status = await h.backend.restart()
    assert.equal(status.state, 'running')
    assert.equal(h.calls.filter((call) => call.spawn).length, 2)
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('dispose：同步杀掉子进程并把状态清成 stopped', async () => {
  const h = harness()
  try {
    await h.backend.start()
    h.backend.dispose()
    assert.equal(h.backend.state, 'stopped')
    assert.equal(h.backend.running, false)
    assert.ok(h.calls.some((call) => call.kill === 4242))
  } finally {
    h.tmp.cleanup()
  }
})

test('status/logTail：日志尾部能从 dsh-quantlab.log 读回来', async () => {
  const h = harness()
  try {
    await h.backend.start()
    const lines = h.backend.logTail(10)
    assert.ok(Array.isArray(lines))
    assert.ok(lines.length >= 1)
    assert.ok(lines.join('\n').includes('[dsh-quantlab]'))
    const status = h.backend.status()
    assert.equal(typeof status.logTail, 'string')
    assert.ok(status.logTail.includes('[dsh-quantlab]'))
  } finally {
    h.backend.dispose()
    h.tmp.cleanup()
  }
})

test('configure：改 projectDir 后日志路径跟着变', () => {
  const h = harness()
  try {
    h.backend.configure({ projectDir: join(h.tmp.dir, 'another') })
    assert.ok(h.backend.logFile.endsWith(join('another', '.quantlab', 'dsh-quantlab.log')))
    assert.equal(h.backend.projectDir, join(h.tmp.dir, 'another'))
  } finally {
    h.tmp.cleanup()
  }
})

test('isPidAlive：正常/非法 pid 不抛异常', () => {
  assert.equal(isPidAlive(0), false)
  assert.equal(isPidAlive(-1), false)
  assert.equal(isPidAlive(Number.NaN), false)
  assert.equal(isPidAlive(process.pid), true)
})

test('没有 projectDir 时构造直接报错', () => {
  assert.throws(() => new QuantLabBackend({}), /需要 projectDir/)
})
