import { useCallback, useEffect, useMemo, useRef, type KeyboardEvent, type ReactNode } from 'react'

/**
 * 轻量 Python 代码编辑器：透明 textarea 叠在高亮层上。
 * 支持：行号、语法高亮、Tab / Shift+Tab 缩进、回车自动缩进、Ctrl+/ 注释、
 * Ctrl+S 保存、Ctrl+Enter 运行、错误行标记。没有引入任何第三方编辑器依赖。
 */

const KEYWORDS = new Set([
  'False', 'None', 'True', 'and', 'as', 'assert', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else',
  'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or',
  'pass', 'raise', 'return', 'try', 'while', 'with', 'yield',
])
const BUILTINS = new Set([
  'abs', 'all', 'any', 'bool', 'dict', 'enumerate', 'filter', 'float', 'int', 'len', 'list', 'map', 'max', 'min',
  'print', 'range', 'reversed', 'round', 'set', 'sorted', 'str', 'sum', 'tuple', 'zip', 'isinstance', 'hasattr',
])
export const SDK_NAMES = new Set([
  'initialize', 'handle_data', 'before_trading_start', 'after_trading_end', 'on_order_filled', 'history',
  'attribute_history', 'get_price', 'get_current_data', 'get_security_info', 'order', 'order_target', 'order_value',
  'order_target_value', 'order_target_percent', 'cancel_all', 'run_daily', 'run_weekly', 'run_monthly',
  'set_benchmark', 'set_order_cost', 'set_slippage', 'set_option', 'set_universe', 'log', 'record', 'g',
  'get_context', 'get_param', 'PARAMS', 'set_trailing_stop', 'set_roi', 'set_stop_loss', 'set_take_profit',
  'custom_stoploss', 'custom_exit', 'adjust_position', 'confirm_entry', 'on_bar', 'on_trade',
  'set_protections', 'set_pairlist', 'get_universe', 'is_locked', 'PROTECTIONS', 'PAIRLIST',
])

const TOKEN_RE =
  /("""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$))|([rbfuRBFU]{0,2}"(?:\\.|[^"\\\n])*"?|[rbfuRBFU]{0,2}'(?:\\.|[^'\\\n])*'?)|(#[^\n]*)|(\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|(@[A-Za-z_][\w.]*)|([A-Za-z_][A-Za-z0-9_]*)/g

function highlight(code: string): ReactNode[] {
  const out: ReactNode[] = []
  let last = 0
  let k = 0
  let prevWord = ''
  TOKEN_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = TOKEN_RE.exec(code))) {
    if (m.index > last) out.push(code.slice(last, m.index))
    const t = m[0]
    let cls = ''
    if (m[1] || m[2]) cls = 'tok-str'
    else if (m[3]) cls = 'tok-com'
    else if (m[4]) cls = 'tok-num'
    else if (m[5]) cls = 'tok-dec'
    else if (m[6]) {
      if (KEYWORDS.has(t)) cls = 'tok-kw'
      else if (prevWord === 'def' || prevWord === 'class') cls = 'tok-def'
      else if (SDK_NAMES.has(t)) cls = 'tok-sdk'
      else if (BUILTINS.has(t)) cls = 'tok-bi'
      else if (t === 'ctx' || t === 'self') cls = 'tok-ctx'
      prevWord = t
    }
    out.push(cls ? <span key={k++} className={cls}>{t}</span> : t)
    last = m.index + t.length
    if (m[0].length === 0) TOKEN_RE.lastIndex++
  }
  if (last < code.length) out.push(code.slice(last))
  return out
}

export function CodeEditor({
  value,
  onChange,
  onSave,
  onRun,
  errorLines = [],
  readOnly,
  minHeight = 420,
}: {
  value: string
  onChange?: (v: string) => void
  onSave?: () => void
  onRun?: () => void
  errorLines?: number[]
  readOnly?: boolean
  minHeight?: number
}) {
  const ta = useRef<HTMLTextAreaElement>(null)
  const layer = useRef<HTMLPreElement>(null)
  const gutter = useRef<HTMLDivElement>(null)
  const lines = useMemo(() => value.split('\n').length, [value])
  const tokens = useMemo(() => highlight(value), [value])
  const errorSet = useMemo(() => new Set(errorLines), [errorLines])

  const sync = useCallback(() => {
    const el = ta.current
    if (!el) return
    if (layer.current) {
      layer.current.scrollTop = el.scrollTop
      layer.current.scrollLeft = el.scrollLeft
    }
    if (gutter.current) gutter.current.scrollTop = el.scrollTop
  }, [])

  useEffect(sync, [value, sync])

  const replace = (start: number, end: number, text: string, selStart: number, selEnd = selStart) => {
    const el = ta.current
    if (!el || !onChange) return
    const next = value.slice(0, start) + text + value.slice(end)
    onChange(next)
    requestAnimationFrame(() => {
      el.selectionStart = selStart
      el.selectionEnd = selEnd
    })
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const el = e.currentTarget
    const mod = e.ctrlKey || e.metaKey
    if (mod && (e.key === 's' || e.key === 'S')) {
      e.preventDefault()
      onSave?.()
      return
    }
    if (mod && e.key === 'Enter') {
      e.preventDefault()
      onRun?.()
      return
    }
    if (readOnly) return
    const { selectionStart: s, selectionEnd: t } = el
    const lineStart = value.lastIndexOf('\n', s - 1) + 1
    if (mod && e.key === '/') {
      e.preventDefault()
      const blockEnd = value.indexOf('\n', t - (t > s && value[t - 1] === '\n' ? 1 : 0))
      const end = blockEnd === -1 ? value.length : blockEnd
      const block = value.slice(lineStart, end)
      const rows = block.split('\n')
      const allCommented = rows.filter((r) => r.trim()).every((r) => /^\s*#/.test(r))
      const changed = rows
        .map((r) => (allCommented ? r.replace(/^(\s*)# ?/, '$1') : r.trim() ? r.replace(/^(\s*)/, '$1# ') : r))
        .join('\n')
      replace(lineStart, end, changed, lineStart, lineStart + changed.length)
      return
    }
    if (e.key === 'Tab') {
      e.preventDefault()
      if (s === t && !e.shiftKey) {
        replace(s, t, '    ', s + 4)
        return
      }
      const end = value.indexOf('\n', t - 1) === -1 ? value.length : value.indexOf('\n', t - 1)
      const block = value.slice(lineStart, Math.max(end, t))
      const rows = block.split('\n')
      const changed = rows.map((r) => (e.shiftKey ? r.replace(/^ {1,4}/, '') : r ? '    ' + r : r)).join('\n')
      replace(lineStart, lineStart + block.length, changed, lineStart, lineStart + changed.length)
      return
    }
    if (e.key === 'Enter' && !e.shiftKey && !mod) {
      e.preventDefault()
      const current = value.slice(lineStart, s)
      const indent = current.match(/^\s*/)?.[0] ?? ''
      const extra = /:\s*(#.*)?$/.test(current) ? '    ' : ''
      const ins = '\n' + indent + extra
      replace(s, t, ins, s + ins.length)
      return
    }
    if (e.key === 'Backspace' && s === t && s > lineStart) {
      const before = value.slice(lineStart, s)
      if (/^ +$/.test(before) && before.length % 4 === 0) {
        e.preventDefault()
        replace(s - 4, s, '', s - 4)
      }
    }
  }

  return (
    <div className="code-editor" style={{ minHeight }}>
      <div className="ce-gutter" ref={gutter} aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => (
          <div key={i} className={errorSet.has(i + 1) ? 'err' : undefined}>{i + 1}</div>
        ))}
        <div>&nbsp;</div>
      </div>
      <div className="ce-body">
        <pre className="ce-layer" ref={layer} aria-hidden="true">
          {tokens}
          {'\n'}
        </pre>
        <textarea
          ref={ta}
          className="ce-input"
          value={value}
          onChange={(e) => onChange?.(e.target.value)}
          onScroll={sync}
          onKeyDown={onKeyDown}
          readOnly={readOnly}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          wrap="off"
          aria-label="策略代码编辑器"
        />
      </div>
    </div>
  )
}

/** 只读代码查看器（帖子、回测源码快照、历史版本）。 */
export function CodeView({ code, maxHeight = 520 }: { code: string; maxHeight?: number }) {
  const lines = code.split('\n')
  return (
    <div className="code-view" style={{ maxHeight }}>
      <div className="ce-gutter static" aria-hidden="true">
        {lines.map((_, i) => <div key={i}>{i + 1}</div>)}
      </div>
      <pre className="ce-layer static">{highlight(code)}</pre>
    </div>
  )
}

/** 从错误信息里提取行号："第 12 行"。 */
export function errorLinesFrom(messages: string[]): number[] {
  const out: number[] = []
  for (const m of messages) {
    for (const hit of m.matchAll(/第\s*(\d+)\s*行/g)) out.push(Number(hit[1]))
  }
  return out
}
