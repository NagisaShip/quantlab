/**
 * 浏览器直连交易所公共行情（只读、无需密钥）。
 *
 * 为什么：关注列表、行情带、数据中心的 K 线是「看」用的、刷新频繁，全部由服务器代为请求时，
 * 一个部署上的所有用户都从同一个服务器 IP 打交易所，很容易被限流甚至封 IP。展示用的行情改由
 * 用户自己的浏览器直接请求（Binance / OKX 的公共接口允许跨域），服务器只负责回测要用的、需要落库审计的数据。
 *
 * - 失败（网络、地域限制、跨域被拦）时返回 null，调用方退回服务器接口；
 * - 同一请求 15 秒内复用结果，多个组件同时要同一组数据只发一次；
 * - 设置里可以关掉（localStorage qa.market.direct = '0'）。
 */
import { tfMs } from './timeframes'

export interface DirectBar { t: number; o: number; h: number; l: number; c: number; v: number; closed: boolean }

const BINANCE_NATIVE = new Set(['1s', '1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w'])
const OKX_BAR: Record<string, string> = { '1m': '1m', '3m': '3m', '5m': '5m', '15m': '15m', '30m': '30m', '1h': '1H', '2h': '2H', '4h': '4H', '6h': '6Hutc', '12h': '12Hutc', '1d': '1Dutc', '1w': '1Wutc' }
const TTL = 15_000
const memo = new Map<string, { at: number; p: Promise<DirectBar[] | null> }>()

export function directEnabled(): boolean {
  try { return localStorage.getItem('qa.market.direct') !== '0' } catch { return true }
}

async function getJson(url: string, ms = 8000): Promise<unknown> {
  const ctl = new AbortController()
  const t = window.setTimeout(() => ctl.abort(), ms)
  try {
    const r = await fetch(url, { signal: ctl.signal, credentials: 'omit', referrerPolicy: 'no-referrer' })
    if (!r.ok) throw new Error(`HTTP ${r.status}`)
    return await r.json()
  } finally {
    window.clearTimeout(t)
  }
}

async function binance(symbol: string, tf: string, limit: number): Promise<DirectBar[] | null> {
  const perp = symbol.toUpperCase().endsWith(':PERP')
  if (!BINANCE_NATIVE.has(tf) || (perp && tf === '1s')) return null
  const raw = symbol.split(':')[0].replace('/', '').toUpperCase()
  const base = perp ? 'https://fapi.binance.com/fapi/v1/klines' : 'https://data-api.binance.vision/api/v3/klines'
  const rows = (await getJson(`${base}?symbol=${raw}&interval=${tf}&limit=${Math.min(limit, perp ? 1500 : 1000)}`)) as unknown[][]
  const now = Date.now()
  return rows.map((r) => ({
    t: Number(r[0]), o: Number(r[1]), h: Number(r[2]), l: Number(r[3]), c: Number(r[4]), v: Number(r[5]),
    closed: Number(r[6]) < now,
  }))
}

async function okx(symbol: string, tf: string, limit: number): Promise<DirectBar[] | null> {
  const bar = OKX_BAR[tf]
  if (!bar) return null
  const inst = symbol.toUpperCase().endsWith(':PERP') ? `${symbol.split(':')[0].replace('/', '-')}-SWAP` : symbol.replace('/', '-')
  const body = (await getJson(`https://www.okx.com/api/v5/market/candles?instId=${inst}&bar=${bar}&limit=${Math.min(limit, 300)}`)) as { data?: string[][] }
  const step = tfMs(tf) ?? 3_600_000
  return (body.data ?? []).map((r) => ({
    t: Number(r[0]), o: Number(r[1]), h: Number(r[2]), l: Number(r[3]), c: Number(r[4]), v: Number(r[5]),
    closed: r[8] === '1' || Number(r[0]) + step < Date.now(),
  })).reverse()
}

/** 只返回已收盘的 K 线（和服务器接口口径一致）；拿不到返回 null。 */
export function directBars(symbol: string, tf: string, limit: number, exchange = 'binance'): Promise<DirectBar[] | null> {
  if (!directEnabled()) return Promise.resolve(null)
  const key = `${exchange}|${symbol}|${tf}|${limit}`
  const hit = memo.get(key)
  if (hit && Date.now() - hit.at < TTL) return hit.p
  const p = (exchange === 'okx' ? okx(symbol, tf, limit + 1) : exchange === 'binance' ? binance(symbol, tf, limit + 1) : Promise.resolve(null))
    .then((bars) => (bars && bars.length ? bars.filter((b) => b.closed).slice(-limit) : null))
    .catch(() => null)
  memo.set(key, { at: Date.now(), p })
  return p
}

/** 浏览器直连成功时的来源标签：仍是交易所真实行情，只是由你的浏览器直接取得 */
export const DIRECT_SOURCE = 'exchange'
