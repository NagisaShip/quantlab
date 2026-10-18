const MIN_CARD_COLUMNS = 5

function norm(text: string | null | undefined): string {
  return (text || '').replace(/\s+/g, ' ').trim()
}

function annotate(table: HTMLTableElement): void {
  const headers = [...(table.tHead?.rows[0]?.cells || [])].map((cell) => norm(cell.textContent))
  if (headers.length < MIN_CARD_COLUMNS) {
    delete table.dataset.mobileCards
    return
  }
  table.dataset.mobileCards = 'true'
  const titleIndex = headers.findIndex(Boolean)
  for (const body of table.tBodies) {
    for (const row of body.rows) {
      ;[...row.cells].forEach((cell, index) => {
        const label = headers[index] || ''
        if (label) cell.dataset.label = label
        else delete cell.dataset.label
        cell.classList.toggle('mobile-card-title', index === titleIndex)
        cell.classList.toggle('mobile-card-leading', !label && index === 0)
        cell.classList.toggle('mobile-card-actions', !label && index > 0)
        cell.classList.toggle('mobile-card-wide', cell.colSpan > 1)
      })
    }
  }
}

function scan(): void {
  document.querySelectorAll<HTMLTableElement>('.table-wrap > table').forEach(annotate)
}

/** Dynamically mark wide tables so CSS can turn rows into cards on phones without horizontal scrolling. */
export function startMobileTableCards(): void {
  let frame = 0
  const schedule = () => {
    if (frame) return
    frame = window.requestAnimationFrame(() => {
      frame = 0
      scan()
    })
  }
  scan()
  const observer = new MutationObserver(schedule)
  observer.observe(document.body, { childList: true, subtree: true })
  window.addEventListener('resize', schedule, { passive: true })
}
