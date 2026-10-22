import type { ReactNode } from 'react'
import type { ErrorExplanation } from '../../http'
import { JOB_LABEL, JOB_STATE_LABEL, cellText } from './format'
import type { Job, PreviewResult, PreviewTable } from './types'

/** 设置内容区里的一张分区卡片。标题用 h3，保持和设置页其它分区一致。 */
export function Section({ title, note, actions, children }: {
  title: ReactNode
  note?: ReactNode
  actions?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="lp-card">
      <div className="lp-card-head">
        <h3>{title}</h3>
        {actions && <div className="lp-card-actions">{actions}</div>}
      </div>
      {note && <p className="lp-card-sub">{note}</p>}
      {children}
    </section>
  )
}

/** 轻量状态徽标：ok / warn / fail / 默认。 */
export function Pill({ tone, children }: { tone?: 'ok' | 'warn' | 'fail'; children: ReactNode }) {
  return <span className={`lp-pill${tone ? ' ' + tone : ''}`}>{children}</span>
}

/** 错误块：人话标题 + 修复动作；技术详情折叠，不再把 JSON 直接甩给用户。 */
export function ErrorAlert({ error, onClose }: { error: ErrorExplanation; onClose: () => void }) {
  return (
    <div className="lp-alert" role="alert">
      <div className="lp-alert-main">
        <b>{error.title}</b>
        {error.message && <span>{error.message}</span>}
        {error.action && <small>{error.action}</small>}
        {error.detail && (
          <details className="lp-more">
            <summary>技术详情</summary>
            <pre className="lp-pre">{error.detail}</pre>
          </details>
        )}
      </div>
      <button type="button" className="lp-x" aria-label="关闭错误提示" onClick={onClose}>×</button>
    </div>
  )
}

/** 指标格：资源占用、缓存等。 */
export function Stat({ label, value, hint }: { label: ReactNode; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="lp-stat">
      <small>{label}</small>
      <b>{value}</b>
      {hint && <span>{hint}</span>}
    </div>
  )
}

/** 长路径 / 解释器路径：等宽字体并强制换行，避免撑破设置内容区。 */
export function PathCode({ label, value }: { label?: ReactNode; value: string }) {
  return (
    <div className="lp-path-line">
      {label && <span>{label}</span>}
      <code className="lp-path" title={value}>{value}</code>
    </div>
  )
}

export function InlineLoading({ label }: { label: string }) {
  return <p className="lp-loading" role="status">{label}…</p>
}

/** 后台管理任务（导出项目 / 创建研究环境）的状态与日志。 */
export function JobStatus({ job }: { job: Job }) {
  const running = job.state === 'running' || job.state === 'queued'
  const tone = job.state === 'completed' ? 'ok' : job.state === 'failed' ? 'fail' : 'warn'
  return (
    <div className={`lp-job${job.state === 'failed' ? ' fail' : ''}`} role="status" aria-live="polite">
      <div className="lp-job-head">
        <b>{JOB_LABEL[job.kind ?? ''] ?? '管理任务'}</b>
        <Pill tone={tone}>{JOB_STATE_LABEL[job.state] ?? job.state}</Pill>
      </div>
      {running && <p className="hint">任务在后台运行，可以离开本页；完成后会自动刷新数据。</p>}
      {job.error && <p className="lp-err">{job.error}</p>}
      {job.log_tail && (
        <details className="lp-more">
          <summary>查看安装日志（最近 3000 字）</summary>
          <pre className="lp-pre">{job.log_tail}</pre>
        </details>
      )}
    </div>
  )
}

function isPreviewTable(value: unknown): value is PreviewTable {
  if (!value || typeof value !== 'object') return false
  const candidate = value as { fields?: unknown; data?: unknown }
  return Array.isArray(candidate.fields) && Array.isArray(candidate.data)
}

/** 数据预览：字段 + 样本表格，或（重新定位等非表格结果）折叠的 JSON。 */
export function PreviewBlock({ preview, onClose }: { preview: PreviewResult; onClose: () => void }) {
  const table = isPreviewTable(preview.value) ? preview.value : null
  return (
    <div className="lp-preview">
      <div className="lp-preview-head">
        <b>{preview.title}</b>
        {table && (
          <small>
            {table.total == null ? `${table.data.length} 行样本` : `共 ${table.total.toLocaleString()} 行`}
            {table.has_more ? ' · 还有更多' : ''}
            {table.fields.length ? ` · ${table.fields.length} 个字段` : ''}
          </small>
        )}
        <button type="button" className="lp-x" aria-label="关闭预览" onClick={onClose}>×</button>
      </div>
      {table ? <PreviewTableBlock table={table} /> : <pre className="lp-pre">{JSON.stringify(preview.value, null, 2)}</pre>}
    </div>
  )
}

function PreviewTableBlock({ table }: { table: PreviewTable }) {
  if (!table.fields.length) return <p className="hint">没有可展示的字段（可能是空文件）。</p>
  return (
    <div className="lp-table-wrap">
      <table className="lp-preview-table">
        <thead>
          <tr>{table.fields.map((field) => <th key={field}>{field}</th>)}</tr>
        </thead>
        <tbody>
          {table.data.map((row, index) => (
            <tr key={index}>
              {table.fields.map((field) => <td key={field}>{cellText(row[field])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
