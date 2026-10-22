import { useCallback, useEffect, useState } from 'react'
import { get, post } from '../http'
import { Callout, EmptyState, Heading, Loading, Panel, Tag } from '../components/ui'
import type { PageProps } from './types'

type ApprovalItem = {
  id: string
  source: 'ai' | 'lab'
  title: string
  summary: string
  risk?: string[]
  created_at?: number
  href?: string
  project_id?: string
  actions?: { tool: string; label: string; category?: string }[]
}

type ApprovalView = {
  items: ApprovalItem[]
  counts: Record<string, number>
  total: number
}

function when(ts?: number): string {
  if (!ts) return ''
  try {
    return new Date(ts).toLocaleString()
  } catch {
    return ''
  }
}

/** 统一审批收件箱：AI 行动与 AI 研究室审批卡片在同一个列表里处理。 */
export default function ApprovalsPage({ onNavigate, notify }: PageProps) {
  const [data, setData] = useState<ApprovalView | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setError('')
    try {
      setData(await get<ApprovalView>('/api/approvals'))
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const decide = async (item: ApprovalItem, approve: boolean) => {
    setBusy(item.id)
    setError('')
    try {
      await post(`/api/approvals/${encodeURIComponent(item.id)}`, { approve })
      notify(approve ? '已批准' : '已拒绝')
      await load()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc))
    } finally {
      setBusy('')
    }
  }

  return (
    <>
      <Heading
        title="待审批"
        description="AI 行动与 AI 研究室的审批卡片集中在这里；批准后仍走原来的执行与审计路径。"
      />
      {error && <Callout tone="warn">{error}</Callout>}
      {data === null ? (
        <Loading />
      ) : data.items.length === 0 ? (
        <EmptyState title="没有待审批项">AI 需要你确认操作时会出现在这里，也可以直接在原对话里处理。</EmptyState>
      ) : (
        <div className="stack">
          {data.items.map((item) => (
            <Panel
              key={item.id}
              title={item.title}
              subtitle={`${item.source === 'lab' ? 'AI 研究室' : 'AI 行动'} · ${when(item.created_at)}`}
              actions={<Tag tone={item.source === 'lab' ? 'info' : 'warn'}>{item.source === 'lab' ? '研究室' : 'AI'}</Tag>}
            >
              <p>{item.summary || '等待你确认'}</p>
              {item.actions && item.actions.length > 0 && (
                <ul className="muted">
                  {item.actions.map((a) => (
                    <li key={`${item.id}-${a.tool}-${a.label}`}>
                      {a.label}
                      {a.category ? `（${a.category}）` : ''}
                    </li>
                  ))}
                </ul>
              )}
              <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                <button className="btn btn-primary" disabled={busy === item.id} onClick={() => void decide(item, true)}>
                  批准
                </button>
                <button className="btn" disabled={busy === item.id} onClick={() => void decide(item, false)}>
                  拒绝
                </button>
                {item.href && (
                  <button className="btn btn-ghost" onClick={() => onNavigate(item.href!.replace(/^#\//, ''))}>
                    打开原对话
                  </button>
                )}
              </div>
            </Panel>
          ))}
        </div>
      )}
    </>
  )
}
