import { expect, test } from '@playwright/test'
import { api, registerOrLogin, uiLogin, uniqueEmail } from './helpers'

test.describe('09 · 首次注册新手引导', () => {
  test('邀请成员首次登录：Bot 欢迎层、步骤推进、完成与持久化', async ({ page, request }) => {
    const admin = await registerOrLogin(request)
    const inv = await api(request, '/api/workspace/invites', {
      method: 'POST',
      workspaceId: admin.workspaceId,
      data: { role: 'member' },
    })
    expect(inv.status, JSON.stringify(inv.body)).toBe(200)
    const email = uniqueEmail('onboarding')
    const password = 'Passw0rd!'
    const reg = await api(request, '/api/auth/register', {
      method: 'POST',
      data: { email, password, name: '新手成员', invite_code: inv.body.invite.code },
    })
    expect(reg.status, JSON.stringify(reg.body)).toBe(200)

    await uiLogin(page, { email, password, workspaceId: reg.body.workspace_id }, { onboarding: true })
    await expect(page.locator('.onboarding-welcome-card')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('heading', { name: '你好，我是 QuantLab Bot' })).toBeVisible()
    await expect(page.locator('.onboarding-welcome-card .qbot-enter')).toHaveCount(1)

    await page.getByRole('button', { name: '开始 3 分钟引导' }).click()
    await expect(page.locator('.onboarding-tutor')).toBeVisible()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('提出第一个研究目标')
    await expect(page.locator('.onboarding-bot .qbot-hand')).toHaveCount(2)
    await page.locator('.onboarding-bot').click()
    await expect(page.locator('.onboarding-tip')).toBeVisible()

    await page.getByRole('button', { name: '我发送了，继续' }).click()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('看多 Agent 如何分工')

    await page.getByRole('button', { name: '我已了解' }).click()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('回测不是只看收益')

    await page.getByRole('button', { name: '我已看过审查' }).click()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('第一条研究链路完成')

    await page.getByRole('button', { name: '完成引导' }).click()
    await expect(page.locator('.onboarding-tutor')).toHaveCount(0)

    const state = await api(request, '/api/onboarding', { workspaceId: reg.body.workspace_id })
    expect(state.body.onboarding.status).toBe('completed')
    expect(state.body.onboarding.completed_steps).toContain('finish')

    await page.reload()
    await expect(page.locator('.onboarding-welcome-card')).toHaveCount(0)
  })
})
