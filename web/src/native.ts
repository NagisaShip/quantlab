/**
 * 桌面外壳适配层：Tauri（新） / pywebview（旧） / 纯浏览器。
 *
 * 页面本身由本机 Python 后端提供（同源），业务请求仍走相对路径 `/api/*`；
 * 这里只封装必须由桌面壳完成的动作：
 * - 系统文件 / 文件夹选择器
 * - 在新窗口打开另一个本地项目
 * - 接收从系统拖入窗口的文件绝对路径（浏览器拖拽拿不到真实路径）
 */

interface PywebviewApi {
  choose_folder: () => Promise<string[] | null>
  choose_files: () => Promise<string[] | null>
  open_project?: (path: string) => Promise<unknown>
}

interface TauriGlobal {
  core?: { invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown> }
  event?: {
    listen: (name: string, cb: (event: { payload: unknown }) => void) => Promise<() => void>
  }
}

interface TauriInternals {
  invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>
}

declare global {
  interface Window {
    pywebview?: { api: PywebviewApi }
    __TAURI__?: TauriGlobal
    __TAURI_INTERNALS__?: TauriInternals
  }
}

export type DesktopKind = 'tauri' | 'pywebview' | 'web'

export function desktopKind(): DesktopKind {
  if (typeof window === 'undefined') return 'web'
  if (window.__TAURI__?.core?.invoke || window.__TAURI_INTERNALS__?.invoke) return 'tauri'
  if (window.pywebview?.api) return 'pywebview'
  return 'web'
}

export const isDesktop = () => desktopKind() !== 'web'

function invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const api = window.__TAURI__?.core?.invoke
  if (api) return api(cmd, args) as Promise<T>
  const internals = window.__TAURI_INTERNALS__?.invoke
  if (internals) return internals(cmd, args) as Promise<T>
  return Promise.reject(new Error('桌面桥接不可用'))
}

async function call<T>(cmd: string, args?: Record<string, unknown>): Promise<T | null> {
  try {
    return await invoke<T>(cmd, args)
  } catch {
    return null
  }
}

/** 选择一个文件夹；浏览器里返回空数组，由调用方提示手动粘贴路径。 */
export async function chooseFolder(): Promise<string[]> {
  if (window.pywebview?.api) {
    try { return (await window.pywebview.api.choose_folder()) ?? [] } catch { return [] }
  }
  if (desktopKind() === 'tauri') {
    return (await call<string[]>('native_choose_folder')) ?? []
  }
  return []
}

/** 选择一个或多个文件。 */
export async function chooseFiles(): Promise<string[]> {
  if (window.pywebview?.api) {
    try { return (await window.pywebview.api.choose_files()) ?? [] } catch { return [] }
  }
  if (desktopKind() === 'tauri') {
    return (await call<string[]>('native_choose_files')) ?? []
  }
  return []
}

/** 在独立窗口打开另一个本地项目；返回是否由桌面壳接管。 */
export async function openProjectWindow(path: string): Promise<boolean> {
  if (window.pywebview?.api?.open_project) {
    try { await window.pywebview.api.open_project(path); return true } catch { return false }
  }
  if (desktopKind() === 'tauri') {
    return (await call<boolean>('native_open_project', { path })) === true
  }
  return false
}

/** 在资源管理器中选中文件/文件夹（仅 Tauri）；浏览器返回 false。 */
export async function revealPath(path: string): Promise<boolean> {
  if (desktopKind() !== 'tauri') return false
  return (await call<boolean>('native_reveal_path', { path })) === true
}

/** 用系统默认应用打开文件/文件夹（仅 Tauri）；浏览器返回 false。 */
export async function openWithSystem(path: string): Promise<boolean> {
  if (desktopKind() !== 'tauri') return false
  return (await call<boolean>('native_open_path', { path })) === true
}

/**
 * 订阅系统文件拖放。Tauri 默认拦截原生拖放（页面收不到 File 对象），
 * 由 Rust 侧把绝对路径通过 `quantlab://files-dropped` 事件转发过来。
 */
export function onDroppedFiles(handler: (paths: string[]) => void): () => void {
  const listen = window.__TAURI__?.event?.listen
  if (!listen) return () => undefined
  let stop: (() => void) | undefined
  let cancelled = false
  listen('quantlab://files-dropped', (event) => {
    const payload = event.payload
    if (Array.isArray(payload) && payload.every((x) => typeof x === 'string')) handler(payload as string[])
  })
    .then((unlisten) => { if (cancelled) unlisten(); else stop = unlisten })
    .catch(() => undefined)
  return () => { cancelled = true; stop?.() }
}
