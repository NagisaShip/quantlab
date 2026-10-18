import { expect, test } from '@playwright/test'

import { api, newIsolatedSession, uiLogin } from './helpers'

test.describe('08 · 研究流程与导师', () => {
  test('浏览器：研究流程展示完整阶段、可搜索，导师课程可独立打开且关闭有提示，移动端无横向溢出', async ({ page, request }) => {
    const session = await newIsolatedSession(request, 'research-flow')
    await uiLogin(page, session)

    await page.goto('/#/flow')
    await expect(page.getByRole('heading', { name: '研究流程', exact: true })).toBeVisible()
    await expect(page.locator('.rf-stage-card')).toHaveCount(7)
    expect(await page.locator('.rf-item').count()).toBeGreaterThan(40)
    expect(await page.locator('.rf-tool').count()).toBeGreaterThan(10)
    await expect(page.locator('.rf-journey button')).toHaveCount(7)
    for (const label of ['AI 研究室', '研究角色库', 'Agent 提示词库', '模型服务与成本', 'API 与工具参考', '群组与任务', '通知中心', '问题反馈']) {
      await expect(page.locator('.rf-tool').filter({ hasText: label })).toHaveCount(1)
    }

    await page.locator('.rf-tool-tabs button').filter({ hasText: '研究配置' }).click()
    await expect(page.locator('.rf-tool')).toHaveCount(5)
    for (const label of ['研究角色库', 'Agent 提示词库', '模型服务与成本', 'API 与工具参考', '能力矩阵与功能开关']) {
      await expect(page.locator('.rf-tool').filter({ hasText: label })).toHaveCount(1)
    }
    await page.locator('.rf-tool-tabs button').filter({ hasText: '全部' }).click()

    await page.locator('.rf-search input').fill('全仓')
    await expect(page.locator('.rf-item')).toHaveCount(1)
    await expect(page.locator('.rf-item')).toContainText('账本 v2 合约模式')

    await page.goto('/#/mentor')
    await expect(page.getByRole('heading', { name: '研究导师', exact: true })).toBeVisible()
    await expect(page.locator('.mentor-card')).toHaveCount(7)

    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/#/flow')
    await expect(page.locator('.rf-stage-card').first()).toBeVisible()
    await page.waitForTimeout(500)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(0)

    // raw fetch 不会自动带 X-Workspace-Id；必须读取前端请求层实际使用的 localStorage 工作区。
    const activeWorkspace = await page.evaluate(() => window.localStorage.getItem('qa.workspace'))
    expect(activeWorkspace, '浏览器请求层没有当前工作区').toBeTruthy()
    const off = await api(request, '/api/workspace/features', { method: 'PUT', workspaceId: activeWorkspace!, data: { features: { 'research.mentor': false } } })
    expect(off.status, JSON.stringify(off.body)).toBe(200)
    const effective = await api(request, '/api/workspace/features', { workspaceId: activeWorkspace! })
    expect(effective.body.enabled['research.mentor'], JSON.stringify(effective.body.enabled)).toBe(false)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/#/mentor')
    await page.reload()
    await expect(page.getByRole('heading', { name: '研究导师', exact: true })).toBeVisible()
    await expect(page.getByText('研究导师课程已关闭')).toBeVisible()
    await expect(page.getByRole('button', { name: '打开功能开关' })).toBeVisible()
  })
})
