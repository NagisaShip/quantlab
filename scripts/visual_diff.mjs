import { readFile, writeFile, mkdir } from 'node:fs/promises'
import path from 'node:path'
import { PNG } from 'pngjs'
import pixelmatch from 'pixelmatch'

export async function compareScreenshot(actualPath, baselineDir, { update = false, maxRatio = 0.001 } = {}) {
  const baseline = path.join(baselineDir, path.basename(actualPath))
  const actualBytes = await readFile(actualPath)
  if (update) {
    await mkdir(baselineDir, { recursive: true })
    await writeFile(baseline, actualBytes)
    return { ok: true, status: 'updated', baseline }
  }
  let baselineBytes
  try { baselineBytes = await readFile(baseline) } catch (error) {
    if (error.code !== 'ENOENT') throw error
    return { ok: false, status: 'missing_baseline', baseline }
  }
  const actual = PNG.sync.read(actualBytes), expected = PNG.sync.read(baselineBytes)
  if (actual.width !== expected.width || actual.height !== expected.height) {
    return { ok: false, status: 'size_mismatch', baseline, actual: [actual.width, actual.height], expected: [expected.width, expected.height] }
  }
  const diff = new PNG({ width: actual.width, height: actual.height })
  const pixels = pixelmatch(expected.data, actual.data, diff.data, actual.width, actual.height, { threshold: 0.1 })
  const ratio = pixels / (actual.width * actual.height)
  const diffPath = actualPath.replace(/\.png$/, '-diff.png')
  await writeFile(diffPath, PNG.sync.write(diff))
  return { ok: ratio <= maxRatio, status: ratio <= maxRatio ? 'matched' : 'changed', pixels, ratio, maxRatio, baseline, diff: diffPath }
}
