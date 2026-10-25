import assert from 'node:assert/strict'
import test from 'node:test'
import { deriveAgentStatus, messageEvidence, messageTag, evidenceProbes } from '../web/src/components/chat/status.ts'

const tool = (seq, name, status, result = {}, error = '') => ({
  seq, role: 'tool', kind: 'tool', content: '',
  data: { name, status, preview: JSON.stringify(result), error },
})
const failedImport = tool(2, 'workflow_import_data', 'error', {}, '名称须以.csv结尾')
const imported = tool(3, 'workflow_import_data', 'ok', { name: 'demo.csv', sha256: 'a'.repeat(64), rows: 6 })
const chart = tool(4, 'local_chart', 'ok', {
  path: 'C:\\project\\demo.png', bytes: 1234, url: '/api/local/files/image?path=C%3A%5Cproject%5Cdemo.png',
})
const state = (messages, status = 'idle') => deriveAgentStatus({ project: { status }, messages })

test('a recovered import and local chart are real output evidence', () => {
  const result = state([failedImport, imported, chart])
  assert.equal(result.label, '已生成')
  assert.equal(result.evidence.length, 2)
  assert.equal(result.evidence[1].id, 'C:\\project\\demo.png')
})

test('pending approval takes precedence over a recoverable tool failure', () => {
  assert.equal(state([failedImport], 'awaiting_user').state, 'waiting')
})

test('unresolved failure and failed artifact tools do not become success', () => {
  assert.equal(state([failedImport]).state, 'failed')
  assert.equal(messageEvidence({ ...chart, data: { ...chart.data, status: 'error' } }).length, 0)
})

test('plain assistant claims, truncated previews and untrusted URLs are not artifacts', () => {
  assert.equal(messageEvidence({ seq: 1, role: 'assistant', kind: 'text', content: '图表已生成 demo.png' }).length, 0)
  assert.equal(messageEvidence({ ...chart, data: { ...chart.data, preview: '{"path":' } }).length, 0)
  assert.equal(messageEvidence(tool(1, 'local_chart', 'ok', { path: 'a', bytes: 1, url: 'https://example.com/a.png' })).length, 0)
  assert.equal(messageEvidence(tool(1, 'unrelated', 'ok', JSON.parse(chart.data.preview))).length, 0)
})

test('previous-turn artifacts cannot hide a new failure', () => {
  assert.equal(state([chart, { seq: 5, role: 'user', kind: 'text', content: '再次运行' }, { ...failedImport, seq: 6 }]).state, 'failed')
})

test('running work retains generating status even after a chart is ready', () => {
  assert.equal(state([chart], 'running').state, 'generating')
})

test('durable Team identity is not completion evidence while waiting for reconciliation', () => {
  const message = { seq: 2, role: 'tool', kind: 'team', content: '', data: { run_id: 'team-run', runtime_run_id: 'team-root-team-run', status: 'waiting' } }
  const result = state([message])
  assert.equal(result.label, '等待核对')
  assert.equal(result.artifacts, false)
  assert.equal(messageTag(message).label, '等待核对')
  assert.deepEqual(evidenceProbes(result.evidence), [{ key: 'team:team-run', url: '/api/agents/team-runs/team-run', kind: 'team' }])
  assert.equal(deriveAgentStatus({ project: { status: 'idle' }, messages: [message], live: { 'team:team-run': 'canceling' } }).label, '取消中')
  assert.equal(messageTag(message, { live: { 'team:team-run': 'canceling' } }).label, '取消中')
  assert.equal(messageTag(message, { live: { 'team:team-run': 'done' } }).label, '已生成')
})


test('temporary model admission is queued rather than asking for human input', () => {
  const result = deriveAgentStatus({ project: { status: 'awaiting_user', runtime_wait_reason: 'admission' }, messages: [] })
  assert.equal(result.state, 'queued')
  assert.equal(result.detail, '等待模型空闲')
})
