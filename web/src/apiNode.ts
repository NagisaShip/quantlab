import { del, get, post } from './http'

export interface NodeHealth {
  now: number
  workers_alive: number
  workers: { id: string; host: string; last_seen: number; alive: boolean }[]
  stale_runs: { id: string; title: string; silent_seconds: number }[]
  clock: { exchange: string; offset_ms: number; rtt_ms: number | null; level: 'ok' | 'warn' | 'bad'; measured_at: number }[]
  rate_limits: { exchange: string; rate_per_s: number; paused_s: number; hits_429: number; waited_s: number }[]
}
export interface BoardSession {
  id: string; strategy: string; execution: string; state: string; return: number | null; equity: number | null
  positions: { security: string; amount: string }[]; halted: boolean; last_update: number | null; spark: number[]
  last_fill: { ts: number; side: string; security: string; price: string; reason: string } | null
}
export interface BotItem { id: string; kind: string; name: string; label: string; hook_path: string | null; created_at: number }
export interface BotKinds { [k: string]: { label: string; fields: string[] } }

export const nodeApi = {
  health: () => get<NodeHealth>('/api/node/health'),
  board: () => get<{ sessions: BoardSession[]; node: NodeHealth; alerts: { title: string; severity: string; created_at: number }[] }>('/api/node/board'),
  bots: () => get<{ bots: BotItem[]; kinds: BotKinds; bindings: { id: string; platform: string; user_id: string; bound_at: number }[] }>('/api/bot'),
  addBot: (kind: string, name: string, creds: Record<string, string>) => post<{ bot: BotItem }>('/api/bot', { kind, name, creds }),
  delBot: (id: string) => del<{ ok: boolean }>(`/api/bot/${id}`),
  bindCode: () => post<{ code: string; expires_at: number }>('/api/bot/bind-code', {}),
  unbind: (id: string) => del<{ ok: boolean }>(`/api/bot/bindings/${encodeURIComponent(id)}`),
  stop: (id: string, close: boolean) => post<{ session: unknown }>(`/api/studio/paper/${id}/${close ? "liquidate" : "stop"}`, {}),
}
