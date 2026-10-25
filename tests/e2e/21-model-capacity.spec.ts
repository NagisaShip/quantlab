import { expect, test } from '@playwright/test'
import { createServer } from 'node:http'
import { api, newIsolatedSession, uiLogin } from './helpers'

test('模型额度暂满时排队，释放后从原步骤恢复', async ({ page, request }, testInfo) => {
  test.skip(process.env.QAGENT_AGENT_RUNTIME !== '1' || process.env.QAGENT_MODEL_MAX_IN_FLIGHT !== '1', 'Requires Runtime and one model slot')
  let calls = 0
  let release: (() => void) | undefined
  const provider = createServer((req, res) => {
    req.resume()
    req.on('end', () => {
      calls += 1
      const finish = () => {
        res.writeHead(200, { 'Content-Type': 'text/event-stream' })
        res.end('data: ' + JSON.stringify({ choices: [{ delta: { content: '额度释放后完成' } }] }) + '\n\n' +
          'data: ' + JSON.stringify({ choices: [], usage: { prompt_tokens: 2, completion_tokens: 1 } }) + '\n\ndata: [DONE]\n\n')
      }
      if (calls === 1) release = finish
      else finish()
    })
  })
  await new Promise<void>((resolve) => provider.listen(0, '127.0.0.1', resolve))
  try {
    const address = provider.address()
    if (!address || typeof address === 'string') throw new Error('provider did not listen')
    const session = await newIsolatedSession(request, 'Model capacity')
    const workspaceId = session.workspaceId
    const configured = await api(request, '/api/config/providers', { workspaceId, method: 'POST', data: {
      name: 'capacity', type: 'openai_compatible', base_url: `http://127.0.0.1:${address.port}/v1`, api_key: 'test-only', models: ['test-model'],
    } })
    expect(configured.status, JSON.stringify(configured.body)).toBe(200)
    await api(request, '/api/config/routing', { workspaceId, method: 'PUT', data: { default: 'capacity/test-model' } })
    const create = async (title: string) => {
      const result = await api(request, '/api/lab/projects', { workspaceId, method: 'POST', data: { kind: 'research', title } })
      expect(result.status, JSON.stringify(result.body)).toBe(200)
      return result.body.project.id as string
    }
    const first = await create('占用模型额度')
    const second = await create('等待模型额度')
    await api(request, `/api/lab/projects/${first}/messages`, { workspaceId, method: 'POST', data: { text: '先执行', wait: false } })
    await expect.poll(() => calls).toBe(1)
    const waiting = await api(request, `/api/lab/projects/${second}/messages`, { workspaceId, method: 'POST', data: { text: '随后执行', wait: true } })
    expect(waiting.status, JSON.stringify(waiting.body)).toBe(200)
    const before = await api(request, `/api/lab/projects/${second}/runtime`, { workspaceId })
    expect(before.body.run.steps[0].state).toBe('WAITING_ADMISSION')
    const stepId = before.body.run.steps[0].id
    await uiLogin(page, session)
    await page.goto(`/#/lab/${second}`)
    await expect(page.locator('.lab-state-pill').first()).toContainText('排队')
    const recovery = page.getByRole('region', { name: '恢复执行' })
    await expect(recovery.getByRole('button', { name: '恢复执行', exact: true })).toBeVisible()
    await expect(page.getByText('模型用量待核对', { exact: true })).toHaveCount(0)
    await expect(page.locator('.lab-task-banner').getByRole('button', { name: '继续', exact: true })).toHaveCount(0)
    expect(calls).toBe(1)
    await page.screenshot({ path: testInfo.outputPath('capacity-wait.png'), animations: 'disabled' })
    release?.()
    release = undefined
    await expect.poll(async () => (await api(request, `/api/lab/projects/${first}/runtime`, { workspaceId })).body.run.state).toBe('COMPLETED')
    await recovery.getByRole('button', { name: '恢复执行', exact: true }).click()
    await expect.poll(async () => (await api(request, `/api/lab/projects/${second}/runtime`, { workspaceId })).body.run.state).toBe('COMPLETED')
    await expect(page.getByText('额度释放后完成', { exact: true })).toBeVisible()
    const completed = await api(request, `/api/lab/projects/${second}/runtime`, { workspaceId })
    expect(completed.body.run.steps).toHaveLength(1)
    expect(completed.body.run.steps[0].id).toBe(stepId)
    expect(calls).toBe(2)
    await page.screenshot({ path: testInfo.outputPath('capacity-completed.png'), animations: 'disabled' })
  } finally {
    release?.()
    await new Promise<void>((resolve, reject) => provider.close((error) => error ? reject(error) : resolve()))
  }
})
