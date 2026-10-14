import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ago, v3 } from '../api2'
import { factors } from '../apiFactors'
import { models } from '../apiModels'
import { lab, missions } from '../apiLab'
import { Icon } from './icons'

/**
 * 全局搜索（Ctrl/⌘ + K）：按类别找 AI 研究产出——研究项目、策略、因子、模型、回测、团队讨论。
 * 打开时才拉取各列表，前端做子串匹配；没有输入时显示最近更新的条目。
 */

type Cat = 'lab' | 'strategy' | 'factor' | 'model' | 'backtest' | 'team'
interface Hit { cat: Cat; id: string; title: string; sub: string; at: number; go: string }

const CATS: { key: Cat; label: string; icon: string }[] = [
  { key: 'lab', label: 'AI 研究', icon: 'lab' },
  { key: 'strategy', label: '策略', icon: 'strategies' },
  { key: 'factor', label: '因子', icon: 'factors' },
  { key: 'model', label: '模型', icon: 'modellib' },
  { key: 'backtest', label: '回测', icon: 'backtests' },
  { key: 'team', label: '团队讨论', icon: 'teams' },
]
const CAT_OF = Object.fromEntries(CATS.map((c) => [c.key, c])) as Record<Cat, (typeof CATS)[number]>
const LAB_KIND: Record<string, string> = { strategy: '策略研究', factor: '因子研究', model: '模型研究', portfolio: '组合研究', execution: '执行研究' }

const ACTIONS = [
  { title: '新建 AI 研究', go: 'lab' },
  { title: '新建策略', go: 'strategies' },
  { title: '新建因子', go: 'factors/new' },
]

async function loadAll(): Promise<Hit[]> {
  const safe = <T,>(p: Promise<T>) => p.catch(() => null)
  const [lp, st, fa, mo, bt, mi] = await Promise.all([
    safe(lab.projects()), safe(v3.strategies()), safe(factors.list()), safe(models.list()),
    safe(v3.backtests(undefined, 100)), safe(missions.list()),
  ])
  const hits: Hit[] = []
  for (const p of lp?.projects ?? []) hits.push({ cat: 'lab', id: p.id, title: p.title || p.idea, sub: `${LAB_KIND[p.kind ?? 'strategy'] ?? 'AI 研究'} · ${p.versions_count} 个版本`, at: p.updated_at, go: `lab/${p.id}` })
  for (const s of st?.strategies ?? []) hits.push({ cat: 'strategy', id: s.slug, title: s.name, sub: `${s.symbol} · ${s.timeframe} · v${s.version}`, at: s.updated_at, go: `editor/${s.slug}` })
  for (const f of fa?.factors ?? []) hits.push({ cat: 'factor', id: f.id, title: f.name, sub: f.expression, at: f.updated_at, go: `factors/${f.id}` })
  for (const m of mo?.models ?? []) hits.push({ cat: 'model', id: m.id, title: m.name, sub: `${m.spec.model} · ${m.status}`, at: m.updated_at, go: `modellib/${m.id}` })
  for (const b of bt?.backtests ?? []) hits.push({ cat: 'backtest', id: b.id, title: b.strategy_name, sub: `${b.timeframe} · ${b.securities.join(', ')}${b.label ? ` · ${b.label}` : ''}`, at: b.created_at, go: `backtest/${b.id}` })
  for (const m of mi?.missions ?? []) hits.push({ cat: 'team', id: m.id, title: m.directive, sub: m.mode === 'pipeline' ? '流水线讨论' : '单周期讨论', at: m.updated_at, go: `teams/${m.id}` })
  return hits
}

export function SearchPalette({ onNavigate, onClose }: { onNavigate: (k: string) => void; onClose: () => void }) {
  const [q, setQ] = useState('')
  const [cat, setCat] = useState<Cat | ''>('')
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [sel, setSel] = useState(0)
  const listRef = useRef<HTMLDivElement>(null)

  useEffect(() => { loadAll().then(setHits) }, [])

  const results = useMemo(() => {
    const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean)
    const pool = (hits ?? []).filter((h) => !cat || h.cat === cat)
    const matched = words.length ? pool.filter((h) => words.every((w) => `${h.title} ${h.sub}`.toLowerCase().includes(w))) : pool
    return [...matched].sort((a, b) => b.at - a.at).slice(0, words.length || cat ? 50 : 8)
  }, [hits, q, cat])
  const actions = q.trim() ? [] : ACTIONS
  const total = results.length + actions.length

  useEffect(() => { setSel(0) }, [q, cat])
  useEffect(() => { listRef.current?.querySelector('[data-sel="true"]')?.scrollIntoView({ block: 'nearest' }) }, [sel])

  const go = (k: string) => { onClose(); onNavigate(k) }
  const choose = (i: number) => (i < results.length ? go(results[i].go) : actions[i - results.length] && go(actions[i - results.length].go))

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') onClose()
    else if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(s + 1, total - 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)) }
    else if (e.key === 'Enter' && total) { e.preventDefault(); choose(sel) }
    else if (e.key === 'Tab') {
      e.preventDefault()
      const keys: (Cat | '')[] = ['', ...CATS.map((c) => c.key)]
      setCat(keys[(keys.indexOf(cat) + (e.shiftKey ? keys.length - 1 : 1)) % keys.length])
    }
  }

  return (
    <div className="sp-backdrop" onMouseDown={onClose}>
      <div className="sp" role="dialog" aria-label="搜索" onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKey}>
        <div className="sp-input">
          <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索研究、策略、因子、模型…" aria-label="搜索" />
          <button className="sp-close" onClick={onClose} aria-label="关闭">×</button>
        </div>
        <div className="sp-tabs" role="tablist">
          <button role="tab" aria-selected={!cat} onClick={() => setCat('')}>全部</button>
          {CATS.map((c) => <button key={c.key} role="tab" aria-selected={cat === c.key} onClick={() => setCat(c.key)}>{c.label}</button>)}
        </div>
        <div className="sp-list" ref={listRef}>
          {hits === null ? <div className="sp-empty">加载中…</div> : <>
            {results.length > 0 && <div className="sp-group">{q.trim() ? '结果' : cat ? CAT_OF[cat].label : '最近更新'}</div>}
            {results.map((h, i) => (
              <button key={`${h.cat}-${h.id}`} className="sp-item" data-sel={sel === i} onMouseMove={() => setSel(i)} onClick={() => choose(i)}>
                <Icon name={CAT_OF[h.cat].icon} size={15} />
                <span className="sp-title">{h.title}</span>
                <span className="sp-sub">{!cat && `${CAT_OF[h.cat].label} · `}{h.sub}</span>
                <span className="sp-at">{ago(h.at)}</span>
              </button>
            ))}
            {!results.length && <div className="sp-empty">{q.trim() ? `没有找到「${q.trim()}」` : cat ? `还没有${CAT_OF[cat].label}` : '还没有 AI 研究产出，从下面开始一个'}</div>}
            {actions.length > 0 && <div className="sp-group">操作</div>}
            {actions.map((a, j) => (
              <button key={a.go} className="sp-item" data-sel={sel === results.length + j} onMouseMove={() => setSel(results.length + j)} onClick={() => choose(results.length + j)}>
                <span className="sp-plus" aria-hidden="true">+</span><span className="sp-title">{a.title}</span>
              </button>
            ))}
          </>}
        </div>
        <div className="sp-foot"><span><kbd>↑</kbd><kbd>↓</kbd> 选择</span><span><kbd>Enter</kbd> 打开</span><span><kbd>Tab</kbd> 切换类别</span><span><kbd>Esc</kbd> 关闭</span></div>
      </div>
    </div>
  )
}

/** 侧栏左上角的搜索按钮，同时注册 Ctrl/⌘ + K。 */
export function SearchButton({ onNavigate }: { onNavigate: (k: string) => void }) {
  const [open, setOpen] = useState(false)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setOpen((o) => !o) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  return (
    <>
      <button className="sp-trigger" onClick={() => setOpen(true)} aria-label="搜索（Ctrl+K）" title="搜索（Ctrl+K）">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5" /><path d="m10.4 10.4 3.1 3.1" /></svg>
      </button>
      {open && createPortal(<SearchPalette onNavigate={onNavigate} onClose={() => setOpen(false)} />, document.body)}
    </>
  )
}
