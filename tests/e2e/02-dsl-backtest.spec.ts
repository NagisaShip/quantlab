/**
 * T12 · 步骤 2/7：新建 DSL 策略并回测，**回测结果由后台 worker 产出**。
 *
 * 覆盖：
 * - DSL schema / validate 接口（表单与 JSON 共用一份 schema，服务端是唯一裁判）；
 * - 非法规则必须被拒绝，且给出字段路径（不是笼统 400）；
 * - 保存策略即登记不可改写版本；
 * - 回测登记为 run，由**独立 worker 进程**认领执行（见 assertRanOnExternalWorker）；
 * - 回测结果里的账本试算平衡、指标、信号都在；
 * - 浏览器走一遍 DSL 编辑器：改规则 → 校验通过 → 保存并回测 → 跳转回测详情。
 */
import { expect, test } from '@playwright/test'
import {
  api,
  assertRanOnExternalWorker,
  idemKey,
  maCrossSpec,
  newIsolatedSession,
  saveDsl,
  uiLogin,
  waitRun,
} from './helpers'

test.describe('02 · DSL 新建与回测（经 worker）', () => {
  test('DSL schema 与校验接口是同一份规则契约', async ({ request }) => {
    const s = await newIsolatedSession(request, 'dsl-schema')

    const schema = await api(request, '/api/dsl/schema', { workspaceId: s.workspaceId })
    expect(schema.status, JSON.stringify(schema.body)).toBe(200)
    expect(Object.keys(schema.body.examples || {}).length, 'schema 应带内置示例').toBeGreaterThan(0)
    expect(schema.body.indicators?.length, 'schema 应列出可用指标').toBeGreaterThan(0)

    // 合法规则：ok=true，并给出预热长度与中文摘要。
    const good = await api(request, '/api/dsl/validate', {
      method: 'POST',
      workspaceId: s.workspaceId,
      data: { spec: maCrossSpec('校验通过') },
    })
    expect(good.status).toBe(200)
    expect(good.body.ok, `合法规则应校验通过：${JSON.stringify(good.body)}`).toBe(true)
    expect(good.body.warmup_bars, '校验结果应给出预热 bar 数').toBeGreaterThan(0)
    expect(good.body.code_hash, '校验结果应给出 code_hash（版本身份）').toBeTruthy()

    // 非法规则：ok=false 且 problems 精确到字段路径。
    const bad = await api(request, '/api/dsl/validate', {
      method: 'POST',
      workspaceId: s.workspaceId,
      data: { spec: { ...maCrossSpec('非法'), entry: { op: 'cross_above', left: { ref: '不存在的指标' }, right: { const: 1 } } } },
    })
    expect(bad.status, 'validate 始终返回 200（结果在 ok/problems 里）').toBe(200)
    expect(bad.body.ok, '引用不存在的指标必须校验失败').toBe(false)
    expect(bad.body.problems?.length, 'problems 应给出至少一条字段级问题').toBeGreaterThan(0)
    expect(
      bad.body.problems.some((p: any) => typeof p.path === 'string' && p.path.length > 0),
      `每条问题都应带字段路径：${JSON.stringify(bad.body.problems)}`,
    ).toBe(true)
  })

  test('非法规则不能保存（服务端拒绝并回传字段路径）', async ({ request }) => {
    const s = await newIsolatedSession(request, 'dsl-bad')
    const r = await api(request, '/api/dsl/strategies', {
      method: 'POST',
      workspaceId: s.workspaceId,
      headers: { 'Idempotency-Key': idemKey('dsl-bad') },
      data: { name: '非法规则', spec: { dsl_version: 2, instrument: 'BTC/USDT', timeframe: '1h' } },
    })
    expect(r.status, `缺字段的规则不应保存成功：${r.status} ${JSON.stringify(r.body)}`).not.toBe(200)
    expect(r.body.code, '应按错误契约返回 code').toBeTruthy()
    expect(r.body.details?.problems?.length, '应回传逐项问题').toBeGreaterThan(0)
  })

  test('保存 DSL 策略登记版本，回测由独立 worker 执行并产出真实账本', async ({ request }) => {
    const s = await newIsolatedSession(request, 'dsl-bt')
    const spec = maCrossSpec('E2E 均线交叉')
    const { slug, version } = await saveDsl(request, s.workspaceId, spec)

    expect(version.version_no, '首次保存应是 v1').toBe(1)
    expect(version.code_hash, '版本应带 code_hash').toBeTruthy()

    // 保存即登记版本，且版本内容不可改写（同一内容不重复登记）。
    const again = await saveDsl(request, s.workspaceId, spec, { slug })
    expect(again.version.id, '同一份内容重复保存应复用同一个版本').toBe(version.id)

    // 发起回测：登记 run → worker 认领 → 产出结果。
    const bt = await api(request, '/api/research/backtests', {
      method: 'POST',
      workspaceId: s.workspaceId,
      headers: { 'Idempotency-Key': idemKey('bt') },
      data: { slug, wait_seconds: 0, persist: true },
    })
    expect([200, 202], `回测请求应被接受：${bt.status} ${JSON.stringify(bt.body)}`).toContain(bt.status)
    const runId = bt.body.run_id
    expect(runId, `回测应登记一个 run：${JSON.stringify(bt.body)}`).toBeTruthy()

    // 关键断言：执行者是独立 worker，不是 API 请求线程。
    const owner = await assertRanOnExternalWorker(request, s.workspaceId, runId)

    const run = await waitRun(request, s.workspaceId, runId, { timeoutMs: 120_000 })
    expect(run.state, `回测 run 失败：${run.error}`).toBe('COMPLETED')
    expect(run.attempts, 'run 应被 worker 认领过（attempts>=1）').toBeGreaterThanOrEqual(1)

    const result = await api(request, `/api/runs/${runId}/result`, { workspaceId: s.workspaceId })
    expect(result.status, JSON.stringify(result.body)).toBe(200)
    const body = result.body.result
    expect(body.metrics, '回测结果应包含指标').toBeTruthy()
    expect(body.timestamps?.length, '回测结果应有净值曲线').toBeGreaterThan(0)

    // 账本口径：每一分钱都能逐笔核对 —— 试算平衡必须为 0。
    expect(body.ledger, '回测结果应带账本').toBeTruthy()
    expect(body.ledger.balanced, `账本应平衡：${JSON.stringify(body.ledger.trial_balance)}`).toBe(true)
    const tb = body.ledger.trial_balance || {}
    expect(Object.keys(tb).length, '账本应给出各币种试算平衡').toBeGreaterThan(0)
    for (const [currency, value] of Object.entries(tb)) {
      expect(Number(value), `${currency} 的试算平衡应为 0，实际 ${value}`).toBe(0)
    }
    expect(Number(body.ledger.entries), '账本应有分录').toBeGreaterThan(0)
    expect(body.ledger.final_equity, '账本应给出期末权益').toBeTruthy()

    // 信号页签的数据来自 worker 执行结果。
    const signals = await api(request, `/api/runs/${runId}/signals`, { workspaceId: s.workspaceId })
    expect(signals.status).toBe(200)
    expect(Array.isArray(signals.body.items), '信号接口应返回数组').toBe(true)

    // 数据来源必须被显式标注（真实行情 or 合成行情），不能让合成数据冒充真实行情。
    const datasets = body.datasets || []
    expect(datasets.length, '回测结果应声明数据集来源').toBeGreaterThan(0)
    expect(String(datasets[0].source || ''), `数据集应标注来源：${JSON.stringify(datasets[0])}`).toMatch(
      /exchange|synthetic/,
    )

    // worker 执行者身份回写到测试输出，便于排查。
    console.log(`[02] 回测 run=${runId} 由 worker ${owner} 执行，成交 ${(body.fills || []).length} 次`)
  })

  test('浏览器：DSL 编辑器改规则 → 校验通过 → 保存并回测 → 进入回测详情', async ({ page, request }) => {
    const session = await newIsolatedSession(request, 'dsl-ui')

    await uiLogin(page, session)

    // 从“策略”页进入新建 DSL。导航里没有直达项，用 hash 路由（App.tsx 支持 #/dsl）。
    await page.goto('/#/dsl')
    // 用 exact + level 1 锁定页面标题：`Panel` 的 title 也是 heading（h2/h3），
    // 子串匹配容易连侧栏面板一起命中而报 strict mode violation。
    await expect(
      page.getByRole('heading', { name: '新建规则策略', exact: true, level: 1 }),
    ).toBeVisible({ timeout: 30_000 })

    // 服务端校验通过后右侧面板显示“校验通过”，且“保存并回测”才可用。
    await expect(page.getByText('校验通过', { exact: false }).first()).toBeVisible({ timeout: 30_000 })

    // 改标的与周期，验证表单编辑会同步到 JSON 文本（同一份数据）。
    const instrument = page.locator('.dsl-section').first().locator('input').nth(1)
    await instrument.fill('ETH/USDT')

    await page.getByRole('button', { name: 'JSON 视图' }).click()
    const json = page.locator('textarea.json-view')
    await expect(json).toBeVisible()
    await expect(json, '表单改动应同步进 JSON 视图').toHaveValue(/ETH\/USDT/)

    // 写一个明显非法的规则，服务端应报错（服务端是唯一裁判）。
    await json.fill(JSON.stringify({ dsl_version: 2, instrument: 'ETH/USDT', timeframe: '1h' }, null, 2))
    await expect(
      page.getByText('未通过校验', { exact: false }).first(),
      '非法规则应显示未通过校验',
    ).toBeVisible({ timeout: 30_000 })

    // 切回表单并载入内置示例，恢复成合法规则。
    await page.getByRole('button', { name: '逐项表单' }).click()
    // 「载入示例…」那个 select 不是页面上第一个 select（表单里还有周期、指标类型等），
    // 所以按它独有的占位选项定位，而不是用 .first()。
    const exampleSelect = page.locator('select').filter({ has: page.locator('option', { hasText: '载入示例' }) })
    await expect(exampleSelect, '没有找到“载入示例”下拉').toHaveCount(1)
    const exampleValue = await exampleSelect.locator('option').nth(1).getAttribute('value')
    await exampleSelect.selectOption(exampleValue as string)
    await expect(page.getByText('校验通过', { exact: false }).first()).toBeVisible({ timeout: 30_000 })

    // 保存并回测：前端会等 worker 完成后跳到回测详情。
    // 不再依赖“回测运行中”这段瞬时文案——它只出现在 document.title，页面会直接进入详情。
    await page.getByRole('button', { name: '保存并回测' }).click()

    // 回测详情页：收益概述 + 交易详情/信号页签。
    await expect(page.getByRole('heading', { name: '收益概述' })).toBeVisible({ timeout: 90_000 })
    await expect(page.locator('.overview-grid').first()).toBeVisible()
    await expect(page.getByRole('button', { name: /信号/ })).toBeVisible()
    await expect(page.getByText('回测完成', { exact: false }).first()).toBeVisible()
  })
})
