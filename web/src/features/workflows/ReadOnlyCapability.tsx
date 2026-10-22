/**
 * 已停用研究能力的只读上下文与提示。
 *
 * 停用只阻止「新的计算 / 变更」，历史资产、报告、图表、源码与个人 PIN 仍可读取；
 * 主导航可以不显示停用能力，但已有深链接仍然可以打开历史。所有禁用控件共用页面顶部的
 * 一条说明，避免在每个按钮旁重复堆警告。
 */
import { createContext, useContext, type ReactNode } from 'react'
import './readOnlyCapability.css'

export type ResearchCapabilityId = 'factor' | 'model' | 'portfolio' | 'execution'
export type ResearchPolicyState = 'ready' | 'loading' | 'error'

export interface ResearchCapabilityState {
  id: ResearchCapabilityId
  name: string
  enabled: boolean
  /** 读取中 / 读取失败时也按只读处理，不能沿用旧工作区的 enabled 快照。 */
  policy: ResearchPolicyState
  onRetry?: () => void
}

/** 禁用控件的 aria-describedby 目标；页面顶部只渲染一次。 */
export const READ_ONLY_REASON_ID = 'research-capability-readonly-reason'

const CapabilityContext = createContext<ResearchCapabilityState | null>(null)

export function ResearchCapabilityProvider({ value, children }: {
  value: ResearchCapabilityState
  children: ReactNode
}) {
  return <CapabilityContext.Provider value={value}>{children}</CapabilityContext.Provider>
}

/** 只有当前页面属于该领域时才返回状态；其它领域或未包在 Provider 内返回 null。 */
export function useReadOnlyCapability(id?: ResearchCapabilityId) {
  const value = useContext(CapabilityContext)
  if (!value || (id && value.id !== id)) return null
  const policy = value.policy ?? 'ready'
  const readOnly = policy !== 'ready' || !value.enabled
  const reason = policy === 'loading'
    ? '研究能力设置正在读取，暂以只读方式打开；读取完成前不会新建或运行。'
    : policy === 'error'
      ? '研究能力设置读取失败，暂以只读方式打开；历史仍可查看，新建与运行不可用。'
      : `「${value.name}」已停用，仅查看历史；启用后可继续新建和运行。`
  return { ...value, policy, readOnly, reason }
}

/** 页面级单条状态条：停用、读取中或读取失败都只占一行。 */
export function ReadOnlyCapabilityBanner({ onNavigate }: { onNavigate: (key: string) => void }) {
  const value = useContext(CapabilityContext)
  if (!value) return null
  const policy = value.policy ?? 'ready'
  if (policy === 'ready' && value.enabled) return null
  const loading = policy === 'loading'
  const failed = policy === 'error'
  return (
    <section className="ro-banner" aria-labelledby="research-capability-readonly-title">
      <span className="ro-banner-mark" aria-hidden>{failed ? '!' : loading ? '…' : '!'}</span>
      <div className="ro-banner-copy">
        <strong id="research-capability-readonly-title">
          {loading ? '正在读取研究能力设置' : failed ? '研究能力设置读取失败' : '已停用，仅查看历史'}
        </strong>
        <span id={READ_ONLY_REASON_ID}>
          {loading
            ? '读取完成前暂以只读方式打开；历史内容仍可查看。'
            : failed
              ? '暂以只读方式打开；历史内容仍可查看，新建、计算与保存暂不可用。'
              : `「${value.name}」的历史资产、报告、图表和源码仍可查看或导出；新建、重新计算、保存和删除暂不可用。`}
        </span>
      </div>
      {failed && value.onRetry && <button type="button" className="button small primary" onClick={value.onRetry}>重试</button>}
      {(failed || !loading) && <button type="button" className="button small" onClick={() => onNavigate('settings/plugins')}>插件管理</button>}
    </section>
  )
}

/**
 * 新建 / 重新计算类路由的只读落点。
 *
 * 历史列表与详情不经过这里；这里是 `#/factors/new`、`#/modellib/train` 这类没有历史内容的
 * 路由，避免整页死路，同时不挂载会发写请求的表单。
 */
export function ReadOnlyRouteGate({ title, historyLabel, historyRoute, onNavigate, children }: {
  title?: string
  historyLabel: string
  historyRoute: string
  onNavigate: (key: string) => void
  children?: ReactNode
}) {
  const ro = useReadOnlyCapability()
  if (!ro?.readOnly) return null
  const loading = ro.policy === 'loading'
  const failed = ro.policy === 'error'
  return (
    <section className="ro-gate" aria-labelledby="research-capability-gate-title">
      <span className="ro-gate-mark" aria-hidden>▤</span>
      <div className="ro-gate-body">
        <h2 id="research-capability-gate-title">
          {loading ? '正在读取研究能力设置' : failed ? '研究能力设置暂不可用' : title ?? '这里暂时只保留历史'}
        </h2>
        <p>
          {loading
            ? '读取完成前不会挂载新建或计算表单；已有历史仍可查看。'
            : failed
              ? '为避免沿用旧权限，暂以只读方式打开；历史仍可查看，新建与计算不可用。'
              : `「${ro.name}」已停用，仅查看历史。新建、重新计算和保存不可用，已有资产仍可在${historyLabel}查看。`}
        </p>
        {children}
        <div className="ro-gate-actions">
          <button type="button" className="button small primary" onClick={() => onNavigate(historyRoute)}>
            {historyLabel}
          </button>
          {failed && ro.onRetry && <button type="button" className="button small" onClick={ro.onRetry}>重试</button>}
          {!loading && <button type="button" className="button small" onClick={() => onNavigate('settings/plugins')}>插件管理</button>}
        </div>
      </div>
    </section>
  )
}
