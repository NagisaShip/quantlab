import { expect, test } from '@playwright/test'
import { registerOrLogin, uiLogin } from './helpers'

test.describe('11 · 运维总览 worker 调度', () => {
  test('管理员可以查看服务器内存并调整 worker 槽位', async ({ page, request }) => {
    const session = await registerOrLogin(request)
    await uiLogin(page, session)
    await page.goto('/#/admin/overview')
    await expect(page.locator('.adm')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('内存', { exact: true }).first()).toBeVisible()
    await expect(page.getByText('worker 调度与内存')).toBeVisible()
    await expect(page.getByText('槽位满时新任务进入 QUEUED 队列自动等待')).toBeVisible()

    const slots = page.locator('.worker-config-row input[type="number"]')
    await expect(slots).toBeVisible()
    await slots.fill('6')
    await page.getByRole('button', { name: '保存槽位' }).click()
    await expect(page.getByText('worker 槽位已保存，运行中的任务不受影响')).toBeVisible({ timeout: 15_000 })
    await expect(slots).toHaveValue('6')
  })
})
