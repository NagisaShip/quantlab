/** 本地项目面板用到的纯展示辅助函数。 */

/** 人类可读的字节大小。 */
export const size = (n: number | null | undefined) => {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—'
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${n.toLocaleString()} B`
}

/** 路径最后一段（兼容 / 与 \）。 */
export const baseName = (path: string) => path.split(/[\\/]/).filter(Boolean).pop() ?? path

/** 服务端任务状态的中文文案与色调。 */
export const RUN_LABEL: Record<string, string> = {
  QUEUED: '排队中',
  RUNNING: '运行中',
  PAUSED: '已暂停',
  RECOVERING: '恢复中',
  COMPLETED: '已完成',
  FAILED: '失败',
  CANCELED: '已取消',
  CANCELLED: '已取消',
}

export const RUN_TONE: Record<string, 'ok' | 'warn' | 'fail'> = {
  QUEUED: 'warn',
  RUNNING: 'ok',
  PAUSED: 'warn',
  RECOVERING: 'warn',
  COMPLETED: 'ok',
  FAILED: 'fail',
  CANCELED: 'warn',
  CANCELLED: 'warn',
}

export const JOB_LABEL: Record<string, string> = {
  'project-export': '导出项目快照',
  'environment-install': '创建独立研究环境',
}

export const JOB_STATE_LABEL: Record<string, string> = {
  running: '进行中',
  queued: '排队中',
  completed: '已完成',
  failed: '失败',
}

/** 预览单元格：对象/数组折叠为 JSON 文本，null 显示为破折号。 */
export const cellText = (value: unknown) => {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
