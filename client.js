/**
 * dsh-quantlab —— 浏览器半端
 * ============================================================================
 * 自注册脚本（和 dsh-mnemon / dsh-feiyu-mood 一样的约定）：
 *
 *   window.__ModuleLoader__.load({ id: 'dsh-quantlab', factory: (require) => ({ inject, apply }) })
 *
 * 这一半只做三件事：
 *   1. 侧栏「QuantLab」图标（id `quantlab`，order 32）；
 *   2. main 面板：顶部状态条 + 内嵌本地 Python 后端页面（iframe 指向 status.url），
 *      原平台 React 界面（web/dist）由后端同源提供，这里尽量不改它；
 *   3. 设置 → QuantLab 卡片：projectDir / autoStart / llmBridge，读改 /dsh-quantlab/config。
 *
 * 所有通信都走宿主注册的 HTTP 路由（/dsh-quantlab/*），不 import 任何宿主代码，
 * 只 require('react')（package.json 的 dsh.client.external 已声明）。
 * ============================================================================
 */
window.__ModuleLoader__.load({
  id: 'dsh-quantlab',
  factory: (require) => {
    const React = require('react')
    const h = React.createElement

    /** 宿主控制路由前缀（与 docs/DSH插件迁移与股票市场.md §4.3 一致）。 */
    const API = '/dsh-quantlab'
    /** 正常轮询间隔；starting 状态下加快，让「启动中 → 运行中」看起来干脆。 */
    const POLL_MS = 5000
    const STARTING_POLL_MS = 1400

    // ─── 样式 ────────────────────────────────────────────────────────────
    // 只用中性色 + DSH 主题变量（--dsw-alias-*，取不到就退到半透明灰），
    // 不碰 body / #root，深浅色主题下都能读。
    const CSS = `
[data-ql-root]{display:flex;flex-direction:column;height:100%;min-height:0;width:100%;min-width:0;color:var(--dsw-alias-label-primary,inherit);font-family:var(--dsw-font-family,inherit);background:var(--dsw-alias-bg-base,transparent);}
[data-ql-bar]{flex:0 0 auto;display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:7px 12px;border-bottom:1px solid var(--dsw-alias-border-l3,rgba(127,127,127,.22));background:var(--dsw-alias-bg-layer-1,rgba(127,127,127,.06));}
[data-ql-dot]{flex:0 0 auto;width:8px;height:8px;border-radius:50%;background:var(--dsw-alias-state-idle-primary,#8b909a);box-shadow:0 0 0 3px rgba(127,127,127,.14);}
[data-ql-dot][data-state="running"]{background:var(--dsw-alias-state-success-primary,#2bb673);box-shadow:0 0 0 3px rgba(43,182,115,.18);}
[data-ql-dot][data-state="starting"]{background:var(--dsw-alias-state-warn-primary,#e0a100);animation:ql-pulse 1.2s ease-in-out infinite;}
[data-ql-dot][data-state="error"]{background:var(--dsw-alias-state-error-primary,#e5484d);box-shadow:0 0 0 3px rgba(229,72,77,.18);}
@keyframes ql-pulse{0%,100%{opacity:1}50%{opacity:.35}}
[data-ql-name]{font-size:13px;font-weight:600;line-height:20px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
[data-ql-state]{font-size:12px;line-height:18px;color:var(--dsw-alias-label-secondary,rgba(127,127,127,.9));}
[data-ql-chip]{font-size:11px;line-height:16px;padding:1px 7px;border-radius:999px;border:1px solid var(--dsw-alias-border-l3,rgba(127,127,127,.3));color:var(--dsw-alias-label-tertiary,rgba(127,127,127,.85));max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
[data-ql-chip][data-ql-warn]{border-color:var(--dsw-alias-state-warn-primary,#e0a100);color:var(--dsw-alias-state-warn-label,#b98400);}
[data-ql-spacer]{flex:1 1 auto;min-width:4px;}
[data-ql-btn]{flex:0 0 auto;display:inline-flex;align-items:center;gap:5px;height:26px;padding:0 10px;font-size:12px;line-height:1;border-radius:var(--dsw-radius-sm,7px);border:1px solid var(--dsw-alias-border-l3,rgba(127,127,127,.32));background:transparent;color:inherit;cursor:pointer;font-family:inherit;}
[data-ql-btn]:hover:not([disabled]){background:var(--dsw-alias-interactive-bg-hover,rgba(127,127,127,.12));}
[data-ql-btn]:focus-visible{outline:var(--dsw-focus-ring-width,2px) solid var(--dsw-focus-ring-color,var(--dsw-alias-state-business-primary,#3b6cf6));outline-offset:1px;}
[data-ql-btn][disabled]{opacity:.5;cursor:default;}
[data-ql-btn][data-ql-variant="primary"]{border-color:transparent;background:var(--dsw-alias-button-primary-fill,#3b6cf6);color:var(--dsw-alias-label-primary-foreground,#fff);}
[data-ql-btn][data-ql-variant="primary"]:hover:not([disabled]){background:var(--dsw-alias-button-primary-hover,#315eda);}
[data-ql-btn][data-ql-variant="danger"]{border-color:var(--dsw-alias-state-error-primary,#e5484d);color:var(--dsw-alias-state-error-primary,#e5484d);}
[data-ql-btn][data-ql-variant="danger"]:hover:not([disabled]){background:var(--dsw-alias-interactive-bg-hover-danger,rgba(229,72,77,.12));}
[data-ql-btn][data-ql-on]{background:var(--dsw-alias-interactive-bg-active,rgba(127,127,127,.18));}
a[data-ql-btn]{text-decoration:none;box-sizing:border-box;}
[data-ql-body]{position:relative;flex:1 1 auto;min-height:0;display:flex;flex-direction:column;}
[data-ql-frame]{flex:1 1 auto;width:100%;min-height:0;border:0;display:block;background:transparent;}
[data-ql-cardwrap]{flex:1 1 auto;min-height:0;overflow:auto;display:flex;align-items:flex-start;justify-content:center;padding:32px 20px;}
[data-ql-card]{width:min(620px,100%);display:flex;flex-direction:column;gap:12px;padding:20px 22px;border-radius:var(--dsw-radius-lg,14px);border:1px solid var(--dsw-alias-settings-card-stroke,var(--dsw-alias-border-l3,rgba(127,127,127,.24)));background:var(--dsw-alias-settings-card-fill,rgba(127,127,127,.05));}
[data-ql-cardhead]{display:flex;align-items:center;gap:10px;}
[data-ql-cardtitle]{font-size:15px;font-weight:600;line-height:22px;}
[data-ql-reason]{font-size:13px;line-height:1.65;color:var(--dsw-alias-label-secondary,rgba(127,127,127,.95));word-break:break-word;}
[data-ql-muted]{font-size:11px;line-height:1.6;color:var(--dsw-alias-label-tertiary,rgba(127,127,127,.8));word-break:break-all;}
[data-ql-actions]{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
[data-ql-msg]{font-size:12px;line-height:1.5;min-height:0;color:var(--dsw-alias-label-secondary,rgba(127,127,127,.95));}
[data-ql-msg][data-ql-tone="error"]{color:var(--dsw-alias-state-error-primary,#e5484d);}
[data-ql-msg][data-ql-tone="ok"]{color:var(--dsw-alias-state-success-primary,#2bb673);}
[data-ql-log]{margin:0;padding:10px 12px;max-height:260px;overflow:auto;border-radius:var(--dsw-radius-md,8px);background:var(--dsw-alias-markdown-code-block,rgba(0,0,0,.28));color:var(--dsw-alias-label-secondary,rgba(200,200,200,.95));font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;line-height:1.55;white-space:pre-wrap;word-break:break-all;}
[data-ql-drawer]{position:absolute;left:0;right:0;bottom:0;z-index:5;display:flex;flex-direction:column;max-height:52%;border-top:1px solid var(--dsw-alias-border-l3,rgba(127,127,127,.3));background:var(--dsw-alias-bg-overlay,var(--dsw-alias-bg-layer-2,#1b1d22));box-shadow:var(--dsw-elevation-prominent,0 -6px 24px rgba(0,0,0,.28));}
[data-ql-drawerhead]{flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:6px 12px;border-bottom:1px solid var(--dsw-alias-border-l3,rgba(127,127,127,.18));font-size:12px;color:var(--dsw-alias-label-secondary,rgba(127,127,127,.95));}
[data-ql-drawerbody]{flex:1 1 auto;min-height:0;overflow:auto;padding:10px 12px;}
[data-ql-drawerbody][data-ql-log]{max-height:none;border-radius:0;background:transparent;padding:10px 12px;}
[data-ql-spin]{width:12px;height:12px;border-radius:50%;border:2px solid var(--dsw-alias-border-l3,rgba(127,127,127,.35));border-top-color:var(--dsw-alias-state-business-primary,#3b6cf6);animation:ql-rotate .8s linear infinite;}
@keyframes ql-rotate{to{transform:rotate(360deg)}}
[data-ql-settings]{display:flex;flex-direction:column;gap:14px;padding:4px 2px 24px;max-width:720px;}
[data-ql-sec]{display:flex;flex-direction:column;gap:10px;padding:14px 16px;border-radius:var(--dsw-radius-lg,12px);border:1px solid var(--dsw-alias-settings-card-stroke,var(--dsw-alias-border-l3,rgba(127,127,127,.22)));background:var(--dsw-alias-settings-card-fill,rgba(127,127,127,.05));}
[data-ql-h]{font-size:13px;font-weight:600;line-height:20px;}
[data-ql-row]{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap;}
[data-ql-grow]{flex:1 1 320px;min-width:0;display:flex;flex-direction:column;gap:5px;}
[data-ql-label]{font-size:13px;line-height:20px;}
[data-ql-note]{font-size:11px;line-height:1.55;color:var(--dsw-alias-label-tertiary,rgba(127,127,127,.85));}
[data-ql-input]{box-sizing:border-box;width:100%;height:30px;padding:0 9px;font-size:13px;font-family:inherit;border-radius:var(--dsw-radius-sm,7px);border:1px solid var(--dsw-alias-border-l4,rgba(127,127,127,.34));background:var(--dsw-alias-bg-layer-3,rgba(127,127,127,.06));color:inherit;}
[data-ql-input]:focus-visible{outline:none;border-color:var(--dsw-alias-state-business-primary,#3b6cf6);}
[data-ql-check]{display:flex;align-items:flex-start;gap:8px;font-size:13px;line-height:20px;cursor:pointer;}
[data-ql-check] input{margin:2px 0 0;}
[data-ql-kv]{font-size:11px;line-height:1.7;color:var(--dsw-alias-label-tertiary,rgba(127,127,127,.85));word-break:break-all;}
[data-ql-importform]{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px;}
[data-ql-importform] input[type="url"]{flex:1 1 320px;min-width:0;}
[data-ql-importform] input[type="text"]{flex:0 1 200px;}
[data-ql-importlist]{display:flex;flex-direction:column;gap:8px;margin-top:8px;}
[data-ql-importrow]{display:flex;flex-direction:column;gap:6px;padding:10px 12px;border-radius:var(--dsw-radius-md,10px);border:1px solid var(--dsw-alias-border-l3,rgba(127,127,127,.22));background:var(--dsw-alias-bg-layer-2,rgba(127,127,127,.05));}
[data-ql-importhead]{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;font-size:13px;}
[data-ql-importmeta]{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:11px;color:var(--dsw-alias-label-tertiary,rgba(127,127,127,.85));}
[data-ql-link]{color:var(--dsw-alias-link,var(--dsw-alias-state-business-primary,#3b6cf6));text-decoration:none;word-break:break-all;}
[data-ql-link]:hover{text-decoration:underline;}
[data-ql-reportcard]{display:flex;flex-direction:column;gap:8px;padding:10px 12px;border-radius:var(--dsw-radius-md,10px);border:1px solid var(--dsw-alias-border-l3,rgba(127,127,127,.24));background:var(--dsw-alias-settings-card-fill,rgba(127,127,127,.05));max-width:680px;}
[data-ql-reportcard][data-ql-report-native]{max-width:960px;}
[data-ql-reportthumb]{width:100%;max-height:180px;object-fit:contain;border-radius:6px;background:rgba(127,127,127,.08);}
[data-ql-reportthumb][data-ql-clickable]{cursor:zoom-in;}
[data-ql-reportframe]{display:block;width:100%;height:min(72vh,620px);min-height:360px;border:1px solid var(--dsw-alias-border-l3,rgba(127,127,127,.24));border-radius:8px;background:#fff;}
[data-ql-reportrow]{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;}
[data-ql-reporttitle]{font-weight:600;font-size:13px;}
[data-ql-reportmetrics]{display:flex;gap:10px;flex-wrap:wrap;font-size:11px;color:var(--dsw-alias-label-secondary,inherit);}
[data-ql-reportmetrics] b{font-weight:600;font-variant-numeric:tabular-nums;}
`

    // ─── HTTP 小工具 ─────────────────────────────────────────────────────
    /** 读 JSON；HTTP 非 2xx / 非 JSON 都抛出，调用方负责把它变成可见文案。 */
    const getJson = async (url) => {
      const res = await fetch(url, { headers: { accept: 'application/json' }, credentials: 'same-origin' })
      const text = await res.text()
      let data = null
      try { data = text ? JSON.parse(text) : null } catch (_) { data = null }
      if (!res.ok) {
        const detail = data && (data.error || data.message || data.reason)
        throw new Error(detail || ('HTTP ' + res.status))
      }
      if (data === null) throw new Error('返回的不是 JSON（插件宿主可能没装好）')
      return data
    }

    const postJson = async (url, body, method) => {
      const verb = String(method || 'POST').toUpperCase()
      const hasBody = verb === 'POST' || verb === 'PUT' || verb === 'PATCH'
      const res = await fetch(url, {
        method: verb,
        headers: { accept: 'application/json', ...(hasBody ? { 'content-type': 'application/json' } : {}) },
        credentials: 'same-origin',
        ...(hasBody ? { body: JSON.stringify(body || {}) } : {}),
      })
      const text = await res.text()
      let data = null
      try { data = text ? JSON.parse(text) : null } catch (_) { data = null }
      if (!res.ok) {
        const detail = data && (data.error || data.message || data.reason)
        throw new Error(detail || ('HTTP ' + res.status))
      }
      return data
    }

    /** /dsh-quantlab/logs 可能是纯文本，也可能是 {text|tail|logTail}。 */
    const getLogsText = async () => {
      const res = await fetch(API + '/logs?lines=120', { headers: { accept: 'text/plain, application/json' }, credentials: 'same-origin' })
      const text = await res.text()
      if (!res.ok) throw new Error('HTTP ' + res.status)
      if (!text) return '（日志为空）'
      try {
        const data = JSON.parse(text)
        if (data && typeof data === 'object') {
          const out = data.text ?? data.tail ?? data.logTail ?? data.logs
          if (typeof out === 'string') return out || '（日志为空）'
        }
      } catch (_) { /* 纯文本，按原样返回 */ }
      return text
    }

    // ─── status 归一化 ──────────────────────────────────────────────────
    // 宿主按 §4.3 返回 {ok,state,url,port,projectDir,pid,logTail,llmBridge,startedAt,error}；
    // 这里再宽容一点（允许包一层 status / 缺字段），面板永远不因为字段缺失白屏。
    const normalizeStatus = (data) => {
      if (!data || typeof data !== 'object') return null
      const st = data.status && typeof data.status === 'object' ? data.status : data
      const state = String(st.state || (st.url ? 'running' : 'stopped'))
      const target = st.resolvedTarget && typeof st.resolvedTarget === 'object' ? st.resolvedTarget : null
      return {
        ok: st.ok !== false,
        state: ['starting', 'running', 'stopped', 'error'].indexOf(state) >= 0 ? state : 'stopped',
        url: typeof st.url === 'string' ? st.url : '',
        port: st.port,
        projectDir: typeof st.projectDir === 'string' ? st.projectDir : '',
        pid: st.pid,
        llmBridge: st.llmBridge,
        startedAt: st.startedAt,
        error: typeof st.error === 'string' ? st.error : (st.error ? String(st.error) : ''),
        logTail: typeof st.logTail === 'string' ? st.logTail : '',
        model: typeof st.model === 'string' ? st.model : '',
        providerName: typeof st.providerName === 'string' ? st.providerName : '',
        resolvedTarget: target,
        // 模型名说人话：宿主解析出的真实目标（如 opencode-go/deepseek-v4.1-flash）
        resolvedTargetLabel: typeof st.resolvedTargetLabel === 'string' ? st.resolvedTargetLabel : '',
      }
    }

    const STATE_LABEL = { running: '运行中', starting: '启动中', stopped: '已停止', error: '异常' }

    /** 项目名：projectDir 的最后一节（Windows / POSIX 都切）。 */
    const projectName = (dir) => {
      const parts = String(dir || '').split(/[\\/]+/).filter(Boolean)
      return parts.length ? parts[parts.length - 1] : 'QuantLab'
    }

    const errorText = (e) => {
      if (!e) return '未知错误'
      const m = e && e.message ? e.message : String(e)
      return m.length > 600 ? m.slice(0, 600) + '…' : m
    }

    // ─── 嵌入参数（与工作流 E 约定） ─────────────────────────────────────
    // html[data-embed="1"] 收起平台侧栏为窄轨、隐藏重复品牌条；
    // html[data-theme="dark"|"light"] 深/浅色；系统主题不传 theme（平台自己跟 prefers-color-scheme）。
    // 也支持 postMessage({type:'quantlab:theme', theme}) 热切换，避免重载 iframe。
    const THEME_QUERY = 'embed=1'

    /** 读 DSH 当前主题：优先显式 data-theme / data-color-scheme，其次系统偏好。 */
    const detectTheme = () => {
      try {
        const root = document.documentElement
        for (const attr of ['data-theme', 'data-dsh-theme', 'data-color-scheme', 'data-colorscheme']) {
          const value = String(root.getAttribute(attr) || '').toLowerCase()
          if (value === 'dark' || value === 'light') return value
        }
        const scheme = String(root.style.colorScheme || '').toLowerCase()
        if (scheme === 'dark' || scheme === 'light') return scheme
      } catch (_) { /* ignore */ }
      try {
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark'
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) return 'light'
      } catch (_) { /* ignore */ }
      return 'auto'
    }

    /** 把后端 URL 变成 iframe 用的嵌入地址：保留 hash，query 加 embed/theme。 */
    const buildFrameUrl = (backendUrl, theme, route) => {
      const url = String(backendUrl || '').trim()
      if (!url) return ''
      try {
        const parsed = new URL(url)
        const params = parsed.searchParams
        params.set('embed', '1')
        if (theme === 'dark' || theme === 'light') params.set('theme', theme)
        else params.delete('theme')
        let hash = parsed.hash
        if (route) {
          const clean = String(route).startsWith('#') ? String(route) : '#' + String(route)
          hash = clean
        }
        return parsed.origin + parsed.pathname + '?' + params.toString() + hash
      } catch (_) {
        const joiner = url.indexOf('?') >= 0 ? '&' : '?'
        const themePart = theme === 'dark' || theme === 'light' ? `&theme=${theme}` : ''
        return `${url}${joiner}${THEME_QUERY}${themePart}${route ? '#' + String(route).replace(/^#/, '') : ''}`
      }
    }

    /** 从 iframe 里拿平台 URL（去掉嵌入参数）用于外部打开。 */
    const plainPlatformUrl = (backendUrl, route) => {
      const url = String(backendUrl || '').trim()
      if (!url) return ''
      if (!route) return url
      try {
        const parsed = new URL(url)
        parsed.hash = String(route).startsWith('#') ? String(route) : '#' + String(route)
        return parsed.origin + parsed.pathname + (parsed.search || '') + parsed.hash
      } catch (_) {
        return url + '#' + String(route).replace(/^#/, '')
      }
    }

    /**
     * 卡片要求面板跳到某条结果：事件只在面板已挂载时听得到，所以先把路由存进
     * sessionStorage，面板首次挂载时消费——否则「在侧栏打开」只会打开首页。
     */
    const PENDING_ROUTE_KEY = 'dsh-quantlab:pending-route'
    const stashPendingRoute = (route) => {
      try {
        const text = String(route || '').trim()
        if (text) sessionStorage.setItem(PENDING_ROUTE_KEY, text)
      } catch (_) { /* 隐私模式等：忽略，事件仍然可用 */ }
    }
    const takePendingRoute = () => {
      try {
        const value = String(sessionStorage.getItem(PENDING_ROUTE_KEY) || '').trim()
        if (value) sessionStorage.removeItem(PENDING_ROUTE_KEY)
        return value
      } catch (_) {
        return ''
      }
    }

    /** 平台回测详情页的路由（单数 backtest；`backtests/<id>` 是列表页的 slug 过滤）。 */
    const backtestRoute = (id) => (id ? '#/backtest/' + encodeURIComponent(String(id)) : '#/backtests')

    /** 从工具结果文本里找卡片 JSON：宿主在 markdown 后面留一行 <!-- quantlab-card {...} -->。 */
    const parseCardJson = (text) => {
      const raw = String(text || '')
      const m = raw.match(/<!--\s*quantlab-card\s+([\s\S]*?)\s*-->/)
      if (!m) return null
      try {
        return JSON.parse(m[1])
      } catch (_) {
        return null
      }
    }

    /** 工具结果 content（数组）里所有 text 块拼起来。 */
    const contentText = (block) => {
      const content = block && Array.isArray(block.content) ? block.content : []
      return content
        .map((item) => (item && item.type === 'text' && typeof item.text === 'string' ? item.text : ''))
        .filter(Boolean)
        .join('\n')
    }

    // ─── main 面板 ───────────────────────────────────────────────────────
    const actFailed = (name) => name === 'start' ? '启动失败' : name === 'stop' ? '停止失败' : '重启失败'

    function QuantLabPanel(props) {
      const openSettings = props && props.openSettings
      const openPanel = props && props.openPanel
      const [status, setStatus] = React.useState(null)
      const [apiError, setApiError] = React.useState('')
      const [busy, setBusy] = React.useState('')
      const [msg, setMsg] = React.useState(null)          // {text, tone}
      const [frameNonce, setFrameNonce] = React.useState(0)
      const [logsOpen, setLogsOpen] = React.useState(false)
      const [logs, setLogs] = React.useState('')
      const [logsError, setLogsError] = React.useState('')
      // 嵌入参数：主题（跟随 DSH/系统，变化时 postMessage 热切换）
      const [theme, setTheme] = React.useState(() => detectTheme())
      const [frameRoute, setFrameRoute] = React.useState(() => takePendingRoute())  // 卡片「打开面板」要落的平台路由
      // 「导入 GitHub」（入口在状态条，不占内容区；结果卡在抽屉里）
      const [importsOpen, setImportsOpen] = React.useState(false)
      const [imports, setImports] = React.useState([])
      const [importsError, setImportsError] = React.useState('')
      const [importUrl, setImportUrl] = React.useState('')
      const [importNote, setImportNote] = React.useState('')
      const [importBusy, setImportBusy] = React.useState('')
      const [importMsg, setImportMsg] = React.useState(null)

      const statusRef = React.useRef(null)
      const logsOpenRef = React.useRef(false)
      const importsOpenRef = React.useRef(false)
      const frameRef = React.useRef(null)
      const msgTimer = React.useRef(null)

      const flash = React.useCallback((text, tone) => {
        setMsg({ text, tone: tone || '' })
        if (msgTimer.current) clearTimeout(msgTimer.current)
        msgTimer.current = setTimeout(() => setMsg(null), 5000)
      }, [])

      const applyStatus = React.useCallback((st) => {
        statusRef.current = st
        setStatus(st)
        setApiError('')
      }, [])

      /** 拉一次状态。失败 = 插件宿主没连上（路由不存在/宿主没加载）→ 可见错误卡。 */
      const refresh = React.useCallback(async () => {
        try {
          const data = await getJson(API + '/status')
          const st = normalizeStatus(data)
          if (!st) throw new Error('状态返回为空')
          applyStatus(st)
          return st
        } catch (e) {
          statusRef.current = null
          setStatus(null)
          setApiError(errorText(e))
          return null
        }
      }, [applyStatus])

      const loadLogs = React.useCallback(async () => {
        try {
          const text = await getLogsText()
          setLogs(text)
          setLogsError('')
          return text
        } catch (e) {
          setLogsError(errorText(e))
          return ''
        }
      }, [])

      const act = React.useCallback(async (name) => {
        setBusy(name)
        try {
          const data = await postJson(API + '/' + name, {})
          const st = normalizeStatus(data)
          if (st) applyStatus(st)
          else await refresh()
          const next = statusRef.current
          if (name === 'start') flash(next && next.state === 'running' ? '后端已运行' : '已请求启动后端…', 'ok')
          else if (name === 'stop') flash('已请求停止后端', 'ok')
          else flash('已请求重启后端…', 'ok')
          if (logsOpenRef.current) loadLogs()
        } catch (e) {
          flash(actFailed(name) + '：' + errorText(e), 'error')
          // 路由失败也可能是宿主没连上：让状态条如实反映
          if (!statusRef.current) setApiError(errorText(e))
        } finally {
          setBusy('')
        }
      }, [applyStatus, refresh, flash, loadLogs])

      // 轮询：document.hidden 时暂停；切回可见立刻补一次。
      // starting 状态下间隔缩短到 1.4s，其余 5s。
      React.useEffect(() => {
        let alive = true
        let timer = null
        const tick = async () => {
          if (!alive) return
          if (!document.hidden) {
            await refresh()
            if (logsOpenRef.current) loadLogs()
          }
          if (!alive) return
          const st = statusRef.current
          const wait = st && st.state === 'starting' ? STARTING_POLL_MS : POLL_MS
          timer = setTimeout(tick, wait)
        }
        tick()
        const onVisible = () => {
          if (document.hidden) return
          if (timer) clearTimeout(timer)
          tick()
        }
        const onFocus = () => { if (!document.hidden) refresh() }
        document.addEventListener('visibilitychange', onVisible)
        window.addEventListener('focus', onFocus)
        return () => {
          alive = false
          if (timer) clearTimeout(timer)
          document.removeEventListener('visibilitychange', onVisible)
          window.removeEventListener('focus', onFocus)
          if (msgTimer.current) clearTimeout(msgTimer.current)
        }
      }, [refresh, loadLogs])

      React.useEffect(() => {
        logsOpenRef.current = logsOpen
        if (logsOpen) loadLogs()
      }, [logsOpen, loadLogs])

      // 主题：DSH 切换深/浅色（或系统偏好变化）时，先 postMessage 给 iframe 热切换；
      // 平台（工作流 E）支持 {type:'quantlab:theme', theme}，不支持也能靠下次刷新拿到新参数。
      React.useEffect(() => {
        const apply = () => {
          const next = detectTheme()
          setTheme(next)
          try {
            const win = frameRef.current && frameRef.current.contentWindow
            if (win) win.postMessage({ type: 'quantlab:theme', theme: next === 'auto' ? 'auto' : next }, '*')
          } catch (_) { /* 跨端口/未加载：忽略 */ }
        }
        let mq = null
        try { mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null } catch (_) { mq = null }
        if (mq && typeof mq.addEventListener === 'function') mq.addEventListener('change', apply)
        else if (mq && typeof mq.addListener === 'function') mq.addListener(apply)
        let observer = null
        try {
          if (typeof MutationObserver === 'function') {
            observer = new MutationObserver(apply)
            observer.observe(document.documentElement, {
              attributes: true,
              attributeFilter: ['data-theme', 'data-dsh-theme', 'data-color-scheme', 'data-colorscheme', 'style', 'class'],
            })
          }
        } catch (_) { observer = null }
        return () => {
          try { if (mq && typeof mq.removeEventListener === 'function') mq.removeEventListener('change', apply) } catch (_) { /* ignore */ }
          try { if (mq && typeof mq.removeListener === 'function') mq.removeListener(apply) } catch (_) { /* ignore */ }
          try { if (observer) observer.disconnect() } catch (_) { /* ignore */ }
        }
      }, [])

      // 卡片里的「在侧栏打开」：`window.dispatchEvent(new CustomEvent('dsh:quantlab-open', {detail:{route:'#/backtest/123'}}))`
      React.useEffect(() => {
        const onOpen = (event) => {
          const detail = (event && event.detail) || {}
          const requested = detail.route
            || (detail.backtestId ? backtestRoute(detail.backtestId) : '')
          // 事件能到说明面板已挂载：顺手清掉待消费路由，避免下次挂载又跳一次。
          const route = requested || takePendingRoute()
          if (requested) takePendingRoute()
          if (route) setFrameRoute(String(route))
          if (detail.focus !== false && typeof openPanel === 'function') {
            try { openPanel() } catch (_) { /* 宿主没暴露 layout：面板已开就无所谓 */ }
          }
        }
        window.addEventListener('dsh:quantlab-open', onOpen)
        return () => window.removeEventListener('dsh:quantlab-open', onOpen)
      }, [openPanel])

      // ─── GitHub 导入（入口在状态条，列表/结果在抽屉里） ──────────────────
      const loadImports = React.useCallback(async () => {
        try {
          const data = await getJson(API + '/imports')
          setImports(Array.isArray(data && data.imports) ? data.imports : [])
          setImportsError('')
          return data
        } catch (e) {
          setImportsError(errorText(e))
          return null
        }
      }, [])

      React.useEffect(() => {
        importsOpenRef.current = importsOpen
        if (importsOpen) loadImports()
      }, [importsOpen, loadImports])

      const toggleImports = () => {
        const next = !importsOpen
        setImportsOpen(next)
        if (next) loadImports()
      }

      const submitImport = async () => {
        const url = String(importUrl || '').trim()
        if (!url) {
          setImportMsg({ text: '先填 GitHub 地址，例如 https://github.com/octocat/Hello-World', tone: 'error' })
          return
        }
        setImportBusy('import')
        setImportMsg({ text: '正在浅克隆仓库（--depth 1，最多 120 秒）…', tone: '' })
        try {
          const data = await postJson(API + '/imports/github', { url, note: String(importNote || '').trim() })
          const rec = data && data.import ? data.import : null
          const strategies = rec && Array.isArray(rec.strategies_imported) ? rec.strategies_imported : []
          const sizeKb = rec && Number(rec.bytes) ? (Number(rec.bytes) / 1024).toFixed(1) : '0'
          setImportMsg({
            text: rec
              ? `已导入 ${rec.name}：${rec.files} 个文件，${sizeKb} KB`
                + (strategies.length ? `，识别并导入 ${strategies.length} 个策略包（${strategies.map((s) => s.slug).join('、')}）` : '')
                + (Array.isArray(rec.warnings) && rec.warnings.length ? `；${rec.warnings.length} 条提示` : '')
              : '已导入',
            tone: 'ok',
          })
          setImportUrl('')
          setImportNote('')
          await loadImports()
        } catch (e) {
          setImportMsg({ text: '导入失败：' + errorText(e), tone: 'error' })
        } finally {
          setImportBusy('')
        }
      }

      const removeImport = async (rec) => {
        if (!rec || !rec.id) return
        if (typeof window !== 'undefined' && typeof window.confirm === 'function') {
          const okDelete = window.confirm(`删除导入「${rec.name || rec.id}」？会移除项目里的仓库目录。`)
          if (!okDelete) return
        }
        setImportBusy('delete:' + rec.id)
        try {
          await postJson(API + '/imports/' + encodeURIComponent(rec.id), undefined, 'DELETE')
          setImportMsg({ text: `已删除 ${rec.name || rec.id}`, tone: 'ok' })
          await loadImports()
        } catch (e) {
          setImportMsg({ text: '删除失败：' + errorText(e), tone: 'error' })
        } finally {
          setImportBusy('')
        }
      }

      const openImportFolder = async (rec) => {
        if (!rec || !rec.id) return
        setImportBusy('open:' + rec.id)
        try {
          const detail = await getJson(API + '/imports/' + encodeURIComponent(rec.id))
          const target = detail && detail.import ? detail.import.path : ''
          if (!target) throw new Error('记录里没有目录路径（可能已被删除）')
          await postJson(API + '/open', { path: target })
          setImportMsg({ text: '已在文件管理器打开：' + target, tone: 'ok' })
        } catch (e) {
          setImportMsg({ text: '打开目录失败：' + errorText(e), tone: 'error' })
        } finally {
          setImportBusy('')
        }
      }

      const state = status ? status.state : (apiError ? 'error' : 'stopped')
      const running = state === 'running' && !!status && !!status.url
      const starting = state === 'starting'
      const canStart = state === 'stopped' || state === 'error'
      const disabled = !!busy

      const openSettingsSafe = () => {
        let opened = false
        try {
          opened = typeof openSettings === 'function' ? openSettings() === true : false
        } catch (error) {
          console.warn('dsh-quantlab: 打开设置失败', error)
        }
        flash(opened ? '已打开设置，找到左侧「QuantLab」分区' : '打开 DSH 设置 → 左侧「QuantLab」分区即可改配置')
      }

      // 状态条
      const bar = h('div', { 'data-ql-bar': '', role: 'toolbar', 'aria-label': 'QuantLab 状态条' },
        h('span', { 'data-ql-dot': '', 'data-state': state, 'aria-hidden': 'true' }),
        h('span', { 'data-ql-name': '', title: 'QuantLab 量化研究台' }, 'QuantLab'),
        h('span', { 'data-ql-state': '' }, STATE_LABEL[state] || state,
          running && status.port ? ' · ' + status.port : ''),
        status && status.projectDir
          ? h('span', { 'data-ql-chip': '', title: '项目目录：' + status.projectDir }, projectName(status.projectDir))
          : null,
        // 模型名说人话：优先宿主解析出的真实模型（DSH · deepseek-v4.1-flash），
        // 退回到 provider 登记名，再退回平台模型 id（dsh-default）。hover 显示 effort/上下文。
        (() => {
          const label = (status && status.resolvedTargetLabel)
            || (status && status.resolvedTarget && status.resolvedTarget.model
              ? [status.providerName || 'DSH', status.resolvedTarget.model].filter(Boolean).join(' · ')
              : '')
            || (status && status.model) || ''
          if (!label) return null
          const target = (status && status.resolvedTarget) || {}
          const tip = [
            '模型：' + label,
            target.provider ? 'provider：' + target.provider : '',
            target.model ? 'model：' + target.model : '',
            target.reasoningEffort ? 'effort：' + target.reasoningEffort : '',
            status && status.llmBridge === 'ready' ? '模型桥：可用' : '模型桥：不可用',
          ].filter(Boolean).join('\n')
          return h('span', { 'data-ql-chip': '', 'data-ql-model-chip': '', title: tip }, label)
        })(),
        status && status.llmBridge === 'unavailable'
          ? h('span', { 'data-ql-chip': '', 'data-ql-warn': '' }, '模型桥不可用')
          : null,
        h('span', { 'data-ql-spacer': '' }),
        canStart
          ? h('button', {
            type: 'button', 'data-ql-btn': '', 'data-ql-variant': 'primary', disabled: disabled,
            onClick: () => act('start'), title: '启动本地 QuantLab 后端',
          }, busy === 'start' ? '启动中…' : '启动')
          : null,
        (running || starting)
          ? h('button', {
            type: 'button', 'data-ql-btn': '', disabled: disabled,
            onClick: () => act('stop'), title: '停止本地 QuantLab 后端',
          }, busy === 'stop' ? '停止中…' : '停止')
          : null,
        status
          ? h('button', {
            type: 'button', 'data-ql-btn': '', disabled: disabled,
            onClick: () => act('restart'), title: '重启本地 QuantLab 后端',
          }, busy === 'restart' ? '重启中…' : '重启')
          : null,
        h('button', {
          type: 'button', 'data-ql-btn': '', disabled: disabled,
          onClick: async () => { await refresh(); setFrameNonce((n) => n + 1); flash('已刷新') },
          title: '重新检查状态并刷新内嵌页面',
        }, '刷新'),
        h('button', {
          type: 'button', 'data-ql-btn': '', 'data-ql-on': importsOpen ? '' : undefined,
          'data-ql-imports-btn': '',
          onClick: toggleImports,
          'aria-pressed': importsOpen ? 'true' : 'false',
          title: '粘贴 GitHub 地址导入仓库（克隆到项目里）',
        }, '导入 GitHub'),
        h('button', {
          type: 'button', 'data-ql-btn': '', 'data-ql-on': logsOpen ? '' : undefined,
          onClick: () => setLogsOpen((v) => !v),
          'aria-pressed': logsOpen ? 'true' : 'false',
          title: '查看后端日志尾部',
        }, '日志'),
        h('button', {
          type: 'button', 'data-ql-btn': '', onClick: openSettingsSafe,
          title: '打开设置中的 QuantLab 分区',
        }, '设置'),
      )

      // 内嵌原平台界面：?embed=1&theme=<dark|light>（工作流 E 的 html[data-embed]/html[data-theme] 约定）
      const frameSrc = running ? buildFrameUrl(status.url, theme, frameRoute) : ''
      const frame = running
        ? h('iframe', {
          key: (status.url || '') + '#' + frameNonce,
          ref: frameRef,
          'data-ql-frame': '',
          'data-ql-embed': '1',
          'data-ql-theme': theme,
          src: frameSrc,
          title: 'QuantLab 平台',
          allow: 'clipboard-read; clipboard-write',
        })
        : null

      // 未运行 / 未连上时的卡片
      const card = frame ? null : h('div', { 'data-ql-cardwrap': '' },
        h('div', { 'data-ql-card': '' },
          h('div', { 'data-ql-cardhead': '' },
            starting ? h('span', { 'data-ql-spin': '', 'aria-hidden': 'true' }) : null,
            h('div', { 'data-ql-cardtitle': '' },
              apiError ? '插件后端没连上' : (starting ? '正在启动 QuantLab…' : (state === 'error' ? 'QuantLab 后端异常' : 'QuantLab 后端未运行')))),
          h('div', { 'data-ql-reason': '' },
            apiError
              ? 'GET ' + API + '/status 失败：' + apiError
                + '\n宿主插件可能没加载（先在设置里确认插件已装好并重启 DSH），或路由被拦截。'
              : starting
                ? '本地 Python 服务正在启动，就绪后这里会自动换成平台界面（每 5 秒检查一次）。'
                : (state === 'error' && status && status.error)
                  ? status.error
                  : '启动本地后端后，原 QuantLab 平台界面会直接嵌入到这个面板里；数据、回测、AI 研究室都用同一套后端。'),
          status && status.projectDir
            ? h('div', { 'data-ql-muted': '' }, '项目目录：' + status.projectDir)
            : null,
          h('div', { 'data-ql-actions': '' },
            canStart && !apiError
              ? h('button', {
                type: 'button', 'data-ql-btn': '', 'data-ql-variant': 'primary', disabled: disabled,
                onClick: () => act('start'),
              }, busy === 'start' ? '启动中…' : '启动后端')
              : null,
            apiError
              ? h('button', {
                type: 'button', 'data-ql-btn': '', 'data-ql-variant': 'primary',
                onClick: () => refresh(),
              }, '重试连接')
              : null,
            h('button', {
              type: 'button', 'data-ql-btn': '', onClick: () => { setLogsOpen(true); loadLogs() },
            }, '查看日志'),
            h('button', { type: 'button', 'data-ql-btn': '', onClick: openSettingsSafe }, '打开设置'),
          ),
          msg ? h('div', { 'data-ql-msg': '', 'data-ql-tone': msg.tone || undefined }, msg.text) : null,
          (status && status.logTail)
            ? h('pre', { 'data-ql-log': '' }, status.logTail)
            : null,
        ))

      // 日志抽屉（覆盖在 iframe 下缘，可关闭）
      const drawer = logsOpen
        ? h('div', { 'data-ql-drawer': '', role: 'dialog', 'aria-label': 'QuantLab 后端日志' },
          h('div', { 'data-ql-drawerhead': '' },
            h('span', null, '后端日志 · 最近 120 行'),
            h('span', { 'data-ql-spacer': '' }),
            h('button', {
              type: 'button', 'data-ql-btn': '',
              onClick: () => loadLogs(), title: '重新读取',
            }, '重新读取'),
            h('button', {
              type: 'button', 'data-ql-btn': '',
              onClick: () => setLogsOpen(false), title: '关闭日志',
            }, '关闭'),
          ),
          h('div', { 'data-ql-drawerbody': '' },
            logsError
              ? h('div', { 'data-ql-msg': '', 'data-ql-tone': 'error' }, '读取日志失败：' + logsError)
              : h('pre', { 'data-ql-log': '' }, logs || '（暂无日志）'),
          ))
        : null

      // GitHub 导入抽屉（入口在状态条；复制地址即可导入，不占内容区）
      const importsDrawer = importsOpen
        ? h('div', {
          'data-ql-drawer': '', 'data-ql-imports-drawer': '', role: 'dialog', 'aria-label': '导入 GitHub 仓库',
        },
        h('div', { 'data-ql-drawerhead': '' },
          h('span', null, '导入 GitHub 仓库'),
          h('span', { 'data-ql-spacer': '' }),
          h('button', {
            type: 'button', 'data-ql-btn': '',
            onClick: () => setImportsOpen(false), title: '关闭导入面板',
          }, '关闭'),
        ),
        h('div', { 'data-ql-drawerbody': '' },
          h('div', { 'data-ql-importform': '' },
            h('input', {
              'data-ql-input': '', 'data-ql-import-url': '', type: 'url',
              placeholder: 'https://github.com/<owner>/<repo>',
              value: importUrl,
              onChange: (e) => setImportUrl(e.target.value),
              onKeyDown: (e) => { if (e.key === 'Enter') submitImport() },
              'aria-label': 'GitHub 仓库地址',
            }),
            h('input', {
              'data-ql-input': '', type: 'text', placeholder: '备注（可选）',
              value: importNote,
              onChange: (e) => setImportNote(e.target.value),
              'aria-label': '导入备注',
            }),
            h('button', {
              type: 'button', 'data-ql-btn': '', 'data-ql-variant': 'primary', 'data-ql-import-submit': '',
              disabled: !!importBusy, onClick: submitImport,
            }, importBusy === 'import' ? '导入中…' : '导入'),
          ),
          h('div', { 'data-ql-note': '' },
            '浅克隆（--depth 1）到 <项目>/.quantlab/imports/github/<owner>-<repo>-<时间>；'
              + '仓库上限 200MB、超时 120s；有 .qstrat 会自动走策略包导入（导入后需你批准才能用于模拟盘）。'),
          importMsg
            ? h('div', {
              'data-ql-msg': '', 'data-ql-import-msg': '', 'data-ql-tone': importMsg.tone || undefined,
            }, importMsg.text)
            : null,
          importsError
            ? h('div', { 'data-ql-msg': '', 'data-ql-tone': 'error' }, '读取导入列表失败：' + importsError)
            : null,
          h('div', { 'data-ql-importlist': '' },
            imports.length === 0
              ? h('div', { 'data-ql-note': '' }, importsError ? '（列表暂不可用）' : '还没有导入过仓库。粘贴一个公开仓库地址试试。')
              : imports.map((rec) => h('div', {
                key: rec.id, 'data-ql-importrow': '', 'data-ql-import-id': rec.id,
              },
              h('div', { 'data-ql-importhead': '' },
                h('b', null, rec.name || rec.id),
                h('span', { 'data-ql-muted': '' },
                  `${rec.files || 0} 个文件 · ${(Number(rec.bytes || 0) / 1024).toFixed(1)} KB`
                    + ` · ${new Date(Number(rec.created_at || 0) * 1000).toLocaleString('zh-CN')}`),
              ),
              h('div', { 'data-ql-importmeta': '' },
                rec.url
                  ? h('a', { href: rec.url, target: '_blank', rel: 'noreferrer', 'data-ql-link': '' }, rec.url)
                  : null,
                rec.exists === false ? h('span', { 'data-ql-note': '' }, '（目录已不在）') : null,
                Array.isArray(rec.strategies_imported) && rec.strategies_imported.length
                  ? h('span', { 'data-ql-chip': '' }, `策略包 ${rec.strategies_imported.length}`)
                  : null,
                Array.isArray(rec.warnings) && rec.warnings.length
                  ? h('span', { 'data-ql-chip': '', 'data-ql-warn': '', title: rec.warnings.join('\n') }, `${rec.warnings.length} 条提示`)
                  : null,
              ),
              h('div', { 'data-ql-actions': '' },
                h('button', {
                  type: 'button', 'data-ql-btn': '', disabled: !!importBusy,
                  onClick: () => openImportFolder(rec), title: '在文件管理器打开导入目录',
                }, importBusy === 'open:' + rec.id ? '打开中…' : '打开文件夹'),
                h('button', {
                  type: 'button', 'data-ql-btn': '', 'data-ql-variant': 'danger', disabled: !!importBusy,
                  onClick: () => removeImport(rec), title: '删除导入目录与记录',
                }, importBusy === 'delete:' + rec.id ? '删除中…' : '删除'),
              ),
            )),
          ),
        ))
        : null

      return h('div', { 'data-ql-root': '', 'data-dsh-plugin': 'dsh-quantlab', 'data-dsh-part': 'quantlab-main-panel' },
        bar,
        msg && frame ? h('div', { style: { padding: '4px 12px', flex: '0 0 auto' } },
          h('div', { 'data-ql-msg': '', 'data-ql-tone': msg.tone || undefined }, msg.text)) : null,
        h('div', { 'data-ql-body': '' }, frame, card, drawer, importsDrawer),
      )
    }

    // ─── 侧栏图标（QuantLab Bot：机器人头 + 蜡烛图眼睛） ──────────────────
    function QuantLabIcon(props) {
      const size = props && typeof props.size === 'number' && props.size > 0 ? props.size : 16
      return h('svg', {
        width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
        'aria-hidden': 'true', focusable: 'false',
        'data-dsh-plugin': 'dsh-quantlab', 'data-dsh-part': 'quantlab-sidebar-icon',
        style: { display: 'block' },
      },
        // 头（用 currentColor 的淡填充 + 描边，深浅色主题都清楚）
        h('rect', {
          x: 2.5, y: 3.25, width: 19, height: 17.5, rx: 5.2,
          fill: 'currentColor', opacity: 0.16, stroke: 'currentColor', strokeWidth: 1.1,
        }),
        // 面罩
        h('rect', {
          x: 4.6, y: 5.85, width: 14.8, height: 10.6, rx: 3.4,
          fill: 'none', stroke: 'currentColor', strokeWidth: 0.9, opacity: 0.55,
        }),
        // 眼睛：两根蜡烛（影线 + 实体），和 Assets 里的 bot logo 一致
        h('path', { d: 'M8.6 6.9v8.6', stroke: 'currentColor', strokeWidth: 0.7, opacity: 0.8 }),
        h('path', { d: 'M15.4 6.9v8.6', stroke: 'currentColor', strokeWidth: 0.7, opacity: 0.8 }),
        h('rect', { x: 6.7, y: 8.8, width: 3.8, height: 4.8, rx: 0.8, fill: 'currentColor' }),
        h('rect', { x: 13.5, y: 9.5, width: 3.8, height: 3.6, rx: 0.8, fill: 'currentColor' }),
      )
    }

    // ─── 设置 → QuantLab ────────────────────────────────────────────────
    function QuantLabSettings() {
      const [cfg, setCfg] = React.useState(null)
      const [form, setForm] = React.useState({ projectDir: '', autoStart: true, llmBridge: true })
      const [loading, setLoading] = React.useState(true)
      const [busy, setBusy] = React.useState('')
      const [msg, setMsg] = React.useState(null)
      const [dirty, setDirty] = React.useState(false)
      const msgTimer = React.useRef(null)

      const flash = (text, tone) => {
        setMsg({ text, tone: tone || '' })
        if (msgTimer.current) clearTimeout(msgTimer.current)
        msgTimer.current = setTimeout(() => setMsg(null), 6000)
      }

      const fromConfig = (data) => {
        const c = data && typeof data === 'object' && data.config && typeof data.config === 'object' ? data.config : data
        if (!c || typeof c !== 'object') return null
        return {
          projectDir: typeof c.projectDir === 'string' ? c.projectDir : '',
          autoStart: c.autoStart !== false,
          llmBridge: c.llmBridge !== false,
          python: typeof c.python === 'string' ? c.python : '',
          srcDir: typeof c.srcDir === 'string' ? c.srcDir : '',
          model: typeof c.model === 'string' ? c.model : '',
        }
      }

      const load = React.useCallback(async () => {
        setLoading(true)
        try {
          const data = await getJson(API + '/config')
          const c = fromConfig(data)
          if (!c) throw new Error('配置返回为空')
          setCfg(c)
          setForm({ projectDir: c.projectDir, autoStart: c.autoStart, llmBridge: c.llmBridge })
          setDirty(false)
        } catch (e) {
          setCfg(null)
          setMsg({ text: '读取配置失败：' + errorText(e) + '（插件宿主可能没加载）', tone: 'error' })
        } finally {
          setLoading(false)
        }
      }, [])

      React.useEffect(() => { load() }, [load])

      const setField = (key, value) => {
        setForm((f) => ({ ...f, [key]: value }))
        setDirty(true)
      }

      const save = async () => {
        setBusy('save')
        try {
          const data = await postJson(API + '/config', {
            projectDir: form.projectDir,
            autoStart: !!form.autoStart,
            llmBridge: !!form.llmBridge,
          })
          const c = fromConfig(data)
          if (c) setCfg(c)
          setDirty(false)
          const st = normalizeStatus(data)
          if (st && st.state === 'starting') flash('已保存，后端正在重启…', 'ok')
          else if (st && st.state === 'running') flash('已保存，后端已重启并生效', 'ok')
          else flash('已保存 · 重启后端后生效', 'ok')
        } catch (e) {
          flash('保存失败：' + errorText(e), 'error')
        } finally {
          setBusy('')
        }
      }

      const restart = async () => {
        setBusy('restart')
        try {
          const data = await postJson(API + '/restart', {})
          const st = normalizeStatus(data)
          flash(st && st.state === 'starting' ? '正在重启后端…' : '已请求重启后端', 'ok')
        } catch (e) {
          flash('重启失败：' + errorText(e), 'error')
        } finally {
          setBusy('')
        }
      }

      if (loading && !cfg) {
        return h('div', { 'data-ql-settings': '' },
          h('div', { 'data-ql-sec': '' },
            h('div', { 'data-ql-h': '' }, 'QuantLab'),
            h('div', { 'data-ql-note': '' }, '正在读取配置…')),
          msg ? h('div', { 'data-ql-msg': '', 'data-ql-tone': msg.tone || undefined }, msg.text) : null,
        )
      }

      return h('div', { 'data-ql-settings': '', 'data-dsh-plugin': 'dsh-quantlab', 'data-dsh-part': 'quantlab-settings' },
        h('div', { 'data-ql-sec': '' },
          h('div', { 'data-ql-h': '' }, 'QuantLab 本地后端'),
          h('div', { 'data-ql-note': '' },
            'QuantLab 的 Python 后端由 DSH 插件管理：面板里的 iframe 指向它，DSH 会话通过它查行情、跑回测。'),
          h('div', { 'data-ql-row': '' },
            h('div', { 'data-ql-grow': '' },
              h('label', { 'data-ql-label': '', htmlFor: 'ql-projectDir' }, '项目目录'),
              h('input', {
                id: 'ql-projectDir', 'data-ql-input': '', type: 'text',
                value: form.projectDir,
                placeholder: '默认 <QuantLab 仓库>/.dsh-project',
                onChange: (e) => setField('projectDir', e.target.value),
              }),
              h('div', { 'data-ql-note': '' },
                '平台的项目文件（策略、回测记录、研究存档）落在这个目录；留空则用插件默认值。改动重启后端后生效。'),
            )),
          h('div', { 'data-ql-row': '' },
            h('div', { 'data-ql-grow': '' },
              h('label', { 'data-ql-check': '' },
                h('input', {
                  type: 'checkbox', checked: !!form.autoStart,
                  onChange: (e) => setField('autoStart', e.target.checked),
                }),
                h('span', null, 'DSH 启动时自动拉起 QuantLab 后端')),
              h('div', { 'data-ql-note': '' }, '关掉后需要到侧栏 QuantLab 面板里手动点「启动」。'),
            )),
          h('div', { 'data-ql-row': '' },
            h('div', { 'data-ql-grow': '' },
              h('label', { 'data-ql-check': '' },
                h('input', {
                  type: 'checkbox', checked: !!form.llmBridge,
                  onChange: (e) => setField('llmBridge', e.target.checked),
                }),
                h('span', null, '启用模型桥（AI 研究室走 DSH 当前模型）')),
              h('div', { 'data-ql-note': '' },
                '打开后插件把 /dsh-quantlab/llm/v1 登记成平台的「DSH · Harness」模型服务；关掉则平台不自动登记模型。'),
            )),
          cfg
            ? h('div', { 'data-ql-kv': '' },
              'Python：' + (cfg.python || '(默认 python)')
              + (cfg.srcDir ? ' · 源码目录：' + cfg.srcDir : '')
              + (cfg.model ? ' · 模型：' + cfg.model : ''))
            : null,
        ),
        h('div', { 'data-ql-actions': '' },
          h('button', {
            type: 'button', 'data-ql-btn': '', 'data-ql-variant': 'primary',
            disabled: !!busy || !dirty, onClick: save,
          }, busy === 'save' ? '保存中…' : '保存'),
          h('button', {
            type: 'button', 'data-ql-btn': '',
            disabled: !!busy, onClick: restart,
          }, busy === 'restart' ? '重启中…' : '立即重启后端'),
          h('button', { type: 'button', 'data-ql-btn': '', disabled: !!busy, onClick: load }, '重新读取'),
          dirty ? h('span', { 'data-ql-note': '' }, '有未保存的改动') : null,
        ),
        h('div', { 'data-ql-note': '' },
          '提示：保存只会更新配置；项目目录等改动要重启后端才生效（按上面的「立即重启后端」，或到面板里点「重启」）。'),
        msg ? h('div', { 'data-ql-msg': '', 'data-ql-tone': msg.tone || undefined }, msg.text) : null,
      )
    }

    // ─── 原生对话里的回测结果卡片（tool.call.toolview keyed slot） ────────
    /**
     * DSH 的工具视图按 wire tool 名 keyed 注册：宿主（lib/tools.js）在结果
     * markdown 末尾留一行 `<!-- quantlab-card {...} -->`，这里解析后画
     * 「净值缩略图 + 关键指标 + 在 QuantLab 打开 / 看图 / 下载」。
     *
     * props 契约（来自 dsh-client-ui-tool 的 slots.ts）：
     *   ToolCallCommonProps & ({phase:'preparing'|'start'|'result', block})
     *   block.args 是懒读视图：block.args.value(key)
     *   result 阶段 block.content 是内容块数组、block.isError / block.error
     */
    function makeReportCard({ openPanel } = {}) {
      const openInPanel = (route) => {
        const target = String(route || '')
        // 面板可能还没挂载：先存路由（面板首次挂载时消费），再发事件给已挂载的面板。
        stashPendingRoute(target)
        try {
          if (typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent('dsh:quantlab-open', { detail: { route: target } }))
          }
        } catch (_) { /* ignore */ }
        try {
          if (typeof openPanel === 'function') openPanel()
        } catch (_) { /* ignore */ }
      }

      const openInTab = (url) => {
        const target = String(url || '').trim()
        if (!target) return
        try {
          if (typeof window !== 'undefined' && typeof window.open === 'function') {
            const win = window.open(target, '_blank', 'noopener,noreferrer')
            if (win) return
          }
        } catch (_) { /* ignore */ }
        try { if (typeof window !== 'undefined') window.location.assign(target) } catch (_) { /* ignore */ }
      }

      const numberish = (value, suffix, digits) => {
        const num = Number(value)
        if (!Number.isFinite(num)) return value === undefined || value === null || value === '' ? null : String(value)
        const text = digits === undefined ? String(num) : num.toFixed(digits)
        return suffix ? text + suffix : text
      }

      /** 百分比：`*Pct` 已是百分数；只有比例字段（total_return=0.0325）时 ×100 免得显示成 0.03%。 */
      const percentish = (pctValue, ratioValue) => {
        if (pctValue !== undefined && pctValue !== null && pctValue !== '') return numberish(pctValue, '%', 2)
        if (ratioValue !== undefined && ratioValue !== null && ratioValue !== '') {
          const num = Number(ratioValue)
          return Number.isFinite(num) ? numberish(num * 100, '%', 2) : String(ratioValue)
        }
        return null
      }

      return function QuantLabReportCard(props) {
        // Hooks 必须在所有 return 之前调用（同实例每次渲染顺序一致）。
        const [view, setView] = React.useState('summary')

        const toolName = String((props && props.toolName) || 'quantlab_export_report')
        const block = props && props.block
        const phase = (props && props.phase) || ''
        const readArg = (key) => {
          try {
            const args = block && block.args
            if (!args || typeof args.value !== 'function') return undefined
            return args.value(key)
          } catch (_) {
            return undefined
          }
        }
        const backtestId = readArg('backtest_id') ?? readArg('backtestId')
        const heading = 'QuantLab 回测报告' + (backtestId !== undefined && backtestId !== null && backtestId !== ''
          ? ' · #' + String(backtestId)
          : '')

        if (phase === 'preparing' || phase === 'start') {
          return h('div', { 'data-ql-reportrow': '', 'data-ql-toolview': toolName },
            h('span', { 'data-ql-spin': '', 'aria-hidden': 'true' }),
            h('span', null, heading + (phase === 'preparing' ? ' · 准备中…' : ' · 导出中…')))
        }

        const text = contentText(block)
        let card = parseCardJson(text)
        if (!card) {
          try {
            const parsed = JSON.parse(text)
            card = parsed && parsed.quantlabCard ? parsed.quantlabCard : parsed
          } catch (_) { card = null }
        }
        const failed = (block && block.isError === true) || Boolean(block && block.error) || (card && card.ok === false)
        if (failed) {
          const detail = (block && block.error && (block.error.message || block.error))
            || (card && (card.error || card.message)) || text || '导出失败'
          return h('div', {
            'data-ql-reportcard': '', 'data-ql-report-error': '', 'data-ql-toolview': toolName,
          },
          h('div', { 'data-ql-reporttitle': '' }, heading),
          h('div', { 'data-ql-note': '' }, String(detail).replace(/\s+/g, ' ').slice(0, 400)))
        }

        const chartUrl = (card && (card.chartUrl || card.chart)) || ''
        const downloadUrl = (card && (card.downloadUrl || card.pdfUrl || card.reportUrl)) || ''
        // 平台原生页面（回测详情）：工具会带 panelUrl；没有时退回面板路由。
        const panelUrl = (card && (card.panelUrl || card.url)) || ''
        // 旧卡片里可能存的是错的复数字路由 `#/backtests/<id>`（会落到列表页），这里纠正成单数。
        const rawPanelRoute = (card && card.panelRoute) || ''
        const panelRoute = (rawPanelRoute
          ? rawPanelRoute.replace(/^#?\/?backtests\/([A-Za-z0-9_-]+)$/, '#/backtest/$1')
          : '')
          || (backtestId !== undefined && backtestId !== null && backtestId !== ''
            ? backtestRoute(backtestId)
            : '')
        const nativeSrc = panelUrl ? buildFrameUrl(panelUrl, detectTheme(), '') : ''
        const metrics = (card && card.metrics) || {}
        const items = [
          ['总收益', percentish(metrics.totalReturnPct, metrics.totalReturn)],
          ['夏普', numberish(metrics.sharpe, '', 2)],
          ['最大回撤', percentish(metrics.maxDrawdownPct, metrics.maxDrawdown)],
          ['交易', numberish(metrics.trades)],
        ].filter((pair) => pair[1] !== null && pair[1] !== undefined)

        // ① 平台原生视图：直接 iframe 平台自己的回测详情页（embed 模式），和平台里看到的一样。
        if (view === 'native' && nativeSrc) {
          return h('div', {
            'data-ql-reportcard': '', 'data-ql-report-native': '', 'data-ql-toolview': toolName,
          },
          h('div', { 'data-ql-reportrow': '' },
            h('span', { 'data-ql-reporttitle': '' }, heading),
            h('span', { 'data-ql-spacer': '' }),
            h('button', {
              type: 'button', 'data-ql-btn': '', onClick: () => setView('summary'),
            }, '收起平台视图'),
            panelUrl
              ? h('button', {
                type: 'button', 'data-ql-btn': '', 'data-ql-variant': 'primary',
                onClick: () => openInTab(panelUrl),
              }, '在新标签打开')
              : null),
          h('iframe', {
            'data-ql-reportframe': '',
            src: nativeSrc,
            title: heading,
            loading: 'lazy',
            referrerPolicy: 'no-referrer',
          }))
        }

        // ② 摘要视图（默认）
        return h('div', { 'data-ql-reportcard': '', 'data-ql-toolview': toolName },
          h('div', { 'data-ql-reportrow': '' }, h('span', { 'data-ql-reporttitle': '' }, heading)),
          chartUrl
            ? h('img', {
              'data-ql-reportthumb': '',
              ...(nativeSrc ? { 'data-ql-clickable': '' } : {}),
              src: chartUrl,
              alt: '回测净值曲线',
              loading: 'lazy',
              onClick: nativeSrc ? () => setView('native') : undefined,
            })
            : null,
          card && card.summary ? h('div', { 'data-ql-note': '' }, String(card.summary).slice(0, 300)) : null,
          items.length
            ? h('div', { 'data-ql-reportmetrics': '' },
              items.map(([label, value]) => h('span', { key: label }, label + ' ', h('b', null, String(value)))))
            : null,
          h('div', { 'data-ql-actions': '' },
            nativeSrc
              ? h('button', {
                type: 'button', 'data-ql-btn': '', 'data-ql-variant': 'primary', 'data-ql-open-native': '',
                onClick: () => setView('native'),
              }, '平台原生视图')
              : null,
            panelUrl
              ? h('button', {
                type: 'button', 'data-ql-btn': '', 'data-ql-open-tab': '',
                onClick: () => openInTab(panelUrl),
              }, '打开回测详情')
              : null,
            panelRoute
              ? h('button', {
                type: 'button', 'data-ql-btn': '',
                'data-ql-open-panel': '', onClick: () => openInPanel(panelRoute),
              }, '在侧栏打开')
              : null,
            chartUrl
              ? h('a', {
                'data-ql-btn': '', href: chartUrl, target: '_blank', rel: 'noreferrer',
              }, '查看大图')
              : null,
            downloadUrl
              ? h('a', {
                'data-ql-btn': '', href: downloadUrl, target: '_blank', rel: 'noreferrer',
              }, card && card.format === 'pdf' ? '下载 PDF' : '下载报告')
              : null,
          ))
      }
    }

    // ─── 插件入口 ────────────────────────────────────────────────────────
    return {
      inject: ['slots'],
      apply(ctx) {
        // 样式：<style data-plugin-css>，随插件卸载移除。
        try {
          if (typeof document !== 'undefined') {
            const tagId = 'dsh-quantlab/client.css'
            let tag = document.querySelector('style[data-plugin-css=' + JSON.stringify(tagId) + ']')
            if (!tag) {
              tag = document.createElement('style')
              tag.setAttribute('data-plugin-css', tagId)
              document.head.appendChild(tag)
            }
            if (tag.textContent !== CSS) tag.textContent = CSS
            if (typeof ctx.effect === 'function') {
              ctx.effect(() => () => tag.remove(), 'dsh-quantlab: 面板样式')
            }
          }
        } catch (error) {
          console.warn('dsh-quantlab: 注入样式失败', error)
        }

        let slots
        try {
          slots = (typeof ctx.get === 'function' ? ctx.get('slots') : undefined) || ctx.slots
        } catch (_) {
          slots = ctx.slots
        }
        if (!slots || typeof slots.register !== 'function') {
          console.warn('dsh-quantlab: 当前宿主没有 slots 服务，面板不注册')
          return
        }

        /**
         * 读一个「可选的」宿主服务。cordis 的 ctx 代理在服务没 inject 时访问属性会直接抛
         * `cannot get property "xxx" without inject` —— 这里把两种读法都包在 try 里，
         * 拿不到就返回 undefined（面板随后只用文字提示，不报错）。
         */
        const readOptionalService = (name) => {
          try {
            if (typeof ctx.get === 'function') {
              const value = ctx.get(name, false)
              if (value !== undefined && value !== null) return value
            }
          } catch (_) { /* 未 inject，继续用属性探测 */ }
          try {
            const value = ctx[name]
            if (value !== undefined && value !== null) return value
          } catch (_) { /* ignore */ }
          return undefined
        }

        /** 尽量把「设置」打开到 QuantLab 分区；宿主没暴露入口就退回文字提示。返回是否打开了。 */
        const openSettings = () => {
          const layout = readOptionalService('layout')
          const settingsService = readOptionalService('settings')
          const candidates = [
            layout && layout.openSettings,
            settingsService && settingsService.open,
            layout && layout.selectSettings,
          ]
          for (let i = 0; i < candidates.length; i += 1) {
            const fn = candidates[i]
            if (typeof fn !== 'function') continue
            const receiver = i === 1 ? settingsService : layout
            try {
              fn.call(receiver)
              return true
            } catch (_) { /* 试下一个入口 */ }
          }
          if (typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent('dsh:open-settings', { detail: { section: 'quantlab' } }))
          }
          return false
        }

        /** 切到侧栏 QuantLab 面板（宿主有 layout 服务才行；没有就只靠事件）。返回是否切了。 */
        const openPanel = () => {
          const layout = readOptionalService('layout')
          try {
            if (layout && typeof layout.selectPanel === 'function') {
              layout.selectPanel('quantlab')
              return true
            }
          } catch (_) { /* ignore */ }
          return false
        }

        const inject = (name, cb) => (typeof slots.inject === 'function' ? slots.inject(name, cb) : cb())

        // 照 dsh-mnemon 的 native panel 写法：先声明 main，再在它里面挂侧栏图标，
        // 两者的 id/key 都是 `quantlab`（侧栏行按 id 寻址 main 的 key）。
        inject('main', () => inject('sidebar.panellist', () => {
          const stopMain = slots.register({
            name: 'main',
            key: 'quantlab',
            inject: () => ({ openSettings, openPanel }),
          }, QuantLabPanel)

          let stopIcon
          try {
            stopIcon = slots.register({
              name: 'sidebar.panellist',
              id: 'quantlab',
              order: 32,
              label: () => 'QuantLab',
            }, QuantLabIcon)
          } catch (error) {
            stopIcon = undefined
            stopMain()
            throw error
          }

          return () => {
            if (typeof stopIcon === 'function') stopIcon()
            if (typeof stopMain === 'function') stopMain()
          }
        }))

        // 设置分区：DSH 设置里的「QuantLab」卡片。
        inject('settings.section', () => slots.register({
          name: 'settings.section',
          id: 'quantlab',
          order: 43,
          label: 'QuantLab',
        }, QuantLabSettings))

        // 原生对话里的工具卡片：quantlab_export_report / quantlab_backtest 的结果
        // 用「净值缩略图 + 指标 + 在面板打开」代替默认 JSON 行。
        // keyed slot 按 wire tool 名精确匹配；DSH 没这个 slot 时 inject 会跳过（不影响其它功能）。
        const ReportCard = makeReportCard({ openPanel })
        for (const toolName of ['quantlab_export_report', 'quantlab_backtest']) {
          try {
            inject('tool.call.toolview', () => slots.register({
              name: 'tool.call.toolview',
              key: toolName,
            }, ReportCard))
          } catch (error) {
            console.warn('dsh-quantlab: 注册工具卡片失败（' + toolName + '）', error)
          }
        }
      },
    }
  },
})
