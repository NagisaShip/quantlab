import { useState } from 'react'
import { Callout } from '../../components/ui'
import { useLocal } from './context'
import { JobStatus, PathCode, Section } from './shared'

/** 设置 · 本地项目：当前项目、切换到其他文件夹、导出与关窗偏好、真实交易能力。 */
export default function ProjectPanel() {
  const s = useLocal()
  const info = s.info
  const locked = s.busy !== ''
  const [confirmReal, setConfirmReal] = useState(false)

  return (
    <Section
      title="当前项目"
      note="文件、环境与运行证据都归属当前项目；本地模式下账号与协作功能停用。"
      actions={
        <button type="button" className="button small" disabled={locked} onClick={() => void s.refreshAll()}>
          刷新
        </button>
      }
    >
      {!info ? (
        <p className="lp-loading" role="status">
          {s.loading ? '正在读取项目信息…' : '还没有读到项目信息，可以刷新重试。'}
        </p>
      ) : (
        <div className="lp-stack tight">
          <div className="lp-identity">
            <div className="lp-identity-main">
              <b>{info.project.name}</b>
              <small>项目 ID {info.project.id.slice(0, 8)} · 本地模式</small>
            </div>
            <PathCode label="项目目录" value={info.root} />
          </div>

          <div className="lp-block">
            <div className="lp-block-head"><b>在其他文件夹打开</b></div>
            <div className="lp-field">
              <span>其他项目文件夹</span>
              <div className="lp-inline">
                <input
                  aria-label="其他项目文件夹"
                  value={s.projectPath}
                  onChange={(e) => s.setProjectPath(e.target.value)}
                  placeholder={info.root}
                />
                <button type="button" className="button small" disabled={locked} onClick={() => void s.pickFolder('project')}>
                  选择文件夹
                </button>
              </div>
              <small>桌面版会打开系统选择器；浏览器版请粘贴本机绝对路径。</small>
            </div>
            <div className="lp-inline">
              <button
                type="button"
                className="button primary"
                disabled={locked || !s.projectPath.trim()}
                onClick={() => void s.openProject()}
              >
                打开为项目
              </button>
              {s.projectLink && (
                <a className="button small" href={s.projectLink} target="_blank" rel="noopener noreferrer">
                  打开新项目 ↗
                </a>
              )}
            </div>
            <p className="hint">打开新项目不会切换正在执行任务的数据库；重开当前目录可恢复草稿和历史记录。</p>
          </div>

          <div className="lp-block">
            <div className="lp-block-head"><b>导出与迁移</b></div>
            <div className="lp-inline">
              <button type="button" className="button" disabled={locked} onClick={() => void s.exportProject()}>
                导出项目快照
              </button>
              {s.exportLink && (
                <a className="button" href={s.exportLink}>
                  下载项目 ZIP
                </a>
              )}
            </div>
            <p className="hint">
              迁移包不含密钥、环境和模型服务配置；外部文件引用需在新位置重新授权或定位。关闭窗口与停止项目服务分别处理。
            </p>
            {s.job && s.job.kind === 'project-export' && <JobStatus job={s.job} />}
          </div>

          <div className="lp-field">
            <span>关闭窗口时</span>
            <select
              aria-label="关窗行为"
              value={info.preferences.close_action}
              disabled={locked}
              onChange={(e) => void s.saveCloseAction(e.target.value)}
            >
              <option value="ask">每次询问（隐藏到托盘 / 退出 / 取消）</option>
              <option value="background">隐藏到托盘继续运行（任务不中断）</option>
              <option value="exit">退出应用（有任务时先提示）</option>
            </select>
            <small>关闭窗口默认只隐藏窗口；退出应用才会停止本机服务。有任务时退出会先给出「隐藏 / 完成后退出 / 取消」。</small>
          </div>

          {s.realSpotAllowed && (
            <div className="lp-block">
              <div className="lp-block-head">
                <b>真实交易能力</b>
                <span className={`lp-pill${s.realSpot ? ' warn' : ''}`}>{s.realSpot ? '已开启' : '默认关闭'}</span>
              </div>
              <div className="lp-inline">
                <button
                  type="button"
                  role="switch"
                  aria-checked={s.realSpot === true}
                  aria-label="真实交易能力"
                  className={`switch${s.realSpot ? ' on' : ''}`}
                  disabled={locked || s.realSpotBusy || s.realSpot === null}
                  onClick={() => {
                    if (s.realSpot) void s.updateRealTrading(false)
                    else setConfirmReal(true)
                  }}
                >
                  <i />
                </button>
                <small>
                  {s.realSpot
                    ? '微实盘入口已显示；每次真实下单仍需输入确认码。'
                    : '开启后才会显示微实盘入口；关闭时服务端拒绝真实资金启动。'}
                </small>
              </div>
              {confirmReal && (
                <Callout tone="warn">
                  <b>开启前请确认</b>
                  <ul>
                    <li>真实资金会按策略在交易所真实成交，亏损由账户自行承担。</li>
                    <li>目前只支持 Binance / OKX 现货；真实资金永续仍然阻断。</li>
                    <li>服务端开关、密钥自检、资金上限与逐次确认码仍然生效。</li>
                  </ul>
                  <div className="lp-inline">
                    <button type="button" className="button" onClick={() => setConfirmReal(false)}>
                      取消
                    </button>
                    <button
                      type="button"
                      className="button primary"
                      disabled={locked || s.realSpotBusy}
                      onClick={() => {
                        setConfirmReal(false)
                        void s.updateRealTrading(true)
                      }}
                    >
                      我已了解风险，开启
                    </button>
                  </div>
                </Callout>
              )}
              <p className="hint">仅本地管理员可操作；账号模式由服务器管理员策略控制。</p>
            </div>
          )}
        </div>
      )}
    </Section>
  )
}
