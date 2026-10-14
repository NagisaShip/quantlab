import { useEffect, useState } from 'react'
import { get } from '../http'
import { LogoMark, Icon } from './icons'
import { getTheme, getUpDown, resolvedTheme, setTheme, setUpDown, type UpDown } from './prefs'

interface Quote { symbol: string; price: number; change: number; source: string }

const SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']

async function fetchQuote(symbol: string): Promise<Quote | null> {
  try {
    let body: { source: string; bars: { c: number }[] }
    try {
      body = await get(`/api/market/bars?symbol=${encodeURIComponent(symbol)}&timeframe=1h&limit=25`)
    } catch {
      return null
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
  const [theme, setThemeState] = useState(resolvedTheme())
  const [updown, setUpDownState] = useState<UpDown>(getUpDown())

  useEffect(() => {
    let alive = true
    const load = async () => {
      const qs = (await Promise.all(SYMBOLS.map(fetchQuote))).filter((q): q is Quote => q !== null)
      if (alive) setQuotes(qs)
    }
    load()
    const t = window.setInterval(load, 60_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  const synthetic = quotes.some((q) => q.source !== 'exchange')
  const toggleTheme = () => {
    const next = resolvedTheme(getTheme()) === 'dark' ? 'light' : 'dark'
    setTheme(next)
    setThemeState(next)
  }
  const toggleUpDown = () => {
    const next: UpDown = updown === 'red' ? 'green' : 'red'
    setUpDown(next)
    setUpDownState(next)
  }

  return (
    <div className="tape" role="region" aria-label="行情">
      <a className="tape-brand" href="#/home" style={{ textDecoration: 'none' }}>
        <LogoMark />
        QuantLab<small>量化研究台</small>
      </a>
      <div className="tape-quotes">
        {quotes.map((q) => (
          <span className="quote" key={q.symbol} title={`${q.symbol} 最近 24 小时（1h K 线）`}>
            <b>{q.symbol.split('/')[0]}</b>
            <span>{fmtPrice(q.price)}</span>
            <em className={q.change >= 0 ? 'up' : 'down'}>{q.change >= 0 ? '+' : ''}{(q.change * 100).toFixed(2)}%</em>
          </span>
        ))}
        {!quotes.length && <span className="quote"><span>行情加载中…</span></span>}
      </div>
      {quotes.length > 0 && <span className="tape-src" title={synthetic ? '交易所接口不可达，显示的是教学合成数据' : '交易所公共行情'}>{synthetic ? '合成行情' : '实时行情 · 24h'}</span>}
      <div className="tape-tools">
        <button className="tape-btn" onClick={toggleUpDown} title="切换涨跌配色" aria-label={updown === 'red' ? '当前红涨绿跌，点击切换为绿涨红跌' : '当前绿涨红跌，点击切换为红涨绿跌'}>
          <span className="swatch"><i style={{ background: 'var(--up)' }} /><i style={{ background: 'var(--down)' }} /></span>
          {updown === 'red' ? '红涨绿跌' : '绿涨红跌'}
        </button>
        <button className="tape-btn" onClick={toggleTheme} aria-label={theme === 'dark' ? '切换到浅色' : '切换到夜间'} title={theme === 'dark' ? '切换到浅色' : '切换到夜间'}>
          <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={13} />
          {theme === 'dark' ? '浅色' : '夜间'}
        </button>
      </div>
    </div>
  )
}
