/**
 * T12 · 步骤 1/7：注册与登录。
 *
 * 覆盖：
 * - 第一个账号成为部署管理员，并获得独立工作区（平台红线：live_enabled=false）；
 * - 部署 bootstrap 后 `/api/auth/status` 反映真实状态；
 * - 密码强度契约（至少 8 位且含字母与数字）——注意 `AuthService.register` 是
 *   **先校验密码、再校验邀请码**，所以弱密码在受控注册下也是先被密码规则拒绝；
 * - 受控注册：bootstrap 之后无邀请码注册返回 403，凭邀请码注册成功并加入工作区；
 * - 错误密码登录返回 401；
 * - 管理员可以新建独立工作区（各 spec 的隔离手段）；
 * - 浏览器：走一遍真实登录表单进入工作台，退出后回到登录页。
 */
import { expect, test } from '@playwright/test'
import { ADMIN, api, newIsolatedSession, registerOrLogin, uiLogin, uniqueEmail } from './helpers'

test.describe('01 · 注册与登录', () => {
  test('首个账号成为部署管理员并拿到独立工作区', async ({ request }) => {
    // 部署里的第一个账号是 admin@example.com（它在整套 E2E 里只被注册一次）。
    const s = await registerOrLogin(request)

    expect(s.me.user.is_admin, `部署管理员应为 is_admin：${JSON.stringify(s.me.user)}`).toBe(true)
    expect(s.me.workspace, '/api/auth/me 应返回当前工作区').toBeTruthy()
    expect(s.me.workspace.role, '管理员在自己的工作区里角色应为 admin').toBe('admin')
    // 平台红线：没有真实下单路径。
    expect(s.me.live_enabled, '平台必须保持 live_enabled=false（没有真实下单路径）').toBe(false)
  })

  test('注册后 auth/status 显示已 bootstrap 且注册受控', async ({ request }) => {
    await registerOrLogin(request)

    const st = await api(request, '/api/auth/status')
    expect(st.status).toBe(200)
    expect(st.body.bootstrapped, '已有用户后 bootstrapped 应为 true').toBe(true)
    expect(
      st.body.open_registration,
      '默认应为受控注册（除非显式设置 QAGENT_OPEN_REGISTRATION=1）',
    ).toBe(false)
  })

  test('弱密码被拒绝，合格密码配邀请码可以注册', async ({ request }) => {
    const admin = await registerOrLogin(request)

    // 密码规则先于邀请码判定：即使没有邀请码，弱密码也应报"参数无效"而不是"请索取邀请码"。
    const weak = await api(request, '/api/auth/register', {
      method: 'POST',
      data: { email: uniqueEmail('weakpw'), password: 'short', name: '弱密码' },
    })
    expect(weak.status, `弱密码不应被接受：${weak.status} ${JSON.stringify(weak.body)}`).not.toBe(200)
    expect(weak.body.code, '弱密码应是参数错误，而不是权限错误').toBe('INVALID_PARAMETER')

    const lettersOnly = await api(request, '/api/auth/register', {
      method: 'POST',
      data: { email: uniqueEmail('letters'), password: 'onlyletters', name: '纯字母' },
    })
    expect(
      lettersOnly.status,
      `纯字母（无数字）密码不应被接受：${lettersOnly.status} ${JSON.stringify(lettersOnly.body)}`,
    ).not.toBe(200)
    expect(lettersOnly.body.code).toBe('INVALID_PARAMETER')

    // 合格密码 + 管理员发的邀请码 → 注册成功。
    const invite = await api(request, '/api/workspace/invites', {
      method: 'POST',
      workspaceId: admin.workspaceId,
      data: { role: 'member' },
    })
    expect(invite.status, `生成邀请码失败：${JSON.stringify(invite.body)}`).toBe(200)
    const code = invite.body.invite?.code
    expect(code, `邀请码接口应返回明文 code（只显示一次）：${JSON.stringify(invite.body)}`).toBeTruthy()

    const email = uniqueEmail('strongpw')
    const ok = await api(request, '/api/auth/register', {
      method: 'POST',
      data: { email, password: 'Str0ngPass!', name: '合格密码', invite_code: code },
    })
    expect(ok.status, `合格密码 + 邀请码应注册成功：${ok.status} ${JSON.stringify(ok.body)}`).toBe(200)
    expect(ok.body.is_admin, '凭邀请码注册的不是部署管理员').toBe(false)
  })

  test('受控注册：无邀请码被拒；错误密码登录返回 401', async ({ request }) => {
    await registerOrLogin(request)

    const noInvite = await api(request, '/api/auth/register', {
      method: 'POST',
      data: { email: uniqueEmail('noinvite'), password: ADMIN.password, name: '无码' },
    })
    expect(
      noInvite.status,
      `已 bootstrap 的部署里无邀请码注册应失败：${noInvite.status} ${JSON.stringify(noInvite.body)}`,
    ).toBe(403)
    expect(noInvite.body.code).toBe('PERMISSION_DENIED')

    const badLogin = await api(request, '/api/auth/login', {
      method: 'POST',
      data: { email: ADMIN.email, password: 'Wr0ngPass!' },
    })
    expect(badLogin.status, '错误密码登录必须失败').toBe(401)
  })

  test('管理员可以新建独立工作区（各 spec 的隔离手段）', async ({ request }) => {
    const first = await newIsolatedSession(request, 'ws-a')
    const second = await newIsolatedSession(request, 'ws-b')

    expect(first.workspaceId, '两个工作区 id 不应相同').not.toBe(second.workspaceId)
    expect(first.me.workspace.role, '新建工作区里自己应是 admin').toBe('admin')

    // 工作区列表里应能看到两个都存在的成员关系。
    const list = await api(request, '/api/workspaces', { workspaceId: first.workspaceId })
    expect(list.status).toBe(200)
    const ids = (list.body.workspaces || []).map((w: any) => w.id)
    expect(ids, '新建的工作区应出现在成员关系里').toContain(first.workspaceId)
    expect(ids).toContain(second.workspaceId)

    // 跨工作区隔离：runs 是按工作区隔离的（`/api/runs` 走 current_workspace）。
    // 新工作区里不应看到任何历史 run。
    const aRuns = await api(request, '/api/runs', { workspaceId: first.workspaceId })
    const bRuns = await api(request, '/api/runs', { workspaceId: second.workspaceId })
    expect(aRuns.status).toBe(200)
    expect(bRuns.status).toBe(200)
    expect(aRuns.body.items?.length, '新工作区不应有历史 run').toBe(0)
    expect(bRuns.body.items?.length, '新工作区不应有历史 run').toBe(0)

    // 用 A 的工作区头去读 B 工作区里不存在的 run，必须 404 而不是泄漏。
    const cross = await api(request, '/api/runs/does-not-exist', { workspaceId: first.workspaceId })
    expect(cross.status, '不存在的 run 应 404').toBe(404)
  })

  test('浏览器：登录表单可以进入工作台，退出后回到登录页', async ({ page, request }) => {
    // 确保部署已 bootstrap，界面走的是“登录”而不是“初始化管理员”。
    await registerOrLogin(request)

    await uiLogin(page, { email: ADMIN.email, password: ADMIN.password })

    await expect(page.locator('nav[aria-label="主导航"]')).toBeVisible()

    // 退出：回到登录页（邮箱输入框再次出现）。
    await page.getByRole('button', { name: '退出' }).click()
    await expect(page.locator('input[type="email"]')).toBeVisible({ timeout: 30_000 })
  })
})