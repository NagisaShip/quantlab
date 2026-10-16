/**
 * 策略血缘：工作区里每个策略的版本链与克隆派生关系，标出每个版本是谁（人 / QuantBot 代某人）改的、
 * 跑过哪些回测。可以只看某一个策略所在的家族（它的上游原型与所有克隆分支）。
 */
import { useEffect, useMemo, useState } from 'react'
import { collab, type LineageEdge, type LineageNode } from '../apiCollab'
import { EmptyState, Loading } from '../components/ui'
import { LineageTree } from '../components/Lineage'
import type { PageProps } from './types'

export default function LineagePage({ arg, onNavigate, notify }: PageProps & { arg?: string }) {
  const [data, setData] = useState<{ nodes: LineageNode[]; edges: LineageEdge[] } | null>(null)
  const [all, setAll] = useState<{ slug: string; name: string }[]>([])
  const [who, setWho] = useState('')
  useEffect(() => {
    setData(null)
    collab.lineage(arg || undefined).then(setData).catch((e) => notify(e.message, true))
  }, [arg, notify])
  useEffect(() => {
    collab.lineage().then((r) => {
      const seen = new Map<string, string>()
      for (const n of r.nodes) if (n.slug && n.exists && !seen.has(n.slug)) seen.set(n.slug, n.name)
      setAll([...seen.entries()].map(([slug, name]) => ({ slug, name })).sort((a, b) => a.name.localeCompare(b.name)))
    }).catch(() => undefined)
  }, [])
  const people = useMemo(() => {
    const m = new Map<string, string>()
    for (const n of data?.nodes ?? []) for (const e of n.editors) m.set(`${e.user_id}:${e.actor_kind}`, e.actor_kind === 'agent' ? `QuantBot（代 ${e.name}）` : e.name)
    return [...m.entries()]
  }, [data])
  const shown = useMemo(() => {
    if (!data || !who) return data
    const keep = new Set(data.nodes.filter((n) => n.editors.some((e) => `${e.user_id}:${e.actor_kind}` === who)).map((n) => n.id))
    // 保留到根的路径，树才连得起来
    let grew = true
    while (grew) {
      grew = false
      for (const e of data.edges) if (keep.has(e.to) && !keep.has(e.from)) { keep.add(e.from); grew = true }
    }
    return { nodes: data.nodes.filter((n) => keep.has(n.id)), edges: data.edges.filter((e) => keep.has(e.from) && keep.has(e.to)) }
  }, [data, who])
  const stats = useMemo(() => {
    const n = data?.nodes ?? []
    return { strategies: new Set(n.map((x) => x.slug).filter(Boolean)).size, versions: n.filter((x) => x.kind === 'version').length,
      clones: (data?.edges ?? []).filter((e) => e.kind === 'clone').length, backtests: n.reduce((s, x) => s + x.backtests.length, 0) }
  }, [data])
  return (
    <div className="lineage-page">
      <div className="board-bar">
        <select value={arg || ''} onChange={(e) => onNavigate(e.target.value ? `lineage/${e.target.value}` : 'lineage')} aria-label="选择策略">
          <option value="">全部策略</option>
          {all.map((s) => <option key={s.slug} value={s.slug}>{s.name}</option>)}
        </select>
        <select value={who} onChange={(e) => setWho(e.target.value)} aria-label="按编辑者筛选">
          <option value="">所有编辑者</option>
          {people.map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <span className="lin-legend"><i className="solid" />改出新版本<i className="dash" />克隆 / 派生<span className="lin-cur">当前</span>当前版本</span>
        <span className="spacer" />
        {data && <span className="hint">{stats.strategies} 个策略 · {stats.versions} 个版本 · {stats.clones} 次克隆 · {stats.backtests} 次回测（每个版本最多列 6 次）</span>}
      </div>
      {!shown ? <Loading label="正在整理血缘" /> : !shown.nodes.length ? (
        <EmptyState title="还没有血缘记录">新建、修改、克隆策略都会记下是谁改的；跑过的回测会挂到对应版本上。</EmptyState>
      ) : <LineageTree nodes={shown.nodes} edges={shown.edges} onOpen={onNavigate} focusSlug={arg || undefined} />}
      <p className="hint">单击节点看详情，双击打开策略。编辑人员标记从启用协作功能后开始记录；更早的版本根据历史快照补出版本链，但没有编辑者。</p>
    </div>
  )
}
