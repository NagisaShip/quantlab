/** 用当前构建抓取平台最新截图（中文 / 英文界面）。需要 API 与 worker 已启动。 */
const { chromium } = require('@playwright/test')
const fs = require('fs')
const path = require('path')

const BASE = process.env.QA_BASE || 'http://127.0.0.1:8123'
const EMAIL = process.env.QA_EMAIL || 'qa-admin@example.com'
const PASSWORD = process.env.QA_PASSWORD || 'Passw0rd!'
const LANG = process.env.QA_LANG === 'en' ? 'en' : 'zh'
const OUT = process.env.QA_OUT
  ? path.resolve(process.cwd(), process.env.QA_OUT)
  : path.join(process.cwd(), 'docs', 'images', 'latest')
const H = { 'X-Requested-With': 'qa', 'Content-Type': 'application/json' }
const ONLY = (process.env.QA_SHOTS || '').split(',').map((s) => s.trim()).filter(Boolean)
const SEED = process.env.QA_SEED !== '0'

const DSL = {
  dsl_version: 2,
  name: process.env.QA_LANG === 'en' ? 'BTC MA Cross (Demo)' : 'BTC 均线交叉（示例）',
  instrument: 'BTC/USDT',
  timeframe: '1h',
  params: { fast: 12, slow: 48 },
  indicators: {
    ma_fast: { type: 'sma', period: '$fast', source: 'close' },
    ma_slow: { type: 'sma', period: '$slow', source: 'close' },
  },
  entry: { op: 'cross_above', left: { ref: 'ma_fast' }, right: { ref: 'ma_slow' } },
  exit: { op: 'cross_below', left: { ref: 'ma_fast' }, right: { ref: 'ma_slow' } },
  position: { mode: 'weight', value: 0.6, max_weight: 1, qty_step: 0.0001, insufficient_funds: 'reject' },
  risk: { stop_loss: 0.08, take_profit: 0, max_trades_per_day: 0, drawdown_pause: 0 },
}

async function j(req, method, url, data) {
  const r = await req.fetch(url, { method, headers: H, timeout: 120_000, data: data === undefined ? undefined : JSON.stringify(data) })
  const text = await r.text()
  let body = null
  try { body = text ? JSON.parse(text) : null } catch { body = text }
  return { status: r.status(), body }
}

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ executablePath: process.env.QA_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe' })
  const context = await browser.newContext({ baseURL: BASE, viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const req = context.request
  let r = await req.post('/api/auth/register', { headers: H, data: JSON.stringify({ email: EMAIL, password: PASSWORD, name: 'QA Admin' }) })
  if (r.status() !== 200) await req.post('/api/auth/login', { headers: H, data: JSON.stringify({ email: EMAIL, password: PASSWORD }) })
  const me = await (await req.get('/api/auth/me')).json()
  const ws = me.workspace.id
  H['X-Workspace-Id'] = ws

  // 一条示例策略 + 一次回测，避免策略 / 回测截图全是空状态。
  // 只抓研究流程、导师等页面时跳过种子数据，节省一次完整回测等待。
  const needsSeed = SEED && (!ONLY.length || ONLY.some((name) => ['strategies', 'backtests', '15-strategy-editor', '16-backtest-detail'].includes(name.replace(/\.png$/, ''))))
  let slug = null
  let btId = null
  if (needsSeed) {
    try {
      const saved = await j(req, 'POST', '/api/dsl/strategies', { name: DSL.name, spec: DSL })
      slug = saved.body?.strategy?.slug
      if (slug) {
        const bt = await j(req, 'POST', '/api/research/backtests', { slug, wait_seconds: 120, persist: true, source: 'synthetic', bars_limit: 600, warmup_bars: 60 })
        const runId = bt.body?.run_id
        if (runId) {
          for (let i = 0; i < 60; i++) {
            const run = await j(req, 'GET', `/api/runs/${runId}`)
            if (run.body?.run?.state === 'COMPLETED') {
              const res = await j(req, 'GET', `/api/runs/${runId}/result`)
              btId = res.body?.result?.id || res.body?.result?.row?.id || run.body.run.summary?.bt_id || null
              break
            }
            if (['FAILED', 'CANCELED'].includes(run.body?.run?.state)) break
            await new Promise((ok) => setTimeout(ok, 1000))
          }
        }
      }
      console.log('seed strategy', slug, 'backtest', btId)
    } catch (e) {
      console.warn('seed skipped:', e.message)
    }
  }

  await context.addInitScript(([lang]) => localStorage.setItem('qa.lang', lang), [LANG])
  const page = await context.newPage()
  const shots = [
    ['home', 'home', '01-home.png'],
    ['flow', 'flow', '17-research-flow.png'],
    ['mentor', 'mentor', '18-mentor.png'],
    ['lab', 'lab', '02-ai-lab.png'],
    ['strategies', 'strategies', '03-strategies.png'],
    ['factors', 'factors', '04-factors.png'],
    ['backtests', 'backtests', '05-backtests.png'],
    ['validation', 'validation', '06-validation.png'],
    ['paper', 'paper', '07-paper.png'],
    ['microlive', 'microlive', '08-microlive.png'],
    ['realperp', 'realperp', '08b-realperp.png'],
    ['data', 'data', '09-data.png'],
    ['square', 'square', '10-square.png'],
    ['groups', 'groups', '11-groups.png'],
    ['roles', 'roles', '12-roles.png'],
    ['models', 'models', '13-models.png'],
    ['settings', 'settings', '14-settings.png'],
  ]
  if (slug) shots.splice(3, 0, [`editor/${slug}`, 'editor', '15-strategy-editor.png'])
  if (btId) shots.splice(6, 0, [`backtest/${btId}`, 'backtest', '16-backtest-detail.png'])
  const selected = ONLY.length ? shots.filter(([, , file]) => ONLY.includes(file) || ONLY.includes(file.replace(/\.png$/, ''))) : shots
  for (const [route, key, file] of selected) {
    await page.goto(`/#/${route}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(key === 'home' ? 2200 : 1200)
    await page.screenshot({ path: path.join(OUT, file) })
    console.log(file)
  }
  // 未登录介绍页：单独上下文，避免登录 Cookie。
  const anon = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await anon.addInitScript(([lang]) => localStorage.setItem('qa.lang', lang), [LANG])
  const p2 = await anon.newPage()
  await p2.goto(BASE + '/', { waitUntil: 'domcontentloaded' })
  await p2.waitForTimeout(2600)
  await p2.screenshot({ path: path.join(OUT, '00-landing.png') })
  console.log('00-landing.png')
  await anon.close()
  await browser.close()
})().catch((e) => { console.error(e); process.exit(1) })
