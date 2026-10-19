import { expect, test, type APIRequestContext } from '@playwright/test'
import { api, registerOrLogin, uiLogin, uniqueEmail } from './helpers'

async function invitedMember(request: APIRequestContext) {
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
  return { email, password, workspaceId: reg.body.workspace_id as string }
}

test.describe('09 · 首次注册新手引导', () => {
  test('邀请成员首次登录：与管理员一致的完整页面链路、完成与持久化', async ({ page, request }) => {
    const session = await invitedMember(request)
    await uiLogin(page, session, { onboarding: true })
    await expect(page.locator('.onboarding-welcome-card')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('heading', { name: '你好，我是 QuantLab Bot' })).toBeVisible()
    await expect(page.locator('.onboarding-welcome-card .qbot-enter')).toHaveCount(1)
    await expect(page.locator('.onboarding-welcome-card')).not.toContainText('真实界面操作，不是看一组说明页')
    await expect(page.locator('.onboarding-welcome-card')).not.toContainText('没有模型 Key 也能先用模板完成回测')
    await expect(page.locator('.onboarding-welcome-card')).not.toContainText('不会自动发送消息，也不会开启真实资金通道')
    await expect(page.getByRole('button', { name: '去配置模型 Key，获得更佳体验' })).toBeVisible()

    await page.getByRole('button', { name: '开始全链路引导' }).click()
    await expect(page.locator('.onboarding-tutor')).toBeVisible()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('先认识研究闭环')
    await expect(page.locator('.onboarding-bot .qbot-hand')).toHaveCount(1)
    await expect(page.locator('.onboarding-bot .qbot-hand-outline')).toHaveCount(1)
    await page.locator('.onboarding-bot').click()
    await expect(page.locator('.onboarding-tip')).toBeVisible()

    await page.getByRole('button', { name: '去 AI 研究室' }).click()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('提出第一个研究目标')
    await page.getByRole('button', { name: '我发送了，继续' }).click()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('看多 Agent 如何分工')
    await page.getByRole('button', { name: '我已了解' }).click()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('回测不是只看收益')
    await page.getByRole('button', { name: '看内置回测' }).click()
    await expect(page).toHaveURL(/#\/backtest\/[^/]+/)
    await expect(page.locator('[data-tour="backtest-review"]')).toBeVisible()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('回测不是只看收益')
    await page.getByRole('button', { name: '我已看过审查' }).click()

    const details: Record<string, { button: string; url: RegExp; text: string }> = {
      因子研究: { button: '看内置因子', url: /#\/factors\/builtin_factor_reversal_5/, text: '内置 · 5期短期反转' },
      预测模型: { button: '看内置模型', url: /#\/modellib\/builtin_model_ridge_multi/, text: '内置 · 线性多因子收益模型' },
      组合与执行: { button: '看内置组合', url: /#\/portfolio\/builtin_portfolio_risk_parity/, text: '内置 · 主流币风险平价' },
      上线验证: { button: '看内置验证', url: /#\/validation\/gate:builtin-ma-cross/, text: '内置 · 双均线趋势' },
      协作与复用: { button: '看内置策略帖', url: /#\/post\/seed-momentum-rotation/, text: '模板分享：多币种动量轮动（趋势质量打分）' },
      群组与任务: { button: '看内置群组', url: /#\/groups\/[a-f0-9]{12}\/tasks/, text: '复核内置双均线策略的回测与审查' },
    }
    for (const title of ['行情与数据', '因子研究', '预测模型', '组合与执行', '上线验证', '纸面模拟', '协作与复用', '群组与任务']) {
      await expect(page.locator('.onboarding-bubble h3')).toHaveText(title)
      const detail = details[title]
      if (detail) {
        await page.getByRole('button', { name: detail.button }).click()
        await expect(page).toHaveURL(detail.url)
        await expect(page.getByText(detail.text).first()).toBeVisible()
        await expect(page.locator('.onboarding-bubble h3')).toHaveText(title)
      }
      await page.locator('.onboarding-actions .primary').click()
    }
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('第一条研究链路完成')
    await page.getByRole('button', { name: '完成引导' }).click()
    await expect(page.locator('.onboarding-tutor')).toHaveCount(0)

    const state = await api(request, '/api/onboarding', { workspaceId: session.workspaceId })
    expect(state.body.onboarding.status).toBe('completed')
    expect(state.body.onboarding.completed_steps).toContain('square')
    expect(state.body.onboarding.completed_steps).toContain('groups')
    expect(state.body.onboarding.completed_steps).toContain('finish')

    await page.reload()
    await expect(page.locator('.onboarding-welcome-card')).toHaveCount(0)
  })

  test('欢迎页：模型 Key 配置入口直达模型 API 设置', async ({ page, request }) => {
    // 使用新邀请成员，避免部署管理员的 onboarding 已被其他 spec 标记完成。
    const session = await invitedMember(request)
    await uiLogin(page, session, { onboarding: true })
    await expect(page.locator('.onboarding-welcome-card')).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: '去配置模型 Key，获得更佳体验' }).click()
    await expect(page).toHaveURL(/#\/models/)
    await expect(page.locator('.set-shell')).toBeVisible()
    await expect(page.locator('.onboarding-welcome-card')).toHaveCount(0)
  })

  test('移动端：导师卡不超出视口、无横向滚动、操作按钮可见', async ({ page, request }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const session = await invitedMember(request)
    await uiLogin(page, session, { onboarding: true })
    await page.getByRole('button', { name: '开始全链路引导' }).click()
    await expect(page.locator('.onboarding-tutor')).toBeVisible()
    expect(await page.evaluate(() => window.innerWidth)).toBe(390)
    const box = await page.locator('.onboarding-tutor').boundingBox()
    expect(box).toBeTruthy()
    expect(box!.x).toBeGreaterThanOrEqual(0)
    expect(box!.x + box!.width).toBeLessThanOrEqual(390.5)
    expect(box!.y + box!.height).toBeLessThanOrEqual(844.5)
    await expect(page.locator('.onboarding-actions .primary')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true)

    // 小屏手机（375×667）同样不能越界，按钮仍需可见。
    await page.setViewportSize({ width: 375, height: 667 })
    await page.waitForTimeout(150)
    const small = await page.locator('.onboarding-tutor').boundingBox()
    expect(small).toBeTruthy()
    expect(small!.x).toBeGreaterThanOrEqual(0)
    expect(small!.x + small!.width).toBeLessThanOrEqual(375.5)
    expect(small!.y + small!.height).toBeLessThanOrEqual(667.5)
    await expect(page.locator('.onboarding-actions .primary')).toBeVisible()
    await page.setViewportSize({ width: 390, height: 844 })
    await page.waitForTimeout(150)

    // 继续走到「行情与数据」：手机端侧栏不可见，聚光灯应落在当前页页头而不是隐藏的导航项。
    await page.getByRole('button', { name: '去 AI 研究室' }).click()
    await page.getByRole('button', { name: '我发送了，继续' }).click()
    await page.getByRole('button', { name: '我已了解' }).click()
    await page.getByRole('button', { name: '我已看过审查' }).click()
    await expect(page.locator('.onboarding-bubble h3')).toHaveText('行情与数据')
    await expect(page.locator('[data-tour="page-data"]')).toBeVisible()
    await expect(page.locator('.onboarding-spotlight')).toBeVisible()
    const spot = await page.locator('.onboarding-spotlight').boundingBox()
    expect(spot).toBeTruthy()
    expect(spot!.x).toBeGreaterThanOrEqual(0)
    expect(spot!.x + spot!.width).toBeLessThanOrEqual(390.5)
    expect(spot!.height).toBeLessThan(260)
  })
})
