import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

// Dev: `npm run dev` on :5173 proxies /api and /ws to the FastAPI server on :8000.
// Build: dist/index.html (war-room dashboard) + dist/phone.html (supervisor phone), served by FastAPI.
export default defineConfig({
  plugins: [react()],
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
        main: resolve(__dirname, 'index.html'),
        phone: resolve(__dirname, 'phone.html'),
      },
    },
  },
})
