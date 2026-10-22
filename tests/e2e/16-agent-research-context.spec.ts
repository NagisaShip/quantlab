import { expect, test, type APIRequestContext, type Page } from '@playwright/test'
import { writeFileSync } from 'node:fs'
import path from 'node:path'
import { api, newIsolatedSession, uiLogin, waitRun, type Session } from './helpers'
import { ARTIFACTS } from './run-env'

const VIEW = { width: 1366, height: 768 }

type Seeded = {
  wid: string
  runId: string
  title: string
  chartHash: string
  artifactView: any
}

async function seedChartResearch(request: APIRequestContext, workspaceId: string, label: string): Promise<Seeded> {
  const fileName = `round3-${label.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '') || 'study'}-${Date.now()}.csv`
  const upload = await request.post(`/api/workflows/files?name=${encodeURIComponent(fileName)}`, {
    headers: { 'X-Workspace-Id': workspaceId, 'Content-Type': 'application/octet-stream' },
    data: 'year,value\n2021,1\n2022,2\n2023,3\n2024,4\n',
  })
  expect(upload.ok(), `上传 CSV 失败 ${upload.status()}`).toBeTruthy()
  const file = await upload.json()
  const catalog = await api(request, '/api/workflows/catalog', { workspaceId })
  expect(catalog.status).toBe(200)
  const flow = catalog.body.templates.find((item: { title: string }) => item.title === 'CSV数据研究')
  flow.title = label
  flow.nodes[0].params.file = file.name
  const chart = catalog.body.cards.find((item: { kind: string }) => item.kind === 'research.chart')
  flow.nodes.push({
    id: 'chart', kind: chart.kind, version: chart.version, title: `${label}图表`,
    params: { chart_type: 'line', x: 'year', y: 'value' }, position: { x: 350, y: 360 },
  })
  flow.edges.push({ id: 'chart-edge', source: 'source', source_port: 'data', target: 'chart', target_port: 'data' })
  const saved = await api(request, '/api/workflows', { workspaceId, method: 'POST', data: { workflow: flow } })
  expect(saved.status, JSON.stringify(saved.body)).toBe(200)
  const wid = saved.body.id as string
  const started = await api(request, `/api/workflows/${wid}/run`, { workspaceId, method: 'POST', data: {} })
  expect(started.status, JSON.stringify(started.body)).toBe(200)
  const run = await waitRun(request, workspaceId, started.body.id)
  expect(run.state, run.error).toBe('COMPLETED')
  const detail = await api(request, `/api/workflows/runs/${started.body.id}`, { workspaceId })
  expect(detail.status, JSON.stringify(detail.body)).toBe(200)
  const ref = detail.body.report.nodes.chart.outputs.chart
  expect(ref?.hash, '真实图表输出缺少工件引用').toBeTruthy()
  const artifact = await api(request, `/api/workflows/artifacts/${ref.hash}?limit=20`, { workspaceId })
  expect(artifact.status, JSON.stringify(artifact.body)).toBe(200)
  expect(artifact.body.files?.some((item: any) => item.image && item.url), '真实图表工件缺少可加载图片').toBeTruthy()
  return {
    wid,
    runId: started.body.id as string,
    title: label,
    chartHash: ref.hash,
    artifactView: { ...artifact.body, workspace_id: workspaceId },
  }
}

function sse(events: any[]): string {
  return events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('') + 'event: done\ndata: {}\n\n'
}

function terminal(content: string) {
  return {
    type: 'final', mode: 'llm', content, model: 'mock/fake-1', thinking: '', code: '',
    checks: {}, notes: [], elapsed_ms: 9,
  }
}

async function openAskAI(page: Page) {
  if (await page.locator('.ask-panel').isVisible()) return
  // The main .ask-fab intentionally opens the full AI Research Lab. The side panel
  // opener is the mini control, revealed by hovering the fab group on desktop.
  await page.locator('.ask-fab-group').hover()
  await page.locator('.ask-fab-mini').click()
  await expect(page.locator('.ask-panel')).toBeVisible()
}

async function gotoResearch(page: Page, seeded: Seeded) {
  await page.goto(`/#/workflows/${seeded.wid}?run_id=${seeded.runId}&node=chart`)
  // On short viewports with the result dock open, Round 1 intentionally hides the
  // title input. Wait on the visible workflow selector + result header instead.
  await expect(page.getByRole('combobox', { name: '打开已保存工作流' })).toHaveValue(seeded.wid)
  await expect(page.locator('.rs-dock')).toBeVisible()
  await expect(page.locator('.rs-dock-title small')).toContainText(seeded.title)
}

test('Round 3：A/B 会话、草稿与输出引用隔离，当前/历史/未保存标签', async ({ page, request }) => {
  const session: Session = await newIsolatedSession(request, 'round3-isolation')
  const a = await seedChartResearch(request, session.workspaceId, 'Round 3 A 研究')
  const b = await seedChartResearch(request, session.workspaceId, 'Round 3 B 研究')
  const captured: any[] = []

  // Startup mock installed before app boot. Only the model stream is deterministic;
  // workflow, runs and artifacts are the real backend.
  await page.route('**/api/ai/ask/stream', async (route) => {
    const body = route.request().postDataJSON()
    captured.push(body)
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: sse([{ type: 'delta', kind: 'text', text: 'MOCK-ANSWER ' + body.question }, terminal('MOCK-ANSWER ' + body.question)]),
    })
  })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await uiLogin(page, session)
  await page.setViewportSize(VIEW)
  await gotoResearch(page, a)
  await openAskAI(page)

  const strip = page.locator('.ask-research')
  await expect(strip).toBeVisible()
  await expect(strip.locator('.ask-research-title')).toHaveText(a.title)
  await expect(strip.locator('.ask-chip.ask-run')).toContainText('冻结历史运行')
  await expect(strip.locator('.ask-chip.ask-run')).toContainText('已完成')
  await expect(strip).toContainText('节点')
  await expect(strip).toContainText(`${a.title}图表`)
  await expect(strip.locator('.ask-ref')).toHaveCount(1)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round3-1366x768-askai-context.png') })

  // Remove the only selected output; the next request must carry zero refs but keep identity.
  await strip.locator('.ask-ref').first().click()
  await expect(strip.locator('.ask-ref')).toHaveCount(0)
  await expect(strip).toContainText('无输出证据')
  await page.locator('.ask-panel textarea').fill('A 问题：这个输出可靠吗？')
  await page.locator('.ask-panel .cmp-send').click()
  await expect(page.locator('.ask-feed')).toContainText('MOCK-ANSWER A 问题')
  expect(captured.at(-1)?.research?.workflow?.id).toBe(a.wid)
  expect(captured.at(-1)?.research?.outputs).toEqual([])
  expect(captured.at(-1)?.research?.selected_node?.id).toBe('chart')
  expect(captured.at(-1)?.research?.run?.view_mode).toBe('frozen')

  // Unsaved title changes are visible as a warning label, never as a new run result.
  // The result dock intentionally hides the title row on short viewports, so close it
  // before editing the real editor value.
  await page.getByRole('button', { name: '收起运行结果' }).click()
  await expect(page.locator('.rs-dock')).toHaveCount(0)
  await page.getByRole('textbox', { name: '研究名称' }).fill(`${a.title}（未保存）`)
  await expect(strip.locator('.ask-chip.warn')).toContainText('有未保存修改')
  await expect(strip.locator('.ask-chip.ask-run')).toContainText('冻结历史运行')
  await page.locator('.ask-panel textarea').fill('A 草稿')

  // B is a different workflow: its own context and output refs, no A history/draft.
  await gotoResearch(page, b)
  await expect(strip.locator('.ask-research-title')).toHaveText(b.title)
  await expect(page.locator('.ask-feed')).not.toContainText('A 问题')
  await expect(page.locator('.ask-feed')).not.toContainText('MOCK-ANSWER A 问题')
  await expect(page.locator('.ask-panel textarea')).toHaveValue('')
  await expect(strip.locator('.ask-ref')).toHaveCount(1)
  await page.locator('.ask-panel textarea').fill('B 问题：B 的真实输出是什么？')
  await page.locator('.ask-panel .cmp-send').click()
  await expect(page.locator('.ask-feed')).toContainText('MOCK-ANSWER B 问题')
  expect(captured.at(-1)?.research?.workflow?.id).toBe(b.wid)
  expect(captured.at(-1)?.research?.outputs?.length).toBe(1)
  expect(captured.at(-1)?.research?.run?.view_mode).toBe('frozen')
  await page.locator('.ask-panel textarea').fill('B 草稿')

  // Returning to A restores its in-memory messages and draft; removed refs stay removed.
  await gotoResearch(page, a)
  await expect(page.locator('.ask-feed')).toContainText('A 问题')
  await expect(page.locator('.ask-feed')).toContainText('MOCK-ANSWER A 问题')
  await expect(page.locator('.ask-feed')).not.toContainText('B 问题')
  await expect(page.locator('.ask-panel textarea')).toHaveValue('A 草稿')
  await expect(strip.locator('.ask-ref')).toHaveCount(0)

  // Loading the latest result flips the run label from frozen historical to current.
  // The open side panel overlays the page's top-right; close it like a user first,
  // click the real result action, then reopen the sidebar with the mini control.
  await page.keyboard.press('Escape')
  await expect(page.locator('.ask-panel')).toBeHidden()
  await page.locator('.rs-result-link').click()
  await expect(page.locator('.rs-dock')).toBeVisible()
  await openAskAI(page)
  await expect(strip.locator('.ask-chip.ask-run.current')).toContainText('当前运行')

  // A real reload is a new in-memory session: no A/B messages leak into it, while the
  // scoped draft and the current research identity still load from the browser.
  await page.waitForTimeout(400)
  await page.reload()
  await expect(page.getByRole('combobox', { name: '打开已保存工作流' })).toHaveValue(a.wid)
  await expect(page.locator('.rs-dock-title small')).toContainText(a.title)
  await openAskAI(page)
  await expect(page.locator('.ask-feed')).not.toContainText('A 问题')
  await expect(page.locator('.ask-feed')).not.toContainText('B 问题')
  await expect(page.locator('.ask-panel textarea')).toHaveValue('A 草稿')
  await expect(page.locator('.ask-research-title')).toHaveText(a.title)
  expect(errors).toEqual([])
})

test('Round 3：切换工作流时，进行中的 A 请求不会写入 B', async ({ page, request }) => {
  const session: Session = await newIsolatedSession(request, 'round3-race')
  const a = await seedChartResearch(request, session.workspaceId, 'Round 3 竞态 A')
  const b = await seedChartResearch(request, session.workspaceId, 'Round 3 竞态 B')
  let askCount = 0
  let releaseA: (() => void) | undefined
  const gate = new Promise<void>((resolve) => { releaseA = resolve })
  await page.route('**/api/ai/ask/stream', async (route) => {
    const body = route.request().postDataJSON()
    askCount += 1
    await gate
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: sse([terminal('SLOW-A-ANSWER ' + body.question)]),
    })
  })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await uiLogin(page, session)
  await page.setViewportSize(VIEW)
  await gotoResearch(page, a)
  await openAskAI(page)
  await page.locator('.ask-panel textarea').fill('慢请求 A')
  await page.locator('.ask-panel .cmp-send').click()
  await expect.poll(() => askCount).toBe(1)
  await expect(page.locator('.ask-feed')).toContainText('慢请求 A')
  await expect(page.locator('.ask-panel')).toContainText('正在思考')

  // Switch while the mocked SSE response is still held open. The request is routed by
  // its captured workflow scope, not by whichever workflow is visible now.
  await gotoResearch(page, b)
  await expect(page.locator('.ask-feed')).not.toContainText('慢请求 A')
  await expect(page.locator('.ask-feed')).not.toContainText('SLOW-A-ANSWER')
  await expect(page.locator('.ask-panel')).not.toContainText('正在思考')

  releaseA?.()
  await page.waitForTimeout(300)
  await expect(page.locator('.ask-feed')).not.toContainText('SLOW-A-ANSWER')

  await gotoResearch(page, a)
  await expect(page.locator('.ask-feed')).toContainText('SLOW-A-ANSWER 慢请求 A')
  expect(askCount).toBe(1)
  expect(errors).toEqual([])
})

test('Round 3：真实工件在问答中结构化显示，1366/深色/390 无溢出', async ({ page, request }, testInfo) => {
  const session: Session = await newIsolatedSession(request, 'round3-artifact')
  const seeded = await seedChartResearch(request, session.workspaceId, 'Round 3 工件研究')
  await page.route('**/api/ai/ask/stream', async (route) => {
    const artifact = { ...seeded.artifactView, format: 'quantlab.artifact-view/1' }
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: sse([
        {
          type: 'tool', tool: 'workflow_artifact', title: '读取研究工件', risk: 'read',
          args: { artifact_hash: seeded.chartHash }, status: 'done', preview: '真实图表',
          artifact_view: artifact,
        },
        terminal('MOCK-ANSWER：这是服务端校验后的真实工件，不是模型生成的数据。'),
      ]),
    })
  })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await uiLogin(page, session)
  await page.setViewportSize(VIEW)
  await gotoResearch(page, seeded)
  await openAskAI(page)
  await page.locator('.ask-panel textarea').fill('展示真实产物')
  const artifactLoaded = page.waitForResponse((response) =>
    response.request().method() === 'GET' && /\/api\/workflows\/artifacts\/[0-9a-f]+/.test(response.url()) && response.ok())
  await page.locator('.ask-panel .cmp-send').click()
  await artifactLoaded

  const panel = page.locator('.ask-panel')
  const artifact = panel.locator('.rs-artifact')
  await expect(artifact).toBeVisible()
  await expect(artifact).toHaveAttribute('aria-busy', 'false')
  const image = artifact.locator('.rs-image img').first()
  await expect(image).toBeVisible()
  await image.scrollIntoViewIfNeeded()
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true)
  const geometry = await page.evaluate(() => {
    const img = document.querySelector<HTMLImageElement>('.ask-panel .rs-artifact .rs-image img')
    const panelEl = document.querySelector<HTMLElement>('.ask-panel')
    if (!img || !panelEl) return null
    const i = img.getBoundingClientRect()
    const p = panelEl.getBoundingClientRect()
    return {
      naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight,
      width: i.width, height: i.height,
      right: i.right, panelRight: p.right, left: i.left, panelLeft: p.left,
      scrollWidth: document.documentElement.scrollWidth, innerWidth,
    }
  })
  expect(geometry, '真实工件图片没有进入问答面板').not.toBeNull()
  expect(geometry!.naturalWidth).toBeGreaterThanOrEqual(200)
  expect(geometry!.naturalHeight).toBeGreaterThanOrEqual(80)
  expect(geometry!.width).toBeGreaterThanOrEqual(120)
  expect(geometry!.height).toBeGreaterThanOrEqual(40)
  expect(geometry!.left).toBeGreaterThanOrEqual(geometry!.panelLeft - 1)
  expect(geometry!.right).toBeLessThanOrEqual(geometry!.panelRight + 1)
  expect(geometry!.scrollWidth).toBeLessThanOrEqual(geometry!.innerWidth + 1)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round3-1366x768-askai-artifact.png') })

  await page.emulateMedia({ colorScheme: 'dark' })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth > 0)).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)
  await page.screenshot({ path: path.join(ARTIFACTS, 'round3-1366x768-askai-dark.png') })

  await page.emulateMedia({ colorScheme: 'light' })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(panel).toBeVisible()
  await expect(image).toBeVisible()
  await image.scrollIntoViewIfNeeded()

  // Bounded two-RAF stabilization. A viewport resize also runs the sidebar/layout
  // transitions, so a single early sample can still see the pre-transition width.
  // Wait until two consecutive animation-frame samples agree and satisfy the intended
  // geometry, then collect the mobile snapshot and full diagnostics in the same layout.
  const observed = await page.evaluate(async () => {
    const box = (selector: string) => {
      const el = document.querySelector<HTMLElement>(selector)
      if (!el) return null
      const r = el.getBoundingClientRect()
      return { left: r.left, right: r.right, width: r.width }
    }
    const geometry = () => ({
      scrollWidth: document.documentElement.scrollWidth,
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      innerWidth,
      panel: box('.ask-panel'),
      strip: box('.ask-research'),
      composer: box('.ask-panel .cmp-box'),
      artifact: box('.ask-panel .rs-artifact'),
      stripVisible: !!document.querySelector('.ask-research'),
    })
    type Geometry = ReturnType<typeof geometry>
    const settled = (g: Geometry) => {
      const { panel, strip, composer, artifact } = g
      if (!panel || !strip || !composer || !artifact) return false
      if (g.scrollWidth > g.innerWidth + 1 || panel.width > g.innerWidth + 1) return false
      return [strip, composer, artifact].every((item) =>
        item.left >= panel.left - 1 && item.right <= panel.right + 1)
    }
    const deadline = performance.now() + 3000
    let previous = geometry()
    while (performance.now() < deadline) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve())))
      const current = geometry()
      if (settled(current) && JSON.stringify(current) === JSON.stringify(previous)) break
      previous = current
    }
    const mobile = geometry()

    const selectors = [
      '.ask-panel', '.ask-feed', '.ask-foot', '.ask-panel .cmp', '.ask-panel .cmp-box',
      '.ask-panel .cmp-above', '.ask-research', '.ask-panel .rs-artifact', '.sidebar', 'main',
    ]
    const rect = (el: Element) => {
      const r = el.getBoundingClientRect()
      return { x: r.x, y: r.y, left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height }
    }
    const nodes = selectors.map((selector) => {
      const el = document.querySelector<HTMLElement>(selector)
      if (!el) return { selector, missing: true }
      const cs = getComputedStyle(el)
      return {
        selector,
        tag: el.tagName.toLowerCase(),
        class: typeof el.className === 'string' ? el.className.slice(0, 160) : el.getAttribute('class') || '',
        rect: rect(el),
        clientWidth: el.clientWidth,
        scrollWidth: el.scrollWidth,
        style: {
          position: cs.position,
          left: cs.left,
          right: cs.right,
          width: cs.width,
          minWidth: cs.minWidth,
          maxWidth: cs.maxWidth,
          transform: cs.transform,
          marginLeft: cs.marginLeft,
          marginRight: cs.marginRight,
          marginTop: cs.marginTop,
          marginBottom: cs.marginBottom,
          paddingLeft: cs.paddingLeft,
          paddingRight: cs.paddingRight,
          borderLeftWidth: cs.borderLeftWidth,
          borderRightWidth: cs.borderRightWidth,
          overflowX: cs.overflowX,
          display: cs.display,
          boxSizing: cs.boxSizing,
        },
      }
    })
    const offenders: unknown[] = []
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      const r = el.getBoundingClientRect()
      if (r.width === 0 && r.height === 0) continue
      if (r.left >= -1 && r.right <= innerWidth + 1) continue
      const cs = getComputedStyle(el)
      offenders.push({
        tag: el.tagName.toLowerCase(),
        class: typeof el.className === 'string' ? el.className.slice(0, 160) : el.getAttribute('class') || '',
        rect: rect(el),
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
        position: cs.position,
      })
      if (offenders.length >= 20) break
    }
    const vv = window.visualViewport
    return {
      mobile,
      diagnostics: {
        window: { innerWidth, innerHeight, scrollX, scrollY, devicePixelRatio },
        visualViewport: vv ? {
          width: vv.width, height: vv.height, offsetLeft: vv.offsetLeft, offsetTop: vv.offsetTop,
          pageLeft: vv.pageLeft, pageTop: vv.pageTop, scale: vv.scale,
        } : null,
        document: {
          clientWidth: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
        },
        body: {
          clientWidth: document.body.clientWidth,
          scrollWidth: document.body.scrollWidth,
        },
        nodes,
        offenders,
      },
    }
  })
  const mobile = observed.mobile
  const diagnostics = observed.diagnostics
  const diagnosticPayload = {
    test: 'Round 3 case3 mobile 390 diagnostics',
    observedAt: new Date().toISOString(),
    mobile,
    ...diagnostics,
  }
  const diagnosticText = JSON.stringify(diagnosticPayload, null, 2)
  const diagnosticPath = path.join(ARTIFACTS, 'round3-mobile-diagnostics.json')
  writeFileSync(diagnosticPath, diagnosticText, 'utf8')
  await testInfo.attach('round3-mobile-diagnostics.json', { body: diagnosticText, contentType: 'application/json' })
  console.log(`round3 mobile diagnostics written: ${diagnosticPath}`)

  expect(mobile.stripVisible).toBe(true)
  expect(mobile.panel).not.toBeNull()
  expect(mobile.panel!.width).toBeLessThanOrEqual(390)
  expect(mobile.scrollWidth).toBeLessThanOrEqual(mobile.innerWidth + 1)
  for (const [name, box] of [['strip', mobile.strip], ['composer', mobile.composer], ['artifact', mobile.artifact]] as const) {
    expect(box, `${name} 不存在`).not.toBeNull()
    expect(box!.left, `${name} 左边越出面板`).toBeGreaterThanOrEqual(mobile.panel!.left - 1)
    expect(box!.right, `${name} 右边越出面板`).toBeLessThanOrEqual(mobile.panel!.right + 1)
  }
  await page.screenshot({ path: path.join(ARTIFACTS, 'round3-390x844-askai.png') })
  expect(errors).toEqual([])
})

function researchPayload(draft: any) {
  const pin = (draft?.pins || []).find((item: any) => typeof item?.title === 'string' && item.title.startsWith('研究上下文 ·'))
  expect(pin, 'handoff 缺少研究上下文 pin').toBeTruthy()
  const data = JSON.parse(pin.text)
  expect(data.format).toBe('quantlab.research-handoff/1')
  return data
}

test('Round 3：主入口 handoff 保留研究身份与已选输出，移除项不出现', async ({ page, request }) => {
  const session: Session = await newIsolatedSession(request, 'round3-handoff')
  const a = await seedChartResearch(request, session.workspaceId, 'Round 3 Handoff 研究')
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem
    ;(window as unknown as { __qaHandoffs?: unknown[] }).__qaHandoffs = []
    Storage.prototype.setItem = function (key: string, value: string) {
      if (key === 'qa.lab.draft') {
        try { ((window as unknown as { __qaHandoffs: unknown[] }).__qaHandoffs).push(JSON.parse(value)) } catch { /* 忽略坏 payload */ }
      }
      return original.call(this, key, value)
    }
  })
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await uiLogin(page, session)
  await page.setViewportSize(VIEW)
  await gotoResearch(page, a)
  await openAskAI(page)
  await expect(page.locator('.ask-ref')).toHaveCount(1)

  // Main AskAI button intentionally navigates to the full lab; capture the handoff
  // payload before the lab consumes it. The sidebar was only used to inspect refs.
  await page.locator('.ask-panel .ask-icon').click()
  await expect(page.locator('.ask-panel')).toBeHidden()
  await page.locator('.ask-fab').click()
  await expect(page).toHaveURL(/#\/lab$/)
  let handoffs = await page.evaluate(() => (window as unknown as { __qaHandoffs?: any[] }).__qaHandoffs || [])
  expect(handoffs).toHaveLength(1)
  const attached = researchPayload(handoffs[0])
  expect(attached.workflow.id).toBe(a.wid)
  expect(attached.workflow.title).toBe(a.title)
  expect(attached.selected_node.id).toBe('chart')
  expect(attached.run.id).toBe(a.runId)
  expect(attached.run.view_mode).toBe('frozen')
  expect(attached.outputs).toEqual([
    { port: 'chart', type: 'chart', hash: a.chartHash, run_id: a.runId, node_id: 'chart' },
  ])
  expect(attached).not.toHaveProperty('nodes')
  expect(attached).not.toHaveProperty('edges')

  // A removed output must not reappear in a later handoff; identity remains.
  await gotoResearch(page, a)
  await openAskAI(page)
  await page.locator('.ask-ref').first().click()
  await expect(page.locator('.ask-ref')).toHaveCount(0)
  await page.locator('.ask-panel .ask-icon').click()
  await page.locator('.ask-fab').click()
  await expect(page).toHaveURL(/#\/lab$/)
  handoffs = await page.evaluate(() => (window as unknown as { __qaHandoffs?: any[] }).__qaHandoffs || [])
  expect(handoffs).toHaveLength(2)
  const removed = researchPayload(handoffs[1])
  expect(removed.workflow.id).toBe(a.wid)
  expect(removed.workflow.title).toBe(a.title)
  expect(removed.selected_node.id).toBe('chart')
  expect(removed.run.id).toBe(a.runId)
  expect(removed.outputs).toEqual([])
  expect(JSON.stringify(removed)).not.toContain(a.chartHash)
  expect(errors).toEqual([])
})
