/**
 * T12 · 步骤 6/7 与 7/7：启动实时模拟 / 回测作为“模拟侧”，导入真实成交，
 * 生成**实盘对照报告**，并核对归因与未归因。
 *
 * 覆盖：
 * - 对照是一个 kind=compare 的 run，由 worker 执行，产出报告；
 * - 报告给出一对一的归因结果：真实成交 / 模拟成交、匹配方式、置信度、滑点；
 * - **无法归因的真实成交单列且不计入策略结果**（这是卡片明确要求的口径）；
 * - 无真实数据时红线：不展示任何实盘收益曲线；
 * - 浏览器：在实盘对照页勾选来源、点“生成对照”，报告表格渲染出归因明细。
 *
 * 归因匹配方式取决于真实成交与模拟成交在时间/品种/方向/数量上的接近程度。
 * 这里的真实成交是**按模拟成交实际产出的成交回填构造**的，所以匹配必须成功；
 * 另外故意多放一笔无法归因的 ETH 成交，用来断言“不混进策略结果”。
 */
import { expect, test } from '@playwright/test'
import {
  alwaysLongSpec,
  api,
  approveStrategy,
  fillsCsv,
  freezeVersion,
  importFills,
  paperCommand,
  newIsolatedSession,
  saveDsl,
  startPaper,
  uiLogin,
  waitPaperFills,
  waitPaperRunState,
} from './helpers'

/** 把 epoch 毫秒写成 UTC 秒级文本，供 CSV 使用。 */
function utc(ms: number): string {
  return new Date(ms).toISOString().slice(0, 19).replace('T', ' ')
}

/**
 * 造一个“模拟侧已运行且有成交”的实验。
 *
 * 用「永远做多」规则 + 1 分钟周期：第一根被评估的 bar 上就建仓，
 * 不必等随机行情走出一次交叉，对照才有素材。
 */
async function paperWithFills(request: any, prefix: string) {
  const s = await newIsolatedSession(request, prefix)
  const { slug, version } = await saveDsl(request, s.workspaceId, alwaysLongSpec(`对照-${prefix}`, { timeframe: '1m' }))
  await freezeVersion(request, s.workspaceId, version.id)
  await approveStrategy(request, s.workspaceId, slug)

  const session = await startPaper(request, s.workspaceId, { slug, source: 'demo', timeframe: '1m' })
  await waitPaperRunState(request, s.workspaceId, session.id, 'RUNNING')
  const fills = await waitPaperFills(request, s.workspaceId, session.id, 1)

  return { s, slug, sessionId: session.id, fills }
}

test.describe('06 · 实盘对照报告', () => {
  test('无真实数据时：live/status 明确为未接入，且不给出任何实盘收益', async ({ request }) => {
    const s = await newIsolatedSession(request, 'cmp-empty')

    const st = await api(request, '/api/live/status', { workspaceId: s.workspaceId })
    expect(st.status).toBe(200)
    expect(st.body.has_live_data, '没有真实成交时不能显示已有实盘数据').toBe(false)
    expect(st.body.live_fills, '没有真实成交时笔数应为 0').toBe(0)
    expect(st.body.last_live_fill_ts, '没有真实成交时不应有时间戳').toBeNull()
    expect(st.body.sources?.length, '没有真实成交时不应有来源').toBe(0)
    // 红线：没有真实数据就不能展示实盘收益。
    expect(st.body.message, `文案应说明不展示实盘收益：${st.body.message}`).toMatch(
      /未接入真实成交|不展示任何实盘收益/,
    )
  })

  test('对照 run 由 worker 执行，报告给出归因、置信度与滑点', async ({ request }) => {
    const { s, sessionId, fills } = await paperWithFills(request, 'cmp-run')

    // 用模拟成交回填真实成交 CSV（同品种/方向/数量/价格、时间偏移 30 秒），
    // 再加一笔与模拟侧无关的 ETH 成交，用于断言“无法归因”。
    const rows = fills.map((f: any) => ({
      time: utc(Number(f.ts) + 30_000),
      symbol: f.security,
      side: String(f.side).toUpperCase() as 'BUY' | 'SELL',
      price: String(f.price),
      qty: String(f.amount),
      fee: String(f.fee ?? '0'),
    }))
    rows.push({
      time: utc(Number(fills[0].ts) + 60_000),
      symbol: 'ETHUSDT',
      side: 'BUY' as const,
      price: '3000',
      qty: '9.99',
      fee: '1',
    })

    const imp = await importFills(request, s.workspaceId, 'live-vs-sim.csv', fillsCsv(rows))
    expect(imp.rows, `CSV 应包含 ${rows.length} 行`).toBe(rows.length)

    const status = await api(request, '/api/live/status', { workspaceId: s.workspaceId })
    expect(status.body.has_live_data).toBe(true)
    const liveRunIds = (status.body.sources || []).map((x: any) => x.id)
    expect(liveRunIds.length, '应能找到真实记录来源').toBeGreaterThan(0)

    // 生成对照。
    const cmp = await api(request, '/api/comparisons', {
      method: 'POST',
      workspaceId: s.workspaceId,
      data: { sim_run_id: sessionId, live_run_ids: liveRunIds, window_minutes: 120 },
    })
    expect([200, 202], `对照请求应被接受：${cmp.status} ${JSON.stringify(cmp.body)}`).toContain(cmp.status)

    // 对照由 worker 执行（kind=compare 是批处理任务）。
    expect(cmp.body.run_id, '对照应登记一个 run').toBeTruthy()
    const cmpRun = await api(request, `/api/runs/${cmp.body.run_id}`, { workspaceId: s.workspaceId })
    expect(cmpRun.status).toBe(200)
    expect(cmpRun.body.run.kind, '对照 run 的 kind 应为 compare').toBe('compare')

    // 等对照完成（worker 执行）。
    let report = cmp.body.comparison
    if (!report) {
      const deadline = Date.now() + 60_000
      while (Date.now() < deadline && !report) {
        const r = await api(request, `/api/runs/${cmp.body.run_id}`, { workspaceId: s.workspaceId })
        if (['FAILED', 'CANCELED'].includes(r.body.run.state)) {
          throw new Error(`对照 run 失败：${r.body.run.error}`)
        }
        if (r.body.run.state === 'COMPLETED') {
          const cid = r.body.run.summary?.comparison_id
          expect(cid, `对照完成但没有 comparison_id：${JSON.stringify(r.body.run.summary)}`).toBeTruthy()
          const got = await api(request, `/api/comparisons/${cid}`, { workspaceId: s.workspaceId })
          expect(got.status).toBe(200)
          report = got.body.comparison
        } else {
          await new Promise((ok) => setTimeout(ok, 500))
        }
      }
    }
    expect(report, '对照报告应生成').toBeTruthy()

    const sum = report.summary
    // 归因：所有模拟成交对应的真实成交都应被归因。
    expect(sum.attributed, `应归因 ${rows.length - 1} 笔，实际 ${sum.attributed}：${JSON.stringify(sum)}`).toBe(
      rows.length - 1,
    )
    // 无法归因的那笔 ETH 成交单列，且不计入策略结果。
    expect(sum.unattributed, `那笔 ETH 成交应无法归因，实际 ${sum.unattributed}`).toBe(1)
    expect(report.unattributed?.length, 'unattributed 明细应有一笔').toBe(1)
    expect(report.unattributed[0].instrument, '无法归因的应是 ETH 成交').toBe('ETH/USDT')
    // live_fills 是**真实成交总笔数**（已归因 + 未归因），见 attribution.py 的 summary()：
    //   "live_fills": matched_fills + unattr_fills
    // 它与 attributed（归因**行数**）不是一回事，别把两者当同一个口径。
    expect(
      sum.live_fills,
      `摘要里的 live_fills 应是全部真实成交笔数（含未归因那笔）：${JSON.stringify(sum)}`,
    ).toBe(sum.attributed + sum.unattributed)
    // orders 是配对 + 未配对的行数。
    expect(sum.orders, 'orders 应是归因行数 + 未归因行数').toBe(sum.attributed + sum.unattributed)

    // 每对都给出匹配方式与置信度。
    expect(report.pairs.length, '应给出逐笔配对').toBe(sum.attributed)
    for (const p of report.pairs) {
      expect(p.live, '每对都应有真实成交').toBeTruthy()
      expect(p.sim, '每对都应有对应的模拟成交').toBeTruthy()
      expect(typeof p.confidence, '应给出置信度').toBe('number')
      expect(p.confidence, `置信度应在 0~1：${p.confidence}`).toBeGreaterThan(0)
      expect(p.confidence).toBeLessThanOrEqual(1)
      expect(p.method, `应给出匹配方式：${JSON.stringify(p)}`).toBeTruthy()
    }

    // 已实现盈亏只统计已归因成交。
    expect(sum.attributed_result, '应给出已归因部分的盈亏').toBeTruthy()
    expect(report.notes, '报告应带口径说明').toBeDefined()

    console.log(
      `[06] 对照：真实 ${sum.live_fills} 笔，已归因 ${sum.attributed}，未归因 ${sum.unattributed}，` +
        `平均置信度 ${sum.avg_confidence}，方式 ${JSON.stringify(sum.by_method)}`,
    )

    // 报告可以从历史列表里再取回。
    const list = await api(request, '/api/comparisons', { workspaceId: s.workspaceId })
    expect(list.status).toBe(200)
    expect((list.body.items || []).length, '对照应出现在历史里').toBeGreaterThan(0)

    await paperCommand(request, s.workspaceId, sessionId, 'liquidate')
  })

  test('浏览器：勾选来源 → 生成对照 → 报告表格渲染出归因明细', async ({ page, request }) => {
    const s = await newIsolatedSession(request, 'cmp-ui')

    // 准备：一个已冻结+已批准策略，一次实时模拟（产生模拟成交），一份对应的真实成交 CSV。
    const { slug, version } = await saveDsl(
      request,
      s.workspaceId,
      alwaysLongSpec('界面对照', { timeframe: '1m' }),
    )
    await freezeVersion(request, s.workspaceId, version.id)
    await approveStrategy(request, s.workspaceId, slug)
    const started = await api(request, '/api/studio/paper/start', {
      method: 'POST',
      workspaceId: s.workspaceId,
      data: { slug, source: 'demo', exchange: 'binance', initial_cash: 10_000, timeframe: '1m' },
    })
    expect(started.status, `启动模拟失败：${JSON.stringify(started.body)}`).toBe(200)
    const sessionId = started.body.session.id
    await waitPaperRunState(request, s.workspaceId, sessionId, 'RUNNING')

    const fills = await waitPaperFills(request, s.workspaceId, sessionId, 1)
    expect(fills.length, '等待窗口内没有模拟成交，无法做界面对照').toBeGreaterThan(0)

    const rows = fills.map((f: any) => ({
      time: utc(Number(f.ts) + 30_000),
      symbol: f.security,
      side: String(f.side).toUpperCase() as 'BUY' | 'SELL',
      price: String(f.price),
      qty: String(f.amount),
      fee: String(f.fee ?? '0'),
    }))
    const imp = await importFills(request, s.workspaceId, 'ui-live.csv', fillsCsv(rows))
    expect(imp.rows).toBe(rows.length)

    await uiLogin(page, s)
    await page.goto('/#/live')
    await expect(
      page.getByRole('heading', { name: '实盘对照', exact: true, level: 1 }),
    ).toBeVisible({ timeout: 30_000 })

    // 已接入真实记录。
    await expect(page.getByText('已接入的真实记录')).toBeVisible({ timeout: 30_000 })

    // 勾选真实记录来源。
    const sourceChecks = page.locator('.check-row input[type="checkbox"]')
    await expect(sourceChecks.first()).toBeVisible({ timeout: 30_000 })
    await sourceChecks.first().check()

    // 生成对照。
    await page.getByRole('button', { name: '生成对照' }).click()

    // 报告面板出现：已归因 / 真实成交、平均置信度。
    await expect(page.getByRole('heading', { name: /对照报告/ })).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText('已归因 / 真实成交')).toBeVisible()
    await expect(page.getByText('平均置信度')).toBeVisible()

    // 归因明细表要有行。
    const reportTable = page.locator('.panel', { has: page.getByRole('heading', { name: /对照报告/ }) })
    await expect(reportTable.locator('tbody tr').first()).toBeVisible({ timeout: 30_000 })

    await api(request, `/api/studio/paper/${sessionId}/liquidate`, { method: 'POST', workspaceId: s.workspaceId })
  })
})
