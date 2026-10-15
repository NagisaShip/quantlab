import { useCallback, useEffect, useState } from 'react'
import { fmtTime } from '../api2'
import { demo, type DemoConnection, type DemoEnv } from '../apiDemo'
import { Callout, EmptyState, Field, Fold, Heading, Loading, Modal, Panel, Tag } from '../components/ui'
import type { PageProps } from './types'
import { appConfirm } from '../components/dialog'

/** 交易所显示名（后端返回小写代号） */
const EXCHANGE_LABEL: Record<string, string> = { binance: 'Binance', okx: 'OKX', bybit: 'Bybit' }

const STATUS: Record<DemoConnection['status'], [string, string]> = {
  active: ['可用', ''], error: ['连接失败', 'fail'], revoked: ['已撤销', 'pending'],
}

/**
 * 交易所模拟盘：真实行情 + 交易所的模拟资金。
 * 这里只收模拟盘 Key；模块里没有实盘地址，平台不会产生真实订单。
 */
export default function DemoAccountsPage({ notify, onNavigate, readonly }: PageProps & { readonly?: boolean }) {
  const [envs, setEnvs] = useState<DemoEnv[] | null>(null)
  const [conns, setConns] = useState<DemoConnection[] | null>(null)
  const [adding, setAdding] = useState<DemoEnv | null>(null)
  const load = useCallback(() => { demo.connections().then((r) => setConns(r.connections)).catch((e) => notify(e.message, true)) }, [notify])
  useEffect(() => {
    demo.environments().then((r) => setEnvs(r.environments)).catch((e) => notify(e.message, true))
    load()
  }, [load, notify])
  if (!envs || !conns) return <Loading />
  const live = conns.filter((c) => c.status !== 'revoked')
  const gone = conns.filter((c) => c.status === 'revoked')

  return (
    <>
      <Heading
        title="交易所模拟盘"
        description="策略用真实行情运行，委托送到交易所的模拟账户成交。成交价、手续费和拒单都来自交易所，比平台撮合更接近实盘。这里只接受模拟盘 Key，平台没有实盘下单的路径。"
        actions={<button className="button" onClick={() => onNavigate('paper')}>去纸面模拟</button>}
      />
      <Panel title="我的模拟盘连接" subtitle="密钥加密保存，只在后台执行进程里解密；策略代码所在的隔离进程拿不到。页面只显示掩码。">
        {live.length === 0 ? (
          <EmptyState glyph="◎" title="还没有连接">从下面选一个模拟环境接入。没有连接也能做纸面模拟（平台撮合）。</EmptyState>
        ) : <div className="demo-conn-grid">{live.map((c) => <ConnCard key={c.id} c={c} notify={notify} onChange={load} readonly={readonly} />)}</div>}
      </Panel>

      <Panel flush title="可接入的模拟环境" subtitle="价格跟随实盘的环境才能判断策略效果；价格独立的测试网只适合联调接口。">
        <div className="table-wrap">
          <table className="demo-env-table">
            <thead><tr><th>环境</th><th>价格</th><th>说明与 Key 获取方式</th><th /></tr></thead>
            <tbody>
              {[...envs].sort((x, y) => Number(y.realistic) - Number(x.realistic)).map((e) => (
                <tr key={`${e.exchange}-${e.env}`} className={e.realistic ? '' : 'dim'}>
                  <td><b>{e.label}</b></td>
                  <td>{e.realistic ? <Tag tone="ok">跟随实盘</Tag> : <Tag tone="warn">独立，仅联调</Tag>}</td>
                  <td>{e.note}<small>{e.key_help}</small></td>
                  <td className="right">{!readonly && <button className="button small" onClick={() => setAdding(e)}>接入</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>

      {gone.length > 0 && (
        <Panel title="已撤销" flush>
          <table><tbody>{gone.map((c) => (
            <tr key={c.id}><td>{c.label}</td><td>{c.env_label}</td><td className="mono">{c.key_mask}</td><td className="muted">创建于 {fmtTime(c.created_at)}</td></tr>
          ))}</tbody></table>
        </Panel>
      )}
      <Fold title="安全边界" count={4}>
        <ul className="checklist">
          <li>只能访问上面列出的模拟环境地址；OKX 请求固定带模拟盘标记，实盘 Key 会被交易所拒绝；Bybit 只连 api-demo / api-testnet 域名。</li>
          <li>每笔订单先过事前风控：单笔金额上限、每分钟笔数上限、当日亏损熔断。</li>
          <li>网络超时不重下单：先按客户端订单号查单，查清楚再决定。</li>
          <li>定期把平台账本和交易所余额对账，不一致就暂停开新仓，等你确认。</li>
        </ul>
      </Fold>
      {adding && <ConnectModal env={adding} notify={notify} onClose={() => setAdding(null)} onDone={() => { setAdding(null); load() }} />}
    </>
  )
}

function ConnCard({ c, notify, onChange, readonly }: { c: DemoConnection; notify: PageProps['notify']; onChange: () => void; readonly?: boolean }) {
  const [busy, setBusy] = useState('')
  const act = async (name: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(name)
    try { await fn(); notify(ok) } catch (e) { notify((e as Error).message, true) } finally { setBusy(''); onChange() }
  }
  const [label, cls] = STATUS[c.status]
  const bal = Object.entries(c.last_test?.balances ?? {})
  return (
    <div className="demo-conn">
      <div className="demo-conn-head">
        <div>
          <b>{c.label}</b> <Tag>{EXCHANGE_LABEL[c.exchange] ?? c.exchange}</Tag> {!c.realistic && <Tag tone="warn">仅联调</Tag>}
          <small className="mono">{c.key_mask} · {c.env_label}</small>
        </div>
        <span className={`table-pill ${cls}`}>{label}</span>
      </div>
      {c.last_test?.error && <Callout tone="fail">{c.last_test.error}</Callout>}
      {bal.length > 0 && (
        <div className="demo-bal">
          {bal.map(([k, v]) => <span key={k}><small>{k}</small>{Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 6 })}</span>)}
        </div>
      )}
      <div className="demo-conn-foot">
        <small className="muted">最近检测 {c.last_test?.at ? fmtTime(c.last_test.at) : '—'}</small>
        {!readonly && (
          <div className="button-row">
            <button className="button small" disabled={!!busy} onClick={() => act('test', () => demo.test(c.id), '检测完成')}>{busy === 'test' ? '检测中…' : '重新检测'}</button>
            <button className="button small danger" disabled={!!busy} onClick={async () => {
              if (!await appConfirm('撤销这个模拟盘连接？平台会擦除保存的密钥。正在用它的纸面模拟会在下次下单时报错。')) return
              act('revoke', () => demo.revoke(c.id), '已撤销。交易所那边的 Key 请到交易所后台删除')
            }}>撤销</button>
          </div>
        )}
      </div>
    </div>
  )
}

function ConnectModal({ env, notify, onClose, onDone }: { env: DemoEnv; notify: PageProps['notify']; onClose: () => void; onDone: () => void }) {
  const [label, setLabel] = useState('')
  const [key, setKey] = useState('')
  const [secret, setSecret] = useState('')
  const [pass, setPass] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const okx = env.exchange === 'okx'
  const submit = async () => {
    setBusy(true)
    setError('')
    try {
      await demo.connect({ exchange: env.exchange, env: env.env, label, api_key: key.trim(), api_secret: secret.trim(), passphrase: okx ? pass.trim() : undefined })
      setKey(''); setSecret(''); setPass('')
      notify('已接入：余额查询通过')
      onDone()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <Modal title={`接入 ${env.label}`} onClose={onClose} width={560} footer={
      <button className="button primary" disabled={busy || key.trim().length < 8 || secret.trim().length < 8 || (okx && !pass.trim())} onClick={submit}>{busy ? '验证中…' : '验证并保存'}</button>
    }>
      <div style={{ display: 'grid', gap: 12 }}>
        <Callout>{env.key_help}。平台会向 <span className="mono">{env.host}</span> 查一次余额，查得到才保存。</Callout>
        {!env.realistic && <Callout tone="warn">{env.note}</Callout>}
        <Field label="名称（可选）"><input value={label} maxLength={60} onChange={(e) => setLabel(e.target.value)} placeholder={env.label} /></Field>
        <Field label="API Key"><input autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} /></Field>
        <Field label="Secret Key"><input type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)} /></Field>
        {okx && <Field label="Passphrase"><input type="password" autoComplete="off" value={pass} onChange={(e) => setPass(e.target.value)} /></Field>}
        {error && <Callout tone="fail">{error}</Callout>}
      </div>
    </Modal>
  )
}
