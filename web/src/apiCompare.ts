/** 回测页「策略对比」批次接口。 */
import type { FullMetrics, Params, Spec } from './api/research'
import { del, get, post } from './http'

export interface CompareMember { slug: string; name: string; backtest_id: string | null; stress_backtest_id: string | null }
export interface CompareBatch {
  id: string; name: string; members: CompareMember[]; spec: Partial<Spec>; stress_spec: Partial<Spec> | null; digest: string; created_at: number
}
export interface CompareItem { id: string; strategy_name: string; version: string; params: Params; metrics: Partial<FullMetrics>; timestamps: number[]; returns: number[] }

export const compareApi = {
  create: (p: { name?: string; slugs: string[]; spec: Partial<Spec>; stress: boolean }) =>
    post<{ batch: CompareBatch }>('/api/research/compare-batches', p),
  addResult: (id: string, slug: string, backtest_id: string, stress = false) =>
    post<{ batch: CompareBatch }>(`/api/research/compare-batches/${id}/results`, { slug, backtest_id, stress }),
  list: () => get<{ batches: CompareBatch[] }>('/api/research/compare-batches'),
  get: (id: string) => get<{ batch: CompareBatch; items: CompareItem[]; stress_items: CompareItem[] }>(`/api/research/compare-batches/${id}`),
  remove: (id: string) => del<{ ok: boolean }>(`/api/research/compare-batches/${id}`),
}
