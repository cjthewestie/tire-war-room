// Supervisor-phone push check against a live backend:
//   uv run python -m warroom.api serve --port 8772   then   node web/e2e/pushcheck.mjs http://localhost:8772
// Phone open but idle -> page-11 event -> phone rings (notification + vibrate + title) without a reload.
// LAN down -> new approval is held, not pushed -> back online -> held push is flushed as 補推.
import { createRequire } from 'node:module'
import { homedir } from 'node:os'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8000'
const results = []
const check = (n, ok, det = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${det ? `  — ${det}` : ''}`) }
const errors = []

const b = await pw.chromium.launch()
const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, timezoneId: 'Asia/Taipei' })
await ctx.grantPermissions(['notifications'], { origin: BASE })
// Record pushes: the headless browser has no notification centre or vibration motor.
await ctx.addInitScript(() => {
  window.__push = []
  class FakeNotification { constructor(title, opts) { window.__push.push({ kind: 'notify', title, body: opts?.body ?? '' }) } }
  FakeNotification.permission = 'granted'
  FakeNotification.requestPermission = async () => 'granted'
  window.Notification = FakeNotification
  navigator.vibrate = (p) => { window.__push.push({ kind: 'vibrate', p }); return true }
})
const ph = await ctx.newPage()
ph.on('pageerror', (e) => errors.push(e.message))
const api = (path, body) => ph.evaluate(async ([p, bd]) => (await fetch(p, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bd) })).status, [path, body])
const pushes = () => ph.evaluate(() => window.__push)
const waitPending = async (sel, n = 120) => { for (let i = 0; i < n; i++) { if ((await ph.locator(sel).count()) > 0) return true; await ph.waitForTimeout(1000) } return false }

await ph.goto(BASE + '/phone', { waitUntil: 'networkidle' })
await api('/api/sim/network', { network: 'online' })
await api('/api/sim/control', { action: 'scenario', scenario: 'normal' })
await ph.waitForTimeout(1500)
check('arm button shown', (await ph.locator('.ph-arm').count()) === 1)
await ph.click('.ph-arm')
check('alerts armed', (await ph.locator('.ph-armed').count()) === 1, await ph.locator('.ph-armed').innerText().catch(() => ''))
const before = (await pushes()).length

// 1) online: approval arrives while the phone is just sitting there
await api('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })
await api('/api/sim/control', { action: 'speed', speed: 30 })
check('approval card arrived', await waitPending('.ph-opt'))
await ph.waitForTimeout(500)
const p1 = (await pushes()).slice(before)
check('system notification fired', p1.some((x) => x.kind === 'notify'), p1.find((x) => x.kind === 'notify')?.title)
check('vibrate fired', p1.some((x) => x.kind === 'vibrate'))
check('tab title shows pending count', /^\(\d+\) 待核准/.test(await ph.title()), await ph.title())

// 2) LAN down: a fresh approval is held; recovery flushes it
await api('/api/sim/network', { network: 'lan_down' })
await api('/api/sim/control', { action: 'scenario', scenario: 'normal' })
await ph.waitForTimeout(1500)
const mark = (await pushes()).length
await api('/api/sim/control', { action: 'scenario', scenario: 'press7_temp_drop' })
await api('/api/sim/control', { action: 'speed', speed: 30 })
const held = await waitPending('.ph-banner:has-text("推播暫存中")')
check('push held while LAN down', held, await ph.locator('.ph-banner').first().innerText().catch(() => ''))
check('no notification while LAN down', !(await pushes()).slice(mark).some((x) => x.kind === 'notify'))
await api('/api/sim/network', { network: 'online' })
await ph.waitForTimeout(3000)
const p2 = (await pushes()).slice(mark)
const flush = p2.find((x) => x.kind === 'notify')
check('held push flushed on recovery', !!flush && flush.body.includes('補推'), flush ? `${flush.title} / ${flush.body.slice(0, 60)}` : '(none)')
check('held banner cleared', (await ph.locator('.ph-banner:has-text("推播暫存中")').count()) === 0)

check('no page errors', errors.length === 0, errors.join(' | '))
await b.close()
const pass = results.filter(Boolean).length
console.log(`\n${pass}/${results.length} passed`)
process.exit(pass === results.length ? 0 : 1)
