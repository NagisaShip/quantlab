/**
 * 首页「关注」列表的设置卡片：选标的 + 选折线的时间区间。
 *
 * 为什么不复用 AI 研究室的 MarketSwitcher：那里是「对话用的标的与 K 线周期」（自动 / 250ms…1d），
 * 关注列表需要的是「看盘用的时间区间」（1 天 / 7 天 / 1 月 / 3 月 / 半年 / 1 年）——两者口径不同，
 * 而且对话的周期里有一堆亚秒级选项，放在看盘卡片里只会误导。这里单独维护一份。
 */
import { useEffect, useState } from 'react'
import { usePopover } from '../../components/chat/Composer'
import { syncData, useSymbols } from '../lab/research'

export interface WatchRange {
  key: string
  /** 菜单里显示的名字 */
  label: string
  /** 卡片/涨跌幅角标上的短标签 */
  short: string
  /** 折线取数用的 K 线周期 */
  tf: string
  /** 打开行情与数据页时对应的「时间段」键（和数据页的 RANGES 一致） */
  dataRange: string
  /** 覆盖该区间需要的 K 线根数 */
  bars: number
  /** 同步到本地行情库时取多少天 */
  days: number
}

export const WATCH_RANGES: WatchRange[] = [
  { key: '1d', label: '1 天', short: '1d', tf: '15m', dataRange: '1d', bars: 96, days: 1 },
  { key: '7d', label: '7 天', short: '7d', tf: '1h', dataRange: '1w', bars: 168, days: 7 },
  { key: '1m', label: '1 月', short: '1m', tf: '4h', dataRange: '1m', bars: 186, days: 30 },
  { key: '3m', label: '3 月', short: '3m', tf: '12h', dataRange: '3m', bars: 186, days: 90 },
  { key: '6m', label: '6 月', short: '6m', tf: '1d', dataRange: '6m', bars: 186, days: 180 },
  { key: '1y', label: '1 年', short: '1y', tf: '1d', dataRange: '1y', bars: 370, days: 365 },
]

export const DEFAULT_WATCH_RANGE = '7d'
export const WATCH_RANGE_KEY = 'qa.home.watchRange'

/** 旧版本只存了 K 线周期，按「原来展示的时间跨度」迁移到新区间。 */
const LEGACY_TF_RANGE: Record<string, string> = { '1h': '7d', '4h': '1m', '1d': '3m' }

export function watchRange(key: string | undefined): WatchRange {
  return WATCH_RANGES.find((r) => r.key === key) ?? WATCH_RANGES.find((r) => r.key === DEFAULT_WATCH_RANGE)!
}

/** 读取本机保存的区间；没有就按旧的 qa.home.watchTf 迁移。 */
export function storedWatchRange(read: (key: string, fallback: string) => string): string {
  const saved = read(WATCH_RANGE_KEY, '')
  if (WATCH_RANGES.some((r) => r.key === saved)) return saved
  return LEGACY_TF_RANGE[read('qa.home.watchTf', '')] ?? DEFAULT_WATCH_RANGE
}

export function WatchSettings({ symbols, range, onChange, notify }: {
  symbols: string[]
  range: string
  onChange: (symbols: string[], range: string) => void
  notify: (msg: string, error?: boolean) => void
}) {
  const pop = usePopover()
  const all = useSymbols()
  const [sel, setSel] = useState<string[]>(symbols)
  const [rng, setRng] = useState(range)
  const [custom, setCustom] = useState('')
  const [syncing, setSyncing] = useState(false)
  const current = watchRange(rng)
  // 每次打开弹层都回到已保存的值，取消不会留下半改的状态
  useEffect(() => {
    if (!pop.open) return
    setSel(symbols)
    setRng(range)
    setCustom('')
  }, [pop.open]) // eslint-disable-line react-hooks/exhaustive-deps

  const options = [...new Set([...symbols, ...all])]
  const label = !symbols.length ? '未设置' : symbols.length > 1 ? `${symbols[0]} 等 ${symbols.length} 个` : symbols[0]
  const toggle = (s: string) => setSel((cur) => (cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s]))
  const addCustom = () => {
    const v = custom.trim().toUpperCase()
    if (!v) return
    const s = v.includes('/') ? v : `${v}/USDT`
    setSel((cur) => [...new Set([...cur, s])])
    setCustom('')
  }
  const doSync = async () => {
    setSyncing(true)
    try {
      const r = await syncData(sel, current.tf, current.days)
      const bad = r.results.filter((x) => !x.ok || x.coverage < 0.9)
      notify(bad.length
        ? `已同步，${bad.map((x) => `${x.symbol} 覆盖 ${Math.round((x.coverage || 0) * 100)}%`).join('；')}`
        : `已同步 ${r.results.length} 个标的近 ${current.days} 天 ${current.tf} 行情`, bad.some((x) => !x.ok))
    } catch (e) { notify(`同步失败：${(e as Error).message}`, true) } finally { setSyncing(false) }
  }

  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button type="button" className={`mk-switch compact${pop.open ? ' on' : ''}`} onClick={() => pop.setOpen(!pop.open)}
        aria-haspopup="dialog" aria-expanded={pop.open}
        aria-label={`关注列表设置：标的 ${label}，折线区间 ${current.label}`}
        title={`当前 ${label} · 折线区间 ${current.label}；点击设置标的与时间区间`}>
        <span className="mk-ico" aria-hidden>
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round">
            <rect x="1.6" y="2.4" width="8.8" height="8" rx="1.4" /><path d="M1.6 4.8h8.8M3.8 1.4v2M8.2 1.4v2" />
          </svg>
        </span>
        <span className="mk-sym">{label}</span>
        <span className="mk-tf">{current.short}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
      </button>
      {pop.open && (
        <div ref={pop.menuRef} className="cmp-menu cmp-menu-right mk-menu watch-menu" style={pop.menuStyle} data-placement={pop.placement} role="dialog" aria-label="关注列表设置">
          <div className="cmp-menu-label">标的（可多选，首页最多显示这些）</div>
          <div className="mk-chips">
            {options.map((s) => (
              <button key={s} type="button" className={`chip${sel.includes(s) ? ' on' : ''}`} aria-pressed={sel.includes(s)} onClick={() => toggle(s)}>{s.replace('/USDT', '')}</button>
            ))}
          </div>
          <form className="mk-add" onSubmit={(e) => { e.preventDefault(); addCustom() }}>
            <input value={custom} onChange={(e) => setCustom(e.target.value)} placeholder="添加其他标的，如 TON 或 ARB/USDT" aria-label="添加其他标的" />
            <button type="submit" className="button small">添加</button>
          </form>
          <div className="cmp-menu-label">折线时间区间</div>
          <div className="mk-ranges" role="group" aria-label="折线时间区间">
            {WATCH_RANGES.map((r) => (
              <button key={r.key} type="button" className={r.key === rng ? 'on' : ''} aria-pressed={r.key === rng} title={`折线与涨跌幅按最近 ${r.label} 计算`} onClick={() => setRng(r.key)}>{r.label}</button>
            ))}
          </div>
          <div className="cmp-menu-hint">折线、卡片上的涨跌幅都按这个区间计算；点卡片可打开行情与数据继续看 K 线。</div>
          <div className="mk-foot">
            <button type="button" className="button small ghost" disabled={syncing || !sel.length} onClick={doSync}
              title="从交易所同步对应区间的真实行情到本地（补齐缺口）">{syncing ? '同步中…' : '同步数据'}</button>
            <span className="lab2-card-gap" />
            <button type="button" className="button small" onClick={() => pop.setOpen(false)}>取消</button>
            <button type="button" className="button small primary" disabled={!sel.length}
              title={sel.length ? '应用标的与时间区间' : '至少保留一个标的'} onClick={() => { onChange(sel, rng); pop.setOpen(false) }}>应用</button>
          </div>
        </div>
      )}
    </div>
  )
}
