/**
 * 试验台账：试验台账（试了多少次、最好成绩按试验次数修正后还剩多少）、紧封测试集、血缘图。
 * 路由：#/discipline · #/discipline/holdout · #/discipline/lineage
 */
import { useEffect, useMemo, useState } from 'react'
import {
  DOMAIN_LABEL, VERDICT_TONE, day, rd, when, type Family, type HoldoutLog, type HoldoutView, type Ledger, type Lineage, type LNode, type Pbo,
  type UnsealRecord, type Verdict,
} from '../apiRd'
import { Scatter } from '../components/charts2'
import { f3, fp } from '../components/factorReport'
import { Callout, EmptyState, Field, Heading, Loading, MetricCard, Panel, Segments, Tag } from '../components/ui'
import type { PageProps } from './types'
import { appConfirm, appPrompt } from '../components/dialog'

type Tab = 'ledger' | 'holdout' | 'lineage'
const TABS: { value: Tab; label: string }[] = [
  { value: 'ledger', label: '全部试验' }, { value: 'holdout', label: '紧封测试集' }, { value: 'lineage', label: '血缘图' },
]
const VERDICT_TEXT: Record<Verdict, string> = { ok: '经得起修正', warn: '存疑', bad: '可能是运气' }

export default function DisciplinePage({ arg, onNavigate, notify }: PageProps & { arg?: string }) {
  const tab: Tab = arg === 'holdout' || arg === 'lineage' ? arg : 'ledger'
  return (
    <>
      <Heading title="试验台账" description="试得越多，越容易碰上一个好看的结果。这里记下每一次试验，按试验次数修正最好成绩；留一段谁都看不到的数据做最终检验；并追踪每个成果用到了什么，上游一变就提示重跑。" />
      <div className="fx-tabs">
        <Segments value={tab} options={TABS} onChange={(t) => onNavigate(t === 'ledger' ? 'discipline' : `discipline/${t}`)} ariaLabel="试验台账视图" />
      </div>
      {tab === 'ledger' && <LedgerView notify={notify} onNavigate={onNavigate} />}
      {tab === 'holdout' && <HoldoutViewPanel notify={notify} onNavigate={onNavigate} />}
      {tab === 'lineage' && <LineageView notify={notify} onNavigate={onNavigate} />}
    </>
  )
}

/* ------------------------------------------------------------------ */
/* 试验台账                                                             */
/* ------------------------------------------------------------------ */
function LedgerView({ notify, onNavigate }: Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [lg, setLg] = useState<Ledger | null>(null)
  const [pbo, setPbo] = useState<{ fam: Family; data: Pbo | null; error?: string } | null>(null)
  useEffect(() => { rd.ledger().then(setLg).catch((e: Error) => notify(e.message, true)) }, [notify])

  const runPbo = async (fam: Family) => {
    setPbo({ fam, data: null })
    try {
      const data = await rd.pbo(fam.domain === 'strategy' ? { slug: fam.family } : { project_id: fam.project_id ?? fam.family })
      setPbo({ fam, data })
    } catch (e) { setPbo({ fam, data: null, error: (e as Error).message }) }
  }

  if (!lg) return <Loading label="正在汇总试验记录" />
  const alpha = (['strategy', 'factor', 'model', 'portfolio'] as const)
  return (
    <>
      <div className="metrics-grid">
        <MetricCard label="累计试验" value={String(lg.total)} unit="次" caption={alpha.map((d) => `${DOMAIN_LABEL[d]} ${lg.by_domain[d] ?? 0}`).join(' · ')} />
        <MetricCard label="找收益的试验" value={String(lg.total_alpha)} unit="次" caption="不含执行方案（执行只比成本）" />
        <MetricCard label="现在需要的 t 值" value={f3(lg.t_needed_global, 2)} caption="按全部试验做多重检验修正（Bonferroni）" />
        <MetricCard label="紧封测试集" value={lg.holdout.sealed ? `${lg.holdout.days} 天` : '未设置'}
          caption={lg.holdout.sealed ? `紧封线 ${day(lg.holdout.cutoff)} · 已开封 ${lg.holdout.unseals ?? 0} 次` : '建议留出最近 60 天做最终检验'} />
      </div>

      <Panel flush title="各研究方向" subtitle="同一个策略的历次回测、同一个研究项目的各个版本算一个方向。策略和组合看「修正后的夏普」（DSR，≥ 95% 才算经得起修正）；因子和模型看 t 值是否过了按试验次数提高的门槛。">
        {lg.families.length === 0 ? (
          <EmptyState glyph="⚖" title="还没有试验记录">跑一次回测、评估一个因子或训练一个模型，就会记在这里。</EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="fx-table rd-ledger">
              <thead><tr><th>方向</th><th className="right">试验</th><th>最好成绩</th><th className="right">修正后</th><th>结论</th><th /></tr></thead>
              <tbody>{lg.families.map((f) => (
                <tr key={`${f.domain}:${f.family}`} className={pbo?.fam === f ? 'rd-picked' : undefined}>
                  <td><strong>{f.label}</strong><span className="ml-sub">{DOMAIN_LABEL[f.domain]} · 最近 {when(f.last_at)}</span></td>
                  <td className="right num">{f.n}</td>
                  <td><BestCell f={f} /></td>
                  <td className="right num">{f.dsr !== undefined ? fp(f.dsr, 0) : f.t_needed !== undefined ? `t ≥ ${f3(f.t_needed, 2)}` : '—'}</td>
                  <td>{f.domain === 'execution' ? <span className="muted">只比成本</span> : f.verdict ? <Tag tone={VERDICT_TONE[f.verdict]}>{VERDICT_TEXT[f.verdict]}</Tag> : <span className="muted">—</span>}</td>
                  <td className="right">
                    {(f.domain === 'strategy' || f.project_id) && f.domain !== 'execution' && f.n >= 2 && (
                      <button className="button small" onClick={() => runPbo(f)}>过拟合检验</button>
                    )}
                    {f.project_id && <button className="button small ghost" onClick={() => onNavigate(`lab/${f.project_id}`)}>研究记录</button>}
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Panel>

      {pbo && <PboPanel fam={pbo.fam} data={pbo.data} error={pbo.error} onClose={() => setPbo(null)} />}

      <Panel flush title="最近的试验" subtitle="每一次评估都留档，删不掉，也不该删：它们决定了上面的修正力度。">
        <RecentTrials rows={lg.recent.slice(0, 200)} />
      </Panel>
    </>
  )
}

type Trial = Ledger['recent'][number]
const BATCH = /^(.*?)\s*第\s*\d+\s*组$/

function score(t: Trial): number | null {
  for (const v of [t.sharpe, t.t_stat, t.value]) if (v !== undefined && v !== null) return Number(v)
  return null
}

function scoreText(t: Trial) {
  return t.sharpe !== undefined && t.sharpe !== null ? `夏普 ${f3(t.sharpe, 2)}` : t.t_stat !== undefined && t.t_stat !== null ? `t ${f3(t.t_stat, 2)}` : t.value !== undefined && t.value !== null ? `${f3(t.value, 1)} bps` : '—'
}

/** 一批调参里每一组的成绩画成一排点：一眼看出这批结果分布在哪、有没有过 0。 */
function DotStrip({ values }: { values: number[] }) {
  const lo = Math.min(...values, 0), hi = Math.max(...values, 0)
  const x = (v: number) => 4 + ((v - lo) / (hi - lo || 1)) * 132
  return (
    <svg className="dot-strip" viewBox="0 0 140 14" width="140" height="14" aria-hidden="true">
      <line x1="4" x2="136" y1="7" y2="7" className="ds-axis" />
      <line x1={x(0)} x2={x(0)} y1="2" y2="12" className="ds-zero" />
      {values.map((v, i) => <circle key={i} cx={x(v)} cy="7" r="2.2" className={v > 0 ? 'ds-pos' : 'ds-neg'} />)}
    </svg>
  )
}

/** 最近的试验：同一方向、同一批调参（「…第 N 组」）合成一行，点开看每一组。 */
function RecentTrials({ rows }: { rows: Trial[] }) {
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [more, setMore] = useState(false)
  const groups: { key: string; head: Trial; items: Trial[]; label: string | null }[] = []
  for (const t of rows) {
    const m = BATCH.exec(t.name)
    const last = groups[groups.length - 1]
    if (m && last && last.label === m[1] && last.head.domain === t.domain && last.head.family_label === t.family_label) {
      last.items.push(t)
      continue
    }
    groups.push({ key: `${t.domain}:${t.id}`, head: t, items: [t], label: m ? m[1] : null })
  }
  const shown = more ? groups : groups.slice(0, 12)
  return (
    <>
      <div className="table-wrap">
        <table className="fx-table">
          <thead><tr><th>时间</th><th>类型</th><th>方向</th><th>试了什么</th><th className="right">成绩</th></tr></thead>
          <tbody>{shown.flatMap((g) => {
            if (g.items.length === 1) {
              const t = g.head
              return [(
                <tr key={g.key}>
                  <td className="muted">{when(t.created_at)}</td>
                  <td>{DOMAIN_LABEL[t.domain]}</td>
                  <td><span className="ellipsis rd-cell">{t.family_label}</span></td>
                  <td><span className="ellipsis rd-cell"><code className="inline">{t.name}</code></span></td>
                  <td className="right num">{scoreText(t)}</td>
                </tr>
              )]
            }
            const vals = g.items.map(score).filter((v): v is number => v !== null)
            const best = g.items.reduce((a, b) => ((score(b) ?? -Infinity) > (score(a) ?? -Infinity) ? b : a))
            const isOpen = !!open[g.key]
            return [(
              <tr key={g.key} className="clickable batch-row" onClick={() => setOpen({ ...open, [g.key]: !isOpen })}>
                <td className="muted">{when(g.head.created_at)}</td>
                <td>{DOMAIN_LABEL[g.head.domain]}</td>
                <td><span className="ellipsis rd-cell">{g.head.family_label}</span></td>
                <td>
                  <span className="batch-name"><span className="batch-caret" aria-hidden="true">{isOpen ? '▾' : '▸'}</span>{g.label} · {g.items.length} 组</span>
                  {vals.length > 1 && <DotStrip values={vals} />}
                </td>
                <td className="right num">{vals.length ? <>最好 {scoreText(best)}<small className="batch-range">{f3(Math.min(...vals), 2)} ~ {f3(Math.max(...vals), 2)}</small></> : '—'}</td>
              </tr>
            ), ...(isOpen ? g.items.map((t) => (
              <tr key={`${t.domain}:${t.id}`} className="batch-child">
                <td className="muted">{when(t.created_at)}</td>
                <td />
                <td />
                <td><span className="ellipsis rd-cell"><code className="inline">{t.name}</code></span></td>
                <td className="right num">{scoreText(t)}</td>
              </tr>
            )) : [])]
          })}</tbody>
        </table>
      </div>
      {groups.length > 12 && (
        <div className="table-foot"><button className="button small ghost" onClick={() => setMore(!more)}>{more ? '收起' : `显示全部 ${groups.length} 行`}</button></div>
      )}
    </>
  )
}

function BestCell({ f }: { f: Family }) {
  if (f.best_sharpe !== undefined) return <span>夏普 <b className="num">{f3(f.best_sharpe, 2)}</b><span className="ml-sub">{f.best_name} · 门槛 {f3(f.sr0_ann, 2)}</span></span>
  if (f.best_t !== undefined) return <span>t <b className="num">{f3(f.best_t, 2)}</b><span className="ml-sub ellipsis rd-cell">{f.best_name}</span></span>
  if (f.domain === 'execution' && f.best_value !== undefined && f.best_value !== null) return <span>最低成本 <b className="num">{f3(f.best_value, 1)}</b> bps</span>
  return <span className="muted">—</span>
}

function PboPanel({ fam, data, error, onClose }: { fam: Family; data: Pbo | null; error?: string; onClose: () => void }) {
  return (
    <Panel title={`过拟合检验 · ${fam.label}`} subtitle="组合对称交叉验证（CSCV）：把时间切成若干段，轮流用一半挑最好的版本，看它在另一半里排第几。挑出来的版本在另一半里经常垫底，说明「最好」靠的是运气。"
      actions={<button className="button small ghost" onClick={onClose}>收起</button>}>
      {error ? <Callout tone="warn" title="做不了这次检验">{error}</Callout> : !data ? <Loading label="正在切分样本、逐段比较" /> : (
        <>
          <div className="metrics-grid">
            <MetricCard label="过拟合概率 PBO" value={fp(data.pbo, 0)} caption={data.pbo > 0.5 ? '超过一半：挑出的最好版本多半靠运气' : data.pbo > 0.2 ? '有一定运气成分' : '挑选过程比较可靠'} />
            <MetricCard label="修正后夏普 DSR" value={fp(data.dsr.dsr, 0)} caption={`考虑 ${data.dsr.n_trials} 次试验后，真实夏普 > 0 的概率`} />
            <MetricCard label="样本外亏钱的概率" value={fp(data.prob_oos_loss, 0)} caption="挑出的最好版本在另一半里收益为负" />
            <MetricCard label="样本内外衰减" value={f3(data.degradation_slope, 2)} caption="样本内每多 1 分，样本外变化多少；接近 1 最好" />
          </div>
          <div className="rd-pbo">
            <div>
              <h3 className="rd-sub">逐段排名的 logit 分布</h3>
              <LogitHist hist={data.hist} />
              <p className="hint">左侧（&lt; 0）是挑出的版本在另一半里排在中位数以下的次数。{data.versions.length} 个版本 · {data.t} 个时间点 · {data.combos} 种切法。</p>
            </div>
            <div>
              <h3 className="rd-sub">样本内 vs 样本外（夏普）</h3>
              <Scatter points={data.scatter.map((p, i) => ({ x: p.is, y: p.oos, label: `切法 ${i + 1}` }))} xLabel="样本内" yLabel="样本外" />
            </div>
          </div>
          <div className="fx-meta"><span>样本内最好的是 <b>{data.best}</b></span>
            <span>各版本夏普：{Object.entries(data.sharpe).slice(0, 8).map(([k, v]) => `${k} ${f3(v, 2)}`).join(' · ')}</span></div>
        </>
      )}
    </Panel>
  )
}

function LogitHist({ hist }: { hist: Pbo['hist'] }) {
  const W = 420, H = 180, P = { l: 30, r: 10, t: 10, b: 26 }
  if (!hist.length) return <div className="empty-state small"><h3>没有分布</h3></div>
  const lo = hist[0].x0, hi = hist[hist.length - 1].x1
  const max = Math.max(...hist.map((h) => h.n), 1)
  const X = (v: number) => P.l + ((v - lo) / (hi - lo || 1)) * (W - P.l - P.r)
  const Y = (n: number) => P.t + (1 - n / max) * (H - P.t - P.b)
  return (
    <div className="chart2"><div className="chart2-frame" style={{ aspectRatio: `${W} / ${H}` }}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="logit 分布">
        {hist.map((h, i) => (
          <rect key={i} x={X(h.x0) + 1} width={Math.max(1, X(h.x1) - X(h.x0) - 2)} y={Y(h.n)} height={H - P.b - Y(h.n)}
            style={{ fill: h.x1 <= 0 ? 'var(--amber)' : 'var(--accent)' }} opacity={0.75} rx={2} />
        ))}
        {lo < 0 && hi > 0 && <line className="chart-zero" x1={X(0)} x2={X(0)} y1={P.t} y2={H - P.b} />}
        <text className="chart-label" x={X(lo)} y={H - 8}>{lo.toFixed(1)}</text>
        {lo < 0 && hi > 0 && <text className="chart-label" x={X(0)} y={H - 8} textAnchor="middle">0</text>}
        <text className="chart-label" x={X(hi)} y={H - 8} textAnchor="end">{hi.toFixed(1)}</text>
      </svg>
    </div></div>
  )
}

/* ------------------------------------------------------------------ */
/* 紧封测试集                                                           */
/* ------------------------------------------------------------------ */
type HoldoutFull = HoldoutView & { records: UnsealRecord[]; log: HoldoutLog[] }
const UNSEAL_KINDS: { id: UnsealRecord['kind']; label: string; layer: string }[] = [
  { id: 'strategy', label: '策略', layer: 'strategy' }, { id: 'factor', label: '因子', layer: 'factor' },
  { id: 'model', label: '模型', layer: 'model' }, { id: 'portfolio', label: '组合方案', layer: 'portfolio' },
]
const LOG_TEXT: Record<HoldoutLog['action'], string> = { seal: '设置紧封线', change: '调整紧封线', remove: '解除紧封' }

function HoldoutViewPanel({ notify, onNavigate }: Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [h, setH] = useState<HoldoutFull | null>(null)
  const [nodes, setNodes] = useState<LNode[]>([])
  const [days, setDays] = useState(60)
  const [note, setNote] = useState('')
  const [kind, setKind] = useState<UnsealRecord['kind']>('strategy')
  const [ref, setRef] = useState('')
  const [busy, setBusy] = useState(false)
  const load = () => rd.holdout().then(setH).catch((e: Error) => notify(e.message, true))
  useEffect(() => { load(); rd.lineage().then((g) => setNodes(g.nodes)).catch(() => undefined) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const options = nodes.filter((n) => n.layer === UNSEAL_KINDS.find((k) => k.id === kind)?.layer)
  const opened = new Set((h?.records ?? []).map((r) => `${r.kind}:${r.ref}`))
  useEffect(() => { setRef(options.find((o) => !opened.has(`${kind}:${o.ref}`))?.ref ?? '') }, [kind, nodes.length, h?.records.length]) // eslint-disable-line react-hooks/exhaustive-deps

  const seal = async () => {
    setBusy(true)
    try { setH(await rd.seal({ days, note })); notify('紧封线已设置') } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const remove = async () => {
    const reason = await appPrompt('解除紧封会让之后的研究用到全部数据，已开封的记录保留。写一句原因：')
    if (!reason || reason.trim().length < 2) return
    try { await rd.removeSeal(reason.trim()); await load(); notify('已解除紧封') } catch (e) { notify((e as Error).message, true) }
  }
  const unseal = async () => {
    const name = options.find((o) => o.ref === ref)?.label ?? ref
    if (!await appConfirm(`开封「${name}」？\n\n每个成果只能开封一次，结果永久记录。开封后如果再改它，这份测试集对它就不再干净。`)) return
    setBusy(true)
    try {
      const r = await rd.unseal(kind, ref)
      notify(r.again ? '这个成果之前开封过，显示的是当时的结果' : '开封完成，结果已记录')
      await load()
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }

  if (!h) return <Loading label="正在读取紧封设置" />
  return (
    <>
      {h.sealed ? (
        <Callout title={`紧封线 ${day(h.cutoff)}：最近 ${h.days} 天的数据被封存`}>
          回测、因子评估、模型训练、组合评估都只能看到紧封线之前的数据。等你认为研究做完了，再「开封」用这段数据做一次最终检验。
          {h.note ? <> 备注：{h.note}</> : null}
        </Callout>
      ) : (
        <Callout tone="warn" title="还没有紧封测试集">
          所有研究都在同一段历史上反复试，最后的好成绩里混着大量运气。建议留出最近 60 天，研究阶段谁都看不到。
        </Callout>
      )}

      <div className="two-columns even">
        <Panel title={h.sealed ? '调整紧封线' : '设置紧封测试集'} subtitle="已经有成果开封过时，紧封线只能往前移（测试集变长），不能往后移。">
          <div className="config-bar rd-form">
            <Field label="封存最近多少天">
              <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
                {[30, 45, 60, 90, 120, 180].map((d) => <option key={d} value={d}>{d} 天</option>)}
              </select>
            </Field>
            <Field label="备注"><input value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：2026 年第四季度研究" /></Field>
          </div>
          <div className="button-row">
            <button className="button primary" disabled={busy} onClick={seal}>{h.sealed ? '更新紧封线' : '封存'}</button>
            {h.sealed && <button className="button danger" onClick={remove}>解除紧封</button>}
          </div>
        </Panel>

        <Panel title="开封检验" subtitle="用紧封的数据评估一个成果。策略跑一次回测，因子和模型算 IC，组合方案做滚动评估。">
          {!h.sealed ? <div className="hint">先设置紧封测试集。</div> : (
            <>
              <div className="config-bar rd-form">
                <Field label="类型">
                  <select value={kind} onChange={(e) => setKind(e.target.value as UnsealRecord['kind'])}>
                    {UNSEAL_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
                  </select>
                </Field>
                <Field label="成果">
                  <select value={ref} onChange={(e) => setRef(e.target.value)}>
                    {options.length === 0 && <option value="">（没有可选的）</option>}
                    {options.map((o) => <option key={o.ref} value={o.ref}>{o.label}{opened.has(`${kind}:${o.ref}`) ? '（已开封）' : ''}</option>)}
                  </select>
                </Field>
              </div>
              <div className="button-row">
                <button className="button primary" disabled={busy || !ref} onClick={unseal}>{busy ? '正在评估…' : '开封检验'}</button>
              </div>
            </>
          )}
        </Panel>
      </div>

      <Panel flush title={`开封记录（${h.records.length}）`} subtitle="研究阶段的成绩和紧封数据上的成绩放在一起看：差得越多，研究阶段过拟合越严重。">
        {h.records.length === 0 ? <EmptyState glyph="⚖" title="还没有开封过">研究做完后，在上面选一个成果开封检验。</EmptyState> : (
          <div className="table-wrap">
            <table className="fx-table">
              <thead><tr><th>成果</th><th>开封人</th><th>时间</th><th className="right">紧封数据上</th><th className="right">研究阶段</th><th /></tr></thead>
              <tbody>{h.records.map((r) => (
                <tr key={r.id}>
                  <td><strong>{r.name}</strong><span className="ml-sub">{String(r.result.label ?? r.kind)} · 紧封线 {day(r.cutoff)}</span></td>
                  <td>{r.user || '—'}</td>
                  <td className="muted">{when(r.at)}</td>
                  <td className="right num">{resultMain(r)}</td>
                  <td className="right num muted">{resultBase(r)}</td>
                  <td className="right">{r.kind === 'strategy' && r.result.bt_id ? <button className="button small ghost" onClick={() => onNavigate(`backtest/${r.result.bt_id}`)}>回测</button> : null}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Panel>

      {h.log.length > 0 && (
        <Panel title="设置留痕">
          <ul className="plain-list">
            {h.log.map((l) => (
              <li key={l.id}><div className="row-between"><b>{LOG_TEXT[l.action]}</b><span className="muted small">{when(l.at)}</span></div>
                <small>{l.user || '—'}{l.cutoff ? ` · 紧封线 ${day(l.cutoff)}` : ''}{l.previous ? `（原 ${day(l.previous)}）` : ''}{l.note ? ` · ${l.note}` : ''}</small></li>
            ))}
          </ul>
        </Panel>
      )}
    </>
  )
}

const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
function resultMain(r: UnsealRecord) {
  const x = r.result
  if (r.kind === 'strategy') return `夏普 ${f3(num(x.sharpe), 2)} · 收益 ${fp(num(x.total_return), 1)}`
  if (r.kind === 'portfolio') return `夏普 ${f3(num(x.sharpe), 2)} · 回撤 ${fp(num(x.max_drawdown), 1)}`
  return `IC ${f3(num(x.rank_ic), 3)} · t ${f3(num(x.ic_t), 2)}`
}
function resultBase(r: UnsealRecord) {
  const x = r.result
  if (r.kind === 'portfolio') return num(x.research_sharpe) !== null ? `夏普 ${f3(num(x.research_sharpe), 2)}` : '—'
  if (r.kind === 'factor' || r.kind === 'model') return num(x.research_rank_ic) !== null ? `IC ${f3(num(x.research_rank_ic), 3)}` : '—'
  return '—'
}

/* ------------------------------------------------------------------ */
/* 血缘图                                                               */
/* ------------------------------------------------------------------ */
const NW = 150, NH = 30, GX = 46, GY = 10, TOP = 30

function LineageView({ notify, onNavigate }: Pick<PageProps, 'notify' | 'onNavigate'>) {
  const [g, setG] = useState<Lineage | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  useEffect(() => { rd.lineage().then(setG).catch((e: Error) => notify(e.message, true)) }, [notify])

  const layout = useMemo(() => {
    if (!g) return null
    const cols = g.layers.filter((l) => g.nodes.some((n) => n.layer === l.id))
    const pos: Record<string, { x: number; y: number }> = {}
    let rows = 0
    cols.forEach((l, ci) => {
      g.nodes.filter((n) => n.layer === l.id).forEach((n, ri) => { pos[n.id] = { x: ci * (NW + GX), y: TOP + ri * (NH + GY) }; rows = Math.max(rows, ri + 1) })
    })
    return { cols, pos, W: cols.length * (NW + GX) - GX, H: TOP + rows * (NH + GY) }
  }, [g])

  const trace = useMemo(() => {
    if (!g || !sel) return null
    const walk = (start: string, dir: 'up' | 'down') => {
      const seen = new Set<string>(); const stack = [start]
      while (stack.length) {
        const cur = stack.pop()!
        for (const e of g.edges) {
          const nxt = dir === 'up' ? (e.to === cur ? e.from : null) : (e.from === cur ? e.to : null)
          if (nxt && !seen.has(nxt)) { seen.add(nxt); stack.push(nxt) }
        }
      }
      return seen
    }
    const up = walk(sel, 'up'), down = walk(sel, 'down')
    return { up, down, all: new Set([sel, ...up, ...down]) }
  }, [g, sel])

  if (!g || !layout) return <Loading label="正在梳理依赖关系" />
  if (g.nodes.length <= 1) return <EmptyState glyph="⇉" title="还没有可追踪的成果">保存因子、模型、组合方案或策略后，这里会画出它们之间的依赖。</EmptyState>
  const byId = Object.fromEntries(g.nodes.map((n) => [n.id, n]))
  const node = sel ? byId[sel] : null

  return (
    <>
      {g.stale.length > 0 && (
        <Callout tone="warn" title={`${g.stale.length} 个成果的上游更新过，结论可能已经过时`}>
          <ul className="rd-stale">
            {g.stale.slice(0, 8).map((s) => (
              <li key={s.id}><button className="link" onClick={() => setSel(s.id)}>{s.label}</button>
                <span className="muted">（{g.layers.find((l) => l.id === s.layer)?.label}）</span> 在
                {s.because.map((b) => ` ${b.label}`).join('、')} 更新之前评估，建议重跑。</li>
            ))}
          </ul>
        </Callout>
      )}
      <Panel title="依赖关系" subtitle="从左到右：行情与数据 → 因子 → 模型 → 组合 / 执行方案 → 策略 → 回测。点一个节点，高亮它的上下游；橙色边框表示上游比它新、需要重跑。">
        <div className="rd-dag-wrap">
          <svg className="rd-dag" width={layout.W} height={layout.H} viewBox={`0 0 ${layout.W} ${layout.H}`} role="img" aria-label="血缘图">
            {layout.cols.map((l, ci) => (
              <text key={l.id} className="chart-label" x={ci * (NW + GX)} y={14}>{l.label}</text>
            ))}
            {g.edges.map((e, i) => {
              const a = layout.pos[e.from], b = layout.pos[e.to]
              if (!a || !b) return null
              const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2
              const on = trace ? trace.all.has(e.from) && trace.all.has(e.to) : false
              return <path key={i} d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`} className={`rd-edge${on ? ' on' : ''}${trace && !on ? ' dim' : ''}`}><title>{e.how}</title></path>
            })}
            {g.nodes.map((n) => {
              const p = layout.pos[n.id]
              if (!p) return null
              const cls = ['rd-node', n.stale ? 'stale' : '', n.missing?.length ? 'missing' : '', sel === n.id ? 'sel' : '',
                trace && !trace.all.has(n.id) ? 'dim' : '', n.status === 'active' ? 'active' : ''].filter(Boolean).join(' ')
              return (
                <g key={n.id} className={cls} transform={`translate(${p.x},${p.y})`} onClick={() => setSel(sel === n.id ? null : n.id)} role="button" tabIndex={0}
                  onKeyDown={(ev) => { if (ev.key === 'Enter') setSel(sel === n.id ? null : n.id) }}>
                  <rect width={NW} height={NH} rx={6} />
                  <text x={10} y={NH / 2 + 4}>{n.label.length > 13 ? `${n.label.slice(0, 12)}…` : n.label}</text>
                  <title>{n.label}{n.detail ? `\n${n.detail}` : ''}</title>
                </g>
              )
            })}
          </svg>
        </div>
      </Panel>
      {node && trace && (
        <Panel title={node.label} subtitle={node.detail || undefined} actions={<>
          {node.route && <button className="button small" onClick={() => onNavigate(node.route!)}>打开</button>}
          <button className="button small ghost" onClick={() => setSel(null)}>取消选择</button>
        </>}>
          <div className="fx-meta">
            <span>{g.layers.find((l) => l.id === node.layer)?.label}{node.status ? ` · ${node.status}` : ''}{node.updated_at ? ` · 更新于 ${when(node.updated_at)}` : ''}</span>
            {node.stale && <span><Tag tone="warn">需要重跑</Tag></span>}
          </div>
          {node.missing?.length ? <Callout tone="warn" title="引用了不存在的成果">{node.missing.join('、')}：在库里找不到同名的因子 / 模型 / 方案，运行时会报错。</Callout> : null}
          <div className="rd-trace">
            <div><h3 className="rd-sub">上游（{trace.up.size}）</h3>{[...trace.up].map((id) => <TraceItem key={id} n={byId[id]} onPick={setSel} />)}</div>
            <div><h3 className="rd-sub">下游（{trace.down.size}）</h3>{trace.down.size ? [...trace.down].map((id) => <TraceItem key={id} n={byId[id]} onPick={setSel} />) : <div className="hint">没有成果用到它。</div>}</div>
          </div>
        </Panel>
      )}
    </>
  )
}

function TraceItem({ n, onPick }: { n?: LNode; onPick: (id: string) => void }) {
  if (!n) return null
  return <button className={`rd-chip${n.stale ? ' stale' : ''}`} onClick={() => onPick(n.id)}>{n.label}</button>
}
