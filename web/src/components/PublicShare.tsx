import { useEffect, useState } from 'react'
import { collab, type PublicShareType } from '../apiCollab'
import { appConfirm } from './dialog'
import QRCode from './QRCode'
import { Modal } from './ui'
import type { PageProps } from '../pages/types'

const TYPE_LABEL: Record<PublicShareType, string> = {
  group: '群组', user: '用户主页', post: '帖子', strategy: '策略',
}

export function shareUrl(token: string): string {
  const url = new URL(window.location.href)
  url.hash = `/share/${token}`
  return url.toString()
}

export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text)
  const el = document.createElement('textarea')
  el.value = text
  el.style.position = 'fixed'
  el.style.opacity = '0'
  document.body.appendChild(el)
  el.select()
  document.execCommand('copy')
  el.remove()
}

export function SharePublicButton({ type, id, title, notify, compact = false }: {
  type: PublicShareType
  id: string
  title?: string
  notify: PageProps['notify']
  compact?: boolean
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className={`button small share-public-trigger${compact ? ' compact' : ''}`}
        onClick={() => setOpen(true)} title={`生成${TYPE_LABEL[type]}公开分享链接与二维码`}>
        <span aria-hidden>⌁</span>{compact ? '分享' : `分享${TYPE_LABEL[type]}`}
      </button>
      {open && <PublicShareDialog type={type} id={id} title={title} notify={notify} onClose={() => setOpen(false)} />}
    </>
  )
}

export function PublicShareDialog({ type, id, title, notify, onClose }: {
  type: PublicShareType
  id: string
  title?: string
  notify: PageProps['notify']
  onClose: () => void
}) {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [token, setToken] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    setState('loading')
    setError('')
    setToken('')
    collab.createPublicShare(type, id).then((r) => {
      if (!alive) return
      setToken(r.share.token)
      setState('ready')
    }).catch((e: Error) => {
      if (!alive) return
      setError(e.message)
      setState('error')
    })
    return () => { alive = false }
  }, [type, id])
  const url = token ? shareUrl(token) : ''
  const copy = async () => {
    try { await copyText(url); notify('公开链接已复制') } catch (e) { notify((e as Error).message, true) }
  }
  const revoke = async () => {
    if (!await appConfirm('撤销后，这个二维码和旧链接会立即失效。确定撤销？', { danger: true })) return
    setBusy(true)
    try {
      await collab.revokePublicShare(token)
      notify('公开分享已撤销')
      onClose()
    } catch (e) { notify((e as Error).message, true) } finally { setBusy(false) }
  }

  return (
    <Modal title={`分享${TYPE_LABEL[type]}`} onClose={onClose} width={520}
      footer={<><button type="button" className="button" onClick={onClose}>关闭</button>{token && <button type="button" className="button danger" disabled={busy} onClick={revoke}>撤销链接</button>}</>}>
      <div className="public-share-dialog">
        <p className="share-dialog-lead">未登录的用户也能打开这个链接查看受限预览；登录后可进入完整{TYPE_LABEL[type]}。</p>
        {title && <div className="share-dialog-title">{title}</div>}
        {state === 'error' ? <div className="callout fail">{error}</div> : state === 'loading' ? <div className="share-qr-skeleton" /> : (
          <>
            <div className="share-qr-stage">
              <QRCode value={url} size={196} label={`${TYPE_LABEL[type]}分享二维码`} />
              <div className="share-qr-copy">
                <span className="share-qr-eyebrow">扫码在手机上打开</span>
                <b>{title || 'QuantLab 公开分享'}</b>
                <code>{url.replace(/^https?:\/\//, '')}</code>
              </div>
            </div>
            <div className="share-link-row">
              <input readOnly value={url} aria-label="公开分享链接" onFocus={(e) => e.currentTarget.select()} />
              <button type="button" className="button small primary" onClick={copy}>复制链接</button>
            </div>
            <div className="share-privacy-note"><span aria-hidden>◈</span> 预览不会暴露私聊、群消息、任务、邮箱或策略源码。链接可随时撤销。</div>
          </>
        )}
      </div>
    </Modal>
  )
}
