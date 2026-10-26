import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Icon } from './icons'

/**
 * 设置：全屏一页，左侧分组导航（带搜索），右侧内容，右上角 × 回到之前的页面。
 * 工作区设置与 AI 设置合在这里；入口在侧栏底部的账号菜单。
 */

export interface SettingsItem { key: string; label: string; icon: string; keywords?: string }

/**
 * 本地项目相关设置：只在本机模式下出现。
 * 本地项目页原来是一个独立页面，现在按“项目 / 环境 / 运行 / 模块库”拆进设置。
 */
const LOCAL_GROUP: { group: string; items: SettingsItem[] } = {
  group: '本机',
  items: [
    { key: 'settings/local', label: '本地项目', icon: 'overview', keywords: '项目 文件夹 路径 数据 文件 导入 引用 复制 权限 快照 导出 迁移 关窗' },
    { key: 'settings/local-env', label: '研究环境', icon: 'data', keywords: 'Python 解释器 依赖 venv conda 环境 检测 切换 修复' },
    { key: 'settings/local-runtime', label: '任务与资源', icon: 'paper', keywords: '任务 取消 资源 CPU 内存 磁盘 GPU 缓存 清理' },
    { key: 'settings/local-library', label: '模块库', icon: 'modellib', keywords: '模块 模板 发布 导入 版本 跨项目' },
  ],
}

/** 本机模式下不显示的账号/协作类设置。 */
const HIDDEN_IN_LOCAL = new Set([
  'settings/account', 'settings/privacy', 'settings/workspace', 'settings/quota', 'settings/retention',
  'settings/members', 'settings/runs', 'settings/node', 'settings/audit', 'settings/system', 'settings/env',
])
/** 账号模式下不显示的本机项目设置（研究执行环境除外）。 */
const HIDDEN_IN_ACCOUNT = new Set([
  'settings/local', 'settings/local-env', 'settings/local-runtime', 'settings/local-library',
])

export const SETTINGS_GROUPS: { group: string; items: SettingsItem[] }[] = [
  LOCAL_GROUP,
  {
    group: '设置',
    items: [
      { key: 'settings/account', label: '我的账号', icon: 'accounts', keywords: '密码 双因素 两步验证 TOTP' },
      { key: 'settings/privacy', label: '隐私与存储', icon: 'overview', keywords: '隐私 保留 清理 存储 磁盘 反馈 评分 自动删除' },
      { key: 'settings/workspace', label: '工作区', icon: 'overview', keywords: '名称 时区 新建 加入 切换' },
      { key: 'settings/quota', label: '额度与用量', icon: 'validation', keywords: '用量 配额 额度 申请 费用 token 限额 审批 CPU 回测 优化 订阅 API 计费' },
      { key: 'settings/retention', label: '研究记录', icon: 'backtests', keywords: '回测 记录 参数优化 因子 模型 模型实验 组合 执行 记录单位 倍率 配额 PIN 自动清理 存储' },
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
      { key: 'settings/plugins', label: '插件管理', icon: 'modellib', keywords: '插件 功能 开关 导航 Agent 模块 精简' },
      { key: 'settings/mcp', label: 'MCP 服务', icon: 'data', keywords: 'MCP 工具 连接 stdio HTTP 外部 服务' },
      { key: 'settings/lab', label: '研究偏好', icon: 'lab', keywords: '默认 研究类型 权限 思考 工具调用 上限 团队 上下文 建议 主题 红涨绿跌 关注 首页' },
      { key: 'settings/env', label: '研究执行环境', icon: 'data', keywords: '执行 Python 解释器 依赖 venv conda 环境 检查 修复 卡片运行' },
      { key: 'models', label: '模型', icon: 'models', keywords: 'AI 模型 密钥 API key 服务商 provider 共享 个人 价格 限额 用量 路由 联网搜索 tavily' },
      { key: 'roles', label: '角色与团队', icon: 'roles', keywords: '角色 团队 agent' },
      { key: 'prompts', label: '提示词', icon: 'prompts', keywords: 'prompt' },
    ],
  },
  {
    group: '开发者',
    items: [
      { key: 'docs', label: 'API 文档', icon: 'docs', keywords: 'sdk 接口 文档' },
      { key: 'brand', label: '视觉规范', icon: 'models', keywords: 'logo bot 品牌 机器人 表情 动效 设计规范 红绿' },
    ],
  },
]

/**
 * 设置小项索引：搜索时除了匹配页面，还能直接匹配页面里的某一项（例如「联网搜索」「单轮工具调用上限」），
 * 点击后跳到对应页面并滚动定位、高亮那一项。text 是页面上该项标题的文字（用来定位），keywords 补充同义词。
 */
export interface SettingsEntry { page: string; text: string; keywords?: string }
export const SETTINGS_ENTRIES: SettingsEntry[] = [
  { page: 'settings/privacy', text: 'Agent 评价弹窗', keywords: '评分 反馈 频率 自动关闭 意见 感谢' },
  { page: 'settings/privacy', text: '隐私与数据保留', keywords: '保留 不保留 删除 隐私政策' },
  { page: 'settings/privacy', text: '服务器存储', keywords: '磁盘 清理 分类 阈值 自动清理 最早数据' },
  { page: 'settings/account', text: '双因素认证', keywords: '两步验证 TOTP 验证器 恢复码 安全' },
  { page: 'settings/account', text: '修改密码', keywords: '密码 当前密码 新密码' },
  { page: 'settings/workspace', text: '工作区名称', keywords: '名称 改名' },
  { page: 'settings/workspace', text: '报表时区', keywords: '时区 timezone' },
  { page: 'settings/workspace', text: '新建工作区', keywords: '创建' },
  { page: 'settings/workspace', text: '凭邀请码加入', keywords: '加入 邀请码' },
  { page: 'settings/quota', text: '运行额度', keywords: '同时进行的任务 每日运行次数 bar 纸面模拟 python 策略' },
  { page: 'settings/quota', text: '用量', keywords: '工作区用量 上限 自定义上限 配额 回测次数 CPU token 数 LLM 调用 下载量 存储' },
  { page: 'settings/quota', text: 'AI 模型额度', keywords: '模型额度 申请 提升 审批' },
  { page: 'settings/quota', text: '我的模型用量', keywords: '费用 token 缓存' },
  { page: 'settings/quota', text: '工作区模型用量', keywords: '成员用量 费用' },
  { page: 'settings/retention', text: '研究记录总览', keywords: '回测 参数优化 因子 模型 组合 执行 记录单位 配额 倍率 PIN' },
  { page: 'settings/retention', text: '按记录类型统计', keywords: '倍率 默认倍率 回测 因子 模型 组合 执行' },
  { page: 'settings/retention', text: '我的自动清理设置', keywords: '自动删除 最久远 PIN 单策略 清理' },
  { page: 'settings/retention', text: '工作区记录单位上限', keywords: '管理员 上限 配额 100000' },
  { page: 'settings/retention', text: '记录明细', keywords: '筛选 删除 PIN 记录类型' },
  { page: 'settings/members', text: '成员与邀请', keywords: '邀请 角色 共享模型 个人模型 限额' },
  { page: 'settings/node', text: '节点状态', keywords: '运行节点 心跳' },
  { page: 'settings/node', text: '交易所时钟', keywords: '时钟偏差 时间同步' },
  { page: 'settings/node', text: '聊天机器人', keywords: 'telegram 飞书 绑定 通知' },
  { page: 'settings/system', text: '网络可用性实测', keywords: '网络 连通 交易所 延迟' },
  { page: 'settings/system', text: 'worker 与策略隔离', keywords: '沙箱 隔离 子进程' },
  { page: 'settings/lab', text: '新对话的默认设置', keywords: '默认' },
  { page: 'settings/local', text: '项目信息', keywords: '本地项目 文件夹 路径 名称 打开 导出 快照 关窗' },
  { page: 'settings/local', text: '数据文件与目录权限', keywords: '文件 数据 导入 引用 复制 权限 只读 可写 目录 预览 拖拽 CSV Excel Parquet' },
  { page: 'settings/local-env', text: 'Python 环境', keywords: '解释器 依赖 venv conda 检测 切换 修复 离线 wheel' },
  { page: 'settings/local-runtime', text: '任务、资源与缓存', keywords: '任务 取消 CPU 内存 磁盘 GPU 缓存 清理' },
  { page: 'settings/local-library', text: '跨项目模块库', keywords: '模块 模板 发布 导入 版本' },
  { page: 'settings/env', text: 'Python 环境', keywords: '执行 环境 依赖 解释器' },
  { page: 'settings/lab', text: '默认研究类型', keywords: '研究类型 自动 策略 因子 模型' },
  { page: 'settings/lab', text: '权限模式', keywords: '自动 手动 计划 审批' },
  { page: 'settings/lab', text: '思考努力程度', keywords: 'effort 推理' },
  { page: 'settings/lab', text: 'AI 生成简短建议', keywords: '建议 后续' },
  { page: 'settings/lab', text: '团队讨论结论自动加入上下文', keywords: '团队 discuss 上下文' },
  { page: 'settings/lab', text: '单轮工具调用上限', keywords: '工具调用 上限 次数' },
  { page: 'settings/lab', text: '涨跌配色', keywords: '红涨绿跌 绿涨红跌 颜色' },
  { page: 'settings/lab', text: '主题', keywords: '深色 浅色 暗色 dark' },
  { page: 'settings/lab', text: '首页关注列表', keywords: '关注 标的 周期 首页' },
  { page: 'settings/lab', text: '浏览器直连交易所', keywords: '行情 直连 IP 封禁 限流 请求' },
  { page: 'models', text: '工作区共享', keywords: '共享模型 服务商 密钥' },
  { page: 'models', text: '用途路由', keywords: '路由 降级链 fallback 重试' },
  { page: 'models', text: '模型目录', keywords: '模型列表 上下文 价格' },
  { page: 'models', text: '计费与限额', keywords: '价格 限额 费用' },
  { page: 'models', text: '连通性测试', keywords: '测试 调用 验证 密钥' },
  { page: 'models', text: '联网搜索', keywords: '搜索 google 谷歌 brave tavily serper searxng rss 来源 密钥 web search' },
  { page: 'models', text: '对话偏好', keywords: '建议 工具调用' },
]

/** 跳转后要定位的设置项文字：SettingsShell 在内容渲染后取走并滚动过去。 */
let pendingLocate = ''
/** 从别处（全局搜索）跳进设置并定位到某一项：先调它，再导航到 entry.page。 */
export function locateSettingAfterNav(text: string) {
  pendingLocate = text
}

/** 在设置内容区里找标题文字匹配的那一项（标题、加粗标签、表单项），滚动到中间并短暂高亮。 */
function locate(root: HTMLElement | null, text: string): boolean {
  if (!root || !text) return false
  const needle = text.trim().toLowerCase()
  const cands = root.querySelectorAll<HTMLElement>('h2, h3, h4, .set-row b, .panel-title, .panel h2, label > span, .field-label, th, legend, summary')
  let hit: HTMLElement | null = null
  for (const el of cands) {
    const t = (el.textContent || '').trim().toLowerCase()
    if (t === needle) { hit = el; break }
    if (!hit && t.startsWith(needle)) hit = el
  }
  if (!hit) for (const el of cands) if ((el.textContent || '').toLowerCase().includes(needle)) { hit = el; break }
  if (!hit) return false
  const block = (hit.closest('.set-row, .set-section, .panel, .field, section') as HTMLElement | null) || hit
  block.scrollIntoView({ behavior: 'smooth', block: 'center' })
  block.classList.remove('set-flash')
  void block.offsetWidth
  block.classList.add('set-flash')
  window.setTimeout(() => block.classList.remove('set-flash'), 2000)
  return true
}

/** 部署管理员才看得到的「运维」分组。 */
export const ADMIN_GROUP: { group: string; items: SettingsItem[] } = {
  group: '部署运维',
  items: [
    { key: 'admin/overview', label: '服务器总览', icon: 'overview', keywords: '运维 服务器 磁盘 数据库 worker 运行队列 健康' },
    { key: 'admin/users', label: '账号管理', icon: 'accounts', keywords: '用户 封禁 解封 重置密码 下线 管理员 创建账号' },
    { key: 'admin/workspaces', label: '全部工作区', icon: 'teams', keywords: '工作区 停用 恢复' },
    { key: 'admin/usage', label: '全站模型用量', icon: 'models', keywords: '用量 费用 token 账号 工作区 服务商' },
    { key: 'admin/announce', label: '公告与平台设置', icon: 'square', keywords: '公告 横幅 开放注册 邀请码' },
    { key: 'admin/audit', label: '全站审计', icon: 'validation', keywords: '审计 日志 操作记录' },
  ],
}

/** 路由（page + arg）落在哪个设置项上。 */
export function settingsKeyOf(page: string, arg: string): string {
  if (page === 'settings') return `settings/${arg.split('/')[0] || 'workspace'}`
  if (page === 'admin') return `admin/${arg.split('/')[0] || 'overview'}`
  return page
}

export function SettingsShell({ current, onNavigate, onClose, badges, children, isAdmin, localMode }: {
  current: string
  onNavigate: (k: string) => void
  onClose: () => void
  badges?: Record<string, string>
  children: ReactNode
  isAdmin?: boolean
  localMode?:boolean
}) {
  const [q, setQ] = useState('')
  const navRef = useRef<HTMLElement>(null)
  // 手机端导航是一行横向滚动的标签，当前项要滚到可见处
  useEffect(() => { navRef.current?.querySelector('[aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'center' }) }, [current])
  const needle = q.trim().toLowerCase()
  const all = isAdmin ? [...SETTINGS_GROUPS, ADMIN_GROUP] : SETTINGS_GROUPS
  // 本机模式与账号模式各自只显示有意义的设置；本机模式不再展示账号/工作区/协作类设置。
  const hidden = localMode ? HIDDEN_IN_LOCAL : HIDDEN_IN_ACCOUNT
  const groups = all.map((g) => ({
    ...g,
    items: g.items.filter((i) => !hidden.has(i.key) && !(localMode && i.key.startsWith('admin/')) && (!needle || `${g.group} ${i.label} ${i.keywords ?? ''}`.toLowerCase().includes(needle))),
  })).filter((g) => g.items.length)
  const label = all.flatMap((g) => g.items).find((i) => i.key === current)?.label ?? '设置'
  const pageLabel = (key: string) => SETTINGS_GROUPS.flatMap((g) => g.items).find((i) => i.key === key)?.label ?? key
  const contentRef = useRef<HTMLDivElement>(null)
  const [, bump] = useState(0)
  // 小项：静态索引 + 当前页面上实际渲染出来的标题（覆盖索引里没有的项）
  const entries = (() => {
    if (!needle) return [] as SettingsEntry[]
    const out = SETTINGS_ENTRIES.filter((e) => `${e.text} ${e.keywords ?? ''} ${pageLabel(e.page)}`.toLowerCase().includes(needle))
    const root = contentRef.current
    if (root) {
      root.querySelectorAll<HTMLElement>('h3, .set-row b, .panel-title, label > span').forEach((el) => {
        const t = (el.textContent || '').trim()
        if (t && t.length <= 30 && t.toLowerCase().includes(needle) && !out.some((e) => e.page === current && e.text === t)) out.push({ page: current, text: t })
      })
    }
    return out.slice(0, 12)
  })()
  const go = (e: SettingsEntry) => {
    setQ('')
    if (e.page === current) { locate(contentRef.current, e.text); return }
    pendingLocate = e.text
    onNavigate(e.page)
  }
  // 跳到新页面后等内容（含异步加载的部分）渲染出来再定位，最多等 2 秒
  useEffect(() => {
    if (!pendingLocate) return
    const text = pendingLocate
    let tries = 0
    const t = window.setInterval(() => {
      tries += 1
      if (locate(contentRef.current, text) || tries > 20) { window.clearInterval(t); pendingLocate = '' }
    }, 100)
    return () => window.clearInterval(t)
  }, [current])

  return (
    <div className="set-shell">
      <aside className="set-nav">
        <div className="set-nav-top">
          <span className="set-title">设置</span>
          <button className="set-close mobile-only" onClick={onClose} aria-label="关闭设置">×</button>
        </div>
        <label className="set-search">
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5" /><path d="m10.4 10.4 3.1 3.1" /></svg>
          <input value={q} onChange={(e) => { setQ(e.target.value); bump((x) => x + 1) }} placeholder="搜索设置或某一项" aria-label="搜索设置"
            onKeyDown={(e) => {
              if (e.key === 'Escape') setQ('')
              if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                if (entries[0]) go(entries[0])
                else if (groups[0]?.items[0]) { setQ(''); onNavigate(groups[0].items[0].key) }
              }
            }} />
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
          {entries.length > 0 && (
            <div className="set-group set-entries anim-fade">
              <div className="set-group-label">设置项</div>
              {entries.map((e) => (
                <button key={`${e.page}:${e.text}`} className="set-item set-entry" onClick={() => go(e)} title={`跳到「${pageLabel(e.page)}」的「${e.text}」`}>
                  <span>{e.text}</span>
                  <small>{pageLabel(e.page)}</small>
                </button>
              ))}
            </div>
          )}
          {!groups.length && !entries.length && <div className="set-none">没有匹配的设置</div>}
        </nav>
        <div className="set-nav-foot">
          <small>{localMode ? '本机模式 · 数据只保存在这台设备' : 'QuantLab v0.1.0-preview.2'}</small>
          <button type="button" className="set-about" onClick={() => window.dispatchEvent(new CustomEvent('qa:about'))}>关于 QuantLab</button>
        </div>
      </aside>
      <section className="set-body" aria-label={label}>
        <button className="set-close desktop-only" onClick={onClose} aria-label="关闭设置">×</button>
        <div className="set-content page-enter" key={current} ref={contentRef}>{children}</div>
      </section>
    </div>
  )
}
