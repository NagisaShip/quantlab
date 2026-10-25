import { useEffect, useRef, useState } from 'react'
import { lab } from '../../apiLab'

type Run = Awaited<ReturnType<typeof lab.runtime>>['run']
export type RecoveryStep = { id: string; kind: string; payload: Record<string, any> }
export type RecoveryResolution =
  | { kind: 'tool'; step_id: string; signal_id: string; outcome: 'succeeded' | 'absent'; result?: unknown }
  | { kind: 'model'; step_id: string; no_charge_confirmed?: boolean; usage?: { input_tokens: number; output_tokens: number; cache_read_input_tokens?: number; cache_creation_input_tokens?: number } }

export function RuntimeRecovery({ projectId, runId, running, revision, canResolve, onChanged, onError }: {
  projectId: string; runId?: string; running: boolean; revision: number; canResolve: boolean
  onChanged: () => Promise<unknown>; onError: (message: string) => void
}) {
  const [run, setRun] = useState<Run | null>(null)
  useEffect(() => {
    let active = true
    if (!runId || running) { setRun(null); return }
    lab.runtime(projectId, runId).then(({ run }) => { if (active) setRun(run) }).catch(() => { if (active) setRun(null) })
    return () => { active = false }
  }, [projectId, runId, running, revision])
  const step = run?.steps.find((step) => step.state === 'RECONCILING')
  const resumable = run?.state === 'RUNNING' && run.steps.some((step) => ['READY', 'WAITING_ADMISSION', 'IN_FLIGHT'].includes(step.state))
  if (!run || running || (!step && !resumable)) return null
  const resume = async () => { await lab.resumeRuntime(projectId, runId); await onChanged() }
  const resolve = async (payload: RecoveryResolution) => {
    let state: string
    if (payload.kind === 'tool') {
      state = (await lab.resolveEffect(projectId, { step_id: payload.step_id, signal_id: payload.signal_id,
        outcome: payload.outcome, result: payload.result }, runId)).state
    } else {
      state = (await lab.resolveModelUsage(projectId, { step_id: payload.step_id, usage: payload.usage,
        no_charge_confirmed: payload.no_charge_confirmed }, runId)).state
    }
    if (!['COMPLETED', 'CANCELED', 'FAILED'].includes(state)) await lab.resumeRuntime(projectId, runId)
    await onChanged()
  }
  return <RecoveryControls step={step} canResolve={canResolve} onResume={resume} onResolve={resolve} onError={onError} />
}

export function RecoveryControls({ step, canResolve, onResume, onResolve, onError, confirmLabel = '确认并继续' }: {
  step?: RecoveryStep; canResolve: boolean; onResume?: () => Promise<unknown>
  onResolve: (payload: RecoveryResolution) => Promise<unknown>; onError: (message: string) => void; confirmLabel?: string
}) {
  const [busy, setBusy] = useState(false)
  const [verified, setVerified] = useState(false)
  const [outcome, setOutcome] = useState<'succeeded' | 'absent'>('succeeded')
  const [result, setResult] = useState('')
  const [inputTokens, setInputTokens] = useState('')
  const [outputTokens, setOutputTokens] = useState('')
  const [cacheRead, setCacheRead] = useState('0')
  const [cacheWrite, setCacheWrite] = useState('0')
  const [noCharge, setNoCharge] = useState(false)
  const signal = useRef(crypto.randomUUID())
  useEffect(() => {
    setVerified(false); setResult(''); setInputTokens(''); setOutputTokens(''); setNoCharge(false)
    setCacheRead('0'); setCacheWrite('0')
    signal.current = crypto.randomUUID()
  }, [step?.id])
  if (!step && !onResume) return null
  async function resume() {
    setBusy(true)
    try { await onResume?.() } catch (error) { onError((error as Error).message) }
    finally { setBusy(false) }
  }
  async function resolve() {
    if (!step) return
    setBusy(true)
    try {
      if (step.kind === 'tool') {
        let confirmed: unknown = result
        try { confirmed = JSON.parse(result) } catch { /* Plain text results remain text. */ }
        await onResolve({ kind: 'tool', step_id: step.id, signal_id: signal.current, outcome,
          result: outcome === 'succeeded' ? confirmed : undefined })
      }
      else await onResolve({ kind: 'model', step_id: step.id, ...(noCharge ? { no_charge_confirmed: true } : {
        usage: { input_tokens: Number(inputTokens), output_tokens: Number(outputTokens),
          cache_read_input_tokens: Number(cacheRead), cache_creation_input_tokens: Number(cacheWrite) } }) })
    } catch (error) { onError((error as Error).message) }
    finally { setBusy(false) }
  }
  return <section className="lab-runtime-recovery" aria-label="恢复执行">
    {!step ? <button className="button small" disabled={busy} onClick={resume}>恢复执行</button> : <>
      <strong>{step.kind === 'tool' ? '上一次操作的结果待核对' : '模型用量待核对'}</strong>
      {!canResolve ? <p>等待管理员核对后继续。</p> : <>
        {step.kind === 'tool' ? <>
          <details><summary>查看待核对操作</summary><pre>{step.payload.name}{'\n'}{JSON.stringify(step.payload.arguments, null, 2)}</pre></details>
          <label>核实结果 <select value={outcome} disabled={busy} onChange={(event) => setOutcome(event.target.value as typeof outcome)}>
            <option value="succeeded">已成功执行</option><option value="absent">未产生效果，可以重试</option>
          </select></label>
          {outcome === 'succeeded' && <label>实际返回结果<textarea value={result} disabled={busy} onChange={(event) => setResult(event.target.value)} /></label>}
        </> : <>
          <label><input type="checkbox" checked={noCharge} disabled={busy} onChange={(event) => setNoCharge(event.target.checked)} />服务商确认未计费</label>
          {!noCharge && <div className="lab-runtime-token-fields">
            <label>未缓存输入 token<input type="number" min="0" step="1" value={inputTokens} disabled={busy} onChange={(event) => setInputTokens(event.target.value)} /></label>
            <label>实际输出 token<input type="number" min="0" step="1" value={outputTokens} disabled={busy} onChange={(event) => setOutputTokens(event.target.value)} /></label>
            <label>缓存读取 token<input type="number" min="0" step="1" value={cacheRead} disabled={busy} onChange={(event) => setCacheRead(event.target.value)} /></label>
            <label>缓存写入 token<input type="number" min="0" step="1" value={cacheWrite} disabled={busy} onChange={(event) => setCacheWrite(event.target.value)} /></label>
          </div>}
        </>}
        <label><input type="checkbox" checked={verified} disabled={busy} onChange={(event) => setVerified(event.target.checked)} />我已在外部系统核实</label>
        <button className="button small" disabled={busy || !verified || (step.kind === 'model' && !noCharge &&
          [inputTokens, outputTokens, cacheRead, cacheWrite].some((value) => value === '' || !Number.isSafeInteger(Number(value)) || Number(value) < 0))}
          onClick={resolve}>{confirmLabel}</button>
      </>}
    </>}
  </section>
}
