import { useEffect, useRef, useState } from 'react'
import { directBars } from '../marketDirect'
import { get } from '../http'
import { LogoMark, Icon } from './icons'
import { getTheme, getUpDown, setTheme, setUpDown, type ThemePref, type UpDown } from './prefs'

interface Quote { symbol: string; price: number; change: number; source: string }

const SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']

async function fetchQuote(symbol: string): Promise<Quote | null> {
  try {
    let body: { source: string; bars: { c: number }[] }
    // 行情带每分钟刷新：由浏览器直连交易所，不经服务器转发
    const direct = await directBars(symbol, '1h', 25)
    if (direct) body = { source: 'exchange', bars: direct }
    else {
      try {
        body = await get(`/api/market/bars?symbol=${encodeURIComponent(symbol)}&timeframe=1h&limit=25`)
      } catch {
        return null
      }
    }
    const bars = body.bars
    if (bars.length < 2) return null
    const last = bars[bars.length - 1].c
    const first = bars[0].c
    return { symbol, price: last, change: first ? last / first - 1 : 0, source: body.source }
  } catch {
    return null
  }
}

const fmtPrice = (v: number) => v.toLocaleString('en-US', { minimumFractionDigits: v >= 100 ? 2 : 4, maximumFractionDigits: v >= 100 ? 2 : 4 })

/** 顶部行情带：最近 24 根 1 小时线的涨跌。合成数据会明确标出。 */
export default function Tape() {
  const [quotes, setQuotes] = useState<Quote[]>([])
  const [loaded, setLoaded] = useState(false)
  const [themePref, setThemePref] = useState<ThemePref>(getTheme())
  const [updown, setUpDownState] = useState<UpDown>(getUpDown())
  const [notice, setNotice] = useState<{ title: string; detail?: string } | null>(null)
  const noticeTimer = useRef<number | null>(null)
  const showNotice = (title: string, detail?: string) => {
    setNotice({ title, detail })
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => setNotice(null), 3200)
  }
  useEffect(() => () => { if (noticeTimer.current) window.clearTimeout(noticeTimer.current) }, [])

  useEffect(() => {
    let alive = true
    const load = async () => {
      const qs = (await Promise.all(SYMBOLS.map(fetchQuote))).filter((q): q is Quote => q !== null)
      if (alive) { setQuotes(qs); setLoaded(true) }
    }
    load()
    const t = window.setInterval(load, 60_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  const synthetic = quotes.some((q) => q.source !== 'exchange')
  const toggleTheme = () => {
    const next: ThemePref = themePref === 'light' ? 'dark' : themePref === 'dark' ? 'system' : 'light'
    setTheme(next)
    setThemePref(next)
    showNotice(next === 'system' ? '主题：跟随系统' : next === 'dark' ? '主题：夜间' : '主题：浅色', next === 'system' ? '随系统深浅色自动切换' : undefined)
  }
  const toggleUpDown = () => {
    const next: UpDown = updown === 'red' ? 'green' : 'red'
    setUpDown(next)
    setUpDownState(next)
    showNotice(
      next === 'red' ? '已切换为红涨绿跌' : '已切换为绿涨红跌',
      next === 'red' ? '红色=上涨，绿色=下跌；只改颜色，不改数值。' : '绿色=上涨，红色=下跌；只改颜色，不改数值。',
    )
  }
  const themeLabel = themePref === 'light' ? '浅色' : themePref === 'dark' ? '夜间' : '跟随系统'
  const nextTheme = themePref === 'light' ? '夜间' : themePref === 'dark' ? '跟随系统' : '浅色'

  return (
    <div className="tape" role="region" aria-label="行情">
      <a className="tape-brand" href="#/home" style={{ textDecoration: 'none' }}>
        <LogoMark />
        <span className="tape-brand-copy"><span>QuantLab</span><small>v 0.1 preview</small></span>
      </a>
      <div className="tape-quotes">
        {quotes.map((q) => (
          <span className="quote" key={q.symbol} title={`${q.symbol} 最近 24 小时（1h K 线）`}>
            <b>{q.symbol.split('/')[0]}</b>
            <span>{fmtPrice(q.price)}</span>
            <em className={q.change >= 0 ? 'up' : 'down'}>{q.change >= 0 ? '+' : ''}{(q.change * 100).toFixed(2)}%</em>
          </span>
        ))}
        {!quotes.length && <span className="quote"><span>{loaded ? '未接入行情数据' : '行情加载中…'}</span></span>}
      </div>
      {quotes.length > 0 && <span className="tape-src" title={synthetic ? '部分标的不是交易所实时行情' : '交易所公共行情'}>{synthetic ? '部分未接入' : '实时行情 · 24h'}</span>}
      <div className="tape-tools">
        <button className="tape-btn" onClick={toggleUpDown} title="切换涨跌配色" aria-label={updown === 'red' ? '当前红涨绿跌，点击切换为绿涨红跌' : '当前绿涨红跌，点击切换为红涨绿跌'}>
          <span className="swatch"><i style={{ background: 'var(--up)' }} /><i style={{ background: 'var(--down)' }} /></span>
          {updown === 'red' ? '红涨绿跌' : '绿涨红跌'}
        </button>
        <button className="tape-btn" onClick={toggleTheme} aria-label={`当前主题：${themeLabel}；点击切换为${nextTheme}`} title={`当前主题：${themeLabel}；点击切换为${nextTheme}`}>
          <Icon name={themePref === 'light' ? 'sun' : themePref === 'dark' ? 'moon' : 'monitor'} size={13} />
          {themeLabel}
        </button>
      </div>
      {notice && <div className="tape-explain anim-in" role="status" aria-live="polite"><b>{notice.title}</b>{notice.detail && <span>{notice.detail}</span>}</div>}
    </div>
  )
}
