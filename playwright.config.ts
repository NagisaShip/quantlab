/**
 * 仓库根目录的 Playwright 入口。
 *
 * 为什么需要它：Playwright 默认在**当前工作目录**找 `playwright.config.ts`，
 * 而 spec 在 `tests/e2e/` 下、Node 的模块解析又是从 spec 所在目录逐级向上找
 * `node_modules`。仓库根的 `node_modules` 是 E2E 自己的依赖（见根 `package.json`），
 * 这样 `@playwright/test` 才能从 `tests/e2e/*.spec.ts` 解析到。
 *
 * 真正的配置只有一份：`web/playwright.config.ts`（任务书要求的路径）。
 * 这里只做转出，避免两份配置漂移。
 */
export { default } from './web/playwright.config'
export * from './web/playwright.config'
