import { useEffect, useState } from 'react'
import { api, type TeamRun } from '../../api/core'
import { RecoveryControls, type RecoveryResolution } from './RuntimeRecovery'

type View = Awaited<ReturnType<typeof api.teamRuntime>>

export function TeamRecovery({ runId, revision, onRun, onError }: {
  runId: string; revision: number; onRun: (run: TeamRun) => void; onError: (message: string) => void
}) {
  const [view, setView] = useState<View | null>(null)
  useEffect(() => {
    let active = true
    const load = () => api.teamRuntime(runId).then((data) => { if (active) setView(data) }).catch(() => { if (active) setView(null) })
    void load()
    const timer = window.setInterval(load, 5000)
    return () => { active = false; window.clearInterval(timer) }
  }, [runId, revision])
  const step = view?.pending[0]
  if (!view || !step) return null
  const resolve = async (payload: RecoveryResolution) => {
    let result: { run: TeamRun }
    if (payload.kind === 'tool') result = await api.resolveTeamEffect(runId, {
      execution_id: step.execution_id, step_id: payload.step_id, signal_id: payload.signal_id,
      outcome: payload.outcome, result: payload.result,
    })
    else result = await api.resolveTeamUsage(runId, { execution_id: step.execution_id, step_id: payload.step_id,
      usage: payload.usage, no_charge_confirmed: payload.no_charge_confirmed })
    if (result.run.can_manage && !['done', 'failed', 'canceled', 'canceling'].includes(result.run.status)) {
      result = await api.resumeTeamRun(runId)
    }
    onRun(result.run)
    setView(await api.teamRuntime(runId))
  }
  return <RecoveryControls step={step} canResolve={view.can_resolve} onResolve={resolve} onError={onError}
    confirmLabel={view.can_manage ? '确认并继续' : '确认核对'} />
}
