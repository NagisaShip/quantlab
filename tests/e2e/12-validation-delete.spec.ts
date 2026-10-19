import { expect, test } from '@playwright/test'
import { registerOrLogin, uiLogin } from './helpers'

test.describe('12 · 上线验证删除策略', () => {
  test('策略阶段可以直接删除策略代码目录', async ({ page, request }) => {
    const session = await registerOrLogin(request)
    await uiLogin(page, session)
    await page.goto('/#/validation')
    await expect(page.locator('.fx-table')).toBeVisible({ timeout: 30_000 })

    const row = page.locator('tr', { hasText: '内置 · 双均线趋势' }).first()
    await expect(row).toBeVisible()
    await row.getByRole('button', { name: '删除' }).click()
    await expect(page.getByRole('alertdialog')).toContainText('代码目录与全部历史版本会真实删除')
    await page.getByRole('button', { name: '确定' }).click()
    await expect(page.getByText('策略及代码目录已删除')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('内置 · 双均线趋势')).toHaveCount(0)
  })
})
