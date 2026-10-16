import { APPROVAL_CATEGORIES, type ApprovalCategory } from '../../apiLab'
/**
 * 对话底栏与侧栏的控件：上下文用量统计（模板化：分段条 + 分类明细 + 剩余空间）、
 * ＋ 菜单（附件、斜杠命令、插件子菜单、自动迭代轮数滑块）、权限模式菜单。
 * 策略室与各研究室共用。
 */
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { ContextView, Permission } from '../../apiLab'
import { StepSlider } from '../StepSlider'
import { QuantEyes, useBotFlash } from '../QuantBot'
import { usePopover } from './Composer'

const fmtK = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const pctOf = (n: number, d: number) => `${((n / Math.max(1, d)) * 100).toFixed(1)}%`

/* ------------------------------------------------------------------ */
/* 上下文用量                                                          */
/* ------------------------------------------------------------------ */
const CATS: { id: string; label: string; color: string; match: (key: string) => boolean }[] = [
  { id: 'system', label: '系统提示', color: '#2f6fdf', match: (k) => k === 'system' },
  { id: 'brief', label: '项目与目标', color: '#18a77a', match: (k) => k === 'brief' },
  { id: 'code', label: '代码 / 规格', color: '#e8833a', match: (k) => k === 'code' },
  { id: 'metrics', label: '回测指标', color: '#8c6be0', match: (k) => k === 'metrics' },
  { id: 'pins', label: '钉住的材料', color: '#d9a21b', match: (k) => k.startsWith('pin:') },
  { id: 'history', label: '对话', color: '#e05a3a', match: (k) => k === 'history' },
  { id: 'summary', label: '历史摘要', color: '#8a8a85', match: (k) => k === 'summary' },
]

export function contextCats(context: ContextView) {
  const rows = CATS.map((c) => ({ ...c, tokens: 0, count: 0 }))
  const other = { id: 'other', label: '其他', color: '#b8b6ae', match: () => true, tokens: 0, count: 0 }
  for (const it of context.items) {
    const row = rows.find((r) => r.match(it.key)) ?? other
    row.tokens += it.tokens
    row.count += 1
  }
  return [...rows, other].filter((r) => r.tokens > 0)
}

/** 上下文统计：「已使用 22%  220.8k / 1M」+ 分段条 + 分类明细 + 剩余空间。 */
export function ContextStats({ context, updatedAt, children }: { context: ContextView; updatedAt?: number; children?: ReactNode }) {
  const cats = contextCats(context)
  const free = Math.max(0, context.budget - context.total)
  const used = Math.min(100, (context.total / Math.max(1, context.budget)) * 100)
  const [, tick] = useState(0)
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 30_000); return () => clearInterval(t) }, [])
  const ago = updatedAt ? Math.max(0, Math.round((Date.now() - updatedAt) / 60_000)) : null
  return (
    <div className="ctx-stats">
      <div className="ctx-stats-head">
        <b>已使用 {Math.round(used)}%</b>
        <span>{fmtK(context.total)} / {fmtK(context.budget)}</span>
      </div>
      <div className="ctx-seg" role="img" aria-label={`上下文已使用 ${Math.round(used)}%`}>
        {cats.map((c) => (
          <i key={c.id} style={{ width: `${(c.tokens / Math.max(1, context.budget)) * 100}%`, background: c.color }} title={`${c.label} ${fmtK(c.tokens)}`} />
        ))}
      </div>
      <ul className="ctx-rows">
        {cats.map((c) => (
          <li key={c.id}>
            <span className="ctx-sq" style={{ background: c.color }} />
            <span className="ctx-name">{c.label}{c.count > 1 ? <em> ×{c.count}</em> : null}</span>
            <span className="ctx-num">{fmtK(c.tokens)}</span>
            <span className="ctx-pct">{pctOf(c.tokens, context.budget)}</span>
          </li>
        ))}
        {context.history_budget !== undefined && (
          <li className="dim">
            <span className="ctx-sq" style={{ background: 'var(--line)' }} />
            <span className="ctx-name">对话原文保留额度</span>
            <span className="ctx-num">{fmtK(context.history_budget)}</span>
            <span className="ctx-pct">{pctOf(context.history_budget, context.budget)}</span>
          </li>
        )}
        <li className="free">
          <span className="ctx-sq" style={{ background: 'var(--line-2)' }} />
          <span className="ctx-name">剩余空间</span>
          <span className="ctx-num">{fmtK(free)}</span>
          <span className="ctx-pct">{pctOf(free, context.budget)}</span>
        </li>
      </ul>
      {children}
      <div className="ctx-foot">
        {context.model ? <>按 {context.model} 的窗口计算。</> : null}
        {ago !== null && (ago < 1 ? '刚刚更新。' : `${ago} 分钟前更新。`)}本轮回复结束后刷新；更早的对话会自动压缩成摘要。
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* ＋ 菜单                                                             */
/* ------------------------------------------------------------------ */
export interface ActionItem {
  label: string
  icon?: ReactNode
  hint?: string
  onClick?: () => void
  disabled?: boolean
  checked?: boolean
  submenu?: ActionItem[]
  /** 自定义整行（例如滑块），不作为按钮渲染 */
  render?: (close: () => void) => ReactNode
  divider?: boolean
}

export const Icons = {
  attach: <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M10.5 4.5 5.4 9.6a1.6 1.6 0 0 0 2.3 2.3l5.4-5.4a3 3 0 0 0-4.3-4.3L3.4 7.6a4.3 4.3 0 0 0 6.1 6.1L13 10.2" /></svg>,
  slash: <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><rect x="2.5" y="2.5" width="11" height="11" rx="2.5" /><path d="m9.5 5.5-3 5" /></svg>,
  plugin: <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M6 2.5v2.5M10 2.5v2.5M4.5 5h7v2.5a3.5 3.5 0 0 1-7 0V5ZM8 11v2.5" /></svg>,
  loop: <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M12.5 6.5A4.8 4.8 0 0 0 3.6 5M3.5 9.5a4.8 4.8 0 0 0 8.9 1.5" /><path d="M3.3 2.6v2.6h2.6M12.7 13.4v-2.6h-2.6" /></svg>,
  pin: <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M9.8 2.5 13.5 6.2 11 7.4 8.6 9.8 8.2 12.4 3.6 7.8 6.2 7.4 8.6 5Z" /><path d="m5.8 10.2-3.3 3.3" /></svg>,
  team: <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><circle cx="6" cy="6" r="2.2" /><circle cx="11.3" cy="6.8" r="1.7" /><path d="M2.5 13c.4-2.2 1.8-3.4 3.5-3.4s3.1 1.2 3.5 3.4M10 10c1.8-.3 3.1.7 3.5 2.6" /></svg>,
  search: <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"><circle cx="7" cy="7" r="4.5" /><path d="m10.4 10.4 3.1 3.1" /></svg>,
  save: <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2.5v7M5 6.8 8 9.8l3-3M3 11.5v1.5h10v-1.5" /></svg>,
  chevron: <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="m4.5 2.5 3.5 3.5-3.5 3.5" /></svg>,
}

export function ActionMenu({ items, label = '添加与操作' }: { items: ActionItem[]; label?: string }) {
  const pop = usePopover()
  const [openSub, setOpenSub] = useState<string | null>(null)
  useEffect(() => { if (!pop.open) setOpenSub(null) }, [pop.open])
  const close = () => pop.setOpen(false)
  const row = (it: ActionItem, nested = false) => {
    if (it.render) return <div key={it.label} className="act-custom">{it.render(close)}</div>
    const open = openSub === it.label
    return (
      <div key={it.label} className={nested ? 'act-nested' : undefined}>
        {it.divider && <div className="cmp-menu-sep" />}
        <button type="button" role={it.checked === undefined ? 'menuitem' : 'menuitemcheckbox'} aria-checked={it.checked}
          aria-expanded={it.submenu ? open : undefined} className={`act-item${open ? ' open' : ''}`} disabled={it.disabled}
          onClick={() => {
            if (it.submenu) { setOpenSub(open ? null : it.label); return }
            it.onClick?.()
            close()
          }}>
          <span className="act-icon">{it.icon}</span>
          <span className="act-text">
            <span className="act-label">{it.label}</span>
            {it.hint && <span className="act-hint">{it.hint}</span>}
          </span>
          {it.checked !== undefined && <span className={`act-check${it.checked ? ' on' : ''}`}>✓</span>}
          {it.submenu && <span className="act-chev">{Icons.chevron}</span>}
        </button>
        {it.submenu && open && <div className="act-sub anim-expand">{it.submenu.map((s) => row(s, true))}</div>}
      </div>
    )
  }
  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button type="button" className={`cmp-icon${pop.open ? ' on' : ''}`} aria-label={label} title={label} aria-expanded={pop.open} onClick={() => pop.setOpen(!pop.open)}>
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden className="plus-rot"><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
      </button>
      {pop.open && <div className="cmp-menu act-menu" role="menu">{items.map((it) => row(it))}</div>}
    </div>
  )
}

/** 菜单里的「自动迭代」：左右滑动设置轮数，点开始。 */
export function AutoIterateRow({ rounds, onRounds, onStart, disabled, needsGoal, onSetGoal, max = 8 }: {
  rounds: number; onRounds: (n: number) => void; onStart: (n: number) => void; disabled?: boolean
  /** 没有目标时自动迭代不可用：显示「先设定目标」 */
  needsGoal?: boolean; onSetGoal?: () => void; max?: number
}) {
  const opts = Array.from({ length: max }, (_, i) => ({ id: String(i + 1), label: String(i + 1) }))
  const fx = useBotFlash()
  return (
    <div className={`auto-row${needsGoal ? ' locked' : ''}`}>
      <div className="auto-row-head">
        <span className="act-icon">{Icons.loop}</span>
        <span className="act-text">
          <span className="act-label">自动迭代</span>
          <span className="act-hint">{needsGoal ? '需要先设定目标；没有目标就直接对话' : `按目标连续改进，最多 ${rounds} 轮`}</span>
        </span>
        {needsGoal
          ? <button type="button" className="button small" onClick={onSetGoal}>设定目标</button>
          : <button type="button" className="button small primary" disabled={disabled} onClick={() => { fx.fire('start'); onStart(rounds) }}><QuantEyes size={14} motion="low" flash={fx.flash} />开始</button>}
      </div>
      {!needsGoal && <StepSlider options={opts} value={String(rounds)} onChange={(v) => onRounds(Number(v))} label="自动迭代轮数" compact />}
    </div>
  )
}

/** 附件：选文件后交给 onFiles（文本类文件读成文字）。 */
export function useFilePicker(onFiles: (files: File[]) => void) {
  const input = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    const el = document.createElement('input')
    el.type = 'file'
    el.multiple = true
    el.accept = '.txt,.md,.csv,.json,.py,.yaml,.yml,.tsv,.log,image/*'
    el.style.display = 'none'
    el.onchange = () => { if (el.files?.length) onFiles([...el.files]); el.value = '' }
    document.body.appendChild(el)
    input.current = el
    return () => { el.remove() }
  }, [onFiles])
  return () => input.current?.click()
}

/* ------------------------------------------------------------------ */
/* 权限模式                                                            */
/* ------------------------------------------------------------------ */
const MODES: { id: Exclude<Permission, 'bypass'>; label: string; hint: string }[] = [
  { id: 'auto', label: '自动', hint: '只有规则要求的操作才审批：删除记录、模拟交易与晋级、改平台配置' },
  { id: 'manual', label: '手动', hint: '改代码、跑回测、保存、删除前始终询问' },
  { id: 'accept_edits', label: '接受编辑', hint: '自动接受代码改动，运行与其余操作前询问' },
  { id: 'plan', label: '计划', hint: '先出计划，确认后再动手' },
]
export const permissionLabel = (p?: Permission) => (p === 'bypass' ? '完全权限' : MODES.find((m) => m.id === (p ?? 'auto'))?.label ?? '自动')
const DEFAULT_BYPASS: ApprovalCategory[] = ['delete']

const ShieldIcon = <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round"><path d="M8 2 3 4v3.6c0 3 2.1 5.4 5 6.4 2.9-1 5-3.4 5-6.4V4Z" /></svg>

export function PermissionMenu({ value, onChange, disabled, approvals, onApprovals }: {
  value?: Permission; onChange: (p: Permission) => void; disabled?: boolean
  /** 完全权限下仍要审批的类别；不传 onApprovals 时不显示选择 */
  approvals?: ApprovalCategory[]; onApprovals?: (a: ApprovalCategory[]) => void
}) {
  const pop = usePopover()
  const cur = value ?? 'auto'
  const chosen = approvals ?? DEFAULT_BYPASS
  const flip = (id: ApprovalCategory) => onApprovals?.(chosen.includes(id) ? chosen.filter((x) => x !== id) : [...chosen, id])
  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button type="button" className={`cmp-perm ${cur}`} disabled={disabled} aria-haspopup="menu" aria-expanded={pop.open}
        title="权限模式：AI 改代码、跑回测前是否要你确认" onClick={() => pop.setOpen(!pop.open)}>
        {ShieldIcon}<span>{permissionLabel(cur)}</span>
      </button>
      {pop.open && (
        <div className="cmp-menu perm-menu" role="menu">
          <div className="cmp-menu-label">模式</div>
          {MODES.map((m) => (
            <button key={m.id} type="button" role="menuitemradio" aria-checked={cur === m.id} className="act-item"
              onClick={() => { onChange(m.id); pop.setOpen(false) }}>
              <span className="act-text"><span className="act-label">{m.label}</span><span className="act-hint">{m.hint}</span></span>
              <span className={`act-check${cur === m.id ? ' on' : ''}`}>✓</span>
            </button>
          ))}
          <div className="cmp-menu-sep" />
          <div className="perm-bypass">
            <span className="act-text"><span className="act-label">完全权限</span><span className="act-hint">除了下面勾选的类别，其余操作 Agent 直接执行</span></span>
            <button type="button" className={`button small${cur === 'bypass' ? ' primary' : ''}`}
              onClick={() => { onChange(cur === 'bypass' ? 'auto' : 'bypass'); if (cur === 'bypass') pop.setOpen(false) }}>{cur === 'bypass' ? '已启用' : '启用'}</button>
          </div>
          {cur === 'bypass' && onApprovals && (
            <div className="perm-cats anim-expand">
              <div className="cmp-menu-label">仍要我审批的操作</div>
              {APPROVAL_CATEGORIES.map((c) => (
                <label key={c.id} className="perm-cat">
                  <input type="checkbox" checked={chosen.includes(c.id)} onChange={() => flip(c.id)} />
                  <span className="act-text"><span className="act-label">{c.label}</span><span className="act-hint">{c.hint}</span></span>
                </label>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
