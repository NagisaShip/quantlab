import { useEffect, useRef, useState } from 'react'
import { demo, type PackageReport } from '../apiDemo'
import { Callout, Field, Loading, Modal, Tag } from './ui'

/** 策略说明书（SPEC.md）：随策略包一起导出，告诉别人这个策略在做什么、该怎么用。 */
export function SpecModal({ slug, onClose, notify }: { slug: string; onClose: () => void; notify: (m: string, e?: boolean) => void }) {
  const [text, setText] = useState<string | null>(null)
  const [draft, setDraft] = useState(false)
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    demo.spec(slug).then((r) => { setText(r.spec); setDraft(r.draft) }).catch((e) => notify((e as Error).message, true))
  }, [slug, notify])
  const save = async () => {
    if (text == null) return
    setSaving(true)
    try { await demo.saveSpec(slug, text); notify('说明书已保存'); onClose() } catch (e) { notify((e as Error).message, true) } finally { setSaving(false) }
  }
  return (
    <Modal title="策略说明书" onClose={onClose} width={760} footer={
      <>
        <a className="button" href={demo.packageUrl(slug)} download>导出策略包</a>
        <button className="button primary" disabled={saving || text == null} onClick={save}>{saving ? '保存中…' : '保存'}</button>
      </>
    }>
      {text == null ? <Loading /> : (
        <>
          {draft && <Callout>这是根据策略的名称、参数和代码注释拼出的初稿，改好后保存。导出策略包时会一起带上。</Callout>}
          <textarea className="spec-editor" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false} aria-label="说明书（Markdown）" />
        </>
      )}
    </Modal>
  )
}

/** 导入策略包：先检查（格式、安全、隔离试跑、标准答案），看过报告再决定导入。 */
export function ImportPackageModal({ onClose, onDone, notify }: { onClose: () => void; onDone: (slug: string) => void; notify: (m: string, e?: boolean) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [report, setReport] = useState<PackageReport | null>(null)
  const [source, setSource] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<'' | 'inspect' | 'import'>('')
  const [name, setName] = useState('')
  const [showCode, setShowCode] = useState(false)
  const input = useRef<HTMLInputElement>(null)

  const pick = async (f: File | null) => {
    setFile(f); setReport(null); setError(''); setSource('')
    if (!f) return
    setBusy('inspect')
    try {
      const r = await demo.inspect(f)
      setReport(r.report); setSource(r.source); setName(String(r.report.manifest.name ?? ''))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }
  const doImport = async () => {
    if (!file) return
    setBusy('import')
    try {
      const r = await demo.importPackage(file, name || undefined)
      notify(`已导入「${r.strategy.name}」：看过代码后批准，才能进入模拟`)
      onDone(r.strategy.slug)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }
  const m = report?.manifest
  return (
    <Modal title="导入策略包" onClose={onClose} width={720} footer={
      <button className="button primary" disabled={!report?.importable || !!busy} onClick={doImport}>{busy === 'import' ? '导入中…' : '导入'}</button>
    }>
      <div style={{ display: 'grid', gap: 12 }}>
        <div className="pkg-drop" onClick={() => input.current?.click()} onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files?.[0] ?? null) }} role="button" tabIndex={0}>
          <input ref={input} type="file" accept=".qstrat,.zip" style={{ display: 'none' }} onChange={(e) => pick(e.target.files?.[0] ?? null)} />
          {file ? <><b>{file.name}</b><small>{(file.size / 1024).toFixed(1)} KB · 点击换一个</small></> : <><b>选择或拖入 .qstrat 文件</b><small>用平台「导出策略包」或 qrun 生成的包</small></>}
        </div>
        {busy === 'inspect' && <Loading label="检查中：格式、安全检查、在隔离进程里重跑标准答案" />}
        {error && <Callout tone="fail">{error}</Callout>}
        {report && m && (
          <>
            <div className="pkg-head">
              <div>
                <b>{m.name}</b> <Tag mono>{m.version || '—'}</Tag>
                <small>作者 {m.author || '未署名'} · {(m.securities ?? []).join('、')} · {m.timeframe} · {report.source_lines} 行代码</small>
              </div>
              {report.demo_ready ? <Tag tone="ok">可上模拟盘</Tag> : report.importable ? <Tag tone="warn">可导入，不能上模拟盘</Tag> : <Tag tone="fail">不能导入</Tag>}
            </div>
            <ul className="checklist">
              {report.checks.map((c) => <li key={c.name} className={c.ok ? '' : c.level === 'warning' ? 'warn' : 'no'}><span><b>{c.name}</b>　{c.detail}</span></li>)}
            </ul>
            {report.importable && !report.golden_match && (
              <Callout tone="warn">标准答案没有核对通过：代码或参数被改过，或者结果和作者给的不一致。可以导入后回测和纸面模拟，但不能上交易所模拟盘。</Callout>
            )}
            {report.importable && <Field label="导入后的名称"><input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} /></Field>}
            <button className="button small ghost" style={{ justifySelf: 'start' }} onClick={() => setShowCode(!showCode)}>{showCode ? '收起代码' : '查看代码'}</button>
            {showCode && <pre className="pkg-code">{source}</pre>}
            {report.spec && <details><summary>说明书</summary><pre className="pkg-code">{report.spec}</pre></details>}
          </>
        )}
      </div>
    </Modal>
  )
}
