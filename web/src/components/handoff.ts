/**
 * 把别的页面里的内容交给 AI 策略室：策略页的「AI 生成」、编辑器与回测详情的「用 AI 改进」、
 * 总览的输入框都走这里。内容暂存在 sessionStorage，策略室新对话页读取后清掉。
 */
import type { LabKind } from '../apiLab'

export interface LabDraft {
  idea: string
  /** 研究类型：因子研究时 code 是因子表达式；模型 / 组合 / 执行研究时 code 是 JSON 规格 */
  kind?: LabKind
  title?: string
  code?: string
  securities?: string[]
  timeframe?: string
  pins?: { kind?: string; title: string; text: string }[]
  /** 显示在输入框上方的「已附带：…」 */
  attached?: string
}

const KEY = 'qa.lab.draft'

export function openInLab(d: LabDraft) {
  try { sessionStorage.setItem(KEY, JSON.stringify(d)) } catch { /* 隐私模式：只带过去文字 */ }
  window.location.hash = '#/lab'
}

export function peekLabDraft(): LabDraft | null {
  try {
    const raw = sessionStorage.getItem(KEY)
    return raw ? (JSON.parse(raw) as LabDraft) : null
  } catch {
    return null
  }
}

export function clearLabDraft() {
  try { sessionStorage.removeItem(KEY) } catch { /* 忽略 */ }
}
