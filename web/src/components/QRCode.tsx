/**
 * 零依赖 QR 编码器（byte 模式，纠错等级 M，版本 1–10）。
 * 足够承载站内分享 URL，不依赖外网二维码服务。
 */
import { useEffect, useMemo, useState } from 'react'

type Block = { count: number; data: number; ecc: number }
type VersionSpec = { version: number; blocks: Block[]; align: number[] }

const SPECS: VersionSpec[] = [
  { version: 1, blocks: [{ count: 1, data: 16, ecc: 10 }], align: [] },
  { version: 2, blocks: [{ count: 1, data: 28, ecc: 16 }], align: [6, 18] },
  { version: 3, blocks: [{ count: 1, data: 44, ecc: 26 }], align: [6, 22] },
  { version: 4, blocks: [{ count: 2, data: 32, ecc: 18 }], align: [6, 26] },
  { version: 5, blocks: [{ count: 2, data: 43, ecc: 24 }], align: [6, 30] },
  { version: 6, blocks: [{ count: 4, data: 27, ecc: 16 }], align: [6, 34] },
  { version: 7, blocks: [{ count: 4, data: 31, ecc: 18 }], align: [6, 22, 38] },
  { version: 8, blocks: [{ count: 2, data: 38, ecc: 22 }, { count: 2, data: 39, ecc: 22 }], align: [6, 24, 42] },
  { version: 9, blocks: [{ count: 3, data: 36, ecc: 22 }, { count: 2, data: 37, ecc: 22 }], align: [6, 26, 46] },
  { version: 10, blocks: [{ count: 4, data: 43, ecc: 26 }, { count: 1, data: 44, ecc: 26 }], align: [6, 28, 50] },
]

const GF_EXP = new Uint8Array(512)
const GF_LOG = new Uint8Array(256)
let gfReady = false
function initGF() {
  if (gfReady) return
  let x = 1
  for (let i = 0; i < 255; i++) {
    GF_EXP[i] = x
    GF_LOG[x] = i
    x <<= 1
    if (x & 0x100) x ^= 0x11d
  }
  for (let i = 255; i < 512; i++) GF_EXP[i] = GF_EXP[i - 255]
  gfReady = true
}
function gfMul(a: number, b: number): number {
  if (!a || !b) return 0
  return GF_EXP[GF_LOG[a] + GF_LOG[b]]
}
function rsEcc(data: number[], degree: number): number[] {
  initGF()
  let gen = [1]
  for (let i = 0; i < degree; i++) {
    const next = new Array(gen.length + 1).fill(0)
    for (let j = 0; j < gen.length; j++) {
      next[j] ^= gen[j]
      next[j + 1] ^= gfMul(gen[j], GF_EXP[i])
    }
    gen = next
  }
  const rem = new Array(degree).fill(0)
  for (const value of data) {
    const factor = value ^ rem[0]
    rem.shift()
    rem.push(0)
    if (factor) for (let i = 0; i < degree; i++) rem[i] ^= gfMul(gen[i + 1], factor)
  }
  return rem
}

function dataCapacity(spec: VersionSpec): number {
  const words = spec.blocks.reduce((n, b) => n + b.count * b.data, 0)
  const countBits = spec.version <= 9 ? 8 : 16
  return Math.floor((words * 8 - 4 - countBits) / 8)
}

function encodeData(spec: VersionSpec, payload: Uint8Array): number[] {
  const capacity = spec.blocks.reduce((n, b) => n + b.count * b.data, 0)
  const capacityBits = capacity * 8
  const bits: number[] = []
  const push = (value: number, length: number) => {
    for (let i = length - 1; i >= 0; i--) bits.push((value >> i) & 1)
  }
  push(0b0100, 4)
  push(payload.length, spec.version <= 9 ? 8 : 16)
  for (const b of payload) push(b, 8)
  push(0, Math.min(4, capacityBits - bits.length))
  while (bits.length % 8) bits.push(0)
  const bytes: number[] = []
  for (let i = 0; i < bits.length; i += 8) {
    let b = 0
    for (let j = 0; j < 8; j++) b = (b << 1) | bits[i + j]
    bytes.push(b)
  }
  let pad = true
  while (bytes.length < capacity) {
    bytes.push(pad ? 0xec : 0x11)
    pad = !pad
  }
  return bytes
}

function makeCodewords(spec: VersionSpec, data: number[]): number[] {
  const blocks: { data: number[]; ecc: number[] }[] = []
  let cursor = 0
  for (const group of spec.blocks) {
    for (let n = 0; n < group.count; n++) {
      const part = data.slice(cursor, cursor + group.data)
      cursor += group.data
      blocks.push({ data: part, ecc: rsEcc(part, group.ecc) })
    }
  }
  const out: number[] = []
  const maxData = Math.max(...blocks.map((b) => b.data.length))
  for (let i = 0; i < maxData; i++) for (const b of blocks) if (i < b.data.length) out.push(b.data[i])
  const maxEcc = Math.max(...blocks.map((b) => b.ecc.length))
  for (let i = 0; i < maxEcc; i++) for (const b of blocks) if (i < b.ecc.length) out.push(b.ecc[i])
  return out
}

function bchFormat(mask: number): number {
  const data = mask // 纠错等级 M = 00
  let rem = data
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537)
  return ((data << 10) | rem) ^ 0x5412
}
function bchVersion(version: number): number {
  let rem = version
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25)
  return (version << 12) | rem
}

export function qrMatrix(text: string): boolean[][] {
  const payload = new TextEncoder().encode(text)
  const spec = SPECS.find((s) => dataCapacity(s) >= payload.length)
  if (!spec) throw new Error('二维码内容过长（最多 213 字节）')
  const size = 17 + spec.version * 4
  const modules = Array.from({ length: size }, () => new Array<boolean>(size).fill(false))
  const fn = Array.from({ length: size }, () => new Array<boolean>(size).fill(false))
  const set = (x: number, y: number, dark: boolean) => {
    if (x >= 0 && y >= 0 && x < size && y < size) {
      modules[y][x] = dark
      fn[y][x] = true
    }
  }
  const finder = (cx: number, cy: number) => {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const dist = Math.max(Math.abs(dx), Math.abs(dy))
        set(cx + dx, cy + dy, dist <= 3 && dist !== 2)
      }
    }
  }
  finder(3, 3)
  finder(size - 4, 3)
  finder(3, size - 4)
  for (let i = 8; i < size - 8; i++) {
    set(i, 6, i % 2 === 0)
    set(6, i, i % 2 === 0)
  }
  for (const cy of spec.align) {
    for (const cx of spec.align) {
      if ((cx === 6 && cy === 6) || (cx === 6 && cy === size - 7) || (cx === size - 7 && cy === 6)) continue
      for (let dy = -2; dy <= 2; dy++) {
        for (let dx = -2; dx <= 2; dx++) set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1)
      }
    }
  }
  // 先占位，稍后写入真实格式信息；右下角暗模块固定为黑。
  for (let i = 0; i < 15; i++) {
    const a = i < 6 ? [8, i] : i < 8 ? [8, i + 1] : i < 9 ? [7, 8] : [14 - i, 8]
    set(a[0], a[1], false)
    const b = i < 8 ? [size - 1 - i, 8] : [8, size - 15 + i]
    set(b[0], b[1], false)
  }
  set(8, size - 8, true)
  if (spec.version >= 7) {
    const bits = bchVersion(spec.version)
    for (let i = 0; i < 18; i++) {
      const bit = ((bits >>> i) & 1) === 1
      const a = size - 11 + (i % 3)
      const b = Math.floor(i / 3)
      set(a, b, bit)
      set(b, a, bit)
    }
  }
  const codewords = makeCodewords(spec, encodeData(spec, payload))
  let bitIndex = 0
  let upward = true
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5
    for (let vert = 0; vert < size; vert++) {
      const y = upward ? size - 1 - vert : vert
      for (let j = 0; j < 2; j++) {
        const x = right - j
        if (fn[y][x]) continue
        let dark = false
        if (bitIndex < codewords.length * 8) {
          dark = ((codewords[bitIndex >>> 3] >>> (7 - (bitIndex & 7))) & 1) === 1
          bitIndex++
        }
        if ((x + y) % 2 === 0) dark = !dark
        modules[y][x] = dark
      }
    }
    upward = !upward
  }
  const format = bchFormat(0)
  for (let i = 0; i < 15; i++) {
    const bit = ((format >>> i) & 1) === 1
    // 左上副本（绕开 timing pattern 的第 6 行 / 列）
    if (i < 6) set(8, i, bit)
    else if (i < 8) set(8, i + 1, bit)
    else if (i < 9) set(7, 8, bit)
    else set(14 - i, 8, bit)
    // 右上 + 左下副本
    if (i < 8) set(size - 1 - i, 8, bit)
    else set(8, size - 15 + i, bit)
  }
  set(8, size - 8, true)
  return modules
}

export default function QRCode({ value, size = 176, label }: { value: string; size?: number; label?: string }) {
  const [failed, setFailed] = useState('')
  const matrix = useMemo(() => {
    try { setFailed(''); return qrMatrix(value) } catch (e) { setFailed((e as Error).message); return null }
  }, [value])
  useEffect(() => { if (failed) setFailed('') }, [value])
  if (!matrix) return <div className="qr-fallback" role="img" aria-label={failed || '二维码生成失败'}>{failed || '二维码生成失败'}</div>
  const n = matrix.length
  const quiet = 4
  const path: string[] = []
  matrix.forEach((row, y) => row.forEach((dark, x) => { if (dark) path.push(`M${x + quiet} ${y + quiet}h1v1h-1z`) }))
  return (
    <div className="qr-wrap" style={{ width: size }} title={label}>
      <svg viewBox={`0 0 ${n + quiet * 2} ${n + quiet * 2}`} role="img" aria-label={label || '分享二维码'}>
        <rect width={n + quiet * 2} height={n + quiet * 2} fill="#fff" />
        <path d={path.join('')} fill="#0b1020" shapeRendering="crispEdges" />
      </svg>
    </div>
  )
}
