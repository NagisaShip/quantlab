import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { demo } from '../apiDemo'
import { SpecModal } from '../components/StrategyPackage'
import {
  ago,
  downloadText,
  fmtDate,
  fmtMoney,
  fmtNum,
  fmtPct,
  fmtTime,
  ORIGIN_LABEL,
  STATUS_LABEL,
  tone,
  v3,
  type BacktestResult,
  type BacktestRow,
  type ChatTurn,
  type CopilotResult,
  type LlmStatus,
  type Params,
  type Spec,
  type StrategyFile,
} from '../api2'
import { CodeEditor, CodeView, errorLinesFrom } from '../components/CodeEditor'
import { COLORS, LineChart } from '../components/charts2'
import { DiffView } from '../components/Diff'
import { Markdown } from '../components/markdown'
import { Callout, EmptyState, Loading, Modal, Segments, Tag } from '../components/ui'
import { openInLab } from '../components/handoff'
import { useAskAIContext } from '../components/AskAI'
import { v4, type DslProblem } from '../api3'
import { ruleCode, type RuleFromCode } from '../apiRuleCode'
import { makeErrAt, RuleForm, type RuleSpec } from './rulecode/RuleForm'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

type Tab = 'result' | 'logs' | 'ai' | 'params' | 'versions' | 'history'
const DAY = 86_400_000
const TF_MS: Record<string, number> = { '1m': 60_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000, '4h': 14_400_000, '1d': DAY }

const toInputDate = (ms: number) => new Date(ms).toISOString().slice(0, 10)
const fromInputDate = (s: string) => (s ? Date.parse(`${s}T00:00:00Z`) : NaN)

interface RunSettings {
  start: string
  end: string
  timeframe: string
  securities: string
  initial_cash: number
  source: Spec['source']
  exchange: Spec['exchange']
  fee_pct: number
  slippage_bps: number
  spread_bps: number
  fill_at: Spec['fill_at']
  warmup_bars: number
  benchmark: string
  rebalance_pct: number
}

function defaultSettings(s: StrategyFile): RunSettings {
  const now = Date.now()
  const days = s.timeframe === '1d' ? 730 : s.timeframe === '4h' ? 240 : s.timeframe === '1h' ? 90 : 20
  return {
    start: toInputDate(now - days * DAY),
    end: toInputDate(now),
    timeframe: s.timeframe,
    securities: s.securities.join(', '),
    initial_cash: 10_000,
    source: 'auto',
    exchange: 'binance',
    fee_pct: 0.1,
    slippage_bps: 5,
    spread_bps: 2,
    fill_at: 'next_open',
    warmup_bars: 100,
    benchmark: '',
    rebalance_pct: 0.5,
  }
}

const SETTINGS_KEY = (slug: string) => `qa.run.${slug}`

export default function EditorPage({ slug, onNavigate, notify, setBusy }: PageProps & { slug: string }) {
  const [strategy, setStrategy] = useState<StrategyFile | null>(null)
  const [code, setCode] = useState('')
  const [saved, setSaved] = useState('')
  const [name, setName] = useState('')
  const [specOpen, setSpecOpen] = useState(false)
  const [issues, setIssues] = useState<string[]>([])
  const [versions, setVersions] = useState<{ version: string; file: string; saved_at: number; lines: number }[]>([])
  const [settings, setSettings] = useState<RunSettings | null>(null)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [tab, setTab] = useState<Tab>('result')
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [running, setRunning] = useState<'' | 'quick' | 'full'>('')
  const [runError, setRunError] = useState('')
  const [overrides, setOverrides] = useState<Params>({})
  const [history, setHistory] = useState<BacktestRow[]>([])
  const [llm, setLlm] = useState<LlmStatus | null>(null)
  const [error, setError] = useState('')
  // 规则 / 代码 切换：代码顶部有 @rule 注释且 AST 与规则生成的代码一致时，可以按规则编辑
  const [mode, setMode] = useState<'rule' | 'code'>('code')
  const [check, setCheck] = useState<RuleFromCode | null>(null)
  const [ruleSpec, setRuleSpec] = useState<RuleSpec | null>(null)
  const [ruleProblems, setRuleProblems] = useState<DslProblem[]>([])
  const [indicatorTypes, setIndicatorTypes] = useState<{ type: string; label: string }[]>([])
  const generated = useRef('') // 最近一次由规则生成的代码：识别回来时不覆盖正在编辑的规则
  const modeDecided = useRef(false)
  const checkTimer = useRef<number>()
  const genTimer = useRef<number>()
  useAskAIContext(strategy ? { kind: 'strategy', title: `策略「${name || strategy.slug}」的代码`, text: '', code } : null)
  const dirty = code !== saved

  const load = useCallback(async () => {
    try {
      const r = await v3.strategy(slug)
      if (r.strategy.kind === 'dsl') {
        // 规则 DSL 策略用结构化编辑器（表单 / JSON 双视图）
        onNavigate(`dsl/${slug}`)
        return
      }
      setStrategy(r.strategy)
      modeDecided.current = false
      setCode(r.strategy.code ?? '')
      setSaved(r.strategy.code ?? '')
      setName(r.strategy.name)
      setIssues(r.issues)
      setVersions(r.versions)
      let st = defaultSettings(r.strategy)
      try {
        const raw = localStorage.getItem(SETTINGS_KEY(slug))
        if (raw) st = { ...st, ...JSON.parse(raw) }
      } catch {
        /* 本地存储不可用时用默认值 */
      }
      setSettings(st)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [slug])

  const loadHistory = useCallback(() => {
    v3.backtests(slug, 50).then((r) => setHistory(r.backtests)).catch(() => undefined)
  }, [slug])

  useEffect(() => {
    load()
    loadHistory()
    v3.llmStatus().then(setLlm).catch(() => undefined)
  }, [load, loadHistory])

  useEffect(() => {
    v4.dslSchema().then((s) => setIndicatorTypes(s.indicators)).catch(() => undefined)
  }, [])

  // 代码一变就（防抖）重新做 AST 检查：是否仍恰好是 @rule 描述的规则
  useEffect(() => {
    if (!strategy) return
    window.clearTimeout(checkTimer.current)
    checkTimer.current = window.setTimeout(() => {
      ruleCode.fromCode(code).then((r) => {
        setCheck(r)
        const fromRule = code === generated.current
        if (r.is_rule && r.spec && !fromRule) setRuleSpec(r.spec)
        if (!modeDecided.current) {
          // 打开策略时：是规则就默认进规则视图
          modeDecided.current = true
          if (r.is_rule && r.spec) setMode('rule')
        }
      }).catch(() => undefined)
    }, code === generated.current ? 0 : 400)
    return () => window.clearTimeout(checkTimer.current)
  }, [code, strategy])

  // 改规则 → （防抖）重新生成代码；规则不合法时代码保持上一版
  const editRule = (next: RuleSpec) => {
    setRuleSpec(next)
    window.clearTimeout(genTimer.current)
    genTimer.current = window.setTimeout(() => {
      ruleCode.toCode({ ...next, name: name.trim() || next.name }).then((r) => {
        setRuleProblems(r.problems)
        if (r.ok && r.code) {
          generated.current = r.code
          setCode(r.code)
        }
      }).catch((e) => notify((e as Error).message, true))
    }, 300)
  }

  const switchMode = (m: 'rule' | 'code') => {
    if (m === mode) return
    if (m === 'rule') {
      if (!check) { notify('正在检查代码，请稍候', true); return }
      if (!check.is_rule || !check.spec) { notify(check.reason, true); return }
      setRuleSpec(check.spec)
      setRuleProblems([])
    }
    setMode(m)
  }

  useEffect(() => {
    if (!settings) return
    try {
      localStorage.setItem(SETTINGS_KEY(slug), JSON.stringify(settings))
    } catch {
      /* 忽略 */
    }
  }, [settings, slug])

  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault()
        e.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])

  const declared = strategy?.params ?? {}

  const save = useCallback(async (): Promise<StrategyFile | null> => {
    if (!strategy) return null
    try {
      const r = await v3.saveStrategy({
        slug: strategy.slug,
        name: name.trim() || strategy.name,
        code,
        params: strategy.params,
        team: strategy.team,
        kind: strategy.kind === 'rule' ? 'code' : strategy.kind,
        source: strategy.source,
        symbol: (settings?.securities.split(/[,，\s]+/)[0] || strategy.symbol).toUpperCase(),
        timeframe: settings?.timeframe || strategy.timeframe,
        securities: (settings?.securities ?? strategy.securities.join(',')).split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
        notes: strategy.notes,
      })
      setStrategy(r.strategy)
      setSaved(code)
      setIssues(r.issues)
      if (r.strategy.version !== strategy.version) {
        notify(r.issues.length ? `已保存（${r.strategy.version}），但有 ${r.issues.length} 个检查问题` : `已保存为新版本 ${r.strategy.version}${strategy.human_approved ? '；代码已变更，需要重新批准' : ''}`, Boolean(r.issues.length))
        v3.strategy(slug).then((x) => setVersions(x.versions)).catch(() => undefined)
      } else {
        notify('已保存（内容未变化）')
      }
      return r.strategy
    } catch (e) {
      notify((e as Error).message, true)
      return null
    }
  }, [strategy, name, code, settings, notify, slug])

  const spec = (): Partial<Spec> | null => {
    if (!settings) return null
    const start = fromInputDate(settings.start)
    const end = fromInputDate(settings.end) + DAY
    if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end) {
      notify('请检查回测起止日期', true)
      return null
    }
    const cleanOverrides: Params = {}
    for (const [k, v] of Object.entries(overrides)) if (v !== '' && v !== undefined && k in declared) cleanOverrides[k] = v
    return {
      securities: settings.securities.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
      timeframe: settings.timeframe,
      start,
      end: Math.min(end, Date.now()),
      initial_cash: settings.initial_cash,
      source: settings.source,
      exchange: settings.exchange,
      taker_fee: settings.fee_pct / 100,
      slippage_bps: settings.slippage_bps,
      spread_bps: settings.spread_bps,
      fill_at: settings.fill_at,
      warmup_bars: settings.warmup_bars,
      benchmark: settings.benchmark.trim().toUpperCase() || null,
      rebalance_tolerance: settings.rebalance_pct / 100,
      params: cleanOverrides,
    }
  }

  const estimatedBars = useMemo(() => {
    if (!settings) return 0
    const s = fromInputDate(settings.start)
    const e = fromInputDate(settings.end) + DAY
    return Math.max(0, Math.round((e - s) / (TF_MS[settings.timeframe] ?? 3_600_000)))
  }, [settings])

  const quickRun = useCallback(async () => {
    const sp = spec()
    if (!sp || !strategy) return
    setRunning('quick')
    setRunError('')
    setTab('result')
    try {
      const r = await v3.runBacktest({ ...sp, slug: strategy.slug, code, persist: false })
      setResult(r.result)
      if (r.result.errors.length) setTab(r.result.fills.length ? 'result' : 'logs')
    } catch (e) {
      setRunError((e as Error).message)
      setResult(null)
    } finally {
      setRunning('')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategy, code, settings, overrides])

  const fullRun = async () => {
    const sp = spec()
    if (!sp || !strategy) return
    if (dirty) {
      const ok = await save()
      if (!ok) return
    }
    setRunning('full')
    setBusy({ title: '正在运行回测', note: '取数 → 预热 → 逐 bar 运行策略 → 撮合 → 计算指标' })
    try {
      const r = await v3.runBacktest({ ...sp, slug: strategy.slug, persist: true })
      notify('回测完成')
      loadHistory()
      if (r.id) onNavigate(`backtest/${r.id}`)
    } catch (e) {
      setRunError((e as Error).message)
      notify((e as Error).message, true)
    } finally {
      setRunning('')
      setBusy(null)
    }
  }

  const approve = async () => {
    if (!strategy) return
    if (dirty) {
      notify('请先保存，批准针对的是已保存的版本', true)
      return
    }
    if (!await appConfirm(`批准版本 ${strategy.version}？\n批准后此版本可以进入模拟交易；之后任何代码或参数改动都会让批准失效。`)) return
    try {
      const r = await v3.approveStrategy(strategy.slug)
      setStrategy({ ...strategy, ...r.strategy })
      notify('已批准，可以在「模拟交易」里启动')
    } catch (e) {
      notify((e as Error).message, true)
    }
  }

  const allErrors = [...(result?.errors ?? []), ...(runError ? [runError] : []), ...issues]
  const errLines = errorLinesFrom(allErrors)

  if (error) return <Callout tone="fail" title="无法打开策略">{error}</Callout>
  if (!strategy || !settings) return <Loading label="正在打开策略" />

  const set = <K extends keyof RunSettings>(k: K, v: RunSettings[K]) => setSettings({ ...settings, [k]: v })

  return (
    <div className="editor-page">
      <div className="editor-toolbar">
        <button className="button small" onClick={() => onNavigate('strategies')}>← 我的策略</button>
        <input className="title-input" value={name} onChange={(e) => setName(e.target.value)} aria-label="策略名称" />
        <Tag mono>{strategy.version}</Tag>
        <span className={`table-pill ${strategy.human_approved ? '' : 'pending'}`}>
          {strategy.human_approved ? '已批准' : STATUS_LABEL[strategy.status] ?? strategy.status}
        </span>
        <Tag tone={strategy.source === 'agent' ? 'agent' : undefined}>{ORIGIN_LABEL[strategy.source] ?? strategy.source}</Tag>
        {check && <Tag>{check.is_rule ? '规则策略' : '代码策略'}</Tag>}
        {dirty && <span className="dirty-dot" title="有未保存的修改">● 未保存</span>}
        <div className="spacer" />
        <button className="button small" onClick={save} title="Ctrl+S">保存</button>
        <button className="button small" disabled={Boolean(running)} onClick={quickRun} title="Ctrl+Enter：用当前编辑器里的代码快速跑一遍，不进回测列表">
          {running === 'quick' ? <><span className="spinner" />运行中</> : '▷ 编译运行'}
        </button>
        <button className="button small primary" disabled={Boolean(running)} onClick={fullRun}>运行回测</button>
        <button className="button small" title="把当前代码带进 AI 研究室，对话式改进并自动回测"
          onClick={() => openInLab({
            idea: '在现有代码基础上改进这个策略：', title: name, code,
            securities: settings.securities.split(/[,，\s]+/).map((x) => x.trim().toUpperCase()).filter(Boolean),
            timeframe: settings.timeframe, attached: `「${name}」的当前代码`,
          })}>用 AI 改进</button>
        <button className="button small" onClick={() => onNavigate(`optimize/${strategy.slug}`)} disabled={!Object.keys(declared).length} title={Object.keys(declared).length ? '' : '策略需要声明 PARAMS 才能做参数优化'}>参数优化</button>
        <button className="button small" onClick={approve} disabled={strategy.human_approved && !dirty}>
          {strategy.human_approved && !dirty ? '已批准' : '批准版本'}
        </button>
        <MoreMenu
          items={[
            { label: '导出策略包 .qstrat', onClick: () => { window.location.href = demo.packageUrl(strategy.slug) } },
            { label: '编辑说明书', onClick: () => setSpecOpen(true) },
            ...(strategy.source === 'freqtrade' ? [{ label: '导出到 Freqtrade（参数写回）', onClick: () => { window.location.href = `/api/freqtrade/export/${encodeURIComponent(strategy.slug)}` } }] : []),
            { label: '导出 .py', onClick: () => window.open(v3.exportUrl(strategy.slug), '_blank') },
            { label: '下载当前代码', onClick: () => downloadText(`${strategy.slug}.py`, code, 'text/x-python;charset=utf-8') },
            { label: '克隆为新策略', onClick: async () => { const r = await v3.cloneStrategy(strategy.slug); onNavigate(`editor/${r.strategy.slug}`) } },
            { label: '去模拟交易', onClick: () => onNavigate('paper') },
          ]}
        />
        {specOpen && <SpecModal slug={strategy.slug} notify={notify} onClose={() => setSpecOpen(false)} />}
      </div>

      <div className="run-settings">
        <label>开始<input type="date" value={settings.start} onChange={(e) => set('start', e.target.value)} /></label>
        <label>结束<input type="date" value={settings.end} onChange={(e) => set('end', e.target.value)} /></label>
        <label>周期
          <select value={settings.timeframe} onChange={(e) => set('timeframe', e.target.value)}>
            {['1m', '5m', '15m', '1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}
          </select>
        </label>
        <label className="wide">标的池<input value={settings.securities} onChange={(e) => set('securities', e.target.value)} /></label>
        <label>初始资金<input type="number" min={100} step={1000} value={settings.initial_cash} onChange={(e) => set('initial_cash', Number(e.target.value))} /></label>
        <label>行情来源
          <select value={settings.source} onChange={(e) => set('source', e.target.value as Spec['source'])}>
            <option value="auto">真实行情（缓存优先）</option>
            <option value="exchange">只用真实行情</option>
          </select>
        </label>
        <button className="button small ghost" onClick={() => setShowAdvanced(!showAdvanced)}>{showAdvanced ? '收起' : '成本与高级'}</button>
        {showAdvanced && (
          <div className="run-advanced">
            <label>交易所<select value={settings.exchange} onChange={(e) => set('exchange', e.target.value as Spec['exchange'])}><option value="binance">Binance</option><option value="okx">OKX</option></select></label>
            <label>手续费 %<input type="number" step={0.01} min={0} value={settings.fee_pct} onChange={(e) => set('fee_pct', Number(e.target.value))} /></label>
            <label>滑点 bps<input type="number" step={1} min={0} value={settings.slippage_bps} onChange={(e) => set('slippage_bps', Number(e.target.value))} /></label>
            <label>价差 bps<input type="number" step={1} min={0} value={settings.spread_bps} onChange={(e) => set('spread_bps', Number(e.target.value))} /></label>
            <label>调仓容差 %<input type="number" step={0.1} min={0} value={settings.rebalance_pct} onChange={(e) => set('rebalance_pct', Number(e.target.value))} /></label>
            <label>预热 bar<input type="number" step={10} min={0} value={settings.warmup_bars} onChange={(e) => set('warmup_bars', Number(e.target.value))} /></label>
            <label>成交时点
              <select value={settings.fill_at} onChange={(e) => set('fill_at', e.target.value as Spec['fill_at'])}>
                <option value="next_open">下一根开盘（推荐）</option>
                <option value="close">当根收盘（乐观）</option>
              </select>
            </label>
            <label>基准（留空=标的池第一个）<input value={settings.benchmark} onChange={(e) => set('benchmark', e.target.value)} placeholder="BTC/USDT" /></label>
          </div>
        )}
        <div className="run-hint">约 {estimatedBars.toLocaleString()} 根 {settings.timeframe} bar（另加 {settings.warmup_bars} 根预热）{estimatedBars > 20000 ? ' · 超过 2 万根，请缩短区间或换更大周期' : ''}</div>
      </div>

      <div className="editor-split">
        <div className="editor-left">
          <div className="rc-bar">
            <Segments value={mode} onChange={switchMode} ariaLabel="编辑方式"
              options={[{ value: 'rule', label: '规则' }, { value: 'code', label: '代码' }]} />
            <RuleStatus check={check} />
          </div>
          {mode === 'rule' && ruleSpec ? (
            <div className="rc-rule">
              <RuleForm spec={ruleSpec} onChange={editRule} errAt={makeErrAt(ruleProblems)} indicatorTypes={indicatorTypes} />
              {ruleProblems.length > 0 && (
                <Callout tone="fail" title="规则未通过校验，代码保持上一版">
                  <ul>{ruleProblems.map((p, i) => <li key={i}><code>{p.path}</code> {p.message}</li>)}</ul>
                </Callout>
              )}
              {check?.is_rule && check.summary && (
                <ol className="rc-summary">{check.summary.map((x) => <li key={x}>{x}</li>)}</ol>
              )}
            </div>
          ) : (
            <CodeEditor value={code} onChange={setCode} onSave={save} onRun={quickRun} errorLines={errLines} minHeight={560} />
          )}
          {issues.length > 0 && (
            <Callout tone="fail" title="沙箱检查">
              <ul>{issues.map((i) => <li key={i}>{i}</li>)}</ul>
            </Callout>
          )}
          <div className="editor-foot">
            <span>Ctrl+S 保存 · Ctrl+Enter 编译运行 · Ctrl+/ 注释 · Tab 缩进</span>
            <a href="#/docs">API 文档</a>
          </div>
        </div>

        <div className="editor-right">
          <div className="tabs">
            {([
              ['result', '运行结果'],
              ['logs', `日志${result ? ` (${result.logs_total}${result.errors.length ? ` · ${result.errors.length} 错误` : ''})` : ''}`],
              ['ai', `AI 助手${llm && !llm.ready ? '·降级' : ''}`],
              ['params', `参数 (${Object.keys(declared).length})`],
              ['versions', `版本 (${versions.length})`],
              ['history', `回测 (${history.length})`],
            ] as [Tab, string][]).map(([k, label]) => (
              <button key={k} aria-pressed={tab === k} onClick={() => setTab(k)}>{label}</button>
            ))}
          </div>
          <div className="tab-body">
            {tab === 'result' && <ResultTab result={result} running={running} error={runError} onFull={fullRun} onFix={() => setTab('ai')} />}
            {tab === 'logs' && <LogsTab result={result} runError={runError} />}
            {tab === 'ai' && (
              <AiTab
                code={code}
                setCode={setCode}
                strategy={strategy}
                settings={settings}
                errors={allErrors}
                logs={result?.logs ?? []}
                ready={Boolean(llm?.ready)}
                notify={notify}
              />
            )}
            {tab === 'params' && (
              <ParamsTab
                declared={declared}
                overrides={overrides}
                setOverrides={setOverrides}
                dirty={dirty}
                onApply={async (p) => {
                  try {
                    const r = await v3.applyParams(strategy.slug, p)
                    setStrategy(r.strategy)
                    setCode(r.strategy.code ?? code)
                    setSaved(r.strategy.code ?? code)
                    setOverrides({})
                    notify(`参数已写回代码，生成新版本 ${r.strategy.version}`)
                  } catch (e) {
                    notify((e as Error).message, true)
                  }
                }}
              />
            )}
            {tab === 'versions' && <VersionsTab slug={strategy.slug} versions={versions} current={code} onRestore={(c) => { setCode(c); notify('已载入历史版本到编辑器，保存后生效') }} />}
            {tab === 'history' && <HistoryTab rows={history} onOpen={(id) => onNavigate(`backtest/${id}`)} />}
          </div>
        </div>
      </div>
    </div>
  )
}

/* 代码是否仍是规则：一行安静的状态说明 */
function RuleStatus({ check }: { check: RuleFromCode | null }) {
  if (!check) return null
  if (check.is_rule) {
    return <span className="rc-status">与规则一致{check.warnings.length ? ` · ${check.warnings[0]}` : ''}</span>
  }
  if (/没有 @rule/.test(check.reason)) return <span className="rc-status">代码策略</span>
  return (
    <span className="rc-status warn" title={check.diff || undefined}>
      {check.reason}{check.diff ? `（${check.diff}）` : ''}
    </span>
  )
}

function MoreMenu({ items }: { items: { label: string; onClick: () => void }[] }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [])
  return (
    <div className="menu" ref={ref}>
      <button className="button small" onClick={() => setOpen(!open)} aria-expanded={open}>更多 ▾</button>
      {open && (
        <div className="menu-list">
          {items.map((it) => <button key={it.label} onClick={() => { setOpen(false); it.onClick() }}>{it.label}</button>)}
        </div>
      )}
    </div>
  )
}

/* ---------------- 运行结果 ---------------- */
export function cumulative(curve: number[]) {
  const base = curve[0] || 1
  return curve.map((v) => v / base - 1)
}

function ResultTab({ result, running, error, onFull, onFix }: { result: BacktestResult | null; running: string; error: string; onFull: () => void; onFix: () => void }) {
  if (running === 'quick') return <Loading label="编译运行中" />
  if (error) return (
    <>
      <Callout tone="fail" title="运行失败">{error}</Callout>
      <button className="button small" onClick={onFix}>让 AI 帮我修复</button>
    </>
  )
  if (!result) return (
    <EmptyState glyph="▷" title="还没有运行">
      「编译运行」用编辑器里的代码快速跑一遍，结果只显示在这里；「运行回测」会先保存，再生成一份完整的回测记录与详情页。
    </EmptyState>
  )
  const m = result.metrics
  const strat = cumulative(result.equity_curve)
  const bench = cumulative(result.benchmark_curve)
  return (
    <div className="result-tab">
      {result.warnings.map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
      {result.errors.length > 0 && (
        <Callout tone="fail" title={`${result.errors.length} 个运行错误`}>
          {result.errors.slice(0, 3).join('；')}　<button className="link" onClick={onFix}>让 AI 修复</button>
        </Callout>
      )}
      <div className="mini-metrics">
        <div><span>策略收益</span><b className={tone(m.total_return)}>{fmtPct(m.total_return, 2, true)}</b></div>
        <div><span>基准收益</span><b>{fmtPct(m.benchmark_return, 2, true)}</b></div>
        <div><span>超额收益</span><b className={tone(m.excess_return)}>{fmtPct(m.excess_return, 2, true)}</b></div>
        <div><span>最大回撤</span><b>{fmtPct(m.max_drawdown)}</b></div>
        <div><span>夏普</span><b>{fmtNum(m.sharpe)}</b></div>
        <div><span>交易 / 成交</span><b>{m.trades} / {m.fills}</b></div>
      </div>
      <LineChart
        timestamps={result.timestamps}
        series={[
          { name: '策略收益', values: strat, color: COLORS.strategy, area: true },
          { name: `基准 ${result.benchmark}`, values: bench, color: COLORS.benchmark, dashed: true },
        ]}
        height={260}
        width={560}
        ariaLabel="策略与基准累计收益"
      />
      <div className="result-foot">
        <span>{result.datasets.map((d) => `${d.symbol} ${d.source_label}`).join(' · ')} · {fmtDate(result.timestamps[0])} → {fmtDate(result.timestamps[result.timestamps.length - 1])} · 用时 {result.duration_ms} ms</span>
        <button className="button small primary" onClick={onFull}>保存为完整回测 →</button>
      </div>
    </div>
  )
}

function LogsTab({ result, runError }: { result: BacktestResult | null; runError: string }) {
  const [level, setLevel] = useState('all')
  if (!result && !runError) return <EmptyState glyph="≡" title="运行后这里显示日志">策略里用 log.info(...) 输出的内容、委托未成交提示、运行错误都会出现在这里。</EmptyState>
  const logs = (result?.logs ?? []).filter((l) => level === 'all' || l.level === level)
  return (
    <div className="logs">
      {runError && <div className="log-line error"><span>—</span><b>ERROR</b>{runError}</div>}
      {(result?.errors ?? []).map((e, i) => <div key={'e' + i} className="log-line error"><span>—</span><b>ERROR</b>{e}</div>)}
      <div className="log-filter">
        {['all', 'info', 'warning', 'error', 'debug'].map((l) => (
          <button key={l} className={`chip${level === l ? ' on' : ''}`} onClick={() => setLevel(l)}>{l === 'all' ? '全部' : l}</button>
        ))}
        {result && result.logs_total > result.logs.length && <span className="hint">仅显示前 {result.logs.length} / {result.logs_total} 条</span>}
      </div>
      {logs.map((l, i) => (
        <div key={i} className={`log-line ${l.level}`}><span>{fmtTime(l.ts)}</span><b>{l.level.toUpperCase()}</b>{l.message}</div>
      ))}
      {!logs.length && <div className="hint">没有该级别的日志</div>}
    </div>
  )
}

/* ---------------- AI 助手 ---------------- */
interface AiMsg { role: 'user' | 'assistant'; content: string; result?: CopilotResult; applied?: boolean; base?: string }

function AiTab({ code, setCode, strategy, settings, errors, logs, ready, notify }: {
  code: string
  setCode: (c: string) => void
  strategy: StrategyFile
  settings: RunSettings
  errors: string[]
  logs: { level: string; message: string }[]
  ready: boolean
  notify: PageProps['notify']
}) {
  const [msgs, setMsgs] = useState<AiMsg[]>([])
  const [input, setInput] = useState('')
  const [mode, setMode] = useState<'edit' | 'chat' | 'generate'>('edit')
  const [busy, setBusy] = useState(false)
  const end = useRef<HTMLDivElement>(null)
  const securities = settings.securities.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean)

  useEffect(() => { end.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }) }, [msgs, busy])

  const history = (): ChatTurn[] => msgs.slice(-8).map((m) => ({ role: m.role, content: m.content.slice(0, 3000) }))

  const ask = async (kind: 'edit' | 'chat' | 'generate' | 'explain' | 'fix', text: string) => {
    setBusy(true)
    const base = code
    setMsgs((m) => [...m, { role: 'user', content: text }])
    try {
      let r: CopilotResult
      if (kind === 'explain') r = await v3.aiExplain(code)
      else if (kind === 'fix') {
        const errs = [...errors, ...logs.filter((l) => l.level === 'error' || l.level === 'warning').map((l) => l.message)].slice(0, 12)
        r = await v3.aiFix({ code, errors: errs, securities, timeframe: settings.timeframe })
      } else if (kind === 'generate') r = await v3.aiGenerate({ description: text, securities, timeframe: settings.timeframe, history: history() })
      else if (kind === 'edit') r = await v3.aiEdit({ code, instruction: text, securities, timeframe: settings.timeframe, history: history() })
      else r = await v3.aiChat({ question: text, code, history: history() })
      setMsgs((m) => [...m, { role: 'assistant', content: r.content, result: r, base }])
    } catch (e) {
      setMsgs((m) => [...m, { role: 'assistant', content: `请求失败：${(e as Error).message}` }])
    } finally {
      setBusy(false)
    }
  }

  const send = () => {
    const t = input.trim()
    if (!t || busy) return
    setInput('')
    ask(mode, t)
  }

  const apply = async (i: number) => {
    const m = msgs[i]
    if (!m.result?.code) return
    if (m.base !== undefined && m.base !== code && !await appConfirm('编辑器里的代码在这条回复之后又改过，应用会覆盖这些改动。继续？')) return
    setCode(m.result.code)
    setMsgs((all) => all.map((x, k) => (k === i ? { ...x, applied: true } : x)))
    notify('已应用到编辑器（尚未保存）')
  }

  return (
    <div className="ai-tab">
      {!ready && (
        <Callout tone="warn" title="未连接模型">
          「解释」「修复」会用规则分析；「生成」按关键词匹配模板；自由修改与问答需要先在 <a href="#/models">AI 模型</a> 中填写密钥。
        </Callout>
      )}
      <div className="ai-quick">
        <button className="chip" disabled={busy} onClick={() => ask('explain', '解释这份策略')}>解释策略</button>
        <button className="chip" disabled={busy} onClick={() => ask('fix', `修复运行错误（${errors.length} 个）`)}>修复报错</button>
        <button className="chip" disabled={busy || !ready} onClick={() => ask('edit', '加入止损：持仓亏损超过 8% 时清仓')}>加止损</button>
        <button className="chip" disabled={busy || !ready} onClick={() => ask('edit', '把可调数字都提到 PARAMS 里，并加中文注释')}>参数化</button>
        <button className="chip" disabled={busy || !ready} onClick={() => ask('edit', '用 record() 记录关键指标，便于在回测图上观察')}>加 record</button>
      </div>
      <div className="ai-thread">
        {!msgs.length && (
          <div className="hint" style={{ padding: 12 }}>
            选择模式后输入需求。「修改代码」会返回完整新代码并显示差异，确认后才写入编辑器；生成的代码都会先经过沙箱检查与合成数据试跑。
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`ai-msg ${m.role}`}>
            {m.role === 'user' ? (
              <div className="bubble">{m.content}</div>
            ) : (
              <div className="bubble">
                {m.result && (
                  <div className="ai-result-head">
                    <Tag tone={m.result.mode === 'llm' ? 'agent' : 'warn'}>{m.result.mode === 'llm' ? m.result.model : '规则 / 模板降级'}</Tag>
                    {m.result.code && <Tag tone={m.result.checks?.ok ? 'ok' : 'fail'}>{m.result.checks?.ok ? '自检通过' : '自检未通过'}</Tag>}
                    {m.result.attempts > 1 && <Tag>自动修复 {m.result.attempts - 1} 次</Tag>}
                  </div>
                )}
                <Markdown text={m.content} compact />
                {m.result?.code && (
                  <div className="ai-code">
                    <DiffView before={m.base ?? code} after={m.result.code} maxHeight={300} />
                    <div className="button-row" style={{ marginTop: 8 }}>
                      <button className="button small primary" disabled={m.applied} onClick={() => apply(i)}>{m.applied ? '已应用' : '应用到编辑器'}</button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
        {busy && <div className="ai-msg assistant"><div className="bubble"><span className="spinner" /> 思考中…（生成代码会自动试跑，可能需要几十秒）</div></div>}
        <div ref={end} />
      </div>
      <div className="ai-input">
        <select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)} aria-label="AI 模式">
          <option value="edit">修改代码</option>
          <option value="chat">提问</option>
          <option value="generate">重新生成</option>
        </select>
        <textarea
          rows={2}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send() }}
          placeholder={mode === 'edit' ? '例：只在 4 小时均线向上时开仓；把仓位改成按波动率调整' : mode === 'chat' ? '例：为什么这段时间一直没有成交？' : '描述一个新策略，生成后可替换当前代码'}
        />
        <button className="button primary small" disabled={busy || !input.trim()} onClick={send}>发送</button>
      </div>
      <div className="hint">Ctrl+Enter 发送 · 当前策略：{strategy.name}</div>
    </div>
  )
}

/* ---------------- 参数 ---------------- */
function ParamsTab({ declared, overrides, setOverrides, dirty, onApply }: {
  declared: Params
  overrides: Params
  setOverrides: (p: Params) => void
  dirty: boolean
  onApply: (p: Params) => void
}) {
  const keys = Object.keys(declared)
  if (!keys.length) return (
    <EmptyState glyph="⚙" title="策略没有声明 PARAMS">
      在策略顶部写 <code className="inline">PARAMS = {'{"fast": 12, "slow": 48}'}</code>，代码里用 <code className="inline">PARAMS["fast"]</code> 读取。
      声明后可以在这里临时改参数回测，或做参数优化。
    </EmptyState>
  )
  const changed = Object.fromEntries(Object.entries(overrides).filter(([k, v]) => v !== '' && v !== declared[k]))
  return (
    <div>
      <p className="hint" style={{ marginBottom: 12 }}>这里填的值只用于本次「编译运行 / 运行回测」，不改代码；满意后点「写回代码」生成新版本。</p>
      <table className="param-table">
        <thead><tr><th>参数</th><th>代码中的值</th><th>本次运行</th></tr></thead>
        <tbody>
          {keys.map((k) => (
            <tr key={k}>
              <td><code className="inline">{k}</code></td>
              <td>{String(declared[k])}</td>
              <td>
                <input
                  type={typeof declared[k] === 'number' ? 'number' : 'text'}
                  step="any"
                  value={overrides[k] === undefined ? '' : String(overrides[k])}
                  placeholder={String(declared[k])}
                  onChange={(e) => {
                    const raw = e.target.value
                    const next = { ...overrides }
                    if (raw === '') delete next[k]
                    else next[k] = typeof declared[k] === 'number' ? Number(raw) : raw
                    setOverrides(next)
                  }}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="button-row" style={{ marginTop: 14 }}>
        <button className="button small" onClick={() => setOverrides({})}>清空</button>
        <button className="button small primary" disabled={!Object.keys(changed).length || dirty} title={dirty ? '请先保存代码' : ''} onClick={() => onApply(changed)}>写回代码</button>
      </div>
    </div>
  )
}

/* ---------------- 版本 ---------------- */
function VersionsTab({ slug, versions, current, onRestore }: { slug: string; versions: { version: string; file: string; saved_at: number; lines: number }[]; current: string; onRestore: (code: string) => void }) {
  const [view, setView] = useState<{ file: string; code: string } | null>(null)
  const [diff, setDiff] = useState(false)
  if (!versions.length) return <EmptyState glyph="◷" title="还没有历史版本">每次保存且内容有变化时，旧版本会自动快照到这里。</EmptyState>
  return (
    <>
      <table>
        <thead><tr><th>版本</th><th>保存时间</th><th className="right">行数</th><th /></tr></thead>
        <tbody>
          {versions.map((v) => (
            <tr key={v.file}>
              <td><code className="inline">{v.version}</code></td>
              <td>{fmtTime(v.saved_at)}</td>
              <td className="right">{v.lines}</td>
              <td><button className="button small" onClick={async () => setView({ file: v.file, code: await v3.version(slug, v.file) })}>查看</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      {view && (
        <Modal title={`历史版本 ${view.file}`} onClose={() => setView(null)} width={980}
          footer={<>
            <button className="button" onClick={() => setDiff(!diff)}>{diff ? '看全文' : '与当前对比'}</button>
            <button className="button primary" onClick={() => { onRestore(view.code); setView(null) }}>载入到编辑器</button>
          </>}>
          {diff ? <DiffView before={view.code} after={current} maxHeight={560} /> : <CodeView code={view.code} maxHeight={560} />}
        </Modal>
      )}
    </>
  )
}

function HistoryTab({ rows, onOpen }: { rows: BacktestRow[]; onOpen: (id: string) => void }) {
  if (!rows.length) return <EmptyState glyph="∿" title="还没有完整回测">点击「运行回测」生成第一份回测记录。</EmptyState>
  return (
    <table>
      <thead><tr><th>时间</th><th>区间</th><th className="right">收益</th><th className="right">回撤</th><th className="right">夏普</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id} className="clickable" onClick={() => onOpen(r.id)}>
            <td>{ago(r.created_at)}<small>{r.version}</small></td>
            <td>{fmtDate(r.start_ts)}<small>→ {fmtDate(r.end_ts)}</small></td>
            <td className={`right ${tone(r.metrics.total_return)}`}>{fmtPct(r.metrics.total_return, 2, true)}</td>
            <td className="right">{fmtPct(r.metrics.max_drawdown)}</td>
            <td className="right">{fmtNum(r.metrics.sharpe)}<small>{fmtMoney(r.initial_cash, 0)}</small></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
