import { useMemo, useState, type ReactNode } from 'react'
import { copyText, downloadBlob } from './protocol'

/**
 * 专业代码块（只展示，不执行）：语法高亮、行号、一键复制 / 下载、长代码折叠、文件名、错误行定位、Diff。
 *
 * 围栏信息写法：```python title=strategy.py {3,7-9}``` 或 ```python:strategy.py```；
 * `{…}` 里的行号会标成错误 / 重点行；语言写 diff（或内容是 +/- 开头的统一 diff）时按差异渲染。
 * 「执行代码」的结果用 ql:run 块（见 RunResult），与这里严格分开。
 */

const KW: Record<string, string[]> = {
  python: ['False', 'None', 'True', 'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield', 'self'],
  js: ['const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break', 'continue', 'new', 'class', 'extends', 'import', 'export', 'from', 'default', 'async', 'await', 'try', 'catch', 'finally', 'throw', 'typeof', 'instanceof', 'in', 'of', 'true', 'false', 'null', 'undefined', 'this', 'interface', 'type', 'enum', 'implements', 'readonly', 'as'],
  sql: ['select', 'from', 'where', 'group', 'by', 'order', 'having', 'limit', 'join', 'left', 'right', 'inner', 'outer', 'on', 'as', 'and', 'or', 'not', 'in', 'is', 'null', 'insert', 'into', 'values', 'update', 'set', 'delete', 'create', 'table', 'with', 'case', 'when', 'then', 'else', 'end', 'distinct', 'union', 'over', 'partition'],
  bash: ['if', 'then', 'else', 'fi', 'for', 'do', 'done', 'while', 'case', 'esac', 'function', 'export', 'echo', 'cd', 'pip', 'python', 'npm', 'git', 'curl'],
  json: ['true', 'false', 'null'],
}
const ALIAS: Record<string, string> = {
  py: 'python', python3: 'python', javascript: 'js', ts: 'js', typescript: 'js', tsx: 'js', jsx: 'js',
  sh: 'bash', shell: 'bash', zsh: 'bash', powershell: 'bash', ps1: 'bash', postgres: 'sql', mysql: 'sql',
}

function langOf(lang: string): string {
  const l = lang.toLowerCase()
  return ALIAS[l] || l
}

const TOKEN = /("""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)|`(?:\\.|[^`\\])*`?|"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?)|(#[^\n]*|\/\/[^\n]*|--[^\n]*)|(\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|(@[A-Za-z_][\w.]*)|([A-Za-z_$][\w$]*)/g

function highlightLine(line: string, lang: string): ReactNode[] {
  const words = new Set(KW[lang] || [])
  const ci = lang === 'sql'
  const out: ReactNode[] = []
  let last = 0
  let k = 0
  let prev = ''
  TOKEN.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = TOKEN.exec(line))) {
    if (m.index > last) out.push(line.slice(last, m.index))
    const t = m[0]
    let cls = ''
    if (m[1]) cls = 'tok-str'
    else if (m[2]) {
      // 注释：python / bash 用 #，js 用 //，sql 用 --
      const c = t.startsWith('#') ? ['python', 'bash', 'yaml', 'toml'].includes(lang) : t.startsWith('//') ? lang === 'js' : lang === 'sql'
      if (!c) { out.push(t[0]); last = m.index + 1; TOKEN.lastIndex = m.index + 1; continue }
      cls = 'tok-com'
    } else if (m[3]) cls = 'tok-num'
    else if (m[4]) cls = lang === 'python' ? 'tok-dec' : ''
    else if (m[5]) {
      if (words.has(ci ? t.toLowerCase() : t)) cls = 'tok-kw'
      else if (prev === 'def' || prev === 'class' || prev === 'function') cls = 'tok-def'
      else if (line.charAt(m.index + t.length) === '(') cls = 'tok-bi'
      prev = t
    }
    out.push(cls ? <span key={k++} className={cls}>{t}</span> : t)
    last = m.index + t.length
    if (!t.length) TOKEN.lastIndex++
  }
  if (last < line.length) out.push(line.slice(last))
  return out
}

/** 解析围栏信息：语言、文件名、标记行。 */
export function parseFence(info: string): { lang: string; filename: string; marks: Set<number> } {
  const marks = new Set<number>()
  let rest = info.trim()
  const brace = rest.match(/\{([\d,\s-]+)\}/)
  if (brace) {
    for (const part of brace[1].split(',')) {
      const [a, b] = part.split('-').map((x) => Number(x.trim()))
      if (Number.isFinite(a)) for (let i = a; i <= (Number.isFinite(b) ? b : a); i++) marks.add(i)
    }
    rest = rest.replace(brace[0], '').trim()
  }
  let filename = ''
  const title = rest.match(/(?:title|file|filename)=("([^"]+)"|(\S+))/)
  if (title) {
    filename = title[2] || title[3]
    rest = rest.replace(title[0], '').trim()
  }
  let lang = rest.split(/\s+/)[0] || ''
  if (lang.includes(':') && !lang.startsWith('ql:')) {
    const [l, f] = lang.split(':')
    lang = l
    filename = filename || f
  }
  return { lang, filename, marks }
}

const FOLD_AT = 28

export function CodeBlock({ code, info = '', errorLines, maxHeight = 520 }: { code: string; info?: string; errorLines?: number[]; maxHeight?: number }) {
  const { lang: rawLang, filename, marks } = parseFence(info)
  const lang = langOf(rawLang)
  const lines = useMemo(() => code.replace(/\n$/, '').split('\n'), [code])
  const isDiff = lang === 'diff' || lang === 'patch' || (lines.length > 2 && lines.filter((l) => /^[+-](?![+-])/.test(l)).length >= lines.length * 0.4 && lines.some((l) => l.startsWith('@@')))
  const [open, setOpen] = useState(lines.length <= FOLD_AT)
  const [copied, setCopied] = useState(false)
  const errs = new Set([...marks, ...(errorLines || [])])
  const shown = open ? lines : lines.slice(0, 12)
  const stats = isDiff ? { add: lines.filter((l) => l.startsWith('+') && !l.startsWith('+++')).length, del: lines.filter((l) => l.startsWith('-') && !l.startsWith('---')).length } : null
  const ext = { python: 'py', js: 'ts', bash: 'sh', json: 'json', sql: 'sql', diff: 'diff' }[lang] || 'txt'
  return (
    <figure className={`qb-code${isDiff ? ' diff' : ''}`}>
      <figcaption className="qb-code-head">
        <span className="qb-code-name">{filename || (isDiff ? '代码差异' : rawLang || '代码')}</span>
        {filename && rawLang && <span className="qb-code-lang">{rawLang}</span>}
        {stats && <span className="qb-code-diffstat"><b className="add">+{stats.add}</b> <b className="del">−{stats.del}</b></span>}
        <span className="qb-code-tag" title="展示用代码，不会被执行">展示</span>
        <span className="qb-grow" />
        <span className="qb-code-count">{lines.length} 行</span>
        <button type="button" className="qb-icon-btn" onClick={() => downloadBlob(filename || `snippet.${ext}`, code)} title="下载">下载</button>
        <button type="button" className="qb-icon-btn" onClick={async () => { setCopied(await copyText(code)); setTimeout(() => setCopied(false), 1400) }} title="复制">
          {copied ? '已复制' : '复制'}
        </button>
      </figcaption>
      <div className="qb-code-body" style={{ maxHeight: open ? maxHeight : undefined }}>
        <table>
          <tbody>
            {shown.map((ln, i) => {
              const no = i + 1
              const cls = isDiff ? (ln.startsWith('+') && !ln.startsWith('+++') ? 'add' : ln.startsWith('-') && !ln.startsWith('---') ? 'del' : ln.startsWith('@@') ? 'hunk' : '') : ''
              return (
                <tr key={i} className={`${cls}${errs.has(no) ? ' err' : ''}`} id={undefined}>
                  <td className="qb-ln" aria-hidden>{no}</td>
                  <td className="qb-src">{isDiff ? ln || ' ' : ln ? highlightLine(ln, lang) : ' '}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {lines.length > FOLD_AT && (
        <button type="button" className="qb-fold" onClick={() => setOpen(!open)}>
          {open ? '收起' : `展开全部 ${lines.length} 行`}
        </button>
      )}
      {errs.size > 0 && (
        <div className="qb-code-errs">
          定位：{[...errs].sort((a, b) => a - b).slice(0, 12).map((n) => <span key={n} className="tag warn mono">第 {n} 行</span>)}
        </div>
      )}
    </figure>
  )
}

/** 代码执行结果：状态、退出码、耗时、标准输出 / 标准错误，与展示代码明确区分。 */
export function RunResult({ data }: { data: Record<string, unknown> }) {
  const status = String(data.status || (data.exit_code === 0 ? 'ok' : data.exit_code === undefined ? 'running' : 'error'))
  const [tab, setTab] = useState<'stdout' | 'stderr' | 'code'>(data.stderr && status === 'error' ? 'stderr' : 'stdout')
  const stdout = String(data.stdout ?? '')
  const stderr = String(data.stderr ?? '')
  const code = typeof data.code === 'string' ? data.code : ''
  const errLines = (stderr.match(/line (\d+)/g) || []).map((x) => Number(x.replace(/\D/g, '')))
  const label = status === 'ok' ? '运行成功' : status === 'running' ? '运行中' : status === 'timeout' ? '超时' : '运行失败'
  return (
    <div className={`qb-run st-${status}`}>
      <div className="qb-run-head">
        <span className={`qb-dot st-${status}`} />
        <b>{String(data.title || '执行结果')}</b>
        <span className="qb-code-tag run">已执行</span>
        <span className="qb-grow" />
        <span>{label}</span>
        {data.exit_code !== undefined && <span className="mono">exit {String(data.exit_code)}</span>}
        {typeof data.duration_ms === 'number' && <span className="mono">{(Number(data.duration_ms) / 1000).toFixed(2)}s</span>}
      </div>
      <div className="qb-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'stdout'} className={tab === 'stdout' ? 'on' : ''} onClick={() => setTab('stdout')}>标准输出</button>
        <button type="button" role="tab" aria-selected={tab === 'stderr'} className={tab === 'stderr' ? 'on' : ''} onClick={() => setTab('stderr')}>
          报错{stderr ? <i className="qb-badge">{stderr.split('\n').filter(Boolean).length}</i> : null}
        </button>
        {code && <button type="button" role="tab" aria-selected={tab === 'code'} className={tab === 'code' ? 'on' : ''} onClick={() => setTab('code')}>代码</button>}
      </div>
      {tab === 'code' ? <CodeBlock code={code} info={String(data.lang || 'python')} errorLines={errLines} /> : (
        <pre className={`qb-run-out${tab === 'stderr' ? ' err' : ''}`}>{(tab === 'stdout' ? stdout : stderr) || (tab === 'stdout' ? '（无输出）' : '（无报错）')}</pre>
      )}
    </div>
  )
}
