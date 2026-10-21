// Live smoke against the real FastAPI backend serving web/dist:
//   uv run python -m warroom.api serve --port 8765   then   node web/e2e/live.mjs http://localhost:8765
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8000'
const SCENARIO = process.argv[3] ?? 'press7_temp_drop'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/screenshots')
const results = []
const check = (n, ok, d = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${d ? `  — ${d}` : ''}`) }
const errors = []

const b = await pw.chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, timezoneId: 'Asia/Taipei' })
const d = await ctx.newPage()
d.on('pageerror', (e) => errors.push(e.message))
await d.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })
await d.waitForSelector('.mode.live', { timeout: 10000 }).catch(() => {})
check('auto mode picked live backend', /連線中|模型未連線/.test(await d.locator('.mode').innerText()) && (await d.locator('.mode').getAttribute('class')).includes('live'), await d.locator('.mode').innerText())
check('snapshot rendered', (await d.locator('body').innerText()).includes('訂單進度'))
const r = await d.evaluate(async (sc) => {
  const post = (body) => fetch('/api/sim/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then((x) => x.status)
  return [await post({ action: 'scenario', scenario: sc }), await post({ action: 'speed', speed: 30 })]
}, SCENARIO)
check('sim control accepted', r.every((s) => s === 200), r.join(','))
let pending = false
for (let i = 0; i < 90 && !pending; i++) { await d.waitForTimeout(1000); pending = (await d.locator('.btn-approve').count()) > 0 }
const t = await d.locator('body').innerText()
check('event reached focus panel with pending approval', pending, t.match(/異常：[^\n]*/)?.[0] ?? '(no focus)')
await d.screenshot({ path: `${OUT}/live-dashboard-1440.png`, fullPage: true })
const ph = await ctx.newPage()
await ph.setViewportSize({ width: 390, height: 844 })
await ph.goto(BASE + '/phone', { waitUntil: 'networkidle' })
await ph.waitForSelector('.ph-card', { timeout: 10000 })
const hasCard = (await ph.locator('.ph-opt').count()) > 0
check('phone shows approval card', hasCard, (await ph.locator('.ph-card').first().innerText()).slice(0, 120).replace(/\n/g, ' | '))
await ph.screenshot({ path: `${OUT}/live-phone-390.png`, fullPage: true })
if (hasCard) {
  await ph.click('.ph-opt >> nth=0')
  await ph.click('.ph-approve')
  await ph.waitForTimeout(2500)
  const after = await ph.locator('body').innerText()
  check('phone approve accepted', !after.includes('請先選擇') && !after.includes('失敗'), after.slice(0, 160).replace(/\n/g, ' | '))
}
await d.click('text=人力排程')
await d.waitForTimeout(2500)
const s = await d.locator('body').innerText()
check('schedule tab renders gantt', (await d.locator('.gantt svg').count()) === 1)
check('timeline has rows', (await d.locator('.tl-row').count()) > 0, `${await d.locator('.tl-row').count()} rows`)
await d.screenshot({ path: `${OUT}/live-schedule-1440.png`, fullPage: true })
check('no page errors', errors.length === 0, errors.slice(0, 3).join(' || '))
await b.close()
console.log(`\n${results.filter(Boolean).length}/${results.length} live checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
