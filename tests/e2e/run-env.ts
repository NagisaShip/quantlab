/**
 * E2E 运行期环境：端口、数据目录、产物目录。
 *
 * 这个模块被 `web/playwright.config.ts` 与 `tests/e2e/global-setup.ts` **共同 import**，
 * 且在 Playwright 主进程里只求值一次（模块缓存），所以两边拿到的路径必然一致。
 *
 * 为什么用「每次运行一个独立子目录」而不是「固定目录 + 先删后建」：
 * Playwright 是**先起 webServer、再跑 globalSetup** 的。后端一起来就会在 QAGENT_HOME 里建
 * SQLite 与行情缓存；此时在 globalSetup 里删除目录有两个坏结果：
 * - Windows 上 `rmSync` 撞 EPERM（目录里有进程占用的文件）；
 * - 就算删成功，也会把**正在运行的后端**刚建好的数据库删掉。
 * 所以这里改为：数据目录始终带一个运行编号子目录，天然不可能读到上一次的残留，
 * 也就不需要删除任何东西。`QAGENT_E2E_HOME` 被当作**父目录**使用（便于 CI 按存储后端分腿），
 * 真正的工作目录是它的 `<运行编号>` 子目录。
 */
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = path.resolve(here, '..', '..')

/** 本次运行的编号：时间戳 + pid，保证同一台机器上并发/连续运行互不干扰。 */
export const RUN_ID =
  process.env.QAGENT_E2E_RUN_ID ||
  `${new Date().toISOString().replace(/[-:]/g, '').replace(/\..+$/, '')}-${process.pid}`

/** 后端 / 前端同源端口。刻意避开 8000（本机常被占用）。 */
export const PORT = Number(process.env.QAGENT_E2E_PORT || 8199)
export const BASE_URL = `http://127.0.0.1:${PORT}`

/** 数据目录的父目录；CI 里按存储后端分腿时指向不同的父目录。 */
export const E2E_HOME_BASE = process.env.QAGENT_E2E_HOME || path.join(REPO_ROOT, 'tests', 'e2e', '.tmp')

/** 本次运行独占的数据目录；绝不落到 ~/.quant_agent。 */
export const E2E_HOME = path.join(E2E_HOME_BASE, `home-${RUN_ID}`)

/** Playwright 的 trace / 截图 / 报告落地处。 */
export const ARTIFACTS = path.join(REPO_ROOT, 'tests', 'e2e', '.artifacts')

/**
 * 固定主密钥：避免在用户主目录生成密钥文件，也让每次运行可复现。
 * 必须是 **32 字节** 的 urlsafe base64（`SecretBox._load_key` 会校验长度，
 * 长度不对会直接抛 ValueError，表现为后端每个请求都 500）。
 * 这里是 `quantagent-e2e-secret-key-32byt!` 的 base64，解码后恰好 32 字节。
 */
export const SECRET_KEY = process.env.QAGENT_SECRET_KEY || 'cXVhbnRhZ2VudC1lMmUtc2VjcmV0LWtleS0zMmJ5dCE='