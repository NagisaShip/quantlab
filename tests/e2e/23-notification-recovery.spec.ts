import { expect, test } from '@playwright/test'
import { newIsolatedSession, uiLogin } from './helpers'

test('通知核对前不显示已推送，确认后显示交付结果', async ({ page, request }, testInfo) => {
  const session = await newIsolatedSession(request, 'Notification recovery')
  let complete = false
  let confirmations = 0
  let resumes = 0
  const base = '/api/long-running/records/durable-note/runtime'
  await page.route('**/api/notifications*', route => route.fulfill({ json: {
    notifications: [{ id: 'durable-note', agent_id: 'daily_digest', run_key: 'digest:2026-10-25', ts: Date.now(),
      status: complete ? 'ok' : 'waiting', runtime_run_id: 'durable-execution',
      delivery_state: complete ? 'sent' : 'reconciling', output: complete ? '已核实的简报' : '', pushed_to: complete ? ['console'] : [] }], pending: [],
  } }))
  await page.route(`**${base}**`, async route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/effect-resolution')) {
      const payload = route.request().postDataJSON()
      expect(payload.step_id).toBe('delivery-step')
      expect(payload.outcome).toBe('succeeded')
      confirmations++
    } else if (path.endsWith('/resume')) { resumes++; complete = true }
    await route.fulfill({ json: { accepted: true, can_resume: true, can_resolve: true,
      run: { state: complete ? 'COMPLETED' : 'WAITING', steps: complete ? [] : [
        { id: 'delivery-step', kind: 'tool', state: 'RECONCILING', payload: { name: 'deliver_notification' } },
      ] } } })
  })
  await uiLogin(page, session)
  await page.goto('/#/long-running/push')
  const card = page.locator('article.role-card').filter({ hasText: 'digest:2026-10-25' })
  await expect(card.getByText('等待核对或执行', { exact: true })).toBeVisible()
  await expect(card.getByText('已推送', { exact: true })).toHaveCount(0)
  const confirm = card.getByRole('button', { name: '确认并继续', exact: true })
  await expect(confirm).toBeDisabled()
  await card.getByLabel('我已在外部系统核实').check()
  await confirm.click()
  await expect(card.getByText('已推送', { exact: true })).toBeVisible()
  await expect(card.getByText('已核实的简报', { exact: true })).toBeVisible()
  expect(confirmations).toBe(1)
  expect(resumes).toBe(1)
  await page.screenshot({ path: testInfo.outputPath('notification-confirmed.png'), fullPage: true })
})
