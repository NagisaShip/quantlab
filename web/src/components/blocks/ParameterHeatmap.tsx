import { HeatmapChart } from './Chart'
import { fmtValue } from './protocol'

/** 参数热力图的数据：来自 parameter_heatmap 工具或 ql:parameter_heatmap 块。 */
export interface ParameterHeatmapData {
  title?: string
  x_param?: string
  y_param?: string
  x?: (number | string)[]
  y?: (number | string)[]
  z?: (number | null)[][]
  metric?: string
  metric_label?: string
  format?: string
  best?: { x?: number | string; y?: number | string; value?: number | null }
  robustness?: { verdict?: string; note?: string; neighbor_mean?: number | null; plateau_share?: number | null }
  status?: 'running' | 'done' | 'error'
  total?: number
  error?: string
  [k: string]: unknown
}

const VERDICT: Record<string, { label: string; tone: string }> = {
  plateau: { label: '稳健平原', tone: 'ok' },
  spike: { label: '孤岛尖峰', tone: 'warn' },
  mixed: { label: '过渡区', tone: 'info' },
  fragile: { label: '较脆弱', tone: 'warn' },
  unavailable: { label: '无结果', tone: 'muted' },
}

function bestOf(d: ParameterHeatmapData) {
  if (d.best?.x !== undefined && d.best?.y !== undefined) return d.best
  const xs = d.x || []
  const ys = d.y || []
  const z = d.z || []
  let best: { x?: number | string; y?: number | string; value?: number | null } = {}
  const lower = d.metric === 'max_drawdown'
  let found: number | null = null
  ys.forEach((yy, i) => xs.forEach((xx, j) => {
    const v = z[i]?.[j]
    if (typeof v !== 'number' || !Number.isFinite(v)) return
    if (found === null || (lower ? v < found : v > found)) {
      found = v
      best = { x: xx, y: yy, value: v }
    }
  }))
  return best
}

export function ParameterHeatmapBody({ data }: { data: ParameterHeatmapData }) {
  const xs = data.x || []
  const ys = data.y || []
  const z = data.z || []
  const best = bestOf(data)
  const verdict = VERDICT[String(data.robustness?.verdict || '')] || null
  const metric = data.metric_label || data.metric || '指标'
  return (
    <>
      <div className="qb-param-meta">
        <span><i className="qb-param-axis x" />X：{data.x_param || '参数 X'}</span>
        <span><i className="qb-param-axis y" />Y：{data.y_param || '参数 Y'}</span>
        <span>颜色：{metric}</span>
        {best?.x !== undefined && best?.y !== undefined && (
          <span className="qb-param-best">
            最佳 {data.x_param || 'X'}={String(best.x)} · {data.y_param || 'Y'}={String(best.y)}
            {best.value !== undefined && best.value !== null ? ` · ${fmtValue(best.value, data.format || 'ratio', 3)}` : ''}
          </span>
        )}
        {verdict && <span className={`qb-param-verdict ${verdict.tone}`}>{verdict.label}</span>}
      </div>
      {data.robustness?.note && <p className="qb-param-note">{data.robustness.note}</p>}
      <HeatmapChart spec={{
        kind: 'heatmap',
        x: xs,
        y: ys,
        z,
        y_format: data.format || 'ratio',
        x_label: data.x_param,
        y_label: data.y_param,
        color_label: metric,
        best: { x: best.x, y: best.y },
      }} />
    </>
  )
}

/** 对话工具卡片：参数热力图（strategy lab 的 tool 消息直接用这个）。 */
export function ParameterHeatmapCard({ data }: { data: ParameterHeatmapData }) {
  const running = data.status === 'running'
  const failed = data.status === 'error'
  return (
    <section className={`qb-block qb-t-parameter_heatmap wide anim-rise${running ? ' qb-running' : ''}`}>
      <header className="qb-head">
        <span className="qb-kind">参数热力图</span>
        <b className="qb-title">{data.title || '参数敏感度'}</b>
        {running && <span className="cb-badge">进行中</span>}
        {failed && <span className="cb-badge warn">失败</span>}
      </header>
      {running ? (
        <div className="qb-pending">
          <span className="cb-spin" />
          正在并行回测 {data.total || '多'} 组参数组合（{data.x_param || 'X'} × {data.y_param || 'Y'}）…
        </div>
      ) : failed ? (
        <div className="qb-callout tone-warn">{data.error || '参数热力图执行失败'}</div>
      ) : (
        <ParameterHeatmapBody data={data} />
      )}
    </section>
  )
}
