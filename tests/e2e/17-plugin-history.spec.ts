import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import path from 'node:path'
import { api, newIsolatedSession, uiLogin, type Session } from './helpers'
import { ARTIFACTS } from './run-env'

const VIEW = { width: 1366, height: 768 }
const REASON = '已停用，仅查看历史'

/** Any non-GET factor request while policy is pending/errored is a fail-open bug. */
function trackFactorWrites(page: Page): string[] {
  const writes: string[] = []
  page.on('request', (request) => {
    if (request.method() === 'GET') return
    const pathname = new URL(request.url()).pathname
    if (pathname === '/api/factors' || pathname.startsWith('/api/factors/')) {
      writes.push(`${request.method()} ${pathname}`)
    }
  })
  return writes
}

async function disable(request: APIRequestContext, workspaceId: string, plugin: string) {
  const r = await api(request, `/api/auth/research-plugins/${plugin}`, {
    workspaceId,
    method: 'PUT',
    data: { enabled: false },
  })
  expect(r.status, `停用 ${plugin} 失败：${JSON.stringify(r.body)}`).toBe(200)
}

async function enable(request: APIRequestContext, workspaceId: string, plugin: string) {
  const r = await api(request, `/api/auth/research-plugins/${plugin}`, {
    workspaceId,
    method: 'PUT',
    data: { enabled: true },
  })
  expect(r.status, `重新启用 ${plugin} 失败：${JSON.stringify(r.body)}`).toBe(200)
}

/** Real backend report, produced before the capability is disabled. No fake result payloads. */
async function seedFactor(request: APIRequestContext, workspaceId: string) {
  const name = 'Round 4 历史反转'
  const created = await api(request, '/api/factors', {
    workspaceId,
    method: 'POST',
    data: { name, expression: '-returns(5)', evaluate: false, status: 'candidate' },
  })
  expect(created.status, JSON.stringify(created.body)).toBe(200)
  const id = created.body.factor.id as string
  const refreshed = await api(request, `/api/factors/${id}/refresh`, {
    workspaceId,
    method: 'POST',
    data: { source: 'synthetic', bars_limit: 600 },
  })
  expect(refreshed.status, JSON.stringify(refreshed.body)).toBe(200)
  expect(refreshed.body.report?.summary?.rank_ic, '历史因子报告没有真实 RankIC').not.toBeNull()
  return { id, name, expression: created.body.factor.expression as string }
}

async function seedModel(request: APIRequestContext, workspaceId: string) {
  const name = 'Round 4 历史模型'
  const trained = await api(request, '/api/models/train', {
    workspaceId,
    method: 'POST',
    data: {
      model: 'ridge', universe: 'major10', timeframe: '4h', bars_limit: 400, folds: 3,
      source: 'synthetic', wait: true,
    },
  })
  expect(trained.status, JSON.stringify(trained.body)).toBe(200)
  const job = trained.body.job
  expect(job.status, JSON.stringify(job)).toBe('done')
  const saved = await api(request, '/api/models', {
    workspaceId,
    method: 'POST',
    data: { run_id: job.run_id, name, status: 'candidate' },
  })
  expect(saved.status, JSON.stringify(saved.body)).toBe(200)
  return { id: saved.body.model.id as string, runId: job.run_id as string, name }
}

async function seedPortfolio(request: APIRequestContext, workspaceId: string) {
  const name = 'Round 4 历史组合'
  const evaluated = await api(request, '/api/portfolio/evaluate', {
    workspaceId,
    method: 'POST',
    data: {
      config: { optimizer: 'hrp' }, universe: 'major10', timeframe: '4h', bars_limit: 500, source: 'synthetic',
    },
  })
  expect(evaluated.status, JSON.stringify(evaluated.body)).toBe(200)
  const run = evaluated.body.run
  const saved = await api(request, '/api/portfolio', {
    workspaceId,
    method: 'POST',
    data: { run_id: run.id, name, status: 'candidate' },
  })
  expect(saved.status, JSON.stringify(saved.body)).toBe(200)
  return { id: saved.body.scheme.id as string, runId: run.id as string, name }
}

async function seedExecution(request: APIRequestContext, workspaceId: string) {
  const name = 'Round 4 历史执行'
  const evaluated = await api(request, '/api/execution/evaluate', {
    workspaceId,
    method: 'POST',
    data: {
      config: { algo: 'twap', episodes: 6 }, symbols: ['BTC/USDT'], source: 'synthetic', steps: 800,
    },
  })
  expect(evaluated.status, JSON.stringify(evaluated.body)).toBe(200)
  const run = evaluated.body.run
  const saved = await api(request, '/api/execution', {
    workspaceId,
    method: 'POST',
    data: { run_id: run.id, name, status: 'candidate' },
  })
  expect(saved.status, JSON.stringify(saved.body)).toBe(200)
  return { id: saved.body.scheme.id as string, runId: run.id as string, name }
}

async function openSession(page: Page, session: Session) {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await uiLogin(page, session)
  await page.setViewportSize(VIEW)
  return errors
}

/** Wait two animation frames until the box geometry stops changing after a resize. */
async function settledGeometry(page: Page) {
  return page.evaluate(async () => {
    const box = (selector: string) => {
      const el = document.querySelector<HTMLElement>(selector)
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { left: r.left, right: r.right, top: r.top, width: r.width, height: r.height }
    }
    const snap = () => ({
      innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      banner: box('.ro-banner'),
      gate: box('.ro-gate'),
      table: box('.table-wrap'),
      report: box('.fr-full, .panel'),
    })
    let previous = snap()
    const deadline = performance.now() + 3000
    while (performance.now() < deadline) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
      const current = snap()
      if (JSON.stringify(current) === JSON.stringify(previous) && current.scrollWidth <= current.innerWidth + 1) return current
      previous = current
    }
    return snap()
  })
}

test('Round 4：因子停用后历史列表/详情/图表/导出/筛选可读，重新启用无需整页刷新', async ({ page, request }) => {
  const session: Session = await newIsolatedSession(request, 'round4-factor-history')
  const factor = await seedFactor(request, session.workspaceId)
  await disable(request, session.workspaceId, 'factor')
  const errors = await openSession(page, session)
  await page.goto('/#/factors')
  // Set only after the last full navigation: the re-enable path below must keep this marker.
  await page.evaluate(() => { (window as unknown as { __round4Alive?: string }).__round4Alive = 'alive' })
  await expect(page.locator('.ro-banner')).toBeVisible()
  await expect(page.locator('.ro-banner')).toContainText(REASON)
  await expect(page.locator('.ro-banner')).toContainText('因子研究')
  // The generic banner names the capability, not the asset; asset presence is the row check below.
  const row = page.locator('.fx-table tbody tr').filter({ hasText: factor.name })
  await expect(row, '停用后历史因子列表没有读到已保存的资产').toBeVisible()

  // History filters/search stay live; they are reads, not new research.
  await page.getByLabel('搜索因子').fill('Round 4')
  await expect(row).toBeVisible()
  await page.getByLabel('搜索因子').fill('')
  await row.click()
  await expect(page).toHaveURL(new RegExp(`#/factors/${factor.id}`))
  await expect(page.getByRole('button', { name: 'Python 代码' })).toBeEnabled()
  await expect(page.getByRole('button', { name: '用最新数据重新评估' })).toBeDisabled()
  await expect(page.getByLabel('状态')).toBeDisabled()
  await expect(page.getByRole('button', { name: '在 AI 研究室中改进' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '删除' })).toBeDisabled()
  const chart = page.locator('.fr-full .chart2 svg[role="img"]').first()
  await expect(chart, '历史因子报告图表没有加载').toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round4-1366x768-factor-history.png') })

  // Existing frozen source stays exportable.
  await page.getByRole('button', { name: 'Python 代码' }).click()
  const modal = page.locator('.modal')
  await expect(modal.locator('.code-view')).toContainText('def ', { timeout: 30_000 })
  await expect(modal.getByRole('button', { name: /下载 .*\.py/ })).toBeEnabled()
  await modal.getByRole('button', { name: '关闭' }).click()

  await page.emulateMedia({ colorScheme: 'dark' })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(chart).toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round4-1366x768-factor-history-dark.png') })
  await page.emulateMedia({ colorScheme: 'light' })

  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.locator('.ro-banner')).toBeVisible()
  await expect(chart).toBeVisible()
  const mobile = await settledGeometry(page)
  expect(mobile.banner, '390 宽下状态条不存在').not.toBeNull()
  expect(mobile.banner!.left).toBeGreaterThanOrEqual(-1)
  expect(mobile.banner!.right, '390 宽下状态条越出视口').toBeLessThanOrEqual(mobile.innerWidth + 1)
  expect(mobile.scrollWidth, '390 宽下详情页横向溢出').toBeLessThanOrEqual(mobile.innerWidth + 1)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round4-390x844-factor-history.png') })
  await page.setViewportSize(VIEW)

  // Fresh evaluator route is a compact explanation, not a full-page dead end and not a mounted form.
  await page.getByRole('button', { name: '评估器' }).click()
  await expect(page.locator('.ro-gate')).toBeVisible()
  await expect(page.locator('.ro-gate')).toContainText(REASON)
  await expect(page.getByRole('textbox', { name: '因子表达式' })).toHaveCount(0)

  // Banner settings action opens the real plugin switch; enabling restores controls in-place.
  await page.locator('.ro-banner').getByRole('button', { name: '插件管理' }).click()
  await expect(page).toHaveURL(/#\/settings\/plugins/)
  const toggle = page.getByRole('switch', { name: '因子研究 启用' })
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  await page.locator('.set-close.desktop-only').click()
  await expect(page).toHaveURL(/#\/factors\/new/)
  await expect(page.locator('.ro-banner')).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: '因子表达式' })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as { __round4Alive?: string }).__round4Alive)).toBe('alive')

  // The recovered detail keeps the same historical report and enables the write controls.
  await page.getByRole('button', { name: '因子库' }).click()
  await page.locator('.fx-table tbody tr').filter({ hasText: factor.name }).click()
  await expect(page.getByRole('button', { name: '用最新数据重新评估' })).toBeEnabled()
  await expect(page.getByLabel('状态')).toBeEnabled()
  await expect(page.getByRole('button', { name: '删除' })).toBeEnabled()
  expect(errors).toEqual([])
})

test('Round 4：模型历史可读，训练台停用，停用前训练仍可取消由 API 契约覆盖', async ({ page, request }) => {
  const session: Session = await newIsolatedSession(request, 'round4-model-history')
  const model = await seedModel(request, session.workspaceId)
  await disable(request, session.workspaceId, 'model')
  const errors = await openSession(page, session)

  await page.goto('/#/modellib')
  await expect(page.locator('.ro-banner')).toContainText(REASON)
  const row = page.locator('.fx-table tbody tr').filter({ hasText: model.name })
  await expect(row).toBeVisible()
  await row.click()
  await expect(page).toHaveURL(new RegExp(`#/modellib/${model.id}`))
  await expect(page.getByLabel('状态')).toBeDisabled()
  await expect(page.getByRole('button', { name: '检查健康度' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '冻结' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '交给 AI 改进' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '召集团队评审' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '删除' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Python 代码' })).toBeEnabled()
  await expect(page.locator('.fr-full')).toBeVisible()
  await expect(page.getByText('泄漏与质量检查')).toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round4-1366x768-model-history.png') })

  await page.getByRole('button', { name: '训练台' }).click()
  await expect(page.locator('.ro-gate')).toBeVisible()
  await expect(page.getByRole('button', { name: '开始训练' })).toHaveCount(0)

  await enable(request, session.workspaceId, 'model')
  await page.reload()
  await page.goto('/#/modellib/train')
  await expect(page.getByRole('button', { name: '开始训练' })).toBeEnabled()
  expect(errors).toEqual([])
})

test('Round 4：组合历史可读，设计与风险体检停用', async ({ page, request }) => {
  const session: Session = await newIsolatedSession(request, 'round4-portfolio-history')
  const scheme = await seedPortfolio(request, session.workspaceId)
  await disable(request, session.workspaceId, 'portfolio')
  const errors = await openSession(page, session)

  await page.goto('/#/portfolio')
  await expect(page.locator('.ro-banner')).toContainText(REASON)
  const row = page.locator('.fx-table tbody tr').filter({ hasText: scheme.name })
  await expect(row).toBeVisible()
  await row.click()
  await expect(page).toHaveURL(new RegExp(`#/portfolio/${scheme.id}`))
  await expect(page.getByLabel('状态')).toBeDisabled()
  await expect(page.getByRole('button', { name: '交给 AI 改进' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '召集团队评审' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '删除' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Python 代码' })).toBeEnabled()
  await expect(page.locator('.chart2 svg[role="img"]').first()).toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round4-1366x768-portfolio-history.png') })

  await page.getByRole('button', { name: '方案设计' }).click()
  await expect(page.locator('.ro-gate')).toBeVisible()
  await expect(page.getByRole('button', { name: '◫ 评估' })).toHaveCount(0)
  await page.getByRole('button', { name: '风险体检' }).click()
  await expect(page.locator('.ro-gate')).toBeVisible()
  await expect(page.getByRole('button', { name: '体检', exact: true })).toHaveCount(0)

  await enable(request, session.workspaceId, 'portfolio')
  await page.reload()
  await page.goto('/#/portfolio/design')
  await expect(page.getByRole('button', { name: '◫ 评估' })).toBeEnabled()
  expect(errors).toEqual([])
})

test('Round 4：执行历史/成本/录制可读，新仿真与录制停用，停止保持可用', async ({ page, request }) => {
  const session: Session = await newIsolatedSession(request, 'round4-execution-history')
  const scheme = await seedExecution(request, session.workspaceId)
  await disable(request, session.workspaceId, 'execution')
  const errors = await openSession(page, session)

  await page.goto('/#/execution')
  await expect(page.locator('.ro-banner')).toContainText(REASON)
  const row = page.locator('.fx-table tbody tr').filter({ hasText: scheme.name })
  await expect(row).toBeVisible()
  await row.click()
  await expect(page).toHaveURL(new RegExp(`#/execution/${scheme.id}`))
  await expect(page.getByLabel('状态')).toBeDisabled()
  await expect(page.getByRole('button', { name: '交给 AI 改进' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '召集团队评审' })).toBeDisabled()
  await expect(page.getByRole('button', { name: '删除' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Python 代码' })).toBeEnabled()
  await expect(page.getByRole('img', { name: /成本-风险前沿|母单规模与成本/ }).first()).toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round4-1366x768-execution-history.png') })

  await page.getByRole('button', { name: '仿真设计' }).click()
  await expect(page.locator('.ro-gate')).toBeVisible()
  await expect(page.getByRole('button', { name: '⇶ 仿真' })).toHaveCount(0)
  for (const [tab, gone] of [['回放监控', '回放'], ['市场雷达', '刷新']] as const) {
    await page.getByRole('button', { name: tab }).click()
    await expect(page.locator('.ro-gate')).toBeVisible()
    await expect(page.getByRole('button', { name: gone, exact: true })).toHaveCount(0)
  }

  await page.getByRole('button', { name: '成本与容量' }).click()
  await expect(page.locator('.ro-banner')).toContainText(REASON)
  await expect(page.getByRole('heading', { name: /成本模型/ }).first()).toBeVisible()
  await expect(page.getByText('使用中').first()).toBeVisible()
  await expect(page.getByRole('button', { name: '估计容量' })).toBeDisabled()

  await page.getByRole('button', { name: '盘口录制' }).click()
  await expect(page.getByRole('button', { name: '● 开始录制' })).toBeDisabled()
  await expect(page.locator('.ro-banner')).toContainText(REASON)

  await enable(request, session.workspaceId, 'execution')
  await page.reload()
  await page.goto('/#/execution/design')
  await expect(page.getByRole('button', { name: '⇶ 仿真' })).toBeEnabled()
  expect(errors).toEqual([])
})

test('Round 4：切换工作区时策略读取未完成，不沿用旧启用态；事件恢复无需刷新', async ({ page, request }) => {
  const a: Session = await newIsolatedSession(request, 'round4-stale-workspace-a')
  const created = await api(request, '/api/workspaces', {
    workspaceId: a.workspaceId,
    method: 'POST',
    data: { name: `round4-stale-workspace-b-${Date.now()}` },
  })
  expect(created.status, JSON.stringify(created.body)).toBe(200)
  const b = created.body.id as string
  await disable(request, b, 'factor')

  const errors = await openSession(page, a)
  await page.goto('/#/factors/new')
  await expect(page.getByRole('textbox', { name: '因子表达式' })).toBeVisible()
  await page.evaluate(() => { (window as unknown as { __r4SwitchAlive?: string }).__r4SwitchAlive = 'alive' })
  const writes = trackFactorWrites(page)

  let release!: () => void
  const held = new Promise<void>((resolve) => { release = resolve })
  let heldCount = 0
  await page.route('**/api/auth/research-plugins', async (route) => {
    if ((route.request().headers()['x-workspace-id'] || '') === b) {
      heldCount += 1
      await held
    }
    await route.continue()
  })

  const meLoaded = page.waitForResponse((response) =>
    response.url().includes('/api/auth/me') && (response.request().headers()['x-workspace-id'] || '') === b)
  await page.evaluate((workspaceId) => {
    localStorage.setItem('qa.workspace', workspaceId)
    window.dispatchEvent(new Event('qa:workspace-invalid'))
  }, b)
  await meLoaded
  await expect.poll(() => heldCount).toBeGreaterThan(0)

  // B's policy is still in flight: the old A-enabled evaluator must be gone, not writable.
  await expect(page.locator('.ro-gate')).toBeVisible()
  await expect(page.getByRole('textbox', { name: '因子表达式' })).toHaveCount(0)
  await expect(page.locator('.ro-banner')).toContainText('正在读取研究能力设置')
  expect(writes).toEqual([])

  release()
  await expect(page.locator('.ro-gate')).toContainText(REASON)
  await expect(page.locator('.ro-banner')).toContainText('已停用，仅查看历史')
  await expect(page.getByRole('textbox', { name: '因子表达式' })).toHaveCount(0)
  expect(writes).toEqual([])

  // Re-enabling through the existing event path restores controls in-place.
  await enable(request, b, 'factor')
  await page.evaluate(() => window.dispatchEvent(new Event('qa:plugins-changed')))
  await expect(page.getByRole('textbox', { name: '因子表达式' })).toBeVisible()
  expect(await page.evaluate(() => (window as unknown as { __r4SwitchAlive?: string }).__r4SwitchAlive)).toBe('alive')
  expect(writes).toEqual([])
  expect(errors).toEqual([])
})

test('Round 4：策略读取失败保持只读，历史仍可读，重试后恢复', async ({ page, request }) => {
  const session: Session = await newIsolatedSession(request, 'round4-policy-error')
  // 在 App 启动前拦住策略读取，才能真实覆盖“加载即失败”的状态；
  // 应用启动后再装路由只会影响后续 refetch，而 hash 导航不会触发 refetch。
  await page.route('**/api/auth/research-plugins', async (route) => { await route.abort() })
  const errors = await openSession(page, session)
  const writes = trackFactorWrites(page)
  await page.goto('/#/factors/new')

  await expect(page.locator('.ro-banner')).toContainText('研究能力设置读取失败')
  await expect(page.locator('.ro-gate')).toContainText('研究能力设置暂不可用')
  await expect(page.getByRole('textbox', { name: '因子表达式' })).toHaveCount(0)
  expect(writes).toEqual([])

  // History remains readable instead of a permanent blank page.
  await page.locator('.ro-gate').getByRole('button', { name: '因子库' }).click()
  await expect(page.locator('.fx-tabs')).toBeVisible()
  await expect(page.locator('.ro-banner')).toContainText('读取失败')

  await page.unroute('**/api/auth/research-plugins')
  await page.locator('.ro-banner').getByRole('button', { name: '重试' }).click()
  await expect(page.locator('.ro-banner')).toHaveCount(0)
  await page.getByRole('button', { name: '评估器' }).click()
  await expect(page.getByRole('textbox', { name: '因子表达式' })).toBeVisible()
  expect(writes).toEqual([])
  expect(errors).toEqual([])
})
