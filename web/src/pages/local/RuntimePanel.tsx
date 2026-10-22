import { useLocal } from './context'
import { RUN_LABEL, RUN_TONE, size } from './format'
import { Pill, Section, Stat } from './shared'

const ACTIVE_RUN_STATES = ['QUEUED', 'RUNNING', 'PAUSED', 'RECOVERING']

/** 设置 · 任务与资源：CPU / 内存 / 磁盘 / GPU、活动任务、缓存清理。 */
export default function RuntimePanel() {
  const s = useLocal()
  const resource = s.resource
  const memory = resource?.memory
  const memoryPercent = memory && memory.total ? Math.round((memory.used / memory.total) * 100) : null
  const locked = s.busy !== ''

  return (
    <div className="lp-stack">
      <Section
        title="资源占用"
        note="每 5 秒自动刷新；CPU 与内存需要服务端安装 psutil，GPU 需要 nvidia-smi。"
        actions={<button type="button" className="button small" disabled={locked} onClick={() => void s.refreshRuntime()}>刷新</button>}
      >
        <div className="lp-stats resource">
          <Stat
            label="CPU"
            value={resource?.cpu_percent == null ? '待检测' : `${resource.cpu_percent}%`}
            hint={resource?.cpu_percent == null ? '未安装 psutil' : '服务所在机器'}
          />
          <Stat
            label="磁盘可用"
            value={typeof resource?.disk_free === 'number' ? size(resource.disk_free) : '—'}
            hint="项目目录所在磁盘"
          />
          <Stat label="活动任务" value={String(resource?.active_runs ?? 0)} hint="运行中的实验与研究对话" />
          <Stat
            label="内存"
            value={memory ? size(memory.used) : '—'}
            hint={memory ? `共 ${size(memory.total)}${memoryPercent == null ? '' : ` · ${memoryPercent}%`}` : '未安装 psutil'}
          />
        </div>
        {resource?.resource_note && <p className="hint">{resource.resource_note}</p>}
        <details className="lp-more">
          <summary>内存、GPU 与进程详情</summary>
          <div className="lp-stack tight">
            {resource?.process && (
              <p className="hint">
                服务进程 PID {resource.process.pid} · RSS {size(resource.process.rss)} · 子进程 {resource.process.children}
              </p>
            )}
            {resource?.gpu?.length ? (
              <div className="lp-table-wrap">
                <table className="lp-preview-table">
                  <thead><tr><th>GPU</th><th>已用</th><th>总量</th><th>利用率</th></tr></thead>
                  <tbody>
                    {resource.gpu.map((gpu, index) => (
                      <tr key={index}>
                        <td>{gpu[0] ?? '—'}</td>
                        <td>{gpu[1] ?? '—'} MB</td>
                        <td>{gpu[2] ?? '—'} MB</td>
                        <td>{gpu[3] ?? '—'}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="hint">没有检测到 NVIDIA GPU（或服务端没有 nvidia-smi）。</p>
            )}
            <pre className="lp-pre">{JSON.stringify(resource ?? {}, null, 2)}</pre>
          </div>
        </details>
      </Section>

      <div className="lp-grid">
        <Section title="最近任务" note="可以取消仍在排队或运行的任务；运行中的研究对话在 AI 研究室里继续。">
          {s.tasks.runs.length === 0 ? (
            <p className="lp-empty">{s.loading ? '正在读取任务…' : '最近没有任务。运行研究工作流后，任务会出现在这里。'}</p>
          ) : (
            <div className="lp-list">
              {s.tasks.runs.map((run) => (
                <div className="lp-list-row" key={run.id}>
                  <div className="lp-list-main">
                    <b>{run.title || run.id}</b>
                    <small>{run.kind} · {run.id}</small>
                  </div>
                  <Pill tone={RUN_TONE[run.state]}>{RUN_LABEL[run.state] ?? run.state}</Pill>
                  {ACTIVE_RUN_STATES.includes(run.state) && (
                    <button type="button" className="button small" disabled={locked} onClick={() => void s.cancelRun(run.id)}>
                      取消
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          {s.tasks.conversations.length > 0 && (
            <div className="lp-block">
              <div className="lp-block-head"><b>运行中的研究对话</b></div>
              <div className="lp-list">
                {s.tasks.conversations.map((conversation) => (
                  <div className="lp-list-row" key={conversation.id}>
                    <div className="lp-list-main">
                      <b>{conversation.title || conversation.id}</b>
                      <small>{conversation.status}</small>
                    </div>
                    <button type="button" className="button small" onClick={() => s.onNavigate(`lab/${conversation.id}`)}>
                      查看对话任务
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Section>

        <Section title="可重建缓存" note="只清理节点计算缓存；运行证据、输入和图表文件保留，后续运行可能重新计算。">
          {s.cache ? (
            <>
              <div className="lp-stats tight">
                <Stat label="缓存条目" value={String(s.cache.entries)} />
                <Stat label="占用空间" value={size(s.cache.bytes)} />
                <Stat label="活动任务" value={String(s.cache.active_runs ?? 0)} />
              </div>
              <p className="hint">{s.cache.effect}</p>
            </>
          ) : (
            <p className="lp-loading">{s.loading ? '正在读取缓存信息…' : '缓存信息不可用。'}</p>
          )}
          <div className="lp-inline">
            <button type="button" className="button" disabled={locked} onClick={() => void s.clearCache()}>
              清理可重建缓存
            </button>
          </div>
        </Section>
      </div>
    </div>
  )
}
