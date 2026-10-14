import { useMemo } from 'react'

type Op = { kind: 'same' | 'add' | 'del'; text: string; a?: number; b?: number }

/** 行级 LCS 差异。策略文件通常几百行以内，O(n·m) 足够。 */
export function lineDiff(a: string, b: string): Op[] {
  const x = a.split('\n')
  const y = b.split('\n')
  const n = x.length
  const m = y.length
  if (n * m > 4_000_000) {
    return [...x.map((t, i) => ({ kind: 'del' as const, text: t, a: i + 1 })), ...y.map((t, j) => ({ kind: 'add' as const, text: t, b: j + 1 }))]
  }
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: Op[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (x[i] === y[j]) {
      out.push({ kind: 'same', text: x[i], a: i + 1, b: j + 1 })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ kind: 'del', text: x[i], a: i + 1 })
      i++
    } else {
      out.push({ kind: 'add', text: y[j], b: j + 1 })
      j++
    }
  }
  while (i < n) out.push({ kind: 'del', text: x[i], a: ++i })
  while (j < m) out.push({ kind: 'add', text: y[j], b: ++j })
  return out
}

/** 只显示改动附近 3 行上下文的差异视图。 */
export function DiffView({ before, after, maxHeight = 420 }: { before: string; after: string; maxHeight?: number }) {
  const ops = useMemo(() => lineDiff(before, after), [before, after])
  const added = ops.filter((o) => o.kind === 'add').length
  const removed = ops.filter((o) => o.kind === 'del').length
  const keep = new Set<number>()
  ops.forEach((o, i) => {
    if (o.kind !== 'same') for (let k = Math.max(0, i - 3); k <= Math.min(ops.length - 1, i + 3); k++) keep.add(k)
  })
  const rows: (Op | null)[] = []
  ops.forEach((o, i) => {
    if (keep.has(i)) rows.push(o)
    else if (rows[rows.length - 1] !== null) rows.push(null)
  })
  if (!added && !removed) return <div className="diff-empty">代码没有变化</div>
  return (
    <div className="diff" style={{ maxHeight }}>
      <div className="diff-stat"><span className="positive">+{added}</span> <span className="negative">−{removed}</span> 行</div>
      {rows.map((o, i) =>
        o === null ? (
          <div key={i} className="diff-gap">⋯</div>
        ) : (
          <div key={i} className={`diff-line ${o.kind}`}>
            <span className="diff-no">{o.kind === 'add' ? '' : o.a}</span>
            <span className="diff-no">{o.kind === 'del' ? '' : o.b}</span>
            <span className="diff-sign">{o.kind === 'add' ? '+' : o.kind === 'del' ? '−' : ' '}</span>
            <code>{o.text || ' '}</code>
          </div>
        ),
      )}
    </div>
  )
}
