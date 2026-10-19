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

// Dev: `pnpm dev` on :5173 proxies /api and /ws to the FastAPI server on :8000.
// The backend treats loopback callers as the on-box supervisor seat, so the dev server binds to
// localhost only, and the proxy forwards the real client address (xfwd -> X-Forwarded-For): a LAN
// client reaching Vite (WARROOM_DEV_HOST=0.0.0.0 to test the phone page on a real phone) is then
// a remote caller and needs the supervisor PIN, exactly as it would hitting FastAPI directly.
const devHost = process.env.WARROOM_DEV_HOST || 'localhost'
// Build: dist/index.html (war-room dashboard) + dist/phone.html (supervisor phone), served by FastAPI.
export default defineConfig({
  plugins: [react(), phoneRoute()],
  server: {
    host: devHost,
    proxy: {
      '/api': { target: 'http://localhost:8000', xfwd: true },
      '/ws': { target: 'ws://localhost:8000', ws: true, xfwd: true },
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
