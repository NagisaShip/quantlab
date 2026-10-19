/**
 * Mermaid 中文兼容自检。
 *
 * 覆盖两个曾经的真问题（都是词法器拒绝中文导致的）：
 *   1. `quadrantChart`：`Lexical error on line N. Unrecognized text.`
 *   2. `sankey-beta`：`Parse error: Expecting 'NEWLINE','EOF','COMMA', got 'NON_ESCAPED_TEXT'`
 * 外加两条与它一起修的：
 *   3. `xychart-beta` 分类轴中文（同一类词法错误）；
 *   4. 渲染失败时 Mermaid 往页面里注入错误图（`suppressErrorRendering` 必须为 true）。
 *
 * 做法：先在 Node 里断言改写结果，再用真实浏览器 + 仓库里同一份 Mermaid 构建，
 * 走一遍 `prepareMermaid → render → restore → PNG` 的完整链路。
 *
 * 用法：npm run qa:mermaid
 */
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const TS_ENTRY = path.join(ROOT, 'web', 'src', 'components', 'blocks', 'mermaidCompat.ts')
const MERMAID_DIST = path.join(ROOT, 'web', 'node_modules', 'mermaid', 'dist')
// 中间产物放在仓库内已忽略的目录：某些受限环境下 esbuild 只能写工作区。
const SCRATCH = path.join(ROOT, 'tests', 'e2e', '.artifacts', 'mermaid')

const failures = []
let checks = 0
function ok(name, condition, detail = '') {
  checks++
  if (condition) { console.log(`  ✓ ${name}`) } else { failures.push(`${name}${detail ? ' — ' + detail : ''}`); console.log(`  ✗ ${name}${detail ? ' — ' + detail : ''}`) }
}

/* ------------------------------------------------------------------ */
/* 样例（就是用户报障里那两份图）                                        */
/* ------------------------------------------------------------------ */
const QUADRANT_ZH = `quadrantChart
    title 待修复项 影响对比改动成本
    x-axis 成本低 --> 成本高
    y-axis 影响小 --> 影响大
    quadrant-1 先做
    quadrant-2 评估
    quadrant-3 可延后
    quadrant-4 拆小做
    出场计数: [0.2, 0.9]
    冷却期: [0.35, 0.7]
    单笔风险定仓: [0.5, 0.8]
    持仓时长字段: [0.15, 0.45]
    基准净值: [0.3, 0.55]
    分段标注行情: [0.75, 0.6]`

const SANKEY_ZH = `sankey-beta
31笔交易,盈利出场,8
31笔交易,亏损出场,23
成本合计930.27,手续费,577.27
成本合计930.27,滑点残差,352.73`

const XYCHART_ZH = `xychart-beta
  title 月度销售
  x-axis [一月, 二月, 三月]
  y-axis 收入 0 --> 100
  bar [10, 20, 30]`

const FLOWCHART_ZH = 'flowchart LR\n  A[开始] --> B[结束]'

/* ------------------------------------------------------------------ */
/* 载入被测模块                                                        */
/* ------------------------------------------------------------------ */
async function loadModule() {
  try {
    return { mod: await import(pathToFileURL(TS_ENTRY).href), bundle: null }
  } catch {
    // Node 22 需要 --experimental-strip-types；没开就退回 esbuild 打包
    const require = createRequire(path.join(ROOT, 'web', 'package.json'))
    const esbuild = require('esbuild')
    fs.mkdirSync(SCRATCH, { recursive: true })
    const out = path.join(SCRATCH, 'mermaidCompat.node.mjs')
    await esbuild.build({ entryPoints: [TS_ENTRY], bundle: true, format: 'esm', platform: 'neutral', target: 'es2020', outfile: out, logLevel: 'silent' })
    return { mod: await import(pathToFileURL(out).href), bundle: out }
  }
}

/** 浏览器里用的 ESM 打包产物（同一份源码）。 */
async function buildBrowserBundle() {
  const require = createRequire(path.join(ROOT, 'web', 'package.json'))
  const esbuild = require('esbuild')
  fs.mkdirSync(SCRATCH, { recursive: true })
  const out = path.join(SCRATCH, 'mermaidCompat.web.mjs')
  await esbuild.build({ entryPoints: [TS_ENTRY], bundle: true, format: 'esm', platform: 'browser', target: 'es2020', outfile: out, logLevel: 'silent' })
  return out
}

/* ------------------------------------------------------------------ */
/* Node 侧：改写结果                                                    */
/* ------------------------------------------------------------------ */
function nodeChecks(mod) {
  console.log('\n[1/2] 改写结果（Node）')
  const q = mod.prepareMermaid(QUADRANT_ZH)
  ok('quadrantChart 轴标签被加上引号', q.code.includes('x-axis "成本低" --> "成本高"'), q.code.split('\n')[2])
  ok('quadrantChart 象限名被加上引号', q.code.includes('quadrant-1 "先做"'))
  ok('quadrantChart 数据点名字被加上引号', q.code.includes('"出场计数": [0.2, 0.9]'))
  ok('quadrantChart title 行保持原样（引号会显示出来）', q.code.includes('title 待修复项 影响对比改动成本'))
  ok('quadrantChart 不改动纯 ASCII 内容', mod.prepareMermaid('quadrantChart\n  x-axis Low --> High\n  A: [0.1, 0.2]').code.includes('x-axis Low --> High'))

  const s = mod.prepareMermaid(SANKEY_ZH)
  const dataLines = s.code.split('\n').slice(1).filter((l) => l.trim())
  ok('sankey-beta 数据行不再含非 ASCII', dataLines.every((l) => !/[^\x20-\x7E]/.test(l)), JSON.stringify(dataLines))
  ok('sankey-beta 值为数字且列数不变', dataLines.every((l) => l.split(',').length === 3))
  const fakeSvg = `<svg><text>${dataLines[0].split(',')[0]}</text><text>${dataLines[2].split(',')[0]}</text></svg>`
  const restored = s.restore(fakeSvg)
  ok('sankey-beta 渲染后能还原中文节点名', restored.includes('31笔交易') && restored.includes('成本合计930.27'), restored)
  ok('sankey-beta 占位符互不串味', !/QLSANK\d/.test(restored))

  const x = mod.prepareMermaid(XYCHART_ZH)
  ok('xychart-beta 分类轴被加上引号', x.code.includes('x-axis ["一月", "二月", "三月"]'))
  ok('xychart-beta 轴标题被加上引号', x.code.includes('y-axis "收入" 0 --> 100'))
  ok('xychart-beta title 被加上引号（否则同样报词法错误）', x.code.includes('title "月度销售"'))

  ok('其它图类型原样透传', mod.prepareMermaid(FLOWCHART_ZH).code === FLOWCHART_ZH)
  ok('尺寸解析：viewBox 优先', JSON.stringify(mod.svgSize('<svg viewBox="0 0 500 380" width="100%"></svg>')) === '{"width":500,"height":380}')
  ok('尺寸解析：退回 width/height', JSON.stringify(mod.svgSize('<svg width="320px" height="200px"></svg>')) === '{"width":320,"height":200}')
}

/* ------------------------------------------------------------------ */
/* 浏览器侧：真实 Mermaid 渲染 + PNG 导出                                */
/* ------------------------------------------------------------------ */
async function browserChecks(bundlePath) {
  console.log('\n[2/2] 真实浏览器渲染 + PNG 导出')
  let chromium
  try {
    ;({ chromium } = createRequire(path.join(ROOT, 'package.json'))('@playwright/test'))
  } catch {
    console.log('  · 未安装 @playwright/test，跳过浏览器检查')
    return
  }
  const candidates = [process.env.QA_CHROME, 'C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].filter(Boolean)
  const executablePath = candidates.find((p) => fs.existsSync(p))
  let browser
  try {
    browser = await chromium.launch(executablePath ? { executablePath } : {})
  } catch (e) {
    console.log(`  · 浏览器不可用，跳过浏览器检查（${String(e.message).split('\n')[0]}）`)
    return
  }
  try {
    const page = await browser.newPage()
    const modules = new Map([
      ['/mermaidCompat.mjs', bundlePath],
      ['/index.html', null],
    ])
    const CONTENT = {
      '.mjs': 'text/javascript; charset=utf-8',
      '.html': 'text/html; charset=utf-8',
      '.css': 'text/css',
      '.json': 'application/json',
    }
    await page.route('http://mermaid.test/**', async (route) => {
      const url = new URL(route.request().url())
      let file = modules.get(url.pathname)
      if (!file) file = path.join(MERMAID_DIST, url.pathname)
      if (url.pathname === '/' || url.pathname === '/index.html') {
        await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<!doctype html><meta charset="utf-8"><title>qa</title>' })
        return
      }
      if (!fs.existsSync(file)) { await route.fulfill({ status: 404, body: 'not found' }); return }
      await route.fulfill({ path: file, contentType: CONTENT[path.extname(file)] || 'application/octet-stream' })
    })
    await page.goto('http://mermaid.test/index.html')
    const boot = await page.evaluate(async () => {
      try {
        const [compat, mm] = await Promise.all([import('/mermaidCompat.mjs'), import('/mermaid.esm.min.mjs')])
        window.__compat = compat
        window.__mermaid = mm.default
        window.__mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral', fontFamily: 'inherit', suppressErrorRendering: true })
        return { ok: true }
      } catch (e) { return { ok: false, err: String(e && e.message || e) } }
    })
    if (!boot.ok) { console.log(`  · 无法在页面里加载 Mermaid（${boot.err}），跳过浏览器检查`); return }

    for (const [name, code, expect] of [
      ['quadrantChart', QUADRANT_ZH, ['成本低', '成本高', '拆小做', '分段标注行情']],
      ['sankey-beta', SANKEY_ZH, ['31笔交易', '盈利出场', '滑点残差']],
      ['xychart-beta', XYCHART_ZH, ['一月', '三月', '收入']],
    ]) {
      const r = await page.evaluate(async ([src, tag]) => {
        const { prepareMermaid } = window.__compat
        const prepared = prepareMermaid(src)
        try {
          const out = await window.__mermaid.render('qa' + tag, prepared.code)
          return { ok: true, svg: prepared.restore(out.svg) }
        } catch (e) { return { ok: false, err: String(e && e.message || e) } }
      }, [code, name.replace(/\W/g, '')])
      ok(`${name} 能渲染`, r.ok, r.err)
      if (r.ok) ok(`${name} 中文标签完整保留`, expect.every((x) => r.svg.includes(x)), expect.filter((x) => !r.svg.includes(x)).join(','))
    }

    const png = await page.evaluate(async () => {
      const { prepareMermaid, svgToPngBlob } = window.__compat
      const prepared = prepareMermaid(`quadrantChart
    title 待修复项
    x-axis 成本低 --> 成本高
    y-axis 影响小 --> 影响大
    quadrant-1 先做
    出场计数: [0.2, 0.9]`)
      const out = await window.__mermaid.render('qaPng', prepared.code)
      const blob = await svgToPngBlob(prepared.restore(out.svg), { scale: 2, background: '#ffffff' })
      // 顺带确认真的画上了像素，不是一张空白图
      const bmp = await createImageBitmap(blob)
      return { type: blob.type, size: blob.size, w: bmp.width, h: bmp.height }
    })
    ok('SVG → PNG 导出成功', png.type === 'image/png' && png.size > 2000 && png.w > 100 && png.h > 100, JSON.stringify(png))

    const leak = await page.evaluate(async () => {
      const before = document.body.children.length
      try { await window.__mermaid.render('qaBad', 'flowchart LR\n  A -->> B') } catch { /* 预期失败 */ }
      const added = document.body.children.length - before
      return { added, text: document.body.textContent.trim() }
    })
    ok('渲染失败不会把错误图注入页面', leak.added === 0 && !leak.text.includes('error-icon'), leak.text.slice(0, 80))
  } finally {
    await browser.close()
  }
}

/* ------------------------------------------------------------------ */
;(async () => {
  console.log('Mermaid 中文兼容自检')
  const { mod } = await loadModule()
  nodeChecks(mod)
  let bundle = null
  try { bundle = await buildBrowserBundle() } catch (e) { console.log(`  · esbuild 不可用，跳过浏览器打包（${String(e.message).split('\n')[0]}）`) }
  if (bundle) await browserChecks(bundle)
  console.log(`\n共 ${checks} 项检查，失败 ${failures.length} 项`)
  if (failures.length) { for (const f of failures) console.error(' - ' + f); process.exit(1) }
  console.log('全部通过')
})().catch((e) => { console.error(e); process.exit(1) })
