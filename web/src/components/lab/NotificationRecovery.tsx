import { useEffect, useRef, useState } from 'react'
import { get, post } from '../../http'
import { RecoveryControls, type RecoveryResolution, type RecoveryStep } from './RuntimeRecovery'

type State = { run: { state: string; steps: (RecoveryStep & { state: string })[] }; can_resume: boolean; can_resolve: boolean }
export function NotificationRecovery({ recordId, onChanged, onError }: { recordId: string; onChanged: () => Promise<unknown>; onError: (message: string) => void }) {
  const [state, setState] = useState<State | null>(null)
  const changed = useRef(onChanged)
  changed.current = onChanged
  const base = `/api/long-running/records/${encodeURIComponent(recordId)}/runtime`
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let wasPending = false
    const load = async () => {
      try {
        const value = await get<State>(base)
        if (!active) return
        setState(value)
        if (['COMPLETED', 'FAILED', 'CANCELED'].includes(value.run.state)) {
          if (wasPending) await changed.current()
          return
        }
        wasPending = true
      } catch { if (active) setState(null) }
      if (active) timer = setTimeout(load, 3000)
    }
    void load()
    return () => { active = false; clearTimeout(timer) }
  }, [base])
  if (!state || ['COMPLETED', 'FAILED', 'CANCELED'].includes(state.run.state)) return null
  const step = state.run.steps.find(value => value.state === 'RECONCILING')
  const resume = async () => { await post(`${base}/resume`, {}); await onChanged(); setState(await get<State>(base)) }
  const resolve = async (payload: RecoveryResolution) => {
    await post(`${base}/${payload.kind === 'tool' ? 'effect-resolution' : 'model-usage'}`, payload)
    if (state.can_resume) await resume()
    else { setState(await get<State>(base)); await onChanged() }
  }
  return <><RecoveryControls step={step} canResolve={state.can_resolve} onResume={state.can_resume ? resume : undefined} onResolve={resolve} onError={onError} />
    {state.can_resume && <button className="button small" onClick={() => { void post(`${base}/cancel`, {}).then(async () => { setState(await get<State>(base)); await onChanged() }).catch(error => onError(String(error))) }}>取消执行</button>}
  </>
}
