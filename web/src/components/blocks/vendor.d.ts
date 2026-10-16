// KaTeX 自带的类型声明没有随 npm 包发布：这里只声明用到的 renderToString 与样式表
declare module 'katex' {
  export function renderToString(tex: string, options?: Record<string, unknown>): string
}
declare module 'katex/dist/katex.min.css'
declare module 'mermaid/dist/mermaid.esm.min.mjs' {
  const mermaid: unknown
  export default mermaid
}
