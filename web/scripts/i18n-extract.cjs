/**
 * 抽取前端里还没有英文译文的中文界面文案（用 TypeScript 编译器精确解析，不靠正则猜）。
 *
 * 用法：node web/scripts/i18n-extract.cjs [输出.json]
 * - 普通字符串、JSX 文本 → 原文（去掉首尾空白），对应 i18n/en.json 的键；
 * - 带 ${…} 的模板字符串 → 正则模板（变量位置写 (.+?)），对应 i18n.en.ts 的 EN_PATTERNS；
 * 已经有译文（词典里有、或能被现有模板匹配）的不输出。
 */
const fs = require('fs')
const path = require('path')
const ts = require(path.join(__dirname, '..', 'node_modules', 'typescript'))

const SRC = path.join(__dirname, '..', 'src')
const CJK = /[一-鿿]/
const files = []
;(function walk(d) {
  for (const f of fs.readdirSync(d)) {
    const p = path.join(d, f)
    if (fs.statSync(p).isDirectory()) walk(p)
    else if (/\.(tsx?|ts)$/.test(f) && !/i18n/.test(f) && !/\.d\.ts$/.test(f)) files.push(p)
  }
})(SRC)

const plain = new Set()
const templates = new Set()
const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

for (const file of files) {
  const text = fs.readFileSync(file, 'utf8')
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith('x') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const visit = (node) => {
    // 不翻译的：import 路径、对象属性名、正则
    if (ts.isImportDeclaration(node) || ts.isRegularExpressionLiteral(node)) return
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      const s = node.text.trim()
      if (CJK.test(s) && s.length <= 400) s.split(/\n/).length > 1 ? s.split('\n').forEach((l) => CJK.test(l) && plain.add(l.trim())) : plain.add(s)
    } else if (ts.isJsxText(node)) {
      const s = node.text.replace(/\s+/g, ' ').trim()
      if (CJK.test(s)) plain.add(s)
    } else if (ts.isTemplateExpression(node)) {
      let pat = esc(node.head.text)
      let static_ = node.head.text
      for (const span of node.templateSpans) { pat += '(.+?)' + esc(span.literal.text); static_ += span.literal.text }
      if (CJK.test(static_) && pat.length <= 400 && !/\n/.test(pat)) templates.add(pat.trim())
      // 模板里的子表达式也可能有中文字面量
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
}

const en = {
  ...JSON.parse(fs.readFileSync(path.join(SRC, 'i18n', 'en.json'), 'utf8')),
  ...JSON.parse(fs.readFileSync(path.join(SRC, 'i18n', 'landing.en.json'), 'utf8')),
}
const patSrc = fs.readFileSync(path.join(SRC, 'i18n.en.ts'), 'utf8')
const known = [...patSrc.matchAll(/\['((?:[^'\\]|\\.)*)',\s*'(?:[^'\\]|\\.)*'\]/g)].map((m) => m[1].replace(/\\\\/g, '\\'))
const regs = known.map((p) => { try { return new RegExp(`^${p}$`, 'u') } catch { return null } }).filter(Boolean)
const has = (s) => en[s] !== undefined || regs.some((r) => r.test(s))

const missingPlain = [...plain].filter((s) => !has(s)).sort()
const knownPat = new Set(known)
const missingTpl = [...templates].filter((p) => !knownPat.has(p)).sort()
const out = { plain: missingPlain, templates: missingTpl }
if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(out, null, 0))
console.log(`plain ${plain.size} (missing ${missingPlain.length}), templates ${templates.size} (missing ${missingTpl.length})`)
