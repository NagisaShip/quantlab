import type { ReactNode } from 'react'
import { QuantBot } from './QuantBot'

/** 16px 线性图标。stroke 跟随 currentColor，深浅主题自动适配。 */
const P: Record<string, ReactNode> = {
  home: <><path d="M2.5 7.2 8 2.8l5.5 4.4" /><path d="M4 6.2V13h3V9.5h2V13h3V6.2" /></>,
  strategies: <><path d="M5.5 4 2.5 8l3 4" /><path d="M10.5 4l3 4-3 4" /><path d="M9 2.8 7 13.2" /></>,
  backtests: <><path d="M2.5 13.5h11" /><path d="M3 11l3-3.5 2.5 2 4.5-5.5" /><path d="M10.5 4h2.5v2.5" /></>,
  paper: <><circle cx="8" cy="8" r="2" /><path d="M4.2 11.8a5.4 5.4 0 0 1 0-7.6" /><path d="M11.8 4.2a5.4 5.4 0 0 1 0 7.6" /></>,
  square: <><path d="M2.5 3.5h8v5.5H6l-2.5 2v-2h-1z" /><path d="M10.5 6h3v5.5h-1v2l-2.5-2H7" /></>,
  overview: <><rect x="2.5" y="2.5" width="4.5" height="4.5" rx=".8" /><rect x="9" y="2.5" width="4.5" height="4.5" rx=".8" /><rect x="2.5" y="9" width="4.5" height="4.5" rx=".8" /><rect x="9" y="9" width="4.5" height="4.5" rx=".8" /></>,
  arena: <><path d="M3.5 13.5V2.5" /><path d="M3.5 3h8l-1.5 2.5L11.5 8h-8" /></>,
  strategy: <><path d="M3 4.5h10M3 8h10M3 11.5h10" /><circle cx="6" cy="4.5" r="1.3" fill="var(--rail)" /><circle cx="10.5" cy="8" r="1.3" fill="var(--rail)" /><circle cx="5" cy="11.5" r="1.3" fill="var(--rail)" /></>,
  experiments: <><path d="M6 2.5h4M6.8 2.5v4L3 12.6a.8.8 0 0 0 .7 1.2h8.6a.8.8 0 0 0 .7-1.2L9.2 6.5v-4" /><path d="M4.8 10h6.4" /></>,
  lab: <><path d="M8 2.2l1.4 3.4 3.4 1.4-3.4 1.4L8 11.8 6.6 8.4 3.2 7l3.4-1.4z" /><path d="M12.5 11v3M11 12.5h3" /></>,
  data: <><ellipse cx="8" cy="4" rx="5" ry="1.8" /><path d="M3 4v8c0 1 2.2 1.8 5 1.8s5-.8 5-1.8V4" /><path d="M3 8c0 1 2.2 1.8 5 1.8S13 9 13 8" /></>,
  teams: <><circle cx="6" cy="5.5" r="2" /><path d="M2.5 13c0-2 1.6-3.5 3.5-3.5S9.5 11 9.5 13" /><circle cx="11" cy="6" r="1.6" /><path d="M10.5 9.6c1.7 0 3 1.3 3 3" /></>,
  roles: <><rect x="2.5" y="3" width="11" height="10" rx="1.2" /><circle cx="6" cy="7" r="1.4" /><path d="M4 11c.3-1 1-1.6 2-1.6s1.7.6 2 1.6M9.5 6.5h2.5M9.5 9h2.5" /></>,
  'long-running': <><circle cx="8" cy="8" r="5.5" /><path d="M8 5v3.2l2.2 1.4" /></>,
  models: <><rect x="4" y="4" width="8" height="8" rx="1.2" /><rect x="6.3" y="6.3" width="3.4" height="3.4" rx=".5" /><path d="M6 2v2M10 2v2M6 12v2M10 12v2M2 6h2M2 10h2M12 6h2M12 10h2" /></>,
  factors: <><path d="M10.5 2.8c-1.8 0-2.3 1-2.6 2.6L6.6 11c-.3 1.4-.8 2.2-2.4 2.2" /><path d="M5 6.5h6" /></>,
  modellib: <><circle cx="3.8" cy="5" r="1.3" /><circle cx="3.8" cy="11" r="1.3" /><circle cx="8" cy="8" r="1.3" /><circle cx="12.2" cy="8" r="1.3" /><path d="M5 5.6l1.9 1.6M5 10.4l1.9-1.6M9.3 8h1.6" /></>,
  portfolio: <><circle cx="8" cy="8" r="5.2" /><path d="M8 2.8V8l4.4 2.8" /></>,
  execution: <><path d="M2.5 4.5h6M2.5 8h8M2.5 11.5h10" /><path d="M11 3l2 1.5-2 1.5" /></>,
  assemble: <><rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" /><rect x="9" y="2.5" width="4.5" height="4.5" rx="1" /><rect x="2.5" y="9" width="11" height="4.5" rx="1" /></>,
  discipline: <><path d="M8 2.5v11M4.5 13.5h7" /><path d="M3 5h10" /><path d="M3 5l-1.5 4h3zM13 5l-1.5 4h3z" /></>,
  prompts: <><path d="M3 3h4.2c.5 0 .8.3.8.8V13c0-.6-.4-1-1-1H3z" /><path d="M13 3H8.8c-.5 0-.8.3-.8.8V13c0-.6.4-1 1-1h4z" /></>,
  docs: <><path d="M4 2.5h5.5L12 5v8.5H4z" /><path d="M9.5 2.5V5H12M6 8h4M6 10.5h4" /></>,
  live: <><path d="M2.5 5.5h9l-2-2" /><path d="M13.5 10.5h-9l2 2" /></>,
  accounts: <><circle cx="5.5" cy="8" r="2.5" /><path d="M8 8h5.5M11.5 8v2.2M13.5 8v1.6" /></>,
  demo: <><rect x="2.5" y="3" width="11" height="8" rx="1.5" /><path d="M5 9V7M8 9V5.5M11 9V6.5M6 13.5h4" /></>,
  validation: <><path d="M8 2.2 3 4v4c0 3 2.2 5 5 5.8 2.8-.8 5-2.8 5-5.8V4z" /><path d="M5.8 8.2 7.4 9.8 10.4 6.6" /></>,
  microlive: <><circle cx="8" cy="8" r="5.5" /><path d="M9.8 5.8C9.4 5.2 8.8 5 8 5c-1.1 0-1.9.6-1.9 1.4 0 1.9 3.9 1 3.9 2.9 0 .9-.9 1.6-2 1.6-.9 0-1.6-.3-2-1M8 3.8V5M8 10.9v1.3" /></>,
  settings: <><path d="M3 4h10M3 8h10M3 12h10" /><path d="M5.5 2.8v2.4M10.5 6.8v2.4M7 10.8v2.4" /></>,
  sun: <><circle cx="8" cy="8" r="2.6" /><path d="M8 1.8v1.4M8 12.8v1.4M1.8 8h1.4M12.8 8h1.4M3.6 3.6l1 1M11.4 11.4l1 1M3.6 12.4l1-1M11.4 4.6l1-1" /></>,
  moon: <path d="M12.8 9.6A5.2 5.2 0 0 1 6.4 3.2a5.2 5.2 0 1 0 6.4 6.4z" />,
}

export function Icon({ name, size = 16 }: { name: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.4}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {P[name] ?? P.docs}
    </svg>
  )
}

/** 品牌标：三根 K 线，最后一根收在最高处。 */
/** 品牌标识：QuantLab Bot（待机，偶尔眨眼）。造型与状态见 QuantBot.tsx。 */
export function LogoMark({ size = 18 }: { size?: number }) {
  return <QuantBot size={size} state="idle" title="QuantLab" />
}
