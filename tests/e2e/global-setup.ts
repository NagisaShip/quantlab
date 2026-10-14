/**
 * 每次 E2E 运行前的数据目录准备。
 *
 * 这里**只创建、不删除**。原因：Playwright 是**先起 webServer、再跑 globalSetup** 的，
 * 此时后端已经在 QAGENT_HOME 里建好了 SQLite 与行情缓存。在这里删目录会：
 * - 在 Windows 上撞 EPERM（目录里有进程占用的文件）；
 * - 就算删成功，也会把**正在运行的后端**刚建好的数据库删掉。
 *
 * 隔离性由「数据目录始终带运行编号子目录」保证（见 run-env.ts），
 * 所以不需要删除，也就不存在删除失败或误删运行中数据的问题。
 */
import fs from 'node:fs'
import { ARTIFACTS, E2E_HOME, RUN_ID } from './run-env'

export default async function globalSetup(): Promise<void> {
  fs.mkdirSync(E2E_HOME, { recursive: true })
  fs.mkdirSync(ARTIFACTS, { recursive: true })

  console.log(`[e2e] 运行编号 ${RUN_ID}`)
  console.log(`[e2e] 数据目录 ${E2E_HOME}（本次运行独占，不会读到上一次的残留）`)
}
