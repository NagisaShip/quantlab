import { useRef, useState, type DragEvent } from 'react'
import { explainError } from '../../http'
import { chooseFiles, isDesktop } from '../../native'
import { useLocal } from './context'
import { size } from './format'
import { PreviewBlock, Section } from './shared'
import DocumentPreview from './DocumentPreview'

/** 设置 · 本地项目：数据文件流式导入、本机路径引用/复制、目录浏览与权限。 */
export default function FilesPanel() {
  const s = useLocal()
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const locked = s.busy !== ''

  const chooseReference = async () => {
    if (isDesktop()) {
      try {
        const paths = await chooseFiles()
        if (paths[0]) s.setFilePath(paths[0])
      } catch (e) {
        // 原生选择器失败也要给人话提示，不把底层异常原样甩给用户。
        s.notify(explainError(e).title, true)
      }
      return
    }
    fileInput.current?.click()
  }

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    setDragging(false)
    s.dropFiles(event)
  }

  return (
    <Section
      title="数据文件与目录权限"
    >
      <div
        className={`lp-drop${dragging ? ' dragging' : ''}`}
        role="group"
        aria-label="导入数据文件"
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <span className="lp-drop-icon" aria-hidden>⇪</span>
        <b>拖入数据文件</b>
        <p>CSV / TSV / XLSX / XLSM / Parquet / JSONL</p>
        <div className="lp-inline lp-inline-center">
          <button type="button" className="button primary" disabled={locked} onClick={() => fileInput.current?.click()}>
            选择文件并复制
          </button>
          <button type="button" className="button" disabled={locked} onClick={() => void chooseReference()}>
            选择本机引用文件
          </button>
        </div>
        <input
          ref={fileInput}
          hidden
          type="file"
          multiple
          accept=".csv,.tsv,.xlsx,.xlsm,.parquet,.jsonl,.ndjson"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? [])
            e.target.value = ''
            if (files.length) void s.uploadFiles(files)
          }}
        />
      </div>

      <div className="lp-block">
        <div className="lp-block-head"><b>本机路径</b></div>
        <div className="lp-field">
          <span>本机文件或目录路径</span>
          <div className="lp-inline">
            <input
              aria-label="本机数据路径"
              value={s.filePath}
              onChange={(e) => s.setFilePath(e.target.value)}
              placeholder={'例如 D:\\data\\bars.csv，或包含数据文件的目录'}
            />
            <button type="button" className="button small" disabled={locked || !s.filePath.trim()} onClick={() => void s.pickFolder('file')}>
              选择文件夹
            </button>
            <button type="button" className="button small" disabled={locked || !s.filePath.trim()} onClick={() => void s.browsePath()}>
              浏览目录
            </button>
          </div>
        </div>
        <div className="lp-inline">
          <label className="lp-mini-field">
            数据编码
            <input aria-label="数据编码" value={s.encoding} onChange={(e) => s.setEncoding(e.target.value)} />
          </label>
          <button type="button" className="button small" disabled={locked || !s.filePath.trim()} onClick={() => void s.previewFile()}>
            预览字段与样本
          </button>
          <button type="button" className="button small" disabled={locked || !s.filePath.trim()} onClick={() => void s.importFromPath('reference')}>
            引用原文件
          </button>
          <button type="button" className="button small" disabled={locked || !s.filePath.trim()} onClick={() => void s.importFromPath('copy')}>
            复制进项目
          </button>
        </div>
        <div className="lp-inline">
          <button type="button" className="button small" disabled={locked || !s.filePath.trim()} onClick={() => void s.grantPermission('read')}>
            授权所选文件/目录只读
          </button>
          <button type="button" className="button small" disabled={locked || !s.filePath.trim()} onClick={() => void s.grantPermission('write')}>
            授权目录可写
          </button>
        </div>
      </div>

      {/\.(docx|pptx|pdf)$/i.test(s.filePath) && <DocumentPreview key={s.filePath} path={s.filePath}/>}
      <div className="lp-browser">
        <div className="lp-browser-head">
          <span className="lp-browser-title">目录内容</span>
          {s.folder && <code className="lp-browser-path" title={s.folder}>{s.folder}</code>}
          {s.info && s.folder && s.folder !== s.info.root && (
            <button type="button" className="button small" onClick={() => void s.browseInto(s.info!.root)}>返回项目目录</button>
          )}
        </div>
        <div className="lp-filelist" role="group" aria-label="目录中的文件">
          {s.entries.length === 0 && (
            <p className="lp-empty">{s.loading ? '正在读取目录…' : '目录为空，或还没有浏览目录。'}</p>
          )}
          {s.entries.map((entry) => {
            const active = !entry.directory && entry.path === s.filePath
            return (
              <button
                type="button"
                key={entry.path}
                className={`lp-file${active ? ' active' : ''}`}
                title={entry.path}
                onClick={() => {
                  if (entry.directory) void s.browseInto(entry.path)
                  else s.setFilePath(entry.path)
                }}
              >
                <span className="lp-file-name">{entry.directory ? '▸ ' : '· '}{entry.name}</span>
                <small>{entry.directory ? '目录' : size(entry.bytes)}</small>
              </button>
            )
          })}
        </div>
      </div>

      {s.reference && (
        <div className="lp-reference">
          <div className="lp-block-head"><b>已接入的数据源</b></div>
          <div className="lp-inline">
            <input aria-label="引用资源名称" value={s.reference} onChange={(e) => s.setReference(e.target.value)} />
            <button type="button" className="button small" disabled={locked || !s.filePath.trim()} onClick={() => void s.relocateReference()}>
              重新定位 / 确认文件变化
            </button>
            <button type="button" className="button small" onClick={() => s.onNavigate('workflows')}>
              去研究工作流
            </button>
          </div>
          <small>引用文件不会复制进项目；换机器或移动目录后需要重新定位，并核对内容指纹是否变化。</small>
        </div>
      )}

      {s.preview && <PreviewBlock preview={s.preview} onClose={s.clearPreview} />}

      <details className="lp-more">
        <summary>已授权范围（{s.info?.permissions.length ?? 0}）</summary>
        <div className="lp-perms">
          {(s.info?.permissions ?? []).map((grant) => (
            <div className="lp-perm" key={grant.path}>
              <code className="lp-path" title={grant.path}>{grant.path}</code>
              <span className={`lp-pill${grant.mode === 'write' ? ' ok' : ''}`}>{grant.mode === 'write' ? '可写' : '只读'}</span>
              {grant.path !== s.info?.root && (
                <button type="button" className="button small" disabled={locked} onClick={() => void s.revokePermission(grant.path)}>
                  撤销
                </button>
              )}
            </div>
          ))}
          {(s.info?.permissions.length ?? 0) === 0 && <p className="lp-empty">还没有授权范围。</p>}
        </div>
      </details>
    </Section>
  )
}
