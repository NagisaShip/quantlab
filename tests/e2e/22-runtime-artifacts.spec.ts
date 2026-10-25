import { expect, test } from '@playwright/test'
import { api, newIsolatedSession, uiLogin } from './helpers'

test('对话展示交付快照下载与页面，拒绝外部链接', async ({ page, request }, testInfo) => {
  const session = await newIsolatedSession(request, 'Artifact view')
  const created = await api(request, '/api/lab/projects', { workspaceId: session.workspaceId, method: 'POST', data: {
    kind: 'research', title: '文档交付验收', idea: '检查交付快照',
  } })
  expect(created.status).toBe(200)
  const pid = created.body.project.id
  const url = '/api/local/runtime-artifacts/abc123/' + 'a'.repeat(64)
  // A UI fixture checks rendering separately from real snapshot authorization HTTP tests.
  await page.route(u => u.pathname === `/api/lab/projects/${pid}`, async route => {
    const response = await route.fetch()
    const body = await response.json()
    body.messages = [...(body.messages || []), { seq: 999, role: 'assistant', kind: 'tool', content: '',
      data: { status: 'ok', label: '交付文件', name: 'document_delivery', preview: {
        download_url: url, pages: [{ page: 1, url }, { page: 2, url: 'https://untrusted.invalid/leak.png' }],
      } } }]
    await route.fulfill({ response, json: body })
  })
  await page.route('**/api/local/runtime-artifacts/**', route => route.fulfill({
    contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="760" viewBox="0 0 600 760"><rect x="1" y="1" width="598" height="758" fill="white" stroke="#d4d4d4"/><text x="50" y="80" font-family="sans-serif" font-size="28">Document preview</text><text x="50" y="120" font-family="sans-serif" font-size="16" fill="#666">Runtime delivery fixture</text><path d="M50 160H550M50 195H500M50 230H540M50 265H480" stroke="#b0b0b0" stroke-width="8"/><rect x="50" y="330" width="500" height="180" fill="#f2f5f8"/><path d="M50 390H550M50 450H550M220 330V510" stroke="#c8cdd3"/><text x="50" y="700" font-family="sans-serif" font-size="14" fill="#777">1</text></svg>',
  }))
  await uiLogin(page, session)
  await page.setViewportSize({ width: 1366, height: 900 })
  await page.goto(`/#/lab/${pid}`)
  const delivery = page.locator('.cb-delivery')
  await expect(delivery.getByRole('link', { name: '下载文件' })).toHaveAttribute('href', new RegExp('/api/local/runtime-artifacts/abc123/'))
  await expect(delivery.getByRole('img', { name: '第 1 页' })).toBeVisible()
  await expect(delivery.getByRole('img', { name: '第 2 页' })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('artifact-delivery.png'), animations: 'disabled' })
  await page.reload()
  await expect(page.locator('.cb-delivery').getByRole('link', { name: '下载文件' })).toBeVisible()
})
