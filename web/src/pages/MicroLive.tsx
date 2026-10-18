/** 微实盘：真实资金、小额上限。开关与风险告知、实盘 Key 自检、资金档位、启动确认码、一键停止。 */
import { useCallback, useEffect, useState } from 'react'
import { get, post, request } from '../http'
import { Markdown } from '../components/markdown'
import { Callout, EmptyState, Field, Heading, Loading, Modal, Panel, Tag } from '../components/ui'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'
import { lang } from '../i18n'

interface Status {
  enabled: boolean; killed: boolean; ready: boolean; blockers: string[]; tiers: number[]; tier_days: number
  notice: string; notice_en?: string; notice_current: string; notice_version: string
  ack_phrase: string; ack_phrase_en?: string; start_phrase: string; start_phrase_en?: string
  server_enabled: boolean; hard_ceiling: number; blockers_en?: string[]
}
interface Conn { id: string; exchange: string; label: string; key_mask: string; status: string; env_label: string
  last_test?: { ok?: boolean; checks?: { name: string; ok: boolean; detail: string }[]; balances?: Record<string, string>; error?: string } }
interface TierRow { slug: string; name: string; cap: number; next_cap: number | null; can_raise: boolean; checks: { label: string; ok: boolean; value?: string }[] }
interface Sess { id: string; strategy_name?: string; strategy_id?: string; status?: string; run_state?: string; initial_cash?: number; equity_points?: { t: number; v: number }[] }
interface Overview { status: Status; connections: Conn[]; exchanges: { exchange: string; label: string }[]; strategies: TierRow[]; sessions: Sess[] }
interface Challenge { challenge_id: string; code: string; expires_at: number; summary: Record<string, string | number> }

export default function MicroLivePage({ notify, onNavigate, isAdmin }: PageProps & { isAdmin: boolean }) {
  const [d, setD] = useState<Overview | null>(null)
  const [busy, setBusy] = useState('')
  const [ack, setAck] = useState('')
  const [adding, setAdding] = useState(false)
  const [launch, setLaunch] = useState<TierRow | null>(null)
  const [killAsk, setKillAsk] = useState(false)
  const load = useCallback(() => { get<Overview>('/api/microlive').then(setD).catch((e) => notify(e.message, true)) }, [notify])
  useEffect(load, [load])
  if (!d) return <Loading />
  const st = d.status
  const en = lang === 'en'
  const notice = en ? st.notice_en || st.notice : st.notice
  const ackPhrase = en ? st.ack_phrase_en || st.ack_phrase : st.ack_phrase
  const startPhrase = en ? st.start_phrase_en || st.start_phrase : st.start_phrase
  const blockers = en ? st.blockers_en || st.blockers : st.blockers
  const act = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key)
    try { await fn(); notify(ok); load() } catch (e) { notify((e as Error).message, true) } finally { setBusy('') }
  }

  return (
    <>
      <Heading title="微实盘" description="用真实资金、很小的金额跑已经毕业的策略。每个策略从最低一档资金上限开始，交易所止损必挂，每次启动都要你输入确认码，随时可以一键停止。"
        actions={(st.enabled || d.sessions.length > 0) && <button className="button danger" onClick={() => setKillAsk(true)}>一键停止</button>} />

      <div className={`ml-banner ${st.killed ? 'bad' : st.ready ? 'ok' : 'warn'}`}>
        <strong>{st.killed ? '已一键停止' : st.ready ? '可以启动' : '还不能启动'}</strong>
        {blockers.length > 0 && <ul>{blockers.map((b) => <li key={b}>{b}</li>)}</ul>}
        {st.killed && isAdmin && <RearmInline phrase={ackPhrase} onDone={load} notify={notify} />}
      </div>

      <div className="stack">
        {(!st.enabled || st.notice_version !== st.notice_current) && (
          <Panel title="风险告知" subtitle="管理员读完后输入下面那句话，工作区才会打开微实盘。告知内容改版后要重新确认。">
            <Markdown text={notice} />
            {isAdmin ? (
              <div className="filter-row">
                <input value={ack} onChange={(e) => setAck(e.target.value)} placeholder={en ? `Type "${ackPhrase}"` : `输入「${ackPhrase}」`} aria-label="确认风险告知" />
                <button className="button primary" disabled={ack.trim() !== ackPhrase || !!busy}
                  onClick={() => act('enable', () => post('/api/microlive/enable', { phrase: ack, notice_version: st.notice_current }), en ? 'Micro live trading enabled' : '已打开微实盘')}>打开微实盘</button>
              </div>
            ) : <p className="hint">只有工作区管理员能打开。</p>}
          </Panel>
        )}

        {d.sessions.length > 0 && (
          <Panel title="运行中" subtitle="真实资金。点进去看成交、持仓、交易所止损单和对账。">
            <table>
              <thead><tr><th>策略</th><th className="right">资金</th><th className="right">收益</th><th>状态</th></tr></thead>
              <tbody>{d.sessions.map((s) => {
                const p = s.equity_points || []
                const ret = p.length > 1 && p[0].v ? p[p.length - 1].v / p[0].v - 1 : null
                return (
                  <tr key={s.id} className="clickable" onClick={() => onNavigate(`paper/${s.id}`)}>
                    <td>{s.strategy_name}</td><td className="right mono">{s.initial_cash?.toFixed(0)}</td>
                    <td className={`right mono ${ret == null ? '' : ret >= 0 ? 'positive' : 'negative'}`}>{ret == null ? '—' : `${(ret * 100).toFixed(2)}%`}</td>
                    <td><Tag tone="fail">实盘</Tag> {s.run_state === 'RUNNING' ? '运行中' : s.run_state}</td>
                  </tr>
                )
              })}</tbody>
            </table>
          </Panel>
        )}

        <div className="two-columns even">
          <Panel title="实盘 API Key" subtitle="只支持 Binance、OKX 现货。保存前检查三项：能交易、不能提现划转、绑定了 IP 白名单；每次启动前再查一遍。"
            actions={isAdmin ? <button className="button small" onClick={() => setAdding(true)}>添加 Key</button> : undefined}>
            {d.connections.length === 0 ? <p className="hint">还没有实盘 Key。</p> : d.connections.map((c) => (
              <div key={c.id} className="ml-conn">
                <div className="filter-row">
                  <strong>{c.label}</strong><span className="mono hint">{c.key_mask}</span>
                  <Tag tone={c.status === 'active' ? 'ok' : 'fail'}>{c.status === 'active' ? '自检通过' : c.status === 'revoked' ? '已删除' : '自检没过'}</Tag>
                  <span className="spacer" />
                  {isAdmin && c.status !== 'revoked' && <>
                    <button className="button small" disabled={!!busy} onClick={() => act(c.id, () => post(`/api/microlive/connections/${c.id}/test`), '已重新自检')}>重新自检</button>
                    <button className="button small" disabled={!!busy} onClick={async () => await appConfirm('删除这个实盘 Key？平台保存的密文会被擦除。') && act(c.id, () => request(`/api/microlive/connections/${c.id}`, { method: 'DELETE' }), '已删除')}>删除</button>
                  </>}
                </div>
                {c.last_test?.checks && <div className="chips">{c.last_test.checks.map((x) => <Tag key={x.name} tone={x.ok ? 'ok' : 'fail'}>{x.name}：{x.detail}</Tag>)}</div>}
                {c.last_test?.error && <p className="hint negative">{c.last_test.error}</p>}
              </div>
            ))}
          </Panel>

          <Panel title="资金档位" subtitle={`每个策略从 ${st.tiers[0]} 起步。在当前一档跑满 ${st.tier_days} 天、没有衰减、回撤不超过 10%，才能由管理员确认升一档；不会自动升。`}>
            {d.strategies.length === 0 ? <EmptyState glyph="◇" title="还没有毕业的策略">在「上线验证 → 晋级关卡」里走完回测、纸面模拟、模拟盘三关后，策略才能上微实盘。</EmptyState> : (
              <table>
                <thead><tr><th>策略</th><th className="right">上限</th><th>升档条件</th><th /></tr></thead>
                <tbody>{d.strategies.map((r) => (
                  <tr key={r.slug}>
                    <td>{r.name}</td>
                    <td className="right mono">{r.cap}{r.next_cap ? <div className="hint">下一档 {r.next_cap}</div> : null}</td>
                    <td className="hint">{r.checks.map((x) => `${x.ok ? '✓' : '○'} ${x.label}${x.value ? `（${x.value}）` : ''}`).join('\n').split('\n').map((l) => <div key={l}>{l}</div>)}</td>
                    <td className="right">
                      {isAdmin && <div className="button-row">
                        <button className="button small primary" disabled={!st.ready || !!busy} onClick={() => setLaunch(r)}>启动</button>
                        {r.can_raise && <button className="button small" onClick={async () => await appConfirm(`把「${r.name}」的资金上限从 ${r.cap} 升到 ${r.next_cap}？`) && act(r.slug, () => post(`/api/microlive/tiers/${r.slug}/raise`), '已升档')}>升档</button>}
                      </div>}
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            )}
          </Panel>
        </div>
      </div>

      {adding && <AddKey exchanges={d.exchanges} notify={notify} onClose={() => setAdding(false)} onDone={() => { setAdding(false); load() }} />}
      {launch && <Launch row={launch} conns={d.connections.filter((c) => c.status === 'active')} startPhrase={startPhrase} notify={notify}
        onClose={() => setLaunch(null)} onDone={(id) => { setLaunch(null); onNavigate(`paper/${id}`) }} />}
      {killAsk && (
        <Modal title="一键停止" onClose={() => setKillAsk(false)} width={440} footer={
          <button className="button danger" disabled={busy === 'kill'} onClick={() => act('kill', () => post('/api/microlive/kill'), '已平仓并停止全部微实盘').then(() => setKillAsk(false))}>平仓并停止全部</button>
        }>
          <p>所有微实盘实验立刻平掉持仓并停止，之后拒绝新的启动和开仓，直到管理员解除。</p>
        </Modal>
      )}
    </>
  )
}

function RearmInline({ phrase, onDone, notify }: { phrase: string; onDone: () => void; notify: PageProps['notify'] }) {
  const [v, setV] = useState('')
  const en = lang === 'en'
  return (
    <div className="filter-row">
      <input value={v} onChange={(e) => setV(e.target.value)} placeholder={en ? `Type "${phrase}" to re-arm` : `输入「${phrase}」解除`} aria-label="解除急停" />
      <button className="button small" disabled={v.trim() !== phrase} onClick={async () => {
        try { await post('/api/microlive/rearm', { phrase: v }); notify(en ? 'Emergency stop cleared' : '已解除急停'); onDone() } catch (e) { notify((e as Error).message, true) }
      }}>解除急停</button>
    </div>
  )
}

function AddKey({ exchanges, notify, onClose, onDone }: { exchanges: { exchange: string; label: string }[]; notify: PageProps['notify']; onClose: () => void; onDone: () => void }) {
  const [ex, setEx] = useState(exchanges[0]?.exchange ?? 'binance')
  const [v, setV] = useState({ api_key: '', api_secret: '', passphrase: '' })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  return (
    <Modal title="添加实盘 API Key" onClose={onClose} width={520} footer={
      <button className="button primary" disabled={busy || !v.api_key || !v.api_secret} onClick={async () => {
        setBusy(true); setErr('')
        try { await post('/api/microlive/connections', { exchange: ex, ...v }); notify('自检通过，已加密保存'); onDone() } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
      }}>{busy ? '自检中…' : '自检并保存'}</button>
    }>
      <Callout tone="warn">在交易所后台创建 Key 时：只勾「现货交易」，关掉提现和划转，IP 白名单填运行节点的出口 IP。三项有一项不满足，平台不会保存。</Callout>
      <Field label="交易所"><select value={ex} onChange={(e) => setEx(e.target.value)}>{exchanges.map((x) => <option key={x.exchange} value={x.exchange}>{x.label}</option>)}</select></Field>
      <Field label="API Key"><input value={v.api_key} onChange={(e) => setV({ ...v, api_key: e.target.value })} /></Field>
      <Field label="Secret"><input type="password" value={v.api_secret} onChange={(e) => setV({ ...v, api_secret: e.target.value })} /></Field>
      {ex === 'okx' && <Field label="Passphrase"><input type="password" value={v.passphrase} onChange={(e) => setV({ ...v, passphrase: e.target.value })} /></Field>}
      {err && <Callout tone="fail">{err}</Callout>}
    </Modal>
  )
}

function Launch({ row, conns, startPhrase, notify, onClose, onDone }: {
  row: TierRow; conns: Conn[]; startPhrase: string; notify: PageProps['notify']; onClose: () => void; onDone: (id: string) => void
}) {
  const [cid, setCid] = useState(conns[0]?.id ?? '')
  const [symbol, setSymbol] = useState('BTC/USDT')
  const [cash, setCash] = useState(String(row.cap))
  const [sl, setSl] = useState('5')
  const [ch, setCh] = useState<Challenge | null>(null)
  const [code, setCode] = useState('')
  const [phrase, setPhrase] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const S = ch?.summary
  const run = async (fn: () => Promise<void>) => { setBusy(true); setErr(''); try { await fn() } catch (e) { setErr((e as Error).message) } finally { setBusy(false) } }
  return (
    <Modal title={`启动微实盘 · ${row.name}`} onClose={onClose} width={560} footer={!ch
      ? <button className="button primary" disabled={busy || !cid} onClick={() => run(async () => setCh(await post<Challenge>('/api/microlive/prepare', { slug: row.slug, connection_id: cid, symbol, initial_cash: Number(cash), stop_loss: Number(sl) / 100 })))}>{busy ? '检查中…' : '生成启动摘要'}</button>
      : <button className="button danger" disabled={busy || code.trim().length !== 6 || phrase.trim() !== startPhrase} onClick={() => run(async () => {
        const r = await post<{ run_id: string }>('/api/microlive/start', { challenge_id: ch.challenge_id, code, phrase })
        notify(lang === 'en' ? 'Micro live session started' : '微实盘已启动'); onDone(r.run_id)
      })}>确认并启动（真实资金）</button>}>
      {!ch ? (
        <>
          {conns.length === 0 && <Callout tone="warn">先添加一个自检通过的实盘 Key。</Callout>}
          <Field label="实盘 Key"><select value={cid} onChange={(e) => setCid(e.target.value)}>{conns.map((c) => <option key={c.id} value={c.id}>{c.label} {c.key_mask}</option>)}</select></Field>
          <div className="two-columns even">
            <Field label="交易对（只做现货）"><input value={symbol} onChange={(e) => setSymbol(e.target.value)} /></Field>
            <Field label={`本次资金（上限 ${row.cap}）`}><input type="number" min={1} max={row.cap} value={cash} onChange={(e) => setCash(e.target.value)} /></Field>
          </div>
          <Field label="默认止损（%）" hint="策略自己没设止损时用这个；每个持仓都会在交易所挂止损单"><input type="number" min={0.5} max={20} step={0.5} value={sl} onChange={(e) => setSl(e.target.value)} /></Field>
        </>
      ) : (
        <>
          <dl className="kv">
            <dt>策略</dt><dd>{S?.name}（版本 {String(S?.version).slice(0, 8)}，{S?.timeframe}）</dd>
            <dt>交易所</dt><dd>{S?.exchange} · {S?.key_mask}</dd>
            <dt>交易对</dt><dd>{S?.symbol}</dd>
            <dt>本次资金</dt><dd><strong>{S?.initial_cash} {S?.quote}</strong>（账户余额 {Number(S?.balance).toFixed(2)}，上限 {S?.cap}）</dd>
            <dt>止损</dt><dd>默认 {(Number(S?.stop_loss) * 100).toFixed(1)}%，交易所挂单</dd>
            <dt>当日亏损熔断</dt><dd>{(Number(S?.max_daily_loss) * 100).toFixed(0)}%</dd>
          </dl>
          <Callout tone="fail">这会用真实资金下单。确认码 <strong className="mono">{ch.code}</strong>，5 分钟内有效。</Callout>
          <div className="two-columns even">
            <Field label="输入确认码"><input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" maxLength={6} autoFocus /></Field>
            <Field label={`输入「${startPhrase}」`}><input value={phrase} onChange={(e) => setPhrase(e.target.value)} /></Field>
          </div>
        </>
      )}
      {err && <Callout tone="fail">{err}</Callout>}
    </Modal>
  )
}
