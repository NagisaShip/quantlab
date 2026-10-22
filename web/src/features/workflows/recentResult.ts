import { useCallback, useEffect, useRef, useState } from 'react'
import { request } from '../../http'

export type RecentAttempt = {
  run_id: string
  state: string
  workflow_id: string
  workflow_title: string
  revision?: number
  origin?: string
  created_at?: number
}

export type RecentResult = {
  run_id: string
  workflow_id: string
  workflow_title: string
  revision?: number
  origin?: string
  created_at?: number
  artifact_count: number
}

export type RecentSummary = {
  format: string
  state: 'ready' | 'empty' | 'running' | 'failed' | 'unavailable'
  result: RecentResult | null
  latest_attempt: RecentAttempt | null
  reason?: string | null
}

type Phase = 'idle' | 'loading' | 'ready' | 'error'

const POLL_MS = 20_000
const ACTIVE = new Set(['QUEUED', 'PENDING', 'RUNNING', 'PAUSED', 'RECOVERING'])
const TERMINAL_FAILURE = new Set(['FAILED', 'CANCELED'])

export function latestAttemptIsActive(summary: RecentSummary | null): boolean {
  return !!summary?.latest_attempt && ACTIVE.has(summary.latest_attempt.state)
}

export function latestAttemptFailed(summary: RecentSummary | null): boolean {
  return !!summary?.latest_attempt && TERMINAL_FAILURE.has(summary.latest_attempt.state)
}

/**
 * 最近结果只轮询一个 workspace 级摘要端点；页面隐藏时暂停，重新可见或窗口聚焦时刷新。
 * 请求始终可取消，避免切换项目后旧响应覆盖新状态。
 */
export function useRecentResult(enabled: boolean) {
  const [summary, setSummary] = useState<RecentSummary | null>(null)
  const [phase, setPhase] = useState<Phase>('idle')
  const [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)
  const hasData = useRef(false)

  const load = useCallback(async () => {
    controller.current?.abort()
    const next = new AbortController()
    controller.current = next
    if (!hasData.current) setPhase('loading')
    else setPhase(current => current === 'error' ? 'loading' : current)
    try {
      const value = await request<RecentSummary>('/api/workflows/recent-result', { signal: next.signal })
      if (next.signal.aborted) return
      hasData.current = true
      setSummary(value)
      setError('')
      setPhase('ready')
    } catch (reason) {
      if (next.signal.aborted) return
      setError(reason instanceof Error ? reason.message : String(reason))
      setPhase('error')
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      controller.current?.abort()
      hasData.current = false
      setSummary(null)
      setError('')
      setPhase('idle')
      return
    }
    void load()
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, POLL_MS)
    const visible = () => { if (document.visibilityState === 'visible') void load() }
    document.addEventListener('visibilitychange', visible)
    window.addEventListener('focus', visible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', visible)
      window.removeEventListener('focus', visible)
      controller.current?.abort()
    }
  }, [enabled, load])

  return { summary, phase, error, reload: load }
}
