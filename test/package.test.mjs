/**
 * 安装契约测试：dsh-quantlab 必须「添加插件（Git / 压缩包 / 本地路径）装完即可运行」。
 *
 * 断言的是一条产品契约 —— DSH 插件页把 spec 交给 pnpm 安装，pnpm 要求：
 *   · 仓库根目录直接有 package.json（分发分支/独立仓库解决，脚本见 scripts/publish-dsh-plugin.mjs）
 *   · 没有 prepare/prepack 构建钩子（有的话 pnpm 会拦住要求 allowBuilds）
 *   · 运行所需文件都在 package.json 的 files 里（tarball 才不丢文件）
 *   · dsh.bundle.patch / dsh.client 元数据齐全（宿主才会加载插件、注入浏览器半端）
 *
 * 真实安装（`dsh plugin add` + 启动 DSH + Playwright）由 scripts/verify-dsh-plugin.mjs
 * 的 --install-spec 完成；这里做静态契约断言，跑得快、离线可用。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const pluginDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')

/** 安装后必须存在、缺一个插件就跑不起来的文件。 */
const REQUIRED_FILES = [
  'package.json',
  'lib/index.js',
  'lib/backend.js',
  'lib/llm-bridge.js',
  'lib/tools.js',
  'client.js',
  'icon.png',
  'cordis.patch.yml',
  'config.defaults.json',
]

const readManifest = () => JSON.parse(readFileSync(join(pluginDir, 'package.json'), 'utf8'))

test('运行所需文件都在仓库里（纯源码，无需构建）', () => {
  for (const relative of REQUIRED_FILES) {
    const file = join(pluginDir, relative)
    assert.ok(existsSync(file), `缺 ${relative}`)
    assert.ok(statSync(file).size > 0, `${relative} 是空文件`)
  }
})

test('package.json：main / type / dsh 元数据满足 DSH 插件加载', () => {
  const manifest = readManifest()
  assert.equal(manifest.name, 'dsh-quantlab')
  assert.match(manifest.version, /^\d+\.\d+\.\d+/)
  assert.equal(manifest.type, 'module')
  assert.equal(manifest.main, 'lib/index.js')

  assert.ok(manifest.dsh, '缺 dsh 元数据')
  assert.equal(manifest.dsh.bundle.patch, './cordis.patch.yml')
  assert.equal(manifest.dsh.client.platform, 'web')
  assert.ok(manifest.dsh.client.inject.includes('slots'), 'dsh.client.inject 缺 slots')
  assert.ok(manifest.dsh.client.external.includes('react'), 'dsh.client.external 缺 react')

  // exports 让 DSH/客户端能按子路径取到 bundle patch 与 client
  assert.equal(manifest.exports['.'], './lib/index.js')
  assert.equal(manifest.exports['./client'], './client.js')
  assert.equal(manifest.exports['./cordis.patch.yml'], './cordis.patch.yml')
})

test('package.json：没有 prepare/prepack 构建钩子（Git 安装不会被 pnpm 拦）', () => {
  const manifest = readManifest()
  const scripts = manifest.scripts || {}
  for (const hook of ['prepare', 'prepack', 'prepublishOnly', 'install', 'postinstall']) {
    assert.equal(scripts[hook], undefined, `package.json 不该有 ${hook}（pnpm 从 Git 安装时会要求 allowBuilds）`)
  }
})

test('package.json：files 覆盖全部运行文件（tarball 不丢东西）', () => {
  const manifest = readManifest()
  const files = manifest.files || []
  const covers = (entry, required) => {
    let cleaned = entry.replace(/^\.\//, '').replace(/\/+$/, '')
    if (cleaned === required) return true
    if (cleaned.includes('*')) {
      const prefix = cleaned.slice(0, cleaned.indexOf('*')).replace(/\/+$/, '')
      return prefix !== '' && required.startsWith(prefix + '/')
    }
    return required.startsWith(cleaned + '/')
  }
  for (const required of REQUIRED_FILES) {
    if (required === 'package.json') continue
    assert.ok(files.some((entry) => covers(entry, required)), `files 没包含 ${required}`)
  }
  // icon 两个格式都发（DSH 插件页优先 png）
  assert.ok(files.some((entry) => /icon\.png$/.test(entry)), 'files 缺 icon.png')
})

test('cordis.patch.yml：插入的插件 name 就是包名', () => {
  const patch = readFileSync(join(pluginDir, 'cordis.patch.yml'), 'utf8')
  assert.match(patch, /-\s*insert:/)
  assert.match(patch, /name:\s*dsh-quantlab\b/)
  assert.match(patch, /id:\s*quantlab\b/)
})

test('config.defaults.json：字段与 lib/index.js 的默认值一致', () => {
  const defaults = JSON.parse(readFileSync(join(pluginDir, 'config.defaults.json'), 'utf8'))
  for (const key of ['projectDir', 'python', 'srcDir', 'autoStart', 'llmBridge', 'providerName', 'model', 'pollMs']) {
    assert.ok(Object.hasOwn(defaults, key), `config.defaults.json 缺 ${key}`)
  }
  assert.equal(defaults.python, 'python')
  assert.equal(defaults.autoStart, true)
  assert.equal(defaults.llmBridge, true)
  assert.equal(defaults.providerName, 'DSH · Harness')
})

test('icon.png 是真的 PNG（插件页能显示图标）', () => {
  const icon = readFileSync(join(pluginDir, 'icon.png'))
  assert.deepEqual([...icon.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], 'icon.png 不是 PNG 文件头')
})

test('peerDependencies：@deepseek-ai/dsh-tools 只是可选 peer（运行时由 DSH 提供）', () => {
  const manifest = readManifest()
  assert.equal(manifest.peerDependencies['@deepseek-ai/dsh-tools'], '*')
  assert.equal(manifest.peerDependenciesMeta['@deepseek-ai/dsh-tools'].optional, true)
  // 不要把它们写成 dependencies：否则插件会自带一份、和宿主里的重复
  assert.equal((manifest.dependencies || {})['@deepseek-ai/dsh-tools'], undefined)
})
