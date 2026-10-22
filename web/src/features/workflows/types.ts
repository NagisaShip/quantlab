export type Port = { type: string; role?: string; role_from?: string | string[]; semantics?: Record<string, unknown>; forbid_roles?: string[]; required?: boolean; schema?: Record<string, unknown> }
export type Field = { type?: string; title?: string; default?: unknown; enum?: unknown[]; description?: string; minimum?: number; maximum?: number; visible_when?: Record<string, unknown[]>; widget?: string; properties?: Record<string, Field>; primary?: string[] }
export type Definition = {
  kind: string; version: string; title: string; description?: string; category?: string; code: string; code_hash: string
  inputs: Record<string, Port>; outputs: Record<string, Port>
  parameters: { properties?: Record<string, Field>; required?: string[] }
  requirements: string[]; bundled_modules?: string[]; builtin?: boolean; subflow?: unknown; file_params?: string[]; level?: string; view?: string; resources?: Record<string, unknown>
}
export type Card = {
  id: string; kind: string; version: string; title?: string; params: Record<string, unknown>
  position: { x: number; y: number }; bindings?: Record<string, { input: string; path: string[] }>
  flipped?: boolean; timeout_seconds?: number
  ui?: { network_layout?: Record<string, {x:number;y:number}> }
}
export type Wire = { id: string; source: string; source_port: string; target: string; target_port: string }
export type Flow = { format: string; title: string; nodes: Card[]; edges: Wire[]; settings?: { seed?: number }; resources?: string[] }
export type Saved = { id: string; title: string; revision: number; workflow: Flow }
export type Ref = { hash: string; type: string; role?: string; bytes: number }
export type NodeRun = { status: string; error?: string; params?: Record<string, unknown>; inputs?: Record<string, Ref>; outputs: Record<string, Ref>; logs?: string[]; stdout?: string; child_run_id?: string; code_hash?: string; file_hashes?: Record<string,string>; environment?: Record<string,unknown>; traceback?: string }
export type Report = { id: string; status: string; mode: string; nodes: Record<string, NodeRun>; workflow: Flow; workflow_hash: string }
export type PlatformRun = { id: string; state: string; error_message?: string; summary?: { download?: string; filename?: string }; progress?: { done: number; total: number } }
export type Check = { name: string; passed: boolean; message?: string; node?: string }
export type Catalog = { cards: Definition[]; templates: Flow[]; custom_code_policy: string }
export const uid = (prefix: string) => `${prefix}-${crypto.randomUUID().slice(0, 8)}`
export const compatible = (a: Port, b: Port) => (b.type === 'any' || a.type === b.type) && (!!a.role_from || ((!b.role || a.role === b.role) && !(b.forbid_roles || []).includes(a.role || ''))) && Object.entries(b.semantics || {}).every(([k,v]) => v == null || JSON.stringify(a.semantics?.[k]) === JSON.stringify(v))
export const statusName: Record<string, string> = { running: '运行中', completed: '完成', cached: '复用缓存', pinned: '调试固定', failed: '失败', blocked: '上游阻断', canceled: '已取消' }
