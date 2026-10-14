import { useCallback, useEffect, useState } from 'react'
import { v4, type Banner } from '../api3'
import { fmtTime } from '../api2'
import { useEventRefresh, useNewestActiveRunId, useRunEvents, type RunEventStatus } from '../hooks/useRunEvents'
import { usePopover } from './chat/Composer'

const STREAM_LABEL: Record<RunEventStatus, string> = {
  idle: '未订阅',
  connecting: '连接中',
  live: '已连接',
  reconnecting: '重连中',
  offline: '网络已断开',
  hidden: '后台暂停',
  done: '已结束',
  error: '连接失败',
  closed: '已关闭',
}

/**
 * 运行状态：顶栏一个小标签（行情来源 + 提醒数），点开看运行模式、行情、最近有效事件、网络、
 * 后台任务、事件流、策略隔离与模型连接。平台本期只做研究与仿真，模式固定为虚拟资金。
 *
 * 刷新方式（T05）：标签本身没有 runId，但它显示的状态恰恰是被 run 的推进改变的。
 * 所以这里订阅当前工作区里**最新一个未结束的 run** 的事件流，有事件才刷新；
 * 只有在"没有活跃 run"或"事件流没连上"时才退回低频兜底轮询（60 秒）。
 */
export default function RunBanner({ onNavigate, llmReady }: { onNavigate: (k: string) => void; llmReady?: boolean }) {
  const [b, setB] = useState<Banner | null>(null)
  const [now, setNow] = useState(Date.now())
  const pop = usePopover()

  const load = useCallback(() => v4.banner().then(setB).catch(() => undefined), [])
  const refresh = useEventRefresh(load, 800)
  const { runId: activeRun } = useNewestActiveRunId()
  const stream = useRunEvents({
    runId: activeRun,
    onEvents: () => refresh(),
    onDone: () => load(),
  })

  useEffect(() => {
    load()
  }, [load])

  // 兜底：没有活跃 run（没有任何事件会到来）或事件流断开时才低频轮询。
  useEffect(() => {
    if (activeRun && stream.connected) return
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [load, activeRun, stream.connected])

  useEffect(() => {
    const c = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(c)
  }, [])

  if (!b) return null

  const m = b.market
  const age = m.last_event_ts ? Math.round((now - m.last_event_ts) / 60_000) : null
  const fresh = age !== null && age <= 75
  const srcLabel = m.source === 'exchange' ? `${m.exchange} 真实行情` : m.source === 'synthetic' ? '合成行情（演示）' : '行情不可用'
  const net = b.network.overall
  const workerOk = b.worker.mode === 'inline' || b.worker.online > 0
  const streamBad = stream.status === 'offline' || stream.status === 'error'
  const rows: { ok: boolean | null; label: string; value: string; go?: string }[] = [
    { ok: null, label: '运行模式', value: '研究模式 · 虚拟资金' },
    { ok: m.source === 'exchange', label: '行情', value: `${srcLabel} · ${m.symbol}` },
    { ok: fresh, label: '最近有效事件', value: m.last_event_ts ? `${fmtTime(m.last_event_ts)} UTC${age !== null ? `（${age} 分钟前）` : ''}` : '—' },
    { ok: net === 'AVAILABLE', label: '网络', value: b.network.label, go: 'settings/system' },
    { ok: workerOk, label: '后台任务', value: b.worker.mode === 'inline' ? '进程内（开发）' : workerOk ? `在线 ${b.worker.online}` : '离线，任务会排队' },
    { ok: activeRun ? !streamBad : null, label: '事件流', value: `${STREAM_LABEL[stream.status]}${activeRun ? ` · 游标 ${stream.cursor}` : ''}` },
    { ok: b.isolation.verified, label: '策略隔离', value: b.isolation.verified ? '已验证' : '部分生效' },
    { ok: !!llmReady, label: 'AI 模型', value: llmReady ? '已连接' : '未配置，AI 功能为规则降级', go: 'models' },
  ]
  const issues = rows.filter((r) => r.ok === false).length

  return (
    <div className="status-anchor" ref={pop.ref}>
      <button type="button" className="status-chip" onClick={() => pop.setOpen(!pop.open)} aria-expanded={pop.open} title="运行状态">
        <span className={`dot ${issues ? 'warn' : 'ok'}`} />
        {m.source === 'exchange' ? '实时行情' : '合成行情'}{issues ? ` · ${issues} 项提醒` : ''}
        {/* 端到端测试读取事件流状态用；视觉上隐藏 */}
        <span className="sr-only" data-testid="banner-event-stream" data-status={stream.status} data-connects={stream.connects}>
          事件流 {STREAM_LABEL[stream.status]}
        </span>
      </button>
      {pop.open && (
        <div className="status-menu" role="dialog" aria-label="运行状态">
          {rows.map((r) => (
            <div key={r.label} className={`status-row${r.go ? ' link' : ''}`} onClick={() => { if (r.go) { pop.setOpen(false); onNavigate(r.go) } }}>
              <span className={`dot ${r.ok === null ? '' : r.ok ? 'ok' : 'warn'}`} />
              <span className="status-label">{r.label}</span>
              <span className="status-value">{r.value}</span>
            </div>
          ))}
          <div className="status-foot">所有时间按 UTC 计</div>
        </div>
      )}
    </div>
  )
}
