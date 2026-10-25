import { expect, test } from '@playwright/test'
import { createServer } from 'node:http'
import { api, newIsolatedSession, uiLogin } from './helpers'

for (const mode of ['parallel', 'sequential', 'debate'] as const) {
test(`团队评审等待时继续与取消均沿用原执行图 · ${mode}`, async ({ page, request }, testInfo) => {
  test.skip(process.env.QAGENT_AGENT_RUNTIME !== '1', 'Runtime migration acceptance')
  let calls = 0
  const provider = createServer((req, res) => {
    req.resume()
    req.on('end', () => {
      calls += 1
      res.writeHead(200, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ model: 'test-model', choices: [{ message: { role: 'assistant', content: '已保存的角色意见' }, finish_reason: 'stop' }] }))
    })
  })
  await new Promise<void>((resolve) => provider.listen(0, '127.0.0.1', resolve))
  try {
    const address = provider.address()
    if (!address || typeof address === 'string') throw new Error('provider did not listen')
    const session = await newIsolatedSession(request, 'Team Runtime')
    const workspaceId = session.workspaceId
    const configured = await api(request, '/api/config/providers', { workspaceId, method: 'POST', data: {
      name: 'team-acceptance', type: 'openai_compatible', base_url: `http://127.0.0.1:${address.port}/v1`, api_key: 'test-only', models: ['test-model'],
    } })
    expect(configured.status, JSON.stringify(configured.body)).toBe(200)
    await api(request, '/api/config/routing', { workspaceId, method: 'PUT', data: { default: 'team-acceptance/test-model' } })
    const role = await api(request, '/api/agents/roles', { workspaceId, method: 'POST', data: { name: '持久评审员', system_prompt: '请评审', tools: [], restrict_tools: true } })
    expect(role.status, JSON.stringify(role.body)).toBe(200)
    const team = await api(request, '/api/agents/teams', { workspaceId, method: 'POST', data: {
      id: 'lab_review', name: '持久评审组', mode, role_ids: mode === 'sequential' ? [role.body.role.id, role.body.role.id] : [role.body.role.id], synthesizer_id: role.body.role.id,
    } })
    expect(team.status, JSON.stringify(team.body)).toBe(200)
    const project = await api(request, '/api/lab/projects', { workspaceId, method: 'POST', data: { kind: 'research', title: 'Team 恢复验收', idea: '验证评审恢复' } })
    expect(project.status, JSON.stringify(project.body)).toBe(200)
    const pid = project.body.project.id
    const started = await api(request, `/api/lab/projects/${pid}/messages`, { workspaceId, method: 'POST', data: { text: '/team 验证持久评审', wait: true } })
    expect(started.status, JSON.stringify(started.body)).toBe(200)
    expect(calls).toBe(1)
    await uiLogin(page, session)
    await page.setViewportSize({ width: 1366, height: 900 })
    await page.goto(`/#/lab/${pid}`)
    const card = page.locator('.lab2-card').filter({ hasText: '持久评审组' })
    await expect(card.getByRole('button', { name: '继续评审', exact: true })).toBeVisible()
    await expect(card).toContainText('等待核对')
    await expect(card.getByText('Runtime', { exact: true })).toHaveCount(0)
    if (mode !== 'parallel') await expect(card).toContainText('等待前序角色')
    await expect(page.locator('.lab-state-pill').first()).toContainText('等待核对')
    await expect(page.locator('.lab-msg-state-label').filter({ hasText: '已生成' })).toHaveCount(0)
    await card.getByRole('button', { name: '继续评审', exact: true }).click()
    await expect(card.getByRole('button', { name: '继续评审', exact: true })).toBeEnabled()
    expect(calls).toBe(1)
    await page.screenshot({ path: testInfo.outputPath('team-waiting.png'), animations: 'disabled' })
    await card.getByRole('button', { name: '取消评审', exact: true }).click()
    await expect(card).toContainText('取消中')
    await expect(card.getByText('失败', { exact: true })).toHaveCount(0)
    await page.reload()
    await expect(card).toContainText('取消中')
    await expect(page.locator('.lab-state-pill').first()).toContainText('取消中')
    await expect(card.getByRole('button', { name: '继续评审', exact: true })).toHaveCount(0)
    expect(calls).toBe(1)
    await page.screenshot({ path: testInfo.outputPath('team-canceling.png'), animations: 'disabled' })
    const recovery = card.getByRole('region', { name: '恢复执行' })
    await expect(recovery).toContainText('模型用量待核对')
    await expect(recovery.getByRole('button', { name: '确认并继续' })).toBeDisabled()
    await recovery.getByLabel('服务商确认未计费').check()
    await recovery.getByLabel('我已在外部系统核实').check()
    await recovery.getByRole('button', { name: '确认并继续' }).click()
    await expect(card).toContainText('已取消')
    await expect(recovery).toHaveCount(0)
    expect(calls).toBe(1)
    await page.reload()
    await expect(card).toContainText('已取消')
    await expect(page.locator('.lab-state-pill').first()).toContainText('已取消')
    await expect(page.locator('.lab-msg-state-label').filter({ hasText: '等待核对' })).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath('team-reconciled-canceled.png'), animations: 'disabled' })
  } finally {
    await new Promise<void>((resolve, reject) => provider.close((error) => error ? reject(error) : resolve()))
  }
})

}
