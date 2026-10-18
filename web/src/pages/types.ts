/** 各页面共用的 props。App 统一注入导航、提示与忙碌遮罩。 */
import type { Me } from '../api/platform'

export interface PageProps {
  onNavigate: (key: string) => void
  notify: (message: string, error?: boolean) => void
  setBusy: (b: { title: string; note?: string } | null) => void
  /** 工作区有效功能开关；空对象表示尚未加载，服务端仍会强制校验。 */
  features?: Record<string, boolean>
  me?: Me
}
