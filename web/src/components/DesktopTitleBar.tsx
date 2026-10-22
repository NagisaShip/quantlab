/**
 * 无边框桌面窗口的自绘标题栏（只在 Tauri 桌面壳里渲染）。
 *
 * - 窗口本身 `decorations: false`，标题栏、拖动、最小化/最大化/关闭都由这里负责；
 * - `data-tauri-drag-region` 由 Tauri 注入的脚本处理拖动，双击最大化需要 capability 里的
 *   `core:window:allow-internal-toggle-maximize`；
 * - 关闭走 `getCurrentWindow().close()`，仍然会触发后端「关窗行为」（询问 / 托盘 / 退出）。
 */
import { useEffect, useState } from 'react'
import { LogoMark } from './icons'
import { desktopKind } from '../native'
import { request } from '../http'
import { useRecentResult } from '../features/workflows/recentResult'
import RecentResultAction from './RecentResultAction'

type TauriWindow = {
  minimize: () => Promise<void>
  toggleMaximize: () => Promise<void>
  close: () => Promise<void>
}

function currentWindow(): TauriWindow | null {
  const api = (window as unknown as { __TAURI__?: { window?: { getCurrentWindow?: () => TauriWindow } } }).__TAURI__
  try {
    return api?.window?.getCurrentWindow?.() ?? null
  } catch {
    return null
  }
}

export default function DesktopTitleBar() {
  const isTauri = desktopKind() === 'tauri'
  const [local, setLocal] = useState(false)
  const [projectName, setProjectName] = useState('')
  const recent = useRecentResult(isTauri && local)

  useEffect(() => {
    if (!isTauri) return
    document.documentElement.dataset.desktopFrame = 'tauri'
    return () => { delete document.documentElement.dataset.desktopFrame }
  }, [isTauri])

  useEffect(() => {
    if (!isTauri) return
    const controller = new AbortController()
    request<{ mode?: string; project?: { name?: string } }>('/api/local/info', { signal: controller.signal })
      .then((info) => {
        if (controller.signal.aborted) return
        setLocal(info.mode === 'local')
        setProjectName(info.project?.name || '')
      })
      .catch(() => { if (!controller.signal.aborted) setLocal(false) })
    return () => controller.abort()
  }, [isTauri])

  if (!isTauri) return null
  const win = currentWindow()
  const run = (action: keyof TauriWindow) => { void win?.[action]?.() }

  return (
    <header className="dtb" data-tauri-drag-region>
      <div className="dtb-brand" data-tauri-drag-region>
        <LogoMark size={15} />
        <span className="dtb-name" data-tauri-drag-region>QuantLab</span>
      </div>
      <div className="dtb-spacer" data-tauri-drag-region />
      {local && <RecentResultAction summary={recent.summary} phase={recent.phase} error={recent.error} projectName={projectName} onReload={() => { void recent.reload() }} />}
      <div className="dtb-actions">
        <button type="button" className="dtb-btn" aria-label="最小化" title="最小化" onClick={() => run('minimize')}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
            <path d="M2 5h6" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </button>
        <button type="button" className="dtb-btn" aria-label="最大化或还原" title="最大化或还原" onClick={() => run('toggleMaximize')}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
            <rect x="2" y="2" width="6" height="6" fill="none" stroke="currentColor" strokeWidth="1.1" />
          </svg>
        </button>
        <button type="button" className="dtb-btn dtb-close" aria-label="关闭" title="关闭" onClick={() => run('close')}>
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden>
            <path d="m2.5 2.5 5 5M7.5 2.5l-5 5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
          </svg>
        </button>
      </div>
    </header>
  )
}
