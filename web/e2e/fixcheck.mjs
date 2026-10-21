// Checks the web review fixes against a live backend:
//   uv run python -m warroom.api serve --port 8771   then   node web/e2e/fixcheck.mjs http://localhost:8771
// Phone: picking the other option then 核准 executes that option (no 修改/駁回 buttons since #16), network-tier buttons work live, step plan has no JSON dumps, ⑨ shows p.6.
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
const ctx = await b.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, timezoneId: 'Asia/Taipei' })
const d = await ctx.newPage()
d.on('pageerror', (e) => errors.push(e.message))
await d.goto(BASE + '/#monitor', { waitUntil: 'networkidle' })
await d.waitForSelector('.mode.live', { timeout: 10000 })
check('header has no English mock badge', !(await d.locator('.mode').innerText()).includes('mock'), await d.locator('.mode').innerText())

// network tiers: enabled and effective in live mode
const lan = d.locator('.seg.net button', { hasText: 'LAN 斷' })
check('LAN 斷 button enabled live', await lan.isEnabled())
await lan.click()
await d.waitForTimeout(5000)
check('header shows LAN tier', (await d.locator('.topbar').innerText()).includes('廠內網路斷線'))
await d.screenshot({ path: `${OUT}/fix-lan-down-1440.png` })
await d.locator('.seg.net button', { hasText: '在線' }).click()
await d.waitForTimeout(5000)
check('back online', !(await d.locator('.topbar').innerText()).includes('廠內網路斷線'))

await d.evaluate(async () => {
  const post = (body) => fetch('/api/sim/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  await post({ action: 'scenario', scenario: 'press7_temp_drop' }); await post({ action: 'speed', speed: 30 })
})
const ph = await ctx.newPage()
await ph.setViewportSize({ width: 390, height: 844 })
ph.on('pageerror', (e) => errors.push(e.message))
await ph.goto(BASE + '/phone', { waitUntil: 'networkidle' })

// approve round 1 normally; on the round with options A/B pick A (not the system's B) and 核准
// (the phone card has only 核准 and 討論 — CJ, 2026-10-20: 「agent 計劃就只有核准跟討論」)
let modified = null
for (let round = 0; round < 3 && !modified; round++) {
  let ok = false
  for (let i = 0; i < 240 && !ok; i++) { await ph.waitForTimeout(1000); ok = (await ph.locator('.ph-approval .ph-approve:enabled').count()) > 0 }
  if (!ok) break
  const title = await ph.locator('.ph-approval .ph-title').first().innerText()
  const opts = await ph.locator('.ph-approval .ph-opt').count()
  check(`phone card has no 修改/駁回 buttons (round ${round + 1})`, (await ph.locator('.ph-approval button', { hasText: /修改|駁回/ }).count()) === 0)
  if (opts >= 2) {
    await ph.screenshot({ path: `${OUT}/fix-phone-r2-390.png`, fullPage: true })
    await d.screenshot({ path: `${OUT}/fix-dash-r2-1440.png`, fullPage: true })
    await ph.locator('.ph-approval .ph-opt').first().click()
    await ph.click('.ph-approval .ph-approve')
    modified = title
  } else {
    await ph.click('.ph-approval .ph-approve')
    await ph.waitForTimeout(3000)
  }
}
check('reached a round with A/B options and approved option A', !!modified, modified ?? '')
// wait for the event to close, approving any later round (no options) from the phone
let ev = null
for (let i = 0; i < 300; i++) {
  await d.waitForTimeout(1000)
  if (await ph.locator('.ph-approval .ph-approve:enabled').count()) { await ph.locator('.ph-approval .ph-approve:enabled').first().click(); await ph.waitForTimeout(1500) }
  const evs = await d.evaluate(() => fetch('/api/events').then((r) => r.json()))
  ev = evs.find((e) => e.machine_id === 'CURE-07')
  if (ev && ['closed', 'safe_stop'].includes(ev.state)) break
}
const rejected = (ev?.steps ?? []).filter((s) => s.state === 'rejected')
check('no step rejected after choosing A', rejected.length === 0, rejected.map((s) => `${s.step.tool}:${s.gate?.reasons?.join(';')}`).join(' | '))
const tl = (ev?.timeline ?? []).map((x) => x.text).join('\n')
check('overtime notify executed (round-1 steps are replaced on replan, so read the timeline)', /\[工具\] 加班 →/.test(tl), tl.split('\n').find((x) => x.includes('加班 →')) ?? '')
check('timeline records the changed choice', tl.includes('改選'), tl.split('\n').find((x) => x.includes('改選')) ?? '')
check('no gate interception in timeline', !tl.includes('Extra inputs'))
check('event closed', ev?.state === 'closed', `${ev?.state} ${ev?.outcome ?? ''}`)
await ph.screenshot({ path: `${OUT}/fix-phone-after-390.png`, fullPage: true })

// focus panel after round 2
const focus = await d.locator('.focus').innerText().catch(() => '')
check('focus panel labels the wake reading', focus.includes('喚醒時') || !focus.includes('128 → 122'), focus.split('\n').slice(0, 4).join(' | '))
await d.screenshot({ path: `${OUT}/fix-dash-final-1440.png`, fullPage: true })

// drawer
await d.locator('[data-machine="CURE-07"]').click()
await d.waitForSelector('.drawer .tiles', { timeout: 5000 })
await d.waitForTimeout(800)
const dr = await d.locator('.drawer').innerText()
check('drawer press shows 在模 not 膠料', dr.includes('在模') && !dr.includes('膠料'), dr.split('\n').slice(0, 6).join(' | '))
// after round 2 the press is usually 停用 (H2 → stop): then its tyre is held for QC and has no de-mold time
const held = dr.includes('扣模待品管')
check('drawer time tile in hours (or 扣模待品管 when the press is stopped)', held || /\d+\.\d \/ [\d.]+ h/.test(dr))
check('drawer de-mold has date (or 扣模待品管 when the press is stopped)', held || /預計出模 \d\d\/\d\d \d\d:\d\d/.test(dr))
await d.screenshot({ path: `${OUT}/fix-drawer-cure07-1440.png` })
await d.keyboard.press('Escape')

await d.click('text=人力排程')
await d.waitForTimeout(2500)
await d.evaluate(() => { const p = document.querySelector('details.plan-panel'); if (p) p.open = true })   // ⑥ is folded by default (UX declutter)
await d.waitForTimeout(200)
const plan = await d.locator('.plan-panel').innerText()
check('step plan has no JSON / key=value dumps', !/[{}]|=\S/.test(plan), plan.match(/.*[{}=].*/)?.[0] ?? '')
check('step plan shows Chinese tool labels, no snake_case names', !/[a-z]+_[a-z_]+/.test(plan), plan.match(/.*[a-z]+_[a-z_]+.*/)?.[0] ?? '')
const ids = await d.$$eval('.step-id', (els) => els.map((e) => [e.textContent, e.scrollWidth, e.clientWidth, e.getBoundingClientRect().height]))
// one line at 16 px × 1.5 = 24 px
check('step ids fit one line', ids.every(([, , , h]) => h <= 24), JSON.stringify(ids.slice(-3)))
const cap = await d.locator('.cap-panel').innerText()
check('⑨ shows page-6 numbers', ['5.9', '4.5', '4.9', '13.3 → 9.3', '2.4 → 2.0'].every((x) => cap.includes(x)), cap.replace(/\n/g, ' | ').slice(0, 200))
check('⑨ footnote is Chinese', !/Mon|Steady|shift/.test(cap))
await d.screenshot({ path: `${OUT}/fix-schedule-1440.png`, fullPage: true })
check('no page errors', errors.length === 0, errors.slice(0, 3).join(' || '))
await b.close()
console.log(`\n${results.filter(Boolean).length}/${results.length} fix checks passed`)
process.exit(results.every(Boolean) ? 0 : 1)
