/** 交易所模拟盘、交易终端、策略包。 */
import { del, get, post, put, request, withWs } from './http'
import type { KBar } from './components/KlineChart'
import type { PaperSessionView } from './api2'

export interface DemoEnv {
  exchange: string
  env: string
  label: string
  realistic: boolean
  note: string
  key_help: string
  host: string
}

export interface DemoConnection {
  id: string
  exchange: string
  env: string
  label: string
  key_mask: string
  status: 'active' | 'error' | 'revoked'
  env_label: string
  realistic: boolean
  env_note: string
  created_at: number
  last_test?: { at: number; ok: boolean; balances?: Record<string, string>; error?: string }
}

export interface OmsOrder {
  client_order_id: string
  security: string
  side: 'buy' | 'sell'
  qty: string
  ref_price: string
  status: string
  message: string
  submitted_at: number
  latency_ms: number
  exchange_order_id: string
  filled_qty: string
  avg_price: string
  fee: string
  fee_currency: string
  slippage_bps: number | null
  reason?: string
}

export interface DemoView {
  connection?: Partial<DemoConnection>
  limits?: { max_order_notional: number; max_orders_per_minute: number; max_daily_loss: number; reconcile_tolerance: number }
  halted?: boolean
  halt_reason?: string
  orders?: OmsOrder[]
  stats?: Record<string, number>
  reconcile?: { at: number; ok: boolean; rows: { asset: string; expected: string; actual: string; diff: string; ok: boolean }[] } | Record<string, never>
  slippage?: { count: number; avg_bps: number | null; max_bps: number | null }
}

export interface FeedStatus {
  mode?: 'ws' | 'rest'
  healthy?: boolean
  lag_ms?: number
  reconnects?: number
  backfilled?: number
  error?: string
  interval_s?: number
  events?: { ts: number; level: string; message: string }[]
}

export type TerminalSession = PaperSessionView & { execution?: 'paper' | 'demo' | 'live'; demo?: DemoView; feed?: 'ws' | 'rest'; feed_status?: FeedStatus }

export interface Terminal {
  session: TerminalSession
  security: string
  securities: string[]
  bars: KBar[]
  bars_error: string
  fills: PaperSessionView['fills']
  orders: OmsOrder[]
  feed: { kind: string; status: FeedStatus }
  live_endpoint: string
}

export interface LiveQuote {
  enabled: boolean
  forming: (KBar & { T: number; closed: boolean }) | null
  healthy?: boolean
  connected?: boolean
  lag_ms?: number
  reconnects?: number
  error?: string
}

export interface PackageCheck { name: string; ok: boolean; detail: string; level: 'error' | 'warning' }
export interface PackageReport {
  manifest: Record<string, unknown> & { name?: string; slug?: string; version?: string; author?: string; securities?: string[]; timeframe?: string; lang?: string; sdk_api?: number; requires?: string[] }
  params: Record<string, unknown>
  spec: string
  lineage: Record<string, unknown> | null
  golden: { hash?: string; fills?: number; final_equity?: string }
  actual: { hash?: string; fills?: number; final_equity?: string } | null
  golden_match: boolean
  code_match: boolean
  importable: boolean
  demo_ready: boolean
  checks: PackageCheck[]
  source_lines: number
}

const binary = { 'Content-Type': 'application/octet-stream' }

export const demo = {
  environments: () => get<{ environments: DemoEnv[] }>('/api/demo/environments'),
  connections: () => get<{ connections: DemoConnection[] }>('/api/demo/connections'),
  connect: (p: { exchange: string; env: string; label?: string; api_key: string; api_secret: string; passphrase?: string }) =>
    post<{ connection: DemoConnection }>('/api/demo/connections', p),
  test: (id: string) => post<{ connection: DemoConnection }>(`/api/demo/connections/${id}/test`),
  revoke: (id: string) => del<{ connection: DemoConnection; note: string }>(`/api/demo/connections/${id}`),
  live: (url: string) => get<LiveQuote>(url),

  terminal: (id: string, security = '', bars = 300) =>
    get<Terminal>(`/api/studio/paper/${id}/terminal?security=${encodeURIComponent(security)}&bars=${bars}`),
  unhalt: (id: string) => post<{ session: TerminalSession }>(`/api/studio/paper/${id}/unhalt`),

  /** 导出：浏览器直接下载（Cookie 会话 + 目标工作区参数） */
  packageUrl: (slug: string) => withWs(`/api/studio/strategies/${slug}/package`),
  inspect: (file: Blob) =>
    request<{ report: PackageReport; source: string }>('/api/studio/packages/inspect', { method: 'POST', body: file, headers: binary }),
  importPackage: (file: Blob, name?: string) =>
    request<{ strategy: { slug: string; name: string }; report: PackageReport }>(
      `/api/studio/packages/import${name ? `?name=${encodeURIComponent(name)}` : ''}`,
      { method: 'POST', body: file, headers: binary },
    ),
  spec: (slug: string) => get<{ spec: string; draft: boolean }>(`/api/studio/strategies/${slug}/spec`),
  saveSpec: (slug: string, spec: string) => put<{ spec: string; draft: boolean }>(`/api/studio/strategies/${slug}/spec`, { spec }),
}

export const ORDER_STATUS: Record<string, [string, string]> = {
  submitted: ['已送出', 'pending'],
  new: ['挂单中', 'pending'],
  partial: ['部分成交', 'pending'],
  filled: ['已成交', ''],
  rejected: ['被拒', 'fail'],
  canceled: ['已撤销', 'pending'],
  unknown: ['状态未知（查单中）', 'pending'],
}
