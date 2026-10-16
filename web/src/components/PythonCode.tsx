/**
 * 「Python 代码」：因子、预测模型、组合方案、执行方案对应的 Python 实现。
 * 因子是独立可运行的 numpy 代码（算子与平台一致，结果逐元素相同）；模型 / 组合 / 执行是规格 + 用平台引擎复现的代码。
 */
import { useState } from 'react'
import { downloadText } from '../api2'
import { post } from '../http'
import { CodeView } from './CodeEditor'
import { Loading, Modal } from './ui'

export type PyKind = 'factor' | 'model' | 'portfolio' | 'execution'

const LABEL: Record<PyKind, string> = { factor: '因子', model: '模型', portfolio: '组合方案', execution: '执行方案' }

export function PythonCodeButton({ kind, artifact, name, className = 'button small' }: {
  kind: PyKind
  /** 因子表达式，或规格（对象 / JSON 字符串） */
  artifact: unknown
  name?: string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const [code, setCode] = useState<string | null>(null)
  const [error, setError] = useState('')
  const show = async () => {
    setOpen(true)
    setError('')
    setCode(null)
    try {
      const r = await post<{ code: string }>('/api/research/pycode', { kind, artifact, name: name ?? '' })
      setCode(r.code)
    } catch (e) {
      setError((e as Error).message)
    }
  }
  const file = `${(name || kind).replace(/[^\w一-鿿-]+/g, '_').slice(0, 40) || kind}.py`
  return (
    <>
      <button type="button" className={className} onClick={show} title={`查看这个${LABEL[kind]}对应的 Python 代码`}>
        <span className="py-badge" aria-hidden>Py</span>Python 代码
      </button>
      {open && (
        <Modal title={`${LABEL[kind]}「${name || '未命名'}」· Python`} onClose={() => setOpen(false)} width={920}
          footer={code ? <>
            <span className="hint" style={{ marginRight: 'auto' }}>{kind === 'factor' ? '只依赖 numpy，可以脱离平台运行，结果与平台逐元素一致' : '规格原样写成 Python；在 QuantLab 环境里可直接复现评估'}</span>
            <button className="button small" onClick={() => { navigator.clipboard?.writeText(code) }}>复制</button>
            <button className="button small primary" onClick={() => downloadText(file, code, 'text/x-python;charset=utf-8')}>下载 {file}</button>
          </> : undefined}>
          {error ? <div className="callout fail">{error}</div> : code === null ? <Loading label="正在生成" /> : <CodeView code={code} maxHeight={560} />}
        </Modal>
      )}
    </>
  )
}
