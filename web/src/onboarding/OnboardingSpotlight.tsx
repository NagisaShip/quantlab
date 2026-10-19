import { useEffect, useState } from 'react'

/** 只负责画高亮环，不拦截点击；导师卡负责操作按钮。 */
export default function OnboardingSpotlight({ target }: { target?: string }) {
  const [rect, setRect] = useState<DOMRect | null>(null)

  useEffect(() => {
    if (!target) { setRect(null); return }
    let el: HTMLElement | null = null
    let ro: ResizeObserver | null = null
    let raf = 0

    const update = () => {
      el = document.querySelector<HTMLElement>(`[data-tour="${target}"]`)
      if (!el) { setRect(null); return }
      setRect(el.getBoundingClientRect())
    }

    raf = requestAnimationFrame(() => {
      update()
      el?.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' })
      ro = new ResizeObserver(update)
      if (el) ro.observe(el)
      window.addEventListener('scroll', update, true)
      window.addEventListener('resize', update)
    })
    return () => {
      cancelAnimationFrame(raf)
      ro?.disconnect()
      window.removeEventListener('scroll', update, true)
      window.removeEventListener('resize', update)
    }
  }, [target])

  if (!rect) return null
  const pad = 7
  return (
    <div
      className="onboarding-spotlight"
      aria-hidden="true"
      style={{
        left: rect.left - pad,
        top: rect.top - pad,
        width: rect.width + pad * 2,
        height: rect.height + pad * 2,
      }}
    />
  )
}
