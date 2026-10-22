import { Callout } from '../../components/ui'
import { useLocal } from './context'
import { JobStatus, PathCode, Pill, Section, Stat } from './shared'

/** 设置 · 研究执行环境：检查真实解释器、发现并切换本机 Python、创建独立环境。 */
export default function EnvironmentPanel() {
  const s = useLocal()
  const env = s.env
  const locked = s.busy !== ''
  const canEdit = s.canEdit

  return (
    <div className="lp-stack">
      {!s.isLocal && (
        <Callout tone="info" title="账号模式">
          这里显示服务器实际使用的执行环境。环境切换与修复只对部署管理员开放，普通成员可以查看依赖检查结果。
        </Callout>
      )}

      <Section
        title="Python 环境"
        note={s.isLocal
          ? '研究卡片和图表预览在当前项目选定的解释器里运行；切换不会删除旧环境。'
          : '服务端执行环境；历史运行记录会冻结当时的解释器与依赖。'}
        actions={<button type="button" className="button small" disabled={locked} onClick={() => void s.refreshEnv()}>重新检查</button>}
      >
        {s.envLoading && !env ? (
          <p className="lp-loading" role="status">正在检查实际解释器与依赖…</p>
        ) : s.envError ? (
          <Callout tone="warn" title="环境检查失败">{s.envError}</Callout>
        ) : env ? (
          <div className="lp-stack tight">
            <div className="lp-env-status">
              <span className={`lp-status-dot${env.ready ? ' ok' : ' warn'}`} aria-hidden />
              <div>
                <b>{env.ready ? '研究依赖就绪' : '需要修复研究依赖'}</b>
                <small>Python {env.version} · {env.ready ? '全部依赖检查通过' : '部分依赖缺失或版本不符'}</small>
              </div>
              <Pill tone={env.ready ? 'ok' : 'warn'}>{env.ready ? '可用' : '待修复'}</Pill>
            </div>
            <PathCode label="当前研究解释器" value={env.python} />
            <p className="hint">服务解释器：<code className="lp-code-inline">{env.server_python}</code></p>
            <div className="lp-checks" role="list" aria-label="依赖检查结果">
              {env.checks.map((check) => (
                <span
                  key={check.requirement}
                  role="listitem"
                  className={`lp-check${check.ok ? ' ok' : ' fail'}`}
                  title={check.message ?? check.version}
                >
                  {check.ok ? '✓' : '✕'} {check.requirement}{check.version ? ` ${check.version}` : ''}
                </span>
              ))}
            </div>
            <p className="hint">{env.note}</p>
          </div>
        ) : (
          <p className="lp-loading">等待环境检查…</p>
        )}
      </Section>

      <div className="lp-grid">
        <Section
          title={s.isLocal ? '发现本机 Python' : '发现可用 Python'}
          note={s.isLocal
            ? '只读扫描常见解释器位置，不改动系统环境；选中一个路径后再检查并切换。'
            : '只读扫描服务器上常见的解释器位置，不改动系统环境；选中一个路径后再检查并切换。'}
        >
          <div className="lp-inline">
            <button
              type="button"
              className="button"
              disabled={!canEdit || locked}
              onClick={() => void s.discoverEnvironments()}
            >
              {s.isLocal ? '检测本机 Python / venv / Conda' : '检测服务器上的 Python / venv / Conda'}
            </button>
            {!canEdit && <small className="hint">仅部署管理员可以更改执行环境。</small>}
          </div>

          {s.environments.length > 0 && (
            <div className="lp-envs" role="list" aria-label="发现的 Python 解释器">
              {s.environments.map((candidate) => (
                <button
                  key={candidate.python}
                  type="button"
                  role="listitem"
                  className={`lp-env-row${candidate.python === s.python ? ' active' : ''}`}
                  disabled={!canEdit || !candidate.ok}
                  title={candidate.message ?? candidate.python}
                  onClick={() => s.setPython(candidate.python)}
                >
                  <span className="lp-env-path">{candidate.python}</span>
                  <span className={`lp-pill${candidate.ok ? ' ok' : ' fail'}`}>
                    {candidate.ok ? (candidate.version ?? '可用') : '不可用'}
                  </span>
                </button>
              ))}
            </div>
          )}

          <div className="lp-field">
            <span>要切换到的 Python 解释器</span>
            <input
              aria-label="Python 解释器路径"
              value={s.python}
              onChange={(e) => s.setPython(e.target.value)}
              placeholder="粘贴 python.exe 绝对路径；留空则由服务 Python 创建独立环境"
              disabled={!canEdit}
            />
            <small>会先做版本与依赖兼容检查，通过后才切换；旧环境与历史运行记录保留。</small>
          </div>
          <div className="lp-inline">
            <button type="button" className="button" disabled={!canEdit || locked || !s.python.trim()} onClick={() => void s.selectEnvironment()}>
              检查并切换
            </button>
          </div>
        </Section>

        <Section
          title="创建独立研究环境"
          note="新建独立 venv 并安装研究依赖，检查通过后自动切换；不改动系统 Python。"
        >
          <div className="lp-field">
            <span>基础解释器</span>
            <input
              aria-label="创建环境的基础 Python 解释器"
              value={s.python}
              onChange={(e) => s.setPython(e.target.value)}
              placeholder="留空使用服务 Python"
              disabled={!canEdit}
            />
            <small>和左侧共用；可以粘贴某个 venv / Conda 里的 Python 路径。</small>
          </div>
          <div className="lp-field">
            <span>离线 wheel 目录（可选）</span>
            <input
              aria-label="离线依赖目录"
              value={s.wheels}
              onChange={(e) => s.setWheels(e.target.value)}
              placeholder="包含 .whl 文件的本地目录；填写后离线安装"
              disabled={!canEdit}
            />
          </div>
          <label className="lp-checkline">
            <input type="checkbox" checked={s.neural} disabled={!canEdit} onChange={(e) => s.setNeural(e.target.checked)} />
            安装神经网络依赖（torch，体积较大）
          </label>
          <div className="lp-inline">
            <button
              type="button"
              className="button primary"
              disabled={!canEdit || locked || s.job?.state === 'running'}
              onClick={() => void s.repairEnvironment()}
            >
              创建并修复研究环境
            </button>
          </div>
          <p className="hint">安装过程可能持续几分钟；日志会显示在下方管理任务里，完成后自动刷新。</p>
          <div className="lp-stats tight">
            <Stat label="研究依赖" value={env ? (env.ready ? '已就绪' : '待修复') : '—'} />
            <Stat label="基础解释器" value={s.python.trim() ? '自定义' : '服务 Python'} />
            <Stat label="神经网络" value={s.neural ? '包含' : '不包含'} />
          </div>
        </Section>
      </div>

      {s.job && s.job.kind === 'environment-install' && (
        <Section title="管理任务" note="环境安装任务在后台运行；关闭设置页不会中断。">
          <JobStatus job={s.job} />
        </Section>
      )}
    </div>
  )
}
