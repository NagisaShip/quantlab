/**
 * 左侧栏对话列表（全局常驻）与 AI 研究室页面之间的小通道：
 * - 研究室里对话变了（新建、改名、跑完一轮）→ labChanged() 让侧栏刷新；
 * - 侧栏点「在项目里新建对话」→ 记下文件夹，研究室打开时取走。
 */
import { ASK_EVENT } from '../../components/blocks/protocol'

const REFRESH = 'qa:lab-refresh'
const NEW = 'qa:lab-new'
let pendingFolder: string | null = null

export function labChanged() {
  window.dispatchEvent(new Event(REFRESH))
}

export function onLabChanged(fn: () => void) {
  window.addEventListener(REFRESH, fn)
  return () => window.removeEventListener(REFRESH, fn)
}

/** 侧栏要求新建对话（可带项目文件夹）。 */
export function requestNewChat(folder = '') {
  pendingFolder = folder
  window.dispatchEvent(new Event(NEW))
}

/** 研究室取走待新建的文件夹；没有请求时返回 null。 */
export function takeNewChat(): string | null {
  const f = pendingFolder
  pendingFolder = null
  return f
}

export function onNewChat(fn: () => void) {
  window.addEventListener(NEW, fn)
  return () => window.removeEventListener(NEW, fn)
}

/** 对话里的结构化内容块（选项、确认、表单、「让 AI 解读」）要把一句话发回当前对话。 */
export function onChatAsk(fn: (text: string) => void) {
  const h = (e: Event) => {
    const text = (e as CustomEvent<string>).detail
    if (typeof text === 'string' && text.trim()) fn(text)
  }
  window.addEventListener(ASK_EVENT, h)
  return () => window.removeEventListener(ASK_EVENT, h)
}
