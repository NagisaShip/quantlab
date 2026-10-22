import { Callout } from '../components/ui'
import { LocalContext } from './local/context'
import EnvironmentPanel from './local/EnvironmentPanel'
import FilesPanel from './local/FilesPanel'
import LibraryPanel from './local/LibraryPanel'
import ProjectPanel from './local/ProjectPanel'
import RuntimePanel from './local/RuntimePanel'
import { Section, ErrorAlert } from './local/shared'
import { useLocalProject } from './local/useLocalProject'
import type { PageProps } from './types'
import './localProject.css'

/**
 * 本地项目 / 研究执行环境。
 *
 * 现在作为设置页里的内嵌面板使用：只渲染内容区，不再有 h1 大页头、外层 max-width
 * 或自带页签；左侧导航、关闭按钮和页头标题都由 SettingsShell / SettingsPage 提供。
 *
 * tab：local（项目与文件）| local-env（研究环境）| local-runtime（任务与资源）
 *      | local-library（跨项目模块库）| env（账号模式下的研究执行环境）。
 */
const TABS = new Set(['local', 'local-env', 'local-runtime', 'local-library', 'env'])

export default function LocalProjectPage(props: PageProps & { tab?: string }) {
  const isLocal = props.me?.mode === 'local'
  const wanted = props.tab && TABS.has(props.tab) ? props.tab : isLocal ? 'local' : 'env'
  const state = useLocalProject({ ...props, isLocal, tab: wanted })
  return (
    <LocalContext.Provider value={state}>
      <div className="local-project">
        {state.error && <ErrorAlert error={state.error} onClose={state.clearError} />}
        {state.busy && <p className="lp-busy" role="status" aria-live="polite">{state.busy}…</p>}
        {!isLocal && wanted !== 'env' ? (
          <AccountModeNotice onNavigate={props.onNavigate} />
        ) : wanted === 'env' || wanted === 'local-env' ? (
          <EnvironmentPanel />
        ) : wanted === 'local-runtime' ? (
          <RuntimePanel />
        ) : wanted === 'local-library' ? (
          <LibraryPanel />
        ) : (
          <div className="lp-grid">
            <ProjectPanel />
            <FilesPanel />
          </div>
        )}
      </div>
    </LocalContext.Provider>
  )
}

/** 账号模式下打开本地项目分区时的温和提示；不会请求任何 /api/local/*。 */
function AccountModeNotice({ onNavigate }: { onNavigate: PageProps['onNavigate'] }) {
  return (
    <Section title="本地项目仅在本地模式可用">
      <div className="lp-locked">
        <p>
          当前是账号模式。项目文件夹、数据文件引用、本机任务与资源、跨项目模块库都归属于本机项目，
          只在桌面本地模式里提供；账号模式的数据保存在服务器工作区。
        </p>
        <Callout tone="info">
          如果你是想检查运行研究卡片的 Python 环境，可以在「研究执行环境」里查看解释器、依赖检查结果，
          部署管理员还可以检测、切换或修复独立环境。
        </Callout>
        <div className="lp-inline">
          <button type="button" className="button" onClick={() => onNavigate('settings/env')}>
            查看研究执行环境
          </button>
          <button type="button" className="button ghost" onClick={() => onNavigate('home')}>
            回到首页
          </button>
        </div>
      </div>
    </Section>
  )
}
