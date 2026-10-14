/**
 * T12 · 步骤 5/7：导入真实成交 CSV。
 *
 * 覆盖：
 * - 交易所导出的 CSV（列名中英文自动识别）导入成功，行数、品种、区间被正确解析；
 * - **全有或全无**：任何一行不合法，整份文件不入库，并逐行报告原因；
 * - 公式注入防护：以 `=` `+` `-` `@` 开头且不是数字的单元格整行拒绝；
 * - 导入记录可列出、可导出、可删除；
 * - 实盘状态：导入成交后 has_live_data 变为 true，且成交计入“真实记录来源”；
 * - 浏览器：在实盘对照页用文件选择器导入一份 CSV，列表出现该文件。
 */
import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { api, fillsCsv, importFills, newIsolatedSession, uiLogin } from './helpers'

const here = path.dirname(fileURLToPath(import.meta.url))

/** 固定的 UTC 时间，让区间断言可复现。 */
const T1 = '2026-09-01 08:00:00'
const T2 = '2026-09-01 09:00:00'
const T3 = '2026-09-01 10:00:00'

test.describe('05 · 导入 CSV', () => {
  test('导入合法的成交 CSV：行数、品种与区间被正确解析', async ({ request }) => {
    const s = await newIsolatedSession(request, 'csv')

    const csv = fillsCsv([
      { time: T1, symbol: 'BTCUSDT', side: 'BUY', price: '60000.5', qty: '0.01', fee: '0.6' },
      { time: T2, symbol: 'BTCUSDT', side: 'SELL', price: '61000', qty: '0.01', fee: '0.61' },
      { time: T3, symbol: 'ETHUSDT', side: 'BUY', price: '3000', qty: '0.5', fee: '1.5' },
    ])

    const imp = await importFills(request, s.workspaceId, 'trades.csv', csv)
    expect(imp.rows, `应解析出 3 行，实际 ${imp.rows}`).toBe(3)
    expect(imp.status, '导入应立即完成').toBe('done')
    expect(imp.kind).toBe('fills')

    // 品种被规范化为 BTC/USDT 这种形式。
    expect(imp.report.instruments?.sort(), `品种应被规范化：${JSON.stringify(imp.report.instruments)}`).toEqual([
      'BTC/USDT',
      'ETH/USDT',
    ])

    // 区间按 UTC 解释。
    expect(imp.report.period, '应给出成交时间区间').toBeTruthy()
    const start = new Date(imp.report.period[0]).toISOString()
    expect(start, `区间起点应按 UTC 解释为 ${T1}`).toBe('2026-09-01T08:00:00.000Z')

    // 明细可以逐行取回，十进制金额精确保存。
    const detail = await api(request, `/api/imports/${imp.id}`, { workspaceId: s.workspaceId })
    expect(detail.status).toBe(200)
    expect(detail.body.rows?.length, '应能取回导入的成交明细').toBeGreaterThanOrEqual(3)
    const btcBuy = detail.body.rows.find((r: any) => r.instrument === 'BTC/USDT' && r.side === 'buy')
    expect(btcBuy, `应能找到 BTC 买入成交：${JSON.stringify(detail.body.rows)}`).toBeTruthy()
    expect(String(btcBuy.price), '金额应按十进制精确保存（不是浮点近似）').toBe('60000.5')
  })

  test('全有或全无：任一行不合法则整份文件不入库', async ({ request }) => {
    const s = await newIsolatedSession(request, 'csv-atomic')

    const before = await api(request, '/api/imports', { workspaceId: s.workspaceId })
    const countBefore = (before.body.items || []).length

    // 第 3 行价格非法 → 整份拒绝。
    const bad = fillsCsv([
      { time: T1, symbol: 'BTCUSDT', side: 'BUY', price: '60000', qty: '0.01' },
      { time: T2, symbol: 'BTCUSDT', side: 'SELL', price: '61000', qty: '0.01' },
      { time: T3, symbol: 'BTCUSDT', side: 'BUY', price: 'not-a-number', qty: '0.01' },
    ])

    const r = await api(request, '/api/imports', {
      method: 'POST',
      workspaceId: s.workspaceId,
      data: { kind: 'fills', filename: 'bad.csv', content: bad, environment: 'live', exchange: 'binance' },
    })
    expect(r.status, `含非法行的文件不应导入成功：${r.status} ${JSON.stringify(r.body)}`).not.toBe(200)
    expect(r.body.details?.errors?.length, `应逐行报告原因：${JSON.stringify(r.body.details)}`).toBeGreaterThan(0)
    expect(r.body.details.errors[0].row, '错误应带行号').toBeGreaterThan(0)

    const after = await api(request, '/api/imports', { workspaceId: s.workspaceId })
    expect(
      (after.body.items || []).length,
      '整份文件被拒绝后，导入记录不应增加（全有或全无）',
    ).toBe(countBefore)
  })

  test('公式注入防护：以 = 开头的非数字单元格整行拒绝', async ({ request }) => {
    const s = await newIsolatedSession(request, 'csv-inject')

    // 直接在价格列放一个公式。
    const injected = [
      'time,symbol,side,price,qty,fee',
      `${T1},BTCUSDT,BUY,=1+1,0.01,0`,
      `${T2},BTCUSDT,SELL,61000,0.01,0`,
    ].join('\n')

    const r = await api(request, '/api/imports', {
      method: 'POST',
      workspaceId: s.workspaceId,
      data: { kind: 'fills', filename: 'inject.csv', content: injected, environment: 'live', exchange: 'binance' },
    })
    expect(
      r.status,
      `含公式注入的文件必须被拒绝：${r.status} ${JSON.stringify(r.body)}`,
    ).not.toBe(200)
    expect(
      JSON.stringify(r.body.details?.errors || r.body),
      '错误里应指出是非法数字 / 公式注入',
    ).toMatch(/不是合法数字|公式|注入|=1\+1/)
  })

  test('导入记录可列出、可导出、可删除', async ({ request }) => {
    const s = await newIsolatedSession(request, 'csv-crud')
    const imp = await importFills(
      request,
      s.workspaceId,
      'crud.csv',
      fillsCsv([{ time: T1, symbol: 'BTCUSDT', side: 'BUY', price: '60000', qty: '0.01' }]),
    )

    const list = await api(request, '/api/imports', { workspaceId: s.workspaceId })
    expect((list.body.items || []).some((i: any) => i.id === imp.id), '导入应出现在列表里').toBe(true)

    // 导出 CSV：公式中和后的文本（此处没有公式，应原样可用）。
    const exported = await api(request, `/api/imports/${imp.id}/export.csv`, { workspaceId: s.workspaceId })
    expect(exported.status, `导出失败：${JSON.stringify(exported.body)}`).toBe(200)
    expect(String(exported.body), '导出内容应包含表头').toMatch(/instrument|symbol|price/m)

    const del = await api(request, `/api/imports/${imp.id}`, { method: 'DELETE', workspaceId: s.workspaceId })
    expect(del.status, `删除失败：${JSON.stringify(del.body)}`).toBe(200)

    // 删除是**软删除**：`LiveService.delete_import` 清掉该次导入的成交/流水/快照，
    // 并把 imports.status 置为 'deleted'，行本身保留（审计需要）。
    // 因此列表接口仍会返回这一行，界面靠 `status === 'done'` 过滤（见 Live.tsx）。
    const after = await api(request, '/api/imports', { workspaceId: s.workspaceId })
    const row = (after.body.items || []).find((i: any) => i.id === imp.id)
    expect(row, '软删除后审计行仍在列表里（供追溯）').toBeTruthy()
    expect(row.status, `删除后状态应变为 deleted，实际 ${row.status}`).toBe('deleted')

    // 关键：该次导入带来的成交数据必须真的没了（不是只改个状态）。
    const detail = await api(request, `/api/imports/${imp.id}`, { workspaceId: s.workspaceId })
    expect(detail.status).toBe(200)
    expect(detail.body.rows?.length, '删除后该次导入的成交明细应为空').toBe(0)

    // 实盘状态也要跟着回落（这笔数据不再计入真实成交）。
    const status = await api(request, '/api/live/status', { workspaceId: s.workspaceId })
    expect(status.body.live_fills, '删除后真实成交数应回到 0').toBe(0)
  })

  test('导入成交后实盘状态从“未接入”变为已接入', async ({ request }) => {
    const s = await newIsolatedSession(request, 'csv-status')

    const before = await api(request, '/api/live/status', { workspaceId: s.workspaceId })
    expect(before.status).toBe(200)
    expect(before.body.has_live_data, '导入前不应有真实成交').toBe(false)
    expect(before.body.message, '无数据时应给出说明文案').toMatch(/未接入真实成交/)

    await importFills(
      request,
      s.workspaceId,
      'status.csv',
      fillsCsv([
        { time: T1, symbol: 'BTCUSDT', side: 'BUY', price: '60000', qty: '0.01' },
        { time: T2, symbol: 'BTCUSDT', side: 'SELL', price: '61000', qty: '0.01' },
      ]),
    )

    const after = await api(request, '/api/live/status', { workspaceId: s.workspaceId })
    expect(after.body.has_live_data, '导入成交后应显示已接入真实成交').toBe(true)
    expect(after.body.live_fills, `真实成交数应为 2，实际 ${after.body.live_fills}`).toBe(2)
    expect(after.body.sources?.length, '应列出真实记录来源').toBeGreaterThan(0)
  })

  test('浏览器：在实盘对照页导入 CSV，导入记录里出现该文件', async ({ page, request }) => {
    const s = await newIsolatedSession(request, 'csv-ui')

    // 先把 CSV 落到磁盘，供浏览器文件选择器使用（用运行目录下的 .tmp，天然隔离）。
    const dir = path.join(here, '.tmp')
    fs.mkdirSync(dir, { recursive: true })
    const file = path.join(dir, `ui-import-${Date.now()}.csv`)
    fs.writeFileSync(
      file,
      fillsCsv([
        { time: T1, symbol: 'BTCUSDT', side: 'BUY', price: '60000', qty: '0.01', fee: '0.6' },
        { time: T2, symbol: 'BTCUSDT', side: 'SELL', price: '61000', qty: '0.01', fee: '0.61' },
      ]),
      'utf-8',
    )

    await uiLogin(page, s)
    await page.goto('/#/live')

    // 未接入真实成交的横幅（红线：不得用模拟曲线补位）。
    await expect(page.getByText('未接入真实成交').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('本页不会用模拟或回测曲线代替实盘结果')).toBeVisible()

    // 选文件并导入。
    await page.locator('input[type="file"]').setInputFiles(file)
    await page.getByRole('button', { name: '校验并导入' }).click()

    const filename = path.basename(file)
    // 文件名会在页面上出现多次（导入记录表格、来源勾选行、成交明细），
    // 用 .first() 避免 strict mode 冲突。
    await expect(page.getByText(filename).first(), '导入记录里应出现该文件').toBeVisible({ timeout: 30_000 })

    // 导入后横幅切换为“已接入的真实记录”。
    await expect(page.getByText('已接入的真实记录')).toBeVisible({ timeout: 30_000 })

    // 后端确认。
    const status = await api(request, '/api/live/status', { workspaceId: s.workspaceId })
    expect(status.body.has_live_data, '界面导入后后端也应显示已接入').toBe(true)
  })
})