/**
 * T12 · 步骤 4/7：启动实时模拟 → 暂停 → 模拟平仓并结束。
 *
 * 覆盖：
 * - 实时模拟是一个 kind=paper 的 run，由**独立 worker 的常驻驱动**推进；
 * - 启动后 run 进入 RUNNING，会话状态 running；
 * - 暂停：run 进入 PAUSED，会话状态 paused，且暂停期间不新增成交；
 * - 恢复：run 回到 RUNNING；
 * - 模拟平仓并结束：run 进入 COMPLETED，会话状态 stopped，持仓被清空，
 *   且平仓成交明确标注为模拟成交（平台没有真实下单路径）；
 * - 浏览器：走一遍启动 → 暂停 → 模拟平仓的按钮链路。
 *
 * 行情口径：用 `source=demo`（时间确定性演示行情）。原因是实时模拟的
 * `latest_bars()` 对 `source=exchange` 走 `source="exchange"` 强制真实行情、
 * 失败即报错——CI 无外网时那会直接失败。demo 行情在界面上被明确标注为演示行情，
 * 断言也按“演示行情”来写，不会把演示结果当成真实结果。
 */
import { expect, test } from '@playwright/test'
import {
  alwaysLongSpec,
  api,
  approveStrategy,
  assertRanOnExternalWorker,
  freezeVersion,
  paperCommand,
  paperSession,
  newIsolatedSession,
  saveDsl,
  startPaper,
  uiLogin,
  uiStartPaper,
  waitPaperFills,
  waitPaperRunState,
  waitPaperSessionStatus,
} from './helpers'

/**
 * 准备一个已冻结 + 已人工批准、可以进入实时模拟的策略。
 *
 * 用「永远做多」规则 + 1 分钟周期：第一根被评估的 bar 上就会建仓，
 * 这样“暂停后不新增成交”“平仓确实卖掉持仓”这些断言不依赖随机行情路径。
 */
async function readyStrategy(request: any, prefix: string) {
  const s = await newIsolatedSession(request, prefix)
  const { slug, version } = await saveDsl(request, s.workspaceId, alwaysLongSpec(`模拟-${prefix}`, { timeframe: '1m' }))
  await freezeVersion(request, s.workspaceId, version.id)
  await approveStrategy(request, s.workspaceId, slug)
  return { s, slug, version }
}

test.describe('04 · 实时模拟：启动 / 暂停 / 模拟平仓', () => {
  test('启动实时模拟：run 由独立 worker 推进，会话进入 running', async ({ request }) => {
    const { s, slug } = await readyStrategy(request, 'paper-start')

    const session = await startPaper(request, s.workspaceId, { slug, source: 'demo' })
    expect(session.id, '启动应返回会话 id（等于 run id）').toBeTruthy()
    expect(session.source, '本次用演示行情').toBe('demo')

    // 常驻任务由独立 worker 认领（不是 inline）。
    const owner = await assertRanOnExternalWorker(request, s.workspaceId, session.id)
    console.log(`[04] 实时模拟 run=${session.id} 由 worker ${owner} 推进`)

    const run = await waitPaperRunState(request, s.workspaceId, session.id, 'RUNNING')
    expect(run.kind, '实时模拟的 run kind 应为 paper').toBe('paper')

    // 会话 status 由 worker 步进时写入，可能比 run 的 state 晚一拍，
    // 所以这里要**等**它变成 running，而不是 run 一 RUNNING 就立刻断言。
    const view = await waitPaperSessionStatus(request, s.workspaceId, session.id, 'running')
    expect(view.status, `会话状态应为 running：${JSON.stringify(view.status)}`).toBe('running')

    // 收尾。
    await paperCommand(request, s.workspaceId, session.id, 'liquidate')
  })

  test('暂停后 run 为 PAUSED、会话为 paused，恢复后回到 RUNNING', async ({ request }) => {
    const { s, slug } = await readyStrategy(request, 'paper-pause')
    const session = await startPaper(request, s.workspaceId, { slug, source: 'demo' })
    await waitPaperRunState(request, s.workspaceId, session.id, 'RUNNING')

    // 先等第一根新 bar 到来、策略建仓，这样“暂停期间不新增成交”才有意义。
    const fills = await waitPaperFills(request, s.workspaceId, session.id, 1)
    expect(fills.length, '1 分钟周期下应在等待窗口内产生至少一笔成交').toBeGreaterThan(0)

    // 暂停。
    await paperCommand(request, s.workspaceId, session.id, 'pause')
    const paused = await waitPaperRunState(request, s.workspaceId, session.id, 'PAUSED')
    expect(paused.state).toBe('PAUSED')

    const pausedView = await waitPaperSessionStatus(request, s.workspaceId, session.id, 'paused')
    expect(pausedView.status, '暂停后会话状态应为 paused').toBe('paused')

    // 暂停期间不新增成交：记录暂停前的成交数，等一个 tick 周期后再看。
    const fillsBefore = (pausedView.fills || []).length
    await new Promise((ok) => setTimeout(ok, 4_000))
    const stillPaused = await paperSession(request, s.workspaceId, session.id)
    expect(stillPaused.status, '暂停期间状态应保持 paused').toBe('paused')
    expect(
      (stillPaused.fills || []).length,
      `暂停期间不应新增成交：暂停前 ${fillsBefore}，现在 ${(stillPaused.fills || []).length}`,
    ).toBe(fillsBefore)

    // 恢复。
    await paperCommand(request, s.workspaceId, session.id, 'resume')
    await waitPaperRunState(request, s.workspaceId, session.id, 'RUNNING')
    const resumed = await waitPaperSessionStatus(request, s.workspaceId, session.id, 'running')
    expect(resumed.status, '恢复后会话状态应为 running').toBe('running')

    await paperCommand(request, s.workspaceId, session.id, 'liquidate')
  })

  test('模拟平仓并结束：run 完成、持仓清空、平仓成交标注为模拟成交', async ({ request }) => {
    const { s, slug } = await readyStrategy(request, 'paper-liquidate')
    const session = await startPaper(request, s.workspaceId, { slug, source: 'demo' })
    await waitPaperRunState(request, s.workspaceId, session.id, 'RUNNING')

    // 等策略真的建了仓，否则“平仓”无事可做，断言就失去意义。
    await waitPaperFills(request, s.workspaceId, session.id, 1)

    const before = await paperSession(request, s.workspaceId, session.id)
    const positionsBefore = Object.keys(before.portfolio?.positions || {}).length
    expect(positionsBefore, `平仓前应有持仓：${JSON.stringify(before.portfolio)}`).toBeGreaterThan(0)

    await paperCommand(request, s.workspaceId, session.id, 'liquidate')
    const done = await waitPaperRunState(request, s.workspaceId, session.id, 'COMPLETED', 90_000)
    expect(done.state).toBe('COMPLETED')

    const view = await waitPaperSessionStatus(request, s.workspaceId, session.id, 'stopped')
    expect(view.status, '平仓结束后会话状态应为 stopped').toBe('stopped')
    expect(
      Object.keys(view.portfolio?.positions || {}).length,
      `平仓后不应还有持仓：${JSON.stringify(view.portfolio?.positions)}`,
    ).toBe(0)

    // 平仓成交必须能被区分出来（reason=liquidate），且明确是模拟成交（source=simulated）。
    const fills = view.fills || []
    const liquidating = fills.filter((f: any) => f.reason === 'liquidate' || f.reason === 'liquidate_and_stop')
    expect(
      liquidating.length,
      `平仓前有 ${positionsBefore} 个持仓，平仓后应留下 reason=liquidate 的成交：${JSON.stringify(fills.slice(-5))}`,
    ).toBeGreaterThan(0)
    for (const f of liquidating) {
      expect(f.side, '平仓应是卖出').toBe('sell')
      expect(f.source, `平仓成交必须标注为模拟成交，实际 source=${f.source}`).toBe('simulated')
    }

    // 会话消息必须说清“模拟成交，不是真实下单”。
    expect(String(view.message || ''), `会话消息应说明是模拟平仓：${view.message}`).toMatch(/模拟平仓|模拟成交/)
  })

  test('实时模拟报告包含 7/30 天检查点，且未满时标注为进行中', async ({ request }) => {
    const { s, slug } = await readyStrategy(request, 'paper-report')
    const session = await startPaper(request, s.workspaceId, { slug, source: 'demo' })
    await waitPaperRunState(request, s.workspaceId, session.id, 'RUNNING')

    const r = await api(request, `/api/studio/paper/${session.id}/report`, { workspaceId: s.workspaceId })
    expect(r.status, `取模拟报告失败：${JSON.stringify(r.body)}`).toBe(200)
    expect(r.body.session, '报告应包含会话').toBeTruthy()
    const windows = r.body.windows?.windows || {}
    expect(Object.keys(windows).length, '报告应包含观察窗口（7/30 天）').toBeGreaterThan(0)
    // 刚启动的实验，7 天检查点必然未满 —— 平台不得把它当成已完成。
    const sevenDay = windows['7d'] || windows['7']
    if (sevenDay) {
      expect(sevenDay.complete, '刚启动的实验 7 天检查点不应显示为完整').toBe(false)
      expect(sevenDay.hours).toBeLessThan(168)
    } else {
      expect(
        Object.values(windows).some((w: any) => w.complete === false),
        `应有未完成的观察窗口：${JSON.stringify(windows)}`,
      ).toBe(true)
    }

    await paperCommand(request, s.workspaceId, session.id, 'liquidate')
  })

  test('浏览器：启动实验 → 暂停 → 模拟平仓并结束', async ({ page, request }) => {
    const { s, slug } = await readyStrategy(request, 'paper-ui')

    await uiLogin(page, s)
    const sessionId = await uiStartPaper(page, { slug, source: 'demo', cash: 10_000 })
    expect(sessionId).toBeTruthy()

    // 详情页：状态标签、净值卡片、口径横幅。
    await expect(page.locator('.table-pill').first()).toContainText(/运行中|排队中|恢复中/, { timeout: 60_000 })
    await expect(page.getByText('演示行情（时间函数生成，只用于体验流程）')).toBeVisible()

    // 暂停（window.confirm 不需要处理，暂停没有确认框）。
    // 刚启动时可能短暂处于排队 / 恢复中，暂停按钮会禁用；等服务端真正进入 running 再点击。
    const pause = page.getByRole('button', { name: '暂停' })
    await expect(pause).toBeEnabled({ timeout: 60_000 })
    await pause.click()
    await expect(page.locator('.table-pill').first(), '暂停后应显示已暂停').toContainText('已暂停', {
      timeout: 60_000,
    })
    // 暂停后按钮变成“恢复”。
    await expect(page.getByRole('button', { name: '恢复' })).toBeVisible()

    // 模拟平仓并结束：使用应用内确认框（不是 window.confirm）。
    await page.getByRole('button', { name: '模拟平仓并结束' }).click()
    await page.getByRole('alertdialog').getByRole('button', { name: '确定' }).click()

    await expect(page.locator('.table-pill').first(), '平仓后应显示已停止').toContainText('已停止', {
      timeout: 60_000,
    })
    await expect(page.getByText('模拟平仓并结束（模拟成交，不是真实下单）', { exact: false })).toBeVisible()

    // 后端确认 run 已 COMPLETED。
    const run = await waitPaperRunState(request, s.workspaceId, sessionId, 'COMPLETED', 60_000)
    expect(run.state).toBe('COMPLETED')
  })
})
