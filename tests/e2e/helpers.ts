/**
 * E2E 公共设施：登录 / 注册、等待 run 终态、构造 DSL 规则、造 CSV。
 *
 * 约定：
 * - 所有接口调用都带上后端要求的 `X-Requested-With`（CSRF）与 `X-Workspace-Id`；
 * - 断言失败时信息要能直接指向“哪一步、什么状态、后端说了什么”；
 * - 不修改任何应用代码来迁就测试：接口行为就是被测对象。
 */
import { expect, type APIRequestContext, type Page } from '@playwright/test'

export const ADMIN = {
  email: 'admin@example.com',
  password: 'Passw0rd!',
  name: '管理员',
}

/** 每个 spec 用独立邮箱，避免共享后端时互相踩到工作区。 */
export function uniqueEmail(prefix: string): string {
  const stamp = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
  return `${prefix}.${stamp}@example.com`
}

/** 工作区名字加时间戳，便于在审计/日志里定位是哪次运行创建的。 */
function uniqueLabel(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
}

export function idemKey(prefix = 'e2e'): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export interface ApiOptions {
  workspaceId?: string
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  data?: unknown
  headers?: Record<string, string>
}

/** 直接打后端接口（后端错误体是 {code, message, trace_id, details}，原样返回便于排错）。 */
export async function api(
  request: APIRequestContext,
  path: string,
  opts: ApiOptions = {},
): Promise<{ status: number; body: any }> {
  const headers: Record<string, string> = {
    'X-Requested-With': 'e2e',
    ...(opts.workspaceId ? { 'X-Workspace-Id': opts.workspaceId } : {}),
    ...(opts.headers || {}),
  }
  // 必须显式声明 JSON：Playwright 在 data 是**字符串**时不会自动补 Content-Type，
  // 而 FastAPI 拿不到 application/json 就会把 body 当成无法解析的对象，回报 422。
  if (opts.data !== undefined) headers['Content-Type'] = 'application/json'
  const res = await request.fetch(path, {
    method: opts.method || 'GET',
    headers,
    data: opts.data === undefined ? undefined : JSON.stringify(opts.data),
  })
  const text = await res.text()
  let body: any = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = text
  }
  return { status: res.status(), body }
}

export interface Session {
  email: string
  password: string
  workspaceId: string
  userId: string
  me: any
}

/**
 * 拿到一个**独占的工作区**，用于一个 spec。
 *
 * 为什么不能每个 spec 都靠"注册新用户"拿工作区：平台的注册契约是
 * **受控注册**（`bootstrapped` 之后必须有邀请码，见 `/api/auth/register` 的 403），
 * 而邀请码注册是"加入已有工作区"，不是新建。所以这里统一：
 * 1. 第一个账号（`admin@example.com`）自注册成为部署管理员并拿到自己的工作区；
 * 2. 之后每个 spec 用这个管理员身份 `POST /api/workspaces` 新建一个**独立工作区**。
 *
 * 这样各 spec 的工作区、配额、策略与版本编号完全隔离，互不干扰，
 * 也不会因为工作区配额（max_concurrent_runs / max_paper_runs）互相拖累。
 *
 * 与 `tests/conftest.py::login` 的契约保持一致：写请求都带 `X-Requested-With`。
 */
export async function registerOrLogin(
  request: APIRequestContext,
  opts: { email?: string; password?: string; name?: string; inviteCode?: string } = {},
): Promise<Session> {
  const email = opts.email || ADMIN.email
  const password = opts.password || ADMIN.password
  const name = opts.name || ADMIN.name

  // 1) 尝试注册（只有部署里的第一个账号能成功）。
  const reg = await api(request, '/api/auth/register', {
    method: 'POST',
    data: { email, password, name, invite_code: opts.inviteCode },
  })

  if (reg.status !== 200) {
    // 2) 已 bootstrap（或本次是后续 spec）：登录已有账号。
    const login = await api(request, '/api/auth/login', { method: 'POST', data: { email, password } })
    expect(
      login.status,
      `注册失败（${reg.status} ${JSON.stringify(reg.body)}）且登录也失败（${login.status} ${JSON.stringify(login.body)}）`,
    ).toBe(200)
  }

  const me = await api(request, '/api/auth/me')
  expect(me.status, `取 /api/auth/me 失败：${JSON.stringify(me.body)}`).toBe(200)
  const workspaceId = me.body?.workspace?.id
  expect(workspaceId, `/api/auth/me 没有返回 workspace.id：${JSON.stringify(me.body)}`).toBeTruthy()

  return { email, password, workspaceId, userId: me.body.user.id, me: me.body }
}

/**
 * 在已登录会话下新建一个独立工作区，并返回它的 id。
 * 每个 spec 用它来取得自己的隔离工作区（见 registerOrLogin 的说明）。
 */
export async function newWorkspace(
  request: APIRequestContext,
  opts: { viaSession: Session; label: string },
): Promise<string> {
  const r = await api(request, '/api/workspaces', {
    method: 'POST',
    workspaceId: opts.viaSession.workspaceId,
    data: { name: uniqueLabel(opts.label) },
  })
  expect(r.status, `新建工作区失败：${r.status} ${JSON.stringify(r.body)}`).toBe(200)
  expect(r.body.id, '新建工作区应返回 id').toBeTruthy()
  return r.body.id
}

/**
 * 一个 spec 的标准开场：确保有部署管理员会话 → 建一个独立工作区 → 返回该工作区的会话。
 *
 * 返回的 `Session.workspaceId` 就是这个 spec 独占的工作区；
 * 所有后续请求都带上它（作为 `X-Workspace-Id`）。
 */
export async function newIsolatedSession(
  request: APIRequestContext,
  label: string,
): Promise<Session> {
  const admin = await registerOrLogin(request)
  const workspaceId = await newWorkspace(request, { viaSession: admin, label })
  const me = await api(request, '/api/auth/me', { workspaceId })
  expect(me.status, `切到新工作区失败：${JSON.stringify(me.body)}`).toBe(200)
  expect(me.body?.workspace?.id, '新工作区应成为当前工作区').toBe(workspaceId)
  return { ...admin, workspaceId, me: me.body }
}

/**
 * 证明这个 run 是**独立 worker 进程**执行的，而不是 API 请求线程里的 inline 执行。
 *
 * 三条证据缺一不可：
 * 1. `/api/runs` 的 `worker_mode` 必须是 `external`（inline 模式下 API 自己会跑任务）；
 * 2. `/api/runs/workers` 里存在最近有心跳的 worker（说明外部 worker 进程真的活着）；
 * 3. 该 run 的 `state=RUNNING` 事件里记录了租约持有者，且不是 `inline:*`。
 *
 * 第 3 条是关键：inline worker 的 owner 固定是 `inline:<pid>`，
 * 外部 worker 的 owner 由 `worker_id()` 生成。
 */
/**
 * 证明某个 run 是被**独立 worker 进程**执行/推进的，而不是在 API 请求线程里跑完的。
 *
 * 三层证据，缺一不可：
 * 1. 后端自报 `worker_mode === 'external'`（`QAGENT_WORKER=external` 生效）；
 * 2. `/api/runs/workers` 里有 60 秒内的在线心跳（确实有 worker 进程活着并注册了自己）；
 * 3. 该 run 的 `state` 事件里，RUNNING 的 `owner` 不是 `inline:*`
 *    （inline 模式下 owner 形如 `inline:<pid>`，见 worker.py 的 worker_id()）。
 *
 * 注意**必须先等 run 被认领**：RUNNING 事件是 worker `claim()` 时才写入的
 * （worker.py: `self.runs.event(run_id, "state", {"state": RUNNING, "owner": self.owner})`）。
 * 若在入队后立刻查事件，只会看到 QUEUED，断言会误报。所以这里先轮询到 run 离开 QUEUED。
 */
export async function assertRanOnExternalWorker(
  request: APIRequestContext,
  workspaceId: string,
  runId: string,
): Promise<string> {
  const runs = await api(request, '/api/runs?limit=1', { workspaceId })
  expect(runs.status, `取 /api/runs 失败：${JSON.stringify(runs.body)}`).toBe(200)
  expect(
    runs.body.worker_mode,
    `E2E 要求回测经独立 worker 执行，但 worker_mode=${runs.body.worker_mode}`,
  ).toBe('external')

  const workers = await api(request, '/api/runs/workers', { workspaceId })
  expect(workers.status, `取 /api/runs/workers 失败：${JSON.stringify(workers.body)}`).toBe(200)
  expect(workers.body.mode, 'workers 接口应报告 external 模式').toBe('external')
  const online = (workers.body.workers || []).filter((w: any) => Date.now() - w.last_seen < 60_000)
  expect(
    online.length,
    `60 秒内没有在线 worker 心跳，无法证明任务由独立 worker 执行：${JSON.stringify(workers.body)}`,
  ).toBeGreaterThan(0)

  // 先等 run 被 worker 认领（离开 QUEUED），再去看执行者事件。
  await expect
    .poll(
      async () => {
        const r = await api(request, `/api/runs/${runId}`, { workspaceId })
        return r.body?.run?.state
      },
      {
        message: `run ${runId} 一直停在 QUEUED：没有 worker 认领它（worker 没起来或心跳失效）`,
        timeout: 60_000,
        intervals: [250, 500, 1_000],
      },
    )
    .not.toBe('QUEUED')

  const events = await api(request, `/api/runs/${runId}/events`, { workspaceId })
  expect(events.status, `取 run 事件失败：${JSON.stringify(events.body)}`).toBe(200)
  const owners = (events.body.items || [])
    .filter((e: any) => e.type === 'state' && e.data?.state === 'RUNNING')
    .map((e: any) => String(e.data?.owner || ''))
  expect(
    owners.length,
    `run ${runId} 没有 RUNNING 状态事件，无法确认执行者：${JSON.stringify(events.body.items)}`,
  ).toBeGreaterThan(0)
  const external = owners.find((o: string) => o && !o.startsWith('inline:'))
  expect(
    external,
    `run ${runId} 的 RUNNING 事件里没有非 inline 的执行者（owners=${JSON.stringify(owners)}）`,
  ).toBeTruthy()
  return external as string
}

/** 等一个 run 到终态；返回最终 run 对象。超时信息里带上最后一次看到的状态。 */
export async function waitRun(
  request: APIRequestContext,
  workspaceId: string,
  runId: string,
  opts: { timeoutMs?: number; states?: string[] } = {},
): Promise<any> {
  const deadline = Date.now() + (opts.timeoutMs ?? 90_000)
  let last: any = null
  while (Date.now() < deadline) {
    const r = await api(request, `/api/runs/${runId}`, { workspaceId })
    expect(r.status, `查询 run ${runId} 失败：${JSON.stringify(r.body)}`).toBe(200)
    last = r.body.run
    const terminal = ['COMPLETED', 'FAILED', 'CANCELED']
    if (opts.states ? opts.states.includes(last.state) : terminal.includes(last.state)) return last
    await new Promise((ok) => setTimeout(ok, 500))
  }
  throw new Error(`run ${runId} 在超时前没有到达目标状态，最后一次状态=${last?.state} 错误=${last?.error}`)
}

/* ------------------------------------------------------------------ */
/* DSL 规则：一份最小可用、离线也能跑出成交的规则                        */
/* ------------------------------------------------------------------ */

export interface DslSpec {
  dsl_version: 2
  name: string
  instrument: string
  timeframe: string
  params: Record<string, number>
  indicators: Record<string, { type: string; period: number | string; source?: string }>
  entry: unknown
  exit?: unknown
  position: { mode: 'weight' | 'quantity'; value: number | string; max_weight?: number; qty_step?: number; insufficient_funds?: 'reject' | 'reduce' }
  risk?: Record<string, number>
}

/**
 * 均线交叉规则：合成行情下也能稳定产生多次成交，便于断言账本与信号。
 * 与后端 `EXAMPLES.ma_cross_rsi` 同结构，但参数更小、预热更短。
 */
export function maCrossSpec(name: string, opts: { instrument?: string; timeframe?: string; fast?: number; slow?: number } = {}): DslSpec {
  const fast = opts.fast ?? 3
  const slow = opts.slow ?? 8
  return {
    dsl_version: 2,
    name,
    instrument: opts.instrument ?? 'BTC/USDT',
    timeframe: opts.timeframe ?? '1h',
    params: { fast, slow },
    indicators: {
      ma_fast: { type: 'sma', period: '$fast', source: 'close' },
      ma_slow: { type: 'sma', period: '$slow', source: 'close' },
    },
    entry: { op: 'cross_above', left: { ref: 'ma_fast' }, right: { ref: 'ma_slow' } },
    exit: { op: 'cross_below', left: { ref: 'ma_fast' }, right: { ref: 'ma_slow' } },
    position: { mode: 'weight', value: 0.8, max_weight: 1, qty_step: 0.0001, insufficient_funds: 'reject' },
    risk: { stop_loss: 0, take_profit: 0, max_trades_per_day: 0, drawdown_pause: 0 },
  }
}

/**
 * 「开盘即做多、靠止损止盈离场」规则。
 *
 * 实时模拟用它可以在**第一根被评估的 bar** 上就建仓，从而不必依赖具体行情路径
 * 恰好走出一次均线交叉。这样“启动 → 有持仓 → 暂停 → 模拟平仓”这条链路才是确定的。
 *
 * 必须带上指标与离场条件：平台自己的 DSL 校验（`/api/dsl/validate`）要求
 * `indicators` 至少一个、且 `exit` 或 `risk.stop_loss` / `take_profit` 至少要有一个，
 * 否则整条规则会被判为"仓位永远不会退出"而拒绝保存。这里用一个恒真的 SMA 指标
 * （`close > sma`，周期 1 时 sma 就是当前收盘价附近）配合宽止损，保证首根 bar 建仓。
 */
export function alwaysLongSpec(name: string, opts: { instrument?: string; timeframe?: string } = {}): DslSpec {
  return {
    dsl_version: 2,
    name,
    instrument: opts.instrument ?? 'BTC/USDT',
    timeframe: opts.timeframe ?? '1m',
    params: {},
    // 恒真入场：只要有行情就满足条件，第一根 bar 即建仓。
    indicators: { ref_ma: { type: 'sma', period: 1, source: 'close' } },
    entry: { op: 'gt', left: { price: 'close' }, right: { const: 0 } },
    // 离场交给止损/止盈（宽区间，短时间内不会被触发），满足"必须能退出"的校验。
    exit: null,
    position: { mode: 'weight', value: 0.5, max_weight: 1, qty_step: 0.0001, insufficient_funds: 'reduce' },
    risk: { stop_loss: 0.5, take_profit: 0.5, max_trades_per_day: 0, drawdown_pause: 0 },
  }
}

/** 保存 DSL 策略（保存即登记不可改写版本）。 */
export async function saveDsl(
  request: APIRequestContext,
  workspaceId: string,
  spec: DslSpec,
  opts: { slug?: string } = {},
): Promise<{ slug: string; version: any; strategy: any }> {
  const r = await api(request, '/api/dsl/strategies', {
    method: 'POST',
    workspaceId,
    headers: { 'Idempotency-Key': idemKey('dsl') },
    data: { name: spec.name, spec, slug: opts.slug },
  })
  expect(r.status, `保存 DSL 策略失败：${r.status} ${JSON.stringify(r.body)}`).toBe(200)
  expect(r.body?.strategy?.slug, `保存返回里没有 slug：${JSON.stringify(r.body)}`).toBeTruthy()
  return { slug: r.body.strategy.slug, version: r.body.version, strategy: r.body.strategy }
}

/** 取版本列表（会顺带把当前内容登记为版本）。 */
export async function listVersions(request: APIRequestContext, workspaceId: string, slug: string): Promise<any[]> {
  const r = await api(request, `/api/strategies/${slug}/versions`, { workspaceId })
  expect(r.status, `取版本列表失败：${JSON.stringify(r.body)}`).toBe(200)
  return r.body.versions
}

/** 冻结一个版本。 */
export async function freezeVersion(request: APIRequestContext, workspaceId: string, versionId: string): Promise<any> {
  const r = await api(request, `/api/strategy-versions/${versionId}/freeze`, { method: 'POST', workspaceId })
  expect(r.status, `冻结版本失败：${r.status} ${JSON.stringify(r.body)}`).toBe(200)
  return r.body.version
}

/* ------------------------------------------------------------------ */
/* CSV                                                                */
/* ------------------------------------------------------------------ */

/** 交易所导出的成交 CSV（列名走中英文自动识别，时间按 UTC 文本解释）。 */
export function fillsCsv(rows: { time: string; symbol: string; side: 'BUY' | 'SELL'; price: string; qty: string; fee?: string }[]): string {
  const head = 'time,symbol,side,price,qty,fee'
  const body = rows.map((r) => [r.time, r.symbol, r.side, r.price, r.qty, r.fee ?? '0'].join(',')).join('\n')
  return `${head}\n${body}\n`
}

/** 导入成交 CSV。 */
export async function importFills(
  request: APIRequestContext,
  workspaceId: string,
  filename: string,
  content: string,
  opts: { environment?: 'live' | 'testnet'; exchange?: string } = {},
): Promise<any> {
  const r = await api(request, '/api/imports', {
    method: 'POST',
    workspaceId,
    headers: { 'Idempotency-Key': idemKey('import') },
    data: {
      kind: 'fills',
      filename,
      content,
      environment: opts.environment ?? 'live',
      exchange: opts.exchange ?? 'binance',
    },
  })
  expect(r.status, `导入 CSV 失败：${r.status} ${JSON.stringify(r.body)}`).toBe(200)
  return r.body.import
}

/* ------------------------------------------------------------------ */
/* 浏览器侧                                                           */
/* ------------------------------------------------------------------ */

/**
 * 通过界面完成登录（真的走一次表单，而不是只调接口）。
 *
 * `Login.tsx` 在部署尚未 bootstrap 时默认切到"注册"模式，所以这里先看页面上有没有
 * 邀请码输入框（只有注册模式才有），有就点"登录"切回登录模式，再填表提交。
 *
 * 关于工作区：前端把目标工作区存在 `localStorage['qa.workspace']`（见 web/src/http.ts），
 * 每个请求带 `X-Workspace-Id`。一个用户可能有多个工作区，**不指定就会落到 memberships[0]**，
 * 而 E2E 里每个 spec 用的是自己新建的工作区。所以登录前先把 `qa.workspace` 写成目标工作区，
 * 保证界面上的读写与 API 准备阶段落在同一个工作区。
 */
async function openAuthDrawer(page: Page): Promise<void> {
  const drawer = page.locator('.lp-drawer')
  if (!(await drawer.count()) || !(await drawer.isVisible())) {
    // 介绍页把登录 / 注册放在右侧抽屉里；未 bootstrap 或带邀请码时会自动打开。
    await page.getByRole('button', { name: '登录 / 注册' }).first().click()
    await expect(drawer, '登录 / 注册抽屉没有打开').toBeVisible({ timeout: 30_000 })
  }
}

export async function uiLogin(page: Page, session: { email: string; password: string; workspaceId?: string }): Promise<void> {
  // 先访问一次同源页面，才能写该源的 localStorage。
  await page.goto('/')
  await page.waitForLoadState('domcontentloaded')
  if (session.workspaceId) {
    await page.evaluate(
      ([key, id]) => window.localStorage.setItem(key, id),
      ['qa.workspace', session.workspaceId] as const,
    )
    await page.reload()
    await page.waitForLoadState('domcontentloaded')
  }

  // 登录模式现在支持“用户名或邮箱”，输入框是 autocomplete=username（不是 type=email）。
  // 注册模式才有邀请码输入框；有的话先切回登录，再定位账号输入框。
  await openAuthDrawer(page)
  const invite = page.locator('input[placeholder="必填"], input[placeholder="可选：加入已有工作区"]')
  if (await invite.count()) {
    await page.locator('.auth-switch button').click()
    await expect(invite).toHaveCount(0)
  }
  const email = page.locator('input[autocomplete="username"]').first()
  await expect(email, '登录页没有渲染出账号输入框').toBeVisible({ timeout: 30_000 })

  await email.fill(session.email)
  await page.locator('input[type="password"]').fill(session.password)
  await page.locator('form button[type="submit"], form button.primary').first().click()

  // 进入工作台：侧边栏主导航出现。
  await expect(page.locator('nav[aria-label="主导航"]'), '提交登录表单后没有进入工作台').toBeVisible({
    timeout: 30_000,
  })

  // 登录流程会刷新工作区列表；确认前端锁定的就是本次 spec 的工作区，
  // 否则后续界面操作会打到别的工作区，断言会以"看不到刚建的数据"的形式失败。
  if (session.workspaceId) {
    await expect
      .poll(
        async () => page.evaluate(() => window.localStorage.getItem('qa.workspace')),
        { message: '登录后 localStorage 里的目标工作区被改掉了', timeout: 30_000 },
      )
      .toBe(session.workspaceId)
  }
}

/** 通过界面注册（用于部署尚未 bootstrap 的场景）。 */
export async function uiRegister(page: Page, session: { email: string; password: string; name?: string }): Promise<void> {
  await page.goto('/')
  await page.waitForLoadState('domcontentloaded')

  // 登录模式只有账号输入框；注册模式才有 type=email。先确保切到注册模式。
  await openAuthDrawer(page)
  const account = page.locator('input[autocomplete="username"]').first()
  await expect(account).toBeVisible({ timeout: 30_000 })
  const email = page.locator('input[type="email"]')
  if (!(await email.count())) {
    await page.locator('.auth-switch button').click()
    await expect(email).toBeVisible({ timeout: 30_000 })
  }
  await account.fill('e2e' + Date.now().toString(36))
  await email.fill(session.email)
  const name = page.locator('input[placeholder^="可选"]')
  if (await name.count()) await name.fill(session.name || 'E2E 用户')
  await page.locator('input[type="password"]').fill(session.password)
  await page.locator('form button[type="submit"], form button.primary').first().click()

  await expect(page.locator('nav[aria-label="主导航"]'), '提交注册表单后没有进入工作台').toBeVisible({
    timeout: 30_000,
  })
}

/* ------------------------------------------------------------------ */
/* 实时模拟                                                           */
/* ------------------------------------------------------------------ */

/** 通过界面启动一次实时模拟，返回会话 id（从 hash 路由里取）。 */
export async function uiStartPaper(
  page: Page,
  opts: { slug: string; source?: 'exchange' | 'demo'; cash?: number },
): Promise<string> {
  await page.goto('/#/paper')
  // 必须用 `exact: true`：「启动新实验」那个 Panel 的 subtitle 里也含「模拟交易」，
  // 而 `Panel` 会把 subtitle 同时写进 `<span class="panel-info">` 的 `title` 与 `aria-label`，
  // 于是它的可访问名变成「启动新实验i」——子串匹配会把它一起算进来，
  // 报 `strict mode violation ... resolved to 2 elements`。
  // 页面标题是唯一的 `<h1 data-section="PAPER TRADING">`，用精确名 + 级别锁定它。
  await expect(
    page.getByRole('heading', { name: '纸面模拟', exact: true, level: 1 }),
  ).toBeVisible({ timeout: 30_000 })

  // 策略下拉：option 的 value 就是 slug，按 value 精确选中。
  const strategySelect = page.locator('.side-column select').first()
  await expect(strategySelect).toBeVisible({ timeout: 30_000 })
  await expect(
    strategySelect.locator(`option[value="${opts.slug}"]`),
    `策略下拉里没有已批准的 ${opts.slug}（还没批准？）`,
  ).toHaveCount(1, { timeout: 30_000 })
  await strategySelect.selectOption(opts.slug)

  if (opts.source === 'demo') {
    await page.locator('.side-column select').nth(2).selectOption('demo') // 策略 / 成交方式 / 行情 / 交易所
    await expect(page.getByText('演示行情由时间函数生成', { exact: false })).toBeVisible()
  }

  if (opts.cash !== undefined) {
    const cash = page.locator('.side-column input[type="number"]').first()
    await cash.fill(String(opts.cash))
  }

  await page.getByRole('button', { name: '启动实验' }).click()

  // 启动后前端 onNavigate(`paper/${id}`)，等详情页工具栏出现。
  await expect(page.getByRole('button', { name: '模拟平仓并结束' })).toBeVisible({ timeout: 60_000 })

  // 路由形如 `#/paper/<session id>`，取**最后一段**作为会话 id。
  // （不要用 split('/')[1]：那样拿到的是字面量 'paper'，会变成一个永远查不到的 run id。）
  const hash = page.url().split('#')[1] || ''
  const segments = hash.split('/').filter(Boolean)
  expect(segments[0], `实时模拟的路由第一段应是 paper，实际 ${hash}`).toBe('paper')
  const id = segments[segments.length - 1]
  expect(id, `没能从 URL 里取到会话 id：${page.url()}`).toBeTruthy()
  expect(id, '取到的会话 id 不应是路由名本身').not.toBe('paper')
  return id
}

/** 通过界面启动实时模拟（demo 行情），断言状态与命令按钮。 */
export async function expectPaperStatus(page: Page, label: string): Promise<void> {
  await expect(page.locator('.table-pill').first()).toContainText(label, { timeout: 60_000 })
}

/** 断言页面上出现了某个 toast 文案（notify 会 4~8.5 秒后自动消失）。 */
export async function expectToast(page: Page, text: string | RegExp): Promise<void> {
  await expect(page.locator('.toast, [class*="toast"]').first()).toContainText(text, { timeout: 20_000 })
}

/** 等一个 run 在界面上进入终态（后端轮询，避免依赖页面的轮询节奏）。 */
export async function apiRunResult(request: APIRequestContext, workspaceId: string, runId: string): Promise<any> {
  const run = await waitRun(request, workspaceId, runId)
  expect(run.state, `run ${runId} 失败：${run.error}`).toBe('COMPLETED')
  const r = await api(request, `/api/runs/${runId}/result`, { workspaceId })
  expect(r.status, `取 run 结果失败：${JSON.stringify(r.body)}`).toBe(200)
  return r.body.result
}

/* ------------------------------------------------------------------ */
/* 实时模拟（接口侧）                                                  */
/* ------------------------------------------------------------------ */

/** 批准（冻结）一个策略版本，使它有资格进入实时模拟。 */
export async function approveStrategy(request: APIRequestContext, workspaceId: string, slug: string): Promise<any> {
  const r = await api(request, `/api/studio/strategies/${slug}/approve`, { method: 'POST', workspaceId })
  expect(r.status, `批准策略失败：${r.status} ${JSON.stringify(r.body)}`).toBe(200)
  return r.body.strategy
}

/** 启动实时模拟（demo 行情：时间确定性、离线可用，界面会明确标注为演示行情）。 */
export async function startPaper(
  request: APIRequestContext,
  workspaceId: string,
  opts: { slug: string; source?: 'exchange' | 'demo'; cash?: number; timeframe?: string },
): Promise<any> {
  const r = await api(request, '/api/studio/paper/start', {
    method: 'POST',
    workspaceId,
    data: {
      slug: opts.slug,
      source: opts.source ?? 'demo',
      exchange: 'binance',
      initial_cash: opts.cash ?? 10_000,
      ...(opts.timeframe ? { timeframe: opts.timeframe } : {}),
    },
  })
  expect(r.status, `启动实时模拟失败：${r.status} ${JSON.stringify(r.body)}`).toBe(200)
  return r.body.session
}

/** 取实时模拟会话（含 run 状态，run 状态是唯一真相）。 */
export async function paperSession(request: APIRequestContext, workspaceId: string, id: string): Promise<any> {
  const r = await api(request, '/api/studio/paper?limit=100', { workspaceId })
  expect(r.status, `取模拟实验列表失败：${JSON.stringify(r.body)}`).toBe(200)
  const found = (r.body.sessions || []).find((s: any) => s.id === id)
  expect(found, `实验 ${id} 不在列表里：${JSON.stringify(r.body.sessions?.map((s: any) => s.id))}`).toBeTruthy()
  return found
}

/** 发一条实时模拟命令（pause / resume / liquidate …）。 */
export async function paperCommand(
  request: APIRequestContext,
  workspaceId: string,
  id: string,
  command: 'pause' | 'resume' | 'stop' | 'liquidate' | 'tick',
): Promise<any> {
  const r = await api(request, `/api/studio/paper/${id}/${command}`, { method: 'POST', workspaceId })
  expect(r.status, `发送命令 ${command} 失败：${r.status} ${JSON.stringify(r.body)}`).toBe(200)
  return r.body.session
}

/**
 * 等实时模拟**会话**进入某个状态。
 *
 * 为什么不能只等 `waitPaperRunState`：会话的 `status` 是 worker 在推进过程中
 * 写进 session 投影的，而 run 的 `state` 由 runner 自己更新，两者不是同一次写入。
 * 实测（04-paper:66）会出现"run 已经 RUNNING、session 还是 queued"的瞬间——
 * 这是正常的步进延迟，不是产品缺陷；直接断言就会随机失败。
 */
export async function waitPaperSessionStatus(
  request: APIRequestContext,
  workspaceId: string,
  id: string,
  status: string,
  timeoutMs = 60_000,
): Promise<any> {
  const deadline = Date.now() + timeoutMs
  let last: any = null
  while (Date.now() < deadline) {
    last = await paperSession(request, workspaceId, id)
    if (last.status === status) return last
    await new Promise((ok) => setTimeout(ok, 500))
  }
  throw new Error(
    `实时模拟会话 ${id} 在超时前没有进入 ${status}，最后一次状态=${JSON.stringify(last?.status)}`,
  )
}

/** 等实时模拟的 run 进入某个状态（run 状态是唯一真相）。 */
export async function waitPaperRunState(
  request: APIRequestContext,
  workspaceId: string,
  id: string,
  state: string,
  timeoutMs = 60_000,
): Promise<any> {
  const deadline = Date.now() + timeoutMs
  let last = ''
  while (Date.now() < deadline) {
    const r = await api(request, `/api/runs/${id}`, { workspaceId })
    if (r.status === 200) {
      last = r.body.run.state
      if (last === state) return r.body.run
    }
    await new Promise((ok) => setTimeout(ok, 500))
  }
  throw new Error(`实时模拟 run ${id} 在超时前没有进入 ${state}，最后一次状态=${last}`)
}

/**
 * 等实时模拟产生至少 `count` 笔成交。
 *
 * 实时模拟只在**新的已收盘 bar** 上调用策略，所以这里必须真的等一根 bar 过去：
 * 用 1 分钟周期时最长约 60 秒；CI / 低配机器上 worker 认领与 tick 调度可能更慢，
 * 因此留 240 秒预算，避免把“慢”误判成“没有成交”。返回成交数组。
 */
export async function waitPaperFills(
  request: APIRequestContext,
  workspaceId: string,
  id: string,
  count = 1,
  timeoutMs = 240_000,
): Promise<any[]> {
  const deadline = Date.now() + timeoutMs
  let last: any[] = []
  while (Date.now() < deadline) {
    const r = await api(request, `/api/runs/${id}/fills`, { workspaceId })
    if (r.status === 200) {
      last = r.body.items || []
      if (last.length >= count) return last
    }
    // 顺带确认 run 没有失败，失败时立刻给出明确原因而不是干等到超时。
    const run = await api(request, `/api/runs/${id}`, { workspaceId })
    if (run.status === 200 && ['FAILED', 'CANCELED'].includes(run.body.run.state)) {
      throw new Error(`实时模拟 run ${id} 提前结束：${run.body.run.state} ${run.body.run.error || ''}`)
    }
    await new Promise((ok) => setTimeout(ok, 1_500))
  }
  return last
}
