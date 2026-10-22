import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import path from 'node:path'
import { api, registerOrLogin, uiLogin, waitRun } from './helpers'
import { ARTIFACTS } from './run-env'

const VIEW = { laptop: { width: 1366, height: 768 }, large: { width: 1380, height: 920 } }

async function seedWorkflow(request: APIRequestContext, workspaceId: string) {
  const upload = await request.post('/api/workflows/files?name=round1-layout.csv', {
    headers: { 'X-Workspace-Id': workspaceId, 'Content-Type': 'application/octet-stream' },
    data: 'year,value\n2021,1\n2022,2\n2023,3\n2024,4\n',
  })
  expect(upload.ok()).toBeTruthy()
  const file = await upload.json()
  const catalog = await api(request, '/api/workflows/catalog', { workspaceId })
  const flow = catalog.body.templates.find((f: { title: string }) => f.title === 'CSV数据研究')
  flow.title = 'Round 1 几何验收'
  flow.nodes[0].params.file = file.name
  const chart = catalog.body.cards.find((d: { kind: string }) => d.kind === 'research.chart')
  flow.nodes.push({ id: 'chart-r1', kind: chart.kind, version: chart.version, title: '年度趋势图', params: { chart_type: 'line', x: 'year', y: 'value' }, position: { x: 350, y: 360 } })
  flow.edges.push({ id: 'chart-r1-edge', source: 'source', source_port: 'data', target: 'chart-r1', target_port: 'data' })
  const saved = await api(request, '/api/workflows', { workspaceId, method: 'POST', data: { workflow: flow } })
  expect(saved.status).toBe(200)
  const started = await api(request, `/api/workflows/${saved.body.id}/run`, { workspaceId, method: 'POST', data: {} })
  expect(started.status).toBe(200)
  const run = await waitRun(request, workspaceId, started.body.id)
  expect(run.state, run.error).toBe('COMPLETED')
  const isolated = await api(request, '/api/workflows', {
    workspaceId,
    method: 'POST',
    data: { workflow: { ...flow, title: 'Round 1 隔离研究' } },
  })
  expect(isolated.status).toBe(200)
  return { wid: saved.body.id as string, runId: started.body.id as string, isolatedWid: isolated.body.id as string }
}

async function openResult(page: Page, wid: string) {
  await page.goto(`/#/workflows/${wid}`)
  const latest = page.getByRole('button', { name: /最近一次结果/ })
  await expect(latest).toBeVisible()
  const before = await page.evaluate(() => window.scrollY)
  await latest.click()
  await expect(page.locator('.rs-dock')).toBeVisible()
  await expect(page.locator('.wf-page')).toHaveAttribute('data-result-layout', 'compact')
  await page.waitForTimeout(450)
  return before
}

async function geometry(page: Page) {
  return page.evaluate(() => {
    const box = (selector: string) => {
      const el = document.querySelector(selector)
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, bottom: r.bottom, left: r.left, right: r.right }
    }
    return {
      innerWidth,
      innerHeight,
      scrollY,
      scrollWidth: document.documentElement.scrollWidth,
      canvas: box('.wf-canvas'),
      dock: box('.rs-dock'),
      dockBody: box('.rs-dock-body'),
      dtb: box('.dtb'),
      ask: box('.ask-panel'),
      askHeader: box('.ask-head'),
      workspace: box('.wf-workspace'),
    }
  })
}

async function nodeVisibility(page: Page) {
  return page.evaluate(() => {
    const canvas = document.querySelector('.wf-canvas')?.getBoundingClientRect()
    if (!canvas) return null
    return Array.from(document.querySelectorAll('.react-flow__node')).map((n) => {
      const r = n.getBoundingClientRect()
      return {
        top: r.top,
        bottom: r.bottom,
        insideVertical: r.top >= canvas.top - 2 && r.bottom <= canvas.bottom + 2,
      }
    })
  })
}

/**
 * 真实交集：元素必须同时与 Dock 滚动容器和浏览器视口相交。
 * Playwright 的 toBeVisible 不检查滚动容器裁切，不能用于“图表真的看得到”。
 */
async function visibleIntersection(page: Page, selector: string) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel)
    const container = document.querySelector('.rs-dock-body')
    if (!el || !container) return { width: 0, height: 0, top: 0, bottom: 0 }
    const r = el.getBoundingClientRect()
    const c = container.getBoundingClientRect()
    const left = Math.max(r.left, c.left, 0)
    const right = Math.min(r.right, c.right, innerWidth)
    const top = Math.max(r.top, c.top, 0)
    const bottom = Math.min(r.bottom, c.bottom, innerHeight)
    return { width: Math.max(0, right - left), height: Math.max(0, bottom - top), top, bottom }
  }, selector)
}

async function tableRowsVisibility(page: Page, count = 2) {
  return page.evaluate((n) => {
    const container = document.querySelector('.rs-dock-body')
    if (!container) return []
    const c = container.getBoundingClientRect()
    return Array.from(document.querySelectorAll('.rs-dock .rs-table tbody tr')).slice(0, n).map((row) => {
      const r = row.getBoundingClientRect()
      const top = Math.max(r.top, c.top, 0)
      const bottom = Math.min(r.bottom, c.bottom, innerHeight)
      const left = Math.max(r.left, c.left, 0)
      const right = Math.min(r.right, c.right, innerWidth)
      return { width: Math.max(0, right - left), height: Math.max(0, bottom - top) }
    })
  }, count)
}

/** 图像必须完整落在 Dock 滚动容器和视口内（2px 容差），而不只是部分交集。 */
async function imageFullyContained(page: Page, selector: string) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel)
    const container = document.querySelector('.rs-dock-body')
    if (!el || !container) return { inside: false, width: 0, height: 0 }
    const r = el.getBoundingClientRect()
    const c = container.getBoundingClientRect()
    const inside = r.top >= c.top - 2 && r.bottom <= c.bottom + 2
      && r.left >= c.left - 2 && r.right <= c.right + 2
      && r.top >= -2 && r.bottom <= innerHeight + 2
      && r.left >= -2 && r.right <= innerWidth + 2
    return { inside, width: r.width, height: r.height, top: r.top, bottom: r.bottom }
  }, selector)
}

async function settleArtifact(page: Page) {
  await expect(page.locator('.rs-dock .rs-artifact').first()).toHaveAttribute('aria-busy', 'false')
  const image = page.locator('.rs-dock .rs-artifact .rs-image img').first()
  if (await image.count()) {
    await expect(image).toBeVisible()
    await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true)
  }
  await page.waitForTimeout(250)
}

test('Round 1：笔记本结果分栏、桌面标题栏与问 AI 几何', async ({ page, request }) => {
  const session = await registerOrLogin(request)
  const { wid, isolatedWid } = await seedWorkflow(request, session.workspaceId)
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(e.message))
  const resultKey = `qa.wf.result.${session.workspaceId}.${wid}`

  await page.addInitScript(() => {
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

  await uiLogin(page, session)
  await page.setViewportSize(VIEW.laptop)
  await page.goto(`/#/workflows/${wid}`)
  await expect(page.locator('.dtb')).toBeVisible()
  await expect(page.locator('html')).toHaveAttribute('data-desktop-frame', 'tauri')

  const before = await openResult(page, wid)
  await expect(page.locator('.rs-dock')).toBeVisible()
  const compact = await geometry(page)
  expect(compact.scrollY).toBe(before)
  expect(compact.canvas?.height).toBeGreaterThanOrEqual(200)
  expect(compact.dock?.bottom).toBeLessThanOrEqual(compact.innerHeight + 1)
  expect(compact.scrollWidth).toBeLessThanOrEqual(compact.innerWidth + 1)
  const nodes = await nodeVisibility(page)
  expect(nodes?.length).toBeGreaterThan(0)
  for (const node of nodes ?? []) expect(node.insideVertical).toBe(true)
  await expect(page.locator('.react-flow__minimap')).toBeHidden()

  // 视口变矮时按实际可用高度收敛，不能只信初始 innerHeight。
  await page.setViewportSize({ width: 1366, height: 720 })
  await page.waitForTimeout(400)
  const shorter = await geometry(page)
  expect(shorter.dock!.bottom).toBeLessThanOrEqual(shorter.innerHeight + 1)
  await page.setViewportSize(VIEW.laptop)
  await page.waitForTimeout(400)
  const restoredViewport = await geometry(page)
  expect(restoredViewport.dock!.bottom).toBeLessThanOrEqual(restoredViewport.innerHeight + 1)

  await page.getByRole('combobox', { name: '查看节点结果' }).selectOption('chart-r1')
  await settleArtifact(page)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-1366x768-light.png') })

  // 真实表格：列头 + 至少两行矩形必须与 Dock 滚动容器和视口相交。
  await page.getByRole('combobox', { name: '查看节点结果' }).selectOption('source')
  await expect(page.locator('.rs-dock .rs-table thead th').first()).toBeVisible()
  await expect(page.locator('.rs-dock .rs-table tbody tr').nth(1)).toBeAttached()
  const tableRows = await tableRowsVisibility(page, 3)
  expect(tableRows.length).toBeGreaterThanOrEqual(2)
  for (const row of tableRows.slice(0, 2)) {
    expect(row.width).toBeGreaterThan(0)
    expect(row.height).toBeGreaterThan(0)
  }
  await expect(page.locator('.rs-artifact-compact .rs-actions a').first()).toBeVisible()
  await expect(page.getByRole('button', { name: '详情' })).toBeVisible()
  await settleArtifact(page)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-1366x768-table.png') })

  // compact 的「详情」是既有筛选/分页路径；展开、操作、收起都要验证。
  await page.getByRole('button', { name: '详情' }).click()
  await expect(page.locator('.rs-dock').getByLabel('筛选当前页数据')).toBeVisible()
  await page.locator('.rs-dock').getByLabel('筛选当前页数据').fill('2023')
  await expect(page.locator('.rs-dock .rs-table tbody tr')).toHaveCount(1)
  await page.locator('.rs-dock').getByLabel('筛选当前页数据').fill('')
  await expect(page.getByRole('button', { name: '收起详情' })).toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-1366x768-table-details.png') })
  await page.getByRole('button', { name: '收起详情' }).click()
  await expect(page.locator('.rs-dock').getByLabel('筛选当前页数据')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '详情' })).toBeVisible()

  // 真实图表：紧凑模式必须先看到真实图像像素，而不是被元数据挤出滚动容器。
  await page.getByRole('combobox', { name: '查看节点结果' }).selectOption('chart-r1')
  await settleArtifact(page)
  const compactImage = page.locator('.rs-dock .rs-artifact .rs-image img').first()
  const compactPixels = await compactImage.evaluate((img: HTMLImageElement) => ({ w: img.naturalWidth, h: img.naturalHeight }))
  expect(compactPixels.w).toBeGreaterThanOrEqual(200)
  expect(compactPixels.h).toBeGreaterThanOrEqual(80)
  const compactVisible = await visibleIntersection(page, '.rs-dock .rs-artifact .rs-image img')
  expect(compactVisible.width).toBeGreaterThanOrEqual(200)
  expect(compactVisible.height).toBeGreaterThanOrEqual(80)
  const compactContained = await imageFullyContained(page, '.rs-dock .rs-artifact .rs-image img')
  expect(compactContained.inside).toBe(true)
  await expect(page.locator('.rs-artifact-compact .rs-actions a').first()).toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-1366x768-chart.png') })

  // Tauri 标题栏下，问 AI 的头部和关闭按钮不能被压住；结果区给面板让位。
  await page.locator('.ask-fab-group').hover()
  await page.locator('.ask-fab-mini').click()
  await expect(page.locator('.ask-panel')).toBeVisible()
  await expect(page.locator('html')).toHaveAttribute('data-askai-open', 'true')
  await settleArtifact(page)
  const ask = await geometry(page)
  expect(ask.ask!.top).toBeGreaterThanOrEqual(ask.dtb!.bottom - 1)
  expect(ask.dock!.right).toBeLessThanOrEqual(ask.ask!.left - 8)
  expect(ask.dock!.width).toBeGreaterThan(360)
  await expect(page.getByRole('button', { name: '收起运行结果' })).toBeVisible()
  await expect(page.locator('.rs-dock .rs-image img')).toBeVisible()
  const askShot = await visibleIntersection(page, '.rs-dock .rs-artifact .rs-image img')
  expect(askShot.height).toBeGreaterThan(0)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-1366x768-askai.png') })
  const closeHit = await page.evaluate(() => {
    const button = document.querySelector('.ask-panel .ask-icon')
    if (!button) return false
    const r = button.getBoundingClientRect()
    const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)
    return !!hit && button.contains(hit)
  })
  expect(closeHit).toBe(true)
  await page.keyboard.press('Escape')
  await expect(page.locator('.ask-panel')).toBeHidden()
  await expect(page.locator('.ask-fab-mini')).toBeFocused()

  // 专注阅读：画布让位，结果正文是受限 flex 子项，内部滚动；图像应明显更大。
  await page.getByRole('button', { name: /最近一次结果/ }).click()
  await expect(page.locator('.rs-dock')).toBeVisible()
  await page.getByRole('combobox', { name: '查看节点结果' }).selectOption('chart-r1')
  await page.getByRole('button', { name: '专注结果' }).click()
  await expect(page.locator('.wf-workspace')).toBeHidden()
  await expect(page.getByRole('button', { name: '返回画布' })).toBeVisible()
  await settleArtifact(page)
  const focus = await geometry(page)
  expect(focus.dockBody!.bottom).toBeLessThanOrEqual(focus.dock!.bottom + 1)
  expect(await page.locator('.rs-dock-body').evaluate((el) => getComputedStyle(el).overflowY)).toBe('auto')
  const focusVisible = await visibleIntersection(page, '.rs-dock .rs-artifact .rs-image img')
  expect(focusVisible.width).toBeGreaterThanOrEqual(200)
  expect(focusVisible.height).toBeGreaterThanOrEqual(200)
  const focusContained = await imageFullyContained(page, '.rs-dock .rs-artifact .rs-image img')
  expect(focusContained.inside).toBe(true)
  await expect(page.locator('.rs-artifact .rs-actions a').first()).toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-1366x768-chart-focus.png') })
  await page.getByRole('button', { name: '返回画布' }).click()
  await expect(page.locator('.wf-workspace')).toBeVisible()
  await page.waitForTimeout(350)

  // 关闭后焦点回到触发按钮，画布恢复。
  await page.getByRole('button', { name: '收起运行结果' }).click()
  await expect(page.locator('.rs-dock')).toBeHidden()
  await expect(page.getByRole('button', { name: /最近一次结果/ })).toBeFocused()

  // 真实 reload 恢复 open/height（compact 默认 196，ArrowUp 后受视口上限约束）。
  await page.getByRole('button', { name: /最近一次结果/ }).click()
  await expect(page.locator('.rs-dock')).toBeVisible()
  await page.locator('.rs-dock [role=separator]').focus()
  await page.keyboard.press('ArrowUp')
  await page.waitForTimeout(500)
  await page.reload()
  await expect(page.locator('.rs-dock')).toBeVisible()
  await expect(page.locator('.wf-page')).toHaveAttribute('data-result-layout', 'compact')
  const restored = await page.locator('.rs-dock-body').boundingBox()
  expect(restored?.height ?? 0).toBeGreaterThanOrEqual(185)

  // 损坏 JSON：不白屏、不自动打开；随后仍能正常打开。
  await page.evaluate((key) => localStorage.setItem(key, '{not-json'), resultKey)
  await page.reload()
  await expect(page.locator('.rs-dock')).toHaveCount(0)
  await page.getByRole('button', { name: /最近一次结果/ }).click()
  await expect(page.locator('.rs-dock')).toBeVisible()

  // 超大高度：读取时按视口上限收敛，不把结果推到视口外。
  await page.evaluate((key) => localStorage.setItem(key, JSON.stringify({ open: true, height: 9999, expanded: false })), resultKey)
  await page.reload()
  await expect(page.locator('.rs-dock')).toBeVisible()
  const oversized = await geometry(page)
  expect(oversized.dockBody!.height).toBeLessThanOrEqual(240)
  expect(oversized.dock!.bottom).toBeLessThanOrEqual(oversized.innerHeight + 1)

  // 工作流/工作区隔离：另一条研究不应继承第一条的 open/height。
  await page.evaluate((workflowId) => {
    localStorage.setItem(`qa.wf.result.foreign-workspace.${workflowId}`, JSON.stringify({ open: false, height: 160, expanded: false }))
  }, wid)
  await page.reload()
  await expect(page.locator('.rs-dock')).toBeVisible()
  await page.goto(`/#/workflows/${isolatedWid}`)
  await expect(page.locator('.dtb')).toBeVisible()
  await expect(page.locator('.rs-dock')).toHaveCount(0)
  await page.goto(`/#/workflows/${wid}`)
  await expect(page.locator('.rs-dock')).toBeVisible()

  // 更宽的笔记本窗口：画布与结果仍同屏。
  await page.setViewportSize(VIEW.large)
  await page.goto(`/#/workflows/${wid}`)
  await page.getByRole('button', { name: /最近一次结果/ }).click()
  await expect(page.locator('.rs-dock')).toBeVisible()
  const large = await geometry(page)
  expect(large.canvas?.height).toBeGreaterThanOrEqual(300)
  expect(large.dock?.bottom).toBeLessThanOrEqual(large.innerHeight + 1)

  // 深色必须由应用主题设置生效，而不是只靠 emulateMedia；截图前等图表 ready。
  await page.emulateMedia({ colorScheme: 'light' })
  await page.evaluate(() => localStorage.setItem('qa.theme', 'dark'))
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.locator('.rs-dock')).toBeVisible()
  await page.getByRole('combobox', { name: '查看节点结果' }).selectOption('chart-r1')
  await settleArtifact(page)
  await page.waitForTimeout(200)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-1380x920-dark.png') })

  await page.evaluate(() => localStorage.setItem('qa.theme', 'light'))
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await page.getByRole('combobox', { name: '查看节点结果' }).selectOption('chart-r1')
  await settleArtifact(page)
  await page.waitForTimeout(200)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-1380x920-light.png') })

  // 390px：不横向溢出，结果标题/关闭仍可达；等图表 ready。
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`/#/workflows/${wid}`)
  await page.getByRole('button', { name: /最近一次结果/ }).click()
  await expect(page.locator('.rs-dock')).toBeVisible()
  await page.getByRole('combobox', { name: '查看节点结果' }).selectOption('chart-r1')
  await settleArtifact(page)
  await page.locator('.rs-dock').scrollIntoViewIfNeeded()
  const mobile = await geometry(page)
  expect(mobile.scrollWidth).toBeLessThanOrEqual(mobile.innerWidth + 1)
  await expect(page.getByRole('button', { name: '收起运行结果' })).toBeVisible()
  await page.screenshot({ path: path.join(ARTIFACTS, 'round1-390x844-light.png') })

  expect(errors).toEqual([])
})
