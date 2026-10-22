/**
 * 上下文包：把「这一轮会进入模型的内容」展示成只读页面。
 *
 * 数据来自 GET /api/lab/projects/{pid}/context-pack（服务端只读投影）：
 * system/项目规则/来源引用/items/预算。页面只展示与导出，不改变提示词、不写入对话。
 * 入口：侧栏「自动化 → 上下文包」；也支持 #/context-pack/<pid> 深链。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { lab, type ContextPack as ContextPackData, type ContextPackItem, type LabProjectBrief } from '../apiLab'
import { fmtTokens } from '../components/modelUsage'
import { Callout, EmptyState, Heading, Loading, MetricCard, Panel, Tag } from '../components/ui'
import type { PageProps } from './types'

const KIND_LABEL: Record<string, string> = {
  strategy: '策略',
  factor: '因子',
  model: '模型',
  portfolio: '组合',
  execution: '执行',
  research: '研究',
  workflow: '工作流',
  ask: '问答',
}
const STATUS_LABEL: Record<string, string> = {
  running: '运行中',
  awaiting_user: '等待你',
  idle: '空闲',
}

const kindLabel = (kind?: string) => KIND_LABEL[kind || ''] || KIND_LABEL.strategy
const statusLabel = (status?: string) => (status ? STATUS_LABEL[status] || '' : '')
const messageOf = (error: unknown) => (error instanceof Error ? error.message : String(error))
const when = (ts?: number) => {
  if (!ts) return ''
  try {
    // 后端项目时间戳按秒返回；兼容毫秒值。
    return new Date(ts < 1e12 ? ts * 1000 : ts).toLocaleString()
  } catch {
    return ''
  }
}
const preview = (value: unknown) => {
  let text = ''
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value)
  } catch {
    text = String(value)
  }
  return text.length > 240 ? text.slice(0, 240) + '…' : text
}
const itemTone = (item: ContextPackItem) => item.key === 'system' ? 'info' : item.key === 'context' ? 'ok' : ''

export default function ContextPackPage({ arg = '', onNavigate, notify }: PageProps & { arg?: string }) {
  const [pid, setPid] = useState(arg)
  const [projects, setProjects] = useState<LabProjectBrief[] | null>(null)
  const [projectsError, setProjectsError] = useState('')
  const [pack, setPack] = useState<ContextPackData | null>(null)
  const [packError, setPackError] = useState('')
  const [loading, setLoading] = useState(false)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    setPid(arg)
  }, [arg])

  useEffect(() => {
    if (pid) return
    let active = true
    setProjectsError('')
    lab.projects()
      .then((value) => { if (active) setProjects(value.projects) })
      .catch((error) => {
        if (!active) return
        setProjects([])
        setProjectsError(messageOf(error))
      })
    return () => { active = false }
  }, [pid, nonce])

  useEffect(() => {
    if (!pid) {
      setPack(null)
      setLoading(false)
      return
    }
    let active = true
    setLoading(true)
    setPackError('')
    lab.contextPack(pid)
      .then((value) => { if (active) setPack(value) })
      .catch((error) => {
        if (!active) return
        setPack(null)
        setPackError(messageOf(error))
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [pid, nonce])

  const choose = useCallback((id: string) => {
    setPid(id)
    onNavigate(`context-pack/${encodeURIComponent(id)}`)
  }, [onNavigate])

  const reload = useCallback(() => setNonce((value) => value + 1), [])

  const copyJson = async () => {
    if (!pack) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(pack, null, 2))
      notify('已复制上下文包 JSON')
    } catch {
      notify('复制失败，请手动选择文本。', true)
    }
  }

  const systemItem = useMemo(() => pack?.items.find((item) => item.key === 'system') || null, [pack])
  const instructionItem = useMemo(() => pack?.items.find((item) => item.key === 'instructions') || null, [pack])
  const contextKeys = useMemo(() => Object.entries(pack?.context || {}), [pack])
  const budgetPct = pack && pack.budget > 0 ? Math.min(100, (pack.total / pack.budget) * 100) : 0

  const actions = pid ? (
    <>
      <button className="button" onClick={reload}>{'刷新'}</button>
      <button className="button" onClick={() => void copyJson()} disabled={!pack}>{'复制 JSON'}</button>
      <button className="button primary" onClick={() => onNavigate(`lab/${encodeURIComponent(pid)}`)}>{'在 AI 研究室打开'}</button>
    </>
  ) : undefined

  return (
    <>
      <Heading
        title="上下文包"
        description="只读展示这一轮会进入模型的系统规则、项目规则、来源引用与 token 预算；不改变提示词，也不写入对话。"
        actions={actions}
      />

      {!pid && (
        <Panel
          title="选择会话"
          subtitle="选择一个 AI 研究室会话，查看它这一轮会进入模型的上下文。"
        >
          {projectsError ? (
            <Callout tone="fail" title="读取会话列表失败">
              {projectsError}
              <div className="ctxpack-actions">
                <button className="button small" onClick={reload}>{'重试'}</button>
                <button className="button small" onClick={() => onNavigate('lab')}>{'打开 AI 研究室'}</button>
              </div>
            </Callout>
          ) : projects === null ? (
            <Loading label="正在读取会话" />
          ) : projects.length === 0 ? (
            <EmptyState
              title="还没有会话"
              action={<button className="button primary" onClick={() => onNavigate('lab')}>{'打开 AI 研究室'}</button>}
            >
              当前工作区还没有会话，先到 AI 研究室新建一个。
            </EmptyState>
          ) : (
            <div className="ctxpack-projects">
              {projects.map((project) => (
                <button key={project.id} type="button" className="ctxpack-project" onClick={() => choose(project.id)}>
                  <span className="ctxpack-project-main">
                    <b>{project.title || '未命名会话'}</b>
                    <small>{kindLabel(project.kind)}</small>
                  </span>
                  <span className="ctxpack-project-meta">
                    {statusLabel(project.status) && (
                      <Tag tone={project.status === 'running' ? 'warn' : ''}>{statusLabel(project.status)}</Tag>
                    )}
                    <small>最近更新 {when(project.updated_at)}</small>
                  </span>
                </button>
              ))}
            </div>
          )}
        </Panel>
      )}

      {pid && packError && (
        <Callout tone="fail" title="读取上下文包失败">
          {packError}
          <div className="ctxpack-actions">
            <button className="button small" onClick={reload}>{'重试'}</button>
            <button className="button small" onClick={() => onNavigate('context-pack')}>{'返回列表'}</button>
          </div>
        </Callout>
      )}

      {pid && !packError && (loading && !pack ? (
        <Loading label="正在读取上下文包" />
      ) : pack ? (
        <div className="ctxpack">
          <div className="ctxpack-head">
            <div className="ctxpack-title">
              <h2>{pack.title || '未命名会话'}</h2>
              <p>
                <Tag tone="info">{kindLabel(pack.kind)}</Tag>
                <code>{pack.project_id}</code>
                <span className="ctxpack-model">{pack.model || '默认模型'}</span>
              </p>
            </div>
            <div className="ctxpack-head-meta">
              <Tag mono>{pack.format}</Tag>
              <Tag>{'只读'}</Tag>
            </div>
          </div>

          <div className="ctxpack-kpis">
            <MetricCard
              label="上下文预算"
              value={fmtTokens(pack.budget)}
              unit="token"
              progress={budgetPct}
              caption="模型上下文窗口上限"
            />
            <MetricCard
              label="已用"
              value={fmtTokens(pack.total)}
              unit="token"
              caption={<>{pack.items.length} {'条'}</>}
            />
            <MetricCard
              label="历史原文预算"
              value={fmtTokens(pack.history_budget)}
              unit="token"
              caption="历史对话的原文预算"
            />
            <MetricCard
              label="自动压缩阈值"
              value={fmtTokens(pack.auto_compact_tokens)}
              unit="token"
              caption="达到阈值后自动压缩更早的对话"
            />
          </div>

          <div className="ctxpack-columns">
            <Panel
              title="系统规则"
              subtitle="平台注入，每轮都发送"
              actions={systemItem ? <Tag mono>{fmtTokens(systemItem.tokens)} token</Tag> : undefined}
            >
              {systemItem ? (
                <>
                  <p className="ctxpack-strong">{systemItem.title}</p>
                  <p className="hint">完整系统提示词由服务端注入，这里只展示名称与占用。</p>
                </>
              ) : (
                <p className="hint">没有可展示的系统规则条目。</p>
              )}
            </Panel>

            <Panel
              title="项目规则"
              subtitle="研究者设定，作为固定条目进入每轮提示词"
              actions={instructionItem ? <Tag mono>{fmtTokens(instructionItem.tokens)} token</Tag> : undefined}
            >
              {pack.instructions ? (
                <pre className="ctxpack-pre">{pack.instructions}</pre>
              ) : (
                <p className="hint">没有设置项目规则。</p>
              )}
            </Panel>
          </div>

          <Panel
            title="来源引用"
            subtitle="来源引用只含标题与 ID，不内联正文"
            actions={<Tag mono>{contextKeys.length} {'项'}</Tag>}
          >
            {pack.sources ? (
              <pre className="ctxpack-pre">{pack.sources}</pre>
            ) : (
              <p className="hint">没有来源引用。</p>
            )}
            {contextKeys.length > 0 && (
              <details className="ctxpack-details">
                <summary>项目上下文键<span>（{contextKeys.length}）</span></summary>
                <div className="ctxpack-sources">
                  {contextKeys.map(([key, value]) => (
                    <div key={key} className="ctxpack-source">
                      <code>{key}</code>
                      <span>{preview(value)}</span>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </Panel>

          <Panel
            title="上下文条目"
            subtitle="这些内容会进入模型上下文；token 为估算值"
            flush
            actions={<Tag mono>{pack.items.length} {'条'}</Tag>}
          >
            {pack.items.length === 0 ? (
              <p className="ctxpack-empty">没有可展示的上下文条目。</p>
            ) : (
              <div className="table-wrap">
                <table className="ctxpack-table" aria-label="上下文包条目">
                  <thead>
                    <tr>
                      <th scope="col">{'条目'}</th>
                      <th scope="col">{'类型'}</th>
                      <th scope="col" className="ctxpack-num">{'token'}</th>
                      <th scope="col">{'占比'}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pack.items.map((item) => (
                      <tr key={item.key}>
                        <td>
                          <b>{item.title}</b>
                          <code>{item.key}</code>
                        </td>
                        <td><Tag mono tone={itemTone(item)}>{item.fixed ? '固定' : '可选'}</Tag></td>
                        <td className="ctxpack-num">{fmtTokens(item.tokens)}</td>
                        <td>
                          <span className="ctxpack-bar" aria-hidden="true">
                            <i style={{ width: `${pack.total > 0 ? Math.max(1, (item.tokens / pack.total) * 100) : 0}%` }} />
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      ) : null)}
    </>
  )
}
