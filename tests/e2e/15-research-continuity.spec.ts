import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import path from 'node:path'
import { api, newWorkspace, registerOrLogin, uiLogin, waitRun, type Session } from './helpers'
import { ARTIFACTS } from './run-env'

const VIEW = { width: 1366, height: 768 }

async function seedResearch(request: APIRequestContext, workspaceId: string) {
  const upload = await request.post('/api/workflows/files?name=round2-continuity.csv', {
    headers: { 'X-Workspace-Id': workspaceId, 'Content-Type': 'application/octet-stream' },
    data: 'year,value\n2021,1\n2022,2\n2023,3\n2024,4\n',
  })
  expect(upload.ok()).toBeTruthy()
  const file = await upload.json()
  const catalog = await api(request, '/api/workflows/catalog', { workspaceId })
  const flow = catalog.body.templates.find((f: { title: string }) => f.title === 'CSV数据研究')
  flow.title = 'Round 2 最近结果'
  flow.nodes[0].params.file = file.name
  const chart = catalog.body.cards.find((d: { kind: string }) => d.kind === 'research.chart')
  flow.nodes.push({ id: 'chart-r2', kind: chart.kind, version: chart.version, title: '年度趋势图', params: { chart_type: 'line', x: 'year', y: 'value' }, position: { x: 350, y: 360 } })
  flow.edges.push({ id: 'chart-r2-edge', source: 'source', source_port: 'data', target: 'chart-r2', target_port: 'data' })
  const saved = await api(request, '/api/workflows', { workspaceId, method: 'POST', data: { workflow: flow } })
  expect(saved.status).toBe(200)
  const started = await api(request, `/api/workflows/${saved.body.id}/run`, { workspaceId, method: 'POST', data: {} })
  expect(started.status).toBe(200)
  const run = await waitRun(request, workspaceId, started.body.id)
  expect(run.state, run.error).toBe('COMPLETED')
  return { wid: saved.body.id as string, runId: started.body.id as string, title: flow.title as string }
}

async function seedSecondWorkflow(request: APIRequestContext, workspaceId: string, wid: string) {
  const current = await api(request, `/api/workflows/${wid}`, { workspaceId })
  expect(current.status).toBe(200)
  const copied = await api(request, '/api/workflows', {
    workspaceId,
    method: 'POST',
    data: { workflow: { ...current.body.workflow, title: 'Round 2 后保存的研究' } },
  })
  expect(copied.status).toBe(200)
  return { wid: copied.body.id as string, title: 'Round 2 后保存的研究' }
}

type PreferenceCalls = {
  gets: number
  posts: Array<{ close_action?: string; last_workflow_id?: string }>
}

/**
 * 本地模式只在 E2E 浏览器外壳里模拟：local 控制端点用测试桩，
 * 工作流、运行、工件和摘要仍走真实后端 API，避免用假结果证明“最近结果”。
 *
 * 必须在 uiLogin 之前安装，否则首次 App bootstrap 会先拿到真实 account mode，
 * 后续同 hash 的 goto 不会重建 DesktopTitleBar 的本地状态。
 */
async function mockLocalChrome(page: Page, preference: { close_action: string; last_workflow_id?: string }, projectName = 'E2E 本地项目'): Promise<PreferenceCalls> {
  const calls: PreferenceCalls = { gets: 0, posts: [] }
  await page.route('**/api/auth/me', async (route) => {
    const response = await route.fetch()
    const value = await response.json()
    await route.fulfill({
      status: response.status(),
      headers: { ...response.headers(), 'content-type': 'application/json' },
      body: JSON.stringify({ ...value, mode: 'local' }),
    })
  })
  await page.route('**/api/local/info', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        mode: 'local',
        project: { name: projectName, id: 'e2e-local-project' },
        root: 'C:\\e2e-project',
        preferences: preference,
      }),
    })
  })
  await page.route('**/api/local/preferences', async (route) => {
    if (route.request().method() === 'POST') {
      const payload = route.request().postDataJSON() as { close_action?: string; last_workflow_id?: string }
      calls.posts.push(payload)
      if (payload.close_action) preference.close_action = payload.close_action
      if (payload.last_workflow_id !== undefined) preference.last_workflow_id = payload.last_workflow_id
    } else {
      calls.gets += 1
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(preference) })
  })
  return calls
}

async function installTauri(page: Page) {
  await page.addInitScript(() => {
    // 冷启动等价：每个真实文档加载都在 React 挂载前清掉会话草稿；
    // hash 内导航不会执行这里，因此不影响页面内正常操作。
    try { sessionStorage.clear() } catch { /* 隐私模式 */ }
    ;(window as unknown as { __TAURI__: unknown }).__TAURI__ = {
      core: { invoke: async () => null },
      window: {
        getCurrentWindow: () => ({
          minimize: async () => undefined,
          toggleMaximize: async () => undefined,
          close: async () => undefined,
        }),
      },
      event: { listen: async () => () => undefined },
    }
  })
}

async function waitForImage(page: Page) {
  const image = page.locator('.rs-dock .rs-artifact .rs-image img').first()
  await expect(image).toBeVisible()
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true)
  const geometry = await page.evaluate(() => {
    const img = document.querySelector<HTMLImageElement>('.rs-dock .rs-artifact .rs-image img')
    const container = document.querySelector<HTMLElement>('.rs-dock-body')
    if (!img || !container) return null
    const r = img.getBoundingClientRect()
    const c = container.getBoundingClientRect()
    const left = Math.max(r.left, c.left, 0)
    const right = Math.min(r.right, c.right, innerWidth)
    const top = Math.max(r.top, c.top, 0)
    const bottom = Math.min(r.bottom, c.bottom, innerHeight)
    return {
      naturalWidth: img.naturalWidth,
      naturalHeight: img.naturalHeight,
      width: Math.max(0, right - left),
      height: Math.max(0, bottom - top),
    }
  })
  expect(geometry, '真实图表没有进入结果滚动容器').not.toBeNull()
  expect(geometry!.naturalWidth).toBeGreaterThanOrEqual(200)
  expect(geometry!.naturalHeight).toBeGreaterThanOrEqual(80)
  expect(geometry!.width).toBeGreaterThanOrEqual(120)
  expect(geometry!.height).toBeGreaterThanOrEqual(40)
  return geometry!
}

test('Round 2：启动恢复上次研究、显式首页、一键最近结果与 1366 标题栏', async ({ page, request }) => {
  const session: Session = await registerOrLogin(request)
  const first = await seedResearch(request, session.workspaceId)
  const second = await seedSecondWorkflow(request, session.workspaceId, first.wid)
  const preference = { close_action: 'background', last_workflow_id: first.wid }
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))

  await installTauri(page)
  await page.setViewportSize(VIEW)
  const calls = await mockLocalChrome(page, preference)
  await uiLogin(page, session)

  // 冷启动等价：先到无参数研究入口，再用真实 reload 重建 App（sessionStorage 在挂载前已清空）。
  await page.goto('/#/workflows')
  await page.reload()
  const title = page.getByRole('textbox', { name: '研究名称' })
  await expect(title).toHaveValue(first.title)
  await expect(page.locator('.dtb')).toBeVisible()
  await expect(page.getByRole('button', { name: '最近结果', exact: true })).toBeEnabled()
  await expect.poll(() => calls.gets).toBeGreaterThan(0)
  expect(calls.posts, '自动恢复旧研究不应写偏好').toHaveLength(0)

  // 用户选择另一条研究后才写入后端偏好；验证真实 POST 请求体，而不只是 mock 变量。
  await page.getByRole('combobox', { name: '打开已保存工作流' }).selectOption(second.wid)
  await expect(title).toHaveValue(second.title)
  await expect.poll(() => calls.posts.length).toBeGreaterThan(0)
  expect(calls.posts.at(-1)?.last_workflow_id).toBe(second.wid)
  await expect.poll(() => preference.last_workflow_id).toBe(second.wid)

  // 同一 URL 的真实 reload，证明恢复来自后端项目偏好而不是列表第一项或 localStorage。
  const getsBeforeRestore = calls.gets
  const postsBeforeRestore = calls.posts.length
  await page.reload()
  await expect(title).toHaveValue(second.title)
  await expect(page.getByRole('button', { name: '最近结果', exact: true })).toBeEnabled()
  await expect.poll(() => calls.gets).toBeGreaterThan(getsBeforeRestore)
  expect(calls.posts, '冷启动恢复不应写偏好').toHaveLength(postsBeforeRestore)

  // 偏好指向已删除研究时，真实 reload 回退到列表最新有效研究，不重定向循环、不写失效 id。
  const getsBeforeDeleted = calls.gets
  const postsBeforeDeleted = calls.posts.length
  preference.last_workflow_id = 'workflow-deleted'
  await page.reload()
  await expect(title).toHaveValue(second.title)
  await expect(page).toHaveURL(/#\/workflows$/)
  await expect.poll(() => calls.gets).toBeGreaterThan(getsBeforeDeleted)
  expect(calls.posts, '失效 id 不应回写').toHaveLength(postsBeforeDeleted)
  expect(errors).toEqual([])

  // 1366×768 标题栏：动作、品牌、窗口按钮互不覆盖，命中自身。
  const fit = await page.evaluate(() => {
    const box = (selector: string) => {
      const el = document.querySelector(selector)
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }
    }
    const recent = document.querySelector<HTMLElement>('.dtb-recent')
    const hit = recent
      ? (() => {
          const r = recent.getBoundingClientRect()
          const element = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2)
          return !!element && recent.contains(element)
        })()
      : false
    return {
      dtb: box('.dtb'),
      brand: box('.dtb-brand'),
      recent: box('.dtb-recent'),
      actions: box('.dtb-actions'),
      controls: document.querySelectorAll('.dtb-actions .dtb-btn').length,
      hit,
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth,
    }
  })
  expect(fit.recent).not.toBeNull()
  expect(fit.brand).not.toBeNull()
  expect(fit.actions).not.toBeNull()
  expect(fit.recent!.left).toBeGreaterThanOrEqual(fit.brand!.right - 1)
  expect(fit.recent!.right).toBeLessThanOrEqual(fit.actions!.left + 1)
  expect(fit.recent!.top).toBeGreaterThanOrEqual(fit.dtb!.top)
  expect(fit.recent!.bottom).toBeLessThanOrEqual(fit.dtb!.bottom + 1)
  expect(fit.recent!.height).toBeGreaterThanOrEqual(22)
  expect(fit.controls).toBe(3)
  expect(fit.hit).toBe(true)
  expect(fit.scrollWidth).toBeLessThanOrEqual(fit.innerWidth + 1)

  // 显式 #/home 仍是普通首页，真实 reload 后也不被本地启动逻辑劫持。
  await page.goto('/#/home')
  await page.reload()
  await expect(page.locator('.home-hero')).toBeVisible()
  await expect(page).toHaveURL(/#\/home$/)
  await expect(page.getByRole('button', { name: '最近结果', exact: true })).toBeEnabled()

  // 从另一个主页面，一次点击进入真实运行深链，并等真实图表加载完成。
  await page.getByRole('button', { name: '最近结果', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`#/workflows/${first.wid}\\?run_id=${first.runId}$`))
  await expect(page.locator('.rs-dock')).toBeVisible()
  await waitForImage(page)
  await expect(page.locator('.rs-dock')).toContainText(first.title)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round2-1366x768-recent-result.png') })

  expect(errors).toEqual([])
})

test('Round 2：无运行项目禁用最近结果，不打开空面板', async ({ page, request }) => {
  const session: Session = await registerOrLogin(request)
  const emptyWorkspace = await newWorkspace(request, { viaSession: session, label: 'round2-empty' })
  const preference = { close_action: 'ask' }
  await installTauri(page)
  await page.setViewportSize(VIEW)
  await mockLocalChrome(page, preference)
  await uiLogin(page, session)
  await page.evaluate((workspaceId) => localStorage.setItem('qa.workspace', workspaceId), emptyWorkspace)
  await page.goto('/#/home')
  await page.reload()
  await expect(page.locator('.home-hero')).toBeVisible()

  const disabled = page.getByRole('button', { name: '暂无研究结果' })
  await expect(disabled).toBeDisabled()
  await expect(disabled).toHaveAttribute('title', /还没有已完成的研究结果/)
  await expect(page.locator('.rs-dock')).toHaveCount(0)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round2-1366x768-no-result.png') })
})
