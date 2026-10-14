import { useCallback, useEffect, useState } from 'react'
import { fmtTime } from '../api2'
import { nodeApi, type BoardSession, type NodeHealth } from '../apiNode'
import { EmptyState, Loading, Modal } from '../components/ui'
import type { PageProps } from './types'

const STATE: Record<string, string> = { RUNNING: '运行中', PAUSED: '已暂停', RECOVERING: '恢复中', QUEUED: '排队中' }

function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return null
  const lo = Math.min(...values), hi = Math.max(...values)
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * 100},${hi === lo ? 15 : 28 - ((v - lo) / (hi - lo)) * 26}`).join(' ')
  const up = values[values.length - 1] >= values[0]
  return (
    <svg className="mb-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
      <polyline points={pts} fill="none" stroke={up ? 'var(--up)' : 'var(--down)'} strokeWidth="1.6" vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

export default function MobileBoardPage({ notify }: PageProps) {
  const [data, setData] = useState<{ sessions: BoardSession[]; node: NodeHealth; alerts: { title: string; severity: string; created_at: number }[] } | null>(null)
  const [ask, setAsk] = useState<{ ids: string[]; close: boolean; label: string } | null>(null)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => { nodeApi.board().then(setData).catch((e) => notify(e.message, true)) }, [notify])
  useEffect(() => {
    load()
    const t = window.setInterval(load, 15_000)
    return () => window.clearInterval(t)
  }, [load])
  if (!data) return <Loading />
  const { sessions, node } = data
  const word = ask?.close ? '平仓' : '停止'
  return (
    <div className="mb">
      <div className={`mb-node ${node.stale_runs.length ? 'bad' : node.workers_alive ? 'ok' : 'warn'}`}>
        <span className="mb-dot" />
        {node.stale_runs.length ? `${node.stale_runs.length} 个实验失联` : node.workers_alive ? `节点在线 · ${node.workers_alive} 个 worker` : '没有在线的 worker'}
        <span className="spacer" />
        <small>{fmtTime(node.now)} 更新</small>
      </div>

      {sessions.length === 0 ? <EmptyState glyph="◷" title="没有运行中的实验">纸面模拟和交易所模拟盘的实验启动后会显示在这里。</EmptyState> : (
        <>
          {sessions.map((s) => (
            <article key={s.id} className="mb-card">
              <header>
                <strong>{s.strategy}</strong>
                <span className={`tag ${s.execution === 'live' ? 'fail' : ''}`}>{s.execution === 'live' ? '实盘' : s.execution === 'demo' ? '模拟盘' : '纸面'}</span>
                {s.halted && <span className="tag fail">已熔断</span>}
                {s.state !== 'RUNNING' && <span className="tag">{STATE[s.state] ?? s.state}</span>}
              </header>
              <div className="mb-row">
                <div className={`mb-ret ${s.return == null ? '' : s.return >= 0 ? 'positive' : 'negative'}`}>{s.return == null ? '—' : `${(s.return * 100).toFixed(2)}%`}</div>
                <Spark values={s.spark} />
              </div>
              <p className="mb-meta">持仓：{s.positions.length ? s.positions.map((p) => p.security).join('、') : '空仓'}
                {s.last_fill && <><br />最近：{s.last_fill.side === 'buy' ? '买入' : '卖出'} {s.last_fill.security}，{s.last_fill.reason || '—'}（{fmtTime(s.last_fill.ts)}）</>}</p>
              <div className="mb-actions">
                <button className="button" onClick={() => { setTyped(''); setAsk({ ids: [s.id], close: false, label: s.strategy }) }}>停止</button>
                <button className="button danger" onClick={() => { setTyped(''); setAsk({ ids: [s.id], close: true, label: s.strategy }) }}>停止并平仓</button>
              </div>
            </article>
          ))}
          {sessions.length > 1 && (
            <button className="button danger mb-all" onClick={() => { setTyped(''); setAsk({ ids: sessions.map((s) => s.id), close: true, label: `全部 ${sessions.length} 个实验` }) }}>全部停止并平仓</button>
          )}
        </>
      )}

      {data.alerts.length > 0 && (
        <section className="mb-alerts">
          <h3>最近告警</h3>
          {data.alerts.map((a, i) => <p key={i} className={a.severity === 'error' ? 'negative' : ''}>{a.title}<small>{fmtTime(a.created_at)}</small></p>)}
        </section>
      )}

      {ask && (
        <Modal title={ask.close ? '停止并平仓' : '停止实验'} onClose={() => setAsk(null)} width={420} footer={
          <button className={`button ${ask.close ? 'danger' : 'primary'}`} disabled={busy || typed.trim() !== word} onClick={async () => {
            setBusy(true)
            try {
              for (const id of ask.ids) await nodeApi.stop(id, ask.close)
              notify(ask.close ? '已发出平仓并停止' : '已停止')
              setAsk(null)
              load()
            } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
          }}>确认{word}</button>
        }>
          <p>{ask.close ? `${ask.label}：先平掉全部持仓再停止。` : `${ask.label}：停止运行，保留持仓。`}</p>
          <label>输入「{word}」确认<input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus /></label>
        </Modal>
      )}
    </div>
  )
}
