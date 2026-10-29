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

    const postJson = async (url, body) => {
      const res = await fetch(url, {
        method: 'POST',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(body || {}),
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

    // ─── main 面板 ───────────────────────────────────────────────────────
    const actFailed = (name) => name === 'start' ? '启动失败' : name === 'stop' ? '停止失败' : '重启失败'

    function QuantLabPanel(props) {
      const openSettings = props && props.openSettings
      const [status, setStatus] = React.useState(null)
      const [apiError, setApiError] = React.useState('')
      const [busy, setBusy] = React.useState('')
      const [msg, setMsg] = React.useState(null)          // {text, tone}
      const [frameNonce, setFrameNonce] = React.useState(0)
      const [logsOpen, setLogsOpen] = React.useState(false)
      const [logs, setLogs] = React.useState('')
      const [logsError, setLogsError] = React.useState('')

      const statusRef = React.useRef(null)
      const logsOpenRef = React.useRef(false)
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

      // 内嵌原平台界面
      const frame = running
        ? h('iframe', {
          key: (status.url || '') + '#' + frameNonce,
          'data-ql-frame': '',
          src: status.url,
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

      return h('div', { 'data-ql-root': '', 'data-dsh-plugin': 'dsh-quantlab', 'data-dsh-part': 'quantlab-main-panel' },
        bar,
        msg && frame ? h('div', { style: { padding: '4px 12px', flex: '0 0 auto' } },
          h('div', { 'data-ql-msg': '', 'data-ql-tone': msg.tone || undefined }, msg.text)) : null,
        h('div', { 'data-ql-body': '' }, frame, card, drawer),
      )
    }

    // ─── 侧栏图标 ────────────────────────────────────────────────────────
    function QuantLabIcon(props) {
      const size = props && typeof props.size === 'number' && props.size > 0 ? props.size : 16
      return h('svg', {
        width: size, height: size, viewBox: '0 0 24 24', fill: 'none',
        'aria-hidden': 'true', focusable: 'false',
        'data-dsh-plugin': 'dsh-quantlab', 'data-dsh-part': 'quantlab-sidebar-icon',
        style: { display: 'block' },
      },
        h('rect', { x: 1.75, y: 1.75, width: 20.5, height: 20.5, rx: 5.25, fill: 'currentColor', opacity: 0.14 }),
        h('path', {
          d: 'M5.2 16.4 9.6 10.6 13.1 13.6 18.4 6.9',
          stroke: 'currentColor', strokeWidth: 1.9, strokeLinecap: 'round', strokeLinejoin: 'round',
        }),
        h('circle', { cx: 18.4, cy: 6.9, r: 1.55, fill: 'currentColor' }),
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

        const inject = (name, cb) => (typeof slots.inject === 'function' ? slots.inject(name, cb) : cb())

        // 照 dsh-mnemon 的 native panel 写法：先声明 main，再在它里面挂侧栏图标，
        // 两者的 id/key 都是 `quantlab`（侧栏行按 id 寻址 main 的 key）。
        inject('main', () => inject('sidebar.panellist', () => {
          const stopMain = slots.register({
            name: 'main',
            key: 'quantlab',
            inject: () => ({ openSettings }),
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
      },
    }
  },
})
