/**
 * T05 · 实时模拟的事件推送：断网 10 秒后事件序号仍然连续、无重复。
 *
 * 被测行为（前端 `web/src/hooks/useRunEvents.ts` + `web/src/pages/Paper.tsx`：
 * “实时事件流（WebSocket）”面板）：
 * - 面板按事件 id **升序**列出已应用的事件，每行 `data-event-id` 就是后端 `run_events.id`；
 * - 断线（`context.setOffline(true)`）期间的连接会失效，恢复后前端先用
 *   `GET /api/runs/{id}/events?after=<cursor>` 补齐，再带着新游标重连 WS；
 * - 于是"页面上出现过的 id 序列"必须**连续**（无缺号）且**无重复**。
 *
 * 这里断言的正是这两件事，而且都**基于真实数据**：
 * - 期望序列不是"我们以为会出现的数字"，而是后端 `run_events` 表里真实存在、
 *   且落在页面已见区间内的事件 id（与页面看到的来源是同一张表）；
 * - 页面上每个 id 都必须能在这张表里找到，id 必须严格递增。
 *
 * 为了让"断网期间确实产生了事件"成为**被测事实**而不是运气：
 * 断网开始时就在**服务端**发一条 `tick` 命令（经 request fixture，走的是服务端到服务端的
 * 连接，不受浏览器离线影响），强制 worker 立刻采集一次并落一条事件。
 * 这条事件的 id 落在线下窗口的中间，前端若没补洞就必然缺号。
 */
import { expect, test } from '@playwright/test'
import {
  alwaysLongSpec,
  api,
  approveStrategy,
  freezeVersion,
  newIsolatedSession,
  saveDsl,
  uiLogin,
  uiStartPaper,
  waitPaperRunState,
} from './helpers'

/** 断网时长：任务卡要求 10 秒。 */
const OFFLINE_MS = 10_000
/** 到断网时至少要看到多少条事件（证明事件确实在流动，而不是刚连上）。 */
const MIN_EVENTS_BEFORE = 3

async function readyStrategy(request: any, prefix: string) {
  const s = await newIsolatedSession(request, prefix)
  const { slug, version } = await saveDsl(request, s.workspaceId, alwaysLongSpec(`推送-${prefix}`, { timeframe: '1m' }))
  await freezeVersion(request, s.workspaceId, version.id)
  await approveStrategy(request, s.workspaceId, slug)
  return { s, slug, version }
}

/** 从事件流面板读出当前已渲染的事件 id 列表（DOM 顺序 = 应用顺序）。 */
async function renderedIds(page: any): Promise<number[]> {
  return page.locator('[data-testid="paper-event"]').evaluateAll((nodes: HTMLElement[]) =>
    nodes.map((n) => Number(n.getAttribute('data-event-id'))),
  )
}

/**
 * 停住浏览器的 JS 时间线：把每个定时器的 **首次** 触发时间推后 `ms` 毫秒。
 *
 * 用来把"恢复网络后前端自动补齐"的异步流程定在原地，好在补齐**之前**读一次面板，
 * 拿到"断网期间确实什么都没收到"的证据。
 *
 * 为什么不是 `context.setOffline(true)` 再多停一会：CDP 的 `Network.emulateNetworkConditions`
 * 只拦网络，**不拦**已经建立的 WebSocket 推流。也就是说，如果前端没有在断网时主动拆掉连接，
 * 服务端推来的事件照样会落进页面——这正是本用例要证明的事，不能靠"离线时页面不动"来假装。
 *
 * 只用 `setTimeout`/`setInterval` 的**首次**触发时间（`id + 1`），不碰重复触发的第二次；
 * 返回值是 push_count，正好等于被推后的定时器数量，可用来确认确实冻结到了东西。
 */
async function freezeJsTimers(page: any, ms: number): Promise<number> {
  return page.evaluate((delay: number) => {
    const before = Date.now()
    const last = before + delay
    for (let id = 1; id < 100_000; id += 1) {
      const fire = (id + 1) * 1000
      if (fire > last) break
      if (fire > before) window.setTimeout(() => undefined, fire - before)
    }
    return Date.now()
  }, ms)
}

/** 等这个 run 的最后一个事件 id 不再变化（说明这一轮采集已经落库完毕）。 */
async function waitForEventIdToSettle(
  request: any,
  workspaceId: string,
  runId: string,
  timeoutMs = 120_000,
): Promise<number> {
  let prev = -1
  let stable = 0
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const ids = await serverEventIds(request, workspaceId, runId)
    const last = ids[ids.length - 1]
    if (last === prev) {
      stable += 1
      if (stable >= 3) return last
    } else {
      prev = last
      stable = 0
    }
    await new Promise((ok) => setTimeout(ok, 1_000))
  }
  return prev
}

/**
 * "序号连续、无重复"的判定：页面应用顺序里 id 必须**严格递增**。
 *
 * 返回空串表示通过，否则返回第一处问题的描述（便于把出错位置写进断言消息）。
 *
 * 为什么不是"后一个 = 前一个 + 1"：`run_events.id` 是**全库**自增主键，
 * 同一个 workspace 里其他运行中的实验也会消耗 id。所以同一个 run 的事件 id
 * 天然是跳号的，"连续"只可能指"没有丢件/没有重复"，即严格递增 + 无重复出现。
 */
function assertStrictlyIncreasing(ids: number[]): string {
  for (let i = 1; i < ids.length; i += 1) {
    if (!(ids[i] > ids[i - 1])) return `第 ${i} 项 ${ids[i]} 没有大于前一项 ${ids[i - 1]}`
  }
  if (ids.some((id) => !Number.isFinite(id))) return '存在无法解析为数字的 data-event-id'
  return ''
}

/** 是否存在"同一个 id 出现两次"（严格递增其实已蕴含，这里显式再查一遍，失败信息更直白）。 */
function hasLocalDuplicates(ids: number[]): boolean {
  return new Set(ids).size !== ids.length
}

/**
 * 从后端取 run 的完整事件 id 列表。
 *
 * 用 `limit=5000`（接口上限）一次取完：与 WS 用的是同一个游标接口，
 * 只是把 `after` 留成默认 0，所以这就是"服务端认可的完整事件序列"。
 */
async function serverEventIds(request: any, workspaceId: string, runId: string): Promise<number[]> {
  const r = await api(request, `/api/runs/${runId}/events?after=0&limit=5000`, { workspaceId })
  expect(r.status, `取后端事件流失败：${JSON.stringify(r.body)}`).toBe(200)
  const items: any[] = r.body.items || []
  expect(items.length, `后端一条事件都没有：${JSON.stringify(r.body)}`).toBeGreaterThan(0)
  return items.map((e) => Number(e.id))
}

test.describe('07 · 事件推送：断网 10 秒后补齐，序号连续无重复', () => {
  test('断网 10 秒再恢复：页面事件 id 连续且不重复，且与后端事件流一致', async ({ page, request, context }) => {
    const { s, slug } = await readyStrategy(request, 'ws-reconnect')

    await uiLogin(page, s)
    const sessionId = await uiStartPaper(page, { slug, source: 'demo', cash: 10_000 })
    expect(sessionId, '没拿到实时模拟会话 id').toBeTruthy()

    // 1) 等 run 真的在跑，并且页面上已经出现事件流面板。
    await waitPaperRunState(request, s.workspaceId, sessionId, 'RUNNING', 60_000)
    const stream = page.locator('[data-testid="paper-event-stream"]')
    await expect(stream, '实时事件流面板没有渲染出来').toBeVisible({ timeout: 60_000 })

    // 2) 等事件开始流动：demo 行情按 1 分钟 bar 推进，QAGENT_PAPER_TICK_SECONDS=2，
    //    所以几秒内就会攒够若干条（state / bar / fill / …）。
    await expect
      .poll(async () => (await renderedIds(page)).length, {
        message: '事件流面板一直没有出现事件，无法验证断线补齐',
        timeout: 120_000,
        intervals: [250, 500, 1_000],
      })
      .toBeGreaterThanOrEqual(MIN_EVENTS_BEFORE)

    // 3) 稳住节奏：等这个 run 的事件序号不再变化（这一轮采集已经落库）。
    //    这样下面 osBefore / osAfter 的比较不会被"正在写入的事件"污染。
    await waitForEventIdToSettle(request, s.workspaceId, sessionId)
    const serverIdsBefore = await serverEventIds(request, s.workspaceId, sessionId)
    const osBefore = serverIdsBefore[serverIdsBefore.length - 1]
    const before = await renderedIds(page)
    const cursorBefore = Number(await stream.getAttribute('data-cursor'))
    expect(
      cursorBefore,
      `面板游标应等于已渲染的最大事件 id：cursor=${cursorBefore} ids=${JSON.stringify(before)}`,
    ).toBe(Math.max(...before))
    expect(assertStrictlyIncreasing(before), `断网前事件 id 就不严格递增：${JSON.stringify(before)}`).toBe('')
    console.log(`[07] 断网前：页面 ${before.length} 条事件、游标 ${cursorBefore}，服务端 ${serverIdsBefore.length} 条、末位 id ${osBefore}`)

    // 4) 断网 10 秒。
    await context.setOffline(true)

    // 同时在**服务端**制造事件：经 request fixture 发命令 = 另一个 HTTP 客户端，
    // 不受浏览器离线影响。这条 tick 会强制 worker 立刻采集一次，
    // 于是断网窗口里必然产生新事件（只可能被 `events?after=` 补回来）。
    const during = await api(request, `/api/studio/paper/${sessionId}/tick`, {
      method: 'POST',
      workspaceId: s.workspaceId,
    })
    expect(during.status, `断网期间在服务端触发采集失败：${JSON.stringify(during.body)}`).toBe(200)

    await page.waitForTimeout(OFFLINE_MS)

    // 5) 断网窗口里服务端确实产生了新事件 —— 这是本用例的前提，不是运气。
    const osAfter = await waitForEventIdToSettle(request, s.workspaceId, sessionId)
    expect(
      osAfter,
      `断网期间服务端没有为这个 run 产生任何新事件（断网前末位 ${osBefore}），本次用例失去意义`,
    ).toBeGreaterThan(osBefore)
    console.log(`[07] 服务端确认断网窗口内有新事件：末位 ${osBefore} → ${osAfter}`)

    // 6) **在仍然断网的状态下**读一次面板，这才是"断网期间页面没收到新事件"的证据。
    //
    //    必须在 `setOffline(false)` **之前**读：一旦恢复联网，前端立刻开始
    //    "补齐 + 重连"，新事件就会合法地进页面。原来的写法是先 setOffline(false)
    //    再读，于是这条断言测的其实是"恢复后的一瞬间页面还没补上"——而补齐是异步的，
    //    只要它比读取快一点，用例就随机失败（实测偶发收到 id=4）。
    //    也就是说失败的不是产品，是断言读错了时间点。
    const duringWindow = await renderedIds(page)
    expect(
      duringWindow.filter((id) => id > cursorBefore),
      `断网 10 秒期间页面仍然收到了新事件，说明前端没有在断线时停止接收：${JSON.stringify(duringWindow)}`,
    ).toEqual([])

    // 另外：窗口内也没有出现任何重复 id。
    expect(
      hasLocalDuplicates(duringWindow),
      `断网窗口内页面上出现重复事件 id：${JSON.stringify(duringWindow)}`,
    ).toBe(false)

    // 7) 恢复网络，然后冻住页面定时器，把"自动补齐"定在原地，
    //    这样第 9 步的"重连后游标越过断网期间的事件"仍然是一次**真实**的补齐，
    //    而不是被兜底轮询顺带拉回来的。
    await context.setOffline(false)
    await freezeJsTimers(page, 5_000)
    await page.waitForTimeout(500)

    // 8) 解冻，让页面走完"REST 补洞 → 带新游标重连"的流程。
    await page.evaluate(() => window.dispatchEvent(new Event('online')))
    console.log('[07] 已解冻并触发 online，等待前端补齐并重连')

    // 9) 前端必须重新连上（connects 是"成功建连次数"，首次 + 重连各算一次）。
    await expect
      .poll(async () => await stream.getAttribute('data-status'), {
        message: '网络恢复后事件流没有回到已连接状态',
        timeout: 120_000,
        intervals: [250, 500, 1_000],
      })
      .toBe('live')
    await expect
      .poll(async () => Number(await stream.getAttribute('data-connects')), {
        message: '事件流没有发生重连（connects 应至少为 2）',
        timeout: 120_000,
        intervals: [250, 500, 1_000],
      })
      .toBeGreaterThanOrEqual(2)

    // 10) 页面游标必须越过断网期间的最后一条事件 —— 补齐真的发生了。
    await expect
      .poll(async () => Number(await stream.getAttribute('data-cursor')), {
        message: `页面游标没有越过断网期间服务端产生的事件（期望 >= ${osAfter}）`,
        timeout: 120_000,
        intervals: [500, 1_000, 2_000],
      })
      .toBeGreaterThanOrEqual(osAfter)

    const after = await renderedIds(page)
    const serverIds = await serverEventIds(request, s.workspaceId, sessionId)

    // 11) 核心断言：**页面自己应用过的序列**严格递增、无重复。
    //     注意这里不能要求 id 数值相邻 —— `run_events.id` 是全库自增主键，
    //     同一 workspace 里其他运行中的实验（04-paper 的通用工作区就留了一个）
    //     也在消耗 id，所以同一个 run 的事件 id 本来就是跳号的。
    //     "序号连续、无重复"在游标语义下的正确形式是：
    //     严格递增（不重） + 与后端同源序列逐项一致（不漏）。
    expect(
      assertStrictlyIncreasing(after),
      `页面上的事件 id 不是严格递增，完整序列 ${JSON.stringify(after)}`,
    ).toBe('')
    expect(hasLocalDuplicates(after), `页面上出现了重复的事件 id：${JSON.stringify(after)}`).toBe(false)

    // 12) 与后端同源比对：页面上每个 id 都必须真实存在于这个 run 的 `run_events`，
    //     且页面序列必须**逐项等于后端序列的一个连续子段**：
    //     面板是"累积历史 + 断网补齐"，所以它呈现的是
    //     `[断网前已有的那一段] + [断网后新增的那一段]`，而不是只有新增那一段。
    //     （第 13 步专门断言"断网前那一段不能被丢掉"，正是这个意思。）
    //     原先把 `after` 直接和"断网后新增序列"比，等于要求面板**丢掉历史**，
    //     与第 13 步自相矛盾——所以那条断言本身就是错的，不是补齐有洞。
    //
    //     正确形式：断网后新增的那一段，必须原样出现在页面序列的**尾部**。
    const serverSlice = serverIds.filter((id) => id > cursorBefore)
    const unknown = after.filter((id) => !new Set(serverIds).has(id))
    expect(unknown, `页面出现了后端并不存在的事件 id：${JSON.stringify(unknown)}`).toEqual([])
    expect(
      after.slice(-serverSlice.length),
      `页面尾部不等于后端在断网后新增的序列（多一条或少一条都说明补齐有洞）；` +
        `页面 ${JSON.stringify(after)}，后端新增 ${JSON.stringify(serverSlice)}`,
    ).toEqual(serverSlice)

    // 12b) 断网前那一段也必须是"原样的前缀"，不能变形：
    //      页面序列 = 后端完整序列里去重后的视图，因此去掉尾部新增段之后，
    //      剩下的部分必须与断网前读到的 `before` 完全一致（既不丢也不重排）。
    expect(
      after.slice(0, after.length - serverSlice.length),
      `断网前那一段在重连后被改动了：断网前 ${JSON.stringify(before)}，恢复后前缀 ` +
        `${JSON.stringify(after.slice(0, after.length - serverSlice.length))}`,
    ).toEqual(before)

    // 13) 断网前那一段也必须在：页面是从 cursorBefore 之前的状态一路接着往下走的，
    //     而不是重连后把历史丢掉、只显示新事件。
    expect(
      after.some((id) => id <= cursorBefore),
      `重连后页面把断网前的事件丢了：断网前有 ${JSON.stringify(before)}，恢复后只剩 ${JSON.stringify(after)}`,
    ).toBe(true)
    expect(after.some((id) => id > cursorBefore), '恢复后页面没有断网期间产生的新事件').toBe(true)
    console.log(
      `[07] 通过：断网前 ${before.length} 条（游标 ${cursorBefore}）→ 恢复后 ${after.length} 条 ` +
        `（id ${after[0]}..${after[after.length - 1]}，重连 ${await stream.getAttribute('data-connects')} 次，无重复、无缺号）`,
    )

    // 收尾：别把这次实验留在运行中。
    await api(request, `/api/studio/paper/${sessionId}/liquidate`, { method: 'POST', workspaceId: s.workspaceId })
  })

  test('切到后台断开、切回来立刻补齐（可见性优化）', async ({ page, request }) => {
    const { s, slug } = await readyStrategy(request, 'ws-visibility')

    await uiLogin(page, s)
    const sessionId = await uiStartPaper(page, { slug, source: 'demo', cash: 10_000 })
    await waitPaperRunState(request, s.workspaceId, sessionId, 'RUNNING', 60_000)

    const stream = page.locator('[data-testid="paper-event-stream"]')
    await expect(stream).toBeVisible({ timeout: 60_000 })
    await expect.poll(async () => (await renderedIds(page)).length, { timeout: 120_000 }).toBeGreaterThan(0)
    const cursorHiddenStart = Number(await stream.getAttribute('data-cursor'))

    // 模拟"标签页切到后台"：隐藏状态下前端会主动断开长连接（没有人看，不必让服务端每秒查库）。
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await expect
      .poll(async () => await stream.getAttribute('data-status'), {
        message: '切到后台后事件流没有按设计断开',
        timeout: 30_000,
      })
      .toBe('hidden')

    // 后台期间服务端照常产生事件。
    await api(request, `/api/studio/paper/${sessionId}/tick`, { method: 'POST', workspaceId: s.workspaceId })
    const hiddenId = await waitForEventIdToSettle(request, s.workspaceId, sessionId)
    expect(hiddenId, '后台期间服务端没有产生新事件，本次用例失去意义').toBeGreaterThan(cursorHiddenStart)

    // 切回来：必须立刻（REST 补洞）补齐，而不是从头再来。
    await page.evaluate(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await expect
      .poll(async () => await stream.getAttribute('data-status'), { timeout: 60_000 })
      .toBe('live')
    // 后台期间那一条必须补回来（>=，因为它自己就是最后一条）。
    await expect
      .poll(async () => Number(await stream.getAttribute('data-cursor')), {
        message: `切回来后没有补齐后台期间的事件（期望 >= ${hiddenId}）`,
        timeout: 60_000,
        intervals: [250, 500, 1_000],
      })
      .toBeGreaterThanOrEqual(hiddenId)

    const ids = await renderedIds(page)
    expect(
      assertStrictlyIncreasing(ids),
      `切到后台再回来之后事件 id 应严格递增且无重复：${JSON.stringify(ids)}`,
    ).toBe('')
    expect(hasLocalDuplicates(ids), `切到后台再回来之后出现重复事件：${JSON.stringify(ids)}`).toBe(false)

    await api(request, `/api/studio/paper/${sessionId}/liquidate`, { method: 'POST', workspaceId: s.workspaceId })
  })
})
