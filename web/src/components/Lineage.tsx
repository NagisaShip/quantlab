/**
 * 策略血缘树：横向的树，从左（最早的版本 / 原始策略）到右（最新版本 / 克隆出来的分支）。
 * - 实线：同一个策略里的「改出新版本」；虚线：克隆 / 派生（从别人的策略或广场帖子复制出来）；
 * - 每个节点标出是谁改的（人 / QuantBot 代某人）、什么时候、这个版本上跑过的回测；
 * - 点节点在右侧看详情：全部编辑者、回测列表，跳转到编辑器 / 回测详情。
 */
import { useMemo, useState } from 'react'
import { since, type LineageEdge, type LineageNode } from '../apiCollab'
import { QuantBot } from './QuantBot'

const W = 212
const H = 82
const COL = W + 56
const ROW = H + 22

interface Placed { node: LineageNode; x: number; y: number; depth: number }

function layout(nodes: LineageNode[], edges: LineageEdge[]) {
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const parent = new Map<string, string>()
  const kids = new Map<string, string[]>()
  // 每个节点只认一个父节点（先认版本链，再认克隆来源），其余边照画但不参与布局
  const sorted = [...edges].sort((a, b) => (a.kind === 'edit' ? 0 : 1) - (b.kind === 'edit' ? 0 : 1))
  for (const e of sorted) {
    if (!byId.has(e.from) || !byId.has(e.to) || parent.has(e.to) || e.from === e.to) continue
    // 防环
    let p: string | undefined = e.from
    let loop = false
    while (p) { if (p === e.to) { loop = true; break } p = parent.get(p) }
    if (loop) continue
    parent.set(e.to, e.from)
    kids.set(e.from, [...(kids.get(e.from) ?? []), e.to])
  }
  const at = (id: string) => byId.get(id)?.at ?? 0
  for (const v of kids.values()) v.sort((a, b) => at(a) - at(b))
  const roots = nodes.filter((n) => !parent.has(n.id)).sort((a, b) => (a.at ?? 0) - (b.at ?? 0))
  const placed = new Map<string, Placed>()
  let row = 0
  const visit = (id: string, depth: number): number => {
    const ch = kids.get(id) ?? []
    let y: number
    if (!ch.length) {
      y = row * ROW
      row += 1
    } else {
      const ys = ch.map((c) => visit(c, depth + 1))
      y = (ys[0] + ys[ys.length - 1]) / 2
    }
    placed.set(id, { node: byId.get(id)!, x: depth * COL, y, depth })
    return y
  }
  const families: { root: string; top: number; bottom: number }[] = []
  for (const r of roots) {
    const top = row * ROW
    visit(r.id, 0)
    families.push({ root: r.id, top, bottom: row * ROW })
    row += 0.4 // 家族之间留一点空
  }
  const width = Math.max(...[...placed.values()].map((p) => p.x + W), W) + 8
  const height = Math.max(row * ROW, ROW)
  return { placed, width, height, families }
}

const pct = (v: number | null | undefined) => (typeof v === 'number' ? `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%` : '—')

export function LineageTree({ nodes, edges, onOpen, focusSlug }: {
  nodes: LineageNode[]; edges: LineageEdge[]; onOpen: (key: string) => void; focusSlug?: string
}) {
  const { placed, width, height } = useMemo(() => layout(nodes, edges), [nodes, edges])
  const [sel, setSel] = useState<string | null>(() => nodes.find((n) => n.current && n.slug === focusSlug)?.id ?? null)
  const selected = sel ? placed.get(sel)?.node : null
  if (!nodes.length) return null
  return (
    <div className="lin-wrap">
      <div className="lin-canvas" role="tree" aria-label="策略血缘">
        <div className="lin-inner" style={{ width, height }}>
          <svg className="lin-edges" width={width} height={height} aria-hidden>
            {edges.map((e, i) => {
              const a = placed.get(e.from)
              const b = placed.get(e.to)
              if (!a || !b) return null
              const x1 = a.x + W
              const y1 = a.y + H / 2
              const x2 = b.x
              const y2 = b.y + H / 2
              const mx = (x1 + x2) / 2
              return (
                <g key={i} className={`lin-edge ${e.kind}`}>
                  <path d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} />
                  {e.kind === 'clone' && <text x={mx} y={(y1 + y2) / 2 - 4} textAnchor="middle">克隆</text>}
                </g>
              )
            })}
          </svg>
          {[...placed.values()].map(({ node: n, x, y }, i) => {
            const parentSlug = edges.find((e) => e.to === n.id)
            const showName = !parentSlug || placed.get(parentSlug.from)?.node.slug !== n.slug
            const bt = n.backtests[0]
            return (
              <button key={n.id} type="button" role="treeitem" aria-selected={sel === n.id}
                className={`lin-node${n.current ? ' current' : ''}${n.kind === 'post' ? ' post' : ''}${!n.exists ? ' gone' : ''}${sel === n.id ? ' on' : ''}${n.slug === focusSlug ? ' focus' : ''}`}
                style={{ left: x, top: y, width: W, height: H, animationDelay: `${Math.min(i, 12) * 25}ms` }}
                onClick={() => setSel(n.id)} onDoubleClick={() => n.kind !== 'post' && n.exists && onOpen(`editor/${n.slug}`)}>
                <div className="lin-top">
                  {showName ? <b className="lin-name">{n.name}</b> : <span className="lin-name muted">{n.action === 'clone' ? '克隆' : '新版本'}</span>}
                  {n.kind !== 'post' && <code className="lin-ver">{n.version}</code>}
                  {n.current && <span className="lin-cur">当前</span>}
                </div>
                <div className="lin-who">
                  {n.author ? (
                    <>
                      <span className={`ed-av${n.author.actor_kind === 'agent' ? ' agent' : ''}`}>
                        {n.author.actor_kind === 'agent' ? <QuantBot size={13} variant="simple" /> : (n.author.name || '?').slice(0, 1)}
                      </span>
                      <span>{n.author.actor_kind === 'agent' ? `QuantBot · 代 ${n.author.name}` : n.author.name}</span>
                      {n.editors.length > 1 && <em>+{n.editors.length - 1}</em>}
                    </>
                  ) : <span className="muted">{n.kind === 'post' ? '策略广场帖子' : '早期版本（无编辑记录）'}</span>}
                  <span className="lin-at">{n.at ? since(n.at) : ''}</span>
                </div>
                <div className="lin-bt">
                  {bt ? (
                    <>
                      <span className={(bt.metrics.total_return ?? 0) >= 0 ? 'up' : 'down'}>{pct(bt.metrics.total_return)}</span>
                      <span>夏普 {typeof bt.metrics.sharpe === 'number' ? bt.metrics.sharpe.toFixed(2) : '—'}</span>
                      <span className="muted">{n.backtests.length} 次回测</span>
                    </>
                  ) : <span className="muted">{n.kind === 'post' ? '' : '未回测'}</span>}
                </div>
              </button>
            )
          })}
        </div>
      </div>
      {selected && (
        <aside className="lin-side anim-pop" key={selected.id}>
          <div className="lin-side-head">
            <b>{selected.name}</b>
            {selected.kind !== 'post' && <code>{selected.version}</code>}
            <button type="button" className="icon-btn" aria-label="关闭" onClick={() => setSel(null)}>×</button>
          </div>
          {selected.note && <p className="hint">{selected.note}</p>}
          <h4>编辑者</h4>
          {selected.editors.length ? (
            <ul className="plain-list">
              {selected.editors.map((e) => (
                <li key={`${e.user_id}-${e.actor_kind}`}>
                  <span className={`ed-av${e.actor_kind === 'agent' ? ' agent' : ''}`}>{e.actor_kind === 'agent' ? <QuantBot size={13} variant="simple" /> : (e.name || '?').slice(0, 1)}</span>
                  {e.actor_kind === 'agent' ? `QuantBot（代 ${e.name}）` : e.name}
                </li>
              ))}
            </ul>
          ) : <div className="hint">没有记录</div>}
          <h4>这个版本上的回测</h4>
          {selected.backtests.length ? (
            <ul className="lin-bts">
              {selected.backtests.map((b) => (
                <li key={b.id}>
                  <a href={`#/backtest/${b.id}`}>{pct(b.metrics.total_return)}</a>
                  <span>夏普 {typeof b.metrics.sharpe === 'number' ? b.metrics.sharpe.toFixed(2) : '—'}</span>
                  <span>回撤 {pct(b.metrics.max_drawdown)}</span>
                  <small>{b.by || '—'} · {since(b.created_at)}{b.data_sources.some((s) => /synthetic|合成/.test(s)) ? ' · 合成数据' : ''}</small>
                </li>
              ))}
            </ul>
          ) : <div className="hint">还没有回测</div>}
          <div className="button-row" style={{ marginTop: 12 }}>
            {selected.kind === 'post'
              ? <button type="button" className="button small" onClick={() => onOpen(`post/${selected.id.replace(/^post:/, '')}`)}>打开帖子</button>
              : selected.exists && <>
                <button type="button" className="button small primary" onClick={() => onOpen(`editor/${selected.slug}`)}>打开策略</button>
                <button type="button" className="button small" onClick={() => onOpen(`backtests/${selected.slug}`)}>全部回测</button>
              </>}
          </div>
        </aside>
      )}
    </div>
  )
}
