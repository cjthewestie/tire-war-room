// Round-4 UI review fixes against a live backend:
//   uv run python -m warroom.api serve --port 8793   then   node web/e2e/r4check.mjs http://localhost:8793
// 1 phone 討論 never runs the plan (changes go through the conversation; no 修改/駁回 buttons since #16); 2 a stopped press shows 扣模待 QC (drawer, Gantt, outcome);
// 3 bad data is visible on the main screen (header chip, ③ note, machine marks, drawer).
import { createRequire } from 'node:module'
import { homedir } from 'node:os'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
let pw
for (const id of ['playwright', 'playwright-core', `${homedir()}/.local/lib/node_modules/@playwright/cli/node_modules/playwright-core`]) { try { pw = require(id); break } catch { /* next */ } }
const BASE = process.argv[2] ?? 'http://localhost:8000'
const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../../docs/screenshots')
const results = []
const check = (n, ok, det = '') => { results.push(ok); console.log(`${ok ? 'PASS' : 'FAIL'}  ${n}${det ? `  — ${det}` : ''}`) }
const errors = []
const b = await pw.chromium.launch()
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, timezoneId: 'Asia/Taipei' })
const d = await ctx.newPage()
d.on('pageerror', (e) => errors.push(e.message))
const post = (body) => d.evaluate((b) => fetch('/api/sim/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }).then((r) => r.ok), body)
await d.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })

// ---- 3 bad data
await post({ action: 'scenario', scenario: 'bad_data' }); await post({ action: 'speed', speed: 30 })
await d.waitForSelector('[data-testid=chip-bad-data]', { timeout: 30000 }).catch(() => {})
await d.waitForTimeout(4000)
const chip = await d.locator('[data-testid=chip-bad-data]').innerText().catch(() => '')
check('header chip counts rejected readings', /驗證層攔下 \d+ 筆壞資料/.test(chip) && chip.includes('未喚醒 AI'), chip)
const note = await d.locator('[data-testid=bad-data-note]').innerText().catch(() => '')
check('③ names the machines', note.includes('3 號硫化機') || note.includes('A 線'), note)
const marks = await d.locator('.mdot.dq').count()
check('machine dots carry a data mark', marks >= 3, `${marks} marked`)
check('header still 異常 0', (await d.locator('[data-testid=tk-fixed]').innerText()).includes('異常 0'))
await d.screenshot({ path: `${OUT}/r4fix-baddata-1440.png` })
await d.locator('.mdot[data-machine=CURE-03]').click()
await d.waitForSelector('[data-testid=drawer-bad-data]', { timeout: 5000 }).catch(() => {})
check('CURE-03 drawer shows rejected count', (await d.locator('[data-testid=drawer-bad-data]').innerText().catch(() => '')).includes('被驗證層攔下'))
await d.keyboard.press('Escape')

// ---- page 11 with the phone
await post({ action: 'scenario', scenario: 'press7_temp_drop' }); await post({ action: 'speed', speed: 20 })
const pctx = await b.newContext({ viewport: { width: 390, height: 844 }, timezoneId: 'Asia/Taipei' })
const ph = await pctx.newPage()
ph.on('pageerror', (e) => errors.push(e.message))
await ph.goto(BASE + '/phone', { waitUntil: 'networkidle' })
const waitOpts = async (n = 1) => { for (let i = 0; i < 240; i++) { if ((await ph.locator('.ph-approval .ph-opt').count()) >= n) return true; await ph.waitForTimeout(1000) } return false }
// round 1: 討論 instead of approving — the note goes to the agent (POST /api/events/{id}/discuss), nothing runs
await waitOpts(2)
check('phone card has only 核准 and 討論', (await ph.locator('.ph-approval button', { hasText: /修改|駁回/ }).count()) === 0 && (await ph.locator('[data-testid=phone-discuss]').count()) === 1)
await ph.locator('[data-testid=phone-discuss]').click()
await ph.fill('[data-testid=phone-discuss-input]', '不要調人')
await ph.screenshot({ path: `${OUT}/r4fix-phone-modify-390.png`, fullPage: true })
await ph.locator('[data-testid=phone-discuss]').click()
await ph.waitForTimeout(4000)
let evs = await d.evaluate(() => fetch('/api/events').then((r) => r.json()))
let ev = evs.find((e) => e.machine_id === 'CURE-07')
check('討論 sent from the phone (confirmation shown)', (await ph.locator('.ph-sent').count()) === 1)
const sv = await d.evaluate(() => fetch('/api/schedule').then((r) => r.json()))
check('schedule unchanged (v1) after 討論', sv.version === 1, `v${sv.version}`)
// approve every later round with the first option (overtime) until closed
for (let i = 0; i < 400; i++) {
  await d.waitForTimeout(1000)
  if (await ph.locator('.ph-approval .ph-opt').count()) await ph.locator('.ph-approval .ph-opt').first().click()
  if (await ph.locator('.ph-approval .ph-approve:enabled').count()) { await ph.locator('.ph-approval .ph-approve:enabled').first().click(); await ph.waitForTimeout(1500) }
  evs = await d.evaluate(() => fetch('/api/events').then((r) => r.json()))
  ev = evs.find((e) => e.machine_id === 'CURE-07')
  if (ev && ['closed', 'safe_stop'].includes(ev.state)) break
}
check('event closed', ev?.state === 'closed', `${ev?.state}: ${ev?.outcome}`)
const m7 = await d.evaluate(() => fetch('/api/machines/CURE-07').then((r) => r.json()))
check('press 7 out of service', m7.state.status === 'out_of_service', m7.state.status)
check('outcome has no superseded cure time', !/硫化 3\d\.\d h/.test(ev?.outcome ?? ''), ev?.outcome)
const vs = (ev?.outcome ?? '').match(/排程 v\d/g) ?? []
check('outcome names one schedule version', vs.length <= 1, vs.join(','))
await d.locator('.mdot[data-machine=CURE-07]').click()
await d.waitForSelector('[data-testid=cure-held]', { timeout: 6000 }).catch(() => {})
const dr = await d.locator('.drawer').innerText()
check('drawer: 扣模待品管, no de-mold ETA', dr.includes('扣模待品管') && !dr.includes('預計出模'), dr.split('\n').filter((x) => x.includes('扣模') || x.includes('出模')).join(' | '))
await d.screenshot({ path: `${OUT}/r4fix-p11-drawer7-1440.png` })
await d.keyboard.press('Escape')
// 硫化機排程 (the Gantt) moved from 人力排程 to 產線監控 (Kyle 10-22)
await d.locator('.tabs button', { hasText: '產線監控' }).click()
await d.waitForTimeout(1500)
check('Gantt marks press 7 stopped', (await d.locator('.cs-held').count()) >= 1)
await d.screenshot({ path: `${OUT}/r4fix-p11-sched-1440.png`, fullPage: true })
await ph.screenshot({ path: `${OUT}/r4fix-phone-final-390.png`, fullPage: true })
check('no page errors', errors.length === 0, errors.join(' | '))
await b.close()
console.log(`${results.filter(Boolean).length}/${results.length} passed`)
process.exit(results.every(Boolean) ? 0 : 1)
