import { currentWorkspace } from '../../http'
import type { Flow, Report, Saved } from './types'

export type WorkspaceDraft = { flow: Flow; record: Saved | null; selected: string; dirty: boolean; report: Report | null; view: 'canvas' | 'steps'; viewport: { x: number; y: number; zoom: number }; updated: number }
const key = () => `quantlab.workflow.draft.v2.${currentWorkspace()}`
const drafts = new Map<string, WorkspaceDraft>()
export function readDraft(): WorkspaceDraft | null {
  if(drafts.has(key())) return drafts.get(key())!
  try { return JSON.parse(sessionStorage.getItem(key()) || 'null') } catch { return null }
}
export function writeDraft(value: WorkspaceDraft) {
  drafts.set(key(),value)
  try { sessionStorage.setItem(key(), JSON.stringify(value)) } catch { /* Current in-memory work remains available when storage is full. */ }
}
export function applyDefinition(node: string, kind: string, version: string) {
  const value = readDraft()
  if (value) writeDraft({ ...value, dirty: true, updated: Date.now(), flow: { ...value.flow, nodes: value.flow.nodes.map(n => n.id === node ? { ...n, kind, version } : n) } })
}
