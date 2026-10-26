/**
 * 调研报告（/report 生成）：带 QuantLab 标识的报告模板，可以直接用浏览器「打印 → 另存为 PDF」导出。
 * 网址：#/report/<对话 id>/<报告 id>，带 ?print=1 时打开后自动弹出打印（导出 PDF）。
 */
import { useEffect, useState } from 'react'
import { lab, type Report } from '../apiLab'
import { Markdown } from '../components/markdown'
import { QuantBot } from '../components/QuantBot'
import { EmptyState, Loading } from '../components/ui'
import { PresenceBar, ShareToButton, usePresence } from '../components/Social'
import type { PageProps } from './types'

const day = (t: number) => new Date(t < 1e12 ? t * 1000 : t).toLocaleString('zh-CN', { hour12: false })

export default function ReportPage({ arg, notify, onNavigate }: PageProps & { arg?: string }) {
  const [path, query] = (arg || '').split('?')
  const [pid, rid] = path.split('/')
  const others = usePresence('report', pid && rid ? `${pid}/${rid}` : undefined)
  const autoPrint = new URLSearchParams(query || '').get('print') === '1'
  const [data, setData] = useState<{ report: Report; project: { id: string; title: string; securities: string[]; timeframe: string } } | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!pid || !rid) { setError('报告地址不完整'); return }
    lab.getReport(pid, rid).then(setData).catch((e: Error) => setError(e.message))
  }, [pid, rid])

  // 打印时只输出报告本身（隐藏侧栏、顶栏等）
  useEffect(() => {
    document.body.classList.add('rp-printing')
    return () => document.body.classList.remove('rp-printing')
  }, [])
  useEffect(() => {
    if (!data) return
    document.title = `${data.report.title} · QuantLab v0.1.0-preview.2 调研报告`
    if (autoPrint) { const t = window.setTimeout(() => window.print(), 600); return () => window.clearTimeout(t) }
  }, [data, autoPrint])

  if (error) return <EmptyState title="打不开这份报告">{error}</EmptyState>
  if (!data) return <Loading label="正在打开报告" />
  const r = data.report
  const exportPdf = () => { notify('在打印对话框里选择「另存为 PDF」'); window.setTimeout(() => window.print(), 200) }
  return (
    <div className="rp-wrap anim-in">
      <div className="rp-actions no-print">
        <a className="button small ghost" href={`#/lab/${pid}`}>← 返回对话</a>
        <PresenceBar others={others} />
        <span className="lab2-card-gap" />
        <ShareToButton item={{ type: 'report', id: `${pid}/${rid}`, title: r.title }} notify={notify} onNavigate={onNavigate} />
        <button className="button small" onClick={() => navigator.clipboard?.writeText(r.markdown).then(() => notify('已复制 Markdown'))}>复制 Markdown</button>
        <button className="button small primary" onClick={exportPdf}>导出 PDF</button>
      </div>
      <article className="rp-doc">
        <header className="rp-brand">
          <span className="rp-logo"><QuantBot size={34} state="idle" motion="none" title="QuantLab" /></span>
          <div>
            <b>QuantLab v0.1.0-preview.2 量化研究台</b>
            <small>AI 研究室 · 调研报告</small>
          </div>
          <span className="rp-no">No. {r.id.toUpperCase()}</span>
        </header>
        <h1 className="rp-title">{r.title}</h1>
        <dl className="rp-meta">
          <div><dt>生成时间</dt><dd>{day(r.created_at)}</dd></div>
          <div><dt>来源对话</dt><dd>{data.project.title}</dd></div>
          {r.securities?.length > 0 && <div><dt>标的</dt><dd>{r.securities.join(', ')}{r.timeframe ? ` · ${r.timeframe}` : ''}</dd></div>}
          {r.model && <div><dt>模型</dt><dd>{r.model}</dd></div>}
        </dl>
        {r.summary && (
          <section className="rp-summary">
            <b>摘要</b>
            <p>{r.summary}</p>
          </section>
        )}
        <section className="rp-body">
          <Markdown text={r.markdown} />
        </section>
        {r.sources?.length > 0 && (
          <section className="rp-sources">
            <h2>参考来源</h2>
            <ol>{r.sources.map((s) => <li key={s}>{/^https?:\/\//.test(s) ? <a href={s} target="_blank" rel="noreferrer">{s}</a> : s}</li>)}</ol>
          </section>
        )}
        <footer className="rp-foot">
          <span>QuantLab v0.1.0-preview.2 量化研究台 · 由 AI 研究室生成，结论需结合原始数据复核</span>
          <span>仅为研究结果，不构成投资建议</span>
        </footer>
      </article>
    </div>
  )
}
