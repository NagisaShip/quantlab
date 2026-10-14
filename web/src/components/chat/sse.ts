/**
 * 基于 fetch 的 SSE 读取器。
 * 不用 EventSource：它不能带自定义头（工作区、跨站防护头）。
 */
import { openStream } from '../../http'

export async function streamSSE<T>(path: string, onData: (data: T) => void, signal?: AbortSignal): Promise<'done' | 'gone' | 'closed'> {
  const res = await openStream(path, signal)
  const reader = res.body?.getReader()
  if (!reader) return 'closed'
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) return 'closed'
    buf += decoder.decode(value, { stream: true })
    let cut: number
    while ((cut = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, cut)
      buf = buf.slice(cut + 2)
      let event = 'message'
      const lines: string[] = []
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''))
      }
      if (event === 'done' || event === 'gone') {
        reader.cancel().catch(() => undefined)
        return event
      }
      if (lines.length) {
        try { onData(JSON.parse(lines.join('\n')) as T) } catch { /* 跳过坏块 */ }
      }
    }
  }
}
