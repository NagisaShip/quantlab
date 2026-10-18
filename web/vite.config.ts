import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.QA_WEB_PORT || 5173),
    proxy: {
      // QA_API_TARGET 让第二个前端连到隔离的测试后端（不影响默认 8000）
      '/api': { target: process.env.QA_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
      // 介绍页底部的 API 文档链接
      '/docs': { target: process.env.QA_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
      '/openapi.json': { target: process.env.QA_API_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // 单个 chunk 超过 1MB 时 esbuild 会走临时文件，Windows 上删除临时文件可能被拒绝导致构建失败
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('lightweight-charts')) return 'charts'
            if (/[\\/]node_modules[\\/]mermaid[\\/]/.test(id)) return undefined // 保留 Mermaid 自己的拆分
            // Mermaid / KaTeX 及其依赖只在对话里遇到图和公式时按需加载：按包拆开，既不进首屏，也不会凑出超过 1MB 的 chunk
            const pkg = id.split(/[\\/]node_modules[\\/]/).pop()!.split(/[\\/]/)
            const name = pkg[0].startsWith('@') ? `${pkg[0]}-${pkg[1]}` : pkg[0]
            if (/^(react|react-dom|scheduler|@fontsource)/.test(name)) return 'vendor'
            return `vendor-${name.replace(/^@/, '')}`
          }
          // 研究页之间会互相引用卡片/终端组件。把这一组固定到同一个研究 chunk，
          // 避免 Rollup 为每页单独建 chunk 后形成 page-lab ↔ page-* 的循环。
          const page = id.match(/[\\/]src[\\/]pages[\\/]([^\\/.]+)/)
          if (page) {
            const research = new Set(['lab', 'data', 'DataCenter', 'Factors', 'ModelLib', 'Portfolio', 'Execution', 'StrategyLab', 'Validation', 'LifecycleView', 'Assemble'])
            const automation = new Set(['LongRunning', 'NightShift'])
            if (research.has(page[1])) return 'page-research'
            if (automation.has(page[1])) return 'page-automation'
            return `page-${page[1]}`
          }
        },
      },
    },
  },
})
