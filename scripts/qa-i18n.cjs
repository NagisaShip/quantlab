/** 用真实浏览器逐页检查英文界面的中文残留，并可选截图。 */
const { chromium } = require('@playwright/test')
const fs = require('fs')
const path = require('path')

const BASE = process.env.QA_BASE || 'http://127.0.0.1:8123'
const EMAIL = process.env.QA_EMAIL || 'qa-admin@example.com'
const PASSWORD = process.env.QA_PASSWORD || 'Passw0rd!'
const OUT = path.join(process.cwd(), 'tests', 'e2e', '.artifacts', 'i18n')
const SHOT = process.env.QA_SHOT === '1'
const LOGGED_OUT = process.env.QA_LOGGED_OUT === '1'
const FRESH = process.env.QA_FRESH === '1'
const PAGES = (process.env.QA_PAGES || 'home,flow,mentor,lab,data,factors,modellib,strategies,portfolio,execution,assemble,backtests,validation,discipline,paper,demo,microlive,realperp,live,accounts,groups,square,issues,lineage,settings,teams,roles,prompts,models,docs,brand,long-running,admin,inbox,board').split(',')

;(async () => {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ executablePath: process.env.QA_CHROME || 'C:/Program Files/Google/Chrome/Application/chrome.exe' })
  const context = await browser.newContext({ baseURL: BASE, viewport: { width: 1440, height: 900 } })
  const req = context.request
  const headers = { 'X-Requested-With': 'qa', 'Content-Type': 'application/json' }
  if (!LOGGED_OUT) {
    let r = await req.post('/api/auth/register', { headers, data: JSON.stringify({ email: EMAIL, password: PASSWORD, name: 'QA Admin' }) })
    if (r.status() !== 200) {
      await req.post('/api/auth/login', { headers, data: JSON.stringify({ email: EMAIL, password: PASSWORD }) })
    }
  }
  let wsId = process.env.QA_WORKSPACE || ''
  if (FRESH) {
    const me = await (await req.get('/api/auth/me')).json()
    const wr = await req.post('/api/workspaces', { headers, data: JSON.stringify({ name: `i18n-empty-${Date.now().toString(36)}` }) })
    wsId = (await wr.json()).id
    void me
  }
  await context.addInitScript(([ws]) => {
    localStorage.setItem('qa.lang', 'en')
    if (ws) localStorage.setItem('qa.workspace', ws)
  }, [wsId])
  const page = await context.newPage()
  const report = {}
  for (const key of (LOGGED_OUT ? ['landing'] : PAGES)) {
    await page.goto(LOGGED_OUT ? '/' : `/#/${key}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(key === 'home' ? 1800 : 900)
    const found = await page.evaluate(() => {
      const CJK = /[\u4e00-\u9fff]/
      const skip = 'script,style,textarea,code,pre,.md > p,.md > ul,.md > ol,.md > blockquote,.md .md-h,.md td,.md th,.md-details,.md-html,.md-alert,.md-footnotes,.qb-claims li > span,.qb-snippet,.qb-note,.cm-editor,.code-editor,[data-no-i18n],[contenteditable="true"]'
      const out = new Map()
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT)
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (n.nodeType === Node.ELEMENT_NODE) {
          for (const a of ['placeholder', 'title', 'aria-label']) {
            const v = n.getAttribute && n.getAttribute(a)
            if (v && CJK.test(v)) out.set(v.trim(), `${a}:${n.tagName.toLowerCase()}.${n.className || ''}`)
          }
        } else {
          const p = n.parentElement
          if (!p || p.closest(skip)) continue
          const s = (n.nodeValue || '').trim()
          if (CJK.test(s)) out.set(s, `${p.tagName.toLowerCase()}.${p.className || ''}`)
        }
      }
      return [...out.entries()].map(([text, where]) => ({ text, where }))
    })
    report[key] = found
    console.log(`${key}: ${found.length}`)
    if (SHOT) await page.screenshot({ path: path.join(OUT, `${key}.png`), fullPage: false })
  }
  fs.writeFileSync(path.join(OUT, 'missing.json'), JSON.stringify(report, null, 2))
  const total = Object.values(report).reduce((n, a) => n + a.length, 0)
  console.log(`total missing visible strings: ${total}`)
  await browser.close()
})().catch((e) => { console.error(e); process.exit(1) })
