/**
 * Claude 式对话输入框：输入区 + 底栏（＋ 菜单、模型与思考努力程度、上下文用量、发送 / 停止）。
 * 策略室、页面内「问 AI」、指挥台共用这一个组件。
 */
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import type { ContextView, Effort, EffortOption, ModelOption } from '../../apiLab'
import { StepSlider } from '../StepSlider'
import { ContextStats } from './LabControls'
import '../../stylesBlocks.css'
import { v3, type ModelUsage } from '../../api/research'
import { v4, type UsageItem } from '../../api/platform'
import { LimitBars, fmtCost, fmtTokens as fmtTok } from '../modelUsage'

const DEFAULT_EFFORTS: EffortOption[] = [
  { id: 'low', label: '低', hint: '快速回答，少想' },
  { id: 'medium', label: '中', hint: '大多数任务' },
  { id: 'high', label: '高', hint: '复杂策略与排错' },
  { id: 'max', label: '极高', hint: '最深入，最慢' },
]

/**
 * 斜杠命令。group 用于在弹出列表里分类；arg 是参数提示；
 * run 存在时由前端直接执行（发送时拦截），否则把整句发给服务端。
 */
export interface SlashCommand { cmd: string; help: string; group?: string; arg?: string; run?: (arg: string) => void; disabled?: boolean }

/** 可以 @ 的对象（团队角色等）。 */
export interface MentionOption { id: string; name: string; hint?: string; avatar?: ReactNode }

/** 输入末尾正在输入的 @词（光标在末尾时）：返回 [起点, 关键字]。 */
export function mentionQuery(value: string): [number, string] | null {
  const m = /(^|\s)@([^\s@]{0,20})$/.exec(value)
  return m ? [value.length - m[2].length - 1, m[2]] : null
}

/** 从输入里解析出被 @ 的对象（按名字最长匹配）。 */
export function findMention<T extends { name: string }>(text: string, options: T[]): T | undefined {
  return [...options].sort((a, b) => b.name.length - a.name.length).find((o) => text.includes(`@${o.name}`))
}

/** @ 弹出列表：输入 @ 后按名字过滤，↑↓ 选择，Enter / Tab / 点击确认。 */
export function MentionList({ options, query, active, onHover, onPick }: {
  options: MentionOption[]; query: string; active: number; onHover: (i: number) => void; onPick: (o: MentionOption) => void
}) {
  const list = filterMentions(options, query)
  if (!list.length) return null
  return (
    <div className="cmp-slash cmp-mention anim-pop" role="listbox" aria-label="选择要 @ 的角色">
      <div className="cmp-slash-group">点名</div>
      {list.map((o, i) => (
        <button key={o.id} type="button" role="option" aria-selected={i === active} className={i === active ? 'on' : ''}
          onMouseEnter={() => onHover(i)} onMouseDown={(e) => { e.preventDefault(); onPick(o) }}>
          <b className="cmp-mention-name">{o.avatar}@{o.name}</b>{o.hint && <span>{o.hint}</span>}
        </button>
      ))}
    </div>
  )
}

export function filterMentions(options: MentionOption[], query: string) {
  const q = query.toLowerCase()
  return options.filter((o) => !q || o.name.toLowerCase().includes(q) || o.id.toLowerCase().includes(q) || (o.hint ?? '').toLowerCase().includes(q)).slice(0, 8)
}

/** 点外面或按 Esc 关闭的弹出层；菜单优先向上展开，空间不足时自动翻到下方并限制高度。 */
export function usePopover() {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [layout, setLayout] = useState<{ placement: 'top' | 'bottom'; maxHeight: number }>({
    placement: 'top', maxHeight: 420,
  })
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('mousedown', onDown); document.removeEventListener('keydown', onKey) }
  }, [open])
  useLayoutEffect(() => {
    if (!open) {
      setLayout((prev) => (prev.placement === 'top' && prev.maxHeight === 420 ? prev : { placement: 'top', maxHeight: 420 }))
      return
    }
    let frame = 0
    const update = () => {
      const anchor = ref.current?.getBoundingClientRect()
      const menu = menuRef.current
      if (!anchor || !menu) return
      const gap = 6
      const margin = 12
      const viewportHeight = window.visualViewport?.height ?? window.innerHeight
      const above = Math.max(0, anchor.top - margin - gap)
      const below = Math.max(0, viewportHeight - anchor.bottom - margin - gap)
      const wanted = Math.min(420, menu.scrollHeight)
      // 保留原来的向上习惯；上方放不下时，优先使用空间更大的那一侧，并让菜单在视口内滚动。
      const placement = wanted <= above || (wanted > below && above >= below) ? 'top' : 'bottom'
      const maxHeight = Math.max(0, Math.min(420, placement === 'top' ? above : below))
      setLayout((prev) => (prev.placement === placement && Math.abs(prev.maxHeight - maxHeight) < 1
        ? prev : { placement, maxHeight }))
    }
    const schedule = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(update)
    }
    update()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    if (menuRef.current) observer?.observe(menuRef.current)
    window.addEventListener('resize', schedule)
    document.addEventListener('scroll', schedule, true)
    return () => {
      cancelAnimationFrame(frame)
      observer?.disconnect()
      window.removeEventListener('resize', schedule)
      document.removeEventListener('scroll', schedule, true)
    }
  }, [open])
  return { open, setOpen, ref, menuRef, placement: layout.placement, menuStyle: { maxHeight: layout.maxHeight } }
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
        <div ref={pop.menuRef} className="cmp-menu cmp-menu-model" data-placement={pop.placement} style={pop.menuStyle} role="menu">
          <div className="cmp-menu-section">
            <div className="cmp-menu-label">思考努力程度</div>
            <StepSlider options={list} value={effort} onChange={onEffort} disabled={!effortSupported} label="思考努力程度" compact />
            <div className="cmp-menu-hint">
              {!effortSupported ? '这个模型不支持调节思考程度'
                : current?.effort === 'auto' ? `${list.find((e) => e.id === effort)?.hint ?? ''}（会按这个程度请求；服务商不支持时自动忽略）`
                  : list.find((e) => e.id === effort)?.hint}
            </div>
          </div>
          <div className="cmp-menu-sep" />
          <button type="button" role="menuitemradio" aria-checked={!value} className={`cmp-menu-item${!value ? ' on' : ''}`}
            onClick={() => { onModel(''); pop.setOpen(false) }} title="默认使用模型清单里的第一个模型；可在设置 → 模型 调整顺序">
            <span className="cmp-menu-main">默认</span>
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
export function ContextRing({ context, onCompact, onManage, compacting, updatedAt }: {
  context: ContextView | null; onCompact?: () => void; onManage?: () => void; compacting?: boolean; updatedAt?: number
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
        title={`上下文 ${context.total.toLocaleString()} / ${context.budget.toLocaleString()} tokens`} aria-label="上下文、用量与额度">
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden>
          <circle cx="9" cy="9" r={r} className="track" />
          <circle cx="9" cy="9" r={r} className="fill" strokeDasharray={`${c * Math.max(ratio, 0.02)} ${c}`} transform="rotate(-90 9 9)" />
        </svg>
      </button>
      {pop.open && (
        <div className="cmp-menu cmp-menu-ctx" role="dialog" aria-label="上下文">
          <div className="ctx-title">上下文</div>
          <ContextStats context={context} updatedAt={updatedAt} />
          <UsageQuota />
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

 /** 圆圈弹层里的「用量与额度」：我今天 / 近 7 天的模型用量、我的限额进度、工作区当日配额。打开时才请求。 */
function UsageQuota() {
  const [mine, setMine] = useState<ModelUsage | null>(null)
  const [week, setWeek] = useState<ModelUsage | null>(null)
  const [ws, setWs] = useState<UsageItem[] | null>(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    let alive = true
    Promise.allSettled([v3.modelUsage(1, 'me'), v3.modelUsage(7, 'me'), v4.usage(1)]).then(([a, b, c]) => {
      if (!alive) return
      if (a.status === 'fulfilled') setMine(a.value)
      if (b.status === 'fulfilled') setWeek(b.value)
      if (c.status === 'fulfilled') setWs(c.value.items.filter((it) => it.metric.startsWith('llm_') || it.metric === 'backtest_runs'))
      if (a.status === 'rejected' && c.status === 'rejected') setErr('用量暂时读不到')
    })
    return () => { alive = false }
  }, [])
  return (
    <div className="cmp-usage anim-fade">
      <div className="ctx-title">用量与额度</div>
      {err && <div className="cmp-usage-row muted">{err}</div>}
      {!mine && !err && <div className="shimmer-line" aria-hidden />}
      {mine && (
        <div className="cmp-usage-grid">
          <div><small>今天</small><b>{fmtTok(mine.total.tokens)}</b><span>{mine.total.calls} 次 · {fmtCost(mine.total.cost)}</span></div>
          {week && <div><small>近 7 天</small><b>{fmtTok(week.total.tokens)}</b><span>{week.total.calls} 次 · {fmtCost(week.total.cost)}</span></div>}
          {typeof mine.total.cache_hit_rate === 'number' && <div><small>缓存命中</small><b>{Math.round(mine.total.cache_hit_rate * 100)}%</b><span>输入 token</span></div>}
        </div>
      )}
      {mine && mine.my_limits.length > 0 && <LimitBars items={mine.my_limits} compact />}
      {ws && ws.length > 0 && (
        <ul className="cmp-usage-ws">
          {ws.map((it) => {
            const pct = it.used_percent === null ? null : Math.min(100, Number(it.used_percent))
            return (
              <li key={it.metric} className={it.exhausted ? 'hot' : pct !== null && pct > 80 ? 'warm' : ''}>
                <span>工作区 · {it.label}</span>
                <span className="mono">{it.used}{it.limit === null ? '' : ` / ${it.limit}`}</span>
                {pct !== null && <i style={{ width: `${pct}%` }} />}
              </li>
            )
          })}
        </ul>
      )}
      <a className="cmp-usage-link" href="#/settings/quota">查看完整用量与申请额度 →</a>
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
/** 单条消息上限 2 万字；超过时整段内容自动转成 txt 文件附带（研究室里 Agent 可分段读取全文） */
export const MESSAGE_LIMIT = 20_000

function textFile(text: string): File {
  const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)
  return new File([text], `长文本-${stamp}.txt`, { type: 'text/plain' })
}

export function Composer({
  value, onChange, onSend, onStop, running, disabled, placeholder, commands = [], above, left, right, autoFocus, sendLabel = '发送',
  hasAttachments, onPasteFiles, mentions, hideDisclaimer, onLongText, queueCount = 0,
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
  hasAttachments?: boolean                 // 带了附件时，没有文字也能发送
  onPasteFiles?: (files: File[]) => void   // 粘贴图片等文件
  mentions?: MentionOption[]               // 输入 @ 时可以点名的对象
  hideDisclaimer?: boolean                 // 输入框下方的「可能会出错」提醒（默认显示）
  onLongText?: (file: File) => void        // 超过 2 万字的内容转成 txt 文件交给它；不提供时截断到上限
  queueCount?: number                      // 运行中已排队的消息数
}) {
  const [overflow, setOverflow] = useState('')
  /** 粘贴 / 输入后超过上限：整段转成 txt 附件，输入框里只留一句说明 */
  const accept = (next: string) => {
    if (next.length <= MESSAGE_LIMIT) { onChange(next); return }
    if (onLongText) {
      onLongText(textFile(next))
      onChange('')
      setOverflow(`内容超过 ${MESSAGE_LIMIT.toLocaleString()} 字，已自动转成 txt 文件附带（共 ${next.length.toLocaleString()} 字）`)
    } else {
      onChange(next.slice(0, MESSAGE_LIMIT))
      setOverflow(`单条消息最多 ${MESSAGE_LIMIT.toLocaleString()} 字，超出部分已截掉`)
    }
    window.setTimeout(() => setOverflow(''), 6000)
  }
  const ta = useRef<HTMLTextAreaElement>(null)
  const [active, setActive] = useState(0)
  const slash = value.startsWith('/') && !/\s/.test(value) ? commands.filter((c) => c.cmd.startsWith(value)) : []
  const mq = mentions?.length ? mentionQuery(value) : null
  const mlist = mq ? filterMentions(mentions!, mq[1]) : []
  const pickMention = (o: MentionOption) => { if (mq) onChange(`${value.slice(0, mq[0])}@${o.name} `); ta.current?.focus() }

  useLayoutEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 260)}px`
  }, [value])
  useEffect(() => { setActive(0) }, [value])

  const canSend = !disabled && (value.trim().length > 0 || !!hasAttachments)
  const pick = (c: SlashCommand) => { onChange(c.cmd + ' '); ta.current?.focus() }

  return (
    <div className="cmp">
      {slash.length > 0 && (
        <div className="cmp-slash" role="listbox">
          {slash.map((c, i) => (
            <Fragment key={c.cmd}>
              {c.group && c.group !== slash[i - 1]?.group && <div className="cmp-slash-group">{c.group}</div>}
              <button type="button" role="option" aria-selected={i === active} aria-disabled={c.disabled || undefined}
                className={`${i === active ? 'on' : ''}${c.disabled ? ' off' : ''}`}
                onMouseEnter={() => setActive(i)} onClick={() => pick(c)}>
                <b>{c.cmd}{c.arg && <i> {c.arg}</i>}</b><span>{c.help}</span>
              </button>
            </Fragment>
          ))}
        </div>
      )}
      {!slash.length && mq && mlist.length > 0 && (
        <MentionList options={mentions!} query={mq[1]} active={Math.min(active, mlist.length - 1)} onHover={setActive} onPick={pickMention} />
      )}
      <div className={`cmp-box${disabled ? ' disabled' : ''}`} onClick={() => ta.current?.focus()}>
        {above && <div className="cmp-above">{above}</div>}
        <textarea ref={ta} rows={1} value={value} autoFocus={autoFocus} disabled={disabled}
          placeholder={placeholder} onChange={(e) => accept(e.target.value)}
          onPaste={(e) => {
            const files = [...e.clipboardData.files]
            if (files.length && onPasteFiles) { e.preventDefault(); onPasteFiles(files); return }
            const pasted = e.clipboardData.getData('text')
            const el = e.currentTarget
            const next = value.slice(0, el.selectionStart) + pasted + value.slice(el.selectionEnd)
            if (next.length > MESSAGE_LIMIT) { e.preventDefault(); accept(next) }
          }}
          onKeyDown={(e) => {
            if (slash.length) {
              if (e.key === 'ArrowDown') { e.preventDefault(); setActive((active + 1) % slash.length); return }
              if (e.key === 'ArrowUp') { e.preventDefault(); setActive((active - 1 + slash.length) % slash.length); return }
              if ((e.key === 'Enter' || e.key === 'Tab') && slash[active] && value !== slash[active].cmd) { e.preventDefault(); pick(slash[active]); return }
            } else if (mlist.length) {
              if (e.key === 'ArrowDown') { e.preventDefault(); setActive((active + 1) % mlist.length); return }
              if (e.key === 'ArrowUp') { e.preventDefault(); setActive((active - 1 + mlist.length) % mlist.length); return }
              if ((e.key === 'Enter' || e.key === 'Tab') && !e.nativeEvent.isComposing) { e.preventDefault(); pickMention(mlist[Math.min(active, mlist.length - 1)]); return }
              if (e.key === 'Escape') { onChange(value + ' '); return }
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
              <>
                {canSend && (
                  <button type="button" className="cmp-send queue" onClick={onSend} aria-label="加入发送队列" title={`加入发送队列${queueCount ? `（已有 ${queueCount} 条）` : ''}`}>
                    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><path d="M7 12V2M2.5 6.5 7 2l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>
                  </button>
                )}
                <button type="button" className="cmp-send stop" onClick={onStop} aria-label="停止" title="停止">
                  <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><rect x="2" y="2" width="8" height="8" rx="1.5" fill="currentColor" /></svg>
                </button>
              </>
            ) : (
              <button type="button" className="cmp-send" disabled={!canSend} onClick={onSend} aria-label={sendLabel} title={`${sendLabel}（Enter）`}>
                <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden><path d="M7 12V2M2.5 6.5 7 2l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></svg>
              </button>
            )}
          </div>
        </div>
      </div>
      {overflow && <div className="cmp-overflow anim-fade" role="status">{overflow}</div>}
      {value.length > MESSAGE_LIMIT * 0.75 && (
        <div className={`cmp-count${value.length >= MESSAGE_LIMIT ? ' full' : ''}`}>{value.length.toLocaleString()} / {MESSAGE_LIMIT.toLocaleString()}</div>
      )}
      {!hideDisclaimer && <div className="cmp-disclaimer">QuantLab Bot 可能会出错，请检查重要信息。</div>}
    </div>
  )
}
