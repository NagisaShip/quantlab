import { useEffect, useMemo, useState } from 'react'
import { api, type PromptDoc } from '../api/core'
import { Callout, EmptyState, Heading, Loading, Panel, SourceBanner, Tag } from '../components/ui'
import type { PageProps } from './types'

export default function PromptLibraryPage({ notify }: PageProps) {
  const [prompts, setPrompts] = useState<PromptDoc[]>([])
  const [selected, setSelected] = useState('')
  const [kind, setKind] = useState<'all' | 'task' | 'role'>('all')
  const [error, setError] = useState('')

  useEffect(() => {
    api
      .prompts()
      .then((d) => {
        setPrompts(d.prompts)
        setSelected(d.prompts[0]?.id ?? '')
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  const visible = useMemo(
    () => prompts.filter((p) => kind === 'all' || (p.kind ?? 'task') === kind),
    [prompts, kind],
  )
  const current = prompts.find((p) => p.id === selected)
  const tasks = prompts.filter((p) => (p.kind ?? 'task') === 'task')
  const rolePrompts = prompts.filter((p) => p.kind === 'role')

  const copy = async () => {
    if (!current) return
    try {
      await navigator.clipboard.writeText(current.body)
      notify('提示词已复制到剪贴板')
    } catch {
      notify('浏览器拒绝了剪贴板访问，请手动选择文本', true)
    }
  }

  return (
    <>
      <Heading
        eyebrow="PROMPT LIBRARY"
        title="提示词"
        description="两类提示词：任务级模板（生成策略、审查、调研、简报）存放在版本化的 Markdown 文件里；角色级提示词直接来自角色库，改角色即改提示词，不重复维护两份。"
      />

      {error && <Callout tone="fail" title="读取失败">{error}</Callout>}

      <SourceBanner
        icon="⌸"
        title={`${tasks.length} 个任务模板 · ${rolePrompts.length} 条角色提示词`}
        detail="模板带版本号与变量清单，运行记录里的哈希会随改动变化——这样才谈得上可复现"
        right="提示词"
      />

      <Callout title="这些模板在做什么">
        把「约束」和「输出格式」写死，把「数据」用分隔符隔开，把变量留成占位符。
        这样同一个模板既能被人参考，也能被 Agent 自动套用，两边不会漂移。
      </Callout>

      {!prompts.length ? (
        <Loading />
      ) : (
        <div className="two-columns prompt-split">
          <Panel
            title={`提示词（${visible.length} / ${prompts.length}）`}
            actions={
              <select value={kind} onChange={(e) => setKind(e.target.value as typeof kind)} aria-label="按类型筛选">
                <option value="all">全部</option>
                <option value="task">任务模板（{tasks.length}）</option>
                <option value="role">角色提示词（{rolePrompts.length}）</option>
              </select>
            }
            flush
          >
            <div style={{ maxHeight: 640, overflowY: 'auto' }}>
              {visible.map((p) => (
                <button
                  key={p.id}
                  onClick={() => setSelected(p.id)}
                  style={{
                    display: 'block',
                    width: '100%',
                    textAlign: 'left',
                    padding: '12px 18px',
                    border: 0,
                    borderBottom: '1px solid var(--line-2)',
                    borderLeft: `3px solid ${p.id === selected ? 'var(--accent)' : 'transparent'}`,
                    background: p.id === selected ? 'var(--sunken)' : 'transparent',
                    font: 'inherit',
                    fontSize: 12,
                  }}
                >
                  <div style={{ color: 'var(--ink)', fontWeight: p.id === selected ? 600 : 400 }}>
                    {p.title ?? p.id}
                  </div>
                  <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--faint)', marginTop: 4 }}>
                    {p.id}
                    {p.kind === 'role' && ' · 角色'}
                  </div>
                </button>
              ))}
            </div>
          </Panel>

          <div>
            {current ? (
              <Panel
                title={current.title ?? current.id}
                actions={
                  <>
                    <Tag mono>v{current.version ?? '1'}</Tag>
                    {current.temperature && <Tag mono>温度 {current.temperature}</Tag>}
                    {current.model && <Tag mono>{current.model}</Tag>}
                    <button className="button small" onClick={copy}>复制</button>
                  </>
                }
              >
                {current.variables && (
                  <div style={{ marginBottom: 14 }}>
                    <div className="hint" style={{ marginBottom: 6 }}>变量</div>
                    <div className="card-tags">
                      {current.variables.split(',').map((v) => (
                        <Tag key={v} mono>{`{{${v.trim()}}}`}</Tag>
                      ))}
                    </div>
                  </div>
                )}
                {current.tools && current.tools.length > 0 && (
                  <div style={{ marginBottom: 14 }}>
                    <div className="hint" style={{ marginBottom: 6 }}>可调用工具</div>
                    <div className="card-tags">
                      {current.tools.map((t) => <Tag key={t} mono>{t}</Tag>)}
                    </div>
                  </div>
                )}
                <pre className="code">{current.body}</pre>
                {current.output_format && (
                  <div className="hint" style={{ marginTop: 12 }}>期望输出格式：{current.output_format}</div>
                )}
                <div className="hint" style={{ marginTop: 14 }}>
                  {current.kind === 'role' ? (
                    <>
                      角色提示词在运行时会被平台统一前置「团队共同红线」（禁止下单、禁止未来数据、禁止收益承诺）。
                      要修改请到「角色库」，改完这里会自动同步。
                    </>
                  ) : (
                    <>
                      自定义模板放在 <code className="inline">~/.quant_agent/prompts/</code> 会覆盖内置同名模板，无需改代码。
                      完整写法与更多场景模板见 <code className="inline">docs/Prompt提示词百科.md</code>。
                    </>
                  )}
                </div>
              </Panel>
            ) : (
              <Panel title="提示词详情">
                <EmptyState glyph="⌸" title="选择左侧一条提示词">左侧列出了全部任务模板与角色提示词。</EmptyState>
              </Panel>
            )}
          </div>
        </div>
      )}
    </>
  )
}
