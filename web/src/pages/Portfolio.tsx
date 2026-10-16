/**
 * 组合与风控：方案库（启用的方案可在策略里 optimize() 调用）、方案设计（评估器）、风险体检、试验记录。
 * 路由：#/portfolio · #/portfolio/design · #/portfolio/risk · #/portfolio/runs · #/portfolio/run/<id> · #/portfolio/<id>
 */
import { useEffect, useMemo, useState } from 'react'
import { usageSnippet } from '../apiModels'
import {
  portfolio, type EvalParams, type PortfolioConfig, type PortfolioMeta, type PortfolioReport, type PortfolioRun, type RiskCheck,
  type Scheme, type SchemeStatus,
} from '../apiPortfolio'
import { openInLab } from '../components/handoff'
import { f3, fp } from '../components/factorReport'
import { UseInStrategy } from '../components/modelReport'
import { PortfolioFullReport, SCHEME_STATUS_TONE, StressTable } from '../components/portfolioReport'
import { EmptyState, Field, Heading, Loading, Panel, Segments, Tag } from '../components/ui'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

type Tab = 'list' | 'design' | 'risk' | 'runs'
const TABS: { value: Tab; label: string }[] = [
  { value: 'list', label: '方案' }, { value: 'design', label: '方案设计' }, { value: 'risk', label: '风险体检' }, { value: 'runs', label: '试验记录' },
]
const STATUS_FALLBACK: Record<SchemeStatus, string> = { candidate: '候选', active: '启用', retired: '退役' }
const when = (ms?: number | null) => (ms ? new Date(ms * (ms < 1e11 ? 1000 : 1)).toISOString().slice(5, 16).replace('T', ' ') : '—')

export default function PortfolioPage({ arg, onNavigate, notify }: PageProps & { arg?: string }) {
  const [meta, setMeta] = useState<PortfolioMeta | null>(null)
  useEffect(() => { portfolio.meta().then(setMeta).catch((e: Error) => notify(e.message, true)) }, [notify])
  const runId = arg?.startsWith('run/') ? arg.slice(4) : null
  const tab: Tab | null = !arg ? 'list' : (['design', 'risk', 'runs'] as string[]).includes(arg) ? (arg as Tab) : null
  const optLabel = (id: string) => meta?.optimizers.find((o) => o.id === id)?.label ?? id
  return (
    <>
      <Heading title="组合与风控" description="把预期收益和风险变成权重，并把风控规则写进方案。每个方案都做滚动组合回测，和等权比较，附带风险分解、压力测试与有效前沿。"
        actions={<button className="button primary" onClick={() => openInLab({ idea: '', kind: 'portfolio' })}>✦ 让 AI 设计一个组合</button>} />
      <div className="fx-tabs">
        <Segments value={tab ?? (runId ? 'runs' : 'list')} options={TABS} onChange={(t) => onNavigate(t === 'list' ? 'portfolio' : `portfolio/${t}`)} ariaLabel="组合与风控视图" />
        {meta && <span className="hint">累计试过 {meta.trials} 种配置</span>}
      </div>
      {tab === 'list' && <SchemeList meta={meta} optLabel={optLabel} onOpen={(id) => onNavigate(`portfolio/${id}`)} notify={notify} />}
      {tab === 'design' && <Designer meta={meta} notify={notify} onSaved={(id) => onNavigate(`portfolio/${id}`)} />}
      {tab === 'risk' && <RiskCheckView notify={notify} />}
      {tab === 'runs' && <Runs optLabel={optLabel} onOpen={(id) => onNavigate(`portfolio/run/${id}`)} notify={notify} />}
      {runId && <RunDetail key={runId} id={runId} optLabel={optLabel} notify={notify} onSaved={(id) => onNavigate(`portfolio/${id}`)} />}
      {tab === null && !runId && arg && <SchemeDetail key={arg} id={arg} meta={meta} optLabel={optLabel} notify={notify} onDeleted={() => onNavigate('portfolio')} />}
    </>
  )
}

/* ------------------------------------------------------------------ */
function SchemeList({ meta, optLabel, onOpen, notify }: { meta: PortfolioMeta | null; optLabel: (id: string) => string; onOpen: (id: string) => void; notify: PageProps['notify'] }) {
  const [rows, setRows] = useState<Scheme[] | null>(null)
  const [status, setStatus] = useState<'' | SchemeStatus>('')
  useEffect(() => { portfolio.list(status || undefined).then((r) => setRows(r.schemes)).catch((e: Error) => notify(e.message, true)) }, [status, notify])
  const statuses = meta?.statuses ?? STATUS_FALLBACK
  return (
    <Panel flush title="配置方案" actions={
      <Segments value={status} onChange={setStatus} ariaLabel="状态"
        options={[{ value: '' as const, label: '全部' }, ...(Object.entries(statuses) as [SchemeStatus, string][]).map(([value, label]) => ({ value, label }))]} />
    }>
      {rows === null ? <Loading label="正在读取方案库" /> : rows.length === 0 ? (
        <EmptyState glyph="◫" title={status ? '没有这个状态的方案' : '方案库还是空的'}>在「方案设计」里评估一个方案，或在 AI 研究室选择「组合」让 AI 设计。</EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="fx-table">
            <thead><tr><th>方案</th><th>状态</th><th className="right">夏普</th><th className="right">等权夏普</th><th className="right">最大回撤</th><th className="right">年化波动</th><th className="right">有效持仓</th><th>更新</th></tr></thead>
            <tbody>{rows.map((s) => (
              <tr key={s.id} className="clickable" onClick={() => onOpen(s.id)}>
                <td><strong>{s.name}</strong><span className="ml-sub">{optLabel(s.config.optimizer)} · {s.symbols.length} 个标的 · {s.timeframe}{riskRuleText(s.config) ? ` · ${riskRuleText(s.config)}` : ''}</span></td>
                <td><Tag tone={SCHEME_STATUS_TONE[s.status]}>{statuses[s.status]}</Tag></td>
                <td className={`right num ${(s.summary?.sharpe ?? 0) >= 0 ? 'positive' : 'negative'}`}>{f3(s.summary?.sharpe, 2)}</td>
                <td className="right num muted">{f3(s.summary?.ew_sharpe, 2)}</td>
                <td className="right num">{fp(s.summary?.max_drawdown, 1)}</td>
                <td className="right num">{fp(s.summary?.ann_vol, 0)}</td>
                <td className="right num">{f3(s.summary?.effective_n, 1)}</td>
                <td className="muted">{when(s.updated_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

export function riskRuleText(c: PortfolioConfig): string {
  const r = c.risk || ({} as PortfolioConfig['risk'])
  return [r.vol_target ? `目标波动 ${fp(r.vol_target)}` : '', r.var_limit ? `日 VaR ≤ ${fp(r.var_limit, 1)}` : '',
    r.dd_stop ? `回撤 ${fp(r.dd_stop)} 减仓` : '', r.cppi ? `CPPI ${fp(r.cppi.floor)}` : ''].filter(Boolean).join('、')
}

/* ------------------------------------------------------------------ */
function SchemeDetail({ id, meta, optLabel, notify, onDeleted }: { id: string; meta: PortfolioMeta | null; optLabel: (id: string) => string; notify: PageProps['notify']; onDeleted: () => void }) {
  const [s, setS] = useState<Scheme | null>(null)
  const [report, setReport] = useState<PortfolioReport | null>(null)
  const [missing, setMissing] = useState(false)
  useEffect(() => {
    portfolio.get(id).then((r) => { setS(r.scheme); portfolio.report(id).then((x) => setReport(x.report)).catch(() => undefined) }).catch(() => setMissing(true))
  }, [id])
  if (missing) return <EmptyState glyph="◫" title="方案不存在">可能已被删除。</EmptyState>
  if (!s) return <Loading label="正在打开方案" />
  const statuses = meta?.statuses ?? STATUS_FALLBACK
  const spec = JSON.stringify({ ...s.config, timeframe: undefined }, null, 2)
  const toLab = (idea: string) => openInLab({
    kind: 'portfolio', idea, code: spec, title: `改进：${s.name}`, securities: s.symbols, timeframe: s.timeframe, attached: `方案「${s.name}」的配置`,
  })
  return (
    <>
      <Panel title={s.name} actions={<>
        <select value={s.status} aria-label="状态" onChange={async (e) => { try { setS((await portfolio.update(id, { status: e.target.value as SchemeStatus })).scheme) } catch (err) { notify((err as Error).message, true) } }}>
          {(Object.entries(statuses) as [SchemeStatus, string][]).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <button className="button small" onClick={() => toLab(`改进组合方案「${s.name}」：在不增加回撤的前提下提高夏普`)}>交给 AI 改进</button>
        <button className="button small" onClick={() => toLab(`召集组合小组评审「${s.name}」：风险估计、约束、压力情景`)}>召集团队评审</button>
        <button className="button small danger" onClick={async () => { if (await appConfirm(`删除方案「${s.name}」？试验记录会保留。`)) { await portfolio.remove(id); onDeleted() } }}>删除</button>
      </>}>
        <div className="fx-meta">
          <span>{optLabel(s.config.optimizer)} · {meta?.risk_models.find((r) => r.id === s.config.risk_model)?.label ?? s.config.risk_model} · {s.symbols.length} 个标的 · {s.timeframe}</span>
          <span>估计窗口 {s.config.lookback} 根 · 每 {s.config.rebalance} 根调仓 · 单币 ≤ {fp(s.config.constraints.max_weight)}{s.config.constraints.top_k ? ` · 只持前 ${s.config.constraints.top_k} 个` : ''}</span>
          {s.project_id && <span><a href={`#/lab/${s.project_id}`}>研究记录</a></span>}
        </div>
        {riskRuleText(s.config) && <div className="hint">风控规则：{riskRuleText(s.config)}</div>}
        {s.note && <div className="hint">{s.note}</div>}
      </Panel>
      <Panel title="用于策略" subtitle="optimize() 按方案在当前 bar 之前的价格上估计风险，返回已经应用约束与风控规则的目标权重。">
        <UseInStrategy code={usageSnippet('optimize', s.name)} />
      </Panel>
      <Panel title="评估报告">{report ? <PortfolioFullReport report={report} /> : <Loading label="正在读取报告" />}</Panel>
    </>
  )
}

/* ------------------------------------------------------------------ */
const pctIn = (v: number | null | undefined) => (v === null || v === undefined ? '' : String(+(v * 100).toFixed(2)))
const pctOut = (s: string) => (s.trim() === '' ? null : Number(s) / 100)

function Designer({ meta, notify, onSaved }: { meta: PortfolioMeta | null; notify: PageProps['notify']; onSaved: (id: string) => void }) {
  const [cfg, setCfg] = useState<PortfolioConfig | null>(null)
  const [env, setEnv] = useState<Omit<EvalParams, 'config'>>({ universe: 'major10', timeframe: '4h', bars_limit: 1500, source: 'auto', cost_bps: 10 })
  const [views, setViews] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ run: PortfolioRun; report: PortfolioReport } | null>(null)
  const [name, setName] = useState('')
  const [showJson, setShowJson] = useState(false)
  useEffect(() => { if (meta && !cfg) setCfg(JSON.parse(JSON.stringify(meta.defaults))) }, [meta, cfg])
  if (!meta || !cfg) return <Loading />
  const opt = meta.optimizers.find((o) => o.id === cfg.optimizer)
  const set = (p: Partial<PortfolioConfig>) => setCfg({ ...cfg, ...p })
  const setC = (p: Partial<PortfolioConfig['constraints']>) => setCfg({ ...cfg, constraints: { ...cfg.constraints, ...p } })
  const setR = (p: Partial<PortfolioConfig['risk']>) => setCfg({ ...cfg, risk: { ...cfg.risk, ...p } })
  const parsedViews = views.split('\n').map((l) => l.trim().split(/[\s,，]+/)).filter((x) => x.length >= 2 && x[0])
    .map(([symbol, ret, conf]) => ({ symbol: symbol.toUpperCase(), ret: Number(ret.replace('%', '')) / 100, confidence: conf ? Number(conf) : 0.5 }))
    .filter((v) => Number.isFinite(v.ret))
  const full: PortfolioConfig = { ...cfg, views: parsedViews }
  const run = async () => {
    setBusy(true)
    try { setResult(await portfolio.evaluate({ ...env, config: full })) } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const save = async (status: SchemeStatus) => {
    if (!result) return
    try { const r = await portfolio.save({ run_id: result.run.id, name: name || undefined, status }); notify(`已存入方案库「${r.scheme.name}」`); onSaved(r.scheme.id) } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <>
      <Panel title="设计一个方案" subtitle="每个调仓点只用之前 lookback 根的收益估计风险与预期收益；权重在调仓之间随价格漂移；扣换手成本；同时跑等权作为基准。">
        <div className="config-bar">
          <Field label="优化器">
            <select value={cfg.optimizer} onChange={(e) => set({ optimizer: e.target.value })}>{meta.optimizers.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}</select>
          </Field>
          <Field label="风险模型">
            <select value={cfg.risk_model} onChange={(e) => set({ risk_model: e.target.value })}>{meta.risk_models.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}</select>
          </Field>
          <Field label="预期收益">
            <select value={cfg.expected} onChange={(e) => set({ expected: e.target.value })}>
              {meta.expected.map((o) => <option key={o.id} value={o.id} disabled={opt?.needs_mu && o.id === 'none'}>{o.label.split('：')[0]}</option>)}
              {meta.models.length > 0 && <optgroup label="模型库">{meta.models.map((m) => <option key={m.name} value={`model:${m.name}`}>模型：{m.name}</option>)}</optgroup>}
              {meta.factors.length > 0 && <optgroup label="因子库">{meta.factors.map((f) => <option key={f.name} value={`factor:${f.name}`}>因子：{f.name}</option>)}</optgroup>}
            </select>
          </Field>
          <Field label="估计窗口（根）"><input type="number" min={30} max={2000} value={cfg.lookback} onChange={(e) => set({ lookback: Number(e.target.value) })} /></Field>
          <Field label="调仓间隔（根）"><input type="number" min={1} max={500} value={cfg.rebalance} onChange={(e) => set({ rebalance: Number(e.target.value) })} /></Field>
          <Field label="标的池">
            <select value={env.universe} onChange={(e) => setEnv({ ...env, universe: e.target.value })}>{meta.universes.map((u) => <option key={u.id} value={u.id}>{u.name}（{u.symbols.length}）</option>)}</select>
          </Field>
          <Field label="周期"><select value={env.timeframe} onChange={(e) => setEnv({ ...env, timeframe: e.target.value })}>{['1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}</select></Field>
          <Field label="K 线数"><select value={env.bars_limit} onChange={(e) => setEnv({ ...env, bars_limit: Number(e.target.value) })}>{[800, 1500, 3000, 6000].map((n) => <option key={n} value={n}>{n}</option>)}</select></Field>
          <Field label="数据">
            <select value={env.source} onChange={(e) => setEnv({ ...env, source: e.target.value as EvalParams['source'] })}>
              <option value="auto">真实行情（缓存优先）</option><option value="exchange">只用真实数据</option>
            </select>
          </Field>
          <Field label="单边成本 bps"><input type="number" min={0} max={200} value={env.cost_bps} onChange={(e) => setEnv({ ...env, cost_bps: Number(e.target.value) })} /></Field>
        </div>
        {opt && <div className="hint">{opt.hint}</div>}

        <div className="pf-groups">
          <fieldset>
            <legend>约束</legend>
            <label className="pf-check"><input type="checkbox" checked={cfg.constraints.long_only} onChange={(e) => setC({ long_only: e.target.checked })} />只做多</label>
            <Field label="单币上限 %"><input type="number" min={2} max={100} value={pctIn(cfg.constraints.max_weight)} onChange={(e) => setC({ max_weight: pctOut(e.target.value) ?? 0.35 })} /></Field>
            <Field label="总仓位上限 %"><input type="number" min={10} max={300} value={pctIn(cfg.constraints.max_gross)} onChange={(e) => setC({ max_gross: pctOut(e.target.value) ?? 1 })} /></Field>
            <Field label="单次换手上限 %"><input type="number" min={1} max={200} value={pctIn(cfg.constraints.max_turnover)} onChange={(e) => setC({ max_turnover: pctOut(e.target.value) ?? 1 })} /></Field>
            <Field label="只持前 k 个（0 = 不限）"><input type="number" min={0} max={50} value={cfg.constraints.top_k} onChange={(e) => setC({ top_k: Number(e.target.value) })} /></Field>
          </fieldset>
          <fieldset>
            <legend>风控规则（留空 = 不启用）</legend>
            <Field label="目标年化波动 %"><input type="number" min={2} max={300} placeholder="例如 50" value={pctIn(cfg.risk.vol_target)} onChange={(e) => setR({ vol_target: pctOut(e.target.value) })} /></Field>
            <Field label="单日 VaR95 上限 %"><input type="number" min={0.2} max={50} step={0.5} placeholder="例如 5" value={pctIn(cfg.risk.var_limit)} onChange={(e) => setR({ var_limit: pctOut(e.target.value) })} /></Field>
            <Field label="回撤超过 % 时减半"><input type="number" min={2} max={90} placeholder="例如 20" value={pctIn(cfg.risk.dd_stop)} onChange={(e) => setR({ dd_stop: pctOut(e.target.value) })} /></Field>
            <label className="pf-check"><input type="checkbox" checked={!!cfg.risk.cppi} onChange={(e) => setR({ cppi: e.target.checked ? { floor: 0.8, multiplier: 3 } : null })} />CPPI 保本（保底 80%，乘数 3）</label>
          </fieldset>
          <fieldset>
            <legend>参数</legend>
            {(cfg.optimizer === 'mean_var' || cfg.optimizer === 'black_litterman') && <Field label="风险厌恶"><input type="number" min={0.1} max={50} step={0.5} value={cfg.risk_aversion} onChange={(e) => set({ risk_aversion: Number(e.target.value) })} /></Field>}
            {cfg.optimizer === 'kelly' && <Field label="凯利比例"><input type="number" min={0.05} max={1} step={0.05} value={cfg.kelly_fraction} onChange={(e) => set({ kelly_fraction: Number(e.target.value) })} /></Field>}
            {cfg.risk_model === 'ewma' && <Field label="EWMA 半衰期（根）"><input type="number" min={5} max={500} value={cfg.ewma_halflife} onChange={(e) => set({ ewma_halflife: Number(e.target.value) })} /></Field>}
            {cfg.risk_model === 'factor' && <Field label="主成分个数"><input type="number" min={1} max={10} value={cfg.factors} onChange={(e) => set({ factors: Number(e.target.value) })} /></Field>}
            {cfg.optimizer === 'min_cvar' && <Field label="CVaR 置信度"><input type="number" min={0.8} max={0.995} step={0.01} value={cfg.cvar_alpha} onChange={(e) => set({ cvar_alpha: Number(e.target.value) })} /></Field>}
            {cfg.optimizer === 'black_litterman' && (
              <Field label="观点（每行：标的 年化收益% 信心0~1）" hint={`已识别 ${parsedViews.length} 条；预期收益选了模型时，模型预测也会作为观点`}>
                <textarea className="fr-editor" rows={3} value={views} placeholder={'BTC/USDT 40 0.6\nSOL/USDT -20 0.3'} onChange={(e) => setViews(e.target.value)} />
              </Field>
            )}
            {!['mean_var', 'black_litterman', 'kelly', 'min_cvar'].includes(cfg.optimizer) && cfg.risk_model !== 'ewma' && cfg.risk_model !== 'factor' && <div className="hint">这个组合不需要额外参数。</div>}
          </fieldset>
        </div>
        <div className="ml-run">
          <button className="button primary" disabled={busy} onClick={run}>{busy ? <><span className="spinner" /> 评估中</> : '◫ 评估'}</button>
          <button className="button small" onClick={() => setShowJson(!showJson)}>{showJson ? '收起 JSON' : '查看 JSON'}</button>
        </div>
        {showJson && <pre className="mr-code" style={{ marginTop: 10 }}>{JSON.stringify(full, null, 2)}</pre>}
      </Panel>
      {result && (
        <Panel title="评估结果" actions={<>
          <input className="fx-search" placeholder="方案名称" value={name} onChange={(e) => setName(e.target.value)} aria-label="方案名称" />
          <button className="button small" onClick={() => save('candidate')}>存为候选</button>
          <button className="button small primary" disabled={!result.report.admitted} title={result.report.admitted ? '' : '风险调整后跑输等权'} onClick={() => save('active')}>启用</button>
        </>}>
          <PortfolioFullReport report={result.report} />
        </Panel>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
function RiskCheckView({ notify }: { notify: PageProps['notify'] }) {
  const [text, setText] = useState('BTC/USDT 40\nETH/USDT 30\nSOL/USDT 15\nDOGE/USDT 15')
  const [tf, setTf] = useState('4h')
  const [source, setSource] = useState('auto')
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState<RiskCheck | null>(null)
  const weights = useMemo(() => Object.fromEntries(text.split('\n').map((l) => l.trim().split(/[\s,，:：]+/)).filter((x) => x.length >= 2 && x[0])
    .map(([s, w]) => [s.toUpperCase(), Number(w.replace('%', '')) / 100] as [string, number]).filter(([, w]) => Number.isFinite(w) && w !== 0)), [text])
  const total = Object.values(weights).reduce((a, b) => a + b, 0)
  const run = async () => {
    setBusy(true)
    try { setRes(await portfolio.risk({ weights, timeframe: tf, source })) } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  return (
    <>
      <Panel title="给一组持仓做风险体检" subtitle="输入你现在（或打算）的持仓比例，看波动、VaR/CVaR、谁贡献了风险，以及历史危机里会亏多少。">
        <div className="pf-riskin">
          <textarea className="fr-editor" rows={6} value={text} onChange={(e) => setText(e.target.value)} aria-label="持仓（每行：标的 百分比）" />
          <div>
            <Field label="周期"><select value={tf} onChange={(e) => setTf(e.target.value)}>{['1h', '4h', '1d'].map((t) => <option key={t}>{t}</option>)}</select></Field>
            <Field label="数据"><select value={source} onChange={(e) => setSource(e.target.value)}><option value="auto">真实行情（缓存优先）</option><option value="exchange">只用真实数据</option></select></Field>
            <div className="hint">已识别 {Object.keys(weights).length} 个标的，合计 {fp(total, 0)}{total < 0.999 ? `（其余 ${fp(1 - total, 0)} 视为现金）` : ''}</div>
            <button className="button primary" disabled={busy || !Object.keys(weights).length} onClick={run}>{busy ? <><span className="spinner" /> 计算中</> : '体检'}</button>
          </div>
        </div>
      </Panel>
      {res && (
        <div className="two-columns even">
          <Panel title="风险度量">
            <div className="pf-risk">
              <div><span>年化波动</span><b className="num">{fp(res.ann_vol, 1)}</b></div>
              <div><span>单日 VaR 95%</span><b className="num">{fp(res.var_95_day_param, 2)}</b></div>
              <div><span>单根 VaR / CVaR 95%</span><b className="num">{fp(res.var_95_bar, 2)} / {fp(res.cvar_95_bar, 2)}</b></div>
            </div>
            <h4 className="ml-h4">风险贡献</h4>
            <div className="pf-weights">
              {Object.entries(res.risk_contrib).sort((a, b) => b[1] - a[1]).map(([s, v]) => (
                <div key={s} className="pf-wrow two"><span className="pf-sym">{s}</span>
                  <span className="pf-bar rc"><i style={{ width: `${Math.min(100, Math.abs(v) * 100)}%` }} /><b className="num">{fp(v, 1)}</b></span>
                  <span className="num muted">持仓 {fp(res.weights[s], 0)}</span></div>
              ))}
            </div>
            {res.panel.synthetic && <div className="hint">含合成行情，只能验证流程。</div>}
          </Panel>
          <Panel title="压力测试"><StressTable rows={res.stress} /></Panel>
        </div>
      )}
    </>
  )
}

/* ------------------------------------------------------------------ */
function Runs({ optLabel, onOpen, notify }: { optLabel: (id: string) => string; onOpen: (id: string) => void; notify: PageProps['notify'] }) {
  const [data, setData] = useState<{ runs: PortfolioRun[]; trials: number } | null>(null)
  useEffect(() => { portfolio.runs().then(setData).catch((e: Error) => notify(e.message, true)) }, [notify])
  if (!data) return <Loading label="正在读取试验记录" />
  return (
    <Panel flush title={`试验记录（${data.runs.length}）`} subtitle="每次评估一笔，包括没通过的。">
      {data.runs.length === 0 ? <EmptyState glyph="☰" title="还没有试验" /> : (
        <div className="table-wrap">
          <table className="fx-table">
            <thead><tr><th>方案</th><th className="right">夏普</th><th className="right">等权</th><th className="right">最大回撤</th><th className="right">波动</th><th>检查</th><th>来源</th><th>时间</th></tr></thead>
            <tbody>{data.runs.map((r) => (
              <tr key={r.id} className="clickable" onClick={() => onOpen(r.id)}>
                <td><strong>{optLabel(r.optimizer)}</strong><span className="ml-sub">{r.risk_model} · {r.expected !== 'none' ? `${r.expected} · ` : ''}{r.symbols.length} 个标的 · {r.timeframe}{r.note ? ` · ${r.note}` : ''}</span></td>
                <td className={`right num ${(r.summary.sharpe ?? 0) >= 0 ? 'positive' : 'negative'}`}>{f3(r.summary.sharpe, 2)}</td>
                <td className="right num muted">{f3(r.summary.ew_sharpe, 2)}</td>
                <td className="right num">{fp(r.summary.max_drawdown, 1)}</td>
                <td className="right num">{fp(r.summary.ann_vol, 0)}</td>
                <td>{r.admitted ? <Tag tone="ok">可启用</Tag> : <Tag>跑输等权</Tag>}</td>
                <td className="muted">{r.source === 'lab' ? 'AI 研究室' : '方案设计'}</td>
                <td className="muted">{when(r.created_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function RunDetail({ id, optLabel, notify, onSaved }: { id: string; optLabel: (id: string) => string; notify: PageProps['notify']; onSaved: (id: string) => void }) {
  const [data, setData] = useState<{ run: PortfolioRun; report: PortfolioReport } | null>(null)
  const [missing, setMissing] = useState(false)
  const [name, setName] = useState('')
  useEffect(() => { portfolio.run(id).then(setData).catch(() => setMissing(true)) }, [id])
  if (missing) return <EmptyState glyph="☰" title="试验不存在" />
  if (!data) return <Loading label="正在打开试验" />
  const save = async (status: SchemeStatus) => {
    try { const r = await portfolio.save({ run_id: id, name: name || undefined, status }); notify(`已存入方案库「${r.scheme.name}」`); onSaved(r.scheme.id) } catch (e) { notify((e as Error).message, true) }
  }
  return (
    <Panel title={optLabel(data.run.optimizer)} subtitle={`${when(data.run.created_at)} · ${data.run.symbols.length} 个标的 · ${data.run.timeframe}`} actions={<>
      <input className="fx-search" placeholder="方案名称" value={name} onChange={(e) => setName(e.target.value)} aria-label="方案名称" />
      <button className="button small" onClick={() => save('candidate')}>存为候选</button>
      <button className="button small primary" disabled={!data.report.admitted} onClick={() => save('active')}>启用</button>
    </>}>
      {data.run.project_id && <div className="hint">来自 <a href={`#/lab/${data.run.project_id}`}>AI 研究室的项目</a></div>}
      <PortfolioFullReport report={data.report} />
    </Panel>
  )
}
