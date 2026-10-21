// The 3D follows a demo started on the war room (「▶ Demo 模式」): two tabs in one headless browser, the war room
// drives (continue pressed there 4 s after each checkpoint is ready, each card approved 4 s after it shows), the 3D
// joins by itself and walks the same beats. Records the 3D tab (webm) and the cues the 3D followed.
//   node web/e2e/demo-follow.mjs http://localhost:8170 --out DIR
import { createRequire } from 'node:module'
import { mkdirSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { resolve } from 'node:path'

const require = createRequire(import.meta.url)
function loadPlaywright() {
  for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) {
    try { return require(id) } catch { /* next */ }
  }
  throw new Error('playwright not found')
}
const { chromium } = loadPlaywright()
const args = process.argv.slice(2)
const BASE = args.find((a) => a.startsWith('http')) ?? 'http://localhost:8000'
const oi = args.indexOf('--out')
const OUT = resolve(oi >= 0 ? args[oi + 1] : 'e2e/screenshots')
mkdirSync(OUT, { recursive: true })
let failed = 0
const check = (name, ok, detail = '') => { if (!ok) failed++; console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  — ${detail}` : ''}`) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const post = (p, b) => fetch(`${BASE}${p}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(b) })
const getJSON = (p) => fetch(`${BASE}${p}`).then((r) => r.json()).catch(() => null)

await post('/api/demo/state', { action: 'exit' }).catch(() => {})
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] })
const room = await (await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: 'zh-TW' })).newPage()
const ctx3d = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: 'zh-TW', recordVideo: { dir: OUT, size: { width: 960, height: 600 } } })
const f3 = await ctx3d.newPage()
await f3.goto(`${BASE}/factory?test=1`)
await f3.waitForSelector('[data-testid="f3-conn"].live', { timeout: 30000 })
await room.goto(`${BASE}/`)
await room.waitForSelector('[data-testid="demo-mode-button"]', { timeout: 30000 })
await sleep(2000)
const t0 = Date.now()
await room.locator('[data-testid="demo-mode-button"]').click()
const joined = await (async () => { for (let i = 0; i < 40; i++) { const d = await f3.evaluate(() => window.__demo3d?.()); if (d?.running) return d; await sleep(500) } return null })()
check('the 3D joins a demo started on the war room', !!joined, JSON.stringify(joined?.cue))
const seenAt = new Map(), approved = new Set()
let readyAt = null, lastCue = ''
const shots = new Set()
while (Date.now() - t0 < 8 * 60 * 1000) {
  const s = await getJSON('/api/demo/state')
  if (!s?.running) break
  const now = Date.now()
  if (s.cue_id !== lastCue) { lastCue = s.cue_id; readyAt = null }
  const d3 = await f3.evaluate(() => window.__demo3d?.())
  if (!shots.has(s.cue_id) && d3?.walk?.done) { shots.add(s.cue_id); await f3.screenshot({ path: resolve(OUT, `follow-${String(s.cue_index).padStart(2, '0')}-${s.cue_id}.png`), timeout: 90000 }).catch(() => {}) }
  if (s.cue_id === 'warn' || s.cue_id === 'end') break
  if (s.waiting_for === 'continue') {
    readyAt ??= now
    if (now - readyAt > (s.cue_id === 'worker' || s.cue_id === 'robot' ? 12000 : 5000)) { await room.keyboard.press('Space'); readyAt = null }
  } else readyAt = null
  // the pitch on the war room waits for nothing more: stop once the 3D stood at the board a while
  if (s.cue_id === 'pitch' && d3?.walk?.at === 'board' && d3.walk.done && !shots.has('pitch-done')) { shots.add('pitch-done'); await sleep(6000); break }
  const pend = await getJSON('/api/approvals?status=pending')
  for (const a of Array.isArray(pend) ? pend : []) {
    if (approved.has(a.id)) continue
    if (!seenAt.has(a.id)) seenAt.set(a.id, now)
    if (now - seenAt.get(a.id) < 4000) continue
    approved.add(a.id)
    const opt = a.options?.find((o) => o.recommended)?.id ?? a.options?.[0]?.id ?? null
    const r = await post(`/api/approvals/${a.id}/decision`, { request_id: a.id, decision: 'approve', option_id: opt, via: 'phone', reason: 'demo-follow e2e' })
    console.log(`      ${((now - t0) / 1000).toFixed(0)}s approved ${a.id} → ${r.status}`)
  }
  await sleep(300)
}
const d = await f3.evaluate(() => window.__demo3d?.())
const rows = (d?.log ?? []).map((r) => ({ cue: r.id, planned: r.planned, followed: r.t }))
console.table(rows)
writeFileSync(resolve(OUT, 'follow-log.json'), JSON.stringify({ rows, signals: d?.signals }, null, 2))
const order = rows.map((r) => r.cue).join(',')
check('the 3D followed every story beat in order', ['start', 'rush', 'console', 'event', 'h1', 'verify', 'h2', 'worker', 'robot', 'agent', 'pitch'].every((id, i, a) => i === 0 || order.indexOf(id) > order.indexOf(a[i - 1])), order)
check('the 3D walked to the whiteboard for the pitch', d?.walk?.at === 'board', JSON.stringify(d?.walk))
await post('/api/demo/state', { action: 'exit' }).catch(() => {})
await ctx3d.close()
await browser.close()
console.log(failed ? `\n${failed} FAILED` : '\nall passed')
process.exit(failed ? 1 : 0)
