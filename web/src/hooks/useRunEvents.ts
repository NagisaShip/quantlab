/**
 * 运行事件流：用 WebSocket 替代轮询。
 *
 * 后端契约（`src/quant_agent/api/routes_runs.py::run_ws` 与 `Runs.events`）：
 * - `GET  /api/runs/{id}/events?after=<cursor>` → `{items, cursor}`；
 * - `WS   /api/runs/{id}/ws?after=<cursor>`     → `{items, cursor}` / `{heartbeat}` / `{done, state}`；
 * - **`after` 是排他游标**：`Runs.events` 的 SQL 是 `WHERE id>?`，只返回**严格大于** after 的事件。
 *   所以"上次拿到的最后一个事件 id"原样带回来就是正确的续传点，既不会重复也不会漏。
 *   这一点决定了本文件里所有游标都表示"已经应用过的最大 id"，而不是"下一条的下标"。
 *
 * 为什么不能只靠 WebSocket 重连：
 * WebSocket 重连**不保证不丢事件**——断线到重连成功之间的那段时间里，服务端产生的事件
 * 既不在旧连接上（已经断了），也不在新连接上（新连接从新游标开始）。必须先用 REST
 * `events?after=<cursor>` 把洞补齐，再带着补齐后的游标去连 WS，才能做到"序号连续、无重复"。
 *
 * 三条防线保证同一个事件只应用一次：
 * 1. 游标单调：`id <= cursor` 的事件一律丢弃（补洞与 WS 推送天然会重叠）；
 * 2. 显式 id 去重集合：挡住同一批/相邻批里的重复投递；
 * 3. 补洞只在**建连之前**做，且补洞与 WS 不会同时在跑（换连接前先断旧的）。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { get } from '../http'

export interface RunEvent {
  id: number
  ts: number
  type: string
  data: Record<string, unknown>
}

export type RunEventStatus =
  | 'idle' // 没有 runId，或调用方关掉了
  | 'connecting' // 首次建连（正在补洞）
  | 'live' // WS 已连上
  | 'reconnecting' // 断了，正在按退避等待重连
  | 'offline' // 浏览器报告离线，等 online 事件
  | 'hidden' // 标签页切到后台，按设计断开
  | 'done' // run 已到终态，事件推完
  | 'error' // 认证/权限失败（4401/4403），重连没有意义
  | 'closed'

export const INITIAL_BACKOFF_MS = 1_000
export const MAX_BACKOFF_MS = 30_000
/** 离线时的探测间隔：不指数退避空转，低频试探即可。 */
export const OFFLINE_PROBE_MS = 3_000
/** REST 补洞每页条数（后端上限 5000）。 */
export const CATCHUP_PAGE = 1_000
/** 事件在内存里保留的条数上限。 */
export const DEFAULT_LIMIT = 2_000
/** 去重集合的清理阈值：游标已经挡住了历史 id，集合只是挡批内重复，可以安全清空。 */
const SEEN_CAP = 4_096
/** 收到任何消息（含心跳）就认为"这条连接还必须留着"的窗口时长。 */
const ALIVE_QUIET_MS = 2_500
/** 存活看门狗的检查间隔。 */
const WATCHDOG_INTERVAL_MS = 500

/**
 * 判断现在应该给这条连接多长的存活窗口。
 *
 * 这里有个实测（Chrome/Edge + Playwright `context.setOffline`）反直觉的地方：
 * **离线不会把已建立的 WebSocket 关掉**。断网后 `readyState` 仍然是 1（OPEN），
 * 不触发 close，也收不到任何帧；等网络恢复，积压的帧会一次性灌进来。
 *
 * 于是断线期间有两种可能，从浏览器侧看起来完全一样：
 *   (a) 连接真的还活着，只是恰好没有新事件（空闲）；
 *   (b) 链路已经断了（或浏览器在缓冲），而且**无法**从 readyState 看出来。
 * 分不清 (a)(b) 就只剩两条路：傻等（事件一直不出现，用户看到的是"卡住"），
 * 或者到点就重连（空闲时纯属白连一次）。
 * 我们选后者，但把窗口取短：2.5 秒静默 + 服务端心跳间隔。
 * 重连本身是幂等且便宜的（REST 补洞带游标 → WS 带游标），最坏只是多一次空批次；
 * 而"卡住不补"是用户可见的正确性事故。
 */
export function aliveWindowMs(heartbeatMs: number): number {
  return ALIVE_QUIET_MS + (heartbeatMs > 0 ? heartbeatMs : WS_DEFAULT_HEARTBEAT_MS)
}

/** 服务端心跳间隔的兜底假设（`QAGENT_WS_HEARTBEAT_SECONDS` 默认为 15）。 */
const WS_DEFAULT_HEARTBEAT_MS = 15_000

/**
 * 判断现在应该给这条连接多长的判死阈值。
 *
 * 上面那个窗口是"要不要重连"的窗口，这里还要单独处理"多久没消息算彻底没救"。
 * 因为重连有退避，窗口太短会在网络抖动时反复弹出"重连中"的状态闪烁。
 */
export function staleThresholdMs(heartbeatMs: number): number {
  return (heartbeatMs > 0 ? heartbeatMs * 3 : 30_000) + 2_000
}

/**
 * 指数退避 + 抖动：1s、2s、4s、8s… 上限 30s，再乘一个 [50%, 100%] 的随机系数。
 * 抖动是必须的——多个标签页/多个组件同时掉线时，没有抖动就会同一毫秒一起回来（惊群）。
 * 导出是为了让"退避确实是指数 + 有上限 + 有抖动"这件事可被单独验证。
 */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const exp = Math.min(MAX_BACKOFF_MS, INITIAL_BACKOFF_MS * 2 ** Math.max(0, attempt))
  return Math.round(exp * (0.5 + random() * 0.5))
}

function wsUrl(runId: string, cursor: number): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${window.location.host}/api/runs/${encodeURIComponent(runId)}/ws?after=${cursor}`
}

function isOffline(): boolean {
  // navigator.onLine 只在"确定离线"时才为 false；为 true 也不代表真能连通，
  // 所以它只用来**省掉无谓的重连**，不能当成"链路正常"的证据（心跳才是）。
  return typeof navigator !== 'undefined' && navigator.onLine === false
}

export interface UseRunEventsOptions {
  runId?: string | null
  /** 关掉时不建连（例如页面还没拿到 runId）。 */
  enabled?: boolean
  /** 内存里保留的事件条数上限。 */
  limit?: number
  /**
   * 标签页切到后台时断开连接（默认 true）。
   * 理由：后台标签页没有人看，维持一条长连接只会让服务端每秒白查一次数据库；
   * 而断开是安全的——切回来时先 REST 补洞再重连，事件一条都不会丢。
   */
  pauseWhenHidden?: boolean
  /** 每批**新应用**的事件（已按 id 升序去重）。 */
  onEvents?: (items: RunEvent[]) => void
  /** run 进入终态、事件推完。 */
  onDone?: (state: string) => void
  onStatus?: (status: RunEventStatus) => void
}

export interface UseRunEventsResult {
  events: RunEvent[]
  cursor: number
  status: RunEventStatus
  connected: boolean
  /** 成功建连的次数（含首次）；E2E 用它证明"确实重连过"。 */
  connects: number
  lastError: string
  /** 手动重连（"立即重试"按钮）。 */
  retry: () => void
}

export function useRunEvents(opts: UseRunEventsOptions): UseRunEventsResult {
  const { runId, enabled = true, limit = DEFAULT_LIMIT, pauseWhenHidden = true } = opts

  const [events, setEvents] = useState<RunEvent[]>([])
  const [cursor, setCursor] = useState(0)
  const [status, setStatus] = useState<RunEventStatus>('idle')
  const [connects, setConnects] = useState(0)
  const [lastError, setLastError] = useState('')

  // 回调放 ref：调用方每次渲染都会给新函数，直接进依赖数组会让 effect 反复重建连接。
  const onEventsRef = useRef(opts.onEvents)
  const onDoneRef = useRef(opts.onDone)
  const onStatusRef = useRef(opts.onStatus)
  onEventsRef.current = opts.onEvents
  onDoneRef.current = opts.onDone
  onStatusRef.current = opts.onStatus

  const cursorRef = useRef(0)
  const seenRef = useRef<Set<number>>(new Set())
  const runIdRef = useRef<string | null>(null)
  const retryRef = useRef<() => void>(() => undefined)

  // 换 run 就从头来：游标、去重集合、已展示的事件全部属于上一个 run。
  useEffect(() => {
    if (runIdRef.current === (runId ?? null)) return
    runIdRef.current = runId ?? null
    cursorRef.current = 0
    seenRef.current.clear()
    setEvents([])
    setCursor(0)
    setConnects(0)
    setLastError('')
    setStatus(runId ? 'connecting' : 'idle')
  }, [runId])

  useEffect(() => {
    if (!enabled || !runId) {
      setStatus('idle')
      return
    }

    let disposed = false
    let socket: WebSocket | null = null
    let reconnectTimer: number | null = null
    let watchdogTimer: number | null = null
    let attempt = 0
    let finished = false
    let lastMessageAt = Date.now()
    let heartbeatMs = 0
    // 建连是异步的（要先 REST 补洞）。用代数号作废"上一次还没走完的 open"，
    // 否则 visibilitychange / online / 退避定时器同时触发时可能并排建出两条连接。
    let generation = 0
    let hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden'

    const set = (next: RunEventStatus) => {
      if (disposed) return
      setStatus(next)
      onStatusRef.current?.(next)
    }

    const clearReconnect = () => {
      if (reconnectTimer !== null) {
        window.clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    const clearWatchdog = () => {
      if (watchdogTimer !== null) {
        window.clearInterval(watchdogTimer)
        watchdogTimer = null
      }
    }

    /** 拆掉 socket 且**不**安排重连（卸载、隐藏、离线时用）。 */
    const dropSocket = () => {
      generation += 1 // 作废任何还在 await 中的 open()
      clearWatchdog()
      const s = socket
      socket = null
      if (s) {
        s.onopen = null
        s.onmessage = null
        s.onerror = null
        s.onclose = null
        try {
          s.close()
        } catch {
          /* 已经关了 */
        }
      }
    }

    /**
     * 应用一批事件：按 id 升序、去重、推进游标。
     * 这里是"序号连续、无重复"的落点——凡是被丢弃的 id 都必须有理由。
     */
    const apply = (raw: RunEvent[]) => {
      if (!raw.length) return
      const batch = [...raw].sort((a, b) => a.id - b.id)
      const fresh: RunEvent[] = []
      for (const item of batch) {
        if (!item || typeof item.id !== 'number') continue
        // 防线 1：游标是排他的（后端 `id>after`），小于等于它的必然已经应用过。
        if (item.id <= cursorRef.current) continue
        // 防线 2：同一批里重复投递的 id。
        if (seenRef.current.has(item.id)) continue
        seenRef.current.add(item.id)
        cursorRef.current = item.id
        fresh.push(item)
      }
      if (seenRef.current.size > SEEN_CAP) seenRef.current.clear()
      if (!fresh.length) return
      setCursor(cursorRef.current)
      setEvents((prev) => {
        const next = prev.concat(fresh)
        return next.length > limit ? next.slice(next.length - limit) : next
      })
      onEventsRef.current?.(fresh)
    }

    /** REST 补洞：把断线期间的事件按游标一段段拉回来。 */
    const catchUp = async () => {
      for (let page = 0; page < 100; page += 1) {
        const r = await get<{ items: RunEvent[]; cursor: number }>(
          `/api/runs/${encodeURIComponent(runId)}/events?after=${cursorRef.current}&limit=${CATCHUP_PAGE}`,
        )
        const items = r.items ?? []
        if (!items.length) return
        apply(items)
        if (items.length < CATCHUP_PAGE) return
      }
    }

    const scheduleReconnect = () => {
      if (disposed || finished) return
      clearReconnect()
      if (isOffline()) {
        // 离线：不空转。低频探测 + 等 online 事件，两者谁先到都行。
        set('offline')
        reconnectTimer = window.setTimeout(() => {
          reconnectTimer = null
          void open()
        }, OFFLINE_PROBE_MS)
        return
      }
      if (hidden && pauseWhenHidden) {
        set('hidden')
        return
      }
      const delay = backoffDelay(attempt)
      attempt += 1
      set('reconnecting')
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null
        void open()
      }, delay)
    }

    const armWatchdog = () => {
      clearWatchdog()
      // 两条时间线：
      // - staleMs：服务端心跳都断了，连接基本没救（3 个心跳间隔 + 2 秒余量）；
      // - aliveMs：只要在窗口内见过任何消息（含心跳）就留着，否则主动重连。
      //   之所以需要第二条：离线时浏览器不会关掉 WebSocket（readyState 仍是 OPEN、
      //   没有 close 事件、也没有帧），只有到点主动重连才能把"卡死"变成"补齐"。
      const staleMs = staleThresholdMs(heartbeatMs)
      watchdogTimer = window.setInterval(() => {
        if (disposed || finished || !socket) return
        const quiet = Date.now() - lastMessageAt
        if (quiet <= aliveWindowMs(heartbeatMs)) return
        // 超时重连会把 attempt 归零，让状态在 live ↔ reconnecting 之间来回闪；
        // 所以真正的判死（心跳都没了）才重置退避，其它情况只在需要时安排一次重连。
        if (quiet > staleMs) attempt = 0
        dropSocket()
        setLastError('连接长时间没有数据，已主动重连以补齐事件')
        scheduleReconnect()
      }, WATCHDOG_INTERVAL_MS)
    }

    const open = async () => {
      if (disposed || finished) return
      if (isOffline()) {
        scheduleReconnect()
        return
      }
      const gen = (generation += 1)
      set(attempt === 0 ? 'connecting' : 'reconnecting')
      // 关键顺序：**先补洞，再连 WS**。反过来就会在"连上但还没补"的窗口里丢事件。
      try {
        await catchUp()
      } catch {
        /* 网络还不通；下面建连多半也会失败，交给退避 */
      }
      if (disposed || finished || gen !== generation) return
      if (isOffline()) {
        scheduleReconnect()
        return
      }
      let s: WebSocket
      try {
        s = new WebSocket(wsUrl(runId, cursorRef.current))
      } catch (err) {
        setLastError((err as Error).message || '无法建立 WebSocket')
        scheduleReconnect()
        return
      }
      socket = s
      lastMessageAt = Date.now()
      s.onopen = () => {
        if (disposed || socket !== s) return
        attempt = 0
        setLastError('')
        setConnects((n) => n + 1)
        // 连接刚建立时也算"刚有过活动"：否则看门狗可能在握手完成的瞬间
        // 就按上一次的静默时间判它卡死。
        lastMessageAt = Date.now()
        set('live')
        armWatchdog()
      }
      s.onmessage = (ev) => {
        if (disposed || socket !== s) return
        lastMessageAt = Date.now()
        let msg: { items?: RunEvent[]; heartbeat?: boolean; interval_ms?: number; done?: boolean; state?: string }
        try {
          msg = JSON.parse(String(ev.data))
        } catch {
          return
        }
        if (typeof msg.interval_ms === 'number' && msg.interval_ms > 0) heartbeatMs = msg.interval_ms
        if (msg.items?.length) apply(msg.items)
        if (msg.done) {
          finished = true
          dropSocket()
          set('done')
          onDoneRef.current?.(String(msg.state ?? ''))
        }
      }
      s.onerror = () => {
        /* 统一走 onclose，避免同一件事触发两条重连路径 */
      }
      s.onclose = (ev) => {
        if (disposed || socket !== s) return
        dropSocket()
        if (finished) return
        if (ev.code === 4401 || ev.code === 4403) {
          // 认证失效 / 无权访问：重连多少次都是同样的结果，别把浏览器变成重连机器。
          setLastError(ev.code === 4401 ? '登录状态已失效，请重新登录' : '无权访问该运行')
          set('error')
          return
        }
        scheduleReconnect()
      }
    }

    const onOnline = () => {
      if (disposed || finished) return
      attempt = 0
      clearReconnect()
      void open()
    }
    const onOffline = () => {
      if (disposed || finished) return
      // 断网时主动拆掉连接：浏览器不一定立刻把 WS 判死（半开状态），
      // 留着它反而会让前端误以为还在收事件。
      dropSocket()
      set('offline')
      // 不依赖 online 事件一定会到：低频探测兜住"恢复了但没通知"的情况。
      scheduleReconnect()
    }
    const onVisibility = () => {
      hidden = document.visibilityState === 'hidden'
      if (disposed || finished) return
      if (hidden && pauseWhenHidden) {
        dropSocket()
        clearReconnect()
        set('hidden')
        return
      }
      if (!hidden) {
        attempt = 0
        clearReconnect()
        void open()
      }
    }

    window.addEventListener('online', onOnline)
    window.addEventListener('offline', onOffline)
    document.addEventListener('visibilitychange', onVisibility)
    retryRef.current = () => {
      if (disposed || finished) return
      attempt = 0
      clearReconnect()
      dropSocket()
      void open()
    }

    if (!(hidden && pauseWhenHidden)) void open()
    else set('hidden')

    return () => {
      // 卸载：干净地关掉 socket 并清掉所有定时器。
      // React 严格模式会"挂载→卸载→再挂载"，漏掉任何一条都会留下幽灵连接。
      disposed = true
      clearReconnect()
      dropSocket()
      window.removeEventListener('online', onOnline)
      window.removeEventListener('offline', onOffline)
      document.removeEventListener('visibilitychange', onVisibility)
      retryRef.current = () => undefined
    }
  }, [runId, enabled, limit, pauseWhenHidden])

  const retry = useCallback(() => retryRef.current(), [])

  return {
    events,
    cursor,
    status,
    connected: status === 'live',
    connects,
    lastError,
    retry,
  }
}

/**
 * 找到当前工作区里"最新且未结束"的 run，用于给全局/列表级视图提供事件源。
 *
 * 为什么需要它：`RunBanner` 这类组件本身没有 runId，但它显示的状态（行情新鲜度、
 * worker 在线数）恰恰是被 run 的推进改变的。挂到活跃 run 的事件流上，就能做到
 * "有事件才刷新"，而不是不管有没有变化都每 60 秒问一次。
 *
 * 这里对 `/api/runs` 的低频查询只是"发现事件源"，不是数据轮询：
 * 它只回答"该订阅哪个 run"，订阅之后页面内容由事件驱动。
 */
export function useNewestActiveRunId(opts: { enabled?: boolean; refreshMs?: number } = {}) {
  const { enabled = true, refreshMs = 30_000 } = opts
  const [runId, setRunId] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (!enabled) return
    let alive = true
    const load = () =>
      get<{ items: { id: string; state: string; terminal?: boolean }[] }>('/api/runs?limit=50')
        .then((r) => {
          if (!alive) return
          const active = (r.items ?? []).find((x) => !x.terminal && !['COMPLETED', 'FAILED', 'CANCELED'].includes(x.state))
          setRunId(active ? active.id : null)
        })
        .catch(() => undefined)
    load()
    const t = window.setInterval(load, refreshMs)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [enabled, refreshMs, tick])

  return { runId, refresh: useCallback(() => setTick((n) => n + 1), []) }
}

/**
 * 把"一串事件"压成"一次刷新"。
 *
 * 断线补齐时 REST 会一次返回几十条事件，逐条触发 `load()` 会把后端打成筛子；
 * 而事件密集时（补洞 + WS 推送重叠）用户也只需要看到最终状态。
 * 所以合并到一个短窗口里只刷新一次。
 */
export function useEventRefresh(run: () => void, waitMs = 400): () => void {
  const timer = useRef<number | null>(null)
  const fn = useRef(run)
  fn.current = run

  const trigger = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => {
      timer.current = null
      fn.current()
    }, waitMs)
  }, [waitMs])

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
      timer.current = null
    },
    [],
  )

  return trigger
}
