/** 测试公用小工具：假 req/res、假 ctx、临时目录。 */
import { EventEmitter } from 'node:events'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

/** 临时目录（test 结束调用 cleanup()）。 */
export function tempDir(prefix = 'dsh-quantlab-test-') {
  const dir = mkdtempSync(join(tmpdir(), prefix))
  return {
    dir,
    cleanup: () => {
      try {
        rmSync(dir, { recursive: true, force: true })
      } catch {
        /* Windows 偶发占用 */
      }
    },
  }
}

/** 假 ServerResponse：收集 write/end，记录状态码与头。 */
export function mockRes() {
  const chunks = []
  let statusCode = 0
  const headers = {}
  let ended = false
  let headersSent = false
  const res = {
    get statusCode() {
      return statusCode
    },
    get headersSent() {
      return headersSent
    },
    get writableEnded() {
      return ended
    },
    get headers() {
      return headers
    },
    writeHead(code, extra) {
      statusCode = code
      headersSent = true
      Object.assign(headers, extra || {})
      return res
    },
    write(chunk) {
      chunks.push(String(chunk))
      return true
    },
    end(chunk) {
      if (chunk !== undefined) chunks.push(String(chunk))
      ended = true
    },
    on() {},
    once() {},
    removeListener() {},
    flushHeaders() {},
    get body() {
      return chunks.join('')
    },
    json() {
      return JSON.parse(res.body)
    },
  }
  return res
}

/** 假 IncomingMessage：method + url + JSON body。 */
export function mockReq({ method = 'GET', url = '/', body, headers = {} } = {}) {
  const req = new EventEmitter()
  req.method = method
  req.url = url
  req.headers = headers
  if (body !== undefined) {
    process.nextTick(() => {
      req.emit('data', Buffer.from(typeof body === 'string' ? body : JSON.stringify(body)))
      req.emit('end')
    })
  } else {
    process.nextTick(() => req.emit('end'))
  }
  return req
}

/**
 * 假 cordis ctx：
 *   · webServer.register 收集 route，返回 disposer
 *   · tools.register 收集工具
 *   · effect(fn) 立即执行并记录 disposer
 *   · get(name) 取注入服务
 */
export function mockCtx({ services = {}, origin = 'http://127.0.0.1:56389' } = {}) {
  const routes = new Map()
  const tools = []
  const disposers = []
  const effects = []
  const listeners = new Map()
  /** tools 数组本身带 register，既方便断言也符合 ctx.tools.register 契约。 */
  tools.register = (definition) => {
    tools.push(definition)
    return () => {
      const index = tools.indexOf(definition)
      if (index >= 0) tools.splice(index, 1)
    }
  }
  const ctx = {
    webServer: {
      register(route) {
        const key = `${route.kind}:${route.path}`
        if (routes.has(key)) throw new Error(`duplicate route ${key}`)
        routes.set(key, route)
        return () => routes.delete(key)
      },
      getOrigin() {
        return origin
      },
    },
    tools,
    on(event, handler) {
      const list = listeners.get(event) || []
      list.push(handler)
      listeners.set(event, list)
      return () => {
        const current = listeners.get(event) || []
        const index = current.indexOf(handler)
        if (index >= 0) current.splice(index, 1)
      }
    },
    emit(event, ...args) {
      for (const handler of listeners.get(event) || []) handler(...args)
    },
    effect(callback) {
      const result = callback()
      effects.push(result)
      if (typeof result === 'function') disposers.push(result)
      return result
    },
    get(name) {
      return services[name]
    },
    routes,
    disposers,
    effects,
    listeners,
    route(kind, path) {
      return routes.get(`${kind}:${path}`)
    },
  }
  return ctx
}

/** 跑一个 route handler 并返回 {res, json()}。 */
export async function callRoute(handler, { method = 'GET', url = '/', body } = {}) {
  const req = mockReq({ method, url, body })
  const res = mockRes()
  await handler(req, res)
  return res
}
