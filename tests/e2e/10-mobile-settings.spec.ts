import { expect, test } from '@playwright/test'
import { registerOrLogin, uiLogin } from './helpers'

test.describe('10 · 手机设置页', () => {
  test('成员与邀请：页面不横向溢出，我的用量完整显示', async ({ page, request }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const session = await registerOrLogin(request)
    await uiLogin(page, session)
    await page.goto('/#/settings/members')
    await expect(page.locator('.members-page')).toBeVisible({ timeout: 30_000 })

    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)
    const panel = await page.locator('.member-self-usage').boundingBox()
    expect(panel).toBeTruthy()
    expect(panel!.x).toBeGreaterThanOrEqual(0)
    expect(panel!.x + panel!.width).toBeLessThanOrEqual(390.5)
    await expect(page.locator('.member-self-usage .usage-kpi')).toHaveCount(5)
    await expect(page.locator('.member-self-usage .usage-composition').first()).toContainText('研究记录构成')
    await expect(page.locator('.member-self-usage .usage-composition-track').first()).toBeVisible()

    const clipped = await page.locator('.member-self-usage .usage-kpi em').evaluateAll((nodes) =>
      nodes.filter((n) => n.scrollWidth > n.clientWidth + 1).length,
    )
    expect(clipped).toBe(0)

    await page.setViewportSize({ width: 375, height: 667 })
    await page.waitForTimeout(150)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)
    const small = await page.locator('.member-self-usage').boundingBox()
    expect(small).toBeTruthy()
    expect(small!.x + small!.width).toBeLessThanOrEqual(375.5)

    // 成员与邀请里的额度入口必须真的切到「额度与用量」，而不是停在原页。
    await page.goto('/#/settings/members')
    await expect(page.locator('.members-page')).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: '额度与用量 →' }).click()
    await expect(page).toHaveURL(/#\/settings\/quota/)
    await expect(page.getByRole('heading', { name: '额度与用量' })).toBeVisible()

    // 研究记录页提供多选与批量管理入口。
    await page.goto('/#/settings/retention')
    await expect(page.locator('.records-table')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: '批量删除' })).toBeVisible()
    await expect(page.getByRole('button', { name: '批量 PIN' })).toBeVisible()

    // 从头像进入设置后的其他常用页签也不能横向撑宽。
    for (const tab of ['account', 'quota', 'runs']) {
      await page.goto(`/#/settings/${tab}`)
      await expect(page.locator('.settings-page')).toBeVisible({ timeout: 30_000 })
      await page.waitForTimeout(200)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)
    }
  })
})
