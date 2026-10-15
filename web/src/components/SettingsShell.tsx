import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon } from './icons'

/**
 * 设置：全屏一页，左侧分组导航（带搜索），右侧内容，右上角 × 回到之前的页面。
 * 工作区设置与 AI 设置合在这里；入口在侧栏底部的账号菜单。
 */

export interface SettingsItem { key: string; label: string; icon: string; keywords?: string }

export const SETTINGS_GROUPS: { group: string; items: SettingsItem[] }[] = [
  {
    group: '设置',
    items: [
      { key: 'settings/account', label: '我的账号', icon: 'accounts', keywords: '密码 双因素 两步验证 TOTP' },
      { key: 'settings/workspace', label: '工作区', icon: 'overview', keywords: '名称 时区 新建 加入 切换' },
      { key: 'settings/quota', label: '额度与升级', icon: 'validation', keywords: '用量 配额 额度 申请 升级 费用 token 限额 审批' },
      { key: 'settings/members', label: '成员与邀请', icon: 'teams', keywords: '邀请码 角色 权限 共享模型 限额 用量' },
      { key: 'settings/runs', label: '任务与运行', icon: 'paper', keywords: 'worker 队列' },
      { key: 'settings/node', label: '运行节点与机器人', icon: 'demo', keywords: '节点 机器人 telegram' },
      { key: 'settings/audit', label: '审计日志', icon: 'validation', keywords: '日志 记录' },
      { key: 'settings/system', label: '网络与隔离', icon: 'data', keywords: '网络 沙箱 隔离' },
    ],
  },
  {
    group: 'AI',
    items: [
      { key: 'models', label: '模型', icon: 'models', keywords: 'AI 模型 密钥 API key 服务商 provider 共享 个人 价格 限额 用量 路由 联网搜索 tavily' },
      { key: 'roles', label: '角色与团队', icon: 'roles', keywords: '角色 团队 agent' },
      { key: 'prompts', label: '提示词', icon: 'prompts', keywords: 'prompt' },
      { key: 'long-running', label: '夜班', icon: 'long-running', keywords: '晨报 队列 自动研究' },
    ],
  },
  {
    group: '开发者',
    items: [
      { key: 'docs', label: '策略 API 文档', icon: 'docs', keywords: 'sdk 接口 文档' },
      { key: 'brand', label: '视觉规范', icon: 'models', keywords: 'logo bot 品牌 机器人 表情 动效 设计规范 红绿' },
    ],
  },
]

/** 路由（page + arg）落在哪个设置项上。 */
export function settingsKeyOf(page: string, arg: string): string {
  if (page === 'settings') return `settings/${arg.split('/')[0] || 'workspace'}`
  return page
}

export function SettingsShell({ current, onNavigate, onClose, badges, children }: {
  current: string
  onNavigate: (k: string) => void
  onClose: () => void
  badges?: Record<string, string>
  children: ReactNode
}) {
  const [q, setQ] = useState('')
  const navRef = useRef<HTMLElement>(null)
  // 手机端导航是一行横向滚动的标签，当前项要滚到可见处
  useEffect(() => { navRef.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'center' }) }, [current])
  const needle = q.trim().toLowerCase()
  const groups = SETTINGS_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((i) => !needle || `${g.group} ${i.label} ${i.keywords ?? ''}`.toLowerCase().includes(needle)),
  })).filter((g) => g.items.length)
  const label = SETTINGS_GROUPS.flatMap((g) => g.items).find((i) => i.key === current)?.label ?? '设置'

  return (
    <div className="set-shell">
      <aside className="set-nav">
        <div className="set-nav-top">
          <span className="set-title">设置</span>
          <button className="set-close mobile-only" onClick={onClose} aria-label="关闭设置">×</button>
        </div>
        <label className="set-search">
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5" /><path d="m10.4 10.4 3.1 3.1" /></svg>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索设置" aria-label="搜索设置" />
        </label>
        <nav aria-label="设置导航" ref={navRef}>
          {groups.map((g) => (
            <div key={g.group} className="set-group">
              <div className="set-group-label">{g.group}</div>
              {g.items.map((i) => (
                <button key={i.key} className="set-item" aria-current={i.key === current ? 'page' : undefined} onClick={() => { setQ(''); onNavigate(i.key) }}>
                  <Icon name={i.icon} size={15} />
                  <span>{i.label}</span>
                  {badges?.[i.key] && <em>{badges[i.key]}</em>}
                </button>
              ))}
            </div>
          ))}
          {!groups.length && <div className="set-none">没有匹配的设置</div>}
        </nav>
      </aside>
      <section className="set-body" aria-label={label}>
        <button className="set-close desktop-only" onClick={onClose} aria-label="关闭设置">×</button>
        <div className="set-content page-enter" key={current}>{children}</div>
      </section>
    </div>
  )
}
