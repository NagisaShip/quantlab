/**
 * T12 · 步骤 3/7：冻结版本。
 *
 * 覆盖：
 * - 保存即登记**不可改写**版本：同内容复用同一版本，改内容产生新版本 v2；
 * - 版本状态机 VALIDATED → FROZEN，冻结后内容不能再改（改内容只会多出一个新版本）；
 * - 平台红线：未冻结 / 未批准的版本不能进入实时模拟（返回明确错误而不是静默放行）；
 * - 浏览器：版本面板里点“冻结”，标签从“已校验”变成“已冻结”。
 */
import { expect, test } from '@playwright/test'
import {
  api,
  approveStrategy,
  freezeVersion,
  idemKey,
  listVersions,
  maCrossSpec,
  newIsolatedSession,
  saveDsl,
  startPaper,
  uiLogin,
} from './helpers'

test.describe('03 · 冻结版本', () => {
  test('版本内容不可改写：同内容复用，改内容才产生新版本', async ({ request }) => {
    const s = await newIsolatedSession(request, 'freeze')
    const spec = maCrossSpec('冻结用例')

    const v1 = await saveDsl(request, s.workspaceId, spec)
    expect(v1.version.version_no).toBe(1)
    expect(v1.version.status, '未批准过的新版本初始应为已校验').toBe('VALIDATED')

    // 同一内容重复保存 → 复用同一个版本（内容即身份，code_hash 相同）。
    const again = await saveDsl(request, s.workspaceId, spec, { slug: v1.slug })
    expect(again.version.id, '同一份内容重复保存应复用同一个版本').toBe(v1.version.id)
    expect(again.version.code_hash).toBe(v1.version.code_hash)

    // 改一个参数 → 新版本 v2，旧版本仍然可按 id 取回（历史可追溯）。
    const changed = { ...spec, params: { ...spec.params, slow: 21 } }
    const v2 = await saveDsl(request, s.workspaceId, changed, { slug: v1.slug })
    expect(v2.version.version_no, '改内容应产生新版本').toBe(2)
    expect(v2.version.id).not.toBe(v1.version.id)
    expect(v2.version.code_hash).not.toBe(v1.version.code_hash)

    const old = await api(request, `/api/strategy-versions/${v1.version.id}`, { workspaceId: s.workspaceId })
    expect(old.status, '旧版本应仍可按 id 取回').toBe(200)
    expect(old.body.version.code_hash, '旧版本内容必须保持不变').toBe(v1.version.code_hash)

    const all = await listVersions(request, s.workspaceId, v1.slug)
    expect(all.length, '版本列表应包含 v1 与 v2').toBeGreaterThanOrEqual(2)
  })

  test('冻结后状态为 FROZEN，并出现在版本列表里', async ({ request }) => {
    const s = await newIsolatedSession(request, 'freeze2')
    const { slug, version } = await saveDsl(request, s.workspaceId, maCrossSpec('冻结状态'))

    const frozen = await freezeVersion(request, s.workspaceId, version.id)
    expect(frozen.status).toBe('FROZEN')
    expect(frozen.id).toBe(version.id)
    // 冻结是状态推进，内容本身不变（不可改写）。
    expect(frozen.code_hash, '冻结不应改变版本内容').toBe(version.code_hash)

    const versions = await listVersions(request, s.workspaceId, slug)
    const target = versions.find((v: any) => v.id === version.id)
    expect(target, '冻结的版本应仍在版本列表里').toBeTruthy()
    expect(target.status, '版本列表里应显示已冻结').toBe('FROZEN')
  })

  test('平台红线：未批准的策略版本不能进入实时模拟', async ({ request }) => {
    const s = await newIsolatedSession(request, 'freeze-gate')
    const { slug, version } = await saveDsl(request, s.workspaceId, maCrossSpec('未批准'))
    await freezeVersion(request, s.workspaceId, version.id)

    // 已冻结但没有人工批准 → 拒绝（这是红线，不能静默放行）。
    const denied = await api(request, '/api/studio/paper/start', {
      method: 'POST',
      workspaceId: s.workspaceId,
      data: { slug, source: 'demo', exchange: 'binance', initial_cash: 10_000 },
    })
    expect(
      denied.status,
      `未人工批准的版本不应进入实时模拟：${denied.status} ${JSON.stringify(denied.body)}`,
    ).not.toBe(200)

    // 人工批准后可以启动。
    await approveStrategy(request, s.workspaceId, slug)
    const session = await startPaper(request, s.workspaceId, { slug, source: 'demo' })
    expect(session.id, '批准后应能启动实时模拟').toBeTruthy()

    // 收尾：结束这个会话，避免占用 worker 的常驻槽位。
    await api(request, `/api/studio/paper/${session.id}/liquidate`, { method: 'POST', workspaceId: s.workspaceId })
  })

  test('浏览器：版本面板可以点“冻结”，标签从已校验变成已冻结', async ({ page, request }) => {
    const s = await newIsolatedSession(request, 'freeze-ui')
    const { slug } = await saveDsl(request, s.workspaceId, maCrossSpec('界面冻结'))

    await uiLogin(page, s)
    await page.goto(`/#/dsl/${slug}`)

    // 版本面板：标题“版本（内容不可改写）”。
    await expect(page.getByRole('heading', { name: /版本（内容不可改写）/ })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('已校验', { exact: false }).first()).toBeVisible({ timeout: 30_000 })

    // 点“冻结”，等列表刷新为“已冻结”。
    await page.getByRole('button', { name: '冻结' }).first().click()
    await expect(page.getByText('已冻结', { exact: false }).first(), '冻结后应显示已冻结').toBeVisible({
      timeout: 30_000,
    })

    // 后端确认：该版本确实是 FROZEN。
    const versions = await listVersions(request, s.workspaceId, slug)
    expect(
      versions.some((v: any) => v.status === 'FROZEN'),
      `界面点了冻结，后端也应有 FROZEN 版本：${JSON.stringify(versions.map((v: any) => v.status))}`,
    ).toBe(true)
  })

  test('冻结请求是幂等的：重复冻结不报错', async ({ request }) => {
    const s = await newIsolatedSession(request, 'freeze-idem')
    const { version } = await saveDsl(request, s.workspaceId, maCrossSpec('重复冻结'))
    await freezeVersion(request, s.workspaceId, version.id)
    const second = await api(request, `/api/strategy-versions/${version.id}/freeze`, {
      method: 'POST',
      workspaceId: s.workspaceId,
      headers: { 'Idempotency-Key': idemKey('freeze') },
    })
    expect(second.status, `重复冻结应保持成功：${JSON.stringify(second.body)}`).toBe(200)
    expect(second.body.version.status).toBe('FROZEN')
  })
})
