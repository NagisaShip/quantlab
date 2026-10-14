/**
 * 模型训练报告的展示部件：指标条、学习曲线、滚动分段、特征重要性、漂移、校准、模型卡、在策略里使用。
 * AI 研究室的报告卡片（紧凑）、模型库详情与训练台（完整）共用。
 */
import { useState } from 'react'
import type { CurvePoint, FoldRow, ModelCard, ModelReport, ModelSummary } from '../apiModels'
import { Checks, DecayAndGroups, f3, fp } from './factorReport'
import { LineChart } from './charts2'

export const MODEL_STATUS_TONE: Record<string, string> = { candidate: '', active: 'ok', decaying: 'warn', retired: 'muted' }
const day = (ms?: number | null) => (ms ? new Date(ms).toISOString().slice(0, 10) : '—')

export const isVol = (s?: Partial<ModelSummary> | null) => s?.label === 'volatility' || s?.mode === 'volatility'

/** 一行关键指标：收益类模型看样本外 RankIC 与基线，波动率模型看预测相关与 QLIKE。 */
export function ModelStats({ s, compact }: { s: Partial<ModelSummary> | null | undefined; compact?: boolean }) {
  if (!s) return null
  const vol = isVol(s)
  const beat = s.beat_baseline ?? null
  const cells: [string, string, string?, string?][] = vol ? [
    ['预测相关', f3(s.oos_rank_ic), (s.oos_rank_ic ?? 0) >= 0 ? 'up' : 'down', '样本外：预测波动与实际波动的秩相关'],
    ['超出基线', f3(beat), beat === null ? undefined : beat > 0 ? 'ok' : 'bad', '基线是“用过去的已实现波动直接当预测”'],
    ['QLIKE', f3(s.qlike), undefined, '波动率预测的常用损失，越低越好'],
    ['各段为正', fp(s.fold_positive), (s.fold_positive ?? 0) >= 0.75 ? 'ok' : 'bad', '滚动分段里相关为正的比例'],
    ['样本', s.samples ? String(s.samples) : '—'],
    ['泄漏检查', s.leak_passed === undefined || s.leak_passed === null ? '—' : s.leak_passed ? '通过' : '未通过', s.leak_passed ? 'ok' : 'bad'],
  ] : [
    [compact ? '样本外 IC' : '样本外 RankIC', f3(s.oos_rank_ic), (s.oos_rank_ic ?? 0) >= 0 ? 'up' : 'down', '滚动训练后，每段只在没见过的数据上预测，再算与未来收益的秩相关'],
    ['超出基线', f3(beat), beat === null ? undefined : beat > 0 ? 'ok' : 'bad', `基线 RankIC ${f3(s.baseline_rank_ic)}`],
    [compact ? '方向' : '方向准确率', fp(s.dir_acc, 1), (s.dir_acc ?? 0) > 0.5 ? 'up' : undefined, '预测方向与实际方向一致的比例'],
    ['多空夏普', f3(s.ls_sharpe, 2), (s.ls_sharpe ?? 0) >= 0 ? 'up' : 'down', '按预测排序做多空，扣成本'],
    ['换手', fp(s.turnover), undefined, '每次调仓的组合权重变化'],
    ['各段为正', fp(s.fold_positive), (s.fold_positive ?? 0) >= 0.75 ? 'ok' : 'bad', '滚动分段里 RankIC 为正的比例'],
  ]
  if (!compact && !vol) {
    cells.push(['ICIR', f3(s.oos_icir, 2), undefined, 'IC 均值 / IC 标准差'])
    if (s.brier !== undefined && s.brier !== null) cells.push(['Brier', f3(s.brier), undefined, '分类概率的均方误差，越低越好'])
  }
  return (
    <div className={`fr-stats${compact ? ' compact' : ''}`}>
      {cells.map(([k, v, t, tip]) => <div key={k} title={tip}><span>{k}</span><b className={t}>{v}</b></div>)}
    </div>
  )
}

export function CurveChart({ curve, height = 180 }: { curve: CurvePoint[]; height?: number }) {
  if (!curve || curve.length < 2) return <div className="hint">这个模型没有迭代过程（闭式解或逐个估计），没有学习曲线。</div>
  return (
    <LineChart series={[{ name: '训练集', values: curve.map((c) => c.train) }, { name: '验证集', values: curve.map((c) => c.val), dashed: true }]}
      timestamps={curve.map((c) => c.epoch)} xLabels={curve.map((c) => `第 ${c.epoch} 轮`)} format="num" height={height} width={560} zeroLine={false}
      ariaLabel="学习曲线（最后一段）" />
  )
}

export function FoldsTable({ folds }: { folds: FoldRow[] }) {
  return (
    <table className="fr-table">
      <thead><tr><th>段</th><th>测试区间</th><th className="right">训练</th><th className="right">测试</th><th className="right">RankIC</th></tr></thead>
      <tbody>{folds.map((f) => (
        <tr key={f.fold}>
          <td>{f.fold}</td><td className="muted">{day(f.start)} ~ {day(f.end)}</td>
          <td className="right num">{f.train}</td><td className="right num">{f.test}</td>
          <td className={`right num ${(f.rank_ic ?? 0) >= 0 ? 'positive' : 'negative'}`}>{f3(f.rank_ic)}</td>
        </tr>
      ))}</tbody>
    </table>
  )
}

/** 横向条形：特征重要性（打乱该特征后 RankIC 下降多少）或 PSI。 */
export function HBars({ rows, fmt, warnAt, empty }: { rows: { name: string; v: number | null }[]; fmt: (v: number) => string; warnAt?: number; empty?: string }) {
  const vals = rows.map((r) => r.v ?? 0)
  const max = Math.max(...vals.map(Math.abs), 1e-9)
  if (!rows.length) return <div className="hint">{empty ?? '暂无'}</div>
  return (
    <div className="mr-hbars">
      {rows.map((r) => {
        const v = r.v ?? 0
        const bad = warnAt !== undefined ? v >= warnAt : v < 0
        return (
          <div key={r.name} className="mr-hbar">
            <span className="mr-hbar-name" title={r.name}>{r.name}</span>
            <span className="mr-hbar-track"><i className={bad ? 'bad' : ''} style={{ width: `${(Math.abs(v) / max) * 100}%` }} /></span>
            <b className="num">{r.v === null ? '—' : fmt(v)}</b>
          </div>
        )
      })}
    </div>
  )
}

export function Calibration({ rows }: { rows: { bin: number; pred: number; actual: number; n: number }[] }) {
  return (
    <table className="fr-table">
      <thead><tr><th>预测概率</th><th className="right">平均预测</th><th className="right">实际上涨比例</th><th className="right">样本</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.bin}><td>{r.bin.toFixed(2)}</td><td className="right num">{fp(r.pred, 1)}</td>
          <td className={`right num${Math.abs(r.actual - r.pred) > 0.1 ? ' negative' : ''}`}>{fp(r.actual, 1)}</td><td className="right num">{r.n}</td></tr>
      ))}</tbody>
    </table>
  )
}

export function ModelCardView({ card }: { card: ModelCard }) {
  return (
    <dl className="mr-card">
      <dt>用途</dt><dd>{card.purpose}</dd>
      <dt>模型</dt><dd>{card.model}{Object.keys(card.params || {}).length > 0 && <code className="mr-params">{Object.entries(card.params).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' ')}</code>}</dd>
      <dt>数据</dt><dd>{card.data}</dd>
      <dt>特征</dt><dd>{card.features.join('、')}</dd>
      <dt>验证</dt><dd>{card.validation}</dd>
      {card.drivers.length > 0 && <><dt>主要驱动</dt><dd>{card.drivers.join('、')}</dd></>}
      <dt>已知弱点</dt><dd><ul>{card.weaknesses.map((w) => <li key={w}>{w}</li>)}</ul></dd>
    </dl>
  )
}

/** 在策略里使用：代码片段 + 复制。 */
export function UseInStrategy({ code, note }: { code: string; note?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try { await navigator.clipboard.writeText(code); setCopied(true); window.setTimeout(() => setCopied(false), 1500) } catch { /* 无剪贴板权限时忽略 */ }
  }
  return (
    <div className="mr-use">
      <div className="mr-use-head"><span className="hint">{note ?? '复制到策略代码里即可使用；回测时平台按时间点预先算好，不会用到未来数据。'}</span>
        <button className="button small" onClick={copy}>{copied ? '已复制' : '复制'}</button></div>
      <pre className="mr-code">{code}</pre>
    </div>
  )
}

/** 完整报告（模型库详情、训练台）。 */
export function ModelFullReport({ report, card }: { report: ModelReport; card?: ModelCard | null }) {
  const s = report.summary
  const p = report.panel
  const vol = isVol(s)
  const ic = report.ic_series || []
  const nav = report.ls_nav || []
  const coef = report.coefficients ? Object.entries(report.coefficients).map(([name, v]) => ({ name, v })) : []
  return (
    <div className="fr-full">
      <ModelStats s={s} />
      <div className="fr-badges">
        <span className={`cb-badge ${report.admitted ? 'ok' : 'warn'}`}>{report.admitted ? '满足上线条件' : '未满足上线条件'}</span>
        <span className="cb-badge">基线：{report.baseline}</span>
        <span className="cb-badge">{s.samples} 个样本 · {s.features} 个特征 · 持有 {s.horizon} 期</span>
        {s.backend && <span className="cb-badge">{s.backend}</span>}
        {s.seconds !== undefined && <span className="cb-badge">用时 {s.seconds} 秒</span>}
        {p?.synthetic && <span className="cb-badge warn">含合成数据</span>}
      </div>
      {(p?.warnings || []).length > 0 && <ul className="fr-warn">{p.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
      <div className="fr-grid">
        <section>
          <h4>泄漏与质量检查</h4>
          <Checks checks={report.checks} />
        </section>
        <section>
          <h4>滚动样本外分段</h4>
          <FoldsTable folds={report.folds} />
        </section>
      </div>
      <div className="fr-grid">
        <section>
          <h4>学习曲线（最后一段，越低越好）</h4>
          <CurveChart curve={report.curve} />
        </section>
        <section>
          <h4>特征重要性（打乱后 RankIC 下降）</h4>
          <HBars rows={report.importance.map((x) => ({ name: x.name, v: x.drop }))} fmt={(v) => v.toFixed(3)} />
          {coef.length > 0 && <>
            <h4 style={{ marginTop: 14 }}>线性系数</h4>
            <HBars rows={coef} fmt={(v) => v.toFixed(3)} />
          </>}
        </section>
      </div>
      {!vol && ic.length > 2 && (
        <section>
          <h4>样本外累计 RankIC</h4>
          <LineChart series={[{ name: '累计 RankIC', values: ic.map((x) => x.cum), area: true }]} timestamps={ic.map((x) => x.t)}
            format="num" height={200} zeroLine ariaLabel="样本外累计 RankIC" />
        </section>
      )}
      {!vol && nav.length > 2 && (
        <section>
          <h4>按预测排序的多空组合净值（扣成本）</h4>
          <LineChart series={[{ name: '多空', values: nav.map((x) => x.nav - 1) }]} timestamps={nav.map((x) => x.t)} format="pct" height={200} ariaLabel="多空净值" />
        </section>
      )}
      <div className="fr-grid">
        {!vol && (report.decay?.length || report.groups?.mean?.length) ? (
          <section>
            <h4>衰减与分组</h4>
            <DecayAndGroups report={{ decay: report.decay || [], groups: report.groups || { n: 0, mean: [] } }} />
          </section>
        ) : null}
        <section>
          <h4>特征漂移（PSI，近期 vs 训练期）</h4>
          <HBars rows={report.psi.map((x) => ({ name: x.name, v: x.psi }))} fmt={(v) => v.toFixed(3)} warnAt={0.25} />
          <div className="hint">PSI &gt; 0.25 表示这个特征最近的分布和训练时明显不同。</div>
        </section>
        {report.regimes && report.regimes.length > 0 && (
          <section>
            <h4>分市场状态</h4>
            <table className="fr-table">
              <thead><tr><th>状态</th><th className="right">RankIC</th><th className="right">期数</th></tr></thead>
              <tbody>{report.regimes.map((r) => (
                <tr key={r.regime}><td>{r.regime}</td><td className={`right num ${(r.rank_ic ?? 0) >= 0 ? 'positive' : 'negative'}`}>{f3(r.rank_ic)}</td><td className="right num">{r.periods}</td></tr>
              ))}</tbody>
            </table>
          </section>
        )}
        {report.calibration && report.calibration.length > 0 && (
          <section>
            <h4>概率校准</h4>
            <Calibration rows={report.calibration} />
          </section>
        )}
      </div>
      {card && (
        <section>
          <h4>模型卡</h4>
          <ModelCardView card={card} />
        </section>
      )}
    </div>
  )
}
