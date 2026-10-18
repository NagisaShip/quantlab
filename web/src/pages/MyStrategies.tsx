import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ago,
  fmtPct,
  fmtNum,
  ORIGIN_LABEL,
  STATUS_LABEL,
  tone,
  v3,
  type BacktestRow,
  type CopilotResult,
  type LlmStatus,
  type SdkTemplate,
  type StrategyFile,
} from '../api/research'
import { CodeView } from '../components/CodeEditor'
import { Markdown } from '../components/markdown'
import { Callout, EmptyState, Heading, SplitButton, Loading, Modal, Panel, Segments, Tag } from '../components/ui'
import { ImportPackageModal } from '../components/StrategyPackage'
import { SharePublicButton } from '../components/PublicShare'
import { FreqtradeImportModal } from '../components/FreqtradeImport'
import { get, post } from '../http'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'
import { EditorStack, VisibilityChip } from '../components/Share'
import { collab, type EditorMark, type Visibility } from '../apiCollab'

const TIMEFRAMES = ['5m', '15m', '1h', '4h', '1d']
const EXAMPLES = [
  'BTC 1小时线，20 和 60 均线金叉买入、死叉卖出，止损 6%，仓位不超过 80%',
  'ETH 4小时 RSI 低于 25 分批买入，高于 70 卖出，仓位 60%',
  '在 BTC、ETH、SOL、BNB 之间做动量轮动，只持有趋势最强的一个，都走弱就空仓',
  'BTC 网格，每格 2%，单边 5 档，仓位上限 80%',
  '突破 55 根 4 小时线新高买入，跌破 20 根新低或 2 倍 ATR 止损离场',
]

export default function MyStrategiesPage({ onNavigate, notify, setBusy }: PageProps) {
  const [items, setItems] = useState<StrategyFile[] | null>(null)
  const [backtests, setBacktests] = useState<BacktestRow[]>([])
  const [filter, setFilter] = useState<'all' | 'draft' | 'backtested' | 'approved'>('all')
  const [q, setQ] = useState('')
  const [showNew, setShowNew] = useState(false)
  const [importing, setImporting] = useState(false)
  const [ftImport, setFtImport] = useState(false)
  const [showAi, setShowAi] = useState(false)
  const [llm, setLlm] = useState<LlmStatus | null>(null)
  const [error, setError] = useState('')
  const [legacy, setLegacy] = useState<{ id: string; name: string }[]>([])
  const [migrating, setMigrating] = useState(false)
  const [marks, setMarks] = useState<{ editors: Record<string, EditorMark[]>; visibility: Record<string, { visibility: Visibility; owner_name: string }> }>({ editors: {}, visibility: {} })

  const load = useCallback(async () => {
    try {
      collab.editors('strategy').then(setMarks).catch(() => undefined)
      const [s, b] = await Promise.all([v3.strategies(), v3.backtests(undefined, 300)])
      setItems(s.strategies.filter((x) => x.kind !== 'rule' || x.source !== 'builtin'))
      setBacktests(b.backtests)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    load()
    v3.llmStatus().then(setLlm).catch(() => setLlm(null))
    get<{ pending: { id: string; name: string }[] }>('/api/strategies-migration').then((r) => setLegacy(r.pending)).catch(() => undefined)
  }, [load])

  const migrate = async () => {
    setMigrating(true)
    try {
      const r = await post<{ results: { ok: boolean; name: string; reason?: string }[] }>('/api/strategies-migration')
      const bad = r.results.filter((x) => !x.ok)
      notify(bad.length ? `转换了 ${r.results.length - bad.length} 个，${bad.length} 个未转换：${bad.map((x) => `${x.name}（${x.reason}）`).join('、')}` : `已转换 ${r.results.length} 个策略`, bad.length > 0)
      setLegacy([])
      await load()
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setMigrating(false)
    }
  }

  const latest = useMemo(() => {
    const m = new Map<string, BacktestRow>()
    for (const b of backtests) if (b.slug && !m.has(b.slug)) m.set(b.slug, b)
    return m
  }, [backtests])
  const shown = (items ?? []).filter((s) => {
    if (filter === 'draft' && s.status !== 'draft') return false
    if (filter === 'backtested' && !latest.has(s.slug)) return false
    if (filter === 'approved' && !s.human_approved) return false
    if (q && !`${s.name} ${s.notes} ${s.securities.join(' ')}`.toLowerCase().includes(q.toLowerCase())) return false
    return true
  })

  const remove = async (s: StrategyFile) => {
    if (!await appConfirm(`删除策略「${s.name}」？代码目录与历史版本会一起删除，回测记录保留。`)) return
    try {
      await v3.deleteStrategy(s.slug)
      notify('已删除')
      load()
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const clone = async (s: StrategyFile) => {
    try {
      setBusy({ title: '正在克隆' })
      const r = await v3.cloneStrategy(s.slug)
      notify(`已克隆为「${r.strategy.name}」`)
      load()
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <Heading
        eyebrow="MY STRATEGIES"
        title="我的策略"
        description="一个策略就是一份 Python 文件：在编辑器里写、用 AI 生成，或从模板与策略广场克隆。每次保存都会留下版本快照；改动代码后需要重新批准，才能进入模拟交易。"
        actions={
          <SplitButton label={<><span>＋</span>新建策略</>} onClick={() => setShowNew(true)} items={[
            { label: '从模板新建', note: '选一个模板，改成自己的 Python 策略', onClick: () => setShowNew(true) },
            { label: 'AI 生成', note: '描述想法，AI 研究室写代码并回测', onClick: () => onNavigate('lab') },
            { label: '规则策略（不写代码）', note: '用条件和动作拼出来', onClick: () => onNavigate('dsl') },
            { label: '导入策略包', note: '.qstrat 文件，带代码和标准答案', onClick: () => setImporting(true) },
            { label: '导入 Freqtrade', note: '粘贴 IStrategy 代码，自动体检', onClick: () => setFtImport(true) },
          ]} />
        }
      />

      {importing && <ImportPackageModal notify={notify} onClose={() => setImporting(false)} onDone={(slug) => { setImporting(false); onNavigate(`editor/${slug}`) }} />}
      {ftImport && <FreqtradeImportModal notify={notify} onClose={() => setFtImport(false)} onDone={(slug) => { setFtImport(false); onNavigate(`editor/${slug}`) }} />}
      {legacy.length > 0 && (
        <div className="mig-banner">
          <span>还有 {legacy.length} 个旧版「统一赛道」策略没有转换成策略文件。转换后可以在这里编辑、回测和对比，原记录会保留。</span>
          <button className="button small primary" disabled={migrating} onClick={migrate}>{migrating ? '转换中…' : '全部转换'}</button>
        </div>
      )}

      <Panel
        title="策略列表"
        subtitle="点击名称进入编辑器；最近回测列为该策略最近一次完整回测的结果"
        actions={
          <>
            <input className="search" placeholder="搜索名称、标的、备注" value={q} onChange={(e) => setQ(e.target.value)} />
            <Segments
              value={filter}
              onChange={setFilter}
              options={[
                { value: 'all', label: '全部' },
                { value: 'draft', label: '草稿' },
                { value: 'backtested', label: '有回测' },
                { value: 'approved', label: '已批准' },
              ]}
            />
          </>
        }
        flush
      >
        {error && <div className="panel-body"><Callout tone="fail">{error}</Callout></div>}
        {!items ? (
          <Loading />
        ) : !shown.length ? (
          <EmptyState glyph="⌗" title={items.length ? '没有符合条件的策略' : '还没有策略'}
            action={!items.length && <button className="button primary" onClick={() => setShowNew(true)}>从模板新建</button>}>
            {!items.length && '从模板开始最快：选一个模板，改两行参数，就能跑出第一份回测。'}
          </EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>策略</th>
                  <th>编辑者</th>
                  <th>来源</th>
                  <th>标的 · 周期</th>
                  <th>状态</th>
                  <th className="right">最近回测收益</th>
                  <th className="right">基准</th>
                  <th className="right">最大回撤</th>
                  <th className="right">夏普</th>
                  <th>更新</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {shown.map((s) => {
                  const b = latest.get(s.slug)
                  return (
                    <tr key={s.slug}>
                      <td className="wrap">
                        <a className="link-strong" href={`#/editor/${s.slug}`}>{s.name}</a>
                        <small>{s.version} · {s.version_count} 个历史版本{s.notes ? ` · ${s.notes.slice(0, 40)}` : ''}</small>
                      </td>
                      <td>
                        <EditorStack editors={marks.editors[s.slug]} max={3} />
                        {marks.visibility[s.slug] && marks.visibility[s.slug].visibility !== 'workspace' && <VisibilityChip v={marks.visibility[s.slug].visibility} />}
                        {!marks.editors[s.slug] && !marks.visibility[s.slug] && <span className="muted">—</span>}
                      </td>
                      <td><Tag tone={s.source === 'agent' ? 'agent' : undefined}>{ORIGIN_LABEL[s.source] ?? s.source}</Tag></td>
                      <td>{s.securities.join(', ')}<small>{s.timeframe}</small></td>
                      <td>
                        <span className={`table-pill ${s.human_approved ? '' : 'pending'}`}>
                          {s.human_approved ? '已批准' : STATUS_LABEL[s.status] ?? s.status}
                        </span>
                      </td>
                      <td className={`right ${tone(b?.metrics.total_return)}`}>
                        {b ? <a href={`#/backtest/${b.id}`}>{fmtPct(b.metrics.total_return, 2, true)}</a> : <span className="muted">未回测</span>}
                      </td>
                      <td className="right">{b ? fmtPct(b.metrics.benchmark_return, 2, true) : ''}</td>
                      <td className="right">{b ? fmtPct(b.metrics.max_drawdown) : ''}</td>
                      <td className="right">{b ? fmtNum(b.metrics.sharpe) : ''}</td>
                      <td>{ago(s.updated_at / 1000)}</td>
                      <td>
                        <div className="row-actions">
                          <button className="button small" onClick={() => onNavigate(`editor/${s.slug}`)}>编辑</button>
                          <button className="button small" onClick={() => onNavigate(`backtests/${s.slug}`)}>回测</button>
                          <SharePublicButton type="strategy" id={s.slug} title={s.name} notify={notify} compact />
                          <button className="button small" onClick={() => clone(s)}>克隆</button>
                          <button className="button small danger" onClick={() => remove(s)}>删除</button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {showNew && <NewStrategyModal onClose={() => setShowNew(false)} onCreated={(slug) => onNavigate(`editor/${slug}`)} notify={notify} />}
      {showAi && <AiGenerateModal onClose={() => setShowAi(false)} onCreated={(slug) => onNavigate(`editor/${slug}`)} notify={notify} ready={Boolean(llm?.ready)} />}
    </>
  )
}

export function NewStrategyModal({ onClose, onCreated, notify }: { onClose: () => void; onCreated: (slug: string) => void; notify: PageProps['notify'] }) {
  const [templates, setTemplates] = useState<SdkTemplate[]>([])
  const [pick, setPick] = useState('ma_cross')
  const [name, setName] = useState('')
  const [securities, setSecurities] = useState('')
  const [timeframe, setTimeframe] = useState('')
  const [preview, setPreview] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    v3.templates().then((r) => setTemplates(r.templates)).catch((e) => notify(e.message, true))
  }, [notify])
  useEffect(() => {
    const t = templates.find((x) => x.id === pick)
    if (t) {
      setSecurities(t.securities.join(', '))
      setTimeframe(t.timeframe)
      if (!name || templates.some((x) => x.name === name)) setName(t.name)
    }
    v3.template(pick).then((r) => setPreview(r.template.code ?? '')).catch(() => setPreview(''))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pick, templates])

  const create = async () => {
    setSaving(true)
    try {
      const r = await v3.newStrategy({
        name: name.trim() || '新策略',
        template_id: pick,
        securities: securities.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
        timeframe,
      })
      notify('已创建，进入编辑器')
      onCreated(r.strategy.slug)
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal title="新建策略" onClose={onClose} width={1040}
      footer={<><button className="button" onClick={onClose}>取消</button><button className="button primary" disabled={saving} onClick={create}>{saving ? '创建中…' : '创建并打开编辑器'}</button></>}>
      <div className="template-layout">
        <div>
          <div className="template-grid">
            {templates.map((t) => (
              <button key={t.id} className={`template-card${pick === t.id ? ' selected' : ''}`} onClick={() => setPick(t.id)}>
                <div className="template-top"><b>{t.name}</b><Tag>{t.category}</Tag></div>
                <p>{t.description}</p>
                <div className="card-tags">
                  <Tag mono>{t.timeframe}</Tag>
                  {t.securities.length > 1 && <Tag>{t.securities.length} 个标的</Tag>}
                  <Tag tone={t.difficulty === '入门' ? 'ok' : 'warn'}>{t.difficulty}</Tag>
                </div>
              </button>
            ))}
          </div>
          <div className="form-grid" style={{ marginTop: 16 }}>
            <label>策略名称<input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} /></label>
            <label>周期
              <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                {TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}
              </select>
            </label>
          </div>
          <label style={{ marginTop: 12 }}>标的池（逗号分隔）<input value={securities} onChange={(e) => setSecurities(e.target.value)} /></label>
        </div>
        <div className="template-preview">
          <div className="hint" style={{ marginBottom: 8 }}>模板代码预览</div>
          <CodeView code={preview || '# 加载中…'} maxHeight={470} />
        </div>
      </div>
    </Modal>
  )
}

export function AiGenerateModal({ onClose, onCreated, notify, ready }: { onClose: () => void; onCreated: (slug: string) => void; notify: PageProps['notify']; ready: boolean }) {
  const [text, setText] = useState('')
  const [securities, setSecurities] = useState('')
  const [timeframe, setTimeframe] = useState('')
  const [result, setResult] = useState<CopilotResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')

  const run = async () => {
    if (text.trim().length < 2) return
    setBusy(true)
    setResult(null)
    try {
      const r = await v3.aiGenerate({
        description: text,
        securities: securities.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
        timeframe: timeframe || undefined,
      })
      setResult(r)
      setName(r.name || 'AI 策略')
    } catch (e) {
      notify((e as Error).message, true)
    } finally {
      setBusy(false)
    }
  }

  const save = async () => {
    if (!result?.code) return
    try {
      const r = await v3.newStrategy({
        name: name || 'AI 策略',
        template_id: null,
        code: result.code,
        securities: result.securities,
        timeframe: result.timeframe,
        origin: 'agent',
        notes: `AI 生成（${result.mode === 'llm' ? result.model : '模板降级'}）：${text.slice(0, 120)}`,
      })
      notify('已保存为草稿策略')
      onCreated(r.strategy.slug)
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  return (
    <Modal title="✦ 用自然语言生成策略" onClose={onClose} width={980}
      footer={
        <>
          <button className="button" onClick={onClose}>关闭</button>
          {result?.code && <button className="button primary" onClick={save}>保存为草稿并打开编辑器</button>}
        </>
      }>
      {!ready && (
        <Callout tone="warn" title="当前未连接模型">
          将按关键词匹配模板并填入描述中的数字（周期、均线、止损、仓位等）。在「AI 模型」里填入任意服务商密钥后，可以生成任意逻辑的代码。
        </Callout>
      )}
      <label>描述你的交易想法
        <textarea rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder="标的、周期、入场条件、离场条件、风控约束……越具体越好" style={{ fontFamily: 'var(--sans)' }} />
      </label>
      <div className="chip-row">
        {EXAMPLES.map((e) => <button key={e} className="chip" onClick={() => setText(e)}>{e.slice(0, 22)}…</button>)}
      </div>
      <div className="form-grid" style={{ marginTop: 12 }}>
        <label>标的池（可选，留空则从描述中识别）<input value={securities} onChange={(e) => setSecurities(e.target.value)} placeholder="BTC/USDT, ETH/USDT" /></label>
        <label>周期（可选）
          <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
            <option value="">从描述中识别</option>
            {TIMEFRAMES.map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
      </div>
      <div className="button-row" style={{ marginTop: 14 }}>
        <button className="button primary" disabled={busy || text.trim().length < 2} onClick={run}>
          {busy ? <><span className="spinner" />生成中（含自检与试跑）…</> : '生成策略'}
        </button>
      </div>
      {result && (
        <div className="ai-result">
          <div className="ai-result-head">
            <Tag tone={result.mode === 'llm' ? 'agent' : 'warn'}>{result.mode === 'llm' ? `模型 ${result.model}` : '模板降级'}</Tag>
            <Tag tone={result.checks?.ok ? 'ok' : 'fail'}>{result.checks?.ok ? `自检通过 · 试跑成交 ${result.checks.fills ?? 0} 笔` : '自检未通过'}</Tag>
            {result.attempts > 1 && <Tag>自动修复 {result.attempts - 1} 次</Tag>}
            <Tag mono>{result.securities.join(', ')} · {result.timeframe}</Tag>
          </div>
          {!result.checks?.ok && (result.checks?.issues?.length || result.checks?.errors?.length) ? (
            <Callout tone="fail" title="检查问题">{[...(result.checks.issues ?? []), ...(result.checks.errors ?? [])].join('；')}</Callout>
          ) : null}
          <div className="two-columns even" style={{ marginTop: 12 }}>
            <div className="ai-text"><Markdown text={result.content} /></div>
            <div>
              <label style={{ marginBottom: 8 }}>保存名称<input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} /></label>
              <CodeView code={result.code || '# 没有生成代码'} maxHeight={420} />
            </div>
          </div>
        </div>
      )}
    </Modal>
  )
}
