/** 展示层共用的格式化与下载工具；api.ts / api2.ts 只做兼容再导出。 */

export type NumLike = number | string | null | undefined

export const toNum = (v: NumLike): number | null =>
  v === null || v === undefined || v === '' ? null : typeof v === 'number' ? v : Number(v)

/** 查询串：自动跳过 undefined / null / 空串 / false。 */
export const qs = (o: Record<string, unknown>) =>
  Object.entries(o)
    .filter(([, v]) => v !== undefined && v !== null && v !== '' && v !== false)
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
    .join('&')

export const fmtPct = (v: number | null | undefined, digits = 2, sign = false) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : `${sign && v > 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`

export const fmtNum = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : v.toFixed(digits)

/** 金额展示：接受数字或十进制字符串（服务端 Decimal）。 */
export const fmtMoney = (v: NumLike, digits = 2) => {
  const n = toNum(v)
  return n === null || !Number.isFinite(n)
    ? '—'
    : n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** 数量展示：十进制字符串原样显示（精确），数字保留 6 位有效数字。 */
export const fmtQty = (v: NumLike) =>
  v === null || v === undefined || v === '' ? '—' : typeof v === 'string' ? v : Number(v.toPrecision(6)).toString()

export const fmtTime = (ms: number | null | undefined) => {
  if (!ms) return '—'
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`
}

export const fmtDate = (ms: number | null | undefined) => (ms ? fmtTime(ms).slice(0, 10) : '—')

/** 把秒级时间戳（store 的 created_at）转成相对时间。 */
export const ago = (sec: number | null | undefined) => {
  if (!sec) return ''
  const diff = Date.now() / 1000 - sec
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)} 天前`
  return fmtDate(sec * 1000)
}

export function downloadText(filename: string, text: string, type = 'text/plain;charset=utf-8') {
  const blob = new Blob([text], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
