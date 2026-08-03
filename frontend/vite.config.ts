import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The built bundle is served by FastAPI at /app (see app/miniapp/static.py),
// so asset URLs must be relative to that base.
// Backend the dev server proxies to. Override with VITE_API_TARGET when the
// API runs on another port (e.g. alongside an already-running instance).
const API_TARGET = process.env.VITE_API_TARGET ?? 'http://127.0.0.1:8001'

export default defineConfig({
  plugins: [react()],
  base: '/app/',
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
      '/api/miniapp/ws': { target: API_TARGET.replace(/^http/, 'ws'), ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
