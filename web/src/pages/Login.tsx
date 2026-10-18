import { useEffect, useState } from 'react'
import { v4 } from '../api/platform'
import Landing from './landing/Landing'
import { ApiError } from '../http'

type Mode = 'login' | 'register'

/** 登录 / 注册。第一位注册者成为部署管理员；之后注册需要邀请码（除非部署开放注册）。 */
export default function LoginPage({ onDone }: { onDone: () => void }) {
  const [status, setStatus] = useState<{ bootstrapped: boolean; open_registration: boolean } | null>(null)
  const [mode, setMode] = useState<Mode>('login')
  const [email, setEmail] = useState('') // 登录时是「用户名或邮箱」，注册时是可选的邮箱
  const [username, setUsername] = useState('')
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [invite, setInvite] = useState(() => new URLSearchParams(window.location.search).get('invite') ?? '')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  // T11：账号启用了双因素时，密码正确但没带验证码会返回 401 + details.totp_required。
  // 这时不当作错误，而是把界面切成"输入验证码"第二步，并复用同一个登录端点重发。
  const [needTotp, setNeedTotp] = useState(false)
  const [totp, setTotp] = useState('')

  useEffect(() => {
    v4.authStatus().then((s) => {
      setStatus(s)
      if (!s.bootstrapped || invite) setMode('register')
    }).catch(() => setStatus({ bootstrapped: true, open_registration: false }))
  }, [invite])

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setBusy(true)
    try {
      if (mode === 'register') await v4.register({ username: username.trim() || undefined, email: email.trim() || undefined, password, name, invite_code: invite || undefined })
      else await v4.login(email, password, totp || undefined)
      onDone()
    } catch (err) {
      if (err instanceof ApiError && err.details?.totp_required) {
        setNeedTotp(true)
        setTotp('')
      } else {
        setError(err instanceof ApiError ? err.message : (err as Error).message)
      }
    } finally {
      setBusy(false)
    }
  }

  const bootstrap = status && !status.bootstrapped
  const canRegister = status && (bootstrap || status.open_registration || invite)

  // 登录 / 注册卡片放在介绍页右侧的抽屉里；第一次部署（要初始化管理员）或带邀请链接时自动打开
  const card = (
        <div className="auth-card">
          {bootstrap ? (
            <>
              <h2>初始化管理员</h2>
              <p>这是本部署的第一个账号，将成为部署管理员，并获得一个独立工作区。</p>
            </>
          ) : needTotp ? (
            <>
              <h2>两步验证</h2>
              <p>该账号已启用双因素认证。请输入验证器当前显示的 6 位验证码，或一个未用过的恢复码。</p>
            </>
          ) : mode === 'login' ? (
            <>
              <h2>登录</h2>
              <p>使用工作区成员账号登录。会话保存在仅服务端可读的 Cookie 中。</p>
            </>
          ) : (
            <>
              <h2>注册</h2>
              <p>{status?.open_registration ? '本部署开放注册，注册后获得自己的工作区。' : '需要工作区管理员发给你的邀请码。'}</p>
            </>
          )}
          <form onSubmit={submit}>
            {needTotp ? (
              <>
                <label>
                  验证码
                  <input inputMode="numeric" autoComplete="one-time-code" autoFocus required
                    value={totp} onChange={(e) => setTotp(e.target.value.trim())} placeholder="6 位数字或恢复码" />
                </label>
                <div className="hint">以 {email} 继续登录。密码已校验通过，本步只补验证码。</div>
                {error && <div className="auth-error" role="alert">{error}</div>}
                <button className="button primary" disabled={busy || !totp}>{busy ? '请稍候…' : '验证并进入'}</button>
                <div className="auth-switch">
                  <button type="button" onClick={() => { setNeedTotp(false); setTotp(''); setError('') }}>换一个账号</button>
                </div>
              </>
            ) : (
              <>
                {mode === 'login' ? (
                  <label>用户名或邮箱<input autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
                ) : (
                  <>
                    <label>用户名<input autoComplete="username" required minLength={2} maxLength={32} pattern="[A-Za-z0-9_.\-一-鿿]{2,32}"
                      title="2–32 个字符：字母、数字、中文、下划线、点或短横线" value={username} onChange={(e) => setUsername(e.target.value)} /></label>
                    <label>邮箱<input type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="可选：之后也可以在账号设置里绑定" /></label>
                    <label>显示名称<input value={name} maxLength={40} onChange={(e) => setName(e.target.value)} placeholder="可选，默认同用户名" /></label>
                  </>
                )}
                <label>
                  密码
                  <input type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'} required minLength={8}
                    value={password} onChange={(e) => setPassword(e.target.value)} />
                </label>
                {mode === 'register' && !bootstrap && (
                  <label>邀请码<input value={invite} onChange={(e) => setInvite(e.target.value.trim())} placeholder={status?.open_registration ? '可选：加入已有工作区' : '必填'} /></label>
                )}
                {mode === 'register' && <div className="hint">密码至少 8 位，需同时包含字母与数字。</div>}
                {error && <div className="auth-error" role="alert">{error}</div>}
                <button className="button primary" disabled={busy || !status}>
                  {busy ? '请稍候…' : mode === 'login' ? '登录' : bootstrap ? '创建管理员并进入' : '注册并进入'}
                </button>
              </>
            )}
          </form>
          {!bootstrap && !needTotp && (
            <div className="auth-switch">
              {mode === 'login' ? (
                canRegister || status?.open_registration ? (
                  <>还没有账号？<button onClick={() => setMode('register')}>注册</button></>
                ) : (
                  <>还没有账号？<button onClick={() => setMode('register')}>凭邀请码注册</button></>
                )
              ) : (
                <>已有账号？<button onClick={() => setMode('login')}>登录</button></>
              )}
            </div>
          )}
        </div>
  )
  return <Landing auth={() => card} autoOpen={!!bootstrap || !!invite} />
}
