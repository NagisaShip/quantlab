import { useCallback, useEffect, useState } from 'react'
import { fmtTime } from '../api2'
import { nodeApi, type BotItem, type BotKinds, type NodeHealth } from '../apiNode'
import { Callout, Field, Loading, Modal, Panel, Tag } from '../components/ui'
import type { PageProps } from './types'

const FIELD_LABEL: Record<string, string> = {
  bot_token: 'Bot Token', app_id: 'App ID', app_secret: 'App Secret', verification_token: 'Verification Token',
  encrypt_key: 'Encrypt Key（可选）', corp_id: '企业 ID（CorpID）', agent_id: '应用 AgentId', secret: '应用 Secret',
  token: '回调 Token', aes_key: '回调 EncodingAESKey',
}
const KIND_HELP: Record<string, string> = {
  feishu: '在飞书开放平台建自建应用，开启机器人能力，事件订阅填下面的回调地址，订阅「接收消息」事件。',
  wecom: '在企业微信管理后台建自建应用，「接收消息」里填下面的回调地址和 Token、EncodingAESKey。',
  telegram: '找 @BotFather 建一个机器人拿 Token。平台用长轮询收消息，服务器不需要公网回调地址。',
}

export default function NodeTab({ notify, isAdmin, onNavigate }: { notify: PageProps['notify']; isAdmin: boolean; onNavigate: (k: string) => void }) {
  const [h, setH] = useState<NodeHealth | null>(null)
  const [bots, setBots] = useState<{ bots: BotItem[]; kinds: BotKinds; bindings: { id: string; platform: string; bound_at: number }[] } | null>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [code, setCode] = useState<{ code: string; expires_at: number } | null>(null)
  const load = useCallback(() => {
    nodeApi.health().then(setH).catch((e) => notify(e.message, true))
    nodeApi.bots().then(setBots).catch((e) => notify(e.message, true))
  }, [notify])
  useEffect(load, [load])

  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  return (
    <div className="stack">
      <div className="two-columns even">
        <Panel title="节点状态" subtitle="worker 每 20 秒续一次租约；运行中的实验超过 2 分钟没心跳就发「运行节点失联」告警。">
          {!h ? <Loading /> : (
            <>
              <dl className="kv">
                <dt>在线 worker</dt><dd>{h.workers_alive}</dd>
                <dt>失联实验</dt><dd>{h.stale_runs.length ? h.stale_runs.map((r) => `${r.title || r.id}（${r.silent_seconds} 秒）`).join('、') : '无'}</dd>
              </dl>
              {h.stale_runs.length > 0 && <Callout tone="fail">有实验失联：检查服务器上的 worker 容器。恢复后从检查点接着跑，停机前送出的订单先查单、不重下。</Callout>}
              <h3>交易所时钟</h3>
              {h.clock.length === 0 ? <p className="hint">有模拟盘实验在跑时，worker 每 10 分钟测一次。签名按交易所时间，偏差超过 5 秒会告警。</p> : (
                <table>
                  <thead><tr><th>交易所</th><th className="right">偏差</th><th className="right">往返</th><th>测于</th></tr></thead>
                  <tbody>{h.clock.map((c) => (
                    <tr key={c.exchange}>
                      <td>{c.exchange}</td>
                      <td className="right mono"><span className={`table-pill ${c.level === 'bad' ? 'fail' : c.level === 'warn' ? 'pending' : ''}`}>{c.offset_ms} ms</span></td>
                      <td className="right mono">{c.rtt_ms ?? '—'} ms</td><td>{fmtTime(c.measured_at)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              )}
              {h.rate_limits.some((r) => r.hits_429 || r.paused_s) && (
                <p className="hint">限频：{h.rate_limits.map((r) => `${r.exchange} 被限 ${r.hits_429} 次${r.paused_s ? `，暂停中 ${r.paused_s} 秒` : ''}`).join('；')}</p>
              )}
            </>
          )}
        </Panel>
        <Panel title="手机看板" subtitle="只读看板，外加一键停止并平仓（要二次确认）。">
          <p>在手机浏览器打开下面的地址并登录，然后用浏览器菜单「添加到主屏幕」，就能像 App 一样打开。</p>
          <p><code className="inline">{origin}/#/board</code></p>
          <button className="button small" onClick={() => onNavigate('board')}>在这里预览</button>
          <p className="hint">部署到云服务器的步骤见 docs/运维与恢复手册.md「运行节点」一节：docker compose 一条命令起服务，容器挂了自动拉起。</p>
        </Panel>
      </div>

      <Panel title="聊天机器人" subtitle="在飞书 / 企业微信 / Telegram 里发「状态」「为什么 1」「停止 1」「平仓 全部」。停止和平仓要回复确认码才执行。"
        actions={isAdmin && bots ? Object.entries(bots.kinds).map(([k, v]) => (
          <button key={k} className="button small" onClick={() => setAdding(k)}>添加{v.label}</button>
        )) : undefined}>
        {!bots ? <Loading /> : (
          <>
            {bots.bots.length === 0 ? <p className="hint">还没有机器人。{isAdmin ? '点右上角添加。' : '请工作区管理员添加。'}</p> : (
              <table>
                <thead><tr><th>名称</th><th>类型</th><th>回调地址</th><th /></tr></thead>
                <tbody>{bots.bots.map((b) => (
                  <tr key={b.id}>
                    <td>{b.name}</td><td>{b.label}</td>
                    <td className="mono wrap">{b.hook_path ? origin + b.hook_path : '不需要（长轮询）'}</td>
                    <td className="right">{isAdmin && <button className="button small" onClick={async () => { await nodeApi.delBot(b.id); load() }}>删除</button>}</td>
                  </tr>
                ))}</tbody>
              </table>
            )}
            <h3>绑定我的聊天</h3>
            <p>只有绑定过的聊天能下命令。生成绑定码后，在机器人的聊天里发「绑定 绑定码」，10 分钟内有效。</p>
            <div className="filter-row">
              <button className="button small primary" disabled={!bots.bots.length} onClick={async () => setCode(await nodeApi.bindCode())}>生成绑定码</button>
              {code && <span>绑定码 <strong className="mono">{code.code}</strong>，{fmtTime(code.expires_at)} 前有效</span>}
            </div>
            {bots.bindings.length > 0 && (
              <div className="chips">{bots.bindings.map((b) => (
                <Tag key={b.id}>{b.platform} · {fmtTime(b.bound_at)} <button className="link" onClick={async () => { await nodeApi.unbind(b.id); load() }}>解绑</button></Tag>
              ))}</div>
            )}
          </>
        )}
      </Panel>
      {adding && bots && <AddBot kind={adding} kinds={bots.kinds} notify={notify} onClose={() => setAdding(null)} onDone={() => { setAdding(null); load() }} />}
    </div>
  )
}

function AddBot({ kind, kinds, notify, onClose, onDone }: { kind: string; kinds: BotKinds; notify: PageProps['notify']; onClose: () => void; onDone: () => void }) {
  const [vals, setVals] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const spec = kinds[kind]
  return (
    <Modal title={`添加${spec.label}机器人`} onClose={onClose} width={520} footer={
      <button className="button primary" disabled={busy} onClick={async () => {
        setBusy(true)
        try { await nodeApi.addBot(kind, spec.label, vals); notify('已添加，凭证加密保存'); onDone() } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
      }}>保存</button>
    }>
      <p className="hint">{KIND_HELP[kind]}</p>
      {spec.fields.map((f) => (
        <Field key={f} label={FIELD_LABEL[f] ?? f}>
          <input type={/secret|token|key/i.test(f) ? 'password' : 'text'} value={vals[f] ?? ''} onChange={(e) => setVals({ ...vals, [f]: e.target.value })} />
        </Field>
      ))}
      {kind !== 'telegram' && <p className="hint">保存后在列表里能看到回调地址，填回飞书 / 企业微信后台。</p>}
    </Modal>
  )
}
