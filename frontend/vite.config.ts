import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => ({
  // 生产环境使用 /app/ 前缀（Render部署），开发环境使用 /
  base: mode === 'production' ? '/app/' : '/',
  plugins: [react()],
  build: {
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          antd: ['antd'],
          icons: ['@ant-design/icons'],
          charts: ['recharts'],
          state: ['axios', 'zustand'],
        },
      },
    },
  },
  server: {
    strictPort: true,
    port: 3000,
  },
}))
