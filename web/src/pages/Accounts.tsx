import { useCallback, useEffect, useState } from 'react'
import { fmtTime } from '../api2'
import { v4, type Connection, type Guide } from '../api3'
import { ApiError } from '../http'
import { Callout, EmptyState, Field, Fold, Heading, Loading, Modal, Panel, Rich, Segments, Tag } from '../components/ui'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

const STATUS: Record<Connection['status'], [string, string]> = {
  active: ['有效', ''], rejected: ['已拒绝（权限过大）', 'fail'], expired: ['已过期 / 被交易所拒绝', 'pending'], revoked: ['已撤销', 'pending'],
}

/**
 * 只读账户：只读接入向导。
 * 默认只显示接入方法；粘贴 Key 不会启动任何交易（live_enabled=false，平台没有真实下单路径）。
 */
export default function AccountsPage({ notify, onNavigate, readonly }: PageProps & { readonly?: boolean }) {
  const [guide, setGuide] = useState<{ exchanges: Record<string, Guide>; notes: string[] } | null>(null)
  const [conns, setConns] = useState<Connection[] | null>(null)
  const [ex, setEx] = useState<'binance' | 'okx'>('binance')
  const [wizard, setWizard] = useState(false)
  const load = useCallback(() => { v4.connections().then((r) => setConns(r.items)).catch((e) => notify(e.message, true)) }, [notify])
  useEffect(() => {
    v4.accountGuide().then(setGuide).catch((e) => notify(e.message, true))
    load()
  }, [load, notify])
  if (!guide || !conns) return <Loading />
  const g = guide.exchanges[ex]
  const active = conns.filter((c) => c.status !== 'revoked')

  return (
    <>
      <Heading title="只读账户" description="只读接入：用于读取余额与真实成交、做实盘对照。平台不托管资金，也不发送任何真实交易指令。"
        actions={!readonly && <button className="button primary" onClick={() => setWizard(true)}>绑定只读账户</button>} />
      <div className="two-columns">
        <div>
          <Panel title="已绑定的连接" subtitle="管理页只返回掩码与 connection_id；凭证用独立主密钥加密，不进数据库、不进日志。">
            {active.length === 0 ? (
              <EmptyState title="未绑定" glyph="⚿">没有账户也能完成策略配置、回测与纸面模拟。需要对照真实成交时再绑定只读 Key，或直接在“实盘对照”导入 CSV。</EmptyState>
            ) : active.map((c) => <ConnectionCard key={c.id} c={c} notify={notify} onChange={load} onNavigate={onNavigate} readonly={readonly} />)}
          </Panel>
          {conns.some((c) => c.status === 'revoked') && (
            <Panel title="已撤销" flush>
              <table><tbody>{conns.filter((c) => c.status === 'revoked').map((c) => (
                <tr key={c.id}><td>{c.label}</td><td>{c.exchange}</td><td className="mono">{c.key_mask}</td><td className="muted">创建于 {fmtTime(c.created_at)}</td></tr>
              ))}</tbody></table>
            </Panel>
          )}
        </div>
        <div>
          <Panel title="接入方法" actions={<Segments value={ex} onChange={setEx} options={[{ value: 'binance', label: 'Binance' }, { value: 'okx', label: 'OKX' }]} />}>
            <ol className="steps">{g.steps.map((s) => <li key={s}><span><Rich text={s} /></span></li>)}</ol>
            <p className="hint">官方说明：<a href={g.doc} target="_blank" rel="noreferrer">{g.name} API 管理</a></p>
          </Panel>
          <Fold title="平台能做什么、不能做什么" count={guide.notes.length}>
            <ul className="checklist">{guide.notes.map((n) => <li key={n}><span><Rich text={n} /></span></li>)}</ul>
          </Fold>
        </div>
      </div>
      {wizard && <ConnectWizard initial={ex} guide={guide.exchanges} notify={notify} onClose={() => setWizard(false)} onDone={() => { setWizard(false); load() }} />}
    </>
  )
}

function ConnectionCard({ c, notify, onChange, onNavigate, readonly }: { c: Connection; notify: PageProps['notify']; onChange: () => void; onNavigate: (k: string) => void; readonly?: boolean }) {
  const [busy, setBusy] = useState('')
  const [symbols, setSymbols] = useState('BTC/USDT')
  const act = async (name: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(name)
    try { await fn(); notify(ok); onChange() } catch (e) { notify((e as Error).message, true); onChange() } finally { setBusy('') }
  }
  const [label, cls] = STATUS[c.status]
  return (
    <div className="panel" style={{ marginBottom: 12 }}>
      <div className="panel-heading">
        <div>
          <h2>{c.label} <Tag>{c.exchange}</Tag> {c.environment === 'testnet' && <Tag tone="warn">测试网</Tag>}</h2>
          <p className="mono">{c.connection_id} · {c.key_mask} · 账户 {c.scope.account || '—'}</p>
        </div>
        <span className={`table-pill ${cls}`}>{label}</span>
      </div>
      <div className="panel-body">
        <ul className="checklist">
          {(c.last_test.checks ?? []).map((k) => <li key={k.name} className={k.ok ? '' : k.name === 'IP 限制' ? 'warn' : 'no'}><b>{k.name}</b>　{k.detail}</li>)}
        </ul>
        {c.last_error && <Callout tone="fail">{c.last_error}</Callout>}
        <dl className="kv" style={{ marginTop: 10 }}>
          <dt>最近检测</dt><dd>{c.last_test.at ? fmtTime(c.last_test.at) : '—'}</dd>
          <dt>最近同步</dt><dd>{c.last_sync_at ? fmtTime(c.last_sync_at) : '从未同步'}</dd>
        </dl>
        {!readonly && (
          <div className="button-row" style={{ marginTop: 12 }}>
            <button className="button small" disabled={!!busy} onClick={() => act('test', () => v4.testConnection(c.id), '检测完成')}>{busy === 'test' ? '检测中…' : '重新检测'}</button>
            {c.status === 'active' && (
              <>
                <input style={{ width: 170, height: 28 }} value={symbols} onChange={(e) => setSymbols(e.target.value)} title="要同步成交的交易对，逗号分隔" />
                <button className="button small primary" disabled={!!busy} onClick={() => act('sync', () => v4.syncConnection(c.id, symbols.split(',').map((x) => x.trim()).filter(Boolean)), '同步完成')}>
                  {busy === 'sync' ? '同步中…' : '同步余额与成交'}
                </button>
                <button className="button small ghost" onClick={() => onNavigate('live')}>去对照</button>
              </>
            )}
            <button className="button small danger" disabled={!!busy} onClick={async () => {
              if (!await appConfirm('撤销连接？平台会删除保存的加密凭证。注意：交易所侧的 API Key 仍然有效，请到交易所后台删除或禁用。')) return
              act('revoke', () => v4.revokeConnection(c.id), '已撤销：请到交易所后台删除该 Key')
            }}>撤销连接</button>
          </div>
        )}
      </div>
    </div>
  )
}

function ConnectWizard({ initial, guide, notify, onClose, onDone }: { initial: 'binance' | 'okx'; guide: Record<string, Guide>; notify: PageProps['notify']; onClose: () => void; onDone: () => void }) {
  const [step, setStep] = useState(1)
  const [ex, setEx] = useState(initial)
  const [env, setEnv] = useState<'live' | 'testnet'>('live')
  const [label, setLabel] = useState('')
  const [key, setKey] = useState('')
  const [secret, setSecret] = useState('')
  const [pass, setPass] = useState('')
  const [ack, setAck] = useState(false)
  const [error, setError] = useState<{ message: string; checks?: { name: string; ok: boolean; detail: string }[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await v4.connect({ exchange: ex, environment: env, label: label || `${ex} 只读`, api_key: key.trim(), api_secret: secret.trim(), passphrase: pass || undefined })
      setKey(''); setSecret(''); setPass('')
      notify('连接已建立：只读权限检测通过')
      onDone()
    } catch (e) {
      const checks = e instanceof ApiError ? (e.details as { report?: { checks?: { name: string; ok: boolean; detail: string }[] } }).report?.checks : undefined
      setError({ message: (e as Error).message, checks })
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal title={`绑定只读账户 · 第 ${step} / 3 步`} onClose={onClose} width={640} footer={
      <>
        {step > 1 && <button className="button" onClick={() => setStep(step - 1)}>上一步</button>}
        {step < 3 && <button className="button primary" disabled={step === 2 && !ack} onClick={() => setStep(step + 1)}>下一步</button>}
        {step === 3 && <button className="button primary" disabled={busy || key.length < 8 || secret.length < 8 || (ex === 'okx' && !pass)} onClick={submit}>{busy ? '检测中…' : '检测并保存'}</button>}
      </>
    }>
      {step === 1 && (
        <div style={{ display: 'grid', gap: 12 }}>
          <Field label="交易所"><Segments value={ex} onChange={setEx} options={[{ value: 'binance', label: 'Binance' }, { value: 'okx', label: 'OKX' }]} /></Field>
          <Field label="环境" hint="测试网连接的成交永远标为“测试”，不会出现在实盘结果中">
            <Segments value={env} onChange={setEnv} options={[{ value: 'live', label: '实盘账户（只读）' }, { value: 'testnet', label: '官方测试网' }]} />
          </Field>
          <Field label="连接名称"><input value={label} maxLength={60} onChange={(e) => setLabel(e.target.value)} placeholder="例如：主账户只读" /></Field>
        </div>
      )}
      {step === 2 && (
        <div>
          <ol className="steps">{guide[ex].steps.map((s) => <li key={s}>{s}</li>)}</ol>
          <label style={{ display: 'flex', gap: 8, marginTop: 14, fontSize: 13 }}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            我已在交易所创建<b>只读</b> Key（未勾选交易、提现、划转），并尽量绑定了 IP 白名单。
          </label>
        </div>
      )}
      {step === 3 && (
        <div style={{ display: 'grid', gap: 12 }}>
          <Callout>平台会先测试时间同步、账户身份与权限：开启了交易或提现权限的 Key 会被<b>拒绝且不保存</b>。通过后用独立主密钥加密保存，之后只显示掩码。</Callout>
          <Field label="API Key"><input autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} /></Field>
          <Field label="Secret Key"><input type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} /></Field>
          {ex === 'okx' && <Field label="Passphrase"><input type="password" autoComplete="off" value={pass} onChange={(e) => setPass(e.target.value)} /></Field>}
          {error && (
            <div className="callout fail">
              <strong>{error.message}</strong>
              {error.checks && <ul className="checklist" style={{ marginTop: 8 }}>{error.checks.map((k) => <li key={k.name} className={k.ok ? '' : 'no'}>{k.name}：{k.detail}</li>)}</ul>}
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
