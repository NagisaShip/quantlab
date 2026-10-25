import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { PNG } from 'pngjs'
import pixelmatch from 'pixelmatch'

function diffPathFor(actualPath) {
  return `${actualPath.replace(/\.png$/i, '')}-diff.png`
}

function dimensions(image) {
  return { width: image.width, height: image.height }
}

function sideBySide(expected, actual) {
  const gap = 16
  const combined = new PNG({ width: expected.width + gap + actual.width, height: Math.max(expected.height, actual.height) })
  combined.data.fill(255)
  PNG.bitblt(expected, combined, 0, 0, expected.width, expected.height, 0, 0)
  PNG.bitblt(actual, combined, 0, 0, actual.width, actual.height, expected.width + gap, 0)
  return combined
}

/** Compare a screenshot with a baseline and always leave an actionable diff image. */
export async function compareScreenshot(actualPath, baselineDir, { update = false, maxRatio = 0.001, threshold = 0.1 } = {}) {
  if (!Number.isFinite(maxRatio) || maxRatio < 0 || maxRatio > 1) throw new Error('maxRatio must be between 0 and 1')
  if (!Number.isFinite(threshold) || threshold < 0 || threshold > 1) throw new Error('threshold must be between 0 and 1')
  const baseline = path.join(baselineDir, path.basename(actualPath))
  const actualBytes = await readFile(actualPath)
  const diff = diffPathFor(actualPath)
  if (update) {
    await mkdir(baselineDir, { recursive: true })
    await writeFile(baseline, actualBytes)
    return { ok: true, status: 'updated', baseline, diff: null }
  }
  let baselineBytes
  try { baselineBytes = await readFile(baseline) } catch (error) {
    if (error.code === 'ENOENT') return { ok: false, status: 'missing_baseline', baseline, diff: null }
    throw error
  }
  const actual = PNG.sync.read(actualBytes)
  const expected = PNG.sync.read(baselineBytes)
  if (actual.width !== expected.width || actual.height !== expected.height) {
    await writeFile(diff, PNG.sync.write(sideBySide(expected, actual)))
    return {
      ok: false,
      status: 'size_mismatch',
      baseline,
      diff,
      expected: dimensions(expected),
      actual: dimensions(actual),
      changed_pixels: null,
      total_pixels: actual.width * actual.height,
      ratio: null,
      maxRatio,
      threshold,
    }
  }
  const output = new PNG({ width: actual.width, height: actual.height })
  const pixels = pixelmatch(expected.data, actual.data, output.data, actual.width, actual.height, { threshold })
  const total = actual.width * actual.height
  const ratio = pixels / total
  await writeFile(diff, PNG.sync.write(output))
  return {
    ok: ratio <= maxRatio,
    status: ratio <= maxRatio ? 'matched' : 'changed',
    baseline,
    diff,
    expected: dimensions(expected),
    actual: dimensions(actual),
    changed_pixels: pixels,
    total_pixels: total,
    ratio,
    maxRatio,
    threshold,
  }
}
