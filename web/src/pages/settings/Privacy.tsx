/** 工作区隐私、反馈频率与存储清理策略（管理员）。 */
import { useCallback, useEffect, useState } from 'react'
import { v4, type Me } from '../../api/platform'
import { appConfirm } from '../../components/dialog'
import { Callout, Field, Loading } from '../../components/ui'
import type { PageProps } from '../types'

const fmtBytes = (n: number) => {
  if (!n) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}
const fmtDate = (ms: number | null) => ms ? new Date(ms).toLocaleDateString('zh-CN') : '—'

type FeedbackCfg = { enabled: boolean; every_n_messages: number; auto_close_seconds: number }
type PrivacyCfg = { retain_agent_history: boolean; retain_uploads: boolean; allow_improvement_feedback: boolean; retention_days: number }
type StorageCfg = { auto_cleanup: boolean; threshold_percent: number; target_percent: number; keep_days: number }

export default function PrivacyTab({ me, isAdmin, notify, onSaved }: {
  me: Me; isAdmin: boolean; notify: PageProps['notify']; onSaved: () => void
}) {
  const s = me.workspace?.settings
  const [feedback, setFeedback] = useState<FeedbackCfg>({ enabled: false, every_n_messages: 10, auto_close_seconds: 0, ...(s?.feedback ?? {}) })
  const [privacy, setPrivacy] = useState<PrivacyCfg>({ retain_agent_history: true, retain_uploads: true, allow_improvement_feedback: true, retention_days: 365, ...(s?.privacy ?? {}) })
  const [storageCfg, setStorageCfg] = useState<StorageCfg>({ auto_cleanup: false, threshold_percent: 90, target_percent: 75, keep_days: 30, ...(s?.storage ?? {}) })
  const [data, setData] = useState<Awaited<ReturnType<typeof v4.workspaceStorage>> | null>(null)
  const [busy, setBusy] = useState(false)
  const [cleanupCat, setCleanupCat] = useState('cache')
  const [keepDays, setKeepDays] = useState(30)
  const [preview, setPreview] = useState<Awaited<ReturnType<typeof v4.storageCleanup>> | null>(null)

  const load = useCallback(() => {
    if (!isAdmin) return
    v4.workspaceStorage().then(setData).catch((e) => notify((e as Error).message, true))
  }, [isAdmin, notify])
  useEffect(load, [load])

  const save = async () => {
    setBusy(true)
    try {
      await v4.updateSettings({ feedback, privacy, storage: storageCfg })
      notify('隐私与存储策略已保存')
      onSaved()
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const runCleanup = async (dry: boolean) => {
    setBusy(true)
    try {
      const r = await v4.storageCleanup({ category: cleanupCat, keep_days: keepDays, dry_run: dry })
      setPreview(r)
      if (!dry) {
        notify(`已清理 ${r.deleted.records} 条记录，${fmtBytes(r.deleted.bytes)}`)
        load()
      }
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }
  const askCleanup = async () => {
    const label = data?.categories.find((c) => c.key === cleanupCat)?.label ?? cleanupCat
    if (await appConfirm(`确认清理「${label}」中超过 ${keepDays} 天的数据？此操作不可撤销。`)) await runCleanup(false)
  }

  return (
    <>
      <div className="set-section">
        <div className="set-section-head"><h3>Agent 评价弹窗</h3></div>
        <p className="set-section-note">Agent 输出完一段消息后，按管理员设置的频率邀请研究者评分。可以自动关闭、主动关闭或填写意见；提交后会显示感谢语，并保存到服务器。</p>
        <div className="set-row">
          <div><b>启用评价弹窗</b><small>关闭后不再主动弹出评分卡，但消息上的点赞 / 点踩仍然可用</small></div>
          <input type="checkbox" checked={feedback.enabled} disabled={!isAdmin} onChange={(e) => setFeedback({ ...feedback, enabled: e.target.checked })} />
        </div>
        <div className="two-columns even">
          <Field label="触发频率（每条 Agent 消息）"><input type="number" min={1} max={1000} disabled={!isAdmin} value={feedback.every_n_messages} onChange={(e) => setFeedback({ ...feedback, every_n_messages: Math.max(1, Number(e.target.value) || 1) })} /></Field>
          <Field label="自动关闭（秒，0 = 不自动关闭）"><input type="number" min={0} max={120} disabled={!isAdmin} value={feedback.auto_close_seconds} onChange={(e) => setFeedback({ ...feedback, auto_close_seconds: Math.max(0, Number(e.target.value) || 0) })} /></Field>
        </div>
      </div>

      <div className="set-section">
        <div className="set-section-head"><h3>隐私与数据保留</h3><a className="button small ghost" href="#/privacy">查看隐私政策</a></div>
        <p className="set-section-note">这些设置作用于本工作区。保留期由后台维护任务执行；运行中的对话和任务不会被自动删除。</p>
        <Callout title="工作区管理员可见范围">工作区管理员可以查看工作区内的共享研究内容、成员用量、存储与审计记录，包括成员设为“仅自己可见”的策略与回测；不能查看自己未加入的私聊，也不能查看个人模型密钥明文。成员把内容上传到工作区、群组或策略广场前，应假定管理员可见。</Callout>
        <div className="set-row"><div><b>保留 Agent 历史</b><small>关闭后，后台会尽快清理已结束对话的旧消息，至少保留一个维护周期</small></div>
          <input type="checkbox" checked={privacy.retain_agent_history} disabled={!isAdmin} onChange={(e) => setPrivacy({ ...privacy, retain_agent_history: e.target.checked })} /></div>
        <div className="set-row"><div><b>保留上传文件</b><small>关闭后，旧的上传附件和对话图片会被后台清理</small></div>
          <input type="checkbox" checked={privacy.retain_uploads} disabled={!isAdmin} onChange={(e) => setPrivacy({ ...privacy, retain_uploads: e.target.checked })} /></div>
        <div className="set-row"><div><b>允许评价用于模型改进</b><small>关闭后仍保存评分用于产品排错，但不进入模型改进数据</small></div>
          <input type="checkbox" checked={privacy.allow_improvement_feedback} disabled={!isAdmin} onChange={(e) => setPrivacy({ ...privacy, allow_improvement_feedback: e.target.checked })} /></div>
        <Field label="默认保留天数（0 = 不按天数自动清理）"><input type="number" min={0} max={3650} disabled={!isAdmin} value={privacy.retention_days} onChange={(e) => setPrivacy({ ...privacy, retention_days: Math.max(0, Number(e.target.value) || 0) })} /></Field>
      </div>

      <div className="set-section">
        <div className="set-section-head"><h3>服务器存储</h3><div className="button-row"><button className="button small" disabled={!isAdmin || busy} onClick={load}>重新统计</button><button className="button small primary" disabled={!isAdmin || busy} onClick={save}>{busy ? '保存中…' : '保存策略'}</button></div></div>
        {!isAdmin ? <Callout tone="warn">只有工作区管理员可以查看和管理服务器存储。</Callout> : !data ? <Loading label="正在统计存储占用" /> : (
          <>
            <div className="storage-summary">
              <div><small>工作区数据</small><b>{fmtBytes(data.total_bytes)}</b></div>
              <div><small>磁盘已用</small><b>{data.disk.used_percent.toFixed(1)}%</b><span>{fmtBytes(data.disk.used)} / {fmtBytes(data.disk.total)}</span></div>
              <div><small>剩余空间</small><b>{fmtBytes(data.disk.free)}</b></div>
            </div>
            <div className="table-wrap"><table><thead><tr><th>分类</th><th className="right">文件</th><th className="right">占用</th><th>最早</th><th>最新</th></tr></thead><tbody>
              {data.categories.map((c) => <tr key={c.key}><td>{c.label}</td><td className="right mono">{c.files.toLocaleString()}</td><td className="right mono">{fmtBytes(c.bytes)}</td><td>{fmtDate(c.oldest)}</td><td>{fmtDate(c.newest)}</td></tr>)}
            </tbody></table></div>
            <div className="set-section-head" style={{ marginTop: 18 }}><h3>自动清理</h3></div>
            <div className="set-row"><div><b>磁盘达到阈值时自动清理</b><small>只删除超过保留期的数据；优先清理缓存、上传、回测，最后才清理旧对话</small></div>
              <input type="checkbox" checked={storageCfg.auto_cleanup} disabled={!isAdmin} onChange={(e) => setStorageCfg({ ...storageCfg, auto_cleanup: e.target.checked })} /></div>
            <div className="two-columns even">
              <Field label="触发阈值（已用 %）"><input type="number" min={50} max={99} disabled={!isAdmin} value={storageCfg.threshold_percent} onChange={(e) => setStorageCfg({ ...storageCfg, threshold_percent: Number(e.target.value) || 90 })} /></Field>
              <Field label="目标占用（清理到 %）"><input type="number" min={10} max={95} disabled={!isAdmin} value={storageCfg.target_percent} onChange={(e) => setStorageCfg({ ...storageCfg, target_percent: Number(e.target.value) || 75 })} /></Field>
              <Field label="自动清理的保留天数"><input type="number" min={1} max={3650} disabled={!isAdmin} value={storageCfg.keep_days} onChange={(e) => setStorageCfg({ ...storageCfg, keep_days: Math.max(1, Number(e.target.value) || 30) })} /></Field>
            </div>
            <div className="set-section-head" style={{ marginTop: 18 }}><h3>手动清理</h3></div>
            <div className="filter-row">
              <select value={cleanupCat} onChange={(e) => { setCleanupCat(e.target.value); setPreview(null) }} aria-label="存储分类">
                {data.categories.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
              </select>
              <input type="number" min={0} max={3650} value={keepDays} onChange={(e) => setKeepDays(Math.max(0, Number(e.target.value) || 0))} aria-label="保留天数" style={{ width: 110 }} />
              <span className="hint">天以前的数据</span>
              <button className="button small" disabled={busy} onClick={() => runCleanup(true)}>预览</button>
              <button className="button small danger" disabled={busy} onClick={askCleanup}>执行清理</button>
            </div>
            {preview && <p className="hint">预览：将删除 {preview.deleted.records} 条记录、{preview.deleted.files} 个文件，约 {fmtBytes(preview.deleted.bytes)}。</p>}
          </>
        )}
      </div>
    </>
  )
}
