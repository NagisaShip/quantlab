import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { PNG } from 'pngjs'
import { compareScreenshot } from '../scripts/visual_diff.mjs'

test('pixel comparison detects missing, changed and resized images without replacing baselines', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'quantlab-diff-'))
  try {
    const shot = path.join(root, 'test.png'), baseline = path.join(root, 'baseline')
    const png = new PNG({ width: 20, height: 20 }); png.data.fill(255)
    await writeFile(shot, PNG.sync.write(png))
    assert.equal((await compareScreenshot(shot, baseline)).status, 'missing_baseline')
    await compareScreenshot(shot, baseline, { update: true })
    assert.equal((await compareScreenshot(shot, baseline)).ratio, 0)
    png.data.fill(0)
    await writeFile(shot, PNG.sync.write(png))
    assert.equal((await compareScreenshot(shot, baseline)).ok, false)
    await writeFile(shot, PNG.sync.write(new PNG({ width: 10, height: 10 })))
    assert.equal((await compareScreenshot(shot, baseline)).status, 'size_mismatch')
  } finally { await rm(root, { recursive: true, force: true }) }
})
