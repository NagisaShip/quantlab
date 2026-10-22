import { useEffect, useRef, type ReactNode } from 'react'

export default function WorkbenchDialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLElement | null>(null)
  const close = useRef(onClose); close.current = onClose
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    panel.current?.querySelector<HTMLButtonElement>('button')?.focus()
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close.current(); return }
      if (event.key !== 'Tab') return
      const controls = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href],[tabindex="0"]') || []).filter(e=>e.offsetParent !== null)
      const first = controls[0]; const last = controls[controls.length - 1]
      if (!first) return
      if (event.shiftKey && (document.activeElement === first || !panel.current?.contains(document.activeElement))) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && (document.activeElement === last || !panel.current?.contains(document.activeElement))) { event.preventDefault(); first.focus() }
    }
    window.addEventListener('keydown',keyboard,true)
    return () => { window.removeEventListener('keydown',keyboard,true); previous?.focus() }
  },[])
  return <div className="wf-modal-backdrop"><section ref={panel} className="wf-modal" role="dialog" aria-modal="true" aria-label={title}>{children}</section></div>
}
