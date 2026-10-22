#!/usr/bin/env node
/**
 * QuantLab 视觉回归基础检查（本地 / CI 手动运行）。
 *
 * 做什么：
 * - 复用已启动的后端（默认 http://127.0.0.1:8399，可用 QUANTLAB_BASE 或 --base 覆盖）；
 * - 用 Playwright 启动系统 Chrome（channel: chrome），打开 #/、#/approvals、#/lab；
 * - 检查每个页面的 pageerror 与横向溢出（documentElement/body scrollWidth > innerWidth）；
 * - 截图写到 .tmp/visual（默认），并生成 report.json（含错误、溢出元素、截图相对路径）。
 *
 * 不做：不自动安装浏览器、不自动启动后端、不修改业务代码、不提交截图。
 *
 * 用法：node scripts/visual_check.mjs [选项]
 */
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import process from 'node:process'
import { compareScreenshot } from './visual_diff.mjs'

const DEFAULT_BASE = process.env.QUANTLAB_BASE || 'http://127.0.0.1:8399'
const DEFAULT_OUT = process.env.QUANTLAB_VISUAL_OUT || path.join('.tmp', 'visual')
const DEFAULT_PAGES = 'home,approvals,lab,context-pack'
const PAGES = {
  home: { route: '#/', title: '首页' },
  approvals: { route: '#/approvals', title: '待审批' },
  lab: { route: '#/lab', title: 'AI 研究室' },
  'context-pack': { route: '#/context-pack', title: '上下文包' },
  mcp: { route: '#/settings/mcp', title: 'MCP 服务' },
}
const OVERFLOW_TOLERANCE = 2

function usage() {
  console.log(`QuantLab 视觉回归基础检查

用法:
  node scripts/visual_check.mjs [选项]

选项:
  -h, --help               显示本帮助并退出（0）
  --base <url>             后端地址，默认 ${DEFAULT_BASE}（也可用环境变量 QUANTLAB_BASE）
  --out <dir>              截图与 report.json 输出目录，默认 ${DEFAULT_OUT}
  --pages <keys>           逗号分隔的页面，默认 ${DEFAULT_PAGES}；可选: ${Object.keys(PAGES).join(' / ')}
  --channel <name>         Playwright 浏览器通道，默认 chrome（系统 Chrome）；CI 可用 chromium
  --width <px>             视口宽度，默认 1440
  --height <px>            视口高度，默认 900
  --timeout <ms>           页面与后端探测超时，默认 20000
  --headed                 显示浏览器窗口（默认无头）
  --no-login               不尝试注册/登录（只抓匿名页面）
  --allow-overflow        横向溢出只记录、不判失败（默认溢出即失败）
  --baseline <dir>         与此目录的基准截图做像素比较；缺失基准视为失败
  --update-baseline        显式更新基准（须指定 --baseline）
  --max-diff-ratio <n>     允许变化的像素比例，默认 0.001

环境变量:
  QUANTLAB_BASE            同 --base
  QUANTLAB_VISUAL_OUT      同 --out
  QUANTLAB_VISUAL_EMAIL    登录用账号，默认 qa-admin@example.com
  QUANTLAB_VISUAL_PASSWORD 登录用密码，默认 Passw0rd!

退出码:
  0  全部页面通过
  1  后端不可用、浏览器缺失、页面 pageerror 或横向溢出
  2  参数错误

示例:
  node scripts/visual_check.mjs
  node scripts/visual_check.mjs --base http://127.0.0.1:8399 --out .tmp/visual
  QUANTLAB_BASE=http://127.0.0.1:8123 node scripts/visual_check.mjs --headed
  node scripts/visual_check.mjs --channel chromium   # CI 已 npx playwright install chromium

输出:
  <out>/<key>-<width>x<height>.png  每个页面一张截图
  <out>/report.json                 页面错误、横向溢出、截图路径与总结
`)
}

function fail(message) {
  console.error(`✖ ${message}`)
  process.exit(1)
}

function parseInteger(value, fallback, name, min = 1) {
  if (value === undefined) return fallback
  const number = Number.parseInt(String(value), 10)
  if (!Number.isFinite(number) || number < min) {
    console.error(`✖ ${name} 需要 >= ${min} 的整数，收到：${value}`)
    process.exit(2)
  }
  return number
}

function parseArgs(argv) {
  const options = {
    base: DEFAULT_BASE,
    out: DEFAULT_OUT,
    pages: DEFAULT_PAGES.split(',').map((item) => item.trim()).filter(Boolean),
    channel: process.env.QUANTLAB_VISUAL_CHANNEL || 'chrome',
    width: 1440,
    height: 900,
    timeout: 20000,
    headed: false,
    login: true,
    allowOverflow: false,
    help: false,
    baseline: '',
    updateBaseline: false,
    maxDiffRatio: 0.001,
  }
  const requireValue = (index, flag) => {
    const value = argv[index]
    if (value === undefined || value.startsWith('-')) {
      console.error(`✖ ${flag} 需要一个值；使用 --help 查看用法`)
      process.exit(2)
    }
    return value
  }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    const [flag, inline] = arg.includes('=') ? [arg.slice(0, arg.indexOf('=')), arg.slice(arg.indexOf('=') + 1)] : [arg, undefined]
    const value = () => (inline !== undefined ? inline : requireValue(++index, flag))
    switch (flag) {
      case '--baseline':
        options.baseline = value()
        break
      case '--update-baseline':
        options.updateBaseline = true
        break
      case '--max-diff-ratio':
        options.maxDiffRatio = Number(value())
        break
      case '-h':
      case '--help':
        options.help = true
        break
      case '--base':
        options.base = value()
        break
      case '--out':
        options.out = value()
        break
      case '--pages':
        options.pages = value().split(',').map((item) => item.trim()).filter(Boolean)
        break
      case '--channel':
        options.channel = value()
        break
      case '--width':
        options.width = parseInteger(value(), 1440, '--width')
        break
      case '--height':
        options.height = parseInteger(value(), 900, '--height')
        break
      case '--timeout':
        options.timeout = parseInteger(value(), 20000, '--timeout')
        break
      case '--headed':
        options.headed = true
        break
      case '--no-login':
        options.login = false
        break
      case '--allow-overflow':
        options.allowOverflow = true
        break
      default:
        console.error(`✖ 未知参数：${arg}；使用 --help 查看用法`)
        process.exit(2)
    }
  }
  return options
}

function normalizeBase(base) {
  return String(base).trim().replace(/\/+$/, '')
}

function pageUrl(base, route) {
  return `${base}/${route.replace(/^\/+/, '')}`
}

async function checkBackend(base, timeout) {
  // /api/healthz 是快速存活探针；/api/health 会探测交易所连通性（可能几十秒），只作为旧版本兜底。
  for (const endpoint of ['/api/healthz', '/api/health']) {
    const url = `${base}${endpoint}`
    try {
      const response = await fetch(url, {
        signal: AbortSignal.timeout(endpoint === '/api/healthz' ? timeout : Math.min(timeout, 5000)),
        headers: { Accept: 'application/json' },
      })
      if (endpoint === '/api/healthz' && response.status === 404) continue
      const text = await response.text()
      let body = null
      try {
        body = text ? JSON.parse(text) : null
      } catch {
        body = text.slice(0, 300)
      }
      return { ok: response.ok, status: response.status, endpoint, body }
    } catch (error) {
      if (endpoint === '/api/healthz') continue
      return { ok: false, status: 0, endpoint, error: error?.cause?.code || error?.code || error?.message || String(error) }
    }
  }
  return { ok: false, status: 0, endpoint: '/api/healthz', error: '无法连接' }
}

function backendHint(base) {
  const port = new URL(base).port || '8399'
  return [
    `无法连接后端：GET ${base}/api/healthz`,
    '请先在另一个终端启动本地后端（任选一种）：',
    `  python -m quant_agent.cli serve --port ${port}`,
    '  # 或：scripts\\dev.ps1（Windows）/ scripts/dev.sh（macOS/Linux）',
    '  # 需要前端页面时先构建：cd web; npm.cmd run build（后端会单端口交付 web/dist）',
    `然后重试：node scripts/visual_check.mjs --base ${base}`,
    '只做参数/环境自检：node scripts/visual_check.mjs --help',
  ].join('\n')
}

async function ensureSelfIgnoringTempDir(outDir) {
  // 默认输出在仓库根 .tmp 下：放一个自忽略的 .gitignore，避免截图/报告出现在 git status。
  const relative = path.relative(process.cwd(), outDir)
  if (!relative || relative.startsWith('..') || path.isAbsolute(relative)) return
  if (relative.split(path.sep)[0] !== '.tmp') return
  const root = path.join(process.cwd(), '.tmp')
  await mkdir(root, { recursive: true })
  await writeFile(path.join(root, '.gitignore'), '*\n', { flag: 'w' }).catch(() => {})
}

async function loginIfPossible(context, warnings) {
  const headers = { 'X-Requested-With': 'visual-check', 'Content-Type': 'application/json' }
  const me = await context.request.get('/api/auth/me').catch(() => null)
  if (me && me.ok()) {
    const body = await me.json().catch(() => ({}))
    return { mode: 'existing', user: body?.user?.email || body?.user?.username || null }
  }
  const email = process.env.QUANTLAB_VISUAL_EMAIL || 'qa-admin@example.com'
  const password = process.env.QUANTLAB_VISUAL_PASSWORD || 'Passw0rd!'
  const register = await context.request
    .post('/api/auth/register', {
      headers,
      data: JSON.stringify({ email, password, name: '视觉回归检查' }),
    })
    .catch(() => null)
  const login = register && register.ok()
    ? null
    : await context.request
        .post('/api/auth/login', { headers, data: JSON.stringify({ email, password }) })
        .catch(() => null)
  if ((register && register.ok()) || (login && login.ok())) {
    // QA 账号首次进入会弹「首次见面」引导，会遮住页面；把它标成已跳过，保证截图是真实页面。
    await context.request
      .put('/api/onboarding', { headers, data: JSON.stringify({ status: 'skipped' }) })
      .catch(() => {})
    return { mode: register && register.ok() ? 'register' : 'login', user: email }
  }
  warnings.push('未登录：注册/登录均失败（本地桌面模式可忽略；账号模式可设置 QUANTLAB_VISUAL_EMAIL/PASSWORD）')
  return { mode: 'anonymous', user: null }
}

async function collectPageIssues(page) {
  return page.evaluate((tolerance) => {
    const root = document.documentElement
    const body = document.body
    const innerWidth = window.innerWidth
    const scrollWidth = Math.max(root ? root.scrollWidth : 0, body ? body.scrollWidth : 0)
    const overflow = scrollWidth > innerWidth + tolerance
    const offenders = []
    if (overflow) {
      for (const element of document.querySelectorAll('body *')) {
        const rect = element.getBoundingClientRect()
        if (rect.width <= 0 || rect.right <= innerWidth + tolerance) continue
        const style = window.getComputedStyle(element)
        if (style.position === 'fixed' || style.visibility === 'hidden' || style.display === 'none') continue
        if (element.closest('[aria-hidden="true"]')) continue
        offenders.push({
          tag: element.tagName.toLowerCase(),
          class: String(element.className || '').slice(0, 120),
          right: Math.round(rect.right),
          width: Math.round(rect.width),
          text: String(element.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
        })
        if (offenders.length >= 5) break
      }
    }
    return { innerWidth, scrollWidth, overflow, offenders }
  }, OVERFLOW_TOLERANCE)
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  if (options.help) {
    usage()
    return 0
  }
  const base = normalizeBase(options.base)
  if ((options.updateBaseline && !options.baseline) || !Number.isFinite(options.maxDiffRatio) || options.maxDiffRatio < 0 || options.maxDiffRatio > 1) {
    console.error('基准更新须指定 --baseline；--max-diff-ratio 须介于 0 和 1 之间')
    return 2
  }
  let baseUrl
  try {
    baseUrl = new URL(base)
  } catch {
    console.error(`✖ --base 不是合法 URL：${options.base}`)
    return 2
  }
  const unknownPages = options.pages.filter((key) => !PAGES[key])
  if (unknownPages.length) {
    console.error(`✖ 未知页面：${unknownPages.join(', ')}；可选：${Object.keys(PAGES).join(', ')}`)
    return 2
  }
  if (!options.pages.length) {
    console.error(`✖ --pages 不能为空；可选：${Object.keys(PAGES).join(', ')}`)
    return 2
  }

  console.log(`QuantLab 视觉检查 · base=${base} · pages=${options.pages.join(',')} · channel=${options.channel}`)
  const health = await checkBackend(base, options.timeout)
  if (!health.ok) {
    console.error(backendHint(base))
    if (health.error) console.error(`（探测结果：${health.error}${health.status ? ` · HTTP ${health.status}` : ''}）`)
    return 1
  }
  console.log(`后端可用：HTTP ${health.status}（${health.endpoint}）`)

  let chromium
  try {
    ;({ chromium } = await import('playwright'))
  } catch (error) {
    console.error('✖ 未找到 Playwright 依赖。请在仓库根安装 E2E 依赖后重试：npm.cmd ci（Windows）或 npm ci')
    console.error(`（${error?.code || error?.message || error}）`)
    return 1
  }

  let browser
  try {
    browser = await chromium.launch({ channel: options.channel, headless: !options.headed })
  } catch (error) {
    console.error(`✖ 无法启动浏览器（channel: ${options.channel}）`)
    console.error('  • 默认使用系统 Google Chrome；未安装时请安装 Chrome，或改用 --channel chromium')
    console.error('  • 使用 chromium 时先执行：npx playwright install chromium')
    console.error(`（${String(error?.message || error).split('\n')[0]}）`)
    return 1
  }

  const outDir = path.resolve(process.cwd(), options.out)
  await ensureSelfIgnoringTempDir(outDir)
  await mkdir(outDir, { recursive: true })
  const warnings = []
  const pages = []
  let context
  try {
    context = await browser.newContext({
      baseURL: base,
      viewport: { width: options.width, height: options.height },
      deviceScaleFactor: 1,
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      colorScheme: 'light',
      reducedMotion: 'reduce',
    })
    context.setDefaultTimeout(options.timeout)
    context.setDefaultNavigationTimeout(options.timeout)
    const auth = options.login ? await loginIfPossible(context, warnings) : { mode: 'anonymous', user: null }
    const page = await context.newPage()
    for (const key of options.pages) {
      const meta = PAGES[key]
      const url = pageUrl(base, meta.route)
      const pageErrors = []
      const consoleErrors = []
      const onPageError = (error) => pageErrors.push(String(error?.stack || error))
      const onConsole = (message) => {
        if (message.type() === 'error') consoleErrors.push(String(message.text()).slice(0, 500))
      }
      page.on('pageerror', onPageError)
      page.on('console', onConsole)
      console.log(`→ ${meta.title} ${url}`)
      try {
        await page.goto(url, { waitUntil: 'domcontentloaded' })
        // 等工作台真正挂载（.shell 出现在登录/会话校验之后），避免截到“正在验证会话…”加载页。
        if (auth.mode === 'anonymous') {
          // 未登录时就是登录页：不等待工作台，避免每页白等一个超时。
          await page.waitForTimeout(800)
        } else {
          await page
            .waitForSelector(key === 'mcp' ? '.mcp-settings' : '.shell, .set-shell', { state: 'visible', timeout: options.timeout })
        }
        await page.evaluate(() => (document.fonts?.ready ? document.fonts.ready : null)).catch(() => {})
        await page.waitForTimeout(key === 'home' ? 1500 : 900)
        const overflow = await collectPageIssues(page)
        const screenshot = path.join(outDir, `${key}-${options.width}x${options.height}.png`)
        await page.screenshot({ path: screenshot, animations: 'disabled', caret: 'hide' })
        const pixelDiff = options.baseline ? await compareScreenshot(screenshot, path.resolve(options.baseline), { update: options.updateBaseline, maxRatio: options.maxDiffRatio }) : null
        pages.push({
          key,
          title: meta.title,
          url,
          screenshot: path.relative(process.cwd(), screenshot),
          page_errors: pageErrors,
          console_errors: consoleErrors,
          horizontal_overflow: overflow,
          pixel_diff: pixelDiff,
          ok: pageErrors.length === 0 && (options.allowOverflow || !overflow.overflow) && (!pixelDiff || pixelDiff.ok),
        })
      } catch (error) {
        pages.push({
          key,
          title: meta.title,
          url,
          screenshot: null,
          page_errors: [...pageErrors, `导航/截图失败：${String(error?.message || error)}`],
          console_errors: consoleErrors,
          horizontal_overflow: null,
          ok: false,
        })
      } finally {
        page.off('pageerror', onPageError)
        page.off('console', onConsole)
      }
    }
    const failed = pages.filter((item) => !item.ok)
    const report = {
      format: 'quantlab.visual-check/1',
      base,
      channel: options.channel,
      viewport: { width: options.width, height: options.height },
      auth,
      warnings,
      health: { ok: health.ok, status: health.status, endpoint: health.endpoint },
      generated_at: new Date().toISOString(),
      pages,
      summary: {
        pages: pages.length,
        failed: failed.length,
        page_errors: pages.reduce((total, item) => total + item.page_errors.length, 0),
        overflow: pages.filter((item) => item.horizontal_overflow?.overflow).length,
        console_errors: pages.reduce((total, item) => total + item.console_errors.length, 0),
      },
      ok: failed.length === 0,
    }
    const reportPath = path.join(outDir, 'report.json')
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, 'utf8')
    for (const item of pages) {
      if (item.ok) {
        console.log(`  ✓ ${item.key}${item.console_errors.length ? `（${item.console_errors.length} 条 console error，已记录）` : ''}`)
        continue
      }
      console.error(`  ✖ ${item.key}：${item.page_errors.length} 个 pageerror，横向溢出=${item.horizontal_overflow?.overflow ?? '未知'}，像素比较=${item.pixel_diff?.status ?? '未启用'}`)
      for (const message of item.page_errors.slice(0, 3)) console.error(`     pageerror: ${message.split('\n')[0]}`)
      for (const offender of item.horizontal_overflow?.offenders || []) {
        console.error(`     溢出元素: <${offender.tag} class="${offender.class}"> right=${offender.right} width=${offender.width}`)
      }
    }
    for (const warning of warnings) console.warn(`  ! ${warning}`)
    console.log(`截图与报告：${path.relative(process.cwd(), outDir)}（已自忽略，不进入 git）`)
    console.log(`结果：${report.ok ? 'PASS' : 'FAIL'} · pages=${report.summary.pages} failed=${report.summary.failed} pageerrors=${report.summary.page_errors} overflow=${report.summary.overflow}`)
    return report.ok ? 0 : 1
  } finally {
    await context?.close().catch(() => {})
    await browser.close().catch(() => {})
  }
}

main()
  .then((code) => {
    process.exitCode = code
  })
  .catch((error) => {
    console.error(`✖ 视觉检查异常：${error?.stack || error}`)
    process.exitCode = 1
  })
