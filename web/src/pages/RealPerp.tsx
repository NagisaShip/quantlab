/** 真实永续：独立开关、专用 Key、期货账户自检、启动确认码、自动急停。 */
import { useCallback, useEffect, useState } from 'react'
import { get, post } from '../http'
import { Markdown } from '../components/markdown'
import { Callout, EmptyState, Field, Heading, Loading, Modal, Panel, Tag } from '../components/ui'
import { v3, type StrategyFile } from '../api/research'
import type { PageProps } from './types'

interface Status {
  enabled: boolean; killed: boolean; ready: boolean; blockers: string[]; notice: string; notice_current: string; notice_version: string
  ack_phrase: string; start_phrase: string; server_enabled: boolean; hard_ceiling: number; supported_exchanges: string[]
}
interface Conn { id: string; exchange: string; label: string; key_mask: string; status: string; env_label: string
  last_test?: { ok?: boolean; balances?: Record<string, string>; positions?: Record<string, string>; error?: string } }
interface Sess { id: string; strategy_name?: string; strategy_id?: string; status?: string; run_state?: string; initial_cash?: number }
interface Overview { status: Status; connections: Conn[]; environments: { exchange: string; label: string; host: string }[]; sessions: Sess[] }
interface Challenge { challenge_id: string; code: string; expires_at: number; summary: Record<string, string | number> }

const emptyConn = { exchange: 'binance', label: '', api_key: '', api_secret: '', passphrase: '' }

export default function RealPerpPage({ notify, onNavigate, isAdmin }: PageProps & { isAdmin: boolean }) {
  const [d, setD] = useState<Overview | null>(null)
  const [strategies, setStrategies] = useState<StrategyFile[]>([])
  const [busy, setBusy] = useState('')
  const [ack, setAck] = useState('')
  const [conn, setConn] = useState(emptyConn)
  const [launch, setLaunch] = useState(false)
  const [challenge, setChallenge] = useState<Challenge | null>(null)
  const [code, setCode] = useState('')
  const [phrase, setPhrase] = useState('')
  const [form, setForm] = useState({ slug: '', connection_id: '', symbol: 'BTC/USDT:PERP', initial_cash: 300, margin_mode: 'isolated', position_mode: 'one-way', leverage: 5, stop_loss: 0.05, max_daily_loss: 0.03, max_order_notional: 100 })
  const load = useCallback(() => {
    get<Overview>('/api/realperp').then(setD).catch((e) => notify(e.message, true))
    v3.strategies().then((r) => setStrategies(r.strategies)).catch(() => undefined)
  }, [notify])
  useEffect(load, [load])
  if (!d) return <Loading />
  const st = d.status
  const act = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key)
    try { await fn(); notify(ok); load() } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }
  const prepare = async () => {
    setBusy('prepare')
    try {
      const r = await post<Challenge & { challenge_id: string; code: string }>('/api/realperp/prepare', form)
      setChallenge(r); setCode(''); setPhrase('')
    } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }
  const start = async () => {
    if (!challenge) return
    setBusy('start')
    try {
      const r = await post<{ run_id: string }>('/api/realperp/start', { challenge_id: challenge.challenge_id, code, phrase })
      notify('真实永续实验已登记'); setLaunch(false); setChallenge(null); load(); if (r.run_id) onNavigate(`paper/${r.run_id}`)
    } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }
  return (
    <>
      <Heading title="真实永续" description="独立于微实盘的真实资金通道：仅 Binance/OKX 线性 USDT-M，逐仓或全仓、单向或对冲，启动前必须完成 Key、期货账户、敞口与止损检查。"
        actions={(st.enabled || d.sessions.length > 0) && <button className="button danger" disabled={!isAdmin} onClick={() => act('kill', () => post('/api/realperp/kill', {}), '已急停')}>一键急停并平仓</button>} />
      <div className={`ml-banner ${st.killed ? 'bad' : st.ready ? 'ok' : 'warn'}`}>
        <strong>{st.killed ? '已急停' : st.ready ? '可以准备启动' : '还不能启动'}</strong>
        {st.blockers.length > 0 && <ul>{st.blockers.map((b) => <li key={b}>{b}</li>)}</ul>}
      </div>
      <div className="stack">
        {(!st.enabled || st.notice_version !== st.notice_current) && (
          <Panel title="真实永续风险告知" subtitle="这份确认与现货微实盘独立；告知改版后需要重新确认。">
            <Markdown text={st.notice} />
            {isAdmin ? <div className="filter-row">
              <input value={ack} onChange={(e) => setAck(e.target.value)} placeholder={`输入「${st.ack_phrase}」`} />
              <button className="button primary" disabled={ack.trim() !== st.ack_phrase || !!busy}
                onClick={() => act('enable', () => post('/api/realperp/enable', { phrase: ack, notice_version: st.notice_current }), '已打开真实永续')}>确认并打开</button>
            </div> : <p className="hint">只有工作区管理员能确认。</p>}
          </Panel>
        )}
        {st.killed && isAdmin && <Panel title="解除急停"><div className="filter-row"><input value={ack} onChange={(e) => setAck(e.target.value)} placeholder={`输入「${st.ack_phrase}」`} /><button className="button" disabled={ack.trim() !== st.ack_phrase} onClick={() => act('rearm', () => post('/api/realperp/rearm', { phrase: ack }), '已解除急停')}>解除急停</button></div></Panel>}

        <Panel title="专用 Key" subtitle="这是独立密钥目录，不会复用微实盘连接。Key 必须仅交易、禁提现/划转、绑定 IP。">
          {d.connections.length === 0 ? <EmptyState title="还没有真实永续连接">先接入 Binance 或 OKX 的 USDT-M 交易 Key。</EmptyState> : <table><thead><tr><th>交易所</th><th>标签</th><th>Key</th><th>状态</th><th>余额</th></tr></thead><tbody>{d.connections.map((c) => <tr key={c.id}><td>{c.exchange}</td><td>{c.label}</td><td className="mono">{c.key_mask}</td><td><Tag tone={c.status === 'active' ? 'ok' : 'bad'}>{c.status}</Tag></td><td>{Object.entries(c.last_test?.balances || {}).slice(0, 3).map(([k, v]) => `${k} ${Number(v).toFixed(2)}`).join(' · ') || '-'}</td></tr>)}</tbody></table>}
          {isAdmin && <div className="bt-form" style={{ marginTop: 12 }}>
            <label>交易所<select value={conn.exchange} onChange={(e) => setConn({ ...conn, exchange: e.target.value })}>{st.supported_exchanges.map((x) => <option key={x}>{x}</option>)}</select></label>
            <label>标签<input value={conn.label} onChange={(e) => setConn({ ...conn, label: e.target.value })} placeholder="可空" /></label>
            <label>API Key<input value={conn.api_key} onChange={(e) => setConn({ ...conn, api_key: e.target.value })} /></label>
            <label>API Secret<input type="password" value={conn.api_secret} onChange={(e) => setConn({ ...conn, api_secret: e.target.value })} /></label>
            {conn.exchange === 'okx' && <label>Passphrase<input type="password" value={conn.passphrase} onChange={(e) => setConn({ ...conn, passphrase: e.target.value })} /></label>}
            <button className="button primary" disabled={!!busy || !conn.api_key || !conn.api_secret} onClick={() => act('connect', () => post('/api/realperp/connections', conn), '已保存并完成自检')}>自检并保存</button>
          </div>}
        </Panel>

        <Panel title="启动真实永续" subtitle="先生成摘要和 6 位确认码；确认后还要通过一次实时自检。">
          {d.sessions.length > 0 && <Callout tone="warn">已有 {d.sessions.length} 个真实永续实验在运行。每个策略同一时间只允许一个。</Callout>}
          <div className="bt-form">
            <label>策略<select value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })}><option value="">选择已毕业策略</option>{strategies.map((s) => <option key={s.slug} value={s.slug}>{s.name}</option>)}</select></label>
            <label>连接<select value={form.connection_id} onChange={(e) => setForm({ ...form, connection_id: e.target.value })}><option value="">选择专用 Key</option>{d.connections.filter((c) => c.status === 'active').map((c) => <option key={c.id} value={c.id}>{c.exchange} · {c.key_mask}</option>)}</select></label>
            <label>合约<input value={form.symbol} onChange={(e) => setForm({ ...form, symbol: e.target.value.toUpperCase() })} /></label>
            <label>本次资金<input type="number" min={10} max={st.hard_ceiling} value={form.initial_cash} onChange={(e) => setForm({ ...form, initial_cash: Number(e.target.value) })} /></label>
            <label>保证金<select value={form.margin_mode} onChange={(e) => setForm({ ...form, margin_mode: e.target.value })}><option value="isolated">逐仓</option><option value="cross">全仓</option></select></label>
            <label>持仓模式<select value={form.position_mode} onChange={(e) => setForm({ ...form, position_mode: e.target.value })}><option value="one-way">单向</option><option value="hedge">对冲</option></select></label>
            <label>最大杠杆<input type="number" min={1} max={50} value={form.leverage} onChange={(e) => setForm({ ...form, leverage: Number(e.target.value) })} /></label>
            <label>单笔名义上限<input type="number" min={1} value={form.max_order_notional} onChange={(e) => setForm({ ...form, max_order_notional: Number(e.target.value) })} /></label>
            <label>止损（%）<input type="number" min={0.5} max={20} step={0.5} value={form.stop_loss * 100} onChange={(e) => setForm({ ...form, stop_loss: Number(e.target.value) / 100 })} /></label>
            <label>日亏熔断（%）<input type="number" min={0.5} max={20} step={0.5} value={form.max_daily_loss * 100} onChange={(e) => setForm({ ...form, max_daily_loss: Number(e.target.value) / 100 })} /></label>
            <button className="button primary" disabled={!isAdmin || !form.slug || !form.connection_id || !!busy} onClick={() => { setLaunch(true); prepare() }}>生成启动摘要</button>
          </div>
        </Panel>
      </div>

      {launch && <Modal title="确认真实永续启动" onClose={() => setLaunch(false)}>
        {!challenge ? <Loading /> : <>
          <dl className="kv">
            <dt>策略</dt><dd>{String(challenge.summary.name)}（{String(challenge.summary.symbol)}）</dd>
            <dt>交易所</dt><dd>{String(challenge.summary.exchange)} · {String(challenge.summary.key_mask)}</dd>
            <dt>模式</dt><dd>{String(challenge.summary.margin_mode)} / {String(challenge.summary.position_mode)} / {String(challenge.summary.leverage)}x</dd>
            <dt>资金</dt><dd>{String(challenge.summary.initial_cash)} USDT；单笔上限 {String(challenge.summary.max_order_notional)}</dd>
            <dt>止损 / 日亏</dt><dd>{(Number(challenge.summary.stop_loss) * 100).toFixed(1)}% / {(Number(challenge.summary.max_daily_loss) * 100).toFixed(1)}%</dd>
          </dl>
          <Callout tone="fail">这会使用真实资金并可能产生强平损失。确认码 <strong className="mono">{challenge.code}</strong>，5 分钟内有效。</Callout>
          <div className="two-columns even">
            <Field label="确认码"><input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" maxLength={6} autoFocus /></Field>
            <Field label={`输入「${st.start_phrase}」`}><input value={phrase} onChange={(e) => setPhrase(e.target.value)} /></Field>
          </div>
          <button className="button danger" disabled={busy === 'start' || code.length !== 6 || phrase.trim() !== st.start_phrase} onClick={start}>确认真实永续并启动</button>
        </>}
      </Modal>}
    </>
  )
}
