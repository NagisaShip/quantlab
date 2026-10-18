/**
 * T12 · 端到端测试配置（Playwright）。
 *
 * 设计要点：
 * - **单端口交付**：后端 `quant_agent.api.app` 在 `web/dist` 存在时直接把前端构建产物
 *   当静态站点服务，所以 E2E 只需要起一个进程（API + 前端同源），不需要 Vite dev server，
 *   也就没有代理与跨域问题。
 * - **不用 8000**：本机 8000 常被开发中的 `qagent serve` 占用。默认端口取 8199，
 *   可用 `QAGENT_E2E_PORT` 覆盖；CI 里也是这个值。
 * - **独立数据目录**：`QAGENT_HOME` 指向 `tests/e2e/.tmp/home-<运行编号>`（见 run-env.ts），
 *   每次运行路径都不同，因此既不污染开发者本机 `~/.quant_agent`，也不可能读到上一次的残留。
 * - **独立 worker 进程**：验收要求"回测经 worker"。所以这里 `QAGENT_WORKER=external`，
 *   webServer 数组起两个进程：API（serve）与 worker（`qagent worker`）。
 *   两者共享同一个 `QAGENT_HOME`、同一个 SQLite 文件，走真实的租约 / 认领 / 检查点路径。
 *   API 必须用 `--no-with-worker` 启动，否则 serve 会在显式 worker 就绪前再拉起一个，
 *   两个 worker 争抢同一 run 的租约会让纸面模拟随机不推进。
 * - **失败可排错**：trace 用 `on-first-retry`，失败必留截图与视频，产物落到
 *   `tests/e2e/.artifacts/`（已 gitignore），CI 失败时整体上传。
 *
 * 入口说明：Playwright 默认在 CWD 找 `playwright.config.ts`。仓库根的
 * `playwright.config.ts` 只是把这份转出去（因为 E2E 的 node_modules 在仓库根，
 * 而 spec 在 `tests/e2e/` 下，Node 要从 spec 所在目录向上解析到根 node_modules）。
 */
import { defineConfig, devices } from '@playwright/test'
import path from 'node:path'

import { ARTIFACTS, BASE_URL, E2E_HOME, PORT, REPO_ROOT, SECRET_KEY } from '../tests/e2e/run-env'

const PYTHON = process.env.QAGENT_E2E_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')

const serverEnv: Record<string, string> = {
  ...(process.env as Record<string, string>),
  PYTHONPATH: path.join(REPO_ROOT, 'src'),
  PYTHONUTF8: '1',
  QAGENT_HOME: E2E_HOME,
  QAGENT_SECRET_KEY: SECRET_KEY,
  // 验收要求回测由后台 worker 产出，而不是在请求线程里同步跑完。
  QAGENT_WORKER: 'external',
  QAGENT_BACKGROUND: '0',
  // 实时模拟：让 worker 的常驻驱动推进得快一点，但仍然是真实的 bar 收盘驱动。
  QAGENT_PAPER_TICK_SECONDS: process.env.QAGENT_PAPER_TICK_SECONDS || '2',
}

// 行情口径：不引入新开关。`MarketDataService.get_bars(source="auto")`（默认）会先试真实行情，
// 拉不到（CI 无外网 / 地域受限 / 交易对缺失）时回退合成行情并显式标注来源。
// 两种情况下 E2E 的断言都对"来源标签"做区分，所以离线与联网都能稳定跑。

export { BASE_URL, E2E_HOME, PORT } from '../tests/e2e/run-env'

export default defineConfig({
  testDir: path.join(REPO_ROOT, 'tests', 'e2e'),
  // 每个 spec 都建自己的账号与数据，但共享一个后端进程与一个 SQLite 文件，
  // 串行执行才能保证"冻结 / 平仓 / 对照"这类状态机断言稳定。
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // 单条用例的预算。这里必须给足：实时模拟（paper）的行情是 demo 的
  // `clock_synthetic_bars`，按**墙钟**生成，1 分钟周期下"等一根新收盘 bar"最坏要等满 60 秒；
  // 一条用例里往往要跨 2~3 次 bar 边界（建仓 → 有成交 → 恢复/平仓），
  // 叠加后端的轮询与 worker 心跳，120 秒会贴边超时（实测 04 的平仓用例跑到 2.0m）。
  // 这是**预算**问题，不是断言问题：所以这里抬预算，绝不放宽断言。
  timeout: 300_000,
  expect: { timeout: 30_000 },
  reporter: process.env.CI
    ? [['list'], ['html', { outputFolder: path.join(ARTIFACTS, 'html'), open: 'never' }]]
    : [['list']],
  outputDir: path.join(ARTIFACTS, 'test-results'),

  globalSetup: path.join(REPO_ROOT, 'tests', 'e2e', 'global-setup.ts'),

  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    // 录像需要 ffmpeg 二进制（Playwright 单独下载）。CI 的
    // `npx playwright install --with-deps chromium` 会一并装好；本地若没装，
    // 可以在**没有浏览器录像**的前提下继续跑（截图与 trace 仍在）：
    //   QAGENT_E2E_VIDEO=off
    video: (process.env.QAGENT_E2E_VIDEO || 'retain-on-failure') as 'off' | 'retain-on-failure',
    actionTimeout: 30_000,
    navigationTimeout: 30_000,
    locale: 'zh-CN',
    timezoneId: 'UTC',
    // 后端对 Cookie 认证的写请求要求 X-Requested-With；前端自己会带，
    // 这里再兜一层，保证直接用 request fixture 打接口时也符合 CSRF 契约。
    extraHTTPHeaders: { 'X-Requested-With': 'e2e' },
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // 本地逃生口：Playwright 自带的 chromium 需要从 CDN 下载，某些网络环境下拉不动
        // （本机实测 0 字节落盘）。此时可以借用已安装的 Microsoft Edge：
        //   QAGENT_E2E_BROWSER_CHANNEL=msedge npx playwright test
        // CI 上不设这个变量，走标准 chromium（`npx playwright install --with-deps chromium`）。
        ...(process.env.QAGENT_E2E_BROWSER_CHANNEL
          ? { channel: process.env.QAGENT_E2E_BROWSER_CHANNEL }
          : {}),
      },
    },
  ],

  webServer: [
    {
      command: `"${PYTHON}" -X utf8 -m quant_agent.cli serve --host 127.0.0.1 --port ${PORT} --no-with-worker`,
      cwd: REPO_ROOT,
      env: serverEnv,
      url: `${BASE_URL}/api/health`,
      reuseExistingServer: false,
      timeout: 180_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: `"${PYTHON}" -X utf8 -m quant_agent.cli worker --slots 2 --poll 0.5`,
      cwd: REPO_ROOT,
      env: serverEnv,
      // worker 没有 HTTP 接口，用"进程还活着"作为就绪判断；真正的就绪由
      // /api/runs/workers 的在线心跳断言来验证。
      reuseExistingServer: false,
      timeout: 180_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
})