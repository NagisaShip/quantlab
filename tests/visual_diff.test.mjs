import { test } from 'node:test'
import assert from 'node:assert/strict'
import { access, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { PNG } from 'pngjs'
import { compareScreenshot } from '../scripts/visual_diff.mjs'

const exists = async (file) => access(file).then(() => true, () => false)

test('pixel comparison detects missing, changed and resized images without replacing baselines', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'quantlab-diff-'))
  try {
    const shot = path.join(root, 'test.png'), baseline = path.join(root, 'baseline')
    const png = new PNG({ width: 20, height: 20 }); png.data.fill(255)
    await writeFile(shot, PNG.sync.write(png))
    const missing = await compareScreenshot(shot, baseline)
    assert.equal(missing.status, 'missing_baseline')
    assert.equal(missing.diff, null)

    await compareScreenshot(shot, baseline, { update: true })
    const matched = await compareScreenshot(shot, baseline)
    assert.equal(matched.status, 'matched')
    assert.equal(matched.changed_pixels, 0)
    assert.equal(matched.total_pixels, 400)
    assert.equal(matched.ratio, 0)
    assert.equal(await exists(shot.replace(/\.png$/, '-diff.png')), true)

    png.data.fill(0)
    await writeFile(shot, PNG.sync.write(png))
    const changed = await compareScreenshot(shot, baseline)
    assert.equal(changed.ok, false)
    assert.equal(changed.changed_pixels, 400)
    assert.equal(changed.ratio, 1)
    assert.equal(changed.status, 'changed')

    const tolerated = await compareScreenshot(shot, baseline, { maxRatio: 1 })
    assert.equal(tolerated.status, 'matched')
    assert.equal(tolerated.maxRatio, 1)

    await writeFile(shot, PNG.sync.write(new PNG({ width: 10, height: 10 })))
    const resized = await compareScreenshot(shot, baseline)
    assert.equal(resized.status, 'size_mismatch')
    assert.deepEqual(resized.actual, { width: 10, height: 10 })
    assert.deepEqual(resized.expected, { width: 20, height: 20 })
    assert.equal(await exists(shot.replace(/\.png$/, '-diff.png')), true)
  } finally { await rm(root, { recursive: true, force: true }) }
})

test('invalid comparison tolerances are rejected instead of silently changing policy', async () => {
  await assert.rejects(() => compareScreenshot('unused.png', 'baseline', { maxRatio: 2 }), /maxRatio/)
  await assert.rejects(() => compareScreenshot('unused.png', 'baseline', { threshold: -1 }), /threshold/)
})
