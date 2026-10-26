import { useEffect, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { desktopKind } from '../native'

/** 桌面共用窗口栏，浏览器保留原来的页面顶栏。 */
export default function AppTopBar({ children }: { children: ReactNode }) {
  const [host, setHost] = useState<HTMLElement | null>(null)
  const desktop = desktopKind() === 'tauri'
  useEffect(() => {
    if (desktop) setHost(document.getElementById('desktop-topbar-slot'))
  }, [desktop])
  const bar = <div className="topbar" role="navigation" aria-label="主导航">{children}</div>
  return desktop ? (host ? createPortal(bar, host) : null) : bar
}
