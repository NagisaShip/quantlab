import { latestAttemptFailed, latestAttemptIsActive, type RecentSummary } from '../features/workflows/recentResult'

type Presentation = {
  label: string
  title: string
  disabled: boolean
  tone: string
  retry: boolean
}

function present(summary: RecentSummary | null, phase: 'idle' | 'loading' | 'ready' | 'error', error: string, projectName: string): Presentation {
  const project = projectName ? `项目「${projectName}」· ` : ''
  if (phase === 'error') {
    return {
      label: '重试最近结果',
      title: `${project}最近结果读取失败：${error || '未知错误'}。点击重试。`,
      disabled: false,
      tone: 'error',
      retry: true,
    }
  }
  if (phase !== 'ready' || !summary) {
    return {
      label: '读取最近结果…',
      title: `${project}正在读取最近完成的研究结果。`,
      disabled: true,
      tone: 'loading',
      retry: false,
    }
  }
  if (!summary.result) {
    if (summary.state === 'running') {
      return { label: '运行中，暂无结果', title: `${project}${summary.reason || '最近一次运行仍在进行，尚无已完成结果。'}`, disabled: true, tone: 'running', retry: false }
    }
    if (summary.state === 'failed') {
      return { label: '最近运行失败', title: `${project}${summary.reason || '最近一次运行未成功，尚无已完成结果。'}`, disabled: true, tone: 'failed', retry: false }
    }
    if (summary.state === 'unavailable') {
      return { label: '最近结果不可用', title: `${project}${summary.reason || '已有运行的报告或工件不可读取。'}`, disabled: true, tone: 'unavailable', retry: false }
    }
    return { label: '暂无研究结果', title: `${project}${summary.reason || '还没有已完成的研究结果。运行一次研究后可从这里打开。'}`, disabled: true, tone: 'empty', retry: false }
  }

  const result = summary.result
  const active = latestAttemptIsActive(summary)
  const failed = latestAttemptFailed(summary)
  const note = active
    ? `最近一次运行正在进行；这里仍打开上一次完成的结果「${result.workflow_title}」r${result.revision ?? '?'}。`
    : failed
      ? `最近一次运行未成功；这里仍打开上一次完成的结果「${result.workflow_title}」r${result.revision ?? '?'}。`
      : `打开最近完成的研究结果「${result.workflow_title}」r${result.revision ?? '?'}，来源 ${result.origin === 'agent' ? 'Agent' : '研究工作区'}。`
  return {
    label: active ? '最近结果 · 运行中' : '最近结果',
    title: `${project}${note} 包含 ${result.artifact_count} 个可读取工件。`,
    disabled: false,
    tone: active ? 'active' : failed ? 'stale' : 'ready',
    retry: false,
  }
}

export default function RecentResultAction({ summary, phase, error, projectName, onReload }: {
  summary: RecentSummary | null
  phase: 'idle' | 'loading' | 'ready' | 'error'
  error: string
  projectName: string
  onReload: () => void
}) {
  const action = present(summary, phase, error, projectName)
  const open = () => {
    if (action.retry) { onReload(); return }
    const result = summary?.result
    if (!result) return
    window.location.hash = `/workflows/${encodeURIComponent(result.workflow_id)}?run_id=${encodeURIComponent(result.run_id)}`
  }
  return (
    <button
      type="button"
      className="dtb-recent"
      data-state={action.tone}
      aria-label={action.label}
      title={action.title}
      disabled={action.disabled}
      onClick={open}
    >
      <span className="dtb-recent-dot" aria-hidden="true" />
      <span className="dtb-recent-label">{action.label}</span>
    </button>
  )
}
