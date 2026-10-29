/**
 * dsh-quantlab · 本地后端进程管理（Host 半端，不依赖 DSH ctx）
 * ============================================================================
 * 管一个 `python -m quant_agent.local.launcher --serve --project <dir> --port <port>`
 * 的本地 QuantLab 服务：
 *   · 复用：先看 `<project>/.quantlab/instance.json` 里的实例是否还活着
 *     （`GET /api/local/info` 校验 root 与本次 projectDir 一致才复用）
 *   · 启动：拿一个空闲端口 → spawn（env: PYTHONPATH / QUANTLAB_MODE=local /
 *     QAGENT_SEED_BUILTINS=0，日志追加到 `<project>/.quantlab/dsh-quantlab.log`）
 *   · 就绪：轮询 `GET /api/healthz`（默认 ≤60s，250ms 一次）
 *   · 停止：先 `POST /api/local/shutdown`（本地模式写接口必须带
 *     `X-Requested-With: quantlab-desktop`），超时再 taskkill / SIGTERM 兜底
 *   · 状态机：starting / running / stopped / error（附 pid / url / port /
 *     logTail / error / startedAt）
 *
 * 纯 Node ESM，没有任何 DSH 依赖；spawn / fetch / sleep / 端口探测 / 进程探测
 * / taskkill 都可注入，方便 `node --test` 单测（见 test/backend.test.mjs）。
 */

import { spawn as nodeSpawn, spawnSync as nodeSpawnSync } from 'node:child_process'
import { createServer } from 'node:net'
import {
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  statSync,
  writeSync,
} from 'node:fs'
import { dirname, join, resolve } from 'node:path'

/** 状态机取值（与 docs 4.3 的 status.state 对齐）。 */
export const STATES = Object.freeze(['starting', 'running', 'stopped', 'error'])

/** 本地模式写接口要求的请求头；缺了会被中间件 403。 */
export const LOCAL_WRITE_HEADER = 'X-Requested-With'
export const LOCAL_WRITE_VALUE = 'quantlab-desktop'

/** 等待就绪时读取日志尾部的行数上限。 */
const LOG_TAIL_BYTES = 64 * 1024

/** 默认 sleep：可被注入替换，测试里就是同步 resolve。 */
const defaultSleep = (ms) => new Promise((done) => setTimeout(done, ms))

/**
 * 默认空闲端口探测：监听 127.0.0.1:0，读回 OS 分配的端口再关掉。
 * 与 Python 侧 `free_port()` 同思路（关掉后有小概率被别的进程抢走，
 * 那种情况由健康检查超时兜底）。
 * @returns {Promise<number>}
 */
export function getFreePort() {
  return new Promise((done, fail) => {
    const server = createServer()
    server.unref()
    server.once('error', fail)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = address && typeof address === 'object' ? address.port : 0
      server.close(() => (port ? done(port) : fail(new Error('无法分配空闲端口'))))
    })
  })
}

/** 默认进程存活探测（信号 0 不发信号，只做存在性检查）。 */
export function isPidAlive(pid) {
  if (!Number.isFinite(pid) || pid <= 0) return false
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    // EPERM = 进程存在但没权限发信号，也算活着。
    return Boolean(error && error.code === 'EPERM')
  }
}

/** 默认终止：Windows 走 taskkill /T /F，其它平台对进程组发 SIGTERM。 */
export function killPid(pid, platform = process.platform) {
  if (!Number.isFinite(pid) || pid <= 0) return false
  try {
    if (platform === 'win32') {
      nodeSpawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
      return true
    }
    try {
      process.kill(-pid, 'SIGTERM')
    } catch {
      process.kill(pid, 'SIGTERM')
    }
    return true
  } catch {
    return false
  }
}

/** 真正 spawn 出来的子进程登记表：DSH 进程意外退出时兜底收尸。 */
const LIVE_CHILDREN = new Set()
let exitHookInstalled = false

function installExitHook() {
  if (exitHookInstalled) return
  exitHookInstalled = true
  process.on('exit', () => {
    for (const pid of LIVE_CHILDREN) {
      try {
        killPid(pid)
      } catch {
        /* 退出阶段不能再抛 */
      }
    }
    LIVE_CHILDREN.clear()
  })
}

/** 读文件的最后 N 字节并按行切（日志尾部）。 */
function readTail(file, maxBytes = LOG_TAIL_BYTES) {
  let fd = null
  try {
    if (!existsSync(file)) return []
    const size = statSync(file).size
    if (size <= 0) return []
    const start = Math.max(0, size - maxBytes)
    const length = size - start
    const buffer = Buffer.alloc(length)
    fd = openSync(file, 'r')
    readSync(fd, buffer, 0, length, start)
    const lines = buffer.toString('utf8').split(/\r?\n/)
    if (lines.length && lines[lines.length - 1] === '') lines.pop()
    return lines
  } catch {
    return []
  } finally {
    if (fd !== null) {
      try {
        closeSync(fd)
      } catch {
        /* ignore */
      }
    }
  }
}

/**
 * @typedef {object} BackendOptions
 * @property {string} projectDir QuantLab 项目目录（数据落在 <projectDir>/.quantlab）
 * @property {string} [python] Python 可执行文件，默认 `python`
 * @property {string} [srcDir] PYTHONPATH，即 QuantLab 的 src 目录
 * @property {string} [logFile] 日志文件，默认 `<projectDir>/.quantlab/dsh-quantlab.log`
 * @property {Function} [spawn] 可注入 spawn（测试用假进程）
 * @property {Function} [fetch] 可注入 fetch
 * @property {Function} [sleep] 可注入 sleep
 * @property {Function} [getFreePort] 可注入端口探测
 * @property {Function} [isPidAlive] 可注入进程探测
 * @property {Function} [killPid] 可注入终止器
 * @property {Function} [onState] 状态变化回调（(status) => void）
 * @property {string} [platform] process.platform 覆盖（测试）
 * @property {number} [healthTimeoutMs] 就绪超时，默认 60000
 * @property {number} [pollIntervalMs] 健康检查间隔，默认 250
 * @property {number} [stopTimeoutMs] 优雅停止等待，默认 6000
 * @property {number} [requestTimeoutMs] 单次 HTTP 超时，默认 3000
 * @property {number} [unrefDelayMs] spawn 后 unref 前的等待，默认 0
 */

export class QuantLabBackend {
  /** @param {BackendOptions} options */
  constructor(options = {}) {
    if (!options.projectDir) throw new Error('QuantLabBackend 需要 projectDir')
    this.projectDir = resolve(options.projectDir)
    this.python = options.python || 'python'
    this.srcDir = options.srcDir ? resolve(options.srcDir) : ''
    this.logFile = options.logFile || join(this.projectDir, '.quantlab', 'dsh-quantlab.log')
    this._spawn = options.spawn || nodeSpawn
    this._realSpawn = !options.spawn
    this._fetch = options.fetch || globalThis.fetch
    this._sleep = options.sleep || defaultSleep
    this._getFreePort = options.getFreePort || getFreePort
    this._isPidAlive = options.isPidAlive || isPidAlive
    this._killPid = options.killPid || ((pid) => killPid(pid, this.platform))
    this._onState = typeof options.onState === 'function' ? options.onState : () => {}
    this.platform = options.platform || process.platform
    this.healthTimeoutMs = options.healthTimeoutMs ?? 60_000
    this.pollIntervalMs = options.pollIntervalMs ?? 250
    this.stopTimeoutMs = options.stopTimeoutMs ?? 6_000
    this.requestTimeoutMs = options.requestTimeoutMs ?? 3_000
    this.unrefDelayMs = options.unrefDelayMs ?? 0
    this._now = typeof options.now === 'function' ? options.now : () => Date.now()

    /** @type {'starting'|'running'|'stopped'|'error'} */
    this._state = 'stopped'
    this._url = ''
    this._port = 0
    this._pid = 0
    this._startedAt = 0
    this._error = ''
    this._reused = false
    this._child = null
    this._childExited = null
    this._exitInfo = null
    this._logFd = null
    this._startPromise = null
    this._stopPromise = null
    this._disposed = false
    this._childExited = null
  }

  // ── 只读状态 ────────────────────────────────────────────────────────────

  get state() {
    return this._state
  }

  get url() {
    return this._state === 'running' ? this._url : ''
  }

  get port() {
    return this._port
  }

  get pid() {
    return this._pid
  }

  /** 后端是否可服务（工具/模型登记据此判断）。 */
  get running() {
    return this._state === 'running' && Boolean(this._url)
  }

  /** 当前状态快照（status 路由直接返回它 + 插件补充字段）。 */
  status() {
    return {
      ok: this._state === 'running',
      state: this._state,
      url: this._url,
      port: this._port,
      pid: this._pid,
      projectDir: this.projectDir,
      startedAt: this._startedAt,
      error: this._error,
      reused: this._reused,
      logTail: this.logTail(40).join('\n'),
    }
  }

  /** 日志尾部（默认 60 行）。 */
  logTail(lines = 60) {
    const all = readTail(this.logFile)
    const n = Math.max(1, Math.min(2000, Number(lines) || 60))
    return all.slice(-n)
  }

  /** 改配置（projectDir / python / srcDir）时重建实例用；调用前应先 stop。 */
  configure(patch = {}) {
    if (patch.projectDir) this.projectDir = resolve(patch.projectDir)
    if (patch.python) this.python = patch.python
    if (typeof patch.srcDir === 'string') this.srcDir = patch.srcDir ? resolve(patch.srcDir) : ''
    this.logFile = join(this.projectDir, '.quantlab', 'dsh-quantlab.log')
    return this
  }

  // ── 启动 / 停止 ────────────────────────────────────────────────────────

  /**
   * 启动（或复用）后端。幂等：并发调用共享同一次启动。
   * 失败不抛异常，返回 state=error 的状态快照。
   */
  async start() {
    if (this._disposed) throw new Error('后端管理器已释放')
    if (this._state === 'running') return this.status()
    if (this._startPromise) return this._startPromise
    this._startPromise = this._doStart()
    try {
      return await this._startPromise
    } finally {
      this._startPromise = null
    }
  }

  async _doStart() {
    this._setState('starting', { error: '' })
    // 没跑完的 stop 先等它结束，避免同时 shutdown + spawn
    if (this._stopPromise) {
      try {
        await this._stopPromise
      } catch {
        /* ignore */
      }
    }
    try {
      const reused = await this._tryReuse()
      if (reused) {
        this._port = reused.port
        this._pid = reused.pid
        this._url = `http://127.0.0.1:${reused.port}`
        this._startedAt = this._now()
        this._reused = true
        this._setState('running', { error: '' })
        return this.status()
      }
      await this._spawnServe()
      return this.status()
    } catch (error) {
      const message = String((error && error.message) || error)
      this._error = message
      this._setState('error', { error: message })
      // 起了一半的进程不要留
      this._killChildImmediate()
      return this.status()
    }
  }

  /** 复用 `.quantlab/instance.json` 里仍存活、且 project root 一致的实例。 */
  async _tryReuse() {
    const file = join(this.projectDir, '.quantlab', 'instance.json')
    let record = null
    try {
      record = JSON.parse(readFileSync(file, 'utf8'))
    } catch {
      return null
    }
    const port = Number(record && record.port)
    const pid = Number(record && record.pid)
    if (!port || !Number.isFinite(port)) return null
    if (pid && !this._isPidAlive(pid)) return null
    const info = await this._getJson(`http://127.0.0.1:${port}/api/local/info`, 2_000)
    if (!info || info.mode !== 'local') return null
    let sameRoot = false
    try {
      sameRoot = resolve(String(info.root || '')) === this.projectDir
    } catch {
      sameRoot = false
    }
    if (!sameRoot) return null
    return { port, pid: pid || Number(info.pid) || 0 }
  }

  /** spawn Python 服务并等健康检查通过。 */
  async _spawnServe() {
    const port = await this._getFreePort()
    if (!port) throw new Error('无法分配空闲端口')
    const home = join(this.projectDir, '.quantlab')
    mkdirSync(home, { recursive: true })
    mkdirSync(dirname(this.logFile), { recursive: true })
    this._logFd = openSync(this.logFile, 'a')
    const env = {
      ...process.env,
      PYTHONPATH: this.srcDir || process.env.PYTHONPATH || '',
      QUANTLAB_MODE: 'local',
      QAGENT_SEED_BUILTINS: '0',
    }
    if (this.srcDir) env.QUANTLAB_SRC = this.srcDir
    const args = [
      '-m',
      'quant_agent.local.launcher',
      '--serve',
      '--project',
      this.projectDir,
      '--port',
      String(port),
    ]
    this._writeLogLine(`[dsh-quantlab] 启动后端：${this.python} ${args.join(' ')}（project=${this.projectDir}）`)
    let child
    try {
      child = this._spawn(this.python, args, {
        cwd: this.projectDir,
        env,
        windowsHide: true,
        // Windows 上新开进程组会换到隐藏窗口；其它平台用进程组方便整组 kill。
        detached: this.platform !== 'win32',
        stdio: ['ignore', this._logFd, this._logFd],
      })
    } catch (error) {
      this._closeLogFd()
      throw new Error(`启动 Python 失败（${this.python}）：${(error && error.message) || error}`)
    }
    if (!child || typeof child.on !== 'function') {
      this._closeLogFd()
      throw new Error('spawn 没有返回子进程句柄')
    }
    this._child = child
    this._exitInfo = null
    this._childExited = new Promise((done) => {
      child.once('exit', (code, signal) => {
        this._exitInfo = { code, signal }
        done({ code, signal })
      })
      child.once('error', (error) => {
        this._exitInfo = { code: null, signal: null, error }
        done({ code: null, signal: null, error })
      })
    })
    this._pid = Number(child.pid) || 0
    this._port = port
    this._url = `http://127.0.0.1:${port}`
    if (this._realSpawn && this._pid) {
      LIVE_CHILDREN.add(this._pid)
      installExitHook()
    }
    if (this.unrefDelayMs > 0) await this._sleep(this.unrefDelayMs)
    try {
      if (typeof child.unref === 'function') child.unref()
    } catch {
      /* ignore */
    }
    await this._waitHealthy()
    this._startedAt = this._now()
    this._reused = false
    this._setState('running', { error: '' })
    this._writeLogLine(`[dsh-quantlab] 后端就绪：${this._url}（pid=${this._pid}）`)
    return this.status()
  }

  /** 轮询 /api/healthz，直到超时或子进程先退出。 */
  async _waitHealthy() {
    const deadline = this._now() + this.healthTimeoutMs
    while (this._now() < deadline) {
      if (this._exitInfo) {
        const detail = this._exitInfo
        const reason = detail.error
          ? String(detail.error.message || detail.error)
          : `退出码 ${detail.code ?? '?'}${detail.signal ? `（${detail.signal}）` : ''}`
        throw new Error(`QuantLab 后端启动后立即退出：${reason}${this._logHint()}`)
      }
      const healthy = await this._checkHealth()
      if (healthy) return true
      // 子进程可能在健康检查等待期间退出
      if (this._exitInfo) continue
      await this._sleep(this.pollIntervalMs)
    }
    throw new Error(`等待后端就绪超时（${Math.round(this.healthTimeoutMs / 1000)}s）${this._logHint()}`)
  }

  async _checkHealth() {
    const result = await this._request(`${this._url}/api/healthz`, { timeoutMs: 2_000 })
    if (!result || !result.ok) return false
    if (result.status < 200 || result.status >= 500) return false
    return true
  }

  _logHint() {
    const tail = this.logTail(6)
    return tail.length ? `；日志尾部：${tail.join(' ⏎ ').slice(0, 600)}` : `；日志：${this.logFile}`
  }

  /**
   * 停止后端：先优雅 shutdown，再 kill 兜底。幂等。
   */
  async stop() {
    if (this._stopPromise) return this._stopPromise
    if (this._state === 'stopped' || this._state === 'error') {
      // error 状态下也尽量清理残留
      if (!this._pid && !this._child) {
        this._setState('stopped', { error: '' })
        return this.status()
      }
    }
    this._stopPromise = this._doStop()
    try {
      return await this._stopPromise
    } finally {
      this._stopPromise = null
    }
  }

  async _doStop() {
    const pid = this._pid
    const url = this._url
    try {
      if (url) {
        this._writeLogLine(`[dsh-quantlab] 请求后端关闭：${url}`)
        await this._request(`${url}/api/local/shutdown`, {
          method: 'POST',
          headers: {
            [LOCAL_WRITE_HEADER]: LOCAL_WRITE_VALUE,
            'content-type': 'application/json; charset=utf-8',
          },
          body: '{}',
          timeoutMs: 2_000,
        })
      }
      const exited = await this._waitExit(pid)
      if (!exited) {
        this._writeLogLine(`[dsh-quantlab] 优雅关闭超时，强制结束 pid=${pid}`)
        this._killPid(pid)
      }
    } catch (error) {
      this._writeLogLine(`[dsh-quantlab] 停止时出错：${(error && error.message) || error}`)
      if (pid) this._killPid(pid)
    } finally {
      this._child = null
      this._childExited = null
      if (this._realSpawn && pid) LIVE_CHILDREN.delete(pid)
      this._closeLogFd()
      this._pid = 0
      this._port = 0
      this._url = ''
      this._startedAt = 0
      this._reused = false
      this._setState('stopped', { error: '' })
    }
    return this.status()
  }

  /** 等进程退出（最多 stopTimeoutMs）。返回是否已退出。 */
  async _waitExit(pid) {
    if (!pid && !this._child) return true
    const deadline = this._now() + this.stopTimeoutMs
    while (this._now() < deadline) {
      const alive = pid ? this._isPidAlive(pid) : false
      if (!alive) return true
      await this._sleep(this.pollIntervalMs)
    }
    return false
  }

  /** 重启：先停再起。 */
  async restart() {
    await this.stop()
    return this.start()
  }

  /**
   * 同步兜底清理（插件 dispose / 进程退出用，不能 await）。
   */
  dispose() {
    if (this._disposed) return
    this._disposed = true
    this._killChildImmediate()
    this._closeLogFd()
    this._state = 'stopped'
    this._pid = 0
    this._port = 0
    this._url = ''
    this._startedAt = 0
  }

  _killChildImmediate() {
    const pid = this._pid
    if (pid) {
      try {
        this._killPid(pid)
      } catch {
        /* ignore */
      }
      if (this._realSpawn) LIVE_CHILDREN.delete(pid)
    }
    this._child = null
    this._childExited = null
    this._exitInfo = null
    this._closeLogFd()
  }

  // ── 内部工具 ────────────────────────────────────────────────────────────

  _setState(state, patch = {}) {
    this._state = state
    if (Object.hasOwn(patch, 'error')) this._error = patch.error
    try {
      this._onState(this.status())
    } catch {
      /* 回调不能影响主流程 */
    }
  }

  _writeLogLine(line) {
    try {
      if (this._logFd === null || this._logFd === undefined) return
      const d = new Date()
      const p = (n) => String(n).padStart(2, '0')
      const stamp = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
      writeSync(this._logFd, `[${stamp}] ${line}\n`)
    } catch {
      /* ignore */
    }
  }

  _closeLogFd() {
    if (this._logFd !== null && this._logFd !== undefined) {
      try {
        closeSync(this._logFd)
      } catch {
        /* ignore */
      }
    }
    this._logFd = null
  }

  /** 发一个 HTTP 请求；永不抛，返回 {ok, status, text, json}。 */
  async _request(url, options = {}) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null
    const timer = controller
      ? setTimeout(() => {
          try {
            controller.abort()
          } catch {
            /* ignore */
          }
        }, options.timeoutMs || this.requestTimeoutMs)
      : null
    if (timer && typeof timer.unref === 'function') timer.unref()
    try {
      const response = await this._fetch(url, {
        method: options.method || 'GET',
        headers: options.headers || {},
        body: options.body,
        signal: controller ? controller.signal : undefined,
      })
      const text = typeof response.text === 'function' ? await response.text() : ''
      let json = null
      try {
        json = text ? JSON.parse(text) : null
      } catch {
        json = null
      }
      return { ok: response.ok !== false, status: Number(response.status) || 0, text, json }
    } catch (error) {
      return { ok: false, status: 0, text: '', json: null, error: String((error && error.message) || error) }
    } finally {
      if (timer) clearTimeout(timer)
    }
  }

  async _getJson(url, timeoutMs) {
    const result = await this._request(url, { timeoutMs })
    return result && result.ok ? result.json : null
  }
}

/**
 * 工厂（调用方习惯用 createBackend）。
 * @param {BackendOptions} options
 * @returns {QuantLabBackend}
 */
export function createBackend(options = {}) {
  return new QuantLabBackend(options)
}
