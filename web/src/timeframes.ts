/**
 * K 线周期 → 毫秒（与后端 quant_agent/timeframes.py 同一套写法）：数字 + 单位 ms / s / m / h / d / w，
 * 例如 100ms、250ms、1s、5s、1m、4h、1d。不合法返回 undefined。
 */
const UNIT: Record<string, number> = { ms: 1, s: 1_000, m: 60_000, h: 3_600_000, d: 86_400_000, w: 604_800_000 }

export function tfMs(tf: string | undefined | null): number | undefined {
  const m = String(tf ?? '').trim().match(/^(\d{1,6})(ms|s|m|h|d|w)$/)
  return m ? Number(m[1]) * UNIT[m[2]] : undefined
}

/** 像字典一样按周期取毫秒数：TF_MS[tf] ?? 默认值（任意合法写法都能取到） */
export const TF_MS: Record<string, number> = new Proxy({} as Record<string, number>, {
  get: (_t, key) => (typeof key === 'string' ? (tfMs(key) as number) : undefined),
})

export const isSubMinute = (tf: string) => (tfMs(tf) ?? 60_000) < 60_000
