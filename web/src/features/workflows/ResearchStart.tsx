import { QuantBot } from '../../components/QuantBot'
import type { Catalog, Flow, Saved } from './types'

type Props = {
  variant: 'empty' | 'missing' | 'error' | 'loading'
  catalog: Catalog | null
  message?: string
  recent?: Saved | null
  onCreate: () => void
  onTemplate: (index: number) => void
  onOpenRecent?: () => void
  onRetry?: () => void
}

const COPY = {
  empty: {
    eyebrow: 'RESEARCH STUDIO',
    title: '从一段可以复核的研究开始',
    lead: '还没有保存的研究。新建空白研究，或从模板开始组织数据、方法与证据。',
  },
  missing: {
    eyebrow: 'RESEARCH UNAVAILABLE',
    title: '这份研究已经不可用',
    lead: '它可能已被删除、移动，或来自另一个项目。你的其他研究不会受影响。',
  },
  error: {
    eyebrow: 'RESEARCH UNAVAILABLE',
    title: '暂时无法读取研究工作区',
    lead: '项目数据没有被修改。可以重试，或先打开最近保存的研究。',
  },
  loading: {
    eyebrow: 'RESEARCH STUDIO',
    title: '正在读取研究路径',
    lead: '正在加载当前项目的研究与运行状态。',
  },
}

export default function ResearchStart({ variant, catalog, message, recent, onCreate, onTemplate, onOpenRecent, onRetry }: Props) {
  const copy = COPY[variant]
  if (variant === 'loading') {
    return (
      <div className="wf-page rs-studio wf-start-page">
        <section className="wf-start" role="status" aria-live="polite">
          <QuantBot size={40} state="thinking" motion="low" title="QuantLab" />
          <span className="rs-eyebrow">{copy.eyebrow}</span>
          <h2>{copy.title}</h2>
          <p>{copy.lead}</p>
        </section>
      </div>
    )
  }
  return (
    <div className="wf-page rs-studio wf-start-page">
      <section className="wf-start" aria-labelledby="wf-start-title">
        <QuantBot size={42} state={variant === 'error' ? 'confused' : 'idle'} motion="low" title="QuantLab" />
        <span className="rs-eyebrow">{copy.eyebrow}</span>
        <h2 id="wf-start-title">{copy.title}</h2>
        <p>{copy.lead}</p>
        {message && <p className="wf-start-error" role="alert">{message}</p>}
        <div className="wf-start-actions">
          {variant === 'empty' && (
            <button type="button" className="button primary" onClick={onCreate}>新建空白研究</button>
          )}
          {recent && onOpenRecent && (
            <button type="button" className="button" onClick={onOpenRecent}>打开最近研究「{recent.title}」</button>
          )}
          {variant !== 'empty' && (
            <button type="button" className="button" onClick={onCreate}>新建研究</button>
          )}
          {onRetry && (
            <button type="button" className="button ghost" onClick={onRetry}>重试</button>
          )}
        </div>
        {variant === 'empty' && (
          <div className="wf-start-templates" aria-label="从模板新建研究">
            <span>或从模板开始</span>
            <div>
              {(catalog?.templates || []).slice(0, 4).map((template: Flow, index: number) => (
                <button key={`${template.title}-${index}`} type="button" className="button small" onClick={() => onTemplate(index)}>
                  {template.title}
                </button>
              ))}
              {!catalog && <small>正在读取模板…</small>}
            </div>
          </div>
        )}
      </section>
    </div>
  )
}
