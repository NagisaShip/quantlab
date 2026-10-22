/**
 * 关于 QuantLab：点击左上角品牌、账号菜单或设置页底部的「关于 QuantLab」打开。
 * 只展示版本、定位、能力边界和开发者入口，不做任何网络请求。
 */
import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { LogoMark } from './icons'
import './stylesAbout.css'

const LINKS = [
  { label: 'GitHub · NagisaShip', href: 'https://github.com/NagisaShip' },
  { label: 'GitHub · jamesusa99', href: 'https://github.com/jamesusa99' },
  { label: 'luoyangyuan110@gmail.com', href: 'mailto:luoyangyuan110@gmail.com' },
]

const HIGHLIGHTS = ['本地优先', '研究可复现', '证据可追溯', '不下真实订单']

export default function AboutDialog({ open, onClose, onNavigate }: {
  open: boolean
  onClose: () => void
  onNavigate?: (key: string) => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (!open) return
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); onClose() } }
    document.addEventListener('keydown', esc)
    const t = window.setTimeout(() => closeRef.current?.focus(), 20)
    const previous = document.activeElement as HTMLElement | null
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', esc)
      window.clearTimeout(t)
      document.body.style.overflow = overflow
      previous?.focus?.()
    }
  }, [open, onClose])
  if (!open) return null
  return createPortal(
    <div className="about-backdrop" role="presentation" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <section className="about-card" role="dialog" aria-modal="true" aria-label="关于 QuantLab">
        <button ref={closeRef} type="button" className="about-close" onClick={onClose} aria-label="关闭关于窗口">×</button>
        <header className="about-hero">
          <LogoMark size={46} />
          <div className="about-title">
            <h2>QuantLab</h2>
            <span className="about-version">v0.1.0-preview.2</span>
          </div>
        </header>
        <p className="about-lead">先把研究做扎实，再谈真金白银。</p>
        <p className="about-text">
          QuantLab 是一个面向加密货币量化研究的开源工作台：AI Agent、函数卡片工作流、真实行情与可复现实验记录都在同一条证据链里。
          本地模式把项目、数据、运行环境和证据留在你自己的文件夹，不依赖平台账号。
        </p>
        <ul className="about-chips">
          {HIGHLIGHTS.map((h) => <li key={h}>{h}</li>)}
        </ul>
        <dl className="about-facts">
          <div><dt>研究边界</dt><dd>仅用于研究与模拟，不构成投资建议</dd></div>
          <div><dt>运行方式</dt><dd>本地项目模式 / 账号协作模式</dd></div>
          <div><dt>开源协议</dt><dd>MIT License</dd></div>
        </dl>
        <footer className="about-foot">
          <div className="about-links">
            {LINKS.map((l) => <a key={l.href} href={l.href} target="_blank" rel="noreferrer">{l.label}</a>)}
          </div>
          <div className="about-actions">
            {onNavigate && (
              <button type="button" className="button" onClick={() => { onClose(); onNavigate('docs') }}>API 文档</button>
            )}
            <button type="button" className="button primary" onClick={onClose}>关闭</button>
          </div>
        </footer>
      </section>
    </div>,
    document.body,
  )
}
