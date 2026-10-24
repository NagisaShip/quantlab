import { expect, test } from '@playwright/test'
import { createServer } from 'node:http'
import { api, newIsolatedSession, uiLogin } from './helpers'

test('研究室核对模型费用后展示已保存回答，不重新生成', async ({ page, request }, testInfo) => {
  test.skip(process.env.QAGENT_AGENT_RUNTIME !== '1', 'This acceptance leg enables the Runtime explicitly')
  let calls = 0
  const provider = createServer((req, res) => {
    req.resume()
    req.on('end', () => {
      calls += 1
      res.writeHead(200, { 'Content-Type': 'text/event-stream' })
      res.end('data: ' + JSON.stringify({ choices: [{ delta: { content: '已保存的真实模型回答' }, finish_reason: null }] }) + '\n\n' +
        'data: ' + JSON.stringify({ choices: [{ delta: {}, finish_reason: 'stop' }] }) + '\n\ndata: [DONE]\n\n')
    })
  })
  await new Promise<void>((resolve) => provider.listen(0, '127.0.0.1', resolve))
  try {
    const address = provider.address()
    if (!address || typeof address === 'string') throw new Error('provider did not listen')
    const session = await newIsolatedSession(request, 'Runtime recovery')
    const workspaceId = session.workspaceId
    const configured = await api(request, '/api/config/providers', { workspaceId, method: 'POST', data: {
      name: 'runtime-acceptance', type: 'openai_compatible', base_url: `http://127.0.0.1:${address.port}/v1`,
      api_key: 'test-only-key', models: ['test-model'],
    } })
    expect(configured.status, JSON.stringify(configured.body)).toBe(200)
    const route = await api(request, '/api/config/routing', { workspaceId, method: 'PUT', data: { default: 'runtime-acceptance/test-model' } })
    expect(route.status, JSON.stringify(route.body)).toBe(200)
    const created = await api(request, '/api/lab/projects', { workspaceId, method: 'POST', data: {
      kind: 'research', title: 'Runtime 恢复验收', idea: '验证持久恢复',
    } })
    expect(created.status, JSON.stringify(created.body)).toBe(200)
    const pid = created.body.project.id
    const started = await api(request, `/api/lab/projects/${pid}/messages`, { workspaceId, method: 'POST', data: { text: '回答一次即可', wait: true } })
    expect(started.status, JSON.stringify(started.body)).toBe(200)
    const state = await api(request, `/api/lab/projects/${pid}/runtime`, { workspaceId })
    expect(state.body.run.state).toBe('WAITING')
    expect(calls).toBe(1)
    await uiLogin(page, session)
    await page.setViewportSize({ width: 1366, height: 900 })
    await page.goto(`/#/lab/${pid}`)
    const recovery = page.getByRole('region', { name: '恢复执行' })
    await expect(recovery).toContainText('模型用量待核对')
    await expect(recovery.getByRole('button', { name: '确认并继续' })).toBeDisabled()
    await recovery.getByLabel('服务商确认未计费').check()
    await recovery.getByLabel('我已在外部系统核实').check()
    await page.screenshot({ path: testInfo.outputPath('runtime-billing-review.png'), animations: 'disabled' })
    await recovery.getByRole('button', { name: '确认并继续' }).click()
    await expect(recovery).toHaveCount(0)
    await expect.poll(async () => (await api(request, `/api/lab/projects/${pid}`, { workspaceId })).body.project.status).toBe('idle')
    await page.reload()
    await expect(page.getByText('已保存的真实模型回答', { exact: true })).toBeVisible()
    await expect(page.getByText('生成中', { exact: true })).toHaveCount(0)
    await expect(page.getByText('执行失败', { exact: true })).toHaveCount(0)
    expect(calls).toBe(1)
    const completed = await api(request, `/api/lab/projects/${pid}/runtime`, { workspaceId })
    expect(completed.body.run.state).toBe('COMPLETED')
    await page.screenshot({ path: testInfo.outputPath('runtime-recovered.png'), animations: 'disabled' })
  } finally {
    await new Promise<void>((resolve, reject) => provider.close((error) => error ? reject(error) : resolve()))
  }
})
