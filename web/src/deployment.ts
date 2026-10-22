let mode = 'account'

/**
 * 部署模式是全局事实，不是某个异步 chunk 的私有状态。
 *
 * Vite 会把很小的模块拆进多个 chunk，`import { mode }` 的副本可能各自初始化；
 * 用 window 上的标记做单一事实源，保证 http 层在 App 完成 /api/auth/me 后立刻生效。
 */
const KEY = '__QUANTLAB_DEPLOYMENT_MODE__'

function readGlobal(): string {
  try {
    const value = (window as unknown as Record<string, unknown>)[KEY]
    return typeof value === 'string' ? value : ''
  } catch {
    return ''
  }
}

export const setDeploymentMode = (value?: string) => {
  mode = value === 'local' ? 'local' : 'account'
  try {
    ;(window as unknown as Record<string, unknown>)[KEY] = mode
  } catch {
    /* 隐私模式忽略 */
  }
}

export const isLocalDeployment = () => (readGlobal() || mode) === 'local'
