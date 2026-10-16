import { createContext, useContext, useEffect } from 'react'

/**
 * 渲染失败上报：Mermaid 语法错、LaTeX 错、ql 块 JSON 解析失败、SVG 被净化成空……
 * 对话页面提供 reporter，把同一条消息里的错误收集起来交回 Agent，让它自动修正（见 pages/StrategyLab.tsx）。
 * 帖子、报告等没有 Agent 的地方不提供 reporter，错误只在原位显示。
 */
export interface RenderError { kind: 'mermaid' | 'math' | 'block' | 'svg' | 'html'; message: string; source: string }
export type RenderErrorReporter = (e: RenderError) => void

export const RenderErrorCtx = createContext<RenderErrorReporter | null>(null)

/** 组件里出错时调用：error 为空表示没有错误（不上报）。 */
export function useReportRenderError(error: RenderError | null) {
  const report = useContext(RenderErrorCtx)
  const key = error ? `${error.kind}|${error.message}|${error.source.length}` : ''
  useEffect(() => {
    if (report && error) report(error)
    // 同一个错误只报一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [report, key])
}
