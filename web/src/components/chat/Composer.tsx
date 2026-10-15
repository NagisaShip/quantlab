/**
 * Claude 式对话输入框：输入区 + 底栏（＋ 菜单、模型与思考努力程度、上下文用量、发送 / 停止）。
 * 策略室、页面内「问 AI」、指挥台共用这一个组件。
 */
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import type { ContextView, Effort, EffortOption, ModelOption } from '../../apiLab'

const DEFAULT_EFFORTS: EffortOption[] = [
  { id: 'low', label: '低', hint: '快速回答，少想' },
  { id: 'medium', label: '中', hint: '大多数任务' },
  { id: 'high', label: '高', hint: '复杂策略与排错' },
  { id: 'max', label: '极高', hint: '最深入，最慢' },
]

export interface SlashCommand { cmd: string; help: string }

/** 点外面或按 Esc 关闭的弹出层。 */
export function usePopover() {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey) }
  }, [open])
  return { open, setOpen, ref }
}

/** 模型显示名：anthropic/claude-opus-5-5 → Claude Opus 5.5；其他模型保留原名。 */
export function modelLabel(target: string | undefined): string {
  if (!target) return '默认模型'
  const m = target.includes('/') ? target.slice(target.indexOf('/') + 1) : target
  const c = /^claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d))?(?:-\d{8})?$/.exec(m)
  if (c) return `Claude ${c[1][0].toUpperCase()}${c[1].slice(1)} ${c[2]}${c[3] ? '.' + c[3] : ''}`
  if (/^gpt-/i.test(m)) return m.replace(/^gpt/i, 'GPT')
  return m
}

const fmtTokens = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n))

/* ------------------------------------------------------------------ */
/* 模型 + 思考努力程度                                                  */
/* ------------------------------------------------------------------ */
export function ModelMenu({ models, value, defaultModel, effort, efforts, onModel, onEffort, disabled }: {
  models: ModelOption[]; value: string; defaultModel?: string; effort: Effort; efforts?: EffortOption[]
  onModel: (target: string) => void; onEffort: (e: Effort) => void; disabled?: boolean
}) {
  const pop = usePopover()
  const effective = value || defaultModel || ''
  const current = models.find((m) => m.target === effective)
  const effortSupported = !current || (current.effort ?? 'native') !== 'none'
  const list = efforts?.length ? efforts : DEFAULT_EFFORTS
  const effortLabel = list.find((e) => e.id === effort)?.label ?? '中'
  const groups = models.reduce<Record<string, ModelOption[]>>((acc, m) => { (acc[m.provider] ||= []).push(m); return acc }, {})

  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button type="button" className="cmp-model" disabled={disabled} onClick={() => pop.setOpen(!pop.open)}
        aria-haspopup="menu" aria-expanded={pop.open} title="选择模型与思考努力程度">
        <span>{current?.label || modelLabel(effective)}</span>
        {effortSupported && <span className="cmp-effort-tag">{effortLabel}</span>}
        <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden><path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
      </button>
      {pop.open && (
        <div className="cmp-menu cmp-menu-model" role="menu">
          <div className="cmp-menu-section">
            <div className="cmp-menu-label">思考努力程度</div>
            <div className="cmp-seg" role="radiogroup" aria-label="思考努力程度">
              {list.map((e) => (
                <button key={e.id} type="button" role="radio" aria-checked={effort === e.id} disabled={!effortSupported}
                  className={effort === e.id ? 'on' : ''} title={e.hint} onClick={() => onEffort(e.id)}>{e.label}</button>
              ))}
            </div>
            <div className="cmp-menu-hint">
              {effortSupported ? list.find((e) => e.id === effort)?.hint : '这个模型不支持调节思考程度'}
            </div>
          </div>
          <div className="cmp-menu-sep" />
          <button type="button" role="menuitemradio" aria-checked={!value} className={`cmp-menu-item${!value ? ' on' : ''}`}
            onClick={() => { onModel(''); pop.setOpen(false) }}>
            <span className="cmp-menu-main">跟随全局路由</span>
            <span className="cmp-menu-sub">{models.find((m) => m.target === defaultModel)?.label || modelLabel(defaultModel)}</span>
          </button>
          {Object.entries(groups).map(([provider, items]) => (
            <div key={provider}>
              <div className="cmp-menu-label">{provider}</div>
              {items.map((m) => {
                const ready = m.ready !== false
                return (
                  <button key={m.target} type="button" role="menuitemradio" aria-checked={value === m.target}
                    className={`cmp-menu-item${value === m.target ? ' on' : ''}${ready ? '' : ' off'}`}
                    onClick={() => {
                      if (!ready) { window.location.hash = '#/models'; return }
                      onModel(m.target)
                      if (m.default_effort) onEffort(m.default_effort)
                      pop.setOpen(false)
                    }}
                    title={ready ? (m.request_model && m.request_model !== m.model ? `${m.target} → ${m.request_model}` : m.target) : '还没配置密钥，点击去AI 模型'}>
                    <span className="cmp-menu-main">{m.label || modelLabel(m.target)}</span>
                    <span className="cmp-menu-sub">
                      {ready ? (m.context_window ? `${fmtTokens(m.context_window)} 上下文` : '') : '未配置'}
                    </span>
                  </button>
                )
              })}
            </div>
          ))}
          <div className="cmp-menu-sep" />
          <a className="cmp-menu-item" href="#/models"><span className="cmp-menu-main">AI 模型…</span></a>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 上下文用量                                                          */
/* ------------------------------------------------------------------ */
export function ContextRing({ context, onCompact, onManage, compacting }: {
  context: ContextView | null; onCompact?: () => void; onManage?: () => void; compacting?: boolean
}) {
  const pop = usePopover()
  if (!context) return null
  const ratio = Math.min(1, context.total / Math.max(1, context.budget))
  const r = 7
  const c = 2 * Math.PI * r
  const tone = ratio > 0.85 ? 'hot' : ratio > 0.6 ? 'warm' : ''
  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button type="button" className={`cmp-ring ${tone}`} onClick={() => pop.setOpen(!pop.open)}
        title={`上下文 ${context.total.toLocaleString()} / ${context.budget.toLocaleString()} tokens`} aria-label="上下文用量">
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden>
          <circle cx="9" cy="9" r={r} className="track" />
          <circle cx="9" cy="9" r={r} className="fill" strokeDasharray={`${c * Math.max(ratio, 0.02)} ${c}`} transform="rotate(-90 9 9)" />
        </svg>
      </button>
      {pop.open && (
        <div className="cmp-menu cmp-menu-ctx" role="dialog" aria-label="上下文">
          <div className="cmp-ctx-head">
            <b>上下文</b>
            <span>{fmtTokens(context.total)} / {fmtTokens(context.budget)} tokens · {Math.round(ratio * 100)}%</span>
          </div>
          <div className="cmp-ctx-bar"><i style={{ width: `${Math.max(1, ratio * 100)}%` }} /></div>
          <ul className="cmp-ctx-list">
            {context.items.map((it) => (
              <li key={it.key}><span>{it.title}</span><span>{fmtTokens(it.tokens)}</span></li>
            ))}
          </ul>
          {context.history_budget !== undefined && (
            <div className="cmp-menu-hint">对话原文最多保留约 {fmtTokens(context.history_budget)} tokens，更早的部分自动压缩成摘要。</div>
          )}
          <div className="cmp-ctx-actions">
            {onManage && <button type="button" className="button small ghost" onClick={() => { onManage(); pop.setOpen(false) }}>管理材料</button>}
            {onCompact && (
              <button type="button" className="button small" disabled={compacting} onClick={() => { onCompact(); pop.setOpen(false) }}>
                {compacting ? '正在压缩…' : '压缩历史'}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* ＋ 菜单                                                             */
/* ------------------------------------------------------------------ */
export interface PlusItem { label: string; hint?: string; onClick: () => void; disabled?: boolean; checked?: boolean; section?: string; keepOpen?: boolean }

export function PlusMenu({ items }: { items: PlusItem[] }) {
  const pop = usePopover()
  if (!items.length) return null
  return (
    <div className="cmp-pop-anchor" ref={pop.ref}>
      <button type="button" className="cmp-icon" aria-label="更多操作" aria-expanded={pop.open} onClick={() => pop.setOpen(!pop.open)}>
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden><path d="M8 3v10M3 8h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
      </button>
      {pop.open && (
        <div className="cmp-menu cmp-menu-plus" role="menu">
          {items.map((it, i) => (
            <Fragment key={it.label}>
              {it.section && it.section !== items[i - 1]?.section && (
                <>{i > 0 && <div className="cmp-menu-sep" />}<div className="cmp-menu-label">{it.section}</div></>
              )}
              <button type="button" role={it.checked === undefined ? 'menuitem' : 'menuitemcheckbox'}
                aria-checked={it.checked} className={`cmp-menu-item cmp-menu-stack${it.checked !== undefined ? " has-check" : ""}`} disabled={it.disabled}
                onClick={() => { it.onClick(); if (!it.keepOpen) pop.setOpen(false) }}>
                <span className="cmp-menu-main">{it.checked !== undefined && <span className="cmp-check">{it.checked ? '✓' : ''}</span>}{it.label}</span>
                {it.hint && <span className="cmp-menu-sub">{it.hint}</span>}
              </button>
            </Fragment>
          ))}
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/* 输入框                                                              */
/* ------------------------------------------------------------------ */
export function Composer({
  value, onChange, onSend, onStop, running, disabled, placeholder, commands = [], above, left, right, autoFocus, sendLabel = '发送',
}: {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onStop?: () => void
  running?: boolean
  disabled?: boolean
  placeholder?: string
  commands?: SlashCommand[]
  above?: ReactNode     // 输入框上方：附带内容、目标标签
  left?: ReactNode      // 底栏左侧：＋ 菜单、模式开关
  right?: ReactNode     // 底栏右侧：模型、上下文
  autoFocus?: boolean
  sendLabel?: string
}) {
  const ta = useRef<HTMLTextAreaElement>(null)
  const [active, setActive] = useState(0)
  const slash = value.startsWith('/') && !/\s/.test(value) ? commands.filter((c) => c.cmd.startsWith(value)) : []

  useLayoutEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`
  }, [value])
  useEffect(() => { setActive(0) }, [value])

  const canSend = !running && !disabled && value.trim().length > 0
  const pick = (c: SlashCommand) => { onChange(c.cmd + ' '); ta.current?.focus() }

  return (
    <div className="cmp">
      {slash.length > 0 && (
        <div className="cmp-slash" role="listbox">
          {slash.map((c, i) => (
            <button key={c.cmd} type="button" role="option" aria-selected={i === active} className={i === active ? 'on' : ''}
              onMouseEnter={() => setActive(i)} onClick={() => pick(c)}>
              <b>{c.cmd}</b><span>{c.help}</span>
            </button>
          ))}
        </div>
      )}
      <div className={`cmp-box${disabled ? ' disabled' : ''}`} onClick={() => ta.current?.focus()}>
        {above && <div className="cmp-above">{above}</div>}
        <textarea ref={ta} rows={1} value={value} autoFocus={autoFocus} disabled={disabled}
          placeholder={placeholder} onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (slash.length) {
              if (e.key === 'ArrowDown') { e.preventDefault(); setActive((active + 1) % slash.length); return }
              if (e.key === 'ArrowUp') { e.preventDefault(); setActive((active - 1 + slash.length) % slash.length); return }
              if ((e.key === 'Enter' || e.key === 'Tab') && slash[active] && value !== slash[active].cmd) { e.preventDefault(); pick(slash[active]); return }
            }
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              if (canSend) onSend()
            }
          }} />
        <div className="cmp-bar" onClick={(e) => e.stopPropagation()}>
          <div className="cmp-left">{left}</div>
          <div className="cmp-right">
            {right}
            {running && onStop ? (
              <button type="button" className="cmp-send stop" onClick={onStop} aria-label="停止" title="停止">
                <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><rect x="2" y="2" width="8" height="8" rx="1.5" fill="currentColor" /></svg>
              </button>
            ) : (
              <button type="button" className="cmp-send" disabled={!canSend} onClick={onSend} aria-label={sendLabel} title={`${sendLabel}（Enter）`}>
                <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><path d="M7 12V2M2.5 6.5 7 2l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
