import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { defineConfig, type Plugin } from 'vite'

// /phone -> phone.html in dev and preview (FastAPI does the same in production).
const phoneRoute = (): Plugin => {
  const rewrite = (req: { url?: string }, _res: unknown, next: () => void) => {
    if (req.url && /^\/phone\/?(\?.*)?$/.test(req.url)) req.url = req.url.replace(/^\/phone\/?/, '/phone.html')
    next()
  }
  return {
    name: 'phone-route',
    configureServer(s) { s.middlewares.use(rewrite) },
    configurePreviewServer(s) { s.middlewares.use(rewrite) },
  }
}

// Dev: `npm run dev` on :5173 proxies /api and /ws to the FastAPI server on :8000.
// Build: dist/index.html (war-room dashboard) + dist/phone.html (supervisor phone), served by FastAPI.
export default defineConfig({
  plugins: [react(), phoneRoute()],
  server: {
    host: true,
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, 'index.html'),
        phone: resolve(import.meta.dirname, 'phone.html'),
      },
    },
  },
})
