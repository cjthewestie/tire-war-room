// Final screenshots for the README: page-11 scenario at the approval moment.
//   ./start.sh   then   node web/e2e/final.mjs http://localhost:8000
// Writes docs/screenshots/final-dashboard.png (1440 wide) and final-phone.png (390 wide).
// Issue #102: 產線監控 shows the approval as one line (到主控台核准) and the card lives on 主控台, so the dashboard shot is
// the 主控台 card. Dataset EV-2: round 1 moves #A-311 to press 6 (round 2, 7 號機停用, is the one that moves it to 9).
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
if (!pw) { console.error('playwright not found (npm i -g @playwright/cli or npx playwright install chromium)'); process.exit(2) }
const BASE = process.argv[2] ?? 'http://localhost:8000'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/screenshots')
const results = []
const check = (n, ok, d = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  — ${d}` : ''}`) }
const errors = []

const b = await pw.chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, timezoneId: 'Asia/Taipei' })
const d = await ctx.newPage()
d.on('pageerror', (e) => errors.push(e.message))
await d.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })
await d.waitForSelector('.mode.live', { timeout: 15000 }).catch(() => {})
check('dashboard on live backend', (await d.locator('.mode.live').count()) === 1 && (await d.locator('[data-testid=mon-answer]').count()) === 1)
const r = await d.evaluate(async () => {
  const post = (body) => fetch('/api/sim/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then((x) => x.status)
  return [await post({ action: 'scenario', scenario: 'press7_temp_drop' }), await post({ action: 'speed', speed: 30 }), await post({ action: 'play' })]
})
check('page-11 scenario loaded', r.every((s) => s === 200), r.join(','))
let pending = false
for (let i = 0; i < 120 && !pending; i++) { await d.waitForTimeout(1000); pending = (await d.locator('[data-testid=monitor-pending]').count()) > 0 }
await d.evaluate(() => fetch('/api/sim/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'pause' }) })).catch(() => {})
await d.waitForTimeout(1500)
const t = await d.locator('body').innerText()
const line = await d.locator('[data-testid=monitor-pending]').innerText().catch(() => '')
check('approval pending on 產線監控: one line to 主控台', pending && line.includes('7 號硫化機') && line.includes('到主控台核准'), line.replace(/\n/g, ' | '))
check('schedule impact names #A-311 to press 6', /排程影響[^\n]*#A-311 改 6 號/.test(t), t.match(/排程影響[^\n]*/)?.[0] ?? '(no impact line)')
await d.locator('[data-testid=monitor-pending-go]').click()
await d.waitForSelector('[data-testid=plan-area] [data-testid=approval-visual]', { timeout: 10000 }).catch(() => {})
check('主控台 shows the approval card with 核准', (await d.locator('[data-testid=plan-area] [data-testid=visual-approve]').count()) >= 1)
await d.waitForTimeout(800)
await d.screenshot({ path: `${OUT}/final-dashboard.png`, fullPage: true })
const ph = await ctx.newPage()
await ph.setViewportSize({ width: 390, height: 844 })
await ph.goto(BASE + '/phone', { waitUntil: 'networkidle' })
await ph.waitForSelector('.ph-card', { timeout: 15000 }).catch(() => {})
const card = (await ph.locator('body').innerText())
check('phone shows approval options', (await ph.locator('.ph-opt').count()) > 0, card.slice(0, 160).replace(/\n/g, ' | '))
check('phone card carries the 28.5 h cure', card.includes('28.5'))
await ph.screenshot({ path: `${OUT}/final-phone.png`, fullPage: true })
check('no page errors', errors.length === 0, errors.slice(0, 3).join(' || '))
await b.close()
console.log(`\n${results.filter(Boolean).length}/${results.length} checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
