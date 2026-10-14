import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // 单个 chunk 超过 1MB 时 esbuild 会走临时文件，Windows 上删除临时文件可能被拒绝导致构建失败
        manualChunks(id) {
          if (id.includes('node_modules')) return id.includes('lightweight-charts') ? 'charts' : 'vendor'
          const page = id.match(/[\\/]src[\\/]pages[\\/]([^\\/.]+)/)
          if (page) return `page-${page[1]}`
        },
      },
    },
  },
})
