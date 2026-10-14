import { useRef, useState } from 'react'
import { v3, type CopilotResult } from '../api2'
import { post } from '../http'
import { CodeView } from './CodeEditor'
import { Markdown } from './markdown'
import { Callout, Field, Modal, Tag } from './ui'

export interface FtReport {
  ok: boolean
  class_name: string
  timeframe: string
  errors: string[]
  warnings: string[]
  features: string[]
  params: Record<string, unknown>
  param_space: Record<string, unknown>
  smoke: { trades?: number; fills?: number; bars?: number; error?: string }
}

export const freqtrade = {
  check: (source: string) => post<FtReport>('/api/freqtrade/check', { source }),
  import: (p: { source: string; name?: string; securities?: string[]; checkup?: boolean }) =>
    post<{ report: FtReport; strategy: { slug: string; name: string } | null; checkup?: { id: string } }>('/api/freqtrade/import', p),
  rewrite: (p: { source: string; errors: string[]; timeframe?: string; securities?: string[] }) => post<CopilotResult>('/api/freqtrade/rewrite', p),
  exportUrl: (slug: string) => `/api/freqtrade/export/${encodeURIComponent(slug)}`,
}

/** 导入 Freqtrade 策略：粘贴或选文件 → 检查（试跑一小段）→ 导入并自动体检；跑不了的写法可以让 AI 改写成平台策略。 */
export function FreqtradeImportModal({ onClose, onDone, notify }: { onClose: () => void; onDone: (slug: string) => void; notify: (m: string, e?: boolean) => void }) {
  const [source, setSource] = useState('')
  const [report, setReport] = useState<FtReport | null>(null)
  const [name, setName] = useState('')
  const [secs, setSecs] = useState('BTC/USDT')
  const [busy, setBusy] = useState<'' | 'check' | 'import' | 'rewrite' | 'save'>('')
  const [error, setError] = useState('')
  const [ai, setAi] = useState<CopilotResult | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const securities = secs.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean)

  const check = async (text = source) => {
    if (!text.trim()) return
    setBusy('check'); setError(''); setReport(null); setAi(null)
    try { const r = await freqtrade.check(text); setReport(r); setName((n) => n || r.class_name) } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  const pick = async (f: File | null) => {
    if (!f) return
    const text = await f.text()
    setSource(text); setName('')
    check(text)
  }
  const doImport = async () => {
    setBusy('import'); setError('')
    try {
      const r = await freqtrade.import({ source, name: name || undefined, securities, checkup: true })
      if (!r.strategy) { setReport(r.report); return }
      notify(`已导入「${r.strategy.name}」，体检已在后台开始`)
      onDone(r.strategy.slug)
    } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  const rewrite = async () => {
    setBusy('rewrite'); setError('')
    try { setAi(await freqtrade.rewrite({ source, errors: report?.errors ?? [], timeframe: report?.timeframe || '1h', securities })) } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  const saveAi = async () => {
    if (!ai?.code) return
    setBusy('save')
    try {
      const r = await v3.newStrategy({ name: `${name || report?.class_name || 'Freqtrade'}（AI 改写）`, code: ai.code, securities, timeframe: report?.timeframe || '1h', notes: '由 AI 从 Freqtrade 策略改写', origin: 'agent' })
      notify('已保存为新策略，看过代码后批准才能进入模拟')
      onDone(r.strategy.slug)
    } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }

  const footer = ai
    ? <button className="button primary" disabled={!ai.code || !!busy} onClick={saveAi}>{busy === 'save' ? '保存中…' : '保存为新策略'}</button>
    : report && !report.ok
      ? <button className="button primary" disabled={!!busy} onClick={rewrite}>{busy === 'rewrite' ? 'AI 改写中…' : '✦ 让 AI 改写成平台策略'}</button>
      : report?.ok
        ? <button className="button primary" disabled={!!busy} onClick={doImport}>{busy === 'import' ? '导入中…' : '导入并体检'}</button>
        : <button className="button primary" disabled={!source.trim() || !!busy} onClick={() => check()}>{busy === 'check' ? '检查中…' : '检查'}</button>

  return (
    <Modal title="导入 Freqtrade 策略" onClose={onClose} width={760} footer={footer}>
      <div style={{ display: 'grid', gap: 12 }}>
        {!report && (
          <>
            <div className="pkg-drop" onClick={() => input.current?.click()} onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files?.[0] ?? null) }} role="button" tabIndex={0}>
              <input ref={input} type="file" accept=".py" style={{ display: 'none' }} onChange={(e) => pick(e.target.files?.[0] ?? null)} />
              <strong>选择或拖入策略文件（.py）</strong>
              <small>也可以直接把代码粘贴到下面</small>
            </div>
            <textarea className="spec-editor ft-paste" value={source} onChange={(e) => setSource(e.target.value)} spellCheck={false}
              placeholder="class MyStrategy(IStrategy): ..." aria-label="Freqtrade 策略代码" />
            <p className="hint">策略类原样保留，只把 import 换成平台的兼容层（TA-Lib 常用指标、qtpylib、hyperopt 参数、ROI、止损、追踪止损、custom_stoploss、大周期 informative 都能用）。只支持现货多头。</p>
          </>
        )}
        {report && (
          <>
            <div className="ft-head">
              <Tag tone={report.ok ? 'ok' : 'fail'}>{report.ok ? '可以导入' : '暂时跑不了'}</Tag>
              <strong>{report.class_name || '未识别的类'}</strong>
              <span className="hint">{report.timeframe} · {Object.keys(report.params).length} 个可调参数{report.smoke?.trades != null ? ` · 试跑 400 根 K 线成交 ${report.smoke.trades} 笔` : ''}</span>
              <span className="spacer" />
              <button className="button small" onClick={() => { setReport(null); setAi(null) }}>重新选择</button>
            </div>
            {report.features.length > 0 && <div className="chips">{report.features.map((f) => <Tag key={f}>{f}</Tag>)}</div>}
            {report.errors.length > 0 && <Callout tone="fail" title="跑不了的原因">{report.errors.map((e) => <div key={e}>· {e}</div>)}</Callout>}
            {report.warnings.length > 0 && <Callout tone="warn" title="会被忽略或行为不同">{report.warnings.map((e) => <div key={e}>· {e}</div>)}</Callout>}
            {report.ok && (
              <div className="two-columns even">
                <Field label="策略名称"><input value={name} onChange={(e) => setName(e.target.value)} /></Field>
                <Field label="交易对" hint="多个用逗号分开"><input value={secs} onChange={(e) => setSecs(e.target.value)} /></Field>
              </div>
            )}
            {report.ok && <p className="hint">导入后自动开始一次体检（未来函数、过拟合、成本敏感度）。和 Freqtrade 一样按收盘价出信号、下一根开盘成交。</p>}
            {ai && (
              <>
                {ai.mode === 'fallback' && <Callout tone="warn">没有可用的模型，改写没有完成。请先在设置里配置模型密钥。</Callout>}
                {ai.content && <Markdown text={ai.content} />}
                {ai.code && <CodeView code={ai.code} />}
                {ai.code && <p className="hint">{ai.checks?.ok ? '试跑通过' : `试跑有问题：${[...(ai.checks?.issues ?? []), ...(ai.checks?.errors ?? [])].join('；')}`}</p>}
              </>
            )}
          </>
        )}
        {error && <Callout tone="fail">{error}</Callout>}
      </div>
    </Modal>
  )
}
